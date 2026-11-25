import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from harness.state_files import StateFiles
from harness.run_store import RunStore
from harness.run_context import RunContext
from harness.review_progress import bind_review_source, repair_findings, required_findings
from harness.util import EvidenceError, HarnessError


class KernelReviewGuards(unittest.TestCase):
    def test_stale_context_rejects_snapshot_before_effect(self):
        with TemporaryDirectory() as tmp:
            first, second = RunStore(tmp), RunStore(tmp)
            first.attach({'id': 'run_guard'})
            first.commit()
            second.attach(json.loads(first._path.read_text()))
            context = object.__new__(RunContext)
            context.store = second
            first.run['changed'] = True
            first.commit()
            with self.assertRaises(HarnessError):
                context.snapshot()

    def test_approval_without_receipt_fails_closed(self):
        with TemporaryDirectory() as tmp:
            context = SimpleNamespace(state=Path(tmp), run={'id': 'run_guard', 'review_receipts': {}})
            with self.assertRaises(HarnessError):
                bind_review_source(context, 'review', 't', {'verdict': 'approve'}, 'candidate')

    def test_historical_ledger_copy_cannot_close_or_erase_original(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'runs/run_guard/dispatches/reviewer/returned-result.json'
            path.parent.mkdir(parents=True)
            body = {'task_id': 't', 'dispatch_id': 'reviewer', 'verdict': 'request_changes',
                    'findings': [{'id': 'f', 'severity': 'blocking'}]}
            path.write_text(json.dumps(body))
            source = {'dispatch_id': 'reviewer', 'artifact': str(path),
                      'artifact_digest': hashlib.sha256(path.read_bytes()).hexdigest(),
                      'snapshot_digest': 'candidate'}
            record = {'dispatch_id': 'reviewer', 'artifact': 'inspectable',
                      'review_source': source, 'snapshot_digest': 'candidate',
                      'review': {k: v for k, v in body.items() if k not in ('task_id', 'dispatch_id')}}
            run = {'id': 'run_guard', 'dispatches': [{'id': 'reviewer', 'role': 'reviewer',
                   'kind': 'review', 'task_id': 't', 'snapshot_digest': 'candidate'}], 'review_history': {'t': [record]}}
            self.assertEqual(len(required_findings(run, 't', 'candidate', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), 1)
            record['review']['findings'] = []
            with self.assertRaises(HarnessError):
                required_findings(run, 't', 'candidate', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))
            record['review']['findings'] = body['findings']
            # An emptied copy is still a review to authenticate, not a record without one.
            saved = record['review']
            for emptied in ({}, None):
                record['review'] = emptied
                for calculate in (lambda: required_findings(run, 't', 'candidate', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"])), lambda: repair_findings(run, 't', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))):
                    with self.assertRaises(EvidenceError) as caught:
                        calculate()
                    self.assertEqual(caught.exception.stop['reason'], 'runtime_integrity')
            record['review'] = saved
            self.assertEqual(len(repair_findings(run, 't', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))), 1)
            path.unlink()
            with self.assertRaises(HarnessError):
                required_findings(run, 't', 'candidate', files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))
