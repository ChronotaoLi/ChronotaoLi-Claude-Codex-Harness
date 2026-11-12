"""A commander question binds until the operator answers it; the answer reaches the next decide."""
import copy
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, decided, finding, revised, trigger_kind


class CommanderAskDecisionTests(RepoCase):
    def saved(self, engine):
        return read_json(engine.directory / 'state.json')

    @staticmethod
    def budgets(state):
        return {key: copy.deepcopy(state[key]) for key in ('attempts', 'agent_dispatches', 'final_history')}

    def answer(self, request, decision='continue'):
        path = self.repo / 'choice.json'
        write_json(path, dict(schema_version=1, request_id=request['id'], task_id=request['task_id'],
                             decision=decision, summary=f'Explicitly {decision} this original request'))
        return path

    def asking_backend(self, target='greet', *, reason='Choose whether to continue', repeat_ask=False):
        def change(backend, kwargs, packet, result):
            if target == 'greet' and kwargs['role'] == 'worker' and sum(c['role'] == 'worker' for c in backend.calls) == 1:
                result.update(status='blocked', blocker_kind='task', blockers=['Need a revised approach'])
            elif target == 'final' and kwargs['kind'] == 'final_review' and sum(c['kind'] == 'final_review' for c in backend.calls) == 1:
                result.update(verdict='request_changes', findings=[finding()])
            elif trigger_kind(kwargs, packet) in ('task', 'final') and (
                    packet['trigger'].get('feedback') or packet['trigger']['kind'] == 'final'):
                if repeat_ask or 'operator_answer' not in packet:
                    decided(result, 'ask', reason=reason)
                elif target == 'greet':
                    revised(result, packet, 'Use the clarified approach',
                            updates=[dict(id='greet', description='Implement using the clarified approach', add_depends_on=[])])
                else:
                    task = copy.deepcopy(packet['original_tasks'][0])
                    task.update(id='correct', depends_on=['greet'])
                    revised(result, packet, 'Resolve final finding', new_tasks=[task])
            elif kwargs['role'] == 'worker' and kwargs['task']['id'] == 'correct':
                with (kwargs['cwd'] / 'hello.py').open('a') as stream:
                    stream.write('# independently reviewed correction\n')
        return ScriptedBackend(change)

    def asked(self, target='greet', **kwargs):
        backend = self.asking_backend(target, **kwargs)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, f'等待操作者决定：task_id={target}'):
            engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        request = self.saved(engine)['decision_requests'][-1]
        self.assertEqual((request['kind'], request['task_id']), ('commander', target))
        return engine, backend

    def test_plain_resume_keeps_question_and_explicit_answer_reaches_the_next_decide(self):
        for target in ('greet', 'final'):
            with self.subTest(target=target):
                self.setUp()
                engine, backend = self.asked(target)
                before = self.saved(engine)
                calls = len(backend.calls)
                for unused in range(2):
                    with self.assertRaisesRegex(HarnessError, '等待操作者决定'):
                        self.engine(backend).resume(engine.run['id'])
                    self.assertEqual(len(backend.calls), calls)
                    self.assertEqual(self.budgets(self.saved(engine)), self.budgets(before))
                request = before['decision_requests'][-1]
                result = self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(request))
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(backend.calls[calls]['kind'], 'decide')
                self.assertEqual(backend.calls[calls]['packet']['operator_answer']['request_id'], request['id'])
                self.assertEqual(backend.calls[calls]['packet']['trigger']['kind'], 'task' if target == 'greet' else 'final')
                self.assertEqual(result['operator_decisions'][0]['request_id'], request['id'])
                if target == 'greet':
                    self.assertEqual(result['attempts'], {'greet': 2})

    def test_stop_answer_requires_explicit_continue(self):
        engine, backend = self.asked()
        request = self.saved(engine)['decision_requests'][0]
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, '操作者决定停止'):
            self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(request, 'stop'))
        before = self.saved(engine)
        with self.assertRaisesRegex(HarnessError, f"操作者已决定停止：request_id={request['id']}"):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(self.budgets(self.saved(engine)), self.budgets(before))
        self.assertEqual(len(backend.calls), calls)
        result = self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(request))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([r['decision'] for r in result['operator_decisions']], ['stop', 'continue'])

    def test_interrupted_durable_answer_resumes_without_reanswering(self):
        engine, backend = self.asked()
        before = self.saved(engine)
        resumed = self.engine(backend)
        record = resumed.services.decisions.record_operator_decisions

        def record_then_interrupt(records):
            record(records)
            raise KeyboardInterrupt()
        with patch.object(resumed.services.decisions, 'record_operator_decisions', side_effect=record_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                resumed.resume(engine.run['id'], operator_decision=self.answer(before['decision_requests'][0]))
        interrupted = self.saved(engine)
        self.assertEqual(len(interrupted['operator_decisions']), 1)
        self.assertEqual(self.budgets(interrupted), self.budgets(before))
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['operator_decisions']), 1)

    def test_answer_saved_before_the_decide_reaches_it_after_interruption(self):
        engine, backend = self.asked()
        before = self.saved(engine)
        resumed = self.engine(backend)
        with patch.object(resumed.services.scheduler, 'execute', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                resumed.resume(engine.run['id'], operator_decision=self.answer(before['decision_requests'][0]))
        interrupted = self.saved(engine)
        self.assertEqual(len(interrupted['operator_decisions']), 1)
        self.assertEqual(self.budgets(interrupted), self.budgets(before))
        calls = len(backend.calls)
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(backend.calls[calls]['kind'], 'decide')
        self.assertEqual(backend.calls[calls]['packet']['operator_answer']['request_id'],
                         before['decision_requests'][0]['id'])

    def test_old_continue_does_not_answer_identical_new_question(self):
        engine, backend = self.asked(repeat_ask=True)
        old_request = self.saved(engine)['decision_requests'][0]
        with self.assertRaisesRegex(HarnessError, '等待操作者决定'):
            self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(old_request))
        state = self.saved(engine)
        current = state['decision_requests'][-1]
        self.assertNotEqual(current['id'], old_request['id'])
        self.assertEqual(current['question'], old_request['question'])
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, '等待操作者决定'):
            self.engine(backend).resume(engine.run['id'])
        # The old answer is stale once a later decide was invoked.
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], operator_decision=self.answer(old_request))
        self.assertEqual(len(backend.calls), calls)
