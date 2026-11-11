"""A coordinator can escalate rejected work without another worker invocation."""
import copy
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, finding


class DispatchReplanTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def backend(self, *, revise=False, operator=False):
        def coordinate(backend, kwargs, packet, result):
            if operator and kwargs["role"] == "worker":
                result.update(status="blocked", blocker_kind="operator", blockers=["Continue within original acceptance?"])
            if kwargs["kind"] == "review" and sum(c["kind"] == "review" for c in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding(category="evidence")])
            if kwargs["kind"] == "dispatch" and not any(c["kind"] == "replan" for c in backend.calls):
                result.update(action="replan", instruction="Acceptance cannot be satisfied by reporting the unresolved failure.")
            if revise and kwargs["kind"] == "replan":
                prerequisite = copy.deepcopy(self.plan()["tasks"][0])
                prerequisite.update(id="prerequisite", title="Resolve prerequisite")
                result.update(action="revise", reason="Resolve the missing prerequisite first", new_tasks=[prerequisite],
                              updates=[{"id": "greet", "description": "Implement after prerequisite", "add_depends_on": ["prerequisite"]}])
        return ScriptedBackend(coordinate)

    def test_rejected_task_can_stop_without_another_worker_and_resume_cannot_reroll(self):
        backend = self.backend()
        engine = self.engine(backend)
        original = self.plan()
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            engine.start("Implement greet", original)
        self.assertEqual(sum(engine.run["attempts"].values()), 1)
        self.assertEqual(engine.run["original_tasks"], original["tasks"])
        self.assertEqual(engine.run["tasks"], original["tasks"])
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(len(engine.run["review_history"]["greet"]), 1)
        self.assertTrue((engine.worktree / "hello.py").exists())
        calls = copy.deepcopy(backend.calls)
        for _ in range(2):
            with self.assertRaisesRegex(HarnessError, "停止重新规划"):
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
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            self.engine(backend).resume(engine.run["id"], operator_decision=answer)
        self.assertEqual(sum(c["role"] == "worker" for c in backend.calls), 1)

    def test_replan_budget_recovery_keeps_selection_and_candidate(self):
        self.config.data["runtime"]["max_replans"] = 0
        backend = self.backend()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "重新规划次数上限"):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertIn("coordination", saved["pending_replan"]["trigger"])
        self.assertNotIn("pending_directive", saved)
        self.assertNotIn("commander:dispatch:dispatching", saved["commander_receipts"])
        self.config.data["runtime"]["max_replans"] = 1
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual([c["kind"] for c in backend.calls], ["implement", "review", "dispatch", "replan"])

    def test_changed_selection_evidence_stops_before_replan(self):
        self.config.data["runtime"]["max_replans"] = 0
        backend = self.backend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        source = engine.run["pending_replan"]["trigger"]["coordination"]
        artifact = Path(source["artifact"])
        artifact.write_bytes(artifact.read_bytes() + b"\n")
        self.config.data["runtime"]["max_replans"] = 1
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "原始结果已改变"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), calls)

    def test_interruption_after_selection_result_reuses_same_receipt(self):
        backend = self.backend()
        engine = self.engine(backend)
        def interrupt(path, value):
            write_json(path, value)
            if Path(path).name == "returned-result.json" and value.get("action") == "replan":
                raise KeyboardInterrupt()
        with patch("harness.dispatch.write_json", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual([c["kind"] for c in backend.calls], ["implement", "review", "dispatch", "replan"])

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
            if kwargs["kind"] == "dispatch":
                result.pop("action")
        backend = ScriptedBackend(omit_action)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "连续两次无效"):
            engine.start("Implement greet", self.plan())
        # One worker ran under the deterministic first directive; the selection
        # without an action was invalid, redone once, and never became a route.
        self.assertEqual([c["kind"] for c in backend.calls], ["implement", "review", "dispatch", "dispatch"])
        self.assertEqual(engine.run["attempts"], {"greet": 1})
        self.assertNotIn("pending_directive", engine.run)
        selections = [d for d in engine.run["dispatches"] if d.get("kind") == "dispatch"]
        self.assertTrue(all(d["status"] == "invalid" and "missing or unexpected fields" in d["error"]
                            for d in selections))
        saved = read_json(engine.directory / "state.json")
        self.assertNotIn("commander:dispatch:dispatching", saved["commander_receipts"])
        with self.assertRaisesRegex(HarnessError, "连续两次无效"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual([c["kind"] for c in backend.calls][4:], ["dispatch", "dispatch"])
        self.assertEqual(read_json(engine.directory / "state.json")["attempts"], {"greet": 1})

    def test_deterministic_first_directive_routes_to_implement_without_commander(self):
        # The pristine directive names action=implement itself; routing reads it
        # with no default, so a missing action could not reach the worker.
        backend = ScriptedBackend()
        result = self.engine(backend).start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertFalse(any(c["kind"] == "dispatch" for c in backend.calls))
        worker = next(c for c in backend.calls if c["role"] == "worker")
        self.assertTrue(worker["packet"]["instruction"].startswith("Implement this task"))
        from harness.contracts import task_directive
        schema = task_directive(["greet"])
        self.assertIn("action", schema["required"])
        self.assertEqual(schema["properties"]["action"]["enum"], ["implement", "replan"])
