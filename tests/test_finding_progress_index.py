import errno
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from harness import state_files
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
            # The later review disposed one runtime key; its saved copy maps the key to the finding.
            'required_findings': [dict(key='f1', review_dispatch_id='original', finding_index=74,
                                       label='finding-74', artifact='full-review.json')],
            'review': {'findings': [], 'finding_dispositions': {'f1': dict(status='resolved', evidence=['checked'])}}}]}}

    def test_summary_retrieves_every_row_and_candidate_scoped_disposition(self):
        with TemporaryDirectory() as directory:
            run = self.history()
            packet = finding_progress_packet(run, ['task'], Path(directory).resolve())
            self.assertEqual(packet['count'], 75)
            self.assertEqual(packet['status_counts'], {'open': 74, 'resolved': 1})
            self.assertNotIn('findings', packet)
            self.assertLess(len(prompt_json(packet)), 2000)
            path = Path(packet['index']['artifact'])
            raw = path.read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), packet['index']['artifact_digest'])
            progress = json.loads(raw)['progress']
            self.assertEqual(progress, finding_progress(run, ['task'], limit=None))
            self.assertEqual(progress['omitted_count'], 0)
            self.assertEqual(progress['findings'][-1]['message'], 'evidence ' * 500)
            self.assertEqual(progress['findings'][-1]['snapshot_digest'], 'candidate-1')
            self.assertEqual(progress['findings'][-1]['disposition_source']['snapshot_digest'], 'candidate-2')
            self.assertEqual(finding_progress_packet(run, ['task'], Path(directory).resolve()), packet)

    def test_status_follows_open_from_any_review_and_resolved_only_from_a_usable_one(self):
        def review(dispatch, snapshot, **statuses):
            return {'artifact': f'{dispatch}.json', 'dispatch_id': dispatch, 'snapshot_digest': snapshot,
                    'required_findings': [dict(key=f'f{i + 1}', review_dispatch_id='original', finding_index=i,
                                               label=name, artifact='original.json') for i, name in enumerate('ABCD')],
                    'review': {'findings': [], 'finding_dispositions': {
                        key: dict(status=status, evidence=['checked'] if status == 'resolved' else [])
                        for key, status in statuses.items()}}}
        for status in ('invalid_rejected', 'invalidated'):
            with self.subTest(status=status):
                original = {'artifact': 'original.json', 'dispatch_id': 'original', 'snapshot_digest': 'candidate-1',
                            'review': {'findings': [dict(id=name, severity='blocking', message=name) for name in 'ABCD']}}
                usable = review('usable', 'candidate-2', f1='resolved', f2='open', f3='not_checked', f4='resolved')
                unusable = {**review('unusable', 'candidate-3', f1='open', f2='resolved', f3='not_checked',
                                     f4='not_checked'), 'status': status}
                run = {'review_history': {'task': [original, usable, unusable]}}
                rows = {row['label']: row for row in finding_progress(run, ['task'], limit=None)['findings']}
                sources = {label: (row['disposition_source'] or {}).get('review_dispatch_id')
                           for label, row in rows.items()}
                # A: the unusable review's open is a kept veto. B: its resolved changes nothing.
                # C: not_checked never sets a status. D: not_checked keeps the usable resolution.
                self.assertEqual({label: row['status'] for label, row in rows.items()},
                                 {'A': 'open', 'B': 'open', 'C': 'open', 'D': 'resolved'})
                self.assertEqual(sources, {'A': 'unusable', 'B': 'usable', 'C': None, 'D': 'usable'})

    def test_changed_index_is_retained_and_rejected(self):
        with TemporaryDirectory() as directory:
            run = self.history()
            packet = finding_progress_packet(run, ['task'], Path(directory).resolve())
            path = Path(packet['index']['artifact'])
            path.write_bytes(b'changed evidence')
            with self.assertRaisesRegex(HarnessError, 'index changed'):
                finding_progress_packet(run, ['task'], Path(directory).resolve())
            self.assertEqual(path.read_bytes(), b'changed evidence')

    def test_failed_index_probe_never_publishes_an_artifact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            failed = state_files.Outcome("failed", str(root), state_files.Step(0, "finding-progress", "open", False),
                                         errno=errno.EIO)
            with patch.object(state_files.StateFiles, "probe", return_value=failed), self.assertRaises(HarnessError) as caught:
                finding_progress_packet(self.history(), ["task"], root)
            self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), ("runtime_failure", "io"))
            self.assertEqual(list(root.iterdir()), [])

    def test_symlink_artifact_and_parent_are_rejected(self):
        for parent in (False, True):
            with self.subTest(parent=parent), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                run = self.history()
                packet = finding_progress_packet(run, ['task'], root)
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
                    finding_progress_packet(run, ['task'], root)

    def test_required_source_projection_preserves_every_reference_and_is_repeatable(self):
        packet = {'required_findings': [dict(key=f'f{i + 1}', review_dispatch_id='review', finding_index=i,
                    label='duplicate', artifact='/private/' + 'original/' * 100 + 'review.json')
                    for i in range(3)]}
        projected = project_finding_sources(packet)
        self.assertEqual(projected['required_finding_sources'], {'review': {'artifact': packet['required_findings'][0]['artifact']}})
        for before, after in zip(packet['required_findings'], projected['required_findings']):
            self.assertEqual({**after, **projected['required_finding_sources'][after['review_dispatch_id']]}, before)
        self.assertEqual(project_finding_sources(projected), projected)
        self.assertTrue(all('artifact' in row for row in packet['required_findings']))
        with self.assertRaises(HarnessError):
            project_finding_sources({'required_findings': [{'key': 'f1', 'review_dispatch_id': 'missing', 'finding_index': 0}]})

    def test_small_required_list_retains_cheaper_inline_form(self):
        packet = {'required_findings': [dict(key='f1', review_dispatch_id='review', finding_index=0,
                                             label='one', artifact='/raw.json')]}
        self.assertEqual(project_finding_sources(packet), packet)

    def test_empty_progress_needs_no_artifact(self):
        with TemporaryDirectory() as directory:
            self.assertIsNone(finding_progress_packet({}, ['task'], Path(directory).resolve()))
            self.assertEqual(list(Path(directory).iterdir()), [])
