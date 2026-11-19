import copy
import json
from pathlib import Path
import unittest

from harness.context import (REVIEW_CONTEXT_CHARS, compact_feedback, compact_review, compact_reviews, review_digest,
                             review_view)
from harness.contracts import validate_review
from harness.util import HarnessError, dump, read_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend, finding, revised, trigger_kind


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


# One constrained budget for BoundedReviewLifecycleTests; longer shared role text (RULES, skills) raises its floor.
CONSTRAINED_CONTEXT_CHARS = 38000


class BoundedReviewLifecycleTests(RepoCase):
    def setUp(self):
        super().setUp()
        # Exercise bounded retrieval under an explicitly constrained budget. A review
        # excerpt fills its fixed share, and shared RULES text sets the rest of the floor.
        self.config.data["runtime"]["context_chars"] = CONSTRAINED_CONTEXT_CHARS

    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def assert_full_evidence(self, record, original):
        excerpt = record["review"]
        self.assertTrue(excerpt["details_truncated"])
        self.assertEqual(identities(excerpt), identities(original))
        self.assertEqual(excerpt["verdict"], original["verdict"])
        # Retrieval is by reference and digest; how to read it lives in shared RULES.
        self.assertNotIn("read_instruction", excerpt)
        # The complete review is in the artifact of the record holding the excerpt, cited once.
        self.assertNotIn("full_review_artifact", excerpt)
        path = Path(record["artifact"])
        self.assertTrue(path.is_absolute())
        self.assertEqual(read_json(path)["review"]["findings"], original["findings"])
        self.assertEqual(read_json(path)["review"]["summary"], original["summary"])
        self.assertEqual(excerpt["review_content_sha256"], review_digest(read_json(path)["review"]))

    def test_maximum_task_rejection_reaches_rework_and_review_with_constrained_context(self):
        rejected = maximum_review()
        sizes = []
        inspected = []

        def repair(backend, kwargs, packet, result):
            sizes.append(len(kwargs["prompt"]))
            count = sum(call["kind"] == "review" for call in backend.calls)
            if kwargs["kind"] == "review" and count == 1:
                result.update(copy.deepcopy(rejected))
            elif kwargs["kind"] == "decide":
                inspected.append("decide")
                self.assert_full_evidence(packet["trigger"]["feedback"], rejected)
                self.assertEqual(engine.run["accepted"], {})
            elif kwargs["role"] == "worker" and engine.run["attempts"]["greet"] == 2:
                self.assert_full_evidence(packet["prior_reviews"][0], rejected)
                self.assertTrue(packet["feedback"]["review_in_prior_reviews"])
                with (kwargs["cwd"] / "hello.py").open("a") as file:
                    file.write("# correction reviewed in a fresh dispatch\n")
            elif kwargs["kind"] == "review" and count == 2:
                self.assert_full_evidence(packet["prior_reviews"][0], rejected)

        self.assertEqual(self.config.data["runtime"]["context_chars"], CONSTRAINED_CONTEXT_CHARS)
        backend = ScriptedBackend(repair)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(result["review_history"]["greet"][0]["review"]["findings"], rejected["findings"])
        self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 2)
        self.assertEqual(inspected, ["decide"])
        self.assertLessEqual(max(sizes), CONSTRAINED_CONTEXT_CHARS)
        dispatches = [item["id"] for item in result["dispatches"] if item["role"] in ("worker", "reviewer")]
        self.assertEqual(len(dispatches), len(set(dispatches)))

    def test_maximum_final_rejection_reaches_decide_and_new_final_review(self):
        rejected = maximum_review()
        sizes = []

        def repair(backend, kwargs, packet, result):
            sizes.append(len(kwargs["prompt"]))
            final_count = sum(call["kind"] == "final_review" for call in backend.calls)
            if kwargs["kind"] == "final_review" and final_count == 1:
                result.update(copy.deepcopy(rejected))
            elif trigger_kind(kwargs, packet) == "final":
                self.assert_full_evidence(packet["trigger"]["final_review"], rejected)
                self.assertEqual(engine.run["status"], "running")
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id="correct", description="Resolve every final finding", depends_on=["greet"])
                revised(result, packet, "Resolve the saved final findings", new_tasks=[task])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                with (kwargs["cwd"] / "hello.py").open("a") as file:
                    file.write("# whole-run correction independently reviewed\n")
            elif kwargs["kind"] == "final_review" and final_count == 2:
                self.assert_full_evidence(packet["prior_reviews"][0], rejected)

        self.assertEqual(self.config.data["runtime"]["context_chars"], CONSTRAINED_CONTEXT_CHARS)
        backend = ScriptedBackend(repair)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["plan_history"]), 2)
        self.assertEqual(len(result["final_history"]), 2)
        self.assertEqual(set(result["accepted"]), {"greet", "correct"})
        first, second = result["final_history"]
        self.assertEqual(first["review"]["findings"], rejected["findings"])
        self.assertEqual((first["status"], second["status"]), ("rejected", "approved"))
        self.assertNotEqual(first["snapshot_digest"], second["snapshot_digest"])
        self.assertLessEqual(max(sizes), CONSTRAINED_CONTEXT_CHARS)

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

    def test_maximum_findings_with_bad_summary_rework_with_full_evidence(self):
        rejected = maximum_review()
        sizes = []

        def rework(backend, kwargs, packet, result):
            sizes.append(len(kwargs["prompt"]))
            count = sum(call["kind"] == "review" for call in backend.calls)
            if kwargs["kind"] == "review" and count == 1:
                # A binding rejection with an over-long summary: invalid, but still a veto.
                result.update(copy.deepcopy(rejected))
                result["summary"] = "x" * 4001
            elif kwargs["role"] == "worker" and engine.run["attempts"]["greet"] == 2:
                self.assert_full_evidence(packet["prior_reviews"][0], {**rejected, "summary": "x" * 4001})
                with (kwargs["cwd"] / "hello.py").open("a") as file:
                    file.write("# rework after the preserved rejection\n")

        backend = ScriptedBackend(rework)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 2)
        first = result["review_history"]["greet"][0]
        self.assertEqual(first["status"], "invalid_rejected")
        self.assertEqual(first["review"]["findings"], rejected["findings"])
        returned = engine.directory / "dispatches" / first["dispatch_id"] / "returned-result.json"
        self.assertEqual(len(read_json(returned)["summary"]), 4001)
        self.assertFalse(any(d.get("retry_of") for d in result["dispatches"]))
        self.assertLessEqual(max(sizes), CONSTRAINED_CONTEXT_CHARS)


