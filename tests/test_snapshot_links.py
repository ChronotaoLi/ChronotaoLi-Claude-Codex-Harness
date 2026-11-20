import git_isolation  # first: no Git process of a test run may read the developer's Git configuration
import errno
import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import git, stops, util
from harness.util import HarnessError, read_json
from test_command_directory import closed, interrupting
from test_harness import ROOT, RepoCase
from test_provider_failures import FifoWatchdog, saved
from test_runtime_changes import ScriptedBackend


def snapshot_before_descriptor_walk(repo, *, binding=None):
    """git.snapshot as it was before it read through descriptors; kept only to compare legitimate candidates."""
    index = git.git_bytes(repo, "ls-files", "--stage", "-z", effect=git.READ, binding=binding)
    git._reject_indexed_submodules(index)
    result = {"head": git.head(repo, binding=binding), "index": hashlib.sha256(index).hexdigest(),
              "files": {}}
    names = git.git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard", effect=git.READ,
                    binding=binding).split("\0")
    for name in sorted(set(names) - {""}):
        path = repo / name
        if path.is_symlink():
            data = os.fsencode(os.readlink(path))
            mode = "link"
        elif path.is_file():
            data = path.read_bytes()
            mode = str(path.stat().st_mode & 0o777)
        elif path.is_dir():
            data, mode = b"", "deleted"
        else:
            data, mode = b"", "deleted"
        result["files"][name] = mode + ":" + hashlib.sha256(data).hexdigest()
    return result


def digest(data):
    return hashlib.sha256(data).hexdigest()


def open_descriptors():
    return len(os.listdir("/dev/fd"))


FILES = {"a.txt": b"alpha\n", "bin/run.sh": b"#!/bin/sh\necho hi\n", "deep/x/y/z.txt": b"zed\n", "empty.txt": b"",
         "space name.txt": b"s\n", "名字.txt": b"n\n", "src/a.txt": b"tree bytes\n", "src/b.txt": b"b\n",
         "src.txt": b"sibling\n", "src-x/c.txt": b"c\n"}
OUTSIDE = b"outside bytes\n"


