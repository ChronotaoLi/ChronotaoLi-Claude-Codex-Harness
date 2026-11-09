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
from .util import HarnessError, read_json


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
    @staticmethod
    def terminal_capture_required(outcome):
        return (outcome and not outcome.get("evidence_complete")
                and outcome.get("started") is True
                and type(outcome.get("exit_code")) is int
                and (outcome.get("termination_reason") == "normal" or outcome["exit_code"] == 0))

    def validate_pending_terminal_captures(self):
        for field in ("review_receipts", "commander_receipts"):
            for receipt in self.context.run[field].values():
                entry = next(d for d in self.context.run["dispatches"] if d["id"] == receipt["dispatch_id"])
                marker = self.context.directory / "dispatches" / entry["id"] / "terminal-capture.json"
                outcome = entry.get("process_outcome")
                if (marker.exists() or receipt.get("terminal_capture_digest")
                        or outcome):
                    self.validate_terminal_capture(receipt)
                    if ((marker.exists() or receipt.get("terminal_capture_digest") or self.terminal_capture_required(outcome))
                            and snapshot_digest(self.context.snapshot()) != receipt["snapshot_digest"]):
                        raise HarnessError("终端结果的代码快照已变化；保留证据，禁止重新抽取结论")

    def validate_terminal_capture(self, receipt):
        """Keep failed cleanup distinct from the captured terminal decision.

        Exit confirmation permits revalidation; it does not erase original
        timeout, nonzero exit, protocol or persistence failures.
        """
        entry = next(d for d in self.context.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        job = self.context.directory / "dispatches" / entry["id"]
        marker = job / "terminal-capture.json"
        outcome = entry.get("process_outcome")
        if (not marker.exists() and outcome and outcome.get("started") is True
                and (outcome.get("termination_reason") != "normal" or outcome.get("exit_code") not in (None, 0))):
            from .adapters import CLIBackend
            CLIBackend(self.context.config, self.context.directory).preserve_terminal(
                job, self.context.config.role(entry["role"], self.context.commander)["cli"], outcome, terminal_only=True)
        if not marker.exists():
            if receipt.get("terminal_capture_digest") or self.terminal_capture_required(outcome):
                raise HarnessError("终端结果的进程证据不完整；保留证据，禁止重新抽取结论")
            return
        self.context.ensure_exited()
        try:
            if marker.is_symlink() or not marker.is_file() or marker.parent.resolve() != marker.parent:
                raise ValueError("unsafe terminal capture path")
            raw = marker.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if receipt.get("terminal_capture_digest", digest) != digest:
                raise ValueError("terminal capture changed")
            capture = json.loads(raw)
            if (not isinstance(capture, dict) or capture.get("version") != 1
                    or capture.get("provider") not in ("claude", "codex")
                    or capture["provider"] != self.context.config.role(entry["role"], self.context.commander)["cli"]
                    or capture.get("source") not in (("response.json",) if capture["provider"] == "codex"
                                                     else ("process.stdout", "stream-terminal.json", "stream-received-terminal.json"))):
                raise ValueError("invalid terminal capture")
            for name, field in ((capture["source"], "source_digest"),
                                ("process.outcome.json", "outcome_digest"),
                                ("returned-result.json", "result_digest")):
                path = job / name
                if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != capture[field]:
                    raise ValueError("terminal evidence changed")
            support = capture.get("support_digests")
            expected_support = ({"capture-manifest.json", "stream-capture.json"}
                                if capture["source"] == "stream-terminal.json" else
                                {"capture-manifest.json"} if capture["provider"] == "claude" else set())
            if not isinstance(support, dict) or set(support) != expected_support:
                raise ValueError("missing terminal protocol evidence")
            for name, expected in support.items():
                path = job / name
                if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError("terminal protocol evidence changed")
            original = read_json(job / "process.outcome.json")
            if (original != outcome or original.get("started") is not True
                    or type(original.get("exit_code")) is not int or original["exit_code"] != 0
                    or original.get("termination_reason") != "normal"
                    or original.get("errors") or original.get("cleanup_errors")):
                raise ValueError("terminal process did not complete normally")
            if capture["source"] == "stream-terminal.json":
                protocol = read_json(job / "stream-capture.json")
                if protocol.get("protocol_complete") is not True or protocol.get("terminal_count") != 1 or protocol.get("exit_code") != 0:
                    raise ValueError("terminal stream incomplete")
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise HarnessError("终端结果证据缺失、改变或进程未正常完成；保留证据，禁止重新抽取结论") from error
        if "terminal_capture_digest" not in receipt:
            receipt["terminal_capture_digest"] = digest
            self.context.save()

    def record_receipt_error(self, receipt, error):
        if receipt is None:
            return
        entry = next(d for d in self.context.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        entry.update(status="failed", error=str(error))
        self.context.run["last_runtime_error"] = {"role": entry["role"], "kind": entry["kind"],
            "task_id": entry["task_id"], "error": str(error), "artifact": str(Path(receipt["artifact"]).parent)}
        self.context.save()

    def finish_commander_receipt(self, key):
        """Replay the original request's result through validation and hook gates."""
        self.services.recovery.validate_invalid_repair_source(key)
        receipt = self.context.run["commander_receipts"][key]
        self.validate_terminal_capture(receipt)
        entry = next(d for d in self.context.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        if key != f"{entry['role']}:{entry['kind']}:{entry['task_id']}" or entry["role"] != "commander":
            raise HarnessError("指挥结果的派发身份与接收记录不符")
        if snapshot_digest(receipt["request"]) != receipt["request_digest"]:
            raise HarnessError("指挥原始请求已改变；保留证据，禁止重新抽取结论")
        if entry["kind"] == "replan" and receipt["request_id"] != self.context.run["pending_replan"]["request_id"]:
            raise HarnessError("指挥结果不属于当前再规划请求")
        artifact = Path(receipt["artifact"])
        raw, _ = read_bound_bytes(receipt, "指挥", bind=self.context.save)
        if (git.head(self.context.worktree, binding=self.context.run["worktree_binding"]) != self.context.run["expected_head"]
                or snapshot_digest(self.context.snapshot()) != receipt["snapshot_digest"]):
            raise HarnessError("指挥或 hook 修改了代码/HEAD；原指挥结果不能用于当前快照")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise HarnessError("指挥原始结果不是有效 JSON；保留证据，禁止重新抽取结论") from error
        contract = envelope(receipt["request"]["schema"], entry["task_id"], entry["id"])
        validate_structure(result, contract)
        previous = self.services.recovery.commander_format_source(key, receipt["request"]["packet"])
        repair = self.services.recovery.active_invalid_repair(key)
        if repair and repair.get("mode") == "format":
            previous = self.services.recovery.validate_invalid_repair_source(key)
        if previous is not None:
            validate_commander_format_correction(entry["kind"], previous, result)
        payload = {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
        if entry["kind"] == "plan":
            self.services.planning.validate_tasks(payload)
        elif entry["kind"] == "replan":
            tasks = apply_revision(self.context.run["tasks"], payload, self.context.run["accepted"],
                                   self.context.config.data["runtime"]["max_tasks"],
                                   additional_allowed_paths=self.context.run.get("approved_scope", []))
            self.services.planning.validate_tasks({"tasks": tasks})
        validate(result, contract)
        if not receipt.get("hook_completed"):
            receipt["hook_attempts"] = receipt.get("hook_attempts", 0) + 1
            self.context.save()
            self.context.emit_hook("post_agent", {**entry, "status": "completed", "result": result},
                            artifact.parent / f"post-agent-{receipt['hook_attempts']}")
            if (git.head(self.context.worktree, binding=self.context.run["worktree_binding"]) != self.context.run["expected_head"]
                    or snapshot_digest(self.context.snapshot()) != receipt["snapshot_digest"]):
                raise HarnessError("commander hook 修改了代码/HEAD；已停止，保留现场")
            receipt["hook_completed"] = True
        self.services.recovery.validate_invalid_repair_source(key)
        self.services.recovery.commander_format_source(key, receipt["request"]["packet"])
        entry["status"] = "completed"
        entry.pop("error", None)
        self.context.save()
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
        receipt = self.context.run["review_receipts"][key]
        self.validate_terminal_capture(receipt)
        entry = next(d for d in self.context.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        artifact = Path(receipt["artifact"])
        result, digest = read_bound_result(receipt, "审阅", bind=self.context.save)
        if "artifact_digest" not in receipt:
            receipt["artifact_digest"] = digest
            self.context.save()
        if digest != receipt["artifact_digest"]:
            raise HarnessError("审阅原始结果已改变；保留证据，禁止重新抽取结论")
        if (git.head(self.context.worktree, binding=self.context.run["worktree_binding"]) != self.context.run["expected_head"]
                or snapshot_digest(self.context.snapshot()) != receipt["snapshot_digest"]):
            raise HarnessError("审阅或 hook 修改了代码/HEAD；原审阅结果不能用于当前快照")
        contract = envelope(schema, entry["task_id"], entry["id"])
        validate_structure(result, contract)
        repair = self.services.recovery.active_invalid_repair(key)
        if repair:
            original = self.services.recovery.validate_invalid_repair_source(key)
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
                self.context.run["review_corrections"][key]["failed"] = True
                self.context.save()
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
                    if not self.context.run["format_repairs"].get(key):
                        raise
                    # A prior candidate may have spent the correction budget.
                    # Keep this candidate's original rejection and rework it.
                    rejection = InvalidReviewRejection(result, receipt, format_error)
        if not receipt.get("hook_completed"):
            receipt["hook_attempts"] = receipt.get("hook_attempts", 0) + 1
            self.context.save()
            logs = artifact.parent / f"post-agent-{receipt['hook_attempts']}"
            self.context.emit_hook("post_agent", {**entry, "status": "invalid_rejected" if rejection else "completed",
                                           "result": result,
                                           **({"validation_error": str(rejection)} if rejection else {})}, logs)
            if (git.head(self.context.worktree, binding=self.context.run["worktree_binding"]) != self.context.run["expected_head"]
                    or snapshot_digest(self.context.snapshot()) != receipt["snapshot_digest"]):
                raise HarnessError("reviewer hook 修改了代码；已停止，保留现场")
            receipt["hook_completed"] = True
        self.services.recovery.validate_invalid_repair_source(key)
        entry["status"] = "invalid_rejected" if rejection else "completed"
        if rejection:
            entry["error"] = str(rejection)
        else:
            entry.pop("error", None)
        self.context.save()
        if rejection:
            self.context.ensure_exited()
            raise rejection
        return {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}

    def dispatch_review(self, kind, task_id, packet, task=None):
        try:
            review = self.services.dispatch.dispatch("reviewer", kind, task_id, REVIEW, packet, task)
        except InvalidReviewRejection as rejection:
            return rejection.review, rejection.diagnostics
        validate_review(review, [p["key"] for p in packet.get("proposed_memories", [])])
        return review, {}
