"""One rule for invalid model output: vetoes bind, the rest is redone once (spec-B3)."""
import copy
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.adapters import CLIBackend, DemoBackend, UndecodableOutput
from harness.dispatch import Dispatcher
from harness.memory import Memory
from harness.util import HarnessError, read_json, write_json
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend, finding, finding_key
from test_worker_return_recovery import stub_provider_clis

REVIEW_KEY = "reviewer:review:greet"


def plan():
    return read_json(ROOT / "examples" / "plan.json")


def calls(backend, role=None, kind=None):
    return [call for call in backend.calls
            if (role is None or call["role"] == role) and (kind is None or call["kind"] == kind)]


def entries(run, role, kind=None):
    return [entry for entry in run["dispatches"] if entry.get("role") == role
            and (kind is None or entry.get("kind") == kind) and entry.get("invoked")]


def capped(limit):
    """Fail loudly instead of looping if a redo is never bounded."""
    def guard(backend, kwargs, packet):
        if len(backend.calls) > limit:
            raise AssertionError("unbounded model calls")
    return guard


def bounded_judgments(limit):
    """Fail loudly if one returned review is judged again and again without a new call."""
    from harness import receipts
    original, seen = receipts.judge_review, []

    def judge(*args, **kwargs):
        seen.append(1)
        if len(seen) > limit:
            raise AssertionError("unbounded review judgments")
        return original(*args, **kwargs)
    return patch.object(receipts, "judge_review", side_effect=judge)


