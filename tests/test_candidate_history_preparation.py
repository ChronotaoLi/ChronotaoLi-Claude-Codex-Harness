"""Changed-candidate preparation leaves the registered history alone until its transition."""
import copy
import errno
import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.state_files import LockedDirectory, StateFiles
from harness.task_lifecycle import TaskLifecycle
from harness.util import EvidenceError, HarnessError
import test_review_transitions as scan


def observe_artifact_reads(artifact, observe):
    original = StateFiles.read

    def read(files, components, **options):
        path = Path(files.path).joinpath(*files.prefix, *components)
        if path == artifact:
            observe(path)
        return original(files, components, **options)

    return patch.object(StateFiles, "read", read)


class CandidateHistoryPreparationTests(scan.TransitionCase):
    def stopped_review(self, history, *, returned=True):
        backend = scan.batches.scripted([1] if history == "registered" else [])
        engine = self.engine(backend)
        target = 2 if history == "registered" else 1

        if returned:
            def ready(run):
                receipt = run["review_receipts"].get(scan.TASK_KEY, {})
                return "artifact_digest" in receipt and len(scan.batches.calls(backend, kind="review")) == target

            window, _ = scan.after_save(engine, ready, scan.interrupt)
            with window, self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", scan.batches.plan())
        else:
            def fail(backend, kwargs, packet):
                if kwargs["kind"] == "review":
                    backend.before_result = None
                    raise HarnessError("review returned no artifact")

            backend.before_result = fail
            with self.assertRaisesRegex(HarnessError, "review returned no artifact"):
                engine.start("Implement greet", scan.batches.plan())
        if history == "empty":
            engine.run["review_history"]["greet"] = []
            engine.context.save()
        path = engine.worktree / "hello.py"
        path.write_bytes(path.read_bytes() + b"# changed while stopped\n")
        return engine, backend

    def test_preparation_is_repeatable_and_returns_independent_evidence(self):
        for history in ("missing", "empty", "registered"):
            with self.subTest(history=history):
                engine, _ = self.stopped_review(history)
                task = engine.services.tasks
                before = copy.deepcopy(engine.run)
                pending = engine.run["pending_task"]
                receipt = engine.run["review_receipts"][scan.TASK_KEY]
                first = task.read_invalidated_review(pending, scan.TASK_KEY)
                second = task.read_invalidated_review(pending, scan.TASK_KEY)
                self.assertEqual(first, second)
                record = task.invalidated_review(pending, receipt, *first)
                expected = task.invalidated_review(pending, receipt, *second)
                self.assertEqual(record, expected)
                first[2]["artifact"] = "changed by caller"
                record["review_source"].clear()
                record["required_findings"].clear()
                record["review"].clear()
                self.assertEqual(task.invalidated_review(pending, receipt, *second), expected)
                self.assertEqual(engine.run, before)
                self.assertEqual(scan.batches.state(engine), before)
                if history == "registered":
                    # Existing registration short-circuits before mapping validation or artifact reads.
                    receipt["dispatch_id"] = before["review_history"]["greet"][0]["dispatch_id"]
                    duplicate = copy.deepcopy(engine.run)
                    self.assertIsNone(task.read_invalidated_review(pending, scan.TASK_KEY))
                    self.assertEqual(engine.run, duplicate)

    def test_before_and_inside_transition_keep_exact_history_and_resume_once(self):
        for history in ("missing", "empty", "registered"):
            endings = []
            for event in (0, 1):
                with self.subTest(history=history, event=event):
                    engine, backend = self.stopped_review(history)
                    saved = scan.batches.state(engine)
                    old_history = saved["review_history"]
                    receipt = saved["review_receipts"][scan.TASK_KEY]
                    active = self.engine(backend)
                    with scan.sigint_in_transition(TaskLifecycle.continue_task, lambda _: True, event,
                                                   "review_receipts", keep=True) as fired, \
                            self.assertRaises(KeyboardInterrupt):
                        active.resume(engine.run["id"])
                    self.assertEqual(fired["ledgers"][0]["review_history"], old_history)
                    self.assertEqual(fired["ledgers"][0]["review_receipts"][scan.TASK_KEY], receipt)
                    stopped = scan.batches.state(engine)
                    self.assertEqual(scan.ledger(stopped), fired["ledgers"][0 if event == 0 else -1])
                    if event == 0:
                        self.assertEqual(stopped["review_history"], old_history)
                        self.assertEqual(stopped["review_receipts"][scan.TASK_KEY], receipt)
                    else:
                        self.assertNotIn(scan.TASK_KEY, stopped["review_receipts"])
                        self.assertEqual(stopped["review_history"]["greet"][:-1], old_history.get("greet", []))
                        kept = stopped["review_history"]["greet"][-1]
                        self.assertEqual(kept["review_source"], receipt)
                        self.assertEqual(kept["status"], "invalidated")
                    before = len(backend.calls)
                    result = self.engine(backend).resume(engine.run["id"])
                    self.assertEqual(result["status"], "completed")
                    self.assertNotIn(scan.TASK_KEY, result["review_receipts"])
                    dispatch = receipt["dispatch_id"]
                    self.assertEqual(sum(scan.registrations(result), []).count(dispatch), 1)
                    self.assertEqual([row["id"] for row in result["dispatches"]].count(dispatch), 1)
                    self.assertEqual(len(scan.batches.calls(backend, kind="review")), 3 if history == "registered" else 2)
                    endings.append(scan.signature(backend)[before:])
            self.assertEqual(*endings)

    def test_missing_artifact_finishes_with_empty_history_and_no_registration(self):
        engine, backend = self.stopped_review("missing", returned=False)
        before = copy.deepcopy(engine.run)
        self.assertFalse(Path(before["review_receipts"][scan.TASK_KEY]["artifact"]).exists())
        self.assertIsNone(engine.services.tasks.read_invalidated_review(before["pending_task"], scan.TASK_KEY))
        self.assertEqual(engine.run, before)
        active = self.engine(backend)
        with scan.sigint_in_transition(TaskLifecycle.continue_task, lambda _: True, 0,
                                      "review_receipts", keep=True) as fired, \
                self.assertRaises(KeyboardInterrupt):
            active.resume(engine.run["id"])
        self.assertEqual(fired["ledgers"][0]["review_history"], {})
        self.assertEqual(scan.ledger(scan.batches.state(engine)), fired["ledgers"][0])
        active = self.engine(backend)
        window, _ = scan.after_save(active, lambda run: "pending_task" not in run, scan.interrupt)
        with window, self.assertRaises(KeyboardInterrupt):
            active.resume(engine.run["id"])
        stopped = scan.batches.state(engine)
        self.assertEqual(stopped["review_history"], {"greet": []})
        self.assertNotIn(scan.TASK_KEY, stopped["review_receipts"])
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(scan.batches.calls(backend, kind="review")), 2)
        self.assertNotIn(before["review_receipts"][scan.TASK_KEY]["dispatch_id"], sum(scan.registrations(result), []))

    def unbound_review(self):
        engine, backend = self.stopped_review("missing")
        receipt = engine.run["review_receipts"][scan.TASK_KEY]
        digest = receipt.pop("artifact_digest")
        engine.context.save()
        artifact = Path(receipt["artifact"])
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), digest)
        return engine, backend, artifact, digest

    def test_existing_digest_mismatch_leaves_history_and_receipt(self):
        engine, backend = self.stopped_review("missing")
        before = scan.batches.state(engine)
        artifact = Path(before["review_receipts"][scan.TASK_KEY]["artifact"])
        artifact.write_bytes(artifact.read_bytes() + b" ")
        with self.assertRaises(EvidenceError):
            self.engine(backend).resume(engine.run["id"])
        stopped = scan.batches.state(engine)
        self.assertEqual(stopped["review_history"], before["review_history"])
        self.assertEqual(stopped["review_receipts"], before["review_receipts"])
        self.assertEqual(len(scan.batches.calls(backend, kind="review")), 1)

    def test_unbound_receipt_with_changed_batch_is_refused_before_read(self):
        engine, backend, artifact, _ = self.unbound_review()
        engine.run["pending_task"]["required_findings"] = [{"key": "changed"}]
        engine.context.save()
        before = copy.deepcopy(engine.run)
        def unread(path):
            self.assertNotEqual(path, artifact)

        with observe_artifact_reads(artifact, unread), self.assertRaises(EvidenceError):
            self.engine(backend).resume(engine.run["id"])
        stopped = scan.batches.state(engine)
        self.assertEqual(stopped["review_history"], before["review_history"])
        self.assertEqual(stopped["review_receipts"], before["review_receipts"])

    def test_failed_first_digest_save_stops_before_parsing_and_can_resume(self):
        engine, backend, _, digest = self.unbound_review()
        active = self.engine(backend)
        original = LockedDirectory.replace
        writes = []

        def replace(locked, name, data):
            receipt = active.run["review_receipts"].get(scan.TASK_KEY, {})
            if name == "state.json" and receipt.get("artifact_digest") == digest:
                writes.append(active.run["status"])
                if writes == ["running"]:
                    raise OSError(errno.EIO, "binding write failed")
            return original(locked, name, data)

        with patch.object(LockedDirectory, "replace", replace), \
                patch.object(TaskLifecycle, "invalidated_review", autospec=True) as prepare, \
                self.assertRaisesRegex(OSError, "binding write failed"):
            active.resume(engine.run["id"])
        prepare.assert_not_called()
        self.assertEqual(writes, ["running", "blocked"])
        stopped = scan.batches.state(engine)
        self.assertEqual(stopped["review_history"], {})
        self.assertIn("pending_task", stopped)
        receipt = stopped["review_receipts"][scan.TASK_KEY]
        self.assertEqual(receipt["artifact_digest"], digest)
        self.assertEqual(scan.batches.stop_of(stopped), ("runtime_failure", "io"))
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(scan.registrations(result), []).count(receipt["dispatch_id"]), 1)
        self.assertNotIn(scan.TASK_KEY, result["review_receipts"])
        self.assertEqual(len(scan.batches.calls(backend, kind="review")), 2)

    def test_first_digest_save_survives_interrupt_and_refuses_replaced_artifact(self):
        engine, backend, artifact, digest = self.unbound_review()
        active = self.engine(backend)
        window, _ = scan.after_save(
            active, lambda run: run["review_receipts"].get(scan.TASK_KEY, {}).get("artifact_digest") == digest,
            scan.interrupt)
        reads = []
        with observe_artifact_reads(artifact, reads.append), window, self.assertRaises(KeyboardInterrupt):
            active.resume(engine.run["id"])
        self.assertEqual(reads, [artifact])
        before = scan.batches.state(engine)
        self.assertEqual(before["review_history"], {})
        self.assertEqual(before["review_receipts"][scan.TASK_KEY]["artifact_digest"], digest)
        artifact.write_bytes(artifact.read_bytes() + b" ")
        with self.assertRaises(EvidenceError):
            self.engine(backend).resume(engine.run["id"])
        stopped = scan.batches.state(engine)
        self.assertEqual(stopped["review_history"], {})
        self.assertEqual(stopped["review_receipts"], before["review_receipts"])
        self.assertEqual(len(scan.batches.calls(backend, kind="review")), 1)


if __name__ == "__main__":
    unittest.main()
