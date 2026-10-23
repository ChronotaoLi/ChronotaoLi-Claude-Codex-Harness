import copy
import hashlib
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json
from test_harness import RepoCase, ROOT, interrupt_started_agent
from test_runtime_changes import ScriptedBackend, finding


class ReviewCorrectionRecoveryTests(RepoCase):
    def backend_with_interrupted_final_correction(self, *, preserve_rejection):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] != "final_review":
                return
            number = sum(call["kind"] == "final_review" for call in backend.calls)
            if number == 1:
                result.update(verdict="request_changes", findings=[finding()], summary="x" * 4001)
            elif number == 2:
                interrupt_started_agent(kwargs)
            elif preserve_rejection:
                result.update(verdict="request_changes", findings=[finding()], summary="The recorded defect remains")
        return ScriptedBackend(change)

    def interrupted_run(self, backend):
        engine = self.engine(backend)
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(engine.run["status"], "interrupted")
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 2)
        return engine

    def test_resumed_final_correction_preserves_rejection_before_replanning(self):
        backend = self.backend_with_interrupted_final_correction(preserve_rejection=True)
        engine = self.interrupted_run(backend)
        head = engine.run["expected_head"]
        resumed = self.engine(backend)
        # Demo commander returns stop; the repaired rejection must reach it.
        with self.assertRaises(HarnessError):
            resumed.resume(engine.run["id"])
        self.assertEqual(resumed.run["expected_head"], head)
        self.assertEqual(resumed.run["final_history"][-1]["status"], "rejected")
        self.assertEqual(resumed.run["final_history"][-1]["review"]["findings"], [finding()])
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 3)
        self.assertEqual(sum(call["kind"] == "replan" for call in backend.calls), 1)
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)
        reviews = [entry["id"] for entry in resumed.run["dispatches"] if entry.get("kind") == "final_review"]
        self.assertEqual(len(reviews), 3)
        self.assertEqual(len(reviews), len(set(reviews)))

    def test_resumed_final_correction_cannot_reroll_or_retry_a_changed_verdict(self):
        backend = self.backend_with_interrupted_final_correction(preserve_rejection=False)
        engine = self.interrupted_run(backend)
        head = engine.run["expected_head"]
        resumed = self.engine(backend)
        with self.assertRaises(HarnessError):
            resumed.resume(engine.run["id"])
        self.assertEqual(resumed.run["status"], "blocked")
        self.assertEqual(resumed.run["expected_head"], head)
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 3)
        reviews = [entry for entry in resumed.run["dispatches"] if entry["role"] == "reviewer"]
        again = self.engine(backend)
        with self.assertRaises(HarnessError):
            again.resume(engine.run["id"])
        self.assertEqual(again.run["status"], "blocked")
        self.assertEqual([entry for entry in again.run["dispatches"] if entry["role"] == "reviewer"], reviews)
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 3)
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)

    def test_exhausted_format_repair_still_preserves_a_new_snapshot_rejection(self):
        self.config.data["runtime"]["max_replans"] = 1
        originals = []

        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                number = sum(call["kind"] == "final_review" for call in backend.calls)
                if number in (1, 3):
                    result.update(verdict="request_changes", findings=[finding()], summary="x" * 4001)
                elif number == 2:
                    result.update(verdict="request_changes", findings=[finding()], summary="The defect remains")
                originals.append(copy.deepcopy(result))
            elif kwargs["kind"] == "replan":
                corrective = copy.deepcopy(packet["original_tasks"][0])
                corrective.update(id="correct", title="Correct finding", description="Address the defect",
                                  depends_on=["greet"])
                result.update(action="revise", reason="Address the final finding", new_tasks=[corrective], updates=[])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# Independently reviewed correction\n")

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "重新规划次数"):
            engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(set(engine.run["accepted"]), {"greet", "correct"})
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 3)
        record = engine.run["final_history"][-1]
        self.assertEqual(record["status"], "invalid_rejected")
        self.assertEqual(record["phase"], "finished")
        self.assertEqual(engine.run["pending_replan"]["trigger"]["final_review"], record)
        self.assert_review_evidence(record, originals[-1])
        self.assertEqual(engine.run["format_repairs"], {"reviewer:final_review:final": 1})
        self.assertEqual(engine.run["review_corrections"], {})
        head = engine.run["expected_head"]
        calls = copy.deepcopy(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "重新规划次数"):
            resumed.resume(engine.run["id"])
        self.assertEqual(resumed.run["status"], "blocked")
        self.assertEqual(resumed.run["expected_head"], head)
        self.assertEqual(backend.calls, calls)
        self.assertEqual(resumed.run["final_attempts"], 2)
        self.assertEqual(resumed.run["final_history"][-1], record)

    def assert_review_evidence(self, record, original):
        artifact = Path(record["returned_artifact"])
        self.assertEqual(read_json(artifact), original)
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), record["artifact_digest"])
        self.assertEqual(record["dispatch_id"], original["dispatch_id"])
        self.assertEqual(record["review"]["findings"], original["findings"])

    def backend_with_new_task_rejection(self, *, invalid_id=False):
        originals = []

        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                number = sum(call["role"] == "worker" for call in backend.calls)
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write(f"# Candidate {number}\n")
            elif kwargs["kind"] == "review":
                number = sum(call["kind"] == "review" for call in backend.calls)
                if number in (1, 2, 3):
                    result.update(verdict="request_changes", findings=[finding()], summary="The defect remains")
                if number == 1 or number == 3 and not invalid_id:
                    result["summary"] = "x" * 4001
                if number == 3 and invalid_id:
                    result["findings"][0]["id"] = "invalid/id"
                originals.append(copy.deepcopy(result))

        return ScriptedBackend(change), originals

    def test_new_task_rejection_after_format_budget_reworks_without_another_correction(self):
        for invalid_id in (False, True):
            with self.subTest(invalid_id=invalid_id):
                backend, originals = self.backend_with_new_task_rejection(invalid_id=invalid_id)
                engine = self.engine(backend)
                result = engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["attempts"], {"greet": 3})
                self.assertEqual(result["format_repairs"], {"reviewer:review:greet": 1})
                self.assertEqual(result["review_corrections"], {})
                record = result["review_history"]["greet"][1]
                self.assertEqual(record["status"], "invalid_rejected")
                self.assert_review_evidence(record, originals[2])
                reviews = [call for call in backend.calls if call["kind"] == "review"]
                self.assertEqual(len(reviews), 4)
                self.assertEqual(sum("format_correction" in call["packet"] for call in reviews), 1)
                workers = [call for call in backend.calls if call["role"] == "worker"]
                prior = workers[-1]["packet"]["prior_reviews"][-1]
                self.assertEqual(prior["status"], "invalid_rejected")
                self.assertEqual(prior["artifact_digest"], record["artifact_digest"])

    def blocked_new_rejection_hook(self):
        backend, originals = self.backend_with_new_task_rejection()
        engine = self.engine(backend)
        attempts = []

        def hook(event, payload, logs):
            if event == "post_agent" and payload.get("status") == "invalid_rejected":
                attempts.append((copy.deepcopy(payload), logs))
                raise HarnessError("review policy unavailable")

        with patch.object(engine.hooks, "emit", side_effect=hook):
            with self.assertRaisesRegex(HarnessError, "review policy unavailable"):
                engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 3)
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(len(engine.run["review_history"]["greet"]), 1)
        return engine, backend, originals[-1], attempts

    def test_new_rejection_with_spent_budget_replays_failed_hook_before_rework(self):
        engine, backend, original, attempts = self.blocked_new_rejection_hook()
        key = "reviewer:review:greet"
        receipt = copy.deepcopy(engine.run["review_receipts"][key])
        artifact_bytes = Path(receipt["artifact"]).read_bytes()
        calls = copy.deepcopy(backend.calls)
        resumed = self.engine(backend)

        def hook(event, payload, logs):
            if event == "post_agent" and payload.get("status") == "invalid_rejected":
                self.assertEqual(backend.calls, calls)
                attempts.append((copy.deepcopy(payload), logs))

        with patch.object(resumed.hooks, "emit", side_effect=hook):
            result = resumed.resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 3})
        self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 4)
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0][1], attempts[1][1])
        self.assertTrue(all(payload["result"] == original for payload, _ in attempts))
        self.assertEqual(Path(receipt["artifact"]).read_bytes(), artifact_bytes)
        self.assert_review_evidence(result["review_history"]["greet"][1], original)

    def test_existing_exhausted_guard_for_original_rejection_is_not_converted(self):
        engine, backend, original, _ = self.blocked_new_rejection_hook()
        key = "reviewer:review:greet"
        receipt = engine.run["review_receipts"][key]
        # Model the persisted terminal guard written by the prior v8 runtime.
        guard = {field: receipt[field] for field in ("artifact", "artifact_digest", "snapshot_digest")}
        guard.update(error="response.summary: string length exceeds permitted bounds", exhausted=True)
        engine.run["review_corrections"][key] = guard
        engine.save()
        calls = copy.deepcopy(backend.calls)
        for options in ({}, {"repair_invalid": receipt["dispatch_id"]}):
            resumed = self.engine(backend)
            with self.assertRaises(HarnessError):
                resumed.resume(engine.run["id"], **options)
            self.assertEqual(backend.calls, calls)
            self.assertEqual(resumed.run["review_corrections"][key], guard)
            self.assertEqual(resumed.run["pending_task"]["stage"], "reviewing")
            self.assertEqual(resumed.run["accepted"], {})
            self.assertEqual(read_json(Path(guard["artifact"])), original)


if __name__ == "__main__":
    import unittest
    unittest.main()

