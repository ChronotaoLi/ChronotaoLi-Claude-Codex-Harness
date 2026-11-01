"""Read-only previews must not create evidence even when validation refuses it."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.cleanup import cleanup_verification
from harness.run_cleanup import cleanup_run
from harness.util import HarnessError, readonly_lock


class ReadonlyCleanupTests(unittest.TestCase):
    def test_preview_does_not_create_missing_lock_or_private_controls(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            directory = repo / '.harness/runs/run_test'
            directory.mkdir(parents=True)
            (directory / 'state.json').write_text('{}')
            before = {str(path.relative_to(repo)): path.read_bytes()
                      for path in repo.rglob('*') if path.is_file()}
            for preview in (cleanup_run, cleanup_verification):
                with self.subTest(preview=preview.__name__):
                    with self.assertRaises(HarnessError):
                        preview(repo, 'run_test')
                    after = {str(path.relative_to(repo)): path.read_bytes()
                             for path in repo.rglob('*') if path.is_file()}
                    self.assertEqual(before, after)
                    self.assertFalse((directory / 'run.lock').exists())
                    self.assertFalse((repo / '.harness/.gitignore').exists())

    def test_readonly_lock_preserves_existing_bytes_and_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'run.lock'
            path.write_bytes(b'control bytes')
            with readonly_lock(path):
                self.assertEqual(path.read_bytes(), b'control bytes')
            path.unlink()
            target = Path(temporary) / 'target'
            target.write_bytes(b'private')
            path.symlink_to(target)
            with self.assertRaises(OSError):
                with readonly_lock(path):
                    self.fail('followed symlink')
            self.assertEqual(target.read_bytes(), b'private')
