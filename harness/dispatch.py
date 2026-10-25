"""Own invocation preparation, budgets and bounded format-correction dispatches."""
from __future__ import annotations

import hashlib
import time
import uuid
from copy import deepcopy
from pathlib import Path

from . import git
from .adapters import provider_usage
from .context import compose
from .contracts import (
    RepairableFormatError, validate, validate_immutable_findings, validate_structure,
    validate_work_semantics,
)
from .receipts import InvalidReviewRejection
from .recovery import snapshot_digest
from .services import RuntimeService
from .shared import validate_mcp
from .util import group_alive, HarnessError, read_json, write_json


class Dispatcher(RuntimeService):
    def dispatch(self, role, kind, task_id, schema, packet, task=None):
        # A format correction is a new process, never a provider-session resume.
        runtime = self.runtime
        key = f"{role}:{kind}:{task_id}"
        packet = runtime.runtime_packet(packet, role=role, kind=kind, task_id=task_id)
        packet = runtime.invalid_repair_packet(key, packet)
        previous_review = None
        pending = runtime.run.setdefault("pending_format_corrections", {})
        guards = runtime.run.setdefault("review_corrections", {})
        guard = guards.get(key) if role == "reviewer" else None
        candidate_digest = (snapshot_digest(runtime.snapshot())
                            if guard or key in pending else None)
        if key in pending:
            if role == "commander" and not pending[key]["correction"].get("source"):
                # Legacy evidence must not disappear through candidate invalidation.
                runtime.commander_format_source(key, {"format_correction": pending[key]["correction"]})
            correction_id = pending[key].get("dispatch_id")
            returned = (correction_id and
                        (runtime.directory / "dispatches" / correction_id / "returned-result.json").exists())
            if role == "worker" and returned:
                # Also closes the crash window after result write, before pending cleanup.
                pending.pop(key)
                runtime.save()
            elif candidate_digest == pending[key]["snapshot_digest"]:
                packet = {**packet, "format_correction": pending[key]["correction"]}
            else:
                pending.pop(key)
                runtime.save()
        if guard:
            if candidate_digest != guard["snapshot_digest"]:
                guards.pop(key)
                pending.pop(key, None)
                runtime.save()
            else:
                if guard.get("failed"):
                    raise HarnessError("审阅格式修复违反原审阅结论；同一快照禁止重复抽取结论；请保留证据并 abandon")
                repair = runtime.active_invalid_repair(key)
                explicit_format = repair and repair.get("mode") == "format"
                if guard.get("unrepairable"):
                    raise HarnessError(f"审阅不可变字段格式无法修复；请保留证据并 abandon：{guard['error']}")
                if guard.get("exhausted") and not explicit_format:
                    original = runtime.read_review_correction(guard)
                    raise HarnessError(f"审阅自动格式修正额度已用：{guard['error']}" +
                                       runtime.format_repair_guidance(key, original))
                previous_review = runtime.read_review_correction(guard)
                previous_review = {k: v for k, v in previous_review.items() if k not in ("task_id", "dispatch_id")}
                packet = {**packet, "format_correction": runtime.review_correction(guard, previous_review)}
        if role == "commander":
            runtime.commander_format_source(key, packet)
        while True:
            runtime.ensure_exited()
            receipts = runtime.run["commander_receipts"] if role == "commander" else runtime.run["review_receipts"]
            receipt = receipts.get(key)
            if receipt and "artifact_digest" not in receipt and not Path(receipt["artifact"]).exists():
                # Intent was saved before invoke; the provider never returned a
                # usable result. Retrying still consumes a fresh dispatch budget.
                receipts.pop(key)
                runtime.save()
            try:
                if role == "reviewer" and key in runtime.run["review_receipts"]:
                    result = runtime.finish_review_receipt(key, schema, packet, previous_review)
                elif role == "commander" and key in receipts:
                    result = runtime.finish_commander_receipt(key)
                else:
                    result = runtime.dispatch_once(role, kind, task_id, schema, packet, task, previous_review)
                runtime.ensure_exited()
                repair = runtime.active_invalid_repair(key)
                if repair:
                    repair.update(status="completed", corrected_dispatch_id=receipts[key]["dispatch_id"])
                    runtime.save()
                if pending.pop(key, None) is not None:
                    runtime.save()
                return result
            except RepairableFormatError as error:
                runtime.ensure_exited()
                receipt = receipts.get(key)
                if runtime.active_invalid_repair(key):
                    runtime.record_receipt_error(receipt, error)
                    raise HarnessError("显式输出修正仍不合法；保留已返回结果，不自动增加修正派发") from error
                previous = (next(d for d in runtime.run["dispatches"] if d["id"] == receipt["dispatch_id"])
                            if receipt else runtime.run["dispatches"][-1])
                runtime.record_receipt_error(receipt, error)
                returned = read_json(runtime.directory / "dispatches" / previous["id"] / "returned-result.json")
                if role == "worker" and returned["status"] == "blocked":
                    artifact = runtime.directory / "dispatches" / previous["id"] / "returned-result.json"
                    runtime.run["feedback"] = {"task_id": task_id, "worker": returned,
                                            "worker_artifact": str(artifact),
                                            "worker_artifact_digest": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                                            "format_error": str(error)}
                    if returned["blocker_kind"] == "task":
                        runtime.run["pending_replan"] = {"stage": "pending",
                            "trigger": deepcopy(runtime.run["feedback"])}
                    runtime.save()
                    if returned["blocker_kind"] == "operator":
                        runtime.synchronize_decision_requests()
                    raise HarnessError(f"worker 已报告 {returned['blocker_kind']} blocker；保留格式错误证据，不自动重派") from error
                if role == "commander" and kind == "replan" and returned["action"] == "stop":
                    request = runtime.run["pending_replan"]
                    runtime.record_replan_stop(request["trigger"], returned, request["attempt"],
                                            format_error=str(error),
                                            returned_artifact=str(runtime.directory / "dispatches" / previous["id"] / "returned-result.json"))
                    raise HarnessError("指挥已停止再规划；保留格式错误证据，不自动重派") from error
                if role == "reviewer" and previous_review is None:
                    previous_review = {k: v for k, v in returned.items() if k not in ("task_id", "dispatch_id")}
                    artifact = runtime.directory / "dispatches" / previous["id"] / "returned-result.json"
                    guard = {"artifact": str(artifact),
                             "artifact_digest": receipt["artifact_digest"],
                             "snapshot_digest": previous["snapshot_digest"], "error": str(error)}
                    guards[key] = guard
                    runtime.save()
                if role == "reviewer":
                    # Validate all immutable fields, even when the first reported
                    # error was in the mutable summary or finding ID.
                    try:
                        validate_immutable_findings(previous_review["findings"])
                    except RepairableFormatError as immutable_error:
                        guard.update(unrepairable=True, error=str(immutable_error))
                        runtime.save()
                        raise HarnessError(f"审阅不可变字段格式无法修复；保留原始证据：{immutable_error}") from error
                # The immutable original remains in the correction guard. A manual
                # resume may retry its correction, but never redraw the verdict.
                repairs = runtime.run.setdefault("format_repairs", {})
                if repairs.get(key, 0):
                    if role == "reviewer":
                        guard.update(exhausted=True, error=str(error))
                    pending.pop(key, None)
                    runtime.save()
                    if role == "worker":
                        raise HarnessError(f"{error}；格式修复额度已用，resume 将执行新的普通 worker 尝试并消耗任务、总尝试及派发预算") from error
                    raise HarnessError(str(error) + runtime.format_repair_guidance(key, previous_review or returned)) from error
                # Keep a returned invalid correction attached until the terminal
                # guard is durable; an interruption must not turn it into a retry.
                if role == "worker":
                    if runtime.task_budget_used(task_id) >= runtime.config.data["runtime"]["max_attempts"]:
                        raise HarnessError(f"{task_id} 已达尝试次数上限；格式修复也计入预算") from error
                    if runtime.run["total_attempts"] >= runtime.config.data["runtime"]["max_total_attempts"]:
                        raise HarnessError("已达 run 总尝试次数上限；格式修复也计入预算") from error
                correction = {"error": str(error), "previous_dispatch": previous["id"],
                              "artifact": str(runtime.directory / "dispatches" / previous["id"]),
                              "instruction": "Correct the reported output format using the current task and files. Recheck the evidence. Return only this dispatch's IDs; do not relax acceptance or discard blocking findings."}
                if role == "worker":
                    correction["instruction"] += (
                        " Broker checkpoint evidence must belong to this dispatch, including during format repair. "
                        "Other allowed regular-file evidence remains usable. Omit optional checkpoints if necessary; "
                        "repeat only authorized checks that are safe to repeat, never side effects just to obtain evidence.")
                elif role == "reviewer":
                    correction = runtime.review_correction(guard, previous_review)
                elif role == "commander":
                    correction["source"] = {field: receipt[field] for field in
                                            ("artifact", "artifact_digest", "dispatch_id", "snapshot_digest")}
                    correction["instruction"] = (
                        "Correct only the reported text format using this dispatch's IDs. Read source.artifact. "
                        "Preserve the original selected_task, or action, new_tasks and updates exactly. "
                        "Do not change scope, acceptance or the selected revision operations.")
                packet = {**packet, "format_correction": correction}
                pending[key] = {"correction": correction,
                                "snapshot_digest": snapshot_digest(runtime.snapshot())}
                if receipt:
                    receipts.pop(key)
                runtime.save()
                runtime.event("output_format_repair", task_id=task_id, role=role, previous_dispatch=previous["id"])
            except InvalidReviewRejection:
                raise
            except HarnessError as error:
                runtime.record_receipt_error(receipts.get(key), error)
                raise

    def dispatch_once(self, role, kind, task_id, schema, packet, task=None, previous_review=None):
        runtime = self.runtime
        if role == "worker":
            runtime.require_task_commands([task_id])
            # Includes automatic format corrections and retries after local
            # preparation failure, before either invocation budget is consumed.
            runtime.check_environment(task_id)
            packet = runtime.runtime_packet(packet, role=role, kind=kind, task_id=task_id)
        dispatch_id = str(uuid.uuid4())
        job = runtime.directory / "dispatches" / dispatch_id
        job.mkdir(parents=True)
        contract, required = runtime._packets.role_input(
            role=role, kind=kind, task_id=task_id, schema=schema,
            dispatch_id=dispatch_id, packet=packet, task=task)
        query = (task["title"] + " " + task["description"]) if task else runtime.run["goal"]
        if role == "commander" and kind == "dispatch":
            query = runtime.run["goal"] + " " + " ".join(t["title"] for t in packet["ready"])
        memories = runtime.memory.search(query, level=1, run_id=runtime.run["id"])
        prompt, report = compose(required, memories, runtime.config.data["runtime"]["context_chars"])
        write_json(job / "context-report.json", report)
        runtime.emit_hook("context_prepared", {"report": report}, job)
        runtime.run["dispatches"].append({"id": dispatch_id, "role": role, "kind": kind, "task_id": task_id,
                                       "status": "running", "pgid": None, "process_state": "prepared", "invoked": False})
        entry = runtime.run["dispatches"][-1]
        if role == "worker":
            (job / "commands").mkdir()
            entry["command_directory"] = str(job / "commands")
        runtime.save()
        runtime.event("dispatch_started", task_id=task_id, dispatch_id=dispatch_id, role=role)
        def on_start(pgid):
            entry["pgid"] = pgid
            entry["process_state"] = "running"
            runtime.save()

        invoked = False
        startup_interrupted = False
        try:
            runtime.emit_hook("pre_agent", entry, job)
            validate_mcp(runtime.config, roles=(role,))
            before = runtime.snapshot()
            entry["snapshot_digest"] = snapshot_digest(before)
            runtime.validate_attempt_accounting()
            runtime.validate_invalid_repair_source(f"{role}:{kind}:{task_id}", before)
            if role == "commander":
                runtime.commander_format_source(f"{role}:{kind}:{task_id}", packet, before)
            if runtime.run["agent_dispatches"] >= runtime.config.data["runtime"]["max_agent_dispatches"]:
                raise HarnessError("已达 run 模型派发次数上限；可显式增大 max_agent_dispatches 后恢复")
            # Persist all invocation budgets together after local preparation.
            # Provider failures still count; local context/hook/MCP/snapshot failures do not.
            if role == "worker":
                attempts = runtime.run["attempts"].get(task_id, 0)
                if runtime.task_budget_used(task_id) >= runtime.config.data["runtime"]["max_attempts"]:
                    raise HarnessError(f"{task_id} 已达尝试次数上限；格式修复也计入预算")
                if runtime.run["total_attempts"] >= runtime.config.data["runtime"]["max_total_attempts"]:
                    raise HarnessError("已达 run 总尝试次数上限；格式修复也计入预算")
                runtime.run["attempts"][task_id] = attempts + 1
                runtime.run["total_attempts"] += 1
                # The saved selection belongs to this worker attempt. Retrying
                # local preparation keeps it; starting a process consumes it.
                runtime.run.pop("pending_directive", None)
            runtime.run["agent_dispatches"] += 1
            repair = runtime.active_invalid_repair(f"{role}:{kind}:{task_id}")
            if repair:
                entry["invalid_repair_source"] = repair["source"]["dispatch_id"]
            if "format_correction" in packet:
                key = f"{role}:{kind}:{task_id}"
                runtime.run.setdefault("format_repairs", {})[key] = 1
                if key in runtime.run["pending_format_corrections"]:
                    runtime.run["pending_format_corrections"][key]["dispatch_id"] = dispatch_id
            if role == "reviewer":
                # Persist the identity and expected path before calling the model:
                # a crash immediately after the atomic result write must not reroll.
                runtime.run["review_receipts"][f"{role}:{kind}:{task_id}"] = {
                    "artifact": str(job / "returned-result.json"), "dispatch_id": dispatch_id,
                    "snapshot_digest": entry["snapshot_digest"]}
            elif role == "commander":
                request = {"schema": deepcopy(schema), "packet": deepcopy(packet)}
                runtime.run["commander_receipts"][f"{role}:{kind}:{task_id}"] = {
                    "artifact": str(job / "returned-result.json"), "dispatch_id": dispatch_id,
                    "snapshot_digest": entry["snapshot_digest"],
                    "request": request, "request_digest": snapshot_digest(request),
                    "request_id": (runtime.run["pending_replan"]["request_id"] if kind == "replan"
                                   else dispatch_id)}
            entry["process_state"] = "starting"
            entry["invoked"] = True
            entry["invoked_at"] = time.time()
            runtime.save()
            invoked = True
            result = runtime.backend.invoke(role=role, model=runtime.config.role(role, runtime.commander), cwd=runtime.worktree,
                                         job=job, schema=contract, prompt=prompt, run_path=runtime.directory / "state.json",
                                         on_start=on_start, task=task, kind=kind)
            # The synchronous backend has returned through run_process cleanup.
            # Receipt validation may itself check that all processes exited.
            entry["exit_confirmed"] = not entry["pgid"] or not group_alive(entry["pgid"])
            entry["process_state"] = "exited" if entry["exit_confirmed"] else "running"
            write_json(job / "returned-result.json", result)
            if role == "worker" and "format_correction" in packet:
                runtime.run["pending_format_corrections"].pop(f"{role}:{kind}:{task_id}", None)
                runtime.save()
            if role == "reviewer":
                artifact = job / "returned-result.json"
                key = f"{role}:{kind}:{task_id}"
                runtime.run["review_receipts"][key] = {
                    "artifact": str(artifact), "artifact_digest": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "dispatch_id": dispatch_id, "snapshot_digest": entry["snapshot_digest"]}
                runtime.save()
                return runtime.finish_review_receipt(key, schema, packet, previous_review)
            if role == "commander":
                return runtime.finish_commander_receipt(f"{role}:{kind}:{task_id}")
            if git.head(runtime.worktree, binding=runtime.run["worktree_binding"]) != runtime.run["expected_head"]:
                raise HarnessError("agent 修改了 Git HEAD；已停止，保留现场")
            # Commander/reviewer receipts own their read-only gates above.
            # Only the mutating worker result reaches this acceptance boundary.
            git.check_scope(runtime.worktree, task["allowed_paths"], binding=runtime.run["worktree_binding"])
            validate_structure(result, contract)
            validate_work_semantics(result)
            entry["worker_outcome"] = ("completed" if result["status"] == "completed"
                                       else result["blocker_kind"] + "_blocked")
            validate(result, contract)
            entry["status"] = "completed"
            runtime.emit_hook("post_agent", {**entry, "result": result}, job)
            if git.head(runtime.worktree, binding=runtime.run["worktree_binding"]) != runtime.run["expected_head"]:
                raise HarnessError("agent hook 修改了 Git HEAD；已停止，保留现场")
            return {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
        except InvalidReviewRejection:
            raise
        except BaseException as error:
            startup_interrupted = (isinstance(error, (KeyboardInterrupt, SystemExit))
                                   and entry["process_state"] == "starting" and entry["pgid"] is None)
            entry["status"] = "failed"
            entry["error"] = str(error)
            if not isinstance(error, (KeyboardInterrupt, SystemExit)):
                runtime.run["last_runtime_error"] = {"role": role, "kind": kind, "task_id": task_id,
                                                 "error": str(error), "artifact": str(job)}
            if role == "worker" and invoked:
                runtime.record_worker_recovery(task_id, dispatch_id, job, error)
            raise
        finally:
            entry["exit_confirmed"] = (not startup_interrupted
                                       and (not entry["pgid"] or not group_alive(entry["pgid"])))
            if entry["exit_confirmed"]:
                entry["process_state"] = "exited"
            if invoked:
                # Observe the provider envelope even on CLI/protocol failures.
                # Telemetry is best effort and cannot change acceptance/recovery.
                try:
                    usage = provider_usage(job, runtime.config.role(role, runtime.commander)["cli"])
                    if usage is not None:
                        entry["provider_usage"] = usage
                except Exception:
                    pass
            runtime.save()
            try:
                runtime.event("dispatch_finished", task_id=task_id, dispatch_id=dispatch_id, role=role,
                           status=entry["status"], provider_usage=entry.get("provider_usage"))
            except Exception:
                pass
