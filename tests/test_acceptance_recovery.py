"""Read and resume ordinary saved acceptance intents."""
from copy import deepcopy
from unittest.mock import patch

from harness import acceptance, git, stops
from harness.util import HarnessError, read_json, write_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend


def hold_acceptance(case, engine, plan, *, branch_at_commit=False, synced=False):
    """Pause the fixture after the first ledger save with an ordinary typed error."""
    pause = HarnessError("Fixture pause after saving the acceptance intent",
                         stop=stops.make("runtime_failure", "invariant"))
    with patch.object(acceptance, "move_branch", side_effect=pause):
        with case.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan)
    case.assertIs(caught.exception, pause)
    reader = case.engine()
    reader.load_run(engine.run["id"])
    pending = reader.run["pending_acceptance"]
    intent = pending["intent"]
    context = reader.context

    def advance():
        case.assertIsInstance(context.run_lock_descriptor, int)
        if branch_at_commit:
            acceptance.move_branch(context, intent, pending["task_id"])
        if synced:
            acceptance.sync_index(context, intent)

    path = reader.directory / "state.json"
    before = path.read_bytes()
    context.run_lock(stops.make("busy", "run_lock"), advance)
    case.assertEqual(path.read_bytes(), before)
    case.assertEqual(git.head(reader.worktree), intent["commit" if branch_at_commit else "parent"])
    return reader.run


