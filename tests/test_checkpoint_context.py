"""Checkpoint capacity through complete Engine prompts, using offline model fixtures."""
import copy
import hashlib
from pathlib import Path

from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class CheckpointContextTests(RepoCase):
    def backend(self, count, blocked_attempts=0):
        evidence = self.repo.resolve() / 'external-output.bin'
        evidence.write_bytes(b'raw output\n')

        def checkpoint(backend, kwargs, packet, result):
            if kwargs['role'] != 'worker':
                return
            result['checkpoints'] = [
                {'id': f'check-{i}', 'summary': 'Command output saved for reuse',
                 'inputs': [{'path': 'hello.py', 'sha256': hashlib.sha256((kwargs['cwd'] / 'hello.py').read_bytes()).hexdigest()}],
                 'evidence': [{'path': str(evidence), 'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()}],
                 'freshness': 'content', 'max_age_seconds': 0} for i in range(count)]
            if sum(c['role'] == 'worker' for c in backend.calls) <= blocked_attempts:
                result.update(status='blocked', blocker_kind='environment', blockers=['Service unavailable'])

        return ScriptedBackend(checkpoint)

    def assert_prompts_fit(self, engine):
        reports = [read_json(path) for path in (engine.directory / 'dispatches').glob('*/context-report.json')]
        self.assertTrue(reports)
        self.assertTrue(all(r['used_chars'] <= 32000 for r in reports))

    def test_sixteen_checkpoints_across_three_attempts_fit_actual_prompts(self):
        backend = self.backend(16, blocked_attempts=2)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, '环境受阻'):
            engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        with self.assertRaisesRegex(HarnessError, '环境受阻'):
            self.engine(backend).resume(engine.run['id'])
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        workers = [c for c in backend.calls if c['role'] == 'worker']
        self.assertEqual(len(workers), 3)
        packet = workers[2]['packet']
        self.assertEqual(len(packet['task_checkpoints']), 16)
        self.assertTrue(all(c['status'] == 'reusable' for c in packet['task_checkpoints']))
        self.assertEqual(packet['checkpoint_history_omitted'], 16)
        self.assertEqual(packet['checkpoint_history'], [])
        index = packet['checkpoint_index']
        raw = Path(index['artifact']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), index['artifact_digest'])
        rows = read_json(Path(index['artifact']))['checkpoints']
        self.assertEqual(len(rows), 32)  # This old index remains immutable after attempt three.
        for row in rows:
            manifest = Path(row['artifact']).read_bytes()
            self.assertEqual(hashlib.sha256(manifest).hexdigest(), row['artifact_digest'])
        self.assertEqual(len(result['task_checkpoints']), 48)
        self.assert_prompts_fit(engine)

    def test_final_review_four_tasks_four_checkpoints_keeps_retrievable_archive(self):
        backend = self.backend(4)
        plan = read_json(ROOT / 'examples' / 'plan.json')
        for number in range(1, 4):
            task = copy.deepcopy(plan['tasks'][0])
            task.update(id=f'other{number}', title=f'Independent work {number}')
            plan['tasks'].append(task)
        engine = self.engine(backend)
        result = engine.start('Implement several tasks', plan)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['accepted']), 4)
        final = next(c['packet'] for c in backend.calls if c['kind'] == 'final_review')
        self.assertEqual(final['task_checkpoints'], [])
        self.assertEqual(final['checkpoint_history'], [])
        self.assertEqual(read_json(Path(final['checkpoint_index']['artifact']))['checkpoints'], result['task_checkpoints'])
        self.assertEqual(final['checkpoint_index']['count'], 16)
        self.assertEqual(result['final_attempts'], 1)
        self.assert_prompts_fit(engine)
