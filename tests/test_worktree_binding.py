import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.memory import Memory, validate_proposals
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class WorktreeBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git.git(self.repo, "init", "-b", "main")
        (self.repo / "a.txt").write_text("original\n")
        git.git(self.repo, "add", "a.txt")
        git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        self.worktree = self.root / "worktrees" / "run"
        git.git(self.repo, "worktree", "add", "-b", "harness/run", str(self.worktree))
        self.binding = git.capture_worktree_binding(self.worktree, "harness/run")
        self.base = git.head(self.repo)
        self.main_index = (self.repo / ".git" / "index").read_bytes()

    def assert_main_unchanged(self):
        self.assertEqual(git.head(self.repo), self.base)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), self.main_index)
        self.assertEqual((self.repo / "a.txt").read_text(), "original\n")

    def assert_bound_operations_blocked(self):
        archive = self.root / "archive"
        operations = [
            lambda: git.head(self.worktree, binding=self.binding),
            lambda: git.snapshot(self.worktree, binding=self.binding),
            lambda: git.check_scope(self.worktree, ["a.txt"], binding=self.binding),
            lambda: git.accept(self.worktree, ["a.txt"], "run", "task", binding=self.binding),
            lambda: git.archive_and_reset(self.worktree, self.base, archive, self.worktree.parent,
                                          binding=self.binding),
            lambda: git.assert_removable_worktree(self.worktree, binding=self.binding),
        ]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaisesRegex(HarnessError, "绑定"):
                operation()
        self.assertFalse(archive.exists())
        self.assert_main_unchanged()

    def test_bound_accept_advances_only_the_run_branch_and_survives_serialization(self):
        saved = self.root / "binding.json"
        write_json(saved, self.binding)
        binding = read_json(saved)
        (self.worktree / "a.txt").write_text("reviewed\n")
        paths = git.check_scope(self.worktree, ["a.txt"], binding=binding)
        accepted = git.accept(self.worktree, paths, "run", "task", binding=binding)
        self.assertNotEqual(accepted, self.base)
        self.assertEqual(git.git(self.repo, "rev-parse", "harness/run").strip(), accepted)
        self.assertTrue(git.clean(self.worktree, binding=binding))
        self.assert_main_unchanged()

    def test_repointing_gitfile_to_main_blocks_read_accept_reset_and_removal(self):
        (self.worktree / "a.txt").write_text("unreviewed\n")
        (self.worktree / ".git").write_text(f"gitdir: {self.repo / '.git'}\n")
        self.assert_bound_operations_blocked()
        self.assertEqual(git.git(self.repo, "rev-parse", "harness/run").strip(), self.base)

    def test_gitfile_symlink_is_rejected_even_with_identical_bytes(self):
        metadata = self.worktree / ".git"
        copied = self.root / "copied.gitfile"
        copied.write_bytes(metadata.read_bytes())
        metadata.unlink()
        metadata.symlink_to(copied)
        self.assert_bound_operations_blocked()

    def test_gitfile_directory_is_rejected(self):
        metadata = self.worktree / ".git"
        metadata.unlink()
        metadata.mkdir()
        with self.assertRaisesRegex(HarnessError, "绑定"):
            git.accept(self.worktree, [], "run", "task", binding=self.binding)
        self.assert_main_unchanged()

    def test_branch_redirect_at_unchanged_commit_is_rejected(self):
        git.git(self.worktree, "symbolic-ref", "HEAD", "refs/heads/main")
        self.assert_bound_operations_blocked()

    def test_nested_symbolic_branch_redirect_is_rejected(self):
        git.git(self.repo, "symbolic-ref", "refs/heads/harness/run", "refs/heads/main")
        self.assert_bound_operations_blocked()

    def test_changed_backlink_or_common_directory_is_rejected(self):
        admin = Path(self.binding["git_dir"])
        for name, content in (("gitdir", os.fsencode(self.repo / ".git") + b"\n"),
                              ("commondir", b"../../other\n")):
            metadata = admin / name
            original = metadata.read_bytes()
            with self.subTest(name=name):
                metadata.write_bytes(content)
                try:
                    self.assert_bound_operations_blocked()
                finally:
                    metadata.write_bytes(original)

    def test_admin_directory_replaced_by_symlink_is_rejected(self):
        admin = Path(self.binding["git_dir"])
        moved = admin.with_name(admin.name + "-moved")
        admin.rename(moved)
        admin.symlink_to(moved, target_is_directory=True)
        self.assert_bound_operations_blocked()

    def test_capture_uses_actual_admin_directory_when_names_collide(self):
        other = self.root / "other" / "run"
        git.git(self.repo, "worktree", "add", "-b", "harness/other", str(other))
        binding = git.capture_worktree_binding(other, "harness/other")
        self.assertNotEqual(binding["git_dir"], self.binding["git_dir"])
        self.assertNotEqual(Path(binding["git_dir"]).name, other.name)
        git.check_worktree_binding(other, binding)
        with self.assertRaisesRegex(HarnessError, "分支绑定"):
            git.capture_worktree_binding(other, "harness/run")

    def test_ambient_git_overrides_cannot_redirect_index_or_worktree(self):
        (self.worktree / "a.txt").write_text("reviewed\n")
        env = {"GIT_DIR": str(self.repo / ".git"), "GIT_WORK_TREE": str(self.repo),
               "GIT_INDEX_FILE": str(self.repo / ".git" / "index"),
               "GIT_COMMON_DIR": str(self.root / "missing")}
        with patch.dict(os.environ, env):
            accepted = git.accept(self.worktree, ["a.txt"], "run", "task", binding=self.binding)
        self.assertNotEqual(accepted, self.base)
        self.assert_main_unchanged()

    def test_command_stays_pinned_if_gitfile_changes_after_precheck(self):
        (self.worktree / "a.txt").write_text("reviewed\n")
        subprocess_run = git.subprocess.run

        def redirect_before_commit(argv, **kwargs):
            if "commit" in argv:
                (self.worktree / ".git").write_text(f"gitdir: {self.repo / '.git'}\n")
            return subprocess_run(argv, **kwargs)

        with patch("harness.git.subprocess.run", side_effect=redirect_before_commit):
            with self.assertRaisesRegex(HarnessError, "绑定"):
                git.accept(self.worktree, ["a.txt"], "run", "task", binding=self.binding)
        self.assert_main_unchanged()
        self.assertNotEqual(git.git(self.repo, "rev-parse", "harness/run").strip(), self.base)

    def test_detached_verification_binding_requires_unchanged_detached_commit(self):
        detached = self.root / "verify"
        git.git(self.repo, "worktree", "add", "--detach", str(detached), self.base)
        binding = git.capture_worktree_binding(detached, None)
        self.assertEqual(git.head(detached, binding=binding), self.base)
        git.git(detached, "symbolic-ref", "HEAD", "refs/heads/main")
        with self.assertRaisesRegex(HarnessError, "detached HEAD 绑定"):
            git.snapshot(detached, binding=binding)
        self.assert_main_unchanged()

    def test_memory_provenance_read_and_promotion_cannot_follow_redirected_gitfile(self):
        memory = Memory(self.root / "memory.sqlite3", repo=self.worktree, binding=self.binding)
        proposal = {"key": "fixture", "abstract": "Fixture source", "content": "The source is a.txt",
                    "source_paths": ["a.txt"]}
        validated = validate_proposals([proposal], self.worktree)
        ident = memory.remember_proposals(validated, ["fixture"], self.base, "run", "task")[0]
        self.assertFalse(memory.read(ident)["possibly_stale"])
        (self.worktree / ".git").write_text(f"gitdir: {self.repo / '.git'}\n")
        with self.assertRaisesRegex(HarnessError, "绑定"):
            memory.read(ident)
        with self.assertRaisesRegex(HarnessError, "绑定"):
            memory.check_proposals(validated, ["fixture"], self.base)
        self.assert_main_unchanged()


