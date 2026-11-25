"""Offline lifecycle checks for a ledger write whose result is uncertain."""

import contextlib
from copy import deepcopy
import errno
import io
import json
import os
import signal
from unittest.mock import patch

from harness import git, state_files
from harness.adapters import DemoBackend
from harness.operations import OperationRecord
from harness.run_store import LEDGER, RunStore
from harness.scheduler import Scheduler
from harness.state_files import LockedDirectory
from harness.util import HarnessError, write_json
from harness.workflow import Workflow
from test_operations import OperationCase
from test_quota_sampling import two_tasks
from test_runtime_changes import ScriptedBackend
from test_stop_exits import final_answer, final_failure_scenario


class TerminalWriteFault:
    def __init__(self, status, *, fault="after replace", failed_reads=(), fail_stop_close=False):
        self.status = status
        self.fault = fault
        self.failed_reads = failed_reads
        self.fail_stop_close = fail_stop_close
        self.stop_close_failed = False
        self.phase = None
        self.publications = []
        self.reads = []
        self.stops = []

    @contextlib.contextmanager
    def install(self):
        write = RunStore._publish_candidate
        commit, resolve = RunStore.commit, RunStore.resolve
        read, rename = LockedDirectory.read, os.replace
        close = os.close
        stop = Workflow.stop

        def publish(store, locked, candidate, data):
            first = candidate["status"] == self.status and not self.publications
            if first:
                self.store = store
                self.live = store.run
                self.before = store._path.read_bytes()
                self.candidate = deepcopy(candidate)
                self.data = data
                self.lock = locked
                self.phase = "terminal write"
            if first or self.publications:
                self.publications.append(candidate["status"])
            return write(store, locked, candidate, data)

        def replace(source, destination, **kwargs):
            if self.phase != "terminal write" or destination != LEDGER or "dst_dir_fd" not in kwargs:
                return rename(source, destination, **kwargs)
            if self.fault != "before replace":
                rename(source, destination, **kwargs)
            self.phase = "write failure read"
            if self.fault == "signal":
                self.revision_at_signal = self.live["store_revision"]
                os.kill(os.getpid(), signal.SIGINT)
                self.signal_deferred = True
                return
            raise OSError(errno.EIO, self.fault)

        def read_ledger(locked, name, **kwargs):
            if self.phase is not None and name == LEDGER:
                self.reads.append((self.phase, locked, locked.identity))
                if self.phase in self.failed_reads:
                    step = state_files.Step(2, LEDGER, "read", True)
                    return state_files.Outcome("failed", str(self.store._path), step, errno=errno.EIO)
            return read(locked, name, **kwargs)

        def close_store_lock(fd):
            if self.phase == "stop commit read":
                opened = os.fstat(fd)
                lock = self.store._path.with_name("store.lock").stat()
                if (opened.st_dev, opened.st_ino) == (lock.st_dev, lock.st_ino):
                    close(fd)
                    self.stop_close_failed = True
                    raise OSError(errno.EIO, "store lock close")
            close(fd)

        def commit_phase(store, **kwargs):
            self.phase = "stop commit read" if kwargs.get("stop") else None
            try:
                return commit(store, **kwargs)
            finally:
                self.phase = None

        def resolve_phase(store):
            self.phase = "boundary resolve read" if self.publications else None
            try:
                return resolve(store)
            finally:
                self.phase = None

        def record_stop(workflow, error):
            try:
                result = stop(workflow, error)
            finally:
                self.saved_stop = workflow.context.saved_stop
            self.stops.append(result)
            return result

        close_fault = (patch.object(state_files.os, "close", close_store_lock)
                       if self.fail_stop_close else contextlib.nullcontext())
        with close_fault, patch.object(RunStore, "_publish_candidate", publish), patch.object(RunStore, "commit", commit_phase), \
                patch.object(RunStore, "resolve", resolve_phase), patch.object(LockedDirectory, "read", read_ledger), \
                patch.object(state_files.os, "replace", replace), patch.object(Workflow, "stop", record_stop):
            yield

    @property
    def phases(self):
        return [phase for phase, _, _ in self.reads]


