"""Worker repair claims follow runtime-keyed delivered obligations; closure stays candidate-bound."""
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from harness.review_progress import claim_gaps, delivered_worker_findings, required_findings
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT
from test_runtime_changes import finding, finding_key


def labels(rows):
    return [row["label"] for row in rows]


class WorkerRepairObligationsTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples/plan.json")

    def scenario(self, *, new_finding=True, unchanged=False, reopen_old=False):
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
            elif kwargs["kind"] == "review":
                if count == 1:
                    result.update(verdict="request_changes", findings=[finding()])
                elif count == 2:
                    result.update(verdict="request_changes", findings=[
                        {**finding(), "id": "second-defect"}] if new_finding else [])
                elif reopen_old:
                    # The reopened finding stays open even though the worker made no claim for it.
                    result["finding_dispositions"][finding_key(packet, "edge-case")]["status"] = "open"

        backend = RecordingBackend(change)
        return backend, schemas

    def assert_completed(self, engine, result, backend):
        self.assertEqual(self.config.data["runtime"]["max_attempts"], 5)
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
        self.assertEqual(labels(workers[2]["packet"]["required_findings"]), ["second-defect"])
        review = [call for call in backend.calls if call["kind"] == "review"][-1]
        self.assertEqual(labels(review["packet"]["required_findings"]), ["edge-case", "second-defect"])
        self.assertEqual([row["key"] for row in review["packet"]["required_findings"]], ["f1", "f2"])
        record = result["review_history"]["greet"][-1]
        self.assertEqual(set(record["review"]["finding_dispositions"]), {"f1", "f2"})
        # The worker claimed only its delivered finding; the review sees it under its own key.
        self.assertEqual(set(review["packet"]["repair_claims"]), {"f2"})

    def test_all_resolved_rejection_omits_claim_schema_but_new_candidate_reopens_for_reviewer(self):
        backend, schemas = self.scenario(new_finding=False)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assert_completed(engine, result, backend)
        self.assertEqual(schemas, [False, True, False])
        review = [call for call in backend.calls if call["kind"] == "review"][-1]
        self.assertEqual(labels(review["packet"]["required_findings"]), ["edge-case"])
        self.assertEqual(review["packet"]["repair_claims"], {})

    def test_unchanged_candidate_retains_existing_independent_resolution(self):
        backend, _ = self.scenario(unchanged=True)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assert_completed(engine, result, backend)
        review = [call for call in backend.calls if call["kind"] == "review"][-1]
        self.assertEqual(labels(review["packet"]["required_findings"]), ["second-defect"])

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
        delivered = delivered_worker_findings(engine.context, "greet", entry["id"])
        self.assertEqual([row["key"] for row in delivered], [f"f{n}" for n in range(1, 13)])
        self.assertEqual([row["finding_index"] for row in delivered], list(range(12)))
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
        self.assertEqual(labels(required_findings(result, "greet", worker["snapshot_digest"])),
                         ["edge-case", "second-defect"])
        packet = read_json(Path(worker["worker_receipt"]["packet_artifact"]))
        self.assertEqual(labels(packet["required_findings"]), ["second-defect"])

    def test_worker_claim_scope_cannot_remove_reviewer_obligation(self):
        backend, _ = self.scenario(reopen_old=True)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "尝试次数"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["accepted"], {})
        record = engine.run["review_history"]["greet"][-1]
        errors = record["disposition_errors"]
        self.assertEqual(errors[0]["code"], "finding_not_independently_resolved")
        row = next(item for item in record["required_findings"] if item["key"] == errors[0]["key"])
        self.assertEqual((row["label"], row["finding_index"]), ("edge-case", 0))

    def test_external_citations_never_become_candidate_repair_paths(self):
        delivered = [{"key": "f1", "review_dispatch_id": "review", "finding_index": 0}]
        result = {"evidence": ["/outside/original.log"], "repair_claims": {"f1": {
            "summary": "Changed the candidate and retained the original log", "evidence_paths": ["hello.py"]}}}
        snapshot = {"files": {"hello.py": "digest"}}
        self.assertEqual(claim_gaps(result, delivered, snapshot), [])
        result["repair_claims"]["f1"]["evidence_paths"] = ["/outside/original.log"]
        self.assertEqual(claim_gaps(result, delivered, snapshot),
                         [{"code": "repair_evidence_outside_candidate", "key": "f1", "path": "/outside/original.log"}])
        del result["repair_claims"]
        self.assertEqual(claim_gaps(result, delivered, snapshot),
                         [{"code": "repair_claim_missing", "key": "f1", "review_dispatch_id": "review",
                           "finding_index": 0}])

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
        worker = [row for row in engine.run["dispatches"] if row["role"] == "worker"][-1]
        packet_path = Path(worker["worker_receipt"]["packet_artifact"])
        before = packet_path.read_bytes()
        delivered = delivered_worker_findings(engine.context, "greet", worker["id"])
        resumed = self.engine(backend)
        result = resumed.resume(engine.run["id"])
        self.assert_completed(resumed, result, backend)
        # The same published packet maps the worker's claims after the interruption.
        self.assertEqual(packet_path.read_bytes(), before)
        self.assertEqual(delivered_worker_findings(resumed.context, "greet", worker["id"]), delivered)
        review = [call for call in backend.calls if call["kind"] == "review"][-1]
        self.assertEqual(review["packet"]["repair_claims"],
                         {finding_key(review["packet"], "second-defect"): read_json(
                             Path(worker["worker_receipt"]["artifact"]))["repair_claims"]["f1"]})

    def test_missing_tampered_or_rebound_delivered_evidence_blocks_recovery(self):
        engine, backend = self.interrupted_preflight()
        state_path = engine.directory / "state.json"
        saved = read_json(state_path)
        worker = next(row for row in reversed(saved["dispatches"]) if row["role"] == "worker")
        intent = copy.deepcopy(worker["worker_receipt"])
        packet_path, schema_path = Path(intent["packet_artifact"]), Path(intent["schema_artifact"])
        packet_bytes, schema_bytes = packet_path.read_bytes(), schema_path.read_bytes()
        source_artifact = Path(saved["review_history"]["greet"][-1]["review_source"]["artifact"])
        source_bytes = source_artifact.read_bytes()
        calls = len(backend.calls)
        for mode in ("missing_artifact", "bytes", "symlink", "index", "label", "source_bytes",
                     "capacity", "schema_keys"):
            with self.subTest(mode=mode):
                state = read_json(state_path)
                entry = next(row for row in state["dispatches"] if row["id"] == worker["id"])
                entry["worker_receipt"] = copy.deepcopy(intent)
                if packet_path.is_symlink():
                    packet_path.unlink()
                packet_path.write_bytes(packet_bytes)
                schema_path.write_bytes(schema_bytes)
                source_artifact.write_bytes(source_bytes)
                packet, schema = read_json(packet_path), read_json(schema_path)
                if mode == "missing_artifact":
                    packet_path.unlink()
                elif mode == "bytes":
                    packet_path.write_bytes(packet_bytes + b" ")
                elif mode == "source_bytes":
                    source_artifact.write_bytes(source_bytes + b" ")
                elif mode == "symlink":
                    other = packet_path.with_name("other-packet.json")
                    other.write_bytes(packet_bytes)
                    packet_path.unlink()
                    packet_path.symlink_to(other)
                else:
                    # Rebind consistent digests: only the delivered identity check can object.
                    if mode == "index":
                        packet["required_findings"][0]["finding_index"] = 5
                    elif mode == "label":
                        packet["required_findings"][0]["label"] = "invented"
                    elif mode == "capacity":
                        packet["required_findings"] *= 61
                    else:
                        schema["properties"]["repair_claims"]["required"] = ["f2"]
                    write_json(packet_path, packet)
                    write_json(schema_path, schema)
                    for field, path in (("packet", packet_path), ("schema", schema_path)):
                        entry["worker_receipt"][field + "_artifact_digest"] = hashlib.sha256(path.read_bytes()).hexdigest()
                write_json(state_path, state)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                self.assertEqual(len(backend.calls), calls)
                self.assertEqual(read_json(state_path)["accepted"], {})
