"""Offline Engine lifecycle coverage for explicit task-scoped choices."""
import copy
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, finding


class DecisionLifecycleTests(RepoCase):
    def plan(self, dependent=False, unrelated=False):
        plan = read_json(ROOT / 'examples' / 'plan.json')
        if dependent:
            task = copy.deepcopy(plan['tasks'][0])
            task.update(id='dependent', title='Dependent work', depends_on=['greet'])
            plan['tasks'].append(task)
        if unrelated:
            task = copy.deepcopy(plan['tasks'][0])
            task.update(id='other', title='Independent work', depends_on=[])
            plan['tasks'].append(task)
        return plan

    def saved(self, engine):
        return read_json(engine.directory / 'state.json')

    def response(self, engine, decision='continue', request=None, summary='Continue original task with the proposed choice'):
        request = request or self.saved(engine)['decision_requests'][0]
        path = self.repo / 'operator-choice.json'
        write_json(path, {'schema_version': 1, 'request_id': request['id'], 'task_id': request['task_id'],
                          'decision': decision, 'summary': summary})
        return path

    def blocked(self):
        def first_block(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker' and sum(c['role'] == 'worker' for c in backend.calls) == 1:
                result.update(status='blocked', blocker_kind='operator', blockers=['Choose whether this work should continue'])
        backend = ScriptedBackend(first_block)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', self.plan())
        self.assertEqual(len(engine.run['decision_requests']), 1)
        return engine, backend

    def test_operator_block_stops_without_replan_and_plain_resume_spends_no_budget(self):
        engine, backend = self.blocked()
        before = self.saved(engine)
        self.assertEqual([c['role'] for c in backend.calls], ['worker'])
        self.assertEqual(before['replan_attempts'], 0)
        for _ in range(2):
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'])
        saved = self.saved(engine)
        for key in ('attempts', 'total_attempts', 'agent_dispatches', 'accepted', 'decision_requests'):
            self.assertEqual(saved[key], before[key])
        self.assertEqual(len(backend.calls), 1)

    def test_continue_starts_fresh_worker_and_reaches_required_review_context(self):
        engine, backend = self.blocked()
        before = self.saved(engine)
        result = self.engine(backend).resume(engine.run['id'], operator_decision=self.response(engine))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts'], {'greet': 2})
        self.assertEqual(result['tasks'], before['tasks'])
        self.assertEqual(result['original_tasks'], before['original_tasks'])
        workers = [d for d in result['dispatches'] if d['role'] == 'worker']
        self.assertEqual(len({d['id'] for d in workers}), 2)
        self.assertTrue(all(d['id'] != workers[0]['id'] for d in workers[1:]))
        for call in backend.calls[1:]:
            with self.subTest(kind=call['kind']):
                decision = call['packet']['operator_decisions'][0]
                self.assertEqual(decision['task_id'], 'greet')
                self.assertEqual(decision['response']['decision'], 'continue')
                self.assertEqual(decision['source']['dispatch_id'], workers[0]['id'])
        self.assertTrue(any(c['kind'] == 'review' for c in backend.calls))
        self.assertTrue(any(c['kind'] == 'final_review' for c in backend.calls))

    def test_stop_then_continue_and_repeated_stop_are_durable_without_model_calls(self):
        engine, backend = self.blocked()
        path = self.response(engine, 'stop')
        for _ in range(2):
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'], operator_decision=path)
            self.assertEqual(len(backend.calls), 1)
            self.assertEqual(len(self.saved(engine)['operator_decisions']), 1)
        result = self.engine(backend).resume(engine.run['id'], operator_decision=self.response(engine))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([r['decision'] for r in result['operator_decisions']], ['stop', 'continue'])

    def handoff(self, reject_source=False, unrelated=False):
        def request_choice(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker' and kwargs['task']['id'] == 'greet':
                result['decision_requests'] = [{'id': 'dependent-choice', 'task_id': 'dependent',
                                               'question': 'Continue with dependent work?'}]
            if kwargs['kind'] == 'review' and reject_source:
                result.update(verdict='request_changes', findings=[finding()])
            if kwargs['role'] == 'worker' and kwargs['task']['id'] != 'greet':
                with (kwargs['cwd'] / 'hello.py').open('a') as stream:
                    stream.write('# ' + kwargs['task']['id'] + '\n')
        backend = ScriptedBackend(request_choice)
        engine = self.engine(backend)
        return engine, backend, self.plan(dependent=True, unrelated=unrelated)

    def test_handoff_only_registers_after_acceptance_and_blocks_dependent_worker(self):
        engine, backend, plan = self.handoff()
        with self.assertRaises(HarnessError):
            engine.start('Implement greet and dependent work', plan)
        saved = self.saved(engine)
        self.assertEqual(set(saved['accepted']), {'greet'})
        self.assertEqual([c['task_id'] for c in backend.calls if c['role'] == 'worker'], ['greet'])
        reviewer = next(c for c in backend.calls if c['kind'] == 'review')
        self.assertEqual(reviewer['packet']['decision_requests'][0]['task_id'], 'dependent')
        request = saved['decision_requests'][0]
        self.assertEqual(request['kind'], 'handoff')
        self.assertEqual(saved['accepted']['greet']['worker_source'], request['source'])
        result = self.engine(backend).resume(engine.run['id'], operator_decision=self.response(engine))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(set(result['accepted']), {'greet', 'dependent'})
        dependent = next(c for c in backend.calls if c['role'] == 'worker' and c['task_id'] == 'dependent')
        self.assertEqual(dependent['packet']['operator_decisions'][0]['response']['decision'], 'continue')

    def test_rejected_source_never_creates_handoff_gate(self):
        self.config.data['runtime']['max_attempts'] = 1
        engine, backend, plan = self.handoff(reject_source=True)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet and dependent work', plan)
        state = self.saved(engine)
        self.assertEqual(state['accepted'], {})
        self.assertEqual(state['decision_requests'], [])
        self.assertFalse(any(c['role'] == 'worker' and c['task_id'] == 'dependent' for c in backend.calls))

    def test_unrelated_work_runs_while_handoff_waits(self):
        engine, backend, plan = self.handoff(unrelated=True)
        with self.assertRaises(HarnessError):
            engine.start('Implement all work', plan)
        saved = self.saved(engine)
        self.assertEqual(set(saved['accepted']), {'greet', 'other'})
        other = next(c for c in backend.calls if c['role'] == 'worker' and c['task_id'] == 'other')
        self.assertEqual(other['packet'].get('operator_decisions', []), [])
        self.assertFalse(any(c['role'] == 'worker' and c['task_id'] == 'dependent' for c in backend.calls))
        result = self.engine(backend).resume(engine.run['id'], operator_decision=self.response(engine))
        self.assertEqual(result['status'], 'completed')

    def test_handoff_stop_can_change_after_unrelated_task_is_accepted(self):
        engine, backend, plan = self.handoff(unrelated=True)
        initial = engine.start('Implement all work', plan, step=True)
        self.assertEqual(set(initial['accepted']), {'greet'})
        request = initial['decision_requests'][0]
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], operator_decision=self.response(engine, 'stop'))
        self.assertEqual(len(backend.calls), calls)
        stopped = self.engine(backend).resume(engine.run['id'], step=True)
        self.assertEqual(set(stopped['accepted']), {'greet', 'other'})
        self.assertEqual(stopped['operator_decisions'][0]['decision'], 'stop')
        self.assertFalse(any(c['role'] == 'worker' and c['task_id'] == 'dependent' for c in backend.calls))
        result = self.engine(backend).resume(engine.run['id'], operator_decision=self.response(engine))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(set(result['accepted']), {'greet', 'other', 'dependent'})
        self.assertEqual([r['decision'] for r in result['operator_decisions']], ['stop', 'continue'])
        self.assertEqual(result['decision_requests'][0]['source'], request['source'])
        for task_id in ('other', 'dependent'):
            self.assertTrue(any(c['kind'] == 'review' and c['task_id'] == task_id for c in backend.calls))
            self.assertTrue(any(d['role'] == 'verification' and d['task_id'] == task_id for d in result['dispatches']))
        self.assertTrue(any(c['kind'] == 'final_review' for c in backend.calls))

    def test_handoff_answer_is_stale_after_target_worker_invocation(self):
        engine, backend, plan = self.handoff()
        engine.start('Implement source and dependent work', plan, step=True)
        original_change = backend.change

        def block_target(backend, kwargs, packet, result):
            original_change(backend, kwargs, packet, result)
            if kwargs['role'] == 'worker' and kwargs['task']['id'] == 'dependent':
                result.update(status='blocked', blocker_kind='environment', blockers=['Service unavailable'])

        backend.change = block_target
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], operator_decision=self.response(engine))
        self.assertTrue(any(c['role'] == 'worker' and c['task_id'] == 'dependent' for c in backend.calls))
        before = (engine.directory / 'state.json').read_bytes()
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, 'stale after subsequent model invocation'):
            self.engine(backend).resume(engine.run['id'], operator_decision=self.response(engine, 'stop'))
        self.assertEqual((engine.directory / 'state.json').read_bytes(), before)
        self.assertEqual(len(backend.calls), calls)

    def test_invalid_combinations_and_tampered_artifacts_have_no_ledger_effects(self):
        engine, backend = self.blocked()
        path = self.response(engine)
        before = (engine.directory / 'state.json').read_bytes()
        for options in ({'environment_update': path}, {'retry_stopped': 'greet'}, {'reverify': True},
                        {'repair_invalid': 'missing'}, {'allow_scope': ['extra.py']}):
            with self.subTest(options=options), self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'], operator_decision=path, **options)
            self.assertEqual((engine.directory / 'state.json').read_bytes(), before)
        request = self.saved(engine)['decision_requests'][0]
        for source in (request['artifact'], request['source']['artifact']):
            artifact = Path(source)
            raw = artifact.read_bytes()
            artifact.write_bytes(raw + b' ')
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'], operator_decision=path)
            self.assertEqual((engine.directory / 'state.json').read_bytes(), before)
            artifact.write_bytes(raw)
        self.assertEqual(len(backend.calls), 1)

    def test_decision_cannot_bypass_pending_review(self):
        engine, backend = self.blocked()
        resumed = self.engine(backend)
        with patch.object(resumed, 'dispatch_review', side_effect=HarnessError('review offline')):
            with self.assertRaises(HarnessError):
                resumed.resume(engine.run['id'], operator_decision=self.response(engine))
        saved = self.saved(engine)
        self.assertEqual(saved['pending_task']['stage'], 'reviewing')
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], operator_decision=self.response(engine, 'stop'))
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(self.saved(engine)['pending_task'], saved['pending_task'])
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(any(c['role'] == 'worker' for c in backend.calls[calls:]))

    def test_continue_preserves_exhausted_worker_budget(self):
        self.config.data['runtime']['max_attempts'] = 1
        engine, backend = self.blocked()
        before = self.saved(engine)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], operator_decision=self.response(engine))
        state = self.saved(engine)
        self.assertEqual(len(state['operator_decisions']), 1)
        self.assertEqual(state['attempts'], before['attempts'])
        self.assertEqual(state['total_attempts'], before['total_attempts'])
        self.assertEqual(len(backend.calls), 1)
        self.config.data['runtime']['max_attempts'] = 2
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts'], {'greet': 2})

    def test_handoff_registration_waits_for_post_accept_completion(self):
        engine, backend, plan = self.handoff()
        finish = engine.finish_post_accept

        def unavailable_after_accept():
            if engine.run.get('pending_post_accept'):
                raise HarnessError('post accept unavailable')
            return finish()

        with patch.object(engine, 'finish_post_accept', side_effect=unavailable_after_accept):
            with self.assertRaises(HarnessError):
                engine.start('Implement source and dependent', plan)
        state = self.saved(engine)
        self.assertEqual(set(state['accepted']), {'greet'})
        self.assertTrue(state['pending_post_accept'])
        self.assertEqual(state['decision_requests'], [])
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        state = self.saved(engine)
        self.assertNotIn('pending_post_accept', state)
        self.assertEqual(len(state['decision_requests']), 1)
        self.assertEqual(len(backend.calls), calls)

    def test_replan_stop_continue_replans_without_rerolling_rejected_final_review(self):
        def reject_then_stop(backend, kwargs, packet, result):
            if kwargs['kind'] == 'final_review':
                result.update(verdict='request_changes', findings=[finding()])
        backend = ScriptedBackend(reject_then_stop)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', self.plan())
        saved = self.saved(engine)
        self.assertEqual(saved['decision_requests'][0]['kind'], 'replan_stop')
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], operator_decision=self.response(engine))
        self.assertEqual([c['kind'] for c in backend.calls[calls:]], ['replan'])
        self.assertEqual(sum(c['kind'] == 'final_review' for c in backend.calls), 1)
        state = self.saved(engine)
        self.assertEqual(state['final_attempts'], 1)
        self.assertEqual(state['replan_attempts'], 2)
        self.assertEqual(state['accepted'], saved['accepted'])
        # The old answer names the earlier commander result, not this new stop.
        snapshot = (engine.directory / 'state.json').read_bytes()
        calls = len(backend.calls)
        old_request = saved['decision_requests'][0]
        old_answer = self.response(engine, request=old_request)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], operator_decision=old_answer)
        self.assertEqual((engine.directory / 'state.json').read_bytes(), snapshot)
        self.assertEqual(len(backend.calls), calls)
