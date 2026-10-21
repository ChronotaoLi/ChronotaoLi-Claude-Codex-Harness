from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from . import git
from .adapters import CLIBackend
from .context import compose
from .contracts import PLAN, REVIEW, TEXT, WORK, envelope, obj, validate, validate_plan
from .memory import Memory
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
                   "next": "Resume the harness run; every dispatch creates a fresh agent process."}
        write_json(self.directory / "handoff.json", handoff)
        (self.directory / "handoff.md").write_text("# Run handoff\n\n```json\n" + dump(handoff) + "\n```\n")

    def accepted_context(self):
        # Keep summaries bounded; accepted commits and artifact paths are never removed.
        return {tid: {"commit": item["commit"], "summary": item["summary"][:600],
                      "summary_truncated": len(item["summary"]) > 600,
                      "verification": item["verification"], "details": str(self.directory / "state.json")}
                for tid, item in self.run["accepted"].items()}

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
        self.hooks.emit("context_compacted", {"report": report, "run_id": self.run["id"]}, job)
        self.run["dispatches"].append({"id": dispatch_id, "role": role, "task_id": task_id, "status": "running", "pgid": None})
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
            return {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
        except BaseException:
            entry["status"] = "failed"
            raise
        finally:
            entry["exit_confirmed"] = not entry["pgid"] or not group_alive(entry["pgid"])
            self.save()

    def start(self, goal, plan=None):
        if not goal.strip():
            raise HarnessError("目标不能为空")
        if not git.clean(self.repo):
            raise HarnessError("目标仓库有未提交变更；请先保存提交，再创建隔离 worktree")
        if not self.config.data["verification"]["commands"]:
            raise HarnessError("请先配置目标项目的 verification.commands")
        ident = uid("run")
        worktree = self.state / "worktrees" / ident
        self.run = {"id": ident, "repo": str(self.repo), "goal": goal, "status": "planning", "commander": self.commander,
                    "branch": "harness/" + ident, "worktree": str(worktree), "tasks": [], "accepted": {}, "attempts": {},
                    "dispatches": [], "expected_head": git.head(self.repo), "feedback": None,
                    "config_path": str(self.config.path), "config_snapshot": self.config.data,
                    "asset_digest": asset_digest(self.config)}
        self.directory.mkdir(parents=True)
        self.save()
        with lock(self.directory / "run.lock"):
            git.git(self.repo, "worktree", "add", "-b", self.run["branch"], str(worktree), "HEAD")
            return self.execute(plan)

    def resume(self, ident):
        if not ident.startswith("run_") or not ident[4:].isalnum():
            raise HarnessError("无效 run ID")
        self.run = read_json(self.state / "runs" / ident / "state.json")
        if self.run["repo"] != str(self.repo):
            raise HarnessError("Run 属于另一个项目")
        self.commander = self.run["commander"]
        if self.run["config_snapshot"] != self.config.data:
            raise HarnessError("配置已改变；请使用原配置恢复，或开始新 run")
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
            return self.execute()

    def verify(self, task_id, attempt):
        folder = self.directory / "verification" / f"{task_id}-{attempt}"
        folder.mkdir(parents=True, exist_ok=True)
        self.hooks.emit("pre_verify", {"task_id": task_id, "worktree": str(self.worktree)}, folder)
        before = git.snapshot(self.worktree)
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

    def execute(self, supplied_plan=None):
        try:
            if not self.run["tasks"]:
                plan = supplied_plan or self.dispatch("commander", "plan", "planning", PLAN,
                         {"goal": self.run["goal"], "instruction": "Inspect the repository, then decompose the goal into bounded tasks. Each worker does one task. Use real dependencies and observable acceptance. Never change files.",
                          "max_tasks": self.config.data["runtime"]["max_tasks"],
                          "verification_commands": self.config.data["verification"]["commands"]})
                self.run["tasks"] = validate_plan(plan, self.config.data["runtime"]["max_tasks"])
                self.save()
            self.run["status"] = "running"
            self.run.pop("error", None)
            self.save()
            while len(self.run["accepted"]) < len(self.run["tasks"]):
                ready = [t for t in self.run["tasks"] if t["id"] not in self.run["accepted"] and set(t["depends_on"]) <= set(self.run["accepted"])]
                if self.run.get("current_task"):
                    ready = [t for t in ready if t["id"] == self.run["current_task"]]
                if not ready:
                    raise HarnessError("没有可执行任务")
                if self.run.get("current_task") and self.run["attempts"].get(self.run["current_task"], 0) >= self.config.data["runtime"]["max_attempts"]:
                    raise HarnessError(f"{self.run['current_task']} 已达尝试次数上限；需要缩小任务或修订目标")
                choices = [t["id"] for t in ready]
                schema = obj(selected_task={"type": "string", "enum": choices}, instruction=TEXT)
                directive = self.dispatch("commander", "dispatch", "dispatching", schema,
                            {"goal": self.run["goal"], "ready": ready, "accepted": self.accepted_context(),
                             "feedback": self.run.get("feedback"), "instruction": "Select one ready task. Give a focused implementation or repair instruction, retaining its original scope and acceptance."}, task=ready[0])
                task = next(t for t in ready if t["id"] == directive["selected_task"])
                tid = task["id"]
                attempts = self.run["attempts"].get(tid, 0)
                if attempts >= self.config.data["runtime"]["max_attempts"]:
                    raise HarnessError(f"{tid} 已达尝试次数上限；需要缩小任务或修订目标")
                attempt = attempts + 1
                self.run["attempts"][tid] = attempt
                self.run["current_task"] = tid
                self.save()
                self.checkpoint()
                result = self.dispatch("worker", "implement", tid, WORK,
                         {"task": task, "instruction": directive["instruction"], "feedback": self.run.get("feedback"),
                          "dependencies": {d: self.accepted_context()[d] for d in task["depends_on"]},
                          "verification_commands": self.config.data["verification"]["commands"]}, task=task)
                paths = git.check_scope(self.worktree, task["allowed_paths"])
                if result["status"] != "completed" or result["blockers"]:
                    raise HarnessError(f"worker 受阻：{result['blockers']} {result['summary']}")
                checks = self.verify(tid, attempt)
                if any(c["exit_code"] for c in checks):
                    self.run["feedback"] = {"task_id": tid, "verification": checks}
                    self.save()
                    self.event("verification_failed", task_id=tid)
                    continue
                evidence = self.directory / "verification" / f"{tid}-{attempt}" / "change.diff"
                evidence.write_text(git.diff(self.worktree))
                reviewed = git.snapshot(self.worktree)
                review = self.dispatch("reviewer", "review", tid, REVIEW,
                         {"task": task, "base_commit": self.run["expected_head"], "changed_files": paths,
                          "diff_artifact": str(evidence), "verification": checks,
                          "instruction": "Independently inspect the actual diff and relevant code. Check acceptance, correctness and regressions. Do not edit files. Approve only when findings is empty."}, task=task)
                if review["verdict"] != "approve" or review["findings"]:
                    self.run["feedback"] = {"task_id": tid, "review": review}
                    self.save()
                    self.event("review_rejected", task_id=tid)
                    continue
                self.hooks.emit("pre_accept", {"task_id": tid, "review": review}, self.directory / "hooks" / f"{tid}-{attempt}")
                if git.snapshot(self.worktree) != reviewed:
                    raise HarnessError("审阅后文件发生变化；禁止接受旧审阅结果")
                commit = git.accept(self.worktree, paths, self.run["id"], tid)
                accepted = {"commit": commit, "summary": result["summary"], "files": paths,
                            "verification": str(evidence.parent / "results.json"), "review": review}
                self.run["expected_head"] = commit
                self.run["accepted"][tid] = accepted
                self.run["feedback"] = None
                self.run["current_task"] = None
                self.save()
                self.memory.remember(key=f"task:{self.run['id']}:{tid}", abstract=task["title"][:500],
                                     overview=result["summary"][:8000], detail=dump(accepted), source=f"git:{commit};run:{self.run['id']}", kind="accepted_task")
                self.event("task_accepted", task_id=tid, commit=commit)
                self.hooks.emit("post_accept", {"task_id": tid, **accepted}, self.directory / "hooks" / f"{tid}-{attempt}")
                self.checkpoint()
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
