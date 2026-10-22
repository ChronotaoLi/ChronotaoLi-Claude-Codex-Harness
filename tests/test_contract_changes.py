import copy
import unittest

from harness.contracts import (REPLAN, review_approved, validate, validate_review,
                               validate_plan, validate_revision, validate_work)
from harness.util import HarnessError


def task(ident="implement", depends_on=None):
    return {"id": ident, "title": ident, "description": "Implement original requirement",
            "depends_on": depends_on or [], "allowed_paths": ["src/*", "tests/*"],
            "acceptance": ["Original requirement remains observable"], "skills": []}


def finding(ident="edge-case", severity="blocking", category="defect"):
    return {"id": ident, "severity": severity, "category": category,
            "location": "src/value.py:12", "message": "Empty inputs fail",
            "evidence": ["An empty input raises IndexError"]}


def review(findings=None, verdict="approve", approved_keys=None):
    return {"verdict": verdict, "summary": "Independent inspection complete",
            "findings": findings or [], "approved_memory_keys": approved_keys or []}


class RevisionContractTests(unittest.TestCase):
    def test_revision_adds_prerequisite_without_losing_original_obligations(self):
        original = [task()]
        revised = copy.deepcopy(original)
        revised[0]["depends_on"] = ["foundation"]
        revised[0]["description"] = "Use the foundation for original requirement"
        prerequisite = task("foundation")
        prerequisite["allowed_paths"] = ["src/*"]
        revised.insert(0, prerequisite)
        self.assertEqual(validate_revision(original, revised, set()), revised)
        self.assertEqual(original[0]["depends_on"], [])

    def test_revision_preserves_accepted_task_exactly(self):
        original = [task("foundation"), task(depends_on=["foundation"])]
        for field, value in (("title", "renamed"), ("description", "different"),
                             ("skills", ["new-skill"]), ("depends_on", ["implement"])):
            revised = copy.deepcopy(original)
            revised[0][field] = value
            with self.subTest(field=field), self.assertRaises(HarnessError):
                validate_revision(original, revised, {"foundation"})

    def test_revision_cannot_drop_rename_weaken_or_expand_existing_task(self):
        original = [task()]
        variants = [[task("renamed")]]
        for field, value in (("acceptance", ["Simpler obligation"]),
                             ("allowed_paths", ["*"]), ("allowed_paths", ["src/*"])):
            revised = copy.deepcopy(original)
            revised[0][field] = value
            variants.append(revised)
        for revised in variants:
            with self.subTest(revised=revised), self.assertRaises(HarnessError):
                validate_revision(original, revised, set())

    def test_revision_cannot_remove_dependency_or_create_cycle(self):
        original = [task("foundation"), task(depends_on=["foundation"])]
        for cycle in (False, True):
            revised = copy.deepcopy(original)
            if cycle:
                revised[0]["depends_on"] = ["implement"]
            else:
                revised[1]["depends_on"] = []
            with self.subTest(cycle=cycle), self.assertRaises(HarnessError):
                validate_revision(original, revised, set())

    def test_new_tasks_cannot_introduce_new_scope_patterns(self):
        original = [task()]
        for scope in (["*"], ["secrets/*"], ["src/new.py"]):
            corrective = task("corrective")
            corrective["allowed_paths"] = scope
            with self.subTest(scope=scope), self.assertRaises(HarnessError):
                validate_revision(original, original + [corrective], set())

    def test_revision_rejects_noop_reordering_and_limit_bypass(self):
        original = [task("one"), task("two")]
        with self.assertRaisesRegex(HarnessError, "有效变更"):
            validate_revision(original, list(reversed(original)), set())
        with self.assertRaises(HarnessError):
            validate_revision(original, original + [task("three")], set(), max_tasks=2)
        with self.assertRaises(HarnessError):
            validate_revision(original, original + [task("three")], {"missing"})

    def test_stop_contract_retains_explicit_reason(self):
        validate({"action": "stop", "reason": "External credentials are unavailable", "new_tasks": [], "updates": []}, REPLAN)
        with self.assertRaises(HarnessError):
            validate({"action": "stop", "reason": "", "new_tasks": [], "updates": []}, REPLAN)

    def test_blank_acceptance_and_scope_do_not_create_obligations(self):
        for field in ("acceptance", "allowed_paths"):
            original = task()
            original[field] = [" \n "]
            with self.subTest(field=field), self.assertRaises(HarnessError):
                validate_plan({"tasks": [original]})


class AcceptanceContractTests(unittest.TestCase):
    def test_advisory_findings_do_not_block_approval(self):
        result = review([finding(severity="advisory", category="preference")])
        validate_review(result)
        self.assertTrue(review_approved(result))
        result["verdict"] = "request_changes"
        self.assertFalse(review_approved(result))

    def test_approve_verdict_cannot_bypass_blocking_finding(self):
        result = review([finding()])
        validate_review(result)
        self.assertFalse(review_approved(result))

    def test_blocking_findings_need_evidence_and_cannot_be_preferences(self):
        for changes in ({"evidence": []}, {"category": "preference"}):
            problem = {**finding(), **changes}
            with self.subTest(changes=changes), self.assertRaises(HarnessError):
                validate_review(review([problem]))

    def test_finding_ids_must_be_unique(self):
        with self.assertRaises(HarnessError):
            validate_review(review([finding(), finding()]))

    def test_memory_approval_is_independent_and_keys_must_exist(self):
        accepted = review(approved_keys=["architecture.storage"])
        validate_review(accepted, proposed_keys=["architecture.storage"])
        with self.assertRaises(HarnessError):
            validate_review(accepted)
        self.assertTrue(review_approved(review()))
        self.assertEqual(review()["approved_memory_keys"], [])

    def test_worker_blocker_classification_is_consistent(self):
        completed = {"status": "completed", "summary": "Complete", "evidence": [],
                     "blockers": [], "blocker_kind": "none", "proposed_memories": []}
        validate_work(completed)
        for change in ({"blocker_kind": "task"}, {"blockers": ["Missing dependency"]},
                       {"status": "blocked"}):
            with self.subTest(change=change), self.assertRaises(HarnessError):
                validate_work({**completed, **change})
        for kind in ("task", "environment"):
            validate_work({**completed, "status": "blocked", "blockers": ["Dependency unavailable"],
                           "blocker_kind": kind})

    def test_worker_proposals_are_bounded_and_unique(self):
        proposal = {"key": "architecture.storage", "abstract": "Storage", "content": "SQLite",
                    "source_paths": ["src/store.py"]}
        result = {"status": "completed", "summary": "Complete", "evidence": [],
                  "blockers": [], "blocker_kind": "none", "proposed_memories": [proposal]}
        validate_work(result)
        for proposals in ([proposal, proposal], [{**proposal, "content": "x" * 8001}],
                          [{**proposal, "key": "../operator"}], [{**proposal, "source_paths": []}]):
            with self.subTest(proposals=proposals), self.assertRaises(HarnessError):
                validate_work({**result, "proposed_memories": proposals})


if __name__ == "__main__":
    unittest.main()
