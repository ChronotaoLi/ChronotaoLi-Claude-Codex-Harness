from unittest.mock import patch

from harness import git
from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class SnapshotEfficiencyTests(RepoCase):
    def test_normal_lifecycle_reuses_only_adjacent_snapshots(self):
        engine = self.engine()
        with patch("harness.git.snapshot", wraps=git.snapshot) as snapshots:
            result = engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_attempts"], 1)
        self.assertEqual(result["final_attempts"], 1)
        self.assertLessEqual(snapshots.call_count, 15)

    def test_reused_verification_snapshot_still_detects_hook_changes(self):
        engine = self.engine()
        original = engine.hooks.emit

        def change_after_checks(event, payload, logs):
            original(event, payload, logs)
            if event == "post_verify":
                with (engine.worktree / "hello.py").open("a") as output:
                    output.write("# Changed after checks\n")

        with patch.object(engine.hooks, "emit", side_effect=change_after_checks):
            with self.assertRaisesRegex(HarnessError, "验证命令修改"):
                engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertFalse(engine.run["accepted"])
        self.assertFalse(any(d["role"] == "reviewer" for d in engine.run["dispatches"]))

    def test_blank_commander_instruction_needs_new_dispatch_before_worker(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "dispatch" and "format_correction" not in packet:
                result["instruction"] = " "

        plan = read_json(ROOT / "examples" / "plan.json")
        plan["tasks"].append({**plan["tasks"][0], "id": "other"})
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        result = engine.start("Implement greet", plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([c["kind"] for c in backend.calls[:3]], ["dispatch", "dispatch", "implement"])
        workers = [c for c in backend.calls if c["role"] == "worker"]
        self.assertTrue(all(c["packet"]["instruction"].strip() for c in workers))
        calls = [d["id"] for d in result["dispatches"] if d["role"] == "commander"]
        self.assertEqual(len(calls), len(set(calls)))
