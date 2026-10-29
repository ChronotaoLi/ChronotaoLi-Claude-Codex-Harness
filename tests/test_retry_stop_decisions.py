"""Stopped planning retries preserve explicit operator decision boundaries."""
import copy
from unittest.mock import patch

from harness.contracts import PLAN
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, finding


class RetryStopDecisionTests(RepoCase):
    def saved(self, engine):
        return read_json(engine.directory / 'state.json')

    @staticmethod
    def budgets(state):
        return {key: copy.deepcopy(state[key]) for key in
                ('attempts', 'total_attempts', 'agent_dispatches', 'replan_attempts', 'final_attempts')}

    def answer(self, request, decision='continue'):
        path = self.repo / 'choice.json'
        write_json(path, dict(schema_version=1, request_id=request['id'], task_id=request['task_id'],
                             decision=decision, summary=f'Explicitly {decision} this original request'))
        return path

    def stopped_backend(self, target='greet', *, reason='Choose whether to continue', repeat_stop=False):
        def change(backend, kwargs, packet, result):
            if target == 'greet' and kwargs['role'] == 'worker' and sum(c['role'] == 'worker' for c in backend.calls) == 1:
                result.update(status='blocked', blocker_kind='task', blockers=['Need a revised approach'])
            elif target == 'final' and kwargs['kind'] == 'final_review' and sum(c['kind'] == 'final_review' for c in backend.calls) == 1:
                result.update(verdict='request_changes', findings=[finding()])
            elif kwargs['kind'] == 'replan':
                if repeat_stop or sum(c['kind'] == 'replan' for c in backend.calls) == 1:
                    result.update(action='stop', reason=reason, new_tasks=[], updates=[])
                elif target == 'greet':
                    result.update(action='revise', reason='Use the clarified approach', new_tasks=[],
                                  updates=[dict(id='greet', description='Implement using the clarified approach', add_depends_on=[])])
                else:
                    task = copy.deepcopy(packet['original_tasks'][0])
                    task.update(id='correct', depends_on=['greet'])
                    result.update(action='revise', reason='Resolve final finding', new_tasks=[task], updates=[])
            elif kwargs['role'] == 'worker' and kwargs['task']['id'] == 'correct':
                with (kwargs['cwd'] / 'hello.py').open('a') as stream:
                    stream.write('# independently reviewed correction\n')
        return ScriptedBackend(change)

    def stopped(self, target='greet', **kwargs):
        backend = self.stopped_backend(target, **kwargs)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        self.assertEqual(self.saved(engine)['replan_stopped']['target'], target)
        return engine, backend

    def assert_retry_needs_answer(self, engine, backend, request):
        before = self.saved(engine)
        calls = copy.deepcopy(backend.calls)
        with self.assertRaises(HarnessError) as caught:
            self.engine(backend).resume(engine.run['id'], retry_stopped=request['task_id'])
        self.assertIn(request['id'], str(caught.exception))
        self.assertIn('--operator-decision', str(caught.exception))
        after = self.saved(engine)
        self.assertEqual(self.budgets(after), self.budgets(before))
        self.assertEqual(after['replan_stopped'], before['replan_stopped'])
        self.assertEqual(after['operator_decisions'], before['operator_decisions'])
        self.assertNotIn('pending_replan', after)
        self.assertEqual(backend.calls, calls)

    def test_task_and_final_retry_reject_unanswered_current_request_before_dispatch(self):
        for target in ('greet', 'final'):
            with self.subTest(target=target):
                engine, backend = self.stopped(target)
                self.assert_retry_needs_answer(engine, backend, self.saved(engine)['decision_requests'][0])

    def test_plain_resume_keeps_stop_and_explicit_answer_reopens_original_request(self):
        engine, backend = self.stopped()
        before = self.saved(engine)
        calls = len(backend.calls)
        for unused in range(2):
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'])
            self.assertEqual(len(backend.calls), calls)
            self.assertEqual(self.budgets(self.saved(engine)), self.budgets(before))
        result = self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(before['decision_requests'][0]))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(backend.calls[calls]['kind'], 'replan')
        self.assertEqual(read_json(engine.directory / 'plans' / 'replan-2.json')['trigger'], before['replan_stopped']['trigger'])
        self.assertEqual(result['attempts'], {'greet': 2})
        self.assertEqual(result['operator_decisions'][0]['request_id'], before['decision_requests'][0]['id'])

    def test_stop_answer_requires_explicit_continue(self):
        engine, backend = self.stopped()
        request = self.saved(engine)['decision_requests'][0]
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(request, 'stop'))
        self.assert_retry_needs_answer(engine, backend, request)
        self.assertEqual(len(backend.calls), calls)
        result = self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(request))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([r['decision'] for r in result['operator_decisions']], ['stop', 'continue'])

    def test_saved_stop_without_registered_request_recovers_gate_before_retry(self):
        def interrupt_registration(*args, **kwargs):
            raise KeyboardInterrupt()
        backend = self.stopped_backend()
        engine = self.engine(backend)
        with patch.object(engine, 'register_decision_request', side_effect=interrupt_registration):
            with self.assertRaises(KeyboardInterrupt):
                engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        before = self.saved(engine)
        self.assertEqual(before['decision_requests'], [])
        self.assertIn('replan_stopped', before)
        calls = len(backend.calls)
        with self.assertRaises(HarnessError) as caught:
            self.engine(backend).resume(engine.run['id'], retry_stopped='greet')
        after = self.saved(engine)
        self.assertEqual(len(after['decision_requests']), 1)
        request = after['decision_requests'][0]
        self.assertIn(request['id'], str(caught.exception))
        self.assertIn('--operator-decision', str(caught.exception))
        self.assertEqual(self.budgets(after), self.budgets(before))
        self.assertEqual(len(backend.calls), calls)
        self.assert_retry_needs_answer(engine, backend, request)
        result = self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(request))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['decision_requests']), 1)

    def test_interrupted_durable_answer_can_reopen_without_reanswering(self):
        engine, backend = self.stopped()
        before = self.saved(engine)
        resumed = self.engine(backend)
        record = resumed.record_operator_decisions
        def record_then_interrupt(records):
            record(records)
            raise KeyboardInterrupt()
        with patch.object(resumed, 'record_operator_decisions', side_effect=record_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                resumed.resume(engine.run['id'], operator_decision=self.answer(before['decision_requests'][0]))
        interrupted = self.saved(engine)
        self.assertIn('replan_stopped', interrupted)
        self.assertEqual(len(interrupted['operator_decisions']), 1)
        self.assertEqual(self.budgets(interrupted), self.budgets(before))
        result = self.engine(backend).resume(engine.run['id'], retry_stopped='greet')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['operator_decisions']), 1)

    def test_answered_retry_interrupted_after_reopening_resumes_saved_planning(self):
        engine, backend = self.stopped()
        before = self.saved(engine)
        resumed = self.engine(backend)
        with patch.object(resumed, 'execute', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                resumed.resume(engine.run['id'], operator_decision=self.answer(before['decision_requests'][0]))
        interrupted = self.saved(engine)
        self.assertNotIn('replan_stopped', interrupted)
        self.assertEqual(interrupted['pending_replan']['trigger'], before['replan_stopped']['trigger'])
        self.assertEqual(self.budgets(interrupted), self.budgets(before))
        calls = len(backend.calls)
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(backend.calls[calls]['kind'], 'replan')

    def test_old_continue_does_not_answer_identical_new_stop(self):
        engine, backend = self.stopped(repeat_stop=True)
        old_request = self.saved(engine)['decision_requests'][0]
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(old_request))
        state = self.saved(engine)
        current = state['decision_requests'][-1]
        self.assertNotEqual(current['id'], old_request['id'])
        self.assertEqual(current['question'], old_request['question'])
        self.assert_retry_needs_answer(engine, backend, current)
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(old_request))
        self.assertEqual(len(backend.calls), calls)

    def test_stale_unanswered_request_does_not_block_legacy_retry(self):
        engine, backend = self.stopped()
        request = self.saved(engine)['decision_requests'][0]
        # Reproduce an older retry that invoked another model while retaining
        # the original stop/request. Expiration follows the real dispatch ledger.
        engine.dispatch('commander', 'plan', 'planning', PLAN, {'goal': engine.run['goal']})
        self.assertGreater(len(engine.run['dispatches']), request['dispatch_count'])
        engine.save()
        result = self.engine(backend).resume(engine.run['id'], retry_stopped='greet')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['operator_decisions'], [])

    def test_legacy_stop_without_source_does_not_reuse_unrelated_stop_answer(self):
        engine, backend = self.stopped()
        request = self.saved(engine)['decision_requests'][0]
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(request, 'stop'))
        engine.load_run(engine.run['id'])
        engine.run['replan_stopped'].pop('dispatch_id')
        engine.run['replan_stopped'].pop('source_digest')
        engine.save()
        result = self.engine(backend).resume(engine.run['id'], retry_stopped='greet')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([r['decision'] for r in result['operator_decisions']], ['stop'])

    def test_legacy_stop_without_registerable_request_keeps_explicit_retry(self):
        for legacy in ('no_source', 'no_digest', 'stale_source'):
            with self.subTest(legacy=legacy):
                backend = self.stopped_backend()
                engine = self.engine(backend)
                # Legacy execution could save a stop without registering a decision.
                with patch.object(engine, 'synchronize_decision_requests'):
                    with self.assertRaises(HarnessError):
                        engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
                if legacy == 'stale_source':
                    engine.dispatch('commander', 'plan', 'planning', PLAN, {'goal': engine.run['goal']})
                else:
                    engine.run['replan_stopped'].pop('source_digest')
                    if legacy == 'no_source':
                        engine.run['replan_stopped'].pop('dispatch_id')
                engine.save()
                self.assertEqual(self.saved(engine)['decision_requests'], [])
                result = self.engine(backend).resume(engine.run['id'], retry_stopped='greet')
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(result['operator_decisions'], [])

    def test_matching_registered_request_survives_missing_legacy_stop_digest(self):
        engine, backend = self.stopped()
        request = self.saved(engine)['decision_requests'][0]
        engine.run['replan_stopped'].pop('source_digest')
        engine.save()
        self.assert_retry_needs_answer(engine, backend, request)

    def test_mismatched_stop_digest_preserves_evidence_without_dispatch(self):
        engine, backend = self.stopped()
        engine.run['replan_stopped']['source_digest'] = '0' * 64
        engine.save()
        before = (engine.directory / 'state.json').read_bytes()
        calls = copy.deepcopy(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], retry_stopped='greet')
        self.assertEqual((engine.directory / 'state.json').read_bytes(), before)
        self.assertEqual(backend.calls, calls)

    def test_nul_stop_without_registerable_request_keeps_explicit_retry(self):
        engine, backend = self.stopped(reason='Choose whether to continue\0')
        before = self.saved(engine)
        self.assertEqual(before['decision_requests'], [])
        result = self.engine(backend).resume(engine.run['id'], retry_stopped='greet')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['operator_decisions'], [])
        self.assertEqual(result['replan_attempts'], before['replan_attempts'] + 1)
