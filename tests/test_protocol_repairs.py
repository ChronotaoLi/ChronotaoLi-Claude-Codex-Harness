import copy
import json
import unittest

from harness.context import compact_checks, compact_feedback, compact_reviews, compose, review_view
from harness.contracts import (PLAN, WORK, ScopeExtensionRequired, apply_revision,
                               envelope, output_constraints, validate,
                               validate_plan, validate_review)
from harness.util import HarnessError, dump
from test_runtime_changes import validate_work_contract


def task(ident="implement", dependencies=None):
    return {"id": ident, "title": "Original title", "description": "Original requirement",
            "depends_on": dependencies or [], "allowed_paths": ["src/**", "tests/**"],
            "acceptance": ["Keep every original obligation"], "skills": []}


def work():
    return {"status": "completed", "summary": "Implemented", "evidence": [],
            "blockers": [], "blocker_kind": "none", "proposed_memories": []}


def finding(ident="missing-case", evidence="Empty input raises IndexError"):
    return {"id": ident, "severity": "blocking", "category": "defect",
            "location": "src/value.py:12", "message": "Empty input fails", "evidence": [evidence]}


def review(findings=None):
    return {"verdict": "request_changes", "summary": "Inspect the recorded failures",
            "findings": findings or [], "approved_memory_keys": []}


def revision(**changes):
    return {"action": "revise", "reason": "Add the missing prerequisite", "new_tasks": [],
            "updates": [], **changes}


class OutputContractTests(unittest.TestCase):
    def test_plan_constraints_expose_existing_identity_and_obligation_requirements(self):
        constraints = {item["path"]: item for item in output_constraints(PLAN)}
        self.assertEqual(constraints["response.tasks"]["minItems"], 1)
        self.assertEqual(constraints["response.tasks[].acceptance"]["minItems"], 1)
        self.assertEqual(constraints["response.tasks[].allowed_paths"]["minItems"], 1)
        for field in ("title", "description", "acceptance[]", "allowed_paths[]"):
            self.assertTrue(constraints[f"response.tasks[].{field}"]["non_blank"])
        self.assertIn("pattern", constraints["response.tasks[].id"])
        self.assertIn("pattern", constraints["response.tasks[].skills[]"])
        long_task = {**task(), "description": "x" * 10000}
        self.assertEqual(validate_plan({"tasks": [long_task]}), [long_task])

    def test_exposed_plan_format_does_not_mask_scope_or_dag_failures(self):
        for current, first in (({**task(), "description": "", "allowed_paths": ["../outside"]}, "描述不能为空"),
                               ({**task(), "skills": ["invalid_skill"], "depends_on": ["implement"]}, "依赖")):
            with self.subTest(current=current):
                with self.assertRaisesRegex(HarnessError, first):
                    validate_plan({"tasks": [current]})

    def test_exposed_constraints_follow_the_local_contract(self):
        schema = copy.deepcopy(WORK)
        schema["properties"]["summary"]["maxLength"] = 17
        constraints = {item["path"]: item for item in output_constraints(schema)}
        self.assertEqual(constraints["response.summary"]["maxLength"], 17)
        self.assertTrue(constraints["response.summary"]["non_blank"])
        self.assertTrue(constraints["response.proposed_memories[].source_paths"]["uniqueItems"])
        validate_work_contract({**work(), "summary": "x" * 32000})
        with self.assertRaisesRegex(HarnessError, "response.summary"):
            validate_work_contract({**work(), "summary": "x" * 32001})

    def test_hard_boundaries_take_priority_over_format_defects(self):
        schema = envelope(WORK, "implement", "dispatch-1")
        result = {"task_id": "implement", "dispatch_id": "dispatch-1", **work(),
                  "summary": "x" * 32001}
        for changes, first in (({"dispatch_id": "other"}, "dispatch_id"), ({"extra": "unexpected"}, "unexpected fields"),
                               ({"blocker_kind": "forged"}, "blocker_kind"),
                               ({"proposed_memories": [False]}, "proposed_memories")):
            with self.subTest(changes=changes):
                # Structure is reported before the summary bound.
                with self.assertRaisesRegex(HarnessError, first):
                    validate({**result, **changes}, schema)

    def test_worker_semantics_cannot_be_repaired_as_formatting(self):
        result = {**work(), "summary": "x" * 32001, "blocker_kind": "task"}
        with self.assertRaisesRegex(HarnessError, "blocker_kind"):
            validate_work_contract(result)

    def test_review_approvals_are_optional_and_blocking_evidence_is_checked_first(self):
        result = {**review(), "summary": "x" * 4001}
        # Unknown approvals are an optional product: only the summary bound is reported.
        with self.assertRaisesRegex(HarnessError, "response.summary"):
            validate_review({**result, "approved_memory_keys": ["unknown"]})
        with self.assertRaisesRegex(HarnessError, "阻断 finding 必须有证据"):
            validate_review({**result, "findings": [{**finding(), "evidence": []}]})

