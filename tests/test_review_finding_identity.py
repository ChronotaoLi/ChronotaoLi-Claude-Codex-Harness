import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from harness.state_files import StateFiles
from harness.review_progress import repair_findings, required_findings, claim_gaps, disposition_gaps
from harness.run_store import RunStore
from harness.task_lifecycle import TaskLifecycle
from harness.util import EvidenceError, HarnessError, read_json
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT
from test_runtime_changes import finding, revised, trigger_kind


def labels(rows):
    return [row['label'] for row in rows]


def reported(*names):
    return dict(verdict='request_changes', findings=[{**finding(), 'id': name} for name in names])


class FindingIdentityTests(unittest.TestCase):
    def history(self, tmp):
        run = {'id': 'run_identity', 'dispatches': [], 'review_history': {'t': []}}

        def record(ident, review, snapshot='candidate', **extra):
            path = Path(tmp).resolve() / 'runs/run_identity/dispatches' / ident / 'returned-result.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(dict(task_id='t', dispatch_id=ident, **review)))
            source = dict(dispatch_id=ident, artifact=str(path),
                          artifact_digest=hashlib.sha256(path.read_bytes()).hexdigest(),
                          snapshot_digest=snapshot)
            row = dict(review=review, snapshot_digest=snapshot, review_source=source,
                       artifact='wrapper', **extra)
            run['dispatches'].append(dict(id=ident, role='reviewer', kind='review',
                                          task_id='t', snapshot_digest=snapshot))
            run['review_history']['t'].append(row)
            return row
        return run, record

    @staticmethod
    def dispositions(**statuses):
        return {key: {'status': status, 'evidence': ['checked'] if status == 'resolved' else []}
                for key, status in statuses.items()}

    def test_duplicate_ids_retain_two_independent_obligations(self):
        with TemporaryDirectory() as tmp:
            run, record = self.history(tmp)
            original = record('first', dict(verdict='request_changes', findings=[finding(), finding()]))
            required = required_findings(run, 't', 'candidate', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))
            self.assertEqual([r['key'] for r in required], ['f1', 'f2'])
            self.assertEqual([r['finding_index'] for r in required], [0, 1])
            self.assertEqual([r['label'] for r in required], ['edge-case'] * 2)
            self.assertTrue(all(r['artifact'] == original['review_source']['artifact'] for r in required))
            approval = dict(verdict='approve', findings=[], finding_dispositions=self.dispositions(f1='resolved'))
            self.assertEqual([gap['key'] for gap in disposition_gaps(approval, required)], ['f2'])
            self.assertEqual([gap['key'] for gap in claim_gaps({'status': 'completed'}, required, {'files': {}})],
                             ['f1', 'f2'])
            # Even a source-authenticated rejection cannot close an older issue
            # using dispositions that failed validation.
            invalid = dict(verdict='request_changes', findings=[],
                           finding_dispositions={'f1': {'status': 'resolved', 'evidence': []},
                                                 'f2': {'status': 'open', 'evidence': []}})
            record('second', invalid, status='invalid_rejected', validation_error='invalid evidence',
                   required_findings=required)
            self.assertEqual(len(required_findings(run, 't', 'candidate', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), 2)
            # A usable review resolving only the first leaves the second, by position.
            partial = dict(verdict='request_changes', findings=[],
                           finding_dispositions=self.dispositions(f1='resolved', f2='open'))
            record('third', partial, required_findings=required_findings(run, 't', 'candidate', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"])))
            remaining = required_findings(run, 't', 'candidate', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))
            self.assertEqual([(r['key'], r['finding_index'], r['label']) for r in remaining], [('f1', 1, 'edge-case')])
            self.assertEqual(original['review']['findings'], [finding(), finding()])

    def test_asymmetric_dispositions_close_only_their_bound_finding(self):
        advisory = {**finding('advisory', 'preference'), 'id': 'N'}
        for statuses, expected in (({'f1': 'resolved', 'f2': 'open'}, [(2, 'B')]),
                                   ({'f1': 'open', 'f2': 'resolved'}, [(0, 'A')])):
            with self.subTest(statuses=statuses), TemporaryDirectory() as tmp:
                run, record = self.history(tmp)
                record('R1', dict(verdict='request_changes', summary='s', approved_memory_keys=[],
                                  findings=[{**finding(), 'id': 'A'}, advisory, {**finding(), 'id': 'B'}]), 'X1')
                copy_rows = required_findings(run, 't', 'X2', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))
                self.assertEqual([(r['key'], r['finding_index'], r['label']) for r in copy_rows],
                                 [('f1', 0, 'A'), ('f2', 2, 'B')])
                second = record('R2', dict(verdict='request_changes', summary='s', approved_memory_keys=[], findings=[],
                                           finding_dispositions=self.dispositions(**statuses)),
                                'X2', required_findings=copy_rows)
                self.assertEqual([(r['finding_index'], r['label']) for r in required_findings(run, 't', 'X2', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))], expected)
                self.assertEqual([r['finding_index'] for r in required_findings(run, 't', 'X3', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))], [0, 2])
                # A copy whose rows no longer equal what the dispatch received cannot close anything.
                second['required_findings'] = list(reversed(copy_rows))
                with self.assertRaisesRegex(HarnessError, '对照不符'):
                    required_findings(run, 't', 'X2', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))
                second['required_findings'] = copy_rows[:1]
                with self.assertRaisesRegex(HarnessError, '对照不符'):
                    required_findings(run, 't', 'X2', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))

    def disposed(self, record, run, tmp, ident, snapshot, statuses, **extra):
        """Record a review of snapshot that disposes the rows its dispatch received."""
        rows = required_findings(run, 't', snapshot, files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))
        return record(ident, dict(verdict='request_changes', findings=[],
                                  finding_dispositions=self.dispositions(**statuses)),
                      snapshot, required_findings=rows, **extra)

    def test_repair_list_follows_usable_resolutions_and_any_reopening(self):
        with TemporaryDirectory() as tmp:
            run, record = self.history(tmp)
            record('R1', reported('A', 'B'), 'X1')
            self.assertEqual(labels(repair_findings(run, 't', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), ['A', 'B'])
            for ident, snapshot, statuses, repair in (
                    # not_checked neither takes B off the list nor, next, puts A back on it.
                    ('R2', 'X2', dict(f1='resolved', f2='not_checked'), ['B']),
                    ('R3', 'X3', dict(f1='not_checked', f2='resolved'), []),
                    ('R4', 'X4', dict(f1='open', f2='not_checked'), ['A'])):
                self.disposed(record, run, tmp, ident, snapshot, statuses)
                self.assertEqual(labels(repair_findings(run, 't', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), repair)
            self.assertEqual([(r['key'], r['finding_index']) for r in repair_findings(run, 't', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))], [('f1', 0)])
            # Leaving the repair list never closes a finding beyond the candidate that was reviewed.
            self.assertEqual(labels(required_findings(run, 't', 'X2', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), ['B'])
            self.assertEqual(labels(required_findings(run, 't', 'X3', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), ['A'])
            self.assertEqual(labels(required_findings(run, 't', 'X5', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), ['A', 'B'])

    def test_unusable_reviews_keep_their_open_vetoes_but_never_resolve(self):
        for status in ('invalid_rejected', 'invalidated'):
            extra = dict(status=status, **({'validation_error': 'invalid evidence'}
                                           if status == 'invalid_rejected' else {}))
            with self.subTest(status=status), TemporaryDirectory() as tmp:
                run, record = self.history(tmp)
                record('R1', reported('A', 'B'), 'X1')
                self.disposed(record, run, tmp, 'R2', 'X2', dict(f1='resolved', f2='resolved'))
                self.assertEqual(repair_findings(run, 't', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"])), [])
                self.disposed(record, run, tmp, 'R3', 'X3', dict(f1='open', f2='resolved'), **extra)
                self.assertEqual(labels(repair_findings(run, 't', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), ['A'])
                self.disposed(record, run, tmp, 'R4', 'X4', dict(f1='resolved', f2='not_checked'), **extra)
                self.assertEqual(labels(repair_findings(run, 't', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), ['A'])
                # Neither unusable review closed anything on the candidate it reviewed.
                self.assertEqual(labels(required_findings(run, 't', 'X3', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), ['A', 'B'])
                self.assertEqual(labels(required_findings(run, 't', 'X4', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), ['A', 'B'])
                self.assertEqual(required_findings(run, 't', 'X2', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"])), [])

    def test_every_saved_copy_is_checked_whatever_its_candidate_or_usability(self):
        tampers = (('order', lambda rows: list(reversed(rows))), ('count', lambda rows: rows[:1]),
                   ('label', lambda rows: [{**rows[0], 'label': 'invented'}, *rows[1:]]))
        with TemporaryDirectory() as tmp:
            run, record = self.history(tmp)
            record('R1', reported('A', 'B'), 'X1')
            saved = [self.disposed(record, run, tmp, ident, snapshot, dict(f1='open', f2='open'), **extra)
                     for ident, snapshot, extra in (
                         ('R2', 'X2', {}),
                         ('R3', 'X3', dict(status='invalid_rejected', validation_error='invalid evidence')),
                         ('R4', 'X4', dict(status='invalidated')))]
            self.assertEqual(labels(required_findings(run, 't', 'X5', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), ['A', 'B'])
            for entry in saved:
                original = entry['required_findings']
                for name, tamper in tampers:
                    with self.subTest(record=entry['review_source']['dispatch_id'], tamper=name):
                        entry['required_findings'] = tamper(original)
                        # Neither list may be derived from a history with a copy that differs.
                        with self.assertRaisesRegex(HarnessError, '对照不符'):
                            required_findings(run, 't', 'X5', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))
                        with self.assertRaisesRegex(HarnessError, '对照不符'):
                            repair_findings(run, 't', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))
                entry['required_findings'] = original
            self.assertEqual(labels(repair_findings(run, 't', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), ['A', 'B'])

    def test_an_empty_or_non_mapping_disposition_field_is_still_checked_against_its_copy(self):
        # An authenticated original registered as invalidated whose dispatch received
        # A as f1: disposing of nothing, or of a list, cannot pass for its saved copy.
        for name, dispositions, keep_copy in (('empty mapping', {}, False), ('empty list', [], False),
                                              ('list of the keys', ['f1'], True)):
            with self.subTest(name), TemporaryDirectory() as tmp:
                run, record = self.history(tmp)
                record('R1', reported('A'), 'X1')
                rows = required_findings(run, 't', 'X2', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))
                self.assertEqual(labels(rows), ['A'])
                record('R2', dict(verdict='request_changes', findings=[], finding_dispositions=dispositions),
                       'X2', required_findings=rows if keep_copy else [], status='invalidated')
                for calculate in (lambda: required_findings(run, 't', 'X3', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"])), lambda: repair_findings(run, 't', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))):
                    with self.assertRaisesRegex(EvidenceError, '对照不符') as caught:
                        calculate()
                    self.assertEqual((caught.exception.stop['kind'], caught.exception.stop['reason']),
                                     ('evidence', 'runtime_integrity'))

    def test_resolution_on_one_candidate_survives_an_unusable_reopening_on_another(self):
        with TemporaryDirectory() as tmp:
            run, record = self.history(tmp)
            record('R1', reported('X', 'Y'), 'S0')
            self.disposed(record, run, tmp, 'R2', 'A', dict(f1='resolved', f2='open'))
            self.disposed(record, run, tmp, 'R3', 'B', dict(f1='open', f2='resolved'),
                          status='invalid_rejected', validation_error='invalid evidence')
            # Back on A's bytes: A's usable resolution of X still holds there, while
            # B's kept veto puts X back on the repair list and B resolves nothing.
            self.assertEqual(labels(required_findings(run, 't', 'A', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), ['Y'])
            self.assertEqual(labels(required_findings(run, 't', 'B', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), ['X', 'Y'])
            self.assertEqual(labels(repair_findings(run, 't', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), ['X', 'Y'])

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
        row = worker['packet']['required_findings'][0]
        self.assertEqual((row['key'], row['finding_index'], row['label']), ('f1', 0, 'bad/id'))


class FinalFindingLifecycleTests(RepoCase):
    def backend(self, *, omit_dispositions=False, disposition_status='resolved'):
        import copy

        def change(backend, kwargs, packet, result):
            number = sum(c['kind'] == 'final_review' for c in backend.calls)
            if kwargs['kind'] == 'final_review' and number == 1:
                result.update(verdict='request_changes', findings=[finding()])
            elif trigger_kind(kwargs, packet) == 'final' and number == 1:
                task = copy.deepcopy(packet['original_tasks'][0])
                task.update(id='correct', depends_on=['greet'])
                revised(result, packet, 'Resolve final blocking finding', new_tasks=[task])
            elif kwargs['role'] == 'worker' and kwargs['task']['id'] == 'correct':
                with (kwargs['cwd'] / 'hello.py').open('a') as stream:
                    stream.write('# Independently reviewed final correction\n')
            elif kwargs['kind'] == 'final_review' and number == 2:
                if omit_dispositions:
                    result.pop('finding_dispositions', None)
                else:
                    for row in result['finding_dispositions'].values():
                        row['status'] = disposition_status

        return RecordingBackend(change)

    def test_missing_final_disposition_is_redone_before_completion(self):
        backend = self.backend(omit_dispositions=True)
        engine = self.engine(backend)
        run = engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
        self.assertEqual(run['status'], 'completed')
        self.assertIn('correct', run['accepted'])
        finals = [d for d in run['dispatches'] if d.get('kind') == 'final_review']
        self.assertEqual([d['status'] for d in finals], ['completed', 'invalid', 'completed'])
        self.assertEqual(finals[2]['retry_of'], finals[1]['id'])
        # The invalid approval left no record; the redo approved with every disposition.
        self.assertEqual([record['status'] for record in run['final_history']], ['rejected', 'approved'])
        approved = run['final_history'][1]
        self.assertEqual(set(approved['review']['finding_dispositions']), {'f1'})
        packets = [c['packet'] for c in backend.calls if c['kind'] == 'final_review']
        for packet in packets[1:]:
            self.assertEqual(packet['required_findings'][0]['review_dispatch_id'],
                             run['final_history'][0]['review_source']['dispatch_id'])
        self.assertEqual(packets[2]['previous_invalid']['dispatch_id'], finals[1]['id'])

    def test_open_final_disposition_cannot_complete(self):
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
        approval = result['final_history'][1]
        self.assertEqual(list(approval['review']['finding_dispositions']), ['f1'])
        row = approval['required_findings'][0]
        self.assertEqual((row['key'], row['review_dispatch_id'], row['finding_index'], row['label']),
                         ('f1', original['review_source']['dispatch_id'], 0, finding()['id']))
        # Replaying approval authenticates all previous source records too.
        Path(original['review_source']['artifact']).write_bytes(raw + b' ')
        with self.assertRaises(HarnessError):
            engine.services.finalization.final_review()

    def test_packets_authenticate_an_emptied_review_instead_of_skipping_it(self):
        engine = self.engine(self.backend())
        result = engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
        self.assertEqual(result['status'], 'completed')
        engine.services.packets.final_context_capacity()
        run = engine.context.run
        for name, record in (('task', run['review_history']['greet'][-1]), ('final', run['final_history'][-1])):
            with self.subTest(name):
                saved = record['review']
                record['review'] = {}
                # The run-wide view validates every history it projects from; an emptied
                # copy no longer hides its original's findings or resolutions.
                with self.assertRaises(EvidenceError) as caught:
                    engine.services.packets.final_context_capacity()
                self.assertEqual(caught.exception.stop['reason'], 'runtime_integrity')
                record['review'] = saved
        engine.services.packets.final_context_capacity()
