from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.util import HarnessError, ProcessTimeout, read_json, run_process
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT
from test_runtime_changes import finding


class TaskStageRecoveryTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def task_checks(self, run):
        return [entry for entry in run["dispatches"]
                if entry["role"] == "verification" and entry["task_id"] == "greet"]

    def assert_review_failure_recovery(self, failure):
        def fail_once(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and sum(call["kind"] == "review" for call in backend.calls) == 1:
                raise failure(kwargs)

        self.config.data["runtime"]["max_attempts"] = 1
        self.config.data["runtime"]["max_total_attempts"] = 1
        backend = RecordingBackend(fail_once)
        engine = self.engine(backend)
        with self.assertRaises((HarnessError, KeyboardInterrupt)):
            engine.start("Implement greet", self.plan())
        pending = deepcopy(engine.run["pending_task"])
        self.assertEqual(pending["stage"], "reviewing")
        evidence = {key: Path(pending[key]).read_bytes()
                    for key in ("verification", "diff_artifact", "snapshot_artifact")}
        checks = self.task_checks(engine.run)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 1})
        self.assertEqual(result["total_attempts"], 1)
        self.assertEqual(self.task_checks(result), checks)
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)
        self.assertFalse(any(call["role"] == "commander" for call in backend.calls))
        self.assertEqual(result["accepted"]["greet"]["verification"], pending["verification"])
        for key, original in evidence.items():
            self.assertEqual(Path(pending[key]).read_bytes(), original)
        reviews = [request for request in backend.requests if request["kind"] == "review"]
        self.assertEqual(len(reviews), 2)
        self.assertEqual(len({request["dispatch_id"] for request in reviews}), 2)
        self.assertTrue(all(request["model"] == self.config.role("reviewer", "astra") for request in reviews))

    def test_reviewer_cli_failure_resumes_review_at_worker_budget_limit(self):
        self.assert_review_failure_recovery(lambda kwargs: HarnessError("Reviewer service unavailable"))

    def test_reviewer_keyboard_interrupt_resumes_review_at_worker_budget_limit(self):
        self.assert_review_failure_recovery(lambda kwargs: KeyboardInterrupt())

    def test_reviewer_timeout_resumes_review_at_worker_budget_limit(self):
        self.assert_review_failure_recovery(lambda kwargs: ProcessTimeout(1, kwargs["job"] / "cli"))

    def test_verification_interrupt_retries_checks_in_new_directory_without_worker(self):
        self.config.data["runtime"]["max_attempts"] = 1
        self.config.data["runtime"]["max_total_attempts"] = 1
        backend = RecordingBackend()
        engine = self.engine(backend)

        def interrupt_after_command(argv, cwd, log, **kwargs):
            run_process(argv, cwd, log, **kwargs)
            raise KeyboardInterrupt()

        with patch("harness.engine.run_process", side_effect=interrupt_after_command), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        pending = deepcopy(engine.run["pending_task"])
        self.assertEqual(pending["stage"], "verifying")
        old_folder = Path(pending["verification"]).parent
        old_logs = {path.name: path.read_bytes() for path in old_folder.iterdir() if path.is_file()}
        self.assertTrue(old_logs)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 1})
        self.assertEqual(result["total_attempts"], 1)
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)
        self.assertFalse(any(call["role"] == "commander" for call in backend.calls))
        self.assertEqual(len(self.task_checks(result)), 2)
        self.assertNotEqual(Path(result["accepted"]["greet"]["verification"]).parent, old_folder)
        for name, contents in old_logs.items():
            self.assertEqual((old_folder / name).read_bytes(), contents)

    def test_changed_candidate_invalidates_pending_review_and_requires_new_worker(self):
        def unavailable_once(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and sum(call["kind"] == "review" for call in backend.calls) == 1:
                raise HarnessError("Reviewer service unavailable")

        backend = RecordingBackend(unavailable_once)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        old_pending = deepcopy(engine.run["pending_task"])
        old_evidence = Path(old_pending["verification"]).read_bytes()
        with (engine.worktree / "hello.py").open("a") as output:
            output.write("# modification after verification\n")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(result["total_attempts"], 2)
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 2)
        self.assertEqual(len(self.task_checks(result)), 2)
        self.assertNotEqual(result["accepted"]["greet"]["verification"], old_pending["verification"])
        self.assertEqual(Path(old_pending["verification"]).read_bytes(), old_evidence)
        self.assertNotIn("pending_task", result)
        self.assertNotIn("modification after verification", (engine.worktree / "hello.py").read_text())

    def test_scope_leftovers_stop_resume_before_model_or_budget_until_manual_cleanup(self):
        def out_of_scope_once(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(call["role"] == "worker" for call in backend.calls) == 1:
                (kwargs["cwd"] / "outside.txt").write_text("Preserve this evidence until the operator inspects it")

        backend = RecordingBackend(out_of_scope_once)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        calls = list(backend.calls)
        dispatches = deepcopy(engine.run["dispatches"])
        for _ in range(2):
            resumed = self.engine(backend)
            with self.assertRaisesRegex(HarnessError, "清理越界修改"):
                resumed.resume(engine.run["id"])
            self.assertEqual(resumed.run["attempts"], {"greet": 1})
            self.assertEqual(resumed.run["total_attempts"], 1)
            self.assertEqual(resumed.run["agent_dispatches"], 1)
            self.assertEqual(resumed.run["dispatches"], dispatches)
            self.assertEqual(backend.calls, calls)
        self.assertTrue((engine.worktree / "outside.txt").exists())
        (engine.worktree / "outside.txt").unlink()
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 2)
        self.assertTrue(git.clean(engine.worktree))

    def test_format_correction_cli_failure_preserves_rejection_without_new_worker(self):
        def reject_then_unavailable(backend, kwargs, packet, result):
            if kwargs["kind"] != "review":
                return
            number = sum(call["kind"] == "review" for call in backend.calls)
            if number == 2:
                raise HarnessError("Reviewer correction service unavailable")
            result.update(verdict="request_changes", findings=[finding()],
                          summary="x" * 4001 if number == 1 else "The original defect remains")

        self.config.data["runtime"]["max_attempts"] = 1
        backend = RecordingBackend(reject_then_unavailable)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "correction service"):
            engine.start("Implement greet", self.plan())
        self.assertFalse(engine.run["review_corrections"]["reviewer:review:greet"].get("failed"))
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "尝试次数上限"):
            resumed.resume(engine.run["id"])
        self.assertEqual(resumed.run["attempts"], {"greet": 1})
        self.assertEqual(resumed.run["total_attempts"], 1)
        self.assertEqual(resumed.run["accepted"], {})
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)
        review = resumed.run["review_history"]["greet"][-1]["review"]
        self.assertEqual(review["verdict"], "request_changes")
        self.assertEqual(review["findings"], [finding()])
        reviews = [request for request in backend.requests if request["kind"] == "review"]
        self.assertEqual(len(reviews), 3)
        self.assertEqual(len({request["dispatch_id"] for request in reviews}), 3)

    def test_planning_cli_failure_does_not_pollute_worker_feedback_or_force_dispatch_commander(self):
        def unavailable_once(backend, kwargs, packet, result):
            if kwargs["kind"] == "plan" and sum(call["kind"] == "plan" for call in backend.calls) == 1:
                raise HarnessError("Planning service unavailable")

        backend = RecordingBackend(unavailable_once)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "Planning service"):
            engine.start("Implement greet")
        self.assertEqual(engine.run["total_attempts"], 0)
        self.assertEqual(engine.run["tasks"], [])
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_attempts"], 1)
        self.assertEqual(sum(call["kind"] == "plan" for call in backend.calls), 2)
        self.assertFalse(any(call["kind"] == "dispatch" for call in backend.calls))
        worker = next(call for call in backend.calls if call["role"] == "worker")
        self.assertIsNone(worker["packet"]["feedback"])
        self.assertEqual(result["agent_dispatches"], len(backend.requests))

    def assert_replan_failure_budget(self, mode):
        def fail_replan(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.update(status="blocked", blocker_kind="task", blockers=["Need prerequisite"], summary="Needs preparation")
            elif kwargs["kind"] == "replan":
                if mode == "cli":
                    raise HarnessError("Replanning service unavailable")
                task = deepcopy(packet["original_tasks"][0])
                task.update(id="prepare", skills=["missing-business-skill"])
                result.update(action="revise", reason="Add prerequisite", new_tasks=[task], updates=[{
                    "id": "greet", "description": packet["original_tasks"][0]["description"],
                    "add_depends_on": ["prepare"]}])

        self.config.data["runtime"]["max_replans"] = 1
        self.config.data["runtime"]["max_agent_dispatches"] = 3
        backend = RecordingBackend(fail_replan)
        engine = self.engine(backend)
        error = "service unavailable" if mode == "cli" else "不存在的 skill"
        with self.assertRaisesRegex(HarnessError, error):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["replan_attempts"], 0)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, error):
            resumed.resume(engine.run["id"])
        self.assertEqual(resumed.run["replan_attempts"], 0)
        expected_dispatches = 3 if mode == "cli" else 2
        self.assertEqual(resumed.run["agent_dispatches"], expected_dispatches)
        self.assertEqual(resumed.run["tasks"], self.plan()["tasks"])
        self.assertEqual(len(resumed.run["plan_history"]), 1)
        calls = list(backend.calls)
        again = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "模型派发次数上限" if mode == "cli" else error):
            again.resume(engine.run["id"])
        self.assertEqual(again.run["agent_dispatches"], expected_dispatches)
        self.assertEqual(again.run["replan_attempts"], 0)
        self.assertEqual(again.run["attempts"], {"greet": 1})
        self.assertEqual(backend.calls, calls)
        self.assertEqual(len({request["dispatch_id"] for request in backend.requests}), expected_dispatches)

    def test_replan_cli_failures_preserve_revision_budget_but_consume_dispatch_budget(self):
        self.assert_replan_failure_budget("cli")

    def test_replan_unknown_skill_preserves_result_and_budgets_on_resume(self):
        self.assert_replan_failure_budget("skill")

    def test_increasing_replan_budget_resumes_corrective_planning_without_rerolling_rejection(self):
        def corrective(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and sum(call["kind"] == "final_review" for call in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding()])
            elif kwargs["kind"] == "replan":
                task = deepcopy(packet["original_tasks"][0])
                task.update(id="correct", title="Fix final review finding", depends_on=["greet"])
                result.update(action="revise", reason="Address the saved rejection", new_tasks=[task], updates=[])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# independently reviewed correction\n")

        self.config.data["runtime"]["max_replans"] = 0
        backend = RecordingBackend(corrective)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "重新规划次数"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["pending_replan"]["trigger"]["final_review"]["status"], "rejected")
        calls = list(backend.calls)
        with self.assertRaisesRegex(HarnessError, "重新规划次数"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)
        self.config.data["runtime"]["max_replans"] = 1
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["replan_attempts"], 1)
        self.assertEqual(set(result["accepted"]), {"greet", "correct"})
        self.assertEqual([record["status"] for record in result["final_history"]], ["rejected", "approved"])
        self.assertNotEqual(result["final_history"][0]["head"], result["final_history"][1]["head"])
        resumed_calls = backend.calls[len(calls):]
        self.assertEqual(resumed_calls[0]["kind"], "replan")
        self.assertEqual(sum(call["kind"] == "final_review" for call in resumed_calls), 1)
