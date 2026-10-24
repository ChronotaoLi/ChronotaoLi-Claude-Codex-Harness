"""Decision context capacity and immutable evidence through offline Engine calls."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_operator_decisions import Fixture
from test_runtime_changes import ScriptedBackend


class DecisionContextTests(RepoCase):
    def exercise(self, count, answer_length):
        def block_first(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker' and not packet.get('operator_decisions'):
                result.update(status='blocked', blocker_kind='operator', blockers=['Choose a safe option. ' * (45 if answer_length == 4000 else 1)])
        backend = ScriptedBackend(block_first)
        plan = read_json(ROOT / 'examples' / 'plan.json')
        for i in range(1, count):
            task = copy.deepcopy(plan['tasks'][0])
            task.update(id=f'other{i}', title=f'Independent task {i}')
            plan['tasks'].append(task)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement tasks', plan)
        originals = {}
        for i in range(count):
            saved = read_json(engine.directory / 'state.json')
            request = saved['decision_requests'][-1]
            for row in saved['decision_requests'] + saved['operator_decisions']:
                originals[row['artifact']] = Path(row['artifact']).read_bytes()
            path = self.repo / 'answer.json'
            write_json(path, {'schema_version': 1, 'request_id': request['id'], 'task_id': request['task_id'],
                              'decision': 'continue', 'summary': 'A' * answer_length})
            resumed = self.engine(backend)
            if i < count - 1:
                with self.assertRaises(HarnessError):
                    resumed.resume(engine.run['id'], operator_decision=path)
            else:
                result = resumed.resume(engine.run['id'], operator_decision=path)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['accepted']), count)
        self.assertEqual(len(result['operator_decisions']), count)
        self.assertTrue(all(r['summary'] == 'A' * answer_length for r in result['operator_decisions']))
        final = next(c['packet'] for c in backend.calls if c['kind'] == 'final_review')
        self.assertEqual({r['id'] for r in final['operator_decisions']}, {r['id'] for r in result['decision_requests']})
        self.assertTrue(all(r['response']['decision'] == 'continue' for r in final['operator_decisions']))
        index = final['operator_decision_index']
        raw = Path(index['artifact']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), index['artifact_digest'])
        archive = read_json(Path(index['artifact']))
        self.assertEqual(archive['responses'], result['operator_decisions'])
        self.assertEqual(archive['requests'], resumed.decision_context())
        for path, raw in originals.items():
            self.assertEqual(Path(path).read_bytes(), raw)
        reports = [read_json(p) for p in (engine.directory / 'dispatches').glob('*/context-report.json')]
        self.assertTrue(reports)
        self.assertTrue(all(r['used_chars'] <= 32000 for r in reports))

    def test_four_full_length_answers_fit_complete_engine_prompts(self):
        self.exercise(4, 4000)

    def test_eight_short_decisions_fit_complete_engine_prompts(self):
        self.exercise(8, 9)


class DecisionIndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.engine = Fixture(temp.name)
        self.request = self.engine.request()

    def test_old_index_retains_unanswered_and_superseded_answers(self):
        engine = self.engine
        before = engine.decision_packet({}, task_id='one')['operator_decision_index']
        raw = Path(before['artifact']).read_bytes()
        engine.record_operator_decision(engine.prepare_operator_decision(engine.response(self.request, 'stop')))
        stopped = engine.decision_packet({}, task_id='one')
        self.assertEqual(stopped['operator_decisions'][0]['response']['decision'], 'stop')
        engine.record_operator_decision(engine.prepare_operator_decision(engine.response(self.request)))
        packet = engine.decision_packet({}, task_id='one')
        index = read_json(Path(packet['operator_decision_index']['artifact']))
        self.assertEqual([r['decision'] for r in index['responses']], ['stop', 'continue'])
        self.assertEqual(index['requests'][0]['response']['decision'], 'continue')
        self.assertEqual(Path(before['artifact']).read_bytes(), raw)
        self.assertIsNone(read_json(Path(before['artifact']))['requests'][0]['response'])

    def test_unselected_source_and_superseded_response_tamper_fail_closed(self):
        engine = self.engine
        engine.record_operator_decision(engine.prepare_operator_decision(engine.response(self.request, 'stop')))
        engine.record_operator_decision(engine.prepare_operator_decision(engine.response(self.request)))
        for path in (engine.source['artifact'], engine.run['operator_decisions'][0]['artifact']):
            original = Path(path).read_bytes()
            Path(path).write_text('{}')
            with self.assertRaises(HarnessError):
                engine.decision_packet({}, task_id='two')
            Path(path).write_bytes(original)
        self.assertEqual(engine.decision_packet({}, task_id='two')['operator_decisions'], [])

    def test_existing_index_tamper_is_not_overwritten(self):
        engine = self.engine
        ref = engine.decision_packet({}, task_id='one')['operator_decision_index']
        Path(ref['artifact']).write_text('{}')
        with self.assertRaisesRegex(HarnessError, 'decision index changed'):
            engine.decision_packet({}, task_id='one')
        self.assertEqual(Path(ref['artifact']).read_text(), '{}')

    def test_index_symlink_is_rejected_without_touching_target(self):
        engine = self.engine
        ref = engine.decision_packet({}, task_id='one')['operator_decision_index']
        artifact = Path(ref['artifact'])
        target = engine.directory / 'unrelated.json'
        target.write_bytes(artifact.read_bytes())
        artifact.unlink()
        artifact.symlink_to(target)
        original = target.read_bytes()
        with self.assertRaisesRegex(HarnessError, 'decision index changed'):
            engine.decision_packet({}, task_id='one')
        self.assertEqual(target.read_bytes(), original)

    def test_run_directory_symlink_cannot_redirect_index_writes(self):
        engine = self.engine
        alias = engine.directory / 'run-alias'
        alias.symlink_to(engine.directory, target_is_directory=True)
        engine.directory = alias
        with self.assertRaisesRegex(HarnessError, 'decision index directory is a symlink'):
            engine._decision_index([])
        self.assertFalse((alias / 'decisions' / 'indexes').exists())
