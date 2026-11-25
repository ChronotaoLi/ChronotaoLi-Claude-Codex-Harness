import contextlib
import copy
import io
import json
import os
import signal
import sys
import uuid
from unittest.mock import patch

from harness.adapters import CLIBackend, DemoBackend, UndecodableOutput
from harness.cli import main
from harness.runtime import Runtime
from harness.util import HarnessError, ProcessTimeout, dump, group_alive, read_json, run_process, write_json
from harness.versions import RUN_SCHEMA_VERSION
from test_harness import RepoCase, ROOT, walk_exit


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
        engine = Runtime(self.config, self.repo, backend=backend)
        with patch.object(backend, "command", return_value=[str(self.repo / "not-installed-cli")]):
            with self.assertRaises(HarnessError):
                engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(saved["accepted"], {})
        self.assertEqual(saved["dispatches"][-1]["status"], "failed")
        self.assertTrue(saved["dispatches"][-1]["exit_confirmed"])
        # Row provider-start: once a working CLI is back (here the demo backend), the listed
        # plain resume is a new worker attempt.
        after = walk_exit(self, engine.directory, ("provider_failure", "local_start"), backend=DemoBackend(),
                          expect_status="completed")
        self.assertEqual(sum(d["role"] == "worker" and d.get("invoked") for d in after["dispatches"]), 2)

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
        state_root = self.repo / ".harness"
        dispatches = state_root / "runs" / "test-run" / "dispatches"
        job = dispatches / ("dispatch-" + cli + "-" + uuid.uuid4().hex)
        job.mkdir(parents=True)
        self.provider_job = job
        model = {"cli": cli, "model": "exact-test-model", "effort": "high"}
        backend = CLIBackend(self.config, state_root)
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
                path = self.provider_job / ("process.stdout" if cli == "claude" else "response.json")
                self.assertEqual(path.read_text(), raw)

    def test_claude_malformed_payload_cannot_escape_as_attribute_or_type_error(self):
        terminal = {"type": "result", "subtype": "success"}
        for payload in ({**terminal, "result": []}, {**terminal, "structured_output": []},
                        {**terminal, "structured_output": False}, {**terminal, "result": "broken"},
                        {"result": "{}"}, {"type": "assistant", "structured_output": {}}):
            with self.subTest(payload=payload):
                # A complete terminal without an object result is an invalid delivery.
                with self.assertRaises(UndecodableOutput):
                    self.invoke_provider("claude", json.dumps(payload))
                self.assertEqual(json.loads((self.provider_job / "process.stdout").read_text()), payload)
                self.assertFalse((self.provider_job / "response.json").exists())
                provider = self.provider_job / "provider-result.json"
                if payload.get("type") == "result":
                    self.assertEqual(read_json(provider), payload)
                else:
                    self.assertFalse(provider.exists())
        error = {"type": "result", "subtype": "error_max_turns", "result": "{}"}
        with self.assertRaises(HarnessError) as caught:
            self.invoke_provider("claude", json.dumps(error))
        self.assertNotIsInstance(caught.exception, UndecodableOutput)
        self.assertEqual(read_json(self.provider_job / "provider-result.json"), error)

    def test_provider_success_preserves_exact_ids_without_fallback(self):
        value = {"task_id": "task-exact", "dispatch_id": "dispatch-exact", "summary": "accepted"}
        for cli in ("claude", "codex"):
            raw = json.dumps({"type": "result", "subtype": "success", "structured_output": value}
                             if cli == "claude" else value)
            with self.subTest(cli=cli):
                self.assertEqual(self.invoke_provider(cli, raw), value)

    def test_status_retains_healthy_runs_when_other_state_is_damaged(self):
        runs = self.repo / ".harness" / "runs"
        good = {"id": "run_good", "status": "completed", "goal": "done", "branch": "harness/run_good"}
        write_json(runs / "run_good" / "state.json", {**good, "schema_version": RUN_SCHEMA_VERSION})
        write_json(runs / "run_partial" / "state.json", {"goal": "partial", "schema_version": RUN_SCHEMA_VERSION})
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
