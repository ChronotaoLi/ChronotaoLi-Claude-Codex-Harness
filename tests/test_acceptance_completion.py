import json
import sqlite3
from pathlib import Path

from harness import git
from harness.adapters import DemoBackend
from harness.util import HarnessError, dump, read_json
from test_harness import ROOT, RepoCase
from test_memory_pipeline import ProposedKnowledge


class AcceptanceCompletionTests(RepoCase):
    def events(self, engine, event):
        return [record for line in (engine.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if (record := json.loads(line))["event"] == event]

    def assert_completed_once(self, engine, original_commit, memory_count):
        result = engine.run
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["accepted"]["greet"]["commit"], original_commit)
        self.assertEqual(git.git(engine.worktree, "rev-list", "--count",
                                 result["base_commit"] + "..HEAD").strip(), "1")
        self.assertEqual(sum(item["role"] == "worker" for item in result["dispatches"]), 1)
        self.assertEqual(sum(item.get("kind") == "review" for item in result["dispatches"]), 1)
        self.assertEqual(len(engine.memory.export()), memory_count)
        self.assertEqual(len(self.events(engine, "task_accepted")), 1)
        self.assertNotIn("pending_post_accept", result)

    def test_approved_review_at_contract_maximum_still_records_task_memory(self):
        class MaximumReview(DemoBackend):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs["kind"] == "review":
                    result["summary"] = "s" * 4000
                    result["findings"] = [{
                        "id": str(index) + "f" * (96 - len(str(index))),
                        "severity": "advisory", "category": "evidence",
                        "location": "l" * 4000, "message": "m" * 4000,
                        "evidence": ["e" * 4000 for _ in range(20)],
                    } for index in range(30)]
                return result

        engine = self.engine(MaximumReview())
        result = engine.start("Implement greet")
        self.assertEqual(result["status"], "completed")
        accepted = result["accepted"]["greet"]
        self.assertGreater(len(dump(accepted)), 100000)
        record = engine.memory.export()[0]
        self.assertLess(len(record["detail"]), 100000)
        detail = json.loads(record["detail"])
        self.assertEqual(detail["review_artifact"], accepted["review_artifact"])
        self.assertEqual(detail["finding_count"], 30)
        self.assertEqual(read_json(Path(detail["review_artifact"]))["review"], accepted["review"])

    def test_memory_write_failure_after_commit_is_retried_before_hook_and_final_review(self):
        engine = self.engine(ProposedKnowledge())

        def unavailable(**kwargs):
            raise sqlite3.OperationalError("temporary database failure")

        engine.memory.remember = unavailable
        with self.assertRaises(sqlite3.OperationalError):
            engine.start("Implement greet")
        commit = engine.run["accepted"]["greet"]["commit"]
        self.assertEqual(engine.memory.export(), [])
        self.assertFalse(self.events(engine, "task_accepted"))
        self.assertFalse(any(item.get("kind") == "final_review" for item in engine.run["dispatches"]))
        persisted = read_json(engine.directory / "state.json")
        self.assertEqual(persisted["pending_post_accept"]["completion"]["approved_keys"], ["greeting"])
        resumed = self.engine(ProposedKnowledge())
        emit = resumed.hooks.emit
        hook_calls = []

        def check_memory(event, payload, logs):
            if event == "post_accept":
                hook_calls.append(event)
                self.assertEqual(len(resumed.memory.export()), 2)
            return emit(event, payload, logs)

        resumed.hooks.emit = check_memory
        resumed.resume(engine.run["id"])
        self.assertEqual(hook_calls, ["post_accept"])
        self.assert_completed_once(resumed, commit, 2)

    def test_interrupt_after_task_memory_transaction_does_not_create_another_version(self):
        engine = self.engine(ProposedKnowledge())
        remember = engine.memory.remember

        def interrupt_after_write(**kwargs):
            remember(**kwargs)
            raise KeyboardInterrupt()

        engine.memory.remember = interrupt_after_write
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet")
        commit = engine.run["accepted"]["greet"]["commit"]
        original = engine.memory.export()[0]
        self.assertNotIn("task_memory_id", engine.run["pending_post_accept"]["completion"])
        resumed = self.engine(ProposedKnowledge())
        resumed.resume(engine.run["id"])
        self.assertEqual(resumed.memory.read(original["id"])["created"], original["created"])
        self.assertEqual(resumed.memory.read(original["id"])["expires"], original["expires"])
        self.assertTrue(resumed.memory.read(original["id"])["active"])
        self.assert_completed_once(resumed, commit, 2)

    def test_partial_knowledge_transaction_rolls_back_and_is_retried_on_resume(self):
        class TwoProposals(ProposedKnowledge):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs["role"] == "worker":
                    result["proposed_memories"].append({**result["proposed_memories"][0], "key": "second"})
                if kwargs["kind"] == "review":
                    result["approved_memory_keys"].append("second")
                return result

        engine = self.engine(TwoProposals())
        insert = engine.memory._insert

        def fail_second(db, **kwargs):
            if kwargs["key"] == "knowledge:second":
                raise sqlite3.OperationalError("storage failed during second proposal")
            return insert(db, **kwargs)

        engine.memory._insert = fail_second
        with self.assertRaises(sqlite3.OperationalError):
            engine.start("Implement greet")
        commit = engine.run["accepted"]["greet"]["commit"]
        records = engine.memory.export()
        self.assertEqual([record["kind"] for record in records], ["accepted_task"])
        self.assertEqual(engine.run["pending_post_accept"]["completion"]["knowledge"]["status"], "pending")
        self.assertFalse(self.events(engine, "memory_proposals_discarded"))
        resumed = self.engine(TwoProposals())
        resumed.resume(engine.run["id"])
        self.assertTrue(resumed.memory.read(records[0]["id"])["active"])
        self.assert_completed_once(resumed, commit, 3)

    def test_interrupt_after_knowledge_transaction_does_not_repeat_promotion(self):
        engine = self.engine(ProposedKnowledge())
        promote = engine.memory.remember_proposals

        def interrupt_after_promotion(*args, **kwargs):
            promote(*args, **kwargs)
            raise KeyboardInterrupt()

        engine.memory.remember_proposals = interrupt_after_promotion
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet")
        commit = engine.run["accepted"]["greet"]["commit"]
        original = {record["id"]: record["expires"] for record in engine.memory.export()}
        self.assertEqual(len(original), 2)
        self.assertEqual(engine.run["pending_post_accept"]["completion"]["knowledge"]["status"], "pending")
        resumed = self.engine(ProposedKnowledge())
        resumed.resume(engine.run["id"])
        self.assertEqual({record["id"]: record["expires"] for record in resumed.memory.export()}, original)
        self.assert_completed_once(resumed, commit, 2)

    def test_replaying_completed_promotion_preserves_a_newer_knowledge_version(self):
        engine = self.engine(ProposedKnowledge())
        promote = engine.memory.remember_proposals

        def interrupt_after_promotion(*args, **kwargs):
            promote(*args, **kwargs)
            raise KeyboardInterrupt()

        engine.memory.remember_proposals = interrupt_after_promotion
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet")
        commit = engine.run["accepted"]["greet"]["commit"]
        completion = engine.run["pending_post_accept"]["completion"]
        newer = promote(completion["proposals"], completion["approved_keys"], commit,
                        "run_other", "newer_task")[0]
        resumed = self.engine(ProposedKnowledge())
        resumed.resume(engine.run["id"])
        self.assertTrue(resumed.memory.read(newer)["active"])
        self.assert_completed_once(resumed, commit, 3)

    def test_step_after_pending_memory_completion_pauses_before_next_worker(self):
        plan = read_json(ROOT / "examples" / "plan.json")
        plan["tasks"].append({**plan["tasks"][0], "id": "confirm", "depends_on": ["greet"]})
        engine = self.engine()

        def unavailable(**kwargs):
            raise sqlite3.OperationalError("temporary database failure")

        engine.memory.remember = unavailable
        with self.assertRaises(sqlite3.OperationalError):
            engine.start("Implement and confirm greet", plan)
        resumed = self.engine()
        paused = resumed.resume(engine.run["id"], step=True)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(set(paused["accepted"]), {"greet"})
        self.assertEqual(sum(item["role"] == "worker" for item in paused["dispatches"]), 1)
        self.assertNotIn("pending_post_accept", paused)
        self.assertEqual(len(resumed.memory.export()), 1)
        completed = self.engine().resume(engine.run["id"], step=True)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(set(completed["accepted"]), {"greet", "confirm"})

    def test_acceptance_event_is_not_repeated_after_append_interruption(self):
        engine = self.engine(ProposedKnowledge())
        event = engine.event

        def interrupt_after_event(name, **fields):
            event(name, **fields)
            if name == "task_accepted":
                raise KeyboardInterrupt()

        engine.event = interrupt_after_event
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet")
        commit = engine.run["accepted"]["greet"]["commit"]
        self.assertEqual(len(self.events(engine, "task_accepted")), 1)
        resumed = self.engine(ProposedKnowledge())
        resumed.resume(engine.run["id"])
        self.assert_completed_once(resumed, commit, 2)

    def test_optional_proposal_discard_survives_hook_failure_and_resume(self):
        engine = self.engine(ProposedKnowledge())
        remember = engine.memory.remember

        def operator_claims_key(**kwargs):
            ident = remember(**kwargs)
            remember(key="knowledge:greeting", abstract="Operator fact", overview="Operator-owned key",
                     detail="Operator-owned key", source="operator")
            return ident

        engine.memory.remember = operator_claims_key
        emit = engine.hooks.emit

        def fail_hook(event, payload, logs):
            if event == "post_accept":
                raise HarnessError("hook unavailable")
            return emit(event, payload, logs)

        engine.hooks.emit = fail_hook
        with self.assertRaisesRegex(HarnessError, "hook unavailable"):
            engine.start("Implement greet")
        commit = engine.run["accepted"]["greet"]["commit"]
        self.assertEqual(engine.run["pending_post_accept"]["completion"]["knowledge"]["status"], "discarded")
        resumed = self.engine(ProposedKnowledge())
        resumed.resume(engine.run["id"])
        self.assertEqual(len(self.events(resumed, "memory_proposals_discarded")), 1)
        self.assertEqual({item["kind"] for item in resumed.memory.export()}, {"accepted_task", "operator"})
        self.assert_completed_once(resumed, commit, 2)
