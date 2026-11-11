import contextlib
import copy
import io
import json
import shutil
import unittest
from types import SimpleNamespace

from harness import cli
from harness.diagnostics import permission_denial_projection
from harness.permission_diagnostics import command_capability_projection, permission_diagnostics
from harness.util import HarnessError, write_json
from harness.versions import RUN_SCHEMA_VERSION
from test_harness import ROOT, RepoCase


class PermissionDiagnosticsTests(unittest.TestCase):
    def test_exact_argv_is_independent_from_unavailable_provider_reason(self):
        config = SimpleNamespace(data={'verification': {'commands': [['python3', '-m', 'unittest']]},
                                      'permissions': {'worker_commands': []}})
        original = copy.deepcopy(config.data)
        terminal = {'permission_denials': [{'tool_name': 'Bash', 'tool_use_id': 'tool_1',
                    'tool_input': {'command': 'PRIVATE'}, 'reason': 'PRIVATE'}]}
        result = permission_diagnostics(config, terminal, argv=['python3', '-m', 'unittest'])
        self.assertTrue(result['local_command_capability']['local_exact_argv_match'])
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
        for argv in (['python3'], ['python3', '-m', 'unittest', '-v'],
                     ['sh', '-c', 'python3 -m unittest']):
            result = permission_diagnostics(config, terminal, argv=argv)
            self.assertFalse(result['local_command_capability']['local_exact_argv_match'])

    def test_unknown_evidence_and_invalid_argv_never_grant_permission(self):
        config = SimpleNamespace(data={'verification': {'commands': []},
                                      'permissions': {'worker_commands': []}})
        self.assertEqual(permission_diagnostics(config)['reported_denial_count'], 0)
        for argv in ('python3', [], [True], ['']):
            with self.assertRaises(HarnessError):
                permission_diagnostics(config, argv=argv)

    def test_denials_never_copy_args_or_invent_reason(self):
        projected = permission_denial_projection({'permission_denials': [
            {'tool_name': 'Bash', 'tool_use_id': 'id1', 'tool_input': 'SECRET', 'reason': 'SECRET'}]})
        self.assertEqual(projected, [{'tool': 'Bash', 'tool_id': 'id1', 'reason': 'unavailable'}])


class CommandCapabilityTests(RepoCase):
    def test_exact_command_capability_does_not_infer_provider_allowance(self):
        command = ['python3', '-m', 'unittest']
        self.config.data['permissions']['worker_commands'] = [command]
        exact = command_capability_projection(self.config, command)
        self.assertTrue(exact['local_exact_argv_match'])
        self.assertFalse(exact['provider_allowance_verified'])
        self.assertEqual(exact['provider_permission_reason'], 'unavailable')
        for other in (command + ['-v'], ['python3'], ['sh', '-c', 'python3 -m unittest']):
            self.assertFalse(command_capability_projection(self.config, other)['local_exact_argv_match'])


def terminal(tool_id):
    return {'type': 'result', 'subtype': 'success', 'permission_denials': [
        {'tool_name': 'Bash', 'tool_use_id': tool_id, 'tool_input': {'command': 'PRIVATE'}}]}


class PermissionDiagnosticsCliTests(RepoCase):
    """Which file is the Claude terminal: fixed for schema 17, declared by a manifest before."""

    RUN = 'run_0123456789abcdef'
    DISPATCH = 'dispatch-1'

    def ledger(self, files, **fields):
        directory = self.repo / '.harness' / 'runs' / self.RUN
        job = directory / 'dispatches' / self.DISPATCH
        job.mkdir(parents=True)
        for name, value in files.items():
            (job / name).write_text(value if isinstance(value, str) else json.dumps(value), encoding='utf-8')
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

    def test_schema16_stream_dispatch_reads_its_published_terminal(self):
        path = self.ledger({'capture-manifest.json': {'version': 1, 'mode': 'stream_metadata_v1'},
                            'stream-terminal.json': terminal('stream_tool'), 'process.stdout': ''}, schema_version=16)
        self.assertEqual([item['tool_id'] for item in self.report(path)['provider_denials']], ['stream_tool'])

    def test_schema17_dispatch_reads_stdout_even_beside_a_stream_manifest(self):
        path = self.ledger({'process.stdout': terminal('json_tool'),
                            'capture-manifest.json': {'version': 1, 'mode': 'stream_metadata_v1'},
                            'stream-terminal.json': terminal('stream_tool')}, schema_version=RUN_SCHEMA_VERSION)
        report = self.report(path)
        self.assertEqual(report['reported_denial_count'], 1)
        self.assertEqual([item['tool_id'] for item in report['provider_denials']], ['json_tool'])

    def test_missing_or_non_integer_version_keeps_the_manifest_rule(self):
        for version in (None, '17'):
            with self.subTest(version=version):
                shutil.rmtree(self.repo / '.harness', ignore_errors=True)
                fields = {} if version is None else {'schema_version': version}
                path = self.ledger({'capture-manifest.json': {'version': 1, 'mode': 'stream_metadata_v1'},
                                    'stream-terminal.json': terminal('stream_tool'),
                                    'process.stdout': terminal('json_tool')}, **fields)
                self.assertEqual([item['tool_id'] for item in self.report(path)['provider_denials']], ['stream_tool'])
