"""Synthetic provider envelopes exercise diagnostics and real local CLI recovery."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.adapters import claude_error_summary, cli_error_summary
from harness.budgets import work_attempts
from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_provider_usage import FIXTURE_PROCESS, FixtureCLIBackend


def usage_limit_envelope():
    # Public field names and synthetic values only; no private raw CLI payload.
    return {"type": "result", "subtype": "success", "is_error": True,
            "terminal_reason": "api_error", "api_error_status": 429,
            "api_error": "usage_limit_reached", "result": "MODEL_PRIVATE_BODY",
            "unrecognized_extra": "PRIVATE_EXTRA_FIELD"}


class ProviderErrorSummaryTests(unittest.TestCase):
    def test_contradictory_success_flag_reports_usage_limit_without_model_text(self):
        summary = claude_error_summary(usage_limit_envelope())
        for diagnostic in ("error", "is_error=true", 'subtype="success"',
                           'terminal_reason="api_error"', "api_error_status=429",
                           'api_error="usage_limit_reached"'):
            self.assertIn(diagnostic, summary)
        self.assertNotIn("MODEL_PRIVATE_BODY", summary)
        self.assertNotIn("PRIVATE_EXTRA_FIELD", summary)

    def test_success_and_non_result_do_not_infer_failure_from_diagnostic_fields(self):
        payload = usage_limit_envelope()
        payload["is_error"] = False
        self.assertEqual(claude_error_summary(payload), "")
        payload["is_error"] = True
        payload["type"] = "assistant"
        self.assertEqual(claude_error_summary(payload), "")
        for value in (None, [], "MODEL_PRIVATE_BODY"):
            self.assertEqual(claude_error_summary(value), "")

    def test_unknown_or_malformed_codes_stay_private_and_status_is_strict_integer(self):
        for item in ("UNRECOGNIZED_SECRET\n\x1b[31m", ["NESTED_SECRET"],
                     {"private": "NESTED_SECRET"}, True, None, "x" * 100000):
            payload = usage_limit_envelope()
            payload.update(subtype=item, terminal_reason=item, api_error=item)
            with self.subTest(kind=type(item).__name__):
                summary = claude_error_summary(payload)
                self.assertIn("error", summary)
                self.assertNotIn("SECRET", summary)
                self.assertNotIn("\n", summary)
                self.assertNotIn("\x1b", summary)
                self.assertLess(len(summary), 220)
        for item in (True, False, 429.0, "429", 399, 600, None, {"status": 429}):
            payload = usage_limit_envelope()
            payload["api_error_status"] = item
            with self.subTest(status=item):
                self.assertIn("api_error_status=invalid", claude_error_summary(payload))
        for item in (400, 429, 500, 599):
            payload = usage_limit_envelope()
            payload["api_error_status"] = item
            self.assertIn(f"api_error_status={item}", claude_error_summary(payload))

    def test_turn_limit_errors_remain_bounded_and_escape_control_characters(self):
        message = "Reached maximum turns\n\r\x00\x1b\x85\u202e" + "\U0001f600" * 1000
        payload = {"type": "result", "subtype": "error_max_turns", "is_error": False,
                   "errors": [message] * 4 + ["PRIVATE_FIFTH_ERROR"],
                   "result": "MODEL_PRIVATE_BODY"}
        summary = claude_error_summary(payload)
        self.assertIn("error_max_turns", summary)
        self.assertIn("is_error=false", summary)
        self.assertEqual(summary.count("Reached maximum turns"), 3)
        self.assertNotIn("PRIVATE_FIFTH_ERROR", summary)
        self.assertNotIn("MODEL_PRIVATE_BODY", summary)
        self.assertTrue(all(32 <= ord(char) < 127 for char in summary))
        self.assertLess(len(summary), 9000)
        payload["errors"] = [{"private": "NESTED_SECRET"}, None, 429]
        self.assertNotIn("NESTED_SECRET", claude_error_summary(payload))

    def test_malformed_flags_have_readable_errors_without_nested_content(self):
        for payload in ({"type": "result", "is_error": True},
                        {"type": "result", "is_error": {"private": "NESTED_SECRET"}},
                        {"type": "result", "subtype": None}):
            summary = claude_error_summary(payload)
            self.assertIn("error", summary)
            self.assertNotIn("NESTED_SECRET", summary)

    def test_cli_stdout_reader_is_bounded_and_never_reads_other_providers(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = Path(temporary)
            output = job / "process.stdout"
            output.write_text(json.dumps(usage_limit_envelope()))
            self.assertIn("usage_limit_reached", cli_error_summary(job, "claude"))
            self.assertEqual(cli_error_summary(job, "codex"), "")
            with patch("harness.adapters._USAGE_MAX_BYTES", 32):
                self.assertEqual(cli_error_summary(job, "claude"), "")
            for raw in (b"{truncated", b"\xff", b"[]", b"null"):
                output.write_bytes(raw)
                self.assertEqual(cli_error_summary(job, "claude"), "")
            output.unlink()
            self.assertEqual(cli_error_summary(job, "claude"), "")


class DiagnosticFixtureBackend(FixtureCLIBackend):
    def command(self, role, model, cwd, job, schema, run_path):
        if role != "commander":
            return super().command(role, model, cwd, job, schema, run_path)
        self.calls.append({"role": role, "model": copy.deepcopy(model), "dispatch_id": job.name})
        result = {key: schema["properties"][key]["enum"][0] for key in ("task_id", "dispatch_id")}
        result.update(selected_task="greet", action="implement", instruction="Complete greet")
        return [sys.executable, "-c", FIXTURE_PROCESS, role, model["cli"], str(job), "{}", json.dumps(result)]


class ProviderErrorRecoveryTests(RepoCase):
    def test_provider_error_with_zero_exit_still_blocks_acceptance(self):
        backend = DiagnosticFixtureBackend(self.config, self.repo / ".harness", {
            "worker": {"envelope": usage_limit_envelope()}})
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "usage_limit_reached") as caught:
            engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.assertNotIn("MODEL_PRIVATE_BODY", str(caught.exception))
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(work_attempts(engine.run), {"greet": 1})
        self.assertEqual(engine.run["dispatches"][-1]["status"], "failed")
        self.assertEqual(len(backend.calls), 1)

    def test_nonzero_exit_keeps_usage_and_budget_then_recovers_with_new_dispatch(self):
        self.config.data["runtime"]["max_attempts"] = 1
        backend = DiagnosticFixtureBackend(self.config, self.repo / ".harness", {
            "worker": {"exit_code": 1, "envelope": usage_limit_envelope()}})
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "worker CLI exit=1") as caught:
            engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.assertIn("usage_limit_reached", str(caught.exception))
        self.assertNotIn("MODEL_PRIVATE_BODY", str(caught.exception))
        failed = engine.run["dispatches"][-1]
        artifact = engine.directory / "dispatches" / failed["id"] / "process.stdout"
        original = artifact.read_bytes()
        self.assertEqual(failed["provider_usage"]["reported_cost_usd"], 0.25)
        self.assertEqual(work_attempts(engine.run), {"greet": 1})
        self.assertNotIn("attempt_exemption", failed)
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(engine.run["agent_dispatches"], 1)
        backend.options = {}
        with self.assertRaisesRegex(HarnessError, "尝试次数上限"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), 1)

        self.config.data["runtime"]["max_attempts"] = 2
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_attempts"], 2)
        self.assertEqual(work_attempts(result), {"greet": 2})
        self.assertEqual(result["agent_dispatches"], 5)
        workers = [call for call in backend.calls if call["role"] == "worker"]
        self.assertEqual(len(workers), 2)
        self.assertNotEqual(workers[0]["dispatch_id"], workers[1]["dispatch_id"])
        self.assertTrue(all(call["model"] == self.config.role("worker") for call in workers))
        self.assertEqual(artifact.read_bytes(), original)
        self.assertEqual(result["dispatches"][0]["provider_usage"], failed["provider_usage"])
        self.assertEqual(len(result["review_history"]["greet"]), 1)
        self.assertEqual(result["final_history"][-1]["status"], "approved")
