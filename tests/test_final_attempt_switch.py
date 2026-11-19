"""Leaving a whole-run attempt when the candidate changes: its returned review becomes its conclusion (spec-1 rule 15)."""
import contextlib
import copy
import hashlib
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

import harness.dispatch as dispatch
import harness.finalization as finalization
from harness.dispatch import Dispatcher
from harness.finalization import FinalAcceptance
from harness.packets import PacketBuilder
from harness.receipts import ResultReceipts
from harness.util import HarnessError, dump, read_json, write_json
from test_harness import RepoCase, after_save
import test_review_batches as batches   # by module: a TestCase imported by name is collected again here
from test_runtime_changes import ScriptedBackend, finding, revised, trigger_kind

KEY = "reviewer:final_review:final"


def three_rejections(extra=None):
    """Whole-run reviews 1-3 reject with 22 new findings each, each followed by a correction task that changes
    the candidate: the fourth attempt lacks 66, batches of 60 and 6. ``extra`` changes the later whole-run reviews."""
    def change(backend, kwargs, packet, result):
        finals = len(batches.calls(backend, kind="final_review"))
        if kwargs["kind"] == "final_review" and finals <= 3:
            result.update(verdict="request_changes",
                          findings=[{**finding(), "id": f"g{finals}-{index}"} for index in range(22)])
        elif kwargs["kind"] == "final_review" and extra:
            extra(backend, kwargs, packet, result, finals)
        elif trigger_kind(kwargs, packet) == "final":
            task = copy.deepcopy(packet["original_tasks"][0])
            task.update(id=f"correct{finals}", depends_on=["greet"])
            revised(result, packet, "Resolve final findings", new_tasks=[task])
        elif kwargs["role"] == "worker" and kwargs["task"]["id"].startswith("correct"):
            batches.touch(kwargs, backend)
    return ScriptedBackend(change)


def reject_on_a(backend, kwargs, packet, result, finals):
    """The fourth attempt's first batch rejects: its first key open and one new finding."""
    if finals == 4:
        result.update(verdict="request_changes", findings=[{**finding(), "id": "found-on-a"}])
        result["finding_dispositions"][packet["required_findings"][0]["key"]] = {
            "status": "open", "evidence": ["still wrong on A"]}


def before_judgment(backend, *at):
    """Stop once before the whole-run review receipt is judged, when ``at`` whole-run reviews have been called;
    the returned receipt stays."""
    original, raised = ResultReceipts.finish_review_receipt, set()

    def interrupt(service, key, schema):
        count = len(batches.calls(backend, kind="final_review"))
        if key == KEY and count in at and count not in raised:
            raised.add(count)
            raise KeyboardInterrupt()
        return original(service, key, schema)
    return patch.object(ResultReceipts, "finish_review_receipt", autospec=True, side_effect=interrupt)


def after_first_approval():
    """Stop when the first whole-run approval returns true: saved approved, the run not completed."""
    original, raised = FinalAcceptance.final_review, []

    def interrupt(service, **kwargs):
        outcome = original(service, **kwargs)
        if outcome and not raised:
            raised.append(True)
            raise KeyboardInterrupt()
        return outcome
    return patch.object(FinalAcceptance, "final_review", autospec=True, side_effect=interrupt)


def reject_first(backend, kwargs, packet, result):
    """The first whole-run review rejects with one new finding; the correction task it leads to changes the candidate."""
    if kwargs["kind"] == "final_review" and len(batches.calls(backend, kind="final_review")) == 1:
        result.update(verdict="request_changes", findings=[{**finding(), "id": "found-on-a"}])
    elif trigger_kind(kwargs, packet) == "final":
        task = copy.deepcopy(packet["original_tasks"][0])
        task.update(id="correct", depends_on=["greet"])
        revised(result, packet, "Resolve final findings", new_tasks=[task])
    elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
        batches.touch(kwargs, backend)


def at_leave_write(action, *, after=True):
    """Run ``action`` once, right after (or before) the leave writes its record file, before the ledger takes it."""
    original, hit = finalization.write_json, []

    def writing(path, value):
        leave = Path(path).name == "final.json" and value.get("status") in ("invalidated", "invalid_rejected")
        if leave and not after and not hit:
            hit.append(True)
            action()
        result = original(path, value)
        if leave and after and not hit:
            hit.append(True)
            action()
        return result
    return patch.object(finalization, "write_json", side_effect=writing), hit


def sigint():
    os.kill(os.getpid(), signal.SIGINT)


