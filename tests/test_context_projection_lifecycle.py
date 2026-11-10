"""Offline dispatch preparation/recovery exercises; no provider integration."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from harness.context import ContextCapacityError
from harness.packets import PacketBuilder
from harness.util import HarnessError, read_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend


class ContextProjectionLifecycleTests(RepoCase):
    def test_replan_overflow_keeps_request_and_budgets_then_resumes_same_request(self):
        def change(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker' and sum(c['role'] == 'worker' for c in backend.calls) == 1:
                result.update(status='blocked', blocker_kind='task', blockers=['Clarify implementation'],
                              summary='Retain this candidate and refine its description')
            elif kwargs['kind'] == 'replan':
                result.update(action='revise', reason='Clarify implementation', new_tasks=[],
                    updates=[{'id': 'greet', 'description': 'Implement the same greeting with clarified detail', 'add_depends_on': []}])
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        original = PacketBuilder.role_input
        before = []
        requests = []
        def oversized(builder, **kwargs):
            contract, required = original(builder, **kwargs)
            if kwargs['kind'] == 'replan':
                required['packet']['overflow_fixture'] = 'x' * 110000
                before.append({key: deepcopy(builder.context.run.get(key)) for key in
                    ('attempts', 'agent_dispatches', 'replan_attempts', 'dispatches')})
                requests.append(deepcopy(builder.context.run['pending_replan']))
            return contract, required
        with patch.object(PacketBuilder, 'role_input', oversized):
            with self.assertRaises(ContextCapacityError):
                engine.start('Implement greeting', read_json(ROOT / 'examples/plan.json'))
            state = read_json(engine.directory / 'state.json')
            for key, value in before[-1].items():
                self.assertEqual(state.get(key), value, key)
            request = deepcopy(state['pending_replan'])
            report_path = Path(state['last_runtime_error']['context_report'])
            report = read_json(report_path)
            self.assertEqual(report['kind'], 'replan')
            self.assertEqual(report['request_id'], request['request_id'])
            self.assertGreater(report['required_chars'], report['budget_chars'])
            self.assertFalse(any(c['kind'] == 'replan' for c in backend.calls))
            self.config.data['runtime']['context_chars'] = 200000
            result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(requests[0]['request_id'], requests[1]['request_id'])
        self.assertEqual(sum(c['kind'] == 'replan' for c in backend.calls), 1)
        self.assertEqual(sum(result['attempts'].values()), 2)

    def test_saved_command_index_tamper_blocks_recovery_before_any_model_call(self):
        self.config.data['permissions']['worker_commands'] = [['python3', '-c', '#' + 'x' * 9000]]
        plan = read_json(ROOT / 'examples/plan.json')
        second = deepcopy(plan['tasks'][0])
        second.update(id='second', title='Second greeting', depends_on=['greet'])
        plan['tasks'].append(second)
        backend = ScriptedBackend()
        engine = self.engine(backend)
        result = engine.start('Implement greeting', plan, step=True)
        self.assertEqual(result['status'], 'paused')
        entry = next(row for row in result['dispatches'] if row.get('input_evidence'))
        ref = entry['input_evidence'][0]
        path = Path(ref['artifact'])
        original = path.read_bytes()
        catalog = read_json(path)
        self.assertEqual(catalog['records'][0]['argv'], self.config.data['permissions']['worker_commands'][0])
        path.write_text('{}')
        count = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, 'input index'):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), count)
        self.assertEqual(path.read_text(), '{}')
        path.write_bytes(original)
