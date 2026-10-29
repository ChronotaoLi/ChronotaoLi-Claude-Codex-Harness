"""Description refinement retains one owned candidate across durable boundaries."""
import copy
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, finding


STAGED = 'def greet(name):\n    return f"Hello, {name}!"\n# retained staged candidate\n'
CANDIDATE = STAGED + "# retained unstaged candidate\n"
NOTES = "untracked evidence from the original worker\n"


class DescriptionBackend(ScriptedBackend):
    def __init__(self, case, *, rejected=False, prerequisite=False):
        super().__init__(self.change_result)
        self.case, self.rejected, self.prerequisite = case, rejected, prerequisite
        self.worker_ids = []
        self.retained_seen = []

    def invoke(self, **kwargs):
        if kwargs["role"] == "worker":
            state = read_json(kwargs["run_path"])
            if self.worker_ids and not self.prerequisite:
                self.case.assertEqual(kwargs["task"]["id"], "greet")
                self.case.assert_candidate(kwargs["cwd"])
                self.case.assertNotIn("retained_candidate", state)
                self.case.assertEqual(state["current_task"], "greet")
                entry = next(d for d in state["dispatches"] if d["id"] == kwargs["job"].name)
                self.case.assertIs(entry["invoked"], True)
                self.case.assertEqual(entry["retained_candidate_revision"], len(state["plan_history"]) - 1)
                self.retained_seen.append(state["total_attempts"])
            if self.worker_ids and self.prerequisite and kwargs["task"]["id"] == "prepare":
                self.case.assertFalse((kwargs["cwd"] / "hello.py").exists())
                self.case.assertFalse((kwargs["cwd"] / "notes.txt").exists())
            self.worker_ids.append(kwargs["job"].name)
        return super().invoke(**kwargs)

    def change_result(self, backend, kwargs, packet, result):
        if kwargs["role"] == "worker" and len(self.worker_ids) == 1:
            (kwargs["cwd"] / "hello.py").write_text(STAGED)
            git.git(kwargs["cwd"], "add", "hello.py")
            (kwargs["cwd"] / "hello.py").write_text(CANDIDATE)
            (kwargs["cwd"] / "notes.txt").write_text(NOTES)
            if not self.rejected:
                result.update(status="blocked", blocker_kind="task", blockers=["Clarify the same task"],
                              summary="Candidate needs description refinement")
        elif kwargs["role"] == "worker":
            with (kwargs["cwd"] / "hello.py").open("a") as stream:
                stream.write("# fresh worker edit after retained guard consumption\n")
        if self.rejected and kwargs["kind"] == "review" and sum(
                call["kind"] == "review" for call in self.calls) == 1:
            result.update(verdict="request_changes", findings=[finding()])
        if self.rejected and kwargs["kind"] == "dispatch" and any(
                call["kind"] == "review" for call in self.calls) and not any(
                call["kind"] == "replan" for call in self.calls):
            result.update(action="replan", instruction="Refine this task without changing its acceptance")
        if kwargs["kind"] == "replan":
            updates = [{"id": "greet", "description": "Refined execution detail for the same greeting acceptance",
                        "add_depends_on": ["prepare"] if self.prerequisite else []}]
            new_tasks = []
            if self.prerequisite:
                prerequisite = copy.deepcopy(self.case.plan()["tasks"][0])
                prerequisite.update(id="prepare", title="Prepare the greeting prerequisite")
                new_tasks.append(prerequisite)
            result.update(action="revise", reason="Clarify execution detail", new_tasks=new_tasks, updates=updates)


