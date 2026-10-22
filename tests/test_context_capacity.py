import contextlib
import copy
import io
import json
import unittest
from pathlib import Path

from harness.context import compact_plan
from harness.contracts import apply_revision
from harness.util import HarnessError, dump, read_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend, finding


def reconstruct(packet):
    tasks = copy.deepcopy(packet[packet.get("tasks_ref", packet.get("tasks_base_ref"))])
    by_id = {task["id"]: task for task in tasks}
    for update in packet.get("tasks_delta", {}).get("updates", []):
        if "description" in update:
            by_id[update["id"]]["description"] = update["description"]
        by_id[update["id"]]["depends_on"].extend(update.get("add_depends_on", []))
    tasks.extend(copy.deepcopy(packet.get("tasks_delta", {}).get("new_tasks", [])))
    return tasks


class PlanCompactionTests(unittest.TestCase):
    def test_consolidated_delta_reconstructs_multiple_revisions_without_repeating_obligations(self):
        original = read_json(ROOT / "examples" / "plan.json")["tasks"]
        first = copy.deepcopy(original[0])
        first.update(id="prepare", title="Prepare", description="First prerequisite")
        current = apply_revision(original, {
            "action": "revise", "reason": "Need prerequisite", "new_tasks": [first],
            "updates": [{"id": "greet", "description": "Implement after preparation",
                         "add_depends_on": ["prepare"]}],
        }, set())
        second = copy.deepcopy(first)
        second.update(id="document", description="Document integration", depends_on=["greet"])
        current = apply_revision(current, {
            "action": "revise", "reason": "Clarify implementation", "new_tasks": [second],
            "updates": [{"id": "greet", "description": "Implement the complete contract after preparation",
                         "add_depends_on": []},
                        {"id": "prepare", "description": "Clarified prerequisite", "add_depends_on": []}],
        }, set())
        packet = compact_plan(original, current)
        self.assertEqual(reconstruct(packet), current)
        self.assertNotIn("tasks", packet)
        self.assertEqual(packet["original_tasks"], original)
        self.assertEqual([task["id"] for task in packet["tasks_delta"]["new_tasks"]], ["prepare", "document"])
        self.assertEqual(len(packet["tasks_delta"]["updates"]), 1)
        self.assertNotIn("acceptance", packet["tasks_delta"]["updates"][0])
        self.assertNotIn("allowed_paths", packet["tasks_delta"]["updates"][0])
        current[0]["acceptance"].append("A later mutation must not change the packet")
        self.assertEqual(packet["original_tasks"], original)

    def test_dependency_only_update_does_not_duplicate_long_description(self):
        original = read_json(ROOT / "examples" / "plan.json")["tasks"]
        original[0]["description"] = "long original requirement " * 100
        new = copy.deepcopy(original[0])
        new.update(id="prepare", description="Prepare")
        current = copy.deepcopy(original) + [new]
        current[0]["depends_on"].append("prepare")
        packet = compact_plan(original, current)
        self.assertEqual(reconstruct(packet), current)
        self.assertNotIn("description", packet["tasks_delta"]["updates"][0])
        self.assertEqual(dump(packet).count(original[0]["description"]), 1)

    def test_changed_original_obligation_cannot_be_silently_dropped(self):
        original = read_json(ROOT / "examples" / "plan.json")["tasks"]
        changed = copy.deepcopy(original)
        changed[0]["acceptance"] = ["weaker"]
        with self.assertRaises(HarnessError):
            compact_plan(original, changed)


class FinalContextCapacityTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def test_twenty_long_tasks_project_overflow_before_workers_run(self):
        tasks = []
        for index in range(20):
            task = copy.deepcopy(self.plan()["tasks"][0])
            task.update(id=f"task{index}", title=f"Task {index}", description="x" * 400)
            tasks.append(task)
        backend = ScriptedBackend()
        engine = self.engine(backend)
        engine.start("Implement greeting throughout the project", {"tasks": tasks}, pause_after_plan=True)
        report = engine.final_context_capacity()
        self.assertEqual(report["status"], "projected_overflow")
        self.assertLess(report["required_chars"], report["budget_chars"])
        self.assertGreater(report["projected_chars"], report["budget_chars"])
        self.assertEqual(backend.calls, [])
        self.assertEqual(engine.run["total_attempts"], 0)
        self.assertEqual(engine.run["accepted"], {})
        self.config.data["runtime"]["context_chars"] = report["projected_chars"]
        self.assertEqual(engine.final_context_capacity()["status"], "fits")

    def test_required_overflow_saves_plan_before_workers_and_resumes_with_larger_budget(self):
        plan = self.plan()
        plan["tasks"][0]["description"] = "Detailed acceptance context " * 1500
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greeting", plan)
        saved = read_json(engine.directory / "state.json")
        report = saved["context_capacity"]
        self.assertEqual(report["status"], "required_overflow")
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(saved["tasks"], plan["tasks"])
        self.assertEqual(saved["original_tasks"], plan["tasks"])
        self.assertEqual(len(saved["plan_history"]), 1)
        self.assertEqual(saved["total_attempts"], 0)
        self.assertEqual(backend.calls, [])
        self.config.data["runtime"]["context_chars"] = report["projected_chars"] + 20000
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["plan_history"]), 1)
        self.assertEqual(result["total_attempts"], 1)
        self.assertFalse(any(call["role"] == "commander" for call in backend.calls))

    def test_projected_overflow_warns_before_worker_but_allows_task_acceptance(self):
        tasks = []
        for index in range(20):
            task = copy.deepcopy(self.plan()["tasks"][0])
            task.update(id=f"task{index}", title=f"Task {index}", description="x" * 400)
            tasks.append(task)
        backend = ScriptedBackend()
        engine = self.engine(backend)
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = engine.start("Implement greeting throughout the project", {"tasks": tasks}, step=True)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["total_attempts"], 1)
        self.assertEqual(set(result["accepted"]), {"task0"})
        report = result["context_capacity"]
        self.assertEqual(report["status"], "projected_overflow")
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        preflight_index = next(i for i, event in enumerate(events) if event["event"] == "final_context_preflight")
        worker_index = next(i for i, event in enumerate(events)
                            if event["event"] == "dispatch_started" and event["role"] == "worker")
        self.assertLess(preflight_index, worker_index)
        before_worker = output.getvalue().split("[harness] dispatch_started task0")[0]
        self.assertIn(str(report["projected_chars"]), before_worker)
        self.assertIn(str(report["budget_chars"]), before_worker)

    def test_initial_candidate_defaults_to_its_own_original_obligations(self):
        engine = self.engine()
        engine.start("Implement greeting", self.plan(), pause_after_plan=True)
        tasks = copy.deepcopy(engine.run["tasks"])
        engine.run.update(tasks=[], original_tasks=[])
        report = engine.final_context_capacity(tasks)
        explicit = engine.final_context_capacity(tasks, tasks)
        self.assertEqual(report, explicit)
        self.config.data["runtime"]["context_chars"] = report["required_chars"] - 1
        self.assertEqual(engine.final_context_capacity(tasks)["status"], "required_overflow")
        self.assertEqual(engine.run["dispatches"], [])

    def test_projection_includes_verification_commands_and_bounded_output(self):
        engine = self.engine()
        engine.start("Implement greeting", self.plan(), pause_after_plan=True)
        first = engine.final_context_capacity()
        self.config.data["verification"]["commands"].append(["python3", "check.py", "x" * 1000])
        second = engine.final_context_capacity()
        self.assertGreater(second["required_chars"], first["required_chars"] + 1000)
        self.assertGreater(second["projected_chars"] - first["projected_chars"],
                           second["required_chars"] - first["required_chars"] + 500)
        self.assertEqual(second["assumptions"]["verification_commands"], 2)

    def test_accepted_context_refers_to_immutable_review_evidence(self):
        def long_review(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                result["summary"] = "independently reviewed " * 50
        backend = ScriptedBackend(long_review)
        engine = self.engine(backend)
        result = engine.start("Implement greeting", self.plan())
        packet = next(call["packet"] for call in backend.calls if call["kind"] == "final_review")
        accepted = packet["accepted"]["greet"]
        self.assertEqual(len(accepted["summary"]), 600)
        self.assertTrue(accepted["summary_truncated"])
        self.assertEqual(accepted["details"], result["accepted"]["greet"]["review_artifact"])
        evidence = read_json(Path(accepted["details"]))
        self.assertEqual(evidence["review"]["summary"], result["accepted"]["greet"]["summary"])
        self.assertNotIn(str(engine.directory / "state.json"), dump(packet["accepted"]))

    def test_final_review_after_replan_keeps_original_obligations_only_once(self):
        final_inputs = []
        def corrective(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                final_inputs.append(json.loads(kwargs["prompt"].split("\n\nREFERENCE MEMORY")[0]))
            if kwargs["kind"] == "final_review" and sum(c["kind"] == "final_review" for c in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding()])
            elif kwargs["kind"] == "replan":
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id="correct", title="Correct final issue", description="Resolve the final finding",
                            depends_on=["greet"])
                result.update(action="revise", reason="Resolve finding", new_tasks=[task], updates=[])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                with (kwargs["cwd"] / "hello.py").open("a") as file:
                    file.write("# corrected integration\n")
        backend = ScriptedBackend(corrective)
        engine = self.engine(backend)
        plan = self.plan()
        plan["tasks"][0]["description"] = "Original detailed requirement " * 450
        result = engine.start("Implement greeting", plan)
        packet = [call["packet"] for call in backend.calls if call["kind"] == "final_review"][-1]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(reconstruct(packet), result["tasks"])
        self.assertNotIn("tasks", packet)
        self.assertEqual(packet["tasks_delta"]["updates"], [])
        self.assertEqual(packet["original_tasks"], plan["tasks"])
        final = final_inputs[-1]
        self.assertLessEqual(len(dump(final)), self.config.data["runtime"]["context_chars"])
        duplicated = copy.deepcopy(final)
        duplicated["packet"]["tasks"] = reconstruct(packet)
        for key in ("tasks_base_ref", "tasks_delta", "tasks_reconstruction"):
            duplicated["packet"].pop(key)
        self.assertGreater(len(dump(duplicated)), self.config.data["runtime"]["context_chars"])


if __name__ == "__main__":
    unittest.main()
