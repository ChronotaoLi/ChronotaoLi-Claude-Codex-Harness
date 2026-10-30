"""Offline failed-process fixtures; no provider accounts are invoked."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness.adapters import CLIBackend, _CODEX_ERROR_TAIL_BYTES, cli_error_summary
from harness.util import HarnessError


def jsonl(*events):
    return b"".join(json.dumps(event, ensure_ascii=False).encode() + b"\n" for event in events)


class FailedCodexBackend(CLIBackend):
    def __init__(self, raw):
        super().__init__(SimpleNamespace(data={"runtime": {"agent_timeout_seconds": 10}}), None)
        self.raw = raw

    def command(self, *args):
        return [sys.executable, "-c",
                "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1])); sys.exit(1)",
                self.raw.hex()]


class CodexErrorDiagnosticTests(unittest.TestCase):
    def invoke_failure(self, raw):
        with tempfile.TemporaryDirectory() as temporary:
            job = Path(temporary)
            backend = FailedCodexBackend(raw)
            with self.assertRaises(HarnessError) as caught:
                backend.invoke(role="reviewer", model={"cli": "codex", "model": "exact-model"},
                               cwd=job, job=job, schema={}, prompt="fixture", run_path=job,
                               on_start=None)
            self.assertEqual((job / "process.stdout").read_bytes(), raw)
            error = str(caught.exception)
            self.assertIn("reviewer CLI exit=1", error)
            self.assertIn(str(job), error)
            self.assertFalse((job / "response.json").exists())
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
