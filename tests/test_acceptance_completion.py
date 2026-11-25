import errno
import json
import os
import sqlite3
from pathlib import Path

from harness import git
from harness.adapters import DemoBackend
from harness.state_files import StateReadError
from harness.util import HarnessError, dump, read_json
from unittest.mock import patch

from test_harness import ROOT, RepoCase, after_save, crash_point
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

    def test_acceptance_tree_stays_out_of_saved_post_accept_payload(self):
        engine = self.engine(DemoBackend())
        published = []

        def capture_publication():
            published.append((
                read_json(engine.directory / "state.json"),
                git.git(engine.worktree, "rev-parse", "HEAD").strip(),
                git.git(engine.worktree, "rev-parse", "HEAD^{tree}").strip(),
            ))

        observer, _ = after_save(engine, lambda run: "pending_post_accept" in run, capture_publication)
        with observer:
            engine.start("Implement greet", step=True)

        (saved, commit, tree), = published
        self.assert_completed_once(engine, commit, 1)
        self.assertEqual(saved["expected_head"], commit)
        self.assertNotIn("pending_acceptance", saved)
        accepted = saved["accepted"]["greet"]
        self.assertEqual(accepted["commit"], commit)
        self.assertEqual(accepted["tree"], tree)
        self.assertEqual(engine.run["accepted"]["greet"], accepted)

        # Signed A1 publishes these acceptance facts in both places. Ordinary
        # acceptance binds the worker source and retains the empty request list.
        old_keys = {"commit", "summary", "summary_source", "files", "verification",
                    "review", "review_artifact", "review_source", "worker_source", "decision_requests"}
        self.assertEqual(set(accepted), old_keys | {"tree"})
        self.assertEqual(accepted["worker_source"], saved["review_history"]["greet"][-1]["worker_source"])
        self.assertEqual(accepted["decision_requests"], [])
        post_accept = saved["pending_post_accept"]
        self.assertEqual(set(post_accept), {"payload", "completion"})

        record, = engine.memory.export()
        self.assertEqual(record["kind"], "accepted_task")
        self.assertEqual(json.loads(record["detail"])["commit"], commit)
        self.assertEqual(record["source"], f"git:{commit};run:{saved['id']}")
        self.assertEqual(record["provenance"], {
            "commit": commit, "run_id": saved["id"], "task_id": "greet",
            "scope": "run", "reference_only": True,
        })
        event, = self.events(engine, "task_accepted")
        self.assertEqual(event["task_id"], "greet")
        self.assertEqual(event["commit"], commit)
        self.assertEqual(event["event_id"], post_accept["completion"]["id"])

        self.assertEqual(set(post_accept["payload"]), old_keys | {"task_id"})
        self.assertEqual(post_accept["payload"], {
            "task_id": "greet", **{key: accepted[key] for key in old_keys},
        })

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

    def test_memory_write_failure_after_commit_is_retried_before_final_review(self):
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
        resumed.resume(engine.run["id"])
        self.assert_completed_once(resumed, commit, 2)

    def test_interrupt_after_task_memory_transaction_does_not_create_another_version(self):
        engine = self.engine(ProposedKnowledge())
        remember = engine.memory.remember
        crash, die = crash_point(engine)

        def interrupt_after_write(**kwargs):
            remember(**kwargs)
            die()

        engine.memory.remember = interrupt_after_write
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet")
        saved = read_json(engine.directory / "state.json")
        commit = saved["accepted"]["greet"]["commit"]
        original = engine.memory.export()[0]
        self.assertNotIn("task_memory_id", saved["pending_post_accept"]["completion"])
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
        crash, die = crash_point(engine)

        def interrupt_after_promotion(*args, **kwargs):
            promote(*args, **kwargs)
            die()

        engine.memory.remember_proposals = interrupt_after_promotion
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet")
        saved = read_json(engine.directory / "state.json")
        commit = saved["accepted"]["greet"]["commit"]
        original = {record["id"]: record["expires"] for record in engine.memory.export()}
        self.assertEqual(len(original), 2)
        self.assertEqual(saved["pending_post_accept"]["completion"]["knowledge"]["status"], "pending")
        resumed = self.engine(ProposedKnowledge())
        resumed.resume(engine.run["id"])
        self.assertEqual({record["id"]: record["expires"] for record in resumed.memory.export()}, original)
        self.assert_completed_once(resumed, commit, 2)

    def test_replaying_completed_promotion_preserves_a_newer_knowledge_version(self):
        engine = self.engine(ProposedKnowledge())
        promote = engine.memory.remember_proposals
        crash, die = crash_point(engine)

        def interrupt_after_promotion(*args, **kwargs):
            promote(*args, **kwargs)
            die()

        engine.memory.remember_proposals = interrupt_after_promotion
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet")
        saved = read_json(engine.directory / "state.json")
        commit = saved["accepted"]["greet"]["commit"]
        completion = saved["pending_post_accept"]["completion"]
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
        event = engine.context.event
        crash, die = crash_point(engine)

        def interrupt_after_event(name, **fields):
            event(name, **fields)
            if name == "task_accepted":
                die()

        engine.context.event = interrupt_after_event
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet")
        saved = read_json(engine.directory / "state.json")
        self.assertIn("pending_post_accept", saved)
        commit = saved["accepted"]["greet"]["commit"]
        self.assertEqual(len(self.events(engine, "task_accepted")), 1)
        resumed = self.engine(ProposedKnowledge())
        resumed.resume(engine.run["id"])
        self.assert_completed_once(resumed, commit, 2)

    def test_acceptance_event_read_error_leaves_pending_post_accept_for_replay(self):
        engine = self.engine(ProposedKnowledge())
        service = engine.services.tasks
        once, open_file = service.acceptance_event_once, os.open
        issued = []

        def fail_read(name, flags, *args, **kwargs):
            if name == "events.jsonl" and kwargs.get("dir_fd") is not None:
                issued.append(name)
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            return open_file(name, flags, *args, **kwargs)

        def read_during_acceptance(*args, **kwargs):
            with patch.object(os, "open", fail_read):
                return once(*args, **kwargs)

        with patch.object(service, "acceptance_event_once", side_effect=read_during_acceptance), \
                self.assertRaises(StateReadError) as caught:
            engine.start("Implement greet")
        self.assertEqual(caught.exception.stop["reason"], "io")
        self.assertEqual(issued, ["events.jsonl"])
        saved = read_json(engine.directory / "state.json")
        self.assertIn("pending_post_accept", saved)
        self.assertEqual(self.events(engine, "task_accepted"), [])
        recovered = self.engine(ProposedKnowledge())
        recovered.resume(engine.run["id"])
        self.assert_completed_once(recovered, saved["accepted"]["greet"]["commit"], 2)

    def test_discarded_knowledge_stays_discarded_after_crash(self):
        engine = self.engine(ProposedKnowledge())
        crash, die = crash_point(engine)
        event = engine.context.event

        def conflict(*args, **kwargs):
            raise HarnessError("knowledge key temporarily owned elsewhere")

        def die_after_discard(name, **fields):
            event(name, **fields)
            if name == "memory_proposals_discarded" and "event_id" in fields:
                die()
        engine.memory.remember_proposals = conflict
        engine.context.event = die_after_discard
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet")
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["pending_post_accept"]["completion"]["knowledge"]["status"], "discarded")
        self.assertFalse(self.events(engine, "task_accepted"))
        # The promotion would succeed now; the saved verdict is not redrawn.
        resumed = self.engine(ProposedKnowledge())
        resumed.resume(saved["id"])
        self.assertEqual(len(self.events(resumed, "memory_proposals_discarded")), 1)
        self.assertEqual({item["kind"] for item in resumed.memory.export()}, {"accepted_task"})
        self.assert_completed_once(resumed, saved["accepted"]["greet"]["commit"], 1)

    def test_completed_task_memory_step_is_not_repeated(self):
        engine = self.engine(ProposedKnowledge())
        crash, die = crash_point(engine)

        def die_before_knowledge(*args, **kwargs):
            die()
        engine.memory.remember_proposals = die_before_knowledge
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet")
        saved = read_json(engine.directory / "state.json")
        self.assertIn("task_memory_id", saved["pending_post_accept"]["completion"])
        resumed = self.engine(ProposedKnowledge())
        # A completed step never touches the memory store again, even when it is unavailable.
        with patch.object(resumed.memory, "remember",
                          side_effect=sqlite3.OperationalError("store unavailable")) as remember:
            resumed.resume(saved["id"])
        remember.assert_not_called()
        self.assert_completed_once(resumed, saved["accepted"]["greet"]["commit"], 2)

    def test_promoted_knowledge_is_not_promoted_again_after_crash(self):
        engine = self.engine(ProposedKnowledge())
        crash, die = crash_point(engine)
        event = engine.context.event

        def die_before_acceptance_event(name, **fields):
            if name == "task_accepted":
                die()
            event(name, **fields)
        engine.context.event = die_before_acceptance_event
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet")
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["pending_post_accept"]["completion"]["knowledge"]["status"], "promoted")
        self.assertFalse(self.events(engine, "task_accepted"))
        resumed = self.engine(ProposedKnowledge())
        unavailable = sqlite3.OperationalError("store unavailable")
        with patch.object(resumed.memory, "remember_proposals", side_effect=unavailable) as promote, \
                patch.object(resumed.memory, "remember", side_effect=unavailable):
            resumed.resume(saved["id"])
        promote.assert_not_called()
        self.assert_completed_once(resumed, saved["accepted"]["greet"]["commit"], 2)
