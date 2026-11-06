"""Sequential task scheduling; services own their stage implementations."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
from .contracts import PLAN
from .continuation import continuation_gate, resolve_continuation, validate_continuation_rejection
from .recovery import invalidate_acceptance_review, snapshot_digest
from .util import HarnessError
from .services import RuntimeService

class Scheduler(RuntimeService):
    def admission(self):
        self.context.ensure_exited()
        validate_continuation_rejection(self.context)
        if self.context.run.get("pending_continuation"):
            blocker = continuation_gate(self.context)
            if blocker is not None:
                raise HarnessError(f"等待续作条件：{blocker}")
            resolve_continuation(self.context)

    def execute(self, supplied_plan=None, *, step=False):
        try:
            self.admission()
            self.services.planning.validate_retained_candidate()
            if invalidate_acceptance_review(self.context.run):
                self.context.save()
            self.services.workflow.synchronize_decision_requests()
            stopped = self.context.run.get("replan_stopped")
            if stopped:
                raise HarnessError(f"指挥停止重新规划：{stopped['reason']}；"
                                   f"显式使用 resume --retry-stopped {stopped['target']} 可重试保存的规划请求")
            if not self.context.run["tasks"]:
                # Supplied plans survive startup before their first revision.
                # Model plans must still revalidate any unconsumed receipt.
                if (supplied_plan is None and self.context.run["original_tasks"]
                        and "commander:plan:planning" not in self.context.run["commander_receipts"]):
                    supplied_plan = {"tasks": deepcopy(self.context.run["original_tasks"])}
                plan = supplied_plan if supplied_plan is not None else self.services.dispatch.dispatch("commander", "plan", "planning", PLAN,
                         {"goal": self.context.run["goal"], "instruction": "Inspect the repository, then decompose the goal into bounded tasks. Each worker does one task. Use real dependencies and observable acceptance. Never change files.",
                          "max_tasks": self.context.config.data["runtime"]["max_tasks"],
                          "tool_constraints": self.services.packets.tool_constraints(),
                          "verification_commands": self.context.config.data["verification"]["commands"]})
                tasks = self.services.planning.validate_tasks(plan)
                self.context.run["original_tasks"] = deepcopy(tasks)
                self.services.planning.record_plan(tasks, "Initial plan")
            self.services.planning.report_command_requirements()
            if not self.context.run["attempts"] or self.context.run.get("environment_updates") or self.context.run.get("operator_decisions"):
                self.services.packets.preflight_final_context()
            if self.context.run["plan_approval_required"] and not self.context.run["plan_approved"]:
                self.context.run["status"] = "awaiting_approval"
                self.context.save()
                self.context.checkpoint()
                self.context.event("plan_awaiting_approval")
                return self.context.run
            self.context.run["status"] = "running"
            self.context.run.pop("error", None)
            self.context.run.pop("error_type", None)
            self.context.save()
            accepted_this_call = int(self.services.tasks.finish_post_accept())
            if self.context.run.get("pending_acceptance"):
                if self.services.tasks.accept_pending():
                    accepted_this_call += 1
            if self.context.run.get("pending_replan"):
                pending = self.context.run["pending_replan"]
                final = pending.get("trigger", {}).get("final_review", {})
                receipt_key = "commander:replan:replanning"
                receipt = self.context.run["commander_receipts"].get(receipt_key)
                returned = (receipt and ("artifact_digest" in receipt or Path(receipt["artifact"]).exists()))
                if (self.context.reverify_requested and pending["stage"] in ("pending", "request")
                        and final.get("status") == "verification_failed"
                        and self.context.run["final_attempts"] < self.context.config.data["runtime"]["max_final_attempts"]
                        and not returned and receipt_key not in self.context.run["pending_format_corrections"]
                        and not self.services.recovery.active_invalid_repair(receipt_key)
                        and snapshot_digest(self.context.snapshot()) == final.get("snapshot_digest")):
                    self.context.run.pop("pending_replan")
                    self.context.run["commander_receipts"].pop(receipt_key, None)
                    self.context.event("unvalidated_replan_cancelled_for_reverification", trigger=deepcopy(pending))
                    self.context.save()
            while True:
                self.admission()
                if self.context.run.get("pending_replan"):
                    self.services.planning.replan(deepcopy(self.context.run["pending_replan"]["trigger"]))
                    continue
                if self.context.run.get("pending_task"):
                    if self.services.tasks.continue_task():
                        accepted_this_call += 1
                    continue
                pending = [t for t in self.context.run["tasks"] if t["id"] not in self.context.run["accepted"]]
                if pending:
                    if step and accepted_this_call:
                        self.context.run["status"] = "paused"
                        self.context.save()
                        self.context.checkpoint()
                        self.context.event("run_paused")
                        return self.context.run
                    ready = [t for t in pending if set(t["depends_on"]) <= set(self.context.run["accepted"])]
                    if self.context.run.get("current_task"):
                        ready = [t for t in ready if t["id"] == self.context.run["current_task"]]
                    if not ready:
                        raise HarnessError("没有可执行任务")
                    runnable = [t for t in ready if self.services.workflow.task_decision_gate(t["id"]) is None]
                    if not runnable:
                        raise self.services.workflow.decision_required(self.services.workflow.task_decision_gate(ready[0]["id"]))
                    ready = runnable
                    # An existing selection owns its recovery edits/directive.
                    # Otherwise independent work may proceed while an operator
                    # supplies another task's explicitly declared commands.
                    missing = self.services.planning.report_command_requirements()
                    if not self.context.run.get("pending_directive"):
                        runnable = [t for t in ready if t["id"] not in missing]
                        if not runnable:
                            self.services.planning.require_task_commands([t["id"] for t in ready])
                        ready = runnable
                    if self.services.tasks.task_attempt(ready):
                        accepted_this_call += 1
                elif self.services.finalization.final_review(reverify=self.context.reverify_requested):
                    break
            self.context.run["status"] = "completed"
            self.context.save()
            self.context.checkpoint()
            self.context.event("run_completed")
            return self.context.run
        except BaseException as e:
            self.services.workflow.stop(e)
            raise
