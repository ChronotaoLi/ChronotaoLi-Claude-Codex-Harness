"""Eligible handoff navigation and operator gates through real Engine packets."""
import copy
import hashlib
from pathlib import Path

from harness.util import HarnessError
import test_decision_lifecycle
from test_harness import RepoCase
from test_runtime_changes import ScriptedBackend


class HandoffProjectionTests(RepoCase):
    plan = test_decision_lifecycle.DecisionLifecycleTests.plan
    saved = test_decision_lifecycle.DecisionLifecycleTests.saved
    response = test_decision_lifecycle.DecisionLifecycleTests.response

    def test_targets_are_exact_and_validation_uses_same_projection(self):
        engine = self.engine()
        engine.run = {'id': 'run_targets', 'tasks': [dict(id='source', title='Source', depends_on=[]),
                                dict(id='middle', title='Middle', depends_on=['source']),
                                dict(id='last', title='Last', depends_on=['middle']),
                                dict(id='other', title='Other', depends_on=[])], 'accepted': {}}
        self.assertEqual(engine.services.workflow.handoff_targets('source'),
                         [{'id': 'middle', 'title': 'Middle'}, {'id': 'last', 'title': 'Last'}])
        for target in ('middle', 'last'):
            engine.services.workflow.validate_handoff_requests('source', {'decision_requests': [
                {'task_id': target, 'question': 'Proceed?'}]})
        engine.run['accepted']['middle'] = {}
        self.assertEqual(engine.services.workflow.handoff_targets('source'), [{'id': 'last', 'title': 'Last'}])
        for target in ('source', 'other', 'missing', 'middle'):
            with self.subTest(target=target), self.assertRaises(HarnessError):
                engine.services.workflow.validate_handoff_requests('source', {'decision_requests': [
                    {'task_id': target, 'question': 'Proceed?'}]})
        self.assertEqual(engine.services.workflow.handoff_targets('last'), [])
        with self.assertRaises(HarnessError):
            engine.services.workflow.validate_handoff_requests('last', {'decision_requests': [
                {'task_id': 'last', 'question': 'Proceed?'}]})

    def test_packet_selected_transitive_target_survives_resume_and_blocks_dispatch(self):
        def choose_from_packet(backend, kwargs, packet, result):
            if kwargs['role'] != 'worker':
                return
            targets = packet['handoff_targets']
            if packet['task']['id'] == 'greet':
                self.assertEqual(targets, [{'id': 'dependent', 'title': 'Dependent work'},
                                           {'id': 'last', 'title': 'Final operation'}])
                target = next(t for t in targets if t['title'] == 'Final operation')
                result['decision_requests'] = [{'id': 'risk', 'task_id': target['id'],
                                               'question': 'Proceed with final operation?'}]
            else:
                with (kwargs['cwd'] / 'hello.py').open('a') as stream:
                    stream.write('# ' + packet['task']['id'] + '\n')
        backend = ScriptedBackend(choose_from_packet)
        engine = self.engine(backend)
        plan = self.plan(dependent=True)
        task = copy.deepcopy(plan['tasks'][0])
        task.update(id='last', title='Final operation', depends_on=['dependent'])
        plan['tasks'].append(task)
        with self.assertRaises(HarnessError):
            engine.start('Implement all work', plan)
        saved = self.saved(engine)
        self.assertEqual(set(saved['accepted']), {'greet', 'dependent'})
        self.assertEqual(saved['decision_requests'][0]['task_id'], 'last')
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(self.saved(engine)['attempts'], saved['attempts'])
        result = self.engine(backend).resume(engine.run['id'], operator_decision=self.response(engine))
        self.assertEqual(result['status'], 'completed')
        workers = [d for d in result['dispatches'] if d['role'] == 'worker']
        self.assertEqual(len({d['id'] for d in workers}), 3)
        for ident in ('greet', 'dependent', 'last'):
            self.assertTrue(any(d['role'] == 'verification' and d['task_id'] == ident
                                for d in result['dispatches']))
            self.assertTrue(any(c['kind'] == 'review' and c['task_id'] == ident for c in backend.calls))
        self.assertTrue(any(c['kind'] == 'final_review' for c in backend.calls))

    def test_invalid_handoff_retains_checkpoint_for_fresh_worker_without_acceptance(self):
        evidence = self.repo.resolve() / 'handoff-check-output.bin'
        evidence.write_bytes(b'actual check output\n')
        evidence_bytes = evidence.read_bytes()
        seen = {}

        def retain_check(backend, kwargs, packet, result):
            if kwargs['kind'] == 'decide' and not seen:
                # Before the next attempt: the rejected output was never verified,
                # reviewed or accepted, and its checkpoint is retained.
                saved = self.saved(engine)
                first = next(d for d in saved['dispatches'] if d['role'] == 'worker')
                self.assertEqual((first['status'], first['worker_receipt_consumed']), ('invalid', 'result_rejected'))
                self.assertEqual(saved['accepted'], {})
                self.assertEqual(saved['decision_requests'], [])
                self.assertEqual(len(saved['task_checkpoints']), 1)
                self.assertFalse(any(d['role'] in ('verification', 'reviewer') for d in saved['dispatches']))
                original = engine.directory / 'dispatches' / first['id'] / 'returned-result.json'
                seen.update(dispatch=first['id'], original=original, bytes=original.read_bytes())
                evidence.unlink()
            if kwargs['role'] != 'worker':
                return
            workers = [call for call in backend.calls if call['role'] == 'worker']
            if len(workers) == 1:
                result['checkpoints'] = [{
                    'id': 'checked-input', 'summary': 'Saved input check',
                    'inputs': [{'path': 'hello.py', 'sha256': hashlib.sha256(
                        (kwargs['cwd'] / 'hello.py').read_bytes()).hexdigest()}],
                    'evidence': [{'path': str(evidence), 'sha256': hashlib.sha256(evidence_bytes).hexdigest()}],
                    'freshness': 'content', 'max_age_seconds': 0}]
                result['decision_requests'] = [{'id': 'risk', 'task_id': packet['task']['id'],
                                               'question': 'Proceed with later work?'}]
            else:
                checkpoint = packet['task_checkpoints'][0]
                self.assertEqual(checkpoint['status'], 'reusable')
                self.assertEqual(Path(checkpoint['evidence'][0]['artifact']).read_bytes(), evidence_bytes)
                self.assertEqual(checkpoint['dispatch_id'], seen['dispatch'])
                source = packet['checkpoint_sources'][checkpoint['dispatch_id']]
                self.assertEqual(Path(source['artifact']).read_bytes(), seen['bytes'])

        backend = ScriptedBackend(retain_check)
        engine = self.engine(backend)
        result = engine.start('Implement greet', self.plan())
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(evidence.exists())
        self.assertEqual(seen['original'].read_bytes(), seen['bytes'])
        workers = [d for d in result['dispatches'] if d['role'] == 'worker']
        self.assertEqual(len({d['id'] for d in workers}), 2)
        self.assertEqual(result['attempts'], {'greet': 2})
        self.assertEqual(set(result['accepted']), {'greet'})
        self.assertEqual(result['decision_requests'], [])
        self.assertTrue(any(d['role'] == 'verification' and d['task_id'] == 'greet'
                            for d in result['dispatches']))
        self.assertTrue(any(c['kind'] == 'review' for c in backend.calls))
        self.assertTrue(any(c['kind'] == 'final_review' for c in backend.calls))
