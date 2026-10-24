"""Offline lifecycle checks; simulated workers do not prove live model calls."""
import hashlib
from pathlib import Path

from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CheckpointLifecycleTests(RepoCase):
    def blocked(self, *, freshness='content', max_age_seconds=0):
        # Explicit test staging lives outside the task worktree; canonical paths
        # avoid macOS /var and /tmp aliases under conservative symlink checks.
        evidence = self.repo.resolve() / 'authorized-check-output.bin'
        evidence.write_bytes(b'complete raw check output\x00\xff\n')

        def block_first(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker' and sum(c['role'] == 'worker' for c in backend.calls) == 1:
                result.update(status='blocked', blocker_kind='environment', blockers=['Service unavailable'],
                              checkpoints=[{'id': 'local-computation', 'summary': 'Local computation finished; service work remains',
                                  'inputs': [{'path': 'hello.py', 'sha256': digest(kwargs['cwd'] / 'hello.py')}],
                                  'evidence': [{'path': str(evidence), 'sha256': digest(evidence)}],
                                  'freshness': freshness, 'max_age_seconds': max_age_seconds}])
        backend = ScriptedBackend(block_first)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, '环境受阻'):
            engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        self.assertEqual(engine.run['accepted'], {})
        self.assertFalse(any(d['role'] in ('verification', 'reviewer') for d in engine.run['dispatches']))
        self.assertEqual(len(engine.run['task_checkpoints']), 1)
        return engine, backend, evidence

    def test_fresh_worker_reuses_raw_copy_but_still_verifies_and_reviews(self):
        engine, backend, evidence = self.blocked()
        original_bytes = evidence.read_bytes()
        evidence.unlink()
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        workers = [call for call in backend.calls if call['role'] == 'worker']
        self.assertEqual(len(workers), 2)
        checkpoint = workers[1]['packet']['task_checkpoints'][0]
        self.assertEqual(checkpoint['status'], 'reusable')
        self.assertEqual(Path(checkpoint['evidence'][0]['artifact']).read_bytes(), original_bytes)
        self.assertEqual(checkpoint['evidence'][0]['path'], str(evidence))
        self.assertEqual(checkpoint['evidence'][0]['artifact_digest'], hashlib.sha256(original_bytes).hexdigest())
        self.assertTrue(Path(checkpoint['source']['artifact']).is_file())
        dispatches = [d for d in result['dispatches'] if d['role'] == 'worker']
        self.assertEqual(len({d['id'] for d in dispatches}), 2)
        self.assertTrue(any(d['role'] == 'verification' and d['task_id'] == 'greet' for d in result['dispatches']))
        self.assertTrue(any(c['role'] == 'reviewer' and c['kind'] == 'review' for c in backend.calls))
        self.assertEqual(set(result['accepted']), {'greet'})
        reviewer = next(c for c in backend.calls if c['kind'] == 'review')
        self.assertEqual(reviewer['packet']['task_checkpoints'][0]['status'], 'reusable')

    def test_changed_input_is_stale_before_new_worker_without_blocking_it(self):
        engine, backend, _ = self.blocked()
        with (engine.worktree / 'hello.py').open('a') as stream:
            stream.write('# input changed after prior computation\n')
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        worker = [c for c in backend.calls if c['role'] == 'worker'][1]
        checkpoint = worker['packet']['task_checkpoints'][0]
        self.assertEqual(checkpoint['status'], 'stale')
        self.assertNotEqual(checkpoint['inputs'][0]['sha256'], checkpoint['inputs'][0]['current_sha256'])
        self.assertTrue(checkpoint['reasons'])

    def test_zero_ttl_is_stale_on_next_dispatch(self):
        engine, backend, _ = self.blocked(freshness='volatile')
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        worker = [c for c in backend.calls if c['role'] == 'worker'][1]
        self.assertEqual(worker['packet']['task_checkpoints'][0]['status'], 'stale')
        self.assertIn('volatile evidence expired', worker['packet']['task_checkpoints'][0]['reasons'])

    def test_tampered_runtime_copy_blocks_before_any_new_dispatch(self):
        engine, backend, _ = self.blocked()
        manifest = read_json(Path(engine.run['task_checkpoints'][0]['artifact']))
        Path(manifest['evidence'][0]['artifact']).write_bytes(b'tampered output')
        calls = len(backend.calls)
        saved = (engine.directory / 'state.json').read_bytes()
        with self.assertRaisesRegex(HarnessError, 'cached evidence changed'):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual((engine.directory / 'state.json').read_bytes(), saved)

    def test_reusable_checkpoint_cannot_accept_failed_verification(self):
        engine, backend, _ = self.blocked()
        previous_change = backend.change

        def break_implementation(backend, kwargs, packet, result):
            previous_change(backend, kwargs, packet, result)
            if kwargs['role'] == 'worker':
                (kwargs['cwd'] / 'hello.py').write_text('def greet(name):\n    return "wrong"\n')
        backend.change = break_implementation
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        saved = read_json(engine.directory / 'state.json')
        self.assertEqual(saved['accepted'], {})
        self.assertTrue(any(d['role'] == 'verification' for d in saved['dispatches']))
        self.assertFalse(any(c['role'] == 'reviewer' for c in backend.calls))
        self.assertEqual([c for c in backend.calls if c['role'] == 'worker'][1]['packet']['task_checkpoints'][0]['status'], 'reusable')
