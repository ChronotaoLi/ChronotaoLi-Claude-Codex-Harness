import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.contracts import apply_revision, validate_revision
from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


def expanded_revision(original, scope="support.txt"):
    prerequisite = copy.deepcopy(original)
    prerequisite.update(id="prepare", title="Prepare prerequisite", allowed_paths=["hello.py", scope])
    return {"action": "revise", "reason": "Add the missing prerequisite",
            "new_tasks": [prerequisite],
            "updates": [{"id": original["id"], "description": original["description"],
                         "add_depends_on": ["prepare"]}]}


class ScopeExtensionContractTests(unittest.TestCase):
    def test_explicit_scope_only_enlarges_new_task_choices(self):
        original = read_json(ROOT / "examples" / "plan.json")["tasks"]
        before = copy.deepcopy(original)
        revision = expanded_revision(original[0])
        with self.assertRaisesRegex(HarnessError, "文件范围"):
            apply_revision(original, revision, set())
        revised = apply_revision(original, revision, set(), additional_allowed_paths=["support.txt"])
        self.assertEqual(original, before)
        self.assertEqual(revised[0]["allowed_paths"], original[0]["allowed_paths"])
        self.assertEqual(revised[0]["acceptance"], original[0]["acceptance"])
        self.assertEqual(revised[1]["allowed_paths"], ["hello.py", "support.txt"])
        for field in ("allowed_paths", "acceptance"):
            changed = copy.deepcopy(revised)
            changed[0][field].append("support.txt")
            with self.subTest(field=field), self.assertRaisesRegex(HarnessError, "既有验收条件或文件范围"):
                validate_revision(original, changed, set(), additional_allowed_paths=["support.txt"])
        with self.assertRaisesRegex(HarnessError, "文件范围"):
            apply_revision(original, expanded_revision(original[0], "src/config.py"), set(),
                           additional_allowed_paths=["src/**"])


class ScopeExtensionRecoveryTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def events(self, engine):
        return [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]

    @staticmethod
    def block_worker(backend, kwargs, packet, result):
        if kwargs["role"] == "worker":
            result.update(status="blocked", blocker_kind="task", blockers=["Need a prerequisite"])

    def test_scope_approval_recovers_same_returned_replan_without_redraw(self):
        def revise(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and len([c for c in backend.calls if c["role"] == "worker"]) == 1:
                self.block_worker(backend, kwargs, packet, result)
            elif kwargs["kind"] == "replan":
                self.assertEqual(packet["approved_scope"], [])
                result.update(expanded_revision(packet["original_tasks"][0]))
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "prepare":
                (kwargs["cwd"] / "support.txt").write_text("Required prerequisite\n")

        backend = ScriptedBackend(revise)
        engine = self.engine(backend)
        plan = self.plan()
        with self.assertRaisesRegex(HarnessError, "文件范围"):
            engine.start("Implement greeting with prerequisite", plan)
        receipt = copy.deepcopy(engine.run["commander_receipts"]["commander:replan:replanning"])
        artifact = Path(receipt["artifact"])
        original_result = artifact.read_bytes()
        calls = copy.deepcopy(backend.calls)
        self.assertEqual(engine.run["replan_attempts"], 0)
        with self.assertRaisesRegex(HarnessError, "文件范围"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)

        operator = self.engine(backend)
        operator.load_run(engine.run["id"])
        operator.approve_scope_extensions(["support.txt", "support.txt"])
        operator.approve_scope_extensions(["support.txt"])
        self.assertEqual(operator.run["approved_scope"], ["support.txt"])
        self.assertEqual(operator.run["original_tasks"], plan["tasks"])
        self.assertEqual(operator.run["tasks"], plan["tasks"])
        self.assertEqual(operator.run["commander_receipts"]["commander:replan:replanning"], receipt)
        self.assertEqual(len([e for e in self.events(operator) if e["event"] == "scope_extension_approved"]), 1)

        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["original_tasks"], plan["tasks"])
        self.assertEqual(result["tasks"][0]["allowed_paths"], plan["tasks"][0]["allowed_paths"])
        self.assertEqual(result["tasks"][0]["acceptance"], plan["tasks"][0]["acceptance"])
        self.assertEqual(result["attempts"], {"greet": 2, "prepare": 1})
        self.assertEqual(result["replan_attempts"], 1)
        self.assertEqual(len([call for call in backend.calls if call["kind"] == "replan"]), 1)
        self.assertEqual(artifact.read_bytes(), original_result)
        self.assertEqual((Path(result["worktree"]) / "support.txt").read_text(), "Required prerequisite\n")
        self.assertFalse((self.repo / "support.txt").exists())

    def test_invalid_batch_is_atomic_and_scope_approval_does_not_restart_stop(self):
        backend = ScriptedBackend(self.block_worker)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            engine.start("Implement greeting", self.plan())
        stopped = copy.deepcopy(engine.run["replan_stopped"])
        before = copy.deepcopy(engine.run)
        events_before = self.events(engine)
        for pattern in (".git/config", ".harness/**", ".codex/**", ".claude/**", "../outside",
                        "/tmp/outside", "src/../outside", "src//bad", "bad\0path", "", None):
            with self.subTest(pattern=pattern), self.assertRaises(HarnessError):
                engine.approve_scope_extensions(["support.txt", pattern])
            self.assertEqual(engine.run, before)
            self.assertEqual(self.events(engine), events_before)
        engine.approve_scope_extensions(["support.txt"])
        self.assertEqual(engine.run["replan_stopped"], stopped)
        self.assertNotIn("pending_replan", engine.run)
        calls = copy.deepcopy(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            resumed.resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)
        self.assertEqual(resumed.run["replan_stopped"], stopped)
        self.assertEqual(resumed.run["approved_scope"], ["support.txt"])

        def inspect_authority(backend, kwargs, packet, result):
            if kwargs["kind"] == "replan":
                self.assertEqual(packet["approved_scope"], ["support.txt"])

        retry_backend = ScriptedBackend(inspect_authority)
        with self.assertRaisesRegex(HarnessError, "停止重新规划"):
            self.engine(retry_backend).resume(engine.run["id"], retry_stopped="greet")
        self.assertEqual([call["kind"] for call in retry_backend.calls], ["replan"])

    def test_scope_approval_requires_an_unvalidated_or_stopped_replan(self):
        engine = self.engine()
        engine.start("Implement greeting", self.plan(), pause_after_plan=True)
        with self.assertRaisesRegex(HarnessError, "尚未校验"):
            engine.approve_scope_extensions(["support.txt"])

        def revise(backend, kwargs, packet, result):
            self.block_worker(backend, kwargs, packet, result)
            if kwargs["kind"] == "replan":
                result.update(expanded_revision(packet["original_tasks"][0], "hello.py"))

        engine = self.engine(ScriptedBackend(revise))
        with patch.object(engine, "apply_pending_revision", side_effect=HarnessError("paused before apply")):
            with self.assertRaisesRegex(HarnessError, "paused before apply"):
                engine.start("Implement greeting", self.plan())
        self.assertEqual(engine.run["pending_replan"]["stage"], "validated")
        before = copy.deepcopy(engine.run)
        with self.assertRaisesRegex(HarnessError, "尚未校验"):
            engine.approve_scope_extensions(["support.txt"])
        self.assertEqual(engine.run, before)


if __name__ == "__main__":
    unittest.main()
