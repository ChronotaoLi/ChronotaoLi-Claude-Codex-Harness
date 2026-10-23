import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, ProcessTimeout, read_json, run_process, write_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend, finding


class FinalStageRecoveryTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def final_checks(self, run):
        return [entry for entry in run["dispatches"]
                if entry["role"] == "verification" and entry["task_id"] == "final"]

    def stop_snapshot_write(self, engine, error):
        def stop(path, value):
            if Path(path).name == "snapshot.json" and "final" in Path(path).parts:
                raise error
            return write_json(path, value)

        with patch("harness.engine.write_json", side_effect=stop), self.assertRaises(type(error)):
            engine.start("Implement greet", self.plan())

    def test_snapshot_write_interruption_resumes_allocated_final_attempt(self):
        self.config.data["runtime"]["max_final_attempts"] = 1
        self.config.data["runtime"]["max_attempts"] = 1
        self.config.data["runtime"]["max_total_attempts"] = 1
        for error in (KeyboardInterrupt(), OSError("Storage temporarily unavailable")):
            with self.subTest(error=type(error).__name__):
                backend = ScriptedBackend()
                engine = self.engine(backend)
                self.stop_snapshot_write(engine, error)
                state = read_json(engine.directory / "state.json")
                self.assertEqual(state["final_attempts"], 1)
                self.assertEqual(len(state["final_history"]), 1)
                record = state["final_history"][0]
                self.assertEqual(record["status"], "running")
                self.assertEqual(record["phase"], "verifying")
                self.assertFalse(Path(record["snapshot_artifact"]).exists())
                self.assertEqual(self.final_checks(state), [])
                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["final_attempts"], 1)
                self.assertEqual(result["total_attempts"], 1)
                self.assertEqual(len(result["final_history"]), 1)
                self.assertEqual(len(self.final_checks(result)), 1)
                self.assertTrue(Path(record["snapshot_artifact"]).exists())
                self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)
                self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 1)

    def test_resume_does_not_overwrite_corrupt_or_mismatched_final_snapshot_evidence(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        self.stop_snapshot_write(engine, KeyboardInterrupt())
        artifact = Path(engine.run["final_history"][0]["snapshot_artifact"])
        artifact.parent.mkdir(parents=True, exist_ok=True)
        calls = list(backend.calls)
        for contents in ("interrupted JSON", "{}"):
            with self.subTest(contents=contents):
                artifact.write_text(contents)
                resumed = self.engine(backend)
                with self.assertRaisesRegex(HarnessError, "整体快照证据"):
                    resumed.resume(engine.run["id"])
                self.assertEqual(artifact.read_text(), contents)
                self.assertEqual(resumed.run["final_attempts"], 1)
                self.assertEqual(self.final_checks(resumed.run), [])
                self.assertEqual(backend.calls, calls)

    def test_reviewer_cli_failure_resumes_review_without_rechecking_or_spending_final_attempt(self):
        def fail_once(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and sum(call["kind"] == "final_review" for call in backend.calls) == 1:
                raise HarnessError("Reviewer service unavailable")

        self.config.data["runtime"]["max_final_attempts"] = 1
        self.config.data["runtime"]["max_attempts"] = 1
        backend = ScriptedBackend(fail_once)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "service unavailable"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["final_history"][-1]["phase"], "reviewing")
        checks = self.final_checks(engine.run)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_attempts"], 1)
        self.assertEqual(result["total_attempts"], 1)
        self.assertEqual(self.final_checks(result), checks)
        reviews = [entry["id"] for entry in result["dispatches"] if entry.get("kind") == "final_review"]
        self.assertEqual(len(reviews), 2)
        self.assertEqual(len(set(reviews)), 2)
        packet = [call["packet"] for call in backend.calls if call["kind"] == "final_review"][-1]
        self.assertEqual(packet["prior_reviews"], [])

    def test_interrupted_verification_keeps_attempt_and_old_evidence(self):
        self.config.data["runtime"]["max_final_attempts"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        verify = engine.verify

        def interrupt_after_checks(*args, **kwargs):
            result = verify(*args, **kwargs)
            if kwargs.get("whole_run"):
                raise KeyboardInterrupt()
            return result

        with patch.object(engine, "verify", side_effect=interrupt_after_checks), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        record = engine.run["final_history"][-1]
        self.assertEqual(record["phase"], "verifying")
        old_folder = Path(record["verification_rounds"][0]["artifact_dir"])
        old_evidence = {path.name: path.read_bytes() for path in old_folder.iterdir() if path.is_file()}
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_attempts"], 1)
        self.assertEqual(result["total_attempts"], 1)
        self.assertEqual(len(self.final_checks(result)), 2)
        record = result["final_history"][-1]
        self.assertEqual(len(record["verification_rounds"]), 2)
        self.assertNotEqual(record["verification_rounds"][0]["artifact_dir"], record["verification_rounds"][1]["artifact_dir"])
        self.assertEqual(old_evidence, {path.name: path.read_bytes() for path in old_folder.iterdir() if path.is_file()})

    def fail_final_check(self, *, timed_out):
        def fail(argv, cwd, log, **kwargs):
            if "final" in Path(log).parts:
                Path(log).with_suffix(".stderr").write_text("Final check did not finish" if timed_out else "Observed failure")
                if timed_out:
                    raise ProcessTimeout(kwargs["timeout"], Path(log))
                return 1
            return run_process(argv, cwd, log, **kwargs)
        return fail

    def test_timeout_resumes_same_snapshot_with_new_bounded_attempt_and_preserves_failure(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with patch("harness.engine.run_process", side_effect=self.fail_final_check(timed_out=True)), self.assertRaisesRegex(HarnessError, "整体验证超时"):
            engine.start("Implement greet", self.plan())
        failed = engine.run["final_history"][-1]
        self.assertEqual(failed["status"], "verification_failed")
        self.assertEqual(failed["failure_kind"], "timeout")
        self.assertFalse(any(call["kind"] in ("final_review", "replan") for call in backend.calls))
        old_artifact = Path(failed["artifact"]).read_bytes()
        self.config.data["runtime"]["verify_timeout_seconds"] += 1
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_attempts"], 2)
        self.assertEqual([record["status"] for record in result["final_history"]], ["verification_failed", "approved"])
        self.assertEqual(result["final_history"][0]["snapshot_digest"], result["final_history"][1]["snapshot_digest"])
        self.assertEqual(Path(failed["artifact"]).read_bytes(), old_artifact)
        final_packet = next(call["packet"] for call in backend.calls if call["kind"] == "final_review")
        self.assertTrue(final_packet["prior_reviews"][0]["verification_failures"][0]["timed_out"])

    def test_failed_check_requires_explicit_reverify_and_keeps_failure_visible(self):
        self.config.data["runtime"]["max_replans"] = 0
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with patch("harness.engine.run_process", side_effect=self.fail_final_check(timed_out=False)), self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        checks = self.final_checks(engine.run)
        with self.assertRaisesRegex(HarnessError, "--reverify"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(self.final_checks(read_json(engine.directory / "state.json")), checks)
        result = self.engine(backend).resume(engine.run["id"], reverify=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_attempts"], 2)
        self.assertEqual(result["final_history"][0]["verification_failures"][0]["exit_code"], 1)
        final_packet = next(call["packet"] for call in backend.calls if call["kind"] == "final_review")
        self.assertEqual(final_packet["prior_reviews"][0]["status"], "verification_failed")

    def test_timeout_retries_cannot_exceed_final_attempt_budget(self):
        self.config.data["runtime"]["max_final_attempts"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with patch("harness.engine.run_process", side_effect=self.fail_final_check(timed_out=True)), self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        dispatches = engine.run["dispatches"]
        with self.assertRaisesRegex(HarnessError, "整体审阅已达尝试次数"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(read_json(engine.directory / "state.json")["dispatches"], dispatches)

    def test_nonzero_final_verification_can_require_a_corrective_task(self):
        def corrective(backend, kwargs, packet, result):
            if kwargs["kind"] == "replan":
                task = deepcopy(packet["original_tasks"][0])
                task.update(id="correct", title="Correct integration failure", depends_on=["greet"])
                result.update(action="revise", reason="Fix the failing integration check", new_tasks=[task], updates=[])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# integration correction\n")

        failed = False

        def fail_first_final(argv, cwd, log, **kwargs):
            nonlocal failed
            if "final" in Path(log).parts and not failed:
                failed = True
                return 1
            return run_process(argv, cwd, log, **kwargs)

        backend = ScriptedBackend(corrective)
        engine = self.engine(backend)
        with patch("harness.engine.run_process", side_effect=fail_first_final):
            result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(result["accepted"]), {"greet", "correct"})
        self.assertEqual(result["final_attempts"], 2)
        self.assertEqual([record["status"] for record in result["final_history"]], ["verification_failed", "approved"])
        self.assertNotEqual(result["final_history"][0]["head"], result["final_history"][1]["head"])
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 1)

    def test_saved_approval_survives_crash_before_run_completion_without_more_calls(self):
        self.config.data["runtime"]["max_final_attempts"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        save = engine.save
        interrupted = False

        def interrupt_after_approval():
            nonlocal interrupted
            save()
            if not interrupted and engine.run["final_history"] and engine.run["final_history"][-1]["status"] == "approved":
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(engine, "save", side_effect=interrupt_after_approval), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        calls = list(backend.calls)
        checks = self.final_checks(engine.run)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_attempts"], 1)
        self.assertEqual(backend.calls, calls)
        self.assertEqual(self.final_checks(result), checks)

    def test_saved_rejection_resumes_pending_replan_after_crash_before_request(self):
        def corrective(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and sum(call["kind"] == "final_review" for call in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding()])
            elif kwargs["kind"] == "replan":
                task = deepcopy(packet["original_tasks"][0])
                task.update(id="correct", title="Correct final finding", depends_on=["greet"])
                result.update(action="revise", reason="Address the final defect", new_tasks=[task], updates=[])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# independently reviewed correction\n")

        backend = ScriptedBackend(corrective)
        engine = self.engine(backend)
        save = engine.save
        interrupted = False

        def interrupt_after_rejection():
            nonlocal interrupted
            save()
            if not interrupted and engine.run["final_history"] and engine.run["final_history"][-1]["status"] == "rejected":
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(engine, "save", side_effect=interrupt_after_rejection), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["pending_replan"]["stage"], "pending")
        self.assertFalse(any(call["kind"] == "replan" for call in backend.calls))
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(result["accepted"]), {"greet", "correct"})
        self.assertEqual(result["final_attempts"], 2)
        self.assertEqual([record["status"] for record in result["final_history"]], ["rejected", "approved"])
        self.assertNotEqual(result["final_history"][0]["head"], result["final_history"][1]["head"])
        self.assertEqual(sum(call["kind"] == "replan" for call in backend.calls), 1)

    def test_reverify_cancels_unvalidated_replan_after_verification_failure(self):
        def unavailable_commander(backend, kwargs, packet, result):
            if kwargs["kind"] == "replan":
                raise HarnessError("Commander service unavailable")

        backend = ScriptedBackend(unavailable_commander)
        engine = self.engine(backend)
        with patch("harness.engine.run_process", side_effect=self.fail_final_check(timed_out=False)), self.assertRaisesRegex(HarnessError, "Commander service"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["pending_replan"]["stage"], "request")
        result = self.engine(backend).resume(engine.run["id"], reverify=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_attempts"], 2)
        self.assertEqual(sum(call["kind"] == "replan" for call in backend.calls), 1)
        self.assertNotIn("pending_replan", result)
        self.assertEqual(result["final_history"][0]["status"], "verification_failed")

    def test_reverify_budget_refusal_preserves_replan_and_both_resume_routes(self):
        for reverify in (False, True):
            with self.subTest(reverify_after_budget_increase=reverify):
                self.config.data["runtime"]["max_final_attempts"] = 1
                backend = ScriptedBackend()
                engine = self.engine(backend)
                with patch("harness.engine.run_process", side_effect=self.fail_final_check(timed_out=False)), \
                        self.assertRaisesRegex(HarnessError, "整体审阅已达尝试次数"):
                    engine.start("Implement greet", self.plan())
                before = read_json(engine.directory / "state.json")
                self.assertEqual(before["pending_replan"]["stage"], "pending")
                failed_artifact = Path(before["final_history"][0]["artifact"])
                failed_bytes = failed_artifact.read_bytes()
                calls = deepcopy(backend.calls)
                events_path = engine.directory / "events.jsonl"
                event_count = len(events_path.read_text().splitlines())

                with self.assertRaisesRegex(HarnessError, "整体审阅已达尝试次数"):
                    self.engine(backend).resume(before["id"], reverify=True)
                after = read_json(engine.directory / "state.json")
                self.assertIn("pending_replan", after)
                for field in ("pending_replan", "commander_receipts", "final_history", "dispatches",
                              "final_attempts", "agent_dispatches", "total_attempts", "replan_attempts"):
                    self.assertEqual(after[field], before[field], field)
                self.assertEqual(after["final_attempts"], 1)
                self.assertEqual(backend.calls, calls)
                self.assertEqual(failed_artifact.read_bytes(), failed_bytes)
                events = [json.loads(line)["event"] for line in events_path.read_text().splitlines()[event_count:]]
                self.assertNotIn("unvalidated_replan_cancelled_for_reverification", events)
                self.assertNotIn("final_reverification_requested", events)

                self.config.data["runtime"]["max_final_attempts"] = 2
                resumed = self.engine(backend)
                if reverify:
                    result = resumed.resume(before["id"], reverify=True)
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(result["final_attempts"], 2)
                    self.assertEqual([record["status"] for record in result["final_history"]],
                                     ["verification_failed", "approved"])
                    self.assertEqual(sum(call["kind"] == "replan" for call in backend.calls), 0)
                    events = [json.loads(line)["event"] for line in events_path.read_text().splitlines()[event_count:]]
                    self.assertEqual(events.count("unvalidated_replan_cancelled_for_reverification"), 1)
                    self.assertEqual(events.count("final_reverification_requested"), 1)
                else:
                    with self.assertRaisesRegex(HarnessError, "指挥停止重新规划"):
                        resumed.resume(before["id"])
                    self.assertEqual(sum(call["kind"] == "replan" for call in backend.calls), 1)
                    self.assertEqual(resumed.run["final_attempts"], 1)
                    self.assertEqual(self.final_checks(resumed.run), self.final_checks(before))
                self.assertEqual(resumed.run["final_history"][0], before["final_history"][0])
                self.assertEqual(failed_artifact.read_bytes(), failed_bytes)

    def test_reverify_budget_refusal_without_pending_replan_does_not_record_request(self):
        self.config.data["runtime"]["max_final_attempts"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with patch("harness.engine.run_process", side_effect=self.fail_final_check(timed_out=False)), \
                self.assertRaisesRegex(HarnessError, "整体审阅已达尝试次数"):
            engine.start("Implement greet", self.plan())
        state_path = engine.directory / "state.json"
        before = read_json(state_path)
        # Older v8 code could cancel this request before refusing the reverify budget.
        before.pop("pending_replan")
        write_json(state_path, before)
        failed_artifact = Path(before["final_history"][0]["artifact"])
        failed_bytes = failed_artifact.read_bytes()
        calls = deepcopy(backend.calls)
        events_path = engine.directory / "events.jsonl"
        event_count = len(events_path.read_text().splitlines())

        with self.assertRaisesRegex(HarnessError, "整体审阅已达尝试次数"):
            self.engine(backend).resume(before["id"], reverify=True)
        after = read_json(state_path)
        for field in ("final_history", "dispatches", "final_attempts", "agent_dispatches", "total_attempts"):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(backend.calls, calls)
        self.assertEqual(failed_artifact.read_bytes(), failed_bytes)
        events = [json.loads(line)["event"] for line in events_path.read_text().splitlines()[event_count:]]
        self.assertNotIn("final_reverification_requested", events)
        self.assertNotIn("unvalidated_replan_cancelled_for_reverification", events)

        self.config.data["runtime"]["max_final_attempts"] = 2
        result = self.engine(backend).resume(before["id"], reverify=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_attempts"], 2)
        self.assertEqual(result["final_history"][0], before["final_history"][0])
        self.assertEqual(failed_artifact.read_bytes(), failed_bytes)
        self.assertEqual(sum(call["kind"] == "replan" for call in backend.calls), 0)
        events = [json.loads(line)["event"] for line in events_path.read_text().splitlines()[event_count:]]
        self.assertEqual(events.count("final_reverification_requested"), 1)

    def test_reverify_does_not_carry_over_to_failure_after_a_returned_revision(self):
        self.config.data["runtime"].update(max_replans=3, max_final_attempts=5)

        def corrective(backend, kwargs, packet, result):
            if kwargs["kind"] == "replan":
                number = sum(call["kind"] == "replan" for call in backend.calls)
                if number <= 2:
                    task = deepcopy(packet["original_tasks"][0])
                    task.update(id=f"c{number}", depends_on=["greet" if number == 1 else "c1"])
                    result.update(action="revise", reason="Fix the final check", new_tasks=[task], updates=[])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] in ("c1", "c2"):
                # DemoBackend writes the same implementation before each callback.
                # c1 changes the accepted snapshot; c2 then leaves it unchanged.
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# independently reviewed correction\n")

        backend = ScriptedBackend(corrective)
        engine = self.engine(backend)
        emit = engine.hooks.emit

        def interrupt_returned_revision(name, payload, folder):
            if name == "post_agent" and payload.get("kind") == "replan":
                raise KeyboardInterrupt()
            return emit(name, payload, folder)

        with patch("harness.engine.run_process", side_effect=self.fail_final_check(timed_out=False)):
            with patch.object(engine.hooks, "emit", side_effect=interrupt_returned_revision), self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", self.plan())
            self.assertEqual(engine.run["pending_replan"]["stage"], "request")
            receipt = engine.run["commander_receipts"]["commander:replan:replanning"]
            self.assertTrue(Path(receipt["artifact"]).is_file())
            self.assertEqual(engine.run["final_attempts"], 1)
            resumed = self.engine(backend)
            with self.assertRaisesRegex(HarnessError, "停止重新规划"):
                resumed.resume(engine.run["id"], reverify=True)

        run = resumed.run
        self.assertEqual(run["final_attempts"], 2)
        self.assertEqual(len(self.final_checks(run)), 2)
        self.assertEqual([record["status"] for record in run["final_history"]], ["verification_failed"] * 2)
        self.assertNotEqual(run["final_history"][0]["snapshot_digest"], run["final_history"][1]["snapshot_digest"])
        self.assertEqual(run["accepted"]["c1"]["commit"], run["accepted"]["c2"]["commit"])
        self.assertEqual(run["replan_attempts"], 3)
        self.assertEqual(sum(call["kind"] == "replan" for call in backend.calls), 3)
        trigger = read_json(engine.directory / "plans" / "replan-3.json")["trigger"]
        self.assertEqual(trigger["unchanged_corrections"], ["c2"])
        self.assertEqual(trigger["final_review"]["attempt"], 2)
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        self.assertFalse(any(event["event"] == "final_reverification_requested" for event in events))

    def assert_reverify_consumed_by_verification_recovery(self, *, timed_out):
        self.config.data["runtime"].update(max_replans=3, max_final_attempts=5)

        def noop_correction(backend, kwargs, packet, result):
            if kwargs["kind"] == "replan" and sum(call["kind"] == "replan" for call in backend.calls) == 1:
                task = deepcopy(packet["original_tasks"][0])
                task.update(id="correct", depends_on=["greet"])
                result.update(action="revise", reason="Fix the final check", new_tasks=[task], updates=[])

        backend = ScriptedBackend(noop_correction)
        engine = self.engine(backend)
        if timed_out:
            with patch("harness.engine.run_process", side_effect=self.fail_final_check(timed_out=True)), self.assertRaisesRegex(HarnessError, "整体验证超时"):
                engine.start("Implement greet", self.plan())
        else:
            self.stop_snapshot_write(engine, KeyboardInterrupt())
            self.assertEqual(engine.run["final_history"][0]["status"], "running")
        resumed = self.engine(backend)
        with patch("harness.engine.run_process", side_effect=self.fail_final_check(timed_out=False)), self.assertRaisesRegex(HarnessError, "停止重新规划"):
            resumed.resume(engine.run["id"], reverify=True)

        run = resumed.run
        expected_attempts = 2 if timed_out else 1
        self.assertEqual(run["final_attempts"], expected_attempts)
        self.assertEqual(len(self.final_checks(run)), expected_attempts)
        self.assertEqual([record["status"] for record in run["final_history"]], ["verification_failed"] * expected_attempts)
        self.assertEqual(run["final_history"][-1]["failure_kind"], "exit_code")
        self.assertEqual(run["accepted"]["greet"]["commit"], run["accepted"]["correct"]["commit"])
        self.assertEqual(sum(call["kind"] == "replan" for call in backend.calls), 2)
        trigger = read_json(engine.directory / "plans" / "replan-2.json")["trigger"]
        self.assertEqual(trigger["unchanged_corrections"], ["correct"])
        self.assertEqual(trigger["final_review"]["attempt"], expected_attempts)
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        self.assertFalse(any(event["event"] == "final_reverification_requested" for event in events))

    def test_reverify_does_not_carry_over_after_resuming_running_verification(self):
        self.assert_reverify_consumed_by_verification_recovery(timed_out=False)

    def test_reverify_does_not_carry_over_after_timeout_recovery(self):
        self.assert_reverify_consumed_by_verification_recovery(timed_out=True)

    def test_reverify_cannot_cancel_pending_replan_for_review_rejection(self):
        def rejected_and_unavailable(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result.update(verdict="request_changes", findings=[finding()])
            elif kwargs["kind"] == "replan" and sum(call["kind"] == "replan" for call in backend.calls) == 1:
                raise HarnessError("Commander service unavailable")

        backend = ScriptedBackend(rejected_and_unavailable)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "Commander service"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["pending_replan"]["stage"], "request")
        resumed = self.engine(backend)
        with self.assertRaises(HarnessError):
            resumed.resume(engine.run["id"], reverify=True)
        self.assertEqual(sum(call["kind"] == "replan" for call in backend.calls), 2)
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 1)
        self.assertEqual(resumed.run["final_attempts"], 1)
        self.assertEqual(resumed.run["final_history"][-1]["status"], "rejected")

    def test_returned_rejection_replays_validation_after_hook_interrupt_without_reroll(self):
        self.config.data["runtime"]["max_replans"] = 0

        def reject_final(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result.update(verdict="request_changes", findings=[finding()])

        backend = ScriptedBackend(reject_final)
        engine = self.engine(backend)
        emit = engine.hooks.emit

        def interrupt_post_agent(name, payload, folder):
            if name == "post_agent" and payload.get("kind") == "final_review":
                raise KeyboardInterrupt()
            return emit(name, payload, folder)

        with patch.object(engine.hooks, "emit", side_effect=interrupt_post_agent), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        calls = list(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaises(HarnessError):
            resumed.resume(engine.run["id"])
        self.assertEqual(resumed.run["final_history"][-1]["status"], "rejected")
        self.assertEqual(resumed.run["final_attempts"], 1)
        self.assertEqual(backend.calls, calls)
        with self.assertRaisesRegex(HarnessError, "同一代码快照"):
            self.engine(backend).resume(engine.run["id"], reverify=True)
        self.assertEqual(backend.calls, calls)
