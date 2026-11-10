"""Read-only metric: blocking finding IDs first reported in each task review round."""
import contextlib
import io
import json
import unittest

from harness import cli, observe
from harness.cli import status_summary
from harness.convergence import review_rounds
from harness.util import read_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend


def finding(ident):
    return {"id": ident, "severity": "blocking", "category": "defect", "location": "hello.py:1",
            "message": f"Defect {ident}", "evidence": ["test_hello.py requires the greeting"]}


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
                    self.assertEqual([row["finding_id"] for row in result["finding_dispositions"]], ["OLD-001"])
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
