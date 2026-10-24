"""Real local broker evidence with simulated workers; no provider integration claim."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from harness.command_server import Broker, command_id, create_manifest
from harness.progress import CheckpointDeclarationError
from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_progress_checkpoints import Runtime
from test_runtime_changes import ScriptedBackend


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BrokerCheckpointLifecycleTests(RepoCase):
    def test_format_repair_preserves_broker_evidence_boundary_and_optional_omission(self):
        for mode in ('old', 'omit', 'repeat'):
            with self.subTest(mode=mode):
                receipts = []
                def repair(backend, kwargs, packet, result):
                    if kwargs['role'] != 'worker':
                        return
                    count = sum(c['role'] == 'worker' for c in backend.calls)
                    if count == 1 or mode == 'repeat':
                        manifest = create_manifest(self.config, kwargs['cwd'], kwargs['job'], engine.directory / 'state.json')
                        receipt = Broker(manifest['artifact'], manifest['artifact_digest']).run_command(
                            {'command_id': command_id(self.config.data['verification']['commands'][0])})
                        self.assertEqual(receipt['exit_code'], 0)
                        receipts.append(receipt)
                    if count == 1 or mode != 'omit':
                        receipt = receipts[-1]
                        result['checkpoints'] = [{'id': 'check', 'summary': 'Observed command output',
                            'inputs': [], 'evidence': [{'path': receipt['stdout_path'], 'sha256': receipt['stdout_sha256']}],
                            'freshness': 'volatile', 'max_age_seconds': 60}]
                    if count == 1:
                        result['summary'] = 'x' * 4001
                backend = ScriptedBackend(repair)
                engine = self.engine(backend)
                result = engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
                self.assertEqual(result['status'], 'completed')
                workers = [d for d in result['dispatches'] if d['role'] == 'worker']
                self.assertEqual(len(workers), 2)
                self.assertNotEqual(workers[0]['id'], workers[1]['id'])
                repair_call = [c for c in backend.calls if c['role'] == 'worker'][1]
                self.assertEqual(repair_call['packet']['format_correction']['previous_dispatch'], workers[0]['id'])
                self.assertEqual(len(receipts), 2 if mode == 'repeat' else 1)
                rows = result['task_checkpoints']
                self.assertEqual(len(rows), 1 if mode == 'repeat' else 0)
                events = [json.loads(line) for line in (engine.directory / 'events.jsonl').read_text().splitlines()]
                self.assertEqual(sum(e['event'] == 'output_format_repair' for e in events), 1)
                discarded = [e for e in events if e['event'] == 'checkpoints_discarded']
                self.assertEqual(len(discarded), 1 if mode == 'old' else 0)
                if mode == 'repeat':
                    saved = read_json(Path(rows[0]['artifact']))
                    self.assertEqual(saved['freshness_origin'], workers[1]['invoked_at'])
                    self.assertIn(workers[1]['id'], saved['evidence'][0]['path'])
                self.assertTrue(Path(receipts[0]['stdout_path']).is_file())
                self.assertTrue(any(d['role'] == 'verification' for d in result['dispatches']))
                self.assertTrue(any(c['kind'] == 'review' for c in backend.calls))
                self.assertTrue(any(c['kind'] == 'final_review' for c in backend.calls))

    def test_broker_output_survives_resume_and_retains_acceptance_gates(self):
        receipts = []

        def block(backend, kwargs, packet, result):
            if kwargs['role'] != 'worker' or sum(c['role'] == 'worker' for c in backend.calls) != 1:
                return
            manifest = create_manifest(self.config, kwargs['cwd'], kwargs['job'], engine.directory / 'state.json')
            broker = Broker(manifest['artifact'], manifest['artifact_digest'])
            receipt = broker.run_command({'command_id': command_id(self.config.data['verification']['commands'][0])})
            receipts.append(receipt)
            self.assertEqual(receipt['exit_code'], 0)
            result.update(status='blocked', blocker_kind='environment', blockers=['Service unavailable'],
                          checkpoints=[{'id': 'tests', 'summary': 'Saved command output',
                              'inputs': [{'path': 'hello.py', 'sha256': digest(kwargs['cwd'] / 'hello.py')}],
                              'evidence': [{'path': receipt[s + '_path'], 'sha256': receipt[s + '_sha256']}
                                           for s in ('stdout', 'stderr')],
                              'freshness': 'content', 'max_age_seconds': 0}])

        backend = ScriptedBackend(block)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, '环境受阻'):
            engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        self.assertEqual(len(engine.run['task_checkpoints']), 1)
        raw = [Path(receipts[0][s + '_path']).read_bytes() for s in ('stdout', 'stderr')]
        for stream in ('stdout', 'stderr'):
            Path(receipts[0][stream + '_path']).unlink()
        state = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(state['status'], 'completed')
        workers = [c for c in backend.calls if c['role'] == 'worker']
        checkpoint = workers[1]['packet']['task_checkpoints'][0]
        self.assertEqual(checkpoint['status'], 'reusable')
        self.assertEqual([Path(e['artifact']).read_bytes() for e in checkpoint['evidence']], raw)
        self.assertEqual(len({d['id'] for d in state['dispatches'] if d['role'] == 'worker'}), 2)
        self.assertTrue(any(d['role'] == 'verification' and d['task_id'] == 'greet' for d in state['dispatches']))
        self.assertTrue(any(c['kind'] == 'review' for c in backend.calls))
        self.assertTrue(any(c['kind'] == 'final_review' for c in backend.calls))


class BrokerCheckpointBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.runtime = Runtime(self.root)
        self.runtime.directory = self.root / '.harness' / 'runs' / 'run'
        self.commands = self.runtime.directory / 'dispatches' / 'worker' / 'commands'
        self.output = self.commands / ('a' * 32) / 'output.stdout'
        self.output.parent.mkdir(parents=True)
        self.output.write_bytes(b'output')

    def test_only_current_dispatch_evidence_gets_private_path_exception(self):
        rt = self.runtime
        self.assertEqual(rt._declared_checkpoint_file(str(self.output), evidence_dispatch='worker'), b'output')
        other_run = self.root / '.harness' / 'runs' / 'other' / 'dispatches' / 'worker' / 'commands' / 'output'
        other_dispatch = self.runtime.directory / 'dispatches' / 'other' / 'commands' / 'output'
        sibling = self.commands.with_name('commands-extra') / 'output'
        private = self.runtime.directory / 'state.json'
        for path in (other_run, other_dispatch, sibling, private):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'output')
            with self.subTest(path=path), self.assertRaises(CheckpointDeclarationError):
                rt._declared_checkpoint_file(str(path), evidence_dispatch='worker')
        with self.assertRaises(CheckpointDeclarationError):
            rt._declared_checkpoint_file(str(self.output), digest=True)
        with self.assertRaises(HarnessError):
            rt._checkpoint_path(str(self.output))  # Freshness input checks get no exception either.
        with self.assertRaises(CheckpointDeclarationError):
            rt._declared_checkpoint_file(str(self.commands / '..' / 'commands' / self.output.parent.name / self.output.name),
                                         evidence_dispatch='worker')

    def test_symlink_components_and_size_limit_are_preserved(self):
        link = self.commands / 'link'
        link.symlink_to(self.output)
        parent_link = self.commands / 'directory-link'
        parent_link.symlink_to(self.output.parent, target_is_directory=True)
        for path in (link, parent_link / self.output.name):
            with self.subTest(path=path), self.assertRaises(CheckpointDeclarationError):
                self.runtime._declared_checkpoint_file(str(path), evidence_dispatch='worker')
        self.output.write_bytes(b'x' * (1024 * 1024 + 1))
        with self.assertRaisesRegex(CheckpointDeclarationError, 'exceeds 1 MiB'):
            self.runtime._declared_checkpoint_file(str(self.output), evidence_dispatch='worker')
