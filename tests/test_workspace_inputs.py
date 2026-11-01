import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from harness import git
from harness.util import HarnessError, write_json
from harness.workspace_inputs import prepare_run, validate_inputs, cleanup_inputs

class WorkspaceInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve() / 'repo'
        self.repo.mkdir()
        git.git(self.repo, 'init')
        git.git(self.repo, 'config', 'user.email', 'test@example.com')
        git.git(self.repo, 'config', 'user.name', 'Test')
        (self.repo / '.gitignore').write_text('.harness/\nvendor/\ndeps/\n')
        git.git(self.repo, 'add', '.gitignore')
        git.git(self.repo, 'commit', '-m', 'initial')
        self.ident = 'run_test'
        self.directory = self.repo / '.harness/runs' / self.ident
        self.directory.mkdir(parents=True)
        self.checkout = self.repo / '.harness/worktrees' / self.ident
        git.git(self.repo, 'worktree', 'add', '-b', 'harness/' + self.ident, str(self.checkout))
        self.run = dict(schema_version=12, workspace_inputs=None, id=self.ident,
                        repo=str(self.repo), worktree=str(self.checkout), status='paused',
                        expected_head=git.head(self.checkout), dispatches=[],
                        worktree_binding=git.capture_worktree_binding(self.checkout, 'harness/' + self.ident))
        self.save()
        (self.repo / 'deps').mkdir()
        self.source = self.repo / 'deps/input.txt'
        self.source.write_text('dependency')
        self.manifest = dict(schema_version=1, run_id=self.ident, head=self.run['expected_head'],
                             source_root='.', max_files=1, max_bytes=100,
                             files=[dict(source='deps/input.txt', destination='vendor/input.txt',
                                         sha256=hashlib.sha256(b'dependency').hexdigest(),mode=0o644,
                                         type='file', ownership='input_copy')])
    def save(self):
        write_json(self.directory / 'state.json', self.run)
    def prepare(self):
        result = prepare_run(self.repo, self.ident, self.manifest)
        import json
        self.run = json.loads((self.directory / 'state.json').read_text())
        return result
    def test_copy_and_terminal_cleanup(self):
        self.prepare()
        target = self.checkout / 'vendor/input.txt'
        self.assertNotEqual(target.stat().st_ino, self.source.stat().st_ino)
        self.prepare()
        validate_inputs(self.run, self.directory)
        self.run['status'] = 'completed'
        cleanup_inputs(self.run, self.directory)
        self.assertTrue(target.exists())
        cleanup_inputs(self.run, self.directory, remove=True)
        cleanup_inputs(self.run, self.directory, remove=True)
        self.assertFalse(target.exists())
        self.assertTrue(self.source.exists())
    def test_unknown_collision_and_escape(self):
        target = self.checkout / 'vendor/input.txt'
        target.parent.mkdir()
        target.write_text('dependency')
        with self.assertRaises(HarnessError): self.prepare()
        self.assertEqual(target.read_text(), 'dependency')
        target.unlink()
        self.manifest['files'][0]['source'] = '../outside'
        with self.assertRaises(HarnessError): self.prepare()
    def test_modified_copy_and_source_retained(self):
        self.prepare()
        target = self.checkout / 'vendor/input.txt'
        target.write_text('changed')
        with self.assertRaises(HarnessError): validate_inputs(self.run, self.directory)
        self.run['status'] = 'abandoned'
        with self.assertRaises(HarnessError): cleanup_inputs(self.run, self.directory, remove=True)
        self.assertTrue(target.exists())
    def test_missing_authority_blocks(self):
        self.prepare()
        (self.directory / 'workspace-inputs-manifest.json').unlink()
        with self.assertRaises((HarnessError, OSError)): validate_inputs(self.run, self.directory)
    def test_source_hardlink_rejected(self):
        os.link(self.source, self.repo / 'deps/alias')
        with self.assertRaises(HarnessError): self.prepare()
    def test_pending_stage_rejected(self):
        self.run['pending_task'] = {'stage':'review'}
        self.save()
        with self.assertRaises(HarnessError): self.prepare()
    def test_source_change_blocks_cleanup(self):
        self.prepare()
        self.source.write_text('new dependency')
        self.run['status'] = 'completed'
        with self.assertRaises(HarnessError): cleanup_inputs(self.run, self.directory, remove=True)
        self.assertTrue((self.checkout / 'vendor/input.txt').exists())
    def test_publish_crash_reconciles_saved_inode(self):
        import harness.workspace_inputs as inputs
        original = inputs._unlink
        def interrupted(path):
            if path.name.startswith('.harness-input-'):
                raise RuntimeError('crash after publication')
            return original(path)
        with patch.object(inputs, '_unlink', side_effect=interrupted):
            with self.assertRaises(RuntimeError): self.prepare()
        self.prepare()
        validate_inputs(self.run, self.directory)
    def test_unknown_replacement_after_publish_not_claimed(self):
        import harness.workspace_inputs as inputs
        with patch.object(inputs, '_publish', side_effect=RuntimeError('crash before publication')):
            with self.assertRaises(RuntimeError): self.prepare()
        target = self.checkout / 'vendor/input.txt'
        target.write_text('dependency')
        with self.assertRaises(HarnessError): self.prepare()
        self.assertTrue(target.exists())
    def test_source_symlink_rejected(self):
        self.source.rename(self.source.with_name('real'))
        self.source.symlink_to('real')
        with self.assertRaises(HarnessError): self.prepare()

    def test_cleanup_selection_flag_alone_is_readonly_preview(self):
        from harness.run_cleanup import cleanup_run
        self.prepare()
        self.run.update(status='completed', branch='harness/' + self.ident)
        self.save()
        target = self.checkout / 'vendor/input.txt'
        state_bytes = (self.directory / 'state.json').read_bytes()
        receipt_bytes = (self.directory / 'workspace-inputs.json').read_bytes()
        result = cleanup_run(self.repo, self.ident, remove_input_copies=True)
        self.assertEqual(result['status'], 'input_copies_present')
        self.assertTrue(target.exists())
        self.assertEqual((self.directory / 'state.json').read_bytes(), state_bytes)
        self.assertEqual((self.directory / 'workspace-inputs.json').read_bytes(), receipt_bytes)
        result = cleanup_run(self.repo, self.ident, remove=True, remove_input_copies=True)
        self.assertEqual(result['status'], 'removed')
        self.assertFalse(self.checkout.exists())
        self.assertEqual(self.source.read_text(), 'dependency')

    def test_recovery_preserves_replaced_temp_and_destination_hardlinks(self):
        import harness.workspace_inputs as inputs
        import json
        with patch.object(inputs, '_unlink', side_effect=RuntimeError('after link')):
            with self.assertRaises(RuntimeError): self.prepare()
        receipt = json.loads((self.directory / 'workspace-inputs.json').read_text())['files'][0]
        temp = Path(receipt['temp'])
        target = self.checkout / 'vendor/input.txt'
        temp.unlink(); target.unlink()
        temp.write_text('dependency')
        os.link(temp, target)
        with self.assertRaises(HarnessError): self.prepare()
        self.assertTrue(temp.exists(), 'unknown temporary file must be preserved')
        self.assertTrue(target.exists())

    def test_prepare_refuses_symlink_run_lock_before_copy(self):
        outside = self.repo.parent / 'outside-lock'
        outside.write_text('keep')
        (self.directory / 'run.lock').symlink_to(outside)
        with self.assertRaises(HarnessError): self.prepare()
        self.assertFalse((self.checkout / 'vendor/input.txt').exists())
        self.assertEqual(outside.read_text(), 'keep')

    def test_fifo_source_is_rejected_without_waiting_for_writer(self):
        import subprocess
        import sys
        self.source.unlink()
        os.mkfifo(self.source)
        code = ('from pathlib import Path; from harness.workspace_inputs import _identity; '
                '_identity(Path(__import__("sys").argv[1]))')
        result = subprocess.run([sys.executable, '-c', code, str(self.source)],
                                capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ordinary independent file required', result.stderr)

    def test_fifo_replaces_registered_copy_and_resume_gate_returns(self):
        import subprocess
        import sys
        self.prepare()
        target = self.checkout / 'vendor/input.txt'
        target.unlink()
        os.mkfifo(target)
        code = ('import json,sys; from pathlib import Path; '
                'from harness.workspace_inputs import validate_inputs; p=Path(sys.argv[1]); '
                'validate_inputs(json.loads((p/"state.json").read_text()),p)')
        result = subprocess.run([sys.executable, '-c', code, str(self.directory)],
                                capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ordinary independent file required', result.stderr)
