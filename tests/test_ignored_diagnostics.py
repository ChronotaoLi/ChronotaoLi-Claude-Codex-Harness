from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.util import read_json, write_json
from test_harness import RepoCase
from test_runtime_changes import ScriptedBackend


class IgnoredDiagnosticRecoveryTests(RepoCase):
    def test_interrupted_initial_inventory_recovers_original_baseline_before_worker_outputs(self):
        with (self.repo / ".gitignore").open("a") as stream:
            stream.write("ignored-helper\n")
        git.git(self.repo, "add", ".gitignore")
        git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                "-c", "commit.gpgsign=false", "commit", "-m", "Ignore task helper")

        def create_ignored_helper(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                (kwargs["cwd"] / "ignored-helper").write_text("worker output")

        backend = ScriptedBackend(create_ignored_helper)
        engine = self.engine(backend)
        with patch.object(engine, "record_ignored_paths", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet")
        saved = read_json(engine.directory / "state.json")
        baseline = saved["ignored_baseline"]
        self.assertEqual(baseline, [])
        self.assertTrue(saved["worktree_binding"])
        self.assertFalse((engine.directory / "ignored-baseline.json").exists())
        self.assertEqual(backend.calls, [])
        self.assertEqual(saved["total_attempts"], 0)

        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["ignored_baseline"], baseline)
        self.assertEqual(read_json(engine.directory / "ignored-baseline.json"), baseline)
        inventory = read_json(Path(result["ignored_diagnostics"]["artifact"]))
        self.assertIn("ignored-helper", inventory["added"])
        self.assertEqual(sum(call["kind"] == "plan" for call in backend.calls), 1)
        self.assertEqual(result["total_attempts"], 1)

    def test_initial_inventory_artifact_write_failure_keeps_durable_baseline(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)

        def fail_baseline(path, value):
            if Path(path).name == "ignored-baseline.json":
                raise OSError("baseline artifact unavailable")
            return write_json(path, value)

        with patch("harness.engine.write_json", side_effect=fail_baseline):
            with self.assertRaisesRegex(OSError, "baseline artifact unavailable"):
                engine.start("Implement greet")
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["ignored_baseline"], [])
        self.assertEqual(backend.calls, [])
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(read_json(engine.directory / "ignored-baseline.json"), saved["ignored_baseline"])