class ReviewRedoTests(RepoCase):
    def test_invalid_approval_is_redone_once_by_a_fresh_dispatch(self):
        seen = []

        def change(backend, kwargs, packet, result):
            if kwargs["kind"] != "review":
                return
            reviews = calls(backend, "reviewer", "review")
            if len(reviews) == 1:
                result["summary"] = "x" * 4001
            else:
                first = entries(engine.run, "reviewer", "review")[0]
                seen.append((first["status"], dict(engine.run["review_receipts"][REVIEW_KEY]), packet["previous_invalid"]))

        backend = RecordingBackend(change)
        engine = self.engine(backend)
        with bounded_judgments(10):
            run = engine.start("Implement greet", plan())
        self.assertEqual(run["status"], "completed")
        first, second = entries(run, "reviewer", "review")
        self.assertEqual((first["status"], second["status"]), ("invalid", "completed"))
        self.assertEqual(second["retry_of"], first["id"])
        self.assertNotIn("retry_of", first)
        requests = [request for request in backend.requests if request["kind"] == "review"]
        self.assertNotEqual(requests[0]["dispatch_id"], requests[1]["dispatch_id"])
        self.assertNotEqual(requests[0]["job"], requests[1]["job"])
        self.assertTrue(all(request["model"] == self.config.role("reviewer", run["commander"]) for request in requests))
        # One worker, two task reviews, one final review: the redo is billed.
        self.assertEqual(run["agent_dispatches"], 4)
        status, receipt, previous = seen[0]
        self.assertEqual(status, "invalid")
        self.assertEqual(receipt["dispatch_id"], second["id"])  # the first receipt was consumed
        original = Path(first["invalid_output"]["artifact"])
        self.assertEqual(previous["dispatch_id"], first["id"])
        self.assertEqual(previous["artifact_digest"], hashlib.sha256(original.read_bytes()).hexdigest())
        self.assertEqual(first["invalid_output"]["artifact_digest"], previous["artifact_digest"])
        self.assertLessEqual(len(previous["error"]), 2000)
        self.assertTrue(original.is_file())

    def test_two_invalid_outputs_stop_and_resume_is_a_new_request(self):
        state = {"bad": 2}

        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and state["bad"]:
                state["bad"] -= 1
                result["summary"] = "x" * 4001

        backend = ScriptedBackend(change)
        backend.before_result = capped(12)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "连续两次无效") as caught:
            engine.start("Implement greet", plan())
        first, second = entries(engine.run, "reviewer", "review")
        self.assertIn(second["invalid_output"]["artifact"], str(caught.exception))
        self.assertNotIn("pending_acceptance", engine.run)
        self.assertEqual(engine.run["pending_task"]["stage"], "reviewing")
        self.assertNotIn(REVIEW_KEY, engine.run["review_receipts"])
        # The resumed call is a new request: no retry_of, the last invalid output as hint.
        state["bad"] = 1
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "连续两次无效"):
            state["bad"] = 2
            resumed.resume(engine.run["id"])
        reviews = entries(resumed.run, "reviewer", "review")
        self.assertEqual(len(reviews), 4)
        self.assertNotIn("retry_of", reviews[2])
        self.assertEqual(reviews[3]["retry_of"], reviews[2]["id"])
        third_packet = calls(backend, "reviewer", "review")[2]["packet"]
        self.assertEqual(third_packet["previous_invalid"]["dispatch_id"], reviews[1]["id"])
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        reviews = entries(result, "reviewer", "review")
        self.assertEqual(len(reviews), 5)
        self.assertNotIn("retry_of", reviews[4])

    def test_invalid_rejection_reworks_without_redo(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(calls(backend, "reviewer", "review")) == 1:
                result.update(verdict="request_changes", findings=[finding()], summary="x" * 4001)

        backend = ScriptedBackend(change)
        run = self.engine(backend).start("Implement greet", plan())
        self.assertEqual(run["status"], "completed")
        first, second = entries(run, "reviewer", "review")
        self.assertEqual(first["status"], "invalid_rejected")
        self.assertNotIn("retry_of", second)
        self.assertEqual(run["attempts"], {"greet": 2})
        record = run["review_history"]["greet"][0]
        self.assertEqual(record["status"], "invalid_rejected")
        rework = calls(backend, "worker")[1]["packet"]["required_findings"]
        self.assertEqual([(row["review_dispatch_id"], row["finding_index"]) for row in rework], [(first["id"], 0)])

    def test_structurally_invalid_reviews_are_redone_not_reworked(self):
        cases = {
            "missing summary": lambda result, packet: (result.update(verdict="request_changes", findings=[finding()]),
                                                       result.pop("summary")),
            "omitted dispositions": lambda result, packet: result.pop("finding_dispositions"),
            "extra key": lambda result, packet: result["finding_dispositions"].update(
                f9={"status": "resolved", "evidence": ["x"]}),
        }
        for name, damage in cases.items():
            with self.subTest(case=name):
                def change(backend, kwargs, packet, result, damage=damage, name=name):
                    if kwargs["kind"] != "review":
                        return
                    number = len(calls(backend, "reviewer", "review"))
                    if name == "missing summary":
                        if number == 1:
                            damage(result, packet)
                    elif number == 1:
                        result.update(verdict="request_changes", findings=[finding()])
                    elif number == 2:
                        damage(result, packet)

                backend = ScriptedBackend(change)
                backend.before_result = capped(20)
                run = self.engine(backend).start("Implement greet", plan())
                self.assertEqual(run["status"], "completed")
                reviews = entries(run, "reviewer", "review")
                invalid = [entry for entry in reviews if entry["status"] == "invalid"]
                self.assertEqual(len(invalid), 1)
                redo = reviews[reviews.index(invalid[0]) + 1]
                self.assertEqual(redo["retry_of"], invalid[0]["id"])
                self.assertTrue(Path(invalid[0]["invalid_output"]["artifact"]).is_file())
                history_ids = {record["dispatch_id"] for record in run["review_history"]["greet"]}
                self.assertNotIn(invalid[0]["id"], history_ids)

    def test_unresolved_required_finding_in_invalid_approval_is_a_veto(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] != "review":
                return
            number = len(calls(backend, "reviewer", "review"))
            if number == 1:
                result.update(verdict="request_changes", findings=[finding()])
            elif number == 2:
                key = finding_key(packet, "edge-case")
                result["finding_dispositions"][key] = {"status": "open", "evidence": []}
                result["summary"] = "x" * 4001

        backend = ScriptedBackend(change)
        run = self.engine(backend).start("Implement greet", plan())
        self.assertEqual(run["status"], "completed")
        second = entries(run, "reviewer", "review")[1]
        self.assertEqual(second["status"], "invalid_rejected")
        record = run["review_history"]["greet"][1]
        self.assertEqual(record["status"], "invalid_rejected")
        self.assertEqual(record["disposition_errors"][0]["key"], "f1")
        self.assertEqual(run["attempts"], {"greet": 3})


class FinalReviewRedoTests(RepoCase):
    def test_invalid_final_rejection_keeps_the_snapshot(self):
        self.config.data["runtime"]["max_replans"] = 0

        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result.update(verdict="request_changes", findings=[finding()], summary="x" * 4001)

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "重新规划次数"):
            engine.start("Implement greet", plan())
        record = engine.run["final_history"][-1]
        self.assertEqual(record["status"], "invalid_rejected")
        before = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(calls(backend, "reviewer", "final_review")), 1)
        self.assertEqual(len(backend.calls), before)

    def test_invalid_final_approval_is_redone_on_the_same_snapshot(self):
        for bad in (1, 2):
            with self.subTest(invalid_outputs=bad):
                state = {"bad": bad}

                def change(backend, kwargs, packet, result):
                    if kwargs["kind"] == "final_review" and state["bad"]:
                        state["bad"] -= 1
                        result["summary"] = "x" * 4001

                backend = ScriptedBackend(change)
                engine = self.engine(backend)
                if bad == 1:
                    run = engine.start("Implement greet", plan())
                else:
                    with self.assertRaisesRegex(HarnessError, "连续两次无效"):
                        engine.start("Implement greet", plan())
                    record = engine.run["final_history"][-1]
                    self.assertEqual((record["status"], record["phase"]), ("running", "reviewing"))
                    run = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(run["status"], "completed")
                self.assertEqual([record["status"] for record in run["final_history"]], ["approved"])
                self.assertEqual(len(calls(backend, "reviewer", "final_review")), bad + 1)


