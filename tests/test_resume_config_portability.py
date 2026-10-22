import contextlib
import copy
import io
import json
import shutil
import signal
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness.adapters import DemoBackend
from harness.cli import main
from harness.config import Config, normalized
from harness.engine import Engine
from harness.shared import asset_digest
from harness.util import HarnessError, group_alive, read_json, run_process, write_json
from test_harness import ROOT, RepoCase


class ResumeConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.config = Config.load(ROOT / "harness.toml")

    def test_dispatch_budget_has_a_bounded_default_and_can_only_increase(self):
        data = copy.deepcopy(self.config.data)
        del data["runtime"]["max_agent_dispatches"]
        self.assertEqual(normalized(data)["runtime"]["max_agent_dispatches"], 200)
        for key in ("max_replans", "max_agent_dispatches"):
            config = copy.deepcopy(self.config)
            old = config.data["runtime"][key]
            config.data["runtime"][key] = old + 1
            with self.subTest(key=key):
                self.assertEqual(config.resume_changes(self.config.data), {key: {"old": old, "new": old + 1}})
                config.data["runtime"][key] = old - 1
                with self.assertRaises(HarnessError):
                    config.resume_changes(self.config.data)
        for value in (0, -1, True, "200"):
            invalid = copy.deepcopy(self.config.data)
            invalid["runtime"]["max_agent_dispatches"] = value
            with self.subTest(value=value), self.assertRaises(HarnessError):
                Config.validate(invalid)

    def test_command_grants_preserve_exact_argv_and_auditable_additions(self):
        first = ["python3", "-c", "print('literal $(value)')"]
        second = ["rm", "--", "generated file.txt"]
        self.config.data["permissions"]["worker_commands"] = [first]
        saved = copy.deepcopy(self.config.data)
        self.config.data["permissions"]["worker_commands"] = [second, first]
        current = copy.deepcopy(self.config.data)
        self.assertEqual(self.config.resume_changes(saved), {
            "worker_commands": {"old": [first], "new": [second, first], "added": [second]}})
        self.assertEqual(self.config.data, current)
        self.assertEqual(saved["permissions"]["worker_commands"], [first])
        self.config.data["permissions"]["worker_commands"] = [first, second]
        self.assertEqual(self.config.resume_changes(current), {})

    def test_command_removals_argument_changes_and_wildcards_remain_rejected(self):
        original = ["rm", "--", "generated.txt"]
        self.config.data["permissions"]["worker_commands"] = [original]
        saved = copy.deepcopy(self.config.data)
        for commands in ([], [["rm", "--", "different.txt"]], [["rm", "generated.txt"]],
                         [original, ["rm", "*"]], [original, ["echo", "line\nbreak"]]):
            self.config.data["permissions"]["worker_commands"] = commands
            with self.subTest(commands=commands), self.assertRaises(HarnessError):
                self.config.resume_changes(saved)


class AssetPortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def installation(self, name):
        root = self.root / name
        shutil.copytree(ROOT / "shared", root / "shared")
        return Config(root / "harness.toml", copy.deepcopy(Config.load(ROOT / "harness.toml").data))

    def test_identical_resources_relocate_without_invalidating_digest(self):
        original, relocated = self.installation("original"), self.installation("relocated")
        self.assertEqual(original.resume_changes(relocated.data), {})
        self.assertEqual(asset_digest(original), asset_digest(relocated))
        for key in ("rules", "mcp", "hooks"):
            path = relocated.asset(key)
            renamed = path.with_name("relocated-" + path.name)
            path.rename(renamed)
            relocated.data["shared"][key] = str(renamed)
        self.assertEqual(asset_digest(original), asset_digest(relocated))

    def test_content_and_skill_identity_changes_still_invalidate_digest(self):
        original = self.installation("original")
        baseline = asset_digest(original)
        for key in ("rules", "mcp", "hooks"):
            changed = self.installation(key)
            path = changed.asset(key)
            path.write_bytes(path.read_bytes() + b"\n")
            with self.subTest(key=key):
                self.assertNotEqual(baseline, asset_digest(changed))
        changed = self.installation("skill-content")
        path = changed.asset("skills") / "harness-worker" / "SKILL.md"
        path.write_bytes(path.read_bytes() + b"\n")
        self.assertNotEqual(baseline, asset_digest(changed))
        changed = self.installation("skill-name")
        path = changed.asset("skills") / "harness-worker"
        path.rename(path.with_name("renamed-worker"))
        self.assertNotEqual(baseline, asset_digest(changed))
        changed = self.installation("skill-removed")
        shutil.rmtree(changed.asset("skills") / "harness-worker")
        self.assertNotEqual(baseline, asset_digest(changed))


