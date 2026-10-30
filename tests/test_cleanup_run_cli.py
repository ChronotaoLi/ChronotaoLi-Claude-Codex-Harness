import contextlib
import io
import json
import shutil
from unittest.mock import patch

from harness import cli
from harness.util import read_json
from test_harness import RepoCase, ROOT


class CleanupRunCliTests(RepoCase):
    def test_cli_preview_and_explicit_removal_do_not_load_config_or_relabel_completion(self):
        engine = self.engine()
        run = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        shutil.rmtree(engine.worktree / "__pycache__", ignore_errors=True)
        original = (engine.directory / "state.json").read_bytes()
        for remove, status in ((False, "eligible"), (True, "removed"), (False, "absent")):
            output = io.StringIO()
            args = ["--config", "/missing", "--repo", str(self.repo), "cleanup-run", run["id"]]
            if remove:
                args.append("--remove")
            with patch("harness.cli.Config.load", side_effect=AssertionError("config loaded")), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(args), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], status)
            self.assertEqual(result["run_status"], "completed")
        self.assertEqual((engine.directory / "state.json").read_bytes(), original)
        self.assertFalse(engine.worktree.exists())

    def test_cli_refuses_nonterminal_without_dispatching_or_deleting(self):
        engine = self.engine()
        run = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"), pause_after_plan=True)
        original = (engine.directory / "state.json").read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            result = cli.main(["--repo", str(self.repo), "cleanup-run", run["id"], "--remove"])
        self.assertEqual(result, 1)
        self.assertEqual((engine.directory / "state.json").read_bytes(), original)
        self.assertTrue(engine.worktree.exists())
