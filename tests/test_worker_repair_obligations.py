"""Worker declarations follow delivered obligations; closure stays candidate-bound."""
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from harness.review_progress import required_findings
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT
from test_runtime_changes import finding


class WorkerRepairObligationsTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples/plan.json")

    def scenario(self, *, new_finding=True, unchanged=False, voluntary=False,
                 omit_old_disposition=False):
        schemas = []
        candidates = {}

        def change(backend, kwargs, packet, result):
            count = sum(call["role"] == "worker" for call in backend.calls)
            if kwargs["role"] == "worker":
                schemas.append("repair_claims" in kwargs["schema"]["properties"])
                path = kwargs["cwd"] / "hello.py"
                value = path.read_text() + f"# candidate {count}\n"
                if unchanged and count == 3:
                    value = candidates[2]
                path.write_text(value)
                candidates[count] = value
                if voluntary and count == 3:
                    original = next(call for call in backend.calls if call["kind"] == "review")
                    result.setdefault("repair_claims", []).append({
                        "review_dispatch_id": original["packet"]["_source"],
                        "finding_id": "edge-case", "summary": "Rechecked the earlier correction",
                        "evidence_paths": ["hello.py"]})
            elif kwargs["kind"] == "review":
                # Private test bookkeeping never goes to the model or ledger.
                backend.calls[-1]["packet"]["_source"] = result["dispatch_id"]
                if count == 1:
                    result.update(verdict="request_changes", findings=[finding()])
                elif count == 2:
                    result.update(verdict="request_changes", findings=[
                        {**finding(), "id": "second-defect"}] if new_finding else [])
                elif omit_old_disposition:
                    result["finding_dispositions"] = [row for row in result.get("finding_dispositions", [])
                                                       if row["finding_id"] != "edge-case"]

        backend = RecordingBackend(change)
        return backend, schemas

    def assert_completed(self, engine, result, backend):
        self.assertEqual(self.config.data["runtime"]["max_attempts"], 3)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 3})
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 3)
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        self.assertFalse(any(row["event"] == "candidate_preflight_failed" for row in events))

    def test_partial_resolution_and_new_finding_complete_with_default_budget(self):
        backend, _ = self.scenario()
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assert_completed(engine, result, backend)
        workers = [call for call in backend.calls if call["role"] == "worker"]
        self.assertEqual([row["finding_id"] for row in workers[2]["packet"]["required_findings"]],
                         ["second-defect"])
        review = [call for call in backend.calls if call["kind"] == "review"][-1]
        self.assertEqual([row["finding_id"] for row in review["packet"]["required_findings"]],
                         ["edge-case", "second-defect"])
        self.assertEqual(len(result["review_history"]["greet"][-1]["review"]["finding_dispositions"]), 2)

    def test_all_resolved_rejection_omits_claim_schema_but_new_candidate_reopens_for_reviewer(self):
        backend, schemas = self.scenario(new_finding=False)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assert_completed(engine, result, backend)
        self.assertEqual(schemas, [False, True, False])
        review = [call for call in backend.calls if call["kind"] == "review"][-1]
        self.assertEqual([row["finding_id"] for row in review["packet"]["required_findings"]], ["edge-case"])

    def test_unchanged_candidate_retains_existing_independent_resolution(self):
        backend, _ = self.scenario(unchanged=True)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assert_completed(engine, result, backend)
        review = [call for call in backend.calls if call["kind"] == "review"][-1]
        self.assertEqual([row["finding_id"] for row in review["packet"]["required_findings"]], ["second-defect"])

    def test_voluntary_claim_can_reference_reopened_current_candidate_finding(self):
        backend, _ = self.scenario(voluntary=True)
        engine = self.engine(backend)
        self.assert_completed(engine, engine.start("Implement greet", self.plan()), backend)

    def test_compacted_delivered_source_table_expands_to_authenticated_originals(self):
        def reject_once(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and sum(call["kind"] == "review" for call in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[
                    {**finding(), "id": f"issue-{index}"} for index in range(12)])

        backend = RecordingBackend(reject_once)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        worker = [call for call in backend.calls if call["role"] == "worker"][-1]
        self.assertIn("required_finding_sources", worker["packet"])
        self.assertTrue(all("artifact" not in row for row in worker["packet"]["required_findings"]))
        entry = [row for row in result["dispatches"] if row["role"] == "worker"][-1]
        delivered = read_json(Path(entry["worker_findings"]["artifact"]))["required_findings"]
        self.assertEqual(len(delivered), 12)
        self.assertTrue(all(row["artifact"] == result["review_history"]["greet"][0]["review_source"]["artifact"]
                            for row in delivered))

    def test_pre_agent_candidate_change_preserves_exact_delivered_requirements(self):
        backend, _ = self.scenario()
        engine = self.engine(backend)

        def mutate(event, payload, logs):
            if (event == "pre_agent" and payload["role"] == "worker"
                    and sum(call["role"] == "worker" for call in backend.calls) == 2):
                with (engine.worktree / "hello.py").open("a") as stream:
                    stream.write("# hook changed the candidate after packet construction\n")

        with patch.object(engine.hooks, "emit", side_effect=mutate):
            result = engine.start("Implement greet", self.plan())
        self.assert_completed(engine, result, backend)
        worker = [row for row in result["dispatches"] if row["role"] == "worker"][-1]
        self.assertEqual([row["finding_id"] for row in required_findings(result, "greet", worker["snapshot_digest"])],
                         ["edge-case", "second-defect"])
        rows = read_json(Path(worker["worker_findings"]["artifact"]))["required_findings"]
        self.assertEqual([row["finding_id"] for row in rows], ["second-defect"])

    def test_worker_claim_scope_cannot_remove_reviewer_obligation(self):
        backend, _ = self.scenario(omit_old_disposition=True)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "尝试次数"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["accepted"], {})
        errors = engine.run["review_history"]["greet"][-1]["disposition_errors"]
        self.assertEqual(errors[0]["code"], "finding_not_independently_resolved")
        self.assertEqual(errors[0]["reference"][1], "edge-case")

    def interrupted_preflight(self):
        backend, _ = self.scenario()
        engine = self.engine(backend)
        original = engine.services.tasks.continue_task

        def interrupt():
            if engine.run["attempts"]["greet"] == 3:
                raise KeyboardInterrupt("saved third candidate before preflight")
            return original()

        with patch.object(engine.services.tasks, "continue_task", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["pending_task"]["stage"], "verifying")
        return engine, backend

    def test_recovery_reuses_saved_delivered_list_without_extra_worker(self):
        engine, backend = self.interrupted_preflight()
        resumed = self.engine(backend)
        result = resumed.resume(engine.run["id"])
        self.assert_completed(resumed, result, backend)

    def test_missing_tampered_or_rebound_delivered_evidence_blocks_recovery(self):
        engine, backend = self.interrupted_preflight()
        state_path = engine.directory / "state.json"
        saved = read_json(state_path)
        worker = next(row for row in reversed(saved["dispatches"]) if row["role"] == "worker")
        binding = copy.deepcopy(worker["worker_findings"])
        artifact = Path(binding["artifact"])
        original = artifact.read_bytes()
        source_artifact = Path(read_json(artifact)["required_findings"][0]["artifact"])
        source_bytes = source_artifact.read_bytes()
        calls = len(backend.calls)
        for mode in ("missing_binding", "missing_artifact", "bytes", "task", "dispatch",
                     "source_identity", "source_bytes", "capacity", "symlink"):
            with self.subTest(mode=mode):
                state = read_json(state_path)
                entry = next(row for row in state["dispatches"] if row["id"] == worker["id"])
                entry["worker_findings"] = copy.deepcopy(binding)
                if artifact.is_symlink():
                    artifact.unlink()
                artifact.write_bytes(original)
                source_artifact.write_bytes(source_bytes)
                value = read_json(artifact)
                if mode == "missing_binding":
                    del entry["worker_findings"]
                elif mode == "missing_artifact":
                    artifact.unlink()
                elif mode == "bytes":
                    artifact.write_bytes(original + b" ")
                elif mode == "source_bytes":
                    source_artifact.write_bytes(source_bytes + b" ")
                elif mode == "symlink":
                    other = artifact.with_name("other-findings.json")
                    other.write_bytes(original)
                    artifact.unlink()
                    artifact.symlink_to(other)
                else:
                    if mode in ("task", "dispatch"):
                        value[{"task": "task_id", "dispatch": "dispatch_id"}[mode]] = "wrong"
                    elif mode == "source_identity":
                        value["required_findings"][0]["finding_id"] = "invented"
                    else:
                        value["required_findings"] *= 61
                    write_json(artifact, value)
                    entry["worker_findings"]["artifact_digest"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
                write_json(state_path, state)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                self.assertEqual(len(backend.calls), calls)
                self.assertEqual(read_json(state_path)["accepted"], {})
