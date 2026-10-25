"""Waits preserve task work budgets, never erase actual invocation accounting."""
import copy
from pathlib import Path
from unittest.mock import patch

from harness.budgets import work_attempts
from harness.cli import status_summary
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, finding


class WorkAttemptBudgetTests(RepoCase):
    def start_blocked(self, kind="environment", *, malformed=False):
        self.config.data["runtime"]["max_attempts"] = 1

        def block_first(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind=kind, blockers=["Wait for prerequisite"])
                if malformed:
                    result["summary"] = "x" * 4001

        backend = ScriptedBackend(block_first)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        return engine, backend

    def test_environment_wait_preserves_work_budget_and_every_invocation(self):
        engine, backend = self.start_blocked()
        before = read_json(engine.directory / "state.json")
        self.assertEqual(before["attempts"], {"greet": 1})
        self.assertEqual(work_attempts(before), {"greet": 0})
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(result["total_attempts"], 2)
        self.assertEqual(work_attempts(result), {"greet": 1})
        self.assertEqual(result["agent_dispatches"], len(backend.calls))
        workers = [d for d in result["dispatches"] if d["role"] == "worker"]
        self.assertEqual(len({d["id"] for d in workers}), 2)
        self.assertTrue(any(c["kind"] == "review" for c in backend.calls))
        self.assertTrue(any(c["kind"] == "final_review" for c in backend.calls))
        self.assertEqual(status_summary(result)["budgets"]["work_attempts"], {"greet": 1})

    def test_operator_wait_still_requires_an_explicit_decision(self):
        engine, backend = self.start_blocked("operator")
        before = read_json(engine.directory / "state.json")
        self.assertEqual(work_attempts(before), {"greet": 0})
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), 1)
        request = before["decision_requests"][0]
        answer = self.repo / "decision.json"
        write_json(answer, {"schema_version": 1, "request_id": request["id"], "task_id": "greet",
                            "decision": "continue", "summary": "Continue with the original task"})
        result = self.engine(backend).resume(engine.run["id"], operator_decision=answer)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_attempts"], 2)
        self.assertEqual(work_attempts(result), {"greet": 1})

    def test_wait_does_not_exempt_global_invocation_limit(self):
        self.config.data["runtime"]["max_total_attempts"] = 1
        engine, backend = self.start_blocked()
        with self.assertRaisesRegex(HarnessError, "run 总尝试次数上限"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), 1)
        self.config.data["runtime"]["max_total_attempts"] = 2
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")

    def test_real_rework_still_exhausts_task_budget_after_environment_wait(self):
        engine, backend = self.start_blocked()

        def reject(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                result.update(verdict="request_changes", findings=[finding()])

        backend.change = reject
        with self.assertRaisesRegex(HarnessError, "尝试次数上限"):
            self.engine(backend).resume(engine.run["id"])
        state = read_json(engine.directory / "state.json")
        self.assertEqual(state["attempts"], {"greet": 2})
        self.assertEqual(work_attempts(state), {"greet": 1})
        self.assertEqual(state["accepted"], {})
        self.assertEqual(len(state["review_history"]["greet"]), 1)

    def test_malformed_blocker_is_not_a_budget_exemption(self):
        engine, backend = self.start_blocked(malformed=True)
        self.assertEqual(work_attempts(engine.run), {"greet": 1})
        with self.assertRaisesRegex(HarnessError, "尝试次数上限"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), 1)

    def test_legacy_run_keeps_original_budget_policy(self):
        engine, backend = self.start_blocked()
        state = read_json(engine.directory / "state.json")
        state.pop("attempt_budget_policy")
        for entry in state["dispatches"]:
            entry.pop("attempt_exemption", None)
        write_json(engine.directory / "state.json", state)
        with self.assertRaisesRegex(HarnessError, "尝试次数上限"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(work_attempts(state), state["attempts"])

    def test_exemption_evidence_tampering_and_invalid_accounting_stop_before_dispatch(self):
        engine, backend = self.start_blocked()
        original = read_json(engine.directory / "state.json")
        for change in ("digest", "identity", "count", "policy", "task_type"):
            state = copy.deepcopy(original)
            exemption = state["dispatches"][0]["attempt_exemption"]
            if change == "digest":
                exemption["artifact_digest"] = "0" * 64
            elif change == "identity":
                exemption["blocker_kind"] = "operator"
            elif change == "count":
                state["attempts"]["greet"] = 0
            elif change == "policy":
                state["attempt_budget_policy"] = "invalid"
            else:
                state["dispatches"][0]["task_id"] = []
            write_json(engine.directory / "state.json", state)
            with self.subTest(change=change), self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run["id"])
            self.assertEqual(len(backend.calls), 1)
        write_json(engine.directory / "state.json", original)
        artifact = Path(original["dispatches"][0]["attempt_exemption"]["artifact"])
        artifact.write_bytes(artifact.read_bytes() + b"\n")
        with self.assertRaisesRegex(HarnessError, "原始结果已改变"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), 1)

    def test_pre_agent_tampering_cannot_spend_budget_using_an_old_exemption(self):
        engine, backend = self.start_blocked()
        before = read_json(engine.directory / "state.json")
        artifact = Path(before["dispatches"][0]["attempt_exemption"]["artifact"])
        resumed = self.engine(backend)

        def tamper(event, payload, logs):
            if event == "pre_agent":
                artifact.write_bytes(artifact.read_bytes() + b"\n")

        with patch.object(resumed.hooks, "emit", side_effect=tamper):
            with self.assertRaisesRegex(HarnessError, "原始结果已改变"):
                resumed.resume(engine.run["id"])
        self.assertEqual(len(backend.calls), 1)
        after = read_json(engine.directory / "state.json")
        for key in ("attempts", "total_attempts", "agent_dispatches"):
            self.assertEqual(after[key], before[key])