class CommanderRedoTests(RepoCase):
    def test_invalid_plan_is_redone(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "plan" and len(calls(backend, "commander", "plan")) == 1:
                result["tasks"].append(copy.deepcopy(result["tasks"][0]))

        backend = ScriptedBackend(change)
        run = self.engine(backend).start("Implement greet")
        self.assertEqual(run["status"], "completed")
        plans = entries(run, "commander", "plan")
        self.assertEqual([entry["status"] for entry in plans], ["invalid", "completed"])
        self.assertEqual(plans[1]["retry_of"], plans[0]["id"])
        self.assertEqual(len(run["original_tasks"]), 1)

    def test_invalid_selection_is_redone_before_the_worker(self):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and len(calls(backend, "worker")) == 1:
                result.update(status="blocked", blocker_kind="environment", blockers=["Docker stopped"])
            if kwargs["kind"] == "dispatch" and len(calls(backend, "commander", "dispatch")) == 1:
                result["instruction"] = "x" * 4001

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "环境受阻"):
            engine.start("Implement greet", plan())
        update = self.repo / "environment-update.json"
        worker = entries(engine.run, "worker")[0]
        write_json(update, {"schema_version": 1, "task_id": "greet", "blocker_dispatch_id": worker["id"],
                            "summary": "Docker restarted"})
        run = self.engine(backend).resume(engine.run["id"], environment_update=update)
        self.assertEqual(run["status"], "completed")
        selections = entries(run, "commander", "dispatch")
        self.assertEqual([entry["status"] for entry in selections], ["invalid", "completed"])
        self.assertEqual(selections[1]["retry_of"], selections[0]["id"])
        self.assertEqual(len(entries(run, "worker")), 2)


