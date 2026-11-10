"""Dispatch events observe the ledger: prepared, invoked and finished, never a budget source."""
import copy
import io
import json
import unittest
from unittest.mock import patch

from harness import observe, run_context
from harness.cli import status_summary
from harness.diagnostics import remaining_model_calls
from harness.util import HarnessError, read_json
from harness.watch_cli import watch_run
from test_harness import RepoCase, ROOT
from test_observe import StateCase, dispatch, ledger
from test_runtime_changes import ScriptedBackend

DISPATCH_EVENTS = ("dispatch_prepared", "dispatch_started", "dispatch_finished")


def events(engine):
    return [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]


def two_tasks():
    plan = read_json(ROOT / "examples" / "plan.json")
    second = copy.deepcopy(plan["tasks"][0])
    second.update(id="polish", title="Polish greet", depends_on=["greet"])
    plan["tasks"].append(second)
    return plan


class DispatchEventLifecycleTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def test_completed_run_has_one_started_event_per_counted_model_call(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        records = events(engine)
        started = [e for e in records if e["event"] == "dispatch_started"]
        model = [d for d in result["dispatches"] if d["role"] != "verification"]
        self.assertEqual(len(started), result["agent_dispatches"])
        self.assertEqual(len(started), len(backend.calls))
        self.assertEqual({e["dispatch_id"] for e in started}, {d["id"] for d in model if d["invoked"] is True})
        self.assertTrue(all(e["invoked"] is True for e in started))
        prepared = [e["dispatch_id"] for e in records if e["event"] == "dispatch_prepared"]
        self.assertEqual(prepared, [d["id"] for d in model])
        finished = {e["dispatch_id"]: e for e in records if e["event"] == "dispatch_finished"}
        self.assertEqual(set(finished), {d["id"] for d in model})
        self.assertTrue(all(finished[d["id"]]["invoked"] is d["invoked"] for d in model))
        for ident in prepared:
            order = [e["event"] for e in records if e.get("dispatch_id") == ident and e["event"] in DISPATCH_EVENTS]
            self.assertEqual(order, list(DISPATCH_EVENTS))
        # Local verification processes are not model calls and never had dispatch events.
        verification = {d["id"] for d in result["dispatches"] if d["role"] == "verification"}
        self.assertTrue(verification)
        self.assertFalse(verification & {e.get("dispatch_id") for e in records})

    def test_exhausted_dispatch_budget_prepares_without_starting(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 0
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "模型派发次数上限"):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        entry, = saved["dispatches"]
        self.assertIs(entry["invoked"], False)
        self.assertEqual(saved["agent_dispatches"], 0)
        self.assertEqual(backend.calls, [])
        records = [e for e in events(engine) if e["event"] in DISPATCH_EVENTS]
        self.assertEqual([(e["event"], e["dispatch_id"]) for e in records],
                         [("dispatch_prepared", entry["id"]), ("dispatch_finished", entry["id"])])
        self.assertIs(records[-1]["invoked"], False)
        self.assertEqual(records[-1]["status"], "failed")

    def test_failed_event_appends_change_neither_invocation_nor_outcome(self):
        real = run_context.append_event
        for failing in (DISPATCH_EVENTS, ("dispatch_started",)):
            with self.subTest(failing=failing):
                self.setUp()

                def append(path, run_id, event, **fields):
                    if event in failing:
                        raise OSError("synthetic event log failure")
                    return real(path, run_id, event, **fields)
                backend = ScriptedBackend()
                engine = self.engine(backend)
                with patch("harness.run_context.append_event", side_effect=append):
                    result = engine.start("Implement greet", self.plan())
                self.assertEqual(result["status"], "completed")
                saved = read_json(engine.directory / "state.json")
                model = [d for d in saved["dispatches"] if d["role"] != "verification"]
                self.assertEqual(len(backend.calls), len(model))
                self.assertEqual(saved["agent_dispatches"], len(model))
                self.assertTrue(all(d["invoked"] is True for d in model))
                names = [e["event"] for e in events(engine)]
                self.assertFalse(set(failing) & set(names))
                self.assertIn("run_completed", names)

    def test_replayed_worker_receipt_emits_no_second_start(self):
        from harness.worker_receipts import WorkerReceipts
        real = WorkerReceipts.finish
        interrupted = []

        def crash_once(receipts, key, task):
            if not interrupted:
                interrupted.append(key)
                raise KeyboardInterrupt()
            return real(receipts, key, task)
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with patch.object(WorkerReceipts, "finish", crash_once), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        self.assertIn("worker:implement:greet", read_json(engine.directory / "state.json")["worker_receipts"])
        with patch.object(WorkerReceipts, "finish", crash_once):
            result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual([call["role"] for call in backend.calls].count("worker"), 1)
        started = [e for e in events(engine) if e["event"] == "dispatch_started"]
        self.assertEqual(len(started), result["agent_dispatches"])
        worker, = [d["id"] for d in result["dispatches"] if d["role"] == "worker"]
        self.assertEqual(sum(e["dispatch_id"] == worker for e in started), 1)

    def test_dispatch_limit_stop_reports_lower_bound_for_two_unaccepted_tasks(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 0
        engine = self.engine(ScriptedBackend())
        with self.assertRaises(HarnessError) as raised:
            engine.start("Implement greet twice", two_tasks())
        self.assertIn("至少还需 5 次", str(raised.exception))
        self.assertIn("下界", str(raised.exception))
        saved = read_json(engine.directory / "state.json")
        self.assertIn("至少还需 5 次", saved["error"])
        summary = status_summary(saved)
        self.assertEqual(summary["remaining_model_calls"]["lower_bound"], 5)
        self.assertEqual(summary["remaining_model_calls"]["tasks"], {"greet": 2, "polish": 2})
        self.assertIn("至少还需 5 次", summary["error"])
        self.assertEqual(saved["config_snapshot"]["runtime"]["max_agent_dispatches"], 0)

    def test_dispatch_limit_stop_with_candidate_awaiting_review_reports_two(self):
        # Worker returns, local verification passes, then the task review exceeds the limit.
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError) as raised:
            engine.start("Implement greet", self.plan())
        self.assertIn("至少还需 2 次", str(raised.exception))
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["pending_task"]["stage"], "reviewing")
        self.assertEqual([call["role"] for call in backend.calls], ["worker"])
        bound = status_summary(saved)["remaining_model_calls"]
        self.assertEqual((bound["lower_bound"], bound["tasks"], bound["final_review"]), (2, {"greet": 1}, 1))
        # Raising only the dispatch limit finishes in exactly the reported calls.
        self.config.data["runtime"]["max_agent_dispatches"] = 3
        resumed = self.engine(backend).resume(saved["id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(len(backend.calls) - 1, 2)


class LowerBoundStageTests(unittest.TestCase):
    def run_state(self, **fields):
        base = {"status": "blocked", "tasks": [{"id": "a"}, {"id": "b"}, {"id": "c"}], "accepted": {},
                "review_receipts": {}, "worker_receipts": {}, "final_history": [], "expected_head": "h1"}
        base.update(fields)
        return base

    def test_each_recorded_stage_counts_only_calls_it_still_needs(self):
        cases = [
            ({}, {"a": 2, "b": 2, "c": 2}, 1),
            ({"accepted": {"a": {}}, "pending_acceptance": {"task_id": "b"}}, {"b": 0, "c": 2}, 1),
            ({"pending_task": {"task_id": "a", "stage": "verifying"}}, {"a": 1, "b": 2, "c": 2}, 1),
            ({"worker_receipts": {"worker:implement:a": {}}}, {"a": 1, "b": 2, "c": 2}, 1),
            ({"pending_task": {"task_id": "a", "stage": "reviewing"},
              "review_receipts": {"reviewer:review:a": {}}}, {"a": 0, "b": 2, "c": 2}, 1),
            ({"accepted": {"a": {}, "b": {}, "c": {}}}, {}, 1),
            ({"accepted": {"a": {}, "b": {}, "c": {}},
              "final_history": [{"status": "approved", "head": "old"}]}, {}, 1),
            ({"accepted": {"a": {}, "b": {}, "c": {}},
              "final_history": [{"status": "rejected", "head": "h1"}, {"status": "approved", "head": "h1"}]}, {}, 0),
            ({"accepted": {"a": {}, "b": {}, "c": {}},
              "review_receipts": {"reviewer:final_review:final": {}}}, {}, 0),
            ({"accepted": {"a": {}, "b": {}},
              "final_history": [{"status": "approved", "head": "h1"}]}, {"c": 2}, 1),
        ]
        for fields, tasks, final in cases:
            with self.subTest(fields=fields):
                bound = remaining_model_calls(self.run_state(**fields))
                self.assertEqual(bound["tasks"], tasks)
                self.assertEqual(bound["final_review"], final)
                self.assertEqual(bound["lower_bound"], sum(tasks.values()) + final)
                self.assertIn(f"至少还需 {bound['lower_bound']} 次", bound["message"])

    def test_terminal_and_damaged_ledgers_never_raise(self):
        self.assertIsNone(remaining_model_calls({"status": "completed"}))
        self.assertIsNone(remaining_model_calls({"status": "abandoned"}))
        damaged = remaining_model_calls({"status": "blocked", "tasks": "x", "accepted": [], "pending_task": 3,
                                         "review_receipts": None, "final_history": {}})
        self.assertEqual(damaged["lower_bound"], 1)


class HistoricalEventObservationTests(StateCase):
    def test_v15_dispatch_started_without_invoked_projects_from_ledger_only(self):
        prepared = dispatch(role="reviewer", kind="review", invoked=False, process_state="prepared",
                            status="failed")
        invoked = dispatch(provider_usage={"provider": "claude", "reported_cost_usd": 0.5})
        run_id = self.save(ledger("run_hist", schema_version=15, status="blocked",
                                  dispatches=[invoked, prepared], agent_dispatches=1))
        usage = {"provider": "claude", "reported_cost_usd": 9.0}
        lines = [{"time": 1.0, "event": "dispatch_started", "run_id": run_id, "dispatch_id": invoked["id"]},
                 {"time": 2.0, "event": "dispatch_finished", "run_id": run_id, "dispatch_id": invoked["id"],
                  "status": "completed", "provider_usage": usage},
                 {"time": 3.0, "event": "dispatch_finished", "run_id": run_id, "dispatch_id": invoked["id"],
                  "status": "completed", "provider_usage": usage},
                 {"time": 4.0, "event": "dispatch_started", "run_id": run_id, "dispatch_id": prepared["id"]},
                 {"time": 5.0, "event": "dispatch_finished", "run_id": run_id, "dispatch_id": prepared["id"],
                  "status": "failed"}]
        (self.state / "runs" / run_id / "events.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
        detail = observe.run_detail(self.state, run_id)
        self.assertIsNone(detail["summary_error"])
        rows = {row["id"]: row for row in detail["dispatches"]}
        self.assertEqual(rows[invoked["id"]]["finished_at"], 2.0)
        self.assertEqual(rows[prepared["id"]]["invocation_class"], "known_not_invoked")
        # Cost and invocation counts come only from the two ledger rows.
        self.assertEqual(detail["cost"]["counted_dispatches"], 2)
        self.assertEqual(detail["cost"]["by_provider"]["claude"]["invoked"], 1)
        self.assertEqual(detail["cost"]["by_provider"]["claude"]["reported_cost_usd"], 0.5)
        self.assertEqual(detail["cost"]["known_not_invoked"], 1)
        self.assertIn("dispatch_prepared", detail["notes"]["events"])
        output = io.StringIO()
        with patch("harness.watch_cli.RunWatcher") as watcher:
            watcher.return_value.__enter__.return_value.backend = "fixture"
            self.assertEqual(watch_run(self.state, run_id, stdout=output), 0)
        snapshot, = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(snapshot["status"], "blocked")
        self.assertEqual(snapshot["active_dispatch_count"], 0)


if __name__ == "__main__":
    unittest.main()
