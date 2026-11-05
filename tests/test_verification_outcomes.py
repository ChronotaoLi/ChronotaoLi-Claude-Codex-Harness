import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.process_runtime import ProcessOutcome
from harness.util import HarnessError, ProcessTimeout
from harness.verification import Verification, checks_passed


class VerificationOutcomeTests(unittest.TestCase):
    def fixture(self, root):
        context = SimpleNamespace(
            run={"dispatches": []}, config=SimpleNamespace(data={"runtime": {"verify_timeout_seconds": 1}}),
            ensure_exited=lambda: None, save=lambda: None)
        return Verification(context), root / "checks"

    def run_fake(self, verification, folder, outcome, error=None):
        def process(argv, cwd, log, **kwargs):
            kwargs["on_outcome"](outcome)
            if error:
                error.outcome = outcome
                raise error
            return outcome.exit_code
        with patch("harness.verification.git.check_worktree_binding"), patch("harness.verification.run_process", process):
            return verification.run_verification_commands("task", [["check"]], folder.parent, folder, {})

    def test_zero_exit_with_unknown_group_cannot_pass_and_leaves_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            verification, folder = self.fixture(Path(directory))
            outcome = ProcessOutcome(started=True, exit_code=0, exit_confirmed=False,
                evidence_complete=False, cleanup_errors=[{"step": "observe_group", "errno": 1}])
            with self.assertRaises(HarnessError):
                self.run_fake(verification, folder, outcome)
            results = json.loads((folder / "results.json").read_text())
            self.assertFalse(checks_passed(results))
            self.assertEqual(results[0]["exit_code"], 0)
            self.assertEqual(results[0]["process_outcome"]["cleanup_errors"][0]["errno"], 1)

    def test_timeout_preserves_observed_signal_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            verification, folder = self.fixture(Path(directory))
            outcome = ProcessOutcome(started=True, exit_code=-15, exit_confirmed=True, termination_reason="timeout")
            results = self.run_fake(verification, folder, outcome, ProcessTimeout(1, folder / "check-0"))
            self.assertEqual(results[0]["exit_code"], -15)
            self.assertEqual(results[0]["status"], "timed_out")
            self.assertFalse(checks_passed(results))

    def test_interrupt_saves_partial_receipt_before_propagation(self):
        with tempfile.TemporaryDirectory() as directory:
            verification, folder = self.fixture(Path(directory))
            outcome = ProcessOutcome(started=True, exit_code=-15, exit_confirmed=True, termination_reason="cancelled")
            with self.assertRaises(KeyboardInterrupt):
                self.run_fake(verification, folder, outcome, KeyboardInterrupt())
            result = json.loads((folder / "results.json").read_text())[0]
            self.assertEqual(result["status"], "interrupted")
            self.assertEqual(verification.context.run["dispatches"][0]["exit_code"], -15)

    def test_ledger_save_failure_still_preserves_command_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            verification, folder = self.fixture(Path(directory))
            count = 0
            def save():
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError("ledger unavailable")
            verification.context.save = save
            outcome = ProcessOutcome(started=True, exit_code=0, exit_confirmed=True)
            with self.assertRaises(OSError):
                self.run_fake(verification, folder, outcome)
            saved = json.loads((folder / "results.json").read_text())[0]
            self.assertEqual(saved["exit_code"], 0)
            self.assertEqual(saved["status"], "failed")
            self.assertFalse(saved["evidence_complete"])

    def test_all_formal_commands_execute_after_known_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            verification, folder = self.fixture(Path(directory))
            with patch("harness.verification.git.check_worktree_binding"):
                results = verification.run_verification_commands("task", [
                    [sys.executable, "-c", "raise SystemExit(3)"],
                    [sys.executable, "-c", "print('second gate ran')"]], Path(directory), folder, {})
            self.assertEqual([row["exit_code"] for row in results], [3, 0])
            self.assertEqual([row["status"] for row in results], ["failed", "passed"])
            self.assertIn("second gate ran", results[1]["stdout"])
            self.assertFalse(checks_passed(results))

    def test_passed_status_requires_consistent_complete_lifecycle(self):
        for result in ({"exit_code": 0}, {"exit_code": None}, {"exit_code": 0, "status": "unknown"}):
            self.assertFalse(checks_passed([result]))
        passed = {"status": "passed", "exit_code": 0, "termination_reason": "normal",
                  "exit_confirmed": True, "evidence_complete": True}
        self.assertTrue(checks_passed([passed]))
        for key, value in (("status", "unknown"), ("exit_code", 1), ("exit_code", False),
                           ("termination_reason", "timeout"), ("exit_confirmed", False),
                           ("evidence_complete", False)):
            with self.subTest(field=key, value=value):
                self.assertFalse(checks_passed([{**passed, key: value}]))
                self.assertFalse(checks_passed([{k: v for k, v in passed.items() if k != key}]))
