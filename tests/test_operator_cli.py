import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness.cli import _main, status_summary
from harness.util import HarnessError


class OperatorCLITests(unittest.TestCase):
    def test_explicit_decision_path_reaches_engine_and_failure_is_reported(self):
        engine = Mock()
        engine.resume.side_effect = HarnessError("decision source has changed")
        errors = io.StringIO()
        with patch("harness.cli.Config.load"), patch("harness.cli.git.repository", return_value=Path("/tmp/repo")), \
                patch("harness.cli.Engine", return_value=engine), contextlib.redirect_stderr(errors):
            code = _main(["resume", "run_test", "--operator-decision", "answer.json", "--step"])
        self.assertEqual(code, 1)
        self.assertEqual(engine.resume.call_args.kwargs["operator_decision"], Path("answer.json"))
        self.assertTrue(engine.resume.call_args.kwargs["step"])
        self.assertIn("decision source has changed", errors.getvalue())

    def test_status_exposes_only_unanswered_current_requests(self):
        run = {"accepted": {}, "decision_requests": [
            {"id": "worker", "task_id": "work", "kind": "worker", "question": "Choose approach"},
            {"id": "old", "task_id": "work", "kind": "replan_stop", "source": {"dispatch_id": "old"}},
            {"id": "new", "task_id": "work", "kind": "replan_stop", "source": {"dispatch_id": "new"}}],
            "replan_stopped": {"dispatch_id": "new"}, "operator_decisions": []}
        self.assertEqual([r["id"] for r in status_summary(run)["pending_decisions"]], ["worker", "new"])
        run["operator_decisions"] = [{"request_id": "worker", "decision": "continue"},
                                     {"request_id": "new", "decision": "stop"}]
        self.assertEqual([r["id"] for r in status_summary(run)["pending_decisions"]], ["new"])
        run["operator_decisions"].append({"request_id": "new", "decision": "continue"})
        self.assertNotIn("pending_decisions", status_summary(run))

    def test_operator_outcome_does_not_claim_acceptance(self):
        result = status_summary({"dispatches": [{"role": "worker", "worker_outcome": "operator_blocked"}]})
        self.assertEqual(result["worker_outcomes"]["operator_blocked"], 1)
        self.assertEqual(result["accepted_tasks"], [])
