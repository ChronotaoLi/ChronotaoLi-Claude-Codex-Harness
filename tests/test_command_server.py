import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from harness.command_server import (Broker, command_id, command_processes, create_manifest,
                                    ensure_command_processes_exited, handle)
from harness.util import HarnessError, write_json


class CommandServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.job = self.root / 'dispatch'

    def broker(self, commands, timeout=3):
        config = {'verification': {'commands': commands}, 'permissions': {'worker_commands': commands},
                  'runtime': {'verify_timeout_seconds': timeout}}
        metadata = create_manifest(config, self.root, self.job, None)
        return Broker(metadata['artifact'], metadata['artifact_digest'])

    def test_exact_argv_and_private_receipts(self):
        argv = [sys.executable, '-c', 'import json,sys;print(json.dumps(sys.argv[1:]));sys.stderr.write("err");sys.exit(7)',
                '*', 'one\ntwo', '$(touch nope)', '; touch nope']
        broker = self.broker([argv])
        self.assertEqual(len(broker.list_commands()), 1)
        result = broker.run_command({'command_id': command_id(argv)})
        self.assertEqual(json.loads(result['stdout']), argv[3:])
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(result['stderr'], 'err')
        self.assertFalse((self.root / 'nope').exists())
        self.assertEqual(hashlib.sha256(Path(result['stdout_path']).read_bytes()).hexdigest(), result['stdout_sha256'])
        self.assertTrue((Path(result['stdout_path']).parent / 'receipt.json').exists())
        self.assertTrue(command_processes(self.job)[0]['exit_confirmed'])

    def test_unauthorized_and_extra_fields_do_not_execute(self):
        argv = [sys.executable, '-c', 'raise Exception("should not run")']
        broker = self.broker([argv])
        for args in ({'command_id': 'no'}, {'command_id': command_id(argv), 'argv': argv}, {}):
            with self.assertRaises(HarnessError):
                broker.run_command(args)
        self.assertEqual(command_processes(self.job), [])
        self.assertTrue(handle({'method': 'tools/call', 'params': {'name': 'list_commands', 'arguments': {'cwd': '.'}}}, broker)['isError'])

    def test_manifest_tamper_and_duplicate_ids(self):
        broker = self.broker([[sys.executable, '-V']])
        manifest = broker.manifest.copy()
        manifest['commands'] *= 2
        write_json(broker.path, manifest)
        with self.assertRaises(HarnessError):
            broker.list_commands()
        digest = hashlib.sha256(broker.path.read_bytes()).hexdigest()
        with self.assertRaises(HarnessError):
            Broker(broker.path, digest)

    def test_start_failure_and_timeout_have_receipts(self):
        argv = [str(self.root / 'missing')]
        sleep = [sys.executable, '-c', 'import time;time.sleep(5)']
        broker = self.broker([argv, sleep], timeout=.1)
        missing = broker.run_command({'command_id': command_id(argv)})
        self.assertIsNotNone(missing['error'])
        self.assertTrue(missing['exit_confirmed'])
        timed = broker.run_command({'command_id': command_id(sleep)})
        self.assertTrue(timed['timed_out'])
        self.assertTrue(timed['exit_confirmed'])
        ensure_command_processes_exited(self.job)

    def test_starting_journal_requires_exact_operator_assertion(self):
        broker = self.broker([])
        folder = self.job / 'commands' / ('a' * 32)
        folder.mkdir()
        write_json(folder / 'process.json', {'id': self.job.name + ':command:' + folder.name,
                   'command_id': 'fixture', 'pgid': None, 'process_state': 'starting', 'exit_confirmed': False})
        record = command_processes(self.job)[0]
        with self.assertRaises(HarnessError):
            ensure_command_processes_exited(self.job)
        parent = {'command_exit_confirmations': {record['id']: {'journal_digest': record['journal_digest']}}}
        ensure_command_processes_exited(self.job, parent)
        write_json(folder / 'process.json', {k: v for k, v in record.items() if k not in ('journal', 'journal_digest')} | {'pgid': 123})
        with self.assertRaises(HarnessError):
            ensure_command_processes_exited(self.job, parent, observe=lambda _: True)

    def test_symlink_journal_refused(self):
        self.broker([])
        (self.job / 'commands' / ('b' * 32)).symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(HarnessError):
            command_processes(self.job)

    def test_fresh_stdio_protocol(self):
        broker = self.broker([[sys.executable, '-V']])
        entry = Path(__file__).resolve().parents[1] / 'harness' / 'command_entry.py'
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'list_commands', 'arguments': {}}}]
        process = subprocess.run([sys.executable, str(entry), '--manifest', str(broker.path), '--sha256', broker.digest],
                                 input=''.join(json.dumps(r) + '\n' for r in requests), text=True, capture_output=True, timeout=5, cwd=self.root)
        self.assertEqual(process.returncode, 0, process.stderr)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([r['id'] for r in responses], [1, 2])
        self.assertEqual(json.loads(responses[1]['result']['content'][0]['text']), broker.list_commands())

    def test_live_source_binding_and_dispatch_tamper(self):
        run_path = self.root / 'run.json'
        state = {'status': 'running', 'worktree': str(self.root), 'expected_head': 'head',
                 'worktree_binding': {'fixture': True}, 'dispatches': [
                     {'id': self.job.name, 'role': 'worker', 'kind': 'implement', 'status': 'running',
                      'invoked': True, 'command_directory': str(self.job / 'commands')}]}
        write_json(run_path, state)
        config = {'verification': {'commands': []}, 'permissions': {'worker_commands': []},
                  'runtime': {'verify_timeout_seconds': 1}}
        metadata = create_manifest(config, self.root, self.job, run_path)
        with patch('harness.command_server.git.check_worktree_binding'), patch('harness.command_server.git.head', return_value='head'):
            broker = Broker(metadata['artifact'], metadata['artifact_digest'])
            self.assertEqual(broker.list_commands(), [])
            state['dispatches'][0]['role'] = 'reviewer'
            write_json(run_path, state)
            with self.assertRaises(HarnessError):
                broker.list_commands()

    def test_large_output_hashing_does_not_load_full_log(self):
        argv = [sys.executable, '-c', 'import sys;sys.stdout.write("x" * 2000000)']
        broker = self.broker([argv])
        original_read = Path.read_bytes
        def bounded_read(path):
            if path.name.startswith('output.'):
                raise AssertionError('log must be hashed incrementally')
            return original_read(path)
        with patch.object(Path, 'read_bytes', bounded_read):
            result = broker.run_command({'command_id': command_id(argv)})
        self.assertEqual(result['exit_code'], 0)
        self.assertLess(len(result['stdout']), 4100)
        self.assertEqual(result['stdout_sha256'], hashlib.sha256(b'x' * 2000000).hexdigest())

    def test_standard_progress_metadata_and_version_negotiation(self):
        argv = [sys.executable, '-c', 'print("metadata call executed")']
        broker = self.broker([argv])
        request = {'method': 'tools/call', 'params': {'name': 'run_command',
                   'arguments': {'command_id': command_id(argv)},
                   '_meta': {'progressToken': 'claude-progress-token'}}}
        response = handle(request, broker)
        self.assertFalse(response['isError'])
        self.assertEqual(json.loads(response['content'][0]['text'])['exit_code'], 0)
        request['params']['arguments']['cwd'] = '/'
        self.assertTrue(handle(request, broker)['isError'])
        request['params'].pop('arguments')
        request['params']['argv'] = ['unauthorized']
        rejected = handle(request, broker)
        self.assertTrue(rejected['isError'])
        self.assertIn('argv', rejected['content'][0]['text'])
        self.assertNotIn('unauthorized', rejected['content'][0]['text'])
        for requested, expected in [('2025-06-18', '2025-06-18'), ('2026-01-01', '2025-11-25')]:
            result = handle({'method': 'initialize', 'params': {'protocolVersion': requested}}, broker)
            self.assertEqual(result['protocolVersion'], expected)
