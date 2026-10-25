"""Partial optional evidence batches retain bindings and recover atomically."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.util import HarnessError, write_json
from test_progress_checkpoints import Runtime, sha


class CheckpointBatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.rt = Runtime(Path(temp.name).resolve())
        (self.rt.worktree / 'out').write_bytes(b'proof')
        self.item = {'id': 'valid', 'summary': 'observed', 'inputs': [],
                     'evidence': [{'path': 'out', 'sha256': sha(b'proof')}],
                     'freshness': 'volatile', 'max_age_seconds': 10}

    def bind(self, items):
        rt = self.rt
        result = {'status': 'blocked', 'blocker_kind': 'environment', 'blockers': ['offline'],
                  'summary': 'blocked', 'evidence': [], 'proposed_memories': [], 'checkpoints': items}
        path = rt.directory / 'dispatches' / 'worker1' / 'returned-result.json'
        write_json(path, {**result, 'task_id': 'task', 'dispatch_id': 'worker1'})
        rt.run['dispatches'] = [{'id': 'worker1', 'task_id': 'task', 'role': 'worker', 'invoked_at': 100}]
        return result, {'artifact': str(path), 'artifact_digest': sha(path.read_bytes())}

    def record(self, result, source):
        return self.rt.record_checkpoints('task', 'worker1', result, source)

    def test_mixed_schema_and_source_failures_keep_valid_and_recovery_time(self):
        bad = copy.deepcopy(self.item)
        bad.update(id='missing', evidence=[{'path': 'missing', 'sha256': '0' * 64}])
        result, source = self.bind([bad, self.item, {'id': 'malformed'}])
        with patch('harness.progress.time.time', return_value=105):
            discarded = self.record(result, source)
        self.assertEqual([d['index'] for d in discarded], [0, 2])
        row = copy.deepcopy(self.rt.run['task_checkpoints'][0])
        manifest = Path(row['artifact']).read_bytes()
        (self.rt.worktree / 'out').unlink()
        with patch('harness.progress.time.time', return_value=200):
            self.assertEqual(self.record(result, source), discarded)
            check = self.rt.progress_packet({})['task_checkpoints'][0]
        self.assertEqual(check['freshness_origin'], 100)
        self.assertEqual(check['status'], 'stale')
        self.assertEqual(Path(row['artifact']).read_bytes(), manifest)
        self.assertEqual(self.rt.run['task_checkpoints'], [row])

    def test_resolution_before_ledger_recovers_only_approved_rows(self):
        bad = copy.deepcopy(self.item)
        bad.update(id='bad', summary='\0')
        result, source = self.bind([self.item, bad])
        with patch.object(self.rt, 'event', side_effect=HarnessError('audit failure')):
            with self.assertRaisesRegex(HarnessError, 'audit failure'):
                self.record(result, source)
        self.assertEqual(self.rt.run['task_checkpoints'], [])
        (self.rt.worktree / 'out').unlink()
        self.assertEqual(self.record(result, source)[0]['index'], 1)
        self.assertEqual(len(self.rt.run['task_checkpoints']), 1)
        self.rt.run['task_checkpoints'] = []
        with self.assertRaisesRegex(HarnessError, 'incomplete'):
            self.rt.progress_packet({})

    def test_resolution_tampering_and_duplicate_batch_fail_closed(self):
        result, source = self.bind([self.item])
        self.record(result, source)
        batch = self.rt.run['checkpoint_batches'][0]
        self.rt.run['checkpoint_batches'].append(copy.deepcopy(batch))
        with self.assertRaisesRegex(HarnessError, 'duplicate'):
            self.rt.progress_packet({})
        self.rt.run['checkpoint_batches'].pop()
        Path(batch['artifact']).write_text('{}')
        with self.assertRaisesRegex(HarnessError, 'resolution changed'):
            self.record(result, source)

    def test_write_failure_never_registers_partial_batch(self):
        result, source = self.bind([self.item])
        real = write_json
        def write(path, value):
            if path.parent.name == 'batches':
                raise OSError('disk failure')
            real(path, value)
        with patch('harness.progress.write_json', side_effect=write):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                self.record(result, source)
        self.assertEqual(self.rt.run['task_checkpoints'], [])
        (self.rt.worktree / 'out').unlink()
        self.assertEqual(self.record(result, source), [])
        self.assertEqual(len(self.rt.run['task_checkpoints']), 1)

    def test_old_broker_evidence_remains_readable_without_new_dispatch_refresh(self):
        rt = self.rt
        rt.directory = rt.worktree.parent / '.harness' / 'runs' / 'run'
        broker_output = rt.directory / 'dispatches' / 'worker1' / 'commands' / 'command' / 'stdout'
        broker_output.parent.mkdir(parents=True)
        broker_output.write_bytes(b'proof')
        self.item['evidence'][0]['path'] = str(broker_output)
        result, source = self.bind([self.item])
        with patch('harness.progress.time.time', return_value=105):
            self.record(result, source)
        old_row = copy.deepcopy(rt.run['task_checkpoints'][0])
        path = rt.directory / 'dispatches' / 'worker2' / 'returned-result.json'
        write_json(path, {**result, 'task_id': 'task', 'dispatch_id': 'worker2'})
        rt.run['dispatches'].append({'id': 'worker2', 'task_id': 'task', 'role': 'worker', 'invoked_at': 200})
        source2 = {'artifact': str(path), 'artifact_digest': sha(path.read_bytes())}
        with patch('harness.progress.time.time', return_value=201):
            discarded = rt.record_checkpoints('task', 'worker2', result, source2)
            packet = rt.progress_packet({})
        self.assertIn('reserved or unsafe', discarded[0]['reason'])
        self.assertEqual(rt.run['task_checkpoints'], [old_row])
        self.assertEqual(packet['task_checkpoints'][0]['freshness_origin'], 100)
        self.assertEqual(packet['task_checkpoints'][0]['status'], 'stale')
        index = json.loads(Path(packet['checkpoint_index']['artifact']).read_bytes())
        record = json.loads(Path(index['checkpoints'][0]['artifact']).read_bytes())
        self.assertEqual(Path(record['evidence'][0]['artifact']).read_bytes(), b'proof')
