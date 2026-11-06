import contextlib
import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from harness.util import HarnessError, ProcessTimeout, read_json, run_process


class ProcessRuntimeTests(unittest.TestCase):
    def invoke(self, script, **kwargs):
        return run_process([sys.executable, "-c", script], self.root, self.log, **kwargs)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.log = self.root / "process"

    def test_real_sigint_after_popen_creation_retains_ownership_and_cleans_child(self):
        original = subprocess.Popen
        children = []
        def interrupted(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            os.kill(os.getpid(), signal.SIGINT)
            return child
        try:
            with patch('harness.util.subprocess.Popen', side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    self.invoke('import time; time.sleep(30)')
            facts = caught.exception.outcome
            self.assertTrue(facts.started)
            self.assertEqual(facts.pid, children[0].pid)
            self.assertEqual(facts.termination_reason, 'cancelled')
            self.assertTrue(facts.exit_confirmed)
            self.assertIsNotNone(children[0].poll())
            saved = read_json(self.log.with_suffix('.outcome.json'))
            self.assertTrue(saved['started'])
            self.assertTrue(saved['exit_confirmed'])
            self.assertEqual(saved['termination_reason'], 'cancelled')
        finally:
            for child in children:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()

    def test_real_sigint_during_handler_restore_is_not_lost(self):
        original = signal.signal
        injected = False
        def handoff(sig, handler):
            nonlocal injected
            result = original(sig, handler)
            if (sig == signal.SIGINT and handler is signal.default_int_handler and not injected
                    and read_json(self.log.with_suffix('.outcome.json'))['exit_confirmed']):
                injected = True
                os.kill(os.getpid(), signal.SIGINT)
            return result
        with patch('harness.process_runtime.signal.signal', side_effect=handoff):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.invoke('pass')
        facts = caught.exception.outcome
        self.assertTrue(injected)
        self.assertEqual(facts.exit_code, 0)
        self.assertTrue(facts.exit_confirmed)
        self.assertEqual(facts.termination_reason, 'cancelled')
        self.assertEqual(read_json(self.log.with_suffix('.outcome.json'))['termination_reason'], 'cancelled')

    def test_handler_restore_sigint_preserves_timeout(self):
        original = signal.signal
        def handoff(sig, handler):
            result = original(sig, handler)
            if (sig == signal.SIGINT and handler is signal.default_int_handler
                    and read_json(self.log.with_suffix('.outcome.json'))['exit_confirmed']):
                os.kill(os.getpid(), signal.SIGINT)
            return result
        with patch('harness.process_runtime.signal.signal', side_effect=handoff):
            with self.assertRaises(ProcessTimeout) as caught:
                self.invoke('import time; time.sleep(30)', timeout=.05)
        self.assertEqual(caught.exception.outcome.termination_reason, 'timeout')
        self.assertTrue(caught.exception.outcome.exit_confirmed)
        self.assertEqual(read_json(self.log.with_suffix('.outcome.json'))['termination_reason'], 'timeout')

    def test_normal_and_signal_codes_are_retained(self):
        for script, expected in [("pass", 0),
                ("import os,signal; os.kill(os.getpid(),signal.SIGTERM)", -signal.SIGTERM)]:
            outcomes = []
            self.assertEqual(self.invoke(script, on_outcome=outcomes.append), expected)
            self.assertEqual(outcomes[0].exit_code, expected)
            self.assertTrue(outcomes[0].exit_confirmed)
            self.assertEqual(read_json(self.log.with_suffix('.outcome.json'))['exit_code'], expected)

    def test_first_real_cancel_during_success_outcome_write_is_retained(self):
        from harness.util import write_json
        def interrupted(path, value):
            if value.get('exit_confirmed'):
                os.kill(os.getpid(), signal.SIGINT)
            write_json(path, value)
        with patch('harness.util.write_json', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.invoke('pass')
        facts = caught.exception.outcome
        self.assertEqual(facts.exit_code, 0)
        self.assertTrue(facts.exit_confirmed)
        self.assertEqual(facts.termination_reason, 'cancelled')
        self.assertEqual(read_json(self.log.with_suffix('.outcome.json'))['termination_reason'], 'cancelled')

    def test_known_zero_survives_observation_permission_failure(self):
        with patch('harness.process_runtime.os.killpg', side_effect=PermissionError(errno.EPERM, 'fault')):
            with self.assertRaises(HarnessError) as caught:
                self.invoke('pass')
        outcome = caught.exception.outcome
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(outcome.termination_reason, 'normal')
        self.assertFalse(outcome.exit_confirmed)
        self.assertFalse(outcome.evidence_complete)
        self.assertEqual(outcome.cleanup_errors[0]['errno'], errno.EPERM)

    def test_timeout_survives_term_failure_and_kill_is_attempted(self):
        original = os.killpg
        actions = []
        def fault(pgid, sig):
            actions.append(sig)
            if sig == signal.SIGTERM:
                raise PermissionError(errno.EPERM, 'fault')
            return original(pgid, sig)
        with patch('harness.process_runtime.os.killpg', side_effect=fault):
            with self.assertRaises(ProcessTimeout) as caught:
                self.invoke('import time; time.sleep(30)', timeout=.05)
        self.assertEqual(caught.exception.outcome.termination_reason, 'timeout')
        self.assertIn(signal.SIGKILL, actions)
        self.assertEqual(caught.exception.outcome.exit_code, -signal.SIGKILL)
        self.assertTrue(caught.exception.outcome.exit_confirmed)

    def test_started_callback_failure_preserves_start_and_cancellation(self):
        def interrupted(pid):
            raise KeyboardInterrupt('cancelled')
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.invoke('import time; time.sleep(30)', on_start=interrupted)
        self.assertTrue(caught.exception.outcome.started)
        self.assertTrue(caught.exception.outcome.exit_confirmed)
        self.assertEqual(caught.exception.outcome.termination_reason, 'cancelled')

    def test_receipt_failure_is_not_success(self):
        from harness.util import write_json
        calls = []
        def fault(path, value):
            calls.append(value)
            if len(calls) == 3:
                raise OSError('receipt fault')
            return write_json(path, value)
        with patch('harness.util.write_json', side_effect=fault):
            with self.assertRaisesRegex(OSError, 'receipt fault') as caught:
                self.invoke('pass')
        self.assertEqual(caught.exception.outcome.exit_code, 0)
        self.assertFalse(caught.exception.outcome.evidence_complete)

    def test_leader_exit_with_live_descendant_is_unknown_and_not_signalled(self):
        child_file = self.root / 'child'
        script = ('import subprocess,sys; p=subprocess.Popen([sys.executable,"-c",'
                  '"import time; time.sleep(30)"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
                  f'open({str(child_file)!r},"w").write(str(p.pid))')
        pgids = []
        try:
            with self.assertRaises(HarnessError) as caught:
                self.invoke(script, on_start=pgids.append)
            self.assertEqual(caught.exception.outcome.exit_code, 0)
            self.assertFalse(caught.exception.outcome.exit_confirmed)
            self.assertNotIn('sigterm', caught.exception.outcome.cleanup_actions)
            os.kill(int(child_file.read_text()), 0)
        finally:
            for pgid in pgids:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)

    def test_descendant_drain_respects_shared_cleanup_deadline(self):
        child_file = self.root / "child"
        script = ('import subprocess,sys; p=subprocess.Popen([sys.executable,"-c",'
                  '"import time; time.sleep(30)"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
                  f'open({str(child_file)!r},"w").write(str(p.pid))')
        started = time.monotonic()
        try:
            with self.assertRaises(HarnessError) as caught:
                self.invoke(script, cleanup_deadline=started + .2)
            self.assertLess(time.monotonic() - started, .6)
            outcome = caught.exception.outcome
            self.assertEqual(outcome.cleanup_actions.count("drain_descendants"), 1)
            self.assertLess(len(outcome.cleanup_actions), 5)
            self.assertEqual(outcome.exit_code, 0)
            self.assertFalse(outcome.exit_confirmed)
            self.assertNotIn("sigterm", outcome.cleanup_actions)
            os.kill(int(child_file.read_text()), 0)
        finally:
            if child_file.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(child_file.read_text()), signal.SIGKILL)

    def test_intent_failure_prevents_start(self):
        with patch('harness.util.write_json', side_effect=OSError('intent fault')), \
                patch('harness.util.subprocess.Popen') as popen:
            with self.assertRaisesRegex(OSError, 'intent fault'):
                self.invoke('pass')
        popen.assert_not_called()

    def test_pid_persistence_failure_still_cleans_started_child(self):
        from harness.util import write_json
        calls = []
        outcomes = []
        def fault(path, value):
            calls.append(value)
            if len(calls) == 2:
                raise OSError('pid fault')
            return write_json(path, value)
        with patch('harness.util.write_json', side_effect=fault):
            with self.assertRaisesRegex(OSError, 'pid fault') as caught:
                self.invoke('import time; time.sleep(30)', on_outcome=outcomes.append)
        self.assertTrue(caught.exception.outcome.started)
        self.assertTrue(caught.exception.outcome.exit_confirmed)
        self.assertFalse(caught.exception.outcome.evidence_complete)
        self.assertEqual(len(outcomes), 1)

    def test_observation_denied_still_terminates_owned_leader(self):
        original = os.killpg
        signals = []
        def denied(pgid, sig):
            signals.append(sig)
            if sig == 0:
                raise PermissionError(errno.EPERM, 'observation denied')
            return original(pgid, sig)
        with patch('harness.process_runtime.os.killpg', side_effect=denied):
            with self.assertRaises(ProcessTimeout) as caught:
                self.invoke('import time; time.sleep(30)', timeout=.05)
        facts = caught.exception.outcome
        self.assertIn(signal.SIGTERM, signals)
        self.assertEqual(facts.exit_code, -signal.SIGTERM)
        self.assertFalse(facts.exit_confirmed)
        with self.assertRaises(ProcessLookupError):
            os.kill(facts.pid, 0)

    def test_repeated_real_sigint_during_cleanup_preserves_timeout_receipt(self):
        original = os.killpg
        def interrupted(pgid, sig):
            if sig == signal.SIGTERM:
                os.kill(os.getpid(), signal.SIGINT)
            return original(pgid, sig)
        with patch('harness.process_runtime.os.killpg', side_effect=interrupted):
            with self.assertRaises(ProcessTimeout) as caught:
                self.invoke('import time; time.sleep(30)', timeout=.05)
        facts = caught.exception.outcome
        self.assertEqual(facts.termination_reason, 'timeout')
        self.assertTrue(facts.exit_confirmed)
        self.assertEqual(read_json(self.log.with_suffix('.outcome.json'))['termination_reason'], 'timeout')

    def test_injected_interrupt_does_not_replace_timeout(self):
        original = os.killpg
        def interrupted(pgid, sig):
            if sig == signal.SIGTERM:
                raise KeyboardInterrupt('cleanup fault')
            return original(pgid, sig)
        with patch('harness.process_runtime.os.killpg', side_effect=interrupted):
            with self.assertRaises(ProcessTimeout) as caught:
                self.invoke('import time; time.sleep(30)', timeout=.05)
        self.assertEqual(caught.exception.outcome.exit_code, -signal.SIGKILL)
        self.assertTrue(caught.exception.outcome.exit_confirmed)
        self.assertEqual(caught.exception.outcome.cleanup_errors[0]['type'], 'KeyboardInterrupt')

    def test_slow_durable_intent_cannot_start_after_absolute_deadline(self):
        import time
        from harness.util import write_json
        witness = self.root / 'witness'
        def slow(path, value):
            if value['startup_state'] == 'starting':
                time.sleep(.08)
            write_json(path, value)
        with patch('harness.util.write_json', side_effect=slow):
            with self.assertRaises(ProcessTimeout) as caught:
                self.invoke(f'open({str(witness)!r},"w").write("started")',
                            execution_deadline=time.monotonic() + .03)
        self.assertFalse(witness.exists())
        self.assertFalse(caught.exception.outcome.started)
        self.assertEqual(caught.exception.outcome.startup_state, 'not_started')
        self.assertTrue(caught.exception.outcome.exit_confirmed)

    def test_preparation_failure_is_not_started(self):
        self.log.with_suffix('.stdout').mkdir()
        with self.assertRaises(IsADirectoryError) as caught:
            self.invoke('pass')
        self.assertFalse(caught.exception.outcome.started)
        self.assertEqual(caught.exception.outcome.startup_state, 'not_started')

    def test_unknown_fatal_cleanup_error_propagates_after_child_cleanup(self):
        class Fatal(BaseException):
            pass
        original = os.killpg
        def fatal(pgid, sig):
            if sig == signal.SIGTERM:
                raise Fatal('fatal cleanup fault')
            return original(pgid, sig)
        with patch('harness.process_runtime.os.killpg', side_effect=fatal):
            with self.assertRaises(Fatal) as caught:
                self.invoke('import time; time.sleep(30)', timeout=.05)
        self.assertEqual(caught.exception.outcome.termination_reason, 'timeout')
        self.assertTrue(caught.exception.outcome.exit_confirmed)
        self.assertEqual(read_json(self.log.with_suffix('.outcome.json'))['termination_reason'], 'timeout')

    def test_term_resistant_child_cleanup_fits_two_second_margin(self):
        import time
        started = time.monotonic()
        with self.assertRaises(ProcessTimeout) as caught:
            self.invoke('import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)',
                        timeout=.2)
        self.assertLess(time.monotonic() - started, 2.2)
        self.assertEqual(caught.exception.outcome.exit_code, -signal.SIGKILL)
        self.assertTrue(caught.exception.outcome.exit_confirmed)
