from __future__ import annotations

import hashlib
import json
import time
import uuid
from copy import deepcopy
from pathlib import Path

from . import git
from .adapters import CLIBackend
from .context import (FINAL_REVIEW_INSTRUCTION, capacity_report, compact_checks,
                      compact_feedback, compact_plan, compact_reviews, compose)
from .contracts import (PLAN, REPLAN, REVIEW, TEXT, WORK, RepairableFormatError, apply_revision,
                        envelope, obj, output_constraints, review_approved, validate,
                        validate_plan, validate_review, validate_review_correction, validate_review_semantics,
                        validate_structure, validate_work, validate_work_semantics)
from .memory import Memory, validate_proposals
from .shared import HookError, Hooks, asset_digest, read_skill, skills_catalog
from .util import (HarnessError, ProcessTimeout, dump, group_alive, lock,
                   read_json, run_process, tail, uid, write_json)


def snapshot_digest(snapshot):
    encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Engine:
    def __init__(self, config, repo, state=None, backend=None, commander="astra"):
        self.config = config
        self.repo = git.repository(Path(repo))
        self.state = Path(state).resolve() if state else self.repo / ".harness"
        git.ignore_state(self.repo)
        self.memory = Memory(self.state / "memory.sqlite3")
        self.backend = backend or CLIBackend(config, self.state)
        self.commander = commander
        self.hooks = Hooks(config)
        self.run = None

    @property
    def directory(self):
        return self.state / "runs" / self.run["id"]

    @property
    def worktree(self):
        return Path(self.run["worktree"])

    def save(self):
        write_json(self.directory / "state.json", self.run)

    def event(self, event, **fields):
        record = {"time": time.time(), "event": event, "run_id": self.run["id"], **fields}
        with (self.directory / "events.jsonl").open("a") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
        print(f"[harness] {event} {fields.get('task_id', '')}", flush=True)

    def checkpoint(self):
        # Model-independent handoff. Full logs remain separate and are never recycled as conversation.
        handoff = {"goal": self.run["goal"], "branch": self.run["branch"], "status": self.run["status"],
                   "accepted": self.run["accepted"], "pending": [t for t in self.run["tasks"] if t["id"] not in self.run["accepted"]],
                   "feedback": self.run.get("feedback"), "expected_head": self.run["expected_head"],
                   "original_tasks": self.run["original_tasks"], "base_commit": self.run["base_commit"],
                   "total_attempts": self.run["total_attempts"], "replan_attempts": self.run["replan_attempts"],
                   "final_history": self.run["final_history"], "plan_revision": len(self.run["plan_history"]),
                   "next": "Resume the harness run; every dispatch creates a fresh agent process."}
        write_json(self.directory / "handoff.json", handoff)
        (self.directory / "handoff.md").write_text("# Run handoff\n\n```json\n" + dump(handoff) + "\n```\n")

    def accepted_context(self):
        # Keep summaries bounded; accepted commits and artifact paths are never removed.
        return {tid: {"commit": item["commit"], "summary": item["summary"][:600],
                      "summary_truncated": len(item["summary"]) > 600,
                      "verification": item["verification"], "details": item["review_artifact"]}
                for tid, item in self.run["accepted"].items()}

    def plan_context(self):
        return compact_plan(self.run["original_tasks"], self.run["tasks"])

    def final_context_capacity(self, tasks=None, original_tasks=None):
        """Preflight a candidate plan without consuming a dispatch or rejecting estimates.

        Required size excludes unknown future summaries/output/path lengths. The
        projection reserves bounded summaries, output excerpts and generous paths;
        only the required lower bound can establish that a plan cannot fit.
        """
        tasks = self.run["tasks"] if tasks is None else tasks
        if original_tasks is None:
            original_tasks = self.run["original_tasks"] or tasks
        dispatch_id = "00000000-0000-0000-0000-000000000000"
        checks = [{"argv": command, "exit_code": 0, "stdout": "", "stderr": "",
                   "log": "", "timed_out": False}
                  for command in self.config.data["verification"]["commands"]]
        required = {
            "role": "reviewer", "kind": "final_review", "task_id": "final", "dispatch_id": dispatch_id,
            "rules": self.config.asset("rules").read_text(),
            "role_skill": read_skill(self.config.asset("skills"), "harness-reviewer"),
            "output_constraints": output_constraints(envelope(REVIEW, "final", dispatch_id)),
            "packet": {"goal": self.run["goal"], **compact_plan(original_tasks, tasks),
                       "accepted": self.accepted_context(), "base_commit": self.run["base_commit"],
                       "head": self.run["expected_head"], "diff_artifact": "", "verification": checks,
                       "tool_constraints": self.tool_constraints(),
                       "prior_reviews": compact_reviews([item for item in self.run.get("final_history", [])
                                                          if item["status"] != "running"]),
                       "instruction": FINAL_REVIEW_INSTRUCTION},
        }
        projected = deepcopy(required)
        packet = projected["packet"]
        runtime = self.config.data["runtime"]
        # Include a UUID-sized suffix as a reserve for distinct recovery artifacts.
        suffix = f"{max(runtime['max_attempts'], runtime['max_total_attempts'])}-{dispatch_id}"
        for task in tasks:
            if task["id"] not in packet["accepted"]:
                folder = self.directory / "verification" / "tasks" / task["id"] / suffix
                packet["accepted"][task["id"]] = {
                    "commit": "0" * len(self.run["base_commit"]), "summary": "x" * 600,
                    "summary_truncated": False, "verification": str(folder / "results.json"),
                    "details": str(folder / "review.json"),
                }
        final_suffix = f"{max(runtime['max_final_attempts'], self.run.get('final_attempts', 0) + 1)}-{dispatch_id}"
        final_folder = self.directory / "verification" / "final" / final_suffix
        packet["diff_artifact"] = str(final_folder / "change.diff")
        for index, check in enumerate(packet["verification"]):
            check.update(stdout="x" * 250, stderr="x" * 250,
                         log=str(final_folder / f"check-{index}"), output_truncated=True)
        report = capacity_report(required, projected, runtime["context_chars"])
        report["assumptions"] = {
            "pending_summary_chars": 600, "verification_output_chars_per_command": 500,
            "verification_commands": len(checks), "tasks": len(tasks),
            "includes_existing_review_history": True, "includes_future_review_history": False,
            "projection_is_guarantee": False,
        }
        return report

    def validate_tasks(self, plan):
        tasks = validate_plan(plan, self.config.data["runtime"]["max_tasks"])
        available = {item["name"] for item in skills_catalog(self.config.asset("skills"))}
        for task in tasks:
            unknown = set(task["skills"]) - available
            if unknown:
                raise HarnessError(f"计划引用不存在的 skill：{sorted(unknown)}")
        return tasks

    def ensure_exited(self):
        for entry in self.run["dispatches"]:
            if not entry.get("exit_confirmed") and entry.get("pgid"):
                if group_alive(entry["pgid"]):
                    raise HarnessError("旧 dispatch 的进程组仍存在；确认退出后才能继续")
                entry["exit_confirmed"] = True

    def stop(self, error):
        self.run["status"] = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "blocked"
        self.run["error"] = str(error)
        self.run["error_type"] = type(error).__name__
        self.save()
        self.checkpoint()
        self.event("run_stopped", error=str(error), error_type=type(error).__name__)
        try:
            self.hooks.emit("on_error", {"error": str(error), "run_id": self.run["id"]}, self.directory / "hooks" / "error")
        except Exception as hook_error:
            self.event("error_hook_failed", error=str(hook_error))

    def tool_constraints(self):
        return {"readonly_commands": self.config.readonly_commands(),
                "worker_commands": self.config.data["permissions"]["worker_commands"],
                "verification_commands": self.config.data["verification"]["commands"]}

    def finish_post_accept(self):
        pending = self.run.get("pending_post_accept")
        if pending:
            # A crash after hook success but before saving can retry the hook.
            # Keep the accepted commit, and require this gate before any dispatch.
            self.hooks.emit("post_accept", pending["payload"], Path(pending["logs"]))
            self.run.pop("pending_post_accept")
            self.save()

    def dispatch(self, role, kind, task_id, schema, packet, task=None):
        # A format correction is a new process, never a provider-session resume.
        key = f"{role}:{kind}:{task_id}"
        previous_review = None
        guards = self.run.setdefault("review_corrections", {})
        guard = guards.get(key) if role == "reviewer" else None
        if guard:
            if snapshot_digest(git.snapshot(self.worktree)) != guard["snapshot_digest"]:
                guards.pop(key)
            else:
                if guard.get("failed"):
                    raise HarnessError("审阅格式修复违反原审阅结论；同一快照禁止重复抽取结论")
                previous_review = read_json(Path(guard["artifact"]))
                previous_review = {k: v for k, v in previous_review.items() if k not in ("task_id", "dispatch_id")}
                packet = {**packet, "format_correction": self.review_correction(guard, previous_review)}
        while True:
            self.ensure_exited()
            receipt = self.run["review_receipts"].get(key)
            if receipt and "artifact_digest" not in receipt and not Path(receipt["artifact"]).exists():
                # Intent was saved before invoke; the provider never returned a
                # usable result. Retrying still consumes a fresh dispatch budget.
                self.run["review_receipts"].pop(key)
                self.save()
            try:
                if role == "reviewer" and key in self.run["review_receipts"]:
                    result = self.finish_review_receipt(key, schema, packet, previous_review)
                else:
                    result = self.dispatch_once(role, kind, task_id, schema, packet, task, previous_review)
                self.ensure_exited()
                return result
            except RepairableFormatError as error:
                self.ensure_exited()
                receipt = self.run["review_receipts"].get(key)
                previous = (next(d for d in self.run["dispatches"] if d["id"] == receipt["dispatch_id"])
                            if receipt else self.run["dispatches"][-1])
                returned = read_json(self.directory / "dispatches" / previous["id"] / "returned-result.json")
                if role == "worker" and returned["status"] == "blocked":
                    self.run["feedback"] = {"task_id": task_id, "worker": returned,
                                            "format_error": str(error)}
                    self.save()
                    raise HarnessError(f"worker 已报告 {returned['blocker_kind']} blocker；保留格式错误证据，不自动重派") from error
                if role == "commander" and kind == "replan" and returned["action"] == "stop":
                    raise HarnessError("指挥已停止再规划；保留格式错误证据，不自动重派") from error
                if role == "reviewer" and previous_review is None:
                    previous_review = {k: v for k, v in returned.items() if k not in ("task_id", "dispatch_id")}
                    guard = {"artifact": str(self.directory / "dispatches" / previous["id"] / "returned-result.json"),
                             "snapshot_digest": previous["snapshot_digest"], "error": str(error)}
                    guards[key] = guard
                    self.save()
                # The immutable original remains in the correction guard. A manual
                # resume may retry its correction, but never redraw the verdict.
                if receipt:
                    self.run["review_receipts"].pop(key)
                    self.save()
                repairs = self.run.setdefault("format_repairs", {})
                if repairs.get(key, 0):
                    raise
                if role == "worker":
                    attempts = self.run["attempts"][task_id]
                    if attempts >= self.config.data["runtime"]["max_attempts"]:
                        raise HarnessError(f"{task_id} 已达尝试次数上限；格式修复也计入预算") from error
                    if self.run["total_attempts"] >= self.config.data["runtime"]["max_total_attempts"]:
                        raise HarnessError("已达 run 总尝试次数上限；格式修复也计入预算") from error
                    self.run["attempts"][task_id] += 1
                    self.run["total_attempts"] += 1
                repairs[key] = 1
                correction = {"error": str(error), "previous_dispatch": previous["id"],
                              "artifact": str(self.directory / "dispatches" / previous["id"]),
                              "instruction": "Correct the reported output format using the current task and files. Recheck the evidence. Return only this dispatch's IDs; do not relax acceptance or discard blocking findings."}
                if role == "reviewer":
                    correction = self.review_correction(guard, previous_review)
                packet = {**packet, "format_correction": correction}
                self.run["feedback"] = {**(self.run.get("feedback") or {}), "format_correction": correction}
                self.save()
                self.event("output_format_repair", task_id=task_id, role=role, previous_dispatch=previous["id"])

    @staticmethod
    def review_correction(guard, previous_review):
        return {"error": guard["error"], "artifact": guard["artifact"],
                "preserve_review": {k: v for k, v in previous_review.items() if k != "summary"},
                "instruction": "Correct the format using this dispatch's IDs. Preserve the supplied verdict, memory approvals and all findings in order, including their exact severity, category, location, message and evidence. Only the summary and invalid finding IDs may be reformatted; do not discard or downgrade findings."}

    def finish_review_receipt(self, key, schema, packet, previous_review=None):
        """Finish a returned review without treating unvalidated JSON as approval."""
        receipt = self.run["review_receipts"][key]
        entry = next(d for d in self.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        artifact = Path(receipt["artifact"])
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if "artifact_digest" not in receipt:
            receipt["artifact_digest"] = digest
            self.save()
        if digest != receipt["artifact_digest"]:
            raise HarnessError("审阅原始结果已改变；保留证据，禁止重新抽取结论")
        if (git.head(self.worktree) != self.run["expected_head"]
                or snapshot_digest(git.snapshot(self.worktree)) != receipt["snapshot_digest"]):
            raise HarnessError("审阅或 hook 修改了代码/HEAD；原审阅结果不能用于当前快照")
        result = read_json(artifact)
        contract = envelope(schema, entry["task_id"], entry["id"])
        validate_structure(result, contract)
        validate_review_semantics(result, [p["key"] for p in packet.get("proposed_memories", [])])
        if previous_review is not None:
            try:
                validate_review_correction(previous_review, result)
            except HarnessError:
                self.run["review_corrections"][key]["failed"] = True
                self.save()
                raise
        validate(result, contract)
        if not receipt.get("hook_completed"):
            receipt["hook_attempts"] = receipt.get("hook_attempts", 0) + 1
            self.save()
            logs = artifact.parent / f"post-agent-{receipt['hook_attempts']}"
            self.hooks.emit("post_agent", {**entry, "status": "completed", "result": result}, logs)
            if (git.head(self.worktree) != self.run["expected_head"]
                    or snapshot_digest(git.snapshot(self.worktree)) != receipt["snapshot_digest"]):
                raise HarnessError("reviewer hook 修改了代码；已停止，保留现场")
            receipt["hook_completed"] = True
        entry["status"] = "completed"
        entry.pop("error", None)
        self.save()
        return {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}

    def dispatch_once(self, role, kind, task_id, schema, packet, task=None, previous_review=None):
        dispatch_id = str(uuid.uuid4())
        job = self.directory / "dispatches" / dispatch_id
        job.mkdir(parents=True)
        contract = envelope(schema, task_id, dispatch_id)
        required = {"role": role, "kind": kind, "task_id": task_id, "dispatch_id": dispatch_id,
                    "rules": self.config.asset("rules").read_text(),
                    "role_skill": read_skill(self.config.asset("skills"), "harness-" + role),
                    "packet": packet, "output_constraints": output_constraints(contract)}
        if role != "reviewer":
            required["available_skills"] = skills_catalog(self.config.asset("skills"))
        if task and role == "worker":
            required["task_skills"] = {s: read_skill(self.config.asset("skills"), s) for s in task["skills"]}
        query = (task["title"] + " " + task["description"]) if task else self.run["goal"]
        memories = self.memory.search(query, level=1)
        prompt, report = compose(required, memories, self.config.data["runtime"]["context_chars"])
        write_json(job / "context-report.json", report)
        self.hooks.emit("context_prepared", {"report": report, "run_id": self.run["id"]}, job)
        self.run["dispatches"].append({"id": dispatch_id, "role": role, "kind": kind, "task_id": task_id, "status": "running", "pgid": None})
        entry = self.run["dispatches"][-1]
        self.save()
        self.event("dispatch_started", task_id=task_id, dispatch_id=dispatch_id, role=role)
        self.hooks.emit("pre_agent", entry, job)
        before = git.snapshot(self.worktree)
        entry["snapshot_digest"] = snapshot_digest(before)

        def on_start(pgid):
            entry["pgid"] = pgid
            self.save()

        try:
            if self.run["agent_dispatches"] >= self.config.data["runtime"]["max_agent_dispatches"]:
                raise HarnessError("已达 run 模型派发次数上限；可显式增大 max_agent_dispatches 后恢复")
            self.run["agent_dispatches"] += 1
            if role == "reviewer":
                # Persist the identity and expected path before calling the model:
                # a crash immediately after the atomic result write must not reroll.
                self.run["review_receipts"][f"{role}:{kind}:{task_id}"] = {
                    "artifact": str(job / "returned-result.json"), "dispatch_id": dispatch_id,
                    "snapshot_digest": entry["snapshot_digest"]}
            self.save()
            result = self.backend.invoke(role=role, model=self.config.role(role, self.commander), cwd=self.worktree,
                                         job=job, schema=contract, prompt=prompt, run_path=self.directory / "state.json",
                                         on_start=on_start, task=task, kind=kind)
            write_json(job / "returned-result.json", result)
            if role == "reviewer":
                artifact = job / "returned-result.json"
                key = f"{role}:{kind}:{task_id}"
                self.run["review_receipts"][key] = {
                    "artifact": str(artifact), "artifact_digest": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "dispatch_id": dispatch_id, "snapshot_digest": entry["snapshot_digest"]}
                self.save()
                return self.finish_review_receipt(key, schema, packet, previous_review)
            if git.head(self.worktree) != self.run["expected_head"]:
                raise HarnessError("agent 修改了 Git HEAD；已停止，保留现场")
            if role != "worker" and git.snapshot(self.worktree) != before:
                raise HarnessError(f"{role} 修改了代码；已停止，保留现场")
            if role == "worker":
                git.check_scope(self.worktree, task["allowed_paths"])
            validate_structure(result, contract)
            if role == "worker":
                validate_work_semantics(result)
            elif role == "reviewer":
                validate_review_semantics(result, [p["key"] for p in packet.get("proposed_memories", [])])
            elif kind in ("plan", "replan"):
                payload = {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
                if kind == "plan":
                    self.validate_tasks(payload)
                else:
                    revised = apply_revision(self.run["tasks"], payload, self.run["accepted"],
                                             self.config.data["runtime"]["max_tasks"])
                    self.validate_tasks({"tasks": revised})
            if previous_review is not None:
                validate_review_correction(previous_review, result)
            validate(result, contract)
            entry["status"] = "completed"
            self.hooks.emit("post_agent", {**entry, "result": result}, job)
            if git.head(self.worktree) != self.run["expected_head"]:
                raise HarnessError("agent hook 修改了 Git HEAD；已停止，保留现场")
            if role != "worker" and git.snapshot(self.worktree) != before:
                raise HarnessError(f"{role} hook 修改了代码；已停止，保留现场")
            return {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
        except BaseException as error:
            entry["status"] = "failed"
            entry["error"] = str(error)
            if not isinstance(error, (KeyboardInterrupt, SystemExit)):
                self.run["last_runtime_error"] = {"role": role, "kind": kind, "task_id": task_id,
                                                 "error": str(error), "artifact": str(job)}
            raise
        finally:
            entry["exit_confirmed"] = not entry["pgid"] or not group_alive(entry["pgid"])
            self.save()

    def start(self, goal, plan=None, *, pause_after_plan=False, step=False):
        if not goal.strip():
            raise HarnessError("目标不能为空")
        if not git.clean(self.repo):
            raise HarnessError("目标仓库有未提交变更；请先保存提交，再创建隔离 worktree")
        if not self.config.data["verification"]["commands"]:
            raise HarnessError("请先配置目标项目的 verification.commands")
        ident = uid("run")
        worktree = self.state / "worktrees" / ident
        self.run = {"schema_version": 4, "id": ident, "repo": str(self.repo), "goal": goal, "status": "planning", "commander": self.commander,
                    "branch": "harness/" + ident, "worktree": str(worktree), "tasks": [], "accepted": {}, "attempts": {},
                    "dispatches": [], "expected_head": git.head(self.repo), "feedback": None,
                    "config_path": str(self.config.path), "config_snapshot": deepcopy(self.config.data),
                    "asset_digest": asset_digest(self.config), "base_commit": git.head(self.repo), "original_tasks": [],
                    "total_attempts": 0, "replan_attempts": 0, "final_attempts": 0,
                    "review_history": {}, "final_history": [], "plan_history": [], "format_repairs": {},
                    "review_corrections": {}, "review_receipts": {}, "agent_dispatches": 0,
                    "plan_approval_required": pause_after_plan, "plan_approved": not pause_after_plan}
        self.directory.mkdir(parents=True)
        self.save()
        with lock(self.directory / "run.lock"):
            try:
                git.git(self.repo, "worktree", "add", "-b", self.run["branch"], str(worktree), "HEAD")
            except BaseException as error:
                self.stop(error)
                raise
            self.memory.repo = self.worktree
            return self.execute(plan, step=step)

    def resume(self, ident, *, step=False, approve_plan=False, reverify=False):
        if not ident.startswith("run_") or not ident[4:].isalnum():
            raise HarnessError("无效 run ID")
        self.run = read_json(self.state / "runs" / ident / "state.json")
        if not isinstance(self.run, dict):
            raise HarnessError("运行台账必须为对象")
        if self.run.get("schema_version") != 4:
            raise HarnessError("旧 run 状态格式无法安全恢复；请保留证据并使用原版本完成，或开始新 run")
        required = {"id": str, "repo": str, "worktree": str, "status": str, "commander": str,
                    "goal": str, "branch": str, "expected_head": str, "base_commit": str,
                    "config_snapshot": dict, "tasks": list, "original_tasks": list,
                    "accepted": dict, "attempts": dict, "dispatches": list,
                    "review_history": dict, "review_corrections": dict, "review_receipts": dict,
                    "final_history": list, "plan_history": list, "format_repairs": dict,
                    "total_attempts": int, "agent_dispatches": int, "final_attempts": int,
                    "replan_attempts": int, "plan_approval_required": bool, "plan_approved": bool}
        if (self.run.get("id") != ident or any(type(self.run.get(k)) is not t for k, t in required.items())
                or any(not isinstance(d, dict) for d in self.run["dispatches"])):
            raise HarnessError("运行台账字段缺失或类型无效；保留原文件并核对证据")
        if self.run["repo"] != str(self.repo):
            raise HarnessError("Run 属于另一个项目")
        self.commander = self.run["commander"]
        self.reverify_requested = reverify
        changes = self.config.resume_changes(self.run["config_snapshot"])
        if self.run.get("asset_digest") != asset_digest(self.config):
            raise HarnessError("共享规则、skills、MCP 或 hooks 已改变；请恢复原版本后继续")
        with lock(self.directory / "run.lock"):
            if self.run["status"] == "completed":
                return self.run
            self.ensure_exited()
            if git.head(self.worktree) != self.run["expected_head"]:
                raise HarnessError("worktree HEAD 与检查点不同；请先核对提交，不自动重放")
            self.memory.repo = self.worktree
            if changes:
                self.run["config_snapshot"] = deepcopy(self.config.data)
                self.event("resume_config_relaxed", changes=changes)
                self.save()
            if self.run["tasks"] and self.run["plan_approval_required"] and not self.run["plan_approved"]:
                if not approve_plan:
                    raise HarnessError("计划等待明确批准；使用 resume --approve-plan 继续")
                self.run["plan_approved"] = True
                self.event("plan_approved")
                self.save()
            return self.execute(step=step)

    def verification_directory(self, task_id, attempt, *, whole_run=False):
        root = self.directory / "verification"
        return root / "final" / str(attempt) if whole_run else root / "tasks" / task_id / str(attempt)

    def verify(self, task_id, attempt, *, whole_run=False, artifact_dir=None):
        folder = Path(artifact_dir) if artifact_dir is not None else self.verification_directory(task_id, attempt, whole_run=whole_run)
        folder.mkdir(parents=True, exist_ok=True)
        before = git.snapshot(self.worktree)
        self.hooks.emit("pre_verify", {"task_id": task_id, "worktree": str(self.worktree),
                                      "scope": "run" if whole_run else "task"}, folder)
        results = []
        for i, command in enumerate(self.config.data["verification"]["commands"]):
            self.ensure_exited()
            log = folder / f"check-{i}"
            entry = {"id": str(uuid.uuid4()), "role": "verification", "task_id": task_id, "status": "running", "pgid": None}
            self.run["dispatches"].append(entry)
            self.save()

            def on_start(pgid):
                entry["pgid"] = pgid
                self.save()

            timed_out = False
            try:
                code = run_process(command, self.worktree, log, timeout=self.config.data["runtime"]["verify_timeout_seconds"], on_start=on_start)
                entry["status"] = "completed" if code == 0 else "failed"
            except ProcessTimeout:
                code, timed_out = 124, True
                entry["status"] = "timed_out"
            except BaseException as error:
                entry["status"] = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
                self.run["feedback"] = {"task_id": task_id,
                    "verification_error": {"argv": command, "error": str(error), "log": str(log)}}
                raise
            finally:
                entry["exit_confirmed"] = not entry["pgid"] or not group_alive(entry["pgid"])
                self.save()
            results.append({"argv": command, "exit_code": code, "stdout": tail(log.with_suffix(".stdout")),
                            "stderr": tail(log.with_suffix(".stderr")), "log": str(log), "timed_out": timed_out})
            write_json(folder / "results.json", results)
            self.ensure_exited()
        self.hooks.emit("post_verify", {"task_id": task_id, "results": results}, folder)
        after = git.snapshot(self.worktree)
        if after != before:
            changes = git.snapshot_changes(before, after)
            write_json(folder / "file-changes.json", changes)
            self.run["feedback"] = {"task_id": task_id, "verification": compact_checks(results),
                "verification_changes": {**changes, "artifact": str(folder / "file-changes.json")}}
            raise HarnessError("验证命令修改了待审阅文件；验证必须为只读检查。"
                               f"新增未跟踪文件：{changes['added']}；修改：{changes['modified']}；删除：{changes['deleted']}。"
                               "请配置构建/测试输出位置或显式忽略生成产物；不自动豁免文件。")
        write_json(folder / "results.json", results)
        return results

    def record_plan(self, tasks, reason, trigger=None, *, finish_replan=False):
        revision = len(self.run["plan_history"])
        artifact = self.directory / "plans" / f"revision-{revision}.json"
        record = {"revision": revision, "reason": reason, "trigger": trigger,
                  "tasks": deepcopy(tasks), "artifact": str(artifact)}
        write_json(artifact, record)
        self.run["plan_history"].append(record)
        self.run["tasks"] = deepcopy(tasks)
        if finish_replan:
            self.run["current_task"] = None
            self.run.pop("pending_replan", None)
        self.save()

    def replan(self, trigger):
        pending = self.run.get("pending_replan")
        if pending and pending["stage"] == "validated":
            return self.apply_pending_revision()
        final = trigger.get("final_review")
        if final:
            if not git.clean(self.worktree):
                raise HarnessError("整体审阅要求干净的已接受提交；请核对并清理未接受修改")
            if snapshot_digest(git.snapshot(self.worktree)) != final["snapshot_digest"]:
                raise HarnessError("待再规划的整体审阅快照已变化；请核对检查点")
        if self.run["total_attempts"] >= self.config.data["runtime"]["max_total_attempts"]:
            raise HarnessError("已达 run 总尝试次数上限；不通过新增任务绕过预算")
        tid = trigger.get("task_id")
        if tid and self.run["attempts"].get(tid, 0) >= self.config.data["runtime"]["max_attempts"]:
            raise HarnessError(f"{tid} 已达尝试次数上限；不通过重新命名任务绕过预算")
        if final and self.run["final_attempts"] >= self.config.data["runtime"]["max_final_attempts"]:
            raise HarnessError("整体审阅已达尝试次数上限；不再派发修正任务")
        if self.run["replan_attempts"] >= self.config.data["runtime"]["max_replans"]:
            prefix = ""
            if final and final["status"] == "rejected":
                prefix = "整体审阅已拒绝同一代码快照；修正计划"
            elif final and final["status"] == "verification_failed":
                prefix = "整体验证未通过；可显式使用 resume --reverify 重验，修正计划"
            raise HarnessError(prefix + "已达重新规划次数上限；可增大 max_replans 后恢复，反馈与证据已保留")
        attempt = self.run["replan_attempts"] + 1
        self.run["pending_replan"] = {"stage": "request", "trigger": deepcopy(trigger), "attempt": attempt}
        self.save()
        result = self.dispatch("commander", "replan", "replanning", REPLAN,
            {"goal": self.run["goal"], **self.plan_context(),
             "accepted": self.accepted_context(), "trigger": compact_feedback(trigger),
             "attempts": self.run["attempts"], "total_attempts": self.run["total_attempts"],
             "max_tasks": self.config.data["runtime"]["max_tasks"],
             "remaining_replans": self.config.data["runtime"]["max_replans"] - attempt,
             "tool_constraints": self.tool_constraints(),
             "instruction": "Propose new_tasks and updates only when prerequisites or corrective tasks can resolve the blocker. Each output update has id, description and add_depends_on; the runtime preserves all other fields. Accepted tasks cannot be updated. Never remove dependencies or weaken the goal. New task allowed_paths must reuse existing scope patterns. Input tasks_ref names the unchanged current list; otherwise reconstruct current tasks from tasks_base_ref and tasks_delta, applying supplied description/dependency updates and appending new_tasks. Return empty operations when stopping. No file edits."})
        artifact = self.directory / "plans" / f"replan-{attempt}.json"
        write_json(artifact, {"trigger": trigger, "result": result})
        tasks = apply_revision(self.run["tasks"], result, self.run["accepted"],
                               self.config.data["runtime"]["max_tasks"])
        self.validate_tasks({"tasks": tasks})
        self.run["replan_attempts"] = attempt
        if result["action"] == "stop":
            self.run.pop("pending_replan", None)
            self.save()
            raise HarnessError(f"指挥停止重新规划：{result['reason']}")
        self.run["pending_replan"].update(stage="validated", tasks=tasks, reason=result["reason"],
                                          artifact=str(artifact), head=self.run["expected_head"])
        self.save()
        self.apply_pending_revision()

    def apply_pending_revision(self):
        pending = self.run["pending_replan"]
        if self.run["expected_head"] != pending["head"] or git.head(self.worktree) != pending["head"]:
            raise HarnessError("应用再规划前 HEAD 与已校验计划不符；保留现场")
        self.preflight_final_context(pending["tasks"], self.run["original_tasks"])
        # A new prerequisite may own another scope. Never carry the blocked task's edits into it.
        if not git.clean(self.worktree):
            folder = self.directory / "recovery" / f"replan-{pending['attempt']}"
            if folder.exists():
                folder = folder.with_name(folder.name + "-" + uid("retry"))
            archive = git.archive_and_reset(self.worktree, self.run["expected_head"],
                                            folder, self.state / "worktrees")
            self.event("replan_edits_archived", task_id=pending["trigger"].get("task_id"), artifact=archive)
        self.run["feedback"] = {"replan": pending["artifact"], "trigger": compact_feedback(pending["trigger"])}
        self.record_plan(pending["tasks"], pending["reason"], pending["trigger"], finish_replan=True)
        self.checkpoint()
        self.event("plan_revised", revision=len(self.run["plan_history"]) - 1)

    def preflight_final_context(self, tasks=None, original_tasks=None):
        report = self.final_context_capacity(tasks, original_tasks)
        if report != self.run.get("context_capacity"):
            self.run["context_capacity"] = report
            self.save()
            self.event("final_context_preflight", report=report)
        if report["status"] == "required_overflow":
            raise HarnessError(f"计划的整体审阅必要上下文 {report['required_chars']} 字符超过预算 "
                               f"{report['budget_chars']}；请增加 context_chars 后恢复，尚未派发 worker")
        if report["status"] == "projected_overflow":
            print(f"[harness] 整体审阅预计上下文 {report['projected_chars']} 字符，超过预算 "
                  f"{report['budget_chars']}；建议提前增加 context_chars。此估计不保证后续历史容量。", flush=True)

    def task_attempt(self, ready):
        current = self.run.get("current_task")
        if current:
            task = next(t for t in self.run["tasks"] if t["id"] == current)
            self.check_task_scope(task)
        if current and self.run["attempts"].get(current, 0) >= self.config.data["runtime"]["max_attempts"]:
            raise HarnessError(f"{current} 已达尝试次数上限")
        if self.run["total_attempts"] >= self.config.data["runtime"]["max_total_attempts"]:
            raise HarnessError("已达 run 总尝试次数上限")
        pristine = len(ready) == 1 and not self.run.get("feedback") and not self.run["attempts"].get(ready[0]["id"])
        if pristine:
            directive = {"selected_task": ready[0]["id"],
                         "instruction": "Implement this task within its complete stated scope and acceptance. Inspect dependency evidence and follow the supplied tool and verification constraints."}
        else:
            schema = obj(selected_task={"type": "string", "enum": [t["id"] for t in ready]}, instruction=TEXT)
            directive = self.dispatch("commander", "dispatch", "dispatching", schema,
                        {"goal": self.run["goal"], "ready": ready, "accepted": self.accepted_context(),
                         "feedback": compact_feedback(self.run.get("feedback")), "attempts": self.run["attempts"],
                         "tool_constraints": self.tool_constraints(),
                         "instruction": "Select one ready task. Give a focused implementation or repair instruction, retaining its original scope and acceptance."}, task=ready[0])
        task = next(t for t in ready if t["id"] == directive["selected_task"])
        tid = task["id"]
        attempt = self.run["attempts"].get(tid, 0) + 1
        if attempt > self.config.data["runtime"]["max_attempts"]:
            raise HarnessError(f"{tid} 已达尝试次数上限")
        self.run["attempts"][tid] = attempt
        self.run["total_attempts"] += 1
        self.run["current_task"] = tid
        self.save()
        self.checkpoint()
        prior_reviews = compact_reviews(self.run["review_history"].get(tid, []))
        result = self.dispatch("worker", "implement", tid, WORK,
                 {"goal": self.run["goal"], "task": task, "instruction": directive["instruction"],
                  "feedback": compact_feedback(self.run.get("feedback"), prior_reviews), "prior_reviews": prior_reviews,
                  "dependencies": {d: self.accepted_context()[d] for d in task["depends_on"]},
                  "verification_commands": self.config.data["verification"]["commands"],
                  "tool_constraints": self.tool_constraints()}, task=task)
        validate_work(result)
        attempt = self.run["attempts"][tid]
        paths = git.check_scope(self.worktree, task["allowed_paths"])
        if result["status"] == "blocked":
            self.run["feedback"] = {"task_id": tid, "worker": result, "attempt": attempt}
            self.save()
            if result["blocker_kind"] != "task":
                raise HarnessError(f"worker 环境受阻：{result['blockers']} {result['summary']}")
            self.replan(deepcopy(self.run["feedback"]))
            return False
        self.run["pending_task"] = {"task_id": tid, "attempt": attempt, "stage": "verifying",
            "snapshot_digest": snapshot_digest(git.snapshot(self.worktree)),
            "worker_result": result, "prior_reviews": prior_reviews, "verification_rounds": 0}
        self.save()
        return self.continue_task()

    def check_task_scope(self, task):
        try:
            return git.check_scope(self.worktree, task["allowed_paths"])
        except HarnessError as error:
            raise HarnessError(f"{error}。工作树：{self.worktree}；请先核对并清理越界修改，再 resume。"
                               "现场与证据已保留，本次预检不会派发 worker。") from error

    def continue_task(self):
        pending = self.run["pending_task"]
        tid, attempt = pending["task_id"], pending["attempt"]
        task = next(t for t in self.run["tasks"] if t["id"] == tid)
        self.ensure_exited()
        if git.head(self.worktree) != self.run["expected_head"]:
            raise HarnessError("任务恢复前 HEAD 与检查点不同；保留现场")
        paths = self.check_task_scope(task)
        key = f"reviewer:review:{tid}"
        if snapshot_digest(git.snapshot(self.worktree)) != pending["snapshot_digest"]:
            self.run.pop("pending_task")
            self.run["review_receipts"].pop(key, None)
            self.run["review_corrections"].pop(key, None)
            self.run["feedback"] = {"task_id": tid, "recovery": {
                "error": "任务候选已变化；旧验证及审阅失效，重新执行任务",
                "verification": pending.get("verification")}}
            self.save()
            self.event("pending_task_invalidated", task_id=tid)
            return False
        if pending["stage"] == "verifying":
            pending["verification_rounds"] += 1
            folder = self.verification_directory(tid, attempt)
            if pending["verification_rounds"] > 1:
                folder /= f"retry-{pending['verification_rounds']}"
            pending["verification"] = str(folder / "results.json")
            self.save()
            checks = self.verify(tid, attempt, artifact_dir=folder)
            if any(c["exit_code"] for c in checks):
                self.run["feedback"] = {"task_id": tid, "verification": compact_checks(checks)}
                self.run.pop("pending_task")
                self.save()
                self.event("verification_failed", task_id=tid)
                return False
            paths = self.check_task_scope(task)
            evidence = folder / "change.diff"
            evidence.write_bytes(git.diff_bytes(self.worktree))
            snapshot_artifact = folder / "snapshot.json"
            write_json(snapshot_artifact, git.snapshot(self.worktree))
            try:
                proposals = validate_proposals(pending["worker_result"]["proposed_memories"], self.worktree)
            except HarnessError as error:
                proposals = []
                self.event("memory_proposals_discarded", task_id=tid, reason=str(error))
            pending.update(stage="reviewing", proposals=proposals,
                           diff_artifact=str(evidence), snapshot_artifact=str(snapshot_artifact))
            self.save()
        evidence = Path(pending["diff_artifact"])
        snapshot_artifact = Path(pending["snapshot_artifact"])
        reviewed_digest = pending["snapshot_digest"]
        checks = read_json(Path(pending["verification"]))
        proposals = pending["proposals"]
        prior_reviews = pending["prior_reviews"]
        review = self.dispatch("reviewer", "review", tid, REVIEW,
                 {"goal": self.run["goal"], "task": task, "base_commit": self.run["expected_head"], "changed_files": paths,
                  "diff_artifact": str(evidence), "verification": compact_checks(checks),
                  "prior_reviews": prior_reviews,
                  "proposed_memories": proposals,
                  "tool_constraints": self.tool_constraints(),
                  "instruction": "Independently inspect the actual diff, relevant code and acceptance. Prior findings are evidence to recheck, not a complete checklist. Separate blocking defects from advisory preferences. Approve only with no unresolved blocking findings; approve memory keys only when their claims and cited sources are independently supported. Your summary becomes the accepted task record: describe only independently verified outcomes and limits. An empty diff can pass only when current evidence already satisfies the task. Do not edit files."}, task=task)
        validate_review(review, [p["key"] for p in proposals])
        review_artifact = evidence.parent / "review.json"
        record = {"attempt": attempt, "review": review, "diff_artifact": str(evidence),
                  "verification": str(evidence.parent / "results.json"), "artifact": str(review_artifact),
                  "snapshot_digest": reviewed_digest, "snapshot_artifact": str(snapshot_artifact)}
        write_json(review_artifact, record)
        approved_keys = review["approved_memory_keys"]
        if review_approved(review):
            try:
                self.memory.check_proposals(proposals, approved_keys)
            except HarnessError as error:
                approved_keys = []
                self.event("memory_proposals_discarded", task_id=tid, reason=str(error))
        self.run["review_history"].setdefault(tid, []).append(record)
        self.run["review_corrections"].pop(key, None)
        self.run["review_receipts"].pop(key, None)
        self.run.pop("pending_task")
        if not review_approved(review):
            self.run["feedback"] = {"task_id": tid, "review": review, "artifact": str(review_artifact)}
            self.save()
            self.event("review_rejected", task_id=tid)
            return False
        self.run["pending_acceptance"] = {"task_id": tid, "attempt": attempt, "files": paths,
            "review": review, "review_artifact": str(review_artifact), "snapshot_digest": reviewed_digest,
            "snapshot_artifact": str(snapshot_artifact), "verification": str(evidence.parent / "results.json"),
            "proposals": proposals, "approved_keys": approved_keys}
        self.save()
        return self.accept_pending()

    def accept_pending(self):
        pending = self.run["pending_acceptance"]
        tid, attempt = pending["task_id"], pending["attempt"]
        task = next(task for task in self.run["tasks"] if task["id"] == tid)
        self.ensure_exited()
        if snapshot_digest(git.snapshot(self.worktree)) != pending["snapshot_digest"]:
            self.run.pop("pending_acceptance")
            self.run["feedback"] = {"task_id": tid, "acceptance": {
                "error": "待接受文件已变化，旧审阅失效；需要重新验证和独立审阅",
                "review_artifact": pending["review_artifact"]}}
            self.save()
            self.event("pending_acceptance_invalidated", task_id=tid)
            return False
        pending["hook_attempts"] = pending.get("hook_attempts", 0) + 1
        logs = self.directory / "hooks" / f"{tid}-{attempt}" / f"pre_accept-{pending['hook_attempts']}"
        self.save()
        try:
            self.hooks.emit("pre_accept", {"task_id": tid, "review": pending["review"]}, logs)
        except Exception as error:
            detail = {"error": str(error), "logs": str(logs), "review_artifact": pending["review_artifact"]}
            if isinstance(error, HookError):
                detail.update(exit_code=error.code, log=str(error.log))
            self.run["feedback"] = {"task_id": tid, "pre_accept": detail}
            self.save()
            raise
        if snapshot_digest(git.snapshot(self.worktree)) != pending["snapshot_digest"]:
            raise HarnessError("审阅后文件发生变化；禁止接受旧审阅结果")
        paths = git.check_scope(self.worktree, task["allowed_paths"])
        commit = git.accept(self.worktree, paths, self.run["id"], tid)
        accepted = {"commit": commit, "summary": pending["review"]["summary"], "summary_source": "reviewer",
                    "files": paths, "verification": pending["verification"], "review": pending["review"],
                    "review_artifact": pending["review_artifact"]}
        self.run["expected_head"] = commit
        self.run["accepted"][tid] = accepted
        self.run["feedback"] = None
        self.run["current_task"] = None
        self.run.pop("pending_acceptance")
        self.run["pending_post_accept"] = {"payload": {"task_id": tid, **accepted},
                                            "logs": str(self.directory / "hooks" / f"{tid}-{attempt}")}
        self.save()
        self.memory.remember(key=f"task:{self.run['id']}:{tid}", abstract=task["title"][:500],
                             overview=accepted["summary"], detail=dump(accepted),
                             source=f"git:{commit};run:{self.run['id']}", kind="accepted_task",
                             ttl_days=self.config.data["memory"]["task_ttl_days"] or None)
        try:
            self.memory.remember_proposals(pending["proposals"], pending["approved_keys"], commit, self.run["id"], tid,
                                           self.config.data["memory"]["knowledge_ttl_days"] or None)
        except HarnessError as error:
            self.event("memory_proposals_discarded", task_id=tid, reason=str(error))
        self.event("task_accepted", task_id=tid, commit=commit)
        self.finish_post_accept()
        self.checkpoint()
        return True

    def final_review(self, *, reverify=False):
        if not git.clean(self.worktree):
            raise HarnessError("整体审阅要求干净的已接受提交；未接受的修改不能用于完成 run")
        snapshot = git.snapshot(self.worktree)
        if snapshot["head"] != self.run["expected_head"]:
            raise HarnessError("整体审阅前 HEAD 与检查点不同；保留现场")
        digest = snapshot_digest(snapshot)
        history = self.run["final_history"]
        same_snapshot = [item for item in history
                         if item["snapshot_digest"] == digest and item["head"] == snapshot["head"]]
        if any(item["status"] == "rejected" for item in same_snapshot):
            raise HarnessError("整体审阅已拒绝同一代码快照；必须先通过修正任务，不能恢复后重复抽取审阅")
        if any(item["status"] == "approved" for item in same_snapshot):
            # Approval is persisted only after all validation and snapshot gates.
            return True
        record = history[-1] if history and history[-1] in same_snapshot else None
        if not record or record["status"] != "running":
            if record and record["status"] == "verification_failed" and record["failure_kind"] != "timeout":
                if not reverify:
                    raise HarnessError("同一代码快照的整体验证曾失败；修复环境后使用 resume --reverify 显式重验")
                self.reverify_requested = False
                self.event("final_reverification_requested", previous_attempt=record["attempt"])
            if self.run["final_attempts"] >= self.config.data["runtime"]["max_final_attempts"]:
                raise HarnessError("整体审阅已达尝试次数上限")
            self.run["final_attempts"] += 1
            attempt = self.run["final_attempts"]
            folder = self.verification_directory("final", attempt, whole_run=True)
            snapshot_artifact = folder / "snapshot.json"
            record = {"attempt": attempt, "head": self.run["expected_head"], "snapshot_digest": digest,
                      "snapshot_artifact": str(snapshot_artifact), "phase": "verifying",
                      "verification_rounds": [], "status": "running", "artifact": str(folder / "final.json")}
            history.append(record)
            self.save()
            self.checkpoint()
        attempt = record["attempt"]
        folder = Path(record["artifact"]).parent
        snapshot_artifact = Path(record["snapshot_artifact"])
        if snapshot_artifact.exists() or snapshot_artifact.is_symlink():
            try:
                saved_snapshot = read_json(snapshot_artifact)
            except (OSError, ValueError) as error:
                raise HarnessError("整体快照证据无法读取；保留原证据，禁止覆盖") from error
            if saved_snapshot != snapshot:
                raise HarnessError("整体快照证据与待恢复记录不符；保留原证据，禁止覆盖")
        else:
            # The attempt and its recovery phase are durable before artifact I/O.
            write_json(snapshot_artifact, snapshot)
        if record["phase"] == "verifying":
            # Interrupted checks may have written partial evidence. Never overwrite it.
            rounds = record["verification_rounds"]
            verification_folder = folder / f"verification-{len(rounds) + 1}"
            round_record = {"artifact_dir": str(verification_folder), "status": "running"}
            rounds.append(round_record)
            self.save()
            checks = self.verify("final", attempt, whole_run=True, artifact_dir=verification_folder)
            if git.snapshot(self.worktree) != snapshot:
                raise HarnessError("整体验证后文件发生变化；禁止使用旧验证结果")
            evidence = folder / "change.diff"
            evidence.write_bytes(git.range_diff_bytes(self.worktree, self.run["base_commit"]))
            round_record["status"] = "completed"
            record.update(verification=str(verification_folder / "results.json"), diff_artifact=str(evidence))
            failures = [check for check in checks if check["exit_code"]]
            if failures:
                record.update(status="verification_failed", phase="finished",
                              failure_kind="timeout" if all(check.get("timed_out") for check in failures) else "exit_code",
                              verification_failures=compact_checks(checks))
                if record["failure_kind"] != "timeout":
                    self.run["feedback"] = {"final_review": deepcopy(record)}
                    self.run["pending_replan"] = {"stage": "pending", "trigger": deepcopy(self.run["feedback"])}
            else:
                record["phase"] = "reviewing"
            write_json(folder / "final.json", record)
            self.save()
        else:
            checks = read_json(Path(record["verification"]))
        if record["phase"] == "reviewing":
            review = self.dispatch("reviewer", "final_review", "final", REVIEW,
                     {"goal": self.run["goal"], **self.plan_context(), "accepted": self.accepted_context(),
                      "base_commit": self.run["base_commit"], "head": self.run["expected_head"],
                      "diff_artifact": record["diff_artifact"], "verification": compact_checks(checks),
                      "tool_constraints": self.tool_constraints(),
                      "prior_reviews": compact_reviews([item for item in history[:-1]
                                                        if item["status"] != "running"]),
                      "instruction": FINAL_REVIEW_INSTRUCTION})
            validate_review(review)
            if git.snapshot(self.worktree) != snapshot:
                raise HarnessError("整体审阅后文件发生变化；禁止完成 run")
            record.update(review=review, phase="finished", status="approved" if review_approved(review) else "rejected")
            self.run["review_corrections"].pop("reviewer:final_review:final", None)
            self.run.get("review_receipts", {}).pop("reviewer:final_review:final", None)
            if record["status"] == "approved":
                self.run["feedback"] = None
            else:
                self.run["feedback"] = {"final_review": deepcopy(record)}
                self.run["pending_replan"] = {"stage": "pending", "trigger": deepcopy(self.run["feedback"])}
        write_json(folder / "final.json", record)
        self.save()
        if git.snapshot(self.worktree) != snapshot:
            raise HarnessError("整体审阅后文件发生变化；禁止完成 run")
        if record["status"] == "approved":
            return True
        self.run["feedback"] = {"final_review": deepcopy(record)}
        self.save()
        self.checkpoint()
        if record["status"] == "verification_failed":
            self.event("final_verification_failed", attempt=attempt, failure_kind=record["failure_kind"])
            if record["failure_kind"] == "timeout":
                raise HarnessError("整体验证超时；证据已保存，修复环境或增加验证超时后 resume 可重验")
        else:
            self.event("final_review_rejected", attempt=attempt)
        self.replan(deepcopy(self.run["feedback"]))
        return False

    def execute(self, supplied_plan=None, *, step=False):
        try:
            if not self.run["tasks"]:
                plan = supplied_plan or self.dispatch("commander", "plan", "planning", PLAN,
                         {"goal": self.run["goal"], "instruction": "Inspect the repository, then decompose the goal into bounded tasks. Each worker does one task. Use real dependencies and observable acceptance. Never change files.",
                          "max_tasks": self.config.data["runtime"]["max_tasks"],
                          "tool_constraints": self.tool_constraints(),
                          "verification_commands": self.config.data["verification"]["commands"]})
                tasks = self.validate_tasks(plan)
                self.run["original_tasks"] = deepcopy(tasks)
                self.record_plan(tasks, "Initial plan")
            if not self.run["attempts"]:
                self.preflight_final_context()
            if self.run["plan_approval_required"] and not self.run["plan_approved"]:
                self.run["status"] = "awaiting_approval"
                self.save()
                self.checkpoint()
                self.event("plan_awaiting_approval")
                return self.run
            self.run["status"] = "running"
            self.run.pop("error", None)
            self.run.pop("error_type", None)
            self.save()
            accepted_this_call = 0
            self.finish_post_accept()
            if self.run.get("pending_acceptance"):
                if self.accept_pending():
                    accepted_this_call += 1
            if self.run.get("pending_replan"):
                pending = self.run["pending_replan"]
                final = pending.get("trigger", {}).get("final_review", {})
                if (getattr(self, "reverify_requested", False) and pending["stage"] in ("pending", "request")
                        and final.get("status") == "verification_failed"
                        and snapshot_digest(git.snapshot(self.worktree)) == final.get("snapshot_digest")):
                    self.run.pop("pending_replan")
                    self.event("unvalidated_replan_cancelled_for_reverification", trigger=deepcopy(pending))
                    self.save()
            if self.run.get("pending_replan"):
                self.replan(deepcopy(self.run["pending_replan"]["trigger"]))
            if self.run.get("pending_task"):
                if self.continue_task():
                    accepted_this_call += 1
            while True:
                pending = [t for t in self.run["tasks"] if t["id"] not in self.run["accepted"]]
                if pending:
                    if step and accepted_this_call:
                        self.run["status"] = "paused"
                        self.save()
                        self.checkpoint()
                        self.event("run_paused")
                        return self.run
                    ready = [t for t in pending if set(t["depends_on"]) <= set(self.run["accepted"])]
                    if self.run.get("current_task"):
                        ready = [t for t in ready if t["id"] == self.run["current_task"]]
                    if not ready:
                        raise HarnessError("没有可执行任务")
                    if self.task_attempt(ready):
                        accepted_this_call += 1
                elif self.final_review(reverify=getattr(self, "reverify_requested", False)):
                    break
            self.run["status"] = "completed"
            self.save()
            self.checkpoint()
            self.event("run_completed")
            return self.run
        except BaseException as e:
            self.stop(e)
            raise