class CommitAmbiguityTests(OperationCase):
    def run_fault(self, fault, *args, operation="ambiguity", errors=None):
        with fault.install():
            return self.invoke(["run", "Implement greet", "--operation-id", operation, *args], errors=errors)

    def assert_saved_candidate(self, fault):
        self.assertEqual(fault.publications, [fault.status])
        self.assertEqual(fault.candidate["store_revision"], json.loads(fault.before)["store_revision"] + 1)
        self.assertEqual(fault.store._path.read_bytes(), fault.data)
        self.assertIs(fault.store.run, fault.live)
        self.assertEqual(fault.live, fault.candidate)
        self.assertTrue(fault.store.terminal_committed)
        self.assertIsNone(fault.store._pending)

    def assert_same_lock_for_recovery_read(self, fault):
        rereads = [(locked, identity) for phase, locked, identity in fault.reads if phase == "write failure read"]
        self.assertEqual(rereads, [(fault.lock, fault.store.directory_identity)])

    def test_replace_succeeds_then_raises_and_the_run_continues(self):
        fault = TerminalWriteFault("completed")
        code, output, errors = self.run_fault(fault)
        self.assertEqual((code, json.loads(output)["status"]), (0, "completed"))
        self.assert_saved_candidate(fault)
        self.assert_same_lock_for_recovery_read(fault)
        self.assertEqual(fault.stops, [])
        self.assertEqual(errors.count("台账写入报告了错误但已落盘"), 1)
        self.assertNotIn("其他写入者", errors)

    def test_replace_fails_before_writing_and_the_stop_uses_the_same_next_revision(self):
        fault = TerminalWriteFault("completed", fault="before replace")
        code, output, errors = self.run_fault(fault)
        self.assertEqual((code, output), (1, ""), errors)
        self.assertEqual(fault.publications, ["completed", "blocked"])
        self.assert_same_lock_for_recovery_read(fault)
        saved = self.ledger(fault.candidate["id"])
        stop = saved.pop("stop")
        candidate = deepcopy(fault.candidate)
        identity = candidate.pop("result")
        self.assertEqual(saved, {**candidate, "status": "blocked"})
        self.assertEqual(saved["store_revision"], json.loads(fault.before)["store_revision"] + 1)
        self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "io"))
        self.assertEqual(fault.stops, [Workflow.WRITTEN])
        backend = ScriptedBackend()
        engine = self.engine(backend)
        engine.load_run(saved["id"])
        result = engine.resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"], identity)
        self.assertEqual(result["final_history"], saved["final_history"])
        self.assertEqual(backend.calls, [])

    def test_failed_write_reread_is_resolved_by_stop_before_saving(self):
        fault = TerminalWriteFault("completed", failed_reads={"write failure read"})
        code, output, errors = self.run_fault(fault)
        self.assertEqual((code, json.loads(output)["status"]), (0, "completed"))
        self.assert_saved_candidate(fault)
        self.assertEqual(fault.phases, ["write failure read", "boundary resolve read"])
        self.assertEqual(fault.stops, [Workflow.KEPT])
        self.assertEqual(fault.live["result"], fault.candidate["result"])
        self.assertIsNot(fault.live["result"], fault.candidate["result"])
        self.assertIn("终态已保存", errors)

    def test_sigint_between_replace_and_adoption_keeps_completed(self):
        fault = TerminalWriteFault("completed", fault="signal")
        code, output, errors = self.run_fault(fault)
        self.assertTrue(fault.signal_deferred)
        self.assertEqual(fault.revision_at_signal, json.loads(fault.before)["store_revision"])
        self.assertEqual((code, json.loads(output)["status"]), (0, "completed"))
        self.assert_saved_candidate(fault)
        self.assertEqual(fault.phases, [])
        self.assertEqual(fault.stops, [])
        self.assertIn("status=completed", errors)
        self.assertEqual(self.record("ambiguity")["result_status"], "completed")

    def test_another_store_commit_owns_the_ledger(self):
        commit = RunStore.commit
        changed = []

        def publish(store, **kwargs):
            result = commit(store, **kwargs)
            if not changed and store.run["status"] == "running":
                other = RunStore(store.root.parent)
                other.attach(json.loads(store._path.read_bytes()))
                commit(other)
                changed.append((other._path, other._path.read_bytes()))
            return result

        with patch.object(RunStore, "commit", publish):
            code, output, errors = self.invoke(["run", "Implement greet", "--operation-id", "external"])
        self.assertEqual((code, output), (1, ""), errors)
        path, external = changed[0]
        self.assertEqual(path.read_bytes(), external)
        stop = self.record("external")["stop"]
        self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "invariant"))

    def test_stop_reconciles_completed_paused_and_approval_under_its_own_lock(self):
        plan = self.outside / "two-tasks.json"
        write_json(plan, two_tasks())
        cases = (("completed", "run_completed", ()), ("paused", "run_paused", ("--plan", str(plan), "--step")),
                 ("awaiting_approval", "plan_awaiting_approval", ("--pause-after-plan",)))
        for status, event, args in cases:
            with self.subTest(status=status):
                fault = TerminalWriteFault(status, failed_reads={"write failure read", "boundary resolve read"})
                code, output, errors = self.run_fault(fault, *args, operation=status)
                self.assertEqual((code, json.loads(output)["status"]), (0, status))
                self.assert_saved_candidate(fault)
                self.assertEqual(fault.phases, ["write failure read", "boundary resolve read", "stop commit read"])
                self.assertEqual(fault.stops, [Workflow.KEPT])
                if status == "completed":
                    self.assertEqual(fault.live["result"], fault.candidate["result"])
                    self.assertIsNot(fault.live["result"], fault.candidate["result"])
                ident = fault.candidate["id"]
                self.assertEqual(self.events(ident, "run_stopped"), [])
                self.assertEqual(len(self.events(ident, event)), 1)
                self.assertEqual(self.record(status)["result_status"], status)
                self.assertIn("终态已保存", errors)

    def test_stop_lock_close_failure_preserves_the_kept_result(self):
        fault = TerminalWriteFault("completed", failed_reads={"write failure read", "boundary resolve read"},
                                   fail_stop_close=True)
        code, output, errors = self.run_fault(fault)
        self.assertTrue(fault.stop_close_failed)
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["status"], "completed")
        self.assert_saved_candidate(fault)
        self.assertEqual(fault.phases, ["write failure read", "boundary resolve read", "stop commit read"])
        self.assertEqual(fault.stops, [Workflow.KEPT])
        self.assertIsNone(fault.saved_stop)
        self.assertEqual(self.events(fault.candidate["id"], "run_stopped"), [])
        self.assertEqual(self.record("ambiguity")["result_status"], "completed")
        self.assertIn("终态已保存", errors)

    def test_sigint_during_terminal_restore_under_stop_lock_preserves_kept(self):
        fault = TerminalWriteFault("completed", failed_reads={"write failure read", "boundary resolve read"})
        restore = RunStore.restore_terminal
        signals = []

        def interrupt_restore(store):
            if fault.phase == "stop commit read" and not signals:
                signals.append(store.terminal_committed)
                os.kill(os.getpid(), signal.SIGINT)
            return restore(store)

        with patch.object(RunStore, "restore_terminal", interrupt_restore):
            code, output, errors = self.run_fault(fault)

        self.assertEqual(signals, [True])
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["status"], "completed")
        self.assert_saved_candidate(fault)
        self.assertEqual(fault.phases, ["write failure read", "boundary resolve read", "stop commit read"])
        self.assertEqual(fault.stops, [Workflow.KEPT])
        self.assertIsNone(fault.saved_stop)
        ident = fault.candidate["id"]
        self.assertEqual(self.events(ident, "run_stopped"), [])
        self.assertEqual(len(self.events(ident, "run_completed")), 1)
        record = self.record("ambiguity")
        self.assertEqual((record["exit_status"], record["result_status"]), (0, "completed"))
        self.assertIn("终态已保存", errors)

    def test_sigint_during_early_terminal_restore_preserves_run_and_abandon(self):
        with contextlib.redirect_stderr(io.StringIO()):
            pending = self.engine().start("Implement greet", pause_after_plan=True)

        restore = RunStore.restore_terminal
        for status in ("completed", "abandoned"):
            with self.subTest(status=status):
                fault = TerminalWriteFault(status, failed_reads={"write failure read"})
                signals = []

                def interrupt_restore(store):
                    signals.append(store.terminal_committed)
                    os.kill(os.getpid(), signal.SIGINT)
                    return restore(store)

                with patch.object(RunStore, "restore_terminal", interrupt_restore):
                    if status == "completed":
                        code, output, errors = self.run_fault(fault)
                    else:
                        with fault.install():
                            code, output, errors = self.invoke(["abandon", pending["id"]])

                self.assertEqual(signals, [True])
                self.assertEqual(code, 0, errors)
                self.assertEqual(json.loads(output)["status"], status)
                self.assert_saved_candidate(fault)
                self.assertEqual(fault.phases, ["write failure read", "boundary resolve read"])
                ident = fault.candidate["id"]
                self.assertEqual(self.events(ident, "run_stopped"), [])
                self.assertEqual(len(self.events(ident, f"run_{status}")), 1)
                self.assertIn("终态已保存", errors)
                if status == "completed":
                    self.assertEqual(fault.stops, [Workflow.KEPT])
                    self.assertIsNone(fault.saved_stop)
                    record = self.record("ambiguity")
                    self.assertEqual((record["exit_status"], record["result_status"]), (0, status))

    def test_rejected_stop_identity_is_reported_after_adopting_the_prior_terminal(self):
        fault = TerminalWriteFault("completed", failed_reads={"write failure read", "boundary resolve read"})
        commit = RunStore.commit

        def reject_stop(store, **kwargs):
            if kwargs.get("stop"):
                store.run["store_revision"] += 10
            return commit(store, **kwargs)

        with patch.object(RunStore, "commit", reject_stop):
            code, output, errors = self.run_fault(fault)
        self.assertEqual((code, output), (1, ""), errors)
        self.assertEqual(fault.publications, ["completed"])
        self.assertEqual(fault.phases, ["write failure read", "boundary resolve read", "stop commit read"])
        self.assertTrue(fault.store.terminal_committed)
        self.assertEqual(fault.store._path.read_bytes(), fault.data)
        self.assertEqual(fault.stops, [])
        self.assertIsNone(fault.saved_stop)
        self.assertEqual(self.events(fault.candidate["id"], "run_stopped"), [])
        record = self.record("ambiguity")
        self.assertEqual((record["exit_status"], record["result_status"]), (1, None))
        self.assertEqual((record["stop"]["kind"], record["stop"]["reason"]), ("runtime_failure", "invariant"))

    def test_sigint_after_writing_the_stop_preserves_its_publication(self):
        fault = TerminalWriteFault("blocked", fault="signal")
        with patch.object(Scheduler, "admission", side_effect=OSError(errno.EIO, "admission")):
            code, output, errors = self.run_fault(fault)
        self.assertTrue(fault.signal_deferred)
        self.assertEqual((code, output), (1, ""), errors)
        self.assert_saved_candidate(fault)
        self.assertEqual(fault.phases, ["stop commit read"])
        self.assertEqual(fault.stops, [Workflow.WRITTEN])
        self.assertIsNotNone(fault.saved_stop)
        self.assertEqual(len(self.events(fault.candidate["id"], "run_stopped")), 1)
        stop = self.record("ambiguity")["stop"]
        self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "io"))

    def test_sigint_inside_kept_warning_preserves_the_cli_result(self):
        class InterruptKeptWarning(io.StringIO):
            interrupted = False

            def write(self, text):
                if "终态已保存" in text and not self.interrupted:
                    self.interrupted = True
                    os.kill(os.getpid(), signal.SIGINT)
                return super().write(text)

        stderr = InterruptKeptWarning()
        fault = TerminalWriteFault("completed", failed_reads={"write failure read"})
        code, output, errors = self.run_fault(fault, errors=stderr)
        self.assertTrue(stderr.interrupted)
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["status"], "completed")
        self.assert_saved_candidate(fault)
        self.assertEqual(fault.stops, [Workflow.KEPT])
        self.assertIsNone(fault.saved_stop)
        self.assertEqual(self.events(fault.candidate["id"], "run_stopped"), [])
        self.assertEqual(self.record("ambiguity")["result_status"], "completed")

    def test_boundary_restores_the_saved_terminal_after_an_observer_changes_live_state(self):
        engine = self.engine()
        published = []

        def fail_after_save(run):
            if run["status"] == "completed":
                path = engine.directory / LEDGER
                published.append((run, path.read_bytes()))
                run["accepted"].clear()
                run["status"] = "damaged observer state"
                raise KeyboardInterrupt("after saving completed")

        engine.context.on_save = fail_after_save
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            result = engine.start("Implement greet")
        (live, saved), = published
        self.assertIs(result, live)
        self.assertEqual(result, json.loads(saved))
        self.assertEqual((engine.directory / LEDGER).read_bytes(), saved)
        self.assertEqual(self.events(result["id"], "run_stopped"), [])
        self.assertIn("终态已保存", errors.getvalue())

    def test_unreadable_through_stop_commit_reports_io_then_status_and_resume_find_completed(self):
        fault = TerminalWriteFault("completed",
                                   failed_reads={"write failure read", "boundary resolve read", "stop commit read"})
        finish = OperationRecord.finish
        reported = []

        def record_failure(operation, **fields):
            reported.append(fields["error"])
            return finish(operation, **fields)

        with patch.object(OperationRecord, "finish", record_failure):
            code, output, errors = self.run_fault(fault)
        self.assertEqual((code, output), (1, ""), errors)
        error, = reported
        self.assertIsInstance(error, state_files.StateReadError)
        self.assertEqual(error.outcome.step.stage, "read")
        self.assertEqual(fault.publications, ["completed"])
        self.assertEqual(fault.phases, ["write failure read", "boundary resolve read", "stop commit read"])
        self.assertEqual(fault.stops, [])
        ident = fault.candidate["id"]
        self.assertEqual(fault.store._path.read_bytes(), fault.data)
        stop = self.record("ambiguity")["stop"]
        self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "io"))
        self.assertEqual(self.events(ident, "run_stopped"), [])
        self.assertEqual(self.events(ident, "run_completed"), [])
        with patch.object(DemoBackend, "invoke", side_effect=AssertionError("resume dispatched after completion")):
            code, output, errors = self.invoke(["status", ident, "--full"])
            self.assertEqual((code, json.loads(output)), (0, fault.candidate), errors)
            code, output, errors = self.invoke(["resume", ident])
            self.assertEqual((code, json.loads(output)["status"]), (0, "completed"), errors)
        self.assertEqual(fault.store._path.read_bytes(), fault.data)

    def test_abandon_resolves_its_ambiguous_write_and_reports_saved_status(self):
        with contextlib.redirect_stderr(io.StringIO()):
            run = self.engine().start("Implement greet", pause_after_plan=True)
        fault = TerminalWriteFault("abandoned", failed_reads={"write failure read"})
        with fault.install():
            code, output, errors = self.invoke(["abandon", run["id"]])
        self.assertEqual((code, json.loads(output)["status"]), (0, "abandoned"))
        self.assert_saved_candidate(fault)
        self.assertEqual(fault.phases, ["write failure read", "boundary resolve read"])
        self.assertEqual(len(self.events(run["id"], "run_abandoned")), 1)
        self.assertIn("终态已保存", errors)

    def assert_kept_at_lifecycle_boundary(self, engine, call):
        workflow = engine.services.workflow
        stop = workflow.stop
        published = []

        def stop_with_committed_terminal(error):
            # These callers cannot normally follow a terminal commit in the same call.
            engine.run["status"] = "completed"
            engine.run.pop("stop", None)
            engine.context.save(terminal=True)
            published.append(((engine.directory / LEDGER).read_bytes(), self.events(engine.run["id"], "run_stopped")))
            return stop(error)

        with patch.object(workflow, "stop", side_effect=stop_with_committed_terminal), \
                contextlib.redirect_stderr(io.StringIO()):
            result = call()
        (saved, stopped_events), = published
        self.assertIs(result, engine.run)
        self.assertEqual(result, json.loads(saved))
        self.assertEqual((engine.directory / LEDGER).read_bytes(), saved)
        self.assertEqual(self.events(result["id"], "run_stopped"), stopped_events)
        self.assertIsNone(engine.context.saved_stop)

    def test_start_returns_the_kept_ledger_after_worktree_creation_fails(self):
        engine = self.engine()
        git_command = git.git

        def fail_worktree_creation(repo, *args, **kwargs):
            if args[:2] == ("worktree", "add"):
                raise HarnessError("worktree creation failed")
            return git_command(repo, *args, **kwargs)

        with patch.object(git, "git", fail_worktree_creation):
            self.assert_kept_at_lifecycle_boundary(engine, lambda: engine.start("Implement greet"))

    def test_resume_returns_the_kept_ledger_after_landing_an_ask_fails(self):
        engine = self.engine()
        with contextlib.redirect_stderr(io.StringIO()):
            run = engine.start("Implement greet", pause_after_plan=True)
        with patch.object(engine.services.planning, "land_returned_ask", side_effect=HarnessError("ask failed")):
            self.assert_kept_at_lifecycle_boundary(engine, lambda: engine.resume(run["id"]))

    def test_resume_returns_the_kept_ledger_after_an_operator_stop(self):
        with contextlib.redirect_stderr(io.StringIO()):
            backend, previous, request = final_failure_scenario(self)
        engine = self.engine(backend)
        answer = final_answer(self, request, "stop")
        self.assert_kept_at_lifecycle_boundary(engine, lambda: engine.resume(previous.run["id"], operator_decision=answer))
        self.assertEqual(engine.run["operator_decisions"][-1]["decision"], "stop")
