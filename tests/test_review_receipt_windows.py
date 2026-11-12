from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, finding


class ReviewReceiptWindowTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def interrupt_after_final_result(self, engine):
        interrupted = False
        artifact = None

        def write_then_interrupt(path, value):
            nonlocal interrupted, artifact
            write_json(path, value)
            if (not interrupted and Path(path).name == "returned-result.json"
                    and engine.run["dispatches"][-1].get("kind") == "final_review"):
                interrupted, artifact = True, Path(path)
                raise KeyboardInterrupt()

        with patch("harness.dispatch.write_json", side_effect=write_then_interrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        self.assertIsNotNone(artifact)
        self.assertTrue(artifact.is_file())
        return artifact

    def test_rejection_written_before_receipt_save_is_revalidated_without_reroll(self):
        def reject_first(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and sum(call["kind"] == "final_review" for call in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding()])

        backend = ScriptedBackend(reject_first)
        engine = self.engine(backend)
        artifact = self.interrupt_after_final_result(engine)
        evidence = artifact.read_bytes()
        calls = list(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定：task_id=final"):
            resumed.resume(engine.run["id"])
        # The written rejection is revalidated, not redrawn; the next call is the decision.
        self.assertEqual([c["kind"] for c in backend.calls[len(calls):]], ["decide"])
        self.assertEqual(resumed.run["final_history"][-1]["status"], "rejected")
        self.assertEqual(len(resumed.run["final_history"]), 1)
        self.assertEqual(artifact.read_bytes(), evidence)
        calls = list(backend.calls)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            self.engine(backend).resume(engine.run["id"], reverify=True)
        self.assertEqual(backend.calls, calls)

    def test_cached_result_with_wrong_dispatch_identity_never_reaches_hook_or_acceptance(self):
        def wrong_identity(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result["dispatch_id"] = "wrong-dispatch-id"

        backend = ScriptedBackend(wrong_identity)
        engine = self.engine(backend)
        artifact = self.interrupt_after_final_result(engine)
        evidence = artifact.read_bytes()
        calls = list(backend.calls)
        resumed = self.engine(backend)
        with patch.object(resumed.hooks, "emit", wraps=resumed.hooks.emit) as hooks:
            # The cached result is judged invalid without a new call, then redone once.
            with self.assertRaisesRegex(HarnessError, "连续两次无效"):
                resumed.resume(engine.run["id"])
        final_hooks = [call for call in hooks.call_args_list
                       if call.args[0] == "post_agent" and call.args[1].get("kind") == "final_review"]
        self.assertEqual(final_hooks, [])
        self.assertEqual(resumed.run["status"], "blocked")
        self.assertEqual(resumed.run["final_history"][-1]["status"], "running")
        self.assertEqual(backend.calls[:len(calls)], calls)
        self.assertEqual([call["kind"] for call in backend.calls[len(calls):]], ["final_review"])
        finals = [d for d in resumed.run["dispatches"] if d.get("kind") == "final_review"]
        self.assertEqual([d["status"] for d in finals], ["invalid", "invalid"])
        self.assertEqual(finals[0]["invalid_output"]["artifact"], str(artifact))
        self.assertEqual(artifact.read_bytes(), evidence)

    def fail_final_hook(self, engine):
        emit = engine.hooks.emit

        def unavailable(event, payload, logs):
            if event == "post_agent" and payload.get("kind") == "final_review":
                raise HarnessError("post-agent policy service unavailable")
            return emit(event, payload, logs)

        with patch.object(engine.hooks, "emit", side_effect=unavailable), self.assertRaisesRegex(HarnessError, "policy service"):
            engine.start("Implement greet", self.plan())
        dispatch = engine.run["dispatches"][-1]
        return engine.directory / "dispatches" / dispatch["id"] / "returned-result.json"

    def test_hook_failure_retries_gate_for_same_returned_approval_without_more_models(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        artifact = self.fail_final_hook(engine)
        evidence = artifact.read_bytes()
        calls = list(backend.calls)
        resumed = self.engine(backend)
        with patch.object(resumed.hooks, "emit", wraps=resumed.hooks.emit) as hooks:
            result = resumed.resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["final_history"]), 1)
        self.assertEqual(backend.calls, calls)
        self.assertEqual(artifact.read_bytes(), evidence)
        final_hooks = [call for call in hooks.call_args_list
                       if call.args[0] == "post_agent" and call.args[1].get("kind") == "final_review"]
        self.assertEqual(len(final_hooks), 1)

    def test_transient_memory_check_error_reuses_valid_task_review_and_worker(self):
        self.config.data["runtime"]["max_attempts"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with patch.object(engine.memory, "check_proposals", side_effect=OSError("memory storage temporarily unavailable")):
            with self.assertRaisesRegex(OSError, "memory storage"):
                engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["accepted"], {})
        self.assertEqual(saved["pending_task"]["stage"], "reviewing")
        artifact = Path(saved["review_receipts"]["reviewer:review:greet"]["artifact"])
        evidence = artifact.read_bytes()
        task_calls = [call for call in backend.calls if call["task_id"] == "greet"]
        task_checks = [entry for entry in saved["dispatches"]
                       if entry["role"] == "verification" and entry["task_id"] == "greet"]
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(result["attempts"].values()), 1)
        self.assertEqual(result["attempts"]["greet"], 1)
        self.assertEqual([call for call in backend.calls if call["task_id"] == "greet"], task_calls)
        self.assertEqual([entry for entry in result["dispatches"]
                          if entry["role"] == "verification" and entry["task_id"] == "greet"], task_checks)
        self.assertEqual(len(result["review_history"]["greet"]), 1)
        self.assertEqual(artifact.read_bytes(), evidence)

    def test_hash_bound_missing_result_is_not_treated_as_an_unreturned_call(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        artifact = self.fail_final_hook(engine)
        calls = list(backend.calls)
        artifact.unlink()
        resumed = self.engine(backend)
        with self.assertRaises((HarnessError, OSError)):
            resumed.resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)
        self.assertEqual(resumed.run["status"], "blocked")
        self.assertNotEqual(resumed.run["final_history"][-1]["status"], "approved")

    def test_hash_bound_modified_result_cannot_bypass_retrying_hook(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        artifact = self.fail_final_hook(engine)
        calls = list(backend.calls)
        changed = read_json(artifact)
        changed["dispatch_id"] = "forged-dispatch-id"
        write_json(artifact, changed)
        resumed = self.engine(backend)
        with patch.object(resumed.hooks, "emit", wraps=resumed.hooks.emit) as hooks:
            with self.assertRaises(HarnessError):
                resumed.resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)
        self.assertFalse(any(call.args[0] == "post_agent" for call in hooks.call_args_list))
        self.assertNotEqual(resumed.run["final_history"][-1]["status"], "approved")

    def test_reviewer_service_retries_are_bounded_and_explicit_budget_increase_recovers(self):
        self.config.data["runtime"].update(max_agent_dispatches=5, max_attempts=1)
        available = False
        dispatch_ids = []

        def service(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                dispatch_ids.append(result["dispatch_id"])
                if not available:
                    raise HarnessError("reviewer service unavailable")

        backend = ScriptedBackend(service)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "reviewer service"):
            engine.start("Implement greet", self.plan())
        for _ in range(2):
            with self.assertRaisesRegex(HarnessError, "reviewer service"):
                self.engine(backend).resume(engine.run["id"])
        calls = list(backend.calls)
        self.assertEqual(sum(call["kind"] == "final_review" for call in calls), 3)
        with self.assertRaisesRegex(HarnessError, "max_agent_dispatches"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["agent_dispatches"], 5)
        self.assertEqual(len(saved["final_history"]), 1)
        self.assertEqual(sum(saved["attempts"].values()), 1)
        available = True
        self.config.data["runtime"]["max_agent_dispatches"] = 6
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["agent_dispatches"], 6)
        self.assertEqual(len(result["final_history"]), 1)
        self.assertEqual(sum(result["attempts"].values()), 1)
        self.assertEqual(len(dispatch_ids), 4)
        self.assertEqual(len(dispatch_ids), len(set(dispatch_ids)))

    def test_observed_malformed_result_after_crash_stays_bound_when_removed(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)

        def interrupt(path, value):
            write_json(path, value)
            if Path(path).name == "returned-result.json" and value.get("task_id") == "final":
                Path(path).write_bytes(b"{")
                raise KeyboardInterrupt()

        with patch("harness.dispatch.write_json", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        key = "reviewer:final_review:final"
        receipt = engine.run["review_receipts"][key]
        self.assertNotIn("artifact_digest", receipt)
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "不是有效 JSON"):
            self.engine(backend).resume(engine.run["id"])
        saved = read_json(engine.directory / "state.json")
        self.assertIn("artifact_digest", saved["review_receipts"][key])
        Path(receipt["artifact"]).unlink()
        with self.assertRaisesRegex(HarnessError, "缺失或无法读取"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(len(read_json(engine.directory / "state.json")["final_history"]), 1)

    def test_missing_review_receipt_updates_dispatch_and_runtime_error(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)

        def fail(event, payload, logs):
            if event == "post_agent" and payload.get("kind") == "final_review":
                raise HarnessError("hook unavailable")
        with patch.object(engine.hooks, "emit", side_effect=fail):
            with self.assertRaisesRegex(HarnessError, "hook unavailable"):
                engine.start("Implement greet", self.plan())
        source = engine.run["review_receipts"]["reviewer:final_review:final"]
        Path(source["artifact"]).unlink()
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "缺失或无法读取"):
            resumed.resume(engine.run["id"])
        self.assertIn("缺失或无法读取", resumed.run["last_runtime_error"]["error"])
        entry = next(d for d in resumed.run["dispatches"] if d["id"] == source["dispatch_id"])
        self.assertEqual(entry["error"], resumed.run["last_runtime_error"]["error"])