class ReplanStopTests(RepoCase):
    def block_once(self, backend, kwargs, result):
        if kwargs["role"] == "worker" and len(calls(backend, "worker")) == 1:
            result.update(status="blocked", blocker_kind="task", blockers=["Clarify prerequisite"])

    def test_structurally_valid_stop_is_a_veto_even_when_invalid(self):
        for case in ("tasks_with_stop", "long_reason"):
            with self.subTest(case=case):
                def change(backend, kwargs, packet, result, case=case):
                    self.block_once(backend, kwargs, result)
                    if kwargs["kind"] == "replan":
                        if case == "tasks_with_stop":
                            task = copy.deepcopy(packet["original_tasks"][0])
                            task.update(id="extra")
                            result.update(action="stop", reason="Operator must decide", new_tasks=[task], updates=[])
                        else:
                            result.update(action="stop", reason="r" * 4001, new_tasks=[], updates=[])

                backend = ScriptedBackend(change)
                engine = self.engine(backend)
                with self.assertRaisesRegex(HarnessError, "指挥已停止再规划"):
                    engine.start("Implement greet", plan())
                replans = entries(engine.run, "commander", "replan")
                self.assertEqual(len(replans), 1)
                self.assertEqual(replans[0]["status"], "invalid_stopped")
                stopped = engine.run["replan_stopped"]
                self.assertIn("output_error", stopped)
                requests = [r for r in engine.run["decision_requests"] if r["kind"] == "replan_stop"]
                self.assertEqual(len(requests), 1 if case == "tasks_with_stop" else 0)
                before = len(backend.calls)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                self.assertEqual(len(backend.calls), before)

    def test_invalid_revision_is_redone(self):
        def change(backend, kwargs, packet, result):
            self.block_once(backend, kwargs, result)
            if kwargs["kind"] == "replan":
                number = len(calls(backend, "commander", "replan"))
                description = "" if number == 1 else "Implement greet after clarification"
                result.update(action="revise", reason="Clarified", new_tasks=[],
                              updates=[{"id": "greet", "description": description, "add_depends_on": []}])

        backend = ScriptedBackend(change)
        run = self.engine(backend).start("Implement greet", plan())
        self.assertEqual(run["status"], "completed")
        replans = entries(run, "commander", "replan")
        self.assertEqual([entry["status"] for entry in replans], ["invalid", "completed"])
        self.assertEqual(run["replan_attempts"], 1)

    def test_out_of_scope_revision_keeps_its_receipt_for_scope_authority(self):
        def change(backend, kwargs, packet, result):
            self.block_once(backend, kwargs, result)
            if kwargs["kind"] == "replan":
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id="prepare", allowed_paths=["hello.py", "support.txt"])
                result.update(action="revise", reason="Add prerequisite", new_tasks=[task],
                              updates=[{"id": "greet", "description": packet["original_tasks"][0]["description"],
                                        "add_depends_on": ["prepare"]}])

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "文件范围"):
            engine.start("Implement greet", plan())
        receipt = engine.run["commander_receipts"]["commander:replan:replanning"]
        self.assertNotEqual(entries(engine.run, "commander", "replan")[0]["status"], "invalid")
        before = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "文件范围"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), before)
        run = self.engine(backend).resume(engine.run["id"], allow_scope=["support.txt"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(entries(run, "commander", "replan")), 1)
        self.assertEqual(entries(run, "commander", "replan")[0]["id"], receipt["dispatch_id"])


class WorkerInvalidOutputTests(RepoCase):
    def test_invalid_worker_output_spends_one_attempt_and_continues(self):
        for case in ("summary", "extra repair key", "handoff target"):
            with self.subTest(case=case):
                def change(backend, kwargs, packet, result, case=case):
                    if kwargs["role"] != "worker" or len(calls(backend, "worker")) != 1:
                        return
                    if case == "summary":
                        result["summary"] = "x" * 32001
                    elif case == "extra repair key":
                        result["repair_claims"] = {"f9": {"summary": "x", "evidence_paths": []}}
                    else:
                        result["decision_requests"] = [{"id": "risk", "task_id": "greet", "question": "Proceed?"}]

                backend = RecordingBackend(change)
                run = self.engine(backend).start("Implement greet", plan())
                self.assertEqual(run["status"], "completed")
                self.assertEqual(run["attempts"], {"greet": 2})
                first, second = entries(run, "worker")
                self.assertEqual((first["status"], first["worker_receipt_consumed"]), ("invalid", "result_rejected"))
                self.assertIn("invalid_output", first)
                order = [(call["role"], call["kind"]) for call in backend.calls]
                self.assertEqual(order[:3], [("worker", "implement"), ("commander", "dispatch"), ("worker", "implement")])
                workers = [request for request in backend.requests if request["role"] == "worker"]
                self.assertNotEqual(workers[0]["dispatch_id"], workers[1]["dispatch_id"])
                self.assertEqual(workers[1]["model"], self.config.role("worker", run["commander"]))

    def test_always_invalid_worker_stops_at_the_attempt_limit(self):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["summary"] = "x" * 32001

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "尝试次数上限"):
            engine.start("Implement greet", plan())
        self.assertEqual(engine.run["attempts"], {"greet": self.config.data["runtime"]["max_attempts"]})
        self.assertEqual(len(calls(backend, "worker")), self.config.data["runtime"]["max_attempts"])

    def test_invalid_environment_and_operator_blockers_keep_their_route_without_exemption(self):
        for kind in ("environment", "operator"):
            with self.subTest(kind=kind):
                def change(backend, kwargs, packet, result, kind=kind):
                    if kwargs["role"] == "worker":
                        result.update(status="blocked", blocker_kind=kind, blockers=["Need a choice"],
                                      summary="x" * 32001)

                backend = ScriptedBackend(change)
                engine = self.engine(backend)
                with self.assertRaisesRegex(HarnessError, "环境受阻" if kind == "environment" else "等待操作者决定"):
                    engine.start("Implement greet", plan())
                worker = entries(engine.run, "worker")[0]
                self.assertEqual(worker["status"], "invalid_blocked")
                self.assertEqual(worker["worker_receipt_consumed"], "invalid_blocker")
                self.assertNotIn("attempt_exemption", worker)
                feedback = engine.run["feedback"]
                self.assertEqual(feedback["worker"]["blocker_kind"], kind)
                self.assertIn("output_error", feedback)
                if kind == "environment":
                    update = self.repo / "environment-update.json"
                    write_json(update, {"schema_version": 1, "task_id": "greet",
                                        "blocker_dispatch_id": worker["id"], "summary": "Restored"})
                    engine.services.environment.prepare_environment_update(update)
                else:
                    self.assertEqual([r["kind"] for r in engine.run["decision_requests"]], ["worker"])

    def test_task_blockers_and_unreadable_blockers_go_to_replanning(self):
        cases = {
            "task with long blocker": lambda result: result.update(status="blocked", blocker_kind="task",
                                                                   blockers=["b" * 4001]),
            "missing summary": lambda result: (result.update(status="blocked", blocker_kind="operator",
                                                             blockers=["Need a choice"]), result.pop("summary")),
            "kind none": lambda result: result.update(status="blocked", blocker_kind="none", blockers=["x"]),
            "invalid continuation": lambda result: result.update(status="blocked", blocker_kind="continuation",
                                                                 blockers=["more time"]),
        }
        for name, damage in cases.items():
            with self.subTest(case=name):
                def change(backend, kwargs, packet, result, damage=damage):
                    if kwargs["role"] == "worker" and len(calls(backend, "worker")) == 1:
                        damage(result)

                backend = ScriptedBackend(change)
                engine = self.engine(backend)
                with self.assertRaisesRegex(HarnessError, "指挥停止重新规划"):
                    engine.start("Implement greet", plan())
                self.assertEqual(len(calls(backend, "commander", "replan")), 1)
                trigger = engine.run["replan_stopped"]["trigger"]
                self.assertIn("output_error", trigger)
                worker = entries(engine.run, "worker")[0]
                self.assertEqual((worker["status"], worker["worker_outcome"]), ("invalid_blocked", "task_blocked"))
                if name != "task with long blocker":
                    self.assertEqual(set(trigger), {"task_id", "attempt", "snapshot_digest", "worker_artifact",
                                                    "worker_artifact_digest", "output_error"})
                    self.assertFalse([r for r in engine.run["decision_requests"] if r["kind"] == "worker"])
                    from harness.cli import status_summary
                    self.assertNotIn("last_environment_blocker", status_summary(engine.run))


    def test_interrupted_invalid_task_blocker_publication_resumes(self):
        revised = "Implement greet after the prerequisite is clarified"

        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and kwargs["task"]["description"] != revised:
                result.update(status="blocked", blocker_kind="task", blockers=["b" * 4001])
            if kwargs["kind"] == "replan":
                result.update(action="revise", reason="Clarified", new_tasks=[],
                              updates=[{"id": "greet", "description": revised, "add_depends_on": []}])

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        record = engine.services.workflow.record_worker_recovery
        state = {"fired": False}

        def interrupt_once(*args, **kwargs):
            # An interruption inside the Git inspection of the invalid blocker's publication.
            if not state["fired"]:
                state["fired"] = True
                raise KeyboardInterrupt
            return record(*args, **kwargs)
        with patch.object(engine.services.workflow, "record_worker_recovery", side_effect=interrupt_once):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", plan())
        saved = read_json(engine.directory / "state.json")
        self.assertIn("worker:implement:greet", saved["worker_receipts"])
        self.assertNotIn("pending_replan", saved)
        run = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(run["status"], "completed")
        first = entries(run, "worker")[0]
        self.assertEqual((first["status"], first["worker_outcome"]), ("invalid_blocked", "task_blocked"))
        self.assertEqual(len(calls(backend, "commander", "replan")), 1)
        self.assertEqual(len(calls(backend, "worker")), 2)


