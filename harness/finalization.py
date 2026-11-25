"""Own final attempt history and the final verification/review acceptance decision."""
from __future__ import annotations

import json
import os
from collections import Counter
from copy import deepcopy
from pathlib import Path

from . import checkout_identity, cleanup, git, interrupts, stops
from .context import compact_checks, compact_feedback
from .contracts import (
    REVIEW, REVIEW_OPTIONAL, core_schema, core_value, envelope, judge_review, review_approved, review_schema, review_veto,
    validate_structure,
)
from .recovery import bound_bytes, decode_evidence_json, evidence_json, evidence_path, run_files, snapshot_digest
from .review_progress import (
    batch_after, bind_review_source, entries, validate_review_source, required_findings, disposition_gaps, require_saved_batch,
)
from .services import RuntimeService
from .verification import checks_failed
from .util import EvidenceError, HarnessError, dump, write_json


FINAL_RECEIPT = "reviewer:final_review:final"


def index_mismatch_paths(listing, tree_listing):
    """Compare the full real index multiset to the stage-zero entries implied by the tree."""
    indexed, expected = Counter(), Counter()
    for entry in listing.split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            mode, oid, stage = metadata.split()
            indexed[path, mode, oid, stage] += 1
    for entry in tree_listing.split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            mode, _, oid = metadata.split()
            expected[path, mode, oid, b"0"] += 1
    return {os.fsdecode(path) for path, _, _, _ in (indexed - expected) | (expected - indexed)}


def same_snapshot_record(history, digest, head):
    """The latest whole-run record for this exact snapshot, if any."""
    return next((item for item in reversed(history)
                 if item["snapshot_digest"] == digest and item["head"] == head), None)


def feedback_view(record):
    """A whole-run record as feedback and decision input: its approved batches carry nothing to rework."""
    return {key: deepcopy(value) for key, value in record.items() if key != "approved_batches"}


def needs_decision(record, reverify):
    """A same-snapshot rejection, or a non-timeout failure not being re-verified, needs a decide."""
    if record is None:
        return False
    if record["status"] in ("rejected", "invalid_rejected"):
        return True
    return record["status"] == "verification_failed" and record["failure_kind"] != "timeout" and not reverify


# The whole-run record shapes final_review and leave_attempt write: each (status, phase) pair with the
# keys every writer of that pair writes, besides the keys every record has.
FINAL_COMMON_KEYS = ("attempt", "head", "tree", "snapshot_digest", "snapshot_artifact", "artifact", "accepted_tasks",
                     "verification_rounds", "approved_batches")
_VERIFIED = ("verification", "diff_artifact")
_CONCLUDED = (*_VERIFIED, "required_findings", "review", "review_source")
FINAL_STATES = {("running", "verifying"): (), ("running", "reviewing"): (*_VERIFIED, "required_findings"),
                ("verification_failed", "finished"): (*_VERIFIED, "failure_kind", "verification_failures"),
                ("approved", "finished"): _CONCLUDED, ("rejected", "finished"): _CONCLUDED,
                ("invalid_rejected", "finished"): _CONCLUDED,
                ("invalidated", "finished"): (*_VERIFIED, "required_findings", "review_source", "reason")}


def _text(value):
    return isinstance(value, str)


def _round(value):
    return (isinstance(value, dict) and _text(value.get("artifact_dir")) and value.get("status") in ("running", "completed")
            and _text(value.get("checkout_digest", "")))


def _check(value):
    return (isinstance(value, dict) and all(_text(value[key]) for key in ("stdout", "stderr", "log") if key in value)
            and isinstance(value.get("process_outcome", {}), dict))


# Wherever they appear, the values the readers index, iterate, join into paths or put in sets.
FINAL_VALUES = {"attempt": lambda value: type(value) is int and value >= 1,
                **dict.fromkeys(("head", "tree", "snapshot_digest", "artifact", "snapshot_artifact", "verification",
                                 "diff_artifact", "reason"), _text),
                "accepted_tasks": lambda value: isinstance(value, list) and all(map(_text, value)),
                "verification_rounds": lambda value: isinstance(value, list) and all(map(_round, value)),
                "required_findings": lambda value: isinstance(value, list),
                "failure_kind": lambda value: value in ("timeout", "exit_code"),
                "verification_failures": lambda value: isinstance(value, list) and all(map(_check, value)),
                "review_source": lambda value: isinstance(value, dict)}
