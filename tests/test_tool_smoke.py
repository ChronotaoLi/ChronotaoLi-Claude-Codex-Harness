import json
import os
import sys
import tempfile
import subprocess
import unittest
from pathlib import Path

from harness.command_server import Broker, create_manifest, handle
from harness.tool_smoke import inspect_evidence, prepare_fixture
from harness.util import HarnessError, read_json, write_json


class ToolSmokeEvidenceTests(unittest.TestCase):
    def evidence(self, root, *, error=True, denial=False, result=True, args=None):
        case = dict(name='broker_rejection', tool='mcp__harness_commands__run_command',
                    input={'command_id': 'unknown'}, expected={}, error=True)
        fixture = dict(cases=[case], expected_files={}, commands=[])
        events = [{'type': 'assistant', 'message': {'content': [dict(type='tool_use', id='call',
                  name=case['tool'], input=args if args is not None else case['input'])]}}]
        if result:
            events.append({'type': 'user', 'message': {'content': [dict(type='tool_result', tool_use_id='call',
                          is_error=error, content=[{'type': 'text', 'text': 'Unknown command_id'}])]}})
        final = dict(type='result', subtype='success')
        if denial:
            final['permission_denials'] = [{'tool_use_id': 'call'}]
        events.append(final)
        path = root / 'stream.jsonl'
        path.write_text('\n'.join(json.dumps(e) for e in events))
        return path, fixture

    def test_broker_rejection_requires_no_cli_permission_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, fixture = self.evidence(Path(tmp))
            self.assertTrue(inspect_evidence(path, fixture)['passed'])
            path, fixture = self.evidence(Path(tmp), denial=True)
            self.assertFalse(inspect_evidence(path, fixture)['passed'])

    def test_missing_outcome_and_wrong_input_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            for options in ({'result': False}, {'error': False}, {'args': {'command_id': 'unknown', 'argv': []}}):
                path, fixture = self.evidence(Path(tmp), **options)
                self.assertFalse(inspect_evidence(path, fixture)['passed'])

    def test_model_prose_does_not_prove_tool_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, fixture = self.evidence(Path(tmp))
            path.write_text(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'All tools worked'}))
            self.assertFalse(inspect_evidence(path, fixture)['passed'])

    def test_semantically_invalid_json_is_successful_tool_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, fixture = self.evidence(Path(tmp), error=False)
            fixture['cases'][0].update(error=False, expected={'valid': False}, tool='mcp__harness__json_check')
            events = [json.loads(line) for line in path.read_text().splitlines()]
            events[0]['message']['content'][0]['name'] = 'mcp__harness__json_check'
            events[1]['message']['content'][0]['content'][0]['text'] = '{"valid":false}'
            path.write_text('\n'.join(json.dumps(e) for e in events))
            self.assertTrue(inspect_evidence(path, fixture)['passed'])

    def test_no_final_result_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'stream'
            path.write_text('{}')
            with self.assertRaises(HarnessError):
                inspect_evidence(path, dict(cases=[], expected_files={}))

class ToolSmokeStartupTests(unittest.TestCase):
    def test_cli_failure_preserves_stderr_and_does_not_claim_model_call(self):
        from unittest.mock import patch
        from harness.config import Config
        from harness.tool_smoke import tool_smoke
        import uuid
        root = Path(__file__).resolve().parents[1]
        config = Config.load(root / 'harness.toml')
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            def failed(argv, cwd, output, **kwargs):
                (output.parent / 'process.stdout').write_text('')
                (output.parent / 'process.stderr').write_text('Invalid session ID')
                return 1
            argv = ['claude', '--json-schema', '{}', '--output-format', 'json']
            def command(role, model, cwd, job, schema, run_path, **kwargs):
                # Like the real adapter, publish the per-dispatch MCP config.
                (job / 'mcp.json').write_text(json.dumps({'mcpServers': {'harness': {'args': []}}}))
                return list(argv)
            with patch('harness.tool_smoke.CLIBackend.command', side_effect=command), patch('harness.tool_smoke.run_process', side_effect=failed):
                with self.assertRaises(HarnessError):
                    tool_smoke(config, tmp)
            reports = list(Path(tmp).glob('.harness/tool-smoke/*/report.json'))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text())
            self.assertTrue(report['cli_attempted'])
            self.assertFalse(report['models_called'])
            self.assertEqual(report['exit_code'], 1)
            self.assertIn('Invalid session ID', report['stderr'])
            self.assertEqual(str(uuid.UUID(report['dispatch_id'])), report['dispatch_id'])


class ToolSmokeFixtureTests(unittest.TestCase):
    def test_extra_execution_argument_uses_provider_stable_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = prepare_fixture(Path(tmp) / 'fixture')
            case = next(c for c in fixture['cases'] if c['name'] == 'extra_argv')
            self.assertEqual(case['input']['argv'], '--ungranted')
            self.assertTrue(case['error'])


