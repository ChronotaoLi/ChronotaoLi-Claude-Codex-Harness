import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from harness.progress import ProgressRecovery
from harness.util import HarnessError, write_json


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class Runtime(ProgressRecovery):
    def __init__(self, root):
        self.directory = root / 'run'
        self.worktree = root / 'worktree'
        self.worktree.mkdir()
        self.run = {'schema_version': 10, 'tasks': [{'id': 'task'}], 'dispatches': [], 'task_checkpoints': []}
        self.events = []
        self.saved = 0

    def save(self):
        self.saved += 1

    def event(self, name, **fields):
        self.events.append((name, fields))


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(Path(self.temp.name).resolve())
        (self.runtime.worktree / 'input').write_bytes(b'input bytes')
        (self.runtime.worktree / 'output').write_bytes(b'raw\x00output\xff\n')

    def record(self, dispatch='worker1', freshness='content', age=0, invoked_at='now'):
        rt = self.runtime
        item = {'id': 'check', 'summary': 'computation', 'inputs': [{'path': 'input', 'sha256': sha(b'input bytes')}],
                'evidence': [{'path': 'output', 'sha256': sha(b'raw\x00output\xff\n')}],
                'freshness': freshness, 'max_age_seconds': age}
        result = {'status': 'blocked', 'blocker_kind': 'environment', 'blockers': ['offline'],
                  'summary': 'blocked', 'evidence': [], 'proposed_memories': [], 'checkpoints': [item]}
        path = rt.directory / 'dispatches' / dispatch / 'returned-result.json'
        write_json(path, {**result, 'task_id': 'task', 'dispatch_id': dispatch})
        rt.run['dispatches'].append({'id': dispatch, 'task_id': 'task', 'role': 'worker', 'invoked_at': time.time() if invoked_at == 'now' else invoked_at})
        source = {'artifact': str(path), 'artifact_digest': sha(path.read_bytes())}
        rt.record_checkpoints('task', dispatch, result, source)
        return result, source

    def test_raw_copy_reuse_without_original_evidence_and_input_invalidation(self):
        self.record()
        rt = self.runtime
        (rt.worktree / 'output').unlink()
        packet = rt.progress_packet({'task': {'id': 'task'}})
        check = packet['task_checkpoints'][0]
        self.assertEqual(check['status'], 'reusable')
        self.assertEqual(Path(check['evidence'][0]['artifact']).read_bytes(), b'raw\x00output\xff\n')
        (rt.worktree / 'input').write_bytes(b'changed')
        self.assertEqual(rt.progress_packet({}, task_id='task')['task_checkpoints'][0]['status'], 'stale')
        (rt.worktree / 'input').unlink()
        self.assertEqual(rt.progress_packet({}, task_id='task')['task_checkpoints'][0]['status'], 'stale')

    def test_volatile_expiry_and_zero(self):
        with patch('harness.progress.time.time', return_value=100):
            self.record(freshness='volatile', age=10)
        with patch('harness.progress.time.time', return_value=109):
            self.assertEqual(self.runtime.progress_packet({})['task_checkpoints'][0]['status'], 'reusable')
        with patch('harness.progress.time.time', return_value=110):
            self.assertEqual(self.runtime.progress_packet({})['task_checkpoints'][0]['status'], 'stale')
        self.record('worker2', freshness='volatile', age=0)
        self.assertEqual(self.runtime.progress_packet({})['task_checkpoints'][0]['status'], 'stale')

    def test_supersession_and_idempotence(self):
        result, source = self.record()
        rt = self.runtime
        rt.record_checkpoints('task', 'worker1', result, source)
        self.assertEqual(len(rt.run['task_checkpoints']), 1)
        self.record('worker2')
        packet = rt.progress_packet({}, task_id='task')
        self.assertEqual(len(packet['task_checkpoints']), 1)
        self.assertEqual(packet['task_checkpoints'][0]['dispatch_id'], 'worker2')
        self.assertEqual(len(packet['checkpoint_history']), 2)
        self.assertTrue(Path(packet['checkpoint_history'][0]['artifact']).exists())
        self.assertEqual(rt.progress_packet({}, task_id='other')['task_checkpoints'], [])

    def test_cached_tampering_and_source_digest_required(self):
        result, source = self.record()
        rt = self.runtime
        check = rt.progress_packet({})['task_checkpoints'][0]
        Path(check['evidence'][0]['artifact']).write_bytes(b'changed')
        with self.assertRaises(HarnessError):
            rt.progress_packet({})
        with self.assertRaises(HarnessError):
            rt.record_checkpoints('task', 'worker1', result, {'artifact': source['artifact']})

    def test_artifact_before_ledger_recovery_preserves_timestamp(self):
        result, source = self.record()
        rt = self.runtime
        row = rt.run['task_checkpoints'][0].copy()
        original = Path(row['artifact']).read_bytes()
        rt.run['task_checkpoints'] = []
        (rt.worktree / 'output').unlink()
        rt.record_checkpoints('task', 'worker1', result, source)
        self.assertEqual(rt.run['task_checkpoints'], [row])
        self.assertEqual(Path(row['artifact']).read_bytes(), original)

    def test_source_result_tampering_fails_closed(self):
        _, source = self.record()
        Path(source['artifact']).write_text('{}')
        with self.assertRaises(HarnessError):
            self.runtime.progress_packet({})

    def test_symlink_evidence_and_declared_digest_mismatch_rejected(self):
        rt = self.runtime
        output = rt.worktree / 'output'
        output.unlink()
        output.symlink_to(rt.worktree / 'input')
        with self.assertRaises(HarnessError):
            self.record()
        self.assertEqual(rt.run['task_checkpoints'], [])
        output.unlink()
        output.write_bytes(b'different')
        with self.assertRaises(HarnessError):
            self.record('worker2')
        self.assertEqual(rt.run['task_checkpoints'], [])

    def test_size_bound_rejects_before_recording(self):
        (self.runtime.worktree / 'output').write_bytes(b'x' * (1024 * 1024 + 1))
        with self.assertRaises(HarnessError):
            self.record()
        self.assertEqual(self.runtime.run['task_checkpoints'], [])

    def test_audit_failure_does_not_register_checkpoint_and_recovers(self):
        rt = self.runtime
        with patch.object(rt, 'event', side_effect=HarnessError('audit unavailable')):
            with self.assertRaises(HarnessError):
                self.record()
        self.assertEqual(rt.run['task_checkpoints'], [])
        artifact = rt.directory / 'checkpoints' / 'worker1' / 'check.json'
        original = artifact.read_bytes()
        # Retrying the same dispatch uses its immutable evidence despite deletion.
        result = {key: value for key, value in json.loads((rt.directory / 'dispatches' / 'worker1' / 'returned-result.json').read_bytes()).items()
                  if key not in ('task_id', 'dispatch_id')}
        source_path = rt.directory / 'dispatches' / 'worker1' / 'returned-result.json'
        source = {'artifact': str(source_path), 'artifact_digest': sha(source_path.read_bytes())}
        (rt.worktree / 'output').unlink()
        rt.record_checkpoints('task', 'worker1', result, source)
        self.assertEqual(len(rt.run['task_checkpoints']), 1)
        self.assertEqual(artifact.read_bytes(), original)

    def test_reserved_git_state_is_not_evidence(self):
        rt = self.runtime
        # No file in Git administration may be harvested even with a matching hash.
        result, source = self.record()
        item = result['checkpoints'][0]
        item['evidence'][0]['path'] = '.git/config'
        source_path = rt.directory / 'dispatches' / 'worker2' / 'returned-result.json'
        write_json(source_path, {**result, 'task_id': 'task', 'dispatch_id': 'worker2'})
        source = {'artifact': str(source_path), 'artifact_digest': sha(source_path.read_bytes())}
        rt.run['dispatches'].append({'id': 'worker2', 'task_id': 'task', 'role': 'worker'})
        with self.assertRaisesRegex(HarnessError, 'reserved or unsafe'):
            rt.record_checkpoints('task', 'worker2', result, source)

    def test_large_valid_batch_bounds_context_and_retains_full_bound_details(self):
        rt = self.runtime
        long_input = './' * 1700 + 'input'
        long_output = './' * 1700 + 'output'
        checkpoints = [{'id': f'check-{i}', 'summary': 's' * 4000,
                        'inputs': [{'path': long_input, 'sha256': sha(b'input bytes')}] * 8,
                        'evidence': [{'path': long_output, 'sha256': sha(b'raw\x00output\xff\n')}] * 8,
                        'freshness': 'content', 'max_age_seconds': 0} for i in range(16)]
        result = {'status': 'blocked', 'blocker_kind': 'environment', 'blockers': ['offline'],
                  'summary': 'blocked', 'evidence': [], 'proposed_memories': [], 'checkpoints': checkpoints}
        source_path = rt.directory / 'dispatches' / 'worker1' / 'returned-result.json'
        write_json(source_path, {**result, 'task_id': 'task', 'dispatch_id': 'worker1'})
        source = {'artifact': str(source_path), 'artifact_digest': sha(source_path.read_bytes())}
        rt.run['dispatches'].append({'id': 'worker1', 'task_id': 'task', 'role': 'worker'})
        rt.record_checkpoints('task', 'worker1', result, source)
        packet = rt.progress_packet({}, task_id='task')
        self.assertLess(len(json.dumps(packet)), 32000)
        self.assertEqual(len(packet['task_checkpoints']), 16)
        self.assertLessEqual(sum(len(c['summary']) for c in packet['task_checkpoints']), 4000)
        for check in packet['task_checkpoints']:
            self.assertTrue(check['summary_truncated'])
            self.assertTrue(check['file_details_omitted'])
            self.assertEqual(check['input_count'], 8)
            self.assertEqual(check['evidence_count'], 8)
            manifest = json.loads(Path(check['artifact']).read_bytes())
            self.assertEqual(manifest['checkpoint']['summary'], 's' * 4000)
            self.assertEqual(manifest['checkpoint']['inputs'][0]['path'], long_input)
            self.assertEqual(sha(Path(check['artifact']).read_bytes()), check['artifact_digest'])
        (rt.worktree / 'input').unlink()
        stale = rt.progress_packet({}, task_id='task')['task_checkpoints']
        self.assertTrue(all(c['status'] == 'stale' and c['changed_input_indices'] == list(range(8)) for c in stale))

    def test_schema_promotion_initializes_all_version_ten_collections(self):
        self.runtime.run['schema_version'] = 9
        self.record()
        self.assertEqual(self.runtime.run['schema_version'], 10)
        for field in ('decision_requests', 'operator_decisions', 'environment_updates', 'environment_checks'):
            self.assertEqual(self.runtime.run[field], [])

    def test_bound_source_invalid_work_semantics_rejected(self):
        result, source = self.record()
        self.runtime.run['task_checkpoints'] = []
        result['blocker_kind'] = 'none'
        source_path = Path(source['artifact'])
        write_json(source_path, {**result, 'task_id': 'task', 'dispatch_id': 'worker1'})
        source['artifact_digest'] = sha(source_path.read_bytes())
        with self.assertRaises(HarnessError):
            self.runtime.record_checkpoints('task', 'worker1', result, source)

    def test_volatile_ttl_includes_long_worker_runtime(self):
        with patch('harness.progress.time.time', return_value=1000):
            self.record(freshness='volatile', age=60, invoked_at=100)
        with patch('harness.progress.time.time', return_value=1001):
            check = self.runtime.progress_packet({})['task_checkpoints'][0]
        self.assertEqual(check['recorded_at'], 1000)
        self.assertEqual(check['freshness_origin'], 100)
        self.assertEqual(check['status'], 'stale')
        self.assertIn('volatile evidence expired', check['reasons'])

    def test_invalid_invocation_times_never_refresh_volatile_evidence(self):
        for index, origin in enumerate((None, True, float('inf'), float('nan'), 1001)):
            with self.subTest(origin=origin), patch('harness.progress.time.time', return_value=1000):
                self.record(dispatch=f'worker{index}', freshness='volatile', age=60, invoked_at=origin)
                check = self.runtime.progress_packet({})['task_checkpoints'][0]
                self.assertIsNone(check['freshness_origin'])
                self.assertEqual(check['status'], 'stale')
                self.assertIn('volatile evidence has no valid source invocation time', check['reasons'])

    def test_legacy_manifest_without_origin_is_stale_and_cannot_extend_ttl(self):
        with patch('harness.progress.time.time', return_value=100):
            self.record(freshness='volatile', age=60)
        row = self.runtime.run['task_checkpoints'][0]
        record = json.loads(Path(row['artifact']).read_bytes())
        record.pop('freshness_origin')
        write_json(Path(row['artifact']), record)
        row['artifact_digest'] = sha(Path(row['artifact']).read_bytes())
        with patch('harness.progress.time.time', return_value=101):
            self.assertEqual(self.runtime.progress_packet({})['task_checkpoints'][0]['status'], 'stale')

    def test_freshness_origin_stays_bound_to_original_dispatch(self):
        with patch('harness.progress.time.time', return_value=100):
            self.record(freshness='volatile', age=60)
        self.runtime.run['dispatches'][0]['invoked_at'] = 101
        with self.assertRaisesRegex(HarnessError, 'freshness origin'):
            self.runtime.progress_packet({})
