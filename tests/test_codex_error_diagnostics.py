"""Offline failed-process fixtures; no provider accounts are invoked."""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from harness.adapters import DemoBackend, _CODEX_ERROR_TAIL_BYTES, cli_error_summary
from harness.runtime import Runtime
from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase


def jsonl(*events):
    return b"".join(json.dumps(event, ensure_ascii=False).encode() + b"\n" for event in events)


class FailedCodexBackend(DemoBackend):
    def __init__(self, cli, raw):
        self.cli = cli
        self.raw = raw
        self.job = None

    def invoke(self, **kwargs):
        if kwargs["role"] != "reviewer":
            return super().invoke(**kwargs)
        self.job = kwargs["job"]
        argv = [sys.executable, "-c",
                "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1])); sys.exit(1)",
                self.raw.hex()]
        with patch.object(self.cli, "command", return_value=argv):
            return self.cli.invoke(**kwargs)


class CodexErrorDiagnosticTests(RepoCase):
    def invoke_failure(self, raw):
        self.config.data["roles"]["reviewer"].update(cli="codex", model="exact-model")
        self.config.data["runtime"]["agent_timeout_seconds"] = 10
        runtime = Runtime(self.config, self.repo)
        backend = FailedCodexBackend(runtime.backend, raw)
        runtime.backend = backend
        with self.assertRaises(HarnessError) as caught:
            runtime.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        job = backend.job
        self.assertIsNotNone(job)
        outcome = read_json(job / "process.outcome.json")
        self.assertEqual((outcome["started"], outcome["termination_reason"], outcome["exit_code"]),
                         (True, "normal", 1))
        self.assertEqual((job / "process.stdout").read_bytes(), raw)
        error = str(caught.exception)
        self.assertIn("reviewer CLI exit=1", error)
        self.assertIn(str(job), error)
        self.assertFalse((job / "response.json").exists())
        self.assertFalse((job / "terminal-capture.json").exists())
        self.assertEqual(getattr(caught.exception, "__notes__", []), [])
        return error

    def test_failed_process_reports_capacity_rate_and_auth_envelopes(self):
        for message in ("Selected model is at capacity. Please try a different model.",
                        "Rate limit exceeded (429)", "Authentication failed (401)"):
            with self.subTest(message=message):
                error = self.invoke_failure(jsonl(
                    {"type": "item.completed", "item": {"text": "PRIVATE_MODEL_TEXT"}},
                    {"type": "error", "message": message, "private": "PRIVATE_EXTRA"},
                    {"type": "turn.failed", "error": {"message": message}}))
                self.assertEqual(error.count(message), 1)
                self.assertNotIn("PRIVATE_", error)

    def test_failed_process_ignores_non_error_payload_and_malformed_records(self):
        raw = b'\xff\n{broken\n' + jsonl(
            {"type": "item.completed", "message": "PRIVATE_MODEL_TEXT",
             "item": {"type": "error", "message": "PRIVATE_TOOL_TEXT"}},
            {"type": "turn.failed", "error": {"message": {"private": "PRIVATE_NESTED"}}},
            {"type": "error", "message": []})
        error = self.invoke_failure(raw)
        self.assertNotIn("PRIVATE_", error)
        self.assertNotIn("Codex reported", error)

    def test_failed_process_escapes_and_bounds_unicode_terminal_text(self):
        message = "capacity\n\r\x00\x1b[31m\x85\u202e" + "😀" * 1000 + "PRIVATE_SUFFIX"
        error = self.invoke_failure(jsonl({"type": "error", "message": message}))
        summary = error.split("Codex reported an error: ", 1)[1].split("；", 1)[0]
        self.assertTrue(all(32 <= ord(char) < 127 for char in summary))
        self.assertIn("[truncated]", summary)
        self.assertNotIn("PRIVATE_SUFFIX", summary)
        self.assertLess(len(summary), 3000)

    def test_tail_reads_late_error_after_large_multibyte_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = Path(temporary)
            path = job / "process.stdout"
            path.write_bytes(jsonl({"type": "item.completed", "text": "界" * 100000}) +
                             jsonl({"type": "error", "message": "at capacity"}))
            self.assertIn("at capacity", cli_error_summary(job, "codex"))
            # Errors outside the tail and suffixes of partial records stay private.
            path.write_bytes(jsonl({"type": "error", "message": "PRIVATE_EARLY"}) +
                             b"x" * (_CODEX_ERROR_TAIL_BYTES + 1))
            self.assertEqual(cli_error_summary(job, "codex"), "")
            path.write_bytes(b"x" * _CODEX_ERROR_TAIL_BYTES +
                             b'{"type":"error","message":"PRIVATE_SUFFIX"}\n')
            self.assertEqual(cli_error_summary(job, "codex"), "")

    def test_only_three_latest_unique_messages_and_missing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = Path(temporary)
            self.assertEqual(cli_error_summary(job, "codex"), "")
            (job / "process.stdout").write_bytes(jsonl(*(
                {"type": "error", "message": f"failure {index}"} for index in range(10))))
            summary = cli_error_summary(job, "codex")
            for index in range(7):
                self.assertNotIn(f"failure {index}", summary)
            for index in range(7, 10):
                self.assertIn(f"failure {index}", summary)
