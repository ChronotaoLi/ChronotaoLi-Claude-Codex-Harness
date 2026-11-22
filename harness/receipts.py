"""Validate durable returned results without redrawing conclusions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import git, stops
from .contracts import envelope, judge_review, review_schema, validate
from .recovery import read_bound_bytes, read_bound_result, snapshot_digest
from .services import RuntimeService
from .state_files import StateFiles
from .util import EvidenceError, HarnessError, read_json, strict_json_loads

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


class RetainedProposal(HarnessError):
    """A judged decide original that cannot land now: its receipt is kept, nothing is recorded invalid.

    The stop is derived again by replaying the same original; no ledger field marks it.
    """


class ScopeHold(RetainedProposal):
    """Only the boundary is missing (scope_extension/initial_plan|revision)."""


class ProposalCapacityHold(RetainedProposal):
    """A judged original over the final review's capacity under current conditions (budget/proposal_capacity)."""


class SuppliedPlanHold(HarnessError):
    """A supplied first plan outside the boundary declared after start (scope_extension/supplied_plan)."""


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



def judged_decide(entry):
    """A decide original already judged valid: ``completed`` (its receipt, if any, still names it)."""
    return entry.get("role") == "commander" and entry.get("kind") == "decide" and entry.get("status") == "completed"


