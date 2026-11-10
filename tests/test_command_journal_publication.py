"""Command and delete_path journals become visible only when complete.

Every exit gate reads every folder in a dispatch's commands/ directory. A call
folder is therefore prepared with its whole journal outside that directory and
published by one rename. These tests break the window before that rename, by a
failed journal write or an interrupt, and check that nothing was deleted or
started, that commands/ holds no partial folder, and that resume, confirmed
resume and abandon all still work. Brokers run in process; no model CLI is
invoked.
"""
import contextlib
import io
import shutil
from pathlib import Path
from unittest.mock import patch

import test_delete_path
from harness import cli, git
from harness import command_server as cs
from harness.cleanup import cleanup_verification
from harness.command_server import (DELETE_COMMAND_ID, STAGING_DIRECTORY, command_id, command_processes,
                                    ensure_command_processes_exited)
from harness.run_cleanup import cleanup_run
from harness.util import (ProcessTimeout, confirm_dispatches_exited, ensure_dispatches_exited, read_json,
                          validate_command_directories)
from test_delete_path import ECHO, BrokerFixture
from test_harness import RepoCase
from test_runtime_changes import ScriptedBackend


def fail_journal_write(error):
    """Make every process.json write by the broker raise ``error``."""
    original = cs.write_json
    def write(path, value):
        if Path(path).name == 'process.json':
            raise error
        return original(path, value)
    return patch.object(cs, 'write_json', side_effect=write)


def interrupt_publication():
    """Interrupt after the staged journal is complete, before the publishing rename."""
    return patch.object(cs, '_rename', side_effect=KeyboardInterrupt('injected before publication'))


INJECTIONS = {'journal_write_oserror': (lambda: fail_journal_write(OSError(28, 'injected no space')), OSError),
              'interrupt_before_publication': (interrupt_publication, KeyboardInterrupt)}
# Like a kill after the folder exists but before its journal is written.
BROKER_INJECTIONS = {**INJECTIONS, 'journal_write_interrupt': (
    lambda: fail_journal_write(KeyboardInterrupt('injected during the journal write')), KeyboardInterrupt)}
EXITS = ('resume', 'resume_confirm_exited', 'abandon')


class BrokerPublicationWindowTests(BrokerFixture):
    def assert_no_partial_record(self):
        self.assertEqual(list((self.job / 'commands').iterdir()), [])
        self.assertEqual(command_processes(self.job), [])
        parent = self.parent_entry()
        validate_command_directories([parent], self.run_dir)
        self.assertEqual(ensure_dispatches_exited([parent]), [])
        audits = []
        confirm_dispatches_exited([parent], [parent['id']], audit=lambda *a, **k: audits.append(a), save=lambda: None)
        self.assertEqual(audits, [])

    def test_delete_window_failure_deletes_nothing_and_publishes_nothing(self):
        for name, (inject, error) in BROKER_INJECTIONS.items():
            with self.subTest(injection=name):
                broker = self.broker()
                original = self.repo / 'legacy' / 'old.txt'
                inode = original.stat().st_ino
                with inject(), self.assertRaises(error):
                    broker.delete_path({'path': 'legacy/old.txt'})
                self.assertEqual(original.stat().st_ino, inode)
                self.assert_no_partial_record()
                # The same dispatch is not blocked: the next call deletes, once.
                result = broker.delete_path({'path': 'legacy/old.txt'})
                self.assertEqual((result['outcome'], result['deleted'], result['evidence_complete']),
                                 ('deleted', True, True))
                self.assertEqual([r['command_id'] for r in ensure_command_processes_exited(self.job)],
                                 [DELETE_COMMAND_ID])
                self.assert_outside_intact()
                git.git(self.repo, 'checkout', '--', 'legacy/old.txt')

    def test_command_window_failure_starts_nothing_and_publishes_nothing(self):
        for name, (inject, error) in BROKER_INJECTIONS.items():
            with self.subTest(injection=name):
                broker = self.broker()
                with inject(), patch.object(cs, 'run_process') as runner, self.assertRaises(error):
                    broker.run_command({'command_id': command_id(ECHO)})
                runner.assert_not_called()
                self.assert_no_partial_record()
                result = broker.run_command({'command_id': command_id(ECHO)})
                self.assertEqual((result['exit_code'], result['exit_confirmed'], result['evidence_complete']),
                                 (0, True, True))

    def test_staged_leftovers_are_not_command_records(self):
        broker = self.broker()
        with fail_journal_write(OSError(28, 'injected')), self.assertRaises(OSError):
            broker.delete_path({'path': 'legacy/old.txt'})
        with interrupt_publication(), self.assertRaises(KeyboardInterrupt):
            broker.run_command({'command_id': command_id(ECHO)})
        staged = sorted((self.job / STAGING_DIRECTORY).iterdir())
        # One folder lost its journal write; the other holds a whole, unpublished journal.
        self.assertEqual(sorted(any(folder.iterdir()) for folder in staged), [False, True])
        self.assertEqual(command_processes(self.job), [])
        self.assertEqual(broker.run_command({'command_id': command_id(ECHO)})['exit_code'], 0)
        self.assertEqual(broker.delete_path({'path': 'legacy/old.txt'})['outcome'], 'deleted')
        self.assertEqual(sorted(r['command_id'] for r in ensure_command_processes_exited(self.job)),
                         sorted([command_id(ECHO), DELETE_COMMAND_ID]))
        self.assertEqual(len(list((self.job / STAGING_DIRECTORY).iterdir())), 2)


