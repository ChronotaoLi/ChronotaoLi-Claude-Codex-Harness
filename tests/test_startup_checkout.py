import contextlib
import io

from harness import git
from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class StartupCheckoutTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples/plan.json")

    def test_main_untracked_files_are_preserved_outside_completed_run(self):
        original_head = git.head(self.repo)
        original = self.repo / "hello.py"
        original.write_text("operator scratch work\n")
        notes = self.repo / "notes"
        notes.mkdir()
        (notes / "draft.txt").write_text("private notes\n")
        engine = self.engine()
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(git.head(self.repo), original_head)
        self.assertEqual(original.read_text(), "operator scratch work\n")
        self.assertEqual((notes / "draft.txt").read_text(), "private notes\n")
        self.assertNotEqual((engine.worktree / "hello.py").read_text(), original.read_text())
        self.assertFalse((engine.worktree / "notes").exists())
        self.assertIn("'hello.py'", output.getvalue())
        self.assertIn("'notes/'", output.getvalue())
        self.assertFalse(git.clean(self.repo))
        self.assertTrue(git.clean(engine.worktree, binding=result["worktree_binding"]))

    def test_staged_changes_stop_before_run_creation_without_touching_user_data(self):
        path = self.repo / "mine.txt"
        path.write_text("staged user work\n")
        git.git(self.repo, "add", "mine.txt")
        refs = git.git(self.repo, "show-ref")
        worktrees = git.git(self.repo, "worktree", "list", "--porcelain")
        index = git.git_bytes(self.repo, "diff", "--cached", "--binary")
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "未提交变更"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(path.read_text(), "staged user work\n")
        self.assertEqual(git.git_bytes(self.repo, "diff", "--cached", "--binary"), index)
        self.assertEqual(git.git(self.repo, "show-ref"), refs)
        self.assertEqual(git.git(self.repo, "worktree", "list", "--porcelain"), worktrees)
        self.assertFalse((engine.state / "runs").exists())
        self.assertEqual(backend.calls, [])

    def test_startup_diagnostic_escapes_paths_and_bounds_its_size(self):
        names = ["00-line\nbreak-\x1b.txt", "01-笔记.txt",
                 "02-" + "x" * 245, *[f"file-{index:02d}" for index in range(12)]]
        for name in names:
            (self.repo / name).write_text("preserve\n")
        output = io.StringIO()
        engine = self.engine()
        with contextlib.redirect_stderr(output):
            result = engine.start("Implement greet", self.plan(), pause_after_plan=True)
        self.assertEqual(result["status"], "awaiting_approval")
        diagnostic = next(line for line in output.getvalue().splitlines() if "主检出" in line)
        self.assertIn("00-line\\nbreak-\\x1b.txt", diagnostic)
        self.assertIn("01-笔记.txt", diagnostic)
        self.assertNotIn("\x1b", diagnostic)
        self.assertNotIn("x" * 245, diagnostic)
        self.assertLess(len(diagnostic), 2600)
        self.assertIn("另有 5 项", diagnostic)
        for name in names:
            self.assertEqual((self.repo / name).read_text(), "preserve\n")
            self.assertFalse((engine.worktree / name).exists())
