"""Optional evidence failures preserve lifecycle gates; all calls are simulated."""
import copy
from pathlib import Path
from unittest.mock import patch

from harness.progress import CheckpointDeclarationError
from harness.util import HarnessError, read_json
from test_checkpoint_lifecycle import digest
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class CheckpointDeclarationFailureTests(RepoCase):
    def declaration(self, cwd, *, bad=True):
        evidence = self.repo.resolve() / 'checkpoint-output.bin'
        evidence.write_bytes(b'raw evidence')
        return {'id': 'computation', 'summary': 'Computed local result',
                'inputs': [{'path': 'hello.py', 'sha256': '0' * 64 if bad else digest(cwd / 'hello.py')}],
                'evidence': [{'path': str(evidence), 'sha256': digest(evidence)}],
                'freshness': 'content', 'max_age_seconds': 0}

    def backend(self, *, blocked=False, mutate=None):
        def change(backend, kwargs, packet, result):
            if kwargs['role'] != 'worker':
                return
            result['checkpoints'] = [self.declaration(kwargs['cwd'])]
            if mutate:
                mutate(result['checkpoints'], kwargs)
            if blocked:
                result.update(status='blocked', blocker_kind='operator', blockers=['Proceed with the operation?'])
        return ScriptedBackend(change)

    def test_bad_hash_completed_still_verifies_reviews_and_accepts(self):
        backend = self.backend()
        engine = self.engine(backend)
        result = engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        self.assertEqual(set(result['accepted']), {'greet'})
        self.assertEqual(result['attempts'], {'greet': 1})
        self.assertEqual(result.get('task_checkpoints', []), [])
        self.assertEqual(sum(c['role'] == 'worker' for c in backend.calls), 1)
        self.assertTrue(any(d['role'] == 'verification' and d['task_id'] == 'greet' for d in result['dispatches']))
        self.assertTrue(any(c['kind'] == 'review' for c in backend.calls))
        events = (engine.directory / 'events.jsonl').read_text()
        self.assertIn('checkpoints_discarded', events)
        self.assertIn('checkpoint declared input SHA256 mismatch', events)

    def test_bad_checkpoint_operator_block_preserves_decision_gate(self):
        backend = self.backend(blocked=True)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        self.assertEqual(len(engine.run['decision_requests']), 1)
        self.assertEqual(engine.run['accepted'], {})
        self.assertFalse(any(d['role'] == 'verification' for d in engine.run['dispatches']))
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)

    def test_partial_batch_registers_no_checkpoints(self):
        def partial(items, kwargs):
            good = self.declaration(kwargs['cwd'], bad=False)
            good['id'] = 'first-valid'
            items.insert(0, good)
        engine = self.engine(self.backend(mutate=partial))
        result = engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        self.assertEqual(set(result['accepted']), {'greet'})
        self.assertEqual(result.get('task_checkpoints', []), [])

    def test_unsafe_missing_oversize_and_invalid_fields_are_declarations(self):
        mutations = [
            lambda item: item['evidence'][0].update(path='../unsafe'),
            lambda item: item['evidence'][0].update(path='missing-evidence'),
            lambda item: item.update(summary='invalid\0summary'),
            lambda item: item.update(unexpected='field'),
            lambda item: item['evidence'][0].update(sha256='f' * 64),
        ]
        backend = self.backend(blocked=True)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        dispatch = next(d for d in engine.run['dispatches'] if d['role'] == 'worker')
        artifact = engine.directory / 'dispatches' / dispatch['id'] / 'returned-result.json'
        original = read_json(artifact)
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bound = copy.deepcopy(original)
                bound['checkpoints'][0]['inputs'][0]['sha256'] = digest(engine.worktree / 'hello.py')
                mutate(bound['checkpoints'][0])
                self.assert_declaration(engine, dispatch, artifact, bound)
        bound = copy.deepcopy(original)
        evidence = Path(bound['checkpoints'][0]['evidence'][0]['path'])
        evidence.write_bytes(b'x' * (1024 * 1024 + 1))
        bound['checkpoints'][0]['inputs'][0]['sha256'] = digest(engine.worktree / 'hello.py')
        self.assert_declaration(engine, dispatch, artifact, bound)

    def assert_declaration(self, engine, dispatch, artifact, bound):
        from harness.util import write_json
        write_json(artifact, bound)
        result = {k: v for k, v in bound.items() if k not in ('task_id', 'dispatch_id')}
        source = {'artifact': str(artifact), 'artifact_digest': digest(artifact)}
        with self.assertRaises(CheckpointDeclarationError):
            engine.record_checkpoints('greet', dispatch['id'], result, source)

    def test_changed_bound_result_is_fatal_even_with_malformed_declaration(self):
        backend = self.backend()
        engine = self.engine(backend)
        original_record = engine.record_checkpoints
        def tamper(task_id, dispatch_id, result, source):
            result['checkpoints'][0]['summary'] = 'invalid\0summary'
            Path(source['artifact']).write_bytes(b'changed original')
            return original_record(task_id, dispatch_id, result, source)
        with patch.object(engine, 'record_checkpoints', side_effect=tamper):
            with self.assertRaisesRegex(HarnessError, 'original result changed') as error:
                engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        self.assertNotIsInstance(error.exception, CheckpointDeclarationError)
        self.assertFalse(any(d['role'] == 'verification' for d in engine.run['dispatches']))
        self.assertEqual(len(backend.calls), 1)

    def test_existing_cached_evidence_is_fatal_before_malformed_new_fields(self):
        from test_checkpoint_lifecycle import CheckpointLifecycleTests
        engine, backend, _ = CheckpointLifecycleTests.blocked(self)
        row = engine.run['task_checkpoints'][0]
        manifest = read_json(Path(row['artifact']))
        Path(manifest['evidence'][0]['artifact']).write_bytes(b'changed cached evidence')
        dispatch = next(d for d in engine.run['dispatches'] if d['id'] == row['dispatch_id'])
        source = manifest['source']
        original = read_json(Path(source['artifact']))
        result = {k: v for k, v in original.items() if k not in ('task_id', 'dispatch_id')}
        result['checkpoints'] = 'malformed'
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, 'cached evidence changed') as error:
            engine.record_checkpoints('greet', dispatch['id'], result, source)
        self.assertNotIsInstance(error.exception, CheckpointDeclarationError)
        with self.assertRaisesRegex(HarnessError, 'cached evidence changed'):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)
        self.assertFalse(any(d['role'] == 'verification' for d in engine.run['dispatches']))

    def test_private_manifest_storage_failure_remains_fatal(self):
        def valid(items, kwargs):
            items[0]['inputs'][0]['sha256'] = digest(kwargs['cwd'] / 'hello.py')
        backend = self.backend(mutate=valid)
        engine = self.engine(backend)
        with patch('harness.progress.write_json', side_effect=OSError('storage unavailable')):
            with self.assertRaisesRegex(OSError, 'storage unavailable'):
                engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        self.assertEqual(engine.run.get('task_checkpoints', []), [])
        self.assertFalse(any(d['role'] == 'verification' for d in engine.run['dispatches']))
        self.assertEqual(len(backend.calls), 1)