class LifecyclePublicationWindowTests(RepoCase):
    """A worker's broker call breaks in the window, the run stops, and one exit is taken."""

    # Shared helpers only; importing that TestCase here would run its tests twice.
    add_tracked = test_delete_path.DeletePathLifecycleTests.add_tracked
    plan = test_delete_path.DeletePathLifecycleTests.plan
    worker_broker = test_delete_path.DeletePathLifecycleTests.worker_broker
    delete_records = test_delete_path.DeletePathLifecycleTests.delete_records

    def setUp(self):
        super().setUp()
        self.config.data['permissions']['worker_commands'] = [ECHO]
        self.add_tracked({'legacy/old.txt': 'old\n'})

    def call(self, broker, operation):
        if operation == 'delete':
            return broker.delete_path({'path': 'legacy/old.txt'})
        return broker.run_command({'command_id': command_id(ECHO)})

    def stopped_run(self, operation, injection):
        inject, error = INJECTIONS[injection]
        later = []
        def worker(backend, kwargs, packet):
            if kwargs['role'] != 'worker':
                return
            broker = self.worker_broker(engine, kwargs)
            if sum(c['role'] == 'worker' for c in backend.calls) > 1:
                later.append(self.call(broker, operation))
                return
            with inject():
                try:
                    # An OSError ends the worker call, as in the reviewer's probe.
                    self.call(broker, operation)
                except KeyboardInterrupt:
                    # The broker process died; its worker CLI is then killed.
                    raise ProcessTimeout(1, kwargs['job'] / 'cli')
        backend = ScriptedBackend()
        backend.before_result = worker
        engine = self.engine(backend)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises((error, ProcessTimeout)):
            engine.start('Implement greet and drop legacy', self.plan('legacy/**'))
        first = next(d for d in read_json(engine.directory / 'state.json')['dispatches'] if d['role'] == 'worker')
        job = Path(first['command_directory']).parent
        self.assertEqual(list((job / 'commands').iterdir()), [])
        self.assertEqual(len(list((job / STAGING_DIRECTORY).iterdir())), 1)
        self.assertTrue((engine.worktree / 'legacy' / 'old.txt').is_file())
        return engine, backend, first, later

    def assert_terminal_views_ignore_staging(self, run, first):
        job = Path(first['command_directory']).parent
        self.assertTrue((job / STAGING_DIRECTORY).is_dir())
        self.assertEqual(command_processes(job), [])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--repo', str(self.repo), 'status', run['id']]), 0)
        self.assertEqual(cleanup_verification(self.repo, run['id'])['checkouts'], [])
        # The simulated verification leaves only these known test caches.
        shutil.rmtree(Path(run['worktree']) / '__pycache__', ignore_errors=True)
        self.assertEqual(cleanup_run(self.repo, run['id'])['status'], 'eligible')
        self.assertEqual(cleanup_run(self.repo, run['id'], remove=True)['status'], 'removed')

    def check_exit(self, operation, injection, exit_name):
        engine, backend, first, later = self.stopped_run(operation, injection)
        with contextlib.redirect_stderr(io.StringIO()):
            if exit_name == 'abandon':
                run = self.engine(backend).abandon(engine.run['id'])
            else:
                confirm = [first['id']] if exit_name == 'resume_confirm_exited' else ()
                run = self.engine(backend).resume(engine.run['id'], confirm_exited=confirm)
        if exit_name == 'abandon':
            self.assertEqual(run['status'], 'abandoned')
            self.assertEqual(later, [])
            self.assertTrue((engine.worktree / 'legacy' / 'old.txt').is_file())
        else:
            # A fresh dispatch's call works normally, exactly once.
            self.assertEqual(run['status'], 'completed')
            self.assertEqual(len(later), 1)
            if operation == 'delete':
                self.assertEqual((later[0]['outcome'], later[0]['deleted']), ('deleted', True))
                tree = git.git(self.repo, 'ls-tree', '-r', '--name-only', run['accepted']['greet']['commit'])
                self.assertNotIn('legacy/old.txt', tree.splitlines())
                self.assertEqual([r['outcome'] for _, r in self.delete_records(run)], ['deleted'])
            else:
                self.assertEqual((later[0]['exit_code'], later[0]['exit_confirmed']), (0, True))
        self.assert_terminal_views_ignore_staging(run, first)

    def test_delete_journal_write_failure_leaves_every_exit_open(self):
        for exit_name in EXITS:
            with self.subTest(exit=exit_name):
                self.check_exit('delete', 'journal_write_oserror', exit_name)

    def test_delete_interrupt_before_publication_leaves_every_exit_open(self):
        for exit_name in EXITS:
            with self.subTest(exit=exit_name):
                self.check_exit('delete', 'interrupt_before_publication', exit_name)

    def test_command_journal_write_failure_leaves_every_exit_open(self):
        for exit_name in EXITS:
            with self.subTest(exit=exit_name):
                self.check_exit('command', 'journal_write_oserror', exit_name)

    def test_command_interrupt_before_publication_leaves_every_exit_open(self):
        for exit_name in EXITS:
            with self.subTest(exit=exit_name):
                self.check_exit('command', 'interrupt_before_publication', exit_name)
