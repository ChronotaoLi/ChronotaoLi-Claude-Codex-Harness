import hashlib
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from harness.verification_plan import build_verification_plan, candidate_preflight, finding_progress, recent_costs, verification_role_view


class VerificationPlanTests(unittest.TestCase):
    def test_complete_ordered_gates_and_focused_checks(self):
        commands = [["long", "one"], ["long", "two"]]
        config = {"verification": {"commands": commands, "fresh_checkout": True,
                                   "setup_commands": [["setup"]]}}
        task = {"id": "task", "acceptance": ["behavior"], "self_checks": [["focused"]]}
        plan = build_verification_plan(config, task)
        self.assertEqual([row["argv"] for row in plan["checks"] if row["responsibility"] == "task_gate"], commands)
        self.assertEqual([row["argv"] for row in plan["checks"] if row["responsibility"] == "final_gate"], commands)
        self.assertEqual(plan["checks"][2]["after"], ["task_gate:0"])
        self.assertFalse(plan["policy"]["worker_checks_replace_gates"])
        self.assertFalse(plan["policy"]["cache_gate_success"])
        self.assertEqual([row["argv"] for row in verification_role_view(plan, "worker")["checks"]],
                         [row["argv"] for row in plan["checks"]])

    def test_cost_estimates_require_measured_command_provenance(self):
        config = {"verification": {"commands": [["test"]]}}
        context = SimpleNamespace(run={"dispatches": [
            {"id": "old", "role": "verification", "argv": ["test"], "duration_seconds": 4,
             "exit_confirmed": True, "evidence_complete": True},
            {"id": "worker", "role": "worker", "argv": ["test"], "duration_seconds": 100,
             "exit_confirmed": True, "evidence_complete": True},
            {"id": "recent", "role": "verification", "argv": ["test"], "duration_seconds": 2,
             "exit_confirmed": True, "evidence_complete": True,
             "timing_scope": "formal_verification_command_including_cleanup"},
            {"id": "unknown", "role": "verification", "argv": ["test"], "duration_seconds": 9,
             "exit_confirmed": False, "evidence_complete": False}]})
        plan = build_verification_plan(config, cost_samples=recent_costs(context))
        estimate = plan["checks"][0]["cost_estimate"]
        self.assertEqual(estimate["seconds"], 2)
        self.assertEqual(estimate["provenance"]["dispatch_id"], "recent")
        self.assertEqual(verification_role_view(plan, "worker")["checks"][0]["cost_estimate"], estimate)
        self.assertFalse(plan["policy"]["cache_gate_success"])
        self.assertIsNone(build_verification_plan(config)["checks"][0]["cost_estimate"]["seconds"])

    def test_missing_required_evidence_fails_even_without_formal_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            task, worker, args = self.fixture(Path(directory))
            worker["evidence_files"] = []
            args["worker_artifact"].write_text(json.dumps(worker))
            args["worker_artifact_digest"] = hashlib.sha256(args["worker_artifact"].read_bytes()).hexdigest()
            plan = build_verification_plan({"verification": {"commands": []}}, task)
            self.assertEqual(plan["checks"], [])
            report = candidate_preflight(task, worker, **args)
            self.assertFalse(report["passed"])
            self.assertEqual(report["failures"][0]["code"], "evidence_reference_missing_or_duplicate")

    def test_progress_absence_and_worker_claim_do_not_close_findings(self):
        run = {"review_history": {"task": [
            {"artifact": "first.json", "snapshot_digest": "candidate-1", "review": {
                "dispatch_id": "review-1", "findings": [{"id": "F1", "severity": "blocking", "message": "gap"}]}},
            {"artifact": "second.json", "snapshot_digest": "candidate-2", "review": {
                "dispatch_id": "review-2", "findings": []}},
        ]}, "feedback": {"worker": {"repair_claims": [{"finding_id": "F1"}]}}}
        row = finding_progress(run, "task")["findings"][0]
        self.assertEqual(row["status"], "open")
        run["review_history"]["task"][-1]["review"]["finding_dispositions"] = [
            {"review_dispatch_id": "review-1", "finding_id": "F1", "status": "resolved", "evidence": ["checked"]}]
        row = finding_progress(run, "task")["findings"][0]
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["disposition_source"]["snapshot_digest"], "candidate-2")
        self.assertEqual(finding_progress(run, "task", limit=0)["omitted_count"], 1)

    def test_progress_uses_shared_aliases_and_authenticated_source_precedence(self):
        from harness.review_progress import finding_references
        review = {"dispatch_id": "stale-body-id", "findings": [
            {"id": "bad/id", "severity": "blocking", "message": "first"},
            {"id": "bad/id", "severity": "blocking", "message": "second"}]}
        original = {"artifact": "wrapper.json", "dispatch_id": "stale-ledger-id",
                    "snapshot_digest": "candidate-1", "review": review,
                    "review_source": {"dispatch_id": "review-1", "artifact": "raw.json",
                                      "artifact_digest": "raw-digest", "snapshot_digest": "candidate-1"}}
        run = {"review_history": {"task": [original]}}
        rows = finding_progress(run, "task")["findings"]
        self.assertEqual([r["finding_id"] for r in rows],
                         [r["finding_id"] for _, r in finding_references(review)])
        self.assertEqual([r["original_finding_id"] for r in rows], ["bad/id", "bad/id"])
        self.assertEqual([r["finding_index"] for r in rows], [0, 1])
        self.assertTrue(all(r["review_dispatch_id"] == "review-1" and r["artifact"] == "raw.json"
                            and r["source_artifact_digest"] == "raw-digest" for r in rows))
        disposition = {"review_dispatch_id": "review-1", "finding_id": "finding-2",
                       "status": "resolved", "evidence": ["checked second"]}
        later = {"dispatch_id": "review-2", "artifact": "later.json", "snapshot_digest": "candidate-2",
                 "review": {"findings": [], "finding_dispositions": [disposition]}}
        run["review_history"]["task"].append(later)
        for diagnostics in ({"status": "invalid_rejected"}, {"disposition_errors": ["gap"]},
                            {"validation_error": "bad evidence"}, {"status": "invalidated"}):
            later.update(diagnostics)
            self.assertEqual([r["status"] for r in finding_progress(run, "task")["findings"]], ["open", "open"])
            for key in diagnostics:
                later.pop(key)
        rows = finding_progress(run, "task")["findings"]
        self.assertEqual([r["status"] for r in rows], ["open", "resolved"])
        self.assertEqual(rows[1]["snapshot_digest"], "candidate-1")
        self.assertEqual(rows[1]["disposition_source"]["snapshot_digest"], "candidate-2")
        # This standalone projection does not claim to verify source files.
        del original["review_source"]
        del original["dispatch_id"]
        del review["dispatch_id"]
        self.assertTrue(all(r["status"] == "unknown" for r in finding_progress(run, "task")["findings"]))

    def fixture(self, root):
        data = b"host evidence"
        (root / "proof.txt").write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        task = {"id": "task", "acceptance": ["host behavior"], "evidence_requirements": [
            {"id": "proof", "acceptance_index": 0, "kind": "host_integration", "path": "proof.txt"}]}
        worker = {"task_id": "task", "dispatch_id": "worker-1", "evidence_files": [
            {"requirement_id": "proof", "path": "proof.txt", "kind": "host_integration", "sha256": sha}]}
        source = root / "worker.json"
        source.write_text(json.dumps(worker))
        snapshot = {"head": "head", "index": "index", "files": {"proof.txt": "644:" + sha}}
        args = dict(worktree=root, candidate_snapshot=snapshot, current_snapshot=snapshot,
                    worker_artifact=source, worker_artifact_digest=hashlib.sha256(source.read_bytes()).hexdigest(),
                    worker_dispatch_id="worker-1")
        return task, worker, args

    def test_bound_evidence_is_only_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            task, worker, args = self.fixture(Path(directory))
            report = candidate_preflight(task, worker, **args)
            self.assertTrue(report["passed"])
            self.assertEqual(report["semantic_coverage"], "requires independent review")

    def test_missing_mismatched_changed_and_unbound_evidence_fail(self):
        for scenario in ("missing", "hash", "candidate", "source", "ignored", "symlink"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                task, worker, args = self.fixture(root)
                if scenario == "missing":
                    worker["evidence_files"] = []
                elif scenario == "hash":
                    (root / "proof.txt").write_text("changed")
                elif scenario == "candidate":
                    args["current_snapshot"] = {**args["current_snapshot"], "head": "other"}
                elif scenario == "source":
                    args["worker_artifact"].write_text("{}")
                elif scenario == "ignored":
                    args["current_snapshot"]["files"] = {}
                elif scenario == "symlink":
                    (root / "proof.txt").rename(root / "other.txt")
                    (root / "proof.txt").symlink_to("other.txt")
                self.assertFalse(candidate_preflight(task, worker, **args)["passed"])

    def test_no_requirements_needs_no_evidence_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            task, worker, args = self.fixture(Path(directory))
            task["evidence_requirements"] = []
            self.assertTrue(candidate_preflight(task, worker, **args)["passed"])


if __name__ == "__main__":
    unittest.main()
