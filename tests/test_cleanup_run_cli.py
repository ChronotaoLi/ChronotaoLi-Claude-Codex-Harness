import contextlib
import io
import json
import shutil
from unittest.mock import patch

from harness import cli, observe
from harness.util import read_json
from test_harness import RepoCase, ROOT


class CleanupRunCliTests(RepoCase):
    def test_cli_preview_and_explicit_removal_do_not_load_config_or_relabel_completion(self):
        engine = self.engine()
        run = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        shutil.rmtree(engine.worktree / "__pycache__", ignore_errors=True)
        original = (engine.directory / "state.json").read_bytes()
        state = self.repo / ".harness"
        self.assertIs(observe.run_detail(state, run["id"])["run"]["worktree_removed"], False)
        for remove, status in ((False, "eligible"), (True, "removed"), (False, "absent")):
            output = io.StringIO()
            args = ["--config", "/missing", "--repo", str(self.repo), "cleanup-run", run["id"]]
            if remove:
                args.append("--remove")
            with patch("harness.cli.Config.load", side_effect=AssertionError("config loaded")), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(args), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["worktree"]["status"], status)
            self.assertEqual(result["checkouts"], [])
            self.assertEqual(result["run_status"], "completed")
        self.assertEqual((engine.directory / "state.json").read_bytes(), original)
        self.assertFalse(engine.worktree.exists())
        # The ledger carries no removal key; observation reads the cleanup receipt.
        self.assertNotIn("worktree_removed", read_json(engine.directory / "state.json"))
        self.assertIs(observe.run_detail(state, run["id"])["run"]["worktree_removed"], True)

    def test_cli_retains_nonterminal_worktree_without_dispatching_or_deleting(self):
        engine = self.engine()
        run = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"), pause_after_plan=True)
        original = (engine.directory / "state.json").read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = cli.main(["--repo", str(self.repo), "cleanup-run", run["id"], "--remove"])
        self.assertEqual(result, 0)
        worktree = json.loads(output.getvalue())["worktree"]
        self.assertEqual(worktree["status"], "retained")
        self.assertIn("awaiting_approval", worktree["reason"])
        self.assertEqual((engine.directory / "state.json").read_bytes(), original)
        self.assertTrue(engine.worktree.exists())
        self.assertIs(observe.run_detail(self.repo / ".harness", run["id"])["run"]["worktree_removed"], False)

    def test_abandon_then_cleanup_run_replaces_the_removed_abandon_option(self):
        engine = self.engine()
        run = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"), pause_after_plan=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--repo", str(self.repo), "abandon", run["id"]]), 0)
            self.assertTrue(engine.worktree.is_dir())
            self.assertEqual(cli.main(["--repo", str(self.repo), "cleanup-run", run["id"], "--remove"]), 0)
        self.assertFalse(engine.worktree.exists())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["status"], "abandoned")
        self.assertFalse({"worktree_removed", "pending_worktree_removal"} & set(saved))
        self.assertEqual(read_json(engine.directory / "worktree-cleanup.json")["status"], "removed")

    def test_removed_cleanup_entry_points_are_refused(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as shown:
            cli._main(["--help"])
        self.assertEqual(shown.exception.code, 0)
        choices = output.getvalue().split("{", 1)[1].split("}", 1)[0].split(",")
        self.assertIn("cleanup-run", choices)
        self.assertNotIn("cleanup-verification", choices)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            cli._main(["abandon", "--help"])
        self.assertNotIn("--remove-worktree", output.getvalue())
        for command in (["cleanup-verification", "run_x"], ["abandon", "run_x", "--remove-worktree"]):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as refused:
                cli._main(["--repo", str(self.repo), *command])
            self.assertEqual(refused.exception.code, 2)
