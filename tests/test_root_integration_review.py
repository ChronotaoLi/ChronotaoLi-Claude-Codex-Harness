"""Independent behavioral checks for read-only budget telemetry."""
import copy
import unittest

from harness.diagnostics import budget_explanation


class RootIntegrationReviewTests(unittest.TestCase):
    def test_dimensions_deduplicate_and_preserve_unknown_candidates(self):
        workers = [dict(id=f'w{i}', role='worker', invoked=True, pgid=100+i, task_id='task', status='finished') for i in range(5)]
        for worker in workers[3:]:
            worker.update(status='failed', worker_receipt_consumed='provider_failure', worker_return_evidence={
                'state': 'retryable', 'provider': {'code': 'invocation_failed', 'http_status': None}})
        run = dict(id='run_abc', dispatches=workers + [copy.deepcopy(workers[0]),
                   dict(id='unlinked', role='worker', task_id='task')],
                   review_history={'task': [dict(artifact=f'review{i}', worker_source={
                     'dispatch_id': f'w{i}', 'artifact': f'/dispatches/w{i}/returned-result.json',
                     'artifact_digest': 'a' * 64}) for i in range(3)] + [dict(artifact='legacy-review')]},
                   schema_version=18, agent_dispatches=9,
                   config_snapshot={'runtime': {'max_attempts': 5, 'max_agent_dispatches': 12}})
        before = copy.deepcopy(run)
        report = budget_explanation(run, {'task': 5})
        dims = report['dimensions']
        self.assertEqual(dims['actual_worker_invocations'], 5)
        self.assertEqual(dims['reviewed_worker_candidates'], 3)
        self.assertEqual(dims['worker_failure_verdicts']['invocation_failed'], 2)
        self.assertEqual(dims['duplicate_dispatch_ids'], 1)
        self.assertEqual(dims['unlinked_review_records'], 1)
        self.assertEqual(report['limits']['task_work']['task']['remaining'], 0)
        self.assertEqual(report['limits']['agent_dispatches'], {'used': 9, 'limit': 12, 'remaining': 3})
        self.assertEqual(run, before)
