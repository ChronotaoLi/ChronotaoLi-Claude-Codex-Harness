import errno
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

from git_isolation import fixture_git
from harness import cleanup
from harness.util import read_json
from test_harness import RepoCase, ROOT, after_save
from test_runtime_changes import ScriptedBackend
from test_state_files import FakeOs


class CheckoutRootTests(RepoCase):
    """D7-T0: removal checks the root of a missing, still-registered checkout."""

    def setUp(self):
        super().setUp()
        self.config.data["verification"].update(fresh_checkout=True, commands=[
            {"cwd": ".", "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('hello.py').is_file()"]},
        ])
        self.backend = ScriptedBackend()
        self.runtime = self.engine(self.backend)

    def start(self):
        return self.runtime.start("Implement greet", read_json(ROOT / "examples/plan.json"))

    def capture_checkout(self):
        artifact, = self.runtime.directory.glob("verification/final/*/verification-*/checkout.json")
        evidence = read_json(artifact)
        self.checkout = Path(evidence["worktree"])
        self.root = self.checkout.parents[1]
        self.admin = Path(evidence["binding"]["git_dir"])
        self.receipt = artifact.with_name("cleanup.json")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def retain_checkout(self):
        with patch("harness.finalization.FinalAcceptance.reclaim_verified_checkout"):
            self.start()
        self.capture_checkout()

    def cleanup_checkout(self, *, remove=False):
        item, = cleanup.cleanup_run(self.repo, self.runtime.run["id"], remove=remove)["checkouts"]
        return item

    def assert_registered(self, registered=True):
        records = fixture_git(self.repo, "worktree", "list", "--porcelain", "-z").split("\0")
        self.assertEqual(f"worktree {self.checkout}" in records, registered)
        self.assertEqual(self.admin.is_dir(), registered)

    def test_missing_root_blocks_preview_and_removal(self):
        self.retain_checkout()
        shutil.rmtree(self.root)
        self.assert_registered()
        for remove in (False, True):
            with self.subTest(remove=remove):
                item = self.cleanup_checkout(remove=remove)
                self.assertEqual(item["status"], "blocked")
                self.assertEqual(item["recorded_worktree"], str(self.checkout))
                self.assert_registered()
                self.assertFalse(self.receipt.exists())

    def test_existing_root_allows_removal_of_a_stale_registration(self):
        self.retain_checkout()
        shutil.rmtree(self.checkout)
        self.assertEqual(self.cleanup_checkout()["status"], "stale_registration")
        self.assert_registered()
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.cleanup_checkout(remove=True)["status"], "removed")
        self.assert_registered(False)
        self.assertEqual(read_json(self.receipt)["status"], "removed")

    def test_root_is_checked_again_after_the_removal_intent(self):
        self.retain_checkout()
        shutil.rmtree(self.checkout)
        write_json = cleanup.write_json

        def remove_root_after_intent(path, data):
            write_json(path, data)
            if path == self.receipt and data["status"] == "pending":
                shutil.rmtree(self.root)

        with patch.object(cleanup, "write_json", side_effect=remove_root_after_intent):
            self.assertEqual(self.cleanup_checkout(remove=True)["status"], "blocked")
        self.assertFalse(self.root.exists())
        self.assert_registered()
        self.assertEqual(read_json(self.receipt)["status"], "pending")

    def test_root_lstat_io_failure_blocks_removal(self):
        self.retain_checkout()
        shutil.rmtree(self.checkout)

        def fail_root_probe(lstat, path):
            if path == self.root:
                raise OSError(errno.EIO, os.strerror(errno.EIO), path)
            return lstat(path)

        with patch.object(cleanup, "os", FakeOs(lstat=fail_root_probe)):
            item = self.cleanup_checkout(remove=True)
        self.assertEqual(item["status"], "blocked")
        self.assertIn(os.strerror(errno.EIO), item["error"])
        self.assert_registered()
        self.assertFalse(self.receipt.exists())

    def test_runtime_reclamation_preserves_registration_and_review_proceeds(self):
        def verdict_saved(run):
            return any("checkout_digest" in row for attempt in run["final_history"]
                       for row in attempt["verification_rounds"])

        def remove_root():
            self.capture_checkout()
            shutil.rmtree(self.root)

        hook, _ = after_save(self.runtime, verdict_saved, remove_root)
        with hook:
            result = self.start()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(call["kind"] == "final_review" for call in self.backend.calls), 1)
        self.assert_registered()
        self.assertFalse(self.receipt.exists())
        events = [json.loads(line) for line in (self.runtime.directory / "events.jsonl").read_text().splitlines()]
        failed, = [event for event in events if event["event"] == "verification_checkout_reclaim_failed"]
        self.assertEqual(failed["recorded_worktree"], str(self.checkout))
