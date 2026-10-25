"""Task phase ownership keeps conflicting recovery records from accepting code."""
from copy import deepcopy
from unittest.mock import patch

from harness import git
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class TaskPhaseHandoffTests(RepoCase):
    def test_conflicting_acceptance_phase_preserves_commit_and_returned_review(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        emit = engine.hooks.emit

        def stop_before_commit(event, payload, logs):
            if event == "pre_accept":
                raise HarnessError("acceptance gate unavailable")
            return emit(event, payload, logs)

        with patch.object(engine.hooks, "emit", side_effect=stop_before_commit):
            with self.assertRaises(HarnessError):
                engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        path = engine.directory / "state.json"
        saved = read_json(path)
        original_head = saved["expected_head"]
        acceptance = deepcopy(saved["pending_acceptance"])
        review_bytes = (engine.directory / "verification/tasks/greet/1/review.json").read_bytes()
        calls = len(backend.calls)
        # Both records are individually well-shaped task references. Their
        # combination must be rejected before it can consume the approval.
        saved["pending_task"] = {"task_id": "greet", "stage": "reviewing"}
        write_json(path, saved)
        with patch("harness.task_lifecycle.git.accept", wraps=git.accept) as accept:
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(saved["id"])
        accept.assert_not_called()
        blocked = read_json(path)
        self.assertEqual(blocked["pending_acceptance"], acceptance)
        self.assertEqual(blocked["accepted"], {})
        self.assertEqual(git.head(engine.worktree), original_head)
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual((engine.directory / "verification/tasks/greet/1/review.json").read_bytes(), review_bytes)
        # Restore the injected corruption; the same durable approval can finish.
        blocked.pop("pending_task")
        write_json(path, blocked)
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)
        self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 1)

    def test_commit_and_post_accept_work_survive_same_save_interruption(self):
        engine = self.engine()
        save = engine.save
        interrupted = False

        def stop_after_handoff():
            nonlocal interrupted
            save()
            if engine.run.get("pending_post_accept") and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(engine, "save", side_effect=stop_after_handoff):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        saved = read_json(engine.directory / "state.json")
        accepted = saved["accepted"]["greet"]
        self.assertEqual(saved["expected_head"], accepted["commit"])
        self.assertEqual(git.head(engine.worktree), accepted["commit"])
        self.assertNotIn("pending_acceptance", saved)
        self.assertNotIn("pending_task", saved)
        self.assertEqual(saved["pending_post_accept"]["payload"]["commit"], accepted["commit"])
        self.assertEqual(engine.memory.export(), [])
        # Reuse the Engine: components must follow load_run's replacement dict.
        result = engine.resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["accepted"]["greet"]["commit"], accepted["commit"])
        self.assertEqual(result["total_attempts"], 1)
        self.assertEqual(len(engine.memory.export()), 1)
