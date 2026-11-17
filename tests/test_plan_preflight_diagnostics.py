import contextlib
import copy
import io
import json
from unittest.mock import patch

from harness import git
from harness.cleanup import cleanup_run
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase, cli_notice, run_cli, scratch
from test_runtime_changes import ScriptedBackend, current_tasks, trigger_kind, revised as revised_


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
        self.assertFalse(any(call["role"] == "commander" for call in backend.calls))

    def test_task_ids_equal_after_folding_are_refused_like_duplicates_before_a_run_exists(self):
        # greet and GREET name one evidence directory on a case-insensitive filesystem; judged by name everywhere.
        path = scratch(self) / "plan.json"
        original_refs = git.git(self.repo, "show-ref")
        original_worktrees = git.git(self.repo, "worktree", "list", "--porcelain")
        backend = ScriptedBackend()
        refusals = {}
        for twin in ("greet", "GREET"):
            with self.subTest(twin=twin):
                plan = self.plan()
                second = copy.deepcopy(plan["tasks"][0])
                second.update(id=twin, depends_on=["greet"])
                plan["tasks"].append(second)
                write_json(path, plan)
                code, errors = run_cli(self, backend, ["run", "Implement greet", "--plan", str(path)])
                notice = cli_notice(errors)
                refusals[twin] = (code, notice["kind"], notice["reason"], notice["data"], notice["exits"])
                self.assertIn(repr(["greet", twin]), errors)
                self.assertFalse((self.repo / ".harness" / "runs").exists())
                self.assertEqual(git.git(self.repo, "show-ref"), original_refs)
                self.assertEqual(git.git(self.repo, "worktree", "list", "--porcelain"), original_worktrees)
                self.assertEqual(backend.calls, [])
        self.assertEqual(refusals["GREET"], (2, "invalid_input", "input_schema", {"option": "--plan"}, []))
        self.assertEqual(refusals["GREET"], refusals["greet"])

    def test_a_ledger_holding_task_ids_equal_after_folding_is_refused_at_load_like_an_exact_duplicate(self):
        # A ledger written under raw-spelling uniqueness at both enforcement points (plan admission, ledger
        # load), as an earlier version or an edit leaves it: greet -> GREET published, paused before any worker.
        plan = self.plan()
        second = copy.deepcopy(plan["tasks"][0])
        second.update(id="GREET", depends_on=["greet"])
        plan["tasks"].append(second)
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with patch("harness.contracts.fold_name", str), patch("harness.run_lifecycle.fold_name", str, create=True):
            paused = engine.start("Implement greet", plan, pause_after_plan=True)
        self.assertEqual((paused["status"], [t["id"] for t in paused["tasks"]]), ("awaiting_approval", ["greet", "GREET"]))
        state_file = engine.directory / "state.json"
        folded = state_file.read_bytes()
        exact = read_json(state_file)
        exact["tasks"][1]["id"] = "greet"  # the control: the same ledger with an exact duplicate
        write_json(state_file, exact)
        refusals = {}
        for name, raw in (("exact", state_file.read_bytes()), ("folded", folded)):
            state_file.write_bytes(raw)
            for argv in (["resume", paused["id"], "--approve-plan"], ["resume", paused["id"]], ["abandon", paused["id"]]):
                with self.subTest(ledger=name, argv=argv):
                    code, errors = run_cli(self, backend, argv)
                    notice = cli_notice(errors)
                    self.assertIsNotNone(notice, errors)
                    refusals.setdefault(name, []).append(
                        (code, notice["kind"], notice["reason"], notice["data"], notice["source"], notice["exits"]))
                    self.assertEqual(state_file.read_bytes(), raw)
        self.assertEqual(refusals["exact"], [(1, "evidence", "load_integrity", {}, {}, [])] * 3)
        self.assertEqual(refusals["folded"], refusals["exact"])
        self.assertEqual(backend.calls, [])
        self.assertFalse((engine.directory / "verification").exists())

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

        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) <= 2:
                result.update(status="blocked", blocker_kind="task", blockers=["Clarify"], summary="Clarify")
            elif trigger_kind(kwargs, packet) == "task" and packet["trigger"]["feedback"]:
                greet = current_tasks(packet)[0]
                if sum(c["kind"] == "decide" for c in backend.calls) == 1:
                    revised_(result, packet, "Refine description", selected="greet",
                             updates=[{"id": "greet", "description": greet["description"] + " with clarification",
                                       "add_depends_on": [], "add_allowed_paths": [], "add_required_commands": []}])
                else:
                    revised_(result, packet, "Add followup", selected="followup",
                             new_tasks=[{**copy.deepcopy(greet), "id": "followup"}])
        engine = self.engine(ScriptedBackend(change))
        result = engine.start("Implement greet", plan)
        self.assertEqual(result["status"], "completed")
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
        emit = engine.context.event

        def interrupt(event, **fields):
            emit(event, **fields)
            if event == "broad_scope_warning":
                raise KeyboardInterrupt()

        with patch.object(engine.context, "event", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan)
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["tasks"], plan["tasks"])
        self.assertEqual(saved["original_tasks"], plan["tasks"])
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["original_tasks"], plan["tasks"])
        self.assertFalse(any(call["role"] == "commander" for call in backend.calls))

    def test_removal_diagnostic_lists_bounded_paths_and_preserves_all_files(self):
        engine = self.engine()
        engine.start("Implement greet", self.plan(), pause_after_plan=True)
        folder = engine.worktree / "evidence"
        folder.mkdir()
        names = [f"evidence-{index:02}.pyc" for index in range(12)]
        for name in names:
            (folder / name).write_bytes(b"preserve evidence")
        unusual = engine.worktree / "scratch\nfile"
        unusual.write_text("preserve scratch")
        self.assertEqual(self.engine().abandon(engine.run["id"])["status"], "abandoned")
        removal = cleanup_run(self.repo, engine.run["id"], remove=True)["worktree"]
        self.assertEqual(removal["status"], "blocked")
        message = removal["error"]
        self.assertIn("evidence/evidence-00.pyc", message)
        self.assertIn("evidence/evidence-09.pyc", message)
        self.assertNotIn("evidence-11.pyc", message)
        self.assertLess(len(message), 3000)
        for name in names:
            self.assertEqual((folder / name).read_bytes(), b"preserve evidence")
        self.assertEqual(unusual.read_text(), "preserve scratch")
        self.assertTrue(engine.worktree.is_dir())
        for name in names:
            (folder / name).unlink()
        folder.rmdir()
        removal = cleanup_run(self.repo, engine.run["id"], remove=True)["worktree"]
        self.assertEqual(removal["status"], "blocked")
        self.assertIn(repr(unusual.name), removal["error"])
        self.assertNotIn("\n", removal["error"])
