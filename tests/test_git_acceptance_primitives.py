"""D1-A1 acceptance primitives, with independent Git and flock controls."""
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.util import HarnessError
from test_git_invocation import IDENTITY, IsolatedRepoCase, Markers, plain_git


class AcceptancePrimitiveTests(IsolatedRepoCase):
    def setUp(self):
        super().setUp()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "old").write_text("tracked\n")
        (self.repo / "x").write_text("unchanged\n")
        plain_git(self.repo, "add", "--", "x", "src")
        plain_git(self.repo, *IDENTITY, "commit", "-qm", "parent fixture")
        self.parent = self.plain(self.repo, "rev-parse", "HEAD").decode().strip()
        self.worktree = self.scratch / "bound"
        self.binding = git.add_worktree(self.repo, self.worktree, self.parent, branch="run")
        self.ref = self.binding["branch"]
        self.index = Path(self.binding["git_dir"]) / "index"
        self.message = git.commit_message_text({"subject": "A café", "body": "  body\n\n next  "}, "run", "task")

    def plain(self, repo, *args, **kwargs):
        return plain_git(repo, *args, **kwargs).stdout

    def build(self, files, allowed=("**",)):
        return git.build_commit(self.worktree, self.parent, files, allowed, "task", self.message, binding=self.binding)

    def control_tree(self, *args):
        env = dict(os.environ, GIT_INDEX_FILE=str(self.scratch / "independent-index"))
        self.plain(self.worktree, "read-tree", self.parent, env=env)
        self.plain(self.worktree, *args, env=env)
        return self.plain(self.worktree, "write-tree", env=env).decode().strip()

    def test_candidate_compares_to_independent_git_and_preserves_index_and_checkout(self):
        name = "a\udcff"
        try:
            (self.worktree / name).write_bytes(b"reviewed\n")
        except OSError as error:
            if error.errno != errno.EILSEQ:
                raise
            name = "a-雪"
            (self.worktree / name).write_bytes(b"reviewed\n")
        paths = ["literal*.txt", "gone", "src/old", name, "link", "exec"]
        for path in ("literal*.txt", "literal-decoy.txt", "gone", "exec"):
            (self.worktree / path).write_bytes(b"reviewed\n")
        (self.worktree / "exec").chmod(0o755)
        (self.worktree / "link").symlink_to("missing\udcfe")
        (self.worktree / "src" / "old").unlink()
        self.plain(self.worktree, "add", "--", "gone", "literal-decoy.txt")
        (self.worktree / "gone").unlink()
        (self.worktree / "literal-decoy.txt").write_bytes(b"now different\n")
        debug_before = self.plain(self.worktree, "ls-files", "--debug", "x")
        before = self.index.read_bytes()
        worktree = self.files(self.worktree, skip=(".git",))
        expected = self.control_tree("--literal-pathspecs", "add", "--", *[p for p in paths if p != "gone"])
        marks = Markers(self.scratch)
        marks.arm(self.repo, scopes=("repo",), programs=False)
        marks.reset()
        calls = []
        launch = git._launch

        def observe(argv, env, *, effect, **kwargs):
            calls.append((argv, effect, env.get("GIT_INDEX_FILE"), kwargs))
            return launch(argv, env, effect=effect, **kwargs)

        with patch.object(git, "_launch", observe):
            built = self.build(paths)
        self.assertEqual(built.tree, expected)
        commit = self.plain(self.worktree, "cat-file", "commit", built.commit)
        header, message = commit.split(b"\n\n", 1)
        self.assertEqual(message, self.message.encode())
        lines = header.splitlines()
        self.assertEqual(lines[:2], [f"tree {built.tree}".encode(), f"parent {self.parent}".encode()])
        for role, line in zip((b"author", b"committer"), lines[2:], strict=True):
            self.assertTrue(line.startswith(role + b" Personal Harness <harness@localhost> "), line)
        main_calls = [(argv, effect, index, kw) for argv, effect, index, kw in calls
                      if argv[-3:] != ["symbolic-ref", "-q", "HEAD"] and "--get-regexp" not in argv]
        expected_calls = [
            (["read-tree", self.parent], git.PRIVATE, True, None, False),
            (["ls-files", "-z"], git.READ, True, None, False),
            (["update-index", "-z", "--force-remove", "--stdin"], git.PRIVATE, True, b"src/old\0", False),
            (["add", "--", name, "exec", "link", "literal*.txt"], git.PRIVATE, True, None, True),
            (["write-tree"], git.PRIVATE, True, None, False),
            (["ls-files", "--stage", "-z"], git.READ, True, None, False),
            (["rev-parse", "--verify", f"{self.parent}^{{tree}}"], git.READ, False, None, False),
            (["diff-tree", "-r", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", self.parent, built.tree],
             git.READ, False, None, False),
            (["commit-tree", built.tree, "-p", self.parent, "-F", "-"], git.PRIVATE, False, message, False),
        ]
        self.assertEqual(marks.read(), [])
        indexes = set()
        for (argv, effect, index, kw), (args, kind, private, data, literal) in zip(
                main_calls, expected_calls, strict=True):
            self.assertEqual(argv[-len(args):], args)
            self.assertIs(effect, kind)
            self.assertEqual(index is not None, private)
            if index:
                indexes.add(Path(index))
            self.assertEqual(kw["input"], data)
            self.assertEqual("--literal-pathspecs" in argv[:-len(args)], literal)
            self.assertEqual(kw.get("pass_fds", ()), ())
        private_index, = indexes
        self.assertFalse(private_index.parent.exists())
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(self.files(self.worktree, skip=(".git",)), worktree)
        self.assertEqual(git.head(self.worktree, binding=self.binding), self.parent)

        # The real-index write is compared to an independently read stage and leaves the checkout alone.
        with (self.scratch / "run.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = git.sync_index(self.worktree, built.commit, lock_fd=lock.fileno(), binding=self.binding)
        self.assertEqual(result.returncode, 0, result.stderr)
        stage = self.plain(self.worktree, "ls-files", "--stage", "-z")
        self.assertEqual(hashlib.sha256(stage).hexdigest(), built.index_synced)
        debug = self.plain(self.worktree, "ls-files", "--debug", "x")
        self.assertEqual(debug, debug_before)
        self.assertEqual(self.files(self.worktree, skip=(".git",)), worktree)
        self.assertEqual(marks.read(), [])

    def test_replaced_ancestor_matches_independent_add_all(self):
        outside = self.scratch / "outside-source"
        outside.mkdir()
        (outside / "old").write_text("must never be followed")
        src = self.worktree / "src"
        (src / "old").unlink()
        src.rmdir()
        for kind in ("link", "ignored link", "file"):
            with self.subTest(kind=kind):
                if kind == "ignored link":
                    exclude = Path(self.binding["common_dir"]) / "info" / "exclude"
                    with exclude.open("a") as stream:
                        stream.write("\n/src\n")
                elif kind == "file":
                    exclude.write_text("")
                if kind == "file":
                    src.write_text("new ancestor")
                else:
                    src.symlink_to(outside, target_is_directory=True)
                paths = git.check_scope(self.worktree, ["**"], binding=self.binding, task_id="task")
                self.assertIn("src/old", paths)
                self.assertEqual("src" in paths, kind != "ignored link")
                built = self.build(paths)
                self.assertEqual(built.tree, self.control_tree("add", "-A"))
                files = self.plain(self.worktree, "ls-tree", "-r", "--name-only", built.tree).splitlines()
                self.assertNotIn(b"src/old", files)
                self.assertEqual(b"src" in files, kind != "ignored link")
                src.unlink()

    def test_tree_gate_and_unrecordable_paths(self):
        x = self.worktree / "x"
        launch = git._launch

        def replace(argv, env, *, effect, **kwargs):
            if argv[-3:] == ["add", "--", "x"]:
                x.unlink()
                x.mkdir()
                (x / "new").write_text("arrived after classification")
            self.assertNotIn("commit-tree", argv)
            return launch(argv, env, effect=effect, **kwargs)

        for allowed, kind, reason, data in (
            (["x"], "scope_violation", "candidate_outside_scope", {"path": "x/new"}),
            (["x/**"], "workspace_mismatch", "candidate_changed", {}),
        ):
            with self.subTest(allowed=allowed), patch.object(git, "_launch", replace):
                with self.assertRaises(HarnessError) as raised:
                    self.build(["x"], allowed)
                self.assertEqual(raised.exception.stop, dict(kind=kind, reason=reason, data=data, source={"task_id": "task"}))
                (x / "new").unlink()
                x.rmdir()
                x.write_text("edited")
        x.unlink()
        x.mkdir()
        (x / "new").write_text("before classification")
        built = self.build(["x"], ["x"])
        self.assertNotIn(b"x", self.plain(self.worktree, "ls-tree", "-r", "--name-only", built.tree).splitlines())
        (x / "new").unlink()
        x.rmdir()
        os.mkfifo(x)
        nested = self.worktree / "sub"
        nested.mkdir()
        self.plain(nested, "init", "-q")
        for path, reason in (("x", "special_file"), ("sub/", "candidate_outside_scope")):
            with self.subTest(path=path), self.assertRaises(git.ScopeViolation) as raised:
                self.build([path])
            self.assertEqual(raised.exception.stop, dict(kind="scope_violation", reason=reason,
                                                         data={"path": path}, source={"task_id": "task"}))
        self.assertEqual(git.head(self.worktree, binding=self.binding), self.parent)

    def test_zero_diff_and_read_primitives(self):
        missing = "f" * len(self.parent)
        built = self.build(["gone"])
        tree = self.plain(self.worktree, "rev-parse", f"{self.parent}^{{tree}}").decode().strip()
        self.assertEqual(built, (tree, self.parent, git.index_digest(self.worktree, binding=self.binding)))
        (self.worktree / "x").write_text("candidate")
        candidate = self.build(["x"])
        self.assertEqual(git.object_kinds(self.worktree, [self.parent, tree, candidate.commit, missing],
                                          binding=self.binding),
                         {self.parent: "commit", tree: "tree", candidate.commit: "commit", missing: None})
        self.assertEqual(git.commit_header(self.worktree, candidate.commit, binding=self.binding),
                         (candidate.tree, (self.parent,)))
        self.assertEqual(git.commit_header(self.worktree, self.parent, binding=self.binding).tree, tree)
        self.assertIsNone(git.tree_readable(self.worktree, tree, binding=self.binding))
        with self.assertRaises(HarnessError):
            git.tree_readable(self.worktree, missing, binding=self.binding)
        alias = "refs/heads/alias"
        self.plain(self.worktree, "symbolic-ref", alias, self.ref)
        self.assertEqual(git.branch_state(self.worktree, alias, binding=self.binding), (True, None))
        self.assertEqual(git.branch_state(self.worktree, self.ref, binding=self.binding), (False, self.parent))
        self.plain(self.worktree, "update-ref", "-d", self.ref)
        self.assertEqual(git.branch_state(self.worktree, self.ref, binding=self.binding), (False, None))

    def test_launch_failures_stay_distinct_from_reset_body_and_binding_mismatch(self):
        launch = git._launch
        for error in (OSError(errno.EAGAIN, "launch failure"), subprocess.TimeoutExpired(["git"], 60)):
            for phase in ("binding", "reset"):
                def fail(argv, env, *, effect, **kwargs):
                    if (phase == "binding" and "--git-dir=" + self.binding["git_dir"] in argv
                            and argv[-3:] == ["symbolic-ref", "-q", "HEAD"]):
                        raise error
                    if phase == "reset" and argv[-4:] == ["reset", "-q", "--hard", "--no-recurse-submodules"]:
                        raise error
                    return launch(argv, env, effect=effect, **kwargs)

                with self.subTest(error=type(error), phase=phase), patch.object(git, "_launch", fail):
                    if phase == "binding":
                        with self.assertRaises(git.GitLaunchError) as raised:
                            git.check_worktree_binding(self.worktree, self.binding)
                    else:
                        new = self.scratch / "reset-fails"
                        with self.assertRaises(HarnessError) as raised:
                            git.add_worktree(self.repo, new, self.parent)
                        self.assertIs(type(raised.exception), HarnessError)
                        self.assertIsNone(raised.exception.stop)
                        self.assertFalse(new.exists())
                    self.assertIs(raised.exception.__cause__, error)
        self.plain(self.worktree, "symbolic-ref", self.ref, "refs/heads/main")
        with self.assertRaises(HarnessError) as raised:
            git.check_worktree_binding(self.worktree, self.binding)
        self.assertIs(type(raised.exception), HarnessError)
        self.assertEqual(raised.exception.stop["reason"], "binding_changed")

    def test_native_cas_at_symbolic_ref_race_keeps_main(self):
        (self.worktree / "x").write_text("candidate")
        candidate = self.build(["x"])
        other = self.plain(self.repo, "rev-parse", f"{self.parent}^").decode().strip()
        launch = git._launch
        native_status = []

        def race(argv, env, *, effect, **kwargs):
            if effect is git.REFS:
                self.plain(self.worktree, "symbolic-ref", self.ref, "refs/heads/main")
            result = launch(argv, env, effect=effect, **kwargs)
            if effect is git.REFS:
                native_status.append(result.returncode)
            return result

        with (self.scratch / "run.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for main in (other, self.parent):
                with self.subTest(main=main):
                    self.plain(self.repo, "update-ref", "refs/heads/main", main)
                    self.plain(self.repo, "update-ref", "--no-deref", self.ref, self.parent)
                    args = (self.worktree, self.ref, candidate.commit, self.parent, "task")
                    kwargs = dict(binding=self.binding, lock_fd=lock.fileno())
                    with patch.object(git, "_launch", race):
                        if main == self.parent:
                            self.assertEqual(git.move_branch(*args, **kwargs).returncode, 0)
                        else:
                            with self.assertRaises(HarnessError) as raised:
                                git.move_branch(*args, **kwargs)
                            self.assertEqual(raised.exception.stop["reason"], "binding_changed")
                    self.assertEqual(self.plain(self.repo, "rev-parse", "refs/heads/main").strip(), main.encode())
        self.assertNotEqual(native_status[0], 0)
        self.assertEqual(native_status[1], 0)

    def test_write_primitives_support_direct_calls_without_a_lock(self):
        (self.worktree / "x").write_text("candidate")
        candidate = self.build(["x"])
        checkout = self.files(self.worktree, skip=(".git",))
        index_env = dict(os.environ, GIT_INDEX_FILE=str(self.scratch / "expected-index"))
        self.plain(self.worktree, "read-tree", "--reset", candidate.commit, env=index_env)
        expected_stage = self.plain(self.worktree, "ls-files", "--stage", "-z", env=index_env)
        launch = git._launch
        writes = []

        def observe(argv, env, *, effect, **kwargs):
            passed = kwargs.get("pass_fds", ())
            if effect in (git.REFS, git.INDEX):
                writes.append((effect, passed))
            else:
                self.assertEqual(passed, ())
            return launch(argv, env, effect=effect, **kwargs)

        for lock_args, expected_fds in (({}, ()), ({"lock_fd": None}, ())):
            with self.subTest(lock_args=lock_args):
                self.plain(self.worktree, "update-ref", "--no-deref", self.ref, self.parent)
                self.plain(self.worktree, "read-tree", "--reset", self.parent)
                initial_stage = self.plain(self.worktree, "ls-files", "--stage", "-z")
                self.assertNotEqual(initial_stage, expected_stage)
                writes.clear()
                with patch.object(git, "_launch", observe):
                    rejected = git.move_branch(self.worktree, self.ref, candidate.commit, candidate.commit,
                                               "task", binding=self.binding, **lock_args)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertEqual(self.plain(self.worktree, "rev-parse", self.ref).strip(), self.parent.encode())

                    moved = git.move_branch(self.worktree, self.ref, candidate.commit, self.parent,
                                            "task", binding=self.binding, **lock_args)
                    self.assertEqual(moved.returncode, 0, moved.stderr)
                    self.assertEqual(self.plain(self.worktree, "rev-parse", self.ref).strip(),
                                     candidate.commit.encode())
                    self.assertEqual(self.plain(self.worktree, "ls-files", "--stage", "-z"), initial_stage)

                    synced = git.sync_index(self.worktree, candidate.commit, binding=self.binding, **lock_args)
                    self.assertEqual(synced.returncode, 0, synced.stderr)

                self.assertEqual(writes, [(git.REFS, expected_fds), (git.REFS, expected_fds),
                                          (git.INDEX, expected_fds)])
                self.assertEqual(self.plain(self.worktree, "ls-files", "--stage", "-z"), expected_stage)
                self.assertEqual(self.files(self.worktree, skip=(".git",)), checkout)

    def test_launch_closes_temporary_descriptors_even_when_start_fails_or_times_out(self):
        program = """
import errno, fcntl, os, subprocess, sys, time
from unittest.mock import patch
from harness import git

def descriptors():
    result = {}
    for entry in os.listdir('/dev/fd'):
        fd = int(entry)
        try:
            info = os.fstat(fd)
        except OSError as error:
            assert error.errno == errno.EBADF, error
            continue
        result[fd] = (info.st_dev, info.st_ino, os.get_inheritable(fd))
    return result

def launch(argv, *, input=None, timeout=5, fds=(0,)):
    return git._launch(argv, os.environ, effect=git.REFS, input=input,
                       deadline=time.monotonic() + timeout, pass_fds=fds)

with open(sys.argv[1], 'a+b') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.dup2(lock.fileno(), 0, inheritable=False)
    before = descriptors()
    echo = [sys.executable, '-c', 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); '
                                   'sys.stderr.write("captured")']
    for input in (None, b'input data'):
        result = launch(echo, input=input)
        assert (result.returncode, result.stdout, result.stderr) == (0, input or b'', b'captured'), result
        assert descriptors() == before, descriptors()

    try:
        launch([sys.argv[1] + '.missing'])
    except FileNotFoundError as error:
        assert error.errno == errno.ENOENT, error
    else:
        raise AssertionError('missing executable started')
    assert descriptors() == before, descriptors()

    try:
        launch([sys.executable, '-c', 'import time; time.sleep(5)'], timeout=.1)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError('sleeping child did not time out')
    assert descriptors() == before, descriptors()

    # Let the first real duplication succeed, then simulate exhausting the descriptor limit.
    duplicate = fcntl.fcntl
    def fail_second(fd, command, minimum):
        if fd == 1:
            raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))
        return duplicate(fd, command, minimum)

    with patch.object(fcntl, 'fcntl', fail_second):
        try:
            launch(echo, fds=(0, 1))
        except OSError as error:
            assert error.errno == errno.EMFILE, error
        else:
            raise AssertionError('duplication failure did not propagate')
    assert descriptors() == before, descriptors()

    os.close(0)
    before = descriptors()
    try:
        launch(echo)
    except OSError as error:
        assert error.errno == errno.EBADF, error
    else:
        raise AssertionError('closed descriptor was accepted')
    assert descriptors() == before, descriptors()
"""
        result = subprocess.run([sys.executable, "-c", program, str(self.scratch / "run.lock")],
                                cwd=Path(git.__file__).resolve().parents[1], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_only_main_writes_inherit_the_live_flock(self):
        (self.worktree / "x").write_text("candidate")
        candidate = self.build(["x"])
        markers = Markers(self.scratch)
        markers.arm(self.repo, scopes=("repo",), programs=False)
        markers.reset()
        child_code = """
import errno, json, os, pathlib, sys, time
ready, release, name, inherit, *argv = sys.argv[1:]
identity = os.stat(name)
matching = []
for entry in os.listdir('/dev/fd'):
    fd = int(entry)
    try:
        if os.path.samestat(os.fstat(fd), identity):
            matching.append(fd)
    except OSError as error:
        assert error.errno == errno.EBADF, error
assert len(matching) == int(inherit == 'True'), matching
assert all(fd >= 3 for fd in matching), matching
assert os.read(0, 1) == b''
pending = pathlib.Path(ready + '.tmp')
pending.write_text(json.dumps(dict(pid=os.getpid(), matching=matching,
                                 device=identity.st_dev, inode=identity.st_ino)))
pending.replace(ready)
deadline = time.monotonic() + 20
while not pathlib.Path(release).exists():
    if time.monotonic() > deadline:
        raise TimeoutError('test did not release Git')
    time.sleep(.01)
if matching:
    os.execvpe(argv[0], argv, os.environ)
"""
        harness_code = """
import fcntl, json, os, pathlib, sys
from unittest.mock import patch
from harness import git
mode, binding, parent, commit, path, ready, release, inherit, fd, child = sys.argv[1:]
fd = int(fd)
binding = json.loads(binding)
repo = pathlib.Path(binding['worktree'])
effect_under_test = git.REFS if mode == 'ref' else git.INDEX
launch = git._launch
with open(path, 'a+b') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.dup2(lock.fileno(), fd, inheritable=False)

    def wrap(argv, env, *, effect, **kwargs):
        passed = kwargs.get('pass_fds', ())
        if effect == effect_under_test:
            assert passed == (fd,), passed
            if inherit == 'False':
                kwargs['pass_fds'] = ()
            argv = [sys.executable, '-c', child, ready, release, path, inherit, *argv]
        else:
            assert passed == (), (argv, passed)
        return launch(argv, env, effect=effect, **kwargs)

    with patch.object(git, '_launch', wrap):
        if mode == 'ref':
            git.move_branch(repo, binding['branch'], commit, parent, 'task', binding=binding, lock_fd=fd)
        else:
            git.sync_index(repo, commit, binding=binding, lock_fd=fd)
"""
        competitor = """
import fcntl, sys
with open(sys.argv[1], 'rb') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(3)
"""
        cases = ((mode, fd, inherit) for mode in ("ref", "index")
                 for fd, inherit in ((0, True), (1, True), (2, True), (9, True), (9, False)))
        for mode, fd, inherit in cases:
            with self.subTest(mode=mode, fd=fd, inherit=inherit):
                previous = git.head(self.worktree, binding=self.binding)
                self.assertEqual(git.move_branch(self.worktree, self.ref, self.parent, previous,
                                                 "fixture", binding=self.binding).returncode, 0)
                self.assertEqual(git.sync_index(self.worktree, self.parent, binding=self.binding).returncode, 0)
                original_index = git.index_digest(self.worktree, binding=self.binding)
                self.assertNotEqual(original_index, candidate.index_synced)
                files = self.scratch / f"{mode}-{fd}-{inherit}"
                files.mkdir()
                lock, ready, release = (files / name for name in ("run.lock", "ready", "release"))
                log = files / "worker.log"

                def competing_exit():
                    return subprocess.run([sys.executable, "-c", competitor, str(lock)], timeout=5).returncode

                with log.open("w+b") as stream:
                    args = [sys.executable, "-c", harness_code, mode, json.dumps(self.binding), self.parent,
                            candidate.commit, str(lock), str(ready), str(release), str(inherit), str(fd), child_code]
                    process = subprocess.Popen(args, cwd=Path(git.__file__).resolve().parents[1],
                                               stdout=stream, stderr=stream)

                    def wait_until(predicate):
                        deadline = time.monotonic() + 10
                        while not predicate():
                            if time.monotonic() > deadline:
                                self.fail(f"timed out, worker exit={process.poll()}, log={log.read_text()}")
                            time.sleep(.01)

                    try:
                        wait_until(lambda: ready.exists() or process.poll() is not None)
                        self.assertTrue(ready.exists(), log.read_text())
                        child = json.loads(ready.read_text())
                        pid = child["pid"]
                        self.assertEqual(len(child["matching"]), int(inherit))
                        if fd >= 3 and inherit:
                            self.assertEqual(child["matching"], [fd])
                        os.kill(pid, 0)
                        self.assertEqual(competing_exit(), 3)
                        process.kill()
                        self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
                        os.kill(pid, 0)
                        self.assertEqual(competing_exit(), 3 if inherit else 0)
                        with self.subTest(pid=pid):
                            # Observe native exec and exit independently of the flock result when kqueue is available.
                            if hasattr(select, "kqueue"):
                                with contextlib.closing(select.kqueue()) as queue:
                                    mask = select.KQ_NOTE_EXEC | select.KQ_NOTE_EXIT
                                    event = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                                         flags=select.KQ_EV_ADD, fflags=mask)
                                    queue.control([event], 0)
                                    release.touch()
                                    observed = 0
                                    while not observed & select.KQ_NOTE_EXIT:
                                        ready_events = queue.control(None, 1, 5)
                                        self.assertTrue(ready_events, f"child {pid} has not exited")
                                        observed |= ready_events[0].fflags
                                    self.assertEqual(bool(observed & select.KQ_NOTE_EXEC), inherit)
                            else:
                                release.touch()
                            wait_until(lambda: competing_exit() == 0)
                    finally:
                        release.touch(exist_ok=True)
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                expected_head = candidate.commit if inherit and mode == "ref" else self.parent
                expected_index = candidate.index_synced if inherit and mode == "index" else original_index
                self.assertEqual(git.head(self.worktree, binding=self.binding), expected_head)
                self.assertEqual(git.index_digest(self.worktree, binding=self.binding), expected_index)
        self.assertEqual(markers.read(), [])
