import copy
import json
from pathlib import Path
import unittest

from harness.context import compact_feedback, compact_reviews, review_digest
from harness.contracts import validate_review
from harness.util import HarnessError, dump, read_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend


def maximum_review():
    def detail(label):
        return (label + "\\\n" * 4000)[:3992] + "END-TEXT"

    findings = []
    for index in range(30):
        category = ("defect", "evidence", "scope", "preference")[index % 4]
        findings.append({
            "id": (f"finding-{index:02}-" + "x" * 96)[:96],
            "severity": "advisory" if category == "preference" else "blocking",
            "category": category, "location": detail(f"hello.py:{index + 1} "),
            "message": detail(f"Finding {index} "),
            "evidence": [detail(f"Evidence {index}/{item} ") for item in range(20)],
        })
    result = {"verdict": "request_changes", "summary": detail("Independent review "),
              "findings": findings, "approved_memory_keys": []}
    validate_review(result)
    return result


def identities(review):
    return [(item["id"], item["severity"], item["category"]) for item in review["findings"]]


class BoundedReviewLifecycleTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def assert_full_evidence(self, excerpt, original):
        self.assertTrue(excerpt["details_truncated"])
        self.assertEqual(identities(excerpt), identities(original))
        self.assertEqual(excerpt["verdict"], original["verdict"])
        self.assertTrue(excerpt["read_instruction"])
        path = Path(excerpt["full_review_artifact"])
        self.assertTrue(path.is_absolute())
        self.assertEqual(read_json(path)["review"]["findings"], original["findings"])
        self.assertEqual(read_json(path)["review"]["summary"], original["summary"])

    def test_maximum_task_rejection_reaches_rework_and_review_with_default_context(self):
        rejected = maximum_review()
        sizes = []

        def repair(backend, kwargs, packet, result):
            sizes.append(len(kwargs["prompt"]))
            count = sum(call["kind"] == "review" for call in backend.calls)
            if kwargs["kind"] == "review" and count == 1:
                result.update(copy.deepcopy(rejected))
            elif kwargs["kind"] == "dispatch":
                self.assert_full_evidence(packet["feedback"]["review"], rejected)
                self.assertEqual(engine.run["accepted"], {})
            elif kwargs["role"] == "worker" and engine.run["attempts"]["greet"] == 2:
                self.assert_full_evidence(packet["prior_reviews"][0]["review"], rejected)
                self.assertTrue(packet["feedback"]["review_in_prior_reviews"])
                with (kwargs["cwd"] / "hello.py").open("a") as file:
                    file.write("# correction reviewed in a fresh dispatch\n")
            elif kwargs["kind"] == "review" and count == 2:
                self.assert_full_evidence(packet["prior_reviews"][0]["review"], rejected)

        self.assertEqual(self.config.data["runtime"]["context_chars"], 32000)
        backend = ScriptedBackend(repair)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(result["review_history"]["greet"][0]["review"]["findings"], rejected["findings"])
        self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 2)
        self.assertLessEqual(max(sizes), 32000)
        dispatches = [item["id"] for item in result["dispatches"] if item["role"] in ("worker", "reviewer")]
        self.assertEqual(len(dispatches), len(set(dispatches)))

    def test_maximum_final_rejection_reaches_replan_and_new_final_review(self):
        rejected = maximum_review()
        sizes = []

        def repair(backend, kwargs, packet, result):
            sizes.append(len(kwargs["prompt"]))
            final_count = sum(call["kind"] == "final_review" for call in backend.calls)
            if kwargs["kind"] == "final_review" and final_count == 1:
                result.update(copy.deepcopy(rejected))
            elif kwargs["kind"] == "replan":
                self.assert_full_evidence(packet["trigger"]["final_review"]["review"], rejected)
                self.assertEqual(engine.run["status"], "running")
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id="correct", description="Resolve every final finding", depends_on=["greet"])
                result.update(action="revise", reason="Resolve the saved final findings", new_tasks=[task], updates=[])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                with (kwargs["cwd"] / "hello.py").open("a") as file:
                    file.write("# whole-run correction independently reviewed\n")
            elif kwargs["kind"] == "final_review" and final_count == 2:
                self.assert_full_evidence(packet["prior_reviews"][0]["review"], rejected)

        self.assertEqual(self.config.data["runtime"]["context_chars"], 32000)
        backend = ScriptedBackend(repair)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["replan_attempts"], 1)
        self.assertEqual(result["final_attempts"], 2)
        self.assertEqual(set(result["accepted"]), {"greet", "correct"})
        first, second = result["final_history"]
        self.assertEqual(first["review"]["findings"], rejected["findings"])
        self.assertEqual((first["status"], second["status"]), ("rejected", "approved"))
        self.assertNotEqual(first["snapshot_digest"], second["snapshot_digest"])
        self.assertLessEqual(max(sizes), 32000)

    def test_large_review_with_approve_and_blocking_findings_still_cannot_accept(self):
        rejected = maximum_review()
        rejected["verdict"] = "approve"
        self.config.data["runtime"]["max_attempts"] = 1

        def reject(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                result.update(copy.deepcopy(rejected))

        engine = self.engine(ScriptedBackend(reject))
        with self.assertRaisesRegex(HarnessError, "尝试次数"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(engine.run["review_history"]["greet"][0]["review"]["findings"], rejected["findings"])

    def test_maximum_findings_with_bad_summary_repair_then_rework_with_full_evidence(self):
        rejected = maximum_review()
        sizes = []
        returned_artifacts = []

        def repair(backend, kwargs, packet, result):
            sizes.append(len(kwargs["prompt"]))
            count = sum(call["kind"] == "review" for call in backend.calls)
            if kwargs["kind"] == "review" and count <= 2:
                result.update(copy.deepcopy(rejected))
                if count == 1:
                    result["summary"] = "x" * 4001
                else:
                    correction = packet["format_correction"]
                    excerpt = correction["preserve_review"]
                    self.assertEqual(identities(excerpt), identities(rejected))
                    self.assertTrue(excerpt["details_truncated"])
                    artifact = Path(excerpt["full_review_artifact"])
                    original = read_json(artifact)
                    returned_artifacts.append(artifact)
                    self.assertEqual(original["findings"], rejected["findings"])
                    self.assertEqual(len(original["summary"]), 4001)
                    self.assertNotEqual(original["dispatch_id"], result["dispatch_id"])
                    self.assertEqual(excerpt["review_content_sha256"], review_digest(original))
                    self.assertEqual(engine.run["accepted"], {})
            elif kwargs["role"] == "worker" and engine.run["attempts"]["greet"] == 2:
                self.assert_full_evidence(packet["prior_reviews"][0]["review"], rejected)
                with (kwargs["cwd"] / "hello.py").open("a") as file:
                    file.write("# repair after preserving the original rejection\n")

        backend = ScriptedBackend(repair)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 3)
        self.assertEqual(len(returned_artifacts), 1)
        self.assertEqual(read_json(returned_artifacts[0])["findings"], rejected["findings"])
        self.assertEqual(result["review_history"]["greet"][0]["review"]["findings"], rejected["findings"])
        self.assertEqual(result["format_repairs"]["reviewer:review:greet"], 1)
        self.assertLessEqual(max(sizes), 32000)

    def test_large_review_correction_cannot_change_evidence_beyond_excerpt(self):
        rejected = maximum_review()
        sizes = []

        def tamper(backend, kwargs, packet, result):
            sizes.append(len(kwargs["prompt"]))
            if kwargs["kind"] != "review":
                return
            result.update(copy.deepcopy(rejected))
            if sum(call["kind"] == "review" for call in backend.calls) == 1:
                result["summary"] = "x" * 4001
            else:
                self.assertTrue(packet["format_correction"]["preserve_review"]["details_truncated"])
                evidence = result["findings"][-1]["evidence"][-1]
                result["findings"][-1]["evidence"][-1] = evidence[:-1] + "Z"

        backend = ScriptedBackend(tamper)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "不得改变 finding"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(engine.run["attempts"], {"greet": 1})
        self.assertTrue(engine.run["review_corrections"]["reviewer:review:greet"]["failed"])
        self.assertLessEqual(max(sizes), 32000)


class BoundedReviewEvidenceTests(unittest.TestCase):
    def test_changed_truncated_tail_is_not_deduplicated_or_hidden_by_later_approval(self):
        first = maximum_review()
        second = copy.deepcopy(first)
        second["findings"][0]["evidence"][-1] = second["findings"][0]["evidence"][-1][:-1] + "Z"
        history = [{"review": first, "artifact": "/run/first-review.json"},
                   {"review": second, "artifact": "/run/second-review.json"},
                   {"review": {"verdict": "approve", "summary": "Reviewed later candidate",
                               "findings": [], "approved_memory_keys": []},
                    "artifact": "/run/third-review.json"}]
        before = copy.deepcopy(history)
        compacted = compact_reviews(history)
        self.assertEqual(history, before)
        self.assertEqual([item["artifact"] for item in compacted], [item["artifact"] for item in history])
        findings = [item for record in compacted for item in record["review"]["findings"]]
        self.assertEqual(len(findings), 31)
        self.assertEqual(sum(item["id"] == first["findings"][0]["id"] for item in findings), 2)
        self.assertEqual(set(identities({"findings": findings})), set(identities(first)))
        self.assertTrue(compacted[0]["review"]["details_truncated"])
        self.assertTrue(compacted[1]["review"]["details_truncated"])

    def test_feedback_digest_deduplicates_exact_review_but_not_changed_evidence(self):
        review = maximum_review()
        history = compact_reviews([{"review": review, "artifact": "/run/review.json"}])
        feedback = {"final_review": {"review": review, "artifact": "/run/review.json"}}
        compacted = compact_feedback(feedback, history)
        self.assertTrue(compacted["final_review"]["review_in_prior_reviews"])
        changed = copy.deepcopy(feedback)
        changed["final_review"]["review"]["findings"][-1]["message"] += "different"
        compacted = compact_feedback(changed, history)
        self.assertIn("review", compacted["final_review"])
        self.assertNotIn("review_in_prior_reviews", compacted["final_review"])
        self.assertEqual(identities(compacted["final_review"]["review"]), identities(review))
        self.assertLess(len(dump(compacted)), 12000)

    def test_review_without_full_artifact_is_never_silently_truncated(self):
        review = maximum_review()
        compacted = compact_feedback({"review": review})
        self.assertEqual(compacted["review"], review)
        self.assertGreater(len(json.dumps(compacted)), 32000)


if __name__ == "__main__":
    unittest.main()
