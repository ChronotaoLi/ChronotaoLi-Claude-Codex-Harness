import copy
import unittest

from harness.cli import status_summary
from harness.convergence import convergence_summary
from harness.versions import RUN_SCHEMA_VERSION


def rejection(*ids, attempt=None, dispatch=None, verdict="request_changes", dispositions=None, **extra):
    record = {"review": {"verdict": verdict, "findings": [
        {"id": ident, "severity": "blocking"} for ident in ids]}}
    if dispositions is not None:
        # Where the runtime keeps them: inside the returned review, keyed f1..fN.
        record["review"]["finding_dispositions"] = {key: {"status": status, "evidence": ["checked on this candidate"]}
                                                    for key, status in dispositions.items()}
    if attempt is not None:
        record.update(attempt=attempt, snapshot_digest=f"s{attempt}")
    if dispatch is not None:
        record["review_source"] = {"dispatch_id": dispatch, "snapshot_digest": record.get("snapshot_digest")}
    return {**record, **extra}


def approval(attempt, dispatch, **extra):
    return rejection(attempt=attempt, dispatch=dispatch, verdict="approve", **extra)


def ref(record, number, outcome):
    """The reference a hint gives for the record at this round."""
    return {"round": number, "attempt": record.get("attempt"),
            "review_dispatch_id": (record.get("review_source") or {}).get("dispatch_id"),
            "snapshot_digest": record.get("snapshot_digest"),
            "verdict": (record.get("review") or {}).get("verdict"), "outcome": outcome}


def task_hint(run, count, task_id="t"):
    warnings = [item for item in convergence_summary(run, {task_id: count})["warnings"] if item["kind"] == "task_rework"]
    return warnings[0] if warnings else None


def running(history, attempts, **fields):
    return {"status": "running", "accepted": {}, "attempts": {"t": attempts}, "review_history": {"t": history}, **fields}


