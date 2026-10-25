import copy
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class CommanderReceiptTests(RepoCase):
    def plan(self, multiple=False):
        plan = read_json(ROOT / "examples" / "plan.json")
        if multiple:
            plan["tasks"].append({**copy.deepcopy(plan["tasks"][0]), "id": "other"})
        return plan

    @staticmethod
    def block_worker(backend, kwargs, packet, result):
        if kwargs["role"] == "worker":
            result.update(status="blocked", blocker_kind="task", blockers=["Plan a prerequisite"])

    def interrupt_result(self, engine, kind, plan=None, **start_kwargs):
        interrupted = False

        def interrupt(path, value):
            nonlocal interrupted
            write_json(path, value)
            if (not interrupted and Path(path).name == "returned-result.json"
                    and engine.run["dispatches"][-1]["kind"] == kind):
                interrupted = True
                raise KeyboardInterrupt()

        with patch("harness.dispatch.write_json", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan, **start_kwargs)
        self.assertTrue(interrupted)
        return read_json(engine.directory / "state.json")

    def fail_post_agent(self, engine, kind, plan=None, **start_kwargs):
        emit = engine.hooks.emit

        def fail(event, payload, logs):
            if event == "post_agent" and payload.get("kind") == kind:
                Path(logs).mkdir(parents=True, exist_ok=True)
                (Path(logs) / "attempt.txt").write_text("failed gate")
                raise HarnessError("policy gate unavailable")
            return emit(event, payload, logs)

        with patch.object(engine.hooks, "emit", side_effect=fail):
            with self.assertRaisesRegex(HarnessError, "policy gate unavailable"):
                engine.start("Implement greet", plan, **start_kwargs)
        return read_json(engine.directory / "state.json")

    def test_plan_atomic_result_write_recovers_original_plan_with_no_budget_left(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        saved = self.interrupt_result(engine, "plan", pause_after_plan=True)
        receipt = saved["commander_receipts"]["commander:plan:planning"]
        self.assertNotIn("artifact_digest", receipt)
        calls = copy.deepcopy(backend.calls)
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(result["agent_dispatches"], 1)
        self.assertEqual(backend.calls, calls)
        self.assertEqual(result["commander_receipts"], {})
        self.assertEqual(result["tasks"], read_json(Path(receipt["artifact"]))["tasks"])

    def test_stop_atomic_result_write_and_repeated_resume_never_reroll(self):
        backend = ScriptedBackend(self.block_worker)
        engine = self.engine(backend)
        saved = self.interrupt_result(engine, "replan", self.plan())
        calls = copy.deepcopy(backend.calls)
        for _ in range(2):
            resumed = self.engine(backend)
            with self.assertRaisesRegex(HarnessError, "停止重新规划"):
                resumed.resume(saved["id"])
            self.assertEqual(backend.calls, calls)
            self.assertEqual(resumed.run["replan_attempts"], 1)
            self.assertEqual(resumed.run["total_attempts"], 1)
        self.assertEqual(resumed.run["commander_receipts"], {})

    def test_plan_post_agent_recovery_keeps_original_result_and_separate_logs(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        saved = self.fail_post_agent(engine, "plan", pause_after_plan=True)
        receipt = saved["commander_receipts"]["commander:plan:planning"]
        first_log = Path(receipt["artifact"]).parent / "post-agent-1" / "attempt.txt"
        resumed = self.engine(backend)
        with patch.object(resumed.hooks, "emit", wraps=resumed.hooks.emit) as hooks:
            result = resumed.resume(saved["id"])
        retry = next(call for call in hooks.call_args_list if call.args[0] == "post_agent")
        self.assertEqual(Path(retry.args[2]).name, "post-agent-2")
        self.assertEqual(first_log.read_text(), "failed gate")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(result["status"], "awaiting_approval")

    def test_stop_post_agent_failure_recovers_stop_before_explicit_fresh_request(self):
        backend = ScriptedBackend(self.block_worker)
        engine = self.engine(backend)
        saved = self.fail_post_agent(engine, "replan", self.plan())
        receipt = saved["commander_receipts"]["commander:replan:replanning"]
        original = Path(receipt["artifact"]).read_bytes()
        calls = copy.deepcopy(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            resumed.resume(saved["id"])
        self.assertEqual(backend.calls, calls)
        self.assertEqual(resumed.run["replan_attempts"], 1)
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            self.engine(backend).resume(saved["id"], retry_stopped="greet")
        final = read_json(engine.directory / "state.json")
        replans = [d for d in final["dispatches"] if d["kind"] == "replan"]
        self.assertEqual(len(replans), 2)
        self.assertEqual(len({d["id"] for d in replans}), 2)
        self.assertEqual(final["replan_attempts"], 2)
        self.assertEqual(Path(receipt["artifact"]).read_bytes(), original)

    def test_selection_result_recovers_and_persists_until_worker_invocation(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        saved = self.interrupt_result(engine, "dispatch", self.plan(multiple=True))
        resumed = self.engine(backend)

        def deny_worker(event, payload, logs):
            if event == "pre_agent" and payload.get("role") == "worker":
                raise HarnessError("worker preparation unavailable")

        with patch.object(resumed.hooks, "emit", side_effect=deny_worker):
            with self.assertRaisesRegex(HarnessError, "worker preparation unavailable"):
                resumed.resume(saved["id"])
        self.assertEqual(resumed.run["commander_receipts"], {})
        self.assertIn("pending_directive", resumed.run)
        self.assertEqual(resumed.run["attempts"], {})
        result = self.engine(backend).resume(saved["id"], step=True)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(sum(c["kind"] == "dispatch" for c in backend.calls), 1)
        self.assertNotIn("pending_directive", result)

    def test_returned_hard_error_is_rechecked_and_dispatch_error_updated(self):
        def invalid(backend, kwargs, packet, result):
            if kwargs["kind"] == "plan":
                result["dispatch_id"] = "unrelated-dispatch"

        backend = ScriptedBackend(invalid)
        engine = self.engine(backend)
        saved = self.interrupt_result(engine, "plan")
        calls = copy.deepcopy(backend.calls)
        for _ in range(2):
            resumed = self.engine(backend)
            with self.assertRaises(HarnessError):
                resumed.resume(saved["id"])
            self.assertEqual(backend.calls, calls)
            self.assertEqual(resumed.run["dispatches"][-1]["status"], "failed")
            self.assertIn("dispatch_id", resumed.run["dispatches"][-1]["error"])
            self.assertEqual(resumed.run["tasks"], [])

    def test_digest_bound_result_cannot_be_modified_or_deleted_for_new_plan(self):
        for mode in ("changed", "missing"):
            with self.subTest(mode=mode):
                backend = ScriptedBackend()
                engine = self.engine(backend)
                saved = self.fail_post_agent(engine, "plan", pause_after_plan=True)
                artifact = Path(saved["commander_receipts"]["commander:plan:planning"]["artifact"])
                if mode == "missing":
                    artifact.unlink()
                else:
                    value = read_json(artifact)
                    value["tasks"][0]["description"] = "A different plan"
                    write_json(artifact, value)
                calls = copy.deepcopy(backend.calls)
                with self.assertRaisesRegex(HarnessError, "指挥原始结果"):
                    self.engine(backend).resume(saved["id"])
                self.assertEqual(backend.calls, calls)

    def test_replan_format_correction_result_is_recovered_without_third_dispatch(self):
        def malformed_first(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                self.block_worker(backend, kwargs, packet, result)
            if kwargs["kind"] == "replan":
                result.update(action="revise", reason="x" * 4001 if "format_correction" not in packet else "Clarify prerequisite",
                    new_tasks=[], updates=[{"id": "greet", "description": "Implement the clarified greeting", "add_depends_on": []}])

        backend = ScriptedBackend(malformed_first)
        engine = self.engine(backend)
        emit = engine.hooks.emit

        def fail_correction(event, payload, logs):
            if event == "post_agent" and payload.get("kind") == "replan":
                raise HarnessError("correction hook unavailable")
            return emit(event, payload, logs)

        with patch.object(engine.hooks, "emit", side_effect=fail_correction):
            with self.assertRaisesRegex(HarnessError, "correction hook unavailable"):
                engine.start("Implement greet", self.plan())
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(c["kind"] == "replan" for c in backend.calls), 2)
        self.assertEqual(result["replan_attempts"], 1)
        self.assertEqual(result["commander_receipts"], {})

    def test_second_invalid_selection_is_terminal_after_one_correction(self):
        def malformed(backend, kwargs, packet, result):
            if kwargs["kind"] == "dispatch":
                result["instruction"] = " "

        backend = ScriptedBackend(malformed)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan(multiple=True))
        calls = copy.deepcopy(backend.calls)
        self.assertEqual(len(calls), 2)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)
        self.assertEqual(engine.run["attempts"], {})

    def test_malformed_task_blocker_preserves_replan_route_and_error_evidence(self):
        def malformed(backend, kwargs, packet, result):
            self.block_worker(backend, kwargs, packet, result)
            if kwargs["role"] == "worker":
                result["summary"] = " "

        self.config.data["runtime"]["max_attempts"] = 1
        backend = ScriptedBackend(malformed)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "不自动重派"):
            engine.start("Implement greet", self.plan())
        trigger = engine.run["pending_replan"]["trigger"]
        self.assertIn("format_error", trigger)
        self.assertTrue(Path(trigger["worker_artifact"]).is_file())
        with self.assertRaisesRegex(HarnessError, "尝试次数上限"):
            self.engine(backend).resume(engine.run["id"])
        self.config.data["runtime"]["max_attempts"] = 2
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            resumed.resume(engine.run["id"])
        self.assertEqual([c["kind"] for c in backend.calls], ["implement", "replan"])
        self.assertEqual(resumed.run["replan_stopped"]["trigger"], trigger)
        self.assertEqual(resumed.run["total_attempts"], 1)

    def test_semantically_invalid_worker_is_not_routed_as_task_blocker(self):
        def invalid(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.update(status="completed", blocker_kind="task", blockers=["Inconsistent status"], summary=" ")

        engine = self.engine(ScriptedBackend(invalid))
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        self.assertNotIn("pending_replan", engine.run)

    def test_unreturned_plan_retry_uses_fresh_dispatch_and_cumulative_budget(self):
        def unavailable_once(backend, kwargs, packet, result):
            if len(backend.calls) == 1:
                raise HarnessError("commander service unavailable")

        self.config.data["runtime"]["max_agent_dispatches"] = 1
        backend = ScriptedBackend(unavailable_once)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "service unavailable"):
            engine.start("Implement greet", pause_after_plan=True)
        with self.assertRaisesRegex(HarnessError, "max_agent_dispatches"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), 1)
        self.config.data["runtime"]["max_agent_dispatches"] = 2
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(result["agent_dispatches"], 2)
        identities = [entry["id"] for entry in result["dispatches"]]
        self.assertEqual(len(identities), len(set(identities)))

    def test_bound_request_cannot_change_schema_before_replay(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        saved = self.fail_post_agent(engine, "plan", pause_after_plan=True)
        receipt = saved["commander_receipts"]["commander:plan:planning"]
        receipt["request"]["schema"]["additionalProperties"] = True
        write_json(engine.directory / "state.json", saved)
        with self.assertRaisesRegex(HarnessError, "指挥原始请求已改变"):
            self.engine(backend).resume(saved["id"])
        self.assertEqual(len(backend.calls), 1)

    def test_reverify_cannot_cancel_returned_commander_stop(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        verify = engine.verify
        emit = engine.hooks.emit

        def failed_final(*args, **kwargs):
            if kwargs.get("whole_run"):
                return [{"argv": ["integration-check"], "exit_code": 1, "stdout": "",
                         "stderr": "Integration failed", "log": "integration.log", "timed_out": False}]
            return verify(*args, **kwargs)

        def fail_replan(event, payload, logs):
            if event == "post_agent" and payload.get("kind") == "replan":
                raise HarnessError("stop hook unavailable")
            return emit(event, payload, logs)

        with patch.object(engine, "verify", side_effect=failed_final), patch.object(engine.hooks, "emit", side_effect=fail_replan):
            with self.assertRaisesRegex(HarnessError, "stop hook unavailable"):
                engine.start("Implement greet", self.plan())
        calls = copy.deepcopy(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            resumed.resume(engine.run["id"], reverify=True)
        self.assertEqual(backend.calls, calls)
        self.assertEqual(resumed.run["replan_stopped"]["target"], "final")

    def test_resumed_invalid_review_records_actual_dispatch_failure(self):
        def invalid(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                result["dispatch_id"] = "unrelated-dispatch"

        backend = ScriptedBackend(invalid)
        engine = self.engine(backend)
        saved = self.interrupt_result(engine, "review", self.plan())
        resumed = self.engine(backend)
        with self.assertRaises(HarnessError):
            resumed.resume(saved["id"])
        entry = resumed.run["dispatches"][-1]
        self.assertEqual(entry["role"], "reviewer")
        self.assertEqual(entry["status"], "failed")
        self.assertIn("dispatch_id", entry["error"])

    def test_stop_artifact_interruption_consumes_replan_budget_once(self):
        self.config.data["runtime"]["max_replans"] = 1
        backend = ScriptedBackend(self.block_worker)
        engine = self.engine(backend)
        writes = 0

        def interrupt_stop_artifact(path, value):
            nonlocal writes
            write_json(path, value)
            if Path(path).name == "replan-1.json":
                writes += 1
                if writes == 2:
                    raise KeyboardInterrupt()

        with patch("harness.planning.write_json", side_effect=interrupt_stop_artifact):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["replan_attempts"], 0)
        self.assertIn("commander:replan:replanning", saved["commander_receipts"])
        calls = copy.deepcopy(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            resumed.resume(saved["id"])
        self.assertEqual(resumed.run["replan_attempts"], 1)
        self.assertEqual(backend.calls, calls)
