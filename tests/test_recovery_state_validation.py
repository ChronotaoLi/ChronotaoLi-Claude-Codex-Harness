import copy
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.util import HarnessError, read_json, write_json
from test_engine_repairs import RecordingBackend
from test_harness import ROOT, RepoCase


class RecoveryStateValidationTests(RepoCase):
    def blocked_review(self):
        """Stop on a retained review receipt: the review's post_agent hook fails."""
        backend = RecordingBackend()
        engine = self.engine(backend)
        emit = engine.hooks.emit

        def fail(event, payload, logs):
            if event == "post_agent" and payload.get("kind") == "review":
                raise HarnessError("policy gate unavailable")
            return emit(event, payload, logs)

        with patch.object(engine.hooks, "emit", side_effect=fail):
            with self.assertRaisesRegex(HarnessError, "policy gate unavailable"):
                engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.engine_before = engine
        self.backend = backend
        self.saved = copy.deepcopy(engine.run)
        self.key = "reviewer:review:greet"
        self.receipt = copy.deepcopy(self.saved["review_receipts"][self.key])

    def assert_stops_before_resume(self, state):
        path = self.engine_before.directory / "state.json"
        write_json(path, state)
        original = path.read_bytes()
        calls = copy.deepcopy(self.backend.calls)
        head = git.head(self.engine_before.worktree)
        events = self.engine_before.directory / "events.jsonl"
        original_events = events.read_bytes()
        memories = self.engine_before.memory.export()
        with self.assertRaisesRegex(HarnessError, "运行台账恢复状态无效"):
            self.engine(self.backend).resume(state["id"])
        self.assertEqual(self.backend.calls, calls)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(git.head(self.engine_before.worktree), head)
        self.assertEqual(events.read_bytes(), original_events)
        self.assertEqual(self.engine_before.memory.export(), memories)

    def test_task_references_stop_before_resume_side_effects(self):
        self.blocked_review()
        for field in ("pending_task", "pending_acceptance", "current_task"):
            for ident in ("ghost", "", [], {}, 1, None):
                if field == "current_task" and ident is None:
                    continue  # An idle run legitimately has no current task.
                with self.subTest(field=field, ident=ident):
                    state = copy.deepcopy(self.saved)
                    if field == "current_task":
                        state[field] = ident
                    else:
                        state.setdefault(field, {})["task_id"] = ident
                    self.assert_stops_before_resume(state)
        for field in ("pending_task", "pending_acceptance"):
            with self.subTest(field=field, missing_id=True):
                state = copy.deepcopy(self.saved)
                state.setdefault(field, {}).pop("task_id", None)
                self.assert_stops_before_resume(state)

    def test_invalid_post_accept_task_cannot_complete_memory_or_events(self):
        self.backend = RecordingBackend()
        self.engine_before = self.engine(self.backend)
        with patch.object(self.engine_before.memory, "remember", side_effect=HarnessError("storage unavailable")):
            with self.assertRaisesRegex(HarnessError, "storage unavailable"):
                self.engine_before.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.saved = copy.deepcopy(self.engine_before.run)
        self.assertEqual(set(self.saved["accepted"]), {"greet"})
        self.assertEqual(self.engine_before.memory.export(), [])
        original_payload = self.saved["pending_post_accept"]["payload"]
        payloads = [None, [], "invalid", {}] + [
            {**original_payload, "task_id": ident} for ident in ("ghost", [], {}, None, 1, "")]
        for payload in payloads:
            with self.subTest(payload=payload):
                state = copy.deepcopy(self.saved)
                state["pending_post_accept"]["payload"] = payload
                self.assert_stops_before_resume(state)
        state = copy.deepcopy(self.saved)
        state["pending_post_accept"].pop("payload")
        self.assert_stops_before_resume(state)

    def test_invalid_task_catalog_cannot_break_reference_validation(self):
        self.blocked_review()
        for tasks in ([None], ["invalid"], [{}], [{"id": []}], [{"id": {}}],
                      [{"id": ""}], self.saved["tasks"] * 2):
            with self.subTest(tasks=tasks):
                state = copy.deepcopy(self.saved)
                state["tasks"] = tasks
                self.assert_stops_before_resume(state)

    def test_optional_state_types_stop_before_dispatch_or_ledger_write(self):
        self.blocked_review()
        for field in ("pending_task", "pending_acceptance", "pending_post_accept", "pending_replan",
                      "pending_directive", "worker_recovery", "replan_stopped"):
            for value in ([], None, "invalid"):
                with self.subTest(field=field, value=value):
                    state = copy.deepcopy(self.saved)
                    state[field] = value
                    self.assert_stops_before_resume(state)

    def test_conflicting_task_and_replan_preserve_candidate(self):
        self.blocked_review()
        state = copy.deepcopy(self.saved)
        state["pending_replan"] = {"stage": "pending", "trigger": {"task_id": "greet"}}
        candidate = (self.engine_before.worktree / "hello.py").read_bytes()
        self.assert_stops_before_resume(state)
        self.assertEqual((self.engine_before.worktree / "hello.py").read_bytes(), candidate)

    def test_receipts_require_existing_dispatch_with_matching_identity(self):
        self.blocked_review()
        worker = next(d for d in self.saved["dispatches"] if d["role"] == "worker")
        for ident in ("missing", worker["id"], [], None):
            with self.subTest(ident=ident):
                state = copy.deepcopy(self.saved)
                state["review_receipts"][self.key]["dispatch_id"] = ident
                self.assert_stops_before_resume(state)
        state = copy.deepcopy(self.saved)
        state["commander_receipts"]["commander:plan:planning"] = self.receipt
        self.assert_stops_before_resume(state)
        state = copy.deepcopy(self.saved)
        state["dispatches"].append(copy.deepcopy(state["dispatches"][-1]))
        self.assert_stops_before_resume(state)

    def test_correction_and_recovery_references_are_checked_at_load(self):
        self.blocked_review()
        cases = [("worker_recovery", "greet", self.receipt)]
        for field, key, value in cases:
            with self.subTest(field=field, value=value):
                state = copy.deepcopy(self.saved)
                state.setdefault(field, {})[key] = value
                self.assert_stops_before_resume(state)
        # Redo links: retry_of must name an invalid output of the same call that
        # was the previous model call; invalid_output must be that entry's own file.
        review = next(d for d in self.saved["dispatches"] if d["id"] == self.receipt["dispatch_id"])
        worker = next(d for d in self.saved["dispatches"] if d["role"] == "worker")
        foreign = {"artifact": str(Path(self.receipt["artifact"]).parents[1] / review["id"] / "returned-result.json"),
                   "artifact_digest": "0" * 64}
        own = {"artifact": str(Path(self.receipt["artifact"]).parents[1] / worker["id"] / "returned-result.json")}
        for name, field, value, target in (("retry_of missing", "retry_of", "missing", review),
                                           ("retry_of non-invalid", "retry_of", worker["id"], review),
                                           ("invalid_output of another entry", "invalid_output", foreign, worker),
                                           ("invalid_output without digest", "invalid_output", own, worker),
                                           ("invalid_output malformed", "invalid_output", [], worker)):
            with self.subTest(case=name):
                state = copy.deepcopy(self.saved)
                entry = next(d for d in state["dispatches"] if d["id"] == target["id"])
                entry[field] = value
                if field == "invalid_output":
                    entry["status"] = "invalid"
                self.assert_stops_before_resume(state)
        for field in ("review_receipts", "commander_receipts", "worker_recovery"):
            with self.subTest(field=field):
                state = copy.deepcopy(self.saved)
                state.setdefault(field, {})[self.key] = []
                self.assert_stops_before_resume(state)
