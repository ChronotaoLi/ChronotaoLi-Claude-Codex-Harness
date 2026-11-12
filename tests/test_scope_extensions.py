import copy
import json
import unittest
from unittest.mock import patch

from harness.contracts import apply_revision, validate_revision
from harness.util import HarnessError, read_json, write_json
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

    def test_invalid_batch_is_atomic_and_scope_approval_does_not_answer_the_question(self):
        backend = ScriptedBackend(self.block_worker)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            engine.start("Implement greeting", self.plan())
        before = copy.deepcopy(engine.run)
        events_before = self.events(engine)
        for pattern in (".git/config", ".harness/**", ".codex/**", ".claude/**", "../outside",
                        "/tmp/outside", "src/../outside", "src//bad", "bad\0path", "", None):
            with self.subTest(pattern=pattern), self.assertRaises(HarnessError):
                engine.services.planning.approve_scope_extensions(["support.txt", pattern])
            self.assertEqual(engine.run, before)
            self.assertEqual(self.events(engine), events_before)
        engine.services.planning.approve_scope_extensions(["support.txt"])
        engine.services.planning.approve_scope_extensions(["support.txt", "support.txt"])
        self.assertEqual(engine.run["decision_requests"], before["decision_requests"])
        self.assertEqual(len([e for e in self.events(engine) if e["event"] == "scope_extension_approved"]), 1)
        calls = copy.deepcopy(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            resumed.resume(engine.run["id"])
        self.assertEqual(backend.calls, calls)
        self.assertEqual(resumed.run["approved_scope"], ["support.txt"])

        def inspect_authority(backend, kwargs, packet, result):
            if kwargs["role"] == "commander":
                self.assertEqual(packet["approved_scope"], ["support.txt"])

        retry_backend = ScriptedBackend(inspect_authority)
        state_path = engine.directory / "state.json"
        request = read_json(state_path)["decision_requests"][-1]
        answer = self.repo / "continue-decision.json"
        write_json(answer, {"schema_version": 1, "request_id": request["id"], "task_id": "greet",
                            "decision": "continue", "summary": "Decide again with the approved scope"})
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            self.engine(retry_backend).resume(engine.run["id"], operator_decision=answer)
        self.assertEqual([call["kind"] for call in retry_backend.calls], ["decide"])
        self.assertNotEqual(read_json(state_path)["decision_requests"][-1]["id"], request["id"])

    def test_scope_approval_requires_an_open_run_without_a_returned_decision(self):
        engine = self.engine()
        engine.start("Implement greeting", self.plan(), pause_after_plan=True)
        engine.services.planning.approve_scope_extensions(["support.txt"])
        self.assertEqual(engine.run["approved_scope"], ["support.txt"])
        completed = self.engine().start("Implement greeting", self.plan())
        done = self.engine()
        done.load_run(completed["id"])
        with self.assertRaisesRegex(HarnessError, "仅用于未完成的 run"):
            done.services.planning.approve_scope_extensions(["support.txt"])

        engine = self.engine(ScriptedBackend(self.block_worker))
        emit = engine.hooks.emit

        def fail(event, payload, logs):
            if event == "post_agent" and payload.get("kind") == "decide":
                raise HarnessError("question hook unavailable")
            return emit(event, payload, logs)
        with patch.object(engine.hooks, "emit", side_effect=fail), self.assertRaisesRegex(HarnessError, "question hook"):
            engine.start("Implement greeting", self.plan())
        self.assertIn("commander:decide:run", engine.run["commander_receipts"])
        before = copy.deepcopy(engine.run)
        with self.assertRaisesRegex(HarnessError, "已返回、尚未处理的指挥决定"):
            engine.services.planning.approve_scope_extensions(["support.txt"])
        self.assertEqual(engine.run, before)

    def test_cli_scope_extension_reaches_runtime(self):
        from test_cli_controls import CLIControlsTests

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
        self.assertIn("等待操作者决定", error)
        self.assertEqual(engines[0].run["approved_scope"], ["support.txt"])
        self.assertEqual(backend.calls, calls)

if __name__ == "__main__":
    unittest.main()
