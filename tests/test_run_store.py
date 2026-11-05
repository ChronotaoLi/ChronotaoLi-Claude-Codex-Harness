from concurrent.futures import ThreadPoolExecutor
import threading
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.run_store import RunStore
from harness.util import HarnessError


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RunStore(self.root)

    def state(self, ident="run_one"):
        return {"id": ident, "accepted": {}, "pending_acceptance": {"task_id": "one"}}

    def load(self, ident="run_one"):
        return json.loads((self.root / "runs" / ident / "state.json").read_text())

    def test_sequential_acceptance_and_post_accept_barriers(self):
        run = self.state()
        self.store.attach(run)
        self.assertEqual(self.store.commit(), 1)
        self.assertIn("pending_acceptance", self.load())
        run["accepted"]["one"] = {"commit": "abc"}
        run.pop("pending_acceptance")
        run["pending_post_accept"] = {"task_id": "one"}
        self.store.commit()
        saved = self.load()
        self.assertEqual(saved["store_revision"], 2)
        self.assertIn("one", saved["accepted"])
        self.assertIn("pending_post_accept", saved)

    def test_two_loaded_writers_cannot_overwrite(self):
        self.store.attach(self.state())
        self.store.commit()
        other = RunStore(self.root)
        other.attach(self.load())
        self.store.run["status"] = "accepted"
        self.store.commit()
        other.run["status"] = "blocked"
        with self.assertRaisesRegex(HarnessError, "其他写入者"):
            other.commit()
        self.assertEqual(self.load()["status"], "accepted")

    def test_competing_new_runs_cannot_replace_first_publication(self):
        self.store.attach(self.state())
        other = RunStore(self.root)
        other.attach(self.state())
        self.store.run["status"] = "planning"
        self.store.commit()
        other.run["status"] = "blocked"
        with self.assertRaises(HarnessError):
            other.commit()
        self.assertEqual(self.load()["status"], "planning")

    def test_deleted_source_is_not_recreated_by_stale_writer(self):
        self.store.attach(self.state())
        self.store.commit()
        path = self.root / "runs/run_one/state.json"
        path.unlink()
        with self.assertRaises(HarnessError):
            self.store.commit()
        self.assertFalse(path.exists())

    def test_concurrent_commits_publish_only_one_revision(self):
        self.store.attach(self.state())
        self.store.commit()
        other = RunStore(self.root)
        other.attach(self.load())
        barrier = threading.Barrier(2)

        def commit(store, status):
            store.run["status"] = status
            barrier.wait()
            try:
                store.commit()
                return status
            except HarnessError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(commit, self.store, "first")
            second = executor.submit(commit, other, "second")
            published = [result for future in (first, second) if (result := future.result())]
        self.assertEqual(len(published), 1)
        self.assertEqual(self.load()["status"], published[0])
        self.assertEqual(self.load()["store_revision"], 2)

    def test_external_edit_at_same_revision_rejected(self):
        self.store.attach(self.state())
        self.store.commit()
        path = self.root / "runs/run_one/state.json"
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(HarnessError, "其他写入者"):
            self.store.commit()

    def test_stale_attachment_does_not_adopt_new_source(self):
        self.store.attach(self.state())
        self.store.commit()
        stale = self.load()
        self.store.commit()
        with self.assertRaisesRegex(HarnessError, "来源不一致"):
            RunStore(self.root).attach(stale)

    def test_switching_runs_preserves_separate_revisions(self):
        self.store.attach(self.state())
        self.store.commit()
        self.store.attach(self.state("run_two"))
        self.assertEqual(self.store.commit(), 1)
        self.store.attach(self.load())
        self.assertEqual(self.store.commit(), 2)
        self.assertEqual(self.load("run_two")["store_revision"], 1)

    def test_write_failure_does_not_advance_expected_revision(self):
        self.store.attach(self.state())
        with patch("harness.run_store.write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.commit()
        self.assertNotIn("store_revision", self.store.run)
        self.assertEqual(self.store.commit(), 1)

    def test_revision_and_identity_tampering_rejected(self):
        self.store.attach(self.state())
        self.store.commit()
        self.store.run["store_revision"] = 7
        with self.assertRaises(HarnessError):
            self.store.commit()
        self.store.run["store_revision"] = 1
        self.store.run["id"] = "run_other"
        with self.assertRaises(HarnessError):
            self.store.commit()
