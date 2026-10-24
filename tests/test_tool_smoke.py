import json
import tempfile
import unittest
from pathlib import Path

from harness.tool_smoke import inspect_evidence, prepare_fixture
from harness.util import HarnessError


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
            def failed(argv, cwd, output, **kwargs):
                (output.parent / 'process.stdout').write_text('')
                (output.parent / 'process.stderr').write_text('Invalid session ID')
                return 1
            argv = ['claude', '--json-schema', '{}', '--output-format', 'json']
            with patch('harness.tool_smoke.CLIBackend.command', return_value=argv), patch('harness.tool_smoke.run_process', side_effect=failed):
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
