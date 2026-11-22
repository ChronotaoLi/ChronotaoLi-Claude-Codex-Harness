import contextlib
import errno
import os
import socket
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import git, stops
from harness.budgets import work_attempts
from harness.util import read_json
from harness.verification import Verification
from test_harness import ROOT, RepoCase, crash_after_save, run_cli, walk_exit
from test_provider_failures import FifoWatchdog
from test_runtime_changes import ScriptedBackend


SPECIAL = ("scope_violation", "special_file")
TRACKED = "hello.py"


class CandidateSpecialFileTests(RepoCase):
    def setUp(self):
        super().setUp()
        # The fake worker writes this same implementation, leaving only the special entry
        # as a worktree change in the untracked cases.
        self.commit_file(TRACKED, 'def greet(name):\n    return f"Hello, {name}!"\n')
        self.plan = read_json(ROOT / "examples" / "plan.json")

    def commit_file(self, name, contents="# original\n"):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        git.git(self.repo, "add", "--", name)
        git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                "-c", "commit.gpgsign=false", "commit", "-m", "Track fixture file")

    def fifo(self, path):
        os.mkfifo(path)
        watchdog = FifoWatchdog(path, bound=5)
        self.addCleanup(lambda: self.assertFalse(watchdog.stop(), f"Blocked on FIFO {path}"))

    def socket_entry(self):
        name = self.outside / "socket"
        operation = "socket()"
        try:
            listener = socket.socket(socket.AF_UNIX)
            self.addCleanup(listener.close)
            with contextlib.chdir(name.parent):
                operation = "bind()"
                listener.bind(name.name)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EPERM):
                self.skipTest(f"AF_UNIX {operation} denied: {error}")
            raise
        return name.rename

    @staticmethod
    def worker_with_entry(name, create):
        def edit(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and len(backend.calls) == 1:
                path = kwargs["cwd"] / name
                path.unlink(missing_ok=True)
                create(path)

        return ScriptedBackend(edit)

    def assert_rejected_worker(self, state):
        first, = state["dispatches"]
        self.assertEqual((first["role"], first["status"], first["worker_receipt_consumed"]),
                         ("worker", "failed", "scope_rejected"))
        self.assertEqual(state["worker_receipts"], {})
        self.assertEqual(state["worker_recovery"]["greet"]["dispatch_id"], first["id"])
        self.assertEqual(state["attempts"]["greet"], 1)
        self.assertEqual(work_attempts(state)["greet"], 1)
        self.assertEqual(state["accepted"], {})
        self.assertNotIn("pending_task", state)
        return first["id"]

    def rejected_entry(self, create):
        backend = self.worker_with_entry(TRACKED, create)
        engine = self.engine(backend)
        with self.assertRaises(git.ScopeViolation):
            engine.start("Implement greet", self.plan)
        state = read_json(engine.directory / "state.json")
        stop = state["stop"]
        self.assertEqual(state["status"], "blocked")
        self.assertEqual((stop["kind"], stop["reason"]), SPECIAL)
        self.assertEqual((stop["data"]["path"], stop["source"]), (TRACKED, {"task_id": "greet"}))
        self.assertEqual(stops.project_ledger(state)["exit_code"], 1)
        return engine, backend, self.assert_rejected_worker(state)

    def repair_entry(self, engine):
        path = engine.worktree / TRACKED
        path.unlink()
        path.write_text("# regular file restored by the operator\n")

    def assert_fresh_worker(self, state, backend, rejected):
        workers = [entry for entry in state["dispatches"] if entry["role"] == "worker"]
        calls = [call for call in backend.calls if call["role"] == "worker"]
        self.assertEqual(len(workers), 2)
        self.assertEqual(len({entry["id"] for entry in workers}), 2)
        self.assertEqual(state["attempts"]["greet"], 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["packet"]["recovery"]["source"]["dispatch_id"], rejected)

    def test_tracked_fifo_recovers_with_a_fresh_worker(self):
        self.config.data["runtime"]["max_attempts"] = 2
        engine, backend, rejected = self.rejected_entry(self.fifo)
        self.assertTrue(stat.S_ISFIFO((engine.worktree / TRACKED).lstat().st_mode))
        calls = list(backend.calls)
        code, _ = run_cli(self, backend, ["resume", engine.run["id"]])
        self.assertEqual(code, 1)
        self.assertEqual(backend.calls, calls)
        self.assert_rejected_worker(read_json(engine.directory / "state.json"))

        self.repair_entry(engine)
        state = walk_exit(self, engine.directory, ("scope_violation", "special_file"), backend=backend,
                          expect_status="completed")
        self.assert_fresh_worker(state, backend, rejected)

    def test_tracked_fifo_spends_the_last_work_attempt(self):
        self.config.data["runtime"]["max_attempts"] = 1
        engine, backend, rejected = self.rejected_entry(self.fifo)
        self.repair_entry(engine)
        state = walk_exit(self, engine.directory, SPECIAL, backend=backend, expect_status="blocked", expect_code=3)
        self.assertEqual((state["stop"]["kind"], state["stop"]["reason"]), ("budget", "task_attempts"))
        self.assertEqual(self.assert_rejected_worker(state), rejected)
        self.assertEqual([call["role"] for call in backend.calls], ["worker"])

    def test_restart_after_rejection_is_saved_and_before_the_stop_is_saved(self):
        self.config.data["runtime"]["max_attempts"] = 2
        backend = self.worker_with_entry(TRACKED, self.fifo)
        engine = self.engine(backend)
        crash, fired = crash_after_save(
            engine, lambda state: any(entry.get("worker_receipt_consumed") == "scope_rejected"
                                      for entry in state["dispatches"]))
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan)
        self.assertTrue(fired["fired"])
        state = read_json(engine.directory / "state.json")
        self.assertEqual(state["status"], "running")
        self.assertNotIn("stop", state)
        rejected = self.assert_rejected_worker(state)
        self.repair_entry(engine)
        state = self.engine(backend).resume(state["id"])
        self.assertEqual(state["status"], "completed")
        self.assert_fresh_worker(state, backend, rejected)

    def assert_untracked_entry_is_ignored(self, name, create, file_type):
        backend = self.worker_with_entry(name, create)
        engine = self.engine(backend)
        state = engine.start("Implement greet", self.plan)
        self.assertEqual(state["status"], "completed")
        accepted = state["accepted"]["greet"]
        self.assertEqual(accepted["files"], [])
        self.assertEqual(accepted["commit"], git.head(self.repo))
        tree = git.git(engine.worktree, "ls-tree", "-r", "--name-only", "-z", accepted["commit"])
        self.assertNotIn(name, tree.split("\0"))
        diffs = [Path(call["packet"]["diff_artifact"]).read_bytes() for call in backend.calls
                 if call["role"] == "reviewer"]
        self.assertEqual(diffs, [b"", b""])
        self.assertEqual(stat.S_IFMT((engine.worktree / name).lstat().st_mode), file_type)

    def test_untracked_fifo_stays_out_of_the_accepted_commit_and_diffs(self):
        self.assert_untracked_entry_is_ignored("untracked.fifo", self.fifo, stat.S_IFIFO)

    def test_tracked_socket_stops_at_worker_return(self):
        self.config.data["runtime"]["max_attempts"] = 2
        engine, backend, rejected = self.rejected_entry(self.socket_entry())
        self.assertTrue(stat.S_ISSOCK((engine.worktree / TRACKED).lstat().st_mode))
        self.repair_entry(engine)
        state = walk_exit(self, engine.directory, SPECIAL, backend=backend, expect_status="completed")
        self.assert_fresh_worker(state, backend, rejected)

    def test_untracked_socket_stays_out_of_the_accepted_commit_and_diffs(self):
        self.assert_untracked_entry_is_ignored("untracked.socket", self.socket_entry(), stat.S_IFSOCK)

    def test_scope_treats_a_tracked_descendant_under_a_symlink_as_deleted(self):
        name = "nested/tracked.txt"
        self.commit_file(name)
        (self.repo / name).unlink()
        (self.repo / "nested").rmdir()
        self.fifo(self.outside / "tracked.txt")
        (self.repo / "nested").symlink_to(self.outside, target_is_directory=True)
        self.assertEqual(git.check_scope(self.repo, ["nested", "nested/**"]), ["nested", name])

    def test_out_of_scope_rejection_precedes_special_file_check_on_the_same_path(self):
        (self.repo / TRACKED).unlink()
        self.fifo(self.repo / TRACKED)
        with self.assertRaises(git.ScopeViolation) as stopped:
            git.check_scope(self.repo, ["test_hello.py"], task_id="greet")
        self.assertEqual(stopped.exception.stop,
                         stops.make("scope_violation", "candidate_outside_scope",
                                    data={"path": TRACKED}, source={"task_id": "greet"}))

    def test_a_metadata_read_failure_propagates_as_local_io(self):
        (self.repo / TRACKED).write_text("# changed\n")
        original = os.stat
        failure = OSError(errno.EIO, "fixture metadata read failed")

        def fail_leaf(path, *args, **kwargs):
            if path == TRACKED and kwargs.get("dir_fd") is not None and kwargs.get("follow_symlinks") is False:
                raise failure
            return original(path, *args, **kwargs)

        with patch.object(os, "stat", side_effect=fail_leaf), self.assertRaises(OSError) as raised:
            git.check_scope(self.repo, [TRACKED])
        self.assertIs(raised.exception, failure)
        stop = stops.of(raised.exception)
        self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "io"))

    def test_special_file_check_rejects_a_different_root_directory(self):
        other_root = self.outside.stat()
        with patch.object(os, "fstat", return_value=other_root), self.assertRaises(OSError) as raised:
            git.special_files(self.repo, [TRACKED])
        self.assertEqual(raised.exception.errno, errno.ESTALE)
        stop = stops.of(raised.exception)
        self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "io"))

    def test_special_file_check_refuses_a_symlink_root(self):
        linked_root = self.outside / "linked-root"
        linked_root.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(OSError) as raised:
            git.special_files(linked_root, [TRACKED])
        self.assertIn(raised.exception.errno, (errno.ENOTDIR, errno.ELOOP))

    def make_root_search_only(self, root):
        original_mode = stat.S_IMODE(root.stat().st_mode)
        self.addCleanup(os.chmod, root, original_mode)
        root.chmod(0o311)

    @unittest.skipUnless(getattr(os, "O_SEARCH", 0) or getattr(os, "O_PATH", 0),
                         "search-only directory descriptors are unavailable")
    def test_task_is_reviewed_and_accepted_with_a_search_only_root(self):
        def edit(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                path = kwargs["cwd"] / TRACKED
                path.write_text(path.read_text() + "# reviewed change\n")
            elif kwargs["kind"] == "review":
                self.assertEqual(stat.S_IMODE(kwargs["cwd"].stat().st_mode), 0o311)

        # The whole-run command starts in a fresh checkout so the original root can stay search-only
        # throughout task review, acceptance and the whole-run gate.
        self.config.data["verification"]["fresh_checkout"] = True
        verify = Verification.verify

        def change_root_after_task_checks(verification, task_id, attempt, **kwargs):
            results = verify(verification, task_id, attempt, **kwargs)
            if not kwargs.get("whole_run"):
                self.assertEqual([item["status"] for item in results], ["passed"])
                self.make_root_search_only(verification.context.worktree)
            return results

        backend = ScriptedBackend(edit)
        engine = self.engine(backend)
        with patch.object(Verification, "verify", change_root_after_task_checks):
            state = engine.start("Implement greet", self.plan)
        self.assertEqual(state["status"], "completed")
        self.assertEqual([call["kind"] for call in backend.calls], ["implement", "review", "final_review"])
        self.assertEqual(state["accepted"]["greet"]["files"], [TRACKED])
        self.assertEqual(stat.S_IMODE(engine.worktree.stat().st_mode), 0o311)

    @unittest.skipUnless(getattr(os, "O_SEARCH", 0) or getattr(os, "O_PATH", 0),
                         "search-only directory descriptors are unavailable")
    def test_archive_restores_regular_file_with_a_search_only_root(self):
        tree = self.outside / "worker"
        git.git(self.repo, "worktree", "add", "-b", "worker", str(tree))
        binding = git.capture_worktree_binding(tree, "worker")
        head = git.head(tree, binding=binding)
        path = tree / TRACKED
        original = path.read_bytes()
        edited = original + b"# archived change\n"
        path.write_bytes(edited)
        self.make_root_search_only(tree)

        archive = self.outside / "archive"
        self.assertEqual(git.archive_and_reset(tree, head, archive, self.outside, binding=binding), str(archive))
        self.assertEqual((archive / "files" / TRACKED).read_bytes(), edited)
        self.assertEqual(read_json(archive / "restored.json"), {"head": head, "paths": [TRACKED]})
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(git.clean(tree, binding=binding))

    def test_archive_rejects_a_tracked_fifo_before_writing_or_resetting(self):
        tree = self.outside / "worker"
        git.git(self.repo, "worktree", "add", "-b", "worker", str(tree))
        binding = git.capture_worktree_binding(tree, "worker")
        head = git.head(tree, binding=binding)
        path = tree / TRACKED
        path.unlink()
        self.fifo(path)
        before = git.snapshot(tree, binding=binding)
        inode = path.lstat().st_ino
        archive = self.outside / "archive"
        with self.assertRaises(git.ScopeViolation) as stopped:
            git.archive_and_reset(tree, head, archive, self.outside, binding=binding)
        self.assertEqual(stopped.exception.stop, stops.make(*SPECIAL, data={"path": TRACKED}))
        self.assertFalse(archive.exists())
        self.assertEqual(git.snapshot(tree, binding=binding), before)
        self.assertEqual(path.lstat().st_ino, inode)
        self.assertTrue(stat.S_ISFIFO(path.lstat().st_mode))
