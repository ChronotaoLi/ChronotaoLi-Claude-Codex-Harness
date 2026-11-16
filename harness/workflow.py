"""Cross-stage policy for handoffs, decisions, and stopping."""
from __future__ import annotations
from pathlib import Path
from . import git, quota_wait, stops
from .decisions import validate_handoff_question
from .util import HarnessError
from .services import RuntimeService

class Workflow(RuntimeService):
    def stop(self, error):
        """Save the status and its settled stop value in one commit, then report the projection.

        A non-interrupt stop is settled first (spec 2.2): the load checks run on the
        ledger about to be saved, and a load failure becomes the final value, written
        back to ``error`` so every later view shows the same stop.
        """
        interrupted = isinstance(error, (KeyboardInterrupt, SystemExit))
        value = stops.of(error)
        self.context.run["status"] = "interrupted" if interrupted else "blocked"
        quota_wait.clear(self.context.run)   # a wait never outlives the stop that ends it
        if interrupted:
            value["data"]["exits_confirmed"] = self.exits_confirmed()
        self.context.run["stop"] = value
        if not interrupted:
            try:
                value = self.services.lifecycle.settle_stop(error)
            except (KeyboardInterrupt, SystemExit) as signal:
                value = stops.of(signal)
                value["data"]["exits_confirmed"] = self.exits_confirmed()
                self.context.run["status"], self.context.run["stop"] = "interrupted", value
                self.context.save()
                raise
            self.context.run["stop"] = value
        self.context.save()
        self.context.checkpoint()
        self.context.event("run_stopped", stop=stops.project(value, self.context.run["id"]))

    def exits_confirmed(self):
        """Whether every dispatch that may have started has a confirmed exit (abandon needs no confirmation)."""
        return all(entry.get("exit_confirmed") is True or entry.get("process_state") == "prepared"
                   for entry in self.context.run.get("dispatches", []))

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

    def commander_gate(self):
        """The latest commander question while it is unanswered or answered stop.

        It blocks every model call, whatever its target. A model call after
        the question (or after its latest answer) retires it.
        """
        dispatches = self.context.run["dispatches"]
        for request in reversed(self.services.decisions.decision_context()):
            if request["kind"] != "commander":
                continue
            response = request["response"]
            boundary = response["dispatch_count"] if response else request["dispatch_count"]
            if any(d.get("invoked", True) for d in dispatches[boundary:]):
                return None
            return request if response is None or response["decision"] == "stop" else None
        return None

    def require_commander_answer(self):
        request = self.commander_gate()
        if request is None:
            return
        if request["response"] is None:
            raise self.decision_required(request)
        raise HarnessError(f"操作者已决定停止：request_id={request['id']}；"
                           "请使用 --operator-decision 显式更新为 continue", stop=self.operator_stopped(request))

    @staticmethod
    def operator_stopped(request):
        return stops.make("operator_decision", "operator_stopped", data={"request_kind": request["kind"]},
                          source={"request_id": request["id"], "task_id": request["task_id"]})

    def worker_recovery(self, task_id, dispatch_id, job):
        """Build a worker recovery record without changing the ledger.

        It inspects Git, which can be interrupted: callers build it before they
        change any entry, then publish it with the judgment it belongs to.
        """
        recovery = {"role": "worker", "kind": "implement", "task_id": task_id, "dispatch_id": dispatch_id,
                    "artifact": str(job)}
        try:
            recovery["changed_files"] = git.changed(self.context.worktree, binding=self.context.run["worktree_binding"])
        except Exception as inspection_error:
            recovery["inspection_error"] = str(inspection_error)
        return recovery

    def decision_required(self, request):
        data = {"request_kind": request["kind"]}
        if request["kind"] == "commander" and request["task_id"] == "final":
            data["reverify_available"] = self.reverify_available()
        return HarnessError(f"等待操作者决定：task_id={request['task_id']} request_id={request['id']}；"
                            "使用 resume --operator-decision FILE 提供决定，不改变验收或工具权限",
                            stop=stops.make("operator_decision", "answer_required", data=data,
                                            source={"request_id": request["id"], "task_id": request["task_id"]}))

    def reverify_available(self):
        """Whether the same snapshot's whole-run record is an ordinary (non-timeout) verification failure.

        Then answering the question together with ``--reverify`` re-verifies the same
        snapshot; a rejection never offers it. A read-only computation, no save.
        """
        from .finalization import same_snapshot_record
        from .recovery import snapshot_digest
        try:
            snapshot = self.context.snapshot()
            record = same_snapshot_record(self.context.run["final_history"], snapshot_digest(snapshot), snapshot["head"])
        except Exception:
            return False
        return bool(record and record["status"] == "verification_failed" and record.get("failure_kind") != "timeout")
