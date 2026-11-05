import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from harness.review_progress import required_findings, claim_gaps, disposition_gaps
from harness.run_store import RunStore
from harness.task_lifecycle import TaskLifecycle
from harness.util import HarnessError, read_json
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT
from test_runtime_changes import finding


class FindingIdentityTests(unittest.TestCase):
    def test_duplicate_ids_retain_two_independent_obligations(self):
        with TemporaryDirectory() as tmp:
            run = {'id': 'run_identity', 'dispatches': [], 'review_history': {'t': []}}

            def record(ident, review, **extra):
                path = Path(tmp).resolve() / 'runs/run_identity/dispatches' / ident / 'returned-result.json'
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(dict(task_id='t', dispatch_id=ident, **review)))
                source = dict(dispatch_id=ident, artifact=str(path),
                              artifact_digest=hashlib.sha256(path.read_bytes()).hexdigest(),
                              snapshot_digest='candidate')
                row = dict(review=review, snapshot_digest='candidate', review_source=source,
                           artifact='wrapper', **extra)
                run['dispatches'].append(dict(id=ident, role='reviewer', kind='review',
                                              task_id='t', snapshot_digest='candidate'))
                run['review_history']['t'].append(row)
                return row

            original = record('first', dict(verdict='request_changes', findings=[finding(), finding()]))
            required = required_findings(run, 't', 'candidate')
            self.assertEqual([r['finding_id'] for r in required], ['finding-1', 'finding-2'])
            self.assertEqual([r['finding_index'] for r in required], [0, 1])
            self.assertEqual([r['original_finding_id'] for r in required], ['edge-case'] * 2)
            self.assertTrue(all(r['artifact'] == original['review_source']['artifact'] for r in required))
            disposition = dict(review_dispatch_id='first', finding_id='finding-1', status='resolved', evidence=['checked'])
            approval = dict(verdict='approve', findings=[], finding_dispositions=[disposition])
            self.assertTrue(disposition_gaps(approval, required))
            self.assertTrue(claim_gaps({'repair_claims': [dict(review_dispatch_id='first', finding_id='finding-1', evidence_paths=[])]}, required, {'files': {}}))
            # Even a source-authenticated rejection cannot close an older issue
            # using dispositions that failed validation.
            invalid = dict(verdict='request_changes', findings=[], finding_dispositions=[{**disposition, 'evidence': []}])
            record('second', invalid, status='invalid_rejected', validation_error='invalid evidence')
            self.assertEqual(len(required_findings(run, 't', 'candidate')), 2)
            record('third', approval)
            self.assertEqual([r['finding_id'] for r in required_findings(run, 't', 'candidate')], ['finding-2'])
            self.assertEqual(original['review']['findings'], [finding(), finding()])

    def test_stale_post_accept_cannot_write_either_memory_stage(self):
        for task_memory_saved in (False, True):
            with self.subTest(task_memory_saved=task_memory_saved), TemporaryDirectory() as tmp:
                completion = dict(id='acceptance', task_memory={}, knowledge={'status': 'pending'},
                                  proposals=[], approved_keys=[], knowledge_ttl_days=1)
                if task_memory_saved:
                    completion['task_memory_id'] = 1
                first = RunStore(tmp)
                first.attach(dict(id='run_guard', pending_post_accept=dict(
                    payload=dict(task_id='t', commit='commit'), completion=completion)))
                first.commit()
                stale = RunStore(tmp)
                stale.attach(json.loads(first._path.read_text()))
                memory = SimpleNamespace(remember=Mock(), remember_proposals=Mock())
                context = SimpleNamespace(run=stale.run, store=stale, memory=memory, save=stale.commit)
                first.run['status'] = 'abandoned'
                first.commit()
                with self.assertRaises(HarnessError):
                    TaskLifecycle(context).finish_post_accept()
                memory.remember.assert_not_called()
                memory.remember_proposals.assert_not_called()


