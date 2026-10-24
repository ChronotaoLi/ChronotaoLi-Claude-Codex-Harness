import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from harness.adapters import CLIBackend
from harness.command_server import command_id
from harness.config import Config
from harness.engine import Engine

ROOT = Path(__file__).resolve().parents[1]


class CommandAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = Config.load(ROOT / 'harness.toml')
        self.literal = ['python3', '-c', 'print("literal")', '*', 'line\nbreak', "single'quote", '$(literal)']
        self.config.data['permissions']['worker_commands'] = [self.literal]
        self.config.data['verification']['commands'] = [['python3', 'verify.py']]

    def command(self, role):
        job = self.root / role
        job.mkdir()
        model = {'cli': 'claude', 'model': 'exact-configured-id', 'effort': 'high'}
        argv = CLIBackend(self.config, self.root / 'state').command(role, model, self.root, job, {}, None)
        return job, argv, json.loads((job / 'mcp.json').read_text())

    def test_worker_manifest_and_mcp_bind_exact_literal_argv(self):
        job, argv, mcp = self.command('worker')
        manifest_path = job / 'commands.json'
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest['dispatch_id'], job.name)
        self.assertEqual(manifest['cwd'], str(self.root))
        self.assertEqual(manifest['role'], 'worker')
        commands = manifest['commands']
        self.assertEqual(commands, [{'command_id': command_id(c), 'argv': c} for c in
                         self.config.data['verification']['commands'] + [self.literal]])
        server = mcp['mcpServers']['harness_commands']
        args = server['args']
        self.assertEqual(args[args.index('--manifest') + 1], str(manifest_path))
        self.assertEqual(args[args.index('--sha256') + 1], hashlib.sha256(manifest_path.read_bytes()).hexdigest())
        settings = json.loads(argv[argv.index('--settings') + 1])
        allow = settings['permissions']['allow']
        self.assertIn('mcp__harness_commands', allow)
        self.assertFalse(any(rule.startswith('Bash(python3 -c') for rule in allow))
        self.assertNotIn('Bash(python3 *)', allow)
        self.assertEqual(argv[argv.index('--model') + 1], 'exact-configured-id')
        self.assertEqual(argv[argv.index('--effort') + 1], 'high')
        self.assertIn('--no-session-persistence', argv)

    def test_readonly_roles_have_no_broker_or_command_manifest(self):
        for role in ('commander', 'reviewer'):
            job, argv, mcp = self.command(role)
            self.assertNotIn('harness_commands', mcp['mcpServers'])
            self.assertFalse((job / 'commands.json').exists())
            allow = json.loads(argv[argv.index('--settings') + 1])['permissions']['allow']
            self.assertNotIn('mcp__harness_commands', allow)

    def test_constraint_packet_ids_match_manifest_ids_and_argv(self):
        engine = object.__new__(Engine)
        engine.config = self.config
        packet = engine.tool_constraints()
        routing = packet['command_execution']
        self.assertTrue(routing['worker_only'])
        self.assertEqual(routing['tool'], 'mcp__harness_commands__run_command')
        self.assertEqual(routing['list_tool'], 'mcp__harness_commands__list_commands')
        self.assertEqual(routing['worker_command_ids'], [command_id(self.literal)])
        self.assertEqual(routing['verification_command_ids'], [command_id(c) for c in packet['verification_commands']])
        self.assertEqual(packet['worker_commands'], [self.literal])
