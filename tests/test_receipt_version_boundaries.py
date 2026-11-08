"""Versioned receiving state cannot downgrade execution or bypass cleanup gates."""
import shutil

from harness.cli import status_summary
from harness.run_cleanup import cleanup_run
from harness.util import HarnessError, read_json, write_json
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT


class ReceiptVersionBoundaryTests(RepoCase):
    def test_schema13_stays_inspectable_and_cleanable_but_cannot_resume(self):
        backend = RecordingBackend()
        runtime = self.engine(backend)
        state = runtime.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        shutil.rmtree(runtime.worktree / "__pycache__", ignore_errors=True)
        state["schema_version"] = 13
        state.pop("worker_receipts")
        for dispatch in state["dispatches"]:
            dispatch.pop("worker_receipt", None)
            dispatch.pop("worker_receipt_consumed", None)
        path = runtime.directory / "state.json"
        write_json(path, state)
        before = path.read_bytes()
        calls = len(backend.calls)
        self.assertEqual(status_summary(state)["status"], "completed")
        with self.assertRaisesRegex(HarnessError, "旧 run"):
            self.engine(backend).resume(state["id"])
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(cleanup_run(self.repo, state["id"])["status"], "eligible")
        self.assertEqual(cleanup_run(self.repo, state["id"], remove=True)["status"], "removed")
        self.assertEqual(path.read_bytes(), before)

    def test_schema14_missing_receipts_blocks_terminal_resume_and_cleanup(self):
        backend = RecordingBackend()
        runtime = self.engine(backend)
        state = runtime.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.assertEqual(state["schema_version"], 14)
        shutil.rmtree(runtime.worktree / "__pycache__", ignore_errors=True)
        state.pop("worker_receipts")
        path = runtime.directory / "state.json"
        write_json(path, state)
        before = path.read_bytes()
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(state["id"])
        with self.assertRaisesRegex(HarnessError, "worker receipts"):
            cleanup_run(self.repo, state["id"], remove=True)
        self.assertTrue(runtime.worktree.exists())
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(path.read_bytes(), before)
