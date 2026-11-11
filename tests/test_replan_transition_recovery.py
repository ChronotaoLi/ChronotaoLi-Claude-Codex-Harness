"""A selection that routes to replanning keeps its recovery gates across stops and interruptions."""
import copy
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class ReplanTransitionRecoveryTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def saved(self, engine):
        return read_json(engine.directory / "state.json")

    def backend(self):
        """The worker output is invalid; the next selection routes to replanning."""
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["summary"] = "x" * 32001
            elif kwargs["kind"] == "dispatch":
                result.update(action="replan", instruction="Resolve the unmet requirement through planning")
        return ScriptedBackend(change)

    def stopped(self):
        backend = self.backend()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            engine.start("Implement greet", self.plan())
        state = self.saved(engine)
        self.assertIn("replan_stopped", state)
        self.assertEqual(state["decision_requests"][-1]["kind"], "replan_stop")
        self.assertEqual([c["kind"] for c in backend.calls], ["implement", "dispatch", "replan"])
        return engine, backend

    def answer(self, engine, decision="continue"):
        request = self.saved(engine)["decision_requests"][-1]
        path = self.repo / "answer.json"
        write_json(path, {"schema_version": 1, "request_id": request["id"], "task_id": "greet",
                          "decision": decision, "summary": "Explicitly " + decision})
        return path

    def assert_preserved(self, before, after, *, replan_delta=0, dispatch_delta=0):
        for field in ("attempts", "accepted", "tasks", "original_tasks", "review_history",
                      "final_attempts", "final_history", "worker_recovery"):
            self.assertEqual(after.get(field), before.get(field), field)
        self.assertEqual(after["replan_attempts"], before["replan_attempts"] + replan_delta)
        self.assertEqual(after["agent_dispatches"], before["agent_dispatches"] + dispatch_delta)
        self.assertEqual(after["dispatches"][:len(before["dispatches"])], before["dispatches"])

    def test_operator_stop_then_continue_reopens_planning_without_worker(self):
        engine, backend = self.stopped()
        before = self.saved(engine)
        calls = len(backend.calls)
        stop = self.answer(engine, "stop")
        for unused in range(2):
            with self.assertRaisesRegex(HarnessError, "操作者决定停止"):
                self.engine(backend).resume(engine.run["id"], operator_decision=stop)
            self.assertEqual(len(backend.calls), calls)
            self.assertEqual(len(self.saved(engine)["operator_decisions"]), 1)
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            self.engine(backend).resume(engine.run["id"], operator_decision=self.answer(engine))
        state = self.saved(engine)
        self.assertEqual([c["kind"] for c in backend.calls[calls:]], ["replan"])
        self.assertEqual([a["decision"] for a in state["operator_decisions"]], ["stop", "continue"])
        self.assert_preserved(before, state, replan_delta=1, dispatch_delta=1)
        self.assertEqual(state["replan_stopped"]["trigger"], before["replan_stopped"]["trigger"])

    def test_transition_save_is_atomic_across_interruptions(self):
        for persist in (False, True):
            with self.subTest(persist=persist):
                backend = self.backend()
                engine = self.engine(backend)
                save = engine.context.save
                interrupted = False

                def interrupt_transition():
                    nonlocal interrupted
                    pending = engine.run.get("pending_replan", {})
                    if not interrupted and pending.get("trigger", {}).get("coordination"):
                        interrupted = True
                        # The selection's successor and its receipt consumption are
                        # one durable transition.
                        self.assertNotIn("commander:dispatch:dispatching", engine.run["commander_receipts"])
                        if persist:
                            save()
                        raise KeyboardInterrupt()
                    save()

                # A process crash cannot run execute's stop/save handler. Keep
                # that handler from making the interrupted transition durable.
                with patch.object(engine.context, "save", side_effect=interrupt_transition), \
                        patch.object(engine.services.workflow, "stop"):
                    with self.assertRaises(KeyboardInterrupt):
                        engine.start("Implement greet", self.plan())
                self.assertTrue(interrupted)
                persisted = self.saved(engine)
                if persist:
                    self.assertIn("pending_replan", persisted)
                    self.assertNotIn("commander:dispatch:dispatching", persisted["commander_receipts"])
                else:
                    self.assertNotIn("pending_replan", persisted)
                    self.assertIn("commander:dispatch:dispatching", persisted["commander_receipts"])
                with self.assertRaisesRegex(HarnessError, "停止重新规划"):
                    self.engine(backend).resume(engine.run["id"])
                self.assertEqual([c["kind"] for c in backend.calls], ["implement", "dispatch", "replan"])
