from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from harness import git
from harness.adapters import DemoBackend
from harness.config import Config
from harness.shared import mcp_configs, toml_value, validate_mcp
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase


class MCPConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "mcp.json"
        self.state = self.root / ".harness"
        self.config = Config.load(ROOT / "harness.toml")
        self.config.data["shared"]["mcp"] = str(self.path)

    def servers(self, servers):
        write_json(self.path, {"servers": servers})

    def test_bad_json_and_containers_raise_harness_error_before_generation(self):
        for value, field in (([], "MCP"), (None, "MCP"), ({"servers": []}, "servers"),
                             ({"servers": {"svc": []}}, "servers.svc")):
            with self.subTest(value=value):
                write_json(self.path, value)
                with self.assertRaises(HarnessError) as raised:
                    mcp_configs(self.config, self.state, "worker")
                self.assertIn(field, str(raised.exception))
                self.assertFalse(self.state.exists())
        self.path.write_text("{not json}", encoding="utf-8")
        with self.assertRaises(HarnessError) as raised:
            validate_mcp(self.config)
        self.assertIn(str(self.path), str(raised.exception))

    def test_missing_connection_fields_identify_server_and_field(self):
        for spec, field in (({}, "transport"), ({"transport": "stdio"}, "command"),
                            ({"transport": "http"}, "url")):
            with self.subTest(field=field):
                self.servers({"svc": spec})
                with self.assertRaises(HarnessError) as raised:
                    validate_mcp(self.config)
                self.assertIn("servers.svc." + field, str(raised.exception))

    def test_active_field_types_are_validated(self):
        stdio = {"transport": "stdio", "command": "example-mcp"}
        http = {"transport": "http", "url": "https://mcp.example.test"}
        cases = [
            (stdio, "enabled", "false"), (stdio, "roles", "worker"),
            (stdio, "roles", ["unknown"]), (stdio, "roles", [{}]),
            (stdio, "read_only", "true"), (stdio, "transport", []),
            (stdio, "command", 7), (stdio, "command", "  "),
            (stdio, "args", "--version"), (stdio, "args", [None]),
            (stdio, "args", ["a\0b"]), (stdio, "env_vars", "TOKEN"),
            (stdio, "env_vars", [7]), (stdio, "env_vars", [""]),
            (http, "url", []), (http, "url", " "),
            (http, "bearer_token_env_var", 1), (http, "bearer_token_env_var", ""),
        ]
        for base, field, value in cases:
            with self.subTest(field=field, value=value):
                self.servers({"svc": {**base, field: value}})
                with self.assertRaises(HarnessError) as raised:
                    mcp_configs(self.config, self.state, "worker")
                self.assertIn("servers.svc." + field, str(raised.exception))

    def test_valid_stdio_http_are_shared_and_preserve_role_visibility(self):
        self.servers({
            "writer": {"transport": "stdio", "command": "/tools/example-mcp",
                       "args": ["--mode", "", "worker"], "env_vars": ["MCP_KEY"]},
            "reader": {"transport": "http", "url": "https://mcp.example.test",
                       "roles": ["commander", "reviewer"], "read_only": True,
                       "bearer_token_env_var": "MCP_TOKEN"},
        })
        self.assertEqual(set(validate_mcp(self.config)), {"writer", "reader"})
        for role in ("worker", "commander", "reviewer"):
            with self.subTest(role=role):
                claude, codex = mcp_configs(self.config, self.state, role)
                expected = "writer" if role == "worker" else "reader"
                self.assertEqual(set(claude["mcpServers"]), {"harness", expected})
                self.assertEqual(set(codex), {"harness", expected})
                self.assertEqual(tomllib.loads("mcp_servers = " + toml_value(codex))["mcp_servers"], codex)
                self.assertTrue(codex[expected]["required"])
                if role == "worker":
                    self.assertEqual(claude["mcpServers"][expected]["env"], {"MCP_KEY": "${MCP_KEY}"})
                    self.assertEqual(codex[expected]["env_vars"], ["MCP_KEY"])
                    self.assertEqual(codex[expected]["args"], ["--mode", "", "worker"])
                else:
                    self.assertEqual(claude["mcpServers"][expected]["headers"],
                                     {"Authorization": "Bearer ${MCP_TOKEN}"})
                    self.assertEqual(codex[expected]["bearer_token_env_var"], "MCP_TOKEN")

    def test_disabled_templates_and_hidden_servers_do_not_require_connection_fields(self):
        self.servers({"template": {"enabled": False}, "unassigned": {"roles": []},
                      "reviewer_only": {"roles": ["reviewer"], "read_only": True}})
        claude, codex = mcp_configs(self.config, self.state, "worker")
        self.assertEqual(set(claude["mcpServers"]), {"harness"})
        self.assertEqual(set(codex), {"harness"})
        with self.assertRaises(HarnessError) as raised:
            validate_mcp(self.config)
        self.assertIn("servers.reviewer_only.transport", str(raised.exception))

    def test_non_worker_servers_require_true_read_only_declaration(self):
        for value in (False, "true", 1):
            with self.subTest(read_only=value):
                self.servers({"svc": {"roles": ["worker", "reviewer"], "transport": "stdio",
                                      "command": "example-mcp", "read_only": value}})
                with self.assertRaises(HarnessError) as raised:
                    validate_mcp(self.config)
                self.assertIn("servers.svc.read_only", str(raised.exception))