class ConvergenceTests(unittest.TestCase):
    def test_recurring_blocker_and_work_attempts_are_advisory_and_do_not_mutate_ledger(self):
        run = {"status": "running", "accepted": {}, "attempts": {"task": 2},
               "review_history": {"task": [rejection("same", "fixed"), rejection("same", "new")]}}
        before = copy.deepcopy(run)
        warning = status_summary(run)["convergence"]["warnings"][0]
        self.assertEqual(warning["repeated_labels"], ["same"])
        self.assertEqual(warning["review_rejections"], 2)
        self.assertEqual(run, before)
        self.assertTrue(status_summary(run)["convergence"]["advisory_only"])

    def test_accepted_and_terminal_runs_do_not_report_resolved_rework_as_current(self):
        run = {"status": "running", "accepted": {"old": {}},
               "review_history": {"old": [rejection("x"), rejection("x")]},
               "final_history": [{"status": "rejected"}, {"status": "rejected"}]}
        result = convergence_summary(run, {"old": 10, "pending": 3})
        self.assertEqual([w["kind"] for w in result["warnings"]], ["task_rework", "whole_run_rework"])
        for status in ("completed", "abandoned"):
            self.assertEqual(convergence_summary({**run, "status": status}, {"old": 10})["warnings"], [])

    def test_signal_uses_work_attempts_and_only_consecutive_rejections(self):
        run = {"schema_version": RUN_SCHEMA_VERSION, "status": "running", "attempts": {"task": 3},
               "dispatches": [{"role": "worker", "task_id": "task", "attempt_exemption": {
                   "blocker_kind": "environment", "artifact": "/evidence/returned-result.json",
                   "artifact_digest": "0" * 64}}],
               "review_history": {"task": [rejection("x"), {"review": {"verdict": "approve"}}]}}
        # Observation uses saved accounting, not an extra interpretation of retries.
        self.assertEqual(status_summary(run)["budgets"]["work_attempts"], {"task": 2})
        self.assertEqual(status_summary(run)["convergence"]["warnings"], [])

    def test_damaged_history_and_large_warning_sets_remain_bounded(self):
        run = {"status": "running", "accepted": {}, "review_history": {"bad": None}, "final_history": None}
        result = convergence_summary(run, {str(i): 3 for i in range(30)})
        self.assertEqual(len(result["warnings"]), 20)
        self.assertTrue(result["truncated"])

    def test_hint_names_the_compared_attempts_and_a_newer_attempt(self):
        history = [rejection(f"only-{n}", *(["shared"] if n >= 4 else []), attempt=n, dispatch=f"d{n}")
                   for n in range(1, 6)]
        hint = task_hint(running(history, 6), 6)
        self.assertEqual(hint, {
            "kind": "task_rework", "task_id": "t", "work_attempts": 6, "review_rejections": 5,
            "repeated_labels": ["shared"], "repeated_labels_truncated": False,
            "reasons": ["work_attempts", "repeated_labels"],
            "compared_reviews": [ref(history[3], 4, "rejected"), ref(history[4], 5, "rejected")],
            "latest_review": ref(history[4], 5, "rejected"), "latest_attempt": 6, "attempts_since_latest_review": 1})
        self.assertEqual(hint["compared_reviews"][0]["review_dispatch_id"], "d4")
        # The sixth attempt's rejection concludes it: the comparison moves and nothing newer has started.
        sixth = rejection("shared", attempt=6, dispatch="d6")
        hint = task_hint(running([*history, sixth], 6), 6)
        self.assertEqual(hint["compared_reviews"], [ref(history[4], 5, "rejected"), ref(sixth, 6, "rejected")])
        self.assertEqual((hint["review_rejections"], hint["latest_review"]["attempt"], hint["attempts_since_latest_review"]),
                         (6, 6, 0))

    def test_attempts_alone_give_no_review_source(self):
        hint = task_hint(running([], 3), 3)
        self.assertEqual(hint, {
            "kind": "task_rework", "task_id": "t", "work_attempts": 3, "review_rejections": 0,
            "repeated_labels": [], "repeated_labels_truncated": False, "reasons": ["work_attempts"],
            "compared_reviews": [], "latest_review": None, "latest_attempt": 3, "attempts_since_latest_review": 3})
        # Labels that differ give no source either.
        hint = task_hint(running([rejection("a", attempt=1, dispatch="d1"), rejection("b", attempt=2, dispatch="d2")], 3), 3)
        self.assertEqual((hint["reasons"], hint["compared_reviews"], hint["latest_review"]["round"]),
                         (["work_attempts"], [], 2))

    def test_an_attempt_in_its_review_stage_has_no_conclusion(self):
        # Attempt 1 left no review record (its preflight failed); attempts 2 and 3 were rejected.
        concluded = [rejection("X", attempt=2, dispatch="d2"), rejection("X", "Y", attempt=3, dispatch="d3")]
        stage = {"task_id": "t", "attempt": 4, "stage": "reviewing"}
        before = task_hint(running(concluded, 4, pending_task=stage), 4)
        self.assertEqual((before["review_rejections"], before["repeated_labels"]), (2, ["X"]))
        self.assertEqual([item["round"] for item in before["compared_reviews"]], [1, 2])
        self.assertEqual((before["latest_review"]["attempt"], before["attempts_since_latest_review"]), (3, 1))
        # The first batch of attempt 4 approved and was registered; its stage stays open.
        batch = approval(4, "d4a")
        self.assertEqual(task_hint(running([*concluded, batch], 4, pending_task=stage), 4), before)
        # The stage ended before the last batch (the candidate changed): still no conclusion for attempt 4.
        self.assertEqual(task_hint(running([*concluded, batch], 4), 4), before)
        # Whatever an open stage's records say, they do not conclude its attempt.
        self.assertEqual(task_hint(running([*concluded, rejection("X", attempt=4, dispatch="d4")], 4,
                                           pending_task=stage), 4), before)
        # Another task's open stage does not hold this one's attempt open.
        other = task_hint(running([*concluded, rejection("X", attempt=4, dispatch="d4")], 4,
                                  pending_task={**stage, "task_id": "u"}), 4)
        self.assertEqual(other["latest_review"]["attempt"], 4)

    def test_a_rejecting_last_batch_concludes_the_attempt_once(self):
        history = [rejection("X", attempt=2, dispatch="d2"), rejection("X", attempt=3, dispatch="d3"),
                   approval(4, "d4a"), approval(4, "d4b"), rejection("X", "Z", attempt=4, dispatch="d4c")]
        hint = task_hint(running(history, 4), 4)
        self.assertEqual(hint["review_rejections"], 3)
        self.assertEqual(hint["latest_review"], ref(history[4], 5, "rejected"))
        self.assertEqual(hint["compared_reviews"], [ref(history[1], 2, "rejected"), ref(history[4], 5, "rejected")])
        self.assertEqual(hint["repeated_labels"], ["X"])
        # Labels come from the concluding batch: one without X compares nothing.
        history[4] = rejection("Z", attempt=4, dispatch="d4c")
        hint = task_hint(running(history, 4), 4)
        self.assertEqual((hint["repeated_labels"], hint["compared_reviews"], hint["latest_review"]["round"]), ([], [], 5))

    def test_rejection_is_the_gate_decision_not_the_raw_verdict(self):
        first = rejection("X", attempt=1, dispatch="d1")
        variants = {
            "blocking finding": rejection("X", attempt=2, dispatch="d2", verdict="approve"),
            "disposition errors": approval(2, "d2", disposition_errors=[{"code": "finding_not_independently_resolved", "key": "f1",
                                                                         "review_dispatch_id": "d1", "finding_index": 0}]),
            "invalid rejected": approval(2, "d2", status="invalid_rejected", validation_error="f1 is open"),
            "validation error": approval(2, "d2", validation_error="schema mismatch"),
        }
        for name, vetoed in variants.items():
            with self.subTest(name=name):
                hint = task_hint(running([first, vetoed], 3), 3)
                self.assertEqual(hint["review_rejections"], 2)
                self.assertEqual(hint["latest_review"], ref(vetoed, 2, "rejected"))
                self.assertEqual(hint["latest_review"]["verdict"], "approve")
                # A vetoed approval is compared like any rejection: it ends a pair it does not share labels with.
                third = rejection("X", attempt=3, dispatch="d3")
                later = task_hint(running([first, vetoed, third], 3), 3)
                self.assertEqual(later["review_rejections"], 3)
                shares = name == "blocking finding"
                self.assertEqual(later["repeated_labels"], ["X"] if shares else [])
                self.assertEqual([item["round"] for item in later["compared_reviews"]], [2, 3] if shares else [])
        hint = task_hint(running([first, variants["blocking finding"]], 2), 2)
        self.assertEqual(hint["compared_reviews"], [ref(first, 1, "rejected"), ref(variants["blocking finding"], 2, "rejected")])
        self.assertEqual([item["verdict"] for item in hint["compared_reviews"]], ["request_changes", "approve"])
        # A gap or an invalid rejection that reports the previous rejection's label again is compared with it.
        shared = {
            "disposition errors": rejection("X", attempt=2, dispatch="d2", verdict="approve", dispositions={"f1": "open"},
                                            disposition_errors=[{"code": "finding_not_independently_resolved", "key": "f1"}]),
            "invalid rejected": rejection("X", attempt=2, dispatch="d2", verdict="approve", dispositions={"f1": "resolved"},
                                          status="invalid_rejected", validation_error="a blocking finding has no evidence"),
        }
        for name, vetoed in shared.items():
            with self.subTest(name=name, shared="X"):
                hint = task_hint(running([first, vetoed], 2), 2)
                self.assertEqual((hint["reasons"], hint["repeated_labels"], hint["review_rejections"]),
                                 (["repeated_labels"], ["X"], 2))
                self.assertEqual(hint["compared_reviews"], [ref(first, 1, "rejected"), ref(vetoed, 2, "rejected")])

    def test_only_the_approval_awaiting_acceptance_is_a_conclusion(self):
        history = [rejection("X", attempt=1, dispatch="d1"), rejection("X", attempt=2, dispatch="d2"),
                   approval(3, "d3a"), approval(3, "d3b")]
        accepting = {"task_id": "t", "attempt": 3, "review_source": dict(history[3]["review_source"])}
        hint = task_hint(running(history, 3, pending_acceptance=accepting), 3)
        self.assertEqual(hint["latest_review"], ref(history[3], 4, "approved"))
        self.assertEqual((hint["attempts_since_latest_review"], hint["repeated_labels"], hint["compared_reviews"],
                          hint["review_rejections"], hint["reasons"]), (0, [], [], 2, ["work_attempts"]))
        unmatched = {"another source": {**accepting, "review_source": {"dispatch_id": "d3a"}},
                     "another attempt": {**accepting, "attempt": 2}, "another task": {**accepting, "task_id": "u"},
                     "no acceptance": None}
        for name, pending in unmatched.items():
            with self.subTest(name=name):
                fields = {"pending_acceptance": pending} if pending else {}
                hint = task_hint(running(history, 3, **fields), 3)
                self.assertEqual(hint["latest_review"], ref(history[1], 2, "rejected"))
                self.assertEqual((hint["attempts_since_latest_review"], hint["repeated_labels"]), (1, ["X"]))

    def test_invalidated_records_conclude_without_counting_or_comparing(self):
        concluded = [rejection("X", attempt=1, dispatch="d1"), rejection("X", attempt=2, dispatch="d2")]
        kept = {"status": "invalidated", "attempt": 3, "dispatch_id": "d3", "snapshot_digest": "s3",
                "review_source": {"dispatch_id": "d3"}, "review": {"verdict": "request_changes",
                                                                  "findings": [{"id": "X", "severity": "blocking"}]}}
        unreadable = {"status": "invalidated", "attempt": 3, "dispatch_id": "d3", "snapshot_digest": "s3",
                      "review_source": {"dispatch_id": "d3"}, "validation_error": "review result is not a JSON object"}
        voided = [approval(3, "d3a"), approval(3, "d3b", status="invalidated")]
        for name, records in {"kept original": [kept], "unreadable original": [unreadable],
                              "voided approval": voided}.items():
            with self.subTest(name=name):
                history = [*concluded, *records]
                hint = task_hint(running(history, 3), 3)
                self.assertEqual(hint["latest_review"], ref(history[-1], len(history), "invalidated"))
                self.assertEqual((hint["review_rejections"], hint["repeated_labels"], hint["compared_reviews"]), (2, [], []))
                # A rejection after it is compared with nothing: its predecessor conclusion is not a rejection.
                after = task_hint(running([*history, rejection("X", attempt=4, dispatch="d4")], 4), 4)
                self.assertEqual((after["review_rejections"], after["repeated_labels"], after["latest_review"]["attempt"]),
                                 (3, [], 4))

    def test_reopened_findings_are_not_repeated_labels(self):
        first = rejection("A", attempt=1, dispatch="d1")
        reopened = rejection(attempt=2, dispatch="d2", dispositions={"f1": "open"})
        self.assertIsNone(task_hint(running([first, reopened], 2), 2))
        hint = task_hint(running([first, reopened], 3), 3)
        self.assertEqual((hint["reasons"], hint["repeated_labels"], hint["review_rejections"]), (["work_attempts"], [], 2))
        hint = task_hint(running([first, rejection("A", attempt=2, dispatch="d2")], 2), 2)
        self.assertEqual((hint["reasons"], hint["repeated_labels"]), (["repeated_labels"], ["A"]))

    def test_whole_run_hint_dates_its_rejection_and_latest_attempt(self):
        def attempt(number, status, phase, **extra):
            return {"attempt": number, "status": status, "phase": phase, "snapshot_digest": f"s{number}",
                    "review_source": {"dispatch_id": f"d{number}"}, **extra}

        def whole(history):
            run = {"status": "running", "accepted": {}, "final_history": history}
            return [item for item in convergence_summary(run, {})["warnings"] if item["kind"] == "whole_run_rework"]

        rejected = [attempt(1, "rejected", "finished"), attempt(2, "rejected", "finished")]
        verifying = {"attempt": 3, "status": "running", "phase": "verifying", "snapshot_digest": "s3"}
        self.assertEqual(whole([*rejected, verifying]), [{
            "kind": "whole_run_rework", "review_rejections": 2,
            "latest_rejection": {"attempt": 2, "review_dispatch_id": "d2", "snapshot_digest": "s2"},
            "latest_final_attempt": {"attempt": 3, "status": "running", "phase": "verifying"}}])
        # An approved intermediate batch keeps the attempt running in its review phase.
        batches = {**verifying, "phase": "reviewing", "approved_batches": [{"dispatch_id": "d3a"}]}
        self.assertEqual(whole([*rejected, batches])[0]["latest_final_attempt"],
                         {"attempt": 3, "status": "running", "phase": "reviewing"})
        # A left attempt that approved nothing is no rejection; a left veto still rejects the snapshot it reviewed.
        left = attempt(2, "invalidated", "finished")
        self.assertEqual(whole([rejected[0], left, {**verifying}]), [])
        hint = whole([rejected[0], left, attempt(3, "invalid_rejected", "finished")])[0]
        self.assertEqual((hint["review_rejections"], hint["latest_rejection"]["attempt"], hint["latest_final_attempt"]),
                         (2, 3, {"attempt": 3, "status": "invalid_rejected", "phase": "finished"}))
        self.assertEqual(whole([*rejected, attempt(3, "approved", "finished")]), [])

    def test_damaged_shapes_never_raise(self):
        record = rejection("X", attempt=1, dispatch="d1")
        damaged_runs = [
            None, [], {"status": "running", "accepted": 5, "attempts": [], "review_history": {"t": [record, record]}},
            running([record, "x", None, {"attempt": True, "review": "text"}, {"review": {"findings": {"a": 1}}},
                     {"status": ["invalidated"], "review": {"verdict": None, "findings": [None, {"severity": "blocking"}]}}],
                    "six", pending_task=[], pending_acceptance="t"),
            running([approval(1, "d1")], 3, pending_acceptance={"task_id": "t", "attempt": 1, "review_source": None}),
            running([{**approval(1, None), "review_source": None}], 3,
                    pending_acceptance={"task_id": "t", "attempt": 1}),
            running([record, rejection("X", attempt=2, dispatch="d2")], 3, pending_task={"task_id": "t", "attempt": True}),
            {"status": "running", "final_history": [None, {"status": "rejected", "review_source": "x", "attempt": "2"},
                                                    {"status": "rejected", "phase": 7, "snapshot_digest": {}}]},
            {"status": "running", "final_history": [{"status": "rejected"}, {"status": "rejected"}, "tail"]},
        ]
        for run in damaged_runs:
            with self.subTest(run=run):
                result = convergence_summary(run, {"t": 3})
                self.assertTrue(result["advisory_only"])
        # A boolean attempt never opens or matches an attempt, and a missing source never marks an approval.
        hint = task_hint(damaged_runs[6], 3)
        self.assertEqual((hint["review_rejections"], hint["latest_review"]["attempt"]), (2, 2))
        for index in (4, 5):
            self.assertIsNone(task_hint(damaged_runs[index], 3)["latest_review"])
        whole = convergence_summary(damaged_runs[7], {})["warnings"][0]
        self.assertEqual((whole["latest_rejection"], whole["latest_final_attempt"]),
                         ({"attempt": None, "review_dispatch_id": None, "snapshot_digest": None},
                          {"attempt": None, "status": "rejected", "phase": None}))
        # A damaged ledger's tail is not an attempt: two rejections, the last object still rejected.
        self.assertEqual(convergence_summary(damaged_runs[8], {})["warnings"][0]["latest_final_attempt"]["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
