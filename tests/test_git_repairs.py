import errno
import os
import tempfile
import unittest
from pathlib import Path

from harness import git
from harness.util import HarnessError, read_json, write_json


class GitBoundaryRepairsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git.git(self.repo, "init", "-b", "main")
        for name in ("src/root.py", "src/nested/leaf.py", "outside.txt"):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("original\n")
        git.git(self.repo, "add", ".")
        git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                "-c", "commit.gpgsign=false", "commit", "-m", "fixture")

    def test_single_segment_glob_cannot_authorize_nested_change(self):
        (self.repo / "src/nested/leaf.py").write_text("changed\n")
        for scope in (["src/*"], ["*.py"], ["leaf.py"]):
            with self.subTest(scope=scope), self.assertRaisesRegex(HarnessError, "越界"):
                git.check_scope(self.repo, scope)
        self.assertEqual(git.check_scope(self.repo, ["src/**/*.py"]), ["src/nested/leaf.py"])
        (self.repo / "src/root.py").write_text("changed root\n")
        self.assertEqual(git.check_scope(self.repo, ["src/**/*.py"]),
                         ["src/nested/leaf.py", "src/root.py"])
        with self.assertRaisesRegex(HarnessError, "越界"):
            git.check_scope(self.repo, ["nested/**/*.py"])

    def test_character_classes_and_question_marks_stay_in_one_segment(self):
        nested = self.repo / "src/nested/leaf.py"
        nested.write_text("changed\n")
        self.assertEqual(git.check_scope(self.repo, ["src/n?sted/[!x]eaf.[py][py]"]),
                         ["src/nested/leaf.py"])
        for pattern in ("src/*leaf.py", "src/?/leaf.py", "src/nested/[!l]eaf.py"):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(HarnessError, "越界"):
                git.check_scope(self.repo, [pattern])

    def test_scope_validator_and_runtime_protect_metadata_roots(self):
        for pattern in ("/src/*", "../src", "src/../outside.txt", "./src/*", "src//*.py",
                        ".git", ".git/config", ".harness/**", ".codex/*", ".claude"):
            with self.subTest(pattern=pattern), self.assertRaises(HarnessError):
                git.validate_scope_pattern(pattern)
        for pattern in ("**", "*", ".*", "src/**", ".gitignore", " file name "):
            git.validate_scope_pattern(pattern)
        for root in (".harness", ".codex", ".claude"):
            path = self.repo / root
            path.write_text("protected\n")
            with self.subTest(root=root), self.assertRaisesRegex(HarnessError, "受保护"):
                git.check_scope(self.repo, ["**"])
            path.unlink()
            path.mkdir()
            (path / "config").write_text("protected\n")
            with self.subTest(root=root + "/config"), self.assertRaisesRegex(HarnessError, "受保护"):
                git.check_scope(self.repo, ["**"])
            (path / "config").unlink()
            path.rmdir()

    def test_deletion_and_rename_require_both_paths_in_scope(self):
        (self.repo / "src/root.py").unlink()
        self.assertEqual(git.check_scope(self.repo, ["src/*"]), ["src/root.py"])
        (self.repo / "outside.txt").unlink()
        with self.assertRaisesRegex(HarnessError, "越界"):
            git.check_scope(self.repo, ["src/**"])
        git.git(self.repo, "reset", "--hard", "HEAD")
        (self.repo / "outside.txt").rename(self.repo / "src/moved.py")
        with self.assertRaisesRegex(HarnessError, "越界"):
            git.check_scope(self.repo, ["src/**"])
        (self.repo / "src/moved.py").rename(self.repo / "outside.txt")
        (self.repo / "src/root.py").rename(self.repo / "renamed.py")
        with self.assertRaisesRegex(HarnessError, "越界"):
            git.check_scope(self.repo, ["src/**"])
        (self.repo / "renamed.py").rename(self.repo / "src/renamed.py")
        paths = git.check_scope(self.repo, ["src/*"])
        self.assertEqual(paths, ["src/renamed.py", "src/root.py"])
        git.accept(self.repo, paths, "run_rename", "rename")
        self.assertTrue(git.clean(self.repo))
        self.assertFalse((self.repo / "src/root.py").exists())
        self.assertEqual((self.repo / "src/renamed.py").read_text(), "original\n")

    def test_scope_does_not_strip_real_filename_spaces(self):
        (self.repo / " name .txt ").write_text("exact name\n")
        with self.assertRaisesRegex(HarnessError, "越界"):
            git.check_scope(self.repo, ["name .txt"])
        self.assertEqual(git.check_scope(self.repo, [" name .txt "]), [" name .txt "])

    def test_snapshot_diagnostics_distinguish_added_modified_and_deleted(self):
        before = git.snapshot(self.repo)
        (self.repo / ".coverage").write_bytes(b"generated\0report")
        (self.repo / "src/root.py").write_text("modified\n")
        (self.repo / "outside.txt").unlink()
        after = git.snapshot(self.repo)
        self.assertEqual(git.snapshot_changes(before, after), {
            "added": [".coverage"], "modified": ["src/root.py"], "deleted": ["outside.txt"],
            "index_changed": False, "head_changed": False,
        })
        git.git(self.repo, "add", "src/root.py")
        staged = git.snapshot(self.repo)
        self.assertEqual(git.snapshot_changes(after, staged), {
            "added": [], "modified": [], "deleted": [],
            "index_changed": True, "head_changed": False,
        })
        git.accept(self.repo, git.changed(self.repo), "run_diagnostics", "diagnostics")
        accepted = git.snapshot(self.repo)
        self.assertTrue(git.snapshot_changes(staged, accepted)["head_changed"])

    def test_tracked_file_replaced_by_directory_can_be_reviewed_and_accepted(self):
        before = git.snapshot(self.repo)
        path = self.repo / "src/root.py"
        path.unlink()
        path.mkdir()
        (path / "main.py").write_text("replacement module\n")
        after = git.snapshot(self.repo)
        self.assertEqual(git.snapshot_changes(before, after), {
            "added": ["src/root.py/main.py"], "modified": [], "deleted": ["src/root.py"],
            "index_changed": False, "head_changed": False,
        })
        paths = git.check_scope(self.repo, ["src/**"])
        git.accept(self.repo, paths, "run_directory", "directory")
        self.assertTrue(git.clean(self.repo))
        self.assertEqual(git.git(self.repo, "show", "HEAD:src/root.py/main.py"), "replacement module\n")

    def test_snapshot_rejects_newly_staged_gitlink_even_without_directory(self):
        git.git(self.repo, "update-index", "--add", "--cacheinfo", "160000", git.head(self.repo), "submodule")
        with self.assertRaisesRegex(HarnessError, "不支持 submodule：submodule"):
            git.snapshot(self.repo)

    def test_tracked_directory_replaced_by_file_records_deleted_descendants(self):
        before = git.snapshot(self.repo)
        path = self.repo / "src/nested"
        (path / "leaf.py").unlink()
        path.rmdir()
        path.write_text("replacement file\n")
        after = git.snapshot(self.repo)
        self.assertEqual(git.snapshot_changes(before, after), {
            "added": ["src/nested"], "modified": [], "deleted": ["src/nested/leaf.py"],
            "index_changed": False, "head_changed": False,
        })
        paths = git.check_scope(self.repo, ["src/**"])
        git.accept(self.repo, paths, "run_file", "file")
        self.assertTrue(git.clean(self.repo))
        self.assertEqual(git.git(self.repo, "show", "HEAD:src/nested"), "replacement file\n")

    def test_accept_uses_literal_filenames_instead_of_git_pathspec_wildcards(self):
        (self.repo / "literal*.txt").write_text("reviewed\n")
        (self.repo / "literal-secret.txt").write_text("not reviewed\n")
        commit = git.accept(self.repo, ["literal*.txt"], "run_literal", "literal")
        self.assertEqual(git.git(self.repo, "show", f"{commit}:literal*.txt"), "reviewed\n")
        self.assertEqual(git.changed(self.repo), ["literal-secret.txt"])

    def test_accept_discards_staged_new_file_deleted_before_review_without_commit(self):
        before = git.head(self.repo)
        path = self.repo / "src/discarded.py"
        path.write_text("staged but absent at review\n")
        git.git(self.repo, "add", "src/discarded.py")
        path.unlink()
        paths = git.check_scope(self.repo, ["src/*"])
        self.assertEqual(paths, ["src/discarded.py"])
        self.assertEqual(git.accept(self.repo, paths, "run_deleted", "deleted"), before)
        self.assertTrue(git.clean(self.repo))

    def test_accept_mixed_index_and_worktree_changes_commits_only_reviewed_contents(self):
        root = self.repo / "src/root.py"
        root.write_text("hidden staged contents\n")
        discarded = self.repo / "src/discarded.py"
        discarded.write_text("staged but absent at review\n")
        git.git(self.repo, "add", "src/root.py", "src/discarded.py")
        root.write_text("original\n")
        discarded.unlink()
        (self.repo / "src/nested/leaf.py").unlink()
        (self.repo / "src/reviewed.py").write_text("reviewed new contents\n")
        (self.repo / "src/link").symlink_to("missing-target")
        paths = git.check_scope(self.repo, ["src/**"])
        commit = git.accept(self.repo, paths, "run_mixed", "mixed")
        self.assertEqual(git.git(self.repo, "show", f"{commit}:src/root.py"), "original\n")
        self.assertEqual(git.git(self.repo, "show", f"{commit}:src/reviewed.py"), "reviewed new contents\n")
        self.assertEqual(git.git(self.repo, "show", f"{commit}:src/link"), "missing-target")
        self.assertEqual(set(git.git(self.repo, "diff", "--name-only", "HEAD^", "HEAD").splitlines()),
                         {"src/nested/leaf.py", "src/reviewed.py", "src/link"})
        self.assertFalse(discarded.exists())
        self.assertFalse((self.repo / "src/nested/leaf.py").exists())
        self.assertTrue(git.clean(self.repo))

    def test_non_utf8_paths_and_link_targets_survive_snapshot_and_acceptance(self):
        names = [os.fsdecode(b"src/name_\xff.py"), os.fsdecode(b"src/name_\xfe.py")]
        for index, name in enumerate(names):
            try:
                (self.repo / name).write_bytes(bytes((255 - index,)) + b"\n")
            except OSError as error:
                if error.errno == errno.EILSEQ:
                    self.skipTest("this filesystem rejects non-UTF-8 filenames")
                raise
        target = os.fsdecode(b"missing_\xff")
        (self.repo / "src/link").symlink_to(target)
        paths = git.check_scope(self.repo, ["src/*"])
        self.assertEqual(set(paths), {*names, "src/link"})
        snapshot = git.snapshot(self.repo)
        self.assertIn(names[0], snapshot["files"])
        self.assertIn(names[1], snapshot["files"])
        self.assertNotEqual(snapshot["files"][names[0]], snapshot["files"][names[1]])
        saved = self.root / "snapshot.json"
        write_json(saved, snapshot)
        self.assertEqual(read_json(saved), snapshot)
        git.accept(self.repo, paths, "run_bytes", "bytes")
        self.assertTrue(git.clean(self.repo))
        self.assertEqual(os.readlink(self.repo / "src/link"), target)
        tracked = git.git_bytes(self.repo, "ls-files", "-z").split(b"\0")
        self.assertIn(os.fsencode(names[0]), tracked)
        self.assertIn(os.fsencode(names[1]), tracked)

    def test_non_utf8_index_paths_remain_distinct_in_scope_and_snapshot(self):
        names = [os.fsdecode(b"src/index_\xff.py"), os.fsdecode(b"src/index_\xfe.py")]
        blob = git.git(self.repo, "hash-object", "-w", "src/root.py").strip()
        for name in names:
            git.git(self.repo, "update-index", "--add", "--cacheinfo", "100644", blob, name)
        self.assertEqual(set(git.check_scope(self.repo, ["src/*"])), set(names))
        snapshot = git.snapshot(self.repo)
        self.assertTrue(set(names) <= snapshot["files"].keys())
        saved = self.root / "index-snapshot.json"
        write_json(saved, snapshot)
        self.assertEqual(read_json(saved), snapshot)

    def test_lossless_diff_can_restore_non_utf8_and_binary_contents(self):
        base = git.head(self.repo)
        (self.repo / "src/root.py").write_bytes(b"non-UTF8 \xff\n")
        (self.repo / "src/blob.bin").write_bytes(b"\0binary\xff\xfe")
        name = "src/other.py"
        (self.repo / name).write_bytes(b"new text \xfe\n")
        raw = git.diff_bytes(self.repo)
        self.assertIn(b"non-UTF8 \xff", raw)
        self.assertIn(b"new text \xfe", raw)
        self.assertIn(b"GIT binary patch", raw)
        self.assertIn("\ufffd", git.diff(self.repo))
        git.accept(self.repo, git.changed(self.repo), "run_bytes", "bytes")
        whole = git.range_diff_bytes(self.repo, base)
        self.assertIn(b"non-UTF8 \xff", whole)
        self.assertIn("\ufffd", git.range_diff(self.repo, base))
        evidence = self.root / "evidence.diff"
        evidence.write_bytes(whole)
        git.git(self.repo, "reset", "--hard", base)
        git.git(self.repo, "apply", "--binary", str(evidence))
        self.assertEqual((self.repo / "src/root.py").read_bytes(), b"non-UTF8 \xff\n")
        self.assertEqual((self.repo / "src/blob.bin").read_bytes(), b"\0binary\xff\xfe")
        self.assertEqual((self.repo / name).read_bytes(), b"new text \xfe\n")

    def test_archive_keeps_non_utf8_evidence_and_restores_worktree(self):
        owned = self.root / "worktrees"
        owned.mkdir()
        worktree = owned / "worker"
        git.git(self.repo, "worktree", "add", "-b", "worker", str(worktree))
        expected = git.head(worktree)
        name = "src/rejected.py"
        (worktree / "src/root.py").write_bytes(b"staged \xff\n")
        git.git(worktree, "add", "src/root.py")
        (worktree / name).write_bytes(b"untracked \xfe\n")
        archive = self.root / "archive"
        before = git.snapshot(worktree)
        git.archive_and_reset(worktree, expected, archive, owned)
        self.assertEqual(read_json(archive / "snapshot.json"), before)
        self.assertIn(b"staged \xff", (archive / "staged.diff").read_bytes())
        self.assertIn(b"untracked \xfe", (archive / "change.diff").read_bytes())
        self.assertEqual((archive / "files" / name).read_bytes(), b"untracked \xfe\n")
        self.assertEqual(git.head(worktree), expected)
        self.assertTrue(git.clean(worktree))
        self.assertFalse((worktree / name).exists())

    def test_archive_restores_file_and_directory_replacements_without_losing_evidence(self):
        worktree = self.root / "worktrees" / "worker"
        git.git(self.repo, "worktree", "add", "-b", "worker", str(worktree))
        binding = git.capture_worktree_binding(worktree, "worker")
        expected = git.head(worktree, binding=binding)
        directory = worktree / "src/nested"
        (directory / "leaf.py").unlink()
        directory.rmdir()
        directory.write_text("replacement file\n")
        file = worktree / "src/root.py"
        file.unlink()
        file.mkdir()
        (file / "main.py").write_text("replacement module\n")
        before = git.snapshot(worktree, binding=binding)
        archive = self.root / "replacements"
        git.archive_and_reset(worktree, expected, archive, worktree.parent, binding=binding)
        self.assertEqual(read_json(archive / "snapshot.json"), before)
        self.assertEqual((archive / "files/src/nested").read_text(), "replacement file\n")
        self.assertEqual((archive / "files/src/root.py/main.py").read_text(), "replacement module\n")
        self.assertEqual((worktree / "src/nested/leaf.py").read_text(), "original\n")
        self.assertEqual((worktree / "src/root.py").read_text(), "original\n")
        self.assertTrue(git.clean(worktree, binding=binding))
        self.assertEqual(git.head(worktree, binding=binding), expected)


if __name__ == "__main__":
    unittest.main()