class RepeatedFinalRejectionTests(RepoCase):
    """A final excerpt reaches the correction worker inside the persisted decide trigger."""

    def reject_finals(self, rejections, long_dispositions=False):
        def change(backend, kwargs, packet, result):
            finals = sum(call["kind"] == "final_review" for call in backend.calls)
            if kwargs["kind"] == "final_review" and finals <= rejections:
                result.update(verdict="request_changes",
                              findings=[{**finding(), "id": f"final{finals}-{i}"} for i in range(30)])
                if long_dispositions:
                    for row in result.get("finding_dispositions", {}).values():
                        row["evidence"] = ["Checked the whole-run candidate against this finding " * 6]
            elif trigger_kind(kwargs, packet) == "final":
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id=f"correct{finals}", depends_on=["greet"])
                revised(result, packet, "Resolve final findings", new_tasks=[task])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"].startswith("correct"):
                with (kwargs["cwd"] / "hello.py").open("a") as file:
                    file.write(f"# correction {finals}\n")
        self.config.data["runtime"]["max_attempts"] = 10
        backend = ScriptedBackend(change)
        return backend, self.engine(backend)

    def assert_bound_final_feedback(self, backend, engine, task_id):
        """The correction worker got the last rejection's excerpt, bound to the saved review."""
        worker = [call for call in backend.calls if call["role"] == "worker" and call["task_id"] == task_id]
        self.assertEqual(len(worker), 1)
        record = worker[0]["packet"]["feedback"]["trigger"]["final_review"]
        excerpt = record["review"]
        rejected = engine.run["final_history"][int(task_id.removeprefix("correct")) - 1]
        # Only identities fit the review share, so the worker packet compacted an excerpt again.
        self.assertGreater(len(dump(excerpt)), REVIEW_CONTEXT_CHARS)
        # The excerpt's complete review is the artifact of the record holding it.
        self.assertNotIn("full_review_artifact", excerpt)
        self.assertEqual(record["artifact"], rejected["artifact"])
        self.assertEqual(excerpt["review_content_sha256"], review_digest(rejected["review"]))
        self.assertEqual(identities(excerpt), identities(rejected["review"]))

    def test_three_full_final_rejections_dispatch_correction_then_review_ninety_findings_in_two_batches(self):
        backend, engine = self.reject_finals(3)
        result = engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assert_bound_final_feedback(backend, engine, "correct3")
        self.assertIn("correct3", result["accepted"])
        # The later gates still apply: the 90 findings are reviewed in two batches on one snapshot.
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["status"] for item in result["final_history"]], ["rejected"] * 3 + ["approved"])
        finals = [call for call in backend.calls if call["kind"] == "final_review"]
        self.assertEqual([len(call["packet"]["required_findings"]) for call in finals], [0, 30, 60, 60, 30])
        last = result["final_history"][-1]
        self.assertEqual((len(last["approved_batches"]), len(last["required_findings"])), (1, 30))

    def test_identity_only_final_excerpt_dispatches_correction_and_final_acceptance_completes(self):
        backend, engine = self.reject_finals(2, long_dispositions=True)
        result = engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assert_bound_final_feedback(backend, engine, "correct2")
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["status"] for item in result["final_history"]], ["rejected", "rejected", "approved"])
        self.assertEqual(len(result["final_history"][-1]["required_findings"]), 60)
        self.assertEqual(set(result["accepted"]), {"greet", "correct1", "correct2"})


