"""Read-only previews must not create evidence even when validation refuses it."""
import contextlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import git_isolation
from harness.cleanup import cleanup_run
from harness.util import HarnessError, read_json
from test_harness import RepoCase, ROOT, cli_notice, run_cli
from test_provider_failures import FifoWatchdog
from test_state_files import held_lock


def tree_bytes(root):
    """Capture file bytes, tree membership and types without opening non-regular entries."""
    return {
        path.relative_to(root): (
            ("link", path.readlink()) if path.is_symlink()
            else ("file", path.read_bytes()) if path.is_file()
            else ("other", path.lstat().st_mode)
        )
        for path in root.rglob("*")
    }


@contextlib.contextmanager
def nonregular(case, path, form):
    saved = path.with_name(path.name + ".original")
    had_file = path.is_file()
    if had_file:
        path.rename(saved)
    watchdog = None
    if form == "fifo":
        os.mkfifo(path)
        watchdog = FifoWatchdog(path, bound=5)
    else:
        path.symlink_to(saved)
    try:
        yield
    finally:
        if watchdog is not None:
            hung = watchdog.stop()
        path.unlink()
        if had_file:
            saved.rename(path)
        if watchdog is not None:
            case.assertFalse(hung, f"blocked opening {path}")


class ReadonlyCleanupTests(unittest.TestCase):
    def test_preview_does_not_create_missing_lock_or_private_controls(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            directory = repo / '.harness/runs/run_test'
            directory.mkdir(parents=True)
            (directory / 'state.json').write_text('{}')
            before = {str(path.relative_to(repo)): path.read_bytes()
                      for path in repo.rglob('*') if path.is_file()}
            with self.assertRaises(HarnessError):
                cleanup_run(repo, 'run_test')
            after = {str(path.relative_to(repo)): path.read_bytes()
                     for path in repo.rglob('*') if path.is_file()}
            self.assertEqual(before, after)
            self.assertFalse((directory / 'run.lock').exists())
            self.assertFalse((repo / '.harness/.gitignore').exists())


class PreviewLockTests(RepoCase):
    def test_abandoned_before_final_verification_can_be_cleaned_up(self):
        engine = self.engine()
        run = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"), pause_after_plan=True)
        run_id = run["id"]
        abandoned = engine.abandon(run_id)
        self.assertEqual((abandoned["status"], abandoned["final_history"]), ("abandoned", []))
        self.assertEqual(cleanup_run(self.repo, run_id)["worktree"]["status"], "eligible")
        self.assertEqual(cleanup_run(self.repo, run_id, remove=True)["worktree"]["status"], "removed")
        self.assertFalse(engine.worktree.exists())

    def test_preview_without_a_lock_does_not_create_one(self):
        engine = self.engine()
        run = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"), pause_after_plan=True)
        (engine.directory / "run.lock").unlink()
        before = tree_bytes(engine.context.state)
        self.assertEqual(cleanup_run(self.repo, run["id"])["worktree"]["status"], "retained")
        self.assertEqual(tree_bytes(engine.context.state), before)

    def test_busy_preview_uses_typed_stop_and_exit_one(self):
        engine = self.engine()
        run = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"), pause_after_plan=True)
        path = engine.directory / "run.lock"
        path.write_bytes(b"control bytes")
        with held_lock(path):
            before = tree_bytes(engine.context.state)
            code, error = run_cli(self, engine.backend, ["cleanup-run", run["id"]])
            stop = cli_notice(error)
            self.assertEqual(code, 1, error)
            self.assertEqual((stop["kind"], stop["reason"]), ("busy", "run_lock"))
            self.assertEqual(tree_bytes(engine.context.state), before)
