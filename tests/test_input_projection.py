"""Offline capacity accounting and verified prompt retrieval, no model calls."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest

from harness.context import compose, ContextCapacityError, prompt_json
from harness.input_projection import project_history, project_commands, validate_indexes
from harness.util import HarnessError


class InputProjectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_exact_accounting_includes_parent_overhead_without_values(self):
        value = {'role': 'commander', 'kind': 'replan', 'task_id': 't',
                 'packet': {'goal': 'secret\\"雪', 'commands': [['private-argv']]}}
        with self.assertRaises(ContextCapacityError) as caught:
            compose(value, [], 2)
        report = caught.exception.report
        self.assertEqual(report['required_chars'], len(prompt_json(value)))
        self.assertEqual(report['kind'], 'replan')
        self.assertNotIn('secret', json.dumps(report))
        self.assertNotIn('private-argv', json.dumps(report))
        rows = {tuple(r['path']): r for r in report['field_sizes']}
        for path, row in rows.items():
            children = [r for p, r in rows.items() if len(p) == len(path) + 1 and p[:-1] == path]
            self.assertEqual(row['chars'], row['overhead_chars'] + sum(c['chars'] for c in children))
        prompt, success = compose(value, [], len(prompt_json(value)))
        self.assertEqual(len(prompt), report['required_chars'])
        self.assertEqual(success['field_sizes'], report['field_sizes'])

    def test_history_bounded_active_conditions_preserved_full_history_retrievable(self):
        rows = [{'id': str(i), 'request': {'reason': 'x' * 1000}} for i in range(100)]
        guidance = [{'id': str(i), 'input': {'constraint': 'g' * 1000},
                     'applicability': {'active': i == 99}} for i in range(100)]
        packet = {'goal': 'original', 'continuation_history': rows, 'pending_continuation': '99',
                  'execution_guidance': guidance}
        before = deepcopy(packet)
        result = project_history(packet, self.root)
        self.assertLess(len(prompt_json(result)), 6000)
        self.assertEqual(result['continuation_history'], [rows[-1]])
        self.assertEqual(result['execution_guidance'], [guidance[-1]])
        self.assertEqual(packet, before)
        refs = list(result['input_evidence_indexes'].values())
        validate_indexes(self.root, refs)
        recovered = []
        for ref in refs:
            recovered += json.loads(Path(ref['artifact']).read_text())['records']
        self.assertIn(rows[0], recovered)
        self.assertIn(guidance[0], recovered)
        path = Path(refs[0]['artifact'])
        path.write_text('{}')
        with self.assertRaises(HarnessError):
            project_history(packet, self.root)
        self.assertEqual(path.read_text(), '{}')

    def test_oversized_applicable_guidance_is_a_mandatory_complete_read(self):
        row = {'id': 'active', 'input': {'constraint': 'binding ' * 2000},
               'source': {'dispatch_id': 'worker'}, 'applicability': {'active': True}}
        result = project_history({'execution_guidance': [row]}, self.root)
        self.assertEqual(result['execution_guidance'], [])
        self.assertIn('before planning or execution', result['execution_guidance_index']['read_instruction'])
        ref = next(iter(result['input_evidence_indexes'].values()))
        self.assertEqual(json.loads(Path(ref['artifact']).read_text())['records'], [row])

    def test_long_exact_commands_are_retrievable_without_changing_source_grants(self):
        argv = ['python3', '-c', 'long exact command ' * 2000]
        packet = {'tool_constraints': {'verification_commands': [argv]},
                  'verification_plan': {'checks': [{'argv': argv}, {'argv': argv}]}}
        before = deepcopy(packet)
        result = project_commands(packet, self.root)
        self.assertLess(len(prompt_json(result)), 3000)
        refs = list(result['input_evidence_indexes'].values())
        validate_indexes(self.root, refs)
        records = json.loads(Path(refs[0]['artifact']).read_text())['records']
        self.assertEqual(records[0]['argv'], argv)
        self.assertEqual(result['verification_plan']['checks'][0]['argv']['exact_command_ref'], records[0]['id'])
        self.assertEqual(packet, before)
        Path(refs[0]['artifact']).unlink()
        with self.assertRaises(HarnessError):
            validate_indexes(self.root, refs)

    def test_nonregular_index_rejected_without_blocking_read(self):
        packet = project_history({'continuation_history': [{'id': 'old', 'reason': 'x' * 5000}]}, self.root)
        ref = next(iter(packet['input_evidence_indexes'].values()))
        path = Path(ref['artifact'])
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(HarnessError, 'regular file'):
            validate_indexes(self.root, [ref])

    def test_small_payload_stays_inline_and_symlink_index_is_rejected(self):
        packet = {'tool_constraints': {'verification_commands': [['python3', '-V']]}}
        self.assertEqual(project_commands(packet, self.root), packet)
        (self.root / 'input-indexes').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(HarnessError):
            project_history({'continuation_history': [{'id': 'a', 'reason': 'x' * 5000}]}, self.root)
