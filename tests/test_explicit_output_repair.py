import copy
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, finding


class ExplicitOutputRepairTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples/plan.json")

    def blocked_approval(self, kind="review", correction=None):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] != kind:
                return
            if sum(call["kind"] == kind for call in backend.calls) == 1:
                result["approved_memory_keys"] = ["nonexistent"]
            elif correction:
                correction(result)
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "不存在"):
            engine.start("Implement greet", self.plan())
        key = f"reviewer:{kind}:{'greet' if kind == 'review' else 'final'}"
        source = copy.deepcopy(engine.run["review_receipts"][key])
        return engine, backend, key, source

    def test_plan_semantic_repair_is_explicit_bounded_and_keeps_original_result(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "plan" and sum(c["kind"] == "plan" for c in backend.calls) == 1:
                result["tasks"][0]["skills"] = ["nonexistent-skill"]
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "不存在的 skill"):
            engine.start("Implement greet", pause_after_plan=True)
        source = copy.deepcopy(engine.run["commander_receipts"]["commander:plan:planning"])
        original = Path(source["artifact"]).read_bytes()
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), 1)
        result = self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(result["agent_dispatches"], 2)
        self.assertEqual(result["total_attempts"], 0)
        self.assertEqual(Path(source["artifact"]).read_bytes(), original)
        repair = result["invalid_repairs"]["commander:plan:planning"]
        self.assertEqual(repair["status"], "completed")
        self.assertNotEqual(repair["corrected_dispatch_id"], source["dispatch_id"])
        self.assertIn("invalid_output_correction", backend.calls[-1]["packet"])

    def test_task_and_final_approval_repair_reuse_worker_and_verification(self):
        for kind in ("review", "final_review"):
            with self.subTest(kind=kind):
                engine, backend, key, source = self.blocked_approval(kind)
                original = Path(source["artifact"]).read_bytes()
                old_checks = [d["id"] for d in engine.run["dispatches"] if d["role"] == "verification"]
                result = self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["total_attempts"], 1)
                self.assertEqual(result["final_attempts"], 1)
                checks = [d["id"] for d in result["dispatches"] if d["role"] == "verification"]
                self.assertEqual(checks[:len(old_checks)], old_checks)
                self.assertEqual(len(checks), 2)
                self.assertEqual(Path(source["artifact"]).read_bytes(), original)
                self.assertEqual(result["invalid_repairs"][key]["status"], "completed")

    def test_duplicate_advisory_ids_repair_keeps_findings_and_valid_memory_approval(self):
        advice = [finding("advisory", "preference"), finding("advisory", "evidence")]
        advice[1]["message"] = "A separate advisory with its own evidence"
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["proposed_memories"] = [{"key": "greeting", "abstract": "Greeting",
                    "content": "greet returns Hello, name!", "source_paths": ["hello.py"]}]
            if kwargs["kind"] == "review":
                result.update(findings=copy.deepcopy(advice), approved_memory_keys=["greeting", "nonexistent"])
                if "invalid_output_correction" in packet:
                    result["findings"][1]["id"] = "separate-advice"
                    result["approved_memory_keys"] = ["greeting"]
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "唯一"):
            engine.start("Implement greet", self.plan())
        source = engine.run["review_receipts"]["reviewer:review:greet"]
        result = self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        self.assertEqual(result["status"], "completed")
        repaired = result["review_history"]["greet"][-1]["review"]
        self.assertEqual(repaired["approved_memory_keys"], ["greeting"])
        self.assertEqual([f["message"] for f in repaired["findings"]], [f["message"] for f in advice])
        self.assertTrue(self.engine().memory.search("greet"))

    def test_invalid_correction_cannot_reroll_or_get_an_extra_format_attempt(self):
        for correction in (lambda r: r.update(summary="x" * 4001),
                           lambda r: r.update(verdict="request_changes"),
                           lambda r: r.update(findings=[finding("advisory", "preference")])):
            with self.subTest(correction=correction):
                engine, backend, key, source = self.blocked_approval(correction=correction)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
                calls = copy.deepcopy(backend.calls)
                saved = read_json(engine.directory / "state.json")
                corrected = saved["review_receipts"][key]["dispatch_id"]
                for options in ({}, {"repair_invalid": corrected}):
                    with self.assertRaises(HarnessError):
                        self.engine(backend).resume(engine.run["id"], **options)
                    self.assertEqual(backend.calls, calls)
                self.assertEqual(saved["total_attempts"], 1)
                self.assertEqual(saved["format_repairs"], {})

    def test_unreturned_correction_failure_resumes_fresh_dispatch_with_same_guard(self):
        engine, backend, key, source = self.blocked_approval()
        invoke = backend.invoke
        failed = False
        def unavailable(**kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise HarnessError("service unavailable")
            return invoke(**kwargs)
        with patch.object(backend, "invoke", side_effect=unavailable):
            with self.assertRaisesRegex(HarnessError, "service unavailable"):
                self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
            saved = read_json(engine.directory / "state.json")
            result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_attempts"], 1)
        self.assertEqual(result["agent_dispatches"], saved["agent_dispatches"] + 2)
        self.assertEqual(result["invalid_repairs"][key]["source"]["dispatch_id"], source["dispatch_id"])

    def test_correction_result_write_interruption_replays_result_without_new_dispatch(self):
        engine, backend, key, source = self.blocked_approval("final_review")
        interrupted = False
        def interrupt(path, value):
            nonlocal interrupted
            write_json(path, value)
            if not interrupted and Path(path).name == "returned-result.json":
                interrupted = True
                raise KeyboardInterrupt()
        with patch("harness.engine.write_json", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        calls = copy.deepcopy(backend.calls)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(backend.calls, calls)
        self.assertEqual(result["invalid_repairs"][key]["status"], "completed")

    def test_original_evidence_tampering_after_authorization_blocks_without_dispatch(self):
        engine, backend, key, source = self.blocked_approval()
        resumed = self.engine(backend)
        def fail(event, payload, logs):
            if event == "pre_agent":
                raise HarnessError("preparation unavailable")
        with patch.object(resumed.hooks, "emit", side_effect=fail):
            with self.assertRaisesRegex(HarnessError, "preparation unavailable"):
                resumed.resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        calls = copy.deepcopy(backend.calls)
        Path(source["artifact"]).write_text("{}")
        with self.assertRaisesRegex(HarnessError, "已改变"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)

    def test_missing_review_receipt_updates_dispatch_and_runtime_error(self):
        engine, backend, key, source = self.blocked_approval("final_review")
        Path(source["artifact"]).unlink()
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "缺失或无法读取"):
            resumed.resume(engine.run["id"])
        self.assertIn("缺失或无法读取", resumed.run["last_runtime_error"]["error"])
        entry = next(d for d in resumed.run["dispatches"] if d["id"] == source["dispatch_id"])
        self.assertEqual(entry["error"], resumed.run["last_runtime_error"]["error"])

    def test_valid_hook_failed_approval_and_rejection_cannot_be_discarded(self):
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                def change(backend, kwargs, packet, result):
                    if rejected and kwargs["kind"] == "final_review":
                        result.update(verdict="request_changes", findings=[finding()])
                backend = ScriptedBackend(change)
                engine = self.engine(backend)
                def fail(event, payload, logs):
                    if event == "post_agent" and payload.get("kind") == "final_review":
                        raise HarnessError("hook unavailable")
                with patch.object(engine.hooks, "emit", side_effect=fail):
                    with self.assertRaisesRegex(HarnessError, "hook unavailable"):
                        engine.start("Implement greet", self.plan())
                source = engine.run["review_receipts"]["reviewer:final_review:final"]
                calls = copy.deepcopy(backend.calls)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
                self.assertEqual(backend.calls, calls)
                self.assertEqual(read_json(engine.directory / "state.json")["invalid_repairs"], {})

    def test_structure_identity_and_changed_snapshot_do_not_authorize_repair(self):
        for mode in ("structure", "identity", "snapshot"):
            with self.subTest(mode=mode):
                def change(backend, kwargs, packet, result):
                    if kwargs["kind"] == "review":
                        result["approved_memory_keys"] = ["nonexistent"]
                        if mode == "structure":
                            result["extra"] = "invalid"
                        if mode == "identity":
                            result["dispatch_id"] = "other-dispatch"
                backend = ScriptedBackend(change)
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start("Implement greet", self.plan())
                source = engine.run["review_receipts"]["reviewer:review:greet"]
                if mode == "snapshot":
                    with (engine.worktree / "hello.py").open("a") as output:
                        output.write("# different candidate\n")
                calls = copy.deepcopy(backend.calls)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
                self.assertEqual(backend.calls, calls)

    def test_explicit_repair_respects_dispatch_budget_and_ordinary_resume_after_increase(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 2
        engine, backend, key, source = self.blocked_approval()
        with self.assertRaisesRegex(HarnessError, "模型派发次数上限"):
            self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        self.assertEqual(len(backend.calls), 2)
        self.config.data["runtime"]["max_agent_dispatches"] = 4
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["agent_dispatches"], 4)
        self.assertEqual(result["total_attempts"], 1)

    def test_version_seven_cannot_ignore_new_repair_guards(self):
        engine, backend, key, source = self.blocked_approval()
        engine.run["schema_version"] = 7
        engine.save()
        with self.assertRaisesRegex(HarnessError, "旧 run"):
            self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])

    def test_replan_semantic_correction_keeps_original_task_and_budget(self):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="task", blockers=["Need prerequisite"])
            if kwargs["kind"] == "replan":
                result.update(action="revise", reason="Prepare prerequisite", new_tasks=[], updates=[])
                if "invalid_output_correction" in packet:
                    original = packet["original_tasks"][0]
                    result["new_tasks"] = [{**copy.deepcopy(original), "id": "prepare"}]
                    result["updates"] = [{"id": original["id"], "description": original["description"],
                                          "add_depends_on": ["prepare"]}]
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        plan = self.plan()
        with self.assertRaisesRegex(HarnessError, "有效变更"):
            engine.start("Implement greet", plan)
        source = engine.run["commander_receipts"]["commander:replan:replanning"]
        result = self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["original_tasks"], plan["tasks"])
        self.assertEqual(result["attempts"], {"greet": 2, "prepare": 1})
        self.assertEqual(result["replan_attempts"], 1)
        self.assertEqual(sum(c["kind"] == "replan" for c in backend.calls), 2)

    def test_cli_explicit_repair_and_scope_extension_reach_runtime(self):
        from test_cli_controls import CLIControlsTests
        engine, backend, key, source = self.blocked_approval()
        code, engines, error = CLIControlsTests.invoke(
            self, ["resume", engine.run["id"], "--repair-invalid", source["dispatch_id"]], backend)
        self.assertEqual(code, 0, error)
        self.assertEqual(engines[0].run["status"], "completed")

        def stopped(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.update(status="blocked", blocker_kind="task", blockers=["Missing scope"])
        backend = ScriptedBackend(stopped)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        calls = copy.deepcopy(backend.calls)
        code, engines, error = CLIControlsTests.invoke(
            self, ["resume", engine.run["id"], "--allow-scope", "support.txt"], backend)
        self.assertEqual(code, 1)
        self.assertIn("停止重新规划", error)
        self.assertEqual(engines[0].run["approved_scope"], ["support.txt"])
        self.assertEqual(backend.calls, calls)
