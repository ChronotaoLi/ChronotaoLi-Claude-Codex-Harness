"""Run coordinator and stable lifecycle interception points.

The run dictionary has one owner. Components hold only a live Engine reference;
all durable barriers and cross-component operations resolve these methods at call
time so recovery and fault injection cannot bypass persistence or hook gates.
"""
from __future__ import annotations

import hashlib
import sys
from copy import deepcopy
from pathlib import Path

from . import git
from .adapters import CLIBackend
from .budgets import AttemptAccounting, work_attempts
from .contracts import PLAN
from .decisions import DecisionRecovery, validate_handoff_question
from .dispatch import Dispatcher
from .environment import EnvironmentRecovery
from .finalization import FinalAcceptance
from .memory import Memory
from .packets import PacketBuilder
from .planning import Planner
from .progress import ProgressRecovery
from .receipts import ResultReceipts
from .recovery import InvalidOutputRecovery, snapshot_digest
from .run_lifecycle import RunLifecycle
from .shared import Hooks
from .task_lifecycle import TaskLifecycle
from .util import (
    append_event, confirm_dispatches_exited, dump, ensure_dispatches_exited, group_alive, HarnessError,
    validate_command_directories, write_json,
)
from .verification import Verification


class Engine(EnvironmentRecovery, DecisionRecovery, ProgressRecovery, InvalidOutputRecovery, AttemptAccounting):
    def __init__(self, config, repo, state=None, backend=None, commander="astra", operation=None):
        self.config = config
        self.repo = git.repository(Path(repo))
        self.state = Path(state).resolve() if state else self.repo / ".harness"
        git.ignore_state(self.repo)
        self.memory = Memory(self.state / "memory.sqlite3")
        self.backend = backend or CLIBackend(config, self.state)
        self.commander = commander
        # Observation only: an object with ``id`` and ``link(run_id)``, or None.
        # It is informed after the first ledger save and never gates the lifecycle.
        self.operation = operation
        self.hooks = Hooks(config)
        self.run = None
        self._packets = PacketBuilder(self)
        self._planning = Planner(self)
        self._dispatch = Dispatcher(self)
        self._receipts = ResultReceipts(self)
        self._run_lifecycle = RunLifecycle(self)
        self._verification = Verification(self)
        self._task_lifecycle = TaskLifecycle(self)
        self._finalization = FinalAcceptance(self)

    @property
    def directory(self):
        return self.state / "runs" / self.run["id"]

    @property
    def worktree(self):
        return Path(self.run["worktree"])

    def emit_hook(self, event, payload, logs):
        # Bind every lifecycle event to its run, including resumed receipts and
        # error paths. Verification may explicitly target a fresh checkout.
        context = {**payload, "run_id": self.run["id"], "repo": str(self.repo),
                   "worktree": payload.get("worktree", str(self.worktree)),
                   "run_dir": str(self.directory)}
        return self.hooks.emit(event, context, logs)

    def snapshot(self):
        git.check_worktree_state(self.worktree)
        return git.snapshot(self.worktree, binding=self.run["worktree_binding"])

    def ignored_paths(self):
        return sorted(filter(None, git.git(self.worktree, "ls-files", "--others", "--ignored",
                           "--exclude-standard", "--directory", "-z",
                           binding=self.run["worktree_binding"]).split("\0")))

    def record_ignored_paths(self, stage, folder):
        """Expose ignored-path inventory as diagnostics, never as content proof."""
        names = self.ignored_paths()
        baseline_path = self.directory / "ignored-baseline.json"
        baseline = self.run["ignored_baseline"]
        # Baseline data is durable before its optional inspectable artifact.
        # Recovery must never recapture an initial state after a worker ran.
        if not baseline_path.exists():
            write_json(baseline_path, baseline)
        detail = {"stage": stage, "paths": names, "added": sorted(set(names) - set(baseline)),
                  "removed": sorted(set(baseline) - set(names)),
                  "limitation": "Path inventory only; existing ignored files and directory contents are not hashed."}
        artifact = Path(folder) / "ignored-paths.json"
        write_json(artifact, detail)
        summary = {"stage": stage, "count": len(names), "added_count": len(detail["added"]),
                   "removed_count": len(detail["removed"]), "artifact": str(artifact),
                   "artifact_digest": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                   "coverage": "paths_only_not_contents"}
        self.run["ignored_diagnostics"] = summary
        self.save()
        if detail["added"] or detail["removed"]:
            self.event("ignored_paths_changed", **summary)
        return summary

    def save(self):
        write_json(self.directory / "state.json", self.run)

    def event(self, event, **fields):
        append_event(self.directory / "events.jsonl", self.run["id"], event, **fields)
        print(f"[harness] {event} {fields.get('task_id', '')}", file=sys.stderr, flush=True)

    def checkpoint(self):
        # Model-independent handoff. Full logs remain separate and are never recycled as conversation.
        handoff = {"goal": self.run["goal"], "branch": self.run["branch"], "status": self.run["status"],
                   "accepted": self.run["accepted"], "pending": [t for t in self.run["tasks"] if t["id"] not in self.run["accepted"]],
                   "feedback": self.run.get("feedback"), "expected_head": self.run["expected_head"],
                   "original_tasks": self.run["original_tasks"], "base_commit": self.run["base_commit"],
                   "total_attempts": self.run["total_attempts"], "replan_attempts": self.run["replan_attempts"],
                   "work_attempts": work_attempts(self.run),
                   "final_history": self.run["final_history"], "plan_revision": len(self.run["plan_history"]),
                   "environment_updates": deepcopy(self.run.get("environment_updates", [])),
                   "environment_checks": deepcopy(self.run.get("environment_checks", [])),
                   "decision_requests": deepcopy(self.run.get("decision_requests", [])),
                   "operator_decisions": deepcopy(self.run.get("operator_decisions", [])),
                   "task_checkpoints": deepcopy(self.run.get("task_checkpoints", [])),
                   "next": "Resume the harness run; every dispatch creates a fresh agent process."}
        write_json(self.directory / "handoff.json", handoff)
        (self.directory / "handoff.md").write_text("# Run handoff\n\n```json\n" + dump(handoff) + "\n```\n", encoding="utf-8")

    def accepted_context(self):
        # Keep summaries bounded; accepted commits and artifact paths are never removed.
        return self._packets.accepted_context()

    def plan_context(self):
        return self._packets.plan_context()

    def final_context_capacity(self, tasks=None, original_tasks=None):
        return self._packets.final_context_capacity(tasks, original_tasks)

    def validate_tasks(self, plan):
        return self._planning.validate_tasks(plan)

    def report_command_requirements(self):
        # Recompute from the plan and current grants; the saved report is only
        # an operator diagnostic and must never authorize a dispatch.
        return self._planning.report_command_requirements()

    def require_task_commands(self, task_ids):
        return self._planning.require_task_commands(task_ids)

    def approve_scope_extensions(self, patterns):
        return self._planning.approve_scope_extensions(patterns)

    def ensure_exited(self):
        validate_command_directories(self.run["dispatches"], self.directory)
        for entry in ensure_dispatches_exited(self.run["dispatches"], observe=group_alive):
            entry["exit_confirmed"] = True

    def confirm_exited(self, identifiers):
        """Record an operator's explicit assertion; never signal an unowned group."""
        validate_command_directories(self.run["dispatches"], self.directory)
        confirm_dispatches_exited(self.run["dispatches"], identifiers,
                                  audit=self.event, save=self.save, observe=group_alive)

    def stop(self, error):
        self.run["status"] = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "blocked"
        self.run["error"] = str(error)
        self.run["error_type"] = type(error).__name__
        self.run["error_hook_attempts"] = self.run.get("error_hook_attempts", 0) + 1
        self.save()
        self.checkpoint()
        self.event("run_stopped", error=str(error), error_type=type(error).__name__)
        try:
            self.emit_hook("on_error", {"error": str(error)},
                            self.directory / "hooks" / f"error-{self.run['error_hook_attempts']}")
        except Exception as hook_error:
            self.event("error_hook_failed", error=str(hook_error))

    def tool_constraints(self):
        return self._packets.tool_constraints()

    def runtime_packet(self, packet, *, role, kind, task_id):
        return self._packets.runtime_packet(packet, role=role, kind=kind, task_id=task_id)

    def handoff_targets(self, task_id):
        """Project only unaccepted transitive descendants, in plan order."""
        tasks = {t["id"]: t for t in self.run["tasks"]}
        targets = []
        for task in self.run["tasks"]:
            if task["id"] == task_id or task["id"] in self.run["accepted"]:
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
        feedback = self.run.get("feedback") or {}
        worker = feedback.get("worker", {})
        if worker.get("status") == "blocked" and worker.get("blocker_kind") == "operator":
            source = {"artifact": feedback["worker_artifact"], "artifact_digest": feedback["worker_artifact_digest"],
                      "dispatch_id": Path(feedback["worker_artifact"]).parent.name}
            self.register_decision_request(feedback["task_id"], feedback["task_id"], "worker", source,
                                           "\n".join(worker["blockers"]))
        stopped = self.run.get("replan_stopped")
        if (stopped and stopped.get("dispatch_id") and stopped["reason"].strip()
                and len(stopped["reason"]) <= 4000 and "\0" not in stopped["reason"]):
            artifact = self.directory / "dispatches" / stopped["dispatch_id"] / "returned-result.json"
            source = {"artifact": str(artifact), "artifact_digest": stopped["source_digest"],
                      "dispatch_id": stopped["dispatch_id"]}
            self.register_decision_request(stopped["target"], "replanning", "replan_stop", source, stopped["reason"])
        if not self.run.get("pending_post_accept"):
            for tid, accepted in self.run["accepted"].items():
                for request in accepted.get("decision_requests", []):
                    if request["task_id"] not in self.run["accepted"]:
                        self.register_decision_request(request["task_id"], tid, "handoff", accepted["worker_source"],
                                                       request["question"], request["id"])

    def task_decision_gate(self, task_id):
        return next((r for r in self.decision_context() if r["task_id"] == task_id
                     and r["kind"] in ("worker", "handoff")
                     and (r["response"] is None or r["response"]["decision"] == "stop")), None)

    def record_worker_recovery(self, task_id, dispatch_id, job, error):
        recovery = {"role": "worker", "kind": "implement", "task_id": task_id, "dispatch_id": dispatch_id,
                    "error": str(error), "error_type": type(error).__name__, "artifact": str(job)}
        try:
            recovery["changed_files"] = git.changed(self.worktree, binding=self.run["worktree_binding"])
        except Exception as inspection_error:
            recovery["inspection_error"] = str(inspection_error)
        self.run.setdefault("worker_recovery", {})[task_id] = recovery

    @staticmethod
    def decision_required(request):
        return HarnessError(f"等待操作者决定：task_id={request['task_id']} request_id={request['id']}；"
                            "使用 resume --operator-decision FILE 提供决定，不改变验收或工具权限")

    def finish_post_accept(self):
        return self._task_lifecycle.finish_post_accept()

    def acceptance_event_once(self, event, event_id, **fields):
        return self._task_lifecycle.acceptance_event_once(event, event_id, **fields)

    def dispatch(self, role, kind, task_id, schema, packet, task=None):
        # A format correction is a new process, never a provider-session resume.
        return self._dispatch.dispatch(role, kind, task_id, schema, packet, task)

    def record_receipt_error(self, receipt, error):
        return self._receipts.record_receipt_error(receipt, error)

    def finish_commander_receipt(self, key):
        return self._receipts.finish_commander_receipt(key)

    @staticmethod
    def read_review_correction(guard):
        return ResultReceipts.read_review_correction(guard)

    @staticmethod
    def review_correction(guard, previous_review):
        return ResultReceipts.review_correction(guard, previous_review)

    def finish_review_receipt(self, key, schema, packet, previous_review=None):
        return self._receipts.finish_review_receipt(key, schema, packet, previous_review)

    def dispatch_once(self, role, kind, task_id, schema, packet, task=None, previous_review=None):
        return self._dispatch.dispatch_once(role, kind, task_id, schema, packet, task, previous_review)

    def start(self, goal, plan=None, *, pause_after_plan=False, step=False):
        return self._run_lifecycle.start(goal, plan, pause_after_plan=pause_after_plan, step=step)

    def load_run(self, ident):
        return self._run_lifecycle.load_run(ident)

    def validate_recovery_links(self):
        return self._run_lifecycle.validate_recovery_links()

    def abandon(self, ident, *, remove_worktree=False, confirm_exited=()):
        return self._run_lifecycle.abandon(ident, remove_worktree=remove_worktree, confirm_exited=confirm_exited)

    def resume(self, ident, *, step=False, approve_plan=False, reverify=False, retry_stopped=None, confirm_exited=(),
               repair_invalid=None, allow_scope=(), environment_update=None, operator_decision=None):
        return self._run_lifecycle.resume(ident, step=step, approve_plan=approve_plan, reverify=reverify, retry_stopped=retry_stopped, confirm_exited=confirm_exited, repair_invalid=repair_invalid, allow_scope=allow_scope, environment_update=environment_update, operator_decision=operator_decision)

    def verification_directory(self, task_id, attempt, *, whole_run=False):
        return self._verification.verification_directory(task_id, attempt, whole_run=whole_run)

    def run_verification_commands(self, task_id, commands, cwd, folder, binding, *, phase=None):
        return self._verification.run_verification_commands(task_id, commands, cwd, folder, binding, phase=phase)

    def verify(self, task_id, attempt, *, whole_run=False, artifact_dir=None, before=None):
        return self._verification.verify(task_id, attempt, whole_run=whole_run, artifact_dir=artifact_dir, before=before)

    def verify_fresh_checkout(self, task_id, folder, source_snapshot):
        return self._verification.verify_fresh_checkout(task_id, folder, source_snapshot)

    def record_plan(self, tasks, reason, trigger=None, *, finish_replan=False):
        return self._planning.record_plan(tasks, reason, trigger, finish_replan=finish_replan)

    def replan(self, trigger):
        return self._planning.replan(trigger)

    def recover_replan_worker_correction(self):
        return self._planning.recover_replan_worker_correction()

    def record_replan_stop(self, trigger, result, attempt, **diagnostics):
        return self._planning.record_replan_stop(trigger, result, attempt, **diagnostics)

    def apply_pending_revision(self):
        return self._planning.apply_pending_revision()

    def validate_retained_candidate(self, *, before=None, worker_task=None):
        return self._planning.validate_retained_candidate(before=before, worker_task=worker_task)

    def preflight_final_context(self, tasks=None, original_tasks=None):
        return self._packets.preflight_final_context(tasks, original_tasks)

    def task_attempt(self, ready):
        return self._task_lifecycle.task_attempt(ready)

    def check_task_scope(self, task):
        return self._task_lifecycle.check_task_scope(task)

    def dispatch_review(self, kind, task_id, packet, task=None):
        return self._receipts.dispatch_review(kind, task_id, packet, task)

    def preserve_invalidated_reviews(self, pending, key):
        return self._task_lifecycle.preserve_invalidated_reviews(pending, key)

    def continue_task(self):
        return self._task_lifecycle.continue_task()

    def accept_pending(self):
        return self._task_lifecycle.accept_pending()

    def final_review(self, *, reverify=False):
        # Consume the resume flag on first entry, including new snapshots and
        # unfinished attempts that do not need an explicit retry.
        return self._finalization.final_review(reverify=reverify)

    def replan_unchanged_corrections(self, record):
        return self._finalization.replan_unchanged_corrections(record)

    def execute(self, supplied_plan=None, *, step=False):
        try:
            self.validate_retained_candidate()
            self.synchronize_decision_requests()
            stopped = self.run.get("replan_stopped")
            if stopped:
                raise HarnessError(f"指挥停止重新规划：{stopped['reason']}；"
                                   f"显式使用 resume --retry-stopped {stopped['target']} 可重试保存的规划请求")
            if not self.run["tasks"]:
                # Supplied plans survive startup before their first revision.
                # Model plans must still revalidate any unconsumed receipt.
                if (supplied_plan is None and self.run["original_tasks"]
                        and "commander:plan:planning" not in self.run["commander_receipts"]):
                    supplied_plan = {"tasks": deepcopy(self.run["original_tasks"])}
                plan = supplied_plan if supplied_plan is not None else self.dispatch("commander", "plan", "planning", PLAN,
                         {"goal": self.run["goal"], "instruction": "Inspect the repository, then decompose the goal into bounded tasks. Each worker does one task. Use real dependencies and observable acceptance. Never change files.",
                          "max_tasks": self.config.data["runtime"]["max_tasks"],
                          "tool_constraints": self.tool_constraints(),
                          "verification_commands": self.config.data["verification"]["commands"]})
                tasks = self.validate_tasks(plan)
                self.run["original_tasks"] = deepcopy(tasks)
                self.record_plan(tasks, "Initial plan")
            self.report_command_requirements()
            if not self.run["attempts"] or self.run.get("environment_updates") or self.run.get("operator_decisions"):
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
                receipt_key = "commander:replan:replanning"
                receipt = self.run["commander_receipts"].get(receipt_key)
                returned = (receipt and ("artifact_digest" in receipt or Path(receipt["artifact"]).exists()))
                if (getattr(self, "reverify_requested", False) and pending["stage"] in ("pending", "request")
                        and final.get("status") == "verification_failed"
                        and self.run["final_attempts"] < self.config.data["runtime"]["max_final_attempts"]
                        and not returned and receipt_key not in self.run["pending_format_corrections"]
                        and not self.active_invalid_repair(receipt_key)
                        and snapshot_digest(self.snapshot()) == final.get("snapshot_digest")):
                    self.run.pop("pending_replan")
                    self.run["commander_receipts"].pop(receipt_key, None)
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
                    runnable = [t for t in ready if self.task_decision_gate(t["id"]) is None]
                    if not runnable:
                        raise self.decision_required(self.task_decision_gate(ready[0]["id"]))
                    ready = runnable
                    # An existing selection owns its recovery edits/directive.
                    # Otherwise independent work may proceed while an operator
                    # supplies another task's explicitly declared commands.
                    missing = self.report_command_requirements()
                    if not self.run.get("pending_directive"):
                        runnable = [t for t in ready if t["id"] not in missing]
                        if not runnable:
                            self.require_task_commands([t["id"] for t in ready])
                        ready = runnable
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
