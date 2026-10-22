import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness.cli import main
from harness import git
from harness.config import Config
from harness.shared import toml_value
from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class UnknownConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "harness.toml"
        self.data = Config.load(ROOT / "harness.toml").data

    def load(self, data):
        self.path.write_text("\n".join(f"{key} = {toml_value(value)}" for key, value in data.items()))
        return Config.load(self.path)

    def test_unknown_keys_fail_before_typo_can_silently_use_default_budget(self):
        cases = [
            ((), "runtim", {}),
            (("runtime",), "max_attempt", 1),
            (("memory",), "task_ttl_day", 0),
            (("permissions",), "worker_command", []),
            (("verification",), "command", [["false"]]),
            (("shared",), "rule", "rules.md"),
            (("roles",), "reviewr", self.data["roles"]["reviewer"]),
            (("roles", "worker"), "modle", "exact-unintended-model"),
        ]
        for parents, key, value in cases:
            data = copy.deepcopy(self.data)
            section = data
            for parent in parents:
                section = section[parent]
            section[key] = value
            if key == "max_attempt":
                del section["max_attempts"]
            field = ".".join((*parents, key))
            with self.subTest(field=field), self.assertRaises(HarnessError) as caught:
                self.load(data)
            self.assertIn(field, str(caught.exception))

    def test_all_unknown_fields_report_paths_and_valid_exact_models_survive(self):
        self.data["roles"]["worker"]["model"] = "exact-custom-worker-model-v42"
        self.assertEqual(self.load(self.data).role("worker")["model"], "exact-custom-worker-model-v42")
        self.data["runtime"]["max_attempt"] = 1
        self.data["roles"]["worker"]["effrot"] = "high"
        with self.assertRaises(HarnessError) as caught:
            self.load(self.data)
        self.assertIn("runtime.max_attempt", str(caught.exception))
        self.assertIn("roles.worker.effrot", str(caught.exception))


class IndependentCLICommandsTests(RepoCase):
    def invoke(self, args, config_path=None):
        output, errors = io.StringIO(), io.StringIO()
        prefix = ["--repo", str(self.repo)]
        if config_path is not None:
            prefix.extend(["--config", str(config_path)])
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main([*prefix, *args])
        return code, output.getvalue(), errors.getvalue()

    def test_status_keeps_ledger_available_with_missing_or_broken_configuration(self):
        engine = self.engine()
        engine.start("Implement greet", read_json(ROOT / "examples/plan.json"), pause_after_plan=True)
        missing = self.repo / "missing-config.toml"
        broken = self.repo / "broken-config.toml"
        broken.write_text("[invalid TOML")
        for path in (missing, broken):
            with self.subTest(path=path):
                code, output, errors = self.invoke(["status", engine.run["id"], "--full"], path)
                self.assertEqual(code, 0, errors)
                self.assertEqual(json.loads(output), read_json(engine.directory / "state.json"))
                code, output, errors = self.invoke(["status"], path)
                self.assertEqual(code, 0, errors)
                self.assertEqual(json.loads(output)[0]["id"], engine.run["id"])

    def test_memory_round_trip_and_retrieval_need_no_configuration(self):
        missing = self.repo / "missing-config.toml"
        code, output, errors = self.invoke([
            "memory", "add", "--key", "architecture.storage", "--title", "Storage",
            "--text", "Project storage uses SQLite",
        ], missing)
        self.assertEqual(code, 0, errors)
        ident = output.strip()
        code, output, errors = self.invoke(["memory", "read", ident], missing)
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["detail"], "Project storage uses SQLite")
        code, output, errors = self.invoke(["memory", "search", "SQLite"], missing)
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)[0]["id"], ident)
        code, output, errors = self.invoke(["memory", "export"], missing)
        self.assertEqual(code, 0, errors)
        self.assertEqual([row["id"] for row in json.loads(output)], [ident])

    def test_installed_cli_without_default_config_reports_explicit_config_option(self):
        with patch("harness.cli.__file__", str(self.repo / "installed" / "harness" / "cli.py")):
            code, output, errors = self.invoke(["doctor"])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("--config", errors)
        self.assertIn(str(self.repo / "installed" / "harness.toml"), errors)

    def test_cli_passes_repeated_dispatch_exit_confirmations_to_recovery(self):
        result = {"id": "run_test", "status": "paused", "branch": "harness/run_test", "worktree": "worktree"}
        for command in ("resume", "abandon"):
            engine = Mock()
            getattr(engine, command).return_value = result
            with self.subTest(command=command), patch("harness.cli.Engine", return_value=engine):
                code, output, errors = self.invoke([
                    command, "run_test", "--confirm-exited", "dispatch_one", "--confirm-exited", "dispatch_two",
                ], ROOT / "harness.toml")
            self.assertEqual(code, 0, errors)
            self.assertEqual(json.loads(output)["id"], "run_test")
            self.assertEqual(getattr(engine, command).call_args.kwargs["confirm_exited"],
                             ["dispatch_one", "dispatch_two"])


