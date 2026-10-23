import copy
import hashlib
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT
from test_runtime_changes import finding


class ImmutableRejectionTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def assert_evidence(self, record, original):
        artifact = Path(record["returned_artifact"])
        raw = artifact.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), record["artifact_digest"])
        self.assertEqual(read_json(artifact), original)
        self.assertEqual(record["dispatch_id"], original["dispatch_id"])
        self.assertEqual(record["review"]["findings"], original["findings"])

    def assert_no_corrections(self, engine, backend):
        self.assertEqual(engine.run["format_repairs"], {})
        self.assertEqual(engine.run["review_corrections"], {})
        self.assertFalse(any("format_correction" in call["packet"] for call in backend.calls))

    def test_oversized_task_finding_reworks_and_preserves_original_even_with_bad_summary(self):
        originals = []

        def reject_once(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and not originals:
                result.update(verdict="request_changes", summary="s" * 4001,
                              findings=[{**finding(), "message": "m" * 4001}])
                originals.append(copy.deepcopy(result))

        backend = RecordingBackend(reject_once)
        engine = self.engine(backend)
        with patch.object(engine.hooks, "emit", wraps=engine.hooks.emit) as hooks:
            result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        record = result["review_history"]["greet"][0]
        self.assertEqual(record["status"], "invalid_rejected")
        self.assertIn("findings[0].message", record["validation_error"])
        self.assert_evidence(record, originals[0])
        workers = [call for call in backend.calls if call["role"] == "worker"]
        reviews = [call for call in backend.calls if call["kind"] == "review"]
        self.assertEqual(len(reviews), 2)
        review_ids = [request["dispatch_id"] for request in backend.requests if request["kind"] == "review"]
        self.assertEqual(len(set(review_ids)), 2)
        for call in (workers[1], reviews[1]):
            prior = call["packet"]["prior_reviews"][0]
            self.assertEqual(prior["status"], "invalid_rejected")
            self.assertEqual(prior["returned_artifact"], record["returned_artifact"])
            self.assertEqual(prior["artifact_digest"], record["artifact_digest"])
        rejected_hooks = [call.args[1] for call in hooks.call_args_list
                          if call.args[0] == "post_agent" and call.args[1].get("status") == "invalid_rejected"]
        self.assertEqual(len(rejected_hooks), 1)
        self.assertEqual(rejected_hooks[0]["result"], originals[0])
        self.assert_no_corrections(engine, backend)

    def test_overfull_final_findings_persist_replan_without_redrawing_same_snapshot(self):
        self.config.data["runtime"]["max_replans"] = 0
        originals = []

        def reject(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result.update(verdict="request_changes",
                              findings=[{**finding(), "id": f"defect-{number}"} for number in range(31)])
                originals.append(copy.deepcopy(result))

        backend = RecordingBackend(reject)
        engine = self.engine(backend)
        with patch.object(engine.hooks, "emit", wraps=engine.hooks.emit) as hooks:
            with self.assertRaisesRegex(HarnessError, "重新规划次数"):
                engine.start("Implement greet", self.plan())
        record = engine.run["final_history"][-1]
        self.assertEqual(record["status"], "invalid_rejected")
        self.assertEqual(record["phase"], "finished")
        self.assert_evidence(record, originals[0])
        self.assertEqual(engine.run["pending_replan"]["trigger"]["final_review"], record)
        rejected_hooks = [call.args[1] for call in hooks.call_args_list
                          if call.args[0] == "post_agent" and call.args[1].get("status") == "invalid_rejected"]
        self.assertEqual(len(rejected_hooks), 1)
        self.assertEqual(rejected_hooks[0]["result"], originals[0])
        calls = copy.deepcopy(backend.calls)
        for options in ({}, {"reverify": True}):
            resumed = self.engine(backend)
            with self.assertRaises(HarnessError):
                resumed.resume(engine.run["id"], **options)
            self.assertEqual(backend.calls, calls)
            self.assertEqual(resumed.run["final_attempts"], 1)
        self.assert_no_corrections(engine, backend)

    def test_approve_with_oversized_blocking_evidence_is_rejected(self):
        originals = []

        def reject_once(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and not originals:
                result.update(verdict="approve", findings=[{**finding(), "evidence": ["e" * 4001]}])
                originals.append(copy.deepcopy(result))

        backend = RecordingBackend(reject_once)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        record = result["review_history"]["greet"][0]
        self.assertEqual(record["status"], "invalid_rejected")
        self.assertEqual(record["review"]["verdict"], "approve")
        self.assert_evidence(record, originals[0])
        self.assert_no_corrections(engine, backend)

    def test_hook_failure_replays_same_overflow_rejection_before_rework(self):
        originals = []

        def reject_once(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and not originals:
                result.update(verdict="request_changes", findings=[{**finding(), "location": "l" * 4001}])
                originals.append(copy.deepcopy(result))

        backend = RecordingBackend(reject_once)
        engine = self.engine(backend)
        rejected_hooks = []

        def failed_hook(event, payload, logs):
            if event == "post_agent" and payload.get("status") == "invalid_rejected":
                rejected_hooks.append((copy.deepcopy(payload), logs))
                raise HarnessError("review policy unavailable")

        with patch.object(engine.hooks, "emit", side_effect=failed_hook):
            with self.assertRaisesRegex(HarnessError, "review policy unavailable"):
                engine.start("Implement greet", self.plan())
        receipt = copy.deepcopy(engine.run["review_receipts"]["reviewer:review:greet"])
        original_bytes = Path(receipt["artifact"]).read_bytes()
        self.assertFalse(receipt.get("hook_completed"))
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(engine.run["review_history"], {})
        self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 1)
        resumed = self.engine(backend)

        def restored_hook(event, payload, logs):
            if event == "post_agent" and payload.get("status") == "invalid_rejected":
                self.assertEqual(len(backend.calls), 2)  # Original worker and review only.
                rejected_hooks.append((copy.deepcopy(payload), logs))

        with patch.object(resumed.hooks, "emit", side_effect=restored_hook):
            result = resumed.resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(rejected_hooks), 2)
        self.assertNotEqual(rejected_hooks[0][1], rejected_hooks[1][1])
        self.assertTrue(all(payload["result"] == originals[0] for payload, _ in rejected_hooks))
        self.assertEqual(Path(receipt["artifact"]).read_bytes(), original_bytes)
        record = result["review_history"]["greet"][0]
        self.assert_evidence(record, originals[0])
        self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 2)
        self.assert_no_corrections(resumed, backend)
