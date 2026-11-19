import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path

from harness.command_server import create_manifest
from harness.commands import granted
from harness.config import Config
from harness.input_projection import project_commands
from harness.packets import PacketBuilder
from harness.verification_plan import build_verification_plan, candidate_preflight, finding_progress, recent_costs, verification_role_view


def rule5_id(command):
    """The command ID as specification rule 5 defines it, computed here rather than by the runtime."""
    canonical = json.dumps({"argv": command["argv"], "cwd": command["cwd"]},
                           ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


class VerificationPlanTests(unittest.TestCase):
    def test_complete_ordered_gates_and_focused_checks(self):
        commands = [{"cwd": ".", "argv": ["long", "one"]}, {"cwd": ".", "argv": ["long", "two"]}]
        config = {"verification": {"commands": commands, "fresh_checkout": True,
                                   "setup_commands": [{"cwd": ".", "argv": ["setup"]}]}}
        task = {"id": "task", "acceptance": ["behavior"], "self_checks": [{"cwd": ".", "argv": ["focused"]}]}
        plan = build_verification_plan(config, task)
        self.assertEqual([row["command"] for row in plan["checks"] if row["responsibility"] == "task_gate"], commands)
        self.assertEqual([row["command"] for row in plan["checks"] if row["responsibility"] == "final_gate"], commands)
        self.assertEqual(plan["checks"][2]["after"], ["task_gate:0"])
        self.assertFalse(plan["policy"]["worker_checks_replace_gates"])
        self.assertFalse(plan["policy"]["cache_gate_success"])
        self.assertEqual([row["command"] for row in verification_role_view(plan, "worker")["checks"]],
                         [row["command"] for row in plan["checks"]])

    def test_cost_estimates_require_measured_command_provenance(self):
        config = {"verification": {"commands": [{"cwd": ".", "argv": ["test"]}]}}
        context = SimpleNamespace(run={"dispatches": [
            {"id": "old", "role": "verification", "command": {"cwd": ".", "argv": ["test"]}, "duration_seconds": 4,
             "exit_confirmed": True, "evidence_complete": True},
            {"id": "worker", "role": "worker", "command": {"cwd": ".", "argv": ["test"]}, "duration_seconds": 100,
             "exit_confirmed": True, "evidence_complete": True},
            {"id": "recent", "role": "verification", "command": {"cwd": ".", "argv": ["test"]}, "duration_seconds": 2,
             "exit_confirmed": True, "evidence_complete": True,
             "timing_scope": "formal_verification_command_including_cleanup"},
            {"id": "unknown", "role": "verification", "command": {"cwd": ".", "argv": ["test"]}, "duration_seconds": 9,
             "exit_confirmed": False, "evidence_complete": False}]})
        plan = build_verification_plan(config, cost_samples=recent_costs(context))
        estimate = plan["checks"][0]["cost_estimate"]
        self.assertEqual(estimate["seconds"], 2)
        self.assertEqual(estimate["provenance"]["dispatch_id"], "recent")
        self.assertEqual(verification_role_view(plan, "worker")["checks"][0]["cost_estimate"], estimate)
        self.assertFalse(plan["policy"]["cache_gate_success"])
        self.assertIsNone(build_verification_plan(config)["checks"][0]["cost_estimate"]["seconds"])

    def test_worker_input_names_each_command_by_one_reference_and_leaves_grants_exact(self):
        # The gate, the worker grant and a setup command share one argv but not a directory. One occurrence
        # is over the inline limit, so the setup command, listed once (as the final setup check), is a reference too.
        argv = ["python3", "-c", "import pathlib; assert pathlib.Path('pubspec.yaml').is_file()  # " + "\u96ea" * 4100]
        gate, grant, setup = ({"cwd": cwd, "argv": list(argv)} for cwd in (".", "app", "tool"))
        # The focused check is not the required command, so the plan shows which list it took.
        focused = {"cwd": "app", "argv": ["python3", "-m", "pytest", "-q"]}
        data = {"verification": {"commands": [gate], "setup_commands": [setup], "fresh_checkout": True},
                "permissions": {"worker_commands": [grant, focused]}, "runtime": {"verify_timeout_seconds": 30}}
        config_before = deepcopy(data)
        task = {"id": "task", "acceptance": ["tests pass"], "required_commands": [deepcopy(grant)],
                "self_checks": [deepcopy(focused)]}
        context = SimpleNamespace(config=SimpleNamespace(data=data, readonly_commands=Config.readonly_commands))
        # Shaped as the worker packet: the constraint lists are the configuration's own lists.
        packet = {"task": task, "verification_commands": data["verification"]["commands"],
                  "tool_constraints": PacketBuilder(context).tool_constraints(),
                  "verification_plan": verification_role_view(build_verification_plan(data, task), "worker")}
        before = deepcopy(packet)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            (base / "worktree").mkdir()
            # The live dispatch's broker manifest is written before its input is projected.
            manifest = Path(create_manifest(data, base / "worktree", base / "run" / "worker-1")["artifact"])
            manifest_bytes = manifest.read_bytes()
            result = project_commands(packet, base / "run")
            self.assertEqual(manifest.read_bytes(), manifest_bytes)
            catalog = result.pop("exact_command_catalog", None)
            self.assertIsInstance(catalog, dict)
            digest = catalog.get("input_index")
            index = base / "run" / "input-indexes" / f"{digest}.json"
            self.assertEqual(catalog, {"input_index": digest, "count": 3})
            self.assertEqual(result.pop("input_evidence_indexes", None),
                             {digest: {"artifact": str(index), "artifact_digest": digest, "count": 3}})
            raw = index.read_bytes()
            # The broker still lists the exact grants after projection, under the IDs the references name.
            after = create_manifest(data, base / "worktree", base / "run" / "worker-2")
        self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
        self.assertCountEqual(json.loads(raw)["records"], [{"id": rule5_id(c), "command": c} for c in (gate, grant, setup)])
        self.assertEqual(data, config_before)
        self.assertEqual(packet, before)
        gate_ref, grant_ref, setup_ref = ({"exact_command_ref": rule5_id(c)} for c in (gate, grant, setup))
        expected = deepcopy(before)
        expected["verification_commands"] = [gate_ref]
        expected["tool_constraints"].update(verification_commands=[gate_ref], worker_commands=[grant_ref, focused])
        expected["task"].update(required_commands=[grant_ref], self_checks=[focused])
        checks = expected["verification_plan"]["checks"]
        self.assertEqual([c["responsibility"] for c in checks], ["worker_self_check", "task_gate", "final_setup", "final_gate"])
        for check, command in zip(checks, (focused, gate_ref, setup_ref, gate_ref)):
            check["command"] = command
        # Everything else, the read-only prefixes and the command IDs among them, stays as built.
        self.assertEqual(result, expected)
        # The setup command shares the gate's argv yet grants the worker nothing.
        execution = result["tool_constraints"]["command_execution"]
        self.assertEqual((execution["worker_command_ids"], execution["verification_command_ids"]),
                         ([rule5_id(grant), rule5_id(focused)], [rule5_id(gate)]))
        self.assertEqual([granted(c, data) for c in (gate, grant, focused, setup)], [True, True, True, False])
        listed = [{"command_id": rule5_id(c), "command": c} for c in (gate, grant, focused)]
        self.assertEqual(json.loads(manifest_bytes)["commands"], listed)
        self.assertEqual(after["commands"], listed)

    def test_missing_required_evidence_fails_even_without_formal_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            task, worker, args = self.fixture(Path(directory))
            (Path(directory) / "proof.txt").unlink()
            plan = build_verification_plan({"verification": {"commands": []}}, task)
            self.assertEqual(plan["checks"], [])
            report = candidate_preflight(task, worker, **args)
            self.assertFalse(report["passed"])
            self.assertEqual(report["failures"], [{"code": "evidence_invalid", "requirement_id": "proof",
                                                   "path": "proof.txt", "reason": "missing"}])

    def test_progress_absence_and_worker_claim_do_not_close_findings(self):
        run = {"review_history": {"task": [
            {"artifact": "first.json", "dispatch_id": "review-1", "snapshot_digest": "candidate-1", "review": {
                "findings": [{"id": "F1", "severity": "blocking", "message": "gap"}]}},
            {"artifact": "second.json", "dispatch_id": "review-2", "snapshot_digest": "candidate-2", "review": {
                "findings": []}},
        ]}, "feedback": {"worker": {"repair_claims": {"f1": {"summary": "fixed", "evidence_paths": []}}}}}
        row = finding_progress(run, ["task"])["findings"][0]
        self.assertEqual(row["status"], "open")
        later = run["review_history"]["task"][-1]
        later["required_findings"] = [{"key": "f1", "review_dispatch_id": "review-1", "finding_index": 0,
                                       "label": "F1", "artifact": "first.json"}]
        later["review"]["finding_dispositions"] = {"f1": {"status": "resolved", "evidence": ["checked"]}}
        row = finding_progress(run, ["task"])["findings"][0]
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["disposition_source"]["snapshot_digest"], "candidate-2")
        self.assertEqual(finding_progress(run, ["task"], limit=0)["omitted_count"], 1)

    def test_progress_identifies_findings_by_source_and_position_with_repeated_labels(self):
        review = {"dispatch_id": "stale-body-id", "findings": [
            {"id": "bad/id", "severity": "blocking", "message": "first"},
            {"id": "bad/id", "severity": "blocking", "message": "second"}]}
        original = {"artifact": "wrapper.json", "dispatch_id": "stale-ledger-id",
                    "snapshot_digest": "candidate-1", "review": review,
                    "review_source": {"dispatch_id": "review-1", "artifact": "raw.json",
                                      "artifact_digest": "raw-digest", "snapshot_digest": "candidate-1"}}
        run = {"review_history": {"task": [original]}}
        rows = finding_progress(run, ["task"])["findings"]
        self.assertEqual([(r["label"], r["finding_index"]) for r in rows], [("bad/id", 0), ("bad/id", 1)])
        self.assertTrue(all(r["review_dispatch_id"] == "review-1" and r["artifact"] == "raw.json"
                            and r["source_artifact_digest"] == "raw-digest" for r in rows))
        later = {"dispatch_id": "review-2", "artifact": "later.json", "snapshot_digest": "candidate-2",
                 "required_findings": [{"key": "f1", "review_dispatch_id": "review-1", "finding_index": 0,
                                        "label": "bad/id", "artifact": "raw.json"},
                                       {"key": "f2", "review_dispatch_id": "review-1", "finding_index": 1,
                                        "label": "bad/id", "artifact": "raw.json"}],
                 "review": {"findings": [], "finding_dispositions": {
                     "f1": {"status": "open", "evidence": []},
                     "f2": {"status": "resolved", "evidence": ["checked second"]}}}}
        run["review_history"]["task"].append(later)
        for diagnostics in ({"status": "invalid_rejected"}, {"disposition_errors": ["gap"]},
                            {"validation_error": "bad evidence"}, {"status": "invalidated"}):
            later.update(diagnostics)
            self.assertEqual([r["status"] for r in finding_progress(run, ["task"])["findings"]], ["open", "open"])
            for key in diagnostics:
                later.pop(key)
        rows = finding_progress(run, ["task"])["findings"]
        self.assertEqual([r["status"] for r in rows], ["open", "resolved"])
        self.assertEqual(rows[1]["snapshot_digest"], "candidate-1")
        self.assertEqual(rows[1]["disposition_source"]["snapshot_digest"], "candidate-2")
        # A key absent from the record's copy is never projected onto a finding.
        later["required_findings"] = later["required_findings"][:1]
        self.assertEqual([r["status"] for r in finding_progress(run, ["task"])["findings"]], ["open", "open"])
        # This standalone projection does not claim to verify source files.
        del original["review_source"]
        del original["dispatch_id"]
        del review["dispatch_id"]
        self.assertTrue(all(r["status"] == "unknown" for r in finding_progress(run, ["task"])["findings"][:2]))

    def fixture(self, root):
        data = b"host evidence"
        (root / "proof.txt").write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        task = {"id": "task", "acceptance": ["host behavior"], "evidence_requirements": [
            {"id": "proof", "acceptance_index": 0, "kind": "host_integration", "path": "proof.txt"}]}
        # The worker result declares no evidence; the runtime owns path, kind and digest.
        worker = {"task_id": "task", "dispatch_id": "worker-1", "status": "completed"}
        source = root / "worker.json"
        source.write_text(json.dumps(worker), encoding="utf-8")
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
            self.assertEqual(report["evidence"], [{"requirement_id": "proof", "path": "proof.txt",
                "sha256": hashlib.sha256(b"host evidence").hexdigest(), "kind": "host_integration",
                "acceptance_index": 0}])
            self.assertEqual(report["semantic_coverage"], "requires independent review")

    def test_missing_mismatched_changed_and_unbound_evidence_fail(self):
        for scenario, reason in (("missing", "missing"), ("hash", "outside_candidate"), ("ignored", "outside_candidate"),
                                 ("symlink", "symlink"), ("parent_symlink", "symlink"),
                                 ("directory", "not_regular_file"), ("unsafe", "unsafe_path"),
                                 ("candidate", None), ("source", None)):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                task, worker, args = self.fixture(root)
                requirement = task["evidence_requirements"][0]
                if scenario == "missing":
                    (root / "proof.txt").unlink()
                elif scenario == "hash":
                    (root / "proof.txt").write_text("changed after the snapshot", encoding="utf-8")
                elif scenario == "candidate":
                    args["current_snapshot"] = {**args["current_snapshot"], "head": "other"}
                elif scenario == "source":
                    args["worker_artifact"].write_text("{}", encoding="utf-8")
                elif scenario == "ignored":
                    args["current_snapshot"]["files"] = {}
                elif scenario == "symlink":
                    (root / "proof.txt").rename(root / "other.txt")
                    (root / "proof.txt").symlink_to("other.txt")
                elif scenario == "parent_symlink":
                    (root / "real").mkdir()
                    (root / "proof.txt").rename(root / "real" / "proof.txt")
                    (root / "linked").symlink_to("real", target_is_directory=True)
                    requirement["path"] = "linked/proof.txt"
                elif scenario == "directory":
                    (root / "proof.txt").unlink()
                    (root / "proof.txt").mkdir()
                elif scenario == "unsafe":
                    requirement["path"] = "../proof.txt"
                report = candidate_preflight(task, worker, **args)
                self.assertFalse(report["passed"])
                if reason:
                    self.assertEqual([(f["code"], f["reason"]) for f in report["failures"]],
                                     [("evidence_invalid", reason)])

    def test_no_requirements_needs_no_evidence_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            task, worker, args = self.fixture(Path(directory))
            task["evidence_requirements"] = []
            report = candidate_preflight(task, worker, **args)
            self.assertTrue(report["passed"])
            self.assertEqual(report["evidence"], [])

if __name__ == "__main__":
    unittest.main()