class ToolSmokeCommandDirectoryTests(unittest.TestCase):
    """The broker's cases checked offline on the local broker's own receipts; it is not the live smoke."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.fixture = prepare_fixture(self.tmp / 'fixture')
        # The read-only tools and delete_path have their own offline checks.
        self.cases = [c for c in self.fixture['cases'] if c['tool'].split('__')[2] in ('list_commands', 'run_command')]
        config = {'verification': {'commands': []}, 'permissions': {'worker_commands': self.fixture['commands']},
                  'runtime': {'verify_timeout_seconds': 30, 'agent_timeout_seconds': 120}}
        self.job = self.tmp / 'job'
        meta = create_manifest(config, Path(self.fixture['worktree']), self.job, None)
        broker = Broker(meta['artifact'], meta['artifact_digest'])
        self.responses = {case['name']: handle({'method': 'tools/call', 'params': {
            'name': case['tool'].split('__')[2], 'arguments': case['input']}}, broker) for case in self.cases}

    def value(self, name):
        return json.loads(self.responses[name]['content'][0]['text'])

    def inspect(self, name=None, change=None):
        """The provider stream of these calls, ``change`` applied to the result of case ``name``."""
        events = []
        for index, case in enumerate(self.cases):
            response = self.responses[case['name']]
            content = response['content']
            if case['name'] == name:
                value = self.value(name)
                change(value)
                content = [{'type': 'text', 'text': json.dumps(value)}]
            events += [{'type': 'assistant', 'message': {'content': [
                           {'type': 'tool_use', 'id': f'c{index}', 'name': case['tool'], 'input': case['input']}]}},
                       {'type': 'user', 'message': {'content': [
                           {'type': 'tool_result', 'tool_use_id': f'c{index}', 'is_error': response['isError'],
                            'content': content}]}}]
        stream = self.tmp / 'stream.jsonl'
        stream.write_text('\n'.join(json.dumps(e) for e in events + [{'type': 'result', 'subtype': 'success'}]))
        report = inspect_evidence(stream, {**self.fixture, 'cases': self.cases, 'absent_files': []}, self.job)
        return report, {check['name']: check['passed'] for check in report['checks']}

    def test_a_subdirectory_and_a_missing_directory_command_pass_on_the_broker_s_receipts(self):
        worktree = Path(self.fixture['worktree'])
        self.assertEqual([c['cwd'] for c in self.fixture['commands']], ['.', '.', 'sub', 'absent'])
        self.assertTrue((worktree / 'sub').is_dir())
        self.assertFalse(os.path.lexists(worktree / 'absent'))
        sub, absent = self.value('subdirectory_command'), self.value('missing_directory')
        self.assertEqual((sub['exit_code'], sub['stdout'], sub['command']['cwd']), (0, 'sub\n', 'sub'))
        # Not a tool error: a receipt of a command that never started.
        self.assertFalse(self.responses['missing_directory']['isError'])
        self.assertEqual((absent['exit_code'], absent['termination_reason'], absent['evidence_complete'],
                          absent['command']['cwd']), (None, 'start_failed', True, 'absent'))
        report, checks = self.inspect()
        self.assertTrue(report['passed'], report['checks'])
        for name in ('list', 'subdirectory_command', 'missing_directory', 'one_record_per_command'):
            self.assertTrue(checks[name], name)

    def test_a_wrong_directory_output_evidence_or_journal_fails_its_check(self):
        changes = {
            'subdirectory_command': {'directory': lambda v: v['command'].update(cwd='.'),
                                     'output': lambda v: v.update(stdout='fixture\n')},
            'missing_directory': {'directory': lambda v: v['command'].update(cwd='.'),
                                  'evidence': lambda v: v.update(evidence_complete=False),
                                  'exit code': lambda v: v.pop('exit_code'),
                                  'reason': lambda v: v.update(stderr='')}}
        for name, variants in changes.items():
            for variant, change in variants.items():
                with self.subTest(name, variant=variant):
                    report, checks = self.inspect(name, change)
                    self.assertFalse(report['passed'])
                    self.assertFalse(checks[name])
        # The journal of the command that never started must say it exited.
        journal = Path(self.value('missing_directory')['stdout_path']).with_name('process.json')
        original = read_json(journal)
        reaped = subprocess.Popen([sys.executable, '-c', 'pass'])
        reaped.wait()
        for variant, entry in (('still running', {'process_state': 'running'}),
                               ('exit unconfirmed', {'pgid': reaped.pid, 'exit_confirmed': False})):
            with self.subTest('missing_directory journal', variant=variant):
                write_json(journal, {**original, **entry})
                try:
                    report, checks = self.inspect()
                finally:
                    write_json(journal, original)
                self.assertFalse(report['passed'])
                self.assertFalse(checks['one_record_per_command'])
        self.assertTrue(self.inspect()[0]['passed'])
