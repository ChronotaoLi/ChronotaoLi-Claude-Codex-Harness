import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

from harness import git
from harness.cleanup import cleanup_verification
from harness.cli import main
from harness.util import HarnessError, lock, read_json, write_json
from test_harness import RepoCase, ROOT


class VerificationCleanupTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.config.data["verification"].update(fresh_checkout=True,
            commands=[[sys.executable, "-c", "from pathlib import Path; assert Path('hello.py').is_file()"]])
        self.engine_instance = self.engine()
        self.run = self.engine_instance.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.directory = self.engine_instance.directory
        self.artifact = next(self.directory.glob("verification/final/*/verification-*/checkout.json"))
        self.environment = read_json(self.artifact)
        self.checkout = Path(self.environment["worktree"])
        self.roots = [self.checkout.parents[1]]
        self.addCleanup(self.cleanup_roots)

    def cleanup_roots(self):
        for root in self.roots:
            shutil.rmtree(root, ignore_errors=True)

    def cleanup(self, remove=False):
        return cleanup_verification(self.repo, self.run["id"], remove=remove)

    def additional_checkout(self):
        root = Path(tempfile.mkdtemp(prefix="harness-verify-")).resolve()
        self.roots.append(root)
        checkout = root / ".harness" / "checkout"
        git.git(self.repo, "worktree", "add", "--detach", str(checkout), self.environment["head"])
        environment = {**self.environment, "worktree": str(checkout),
                       "binding": git.capture_worktree_binding(checkout, None)}
        final = self.run["final_history"][-1]
        folder = self.artifact.parent.parent / f"verification-{len(final['verification_rounds']) + 1}"
        artifact = folder / "checkout.json"
        write_json(artifact, environment)
        final["verification_rounds"].append({"artifact_dir": str(folder), "status": "completed"})
        write_json(self.directory / "state.json", self.run)
        return checkout, artifact

    def test_preview_then_remove_preserves_run_and_evidence_and_is_idempotent(self):
        evidence = self.artifact.read_bytes()
        ledger = (self.directory / "state.json").read_bytes()
        results = (self.artifact.parent / "results.json").read_bytes()
        self.assertEqual(self.cleanup()["checkouts"][0]["status"], "eligible")
        self.assertTrue(self.checkout.is_dir())
        self.assertFalse(self.artifact.with_name("cleanup.json").exists())
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "removed")
        self.assertFalse(self.checkout.parents[1].exists())
        self.assertEqual(self.artifact.read_bytes(), evidence)
        self.assertEqual((self.artifact.parent / "results.json").read_bytes(), results)
        self.assertEqual((self.directory / "state.json").read_bytes(), ledger)
        self.assertTrue(self.engine_instance.worktree.is_dir())
        self.assertEqual(git.head(self.engine_instance.worktree), self.run["expected_head"])
        self.assertEqual(self.cleanup()["checkouts"][0]["status"], "absent")
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "removed")
        self.assertEqual(read_json(self.artifact.with_name("cleanup.json"))["status"], "removed")

    def test_refuses_modified_untracked_ignored_and_hidden_index_files(self):
        variants = ("tracked", "untracked", "ignored", "assume-unchanged", "skip-worktree")
        for variant in variants:
            with self.subTest(variant=variant):
                if variant == "tracked":
                    (self.checkout / "hello.py").write_text("unsaved")
                elif variant in ("untracked", "ignored"):
                    name = "scratch" if variant == "untracked" else "__pycache__/scratch"
                    path = self.checkout / name
                    path.parent.mkdir(exist_ok=True)
                    path.write_text("unsaved")
                else:
                    git.git(self.checkout, "update-index", "--" + variant, "hello.py")
                item = self.cleanup(remove=True)["checkouts"][0]
                self.assertEqual(item["status"], "blocked")
                self.assertTrue(self.checkout.is_dir())
                self.assertFalse(self.artifact.with_name("cleanup.json").exists())
                if variant == "tracked":
                    git.git(self.checkout, "checkout", "--", "hello.py")
                elif variant in ("untracked", "ignored"):
                    self.assertEqual(path.read_text(), "unsaved")
                    path.unlink()
                    if variant == "ignored":
                        path.parent.rmdir()
                else:
                    git.git(self.checkout, "update-index", "--no-" + variant, "hello.py")

    def test_run_lock_and_live_or_unknown_process_refuse_all_removal(self):
        with lock(self.directory / "run.lock"), self.assertRaisesRegex(HarnessError, "运行锁"):
            self.cleanup(remove=True)
        self.run["dispatches"].append({"id": "old", "role": "verification", "pgid": 12345,
                                       "status": "running", "exit_confirmed": False})
        write_json(self.directory / "state.json", self.run)
        with patch("harness.cleanup.group_alive", return_value=True), self.assertRaisesRegex(HarnessError, "进程组仍存在"):
            self.cleanup(remove=True)
        self.run["dispatches"][-1]["pgid"] = None
        write_json(self.directory / "state.json", self.run)
        with self.assertRaisesRegex(HarnessError, "无法确认"):
            self.cleanup(remove=True)
        self.assertTrue(self.checkout.exists())
        self.assertFalse(self.artifact.with_name("cleanup.json").exists())

    def test_changed_binding_and_run_worktree_substitution_are_preserved(self):
        gitfile = self.checkout / ".git"
        original = gitfile.read_bytes()
        gitfile.write_bytes(original + b"\n")
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "blocked")
        gitfile.write_bytes(original)
        write_json(self.artifact, {**self.environment, "worktree": str(self.engine_instance.worktree)})
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "blocked")
        self.assertTrue(self.engine_instance.worktree.exists())
        self.assertTrue(self.checkout.exists())
        write_json(self.artifact, {**self.environment, "head": "0" * 40})
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "blocked")
        write_json(self.artifact, self.environment)
        git.git(self.checkout, "checkout", "--detach", self.run["base_commit"])
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "blocked")
        self.assertTrue(self.checkout.exists())

    def test_missing_checkout_unregisters_only_exact_saved_binding(self):
        other, other_artifact = self.additional_checkout()
        shutil.rmtree(self.checkout)
        shutil.rmtree(other)
        # The unreferenced stale checkout must never be touched by a broad prune.
        self.run["final_history"][-1]["verification_rounds"].pop()
        write_json(self.directory / "state.json", self.run)
        other_artifact.unlink()
        self.assertEqual(self.cleanup()["checkouts"][0]["status"], "stale_registration")
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "removed")
        registrations = git.git(self.repo, "worktree", "list", "--porcelain")
        self.assertNotIn(str(self.checkout), registrations)
        self.assertIn(str(other), registrations)

    def test_missing_checkout_with_tampered_backlink_keeps_registration(self):
        shutil.rmtree(self.checkout)
        backlink = Path(self.environment["binding"]["git_dir"]) / "gitdir"
        backlink.write_text(str(self.engine_instance.worktree / ".git") + "\n")
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "blocked")
        self.assertTrue(backlink.parent.exists())
        self.assertTrue(self.engine_instance.worktree.exists())

    def test_completed_cleanup_ignores_reused_git_administrative_name(self):
        self.cleanup(remove=True)
        other, artifact = self.additional_checkout()
        self.assertEqual(read_json(artifact)["binding"]["git_dir"], self.environment["binding"]["git_dir"])
        result = self.cleanup()
        self.assertEqual([item["status"] for item in result["checkouts"]], ["absent", "eligible"])
        self.assertTrue(other.is_dir())

    def test_interruption_after_git_removal_recovers_pending_receipt(self):
        original_git = git.git

        def interrupted(repo, *args, **kwargs):
            result = original_git(repo, *args, **kwargs)
            if args[:2] == ("worktree", "remove"):
                raise KeyboardInterrupt()
            return result

        with patch("harness.cleanup.git.git", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.cleanup(remove=True)
        receipt = read_json(self.artifact.with_name("cleanup.json"))
        self.assertEqual(receipt["status"], "pending")
        self.assertFalse(self.checkout.exists())
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "removed")
        self.assertFalse(self.checkout.parents[1].exists())

    def test_partial_cleanup_preserves_dirty_checkout_and_unknown_sibling(self):
        other, _ = self.additional_checkout()
        (self.checkout / "scratch").write_text("keep")
        sibling = other.parents[1] / "operator-notes"
        sibling.write_text("keep")
        statuses = [item["status"] for item in self.cleanup(remove=True)["checkouts"]]
        self.assertEqual(statuses, ["blocked", "removed"])
        self.assertEqual((self.checkout / "scratch").read_text(), "keep")
        self.assertFalse(other.exists())
        self.assertEqual(sibling.read_text(), "keep")

    def test_cli_works_without_configuration_and_reports_refusals(self):
        (self.checkout / "__pycache__").mkdir()
        (self.checkout / "__pycache__" / "generated").write_text("keep")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--repo", str(self.repo), "--config", str(self.repo / "absent.toml"),
                         "cleanup-verification", self.run["id"], "--remove"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["checkouts"][0]["status"], "blocked")
        self.assertEqual(stderr.getvalue(), "")
        self.assertTrue(self.checkout.exists())
