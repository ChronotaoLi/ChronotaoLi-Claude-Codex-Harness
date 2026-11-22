"""Own final attempt history and the final verification/review acceptance decision."""
from __future__ import annotations

import json
import os
import stat
from copy import deepcopy
from pathlib import Path

from . import cleanup, git, stops
from .context import compact_checks, compact_feedback
from .contracts import (
    REVIEW, REVIEW_OPTIONAL, core_schema, core_value, envelope, judge_review, review_approved, review_schema, review_veto,
    validate_structure,
)
from .recovery import read_bound_bytes, read_evidence_json, snapshot_digest
from .review_progress import (
    batch_after, bind_review_source, validate_review_source, required_findings, disposition_gaps, require_saved_batch,
)
from .services import RuntimeService
from .verification import checks_failed
from .util import EvidenceError, HarnessError, dump, read_json, write_json


def regular_bytes(path):
    """A file's bytes, opened without following a link or blocking; None when it is no regular file or unreadable."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            return stream.read() if stat.S_ISREG(os.fstat(stream.fileno()).st_mode) else None
    except OSError:
        return None


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
FINAL_COMMON_KEYS = ("attempt", "head", "snapshot_digest", "snapshot_artifact", "artifact", "accepted_tasks",
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
                **dict.fromkeys(("head", "snapshot_digest", "artifact", "snapshot_artifact", "verification",
                                 "diff_artifact", "reason"), _text),
                "accepted_tasks": lambda value: isinstance(value, list) and all(map(_text, value)),
                "verification_rounds": lambda value: isinstance(value, list) and all(map(_round, value)),
                "required_findings": lambda value: isinstance(value, list),
                "failure_kind": lambda value: value in ("timeout", "exit_code"),
                "verification_failures": lambda value: isinstance(value, list) and all(map(_check, value)),
                "review_source": lambda value: isinstance(value, dict)}
REVIEW_CORE = core_schema(REVIEW, REVIEW_OPTIONAL)


def validate_final_records(history):
    """Refuse a whole-run record its readers would index into an unclassified crash; run after review_progress.entries.

    A projection may read a record before any traversal authenticates it (trigger
    without an operator answer), so the reviews' core structure, the one
    judge_review held before any was written, is checked here too. Digests,
    artifacts, mappings, dispositions and memory approvals stay with the traversals.
    """
    def fail(problem):
        raise HarnessError(f"运行台账整体记录无效：{problem}；保留原文件并核对证据")

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

        Only a round whose saved verdict bound its checkout record is touched:
        that digest is of the bytes that said ``verified``. Authority and exit
        evidence failures stop the run as anywhere else; only the removal's own
        failure is recorded as a best-effort event, and the review proceeds.
        """
        rounds = record["verification_rounds"]
        if not rounds or rounds[-1].get("status") != "completed" or "checkout_digest" not in rounds[-1]:
            return
        artifact = cleanup.checkout_folder(self.context.directory, record, len(rounds)) / "checkout.json"
        self.context.store.check_current()
        self.context.ensure_exited()
        try:
            result = cleanup.clean_checkout(self.context.repo, self.context.run, self.context.directory, record,
                                            len(rounds), cleanup.common_dir(self.context.repo), True,
                                            state=self.context.state, by="runtime")
        except (HarnessError, OSError, ValueError) as error:
            self.context.observe("verification_checkout_reclaim_failed", attempt=record["attempt"],
                                 artifact=str(artifact),
                                 recorded_worktree=cleanup.recorded_worktree(artifact, state=self.context.state),
                                 error=str(error)[:2000])
            return
        if "removed_by" in result:
            return
        self.context.observe("verification_checkout_reclaimed", attempt=record["attempt"],
                             artifact=str(artifact), worktree=result["worktree"])

    def completion_gate(self, digest, head):
        """Whether an approval of this snapshot completes the run; first approvals and reused ones alike.

        Each approved record is authenticated and its mapping rechecked as the
        batch after everything before it, its own approved batches included;
        then one traversal of the whole history (approval contracts and copies
        are checked inside it) must leave nothing this snapshot lacks a
        resolution for. Findings reported
        since on another snapshot are not an error: a new attempt reviews them.
        """
        history = self.context.run["final_history"]
        approved = [(index, item) for index, item in enumerate(history) if item["snapshot_digest"] == digest
                    and item["head"] == head and item["status"] == "approved"]
        if not approved:
            return False
        for index, item in approved:
            validate_review_source(self.context.run, "final", item, kind="final_review", candidate_digest=digest)
            # The attempt as it was when its last batch was computed: running, with its approved batches.
            before = {"status": "running", "snapshot_digest": digest, "approved_batches": item.get("approved_batches")}
            if required_findings(self.context.run, "final", digest, whole_run=True,
                                 records=[*history[:index], before]) != item.get("required_findings"):
                raise HarnessError("整体批准缺少历史阻断问题的独立解决处置；禁止完成 run", stop=stops.make("runtime_failure", "invariant"))
        return not required_findings(self.context.run, "final", digest, whole_run=True)

    def leave_attempt(self, *, written=False):
        """Leave the last attempt, running on another snapshot: its returned review becomes its conclusion.

        Without a receipt it stays running; an unreturned intent is discarded.
        A returned review is judged under the attempt's saved mapping: a veto
        stays a rejection of the snapshot it reviewed (invalid_rejected),
        anything else approves nothing (invalidated); neither closes a finding.
        A terminal capture is never archived on another snapshot, and a returned
        review bound to another candidate may be a veto no attempt can hold:
        both stop and keep the receipt. Every read and check comes first, then
        the record file, then one transition. Returns whether the ledger changed.

        The written record file is the leave's publication point: its bytes
        follow from the attempt, its saved mapping and the returned original
        alone, never from the candidate a later call finds, so a leave once
        written stands on any snapshot. With ``written`` (``publish_written_leave``,
        whatever the candidate) nothing is written: the leave is published only
        when the file holds exactly the bytes it would write. Anything else there
        is no evidence of a leave: on the attempt's snapshot the receipt replays
        as its review, on another one the leave is made as before.
        """
        history = self.context.run["final_history"]
        record = history[-1]
        key = "reviewer:final_review:final"
        receipts = self.services.receipts
        receipt = self.context.run["review_receipts"].get(key)
        if written:
            # A leave binds and saves its original's digest before it writes; no other receipt was ever left.
            if (receipt is None or "artifact_digest" not in receipt
                    or receipt.get("snapshot_digest") != record["snapshot_digest"]):
                return False
            on_disk = regular_bytes(record["artifact"])
            try:
                shape = json.loads(on_disk) if on_disk is not None else None
            except (ValueError, UnicodeError, RecursionError):
                shape = None
            # Only a necessary condition, so that the attempt's own record costs no further read.
            if not isinstance(shape, dict) or shape.get("status") not in ("invalidated", "invalid_rejected"):
                return False
        else:
            if receipt is None:
                return False
            if not receipts.returned(receipt):
                receipts.discard_unreturned(key)
                return True
            if receipt.get("snapshot_digest") != record["snapshot_digest"]:
                raise EvidenceError("整体审阅回执与进行中的尝试不属于同一候选；保留证据",
                                    stop=stops.make("evidence", "runtime_integrity", source={"dispatch_id": receipt.get("dispatch_id")}))
        self.context.ensure_exited()
        receipts.validate_terminal_capture(receipt)
        if receipts.has_terminal_capture(receipt):
            if written:
                # A capture is never left; on its own snapshot it is consumed as before.
                return False
            # The rule of resume's preflight, for any path that did not pass it.
            raise HarnessError("终端结果的代码快照已变化；保留证据，禁止重新抽取结论", stop=stops.make("workspace_mismatch", "candidate_changed"))
        if "required_findings" not in record:
            raise HarnessError("整体审阅缺少必需问题对照；保留证据", stop=stops.make("runtime_failure", "invariant"))
        required = record["required_findings"]
        # Checked before the original is read or its digest bound.
        require_saved_batch(self.context.run, "final", record["snapshot_digest"], required, whole_run=True,
                            records=history, receipt=receipt)
        raw, digest = read_bound_bytes(receipt, "审阅", bind=self.context.save)
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
                validate_review_source(self.context.run, "final", updated, kind="final_review",
                                       candidate_digest=record["snapshot_digest"])
        if not written:
            write_json(Path(record["artifact"]), updated)
        elif on_disk != (dump(updated) + "\n").encode("utf-8"):
            # Compared with the exact bytes write_json writes for this leave.
            return False
        with self.context.transition():
            history[-1] = updated
            self.context.run["review_receipts"].pop(key, None)
        self.context.event("final_attempt_left", attempt=updated["attempt"], dispatch_id=dispatch_id,
                           status=updated["status"])
        return True

    def publish_written_leave(self):
        """Publish a leave an earlier call wrote but never saved, before anything is derived from the history.

        Decided from the ledger in memory first: only a running last attempt
        whose returned review has its digest bound can have one, and only then
        is its record file read. The same record is published on any candidate.
        """
        history = self.context.run["final_history"]
        return bool(history and history[-1]["status"] == "running" and self.leave_attempt(written=True))

    def final_review(self, *, reverify=False):
        # Consume the resume flag on first entry, including new snapshots and
        # unfinished attempts that do not need an explicit retry.
        self.context.reverify_requested = False
        git.check_worktree_state(self.context.worktree)
        if not git.clean(self.context.worktree, binding=self.context.run["worktree_binding"]):
            raise HarnessError("整体审阅要求干净的已接受提交；未接受的修改不能用于完成 run", stop=stops.make("workspace_mismatch", "dirty_final_workspace"))
        snapshot = self.context.snapshot()
        if snapshot["head"] != self.context.run["expected_head"]:
            raise HarnessError("整体审阅前 HEAD 与检查点不同；保留现场", stop=stops.make("workspace_mismatch", "head_changed", data={"expected_head": self.context.run["expected_head"]}))
        digest = snapshot_digest(snapshot)
        history = self.context.run["final_history"]
        # A leave an earlier call wrote was published before this step was derived (publish_written_leave).
        if history and history[-1]["status"] == "running" and (
                history[-1]["snapshot_digest"] != digest or history[-1]["head"] != snapshot["head"]):
            # Before any use of this snapshot's history: another snapshot's returned veto enters it first.
            if self.leave_attempt() and self.context.snapshot() != snapshot:
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
        if self.completion_gate(digest, snapshot["head"]):
            return True
        # An approval that does not cover the whole history is reviewed again here: the running attempt, else a new one.
        record = history[-1] if history and history[-1] in same_snapshot else None
        if not record or record["status"] != "running":
            if record and record["status"] == "verification_failed" and record["failure_kind"] != "timeout":
                if not reverify:
                    raise HarnessError("同一代码快照的整体验证曾失败；修复环境后使用 resume --reverify 显式重验", stop=stops.make("runtime_failure", "invariant"))
                # An explicit re-verification replaces a decide that never
                # returned; a returned one was refused before any write.
                self.services.receipts.discard_unreturned("commander:decide:run")
                self.context.event("final_reverification_requested", previous_attempt=record["attempt"])
            attempt = len(history) + 1
            folder = self.services.verification.verification_directory("final", attempt, whole_run=True)
            snapshot_artifact = folder / "snapshot.json"
            record = {"attempt": attempt, "head": self.context.run["expected_head"], "snapshot_digest": digest,
                      "accepted_tasks": sorted(self.context.run["accepted"]),
                      "snapshot_artifact": str(snapshot_artifact), "phase": "verifying",
                      "verification_rounds": [], "approved_batches": [], "status": "running",
                      "artifact": str(folder / "final.json")}
            history.append(record)
            self.context.save()
            self.context.checkpoint()
        attempt = record["attempt"]
        folder = Path(record["artifact"]).parent
        snapshot_artifact = Path(record["snapshot_artifact"])
        if snapshot_artifact.exists() or snapshot_artifact.is_symlink():
            with stops.evidence_reads(source={"artifact": str(snapshot_artifact)},
                                      message="整体快照证据无法读取或已改变；保留原证据，禁止覆盖"):
                saved_snapshot = read_json(snapshot_artifact)
                if saved_snapshot != snapshot:
                    raise HarnessError("整体快照证据与待恢复记录不符；保留原证据，禁止覆盖")
        else:
            # The attempt and its recovery phase are durable before artifact I/O.
            write_json(snapshot_artifact, snapshot)
        if record["phase"] == "verifying":
            # Computed before any record mutation: a failed evidence read saves nothing half-done.
            required = required_findings(self.context.run, "final", digest, whole_run=True, records=history[:-1])
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
            evidence.write_bytes(git.range_diff_bytes(self.context.worktree, self.context.run["base_commit"], binding=self.context.run["worktree_binding"]))
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
            with self.context.transition():
                history[-1] = updated
                if feedback is not None:
                    self.context.run["feedback"] = feedback
            record = updated
        else:
            checks = read_evidence_json(Path(record["verification"]))
        if record["phase"] == "reviewing":
            # The verdict is durable: first entry and recovery try to reclaim at this one point.
            self.reclaim_verified_checkout(record)
            changed_paths = git.range_paths(self.context.worktree, self.context.run["base_commit"],
                                            binding=self.context.run["worktree_binding"]).split("\0")
            if "required_findings" not in record:
                raise HarnessError("整体审阅缺少必需问题对照；保留证据", stop=stops.make("runtime_failure", "invariant"))
            required = record["required_findings"]
            # The saved mapping is rechecked against the history before its review is dispatched or replayed.
            require_saved_batch(self.context.run, "final", digest, required, whole_run=True, records=history,
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
            remaining = (batch_after(self.context.run, "final", digest, required, whole_run=True, records=[
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
            with self.context.transition():
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
            # The first approval passes the same gate as a reused one.
            return self.completion_gate(digest, snapshot["head"])
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