class UndecodableOutputTests(RepoCase):
    def cli_backend(self, outputs):
        """A real CLIBackend whose CLI is a local process printing the given terminal bytes."""
        config, repo = self.config, self.repo

        class Local(DemoBackend):
            def invoke(self, **kwargs):
                key = kwargs["role"]
                if key not in outputs or not outputs[key]:
                    return super().invoke(**kwargs)
                raw = outputs[key].pop(0)
                cli = CLIBackend(config, repo / ".harness")
                cli.command = lambda *args: [sys.executable, "-c",
                                             f"import sys; sys.stdout.write({raw!r}); sys.stdout.flush()"]
                return cli.invoke(**kwargs)
        return Local()

    TERMINALS = {
        "duplicate key": '{"type":"result","subtype":"success","structured_output":{"status":"blocked","status":"completed"}}',
        "nan": '{"type":"result","subtype":"success","structured_output":{"value":NaN}}',
        "array result": '{"type":"result","subtype":"success","structured_output":[]}',
        "prose result": '{"type":"result","subtype":"success","result":"Done, all good."}',
    }

    def test_worker_undecodable_output_is_an_invalid_delivery_through_the_real_adapter(self):
        from harness.worker_returns import decode_terminal, inspect_return
        stub_provider_clis(self)
        for name, terminal in self.TERMINALS.items():
            with self.subTest(case=name):
                self.setUp()
                stub_provider_clis(self)
                run = self.engine(self.cli_backend({"worker": [terminal]})).start("Implement greet", plan())
                self.assertEqual(run["status"], "completed")
                first = entries(run, "worker")[0]
                evidence = first["worker_return_evidence"]
                self.assertEqual(evidence["state"], "invalid")
                self.assertEqual((first["status"], first["worker_receipt_consumed"]), ("invalid", "result_rejected"))
                job = Path(run["worktree"]).parent.parent / "runs" / run["id"] / "dispatches" / first["id"]
                self.assertFalse((job / "response.json").exists())
                self.assertEqual(decode_terminal((job / "process.stdout").read_bytes())[0], "undecodable")
                self.assertEqual(inspect_return(job)["state"], "invalid")
                self.assertEqual(run["attempts"], {"greet": 2})

    def test_invalid_evidence_must_carry_its_rejection_at_load(self):
        stub_provider_clis(self)
        run = self.engine(self.cli_backend({"worker": [self.TERMINALS["prose result"]]})).start(
            "Implement greet", plan())
        path = Path(run["worktree"]).parent.parent / "runs" / run["id"] / "state.json"
        saved = read_json(path)
        for name, change in (("provider failure", {"worker_receipt_consumed": "provider_failure"}),
                             ("not invalid", {"status": "failed"})):
            with self.subTest(case=name):
                state = copy.deepcopy(saved)
                state["dispatches"][0].update(change)
                state["status"] = "blocked"
                write_json(path, state)
                original = path.read_bytes()
                with self.assertRaisesRegex(HarnessError, "provider failure lacks retry evidence|"
                                                          "invalid-output evidence lacks its rejection"):
                    self.engine(DemoBackend()).resume(run["id"])
                self.assertEqual(path.read_bytes(), original)

    def test_review_undecodable_output_is_redone_through_the_real_adapter(self):
        for name, terminal in self.TERMINALS.items():
            with self.subTest(case=name):
                self.setUp()
                stub_provider_clis(self)
                self.config.data["roles"]["reviewer"]["cli"] = "claude"
                run = self.engine(self.cli_backend({"reviewer": [terminal]})).start("Implement greet", plan())
                self.assertEqual(run["status"], "completed")
                first, second = entries(run, "reviewer", "review")
                self.assertEqual(first["status"], "invalid")
                self.assertTrue(first["invalid_output"]["artifact"].endswith("process.stdout"))
                self.assertEqual(second["retry_of"], first["id"])

    def test_undecodable_output_from_a_test_backend(self):
        class Raising(ScriptedBackend):
            def invoke(self, **kwargs):
                if kwargs["kind"] == "review" and not calls(self, "reviewer", "review"):
                    self.calls.append({"role": "reviewer", "kind": "review", "packet": {}})
                    path = kwargs["job"] / "response.json"
                    path.write_text('{"verdict": "approve", "verdict": "request_changes"}')
                    raise UndecodableOutput(path, "duplicate key")
                return super().invoke(**kwargs)

        run = self.engine(Raising()).start("Implement greet", plan())
        self.assertEqual(run["status"], "completed")
        first, second = entries(run, "reviewer", "review")
        self.assertEqual(first["status"], "invalid")
        self.assertTrue(first["invalid_output"]["artifact"].endswith("response.json"))
        self.assertEqual(second["retry_of"], first["id"])


