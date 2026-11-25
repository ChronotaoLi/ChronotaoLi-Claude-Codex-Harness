"""Exercise the controller with owned POSIX signals and local ledger state."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harness import cli, interrupts, quota, stops
from harness.process_runtime import defer_cleanup_signals
from harness.run_lifecycle import RunLifecycle
from harness.run_store import RunStore
from harness.util import HarnessError


def send(signum=signal.SIGINT):
    signal.raise_signal(signum)


class InterruptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name))
        self.output = StringIO()
        stderr = redirect_stderr(self.output)
        stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)

    def install(self):
        return patch.object(interrupts, "ENABLED", True)

    def test_disabled_controller_preserves_handlers_and_outer_hold(self):
        handlers = [signal.getsignal(n) for n in interrupts._SIGNALS]
        with patch.object(interrupts, "ENABLED", False), interrupts.installed():
            interrupts.bind(self.store)
            with self.assertRaises(KeyboardInterrupt):
                with interrupts.hold(), interrupts.hold(), defer_cleanup_signals() as captured:
                    send(signal.SIGTERM)
                    self.assertEqual(captured, [])
                    self.assertTrue(interrupts.holding())
                    interrupts.deliver()
            self.assertFalse(interrupts.holding())
            self.assertIsNone(interrupts.active())
            self.assertEqual([signal.getsignal(n) for n in interrupts._SIGNALS], handlers)
            with self.assertRaises(KeyboardInterrupt), quota.ProbeSignals() as probe:
                send(signal.SIGHUP)
                self.assertTrue(probe.pending())

    def test_one_delivery_outermost_owner_and_repeat(self):
        handlers = [signal.getsignal(n) for n in interrupts._SIGNALS]
        with self.install(), interrupts.installed():
            controller = interrupts.active()
            with interrupts.installed():
                self.assertIs(interrupts.active(), controller)
                with defer_cleanup_signals() as captured:
                    send(signal.SIGHUP)
                    self.assertEqual(captured, [])
                self.assertEqual(controller.first[0], signal.SIGHUP)
                self.assertGreater(controller.first[1], 0)
            with self.assertRaises(KeyboardInterrupt):
                interrupts.deliver()
            self.assertEqual(controller.delivered_from, self.test_one_delivery_outermost_owner_and_repeat.__qualname__)
            send(signal.SIGINT)
            self.assertTrue(interrupts.hurry())
            interrupts.deliver()
            self.assertTrue(interrupts.hurry())
        self.assertEqual(self.output.getvalue().count("第 2 次"), 1)
        self.assertEqual([signal.getsignal(n) for n in interrupts._SIGNALS], handlers)
        self.assertIsNone(interrupts.active())
        with self.install(), interrupts.installed():
            self.assertFalse(interrupts.hurry())
            self.assertFalse(interrupts.deliverable())

    def test_dirty_ledger_rechecks_only_after_adoption(self):
        self.store.attach({"id": "run_one", "status": "running"})
        self.store.commit()
        # Measure the controller's production cache without the offline wait reporter's own checks.
        with (self.install(), interrupts.installed(), patch.object(interrupts, "_wait_reporter", None),
              patch.object(self.store, "matches_commit", wraps=self.store.matches_commit) as matches):
            interrupts.bind(self.store)
            interrupts.deliver()
            matches.assert_not_called()
            self.store.run["status"] = "planning"
            self.store.run["store_revision"] += 1
            send()
            for _ in range(3):
                interrupts.deliver()
            self.assertEqual(matches.call_count, 1)
            self.store.run["store_revision"] -= 1
            self.store.commit()
            with self.assertRaises(KeyboardInterrupt):
                interrupts.deliver()
            self.assertEqual(matches.call_count, 2)

    def test_clean_reformatted_ledger_and_no_selected_ledger_can_deliver(self):
        run = {"id": "run_one", "status": "running"}
        self.store.attach(run)
        self.store.commit()
        path = self.store.root / run["id"] / "state.json"
        path.write_text(json.dumps(run, indent=4))
        self.store.attach(json.loads(path.read_text()))
        for selected in (self.store, RunStore(self.store.root.parent)):
            with self.subTest(selected=selected.run), self.install(), interrupts.installed():
                interrupts.bind(selected)
                send()
                with self.assertRaises(KeyboardInterrupt):
                    interrupts.deliver()

    def test_hold_and_publication_block_delivery(self):
        read = SimpleNamespace(interruptible=True)
        with self.install(), interrupts.installed():
            self.assertTrue(interrupts.abortable(read))
            self.assertFalse(interrupts.abortable(None))
            self.assertFalse(interrupts.abortable(SimpleNamespace(interruptible=False)))
            with interrupts.hold(), interrupts.hold():
                send()
                interrupts.deliver()
                self.assertFalse(interrupts.abortable(read))
            with interrupts.publishing(), interrupts.publishing():
                self.assertFalse(interrupts.abortable(read))
                self.assertFalse(interrupts.deliverable())
                for action in (interrupts.deliver, lambda: interrupts.sleep(0), lambda: interrupts.hold().__enter__()):
                    with self.assertRaises(HarnessError) as caught:
                        action()
                    self.assertEqual(stops.of(caught.exception)["reason"], "invariant")
            with self.assertRaises(KeyboardInterrupt):
                interrupts.deliver()
        with interrupts.publishing(), self.assertRaises(HarnessError):
            interrupts.deliver()

    def test_terminal_commit_keeps_status_and_outgoing_error(self):
        failure = OSError("ordinary failed callback")
        self.store.attach({"id": "run_one", "status": "running"})
        with self.assertRaises(OSError) as caught:
            with self.install(), interrupts.installed():
                interrupts.bind(self.store)
                self.store.run["status"] = "blocked"
                send(signal.SIGTERM)
                self.store.commit(stop=True)
                interrupts.deliver()
                raise failure
        self.assertIs(caught.exception, failure)
        self.assertIn("SIGTERM", self.output.getvalue())
        self.assertIn("status=blocked", self.output.getvalue())

    def test_terminal_adoption_after_ambiguous_write_still_suppresses_delivery(self):
        self.store.attach({"id": "run_one", "status": "running"})
        self.store.commit()
        publish = self.store._publish_candidate
        def committed_then_failed(*args):
            publish(*args)
            send()
            raise OSError("right after publish")
        with self.install(), interrupts.installed(), patch.object(
                self.store, "_publish_candidate", side_effect=committed_then_failed):
            interrupts.bind(self.store)
            self.store.run["status"] = "completed"
            self.assertEqual(self.store.commit(terminal=True), 2)
            self.assertTrue(self.store.terminal_committed)
            interrupts.deliver()
        self.assertIn("status=completed", self.output.getvalue())

    def test_signal_records_survive_blocked_restore(self):
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGHUP})
        try:
            with self.install(), interrupts.installed():
                send(signal.SIGHUP)
                self.assertFalse(interrupts.deliverable())
            self.assertNotIn(signal.SIGHUP, signal.sigpending())
            self.assertIn(signal.SIGHUP, signal.pthread_sigmask(signal.SIG_BLOCK, ()))
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        self.assertIn("SIGHUP", self.output.getvalue())

    def test_signal_on_handler_restore_remains_with_invocation(self):
        original = signal.signal
        def restore(signum, handler):
            previous = original(signum, handler)
            if signum == signal.SIGTERM and handler == original_term:
                send(signum)
            return previous
        original_term = signal.getsignal(signal.SIGTERM)
        with self.install(), patch.object(signal, "signal", side_effect=restore), interrupts.installed():
            self.assertFalse(interrupts.deliverable())
            interrupts.terminal()
        self.assertIn("SIGTERM", self.output.getvalue())

    def test_pending_signal_is_collected_before_restoring_ignore(self):
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        pending = signal.sigpending

        def snapshot():
            send(signal.SIGTERM)
            observed = pending()
            self.assertIn(signal.SIGTERM, observed)
            return observed

        try:
            with self.install(), patch.object(signal, "sigpending", side_effect=snapshot), interrupts.installed():
                controller = interrupts.active()
            self.assertEqual(controller.count, 1)
            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
            self.assertNotIn(signal.SIGTERM, pending())
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assertIn("SIGTERM", self.output.getvalue())

    def test_final_unmask_propagates_old_handler_after_cleanup_and_reporting(self):
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        real_mask = signal.pthread_sigmask
        status = {"id": "run_one", "status": "completed"}
        self.store.attach(status)
        handoff = []

        def mask(how, signals):
            if how == signal.SIG_SETMASK and signal.getsignal(signal.SIGINT) is signal.default_int_handler:
                send()
                handoff.append((interrupts._controller, controller.reader, controller.writer,
                                self.output.getvalue(), signal.SIGINT in signal.sigpending()))
            return real_mask(how, signals)

        try:
            with self.install(), patch.object(signal, "pthread_sigmask", side_effect=mask):
                with self.assertRaises(KeyboardInterrupt), interrupts.installed():
                    controller = interrupts.active()
                    interrupts.bind(self.store)
                    self.store.commit(terminal=True)
                    send(signal.SIGHUP)
            [(owner, reader, writer, diagnostic, pending)] = handoff
            self.assertEqual((owner, reader, writer), (None, None, None))
            self.assertIn("status=completed", diagnostic)
            self.assertTrue(pending)
            self.assertEqual(controller.count, 1)
            self.assertEqual(json.loads((self.store.root / status["id"] / "state.json").read_text()), status)
        finally:
            signal.signal(signal.SIGINT, previous)

    def test_full_wake_pipe_cannot_block_recording(self):
        with self.install(), interrupts.installed():
            controller = interrupts.active()
            self.assertFalse(os.get_inheritable(controller.writer))
            self.assertFalse(os.get_inheritable(controller.reader))
            with self.assertRaises(BlockingIOError):
                while True:
                    os.write(controller.writer, b"x" * 512)
            with self.assertRaises(KeyboardInterrupt):
                send()
                interrupts.deliver()

    def test_sleep_real_and_fake_clocks(self):
        with self.install(), interrupts.installed():
            start = time.monotonic()
            timer = threading.Timer(0.05, os.kill, (os.getpid(), signal.SIGTERM))
            timer.start()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    interrupts.sleep(5)
            finally:
                timer.join()
            self.assertLess(time.monotonic() - start, 2)
        slept = []
        def fake_sleep(seconds):
            slept.append(seconds)
            send()
        with self.install(), interrupts.installed(), self.assertRaises(KeyboardInterrupt):
            interrupts.sleep(3, clock=SimpleNamespace(sleep=fake_sleep))
        self.assertEqual(slept, [3])

    def test_probe_checkpoints_obey_controller_and_leave_original_signal_pending(self):
        with self.install(), interrupts.installed(), quota.ProbeSignals() as probe:
            with interrupts.hold():
                send(signal.SIGTERM)
                self.assertTrue(probe.pending())
                with self.assertRaises(quota.Unavailable):
                    probe.checkpoint()
            with self.assertRaises(KeyboardInterrupt):
                probe.checkpoint()
        with self.install(), interrupts.installed():
            interrupts.terminal()
            with quota.ProbeSignals() as probe:
                send(signal.SIGHUP)
                self.assertTrue(probe.pending())
                with self.assertRaises(quota.Unavailable):
                    probe.checkpoint()

    def test_terminal_probe_returns_an_unavailable_snapshot_without_launch(self):
        with self.install(), interrupts.installed(), patch.dict(
                quota.ARGV, {"codex": [sys.executable, "-c", "pass"]}):
            interrupts.terminal()
            send(signal.SIGHUP)
            record = quota.snapshot("codex", os.environ)
        self.assertFalse(record["available"])
        self.assertEqual(record["reason"], "internal_error")
        self.assertEqual(record["detail"], {"error_type": "InterruptedProbe"})
        self.assertFalse(record["process"]["started"])
        self.assertTrue(record["process"]["temp_removed"])

    def test_worker_thread_cannot_take_or_deliver_main_thread_request(self):
        def worker():
            with interrupts.installed(), interrupts.hold(), quota.ProbeSignals() as probe:
                interrupts.bind(None)
                interrupts.deliver()
                return interrupts.active(), interrupts.deliverable(), probe.pending()
        with self.install(), interrupts.installed():
            interrupts.bind(self.store)
            controller = interrupts.active()
            send()
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(pool.submit(worker).result(timeout=2), (None, False, False))
            self.assertIs(controller.store, self.store)
            with self.assertRaises(KeyboardInterrupt):
                interrupts.deliver()

    def test_fork_does_not_share_handlers_pipe_or_cancellation(self):
        result = subprocess.run([sys.executable, "-c", textwrap.dedent("""
            import errno, os, signal
            from harness import interrupts

            seen = []
            def baseline(signum, frame):
                seen.append(signum)
            signal.signal(signal.SIGTERM, baseline)
            interrupts.ENABLED = True
            with interrupts.installed():
                controller = interrupts.active()
                fds = (controller.reader, controller.writer)
                signal.raise_signal(signal.SIGINT)
                pid = os.fork()
                if not pid:
                    assert interrupts.active() is None
                    assert signal.getsignal(signal.SIGTERM) is baseline
                    for fd in fds:
                        try:
                            os.fstat(fd)
                        except OSError as error:
                            assert error.errno == errno.EBADF
                        else:
                            raise AssertionError('inherited wake pipe was not closed')
                    signal.raise_signal(signal.SIGTERM)
                    assert seen == [signal.SIGTERM]
                    with interrupts.installed():
                        assert not interrupts.deliverable()
                    os._exit(0)
                assert os.waitpid(pid, 0)[1] == 0
                assert interrupts.active() is controller and controller.count == 1
                try:
                    interrupts.deliver()
                except KeyboardInterrupt:
                    pass
                else:
                    raise AssertionError('parent lost the request')
        """)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lifecycle_abandon_terminal_rereport_marks_controller(self):
        self.store.attach({"id": "run_one", "status": "abandoned"})
        self.store.commit()
        def locked(stop, callback):
            self.assertIs(interrupts.active().store, self.store)
            result = callback()
            send()
            interrupts.deliver()
            return result
        context = SimpleNamespace(store=self.store, run=self.store.run, run_lock=locked,
                                  confirm_exited=lambda ids: None, ensure_exited=lambda: None)
        lifecycle = RunLifecycle(context, None)
        with self.install(), patch.object(lifecycle, "load_run", return_value=None):
            self.assertEqual(lifecycle.abandon("run_one")["status"], "abandoned")
        self.assertIn("status=abandoned", self.output.getvalue())

    def test_cli_operate_spans_record_creation_and_finish(self):
        args = SimpleNamespace(command="resume", run_id="run_one", approve_plan=False, step=False,
                               reverify=False, operation_id=None)
        store = self.store
        store.attach({"id": "run_one", "status": "running"})
        def create(state, **kwargs):
            send(signal.SIGHUP)
            self.assertTrue(interrupts.deliverable())
            return SimpleNamespace(id="operation_one", path="operation.json", finish=finish)
        def resume(*args, **kwargs):
            interrupts.bind(store)
            store.run.update(status="completed", branch="branch", worktree="worktree")
            store.commit(terminal=True)
            return store.run
        finished = []
        def finish(**outcome):
            send(signal.SIGTERM)
            interrupts.deliver()
            finished.append(outcome)
        def output(result):
            self.assertEqual(result["status"], "completed")
            interrupts.deliver()
        with (self.install(), patch.object(cli.OperationRecord, "create", side_effect=create),
              patch.object(cli, "operation_arguments"), patch.object(cli, "file_inputs_present"),
              patch.object(cli, "resume_options", return_value={}),
              patch.object(cli, "Runtime", return_value=SimpleNamespace(resume=resume)),
              patch.object(cli, "print_saved_result", side_effect=output)):
            self.assertEqual(cli.operate(args, None, None, None), 0)
        self.assertEqual(finished, [dict(exit_status=0, result_status="completed", error=None, stop=None, run_id="run_one")])
        self.assertIn("status=completed", self.output.getvalue())

    def test_entry_lifecycle_start_and_resume_bind_and_restore(self):
        expected = HarnessError("validation failed")
        context = SimpleNamespace(store=self.store, repo=None, saved_stop=None)
        lifecycle = RunLifecycle(context, None)
        def preflight(repo):
            self.assertIs(interrupts.active().store, self.store)
            send(signal.SIGTERM)
            raise expected
        with self.install(), patch("harness.run_lifecycle.git.check_starting_checkout", side_effect=preflight):
            with self.assertRaises(HarnessError) as failed:
                lifecycle.start("goal")
        self.assertIs(failed.exception, expected)
        self.assertIn("SIGTERM", self.output.getvalue())
        self.assertIsNone(interrupts.active())

        self.store.attach({"id": "run_one", "status": "completed", "commander": "astra"})
        self.store.commit()
        context.run = self.store.run
        def locked(stop, callback):
            self.assertIs(interrupts.active().store, self.store)
            result = callback()
            send(signal.SIGINT)
            interrupts.deliver()
            return result
        context.run_lock = locked
        with self.install(), patch.object(lifecycle, "load_run", return_value=None):
            self.assertEqual(lifecycle.resume("run_one")["status"], "completed")
        self.assertIn("status=completed", self.output.getvalue())
        self.assertIsNone(interrupts.active())

    def test_cli_abandon_spans_precheck_and_result_output(self):
        self.store.attach({"id": "run_one", "status": "running"})
        original_term = signal.getsignal(signal.SIGTERM)
        def precheck(*args, **kwargs):
            send(signal.SIGTERM)
            self.assertTrue(interrupts.deliverable())
        def abandon(*args, **kwargs):
            interrupts.bind(self.store)
            self.store.run["status"] = "abandoned"
            self.store.commit(terminal=True)
            return self.store.run
        def output(result):
            self.assertEqual(result["status"], "abandoned")
            interrupts.deliver()
        with (self.install(), patch.object(cli, "precheck_run", side_effect=precheck),
              patch.object(cli.Config, "load"), patch.object(cli.git, "repository", return_value=Path(self.temp.name)),
              patch.object(cli, "Runtime", return_value=SimpleNamespace(abandon=abandon)),
              patch.object(cli, "status_summary", side_effect=lambda run: run),
              patch.object(cli, "print_saved_result", side_effect=output)):
            self.assertEqual(cli.main(["abandon", "run_one"]), 0)
        self.assertEqual(signal.getsignal(signal.SIGTERM), original_term)
        self.assertIn("status=abandoned", self.output.getvalue())