class AcceptanceRecoveryTests(RepoCase):
    def hold(self, backend, **progress):
        engine = self.engine(backend)
        saved = hold_acceptance(self, engine, read_json(ROOT / "examples" / "plan.json"), **progress)
        return engine, saved

    def test_read_only_precheck_and_resume_each_normal_progress_point(self):
        for moved, synced in ((False, False), (True, False), (True, True)):
            with self.subTest(moved=moved, synced=synced):
                backend = ScriptedBackend()
                engine, saved = self.hold(backend, branch_at_commit=moved, synced=synced)
                pending = saved["pending_acceptance"]
                intent = pending["intent"]
                binding = saved["worktree_binding"]
                path = engine.directory / "state.json"
                before = path.read_bytes()
                index_before = git.index_digest(engine.worktree, binding=binding)
                calls_before = len(backend.calls)
                reader = self.engine(backend)
                reader.load_run(saved["id"])
                self.assertEqual(reader.run["pending_acceptance"], pending)

                def check():
                    self.assertIsInstance(reader.context.run_lock_descriptor, int)
                    reader.services.lifecycle.verify_head_and_inputs()
                    with self.assertRaises(HarnessError) as caught:
                        self.engine(backend).resume(saved["id"])
                    self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), ("busy", "run_lock"))
                    self.assertEqual(path.read_bytes(), before)

                reader.context.run_lock(stops.make("busy", "run_lock"), check)
                self.assertEqual(git.head(engine.worktree, binding=binding),
                                 intent["commit" if moved else "parent"])
                self.assertEqual(git.index_digest(engine.worktree, binding=binding), index_before)
                self.assertEqual(len(backend.calls), calls_before)

                with patch.object(git, "build_commit", wraps=git.build_commit) as build, \
                        patch.object(acceptance, "move_branch", wraps=acceptance.move_branch) as move, \
                        patch.object(acceptance, "sync_index", wraps=acceptance.sync_index) as sync:
                    result = reader.resume(saved["id"])
                build.assert_not_called()
                self.assertEqual(move.call_count, int(not moved))
                self.assertEqual(sync.call_count, int(not synced))
                self.assertEqual(result["status"], "completed")
                self.assertNotIn("pending_acceptance", result)
                self.assertEqual(result["expected_head"], intent["commit"])
                self.assertEqual(result["accepted"]["greet"]["tree"], intent["tree"])
                self.assertEqual(git.index_digest(engine.worktree, binding=binding), intent["index_synced"])
                self.assertEqual(git.git(self.repo, "rev-list", "--count",
                                         intent["parent"] + ".." + intent["commit"]).strip(), "1")
                self.assertEqual([(call["role"], call["kind"]) for call in backend.calls[calls_before:]],
                                 [("reviewer", "final_review")])
                after = path.read_bytes()
                self.assertEqual(reader.resume(saved["id"]), result)
                self.assertEqual(path.read_bytes(), after)

    def test_invalid_ledger_is_refused_before_git_or_a_state_write(self):
        backend = ScriptedBackend()
        engine, saved = self.hold(backend, branch_at_commit=True)
        path = engine.directory / "state.json"
        calls_before = len(backend.calls)
        cases = [
            ("intent", lambda run, intent: run["pending_acceptance"].update(intent=None)),
            ("intent", lambda run, intent: intent.pop("files")),
            ("intent", lambda run, intent: intent.update(extra=True)),
            ("intent.ref", lambda run, intent: intent.update(ref="refs/heads/main")),
            ("intent.parent", lambda run, intent: intent.update(parent=intent["commit"])),
            ("intent.tree", lambda run, intent: intent.update(tree="a")),
            ("intent.commit", lambda run, intent: intent.update(commit="A" * len(intent["parent"]))),
            ("intent.files", lambda run, intent: intent.update(files="hello.py")),
            ("intent.files", lambda run, intent: intent.update(files=[True])),
            ("intent.index_before", lambda run, intent: intent.update(index_before="f" * 63)),
            ("intent.index_synced", lambda run, intent: intent.update(index_synced="G" * 64)),
            ("accepted.greet.tree", lambda run, intent: run["accepted"].update(greet={})),
            ("accepted.greet.tree", lambda run, intent: run["accepted"].update(greet={"tree": []})),
        ]
        for field, mutate in cases:
            with self.subTest(field=field):
                damaged = deepcopy(saved)
                mutate(damaged, damaged["pending_acceptance"]["intent"])
                write_json(path, damaged)
                before = path.read_bytes()
                reader = self.engine(backend)
                with patch.object(acceptance, "state", wraps=acceptance.state) as git_state:
                    for load in (reader.load_run, reader.resume):
                        with self.assertRaises(HarnessError) as caught:
                            load(saved["id"])
                        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), ("evidence", "load_integrity"))
                        self.assertIn(field, str(caught.exception))
                        self.assertEqual(path.read_bytes(), before)
                    git_state.assert_not_called()
        self.assertEqual(len(backend.calls), calls_before)

    def test_valid_shape_with_index_mismatch_preserves_the_intent(self):
        backend = ScriptedBackend()
        engine, saved = self.hold(backend, branch_at_commit=True)
        pending = saved["pending_acceptance"]
        intent = pending["intent"]
        path = engine.directory / "state.json"
        binding = saved["worktree_binding"]
        actual_index = git.index_digest(engine.worktree, binding=binding)
        different_digest = ("1" if actual_index.startswith("0") else "0") * 64
        intent.update(index_before=different_digest, index_synced=different_digest)
        write_json(path, saved)
        before = path.read_bytes()
        calls_before = len(backend.calls)
        reader = self.engine(backend)
        reader.load_run(saved["id"])
        self.assertEqual(reader.run["pending_acceptance"], pending)
        with patch.object(git, "build_commit", wraps=git.build_commit) as build, \
                patch.object(acceptance, "move_branch", wraps=acceptance.move_branch) as move, \
                patch.object(acceptance, "sync_index", wraps=acceptance.sync_index) as sync:
            with self.assertRaises(HarnessError) as caught:
                reader.resume(saved["id"])
        stop = caught.exception.stop
        self.assertEqual((stop["kind"], stop["reason"]), ("workspace_mismatch", "candidate_changed"))
        self.assertEqual(stop["source"]["task_id"], pending["task_id"])
        build.assert_not_called()
        move.assert_not_called()
        sync.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(git.head(engine.worktree, binding=binding), intent["commit"])
        self.assertEqual(git.index_digest(engine.worktree, binding=binding), actual_index)
        self.assertEqual(len(backend.calls), calls_before)
