import tempfile
import unittest
from pathlib import Path

from harness.config import Config
from harness.context import compose
from harness.util import HarnessError, dump


ROOT = Path(__file__).resolve().parents[1]


class RuntimeBudgetDefaultsTests(unittest.TestCase):
    def load_runtime(self, runtime):
        source = (ROOT / "harness.toml").read_text(encoding="utf-8")
        start = source.index("[runtime]")
        end = source.index("[permissions]", start)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "harness.toml"
            path.write_text(source[:start] + runtime + source[end:], encoding="utf-8")
            return Config.load(path)

    def test_omitted_runtime_and_omitted_runtime_keys_use_new_defaults(self):
        for runtime in ("", "[runtime]\nmax_attempts = 4\n\n"):
            with self.subTest(runtime=runtime):
                config = self.load_runtime(runtime)
                self.assertEqual(config.data["runtime"]["context_chars"], 100000)
                self.assertEqual(config.data["runtime"]["agent_timeout_seconds"], 3600)

    def test_checked_in_defaults_and_explicit_overrides_remain_distinct(self):
        config = Config.load(ROOT / "harness.toml")
        self.assertEqual(config.data["runtime"]["context_chars"], 100000)
        self.assertEqual(config.data["runtime"]["agent_timeout_seconds"], 3600)
        override = self.load_runtime(
            "[runtime]\ncontext_chars = 32000\nagent_timeout_seconds = 90\n\n"
        )
        self.assertEqual(override.data["runtime"]["context_chars"], 32000)
        self.assertEqual(override.data["runtime"]["agent_timeout_seconds"], 90)

    def test_explicit_budget_increases_remain_available_on_resume(self):
        previous = self.load_runtime(
            "[runtime]\ncontext_chars = 32000\nagent_timeout_seconds = 1200\n\n"
        )
        current = self.load_runtime("")
        self.assertEqual(current.resume_changes(previous.data), {
            "context_chars": {"old": 32000, "new": 100000},
            "agent_timeout_seconds": {"old": 1200, "new": 3600},
        })


class ContextBudgetAccountingTests(unittest.TestCase):
    def test_required_packet_at_budget_is_lossless_and_omits_optional_memory(self):
        required = {"goal": "Preserve full acceptance obligations", "acceptance": ["完整内容"]}
        core = dump(required)
        packet, report = compose(required, [{"id": "optional", "body": "reference"}], len(core))
        self.assertEqual(packet, core)
        self.assertEqual(report["required_chars"], len(core))
        self.assertEqual(report["memory_chars"], 0)
        self.assertEqual(report["used_chars"], len(packet))
        self.assertEqual(report["memory_ids"], [])
        self.assertEqual(report["omitted_memories"], 1)

    def test_optional_records_are_whole_and_accounting_includes_framing(self):
        required = {"goal": "required"}
        small = {"id": "small", "body": "retrievable knowledge"}
        large = {"id": "large", "body": "x" * 5000}
        full, _ = compose(required, [small], 10000)
        packet, report = compose(required, [large, small], len(full))
        self.assertEqual(packet, full)
        self.assertIn(dump(small), packet)
        self.assertNotIn(dump(large), packet)
        self.assertEqual(report["memory_ids"], ["small"])
        self.assertEqual(report["omitted_memories"], 1)
        self.assertEqual(report["required_chars"], len(dump(required)))
        self.assertEqual(report["memory_chars"], len(packet) - len(dump(required)))
        self.assertGreater(report["memory_chars"], len(dump(small)))
        self.assertEqual(report["used_chars"], report["required_chars"] + report["memory_chars"])

    def test_required_overflow_still_fails_even_without_optional_memory(self):
        required = {"acceptance": ["x" * 200]}
        with self.assertRaises(HarnessError):
            compose(required, [], len(dump(required)) - 1)
