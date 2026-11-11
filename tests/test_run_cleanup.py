from pathlib import Path
import shutil
from unittest.mock import patch

from harness import git
from harness.cleanup import cleanup_run
from harness.util import HarnessError, lock, read_json, write_json
from test_harness import RepoCase, ROOT


class RunCleanupTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.runtime = self.engine()
        self.run = self.runtime.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.directory = self.runtime.directory
        self.checkout = self.runtime.worktree
        # The simulated verification generates only these known test caches.
        shutil.rmtree(self.checkout / "__pycache__", ignore_errors=True)
        self.state = self.directory / "state.json"
        self.receipt = self.directory / "worktree-cleanup.json"

    def cleanup(self, remove=False, **kwargs):
        """The worktree resource of the single cleanup entry; this fixture has no checkouts."""
        result = cleanup_run(self.repo, self.run["id"], remove=remove, **kwargs)
        self.assertEqual(result["checkouts"], [])
        return {**result["worktree"], "run_status": result["run_status"]}

    def assert_blocked(self, pattern=None, **kwargs):
        result = self.cleanup(True, **kwargs)
        self.assertEqual(result["status"], "blocked")
        if pattern is not None:
            self.assertRegex(result["error"], pattern)
        return result

    def test_preview_and_removal_retain_exact_state_evidence_and_branch(self):
        original = {p: p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}
        self.assertEqual(self.cleanup()["status"], "eligible")
        self.assertFalse(self.receipt.exists())
        self.assertTrue(self.checkout.exists())
        self.assertEqual(self.cleanup(True)["status"], "removed")
        self.assertFalse(self.checkout.exists())
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data, str(path))
        self.assertEqual(git.git(self.repo, "rev-parse", "refs/heads/" + self.run["branch"]).strip(), self.run["expected_head"])
        self.assertEqual(read_json(self.state)["status"], "completed")
        self.assertEqual(self.cleanup()["status"], "absent")
        self.assertEqual(self.cleanup(True)["status"], "removed")

    def test_lock_refuses_and_nonterminal_worktree_is_retained(self):
        with lock(self.directory / "run.lock"), self.assertRaises(HarnessError):
            self.cleanup(True)
        # The current version can still finish or abandon the run; older ones cannot.
        for version, kind in ((16, "finish_or_abandon_first"), (13, "manual_removal"), (7, "manual_removal")):
            for status in ("running", "stopped", "planning"):
                with self.subTest(version=version, status=status):
                    self.run.update(status=status, schema_version=version)
                    write_json(self.state, self.run)
                    for remove in (True, False):
                        result = self.cleanup(remove)
                        self.assertEqual((result["status"], result["reason_kind"]), ("retained", kind))
                        self.assertTrue(result["reason"])
                        self.assertNotIn("error", result)
        self.assertTrue(self.checkout.exists())
        self.assertFalse(self.receipt.exists())

    def test_removed_receipt_does_not_hide_recreated_git_admin_directory(self):
        admin = Path(self.run["worktree_binding"]["git_dir"])
        self.assertEqual(self.cleanup(True)["status"], "removed")
        admin.mkdir(parents=True)
        (admin / "unrelated-data").write_text("preserve")
        self.assert_blocked("重新出现")
        self.assertEqual((admin / "unrelated-data").read_text(), "preserve")

    def test_abandoned_terminal_is_eligible(self):
        self.run["status"] = "abandoned"
        write_json(self.state, self.run)
        self.assertEqual(self.cleanup(True)["run_status"], "abandoned")
        self.assertEqual(read_json(self.state)["status"], "abandoned")

    def test_unsaved_and_hidden_index_data_refuse_removal(self):
        original = (self.checkout / "hello.py").read_bytes()
        for variant in ("tracked", "staged", "untracked", "ignored", "assume-unchanged", "skip-worktree"):
            with self.subTest(variant=variant):
                if variant in ("tracked", "staged"):
                    (self.checkout / "hello.py").write_bytes(original + b"\n# unsaved\n")
                    if variant == "staged":
                        git.git(self.checkout, "add", "hello.py")
                elif variant in ("untracked", "ignored"):
                    path = self.checkout / ("scratch" if variant == "untracked" else "__pycache__/scratch")
                    path.parent.mkdir(exist_ok=True)
                    path.write_text("keep")
                else:
                    git.git(self.checkout, "update-index", "--" + variant, "hello.py")
                self.assert_blocked()
                self.assertTrue(self.checkout.exists())
                self.assertFalse(self.receipt.exists())
                if variant in ("tracked", "staged"):
                    git.git(self.checkout, "reset", "--hard", "HEAD")
                elif variant in ("untracked", "ignored"):
                    path.unlink()
                    if variant == "ignored":
                        path.parent.rmdir()
                else:
                    git.git(self.checkout, "update-index", "--no-" + variant, "hello.py")

    def test_unknown_process_requires_audited_confirmation(self):
        self.run["dispatches"].append({"id": "old", "process_state": "starting"})
        write_json(self.state, self.run)
        with self.assertRaisesRegex(HarnessError, "无法确认"):
            self.cleanup(True)
        self.assertEqual(self.cleanup(True, confirm_exited=["old"])["status"], "removed")
        self.assertTrue(read_json(self.state)["dispatches"][-1]["exit_confirmed"])
        self.assertIn("dispatch_exit_confirmed", (self.directory / "events.jsonl").read_text())

    def test_unknown_command_child_blocks_even_with_confirmed_parent(self):
        commands = self.directory / "dispatches" / "old" / "commands"
        commands.mkdir(parents=True)
        self.run["dispatches"].append({"id": "old", "exit_confirmed": True,
                                       "command_directory": str(commands)})
        write_json(self.state, self.run)
        folder = commands / ("a" * 32)
        folder.mkdir()
        write_json(folder / "process.json", {"id": "old:command:" + folder.name,
            "command_id": "test", "pgid": None, "process_state": "starting", "exit_confirmed": False})
        with self.assertRaisesRegex(HarnessError, "Cannot confirm command"):
            self.cleanup(True)
        self.assertTrue(self.checkout.exists())
        self.assertEqual(self.cleanup(True, confirm_exited=["old"])["status"], "removed")
        self.assertTrue(read_json(self.state)["dispatches"][-1]["command_exit_confirmations"])

    def test_redirected_git_binding_refuses(self):
        path = self.checkout / ".git"
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        self.assert_blocked("绑定")
        self.assertTrue(self.checkout.exists())

    def test_missing_path_does_not_authorize_stale_registration_removal(self):
        shutil.rmtree(self.checkout)
        self.assert_blocked("清理意图或 Git 注册不符")
        self.assertIn(str(self.checkout), git.git(self.repo, "worktree", "list", "--porcelain"))

    def test_interrupted_removal_recovers_saved_intent(self):
        original_git = git.git
        def interrupted(repo, *args, **kwargs):
            result = original_git(repo, *args, **kwargs)
            if args[:2] == ("worktree", "remove"):
                raise KeyboardInterrupt()
            return result
        with patch("harness.cleanup.git.git", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.cleanup(True)
        self.assertEqual(read_json(self.receipt)["status"], "pending")
        self.assertEqual(self.cleanup(True)["status"], "removed")
        self.assertEqual(read_json(self.state)["status"], "completed")

    def interrupt_removal(self, *, after_git):
        original_git = git.git

        def interrupted(repo, *args, **kwargs):
            if args[:2] == ("worktree", "remove") and not after_git:
                raise KeyboardInterrupt()
            result = original_git(repo, *args, **kwargs)
            if args[:2] == ("worktree", "remove"):
                raise KeyboardInterrupt()
            return result
        with patch("harness.cleanup.git.git", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.cleanup(True)
        self.assertEqual(read_json(self.receipt)["status"], "pending")
        self.assertEqual(self.checkout.exists(), not after_git)

    def continue_after_exit_confirmation(self, *, after_git):
        self.interrupt_removal(after_git=after_git)
        # A dispatch whose exit is unknown blocks the gate until it is confirmed.
        run = read_json(self.state)
        run["dispatches"].append({"id": "old", "process_state": "starting"})
        write_json(self.state, run)
        with self.assertRaisesRegex(HarnessError, "无法确认"):
            self.cleanup(True)
        before = self.state.read_bytes()
        self.assertEqual(self.cleanup(True, confirm_exited=["old"])["status"], "removed")
        self.assertNotEqual(self.state.read_bytes(), before)
        self.assertFalse(self.checkout.exists())
        self.assertEqual(read_json(self.receipt)["status"], "removed")
        self.assertEqual(self.cleanup()["status"], "absent")

    def test_pending_removal_before_git_continues_after_exit_confirmation(self):
        self.continue_after_exit_confirmation(after_git=False)

    def test_pending_removal_after_git_continues_after_exit_confirmation(self):
        self.continue_after_exit_confirmation(after_git=True)

    def test_state_race_after_saved_intent_refuses_deletion(self):
        original_write = write_json
        def raced(path, value):
            original_write(path, value)
            if path == self.receipt:
                self.state.write_bytes(self.state.read_bytes() + b"\n")
        with patch("harness.cleanup.write_json", side_effect=raced):
            self.assert_blocked("台账在清理前改变")
        self.assertTrue(self.checkout.exists())
        self.assertEqual(read_json(self.receipt)["status"], "pending")

    def test_changed_branch_refuses_loss_of_named_checkpoint(self):
        git.git(self.checkout, "checkout", "--detach")
        git.git(self.repo, "update-ref", "refs/heads/" + self.run["branch"], self.run["base_commit"])
        self.assert_blocked("检查点")
        self.assertTrue(self.checkout.exists())

    def test_private_state_directory_is_never_removed(self):
        (self.checkout / ".harness").mkdir()
        self.assert_blocked("受保护路径")
        self.assertTrue((self.checkout / ".harness").is_dir())

    def test_raw_bytes_with_restored_stat_cache_still_refuse_removal(self):
        import os
        path = self.checkout / "hello.py"
        original = path.read_bytes()
        info = path.stat()
        path.write_bytes(original.replace(b"Hello", b"Hallo"))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assert_blocked("文件内容与 HEAD 不同")
        self.assertTrue(path.exists())
