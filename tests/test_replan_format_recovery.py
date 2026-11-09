"""Replanning retires abandoned worker format repair without relaxing recovery gates."""
import copy
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class ReplanFormatRecoveryTests(RepoCase):
    key = "worker:implement:greet"

    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def saved(self, engine):
        return read_json(engine.directory / "state.json")

    def interrupted_correction(self, failure="pre_agent"):
        """Reach the real automatic-repair barrier, including its source artifact."""
        def fail_repair(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                if "format_correction" not in packet:
                    result["summary"] = "x" * 4001
            elif kwargs["kind"] == "dispatch":
                result.update(action="replan", instruction="Resolve the unmet requirement through planning")

        def fail_before_result(backend, kwargs, packet):
            if kwargs["role"] == "worker" and "format_correction" in packet and failure == "provider":
                # A failed mutating invocation can leave a newer candidate.
                with (kwargs["cwd"] / "hello.py").open("a") as stream:
                    stream.write("# partial format correction\n")
                self.assertFalse((kwargs["job"] / "response.json").exists())
                raise HarnessError("provider failed before returning a result")

        backend = ScriptedBackend(fail_repair)
        backend.before_result = fail_before_result
        engine = self.engine(backend)
        emit = engine.context.emit_hook

        def local_failure(event, payload, logs):
            if (failure == "pre_agent" and event == "pre_agent" and payload.get("role") == "worker"
                    and len([d for d in engine.run["dispatches"] if d["role"] == "worker"]) == 2):
                raise HarnessError("local pre_agent preparation failed")
            return emit(event, payload, logs)

        with patch.object(engine.context, "emit_hook", side_effect=local_failure):
            with self.assertRaisesRegex(HarnessError, "pre_agent preparation|provider failed"):
                engine.start("Implement greet", self.plan())
        state = self.saved(engine)
        correction = copy.deepcopy(state["pending_format_corrections"][self.key])
        workers = [d for d in state["dispatches"] if d["role"] == "worker"]
        self.assertEqual(len(workers), 2)
        self.assertEqual([d["invoked"] for d in workers], [True, failure == "provider"])
        self.assertFalse((engine.directory / "dispatches" / workers[-1]["id"] / "returned-result.json").exists())
        self.assertEqual(state["total_attempts"], 2 if failure == "provider" else 1)
        self.assertEqual(state["format_repairs"], {self.key: 1} if failure == "provider" else {})
        source = Path(correction["correction"]["artifact"]) / "returned-result.json"
        self.assertEqual(read_json(source)["dispatch_id"], workers[0]["id"])
        return engine, backend, correction

    def stopped(self, failure="pre_agent", *, legacy=False):
        engine, backend, correction = self.interrupted_correction(failure)
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            self.engine(backend).resume(engine.run["id"])
        state = self.saved(engine)
        self.assertIn("replan_stopped", state)
        self.assertEqual(state["decision_requests"][-1]["kind"], "replan_stop")
        if legacy:
            state["pending_format_corrections"][self.key] = correction
            write_json(engine.directory / "state.json", state)
        return engine, backend, correction

    def answer(self, engine, decision="continue"):
        request = self.saved(engine)["decision_requests"][-1]
        path = self.repo / "answer.json"
        write_json(path, {"schema_version": 1, "request_id": request["id"], "task_id": "greet",
                          "decision": decision, "summary": "Explicitly " + decision})
        return path

    def assert_preserved(self, before, after, *, replan_delta=0, dispatch_delta=0):
        for field in ("attempts", "total_attempts", "format_repairs", "accepted", "tasks", "original_tasks",
                      "review_history", "final_attempts", "final_history", "worker_recovery"):
            self.assertEqual(after.get(field), before.get(field), field)
        self.assertEqual(after["replan_attempts"], before["replan_attempts"] + replan_delta)
        self.assertEqual(after["agent_dispatches"], before["agent_dispatches"] + dispatch_delta)
        prior_dispatches = copy.deepcopy(before["dispatches"])
        for prior, current in zip(prior_dispatches, after["dispatches"]):
            if not prior.get("worker_receipt_consumed") and current.get("worker_receipt_consumed") == "provider_failure":
                self.assertEqual(current.get("worker_receipt_consumed"), "provider_failure")
                prior["worker_receipt_consumed"] = "provider_failure"
                self.assertEqual(current["worker_return_evidence"]["state"], "retryable")
                prior["worker_return_evidence"] = copy.deepcopy(current["worker_return_evidence"])
        self.assertEqual(after["dispatches"][:len(prior_dispatches)], prior_dispatches)

    def test_replan_selection_retires_only_worker_correction_for_both_failure_boundaries(self):
        for failure in ("pre_agent", "provider"):
            with self.subTest(failure=failure):
                engine, backend, correction = self.interrupted_correction(failure)
                before = self.saved(engine)
                source = Path(correction["correction"]["artifact"]) / "returned-result.json"
                evidence = source.read_bytes()
                with self.assertRaisesRegex(HarnessError, "停止重新规划"):
                    self.engine(backend).resume(engine.run["id"])
                state = self.saved(engine)
                self.assertNotIn(self.key, state["pending_format_corrections"])
                self.assertNotIn("pending_directive", state)
                self.assertNotIn("commander:dispatch:dispatching", state["commander_receipts"])
                self.assert_preserved(before, state, replan_delta=1, dispatch_delta=2)
                self.assertEqual(source.read_bytes(), evidence)
                calls = len(backend.calls)
                with self.assertRaisesRegex(HarnessError, "停止重新规划"):
                    self.engine(backend).resume(engine.run["id"])
                self.assertEqual(len(backend.calls), calls)

    def test_operator_stop_then_continue_reopens_planning_without_worker(self):
        engine, backend, unused = self.stopped("provider")
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
                engine, backend, unused = self.interrupted_correction()
                resumed = self.engine(backend)
                save = resumed.context.save
                interrupted = False

                def interrupt_transition():
                    nonlocal interrupted
                    pending = resumed.run.get("pending_replan", {})
                    if not interrupted and pending.get("trigger", {}).get("coordination"):
                        interrupted = True
                        # The selection's successor and correction removal belong
                        # to the same durable transition, including stop handling.
                        self.assertNotIn(self.key, resumed.run["pending_format_corrections"])
                        if persist:
                            save()
                        raise KeyboardInterrupt()
                    save()

                # A process crash cannot run execute's stop/save handler. Keep
                # that handler from making the interrupted transition durable.
                with patch.object(resumed.context, "save", side_effect=interrupt_transition), patch.object(resumed.services.workflow, "stop"):
                    with self.assertRaises(KeyboardInterrupt):
                        resumed.resume(engine.run["id"])
                self.assertTrue(interrupted)
                persisted = self.saved(engine)
                if persist:
                    self.assertIn("pending_replan", persisted)
                    self.assertNotIn(self.key, persisted["pending_format_corrections"])
                    self.assertNotIn("commander:dispatch:dispatching", persisted["commander_receipts"])
                else:
                    self.assertNotIn("pending_replan", persisted)
                    self.assertIn(self.key, persisted["pending_format_corrections"])
                    self.assertIn("commander:dispatch:dispatching", persisted["commander_receipts"])
                with self.assertRaisesRegex(HarnessError, "停止重新规划"):
                    self.engine(backend).resume(engine.run["id"])
                self.assertNotIn(self.key, self.saved(engine)["pending_format_corrections"])
                self.assertEqual([c["kind"] for c in backend.calls], ["implement", "dispatch", "replan"])

    def test_superseded_correction_is_rejected_without_migrating_stopped_run(self):
        for failure in ('pre_agent', 'provider'):
            with self.subTest(failure=failure):
                engine, backend, correction = self.stopped(failure, legacy=True)
                before = self.saved(engine)
                calls = len(backend.calls)
                for answer in (None, self.answer(engine, 'continue')):
                    with self.assertRaisesRegex(HarnessError, 'does not repair legacy state'):
                        self.engine(backend).resume(engine.run['id'], operator_decision=answer)
                    self.assertEqual(self.saved(engine), before)
                    self.assertEqual(len(backend.calls), calls)

    def test_superseded_correction_is_rejected_at_pending_replan_without_spending_budget(self):
        self.config.data['runtime']['max_replans'] = 0
        engine, backend, correction = self.interrupted_correction('provider')
        with self.assertRaisesRegex(HarnessError, '重新规划次数上限'):
            self.engine(backend).resume(engine.run['id'])
        state = self.saved(engine)
        state['pending_format_corrections'][self.key] = correction
        write_json(engine.directory / 'state.json', state)
        calls = len(backend.calls)
        self.config.data['runtime']['max_replans'] = 1
        with self.assertRaisesRegex(HarnessError, 'does not repair legacy state'):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(self.saved(engine), state)
        self.assertEqual(len(backend.calls), calls)

    def test_exit_gate_precedes_rejection_of_superseded_correction(self):
        engine, backend, unused = self.stopped('provider', legacy=True)
        before = self.saved(engine)
        calls = len(backend.calls)
        resumed = self.engine(backend)
        with patch.object(resumed.context, 'ensure_exited', side_effect=HarnessError('exit not confirmed')):
            with self.assertRaisesRegex(HarnessError, 'exit not confirmed'):
                resumed.resume(engine.run['id'], operator_decision=self.answer(engine))
        self.assertEqual(self.saved(engine), before)
        self.assertEqual(len(backend.calls), calls)
