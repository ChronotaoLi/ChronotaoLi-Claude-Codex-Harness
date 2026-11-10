"""Versioned receiving state cannot downgrade execution or bypass cleanup gates."""
import shutil

from harness.cli import status_summary
from harness.run_cleanup import cleanup_run
from harness.util import HarnessError, read_json, write_json
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT


class ReceiptVersionBoundaryTests(RepoCase):
    def test_schema14_is_history_only_without_migration_or_dispatch(self):
        backend = RecordingBackend()
        runtime = self.engine(backend)
        state = runtime.start('Implement greet', read_json(ROOT / 'examples/plan.json'), pause_after_plan=True)
        state['schema_version'] = 14
        path = runtime.directory / 'state.json'
        write_json(path, state)
        before = path.read_bytes()
        with self.assertRaisesRegex(HarnessError, '旧 run'):
            self.engine(backend).resume(state['id'], approve_plan=True)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(backend.calls, [])

    def test_current_capture_contract_and_config_snapshot_cannot_be_omitted(self):
        import copy
        runtime = self.engine()
        state = runtime.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
        original = copy.deepcopy(state)
        path = runtime.directory / 'state.json'
        for missing in ('capture', 'output_mode', 'invoked'):
            with self.subTest(missing=missing):
                state = copy.deepcopy(original)
                worker = next(d for d in state['dispatches'] if d['role'] == 'worker')
                if missing == 'capture':
                    worker['worker_receipt'].pop('capture')
                elif missing == 'output_mode':
                    state['config_snapshot']['runtime'].pop('claude_output_mode')
                else:
                    worker.pop('invoked')
                write_json(path, state)
                before = path.read_bytes()
                with self.assertRaises(HarnessError):
                    self.engine().resume(state['id'])
                self.assertEqual(path.read_bytes(), before)

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

    def test_current_schema_missing_receipts_blocks_terminal_resume_and_cleanup(self):
        backend = RecordingBackend()
        runtime = self.engine(backend)
        state = runtime.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.assertEqual(state["schema_version"], 16)
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