class ResultReceipts(RuntimeService):
    def artifact_files(self, receipt):
        dispatch_id = receipt["dispatch_id"]
        job = self.context.directory / "dispatches" / dispatch_id
        artifact = job / "returned-result.json"
        if receipt["artifact"] != str(artifact):
            raise EvidenceError("返回结果的回执路径与派发不符；保留证据",
                                stop=stops.make("evidence", "runtime_integrity",
                                                source={"dispatch_id": dispatch_id}))
        return self._dispatch_files(dispatch_id)

    def _dispatch_files(self, dispatch_id):
        return StateFiles(self.context.state).under("runs", self.context.run["id"], "dispatches", dispatch_id)

    @staticmethod
    def _present(files, name, source, *, message):
        outcome = files.probe((name,)).or_local_io()
        if outcome.state == "present":
            return True
        if outcome.state == "absent" and outcome.step.leaf:
            return False
        try:
            outcome.evidence(source)
        except EvidenceError as error:
            raise EvidenceError(f"{message}：{error}", stop=error.stop) from error

    def returned(self, receipt):
        """Check a saved intent for a returned result without writing evidence."""
        if "artifact_digest" in receipt:
            return True
        return self._present(self.artifact_files(receipt), "returned-result.json",
                             {"dispatch_id": receipt["dispatch_id"], "artifact": receipt["artifact"]},
                             message="返回结果证据缺失或改变")

    def _capture_present(self, dispatch_id):
        name = "terminal-capture.json"
        marker = self.context.directory / "dispatches" / dispatch_id / name
        return self._present(self._dispatch_files(dispatch_id), name,
                             {"dispatch_id": dispatch_id, "artifact": str(marker)},
                             message="终端结果证据缺失或改变")

    @staticmethod
    def terminal_capture_required(outcome):
        return (outcome and not outcome.get("evidence_complete")
                and outcome.get("started") is True
                and type(outcome.get("exit_code")) is int
                and (outcome.get("termination_reason") == "normal" or outcome["exit_code"] == 0))

    def has_terminal_capture(self, receipt):
        """Whether the receipt's result was, or must be, recovered from the provider terminal."""
        entry = next(d for d in self.context.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        return bool("terminal_capture_digest" in receipt
                    or self.terminal_capture_required(entry.get("process_outcome"))
                    or self._capture_present(entry["id"]))

    def validate_pending_terminal_captures(self):
        for field in ("review_receipts", "commander_receipts"):
            for receipt in self.context.run[field].values():
                entry = next(d for d in self.context.run["dispatches"] if d["id"] == receipt["dispatch_id"])
                if ("terminal_capture_digest" in receipt or "process_outcome" in entry
                        or self._capture_present(entry["id"])):
                    self.validate_terminal_capture(receipt)
                    if (self.has_terminal_capture(receipt)
                            and snapshot_digest(self.context.snapshot()) != receipt["snapshot_digest"]):
                        raise HarnessError("终端结果的代码快照已变化；保留证据，禁止重新抽取结论", stop=stops.make("workspace_mismatch", "candidate_changed"))

    def validate_terminal_capture(self, receipt):
        """Keep failed cleanup distinct from the captured terminal decision.

        Exit confirmation permits revalidation; it does not erase original
        timeout, nonzero exit, protocol or persistence failures.
        """
        entry = next(d for d in self.context.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        job = self.context.directory / "dispatches" / entry["id"]
        marker = job / "terminal-capture.json"
        outcome = entry.get("process_outcome")
        captured = self._capture_present(entry["id"])
        if (not captured and outcome and outcome.get("started") is True
                and (outcome.get("termination_reason") != "normal" or outcome.get("exit_code") not in (None, 0))):
            from .adapters import CLIBackend
            CLIBackend(self.context.config, self.context.state).preserve_terminal(
                job, self.context.config.role(entry["role"], self.context.commander)["cli"], outcome, terminal_only=True)
            captured = self._capture_present(entry["id"])
        if not captured:
            if receipt.get("terminal_capture_digest") or self.terminal_capture_required(outcome):
                raise HarnessError("终端结果的进程证据不完整；保留证据，禁止重新抽取结论",
                                   stop=stops.make("evidence", "process_incomplete", data={"exit_unconfirmed": False},
                                                   source={"dispatch_id": entry["id"]}))
            return
        self.context.ensure_exited()
        with stops.evidence_reads(source={"artifact": str(marker), "dispatch_id": entry["id"]},
                                  message="终端结果证据缺失或改变；保留证据，禁止重新抽取结论"):
            digest = self._verified_terminal(entry, job, marker, receipt, outcome)
        if "terminal_capture_digest" not in receipt:
            receipt["terminal_capture_digest"] = digest
            self.context.save()  # outside the evidence read: a failed save stays local I/O

    def _verified_terminal(self, entry, job, marker, receipt, outcome):
        """Read and check the terminal capture and its sources; returns the capture digest."""
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
                raise HarnessError("终端结果的进程未正常完成；保留证据，禁止重新抽取结论",
                                   stop=stops.make("evidence", "process_incomplete", data={"exit_unconfirmed": False},
                                                   source={"dispatch_id": entry["id"]}))
        except (TypeError, KeyError) as error:  # malformed capture content: a content-check failure
            raise ValueError(f"invalid terminal capture: {error!r}") from error
        return digest

    def record_receipt_error(self, receipt, error):
        if receipt is None:
            return
        entry = next(d for d in self.context.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        if judged_decide(entry):
            # A replay failed, not the original: the judgment stands (spec-6 2.3.4).
            entry["error"] = str(error)
        else:
            entry.update(status="failed", error=str(error))
        self.context.save()

    def discard_unreturned(self, key):
        """Drop a commander or review intent that never returned; a returned result never is.

        The spent dispatch budget and the dispatch entry stay as they are.
        """
        receipts = self.context.run["commander_receipts" if key.startswith("commander:") else "review_receipts"]
        receipt = receipts.get(key)
        if receipt is None:
            return
        if self.returned(receipt):
            raise HarnessError("已有返回、尚未处理的结果；先普通 resume，不能丢弃")
        self.context.ensure_exited()
        self.validate_terminal_capture(receipt)
        receipts.pop(key)
        self.context.save()

    def record_invalid(self, key, error, *, source=None):
        """Record an output without a readable veto and consume its receipt in one save.

        ``source``: an undecodable provider file with the digest of the bytes the judgment read
        (``UndecodableOutput``), never read again here; otherwise the receipt's returned result.
        """
        field = "commander_receipts" if key.startswith("commander:") else "review_receipts"
        receipt = self.context.run[field][key]
        entry = next(d for d in self.context.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        if source is None:
            source = {"artifact": receipt["artifact"], "artifact_digest": receipt["artifact_digest"]}
        entry.update(status="invalid", error=str(error), invalid_output=source)
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
        """Judge the original decide result.

        ``ask`` is a veto read after the common preconditions only (exit
        confirmed, bytes bound, strict object, envelope identity): any other
        defect is recorded with the question, never redone. Recognising it does
        not depend on the candidate gate either: a changed candidate or HEAD is
        recorded with the question, which grants that candidate nothing. Every
        other decision needs the bound snapshot and HEAD before it is judged.

        Four classes of checks (spec-6 2.3): evidence failures stop and keep the
        receipt; the original's contract depends on it and its frozen baseline
        only, so it can fail only on the first judgment (then invalid output); a
        proposal missing only scope is held; the final review's capacity is a
        running condition: invalid on the first judgment, a kept proposal on a
        replay. A judged original's entry is ``completed`` while its receipt stays.
        """
        receipt = self.context.run["commander_receipts"][key]
        self.validate_terminal_capture(receipt)
        entry = next(d for d in self.context.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        if key != f"{entry['role']}:{entry['kind']}:{entry['task_id']}" or entry["role"] != "commander":
            raise HarnessError("指挥结果的派发身份与接收记录不符")
        if snapshot_digest(receipt["request"]) != receipt["request_digest"]:
            raise EvidenceError("指挥原始请求已改变；保留证据，禁止重新抽取结论", stop=stops.make("evidence", "runtime_integrity", source={"dispatch_id": entry["id"]}))
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
            raise HarnessError("指挥期间或之后修改了代码/HEAD；原指挥结果不能用于当前快照", stop=stops.make("workspace_mismatch", "candidate_changed", source={"dispatch_id": entry["id"]}))
        if asked:
            payload = {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
            try:
                validate(result, contract)
                self.services.planning.check_decision(payload, packet)
            except EvidenceError:
                raise  # changed runtime evidence is never a verdict on the model's output
            except HarnessError as error:
                # The landing save records invalid_asked with this error.
                return Decision(payload, veto=str(error), candidate_changed=self.candidate_changed(receipt))
        else:
            payload = {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")} if isinstance(result, dict) else None
            judged = judged_decide(entry)  # read before this judgment changes anything
            planning = self.services.planning
            try:
                validate(result, contract)
                action, proposed, gaps = planning.check_decision(payload, packet)
            except EvidenceError:
                raise  # changed runtime evidence is never a verdict on the model's output
            except HarnessError as error:
                if judged:
                    raise HarnessError("已判定合格的指挥原件重新判定为不合格；保留回执与现场",
                                       stop=stops.make("runtime_failure", "invariant")) from error
                raise self.record_invalid(key, error) from error
            if action == "revise":
                original = proposed if not self.context.run["tasks"] else None
                if not judged:
                    # First judgment: an oversized plan is invalid output, before any hold.
                    report = planning.capacity_report(proposed, original)
                    if report["status"] == "required_overflow":
                        raise self.record_invalid(key, planning.capacity_error(report))
                if gaps:
                    if not judged or "error" in entry:
                        self.judged(entry)
                    raise planning.hold_stop(receipt, payload, gaps, proposed)
                if judged:
                    report = planning.capacity_report(proposed, original)
                    if report["status"] == "required_overflow":
                        raise planning.capacity_hold(receipt, report)
        entry["status"] = "completed"
        entry.pop("error", None)
        self.context.save()
        if asked:
            # Read before the landing save that records it.
            return Decision(payload, candidate_changed=self.candidate_changed(receipt))
        return Decision(payload, proposed)

    def judged(self, entry):
        """Save the judgment of a returned original (the hold keeps its receipt)."""
        entry["status"] = "completed"
        entry.pop("error", None)
        self.context.save()

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
        result, digest = read_bound_result(receipt, "审阅", bind=self.context.save)
        if "artifact_digest" not in receipt:
            receipt["artifact_digest"] = digest
            self.context.save()
        if digest != receipt["artifact_digest"]:
            raise EvidenceError("审阅原始结果已改变；保留证据，禁止重新抽取结论", stop=stops.make("evidence", "runtime_integrity", source={"dispatch_id": entry["id"], "artifact": receipt["artifact"]}))
        if (git.head(self.context.worktree, binding=self.context.run["worktree_binding"]) != self.context.run["expected_head"]
                or snapshot_digest(self.context.snapshot()) != receipt["snapshot_digest"]):
            raise HarnessError("审阅期间或之后修改了代码/HEAD；原审阅结果不能用于当前快照", stop=stops.make("workspace_mismatch", "candidate_changed", source={"dispatch_id": entry["id"]}))
        self.context.ensure_exited()
        judgment = judge_review(result, envelope(schema, entry["task_id"], entry["id"]))
        if judgment.state == "invalid":
            raise self.record_invalid(key, judgment.error) from judgment.error
        rejection = InvalidReviewRejection(result, receipt, judgment.error) if judgment.state == "veto" else None
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
