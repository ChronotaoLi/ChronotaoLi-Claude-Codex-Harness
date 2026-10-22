import contextlib
import copy
import io
import json
import os
import signal
import sys
from unittest.mock import patch

from harness.adapters import CLIBackend
from harness.cli import main
from harness.engine import Engine
from harness.shared import HookError, Hooks
from harness.util import HarnessError, ProcessTimeout, dump, group_alive, read_json, run_process, write_json
from test_harness import RepoCase, ROOT


class ErrorRepairsTests(RepoCase):
    def test_timeout_preserves_evidence_and_reaps_the_owned_process(self):
        log = self.repo / "timeout"
        pgids = []
        with self.assertRaises(ProcessTimeout) as caught:
            run_process([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
                        self.repo, log, timeout=0.2, on_start=pgids.append)
        self.assertEqual(caught.exception.timeout, 0.2)
        self.assertEqual(caught.exception.log, log)
        self.assertEqual(log.with_suffix(".stdout").read_text(), "started\n")
        self.assertEqual(len(pgids), 1)
        self.assertFalse(group_alive(pgids[0]))

    def test_missing_executable_is_an_environment_failure_with_logs(self):
        log = self.repo / "missing"
        pgids = []
        with self.assertRaises(HarnessError) as caught:
            run_process([str(self.repo / "not-installed-cli")], self.repo, log, on_start=pgids.append)
        self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)
        self.assertEqual(pgids, [])
        self.assertIn("not-installed-cli", log.with_suffix(".stderr").read_text())
        self.assertTrue(log.with_suffix(".stdout").exists())

    def test_missing_cli_blocks_run_instead_of_classifying_it_as_interrupted(self):
        backend = CLIBackend(self.config, self.repo / ".harness")
        engine = Engine(self.config, self.repo, backend=backend)
        with patch.object(backend, "command", return_value=[str(self.repo / "not-installed-cli")]):
            with self.assertRaises(HarnessError):
                engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(saved["accepted"], {})
        self.assertEqual(saved["dispatches"][-1]["status"], "failed")
        self.assertTrue(saved["dispatches"][-1]["exit_confirmed"])

    def test_process_group_permission_denial_never_allows_redispatch(self):
        with patch("harness.util.os.killpg", side_effect=PermissionError):
            self.assertTrue(group_alive(12345))
        with patch("harness.util.os.killpg", side_effect=ProcessLookupError):
            self.assertFalse(group_alive(12345))

    def test_json_roundtrip_preserves_non_utf8_paths_without_losing_unicode(self):
        filename = os.fsdecode(b"file-\xff.txt")
        value = {filename: {"source": filename, "description": "验收通过"}}
        path = self.repo / "snapshot.json"
        write_json(path, value)
        self.assertEqual(read_json(path), value)
        self.assertEqual(json.loads(dump(value)), value)
        self.assertIn("验收通过", path.read_text(encoding="utf-8"))
        self.assertEqual(os.fsencode(next(iter(read_json(path)))), b"file-\xff.txt")

    def invoke_provider(self, cli, raw):
        job = self.repo / ("dispatch-" + cli)
        job.mkdir(exist_ok=True)
        model = {"cli": cli, "model": "exact-test-model", "effort": "high"}
        backend = CLIBackend(self.config, self.repo / ".harness")
        if cli == "codex":
            script = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])"
            argv = [sys.executable, "-c", script, str(job / "response.json"), raw]
        else:
            argv = [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", raw]
        with patch.object(backend, "command", return_value=argv):
            return backend.invoke(role="reviewer", model=model, cwd=self.repo, job=job,
                                  schema={}, prompt="Offline provider transport fixture", run_path=None,
                                  on_start=lambda _: None)

    def test_malformed_provider_json_is_a_hard_error_with_original_evidence(self):
        for cli in ("claude", "codex"):
            for raw in ("invalid JSON", "[]", "null"):
                with self.subTest(cli=cli, raw=raw), self.assertRaises(HarnessError):
                    self.invoke_provider(cli, raw)
                path = self.repo / ("dispatch-" + cli) / ("process.stdout" if cli == "claude" else "response.json")
                self.assertEqual(path.read_text(), raw)

    def test_claude_malformed_payload_cannot_escape_as_attribute_or_type_error(self):
        for payload in ({"result": []}, {"structured_output": []}, {"structured_output": False},
                        {"result": "broken"}, {"subtype": "error_max_turns", "result": "{}"}):
            with self.subTest(payload=payload), self.assertRaises(HarnessError):
                self.invoke_provider("claude", json.dumps(payload))
            self.assertEqual(read_json(self.repo / "dispatch-claude" / "provider-result.json"), payload)

    def test_provider_success_preserves_exact_ids_without_fallback(self):
        value = {"task_id": "task-exact", "dispatch_id": "dispatch-exact", "summary": "accepted"}
        for cli in ("claude", "codex"):
            raw = json.dumps({"structured_output": value} if cli == "claude" else value)
            with self.subTest(cli=cli):
                self.assertEqual(self.invoke_provider(cli, raw), value)

    def hooks(self, spec):
        path = self.repo / ".harness" / "hooks.json"
        write_json(path, spec)
        self.config.data["shared"]["hooks"] = str(path)
        return Hooks(self.config)

    def test_all_hook_configuration_is_validated_before_any_hook_executes(self):
        valid = {"argv": [sys.executable, "-c", "pass"]}
        invalid = [[], {"unknown": []}, {"pre_agent": {}}, {"post_accept": [None]},
                   {"post_accept": [{}]}, {"post_accept": [{"argv": "echo nope"}]},
                   {"post_accept": [{"argv": [""]}]}, {"post_accept": [{"argv": ["echo", "\0"]}]},
                   {"post_accept": [{"argv": ["echo"], "extra": "typo"}]}]
        invalid += [{"post_accept": [{**valid, "timeout_seconds": value}]}
                    for value in (True, False, 0, -1, "30", None, float("nan"), float("inf"), 10 ** 1000)]
        with patch("harness.shared.run_process") as process:
            for spec in invalid:
                with self.subTest(spec=spec), self.assertRaises(HarnessError):
                    self.hooks(spec)
            hooks = self.hooks({"pre_agent": [valid]})
            hooks.spec["post_accept"] = [{"argv": []}]
            with self.assertRaises(HarnessError):
                hooks.emit("pre_agent", {}, self.repo / "logs")
        process.assert_not_called()

    def test_hook_rejection_carries_exit_code_and_evidence(self):
        hooks = self.hooks({"pre_accept": [{"argv": [sys.executable, "-c", "print('policy failed'); raise SystemExit(7)"]}]})
        with self.assertRaises(HookError) as caught:
            hooks.emit("pre_accept", {"task_id": "greet"}, self.repo / "logs")
        error = caught.exception
        self.assertEqual((error.event, error.code, error.timeout), ("pre_accept", 7, None))
        self.assertEqual(error.log.with_suffix(".stdout").read_text(), "policy failed\n")

    def test_hook_timeout_and_missing_command_keep_structured_failure(self):
        fixtures = [("timeout", [sys.executable, "-c", "import time; time.sleep(30)"], 0.05),
                    ("startup", [str(self.repo / "missing-hook")], 30)]
        for name, argv, timeout in fixtures:
            hooks = self.hooks({"pre_accept": [{"argv": argv, "timeout_seconds": timeout}]})
            with self.subTest(name=name), self.assertRaises(HookError) as caught:
                hooks.emit("pre_accept", {}, self.repo / name)
            self.assertEqual(caught.exception.event, "pre_accept")
            self.assertIsNone(caught.exception.code)
            self.assertEqual(caught.exception.timeout, timeout if name == "timeout" else None)
            self.assertTrue(caught.exception.log.with_suffix(".stderr").exists())

    def test_final_attempt_budget_is_independent_and_can_increase_on_resume(self):
        snapshot = copy.deepcopy(self.config.data)
        del snapshot["runtime"]["max_final_attempts"]
        self.assertEqual(self.config.resume_changes(snapshot), {})
        self.config.data["runtime"]["max_final_attempts"] = 4
        self.assertEqual(self.config.resume_changes(snapshot), {"max_final_attempts": {"old": 3, "new": 4}})
        self.assertEqual(self.config.data["runtime"]["max_attempts"], 3)
        self.config.data["runtime"]["max_final_attempts"] = 2
        with self.assertRaises(HarnessError):
            self.config.resume_changes(snapshot)

    def test_status_retains_healthy_runs_when_other_state_is_damaged(self):
        runs = self.repo / ".harness" / "runs"
        good = {"id": "run_good", "status": "completed", "goal": "done", "branch": "harness/run_good"}
        write_json(runs / "run_good" / "state.json", good)
        write_json(runs / "run_partial" / "state.json", {"goal": "partial"})
        write_json(runs / "run_shape" / "state.json", [])
        write_json(runs / "run_broken" / "state.json", {})
        (runs / "run_broken" / "state.json").write_text("{")
        (runs / "run_missing").mkdir()
        output, errors = io.StringIO(), io.StringIO()
        previous = signal.getsignal(signal.SIGTERM)
        try:
            with patch("harness.cli.Config.load", return_value=self.config), contextlib.redirect_stdout(output), \
                    contextlib.redirect_stderr(errors):
                code = main(["--repo", str(self.repo), "status"])
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assertEqual(code, 0)
        values = json.loads(output.getvalue())
        self.assertIn(good, values)
        self.assertEqual(len(values), 2)
        self.assertIn({"id": "run_partial", "status": "unknown", "goal": "partial", "branch": None}, values)
        for name in ("run_broken", "run_partial", "run_shape", "run_missing"):
            self.assertIn(name, errors.getvalue())
