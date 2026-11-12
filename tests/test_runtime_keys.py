"""Runtime-keyed required findings: models never write identities the runtime owns.

Each dispatch receives keys f1..fN for its required findings; dispositions and
repair claims are fixed-key objects; the key-to-finding mapping is saved before
the dispatch and closes only the finding it named.
"""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from harness.adapters import provider_schema
from harness.contracts import (DECIDE, PLAN, REVIEW, WORK, envelope, output_constraints,
                               review_schema, validate_structure, with_keys)
from harness.review_progress import claims_for_review, delivered_worker_findings
from harness.util import HarnessError, read_json, write_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend, finding, finding_key, revised, trigger_kind


def rows(count):
    return [{"key": f"f{number}"} for number in range(1, count + 1)]


def labelled(packet):
    return [(row["key"], row["finding_index"], row["label"]) for row in packet.get("required_findings", [])]


def complete_rows(packet):
    """A dispatched packet's required rows with each source artifact restored.

    The packet may factor artifacts into required_finding_sources; comparing
    complete rows also covers a mutated source path.
    """
    sources = packet.get("required_finding_sources", {})
    return [{**row, "artifact": row["artifact"] if "artifact" in row
             else sources[row["review_dispatch_id"]]["artifact"]}
            for row in packet.get("required_findings", [])]


class KeyedContractTests(unittest.TestCase):
    def test_keyed_fields_exist_only_for_required_findings_and_require_every_key(self):
        self.assertNotIn("finding_dispositions", review_schema([])["properties"])
        self.assertNotIn("repair_claims", with_keys(WORK, "repair_claims", [])["properties"])
        for schema, field in ((review_schema(rows(3)), "finding_dispositions"),
                              (with_keys(WORK, "repair_claims", rows(3)), "repair_claims")):
            keyed = schema["properties"][field]
            self.assertEqual(keyed["required"], ["f1", "f2", "f3"])
            self.assertIs(keyed["additionalProperties"], False)
            # A review must dispose every required finding; repair claims stay optional.
            self.assertEqual(field in schema["required"], field == "finding_dispositions")
            for strict in (False, True):
                wire = provider_schema(envelope(schema, "t", "d"), strict)["properties"][field]
                self.assertEqual((wire["required"], wire["additionalProperties"]), (["f1", "f2", "f3"], False))
        self.assertNotIn("finding_dispositions", REVIEW["properties"])
        self.assertFalse({"repair_claims", "evidence_files"} & set(WORK["properties"]))

    def test_identical_keyed_items_share_one_constraint_path(self):
        def paths(count):
            return [item["path"] for item in output_constraints(envelope(review_schema(rows(count)), "t", "d"))]
        self.assertIn("response.finding_dispositions.f1.evidence", paths(1))
        self.assertIn("response.finding_dispositions.*.evidence", paths(3))
        self.assertFalse(any(".f2" in path for path in paths(3)))
        self.assertEqual(len(paths(3)), len(paths(60)))
        for schema in (PLAN, DECIDE, WORK, REVIEW, envelope(WORK, "t", "d")):
            self.assertFalse(any(".*" in item["path"] for item in output_constraints(schema)))

    def test_extra_missing_or_misshapen_keys_are_structural_errors(self):
        base = {"task_id": "t", "dispatch_id": "d", "verdict": "approve", "summary": "ok", "findings": [],
                "approved_memory_keys": []}
        good = {"status": "resolved", "evidence": ["checked"]}
        cases = {"extra key": {"f1": good, "f2": good, "f3": good},
                 "missing key": {"f1": good},
                 "extra value field": {"f1": good, "f2": {**good, "review_dispatch_id": "r"}},
                 "missing value field": {"f1": good, "f2": {"status": "open"}}}
        schema = envelope(review_schema(rows(2)), "t", "d")
        validate_structure({**base, "finding_dispositions": {"f1": good, "f2": good}}, schema)
        for label, value in cases.items():
            with self.subTest(label=label), self.assertRaises(HarnessError):
                validate_structure({**base, "finding_dispositions": value}, schema)
        with self.assertRaises(HarnessError):
            validate_structure(base, schema)
        with self.assertRaises(HarnessError):
            validate_structure({**base, "finding_dispositions": {"f1": good}}, envelope(review_schema([]), "t", "d"))
        work = {"task_id": "t", "dispatch_id": "d", "status": "completed", "summary": "s", "evidence": [],
                "blockers": [], "blocker_kind": "none", "proposed_memories": []}
        claim = {"summary": "fixed", "evidence_paths": ["hello.py"]}
        contract = envelope(with_keys(WORK, "repair_claims", rows(1)), "t", "d")
        validate_structure({**work, "repair_claims": {"f1": claim}}, contract)
        for value in ({"f1": claim, "f2": claim}, {}, {"f1": {"summary": "fixed"}}):
            with self.subTest(claims=value), self.assertRaises(HarnessError):
                validate_structure({**work, "repair_claims": value}, contract)

    def test_worker_claims_are_rekeyed_onto_the_reviews_keys_by_source_and_position(self):
        def row(key, index):
            return {"key": key, "review_dispatch_id": "R1", "finding_index": index}
        delivered = [row("f1", 0), row("f2", 1)]  # A, B
        required = [row("f1", 2), row("f2", 0), row("f3", 1)]  # C, A, B
        claims = {"f1": {"summary": "A", "evidence_paths": []}, "f2": {"summary": "B", "evidence_paths": []}}
        self.assertEqual(claims_for_review(required, delivered, claims), {"f2": claims["f1"], "f3": claims["f2"]})
        self.assertEqual(claims_for_review(required, delivered, None), {})


class KeyedLifecycleTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples/plan.json")

    def calls(self, backend, **match):
        return [call for call in backend.calls if all(call.get(k) == v for k, v in match.items())]

    @staticmethod
    def candidate(kwargs, backend):
        with (kwargs["cwd"] / "hello.py").open("a") as stream:
            stream.write(f"# candidate {sum(c['role'] == 'worker' for c in backend.calls)}\n")

    def test_extra_disposition_key_is_invalid_and_redone_keeping_the_returned_review(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] != "review":
                return
            if len(self.calls(backend, kind="review")) == 1:
                result.update(verdict="request_changes", findings=[finding()])
            else:
                result["finding_dispositions"]["f9"] = {"status": "resolved", "evidence": ["advisory"]}
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "连续两次无效：response.finding_dispositions"):
            engine.start("Implement greet", self.plan())
        self.assertNotIn("reviewer:review:greet", engine.run["review_receipts"])
        reviews = [d for d in engine.run["dispatches"] if d.get("kind") == "review"]
        self.assertEqual([d["status"] for d in reviews], ["completed", "invalid", "invalid"])
        self.assertEqual(reviews[2]["retry_of"], reviews[1]["id"])
        for entry in reviews[1:]:
            returned = read_json(Path(entry["invalid_output"]["artifact"]))
            self.assertIn("f9", returned["finding_dispositions"])
        self.assertNotIn("pending_acceptance", engine.run)
        self.assertEqual(len(engine.run["review_history"]["greet"]), 1)

    def test_extra_repair_claim_key_rejects_the_worker_result(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(self.calls(backend, kind="review")) == 1:
                result.update(verdict="request_changes", findings=[finding()])
            elif kwargs["role"] == "worker" and "repair_claims" in result:
                result["repair_claims"]["f9"] = {"summary": "voluntary", "evidence_paths": ["hello.py"]}
                write_json(kwargs["job"] / "response.json", result)
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        # Every rework result with the extra key is invalid: each spends one attempt.
        with self.assertRaisesRegex(HarnessError, "尝试次数上限"):
            engine.start("Implement greet", self.plan())
        workers = [d for d in engine.run["dispatches"] if d["role"] == "worker"]
        self.assertEqual([(d["status"], d["worker_receipt_consumed"]) for d in workers[1:]],
                         [("invalid", "result_rejected")] * (len(workers) - 1))
        self.assertIn("missing or unexpected fields", workers[-1]["error"])
        self.assertEqual(engine.run["attempts"], {"greet": self.config.data["runtime"]["max_attempts"]})
        self.assertNotIn("pending_task", engine.run)

    def test_field_test_shape_approves_with_exactly_the_required_keys(self):
        # HF-02: the third review could dispose an advisory from history. With
        # runtime keys there is no place to write it.
        def change(backend, kwargs, packet, result):
            number = len(self.calls(backend, kind="review"))
            if kwargs["role"] == "worker":
                self.candidate(kwargs, backend)
            elif kwargs["kind"] == "review" and number == 1:
                result.update(verdict="request_changes", findings=[{**finding(), "id": x} for x in "ABC"])
            elif kwargs["kind"] == "review" and number == 2:
                result.update(verdict="request_changes", findings=[
                    {**finding(), "id": "E"}, {**finding("advisory", "preference"), "id": "D"}])
        backend = ScriptedBackend(change)
        result = self.engine(backend).start("Implement greet", self.plan())
        self.assertEqual((result["status"], result["attempts"]), ("completed", {"greet": 3}))
        workers, reviews = self.calls(backend, role="worker"), self.calls(backend, kind="review")
        self.assertEqual([label for _, _, label in labelled(workers[2]["packet"])], ["E"])
        self.assertEqual([(key, label) for key, _, label in labelled(reviews[2]["packet"])],
                         [("f1", "A"), ("f2", "B"), ("f3", "C"), ("f4", "E")])
        last = result["review_history"]["greet"][-1]
        self.assertEqual(set(last["review"]["finding_dispositions"]), {"f1", "f2", "f3", "f4"})
        self.assertEqual(last["required_findings"], complete_rows(reviews[2]["packet"]))

    def test_repeated_labels_are_independent_findings_and_approval_labels_may_repeat(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(self.calls(backend, kind="review")) == 1:
                result.update(verdict="request_changes", findings=[finding(), finding()])
            elif kwargs["kind"] == "review":
                result["findings"] = [finding("advisory", "preference")] * 2
        backend = ScriptedBackend(change)
        result = self.engine(backend).start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        worker = self.calls(backend, role="worker")[1]
        self.assertEqual(labelled(worker["packet"]), [("f1", 0, "edge-case"), ("f2", 1, "edge-case")])
        self.assertEqual(set(result["review_history"]["greet"][-1]["review"]["finding_dispositions"]), {"f1", "f2"})

    def asymmetric(self, resolved, still_open):
        def change(backend, kwargs, packet, result):
            number = len(self.calls(backend, kind="review"))
            if kwargs["role"] == "worker":
                self.candidate(kwargs, backend)
            elif kwargs["kind"] == "review" and number == 1:
                result.update(verdict="request_changes", findings=[
                    {**finding(), "id": "A"}, {**finding("advisory", "preference"), "id": "N"}, {**finding(), "id": "B"}])
            elif kwargs["kind"] == "review" and number == 2:
                result.update(verdict="request_changes", findings=[])
                result["finding_dispositions"][finding_key(packet, resolved)]["status"] = "resolved"
                result["finding_dispositions"][finding_key(packet, still_open)]["status"] = "open"
        return ScriptedBackend(change)

    def test_each_key_closes_only_the_finding_it_was_bound_to(self):
        for resolved, still_open in (("A", "B"), ("B", "A")):
            with self.subTest(resolved=resolved):
                self.setUp()
                backend = self.asymmetric(resolved, still_open)
                result = self.engine(backend).start("Implement greet", self.plan())
                self.assertEqual((result["status"], result["attempts"]), ("completed", {"greet": 3}))
                worker = self.calls(backend, role="worker")[2]
                self.assertEqual([label for _, _, label in labelled(worker["packet"])], [still_open])
                self.assertEqual(worker["packet"]["required_findings"][0]["key"], "f1")
                reviews = self.calls(backend, kind="review")
                self.assertEqual([label for _, _, label in labelled(reviews[2]["packet"])], ["A", "B"])
                for record, call in zip(result["review_history"]["greet"], reviews):
                    self.assertEqual(record["required_findings"], complete_rows(call["packet"]))

    def test_changed_copy_stops_before_any_worker_or_review_dispatch(self):
        self.config.data["runtime"]["max_attempts"] = 2
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                result.update(verdict="request_changes", findings=[{**finding(), "id": f"R{len(backend.calls)}"}])
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "尝试次数上限"):
            engine.start("Implement greet", self.plan())
        state_path = engine.directory / "state.json"
        state = read_json(state_path)
        state["review_history"]["greet"][-1]["required_findings"][0]["label"] = "rebound"
        write_json(state_path, state)
        self.config.data["runtime"]["max_attempts"] = 3
        workers, reviews = len(self.calls(backend, role="worker")), len(self.calls(backend, role="reviewer"))
        with self.assertRaisesRegex(HarnessError, "对照不符"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(self.calls(backend, role="worker")), workers)
        self.assertEqual(len(self.calls(backend, role="reviewer")), reviews)

    def rework_round(self, second_worker):
        """First review rejects; the second worker returns a result that carries repair_claims."""
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(self.calls(backend, kind="review")) == 1:
                result.update(verdict="request_changes", findings=[finding()])
            elif kwargs["role"] == "worker" and len(self.calls(backend, role="worker")) == 2:
                self.assertIn("f1", result["repair_claims"])
                second_worker(kwargs, result)
                write_json(kwargs["job"] / "response.json", result)
        return ScriptedBackend(change)

    def test_checkpoint_sources_with_claims_validate_against_the_delivered_contract(self):
        import hashlib
        def checkpoint(kwargs, result):
            digest = hashlib.sha256((kwargs["cwd"] / "hello.py").read_bytes()).hexdigest()
            result["checkpoints"] = [{"id": "greet-file", "summary": "greet exists",
                                      "inputs": [{"path": "hello.py", "sha256": digest}],
                                      "evidence": [{"path": "hello.py", "sha256": digest}],
                                      "freshness": "content", "max_age_seconds": 0}]
        backend = self.rework_round(checkpoint)
        result = self.engine(backend).start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["task_checkpoints"]), 1)

    def test_environment_blocker_with_claims_keeps_exemption_and_update_source(self):
        def environment(kwargs, result):
            result.update(status="blocked", blocker_kind="environment", blockers=["Dependency unavailable"])
        backend = self.rework_round(environment)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "环境受阻"):
            engine.start("Implement greet", self.plan())
        blocked = [d for d in engine.run["dispatches"] if d["role"] == "worker"][-1]
        self.assertEqual(blocked["attempt_exemption"]["blocker_kind"], "environment")
        update = self.repo / "operator-update.json"
        write_json(update, {"schema_version": 1, "task_id": "greet", "blocker_dispatch_id": blocked["id"],
                            "summary": "Operator installed the dependency", "checks": []})
        result = self.engine(backend).resume(engine.run["id"], environment_update=update)
        self.assertEqual(result["status"], "completed")

    def test_operator_blocker_with_claims_registers_and_answers_its_decision(self):
        def operator(kwargs, result):
            result.update(status="blocked", blocker_kind="operator", blockers=["Choose the greeting style"])
        backend = self.rework_round(operator)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        request = engine.run["decision_requests"][0]
        answer = self.repo / "answer.json"
        write_json(answer, {"schema_version": 1, "request_id": request["id"], "task_id": request["task_id"],
                            "decision": "continue", "summary": "Use the plain greeting"})
        result = self.engine(backend).resume(engine.run["id"], operator_decision=answer)
        self.assertEqual(result["status"], "completed")

    def test_continuation_with_claims_records_its_bound_source(self):
        command = ["python3", "-c", "print('focused check')"]
        def continuation(kwargs, result):
            result.update(status="blocked", blocker_kind="continuation", blockers=["Need a focused check"],
                          continuation_request={"kind": "commands", "commands": [command],
                                                "minimum_timeout_seconds": 0, "reason": "Finish the candidate"})
        backend = self.rework_round(continuation)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待续作条件"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(len(engine.run["continuation_history"]), 1)
        self.config.data["permissions"]["worker_commands"].append(command)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")


