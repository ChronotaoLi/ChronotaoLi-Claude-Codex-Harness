import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.engine import snapshot_digest
from harness.shared import HookError
from harness.util import HarnessError, read_json, write_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend, finding


class RecordingBackend(ScriptedBackend):
    """Record actual dispatch contracts while simulating only model responses."""

    def __init__(self, change=None):
        super().__init__(change)
        self.requests = []

    def invoke(self, **kwargs):
        self.requests.append({
            "role": kwargs["role"], "kind": kwargs["kind"], "model": copy.deepcopy(kwargs["model"]),
            "dispatch_id": kwargs["schema"]["properties"]["dispatch_id"]["enum"][0],
            "job": kwargs["job"], "prompt": kwargs["prompt"],
        })
        result = super().invoke(**kwargs)
        write_json(kwargs["job"] / "response.json", result)
        return result


class EngineRepairsTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def pre_accept_gate(self, engine, marker):
        engine.hooks.spec = {"pre_accept": [{"argv": [sys.executable, "-c",
            "import pathlib,sys; print('checking acceptance policy'); "
            "sys.exit(0 if pathlib.Path(sys.argv[1]).exists() else 7)", str(marker)]}]}

    def test_one_format_repair_has_fresh_dispatch_and_preserves_model_and_budget(self):
        def malformed(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result["summary"] = "x" * 4001
        backend = RecordingBackend(malformed)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        workers = [request for request in backend.requests if request["role"] == "worker"]
        self.assertEqual(len(workers), 2)
        self.assertNotEqual(workers[0]["dispatch_id"], workers[1]["dispatch_id"])
        self.assertNotEqual(workers[0]["job"], workers[1]["job"])
        self.assertEqual([request["model"] for request in workers], [self.config.role("worker")] * 2)
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(result["total_attempts"], 2)
        correction = [call for call in backend.calls if call["role"] == "worker"][1]["packet"]["format_correction"]
        self.assertEqual(correction["previous_dispatch"], workers[0]["dispatch_id"])
        self.assertEqual(len(read_json(Path(correction["artifact"]) / "response.json")["summary"]), 4001)
        self.assertEqual(result["accepted"]["greet"]["verification"],
                         str(engine.verification_directory("greet", 2) / "results.json"))

    def test_second_format_violation_blocks_without_a_third_worker(self):
        def malformed(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["summary"] = "x" * 4001
        backend = RecordingBackend(malformed)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["status"], "blocked")
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(engine.run["attempts"]["greet"], 2)
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 2)
        self.assertEqual(sum(engine.run["format_repairs"].values()), 1)

    def test_format_repair_cannot_exceed_task_or_total_worker_budget(self):
        def malformed(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["summary"] = "x" * 4001
        for limit in ("max_attempts", "max_total_attempts"):
            with self.subTest(limit=limit):
                self.config.data["runtime"]["max_attempts"] = 3
                self.config.data["runtime"]["max_total_attempts"] = 60
                self.config.data["runtime"][limit] = 1
                backend = RecordingBackend(malformed)
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start("Implement greet", self.plan())
                self.assertEqual(engine.run["total_attempts"], 1)
                self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)
                self.assertEqual(engine.run["format_repairs"], {})

    def test_identity_scope_and_semantic_errors_never_become_format_repairs(self):
        for mode in ("identity", "scope", "worker_state", "memory_approval"):
            def invalid(backend, kwargs, packet, result):
                if kwargs["role"] == "worker" and mode != "memory_approval":
                    result["summary"] = "x" * 4001
                    if mode == "identity":
                        result["dispatch_id"] = "previous-dispatch"
                    elif mode == "scope":
                        (kwargs["cwd"] / "outside.txt").write_text("not authorized\n")
                    else:
                        result["blocker_kind"] = "environment"
                elif kwargs["kind"] == "review" and mode == "memory_approval":
                    result.update(summary="x" * 4001, approved_memory_keys=["unproposed"])
            with self.subTest(mode=mode):
                backend = RecordingBackend(invalid)
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start("Implement greet", self.plan())
                self.assertEqual(engine.run["format_repairs"], {})
                self.assertEqual(engine.run["accepted"], {})
                self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)
                self.assertLessEqual(sum(call["kind"] == "review" for call in backend.calls), 1)
                self.assertEqual(git.head(engine.worktree), engine.run["base_commit"])

    def test_review_format_correction_cannot_drop_blocking_findings_or_change_verdict(self):
        for verdict in ("request_changes", "approve"):
            def rewrite_review(backend, kwargs, packet, result):
                if kwargs["kind"] == "review":
                    count = sum(call["kind"] == "review" for call in backend.calls)
                    if count == 1:
                        result.update(verdict=verdict, findings=[finding()], summary="x" * 4001)
                    else:
                        result.update(verdict="approve", findings=[])
            with self.subTest(verdict=verdict):
                backend = RecordingBackend(rewrite_review)
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start("Implement greet", self.plan())
                self.assertEqual(engine.run["status"], "blocked")
                self.assertEqual(engine.run["accepted"], {})
                self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)
                self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 2)
                self.assertEqual(git.head(engine.worktree), engine.run["base_commit"])

    def test_review_format_correction_keeps_negative_findings_for_normal_rework(self):
        def preserve_review(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                count = sum(call["kind"] == "review" for call in backend.calls)
                if count <= 2:
                    result.update(verdict="request_changes", findings=[finding()])
                    if count == 1:
                        result["summary"] = "x" * 4001
        backend = RecordingBackend(preserve_review)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 2)
        self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 3)
        workers = [call for call in backend.calls if call["role"] == "worker"]
        self.assertEqual(workers[1]["packet"]["prior_reviews"][0]["review"]["findings"], [finding()])
        self.assertEqual(result["review_history"]["greet"][0]["review"]["verdict"], "request_changes")

    def test_blocked_worker_with_format_fault_does_not_get_automatic_retry(self):
        for blocker_kind in ("environment", "task"):
            def blocked(backend, kwargs, packet, result):
                if kwargs["role"] == "worker":
                    result.update(status="blocked", blocker_kind=blocker_kind,
                                  blockers=["Required capability is missing"], summary="x" * 4001)
            with self.subTest(blocker_kind=blocker_kind):
                backend = RecordingBackend(blocked)
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start("Implement greet", self.plan())
                self.assertEqual(engine.run["format_repairs"], {})
                self.assertEqual(engine.run["total_attempts"], 1)
                self.assertEqual(engine.run["replan_attempts"], 0)
                self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)

    def test_three_loud_failures_reach_repair_within_context_budget_and_keep_logs(self):
        script = ("import sys; from hello import greet; "
                  "ok = greet('Ada') == 'Hello, Ada!'; "
                  "print('O' * 8000 if not ok else 'pass'); "
                  "print('E' * 8000 if not ok else '', file=sys.stderr); sys.exit(0 if ok else 1)")
        self.config.data["verification"]["commands"] = [[sys.executable, "-c", script] for _ in range(3)]
        def fail_first(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                (kwargs["cwd"] / "hello.py").write_text('def greet(name):\n    return "wrong"\n')
        backend = RecordingBackend(fail_first)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        workers = [call for call in backend.calls if call["role"] == "worker"]
        self.assertEqual(len(workers), 2)
        self.assertEqual(workers[1]["packet"]["task"], self.plan()["tasks"][0])
        checks = workers[1]["packet"]["feedback"]["verification"]
        self.assertEqual(len(checks), 3)
        self.assertTrue(all(check["exit_code"] == 1 for check in checks))
        for check in checks:
            self.assertLessEqual(len(check["stdout"]) + len(check["stderr"]), 500)
            self.assertTrue(check["output_truncated"])
            self.assertGreater(Path(check["log"]).with_suffix(".stdout").stat().st_size, 8000)
            self.assertGreater(Path(check["log"]).with_suffix(".stderr").stat().st_size, 8000)
        self.assertTrue(all(len(request["prompt"]) <= 32000 for request in backend.requests))
        self.assertEqual(len(read_json(engine.verification_directory("greet", 1) / "results.json")), 3)

    def test_only_independently_reviewed_summary_enters_accepted_memory(self):
        worker_claim = "UNVERIFIED: a remote production deployment succeeded"
        reviewed = "The greeting function satisfies the local acceptance test."
        def summaries(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["summary"] = worker_claim
            elif kwargs["kind"] == "review":
                result["summary"] = reviewed
        backend = RecordingBackend(summaries)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["accepted"]["greet"]["summary"], reviewed)
        self.assertEqual(result["accepted"]["greet"]["summary_source"], "reviewer")
        memories = engine.memory.export()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["overview"], reviewed)
        self.assertNotIn(worker_claim, json.dumps(memories))
        for request in backend.requests:
            if request["role"] == "reviewer":
                self.assertNotIn(worker_claim, request["prompt"])

    def test_pre_accept_failure_resumes_same_candidate_without_repeating_agents(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        marker = engine.state / "acceptance-permitted"
        self.pre_accept_gate(engine, marker)
        with self.assertRaises(HookError):
            engine.start("Implement greet", self.plan())
        feedback = engine.run["feedback"]["pre_accept"]
        self.assertEqual(feedback["exit_code"], 7)
        self.assertIn("checking acceptance policy", Path(feedback["log"]).with_suffix(".stdout").read_text())
        self.assertTrue(Path(feedback["review_artifact"]).exists())
        self.assertIn("pending_acceptance", read_json(engine.directory / "state.json"))
        self.assertEqual(engine.run["accepted"], {})
        marker.touch()
        resumed = self.engine(backend)
        self.pre_accept_gate(resumed, marker)
        result = resumed.resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)
        self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 1)
        self.assertEqual(result["total_attempts"], 1)
        self.assertEqual(sum(d["role"] == "verification" and d["task_id"] == "greet" for d in result["dispatches"]), 1)
        self.assertNotIn("pending_acceptance", result)
        self.assertTrue((engine.directory / "hooks/greet-1/pre_accept-2/hook-pre_accept-0.stdout").exists())

    def test_changed_pending_candidate_requires_new_validation_and_review(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        marker = engine.state / "acceptance-permitted"
        self.pre_accept_gate(engine, marker)
        with self.assertRaises(HookError):
            engine.start("Implement greet", self.plan())
        with (engine.worktree / "hello.py").open("a") as file:
            file.write("# change after rejected acceptance hook\n")
        marker.touch()
        resumed = self.engine(backend)
        self.pre_accept_gate(resumed, marker)
        result = resumed.resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        workers = [call for call in backend.calls if call["role"] == "worker"]
        self.assertEqual(len(workers), 2)
        self.assertIn("acceptance", workers[1]["packet"]["feedback"])
        self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 2)
        self.assertEqual(sum(d["role"] == "verification" and d["task_id"] == "greet" for d in result["dispatches"]), 2)
        self.assertEqual(len(result["review_history"]["greet"]), 2)

    def test_runtime_failure_is_blocked_and_operator_interrupt_is_interrupted(self):
        for error, expected in ((ValueError("bad runtime value"), "blocked"), (KeyboardInterrupt(), "interrupted")):
            def fail(backend, kwargs, packet, result):
                if kwargs["role"] == "worker":
                    raise error
            with self.subTest(error=type(error).__name__):
                engine = self.engine(RecordingBackend(fail))
                with self.assertRaises(type(error)):
                    engine.start("Implement greet", self.plan())
                state = read_json(engine.directory / "state.json")
                self.assertEqual(state["status"], expected)
                self.assertEqual(state["error_type"], type(error).__name__)

    def test_real_verification_timeout_records_feedback_then_repairs(self):
        self.config.data["runtime"]["verify_timeout_seconds"] = 1
        self.config.data["verification"]["commands"] = [[sys.executable, "-c",
            "from hello import greet; print('starting check', flush=True); assert greet('Ada') == 'Hello, Ada!'"]]
        def slow_first(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                (kwargs["cwd"] / "hello.py").write_text(
                    "import time\ndef greet(name):\n    time.sleep(5)\n    return f'Hello, {name}!'\n")
        backend = RecordingBackend(slow_first)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        timeout = next(dispatch for dispatch in result["dispatches"] if dispatch["status"] == "timed_out")
        self.assertTrue(timeout["exit_confirmed"])
        checks = read_json(engine.verification_directory("greet", 1) / "results.json")
        self.assertEqual(checks[0]["exit_code"], 124)
        self.assertTrue(checks[0]["timed_out"])
        workers = [call for call in backend.calls if call["role"] == "worker"]
        self.assertTrue(workers[1]["packet"]["feedback"]["verification"][0]["timed_out"])
        self.assertIn("starting check", checks[0]["stdout"])
        self.assertFalse(any(dispatch["status"] == "running" for dispatch in result["dispatches"]))

    def test_verifier_generated_untracked_file_blocks_with_file_diagnostics(self):
        self.config.data["verification"]["commands"] = [[sys.executable, "-c",
            "from pathlib import Path; Path('.coverage').write_bytes(b'generated coverage')"]]
        backend = RecordingBackend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["status"], "blocked")
        self.assertEqual(engine.run["accepted"], {})
        self.assertFalse(any(call["role"] == "reviewer" for call in backend.calls))
        changes = engine.run["feedback"]["verification_changes"]
        self.assertEqual(changes["added"], [".coverage"])
        self.assertEqual(changes["modified"], [])
        self.assertEqual(read_json(Path(changes["artifact"]))["added"], [".coverage"])
        self.assertTrue((engine.worktree / ".coverage").exists())
        self.assertEqual(engine.run["feedback"]["verification"][0]["exit_code"], 0)

    def test_worktree_creation_failure_persists_blocked_checkpoint(self):
        engine = self.engine(RecordingBackend())
        original = git.git
        def fail_creation(repo, *args, **kwargs):
            if args[:2] == ("worktree", "add"):
                raise OSError("worktree directory unavailable")
            return original(repo, *args, **kwargs)
        with patch("harness.engine.git.git", side_effect=fail_creation), self.assertRaises(OSError):
            engine.start("Implement greet", self.plan())
        state = read_json(engine.directory / "state.json")
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["error_type"], "OSError")
        self.assertEqual(state["dispatches"], [])
        self.assertEqual(read_json(engine.directory / "handoff.json")["status"], "blocked")

    def test_validated_replan_survives_archive_failure_without_new_commander(self):
        self.config.data["runtime"]["max_replans"] = 1
        def replan(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="task", blockers=["Need a clearer prerequisite"], summary="Blocked")
            elif kwargs["kind"] == "replan":
                prerequisite = copy.deepcopy(self.plan()["tasks"][0])
                prerequisite.update(id="prerequisite", title="Resolve prerequisite")
                result.update(action="revise", reason="Resolve prerequisite work", new_tasks=[prerequisite], updates=[{
                    "id": "greet", "description": "Implement after prerequisite", "add_depends_on": ["prerequisite"]}])
        backend = RecordingBackend(replan)
        engine = self.engine(backend)
        def fail_archive(repo, expected_head, archive, owned_root, **kwargs):
            archive.mkdir(parents=True)
            (archive / "partial-evidence").write_text("preserve this failed attempt")
            raise HarnessError("archive unavailable")
        with patch("harness.engine.git.archive_and_reset", side_effect=fail_archive), self.assertRaisesRegex(HarnessError, "archive unavailable"):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["pending_replan"]["stage"], "validated")
        self.assertEqual(saved["replan_attempts"], 1)
        self.assertEqual(saved["total_attempts"], 1)
        self.assertTrue((engine.worktree / "hello.py").exists())
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["replan_attempts"], 1)
        self.assertEqual(sum(call["kind"] == "replan" for call in backend.calls), 1)
        self.assertEqual(result["total_attempts"], 3)
        self.assertNotIn("pending_replan", result)
        self.assertEqual((engine.directory / "recovery/replan-1/partial-evidence").read_text(), "preserve this failed attempt")
        self.assertEqual(len(list((engine.directory / "recovery").glob("replan-1-*/restored.json"))), 1)

    def test_unknown_skill_stops_before_worker_dispatch(self):
        plan = self.plan()
        plan["tasks"][0]["skills"] = ["missing-skill"]
        backend = RecordingBackend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan)
        self.assertEqual(backend.calls, [])
        self.assertIsNone(engine.run)
        self.assertFalse((engine.state / "runs").exists())
        self.assertFalse((engine.state / "worktrees").exists())

    def test_final_history_stores_digest_and_external_snapshot(self):
        engine = self.engine(RecordingBackend())
        result = engine.start("Implement greet", self.plan())
        for record in result["final_history"]:
            self.assertNotIn("snapshot", record)
            self.assertNotIn("files", record)
            snapshot = read_json(Path(record["snapshot_artifact"]))
            self.assertIn("hello.py", snapshot["files"])
            self.assertEqual(record["snapshot_digest"], snapshot_digest(snapshot))
        handoff = read_json(engine.directory / "handoff.json")
        self.assertEqual(handoff["final_history"], result["final_history"])

    def test_final_attempt_budget_is_independent_of_per_task_attempt_budget(self):
        self.config.data["runtime"]["max_attempts"] = 1
        self.config.data["runtime"]["max_final_attempts"] = 2
        def corrective(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and sum(c["kind"] == "final_review" for c in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding()])
            elif kwargs["kind"] == "replan":
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id="correct", title="Correct greeting evidence", depends_on=["greet"])
                result.update(action="revise", reason="Resolve final finding", new_tasks=[task], updates=[])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                with (kwargs["cwd"] / "hello.py").open("a") as file:
                    file.write("# independently reviewed correction\n")
        result = self.engine(RecordingBackend(corrective)).start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 1, "correct": 1})
        self.assertEqual(result["final_attempts"], 2)

    def test_final_attempt_limit_stops_before_dispatching_an_unusable_correction(self):
        self.config.data["runtime"]["max_final_attempts"] = 1
        def reject(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result.update(verdict="request_changes", findings=[finding()])
        backend = RecordingBackend(reject)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "整体审阅已达尝试次数"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["final_attempts"], 1)
        self.assertEqual(engine.run["replan_attempts"], 0)
        self.assertFalse(any(call["kind"] == "replan" for call in backend.calls))


if __name__ == "__main__":
    import unittest
    unittest.main()
