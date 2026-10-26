import copy
import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.adapters import CLIBackend
from harness.config import Config, RESUME_INCREASES
from harness.contracts import REVIEW, WORK, envelope, validate
from harness.shared import toml_value
from harness.util import HarnessError, read_json, write_json


ROOT = Path(__file__).resolve().parents[1]


class ConfigChangesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = Config.load(ROOT / "harness.toml").data
        self.data["shared"] = {key: str(ROOT / path) for key, path in self.data["shared"].items()}

    def load(self, data=None):
        data = self.data if data is None else data
        path = self.root / "harness.toml"
        path.write_text("\n".join(f"{key} = {toml_value(value)}" for key, value in data.items()))
        return Config.load(path)

    def test_existing_configuration_gets_bounded_defaults(self):
        del self.data["permissions"]
        del self.data["memory"]
        del self.data["runtime"]["max_total_attempts"]
        del self.data["runtime"]["max_replans"]
        config = self.load()
        self.assertEqual(config.data["runtime"]["max_total_attempts"], 60)
        self.assertEqual(config.data["runtime"]["max_replans"], 2)
        self.assertEqual(config.data["permissions"]["worker_commands"], [])
        self.assertEqual(config.data["memory"], {"task_ttl_days": 180, "knowledge_ttl_days": 180})
        self.assertEqual(config.resume_changes(self.data), {})

    def test_rejects_unsupported_codex_worker_before_dispatch(self):
        self.data["roles"]["worker"]["cli"] = "codex"
        with self.assertRaisesRegex(HarnessError, "不能配置为 worker"):
            self.load()

    def test_runtime_budgets_reject_boolean_zero_and_negative_values(self):
        for key in self.data["runtime"]:
            if key == "claude_max_turns_by_role":
                continue
            for value in (True, False, -1, 0):
                if key == "max_replans" and type(value) is int and value == 0:
                    continue
                data = copy.deepcopy(self.data)
                data["runtime"][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(HarnessError):
                    self.load(data)
        self.data["runtime"]["max_replans"] = 0
        self.assertEqual(self.load().data["runtime"]["max_replans"], 0)

    def test_memory_ttl_allows_disabled_expiry_but_rejects_invalid_values(self):
        self.data["memory"] = {"task_ttl_days": 0, "knowledge_ttl_days": 0}
        self.assertEqual(self.load().data["memory"]["task_ttl_days"], 0)
        for value in (-1, True, "180"):
            self.data["memory"]["knowledge_ttl_days"] = value
            with self.subTest(value=value), self.assertRaises(HarnessError):
                self.load()

    def test_worker_permissions_require_exact_command_arrays(self):
        for commands in ("npm test", ["npm test"], [[]], [[""]], [["npm", 7]], [["echo", "bad\0arg"]]):
            self.data["permissions"]["worker_commands"] = commands
            with self.subTest(commands=commands), self.assertRaises(HarnessError):
                self.load()
        self.data["permissions"]["worker_commands"] = [["npm", "run", "build"]]
        self.assertEqual(self.load().data["permissions"]["worker_commands"], [["npm", "run", "build"]])

    def test_worker_commands_preserve_literal_star_and_line_breaks(self):
        commands = [["echo", "*", "a\nb", "a\rb"]]
        self.data["permissions"]["worker_commands"] = commands
        self.assertEqual(self.load().data["permissions"]["worker_commands"], commands)

    def test_resume_returns_only_increases_and_preserves_saved_configuration(self):
        config = self.load()
        snapshot = copy.deepcopy(config.data)
        original = copy.deepcopy(snapshot)
        for key in RESUME_INCREASES:
            config.data["runtime"][key] += 1
        self.assertEqual(config.resume_changes(snapshot), {
            key: {"old": original["runtime"][key], "new": original["runtime"][key] + 1}
            for key in RESUME_INCREASES
        })
        self.assertEqual(snapshot, original)
        self.assertEqual(config.data["runtime"]["max_attempts"], original["runtime"]["max_attempts"] + 1)

    def test_resume_rejects_lowering_any_allowlisted_budget(self):
        for key in RESUME_INCREASES:
            config = self.load()
            snapshot = copy.deepcopy(config.data)
            config.data["runtime"][key] -= 1
            with self.subTest(key=key), self.assertRaisesRegex(HarnessError, "只能保持或增大"):
                config.resume_changes(snapshot)

    def test_resume_rejects_model_acceptance_and_other_fixed_config_changes(self):
        mutations = [
            ("roles", "reviewer", "model", "configured-other-model"),
            ("roles", "reviewer", "effort", "low"),
            ("verification", "commands", [["true"]]),
            ("shared", "rules", "different-rules.md"),
            ("memory", "task_ttl_days", 365),
        ]
        for *keys, value in mutations:
            config = self.load()
            snapshot = copy.deepcopy(config.data)
            target = config.data
            for key in keys[:-1]:
                target = target[key]
            target[keys[-1]] = value
            with self.subTest(keys=keys), self.assertRaisesRegex(HarnessError, "配置已改变"):
                config.resume_changes(snapshot)

    def command(self, config, role, model=None):
        job = self.root / "dispatch"
        job.mkdir(exist_ok=True)
        argv = CLIBackend(config, self.root / ".harness").command(
            role, model or config.role("commander", "fable"), self.root, job, {}, None)
        settings = json.loads(argv[argv.index("--settings") + 1])
        return argv, settings["permissions"]["allow"]

    def test_claude_readonly_roles_get_constrained_inspection_without_worker_commands(self):
        self.data["permissions"]["worker_commands"] = [["npm", "run", "build"]]
        config = self.load()
        for role in ("commander", "reviewer"):
            argv, allow = self.command(config, role)
            with self.subTest(role=role):
                self.assertIn("Bash", argv[argv.index("--tools") + 1].split(","))
                self.assertNotIn("Edit", allow)
                self.assertNotIn("Write", allow)
                self.assertNotIn("Bash(npm run build)", allow)
                self.assertIn("Bash(git --no-pager diff --no-ext-diff --no-textconv -- *)", allow)
                self.assertIn("Bash(git --no-pager show --no-ext-diff --no-textconv HEAD -- *)", allow)
                self.assertIn("Bash(rg -- *)", allow)
                self.assertIn("Bash(wc -- *)", allow)
                self.assertFalse(any(rule.startswith("Bash(find") for rule in allow))
                self.assertNotIn("Bash", allow)
                self.assertNotIn("Bash(git *)", allow)
                self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")

    def test_worker_extra_commands_are_exact_and_keep_quoted_arguments(self):
        extra = ["python3", "-c", "print('comma, paren() and $(literal)')"]
        self.data["permissions"]["worker_commands"] = [extra]
        config = self.load()
        argv, allow = self.command(config, "worker", config.role("worker"))
        self.assertIn("Bash(" + shlex.join(extra) + ")", allow)
        self.assertNotIn("Bash(python3 *)", allow)
        self.assertIn("Edit(/" + self.root.resolve().as_posix() + "/**)", allow)
        self.assertNotIn("Edit", allow)
        self.assertNotIn("Write", allow)
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Agent,Task")

    def test_verification_literal_star_never_becomes_wildcard_permission(self):
        self.data["verification"]["commands"] = [["python3", "-c", "print(2 * 3)"]]
        config = self.load()
        _, allow = self.command(config, "worker", config.role("worker"))
        self.assertFalse(any(rule.startswith("Bash(python3") for rule in allow))
        self.assertEqual(config.data["verification"]["commands"], [["python3", "-c", "print(2 * 3)"]])

    def test_adapter_keeps_exact_model_and_fresh_process_options(self):
        config = self.load()
        for cli in ("claude", "codex"):
            model = {"cli": cli, "model": "exact-configured-model-v7", "effort": "high"}
            job = self.root / cli
            job.mkdir()
            argv = CLIBackend(config, self.root / ".harness").command("reviewer", model, self.root, job, {}, None)
            with self.subTest(cli=cli):
                self.assertEqual(argv[argv.index("--model") + 1], model["model"])
                self.assertIn("--no-session-persistence" if cli == "claude" else "--ephemeral", argv)
                self.assertFalse(set(argv) & {"--resume", "--continue", "--fallback-model", "--dangerously-skip-permissions"})

    def test_adapter_rejects_codex_worker_even_if_config_was_mutated(self):
        config = self.load()
        config.data["roles"]["worker"]["cli"] = "codex"
        job = self.root / "missing-job"
        with self.assertRaisesRegex(HarnessError, "不能配置为 worker"):
            CLIBackend(config, self.root / ".harness").command("worker", config.role("worker"), self.root, job, {}, None)
        self.assertFalse(job.exists())

    def test_readonly_adapters_disable_automatic_project_instruction_loading(self):
        config = self.load()
        for cli in ("codex", "claude"):
            for role in ("commander", "reviewer"):
                job = self.root / f"{cli}-{role}"
                job.mkdir()
                model = {"cli": cli, "model": "exact-configured-model-v7", "effort": "high"}
                argv = CLIBackend(config, self.root / ".harness").command(
                    role, model, self.root, job, {}, None)
                with self.subTest(cli=cli, role=role):
                    if cli == "codex":
                        overrides = [argv[index + 1] for index, arg in enumerate(argv) if arg == "-c"]
                        self.assertIn("project_doc_max_bytes=0", overrides)
                        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
                    else:
                        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")

    def test_provider_projection_preserves_identity_and_structure_with_local_bounds(self):
        config = self.load()
        schema = envelope(REVIEW, "task-exact", "dispatch-exact")
        original = copy.deepcopy(schema)
        core = {"type", "properties", "required", "additionalProperties", "items", "enum"}

        def check_core(local, wire):
            self.assertLessEqual(set(wire), core)
            for key in ("type", "required", "additionalProperties", "enum"):
                if key in local:
                    self.assertEqual(wire[key], local[key])
            if "properties" in local:
                self.assertEqual(set(wire["properties"]), set(local["properties"]))
                for key in local["properties"]:
                    check_core(local["properties"][key], wire["properties"][key])
            if "items" in local:
                check_core(local["items"], wire["items"])

        for cli in ("claude", "codex"):
            job = self.root / cli
            job.mkdir()
            argv = CLIBackend(config, self.root / ".harness").command(
                "reviewer", {"cli": cli, "model": "exact-model", "effort": "high"},
                self.root, job, schema, None)
            if cli == "claude":
                wire = json.loads(argv[argv.index("--json-schema") + 1])
            else:
                wire = read_json(Path(argv[argv.index("--output-schema") + 1]))
            with self.subTest(cli=cli):
                check_core(schema, wire)
                self.assertEqual(read_json(job / "provider-schema.json"), wire)
                self.assertEqual(schema, original)
        duplicate_keys = {"task_id": "task-exact", "dispatch_id": "dispatch-exact",
                          "verdict": "approve", "summary": "Checked sources", "findings": [],
                          "approved_memory_keys": ["same", "same"]}
        with self.assertRaisesRegex(HarnessError, "duplicate entries"):
            validate(duplicate_keys, schema)
        proposal = {"key": "storage", "abstract": "Storage", "content": "SQLite", "source_paths": ["store.py"]}
        oversized = {"status": "completed", "summary": "Implemented", "evidence": [], "blockers": [],
                     "blocker_kind": "none", "proposed_memories": [proposal] * 9}
        with self.assertRaisesRegex(HarnessError, "array length"):
            validate(oversized, WORK)

    def test_invocation_retains_full_local_schema_beside_provider_projection(self):
        config = self.load()
        job = self.root / "invoke"
        job.mkdir()
        schema = envelope(REVIEW, "task-exact", "dispatch-exact")
        response = {"task_id": "task-exact", "dispatch_id": "dispatch-exact",
                    "verdict": "approve", "summary": "Offline adapter test", "findings": [],
                    "approved_memory_keys": []}

        def simulated_process(*args, **kwargs):
            write_json(job / "response.json", response)
            return 0

        with patch("harness.adapters.run_process", side_effect=simulated_process):
            value = CLIBackend(config, self.root / ".harness").invoke(
                role="reviewer", model=config.role("reviewer"), cwd=self.root, job=job,
                schema=schema, prompt="Offline test", run_path=None, on_start=lambda _: None)
        self.assertEqual(value, response)
        self.assertEqual(read_json(job / "schema.json"), schema)
        self.assertTrue(read_json(job / "schema.json")["properties"]["approved_memory_keys"]["uniqueItems"])
        self.assertNotIn("uniqueItems", read_json(job / "provider-schema.json")["properties"]["approved_memory_keys"])


if __name__ == "__main__":
    unittest.main()
