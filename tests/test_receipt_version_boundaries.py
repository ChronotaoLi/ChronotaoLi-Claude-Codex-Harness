"""Versioned receiving state cannot downgrade execution or bypass cleanup gates."""
import shutil

from harness.cleanup import cleanup_run
from harness.util import HarnessError, read_json, write_json
from harness.versions import RUN_SCHEMA_VERSION
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT


class ReceiptVersionBoundaryTests(RepoCase):
    def test_previous_version_is_refused_without_migration_or_dispatch(self):
        backend = RecordingBackend()
        runtime = self.engine(backend)
        state = runtime.start('Implement greet', read_json(ROOT / 'examples/plan.json'), pause_after_plan=True)
        state['schema_version'] = RUN_SCHEMA_VERSION - 1
        path = runtime.directory / 'state.json'
        write_json(path, state)
        before = path.read_bytes()
        with self.assertRaisesRegex(HarnessError, f'台账版本 {RUN_SCHEMA_VERSION - 1} 不支持'):
            self.engine(backend).resume(state['id'], approve_plan=True)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(backend.calls, [])

    def test_current_ledger_refuses_missing_invocation_fact_and_removed_output_mode(self):
        runtime = self.engine()
        state = runtime.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
        shutil.rmtree(runtime.worktree / '__pycache__', ignore_errors=True)
        path = runtime.directory / 'state.json'
        worker = next(d for d in state['dispatches'] if d['role'] == 'worker')
        worker.pop('invoked')
        write_json(path, state)
        before = path.read_bytes()
        with self.assertRaises(HarnessError):
            self.engine().resume(state['id'])
        self.assertEqual(path.read_bytes(), before)
        # A saved configuration snapshot cannot carry the removed output-mode key.
        paused = self.engine().start('Implement greet', read_json(ROOT / 'examples/plan.json'), pause_after_plan=True)
        paused['config_snapshot']['runtime']['claude_output_mode'] = 'legacy_json'
        path = self.repo / '.harness' / 'runs' / paused['id'] / 'state.json'
        write_json(path, paused)
        before = path.read_bytes()
        with self.assertRaisesRegex(HarnessError, '未知配置键：runtime.claude_output_mode'):
            self.engine().resume(paused['id'], approve_plan=True)
        self.assertEqual(path.read_bytes(), before)

    def test_current_schema_missing_receipts_blocks_terminal_resume_and_cleanup(self):
        backend = RecordingBackend()
        runtime = self.engine(backend)
        state = runtime.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.assertEqual(state["schema_version"], RUN_SCHEMA_VERSION)
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