class EngineWorktreeBindingTests(RepoCase):
    def test_worker_redirect_is_preserved_and_blocks_resume_and_removal(self):
        base = git.head(self.repo)

        def redirect(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                (kwargs["cwd"] / ".git").write_text(f"gitdir: {self.repo / '.git'}\n")

        backend = ScriptedBackend(redirect)
        engine = self.engine(backend)
        main_index = (self.repo / ".git" / "index").read_bytes()
        with self.assertRaisesRegex(HarnessError, "绑定"):
            engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertFalse(engine.run["accepted"])
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(engine.run["total_attempts"], 1)
        self.assertEqual(git.head(self.repo), base)
        self.assertEqual(git.git(self.repo, "rev-parse", engine.run["branch"]).strip(), base)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), main_index)
        self.assertFalse((self.repo / "hello.py").exists())
        for operation in (lambda: self.engine(backend).resume(engine.run["id"]),
                          lambda: self.engine(backend).abandon(engine.run["id"], remove_worktree=True)):
            with self.assertRaisesRegex(HarnessError, "绑定"):
                operation()
        self.assertEqual(len(backend.calls), 1)
        self.assertTrue((engine.worktree / "hello.py").is_file())

    def test_pre_accept_redirect_cannot_commit_to_main(self):
        engine = self.engine()
        original = engine.hooks.emit
        base = git.head(self.repo)
        main_index = (self.repo / ".git" / "index").read_bytes()

        def redirect(event, payload, logs):
            original(event, payload, logs)
            if event == "pre_accept":
                (engine.worktree / ".git").write_text(f"gitdir: {self.repo / '.git'}\n")

        with patch.object(engine.hooks, "emit", side_effect=redirect):
            with self.assertRaisesRegex(HarnessError, "绑定"):
                engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertFalse(engine.run["accepted"])
        self.assertEqual(git.head(self.repo), base)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), main_index)
        self.assertEqual(git.git(self.repo, "rev-parse", engine.run["branch"]).strip(), base)


if __name__ == "__main__":
    unittest.main()