class ExitConfirmationRecoveryTests(RepoCase):
    def paused_run(self, count=1):
        engine = self.engine()
        engine.start("Implement greet", read_json(ROOT / "examples/plan.json"), pause_after_plan=True)
        entries = [{"id": f"dispatch_old_{index}", "role": "verification", "task_id": "greet",
                    "status": "interrupted", "pgid": 2_000_000_000 + index, "exit_confirmed": False}
                   for index in range(1, count + 1)]
        engine.run["dispatches"].extend(entries)
        engine.save()
        return engine, entries

    def confirmation_events(self, engine):
        return [event for line in (engine.directory / "events.jsonl").read_text().splitlines()
                if (event := json.loads(line))["event"] == "dispatch_exit_confirmed_by_operator"]

    def test_confirming_one_dispatch_preserves_other_live_dispatch_gate_and_audit(self):
        engine, entries = self.paused_run(count=2)
        selected, other = entries
        backend = ScriptedBackend()
        with patch("harness.engine.group_alive", return_value=True), \
                patch("harness.util.os.killpg", side_effect=AssertionError("must not signal old groups")), \
                self.assertRaisesRegex(HarnessError, other["id"]):
            self.engine(backend).resume(engine.run["id"], approve_plan=True,
                                        confirm_exited=[selected["id"], selected["id"]])
        saved = read_json(engine.directory / "state.json")
        self.assertTrue(saved["dispatches"][0]["exit_confirmed"])
        self.assertFalse(saved["dispatches"][1]["exit_confirmed"])
        self.assertEqual(saved["dispatches"][1], other)
        self.assertEqual(saved["status"], "awaiting_approval")
        self.assertEqual(backend.calls, [])
        events = self.confirmation_events(engine)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["dispatch_id"], selected["id"])
        self.assertEqual(events[0]["observed"]["pgid"], selected["pgid"])
        self.assertTrue(events[0]["observed"]["group_alive"])
        self.assertEqual(saved["dispatches"][0]["operator_exit_confirmation"]["observed"], events[0]["observed"])

    def test_unknown_confirmation_rejects_entire_batch_without_observation_or_mutation(self):
        engine, entries = self.paused_run(count=2)
        state_path = engine.directory / "state.json"
        event_path = engine.directory / "events.jsonl"
        before = state_path.read_bytes(), event_path.read_bytes()
        for action in ("resume", "abandon"):
            backend = ScriptedBackend()
            with self.subTest(action=action), patch("harness.engine.group_alive") as observe, \
                    self.assertRaisesRegex(HarnessError, "dispatch_unknown"):
                getattr(self.engine(backend), action)(engine.run["id"],
                    confirm_exited=[entries[0]["id"], "dispatch_unknown"])
            observe.assert_not_called()
            self.assertEqual((state_path.read_bytes(), event_path.read_bytes()), before)
            self.assertEqual(backend.calls, [])

    def test_explicit_confirmation_allows_fresh_dispatch_and_full_acceptance_after_pgid_reuse(self):
        engine, entries = self.paused_run()
        old = entries[0]
        backend = ScriptedBackend()
        with patch("harness.engine.group_alive", side_effect=lambda pgid: pgid == old["pgid"]):
            with self.assertRaisesRegex(HarnessError, old["id"]):
                self.engine(backend).resume(engine.run["id"], approve_plan=True)
            with patch("harness.util.os.killpg", wraps=os.killpg) as signal_group:
                result = self.engine(backend).resume(engine.run["id"], approve_plan=True,
                                                     confirm_exited=[old["id"]])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_attempts"], 1)
        self.assertEqual(set(result["accepted"]), {"greet"})
        self.assertTrue(any(call["kind"] == "final_review" for call in backend.calls))
        self.assertTrue(all(call.args[0] != old["pgid"] for call in signal_group.call_args_list))
        self.assertEqual(result["dispatches"][0]["id"], old["id"])
        self.assertTrue(result["dispatches"][0]["exit_confirmed"])
        self.assertTrue(all(entry["id"] != old["id"] for entry in result["dispatches"][1:]))
        self.assertEqual(len(self.confirmation_events(engine)), 1)

    def test_abandon_accepts_confirmation_without_signalling_reused_process_group(self):
        engine, entries = self.paused_run()
        with patch("harness.engine.group_alive", return_value=True), \
                patch("harness.util.os.killpg", side_effect=AssertionError("must not signal old group")):
            result = self.engine().abandon(engine.run["id"], confirm_exited=[entries[0]["id"]])
        self.assertEqual(result["status"], "abandoned")
        self.assertTrue(engine.worktree.exists())
        self.assertEqual(git.git(self.repo, "rev-parse", result["branch"]).strip(), result["expected_head"])
        self.assertTrue(read_json(engine.directory / "state.json")["dispatches"][0]["exit_confirmed"])
        self.assertEqual(self.confirmation_events(engine)[0]["dispatch_id"], entries[0]["id"])
        with self.assertRaisesRegex(HarnessError, "已放弃"):
            self.engine().resume(engine.run["id"], confirm_exited=[entries[0]["id"]])

    def test_confirmation_does_not_bypass_resume_or_removal_head_gate(self):
        engine, entries = self.paused_run()
        git.git(engine.worktree, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Unexpected HEAD")
        backend = ScriptedBackend()
        with patch("harness.engine.group_alive", return_value=True):
            with self.assertRaisesRegex(HarnessError, "HEAD"):
                self.engine(backend).resume(engine.run["id"], approve_plan=True,
                                            confirm_exited=[entries[0]["id"]])
            with self.assertRaisesRegex(HarnessError, "HEAD"):
                self.engine(backend).abandon(engine.run["id"], remove_worktree=True,
                                             confirm_exited=[entries[0]["id"]])
        self.assertEqual(backend.calls, [])
        self.assertTrue(engine.worktree.exists())
        self.assertEqual(read_json(engine.directory / "state.json")["status"], "awaiting_approval")

    def test_confirmation_does_not_bypass_ignored_unsaved_file_removal_gate(self):
        engine, entries = self.paused_run()
        unsaved = engine.worktree / "__pycache__" / "uncommitted.bin"
        unsaved.parent.mkdir()
        unsaved.write_bytes(b"unsaved ignored file")
        with patch("harness.engine.group_alive", return_value=True), \
                self.assertRaises(HarnessError):
            self.engine().abandon(engine.run["id"], remove_worktree=True,
                                  confirm_exited=[entries[0]["id"]])
        self.assertEqual(unsaved.read_bytes(), b"unsaved ignored file")
        saved = read_json(engine.directory / "state.json")
        self.assertTrue(saved["dispatches"][0]["exit_confirmed"])
        self.assertEqual(saved["status"], "awaiting_approval")
        unsaved.unlink()
        unsaved.parent.rmdir()
        result = self.engine().abandon(engine.run["id"], remove_worktree=True)
        self.assertEqual(result["status"], "abandoned")
        self.assertTrue(result["worktree_removed"])
        self.assertFalse(engine.worktree.exists())
        self.assertEqual(git.git(self.repo, "rev-parse", result["branch"]).strip(), result["expected_head"])
        self.assertEqual(len(self.confirmation_events(engine)), 1)


if __name__ == "__main__":
    unittest.main()