REVIEW_CORE = core_schema(REVIEW, REVIEW_OPTIONAL)


def validate_final_records(history):
    """Refuse a whole-run record its readers would index into an unclassified crash.

    A projection may read a record before any traversal authenticates it (trigger
    without an operator answer), so the reviews' core structure, the one
    judge_review held before any was written, is checked here too. Digests,
    artifacts, mappings, dispositions and memory approvals stay with the traversals.
    """
    def fail(problem):
        raise HarnessError(f"运行台账整体记录无效：{problem}；保留原文件并核对证据")

    if not isinstance(history, list):
        fail("final_history 不是列表")
    entries(history, whole_run=True)
    for index, record in enumerate(history):
        field = f"final_history[{index}]"
        status, phase = record.get("status"), record.get("phase")
        keys = FINAL_STATES.get((status, phase)) if _text(status) and _text(phase) else None
        if keys is None:
            fail(f"{field}.status/phase 组合无效")
        for key in (*FINAL_COMMON_KEYS, *keys):
            if key not in record:
                fail(f"{field}.{key} 缺失")
        for key, valid in FINAL_VALUES.items():
            if key in record and not valid(record[key]):
                fail(f"{field}.{key} 取值无效")
        reviews = [(f"{field}.approved_batches[{number}].review", batch["review"])
                   for number, batch in enumerate(record["approved_batches"])]
        if "review" in record:
            reviews.append((f"{field}.review", record["review"]))
        for path, review in reviews:
            if not isinstance(review, dict):
                fail(f"{path} 不是对象")
            if not isinstance(review.get("finding_dispositions", {}), dict):
                fail(f"{path}.finding_dispositions 不是对象")
            try:
                validate_structure(core_value(review, (*REVIEW_OPTIONAL, "finding_dispositions")), REVIEW_CORE, path)
            except HarnessError as error:
                fail(f"审阅不合审阅契约的核心结构（{error}）")


def _matching_approvals(history, digest, commit, tree):
    return [(index, record) for index, record in enumerate(history)
            if record["status"] == "approved" and record["snapshot_digest"] == digest
            and record["head"] == commit and record["tree"] == tree]


def _verification_mode(run):
    return "fresh_checkout" if run["config_snapshot"]["verification"]["fresh_checkout"] else "run_worktree"


def validate_result(run):
    """Check the completed identity against the validated ledger, without consulting Git."""
    if run["status"] != "completed":
        if "result" in run:
            raise HarnessError("运行台账 result 只适用于 completed；保留原文件并核对证据")
        return
    result = run.get("result")
    if (not isinstance(result, dict)
            or result.keys() != {"commit", "tree", "snapshot_digest", "attempts", "verification_mode"}
            or result["commit"] != run["expected_head"]
            or not isinstance(result["tree"], str) or not isinstance(result["snapshot_digest"], str)
            or result["verification_mode"] != _verification_mode(run)):
        raise HarnessError("运行台账 result 的身份或验证模式无效；保留原文件并核对证据")

    attempts = result["attempts"]
    approved = _matching_approvals(run["final_history"], result["snapshot_digest"], result["commit"], result["tree"])
    if (not isinstance(attempts, list) or any(type(attempt) is not int for attempt in attempts)
            or not approved or attempts != [index + 1 for index, _ in approved]
            or any(record["attempt"] != index + 1 for index, record in approved)):
        raise HarnessError("运行台账 result.attempts 与批准记录不符；保留原文件并核对证据")


def verification_failure_kind(checks):
    failures = [check for check in checks if checks_failed(check)]
    if not failures:
        return None
    return "timeout" if all(check.get("timed_out") for check in failures) else "exit_code"