class FinalKeyedLifecycleTests(RepoCase):
    def backend(self):
        def change(backend, kwargs, packet, result):
            number = sum(c["kind"] == "final_review" for c in backend.calls)
            if kwargs["kind"] == "final_review" and number == 1:
                result.update(verdict="request_changes", findings=[finding()])
            elif trigger_kind(kwargs, packet) == "final" and number == 1:
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id="correct", depends_on=["greet"])
                revised(result, packet, "Resolve final blocking finding", new_tasks=[task])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                with (kwargs["cwd"] / "hello.py").open("a") as stream:
                    stream.write("# Independently reviewed final correction\n")
        return ScriptedBackend(change)

    def finals(self, backend):
        return [call for call in backend.calls if call["kind"] == "final_review"]

    def test_final_mapping_is_saved_with_the_reviewing_phase_and_rechecked_on_approval(self):
        backend = self.backend()
        engine = self.engine(backend)
        result = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.assertEqual(result["status"], "completed")
        first, second = result["final_history"]
        self.assertEqual(first["required_findings"], [])
        self.assertEqual(second["required_findings"], complete_rows(self.finals(backend)[1]["packet"]))
        self.assertEqual(second["required_findings"][0]["review_dispatch_id"], first["review_source"]["dispatch_id"])
        self.assertTrue(engine.services.finalization.final_review())
        second["required_findings"][0]["label"] = "rebound"
        with self.assertRaisesRegex(HarnessError, "整体批准"):
            engine.services.finalization.final_review()

    def test_interrupted_final_review_reuses_the_saved_mapping_without_redispatch(self):
        for point in ("before_dispatch", "after_return"):
            with self.subTest(point=point):
                self.setUp()
                backend = self.backend()
                engine = self.engine(backend)
                raised = []
                if point == "before_dispatch":
                    original = engine.services.receipts.dispatch_review
                    def interrupt(kind, *args, **kwargs):
                        if kind == "final_review" and len(self.finals(backend)) == 1 and not raised:
                            raised.append(True)
                            raise KeyboardInterrupt()
                        return original(kind, *args, **kwargs)
                    target = patch.object(engine.services.receipts, "dispatch_review", side_effect=interrupt)
                else:
                    import harness.finalization as finalization
                    original = finalization.bind_review_source
                    def interrupt(context, kind, *args):
                        if len(self.finals(backend)) == 2 and not raised:
                            raised.append(True)
                            raise KeyboardInterrupt()
                        return original(context, kind, *args)
                    target = patch.object(finalization, "bind_review_source", side_effect=interrupt)
                with target, self.assertRaises(KeyboardInterrupt):
                    engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
                saved = read_json(engine.directory / "state.json")["final_history"][-1]
                self.assertEqual((saved["phase"], saved["status"]), ("reviewing", "running"))
                calls = len(self.finals(backend))
                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(len(self.finals(backend)), 2 if point == "after_return" else calls + 1)
                self.assertEqual(result["final_history"][-1]["required_findings"], saved["required_findings"])