class MCPPreflightLifecycleTests(RepoCase):
    def mcp_path(self):
        path = self.repo / ".harness" / "test-mcp.json"
        self.config.data["shared"]["mcp"] = str(path)
        return path

    def test_bad_worker_mcp_prevents_run_and_branch_creation(self):
        path = self.mcp_path()
        write_json(path, {"servers": {"worker_service": {"transport": "stdio"}}})
        backend = DemoBackend()
        engine = self.engine(backend)
        branches = git.git(self.repo, "branch", "--list")
        with patch.object(backend, "invoke", wraps=backend.invoke) as invoke:
            with self.assertRaises(HarnessError) as raised:
                engine.start("Implement greet")
        self.assertIn("servers.worker_service.command", str(raised.exception))
        invoke.assert_not_called()
        self.assertIsNone(engine.run)
        self.assertFalse((engine.state / "runs").exists())
        self.assertFalse((engine.state / "worktrees").exists())
        self.assertEqual(git.git(self.repo, "branch", "--list"), branches)

    def test_dispatch_preflight_failure_preserves_worker_budget_and_can_recover(self):
        path = self.mcp_path()
        write_json(path, {"servers": {}})
        original = path.read_bytes()
        backend = DemoBackend()
        engine = self.engine(backend)
        original_emit = engine.hooks.emit

        def corrupt_mcp(event, payload, logs):
            if event == "pre_agent" and payload["role"] == "worker":
                write_json(path, {"servers": {"worker_service": {"transport": "http"}}})
            return original_emit(event, payload, logs)

        with patch.object(engine.hooks, "emit", side_effect=corrupt_mcp), \
                patch.object(backend, "invoke", wraps=backend.invoke) as invoke:
            with self.assertRaises(HarnessError) as raised:
                engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertIn("servers.worker_service.url", str(raised.exception))
        invoke.assert_not_called()
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["total_attempts"], 0)
        self.assertEqual(saved["attempts"], {})
        self.assertEqual(saved["agent_dispatches"], 0)
        self.assertEqual(len(saved["dispatches"]), 1)
        self.assertEqual(saved["dispatches"][0]["status"], "failed")
        self.assertTrue(saved["dispatches"][0]["exit_confirmed"])
        path.write_bytes(original)
        result = self.engine().resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 1})
        self.assertEqual(result["agent_dispatches"], 3)


if __name__ == "__main__":
    unittest.main()
