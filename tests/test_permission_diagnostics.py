import contextlib
import copy
import io
import json
import re
import shutil
import sys
import unittest
from types import SimpleNamespace

from harness import cli
from harness.diagnostics import permission_denial_projection
from harness.permission_diagnostics import command_capability_projection, permission_diagnostics
from harness.util import HarnessError, write_json
from harness.versions import RUN_SCHEMA_VERSION
from test_harness import ROOT, RepoCase, cli_notice, write_project_config


class PermissionDiagnosticsTests(unittest.TestCase):
    def test_exact_command_is_independent_from_unavailable_provider_reason(self):
        command = {'cwd': '.', 'argv': ['python3', '-m', 'unittest']}
        config = SimpleNamespace(data={'verification': {'commands': [command]},
                                      'permissions': {'worker_commands': []}})
        original = copy.deepcopy(config.data)
        terminal = {'permission_denials': [{'tool_name': 'Bash', 'tool_use_id': 'tool_1',
                    'tool_input': {'command': 'PRIVATE'}, 'reason': 'PRIVATE'}]}
        result = permission_diagnostics(config, terminal)
        self.assertNotIn('local_command_capability', result)
        self.assertTrue(command_capability_projection(config, command)['local_exact_command_match'])
        self.assertEqual(result['provider_denials'][0]['reason'], 'unavailable')
        self.assertFalse(result['provider_allowance_verified'])
        self.assertNotIn('PRIVATE', repr(result))
        self.assertIn('Read', result['native_read_tools'])
        self.assertIn('harness.file_sha256', result['readonly_file_tools'])
        # Every broker tool a worker can be given is named, deletion included.
        from harness.command_server import DELETE_TOOL, TOOLS
        for tool in TOOLS + [DELETE_TOOL]:
            self.assertIn('harness_commands.' + tool['name'], result['command_broker'])
        self.assertEqual(config.data, original)
        for other in (['python3'], ['python3', '-m', 'unittest', '-v'], ['sh', '-c', 'python3 -m unittest']):
            result = command_capability_projection(config, {'cwd': '.', 'argv': other})
            self.assertFalse(result['local_exact_command_match'])

    def test_unknown_evidence_and_invalid_command_never_grant_permission(self):
        config = SimpleNamespace(data={'verification': {'commands': []},
                                      'permissions': {'worker_commands': []}})
        self.assertEqual(permission_diagnostics(config)['reported_denial_count'], 0)
        for value in (None, '', 'python3', [], ['python3'], {'argv': ['python3']}, {'cwd': '.', 'argv': []},
                      {'cwd': '.', 'argv': [True]}, {'cwd': '.', 'argv': ['']}):
            with self.subTest(value=value), self.assertRaisesRegex(HarnessError, 'must be {cwd, argv}'):
                command_capability_projection(config, value)

    def test_denials_never_copy_args_or_invent_reason(self):
        projected = permission_denial_projection({'permission_denials': [
            {'tool_name': 'Bash', 'tool_use_id': 'id1', 'tool_input': 'SECRET', 'reason': 'SECRET'}]})
        self.assertEqual(projected, [{'tool': 'Bash', 'tool_id': 'id1', 'reason': 'unavailable'}])


class CommandCapabilityTests(RepoCase):
    def test_exact_command_capability_does_not_infer_provider_allowance(self):
        command = {'cwd': '.', 'argv': ['python3', '-m', 'unittest']}
        self.config.data['permissions']['worker_commands'] = [command]
        exact = command_capability_projection(self.config, command)
        self.assertTrue(exact['local_exact_command_match'])
        self.assertFalse(exact['provider_allowance_verified'])
        self.assertEqual(exact['provider_permission_reason'], 'unavailable')
        for other in (command['argv'] + ['-v'], ['python3'], ['sh', '-c', 'python3 -m unittest']):
            other = {'cwd': '.', 'argv': other}
            self.assertFalse(command_capability_projection(self.config, other)['local_exact_command_match'])


