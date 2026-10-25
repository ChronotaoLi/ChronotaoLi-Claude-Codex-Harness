import copy
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.shared import HookError, read_skill
from harness.util import HarnessError, read_json, write_json
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT
from test_runtime_changes import finding


class DispatchAccountingTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def test_worker_context_overflow_preserves_budget_and_pristine_dispatch_on_resume(self):
        def lengthy_worker_skill(path, name):
            return "Worker instructions " * 2000 if name == "harness-worker" else read_skill(path, name)

        self.config.data["runtime"].update(max_attempts=1, max_total_attempts=1, context_chars=32000)
        backend = RecordingBackend()
        engine = self.engine(backend)
        with patch("harness.packets.read_skill", side_effect=lengthy_worker_skill):
            with self.assertRaises(HarnessError):
                engine.start("Implement greet", self.plan())
            for _ in range(2):
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                saved = read_json(engine.directory / "state.json")
                self.assertEqual(saved["attempts"], {})
                self.assertEqual(saved["total_attempts"], 0)
                self.assertEqual(saved["agent_dispatches"], 0)
                self.assertEqual(backend.requests, [])
            self.config.data["runtime"]["context_chars"] = 100000
            result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 1})
        self.assertFalse(any(call["role"] == "commander" for call in backend.calls))

    def test_pre_agent_failure_finishes_entry_without_spending_worker_budget(self):
        backend = RecordingBackend()
        engine = self.engine(backend)

        def deny_worker(event, payload, logs):
            if event == "pre_agent" and payload["role"] == "worker":
                raise HookError(event, 7, logs)

        with patch.object(engine.hooks, "emit", side_effect=deny_worker):
            with self.assertRaises(HookError):
                engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        entry = saved["dispatches"][-1]
        self.assertEqual(entry["status"], "failed")
        self.assertTrue(entry["exit_confirmed"])
        self.assertEqual(saved["attempts"], {})
        self.assertEqual(saved["total_attempts"], 0)
        self.assertEqual(saved["agent_dispatches"], 0)
        self.assertEqual(backend.requests, [])
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 1})
        self.assertFalse(any(call["kind"] == "dispatch" for call in backend.calls))

    def test_snapshot_failure_finishes_prepared_dispatch_without_accounting(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        original_snapshot = git.snapshot

        def unreadable_candidate(path, **kwargs):
            entries = engine.run["dispatches"] if engine.run else []
            if entries and entries[-1]["role"] == "worker" and entries[-1]["status"] == "running":
                raise HarnessError("candidate snapshot unavailable")
            return original_snapshot(path, **kwargs)

        with patch("harness.engine.git.snapshot", side_effect=unreadable_candidate):
            with self.assertRaisesRegex(HarnessError, "snapshot unavailable"):
                engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["dispatches"][-1]["status"], "failed")
        self.assertTrue(saved["dispatches"][-1]["exit_confirmed"])
        self.assertEqual(saved["attempts"], {})
        self.assertEqual(saved["total_attempts"], 0)
        self.assertEqual(saved["agent_dispatches"], 0)
        self.assertEqual(backend.requests, [])

    def test_exhausted_dispatch_budget_does_not_spend_worker_attempt(self):
        plan = self.plan()
        second = copy.deepcopy(plan["tasks"][0])
        second.update(id="second", title="Second independent review")
        plan["tasks"].append(second)
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        backend = RecordingBackend()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "max_agent_dispatches"):
            engine.start("Implement greet", plan)
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["agent_dispatches"], 1)
        self.assertEqual(saved["attempts"], {})
        self.assertEqual(saved["total_attempts"], 0)
        self.assertEqual([call["role"] for call in backend.calls], ["commander"])
        self.assertTrue(saved["dispatches"][-1]["exit_confirmed"])
        self.assertEqual(saved["dispatches"][-1]["status"], "failed")

    def test_backend_failure_keeps_worker_and_dispatch_counts_persisted_together(self):
        def unavailable(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                saved = read_json(kwargs["run_path"])
                self.assertEqual(saved["attempts"], {"greet": 1})
                self.assertEqual(saved["total_attempts"], 1)
                self.assertEqual(saved["agent_dispatches"], 1)
                raise HarnessError("worker provider unavailable")

        self.config.data["runtime"]["max_attempts"] = 1
        backend = RecordingBackend(unavailable)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "provider unavailable"):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["attempts"], {"greet": 1})
        self.assertEqual(saved["total_attempts"], 1)
        self.assertEqual(saved["agent_dispatches"], 1)
        with self.assertRaisesRegex(HarnessError, "尝试次数上限"):
            self.engine(backend).resume(saved["id"])
        self.assertEqual(len(backend.requests), 1)

    def fail_worker_correction_preparation(self, engine, backend):
        def deny_correction(event, payload, logs):
            if event == "pre_agent" and payload["role"] == "worker" and backend.calls:
                raise HookError(event, 7, logs)

        with patch.object(engine.hooks, "emit", side_effect=deny_correction):
            with self.assertRaises(HookError):
                engine.start("Implement greet", self.plan())

    def test_worker_correction_preparation_can_resume_without_spending_attempt(self):
        def malformed_once(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result["summary"] = "x" * 4001

        self.config.data["runtime"].update(max_attempts=2, max_total_attempts=2)
        backend = RecordingBackend(malformed_once)
        engine = self.engine(backend)
        self.fail_worker_correction_preparation(engine, backend)
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["attempts"], {"greet": 1})
        self.assertEqual(saved["total_attempts"], 1)
        self.assertEqual(saved["agent_dispatches"], 1)
        self.assertEqual(saved["format_repairs"], {})
        self.assertIsNone(saved["feedback"])
        self.assertIn("worker:implement:greet", saved["pending_format_corrections"])
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_attempts"], 2)
        workers = [call for call in backend.calls if call["role"] == "worker"]
        self.assertIn("format_correction", workers[-1]["packet"])
        self.assertIsNone(workers[-1]["packet"]["feedback"])
        self.assertEqual(result["pending_format_corrections"], {})
        self.assertEqual(result["format_repairs"], {"worker:implement:greet": 1})
        self.assertEqual(result["agent_dispatches"], len(backend.requests))

    def test_changed_candidate_discards_pending_worker_format_correction(self):
        def malformed_once(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result["summary"] = "x" * 4001

        backend = RecordingBackend(malformed_once)
        engine = self.engine(backend)
        self.fail_worker_correction_preparation(engine, backend)
        with (engine.worktree / "hello.py").open("a", encoding="utf-8") as output:
            output.write("# candidate changed while the correction was pending\n")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        workers = [call for call in backend.calls if call["role"] == "worker"]
        self.assertNotIn("format_correction", workers[-1]["packet"])
        self.assertEqual(result["pending_format_corrections"], {})
        self.assertEqual(result["format_repairs"], {})

    def test_replan_correction_is_separate_from_worker_feedback(self):
        def revise_after_blocker(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="task", blockers=["Clarify prerequisite"])
            elif kwargs["kind"] == "replan":
                first = sum(c["kind"] == "replan" for c in backend.calls) == 1
                result.update(action="revise", reason="x" * 4001 if first else "Clarified task",
                              new_tasks=[], updates=[{"id": "greet", "description": "Implement complete greeting",
                                                      "add_depends_on": []}])

        backend = RecordingBackend(revise_after_blocker)
        result = self.engine(backend).start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        replans = [call for call in backend.calls if call["kind"] == "replan"]
        self.assertEqual(len(replans), 2)
        self.assertIn("format_correction", replans[1]["packet"])
        for call in backend.calls:
            if call["role"] == "worker":
                self.assertNotIn("format_correction", call["packet"])
                self.assertNotIn("format_correction", call["packet"].get("feedback") or {})
        self.assertEqual(result["pending_format_corrections"], {})

    def test_review_correction_guard_fails_closed_on_changed_or_missing_original(self):
        def reject_then_unavailable(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                number = sum(call["kind"] == "review" for call in backend.calls)
                if number > 1:
                    raise HarnessError("correction service unavailable")
                result.update(verdict="request_changes", findings=[finding()], summary="x" * 4001)

        for mode in ("changed", "missing"):
            with self.subTest(mode=mode):
                backend = RecordingBackend(reject_then_unavailable)
                engine = self.engine(backend)
                with self.assertRaisesRegex(HarnessError, "correction service"):
                    engine.start("Implement greet", self.plan())
                guard = engine.run["review_corrections"]["reviewer:review:greet"]
                self.assertIn("artifact_digest", guard)
                artifact = Path(guard["artifact"])
                if mode == "missing":
                    artifact.unlink()
                else:
                    changed = read_json(artifact)
                    changed.update(verdict="approve", findings=[])
                    write_json(artifact, changed)
                requests = list(backend.requests)
                resumed = self.engine(backend)
                with self.assertRaisesRegex(HarnessError, "原始结果"):
                    resumed.resume(engine.run["id"])
                self.assertEqual(backend.requests, requests)
                self.assertEqual(resumed.run["accepted"], {})
                self.assertEqual(git.head(resumed.worktree), resumed.run["base_commit"])
