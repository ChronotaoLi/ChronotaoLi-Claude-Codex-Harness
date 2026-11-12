"""Description refinement retains one owned candidate across durable boundaries."""
import copy
import hashlib
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, finding, revised, trigger_kind


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
                self.case.assertNotIn("pending_directive", state)
                self.case.assertEqual(state["current_task"], "greet")
                entry = next(d for d in state["dispatches"] if d["id"] == kwargs["job"].name)
                self.case.assertIs(entry["invoked"], True)
                self.retained_seen.append(sum(state["attempts"].values()))
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
        if trigger_kind(kwargs, packet) == "task" and packet["trigger"]["feedback"] and len(packet.get("accepted", {})) == 0:
            updates = [{"id": "greet", "description": "Refined execution detail for the same greeting acceptance",
                        "add_depends_on": ["prepare"] if self.prerequisite else []}]
            new_tasks = []
            if self.prerequisite:
                prerequisite = copy.deepcopy(self.case.plan()["tasks"][0])
                prerequisite.update(id="prepare", title="Prepare the greeting prerequisite")
                new_tasks.append(prerequisite)
            revised(result, packet, "Clarify execution detail", new_tasks=new_tasks, updates=updates,
                    selected="prepare" if self.prerequisite else "greet",
                    instruction="Refine this task without changing its acceptance")


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
        self.assertEqual(list((engine.directory / "recovery").glob("revision-*")), [])

    def interrupted(self, boundary="pending", *, rejected=False, two_ready=False):
        """Stop after the revision landed (pending) or after it was published."""
        backend = DescriptionBackend(self, rejected=rejected)
        engine = self.engine(backend)
        planning = engine.services.planning
        if boundary == "pending":
            interruption = patch.object(planning, "apply_pending_revision", side_effect=KeyboardInterrupt())
        else:
            original = planning.apply_pending_revision

            def publish():
                original()
                raise KeyboardInterrupt()
            interruption = patch.object(planning, "apply_pending_revision", side_effect=publish)
        with interruption, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greeting", self.plan(two_ready=two_ready))
        self.assert_candidate(engine.worktree)
        self.assert_no_archive(engine)
        return engine, backend

    def plan_file(self, engine, revision=1):
        return read_json(engine.directory / "plans" / f"revision-{revision}.json")

    def assert_stops_without_reset_or_calls(self, engine, backend, pattern=""):
        calls = copy.deepcopy(backend.calls)
        resumed = self.engine(backend)
        with patch("harness.planning.git.archive_and_reset") as archive, self.assertRaisesRegex(HarnessError, pattern):
            resumed.resume(engine.run["id"])
        archive.assert_not_called()
        self.assertEqual(backend.calls, calls)
        self.assert_no_archive(engine)
        return resumed

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
        self.assertNotIn("pending_directive", result)
        self.assert_no_archive(engine)

    def test_rejected_candidate_gets_fresh_verification_and_review(self):
        backend = DescriptionBackend(self, rejected=True)
        engine = self.engine(backend)
        result = engine.start("Implement greeting", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(result["attempts"].values()), 2)
        self.assertEqual(len(result["plan_history"]), 2)
        self.assertEqual(self.plan_file(engine)["candidate"], "retain")
        reviews = result["review_history"]["greet"]
        self.assertEqual([item["review"]["verdict"] for item in reviews], ["request_changes", "approve"])
        self.assertNotEqual(reviews[0]["artifact"], reviews[1]["artifact"])
        reviewers = [d for d in result["dispatches"] if d["role"] == "reviewer" and d["kind"] == "review"]
        self.assertEqual(len({d["id"] for d in reviewers}), 2)
        verifications = [d for d in result["dispatches"] if d["role"] == "verification" and d["task_id"] == "greet"]
        self.assertEqual(len(verifications), 2)
        self.assertIn("fresh worker edit", (engine.worktree / "hello.py").read_text())
        self.assert_no_archive(engine)

    def test_interruption_before_publication_reuses_landed_revision(self):
        engine, backend = self.interrupted()
        saved = read_json(engine.directory / "state.json")
        pending = saved["pending_revision"]
        self.assertEqual(set(pending), {"revision", "artifact", "artifact_digest"})
        self.assertEqual(pending["revision"], 1)
        self.assertNotIn("commander:decide:run", saved["commander_receipts"])
        content = self.plan_file(engine)
        self.assertEqual(content["candidate"], "retain")
        self.assertEqual(content["directive"]["selected_task"], "greet")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(call["role"] == "commander" for call in backend.calls), 1)
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(backend.retained_seen, [2])
        self.assert_no_archive(engine)

    def test_published_revision_binds_directive_task_and_retained_snapshot(self):
        engine, backend = self.interrupted("published", two_ready=True)
        saved = read_json(engine.directory / "state.json")
        content = self.plan_file(engine)
        self.assertNotIn("pending_revision", saved)
        self.assertEqual(saved["plan_history"][-1], {"revision": 1, "artifact": content and str(
            engine.directory / "plans" / "revision-1.json"), "artifact_digest": saved["plan_history"][-1]["artifact_digest"]})
        self.assertEqual(set(saved["plan_history"][-1]), {"revision", "artifact", "artifact_digest"})
        self.assertEqual(saved["current_task"], "greet")
        self.assertEqual(saved["pending_directive"], {"directive": content["directive"],
                                                      "snapshot_digest": content["snapshot_digest"], "revision": 1})
        self.assertEqual(saved["tasks"], content["tasks"])
        result = self.engine(backend).resume(engine.run["id"], step=True)
        self.assertEqual(set(result["accepted"]), {"greet"})
        self.assertEqual(sum(result["attempts"].values()), 2)
        # One decide selected among two ready tasks, one revised after the blocker.
        self.assertEqual(sum(call["role"] == "commander" for call in backend.calls), 2)
        # The next model call after the revision was the retained task's worker.
        roles = [call["role"] for call in backend.calls]
        self.assertEqual(roles[len(roles) - roles[::-1].index("commander")], "worker")
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
                snapshot = engine.context.snapshot()
                resumed = self.assert_stops_without_reset_or_calls(engine, backend, "修订保留候选的代码或暂存区已变化")
                self.assertEqual(resumed.context.snapshot(), snapshot)
                self.assertIn("pending_revision", read_json(engine.directory / "state.json"))

    def test_published_guard_rejects_later_candidate_mutation_before_dispatch(self):
        engine, backend = self.interrupted("published")
        (engine.worktree / "notes.txt").unlink()
        self.assert_stops_without_reset_or_calls(engine, backend, "待派发指令的代码快照已变化")
        self.assertEqual((engine.worktree / "hello.py").read_text(), CANDIDATE)

    def test_missing_or_changed_plan_or_original_stops_without_new_calls(self):
        for boundary, target, change in (("pending", "plan", "changed"), ("pending", "plan", "missing"),
                                         ("pending", "original", "changed"), ("published", "plan", "changed"),
                                         ("published", "plan", "missing")):
            with self.subTest(boundary=boundary, target=target, change=change):
                engine, backend = self.interrupted(boundary)
                content = self.plan_file(engine)
                artifact = (engine.directory / "plans" / "revision-1.json" if target == "plan"
                            else Path(content["source"]["artifact"]))
                if change == "missing":
                    artifact.unlink()
                else:
                    artifact.write_bytes(artifact.read_bytes() + b"\n")
                self.assert_stops_without_reset_or_calls(engine, backend)
                self.assert_candidate(engine.worktree)

    def test_pending_revision_replays_original_against_ledger_before_any_reset(self):
        for change in ("acceptance", "current_task", "plan_directive"):
            with self.subTest(change=change):
                engine, backend = self.interrupted(two_ready=True)
                saved = read_json(engine.directory / "state.json")
                if change == "acceptance":
                    saved["tasks"][0]["acceptance"].append("An acceptance the revision never saw")
                elif change == "current_task":
                    saved["current_task"] = None
                write_json(engine.directory / "state.json", saved)
                if change == "plan_directive":
                    # A file that is consistent with its own digest but not with the original.
                    path = engine.directory / "plans" / "revision-1.json"
                    content = read_json(path)
                    content["directive"]["instruction"] = "A different instruction"
                    write_json(path, content)
                    saved["pending_revision"]["artifact_digest"] = hashlib.sha256(path.read_bytes()).hexdigest()
                    write_json(engine.directory / "state.json", saved)
                self.assert_stops_without_reset_or_calls(engine, backend)
                self.assert_candidate(engine.worktree)

    def test_published_directive_rejects_ledger_tampering_and_task_rebinding(self):
        for change in ("description", "acceptance", "scope", "current_task", "directive"):
            with self.subTest(change=change):
                engine, backend = self.interrupted("published", two_ready=True)
                saved = read_json(engine.directory / "state.json")
                if change == "directive":
                    saved.pop("pending_directive")
                elif change == "current_task":
                    saved["current_task"] = "other"
                elif change == "description":
                    saved["tasks"][0]["description"] = "A different description absent from the original revision"
                elif change == "scope":
                    saved["tasks"][0]["allowed_paths"] = ["hello.py"]
                else:
                    saved["tasks"][0]["acceptance"].append("An unexplained acceptance change")
                write_json(engine.directory / "state.json", saved)
                self.assert_stops_without_reset_or_calls(engine, backend)
                self.assert_candidate(engine.worktree)

    def test_head_or_git_binding_change_stops_without_archiving_candidate(self):
        for change in ("head", "binding"):
            with self.subTest(change=change):
                engine, backend = self.interrupted()
                if change == "head":
                    git.git(engine.worktree, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                            "-c", "commit.gpgsign=false", "commit", "--only", "--allow-empty", "-m", "external commit")
                else:
                    gitfile = engine.worktree / ".git"
                    gitfile.write_bytes(gitfile.read_bytes() + b"\n")
                self.assert_stops_without_reset_or_calls(engine, backend)
                self.assertEqual((engine.worktree / "hello.py").read_text(), CANDIDATE)
                self.assertEqual((engine.worktree / "notes.txt").read_text(), NOTES)

    def test_pre_agent_preparation_failure_keeps_directive_and_invocation_budgets(self):
        engine, backend = self.interrupted("published")
        saved = read_json(engine.directory / "state.json")
        resumed = self.engine(backend)

        def fail_worker(event, payload, logs):
            if event == "pre_agent" and payload["role"] == "worker":
                raise HarnessError("local worker preparation unavailable")

        with patch.object(resumed.hooks, "emit", side_effect=fail_worker), self.assertRaisesRegex(
                HarnessError, "local worker preparation"):
            resumed.resume(engine.run["id"])
        after = read_json(engine.directory / "state.json")
        self.assertEqual(after["pending_directive"], saved["pending_directive"])
        self.assertEqual(after["attempts"], saved["attempts"])
        self.assertEqual(after["agent_dispatches"], saved["agent_dispatches"])
        self.assertEqual(backend.calls[-1]["role"], "commander")
        self.assertEqual(after["agent_dispatches"], sum(d.get("invoked", True) for d in after["dispatches"]
                                                      if d["role"] in ("worker", "reviewer", "commander")))
        self.assertEqual(len(backend.worker_ids), 1)
        self.assert_candidate(engine.worktree)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(backend.retained_seen, [2])
        self.assertNotIn("pending_directive", result)

    def test_pre_agent_hook_cannot_replace_retained_candidate(self):
        for target in ("bytes", "index"):
            with self.subTest(target=target):
                engine, backend = self.interrupted("published")
                saved = read_json(engine.directory / "state.json")
                resumed = self.engine(backend)
                emit = resumed.hooks.emit

                def mutate(event, payload, logs):
                    if event == "pre_agent" and payload["role"] == "worker":
                        if target == "bytes":
                            (resumed.worktree / "hello.py").write_text(CANDIDATE + "# hook edit\n")
                        else:
                            git.git(resumed.worktree, "add", "hello.py")
                    return emit(event, payload, logs)
                with patch.object(resumed.hooks, "emit", side_effect=mutate), self.assertRaisesRegex(
                        HarnessError, "待派发指令与已发布计划或候选不符"):
                    resumed.resume(engine.run["id"])
                after = read_json(engine.directory / "state.json")
                self.assertEqual(after["pending_directive"], saved["pending_directive"])
                self.assertEqual((after["attempts"], after["agent_dispatches"]), (saved["attempts"], saved["agent_dispatches"]))
                entry = after["dispatches"][-1]
                self.assertEqual((entry["role"], entry["invoked"]), ("worker", False))
                self.assertEqual(len(backend.worker_ids), 1)
                # Restoring the bound candidate lets the same directive dispatch.
                if target == "bytes":
                    (engine.worktree / "hello.py").write_text(CANDIDATE)
                else:
                    git.git(engine.worktree, "reset", "-q", "hello.py")
                    git.git(engine.worktree, "add", "hello.py")
                    (engine.worktree / "hello.py").write_text(STAGED)
                    git.git(engine.worktree, "add", "hello.py")
                    (engine.worktree / "hello.py").write_text(CANDIDATE)
                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")

    def test_implement_directive_still_lets_pre_agent_hook_edit_candidate(self):
        # Contrast: only retained and rejection directives lock the candidate after pre_agent.
        engine = self.engine(ScriptedBackend())
        emit = engine.hooks.emit

        def mutate(event, payload, logs):
            if event == "pre_agent" and payload["role"] == "worker":
                (engine.worktree / "notes.txt").write_text("prepared by hook\n")
            return emit(event, payload, logs)
        with patch.object(engine.hooks, "emit", side_effect=mutate):
            result = engine.start("Implement greeting", self.plan(two_ready=True), step=True)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(set(result["accepted"]), {"greet"})

    def test_new_prerequisite_still_archives_candidate_before_other_task(self):
        backend = DescriptionBackend(self, prerequisite=True)
        engine = self.engine(backend)
        result = engine.start("Implement greeting", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2, "prepare": 1})
        archive = engine.directory / "recovery" / "revision-1"
        self.assertEqual((archive / "files" / "hello.py").read_text(), CANDIDATE)
        self.assertEqual((archive / "files" / "notes.txt").read_text(), NOTES)
        content = self.plan_file(engine)
        self.assertEqual(content["candidate"], "archive")
        self.assertNotIn("pending_directive", result)