def c_then_a(verdict):
    """The first whole-run review (on C) rejects; the second (on A) returns ``verdict``; the decide on a whole-run
    rejection adds a correction task that changes the candidate; later reviews approve."""
    def change(backend, kwargs, packet, result):
        finals = len(batches.calls(backend, kind="final_review"))
        if kwargs["kind"] == "final_review" and (finals == 1 or finals == 2 and verdict == "request_changes"):
            result.update(verdict="request_changes", findings=[{**finding(), "id": f"found-{finals}"}])
        elif trigger_kind(kwargs, packet) == "final":
            task = copy.deepcopy(packet["original_tasks"][0])
            task.update(id="correct", depends_on=["greet"])
            revised(result, packet, "Resolve final findings", new_tasks=[task])
        elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
            batches.touch(kwargs, backend)
    return ScriptedBackend(change)


def rejected_on_c(case, engine, run):
    """``run`` reaches the first whole-run review, on C; the run stops once its rejection is saved, before the decide."""
    window, _ = after_save(engine, lambda saved: statuses(saved) == ["rejected"], sigint)
    with window, case.assertRaises(KeyboardInterrupt):
        run()


def written_on_b(case, engine, backend, *, published=False):
    """C rejected and stopped before the decide: A's whole-run review returns and the run stops before judging it; on
    B the leave of A's attempt writes its record file and a signal arrives before the ledger takes it (``published``:
    right after its transition). Returns the candidate file and A's receipt; the candidate is B."""
    hello = engine.worktree / "hello.py"
    os.chmod(hello, 0o664)                              # A
    with before_judgment(backend, 2), case.assertRaises(KeyboardInterrupt):
        case.engine(backend).resume(engine.run["id"])
    receipt = batches.state(engine)["review_receipts"][KEY]
    os.chmod(hello, 0o646)                              # B
    resumed = case.engine(backend)
    window, hit = ((batches.interrupt_at_event(resumed, "final_attempt_left"), [True]) if published
                   else at_leave_write(sigint))
    with window, case.assertRaises(KeyboardInterrupt):
        resumed.resume(engine.run["id"])
    case.assertTrue(hit)
    saved = batches.state(engine)
    case.assertEqual((statuses(saved)[1] == "running", KEY in saved["review_receipts"]), (not published, not published))
    return hello, receipt


def call_trace(calls):
    return [(call["role"], call["kind"], call["task_id"],
             [row.get("label") for row in call["packet"].get("required_findings", [])]) for call in calls]


def ident(row):
    return row["review_dispatch_id"], row["finding_index"]


def statuses(saved):
    return [record["status"] for record in saved["final_history"]]


class AttemptSwitchTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.config.data["runtime"].update(max_attempts=10)

    def fourth_returned(self, backend):
        """The fourth attempt's first batch returns and the run stops before it is judged."""
        engine = self.engine(backend)
        with before_judgment(backend, 4), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", batches.plan())
        saved = batches.state(engine)
        self.assertEqual(statuses(saved), ["rejected"] * 3 + ["running"])
        self.assertIn("artifact_digest", saved["review_receipts"][KEY])
        return engine, saved

    def assert_left(self, record, receipt, status):
        self.assertEqual((record["status"], record["phase"]), (status, "finished"))
        self.assertEqual(record["review_source"]["dispatch_id"], receipt["dispatch_id"])
        self.assertTrue(record["reason"].startswith("候选已离开"))
        self.assertNotIn("returned_artifact", record)
        self.assertEqual(read_json(Path(record["artifact"])), record)

    def test_a_returned_rejection_is_kept_as_the_left_attempts_veto(self):
        """T4-1: no candidate_changed stop; the left rejection's open key and new finding are in B's batches."""
        backend = three_rejections(reject_on_a)
        engine, saved = self.fourth_returned(backend)
        receipt, reopened = saved["review_receipts"][KEY], saved["final_history"][-1]["required_findings"][0]
        os.chmod(engine.worktree / "hello.py", 0o664)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(statuses(result), ["rejected"] * 3 + ["invalid_rejected", "approved"])
        left = result["final_history"][3]
        self.assert_left(left, receipt, "invalid_rejected")
        self.assertEqual(left["review"]["verdict"], "request_changes")
        self.assertNotIn("validation_error", left)
        self.assertNotEqual(result["final_history"][4]["snapshot_digest"], left["snapshot_digest"])
        finals = batches.calls(backend, kind="final_review")
        self.assertEqual(batches.sizes(finals), [0, 22, 44, 60, 60, 7])
        # Open findings are chosen first: the key the left rejection held open and the finding it reported.
        first = finals[4]["packet"]["required_findings"]
        self.assertIn(ident(reopened), [ident(row) for row in first])
        self.assertEqual(batches.labels(first)[-1], "found-on-a")
        self.assertEqual([(row["attempt"], row["dispatch_id"], row["status"]) for row in batches.events(engine, "final_attempt_left")],
                         [(4, receipt["dispatch_id"], "invalid_rejected")])

    def test_an_unreturned_intent_is_discarded_and_the_attempt_stays_running(self):
        """T4-2: the call never returned; the fourth attempt keeps running and B starts its own."""
        def unreturned(backend, kwargs, packet, result, finals):
            if finals == 4:
                raise HarnessError("Reviewer service unavailable")
        backend = three_rejections(unreturned)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "Reviewer service unavailable"):
            engine.start("Implement greet", batches.plan())
        saved = batches.state(engine)
        receipt = saved["review_receipts"][KEY]
        self.assertNotIn("artifact_digest", receipt)
        self.assertFalse(Path(receipt["artifact"]).exists())
        os.chmod(engine.worktree / "hello.py", 0o664)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(statuses(result), ["rejected"] * 3 + ["running", "approved"])
        self.assertEqual(result["final_history"][3], saved["final_history"][3])
        self.assertEqual(batches.sizes(batches.calls(backend, kind="final_review")), [0, 22, 44, 60, 60, 6])
        self.assertEqual(batches.events(engine, "final_attempt_left"), [])

    def test_a_failed_record_write_leaves_the_attempt_running_and_is_redone(self):
        """T4-3: the record file is written before the ledger; a failed write changes nothing the resume needs."""
        backend = three_rejections(reject_on_a)
        engine, saved = self.fourth_returned(backend)
        os.chmod(engine.worktree / "hello.py", 0o664)
        original, hit = finalization.write_json, []

        def writing(path, value):
            if Path(path).name == "final.json" and isinstance(value, dict) and "reason" in value and not hit:
                hit.append(True)
                raise PermissionError(13, "denied", str(path))
            return original(path, value)
        finals = len(batches.calls(backend, kind="final_review"))
        with patch.object(finalization, "write_json", side_effect=writing), \
                self.assertRaises((HarnessError, PermissionError)):
            self.engine(backend).resume(engine.run["id"])
        self.assertTrue(hit)
        failed = batches.state(engine)
        self.assertNotEqual(failed["status"], "completed")
        record = failed["final_history"][3]
        self.assertEqual((record["status"], record["phase"], len(failed["final_history"])), ("running", "reviewing", 4))
        self.assertEqual(failed["review_receipts"][KEY], saved["review_receipts"][KEY])
        self.assertEqual(read_json(Path(record["artifact"]))["status"], "running")
        self.assertEqual(len(batches.calls(backend, kind="final_review")), finals)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assert_left(result["final_history"][3], saved["review_receipts"][KEY], "invalid_rejected")
        self.assertEqual(batches.sizes(batches.calls(backend, kind="final_review")), [0, 22, 44, 60, 60, 7])

    def test_a_failed_record_write_after_binding_the_digest_is_redone(self):
        """T4-3, the original written before its digest was bound: the leave binds it in a save of its own (evidence
        binding) before the record file. A failed write keeps the attempt and the receipt slot, the receipt now with
        its digest, and a resume redoes the leave."""
        backend = ScriptedBackend()
        engine = self.engine(backend)
        original = dispatch.write_json

        def signal_after_the_original(path, value):
            result = original(path, value)
            if Path(path).name == "returned-result.json" and value.get("task_id") == "final":
                sigint()
            return result
        with patch.object(dispatch, "write_json", side_effect=signal_after_the_original), \
                self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", batches.plan())
        old = batches.state(engine)
        receipt = old["review_receipts"][KEY]
        self.assertNotIn("artifact_digest", receipt)
        digest = hashlib.sha256(Path(receipt["artifact"]).read_bytes()).hexdigest()
        os.chmod(engine.worktree / "hello.py", 0o664)
        written = finalization.write_json

        def failing(path, value):
            if Path(path).name == "final.json" and value.get("status") == "invalidated":
                raise PermissionError(13, "denied", str(path))
            return written(path, value)
        with patch.object(finalization, "write_json", side_effect=failing), \
                self.assertRaises((HarnessError, PermissionError)):
            self.engine(backend).resume(engine.run["id"])
        failed = batches.state(engine)
        self.assertEqual(failed["final_history"], old["final_history"])
        self.assertEqual(failed["review_receipts"][KEY], {**receipt, "artifact_digest": digest})
        self.assertEqual(read_json(Path(old["final_history"][0]["artifact"]))["status"], "running")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(statuses(result), ["invalidated", "approved"])
        self.assert_left(result["final_history"][0], failed["review_receipts"][KEY], "invalidated")
        self.assertEqual(len(batches.calls(backend, kind="final_review")), 2)

    def test_a_returned_veto_on_another_snapshot_enters_before_a_reused_approval(self):
        """T4-4: A approved and saved; B's first batch returns a rejection, unregistered; back on A it is reviewed."""
        phase = {"b": False}

        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and phase["b"]:
                phase["b"] = False
                result.update(verdict="request_changes", findings=[{**finding(), "id": "only-on-b"}])
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with after_first_approval(), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", batches.plan())
        self.assertEqual(statuses(batches.state(engine)), ["approved"])
        hello = engine.worktree / "hello.py"
        os.chmod(hello, 0o664)
        phase["b"] = True
        with before_judgment(backend, 2), self.assertRaises(KeyboardInterrupt):
            self.engine(backend).resume(engine.run["id"])
        saved = batches.state(engine)
        self.assertEqual(statuses(saved), ["approved", "running"])
        self.assertIn(KEY, saved["review_receipts"])
        os.chmod(hello, 0o644)
        before = len(batches.calls(backend, kind="final_review"))
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(statuses(result), ["approved", "invalid_rejected", "approved"])
        history = result["final_history"]
        self.assertEqual(history[0]["snapshot_digest"], history[2]["snapshot_digest"])
        finals = batches.calls(backend, kind="final_review")
        self.assertEqual(len(finals) - before, 1)
        self.assertEqual(batches.labels(finals[-1]["packet"]["required_findings"]), ["only-on-b"])

    def test_a_left_rejection_is_not_redrawn_on_its_snapshot(self):
        """T4-5: back on A, whose left rejection still rejects it, the next call is a decide, never a review of A."""
        backend = three_rejections(reject_on_a)
        engine = self.engine(backend)
        with before_judgment(backend, 4, 5):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", batches.plan())
            hello = engine.worktree / "hello.py"
            a_digest = batches.state(engine)["final_history"][-1]["snapshot_digest"]
            os.chmod(hello, 0o664)                      # B: A is left; B's first batch returns, unregistered
            with self.assertRaises(KeyboardInterrupt):
                self.engine(backend).resume(engine.run["id"])
            mid = batches.state(engine)
            self.assertEqual(statuses(mid), ["rejected"] * 3 + ["invalid_rejected", "running"])
            b_receipt = mid["review_receipts"][KEY]
            os.chmod(hello, 0o644)                      # back to A
            before = len(backend.calls)
            result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        after = backend.calls[before:]
        self.assertEqual((after[0]["role"], after[0]["kind"]), ("commander", "decide"))
        self.assertEqual(after[0]["packet"]["trigger"]["final_review"]["attempt"], 4)
        history = result["final_history"]
        self.assertEqual([record["attempt"] for record in history[5:] if record["snapshot_digest"] == a_digest], [])
        # B's attempt was left at the next whole-run review: its returned approval approves nothing.
        self.assert_left(history[4], b_receipt, "invalidated")
        self.assertEqual(history[4]["review"]["verdict"], "approve")
        self.assertEqual([row["status"] for row in batches.events(engine, "final_attempt_left")],
                         ["invalid_rejected", "invalidated"])

    def test_a_terminal_capture_on_another_snapshot_keeps_the_original_stop(self):
        """T4-6: resume's preflight refuses, and so does the leave when a path skips that preflight."""
        from test_terminal_capture_recovery import LingeringCLI
        self.config.data["roles"]["reviewer"]["cli"] = "claude"
        backend = LingeringCLI(self.config, self.repo, "final_review")
        self.addCleanup(backend.release)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "Process evidence incomplete"):
            engine.start("Implement greet", batches.plan())
        backend.release()
        stopped = batches.state(engine)
        receipt = stopped["review_receipts"][KEY]
        self.assertIn("terminal_capture_digest", receipt)
        hello = engine.worktree / "hello.py"
        os.chmod(hello, 0o664)
        calls = len(backend.calls)
        # The leave applies the same rule when a path reaches it without resume's preflight check.
        skip = lambda: patch.object(ResultReceipts, "validate_pending_terminal_captures", autospec=True)
        for name, window in (("resume preflight", contextlib.nullcontext), ("leave", skip)):
            with self.subTest(name):
                with window(), self.assertRaises(HarnessError) as caught:
                    self.engine(backend).resume(engine.run["id"])
                # The raised stop is the refusal's; the preflight does not write it into the ledger.
                self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]),
                                 ("workspace_mismatch", "candidate_changed"))
                saved = batches.state(engine)
                self.assertEqual(saved["final_history"], stopped["final_history"])
                self.assertEqual(saved["review_receipts"][KEY], receipt)
                self.assertEqual(len(backend.calls), calls)
        os.chmod(hello, 0o644)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(statuses(result), ["approved"])
        self.assertEqual(result["final_history"][0]["review_source"]["dispatch_id"], receipt["dispatch_id"])

    def test_a_returned_approval_of_the_left_attempt_closes_nothing(self):
        """T4-7: an approval does not survive a candidate change; B's first batch is the same 60 rows."""
        backend = three_rejections()
        engine, saved = self.fourth_returned(backend)
        rows = saved["final_history"][-1]["required_findings"]
        os.chmod(engine.worktree / "hello.py", 0o664)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(statuses(result), ["rejected"] * 3 + ["invalidated", "approved"])
        left = result["final_history"][3]
        self.assert_left(left, saved["review_receipts"][KEY], "invalidated")
        self.assertEqual(left["review"]["verdict"], "approve")
        finals = batches.calls(backend, kind="final_review")
        self.assertEqual(batches.sizes(finals), [0, 22, 44, 60, 60, 6])
        self.assertEqual([(row["key"], *ident(row)) for row in finals[4]["packet"]["required_findings"]],
                         [(row["key"], *ident(row)) for row in rows])

    def test_an_unreadable_returned_output_is_kept_without_a_review(self):
        """Rule 15: an original without a readable structure is kept as its validation error only; the ledger
        still loads, and the left attempt closes and reports nothing."""
        def unreadable(backend, kwargs, packet, result, finals):
            if finals == 4:
                result.pop("verdict")
        backend = three_rejections(unreadable)
        engine, saved = self.fourth_returned(backend)
        os.chmod(engine.worktree / "hello.py", 0o664)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        left = result["final_history"][3]
        self.assert_left(left, saved["review_receipts"][KEY], "invalidated")
        self.assertNotIn("review", left)
        self.assertTrue(left["validation_error"])
        self.assertEqual(batches.sizes(batches.calls(backend, kind="final_review")), [0, 22, 44, 60, 60, 6])
        engine.services.lifecycle.load_checks()

    def test_a_returned_review_bound_to_another_candidate_stops_and_keeps_it(self):
        """Rule 15, third round: approved on S, an attempt on T whose review was bound to S (the dispatch race the
        first-dispatch check now refuses). The review may be a veto no attempt holds: S's approval is never reused."""
        phase = {"race": False}

        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and phase["race"]:
                result.update(verdict="request_changes", findings=[{**finding(), "id": "found-on-s"}])
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with after_first_approval(), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", batches.plan())
        hello = engine.worktree / "hello.py"
        os.chmod(hello, 0o664)                          # T
        phase["race"] = True
        build = PacketBuilder.final_review_packet

        def built_then_switched(service, **kwargs):
            packet = build(service, **kwargs)
            os.chmod(hello, 0o644)                      # back to S before the dispatch binds its snapshot
            return packet
        with patch.object(PacketBuilder, "final_review_packet", autospec=True, side_effect=built_then_switched), \
                patch.object(Dispatcher, "require_review_candidate", autospec=True), \
                self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        stopped = batches.state(engine)
        self.assertEqual(statuses(stopped), ["approved", "running"])
        receipt = stopped["review_receipts"][KEY]
        self.assertIn("artifact_digest", receipt)
        self.assertEqual(receipt["snapshot_digest"], stopped["final_history"][0]["snapshot_digest"])
        self.assertNotEqual(receipt["snapshot_digest"], stopped["final_history"][1]["snapshot_digest"])
        calls = len(backend.calls)
        with self.assertRaises(HarnessError) as caught:
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), ("evidence", "runtime_integrity"))
        self.assertIn("不属于同一候选", str(caught.exception))
        saved = batches.state(engine)
        self.assertNotEqual(saved["status"], "completed")
        self.assertEqual(batches.stop_of(saved), ("evidence", "runtime_integrity"))
        self.assertEqual(saved["final_history"], stopped["final_history"])
        self.assertEqual(saved["review_receipts"][KEY], receipt)
        self.assertEqual(len(backend.calls), calls)

    def switched_back(self, backend, *, written):
        """The first whole-run review returns on A and the run stops before judging it; on B a signal interrupts the
        leave after (``written``) or before it writes the record file; then the candidate is A again."""
        engine = self.engine(backend)
        with before_judgment(backend, 1), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", batches.plan())
        receipt = batches.state(engine)["review_receipts"][KEY]
        self.assertIn("artifact_digest", receipt)
        hello = engine.worktree / "hello.py"
        os.chmod(hello, 0o664)                          # B
        window, hit = at_leave_write(sigint, after=written)
        with window, self.assertRaises(KeyboardInterrupt):
            self.engine(backend).resume(engine.run["id"])
        self.assertTrue(hit)
        stopped = batches.state(engine)
        self.assertEqual(statuses(stopped), ["running"])
        self.assertEqual(stopped["review_receipts"][KEY], receipt)
        on_disk = read_json(Path(stopped["final_history"][0]["artifact"]))["status"]
        self.assertEqual(on_disk != "running", written)
        os.chmod(hello, 0o644)                          # A again
        return engine, receipt

    def test_a_written_leave_stands_when_the_candidate_changes_back(self):
        """Review 1d2, blocker 1: a leave whose record file is written is a fact on any candidate. Back on A the
        approval the left attempt held never completes the run; A is reviewed again. Interrupted before the
        write, nothing was left: the original replays as A's review."""
        for written in (True, False):
            with self.subTest(written=written):
                self.setUp()
                backend = ScriptedBackend()
                engine, receipt = self.switched_back(backend, written=written)
                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                history, finals = result["final_history"], batches.calls(backend, kind="final_review")
                left = [(row["attempt"], row["dispatch_id"], row["status"])
                        for row in batches.events(engine, "final_attempt_left")]
                if written:
                    self.assertEqual(statuses(result), ["invalidated", "approved"])
                    self.assert_left(history[0], receipt, "invalidated")
                    self.assertEqual(history[0]["review"]["verdict"], "approve")
                    self.assertEqual(history[1]["snapshot_digest"], history[0]["snapshot_digest"])
                    self.assertNotEqual(history[1]["review_source"]["dispatch_id"], receipt["dispatch_id"])
                    self.assertEqual((len(finals), left), (2, [(1, receipt["dispatch_id"], "invalidated")]))
                else:
                    self.assertEqual(statuses(result), ["approved"])
                    self.assertEqual(history[0]["review_source"]["dispatch_id"], receipt["dispatch_id"])
                    self.assertEqual((len(finals), left), (1, []))

    def test_a_written_left_rejection_stands_when_the_candidate_changes_back(self):
        """Blocker 1, a rejection: the written leave keeps the veto as invalid_rejected and back on A the next call is
        the commander's decide, never a review of A. Interrupted before the write, the original replays as A's own
        rejection, which also leads to the decide."""
        for written in (True, False):
            with self.subTest(written=written):
                self.setUp()
                backend = ScriptedBackend(reject_first)
                engine, receipt = self.switched_back(backend, written=written)
                before = len(backend.calls)
                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                status = "invalid_rejected" if written else "rejected"
                self.assertEqual(statuses(result), [status, "approved"])
                after = backend.calls[before:]
                self.assertEqual((after[0]["role"], after[0]["kind"]), ("commander", "decide"))
                trigger = after[0]["packet"]["trigger"]["final_review"]
                self.assertEqual((trigger["attempt"], trigger["status"]), (1, status))
                history = result["final_history"]
                self.assertNotEqual(history[1]["snapshot_digest"], history[0]["snapshot_digest"])
                finals = batches.calls(backend, kind="final_review")
                self.assertEqual(len(finals), 2)
                self.assertEqual(batches.labels(finals[1]["packet"]["required_findings"]), ["found-on-a"])
                if written:
                    self.assert_left(history[0], receipt, "invalid_rejected")

    def test_a_record_file_that_only_looks_left_is_no_leave(self):
        """Only the exact bytes the leave writes, in a regular file, make it a fact: altered content under a left
        status, the exact bytes behind a link, or a FIFO are no evidence, and the receipt replays as A's review."""
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)

        def altered(path):
            value = read_json(path)
            value["reason"] = value["reason"] + " (edited)"
            write_json(path, value)

        def replaced(path, make):
            temporary = path.with_name("final.json.replaced")
            make(temporary)
            os.replace(temporary, path)

        def linked(path):
            copied = Path(outside.name) / f"{len(os.listdir(outside.name))}.json"
            copied.write_bytes(path.read_bytes())
            replaced(path, lambda temporary: temporary.symlink_to(copied))
        damages = (("altered", altered), ("link", linked), ("fifo", lambda path: replaced(path, os.mkfifo)))
        for name, damage in damages:
            with self.subTest(name):
                self.setUp()
                backend = ScriptedBackend()
                engine, receipt = self.switched_back(backend, written=True)
                path = Path(batches.state(engine)["final_history"][0]["artifact"])
                damage(path)
                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(statuses(result), ["approved"])
                self.assertEqual(result["final_history"][0]["review_source"]["dispatch_id"], receipt["dispatch_id"])
                self.assertEqual(len(batches.calls(backend, kind="final_review")), 1)
                self.assertEqual(batches.events(engine, "final_attempt_left"), [])
                self.assertFalse(path.is_symlink())
                self.assertEqual(read_json(path), result["final_history"][0])

    def test_a_candidate_change_while_leaving_stops_before_any_reuse(self):
        """Review 1d2, blocker 2: A approved and saved; B's first batch returned, unregistered; back on A the candidate
        becomes C while the leave writes. Nothing is reused for the entry snapshot: the run stops at candidate_changed
        with the leave saved, and completes only after C is reviewed, B's veto included."""
        for verdict in ("approve", "request_changes"):
            with self.subTest(verdict=verdict):
                self.setUp()
                phase = {"b": False}

                def change(backend, kwargs, packet, result):
                    if kwargs["kind"] == "final_review" and phase["b"]:
                        phase["b"] = False
                        if verdict == "request_changes":
                            result.update(verdict="request_changes", findings=[{**finding(), "id": "only-on-b"}])
                backend = ScriptedBackend(change)
                engine = self.engine(backend)
                with after_first_approval(), self.assertRaises(KeyboardInterrupt):
                    engine.start("Implement greet", batches.plan())
                hello = engine.worktree / "hello.py"
                os.chmod(hello, 0o664)                  # B
                phase["b"] = True
                with before_judgment(backend, 2), self.assertRaises(KeyboardInterrupt):
                    self.engine(backend).resume(engine.run["id"])
                receipt = batches.state(engine)["review_receipts"][KEY]
                os.chmod(hello, 0o644)                  # A
                window, hit = at_leave_write(lambda: os.chmod(hello, 0o646))     # C, while the leave writes
                with window, self.assertRaises(HarnessError) as caught:
                    self.engine(backend).resume(engine.run["id"])
                self.assertTrue(hit)
                self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]),
                                 ("workspace_mismatch", "candidate_changed"))
                stopped = batches.state(engine)
                left = "invalidated" if verdict == "approve" else "invalid_rejected"
                self.assertNotEqual(stopped["status"], "completed")
                self.assertEqual(batches.stop_of(stopped), ("workspace_mismatch", "candidate_changed"))
                self.assertEqual(statuses(stopped), ["approved", left])
                self.assertNotIn(KEY, stopped["review_receipts"])
                self.assert_left(stopped["final_history"][1], receipt, left)
                self.assertEqual(len(batches.calls(backend, kind="final_review")), 2)
                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                history = result["final_history"]
                self.assertEqual(statuses(result), ["approved", left, "approved"])
                self.assertEqual(len({record["snapshot_digest"] for record in history}), 3)
                finals = batches.calls(backend, kind="final_review")
                self.assertEqual(len(finals), 3)
                self.assertEqual(batches.labels(finals[2]["packet"]["required_findings"]),
                                 [] if verdict == "approve" else ["only-on-b"])

    def test_a_written_leave_enters_before_a_rejected_third_candidates_decide(self):
        """Review 1d2 r2: C rejected, A's review returned, the leave written on B, back on C. The scheduler derives
        C's decide only after the written leave is published: before the first model call the history holds the
        left attempt and A's receipt is consumed, and every later call is the one made after the leave was
        published on B. A left approval approves nothing."""
        for verdict in ("request_changes", "approve"):
            left = "invalid_rejected" if verdict == "request_changes" else "invalidated"
            ends = {}
            for published in (True, False):
                with self.subTest(verdict=verdict, published=published):
                    self.setUp()
                    backend = c_then_a(verdict)
                    engine = self.engine(backend)
                    rejected_on_c(self, engine, lambda: engine.start("Implement greet", batches.plan()))
                    hello, receipt = written_on_b(self, engine, backend, published=published)
                    os.chmod(hello, 0o644)                  # C
                    seen, change = [], backend.change

                    def observe(backend, kwargs, packet, result, change=change, engine=engine, seen=seen):
                        saved = batches.state(engine)
                        seen.append((statuses(saved), KEY in saved["review_receipts"]))
                        change(backend, kwargs, packet, result)
                    backend.change = observe
                    before = len(backend.calls)
                    result = self.engine(backend).resume(engine.run["id"])
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(seen[0], (["rejected", left], False))
                    after = backend.calls[before:]
                    self.assertEqual((after[0]["role"], after[0]["kind"]), ("commander", "decide"))
                    trigger = after[0]["packet"]["trigger"]["final_review"]
                    self.assertEqual((trigger["attempt"], trigger["status"]), (1, "rejected"))
                    self.assertEqual(statuses(result), ["rejected", left, "approved"])
                    self.assert_left(result["final_history"][1], receipt, left)
                    self.assertEqual([(row["attempt"], row["status"]) for row in batches.events(engine, "final_attempt_left")],
                                     [(2, left)])
                    ends[published] = call_trace(after)
                    self.assertEqual(ends[published][-1][3],
                                     ["found-1", "found-2"] if left == "invalid_rejected" else ["found-1"])
            with self.subTest(verdict=verdict, compared="calls after the resume on C"):
                self.assertEqual(ends.get(False), ends.get(True))

    def test_a_written_leave_publishes_the_same_record_on_every_candidate(self):
        """The written leave follows from the attempt, its saved mapping and its original alone: resumed on A, B or C
        from the same ledger, the scheduler publishes the same record before any model call, byte for byte the file
        the leave wrote."""
        for verdict in ("request_changes", "approve"):
            with self.subTest(verdict=verdict):
                self.setUp()
                backend = c_then_a(verdict)
                engine = self.engine(backend)
                rejected_on_c(self, engine, lambda: engine.start("Implement greet", batches.plan()))
                hello, receipt = written_on_b(self, engine, backend)
                state = engine.directory / "state.json"
                ledger = state.read_bytes()
                artifact = Path(batches.state(engine)["final_history"][1]["artifact"])
                written = artifact.read_bytes()
                published = {}
                for name, mode in (("A", 0o664), ("B", 0o646), ("C", 0o644)):
                    state.write_bytes(ledger)
                    os.chmod(hello, mode)
                    resumed = self.engine(backend)
                    calls = len(backend.calls)
                    with batches.interrupt_at_event(resumed, "final_attempt_left"), self.assertRaises(KeyboardInterrupt):
                        resumed.resume(engine.run["id"])
                    saved = batches.state(engine)
                    self.assertEqual(len(backend.calls), calls)
                    self.assertNotIn(KEY, saved["review_receipts"])
                    self.assert_left(saved["final_history"][1], receipt, "invalid_rejected" if verdict == "request_changes"
                                     else "invalidated")
                    published[name] = ((dump(saved["final_history"][1]) + "\n").encode("utf-8"), artifact.read_bytes())
                self.assertEqual(published, {name: (written, written) for name in "ABC"})

    def test_a_written_leave_enters_before_the_final_context_preflight(self):
        """With an environment update on record, resume's first reader of the whole-run history is the context
        preflight, before any step is derived: the written leave is published before it reads."""
        backend = c_then_a("request_changes")
        change = backend.change

        def blocked_first(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and len(batches.calls(backend, role="worker")) == 1:
                result.update(status="blocked", blocker_kind="environment", blockers=["Dependency unavailable"])
            else:
                change(backend, kwargs, packet, result)
        backend.change = blocked_first
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", batches.plan())
        source = next(d["id"] for d in engine.run["dispatches"] if d["role"] == "worker")
        update = self.repo / "operator-update.json"
        write_json(update, {"schema_version": 1, "task_id": "greet", "blocker_dispatch_id": source,
                            "summary": "Operator installed the required dependency", "checks": []})
        resumed = self.engine(backend)
        rejected_on_c(self, resumed, lambda: resumed.resume(engine.run["id"], environment_update=update))
        self.assertTrue(batches.state(engine)["environment_updates"])
        hello, receipt = written_on_b(self, engine, backend)
        os.chmod(hello, 0o644)                          # C
        seen, preflight = [], PacketBuilder.preflight_final_context

        def observed(service, *args, **kwargs):
            seen.append((statuses(service.context.run), KEY in service.context.run["review_receipts"]))
            return preflight(service, *args, **kwargs)
        with patch.object(PacketBuilder, "preflight_final_context", autospec=True, side_effect=observed):
            result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(seen[0], (["rejected", "invalid_rejected"], False))
        self.assertEqual(statuses(result), ["rejected", "invalid_rejected", "approved"])
        self.assert_left(result["final_history"][1], receipt, "invalid_rejected")

    def test_the_regular_flow_reads_no_record_file(self):
        """The check before each derivation is decided from the ledger in memory: an uninterrupted run reads no
        record file; a review interrupted before its judgment and replayed on its snapshot reads its own once."""
        reads, regular = [], finalization.regular_bytes

        def counted(path):
            reads.append(Path(path).name)
            return regular(path)
        with patch.object(finalization, "regular_bytes", side_effect=counted):
            result = self.engine(ScriptedBackend()).start("Implement greet", batches.plan())
            self.assertEqual((result["status"], reads), ("completed", []))
            self.setUp()
            backend = ScriptedBackend()
            engine = self.engine(backend)
            with before_judgment(backend, 1), self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", batches.plan())
            self.assertEqual(reads, [])
            result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual((result["status"], statuses(result), reads), ("completed", ["approved"], ["final.json"]))


if __name__ == "__main__":
    unittest.main()