class EvidenceDamageTests(RepoCase):
    def test_changed_original_stops_without_redo(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(calls(backend, "reviewer", "review")) == 1:
                result["summary"] = "x" * 4001

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        original = engine.services.receipts.finish_review_receipt

        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt
        with patch.object(engine.services.receipts, "finish_review_receipt", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", plan())
        receipt = engine.run["review_receipts"][REVIEW_KEY]
        self.assertIn("artifact_digest", receipt)
        artifact = Path(receipt["artifact"])
        artifact.write_text(json.dumps({**read_json(artifact), "summary": "tampered"}))
        before = len(backend.calls)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "已改变"):
            resumed.resume(engine.run["id"])
        self.assertEqual(len(backend.calls), before)
        self.assertIn(REVIEW_KEY, resumed.run["review_receipts"])
        self.assertNotEqual(entries(resumed.run, "reviewer", "review")[0]["status"], "invalid")

    def test_changed_previous_invalid_original_stops_before_dispatch(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(calls(backend, "reviewer", "review")) == 1:
                result["summary"] = "x" * 4001

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        original = Dispatcher.dispatch_once

        def stop_redo(self_, role, kind, task_id, schema, packet, task=None, **kwargs):
            if role == "reviewer" and kwargs.get("retry_of"):
                raise KeyboardInterrupt
            return original(self_, role, kind, task_id, schema, packet, task, **kwargs)
        with patch.object(Dispatcher, "dispatch_once", stop_redo):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", plan())
        first = entries(engine.run, "reviewer", "review")[0]
        Path(first["invalid_output"]["artifact"]).write_text("{}")
        before = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "无效输出原始结果已改变"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), before)


class OptionalProductTests(RepoCase):
    def checkpoint(self, kwargs, ident):
        source = kwargs["cwd"] / "hello.py"
        digest = hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else "0" * 64
        return {"id": ident, "summary": "Saved check", "inputs": [{"path": "hello.py", "sha256": digest}],
                "evidence": [{"path": "hello.py", "sha256": digest}], "freshness": "content", "max_age_seconds": 0}

    def test_invalid_optional_products_are_discarded_alone(self):
        for case in ("17 checkpoints", "unknown approval", "long proposal"):
            with self.subTest(case=case):
                def change(backend, kwargs, packet, result, case=case):
                    if kwargs["role"] == "worker" and case == "17 checkpoints":
                        result["checkpoints"] = [self.checkpoint(kwargs, f"c{i}") for i in range(17)]
                    if kwargs["role"] == "worker" and case == "long proposal":
                        result["proposed_memories"] = [{"key": "k", "abstract": "a" * 501, "content": "c",
                                                        "source_paths": ["hello.py"]}]
                    if kwargs["kind"] == "review" and case == "unknown approval":
                        result["approved_memory_keys"] = ["missing"]

                backend = ScriptedBackend(change)
                engine = self.engine(backend)
                run = engine.start("Implement greet", plan())
                self.assertEqual(run["status"], "completed")
                self.assertEqual(run["attempts"], {"greet": 1})
                events = [json.loads(line)["event"] for line in (engine.directory / "events.jsonl").read_text().splitlines()]
                self.assertIn("checkpoints_discarded" if case == "17 checkpoints" else "memory_proposals_discarded", events)

    def test_valid_blocker_with_invalid_checkpoints_keeps_its_exemption(self):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and len(calls(backend, "worker")) == 1:
                result.update(status="blocked", blocker_kind="environment", blockers=["Docker stopped"],
                              checkpoints=[{"id": "broken"}])

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "环境受阻"):
            engine.start("Implement greet", plan())
        worker = entries(engine.run, "worker")[0]
        self.assertIn("attempt_exemption", worker)
        update = self.repo / "environment-update.json"
        write_json(update, {"schema_version": 1, "task_id": "greet", "blocker_dispatch_id": worker["id"],
                            "summary": "Docker restarted"})
        run = self.engine(backend).resume(engine.run["id"], environment_update=update)
        self.assertEqual(run["status"], "completed")


class AcceptancePreflightTests(RepoCase):
    def test_task_memory_record_failure_stops_before_commit(self):
        engine = self.engine(ScriptedBackend())
        hooks = []
        emit = engine.context.emit_hook

        def record(event, payload, logs):
            hooks.append(event)
            return emit(event, payload, logs)
        with patch.object(engine.context, "emit_hook", side_effect=record), \
                patch.object(Memory, "validate_record", side_effect=HarnessError("too large")):
            with self.assertRaisesRegex(HarnessError, "任务记忆记录不合格；未提交"):
                engine.start("Implement greet", plan())
        self.assertEqual(git.head(engine.worktree), engine.run["base_commit"])
        self.assertIn("pending_acceptance", engine.run)
        self.assertNotIn("pre_accept", hooks)


