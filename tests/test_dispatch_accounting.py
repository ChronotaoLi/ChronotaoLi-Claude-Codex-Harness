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

        self.config.data["runtime"].update(max_attempts=1, context_chars=32000)
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
                self.assertEqual(sum(saved["attempts"].values()), 0)
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
        self.assertEqual(sum(saved["attempts"].values()), 0)
        self.assertEqual(saved["agent_dispatches"], 0)
        self.assertEqual(backend.requests, [])
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 1})
        self.assertFalse(any(call["role"] == "commander" for call in backend.calls))

    def test_snapshot_failure_finishes_prepared_dispatch_without_accounting(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        original_snapshot = git.snapshot

        def unreadable_candidate(path, **kwargs):
            entries = engine.run["dispatches"] if engine.run else []
            if entries and entries[-1]["role"] == "worker" and entries[-1]["status"] == "running":
                raise HarnessError("candidate snapshot unavailable")
            return original_snapshot(path, **kwargs)

        with patch("harness.git.snapshot", side_effect=unreadable_candidate):
            with self.assertRaisesRegex(HarnessError, "snapshot unavailable"):
                engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["dispatches"][-1]["status"], "failed")
        self.assertTrue(saved["dispatches"][-1]["exit_confirmed"])
        self.assertEqual(saved["attempts"], {})
        self.assertEqual(sum(saved["attempts"].values()), 0)
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
        self.assertEqual(sum(saved["attempts"].values()), 0)
        self.assertEqual([call["role"] for call in backend.calls], ["commander"])
        self.assertTrue(saved["dispatches"][-1]["exit_confirmed"])
        self.assertEqual(saved["dispatches"][-1]["status"], "failed")

    def test_backend_failure_keeps_worker_and_dispatch_counts_persisted_together(self):
        def unavailable(backend, kwargs, packet):
            if kwargs["role"] == "worker":
                saved = read_json(kwargs["run_path"])
                self.assertEqual(saved["attempts"], {"greet": 1})
                self.assertEqual(sum(saved["attempts"].values()), 1)
                self.assertEqual(saved["agent_dispatches"], 1)
                raise HarnessError("worker provider unavailable")

        self.config.data["runtime"]["max_attempts"] = 1
        backend = RecordingBackend()
        backend.before_result = unavailable
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "provider unavailable"):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["attempts"], {"greet": 1})
        self.assertEqual(sum(saved["attempts"].values()), 1)
        self.assertEqual(saved["agent_dispatches"], 1)
        self.assertFalse((backend.requests[0]["job"] / "response.json").exists())
        with self.assertRaisesRegex(HarnessError, "尝试次数上限"):
            self.engine(backend).resume(saved["id"])
        self.assertEqual(len(backend.requests), 1)
