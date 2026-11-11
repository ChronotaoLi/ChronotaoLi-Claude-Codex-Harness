import hashlib
import json
import sys

from harness.cli import status_summary
from harness.util import HarnessError, read_json, write_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend, finding


class RedesignIntegrationTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples/plan.json")

    def waiting_backend(self, *, kind="commands", minimum=0):
        command = [sys.executable, "-c", "print('focused fixture')"]

        def wait(backend, kwargs, packet, result):
            workers = [c for c in backend.calls if c["role"] == "worker"]
            if kwargs["role"] == "worker" and len(workers) == 1:
                result.update(status="blocked", blocker_kind="continuation", blockers=["Need execution prerequisites"],
                    continuation_request={"kind": kind, "commands": [command] if kind == "commands" else [],
                        "minimum_timeout_seconds": minimum, "reason": "Finish the retained candidate"})
        return ScriptedBackend(wait), command

    def test_command_continuation_waits_without_dispatch_then_uses_fresh_worker(self):
        backend, command = self.waiting_backend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["attempts"]["greet"], 1)
        self.assertEqual(engine.run["accepted"], {})
        outcomes = status_summary(engine.run)["worker_outcomes"]
        self.assertEqual(outcomes["continuation_blocked"], 1)
        self.assertEqual(outcomes["unknown"], 0)
        initial = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), initial)
        self.config.data["permissions"]["worker_commands"].append(command)
        resumed = self.engine(backend)
        result = resumed.resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        workers = [row for row in result["dispatches"] if row["role"] == "worker"]
        self.assertEqual(len(workers), 2)
        self.assertEqual(len({row["id"] for row in workers}), 2)
        self.assertEqual(sum(result["attempts"].values()), 2)
        self.assertFalse(any(row.get("attempt_exemption") for row in workers))
        self.assertEqual(len(result["continuation_history"]), 1)
        self.assertIsNone(result["pending_continuation"])
        outcomes = status_summary(result)["worker_outcomes"]
        self.assertEqual(outcomes["continuation_blocked"], 1)
        self.assertEqual(outcomes["completed"], 1)
        self.assertEqual(outcomes["unknown"], 0)

    def test_budget_request_requires_explicit_increase(self):
        before = self.config.data["runtime"]["agent_timeout_seconds"]
        backend, _ = self.waiting_backend(kind="budget", minimum=before + 60)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(self.config.data["runtime"]["agent_timeout_seconds"], before)
        self.config.data["runtime"]["agent_timeout_seconds"] = before + 60
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"]["greet"], 2)

    def test_changed_candidate_does_not_reuse_continuation(self):
        backend, command = self.waiting_backend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        (engine.worktree / "hello.py").write_text("# changed while waiting\n")
        self.config.data["permissions"]["worker_commands"].append(command)
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "candidate"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), calls)

    def test_guidance_and_grants_are_distinct_on_resume(self):
        backend, command = self.waiting_backend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        source = engine.run["continuation_history"][-1]
        guidance = self.repo / "guidance.json"
        write_json(guidance, {"task_id": "greet", "source_dispatch_id": source["source"]["dispatch_id"],
                             "source_snapshot_digest": source["snapshot_digest"],
                             "summary": "Use focused checks; runtime owns complete formal gates."})
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"], execution_guidance=guidance)
        self.assertEqual(len(backend.calls), calls)
        self.assertNotIn(command, self.config.data["permissions"]["worker_commands"])
        self.config.data["permissions"]["worker_commands"].append(command)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        worker = [call for call in backend.calls if call["role"] == "worker"][-1]
        self.assertEqual(worker["packet"]["execution_guidance"][0]["input"]["summary"],
                         "Use focused checks; runtime owns complete formal gates.")

    def test_missing_required_evidence_stops_before_expensive_gate(self):
        plan = self.plan()
        plan["tasks"][0]["evidence_requirements"] = [
            {"id": "proof", "acceptance_index": 0, "kind": "static", "path": "proof.txt"}]
        self.config.data["runtime"]["max_attempts"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan)
        self.assertFalse(any(d["role"] in ("verification", "reviewer") for d in engine.run["dispatches"]))
        failure = engine.run["feedback"]["preflight"]["failures"][0]
        self.assertEqual((failure["code"], failure["reason"], failure["path"]), ("evidence_invalid", "missing", "proof.txt"))

    def test_bound_evidence_keeps_full_task_and_final_checks(self):
        plan = self.plan()
        plan["tasks"][0]["evidence_requirements"] = [
            {"id": "implementation", "acceptance_index": 0, "kind": "static", "path": "hello.py"}]
        # The worker declares nothing; the runtime checks the planned file itself.
        engine = self.engine(ScriptedBackend())
        result = engine.start("Implement greet", plan)
        self.assertEqual(result["status"], "completed")
        preflight = read_json(engine.directory / "preflight" / "greet" / "1" / "result.json")
        self.assertEqual(preflight["evidence"][0]["sha256"],
                         hashlib.sha256((engine.worktree / "hello.py").read_bytes()).hexdigest())
        checks = [d for d in result["dispatches"] if d["role"] == "verification"]
        self.assertEqual([d["task_id"] for d in checks], ["greet", "final"])
        self.assertTrue(all(d["status"] == "completed" for d in checks))

    def test_missing_disposition_cannot_accept_approval(self):
        self.config.data["runtime"]["max_attempts"] = 2

        def unresolved(backend, kwargs, packet, result):
            if kwargs["kind"] != "review":
                return
            reviews = [call for call in backend.calls if call["kind"] == "review"]
            if len(reviews) == 1:
                result.update(verdict="request_changes", findings=[finding()])
            else:
                result.pop("finding_dispositions", None)
        engine = self.engine(ScriptedBackend(unresolved))
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["accepted"], {})
        last = engine.run["review_history"]["greet"][-1]
        self.assertEqual(last["review"]["verdict"], "approve")
        self.assertTrue(last["disposition_errors"])

    def test_old_run_is_evidence_only_and_not_migrated(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        engine.start("Implement greet", self.plan(), pause_after_plan=True)
        path = engine.directory / "state.json"
        state = read_json(path)
        state["schema_version"] = 12
        write_json(path, state)
        before = path.read_bytes()
        with self.assertRaisesRegex(HarnessError, "旧 run"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(backend.calls, [])
