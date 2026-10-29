from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from harness.recovery import worker_recovery_context
from harness.util import HarnessError, ProcessTimeout, dump, read_json
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT
from test_runtime_changes import finding


class RecoveryProjectionTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def interrupted_rework(self):
        self.config.data["runtime"]["max_attempts"] = 4

        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                number = sum(call["role"] == "worker" for call in backend.calls)
                if number in (1, 3):
                    (kwargs["job"] / "failure.log").write_text(f"timeout evidence {number}\n")
                    raise ProcessTimeout(number, kwargs["job"] / "cli")
            elif kwargs["kind"] == "review" and sum(call["kind"] == "review" for call in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding()])

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(ProcessTimeout):
            engine.start("Implement greet", self.plan())
        original = deepcopy(engine.run["worker_recovery"]["greet"])
        resumed = self.engine(backend)
        with self.assertRaises(ProcessTimeout):
            resumed.resume(engine.run["id"])
        return resumed, backend, original

    def test_completed_verified_rejected_successor_is_distinct_from_old_timeout(self):
        engine, backend, original = self.interrupted_rework()
        workers = [entry for entry in engine.run["dispatches"] if entry["role"] == "worker"]
        history = engine.run["review_history"]["greet"][-1]
        calls = [call for call in backend.calls if call["kind"] == "dispatch" or call["role"] == "worker"]
        # The second commander selection and third worker both repair the saved
        # rejection, while still retaining the first invocation's timeout.
        for call in calls[-2:]:
            recovery = call["packet"]["recovery"]
            self.assertEqual({key: recovery[key] for key in original}, original)
            self.assertEqual(recovery["relation"], "earlier_worker_failure")
            self.assertEqual(recovery["source"]["dispatch_id"], workers[0]["id"])
            self.assertEqual(recovery["source"]["attempt"], 1)
            self.assertEqual(recovery["source"]["invoked_at"], workers[0]["invoked_at"])
            self.assertEqual(recovery["superseded_by"], workers[1]["id"])
            latest = recovery["latest"]
            self.assertEqual(latest["worker"]["dispatch_id"], workers[1]["id"])
            self.assertEqual(latest["worker"]["worker_outcome"], "completed")
            self.assertEqual(latest["worker"]["attempt"], 2)
            self.assertEqual(latest["verification"], {
                "attempt": 2, "artifact": history["verification"], "status": "passed"})
            self.assertEqual(latest["review"]["status"], "rejected")
            self.assertEqual(latest["review"]["artifact"], history["artifact"])
            self.assertEqual(latest["review"]["snapshot_digest"], history["snapshot_digest"])
            self.assertEqual(latest["review"]["attempt"], 2)
            self.assertTrue(all(check["exit_code"] == 0 for check in read_json(Path(latest["verification"]["artifact"]))))
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(engine.run["attempts"], {"greet": 3})
        self.assertNotIn("source", engine.run["worker_recovery"]["greet"])

    def test_later_failure_replaces_projection_and_acceptance_retains_audit_evidence(self):
        engine, backend, original = self.interrupted_rework()
        failed = deepcopy(engine.run["worker_recovery"]["greet"])
        before = deepcopy(engine.run)
        projected = worker_recovery_context(engine.run, engine.directory, "greet")
        self.assertEqual(engine.run, before)
        self.assertEqual(projected["relation"], "latest_worker_failure")
        self.assertEqual(projected["source"]["dispatch_id"], failed["dispatch_id"])
        self.assertEqual(projected["source"]["attempt"], 3)
        self.assertNotIn("superseded_by", projected)
        self.assertNotIn("review", projected["latest"])
        self.assertNotIn("verification", projected["latest"])
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 4})
        self.assertEqual(result["total_attempts"], 4)
        self.assertEqual(result["agent_dispatches"], len(backend.requests))
        self.assertEqual(len({request["dispatch_id"] for request in backend.requests}), len(backend.requests))
        self.assertNotIn("greet", result["worker_recovery"])
        self.assertIsNone(worker_recovery_context(result, engine.directory, "greet"))
        for source, attempt in ((original, 1), (failed, 3)):
            entry = next(item for item in result["dispatches"] if item["id"] == source["dispatch_id"])
            self.assertEqual(entry["status"], "failed")
            self.assertEqual(entry["error"], source["error"])
            self.assertEqual((Path(source["artifact"]) / "failure.log").read_text(), f"timeout evidence {attempt}\n")
        self.assertEqual([item["attempt"] for item in result["review_history"]["greet"]], [2, 4])
        self.assertEqual(result["review_history"]["greet"][0]["review"]["findings"], [finding()])
        successors = backend.calls[-4:-2]
        self.assertEqual([call["role"] for call in successors], ["commander", "worker"])
        for call in successors:
            self.assertEqual(call["packet"]["recovery"]["source"]["dispatch_id"], failed["dispatch_id"])

    def test_pending_review_uses_saved_worker_and_formal_verification(self):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(call["role"] == "worker" for call in backend.calls) == 1:
                raise ProcessTimeout(1, kwargs["job"] / "cli")
            if kwargs["kind"] == "review":
                raise HarnessError("review service unavailable")

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(ProcessTimeout):
            engine.start("Implement greet", self.plan())
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "review service"):
            resumed.resume(engine.run["id"])
        packet = next(call["packet"] for call in backend.calls if call["kind"] == "review")
        projected = packet["recovery"]
        self.assertEqual(projected["latest"]["worker"]["attempt"], 2)
        self.assertEqual(projected["latest"]["verification"]["status"], "passed")
        self.assertEqual(projected["latest"]["verification"]["artifact"], resumed.run["pending_task"]["verification"])
        self.assertEqual(projected["latest"]["review"]["status"], "pending")
        self.assertEqual(resumed.run["accepted"], {})

    def assert_changed_approval_projection(self, *, replan=False, legacy=False):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(call["role"] == "worker" for call in backend.calls) in (1, 3):
                raise ProcessTimeout(1, kwargs["job"] / "cli")
            if replan and kwargs["kind"] == "dispatch" and (packet.get("feedback") or {}).get("acceptance"):
                result.update(action="replan", instruction="Clarify implementation details before retrying")
            elif kwargs["kind"] == "replan":
                result.update(action="revise", reason="Clarify implementation details", new_tasks=[],
                              updates=[{"id": "greet", "description": "Implement and independently verify the greeting",
                                        "add_depends_on": []}])

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(ProcessTimeout):
            engine.start("Implement greet", self.plan())
        run_id = engine.run["id"]
        engine = self.engine(backend)

        def unavailable(event, payload, logs):
            if event == "pre_accept":
                raise HarnessError("acceptance policy unavailable")

        with patch.object(engine.hooks, "emit", side_effect=unavailable), self.assertRaisesRegex(HarnessError, "policy unavailable"):
            engine.resume(run_id)
        record = deepcopy(engine.run["review_history"]["greet"][-1])
        review_bytes = Path(record["artifact"]).read_bytes()
        self.assertEqual(record["review"]["verdict"], "approve")
        self.assertIn("pending_acceptance", engine.run)
        unchanged = worker_recovery_context(engine.run, engine.directory, "greet")["latest"]
        self.assertEqual(unchanged["verification"]["status"], "passed")
        self.assertEqual(unchanged["review"]["status"], "approved")
        with (engine.worktree / "hello.py").open("a") as stream:
            stream.write("# candidate changed after approval\n")
        if legacy:
            # Reproduce a saved old run: acceptance invalidation had feedback
            # evidence, but the corresponding history record lacked a marker.
            self.assertFalse(engine.accept_pending())
            engine.run["review_history"]["greet"][-1].pop("status")
            engine.run["pending_replan"] = {"stage": "pending", "trigger": {
                "task_id": "greet", "feedback": deepcopy(engine.run["feedback"])}}
            engine.save()
        resumed = self.engine(backend)
        with self.assertRaises(ProcessTimeout):
            resumed.resume(engine.run["id"])
        for call in backend.calls[-2:]:
            self.assertIn(call["role"], ("commander", "worker"))
            latest = call["packet"]["recovery"]["latest"]
            self.assertEqual(latest["worker"]["attempt"], 2)
            self.assertEqual(latest["verification"]["status"], "invalidated")
            self.assertEqual(latest["verification"]["artifact"], record["verification"])
            self.assertEqual(latest["review"]["status"], "invalidated")
            self.assertEqual(latest["review"]["verdict"], "approve")
            self.assertEqual(latest["review"]["artifact"], record["artifact"])
            if not replan:
                self.assertEqual(latest["review"]["invalidation_source"], "feedback.acceptance")
        self.assertEqual(resumed.run["review_history"]["greet"][-1], {**record, "status": "invalidated"})
        self.assertEqual(Path(record["artifact"]).read_bytes(), review_bytes)
        self.assertEqual(resumed.run["accepted"], {})
        self.assertEqual(resumed.run["attempts"], {"greet": 3})
        if replan:
            self.assertEqual(resumed.run["replan_attempts"], 1)
            self.assertNotIn("acceptance", resumed.run["feedback"])
            self.assertIn("retained_candidate", resumed.run["plan_history"][-1])

    def test_changed_pending_acceptance_invalidates_projected_approval(self):
        self.assert_changed_approval_projection()

    def test_invalidated_approval_stays_invalid_after_description_replan(self):
        self.assert_changed_approval_projection(replan=True)

    def test_legacy_invalidated_approval_is_normalized_before_pending_replan(self):
        self.assert_changed_approval_projection(replan=True, legacy=True)

    def test_legacy_missing_invocation_evidence_does_not_invent_attempt_time_or_success(self):
        engine, backend, original = self.interrupted_rework()
        legacy = deepcopy(engine.run)
        legacy["worker_recovery"]["greet"] = original
        for entry in legacy["dispatches"]:
            if entry["role"] == "worker":
                for key in ("invoked", "invoked_at", "worker_outcome"):
                    entry.pop(key, None)
        projected = worker_recovery_context(legacy, engine.directory, "greet")
        self.assertEqual(projected["source"]["dispatch_id"], original["dispatch_id"])
        self.assertNotIn("attempt", projected["source"])
        self.assertNotIn("invoked_at", projected["source"])
        self.assertNotIn("superseded_by", projected)
        self.assertNotIn("verification", projected["latest"])
        self.assertNotIn("review", projected["latest"])
        legacy["dispatches"] = []
        projected = worker_recovery_context(legacy, engine.directory, "greet")
        self.assertEqual(projected["relation"], "unknown")
        self.assertEqual(projected["source"], {"status": "unknown"})
        self.assertEqual({key: projected[key] for key in original}, original)

    def test_new_invocation_without_legal_return_does_not_supersede_failure(self):
        engine, backend, original = self.interrupted_rework()
        run = deepcopy(engine.run)
        run["worker_recovery"]["greet"] = original
        latest = next(entry for entry in reversed(run["dispatches"]) if entry["role"] == "worker")
        latest.update(status="running", process_state="running")
        latest.pop("worker_outcome", None)
        projected = worker_recovery_context(run, engine.directory, "greet")
        self.assertEqual(projected["relation"], "earlier_worker_failure")
        self.assertEqual(projected["latest"]["worker"]["status"], "running")
        self.assertNotIn("superseded_by", projected)
        self.assertNotIn("verification", projected["latest"])

    def test_projection_size_does_not_grow_with_repeated_review_history(self):
        engine, backend, original = self.interrupted_rework()
        run = deepcopy(engine.run)
        # Observe the latest returned worker before the next invocation started.
        run["dispatches"] = [entry for entry in run["dispatches"] if entry["id"] != run["worker_recovery"]["greet"]["dispatch_id"]]
        run["attempts"]["greet"] = 2
        run["worker_recovery"]["greet"] = original
        before = worker_recovery_context(run, engine.directory, "greet")
        run["review_history"]["greet"] *= 100
        after = worker_recovery_context(run, engine.directory, "greet")
        self.assertEqual(after, before)
        self.assertLess(len(dump(after)) - len(dump(original)), 2500)
