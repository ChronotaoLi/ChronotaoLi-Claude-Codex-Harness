"""Provider failure classification, the immediate worker judgment and the work-attempt exemption (spec-5).

Every envelope is synthetic: the field names and value types follow Claude's
terminal envelope, the contents are invented. Calls that run ``CLIBackend`` use a
local child process in place of the CLI, and ``claude``/``codex`` stubs on PATH
fail the test if either is ever executed.
"""
import copy
import hashlib
import json
import os
import signal
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from harness import git, stops
from harness.adapters import CLIBackend, DemoBackend
from harness.budgets import work_attempts
from harness.diagnostics import budget_explanation
from harness.provider_failures import classify
from harness.run_store import RunStore
from harness.runtime import Runtime
from harness.util import HarnessError, read_json, write_json
from harness.worker_receipts import WorkerReceipts
from harness.worker_returns import inspect_return, validate_return_evidence
from test_harness import ROOT, RepoCase, after_save, crash_after_save, crash_point, run_cli, walk_exit
from test_runtime_changes import ScriptedBackend
from test_worker_return_recovery import stub_provider_clis

EXEMPT_CODES = ("usage_limit", "rate_limit", "overloaded", "authentication")  # literal: not imported from the code
NORMAL = {"started": True, "startup_state": "started", "pid": 1, "pgid": 1, "exit_code": 1,
          "termination_reason": "normal", "exit_confirmed": True, "cleanup_errors": [],
          "cleanup_actions": [], "evidence_complete": True, "errors": []}


def envelope(**fields):
    value = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 3,
             "result": "SYNTHETIC MODEL TEXT"}
    value.update(fields)
    return value


QUOTA = envelope(terminal_reason="api_error", api_error="usage_limit_reached", api_error_status=429)
RATE = envelope(api_error="rate_limit_error", api_error_status=429)
OVERLOADED = envelope(api_error="overloaded_error", api_error_status=529)
AUTHENTICATION = envelope(api_error="authentication_error", api_error_status=401)

CLASSIFY_ROWS = [
    ("usage limit", NORMAL, QUOTA, "usage_limit", 429),
    ("rate limit label", NORMAL, RATE, "rate_limit", 429),
    ("429 only", NORMAL, envelope(api_error_status=429), "rate_limit", 429),
    ("429 with a null label", NORMAL, envelope(api_error=None, api_error_status=429), "rate_limit", 429),
    ("unknown label with 429", NORMAL, envelope(api_error="SOMETHING_NEW", api_error_status=429), "invocation_failed", 429),
    ("overloaded", NORMAL, OVERLOADED, "overloaded", 529),
    ("authentication", NORMAL, AUTHENTICATION, "authentication", 401),
    ("permission", NORMAL, envelope(api_error="permission_error", api_error_status=403), "authentication", 403),
    ("max turns", NORMAL, envelope(subtype="error_max_turns", is_error=False), "agent_turns", None),
    ("max turns beats 429", NORMAL, envelope(subtype="error_max_turns", api_error="rate_limit_error",
                                             api_error_status=429), "agent_turns", 429),
    ("other explicit error", NORMAL, envelope(subtype="error_during_execution"), "invocation_failed", None),
    ("quota words only in model text", NORMAL, envelope(api_error="invalid_request_error",
        result="You've hit your limit usage_limit_reached rate_limit_error 429",
        errors=["usage_limit_reached"]), "invocation_failed", None),
    ("empty output", NORMAL, None, "unknown", None),
    ("object result is not an error", NORMAL, {"type": "result", "subtype": "success", "is_error": False,
        "structured_output": {}, "api_error": "usage_limit_reached"}, "unknown", None),
    ("timeout beats the envelope", {**NORMAL, "termination_reason": "timeout", "exit_code": -15}, QUOTA,
     "agent_timeout", None),
    ("start failure", {**NORMAL, "started": False, "startup_state": "not_started", "pid": None, "pgid": None,
                       "exit_code": None, "termination_reason": "start_failed"}, None, "local_start", None),
    ("cleanup error", {**NORMAL, "cleanup_errors": [{"step": "sigterm"}]}, QUOTA, "unknown", None),
    ("group outlived the leader", {**NORMAL, "exit_confirmed": False, "evidence_complete": False}, QUOTA,
     "unknown", None),
    ("cancelled", {**NORMAL, "termination_reason": "cancelled"}, QUOTA, "unknown", None),
    ("no process record", None, QUOTA, "unknown", None),
    ("undecodable process record", b"{broken", QUOTA, "unknown", None),
]