class IncrementalRevisionTests(unittest.TestCase):
    def test_incremental_update_preserves_runtime_owned_fields_and_inputs(self):
        original = [task()]
        original[0]["allowed_paths"] = ["src/cafe\u0301.py", "src/ space .py"]
        prerequisite = task("foundation")
        prerequisite["allowed_paths"] = original[0]["allowed_paths"][:1]
        operation = revision(new_tasks=[prerequisite], updates=[
            {"id": "implement", "description": "Use the prerequisite", "add_depends_on": ["foundation"]}])
        before = copy.deepcopy((original, operation))
        revised = apply_revision(original, operation, set())
        self.assertEqual((original, operation), before)
        self.assertEqual(revised[0]["depends_on"], ["foundation"])
        for field in ("id", "title", "allowed_paths", "acceptance", "skills"):
            self.assertEqual(revised[0][field], original[0][field])
        revised[0]["allowed_paths"].append("other")
        self.assertEqual((original, operation), before)

    def test_updates_cannot_supply_immutable_fields_or_touch_accepted_tasks(self):
        original = [task()]
        update = {"id": "implement", "description": "Refined brief", "add_depends_on": []}
        for field in ("title", "allowed_paths", "acceptance", "skills", "depends_on"):
            with self.subTest(field=field), self.assertRaises(HarnessError):
                apply_revision(original, revision(updates=[{**update, field: original[0][field]}]), set())
        with self.assertRaises(HarnessError):
            apply_revision(original, revision(updates=[update]), {"implement"})

    def test_unknown_duplicate_and_cyclic_updates_fail_before_mutation(self):
        original = [task("foundation"), task(dependencies=["foundation"])]
        before = copy.deepcopy(original)
        update = {"id": "implement", "description": "Refined", "add_depends_on": []}
        variants = [[{**update, "id": "unknown"}], [update, update],
                    [{**update, "id": "foundation", "add_depends_on": ["implement"]}]]
        for updates in variants:
            with self.subTest(updates=updates), self.assertRaises(HarnessError):
                apply_revision(original, revision(updates=updates), set())
            self.assertEqual(original, before)

    def test_new_tasks_and_noop_updates_cannot_bypass_revision_gates(self):
        original = [task()]
        for operation in (revision(), revision(new_tasks=[task("implement")]),
                          revision(new_tasks=[{**task("extra"), "allowed_paths": ["*"]}]),
                          revision(updates=[{"id": "implement", "description": original[0]["description"],
                                             "add_depends_on": []}])):
            with self.subTest(operation=operation), self.assertRaises(HarnessError):
                apply_revision(original, operation, set())
        with self.assertRaises(HarnessError):
            apply_revision(original, revision(new_tasks=[task("extra")]), set(), max_tasks=1)

    def test_stop_has_no_operations_and_returns_an_independent_snapshot(self):
        original = [task()]
        stopped = apply_revision(original, revision(action="stop"), set())
        self.assertEqual(stopped, original)
        self.assertIsNot(stopped[0], original[0])
        with self.assertRaises(HarnessError):
            apply_revision(original, revision(action="stop", new_tasks=[task("extra")]), set())

    def test_replan_bounds_error_cannot_hide_scope_cycle_or_stop_operation_errors(self):
        original = [task()]
        for operation, scope in ((revision(action="stop", new_tasks=[task("extra")]), False),
                                 (revision(new_tasks=[{**task("extra"), "allowed_paths": ["secrets/**"]}]), True),
                                 (revision(updates=[{"id": "implement", "description": "Refined",
                                                     "add_depends_on": ["implement"]}]), False)):
            operation["reason"] = "x" * 4001
            with self.subTest(operation=operation["action"], scope=scope):
                with self.assertRaises(HarnessError) as raised:
                    apply_revision(original, operation, set())
                self.assertNotIn("response.reason", str(raised.exception))
                # Only an unapproved scope can be resolved by an operator's grant.
                self.assertEqual(isinstance(raised.exception, ScopeExtensionRequired), scope)
        with self.assertRaisesRegex(HarnessError, "response.reason") as raised:
            apply_revision(original, revision(action="stop", reason="x" * 4001), set())
        self.assertNotIsInstance(raised.exception, ScopeExtensionRequired)


