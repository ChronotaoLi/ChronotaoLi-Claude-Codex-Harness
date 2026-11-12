"""After rejected work the commander decides in one call, without another worker invocation."""
import copy
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, decided, finding, revised, trigger_kind


class DispatchReplanTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def backend(self, *, revise=False, operator=False):
        def coordinate(backend, kwargs, packet, result):
            if operator and kwargs["role"] == "worker":
                result.update(status="blocked", blocker_kind="operator", blockers=["Continue within original acceptance?"])
            if kwargs["kind"] == "review" and sum(c["kind"] == "review" for c in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding(category="evidence")])
            if trigger_kind(kwargs, packet) == "task" and packet["trigger"]["feedback"] and not revise:
                decided(result, "ask", reason="Acceptance cannot be satisfied by reporting the unresolved failure.")
            elif trigger_kind(kwargs, packet) == "task" and packet["trigger"]["feedback"] and not packet.get("accepted"):
                prerequisite = copy.deepcopy(self.plan()["tasks"][0])
                prerequisite.update(id="prerequisite", title="Resolve prerequisite")
                revised(result, packet, "Resolve the missing prerequisite first", new_tasks=[prerequisite], selected="prerequisite",
                        updates=[{"id": "greet", "description": "Implement after prerequisite", "add_depends_on": ["prerequisite"]}])
        return ScriptedBackend(coordinate)

    def test_rejected_task_can_ask_without_another_worker_and_resume_cannot_reroll(self):
        backend = self.backend()
        engine = self.engine(backend)
        original = self.plan()
        with self.assertRaisesRegex(HarnessError, "等待操作者决定：task_id=greet"):
            engine.start("Implement greet", original)
        self.assertEqual(sum(engine.run["attempts"].values()), 1)
        self.assertEqual(engine.run["original_tasks"], original["tasks"])
        self.assertEqual(engine.run["tasks"], original["tasks"])
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(len(engine.run["review_history"]["greet"]), 1)
        self.assertTrue((engine.worktree / "hello.py").exists())
        self.assertEqual([c["kind"] for c in backend.calls], ["implement", "review", "decide"])
        calls = copy.deepcopy(backend.calls)
        for _ in range(2):
            with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
                self.engine(backend).resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)

    def test_continue_answer_does_not_force_an_infeasible_worker_or_change_acceptance(self):
        backend = self.backend(operator=True)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            engine.start("Implement greet", self.plan())
        request = engine.run["decision_requests"][0]
        answer = self.repo / "choice.json"
        write_json(answer, {"schema_version": 1, "request_id": request["id"], "task_id": "greet",
                            "decision": "continue", "summary": "Continue without changing acceptance"})
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            self.engine(backend).resume(engine.run["id"], operator_decision=answer)
        self.assertEqual(sum(c["role"] == "worker" for c in backend.calls), 1)
        self.assertEqual(engine.run["tasks"], self.plan()["tasks"])
        self.assertEqual(read_json(engine.directory / "state.json")["decision_requests"][-1]["kind"], "commander")

    def test_interruption_after_decide_result_reuses_same_receipt(self):
        backend = self.backend()
        engine = self.engine(backend)
        def interrupt(path, value):
            write_json(path, value)
            if Path(path).name == "returned-result.json" and value.get("action") == "ask":
                raise KeyboardInterrupt()
        with patch("harness.dispatch.write_json", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual([c["kind"] for c in backend.calls], ["implement", "review", "decide"])

    def test_revision_can_resolve_prerequisite_then_accept_original_task(self):
        backend = self.backend(revise=True)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(result["accepted"]), {"greet", "prerequisite"})
        self.assertEqual(sum(result["attempts"].values()), 3)
        self.assertEqual(result["tasks"][0]["acceptance"], self.plan()["tasks"][0]["acceptance"])
        self.assertEqual(result["final_history"][-1]["status"], "approved")

    def test_selection_without_action_is_invalid_not_an_implement_route(self):
        def omit_action(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and sum(c["kind"] == "review" for c in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding(category="evidence")])
            if kwargs["role"] == "commander":
                result.pop("action")
        backend = ScriptedBackend(omit_action)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "连续两次无效"):
            engine.start("Implement greet", self.plan())
        # One worker ran under the deterministic first directive; the selection
        # without an action was invalid, redone once, and never became a route.
        self.assertEqual([c["kind"] for c in backend.calls], ["implement", "review", "decide", "decide"])
        self.assertEqual(engine.run["attempts"], {"greet": 1})
        self.assertNotIn("pending_directive", engine.run)
        selections = [d for d in engine.run["dispatches"] if d.get("kind") == "decide"]
        self.assertTrue(all(d["status"] == "invalid" and "missing or unexpected fields" in d["error"]
                            for d in selections))
        saved = read_json(engine.directory / "state.json")
        self.assertNotIn("commander:decide:run", saved["commander_receipts"])
        with self.assertRaisesRegex(HarnessError, "连续两次无效"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual([c["kind"] for c in backend.calls][4:], ["decide", "decide"])
        self.assertEqual(read_json(engine.directory / "state.json")["attempts"], {"greet": 1})

    def test_deterministic_first_directive_routes_to_implement_without_commander(self):
        # A pristine single ready task needs no decision; the worker skill states
        # the default and the packet carries no commander text.
        backend = ScriptedBackend()
        result = self.engine(backend).start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertFalse(any(c["role"] == "commander" for c in backend.calls))
        worker = next(c for c in backend.calls if c["role"] == "worker")
        self.assertIsNone(worker["packet"]["instruction"])
