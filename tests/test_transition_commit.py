"""A task approval's ledger barrier owns its save through deferred cancellation."""

from contextlib import contextmanager
import errno
import os
import signal
from unittest.mock import patch

from harness import git, state_files
from harness.process_runtime import defer_cleanup_signals
from harness.run_store import LEDGER, RunStore
from harness.state_files import LockedDirectory
from harness.workflow import Workflow
from test_harness import after_save
import test_review_transitions as transitions


class TransitionCommitTests(transitions.TransitionCase):
    def exercise_approval(self, cut):
        backend = transitions.ScriptedBackend()
        engine = self.engine(backend)
        returned, _ = after_save(
            engine, lambda run: "artifact_digest" in run["review_receipts"].get(transitions.TASK_KEY, {}),
            transitions.interrupt)
        with returned, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", transitions.batches.plan())
        receipt = transitions.batches.state(engine)["review_receipts"][transitions.TASK_KEY]
        before_calls = transitions.signature(backend)
        resumed = self.engine(backend)
        context = resumed.context
        transition = context.transition
        replace = LockedDirectory.replace
        read = LockedDirectory.read
        publish = RunStore._publish_candidate
        stop = resumed.services.workflow.stop
        publications = []
        disk = {}
        rereads = []
        live_revisions = []
        write_failed = False
        failure = OSError(errno.EIO, "approval publication")
        body_error = KeyboardInterrupt("synchronous approval body") if cut == "synchronous" else failure
        abort_body = cut in {"synchronous", "body error"}
        write_error = cut in {"before replace", "ambiguous replace"}

        @contextmanager
        def approval_transition():
            with transition():
                yield
                if abort_body:
                    raise body_error
                if cut != "during replace":
                    os.kill(os.getpid(), signal.SIGINT)

        def record_publication(store, locked, candidate, data):
            publications.append(candidate)
            return publish(store, locked, candidate, data)

        def trigger_replace(locked, name, data):
            nonlocal write_failed
            candidate = publications[-1]
            if (name != LEDGER or "pending_acceptance" not in candidate or candidate.get("stop")
                    or "before replace" in disk):
                return replace(locked, name, data)
            disk["before replace"] = transitions.batches.state(resumed)
            if cut != "before replace":
                result = replace(locked, name, data)
                self.assertEqual(result.state, "written")
            if cut == "during replace":
                os.kill(os.getpid(), signal.SIGINT)
            if write_error:
                write_failed = True
                raise failure
            return result

        def fail_immediate_reread(locked, name, **kwargs):
            nonlocal write_failed
            if name == LEDGER and write_failed:
                write_failed = False
                if cut == "ambiguous replace":
                    rereads.append(locked.identity)
                    return state_files.Outcome(
                        "failed", str(resumed.directory / LEDGER),
                        state_files.Step(2, LEDGER, "read", True), errno=errno.EIO)
            return read(locked, name, **kwargs)

        def record_stop(error):
            disk["before stop"] = transitions.batches.state(resumed)
            live_revisions.append(context.run["store_revision"])
            result = stop(error)
            self.assertIs(result, Workflow.WRITTEN)
            return result

        def resume_interrupted():
            original = body_error if abort_body else failure if write_error else None
            with self.assertRaises(type(original) if original is not None else KeyboardInterrupt) as caught:
                resumed.resume(engine.run["id"])
            if original is not None:
                self.assertIs(caught.exception, original)

        with (patch.object(context, "transition", approval_transition),
              patch.object(RunStore, "_publish_candidate", record_publication),
              patch.object(LockedDirectory, "replace", trigger_replace),
              patch.object(LockedDirectory, "read", fail_immediate_reread),
              patch.object(resumed.services.workflow, "stop", record_stop)):
            if cut == "outer pending":
                with self.assertRaises(KeyboardInterrupt) as caught:
                    with defer_cleanup_signals() as outer:
                        os.kill(os.getpid(), signal.SIGTERM)
                        resume_interrupted()
                        self.assertEqual(outer, [signal.SIGTERM])
                self.assertIn(str(signal.SIGTERM.value), str(caught.exception))
            else:
                resume_interrupted()

        if cut == "ambiguous replace":
            self.assertEqual(rereads, [context.store.directory_identity])
        else:
            self.assertEqual(rereads, [])
        saved = transitions.batches.state(resumed)
        if abort_body:
            self.assertNotIn("before replace", disk)
            self.assertNotIn("pending_acceptance", disk["before stop"])
            self.assertEqual(saved["review_receipts"][transitions.TASK_KEY], receipt)
            self.assertEqual(saved["store_revision"], disk["before stop"]["store_revision"] + 1)
            self.assertEqual([row for row in publications if "pending_acceptance" in row], [])
        else:
            attempted, = [row for row in publications if "pending_acceptance" in row and not row.get("stop")]
            self.assertEqual(disk["before replace"]["review_receipts"][transitions.TASK_KEY], receipt)
            self.assertEqual(attempted["store_revision"], disk["before replace"]["store_revision"] + 1)
            self.assertEqual(disk["before stop"], disk["before replace"] if cut == "before replace" else attempted)
            live_before_stop = disk["before replace"]["store_revision"] if write_error else attempted["store_revision"]
            self.assertEqual(live_revisions, [live_before_stop])
            self.assertNotIn(transitions.TASK_KEY, saved["review_receipts"])
            self.assertEqual(saved["pending_acceptance"], attempted["pending_acceptance"])
            self.assertEqual(saved["store_revision"], disk["before stop"]["store_revision"] + 1)
        if write_error or cut == "body error":
            self.assert_stop(saved, "runtime_failure", "io")
        else:
            self.assert_stop(saved, "interrupted", "signal")
        self.assertEqual(transitions.signature(backend), before_calls)
        fresh = self.engine(backend)
        completed = fresh.resume(engine.run["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(git.head(fresh.worktree), completed["expected_head"])
        self.assertEqual(transitions.signature(backend)[len(before_calls):], [("reviewer", "final_review", "final", 0)])
        review, = completed["review_history"]["greet"]
        self.assertEqual(review["review_source"]["dispatch_id"], receipt["dispatch_id"])

    def test_determinate_write_failure_preserves_the_io_error_with_a_held_signal(self):
        self.exercise_approval("before replace")

    def test_held_signal_does_not_mask_an_ambiguous_write(self):
        self.exercise_approval("ambiguous replace")

    def test_signal_during_replace_leaves_an_adopted_approval(self):
        self.exercise_approval("during replace")

    def test_held_outer_signal_is_delivered_once_after_the_inner_transition(self):
        self.exercise_approval("outer pending")

    def test_synchronous_interrupt_does_not_publish_the_body(self):
        self.exercise_approval("synchronous")

    def test_body_error_does_not_publish_the_body(self):
        self.exercise_approval("body error")
