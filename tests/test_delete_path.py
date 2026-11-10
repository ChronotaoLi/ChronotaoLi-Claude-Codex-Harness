"""delete_path: in-scope tracked-file deletion through the worker command broker.

Brokers are driven directly, or from simulated workers inside offline runs; no
model CLI is invoked, so nothing here proves a provider can call the tool.
"""
import copy
import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import command_server as cs
from harness import git
from harness.adapters import CLIBackend
from harness.command_server import (DELETE_COMMAND_ID, Broker, command_id, command_processes, create_manifest,
                                    ensure_command_processes_exited, handle)
from harness.config import Config
from harness.continuation import continuation_gate
from harness.util import (HarnessError, ProcessTimeout, ensure_dispatches_exited, read_json,
                          validate_command_directories, write_json)
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend

ECHO = [sys.executable, '-c', 'print("checked")']


def sha(data):
    return hashlib.sha256(data).hexdigest()


def commit_all(repo, message='fixture'):
    git.git(repo, 'add', '.')
    git.git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', '-c', 'commit.gpgsign=false',
            'commit', '-q', '-m', message)


def receipt_of(job, result):
    return read_json(job / 'commands' / result['call_id'].rsplit(':', 1)[1] / 'receipt.json')


def call(broker, name, arguments):
    response = handle({'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}, broker)
    return response['isError'], json.loads(response['content'][0]['text'])


class BrokerFixture(unittest.TestCase):
    """A real Git repository, a sibling outside directory and ledger-less brokers."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.repo = self.base / 'repo'
        files = {'legacy/old.txt': 'old\n', 'legacy/tool.sh': '#!/bin/sh\necho tool\n',
                 'legacy/nested/deep.txt': 'deep\n', 'keep/other.txt': 'keep\n',
                 '.Claude/notes.md': 'alias of a protected root\n'}
        for name, text in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
        git.git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'legacy' / 'tool.sh').chmod(0o755)
        (self.repo / 'legacy' / 'link').symlink_to('old.txt')
        commit_all(self.repo)
        self.head = git.head(self.repo)
        # Same relative names outside the worktree. No case here, the known-limit
        # cases included, moves anything onto them, so each must stay intact.
        self.outside = self.base / 'outside'
        for name in ('legacy/old.txt', 'legacy/nested/deep.txt', 'target.txt'):
            (self.outside / name).parent.mkdir(parents=True, exist_ok=True)
            (self.outside / name).write_text('outside ' + name + '\n')
        self.sentinels = {path: path.read_bytes() for path in self.outside.rglob('*') if path.is_file()}
        self.run_dir = self.base / 'run'
        self.count = 0

    def broker(self, allowed=('legacy/**',), *, scoped=True, commands=(ECHO,)):
        self.count += 1
        self.job = self.run_dir / 'dispatches' / f'worker-{self.count}'
        config = {'verification': {'commands': list(commands)}, 'permissions': {'worker_commands': []},
                  'runtime': {'verify_timeout_seconds': 5, 'agent_timeout_seconds': 30}}
        scope = {'task_id': 'cleanup', 'allowed_paths': list(allowed)} if scoped else None
        meta = create_manifest(config, self.repo, self.job, None, delete_scope=scope)
        return Broker(meta['artifact'], meta['artifact_digest'])

    def parent_entry(self):
        return {'id': self.job.name, 'pgid': None, 'process_state': 'exited', 'exit_confirmed': True,
                'command_directory': str(self.job / 'commands')}

    def assert_outside_intact(self):
        for path, data in self.sentinels.items():
            self.assertTrue(path.is_file() and not path.is_symlink(), path)
            self.assertEqual(path.read_bytes(), data, path)

    def interpose(self, primitive, action):
        """Call action(original, *args) in place of one filesystem primitive."""
        original = getattr(cs, primitive)
        return patch.object(cs, primitive, side_effect=lambda *a, **k: action(original, *a, **k))


class DeletePathBrokerTests(BrokerFixture):
    def test_in_scope_tracked_files_are_deleted_with_durable_receipts(self):
        broker = self.broker()
        for path, mode in (('legacy/old.txt', '100644'), ('legacy/tool.sh', '100755')):
            raw = (self.repo / path).read_bytes()
            result = broker.delete_path({'path': path})
            self.assertEqual(result['outcome'], 'deleted', result)
            self.assertTrue(result['deleted'])
            self.assertEqual((result['sha256'], result['bytes']), (sha(raw), len(raw)))
            self.assertEqual((result['head'], result['head_mode'], result['task_id']), (self.head, mode, 'cleanup'))
            self.assertTrue(result['evidence_complete'] and result['exit_confirmed'])
            self.assertFalse(os.path.lexists(self.repo / path))
            self.assertEqual(receipt_of(self.job, result), result)
            intent = read_json(Path(result['intent_path']))
            self.assertEqual((intent['path'], intent['sha256'], intent['bytes']), (path, sha(raw), len(raw)))
            self.assertLessEqual(result['started_at'], result['finished_at'])
        status = git.git(self.repo, 'status', '--porcelain', '--untracked-files=no').splitlines()
        self.assertEqual(sorted(status), [' D legacy/old.txt', ' D legacy/tool.sh'])
        # Siblings and parent directories are left alone.
        self.assertTrue((self.repo / 'legacy' / 'nested' / 'deep.txt').is_file())
        records = ensure_command_processes_exited(self.job)
        self.assertEqual([(r['command_id'], r['pgid'], r['process_state'], r['exit_confirmed']) for r in records],
                         [(DELETE_COMMAND_ID, None, 'exited', True)] * 2)
        self.assert_outside_intact()

    def test_refusals_are_recorded_and_leave_every_file(self):
        (self.repo / 'legacy' / 'untracked.txt').write_text('untracked\n')
        (self.repo / '.harness').mkdir()
        (self.repo / '.harness' / 'x').write_text('private\n')
        cases = [
            ('keep/other.txt', 'keep/other.txt', 'outside the task scope'),
            ('legacy/untracked.txt', 'legacy/untracked.txt', 'not a file tracked in HEAD'),
            ('legacy/link', 'legacy/link', 'not a regular file in HEAD'),
            ('legacy/nested', 'legacy/nested/deep.txt', 'not a file tracked in HEAD'),
            ('.harness/x', '.harness/x', 'protected path'),
            ('.git/config', '.git/config', 'protected path'),
            (str(self.repo / 'legacy' / 'old.txt'), 'legacy/old.txt', 'absolute path'),
            ('legacy/../legacy/old.txt', 'legacy/old.txt', "'..' segment"),
            ('./legacy/old.txt', 'legacy/old.txt', "'.' or '..' segment"),
            ('legacy//old.txt', 'legacy/old.txt', 'empty'),
            ('', 'legacy/old.txt', 'empty path'),
            ('legacy/old.txt\0', 'legacy/old.txt', 'NUL'),
            ('.HARNESS/x', '.harness/x', 'protected path'),
            ('.Git/config', '.git/config', 'protected path'),
        ]
        broker = self.broker()
        before = {name: os.lstat(self.repo / name) for _, name, _ in cases}
        for path, kept, fragment in cases:
            with self.subTest(path=path):
                result = broker.delete_path({'path': path})
                self.assertEqual(result['outcome'], 'refused', result)
                self.assertFalse(result['deleted'])
                self.assertIn(fragment, result['error'])
                self.assertIsNone(result['intent_path'])
                self.assertEqual(receipt_of(self.job, result), result)
                self.assertEqual(os.lstat(self.repo / kept).st_ino, before[kept].st_ino)
        self.assertTrue((self.repo / 'legacy' / 'link').is_symlink())
        self.assertEqual(len(command_processes(self.job)), len(cases))
        self.assertEqual(git.git(self.repo, 'status', '--porcelain', '--untracked-files=no'), '')

    def test_protected_root_aliases_are_refused_even_when_tracked_and_in_scope(self):
        broker = self.broker(allowed=('**',))
        for path in ('.Claude/notes.md', '.har‌ness/x', '．ｇｉｔ/config', 'legacy/.GIT/x',
                     '.codex./x'):
            with self.subTest(path=path):
                result = broker.delete_path({'path': path})
                self.assertEqual(result['outcome'], 'refused')
                self.assertIn('protected path', result['error'])
        self.assertTrue((self.repo / '.Claude' / 'notes.md').is_file())
        # The same broker still deletes an ordinary tracked file under '**'.
        self.assertEqual(broker.delete_path({'path': 'keep/other.txt'})['outcome'], 'deleted')

    def test_worktree_file_replaced_by_link_is_refused(self):
        deep = self.repo / 'legacy' / 'nested' / 'deep.txt'
        deep.unlink()
        deep.symlink_to(self.outside / 'target.txt')
        result = self.broker().delete_path({'path': 'legacy/nested/deep.txt'})
        self.assertEqual(result['outcome'], 'refused')
        self.assertIn('not a regular file in the worktree', result['error'])
        self.assertTrue(deep.is_symlink())
        self.assert_outside_intact()

    def test_unavailable_without_task_scope_and_hidden_from_tool_list(self):
        unscoped = self.broker(scoped=False)
        self.assertNotIn('delete_path', [tool['name'] for tool in unscoped.tools()])
        with self.assertRaisesRegex(HarnessError, 'no current task scope'):
            unscoped.delete_path({'path': 'legacy/old.txt'})
        failed, payload = call(unscoped, 'delete_path', {'path': 'legacy/old.txt'})
        self.assertTrue(failed)
        self.assertEqual(payload['started'], False)
        self.assertEqual(command_processes(self.job), [])
        scoped = self.broker()
        listed = handle({'method': 'tools/list'}, scoped)['tools']
        self.assertEqual([tool['name'] for tool in listed], ['list_commands', 'run_command', 'delete_path'])
        self.assertEqual(listed[-1]['inputSchema']['required'], ['path'])
        self.assertTrue((self.repo / 'legacy' / 'old.txt').is_file())

    def test_tool_protocol_marks_refusal_as_error_and_rejects_extra_fields_unrecorded(self):
        broker = self.broker()
        failed, payload = call(broker, 'delete_path', {'path': 'keep/other.txt'})
        self.assertTrue(failed)
        self.assertEqual(payload['outcome'], 'refused')
        for arguments in ({'path': 'legacy/old.txt', 'force': True}, {'paths': ['legacy/old.txt']}, {'path': 7},
                          {'path': 'x' * (cs.MAX_DELETE_PATH_CHARS + 1)}):
            with self.subTest(arguments=str(arguments)[:40]):
                failed, payload = call(broker, 'delete_path', arguments)
                self.assertTrue(failed)
                self.assertIn('accepts only path', payload['error'])
        self.assertEqual(len(command_processes(self.job)), 1)
        failed, payload = call(broker, 'delete_path', {'path': 'legacy/old.txt'})
        self.assertFalse(failed)
        self.assertEqual(payload['outcome'], 'deleted')

    def test_commands_and_deletions_share_admission_and_evidence(self):
        broker = self.broker()
        self.assertEqual(broker.run_command({'command_id': command_id(ECHO)})['exit_code'], 0)
        deleted = broker.delete_path({'path': 'legacy/old.txt'})
        self.assertEqual(deleted['outcome'], 'deleted')
        self.assertEqual(broker.run_command({'command_id': command_id(ECHO)})['exit_code'], 0)
        receipt = Path(deleted['intent_path']).with_name('receipt.json')
        write_json(receipt, {**read_json(receipt), 'sha256': '0' * 64, 'evidence_complete': False})
        for attempt in (lambda: broker.run_command({'command_id': command_id(ECHO)}),
                        lambda: broker.delete_path({'path': 'legacy/tool.sh'})):
            with self.assertRaisesRegex(HarnessError, 'evidence incomplete'):
                attempt()
        Path(deleted['intent_path']).write_text('{}')
        with self.assertRaisesRegex(HarnessError, 'evidence incomplete'):
            broker.delete_path({'path': 'legacy/tool.sh'})
        self.assertTrue((self.repo / 'legacy' / 'tool.sh').is_file())

    def test_unconfirmed_earlier_command_blocks_deletion_without_a_record(self):
        broker = self.broker()
        folder = self.job / 'commands' / ('a' * 32)
        folder.mkdir()
        write_json(folder / 'process.json', {'id': self.job.name + ':command:' + folder.name,
                   'command_id': command_id(ECHO), 'pgid': None, 'process_state': 'starting', 'exit_confirmed': False})
        with self.assertRaisesRegex(HarnessError, 'Cannot confirm command process exited'):
            broker.delete_path({'path': 'legacy/old.txt'})
        self.assertEqual(len(command_processes(self.job)), 1)
        self.assertTrue((self.repo / 'legacy' / 'old.txt').is_file())

    def test_admission_lock_held_elsewhere_refuses_without_a_record(self):
        import fcntl
        broker = self.broker()
        with open(self.job / 'command-admission.lock', 'a+') as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(HarnessError):
                broker.delete_path({'path': 'legacy/old.txt'})
        self.assertEqual(command_processes(self.job), [])
        self.assertTrue((self.repo / 'legacy' / 'old.txt').is_file())


class DeletePathRaceTests(BrokerFixture):
    """Concurrent renames interposed at the broker's own filesystem steps."""

    def test_parent_swapped_for_outside_link_before_open_is_refused(self):
        nested = self.repo / 'legacy' / 'nested'
        shutil.rmtree(nested)
        nested.symlink_to(self.outside / 'legacy' / 'nested', target_is_directory=True)
        result = self.broker().delete_path({'path': 'legacy/nested/deep.txt'})
        self.assertEqual(result['outcome'], 'refused')
        self.assertIn("parent 'nested'", result['error'])
        self.assert_outside_intact()

    def test_parent_swapped_at_the_moment_of_opening_is_refused(self):
        nested = self.repo / 'legacy' / 'nested'
        def swap(original, name, flags, dir_fd=None):
            if name == 'nested' and not nested.is_symlink():
                shutil.rmtree(nested)
                nested.symlink_to(self.outside / 'legacy' / 'nested', target_is_directory=True)
            return original(name, flags, dir_fd=dir_fd)
        with self.interpose('_openat', swap):
            result = self.broker().delete_path({'path': 'legacy/nested/deep.txt'})
        self.assertEqual(result['outcome'], 'refused')
        self.assert_outside_intact()

    def test_parent_moved_out_after_open_is_refused(self):
        for replace in (False, True):
            with self.subTest(replaced_inside=replace):
                if replace:
                    git.git(self.repo, 'checkout', '--', 'legacy/nested/deep.txt')
                escaped = self.base / f'escaped-{replace}'
                def move(original, name, flags, dir_fd=None):
                    descriptor = original(name, flags, dir_fd=dir_fd)
                    if name == 'nested':
                        (self.repo / 'legacy' / 'nested').rename(escaped)
                        if replace:
                            (self.repo / 'legacy' / 'nested').mkdir()
                            (self.repo / 'legacy' / 'nested' / 'deep.txt').write_text('new\n')
                    return descriptor
                with self.interpose('_openat', move):
                    result = self.broker().delete_path({'path': 'legacy/nested/deep.txt'})
                self.assertEqual(result['outcome'], 'refused', result)
                self.assertIn('nothing was deleted', result['error'])
                self.assertFalse(result['deleted'])
                self.assertEqual((escaped / 'deep.txt').read_text(), 'deep\n')
                if replace:
                    self.assertEqual((self.repo / 'legacy' / 'nested' / 'deep.txt').read_text(), 'new\n')
                self.assert_outside_intact()

    def replace_final(self, kind):
        old, moved = self.repo / 'legacy' / 'old.txt', self.repo / 'legacy' / 'old.moved'
        old.rename(moved)
        if kind == 'link':
            old.symlink_to(self.outside / 'target.txt')
        else:
            old.mkdir()
            (old / 'inner.txt').write_text('inner\n')
        return old, moved

    def test_final_entry_replaced_after_verification_is_refused(self):
        for kind in ('link', 'directory'):
            with self.subTest(kind=kind):
                seen = []
                def recheck(original, name, dir_fd):
                    # The second inspection of the final name is the pre-unlink re-check.
                    if name == 'old.txt':
                        seen.append(name)
                        if len(seen) == 2:
                            self.replace_final(kind)
                    return original(name, dir_fd)
                with self.interpose('_lstatat', recheck):
                    result = self.broker().delete_path({'path': 'legacy/old.txt'})
                self.assertEqual(result['outcome'], 'refused', result)
                self.assertIn("'old.txt' was replaced", result['error'])
                self.assertFalse(result['deleted'])
                # The intent precedes the final check, so this refusal keeps it.
                intent = read_json(Path(result['intent_path']))
                self.assertEqual((intent['sha256'], result['sha256']), (sha(b'old\n'), sha(b'old\n')))
                self.assertEqual(receipt_of(self.job, result), result)
                old, moved = self.repo / 'legacy' / 'old.txt', self.repo / 'legacy' / 'old.moved'
                self.assertTrue(old.is_symlink() if kind == 'link' else (old / 'inner.txt').is_file())
                self.assertEqual(moved.read_text(), 'old\n')
                self.assert_outside_intact()
                if kind == 'link':
                    old.unlink()
                else:
                    shutil.rmtree(old)
                moved.rename(old)

    def test_final_entry_replaced_in_the_last_window_is_never_followed(self):
        for kind in ('link', 'directory'):
            with self.subTest(kind=kind):
                def swap(original, name, dir_fd):
                    self.replace_final(kind)
                    return original(name, dir_fd)
                with self.interpose('_unlinkat', swap):
                    result = self.broker().delete_path({'path': 'legacy/old.txt'})
                old, moved = self.repo / 'legacy' / 'old.txt', self.repo / 'legacy' / 'old.moved'
                self.assertFalse(result['deleted'], result)
                if kind == 'link':
                    # unlinkat removes the link itself, never its target.
                    self.assertEqual(result['outcome'], 'concurrent_change')
                    self.assertIn('not the verified file', result['error'])
                    self.assertFalse(os.path.lexists(old))
                else:
                    self.assertEqual(result['outcome'], 'failed')
                    self.assertTrue((old / 'inner.txt').is_file())
                    shutil.rmtree(old)
                self.assertEqual(moved.read_text(), 'old\n')
                self.assert_outside_intact()
                moved.rename(old)

    def test_known_limit_parent_left_outside_after_the_last_check_is_not_prevented(self):
        """Known limit, not a guarantee: there is no isolation from concurrent renames.

        A rename between the final chain check and unlinkat moves the verified
        directory out of the worktree before the unlink, so the file is removed
        outside it. Here the directory stays outside and the best-effort
        after-check happens to see that; it is not certain to (see the next case).
        """
        escaped = self.base / 'escaped'
        def move(original, name, dir_fd):
            (self.repo / 'legacy' / 'nested').rename(escaped)
            return original(name, dir_fd)
        with self.interpose('_unlinkat', move):
            result = self.broker().delete_path({'path': 'legacy/nested/deep.txt'})
        self.assertEqual(result['outcome'], 'concurrent_change')
        self.assertTrue(result['deleted'])
        self.assertIn("parent 'nested'", result['error'])
        self.assertEqual(result['sha256'], sha(b'deep\n'))
        self.assertFalse((escaped / 'deep.txt').exists())
        self.assert_outside_intact()

    def test_known_limit_parent_moved_out_and_back_around_unlink_goes_unreported(self):
        """Known limit, not a guarantee: the after-the-fact check can miss a rename.

        The verified parent is moved out of the worktree just before unlinkat
        and moved back just after. The unlink therefore happens outside the
        worktree, yet the after-check finds the original chain and reports no
        concurrent change. This records the actual behaviour.
        """
        parent, escaped = self.repo / 'legacy' / 'nested', self.base / 'escaped'
        observed = []
        def round_trip(original, name, dir_fd):
            parent.rename(escaped)
            observed.append(('outside before unlink', (escaped / name).is_file()))
            try:
                return original(name, dir_fd)
            finally:
                observed.append(('outside after unlink', os.path.lexists(escaped / name)))
                escaped.rename(parent)
        with self.interpose('_unlinkat', round_trip):
            result = self.broker().delete_path({'path': 'legacy/nested/deep.txt'})
        self.assertEqual(observed, [('outside before unlink', True), ('outside after unlink', False)])
        self.assertEqual((result['outcome'], result['deleted'], result['concurrent_change'], result['error']),
                         ('deleted', True, None, None))
        self.assertEqual(result['sha256'], sha(b'deep\n'))
        self.assertEqual(receipt_of(self.job, result), result)
        self.assertTrue(parent.is_dir() and not os.path.lexists(parent / 'deep.txt'))
        self.assert_outside_intact()

    def test_intent_is_durable_before_the_final_check(self):
        """Only the final check's own system calls separate the intent from unlinkat."""
        steps = []
        original_write = cs.write_json
        def write(path, value):
            original_write(path, value)
            if Path(path).name == 'delete-intent.json':
                steps.append('intent')
        def lstat(original, name, dir_fd):
            steps.append('lstat ' + name)
            return original(name, dir_fd)
        def unlink(original, name, dir_fd):
            steps.append('unlink ' + name)
            return original(name, dir_fd)
        with patch.object(cs, 'write_json', side_effect=write), self.interpose('_lstatat', lstat), \
                self.interpose('_unlinkat', unlink):
            result = self.broker().delete_path({'path': 'legacy/nested/deep.txt'})
        self.assertEqual(result['outcome'], 'deleted')
        window = steps[steps.index('intent'):steps.index('unlink deep.txt') + 1]
        self.assertEqual(window, ['intent', 'lstat legacy', 'lstat nested', 'lstat deep.txt', 'unlink deep.txt'])
        # A failed intent write stops before the final check: nothing is unlinked.
        git.git(self.repo, 'checkout', '--', 'legacy/nested/deep.txt')
        def fail_intent(path, value):
            if Path(path).name == 'delete-intent.json':
                raise OSError(28, 'injected intent write failure')
            return original_write(path, value)
        with patch.object(cs, 'write_json', side_effect=fail_intent):
            result = self.broker().delete_path({'path': 'legacy/nested/deep.txt'})
        self.assertEqual((result['outcome'], result['deleted'], result['intent_path']), ('failed', False, None))
        self.assertTrue((self.repo / 'legacy' / 'nested' / 'deep.txt').is_file())
        self.assertEqual(receipt_of(self.job, result), result)


class DeletePathInterruptionTests(BrokerFixture):
    def test_interruption_before_or_after_unlink_leaves_a_complete_record(self):
        for when in ('before', 'after'):
            for error in (KeyboardInterrupt('injected interrupt'), OSError(5, 'injected I/O error')):
                with self.subTest(when=when, error=type(error).__name__):
                    git.git(self.repo, 'checkout', '--', 'legacy/old.txt')
                    raw = (self.repo / 'legacy' / 'old.txt').read_bytes()
                    def interrupt(original, name, dir_fd):
                        if when == 'after':
                            original(name, dir_fd)
                        raise error
                    broker = self.broker()
                    with self.interpose('_unlinkat', interrupt):
                        result = broker.delete_path({'path': 'legacy/old.txt'})
                    self.assertEqual(result['outcome'], 'failed')
                    self.assertIn('injected', result['error'])
                    self.assertEqual(result['deleted'], when == 'after')
                    self.assertEqual((self.repo / 'legacy' / 'old.txt').exists(), when == 'before')
                    self.assertEqual((result['sha256'], result['bytes']), (sha(raw), len(raw)))
                    self.assertTrue(result['evidence_complete'])
                    self.assertEqual(receipt_of(self.job, result), result)
                    # Recovery sees an exited child, never an unknown exit.
                    parent = self.parent_entry()
                    validate_command_directories([parent], self.run_dir)
                    self.assertEqual(ensure_dispatches_exited([parent]), [])
                    # Later calls are admitted and nothing repeats the deletion.
                    self.assertEqual(broker.run_command({'command_id': command_id(ECHO)})['exit_code'], 0)
                    self.assertEqual((self.repo / 'legacy' / 'old.txt').exists(), when == 'before')
                    self.assertEqual(sum(r['command_id'] == DELETE_COMMAND_ID for r in command_processes(self.job)), 1)

    def test_lost_receipt_blocks_only_its_own_dispatch(self):
        broker = self.broker()
        original = write_json
        def crash(path, value):
            if Path(path).name == 'receipt.json':
                raise SystemExit('simulated crash before the receipt')
            return original(path, value)
        with patch('harness.command_server.write_json', side_effect=crash):
            result = broker.delete_path({'path': 'legacy/old.txt'})
        self.assertTrue(result['deleted'])
        self.assertFalse(result['evidence_complete'])
        self.assertIn('simulated crash', result['persistence_error'])
        folder = Path(result['intent_path']).parent
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['delete-intent.json', 'process.json'])
        # Exit is already confirmed: resume and cleanup need no operator assertion.
        self.assertEqual(ensure_dispatches_exited([self.parent_entry()]), [])
        # The same dispatch refuses further calls instead of guessing; nothing is repeated.
        for attempt in (lambda: broker.delete_path({'path': 'legacy/tool.sh'}),
                        lambda: broker.run_command({'command_id': command_id(ECHO)})):
            with self.assertRaisesRegex(HarnessError, 'evidence incomplete'):
                attempt()
        self.assertTrue((self.repo / 'legacy' / 'tool.sh').is_file())
        self.assertEqual(self.broker().delete_path({'path': 'legacy/tool.sh'})['outcome'], 'deleted')

    def test_signal_during_deletion_waits_for_the_receipt(self):
        broker = self.broker()
        def signal_then_unlink(original, name, dir_fd):
            os.kill(os.getpid(), signal.SIGINT)
            return original(name, dir_fd)
        with self.interpose('_unlinkat', signal_then_unlink), self.assertRaises(KeyboardInterrupt):
            broker.delete_path({'path': 'legacy/old.txt'})
        folder = next(r for r in command_processes(self.job))['journal']
        receipt = read_json(Path(folder).with_name('receipt.json'))
        self.assertEqual((receipt['outcome'], receipt['deleted'], receipt['evidence_complete']), ('deleted', True, True))
        self.assertFalse((self.repo / 'legacy' / 'old.txt').exists())
        self.assertEqual(broker.run_command({'command_id': command_id(ECHO)})['exit_code'], 0)


class DeletePathRoleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.config = Config.load(ROOT / 'harness.toml')

    def command(self, role, cli, **kwargs):
        job = self.root / f'{role}-{cli}-{len(list(self.root.iterdir()))}'
        job.mkdir()
        model = {'cli': cli, 'model': 'exact-configured-id', 'effort': 'high'}
        argv = CLIBackend(self.config, self.root / 'state').command(role, model, self.root, job, {}, None, **kwargs)
        return job, argv, read_json(job / 'mcp.json')

    def test_read_only_roles_get_no_delete_tool(self):
        from harness.mcp_server import TOOLS
        self.assertNotIn('delete_path', [tool['name'] for tool in TOOLS])
        for role in ('commander', 'reviewer'):
            for cli in ('claude', 'codex'):
                with self.subTest(role=role, cli=cli):
                    job, argv, mcp = self.command(role, cli)
                    self.assertNotIn('harness_commands', mcp['mcpServers'])
                    self.assertFalse(any('harness_commands' in arg or 'delete_path' in arg for arg in argv))
                    self.assertFalse((job / 'commands.json').exists())

    def test_worker_tool_follows_the_manifest_scope(self):
        job, argv, mcp = self.command('worker', 'claude')
        args = mcp['mcpServers']['harness_commands']['args']
        manifest = args[args.index('--manifest') + 1]
        self.assertIsNone(read_json(Path(manifest))['delete_scope'])
        names = [tool['name'] for tool in Broker(manifest, args[args.index('--sha256') + 1]).tools()]
        self.assertNotIn('delete_path', names)
        scope = {'task_id': 'probe', 'allowed_paths': ['legacy/**']}
        job, argv, mcp = self.command('worker', 'claude', delete_scope=scope)
        args = mcp['mcpServers']['harness_commands']['args']
        broker = Broker(args[args.index('--manifest') + 1], args[args.index('--sha256') + 1])
        self.assertIn('delete_path', [tool['name'] for tool in broker.tools()])
        self.assertEqual({k: broker.manifest['delete_scope'][k] for k in scope}, scope)
        allow = json.loads(argv[argv.index('--settings') + 1])['permissions']['allow']
        self.assertIn('mcp__harness_commands', allow)
        self.assertFalse(any(rule.startswith('Bash(rm') for rule in allow))
        for bad in ({'task_id': 'probe', 'allowed_paths': ['.harness/**']}, {'task_id': '', 'allowed_paths': ['a']},
                    {'task_id': 'probe', 'allowed_paths': []}, ['legacy/**']):
            with self.subTest(scope=bad), self.assertRaisesRegex(HarnessError, 'delete_path task scope'):
                create_manifest(self.config, self.root, self.root / 'bad' / str(id(bad)), None, delete_scope=bad)


class DeletePathLifecycleTests(RepoCase):
    def add_tracked(self, files, executable=()):
        for name, text in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
        for name in executable:
            (self.repo / name).chmod(0o755)
        commit_all(self.repo, 'legacy files')

    def plan(self, *allowed):
        plan = read_json(ROOT / 'examples' / 'plan.json')
        plan['tasks'][0]['allowed_paths'] = ['hello.py', *allowed]
        return plan

    def worker_broker(self, engine, kwargs):
        manifest = create_manifest(self.config, kwargs['cwd'], kwargs['job'], engine.directory / 'state.json')
        return Broker(manifest['artifact'], manifest['artifact_digest'])

    def delete_records(self, run):
        rows = []
        for dispatch in run['dispatches']:
            if dispatch.get('command_directory'):
                for record in command_processes(dispatch['command_directory']):
                    if record['command_id'] == DELETE_COMMAND_ID:
                        rows.append((dispatch['id'], read_json(Path(record['journal']).with_name('receipt.json'))))
        return rows

    def test_deleted_file_reaches_review_and_the_accepted_commit(self):
        self.add_tracked({'legacy/old.txt': 'old\n', 'legacy/keep.txt': 'keep\n'})
        receipts = []
        def delete(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker':
                broker = self.worker_broker(engine, kwargs)
                self.assertEqual({k: broker.manifest['delete_scope'][k] for k in ('task_id', 'allowed_paths')},
                                 {'task_id': 'greet', 'allowed_paths': ['hello.py', 'legacy/**']})
                receipts.append(broker.delete_path({'path': 'legacy/old.txt'}))
        backend = ScriptedBackend(delete)
        engine = self.engine(backend)
        result = engine.start('Implement greet and drop legacy', self.plan('legacy/**'))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([r['outcome'] for r in receipts], ['deleted'])
        review = next(c for c in backend.calls if c['kind'] == 'review')
        self.assertIn('legacy/old.txt', review['packet']['changed_files'])
        diff = Path(review['packet']['diff_artifact']).read_text()
        self.assertIn('deleted file mode 100644', diff)
        self.assertIn('a/legacy/old.txt', diff)
        accepted = result['accepted']['greet']
        self.assertIn('legacy/old.txt', accepted['files'])
        changes = git.git(self.repo, 'show', '--name-status', '--format=', accepted['commit']).splitlines()
        self.assertIn('D\tlegacy/old.txt', changes)
        tree = git.git(self.repo, 'ls-tree', '-r', '--name-only', accepted['commit']).splitlines()
        self.assertNotIn('legacy/old.txt', tree)
        self.assertIn('legacy/keep.txt', tree)
        worker = next(c for c in backend.calls if c['role'] == 'worker')
        routing = worker['packet']['tool_constraints']['command_execution']
        self.assertEqual(routing['delete_tool'], 'mcp__harness_commands__delete_path')
        for call_record in backend.calls:
            if call_record['role'] != 'worker':
                self.assertNotIn('command_execution', call_record['packet'].get('tool_constraints', {}))

    def test_ledger_scope_change_refuses_deletion(self):
        self.add_tracked({'legacy/old.txt': 'old\n'})
        outcomes = []
        def tamper(backend, kwargs, packet, result):
            if kwargs['role'] != 'worker':
                return
            broker = self.worker_broker(engine, kwargs)
            state_path = engine.directory / 'state.json'
            raw = state_path.read_bytes()
            state = json.loads(raw)
            state['tasks'][0]['allowed_paths'] = ['hello.py']
            write_json(state_path, state)
            try:
                with self.assertRaisesRegex(HarnessError, 'no longer matches the run ledger'):
                    broker.delete_path({'path': 'legacy/old.txt'})
            finally:
                state_path.write_bytes(raw)
            outcomes.append(command_processes(kwargs['job']))
        engine = self.engine(ScriptedBackend(tamper))
        result = engine.start('Implement greet', self.plan('legacy/**'))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(outcomes, [[]])
        self.assertTrue((engine.worktree / 'legacy' / 'old.txt').is_file())

    def test_rm_continuation_request_still_stops_as_before(self):
        self.add_tracked({'legacy/old.txt': 'old\n'})
        rm = ['rm', '--', 'legacy/old.txt']
        def request(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker' and sum(c['role'] == 'worker' for c in backend.calls) == 1:
                result.update(status='blocked', blocker_kind='continuation', blockers=['rm was denied'],
                              continuation_request={'kind': 'commands', 'commands': [copy.deepcopy(rm)],
                                                    'minimum_timeout_seconds': 0, 'reason': 'Delete legacy file'})
        backend = ScriptedBackend(request)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', self.plan('legacy/**'))
        events = [json.loads(line)['event'] for line in (engine.directory / 'events.jsonl').read_text().splitlines()]
        self.assertIn('run_stopped', events)
        self.assertEqual(continuation_gate(engine.context)['missing_commands'], [rm])
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)
        self.assertTrue((engine.worktree / 'legacy' / 'old.txt').is_file())

    def test_field_shape_nineteen_deletions_complete_without_continuation(self):
        names = (['probes/ios-pencil/.gitignore', 'probes/ios-pencil/README.md', 'probes/ios-pencil/run.sh']
                 + [f'probes/ios-pencil/Sources/Probe{i}.swift' for i in range(8)]
                 + [f'probes/legacy-notes/{i:02d}.md' for i in range(8)])
        self.assertEqual(len(names), 19)
        self.add_tracked({**{name: f'legacy {name}\n' for name in names}, 'docs/keep.md': 'keep\n'},
                         executable=['probes/ios-pencil/run.sh'])
        def cleanup(backend, kwargs, packet, result):
            if kwargs['role'] != 'worker':
                return
            broker = self.worker_broker(engine, kwargs)
            self.assertIn('delete_path', [tool['name'] for tool in handle({'method': 'tools/list'}, broker)['tools']])
            for name in names:
                failed, payload = call(broker, 'delete_path', {'path': name})
                self.assertFalse(failed, payload)
            failed, payload = call(broker, 'delete_path', {'path': 'docs/keep.md'})
            self.assertTrue(failed)
            result.update(summary='Removed 19 legacy probe files with delete_path')
        backend = ScriptedBackend(cleanup)
        engine = self.engine(backend)
        result = engine.start('legacy_cleanup', self.plan('probes/**'))
        self.assertEqual(result['status'], 'completed')
        events = [json.loads(line)['event'] for line in (engine.directory / 'events.jsonl').read_text().splitlines()]
        self.assertNotIn('run_stopped', events)
        self.assertEqual(result.get('continuation_history', []), [])
        self.assertIsNone(result.get('pending_continuation'))
        self.assertEqual(sum(d['role'] == 'worker' for d in result['dispatches']), 1)
        self.assertEqual(self.config.data['permissions']['worker_commands'], [])
        rows = self.delete_records(result)
        self.assertEqual(sorted(r['path'] for _, r in rows if r['outcome'] == 'deleted'), sorted(names))
        self.assertEqual([r['path'] for _, r in rows if r['outcome'] == 'refused'], ['docs/keep.md'])
        commit = result['accepted']['greet']['commit']
        changes = git.git(self.repo, 'show', '--name-status', '--format=', commit).splitlines()
        self.assertEqual(sorted(line for line in changes if line.startswith('D\t')), sorted('D\t' + n for n in names))
        tree = git.git(self.repo, 'ls-tree', '-r', '--name-only', commit).splitlines()
        self.assertFalse(set(names) & set(tree))
        self.assertIn('docs/keep.md', tree)
        # Deletion never removes the emptied parent directories.
        self.assertTrue((engine.worktree / 'probes' / 'ios-pencil' / 'Sources').is_dir())
        self.assertEqual(list((engine.worktree / 'probes' / 'ios-pencil' / 'Sources').iterdir()), [])

    def interrupted_then_resumed(self, when):
        """A worker interrupted around unlink, then killed; resume starts a fresh worker."""
        self.add_tracked({'legacy/old.txt': 'old\n'})
        def crash(backend, kwargs, packet):
            # Before any designated result exists, like a killed worker CLI.
            if kwargs['role'] != 'worker' or sum(c['role'] == 'worker' for c in backend.calls) != 1:
                return
            broker = self.worker_broker(engine, kwargs)
            original = cs._unlinkat
            def interrupt(name, dir_fd):
                if when == 'after':
                    original(name, dir_fd)
                raise KeyboardInterrupt('injected')
            with patch.object(cs, '_unlinkat', side_effect=interrupt):
                self.assertEqual(broker.delete_path({'path': 'legacy/old.txt'})['outcome'], 'failed')
            raise ProcessTimeout(1, kwargs['job'] / 'cli')
        backend = ScriptedBackend()
        backend.before_result = crash
        engine = self.engine(backend)
        with self.assertRaises(ProcessTimeout):
            engine.start('Implement greet', self.plan('legacy/**'))
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        workers = [d['id'] for d in result['dispatches'] if d['role'] == 'worker']
        self.assertEqual(len(workers), 2)
        # One record, in the interrupted dispatch only: nothing replayed the call.
        self.assertEqual([(d, r['deleted']) for d, r in self.delete_records(result)], [(workers[0], when == 'after')])
        return result

    def test_interruption_before_unlink_does_not_block_resume_or_repeat(self):
        result = self.interrupted_then_resumed('before')
        tree = git.git(self.repo, 'ls-tree', '-r', '--name-only', result['accepted']['greet']['commit']).splitlines()
        self.assertIn('legacy/old.txt', tree)

    def test_interruption_after_unlink_does_not_block_resume_or_repeat(self):
        result = self.interrupted_then_resumed('after')
        # The completed unlink stays in the retained candidate; review and acceptance
        # see it as an ordinary in-scope deletion rather than a call to redo.
        tree = git.git(self.repo, 'ls-tree', '-r', '--name-only', result['accepted']['greet']['commit']).splitlines()
        self.assertNotIn('legacy/old.txt', tree)
        self.assertIn('legacy/old.txt', result['accepted']['greet']['files'])


class DeletePathToolSmokeFixtureTests(unittest.TestCase):
    def test_smoke_delete_cases_hold_against_the_local_broker(self):
        """Checks the live probe's own expectations offline; it is not the live smoke."""
        from harness.tool_smoke import inspect_evidence, prepare_fixture
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            fixture = prepare_fixture(tmp / 'fixture')
            cases = [case for case in fixture['cases'] if case['tool'].endswith('__delete_path')]
            self.assertEqual([case['error'] for case in cases], [False, True])
            config = {'verification': {'commands': []}, 'permissions': {'worker_commands': fixture['commands']},
                      'runtime': {'verify_timeout_seconds': 5}}
            job = tmp / 'job'
            meta = create_manifest(config, Path(fixture['worktree']), job, None, delete_scope=fixture['delete_scope'])
            broker = Broker(meta['artifact'], meta['artifact_digest'])
            events = []
            for index, case in enumerate(cases):
                failed, value = call(broker, 'delete_path', case['input'])
                self.assertEqual(failed, case['error'], value)
                self.assertEqual({k: value[k] for k in case['expected']}, case['expected'])
                events += [{'type': 'assistant', 'message': {'content': [
                               {'type': 'tool_use', 'id': f'c{index}', 'name': case['tool'], 'input': case['input']}]}},
                           {'type': 'user', 'message': {'content': [
                               {'type': 'tool_result', 'tool_use_id': f'c{index}', 'is_error': failed,
                                'content': [{'type': 'text', 'text': json.dumps(value)}]}]}}]
            stream = tmp / 'stream.jsonl'
            stream.write_text('\n'.join(json.dumps(e) for e in events + [{'type': 'result', 'subtype': 'success'}]))
            known = str(Path(fixture['worktree']) / 'known.txt')
            report = inspect_evidence(stream, {**fixture, 'cases': cases, 'commands': [],
                                               'expected_files': {known: fixture['expected_files'][known]}}, job)
            self.assertTrue(report['passed'], report['checks'])
            self.assertIn('one_record_per_delete_call', [check['name'] for check in report['checks']])
