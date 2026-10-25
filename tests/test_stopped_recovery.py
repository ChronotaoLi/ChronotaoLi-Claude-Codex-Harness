import copy
import sys
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.util import HarnessError, read_json, run_process
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, finding


class StoppedRecoveryTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def stopped_backend(self):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blockers=["Task needs a revised approach"], blocker_kind="task")
            if kwargs["kind"] == "replan" and sum(c["kind"] == "replan" for c in backend.calls) > 1:
                result.update(action="revise", reason="Use the clarified implementation approach", new_tasks=[],
                              updates=[{"id": "greet", "description": "Implement greet with the revised approach",
                                        "add_depends_on": []}])
        return ScriptedBackend(change)

    def test_stop_survives_plain_resume_and_explicit_retry_reopens_only_planning(self):
        backend = self.stopped_backend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        calls = copy.deepcopy(backend.calls)
        budget = (engine.run["total_attempts"], engine.run["agent_dispatches"], engine.run["replan_attempts"])
        for _ in range(2):
            resumed = self.engine(backend)
            with self.assertRaisesRegex(HarnessError, "retry-stopped"):
                resumed.resume(engine.run["id"])
            self.assertEqual(backend.calls, calls)
            self.assertEqual((resumed.run["total_attempts"], resumed.run["agent_dispatches"], resumed.run["replan_attempts"]), budget)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"], retry_stopped="final")
        self.assertEqual(backend.calls, calls)
        result = self.engine(backend).resume(engine.run["id"], retry_stopped="greet")
        self.assertEqual(backend.calls[len(calls)]["kind"], "replan")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(result["replan_attempts"], 2)
        self.assertNotIn("replan_stopped", result)

    def test_explicit_retry_preserves_request_when_replan_budget_is_exhausted(self):
        self.config.data["runtime"]["max_replans"] = 1
        backend = self.stopped_backend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        calls = len(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaises(HarnessError):
            resumed.resume(engine.run["id"], retry_stopped="greet")
        self.assertEqual(len(backend.calls), calls)
        self.assertIn("pending_replan", resumed.run)
        self.config.data["runtime"]["max_replans"] = 2
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")

    def test_reverify_and_retry_stopped_are_rejected_without_changing_saved_stop(self):
        for target in ("final", "greet"):
            with self.subTest(target=target):
                def block_task(backend, kwargs, packet, result):
                    if target == "greet" and kwargs["role"] == "worker":
                        result.update(status="blocked", blockers=["Task needs a revised approach"], blocker_kind="task")

                def fail_final(argv, cwd, log, **kwargs):
                    if "final" in Path(log).parts:
                        return 1
                    return run_process(argv, cwd, log, **kwargs)

                backend = ScriptedBackend(block_task)
                engine = self.engine(backend)
                with patch("harness.verification.run_process", side_effect=fail_final), self.assertRaisesRegex(HarnessError, "停止重新规划"):
                    engine.start("Implement greet", self.plan())
                self.assertEqual(engine.run["replan_stopped"]["target"], target)
                # Even an explicit exit confirmation must wait until the option
                # combination has been validated, before auditing or saving it.
                dispatch = engine.run["dispatches"][0]
                dispatch["exit_confirmed"] = False
                engine.save()
                state_path = engine.directory / "state.json"
                events_path = engine.directory / "events.jsonl"
                state_before = state_path.read_bytes()
                events_before = events_path.read_bytes()
                stop_before = copy.deepcopy(engine.run["replan_stopped"])
                calls_before = copy.deepcopy(backend.calls)

                with self.assertRaisesRegex(HarnessError, "--reverify.*--retry-stopped"):
                    self.engine(backend).resume(engine.run["id"], retry_stopped=target, reverify=True,
                                                confirm_exited=[dispatch["id"]])
                self.assertEqual(state_path.read_bytes(), state_before)
                self.assertEqual(events_path.read_bytes(), events_before)
                self.assertEqual(read_json(state_path)["replan_stopped"], stop_before)
                self.assertEqual(backend.calls, calls_before)

                resumed = self.engine(backend)
                with self.assertRaisesRegex(HarnessError, "停止重新规划"):
                    resumed.resume(engine.run["id"], retry_stopped=target, confirm_exited=[dispatch["id"]])
                self.assertEqual(len(backend.calls), len(calls_before) + 1)
                self.assertEqual(backend.calls[-1]["kind"], "replan")
                self.assertEqual(read_json(engine.directory / "plans" / "replan-2.json")["trigger"], stop_before["trigger"])
                self.assertEqual(resumed.run["replan_attempts"], 2)
                self.assertEqual(resumed.run["final_attempts"], engine.run["final_attempts"])
                self.assertIn("operator_exit_confirmation", resumed.run["dispatches"][0])

    def corrective_backend(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and sum(c["kind"] == "final_review" for c in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding()])
            elif kwargs["kind"] == "replan":
                number = sum(c["kind"] == "replan" for c in backend.calls)
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id=f"correct{number}", depends_on=["greet"], description="Address final finding")
                result.update(action="revise", reason="Resolve final finding", new_tasks=[task], updates=[])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct2":
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# Independently reviewed correction\n")
        return ScriptedBackend(change)

    def test_noop_correction_requests_another_plan_without_redrawing_review(self):
        backend = self.corrective_backend()
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        replans = [c for c in backend.calls if c["kind"] == "replan"]
        self.assertEqual(len(replans), 2)
        self.assertEqual(replans[1]["packet"]["trigger"]["unchanged_corrections"], ["correct1"])
        self.assertEqual(result["accepted"]["greet"]["commit"], result["accepted"]["correct1"]["commit"])
        self.assertNotEqual(result["accepted"]["greet"]["commit"], result["accepted"]["correct2"]["commit"])
        self.assertEqual([r["status"] for r in result["final_history"]], ["rejected", "approved"])
        self.assertEqual(sum(c["kind"] == "final_review" for c in backend.calls), 2)

    def test_noop_replanning_intent_survives_budget_exhaustion(self):
        self.config.data["runtime"]["max_replans"] = 1
        backend = self.corrective_backend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        self.assertIn("pending_replan", engine.run)
        self.assertEqual(sum(c["kind"] == "final_review" for c in backend.calls), 1)
        self.config.data["runtime"]["max_replans"] = 2
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["replan_attempts"], 2)

    def test_structurally_invalid_rejection_is_preserved_on_resume(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result.update(verdict="request_changes", findings=[finding()], extra="invalid field")
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        calls = copy.deepcopy(backend.calls)
        for options in ({}, {"retry_stopped": "final"}, {"reverify": True}):
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run["id"], **options)
            self.assertEqual(backend.calls, calls)

    def test_final_stop_retry_replans_before_a_new_snapshot_can_be_reviewed(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and sum(c["kind"] == "final_review" for c in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding()])
            elif kwargs["kind"] == "replan" and sum(c["kind"] == "replan" for c in backend.calls) == 2:
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id="correct", depends_on=["greet"])
                result.update(action="revise", reason="Resolve final finding", new_tasks=[task], updates=[])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# Reviewed correction\n")
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), calls)
        result = self.engine(backend).resume(engine.run["id"], retry_stopped="final")
        self.assertEqual(backend.calls[calls]["kind"], "replan")
        self.assertEqual([r["status"] for r in result["final_history"]], ["rejected", "approved"])
        self.assertEqual(result["status"], "completed")

    def test_generated_final_artifact_can_resume_after_explicit_local_exclusion(self):
        marker = self.repo / ".harness" / "check-count"
        script = ("from pathlib import Path; "
                  f"p=Path({str(marker)!r}); n=int(p.read_text())+1 if p.exists() else 1; "
                  "p.write_text(str(n)); "
                  "Path('report.tmp').write_text('generated') if n > 1 else None")
        self.config.data["verification"]["commands"] = [[sys.executable, "-c", script]]
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "info/exclude"):
            engine.start("Implement greet", self.plan())
        with (self.repo / ".git" / "info" / "exclude").open("a") as output:
            output.write("\n/report.tmp\n")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_attempts"], 1)
        self.assertEqual(result["final_attempts"], 1)
        self.assertEqual(len(result["final_history"][0]["verification_rounds"]), 2)

    def test_abandon_preserves_dirty_worktree_and_prevents_resume(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        engine.start("Implement greet", self.plan(), pause_after_plan=True)
        (engine.worktree / "unsaved.txt").write_text("keep me")
        result = self.engine(backend).abandon(engine.run["id"])
        self.assertEqual(result["status"], "abandoned")
        self.assertEqual((engine.worktree / "unsaved.txt").read_text(), "keep me")
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(backend.calls, [])
        with self.assertRaises(HarnessError):
            self.engine(backend).abandon(engine.run["id"], remove_worktree=True)
        self.assertTrue(engine.worktree.exists())

    def test_abandon_removes_only_clean_owned_worktree_and_keeps_branch_evidence(self):
        engine = self.engine()
        engine.start("Implement greet", self.plan(), pause_after_plan=True)
        result = self.engine().abandon(engine.run["id"], remove_worktree=True)
        self.assertFalse(engine.worktree.exists())
        self.assertEqual(git.git(self.repo, "rev-parse", result["branch"]).strip(), result["expected_head"])
        self.assertTrue((engine.directory / "state.json").exists())
        self.assertTrue(result["worktree_removed"])
        self.assertEqual(self.engine().abandon(engine.run["id"], remove_worktree=True)["status"], "abandoned")

    def test_abandon_refuses_live_process_or_ignored_unsaved_file(self):
        engine = self.engine()
        engine.start("Implement greet", self.plan(), pause_after_plan=True)
        engine.run["dispatches"].append({"pgid": 123456, "exit_confirmed": False})
        engine.save()
        with patch("harness.engine.group_alive", return_value=True), self.assertRaises(HarnessError):
            self.engine().abandon(engine.run["id"])
        engine.run["dispatches"].clear()
        engine.save()
        cache = engine.worktree / "__pycache__"
        cache.mkdir()
        (cache / "unsaved").write_text("keep")
        with self.assertRaises(HarnessError):
            self.engine().abandon(engine.run["id"], remove_worktree=True)
        self.assertTrue((cache / "unsaved").exists())

    def test_worktree_removal_interruption_finishes_saved_intent(self):
        engine = self.engine()
        engine.start("Implement greet", self.plan(), pause_after_plan=True)
        real_git = git.git

        def remove_then_interrupt(repo, *args, **kwargs):
            result = real_git(repo, *args, **kwargs)
            if args[:2] == ("worktree", "remove"):
                raise KeyboardInterrupt()
            return result

        with patch("harness.git.git", side_effect=remove_then_interrupt), self.assertRaises(KeyboardInterrupt):
            self.engine().abandon(engine.run["id"], remove_worktree=True)
        saved = read_json(engine.directory / "state.json")
        self.assertTrue(saved["pending_worktree_removal"])
        self.assertFalse(engine.worktree.exists())
        result = self.engine().abandon(engine.run["id"], remove_worktree=True)
        self.assertTrue(result["worktree_removed"])
        self.assertNotIn("pending_worktree_removal", result)

    def test_resume_reloads_abandonment_after_acquiring_lock(self):
        engine = self.engine()
        engine.start("Implement greet", self.plan(), pause_after_plan=True)
        backend = ScriptedBackend()
        resumed = self.engine(backend)
        load = resumed.load_run
        abandoned = False

        def abandon_between_load_and_lock(ident):
            nonlocal abandoned
            load(ident)
            if not abandoned:
                abandoned = True
                self.engine().abandon(ident)

        with patch.object(resumed, "load_run", side_effect=abandon_between_load_and_lock):
            with self.assertRaisesRegex(HarnessError, "放弃"):
                resumed.resume(engine.run["id"], approve_plan=True)
        self.assertEqual(backend.calls, [])
        self.assertEqual(read_json(engine.directory / "state.json")["status"], "abandoned")
