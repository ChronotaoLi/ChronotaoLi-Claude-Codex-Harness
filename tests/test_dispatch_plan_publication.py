"""Faults at dispatch and plan publication boundaries leave recoverable ledgers."""

from contextlib import contextmanager
import json
from unittest.mock import patch

from harness import stops
from harness.planning import DECIDE_KEY
from harness.util import HarnessError, read_json
import test_dispatch_replan
from test_engine_repairs import RecordingBackend
from test_harness import ROOT, RepoCase


class DispatchPlanPublicationTests(RepoCase):
    def exercise_failed_publication(self, engine, selected, start):
        publish = engine.context.publish
        durable = {}
        injected = HarnessError("injected publication body error",
                                stop=stops.make("runtime_failure", "invariant"))

        @contextmanager
        def fail_before_commit(**options):
            before = read_json(engine.directory / "state.json")
            with publish(**options):
                yield
                if selected(before, engine.run):
                    durable.update(before)
                    self.assertEqual(json.loads((engine.directory / "state.json").read_bytes()), before)
                    raise injected

        with patch.object(engine.context, "publish", fail_before_commit), self.assertRaises(HarnessError) as caught:
            start()
        error = caught.exception
        self.assertIs(error if error is injected else error.__context__, injected)
        self.assertTrue(durable)
        saved = read_json(engine.directory / "state.json")
        self.assertEqual((saved["stop"]["kind"], saved["stop"]["reason"]), ("runtime_failure", "invariant"))
        loaded = self.engine()
        loaded.services.lifecycle.load_run(saved["id"])
        self.assertEqual(loaded.run, saved)
        return durable, saved

    def test_dispatch_creation_does_not_register_partial_entry(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        plan = read_json(ROOT / "examples" / "plan.json")
        before, stopped = self.exercise_failed_publication(
            engine, lambda old, run: len(run["dispatches"]) > len(old["dispatches"]),
            lambda: engine.start("Implement greet", plan))
        self.assertEqual(backend.calls, [])
        self.assertEqual(stopped["dispatches"], before["dispatches"])
        self.assertEqual(stopped["agent_dispatches"], 0)
        unused, = (engine.directory / "dispatches").iterdir()
        self.assertTrue((unused / "commands").is_dir())

        completed = self.engine(backend).resume(stopped["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["attempts"], {"greet": 1})
        self.assertNotIn(unused.name, {entry["id"] for entry in completed["dispatches"]})

    def test_dispatch_accounting_body_error_cannot_be_saved_by_dispatch_cleanup(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        plan = read_json(ROOT / "examples" / "plan.json")
        dispatch = engine.services.dispatch
        with patch.object(dispatch, "judge_worker_failure", wraps=dispatch.judge_worker_failure) as judge:
            before, stopped = self.exercise_failed_publication(
                engine, lambda old, run: run["agent_dispatches"] > old["agent_dispatches"],
                lambda: engine.start("Implement greet", plan))
        judge.assert_not_called()
        self.assertEqual(backend.calls, [])
        for key in ("agent_dispatches", "attempts", "worker_receipts", "pending_directive"):
            self.assertEqual(stopped.get(key), before.get(key), key)
        prepared, = stopped["dispatches"]
        self.assertFalse(prepared["invoked"])
        self.assertEqual(prepared["process_state"], "prepared")
        self.assertNotIn("worker_receipt", prepared)
        self.assertNotIn("snapshot_digest", prepared)

        completed = self.engine(backend).resume(stopped["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["attempts"], {"greet": 1})
        workers = [entry for entry in completed["dispatches"] if entry["role"] == "worker" and entry["invoked"]]
        self.assertEqual(len(workers), 1)
        self.assertNotEqual(workers[0]["id"], prepared["id"])

    def test_initial_plan_retry_reuses_original_commander_result_and_plan_file(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        before, stopped = self.exercise_failed_publication(
            engine, lambda old, run: not old["plan_history"] and bool(run["plan_history"]),
            lambda: engine.start("Implement greet"))
        self.assertEqual(stopped["commander_receipts"], before["commander_receipts"])
        self.assertIn(DECIDE_KEY, stopped["commander_receipts"])
        self.assertEqual(stopped["plan_history"], [])
        self.assertEqual(stopped["original_tasks"], [])
        self.assertEqual(stopped["tasks"], [])
        plan = engine.directory / "plans" / "revision-0.json"
        identity = plan.stat().st_ino, plan.read_bytes()

        completed = self.engine(backend).resume(stopped["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(sum(call["role"] == "commander" for call in backend.calls), 1)
        self.assertEqual((plan.stat().st_ino, plan.read_bytes()), identity)
        self.assertEqual(completed["original_tasks"], completed["tasks"])

    def check_revision_retry(self, *, applying):
        backend = test_dispatch_replan.DispatchReplanTests.backend(self, revise=True)
        engine = self.engine(backend)
        before, stopped = self.exercise_failed_publication(
            engine,
            lambda old, run: (
                old.get("pending_revision") and len(run["plan_history"]) > len(old["plan_history"])
                if applying else run.get("pending_revision") and not old.get("pending_revision")
            ),
            lambda: engine.start("Implement greet", self.plan()))
        self.assertEqual([call["kind"] for call in backend.calls], ["implement", "review", "decide"])
        for key in ("plan_history", "tasks", "current_task", "pending_directive", "feedback",
                    "worker_recovery", "pending_revision", "commander_receipts"):
            self.assertEqual(stopped.get(key), before.get(key), key)
        self.assertEqual(DECIDE_KEY in stopped["commander_receipts"], not applying)
        self.assertEqual("pending_revision" in stopped, applying)
        plan = engine.directory / "plans" / "revision-1.json"
        identity = plan.stat().st_ino, plan.read_bytes()
        archives = engine.directory / "recovery"

        revision_source = read_json(plan)["source"]["dispatch_id"]
        self.assertEqual(revision_source, stopped["dispatches"][-1]["id"])
        completed = self.engine(backend).resume(stopped["id"])
        self.assertEqual(completed["status"], "completed")
        # The revised prerequisite runs first. After accepting it, the
        # scheduler requests a new decision for the original task.
        self.assertEqual([call["kind"] for call in backend.calls[3:]],
                         ["implement", "review", "decide", "implement", "review", "final_review"])
        self.assertEqual(set(completed["accepted"]), {"greet", "prerequisite"})
        self.assertEqual(len(completed["plan_history"]), 2)
        self.assertNotIn("pending_revision", completed)
        self.assertEqual((plan.stat().st_ino, plan.read_bytes()), identity)
        self.assertEqual([path.name for path in archives.glob("revision-1*")], ["revision-1"])

    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def test_revision_landing_failure_reuses_original_commander_result(self):
        self.check_revision_retry(applying=False)

    def test_revision_application_failure_reuses_pending_revision_without_duplicate_archive(self):
        self.check_revision_retry(applying=True)
