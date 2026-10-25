"""Bound artifact validation is shared only within one checkpoint operation."""
from collections import Counter
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import harness.progress as progress
from harness.util import HarnessError, write_json
from test_progress_checkpoints import Runtime, sha


class CheckpointReadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runtime = Runtime(Path(temp.name).resolve())
        rt = self.runtime
        (rt.worktree / 'input').write_bytes(b'input')
        (rt.worktree / 'output').write_bytes(b'proof')
        items = [{'id': f'check-{i}', 'summary': 'observed',
                  'inputs': [{'path': 'input', 'sha256': sha(b'input')}],
                  'evidence': [{'path': 'output', 'sha256': sha(b'proof')}],
                  'freshness': 'content', 'max_age_seconds': 0} for i in range(3)]
        result = {'status': 'blocked', 'blocker_kind': 'environment', 'blockers': ['offline'],
                  'summary': 'blocked', 'evidence': [], 'proposed_memories': [], 'checkpoints': items}
        self.source = rt.directory / 'dispatches' / 'worker' / 'returned-result.json'
        write_json(self.source, {**result, 'task_id': 'task', 'dispatch_id': 'worker'})
        rt.run['dispatches'] = [{'id': 'worker', 'task_id': 'task', 'role': 'worker', 'invoked_at': 100}]
        rt.record_checkpoints('task', 'worker', result,
                              {'artifact': str(self.source), 'artifact_digest': sha(self.source.read_bytes())})
        self.manifests = [Path(row['artifact']) for row in rt.run['task_checkpoints']]
        self.evidence = [Path(json.loads(path.read_bytes())['evidence'][0]['artifact'])
                         for path in self.manifests]
        self.resolution = Path(rt.run['checkpoint_batches'][0]['artifact'])

    def test_validation_and_projection_read_hash_and_parse_bound_artifacts_once(self):
        artifacts = [self.source, self.resolution, *self.manifests, *self.evidence]
        for operation in (self.runtime.validate_checkpoint_links, lambda: self.runtime.progress_packet({})):
            with self.subTest(operation=operation), \
                    patch('harness.progress._read', wraps=progress._read) as reads, \
                    patch('harness.progress._digest', wraps=progress._digest) as digests, \
                    patch('harness.progress.json.loads', wraps=json.loads) as parses:
                operation()
            counts = Counter(Path(call.args[0]) for call in reads.call_args_list)
            self.assertEqual({path: counts[path] for path in artifacts}, {path: 1 for path in artifacts})
            # Evidence bytes have no parsed representation; their hashes still
            # must be checked once per distinct private copy, including history.
            self.assertEqual(sum(call.args[0] == b'proof' for call in digests.call_args_list), len(self.evidence))
            self.assertEqual(parses.call_count, 2 + len(self.manifests))

    def test_next_operation_rechecks_every_private_artifact(self):
        artifacts = [(self.source, 'original result changed'),
                     (self.resolution, 'resolution changed'),
                     (self.manifests[0], 'manifest changed'),
                     (self.evidence[0], 'cached evidence changed')]
        for path, message in artifacts:
            with self.subTest(path=path):
                original = path.read_bytes()
                self.runtime.progress_packet({})
                path.write_bytes(b'tampered')
                try:
                    with self.assertRaisesRegex(HarnessError, message):
                        self.runtime.validate_checkpoint_links()
                    with self.assertRaisesRegex(HarnessError, message):
                        self.runtime.progress_packet({})
                finally:
                    path.write_bytes(original)

    def test_next_operation_rejects_symlink_with_identical_bytes(self):
        self.runtime.progress_packet({})
        evidence = self.evidence[0]
        copy_path = self.runtime.worktree / 'identical-proof'
        copy_path.write_bytes(evidence.read_bytes())
        evidence.unlink()
        evidence.symlink_to(copy_path)
        with self.assertRaisesRegex(HarnessError, 'non-symlink file'):
            self.runtime.progress_packet({})

    def test_reused_manifest_keeps_exact_batch_digest_and_ledger_bindings(self):
        self.runtime.validate_checkpoint_links()
        original = self.resolution.read_bytes()
        resolution = json.loads(original)
        resolution['rows'][0]['artifact_digest'] = '0' * 64
        write_json(self.resolution, resolution)
        self.runtime.run['checkpoint_batches'][0]['artifact_digest'] = sha(self.resolution.read_bytes())
        # The manifest has already been validated from the ledger when the
        # batch presents a different hash for that same path.
        with self.assertRaisesRegex(HarnessError, 'manifest changed'):
            self.runtime.progress_packet({})

        self.resolution.write_bytes(original)
        self.runtime.run['checkpoint_batches'][0]['artifact_digest'] = sha(original)
        row = copy.deepcopy(self.runtime.run['task_checkpoints'].pop())
        with self.assertRaisesRegex(HarnessError, 'ledger is incomplete'):
            self.runtime.progress_packet({})
        self.runtime.run['task_checkpoints'].append(row)
        self.assertEqual(len(self.runtime.progress_packet({})['task_checkpoints']), 3)

    def test_live_inputs_are_rehashed_on_later_projection(self):
        self.assertTrue(all(row['status'] == 'reusable'
                            for row in self.runtime.progress_packet({})['task_checkpoints']))
        (self.runtime.worktree / 'input').write_bytes(b'changed')
        rows = self.runtime.progress_packet({})['task_checkpoints']
        self.assertTrue(all(row['status'] == 'stale' and row['changed_input_indices'] == [0] for row in rows))
