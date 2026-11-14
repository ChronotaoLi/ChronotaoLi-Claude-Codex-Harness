import copy
import json
import unittest
from pathlib import Path

from harness.contracts import apply_revision, ScopeExtensionRequired, validate_revision
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase, crash_after_save, decide_bound
from test_runtime_changes import ScriptedBackend


def expanded_revision(original, scope="support.txt"):
    prerequisite = copy.deepcopy(original)
    prerequisite.update(id="prepare", title="Prepare prerequisite", allowed_paths=["hello.py", scope])
    return {"action": "revise", "reason": "Add the missing prerequisite",
            "new_tasks": [prerequisite],
            "updates": [{"id": original["id"], "description": original["description"],
                         "add_depends_on": ["prepare"], "add_allowed_paths": [], "add_required_commands": []}]}


class ScopeExtensionContractTests(unittest.TestCase):
    def test_explicit_scope_only_enlarges_new_task_choices(self):
        original = read_json(ROOT / "examples" / "plan.json")["tasks"]
        before = copy.deepcopy(original)
        revision = expanded_revision(original[0])
        with self.assertRaisesRegex(ScopeExtensionRequired, "无法证明"):
            apply_revision(original, revision, set())
        revised = apply_revision(original, revision, set(), scope=["support.txt"])
        self.assertEqual(original, before)
        self.assertEqual(revised[0]["allowed_paths"], original[0]["allowed_paths"])
        self.assertEqual(revised[0]["acceptance"], original[0]["acceptance"])
        self.assertEqual(revised[1]["allowed_paths"], ["hello.py", "support.txt"])
        # An existing task's acceptance never changes; its scope may only grow at the end.
        changed = copy.deepcopy(revised)
        changed[0]["acceptance"].append("support.txt")
        with self.assertRaisesRegex(HarnessError, "既有验收条件"):
            validate_revision(original, changed, set(), scope=["support.txt"])
        changed = copy.deepcopy(revised)
        changed[0]["allowed_paths"] = ["support.txt"]
        with self.assertRaisesRegex(HarnessError, "追加"):
            validate_revision(original, changed, set(), scope=["support.txt"])
        changed = copy.deepcopy(revised)
        changed[0]["allowed_paths"].append("support.txt")
        self.assertEqual(validate_revision(original, changed, set(), scope=["support.txt"])[0]["allowed_paths"],
                         ["hello.py", "support.txt"])
        # A literal subtree covers what is under it, by path segments only.
        self.assertEqual(apply_revision(original, expanded_revision(original[0], "src/config.py"), set(),
                                        scope=["src/**"])[1]["allowed_paths"], ["hello.py", "src/config.py"])
        with self.assertRaises(ScopeExtensionRequired) as caught:
            apply_revision(original, expanded_revision(original[0], "src2/config.py"), set(), scope=["src/**"])
        self.assertEqual(caught.exception.gaps, {"prepare": ["src2/config.py"]})


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
        state_path = engine.directory / "state.json"
        before, events_before = state_path.read_bytes(), self.events(engine)
        calls = copy.deepcopy(backend.calls)
        for pattern in (".git/config", ".harness/**", ".codex/**", ".claude/**", "../outside",
                        "/tmp/outside", "src/../outside", "src//bad", "bad\0path", "", None, "src/*.py", "**/x"):
            with self.subTest(pattern=pattern), self.assertRaises(HarnessError) as caught:
                self.engine(backend).resume(engine.run["id"], scope=["support.txt", pattern])
            self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]),
                             ("invalid_input", "scope_pattern"))
            self.assertEqual((state_path.read_bytes(), self.events(engine)), (before, events_before))
        # A valid batch is saved once with one audit event; declaring it again changes nothing.
        for _ in range(2):
            with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
                self.engine(backend).resume(engine.run["id"], scope=["support.txt", "support.txt"])
        saved = read_json(state_path)
        self.assertEqual(saved["scope"], ["support.txt"])
        self.assertEqual(len([e for e in self.events(engine) if e["event"] == "scope_extended"]), 1)
        # The boundary answers no question: still waiting, no model call.
        self.assertEqual(saved["decision_requests"], read_json(state_path)["decision_requests"])
        self.assertEqual(backend.calls, calls)

        def inspect_boundary(backend, kwargs, packet, result):
            if kwargs["role"] == "commander":
                self.assertEqual(packet["scope"], ["support.txt"])

        retry_backend = ScriptedBackend(inspect_boundary)
        request = saved["decision_requests"][-1]
        answer = self.repo / "continue-decision.json"
        write_json(answer, {"schema_version": 1, "request_id": request["id"], "task_id": "greet",
                            "decision": "continue", "summary": "Decide again with the declared scope"})
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            self.engine(retry_backend).resume(engine.run["id"], operator_decision=answer)
        self.assertEqual([call["kind"] for call in retry_backend.calls], ["decide"])
        self.assertNotEqual(read_json(state_path)["decision_requests"][-1]["id"], request["id"])

    def test_scope_requires_an_unfinished_run(self):
        completed = self.engine().start("Implement greeting", self.plan())
        path = self.repo / ".harness" / "runs" / completed["id"] / "state.json"
        raw = path.read_bytes()
        with self.assertRaises(HarnessError) as caught:
            self.engine().resume(completed["id"], scope=["support.txt"])
        self.assertEqual((caught.exception.stop["reason"], caught.exception.stop["data"]["status"]),
                         ("run_state", "completed"))
        self.assertEqual(path.read_bytes(), raw)

        def revise_in_scope(backend, kwargs, packet, result):
            workers = [c for c in backend.calls if c["role"] == "worker"]
            if kwargs["role"] == "worker" and len(workers) == 1:
                result.update(status="blocked", blocker_kind="task", blockers=["Need a clearer brief"])
            if kwargs["role"] == "commander" and (packet["trigger"].get("feedback") or {}).get("worker"):
                original = packet["original_tasks"][0]
                result.update(action="revise", reason="Clarify", selected_task="greet", instruction="Continue",
                              new_tasks=[], updates=[{"id": "greet", "description": "Implement greet precisely",
                                                      "add_depends_on": [], "add_allowed_paths": [],
                                                      "add_required_commands": []}])
        backend = ScriptedBackend(revise_in_scope)
        stopped = self.engine(backend)
        # The controller dies right after the returned revision is bound, before its judgment.
        crash, state = crash_after_save(stopped, decide_bound)
        with crash, self.assertRaises(KeyboardInterrupt):
            stopped.start("Implement greeting", self.plan())
        self.assertTrue(state["fired"])
        bound = read_json(stopped.directory / "state.json")["commander_receipts"]["commander:decide:run"]["dispatch_id"]
        calls = len([c for c in backend.calls if c["role"] == "commander"])
        # A returned decision does not block widening the boundary; the decision then lands as it is.
        run = self.engine(backend).resume(stopped.run["id"], scope=["support.txt"])
        self.assertEqual((run["status"], run["scope"]), ("completed", ["support.txt"]))
        self.assertEqual(read_json(Path(run["plan_history"][1]["artifact"]))["source"]["dispatch_id"], bound)
        self.assertEqual(len([c for c in backend.calls if c["role"] == "commander"]), calls)

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
            self, ["resume", engine.run["id"], "--scope", "support.txt"], backend)
        self.assertEqual(code, 3)
        self.assertIn("等待操作者决定", error)
        self.assertEqual(engines[0].run["scope"], ["support.txt"])
        self.assertEqual(backend.calls, calls)

if __name__ == "__main__":
    unittest.main()