def long_finding(round_number, index):
    """A blocking finding of realistic length: a 27-character location, a 459-character message, two evidence lines."""
    return {"id": f"r{round_number}-{index}", "severity": "blocking", "category": "defect",
            "location": f"src/module_{index}/handler.py:{100 + round_number}",
            "message": ("The handler accepts an unchecked value and writes it without the guard the "
                        "acceptance criterion requires; the earlier fix covered only the first branch. ") * 3,
            "evidence": ["Read src/module/handler.py lines 90-140: the second branch has no guard and "
                         "the test only covers the first one.",
                         "tests/test_handler.py exercises only the happy path; no case drives the "
                         "rejected-value branch the criterion names."]}


class LongReworkContextTests(RepoCase):
    """Rule 14: a long rework stays within the default context once earlier mappings leave prior_reviews."""

    def rework(self, rejections, new=5):
        """Every worker changes the candidate; the first ``rejections`` task reviews each report ``new`` findings.

        Returns the backend, the run and each dispatch kind's required_chars in dispatch order.
        """
        self.config.data["runtime"]["max_attempts"] = 30
        rejected = []

        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                with (Path(kwargs["cwd"]) / "hello.py").open("a", encoding="utf-8") as stream:
                    stream.write(f"# rework {sum(call['role'] == 'worker' for call in backend.calls)}\n")
                for claim in result.get("repair_claims", {}).values():
                    claim["summary"] = ("Added the guard to the second branch and a test that drives the "
                                        "rejected value through it; the first branch keeps its earlier fix.")
            elif kwargs["kind"] == "review":
                for row in result.get("finding_dispositions", {}).values():
                    row["evidence"] = ["Re-read the changed handler: the guard now covers both branches and "
                                       "the new test drives the rejected value through the second branch.",
                                       "Verification output shows the new test passing on this candidate."]
                if len(rejected) < rejections:
                    rejected.append(True)
                    result.update(verdict="request_changes", summary="Changes requested. " * 20,
                                  findings=[long_finding(len(rejected), index) for index in range(new)])
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        result = engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        chars = {}
        for entry in result["dispatches"]:
            if entry.get("invoked") and entry["role"] in ("worker", "reviewer"):
                report = read_json(engine.directory / "dispatches" / entry["id"] / "context-report.json")
                chars.setdefault(entry["kind"], []).append(report["required_chars"])
        return backend, result, chars

    def test_fourteen_rounds_reach_approval_at_the_default_context(self):
        """T6-1: thirteen rejections of five long findings, then approval, at context_chars 100000."""
        self.assertEqual(self.config.data["runtime"]["context_chars"], 100000)
        backend, result, chars = self.rework(13)
        self.assertEqual(result["status"], "completed")
        self.assertIn("greet", result["accepted"])
        workers = [call for call in backend.calls if call["role"] == "worker"]
        reviews = [call for call in backend.calls if call["kind"] == "review"]
        self.assertEqual([len(call["packet"].get("required_findings", [])) for call in workers], [0] + [5] * 13)
        # The fourteenth candidate lacks 65 resolutions: two batches on one candidate.
        self.assertEqual([len(call["packet"].get("required_findings", [])) for call in reviews],
                         [5 * n for n in range(13)] + [60, 5])
        self.assertEqual((len(chars["implement"]), len(chars["review"])), (14, 15))
        self.assertLess(max(chars["implement"] + chars["review"]), 100000)


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
        # Every finding and its evidence stay; only the memory approvals are not projected.
        self.assertEqual(compacted["review"], review_view(review))
        self.assertEqual(compacted["review"]["findings"], review["findings"])
        self.assertGreater(len(json.dumps(compacted)), 32000)

    def test_compacted_review_compacts_again_with_its_original_binding(self):
        review = maximum_review()
        review["finding_dispositions"] = {f"f{index}": {"status": "open", "evidence": ["x" * 200]}
                                          for index in range(1, 61)}
        artifact = "/run/final-3/final.json"
        # FinalAcceptance.trigger compacts the record; a revision persists that trigger as feedback.
        trigger = {"kind": "final",
                   "final_review": compact_feedback({"final_review": {"artifact": artifact, "review": review}})["final_review"]}
        excerpt = trigger["final_review"]["review"]
        self.assertTrue(all(set(item) == {"id", "severity", "category"} for item in excerpt["findings"]))
        again = compact_feedback({"revision": 2, "plan_artifact": "/run/plan-2.json", "trigger": trigger})
        self.assertEqual(again["trigger"]["final_review"]["review"], excerpt)
        self.assertNotIn("full_review_artifact", excerpt)
        self.assertEqual((trigger["final_review"]["artifact"], excerpt["review_content_sha256"]),
                         (artifact, review_digest(review)))
        # A smaller share trims only the fields an excerpt still has, keeping its original digest;
        # the complete review is the caller's (enclosing record's) artifact, and without it nothing is trimmed.
        original = maximum_review()
        partial = compact_review(original, artifact)
        self.assertEqual(compact_review(partial, None, 2000), partial)
        smaller = compact_review(partial, artifact, 2000)
        self.assertTrue(all(set(item) == {"id", "severity", "category"} for item in smaller["findings"]))
        for value in (partial, smaller, compact_review(smaller, artifact, 2000)):
            self.assertNotIn("full_review_artifact", value)
            self.assertEqual(value["review_content_sha256"], review_digest(original))
            self.assertEqual(identities(value), identities(original))


if __name__ == "__main__":
    unittest.main()