class LedgerLinkTests(RepoCase):
    def invalid_then_redo(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(calls(backend, "reviewer", "review")) == 1:
                result["summary"] = "x" * 4001
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        engine.start("Implement greet", plan())
        return engine, backend

    def test_retry_and_invalid_output_links_are_checked_at_load(self):
        engine, backend = self.invalid_then_redo()
        path = engine.directory / "state.json"
        saved = read_json(path)
        first, second = entries(saved, "reviewer", "review")
        worker = entries(saved, "worker")[0]
        mutations = {
            "missing": lambda run, a, b: b.update(retry_of="missing"),
            "not invalid": lambda run, a, b: a.update(status="completed"),
            "redo of a redo": lambda run, a, b: a.update(retry_of="other"),
            "not the last call": lambda run, a, b: b.update(retry_of=worker["id"]),
            "foreign artifact": lambda run, a, b: a["invalid_output"].update(
                artifact=str(engine.directory / "dispatches" / worker["id"] / "returned-result.json")),
            "bad digest": lambda run, a, b: a["invalid_output"].update(artifact_digest="x"),
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                state = copy.deepcopy(saved)
                a = next(d for d in state["dispatches"] if d["id"] == first["id"])
                b = next(d for d in state["dispatches"] if d["id"] == second["id"])
                mutate(state, a, b)
                state["status"] = "blocked"
                write_json(path, state)
                original = path.read_bytes()
                with self.assertRaisesRegex(HarnessError, "运行台账恢复状态无效"):
                    self.engine(backend).resume(saved["id"])
                self.assertEqual(path.read_bytes(), original)

    def test_cli_rejects_the_removed_repair_option(self):
        engine, _ = self.invalid_then_redo()
        path = engine.directory / "state.json"
        before = path.read_bytes()
        process = subprocess.run([sys.executable, "-m", "harness", "--config", str(ROOT / "harness.toml"),
                                  "--repo", str(self.repo), "resume", engine.run["id"], "--repair-invalid", "x"],
                                 capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(process.returncode, 2)
        self.assertIn("--repair-invalid", process.stderr)
        self.assertEqual(path.read_bytes(), before)


class RedoBoundaryTests(RepoCase):
    def invalid_first_review(self, backend):
        def change(b, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(calls(b, "reviewer", "review")) <= backend:
                result["summary"] = "x" * 4001
        return change

    def test_dispatch_budget_exhausted_at_the_first_invalid_output(self):
        for kind in ("review", "dispatch"):
            with self.subTest(kind=kind):
                self.setUp()

                def change(backend, kwargs, packet, result, kind=kind):
                    if kind == "dispatch" and kwargs["role"] == "worker" and len(calls(backend, "worker")) == 1:
                        result["summary"] = "x" * 32001
                    if kwargs["kind"] == kind and len(calls(backend, kwargs["role"], kind)) == 1:
                        if kind == "review":
                            result["summary"] = "x" * 4001
                        else:
                            result["instruction"] = "x" * 4001

                backend = ScriptedBackend(change)
                # review: worker, review. dispatch: worker, selection.
                self.config.data["runtime"]["max_agent_dispatches"] = 2
                engine = self.engine(backend)
                with self.assertRaisesRegex(HarnessError, "模型派发次数上限"):
                    engine.start("Implement greet", plan())
                role = "reviewer" if kind == "review" else "commander"
                invalid = entries(engine.run, role, kind)
                self.assertEqual([entry["status"] for entry in invalid], ["invalid"])
                attempts = dict(engine.run["attempts"])
                spent = engine.run["agent_dispatches"]
                self.assertEqual(spent, 2)
                self.assertNotIn(f"{role}:{kind}:" + ("greet" if kind == "review" else "dispatching"),
                                 engine.run["review_receipts" if kind == "review" else "commander_receipts"])
                self.config.data["runtime"]["max_agent_dispatches"] = 200
                run = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(run["status"], "completed")
                redo = entries(run, role, kind)[1]
                self.assertEqual(redo["retry_of"], invalid[0]["id"])
                invoked = [d for d in run["dispatches"] if d.get("role") in ("commander", "worker", "reviewer")
                           and d.get("invoked")]
                self.assertEqual(run["agent_dispatches"], len(invoked))
                if kind == "review":
                    self.assertEqual(run["attempts"], attempts)

    def test_resumed_call_after_an_intervening_model_call_is_a_new_request(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(calls(backend, "reviewer", "review")) == 1:
                result["summary"] = "x" * 4001

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        original = Dispatcher.dispatch_once

        def stop_redo(self_, role, kind, task_id, schema, packet, task=None, **kwargs):
            if role == "reviewer" and kwargs.get("retry_of"):
                raise KeyboardInterrupt
            return original(self_, role, kind, task_id, schema, packet, task, **kwargs)
        with patch.object(Dispatcher, "dispatch_once", stop_redo):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", plan())
        # The operator changes the candidate: a new worker runs before the next review.
        with (engine.worktree / "hello.py").open("a") as stream:
            stream.write("# operator edit\n")
        run = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(run["status"], "completed")
        reviews = entries(run, "reviewer", "review")
        self.assertEqual(len(reviews), 2)
        self.assertNotIn("retry_of", reviews[1])
        self.assertNotIn("previous_invalid", calls(backend, "reviewer", "review")[1]["packet"])

    def test_unconfirmed_exit_blocks_judgment(self):
        class Lingering(ScriptedBackend):
            """The first review leaves a process group that is still observed alive."""
            def invoke(self, **kwargs):
                if kwargs["kind"] == "review" and not calls(self, "reviewer", "review"):
                    kwargs["on_start"](999999)
                return super().invoke(**kwargs)

        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(calls(backend, "reviewer", "review")) == 1:
                result["summary"] = "x" * 4001

        backend = Lingering(change)
        engine = self.engine(backend)
        with patch("harness.dispatch.group_alive", return_value=True), \
                patch("harness.run_context.group_alive", return_value=True):
            # Neither the returning process nor a later resume may judge before exit is confirmed.
            with self.assertRaisesRegex(HarnessError, "进程组仍存在"):
                engine.start("Implement greet", plan())
            path = engine.directory / "state.json"
            for attempt in range(2):
                if attempt:
                    with self.assertRaisesRegex(HarnessError, "进程组仍存在"):
                        self.engine(backend).resume(engine.run["id"])
                saved = read_json(path)
                self.assertIn(REVIEW_KEY, saved["review_receipts"])
                review = entries(saved, "reviewer", "review")[0]
                self.assertNotIn(review["status"], ("invalid", "completed"))
                self.assertNotIn("invalid_output", review)
                self.assertEqual(len(calls(backend, "reviewer", "review")), 1)
        # Once the group has exited, the same original is judged and redone as in J1.
        run = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(run["status"], "completed")
        first, second = entries(run, "reviewer", "review")
        self.assertEqual((first["status"], second["retry_of"]), ("invalid", first["id"]))

    def test_hint_is_dropped_when_the_redo_would_overflow_context(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(calls(backend, "reviewer", "review")) == 1:
                result["summary"] = "x" * 4001

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        sizes = []
        from harness import dispatch as dispatch_module
        compose = dispatch_module.compose

        def bounded(required, memories, limit, **kwargs):
            if required["role"] == "reviewer" and required["kind"] == "review":
                prompt, report = compose(required, memories, 10**9, **kwargs)
                sizes.append(("previous_invalid" in required["packet"], report["required_chars"]))
                first_size = next(size for hinted, size in sizes if not hinted)
                if report["required_chars"] > first_size + 50:
                    from harness.context import ContextCapacityError
                    raise ContextCapacityError({**report, "budget_chars": first_size + 50})
            return compose(required, memories, limit, **kwargs)
        with patch.object(dispatch_module, "compose", side_effect=bounded):
            run = engine.start("Implement greet", plan())
        self.assertEqual(run["status"], "completed")
        first, second = entries(run, "reviewer", "review")
        self.assertEqual(second["retry_of"], first["id"])
        self.assertTrue(second["previous_invalid_omitted"])
        self.assertNotIn("previous_invalid", calls(backend, "reviewer", "review")[1]["packet"])


class HistoryRecordTests(RepoCase):
    def test_invalidated_record_without_review_is_skipped_by_later_dispatches(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(calls(backend, "reviewer", "review")) == 1:
                result.pop("summary")

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with patch.object(engine.services.receipts, "finish_review_receipt", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", plan())
        # The candidate changes before the structurally invalid review is judged (I10).
        with (engine.worktree / "hello.py").open("a") as stream:
            stream.write("# operator edit\n")
        run = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(run["status"], "completed")
        record = run["review_history"]["greet"][0]
        self.assertEqual(record["status"], "invalidated")
        self.assertNotIn("review", record)
        self.assertIn("missing or unexpected fields", record["validation_error"])
        self.assertEqual(len(calls(backend, "worker")), 2)
        later = calls(backend, "reviewer", "review")[1]["packet"]
        self.assertNotIn("previous_invalid", later)
        self.assertFalse(later.get("required_findings"))


class InterruptionTests(RepoCase):
    def change(self, invalid_reviews):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(calls(backend, "reviewer", "review")) <= invalid_reviews:
                result["summary"] = "x" * 4001
        return change

    def test_interrupted_before_judgment_rejudges_the_same_original(self):
        backend = ScriptedBackend(self.change(1))
        engine = self.engine(backend)
        with patch.object(engine.services.receipts, "finish_review_receipt", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", plan())
        run = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(run["status"], "completed")
        first, second = entries(run, "reviewer", "review")
        self.assertEqual((first["status"], second["retry_of"]), ("invalid", first["id"]))

    def test_interrupted_after_judgment_resumes_as_the_redo(self):
        backend = ScriptedBackend(self.change(2))
        engine = self.engine(backend)
        original = Dispatcher.dispatch_once

        def stop_redo(self_, role, kind, task_id, schema, packet, task=None, **kwargs):
            if role == "reviewer" and kwargs.get("retry_of"):
                raise KeyboardInterrupt
            return original(self_, role, kind, task_id, schema, packet, task, **kwargs)
        with patch.object(Dispatcher, "dispatch_once", stop_redo):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", plan())
        self.assertEqual(len(calls(backend, "reviewer", "review")), 1)
        resumed = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "连续两次无效"):
            resumed.resume(engine.run["id"])
        first, second = entries(resumed.run, "reviewer", "review")
        self.assertEqual(second["retry_of"], first["id"])
        self.assertEqual(len(calls(backend, "reviewer", "review")), 2)

    def test_returned_redo_is_judged_without_a_new_call(self):
        backend = ScriptedBackend(self.change(1))
        engine = self.engine(backend)
        original = engine.services.receipts.finish_review_receipt
        state = {"calls": 0}

        def interrupt_second(key, schema):
            state["calls"] += 1
            if state["calls"] == 2:
                raise KeyboardInterrupt
            return original(key, schema)
        with patch.object(engine.services.receipts, "finish_review_receipt", side_effect=interrupt_second):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", plan())
        self.assertEqual(len(calls(backend, "reviewer", "review")), 2)
        run = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(calls(backend, "reviewer", "review")), 2)


if __name__ == "__main__":
    unittest.main()
