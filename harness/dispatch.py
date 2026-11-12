"""Own invocation preparation, budgets, and the bounded redo of invalid outputs."""
from __future__ import annotations

import hashlib
import time
import uuid
from copy import deepcopy
from pathlib import Path

from .adapters import provider_usage, UndecodableOutput
from .context import compose
from .continuation import validate_continuation_rejection
from .receipts import ERROR_EXCERPT_CHARS, InvalidOutput, InvalidReviewRejection, ScopeStop
from .input_projection import validate_dispatch_indexes as validate_input_dispatch_indexes
from .path_evidence import validate_dispatch_indexes, validate_indexes
from .planning import attempt_limit_error
from .recovery import read_bound_bytes, snapshot_digest
from .services import RuntimeService
from .shared import validate_mcp
from .util import group_alive, HarnessError, write_json

MODEL_ROLES = ("commander", "worker", "reviewer")
JUDGED = ("invalid", "invalid_rejected", "invalid_asked", "invalid_blocked")


def dispatch_key(entry):
    return f"{entry.get('role')}:{entry.get('kind')}:{entry.get('task_id')}"


def last_model_call(dispatches, before=None):
    """The latest invoked commander, worker or reviewer entry (optionally before an index)."""
    entries = dispatches if before is None else dispatches[:before]
    return next((entry for entry in reversed(entries)
                 if entry.get("role") in MODEL_ROLES and entry.get("invoked") is True), None)


