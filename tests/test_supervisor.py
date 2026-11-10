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
from harness.notifications import NotificationStore
from harness.supervisor import supervise
from harness.util import HarnessError


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        git.git(self.repo, 'init', '-q')

    def test_launch_failure_before_run_has_operation_notification(self):
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
        with NotificationStore(self.repo, readonly=True) as store:
            row, = store.list_notifications()
            self.assertEqual(row['operation_id'], 'op_launch')
            self.assertIsNone(row['run_id'])
            self.assertNotIn('private goal', json.dumps(row))
            self.assertNotIn('sensitive local path', json.dumps(row))
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
        with NotificationStore(self.repo, readonly=True) as store:
            row, = store.list_notifications()
            self.assertEqual(row['current']['ledger_status'], 'running')
            self.assertEqual(row['current']['process_observation'], 'exited')
            self.assertEqual(row['payload']['broker_processes'], 'unknown')

    def test_actual_harness_early_failure_is_not_lost(self):
        # Invalid run ID fails before a run/model invocation; real Harness CLI only.
        receipt = supervise(self.repo, ['resume', 'invalid_run_id'])
        self.assertNotEqual(receipt['exit_code'], 0)
        self.assertEqual(receipt['process_observation'], 'exited')
        self.assertIsNone(receipt['run_id'])
        with NotificationStore(self.repo, readonly=True) as store:
            self.assertEqual(len(store.list_notifications()), 1)

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
        with NotificationStore(self.repo, readonly=True) as store:
            row, = store.list_notifications()
            self.assertEqual(row['payload']['exit_code'], -signal.SIGTERM)
            self.assertEqual(row['payload']['process_observation'], 'exited')

    def test_persisted_exit_recovers_after_repeated_inbox_handoff_failure(self):
        import sqlite3
        launches = []
        def launch(argv, **kwargs):
            launches.append(argv)
            return subprocess.Popen([sys.executable, '-c', 'raise SystemExit(9)'], **kwargs)
        with patch.object(NotificationStore, 'add_process_observation', side_effect=sqlite3.OperationalError('simulated unavailable inbox')):
            # The inbox failure is recorded, never substituted for the child's exit.
            receipt = supervise(self.repo, ['run', 'goal'], popen=launch)
            self.assertEqual(receipt['exit_code'], 9)
            self.assertEqual(receipt['notification_error'],
                             {'type': 'OperationalError', 'message': 'simulated unavailable inbox'})
            self.assertNotIn('notification_id', receipt)
            receipts = list((self.repo / '.harness/supervision').glob('supervision_*.json'))
            self.assertEqual(len(receipts), 1)
            saved = receipts[0].read_bytes()
            terminal = json.loads(saved)
            self.assertEqual(terminal, receipt)
            self.assertEqual(terminal['process_observation'], 'exited')
            self.assertEqual(terminal['exit_code'], 9)
            for _ in range(2):
                with self.assertRaises(sqlite3.OperationalError):
                    NotificationStore(self.repo)
        with NotificationStore(self.repo) as store:
            row, = store.list_notifications()
            ident = row['id']
            self.assertEqual(row['payload']['exit_code'], 9)
            self.assertEqual(store.add_process_observation(terminal), ident)
            seen = []
            store.deliver_due(seen.append)
            self.assertEqual([r['notification_id'] for r in seen], [ident])
        with NotificationStore(self.repo) as store:
            row, = store.list_notifications()
            self.assertEqual(row['id'], ident)
            self.assertEqual(row['delivery_status'], 'sent')
        self.assertEqual(receipts[0].read_bytes(), saved)
        self.assertEqual(len(launches), 1)

    def test_active_store_recovers_receipt_on_delivery_without_relaunch(self):
        import sqlite3
        with NotificationStore(self.repo) as active:
            with patch('harness.supervisor.NotificationStore', side_effect=sqlite3.OperationalError):
                receipt = supervise(self.repo, ['run', 'goal'], popen=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
            self.assertEqual(receipt['process_observation'], 'launch_failed')
            self.assertEqual(receipt['notification_error']['type'], 'OperationalError')
            for _ in range(2):
                with patch.object(active, '_insert', side_effect=sqlite3.OperationalError):
                    with self.assertRaises(sqlite3.OperationalError):
                        active.deliver_due(lambda _: None)
            seen = []
            active.deliver_due(seen.append)
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0]['process_observation'], 'launch_failed')
            active.deliver_due(seen.append)
            self.assertEqual(len(seen), 1)

    def test_cli_exit_code_follows_child_when_inbox_cannot_open(self):
        import sqlite3
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
        with patch('harness.supervisor.subprocess', stub), \
                patch('harness.supervisor.NotificationStore', side_effect=sqlite3.OperationalError('inbox locked')):
            code = cli._main(['--repo', str(self.repo), 'supervise', '--', 'run', 'goal'])
        self.assertEqual(code, 7)
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0][:3], [sys.executable, '-m', 'harness'])
        receipt, = [json.loads(path.read_text()) for path in (self.repo / '.harness/supervision').glob('supervision_*.json')]
        self.assertEqual(receipt['exit_code'], 7)
        self.assertEqual(receipt['process_observation'], 'exited')
        self.assertEqual(receipt['notification_error'], {'type': 'OperationalError', 'message': 'inbox locked'})
        self.assertNotIn('notification_id', receipt)

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

    def test_inbox_failure_never_replaces_a_propagating_interrupt(self):
        import sqlite3
        with patch('harness.supervisor.NotificationStore', side_effect=sqlite3.OperationalError('inbox locked')):
            with self.assertRaises(KeyboardInterrupt):
                supervise(self.repo, ['run', 'goal'], popen=self.interrupted_launch())
        receipt, = self.receipts()
        self.assertEqual(receipt['exit_code'], -signal.SIGTERM)
        self.assertEqual(receipt['process_observation'], 'exited')
        self.assertEqual(receipt['notification_error']['type'], 'OperationalError')

    def test_receipt_write_failure_after_inbox_failure_never_replaces_a_propagating_interrupt(self):
        import io
        import sqlite3
        from harness import supervisor
        real = supervisor.write_json
        # Fail only the final write that carries notification_error, or every write in
        # the cleanup once the observation time is set.
        for failing in ('notification_error', 'observed_at'):
            with self.subTest(failing=failing):
                self.setUp()
                def write(path, value, failing=failing):
                    if value.get(failing) is not None:
                        raise OSError('simulated receipt write failure')
                    return real(path, value)
                stderr = io.StringIO()
                with patch('harness.supervisor.NotificationStore', side_effect=sqlite3.OperationalError('inbox locked')), \
                        patch('harness.supervisor.write_json', side_effect=write), patch('sys.stderr', stderr):
                    with self.assertRaises(KeyboardInterrupt) as raised:
                        supervise(self.repo, ['run', 'goal'], popen=self.interrupted_launch())
                notes = getattr(raised.exception, '__notes__', [])
                self.assertTrue(notes)
                self.assertTrue(all('simulated receipt write failure' in note for note in notes))
                self.assertIn('simulated receipt write failure', stderr.getvalue())
                receipt, = self.receipts()
                self.assertNotIn('notification_error', receipt)
                if failing == 'notification_error':
                    self.assertEqual(len(notes), 1)
                    self.assertEqual(receipt['process_observation'], 'exited')
                    self.assertEqual(receipt['exit_code'], -signal.SIGTERM)
                else:
                    self.assertEqual(len(notes), 2)
                    self.assertEqual(receipt['process_observation'], 'running')

    def test_failing_stderr_after_receipt_write_failure_never_replaces_a_propagating_interrupt(self):
        import sqlite3
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
        with patch('harness.supervisor.NotificationStore', side_effect=sqlite3.OperationalError('inbox locked')), \
                patch('harness.supervisor.write_json', side_effect=write), patch('sys.stderr', BrokenStderr()):
            with self.assertRaises(KeyboardInterrupt) as raised:
                supervise(self.repo, ['run', 'goal'], popen=self.interrupted_launch())
        self.assertTrue(attempts)
        notes = raised.exception.__notes__
        self.assertEqual(len(notes), 2)
        self.assertTrue(all('simulated receipt write failure' in note for note in notes))
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
