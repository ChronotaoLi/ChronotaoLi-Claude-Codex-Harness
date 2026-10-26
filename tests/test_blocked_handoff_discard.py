"""Simulated lifecycle checks for inapplicable blocked-worker handoffs."""
import hashlib
import json

from harness.util import HarnessError, read_json, write_json
from test_checkpoint_lifecycle import digest
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class BlockedHandoffDiscardTests(RepoCase):
    def blocked(self, kind, requests):
        evidence = self.repo.resolve() / 'checkpoint-evidence.bin'
        evidence.write_bytes(b'completed local check')

        def first_block(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker' and sum(c['role'] == 'worker' for c in backend.calls) == 1:
                result.update(status='blocked', blocker_kind=kind, blockers=['Service unavailable' if kind == 'environment' else 'Continue?'],
                              decision_requests=requests,
                              checkpoints=[{'id': 'check', 'summary': 'Local check completed',
                                  'inputs': [{'path': 'hello.py', 'sha256': digest(kwargs['cwd'] / 'hello.py')}],
                                  'evidence': [{'path': str(evidence), 'sha256': digest(evidence)}],
                                  'freshness': 'content', 'max_age_seconds': 0}])
        backend = ScriptedBackend(first_block)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        return engine, backend

    def assert_preserved(self, engine, requests):
        run = read_json(engine.directory / 'state.json')
        worker = next(d for d in run['dispatches'] if d['role'] == 'worker')
        artifact = engine.directory / 'dispatches' / worker['id'] / 'returned-result.json'
        raw = artifact.read_bytes()
        source_digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(json.loads(raw)['decision_requests'], requests)
        self.assertEqual(run['feedback']['worker']['decision_requests'], requests)
        self.assertEqual(run['feedback']['worker_artifact_digest'], source_digest)
        self.assertEqual(worker['attempt_exemption']['artifact_digest'], source_digest)
        self.assertEqual(engine.task_budget_used('greet'), 0)
        self.assertEqual(len(run['task_checkpoints']), 1)
        self.assertEqual(run['accepted'], {})
        self.assertFalse(any(d['role'] in ('verification', 'reviewer') for d in run['dispatches']))
        events = [json.loads(line) for line in (engine.directory / 'events.jsonl').read_text().splitlines()]
        discarded = [e for e in events if e.get('event') == 'decision_requests_discarded']
        self.assertEqual(len(discarded), 1)
        self.assertEqual(discarded[0]['worker_artifact_digest'], source_digest)
        engine.validate_attempt_accounting()
        return artifact, raw

    def test_environment_block_keeps_duplicate_invalid_target_requests_as_raw_evidence(self):
        requests = [{'id': 'same', 'task_id': 'unknown', 'question': 'Continue?'},
                    {'id': 'same', 'task_id': 'greet', 'question': 'Continue?'}]
        engine, backend = self.blocked('environment', requests)
        artifact, raw = self.assert_preserved(engine, requests)
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(artifact.read_bytes(), raw)
        self.assertEqual(len([c for c in backend.calls if c['role'] == 'worker']), 2)
        self.assertEqual(result['decision_requests'], [])

    def test_operator_block_preserves_gate_source_and_explicit_resume(self):
        requests = [{'id': 'same', 'task_id': 'unknown', 'question': 'Continue?'},
                    {'id': 'same', 'task_id': 'greet', 'question': 'Continue?'}]
        engine, backend = self.blocked('operator', requests)
        artifact, raw = self.assert_preserved(engine, requests)
        self.assertEqual(len(engine.run['decision_requests']), 1)
        calls = len(backend.calls)
        for _ in range(2):
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)
        answer = self.repo / 'answer.json'
        request = engine.run['decision_requests'][0]
        write_json(answer, {'schema_version': 1, 'request_id': request['id'], 'task_id': 'greet',
                            'decision': 'continue', 'summary': 'Proceed'})
        result = self.engine(backend).resume(engine.run['id'], operator_decision=answer)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(artifact.read_bytes(), raw)
        self.assertEqual(len(result['decision_requests']), 1)

    def test_tampered_blocked_source_stops_before_new_model_call(self):
        requests = [{'id': 'same', 'task_id': 'unknown', 'question': 'Continue?'}] * 2
        engine, backend = self.blocked('operator', requests)
        artifact, _ = self.assert_preserved(engine, requests)
        artifact.write_bytes(artifact.read_bytes() + b'\n')
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(engine.run['accepted'], {})

    def test_invalid_structure_or_format_still_rejects_without_exemption(self):
        for requests in ([{'id': 'same', 'task_id': 'greet', 'question': 'Continue?', 'extra': True}],
                         [{'id': 'bad id', 'task_id': 'greet', 'question': 'Continue?'}]):
            with self.subTest(requests=requests):
                engine, backend = self.blocked('environment', requests)
                self.assertFalse(engine.run.get('task_checkpoints'))
                self.assertFalse(any(d.get('attempt_exemption') for d in engine.run['dispatches']))
                self.assertEqual(len(backend.calls), 1)

    def test_completed_duplicate_requests_remain_strict(self):
        def duplicate(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker':
                result['decision_requests'] = [{'id': 'same', 'task_id': 'greet', 'question': 'Continue?'}] * 2
        backend = ScriptedBackend(duplicate)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, 'id 必须唯一'):
            engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        self.assertEqual(engine.run['accepted'], {})
        self.assertEqual(len(backend.calls), 1)
