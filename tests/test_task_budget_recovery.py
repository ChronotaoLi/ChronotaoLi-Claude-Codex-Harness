import copy
import json
from pathlib import Path

from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class TaskBudgetRecoveryTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    @staticmethod
    def revision(original):
        prerequisite = copy.deepcopy(original)
        prerequisite.update(id="prepare", title="Prepare prerequisite")
        return {"action": "revise", "reason": "Add the missing prerequisite",
                "new_tasks": [prerequisite],
                "updates": [{"id": original["id"], "description": original["description"],
                             "add_depends_on": ["prepare"]}]}

    @staticmethod
    def block_first_worker(backend, kwargs, result):
        if kwargs["role"] == "worker" and sum(call["role"] == "worker" for call in backend.calls) == 1:
            result.update(status="blocked", blocker_kind="task", blockers=["Need a prerequisite"])

    def assert_budget_audit(self, engine):
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        relaxed = [event for event in events if event["event"] == "resume_config_relaxed"]
        self.assertEqual([event["changes"] for event in relaxed], [{"max_tasks": {"old": 1, "new": 2}}])
        self.assertEqual(engine.run["config_snapshot"]["runtime"]["max_tasks"], 2)

    def test_increase_revalidates_existing_overlimit_revision_without_new_dispatch(self):
        self.config.data["runtime"]["max_tasks"] = 1

        def revise(backend, kwargs, packet, result):
            self.block_first_worker(backend, kwargs, result)
            if kwargs["kind"] == "replan":
                self.assertEqual(packet["max_tasks"], 1)
                result.update(self.revision(packet["original_tasks"][0]))

        backend = ScriptedBackend(revise)
        engine = self.engine(backend)
        original_plan = self.plan()
        with self.assertRaisesRegex(HarnessError, "max_tasks"):
            engine.start("Implement greeting with prerequisite", original_plan)
        receipt = copy.deepcopy(engine.run["commander_receipts"]["commander:replan:replanning"])
        artifact = Path(receipt["artifact"])
        original_result = artifact.read_bytes()
        calls = copy.deepcopy(backend.calls)
        self.assertEqual(engine.run["replan_attempts"], 0)
        with self.assertRaisesRegex(HarnessError, "max_tasks"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)

        self.config.data["runtime"]["max_tasks"] = 2
        resumed = self.engine(backend)
        result = resumed.resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["original_tasks"], original_plan["tasks"])
        self.assertEqual(result["tasks"][0]["allowed_paths"], original_plan["tasks"][0]["allowed_paths"])
        self.assertEqual(result["tasks"][0]["acceptance"], original_plan["tasks"][0]["acceptance"])
        self.assertEqual(result["attempts"], {"greet": 2, "prepare": 1})
        self.assertEqual(result["replan_attempts"], 1)
        replans = [entry for entry in result["dispatches"] if entry.get("kind") == "replan"]
        self.assertEqual([entry["id"] for entry in replans], [receipt["dispatch_id"]])
        self.assertEqual(sum(call["kind"] == "replan" for call in backend.calls), 1)
        self.assertEqual(artifact.read_bytes(), original_result)
        self.assert_budget_audit(resumed)

    def test_increase_preserves_stop_until_explicit_retry_with_fresh_dispatch(self):
        self.config.data["runtime"]["max_tasks"] = 1

        def stop_then_revise(backend, kwargs, packet, result):
            self.block_first_worker(backend, kwargs, result)
            if kwargs["kind"] == "replan" and sum(call["kind"] == "replan" for call in backend.calls) == 2:
                self.assertEqual(packet["max_tasks"], 2)
                result.update(self.revision(packet["original_tasks"][0]))

        backend = ScriptedBackend(stop_then_revise)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            engine.start("Implement greeting with prerequisite", self.plan())
        stopped = copy.deepcopy(engine.run["replan_stopped"])
        artifact = Path(stopped["artifact"])
        original_result = artifact.read_bytes()
        calls = copy.deepcopy(backend.calls)

        self.config.data["runtime"]["max_tasks"] = 2
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            resumed.resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)
        self.assertEqual(resumed.run["replan_stopped"], stopped)
        self.assertEqual(resumed.run["replan_attempts"], 1)
        self.assert_budget_audit(resumed)

        result = self.engine(backend).resume(engine.run["id"], retry_stopped="greet")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["replan_attempts"], 2)
        self.assertEqual(result["attempts"], {"greet": 2, "prepare": 1})
        replans = [entry for entry in result["dispatches"] if entry.get("kind") == "replan"]
        self.assertEqual(len(replans), 2)
        self.assertEqual(len({entry["id"] for entry in replans}), 2)
        self.assertEqual(artifact.read_bytes(), original_result)
