"""After an invalid worker output the commander's question keeps its recovery gates across answers."""

from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, decided, trigger_kind


class ReplanTransitionRecoveryTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def saved(self, engine):
        return read_json(engine.directory / "state.json")

    def backend(self):
        """The worker output is invalid; the next decision asks the operator."""
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["summary"] = "x" * 32001
            elif trigger_kind(kwargs, packet) == "task":
                decided(result, "ask", reason="Resolve the unmet requirement with the operator")
        return ScriptedBackend(change)

    def stopped(self):
        backend = self.backend()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            engine.start("Implement greet", self.plan())
        state = self.saved(engine)
        self.assertEqual(state["decision_requests"][-1]["kind"], "commander")
        self.assertEqual([c["kind"] for c in backend.calls], ["implement", "decide"])
        return engine, backend

    def answer(self, engine, decision="continue"):
        request = self.saved(engine)["decision_requests"][-1]
        path = self.repo / "answer.json"
        write_json(path, {"schema_version": 1, "request_id": request["id"], "task_id": "greet",
                          "decision": decision, "summary": "Explicitly " + decision})
        return path

    def assert_preserved(self, before, after, *, dispatch_delta=0):
        for field in ("attempts", "accepted", "tasks", "original_tasks", "review_history",
                      "final_history", "worker_recovery", "plan_history"):
            self.assertEqual(after.get(field), before.get(field), field)
        self.assertEqual(after["agent_dispatches"], before["agent_dispatches"] + dispatch_delta)
        self.assertEqual(after["dispatches"][:len(before["dispatches"])], before["dispatches"])

    def test_operator_stop_then_continue_decides_again_without_worker(self):
        engine, backend = self.stopped()
        before = self.saved(engine)
        calls = len(backend.calls)
        stop = self.answer(engine, "stop")
        for unused in range(2):
            with self.assertRaisesRegex(HarnessError, "操作者决定停止"):
                self.engine(backend).resume(engine.run["id"], operator_decision=stop)
            self.assertEqual(len(backend.calls), calls)
            self.assertEqual(len(self.saved(engine)["operator_decisions"]), 1)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            self.engine(backend).resume(engine.run["id"], operator_decision=self.answer(engine))
        state = self.saved(engine)
        self.assertEqual([c["kind"] for c in backend.calls[calls:]], ["decide"])
        self.assertEqual(backend.calls[-1]["packet"]["operator_answer"]["decision"], "continue")
        self.assertEqual([a["decision"] for a in state["operator_decisions"]], ["stop", "continue"])
        self.assert_preserved(before, state, dispatch_delta=1)
