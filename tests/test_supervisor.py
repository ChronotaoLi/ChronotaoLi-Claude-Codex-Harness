"""One-shot host lifecycle tests using offline subprocess fixtures."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from harness import git
from harness.supervisor import supervise
from harness.util import HarnessError, write_json


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        git.git(self.repo, 'init', '-q')

    def test_launch_failure_before_run_has_operation_receipt(self):
        seen = []
        def launch(argv, **kwargs):
            seen.append((argv, kwargs))
            raise FileNotFoundError('sensitive local path')
        receipt = supervise(self.repo, ['run', 'private goal'], operation_id='op_launch', popen=launch)
        self.assertEqual(receipt['process_observation'], 'launch_failed')
        self.assertIsNone(receipt['run_id'])
        self.assertEqual(receipt['operation_id'], 'op_launch')
        self.assertEqual(len(seen), 1)
        argv, kwargs = seen[0]
        self.assertEqual(argv[:3], [sys.executable, '-m', 'harness'])
        self.assertTrue(kwargs['start_new_session'])
        self.assertNotIn('stdin', kwargs)
        self.assertNotIn('stdout', kwargs)
        self.assertNotIn('stderr', kwargs)
        saved, = self.receipts()
        self.assertEqual(saved, receipt)
        self.assertEqual(saved['launch_error'], 'FileNotFoundError')
        self.assertNotIn('private goal', json.dumps(saved))
        self.assertNotIn('sensitive local path', json.dumps(saved))
        self.assert_receipt_only(saved)
        self.assertFalse((self.repo / '.harness/runs').exists())

    def test_nonzero_exit_and_operation_link_never_restarts_or_relabels_ledger(self):
        launches = []
        def launch(argv, **kwargs):
            launches.append(argv)
            operation = argv[argv.index('--operation-id') + 1]
            state = self.repo / '.harness'
            (state / 'operations').mkdir()
            (state / 'operations' / (operation + '.json')).write_text(json.dumps(dict(operation_id=operation, run_id='run_abc')))
            run = state / 'runs/run_abc'
            run.mkdir(parents=True)
            (run / 'state.json').write_text(json.dumps(dict(id='run_abc', repo=str(self.repo), status='running')))
            return subprocess.Popen([sys.executable, '-c', 'raise SystemExit(7)'], **kwargs)
        receipt = supervise(self.repo, ['resume', 'run_abc'], popen=launch)
        self.assertEqual(len(launches), 1)
        self.assertEqual(receipt['exit_code'], 7)
        self.assertEqual(receipt['run_id'], 'run_abc')
        ledger = json.loads((self.repo / '.harness/runs/run_abc/state.json').read_text())
        self.assertEqual(ledger['status'], 'running')
        saved, = self.receipts()
        self.assertEqual(saved, receipt)
        self.assertEqual((saved['process_observation'], saved['broker_processes']), ('exited', 'unknown'))
        self.assert_receipt_only(saved)

    def test_actual_harness_early_failure_is_not_lost(self):
        # Invalid run ID fails before a run/model invocation; real Harness CLI only.
        receipt = supervise(self.repo, ['resume', 'invalid_run_id'])
        self.assertNotEqual(receipt['exit_code'], 0)
        self.assertEqual(receipt['process_observation'], 'exited')
        self.assertIsNone(receipt['run_id'])
        self.assertEqual(self.receipts(), [receipt])
        self.assert_receipt_only(receipt)

    def test_inbox_commands_are_gone_and_an_old_inbox_file_is_never_touched(self):
        import contextlib
        import io
        from harness import cli
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as shown:
            cli._main(['--help'])
        self.assertEqual(shown.exception.code, 0)
        choices = output.getvalue().split('{', 1)[1].split('}', 1)[0].split(',')
        self.assertIn('supervise', choices)
        self.assertFalse({'notify', 'inbox', 'ack'} & set(choices))
        for command in (['notify', 'run_x'], ['inbox'], ['ack', 'n1', '--run', 'run_x']):
            with self.subTest(command=command[0]), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as refused:
                cli._main(['--repo', str(self.repo), *command])
            self.assertEqual(refused.exception.code, 2)
        inbox = self.repo / '.harness/notifications/inbox.sqlite3'
        inbox.parent.mkdir(parents=True)
        inbox.write_bytes(b'not opened by supervise')
        before = inbox.stat()
        receipt = supervise(self.repo, ['run', 'goal'],
                            popen=lambda argv, **kwargs: subprocess.Popen([sys.executable, '-c', 'raise SystemExit(3)'], **kwargs))
        self.assertEqual(receipt['exit_code'], 3)
        self.assertFalse({'notification_id', 'notification_error'} & set(receipt))
        after = inbox.stat()
        self.assertEqual(inbox.read_bytes(), b'not opened by supervise')
        self.assertEqual((after.st_ino, after.st_mtime_ns, after.st_size), (before.st_ino, before.st_mtime_ns, before.st_size))
        self.assertEqual(sorted(path.name for path in inbox.parent.iterdir()), ['inbox.sqlite3'])

    def test_arbitrary_commands_and_id_override_rejected_before_state_write(self):
        for arguments in (['sh', '-c', 'echo unsafe'], ['run', 'goal', '--operation-id', 'other']):
            with self.assertRaises(HarnessError):
                supervise(self.repo, arguments)
        self.assertFalse((self.repo / '.harness').exists())

    def test_fresh_supervisions_have_fresh_operation_ids_and_duplicate_refused(self):
        def launch(argv, **kwargs):
            raise FileNotFoundError
        a = supervise(self.repo, ['run', 'goal'], popen=launch)
        b = supervise(self.repo, ['run', 'goal'], popen=launch)
        self.assertNotEqual(a['operation_id'], b['operation_id'])
        self.assertNotEqual(a['supervision_id'], b['supervision_id'])
        with self.assertRaises(HarnessError):
            supervise(self.repo, ['run', 'goal'], operation_id=a['operation_id'], popen=launch)

    def test_signal_forwarding_reaps_group_and_records_exit(self):
        handlers = {}
        original_handlers = {signal.SIGTERM: object(), signal.SIGINT: object()}
        class Child:
            pid = 314159
            returncode = -signal.SIGTERM
            def wait(self, timeout=None):
                if timeout is None:
                    handlers[signal.SIGTERM](signal.SIGTERM, None)
                return self.returncode
        def install(signum, handler):
            old = handlers.get(signum, original_handlers[signum])
            handlers[signum] = handler
            return old
        with patch('harness.supervisor.signal.signal', side_effect=install), patch('harness.supervisor.os.killpg') as kill:
            with self.assertRaises(SystemExit):
                supervise(self.repo, ['run', 'goal'], popen=lambda *a, **k: Child())
        self.assertEqual(kill.call_args_list[0].args, (314159, signal.SIGTERM))
        self.assertIs(handlers[signal.SIGTERM], original_handlers[signal.SIGTERM])
        saved, = self.receipts()
        self.assertEqual((saved['exit_code'], saved['process_observation']), (-signal.SIGTERM, 'exited'))
        self.assertEqual(saved['forwarded_signal'], signal.SIGTERM)
        self.assert_receipt_only(saved)

    def test_cli_exit_code_follows_child(self):
        from harness import cli
        real = subprocess.Popen
        launched = []
        def launch(argv, **kwargs):
            # Stand in for the supervised Harness command; no run or model call happens.
            launched.append(argv)
            return real([sys.executable, '-c', 'raise SystemExit(7)'], **kwargs)
        from types import SimpleNamespace
        # Replace only the supervisor's launcher; git and other subprocesses stay real.
        stub = SimpleNamespace(Popen=launch, TimeoutExpired=subprocess.TimeoutExpired)
        with patch('harness.supervisor.subprocess', stub):
            code = cli._main(['--repo', str(self.repo), 'supervise', '--', 'run', 'goal'])
        self.assertEqual(code, 7)
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0][:3], [sys.executable, '-m', 'harness'])
        receipt, = [json.loads(path.read_text()) for path in (self.repo / '.harness/supervision').glob('supervision_*.json')]
        self.assertEqual(receipt['exit_code'], 7)
        self.assertEqual(receipt['process_observation'], 'exited')
        self.assert_receipt_only(receipt)

    def interrupted_launch(self):
        """A real sleeping child whose first wait() is interrupted like Ctrl-C."""
        children = []
        class Interrupted:
            def __init__(self, child):
                self.child, self.pid, self.calls = child, child.pid, 0
            @property
            def returncode(self):
                return self.child.returncode
            def wait(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt
                return self.child.wait(timeout)
        def launch(argv, **kwargs):
            children.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs))
            return Interrupted(children[-1])
        def cleanup():
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()
        self.addCleanup(cleanup)
        return launch

    def receipts(self):
        return [json.loads(path.read_text()) for path in (self.repo / '.harness/supervision').glob('supervision_*.json')]

    def assert_receipt_only(self, receipt):
        """The receipt is the whole observation: no inbox link and no inbox store."""
        self.assertFalse({'notification_id', 'notification_error'} & set(receipt))
        self.assertLessEqual(set(receipt),
                             {'schema_version', 'supervision_id', 'operation_id', 'run_id', 'source',
                              'process_observation', 'observer_connection', 'broker_processes', 'started_at',
                              'observed_at', 'pid', 'exit_code', 'forwarded_signal', 'launch_error'})
        self.assertFalse((self.repo / '.harness/notifications').exists())

    def test_interrupt_propagates_after_receipt_records_reaped_exit(self):
        with self.assertRaises(KeyboardInterrupt) as raised:
            supervise(self.repo, ['run', 'goal'], popen=self.interrupted_launch())
        self.assertFalse(getattr(raised.exception, '__notes__', []))
        receipt, = self.receipts()
        self.assertEqual(receipt['exit_code'], -signal.SIGTERM)
        self.assertEqual(receipt['process_observation'], 'exited')
        self.assert_receipt_only(receipt)

    def test_receipt_write_failure_never_replaces_a_propagating_interrupt(self):
        import io
        from harness import supervisor
        real = supervisor.write_json
        def write(path, value):
            if value.get('observed_at') is not None:
                raise OSError('simulated receipt write failure')
            return real(path, value)
        stderr = io.StringIO()
        with patch('harness.supervisor.write_json', side_effect=write), patch('sys.stderr', stderr):
            with self.assertRaises(KeyboardInterrupt) as raised:
                supervise(self.repo, ['run', 'goal'], popen=self.interrupted_launch())
        notes = raised.exception.__notes__
        self.assertEqual(len(notes), 1)
        self.assertIn('simulated receipt write failure', notes[0])
        self.assertIn('simulated receipt write failure', stderr.getvalue())
        receipt, = self.receipts()
        self.assertEqual(receipt['process_observation'], 'running')
        self.assert_receipt_only(receipt)

    def test_failing_stderr_after_receipt_write_failure_never_replaces_a_propagating_interrupt(self):
        from harness import supervisor
        real = supervisor.write_json
        attempts = []
        class BrokenStderr:
            def write(self, text):
                attempts.append(text)
                raise OSError('simulated stderr failure')
            def flush(self):
                raise OSError('simulated stderr failure')
        def write(path, value):
            if value.get('observed_at') is not None:
                raise OSError('simulated receipt write failure')
            return real(path, value)
        with patch('harness.supervisor.write_json', side_effect=write), patch('sys.stderr', BrokenStderr()):
            with self.assertRaises(KeyboardInterrupt) as raised:
                supervise(self.repo, ['run', 'goal'], popen=self.interrupted_launch())
        self.assertTrue(attempts)
        notes = raised.exception.__notes__
        self.assertEqual(len(notes), 1)
        self.assertIn('simulated receipt write failure', notes[0])
        receipt, = self.receipts()
        self.assertEqual(receipt['process_observation'], 'running')

    def test_receipt_write_failure_without_a_propagating_exception_is_raised(self):
        from harness import supervisor
        real = supervisor.write_json
        def write(path, value):
            if value.get('observed_at') is not None:
                raise OSError('simulated receipt write failure')
            return real(path, value)
        def launch(argv, **kwargs):
            return subprocess.Popen([sys.executable, '-c', 'raise SystemExit(0)'], **kwargs)
        with patch('harness.supervisor.write_json', side_effect=write):
            with self.assertRaisesRegex(OSError, 'simulated receipt write failure'):
                supervise(self.repo, ['run', 'goal'], popen=launch)

    def test_supervisor_storage_failure_after_launch_reaps_child(self):
        children = []
        def launch(*args, **kwargs):
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            children.append(child)
            return child
        calls = 0
        def fail_first_write(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError('synthetic receipt disk failure')
            return write_json(*args)
        try:
            with patch('harness.supervisor.write_json', side_effect=fail_first_write):
                with self.assertRaises(OSError):
                    supervise(self.repo, ['run', 'fixture'], popen=launch)
            self.assertIsNotNone(children[0].poll(), 'supervised child leaked after receipt failure')
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()
