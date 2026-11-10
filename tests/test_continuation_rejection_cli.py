"""CLI routing and private operation records for rejection file inputs."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from harness import git
from harness.cli import _main
from harness.util import HarnessError, read_json


class RejectionCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name).resolve()
        git.git(self.repo, 'init', '-q')

    def invoke(self, arguments, error=None):
        engine = Mock()
        engine.resume.return_value = {'id': 'run_fixture', 'status': 'paused', 'branch': 'fixture', 'worktree': '/tmp/fixture'}
        engine.resume.side_effect = error
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('harness.cli.Config.load'), patch('harness.cli.git.repository', return_value=self.repo), \
                patch('harness.operations.probe_cli', return_value={'path': None, 'version': None}), \
                patch('harness.cli.Runtime', return_value=engine), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = _main(['resume', 'run_fixture', *arguments])
        return code, engine, stdout.getvalue(), stderr.getvalue()

    def test_rejection_argument_routes_path_and_ordinary_resume_omits_it(self):
        code, engine, _, _ = self.invoke(['--reject-continuation', 'input.json'])
        self.assertEqual(code, 0)
        self.assertEqual(engine.resume.call_args.kwargs['reject_continuation'], Path('input.json'))
        code, engine, _, _ = self.invoke([])
        self.assertEqual(code, 0)
        self.assertNotIn('reject_continuation', engine.resume.call_args.kwargs)

    def test_file_input_error_text_is_not_copied_to_operation_record(self):
        secret = 'private operator reason and candidate contents'
        code, engine, _, _ = self.invoke(['--reject-continuation', 'private.json'], HarnessError(secret))
        self.assertEqual(code, 1)
        engine.resume.assert_called_once()
        records = list((self.repo / '.harness/operations').glob('*.json'))
        self.assertEqual(len(records), 1)
        record = read_json(records[0])
        self.assertEqual(record['arguments']['reject_continuation'], 'private.json')
        self.assertEqual(record['error_type'], 'HarnessError')
        self.assertIsNone(record['error'])
        self.assertTrue(record['error_omitted'])
        self.assertNotIn(secret, records[0].read_text())
