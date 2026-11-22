import copy
import errno
import json
import hashlib
import os
from pathlib import Path
from unittest.mock import patch

from harness import git, state_files
from harness.task_lifecycle import TaskLifecycle
from harness.util import EvidenceError, HarnessError, read_json, write_json
import test_review_batches as batches  # by module: its TestCases imported by name would be collected again here
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT, crash_after_save, review_bound, walk_exit
from test_provider_failures import FifoWatchdog
from test_runtime_changes import finding, invalid_rejection, revised, trigger_kind


class InvalidReviewRecoveryTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def assert_raw_evidence(self, record):
        raw = Path(record["returned_artifact"]).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), record["artifact_digest"])
        result = read_json(Path(record["returned_artifact"]))
        self.assertEqual(result["dispatch_id"], record["dispatch_id"])
        self.assertEqual(result["findings"], record["review"]["findings"])

    def test_invalid_final_rejection_persists_without_same_snapshot_reroll(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                invalid_rejection(result)

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        # The demo commander asks about the rejection; it is never re-reviewed.
        with self.assertRaisesRegex(HarnessError, "等待操作者决定：task_id=final"):
            engine.start("Implement greet", self.plan())
        record = engine.run["final_history"][-1]
        self.assertEqual(record["status"], "invalid_rejected")
        self.assertEqual(record["phase"], "finished")
        self.assertIn("阻断 finding 必须有证据", record["validation_error"])
        self.assert_raw_evidence(record)
        seen = [c for c in backend.calls if c["kind"] == "decide"][-1]["packet"]["trigger"]["final_review"]
        self.assertEqual((seen["status"], seen["attempt"]), (record["status"], record["attempt"]))
        calls = copy.deepcopy(backend.calls)
        for options in ({}, {"reverify": True}):
            resumed = self.engine(backend)
            with self.assertRaises(HarnessError):
                resumed.resume(engine.run["id"], **options)
            self.assertEqual(len(resumed.run["final_history"]), 1)
            self.assertEqual(backend.calls, calls)

    def test_approve_with_invalid_blocking_finding_is_still_rejection(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result.update(verdict="approve", findings=[{**finding(), "evidence": []}],
                              approved_memory_keys=["nonexistent"])

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        record = engine.run["final_history"][-1]
        self.assertEqual(record["status"], "invalid_rejected")
        self.assertEqual(record["review"]["verdict"], "approve")
        self.assert_raw_evidence(record)
        self.assertNotEqual(engine.run["status"], "completed")

    def test_invalid_task_rejection_reaches_worker_and_independent_review(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and sum(c["kind"] == "review" for c in backend.calls) == 1:
                invalid_rejection(result)
                result["approved_memory_keys"] = ["unapproved"]

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        record = result["review_history"]["greet"][0]
        self.assertEqual(record["status"], "invalid_rejected")
        self.assert_raw_evidence(record)
        workers = [call for call in backend.calls if call["role"] == "worker"]
        reviews = [call for call in backend.calls if call["kind"] == "review"]
        for call in (workers[1], reviews[1]):
            prior = call["packet"]["prior_reviews"][0]
            self.assertEqual(prior["review"]["findings"], record["review"]["findings"])
            self.assertEqual(prior["returned_artifact"], record["returned_artifact"])
            self.assertEqual(prior["status"], "invalid_rejected")
        self.assertEqual(len(reviews), 2)
        self.assertEqual(result["agent_dispatches"], len(backend.requests))
        self.assertEqual(engine.memory.search("unapproved"), [])

    def test_noop_final_correction_keeps_invalid_rejection_until_changed_fix(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and sum(c["kind"] == "final_review" for c in backend.calls) == 1:
                invalid_rejection(result)
            elif trigger_kind(kwargs, packet) == "final":
                number = sum(c["kind"] == "decide" for c in backend.calls)
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id=f"correct{number}", depends_on=["greet"])
                revised(result, packet, "Resolve recorded findings", new_tasks=[task])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct2":
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# Independently reviewed correction\n")

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual([r["status"] for r in result["final_history"]], ["invalid_rejected", "approved"])
        self.assertEqual(sum(c["kind"] == "final_review" for c in backend.calls), 2)
        self.assertEqual(sum(c["kind"] == "decide" for c in backend.calls), 2)
        accepted = result["accepted"]
        self.assertEqual(accepted["greet"]["commit"], accepted["correct1"]["commit"])
        self.assertNotEqual(accepted["greet"]["commit"], accepted["correct2"]["commit"])
        self.assert_raw_evidence(result["final_history"][0])

    def test_whole_run_memory_approval_is_discarded_without_redraw(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result["approved_memory_keys"] = ["nonexistent"]

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 1)
        self.assertEqual([record["status"] for record in result["final_history"]], ["approved"])
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        discarded = [e for e in events if e["event"] == "memory_proposals_discarded"]
        self.assertEqual([(e["task_id"], e["discarded_keys"]) for e in discarded], [("final", ["nonexistent"])])

    def test_returned_invalid_rejection_survives_receipt_write_interruption(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                invalid_rejection(result)

        backend = RecordingBackend(change)
        engine = self.engine(backend)

        def interrupt(path, value):
            write_json(path, value)
            if Path(path).name == "returned-result.json" and value.get("task_id") == "final":
                raise KeyboardInterrupt()

        with patch("harness.dispatch.write_json", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        calls = copy.deepcopy(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定：task_id=final"):
            resumed.resume(engine.run["id"])
        # The returned rejection is replayed, not redrawn; only the decision is new.
        self.assertEqual([c["kind"] for c in backend.calls[len(calls):]], ["decide"])
        self.assertEqual(resumed.run["final_history"][-1]["status"], "invalid_rejected")
        self.assert_raw_evidence(resumed.run["final_history"][-1])

    def pending_invalid_task_review(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and sum(c["kind"] == "review" for c in backend.calls) == 1:
                invalid_rejection(result)

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        # The controller dies right after the returned invalid rejection is bound.
        crash, state = crash_after_save(engine, review_bound("review"))
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        self.assertTrue(state["fired"])
        run_id = engine.run["id"]
        engine = self.engine(backend)
        engine.load_run(run_id)
        self.assertEqual(engine.run["accepted"], {})
        return engine, backend

    def test_bound_invalid_rejection_is_reused_before_rework(self):
        engine, backend = self.pending_invalid_task_review()
        resumed = self.engine(backend)
        result = resumed.resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(c["kind"] == "review" for c in backend.calls), 2)
        self.assertEqual(result["review_history"]["greet"][0]["status"], "invalid_rejected")

    def test_changed_candidate_preserves_unconsumed_invalid_review_evidence(self):
        engine, backend = self.pending_invalid_task_review()
        receipt = copy.deepcopy(engine.run["review_receipts"]["reviewer:review:greet"])
        with (engine.worktree / "hello.py").open("a") as output:
            output.write("# Candidate changed while policy service was unavailable\n")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        record = result["review_history"]["greet"][0]
        self.assertEqual(record["status"], "invalidated")
        self.assertEqual(record["artifact"], receipt["artifact"])
        self.assertEqual(record["artifact_digest"], receipt["artifact_digest"])
        self.assertEqual(len(record["review"]["findings"]), 1)
        worker = [c for c in backend.calls if c["role"] == "worker"][1]
        self.assertEqual(worker["packet"]["prior_reviews"][0]["review"]["findings"], record["review"]["findings"])

    def test_changed_candidate_cannot_hide_tampered_review_evidence(self):
        engine, backend = self.pending_invalid_task_review()
        receipt = engine.run["review_receipts"]["reviewer:review:greet"]
        Path(receipt["artifact"]).write_text('{}\n')
        with (engine.worktree / "hello.py").open("a") as output:
            output.write("# Candidate changed\n")
        calls = copy.deepcopy(backend.calls)
        with self.assertRaisesRegex(HarnessError, "审阅原始结果已改变"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)
        self.assertEqual(git.head(engine.worktree), engine.run["expected_head"])

    def returned_second_review(self):
        """The run dies after a second task review returned and was bound by its digest, a blocking finding open."""
        backend = batches.scripted([1])
        engine = self.engine(backend)
        finish = TaskLifecycle._finish_review

        def interrupt(service, *args):
            if len(batches.calls(backend, kind="review")) == 2:
                raise KeyboardInterrupt()
            return finish(service, *args)
        with patch.object(TaskLifecycle, "_finish_review", autospec=True, side_effect=interrupt), \
                self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        before = read_json(engine.directory / "state.json")
        receipt = before["review_receipts"]["reviewer:review:greet"]
        self.assertIn("artifact_digest", receipt)
        self.assertTrue(before["pending_task"]["required_findings"])
        with (engine.worktree / "hello.py").open("a", encoding="utf-8") as stream:
            stream.write("# Candidate changed after the review returned\n")
        return backend, engine, before, Path(receipt["artifact"])

    def stopped_on_changed_candidate(self, damage):
        """The returned original is damaged by ``damage(path)`` and the candidate changed; ``resume`` stops.
        Returns the backend, the engine, the ledger before the resume, the original's path, the error, and the
        ledger after."""
        backend, engine, before, original = self.returned_second_review()
        damage(original)
        calls = copy.deepcopy(backend.calls)
        with self.assertRaises(BaseException) as raised:
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)
        after = read_json(engine.directory / "state.json")
        for key in ("review_receipts", "review_history", "pending_task", "feedback", "attempts"):
            self.assertEqual(after.get(key), before.get(key), key)
        return backend, engine, before, original, raised.exception, after

    def test_a_missing_or_replaced_original_stops_as_evidence_naming_its_dispatch(self):
        for layout in ("deleted", "a directory"):
            with self.subTest(layout=layout):
                if layout != "deleted":
                    self.setUp()
                backend, engine, before, original, _, after = self.stopped_on_changed_candidate(
                    lambda path: (path.unlink(), path.mkdir() if layout == "a directory" else None))
                dispatch_id = before["review_receipts"]["reviewer:review:greet"]["dispatch_id"]
                self.assertEqual((after["stop"]["kind"], after["stop"]["reason"], after["stop"]["source"]),
                                 ("evidence", "runtime_integrity", {"artifact": str(original), "dispatch_id": dispatch_id}))
                walk_exit(self, engine.directory, ("evidence", "runtime_integrity"), backend=backend, choose=1,
                          expect_status="abandoned")

    def test_changed_original_bytes_name_the_dispatch_and_resume_after_restoring(self):
        kept = {}
        backend, engine, before, original, _, after = self.stopped_on_changed_candidate(
            lambda path: (kept.update(raw=path.read_bytes()), path.write_text("{}\n")))
        dispatch_id = before["review_receipts"]["reviewer:review:greet"]["dispatch_id"]
        self.assertEqual((after["stop"]["kind"], after["stop"]["reason"], after["stop"]["source"]),
                         ("evidence", "runtime_integrity", {"artifact": str(original), "dispatch_id": dispatch_id}))
        original.write_bytes(kept["raw"])
        result = walk_exit(self, engine.directory, ("evidence", "runtime_integrity"), backend=backend,
                           expect_status="completed")
        self.assertIn(("invalidated", dispatch_id),
                      [(record.get("status"), record.get("dispatch_id")) for record in result["review_history"]["greet"]])

    def test_an_unreadable_original_is_local_io_and_keeps_the_receipt(self):
        for phase in ("probe", "read"):
            with self.subTest(phase=phase):
                if phase == "read":
                    self.setUp()
                backend, engine, before, original = self.returned_second_review()
                key = "reviewer:review:greet"
                receipt = before["review_receipts"][key]
                if phase == "probe":
                    del receipt["artifact_digest"]
                    write_json(engine.directory / "state.json", before)

                hits = []
                identity = original.stat()
                parent = original.parent.stat()
                stat, pread = os.stat, os.pread

                def failed_at_probe(name, *args, **kwargs):
                    fd = kwargs.get("dir_fd")
                    if fd is not None and name == original.name and os.path.samestat(os.fstat(fd), parent):
                        hits.append(phase)
                        raise OSError(errno.EIO, "synthetic probe fault")
                    return stat(name, *args, **kwargs)

                def failed_at_read(fd, length, offset):
                    if os.path.samestat(os.fstat(fd), identity):
                        hits.append(phase)
                        raise OSError(errno.EIO, "synthetic read fault")
                    return pread(fd, length, offset)

                calls = copy.deepcopy(backend.calls)
                operation, fault = ("stat", failed_at_probe) if phase == "probe" else ("pread", failed_at_read)
                with patch.object(state_files.os, operation, fault), self.assertRaises(state_files.StateReadError) as raised:
                    self.engine(backend).resume(engine.run["id"])
                self.assertEqual(hits, [phase])
                self.assertEqual(raised.exception.outcome.errno, errno.EIO)
                after = read_json(engine.directory / "state.json")
                stop = after["stop"]
                self.assertEqual((stop["kind"], stop["reason"], stop["source"], stop["data"]["error_type"]),
                                 ("runtime_failure", "io", {}, "OSError"))
                for key in ("review_receipts", "review_history", "pending_task", "agent_dispatches"):
                    self.assertEqual(after[key], before[key])
                self.assertEqual(backend.calls, calls)
                self.assertEqual(batches.calls(backend, kind="review"), [c for c in calls if c["kind"] == "review"])

                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                self.assertIn(("invalidated", receipt["dispatch_id"], hashlib.sha256(original.read_bytes()).hexdigest()),
                              [(record.get("status"), record.get("dispatch_id"), record.get("artifact_digest"))
                               for record in result["review_history"]["greet"]])

    def test_bound_review_refuses_a_link_or_fifo_before_invalidating_the_candidate(self):
        for layout in ("link", "fifo"):
            with self.subTest(layout=layout):
                if layout == "fifo":
                    self.setUp()
                watchdog = None

                def damage(path):
                    nonlocal watchdog
                    raw = path.read_bytes()
                    path.unlink()
                    if layout == "link":
                        target = path.with_name("ordinary-copy.json")
                        target.write_bytes(raw)
                        path.symlink_to(target)
                    else:
                        os.mkfifo(path)
                        watchdog = FifoWatchdog(path, bound=5)

                try:
                    _, _, before, original, error, after = self.stopped_on_changed_candidate(damage)
                finally:
                    if watchdog is not None:
                        self.assertFalse(watchdog.stop())
                self.assertIsInstance(error, EvidenceError)
                self.assertEqual((after["stop"]["kind"], after["stop"]["reason"], after["stop"]["source"]),
                                 ("evidence", "runtime_integrity", {}))
                self.assertEqual(after["agent_dispatches"], before["agent_dispatches"])
                if layout == "link":
                    self.assertTrue(original.is_symlink())

    def test_a_reviewing_gate_without_its_mapping_is_an_invariant_stop(self):
        backend, engine, before, original = self.returned_second_review()
        del before["pending_task"]["required_findings"]
        write_json(engine.directory / "state.json", before)
        calls = copy.deepcopy(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)
        after = read_json(engine.directory / "state.json")
        self.assertEqual((after["stop"]["kind"], after["stop"]["reason"]), ("runtime_failure", "invariant"))
        self.assertEqual((after["review_receipts"], after["pending_task"]), (before["review_receipts"], before["pending_task"]))

    def test_version_five_ledger_requires_original_runtime(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        engine.start("Implement greet", self.plan(), pause_after_plan=True)
        engine.run["schema_version"] = 5
        engine.context.save()
        with self.assertRaisesRegex(HarnessError, "台账版本 5 不支持"):
            self.engine(backend).resume(engine.run["id"], approve_plan=True)
        self.assertEqual(backend.calls, [])
