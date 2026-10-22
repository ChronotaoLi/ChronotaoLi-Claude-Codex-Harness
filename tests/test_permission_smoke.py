"""Offline tests of the probe's evidence gate; these do not invoke a model."""
import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.config import Config
from harness.permission_smoke import inspect_evidence, permission_smoke, prepare_fixture
from harness.util import HarnessError


ROOT = Path(__file__).resolve().parents[1]


class PermissionSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fixture = prepare_fixture(self.root / ".harness" / "fixture")
        self.stream = self.root / "stream.jsonl"

    def events(self):
        events, denials = [], []
        for index, case in enumerate(self.fixture["cases"]):
            identity = f"tool-{index}"
            events.append({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": identity, "name": case["tool"], "input": case["input"]}]}})
            events.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": identity, "is_error": not case["allowed"],
                 "content": case.get("contains", "ok" if case["allowed"] else "permission denied")}]}})
            if not case["allowed"]:
                denials.append({"tool_use_id": identity, "tool_name": case["tool"], "tool_input": case["input"]})
        events.append({"type": "result", "subtype": "success", "is_error": False,
                       "permission_denials": denials, "total_cost_usd": 0.1, "num_turns": 5})
        return copy.deepcopy(events)

    def apply_controls(self):
        for name, expected in self.fixture["expected_files"].items():
            if expected is not None:
                Path(name).write_text(expected, encoding="utf-8")

    def inspect(self, events):
        self.stream.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        return inspect_evidence(self.stream, self.fixture)

    def test_complete_provider_evidence_and_files_pass(self):
        self.apply_controls()
        result = self.inspect(self.events())
        self.assertTrue(result["passed"], result)

    def test_prose_claims_and_unchanged_sentinels_do_not_prove_permissions(self):
        self.apply_controls()
        result = self.inspect([{"type": "assistant", "message": {"content": [
            {"type": "text", "text": "All forbidden operations were denied."}]}}, self.events()[-1]])
        self.assertFalse(result["passed"])
        self.assertTrue(all(not check["passed"] for check in result["checks"] if "expected" in check))

    def test_tool_error_without_provider_permission_denial_fails(self):
        self.apply_controls()
        events = self.events()
        events[-1]["permission_denials"] = []
        self.assertFalse(self.inspect(events)["passed"])

    def test_denial_must_match_actual_tool_identity_and_input(self):
        self.apply_controls()
        for field, value in (("tool_use_id", "unrelated"), ("tool_name", "Other"), ("tool_input", {})):
            with self.subTest(field=field):
                events = self.events()
                events[-1]["permission_denials"][0][field] = value
                self.assertFalse(self.inspect(events)["passed"])

    def test_allowed_controls_must_execute_and_write_expected_bytes(self):
        events = self.events()
        self.assertFalse(self.inspect(events)["passed"])
        self.apply_controls()
        events[1]["message"]["content"][0]["content"] = "claimed read succeeded without actual content"
        self.assertFalse(self.inspect(events)["passed"])

    def test_denied_attempt_cannot_hide_successful_attempt_of_same_operation(self):
        self.apply_controls()
        events = self.events()
        index = next(i for i, case in enumerate(self.fixture["cases"]) if not case["allowed"])
        call, outcome = copy.deepcopy(events[index * 2:index * 2 + 2])
        call["message"]["content"][0]["id"] = "unexpected-success"
        outcome["message"]["content"][0].update(tool_use_id="unexpected-success", is_error=False)
        events[0:0] = [call, outcome]
        self.assertFalse(self.inspect(events)["passed"])

    def test_changed_external_sentinel_or_git_binding_fails(self):
        self.apply_controls()
        names = [name for name in self.fixture["expected_files"] if name.endswith(("outside.txt", "/.git"))]
        for name in names:
            with self.subTest(path=name):
                previous = Path(name).read_bytes() if Path(name).exists() else None
                Path(name).write_text("changed", encoding="utf-8")
                self.assertFalse(self.inspect(self.events())["passed"])
                if previous is None:
                    Path(name).unlink()
                else:
                    Path(name).write_bytes(previous)

    def test_unrequested_tools_or_failed_provider_result_fail(self):
        self.apply_controls()
        events = self.events()
        events[-1]["subtype"] = "error_max_turns"
        self.assertFalse(self.inspect(events)["passed"])
        events = self.events()
        events.insert(0, {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "extra", "name": "Bash", "input": {"command": "echo extra"}}]}})
        self.assertFalse(self.inspect(events)["passed"])

    def test_missing_or_malformed_stream_is_diagnostic(self):
        with self.assertRaises(HarnessError):
            inspect_evidence(self.stream, self.fixture)
        self.stream.write_text("not json", encoding="utf-8")
        with self.assertRaises(HarnessError):
            inspect_evidence(self.stream, self.fixture)
        with self.assertRaises(HarnessError):
            self.inspect(self.events()[:-1])

    def test_probe_keeps_model_id_and_bounds_calls_without_project_commands(self):
        config = Config.load(ROOT / "harness.toml")
        config.data["roles"]["worker"]["model"] = "exact-configured-model-id"
        config.data["permissions"]["worker_commands"] = [["project-side-effect"]]
        original = copy.deepcopy(config.data)
        seen = []

        def process(argv, cwd, log, **kwargs):
            seen.append(argv)
            self.assertEqual(argv[argv.index("--model") + 1], "exact-configured-model-id")
            self.assertNotIn("--resume", argv)
            self.assertNotIn("--fallback-model", argv)
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(argv[argv.index("--max-turns") + 1], "16")
            self.assertIn("--no-session-persistence", argv)
            self.assertNotIn("project-side-effect", " ".join(argv))
            self.assertEqual(kwargs["timeout"], 180)
            log.with_suffix(".stdout").write_text(json.dumps({
                "type": "result", "subtype": "success", "permission_denials": []}), encoding="utf-8")
            return 0

        with patch("harness.permission_smoke.run_process", side_effect=process), contextlib.redirect_stderr(io.StringIO()):
            report = permission_smoke(config, self.root)
        self.assertEqual(len(seen), 1)
        self.assertFalse(report["passed"])
        self.assertTrue((Path(report["evidence"]) / "report.json").is_file())
        self.assertEqual(config.data, original)


if __name__ == "__main__":
    unittest.main()
