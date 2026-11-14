import copy
import unittest
from harness.diagnostics import budget_explanation
from harness.versions import RUN_SCHEMA_VERSION


class BudgetExplanationTests(unittest.TestCase):
    def test_counts_unique_candidates_and_separate_failure_correction_dimensions(self):
        workers = [{'id': 'd'+str(i), 'role': 'worker', 'task_id': 'x', 'invoked': True, 'pgid': 100+i,
                    'status': 'completed' if i < 3 else 'failed'} for i in range(5)]
        for entry in workers[3:]:  # judged worker failures carry their provider classification
            entry.update(worker_receipt_consumed='provider_failure', worker_return_evidence={
                'state': 'retryable', 'provider': {'code': 'invocation_failed', 'http_status': None}})
        invalid = [{'id': 'c1', 'role': 'commander', 'kind': 'decide', 'task_id': 'run', 'invoked': True, 'status': 'invalid'},
                   {'id': 'r1', 'role': 'reviewer', 'task_id': 'x', 'invoked': True, 'status': 'invalid'}]
        records = [{'artifact': 'review'+str(i), 'worker_source': {'dispatch_id': 'd'+str(i),
                    'artifact_digest': 'a'*64}} for i in range(3)]
        run = {'id': 'run_a', 'dispatches': workers + [workers[-1]] + invalid,
               'review_history': {'x': records + [records[-1]]}, 'schema_version': RUN_SCHEMA_VERSION,
               'agent_dispatches': 8,
               'config_snapshot': {'runtime': {'max_attempts': 5, 'max_agent_dispatches': 20}}}
        original = copy.deepcopy(run)
        report = budget_explanation(run, {'x': 5})
        dimensions = report['dimensions']
        self.assertEqual(dimensions['actual_worker_invocations'], 5)
        self.assertEqual(dimensions['reviewed_worker_candidates'], 3)
        self.assertEqual(dimensions['worker_failure_verdicts']['invocation_failed'], 2)
        self.assertEqual(dimensions['invalid_outputs'], {'commander': 1, 'worker': 0, 'reviewer': 1})
        self.assertEqual(report['limits']['task_work']['x']['remaining'], 0)
        # There is no run-wide worker counter or limit to explain.
        self.assertEqual(set(report['limits']), {'task_work', 'agent_dispatches'})
        self.assertEqual(report['limits']['agent_dispatches'], {'used': 8, 'limit': 20, 'remaining': 12})
        self.assertEqual(run, original)

    def test_unlinked_review_history_remains_unknown(self):
        run = {'dispatches': [{'id': 'old', 'role': 'worker', 'status': 'failed'}],
               'review_history': {'x': [{'artifact': 'r'}]}}
        facts = budget_explanation(run, {})['dimensions']
        self.assertEqual(facts['reviewed_worker_candidates'], 0)
        self.assertEqual(facts['unlinked_review_records'], 1)

    def test_charged_start_failure_is_not_an_actual_process_start(self):
        run = {'dispatches': [{'id': 'failed', 'role': 'worker', 'invoked': True, 'pgid': None,
                              'status': 'failed', 'worker_receipt_consumed': 'provider_failure',
                              'worker_return_evidence': {'state': 'retryable',
                                                         'provider': {'code': 'local_start', 'http_status': None}}}]}
        dimensions = budget_explanation(run, {})['dimensions']
        self.assertEqual(dimensions['charged_worker_invocations'], 1)
        self.assertEqual(dimensions['actual_worker_invocations'], 0)
        self.assertEqual(dimensions['worker_failure_verdicts']['local_start'], 1)
