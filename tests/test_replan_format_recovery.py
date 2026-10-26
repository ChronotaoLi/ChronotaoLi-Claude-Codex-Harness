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
                elif failure == "provider":
                    # A failed mutating invocation can leave a newer candidate.
                    with (kwargs["cwd"] / "hello.py").open("a") as stream:
                        stream.write("# partial format correction\n")
                    raise HarnessError("provider failed before returning a result")
            elif kwargs["kind"] == "dispatch":
                result.update(action="replan", instruction="Resolve the unmet requirement through planning")

        backend = ScriptedBackend(fail_repair)
        engine = self.engine(backend)
        emit = engine.emit_hook

        def local_failure(event, payload, logs):
            if (failure == "pre_agent" and event == "pre_agent" and payload.get("role") == "worker"
                    and len([d for d in engine.run["dispatches"] if d["role"] == "worker"]) == 2):
                raise HarnessError("local pre_agent preparation failed")
            return emit(event, payload, logs)

        with patch.object(engine, "emit_hook", side_effect=local_failure):
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
        self.assertEqual(after["dispatches"][:len(before["dispatches"])], before["dispatches"])

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

    def test_legacy_stopped_cleanup_preserves_stop_and_budgets_without_invoking(self):
        for failure in ("pre_agent", "provider"):
            with self.subTest(failure=failure):
                engine, backend, correction = self.stopped(failure, legacy=True)
                before = self.saved(engine)
                calls = len(backend.calls)
                with self.assertRaisesRegex(HarnessError, "停止重新规划"):
                    self.engine(backend).resume(engine.run["id"])
                state = self.saved(engine)
                self.assertNotIn(self.key, state["pending_format_corrections"])
                self.assertEqual(state["replan_stopped"], before["replan_stopped"])
                self.assertEqual(state["decision_requests"], before["decision_requests"])
                self.assertEqual(state["operator_decisions"], [])
                self.assertEqual(len(backend.calls), calls)
                self.assert_preserved(before, state)

    def test_legacy_stopped_correction_does_not_block_explicit_decisions(self):
        engine, backend, unused = self.stopped("provider", legacy=True)
        before = self.saved(engine)
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "操作者决定停止"):
            self.engine(backend).resume(engine.run["id"], operator_decision=self.answer(engine, "stop"))
        self.assertEqual(len(backend.calls), calls)
        self.assertNotIn(self.key, self.saved(engine)["pending_format_corrections"])
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            self.engine(backend).resume(engine.run["id"], operator_decision=self.answer(engine))
        self.assertEqual([c["kind"] for c in backend.calls[calls:]], ["replan"])
        self.assert_preserved(before, self.saved(engine), replan_delta=1, dispatch_delta=1)

    def test_legacy_pending_replan_recovers_without_reselecting_or_worker(self):
        self.config.data["runtime"]["max_replans"] = 0
        engine, backend, correction = self.interrupted_correction("provider")
        with self.assertRaisesRegex(HarnessError, "重新规划次数上限"):
            self.engine(backend).resume(engine.run["id"])
        state = self.saved(engine)
        self.assertEqual(state["pending_replan"]["stage"], "pending")
        state["pending_format_corrections"][self.key] = correction
        write_json(engine.directory / "state.json", state)
        calls = len(backend.calls)
        # Normalization must not reset the exhausted replan budget.
        with self.assertRaisesRegex(HarnessError, "重新规划次数上限"):
            self.engine(backend).resume(engine.run["id"])
        normalized = self.saved(engine)
        self.assertNotIn(self.key, normalized["pending_format_corrections"])
        self.assertEqual(normalized["pending_replan"], state["pending_replan"])
        self.assertEqual(len(backend.calls), calls)
        self.assert_preserved(state, normalized)
        self.config.data["runtime"]["max_replans"] = 1
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual([c["kind"] for c in backend.calls[calls:]], ["replan"])
        self.assert_preserved(state, self.saved(engine), replan_delta=1, dispatch_delta=1)

    def test_transition_save_is_atomic_across_interruptions(self):
        for persist in (False, True):
            with self.subTest(persist=persist):
                engine, backend, unused = self.interrupted_correction()
                resumed = self.engine(backend)
                save = resumed.save
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
                with patch.object(resumed, "save", side_effect=interrupt_transition), patch.object(resumed, "stop"):
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

    def test_legacy_cleanup_rejects_changed_selection_or_candidate_without_mutation(self):
        engine, backend, unused = self.stopped("provider", legacy=True)
        original = self.saved(engine)
        source = original["replan_stopped"]["trigger"]["coordination"]
        artifact = Path(source["artifact"])
        artifact_bytes = artifact.read_bytes()
        hello = engine.worktree / "hello.py"
        hello_bytes = hello.read_bytes()
        calls = len(backend.calls)
        for mutation in ("artifact", "digest", "source", "selection", "snapshot", "current_task"):
            with self.subTest(mutation=mutation):
                state = copy.deepcopy(original)
                trigger = state["replan_stopped"]["trigger"]
                if mutation == "artifact":
                    artifact.write_bytes(artifact_bytes + b"\n")
                elif mutation == "digest":
                    trigger["coordination"].pop("artifact_digest")
                elif mutation == "source":
                    trigger["coordination"]["dispatch_id"] = state["dispatches"][0]["id"]
                elif mutation == "selection":
                    trigger["coordination"]["instruction"] = "Altered selection"
                elif mutation == "snapshot":
                    hello.write_bytes(hello_bytes + b"# changed after selection\n")
                else:
                    state["current_task"] = None
                write_json(engine.directory / "state.json", state)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"], operator_decision=self.answer(engine))
                saved = self.saved(engine)
                self.assertEqual(saved, state)
                self.assertEqual(saved["pending_format_corrections"][self.key], original["pending_format_corrections"][self.key])
                self.assertEqual(len(backend.calls), calls)
                artifact.write_bytes(artifact_bytes)
                hello.write_bytes(hello_bytes)
        write_json(engine.directory / "state.json", original)

    def test_existing_exit_gate_precedes_legacy_cleanup(self):
        engine, backend, unused = self.stopped("provider", legacy=True)
        before = self.saved(engine)
        calls = len(backend.calls)
        resumed = self.engine(backend)
        with patch.object(resumed, "ensure_exited", side_effect=HarnessError("exit not confirmed")):
            with self.assertRaisesRegex(HarnessError, "exit not confirmed"):
                resumed.resume(engine.run["id"], operator_decision=self.answer(engine))
        self.assertEqual(self.saved(engine), before)
        self.assertEqual(len(backend.calls), calls)

    def test_legacy_cleanup_preserves_unrelated_correction_and_receipt_gates(self):
        engine, backend, worker_correction = self.interrupted_correction()
        change = backend.change
        commander_key = "commander:dispatch:dispatching"

        def correct_selection(backend, kwargs, packet, result):
            change(backend, kwargs, packet, result)
            if kwargs["kind"] == "dispatch" and "format_correction" not in packet:
                result["instruction"] = " "

        backend.change = correct_selection
        resumed = self.engine(backend)
        save = resumed.save
        captured = {}

        def capture_real_gates():
            pending = resumed.run["pending_format_corrections"].get(commander_key)
            receipt = resumed.run["commander_receipts"].get(commander_key)
            if pending:
                captured["correction"] = copy.deepcopy(pending)
            if receipt and receipt.get("hook_completed"):
                captured["receipt"] = copy.deepcopy(receipt)
            save()

        with patch.object(resumed, "save", side_effect=capture_real_gates):
            with self.assertRaisesRegex(HarnessError, "停止重新规划"):
                resumed.resume(engine.run["id"])
        self.assertEqual(set(captured), {"correction", "receipt"})
        original = self.saved(engine)
        calls = len(backend.calls)
        # Each restored gate was produced by this run's actual selection repair.
        # Neither is authority to waive operator-decision busy checks.
        for gate in ("correction", "receipt"):
            with self.subTest(gate=gate):
                state = copy.deepcopy(original)
                state["pending_format_corrections"][self.key] = worker_correction
                field = "pending_format_corrections" if gate == "correction" else "commander_receipts"
                state[field][commander_key] = copy.deepcopy(captured[gate])
                write_json(engine.directory / "state.json", state)
                with self.assertRaisesRegex(HarnessError, "pending lifecycle gates"):
                    self.engine(backend).resume(engine.run["id"], operator_decision=self.answer(engine))
                after = self.saved(engine)
                self.assertNotIn(self.key, after["pending_format_corrections"])
                self.assertEqual(after[field][commander_key], captured[gate])
                self.assertEqual(after["operator_decisions"], [])
                self.assertEqual(after["replan_stopped"], original["replan_stopped"])
                self.assert_preserved(state, after)
                self.assertEqual(len(backend.calls), calls)

    def test_legacy_cleanup_rejects_worker_origins_after_selection(self):
        engine, backend, unused = self.stopped("provider", legacy=True)
        original = self.saved(engine)
        calls = len(backend.calls)
        for origin in ("previous", "correction", "subsequent_worker"):
            with self.subTest(origin=origin):
                state = copy.deepcopy(original)
                pending = state["pending_format_corrections"][self.key]
                ident = pending["correction"]["previous_dispatch"] if origin == "previous" else pending["dispatch_id"]
                row = next(d for d in state["dispatches"] if d["id"] == ident)
                state["dispatches"].remove(row)
                state["dispatches"].append(row)
                if origin == "subsequent_worker":
                    # Isolate the no-later-worker guard from origin chronology:
                    # the old origin stays before selection, but another invoked
                    # worker now appears after it in the tampered ledger.
                    pending.pop("dispatch_id")
                write_json(engine.directory / "state.json", state)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"], operator_decision=self.answer(engine))
                self.assertEqual(self.saved(engine), state)
                self.assertEqual(len(backend.calls), calls)

    def test_legacy_cleanup_requires_a_saved_coordination_selection(self):
        engine, backend, unused = self.stopped(legacy=True)
        state = self.saved(engine)
        state["replan_stopped"]["trigger"].pop("coordination")
        write_json(engine.directory / "state.json", state)
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "pending lifecycle gates"):
            self.engine(backend).resume(engine.run["id"], operator_decision=self.answer(engine))
        self.assertEqual(self.saved(engine), state)
        self.assertEqual(len(backend.calls), calls)
