"""Built-in git_query coverage at the four exact-argv gates, exercised through run lifecycles."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

from harness import git
from harness.continuation import continuation_gate
from harness.permission_diagnostics import command_capability_projection, permission_diagnostics
from harness.requirements import missing_commands
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend

COVERED = ["git", "ls-files", "-z", "--", "x"]
UNLISTED = ["git", "ls-files", "--output=y", "--", "x"]


def events(engine):
    path = engine.directory / "events.jsonl"
    return [json.loads(line)["event"] for line in path.read_text().splitlines() if line.strip()]


class GitQueryCoverageLifecycleTests(RepoCase):
    def plan(self, **fields):
        plan = read_json(ROOT / "examples" / "plan.json")
        plan["tasks"][0].update(copy.deepcopy(fields))
        return plan

    def test_plan_prerequisite_in_table_form_needs_no_grant(self):
        self.assertEqual(self.config.data["permissions"]["worker_commands"], [])
        backend = ScriptedBackend()
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan(required_commands=[COVERED]))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["command_requirements"]["missing"], {})
        self.assertNotIn("run_stopped", events(engine))
        self.assertEqual(self.config.data["permissions"]["worker_commands"], [])
        workers = [call for call in backend.calls if call["role"] == "worker"]
        self.assertTrue(workers)
        for call in backend.calls:
            tools = call["packet"].get("tool_constraints", {}).get("file_tools", [])
            # Only the role that runs queries gets the prompt entry, exactly once.
            self.assertEqual(tools.count("mcp__harness__git_query"), int(call["role"] == "worker"))

    def test_self_check_in_table_form_needs_no_grant(self):
        engine = self.engine(ScriptedBackend())
        result = engine.start("Implement greet", self.plan(self_checks=[["git", "rev-parse", "--verify", "HEAD"]]))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["command_requirements"]["missing"], {})

    def test_out_of_table_prerequisite_still_blocks_only_itself(self):
        for field, unlisted in (("required_commands", UNLISTED), ("self_checks", ["sh", "-c", "git ls-files | head"])):
            with self.subTest(field=field):
                backend = ScriptedBackend()
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start("Implement greet", self.plan(**{field: [COVERED, unlisted]}))
                saved = read_json(engine.directory / "state.json")
                self.assertEqual(saved["command_requirements"]["missing"], {"greet": [unlisted]})
                self.assertEqual(backend.calls, [])
        task = self.plan(required_commands=[COVERED, UNLISTED])["tasks"][0]
        self.assertEqual(missing_commands([task], {}, self.config.data), {"greet": [UNLISTED]})

    def continuation_backend(self, commands):
        def wait(backend, kwargs, packet, result):
            workers = [c for c in backend.calls if c["role"] == "worker"]
            if kwargs["role"] == "worker" and len(workers) == 1:
                result.update(status="blocked", blocker_kind="continuation", blockers=["Need a read-only Git query"],
                              continuation_request={"kind": "commands", "commands": copy.deepcopy(commands),
                                                    "minimum_timeout_seconds": 0, "reason": "Compare the tree"})
        return ScriptedBackend(wait)

    def test_continuation_with_only_table_queries_dispatches_again_without_stopping(self):
        query = ["git", "ls-tree", "-r", git.head(self.repo), "--", "a"]
        self.assertEqual(len(query[3]), 40)
        backend = self.continuation_backend([query])
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("run_stopped", events(engine))
        workers = [row for row in result["dispatches"] if row["role"] == "worker"]
        self.assertEqual(len(workers), 2)
        self.assertEqual(len(result["continuation_history"]), 1)
        self.assertEqual(result["continuation_history"][0]["request"]["commands"], [query])
        self.assertIsNone(result["pending_continuation"])
        self.assertEqual(self.config.data["permissions"]["worker_commands"], [])

    def test_continuation_with_one_unlisted_command_stops_only_for_it(self):
        query = ["git", "ls-tree", "-r", git.head(self.repo), "--", "a"]
        unlisted = ["git", "ls-files", "--others", "--", "a"]
        backend = self.continuation_backend([query, unlisted])
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        self.assertIn("run_stopped", events(engine))
        self.assertEqual(continuation_gate(engine.context)["missing_commands"], [unlisted])
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), calls)
        self.config.data["permissions"]["worker_commands"].append(unlisted)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.config.data["permissions"]["worker_commands"], [unlisted])

    def environment_blocked(self, checks):
        def block_first(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="environment", blockers=["Git metadata unavailable"])
        backend = ScriptedBackend(block_first)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        source = next(d["id"] for d in engine.run["dispatches"] if d["role"] == "worker")
        path = self.repo / "operator-update.json"
        write_json(path, {"schema_version": 1, "task_id": "greet", "blocker_dispatch_id": source,
                          "summary": "Use the built-in Git query tool", "checks": checks})
        return engine, backend, path

    def test_environment_command_check_counts_builtin_capability_without_execution(self):
        check = {"kind": "worker_command", "argv": COVERED}
        engine, backend, path = self.environment_blocked([check])
        result = self.engine(backend).resume(engine.run["id"], environment_update=path)
        self.assertEqual(result["status"], "completed")
        report = read_json(Path(result["environment_checks"][-1]["artifact"]))
        self.assertTrue(report["passed"])
        self.assertEqual(report["results"], [{"check": check, "passed": True, "coverage": "builtin_git_query"}])
        self.assertIn("no commands executed", report["limitation"])
        self.assertEqual(self.config.data["permissions"]["worker_commands"], [])

    def test_environment_command_check_with_unlisted_argv_still_fails(self):
        engine, backend, path = self.environment_blocked([{"kind": "worker_command", "argv": COVERED},
                                                          {"kind": "worker_command", "argv": UNLISTED}])
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"], environment_update=path)
        saved = read_json(engine.directory / "state.json")
        self.assertFalse(saved["environment_checks"][-1]["passed"])
        report = read_json(Path(saved["environment_checks"][-1]["artifact"]))
        self.assertEqual([item["passed"] for item in report["results"]], [True, False])
        self.assertNotIn("coverage", report["results"][1])
        self.assertEqual(len(backend.calls), calls)


class GitQueryPermissionDiagnosticsTests(RepoCase):
    def test_builtin_coverage_is_reported_apart_from_configured_exact_argv(self):
        config = SimpleNamespace(data={"verification": {"commands": []},
                                       "permissions": {"worker_commands": [["git", "rev-parse", "HEAD"]]}})
        covered = command_capability_projection(config, COVERED)
        self.assertFalse(covered["local_exact_argv_match"])
        self.assertEqual(covered["configured_sources"], [])
        self.assertTrue(covered["builtin_coverage"]["covered"])
        self.assertEqual(covered["builtin_coverage"]["form"], "ls-files")
        self.assertFalse(covered["provider_allowance_verified"])
        both = command_capability_projection(config, ["git", "rev-parse", "HEAD"])
        self.assertTrue(both["local_exact_argv_match"])
        self.assertEqual(both["configured_sources"], ["permissions.worker_commands"])
        self.assertTrue(both["builtin_coverage"]["covered"])
        for argv in (UNLISTED, ["sh", "-c", "git ls-files | head"]):
            report = permission_diagnostics(config, None, argv=argv)["local_command_capability"]
            self.assertFalse(report["local_exact_argv_match"])
            self.assertFalse(report["builtin_coverage"]["covered"])
            self.assertTrue(report["builtin_coverage"]["reason"])
