"""Optional checkpoint semantics preserve worker outcomes and acceptance gates.

All agent responses are simulated; Git, verification and recovery are real.
"""
import copy
import json
from pathlib import Path

from harness.budgets import work_attempts
from harness.util import HarnessError, read_json, write_json
from test_checkpoint_lifecycle import digest
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class CheckpointSemanticsTests(RepoCase):
    variants = ('empty_inputs', 'content_ttl', 'duplicate_ids')

    def backend(self, variant, blocker=None, *, always=False):
        def change(backend, kwargs, packet, result):
            if kwargs['role'] != 'worker':
                return
            count = sum(c['role'] == 'worker' for c in backend.calls)
            if count != 1 and not always:
                return
            ref = {'path': 'hello.py', 'sha256': digest(kwargs['cwd'] / 'hello.py')}
            good = {'id': 'valid', 'summary': 'Observed candidate', 'inputs': [ref],
                    'evidence': [ref.copy()], 'freshness': 'content', 'max_age_seconds': 0}
            bad = copy.deepcopy(good)
            bad['id'] = 'invalid'
            if variant == 'empty_inputs':
                bad['inputs'] = []
            elif variant == 'content_ttl':
                bad['max_age_seconds'] = 1
            elif variant == 'duplicate_ids':
                bad['id'] = good['id']
            elif variant == 'over_limit':
                result['checkpoints'] = [dict(good, id=f'item_{i}') for i in range(17)]
            else:
                raise AssertionError(variant)
            if variant != 'over_limit':
                result['checkpoints'] = [bad, good]
            if blocker:
                result.update(status='blocked', blocker_kind=blocker, blockers=['Wait for prerequisite'])
        return ScriptedBackend(change)

    def start(self, backend):
        engine = self.engine(backend)
        engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        return engine

    def assert_discard(self, engine, variant):
        run = engine.run
        self.assertEqual([r['checkpoint_id'] for r in run.get('task_checkpoints', [])],
                         [] if variant == 'duplicate_ids' else ['valid'])
        worker = next(d for d in run['dispatches'] if d['role'] == 'worker')
        self.assertEqual(worker['status'], 'completed')
        original = read_json(engine.directory / 'dispatches' / worker['id'] / 'returned-result.json')
        self.assertEqual(len(original['checkpoints']), 2)
        self.assertNotIn('worker_recovery', run)
        events = [json.loads(line) for line in (engine.directory / 'events.jsonl').read_text().splitlines()]
        self.assertTrue(any(e.get('event') == 'checkpoints_discarded' for e in events))
        if variant != 'duplicate_ids':
            batch = run['checkpoint_batches'][0]
            resolution = read_json(Path(batch['artifact']))
            self.assertEqual([d['index'] for d in resolution['discarded']], [0])

    def assert_accepted(self, engine, backend):
        self.assertEqual(engine.run['status'], 'completed')
        self.assertEqual(set(engine.run['accepted']), {'greet'})
        for target in ('greet', 'final'):
            self.assertTrue(any(d['role'] == 'verification' and d['task_id'] == target
                                for d in engine.run['dispatches']))
        self.assertEqual([c['kind'] for c in backend.calls if c['role'] == 'reviewer'],
                         ['review', 'final_review'])

    def test_completed_discards_only_optional_evidence_and_still_verifies_reviews(self):
        for variant in self.variants:
            with self.subTest(variant=variant):
                backend = self.backend(variant)
                engine = self.start(backend)
                self.assert_discard(engine, variant)
                self.assert_accepted(engine, backend)
                self.assertEqual(engine.run['attempts'], {'greet': 1})
                self.assertEqual(sum(c['role'] == 'worker' for c in backend.calls), 1)

    def test_operator_semantic_errors_preserve_gate_and_budget_exemption(self):
        self.config.data['runtime']['max_attempts'] = 1
        for variant in self.variants:
            with self.subTest(variant=variant):
                backend = self.backend(variant, 'operator')
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
                self.assert_discard(engine, variant)
                self.assertEqual(engine.run['feedback']['worker']['blocker_kind'], 'operator')
                self.assertEqual(engine.run['feedback']['worker']['blockers'], ['Wait for prerequisite'])
                self.assertEqual(work_attempts(engine.run), {'greet': 0})
                self.assertEqual(len(engine.run['decision_requests']), 1)
                self.assertEqual(engine.run['accepted'], {})
                with self.assertRaisesRegex(HarnessError, '等待操作者决定'):
                    self.engine(backend).resume(engine.run['id'])
                self.assertEqual(len(backend.calls), 1)
                self.assertFalse(any(d['role'] == 'verification' for d in engine.run['dispatches']))
                self.assertEqual(work_attempts(read_json(engine.directory / 'state.json')), {'greet': 0})

    def test_environment_semantic_errors_preserve_feedback_update_and_resume(self):
        self.config.data['runtime']['max_attempts'] = 1
        for variant in self.variants:
            with self.subTest(variant=variant):
                backend = self.backend(variant, 'environment')
                engine = self.engine(backend)
                with self.assertRaisesRegex(HarnessError, '环境受阻'):
                    engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
                self.assert_discard(engine, variant)
                before = read_json(engine.directory / 'state.json')
                self.assertEqual(before['feedback']['worker']['blocker_kind'], 'environment')
                self.assertEqual(before['feedback']['worker']['blockers'], ['Wait for prerequisite'])
                self.assertIn('checkpoints_discarded', before['feedback'])
                self.assertEqual(work_attempts(before), {'greet': 0})
                worker = next(d for d in before['dispatches'] if d['role'] == 'worker')
                update = self.repo / 'environment-update.json'
                write_json(update, {'schema_version': 1, 'task_id': 'greet',
                                    'blocker_dispatch_id': worker['id'], 'summary': 'Prerequisite restored'})
                resumed = self.engine(backend)
                result = resumed.resume(before['id'], environment_update=update)
                self.assert_accepted(resumed, backend)
                self.assertEqual(result['attempts'], {'greet': 2})
                self.assertEqual(work_attempts(result), {'greet': 1})
                self.assertEqual(len(result['environment_updates']), 1)
                workers = [d for d in result['dispatches'] if d['role'] == 'worker']
                self.assertNotEqual(workers[0]['id'], workers[1]['id'])
                self.assertTrue(all(c['packet']['feedback']['checkpoints_discarded'] ==
                                    before['feedback']['checkpoints_discarded']
                                    for c in backend.calls[1:] if c['role'] in ('worker', 'commander')))

    def test_over_limit_completed_gets_one_bounded_format_correction(self):
        backend = self.backend('over_limit')
        engine = self.start(backend)
        self.assert_accepted(engine, backend)
        workers = [c for c in backend.calls if c['role'] == 'worker']
        self.assertEqual(len(workers), 2)
        self.assertIn('format_correction', workers[1]['packet'])
        self.assertEqual(engine.run['attempts'], {'greet': 2})
        backend = self.backend('over_limit', always=True)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        self.assertEqual(sum(c['role'] == 'worker' for c in backend.calls), 2)
        self.assertEqual(engine.run['accepted'], {})
        self.assertFalse(any(d['role'] == 'verification' for d in engine.run['dispatches']))

    def test_over_limit_blocked_keeps_blocker_without_correction_or_exemption(self):
        for blocker in ('operator', 'environment'):
            with self.subTest(blocker=blocker):
                backend = self.backend('over_limit', blocker)
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
                self.assertEqual(engine.run['feedback']['worker']['blocker_kind'], blocker)
                self.assertEqual(engine.run['feedback']['worker']['blockers'], ['Wait for prerequisite'])
                self.assertEqual(work_attempts(engine.run), {'greet': 1})
                self.assertEqual(len(backend.calls), 1)
                self.assertEqual(engine.run['accepted'], {})
                self.assertFalse(any(d['role'] == 'verification' for d in engine.run['dispatches']))
                if blocker == 'operator':
                    self.assertEqual(len(engine.run['decision_requests']), 1)
                    with self.assertRaisesRegex(HarnessError, '等待操作者决定'):
                        self.engine(backend).resume(engine.run['id'])
                    self.assertEqual(len(backend.calls), 1)
