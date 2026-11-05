"""Offline evidence and permission gates; no simulated model proves live access."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from harness.continuation import (continuation_gate, packet, record_continuation,
                                  record_execution_guidance, resolve_continuation)
from harness.recovery import snapshot_digest
from harness.util import HarnessError, write_json


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.candidate = {'head': 'head', 'index': 'index', 'files': {'code': 'candidate'}}
        self.saved = []
        self.context = SimpleNamespace(directory=self.root / 'run',
            run={'id': 'run', 'tasks': [{'id': 'task'}], 'accepted': {}, 'dispatches': [],
                 'current_task': 'task', 'status': 'blocked', 'attempts': {'task': 1}, 'rounds': 3},
            config={'runtime': {'agent_timeout_seconds': 30}, 'permissions': {'worker_commands': []},
                    'verification': {'commands': [['python3', '-m', 'unittest']]}},
            snapshot=lambda: deepcopy(self.candidate), save=lambda: self.saved.append(True))
        self.result = dict(task_id='task', dispatch_id='worker1', status='blocked', blocker_kind='continuation',
            summary='remaining edits', evidence=[], blockers=['need exact test'], proposed_memories=[],
            continuation_request={'kind': 'commands', 'commands': [['python3', 'focused.py']],
                                  'minimum_timeout_seconds': 0, 'reason': 'focused regression'})

    def record(self, dispatch='worker1'):
        self.result['dispatch_id'] = dispatch
        path = self.context.directory / 'dispatches' / dispatch / 'returned-result.json'
        write_json(path, self.result)
        self.source = {'dispatch_id': dispatch, 'artifact': str(path),
                       'artifact_digest': hashlib.sha256(path.read_bytes()).hexdigest()}
        self.context.run['dispatches'].append({'id': dispatch, 'role': 'worker', 'task_id': 'task', 'invoked': True})
        return record_continuation(self.context, 'task', self.result, self.source, self.candidate)

    def test_exact_grant_gate_preserves_candidate_and_cumulative_budgets(self):
        self.record()
        before = deepcopy(self.context.run)
        self.assertEqual(continuation_gate(self.context)['missing_commands'], [['python3', 'focused.py']])
        self.context.config['permissions']['worker_commands'] = [['python3']]
        self.assertIsNotNone(continuation_gate(self.context))
        with self.assertRaises(HarnessError):
            resolve_continuation(self.context)
        self.context.config['permissions']['worker_commands'].append(['python3', 'focused.py'])
        resolve_continuation(self.context)
        self.assertIsNone(self.context.run['pending_continuation'])
        for key in ('attempts', 'rounds', 'continuation_history', 'accepted'):
            self.assertEqual(self.context.run[key], before[key])
        self.assertEqual(self.context.snapshot(), self.candidate)

    def test_budget_request_does_not_implicitly_extend_or_reset(self):
        self.result['continuation_request'] = {'kind': 'budget', 'commands': [], 'minimum_timeout_seconds': 90, 'reason': 'long computation'}
        self.record()
        self.assertEqual(continuation_gate(self.context)['minimum_timeout_seconds'], 90)
        self.assertEqual(self.context.config['runtime']['agent_timeout_seconds'], 30)
        self.context.config['runtime']['agent_timeout_seconds'] = 90
        self.assertIsNone(continuation_gate(self.context))

    def test_changed_candidate_and_changed_original_source_fail_closed(self):
        self.record()
        self.candidate['files']['code'] = 'edited'
        with self.assertRaisesRegex(HarnessError, 'candidate'):
            continuation_gate(self.context)
        Path(self.source['artifact']).write_text('{}')
        with self.assertRaises(HarnessError):
            packet(self.context, 'task')

    def test_pending_request_cannot_be_replayed_after_another_model_call(self):
        self.record()
        self.context.run['dispatches'].append({'id': 'commander2', 'role': 'commander', 'invoked': True})
        with self.assertRaisesRegex(HarnessError, 'superseded'):
            continuation_gate(self.context)

    def test_repeated_requests_preserve_immutable_history(self):
        first = self.record()
        self.context.config['permissions']['worker_commands'] = [['python3', 'focused.py']]
        resolve_continuation(self.context)
        self.context.run['attempts']['task'] += 1
        second = self.record('worker2')
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(len(packet(self.context)['continuation_history']), 2)
        self.assertEqual(self.context.run['attempts']['task'], 2)
        self.assertTrue(Path(first['artifact']).exists())

    def guidance(self):
        path = self.root / 'guidance.json'
        write_json(path, {'task_id': 'task', 'source_dispatch_id': 'worker1',
            'source_snapshot_digest': snapshot_digest(self.candidate), 'summary': 'Check the rollback path before expensive verification.'})
        return path

    def test_guidance_is_bound_and_permission_neutral(self):
        self.record()
        config = deepcopy(self.context.config)
        row = record_execution_guidance(self.context, self.guidance())
        self.assertEqual(packet(self.context, 'task')['execution_guidance'][0]['id'], row['id'])
        self.assertEqual(self.context.config, config)
        self.assertIsNotNone(continuation_gate(self.context))
        Path(row['artifact']).write_text('{}')
        with self.assertRaises(HarnessError):
            packet(self.context)

    def test_guidance_rejects_live_dispatch_or_stale_candidate(self):
        self.record()
        path = self.guidance()
        self.context.run['status'] = 'running'
        with self.assertRaisesRegex(HarnessError, 'safe'):
            record_execution_guidance(self.context, path)
        self.context.run['status'] = 'blocked'
        self.candidate['files']['code'] = 'changed'
        with self.assertRaisesRegex(HarnessError, 'candidate'):
            record_execution_guidance(self.context, path)

    def test_guidance_feedback_requires_recorded_source_snapshot(self):
        self.record()
        self.context.run['continuation_history'] = []
        self.context.run['pending_continuation'] = None
        self.context.run['feedback'] = {'task_id': 'task',
            'worker_artifact': self.source['artifact'],
            'worker_artifact_digest': self.source['artifact_digest']}
        path = self.guidance()
        with self.assertRaisesRegex(HarnessError, 'candidate lacks authoritative'):
            record_execution_guidance(self.context, path)
        self.context.run['feedback']['snapshot_digest'] = snapshot_digest(self.candidate)
        self.assertIsNotNone(record_execution_guidance(self.context, path))

    def test_invalid_pending_pointer_and_symlink_source_block_projection(self):
        row = self.record()
        self.context.run['pending_continuation'] = 'missing'
        with self.assertRaisesRegex(HarnessError, 'pointer'):
            packet(self.context)
        self.context.run['pending_continuation'] = row['id']
        source = Path(self.source['artifact'])
        other = self.root / 'same-bytes.json'
        other.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(other)
        with self.assertRaisesRegex(HarnessError, 'non-symlink'):
            continuation_gate(self.context)

    def test_guidance_does_not_trust_unbound_original_bytes(self):
        self.record()
        self.context.run['continuation_history'] = []
        self.context.run['pending_continuation'] = None
        with self.assertRaisesRegex(HarnessError, 'authoritative'):
            record_execution_guidance(self.context, self.guidance())


if __name__ == '__main__':
    unittest.main()
