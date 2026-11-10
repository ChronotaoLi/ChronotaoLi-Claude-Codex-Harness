import copy
import json
import sys
import unittest
from pathlib import Path

from harness.adapters import CLIBackend
from harness.config import Config
from harness.permission_diagnostics import command_capability_projection
from harness.util import HarnessError, read_json
from test_harness import RepoCase, ROOT
from test_provider_usage import FixtureCLIBackend


class StreamFixtureBackend(FixtureCLIBackend):
    def __init__(self, config, state, tail='', exit_code=0):
        super().__init__(config, state)
        self.allow_stream_fixture = True
        self.tail, self.exit_code = tail, exit_code

    def command(self, role, model, cwd, job, schema, run_path):
        argv = super().command(role, model, cwd, job, schema, run_path)
        # Existing synthetic provider writes a unique terminal envelope.
        argv[2] = argv[2].replace('sys.exit(options.get("exit_code", 0))',
                                 'sys.stdout.write(' + repr(self.tail) + '); sys.exit(' + str(self.exit_code) + ')')
        return argv


class StreamAdapterTests(RepoCase):
    def test_stream_terminal_and_usage_pass_existing_acceptance_lifecycle(self):
        self.config.data['runtime']['claude_output_mode'] = 'stream_metadata_v1'
        backend = StreamFixtureBackend(self.config, self.repo / '.harness')
        result = self.engine(backend).start('Implement greet', read_json(ROOT / 'examples/plan.json'))
        self.assertEqual(result['status'], 'completed')
        worker = next(d for d in result['dispatches'] if d['role'] == 'worker')
        self.assertEqual(worker['provider_usage']['source'], 'stream-terminal.json')
        self.assertEqual(worker['provider_usage']['reported_cost_usd'], .25)

    def test_corrupt_tail_and_nonzero_never_publish_response_or_accept(self):
        for tail, code in (('{bad}\n', 0), ('{"type":"result"}\n', 0), ('', 9)):
            with self.subTest(tail=tail, code=code):
                self.config.data['runtime']['claude_output_mode'] = 'stream_metadata_v1'
                backend = StreamFixtureBackend(self.config, self.repo / '.harness', tail, code)
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
                self.assertEqual(engine.run['accepted'], {})
                self.assertEqual(sum(engine.run['attempts'].values()), 1)
                self.assertEqual(len(backend.calls), 1)
                worker = engine.run['dispatches'][-1]
                job = engine.directory / 'dispatches' / worker['id']
                self.assertFalse((job / 'response.json').exists())

    def test_mode_defaults_to_legacy_and_cannot_change_on_resume(self):
        self.assertEqual(self.config.data['runtime']['claude_output_mode'], 'legacy_json')
        snapshot = copy.deepcopy(self.config.data)
        snapshot['runtime'].pop('claude_output_mode')
        with self.assertRaisesRegex(HarnessError, '快照字段缺失'):
            self.config.resume_changes(snapshot)
        self.config.data['runtime']['claude_output_mode'] = 'stream_metadata_v1'
        with self.assertRaises(HarnessError):
            self.config.resume_changes(snapshot)

    def test_exact_command_capability_does_not_infer_provider_allowance(self):
        command = ['python3', '-m', 'unittest']
        self.config.data['permissions']['worker_commands'] = [command]
        exact = command_capability_projection(self.config, command)
        self.assertTrue(exact['local_exact_argv_match'])
        self.assertFalse(exact['provider_allowance_verified'])
        self.assertEqual(exact['provider_permission_reason'], 'unavailable')
        for other in (command + ['-v'], ['python3'], ['sh', '-c', 'python3 -m unittest']):
            self.assertFalse(command_capability_projection(self.config, other)['local_exact_argv_match'])

    def test_invalid_mode_rejected(self):
        self.config.data['runtime']['claude_output_mode'] = 'stream-json'
        with self.assertRaises(HarnessError):
            Config.validate(self.config.data)
