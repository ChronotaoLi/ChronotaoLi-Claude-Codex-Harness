import copy
from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class InvalidRepairBoundaryTests(RepoCase):
    def blocked_source(self, kind, callback=None):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] != kind:
                return
            if "invalid_output_correction" not in packet:
                if kind == "plan":
                    result["tasks"][0]["skills"] = ["nonexistent-skill"]
                else:
                    result["approved_memory_keys"] = ["nonexistent-memory"]
            elif callback:
                callback(packet, result)

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "不存在"):
            if kind == "plan":
                engine.start("Implement greet", pause_after_plan=True)
            else:
                engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        role = "commander" if kind == "plan" else "reviewer"
        task = "planning" if kind == "plan" else "greet" if kind == "review" else "final"
        key = f"{role}:{kind}:{task}"
        receipts = engine.run["commander_receipts"] if kind == "plan" else engine.run["review_receipts"]
        return engine, backend, key, copy.deepcopy(receipts[key])

    def test_pre_dispatch_hooks_cannot_change_source_candidate_or_spend_dispatch(self):
        for event in ("context_prepared", "pre_agent"):
            for kind in ("plan", "review", "final_review"):
                with self.subTest(event=event, kind=kind):
                    engine, backend, key, source = self.blocked_source(kind)
                    resumed = self.engine(backend)
                    calls = len(backend.calls)
                    budget = engine.run["agent_dispatches"]

                    def mutate(hook, payload, logs):
                        if hook == event:
                            with (resumed.worktree / "hello.py").open("a") as stream:
                                stream.write("# pre-dispatch changed the candidate\n")

                    with patch.object(resumed.hooks, "emit", side_effect=mutate):
                        with self.assertRaisesRegex(HarnessError, "快照已变化"):
                            resumed.resume(engine.run["id"], repair_invalid=source["dispatch_id"])
                    self.assertEqual(len(backend.calls), calls)
                    saved = read_json(engine.directory / "state.json")
                    self.assertEqual(saved["agent_dispatches"], budget)
                    self.assertEqual(saved["invalid_repairs"][key]["status"], "pending")
                    self.assertTrue(Path(source["artifact"]).is_file())

    def test_model_cannot_delete_or_replace_original_evidence(self):
        for kind in ("plan", "review", "final_review"):
            for mode in ("missing", "changed"):
                with self.subTest(kind=kind, mode=mode):
                    def tamper(packet, result):
                        artifact = Path(packet["invalid_output_correction"]["artifact"])
                        if mode == "missing":
                            artifact.unlink()
                        else:
                            artifact.write_text("{}")

                    engine, backend, key, source = self.blocked_source(kind, tamper)
                    resumed = self.engine(backend)
                    with self.assertRaisesRegex(HarnessError, "原始结果.*(?:缺失|已改变)"):
                        resumed.resume(engine.run["id"], repair_invalid=source["dispatch_id"])
                    saved = read_json(engine.directory / "state.json")
                    self.assertEqual(saved["invalid_repairs"][key]["status"], "pending")
                    self.assertEqual(saved["agent_dispatches"], engine.run["agent_dispatches"] + 1)
                    self.assertEqual(saved["accepted"], engine.run["accepted"])
                    calls = len(backend.calls)
                    with self.assertRaisesRegex(HarnessError, "原始结果.*(?:缺失|已改变)"):
                        self.engine(backend).resume(engine.run["id"])
                    self.assertEqual(len(backend.calls), calls)

    def test_post_agent_hook_cannot_erase_original_evidence(self):
        for kind in ("plan", "review", "final_review"):
            with self.subTest(kind=kind):
                engine, backend, key, source = self.blocked_source(kind)
                resumed = self.engine(backend)

                def tamper(event, payload, logs):
                    if event == "post_agent" and payload.get("kind") == kind:
                        Path(source["artifact"]).unlink()

                with patch.object(resumed.hooks, "emit", side_effect=tamper):
                    with self.assertRaisesRegex(HarnessError, "原始结果缺失"):
                        resumed.resume(engine.run["id"], repair_invalid=source["dispatch_id"])
                saved = read_json(engine.directory / "state.json")
                self.assertEqual(saved["invalid_repairs"][key]["status"], "pending")
                self.assertEqual(saved["accepted"], engine.run["accepted"])
                self.assertNotIn("pending_acceptance", saved)
                self.assertNotEqual(saved["status"], "completed")

    def test_observed_malformed_result_after_crash_stays_bound_when_removed(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)

        def interrupt(path, value):
            write_json(path, value)
            if Path(path).name == "returned-result.json" and value.get("task_id") == "final":
                Path(path).write_bytes(b"{")
                raise KeyboardInterrupt()

        with patch("harness.engine.write_json", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        key = "reviewer:final_review:final"
        receipt = engine.run["review_receipts"][key]
        self.assertNotIn("artifact_digest", receipt)
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "不是有效 JSON"):
            self.engine(backend).resume(engine.run["id"])
        saved = read_json(engine.directory / "state.json")
        self.assertIn("artifact_digest", saved["review_receipts"][key])
        Path(receipt["artifact"]).unlink()
        with self.assertRaisesRegex(HarnessError, "缺失或无法读取"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(read_json(engine.directory / "state.json")["final_attempts"], 1)

    def test_reverify_cannot_cancel_authorized_unreturned_replan_repair(self):
        correction_calls = 0

        def change(backend, kwargs, packet, result):
            nonlocal correction_calls
            if kwargs["kind"] != "replan":
                return
            if "invalid_output_correction" in packet:
                correction_calls += 1
                if correction_calls == 1:
                    raise HarnessError("correction service unavailable")
                result.update(action="stop", reason="Preserve failed verification for operator inspection",
                              new_tasks=[], updates=[])
            else:
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id="repair", depends_on=["greet"], allowed_paths=["outside.txt"])
                result.update(action="revise", reason="Repair failed verification", new_tasks=[task], updates=[])

        backend = ScriptedBackend(change)
        engine = self.engine(backend)

        def fail_final(event, payload, logs):
            if event == "post_verify" and payload.get("task_id") == "final":
                payload["results"][0]["exit_code"] = 1

        with patch.object(engine.hooks, "emit", side_effect=fail_final):
            with self.assertRaisesRegex(HarnessError, "不得自行扩大范围"):
                engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        key = "commander:replan:replanning"
        source = engine.run["commander_receipts"][key]
        with self.assertRaisesRegex(HarnessError, "service unavailable"):
            self.engine(backend).resume(engine.run["id"], repair_invalid=source["dispatch_id"])
        with self.assertRaisesRegex(HarnessError, "指挥停止重新规划"):
            self.engine(backend).resume(engine.run["id"], reverify=True)
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(correction_calls, 2)
        self.assertEqual(saved["final_attempts"], 1)
        self.assertEqual(saved["replan_attempts"], 1)
        self.assertEqual(saved["replan_stopped"]["target"], "final")
        self.assertEqual(saved["invalid_repairs"][key]["status"], "completed")
        self.assertEqual(len([d for d in saved["dispatches"] if d["role"] == "verification" and d["task_id"] == "final"]), 1)