class Dispatcher(RuntimeService):
    def redo_basis(self, key):
        """Derive the previous invalid output and redo eligibility from the ledger.

        Only the run's last model call counts: when it is an invalid output of
        this same call without retry_of, this call is its one automatic redo.
        When that invalid output was itself a redo, or a revision stopped only
        for scope authority (never redone), this is a new request (a resume
        after the stop), still shown the previous invalid output.
        """
        last = last_model_call(self.context.run["dispatches"])
        if last is None or dispatch_key(last) != key or last.get("status") != "invalid":
            return None, None
        source = last["invalid_output"]
        read_bound_bytes(source, "无效输出")
        previous = {"dispatch_id": last["id"], "artifact": source["artifact"],
                    "artifact_digest": source["artifact_digest"],
                    "error": str(last.get("error", ""))[:ERROR_EXCERPT_CHARS]}
        return previous, (None if last.get("retry_of") or "out_of_scope_paths" in last else last["id"])

    def dispatch(self, role, kind, task_id, schema, packet, task=None):
        validate_dispatch_indexes(self.context.directory, self.context.run["dispatches"])
        validate_input_dispatch_indexes(self.context.directory, self.context.run["dispatches"])
        key = f"{role}:{kind}:{task_id}"
        if role == "worker" and key in self.context.run["worker_receipts"]:
            return self.services.worker_receipts.finish(key, task)
        packet = self.services.packets.runtime_packet(packet, role=role, kind=kind, task_id=task_id)
        while True:
            self.context.ensure_exited()
            receipts = self.context.run[{"commander": "commander_receipts", "worker": "worker_receipts"}.get(role, "review_receipts")]
            receipt = receipts.get(key)
            if role != "worker" and receipt and "artifact_digest" not in receipt and not Path(receipt["artifact"]).exists():
                # Intent was saved before invoke; the provider never returned a
                # usable result. Retrying still consumes a fresh dispatch budget.
                self.services.receipts.discard_unreturned(key)
            try:
                if role == "worker" and key in receipts:
                    result = self.services.worker_receipts.finish(key, task)
                elif role == "reviewer" and key in self.context.run["review_receipts"]:
                    result = self.services.receipts.finish_review_receipt(key, schema)
                elif role == "commander" and key in receipts:
                    result = self.services.receipts.finish_commander_receipt(key)
                else:
                    previous, retry_of = (None, None) if role == "worker" else self.redo_basis(key)
                    result = self.dispatch_once(role, kind, task_id, schema, packet, task,
                                                retry_of=retry_of, previous_invalid=previous)
                self.context.ensure_exited()
                validate_dispatch_indexes(self.context.directory, self.context.run["dispatches"])
                validate_input_dispatch_indexes(self.context.directory, self.context.run["dispatches"])
                return result
            except UndecodableOutput as error:
                if role == "worker":
                    # The same classification and consumption as a resume after a
                    # crash: an undecodable complete output is an invalid delivery.
                    if self.services.worker_receipts.pending() is not None:
                        raise
                    return None
                self.context.ensure_exited()
                invalid = self.services.receipts.record_invalid(key, error, artifact=error.artifact)
                if invalid.entry.get("retry_of"):
                    raise self.stop_invalid(role, invalid) from error
            except InvalidOutput as invalid:
                if invalid.entry.get("retry_of"):
                    raise self.stop_invalid(role, invalid) from invalid
            except (InvalidReviewRejection, ScopeStop):
                raise
            except HarnessError as error:
                # Also covers post-receipt process/index checks. Worker receipt
                # entry points record their own errors before this outer boundary.
                self.services.receipts.record_receipt_error(receipts.get(key), error)
                raise

    @staticmethod
    def stop_invalid(role, invalid):
        return HarnessError(f"{role} 输出连续两次无效：{invalid}；原件 {invalid.entry['invalid_output']['artifact']}；"
                            "普通 resume 重新请求，或 abandon")

    def dispatch_once(self, role, kind, task_id, schema, packet, task=None, *, retry_of=None, previous_invalid=None):
        self.context.ensure_exited()
        if role == "worker":
            self.services.planning.require_task_commands([task_id])
            # Includes retries after local preparation failure, before either
            # invocation budget is consumed.
            self.services.environment.check_environment(task_id)
            packet = self.services.packets.runtime_packet(packet, role=role, kind=kind, task_id=task_id)
        dispatch_id = str(uuid.uuid4())
        job = self.context.directory / "dispatches" / dispatch_id
        job.mkdir(parents=True)
        if previous_invalid is not None:
            packet = {**packet, "previous_invalid": previous_invalid}
        contract, required = self.services.packets.role_input(
            role=role, kind=kind, task_id=task_id, schema=schema,
            dispatch_id=dispatch_id, packet=packet, task=task)
        query = (task["title"] + " " + task["description"]) if task else self.context.run["goal"]
        if role == "commander":
            ready = set(packet.get("trigger", {}).get("ready", []))
            query = self.context.run["goal"] + " " + " ".join(
                t["title"] for t in self.context.run["tasks"] if t["id"] in ready)
        memory_diagnostics = {}
        memories = self.context.memory.search(query, level=1, run_id=self.context.run["id"],
                                         diagnostics=memory_diagnostics)
        from .context import ContextCapacityError
        hint_omitted = False
        try:
            try:
                prompt, report = compose(required, memories, self.context.config.data["runtime"]["context_chars"],
                                         memory_diagnostics=memory_diagnostics)
            except ContextCapacityError:
                if previous_invalid is None:
                    raise
                # The hint must not decide whether the redo fits: drop it and
                # compose once more; retry_of is still recorded.
                packet = {key: value for key, value in packet.items() if key != "previous_invalid"}
                contract, required = self.services.packets.role_input(
                    role=role, kind=kind, task_id=task_id, schema=schema,
                    dispatch_id=dispatch_id, packet=packet, task=task)
                prompt, report = compose(required, memories, self.context.config.data["runtime"]["context_chars"],
                                         memory_diagnostics=memory_diagnostics)
                hint_omitted = True
        except ContextCapacityError as error:
            report = {**error.report, "request_id": None}
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
            if previous_invalid is not None:
                # The invalid original this call refers to (as a redo, or as the
                # hint of a new request) must still be the bound bytes after
                # local preparation; damage stops before the call is accounted.
                read_bound_bytes(previous_invalid, "无效输出")
            before = self.context.snapshot()
            entry["snapshot_digest"] = snapshot_digest(before)
            rejection = validate_continuation_rejection(self.context, before,
                                                       worker_task=task_id if role == "worker" else None)
            if rejection and role != "worker":
                raise HarnessError("continuation revision requires a fresh worker before other model calls")
            if self.context.run.get("pending_directive"):
                if role != "worker":
                    raise HarnessError("待派发指令存在时不得派发其他模型调用；保留现场")
                # After every local preparation step (pre_agent included) and
                # before the directive or any budget is consumed.
                self.services.planning.check_directive(task_id, before, prepared=True)
            self.services.budgets.validate_attempt_accounting()
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
                    raise attempt_limit_error(task_id, self.context.config.data["runtime"]["max_attempts"])
                # Publish and verify the immutable packet and contract before any
                # budget, receipt or invoked mutation; a failed write charges nothing.
                inputs = self.services.worker_receipts.publish_inputs(entry, contract, required["packet"])
                self.context.run["attempts"][task_id] = attempts + 1
                # The saved selection belongs to this worker attempt. Retrying
                # local preparation keeps it; starting a process consumes it.
                self.context.run.pop("pending_directive", None)
            self.context.run["agent_dispatches"] += 1
            if retry_of is not None:
                entry["retry_of"] = retry_of
                if hint_omitted:
                    entry["previous_invalid_omitted"] = True
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
                    "request_id": dispatch_id}
            entry["process_state"] = "starting"
            entry["invoked"] = True
            entry["invoked_at"] = time.time()
            if rejection:
                entry["continuation_rejection_id"] = rejection["id"]
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
                return self.services.receipts.finish_review_receipt(key, schema)
            if role == "commander":
                return self.services.receipts.finish_commander_receipt(f"{role}:{kind}:{task_id}")
        except (InvalidReviewRejection, InvalidOutput, ScopeStop):
            # Already judged: the entry carries its verdict, never "failed".
            raise
        except BaseException as error:
            invocation_error = error
            startup_interrupted = (isinstance(error, (KeyboardInterrupt, SystemExit))
                                   and entry["process_state"] == "starting" and entry["pgid"] is None)
            # Read and inspect first (Git, evidence digests); change the entry after.
            recovery = (self.services.workflow.worker_recovery(task_id, dispatch_id, job, error)
                        if role == "worker" and invoked else None)
            bound = {}
            receipt = None
            if role in ("reviewer", "commander"):
                receipt = self.context.run["review_receipts" if role == "reviewer" else "commander_receipts"].get(f"{role}:{kind}:{task_id}")
                capture = job / "terminal-capture.json"
                if receipt and capture.exists():
                    if capture.is_file() and not capture.is_symlink():
                        bound["terminal_capture_digest"] = hashlib.sha256(capture.read_bytes()).hexdigest()
                    artifact = job / "returned-result.json"
                    if artifact.is_file() and not artifact.is_symlink():
                        bound["artifact_digest"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            if entry["status"] not in JUDGED:
                # A judged delivery keeps its verdict (e.g. an invalid blocker's
                # stop); only an unjudged failure becomes "failed".
                entry["status"] = "failed"
                entry["error"] = str(error)
            if receipt is not None:
                receipt.update(bound)
            if not isinstance(error, (KeyboardInterrupt, SystemExit)):
                self.context.run["last_runtime_error"] = {"role": role, "kind": kind, "task_id": task_id,
                                                 "error": str(error), "artifact": str(job)}
            if recovery is not None:
                self.context.run.setdefault("worker_recovery", {})[task_id] = recovery
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