class TaskReviewInterruptionTests(RepoCase):
    def backend(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and sum(c["kind"] == "review" for c in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding(), {**finding(), "id": "second"}])
        return ScriptedBackend(change)

    def test_interrupted_review_reuses_the_saved_mapping_without_extra_model_calls(self):
        from harness.task_lifecycle import TaskLifecycle
        for point in ("before_dispatch", "after_return"):
            with self.subTest(point=point):
                self.setUp()
                backend = self.backend()
                engine = self.engine(backend)
                raised = []
                reviews = lambda: [c for c in backend.calls if c["kind"] == "review"]
                if point == "before_dispatch":
                    original = engine.services.receipts.dispatch_review
                    def interrupt(kind, *args, **kwargs):
                        if len(reviews()) == 1 and not raised:
                            raised.append(True)
                            raise KeyboardInterrupt()
                        return original(kind, *args, **kwargs)
                    target = patch.object(engine.services.receipts, "dispatch_review", side_effect=interrupt)
                else:
                    original = TaskLifecycle._finish_review
                    def interrupt(service, *args):
                        if len(reviews()) == 2 and not raised:
                            raised.append(True)
                            raise KeyboardInterrupt()
                        return original(service, *args)
                    target = patch.object(TaskLifecycle, "_finish_review", autospec=True, side_effect=interrupt)
                with target, self.assertRaises(KeyboardInterrupt):
                    engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
                mapping = read_json(engine.directory / "state.json")["pending_task"]["required_findings"]
                self.assertEqual([row["key"] for row in mapping], ["f1", "f2"])
                workers = len([c for c in backend.calls if c["role"] == "worker"])
                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(len([c for c in backend.calls if c["role"] == "worker"]), workers)
                self.assertEqual(len(reviews()), 2)
                self.assertEqual(complete_rows(reviews()[1]["packet"]), mapping)
                self.assertEqual(result["review_history"]["greet"][-1]["required_findings"], mapping)


if __name__ == "__main__":
    unittest.main()