class DescriptionReplanTests(RepoCase):
    def plan(self, *, two_ready=False):
        plan = read_json(ROOT / "examples/plan.json")
        plan["tasks"][0]["allowed_paths"].extend(["notes.txt", "extra.txt"])
        if two_ready:
            other = copy.deepcopy(plan["tasks"][0])
            other.update(id="other", title="Another independently ready task", allowed_paths=["other.txt"])
            plan["tasks"].append(other)
        return plan

    def assert_candidate(self, worktree):
        self.assertEqual((worktree / "hello.py").read_text(), CANDIDATE)
        self.assertEqual(git.git(worktree, "show", ":hello.py"), STAGED)
        self.assertEqual((worktree / "notes.txt").read_text(), NOTES)
        self.assertIn("notes.txt", git.git(worktree, "ls-files", "--others", "--exclude-standard"))

    def assert_no_archive(self, engine):
        self.assertEqual(list((engine.directory / "recovery").glob("replan-*")), [])

    def interrupted(self, boundary="validated", *, rejected=False, two_ready=False):
        backend = DescriptionBackend(self, rejected=rejected)
        engine = self.engine(backend)
        if boundary == "validated":
            interruption = patch.object(engine, "apply_pending_revision", side_effect=KeyboardInterrupt())
        else:
            original = engine.record_plan

            def record(*args, **kwargs):
                original(*args, **kwargs)
                if kwargs.get("finish_replan"):
                    raise KeyboardInterrupt()

            interruption = patch.object(engine, "record_plan", side_effect=record)
        with interruption, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greeting", self.plan(two_ready=two_ready))
        self.assert_candidate(engine.worktree)
        self.assert_no_archive(engine)
        return engine, backend

    def test_two_ready_tasks_keep_candidate_bound_to_its_original_task(self):
        backend = DescriptionBackend(self)
        engine = self.engine(backend)
        original = self.plan(two_ready=True)
        result = engine.start("Implement greeting", original, step=True)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(set(result["accepted"]), {"greet"})
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(backend.retained_seen, [2])
        self.assertEqual(len(set(backend.worker_ids)), 2)
        self.assertEqual(result["tasks"][1], original["tasks"][1])
        self.assertEqual(result["original_tasks"], original["tasks"])
        self.assertNotIn("retained_candidate", result)
        self.assert_no_archive(engine)

    def test_rejected_candidate_gets_fresh_verification_and_review(self):
        backend = DescriptionBackend(self, rejected=True)
        engine = self.engine(backend)
        result = engine.start("Implement greeting", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_attempts"], 2)
        self.assertEqual(result["replan_attempts"], 1)
        reviews = result["review_history"]["greet"]
        self.assertEqual([item["review"]["verdict"] for item in reviews], ["request_changes", "approve"])
        self.assertNotEqual(reviews[0]["artifact"], reviews[1]["artifact"])
        reviewers = [d for d in result["dispatches"] if d["role"] == "reviewer" and d["kind"] == "review"]
        self.assertEqual(len({d["id"] for d in reviewers}), 2)
        verifications = [d for d in result["dispatches"] if d["role"] == "verification" and d["task_id"] == "greet"]
        self.assertEqual(len(verifications), 2)
        self.assertIn("fresh worker edit", (engine.worktree / "hello.py").read_text())
        self.assert_no_archive(engine)

    def test_interruption_before_application_reuses_validated_revision(self):
        engine, backend = self.interrupted()
        pending = read_json(engine.directory / "state.json")["pending_replan"]
        self.assertEqual(pending["stage"], "validated")
        self.assertEqual(pending["candidate_policy"], "retain")
        self.assertEqual(pending["retained_candidate"]["task_id"], "greet")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(call["kind"] == "replan" for call in backend.calls), 1)
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(backend.retained_seen, [2])
        self.assert_no_archive(engine)

    def test_interruption_after_record_plan_keeps_guard_and_current_task(self):
        engine, backend = self.interrupted("applied", two_ready=True)
        saved = read_json(engine.directory / "state.json")
        self.assertNotIn("pending_replan", saved)
        self.assertEqual(saved["current_task"], "greet")
        self.assertEqual(saved["retained_candidate"], saved["plan_history"][-1]["retained_candidate"])
        result = self.engine(backend).resume(engine.run["id"], step=True)
        self.assertEqual(set(result["accepted"]), {"greet"})
        self.assertEqual(result["total_attempts"], 2)
        self.assertEqual(sum(call["kind"] == "replan" for call in backend.calls), 1)
        self.assert_no_archive(engine)

    def test_changed_bytes_index_or_untracked_file_stop_without_reset_or_calls(self):
        for mutation in ("bytes", "index", "untracked", "new_untracked"):
            with self.subTest(mutation=mutation):
                engine, backend = self.interrupted()
                if mutation == "bytes":
                    (engine.worktree / "hello.py").write_text(CANDIDATE + "# outside edit\n")
                elif mutation == "index":
                    git.git(engine.worktree, "add", "hello.py")
                elif mutation == "untracked":
                    (engine.worktree / "notes.txt").write_text(NOTES + "changed\n")
                else:
                    (engine.worktree / "extra.txt").write_text("An additional in-scope untracked file\n")
                snapshot = engine.snapshot()
                calls = copy.deepcopy(backend.calls)
                with patch("harness.planning.git.archive_and_reset") as archive, self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                archive.assert_not_called()
                self.assertEqual(engine.snapshot(), snapshot)
                self.assertEqual(backend.calls, calls)
                self.assert_no_archive(engine)

    def test_applied_guard_rejects_later_candidate_mutation_before_dispatch(self):
        engine, backend = self.interrupted("applied")
        (engine.worktree / "notes.txt").unlink()
        calls = copy.deepcopy(backend.calls)
        with patch("harness.planning.git.archive_and_reset") as archive, self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        archive.assert_not_called()
        self.assertEqual(backend.calls, calls)
        self.assertEqual((engine.worktree / "hello.py").read_text(), CANDIDATE)
        self.assert_no_archive(engine)

    def test_missing_or_changed_bound_replan_result_stops_without_new_calls(self):
        for change in ("missing", "changed"):
            with self.subTest(change=change):
                engine, backend = self.interrupted("applied")
                guard = read_json(engine.directory / "state.json")["retained_candidate"]
                artifact = Path(guard["artifact"])
                if change == "missing":
                    artifact.unlink()
                else:
                    artifact.write_bytes(artifact.read_bytes() + b"\n")
                calls = copy.deepcopy(backend.calls)
                with patch("harness.planning.git.archive_and_reset") as archive, self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                archive.assert_not_called()
                self.assertEqual(backend.calls, calls)
                self.assert_candidate(engine.worktree)

    def test_guard_reconstructs_original_revision_and_rejects_task_rebinding(self):
        for change in ("description", "acceptance", "scope", "current_task", "guard"):
            with self.subTest(change=change):
                engine, backend = self.interrupted("applied", two_ready=True)
                saved = read_json(engine.directory / "state.json")
                if change == "guard":
                    saved.pop("retained_candidate")
                elif change == "current_task":
                    saved["current_task"] = "other"
                else:
                    for tasks in (saved["tasks"], saved["plan_history"][-1]["tasks"]):
                        if change == "description":
                            tasks[0]["description"] = "A different description absent from the original replan"
                        elif change == "scope":
                            tasks[0]["allowed_paths"] = ["hello.py"]
                        else:
                            tasks[0]["acceptance"].append("An unexplained acceptance change")
                    if change == "scope":
                        saved["plan_history"][0]["tasks"][0]["allowed_paths"] = ["hello.py"]
                write_json(engine.directory / "state.json", saved)
                calls = copy.deepcopy(backend.calls)
                with patch("harness.planning.git.archive_and_reset") as archive, self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                archive.assert_not_called()
                self.assertEqual(backend.calls, calls)
                self.assert_candidate(engine.worktree)

    def test_head_or_git_binding_change_stops_without_archiving_candidate(self):
        for change in ("head", "binding"):
            with self.subTest(change=change):
                engine, backend = self.interrupted("applied")
                if change == "head":
                    git.git(engine.worktree, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                            "-c", "commit.gpgsign=false", "commit", "--only", "--allow-empty", "-m", "external commit")
                else:
                    gitfile = engine.worktree / ".git"
                    gitfile.write_bytes(gitfile.read_bytes() + b"\n")
                calls = copy.deepcopy(backend.calls)
                with patch("harness.planning.git.archive_and_reset") as archive, self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                archive.assert_not_called()
                self.assertEqual(backend.calls, calls)
                self.assertEqual((engine.worktree / "hello.py").read_text(), CANDIDATE)
                self.assertEqual((engine.worktree / "notes.txt").read_text(), NOTES)

    def test_pre_agent_preparation_failure_keeps_guard_and_invocation_budgets(self):
        engine, backend = self.interrupted("applied")
        saved = read_json(engine.directory / "state.json")
        resumed = self.engine(backend)

        def fail_worker(event, payload, logs):
            if event == "pre_agent" and payload["role"] == "worker":
                raise HarnessError("local worker preparation unavailable")

        with patch.object(resumed.hooks, "emit", side_effect=fail_worker), self.assertRaisesRegex(
                HarnessError, "local worker preparation"):
            resumed.resume(engine.run["id"])
        after = read_json(engine.directory / "state.json")
        self.assertEqual(after["retained_candidate"], saved["retained_candidate"])
        for key in ("attempts", "total_attempts"):
            self.assertEqual(after[key], saved[key])
        self.assertEqual(after["agent_dispatches"], saved["agent_dispatches"] + 1)
        self.assertEqual(backend.calls[-1]["role"], "commander")
        self.assertEqual(backend.calls[-1]["kind"], "dispatch")
        self.assertEqual(after["agent_dispatches"], sum(d.get("invoked", True) for d in after["dispatches"]
                                                      if d["role"] in ("worker", "reviewer", "commander")))
        self.assertEqual(len(backend.worker_ids), 1)
        self.assert_candidate(engine.worktree)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(backend.retained_seen, [2])
        self.assertNotIn("retained_candidate", result)

    def test_legacy_validated_revision_without_retention_policy_still_archives(self):
        engine, backend = self.interrupted()
        saved = read_json(engine.directory / "state.json")
        saved["pending_replan"].pop("candidate_policy")
        saved["pending_replan"].pop("retained_candidate")
        write_json(engine.directory / "state.json", saved)
        backend.prerequisite = True  # The next worker should see a fresh archived candidate.
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        archive = engine.directory / "recovery" / "replan-1"
        self.assertEqual((archive / "files" / "hello.py").read_text(), CANDIDATE)
        self.assertEqual((archive / "files" / "notes.txt").read_text(), NOTES)
        self.assertNotIn("retained_candidate", result)

    def test_new_prerequisite_still_archives_candidate_before_other_task(self):
        backend = DescriptionBackend(self, prerequisite=True)
        engine = self.engine(backend)
        result = engine.start("Implement greeting", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2, "prepare": 1})
        archive = engine.directory / "recovery" / "replan-1"
        self.assertEqual((archive / "files" / "hello.py").read_text(), CANDIDATE)
        self.assertEqual((archive / "files" / "notes.txt").read_text(), NOTES)
        self.assertNotIn("retained_candidate", result)
