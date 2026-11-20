import errno
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import git, state_files, stops
from harness.private_state import CONTROL_LIMIT, ensure_private_state
from harness.state_files import StateReadError
from harness.util import HarnessError
from test_provider_failures import FifoWatchdog
from test_state_files import FakeOs, failing


class PrivateStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve() / 'repo'
        self.repo.mkdir()
        self.command('init', '-q')
        self.command('config', 'user.email', 'test@example.com')
        self.command('config', 'user.name', 'Test')
        (self.repo / 'file').write_text('base')
        self.command('add', 'file')
        self.command('commit', '-qm', 'initial')

    def command(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True).stdout

    def test_negations_and_future_nested_rules(self):
        (self.repo / '.gitignore').write_text('*\n!*/\n!AGENTS.md\n!.harness/\n!.harness/**\n')
        index = (self.repo / '.git/index').read_bytes()
        exclude = self.repo / '.git/info/exclude'
        original = b'# custom bytes\xff'
        exclude.write_bytes(original)
        ensure_private_state(self.repo)
        nested = self.repo / '.harness/worktrees/example'
        nested.mkdir(parents=True)
        (nested / '.gitignore').write_text('!AGENTS.md\n')
        (nested / 'AGENTS.md').write_text('secret')
        self.assertEqual(git.git(self.repo, 'ls-files', '--others', '--exclude-standard', '--', '.harness'), '')
        saved = exclude.read_bytes()
        self.assertTrue(saved.startswith(original))
        ensure_private_state(self.repo)
        self.assertEqual(saved, exclude.read_bytes())
        self.assertEqual(index, (self.repo / '.git/index').read_bytes())

    def test_conflicting_control_preserved(self):
        root = self.repo / '.harness'
        root.mkdir()
        control = root / '.gitignore'
        control.write_bytes(b'*\n!secret\n')
        exclude = (self.repo / '.git/info/exclude').read_bytes()
        with self.assertRaises(HarnessError):
            ensure_private_state(self.repo)
        self.assertEqual(control.read_bytes(), b'*\n!secret\n')
        self.assertEqual(exclude, (self.repo / '.git/info/exclude').read_bytes())

    def test_tracked_even_after_index_deletion_rejected(self):
        root = self.repo / '.harness'
        root.mkdir()
        (root / 'secret').write_text('secret')
        self.command('add', '-f', '.harness/secret')
        with self.assertRaises(HarnessError):
            ensure_private_state(self.repo)
        self.command('commit', '-qm', 'private')
        self.command('rm', '--cached', '.harness/secret')
        with self.assertRaises(HarnessError):
            ensure_private_state(self.repo)

    def test_symlink_root_and_control_rejected(self):
        outside = Path(self.temp.name).resolve() / 'outside'
        outside.mkdir()
        root = self.repo / '.harness'
        root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(HarnessError):
            ensure_private_state(self.repo)
        root.unlink()
        root.mkdir()
        (outside / 'rules').write_text('*\n')
        (root / '.gitignore').symlink_to(outside / 'rules')
        with self.assertRaises(HarnessError):
            ensure_private_state(self.repo)

    def test_the_control_file_is_read_once_without_waiting_following_or_reading_past_its_limit(self):
        root = self.repo / '.harness'
        root.mkdir()
        control = root / '.gitignore'
        exclude = self.repo / '.git/info/exclude'
        before = exclude.read_bytes()
        os.mkfifo(control)
        watchdog = FifoWatchdog(control, bound=5)
        try:
            with self.assertRaisesRegex(HarnessError, '私有状态控制路径类型无效'):
                ensure_private_state(self.repo)
        finally:
            self.assertFalse(watchdog.stop())
        control.unlink()
        # Past the limit is a conflict even when the rules beyond it are only comments; the bytes stay.
        large = b'*\n' + b'#' * CONTROL_LIMIT + b'\n'
        control.write_bytes(large)
        with self.assertRaisesRegex(HarnessError, '私有忽略控制文件冲突'):
            ensure_private_state(self.repo)
        self.assertEqual(control.read_bytes(), large)
        control.write_bytes(b'*\n' + b'#' * (CONTROL_LIMIT - 3) + b'\n')
        ensure_private_state(self.repo)
        # A read that fails is local I/O, never a missing control file that would be written anew.
        control.write_bytes(b'*\n')
        exclude.write_bytes(before)
        with patch.object(state_files, 'os', FakeOs(open=failing(errno.EIO, '.gitignore'))), \
                self.assertRaises(StateReadError) as caught:
            ensure_private_state(self.repo)
        self.assertEqual(stops.of(caught.exception)['kind'], 'runtime_failure')
        self.assertEqual((control.read_bytes(), exclude.read_bytes()), (b'*\n', before))

    def test_linked_checkout_and_external_state(self):
        linked = Path(self.temp.name).resolve() / 'linked'
        self.command('worktree', 'add', '-qb', 'linked', str(linked))
        ensure_private_state(linked)
        self.assertEqual((linked / '.harness/.gitignore').read_bytes(), b'*\n')
        state = Path(self.temp.name).resolve() / 'custom'
        self.assertEqual(ensure_private_state(self.repo, state), state)
        self.assertTrue(state.is_dir())
