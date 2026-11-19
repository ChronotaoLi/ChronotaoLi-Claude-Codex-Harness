"""Read-only metric: blocking finding IDs first reported in each task review round."""
import contextlib
import io
import json
import unittest
from pathlib import Path

from harness import cli, observe
from harness.cli import status_summary
from harness.convergence import review_rounds
from harness.util import read_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend


def finding(ident):
    return {"id": ident, "severity": "blocking", "category": "defect", "location": "hello.py:1",
            "message": f"Defect {ident}", "evidence": ["test_hello.py requires the greeting"]}


def calls(backend, **match):
    """Calls so far (the current one included) whose role and kind match."""
    return sum(all(call[key] == value for key, value in match.items()) for call in backend.calls)


class ReviewRoundLifecycleTests(RepoCase):
    def test_three_round_run_marks_the_new_blocking_id_in_round_two(self):
        reviews = []

        def rounds(backend, kwargs, packet, result):
            if kwargs["role"] == "reviewer" and kwargs["kind"] == "review":
                reviews.append(packet)
                if len(reviews) == 1:
                    result.update(verdict="request_changes",
                                  findings=[finding("OLD-001"), {**finding("NOTE-9"), "severity": "advisory"}])
                elif len(reviews) == 2:
                    # The first round's issue is resolved while a new blocking one appears.
                    self.assertEqual([row["label"] for row in packet["required_findings"]], ["OLD-001"])
                    self.assertEqual(list(result["finding_dispositions"]), ["f1"])
                    result.update(verdict="request_changes", findings=[finding("NEW-002")])
        backend = ScriptedBackend(rounds)
        engine = self.engine(backend)
        result = engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(reviews), 3)
        history = result["review_history"]["greet"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--repo", str(self.repo), "status", result["id"]]), 0)
        printed = json.loads(output.getvalue())
        metric = printed["review_rounds"]
        self.assertTrue(metric["read_only"])
        self.assertEqual(metric["cause"], "unknown")
        rows = metric["tasks"]["greet"]
        self.assertEqual([(row["round"], row["verdict"], row["new_blocking"]) for row in rows],
                         [(1, "request_changes", ["OLD-001"]), (2, "request_changes", ["NEW-002"]),
                          (3, "approve", [])])
        self.assertEqual([row["review_dispatch_id"] for row in rows],
                         [record["review_source"]["dispatch_id"] for record in history])
        self.assertEqual([row["snapshot_digest"] for row in rows], [record["snapshot_digest"] for record in history])
        self.assertEqual([row["attempt"] for row in rows], [1, 2, 3])
        reviewer_ids = [d["id"] for d in result["dispatches"] if d["role"] == "reviewer" and d["kind"] == "review"]
        self.assertEqual([row["review_dispatch_id"] for row in rows], reviewer_ids)
        # The projection is the same through the dashboard's read-only observation layer.
        detail = observe.run_detail(self.repo / ".harness", result["id"])
        self.assertEqual(detail["summary"]["review_rounds"], metric)
        # Reading the metric made no model call.
        calls = len(backend.calls)
        status_summary(read_json(engine.directory / "state.json"))
        self.assertEqual(len(backend.calls), calls)

    def hint_during(self, kwargs):
        """The rework hint the dashboard reads in the middle of a model call."""
        state_json = Path(kwargs["run_path"])
        warnings = observe.run_detail(state_json.parents[2], state_json.parent.name)["summary"]["convergence"]["warnings"]
        return [item for item in warnings if item["kind"] == "task_rework"]

    def test_sixth_attempt_after_five_rejections_is_dated(self):
        self.config.data["runtime"]["max_attempts"] = 6
        seen = {}

        def script(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                path = Path(kwargs["cwd"]) / "hello.py"
                path.write_text(path.read_text(encoding="utf-8") + f"# attempt {calls(backend, role='worker')}\n",
                                encoding="utf-8")
            elif kwargs["kind"] == "review" and calls(backend, kind="review") <= 5:
                result.update(verdict="request_changes", findings=[finding("ds1-write-gate")])

        def during(backend, kwargs, packet):
            if calls(backend, role="worker") == 6 and kwargs["role"] == "worker":
                seen["sixth worker"] = self.hint_during(kwargs)
            if calls(backend, kind="review") == 6 and kwargs["kind"] == "review":
                seen["sixth review"] = self.hint_during(kwargs)

        backend = ScriptedBackend(script)
        backend.before_result = during
        result = self.engine(backend).start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(result["status"], "completed")
        history = result["review_history"]["greet"]
        reviewers = [d["id"] for d in result["dispatches"] if d["role"] == "reviewer" and d["kind"] == "review"]
        self.assertEqual([record["review_source"]["dispatch_id"] for record in history], reviewers)
        reference = lambda n: {"round": n, "attempt": n, "review_dispatch_id": reviewers[n - 1],
                               "snapshot_digest": history[n - 1]["snapshot_digest"],
                               "verdict": "request_changes", "outcome": "rejected"}
        self.assertEqual(seen["sixth worker"], [{
            "kind": "task_rework", "task_id": "greet", "work_attempts": 6, "review_rejections": 5,
            "repeated_labels": ["ds1-write-gate"], "repeated_labels_truncated": False,
            "reasons": ["work_attempts", "repeated_labels"], "compared_reviews": [reference(4), reference(5)],
            "latest_review": reference(5), "latest_attempt": 6, "attempts_since_latest_review": 1}])
        # The sixth review is running: its attempt has no conclusion yet.
        self.assertEqual(seen["sixth review"], seen["sixth worker"])
        self.assertEqual(observe.run_detail(self.repo / ".harness", result["id"])["summary"]["convergence"]["warnings"], [])

    def test_batches_conclude_an_attempt_only_at_its_last_batch(self):
        """63 historical findings make attempt 4 a review in two batches (60 and 3 rows)."""
        self.config.data["runtime"]["max_attempts"] = 5
        seen = {}

        def script(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                path = Path(kwargs["cwd"]) / "hello.py"
                path.write_text(path.read_text(encoding="utf-8") + f"# worker {calls(backend, role='worker')}\n",
                                encoding="utf-8")
                return
            if kwargs["kind"] != "review":
                return
            number = calls(backend, kind="review")
            new = [finding(f"r{number}-{index}") for index in range(20)] + [finding("dup")]
            if number in (1, 3):
                result.update(verdict="request_changes", findings=new)
            elif number == 2:
                result.update(verdict="approve", findings=new)  # the gate rejects an approval with blocking findings
            elif number == 5:
                # The second batch of attempt 4 reports nothing new: every old finding stays open.
                result.update(verdict="request_changes", findings=[])
                for row in result["finding_dispositions"].values():
                    row.update(status="open", evidence=["still wrong on this candidate"])

        def during(backend, kwargs, packet):
            if kwargs["kind"] == "review" and calls(backend, kind="review") in (4, 5):
                run = read_json(Path(kwargs["run_path"]))
                seen[f"review {calls(backend, kind='review')}"] = {
                    "rows": len(packet["required_findings"]), "pending": run["pending_task"]["attempt"],
                    "records": len(run["review_history"]["greet"]), "hint": self.hint_during(kwargs)}
            if kwargs["role"] == "worker" and calls(backend, role="worker") == 5:
                seen["worker 5"] = {"hint": self.hint_during(kwargs)}

        backend = ScriptedBackend(script)
        backend.before_result = during
        result = self.engine(backend).start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(result["status"], "completed")
        history = result["review_history"]["greet"]
        self.assertEqual([(record["attempt"], record["review"]["verdict"]) for record in history],
                         [(1, "request_changes"), (2, "approve"), (3, "request_changes"), (4, "approve"),
                          (4, "request_changes"), (5, "approve"), (5, "approve")])
        reference = lambda n, outcome="rejected": {
            "round": n, "attempt": history[n - 1]["attempt"],
            "review_dispatch_id": history[n - 1]["review_source"]["dispatch_id"],
            "snapshot_digest": history[n - 1]["snapshot_digest"],
            "verdict": history[n - 1]["review"]["verdict"], "outcome": outcome}
        first, second = seen["review 4"], seen["review 5"]
        # The second batch runs after the first one's approval was registered, in the same reviewing stage.
        self.assertEqual((first["rows"], first["pending"], first["records"]), (60, 4, 3))
        self.assertEqual((second["rows"], second["pending"], second["records"]), (3, 4, 4))
        hint = {"kind": "task_rework", "task_id": "greet", "work_attempts": 4, "review_rejections": 3,
                "repeated_labels": ["dup"], "repeated_labels_truncated": False,
                "reasons": ["work_attempts", "repeated_labels"], "compared_reviews": [reference(2), reference(3)],
                "latest_review": reference(3), "latest_attempt": 4, "attempts_since_latest_review": 1}
        self.assertEqual(first["hint"], [hint])
        self.assertEqual(second["hint"], first["hint"])
        # The rejecting second batch concludes attempt 4 once; it reported no label, old findings stayed open.
        self.assertEqual(seen["worker 5"]["hint"], [{
            **hint, "work_attempts": 5, "review_rejections": 4, "repeated_labels": [],
            "reasons": ["work_attempts"], "compared_reviews": [], "latest_review": reference(5),
            "latest_attempt": 5}])
        self.assertEqual(status_summary(result)["convergence"]["warnings"], [])


class ReviewRoundProjectionTests(unittest.TestCase):
    def test_damaged_invalidated_and_repeated_records_stay_readable(self):
        run = {"review_history": {
            "t": [{"attempt": 1, "dispatch_id": "d1", "snapshot_digest": "s1",
                   "review": {"verdict": "request_changes", "findings": [finding("A"), finding("A"), finding("B")]}},
                  "garbage",
                  {"status": "invalidated", "attempt": 2, "review_source": {"dispatch_id": "d2"},
                   "snapshot_digest": "s2", "validation_error": "bad"},
                  {"attempt": 3, "review_source": {"dispatch_id": "d3"}, "snapshot_digest": "s3",
                   "review": {"verdict": "request_changes", "findings": [finding("B"), finding("C"), {"id": 3}]}}],
            "u": "not a list", 7: []}}
        rows = review_rounds(run)["tasks"]
        self.assertEqual(list(rows), ["t"])
        self.assertEqual([(r["round"], r["review_dispatch_id"], r["verdict"], r["new_blocking"], r.get("status"))
                          for r in rows["t"]],
                         [(1, "d1", "request_changes", ["A", "B"], None), (2, "d2", None, [], "invalidated"),
                          (3, "d3", "request_changes", ["C"], None)])
        for damaged in ({}, {"review_history": []}, {"review_history": {"t": [{"review": {"findings": "x"}}]}}, None):
            with self.subTest(damaged=damaged):
                review_rounds(damaged)


if __name__ == "__main__":
    unittest.main()
