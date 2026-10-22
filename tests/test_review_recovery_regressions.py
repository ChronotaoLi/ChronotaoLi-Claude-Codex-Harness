import copy
import sys
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json, run_process
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, finding


class ReviewRecoveryRegressionTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def assert_blocker_replans_after_budget_increase(self, limit, initial):
        self.config.data["runtime"][limit] = initial

        def blocked(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.update(status="blocked", blocker_kind="task",
                              blockers=["A prerequisite must be planned first"])

        backend = ScriptedBackend(blocked)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        trigger = saved["pending_replan"]["trigger"]
        self.assertEqual(trigger["task_id"], "greet")
        self.assertEqual(trigger["worker"]["blocker_kind"], "task")
        self.assertEqual(saved["attempts"], {"greet": 1})
        self.assertEqual(saved["total_attempts"], 1)
        calls = copy.deepcopy(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(saved["id"])
        self.assertEqual(backend.calls, calls)

        self.config.data["runtime"][limit] = initial + 1
        resumed = self.engine(backend)
        # The demo commander's stop makes the first resumed operation observable.
        with self.assertRaises(HarnessError):
            resumed.resume(saved["id"])
        self.assertEqual([(call["role"], call["kind"]) for call in backend.calls[len(calls):]],
                         [("commander", "replan")])
        self.assertEqual(resumed.run["attempts"], saved["attempts"])
        self.assertEqual(resumed.run["total_attempts"], saved["total_attempts"])
        self.assertEqual(resumed.run["replan_attempts"], 1)
        self.assertEqual(resumed.run["replan_stopped"]["trigger"], trigger)

    def test_last_task_attempt_preserves_replanning_intent(self):
        self.assert_blocker_replans_after_budget_increase("max_attempts", 1)

    def test_exhausted_total_attempts_preserve_replanning_intent(self):
        self.assert_blocker_replans_after_budget_increase("max_total_attempts", 1)

    def test_exhausted_replans_preserve_task_blocker_intent(self):
        self.assert_blocker_replans_after_budget_increase("max_replans", 0)

    def assert_immutable_review_format_error_stops(self, kind):
        malformed = finding("advisory", "preference")
        malformed["message"] = "x" * 4001

        def long_finding(backend, kwargs, packet, result):
            if kwargs["kind"] == kind:
                result.update(verdict="approve", findings=[copy.deepcopy(malformed)])

        backend = ScriptedBackend(long_finding)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        key = f"reviewer:{kind}:{'final' if kind == 'final_review' else 'greet'}"
        guard = saved["review_corrections"][key]
        self.assertTrue(guard["unrepairable"])
        self.assertEqual(read_json(Path(guard["artifact"]))["findings"], [malformed])
        self.assertEqual(sum(call["kind"] == kind for call in backend.calls), 1)
        calls = copy.deepcopy(backend.calls)
        for _ in range(2):
            resumed = self.engine(backend)
            with self.assertRaises(HarnessError):
                resumed.resume(saved["id"])
            self.assertEqual(backend.calls, calls)
            self.assertEqual(resumed.run["agent_dispatches"], saved["agent_dispatches"])
            self.assertEqual(resumed.run["accepted"], saved["accepted"])
            self.assertEqual(resumed.run["review_corrections"][key], guard)

    def test_task_review_immutable_format_error_never_dispatches_correction(self):
        self.assert_immutable_review_format_error_stops("review")

    def test_final_review_immutable_format_error_never_dispatches_correction(self):
        self.assert_immutable_review_format_error_stops("final_review")

    def test_second_invalid_review_summary_is_terminal_after_one_correction(self):
        def long_summary(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                result["summary"] = "x" * 4001

        backend = ScriptedBackend(long_summary)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 2)
        saved = read_json(engine.directory / "state.json")
        self.assertTrue(saved["review_corrections"]["reviewer:review:greet"]["exhausted"])
        calls = copy.deepcopy(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(saved["id"])
        self.assertEqual(backend.calls, calls)
        self.assertEqual(read_json(engine.directory / "state.json")["agent_dispatches"], saved["agent_dispatches"])

    def test_interrupted_terminal_guard_write_keeps_invalid_correction_receipt(self):
        def long_summary(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                result["summary"] = "x" * 4001

        backend = ScriptedBackend(long_summary)
        engine = self.engine(backend)
        save = engine.save
        key = "reviewer:review:greet"

        def crash_before_terminal_save():
            if engine.run["review_corrections"].get(key, {}).get("exhausted"):
                raise KeyboardInterrupt()
            save()

        with patch.object(engine, "save", side_effect=crash_before_terminal_save):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertFalse(saved["review_corrections"][key].get("exhausted"))
        receipt = saved["review_receipts"][key]
        self.assertEqual(receipt["dispatch_id"], saved["dispatches"][-1]["id"])
        calls = copy.deepcopy(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaises(HarnessError):
            resumed.resume(saved["id"])
        self.assertTrue(resumed.run["review_corrections"][key]["exhausted"])
        self.assertEqual(backend.calls, calls)
        self.assertEqual(resumed.run["agent_dispatches"], saved["agent_dispatches"])

    def test_blank_reason_stop_is_persistent_counted_and_explicitly_retryable(self):
        def stop_once(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="task", blockers=["Clarify the prerequisite"])
            if kwargs["kind"] == "replan":
                if sum(c["kind"] == "replan" for c in backend.calls) == 1:
                    result.update(action="stop", reason=" ", new_tasks=[], updates=[])
                else:
                    result.update(action="revise", reason="Use the clarified implementation approach", new_tasks=[],
                                  updates=[{"id": "greet", "description": "Implement the clarified greeting behavior",
                                            "add_depends_on": []}])

        backend = ScriptedBackend(stop_once)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        stopped = saved["replan_stopped"]
        self.assertEqual(stopped["target"], "greet")
        self.assertEqual(stopped["reason"], " ")
        self.assertTrue(Path(stopped["artifact"]).is_file())
        evidence = Path(stopped["artifact"]).read_bytes()
        self.assertEqual(saved["replan_attempts"], 1)
        self.assertEqual(saved["format_repairs"], {})
        calls = copy.deepcopy(backend.calls)
        for _ in range(2):
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(saved["id"])
            self.assertEqual(backend.calls, calls)
            self.assertEqual(read_json(engine.directory / "state.json")["replan_attempts"], 1)
        result = self.engine(backend).resume(saved["id"], retry_stopped="greet")
        self.assertEqual(backend.calls[len(calls)]["kind"], "replan")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(result["replan_attempts"], 2)
        self.assertEqual(Path(stopped["artifact"]).read_bytes(), evidence)

    def test_failed_worker_recovery_reaches_same_task_without_leaking_to_next_task(self):
        def interrupted_edit(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                (kwargs["cwd"] / "hello.py").write_text("def greet(name:\n    # unfinished edit\n")
                raise HarnessError("Claude 未成功完成：error_max_turns")

        plan = self.plan()
        plan["tasks"].append({**copy.deepcopy(plan["tasks"][0]), "id": "confirm", "depends_on": ["greet"]})
        backend = ScriptedBackend(interrupted_edit)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement and confirm greet", plan)
        failure = copy.deepcopy(engine.run["last_runtime_error"])
        self.assertIn("unfinished edit", (engine.worktree / "hello.py").read_text())
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        same_task = [call for call in backend.calls[1:]
                     if call["task_id"] == "greet" or call["kind"] == "dispatch"]
        self.assertTrue(any(call["role"] == "worker" for call in same_task))
        self.assertTrue(any(call["role"] == "commander" for call in same_task))
        for call in same_task:
            recovery = call["packet"]["recovery"]
            self.assertEqual(recovery["task_id"], "greet")
            self.assertEqual(recovery["kind"], "implement")
            self.assertEqual(recovery["artifact"], failure["artifact"])
            self.assertIn("error_max_turns", recovery["error"])
            self.assertIn("hello.py", recovery["changed_files"])
        other_task = [call for call in backend.calls if call["task_id"] == "confirm"]
        self.assertTrue(other_task)
        self.assertTrue(all(not call["packet"].get("recovery") for call in other_task))
        self.assertNotIn("greet", result.get("worker_recovery", {}))
        self.assertEqual(result["attempts"], {"greet": 2, "confirm": 1})

    def hook_spec(self, event, marker, fail_first=False):
        script = ("from pathlib import Path; import sys; "
                  "p=Path(sys.argv[1]); n=int(p.read_text())+1 if p.exists() else 1; "
                  "p.write_text(str(n)); print('hook attempt', n); "
                  f"sys.exit(7 if {fail_first!r} and n == 1 else 0)")
        return {event: [{"argv": [sys.executable, "-c", script, str(marker)]}]}

    def test_post_accept_retry_retains_both_hook_outputs(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        spec = self.hook_spec("post_accept", engine.state / "post-accept-count", fail_first=True)
        engine.hooks.spec = spec
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        commit = engine.run["accepted"]["greet"]["commit"]
        before = {path: path.read_bytes() for path in engine.directory.rglob("hook-post_accept-0.stdout")}
        self.assertEqual(len(before), 1)
        resumed = self.engine(backend)
        resumed.hooks.spec = spec
        result = resumed.resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        outputs = {path: path.read_bytes() for path in engine.directory.rglob("hook-post_accept-0.stdout")}
        self.assertEqual(len(outputs), 2)
        self.assertEqual(set(outputs.values()), {b"hook attempt 1\n", b"hook attempt 2\n"})
        self.assertTrue(all(outputs[path] == content for path, content in before.items()))
        self.assertEqual(result["accepted"]["greet"]["commit"], commit)
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)

    def test_repeated_errors_retain_each_on_error_hook_output(self):
        def fail_worker(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                raise HarnessError("worker service unavailable")

        backend = ScriptedBackend(fail_worker)
        engine = self.engine(backend)
        spec = self.hook_spec("on_error", engine.state / "on-error-count")
        engine.hooks.spec = spec
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        before = {path: path.read_bytes() for path in engine.directory.rglob("hook-on_error-0.stdout")}
        self.assertEqual(len(before), 1)
        resumed = self.engine(backend)
        resumed.hooks.spec = spec
        with self.assertRaises(HarnessError):
            resumed.resume(engine.run["id"])
        outputs = {path: path.read_bytes() for path in engine.directory.rglob("hook-on_error-0.stdout")}
        self.assertEqual(len(outputs), 2)
        self.assertEqual(set(outputs.values()), {b"hook attempt 1\n", b"hook attempt 2\n"})
        self.assertTrue(all(outputs[path] == content for path, content in before.items()))

    def test_noop_final_verification_correction_replans_without_reverification(self):
        def correct_in_second_task(backend, kwargs, packet, result):
            if kwargs["kind"] == "replan":
                number = sum(call["kind"] == "replan" for call in backend.calls)
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id=f"correct{number}", depends_on=["greet"],
                            description="Address the final integration failure")
                result.update(action="revise", reason="Resolve the integration failure", new_tasks=[task], updates=[])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct2":
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# integration correction\n")

        def final_gate(argv, cwd, log, **kwargs):
            if "final" in Path(log).parts and "# integration correction" not in (cwd / "hello.py").read_text():
                Path(log).with_suffix(".stderr").write_text("Integration correction is missing")
                return 1
            return run_process(argv, cwd, log, **kwargs)

        backend = ScriptedBackend(correct_in_second_task)
        engine = self.engine(backend)
        with patch("harness.engine.run_process", side_effect=final_gate):
            result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        replans = [call for call in backend.calls if call["kind"] == "replan"]
        self.assertEqual(len(replans), 2)
        self.assertEqual(replans[1]["packet"]["trigger"]["unchanged_corrections"], ["correct1"])
        self.assertEqual(result["accepted"]["greet"]["commit"], result["accepted"]["correct1"]["commit"])
        self.assertNotEqual(result["accepted"]["greet"]["commit"], result["accepted"]["correct2"]["commit"])
        self.assertEqual(result["final_attempts"], 2)
        self.assertEqual([record["status"] for record in result["final_history"]], ["verification_failed", "approved"])
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 1)
        failed = result["final_history"][0]
        self.assertEqual(failed["verification_failures"][0]["exit_code"], 1)
        self.assertTrue(Path(failed["artifact"]).is_file())
