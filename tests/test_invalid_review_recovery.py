import copy
import hashlib
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.util import HarnessError, read_json, write_json
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT
from test_runtime_changes import finding


def duplicate_rejection(result):
    first, second = finding(), finding()
    second["message"] = "A separate defect sharing the same invalid ID"
    result.update(verdict="request_changes", findings=[first, second])


class InvalidReviewRecoveryTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def assert_raw_evidence(self, record):
        raw = Path(record["returned_artifact"]).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), record["artifact_digest"])
        result = read_json(Path(record["returned_artifact"]))
        self.assertEqual(result["dispatch_id"], record["dispatch_id"])
        self.assertEqual(result["findings"], record["review"]["findings"])

    def test_invalid_final_rejection_persists_without_same_snapshot_reroll(self):
        self.config.data["runtime"]["max_replans"] = 0

        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                duplicate_rejection(result)

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "重新规划次数"):
            engine.start("Implement greet", self.plan())
        record = engine.run["final_history"][-1]
        self.assertEqual(record["status"], "invalid_rejected")
        self.assertEqual(record["phase"], "finished")
        self.assertIn("唯一", record["validation_error"])
        self.assert_raw_evidence(record)
        self.assertEqual(engine.run["pending_replan"]["trigger"]["final_review"], record)
        calls = copy.deepcopy(backend.calls)
        for options in ({}, {"reverify": True}):
            resumed = self.engine(backend)
            with self.assertRaises(HarnessError):
                resumed.resume(engine.run["id"], **options)
            self.assertEqual(resumed.run["final_attempts"], 1)
            self.assertEqual(backend.calls, calls)

    def test_approve_with_invalid_blocking_finding_is_still_rejection(self):
        self.config.data["runtime"]["max_replans"] = 0

        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result.update(verdict="approve", findings=[{**finding(), "evidence": []}],
                              approved_memory_keys=["nonexistent"])

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        record = engine.run["final_history"][-1]
        self.assertEqual(record["status"], "invalid_rejected")
        self.assertEqual(record["review"]["verdict"], "approve")
        self.assert_raw_evidence(record)
        self.assertNotEqual(engine.run["status"], "completed")

    def test_invalid_task_rejection_reaches_worker_and_independent_review(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and sum(c["kind"] == "review" for c in backend.calls) == 1:
                duplicate_rejection(result)
                result["approved_memory_keys"] = ["unapproved"]

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        record = result["review_history"]["greet"][0]
        self.assertEqual(record["status"], "invalid_rejected")
        self.assert_raw_evidence(record)
        workers = [call for call in backend.calls if call["role"] == "worker"]
        reviews = [call for call in backend.calls if call["kind"] == "review"]
        for call in (workers[1], reviews[1]):
            prior = call["packet"]["prior_reviews"][0]
            self.assertEqual(prior["review"]["findings"], record["review"]["findings"])
            self.assertEqual(prior["returned_artifact"], record["returned_artifact"])
            self.assertEqual(prior["status"], "invalid_rejected")
        self.assertEqual(len(reviews), 2)
        self.assertEqual(result["agent_dispatches"], len(backend.requests))
        self.assertEqual(engine.memory.search("unapproved"), [])

    def test_noop_final_correction_keeps_invalid_rejection_until_changed_fix(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and sum(c["kind"] == "final_review" for c in backend.calls) == 1:
                duplicate_rejection(result)
            elif kwargs["kind"] == "replan":
                number = sum(c["kind"] == "replan" for c in backend.calls)
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id=f"correct{number}", depends_on=["greet"])
                result.update(action="revise", reason="Resolve recorded findings", new_tasks=[task], updates=[])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct2":
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# Independently reviewed correction\n")

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual([r["status"] for r in result["final_history"]], ["invalid_rejected", "approved"])
        self.assertEqual(sum(c["kind"] == "final_review" for c in backend.calls), 2)
        self.assertEqual(sum(c["kind"] == "replan" for c in backend.calls), 2)
        accepted = result["accepted"]
        self.assertEqual(accepted["greet"]["commit"], accepted["correct1"]["commit"])
        self.assertNotEqual(accepted["greet"]["commit"], accepted["correct2"]["commit"])
        self.assert_raw_evidence(result["final_history"][0])

    def test_invalid_nonrejecting_approval_remains_blocked_and_cannot_be_redrawn(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result["approved_memory_keys"] = ["nonexistent"]

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "不存在"):
            engine.start("Implement greet", self.plan())
        calls = copy.deepcopy(backend.calls)
        for _ in range(2):
            with self.assertRaisesRegex(HarnessError, "不存在"):
                self.engine(backend).resume(engine.run["id"])
            self.assertEqual(backend.calls, calls)

    def test_returned_invalid_rejection_survives_receipt_write_interruption(self):
        self.config.data["runtime"]["max_replans"] = 0

        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                duplicate_rejection(result)

        backend = RecordingBackend(change)
        engine = self.engine(backend)

        def interrupt(path, value):
            write_json(path, value)
            if Path(path).name == "returned-result.json" and value.get("task_id") == "final":
                raise KeyboardInterrupt()

        with patch("harness.engine.write_json", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        calls = copy.deepcopy(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "重新规划次数"):
            resumed.resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)
        self.assertEqual(resumed.run["final_history"][-1]["status"], "invalid_rejected")
        self.assert_raw_evidence(resumed.run["final_history"][-1])

    def pending_invalid_task_review(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and sum(c["kind"] == "review" for c in backend.calls) == 1:
                duplicate_rejection(result)

        backend = RecordingBackend(change)
        engine = self.engine(backend)

        def fail_hook(event, payload, logs):
            if event == "post_agent" and payload.get("status") == "invalid_rejected":
                raise HarnessError("invalid-review policy unavailable")

        with patch.object(engine.hooks, "emit", side_effect=fail_hook), self.assertRaisesRegex(HarnessError, "policy unavailable"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["accepted"], {})
        return engine, backend

    def test_invalid_rejection_hook_recovery_reuses_result_before_rework(self):
        engine, backend = self.pending_invalid_task_review()
        self.config.data["runtime"]["max_attempts"] = 3
        resumed = self.engine(backend)
        result = resumed.resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(c["kind"] == "review" for c in backend.calls), 2)
        self.assertEqual(result["review_history"]["greet"][0]["status"], "invalid_rejected")

    def test_changed_candidate_preserves_unconsumed_invalid_review_evidence(self):
        engine, backend = self.pending_invalid_task_review()
        receipt = copy.deepcopy(engine.run["review_receipts"]["reviewer:review:greet"])
        with (engine.worktree / "hello.py").open("a") as output:
            output.write("# Candidate changed while policy service was unavailable\n")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        record = result["review_history"]["greet"][0]
        self.assertEqual(record["status"], "invalidated")
        self.assertEqual(record["artifact"], receipt["artifact"])
        self.assertEqual(record["artifact_digest"], receipt["artifact_digest"])
        self.assertEqual(len(record["review"]["findings"]), 2)
        worker = [c for c in backend.calls if c["role"] == "worker"][1]
        self.assertEqual(worker["packet"]["prior_reviews"][0]["review"]["findings"], record["review"]["findings"])

    def test_changed_candidate_cannot_hide_tampered_review_evidence(self):
        engine, backend = self.pending_invalid_task_review()
        receipt = engine.run["review_receipts"]["reviewer:review:greet"]
        Path(receipt["artifact"]).write_text('{}\n')
        with (engine.worktree / "hello.py").open("a") as output:
            output.write("# Candidate changed\n")
        calls = copy.deepcopy(backend.calls)
        with self.assertRaisesRegex(HarnessError, "审阅原始结果已改变"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)
        self.assertEqual(git.head(engine.worktree), engine.run["expected_head"])

    def test_version_five_ledger_requires_original_runtime(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        engine.start("Implement greet", self.plan(), pause_after_plan=True)
        engine.run["schema_version"] = 5
        engine.save()
        with self.assertRaisesRegex(HarnessError, "旧 run"):
            self.engine(backend).resume(engine.run["id"], approve_plan=True)
        self.assertEqual(backend.calls, [])
