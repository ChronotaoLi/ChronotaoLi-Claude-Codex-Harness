from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import uuid
from copy import deepcopy
from pathlib import Path

from . import git
from .adapters import CLIBackend
from .context import (FINAL_REVIEW_INSTRUCTION, capacity_report, compact_checks,
                      compact_feedback, compact_plan, compact_review, compact_reviews, compose)
from .contracts import (DETAIL, PLAN, REPLAN, REVIEW, WORK, RepairableFormatError, apply_revision,
                        envelope, obj, output_constraints, review_output_constraints, review_approved, validate,
                        validate_plan, validate_review, validate_review_correction, validate_review_semantics,
                        validate_structure, validate_work, validate_work_semantics)
from .memory import Memory, validate_proposals
from .shared import HookError, Hooks, asset_digest, read_skill, skills_catalog
from .util import (HarnessError, ProcessTimeout, dump, group_alive, lock,
                   read_json, run_process, tail, uid, write_json)


def snapshot_digest(snapshot):
    encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class InvalidReviewRejection(HarnessError):
    """An attributable rejection is evidence, never a validated approval."""

    def __init__(self, review, receipt, error):
        super().__init__(str(error))
        self.review = {k: v for k, v in review.items() if k not in ("task_id", "dispatch_id")}
        self.diagnostics = {"status": "invalid_rejected", "validation_error": str(error),
                            "returned_artifact": receipt["artifact"],
                            "artifact_digest": receipt["artifact_digest"],
                            "dispatch_id": receipt["dispatch_id"]}


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

    def snapshot(self):
        git.check_worktree_state(self.worktree)
        return git.snapshot(self.worktree)

    def save(self):
        write_json(self.directory / "state.json", self.run)

    def event(self, event, **fields):
        record = {"time": time.time(), "event": event, "run_id": self.run["id"], **fields}
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
        print(f"[harness] {event} {fields.get('task_id', '')}", file=sys.stderr, flush=True)

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
        (self.directory / "handoff.md").write_text("# Run handoff\n\n```json\n" + dump(handoff) + "\n```\n", encoding="utf-8")

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
            "rules": self.config.asset("rules").read_text(encoding="utf-8"),
            "role_skill": read_skill(self.config.asset("skills"), "harness-reviewer"),
            "output_constraints": review_output_constraints(envelope(REVIEW, "final", dispatch_id)),
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
                    raise HarnessError(f"旧 dispatch {entry.get('id', '<unknown>')} 的进程组仍存在；"
                                       "核实旧进程已退出后，可用 --confirm-exited <dispatch_id> 留痕确认")
                entry["exit_confirmed"] = True

    def confirm_exited(self, identifiers):
        """Record an operator's explicit assertion; never signal an unowned group."""
        entries = {entry.get("id"): entry for entry in self.run["dispatches"]}
        unknown = set(identifiers) - entries.keys()
        if unknown:
            raise HarnessError(f"未知 dispatch ID：{sorted(unknown)}")
        for ident in dict.fromkeys(identifiers):
            entry = entries[ident]
            if entry.get("exit_confirmed"):
                continue
            pgid = entry.get("pgid")
            observed = {"pgid": pgid, "group_alive": bool(pgid and group_alive(pgid))}
            if pgid:
                try:
                    process = subprocess.run(
                        ["ps", "-p", str(pgid), "-o", "pid=,ppid=,pgid=,lstart=,comm="],
                        capture_output=True, text=True, errors="replace", timeout=5, check=False)
                    observed.update(leader=process.stdout.strip(), query_exit_code=process.returncode)
                except (OSError, subprocess.TimeoutExpired) as error:
                    observed["query_error"] = str(error)
            confirmation = {"time": time.time(), "observed": observed}
            # Append the audit record before persisting the override, so a crash
            # cannot make a process appear exited without a recorded assertion.
            self.event("dispatch_exit_confirmed_by_operator", dispatch_id=ident, **confirmation)
            entry.update(exit_confirmed=True, operator_exit_confirmation=confirmation)
            self.save()

    def stop(self, error):
        self.run["status"] = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "blocked"
        self.run["error"] = str(error)
        self.run["error_type"] = type(error).__name__
        self.run["error_hook_attempts"] = self.run.get("error_hook_attempts", 0) + 1
        self.save()
        self.checkpoint()
        self.event("run_stopped", error=str(error), error_type=type(error).__name__)
        try:
            self.hooks.emit("on_error", {"error": str(error), "run_id": self.run["id"]},
                            self.directory / "hooks" / f"error-{self.run['error_hook_attempts']}")
        except Exception as hook_error:
            self.event("error_hook_failed", error=str(hook_error))

    def tool_constraints(self):
        return {"readonly_commands": self.config.readonly_commands(),
                "worker_commands": self.config.data["permissions"]["worker_commands"],
                "verification_commands": self.config.data["verification"]["commands"]}

    def finish_post_accept(self):
        pending = self.run.get("pending_post_accept")
        if not pending:
            return False
        completion = pending["completion"]
        payload = pending["payload"]
        tid, commit = payload["task_id"], payload["commit"]
        # These IDs are committed in the same SQLite transaction as the records.
        # A crash after a successful write but before save must not create a version.
        if not completion.get("task_memory_id"):
            completion["task_memory_id"] = self.memory.remember(
                **completion["task_memory"], operation_id=completion["id"] + ":task")
            self.save()
        if completion["knowledge"]["status"] == "pending":
            try:
                identifiers = self.memory.remember_proposals(
                    completion["proposals"], completion["approved_keys"], commit, self.run["id"], tid,
                    completion["knowledge_ttl_days"], operation_id=completion["id"] + ":knowledge")
            except HarnessError as error:
                completion["knowledge"] = {"status": "discarded", "reason": str(error)}
            else:
                completion["knowledge"] = {"status": "promoted", "memory_ids": identifiers}
            self.save()
        if completion["knowledge"]["status"] == "discarded":
            self.acceptance_event_once("memory_proposals_discarded", completion["id"] + ":discarded",
                                       task_id=tid, reason=completion["knowledge"]["reason"])
        self.acceptance_event_once("task_accepted", completion["id"], task_id=tid, commit=commit)
        # A crash after hook success but before saving can retry the hook.
        # Memory completion and this gate both precede any subsequent dispatch.
        pending["hook_attempts"] = pending.get("hook_attempts", 0) + 1
        self.save()
        self.hooks.emit("post_accept", payload, Path(pending["logs"]) / f"post_accept-{pending['hook_attempts']}")
        self.run.pop("pending_post_accept")
        self.save()
        return True

    def acceptance_event_once(self, event, event_id, **fields):
        """Recover the event-append/state-save boundary using the stable acceptance ID."""
        path = self.directory / "events.jsonl"
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        for line in content.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("event_id") == event_id:
                return
        if content and not content.endswith("\n"):
            # Preserve a partial final append as evidence without joining the retry to it.
            with path.open("a", encoding="utf-8") as stream:
                stream.write("\n")
        self.event(event, event_id=event_id, **fields)

    def dispatch(self, role, kind, task_id, schema, packet, task=None):
        # A format correction is a new process, never a provider-session resume.
        key = f"{role}:{kind}:{task_id}"
        previous_review = None
        pending = self.run.setdefault("pending_format_corrections", {})
        guards = self.run.setdefault("review_corrections", {})
        guard = guards.get(key) if role == "reviewer" else None
        candidate_digest = (snapshot_digest(self.snapshot())
                            if guard or key in pending else None)
        if key in pending:
            correction_id = pending[key].get("dispatch_id")
            returned = (correction_id and
                        (self.directory / "dispatches" / correction_id / "returned-result.json").exists())
            if role != "reviewer" and returned:
                # Also closes the crash window after result write, before pending cleanup.
                pending.pop(key)
                self.save()
            elif candidate_digest == pending[key]["snapshot_digest"]:
                packet = {**packet, "format_correction": pending[key]["correction"]}
            else:
                pending.pop(key)
                self.save()
        if guard:
            if candidate_digest != guard["snapshot_digest"]:
                guards.pop(key)
                pending.pop(key, None)
                self.save()
            else:
                if guard.get("failed"):
                    raise HarnessError("审阅格式修复违反原审阅结论；同一快照禁止重复抽取结论")
                if guard.get("unrepairable") or guard.get("exhausted"):
                    raise HarnessError(f"审阅格式无法在保留证据和修复额度内修正；禁止重复派发：{guard['error']}")
                previous_review = self.read_review_correction(guard)
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
                if pending.pop(key, None) is not None:
                    self.save()
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
                    request = self.run["pending_replan"]
                    self.record_replan_stop(request["trigger"], returned, request["attempt"],
                                            format_error=str(error),
                                            returned_artifact=str(self.directory / "dispatches" / previous["id"] / "returned-result.json"))
                    raise HarnessError("指挥已停止再规划；保留格式错误证据，不自动重派") from error
                if role == "reviewer" and previous_review is None:
                    previous_review = {k: v for k, v in returned.items() if k not in ("task_id", "dispatch_id")}
                    artifact = self.directory / "dispatches" / previous["id"] / "returned-result.json"
                    guard = {"artifact": str(artifact),
                             "artifact_digest": receipt["artifact_digest"],
                             "snapshot_digest": previous["snapshot_digest"], "error": str(error)}
                    guards[key] = guard
                    self.save()
                if role == "reviewer":
                    # Validate all immutable fields, even when the first reported
                    # error was in the mutable summary or finding ID.
                    immutable = deepcopy(REVIEW["properties"]["findings"])
                    immutable["items"]["properties"]["id"] = {"type": "string"}
                    try:
                        validate(previous_review["findings"], immutable, "response.findings")
                    except RepairableFormatError as immutable_error:
                        guard.update(unrepairable=True, error=str(immutable_error))
                        self.save()
                        raise HarnessError(f"审阅不可变字段格式无法修复；保留原始证据：{immutable_error}") from error
                # The immutable original remains in the correction guard. A manual
                # resume may retry its correction, but never redraw the verdict.
                repairs = self.run.setdefault("format_repairs", {})
                if repairs.get(key, 0):
                    if role == "reviewer":
                        guard.update(exhausted=True, error=str(error))
                    pending.pop(key, None)
                    self.save()
                    if role == "worker":
                        raise HarnessError(f"{error}；格式修复额度已用，resume 将执行新的普通 worker 尝试并消耗任务、总尝试及派发预算") from error
                    raise
                # Keep a returned invalid correction attached until the terminal
                # guard is durable; an interruption must not turn it into a retry.
                if receipt:
                    self.run["review_receipts"].pop(key)
                    self.save()
                if role == "worker":
                    attempts = self.run["attempts"][task_id]
                    if attempts >= self.config.data["runtime"]["max_attempts"]:
                        raise HarnessError(f"{task_id} 已达尝试次数上限；格式修复也计入预算") from error
                    if self.run["total_attempts"] >= self.config.data["runtime"]["max_total_attempts"]:
                        raise HarnessError("已达 run 总尝试次数上限；格式修复也计入预算") from error
                correction = {"error": str(error), "previous_dispatch": previous["id"],
                              "artifact": str(self.directory / "dispatches" / previous["id"]),
                              "instruction": "Correct the reported output format using the current task and files. Recheck the evidence. Return only this dispatch's IDs; do not relax acceptance or discard blocking findings."}
                if role == "reviewer":
                    correction = self.review_correction(guard, previous_review)
                packet = {**packet, "format_correction": correction}
                pending[key] = {"correction": correction,
                                "snapshot_digest": snapshot_digest(self.snapshot())}
                self.save()
                self.event("output_format_repair", task_id=task_id, role=role, previous_dispatch=previous["id"])

    @staticmethod
    def read_review_correction(guard):
        artifact = Path(guard["artifact"])
        try:
            raw = artifact.read_bytes()
        except OSError as error:
            raise HarnessError("审阅修正的原始结果缺失或无法读取；保留证据，禁止重新抽取结论") from error
        if hashlib.sha256(raw).hexdigest() != guard.get("artifact_digest"):
            raise HarnessError("审阅修正的原始结果已改变；保留证据，禁止重新抽取结论")
        return json.loads(raw)

    @staticmethod
    def review_correction(guard, previous_review):
        preserved = compact_review(previous_review, guard["artifact"])
        return {"error": guard["error"], "artifact": guard["artifact"],
                "preserve_review": {k: v for k, v in preserved.items() if k != "summary"},
                "instruction": "Correct the format using this dispatch's IDs. Read the original result at artifact when preserve_review contains excerpts. Preserve the original verdict, memory approvals and all findings in order, including their exact severity, category, location, message and evidence. Only the summary and invalid finding IDs may be reformatted; do not discard or downgrade findings."}

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
                or snapshot_digest(self.snapshot()) != receipt["snapshot_digest"]):
            raise HarnessError("审阅或 hook 修改了代码/HEAD；原审阅结果不能用于当前快照")
        result = read_json(artifact)
        contract = envelope(schema, entry["task_id"], entry["id"])
        validate_structure(result, contract)
        rejection = None
        try:
            validate_review_semantics(result, [p["key"] for p in packet.get("proposed_memories", [])])
        except HarnessError as error:
            # Never salvage a correction that may have changed its original verdict,
            # or infer rejection merely from invalid approval metadata.
            if previous_review is not None or review_approved(result):
                raise
            rejection = InvalidReviewRejection(result, receipt, error)
        if previous_review is not None:
            try:
                validate_review_correction(previous_review, result)
            except HarnessError:
                self.run["review_corrections"][key]["failed"] = True
                self.save()
                raise
        if rejection is None:
            validate(result, contract)
        if not receipt.get("hook_completed"):
            receipt["hook_attempts"] = receipt.get("hook_attempts", 0) + 1
            self.save()
            logs = artifact.parent / f"post-agent-{receipt['hook_attempts']}"
            self.hooks.emit("post_agent", {**entry, "status": "invalid_rejected" if rejection else "completed",
                                           "result": result,
                                           **({"validation_error": str(rejection)} if rejection else {})}, logs)
            if (git.head(self.worktree) != self.run["expected_head"]
                    or snapshot_digest(self.snapshot()) != receipt["snapshot_digest"]):
                raise HarnessError("reviewer hook 修改了代码；已停止，保留现场")
            receipt["hook_completed"] = True
        entry["status"] = "invalid_rejected" if rejection else "completed"
        if rejection:
            entry["error"] = str(rejection)
        else:
            entry.pop("error", None)
        self.save()
        if rejection:
            self.ensure_exited()
            raise rejection
        return {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}

    def dispatch_once(self, role, kind, task_id, schema, packet, task=None, previous_review=None):
        dispatch_id = str(uuid.uuid4())
        job = self.directory / "dispatches" / dispatch_id
        job.mkdir(parents=True)
        contract = envelope(schema, task_id, dispatch_id)
        required = {"role": role, "kind": kind, "task_id": task_id, "dispatch_id": dispatch_id,
                    "rules": self.config.asset("rules").read_text(encoding="utf-8"),
                    "role_skill": read_skill(self.config.asset("skills"), "harness-" + role),
                    "packet": packet, "output_constraints": output_constraints(contract)}
        if role == "reviewer":
            required["output_constraints"] = review_output_constraints(
                contract, [p["key"] for p in packet.get("proposed_memories", [])])
        if role != "reviewer":
            required["available_skills"] = skills_catalog(self.config.asset("skills"))
        if task and role == "worker":
            required["task_skills"] = {s: read_skill(self.config.asset("skills"), s) for s in task["skills"]}
        query = (task["title"] + " " + task["description"]) if task else self.run["goal"]
        if role == "commander" and kind == "dispatch":
            query = self.run["goal"] + " " + " ".join(t["title"] for t in packet["ready"])
        memories = self.memory.search(query, level=1)
        prompt, report = compose(required, memories, self.config.data["runtime"]["context_chars"])
        write_json(job / "context-report.json", report)
        self.hooks.emit("context_prepared", {"report": report, "run_id": self.run["id"]}, job)
        self.run["dispatches"].append({"id": dispatch_id, "role": role, "kind": kind, "task_id": task_id, "status": "running", "pgid": None})
        entry = self.run["dispatches"][-1]
        self.save()
        self.event("dispatch_started", task_id=task_id, dispatch_id=dispatch_id, role=role)
        def on_start(pgid):
            entry["pgid"] = pgid
            self.save()

        invoked = False
        try:
            self.hooks.emit("pre_agent", entry, job)
            before = self.snapshot()
            entry["snapshot_digest"] = snapshot_digest(before)
            if self.run["agent_dispatches"] >= self.config.data["runtime"]["max_agent_dispatches"]:
                raise HarnessError("已达 run 模型派发次数上限；可显式增大 max_agent_dispatches 后恢复")
            # Persist all invocation budgets together after local preparation.
            # Provider failures still count, while context/hook/snapshot failures do not.
            if role == "worker":
                attempts = self.run["attempts"].get(task_id, 0)
                if attempts >= self.config.data["runtime"]["max_attempts"]:
                    raise HarnessError(f"{task_id} 已达尝试次数上限；格式修复也计入预算")
                if self.run["total_attempts"] >= self.config.data["runtime"]["max_total_attempts"]:
                    raise HarnessError("已达 run 总尝试次数上限；格式修复也计入预算")
                self.run["attempts"][task_id] = attempts + 1
                self.run["total_attempts"] += 1
            self.run["agent_dispatches"] += 1
            if "format_correction" in packet:
                key = f"{role}:{kind}:{task_id}"
                self.run.setdefault("format_repairs", {})[key] = 1
                if key in self.run["pending_format_corrections"]:
                    self.run["pending_format_corrections"][key]["dispatch_id"] = dispatch_id
            if role == "reviewer":
                # Persist the identity and expected path before calling the model:
                # a crash immediately after the atomic result write must not reroll.
                self.run["review_receipts"][f"{role}:{kind}:{task_id}"] = {
                    "artifact": str(job / "returned-result.json"), "dispatch_id": dispatch_id,
                    "snapshot_digest": entry["snapshot_digest"]}
            self.save()
            invoked = True
            result = self.backend.invoke(role=role, model=self.config.role(role, self.commander), cwd=self.worktree,
                                         job=job, schema=contract, prompt=prompt, run_path=self.directory / "state.json",
                                         on_start=on_start, task=task, kind=kind)
            write_json(job / "returned-result.json", result)
            if role != "reviewer" and "format_correction" in packet:
                self.run["pending_format_corrections"].pop(f"{role}:{kind}:{task_id}", None)
                self.save()
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
            if role != "worker" and self.snapshot() != before:
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
            if role != "worker" and self.snapshot() != before:
                raise HarnessError(f"{role} hook 修改了代码；已停止，保留现场")
            return {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
        except InvalidReviewRejection:
            raise
        except BaseException as error:
            entry["status"] = "failed"
            entry["error"] = str(error)
            if not isinstance(error, (KeyboardInterrupt, SystemExit)):
                self.run["last_runtime_error"] = {"role": role, "kind": kind, "task_id": task_id,
                                                 "error": str(error), "artifact": str(job)}
            if role == "worker" and invoked:
                recovery = {"role": role, "kind": kind, "task_id": task_id, "dispatch_id": dispatch_id,
                            "error": str(error), "error_type": type(error).__name__, "artifact": str(job)}
                try:
                    recovery["changed_files"] = git.changed(self.worktree)
                except Exception as inspection_error:
                    recovery["inspection_error"] = str(inspection_error)
                self.run.setdefault("worker_recovery", {})[task_id] = recovery
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
        git.reject_submodules(self.repo)
        ident = uid("run")
        worktree = self.state / "worktrees" / ident
        self.run = {"schema_version": 6, "id": ident, "repo": str(self.repo), "goal": goal, "status": "planning", "commander": self.commander,
                    "branch": "harness/" + ident, "worktree": str(worktree), "tasks": [], "accepted": {}, "attempts": {},
                    "dispatches": [], "expected_head": git.head(self.repo), "feedback": None,
                    "config_path": str(self.config.path), "config_snapshot": deepcopy(self.config.data),
                    "asset_digest": asset_digest(self.config), "base_commit": git.head(self.repo), "original_tasks": [],
                    "total_attempts": 0, "replan_attempts": 0, "final_attempts": 0,
                    "review_history": {}, "final_history": [], "plan_history": [], "format_repairs": {},
                    "review_corrections": {}, "review_receipts": {}, "pending_format_corrections": {}, "agent_dispatches": 0,
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

    def load_run(self, ident):
        if not ident.startswith("run_") or not ident[4:].isalnum():
            raise HarnessError("无效 run ID")
        self.run = read_json(self.state / "runs" / ident / "state.json")
        if not isinstance(self.run, dict):
            raise HarnessError("运行台账必须为对象")
        if self.run.get("schema_version") != 6:
            raise HarnessError("旧 run 状态格式无法安全恢复；请保留证据并使用原版本完成，或开始新 run")
        required = {"id": str, "repo": str, "worktree": str, "status": str, "commander": str,
                    "goal": str, "branch": str, "expected_head": str, "base_commit": str,
                    "config_snapshot": dict, "tasks": list, "original_tasks": list,
                    "accepted": dict, "attempts": dict, "dispatches": list,
                    "review_history": dict, "review_corrections": dict, "review_receipts": dict,
                    "pending_format_corrections": dict,
                    "final_history": list, "plan_history": list, "format_repairs": dict,
                    "total_attempts": int, "agent_dispatches": int, "final_attempts": int,
                    "replan_attempts": int, "plan_approval_required": bool, "plan_approved": bool}
        if (self.run.get("id") != ident or any(type(self.run.get(k)) is not t for k, t in required.items())
                or any(not isinstance(d, dict) for d in self.run["dispatches"])):
            raise HarnessError("运行台账字段缺失或类型无效；保留原文件并核对证据")
        if self.run["repo"] != str(self.repo):
            raise HarnessError("Run 属于另一个项目")

    def abandon(self, ident, *, remove_worktree=False, confirm_exited=()):
        self.load_run(ident)
        with lock(self.directory / "run.lock"):
            # Reload under the lock so a concurrent completion cannot be lost.
            self.load_run(ident)
            if self.run["status"] == "completed":
                raise HarnessError("已完成的 run 不能标记为放弃")
            self.confirm_exited(confirm_exited)
            self.ensure_exited()
            removal_finished = False
            if remove_worktree and not self.run.get("worktree_removed"):
                expected = (self.state / "worktrees" / ident).resolve()
                if self.worktree.resolve() != expected or self.worktree.is_symlink():
                    raise HarnessError("只能移除该 run 所有的隔离 worktree")
                if not self.worktree.exists() and self.run.get("pending_worktree_removal"):
                    registered = {field.removeprefix("worktree ") for field in
                                  git.git(self.repo, "worktree", "list", "--porcelain", "-z").split("\0")
                                  if field.startswith("worktree ")}
                    if str(self.worktree) in registered:
                        raise HarnessError("工作树路径缺失但 Git 注册仍存在；请核对后再清理")
                    removal_finished = True
                else:
                    if not (self.worktree / ".git").is_file():
                        raise HarnessError("只能移除该 run 所有的隔离 worktree")
                    if git.head(self.worktree) != self.run["expected_head"]:
                        raise HarnessError("worktree HEAD 与检查点不同；保留工作树供核对")
                    git.assert_removable_worktree(self.worktree)
            if self.run["status"] != "abandoned":
                self.run["status"] = "abandoned"
                self.save()
                self.checkpoint()
                self.event("run_abandoned")
            if remove_worktree and not self.run.get("worktree_removed"):
                if not removal_finished:
                    self.run["pending_worktree_removal"] = True
                    self.save()
                    git.git(self.repo, "worktree", "remove", str(self.worktree))
                self.run["worktree_removed"] = True
                self.run.pop("pending_worktree_removal", None)
                self.save()
                self.event("worktree_removed")
            return self.run

    def resume(self, ident, *, step=False, approve_plan=False, reverify=False, retry_stopped=None, confirm_exited=()):
        self.load_run(ident)
        with lock(self.directory / "run.lock"):
            self.load_run(ident)
            if self.run["status"] == "abandoned":
                raise HarnessError("该 run 已放弃；保留分支、工作树与证据，不能继续派发")
            self.commander = self.run["commander"]
            if self.run["status"] == "completed":
                if confirm_exited:
                    raise HarnessError("--confirm-exited 仅用于未完成的 run")
                return self.run
            self.reverify_requested = reverify
            changes = self.config.resume_changes(self.run["config_snapshot"])
            if self.run.get("asset_digest") != asset_digest(self.config):
                raise HarnessError("共享规则、skills、MCP 或 hooks 已改变；请恢复原版本后继续")
            self.confirm_exited(confirm_exited)
            self.ensure_exited()
            if git.head(self.worktree) != self.run["expected_head"]:
                raise HarnessError("worktree HEAD 与检查点不同；请先核对提交，不自动重放")
            if retry_stopped is not None:
                stopped = self.run.get("replan_stopped")
                if not stopped or stopped["target"] != retry_stopped:
                    raise HarnessError("--retry-stopped 必须匹配已停止的任务 ID，或 final")
                # Reopen the saved planning request, not another attempt by the
                # already blocked worker. Preserve the original stop as evidence.
                self.run["pending_replan"] = {"stage": "request", "trigger": deepcopy(stopped["trigger"])}
                self.run.pop("replan_stopped")
                self.save()
                self.event("stopped_replan_retry_requested", target=retry_stopped, artifact=stopped["artifact"])
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

    def verify(self, task_id, attempt, *, whole_run=False, artifact_dir=None, before=None):
        folder = Path(artifact_dir) if artifact_dir is not None else self.verification_directory(task_id, attempt, whole_run=whole_run)
        folder.mkdir(parents=True, exist_ok=True)
        # Callers may pass a just-computed snapshot only when no hook, model or
        # verification process has run since. The exit gate always rehashes files.
        before = self.snapshot() if before is None else before
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
        after = self.snapshot()
        if after != before:
            changes = git.snapshot_changes(before, after)
            write_json(folder / "file-changes.json", changes)
            self.run["feedback"] = {"task_id": task_id, "verification": compact_checks(results),
                "verification_changes": {**changes, "artifact": str(folder / "file-changes.json")}}
            raise HarnessError("验证命令修改了待审阅文件；验证必须为只读检查。"
                               f"新增未跟踪文件：{changes['added']}；修改：{changes['modified']}；删除：{changes['deleted']}。"
                               "请配置构建/测试输出位置或显式忽略生成产物；不自动豁免文件。"
                               "运行中可在公共 Git 目录的 info/exclude 精确忽略新生成文件；"
                               "这会改变后续快照可见范围，不豁免已跟踪文件，请先核对文件内容。")
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
        # Persist the route before any budget guard. Increasing a budget must
        # continue this request, never rerun the worker that reported the blocker.
        if not pending:
            self.run["pending_replan"] = {"stage": "pending", "trigger": deepcopy(trigger)}
            self.save()
        final = trigger.get("final_review")
        if final:
            if not git.clean(self.worktree):
                raise HarnessError("整体审阅要求干净的已接受提交；请核对并清理未接受修改")
            if snapshot_digest(self.snapshot()) != final["snapshot_digest"]:
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
            if final and final["status"] in ("rejected", "invalid_rejected"):
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
            self.record_replan_stop(trigger, result, attempt)
            raise HarnessError(f"指挥停止重新规划：{result['reason']}")
        self.run["pending_replan"].update(stage="validated", tasks=tasks, reason=result["reason"],
                                          artifact=str(artifact), head=self.run["expected_head"])
        self.save()
        self.apply_pending_revision()

    def record_replan_stop(self, trigger, result, attempt, **diagnostics):
        artifact = self.directory / "plans" / f"replan-{attempt}.json"
        write_json(artifact, {"trigger": trigger, "result": result, **diagnostics})
        self.run["replan_attempts"] = attempt
        self.run["replan_stopped"] = {"target": trigger.get("task_id") or "final",
            "trigger": deepcopy(trigger), "attempt": attempt, "artifact": str(artifact),
            "reason": result["reason"], **diagnostics}
        self.run.pop("pending_replan", None)
        self.save()

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
        self.run.pop("worker_recovery", None)
        self.run["pending_format_corrections"].clear()
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
                  f"{report['budget_chars']}；建议提前增加 context_chars。此估计不保证后续历史容量。", file=sys.stderr, flush=True)

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
            schema = obj(selected_task={"type": "string", "enum": [t["id"] for t in ready]}, instruction=DETAIL)
            directive = self.dispatch("commander", "dispatch", "dispatching", schema,
                        {"goal": self.run["goal"], "ready": ready, "accepted": self.accepted_context(),
                         "feedback": compact_feedback(self.run.get("feedback")), "attempts": self.run["attempts"],
                         "recovery": deepcopy(self.run.get("worker_recovery", {}).get(current)) if current in {t["id"] for t in ready} else None,
                         "tool_constraints": self.tool_constraints(),
                         "instruction": "Select one ready task. Give a focused implementation or repair instruction, retaining its original scope and acceptance."}, task=ready[0])
        task = next(t for t in ready if t["id"] == directive["selected_task"])
        tid = task["id"]
        attempt = self.run["attempts"].get(tid, 0) + 1
        if attempt > self.config.data["runtime"]["max_attempts"]:
            raise HarnessError(f"{tid} 已达尝试次数上限")
        self.run["current_task"] = tid
        self.save()
        self.checkpoint()
        prior_reviews = compact_reviews(self.run["review_history"].get(tid, []))
        result = self.dispatch("worker", "implement", tid, WORK,
                 {"goal": self.run["goal"], "task": task, "instruction": directive["instruction"],
                  "feedback": compact_feedback(self.run.get("feedback"), prior_reviews), "prior_reviews": prior_reviews,
                  "recovery": deepcopy(self.run.get("worker_recovery", {}).get(tid)),
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
            "snapshot_digest": snapshot_digest(self.snapshot()),
            "worker_result": result, "prior_reviews": prior_reviews, "verification_rounds": 0}
        self.save()
        return self.continue_task()

    def check_task_scope(self, task):
        try:
            return git.check_scope(self.worktree, task["allowed_paths"])
        except HarnessError as error:
            raise HarnessError(f"{error}。工作树：{self.worktree}；请先核对并清理越界修改，再 resume。"
                               "现场与证据已保留，本次预检不会派发 worker。") from error

    def dispatch_review(self, kind, task_id, packet, task=None):
        try:
            review = self.dispatch("reviewer", kind, task_id, REVIEW, packet, task)
        except InvalidReviewRejection as rejection:
            return rejection.review, rejection.diagnostics
        validate_review(review, [p["key"] for p in packet.get("proposed_memories", [])])
        return review, {}

    def preserve_invalidated_reviews(self, pending, key):
        """Keep evidence when a changed candidate invalidates its pending gate."""
        history = self.run["review_history"].setdefault(pending["task_id"], [])
        sources = [self.run["review_corrections"].get(key), self.run["review_receipts"].get(key)]
        for source in sources:
            if not source:
                continue
            artifact = Path(source["artifact"])
            if not artifact.exists() and "artifact_digest" not in source:
                continue  # A provider invocation that never returned a result.
            raw = artifact.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if "artifact_digest" not in source:
                source["artifact_digest"] = digest
                self.save()
            if digest != source["artifact_digest"]:
                raise HarnessError("审阅原始结果已改变；禁止通过候选变化丢弃证据")
            dispatch_id = source.get("dispatch_id", artifact.parent.name)
            if any(item.get("dispatch_id") == dispatch_id for item in history):
                continue
            result = json.loads(raw)
            record = {"status": "invalidated", "attempt": pending["attempt"],
                      "dispatch_id": dispatch_id, "artifact": str(artifact),
                      "artifact_digest": digest, "snapshot_digest": source["snapshot_digest"],
                      "verification": pending.get("verification"),
                      "diff_artifact": pending.get("diff_artifact"),
                      "reason": "候选已变化；原结果仅作待复核证据，不构成当前候选的批准"}
            try:
                validate_structure(result, envelope(REVIEW, pending["task_id"], dispatch_id))
            except HarnessError as error:
                # Unattributable or malformed data remains inspectable, but is not
                # presented as a structurally valid review of this task.
                record["validation_error"] = str(error)
            else:
                record["review"] = {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
                try:
                    validate_review(record["review"], [p["key"] for p in pending.get("proposals", [])])
                except HarnessError as error:
                    record["validation_error"] = str(error)
            history.append(record)

    def continue_task(self):
        pending = self.run["pending_task"]
        tid, attempt = pending["task_id"], pending["attempt"]
        task = next(t for t in self.run["tasks"] if t["id"] == tid)
        self.ensure_exited()
        if git.head(self.worktree) != self.run["expected_head"]:
            raise HarnessError("任务恢复前 HEAD 与检查点不同；保留现场")
        paths = self.check_task_scope(task)
        key = f"reviewer:review:{tid}"
        snapshot = self.snapshot()
        if snapshot_digest(snapshot) != pending["snapshot_digest"]:
            self.preserve_invalidated_reviews(pending, key)
            self.run.pop("pending_task")
            self.run["review_receipts"].pop(key, None)
            self.run["review_corrections"].pop(key, None)
            self.run["pending_format_corrections"].pop(key, None)
            self.run["pending_format_corrections"].pop(f"worker:implement:{tid}", None)
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
            checks = self.verify(tid, attempt, artifact_dir=folder, before=snapshot)
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
            write_json(snapshot_artifact, snapshot)
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
        review, diagnostics = self.dispatch_review("review", tid,
                 {"goal": self.run["goal"], "task": task, "base_commit": self.run["expected_head"], "changed_files": paths,
                  "diff_artifact": str(evidence), "verification": compact_checks(checks),
                  "prior_reviews": prior_reviews,
                  "recovery": deepcopy(self.run.get("worker_recovery", {}).get(tid)),
                  "proposed_memories": proposals,
                  "tool_constraints": self.tool_constraints(),
                  "instruction": "Independently inspect the actual diff, relevant code and acceptance. Prior findings are evidence to recheck, not a complete checklist. Separate blocking defects from advisory preferences. Approve only with no unresolved blocking findings; approve memory keys only when their claims and cited sources are independently supported. Your summary becomes the accepted task record: describe only independently verified outcomes and limits. An empty diff can pass only when current evidence already satisfies the task. Do not edit files."}, task=task)
        review_artifact = evidence.parent / "review.json"
        record = {"attempt": attempt, "review": review, "diff_artifact": str(evidence),
                  "verification": str(evidence.parent / "results.json"), "artifact": str(review_artifact),
                  "snapshot_digest": reviewed_digest, "snapshot_artifact": str(snapshot_artifact), **diagnostics}
        write_json(review_artifact, record)
        approved_keys = [] if diagnostics else review["approved_memory_keys"]
        if not diagnostics and review_approved(review):
            try:
                self.memory.check_proposals(proposals, approved_keys)
            except HarnessError as error:
                approved_keys = []
                self.event("memory_proposals_discarded", task_id=tid, reason=str(error))
        self.run["review_history"].setdefault(tid, []).append(record)
        self.run["review_corrections"].pop(key, None)
        self.run["review_receipts"].pop(key, None)
        self.run.pop("pending_task")
        if diagnostics or not review_approved(review):
            self.run["feedback"] = {"task_id": tid, "review": review, "artifact": str(review_artifact), **diagnostics}
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
        if snapshot_digest(self.snapshot()) != pending["snapshot_digest"]:
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
        if snapshot_digest(self.snapshot()) != pending["snapshot_digest"]:
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
        self.run.get("worker_recovery", {}).pop(tid, None)
        self.run.pop("pending_acceptance")
        detail = {"commit": commit, "summary": accepted["summary"], "summary_source": "reviewer",
                  "changed_file_count": len(paths), "finding_count": len(pending["review"]["findings"]),
                  "verification": accepted["verification"], "review_artifact": accepted["review_artifact"],
                  "snapshot_artifact": pending["snapshot_artifact"]}
        completion = {
            "id": f"acceptance:{self.run['id']}:{tid}",
            "task_memory": {"key": f"task:{self.run['id']}:{tid}", "abstract": task["title"][:500],
                            "overview": accepted["summary"], "detail": dump(detail),
                            "source": f"git:{commit};run:{self.run['id']}", "kind": "accepted_task",
                            "ttl_days": self.config.data["memory"]["task_ttl_days"] or None},
            "proposals": pending["proposals"], "approved_keys": pending["approved_keys"],
            "knowledge_ttl_days": self.config.data["memory"]["knowledge_ttl_days"] or None,
            "knowledge": {"status": "pending"},
        }
        self.run["pending_post_accept"] = {
            "payload": {"task_id": tid, **accepted},
            "logs": str(self.directory / "hooks" / f"{tid}-{attempt}"), "completion": completion}
        self.save()
        self.finish_post_accept()
        self.checkpoint()
        return True

    def final_review(self, *, reverify=False):
        git.check_worktree_state(self.worktree)
        if not git.clean(self.worktree):
            raise HarnessError("整体审阅要求干净的已接受提交；未接受的修改不能用于完成 run")
        snapshot = self.snapshot()
        if snapshot["head"] != self.run["expected_head"]:
            raise HarnessError("整体审阅前 HEAD 与检查点不同；保留现场")
        digest = snapshot_digest(snapshot)
        history = self.run["final_history"]
        same_snapshot = [item for item in history
                         if item["snapshot_digest"] == digest and item["head"] == snapshot["head"]]
        rejected = next((item for item in reversed(same_snapshot)
                         if item["status"] in ("rejected", "invalid_rejected")), None)
        if rejected:
            # A corrective task can validly accept an empty diff. Its acceptance
            # does not erase the earlier rejection; ask for another bounded plan
            # once per newly accepted correction, without calling the reviewer.
            if self.replan_unchanged_corrections(rejected):
                return False
            raise HarnessError("整体审阅已拒绝同一代码快照；必须先通过修正任务，不能恢复后重复抽取审阅")
        if any(item["status"] == "approved" for item in same_snapshot):
            # Approval is persisted only after all validation and snapshot gates.
            return True
        record = history[-1] if history and history[-1] in same_snapshot else None
        if not record or record["status"] != "running":
            if record and record["status"] == "verification_failed" and record["failure_kind"] != "timeout":
                if not reverify:
                    if self.replan_unchanged_corrections(record):
                        return False
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
                      "accepted_tasks": sorted(self.run["accepted"]),
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
            checks = self.verify("final", attempt, whole_run=True, artifact_dir=verification_folder, before=snapshot)
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
            review, diagnostics = self.dispatch_review("final_review", "final",
                     {"goal": self.run["goal"], **self.plan_context(), "accepted": self.accepted_context(),
                      "base_commit": self.run["base_commit"], "head": self.run["expected_head"],
                      "diff_artifact": record["diff_artifact"], "verification": compact_checks(checks),
                      "tool_constraints": self.tool_constraints(),
                      "prior_reviews": compact_reviews([item for item in history[:-1]
                                                        if item["status"] != "running"]),
                      "instruction": FINAL_REVIEW_INSTRUCTION})
            if self.snapshot() != snapshot:
                raise HarnessError("整体审阅后文件发生变化；禁止完成 run")
            record.update(review=review, phase="finished", status="approved" if review_approved(review) else "rejected")
            record.update(diagnostics)
            self.run["review_corrections"].pop("reviewer:final_review:final", None)
            self.run.get("review_receipts", {}).pop("reviewer:final_review:final", None)
            if record["status"] == "approved":
                self.run["feedback"] = None
            else:
                self.run["feedback"] = {"final_review": deepcopy(record)}
                self.run["pending_replan"] = {"stage": "pending", "trigger": deepcopy(self.run["feedback"])}
        write_json(folder / "final.json", record)
        self.save()
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

    def replan_unchanged_corrections(self, record):
        """Empty corrections do not erase a failed verification or review."""
        handled = set(record.get("noop_corrections_handled", []))
        corrections = set(self.run["accepted"]) - set(record["accepted_tasks"]) - handled
        if not corrections:
            return False
        record["noop_corrections_handled"] = sorted(handled | corrections)
        trigger = {"final_review": deepcopy(record), "unchanged_corrections": sorted(corrections),
                   "reason": "已接受的修正任务未改变未通过整体验收的代码快照，需要新的修正计划"}
        self.run["feedback"] = deepcopy(trigger)
        self.run["pending_replan"] = {"stage": "pending", "trigger": trigger}
        self.save()
        self.replan(trigger)
        return True

    def execute(self, supplied_plan=None, *, step=False):
        try:
            stopped = self.run.get("replan_stopped")
            if stopped:
                raise HarnessError(f"指挥停止重新规划：{stopped['reason']}；"
                                   f"显式使用 resume --retry-stopped {stopped['target']} 可重试保存的规划请求")
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
            accepted_this_call = int(self.finish_post_accept())
            if self.run.get("pending_acceptance"):
                if self.accept_pending():
                    accepted_this_call += 1
            if self.run.get("pending_replan"):
                pending = self.run["pending_replan"]
                final = pending.get("trigger", {}).get("final_review", {})
                if (getattr(self, "reverify_requested", False) and pending["stage"] in ("pending", "request")
                        and final.get("status") == "verification_failed"
                        and snapshot_digest(self.snapshot()) == final.get("snapshot_digest")):
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
