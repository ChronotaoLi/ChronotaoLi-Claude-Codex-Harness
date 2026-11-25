"""Native controls for the completion image primitives (design 1, case 10)."""
import errno
import hashlib
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.util import HarnessError
from test_git_invocation import IDENTITY, IsolatedRepoCase, plain_git


class WorktreeImageTests(IsolatedRepoCase):
    def setUp(self):
        super().setUp()
        (self.repo / ".gitattributes").write_text("filtered filter=fixture\nexisting-crlf -text\n")
        (self.repo / ".gitignore").write_text("build/\n")
        (self.repo / "tracked").write_bytes(b"old\n")
        (self.repo / "filtered").write_bytes(b"original\n")
        (self.repo / "existing-crlf").write_bytes(b"old\r\n")
        self.plain(self.repo, "config", "core.autocrlf", "false")
        self.plain(self.repo, "add", "--", ".gitattributes", ".gitignore", "tracked", "filtered", "existing-crlf")
        self.plain(self.repo, *IDENTITY, "commit", "-qm", "image fixture")
        (self.repo / ".gitattributes").write_text("filtered filter=fixture\nexisting-crlf text=auto\n")
        self.plain(self.repo, "add", "--", ".gitattributes")
        self.plain(self.repo, *IDENTITY, "commit", "-qm", "enable text auto with a preexisting CRLF blob")
        self.commit = self.plain(self.repo, "rev-parse", "HEAD").decode().strip()
        self.tree = self.plain(self.repo, "rev-parse", "HEAD^{tree}").decode().strip()
        self.worktree = self.scratch / "image"
        self.binding = git.add_worktree(self.repo, self.worktree, self.commit, branch="image")
        self.admin = Path(self.binding["git_dir"])

    def plain(self, repo, *args, **kwargs):
        return plain_git(repo, *args, **kwargs).stdout

    def fingerprint(self):
        common = Path(self.binding["common_dir"])
        return self.files(
            self.worktree, common / "index", common / "packed-refs", common / "refs", common / "logs",
            common / "HEAD", self.admin / "index", self.admin / "HEAD", self.admin / "ORIG_HEAD",
            self.admin / "refs", self.admin / "logs",
        )

    def control(self):
        """Plain Git from the same commit, in a separate, previously absent index."""
        index = self.scratch / "control-index"
        index.unlink(missing_ok=True)
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        self.plain(self.worktree, "read-tree", self.commit, env=env)
        added = plain_git(self.worktree, "add", "-A", check=False, env=env)
        if added.returncode:
            return None, added.stderr.decode("utf-8", errors="replace").splitlines()[0]
        return self.plain(self.worktree, "write-tree", env=env).decode().strip(), None

    def assert_image(self, *names, failed=False):
        before = self.fingerprint()
        observed = []
        launch = git._launch

        def observe(argv, env, *, effect, **kwargs):
            observed.append((argv, env.get("GIT_INDEX_FILE"), effect))
            return launch(argv, env, effect=effect, **kwargs)

        with patch.object(git, "_launch", observe):
            result = git.prove_image(self.worktree, self.commit, self.tree, binding=self.binding)
        self.assertEqual(self.fingerprint(), before)
        image, error = self.control()
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(result.detail, error)
        self.assertEqual(result.equal, not (names or failed))
        self.assertEqual(set(result.paths), set(names))
        self.assertEqual(len(result.paths), len(names))
        if failed:
            self.assertIsNotNone(error)
        elif names:
            self.assertNotEqual(image, self.tree)
            self.assertIsNone(error)
            paths = self.plain(self.worktree, "diff-tree", "-r", "--name-only", "--no-renames", "-z", self.tree, image)
            self.assertEqual(tuple(os.fsdecode(p) for p in paths.split(b"\0") if p), result.paths)
        else:
            self.assertEqual(image, self.tree)
            self.assertIsNone(error)
        commands = [(argv, index, effect) for argv, index, effect in observed
                    if argv[-3:] != ["symbolic-ref", "-q", "HEAD"] and "--get-regexp" not in argv]
        expected = [(["read-tree", self.commit], git.PRIVATE), (["add", "-A"], git.PRIVATE)]
        if not failed:
            expected.append((["write-tree"], git.PRIVATE))
        if names:
            expected.append((["diff-tree", "-r", "--name-only", "--no-renames", "--no-ext-diff", "--no-textconv", "-z",
                              self.tree, image], git.READ))
        self.assertEqual(len(commands), len(expected))
        index_files = set()
        for (argv, index, effect), (args, expected_effect) in zip(commands, expected, strict=True):
            self.assertEqual(argv[-len(args):], args)
            self.assertIs(effect, expected_effect)
            self.assertTrue(effect.interruptible)
            self.assertIn("--git-dir=" + self.binding["git_dir"], argv)
            self.assertIn("--work-tree=" + self.binding["worktree"], argv)
            if effect is git.PRIVATE:
                self.assertIsNotNone(index)
                index_files.add(Path(index))
            else:
                self.assertIsNone(index)
        private_index, = index_files
        self.assertFalse(private_index.parent.exists())
        self.assertNotEqual(private_index, self.admin / "index")

    def test_hidden_and_same_stat_changes_reach_the_image(self):
        path = self.worktree / "tracked"
        info = path.stat()
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                self.plain(self.worktree, "update-index", flag, "tracked")
                path.write_bytes(b"new\n")
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                self.assert_image("tracked")
                path.write_bytes(b"old\n")
                self.plain(self.worktree, "update-index", "--no-" + flag.removeprefix("--"), "tracked")
        path.write_bytes(b"new\n")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), (info.st_size, info.st_mtime_ns))
        self.assert_image("tracked")

    def test_untracked_paths_and_nested_repository(self):
        name = "odd \t\n\udcff"
        try:
            (self.worktree / name).write_bytes(b"file")
        except OSError as error:
            if error.errno != errno.EILSEQ:
                raise
            name = "odd \t\n雪"
            (self.worktree / name).write_bytes(b"file")
        (self.worktree / "link").symlink_to("missing")
        nested = self.worktree / "nested"
        nested.mkdir()
        self.plain(nested, "init", "-q")
        self.plain(nested, *IDENTITY, "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                   "commit", "--allow-empty", "-qm", "nested fixture")
        self.assert_image(name, "link", "nested")

    def test_real_index_listing_includes_stage_only_bytes_and_is_read_once(self):
        path = self.worktree / "odd\t\n雪"
        path.write_bytes(b"staged only")
        self.plain(self.worktree, "add", "--", path.name)
        path.unlink()
        listing = self.plain(self.worktree, "ls-files", "--stage", "-z")
        before = self.fingerprint()
        with patch.object(git, "git_bytes", wraps=git.git_bytes) as calls:
            snapshot, raw = git.read_worktree(self.worktree, binding=self.binding)
        reads = [call for call in calls.call_args_list if call.args[1:] == ("ls-files", "--stage", "-z")]
        self.assertEqual(len(reads), 1)
        self.assertIs(reads[0].kwargs["effect"], git.READ)
        self.assertIsNone(reads[0].kwargs.get("index_file"))
        self.assertIn(b" 0\t" + os.fsencode(path.name) + b"\0", raw)
        self.assertEqual(raw, listing)
        self.assertEqual(snapshot["index"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(snapshot["files"][path.name].startswith("deleted:"))
        self.assertEqual(snapshot, git.snapshot(self.worktree, binding=self.binding))
        self.assertEqual(self.fingerprint(), before)
        self.assert_image()

    def test_ignored_build_output_and_untracked_fifo_are_absent(self):
        build = self.worktree / "build"
        build.mkdir()
        (build / "product").write_bytes(b"ignored")
        os.mkfifo(self.worktree / "pipe")
        self.assert_image()

    def test_executable_bit_follows_core_filemode(self):
        (self.worktree / "tracked").chmod(0o755)
        self.plain(self.worktree, "config", "core.fileMode", "true")
        self.assert_image("tracked")
        self.plain(self.worktree, "config", "core.fileMode", "false")
        self.assert_image()

    def test_reversible_filter_uses_git_conversion_even_for_exact_blob_bytes(self):
        self.plain(self.worktree, "config", "filter.fixture.clean", "tr a-z n-za-m")
        self.plain(self.worktree, "config", "filter.fixture.smudge", "tr a-z n-za-m")
        self.plain(self.worktree, "config", "filter.fixture.required", "true")
        path = self.worktree / "filtered"
        path.unlink()
        self.plain(self.worktree, "checkout-index", "--", "filtered")
        self.assertEqual(path.read_bytes(), b"bevtvany\n")
        self.assert_image()
        blob = self.plain(self.worktree, "cat-file", "blob", "HEAD:filtered")
        path.write_bytes(blob)
        self.assert_image("filtered")

    def test_crlf_is_preserved_if_the_committed_blob_already_has_crlf(self):
        self.assertEqual(self.plain(self.worktree, "cat-file", "blob", "HEAD:existing-crlf"), b"old\r\n")
        self.plain(self.worktree, "config", "core.autocrlf", "true")
        self.assert_image()

    def test_required_filter_failure_is_evidence_from_add(self):
        self.plain(self.worktree, "config", "filter.fixture.clean", "false")
        self.plain(self.worktree, "config", "filter.fixture.required", "true")
        self.assert_image(failed=True)

    def test_unequal_trees_with_no_leaf_difference_do_not_prove_equal(self):
        empty = self.plain(self.worktree, "mktree", input=b"").strip()
        entries = self.plain(self.worktree, "ls-tree", self.tree)
        extra = self.plain(self.worktree, "mktree", input=entries + b"040000 tree " + empty + b"\tempty-dir\n")
        extra_tree = extra.decode().strip()
        self.assertNotEqual(extra_tree, self.tree)
        self.assertEqual(self.plain(self.worktree, "diff-tree", "-r", "--name-only", "--no-renames", "-z",
                                    extra_tree, self.tree), b"")
        commit = self.plain(self.worktree, *IDENTITY, "commit-tree", extra_tree, "-p", self.commit,
                            input=b"explicit empty subtree\n").decode().strip()
        run_git = git.git_process

        def substitute_written_tree(repo, *args, **kwargs):
            result = run_git(repo, *args, **kwargs)
            if args == ("write-tree",):
                self.assertEqual(result.returncode, 0, result.stderr)
                # A clean add can retain the subtree in the cache tree. Exercise the
                # comparison with the independently built tree that drops it.
                return subprocess.CompletedProcess(result.args, 0, (self.tree + "\n").encode(), result.stderr)
            return result

        before = self.fingerprint()
        with patch.object(git, "git_process", substitute_written_tree):
            proof = git.prove_image(self.worktree, commit, extra_tree, binding=self.binding)
        self.assertEqual(self.fingerprint(), before)
        self.assertFalse(proof.equal)
        self.assertEqual(proof.paths, ())
        self.assertIn(extra_tree, proof.detail)
        self.assertIn(self.tree, proof.detail)

    def test_proof_is_refused_in_a_read_only_scope(self):
        before = self.fingerprint()
        with git.read_only(), patch.object(git, "_launch", wraps=git._launch) as launch:
            with self.assertRaises(HarnessError):
                git.prove_image(self.worktree, self.commit, self.tree, binding=self.binding)
        launch.assert_not_called()
        self.assertEqual(self.fingerprint(), before)
