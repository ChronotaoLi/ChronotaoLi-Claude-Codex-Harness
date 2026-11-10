"""Own invocation preparation, budgets and bounded format-correction dispatches."""
from __future__ import annotations

import hashlib
import time
import uuid
from copy import deepcopy
from pathlib import Path

from .adapters import provider_usage
from .context import compose
from .continuation import validate_continuation_rejection
from .contracts import (
    RepairableFormatError, validate_immutable_findings,
)
from .receipts import InvalidReviewRejection
from .input_projection import validate_dispatch_indexes as validate_input_dispatch_indexes
from .path_evidence import validate_dispatch_indexes, validate_indexes
from .recovery import snapshot_digest
from .review_progress import bind_worker_findings
from .services import RuntimeService
from .shared import validate_mcp
from .util import group_alive, HarnessError, read_json, write_json


class Dispatcher(RuntimeService):
    def dispatch(self, role, kind, task_id, schema, packet, task=None):
        # A format correction is a new process, never a provider-session resume.
        validate_dispatch_indexes(self.context.directory, self.context.run["dispatches"])
        validate_input_dispatch_indexes(self.context.directory, self.context.run["dispatches"])
        key = f"{role}:{kind}:{task_id}"
        if role == "worker" and key in self.context.run["worker_receipts"]:
            try:
                result = self.services.worker_receipts.finish(key, task)
                if self.context.run["pending_format_corrections"].pop(key, None) is not None:
                    self.context.save()
                return result
            except RepairableFormatError:
                # Read back the referenced immutable input and verify its digest.
                packet = self.services.worker_receipts.original_packet(key)
        packet = self.services.packets.runtime_packet(packet, role=role, kind=kind, task_id=task_id)
        packet = self.services.recovery.invalid_repair_packet(key, packet)
        previous_review = None
        pending = self.context.run.setdefault("pending_format_corrections", {})
        guards = self.context.run.setdefault("review_corrections", {})
        guard = guards.get(key) if role == "reviewer" else None
        candidate_digest = (snapshot_digest(self.context.snapshot())
                            if guard or key in pending else None)
        if key in pending:
            if role == "commander" and not pending[key]["correction"].get("source"):
                # Missing source evidence must fail before candidate invalidation.
                self.services.recovery.commander_format_source(key, {"format_correction": pending[key]["correction"]})
            correction_id = pending[key].get("dispatch_id")
            returned = (correction_id and
                        (self.context.directory / "dispatches" / correction_id / "returned-result.json").exists())
            if role == "worker" and returned:
                # Also closes the crash window after result write, before pending cleanup.
                pending.pop(key)
                self.context.save()
            elif candidate_digest == pending[key]["snapshot_digest"]:
                packet = {**packet, "format_correction": pending[key]["correction"]}
            else:
                pending.pop(key)
                self.context.save()
        if guard:
            if candidate_digest != guard["snapshot_digest"]:
                guards.pop(key)
                pending.pop(key, None)
                self.context.save()
            else:
                if guard.get("failed"):
                    raise HarnessError("审阅格式修复违反原审阅结论；同一快照禁止重复抽取结论；请保留证据并 abandon")
                repair = self.services.recovery.active_invalid_repair(key)
                explicit_format = repair and repair.get("mode") == "format"
                if guard.get("unrepairable"):
                    raise HarnessError(f"审阅不可变字段格式无法修复；请保留证据并 abandon：{guard['error']}")
                if guard.get("exhausted") and not explicit_format:
                    original = self.services.receipts.read_review_correction(guard)
                    raise HarnessError(f"审阅自动格式修正额度已用：{guard['error']}" +
                                       self.services.recovery.format_repair_guidance(key, original))
                previous_review = self.services.receipts.read_review_correction(guard)
                previous_review = {k: v for k, v in previous_review.items() if k not in ("task_id", "dispatch_id")}
                packet = {**packet, "format_correction": self.services.receipts.review_correction(guard, previous_review)}
        if role == "commander":
            self.services.recovery.commander_format_source(key, packet)
        while True:
            self.context.ensure_exited()
            receipts = self.context.run[{"commander": "commander_receipts", "worker": "worker_receipts"}.get(role, "review_receipts")]
            receipt = receipts.get(key)
            if role != "worker" and receipt and "artifact_digest" not in receipt and not Path(receipt["artifact"]).exists():
                self.services.receipts.validate_terminal_capture(receipt)
                # Intent was saved before invoke; the provider never returned a
                # usable result. Retrying still consumes a fresh dispatch budget.
                receipts.pop(key)
                self.context.save()
            try:
                if role == "worker" and key in receipts:
                    result = self.services.worker_receipts.finish(key, task)
                elif role == "reviewer" and key in self.context.run["review_receipts"]:
                    result = self.services.receipts.finish_review_receipt(key, schema, packet, previous_review)
                elif role == "commander" and key in receipts:
                    result = self.services.receipts.finish_commander_receipt(key)
                else:
                    result = self.dispatch_once(role, kind, task_id, schema, packet, task, previous_review)
                self.context.ensure_exited()
                validate_dispatch_indexes(self.context.directory, self.context.run["dispatches"])
                validate_input_dispatch_indexes(self.context.directory, self.context.run["dispatches"])
                repair = self.services.recovery.active_invalid_repair(key)
                if repair:
                    repair.update(status="completed", corrected_dispatch_id=receipts[key]["dispatch_id"])
                    self.context.save()
                if pending.pop(key, None) is not None:
                    self.context.save()
                return result
            except RepairableFormatError as error:
                self.context.ensure_exited()
                receipt = receipts.get(key)
                if self.services.recovery.active_invalid_repair(key):
                    self.services.receipts.record_receipt_error(receipt, error)
                    raise HarnessError("显式输出修正仍不合法；保留已返回结果，不自动增加修正派发") from error
                previous = (next(d for d in self.context.run["dispatches"] if d["id"] == receipt["dispatch_id"])
                            if receipt else self.context.run["dispatches"][-1])
                self.services.receipts.record_receipt_error(receipt, error)
                returned = read_json(self.context.directory / "dispatches" / previous["id"] / "returned-result.json")
                if role == "worker" and returned["status"] == "blocked":
                    artifact = self.context.directory / "dispatches" / previous["id"] / "returned-result.json"
                    self.context.run["feedback"] = {"task_id": task_id, "worker": returned,
                                            "worker_artifact": str(artifact),
                                            "worker_artifact_digest": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                                            "format_error": str(error)}
                    if returned["blocker_kind"] == "task":
                        self.context.run["pending_replan"] = {"stage": "pending",
                            "trigger": deepcopy(self.context.run["feedback"])}
                    self.services.worker_receipts.consume(key, "invalid_blocker")
                    self.context.save()
                    if returned["blocker_kind"] == "operator":
                        self.services.workflow.synchronize_decision_requests()
                    raise HarnessError(f"worker 已报告 {returned['blocker_kind']} blocker；保留格式错误证据，不自动重派") from error
                if role == "commander" and kind == "replan" and returned["action"] == "stop":
                    request = self.context.run["pending_replan"]
                    self.services.planning.record_replan_stop(request["trigger"], returned, request["attempt"],
                                            format_error=str(error),
                                            returned_artifact=str(self.context.directory / "dispatches" / previous["id"] / "returned-result.json"))
                    raise HarnessError("指挥已停止再规划；保留格式错误证据，不自动重派") from error
                if role == "reviewer" and previous_review is None:
                    previous_review = {k: v for k, v in returned.items() if k not in ("task_id", "dispatch_id")}
                    artifact = self.context.directory / "dispatches" / previous["id"] / "returned-result.json"
                    guard = {"artifact": str(artifact),
                             "artifact_digest": receipt["artifact_digest"],
                             "snapshot_digest": previous["snapshot_digest"], "error": str(error)}
                    guards[key] = guard
                    self.context.save()
                if role == "reviewer":
                    # Validate all immutable fields, even when the first reported
                    # error was in the mutable summary or finding ID.
                    try:
                        validate_immutable_findings(previous_review["findings"])
                    except RepairableFormatError as immutable_error:
                        guard.update(unrepairable=True, error=str(immutable_error))
                        self.context.save()
                        raise HarnessError(f"审阅不可变字段格式无法修复；保留原始证据：{immutable_error}") from error
                # The immutable original remains in the correction guard. A manual
                # resume may retry its correction, but never redraw the verdict.
                repairs = self.context.run.setdefault("format_repairs", {})
                if repairs.get(key, 0):
                    if role == "reviewer":
                        guard.update(exhausted=True, error=str(error))
                    pending.pop(key, None)
                    if role == "worker":
                        self.services.worker_receipts.reject(key, error, "format_exhausted")
                        raise HarnessError(f"{error}；格式修复额度已用，resume 将执行新的普通 worker 尝试并消耗任务及派发预算") from error
                    self.context.save()
                    raise HarnessError(str(error) + self.services.recovery.format_repair_guidance(key, previous_review or returned)) from error
                # Keep a returned invalid correction attached until the terminal
                # guard is durable; an interruption must not turn it into a retry.
                if role == "worker":
                    if self.services.budgets.task_budget_used(task_id) >= self.context.config.data["runtime"]["max_attempts"]:
                        raise HarnessError(f"{task_id} 已达尝试次数上限；格式修复也计入预算") from error
                correction = {"error": str(error), "previous_dispatch": previous["id"],
                              "artifact": str(self.context.directory / "dispatches" / previous["id"]),
                              "instruction": "Correct the reported output format using the current task and files. Recheck the evidence. Return only this dispatch's IDs; do not relax acceptance or discard blocking findings."}
                if role == "worker":
                    correction["instruction"] += (
                        " Broker checkpoint evidence must belong to this dispatch, including during format repair. "
                        "Other allowed regular-file evidence remains usable. Omit optional checkpoints if necessary; "
                        "repeat only authorized checks that are safe to repeat, never side effects just to obtain evidence.")
                elif role == "reviewer":
                    correction = self.services.receipts.review_correction(guard, previous_review)
                elif role == "commander":
                    correction["source"] = {field: receipt[field] for field in
                                            ("artifact", "artifact_digest", "dispatch_id", "snapshot_digest")}
                    correction["instruction"] = (
                        "Correct only the reported text format using this dispatch's IDs. Read source.artifact. "
                        "Preserve the original selected_task and action, or action, new_tasks and updates exactly. "
                        "Do not change scope, acceptance or the selected revision operations.")
                packet = {**packet, "format_correction": correction}
                pending[key] = {"correction": correction,
                                "snapshot_digest": snapshot_digest(self.context.snapshot())}
                if receipt:
                    if role == "worker":
                        self.services.worker_receipts.consume(key, "format_correction")
                    else:
                        receipts.pop(key)
                self.context.save()
                self.context.event("output_format_repair", task_id=task_id, role=role, previous_dispatch=previous["id"])
            except InvalidReviewRejection:
                raise
            except HarnessError as error:
                # Also covers post-receipt process/index checks. Worker receipt
                # entry points record their own errors before this outer boundary.
                self.services.receipts.record_receipt_error(receipts.get(key), error)
                raise

    def dispatch_once(self, role, kind, task_id, schema, packet, task=None, previous_review=None):
        self.context.ensure_exited()
        if role == "worker":
            self.services.planning.require_task_commands([task_id])
            # Includes automatic format corrections and retries after local
            # preparation failure, before either invocation budget is consumed.
            self.services.environment.check_environment(task_id)
            packet = self.services.packets.runtime_packet(packet, role=role, kind=kind, task_id=task_id)
        dispatch_id = str(uuid.uuid4())
        job = self.context.directory / "dispatches" / dispatch_id
        job.mkdir(parents=True)
        contract, required = self.services.packets.role_input(
            role=role, kind=kind, task_id=task_id, schema=schema,
            dispatch_id=dispatch_id, packet=packet, task=task)
        query = (task["title"] + " " + task["description"]) if task else self.context.run["goal"]
        if role == "commander" and kind == "dispatch":
            query = self.context.run["goal"] + " " + " ".join(t["title"] for t in packet["ready"])
        memory_diagnostics = {}
        memories = self.context.memory.search(query, level=1, run_id=self.context.run["id"],
                                         diagnostics=memory_diagnostics)
        from .context import ContextCapacityError
        try:
            prompt, report = compose(required, memories, self.context.config.data["runtime"]["context_chars"],
                                     memory_diagnostics=memory_diagnostics)
        except ContextCapacityError as error:
            report = {**error.report, "request_id": self.context.run.get("pending_replan", {}).get("request_id")}
            write_json(job / "context-report.json", report)
            self.context.run["last_runtime_error"] = {"role": role, "kind": kind, "task_id": task_id,
                "error": str(error), "artifact": str(job), "context_report": str(job / "context-report.json"),
                "request_id": report["request_id"]}
            self.context.save()
            raise
        write_json(job / "context-report.json", report)
        self.context.emit_hook("context_prepared", {"report": report}, job)
        self.context.run["dispatches"].append({"id": dispatch_id, "role": role, "kind": kind, "task_id": task_id,
                                       "status": "running", "pgid": None, "process_state": "prepared", "invoked": False})
        entry = self.context.run["dispatches"][-1]
        path_evidence = list(required["packet"].get("path_evidence_indexes", {}).values())
        if path_evidence:
            entry["path_evidence"] = path_evidence
        input_evidence = list(required["packet"].get("input_evidence_indexes", {}).values())
        if input_evidence:
            entry["input_evidence"] = input_evidence
        if role == "worker":
            (job / "commands").mkdir()
            entry["command_directory"] = str(job / "commands")
            bind_worker_findings(self.context, entry, required["packet"])
        self.context.save()
        # Events observe the ledger; the saved invoked flag stays authoritative.
        self._observe("dispatch_prepared", task_id=task_id, dispatch_id=dispatch_id, role=role)
        def on_start(pgid):
            entry["pgid"] = pgid
            entry["process_state"] = "running"
            self.context.save()

        process_outcome = None
        def apply_outcome(value):
            entry.update(process_outcome=value.to_dict(), started=value.started,
                         pid=value.pid, pgid=value.pgid, exit_code=value.exit_code,
                         termination_reason=value.termination_reason,
                         evidence_complete=value.evidence_complete,
                         exit_confirmed=value.exit_confirmed,
                         process_state=("exited" if value.exit_confirmed else
                                        "running" if value.started else "starting"))

        def on_outcome(value):
            nonlocal process_outcome
            process_outcome = value
            apply_outcome(value)
            self.context.save()

        def observe_fallback():
            # Simulated backends have no shared process outcome. Keep their
            # historical exit gate, but observation failure cannot replace the
            # invocation exception or manufacture confirmed exit.
            try:
                entry["exit_confirmed"] = (not startup_interrupted
                    and (not entry["pgid"] or not group_alive(entry["pgid"])))
            except Exception as error:
                entry["exit_confirmed"] = False
                entry["process_observation_error"] = {
                    "type": type(error).__name__, "message": str(error),
                    "errno": getattr(error, "errno", None)}
            if entry["exit_confirmed"]:
                entry["process_state"] = "exited"

        invoked = False
        startup_interrupted = False
        invocation_error = None
        try:
            self.context.emit_hook("pre_agent", entry, job)
            from .workspace_inputs import validate_inputs
            validate_inputs(self.context.run, self.context.directory)
            validate_indexes(self.context.directory, path_evidence)
            validate_mcp(self.context.config, roles=(role,))
            before = self.context.snapshot()
            entry["snapshot_digest"] = snapshot_digest(before)
            rejection = validate_continuation_rejection(self.context, before,
                                                       worker_task=task_id if role == "worker" else None)
            if rejection and role != "worker":
                raise HarnessError("continuation revision requires a fresh worker before other model calls")
            self.services.planning.validate_retained_candidate(before=before, worker_task=task_id if role == "worker" else None)
            self.services.budgets.validate_attempt_accounting()
            self.services.recovery.validate_invalid_repair_source(f"{role}:{kind}:{task_id}", before)
            if role == "commander":
                self.services.recovery.commander_format_source(f"{role}:{kind}:{task_id}", packet, before)
            limit = self.context.config.data["runtime"]["max_agent_dispatches"]
            if self.context.run["agent_dispatches"] >= limit:
                from .diagnostics import remaining_model_calls
                remaining = remaining_model_calls(self.context.run)
                raise HarnessError(
                    f"已达 run 模型派发次数上限（已用 {self.context.run['agent_dispatches']}，上限 {limit}）；"
                    + (remaining["message"] + "；" if remaining else "")
                    + "可显式增大 max_agent_dispatches 后恢复")
            # Persist all invocation budgets together after local preparation.
            # Provider failures still count; local context/hook/MCP/snapshot failures do not.
            if role == "worker":
                attempts = self.context.run["attempts"].get(task_id, 0)
                if self.services.budgets.task_budget_used(task_id) >= self.context.config.data["runtime"]["max_attempts"]:
                    raise HarnessError(f"{task_id} 已达尝试次数上限；格式修复也计入预算")
                # Publish and verify the immutable packet and contract before any
                # budget, receipt or invoked mutation; a failed write charges nothing.
                inputs = self.services.worker_receipts.publish_inputs(entry, contract, required["packet"])
                self.context.run["attempts"][task_id] = attempts + 1
                # The saved selection belongs to this worker attempt. Retrying
                # local preparation keeps it; starting a process consumes it.
                self.context.run.pop("pending_directive", None)
            self.context.run["agent_dispatches"] += 1
            repair = self.services.recovery.active_invalid_repair(f"{role}:{kind}:{task_id}")
            if repair:
                entry["invalid_repair_source"] = repair["source"]["dispatch_id"]
            if "format_correction" in packet:
                entry["format_correction"] = True
                key = f"{role}:{kind}:{task_id}"
                self.context.run.setdefault("format_repairs", {})[key] = 1
                if key in self.context.run["pending_format_corrections"]:
                    self.context.run["pending_format_corrections"][key]["dispatch_id"] = dispatch_id
            if role == "worker":
                self.services.worker_receipts.intent(entry, inputs)
            if role == "reviewer":
                # Persist the identity and expected path before calling the model:
                # a crash immediately after the atomic result write must not reroll.
                self.context.run["review_receipts"][f"{role}:{kind}:{task_id}"] = {
                    "artifact": str(job / "returned-result.json"), "dispatch_id": dispatch_id,
                    "snapshot_digest": entry["snapshot_digest"]}
            elif role == "commander":
                request = {"schema": deepcopy(schema), "packet": deepcopy(packet)}
                self.context.run["commander_receipts"][f"{role}:{kind}:{task_id}"] = {
                    "artifact": str(job / "returned-result.json"), "dispatch_id": dispatch_id,
                    "snapshot_digest": entry["snapshot_digest"],
                    "request": request, "request_digest": snapshot_digest(request),
                    "request_id": (self.context.run["pending_replan"]["request_id"] if kind == "replan"
                                   else dispatch_id)}
            entry["process_state"] = "starting"
            entry["invoked"] = True
            entry["invoked_at"] = time.time()
            if rejection:
                entry["continuation_rejection_id"] = rejection["id"]
            if role == "worker" and self.context.run.get("retained_candidate"):
                entry["retained_candidate_revision"] = self.context.run.pop("retained_candidate")["plan_revision"]
            self.context.save()
            invoked = True
            # Only after the durable invoked=True; a failed append never blocks the call.
            self._observe("dispatch_started", task_id=task_id, dispatch_id=dispatch_id, role=role, invoked=True)
            result = self.context.backend.invoke(role=role, model=self.context.config.role(role, self.context.commander), cwd=self.context.worktree,
                                         job=job, schema=contract, prompt=prompt, run_path=self.context.directory / "state.json",
                                         on_start=on_start, task=task, kind=kind, on_outcome=on_outcome)
            # The synchronous backend has returned through run_process cleanup.
            # Receipt validation may itself check that all processes exited.
            if process_outcome is None:
                observe_fallback()
            if role == "worker":
                self.services.worker_receipts.seal(entry, result)
                return self.services.worker_receipts.finish(f"worker:{kind}:{task_id}", task)
            write_json(job / "returned-result.json", result)
            if role == "reviewer":
                artifact = job / "returned-result.json"
                key = f"{role}:{kind}:{task_id}"
                self.context.run["review_receipts"][key] = {
                    "artifact": str(artifact), "artifact_digest": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "dispatch_id": dispatch_id, "snapshot_digest": entry["snapshot_digest"]}
                self.context.save()
                return self.services.receipts.finish_review_receipt(key, schema, packet, previous_review)
            if role == "commander":
                return self.services.receipts.finish_commander_receipt(f"{role}:{kind}:{task_id}")
        except InvalidReviewRejection:
            raise
        except BaseException as error:
            invocation_error = error
            startup_interrupted = (isinstance(error, (KeyboardInterrupt, SystemExit))
                                   and entry["process_state"] == "starting" and entry["pgid"] is None)
            entry["status"] = "failed"
            entry["error"] = str(error)
            if role in ("reviewer", "commander"):
                receipt = self.context.run["review_receipts" if role == "reviewer" else "commander_receipts"].get(f"{role}:{kind}:{task_id}")
                capture = job / "terminal-capture.json"
                if receipt and capture.exists():
                    if capture.is_file() and not capture.is_symlink():
                        receipt["terminal_capture_digest"] = hashlib.sha256(capture.read_bytes()).hexdigest()
                    artifact = job / "returned-result.json"
                    if artifact.is_file() and not artifact.is_symlink():
                        receipt["artifact_digest"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            if not isinstance(error, (KeyboardInterrupt, SystemExit)):
                self.context.run["last_runtime_error"] = {"role": role, "kind": kind, "task_id": task_id,
                                                 "error": str(error), "artifact": str(job)}
            if role == "worker" and invoked:
                self.services.workflow.record_worker_recovery(task_id, dispatch_id, job, error)
            raise
        finally:
            if process_outcome is None:
                observe_fallback()
            else:
                # Supervision can add callback/persistence failures after the
                # callback returns. Preserve those final facts as well.
                apply_outcome(process_outcome)
            if invoked:
                # Observe the provider envelope even on CLI/protocol failures.
                # Telemetry is best effort and cannot change acceptance/recovery.
                try:
                    from .diagnostics import invocation_diagnostics
                    entry["diagnostics"] = invocation_diagnostics(job, self.context.config.role(role, self.context.commander)["cli"], invocation_error)
                    usage = provider_usage(job, self.context.config.role(role, self.context.commander)["cli"])
                    if usage is not None:
                        entry["provider_usage"] = usage
                except Exception:
                    pass
            self.context.save()
            self._observe("dispatch_finished", task_id=task_id, dispatch_id=dispatch_id, role=role,
                          status=entry["status"], invoked=invoked, provider_usage=entry.get("provider_usage"))

    def _observe(self, event, **fields):
        """Append a best-effort observation; consumers de-duplicate by dispatch_id.

        The ledger and its event log are not written atomically: a crash can omit
        an event, and recovery neither re-emits nor repeats one.
        """
        try:
            self.context.event(event, **fields)
        except Exception:
            pass
