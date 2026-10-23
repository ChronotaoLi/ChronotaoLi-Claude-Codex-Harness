import copy
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.util import HarnessError, read_json, write_json
from test_engine_repairs import RecordingBackend
from test_harness import ROOT, RepoCase


class RecoveryStateValidationTests(RepoCase):
    def blocked_review(self):
        def invalid_approval(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                result["approved_memory_keys"] = ["unknown"]

        backend = RecordingBackend(invalid_approval)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
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
        invalid = {**self.receipt, "dispatch_id": "missing"}
        cases = [
            ("review_corrections", self.key, {**self.receipt,
                "artifact": str(Path(self.receipt["artifact"]).parents[1] / "missing" / "returned-result.json")}),
            ("pending_format_corrections", self.key, {"dispatch_id": "missing", "correction": {}}),
            ("pending_format_corrections", self.key, {"correction": {"previous_dispatch": "missing"}}),
            ("pending_format_corrections", self.key, {"correction": {"source": invalid}}),
            ("invalid_repairs", self.key, {"source": invalid}),
            ("invalid_repairs", self.key, {"source": self.receipt, "original": invalid}),
            ("invalid_repairs", self.key, {"source": self.receipt, "corrected_dispatch_id": "missing"}),
            ("worker_recovery", "greet", self.receipt),
        ]
        for field, key, value in cases:
            with self.subTest(field=field, value=value):
                state = copy.deepcopy(self.saved)
                state.setdefault(field, {})[key] = value
                self.assert_stops_before_resume(state)
        for field in ("review_receipts", "commander_receipts", "review_corrections",
                      "pending_format_corrections", "invalid_repairs", "worker_recovery"):
            with self.subTest(field=field):
                state = copy.deepcopy(self.saved)
                state.setdefault(field, {})[self.key] = []
                self.assert_stops_before_resume(state)

    def test_unstarted_correction_and_retained_repair_sources_remain_loadable(self):
        self.blocked_review()
        state = copy.deepcopy(self.saved)
        state["review_corrections"][self.key] = copy.deepcopy(self.receipt)
        state["pending_format_corrections"][self.key] = {
            "snapshot_digest": self.receipt["snapshot_digest"],
            "correction": {"artifact": self.receipt["artifact"]}}
        state["invalid_repairs"][self.key] = {
            "status": "invalidated", "source": self.receipt, "original": self.receipt,
            "corrected_dispatch_id": self.receipt["dispatch_id"]}
        write_json(self.engine_before.directory / "state.json", state)
        loaded = self.engine(self.backend)
        loaded.load_run(state["id"])
        self.assertEqual(loaded.run, state)
        self.assertEqual(len(self.backend.calls), 2)
