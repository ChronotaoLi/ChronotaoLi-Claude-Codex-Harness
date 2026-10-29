"""Explicit, bounded repair of attributable non-rejecting model output."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .contracts import (REVIEW, RepairableFormatError, apply_revision, envelope,
                        review_approved, validate, validate_commander_format_correction,
                        validate_immutable_findings, validate_review_correction,
                        validate_review_semantics, validate_structure)
from .util import HarnessError


# Shared pending lifecycle gates; each recovery entry keeps its own extra checks.
RECOVERY_BUSY_KEYS = (
    "pending_task", "pending_acceptance", "pending_post_accept",
    "pending_replan", "pending_directive", "review_receipts",
    "commander_receipts", "pending_format_corrections", "review_corrections",
)


def snapshot_digest(snapshot):
    encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def invalidate_acceptance_review(run, feedback=None, *, attempt=None, candidate_digest=None):
    """Persist a known acceptance invalidation without changing its evidence.

    Legacy feedback carries the exact review artifact even when its history
    record predates the durable invalidation marker. Call before replacing that
    feedback, so a later plan cannot make the old approval look current again.
    """
    feedback = run.get("feedback") if feedback is None else feedback
    if not isinstance(feedback, dict) or not isinstance(feedback.get("acceptance"), dict):
        return False
    artifact = feedback["acceptance"].get("review_artifact")
    if not artifact:
        return False
    changed = False
    for record in run.get("review_history", {}).get(feedback.get("task_id"), []):
        if (record.get("artifact") == artifact
                and (attempt is None or record.get("attempt") == attempt)
                and (candidate_digest is None or record.get("snapshot_digest") == candidate_digest)
                and record.get("status") != "invalidated"):
            record["status"] = "invalidated"
            changed = True
    return changed


def worker_recovery_context(run, directory, task_id):
    """Separate a retained failure from the latest evidenced task stages.

    This is navigation over persisted records, never an acceptance decision or
    a claim that the original failure's cause was fixed. Only one worker and
    its matching stages are projected; full histories remain in the ledger.
    """
    recovery = run.get("worker_recovery", {}).get(task_id)
    if not recovery:
        return None
    result = deepcopy(recovery)
    workers = [entry for entry in run.get("dispatches", [])
               if entry.get("role") == "worker" and entry.get("task_id") == task_id]
    invoked = [entry for entry in workers if entry.get("invoked") is True]
    # Do not assign ordinal attempts to legacy ledgers with missing invocation
    # evidence, or confuse a local preparation failure with a worker attempt.
    numbered = (all(type(entry.get("invoked")) is bool for entry in workers)
                and len(invoked) == run.get("attempts", {}).get(task_id))
    attempts = {entry["id"]: index for index, entry in enumerate(invoked, 1)} if numbered else {}

    def source(entry):
        value = {"dispatch_id": entry["id"], "status": entry.get("status", "unknown"),
                 "artifact": str(Path(directory) / "dispatches" / entry["id"])}
        for key in ("invoked_at", "worker_outcome"):
            if key in entry:
                value[key] = entry[key]
        if entry["id"] in attempts:
            value["attempt"] = attempts[entry["id"]]
        return value

    original = next((entry for entry in workers if entry["id"] == recovery.get("dispatch_id")), None)
    latest = next((entry for entry in reversed(workers) if entry.get("invoked") is not False), None)
    result["source"] = source(original) if original else {"status": "unknown"}
    result["relation"] = "unknown"
    if latest is None:
        return result
    result["latest"] = {"worker": source(latest)}
    if original is latest:
        result["relation"] = "latest_worker_failure"
    elif original is not None and workers.index(original) < workers.index(latest):
        result["relation"] = "earlier_worker_failure"
        # A subsequent invocation alone does not supersede the failed result.
        if latest.get("status") == "completed" and latest.get("worker_outcome"):
            result["superseded_by"] = latest["id"]

    attempt = attempts.get(latest["id"])
    if attempt is None or latest.get("status") != "completed" or latest.get("worker_outcome") != "completed":
        return result
    pending = run.get("pending_task", {})
    if pending.get("task_id") == task_id and pending.get("attempt") == attempt:
        if pending.get("verification"):
            result["latest"]["verification"] = {
                "attempt": attempt, "artifact": pending["verification"],
                "status": "passed" if pending["stage"] == "reviewing" else "pending"}
        if pending["stage"] == "reviewing":
            result["latest"]["review"] = {"attempt": attempt, "status": "pending"}
        return result
    record = next((item for item in reversed(run.get("review_history", {}).get(task_id, []))
                   if item.get("attempt") == attempt), None)
    if record:
        feedback = run.get("feedback") or {}
        acceptance = feedback.get("acceptance", {}) if feedback.get("task_id") == task_id else {}
        acceptance_invalidated = (bool(record.get("artifact"))
                                  and acceptance.get("review_artifact") == record["artifact"])
        invalidated = record.get("status") == "invalidated" or acceptance_invalidated
        if record.get("verification"):
            result["latest"]["verification"] = {
                "attempt": attempt, "artifact": record["verification"],
                "status": "invalidated" if invalidated else "passed"}
        review = {key: record[key] for key in ("attempt", "artifact", "snapshot_digest", "dispatch_id")
                  if key in record}
        review["status"] = ("invalidated" if invalidated else record["status"] if "status" in record else
                            "approved" if review_approved(record["review"]) else "rejected")
        if acceptance_invalidated:
            review["invalidation_source"] = "feedback.acceptance"
        if "review" in record:
            review["verdict"] = record["review"]["verdict"]
        result["latest"]["review"] = review
    return result


def read_bound_bytes(receipt, label, *, bind=None):
    """Read and bind original bytes before parsing or any later lifecycle gate."""
    try:
        raw = Path(receipt["artifact"]).read_bytes()
    except OSError as error:
        raise HarnessError(f"{label}原始结果缺失或无法读取；保留证据，禁止重新抽取结论") from error
    digest = hashlib.sha256(raw).hexdigest()
    if receipt.get("artifact_digest", digest) != digest:
        raise HarnessError(f"{label}原始结果已改变；保留证据，禁止重新抽取结论")
    if "artifact_digest" not in receipt and bind is not None:
        receipt["artifact_digest"] = digest
        bind()
    return raw, digest


def read_bound_result(receipt, label, *, bind=None):
    raw, digest = read_bound_bytes(receipt, label, bind=bind)
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise HarnessError(f"{label}原始结果不是有效 JSON") from error
    return result, digest


class InvalidOutputRecovery:
    def format_repair_guidance(self, key, original):
        role, kind, _ = key.split(":", 2)
        receipts = self.run["commander_receipts"] if role == "commander" else self.run["review_receipts"]
        receipt = receipts.get(key)
        eligible = ((role == "reviewer" and review_approved(original)) or
                    role == "commander" and (kind == "dispatch" or kind == "replan" and original["action"] == "revise"))
        if role == "commander" and receipt:
            correction = receipt["request"]["packet"].get("format_correction")
            eligible = eligible and (correction is None or bool(correction.get("source")))
        if eligible and receipt and key not in self.run["invalid_repairs"]:
            return (f"；若仅需修正可变字段格式，可显式 resume {self.run['id']} "
                    f"--repair-invalid {receipt['dispatch_id']}（此角色/调用类型/任务在整个 run 仅一次）")
        return "；请保留原始证据并 abandon"

    def active_invalid_repair(self, key):
        repair = self.run["invalid_repairs"].get(key)
        if not repair or repair["status"] == "invalidated":
            return None
        receipts = self.run["commander_receipts"] if key.startswith("commander:") else self.run["review_receipts"]
        receipt = receipts.get(key)
        if repair["status"] == "pending" or receipt and receipt["dispatch_id"] == repair.get("corrected_dispatch_id"):
            return repair
        return None

    def invalid_repair_packet(self, key, packet):
        repair = self.active_invalid_repair(key)
        if not repair:
            return packet
        source = repair.get("original", repair["source"])
        self.validate_invalid_repair_source(key)
        correction = {"artifact": source["artifact"], "artifact_digest": source["artifact_digest"],
                      "source_dispatch_id": source["dispatch_id"], "error": repair["error"],
                      "instruction": "Read the original result and correct its reported contract error. Use this dispatch's IDs. All original goal, scope, acceptance and budget constraints remain binding."}
        if key.startswith("reviewer:"):
            correction.update(proposed_memory_keys=repair["proposed_keys"], instruction=(
                "Read the original approval. Preserve its verdict and every finding's order, severity, category, "
                "location, message and evidence exactly. Only repair duplicate/invalid finding IDs, reformat summary, "
                "deduplicate memory keys and remove keys absent from proposed_memory_keys. Preserve every originally "
                "approved valid key; add no approvals. Use this dispatch's IDs. This is metadata correction, not a new review."))
        if repair.get("mode") == "format":
            correction["failed_result"] = {field: repair["source"][field] for field in
                                           ("artifact", "artifact_digest", "dispatch_id")}
            if key.startswith("commander:"):
                correction["instruction"] = (
                    "Read the original result and failed_result. Correct only the reported text formatting. "
                    "Preserve selected_task and action (omitted action means implement) for task selection, or action, new_tasks and updates exactly for a revision. "
                    "Use this dispatch's IDs. All original goal, scope, acceptance and budget constraints remain binding.")
        return {**packet, "invalid_output_correction": correction}

    def validate_invalid_repair_source(self, key, candidate=None):
        repair = self.active_invalid_repair(key)
        if repair:
            result, _ = read_bound_result(repair["source"], "显式修正依据的")
            candidate = self.snapshot() if candidate is None else candidate
            if snapshot_digest(candidate) != repair["source"]["snapshot_digest"]:
                raise HarnessError("显式修正依据的代码快照已变化；不能修正旧候选的结果")
            if "original" in repair:
                result = self.read_format_source(key, repair["original"], candidate)
            return result

    def read_format_source(self, key, source, candidate=None):
        """Verify previously bound evidence; never invent a digest for old guards."""
        if not source.get("artifact_digest"):
            raise HarnessError("旧格式修正缺少原始结果摘要，无法安全追加修正；请保留证据并 abandon")
        result, _ = read_bound_result(source, "格式修正依据的")
        entry = next((d for d in self.run["dispatches"] if d["id"] == source["dispatch_id"]), None)
        if entry is None or key != f"{entry['role']}:{entry['kind']}:{entry['task_id']}":
            raise HarnessError("格式修正原始依据与派发身份不符")
        candidate = self.snapshot() if candidate is None else candidate
        if snapshot_digest(candidate) != source["snapshot_digest"]:
            raise HarnessError("格式修正原始依据的代码快照已变化")
        return result

    def commander_format_source(self, key, packet, candidate=None):
        correction = packet.get("format_correction")
        if correction is None:
            return None
        source = correction.get("source")
        if not source:
            raise HarnessError("旧指挥格式修正缺少原始结果摘要，无法安全恢复；请保留证据并 abandon")
        return self.read_format_source(key, source, candidate)

    def prepare_invalid_repair(self, dispatch_id):
        """Authorize one correction per role/kind/task without discarding evidence."""
        matches = [(key, receipt, receipts) for receipts in
                   (self.run["commander_receipts"], self.run["review_receipts"])
                   for key, receipt in receipts.items() if receipt["dispatch_id"] == dispatch_id]
        if len(matches) != 1:
            raise HarnessError("--repair-invalid 必须匹配当前未消费的非法结果 dispatch ID")
        key, receipt, receipts = matches[0]
        if key in self.run["invalid_repairs"]:
            raise HarnessError("该角色/调用类型/任务的显式输出修正额度已使用；不能再次抽取结果")
        result, digest = read_bound_result(receipt, "待修正的", bind=self.save)
        if snapshot_digest(self.snapshot()) != receipt["snapshot_digest"]:
            raise HarnessError("待修正结果的代码快照已变化")
        entry = next(d for d in self.run["dispatches"] if d["id"] == dispatch_id)
        if key != f"{entry['role']}:{entry['kind']}:{entry['task_id']}":
            raise HarnessError("待修正结果与派发身份不符")
        proposed_keys = []
        original = None
        mode = "semantic"
        if entry["role"] == "commander":
            request = receipt["request"]
            if snapshot_digest(request) != receipt["request_digest"]:
                raise HarnessError("指挥原始请求已改变")
            validate_structure(result, envelope(request["schema"], entry["task_id"], dispatch_id))
            if entry["kind"] not in ("plan", "replan", "dispatch") or entry["kind"] == "replan" and result["action"] != "revise":
                raise HarnessError("显式输出修正不能丢弃指挥 stop")
            if entry["kind"] == "replan" and receipt["request_id"] != self.run["pending_replan"]["request_id"]:
                raise HarnessError("指挥结果不属于当前再规划请求")
            payload = {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
            try:
                if entry["kind"] == "plan":
                    self.validate_tasks(payload)
                elif entry["kind"] == "replan":
                    tasks = apply_revision(self.run["tasks"], payload, self.run["accepted"],
                                           self.config.data["runtime"]["max_tasks"],
                                           additional_allowed_paths=self.run["approved_scope"])
                    self.validate_tasks({"tasks": tasks})
                validate(result, envelope(request["schema"], entry["task_id"], dispatch_id))
            except RepairableFormatError as error:
                if entry["kind"] not in ("dispatch", "replan") or not self.run["format_repairs"].get(key):
                    raise HarnessError("格式修正自动额度尚未耗尽，或原计划包含不可变格式错误") from error
                if entry["kind"] == "replan":
                    tasks = apply_revision(self.run["tasks"], {**payload, "reason": "Format correction"},
                                           self.run["accepted"], self.config.data["runtime"]["max_tasks"],
                                           additional_allowed_paths=self.run["approved_scope"])
                    self.validate_tasks({"tasks": tasks})
                correction = request["packet"].get("format_correction")
                if correction is not None and not correction.get("source"):
                    raise HarnessError("旧指挥格式修正缺少原始结果摘要，无法安全追加修正；请保留证据并 abandon") from error
                original = deepcopy(correction["source"] if correction else receipt)
                previous = self.read_format_source(key, original)
                validate_structure(previous, envelope(request["schema"], entry["task_id"], original["dispatch_id"]))
                validate_commander_format_correction(entry["kind"], previous, result)
                mode, validation_error = "format", str(error)
            except HarnessError as error:
                if (entry["kind"] == "dispatch" or key in self.run["pending_format_corrections"]
                        or "format_correction" in request["packet"]):
                    raise HarnessError("格式修正结果存在非格式错误；不能丢弃原指挥决定") from error
                validation_error = str(error)
            else:
                raise HarnessError("原指挥结果合法；不能丢弃结果或绕过 hook 门禁")
        else:
            validate_structure(result, envelope(REVIEW, entry["task_id"], dispatch_id))
            if not review_approved(result):
                raise HarnessError("拒绝或包含 blocking finding 的审阅不能通过 --repair-invalid 重抽")
            if entry["kind"] == "review":
                pending = self.run.get("pending_task", {})
                if pending.get("task_id") != entry["task_id"]:
                    raise HarnessError("待修正审阅不属于当前任务")
                proposed_keys = [p["key"] for p in pending["proposals"]]
            elif entry["kind"] != "final_review":
                raise HarnessError("不支持此类审阅修正")
            validate_immutable_findings(result["findings"])
            guard = self.run["review_corrections"].get(key)
            if guard:
                if guard.get("failed") or guard.get("unrepairable"):
                    raise HarnessError("原审阅修正已违反结论或包含不可修正字段；不能追加修正")
                original = {**deepcopy(guard), "dispatch_id": Path(guard["artifact"]).parent.name}
                previous = self.read_format_source(key, original)
                validate_structure(previous, envelope(REVIEW, entry["task_id"], original["dispatch_id"]))
                if not review_approved(previous):
                    raise HarnessError("原审阅拒绝或包含 blocking finding；不能追加修正")
                validate_immutable_findings(previous["findings"])
                validate_review_semantics(previous, proposed_keys)
                validate_review_semantics(result, proposed_keys)
                # Recheck even when a crash prevented failed/exhausted from being saved.
                validate_review_correction(previous, result)
                if not self.run["format_repairs"].get(key):
                    raise HarnessError("格式修正自动额度尚未耗尽；请普通 resume")
                try:
                    validate(result, envelope(REVIEW, entry["task_id"], dispatch_id))
                except RepairableFormatError as error:
                    mode, validation_error = "format", str(error)
                else:
                    raise HarnessError("原审阅修正结果合法；不能丢弃结果或绕过 hook 门禁")
            else:
                if key in self.run["pending_format_corrections"]:
                    raise HarnessError("格式修正缺少原审阅守卫；不能丢弃原始依据")
                try:
                    validate_review_semantics(result, proposed_keys)
                except HarnessError as error:
                    validation_error = str(error)
                else:
                    raise HarnessError("审阅没有可修正的批准元数据错误；不能绕过格式额度或 hook 门禁")
        source = {**deepcopy(receipt), "artifact_digest": digest}
        self.run["invalid_repairs"][key] = {"status": "pending", "source": source,
            "error": validation_error, "proposed_keys": proposed_keys, "mode": mode,
            **({"original": original} if original else {})}
        receipts.pop(key)
        # Original evidence and replacement intent are durable in the same write.
        self.save()
        self.event("invalid_output_repair_requested", dispatch_id=dispatch_id, key=key,
                   error=validation_error, artifact=source["artifact"], artifact_digest=digest)
