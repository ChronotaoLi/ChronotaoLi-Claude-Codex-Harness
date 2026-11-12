import copy
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, decided, trigger_kind


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
        saved = self.interrupt_result(engine, "decide", pause_after_plan=True)
        receipt = saved["commander_receipts"]["commander:decide:run"]
        self.assertNotIn("artifact_digest", receipt)
        calls = copy.deepcopy(backend.calls)
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(result["agent_dispatches"], 1)
        self.assertEqual(backend.calls, calls)
        self.assertEqual(result["commander_receipts"], {})
        self.assertEqual(result["tasks"], read_json(Path(receipt["artifact"]))["new_tasks"])

    def test_ask_atomic_result_write_and_repeated_resume_never_reroll(self):
        backend = ScriptedBackend(self.block_worker)
        engine = self.engine(backend)
        saved = self.interrupt_result(engine, "decide", self.plan())
        calls = copy.deepcopy(backend.calls)
        for _ in range(2):
            resumed = self.engine(backend)
            with self.assertRaisesRegex(HarnessError, "等待操作者决定：task_id=greet"):
                resumed.resume(saved["id"])
            self.assertEqual(backend.calls, calls)
            self.assertEqual(len(resumed.run["decision_requests"]), 1)
            self.assertEqual(sum(resumed.run["attempts"].values()), 1)
        self.assertEqual(resumed.run["commander_receipts"], {})

    def test_plan_post_agent_recovery_keeps_original_result_and_separate_logs(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        saved = self.fail_post_agent(engine, "decide", pause_after_plan=True)
        receipt = saved["commander_receipts"]["commander:decide:run"]
        first_log = Path(receipt["artifact"]).parent / "post-agent-1" / "attempt.txt"
        resumed = self.engine(backend)
        with patch.object(resumed.hooks, "emit", wraps=resumed.hooks.emit) as hooks:
            result = resumed.resume(saved["id"])
        retry = next(call for call in hooks.call_args_list if call.args[0] == "post_agent")
        self.assertEqual(Path(retry.args[2]).name, "post-agent-2")
        self.assertEqual(first_log.read_text(), "failed gate")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(result["status"], "awaiting_approval")

    def test_ask_post_agent_failure_recovers_question_before_explicit_fresh_request(self):
        backend = ScriptedBackend(self.block_worker)
        engine = self.engine(backend)
        saved = self.fail_post_agent(engine, "decide", self.plan())
        receipt = saved["commander_receipts"]["commander:decide:run"]
        original = Path(receipt["artifact"]).read_bytes()
        calls = copy.deepcopy(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            resumed.resume(saved["id"])
        self.assertEqual(backend.calls, calls)
        asked = read_json(engine.directory / "state.json")
        self.assertEqual(asked["agent_dispatches"], resumed.run["agent_dispatches"])
        self.assertEqual(len(asked["decision_requests"]), 1)
        answer = self.repo / "continue-decision.json"
        write_json(answer, {"schema_version": 1, "request_id": asked["decision_requests"][0]["id"],
                            "task_id": "greet", "decision": "continue", "summary": "Decide again with this answer"})
        # The demo commander asks again about the same blocker: a fresh request, not a replay.
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            self.engine(backend).resume(saved["id"], operator_decision=answer)
        final = read_json(engine.directory / "state.json")
        decisions = [d for d in final["dispatches"] if d["kind"] == "decide"]
        self.assertEqual(len(decisions), 2)
        self.assertEqual(len({d["id"] for d in decisions}), 2)
        self.assertEqual(len(final["decision_requests"]), 2)
        self.assertEqual(Path(receipt["artifact"]).read_bytes(), original)

    def test_selection_result_recovers_and_persists_until_worker_invocation(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        saved = self.interrupt_result(engine, "decide", self.plan(multiple=True))
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
        self.assertEqual(sum(c["kind"] == "decide" for c in backend.calls), 1)
        self.assertNotIn("pending_directive", result)

    def test_returned_identity_error_is_judged_invalid_and_redone_once(self):
        def invalid(backend, kwargs, packet, result):
            if trigger_kind(kwargs, packet) == "plan":
                result["dispatch_id"] = "unrelated-dispatch"

        backend = ScriptedBackend(invalid)
        engine = self.engine(backend)
        saved = self.interrupt_result(engine, "decide")
        calls = len(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "连续两次无效"):
            resumed.resume(saved["id"])
        # The returned original is judged without a new call; only its redo is new.
        self.assertEqual(len(backend.calls), calls + 1)
        first, second = [d for d in resumed.run["dispatches"] if d.get("kind") == "decide"]
        self.assertEqual((first["status"], second["status"]), ("invalid", "invalid"))
        self.assertEqual(second["retry_of"], first["id"])
        self.assertIn("dispatch_id", first["error"])
        self.assertEqual(resumed.run["tasks"], [])

    def test_digest_bound_result_cannot_be_modified_or_deleted_for_new_plan(self):
        for mode in ("changed", "missing"):
            with self.subTest(mode=mode):
                backend = ScriptedBackend()
                engine = self.engine(backend)
                saved = self.fail_post_agent(engine, "decide", pause_after_plan=True)
                artifact = Path(saved["commander_receipts"]["commander:decide:run"]["artifact"])
                if mode == "missing":
                    artifact.unlink()
                else:
                    value = read_json(artifact)
                    value["new_tasks"][0]["description"] = "A different plan"
                    write_json(artifact, value)
                calls = copy.deepcopy(backend.calls)
                with self.assertRaisesRegex(HarnessError, "指挥原始结果"):
                    self.engine(backend).resume(saved["id"])
                self.assertEqual(backend.calls, calls)

    def test_second_invalid_selection_stops_and_resume_is_a_new_request(self):
        def malformed(backend, kwargs, packet, result):
            if kwargs["role"] == "commander":
                result["instruction"] = " "

        backend = ScriptedBackend(malformed)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "连续两次无效"):
            engine.start("Implement greet", self.plan(multiple=True))
        self.assertEqual(len(backend.calls), 2)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "连续两次无效"):
            resumed.resume(engine.run["id"])
        selections = [d for d in resumed.run["dispatches"] if d.get("kind") == "decide"]
        self.assertEqual([d["status"] for d in selections], ["invalid"] * 4)
        self.assertEqual([d.get("retry_of") for d in selections],
                         [None, selections[0]["id"], None, selections[2]["id"]])
        self.assertEqual(resumed.run["attempts"], {})

    def test_malformed_task_blocker_reaches_the_commander_with_error_evidence(self):
        def malformed(backend, kwargs, packet, result):
            self.block_worker(backend, kwargs, packet, result)
            if kwargs["role"] == "worker":
                result["summary"] = " "
            if trigger_kind(kwargs, packet) == "task" and "output_error" in (packet["trigger"]["feedback"] or {}):
                decided(result, "ask", reason="The blocker could not be read; how should greet proceed?")

        self.config.data["runtime"]["max_attempts"] = 1
        backend = ScriptedBackend(malformed)
        engine = self.engine(backend)
        # An invalid task blocker spends its attempt; the exhausted budget stops before any decide.
        with self.assertRaisesRegex(HarnessError, "尝试次数上限"):
            engine.start("Implement greet", self.plan())
        feedback = engine.run["feedback"]
        self.assertIn("output_error", feedback)
        self.assertTrue(Path(feedback["worker_artifact"]).is_file())
        with self.assertRaisesRegex(HarnessError, "尝试次数上限"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual([c["kind"] for c in backend.calls], ["implement"])
        self.config.data["runtime"]["max_attempts"] = 2
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            resumed.resume(engine.run["id"])
        self.assertEqual([c["kind"] for c in backend.calls], ["implement", "decide"])
        seen = backend.calls[-1]["packet"]["trigger"]["feedback"]
        self.assertEqual((seen["worker_artifact"], seen["output_error"]), (feedback["worker_artifact"], feedback["output_error"]))
        self.assertEqual(sum(resumed.run["attempts"].values()), 1)

    def test_semantically_invalid_worker_is_not_routed_as_task_blocker(self):
        def invalid(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.update(status="completed", blocker_kind="task", blockers=["Inconsistent status"], summary=" ")

        engine = self.engine(ScriptedBackend(invalid))
        with self.assertRaisesRegex(HarnessError, "尝试次数上限"):
            engine.start("Implement greet", self.plan())
        workers = [d for d in engine.run["dispatches"] if d["role"] == "worker"]
        self.assertEqual({(d["status"], d["worker_receipt_consumed"]) for d in workers}, {("invalid", "result_rejected")})
        self.assertFalse(any("worker_outcome" in d for d in workers))
        self.assertEqual(engine.run["decision_requests"], [])

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
        saved = self.fail_post_agent(engine, "decide", pause_after_plan=True)
        receipt = saved["commander_receipts"]["commander:decide:run"]
        receipt["request"]["schema"]["additionalProperties"] = True
        write_json(engine.directory / "state.json", saved)
        with self.assertRaisesRegex(HarnessError, "指挥原始请求已改变"):
            self.engine(backend).resume(saved["id"])
        self.assertEqual(len(backend.calls), 1)

    def test_reverify_cannot_discard_returned_commander_question(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        verify = engine.services.verification.verify
        emit = engine.hooks.emit

        def failed_final(*args, **kwargs):
            if kwargs.get("whole_run"):
                return [{"argv": ["integration-check"], "exit_code": 1, "stdout": "",
                         "stderr": "Integration failed", "log": "integration.log", "timed_out": False}]
            return verify(*args, **kwargs)

        def fail_replan(event, payload, logs):
            if event == "post_agent" and payload.get("kind") == "decide":
                raise HarnessError("stop hook unavailable")
            return emit(event, payload, logs)

        with patch.object(engine.services.verification, "verify", side_effect=failed_final), patch.object(engine.hooks, "emit", side_effect=fail_replan):
            with self.assertRaisesRegex(HarnessError, "stop hook unavailable"):
                engine.start("Implement greet", self.plan())
        calls = copy.deepcopy(backend.calls)
        before = (engine.directory / "state.json").read_bytes()
        with self.assertRaisesRegex(HarnessError, "--reverify 不能用于已返回、尚未处理的指挥决定"):
            self.engine(backend).resume(engine.run["id"], reverify=True)
        self.assertEqual((engine.directory / "state.json").read_bytes(), before)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定：task_id=final"):
            resumed.resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)
        self.assertEqual(resumed.run["decision_requests"][-1]["kind"], "commander")

    def test_resumed_invalid_review_records_its_error_and_is_redone(self):
        def invalid(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                result["dispatch_id"] = "unrelated-dispatch"

        backend = ScriptedBackend(invalid)
        engine = self.engine(backend)
        saved = self.interrupt_result(engine, "review", self.plan())
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "连续两次无效"):
            resumed.resume(saved["id"])
        first, second = [d for d in resumed.run["dispatches"] if d.get("kind") == "review"]
        self.assertEqual((first["status"], second["status"]), ("invalid", "invalid"))
        self.assertEqual(second["retry_of"], first["id"])
        for entry in (first, second):
            self.assertIn("dispatch_id", entry["error"])

    def test_ask_request_file_interruption_registers_one_request_without_new_decide(self):
        backend = ScriptedBackend(self.block_worker)
        engine = self.engine(backend)
        stopped = False

        def interrupt_request_file(path, value):
            nonlocal stopped
            write_json(path, value)
            if not stopped and Path(path).parent.name == "decisions":
                stopped = True
                raise KeyboardInterrupt()

        with patch("harness.decisions.write_json", side_effect=interrupt_request_file):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["decision_requests"], [])
        self.assertIn("commander:decide:run", saved["commander_receipts"])
        artifact = next((engine.directory / "decisions").glob("request_*.json"))
        written = artifact.read_bytes()
        calls = copy.deepcopy(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            resumed.resume(saved["id"])
        self.assertEqual(backend.calls, calls)
        self.assertEqual(len(resumed.run["decision_requests"]), 1)
        self.assertEqual(resumed.run["decision_requests"][0]["artifact"], str(artifact))
        self.assertEqual(artifact.read_bytes(), written)
        self.assertEqual(resumed.run["commander_receipts"], {})
