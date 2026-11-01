import copy
import unittest
from harness.diagnostics import budget_explanation


class BudgetExplanationTests(unittest.TestCase):
    def test_counts_unique_candidates_and_separate_failure_correction_dimensions(self):
        workers = [{'id': 'd'+str(i), 'role': 'worker', 'task_id': 'x', 'invoked': True, 'pgid': 100+i,
                    'status': 'completed' if i < 3 else 'failed',
                    'diagnostics': {'failure_class': 'provider_reported'}} for i in range(5)]
        workers[-1]['format_correction'] = True
        records = [{'artifact': 'review'+str(i), 'worker_source': {'dispatch_id': 'd'+str(i),
                    'artifact_digest': 'a'*64}} for i in range(3)]
        run = {'id': 'run_a', 'dispatches': workers + [workers[-1]],
               'review_history': {'x': records + [records[-1]]}, 'total_attempts': 5,
               'agent_dispatches': 8, 'final_attempts': 0, 'replan_attempts': 0,
               'config_snapshot': {'runtime': {'max_attempts': 5, 'max_total_attempts': 20}}}
        original = copy.deepcopy(run)
        report = budget_explanation(run, {'x': 5})
        dimensions = report['dimensions']
        self.assertEqual(dimensions['actual_worker_invocations'], 5)
        self.assertEqual(dimensions['reviewed_worker_candidates'], 3)
        self.assertEqual(dimensions['failed_worker_invocations']['provider_reported'], 2)
        self.assertEqual(dimensions['worker_format_corrections'], 1)
        self.assertEqual(report['limits']['task_work']['x']['remaining'], 0)
        self.assertEqual(run, original)

    def test_unlinked_history_and_legacy_invocation_remain_unknown(self):
        run = {'dispatches': [{'id': 'old', 'role': 'worker', 'status': 'failed'}],
               'review_history': {'x': [{'artifact': 'r'}]}}
        facts = budget_explanation(run, {})['dimensions']
        self.assertEqual(facts['reviewed_worker_candidates'], 0)
        self.assertEqual(facts['unlinked_review_records'], 1)
        self.assertEqual(facts['historical_invocation_unknown'], 1)

    def test_charged_start_failure_is_not_an_actual_process_start(self):
        run = {'dispatches': [{'id': 'failed', 'role': 'worker', 'invoked': True, 'pgid': None,
                              'status': 'failed', 'diagnostics': {'failure_class': 'local_start'}}]}
        dimensions = budget_explanation(run, {})['dimensions']
        self.assertEqual(dimensions['charged_worker_invocations'], 1)
        self.assertEqual(dimensions['actual_worker_invocations'], 0)
        self.assertEqual(dimensions['failed_worker_invocations']['local_start'], 1)