def raw(value):
    if value is None:
        return None
    return value if isinstance(value, bytes) else json.dumps(value).encode()


def plan():
    return read_json(ROOT / "examples" / "plan.json")


def worker(run):
    return next(d for d in run["dispatches"] if d.get("role") == "worker")


def saved(engine):
    return read_json(engine.directory / "state.json")


class ClassifierTests(unittest.TestCase):
    def test_classifier_rows(self):
        for name, outcome, terminal, code, status in CLASSIFY_ROWS:
            with self.subTest(row=name):
                self.assertEqual(classify(raw(outcome), raw(terminal) or b"", "claude"),
                                 {"code": code, "http_status": status})
        self.assertEqual(classify(raw(NORMAL), raw(QUOTA), "codex")["code"], "unknown")

    def test_worker_return_evidence_v2_shape_is_strict(self):
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder).resolve()
            (job / "process.outcome.json").write_text(json.dumps(NORMAL))
            (job / "process.stdout").write_text(json.dumps(QUOTA))
            record = inspect_return(job)
            self.assertEqual((record["version"], record["provider"]), (2, {"code": "usage_limit", "http_status": 429}))
            validate_return_evidence(job, record)
            for change in ({"version": 1}, {"provider": None}, {"provider": {"code": "nope", "http_status": None}},
                           {"provider": {"code": "usage_limit", "http_status": "429"}}):
                with self.subTest(change=change), self.assertRaisesRegex(HarnessError, "invalid worker return evidence"):
                    validate_return_evidence(job, {**record, **change})
            without = {key: value for key, value in record.items() if key != "provider"}
            with self.assertRaisesRegex(HarnessError, "invalid worker return evidence"):
                validate_return_evidence(job, without)
            (job / "process.stdout").write_text('{"type":"result","subtype":"succ')
            invalid = inspect_return(job)
            with self.assertRaisesRegex(HarnessError, "invalid worker return evidence"):
                validate_return_evidence(job, {**invalid, "provider": {"code": "unknown", "http_status": None}})


class BudgetExplanationTests(unittest.TestCase):
    def test_budget_explanation_counts_verdicts_and_both_exemption_shapes(self):
        def judged(ident, code, exemption=None):
            entry = {"id": ident, "role": "worker", "task_id": "x", "invoked": True, "status": "failed",
                     "worker_receipt_consumed": "provider_failure",
                     "worker_return_evidence": {"state": "retryable", "provider": {"code": code, "http_status": None}}}
            if exemption:
                entry["attempt_exemption"] = exemption
            return entry
        blocker = {"blocker_kind": "environment", "artifact": "/r/dispatches/w3/returned-result.json",
                   "artifact_digest": "a" * 64}
        run = {"id": "run_a", "dispatches": [
            judged("w1", "usage_limit", {"basis": "provider_error", "code": "usage_limit",
                                         "artifact": "/r/dispatches/w1/process.stdout", "artifact_digest": "b" * 64}),
            judged("w2", "agent_timeout"),
            {"id": "w3", "role": "worker", "task_id": "x", "invoked": True, "status": "completed",
             "worker_outcome": "environment_blocked", "attempt_exemption": blocker},
            {**judged("w4", "rate_limit"), "worker_return_evidence": "damaged"}]}
        dimensions = budget_explanation(run, {"x": 2})["dimensions"]
        self.assertNotIn("failed_worker_invocations", dimensions)
        verdicts = dimensions["worker_failure_verdicts"]
        self.assertEqual((verdicts["usage_limit"], verdicts["agent_timeout"], verdicts["malformed"]), (1, 1, 1))
        exemptions = dimensions["recorded_exemptions"]
        self.assertEqual((exemptions["usage_limit"], exemptions["environment"], exemptions["unknown"]), (1, 1, 0))


class FailingWorker(DemoBackend):
    """The listed worker calls run a real local child that prints ``terminal`` and exits; the rest are offline fixtures."""

    def __init__(self, config, repo, terminal, exit_code=1, failures=(1,), program=None):
        self.config, self.repo = config, repo
        self.terminal, self.exit_code, self.failures, self.program = terminal, exit_code, failures, program
        self.workers = 0
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs["role"])
        if kwargs["role"] != "worker":
            return super().invoke(**kwargs)
        self.workers += 1
        if self.workers not in self.failures:
            return super().invoke(**kwargs)
        cli = CLIBackend(self.config, self.repo / ".harness")
        printed = "" if self.terminal is None else f"print({json.dumps(self.terminal)!r}, flush=True); "
        program = self.program or f"import sys; {printed}sys.exit({self.exit_code})"
        cli.command = lambda *args: [sys.executable, "-c", program]
        return cli.invoke(**kwargs)