class InvalidFindingLifecycleTests(RepoCase):
    def test_invalid_original_id_can_be_repaired_without_changing_raw_review(self):
        def change(backend, kwargs, packet, result):
            if kwargs['kind'] == 'review' and sum(c['kind'] == 'review' for c in backend.calls) == 1:
                result.update(verdict='request_changes', findings=[{
                    **finding(), 'id': 'bad/id', 'message': 'x' * 4001}])

        backend = RecordingBackend(change)
        result = self.engine(backend).start('Implement greet', read_json(ROOT / 'examples/plan.json'))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts'], {'greet': 2})
        raw = read_json(Path(result['review_history']['greet'][0]['review_source']['artifact']))
        self.assertEqual(raw['findings'][0]['id'], 'bad/id')
        self.assertEqual(len(raw['findings'][0]['message']), 4001)
        worker = [c for c in backend.calls if c['role'] == 'worker'][1]
        self.assertEqual(worker['packet']['required_findings'][0]['finding_id'], 'finding-1')
        self.assertEqual(worker['packet']['required_findings'][0]['original_finding_id'], 'bad/id')


class FinalFindingLifecycleTests(RepoCase):
    def backend(self, *, omit_dispositions=False, disposition_status='resolved'):
        import copy

        def change(backend, kwargs, packet, result):
            number = sum(c['kind'] == 'final_review' for c in backend.calls)
            if kwargs['kind'] == 'final_review' and number == 1:
                result.update(verdict='request_changes', findings=[finding()])
            elif kwargs['kind'] == 'replan':
                task = copy.deepcopy(packet['original_tasks'][0])
                task.update(id='correct', depends_on=['greet'])
                result.update(action='revise', reason='Resolve final blocking finding', new_tasks=[task], updates=[])
            elif kwargs['role'] == 'worker' and kwargs['task']['id'] == 'correct':
                with (kwargs['cwd'] / 'hello.py').open('a') as stream:
                    stream.write('# Independently reviewed final correction\n')
            elif kwargs['kind'] == 'final_review' and number == 2:
                if omit_dispositions:
                    result.pop('finding_dispositions', None)
                else:
                    for row in result.get('finding_dispositions', []):
                        row['status'] = disposition_status

        return RecordingBackend(change)

    def test_missing_final_disposition_cannot_complete_after_corrective_task(self):
        self.config.data['runtime']['max_replans'] = 1
        backend = self.backend(omit_dispositions=True)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
        self.assertNotEqual(engine.run['status'], 'completed')
        self.assertIn('correct', engine.run['accepted'])
        second = engine.run['final_history'][1]
        self.assertEqual(second['status'], 'rejected')
        self.assertEqual(second['review']['verdict'], 'approve')
        self.assertEqual(second['disposition_errors'][0]['code'], 'finding_not_independently_resolved')
        packet = [c['packet'] for c in backend.calls if c['kind'] == 'final_review'][1]
        self.assertEqual(packet['required_findings'][0]['review_dispatch_id'],
                         engine.run['final_history'][0]['review_source']['dispatch_id'])

    def test_open_final_disposition_cannot_complete(self):
        self.config.data['runtime']['max_replans'] = 1
        engine = self.engine(self.backend(disposition_status='open'))
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
        self.assertNotEqual(engine.run['status'], 'completed')
        self.assertEqual(engine.run['final_history'][1]['status'], 'rejected')
        self.assertTrue(engine.run['final_history'][1]['disposition_errors'])

    def test_resolved_final_disposition_completes_with_original_evidence_preserved(self):
        backend = self.backend()
        engine = self.engine(backend)
        result = engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([r['status'] for r in result['final_history']], ['rejected', 'approved'])
        original = result['final_history'][0]
        raw = Path(original['review_source']['artifact']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), original['review_source']['artifact_digest'])
        self.assertEqual(json.loads(raw)['findings'], [finding()])
        disposition = result['final_history'][1]['review']['finding_dispositions'][0]
        self.assertEqual(disposition['review_dispatch_id'], original['review_source']['dispatch_id'])
        self.assertEqual(disposition['finding_id'], finding()['id'])
        # Replaying approval authenticates all previous source records too.
        Path(original['review_source']['artifact']).write_bytes(raw + b' ')
        with self.assertRaises(HarnessError):
            engine.services.finalization.final_review()
