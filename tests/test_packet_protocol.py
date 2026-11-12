"""Role packets carry data, indexes and live workspace facts; protocol text lives in shared assets.

Offline lifecycle checks with ScriptedBackend; they do not prove live model calls.
"""
import copy
import hashlib
from pathlib import Path

from harness import git
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend

REMOVED_TEXT_KEYS = {"environment_update_policy", "operator_decision_policy", "checkpoint_policy",
                     "permission_guidance"}
ENVIRONMENT_LISTS = {"environment_updates", "environment_observations", "environment_history",
                     "environment_check_history"}


def keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys(item)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PacketProtocolTests(RepoCase):
    def plan(self, unrelated=False):
        plan = read_json(ROOT / "examples" / "plan.json")
        if unrelated:
            task = copy.deepcopy(plan["tasks"][0])
            task.update(id="other", title="Independent work", depends_on=[])
            plan["tasks"].append(task)
        return plan

    def update(self, engine, task_id="greet"):
        source = next(d["id"] for d in engine.run["dispatches"]
                      if d["role"] == "worker" and d["task_id"] == task_id)
        path = self.repo / "environment-update.json"
        write_json(path, {"schema_version": 1, "task_id": task_id, "blocker_dispatch_id": source,
                          "summary": "Operator restored the service", "checks": []})
        return path

    def assert_data_only(self, calls):
        self.assertTrue(calls)
        for call in calls:
            with self.subTest(role=call["role"], kind=call["kind"], task=call["task_id"]):
                self.assertFalse(REMOVED_TEXT_KEYS & set(keys(call["packet"])))
                if call["role"] == "reviewer":
                    # Review protocol lives in the reviewer skill; the packet keeps evidence only.
                    self.assertNotIn("instruction", call["packet"])
                    self.assertNotIn("instruction", call["packet"]["verification_plan"])
                    self.assertEqual(set(call["packet"]["instruction_file_changes"]), {"paths", "trust"})

    def test_commander_workspace_matches_ledger_head_and_git_status(self):
        evidence = self.repo.resolve() / "check-output.bin"
        evidence.write_bytes(b"raw check output\n")
        observed = []

        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "commander":
                # The same instant the provider would see: ledger HEAD, Git HEAD and status.
                observed.append((packet["trigger"]["kind"], packet["workspace"], read_json(kwargs["run_path"])["expected_head"],
                                 git.head(kwargs["cwd"]), git.git(kwargs["cwd"], "status", "--porcelain")))
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="environment", blockers=["Service unavailable"],
                              checkpoints=[{"id": "local", "summary": "Local part finished",
                                            "inputs": [{"path": "hello.py", "sha256": digest(kwargs["cwd"] / "hello.py")}],
                                            "evidence": [{"path": str(evidence), "sha256": digest(evidence)}],
                                            "freshness": "content", "max_age_seconds": 0}])

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet")  # No supplied plan: the commander plans.
        result = self.engine(backend).resume(engine.run["id"], environment_update=self.update(engine))
        self.assertEqual(result["status"], "completed")
        self.assertEqual([kind for kind, *_ in observed], ["plan", "task"])
        for kind, workspace, ledger_head, git_head, status in observed:
            with self.subTest(kind=kind):
                self.assertEqual(set(workspace), {"head", "clean"})
                self.assertEqual(workspace["head"], ledger_head)
                self.assertEqual(workspace["head"], git_head)
                self.assertIs(workspace["clean"], status == "")
        # Planning saw a clean tree; the retained blocked candidate makes the later decision dirty.
        self.assertEqual([workspace["clean"] for _, workspace, *_ in observed], [True, False])
        self.assertFalse(any("workspace" in call["packet"] for call in backend.calls if call["role"] != "commander"))
        worker = [call["packet"] for call in backend.calls if call["role"] == "worker"][1]
        self.assertTrue({"environment_updates", "environment_index", "task_checkpoints", "checkpoint_index"} <= set(worker))
        self.assert_data_only(backend.calls)

    def test_decide_packet_carries_workspace_saved_in_its_commander_receipt(self):
        observed = []

        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "commander":
                state = read_json(kwargs["run_path"])
                receipt = state["commander_receipts"]["commander:decide:run"]
                observed.append((packet["trigger"]["kind"], packet["workspace"], receipt["request"]["packet"]["workspace"],
                                 state["expected_head"], git.git(kwargs["cwd"], "status", "--porcelain")))
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="task", blockers=["Missing prerequisite"])

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())  # The demo commander asks and stops the run.
        self.assertEqual([kind for kind, *_ in observed], ["task"])
        _, workspace, saved, ledger_head, status = observed[0]
        self.assertEqual(workspace, saved)
        self.assertEqual(workspace["head"], ledger_head)
        # The blocked candidate is retained for the decision, so the tree is dirty.
        self.assertIs(workspace["clean"], status == "")
        self.assertIs(workspace["clean"], False)

    def test_task_without_environment_update_receives_only_the_index(self):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and kwargs["task"]["id"] == "greet" and not any(
                    c["role"] == "worker" and c["task_id"] == "greet" for c in backend.calls[:-1]):
                result.update(status="blocked", blocker_kind="environment", blockers=["Service unavailable"])

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement both", self.plan(unrelated=True))
        result = self.engine(backend).resume(engine.run["id"], environment_update=self.update(engine))
        self.assertEqual(result["status"], "completed")
        other = [call for call in backend.calls if call["task_id"] == "other"]
        self.assertEqual({call["role"] for call in other}, {"worker", "reviewer"})
        for call in other:
            with self.subTest(role=call["role"]):
                self.assertIn("environment_index", call["packet"])
                self.assertFalse(ENVIRONMENT_LISTS & set(call["packet"]))
        greet = [call for call in backend.calls if call["role"] == "worker" and call["task_id"] == "greet"][-1]
        self.assertEqual(greet["packet"]["environment_updates"][0]["task_id"], "greet")
        self.assert_data_only(backend.calls)

    def test_operator_decision_packets_carry_answers_and_index_only(self):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="operator", blockers=["Choose whether to continue"])

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        request = read_json(engine.directory / "state.json")["decision_requests"][0]
        answer = self.repo / "decision.json"
        write_json(answer, {"schema_version": 1, "request_id": request["id"], "task_id": request["task_id"],
                            "decision": "continue", "summary": "Continue the original task"})
        result = self.engine(backend).resume(engine.run["id"], operator_decision=[answer])
        self.assertEqual(result["status"], "completed")
        worker = [call["packet"] for call in backend.calls if call["role"] == "worker"][-1]
        self.assertEqual(worker["operator_decisions"][0]["response"]["decision"], "continue")
        self.assertIn("operator_decision_index", worker)
        self.assert_data_only(backend.calls)
