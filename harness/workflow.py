"""Cross-stage policy for handoffs, decisions, and stopping."""
from __future__ import annotations
from pathlib import Path
from . import git
from .decisions import validate_handoff_question
from .util import HarnessError
from .services import RuntimeService

class Workflow(RuntimeService):
    def stop(self, error):
        self.context.run["status"] = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "blocked"
        self.context.run["error"] = str(error)
        self.context.run["error_type"] = type(error).__name__
        self.context.run["error_hook_attempts"] = self.context.run.get("error_hook_attempts", 0) + 1
        self.context.save()
        self.context.checkpoint()
        self.context.event("run_stopped", error=str(error), error_type=type(error).__name__)
        try:
            self.context.emit_hook("on_error", {"error": str(error)},
                            self.context.directory / "hooks" / f"error-{self.context.run['error_hook_attempts']}")
        except Exception as hook_error:
            self.context.event("error_hook_failed", error=str(hook_error))

    def handoff_targets(self, task_id):
        """Project only unaccepted transitive descendants, in plan order."""
        tasks = {t["id"]: t for t in self.context.run["tasks"]}
        targets = []
        for task in self.context.run["tasks"]:
            if task["id"] == task_id or task["id"] in self.context.run["accepted"]:
                continue
            seen, pending = set(), list(task["depends_on"])
            while pending:
                ident = pending.pop()
                if ident not in seen:
                    seen.add(ident)
                    pending.extend(tasks[ident]["depends_on"])
            if task_id in seen:
                targets.append({"id": task["id"], "title": task.get("title", task["id"])})
        return targets

    def validate_handoff_requests(self, task_id, result):
        if result.get("status") == "blocked":
            return
        targets = {task["id"] for task in self.handoff_targets(task_id)}
        for request in result.get("decision_requests", []):
            validate_handoff_question(request["question"])
            if request["task_id"] not in targets:
                raise HarnessError("后续决策必须指向尚未接受且依赖当前任务的任务")

    def synchronize_decision_requests(self):
        """Reconstruct request registration across result/acceptance save windows."""
        feedback = self.context.run.get("feedback") or {}
        worker = feedback.get("worker", {})
        if worker.get("status") == "blocked" and worker.get("blocker_kind") == "operator":
            source = {"artifact": feedback["worker_artifact"], "artifact_digest": feedback["worker_artifact_digest"],
                      "dispatch_id": Path(feedback["worker_artifact"]).parent.name}
            self.services.decisions.register_decision_request(feedback["task_id"], feedback["task_id"], "worker", source,
                                           "\n".join(worker["blockers"]))
        stopped = self.context.run.get("replan_stopped")
        if (stopped and stopped.get("dispatch_id") and stopped["reason"].strip()
                and len(stopped["reason"]) <= 4000 and "\0" not in stopped["reason"]):
            artifact = self.context.directory / "dispatches" / stopped["dispatch_id"] / "returned-result.json"
            source = {"artifact": str(artifact), "artifact_digest": stopped["source_digest"],
                      "dispatch_id": stopped["dispatch_id"]}
            self.services.decisions.register_decision_request(stopped["target"], "replanning", "replan_stop", source, stopped["reason"])
        if not self.context.run.get("pending_post_accept"):
            for tid, accepted in self.context.run["accepted"].items():
                for request in accepted.get("decision_requests", []):
                    if request["task_id"] not in self.context.run["accepted"]:
                        self.services.decisions.register_decision_request(request["task_id"], tid, "handoff", accepted["worker_source"],
                                                       request["question"], request["id"])

    def task_decision_gate(self, task_id):
        return next((r for r in self.services.decisions.decision_context() if r["task_id"] == task_id
                     and r["kind"] in ("worker", "handoff")
                     and (r["response"] is None or r["response"]["decision"] == "stop")), None)

    def worker_recovery(self, task_id, dispatch_id, job, error):
        """Build a worker recovery record without changing the ledger.

        It inspects Git, which can be interrupted: callers build it before they
        change any entry, then publish it with the judgment it belongs to.
        """
        recovery = {"role": "worker", "kind": "implement", "task_id": task_id, "dispatch_id": dispatch_id,
                    "error": str(error), "error_type": type(error).__name__, "artifact": str(job)}
        try:
            recovery["changed_files"] = git.changed(self.context.worktree, binding=self.context.run["worktree_binding"])
        except Exception as inspection_error:
            recovery["inspection_error"] = str(inspection_error)
        return recovery

    @staticmethod
    def decision_required(request):
        return HarnessError(f"等待操作者决定：task_id={request['task_id']} request_id={request['id']}；"
                            "使用 resume --operator-decision FILE 提供决定，不改变验收或工具权限")
