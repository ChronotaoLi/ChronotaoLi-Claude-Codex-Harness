"""Validate durable returned results and post-agent gates without redrawing conclusions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import git
from .context import compact_review
from .contracts import (
    apply_revision, envelope, RepairableFormatError, REVIEW, review_approved, validate,
    validate_approval_repair, validate_commander_format_correction, validate_immutable_findings,
    validate_review, validate_review_correction, validate_review_semantics, validate_structure,
)
from .recovery import read_bound_bytes, read_bound_result, snapshot_digest
from .services import RuntimeService
from .util import HarnessError


class InvalidReviewRejection(HarnessError):
    """An attributable rejection is evidence, never a validated approval."""

    def __init__(self, review, receipt, error):
        super().__init__(str(error))
        self.review = {k: v for k, v in review.items() if k not in ("task_id", "dispatch_id")}
        self.diagnostics = {"status": "invalid_rejected", "validation_error": str(error),
                            "returned_artifact": receipt["artifact"],
                            "artifact_digest": receipt["artifact_digest"],
                            "dispatch_id": receipt["dispatch_id"]}



class ResultReceipts(RuntimeService):
    def record_receipt_error(self, receipt, error):
        runtime = self.runtime
        if receipt is None:
            return
        entry = next(d for d in runtime.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        entry.update(status="failed", error=str(error))
        runtime.run["last_runtime_error"] = {"role": entry["role"], "kind": entry["kind"],
            "task_id": entry["task_id"], "error": str(error), "artifact": str(Path(receipt["artifact"]).parent)}
        runtime.save()

    def finish_commander_receipt(self, key):
        """Replay the original request's result through validation and hook gates."""
        runtime = self.runtime
        runtime.validate_invalid_repair_source(key)
        receipt = runtime.run["commander_receipts"][key]
        entry = next(d for d in runtime.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        if key != f"{entry['role']}:{entry['kind']}:{entry['task_id']}" or entry["role"] != "commander":
            raise HarnessError("指挥结果的派发身份与接收记录不符")
        if snapshot_digest(receipt["request"]) != receipt["request_digest"]:
            raise HarnessError("指挥原始请求已改变；保留证据，禁止重新抽取结论")
        if entry["kind"] == "replan" and receipt["request_id"] != runtime.run["pending_replan"]["request_id"]:
            raise HarnessError("指挥结果不属于当前再规划请求")
        artifact = Path(receipt["artifact"])
        raw, _ = read_bound_bytes(receipt, "指挥", bind=runtime.save)
        if (git.head(runtime.worktree, binding=runtime.run["worktree_binding"]) != runtime.run["expected_head"]
                or snapshot_digest(runtime.snapshot()) != receipt["snapshot_digest"]):
            raise HarnessError("指挥或 hook 修改了代码/HEAD；原指挥结果不能用于当前快照")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise HarnessError("指挥原始结果不是有效 JSON；保留证据，禁止重新抽取结论") from error
        contract = envelope(receipt["request"]["schema"], entry["task_id"], entry["id"])
        validate_structure(result, contract)
        previous = runtime.commander_format_source(key, receipt["request"]["packet"])
        repair = runtime.active_invalid_repair(key)
        if repair and repair.get("mode") == "format":
            previous = runtime.validate_invalid_repair_source(key)
        if previous is not None:
            validate_commander_format_correction(entry["kind"], previous, result)
        payload = {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
        if entry["kind"] == "plan":
            runtime.validate_tasks(payload)
        elif entry["kind"] == "replan":
            tasks = apply_revision(runtime.run["tasks"], payload, runtime.run["accepted"],
                                   runtime.config.data["runtime"]["max_tasks"],
                                   additional_allowed_paths=runtime.run.get("approved_scope", []))
            runtime.validate_tasks({"tasks": tasks})
        validate(result, contract)
        if not receipt.get("hook_completed"):
            receipt["hook_attempts"] = receipt.get("hook_attempts", 0) + 1
            runtime.save()
            runtime.emit_hook("post_agent", {**entry, "status": "completed", "result": result},
                            artifact.parent / f"post-agent-{receipt['hook_attempts']}")
            if (git.head(runtime.worktree, binding=runtime.run["worktree_binding"]) != runtime.run["expected_head"]
                    or snapshot_digest(runtime.snapshot()) != receipt["snapshot_digest"]):
                raise HarnessError("commander hook 修改了代码/HEAD；已停止，保留现场")
            receipt["hook_completed"] = True
        runtime.validate_invalid_repair_source(key)
        runtime.commander_format_source(key, receipt["request"]["packet"])
        entry["status"] = "completed"
        entry.pop("error", None)
        runtime.save()
        return payload

    @staticmethod
    def read_review_correction(guard):
        artifact = Path(guard["artifact"])
        try:
            raw = artifact.read_bytes()
        except OSError as error:
            raise HarnessError("审阅修正的原始结果缺失或无法读取；保留证据，禁止重新抽取结论") from error
        if hashlib.sha256(raw).hexdigest() != guard.get("artifact_digest"):
            raise HarnessError("审阅修正的原始结果已改变；保留证据，禁止重新抽取结论")
        return json.loads(raw)

    @staticmethod
    def review_correction(guard, previous_review):
        preserved = compact_review(previous_review, guard["artifact"])
        return {"error": guard["error"], "artifact": guard["artifact"],
                "preserve_review": {k: v for k, v in preserved.items() if k != "summary"},
                "instruction": "Correct the format using this dispatch's IDs. Read the original result at artifact when preserve_review contains excerpts. Preserve the original verdict, memory approvals and all findings in order, including their exact severity, category, location, message and evidence. Only the summary and invalid finding IDs may be reformatted; do not discard or downgrade findings."}

    def finish_review_receipt(self, key, schema, packet, previous_review=None):
        """Finish a returned review without treating unvalidated JSON as approval."""
        runtime = self.runtime
        receipt = runtime.run["review_receipts"][key]
        entry = next(d for d in runtime.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        artifact = Path(receipt["artifact"])
        result, digest = read_bound_result(receipt, "审阅", bind=runtime.save)
        if "artifact_digest" not in receipt:
            receipt["artifact_digest"] = digest
            runtime.save()
        if digest != receipt["artifact_digest"]:
            raise HarnessError("审阅原始结果已改变；保留证据，禁止重新抽取结论")
        if (git.head(runtime.worktree, binding=runtime.run["worktree_binding"]) != runtime.run["expected_head"]
                or snapshot_digest(runtime.snapshot()) != receipt["snapshot_digest"]):
            raise HarnessError("审阅或 hook 修改了代码/HEAD；原审阅结果不能用于当前快照")
        contract = envelope(schema, entry["task_id"], entry["id"])
        validate_structure(result, contract)
        repair = runtime.active_invalid_repair(key)
        if repair:
            original = runtime.validate_invalid_repair_source(key)
            validate_approval_repair(original, result, repair["proposed_keys"])
        rejection = None
        try:
            validate_review_semantics(result, [p["key"] for p in packet.get("proposed_memories", [])])
        except HarnessError as error:
            # Never salvage a correction that may have changed its original verdict,
            # or infer rejection merely from invalid approval metadata.
            if previous_review is not None or repair or review_approved(result):
                raise
            rejection = InvalidReviewRejection(result, receipt, error)
        if previous_review is not None:
            try:
                validate_review_correction(previous_review, result)
            except HarnessError:
                runtime.run["review_corrections"][key]["failed"] = True
                runtime.save()
                raise
        if rejection is None:
            try:
                validate(result, contract)
            except RepairableFormatError as format_error:
                if previous_review is not None or repair or review_approved(result):
                    raise
                # An original rejection with immutable overflow still requires
                # rework. Preserve its evidence instead of asking for an
                # impossible format correction or weakening approval gates.
                try:
                    validate_immutable_findings(result["findings"])
                except RepairableFormatError as error:
                    rejection = InvalidReviewRejection(result, receipt, error)
                else:
                    if not runtime.run["format_repairs"].get(key):
                        raise
                    # A prior candidate may have spent the correction budget.
                    # Keep this candidate's original rejection and rework it.
                    rejection = InvalidReviewRejection(result, receipt, format_error)
        if not receipt.get("hook_completed"):
            receipt["hook_attempts"] = receipt.get("hook_attempts", 0) + 1
            runtime.save()
            logs = artifact.parent / f"post-agent-{receipt['hook_attempts']}"
            runtime.emit_hook("post_agent", {**entry, "status": "invalid_rejected" if rejection else "completed",
                                           "result": result,
                                           **({"validation_error": str(rejection)} if rejection else {})}, logs)
            if (git.head(runtime.worktree, binding=runtime.run["worktree_binding"]) != runtime.run["expected_head"]
                    or snapshot_digest(runtime.snapshot()) != receipt["snapshot_digest"]):
                raise HarnessError("reviewer hook 修改了代码；已停止，保留现场")
            receipt["hook_completed"] = True
        runtime.validate_invalid_repair_source(key)
        entry["status"] = "invalid_rejected" if rejection else "completed"
        if rejection:
            entry["error"] = str(rejection)
        else:
            entry.pop("error", None)
        runtime.save()
        if rejection:
            runtime.ensure_exited()
            raise rejection
        return {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}

    def dispatch_review(self, kind, task_id, packet, task=None):
        runtime = self.runtime
        try:
            review = runtime.dispatch("reviewer", kind, task_id, REVIEW, packet, task)
        except InvalidReviewRejection as rejection:
            return rejection.review, rejection.diagnostics
        validate_review(review, [p["key"] for p in packet.get("proposed_memories", [])])
        return review, {}