class ContextCompactionTests(unittest.TestCase):
    def test_noisy_verification_preserves_outcomes_and_fits_a_small_packet(self):
        checks = [{"argv": ["python3", "check.py", str(i)], "exit_code": 1,
                   "log": f"verification/{i}", "stdout": "o" * 8000,
                   "stderr": "e" * 8000, "timed_out": i == 2} for i in range(3)]
        original = copy.deepcopy(checks)
        compacted = compact_checks(checks)
        for old, new in zip(checks, compacted):
            for key in ("argv", "exit_code", "log", "timed_out"):
                self.assertEqual(new[key], old[key])
            self.assertLessEqual(len(new["stdout"]) + len(new["stderr"]), 500)
            self.assertTrue(new["stdout"] and new["stderr"])
            self.assertTrue(new["output_truncated"])
        prompt, report = compose({"task": task(), "verification": compacted}, [], 5000)
        self.assertLessEqual(report["used_chars"], 5000)
        self.assertEqual(json.loads(prompt)["task"]["acceptance"], task()["acceptance"])
        self.assertEqual(checks, original)
        self.assertGreater(len(dump(checks)), 32000)

    def test_reviews_keep_all_distinct_evidence_and_the_complete_latest_review(self):
        first, changed = finding(), finding(evidence="A new repro still fails")
        history = [{"attempt": 1, "review": review([first]), "artifact": "review-1.json"},
                   {"attempt": 2, "review": review([first]), "artifact": "review-2.json"},
                   {"attempt": 3, "review": review([changed]), "artifact": "review-3.json"},
                   {"attempt": 4, "review": {**review(), "verdict": "approve"}, "artifact": "review-4.json"}]
        original = copy.deepcopy(history)
        compacted = compact_reviews(history)
        self.assertEqual(compacted[-1], {**history[-1], "review": review_view(history[-1]["review"])})
        findings = [finding for item in compacted for finding in item["review"]["findings"]]
        self.assertCountEqual(findings, [first, changed])
        self.assertEqual([item["artifact"] for item in compacted], [item["artifact"] for item in history])
        self.assertEqual(history, original)
        self.assertEqual(compact_reviews([history[0], history[0]]),
                         [{**history[0], "review": review_view(history[0]["review"])}])

    def test_feedback_deduplicates_only_reviews_already_in_the_packet(self):
        result = review([finding()])
        history = [{"review": result, "artifact": "review.json"}]
        feedback = {"task_id": "implement", "review": result, "artifact": "review.json",
                    "trigger": {"verification": [{"argv": ["check"], "exit_code": 1,
                                 "stdout": "x" * 8000, "stderr": "", "log": "check"}]}}
        original = copy.deepcopy(feedback)
        compacted = compact_feedback(feedback, compact_reviews(history))
        self.assertNotIn("review", compacted)
        self.assertTrue(compacted["review_in_prior_reviews"])
        self.assertEqual(compacted["artifact"], "review.json")
        self.assertLessEqual(len(compacted["trigger"]["verification"][0]["stdout"]), 500)
        self.assertEqual(compact_feedback(feedback)["review"], review_view(result))
        self.assertEqual(feedback, original)


if __name__ == "__main__":
    unittest.main()