def terminal(tool_id):
    return {'type': 'result', 'subtype': 'success', 'permission_denials': [
        {'tool_name': 'Bash', 'tool_use_id': tool_id, 'tool_input': {'command': 'PRIVATE'}}]}


class PermissionDiagnosticsCliTests(RepoCase):
    """The Claude terminal is always process.stdout; other ledger versions are refused."""

    RUN = 'run_0123456789abcdef'
    DISPATCH = 'dispatch-1'

    def ledger(self, files, **fields):
        directory = self.repo / '.harness' / 'runs' / self.RUN
        job = directory / 'dispatches' / self.DISPATCH
        job.mkdir(parents=True)
        for name, value in files.items():
            (job / name).write_text(value if isinstance(value, str) else json.dumps(value), encoding='utf-8')
        write_project_config(self.repo)  # permission-diagnostics compares against the project's commands
        run = {'id': self.RUN, 'commander': 'astra', 'config_snapshot': {'roles': {'worker': {'cli': 'claude'}}},
               'dispatches': [{'id': self.DISPATCH, 'role': 'worker'}], **fields}
        write_json(directory / 'state.json', run)
        return directory / 'state.json'

    def report(self, path):
        before = path.read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(['--config', str(ROOT / 'harness.toml'), '--repo', str(self.repo),
                             'permission-diagnostics', self.RUN, self.DISPATCH])
        self.assertEqual(code, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn('PRIVATE', output.getvalue())
        return json.loads(output.getvalue())

    def diagnose(self, *options):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(['--config', str(ROOT / 'harness.toml'), '--repo', str(self.repo),
                             'permission-diagnostics', self.RUN, self.DISPATCH, *options])
        return code, output.getvalue(), errors.getvalue()

    APP = {'cwd': 'app', 'argv': ['flutter', 'test']}
    QUERY = ['git', 'ls-files', '-z', '--', 'src']
    ROOT_ONLY = {'tool': 'harness.git_query', 'covered': False, 'reason': 'git_query runs only at the worktree root'}

    def test_command_json_compares_the_exact_directory_and_argv_pair(self):
        path = self.ledger({'process.stdout': terminal('json_tool')}, schema_version=RUN_SCHEMA_VERSION)
        verification = {'cwd': '.', 'argv': [sys.executable, '-m', 'unittest', '-v']}
        # Grants in either case and either Unicode form (precomposed, decomposed); each is matched only as written.
        lib, cafe, munich = ({**self.APP, 'cwd': cwd} for cwd in ('Lib', 'caf\u00e9', 'mu\u0308nchen'))
        write_project_config(self.repo, [verification], [self.APP, {'cwd': '.', 'argv': self.QUERY}, lib, cafe, munich])
        before = path.read_bytes()
        covered = {'tool': 'harness.git_query', 'covered': True, 'form': 'ls-files',
                   'meaning': 'capability_available_not_executed'}
        cases = [(self.APP, ['permissions.worker_commands'], self.ROOT_ONLY),
                 ({'cwd': 'App', 'argv': ['flutter', 'test']}, [], self.ROOT_ONLY),
                 (lib, ['permissions.worker_commands'], self.ROOT_ONLY),
                 ({'cwd': 'lib', 'argv': ['flutter', 'test']}, [], self.ROOT_ONLY),
                 (cafe, ['permissions.worker_commands'], self.ROOT_ONLY),
                 ({**cafe, 'cwd': 'cafe\u0301'}, [], self.ROOT_ONLY),
                 (munich, ['permissions.worker_commands'], self.ROOT_ONLY),
                 ({**munich, 'cwd': 'm\u00fcnchen'}, [], self.ROOT_ONLY),
                 ({'cwd': '.', 'argv': ['flutter', 'test']}, [], None),
                 ({'cwd': 'app', 'argv': self.QUERY}, [], self.ROOT_ONLY),
                 ({'cwd': '.', 'argv': self.QUERY}, ['permissions.worker_commands'], covered),
                 (verification, ['verification.commands'], None)]
        for command, sources, coverage in cases:
            with self.subTest(command=command):
                code, output, errors = self.diagnose('--command-json', json.dumps(command))
                self.assertEqual(code, 0, errors)
                local = json.loads(output)['local_command_capability']
                self.assertEqual((local['local_exact_command_match'], local['configured_sources']), (bool(sources), sources))
                self.assertNotIn('local_exact_argv_match', local)
                if coverage is None:  # an argv outside the table: not covered, for its own reason
                    self.assertFalse(local['builtin_coverage']['covered'])
                    self.assertNotIn(local['builtin_coverage']['reason'], ('', self.ROOT_ONLY['reason']))
                else:
                    self.assertEqual(local['builtin_coverage'], coverage)
        code, output, _ = self.diagnose()  # no option, no comparison
        self.assertEqual(code, 0)
        self.assertNotIn('local_command_capability', json.loads(output))
        self.assertEqual(path.read_bytes(), before)

    def test_command_json_that_is_not_one_command_object_is_refused_without_a_report(self):
        path = self.ledger({'process.stdout': terminal('json_tool')}, schema_version=RUN_SCHEMA_VERSION)
        write_project_config(self.repo, None, [self.APP])
        before = path.read_bytes()
        # Each refusal names the field it rejects; text that is not JSON fails before any field is read, naming none.
        for raw, field in (('null', '--command-json'), ('', None), ('["flutter", "test"]', '--command-json'),
                           (json.dumps([self.APP]), '--command-json'), ('{"argv": ["flutter", "test"]}', '--command-json'),
                           (json.dumps({**self.APP, 'env': {}}), '--command-json'),
                           ('{"cwd": " ", "argv": ["flutter", "test"]}', '--command-json.cwd'),
                           ('{"cwd": "app/../app", "argv": ["flutter", "test"]}', '--command-json.cwd'),
                           ('{"cwd": "app", "argv": []}', '--command-json.argv')):
            with self.subTest(raw=raw):
                code, output, errors = self.diagnose('--command-json', raw)
                self.assertEqual((code, output, cli_notice(errors)['kind']), (1, '', 'unclassified'))
                self.assertEqual(re.findall(r'--command-json(?:\.cwd|\.argv)?\b(?!\.)', errors), [field] if field else [], errors)
                self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(SystemExit) as raised:
            self.diagnose('--argv-json', '["flutter", "test"]')
        self.assertEqual(raised.exception.code, 2)

    def test_dispatch_reads_stdout_even_beside_a_stream_manifest(self):
        path = self.ledger({'process.stdout': terminal('json_tool'),
                            'capture-manifest.json': {'version': 1, 'mode': 'stream_metadata_v1'},
                            'stream-terminal.json': terminal('stream_tool')}, schema_version=RUN_SCHEMA_VERSION)
        report = self.report(path)
        self.assertEqual(report['reported_denial_count'], 1)
        self.assertEqual([item['tool_id'] for item in report['provider_denials']], ['json_tool'])

    def test_other_ledger_versions_are_refused_before_any_evidence_is_read(self):
        for version, shown in ((None, '缺失或无效'), ('19', '缺失或无效'), (18, ' 18 '), (17, ' 17 ')):
            with self.subTest(version=version):
                shutil.rmtree(self.repo / '.harness', ignore_errors=True)
                fields = {} if version is None else {'schema_version': version}
                path = self.ledger({'capture-manifest.json': {'version': 1, 'mode': 'stream_metadata_v1'},
                                    'stream-terminal.json': terminal('stream_tool'),
                                    'process.stdout': terminal('json_tool')}, **fields)
                before = path.read_bytes()
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = cli.main(['--config', str(ROOT / 'harness.toml'), '--repo', str(self.repo),
                                     'permission-diagnostics', self.RUN, self.DISPATCH])
                self.assertEqual((code, output.getvalue()), (2, ''))  # an unsupported ledger is an input refusal
                self.assertIn(f'台账版本{shown}', errors.getvalue())
                self.assertIn('不支持（当前版本 19）', errors.getvalue())
                self.assertEqual(path.read_bytes(), before)
