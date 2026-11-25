"""Integration of the private acceptance image with the ledger and real Git."""
import errno
import fcntl
import hashlib
import inspect
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from harness import acceptance, git
from harness.util import HarnessError, read_json
from test_git_invocation import IDENTITY, plain_git
from test_harness import ROOT, RepoCase, crash_after_save
from test_runtime_changes import ScriptedBackend


class AcceptanceIntentTests(RepoCase):
    def plan(self, *extra_paths):
        plan = read_json(ROOT / "examples/plan.json")
        plan["tasks"][0]["allowed_paths"].extend(extra_paths)
        return plan

    def pending_intent(self, backend=None):
        engine = self.engine(backend)
        crash, marker = crash_after_save(engine, lambda run: "intent" in run.get("pending_acceptance", {}))
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        self.assertTrue(marker["fired"])
        return engine, read_json(engine.directory / "state.json")["pending_acceptance"]["intent"]

    def assert_stop(self, error, kind, reason):
        self.assertEqual((error.stop["kind"], error.stop["reason"]), (kind, reason))

    def test_real_mixed_image_and_intent_bind_message_and_indexes(self):
        raw_name = os.fsdecode(b"raw-\xff.txt")
        try:
            (self.outside / raw_name).touch()
        except OSError as error:
            if error.errno != errno.EILSEQ:
                raise
            raw_name = "raw-café.txt"
        names = ["literal*.txt", "literalX.txt", "gone.txt", "link", "tool", raw_name]
        before = self.outside / "before"
        before.mkdir()
        images = []

        def changes(backend, kwargs, packet, result):
            if kwargs["role"] != "worker":
                return
            cwd = kwargs["cwd"]
            for name in names[:3] + names[-2:]:
                (cwd / name).write_text(f"file {names.index(name)}\n")
            (cwd / "link").symlink_to("missing")
            (cwd / "tool").chmod(0o755)
            plain_git(cwd, "--literal-pathspecs", "add", "--", names[0], "gone.txt")
            (cwd / names[0]).write_text("reviewed, not staged\n")
            (cwd / "gone.txt").unlink()
            result["commit_message"] = {"subject": "Mixed candidate", "body": "Review image\n\nUTF-8 café."}

        engine = self.engine(ScriptedBackend(changes))
        parent = git.head(self.repo)

        def capture(run):
            pending = run.get("pending_acceptance", {})
            if "intent" not in pending or images:
                return
            intent = pending["intent"]
            cwd = engine.worktree
            tracked = set(plain_git(cwd, "ls-tree", "-r", "--name-only", "-z", parent).stdout.split(b"\0"))
            stage = [name for name in intent["files"] if os.fsencode(name) in tracked
                     or (cwd / name).exists() or (cwd / name).is_symlink()]
            private = dict(os.environ, GIT_INDEX_FILE=str(before / "index"))
            plain_git(cwd, "read-tree", parent, env=private)
            plain_git(cwd, "--literal-pathspecs", "add", "--", *stage, env=private)
            expected_tree = plain_git(cwd, "write-tree", env=private).stdout.decode().strip()
            real_index = plain_git(cwd, "ls-files", "--stage", "-z").stdout
            expected_index = plain_git(cwd, "ls-files", "--stage", "-z", env=private).stdout
            stat_before = plain_git(cwd, "ls-files", "--debug", "test_hello.py").stdout
            images.append((intent.copy(), expected_tree, real_index, expected_index, stat_before,
                           (cwd / names[0]).read_bytes(), Path(pending["worker_source"]["artifact"])))

        engine.context.on_save = capture
        result = engine.start("Implement greet", self.plan("*.txt", "link", "tool"))
        intent, expected_tree, real_before, real_after, stat_before, worktree_file, original = images[0]
        accepted = result["accepted"]["greet"]
        commit = accepted["commit"]
        self.assertEqual((intent["parent"], intent["ref"], intent["commit"], intent["tree"], intent["files"]),
                         (parent, result["worktree_binding"]["branch"], commit, expected_tree, accepted["files"]))
        self.assertEqual(accepted["tree"], expected_tree)
        self.assertEqual(intent["index_before"], hashlib.sha256(real_before).hexdigest())
        self.assertEqual(intent["index_synced"], hashlib.sha256(real_after).hexdigest())
        self.assertEqual(plain_git(engine.worktree, "ls-files", "--stage", "-z").stdout, real_after)
        self.assertEqual(plain_git(engine.worktree, "ls-files", "--debug", "test_hello.py").stdout, stat_before)
        self.assertEqual((engine.worktree / names[0]).read_bytes(), worktree_file)
        self.assertTrue((engine.worktree / "link").is_symlink())
        self.assertFalse((engine.worktree / "gone.txt").exists())
        raw = plain_git(engine.worktree, "cat-file", "commit", commit).stdout
        header, _, message = raw.partition(b"\n\n")
        self.assertEqual(message, git.commit_message_text(read_json(original)["commit_message"], result["id"], "greet").encode())
        for role in (b"author", b"committer"):
            self.assertIn(role + b" Personal Harness <harness@localhost> ", header)
        self.assertNotIn(b"\nencoding ", header)
        self.assertNotIn(b"\ngpgsig ", header)

    def test_zero_difference_clears_index_without_moving_branch(self):
        source = b'def greet(name):\n    return f"Hello, {name}!"\n'
        (self.repo / "hello.py").write_bytes(source)
        plain_git(self.repo, "add", "hello.py")
        plain_git(self.repo, *IDENTITY, "commit", "-m", "already implemented")
        parent = git.head(self.repo)

        def stage_then_delete(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                cwd = kwargs["cwd"]
                (cwd / "hello.py").write_bytes(source)
                (cwd / "gone.txt").write_text("staged only")
                plain_git(cwd, "add", "gone.txt")
                (cwd / "gone.txt").unlink()

        engine = self.engine(ScriptedBackend(stage_then_delete))
        with patch.object(git, "move_branch", wraps=git.move_branch) as move, \
                patch.object(git, "sync_index", wraps=git.sync_index) as sync:
            result = engine.start("Implement greet", self.plan("gone.txt"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["accepted"]["greet"]["commit"], parent)
        self.assertEqual(git.head(engine.worktree), parent)
        self.assertEqual(plain_git(engine.worktree, "ls-files", "gone.txt").stdout, b"")
        move.assert_not_called()
        sync.assert_called_once()

    def test_resume_reuses_saved_intent_and_rebuilds_only_after_confirmed_loss(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                backend = ScriptedBackend()
                engine, intent = self.pending_intent(backend)
                if missing:
                    oid = intent["commit"]
                    (Path(engine.run["worktree_binding"]["common_dir"]) / "objects" / oid[:2] / oid[2:]).unlink()
                resumed = self.engine(backend)
                with patch.object(git, "build_commit", wraps=git.build_commit) as build:
                    result = resumed.resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(build.call_count, int(missing))
                accepted = result["accepted"]["greet"]
                self.assertEqual(accepted["tree"], intent["tree"])
                if not missing:
                    self.assertEqual(accepted["commit"], intent["commit"])
                self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)

    def test_real_fatal_binding_read_preserves_legacy_stop_and_becomes_io_in_acceptance(self):
        engine, intent = self.pending_intent()
        binding = engine.run["worktree_binding"]
        configuration = Path(binding["common_dir"]) / "config"
        original = configuration.read_bytes()
        configuration.write_bytes(original + b"\n[unterminated\n")
        try:
            native = plain_git(engine.worktree, "symbolic-ref", "-q", "HEAD", check=False)
            self.assertNotIn(native.returncode, (0, 1))
            with self.assertRaises(HarnessError) as legacy:
                git.check_worktree_binding(engine.worktree, binding)
            self.assertIs(type(legacy.exception), HarnessError)
            self.assertIn("分支绑定已改变", str(legacy.exception))
            self.assert_stop(legacy.exception, "workspace_mismatch", "binding_changed")
            self.assertEqual(legacy.exception.binding_git_result.returncode, native.returncode)
            with self.assertRaises(HarnessError) as caught:
                acceptance.state(engine.context, intent)
            self.assert_stop(caught.exception, "runtime_failure", "io")
            self.assertIn(f"exited {native.returncode}", str(caught.exception))
        finally:
            configuration.write_bytes(original)

        for failure in (OSError(errno.EAGAIN, "probe launch"), subprocess.TimeoutExpired("git", 1)):
            with self.subTest(failure=failure), patch.object(git, "_launch", side_effect=failure):
                with self.assertRaises(git.GitLaunchError) as caught:
                    acceptance.state(engine.context, intent)
                self.assert_stop(caught.exception, "runtime_failure", "io")
                self.assertIs(caught.exception.__cause__, failure)

        metadata = Path(binding["git_dir"]) / "HEAD"
        original = metadata.read_bytes()
        for replacement in (b"ref: refs/heads/main\n", intent["parent"].encode() + b"\n"):
            with self.subTest(head=replacement):
                metadata.write_bytes(replacement)
                with self.assertRaises(HarnessError) as caught:
                    acceptance.state(engine.context, intent)
                self.assert_stop(caught.exception, "workspace_mismatch", "binding_changed")
                self.assertFalse(hasattr(caught.exception, "binding_git_result"))
        metadata.write_bytes(original)
        gitfile = engine.worktree / ".git"
        original = gitfile.read_bytes()
        gitfile.write_bytes(b"gitdir: /wrong\n")
        try:
            with self.assertRaises(HarnessError) as caught:
                acceptance.state(engine.context, intent)
            self.assert_stop(caught.exception, "workspace_mismatch", "binding_changed")
            self.assertFalse(hasattr(caught.exception, "binding_git_result"))
        finally:
            gitfile.write_bytes(original)
        self.assertEqual(acceptance.state(engine.context, intent).branch, intent["parent"])

    def test_fatal_git_read_stops_completion_at_state_cas_and_confirmation(self):
        for location in ("state", "cas", "confirm"):
            with self.subTest(location=location):
                engine = self.engine()
                move = git.move_branch
                branch = git.branch_state
                failed_cas = False
                observed = []

                def broken_config_during(operation, *args, **kwargs):
                    config = Path(engine.run["worktree_binding"]["common_dir"]) / "config"
                    original = config.read_bytes()
                    config.write_bytes(original + b"\n[unterminated\n")
                    try:
                        return operation(*args, **kwargs)
                    finally:
                        config.write_bytes(original)

                def stale_cas(repo, ref, commit, parent, task_id, **kwargs):
                    nonlocal failed_cas
                    result = move(repo, ref, commit, commit, task_id, **kwargs)
                    self.assertNotEqual(result.returncode, 0)
                    failed_cas = True
                    return result

                def confirm_branch(*args, **kwargs):
                    if failed_cas:
                        return broken_config_during(branch, *args, **kwargs)
                    return branch(*args, **kwargs)

                def observe_fatal(argv, env, **kwargs):
                    result = launch(argv, env, **kwargs)
                    if argv[-3:] == ["symbolic-ref", "-q", "HEAD"] and result.returncode not in (0, 1):
                        observed.append(result.returncode)
                    return result

                launch = git._launch
                if location == "cas":
                    boundary = patch.multiple(git, move_branch=stale_cas, branch_state=confirm_branch)
                else:
                    operation = getattr(acceptance, location)
                    boundary = patch.object(acceptance, location, side_effect=lambda *args: broken_config_during(
                        operation, *args))
                with boundary, patch.object(git, "_launch", observe_fatal), self.assertRaises(HarnessError) as caught:
                    engine.start("Implement greet", self.plan())
                self.assert_stop(caught.exception, "runtime_failure", "io")
                self.assertEqual(len(observed), 1)
                saved = read_json(engine.directory / "state.json")
                intent = saved["pending_acceptance"]["intent"]
                self.assertEqual(saved["expected_head"], intent["parent"])
                self.assertEqual(saved["accepted"], {})
                self.assertEqual(git.head(engine.worktree), intent["commit"] if location == "confirm" else intent["parent"])

    def test_directory_replaced_by_link_with_and_without_ignore(self):
        (self.repo / ".gitignore").write_text("__pycache__/\n/ignored-src\n")
        for name in ("src", "ignored-src"):
            (self.repo / name).mkdir()
            (self.repo / name / "tracked").write_text("old contents")
            plain_git(self.repo, "add", "-f", name)
        plain_git(self.repo, "add", ".gitignore")
        plain_git(self.repo, *IDENTITY, "commit", "-m", "directory fixtures")
        outside = self.outside / "outside-directory"
        outside.mkdir()
        (outside / "sentinel").write_text("outside")
        parent = git.head(self.repo)
        for name in ("src", "ignored-src"):
            with self.subTest(name=name):
                expected = []

                def replace(backend, kwargs, packet, result):
                    if kwargs["role"] != "worker":
                        return
                    root = kwargs["cwd"]
                    shutil.rmtree(root / name)
                    (root / name).symlink_to(outside, target_is_directory=True)
                    env = dict(os.environ, GIT_INDEX_FILE=str(self.outside / f"{name}.index"))
                    plain_git(root, "read-tree", parent, env=env)
                    plain_git(root, "add", "-A", env=env)
                    expected.append(plain_git(root, "write-tree", env=env).stdout.decode().strip())

                engine = self.engine(ScriptedBackend(replace))
                result = engine.start("Implement greet", self.plan(f"{name}/**"))
                self.assertEqual(result["status"], "completed")
                saved = result["accepted"]["greet"]
                self.assertEqual(saved["tree"], expected[0])
                content = plain_git(engine.worktree, "ls-tree", "-r", saved["commit"], "--", name).stdout
                if name == "src":
                    self.assertTrue(content.startswith(b"120000 blob "))
                    self.assertTrue(content.endswith(b"\tsrc\n"))
                else:
                    self.assertEqual(content, b"")
                self.assertEqual((outside / "sentinel").read_text(), "outside")

    def test_untracked_nested_repository_is_rejected_before_intent(self):
        engine = self.engine()
        real_scope = git.check_scope
        created = []

        def add_nested_at_acceptance(repo, *args, **kwargs):
            if not created and "pending_acceptance" in engine.run:
                nested = repo / "sub"
                nested.mkdir()
                plain_git(nested, "init", "-q")
                (nested / "file").write_text("nested repository")
                plain_git(nested, "add", "file")
                plain_git(nested, *IDENTITY, "commit", "-qm", "nested")
                created.append(nested)
            return real_scope(repo, *args, **kwargs)

        with patch.object(git, "check_scope", add_nested_at_acceptance), self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", self.plan("sub/**"))
        self.assertEqual(len(created), 1)
        self.assert_stop(caught.exception, "scope_violation", "candidate_outside_scope")
        self.assertEqual(caught.exception.stop["data"]["path"], "sub/")
        saved = read_json(engine.directory / "state.json")
        self.assertNotIn("intent", saved["pending_acceptance"])
        self.assertEqual(saved["accepted"], {})
        self.assertEqual(git.head(engine.worktree), saved["expected_head"])

    def test_late_directory_or_fifo_is_checked_before_intent(self):
        (self.repo / "x").write_text("original\n")
        plain_git(self.repo, "add", "x")
        plain_git(self.repo, *IDENTITY, "commit", "-m", "tracked path for acceptance race")
        parent = git.head(self.repo)

        def candidate(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                (kwargs["cwd"] / "x").write_text("candidate\n")

        for scope, late, stop in (("x", "add", ("scope_violation", "candidate_outside_scope")),
                                  ("x/**", "add", ("workspace_mismatch", "candidate_changed")),
                                  ("x/**", "classification", None),
                                  ("x", "fifo", ("scope_violation", "special_file"))):
            with self.subTest(scope=scope, late=late):
                engine = self.engine(ScriptedBackend(candidate))
                launch = git._launch
                classify = git._accept_paths
                replacements = []

                def replace():
                    path = engine.worktree / "x"
                    path.unlink()
                    if late == "fifo":
                        os.mkfifo(path)
                    else:
                        path.mkdir()
                        (path / "new").write_text("not reviewed\n")
                    replacements.append(path)

                def before_classification(*args, **kwargs):
                    if late != "add":
                        replace()
                    return classify(*args, **kwargs)

                def before_add(argv, env, *, effect, **kwargs):
                    if late == "add" and effect is git.PRIVATE and "add" in argv:
                        replace()
                    return launch(argv, env, effect=effect, **kwargs)

                with patch.object(git, "_accept_paths", before_classification), patch.object(git, "_launch", before_add):
                    if stop:
                        with self.assertRaises(HarnessError) as caught:
                            engine.start("Implement greet", self.plan(scope))
                        self.assert_stop(caught.exception, *stop)
                        expected_path = {"fifo": "x", "add": "x/new"}[late]
                        self.assertEqual(caught.exception.stop["data"].get("path"),
                                         expected_path if stop[0] == "scope_violation" else None)
                        saved = read_json(engine.directory / "state.json")
                        self.assertEqual(saved["accepted"], {})
                        self.assertNotIn("intent", saved["pending_acceptance"])
                        self.assertEqual(git.head(engine.worktree), parent)
                    else:
                        crash, marker = crash_after_save(engine, lambda run: bool(run.get("accepted")))
                        with crash, self.assertRaises(KeyboardInterrupt):
                            engine.start("Implement greet", self.plan(scope))
                        self.assertTrue(marker["fired"])
                        saved = read_json(engine.directory / "state.json")["accepted"]["greet"]
                        self.assertEqual(plain_git(engine.worktree, "ls-tree", "-r", saved["commit"], "--", "x").stdout, b"")
                        self.assertEqual((engine.worktree / "x" / "new").read_text(), "not reviewed\n")
                self.assertEqual(len(replacements), 1)

    def test_run_lock_descriptor_survives_interrupted_callback_cleanup(self):
        engine, _ = self.pending_intent()
        context = engine.context
        path = engine.directory / "run.lock"
        callback_error = ValueError("callback failed")
        self.assertIsNone(context.run_lock_descriptor)

        def throw():
            raise callback_error

        with self.assertRaises(ValueError) as thrown:
            context.run_lock(None, throw)
        self.assertIs(thrown.exception, callback_error)
        self.assertIsNone(context.run_lock_descriptor)

        old_holder = old_fd = None

        def throw_with_lock():
            nonlocal old_holder, old_fd
            old_holder = context._run_lock
            old_fd = context.run_lock_descriptor
            self.assertEqual(old_fd, old_holder.descriptor)
            self.assertTrue(os.path.samestat(os.fstat(old_fd), path.stat()))
            raise callback_error

        source, start = inspect.getsourcelines(type(context).run_lock)
        cleanup = next(start + i for i, line in enumerate(source) if line.strip() == "self._run_lock = None")
        filename = inspect.getsourcefile(type(context).run_lock)
        landed = []

        def trace(frame, event, arg):
            if event == "line" and frame.f_code.co_filename == filename and frame.f_lineno == cleanup:
                landed.append(sys.exception())
                signal.raise_signal(signal.SIGINT)
            return trace

        previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
        previous_trace = sys.gettrace()
        try:
            with self.assertRaises(KeyboardInterrupt) as interrupted:
                sys.settrace(trace)
                context.run_lock(None, throw_with_lock)
        finally:
            sys.settrace(previous_trace)
            signal.signal(signal.SIGINT, previous_handler)

        self.assertEqual(landed, [callback_error])
        self.assertIs(interrupted.exception.__context__, callback_error)
        self.assertIs(context._run_lock, old_holder)
        with self.assertRaises(OSError) as closed:
            os.fstat(old_fd)
        self.assertEqual(closed.exception.errno, errno.EBADF)
        with self.assertRaisesRegex(RuntimeError, "the lock is not held"):
            _ = old_holder.descriptor
        with path.open("rb") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

        self.assertIsNone(context.run_lock_descriptor)

        def reacquired():
            self.assertIsNot(context._run_lock, old_holder)
            current_fd = context.run_lock_descriptor
            self.assertEqual(current_fd, old_fd)  # reacquisition reuses the lowest available descriptor
            self.assertTrue(os.path.samestat(os.fstat(current_fd), path.stat()))
            with self.assertRaises(HarnessError) as nested:
                context.run_lock({"kind": "busy", "reason": "run_lock"}, self.fail)
            self.assert_stop(nested.exception, "busy", "run_lock")
            self.assertEqual(context.run_lock_descriptor, current_fd)
            self.assertIsNone(old_holder._shut())
            self.assertTrue(os.path.samestat(os.fstat(current_fd), path.stat()))
            with path.open("rb") as contender, self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return current_fd

        current_fd = context.run_lock(None, reacquired)
        self.assertIsNone(context.run_lock_descriptor)
        with self.assertRaises(OSError) as closed:
            os.fstat(current_fd)
        self.assertEqual(closed.exception.errno, errno.EBADF)
        with path.open("rb") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_two_builds_and_only_publishing_git_children_inherit_the_run_lock(self):
        plan = self.plan()
        plan["tasks"].append({**plan["tasks"][0], "id": "document", "depends_on": ["greet"],
                              "allowed_paths": ["usage.txt"]})

        def write_document(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and kwargs["task"]["id"] == "document":
                (kwargs["cwd"] / "usage.txt").write_text("Use greet(name).\n")

        engine = self.engine(ScriptedBackend(write_document))
        spawn = subprocess.Popen
        launch = git._launch
        writes = []
        pathspec_adds = []
        image_adds = []
        child_check = """
import fcntl, os, sys
fd, path, *argv = sys.argv[1:]
passed = os.fstat(int(fd))
lock = os.stat(path)
assert (passed.st_dev, passed.st_ino) == (lock.st_dev, lock.st_ino)
with open(path, 'rb') as contender:
    try:
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise AssertionError('the run lock was not held')
os.execvp(argv[0], argv)
"""

        def observe(argv, env, *, effect, **kwargs):
            if effect is git.PRIVATE and "add" in argv:
                add = argv.index("add")
                if argv[add:] == ["add", "-A"]:
                    self.assertNotIn("--", argv)
                    self.assertNotIn("--literal-pathspecs", argv)
                    self.assertEqual(kwargs.get("pass_fds", ()), ())
                    image_adds.append(argv[add:])
                else:
                    self.assertEqual(argv[add - 1:add + 2], ["--literal-pathspecs", "add", "--"])
                    paths = argv[add + 2:]
                    self.assertTrue(paths)
                    self.assertTrue(all(not (engine.worktree / name).is_dir() for name in paths))
                    pathspec_adds.append(paths)
            return launch(argv, env, effect=effect, **kwargs)

        def invoke(argv, **kwargs):
            if argv[0] != "git":
                return spawn(argv, **kwargs)
            fds = kwargs.get("pass_fds", ())
            publish = "--no-deref" in argv and "update-ref" in argv
            sync = "--reset" in argv and "read-tree" in argv
            if publish or sync:
                lock_fd = engine.context.run_lock_descriptor
                self.assertIsNotNone(lock_fd)
                self.assertEqual(fds, (lock_fd,))
                writes.append("ref" if publish else "index")
                return spawn([sys.executable, "-c", child_check, str(lock_fd), str(engine.directory / "run.lock"),
                            *argv], **kwargs)
            self.assertEqual(fds, ())
            return spawn(argv, **kwargs)

        with patch.object(git, "build_commit", wraps=git.build_commit) as build, \
                patch.object(git, "_launch", observe), patch.object(git.subprocess, "Popen", invoke):
            result = engine.start("Implement and document greet", plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(build.call_count, len(result["accepted"]))
        self.assertEqual(pathspec_adds, [["hello.py"], ["usage.txt"]])
        self.assertEqual(image_adds, [["add", "-A"]])
        self.assertEqual(writes, ["ref", "index", "ref", "index"])
        self.assertIsNone(engine.context.run_lock_descriptor)