class ResumeConfigurationIntegrationTests(RepoCase):
    def paused_run(self):
        engine = self.engine()
        engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"), pause_after_plan=True)
        return engine

    def test_additional_command_is_saved_and_audited_on_resume(self):
        engine = self.paused_run()
        command = ["rm", "--", "generated file.txt"]
        self.config.data["permissions"]["worker_commands"] = [command]
        result = self.engine().resume(engine.run["id"], approve_plan=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["config_snapshot"]["permissions"]["worker_commands"], [command])
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        changes = [item["changes"] for item in events if item["event"] == "resume_config_relaxed"]
        self.assertEqual(changes, [{"worker_commands": {"old": [], "new": [command], "added": [command]}}])

    def test_relocated_identical_shared_resources_can_resume_the_same_run(self):
        engine = self.paused_run()
        relocated = self.repo / ".harness" / "relocated-installation"
        shutil.copytree(ROOT / "shared", relocated / "shared")
        config = Config(relocated / "harness.toml", copy.deepcopy(self.config.data))
        result = Engine(config, self.repo, backend=DemoBackend()).resume(engine.run["id"], approve_plan=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["asset_digest"], asset_digest(config))


class CLIBoundaryTests(RepoCase):
    def invoke(self, args, **patches):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("harness.cli.Config.load", return_value=self.config))
            for name, value in patches.items():
                stack.enter_context(patch("harness.cli." + name, value))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            code = main(["--repo", str(self.repo), *args])
        return code, output.getvalue(), errors.getvalue()

    def test_specific_status_rejects_nonobject_ledger_without_traceback(self):
        path = self.repo / ".harness" / "runs" / "run_broken" / "state.json"
        for value in ([], None, "invalid", 7):
            write_json(path, value)
            code, output, errors = self.invoke(["status", "run_broken"])
            with self.subTest(value=value):
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertIn("运行台账必须为对象", errors)
                self.assertNotIn("Traceback", errors)
                self.assertEqual(read_json(path), value)

    def test_resume_rejects_invalid_ledger_shape_without_rewriting_evidence(self):
        path = self.repo / ".harness" / "runs" / "run_broken" / "state.json"
        for value in ([], None, "invalid", 7, {"schema_version": 4}, {"schema_version": 3}):
            write_json(path, value)
            code, output, errors = self.invoke(["resume", "run_broken"])
            with self.subTest(value=value):
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertIn("harness:", errors)
                self.assertNotIn("Traceback", errors)
                self.assertEqual(read_json(path), value)

    def test_corrupt_sqlite_is_reported_and_programming_errors_are_not_hidden(self):
        path = self.repo / ".harness" / "memory.sqlite3"
        path.parent.mkdir()
        path.write_bytes(b"damaged sqlite database")
        code, _, errors = self.invoke(["memory", "export"])
        self.assertEqual(code, 1)
        self.assertIn("SQLite", errors)
        self.assertNotIn("Traceback", errors)
        self.assertEqual(path.read_bytes(), b"damaged sqlite database")
        for error in (sqlite3.ProgrammingError, sqlite3.InterfaceError, TypeError, AttributeError):
            with self.subTest(error=error), self.assertRaises(error):
                self.invoke(["memory", "export"], Memory=Mock(side_effect=error("implementation defect")))

    def test_explicit_reverification_is_forwarded(self):
        engine = Mock()
        engine.resume.return_value = {"id": "run_test", "status": "completed", "branch": "branch", "worktree": "tree"}
        code, output, errors = self.invoke(["resume", "run_test", "--reverify"], Engine=Mock(return_value=engine))
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["status"], "completed")
        engine.resume.assert_called_once_with("run_test", approve_plan=False, step=False, reverify=True)

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "SIGHUP is a POSIX signal")
    def test_hangup_preserves_interrupted_run_and_restores_signal_handlers(self):
        class HangupBackend(DemoBackend):
            def invoke(self, **kwargs):
                if kwargs["role"] == "worker":
                    run_process([sys.executable, "-c",
                                 "import os, signal, time; os.kill(os.getppid(), signal.SIGHUP); time.sleep(30)"],
                                kwargs["cwd"], kwargs["job"] / "signal-child", on_start=kwargs["on_start"])
                return super().invoke(**kwargs)

        engine = Engine(self.config, self.repo, backend=HangupBackend())
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
        code, _, errors = self.invoke(["run", "Implement greet", "--plan", str(ROOT / "examples" / "plan.json")],
                                      Engine=Mock(return_value=engine))
        self.assertEqual(code, 130, errors)
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["accepted"], {})
        self.assertTrue(engine.worktree.is_dir())
        dispatch = saved["dispatches"][-1]
        self.assertTrue(dispatch["pgid"])
        self.assertTrue(dispatch["exit_confirmed"])
        self.assertFalse(group_alive(dispatch["pgid"]))
        self.assertEqual({sig: signal.getsignal(sig) for sig in previous}, previous)


if __name__ == "__main__":
    unittest.main()
