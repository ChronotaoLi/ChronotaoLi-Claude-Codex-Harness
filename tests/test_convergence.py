import copy
import unittest

from harness.cli import status_summary
from harness.convergence import convergence_summary


def rejection(*ids):
    return {"review": {"verdict": "request_changes", "findings": [
        {"id": ident, "severity": "blocking"} for ident in ids]}}


class ConvergenceTests(unittest.TestCase):
    def test_recurring_blocker_and_work_attempts_are_advisory_and_do_not_mutate_ledger(self):
        run = {"status": "running", "accepted": {}, "attempts": {"task": 2},
               "review_history": {"task": [rejection("same", "fixed"), rejection("same", "new")]}}
        before = copy.deepcopy(run)
        warning = status_summary(run)["convergence"]["warnings"][0]
        self.assertEqual(warning["recurring_findings"], ["same"])
        self.assertEqual(warning["review_rejections"], 2)
        self.assertEqual(run, before)
        self.assertTrue(status_summary(run)["convergence"]["advisory_only"])

    def test_accepted_and_terminal_runs_do_not_report_resolved_rework_as_current(self):
        run = {"status": "running", "accepted": {"old": {}},
               "review_history": {"old": [rejection("x"), rejection("x")]},
               "final_history": [{"status": "rejected"}, {"status": "rejected"}]}
        result = convergence_summary(run, {"old": 10, "pending": 3})
        self.assertEqual([w["kind"] for w in result["warnings"]], ["task_rework", "whole_run_rework"])
        for status in ("completed", "abandoned"):
            self.assertEqual(convergence_summary({**run, "status": status}, {"old": 10})["warnings"], [])

    def test_signal_uses_work_attempts_and_only_consecutive_rejections(self):
        run = {"status": "running", "attempts": {"task": 3}, "attempt_budget_policy": "work",
               "dispatches": [{"role": "worker", "task_id": "task", "attempt_exemption": {
                   "blocker_kind": "environment", "artifact": "/evidence/returned-result.json",
                   "artifact_digest": "0" * 64}}],
               "review_history": {"task": [rejection("x"), {"review": {"verdict": "approve"}}]}}
        # Observation uses saved accounting, not an extra interpretation of retries.
        self.assertEqual(status_summary(run)["budgets"]["work_attempts"], {"task": 2})
        self.assertEqual(status_summary(run)["convergence"]["warnings"], [])
        run["attempt_budget_policy"] = "all_invocations"
        self.assertEqual(status_summary(run)["convergence"]["warnings"][0]["recurring_findings"], [])

    def test_damaged_history_and_large_warning_sets_remain_bounded(self):
        run = {"status": "running", "accepted": {}, "review_history": {"bad": None}, "final_history": None}
        result = convergence_summary(run, {str(i): 3 for i in range(30)})
        self.assertEqual(len(result["warnings"]), 20)
        self.assertTrue(result["truncated"])
