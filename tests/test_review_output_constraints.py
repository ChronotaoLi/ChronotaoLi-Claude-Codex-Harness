import copy
import unittest

from harness.adapters import provider_schema
from harness.contracts import (REVIEW, RepairableFormatError, envelope,
                               review_output_constraints, validate_review)
from harness.util import HarnessError


def finding(ident="empty-input", **changes):
    return {"id": ident, "severity": "blocking", "category": "defect",
            "location": "src/value.py:12", "message": "Empty input raises IndexError",
            "evidence": ["value([]) raises IndexError"], **changes}


def review(findings=(), approved_keys=()):
    return {"verdict": "request_changes", "summary": "Inspected the input handling",
            "findings": list(findings), "approved_memory_keys": list(approved_keys)}


class ReviewOutputConstraintTests(unittest.TestCase):
    def constraints(self, proposed_keys=()):
        return {item["path"]: item for item in review_output_constraints(
            envelope(REVIEW, "implement", "dispatch-1"), proposed_keys)}

    def assert_semantic_rejection(self, result, proposed_keys=()):
        with self.assertRaises(HarnessError) as raised:
            validate_review(result, proposed_keys)
        self.assertNotIsInstance(raised.exception, RepairableFormatError)

    def test_finding_identity_is_unique_even_when_other_fields_differ(self):
        bounds = self.constraints()["response.findings"]
        self.assertEqual(bounds["uniqueBy"], "id")
        self.assertEqual(bounds["maxItems"], 30)
        first = finding()
        second = finding(message="A different defect uses the same ID")
        self.assert_semantic_rejection(review([first, second]))
        validate_review(review([first, {**second, "id": "second-defect"}]))

    def test_blocking_condition_preserves_advisory_without_evidence(self):
        constraint = self.constraints()["response.findings[]"]
        self.assertEqual(constraint["if"], {"properties": {"severity": {"enum": ["blocking"]}}})
        required = constraint["then"]["properties"]
        self.assertEqual(required["evidence"]["minItems"], 1)
        self.assertEqual(set(required["category"]["enum"]), {"defect", "evidence", "scope"})
        for category in required["category"]["enum"]:
            with self.subTest(category=category):
                validate_review(review([finding(category=category)]))
                self.assert_semantic_rejection(review([finding(category=category, evidence=[])]))
        self.assert_semantic_rejection(review([finding(category="preference")]))
        validate_review(review([finding(severity="advisory", category="preference", evidence=[])]))

    def test_approval_subset_uses_current_proposals_and_does_not_mutate_schema(self):
        schema = envelope(REVIEW, "implement", "dispatch-1")
        before = copy.deepcopy(schema)
        provider_before = provider_schema(schema)
        keys = ["storage.format", "storage.ttl"]
        constraints = {item["path"]: item for item in review_output_constraints(schema, keys)}
        self.assertEqual(schema, before)
        self.assertEqual(provider_schema(schema), provider_before)
        approval = constraints["response.approved_memory_keys"]
        self.assertEqual(approval["subsetOf"], keys)
        self.assertEqual(approval["maxItems"], 8)
        self.assertTrue(approval["uniqueItems"])
        validate_review(review(approved_keys=[keys[0]]), keys)
        validate_review(review(approved_keys=keys), keys)
        validate_review(review(), keys)
        self.assert_semantic_rejection(review(approved_keys=["invented"]), keys)
        with self.assertRaises(RepairableFormatError):
            validate_review(review(approved_keys=[keys[0], keys[0]]), keys)
        keys.append("new-proposal")
        self.assertNotIn("new-proposal", approval["subsetOf"])

    def test_final_review_cannot_approve_any_memory(self):
        constraints = {item["path"]: item for item in review_output_constraints(
            envelope(REVIEW, "final", "final-dispatch"))}
        approval = constraints["response.approved_memory_keys"]
        self.assertEqual(approval["maxItems"], 0)
        self.assertEqual(approval["subsetOf"], [])
        validate_review(review())
        self.assert_semantic_rejection(review(approved_keys=["storage.format"]))


if __name__ == "__main__":
    unittest.main()
