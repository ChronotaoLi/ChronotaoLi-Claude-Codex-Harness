import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.file_tools import file_diff, file_sha256, json_check, MAX_DIFF_CHARS
from harness.mcp_server import handle, TOOLS
from harness.util import HarnessError


class FileToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def put(self, name, data):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_hash_relative_absolute_and_resolved_link(self):
        path = self.put('untracked.json', b'{"hello": [true, null, 42]}')
        (self.base / 'link').symlink_to(path)
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(file_sha256('link', self.base)['sha256'], expected)
        self.assertEqual(file_sha256(str(path), Path('/'))['sha256'], expected)
        result = json_check('untracked.json', self.base)
        self.assertTrue(result['valid'])
        self.assertEqual(result['root_type'], 'object')
        self.assertEqual(result['value_count'], 5)

    def test_invalid_json_is_evidence_not_tool_failure(self):
        for data in (b'{"a": 1, "a": 2}', b'[NaN]', b'Infinity', b'1e999', b'\xff', b'{\n "a": }'):
            with self.subTest(data=data):
                path = self.put('bad.json', data)
                result = json_check(str(path), self.base)
                self.assertFalse(result['valid'])
                self.assertTrue(result['error']['message'])
                self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(result['error']['line'], 2)
        self.assertIsInstance(result['error']['column'], int)

    def test_large_integer_is_valid_and_depth_limit_is_tool_error(self):
        self.put('integer.json', b'9' * 5000)
        result = json_check('integer.json', self.base)
        self.assertTrue(result['valid'])
        self.assertEqual(result['root_type'], 'number')
        self.assertEqual(result['value_count'], 1)
        self.put('deep.json', b'[' * 2000 + b'0' + b']' * 2000)
        # Python versions differ in whether this nesting exceeds parser limits.
        try:
            deep = json_check('deep.json', self.base)
        except HarnessError as error:
            self.assertIn('parser capabilities', str(error))
        else:
            self.assertTrue(deep['valid'])
            self.assertEqual(deep['value_count'], 2001)
        with patch('harness.file_tools.json.loads', side_effect=RecursionError('depth limit')):
            with self.assertRaisesRegex(HarnessError, 'parser capabilities'):
                json_check('deep.json', self.base)
        with patch('harness.file_tools.json.loads', side_effect=ValueError('implementation limit')):
            with self.assertRaisesRegex(HarnessError, 'parser capabilities'):
                json_check('integer.json', self.base)

    def test_nonfiles_missing_and_size_limits(self):
        for path in ('missing', '.'):
            with self.assertRaises(HarnessError):
                file_sha256(path, self.base)
        os.mkfifo(self.base / 'pipe')
        with self.assertRaisesRegex(HarnessError, 'regular file'):
            json_check('pipe', self.base)
        self.put('large', b'123456')
        with patch('harness.file_tools.MAX_TEXT_BYTES', 5):
            with self.assertRaisesRegex(HarnessError, 'limit'):
                json_check('large', self.base)

    def test_diff_external_clones_binary_and_output_limit(self):
        left = self.put('clone_a/data.txt', b'common\nold\n')
        right = self.put('clone_b/data.txt', b'common\nnew\n')
        result = file_diff(str(left), str(right), Path('/'))
        self.assertTrue(result['different'])
        self.assertIn('-old', result['diff'])
        self.assertIn('+new', result['diff'])
        self.assertFalse(file_diff(str(left), str(left), self.base)['different'])
        right.write_bytes(b'\x00binary')
        self.assertTrue(file_diff(str(left), str(right), self.base)['binary'])
        right.write_bytes(b'x' * (MAX_DIFF_CHARS + 100))
        result = file_diff(str(left), str(right), self.base)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['diff']), MAX_DIFF_CHARS)

    def test_calls_are_closed_read_only_and_use_run_worktree(self):
        self.put('a.json', b'{}')
        run = self.put('run.json', json.dumps({'worktree': str(self.base)}).encode())
        before = {p.relative_to(self.base): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        def call(name, arguments):
            return handle({'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}, None, None, run)
        with patch('subprocess.run', side_effect=AssertionError('process launched')), patch('subprocess.Popen', side_effect=AssertionError('process launched')):
            for name, args in [('file_sha256', {'path': 'a.json'}), ('json_check', {'path': 'a.json'}), ('file_diff', {'left': 'a.json', 'right': 'a.json'})]:
                self.assertFalse(call(name, args)['isError'])
                self.assertTrue(call(name, dict(args, extra=True))['isError'])
                self.assertTrue(call(name, {})['isError'])
                self.assertTrue(call(name, None)['isError'])
            self.assertTrue(call('file_sha256', {'path': 12})['isError'])
        after = {p.relative_to(self.base): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        for tool in TOOLS:
            self.assertTrue(tool['annotations']['readOnlyHint'])


if __name__ == '__main__':
    unittest.main()
