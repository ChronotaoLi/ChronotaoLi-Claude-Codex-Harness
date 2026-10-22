import copy
import json
import sys
from pathlib import Path

from harness import git
from harness.adapters import DemoBackend
from harness.util import HarnessError, read_json
from test_harness import RepoCase, ROOT, interrupt_started_agent


def finding(severity="blocking", category="defect"):
    return {"id": "edge-case", "severity": severity, "category": category,
            "location": "hello.py:1", "message": "Inspect the acceptance edge case",
            "evidence": ["test_hello.py requires the greeting"]}


class ScriptedBackend(DemoBackend):
    def __init__(self, change=None):
        self.change = change
        self.calls = []

    def invoke(self, **kwargs):
        packet = json.loads(kwargs["prompt"].split("\n\nREFERENCE MEMORY")[0])["packet"]
        self.calls.append({"role": kwargs["role"], "kind": kwargs["kind"],
                           "packet": copy.deepcopy(packet), "task_id": kwargs["schema"]["properties"]["task_id"]["enum"][0]})
        result = super().invoke(**kwargs)
        if self.change:
            self.change(self, kwargs, packet, result)
        return result


class RuntimeChangesTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def test_final_gate_reviews_original_goal_full_diff_and_runs_verification(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertFalse(any(call["role"] == "commander" for call in backend.calls))
        final = [call for call in backend.calls if call["kind"] == "final_review"]
        self.assertEqual(len(final), 1)
        packet = final[0]["packet"]
        self.assertEqual(packet["goal"], "Implement greet")
        self.assertEqual(packet["original_tasks"], self.plan()["tasks"])
        self.assertEqual(packet["base_commit"], git.head(self.repo))
        self.assertIn('+def greet(name):', Path(packet["diff_artifact"]).read_text())
        self.assertTrue(any(d["role"] == "verification" and d["task_id"] == "final" for d in result["dispatches"]))
        self.assertEqual(result["final_history"][-1]["status"], "approved")
        worker = next(call for call in backend.calls if call["role"] == "worker")
        self.assertEqual(worker["packet"]["goal"], "Implement greet")
        self.assertIn("readonly_commands", worker["packet"]["tool_constraints"])

    def test_final_rejection_cannot_reroll_same_snapshot_or_accept_dirty_fix(self):
        def reject_final(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result.update(verdict="request_changes", findings=[finding()])
        backend = ScriptedBackend(reject_final)
        self.config.data["runtime"]["max_replans"] = 0
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "重新规划次数"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(len(engine.run["accepted"]), 1)
        count = len(engine.run["dispatches"])
        with self.assertRaisesRegex(HarnessError, "同一代码快照"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(read_json(engine.directory / "state.json")["dispatches"]), count)
        (engine.worktree / "hello.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n# unreviewed\n')
        with self.assertRaisesRegex(HarnessError, "干净的已接受提交"):
            self.engine(backend).resume(engine.run["id"])

    def test_final_rejection_adds_corrective_task_without_weakening_original(self):
        def corrective(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and sum(c["kind"] == "final_review" for c in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding()])
            if kwargs["kind"] == "replan":
                tasks = copy.deepcopy(packet.get("tasks", packet["original_tasks"]))
                task = copy.deepcopy(tasks[0])
                task.update(id="correct", title="Correct edge case", description="Correct the final finding", depends_on=["greet"])
                result.update(action="revise", reason="Address final review finding", new_tasks=[task], updates=[])
            if kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                with (kwargs["cwd"] / "hello.py").open("a") as file:
                    file.write("# correction independently reviewed\n")
        backend = ScriptedBackend(corrective)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(result["accepted"]), {"greet", "correct"})
        self.assertEqual(result["original_tasks"], self.plan()["tasks"])
        self.assertEqual(result["tasks"][0], self.plan()["tasks"][0])
        self.assertEqual(result["replan_attempts"], 1)
        self.assertEqual(result["final_attempts"], 2)
        self.assertEqual([item["status"] for item in result["final_history"]], ["rejected", "approved"])
        self.assertEqual(result["total_attempts"], 2)
        self.assertNotEqual(result["final_history"][0]["head"], result["final_history"][1]["head"])

    def test_noop_corrective_task_does_not_get_fresh_final_verdict(self):
        self.config.data["runtime"]["max_replans"] = 1
        def no_change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result.update(verdict="request_changes", findings=[finding()])
            elif kwargs["kind"] == "replan":
                task = copy.deepcopy(packet.get("tasks", packet["original_tasks"])[0])
                task.update(id="correct", depends_on=["greet"])
                result.update(action="revise", reason="Fix finding", new_tasks=[task], updates=[])
        engine = self.engine(ScriptedBackend(no_change))
        with self.assertRaisesRegex(HarnessError, "同一代码快照"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["final_attempts"], 1)
        self.assertEqual(len(engine.run["accepted"]), 2)
        self.assertEqual(engine.run["pending_replan"]["trigger"]["unchanged_corrections"], ["correct"])

    def test_final_readonly_mutation_never_completes(self):
        def mutate(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                (kwargs["cwd"] / "oops.txt").write_text("changed")
        engine = self.engine(ScriptedBackend(mutate))
        with self.assertRaisesRegex(HarnessError, "修改了代码"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["status"], "blocked")

    def test_review_history_survives_later_verification_failure(self):
        def repair(backend, kwargs, packet, result):
            worker_count = sum(call["role"] == "worker" for call in backend.calls)
            if kwargs["kind"] == "review" and worker_count == 1:
                result.update(verdict="request_changes", findings=[finding()])
            if kwargs["role"] == "worker" and worker_count == 2:
                (kwargs["cwd"] / "hello.py").write_text('def greet(name):\n    return "wrong"\n')
        backend = ScriptedBackend(repair)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["attempts"]["greet"], 3)
        review = [call for call in backend.calls if call["kind"] == "review"][-1]
        self.assertEqual(review["packet"]["prior_reviews"][0]["review"]["findings"][0]["id"], "edge-case")
        artifact = review["packet"]["prior_reviews"][0]["artifact"]
        self.assertTrue(Path(artifact).exists())
        self.assertEqual(len(result["review_history"]["greet"]), 2)

    def test_advisory_findings_can_approve_but_request_changes_still_blocks(self):
        def advisory(backend, kwargs, packet, result):
            if kwargs["role"] == "reviewer":
                result["findings"] = [finding("advisory", "preference")]
        result = self.engine(ScriptedBackend(advisory)).start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"]["greet"], 1)
        def reject(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                result.update(verdict="request_changes", findings=[])
        self.config.data["runtime"]["max_attempts"] = 1
        engine = self.engine(ScriptedBackend(reject))
        with self.assertRaisesRegex(HarnessError, "尝试次数"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["accepted"], {})

    def test_task_blocker_replans_and_archives_partial_edits_before_prerequisite(self):
        def prerequisite(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and kwargs["task"]["id"] == "greet" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                (kwargs["cwd"] / "hello.py").write_text("partial blocked implementation\n")
                result.update(status="blocked", blocker_kind="task", blockers=["Need prerequisite"], summary="Missing prerequisite")
            elif kwargs["kind"] == "replan":
                original = copy.deepcopy(packet.get("tasks", packet["original_tasks"])[0])
                prerequisite_task = copy.deepcopy(original)
                prerequisite_task.update(id="prepare", title="Prepare prerequisite")
                result.update(action="revise", reason="Prepare prerequisite before original task", new_tasks=[prerequisite_task],
                              updates=[{"id": original["id"], "description": original["description"], "add_depends_on": ["prepare"]}])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "prepare":
                # DemoBackend already generated a valid file; inspect the archived original separately.
                with (kwargs["cwd"] / "hello.py").open("a") as file:
                    file.write("# prerequisite\n")
        engine = self.engine(ScriptedBackend(prerequisite))
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["attempts"], {"greet": 2, "prepare": 1})
        self.assertEqual(result["total_attempts"], 3)
        archive = engine.directory / "recovery" / "replan-1"
        self.assertEqual((archive / "files" / "hello.py").read_text(), "partial blocked implementation\n")
        self.assertEqual(read_json(archive / "restored.json")["head"], result["base_commit"])
        self.assertEqual(git.head(self.repo), result["base_commit"])

    def test_environment_failure_and_scope_violation_do_not_trigger_replan(self):
        for mode in ("environment", "scope"):
            def blocked(backend, kwargs, packet, result):
                if kwargs["role"] == "worker":
                    result.update(status="blocked", blocker_kind="environment" if mode == "environment" else "task",
                                  blockers=["Permission or prerequisite unavailable"], summary="Blocked")
                    if mode == "scope":
                        (kwargs["cwd"] / "outside.txt").write_text("out of scope")
            backend = ScriptedBackend(blocked)
            engine = self.engine(backend)
            with self.subTest(mode=mode), self.assertRaises(HarnessError):
                engine.start("Implement greet", self.plan())
            self.assertEqual(engine.run["replan_attempts"], 0)
            self.assertFalse(any(call["kind"] == "replan" for call in backend.calls))

    def test_exhausted_attempt_and_total_budgets_stop_before_replan(self):
        def blocked(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.update(status="blocked", blocker_kind="task", blockers=["Need prerequisite"], summary="Blocked")
        self.config.data["runtime"]["max_attempts"] = 1
        engine = self.engine(ScriptedBackend(blocked))
        with self.assertRaisesRegex(HarnessError, "重新命名任务"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["replan_attempts"], 0)
        self.config.data["runtime"]["max_attempts"] = 3
        self.config.data["runtime"]["max_total_attempts"] = 1
        engine = self.engine(ScriptedBackend(blocked))
        with self.assertRaisesRegex(HarnessError, "总尝试次数"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["total_attempts"], 1)
        self.assertEqual(engine.run["replan_attempts"], 0)

    def test_resume_budget_increase_preserves_attempts_and_logs_change(self):
        def interrupt(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                interrupt_started_agent(kwargs)
        engine = self.engine(ScriptedBackend(interrupt))
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        old_budget = engine.run["config_snapshot"]["runtime"]["context_chars"]
        self.config.data["runtime"]["context_chars"] += 100
        self.assertEqual(engine.run["config_snapshot"]["runtime"]["context_chars"], old_budget)
        result = self.engine().resume(engine.run["id"])
        self.assertEqual(result["attempts"]["greet"], 2)
        self.assertEqual(result["total_attempts"], 2)
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        change = next(event for event in events if event["event"] == "resume_config_relaxed")
        self.assertEqual(change["changes"]["context_chars"], {"old": old_budget, "new": old_budget + 100})

    def test_unknown_memory_approval_stops_before_commit(self):
        def invalid(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                result["approved_memory_keys"] = ["invented.key"]
        engine = self.engine(ScriptedBackend(invalid))
        with self.assertRaisesRegex(HarnessError, "不存在的记忆"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(git.head(engine.worktree), engine.run["base_commit"])

    def test_schema_v1_resume_refuses_without_dispatch(self):
        engine = self.engine()
        result = engine.start("Implement greet", self.plan(), pause_after_plan=True)
        engine.run.pop("schema_version")
        engine.save()
        with self.assertRaisesRegex(HarnessError, "旧 run 状态格式"):
            self.engine().resume(result["id"], approve_plan=True)
        self.assertEqual(engine.run["dispatches"], [])

    def test_task_named_final_has_separate_verification_artifacts(self):
        plan = self.plan()
        plan["tasks"][0]["id"] = "final"
        engine = self.engine()
        result = engine.start("Implement greet", plan)
        task_diff = Path(result["accepted"]["final"]["verification"]).parent / "change.diff"
        final_diff = Path(result["final_history"][-1]["diff_artifact"])
        self.assertNotEqual(task_diff, final_diff)
        self.assertIn("tasks/final/1", str(task_diff))
        self.assertTrue(task_diff.exists())
        self.assertTrue(final_diff.exists())

    def test_resume_continues_interrupted_replan_without_rerolling_final_review(self):
        def interrupt_replan(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result.update(verdict="request_changes", findings=[finding()])
            elif kwargs["kind"] == "replan":
                interrupt_started_agent(kwargs)
        engine = self.engine(ScriptedBackend(interrupt_replan))
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        self.assertIn("pending_replan", engine.run)
        def continue_replan(backend, kwargs, packet, result):
            if kwargs["kind"] == "replan":
                task = copy.deepcopy(packet.get("tasks", packet["original_tasks"])[0])
                task.update(id="correct", depends_on=["greet"])
                result.update(action="revise", reason="Fix the recorded final finding", new_tasks=[task], updates=[])
            elif kwargs["role"] == "worker":
                with (kwargs["cwd"] / "hello.py").open("a") as file:
                    file.write("# reviewed correction\n")
        backend = ScriptedBackend(continue_replan)
        resumed = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(backend.calls[0]["kind"], "replan")
        self.assertEqual(resumed["final_attempts"], 2)
        self.assertEqual(resumed["replan_attempts"], 1)
        self.assertNotIn("pending_replan", resumed)

    def test_applied_revision_checkpoint_clears_old_task_cursor(self):
        def revise(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.update(status="blocked", blocker_kind="task", blockers=["Need preparation"], summary="Need preparation")
            elif kwargs["kind"] == "replan":
                task = copy.deepcopy(packet.get("tasks", packet["original_tasks"])[0])
                prerequisite = copy.deepcopy(task)
                prerequisite["id"] = "prepare"
                result.update(action="revise", reason="Add preparation", new_tasks=[prerequisite],
                              updates=[{"id": task["id"], "description": task["description"], "add_depends_on": ["prepare"]}])
        engine = self.engine(ScriptedBackend(revise))
        save = engine.save
        def interrupt_after_application():
            save()
            if len(engine.run["plan_history"]) == 2:
                raise KeyboardInterrupt()
        engine.save = interrupt_after_application
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        persisted = read_json(engine.directory / "state.json")
        self.assertIsNone(persisted["current_task"])
        self.assertNotIn("pending_replan", persisted)
        result = self.engine().resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(result["accepted"]), {"prepare", "greet"})

    def test_pre_verify_hook_cannot_rewrite_candidate(self):
        engine = self.engine()
        emit = engine.hooks.emit
        def mutate(event, payload, folder):
            if event == "pre_verify":
                with (engine.worktree / "hello.py").open("a") as file:
                    file.write("# hook rewrite\n")
            return emit(event, payload, folder)
        engine.hooks.emit = mutate
        with self.assertRaisesRegex(HarnessError, "验证命令修改"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["accepted"], {})

    def test_approval_cannot_preapprove_plan_generated_after_interruption(self):
        def interrupt(backend, kwargs, packet, result):
            if kwargs["kind"] == "plan":
                interrupt_started_agent(kwargs)
        engine = self.engine(ScriptedBackend(interrupt))
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", pause_after_plan=True)
        self.assertEqual(engine.run["tasks"], [])
        backend = ScriptedBackend()
        result = self.engine(backend).resume(engine.run["id"], approve_plan=True)
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertFalse(result["plan_approved"])
        self.assertEqual([call["kind"] for call in backend.calls], ["plan"])
        result = self.engine().resume(result["id"], approve_plan=True)
        self.assertEqual(result["status"], "completed")

    def test_hidden_staged_out_of_scope_content_never_reaches_acceptance(self):
        def hidden(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                target = kwargs["cwd"] / "test_hello.py"
                original = target.read_text()
                target.write_text("# hidden unreviewed content\n")
                git.git(kwargs["cwd"], "add", "test_hello.py")
                target.write_text(original)
        engine = self.engine(ScriptedBackend(hidden))
        with self.assertRaisesRegex(HarnessError, "越界"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(git.head(engine.worktree), engine.run["base_commit"])

    def test_reviewer_index_only_mutation_is_detected(self):
        def staged_only(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                target = kwargs["cwd"] / "hello.py"
                original = target.read_text()
                target.write_text("# unreviewed staged file\n")
                git.git(kwargs["cwd"], "add", "hello.py")
                target.write_text(original)
        engine = self.engine(ScriptedBackend(staged_only))
        with self.assertRaisesRegex(HarnessError, "修改了代码"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["accepted"], {})

    def test_accept_rebuilds_index_from_reviewed_paths(self):
        original = (self.repo / "test_hello.py").read_text()
        (self.repo / "test_hello.py").write_text("# hidden staged change\n")
        git.git(self.repo, "add", "test_hello.py")
        (self.repo / "test_hello.py").write_text(original)
        (self.repo / "hello.py").write_text("# allowed\n")
        commit = git.accept(self.repo, ["hello.py"], "run_test", "test")
        self.assertEqual(git.git(self.repo, "show", f"{commit}:test_hello.py"), original)
        self.assertEqual(git.git(self.repo, "show", f"{commit}:hello.py"), "# allowed\n")

    def test_archive_refuses_symlink_ancestor_without_reset(self):
        (self.repo / "folder").mkdir()
        (self.repo / "folder" / "file.txt").write_text("tracked\n")
        git.git(self.repo, "add", "folder/file.txt")
        git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false", "commit", "-m", "directory fixture")
        engine = self.engine()
        engine.start("Implement greet", self.plan(), pause_after_plan=True)
        (engine.worktree / "folder" / "file.txt").unlink()
        (engine.worktree / "folder").rmdir()
        (engine.worktree / "folder").symlink_to(self.repo / "folder", target_is_directory=True)
        archive = engine.directory / "recovery" / "symlink-test"
        with self.assertRaisesRegex(HarnessError, "符号链接祖先"):
            git.archive_and_reset(engine.worktree, engine.run["expected_head"], archive, engine.state / "worktrees")
        self.assertFalse(archive.exists())
        self.assertTrue((engine.worktree / "folder").is_symlink())
        self.assertEqual((self.repo / "folder" / "file.txt").read_text(), "tracked\n")

    def test_post_accept_hook_must_succeed_on_resume_without_repeating_worker(self):
        backend = ScriptedBackend()
        def failing_hook(engine):
            emit = engine.hooks.emit
            def fail(event, payload, logs):
                if event == "post_accept":
                    raise HarnessError("post_accept policy rejected")
                return emit(event, payload, logs)
            engine.hooks.emit = fail
        engine = self.engine(backend)
        failing_hook(engine)
        with self.assertRaisesRegex(HarnessError, "post_accept policy rejected"):
            engine.start("Implement greet", self.plan())
        accepted = engine.run["accepted"]["greet"]["commit"]
        self.assertIn("pending_post_accept", engine.run)
        resumed = self.engine(backend)
        failing_hook(resumed)
        with self.assertRaisesRegex(HarnessError, "post_accept policy rejected"):
            resumed.resume(engine.run["id"])
        self.assertEqual(resumed.run["status"], "blocked")
        self.assertEqual(resumed.run["accepted"]["greet"]["commit"], accepted)
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)
        self.assertFalse(any(call["kind"] == "final_review" for call in backend.calls))
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("pending_post_accept", result)
        self.assertEqual(result["accepted"]["greet"]["commit"], accepted)
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)
