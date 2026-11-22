"""Offline capacity accounting and verified prompt retrieval, no model calls."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from harness.context import compose, ContextCapacityError, prompt_json
from harness.input_projection import project_history, project_commands, validate_indexes
from harness import stops
from harness.util import EvidenceError, HarnessError


def rule5_id(command):
    """The command ID as specification rule 5 defines it, computed here rather than by the runtime."""
    canonical = json.dumps({'argv': command['argv'], 'cwd': command['cwd']},
                           ensure_ascii=True, separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(canonical.encode('ascii')).hexdigest()


def ref(command):
    return {'exact_command_ref': rule5_id(command)}


class InputProjectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def index_records(self, path, digest):
        """An index's records, read as bytes and checked against the registered SHA256 here rather than by the runtime."""
        raw = Path(path).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
        return json.loads(raw)['records']

    def assert_catalog(self, result, entries, indexed):
        """Pop the command catalog, and the index registration holding it, from result; compare it with entries."""
        catalog = result.pop('exact_command_catalog', None)
        if indexed:
            self.assertIsInstance(catalog, dict)
            digest = catalog.get('input_index')
            path = self.root / 'input-indexes' / f'{digest}.json'
            self.assertEqual(catalog, {'input_index': digest, 'count': len(entries)})
            self.assertEqual(result.pop('input_evidence_indexes', None),
                             {digest: {'artifact': str(path), 'artifact_digest': digest, 'count': len(entries)}})
            catalog = self.index_records(path, digest)
        self.assertCountEqual(catalog, entries)

    def test_exact_accounting_includes_parent_overhead_without_values(self):
        value = {'role': 'commander', 'kind': 'decide', 'task_id': 'run',
                 'packet': {'goal': 'secret\\"\u96ea', 'commands': [{'cwd': '.', 'argv': ['private-argv']}]}}
        with self.assertRaises(ContextCapacityError) as caught:
            compose(value, [], 2)
        report = caught.exception.report
        self.assertEqual(report['required_chars'], len(prompt_json(value)))
        self.assertEqual(report['kind'], 'decide')
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
            recovered += self.index_records(ref['artifact'], ref['artifact_digest'])
        self.assertIn(rows[0], recovered)
        self.assertIn(guidance[0], recovered)
        path = Path(refs[0]['artifact'])
        path.write_bytes(b'{}')
        with self.assertRaises(HarnessError):
            project_history(packet, self.root)
        self.assertEqual(path.read_bytes(), b'{}')

    def test_oversized_applicable_guidance_is_a_mandatory_complete_read(self):
        row = {'id': 'active', 'input': {'constraint': 'binding ' * 2000},
               'source': {'dispatch_id': 'worker'}, 'applicability': {'active': True}}
        result = project_history({'execution_guidance': [row]}, self.root)
        self.assertEqual(result['execution_guidance'], [])
        # The index is a reference and count; reading it is a shared RULES obligation.
        ref = next(iter(result['input_evidence_indexes'].values()))
        self.assertEqual(result['execution_guidance_index'],
                         {'input_index': ref['artifact_digest'], 'count': 1})
        self.assertEqual(self.index_records(ref['artifact'], ref['artifact_digest']), [row])

    def test_long_exact_commands_are_retrievable_without_changing_source_grants(self):
        argv = {'cwd': '.', 'argv': ['python3', '-c', 'long exact command ' * 2000]}
        packet = {'tool_constraints': {'verification_commands': [argv]},
                  'verification_plan': {'checks': [{'command': argv}, {'command': argv}]}}
        before = deepcopy(packet)
        result = project_commands(packet, self.root)
        self.assertLess(len(prompt_json(result)), 3000)
        refs = list(result['input_evidence_indexes'].values())
        validate_indexes(self.root, refs)
        records = self.index_records(refs[0]['artifact'], refs[0]['artifact_digest'])
        self.assertEqual(records[0]['command'], argv)
        self.assertEqual(result['verification_plan']['checks'][0]['command']['exact_command_ref'], records[0]['id'])
        self.assertEqual(packet, before)
        Path(refs[0]['artifact']).unlink()
        with self.assertRaises(HarnessError):
            validate_indexes(self.root, refs)

    def test_one_argv_in_two_directories_is_two_catalog_entries_named_by_command_ids(self):
        for placement, length in (('inline', 400), ('index', 2000)):
            with self.subTest(placement=placement):
                argv = ['flutter', 'test', '--plain-name', '\u96ea ' + 'x' * length]
                root, app = {'cwd': '.', 'argv': argv}, {'cwd': 'app', 'argv': list(argv)}
                # Directories compare as written: case and Unicode normalization name other directories.
                upper, composed, decomposed = ({'cwd': cwd, 'argv': list(argv)} for cwd in ('App', 'caf\u00e9', 'cafe\u0301'))
                short = {'cwd': 'app', 'argv': ['flutter', 'test']}
                packet = {'tool_constraints': {'worker_commands': [app, upper, composed, decomposed, short],
                                               'verification_commands': [root]},
                          # The same pair with its keys in the other order is the same command.
                          'verification_plan': {'checks': [{'command': {'argv': list(argv), 'cwd': '.'}},
                                                           {'command': deepcopy(app)}, {'command': deepcopy(short)}]},
                          'task': {'required_commands': [deepcopy(upper), deepcopy(composed)],
                                   'self_checks': [deepcopy(decomposed), deepcopy(short)]},
                          'stop': {'data': {'missing': {'task': [deepcopy(app)]}}}}
                before = deepcopy(packet)
                result = project_commands(packet, self.root)
                self.assertEqual(packet, before)
                expected = deepcopy(before)
                expected['tool_constraints'] = {'worker_commands': [ref(app), ref(upper), ref(composed), ref(decomposed), short],
                                                'verification_commands': [ref(root)]}
                expected['verification_plan']['checks'][:2] = [{'command': ref(root)}, {'command': ref(app)}]
                expected['task'] = {'required_commands': [ref(upper), ref(composed)], 'self_checks': [ref(decomposed), short]}
                expected['stop']['data']['missing']['task'] = [ref(app)]
                # One entry per (directory, argv) pair, each exactly {id, command} with id the rule 5 ID.
                self.assert_catalog(result, [{'id': rule5_id(c), 'command': c}
                                             for c in (root, app, upper, composed, decomposed)],
                                    indexed=placement == 'index')
                self.assertEqual(result, expected)

    def test_one_directory_with_argv_differences_is_one_catalog_entry_per_argv(self):
        for placement, length in (('inline', 400), ('index', 2000)):
            with self.subTest(placement=placement):
                tail = 'x' * length
                argv = ['flutter', 'test', '--plain-name', 'caf\u00e9 ' + tail]
                # Arguments compare as written: case, Unicode normalization, one more argument and
                # argument order each name another command in the same directory.
                variants = (argv, ['flutter', 'test', '--plain-name', 'Caf\u00e9 ' + tail],
                            ['flutter', 'test', '--plain-name', 'cafe\u0301 ' + tail], [*argv, '--reporter=expanded'],
                            ['flutter', 'test', 'caf\u00e9 ' + tail, '--plain-name'])
                commands = [{'cwd': 'app', 'argv': list(v)} for v in variants]
                packet = {'tool_constraints': {'worker_commands': commands},
                          'verification_plan': {'checks': [{'command': deepcopy(c)} for c in commands]}}
                before = deepcopy(packet)
                result = project_commands(packet, self.root)
                self.assertEqual(packet, before)
                self.assert_catalog(result, [{'id': rule5_id(c), 'command': c} for c in commands],
                                    indexed=placement == 'index')
                self.assertEqual(result, {'tool_constraints': {'worker_commands': [ref(c) for c in commands]},
                                          'verification_plan': {'checks': [{'command': ref(c)} for c in commands]}})

    def test_commands_are_projected_under_any_field_at_any_depth(self):
        command = {'cwd': 'tools/gen', 'argv': ['dart', 'run', 'build_runner', 'build', '--define=' + 'd' * 600]}
        # Over the inline limit a single occurrence is replaced as well.
        alone = {'cwd': '.', 'argv': ['python3', '-c', 'pass  # ' + 'a' * 4100]}
        packet = {'meta': {'example': command},
                  'steps': [{'name': 'gen', 'run': {'with': [{'script': deepcopy(command)}]}}],
                  'matrix': [[{'entry': {'deep': deepcopy(command)}}], [deepcopy(command)]],
                  'notes': {'once': [{'when': 'rarely', 'how': alone}]}}
        before = deepcopy(packet)
        result = project_commands(packet, self.root)
        self.assertEqual(packet, before)
        self.assert_catalog(result, [{'id': rule5_id(command), 'command': command},
                                     {'id': rule5_id(alone), 'command': alone}], indexed=True)
        expected = deepcopy(before)
        expected['meta']['example'] = ref(command)
        expected['steps'][0]['run']['with'][0]['script'] = ref(command)
        expected['matrix'] = [[{'entry': {'deep': ref(command)}}], [ref(command)]]
        expected['notes']['once'][0]['how'] = ref(alone)
        self.assertEqual(result, expected)

    def test_catalog_index_joins_existing_indexes_and_is_bound_to_its_bytes(self):
        prior = project_history({'continuation_history': [{'id': 'old', 'reason': 'h' * 5000}]}, self.root)
        command = {'cwd': '.', 'argv': ['tool', 'x' * 5000]}
        packet = {**prior, 'commands': [command, deepcopy(command)]}
        before = deepcopy(packet)
        result = project_commands(packet, self.root)
        self.assertEqual(packet, before)
        catalog = result.pop('exact_command_catalog', None)
        self.assertIsInstance(catalog, dict)
        digest = catalog.get('input_index')
        path = self.root / 'input-indexes' / f'{digest}.json'
        self.assertEqual(catalog, {'input_index': digest, 'count': 1})
        expected = deepcopy(before)
        expected['commands'] = [ref(command), ref(command)]
        # The catalog's registration is added beside the history index the packet already carries.
        expected['input_evidence_indexes'][digest] = {'artifact': str(path), 'artifact_digest': digest, 'count': 1}
        self.assertEqual(result, expected)
        self.assertEqual(self.index_records(path, digest), [{'id': rule5_id(command), 'command': command}])
        # Trailing whitespace keeps the JSON value; only the SHA256 binding can notice it.
        tampered = path.read_bytes() + b' '
        path.write_bytes(tampered)
        refs = list(expected['input_evidence_indexes'].values())
        for attempt in (lambda: validate_indexes(self.root, refs), lambda: project_commands(packet, self.root),
                        lambda: project_commands({**result, 'exact_command_catalog': catalog}, self.root)):
            with self.assertRaises(HarnessError):
                attempt()
            # A refusal leaves the evidence as it found it.
            self.assertEqual(path.read_bytes(), tampered)

    def test_only_whole_typed_command_objects_are_projected(self):
        argv = ['python3', '-c', 'print("exact")  # ' + 'r' * 600]
        command = {'cwd': '.', 'argv': argv}
        near = {'extra_key': {'cwd': '.', 'argv': list(argv), 'env': {}}, 'no_cwd': {'argv': list(argv)},
                'cwd_not_text': {'cwd': 1, 'argv': list(argv)}, 'argv_not_list': {'cwd': '.', 'argv': ' '.join(argv)},
                'argv_item_not_text': {'cwd': '.', 'argv': [*argv, 7]}}
        # Bare argv lists, under every name the argv-era projection deduplicated, stay verbatim.
        bare = {key: [list(argv), list(argv)] for key in ('readonly_commands', 'worker_commands', 'verification_commands',
                                                         'commands', 'required_commands', 'self_checks', 'missing_commands')}
        packet = {'tool_constraints': bare, 'near': [near, deepcopy(near)],
                  'checks': [{'command': command}, {'command': deepcopy(command)}]}
        before = deepcopy(packet)
        result = project_commands(packet, self.root)
        self.assertEqual(packet, before)
        expected = deepcopy(before)
        expected['checks'] = [{'command': ref(command)}, {'command': ref(command)}]
        expected['exact_command_catalog'] = [{'id': rule5_id(command), 'command': command}]
        self.assertEqual(result, expected)

    def test_nonregular_index_rejected_without_blocking_read(self):
        packet = project_history({'continuation_history': [{'id': 'old', 'reason': 'x' * 5000}]}, self.root)
        ref = next(iter(packet['input_evidence_indexes'].values()))
        path = Path(ref['artifact'])
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(EvidenceError) as raised:
            validate_indexes(self.root, [ref])
        self.assertEqual(stops.of(raised.exception)['reason'], 'runtime_integrity')

    def test_small_payload_stays_inline_and_symlink_index_is_rejected(self):
        packet = {'tool_constraints': {'verification_commands': [{'cwd': '.', 'argv': ['python3', '-V']}]}}
        self.assertEqual(project_commands(packet, self.root), packet)
        (self.root / 'input-indexes').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(HarnessError):
            project_history({'continuation_history': [{'id': 'a', 'reason': 'x' * 5000}]}, self.root)
