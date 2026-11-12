import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from harness.process_runtime import ProcessOutcome
from harness.util import HarnessError, ProcessTimeout, read_json, run_process, write_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend, decided, finding, revised, trigger_kind


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

        with patch("harness.finalization.write_json", side_effect=stop), self.assertRaises(type(error)):
            engine.start("Implement greet", self.plan())

    def test_snapshot_write_interruption_resumes_allocated_final_attempt(self):
        self.config.data["runtime"]["max_attempts"] = 1
        for error in (KeyboardInterrupt(), OSError("Storage temporarily unavailable")):
            with self.subTest(error=type(error).__name__):
                backend = ScriptedBackend()
                engine = self.engine(backend)
                self.stop_snapshot_write(engine, error)
                state = read_json(engine.directory / "state.json")
                self.assertEqual(len(state["final_history"]), 1)
                record = state["final_history"][0]
                self.assertEqual(record["status"], "running")
                self.assertEqual(record["phase"], "verifying")
                self.assertFalse(Path(record["snapshot_artifact"]).exists())
                self.assertEqual(self.final_checks(state), [])
                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(sum(result["attempts"].values()), 1)
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
                self.assertEqual(len(resumed.run["final_history"]), 1)
                self.assertEqual(self.final_checks(resumed.run), [])
                self.assertEqual(backend.calls, calls)

    def test_reviewer_cli_failure_resumes_review_without_rechecking_or_spending_final_attempt(self):
        def fail_once(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and sum(call["kind"] == "final_review" for call in backend.calls) == 1:
                raise HarnessError("Reviewer service unavailable")

        self.config.data["runtime"]["max_attempts"] = 1
        backend = ScriptedBackend(fail_once)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "service unavailable"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["final_history"][-1]["phase"], "reviewing")
        checks = self.final_checks(engine.run)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["final_history"]), 1)
        self.assertEqual(sum(result["attempts"].values()), 1)
        self.assertEqual(self.final_checks(result), checks)
        reviews = [entry["id"] for entry in result["dispatches"] if entry.get("kind") == "final_review"]
        self.assertEqual(len(reviews), 2)
        self.assertEqual(len(set(reviews)), 2)
        packet = [call["packet"] for call in backend.calls if call["kind"] == "final_review"][-1]
        self.assertEqual(packet["prior_reviews"], [])

    def test_interrupted_verification_keeps_attempt_and_old_evidence(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        verify = engine.services.verification.verify

        def interrupt_after_checks(*args, **kwargs):
            result = verify(*args, **kwargs)
            if kwargs.get("whole_run"):
                raise KeyboardInterrupt()
            return result

        with patch.object(engine.services.verification, "verify", side_effect=interrupt_after_checks), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        record = engine.run["final_history"][-1]
        self.assertEqual(record["phase"], "verifying")
        old_folder = Path(record["verification_rounds"][0]["artifact_dir"])
        old_evidence = {path.name: path.read_bytes() for path in old_folder.iterdir() if path.is_file()}
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["final_history"]), 1)
        self.assertEqual(sum(result["attempts"].values()), 1)
        self.assertEqual(len(self.final_checks(result)), 2)
        record = result["final_history"][-1]
        self.assertEqual(len(record["verification_rounds"]), 2)
        self.assertNotEqual(record["verification_rounds"][0]["artifact_dir"], record["verification_rounds"][1]["artifact_dir"])
        self.assertEqual(old_evidence, {path.name: path.read_bytes() for path in old_folder.iterdir() if path.is_file()})

    def failed_check_outcome(self, log, callbacks, *, timed_out=False):
        # This fixture models a started child whose exit and cleanup are known.
        # Returning an integer alone leaves the strict process gate unresolved.
        callbacks["on_start"](123)
        Path(log).with_suffix(".stdout").write_text("")
        Path(log).with_suffix(".stderr").write_text(
            "Final check did not finish" if timed_out else "Observed failure")
        outcome = ProcessOutcome(started=True, startup_state="started", pid=123, pgid=123,
                                 exit_code=-15 if timed_out else 1, exit_confirmed=True,
                                 termination_reason="timeout" if timed_out else "normal")
        callbacks["on_outcome"](outcome)
        return outcome

    def fail_final_check(self, *, timed_out):
        def fail(argv, cwd, log, **kwargs):
            if "final" in Path(log).parts:
                outcome = self.failed_check_outcome(log, kwargs, timed_out=timed_out)
                if timed_out:
                    error = ProcessTimeout(kwargs["timeout"], Path(log))
                    error.outcome = outcome
                    raise error
                return outcome.exit_code
            return run_process(argv, cwd, log, **kwargs)
        return fail

    def test_timeout_resumes_same_snapshot_with_new_bounded_attempt_and_preserves_failure(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with patch("harness.verification.run_process", side_effect=self.fail_final_check(timed_out=True)), self.assertRaisesRegex(HarnessError, "整体验证超时"):
            engine.start("Implement greet", self.plan())
        failed = engine.run["final_history"][-1]
        self.assertEqual(failed["status"], "verification_failed")
        self.assertEqual(failed["failure_kind"], "timeout")
        self.assertFalse(any(call["kind"] in ("final_review", "decide") for call in backend.calls))
        old_artifact = Path(failed["artifact"]).read_bytes()
        self.config.data["runtime"]["verify_timeout_seconds"] += 1
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual([record["status"] for record in result["final_history"]], ["verification_failed", "approved"])
        self.assertEqual(result["final_history"][0]["snapshot_digest"], result["final_history"][1]["snapshot_digest"])
        self.assertEqual(Path(failed["artifact"]).read_bytes(), old_artifact)
        final_packet = next(call["packet"] for call in backend.calls if call["kind"] == "final_review")
        self.assertTrue(final_packet["prior_reviews"][0]["verification_failures"][0]["timed_out"])

    def test_failed_check_needs_a_decision_unless_explicitly_reverified(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        # The demo commander asks after a non-timeout whole-run failure.
        with patch("harness.verification.run_process", side_effect=self.fail_final_check(timed_out=False)), \
                self.assertRaisesRegex(HarnessError, "等待操作者决定：task_id=final"):
            engine.start("Implement greet", self.plan())
        checks = self.final_checks(engine.run)
        decide = [call for call in backend.calls if call["kind"] == "decide"]
        self.assertEqual(len(decide), 1)
        self.assertEqual(decide[0]["packet"]["trigger"]["kind"], "final")
        self.assertEqual(decide[0]["packet"]["trigger"]["final_review"]["status"], "verification_failed")
        calls = list(backend.calls)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(self.final_checks(read_json(engine.directory / "state.json")), checks)
        self.assertEqual(backend.calls, calls)
        self.assertFalse(any(call["kind"] == "final_review" for call in backend.calls))
        self.assertEqual(engine.run["final_history"][0]["verification_failures"][0]["exit_code"], 1)

    def test_nonzero_final_verification_can_require_a_corrective_task(self):
        def corrective(backend, kwargs, packet, result):
            if trigger_kind(kwargs, packet) == "final":
                task = deepcopy(packet["original_tasks"][0])
                task.update(id="correct", title="Correct integration failure", depends_on=["greet"])
                revised(result, packet, "Fix the failing integration check", new_tasks=[task])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# integration correction\n")

        failed = False

        def fail_first_final(argv, cwd, log, **kwargs):
            nonlocal failed
            if "final" in Path(log).parts and not failed:
                failed = True
                return self.failed_check_outcome(log, kwargs).exit_code
            return run_process(argv, cwd, log, **kwargs)

        backend = ScriptedBackend(corrective)
        engine = self.engine(backend)
        with patch("harness.verification.run_process", side_effect=fail_first_final):
            result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(result["accepted"]), {"greet", "correct"})
        self.assertEqual([record["status"] for record in result["final_history"]], ["verification_failed", "approved"])
        self.assertNotEqual(result["final_history"][0]["head"], result["final_history"][1]["head"])
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 1)

    def test_saved_approval_survives_crash_before_run_completion_without_more_calls(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        save = engine.context.save
        interrupted = False

        def interrupt_after_approval():
            nonlocal interrupted
            save()
            if not interrupted and engine.run["final_history"] and engine.run["final_history"][-1]["status"] == "approved":
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(engine.context, "save", side_effect=interrupt_after_approval), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        calls = list(backend.calls)
        checks = self.final_checks(engine.run)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["final_history"]), 1)
        self.assertEqual(backend.calls, calls)
        self.assertEqual(self.final_checks(result), checks)

    def test_saved_rejection_derives_the_decision_after_crash_before_request(self):
        def corrective(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and sum(call["kind"] == "final_review" for call in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding()])
            elif trigger_kind(kwargs, packet) == "final":
                task = deepcopy(packet["original_tasks"][0])
                task.update(id="correct", title="Correct final finding", depends_on=["greet"])
                revised(result, packet, "Address the final defect", new_tasks=[task])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# independently reviewed correction\n")

        backend = ScriptedBackend(corrective)
        engine = self.engine(backend)
        save = engine.context.save
        interrupted = False

        def interrupt_after_rejection():
            nonlocal interrupted
            save()
            if not interrupted and engine.run["final_history"] and engine.run["final_history"][-1]["status"] == "rejected":
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(engine.context, "save", side_effect=interrupt_after_rejection), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        self.assertFalse(any(call["role"] == "commander" for call in backend.calls))
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(result["accepted"]), {"greet", "correct"})
        self.assertEqual([record["status"] for record in result["final_history"]], ["rejected", "approved"])
        self.assertNotEqual(result["final_history"][0]["head"], result["final_history"][1]["head"])
        self.assertEqual(sum(call["kind"] == "decide" for call in backend.calls), 1)
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 2)

    def test_reverify_discards_unreturned_decide_after_verification_failure(self):
        def unavailable_commander(backend, kwargs, packet, result):
            if kwargs["role"] == "commander":
                raise HarnessError("Commander service unavailable")

        backend = ScriptedBackend(unavailable_commander)
        engine = self.engine(backend)
        with patch("harness.verification.run_process", side_effect=self.fail_final_check(timed_out=False)), self.assertRaisesRegex(HarnessError, "Commander service"):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertIn("commander:decide:run", saved["commander_receipts"])
        spent = saved["agent_dispatches"]
        result = self.engine(backend).resume(engine.run["id"], reverify=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([record["status"] for record in result["final_history"]], ["verification_failed", "approved"])
        self.assertEqual(result["final_history"][-1]["attempt"], len(result["final_history"]))
        self.assertEqual(sum(call["kind"] == "decide" for call in backend.calls), 1)
        self.assertNotIn("commander:decide:run", result["commander_receipts"])
        # The discarded call keeps its spent budget and its dispatch entry.
        self.assertEqual(result["agent_dispatches"], spent + 1)
        self.assertEqual(sum(d["role"] == "commander" for d in result["dispatches"]), 1)

    def test_reverify_refuses_a_returned_decide_before_any_write(self):
        def corrective(backend, kwargs, packet, result):
            if trigger_kind(kwargs, packet) == "final" and not packet["trigger"]["accepted_since"] \
                    and sum(call["kind"] == "decide" for call in backend.calls) == 1:
                task = deepcopy(packet["original_tasks"][0])
                task.update(id="c1", depends_on=["greet"])
                revised(result, packet, "Fix the final check", new_tasks=[task])
            elif trigger_kind(kwargs, packet) == "final":
                decided(result, "ask", reason="The check still fails; is the environment at fault?")
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "c1":
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# independently reviewed correction\n")

        backend = ScriptedBackend(corrective)
        engine = self.engine(backend)
        emit = engine.hooks.emit

        def interrupt_returned_decide(name, payload, folder):
            if name == "post_agent" and payload.get("kind") == "decide":
                raise KeyboardInterrupt()
            return emit(name, payload, folder)

        with patch("harness.verification.run_process", side_effect=self.fail_final_check(timed_out=False)):
            with patch.object(engine.hooks, "emit", side_effect=interrupt_returned_decide), self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", self.plan())
            receipt = engine.run["commander_receipts"]["commander:decide:run"]
            self.assertTrue(Path(receipt["artifact"]).is_file())
            state = engine.directory / "state.json"
            before = state.read_bytes()
            events = (engine.directory / "events.jsonl").read_bytes()
            with self.assertRaisesRegex(HarnessError, "--reverify 不能用于已返回、尚未处理的指挥决定"):
                self.engine(backend).resume(engine.run["id"], reverify=True)
            self.assertEqual(state.read_bytes(), before)
            self.assertEqual((engine.directory / "events.jsonl").read_bytes(), events)
            # An ordinary resume lands the same returned revision without another decide.
            resumed = self.engine(backend)
            with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
                resumed.resume(engine.run["id"])

        run = resumed.run
        self.assertEqual(sum(call["kind"] == "decide" for call in backend.calls), 2)
        self.assertEqual(len(run["plan_history"]), 2)
        self.assertEqual([record["status"] for record in run["final_history"]], ["verification_failed"] * 2)
        self.assertNotEqual(run["final_history"][0]["snapshot_digest"], run["final_history"][1]["snapshot_digest"])
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        self.assertFalse(any(event["event"] == "final_reverification_requested" for event in events))

    def assert_reverify_consumed_by_verification_recovery(self, *, timed_out):
        def noop_correction(backend, kwargs, packet, result):
            if trigger_kind(kwargs, packet) == "final" and not packet["trigger"]["accepted_since"]:
                task = deepcopy(packet["original_tasks"][0])
                task.update(id="correct", depends_on=["greet"])
                revised(result, packet, "Fix the final check", new_tasks=[task])
            elif trigger_kind(kwargs, packet) == "final":
                decided(result, "ask", reason="The correction changed nothing; what now?")

        backend = ScriptedBackend(noop_correction)
        engine = self.engine(backend)
        if timed_out:
            with patch("harness.verification.run_process", side_effect=self.fail_final_check(timed_out=True)), self.assertRaisesRegex(HarnessError, "整体验证超时"):
                engine.start("Implement greet", self.plan())
        else:
            self.stop_snapshot_write(engine, KeyboardInterrupt())
            self.assertEqual(engine.run["final_history"][0]["status"], "running")
        resumed = self.engine(backend)
        with patch("harness.verification.run_process", side_effect=self.fail_final_check(timed_out=False)), self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            resumed.resume(engine.run["id"], reverify=True)

        run = resumed.run
        expected_attempts = 2 if timed_out else 1
        self.assertEqual(len(run["final_history"]), expected_attempts)
        self.assertEqual(len(self.final_checks(run)), expected_attempts)
        self.assertEqual([record["status"] for record in run["final_history"]], ["verification_failed"] * expected_attempts)
        self.assertEqual(run["final_history"][-1]["failure_kind"], "exit_code")
        self.assertEqual(run["accepted"]["greet"]["commit"], run["accepted"]["correct"]["commit"])
        decides = [call for call in backend.calls if call["kind"] == "decide"]
        self.assertEqual(len(decides), 2)
        trigger = decides[1]["packet"]["trigger"]
        self.assertEqual(trigger["accepted_since"], ["correct"])
        self.assertEqual(trigger["final_review"]["attempt"], expected_attempts)
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        self.assertFalse(any(event["event"] == "final_reverification_requested" for event in events))

    def test_reverify_does_not_carry_over_after_resuming_running_verification(self):
        self.assert_reverify_consumed_by_verification_recovery(timed_out=False)

    def test_reverify_does_not_carry_over_after_timeout_recovery(self):
        self.assert_reverify_consumed_by_verification_recovery(timed_out=True)

    def test_reverify_cannot_skip_the_decision_after_review_rejection(self):
        def rejected_and_unavailable(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result.update(verdict="request_changes", findings=[finding()])
            elif kwargs["role"] == "commander" and sum(call["kind"] == "decide" for call in backend.calls) == 1:
                raise HarnessError("Commander service unavailable")

        backend = ScriptedBackend(rejected_and_unavailable)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "Commander service"):
            engine.start("Implement greet", self.plan())
        self.assertIn("commander:decide:run", engine.run["commander_receipts"])
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            resumed.resume(engine.run["id"], reverify=True)
        self.assertEqual(sum(call["kind"] == "decide" for call in backend.calls), 2)
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 1)
        self.assertEqual(len(resumed.run["final_history"]), 1)
        self.assertEqual(resumed.run["final_history"][-1]["status"], "rejected")

    def test_returned_rejection_replays_validation_after_hook_interrupt_without_reroll(self):
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
        # The replayed rejection is final for this snapshot; the demo commander then asks.
        with self.assertRaisesRegex(HarnessError, "等待操作者决定：task_id=final"):
            resumed.resume(engine.run["id"])
        self.assertEqual(resumed.run["final_history"][-1]["status"], "rejected")
        self.assertEqual(len(resumed.run["final_history"]), 1)
        self.assertEqual([call["kind"] for call in backend.calls[len(calls):]], ["decide"])
        calls = list(backend.calls)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            self.engine(backend).resume(engine.run["id"], reverify=True)
        self.assertEqual(backend.calls, calls)