def verdict(run, directory):
    """The judgment without per-run bytes; each digest is checked against its own file."""
    entry = worker(run)
    job = Path(directory) / "dispatches" / entry["id"]
    evidence = copy.deepcopy(entry.get("worker_return_evidence"))
    for name, digest in evidence["sources"].items():
        assert digest == (hashlib.sha256((job / name).read_bytes()).hexdigest() if (job / name).exists() else None)
    evidence["sources"] = sorted(name for name, digest in evidence["sources"].items() if digest)
    exemption = copy.deepcopy(entry.get("attempt_exemption"))
    if exemption:
        assert exemption["artifact_digest"] == hashlib.sha256((job / "process.stdout").read_bytes()).hexdigest()
        exemption = {key: exemption[key] for key in ("basis", "code")}
    recovery = run.get("worker_recovery", {}).get(entry["task_id"], {}).get("dispatch_id") == entry["id"]
    return {"evidence": evidence, "exemption": exemption, "consumed": entry.get("worker_receipt_consumed"),
            "status": entry.get("status"), "recovery": recovery}


class ImmediateJudgmentTests(RepoCase):
    def setUp(self):
        super().setUp()
        stub_provider_clis(self)

    def failed(self, terminal=QUOTA, **options):
        backend = FailingWorker(self.config, self.repo, terminal, **options)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        return engine, backend, caught.exception

    def test_quota_failure_is_judged_exempted_and_typed_at_once(self):
        engine, backend, error = self.failed()
        self.assertEqual((stops.of(error)["kind"], stops.of(error)["reason"]), ("provider_quota", "usage_limit"))
        state = saved(engine)
        entry = worker(state)
        job = engine.directory / "dispatches" / entry["id"]
        stdout_digest = hashlib.sha256((job / "process.stdout").read_bytes()).hexdigest()
        self.assertEqual(state["worker_receipts"], {})
        self.assertEqual(entry["worker_receipt_consumed"], "provider_failure")
        self.assertEqual(entry["worker_return_evidence"]["version"], 2)
        self.assertEqual(entry["worker_return_evidence"]["provider"], {"code": "usage_limit", "http_status": 429})
        self.assertEqual(entry["attempt_exemption"], {"basis": "provider_error", "code": "usage_limit",
                                                      "artifact": str(job / "process.stdout"),
                                                      "artifact_digest": stdout_digest})
        self.assertEqual((state["attempts"], work_attempts(state), state["agent_dispatches"]), ({"greet": 1}, {"greet": 0}, 1))
        self.assertEqual(state["worker_recovery"]["greet"]["dispatch_id"], entry["id"])
        self.assertEqual(state["stop"]["source"], {"dispatch_id": entry["id"], "task_id": "greet",
                                                   "artifact": str(job / "process.stdout"),
                                                   "artifact_digest": stdout_digest})
        self.assertEqual(state["stop"]["data"]["http_status"], 429)
        after = walk_exit(self, engine.directory, ("provider_quota", "usage_limit"), backend=backend,
                          expect_status="completed")
        self.assertEqual((after["attempts"], work_attempts(after)), ({"greet": 2}, {"greet": 1}))

    def exempted(self, terminal, code):
        engine, backend, _ = self.failed(terminal)
        self.assertEqual(worker(saved(engine))["attempt_exemption"]["code"], code)
        return engine, backend

    def spared(self, after):
        self.assertEqual((after["attempts"], work_attempts(after)), ({"greet": 2}, {"greet": 1}))

    def test_rate_limit_row(self):
        engine, backend = self.exempted(RATE, "rate_limit")
        self.spared(walk_exit(self, engine.directory, ("provider_quota", "rate_limit"), backend=backend,
                              expect_status="completed"))

    def test_overloaded_row(self):
        engine, backend = self.exempted(OVERLOADED, "overloaded")
        self.spared(walk_exit(self, engine.directory, ("provider_failure", "overloaded"), backend=backend,
                              expect_status="completed"))

    def test_authentication_row(self):
        engine, backend = self.exempted(AUTHENTICATION, "authentication")
        # Exit 0 is `doctor` (a login check against the real CLIs); the run's own exit is `resume`.
        self.assertEqual(stops.project_ledger(saved(engine))["exits"][0], "doctor")
        self.spared(walk_exit(self, engine.directory, ("provider_failure", "authentication"), backend=backend,
                              expect_status="completed", choose=1))

    def test_codes_outside_the_four_are_never_exempted(self):
        for name, terminal, exit_code, code in (
                ("other error", envelope(subtype="error_during_execution"), 1, "invocation_failed"),
                ("max turns", envelope(subtype="error_max_turns", api_error_status=429), 1, "agent_turns"),
                ("empty", None, 1, "unknown"),
                ("exit zero quota", QUOTA, 0, "usage_limit")):
            with self.subTest(row=name):
                engine, _, _ = self.failed(terminal, exit_code=exit_code)
                entry = worker(saved(engine))
                self.assertEqual(entry["worker_return_evidence"]["provider"]["code"], code)
                self.assertEqual("attempt_exemption" in entry, code in EXEMPT_CODES)
                self.assertEqual(work_attempts(saved(engine)), {"greet": 0 if code in EXEMPT_CODES else 1})

    def test_crash_before_judgment_reaches_the_same_verdict(self):
        for terminal in (QUOTA, envelope(subtype="error_max_turns"), None, OVERLOADED):
            with self.subTest(terminal=terminal and (terminal.get("api_error") or terminal.get("subtype"))):
                engine, _, _ = self.failed(terminal)
                expected = verdict(saved(engine), engine.directory)
                backend = FailingWorker(self.config, self.repo, terminal)
                engine = self.engine(backend)
                died, _ = crash_after_save(engine, lambda run: any(
                    d.get("role") == "worker" and d.get("status") == "failed" for d in run["dispatches"]))
                with died, self.assertRaises(KeyboardInterrupt):
                    engine.start("Implement greet", plan())
                state = saved(engine)
                self.assertIn("worker:implement:greet", state["worker_receipts"])  # S_f only: not judged yet
                fresh = self.engine(backend)
                with patch.object(fresh.services.tasks, "task_attempt", side_effect=HarnessError("stop before work")):
                    with self.assertRaisesRegex(HarnessError, "stop before work"):
                        fresh.resume(state["id"])
                self.assertEqual(verdict(saved(engine), engine.directory), expected)

    def test_judgment_exemption_and_consumption_share_one_commit(self):
        backend = FailingWorker(self.config, self.repo, QUOTA)
        engine = self.engine(backend)
        commits = []
        original = RunStore.commit

        def record(store):
            revision = original(store)
            commits.append(copy.deepcopy(store.run))
            return revision
        with patch.object(RunStore, "commit", record), self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        checked = 0
        for run in commits:
            workers = [d for d in run["dispatches"] if d.get("role") == "worker"]
            if not workers or workers[0].get("invoked") is not True:
                continue
            entry, active = workers[0], "worker:implement:greet" in run["worker_receipts"]
            # Judged, exempted, consumed and the recovery record appear together or not at all.
            self.assertEqual("worker_return_evidence" in entry, not active)
            self.assertEqual("attempt_exemption" in entry, not active)
            self.assertEqual(entry.get("worker_receipt_consumed") is not None, not active)
            self.assertEqual("greet" in run.get("worker_recovery", {}), not active)
            checked += 1
        self.assertGreaterEqual(checked, 3)

    def test_crash_after_judgment_does_not_judge_again(self):
        backend = FailingWorker(self.config, self.repo, QUOTA)
        engine = self.engine(backend)
        died, _ = crash_after_save(engine, lambda run: any(
            d.get("role") == "worker" and "attempt_exemption" in d for d in run["dispatches"]))
        with died, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        state = saved(engine)
        self.assertEqual((state["status"], "stop" in state), ("running", False))  # S_j saved, S_stop not
        result = self.engine(backend).resume(state["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum("attempt_exemption" in d for d in result["dispatches"]), 1)
        self.assertEqual((result["attempts"], work_attempts(result)), ({"greet": 2}, {"greet": 1}))

    def test_unconfirmed_exit_keeps_the_receipt_unjudged(self):
        pid_file = self.repo.parent / f"descendant-{uuid.uuid4().hex}.pid"
        program = ("import subprocess,sys; print(" + repr(json.dumps(QUOTA)) + ", flush=True); "
                   "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
                   "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                   f"open({str(pid_file)!r},'w').write(str(p.pid)); sys.exit(1)")

        def release():
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)  # only the child this test started
                except ProcessLookupError:
                    pass
                pid_file.unlink()
        self.addCleanup(release)
        engine, backend, error = self.failed(program=program)
        state = saved(engine)
        entry = worker(state)
        # The state decides (not the exception text): the receipt is kept, nothing judged or exempted.
        self.assertIn("worker:implement:greet", state["worker_receipts"])
        self.assertNotIn("worker_return_evidence", entry)
        self.assertNotIn("attempt_exemption", entry)
        self.assertEqual(stops.of(error)["kind"], "process_unconfirmed")
        self.assertIn("Process evidence incomplete", entry["error"])  # the call's own failure text is kept
        release()
        import time
        deadline = time.monotonic() + 10
        from harness.util import group_alive
        while entry["pgid"] and group_alive(entry["pgid"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        result = self.engine(backend).resume(state["id"])
        self.assertEqual(result["status"], "completed")
        entry = worker(result)
        # The process record says the group outlived the leader: no provider classification.
        self.assertEqual(entry["worker_return_evidence"]["provider"]["code"], "unknown")
        self.assertNotIn("attempt_exemption", entry)

    def test_changed_original_is_refused_at_load_and_before_dispatch(self):
        engine, backend, _ = self.failed()
        stdout = engine.directory / "dispatches" / worker(engine.run)["id"] / "process.stdout"
        original = stdout.read_bytes()
        stdout.write_bytes(original.replace(b"usage_limit_reached", b"overloaded_error"))
        for action in ("resume", "abandon"):
            with self.subTest(action=action), self.assertRaisesRegex(HarnessError, "return evidence changed") as caught:
                getattr(self.engine(backend), action)(engine.run["id"])
            self.assertEqual(stops.of(caught.exception)["reason"], "load_integrity")
        stdout.write_bytes(original)
        state_path = engine.directory / "state.json"
        clean = read_json(state_path)
        for change in ("digest", "code", "basis", "shape"):
            state = copy.deepcopy(clean)
            exemption = worker(state)["attempt_exemption"]
            if change == "digest":
                exemption["artifact_digest"] = "0" * 64
            elif change == "code":
                exemption["code"] = "overloaded"
            elif change == "basis":
                exemption["basis"] = "worker_blocker"
            else:
                exemption["extra"] = True
            write_json(state_path, state)
            with self.subTest(change=change), self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run["id"])
        write_json(state_path, clean)
        calls = len(backend.calls)
        fresh = self.engine(backend)
        tamper, _ = after_save(fresh, lambda run: run.get("status") == "running",
                               lambda: stdout.write_bytes(original + b"\n"))
        with tamper, self.assertRaisesRegex(HarnessError, "return evidence changed") as caught:
            fresh.resume(engine.run["id"])
        self.assertEqual(len(backend.calls), calls)  # refused before the next model call
        self.assertEqual(stops.of(caught.exception)["reason"], "load_integrity")

    def test_exempted_call_still_counts_against_the_dispatch_limit(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        engine, backend, _ = self.failed()
        self.assertEqual(saved(engine)["agent_dispatches"], 1)
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "派发次数上限"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(saved(engine)["agent_dispatches"], 1)

    def test_a_quota_that_has_not_returned_fails_again_and_the_dispatch_limit_ends_it(self):
        # Each resume before the quota returns fails once more: every failure is judged and
        # exempted on its own, every call still counts, and max_agent_dispatches ends the cycle.
        # The wait is judged each time and declines (no quota sampler here, then the dispatch limit).
        self.config.data["runtime"]["max_agent_dispatches"] = 3
        backend = FailingWorker(self.config, self.repo, QUOTA, failures=(1, 2))
        engine = self.engine(backend)
        with self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        self.assertEqual(stops.of(caught.exception)["data"]["wait_reason"], "no_sampler")
        first = saved(engine)
        self.assertEqual((first["attempts"], work_attempts(first), first["agent_dispatches"]),
                         ({"greet": 1}, {"greet": 0}, 1))
        second = walk_exit(self, engine.directory, ("provider_quota", "usage_limit"), backend=backend,
                           expect_status="blocked", expect_code=3)
        self.assertEqual((second["stop"]["kind"], second["stop"]["reason"]), ("provider_quota", "usage_limit"))
        self.assertEqual(second["stop"]["data"]["wait_reason"], "dispatch_budget")
        failed = [d for d in second["dispatches"] if d.get("role") == "worker"]
        self.assertEqual([d["attempt_exemption"]["code"] for d in failed], ["usage_limit", "usage_limit"])
        for entry in failed:   # each exemption is bound to its own failed call's original
            stdout = engine.directory / "dispatches" / entry["id"] / "process.stdout"
            self.assertEqual(entry["attempt_exemption"]["artifact_digest"],
                             hashlib.sha256(stdout.read_bytes()).hexdigest())
        self.assertEqual(second["stop"]["source"]["dispatch_id"], failed[1]["id"])
        self.assertEqual((second["attempts"], work_attempts(second), second["agent_dispatches"]),
                         ({"greet": 2}, {"greet": 0}, 3))
        calls = len(backend.calls)
        third = walk_exit(self, engine.directory, ("provider_quota", "usage_limit"), backend=backend,
                          expect_status="blocked", expect_code=3)
        self.assertEqual((third["stop"]["kind"], third["stop"]["reason"]), ("budget", "agent_dispatches"))
        self.assertEqual(len(backend.calls), calls)   # the limit stops before any model call
        self.assertEqual((third["attempts"], work_attempts(third), third["agent_dispatches"]),
                         ({"greet": 2}, {"greet": 0}, 3))

    def test_an_interrupt_while_the_rejection_reads_git_leaves_the_failure_unjudged(self):
        # spec-5 13.3 window B: reject() builds the recovery record (a Git read) before it
        # changes any entry; a death or a Ctrl-C there leaves the call to be judged on resume.
        engine, _, _ = self.failed()
        expected = verdict(saved(engine), engine.directory)
        for death in (True, False):
            with self.subTest(death=death):
                backend = FailingWorker(self.config, self.repo, QUOTA)
                engine = self.engine(backend)
                dying, die = crash_point(engine)
                build = engine.services.workflow.worker_recovery
                seen = []

                def interrupt(*args, **kwargs):
                    if death:
                        die()
                    raise KeyboardInterrupt()

                def recovery_record(task_id, dispatch_id, job):
                    seen.append(sorted(engine.run["worker_receipts"]))
                    with patch.object(git, "changed", side_effect=interrupt):
                        return build(task_id, dispatch_id, job)
                with dying, patch.object(engine.services.workflow, "worker_recovery", recovery_record), \
                        self.assertRaises(KeyboardInterrupt):
                    engine.start("Implement greet", plan())
                self.assertEqual(seen, [["worker:implement:greet"]])
                state = saved(engine)
                entry = worker(state)
                # Nothing of the verdict reached the ledger: the receipt is kept, the failure unjudged.
                self.assertIn("worker:implement:greet", state["worker_receipts"])
                self.assertEqual(entry["status"], "failed")
                for key in ("worker_return_evidence", "attempt_exemption", "worker_receipt_consumed"):
                    self.assertNotIn(key, entry)
                self.assertNotEqual(state.get("worker_recovery", {}).get("greet", {}).get("dispatch_id"), entry["id"])
                self.assertEqual(state["status"], "running" if death else "interrupted")
                fresh = self.engine(backend)
                with patch.object(fresh.services.tasks, "task_attempt", side_effect=HarnessError("stop before work")):
                    with self.assertRaisesRegex(HarnessError, "stop before work"):
                        fresh.resume(state["id"])   # judged on resume without a stop of its own
                self.assertEqual(verdict(saved(engine), engine.directory), expected)
                result = self.engine(backend).resume(state["id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(sum("attempt_exemption" in d for d in result["dispatches"]), 1)
                self.assertEqual((result["attempts"], work_attempts(result)), ({"greet": 2}, {"greet": 1}))

    def test_sealed_or_returned_results_are_never_provider_failures(self):
        ok = {"type": "result", "subtype": "success", "is_error": False, "api_error": "usage_limit_reached",
              "api_error_status": 429}

        class ObjectThenExit(FailingWorker):
            def invoke(self, **kwargs):
                if kwargs["role"] == "worker":
                    schema = kwargs["schema"]
                    ids = {k: schema["properties"][k]["enum"][0] for k in ("task_id", "dispatch_id")}
                    self.terminal = {**ok, "structured_output": {
                        **ids, "status": "completed", "summary": "s", "evidence": ["hello.py"], "blockers": [],
                        "blocker_kind": "none", "proposed_memories": [], "commit_message": {"subject": "x", "body": ""}}}
                return super().invoke(**kwargs)
        engine = self.engine(ObjectThenExit(self.config, self.repo, None))
        with self.assertRaisesRegex(HarnessError, "before immutable seal") as caught:
            engine.start("Implement greet", plan())
        self.assertEqual(stops.of(caught.exception)["reason"], "unsealed_return")
        entry = worker(saved(engine))
        self.assertEqual((entry["worker_return_evidence"]["state"], entry["worker_return_evidence"]["provider"]),
                         ("returned", None))
        self.assertNotIn("attempt_exemption", entry)
        self.assertIn("worker CLI exit=1", entry["error"])  # the call's own failure stays on its entry
        # A sealed return that later fails a local gate keeps its sealed receipt.
        engine = self.engine(DemoBackend())
        failing = {"once": True}
        real = WorkerReceipts._finish

        def finish(receipts, key, task):
            if failing["once"]:
                failing["once"] = False
                raise HarnessError("post-seal local gate failed")
            return real(receipts, key, task)
        with patch.object(WorkerReceipts, "_finish", finish), \
                self.assertRaisesRegex(HarnessError, "post-seal local gate failed"):
            engine.start("Implement greet", plan())
        state = saved(engine)
        entry = worker(state)
        self.assertEqual(state["worker_receipts"]["worker:implement:greet"]["phase"], "sealed")
        self.assertNotIn("worker_return_evidence", entry)
        self.assertNotIn("attempt_exemption", entry)

    def test_timeout_with_partial_or_complete_output_is_judged_by_state_first(self):
        self.config.data["runtime"]["agent_timeout_seconds"] = 2
        partial = 'import sys,time; print(\'{"type":"result","subtype":"succ\', end="", flush=True); time.sleep(30)'
        backend = FailingWorker(self.config, self.repo, None, program=partial)
        result = self.engine(backend).start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")  # an invalid delivery: no agent_timeout stop
        entry = worker(result)
        self.assertEqual((entry["worker_return_evidence"]["state"], entry["worker_receipt_consumed"]),
                         ("invalid", "result_rejected"))
        self.assertEqual(entry["termination_reason"], "timeout")
        complete = envelope(is_error=False, structured_output={"status": "completed"})
        program = f"import time; print({json.dumps(complete)!r}, flush=True); time.sleep(30)"
        engine, _, error = self.failed(program=program)
        self.assertEqual(stops.of(error)["reason"], "unsealed_return")
        self.assertEqual(worker(saved(engine))["worker_return_evidence"]["state"], "returned")

    def test_original_changed_after_judgment_settles_the_stop_as_load_failure(self):
        backend = FailingWorker(self.config, self.repo, QUOTA)
        engine = self.engine(backend)

        def tamper():
            entry = worker(engine.run)
            path = engine.directory / "dispatches" / entry["id"] / "process.stdout"
            path.write_bytes(path.read_bytes() + b"\n")
        changed, _ = after_save(engine, lambda run: any("attempt_exemption" in d for d in run["dispatches"]), tamper)
        with changed, self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        state = saved(engine)
        entry = worker(state)
        self.assertEqual((entry["worker_receipt_consumed"], entry["attempt_exemption"]["code"]),
                         ("provider_failure", "usage_limit"))
        self.assertEqual((state["stop"]["kind"], state["stop"]["reason"]), ("evidence", "load_integrity"))
        self.assertEqual(state["stop"]["data"]["superseded"], {"kind": "provider_quota", "reason": "usage_limit"})
        self.assertEqual(stops.of(caught.exception)["reason"], "load_integrity")

    def test_outcome_write_failure_is_judged_unknown_without_exemption(self):
        import harness.util as util
        real, writes = util.write_json, {"n": 0}

        def failing(path, value):
            if Path(path).name == "process.outcome.json":
                writes["n"] += 1
                if writes["n"] == 3:  # the final record after exit
                    raise OSError(28, "No space left on device")
            return real(path, value)
        backend = FailingWorker(self.config, self.repo, QUOTA)
        engine = self.engine(backend)
        with patch("harness.util.write_json", failing), self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        self.assertEqual((stops.of(caught.exception)["kind"], stops.of(caught.exception)["reason"]),
                         ("provider_failure", "unknown"))
        entry = worker(saved(engine))
        self.assertEqual(entry["worker_return_evidence"]["provider"]["code"], "unknown")
        self.assertNotIn("attempt_exemption", entry)
        self.assertEqual(self.engine(backend).resume(engine.run["id"])["status"], "completed")

    def test_read_failure_records_no_verdict_and_resume_judges(self):
        import harness.worker_returns as returns
        real, failed = returns._read_limited, {"once": True}

        def denied(job, name):
            if name == "process.stdout" and failed["once"]:
                failed["once"] = False
                raise PermissionError(13, "Permission denied")
            return real(job, name)
        backend = FailingWorker(self.config, self.repo, QUOTA)
        engine = self.engine(backend)
        with patch.object(returns, "_read_limited", denied), self.assertRaises(PermissionError) as caught:
            engine.start("Implement greet", plan())
        state = saved(engine)
        self.assertEqual((state["stop"]["kind"], state["stop"]["reason"]), ("runtime_failure", "io"))
        self.assertIn("worker:implement:greet", state["worker_receipts"])
        self.assertNotIn("worker_return_evidence", worker(state))
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(worker(result)["attempt_exemption"]["code"], "usage_limit")

    def test_recovery_packet_names_the_failure_type(self):
        engine, backend, _ = self.failed()
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        failed = worker(result)
        second = [d for d in result["dispatches"] if d.get("role") == "worker"][1]
        packet = read_json(engine.directory / "dispatches" / second["id"] / "packet.json")
        source = packet["recovery"]["source"]
        self.assertEqual((source["dispatch_id"], source["failure"]),
                         (failed["id"], {"kind": "provider_quota", "reason": "usage_limit"}))


class UnconfirmedExitWithResultTests(RepoCase):
    def leave_a_child(self, *, returned):
        def hook(backend, kwargs, packet):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                folder = Path(kwargs["job"]) / "commands" / uuid.uuid4().hex
                folder.mkdir()
                write_json(folder / "process.json", {"id": f"{Path(kwargs['job']).name}:command:{folder.name}",
                                                     "command_id": "fixture", "pgid": None,
                                                     "process_state": "starting", "exit_confirmed": False})
                if not returned:
                    raise HarnessError("worker CLI failed; no result")
        backend = ScriptedBackend()
        backend.before_result = hook
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        return backend, engine

    def test_unconfirmed_exit_with_a_returned_result_lists_only_abandon(self):
        backend, engine = self.leave_a_child(returned=True)
        state = saved(engine)
        self.assertEqual((state["stop"]["kind"], state["stop"]["reason"], state["stop"]["data"]["returned_unsealed"]),
                         ("process_unconfirmed", "command_exit", True))
        entry = worker(state)
        self.assertEqual(stops.project_ledger(state)["exits"],
                         [f"abandon {state['id']} --confirm-exited {entry['id']}"])
        walk_exit(self, engine.directory, ("process_unconfirmed", "command_exit"), backend=backend,
                  expect_status="abandoned")

    def test_unconfirmed_exit_without_a_result_lists_both_exits(self):
        backend, engine = self.leave_a_child(returned=False)
        state = saved(engine)
        self.assertFalse(state["stop"]["data"]["returned_unsealed"])
        self.assertEqual(len(stops.project_ledger(state)["exits"]), 2)
        after = walk_exit(self, engine.directory, ("process_unconfirmed", "command_exit"), backend=backend,
                          expect_status="completed")
        self.assertEqual(worker(after)["worker_receipt_consumed"], "provider_failure")


class CommanderFailureTests(RepoCase):
    def setUp(self):
        super().setUp()
        stub_provider_clis(self)

    def failing_commander(self, event, cli_name):
        config, repo = self.config, self.repo

        class Failing(DemoBackend):
            def invoke(self, **kwargs):
                if kwargs["role"] == "commander":
                    cli = CLIBackend(config, repo / ".harness")
                    program = f"import sys; print({json.dumps(event)!r}, flush=True); sys.exit(1)"
                    cli.command = lambda *args: [sys.executable, "-c", program]
                    return cli.invoke(**kwargs)
                return super().invoke(**kwargs)
        return Failing()

    def test_claude_commander_uses_the_same_classifier_without_exemption(self):
        engine = Runtime(self.config, self.repo, backend=self.failing_commander(QUOTA, "claude"), commander="fable")
        with self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet")
        self.assertEqual((stops.of(caught.exception)["kind"], stops.of(caught.exception)["reason"]),
                         ("provider_quota", "usage_limit"))
        state = saved(engine)
        self.assertFalse(any("attempt_exemption" in d or "worker_return_evidence" in d for d in state["dispatches"]))
        self.assertEqual((state["agent_dispatches"], state["stop"]["data"]["role"]), (1, "commander"))

    def test_codex_commander_failure_stays_unknown(self):
        event = {"type": "turn.failed", "error": {"message": "usage_limit_reached 429"}}
        engine = self.engine(self.failing_commander(event, "codex"))
        with self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet")
        self.assertEqual(stops.of(caught.exception)["reason"], "unknown")


if __name__ == "__main__":
    unittest.main()
