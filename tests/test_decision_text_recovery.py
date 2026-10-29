"""Malformed questions retain explicit gates and immutable source evidence."""
import copy
import hashlib
from pathlib import Path
from unittest.mock import patch

from harness.decisions import decision_question
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class DecisionTextRecoveryTests(RepoCase):
    def plan(self, *, dependent=False, unrelated=False):
        plan = read_json(ROOT / 'examples' / 'plan.json')
        if dependent or unrelated:
            task = copy.deepcopy(plan['tasks'][0])
            task.update(id='later', depends_on=[] if unrelated else ['greet'])
            plan['tasks'].append(task)
        return plan

    def saved(self, engine):
        return read_json(engine.directory / 'state.json')

    def answer(self, engine, decision='continue'):
        request = self.saved(engine)['decision_requests'][0]
        path = self.repo / 'decision.json'
        write_json(path, {'schema_version': 1, 'request_id': request['id'], 'task_id': request['task_id'],
                          'decision': decision, 'summary': 'Read the original evidence; ' + decision})
        return path

    def backend(self, *, blockers=None, handoff=None):
        def first_worker(backend, kwargs, packet, result):
            if kwargs['role'] != 'worker' or sum(c['role'] == 'worker' for c in backend.calls) != 1:
                return
            if blockers is not None:
                result.update(status='blocked', blocker_kind='operator', blockers=blockers)
            if handoff is not None:
                result['decision_requests'] = [{'id': 'risk', 'task_id': 'later', 'question': handoff}]
        return ScriptedBackend(first_worker)

    def assert_plain_resume_gated(self, engine, backend):
        before = self.saved(engine)
        calls = len(backend.calls)
        for _ in range(2):
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'])
        after = self.saved(engine)
        self.assertEqual(len(backend.calls), calls)
        for key in ('attempts', 'total_attempts', 'agent_dispatches', 'accepted', 'decision_requests'):
            self.assertEqual(after[key], before[key])

    def test_malformed_operator_questions_wait_for_explicit_answer(self):
        for blockers in (['Proceed\0?'], [''], ['  '], ['x' * 80020]):
            with self.subTest(blockers_length=sum(map(len, blockers))):
                backend = self.backend(blockers=blockers)
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start('Implement greet', self.plan())
                state = self.saved(engine)
                self.assertEqual(state['accepted'], {})
                self.assertEqual(len(state['decision_requests']), 1)
                request = state['decision_requests'][0]
                self.assertNotIn('\0', request['question'])
                self.assertTrue(request['question'].strip())
                raw = Path(request['source']['artifact']).read_bytes()
                self.assertEqual(read_json(Path(request['source']['artifact']))['blockers'], blockers)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), request['source']['artifact_digest'])
                self.assertFalse(any(d['role'] == 'verification' for d in state['dispatches']))
                self.assert_plain_resume_gated(engine, backend)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(engine, 'stop'))
                self.assertEqual(len(backend.calls), 1)
                completed = self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(engine))
                self.assertEqual(completed['status'], 'completed')
                self.assertEqual(completed['attempts'], {'greet': 2})
                workers = [d for d in completed['dispatches'] if d['role'] == 'worker']
                self.assertEqual(len({d['id'] for d in workers}), 2)
                self.assertEqual(Path(request['source']['artifact']).read_bytes(), raw)
                self.assertTrue(any(d['role'] == 'verification' for d in completed['dispatches']))
                self.assertTrue(any(c['kind'] == 'review' for c in backend.calls))

    def test_invalid_handoff_text_and_target_retain_recovery_before_acceptance(self):
        for question, unrelated in (('Proceed\0?', False), ('Proceed?', True)):
            with self.subTest(unrelated=unrelated):
                backend = self.backend(handoff=question)
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start('Implement source and later work', self.plan(dependent=True, unrelated=unrelated))
                state = self.saved(engine)
                self.assertEqual(state['accepted'], {})
                self.assertEqual(state['decision_requests'], [])
                self.assertNotIn('pending_task', state)
                recovery = state['worker_recovery']['greet']
                self.assertTrue(recovery['error'])
                self.assertEqual(recovery['dispatch_id'], state['dispatches'][-1]['id'])
                self.assertEqual(state['dispatches'][-1]['status'], 'failed')
                self.assertFalse(any(d['role'] == 'verification' for d in state['dispatches']))
                calls = len(backend.calls)
                original_change = backend.change

                def inspect_recovery(backend, kwargs, packet, returned):
                    original_change(backend, kwargs, packet, returned)
                    if packet.get('recovery'):
                        self.assertEqual(self.saved(engine)['worker_recovery']['greet'], recovery)

                backend.change = inspect_recovery
                result = self.engine(backend).resume(engine.run['id'])
                self.assertEqual(result['status'], 'completed')
                resumed = backend.calls[calls:]
                for role in ('commander', 'worker'):
                    call = next(c for c in resumed if c['role'] == role)
                    projected = call['packet']['recovery']
                    self.assertEqual({key: projected[key] for key in recovery}, recovery)

    def test_legacy_accepted_handoff_registers_gate_without_reaccepting(self):
        backend = self.backend(handoff='Proceed\0?')
        engine = self.engine(backend)
        # Reproduce the old late-registration failure without editing saved
        # evidence, accepted records, or their bound digests.
        with patch('harness.engine.validate_handoff_question', return_value=None), \
                patch('harness.decisions.decision_question', side_effect=lambda q, kind: q):
            with self.assertRaises(HarnessError):
                engine.start('Implement source and later work', self.plan(dependent=True))
        before = self.saved(engine)
        self.assertEqual(set(before['accepted']), {'greet'})
        self.assertEqual(before['decision_requests'], [])
        source = before['accepted']['greet']['worker_source']
        raw = Path(source['artifact']).read_bytes()
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        after = self.saved(engine)
        self.assertEqual(after['accepted'], before['accepted'])
        self.assertEqual(len(after['decision_requests']), 1)
        self.assertEqual(after['decision_requests'][0]['source'], source)
        self.assertEqual(len(backend.calls), calls)
        self.assert_plain_resume_gated(engine, backend)
        result = self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(engine))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['accepted']['greet'], before['accepted']['greet'])
        self.assertEqual(result['attempts'], {'greet': 1, 'later': 1})
        self.assertEqual(Path(source['artifact']).read_bytes(), raw)
        self.assertEqual(result['schema_version'], 11)

    def test_legacy_operator_registration_failure_recovers_to_explicit_gate(self):
        for blockers in (['Proceed\0?'], ['']):
            with self.subTest(blockers=blockers):
                backend = self.backend(blockers=blockers)
                engine = self.engine(backend)
                with patch('harness.decisions.decision_question', side_effect=lambda q, kind: q):
                    with self.assertRaises(HarnessError):
                        engine.start('Implement greet', self.plan())
                self.assertEqual(self.saved(engine)['decision_requests'], [])
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run['id'])
                self.assertEqual(len(self.saved(engine)['decision_requests']), 1)
                self.assertEqual(len(backend.calls), 1)
                self.assert_plain_resume_gated(engine, backend)
                result = self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(engine))
                self.assertEqual(result['status'], 'completed')

    def test_legacy_pending_source_finishes_saved_gates_before_requesting_decision(self):
        for method, stage in (('continue_task', 'pending_task'), ('accept_pending', 'pending_acceptance')):
            with self.subTest(stage=stage):
                backend = self.backend(handoff='Proceed\0?')
                engine = self.engine(backend)
                with patch('harness.engine.validate_handoff_question', return_value=None), \
                        patch.object(engine, method, side_effect=HarnessError('saved old lifecycle stage')):
                    with self.assertRaises(HarnessError):
                        engine.start('Implement source and later work', self.plan(dependent=True))
                before = self.saved(engine)
                self.assertIn(stage, before)
                self.assertEqual(before['accepted'], {})
                worker = next(d for d in before['dispatches'] if d['role'] == 'worker')
                artifact = engine.directory / 'dispatches' / worker['id'] / 'returned-result.json'
                raw = artifact.read_bytes()
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run['id'])
                state = self.saved(engine)
                self.assertEqual(set(state['accepted']), {'greet'})
                self.assertEqual(state['attempts'], {'greet': 1})
                self.assertEqual(len(state['decision_requests']), 1)
                self.assertEqual([c['task_id'] for c in backend.calls if c['role'] == 'worker'], ['greet'])
                self.assertEqual(sum(c['kind'] == 'review' for c in backend.calls), 1)
                self.assertTrue(any(d['role'] == 'verification' for d in state['dispatches']))
                self.assertEqual(artifact.read_bytes(), raw)
                self.assert_plain_resume_gated(engine, backend)
                result = self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(engine))
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(result['attempts'], {'greet': 1, 'later': 1})

    def test_projected_question_and_raw_source_both_remain_bound(self):
        backend = self.backend(blockers=['Proceed\0?'])
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', self.plan())
        request = self.saved(engine)['decision_requests'][0]
        with self.assertRaisesRegex(HarnessError, 'does not match operator blocker'):
            engine._decision_source({**request, 'question': 'A different decision'})
        before = (engine.directory / 'state.json').read_bytes()
        artifact = Path(request['source']['artifact'])
        artifact.write_bytes(artifact.read_bytes() + b'\n')
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(engine))
        self.assertEqual((engine.directory / 'state.json').read_bytes(), before)
        self.assertEqual(len(backend.calls), 1)

    def test_escaped_notice_fits_handoff_limit_and_valid_identity_is_unchanged(self):
        question = '\U0001f600' * 500 + '\0'
        projected = decision_question(question, 'handoff')
        self.assertLessEqual(len(projected), 4000)
        self.assertNotIn('\0', projected)
        self.assertEqual(decision_question('Proceed?', 'worker'), 'Proceed?')
        self.assertEqual(decision_question('Proceed?', 'handoff'), 'Proceed?')

    def test_commander_stop_with_nul_retains_explicit_retry_exit(self):
        def stop(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker':
                result.update(status='blocked', blocker_kind='task', blockers=['Need a prerequisite'])
            elif kwargs['kind'] == 'replan':
                result.update(action='stop', reason='Need operator choice\0', new_tasks=[], updates=[])
        backend = ScriptedBackend(stop)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', self.plan())
        self.assertTrue(self.saved(engine)['replan_stopped'])
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, '--retry-stopped greet'):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)
