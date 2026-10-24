"""Prospective final capacity must not hard-reject temporary task evidence."""
import copy
import hashlib
import io
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class DecisionCapacityTests(RepoCase):
    def answer(self, engine):
        request = engine.run['decision_requests'][0]
        path = self.repo / 'answer.json'
        write_json(path, {'schema_version': 1, 'request_id': request['id'], 'task_id': request['task_id'],
                          'decision': 'continue', 'summary': 's' * 4000})
        return path

    def test_decisions_warn_in_projection_and_trigger_resume_preflight(self):
        def block_first(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker' and sum(c['role'] == 'worker' for c in backend.calls) == 1:
                result.update(status='blocked', blocker_kind='operator', blockers=['Choose ' + 'q' * 900])
        backend = ScriptedBackend(block_first)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        before = engine.final_context_capacity()
        path = self.answer(engine)
        engine.record_operator_decision(engine.prepare_operator_decision(path))
        after = engine.final_context_capacity()
        self.assertEqual(after['required_chars'], before['required_chars'])
        self.assertGreater(after['projected_chars'], before['projected_chars'])
        original_budget = self.config.data['runtime']['context_chars']
        self.config.data['runtime']['context_chars'] = after['required_chars'] + 1
        output = io.StringIO()
        with redirect_stderr(output):
            engine.preflight_final_context()
        self.assertEqual(engine.run['context_capacity']['status'], 'projected_overflow')
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(engine.run['accepted'], {})
        self.config.data['runtime']['context_chars'] = original_budget
        resumed = self.engine(backend)
        counts = []
        preflight = resumed.preflight_final_context
        def observe(*args, **kwargs):
            counts.append(len(backend.calls))
            return preflight(*args, **kwargs)
        with patch.object(resumed, 'preflight_final_context', side_effect=observe):
            result = resumed.resume(engine.run['id'], operator_decision=path)
        self.assertEqual(counts, [1])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts']['greet'], 2)

    def test_pending_checkpoint_projection_is_not_a_final_lower_bound(self):
        plan = read_json(ROOT / 'examples' / 'plan.json')
        other = copy.deepcopy(plan['tasks'][0])
        other.update(id='other', title='Independent work')
        plan['tasks'].append(other)
        evidence = self.repo.resolve() / 'evidence.bin'
        evidence.write_bytes(b'raw output\n')
        seen = set()
        def change(backend, kwargs, packet, result):
            if kwargs['role'] != 'worker' or kwargs['task']['id'] in seen:
                return
            tid = kwargs['task']['id']
            seen.add(tid)
            if tid == 'greet':
                result.update(status='blocked', blocker_kind='operator', blockers=['Continue? ' + 'q' * 900])
            else:
                result.update(status='blocked', blocker_kind='environment', blockers=['Service unavailable'],
                    checkpoints=[{'id': f'check-{i}', 'summary': 'Command output saved for reuse',
                        'inputs': [{'path': 'hello.py', 'sha256': hashlib.sha256((kwargs['cwd'] / 'hello.py').read_bytes()).hexdigest()}],
                        'evidence': [{'path': str(evidence), 'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()}],
                        'freshness': 'content', 'max_age_seconds': 0} for i in range(14)])
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement both tasks', plan)
        pending = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, '环境受阻'):
            pending.resume(engine.run['id'], operator_decision=self.answer(engine))
        accepted = copy.deepcopy(pending.run['accepted'])
        self.assertEqual(set(accepted), {'greet'})
        report = pending.final_context_capacity()
        self.assertNotEqual(report['status'], 'required_overflow')
        projected = pending.progress_packet({}, kind='final_review', task_id='final')
        self.assertEqual(projected['task_checkpoints'], [])
        self.assertEqual(projected['checkpoint_index']['count'], 14)
        self.assertEqual(pending.run['accepted'], accepted)
        rows = read_json(Path(projected['checkpoint_index']['artifact']))['checkpoints']
        self.assertEqual(rows, pending.run['task_checkpoints'])
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        final = next(d for d in result['dispatches'] if d.get('kind') == 'final_review')
        used = read_json(engine.directory / 'dispatches' / final['id'] / 'context-report.json')['used_chars']
        self.assertLessEqual(report['required_chars'], used)
        self.assertLessEqual(used, 32000)
