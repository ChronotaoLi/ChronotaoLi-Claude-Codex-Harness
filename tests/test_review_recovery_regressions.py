import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

from harness.process_runtime import ProcessOutcome
from harness.util import HarnessError, read_json, run_process
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, decided, finding, revised, trigger_kind


class ReviewRecoveryRegressionTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def assert_blocker_decides_after_budget_increase(self, limit, initial):
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
        # Nothing persistent marks the decision; the saved blocker derives it.
        feedback = saved["feedback"]
        self.assertEqual(feedback["task_id"], "greet")
        self.assertEqual(feedback["worker"]["blocker_kind"], "task")
        self.assertEqual(saved["attempts"], {"greet": 1})
        calls = copy.deepcopy(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(saved["id"])
        self.assertEqual(backend.calls, calls)

        self.config.data["runtime"][limit] = initial + 1
        resumed = self.engine(backend)
        # The demo commander's question makes the first resumed operation observable.
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            resumed.resume(saved["id"])
        self.assertEqual([(call["role"], call["kind"]) for call in backend.calls[len(calls):]],
                         [("commander", "decide")])
        seen = backend.calls[len(calls)]["packet"]["trigger"]["feedback"]["worker"]
        self.assertEqual((seen["blocker_kind"], seen["blockers"]), ("task", feedback["worker"]["blockers"]))
        self.assertEqual(resumed.run["attempts"], saved["attempts"])
        self.assertEqual(resumed.run["decision_requests"][-1]["kind"], "commander")

    def test_last_task_attempt_preserves_the_derived_decision(self):
        self.assert_blocker_decides_after_budget_increase("max_attempts", 1)

    def test_exhausted_dispatches_preserve_the_derived_decision(self):
        self.assert_blocker_decides_after_budget_increase("max_agent_dispatches", 1)

    def test_second_invalid_review_summary_stops_after_one_redo(self):
        def long_summary(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                result["summary"] = "x" * 4001

        backend = ScriptedBackend(long_summary)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "连续两次无效"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 2)
        saved = read_json(engine.directory / "state.json")
        reviews = [d for d in saved["dispatches"] if d.get("kind") == "review"]
        self.assertEqual([d["status"] for d in reviews], ["invalid", "invalid"])
        self.assertEqual(reviews[1]["retry_of"], reviews[0]["id"])
        # A resume is a new request (no retry_of), then its own single redo.
        with self.assertRaisesRegex(HarnessError, "连续两次无效"):
            self.engine(backend).resume(saved["id"])
        after = read_json(engine.directory / "state.json")
        reviews = [d for d in after["dispatches"] if d.get("kind") == "review"]
        self.assertEqual([d.get("retry_of") for d in reviews], [None, reviews[0]["id"], None, reviews[2]["id"]])
        self.assertEqual(after["agent_dispatches"], saved["agent_dispatches"] + 2)
        self.assertEqual(after["attempts"], saved["attempts"])

    def test_blank_question_still_registers_and_the_answer_reaches_the_next_decide(self):
        def ask_once(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="task", blockers=["Clarify the prerequisite"])
            if trigger_kind(kwargs, packet) == "task" and packet["trigger"]["feedback"]:
                if "operator_answer" not in packet:
                    decided(result, "ask", reason=" ")
                else:
                    revised(result, packet, "Use the clarified implementation approach",
                            updates=[{"id": "greet", "description": "Implement the clarified greeting behavior",
                                      "add_depends_on": []}])

        backend = ScriptedBackend(ask_once)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定：task_id=greet"):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        asked = next(d for d in saved["dispatches"] if d.get("kind") == "decide")
        # An ask is a veto even with a blank question; the request carries a bounded notice.
        self.assertEqual(asked["status"], "invalid_asked")
        self.assertIn("reason", asked["error"])
        request = saved["decision_requests"][-1]
        self.assertIn("Read the bound original result", request["question"])
        evidence = Path(request["source"]["artifact"]).read_bytes()
        calls = copy.deepcopy(backend.calls)
        for _ in range(2):
            with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
                self.engine(backend).resume(saved["id"])
            self.assertEqual(backend.calls, calls)
        answer = self.repo / "answer.json"
        answer.write_text(json.dumps({"schema_version": 1, "request_id": request["id"], "task_id": "greet",
                                                    "decision": "continue", "summary": "Use the documented interface"}))
        result = self.engine(backend).resume(saved["id"], operator_decision=[str(answer)])
        self.assertEqual(backend.calls[len(calls)]["kind"], "decide")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(len(result["plan_history"]), 2)
        self.assertEqual(Path(request["source"]["artifact"]).read_bytes(), evidence)

    def test_failed_worker_recovery_reaches_same_task_without_leaking_to_next_task(self):
        def interrupted_edit(backend, kwargs, packet):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                (kwargs["cwd"] / "hello.py").write_text("def greet(name:\n    # unfinished edit\n")
                self.assertFalse((kwargs["job"] / "response.json").exists())
                raise HarnessError("Claude 未成功完成：error_max_turns")

        plan = self.plan()
        plan["tasks"].append({**copy.deepcopy(plan["tasks"][0]), "id": "confirm", "depends_on": ["greet"]})
        backend = ScriptedBackend()
        backend.before_result = interrupted_edit
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement and confirm greet", plan)
        failure = copy.deepcopy(engine.run["last_runtime_error"])
        self.assertIn("unfinished edit", (engine.worktree / "hello.py").read_text())
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        same_task = [call for call in backend.calls[1:]
                     if call["task_id"] == "greet" or call["kind"] == "decide"]
        self.assertTrue(any(call["role"] == "worker" for call in same_task))
        self.assertTrue(any(call["role"] == "commander" for call in same_task))
        for call in same_task:
            recovery = call["packet"]["trigger" if call["role"] == "commander" else "recovery"]
            recovery = recovery["recovery"] if call["role"] == "commander" else recovery
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
        def fail_worker(backend, kwargs, packet):
            if kwargs["role"] == "worker":
                self.assertFalse((kwargs["job"] / "response.json").exists())
                raise HarnessError("worker service unavailable")

        backend = ScriptedBackend()
        backend.before_result = fail_worker
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

    def test_noop_final_verification_correction_decides_again_without_reverification(self):
        def correct_in_second_task(backend, kwargs, packet, result):
            if trigger_kind(kwargs, packet) == "final":
                number = sum(call["kind"] == "decide" for call in backend.calls)
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id=f"correct{number}", depends_on=["greet"],
                            description="Address the final integration failure")
                revised(result, packet, "Resolve the integration failure", new_tasks=[task])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct2":
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# integration correction\n")

        def final_gate(argv, cwd, log, **kwargs):
            if "final" in Path(log).parts and "# integration correction" not in (cwd / "hello.py").read_text():
                kwargs["on_start"](123)
                Path(log).with_suffix(".stdout").write_text("")
                Path(log).with_suffix(".stderr").write_text("Integration correction is missing")
                kwargs["on_outcome"](ProcessOutcome(started=True, startup_state="started", pid=123, pgid=123,
                                                    exit_code=1, exit_confirmed=True))
                return 1
            return run_process(argv, cwd, log, **kwargs)

        backend = ScriptedBackend(correct_in_second_task)
        engine = self.engine(backend)
        with patch("harness.verification.run_process", side_effect=final_gate):
            result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        replans = [call for call in backend.calls if call["kind"] == "decide"]
        self.assertEqual(len(replans), 2)
        self.assertEqual(replans[1]["packet"]["trigger"]["accepted_since"], ["correct1"])
        self.assertEqual(result["accepted"]["greet"]["commit"], result["accepted"]["correct1"]["commit"])
        self.assertNotEqual(result["accepted"]["greet"]["commit"], result["accepted"]["correct2"]["commit"])
        self.assertEqual([record["status"] for record in result["final_history"]], ["verification_failed", "approved"])
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 1)
        failed = result["final_history"][0]
        self.assertEqual(failed["verification_failures"][0]["exit_code"], 1)
        self.assertTrue(Path(failed["artifact"]).is_file())
