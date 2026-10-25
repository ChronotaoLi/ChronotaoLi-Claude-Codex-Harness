"""Final-review estimates and dispatched evidence share live packet construction."""
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from harness.context import capacity_report
from harness.util import read_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, finding


class FinalPacketBuilderTests(RepoCase):
    def test_preflight_and_final_dispatch_share_evidence_across_corrective_plan(self):
        evidence = self.repo.resolve() / "evidence.bin"
        evidence.write_bytes(b"independent check evidence\n")
        observed = []

        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                if kwargs["task"]["id"] == "correct":
                    with (kwargs["cwd"] / "hello.py").open("a") as stream:
                        stream.write("# accepted corrective change\n")
                result["checkpoints"] = [{
                    "id": "source-check", "summary": "Source and external evidence checked",
                    "inputs": [{"path": "hello.py", "sha256": hashlib.sha256(
                        (kwargs["cwd"] / "hello.py").read_bytes()).hexdigest()}],
                    "evidence": [{"path": str(evidence), "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}],
                    "freshness": "content", "max_age_seconds": 0,
                }]
            elif kwargs["kind"] == "final_review":
                captured = []

                def capture(required, projected, budget):
                    captured.append((copy.deepcopy(required), copy.deepcopy(projected)))
                    return capacity_report(required, projected, budget)

                with patch("harness.packets.capacity_report", side_effect=capture):
                    engine.final_context_capacity()
                actual = json.loads(kwargs["prompt"].split("\n\nREFERENCE MEMORY")[0])
                observed.append((actual, *captured[0]))
                if len(observed) == 1:
                    result.update(verdict="request_changes", findings=[finding()])
            elif kwargs["kind"] == "replan":
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id="correct", title="Correct final finding",
                            description="Resolve the final review finding", depends_on=["greet"])
                result.update(action="revise", reason="Correct the rejected result", new_tasks=[task], updates=[])

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        result = engine.start("Implement greeting", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(observed), 2)
        for actual, required, projected in observed:
            for estimated in (required, projected):
                # Dispatch identity is new; future output and artifact lengths
                # are the deliberate preflight estimates, not current evidence.
                expected = copy.deepcopy(actual)
                expected["dispatch_id"] = estimated["dispatch_id"]
                expected["packet"]["diff_artifact"] = estimated["packet"]["diff_artifact"]
                expected["packet"]["verification"] = estimated["packet"]["verification"]
                self.assertEqual(estimated, expected)
                self.assertEqual(estimated["packet"]["task_checkpoints"], [])
                self.assertEqual(estimated["packet"]["checkpoint_history"], [])
            packet = actual["packet"]
            self.assertTrue(Path(packet["diff_artifact"]).is_file())
            self.assertTrue(all(check["exit_code"] == 0 for check in packet["verification"]))
            rows = read_json(Path(packet["checkpoint_index"]["artifact"]))["checkpoints"]
            self.assertEqual(len(rows), packet["checkpoint_index"]["count"])
        first, second = observed[0][0]["packet"], observed[1][0]["packet"]
        self.assertEqual(first["prior_reviews"], [])
        self.assertEqual(second["prior_reviews"][0]["status"], "rejected")
        self.assertEqual(second["prior_reviews"][0]["artifact"], result["final_history"][0]["artifact"])
        self.assertEqual(second["tasks_delta"]["new_tasks"][0]["id"], "correct")
        self.assertEqual(second["checkpoint_index"]["count"], 2)

    def test_builder_reads_replaced_run_and_does_not_reuse_plan_or_history(self):
        engine = self.engine()
        engine.start("Initial goal", read_json(ROOT / "examples" / "plan.json"), pause_after_plan=True)
        builder = engine._packets
        first = builder.final_review_packet(checks=[], diff_artifact="first.diff", changed_paths=[])
        replacement = copy.deepcopy(engine.run)
        replacement["goal"] = "Replacement goal"
        replacement["tasks"][0]["description"] = "Refined implementation requirement"
        replacement["final_history"] = [
            {"status": "verification_failed", "artifact": "old-final.json", "verification_failures": []},
            {"status": "running", "artifact": "active-final.json"},
        ]
        engine.run = replacement
        second = builder.final_review_packet(checks=[], diff_artifact="second.diff", changed_paths=["AGENTS.md"])
        self.assertEqual(first["goal"], "Initial goal")
        self.assertEqual(first["prior_reviews"], [])
        self.assertEqual(second["goal"], "Replacement goal")
        self.assertEqual(second["tasks_delta"]["updates"][0]["description"], replacement["tasks"][0]["description"])
        self.assertEqual([row["artifact"] for row in second["prior_reviews"]], ["old-final.json"])
        self.assertEqual(second["instruction_file_changes"]["paths"], ["AGENTS.md"])
        self.assertEqual(first["instruction_file_changes"]["paths"], [])
