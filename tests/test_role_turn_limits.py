"""Per-dispatch capacity preserves recovery accounting and acceptance gates."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.adapters import CLIBackend
from harness.config import Config
from harness.permission_smoke import permission_smoke
from harness.tool_smoke import tool_smoke
from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_provider_usage import FIXTURE_PROCESS, FixtureCLIBackend


class RoleTurnLimitTests(unittest.TestCase):
    def config(self):
        config = Config.load(ROOT / "harness.toml")
        config.data["runtime"].pop("claude_max_turns_by_role", None)
        return config

    def test_legacy_fallback_and_actual_role_routing_keep_exact_models(self):
        config = self.config()
        config.data["runtime"]["claude_max_turns"] = 73
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for index, overrides in enumerate(({}, {"worker": 210, "commander": 80, "reviewer": 120})):
                config.data["runtime"]["claude_max_turns_by_role"] = overrides
                for role in ("worker", "commander", "reviewer"):
                    job = root / f"{index}-{role}"
                    job.mkdir()
                    model = config.role("commander", "fable") if role == "commander" else {
                        "cli": "claude", "model": "exact-" + role, "effort": "high"}
                    argv = CLIBackend(config, root / "state").command(role, model, root, job, {}, None)
                    self.assertEqual(int(argv[argv.index("--max-turns") + 1]), overrides.get(role, 73))
                    self.assertEqual(argv[argv.index("--model") + 1], model["model"])
                    self.assertIn("--no-session-persistence", argv)
                    self.assertFalse(set(argv) & {"--resume", "--continue", "--fallback-model"})
            argv = CLIBackend(config, root / "state").command(
                "reviewer", config.role("reviewer"), root, job, {}, None)
            self.assertNotIn("--max-turns", argv)

    def test_invalid_role_limits_fail_before_dispatch(self):
        for limits in (None, [], 100, {"fable": 100}, {"workre": 100},
                       *({"worker": value} for value in (True, False, 0, -1, 1.5, "100"))):
            config = self.config()
            config.data["runtime"]["claude_max_turns_by_role"] = limits
            with self.subTest(limits=limits), self.assertRaisesRegex(HarnessError, "claude_max_turns_by_role"):
                Config.validate(config.data)

    def test_resume_compares_effective_limits_without_mutating_inputs(self):
        cases = [
            (50, {}, 50, {"worker": 200}, True),
            (50, {"worker": 200}, 50, {"worker": 250}, True),
            (50, {"worker": 200}, 200, {}, True),
            (50, {"worker": 50}, 50, {}, True),
            (50, {"worker": 200}, 50, {}, False),
            (50, {"worker": 200}, 100, {}, False),
            (50, {"worker": 200}, 250, {"worker": 199}, False),
            (100, {}, 100, {"reviewer": 99}, False),
            (100, {}, 100, {"commander": 99}, False),
        ]
        for old_default, old_roles, new_default, new_roles, allowed in cases:
            config = self.config()
            config.data["runtime"].update(claude_max_turns=old_default,
                                          claude_max_turns_by_role=old_roles)
            snapshot = copy.deepcopy(config.data)
            config.data["runtime"].update(claude_max_turns=new_default,
                                          claude_max_turns_by_role=new_roles)
            before = copy.deepcopy((snapshot, config.data))
            with self.subTest(case=(old_default, old_roles, new_default, new_roles)):
                if allowed:
                    changes = config.resume_changes(snapshot)
                    if old_roles != new_roles:
                        self.assertEqual(changes["claude_max_turns_by_role"],
                                         {"old": old_roles, "new": new_roles})
                else:
                    with self.assertRaisesRegex(HarnessError, "只能保持或增大"):
                        config.resume_changes(snapshot)
                self.assertEqual((snapshot, config.data), before)

    def test_old_snapshot_without_overrides_preserves_fallback(self):
        config = self.config()
        snapshot = copy.deepcopy(config.data)
        self.assertEqual(config.resume_changes(snapshot), {})
        config.data["runtime"]["claude_max_turns_by_role"] = {"worker": 200}
        self.assertEqual(config.resume_changes(snapshot), {
            "claude_max_turns_by_role": {"old": {}, "new": {"worker": 200}}})
        self.assertNotIn("claude_max_turns_by_role", snapshot["runtime"])

    def test_smoke_explicit_limit_overrides_project_profile_without_calling_provider(self):
        for probe, module in ((permission_smoke, "harness.permission_smoke"),
                              (tool_smoke, "harness.tool_smoke")):
            config = self.config()
            config.data["runtime"]["claude_max_turns_by_role"] = {"worker": 500}
            before = copy.deepcopy(config.data)
            observed = []

            def fail_locally(argv, cwd, output, **kwargs):
                observed.append(argv)
                raise HarnessError("simulated local startup failure")

            with self.subTest(probe=module), tempfile.TemporaryDirectory() as tmp:
                with patch(module + ".run_process", side_effect=fail_locally), self.assertRaises(HarnessError):
                    probe(config, tmp, max_turns=7)
                self.assertEqual(len(observed), 1)
                self.assertEqual(observed[0][observed[0].index("--max-turns") + 1], "7")
                self.assertEqual(config.data, before)


class BudgetFixtureBackend(FixtureCLIBackend):
    """Generate real adapter arguments, then execute only local fixture processes."""
    def __init__(self, config, state, options=None):
        super().__init__(config, state, options)
        self.adapter_calls = []

    def command(self, role, model, cwd, job, schema, run_path):
        argv = CLIBackend.command(self, role, model, cwd, job, schema, run_path)
        self.adapter_calls.append({"role": role, "dispatch_id": job.name, "argv": argv})
        if role != "commander":
            return super().command(role, model, cwd, job, schema, run_path)
        result = {key: schema["properties"][key]["enum"][0] for key in ("task_id", "dispatch_id")}
        result.update(selected_task="greet", action="implement", instruction="Finish the original task")
        return [sys.executable, "-c", FIXTURE_PROCESS, role, model["cli"], str(job), "{}", json.dumps(result)]


class RoleTurnLimitRecoveryTests(RepoCase):
    def test_exhaustion_then_role_increase_keeps_counts_evidence_and_fresh_dispatch(self):
        # An old run has no role table. Its existing scalar still applies.
        self.config.data["runtime"].pop("claude_max_turns_by_role", None)
        self.config.data["runtime"]["claude_max_turns"] = 100
        backend = BudgetFixtureBackend(self.config, self.repo / ".harness", {
            "worker": {"exit_code": 1, "envelope": {"subtype": "error_max_turns", "is_error": True,
                         "errors": ["Reached maximum number of turns (100)"], "num_turns": 101}}})
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "error_max_turns"):
            engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        first = engine.run["dispatches"][-1]
        artifact = engine.directory / "dispatches" / first["id"] / "process.stdout"
        original = artifact.read_bytes()
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(engine.run["attempts"], {"greet": 1})
        self.assertNotIn("attempt_exemption", first)
        self.assertEqual(len(backend.adapter_calls), 1)
        self.config.data["runtime"]["claude_max_turns_by_role"] = {"worker": 200}
        backend.options = {"worker": {"envelope": {"num_turns": 256}}}
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(result["total_attempts"], 2)
        self.assertEqual(result["agent_dispatches"], 5)
        self.assertEqual(artifact.read_bytes(), original)
        workers = [call for call in backend.adapter_calls if call["role"] == "worker"]
        self.assertEqual([call["argv"][call["argv"].index("--max-turns") + 1] for call in workers], ["100", "200"])
        self.assertNotEqual(workers[0]["dispatch_id"], workers[1]["dispatch_id"])
        self.assertEqual(len({entry["pgid"] for entry in result["dispatches"] if entry["role"] == "worker"}), 2)
        self.assertEqual(len(result["review_history"]["greet"]), 1)
        self.assertEqual(result["final_history"][-1]["status"], "approved")
        for call in workers:
            self.assertEqual(call["argv"][call["argv"].index("--model") + 1], self.config.role("worker")["model"])

    def test_effective_decrease_rejects_resume_before_dispatch_and_preserves_ledger(self):
        self.config.data["runtime"]["claude_max_turns_by_role"] = {"worker": 200}
        backend = BudgetFixtureBackend(self.config, self.repo / ".harness")
        engine = self.engine(backend)
        engine.start("Implement greet", read_json(ROOT / "examples/plan.json"), pause_after_plan=True)
        state_path = engine.directory / "state.json"
        before = state_path.read_bytes()
        self.config.data["runtime"]["claude_max_turns_by_role"] = {}
        with self.assertRaisesRegex(HarnessError, "只能保持或增大"):
            self.engine(backend).resume(engine.run["id"], approve_plan=True)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(backend.adapter_calls, [])
