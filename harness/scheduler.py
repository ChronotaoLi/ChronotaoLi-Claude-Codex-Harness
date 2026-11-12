"""Sequential task scheduling; services own their stage implementations.

``next_step`` is the single derivation of what happens next. It reads only the
ledger, the worktree snapshot, the configured command grants and this process's
``--reverify``/``--step`` options; no persistent flag says a decision is needed.
"""
from __future__ import annotations
from collections import namedtuple
from copy import deepcopy
from .continuation import continuation_gate, resolve_continuation, validate_continuation_rejection
from .finalization import needs_decision, same_snapshot_record
from .planning import attempt_limit_error, DECIDE_KEY, directive_mismatch
from .recovery import invalidate_acceptance_review, snapshot_digest
from .util import HarnessError
from .services import RuntimeService

Step = namedtuple("Step", "kind data")
# Steps that advance durable state; none may run beside an unlanded decide receipt.
ADVANCING = ("apply_revision", "continue_task", "supplied_plan", "worker", "final_review")


class Scheduler(RuntimeService):
    def admission(self):
        self.context.ensure_exited()
        validate_continuation_rejection(self.context)
        if self.context.run.get("pending_continuation"):
            blocker = continuation_gate(self.context)
            if blocker is not None:
                raise HarnessError(f"等待续作条件：{blocker}")
            resolve_continuation(self.context)

    def next_step(self, *, step=False, accepted_this_call=0):
        # Every step derives from the plan: its published files are the authority.
        self.services.planning.verify_plan_authority()
        result = self._next_step(step=step, accepted_this_call=accepted_this_call)
        if (DECIDE_KEY in self.context.run["commander_receipts"] and result.kind in ADVANCING
                and not (result.kind == "final_review" and self.context.reverify_requested)):
            raise HarnessError("指挥决定回执与当前推导不符；保留现场")
        return result

    def _next_step(self, *, step, accepted_this_call):
        run = self.context.run
        workflow, planning = self.services.workflow, self.services.planning
        workflow.require_commander_answer()
        if run.get("pending_revision"):
            return Step("apply_revision", None)
        if run.get("pending_task"):
            return Step("continue_task", None)
        if not run["tasks"]:
            if run["original_tasks"]:
                return Step("supplied_plan", {"tasks": deepcopy(run["original_tasks"])})
            return Step("decide", {"kind": "plan"})
        pending = [t for t in run["tasks"] if t["id"] not in run["accepted"]]
        if pending:
            if step and accepted_this_call:
                return Step("pause", None)
            ready = [t for t in pending if set(t["depends_on"]) <= set(run["accepted"])]
            if run.get("current_task"):
                ready = [t for t in ready if t["id"] == run["current_task"]]
            if not ready:
                raise HarnessError("没有可执行任务")
            runnable = [t for t in ready if workflow.task_decision_gate(t["id"]) is None]
            if not runnable:
                raise workflow.decision_required(workflow.task_decision_gate(ready[0]["id"]))
            ready = runnable
            # An existing selection owns its recovery edits/directive.
            # Otherwise independent work may proceed while an operator
            # supplies another task's explicitly declared commands.
            missing = planning.report_command_requirements()
            if not run.get("pending_directive"):
                runnable = [t for t in ready if t["id"] not in missing]
                if not runnable:
                    planning.require_task_commands([t["id"] for t in ready])
                ready = runnable
            current = run.get("current_task")
            limit = self.context.config.data["runtime"]["max_attempts"]
            if current:
                self.services.tasks.check_task_scope(next(t for t in run["tasks"] if t["id"] == current))
                if self.services.budgets.task_budget_used(current) >= limit:
                    raise attempt_limit_error(current, limit)
                # A task's prerequisites must not prevent selecting other ready work.
                # The selected worker is checked again before its invocation is counted.
                self.services.environment.check_environment(current)
            saved = run.get("pending_directive")
            if saved:
                task = next((t for t in ready if t["id"] == saved["directive"]["selected_task"]), None)
                if task is None:
                    raise directive_mismatch()
                return Step("worker", (task, saved["directive"], True))
            if planning.published_directive_missing():
                raise HarnessError("修订已发布但待派发指令缺失；保留现场")
            if len(ready) == 1 and not run.get("feedback") and not run["attempts"].get(ready[0]["id"]):
                # Direct dispatch: the worker skill states the default; no commander text.
                return Step("worker", (ready[0], {"selected_task": ready[0]["id"], "action": "implement",
                                                  "instruction": None}, False))
            return Step("decide", planning.task_trigger(ready))
        record = None
        if run["final_history"]:
            # Only an earlier whole-run record can bind this snapshot to a decision.
            snapshot = self.context.snapshot()
            record = same_snapshot_record(run["final_history"], snapshot_digest(snapshot), snapshot["head"])
        if needs_decision(record, self.context.reverify_requested):
            return Step("decide", self.services.finalization.trigger(record))
        return Step("final_review", None)

    def execute(self, supplied_plan=None, *, step=False):
        try:
            self.admission()
            replayed = self.services.tasks.replay_worker()
            if invalidate_acceptance_review(self.context.run):
                self.context.save()
            self.services.workflow.synchronize_decision_requests()
            while not self.context.run["tasks"]:
                planned = self.next_step()
                if planned.kind == "supplied_plan":
                    plan = supplied_plan if supplied_plan is not None else planned.data
                    self.services.planning.record_plan(self.services.planning.validate_tasks(plan), "Initial plan")
                elif planned.kind == "decide":
                    self.services.planning.decide(planned.data)
                else:
                    raise HarnessError("首版计划尚未登记；保留现场")
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
            accepted_this_call = int(bool(replayed)) + int(self.services.tasks.finish_post_accept())
            if self.context.run.get("pending_acceptance"):
                if self.services.tasks.accept_pending():
                    accepted_this_call += 1
            while True:
                self.admission()
                action = self.next_step(step=step, accepted_this_call=accepted_this_call)
                if action.kind == "apply_revision":
                    self.services.planning.apply_pending_revision()
                elif action.kind == "continue_task":
                    if self.services.tasks.continue_task():
                        accepted_this_call += 1
                elif action.kind == "pause":
                    self.context.run["status"] = "paused"
                    self.context.save()
                    self.context.checkpoint()
                    self.context.event("run_paused")
                    return self.context.run
                elif action.kind == "worker":
                    if self.services.tasks.task_attempt(*action.data):
                        accepted_this_call += 1
                elif action.kind == "decide":
                    self.services.planning.decide(action.data)
                elif action.kind == "final_review":
                    if self.services.finalization.final_review(reverify=self.context.reverify_requested):
                        break
                else:
                    raise HarnessError(f"调度推导出未知步骤：{action.kind}")
            self.services.planning.verify_plan_authority()
            self.context.run["status"] = "completed"
            self.context.save()
            self.context.checkpoint()
            self.context.event("run_completed")
            return self.context.run
        except BaseException as e:
            self.services.workflow.stop(e)
            raise
