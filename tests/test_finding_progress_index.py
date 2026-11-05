import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from harness.context import prompt_json
from harness.util import HarnessError
from harness.verification_plan import finding_progress, finding_progress_packet, project_finding_sources


class FindingProgressIndexTests(unittest.TestCase):
    def history(self):
        return {'review_history': {'task': [{
            'artifact': 'full-review.json', 'dispatch_id': 'original', 'snapshot_digest': 'candidate-1',
            'review': {'findings': [dict(id=f'finding-{i}', severity='blocking', message='evidence ' * 500)
                                    for i in range(75)]}}, {
            'artifact': 'later-review.json', 'dispatch_id': 'later', 'snapshot_digest': 'candidate-2',
            'review': {'findings': [], 'finding_dispositions': [dict(
                review_dispatch_id='original', finding_id='finding-74', status='resolved', evidence=['checked'])]}}]}}

    def test_summary_retrieves_every_row_and_candidate_scoped_disposition(self):
        with TemporaryDirectory() as directory:
            run = self.history()
            packet = finding_progress_packet(run, 'task', Path(directory).resolve())
            self.assertEqual(packet['count'], 75)
            self.assertEqual(packet['status_counts'], {'open': 74, 'resolved': 1})
            self.assertNotIn('findings', packet)
            self.assertLess(len(prompt_json(packet)), 2000)
            path = Path(packet['index']['artifact'])
            raw = path.read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), packet['index']['artifact_digest'])
            progress = json.loads(raw)['progress']
            self.assertEqual(progress, finding_progress(run, 'task', limit=None))
            self.assertEqual(progress['omitted_count'], 0)
            self.assertEqual(progress['findings'][-1]['message'], 'evidence ' * 500)
            self.assertEqual(progress['findings'][-1]['snapshot_digest'], 'candidate-1')
            self.assertEqual(progress['findings'][-1]['disposition_source']['snapshot_digest'], 'candidate-2')
            self.assertEqual(finding_progress_packet(run, 'task', Path(directory).resolve()), packet)

    def test_changed_index_is_retained_and_rejected(self):
        with TemporaryDirectory() as directory:
            run = self.history()
            packet = finding_progress_packet(run, 'task', Path(directory).resolve())
            path = Path(packet['index']['artifact'])
            path.write_bytes(b'changed evidence')
            with self.assertRaisesRegex(HarnessError, 'index changed'):
                finding_progress_packet(run, 'task', Path(directory).resolve())
            self.assertEqual(path.read_bytes(), b'changed evidence')

    def test_symlink_artifact_and_parent_are_rejected(self):
        for parent in (False, True):
            with self.subTest(parent=parent), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                run = self.history()
                packet = finding_progress_packet(run, 'task', root)
                artifact = Path(packet['index']['artifact'])
                if parent:
                    folder = artifact.parent
                    renamed = folder.with_name('retained-indexes')
                    folder.rename(renamed)
                    folder.symlink_to(renamed, target_is_directory=True)
                else:
                    retained = artifact.with_suffix('.retained')
                    artifact.rename(retained)
                    artifact.symlink_to(retained)
                with self.assertRaisesRegex(HarnessError, 'symlink'):
                    finding_progress_packet(run, 'task', root)

    def test_required_source_projection_preserves_every_reference_and_is_repeatable(self):
        packet = {'required_findings': [dict(review_dispatch_id='review', finding_id=f'finding-{i}',
                    original_finding_id='duplicate', finding_index=i, artifact='/private/' + 'original/' * 100 + 'review.json', status='open')
                    for i in range(3)]}
        packet['repair_claims'] = [dict(review_dispatch_id='review', finding_id=f'finding-{i}',
                                       summary=f'Claim {i}', evidence_paths=['proof.txt']) for i in range(3)]
        projected = project_finding_sources(packet)
        self.assertEqual(projected['required_finding_sources'], {'review': {'artifact': packet['required_findings'][0]['artifact']}})
        for before, after in zip(packet['required_findings'], projected['required_findings']):
            self.assertEqual({**after, **projected['required_finding_sources'][after['review_dispatch_id']]}, before)
        for before, after in zip(packet['repair_claims'], projected['repair_claims']):
            row = projected['required_findings'][after['required_finding_index']]
            restored = {k: v for k, v in after.items() if k != 'required_finding_index'}
            restored.update(review_dispatch_id=row['review_dispatch_id'], finding_id=row['finding_id'])
            self.assertEqual(restored, before)
        self.assertEqual(project_finding_sources(projected), projected)
        self.assertTrue(all('artifact' in row for row in packet['required_findings']))
        with self.assertRaises(HarnessError):
            project_finding_sources({'required_findings': [{'review_dispatch_id': 'missing', 'finding_id': 'f'}]})

    def test_duplicate_and_unknown_claims_are_retained(self):
        known = dict(review_dispatch_id='review', finding_id='finding', summary='one', evidence_paths=['a'])
        unknown = dict(review_dispatch_id='unknown', finding_id='other', summary='unknown', evidence_paths=['b'])
        packet = {'required_findings': [dict(review_dispatch_id='review', finding_id='finding', artifact='/' + 'private-source/' * 100 + 'raw.json'),
                    dict(review_dispatch_id='review', finding_id='second', artifact='/' + 'private-source/' * 100 + 'raw.json')],
                  'repair_claims': [known, {**known, 'summary': 'two'}, unknown]}
        projected = project_finding_sources(packet)
        self.assertEqual(len(projected['repair_claims']), 3)
        self.assertEqual([r['required_finding_index'] for r in projected['repair_claims'][:2]], [0, 0])
        self.assertEqual([r['summary'] for r in projected['repair_claims']], ['one', 'two', 'unknown'])
        self.assertEqual(projected['repair_claims'][2], unknown)
        self.assertEqual(project_finding_sources(projected), projected)

    def test_small_required_list_retains_cheaper_inline_form(self):
        packet = {'required_findings': [dict(review_dispatch_id='review', finding_id='one', artifact='/raw.json')]}
        self.assertEqual(project_finding_sources(packet), packet)

    def test_empty_progress_needs_no_artifact(self):
        with TemporaryDirectory() as directory:
            self.assertIsNone(finding_progress_packet({}, 'task', Path(directory).resolve()))
            self.assertEqual(list(Path(directory).iterdir()), [])
