"""Draft scaffolding preserves source identities without granting acceptance."""
import copy
import hashlib
from pathlib import Path

from harness.review_progress import claim_gaps, delivered_worker_findings
from harness.util import read_json
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT
from test_runtime_changes import finding


class RepairClaimTemplateTests(RepoCase):
    def test_bound_template_is_retrievable_before_invoke_and_covers_every_source(self):
        inspected = []

        def inspect(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and sum(c["kind"] == "review" for c in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[
                    {**finding(), "id": f"issue-{index}"} for index in range(12)])
            if kwargs["role"] != "worker":
                return
            reference = packet.get("repair_claims_template")
            if not packet.get("required_findings"):
                self.assertIsNone(reference)
                return
            raw = Path(reference["artifact"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), reference["artifact_digest"])
            saved = read_json(Path(reference["artifact"]))
            self.assertEqual(saved["dispatch_id"], result["dispatch_id"])
            self.assertEqual(saved["task_id"], result["task_id"])
            draft = saved[reference["field"]]
            self.assertEqual(reference["required_count"], 12)
            self.assertEqual([(c["review_dispatch_id"], c["finding_id"]) for c in draft],
                             [(c["review_dispatch_id"], c["finding_id"]) for c in packet["required_findings"]])
            self.assertTrue(all(c["summary"] == "" and c["evidence_paths"] == [] for c in draft))
            self.assertNotIn("claims", reference)  # The full skeleton stays outside the prompt.
            inspected.append((result["dispatch_id"], copy.deepcopy(reference)))

        backend = RecordingBackend(inspect)
        engine = self.engine(backend)
        result = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(inspected), 1)
        dispatch_id, reference = inspected[0]
        entry = next(d for d in result["dispatches"] if d["id"] == dispatch_id)
        self.assertEqual(entry["worker_findings"]["artifact_digest"], reference["artifact_digest"])
        self.assertEqual(len(delivered_worker_findings(engine.context, "greet", dispatch_id)), 12)
        self.assertEqual(len(result["review_history"]["greet"][-1]["review"]["finding_dispositions"]), 12)

    def test_external_citations_never_become_candidate_repair_paths(self):
        required = [{"review_dispatch_id": "review", "finding_id": "issue"}]
        result = {"evidence": ["/outside/original.log"], "repair_claims": [{
            **required[0], "summary": "Changed the candidate and retained the original log",
            "evidence_paths": ["hello.py"]}]}
        snapshot = {"files": {"hello.py": "digest"}}
        self.assertEqual(claim_gaps(result, required, snapshot), [])
        result["repair_claims"][0]["evidence_paths"] = ["/outside/original.log"]
        self.assertEqual(claim_gaps(result, required, snapshot)[0]["code"], "repair_evidence_outside_candidate")
        result["repair_claims"] = []
        self.assertEqual(claim_gaps(result, required, snapshot)[0]["code"], "repair_claim_missing")