class SnapshotDescriptorWalkTests(unittest.TestCase):
    """A bound run worktree whose candidate the snapshot reads; ``outside`` holds an a.txt of its own."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.tree, self.binding, self.outside = self.fixture("run")

    def fixture(self, name):
        root = self.base / name
        repo, outside = root / "repo", root / "outside"
        repo.mkdir(parents=True)
        outside.mkdir()
        (outside / "a.txt").write_bytes(OUTSIDE)
        git.git(repo, "init", "-b", "main")
        for path, data in FILES.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_bytes(data)
        os.chmod(repo / "bin" / "run.sh", 0o755)
        for path, target in (("link-in", "a.txt"), ("link-out", str(outside)), ("dangling", "nowhere")):
            os.symlink(target, repo / path)
        git.git(repo, "add", ".")
        git.git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false",
                "commit", "-m", "fixture")
        tree = root / "worktrees" / "run"
        git.git(repo, "worktree", "add", "-b", "harness/run", str(tree))
        os.chmod(tree / "deep" / "x" / "y" / "z.txt", 0o600)
        return tree, git.capture_worktree_binding(tree, "harness/run"), outside

    def assert_stop(self, error, kind, reason):
        self.assertEqual((error.stop["kind"], error.stop["reason"]), (kind, reason))

    def after_the_last_listing(self, change):
        """Patch Git's launcher so that ``change()`` runs once a snapshot's last listing has returned."""
        real = git._launch

        def launch(argv, env, **kwargs):
            result = real(argv, env, **kwargs)
            if "--others" in argv:
                change()
            return result
        return patch.object(git, "_launch", launch)

    def test_legitimate_candidates_snapshot_as_before(self):
        def untracked(tree, outside):
            (tree / "new.txt").write_bytes(b"new\n")
            (tree / "newdir" / "deeper").mkdir(parents=True)
            (tree / "newdir" / "n.txt").write_bytes(b"n\n")
            (tree / "newdir" / "deeper" / "m.txt").write_bytes(b"m\n")
            os.symlink("src", tree / "linkdir")
            (tree / "nested").mkdir()
            git.git(tree / "nested", "init", "-b", "main")
            (tree / "nested" / "inner.txt").write_bytes(b"i\n")

        def edited(tree, outside):
            (tree / "a.txt").write_bytes(b"changed\n")
            (tree / "deep" / "x" / "y" / "z.txt").write_bytes(b"zed changed\n")
            os.chmod(tree / "src" / "b.txt", 0o755)
            os.remove(tree / "empty.txt")

        def replaced(tree, outside):
            os.remove(tree / "src.txt")
            (tree / "src.txt").mkdir()
            (tree / "src.txt" / "inner.txt").write_bytes(b"i\n")
            os.remove(tree / "space name.txt")
            os.mkfifo(tree / "space name.txt")
            os.remove(tree / "link-in")
            os.symlink("src/b.txt", tree / "link-in")
            os.remove(tree / "dangling")
            os.symlink(str(outside / "a.txt"), tree / "dangling")
            # A link's text is filesystem bytes: a non-UTF-8 target survives (filesystems may refuse such names).
            os.symlink(b"raw-\xff\xfe", os.fsencode(tree / "raw-link"))

        def search_only(tree, outside):
            os.chmod(tree / "deep" / "x", 0o311)
            self.addCleanup(os.chmod, tree / "deep" / "x", 0o755)

        def ancestor_file(tree, outside):
            shutil.rmtree(tree / "src-x")
            (tree / "src-x").write_bytes(b"now a file\n")

        def ancestor_fifo(tree, outside):
            shutil.rmtree(tree / "src-x")
            os.mkfifo(tree / "src-x")

        layouts = {"unchanged": lambda tree, outside: None, "untracked files, directories and links": untracked,
                   "content, mode and deletion": edited, "directory, FIFO and relinked leaves": replaced,
                   "search-only ancestor": search_only, "ancestor replaced by a file": ancestor_file,
                   "ancestor replaced by a FIFO": ancestor_fifo}
        for number, (label, mutate) in enumerate(layouts.items()):
            with self.subTest(label):
                tree, binding, outside = self.fixture(f"layout-{number}")
                mutate(tree, outside)
                baseline = open_descriptors()
                bound = git.snapshot(tree, binding=binding)
                self.assertEqual(open_descriptors(), baseline)
                self.assertEqual(bound, snapshot_before_descriptor_walk(tree, binding=binding))
                self.assertEqual(git.snapshot(tree), snapshot_before_descriptor_walk(tree))
                self.assertEqual(bound["files"]["link-out"], "link:" + digest(os.fsencode(str(outside))))
                if mutate is replaced:
                    self.assertEqual(bound["files"]["raw-link"], "link:" + digest(b"raw-\xff\xfe"))
                if label.startswith("ancestor replaced"):
                    self.assertTrue(bound["files"]["src-x/c.txt"].startswith("deleted:"))

    def test_an_ancestor_replaced_by_a_link_out_of_the_worktree_is_deleted_as_git_reports(self):
        before = git.snapshot(self.tree, binding=self.binding)
        shutil.rmtree(self.tree / "src")
        os.symlink(self.outside, self.tree / "src")
        linked = git.snapshot(self.tree, binding=self.binding)
        self.assertEqual({name: linked["files"][name] for name in ("src", "src/a.txt", "src/b.txt")},
                         {"src": "link:" + digest(os.fsencode(str(self.outside))),
                          "src/a.txt": "deleted:" + digest(b""), "src/b.txt": "deleted:" + digest(b"")})
        status = git.git(self.tree, "status", "--porcelain").splitlines()
        self.assertEqual([line for line in status if "src" in line and "src." not in line and "src-" not in line],
                         [" D src/a.txt", " D src/b.txt", "?? src"])
        # What lies behind the link is no part of the candidate.
        (self.outside / "a.txt").write_bytes(b"outside changed\n")
        self.assertEqual(git.snapshot(self.tree, binding=self.binding), linked)
        os.remove(self.tree / "src")
        git.git(self.tree, "checkout", "--", "src")
        self.assertEqual(git.snapshot(self.tree, binding=self.binding), before)

    def test_an_ancestor_swapped_after_it_opened_is_read_through_the_opened_directory(self):
        real_open, moved = util.Descriptors.open, self.base / "moved"
        # The entry as it was, mode included: the checkout's modes follow the umask.
        before = git.snapshot(self.tree, binding=self.binding)["files"]["src/a.txt"]
        self.assertEqual(before.partition(":")[2], digest(FILES["src/a.txt"]))

        def open_then_swap(held, path, flags, *, dir_fd=None):
            descriptor = real_open(held, path, flags, dir_fd=dir_fd)
            if path == "src" and dir_fd is not None and not moved.exists():
                os.rename(self.tree / "src", moved)
                os.symlink(self.outside, self.tree / "src")
            return descriptor

        with patch.object(util.Descriptors, "open", open_then_swap):
            raced = git.snapshot(self.tree, binding=self.binding)
        self.assertTrue(moved.is_dir() and (self.tree / "src").is_symlink())
        self.assertEqual(raced["files"]["src/a.txt"], before)
        self.assertEqual(git.snapshot(self.tree, binding=self.binding)["files"]["src/a.txt"], "deleted:" + digest(b""))

    def test_an_entry_changing_type_while_read_stops_as_candidate_changed(self):
        real_stat, real_open = os.stat, util.Descriptors.open

        def after_stat(name, change):
            done = []

            def stat(path, *args, dir_fd=None, follow_symlinks=True):
                info = real_stat(path, *args, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
                if path == name and dir_fd is not None and not done:
                    done.append(change())
                return info
            return patch("harness.git.os.stat", side_effect=stat), done

        def refused_while_directory(held, path, flags, *, dir_fd=None):
            if path == "src" and dir_fd is not None:
                raise NotADirectoryError(errno.ENOTDIR, "Not a directory", path)
            return real_open(held, path, flags, dir_fd=dir_fd)

        for number, label in enumerate(("leaf file becomes a FIFO", "leaf link becomes a file",
                                         "ancestor open refused while a directory")):
            with self.subTest(label):
                tree, binding, _ = self.fixture(f"change-{number}")
                fifo, link, watchdog = tree / "a.txt", tree / "link-in", None
                if number == 0:
                    swap, done = after_stat("a.txt", lambda: (os.remove(fifo), os.mkfifo(fifo)))
                    watchdog = FifoWatchdog(fifo)
                elif number == 1:
                    swap, done = after_stat("link-in", lambda: (os.remove(link), link.write_bytes(b"file\n")))
                else:
                    swap, done = patch.object(util.Descriptors, "open", refused_while_directory), [True]
                try:
                    with swap, self.assertRaises(HarnessError) as caught:
                        git.snapshot(tree, binding=binding)
                finally:
                    hung = watchdog is not None and watchdog.stop()
                self.assertTrue(done)
                self.assertFalse(hung)
                self.assert_stop(caught.exception, "workspace_mismatch", "candidate_changed")

    def test_an_ancestor_that_cannot_be_read_is_no_deletion(self):
        real_open = util.Descriptors.open

        def failing(held, path, flags, *, dir_fd=None):
            if path == "x" and dir_fd is not None:
                raise OSError(errno.EIO, "Input/output error", path)
            return real_open(held, path, flags, dir_fd=dir_fd)

        with patch.object(util.Descriptors, "open", failing), self.assertRaises(OSError) as caught:
            git.snapshot(self.tree, binding=self.binding)
        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertEqual((stops.of(caught.exception)["kind"], stops.of(caught.exception)["reason"]),
                         ("runtime_failure", "io"))
        if os.geteuid() == 0:
            self.skipTest("root searches any directory")
        os.chmod(self.tree / "deep", 0)
        self.addCleanup(os.chmod, self.tree / "deep", 0o755)
        for binding in (self.binding, None):
            with self.subTest(bound=binding is not None), self.assertRaises(PermissionError) as caught:
                git.snapshot(self.tree, binding=binding)
            self.assertEqual(stops.of(caught.exception)["data"]["error_type"], "PermissionError")

    def test_a_search_only_bound_root_snapshots_as_a_readable_one(self):
        # Git lists below a root it may only search, so the walk from that root must not need to read it either.
        before = git.snapshot(self.tree, binding=self.binding)
        os.chmod(self.tree, 0o311)
        self.addCleanup(os.chmod, self.tree, 0o755)
        baseline = open_descriptors()
        self.assertEqual(git.snapshot(self.tree, binding=self.binding), before)
        self.assertEqual(open_descriptors(), baseline)
        self.assertEqual(git.snapshot(self.tree), before)
        self.assertEqual(snapshot_before_descriptor_walk(self.tree, binding=self.binding), before)

    def test_a_root_swapped_for_a_link_after_the_last_binding_check_stops_as_binding_changed(self):
        impostor, parked = self.base / "impostor", self.base / "parked"
        impostor.mkdir()
        shutil.copyfile(self.tree / ".git", impostor / ".git")
        (impostor / "a.txt").write_bytes(OUTSIDE)
        real, real_open = git._open_snapshot_root, os.open

        def swap():
            os.rename(self.tree, parked)
            os.symlink(impostor, self.tree)

        # As the check on the root begins (its path comparison refuses the link), and as the root itself opens,
        # after that comparison: not following the link refuses it before any descriptor of the impostor exists.
        for moment in ("as the check begins", "as the root opens"):
            with self.subTest(moment):
                opened = []

                def recorded(path, flags, *args, **kwargs):
                    if moment == "as the root opens" and path == self.binding["worktree"] and not self.tree.is_symlink():
                        swap()
                    descriptor = real_open(path, flags, *args, **kwargs)
                    opened.append(path)
                    return descriptor

                def checked(held, worktree, binding):
                    if moment == "as the check begins":
                        swap()
                    with patch("harness.util.os.open", side_effect=recorded):
                        return real(held, worktree, binding)

                try:
                    with patch.object(git, "_open_snapshot_root", checked), self.assertRaises(HarnessError) as caught:
                        git.snapshot(self.tree, binding=self.binding)
                finally:
                    if self.tree.is_symlink():
                        os.remove(self.tree)
                        os.rename(parked, self.tree)
                self.assert_stop(caught.exception, "workspace_mismatch", "binding_changed")
                self.assertEqual(opened, [])

    def test_the_listing_s_failure_is_reported_after_the_binding_check_that_follows_it(self):
        # git_process's check closing the last listing comes before git_bytes reads that listing's status.
        real, gitfile = git._launch, self.tree / ".git"
        content = gitfile.read_bytes()
        self.addCleanup(gitfile.write_bytes, content)
        for changed in (False, True):
            with self.subTest(binding_changed=changed):
                def failing(argv, env, **kwargs):
                    if "--others" not in argv:
                        return real(argv, env, **kwargs)
                    if changed:
                        gitfile.write_bytes(content + b"\n")
                    return subprocess.CompletedProcess(argv, 128, b"", b"fatal: listing failed")

                with patch.object(git, "_launch", failing), self.assertRaises(HarnessError) as caught:
                    git.snapshot(self.tree, binding=self.binding)
                gitfile.write_bytes(content)
                if changed:
                    self.assert_stop(caught.exception, "workspace_mismatch", "binding_changed")
                else:
                    self.assertIsNone(caught.exception.stop)
                    self.assertIn("fatal: listing failed", str(caught.exception))

    def test_a_root_failing_after_the_last_listing_stops_as_that_listing_s_failure(self):
        # The path check closing the last listing meets the failure before the walk's root opens, and git_bytes
        # wraps it as that listing's own failure, as it did before the snapshot read through descriptors.
        real_lstat, real_open, failing = Path.lstat, util.Descriptors.open, []

        def lstat(path):
            if failing and path == failing[0]:
                raise OSError(errno.EIO, "Input/output error", str(path))
            return real_lstat(path)

        def open_(held, path, flags, *, dir_fd=None):
            if failing and path == ".git" and dir_fd is not None:
                raise OSError(errno.EIO, "Input/output error", path)
            return real_open(held, path, flags, dir_fd=dir_fd)

        cases = {"every read of .git fails": (errno.EIO, lambda tree: failing.append(tree / ".git")),
                 "root without permissions": (errno.EACCES, lambda tree: os.chmod(tree, 0)),
                 "root readable but not searchable": (errno.EACCES, lambda tree: os.chmod(tree, 0o600))}
        for number, (label, (code, change)) in enumerate(cases.items()):
            with self.subTest(label):
                if code == errno.EACCES and os.geteuid() == 0:
                    self.skipTest("root searches any directory")
                tree, binding, _ = self.fixture(f"failing-root-{number}")
                self.addCleanup(os.chmod, tree, 0o755)
                failing.clear()
                with self.after_the_last_listing(lambda: change(tree)), patch.object(Path, "lstat", lstat), \
                        patch.object(util.Descriptors, "open", open_), self.assertRaises(HarnessError) as caught:
                    git.snapshot(tree, binding=binding)
                result = stops.of(caught.exception)
                self.assertEqual((result["kind"], result["reason"], caught.exception.__cause__.errno),
                                 ("unclassified", "untyped_harness_error", code))

    def test_a_gitfile_swapped_for_an_unreadable_fifo_after_the_last_listing_stops_as_binding_changed(self):
        # The path check closing the last listing sees by lstat that .git is no regular file, without opening it.
        gitfile = self.tree / ".git"

        def unreadable_fifo():
            os.remove(gitfile)
            os.mkfifo(gitfile)
            os.chmod(gitfile, 0)

        with self.after_the_last_listing(unreadable_fifo), self.assertRaises(HarnessError) as caught:
            git.snapshot(self.tree, binding=self.binding)
        self.assert_stop(caught.exception, "workspace_mismatch", "binding_changed")

    def test_an_interrupt_once_a_descriptor_is_opened_leaves_none_open(self):
        real_open, real_root = os.open, git._open_snapshot_root

        def interrupted(at):
            """Send SIGINT once the ``at``-th descriptor of the snapshot exists, before its caller stores it."""
            opened = []

            def open_(path, flags, *args, dir_fd=None, **kwargs):
                descriptor = real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)
                if dir_fd is not None or path == self.binding["worktree"]:
                    opened.append(descriptor)
                    if len(opened) == at:
                        signal.raise_signal(signal.SIGINT)
                return descriptor
            return patch("harness.util.os.open", side_effect=open_), opened

        def root_then_interrupt(*args):
            root = real_root(*args)
            signal.raise_signal(signal.SIGINT)
            return root

        recording, every = interrupted(0)
        baseline = open_descriptors()
        with recording:
            git.snapshot(self.tree, binding=self.binding)
        # The root, its .git, each directory once and each regular file.
        self.assertEqual(len(every), 2 + 6 + 10)
        self.assertEqual(open_descriptors(), baseline)
        for at in range(1, len(every) + 1):
            with self.subTest(at=at):
                recording, opened = interrupted(at)
                with interrupting(), recording, self.assertRaises(KeyboardInterrupt):
                    git.snapshot(self.tree, binding=self.binding)
                self.assertEqual(len(opened), at)
                self.assertTrue(all(map(closed, opened)))
                self.assertEqual(open_descriptors(), baseline)
        with self.subTest("after the root opened"), interrupting(), \
                patch.object(git, "_open_snapshot_root", root_then_interrupt), self.assertRaises(KeyboardInterrupt):
            git.snapshot(self.tree, binding=self.binding)
        self.assertEqual(open_descriptors(), baseline)


class UnreadableAncestorLifecycleTests(RepoCase):
    def test_a_candidate_ancestor_without_permission_stops_the_run_as_local_io(self):
        if os.geteuid() == 0:
            self.skipTest("root searches any directory")
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "data.txt").write_text("tracked\n")
        git.git(self.repo, "add", "pkg/data.txt")
        git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false",
                "commit", "-m", "package fixture")
        backend = ScriptedBackend()
        engine = self.engine(backend)
        engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"), pause_after_plan=True)
        locked = engine.worktree / "pkg"
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        with self.assertRaises(PermissionError):
            self.engine(backend).resume(engine.run["id"], approve_plan=True)
        state = saved(engine)
        self.assertEqual((state["status"], state["stop"]["kind"], state["stop"]["reason"],
                          state["stop"]["data"]["error_type"]), ("blocked", "runtime_failure", "io", "PermissionError"))
        # The worker's dispatch stopped at its candidate snapshot, before any call or budget.
        worker = [entry for entry in state["dispatches"] if entry["role"] == "worker"]
        self.assertEqual([(entry["invoked"], "snapshot_digest" in entry) for entry in worker], [(False, False)])
        self.assertEqual(backend.calls, [])
        self.assertEqual(state["agent_dispatches"], 0)


if __name__ == "__main__":
    unittest.main()
