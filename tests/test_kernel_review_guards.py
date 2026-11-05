import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from harness.run_store import RunStore
from harness.run_context import RunContext
from harness.review_progress import bind_review_source, required_findings
from harness.util import HarnessError


class KernelReviewGuards(unittest.TestCase):
    def test_stale_context_rejects_hook_and_snapshot_before_effect(self):
        with TemporaryDirectory() as tmp:
            first, second = RunStore(tmp), RunStore(tmp)
            first.attach({'id': 'run_guard'})
            first.commit()
            second.attach(json.loads(first._path.read_text()))
            context = object.__new__(RunContext)
            context.store = second
            context.hooks = SimpleNamespace(emit=Mock())
            first.run['changed'] = True
            first.commit()
            with self.assertRaises(HarnessError):
                context.emit_hook('context_prepared', {}, Path(tmp))
            context.hooks.emit.assert_not_called()
            with self.assertRaises(HarnessError):
                context.snapshot()

    def test_approval_without_receipt_fails_closed(self):
        context = SimpleNamespace(run={'review_receipts': {}})
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
            self.assertEqual(len(required_findings(run, 't', 'candidate')), 1)
            record['review']['findings'] = []
            with self.assertRaises(HarnessError):
                required_findings(run, 't', 'candidate')
            record['review']['findings'] = body['findings']
            path.unlink()
            with self.assertRaises(HarnessError):
                required_findings(run, 't', 'candidate')
