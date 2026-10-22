import contextlib
import copy
import io
import json
from unittest.mock import patch

from harness import git
from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class PlanPreflightDiagnosticsTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples/plan.json")

    def test_invalid_supplied_plans_leave_no_run_branch_or_worktree(self):
        missing_skill = self.plan()
        missing_skill["tasks"][0]["skills"] = ["no-such-skill"]
        invalid_scope = self.plan()
        invalid_scope["tasks"][0]["allowed_paths"] = [".git/**"]
        original_refs = git.git(self.repo, "show-ref")
        original_worktrees = git.git(self.repo, "worktree", "list", "--porcelain")
        backend = ScriptedBackend()
        engine = self.engine(backend)
        for plan in ({}, {"tasks": []}, missing_skill, invalid_scope):
            with self.subTest(plan=plan), self.assertRaises(HarnessError):
                engine.start("Implement greet", plan)
            self.assertEqual(git.git(self.repo, "show-ref"), original_refs)
            self.assertEqual(git.git(self.repo, "worktree", "list", "--porcelain"), original_worktrees)
            self.assertFalse((engine.state / "runs").exists())
            self.assertFalse((engine.state / "worktrees").exists())
            self.assertEqual(backend.calls, [])
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertFalse(any(call["kind"] == "plan" for call in backend.calls))

    def test_broad_scope_is_audited_without_relaxing_protected_paths(self):
        plan = self.plan()
        plan["tasks"][0]["allowed_paths"] = ["**", "**/*", "src/**"]
        backend = ScriptedBackend()
        engine = self.engine(backend)
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = engine.start("Implement greet", plan, pause_after_plan=True)
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(backend.calls, [])
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        warnings = [event for event in events if event["event"] == "broad_scope_warning"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["scopes"], [
            {"task_id": "greet", "pattern": "**"}, {"task_id": "greet", "pattern": "**/*"}])
        self.assertIn("**", output.getvalue())
        protected = engine.worktree / ".codex" / "config.toml"
        protected.parent.mkdir()
        protected.write_text("unreviewed\n")
        with self.assertRaises(HarnessError):
            git.check_scope(engine.worktree, ["**"], binding=engine.run["worktree_binding"])
        protected.unlink()
        protected.parent.rmdir()
        completed = self.engine(backend).resume(result["id"], approve_plan=True)
        self.assertEqual(completed["status"], "completed")
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        self.assertEqual(sum(event["event"] == "broad_scope_warning" for event in events), 1)

    def test_revised_plan_warns_only_for_new_broad_task_scope(self):
        plan = self.plan()
        plan["tasks"][0]["allowed_paths"] = ["**/*"]
        engine = self.engine()
        engine.start("Implement greet", plan, pause_after_plan=True)
        revised = copy.deepcopy(plan["tasks"])
        revised[0]["description"] += " with clarification"
        engine.record_plan(revised, "Refine description")
        added = {**copy.deepcopy(revised[0]), "id": "followup"}
        engine.record_plan([*revised, added], "Add followup")
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        warnings = [event for event in events if event["event"] == "broad_scope_warning"]
        self.assertEqual([event["revision"] for event in warnings], [0, 2])
        self.assertEqual(warnings[1]["scopes"], [{"task_id": "followup", "pattern": "**/*"}])

    def test_scope_diagnostic_interruption_preserves_supplied_acceptance_on_resume(self):
        plan = self.plan()
        plan["tasks"][0]["allowed_paths"] = ["**"]
        plan["tasks"][0]["acceptance"].append("Preserve this operator supplied obligation")
        backend = ScriptedBackend()
        engine = self.engine(backend)
        emit = engine.event

        def interrupt(event, **fields):
            emit(event, **fields)
            if event == "broad_scope_warning":
                raise KeyboardInterrupt()

        with patch.object(engine, "event", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan)
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["tasks"], plan["tasks"])
        self.assertEqual(saved["original_tasks"], plan["tasks"])
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["original_tasks"], plan["tasks"])
        self.assertFalse(any(call["kind"] == "plan" for call in backend.calls))

    def test_removal_diagnostic_lists_bounded_paths_and_preserves_all_files(self):
        engine = self.engine()
        engine.start("Implement greet", self.plan(), pause_after_plan=True)
        folder = engine.worktree / "__pycache__"
        folder.mkdir()
        names = [f"evidence-{index:02}.pyc" for index in range(12)]
        for name in names:
            (folder / name).write_bytes(b"preserve evidence")
        unusual = engine.worktree / "scratch\nfile"
        unusual.write_text("preserve scratch")
        with self.assertRaises(HarnessError) as caught:
            self.engine().abandon(engine.run["id"], remove_worktree=True)
        message = str(caught.exception)
        self.assertIn("__pycache__/evidence-00.pyc", message)
        self.assertIn("__pycache__/evidence-09.pyc", message)
        self.assertNotIn("evidence-11.pyc", message)
        self.assertLess(len(message), 3000)
        for name in names:
            self.assertEqual((folder / name).read_bytes(), b"preserve evidence")
        self.assertEqual(unusual.read_text(), "preserve scratch")
        self.assertEqual(read_json(engine.directory / "state.json")["status"], "awaiting_approval")
        for name in names:
            (folder / name).unlink()
        folder.rmdir()
        with self.assertRaises(HarnessError) as caught:
            self.engine().abandon(engine.run["id"], remove_worktree=True)
        self.assertIn(repr(unusual.name), str(caught.exception))
        self.assertNotIn("\n", str(caught.exception))
