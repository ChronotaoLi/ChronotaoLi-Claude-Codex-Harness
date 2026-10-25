import copy
import hashlib
from pathlib import Path
from unittest.mock import patch

from harness.recovery import snapshot_digest
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, finding


class ExplicitFormatRepairTests(RepoCase):
    def plan(self, multiple=False):
        plan = read_json(ROOT / "examples/plan.json")
        if multiple:
            plan["tasks"].append({**copy.deepcopy(plan["tasks"][0]), "id": "other"})
        return plan

    def review_failure(self, kind="review", change=None, engine_setup=None):
        def malformed(backend, kwargs, packet, result):
            if kwargs["kind"] != kind:
                return
            result["findings"] = [finding("advisory", "preference")]
            if "invalid_output_correction" not in packet:
                result["summary"] = "x" * 4001
            if change:
                change(backend, kwargs, packet, result)

        backend = ScriptedBackend(malformed)
        engine = self.engine(backend)
        if engine_setup:
            engine_setup(engine)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        key = f"reviewer:{kind}:{'greet' if kind == 'review' else 'final'}"
        return engine, backend, key, copy.deepcopy(engine.run["review_receipts"][key])

    def test_task_and_final_exhausted_format_repair_keep_original_evidence_and_attempts(self):
        for kind in ("review", "final_review"):
            with self.subTest(kind=kind):
                engine, backend, key, source = self.review_failure(kind)
                guard = copy.deepcopy(engine.run["review_corrections"][key])
                evidence = {path: Path(path).read_bytes() for path in (guard["artifact"], source["artifact"])}
                calls = len(backend.calls)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                self.assertEqual(len(backend.calls), calls)
                result = self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["total_attempts"], 1)
                self.assertEqual(result["final_attempts"], 1)
                self.assertEqual(result["format_repairs"][key], 1)
                repair = result["invalid_repairs"][key]
                self.assertEqual(repair["mode"], "format")
                self.assertEqual(repair["source"], source)
                self.assertEqual(repair["original"]["artifact_digest"], guard["artifact_digest"])
                self.assertNotEqual(repair["corrected_dispatch_id"], source["dispatch_id"])
                for path, raw in evidence.items():
                    self.assertEqual(Path(path).read_bytes(), raw)
                reviews = [c for c in backend.calls if c["kind"] == kind]
                self.assertEqual(len(reviews), 3)
                packet = reviews[-1]["packet"]["invalid_output_correction"]
                self.assertEqual(packet["artifact"], guard["artifact"])
                self.assertEqual(packet["failed_result"]["artifact"], source["artifact"])

    def test_explicit_review_cannot_change_earliest_findings_or_valid_ids(self):
        for mutation in (lambda r: r["findings"][0].update(id="new-valid-id"),
                         lambda r: r["findings"][0].update(message="Different advice"),
                         lambda r: r.update(findings=[]),
                         lambda r: r.update(verdict="request_changes")):
            with self.subTest(mutation=mutation):
                def change(backend, kwargs, packet, result):
                    if "invalid_output_correction" in packet:
                        mutation(result)
                engine, backend, key, source = self.review_failure(change=change)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
                saved = read_json(engine.directory / "state.json")
                self.assertEqual(saved["accepted"], {})
                calls = len(backend.calls)
                for options in ({}, {"repair_invalid": saved["review_receipts"][key]["dispatch_id"]}):
                    with self.assertRaises(HarnessError):
                        self.engine(backend).resume(engine.run["id"], **options)
                    self.assertEqual(len(backend.calls), calls)

    def test_format_repair_preserves_valid_memory_approvals_and_earliest_valid_ids(self):
        def malformed(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["proposed_memories"] = [{"key": "greeting", "abstract": "Greeting",
                    "content": "The greeting is independently tested", "source_paths": ["hello.py"]}]
            if kwargs["kind"] == "review":
                advice = [finding("advisory", "preference"), finding("advisory", "evidence")]
                advice[1].update(id="invalid/id", message="Another independent advisory")
                if "format_correction" in packet:
                    advice[1]["id"] = "automatic-id"
                result.update(findings=advice, approved_memory_keys=["greeting", "greeting"], summary=" ")
                if "invalid_output_correction" in packet:
                    advice[1]["id"] = "explicit-id"
                    result.update(summary="Approved after formatting", approved_memory_keys=["greeting"])
        backend = ScriptedBackend(malformed)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        key = "reviewer:review:greet"
        source = engine.run["review_receipts"][key]
        result = self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        review = result["review_history"]["greet"][-1]["review"]
        self.assertEqual(review["approved_memory_keys"], ["greeting"])
        self.assertEqual([f["id"] for f in review["findings"]], ["edge-case", "explicit-id"])
        self.assertTrue(self.engine().memory.search("greeting"))

    def test_valid_automatic_correction_with_failed_hook_cannot_be_replaced(self):
        def valid_correction(backend, kwargs, packet, result):
            if "format_correction" in packet:
                result["summary"] = "Approval with corrected formatting"
        def setup(engine):
            def fail(event, payload, logs):
                if event == "post_agent" and payload.get("kind") == "review":
                    raise HarnessError("approval hook unavailable")
            engine.hooks.emit = fail
        engine, backend, key, source = self.review_failure(change=valid_correction, engine_setup=setup)
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "结果合法"):
            self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        self.assertEqual(len(backend.calls), calls)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(c["kind"] == "review" for c in backend.calls), 2)

    def test_exhausted_guard_cannot_hide_rejection_structure_semantics_or_immutable_error(self):
        for mode in ("rejected", "blocking", "immutable", "structure", "identity", "semantics", "changed"):
            with self.subTest(mode=mode):
                def change(backend, kwargs, packet, result):
                    if mode == "rejected":
                        result["verdict"] = "request_changes"
                    elif mode == "blocking":
                        result["findings"] = [finding()]
                    elif mode == "immutable":
                        result["findings"][0]["evidence"] = ["x" * 4001]
                    elif "format_correction" in packet:
                        if mode == "structure":
                            result["extra"] = "unexpected"
                        elif mode == "identity":
                            result["dispatch_id"] = "other"
                        elif mode == "semantics":
                            result["approved_memory_keys"] = ["missing"]
                        elif mode == "changed":
                            result["findings"][0]["message"] = "Changed finding"
                engine, backend, key, source = self.review_failure(change=change)
                calls = len(backend.calls)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
                self.assertEqual(len(backend.calls), calls)
                self.assertEqual(read_json(engine.directory / "state.json")["invalid_repairs"], {})

    def test_returned_explicit_format_error_is_terminal_without_another_automatic_attempt(self):
        def malformed(backend, kwargs, packet, result):
            result["summary"] = " "
        engine, backend, key, source = self.review_failure(change=malformed)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        saved = read_json(engine.directory / "state.json")
        calls = len(backend.calls)
        for options in ({}, {"repair_invalid": saved["review_receipts"][key]["dispatch_id"]}):
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run["id"], **options)
            self.assertEqual(len(backend.calls), calls)
        self.assertEqual(saved["agent_dispatches"], engine.run["agent_dispatches"] + 1)
        self.assertEqual(saved["format_repairs"][key], 1)

    def test_unreturned_explicit_format_correction_retries_fresh_dispatch_and_counts_budget(self):
        failed = False
        def unavailable(backend, kwargs, packet, result):
            nonlocal failed
            if "invalid_output_correction" in packet and not failed:
                failed = True
                raise HarnessError("correction service unavailable")
        engine, backend, key, source = self.review_failure("final_review", unavailable)
        with self.assertRaisesRegex(HarnessError, "service unavailable"):
            self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        saved = read_json(engine.directory / "state.json")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["agent_dispatches"], saved["agent_dispatches"] + 1)
        self.assertEqual(result["total_attempts"], 1)
        self.assertNotEqual(result["invalid_repairs"][key]["corrected_dispatch_id"], saved["dispatches"][-1]["id"])

    def test_explicit_correction_result_write_crash_replays_without_new_dispatch(self):
        engine, backend, key, source = self.review_failure("final_review")
        interrupted = False
        def interrupt(path, value):
            nonlocal interrupted
            write_json(path, value)
            if not interrupted and Path(path).name == "returned-result.json":
                interrupted = True
                raise KeyboardInterrupt()
        with patch("harness.dispatch.write_json", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        calls = len(backend.calls)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(result["invalid_repairs"][key]["status"], "completed")

    def test_interrupted_exhausted_guard_write_can_authorize_from_bound_receipt(self):
        backend = ScriptedBackend(lambda backend, kwargs, packet, result:
            result.update(summary="x" * 4001) if kwargs["kind"] == "review" and
            "invalid_output_correction" not in packet else None)
        engine = self.engine(backend)
        save = engine.save
        key = "reviewer:review:greet"
        def interrupt():
            if engine.run["review_corrections"].get(key, {}).get("exhausted"):
                raise KeyboardInterrupt()
            save()
        with patch.object(engine, "save", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertFalse(saved["review_corrections"][key].get("exhausted"))
        source = saved["review_receipts"][key]
        result = self.engine(backend).resume(saved["id"], repair_invalid=source["dispatch_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(c["kind"] == "review" for c in backend.calls), 3)

    def test_interrupted_failed_guard_write_cannot_authorize_a_changed_conclusion(self):
        def malformed(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                result.update(summary=" ", findings=[finding("advisory", "preference")])
                if "format_correction" in packet:
                    result["findings"] = []
        backend = ScriptedBackend(malformed)
        engine = self.engine(backend)
        save = engine.save
        key = "reviewer:review:greet"
        def interrupt():
            if engine.run["review_corrections"].get(key, {}).get("failed"):
                raise KeyboardInterrupt()
            save()
        with patch.object(engine, "save", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertFalse(saved["review_corrections"][key].get("failed"))
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "不得改变"):
            self.engine(backend).resume(saved["id"], repair_invalid=saved["review_receipts"][key]["dispatch_id"])
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(read_json(engine.directory / "state.json")["invalid_repairs"], {})

    def test_both_original_and_failed_correction_remain_bound_after_authorization(self):
        for which in ("original", "source"):
            with self.subTest(which=which):
                engine, backend, key, source = self.review_failure()
                resumed = self.engine(backend)
                def fail(event, payload, logs):
                    if event == "pre_agent":
                        raise HarnessError("preparation unavailable")
                with patch.object(resumed.hooks, "emit", side_effect=fail), self.assertRaisesRegex(HarnessError, "preparation unavailable"):
                    resumed.resume(engine.run["id"], repair_invalid=source["dispatch_id"])
                saved = read_json(engine.directory / "state.json")
                artifact = Path(saved["invalid_repairs"][key][which]["artifact"])
                artifact.write_text("{}")
                calls = len(backend.calls)
                with self.assertRaisesRegex(HarnessError, "已改变"):
                    self.engine(backend).resume(engine.run["id"])
                self.assertEqual(len(backend.calls), calls)

    def test_format_repair_dispatch_budget_can_be_increased_without_resetting_attempts(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 3
        engine, backend, key, source = self.review_failure()
        with self.assertRaisesRegex(HarnessError, "模型派发次数上限"):
            self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        self.assertEqual(len(backend.calls), 3)
        self.config.data["runtime"]["max_agent_dispatches"] = 5
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["agent_dispatches"], 5)
        self.assertEqual(result["attempts"], {"greet": 1})

    def test_changed_candidate_archives_both_reviews_and_keeps_spent_explicit_quota(self):
        engine, backend, key, source = self.review_failure()
        resumed = self.engine(backend)
        def fail(event, payload, logs):
            if event == "pre_agent":
                raise HarnessError("preparation unavailable")
        with patch.object(resumed.hooks, "emit", side_effect=fail), self.assertRaises(HarnessError):
            resumed.resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        with (engine.worktree / "hello.py").open("a") as stream:
            stream.write("# candidate changed\n")
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["invalid_repairs"][key]["status"], "invalidated")
        old = saved["invalid_repairs"][key]
        archived = {r["artifact"] for r in saved["review_history"]["greet"]}
        self.assertTrue({old["original"]["artifact"], old["source"]["artifact"]} <= archived)
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "额度已使用"):
            self.engine(backend).resume(saved["id"], repair_invalid=saved["review_receipts"][key]["dispatch_id"])
        self.assertEqual(len(backend.calls), calls)

    def commander_failure(self, kind, change=None):
        def malformed(backend, kwargs, packet, result):
            if kind == "replan" and kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="task", blockers=["Clarify the prerequisite"])
            if kwargs["kind"] != kind:
                return
            explicit = "invalid_output_correction" in packet
            if kind == "dispatch":
                result.update(selected_task="other", instruction="Implement the selected task" if explicit else " ")
            else:
                result.update(action="revise", reason="Clarify the implementation" if explicit else " ", new_tasks=[],
                    updates=[{"id": "greet", "description": "Implement the clarified greeting", "add_depends_on": []}])
            if change:
                change(backend, kwargs, packet, result)
        backend = ScriptedBackend(malformed)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan(multiple=kind == "dispatch"))
        key = f"commander:{kind}:{'dispatching' if kind == 'dispatch' else 'replanning'}"
        return engine, backend, key, copy.deepcopy(engine.run["commander_receipts"][key])

    def test_commander_selection_and_revision_format_repair_preserve_original_decision(self):
        for kind in ("dispatch", "replan"):
            with self.subTest(kind=kind):
                engine, backend, key, source = self.commander_failure(kind)
                result = self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"], step=True)
                self.assertEqual(result["status"], "paused" if kind == "dispatch" else "completed")
                self.assertEqual(sum(c["kind"] == kind for c in backend.calls), 3)
                self.assertEqual(result["format_repairs"][key], 1)
                self.assertEqual(result["invalid_repairs"][key]["mode"], "format")
                if kind == "dispatch":
                    self.assertEqual(set(result["accepted"]), {"other"})
                else:
                    self.assertEqual(result["replan_attempts"], 1)
                    self.assertEqual(result["tasks"][0]["description"], "Implement the clarified greeting")

    def test_automatic_and_explicit_commander_format_correction_cannot_replace_decision(self):
        for kind in ("dispatch", "replan"):
            for stage in ("automatic", "explicit"):
                with self.subTest(kind=kind, stage=stage):
                    def change(backend, kwargs, packet, result):
                        if (stage == "automatic" and "format_correction" in packet or
                                stage == "explicit" and "invalid_output_correction" in packet):
                            if kind == "dispatch":
                                result["selected_task"] = "greet"
                            else:
                                result["updates"][0]["description"] = "Choose a different revision"
                    engine, backend, key, source = self.commander_failure(kind, change)
                    calls = len(backend.calls)
                    with self.assertRaisesRegex(HarnessError, "不得改变"):
                        self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
                    self.assertEqual(len(backend.calls), calls + (stage == "explicit"))
                    saved = read_json(engine.directory / "state.json")
                    self.assertEqual(saved["accepted"], {})
                    with self.assertRaises(HarnessError):
                        self.engine(backend).resume(engine.run["id"])
                    self.assertEqual(len(backend.calls), calls + (stage == "explicit"))

    def test_old_commander_format_receipt_without_original_digest_cannot_authorize(self):
        engine, backend, key, source = self.commander_failure("dispatch")
        receipt = engine.run["commander_receipts"][key]
        receipt["request"]["packet"]["format_correction"].pop("source")
        receipt["request_digest"] = snapshot_digest(receipt["request"])
        engine.save()
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "缺少原始结果摘要"):
            self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        self.assertEqual(len(backend.calls), calls)

    def test_old_unreturned_commander_format_correction_cannot_resume_new_dispatch(self):
        for kind in ("dispatch", "replan"):
            with self.subTest(kind=kind):
                def unavailable(backend, kwargs, packet, result):
                    if "format_correction" in packet:
                        raise HarnessError("legacy correction service unavailable")
                engine, backend, key, source = self.commander_failure(kind, unavailable)
                pending = engine.run["pending_format_corrections"][key]
                pending["correction"].pop("source")
                receipt = engine.run["commander_receipts"][key]
                receipt["request"]["packet"]["format_correction"].pop("source")
                receipt["request_digest"] = snapshot_digest(receipt["request"])
                engine.save()
                self.assertFalse(Path(source["artifact"]).exists())
                calls, dispatches = len(backend.calls), len(engine.run["dispatches"])
                for _ in range(2):
                    with self.assertRaisesRegex(HarnessError, "缺少原始结果摘要.*abandon"):
                        self.engine(backend).resume(engine.run["id"])
                    saved = read_json(engine.directory / "state.json")
                    self.assertEqual(len(backend.calls), calls)
                    self.assertEqual(len(saved["dispatches"]), dispatches)
                    self.assertEqual(saved["accepted"], {})
                    self.assertEqual(saved["pending_format_corrections"][key], pending)

    def test_old_returned_commander_format_correction_cannot_accept_replaced_decision(self):
        for kind in ("dispatch", "replan"):
            with self.subTest(kind=kind):
                engine, backend, key, source = self.commander_failure(kind)
                receipt = engine.run["commander_receipts"][key]
                correction = receipt["request"]["packet"]["format_correction"]
                original = copy.deepcopy(correction.pop("source"))
                original_raw = Path(original["artifact"]).read_bytes()
                # Reconstruct a legacy result that changed the decision and
                # passed validation before its post_agent hook failed.
                result = read_json(Path(source["artifact"]))
                if kind == "dispatch":
                    result.update(selected_task="greet", instruction="Implement another task")
                else:
                    result.update(reason="Use a different revision")
                    result["updates"][0]["description"] = "Replace the original revision"
                write_json(Path(source["artifact"]), result)
                receipt["artifact_digest"] = hashlib.sha256(Path(source["artifact"]).read_bytes()).hexdigest()
                receipt["request_digest"] = snapshot_digest(receipt["request"])
                receipt["hook_attempts"] = 1
                engine.save()
                calls, dispatches = len(backend.calls), len(engine.run["dispatches"])
                for _ in range(2):
                    with self.assertRaisesRegex(HarnessError, "缺少原始结果摘要.*abandon"):
                        self.engine(backend).resume(engine.run["id"], step=True)
                    saved = read_json(engine.directory / "state.json")
                    self.assertEqual(len(backend.calls), calls)
                    self.assertEqual(len(saved["dispatches"]), dispatches)
                    self.assertEqual(saved["accepted"], {})
                    self.assertEqual(saved["replan_attempts"], 0)
                    self.assertEqual(saved["commander_receipts"][key]["hook_attempts"], 1)
                    self.assertEqual(Path(original["artifact"]).read_bytes(), original_raw)
