import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness.cli import _main, status_summary
from harness.util import HarnessError


class EnvironmentCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)

    def invoke(self, extra, error=None):
        engine = Mock()
        engine.resume.return_value = dict(id="run_abc", status="paused", branch="test", worktree="/tmp/test")
        engine.resume.side_effect = error
        output, errors = io.StringIO(), io.StringIO()
        with patch("harness.cli.Config.load"), patch("harness.cli.git.repository", return_value=self.repo), \
                patch("harness.operations.probe_cli", return_value={"path": None, "version": None}), \
                patch("harness.cli.Engine", return_value=engine), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = _main(["resume", "run_abc", *extra])
        return code, engine.resume, errors.getvalue()

    def test_environment_update_forwarded_as_path_only_when_supplied(self):
        code, resume, _ = self.invoke(["--environment-update", "clarification.json"])
        self.assertEqual(code, 0)
        self.assertEqual(resume.call_args.kwargs["environment_update"], Path("clarification.json"))
        code, resume, _ = self.invoke([])
        self.assertEqual(code, 0)
        self.assertNotIn("environment_update", resume.call_args.kwargs)

    def test_environment_update_error_returns_cli_failure(self):
        code, resume, errors = self.invoke(["--environment-update", "missing.json"], HarnessError("missing update"))
        self.assertEqual(code, 1)
        self.assertEqual(resume.call_count, 1)
        self.assertIn("missing update", errors)

    def fixture(self):
        return {"current_task": "task", "accepted": {}, "total_attempts": 7,
                "feedback": {"task_id": "task", "attempt": 2, "worker": {"status": "blocked", "blocker_kind": "environment", "blockers": ["missing dependency"]},
                             "worker_artifact": "/private/dispatches/d3/returned-result.json", "worker_artifact_digest": "abc"},
                "dispatches": [{"id": "d1", "role": "worker", "status": "completed"},
                               {"id": "d2", "role": "worker", "status": "failed"},
                               {"id": "d3", "role": "worker", "status": "completed", "worker_outcome": "environment_blocked"},
                               {"role": "reviewer", "status": "failed", "invoked": False}],
                "environment_updates": [{"id": "update1", "sequence": 1, "task_id": "task", "blocker_dispatch_id": "d3"}]}

    def test_outcomes_are_observations_and_current_blocker_has_source(self):
        run = self.fixture()
        summary = status_summary(run)
        self.assertEqual(summary["budgets"]["total_attempts"], 7)
        self.assertEqual(summary["worker_outcomes"], dict(completed=0, task_blocked=0, environment_blocked=1, operator_blocked=0, runtime_failed=1, unknown=1))
        blocker = summary["last_environment_blocker"]
        self.assertFalse(blocker["stale"])
        self.assertEqual(blocker["dispatch_id"], "d3")
        self.assertEqual(blocker["attempt"], 2)
        self.assertEqual(blocker["source_digest"], "abc")
        self.assertEqual(blocker["latest_update_version"], 1)

    def test_blocker_is_historical_after_acceptance_task_change_or_later_worker(self):
        for change in (lambda run: run["accepted"].update(task={}),
                       lambda run: run.update(current_task="other"),
                       lambda run: run["dispatches"].append({"role": "worker", "process_state": "prepared"})):
            run = self.fixture()
            change(run)
            self.assertTrue(status_summary(run)["last_environment_blocker"]["stale"])

    def test_preparation_failure_keeps_blocker_current_until_backend_invocation(self):
        run = self.fixture()
        later = {"role": "worker", "process_state": "prepared", "invoked": False}
        run["dispatches"].append(later)
        self.assertFalse(status_summary(run)["last_environment_blocker"]["stale"])
        later.update(invoked=True, process_state="starting")
        self.assertTrue(status_summary(run)["last_environment_blocker"]["stale"])

    def test_later_commander_selection_makes_blocker_historical_without_worker(self):
        run = self.fixture()
        run["dispatches"].append({"role": "commander", "kind": "dispatch", "invoked": True})
        self.assertTrue(status_summary(run)["last_environment_blocker"]["stale"])

    def test_invalid_or_absent_attempt_is_not_inferred(self):
        for attempt in (None, True, 0, -1, "2"):
            run = self.fixture()
            run["feedback"]["attempt"] = attempt
            self.assertNotIn("attempt", status_summary(run)["last_environment_blocker"])

    def test_corrupt_optional_diagnostics_are_safe(self):
        run = self.fixture()
        run.update(dispatches=[None, {"role": "worker", "worker_outcome": []}], environment_updates=[None])
        run["feedback"]["worker_artifact"] = []
        run["feedback"]["worker"]["blockers"] = "wrong"
        summary = status_summary(run)
        self.assertEqual(summary["worker_outcomes"]["unknown"], 1)
        self.assertTrue(summary["last_environment_blocker"]["stale"])
        self.assertEqual(summary["last_environment_blocker"]["blockers"], [])
