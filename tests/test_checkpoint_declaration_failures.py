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
        diagnostic = engine.run['feedback']['checkpoints_discarded']
        dispatch = next(d for d in engine.run['dispatches'] if d['role'] == 'worker')
        self.assertEqual(diagnostic['task_id'], 'greet')
        self.assertEqual(diagnostic['dispatch_id'], dispatch['id'])
        self.assertIn('checkpoint declared input SHA256 mismatch', diagnostic['reason'])
        self.assertEqual(engine.run['feedback']['worker']['blocker_kind'], 'operator')
        self.assertEqual(engine.run['feedback']['worker']['blockers'], ['Proceed with the operation?'])
        self.assertEqual(engine.run['accepted'], {})
        self.assertFalse(any(d['role'] == 'verification' for d in engine.run['dispatches']))
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)

    def test_discard_diagnostic_reaches_next_commander_and_worker_after_resume(self):
        def block_first(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker' and sum(c['role'] == 'worker' for c in backend.calls) == 1:
                result.update(status='blocked', blocker_kind='environment', blockers=['Service unavailable'],
                              checkpoints=[self.declaration(kwargs['cwd'])])
        backend = ScriptedBackend(block_first)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, '环境受阻'):
            engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        saved = read_json(engine.directory / 'state.json')
        diagnostic = saved['feedback']['checkpoints_discarded']
        self.assertEqual(diagnostic['task_id'], 'greet')
        self.assertEqual(diagnostic['dispatch_id'], saved['dispatches'][-1]['id'])
        self.assertEqual(saved['feedback']['worker']['blockers'], ['Service unavailable'])
        self.assertEqual(saved.get('task_checkpoints', []), [])
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        commander = next(c for c in backend.calls if c['role'] == 'commander')
        workers = [c for c in backend.calls if c['role'] == 'worker']
        self.assertEqual(commander['packet']['feedback']['checkpoints_discarded'], diagnostic)
        self.assertEqual(workers[1]['packet']['feedback']['checkpoints_discarded'], diagnostic)
        dispatches = [d for d in result['dispatches'] if d['role'] == 'worker']
        self.assertNotEqual(dispatches[0]['id'], dispatches[1]['id'])
        self.assertTrue(any(d['role'] == 'verification' for d in result['dispatches']))
        self.assertIn('checkpoints_discarded', (engine.directory / 'events.jsonl').read_text())

    def test_discard_reason_is_bounded_without_changing_operator_gate(self):
        engine = self.engine(self.backend(blocked=True))
        with patch.object(engine, 'record_checkpoints', side_effect=CheckpointDeclarationError('x' * 10000)):
            with self.assertRaises(HarnessError):
                engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        self.assertEqual(len(engine.run['feedback']['checkpoints_discarded']['reason']), 2000)
        self.assertEqual(len(engine.run['decision_requests']), 1)
        self.assertEqual(engine.run['accepted'], {})

    def test_partial_batch_registers_valid_checkpoints(self):
        def partial(items, kwargs):
            good = self.declaration(kwargs['cwd'], bad=False)
            good['id'] = 'first-valid'
            items.insert(0, good)
        engine = self.engine(self.backend(mutate=partial))
        result = engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        self.assertEqual(set(result['accepted']), {'greet'})
        self.assertEqual([r['checkpoint_id'] for r in result['task_checkpoints']], ['first-valid'])

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
        engine.run['checkpoint_batches'] = []
        resolution = engine.directory / 'checkpoints' / 'batches' / (dispatch['id'] + '.json')
        resolution.unlink(missing_ok=True)
        write_json(artifact, bound)
        result = {k: v for k, v in bound.items() if k not in ('task_id', 'dispatch_id')}
        source = {'artifact': str(artifact), 'artifact_digest': digest(artifact)}
        discarded = engine.record_checkpoints('greet', dispatch['id'], result, source)
        self.assertTrue(discarded)

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