class FinalAcceptance(RuntimeService):
    def trigger(self, record):
        """The derived final-stage decision input; nothing persistent marks it."""
        return {"kind": "final",
                "final_review": compact_feedback({"final_review": feedback_view(record)})["final_review"],
                "accepted_since": sorted(set(self.context.run["accepted"]) - set(record["accepted_tasks"]))}

    def reclaim_verified_checkout(self, record):
        """Try to remove the latest round's verified fresh checkout; a failure never changes the verdict.

        Only a completed round with a creation registration is reclaimed.
        Authority and exit evidence failures stop the run as anywhere else; only the removal's own
        failure is recorded as a best-effort event, and the review proceeds.
        """
        rounds = record["verification_rounds"]
        if not rounds or rounds[-1].get("status") != "completed":
            return
        entry = checkout_identity.entry_for(self.context.run, record["attempt"], len(rounds))
        if entry is None:
            return
        artifact = cleanup.checkout_folder(self.context.directory, record, len(rounds)) / "checkout.json"
        self.context.store.check_current()
        self.context.ensure_exited()
        try:
            result = cleanup.clean_checkout(self.context.repo, self.context.run, self.context.directory, record,
                                            len(rounds), cleanup.common_dir(self.context.repo), True,
                                            state=self.context.state, by="runtime",
                                            authority=self.context.store.check_current)
        except cleanup.CheckoutRemovalIncomplete as error:
            self.context.observe("verification_checkout_reclaim_failed", attempt=record["attempt"],
                                 artifact=str(artifact),
                                 recorded_worktree=entry["worktree"],
                                 error=str(error)[:2000])
            return
        if "removed_by" in result:
            return
        self.context.observe("verification_checkout_reclaimed", attempt=record["attempt"],
                             artifact=str(artifact), worktree=result["worktree"])

    def completion_gate(self, digest, commit, tree):
        """Return the result identity for this snapshot if its approvals complete the run.

        Each approved record is authenticated and its mapping rechecked as the
        batch after everything before it, its own approved batches included;
        then one traversal of the whole history (approval contracts and copies
        are checked inside it) must leave nothing this snapshot lacks a
        resolution for. Findings reported
        since on another snapshot are not an error: a new attempt reviews them.
        """
        history = self.context.run["final_history"]
        approved = _matching_approvals(history, digest, commit, tree)
        if not approved:
            return False
        for index, item in approved:
            if item["attempt"] != index + 1:
                raise EvidenceError("整体批准的编号与台账位置不符；保留原证据，禁止完成 run",
                                    stop=stops.make("evidence", "runtime_integrity", source={"artifact": item["artifact"]}))
            validate_review_source(self.context.run, "final", item, kind="final_review", files=run_files(self.context), candidate_digest=digest)
            # The attempt as it was when its last batch was computed: running, with its approved batches.
            before = {"status": "running", "snapshot_digest": digest, "approved_batches": item.get("approved_batches")}
            if required_findings(self.context.run, "final", digest, files=run_files(self.context), whole_run=True,
                                 records=[*history[:index], before]) != item.get("required_findings"):
                raise HarnessError("整体批准缺少历史阻断问题的独立解决处置；禁止完成 run", stop=stops.make("runtime_failure", "invariant"))
        if required_findings(self.context.run, "final", digest, files=run_files(self.context), whole_run=True):
            return False
        return {"commit": commit, "tree": tree, "snapshot_digest": digest,
                "attempts": [item["attempt"] for _, item in approved],
                "verification_mode": _verification_mode(self.context.run)}

    def _written_candidate(self):
        """Find a possible written leave before any exit or terminal-capture checks."""
        history = self.context.run["final_history"]
        if not history or history[-1]["status"] != "running":
            return None
        record = history[-1]
        receipt = self.context.run["review_receipts"].get(FINAL_RECEIPT)
        # A leave binds and saves its original's digest before it writes; no other receipt was ever left.
        if (receipt is None or "artifact_digest" not in receipt
                or receipt.get("snapshot_digest") != record["snapshot_digest"]):
            return None
        files = run_files(self.context).under("verification", "final", str(record["attempt"]), "final.json")
        origin = {"artifact": record["artifact"]}
        evidence_path(files, origin)
        # This preflight looks for an optional, not-yet-registered leave.
        on_disk = files.read((), limit=None).or_local_io().bytes_or_none()
        if on_disk is None:
            return None
        try:
            shape = json.loads(on_disk)
        except (ValueError, UnicodeError, RecursionError):
            shape = None
        # Only a necessary condition, so that the attempt's own record costs no further read.
        if not isinstance(shape, dict) or shape.get("status") not in ("invalidated", "invalid_rejected"):
            return None
        return receipt, on_disk

    def _leave_original(self, record, receipt, *, bind=None):
        if "required_findings" not in record:
            raise HarnessError("整体审阅缺少必需问题对照；保留证据", stop=stops.make("runtime_failure", "invariant"))
        # Checked before the original is read or its digest bound.
        require_saved_batch(self.context.run, "final", record["snapshot_digest"], record["required_findings"],
                            files=run_files(self.context), whole_run=True,
                            records=self.context.run["final_history"], receipt=receipt)
        return bound_bytes(self.services.receipts.artifact_files(receipt).under("returned-result.json"),
                           receipt, "审阅", bind=bind)

    def _left_record(self, record, receipt, raw, digest):
        required = record["required_findings"]
        dispatch_id = receipt["dispatch_id"]
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            result = None
        updated = {**deepcopy(record), "phase": "finished", "status": "invalidated",
                   "review_source": {"dispatch_id": dispatch_id, "artifact": receipt["artifact"],
                                     "artifact_digest": digest, "snapshot_digest": record["snapshot_digest"]},
                   "reason": "候选已离开；原结果仅作待复核证据，不构成任何快照的批准"}
        if not isinstance(result, dict):
            updated["validation_error"] = "review result is not a JSON object"
        else:
            judgment = judge_review(result, envelope(review_schema(required), "final", dispatch_id))
            if judgment.state == "invalid" and not judgment.readable:
                updated["validation_error"] = str(judgment.error)
            else:
                updated["review"] = {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
                if judgment.error is not None:
                    updated["validation_error"] = str(judgment.error)
                if review_veto(result):
                    updated.update(status="invalid_rejected", reason="候选已离开；已返回的拒绝仍是对它审阅的快照的否决")
                # Authenticated as every later traversal will: a record none would accept is never written.
                validate_review_source(self.context.run, "final", updated, kind="final_review", files=run_files(self.context),
                                       candidate_digest=record["snapshot_digest"])
        return updated

    def _written_record(self, receipt, on_disk):
        """Authenticate a possible leave without writing or binding evidence."""
        record = self.context.run["final_history"][-1]
        raw, digest = self._leave_original(record, receipt)
        updated = self._left_record(record, receipt, raw, digest)
        # Compare with the exact bytes write_json writes for this leave.
        if on_disk == (dump(updated) + "\n").encode("utf-8"):
            return updated
        return None

    def leave_attempt(self):
        """Write the previous snapshot's returned review, then account for it.

        A veto remains evidence against its own snapshot. An approval closes
        nothing. An unreturned intent is discarded without closing the attempt.
        """
        history = self.context.run["final_history"]
        record = history[-1]
        receipts = self.services.receipts
        receipt = self.context.run["review_receipts"].get(FINAL_RECEIPT)
        if receipt is None:
            return False
        self.context.ensure_exited()
        undecodable = receipts.settle_return(FINAL_RECEIPT)
        if undecodable is not None:
            receipts.record_invalid(FINAL_RECEIPT, undecodable, source={
                "artifact": str(undecodable.artifact), "artifact_digest": undecodable.artifact_digest})
            return True
        if FINAL_RECEIPT not in self.context.run["review_receipts"]:
            return True
        if receipt.get("snapshot_digest") != record["snapshot_digest"]:
            raise EvidenceError("整体审阅回执与进行中的尝试不属于同一候选；保留证据",
                                stop=stops.make("evidence", "runtime_integrity", source={"dispatch_id": receipt.get("dispatch_id")}))
        receipts.validate_terminal_capture(receipt)
        if receipts.has_terminal_capture(receipt):
            # The rule of resume's preflight, for any path that did not pass it.
            raise HarnessError("终端结果的代码快照已变化；保留证据，禁止重新抽取结论", stop=stops.make("workspace_mismatch", "candidate_changed"))
        raw, digest = self._leave_original(record, receipt, bind=self.context.save)
        updated = self._left_record(record, receipt, raw, digest)
        write_json(Path(record["artifact"]), updated)
        with self.context.publish():
            history[-1] = updated
            self.context.run["review_receipts"].pop(FINAL_RECEIPT, None)
        self.context.event("final_attempt_left", attempt=updated["attempt"], dispatch_id=receipt["dispatch_id"],
                           status=updated["status"])
        return True

    def publish_written_leave(self):
        """Account for an authenticated leave from an earlier call, on any candidate."""
        found = self._written_candidate()
        if found is None:
            return False
        receipt, on_disk = found
        receipts = self.services.receipts
        self.context.ensure_exited()
        receipts.validate_terminal_capture(receipt)
        if receipts.has_terminal_capture(receipt):
            return False
        updated = self._written_record(receipt, on_disk)
        if updated is None:
            return False
        history = self.context.run["final_history"]
        with self.context.publish():
            history[-1] = updated
            self.context.run["review_receipts"].pop(FINAL_RECEIPT, None)
        self.context.event("final_attempt_left", attempt=updated["attempt"], dispatch_id=receipt["dispatch_id"],
                           status=updated["status"])
        interrupts.deliver()
        return True

    def written_leave_for_abandon(self):
        """Authenticate a possible leave after exit confirmation, without terminal capture."""
        found = self._written_candidate()
        if found is None:
            return None
        receipt, on_disk = found
        receipts = self.services.receipts
        if receipts.has_terminal_capture(receipt) or receipts.terminal_capture_pending(receipt, captured=False):
            return None
        return self._written_record(receipt, on_disk)

    def final_review(self, *, reverify=False):
        # Consume the resume flag on first entry, including new snapshots and
        # unfinished attempts that do not need an explicit retry.
        self.context.reverify_requested = False
        worktree = self.context.worktree
        binding = self.context.run["worktree_binding"]
        git.check_worktree_state(worktree)
        snapshot, listing = self.context.read_worktree()
        commit = self.context.run["expected_head"]
        if snapshot["head"] != commit:
            raise HarnessError("整体审阅前 HEAD 与检查点不同；保留现场",
                               stop=stops.make("workspace_mismatch", "head_changed", data={"expected_head": commit}))
        tree = git.git(worktree, "rev-parse", "--verify", f"{commit}^{{tree}}", effect=git.READ, binding=binding).strip()
        history = self.context.run["final_history"]
        for item in history:
            if item["head"] == commit and item["tree"] != tree:
                raise EvidenceError("整体尝试记录的树与提交不符；保留原证据，禁止完成 run",
                                    stop=stops.make("evidence", "runtime_integrity", source={"artifact": item["artifact"]}))
        tree_listing = git.git_bytes(worktree, "ls-tree", "-r", "-z", commit, effect=git.READ, binding=binding)
        paths = index_mismatch_paths(listing, tree_listing)
        proof = git.prove_image(worktree, commit, tree, binding=binding)
        if paths or not proof.equal:
            detail = proof.detail or git.describe_paths(sorted(paths.union(proof.paths)))
            raise HarnessError(detail, stop=stops.make("workspace_mismatch", "dirty_final_workspace"))
        digest = snapshot_digest(snapshot)
        # A leave an earlier call wrote was published before this step was derived (publish_written_leave).
        if history and history[-1]["status"] == "running" and (
                history[-1]["snapshot_digest"] != digest or history[-1]["head"] != snapshot["head"]):
            # Before any use of this snapshot's history: another snapshot's returned veto enters it first.
            if self.leave_attempt():
                interrupts.deliver()
                if self.context.snapshot() != snapshot:
                    # The leave wrote and saved; nothing is reused or completed for the entry snapshot.
                    raise HarnessError("离开进行中的整体尝试期间代码快照已变化；离开已保存，禁止复用批准或完成 run",
                                       stop=stops.make("workspace_mismatch", "candidate_changed"))
        same_snapshot = [item for item in history
                         if item["snapshot_digest"] == digest and item["head"] == snapshot["head"]]
        rejected = next((item for item in reversed(same_snapshot)
                         if item["status"] in ("rejected", "invalid_rejected")), None)
        if rejected:
            # The scheduler routes this snapshot to a decide; the review is never redrawn.
            raise HarnessError("整体审阅已拒绝同一代码快照；必须先通过修正任务，不能恢复后重复抽取审阅", stop=stops.make("runtime_failure", "invariant"))
        result = self.completion_gate(digest, commit, tree)
        if result:
            return result
        # An approval that does not cover the whole history is reviewed again here: the running attempt, else a new one.
        record = history[-1] if history and history[-1] in same_snapshot else None
        if not record or record["status"] != "running":
            retry_failed = record and record["status"] == "verification_failed" and record["failure_kind"] != "timeout"
            if retry_failed and not reverify:
                raise HarnessError("同一代码快照的整体验证曾失败；修复环境后使用 resume --reverify 显式重验", stop=stops.make("runtime_failure", "invariant"))
            attempt = len(history) + 1
            directories = run_files(self.context).make_directories(("verification", "final", str(attempt)))
            if directories.state != "present":
                directories.evidence({"artifact": directories.path})
            if retry_failed:
                # Explicit re-verification replaces a decide that never returned.
                self.services.receipts.discard_unreturned("commander:decide:run")
                self.context.event("final_reverification_requested", previous_attempt=record["attempt"])
            folder = self.services.verification.verification_directory("final", attempt, whole_run=True)
            snapshot_artifact = folder / "snapshot.json"
            record = {"attempt": attempt, "head": commit, "tree": tree, "snapshot_digest": digest,
                      "accepted_tasks": sorted(self.context.run["accepted"]),
                      "snapshot_artifact": str(snapshot_artifact), "phase": "verifying",
                      "verification_rounds": [], "approved_batches": [], "status": "running",
                      "artifact": str(folder / "final.json")}
            history.append(record)
            self.context.save()
            self.context.checkpoint()
        attempt = record["attempt"]
        files = run_files(self.context).under("verification", "final", str(attempt))
        folder = evidence_path(files.under("final.json"), {"artifact": record["artifact"]}).parent
        snapshot_files = files.under("snapshot.json")
        origin = {"artifact": record["snapshot_artifact"]}
        snapshot_artifact = evidence_path(snapshot_files, origin)
        outcome = snapshot_files.read((), limit=None).or_local_io()
        if outcome.state == "absent" and outcome.step.leaf:
            # The attempt directory existed before the ledger registered it.
            write_json(snapshot_artifact, snapshot)
        elif decode_evidence_json(
                outcome.evidence(origin), origin,
                message="整体快照证据无法读取或已改变；保留原证据，禁止覆盖") != snapshot:
            raise EvidenceError("整体快照证据与待恢复记录不符；保留原证据，禁止覆盖",
                                stop=stops.make("evidence", "runtime_integrity", source=origin))
        if record["phase"] == "verifying":
            # Computed before any record mutation: a failed evidence read saves nothing half-done.
            required = required_findings(self.context.run, "final", digest, files=run_files(self.context), whole_run=True, records=history[:-1])
            # Interrupted checks may have written partial evidence. Never overwrite it.
            rounds = record["verification_rounds"]
            verification_folder = folder / f"verification-{len(rounds) + 1}"
            round_record = {"artifact_dir": str(verification_folder), "status": "running"}
            rounds.append(round_record)
            self.context.save()
            checks = self.services.verification.verify("final", attempt, whole_run=True, artifact_dir=verification_folder,
                                                       before=snapshot, round_index=len(rounds))
            # Read before any record change: the verdict save binds this round's checkout record.
            checkout_digest = (cleanup.checkout_record_digest(self.context.directory, record, len(rounds),
                                                              state=self.context.state)
                               if self.context.config.data["verification"]["fresh_checkout"] else None)
            evidence = folder / "change.diff"
            evidence.write_bytes(git.range_diff_bytes(worktree, self.context.run["base_commit"], commit, binding=binding))
            # The verdict is computed and its record file written first; the ledger takes both in one transition.
            updated = deepcopy(record)
            completed = updated["verification_rounds"][-1]
            completed["status"] = "completed"
            if checkout_digest is not None:
                completed["checkout_digest"] = checkout_digest
            updated.update(verification=str(verification_folder / "results.json"), diff_artifact=str(evidence))
            failure_kind = verification_failure_kind(checks)
            feedback = None
            if failure_kind is not None:
                updated.update(status="verification_failed", phase="finished",
                               failure_kind=failure_kind, verification_failures=compact_checks(checks))
                if updated["failure_kind"] != "timeout":
                    feedback = {"final_review": feedback_view(updated)}
            else:
                updated.update(phase="reviewing", required_findings=required)
            write_json(folder / "final.json", updated)
            with self.context.publish():
                history[-1] = updated
                if feedback is not None:
                    self.context.run["feedback"] = feedback
            record = updated
        else:
            checks_files = files.under(f"verification-{len(record['verification_rounds'])}", "results.json")
            checks = evidence_json(checks_files, record["verification"])
        if record["phase"] == "reviewing":
            # The verdict is durable: first entry and recovery try to reclaim at this one point.
            self.reclaim_verified_checkout(record)
            changed_paths = git.range_paths(worktree, self.context.run["base_commit"], commit, binding=binding).split("\0")
            if "required_findings" not in record:
                raise HarnessError("整体审阅缺少必需问题对照；保留证据", stop=stops.make("runtime_failure", "invariant"))
            required = record["required_findings"]
            # The saved mapping is rechecked against the history before its review is dispatched or replayed.
            require_saved_batch(self.context.run, "final", digest, required, files=run_files(self.context), whole_run=True, records=history,
                                receipt=self.context.run.get("review_receipts", {}).get("reviewer:final_review:final"))
            review, diagnostics = self.services.receipts.dispatch_review("final_review", "final",
                     self.services.packets.final_review_packet(
                         checks=checks, diff_artifact=record["diff_artifact"],
                         changed_paths=changed_paths, ignored_paths=self.context.ignored_inventory(),
                         history=history[:-1], required=required),
                     required=required)
            source = bind_review_source(self.context, "final_review", "final", review, digest)
            discard_memory = not diagnostics
            disposition_errors = disposition_gaps(review, required)
            if disposition_errors:
                diagnostics = {**diagnostics, "disposition_errors": disposition_errors}
            if self.context.snapshot() != snapshot:
                raise HarnessError("整体审阅后文件发生变化；禁止完成 run", stop=stops.make("runtime_failure", "invariant"))
            approved = not diagnostics and review_approved(review)
            batch = {"dispatch_id": source["dispatch_id"], "required_findings": deepcopy(required),
                     "review": review, "review_source": source}
            # Computed before any record change: a failed read leaves nothing half-done.
            remaining = (batch_after(self.context.run, "final", digest, required, files=run_files(self.context), whole_run=True, records=[
                *history[:-1], {**record, "approved_batches": [*record["approved_batches"], batch]}])
                if approved else [])
            if discard_memory and not remaining:
                # The whole-run review approves no memory; any approvals are discarded (R8).
                self.services.tasks.approved_memory_keys("final", review, [])
            # The record file is written first; the ledger advances only after
            # it, so a failed write leaves the receipt to be replayed.
            if remaining:
                # An approving batch: the attempt stays in reviewing with the next batch's mapping.
                updated = {**deepcopy(record), "approved_batches": [*deepcopy(record["approved_batches"]), batch],
                           "required_findings": remaining}
            else:
                updated = {**deepcopy(record), "review_source": source, "review": review, "phase": "finished",
                           "status": "approved" if approved else "rejected", **diagnostics}
                feedback = None if approved else {"final_review": feedback_view(updated)}
            write_json(folder / "final.json", updated)
            with self.context.publish():
                history[-1] = updated
                self.context.run.get("review_receipts", {}).pop("reviewer:final_review:final", None)
                if not remaining:
                    self.context.run["feedback"] = feedback
            record = updated
            if remaining:
                self.context.event("final_review_batch_approved", attempt=attempt,
                                   dispatch_id=source["dispatch_id"], next_batch=len(remaining))
                return False
        if record["status"] == "approved":
            # Once the review is published, deliver before starting the completion gate.
            interrupts.deliver()
            # The first approval passes the same gate as a reused one.
            return self.completion_gate(digest, commit, tree)
        if record["status"] == "verification_failed":
            # A review's rejection already saved its feedback in the same save as its registration.
            self.context.run["feedback"] = {"final_review": feedback_view(record)}
            self.context.save()
        self.context.checkpoint()
        if record["status"] == "verification_failed":
            self.context.event("final_verification_failed", attempt=attempt, failure_kind=record["failure_kind"])
            if record["failure_kind"] == "timeout":
                raise HarnessError("整体验证超时；证据已保存，修复环境或增加验证超时后 resume 可重验", stop=stops.make("verification", "final_timeout", data={"config_key": "runtime.verify_timeout_seconds", "attempt": attempt}, source={"artifact": record["artifact"]}))
        else:
            self.context.event("final_review_rejected", attempt=attempt)
        return False
