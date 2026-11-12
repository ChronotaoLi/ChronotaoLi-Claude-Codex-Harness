"""Validate durable returned results and post-agent gates without redrawing conclusions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import git
from .contracts import envelope, judge_review, review_schema, ScopeExtensionRequired, validate
from .recovery import read_bound_bytes, read_bound_result, snapshot_digest
from .services import RuntimeService
from .util import HarnessError, read_json, strict_json_loads

# Error text carried into the next request's previous_invalid and events; the
# dispatch entry keeps the full text.
ERROR_EXCERPT_CHARS = 2000


class InvalidOutput(HarnessError):
    """A returned output failed its contract and no veto can be read from it.

    Raised only after its dispatch entry is recorded invalid and its receipt is
    consumed in one save; the dispatcher then redoes it once or stops.
    """

    def __init__(self, entry, error):
        super().__init__(str(error))
        self.entry = entry


class ScopeStop(HarnessError):
    """A revision whose only defect is new scope: recorded invalid, consumed, never redrawn."""

    def __init__(self, entry, error):
        super().__init__(str(error))
        self.entry = entry


class Decision:
    """A judged decide result: its payload, a revision's tasks, an ask's veto error,
    and whether the candidate or HEAD changed during the call (recorded with an ask)."""

    def __init__(self, payload, proposed=None, veto=None, candidate_changed=False):
        self.payload, self.proposed, self.veto = payload, proposed, veto
        self.candidate_changed = candidate_changed


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
                    or capture.get("source") != ("response.json" if capture["provider"] == "codex" else "process.stdout")):
                raise ValueError("invalid terminal capture")
            for name, field in ((capture["source"], "source_digest"),
                                ("process.outcome.json", "outcome_digest"),
                                ("returned-result.json", "result_digest")):
                path = job / name
                if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != capture[field]:
                    raise ValueError("terminal evidence changed")
            original = read_json(job / "process.outcome.json")
            if (original != outcome or original.get("started") is not True
                    or type(original.get("exit_code")) is not int or original["exit_code"] != 0
                    or original.get("termination_reason") != "normal"
                    or original.get("errors") or original.get("cleanup_errors")):
                raise ValueError("terminal process did not complete normally")
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

    def discard_unreturned(self, key):
        """Drop a commander or review intent that never returned; a returned result never is.

        The spent dispatch budget and the dispatch entry stay as they are.
        """
        receipts = self.context.run["commander_receipts" if key.startswith("commander:") else "review_receipts"]
        receipt = receipts.get(key)
        if receipt is None:
            return
        if "artifact_digest" in receipt or Path(receipt["artifact"]).exists():
            raise HarnessError("已有返回、尚未处理的结果；先普通 resume，不能丢弃")
        self.context.ensure_exited()
        self.validate_terminal_capture(receipt)
        receipts.pop(key)
        self.context.save()

    def record_invalid(self, key, error, *, artifact=None, out_of_scope_paths=None):
        """Record an output without a readable veto and consume its receipt in one save."""
        field = "commander_receipts" if key.startswith("commander:") else "review_receipts"
        receipt = self.context.run[field][key]
        entry = next(d for d in self.context.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        if artifact is None:
            source = {"artifact": receipt["artifact"], "artifact_digest": receipt["artifact_digest"]}
        else:
            # An undecodable provider file: bind the bytes the judgment read.
            source = {"artifact": str(artifact), "artifact_digest": hashlib.sha256(Path(artifact).read_bytes()).hexdigest()}
        entry.update(status="invalid", error=str(error), invalid_output=source)
        if out_of_scope_paths is not None:
            # Never auto-redone: the redo rule treats it like a redo already spent.
            entry["out_of_scope_paths"] = list(out_of_scope_paths)
        self.context.run["last_runtime_error"] = {"role": entry["role"], "kind": entry["kind"],
            "task_id": entry["task_id"], "error": str(error), "artifact": str(Path(receipt["artifact"]).parent)}
        self.context.run[field].pop(key)
        self.context.save()
        self.observe_invalid(entry)
        return InvalidOutput(entry, error)

    def observe_invalid(self, entry):
        """Observation only; never a recovery input."""
        try:
            self.context.event("output_invalid", task_id=entry["task_id"], role=entry["role"], kind=entry["kind"],
                               dispatch_id=entry["id"], status=entry["status"], retry_of=entry.get("retry_of"),
                               error=str(entry.get("error", ""))[:ERROR_EXCERPT_CHARS])
        except Exception:
            pass

    def finish_commander_receipt(self, key):
        """Judge the original decide result, then run its hook gate when valid.

        ``ask`` is a veto read after the common preconditions only (exit
        confirmed, bytes bound, strict object, envelope identity): any other
        defect is recorded with the question, never redone. Recognising it does
        not depend on the candidate gate either: a changed candidate or HEAD is
        recorded with the question, which grants that candidate nothing. Every
        other decision needs the bound snapshot and HEAD before it is judged.
        """
        receipt = self.context.run["commander_receipts"][key]
        self.validate_terminal_capture(receipt)
        entry = next(d for d in self.context.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        if key != f"{entry['role']}:{entry['kind']}:{entry['task_id']}" or entry["role"] != "commander":
            raise HarnessError("指挥结果的派发身份与接收记录不符")
        if snapshot_digest(receipt["request"]) != receipt["request_digest"]:
            raise HarnessError("指挥原始请求已改变；保留证据，禁止重新抽取结论")
        artifact = Path(receipt["artifact"])
        raw, _ = read_bound_bytes(receipt, "指挥", bind=self.context.save)
        try:
            result = strict_json_loads(raw)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise HarnessError("指挥原始结果不是有效 JSON；保留证据，禁止重新抽取结论") from error
        self.context.ensure_exited()
        contract = envelope(receipt["request"]["schema"], entry["task_id"], entry["id"])
        packet = receipt["request"]["packet"]
        asked = (isinstance(result, dict) and result.get("action") == "ask"
                 and result.get("task_id") == entry["task_id"] and result.get("dispatch_id") == entry["id"])
        if not asked and self.candidate_changed(receipt):
            raise HarnessError("指挥或 hook 修改了代码/HEAD；原指挥结果不能用于当前快照")
        if asked:
            payload = {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
            try:
                validate(result, contract)
                self.services.planning.check_decision(payload, packet)
            except HarnessError as error:
                # The landing save records invalid_asked with this error; no hook runs.
                return Decision(payload, veto=str(error), candidate_changed=self.candidate_changed(receipt))
        else:
            payload = {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")} if isinstance(result, dict) else None
            try:
                validate(result, contract)
                action, proposed = self.services.planning.check_decision(payload, packet)
            except ScopeExtensionRequired as error:
                message = (f"指挥修订越出已批准的文件范围：{json.dumps(error.patterns, ensure_ascii=False)}；"
                           "核对后可用 resume RUN --allow-scope 'PATTERN' 授权；授权与否，普通 resume 都发起新的指挥决定；"
                           f"原件 {artifact}")
                invalid = self.record_invalid(key, message, out_of_scope_paths=error.patterns)
                raise ScopeStop(invalid.entry, message) from error
            except HarnessError as error:
                raise self.record_invalid(key, error) from error
        if not receipt.get("hook_completed"):
            receipt["hook_attempts"] = receipt.get("hook_attempts", 0) + 1
            self.context.save()
            self.context.emit_hook("post_agent", {**entry, "status": "completed", "result": result},
                            artifact.parent / f"post-agent-{receipt['hook_attempts']}")
            if not asked and self.candidate_changed(receipt):
                raise HarnessError("commander hook 修改了代码/HEAD；已停止，保留现场")
            receipt["hook_completed"] = True
        entry["status"] = "completed"
        entry.pop("error", None)
        self.context.save()
        if asked:
            # Read after the hook, before the landing save that records it.
            return Decision(payload, candidate_changed=self.candidate_changed(receipt))
        return Decision(payload, proposed)

    def returned_ask(self, key):
        """Whether the returned, unlanded original names an ask for this very call.

        A read-only look at envelope identity and action only; landing it runs
        every common precondition again (exit, binding, strict decoding).
        """
        receipt = self.context.run["commander_receipts"].get(key)
        if receipt is None:
            return False
        try:
            raw = Path(receipt["artifact"]).read_bytes()
            result = strict_json_loads(raw)
        except (OSError, ValueError, UnicodeError, RecursionError):
            return False
        return (isinstance(result, dict) and result.get("action") == "ask"
                and result.get("task_id") == key.rsplit(":", 1)[1] and result.get("dispatch_id") == receipt["dispatch_id"])

    def candidate_changed(self, receipt):
        """Whether HEAD or the candidate snapshot differs from the one the call was bound to."""
        return (git.head(self.context.worktree, binding=self.context.run["worktree_binding"]) != self.context.run["expected_head"]
                or snapshot_digest(self.context.snapshot()) != receipt["snapshot_digest"])

    def finish_review_receipt(self, key, schema):
        """Judge a returned review without treating unvalidated JSON as approval."""
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
        self.context.ensure_exited()
        judgment = judge_review(result, envelope(schema, entry["task_id"], entry["id"]))
        if judgment.state == "invalid":
            raise self.record_invalid(key, judgment.error) from judgment.error
        rejection = InvalidReviewRejection(result, receipt, judgment.error) if judgment.state == "veto" else None
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
        entry["status"] = "invalid_rejected" if rejection else "completed"
        if rejection:
            entry["error"] = str(rejection)
        else:
            entry.pop("error", None)
        self.context.save()
        if rejection:
            self.observe_invalid(entry)
            self.context.ensure_exited()
            raise rejection
        return {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}

    def dispatch_review(self, kind, task_id, packet, task=None, *, required):
        """Dispatch one review whose runtime-keyed dispositions follow its saved mapping."""
        try:
            review = self.services.dispatch.dispatch("reviewer", kind, task_id, review_schema(required), packet, task)
        except InvalidReviewRejection as rejection:
            return rejection.review, rejection.diagnostics
        return review, {}
