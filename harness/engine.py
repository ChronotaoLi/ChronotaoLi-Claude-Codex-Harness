from __future__ import annotations

import json
import time
import uuid
from copy import deepcopy
from pathlib import Path

from . import git
from .adapters import CLIBackend
from .context import compose
from .contracts import (PLAN, REPLAN, REVIEW, TEXT, WORK, envelope, obj, review_approved,
                        validate, validate_plan, validate_review, validate_revision, validate_work)
from .memory import Memory, validate_proposals
from .shared import Hooks, asset_digest, read_skill, skills_catalog
from .util import HarnessError, dump, group_alive, lock, read_json, run_process, tail, uid, write_json


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
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
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
                      "verification": item["verification"], "details": str(self.directory / "state.json")}
                for tid, item in self.run["accepted"].items()}

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
        dispatch_id = str(uuid.uuid4())
        job = self.directory / "dispatches" / dispatch_id
        job.mkdir(parents=True)
        contract = envelope(schema, task_id, dispatch_id)
        required = {"role": role, "kind": kind, "task_id": task_id, "dispatch_id": dispatch_id,
                    "rules": self.config.asset("rules").read_text(),
                    "role_skill": read_skill(self.config.asset("skills"), "harness-" + role),
                    "packet": packet}
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

        def on_start(pgid):
            entry["pgid"] = pgid
            self.save()

        try:
            result = self.backend.invoke(role=role, model=self.config.role(role, self.commander), cwd=self.worktree,
                                         job=job, schema=contract, prompt=prompt, run_path=self.directory / "state.json",
                                         on_start=on_start, task=task, kind=kind)
            validate(result, contract)
            if git.head(self.worktree) != self.run["expected_head"]:
                raise HarnessError("agent 修改了 Git HEAD；已停止，保留现场")
            if role != "worker" and git.snapshot(self.worktree) != before:
                raise HarnessError(f"{role} 修改了代码；已停止，保留现场")
            entry["status"] = "completed"
            self.hooks.emit("post_agent", {**entry, "result": result}, job)
            if git.head(self.worktree) != self.run["expected_head"]:
                raise HarnessError("agent hook 修改了 Git HEAD；已停止，保留现场")
            if role != "worker" and git.snapshot(self.worktree) != before:
                raise HarnessError(f"{role} hook 修改了代码；已停止，保留现场")
            return {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
        except BaseException:
            entry["status"] = "failed"
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
        self.run = {"schema_version": 2, "id": ident, "repo": str(self.repo), "goal": goal, "status": "planning", "commander": self.commander,
                    "branch": "harness/" + ident, "worktree": str(worktree), "tasks": [], "accepted": {}, "attempts": {},
                    "dispatches": [], "expected_head": git.head(self.repo), "feedback": None,
                    "config_path": str(self.config.path), "config_snapshot": deepcopy(self.config.data),
                    "asset_digest": asset_digest(self.config), "base_commit": git.head(self.repo), "original_tasks": [],
                    "total_attempts": 0, "replan_attempts": 0, "final_attempts": 0,
                    "review_history": {}, "final_history": [], "plan_history": [],
                    "plan_approval_required": pause_after_plan, "plan_approved": not pause_after_plan}
        self.directory.mkdir(parents=True)
        self.save()
        with lock(self.directory / "run.lock"):
            git.git(self.repo, "worktree", "add", "-b", self.run["branch"], str(worktree), "HEAD")
            self.memory.repo = self.worktree
            return self.execute(plan, step=step)

    def resume(self, ident, *, step=False, approve_plan=False):
        if not ident.startswith("run_") or not ident[4:].isalnum():
            raise HarnessError("无效 run ID")
        self.run = read_json(self.state / "runs" / ident / "state.json")
        if self.run["repo"] != str(self.repo):
            raise HarnessError("Run 属于另一个项目")
        if self.run.get("schema_version") != 2:
            raise HarnessError("旧 run 状态格式无法安全恢复；请保留证据并开始新 run")
        self.commander = self.run["commander"]
        changes = self.config.resume_changes(self.run["config_snapshot"])
        if self.run.get("asset_digest") != asset_digest(self.config):
            raise HarnessError("共享规则、skills、MCP 或 hooks 已改变；请恢复原版本后继续")
        with lock(self.directory / "run.lock"):
            if self.run["status"] == "completed":
                return self.run
            for entry in self.run["dispatches"]:
                if not entry.get("exit_confirmed") and entry.get("pgid") and group_alive(entry["pgid"]):
                    raise HarnessError("旧 dispatch 的进程组仍存在；确认退出后才能恢复")
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

    def verify(self, task_id, attempt, *, whole_run=False):
        folder = self.verification_directory(task_id, attempt, whole_run=whole_run)
        folder.mkdir(parents=True, exist_ok=True)
        before = git.snapshot(self.worktree)
        self.hooks.emit("pre_verify", {"task_id": task_id, "worktree": str(self.worktree),
                                      "scope": "run" if whole_run else "task"}, folder)
        results = []
        for i, command in enumerate(self.config.data["verification"]["commands"]):
            log = folder / f"check-{i}"
            entry = {"id": str(uuid.uuid4()), "role": "verification", "task_id": task_id, "status": "running", "pgid": None}
            self.run["dispatches"].append(entry)
            self.save()

            def on_start(pgid):
                entry["pgid"] = pgid
                self.save()

            try:
                code = run_process(command, self.worktree, log, timeout=self.config.data["runtime"]["verify_timeout_seconds"], on_start=on_start)
                entry["status"] = "completed" if code == 0 else "failed"
            finally:
                entry["exit_confirmed"] = not entry["pgid"] or not group_alive(entry["pgid"])
                self.save()
            results.append({"argv": command, "exit_code": code, "stdout": tail(log.with_suffix(".stdout")),
                            "stderr": tail(log.with_suffix(".stderr")), "log": str(log)})
        self.hooks.emit("post_verify", {"task_id": task_id, "results": results}, folder)
        if git.snapshot(self.worktree) != before:
            raise HarnessError("验证命令修改了待审阅文件；验证必须为只读检查")
        write_json(folder / "results.json", results)
        return results

    def record_plan(self, tasks, reason, trigger=None):
        revision = len(self.run["plan_history"])
        artifact = self.directory / "plans" / f"revision-{revision}.json"
        record = {"revision": revision, "reason": reason, "trigger": trigger,
                  "tasks": deepcopy(tasks), "artifact": str(artifact)}
        write_json(artifact, record)
        self.run["plan_history"].append(record)
        self.run["tasks"] = deepcopy(tasks)
        self.save()

    def replan(self, trigger):
        if self.run["total_attempts"] >= self.config.data["runtime"]["max_total_attempts"]:
            raise HarnessError("已达 run 总尝试次数上限；不通过新增任务绕过预算")
        tid = trigger.get("task_id")
        if tid and self.run["attempts"].get(tid, 0) >= self.config.data["runtime"]["max_attempts"]:
            raise HarnessError(f"{tid} 已达尝试次数上限；不通过重新命名任务绕过预算")
        if "final_review" in trigger and self.run["final_attempts"] >= self.config.data["runtime"]["max_attempts"]:
            raise HarnessError("整体审阅已达尝试次数上限；不再派发修正任务")
        if self.run["replan_attempts"] >= self.config.data["runtime"]["max_replans"]:
            raise HarnessError("已达重新规划次数上限；反馈与证据已保留")
        self.run["replan_attempts"] += 1
        attempt = self.run["replan_attempts"]
        self.run["pending_replan"] = deepcopy(trigger)
        self.save()
        result = self.dispatch("commander", "replan", "replanning", REPLAN,
            {"goal": self.run["goal"], "original_tasks": self.run["original_tasks"],
             "tasks": self.run["tasks"], "accepted": self.accepted_context(), "trigger": trigger,
             "attempts": self.run["attempts"], "total_attempts": self.run["total_attempts"],
             "remaining_replans": self.config.data["runtime"]["max_replans"] - attempt,
             "tool_constraints": self.tool_constraints(),
             "instruction": "Revise only when prerequisite or corrective tasks can resolve the recorded blocker. Preserve every existing task ID, accepted task, scope and acceptance. Never remove dependencies or weaken the original goal. New task allowed_paths must be selected from existing scope patterns. Return the complete revised DAG, or stop with an actionable reason. No file edits."})
        artifact = self.directory / "plans" / f"replan-{attempt}.json"
        write_json(artifact, {"trigger": trigger, "result": result})
        if result["action"] == "stop":
            self.run.pop("pending_replan", None)
            self.save()
            raise HarnessError(f"指挥停止重新规划：{result['reason']}")
        tasks = validate_revision(self.run["tasks"], result["tasks"], self.run["accepted"],
                                  self.config.data["runtime"]["max_tasks"])
        if tasks == self.run["tasks"]:
            raise HarnessError("重新规划没有改变计划；保留反馈后停止")
        if len(self.run["accepted"]) == len(tasks):
            raise HarnessError("整体审阅未通过，重新规划必须包含未完成的修正任务")
        # A new prerequisite may own another scope. Never carry the blocked task's edits into it.
        if not git.clean(self.worktree):
            archive = git.archive_and_reset(self.worktree, self.run["expected_head"],
                         self.directory / "recovery" / f"replan-{attempt}", self.state / "worktrees")
            self.event("replan_edits_archived", task_id=tid, artifact=archive)
        self.run["current_task"] = None
        self.run["feedback"] = {"replan": str(artifact), "trigger": trigger}
        self.run.pop("pending_replan", None)
        self.record_plan(tasks, result["reason"], trigger)
        self.checkpoint()
        self.event("plan_revised", revision=len(self.run["plan_history"]) - 1)

    def task_attempt(self, ready):
        current = self.run.get("current_task")
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
                         "feedback": self.run.get("feedback"), "attempts": self.run["attempts"],
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
        result = self.dispatch("worker", "implement", tid, WORK,
                 {"goal": self.run["goal"], "task": task, "instruction": directive["instruction"],
                  "feedback": self.run.get("feedback"), "prior_reviews": self.run["review_history"].get(tid, []),
                  "dependencies": {d: self.accepted_context()[d] for d in task["depends_on"]},
                  "verification_commands": self.config.data["verification"]["commands"],
                  "tool_constraints": self.tool_constraints()}, task=task)
        validate_work(result)
        paths = git.check_scope(self.worktree, task["allowed_paths"])
        if result["status"] == "blocked":
            self.run["feedback"] = {"task_id": tid, "worker": result, "attempt": attempt}
            self.save()
            if result["blocker_kind"] != "task":
                raise HarnessError(f"worker 环境受阻：{result['blockers']} {result['summary']}")
            self.replan(deepcopy(self.run["feedback"]))
            return False
        checks = self.verify(tid, attempt)
        if any(c["exit_code"] for c in checks):
            self.run["feedback"] = {"task_id": tid, "verification": checks}
            self.save()
            self.event("verification_failed", task_id=tid)
            return False
        paths = git.check_scope(self.worktree, task["allowed_paths"])
        evidence = self.verification_directory(tid, attempt) / "change.diff"
        evidence.write_text(git.diff(self.worktree))
        reviewed = git.snapshot(self.worktree)
        try:
            proposals = validate_proposals(result["proposed_memories"], self.worktree)
        except HarnessError as error:
            proposals = []
            self.event("memory_proposals_discarded", task_id=tid, reason=str(error))
        review = self.dispatch("reviewer", "review", tid, REVIEW,
                 {"goal": self.run["goal"], "task": task, "base_commit": self.run["expected_head"], "changed_files": paths,
                  "diff_artifact": str(evidence), "verification": checks,
                  "prior_reviews": deepcopy(self.run["review_history"].get(tid, [])),
                  "proposed_memories": proposals,
                  "tool_constraints": self.tool_constraints(),
                  "instruction": "Independently inspect the actual diff, relevant code and acceptance. Prior findings are evidence to recheck, not a complete checklist. Separate blocking defects from advisory preferences. Approve only with no unresolved blocking findings; approve memory keys only when their claims and cited sources are independently supported. Do not edit files."}, task=task)
        validate_review(review, [p["key"] for p in proposals])
        review_artifact = evidence.parent / "review.json"
        record = {"attempt": attempt, "review": review, "diff_artifact": str(evidence),
                  "verification": str(evidence.parent / "results.json"), "artifact": str(review_artifact)}
        write_json(review_artifact, record)
        self.run["review_history"].setdefault(tid, []).append(record)
        self.save()
        if not review_approved(review):
            self.run["feedback"] = {"task_id": tid, "review": review, "artifact": str(review_artifact)}
            self.save()
            self.event("review_rejected", task_id=tid)
            return False
        approved_keys = review["approved_memory_keys"]
        try:
            self.memory.check_proposals(proposals, approved_keys)
        except HarnessError as error:
            approved_keys = []
            self.event("memory_proposals_discarded", task_id=tid, reason=str(error))
        self.hooks.emit("pre_accept", {"task_id": tid, "review": review}, self.directory / "hooks" / f"{tid}-{attempt}")
        if git.snapshot(self.worktree) != reviewed:
            raise HarnessError("审阅后文件发生变化；禁止接受旧审阅结果")
        commit = git.accept(self.worktree, paths, self.run["id"], tid)
        accepted = {"commit": commit, "summary": result["summary"], "files": paths,
                    "verification": str(evidence.parent / "results.json"), "review": review,
                    "review_artifact": str(review_artifact)}
        self.run["expected_head"] = commit
        self.run["accepted"][tid] = accepted
        self.run["feedback"] = None
        self.run["current_task"] = None
        self.run["pending_post_accept"] = {"payload": {"task_id": tid, **accepted},
                                            "logs": str(self.directory / "hooks" / f"{tid}-{attempt}")}
        self.save()
        self.memory.remember(key=f"task:{self.run['id']}:{tid}", abstract=task["title"][:500],
                             overview=result["summary"][:8000], detail=dump(accepted),
                             source=f"git:{commit};run:{self.run['id']}", kind="accepted_task",
                             ttl_days=self.config.data["memory"]["task_ttl_days"] or None)
        try:
            self.memory.remember_proposals(proposals, approved_keys, commit, self.run["id"], tid,
                                           self.config.data["memory"]["knowledge_ttl_days"] or None)
        except HarnessError as error:
            self.event("memory_proposals_discarded", task_id=tid, reason=str(error))
        self.event("task_accepted", task_id=tid, commit=commit)
        self.finish_post_accept()
        self.checkpoint()
        return True

    def final_review(self):
        if not git.clean(self.worktree):
            raise HarnessError("整体审阅要求干净的已接受提交；未接受的修改不能用于完成 run")
        snapshot = git.snapshot(self.worktree)
        history = self.run["final_history"]
        if history and history[-1]["status"] == "rejected" and history[-1]["snapshot"] == snapshot:
            raise HarnessError("整体审阅已拒绝同一代码快照；必须先通过修正任务，不能恢复后重复抽取审阅")
        if self.run["final_attempts"] >= self.config.data["runtime"]["max_attempts"]:
            raise HarnessError("整体审阅已达尝试次数上限")
        self.run["final_attempts"] += 1
        attempt = self.run["final_attempts"]
        folder = self.verification_directory("final", attempt, whole_run=True)
        record = {"attempt": attempt, "head": self.run["expected_head"], "snapshot": snapshot,
                  "status": "running", "artifact": str(folder / "final.json")}
        history.append(record)
        self.save()
        self.checkpoint()
        checks = self.verify("final", attempt, whole_run=True)
        evidence = folder / "change.diff"
        evidence.write_text(git.range_diff(self.worktree, self.run["base_commit"]))
        record.update(verification=str(folder / "results.json"), diff_artifact=str(evidence))
        if any(c["exit_code"] for c in checks):
            record.update(status="rejected", verification_failures=checks)
        else:
            review = self.dispatch("reviewer", "final_review", "final", REVIEW,
                     {"goal": self.run["goal"], "original_tasks": self.run["original_tasks"],
                      "tasks": self.run["tasks"], "accepted": self.accepted_context(),
                      "base_commit": self.run["base_commit"], "head": self.run["expected_head"],
                      "diff_artifact": str(evidence), "verification": checks,
                      "tool_constraints": self.tool_constraints(),
                      "prior_reviews": [{k: v for k, v in item.items() if k != "snapshot"} for item in history[:-1]],
                      "instruction": "Independently review the complete base-to-HEAD result against the immutable original user goal and original acceptance requirements. Check cross-task integration, omissions and regressions. Task approvals are evidence, not proof of whole-run completion. Separate blocking defects from advisory preferences; approved_memory_keys must be empty. Do not edit files."})
            validate_review(review)
            record.update(review=review, status="approved" if review_approved(review) else "rejected")
        write_json(folder / "final.json", record)
        self.save()
        if git.snapshot(self.worktree) != snapshot:
            raise HarnessError("整体审阅后文件发生变化；禁止完成 run")
        if record["status"] == "approved":
            return True
        self.run["feedback"] = {"final_review": {k: deepcopy(v) for k, v in record.items() if k != "snapshot"}}
        self.save()
        self.checkpoint()
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
                tasks = validate_plan(plan, self.config.data["runtime"]["max_tasks"])
                self.run["original_tasks"] = deepcopy(tasks)
                self.record_plan(tasks, "Initial plan")
            if self.run["plan_approval_required"] and not self.run["plan_approved"]:
                self.run["status"] = "awaiting_approval"
                self.save()
                self.checkpoint()
                self.event("plan_awaiting_approval")
                return self.run
            self.run["status"] = "running"
            self.run.pop("error", None)
            self.save()
            accepted_this_call = 0
            self.finish_post_accept()
            if self.run.get("pending_replan"):
                self.replan(deepcopy(self.run["pending_replan"]))
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
                elif self.final_review():
                    break
            self.run["status"] = "completed"
            self.save()
            self.checkpoint()
            self.event("run_completed")
            return self.run
        except BaseException as e:
            self.run["status"] = "blocked" if isinstance(e, HarnessError) else "interrupted"
            self.run["error"] = str(e)
            self.save()
            self.checkpoint()
            self.event("run_stopped", error=str(e))
            try:
                self.hooks.emit("on_error", {"error": str(e), "run_id": self.run["id"]}, self.directory / "hooks" / "error")
            except Exception as hook_error:
                self.event("error_hook_failed", error=str(hook_error))
            raise
