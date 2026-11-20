"""Provider failure classification, the immediate worker judgment and the work-attempt exemption (spec-5).

Every envelope but one is synthetic: the field names and value types follow Claude's
terminal envelope, the contents are invented. ``REAL_CONNECTION_REFUSED`` is a real
envelope with its identifying keys removed; it shows how the classifier judges real
bytes, not how the CLI behaves. Calls that run ``CLIBackend`` use a local child
process in place of the CLI, and ``claude``/``codex`` stubs on PATH fail the test if
either is ever executed.
"""
import contextlib
import copy
import errno
import hashlib
import json
import os
import signal
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from harness import git, stops
from harness.adapters import CLIBackend, DemoBackend
from harness.budgets import work_attempts
from harness.cli import status_summary
from harness.diagnostics import budget_explanation
from harness import adapters, dispatch, provider_failures
from harness.observe import run_detail
from harness.provider_failures import classify, stop_facts
from harness.run_store import RunStore
from harness.runtime import Runtime
from harness.task_lifecycle import TaskLifecycle
from harness.util import HarnessError, read_json, tail_text, write_json
from harness.worker_receipts import WorkerReceipts
from harness.worker_returns import inspect_return, validate_return_evidence
from test_harness import ROOT, RepoCase, after_save, crash_after_save, crash_point, run_cli, walk_exit
from test_operations import OperationCase
from test_runtime_changes import ScriptedBackend
from test_worker_return_recovery import stub_provider_clis

EXEMPT_CODES = ("usage_limit", "rate_limit", "overloaded", "authentication", "transport")  # literal: not imported from the code
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
# Real: Claude Code 2.1.327 on 2026-11-18, one `claude -p` with HTTPS_PROXY pointing at a closed local
# port, so the request never reached the API. Removed: session_id, uuid, credential_expires_at, usage,
# modelUsage; the other 16 keys are as the CLI wrote them.
REAL_CONNECTION_REFUSED = {
    "api_error": "connection_failed", "api_error_status": None, "duration_api_ms": 0, "duration_ms": 186574,
    "is_error": True, "num_turns": 1, "permission_denials": [], "queued_turn_count": 0,
    "result": "API Error: Connection refused — a firewall or proxy may be blocking it (ECONNREFUSED)",
    "result_index": 0, "safety_stops": 0, "stop_reason": "stop_sequence", "subtype": "success",
    "terminal_reason": "api_error", "total_cost_usd": 0, "type": "result"}
ABSENT = object()


def real(**changes):
    """The real envelope with fields replaced, or removed where the value is ``ABSENT``."""
    value = {**REAL_CONNECTION_REFUSED, **changes}
    return {key: item for key, item in value.items() if item is not ABSENT}


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
    # Transport: only connection_failed, without a body type, with no HTTP status and terminal_reason api_error.
    ("real connection refused", NORMAL, REAL_CONNECTION_REFUSED, "transport", None),
    ("real, status missing", NORMAL, real(api_error_status=ABSENT), "transport", None),
    ("real, null body type", NORMAL, real(api_error_type=None), "transport", None),
    ("real, HTTP 503", NORMAL, real(api_error_status=503), "invocation_failed", 503),
    ("real, invalid status", NORMAL, real(api_error_status="503"), "invocation_failed", None),
    ("real, with a body type", NORMAL, real(api_error_type="overloaded_error"), "invocation_failed", None),
    ("real, terminal_reason model_error", NORMAL, real(terminal_reason="model_error"), "invocation_failed", None),
    ("real, terminal_reason missing", NORMAL, real(terminal_reason=ABSENT), "invocation_failed", None),
    ("stream_idle_timeout", NORMAL, real(api_error="stream_idle_timeout"), "invocation_failed", None),
    ("no_response", NORMAL, real(api_error="no_response"), "invocation_failed", None),
    ("computer_slept", NORMAL, real(api_error="computer_slept"), "invocation_failed", None),
    ("ECONNRESET only in text", NORMAL, real(api_error=ABSENT, result="API Error: socket hang up (ECONNRESET)"),
     "invocation_failed", None),
    ("real, cleanup error", {**NORMAL, "cleanup_errors": [{"step": "sigterm"}]}, REAL_CONNECTION_REFUSED,
     "unknown", None),
    ("real, group outlived the leader", {**NORMAL, "exit_confirmed": False, "evidence_complete": False},
     REAL_CONNECTION_REFUSED, "unknown", None),
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


class FifoWatchdog:
    """After ``bound`` seconds, opens the write end of the FIFO at ``path`` whenever a reader waits on it.

    A reader blocked opening a FIFO without a writer never returns by itself: this ends each such
    read (end of file) and records it, so a regression fails instead of hanging. ``hung`` says only
    that a reader was stuck; nothing is judged by how long a call took.
    """

    def __init__(self, path, bound=30):
        self.path, self.bound, self.hung, self.done = path, bound, False, threading.Event()
        self.thread = threading.Thread(target=self.watch, daemon=True)
        self.thread.start()

    def watch(self):
        if self.done.wait(self.bound):
            return
        while not self.done.is_set():
            try:
                os.close(os.open(self.path, os.O_WRONLY | os.O_NONBLOCK))
                self.hung = True
            except OSError:  # ENXIO: no reader holds the FIFO
                pass
            self.done.wait(0.1)

    def stop(self):
        self.done.set()
        self.thread.join()
        return self.hung


class FailingClose:
    """``os`` for ``provider_failures`` only: every descriptor it closes fails after closing; with ``streams``, every stream too."""

    def __init__(self, streams=False):
        self.streams = streams

    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def close(descriptor):
        os.close(descriptor)
        raise OSError(errno.EIO, "synthetic close failure")

    def fdopen(self, descriptor, mode):
        stream = os.fdopen(descriptor, mode)
        return FailingStreamClose(stream) if self.streams else stream


class FailingStreamClose:
    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        return self.stream

    def __exit__(self, *exc):
        self.stream.close()
        raise OSError(errno.EIO, "synthetic close failure")


class ClassifierTests(unittest.TestCase):
    def test_classifier_rows(self):
        for name, outcome, terminal, code, status in CLASSIFY_ROWS:
            with self.subTest(row=name):
                self.assertEqual(classify(raw(outcome), raw(terminal) or b"", "claude"),
                                 {"code": code, "http_status": status})
        self.assertEqual(classify(raw(NORMAL), raw(QUOTA), "codex")["code"], "unknown")
        self.assertEqual(classify(raw(NORMAL), raw(REAL_CONNECTION_REFUSED), "codex")["code"], "unknown")

    def test_label_placements(self):
        """``api_error`` and ``api_error_type`` declare a class each; a conflict is never spared."""
        def code(status=ABSENT, **labels):
            value = envelope(**labels, **({} if status is ABSENT else {"api_error_status": status}))
            return classify(raw(NORMAL), raw(value), "claude")["code"]
        for body, status, expected in (("rate_limit_error", 429, "rate_limit"), ("overloaded_error", 529, "overloaded"),
                                       ("authentication_error", 401, "authentication"),
                                       ("permission_error", 403, "authentication")):
            with self.subTest(body_only=body):
                self.assertEqual(code(status, api_error_type=body), expected)
        # The usage-limit kind beside the rate-limit body type: a usage limit only for 429, missing or null.
        for status, expected in ((429, "usage_limit"), (ABSENT, "usage_limit"), (None, "usage_limit"),
                                 (503, "invocation_failed"), (400, "invocation_failed"), ("429", "invocation_failed")):
            with self.subTest(pair_status=status):
                self.assertEqual(code(status, api_error="usage_limit_reached", api_error_type="rate_limit_error"),
                                 expected)
        for status in (429, ABSENT, None, 503, "429"):   # alone it is a usage limit whatever the status
            with self.subTest(alone_status=status):
                self.assertEqual(code(status, api_error="usage_limit_reached"), "usage_limit")
                self.assertEqual(code(status, api_error="usage_limit_reached", api_error_type=None), "usage_limit")
        for name, labels, status in (
                ("usage limit beside overloaded", {"api_error": "usage_limit_reached",
                                                   "api_error_type": "overloaded_error"}, 429),
                ("unknown kind beside permission", {"api_error": "hook_denied", "api_error_type": "permission_error"}, 403),
                ("legacy kind beside another type", {"api_error": "overloaded_error",
                                                     "api_error_type": "rate_limit_error"}, 429),
                ("unlisted type with 429", {"api_error_type": "invalid_request_error"}, 429),
                ("non-string type with 429", {"api_error_type": 7}, 429),
                ("connection kind beside a body type", {"api_error": "connection_failed",
                                                        "api_error_type": "overloaded_error",
                                                        "terminal_reason": "api_error"}, None)):
            with self.subTest(conflict=name):
                self.assertEqual(code(status, **labels), "invocation_failed")
        self.assertEqual(code(429, api_error="rate_limit_error", api_error_type="rate_limit_error"), "rate_limit")
        self.assertEqual(code(429, api_error_type=None), "rate_limit")

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


REAL_FACTS = {"exit_confirmed": True, "api_error": "connection_failed", "terminal_reason": "api_error",
              "http_status_state": "null"}


class StopFactsTests(unittest.TestCase):
    """What a failure stop may show from the originals' bytes (spec-3 rule 8): by the field's source, behind the gate."""

    def facts(self, terminal, outcome=NORMAL, provider="claude"):
        return stop_facts(raw(outcome), raw(terminal) or b"", provider)

    def test_labels_show_by_source_and_values_that_cannot_show_never_appear(self):
        self.assertEqual(self.facts(REAL_CONNECTION_REFUSED), REAL_FACTS)
        # A new CLI kind shows as written; a standard body type shows although it classifies as "other".
        self.assertEqual(self.facts(real(api_error="brand_new_kind", api_error_type="billing_error")),
                         {**REAL_FACTS, "api_error": "brand_new_kind", "api_error_type": "billing_error"})
        self.assertEqual(classify(raw(NORMAL), raw(real(api_error_type="billing_error")), "claude")["code"],
                         "invocation_failed")
        for field in ("api_error", "api_error_type", "terminal_reason"):
            for item in ("private_project_olive", "UNRECOGNIZED_SECRET\n", ["x"], None, "x" * 65):
                if field != "api_error_type" and item == "private_project_olive":
                    continue   # identifier-shaped: the CLI's own constants show it
                with self.subTest(field=field, value=item):
                    facts = self.facts(real(**{field: item}))
                    expected = {key: value for key, value in REAL_FACTS.items() if key != field}
                    # Only the field's name: the value is nowhere in the facts.
                    self.assertEqual(facts, {**expected, "unrecognized_labels": [field]})
                    made = stops.make("provider_failure", "invocation_failed", data=facts)
                    self.assertNotIn("dropped_fields", made["data"])
        self.assertEqual(self.facts(real(api_error=7, terminal_reason="API_ERROR"))["unrecognized_labels"],
                         ["api_error", "terminal_reason"])
        for status, state in ((ABSENT, "missing"), (None, "null"), (503, "valid"), ("503", "invalid")):
            with self.subTest(status=status):
                self.assertEqual(self.facts(real(api_error_status=status))["http_status_state"], state)

    def test_labels_come_only_from_the_error_envelope_the_classifier_reads(self):
        """The gate's two ``unknown`` branches (3a review): the record's facts in both, labels in neither."""
        # The process facts do not allow reading the output: no labels, even beside a real error envelope.
        for name, outcome, expected in (
                ("cleanup error", {**NORMAL, "cleanup_errors": [{"step": "sigterm"}]}, {"exit_confirmed": True}),
                ("group outlived the leader", {**NORMAL, "exit_confirmed": False, "evidence_complete": False},
                 {"exit_confirmed": False}),
                ("timeout", {**NORMAL, "termination_reason": "timeout", "exit_code": -15}, {"exit_confirmed": True}),
                ("exit confirmation not a boolean", {**NORMAL, "exit_confirmed": "yes"}, {}),
                ("no record", None, {}), ("undecodable record", b"{broken", {})):
            with self.subTest(gate=name):
                self.assertEqual(self.facts(REAL_CONNECTION_REFUSED, outcome), expected)
        self.assertEqual(self.facts(REAL_CONNECTION_REFUSED, provider="codex"), {"exit_confirmed": True})
        # The record allows it and the output was decoded, but holds no explicit error terminal: the same
        # unknown code, the complete record's exit confirmation, and no label from a non-error envelope.
        for name, terminal in (("absent", None), ("undecodable", b'{"type":"result","subtype":"succ'),
                               ("object result", {"type": "result", "subtype": "success", "is_error": False,
                                                  "structured_output": {}, "api_error": "usage_limit_reached",
                                                  "terminal_reason": "completed", "api_error_status": 429})):
            with self.subTest(output=name):
                self.assertEqual(classify(raw(NORMAL), raw(terminal) or b"", "claude")["code"], "unknown")
                self.assertEqual(self.facts(terminal), {"exit_confirmed": True})

    def test_failure_stop_shows_original_facts_only_when_both_read_back_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            (job / "process.outcome.json").write_text(json.dumps(NORMAL))
            (job / "process.stdout").write_text(json.dumps(REAL_CONNECTION_REFUSED))
            digests = {name: hashlib.sha256((job / name).read_bytes()).hexdigest()
                       for name in ("process.outcome.json", "process.stdout")}

            def data(code="transport", http_status=None, attempt_charged=None, **changes):
                return dispatch.failure_stop(code, http_status, role="worker", provider="claude", dispatch_id="w1",
                                             task_id="greet", timeout=600, artifacts={"job": job, **digests, **changes},
                                             attempt_charged=attempt_charged)["data"]
            base = {"role": "worker", "provider": "claude"}
            self.assertEqual(data(attempt_charged=False), {**base, **REAL_FACTS, "attempt_charged": False})
            # A reference digest of None (absent or unreadable when typed) or a different one: no original fact,
            # while the judged HTTP status and the ledger's attempt_charged stay.
            for name in digests:
                for digest in (None, "0" * 64):
                    with self.subTest(file=name, digest=digest):
                        self.assertEqual(data("rate_limit", 429, True, **{name: digest}),
                                         {**base, "http_status": 429, "attempt_charged": True})
            # A timeout or a start failure shows the record's exit confirmation, never labels or the HTTP state.
            self.assertEqual(data("agent_timeout"), {"role": "worker", "config_key": "runtime.agent_timeout_seconds",
                                                     "limit_seconds": 600, "exit_confirmed": True})
            self.assertEqual(data("local_start"), {**base, "exit_confirmed": True})
            (job / "process.stdout").unlink()   # absent when typed and now: None never matches None
            self.assertEqual(data("unknown", **{"process.stdout": None}), base)


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

    def test_transport_row(self):
        engine, backend, error = self.failed(REAL_CONNECTION_REFUSED)
        self.assertEqual((stops.of(error)["kind"], stops.of(error)["reason"]), ("provider_failure", "transport"))
        state = saved(engine)
        entry = worker(state)
        stdout = engine.directory / "dispatches" / entry["id"] / "process.stdout"
        self.assertEqual(entry["worker_return_evidence"]["provider"], {"code": "transport", "http_status": None})
        self.assertEqual(entry["attempt_exemption"], {"basis": "provider_error", "code": "transport",
                                                      "artifact": str(stdout),
                                                      "artifact_digest": hashlib.sha256(stdout.read_bytes()).hexdigest()})
        self.assertEqual((state["attempts"], work_attempts(state), state["agent_dispatches"]), ({"greet": 1}, {"greet": 0}, 1))
        dimensions = status_summary(state)["budget_explanation"]["dimensions"]
        self.assertEqual((dimensions["worker_failure_verdicts"]["transport"], dimensions["recorded_exemptions"]["transport"]),
                         (1, 1))
        data = state["stop"]["data"]
        self.assertEqual({key: data.get(key) for key in (*REAL_FACTS, "attempt_charged")},
                         {**REAL_FACTS, "attempt_charged": False})
        self.assertFalse({"api_error_type", "unrecognized_labels", "http_status"} & set(data))
        self.assertIn('api_error="connection_failed"', data["detail"])
        self.assertIn("api_error_status=null", data["detail"])
        after = walk_exit(self, engine.directory, ("provider_failure", "transport"), backend=backend,
                          expect_status="completed")
        self.spared(after)

    def test_codes_outside_the_five_are_never_exempted(self):
        for name, terminal, exit_code, code in (
                ("other error", envelope(subtype="error_during_execution"), 1, "invocation_failed"),
                ("connection failure with an HTTP status", real(api_error_status=503), 1, "invocation_failed"),
                ("max turns", envelope(subtype="error_max_turns", api_error_status=429), 1, "agent_turns"),
                ("empty", None, 1, "unknown"),
                ("exit zero quota", QUOTA, 0, "usage_limit")):
            with self.subTest(row=name):
                engine, _, _ = self.failed(terminal, exit_code=exit_code)
                entry = worker(saved(engine))
                self.assertEqual(entry["worker_return_evidence"]["provider"]["code"], code)
                self.assertEqual("attempt_exemption" in entry, code in EXEMPT_CODES)
                self.assertEqual(work_attempts(saved(engine)), {"greet": 0 if code in EXEMPT_CODES else 1})
                self.assertIs(saved(engine)["stop"]["data"]["attempt_charged"], code not in EXEMPT_CODES)

    def test_crash_before_judgment_reaches_the_same_verdict(self):
        for terminal in (QUOTA, envelope(subtype="error_max_turns"), None, OVERLOADED, REAL_CONNECTION_REFUSED):
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
        pid_file = self.outside / f"descendant-{uuid.uuid4().hex}.pid"
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

    def test_changed_transport_original_is_refused_at_load(self):
        engine, backend, _ = self.failed(REAL_CONNECTION_REFUSED)
        stdout = engine.directory / "dispatches" / worker(engine.run)["id"] / "process.stdout"
        original = stdout.read_bytes()
        changed = original.replace(b'"api_error_status": null', b'"api_error_status": 503')
        self.assertNotEqual(changed, original)
        stdout.write_bytes(changed)
        calls = len(backend.calls)
        for action in ("resume", "abandon"):
            with self.subTest(action=action), self.assertRaisesRegex(HarnessError, "return evidence changed") as caught:
                getattr(self.engine(backend), action)(engine.run["id"])
            self.assertEqual(stops.of(caught.exception)["reason"], "load_integrity")
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(worker(saved(engine))["attempt_exemption"]["code"], "transport")

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

    def test_transport_original_changed_after_judgment_settles_the_stop_as_load_failure(self):
        """Showing the facts only omits them; the settle that follows reports the change (spec-3 rule 8)."""
        made, build = [], dispatch.failure_stop

        def recorded(*args, **kwargs):
            made.append(build(*args, **kwargs))
            return made[-1]
        engine = self.engine(FailingWorker(self.config, self.repo, REAL_CONNECTION_REFUSED))

        def tamper():
            path = engine.directory / "dispatches" / worker(engine.run)["id"] / "process.stdout"
            path.write_bytes(path.read_bytes() + b"\n")
        changed, _ = after_save(engine, lambda run: any("attempt_exemption" in d for d in run["dispatches"]), tamper)
        with changed, patch.object(dispatch, "failure_stop", recorded), self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        self.assertEqual([(value["kind"], value["reason"], value["data"]) for value in made],
                         [("provider_failure", "transport", {"role": "worker", "provider": "claude",
                                                             "attempt_charged": False})])
        state = saved(engine)
        self.assertEqual(worker(state)["attempt_exemption"]["code"], "transport")
        self.assertEqual((state["stop"]["kind"], state["stop"]["reason"]), ("evidence", "load_integrity"))
        self.assertEqual({key: value for key, value in state["stop"]["data"].items() if key != "detail"},
                         {"superseded": {"kind": "provider_failure", "reason": "transport"}})
        self.assertEqual(stops.of(caught.exception), state["stop"])

    def test_fifo_in_place_of_the_original_after_judgment_never_blocks_the_stop(self):
        """Read again, a FIFO without a writer is unreadable at once: the facts drop and the settle still runs."""
        made, build, watchdogs = [], dispatch.failure_stop, []

        def recorded(*args, **kwargs):
            made.append(build(*args, **kwargs))
            return made[-1]
        engine = self.engine(FailingWorker(self.config, self.repo, QUOTA))

        def replace():
            path = engine.directory / "dispatches" / worker(engine.run)["id"] / "process.stdout"
            path.unlink()
            os.mkfifo(path)
            watchdogs.append(FifoWatchdog(path))
        replaced, _ = after_save(engine, lambda run: any("attempt_exemption" in d for d in run["dispatches"]), replace)
        try:
            with replaced, patch.object(dispatch, "failure_stop", recorded), self.assertRaises(HarnessError) as caught:
                engine.start("Implement greet", plan())
        finally:
            hung = [watchdog.stop() for watchdog in watchdogs]
        self.assertEqual(hung, [False])
        self.assertEqual([(value["kind"], value["reason"], value["data"]) for value in made],
                         [("provider_quota", "usage_limit", {"role": "worker", "provider": "claude",
                                                             "http_status": 429, "attempt_charged": False})])
        state = saved(engine)
        self.assertEqual(worker(state)["attempt_exemption"]["code"], "usage_limit")
        self.assertEqual((state["stop"]["kind"], state["stop"]["reason"]), ("evidence", "load_integrity"))
        self.assertEqual({key: value for key, value in state["stop"]["data"].items() if key != "detail"},
                         {"superseded": {"kind": "provider_quota", "reason": "usage_limit"}})
        self.assertEqual(stops.of(caught.exception), state["stop"])

    def test_failed_close_in_the_facts_read_keeps_the_judged_type(self):
        """The facts read a FIFO again and its close fails: unreadable like any read, never raised over the judged stop."""
        made, build, watchdogs = [], dispatch.failure_stop, []
        closing = patch.object(provider_failures, "os", FailingClose())

        def recorded(*args, **kwargs):
            made.append(build(*args, **kwargs))
            return made[-1]
        engine = self.engine(FailingWorker(self.config, self.repo, QUOTA))

        def replace():
            path = engine.directory / "dispatches" / worker(engine.run)["id"] / "process.stdout"
            path.unlink()
            os.mkfifo(path)
            watchdogs.append(FifoWatchdog(path))
            closing.start()
        replaced, fired = after_save(engine, lambda run: any("attempt_exemption" in d for d in run["dispatches"]), replace)
        try:
            with replaced, patch.object(dispatch, "failure_stop", recorded), self.assertRaises(HarnessError) as caught:
                engine.start("Implement greet", plan())
        finally:
            if fired["fired"]:
                closing.stop()
            hung = [watchdog.stop() for watchdog in watchdogs]
        self.assertEqual(hung, [False])
        self.assertEqual([(value["kind"], value["reason"], value["data"]) for value in made],
                         [("provider_quota", "usage_limit", {"role": "worker", "provider": "claude",
                                                             "http_status": 429, "attempt_charged": False})])
        state = saved(engine)
        self.assertEqual((state["stop"]["kind"], state["stop"]["reason"]), ("evidence", "load_integrity"))
        self.assertEqual(state["stop"]["data"]["superseded"], {"kind": "provider_quota", "reason": "usage_limit"})
        self.assertEqual(stops.of(caught.exception), state["stop"])

    def test_unreadable_original_omits_only_the_original_facts(self):
        """The facts read the originals again; a failed read drops those facts only, never the type."""
        reader = provider_failures.read_for_boundary
        for state in ("unreadable", "absent"):
            def read(path, state=state):
                return (state, None) if path.name == "process.stdout" else reader(path)
            with self.subTest(read=state):
                with patch.object(provider_failures, "read_for_boundary", read):
                    engine, _, error = self.failed()
                run = saved(engine)
                # The judgment read its own bytes, the settle found the originals intact: the type stays.
                self.assertEqual(worker(run)["attempt_exemption"]["code"], "usage_limit")
                self.assertEqual((run["stop"]["kind"], run["stop"]["reason"]), ("provider_quota", "usage_limit"))
                self.assertEqual(run["stop"], stops.of(error))
                data = run["stop"]["data"]
                self.assertEqual((data["http_status"], data["attempt_charged"]), (429, False))
                self.assertFalse({"api_error", "api_error_type", "terminal_reason", "http_status_state",
                                  "exit_confirmed", "unrecognized_labels"} & set(data))

    def test_labels_show_by_source_in_the_stop_and_the_diagnostic(self):
        terminal = envelope(terminal_reason="api_error", api_error="brand_new_kind",
                            api_error_type="private_project_olive", api_error_status=None)
        engine, _, error = self.failed(terminal)
        run = saved(engine)
        stop, entry = run["stop"], worker(run)
        self.assertEqual((stop["kind"], stop["reason"]), ("provider_failure", "invocation_failed"))
        self.assertEqual((stop["data"]["api_error"], stop["data"]["unrecognized_labels"], stop["data"]["attempt_charged"]),
                         ("brand_new_kind", ["api_error_type"], True))
        self.assertNotIn("api_error_type", stop["data"])
        for text in (json.dumps(stop), str(error), entry["error"]):
            self.assertNotIn("private_project_olive", text)
        # The diagnostic text, not the serialized stop: escaping changes the quotes there.
        for text in (stop["data"]["detail"], str(error), entry["error"]):
            self.assertIn('api_error="brand_new_kind"', text)
            self.assertIn("api_error_type=unrecognized", text)

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

    def test_recovery_packet_names_the_transport_failure(self):
        engine, backend, _ = self.failed(REAL_CONNECTION_REFUSED)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        failed, second = [d for d in result["dispatches"] if d.get("role") == "worker"][:2]
        packet = read_json(engine.directory / "dispatches" / second["id"] / "packet.json")
        source = packet["recovery"]["source"]
        self.assertEqual((source["dispatch_id"], source["failure"]),
                         (failed["id"], {"kind": "provider_failure", "reason": "transport"}))


class StopExitLabelTests(OperationCase):
    """``test_labels_show_by_source_in_the_stop_and_the_diagnostic`` through the CLI's other exits of the stop."""

    def setUp(self):
        super().setUp()
        stub_provider_clis(self)

    def test_unlisted_body_type_reaches_no_exit_but_its_field_name_does(self):
        private = "private_project_olive"
        terminal = envelope(terminal_reason="api_error", api_error="brand_new_kind", api_error_type=private,
                            api_error_status=None)
        code, output, errors = self.invoke(["run", "Implement greet", "--operation-id", "olive"],
                                           FailingWorker(self.config, self.repo, terminal))
        self.assertEqual(code, 3, errors)
        record = self.record("olive")
        events = self.events(record["run_id"], "run_stopped")
        notices = [json.loads(line.split(" ", 1)[1]) for line in errors.splitlines() if line.startswith("[harness-json] ")]
        detail = run_detail(self.state, record["run_id"])
        exits = {"run_stopped": events[-1]["stop"], "operation": record["stop"], "summary": detail["summary"]["stop"],
                 "stderr": next(notice["stop"] for notice in notices if notice["event"] == "stop")}
        for name, stop in exits.items():
            with self.subTest(exit=name):
                self.assertEqual((stop["kind"], stop["reason"]), ("provider_failure", "invocation_failed"))
                self.assertEqual((stop["data"]["api_error"], stop["data"]["unrecognized_labels"]),
                                 ("brand_new_kind", ["api_error_type"]))
                self.assertNotIn("api_error_type", stop["data"])
        for name, value in {"run_stopped": events, "operation": record, "summary": detail, "stderr": errors,
                            "stdout": output}.items():
            with self.subTest(whole=name):
                self.assertNotIn(private, value if isinstance(value, str) else json.dumps(value))


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

    def failing_commander(self, event, cli_name, once=False):
        config, repo = self.config, self.repo

        class Failing(DemoBackend):
            failed = False

            def invoke(self, **kwargs):
                if kwargs["role"] == "commander" and not (once and self.failed):
                    self.failed = True
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

    def test_claude_commander_transport_failure(self):
        backend = self.failing_commander(REAL_CONNECTION_REFUSED, "claude", once=True)
        engine = Runtime(self.config, self.repo, backend=backend, commander="fable")
        with self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet")
        self.assertEqual((stops.of(caught.exception)["kind"], stops.of(caught.exception)["reason"]),
                         ("provider_failure", "transport"))
        state = saved(engine)
        self.assertFalse(any("attempt_exemption" in d or "worker_return_evidence" in d for d in state["dispatches"]))
        self.assertEqual((state["agent_dispatches"], state["stop"]["data"]["role"]), (1, "commander"))
        # The facts of the commander's own originals; no work attempt to tell about.
        self.assertEqual({key: value for key, value in state["stop"]["data"].items() if key != "detail"},
                         {"role": "commander", "provider": "claude", **REAL_FACTS})
        after = walk_exit(self, engine.directory, ("provider_failure", "transport"), backend=backend,
                          expect_status="completed")
        self.assertFalse(any("attempt_exemption" in d for d in after["dispatches"]))

    def test_unreadable_output_twice_shows_no_process_facts(self):
        """Unreadable at the first read and again for the facts: two missing digests never match (spec-3 rule 8)."""
        reader, reads = provider_failures.read_for_boundary, []

        def read(path):
            if path.name == "process.stdout":
                reads.append(path)
                return "unreadable", None
            return reader(path)
        engine = Runtime(self.config, self.repo, backend=self.failing_commander(REAL_CONNECTION_REFUSED, "claude"),
                         commander="fable")
        with patch.object(provider_failures, "read_for_boundary", read), self.assertRaises(HarnessError):
            engine.start("Implement greet")
        self.assertEqual(len(reads), 2)
        stop = saved(engine)["stop"]
        self.assertEqual((stop["kind"], stop["reason"]), ("provider_failure", "unknown"))
        self.assertEqual({key: value for key, value in stop["data"].items() if key != "detail"},
                         {"role": "commander", "provider": "claude"})

    def test_fifo_output_at_the_call_boundary_never_blocks(self):
        """Both reads of a commander's or reviewer's output return at once; a FIFO is unreadable, never an absence."""
        for role in ("commander", "reviewer"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as folder:
                job = Path(folder)
                (job / "process.outcome.json").write_text(json.dumps(NORMAL))
                os.mkfifo(job / "process.stdout")
                watchdog = FifoWatchdog(job / "process.stdout")
                try:
                    stop = dispatch.invocation_stop(HarnessError("synthetic failure"), job=job, role=role,
                                                    provider="claude", dispatch_id="probe", task_id="greet", timeout=60)
                finally:
                    hung = watchdog.stop()
                self.assertFalse(hung)
                self.assertEqual((stop["kind"], stop["reason"], stop["data"]),
                                 ("provider_failure", "unknown", {"role": role, "provider": "claude"}))

    def test_only_a_regular_original_reads(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "regular").write_bytes(b"{}")
            (base / "link").symlink_to(base / "regular")
            os.mkfifo(base / "fifo")
            watchdog = FifoWatchdog(base / "fifo")
            try:
                states = {name: provider_failures.read_for_boundary(path) for name, path in (
                    ("regular", base / "regular"), ("missing", base / "missing"), ("fifo", base / "fifo"),
                    ("link", base / "link"), ("directory", base), ("device", Path(os.devnull)))}
            finally:
                hung = watchdog.stop()
        self.assertFalse(hung)
        self.assertEqual(states, {"regular": ("read", b"{}"), "missing": ("absent", None),
                                  **{name: ("unreadable", None) for name in ("fifo", "link", "directory", "device")}})

    def test_a_failed_close_is_unreadable_and_never_raised(self):
        """Closing a FIFO's descriptor or a regular file's stream fails: the read is unreadable, nothing escapes."""
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "regular").write_bytes(b"{}")
            os.mkfifo(base / "fifo")
            watchdog = FifoWatchdog(base / "fifo")
            try:
                with patch.object(provider_failures, "os", FailingClose(streams=True)):
                    bounded = {name: provider_failures.read_for_boundary(base / name) for name in ("fifo", "regular")}
                    whole = {name: provider_failures.read_original(base / name, lambda stream: stream.read())
                             for name in ("fifo", "regular")}
            finally:
                hung = watchdog.stop()
        self.assertFalse(hung)
        self.assertEqual(bounded, {"fifo": ("unreadable", None), "regular": ("unreadable", None)})
        self.assertEqual({name: (state, error.errno) for name, (state, error) in whole.items()},
                         {"fifo": ("unreadable", errno.EIO), "regular": ("unreadable", errno.EIO)})


class FifoAfterExitTests(RepoCase):
    """A commander's or reviewer's ``process.stdout`` becomes a FIFO without a writer once its CLI exited.

    Every later read of the originals until the stop is saved (the CLI summary, the result, the
    boundary, the dispatch's diagnostics and usage) returns at once; one that waits is ended by
    ``FifoWatchdog`` and fails the test. The CLI is a local child process.
    """
    TERMINAL = {**QUOTA, "permission_denials": [{"tool_name": "Bash", "tool_use_id": "tool_1"}]}
    NO_FACTS = {"permission_denials": {"status": "unknown"}}

    def setUp(self):
        super().setUp()
        stub_provider_clis(self)
        self.config.data["roles"]["reviewer"] = {"cli": "claude", "model": "claude-sonnet-9-1", "effort": "high"}

    def call(self, role, *, exit_code, fifo):
        """Run until ``role``'s one call fails; its output becomes a FIFO at ``fifo``: "exit", "boundary" or None."""
        config, repo, watchdogs, typed = self.config, self.repo, [], []
        program = (f"import sys; print({json.dumps(self.TERMINAL)!r}, flush=True); "
                   f"print('SYNTHETIC STDERR', file=sys.stderr); sys.exit({exit_code})")

        class Backend(DemoBackend):
            def invoke(self, **kwargs):
                if kwargs["role"] != role:
                    return super().invoke(**kwargs)
                cli = CLIBackend(config, repo / ".harness")
                cli.command = lambda *args: [sys.executable, "-c", program]
                return cli.invoke(**kwargs)

        def replace(path, when):
            if fifo == when:
                path.unlink()
                os.mkfifo(path)
                watchdogs.append(FifoWatchdog(path))
        process, boundary = adapters.run_process, dispatch.invocation_stop

        def ran(*args, **kwargs):
            code = process(*args, **kwargs)
            replace(Path(args[2]).with_suffix(".stdout"), "exit")
            return code

        def typed_at_boundary(failure, **kwargs):
            typed.append(boundary(failure, **kwargs))
            replace(Path(kwargs["job"]) / "process.stdout", "boundary")
            return typed[-1]
        engine = Runtime(self.config, self.repo, backend=Backend(), commander="fable")
        try:
            with patch.object(adapters, "run_process", ran), patch.object(dispatch, "invocation_stop", typed_at_boundary), \
                    self.assertRaises(HarnessError) as caught:
                engine.start("Implement greet", *(() if role == "commander" else (plan(),)))
        finally:
            hung = [watchdog.stop() for watchdog in watchdogs]
        self.assertEqual(hung, [False] if fifo else [])
        state = saved(engine)
        self.assertEqual(stops.of(caught.exception), state["stop"])
        self.assertEqual([(value["kind"], value["reason"]) for value in typed],
                         [(state["stop"]["kind"], state["stop"]["reason"])])
        entry = next(d for d in state["dispatches"] if d["role"] == role)
        return state["stop"], entry

    def test_fifo_after_a_nonzero_exit(self):
        for role in ("commander", "reviewer"):
            with self.subTest(role=role):
                stop, entry = self.call(role, exit_code=1, fifo="exit")
                self.assertEqual((stop["kind"], stop["reason"]), ("provider_failure", "unknown"))
                self.assertEqual({key: value for key, value in stop["data"].items() if key != "detail"},
                                 {"role": role, "provider": "claude"})
                self.assertEqual((entry["diagnostics"], "provider_usage" in entry), (self.NO_FACTS, False))
                self.assertNotIn("Claude reported an error", entry["error"])
                self.assertIn("SYNTHETIC STDERR", entry["error"])

    def test_fifo_after_an_error_envelope_with_exit_0(self):
        for role in ("commander", "reviewer"):
            with self.subTest(role=role):
                stop, entry = self.call(role, exit_code=0, fifo="exit")
                self.assertEqual((stop["kind"], stop["reason"]), ("provider_failure", "unknown"))
                self.assertEqual({key: value for key, value in stop["data"].items() if key != "detail"},
                                 {"role": role, "provider": "claude"})
                self.assertEqual((entry["diagnostics"], "provider_usage" in entry), (self.NO_FACTS, False))

    def test_fifo_after_the_boundary_typed_the_stop(self):
        """Only the dispatch's diagnostics and usage read after the stop was typed: it is saved as typed."""
        for role in ("commander", "reviewer"):
            with self.subTest(role=role):
                stop, entry = self.call(role, exit_code=1, fifo="boundary")
                self.assertEqual((stop["kind"], stop["reason"], stop["data"]["http_status"]),
                                 ("provider_quota", "usage_limit", 429))
                self.assertEqual(stop["data"]["api_error"], "usage_limit_reached")
                self.assertEqual((entry["diagnostics"], "provider_usage" in entry), (self.NO_FACTS, False))

    def test_regular_originals_read_as_before(self):
        """Without a FIFO every read on the way reads the regular file: summary, stderr, type, diagnostics, usage."""
        for role in ("commander", "reviewer"):
            for exit_code in (1, 0):
                with self.subTest(role=role, exit_code=exit_code):
                    stop, entry = self.call(role, exit_code=exit_code, fifo=None)
                    self.assertEqual((stop["kind"], stop["reason"], stop["data"]["api_error"]),
                                     ("provider_quota", "usage_limit", "usage_limit_reached"))
                    self.assertEqual(entry["diagnostics"], {"permission_denials": [
                        {"reason": "unavailable", "tool": "Bash", "tool_id": "tool_1"}]})
                    self.assertEqual(entry["provider_usage"], {"provider": "claude", "num_turns": 3})
                    self.assertIn('api_error="usage_limit_reached"', entry["error"])
                    self.assertEqual("SYNTHETIC STDERR" in entry["error"], exit_code == 1)


class ReadsOf:
    """``os`` for ``provider_failures``: wraps every stream opened on ``targets`` (paths, filled in late) with ``wrap``.

    ``reads`` collects the size of every read on any stream; ``fail_open`` names files whose
    second open fails with EIO.
    """

    def __init__(self, targets=(), wrap=None, fail_open=()):
        self.targets, self.wrap, self.fail_open, self.opens, self.reads = targets, wrap, fail_open, {}, []

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, path, flags):
        name = Path(path).name
        self.opens[name] = self.opens.get(name, 0) + 1
        if name in self.fail_open and self.opens[name] > 1:
            raise OSError(errno.EIO, "synthetic read failure")
        return os.open(path, flags)

    def fdopen(self, descriptor, mode):
        stream = Counted(os.fdopen(descriptor, mode), self.reads)
        identity = os.fstat(descriptor)
        if self.wrap and any(path.exists() and os.path.samestat(identity, path.stat()) for path in self.targets):
            return self.wrap(stream)
        return stream


class Counted:
    def __init__(self, stream, reads):
        self.stream, self.reads = stream, reads

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stream.close()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.reads.append(len(data))
        return data


class FailingStat(contextlib.ExitStack):
    """While entered, every stat of ``targets`` (paths, filled in late) fails with EIO: by path, link or descriptor.

    pathlib's checks stat by path or link (Python 3.11 to 3.13 let the EIO out, later versions
    swallow it), ``read_original`` by descriptor: whatever asks, the file's metadata cannot be
    read, so a case means the same on every version.
    """

    def __init__(self):
        super().__init__()
        self.targets, self.real = [], {name: getattr(os, name) for name in ("stat", "lstat", "fstat")}

    def __enter__(self):
        for name, real in self.real.items():
            self.enter_context(patch.object(os, name, self.failing(real)))
        return self

    def failing(self, real):
        def stat(*args, **kwargs):
            info = real(*args, **kwargs)
            for path in self.targets:
                try:
                    same = os.path.samestat(info, self.real["stat"](path))
                except OSError:
                    same = False
                if same:
                    raise OSError(errno.EIO, "synthetic stat failure", str(path))
            return info
        return stat


class FailedCallReadTests(RepoCase):
    """The rest of the reads of a call's originals once its CLI exited (BRIEF-3b4): bounded, never blocking,
    and a read that misses never replaces the failure being reported. The CLI is a local child process."""
    TERMINAL = {"type": "result", "subtype": "success", "is_error": False, "structured_output": {}}

    def setUp(self):
        super().setUp()
        stub_provider_clis(self)

    def backend(self, role, program):
        """``program(job)``: the local process standing in for ``role``'s CLI."""
        config, repo = self.config, self.repo

        class Backend(DemoBackend):
            def invoke(self, **kwargs):
                if kwargs["role"] != role:
                    return super().invoke(**kwargs)
                cli = CLIBackend(config, repo / ".harness")
                cli.command = lambda *args: [sys.executable, "-c", program(kwargs["job"])]
                return cli.invoke(**kwargs)
        return Backend()

    def test_recording_an_invalid_output_never_reads_the_original_again(self):
        """The redo's original becomes a FIFO once judged: the invalid output and the stop bind the judged bytes."""
        cases = (("commander", "process.stdout", lambda job: "print('{broken')", b"{broken\n"),
                 ("reviewer", "response.json",
                  lambda job: f"open({str(job / 'response.json')!r}, 'w').write('{{broken')", b"{broken"))
        for role, name, program, written in cases:
            with self.subTest(role=role):
                self.setUp()
                judged, watchdogs = [], []
                original = CLIBackend.completed_result

                def completed(backend, *args):
                    try:
                        return original(backend, *args)
                    except adapters.UndecodableOutput as error:
                        judged.append(error.artifact)
                        if len(judged) == 2:
                            error.artifact.unlink()
                            os.mkfifo(error.artifact)
                            watchdogs.append(FifoWatchdog(error.artifact))
                        raise
                engine = Runtime(self.config, self.repo, backend=self.backend(role, program),
                                 **({"commander": "fable"} if role == "commander" else {}))
                try:
                    with patch.object(CLIBackend, "completed_result", completed), self.assertRaises(HarnessError) as caught:
                        engine.start("Implement greet", *(() if role == "commander" else (plan(),)))
                finally:
                    hung = [watchdog.stop() for watchdog in watchdogs]
                self.assertEqual(hung, [False])
                state = saved(engine)
                self.assertEqual((state["stop"]["kind"], state["stop"]["reason"]), ("invalid_output", "redo_exhausted"))
                self.assertEqual(stops.of(caught.exception), state["stop"])
                first, second = [d for d in state["dispatches"] if d["role"] == role]
                digest = hashlib.sha256(written).hexdigest()
                self.assertEqual([d["invalid_output"] for d in (first, second)],
                                 [{"artifact": str(path), "artifact_digest": digest} for path in judged])
                self.assertEqual([path.name for path in judged], [name, name])
                self.assertEqual((state["stop"]["source"]["artifact"], state["stop"]["source"]["artifact_digest"]),
                                 (str(judged[1]), digest))

    def failed_close(self, target):
        """A Claude commander returns a whole terminal and exits 7; closing ``target`` after reading it fails."""
        targets = []
        return self.failed_read(target, targets, patch.object(provider_failures, "os", ReadsOf(targets, wrap=FailingStreamClose)))

    def failed_stat(self, target):
        """The same call; once it exited, every stat of ``target`` fails."""
        failing = FailingStat()
        return self.failed_read(target, failing.targets, failing)

    def failed_read(self, target, targets, injection):
        """The same call under ``injection``, with ``target`` added to ``targets`` once the CLI exited."""
        terminal = json.dumps(self.TERMINAL)
        backend = self.backend("commander", lambda job: f"import sys; print({terminal!r}); sys.exit(7)")
        engine = Runtime(self.config, self.repo, backend=backend, commander="fable")
        process = adapters.run_process

        def ran(*args, **kwargs):
            code = process(*args, **kwargs)
            targets.extend([Path(args[2]).with_name(target)] if target else [])
            return code
        with patch.object(adapters, "run_process", ran), injection, self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet")
        state = saved(engine)
        self.assertEqual((state["stop"]["kind"], state["stop"]["reason"]), ("provider_failure", "unknown"))
        self.assertEqual(stops.of(caught.exception), state["stop"])
        self.assertTrue(str(caught.exception).startswith("commander CLI exit=7"))
        job = engine.directory / "dispatches" / next(d["id"] for d in state["dispatches"] if d["role"] == "commander")
        return caught.exception, job

    def test_a_failed_close_of_the_outcome_on_a_nonzero_exit_never_replaces_the_cli_failure(self):
        """The capture cannot bind the process record: it is a note on the CLI failure, which is what stops."""
        error, job = self.failed_close("process.outcome.json")
        self.assertFalse((job / "terminal-capture.json").exists())
        self.assertEqual(getattr(error, "__notes__", []),
                         ["Terminal evidence preservation failed: HarnessError: Terminal evidence must be a regular file"])

    def test_a_failed_close_of_the_terminal_on_a_nonzero_exit_captures_nothing(self):
        """The capture's one read of the terminal misses: nothing is captured and the CLI failure stops."""
        error, job = self.failed_close("process.stdout")
        self.assertFalse((job / "terminal-capture.json").exists())
        self.assertEqual(getattr(error, "__notes__", []), [])

    def test_a_regular_terminal_on_a_nonzero_exit_is_captured_as_before(self):
        error, job = self.failed_close(None)
        capture = read_json(job / "terminal-capture.json")
        self.assertEqual((capture["source_digest"], capture["outcome_digest"]),
                         tuple(hashlib.sha256((job / name).read_bytes()).hexdigest()
                               for name in ("process.stdout", "process.outcome.json")))
        self.assertEqual(read_json(job / "returned-result.json"), {})
        self.assertEqual(getattr(error, "__notes__", []), [])

    def test_a_failed_stat_of_the_outcome_on_a_nonzero_exit_never_replaces_the_cli_failure(self):
        """No check of the originals' metadata precedes the capture's reads: the same note as a failed close."""
        error, job = self.failed_stat("process.outcome.json")
        self.assertFalse((job / "terminal-capture.json").exists())
        self.assertEqual(getattr(error, "__notes__", []),
                         ["Terminal evidence preservation failed: HarnessError: Terminal evidence must be a regular file"])

    def test_a_failed_stat_of_the_terminal_on_a_nonzero_exit_captures_nothing(self):
        error, job = self.failed_stat("process.stdout")
        self.assertFalse((job / "terminal-capture.json").exists())
        self.assertEqual(getattr(error, "__notes__", []), [])

    def test_a_failed_write_of_the_capture_on_a_nonzero_exit_stays_local_io(self):
        """Harness's own write is not a read of an original: its failure leaves as local I/O, as it always did."""
        terminal = json.dumps(self.TERMINAL)
        backend = self.backend("commander", lambda job: f"import sys; print({terminal!r}); sys.exit(7)")
        engine = Runtime(self.config, self.repo, backend=backend, commander="fable")
        write = adapters.write_json

        def failing(path, value):
            if path.name == "terminal-capture.json":
                raise OSError(errno.EIO, "synthetic write failure")
            write(path, value)
        with patch.object(adapters, "write_json", failing), self.assertRaises(OSError) as caught:
            engine.start("Implement greet")
        state = saved(engine)
        self.assertEqual((state["stop"]["kind"], state["stop"]["reason"]), ("runtime_failure", "io"))
        self.assertEqual(stops.of(caught.exception), state["stop"])
        self.assertEqual(caught.exception.errno, errno.EIO)


class TerminalLayoutTests(unittest.TestCase):
    """The capture's own reads refuse a terminal or process record that is a link, directory, FIFO or absent.

    No path check precedes them, and every layout keeps the result it had when one did.
    """

    def test_every_layout_of_the_terminal_and_the_process_record_on_every_branch(self):
        refused = "Terminal evidence must be a regular file"
        branches = (("nonzero exit", {**NORMAL, "exit_code": 7}, True),
                    ("exception before a known end", {**NORMAL, "exit_code": None, "termination_reason": "timeout"}, False),
                    ("exception after exit 0", {**NORMAL, "exit_code": 0, "evidence_complete": False}, False))
        for provider, source, terminal in (("claude", "process.stdout", b'{"type":"result","structured_output":{"answer":42}}'),
                                           ("codex", "response.json", b'{"answer":42}')):
            for name in (source, "process.outcome.json"):
                for layout in ("symlink", "directory", "fifo", "absent"):
                    for branch, outcome, terminal_only in branches:
                        with self.subTest(provider=provider, file=name, layout=layout, branch=branch), \
                                tempfile.TemporaryDirectory() as folder:
                            job = Path(folder)
                            (job / source).write_bytes(terminal)
                            (job / "process.outcome.json").write_text(json.dumps(outcome))
                            (job / "elsewhere").write_bytes((job / name).read_bytes())
                            (job / name).unlink()
                            {"symlink": lambda path: path.symlink_to(job / "elsewhere"), "directory": Path.mkdir,
                             "fifo": os.mkfifo, "absent": lambda path: None}[layout](job / name)
                            before = set(os.listdir(job))
                            watchdog = FifoWatchdog(job / name) if layout == "fifo" else None
                            try:
                                result = CLIBackend(None, None).preserve_terminal(job, provider, outcome, terminal_only=terminal_only)
                            except HarnessError as error:
                                result = (type(error).__name__, str(error))
                            finally:
                                hung = watchdog.stop() if watchdog else False
                            first_read = branch != "exception after exit 0"
                            if name == source:
                                expected = (False if first_read else ("HarnessError", refused)), set()
                            else:
                                # Only a returned Claude result writes before the record is read.
                                expected = (("HarnessError", refused),
                                            {"provider-result.json", "response.json"} if provider == "claude" and first_read else set())
                            self.assertEqual((result, set(os.listdir(job)) - before, hung), (*expected, False))


class OriginalReadBoundTests(unittest.TestCase):
    """A failed call's reads of its originals are bounded: whole reads at ``LIMIT``, the stderr tail at its
    size. A returned result (exit 0) is read whole, as it always was."""

    def test_an_original_over_the_limit_is_never_read_whole(self):
        limit = provider_failures.LIMIT
        backend = CLIBackend(None, None)
        for provider, name in (("claude", "process.stdout"), ("codex", "response.json")):
            value = {"answer": 42}
            within = json.dumps({"type": "result", "structured_output": value} if provider == "claude" else value).encode()
            for label, data in (("within", within), ("over", within + b" " * limit)):
                with self.subTest(provider=provider, size=label), tempfile.TemporaryDirectory() as folder:
                    job = Path(folder)
                    (job / name).write_bytes(data)
                    (job / "process.outcome.json").write_text(json.dumps({**NORMAL, "exit_code": 7}))
                    reads = ReadsOf()
                    results = []
                    with patch.object(provider_failures, "os", reads):
                        for call in (lambda: backend.preserve_terminal(job, provider, {**NORMAL, "exit_code": 7},
                                                                       terminal_only=True),
                                     lambda: backend.preserve_terminal(job, provider, {**NORMAL, "exit_code": 0})):
                            try:
                                results.append(call())
                            except HarnessError as error:
                                results.append(str(error).replace(folder, "<job>"))
                    self.assertLessEqual(max(reads.reads), limit + 1)
                    self.assertEqual(results, [True, True] if label == "within"
                                     else [False, "Terminal evidence must be a regular file"])

    def test_a_returned_result_over_the_limit_is_read_whole(self):
        """Claude's envelope has no size bound (``permission_denials`` accumulate): exit 0 reads it all."""
        pad = "x" * (provider_failures.LIMIT + 1)
        value = {"answer": 42}
        for provider, name, data in (
                ("claude", "process.stdout", json.dumps({"type": "result", "structured_output": value, "permission_denials": [
                    {"tool_name": "Edit", "tool_use_id": "tool_1", "tool_input": {"new_string": pad}}]}).encode()),
                ("codex", "response.json", json.dumps({**value, "summary": pad}).encode())):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                job = Path(folder)
                (job / name).write_bytes(data)
                reads = ReadsOf()
                with patch.object(provider_failures, "os", reads):
                    result = CLIBackend(None, None).completed_result(job, provider)
                self.assertEqual(result, value if provider == "claude" else {**value, "summary": pad})
                # Still the one read of read_original: never blocking, never through a link, a regular file only.
                self.assertEqual((reads.opens, sum(reads.reads)), ({name: 1}, len(data)))

    def test_the_stderr_tail_never_reads_past_its_size_when_the_file_grows(self):
        class Growing(Counted):
            def seek(self, offset, whence=os.SEEK_SET):
                if whence == os.SEEK_SET:
                    with open(path, "ab") as more:
                        more.write(b"b" * (1 << 20))
                return self.stream.seek(offset, whence)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "process.stderr"
            path.write_bytes(b"a" * 10000)
            reads = ReadsOf([path], wrap=lambda stream: Growing(stream.stream, stream.reads))
            with patch.object(provider_failures, "os", reads):
                state, text = provider_failures.read_original(path, tail_text)
        self.assertEqual((state, text), ("read", "[earlier output omitted; see log]\n" + "a" * 4000))
        self.assertEqual(sum(reads.reads), 4000)

    def test_a_capture_after_exit_0_reads_the_terminal_once(self):
        """The capture binds and decodes the same bytes: a second read of the terminal would fail here."""
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            (job / "process.stdout").write_text(json.dumps({"type": "result", "structured_output": {"answer": 42}}))
            outcome = {**NORMAL, "exit_code": 0, "evidence_complete": False}
            (job / "process.outcome.json").write_text(json.dumps(outcome))
            reads = ReadsOf(fail_open=("process.stdout",))
            with patch.object(provider_failures, "os", reads):
                captured = CLIBackend(None, None).preserve_terminal(job, "claude", outcome)
            capture = read_json(job / "terminal-capture.json")
            self.assertEqual((captured, reads.opens["process.stdout"]), (True, 1))
            self.assertEqual(read_json(job / "returned-result.json"), {"answer": 42})
            self.assertEqual(capture, {
                "version": 1, "provider": "claude", "source": "process.stdout",
                **{field: hashlib.sha256((job / name).read_bytes()).hexdigest() for field, name in (
                    ("source_digest", "process.stdout"), ("outcome_digest", "process.outcome.json"),
                    ("result_digest", "returned-result.json"))}})


class LargeReturnedResultTests(RepoCase):
    """A valid result in a Claude envelope over ``LIMIT`` (padded ``permission_denials``), exit 0, completes the run.

    The readers that always had their bound keep it: the diagnostics stay unknown and no usage is
    recorded. The CLI is a local child process.
    """

    def setUp(self):
        super().setUp()
        stub_provider_clis(self)
        self.config.data["roles"]["reviewer"] = {"cli": "claude", "model": "claude-sonnet-9-1", "effort": "high"}

    def test_every_role_completes_with_an_envelope_over_the_limit(self):
        for role in ("commander", "reviewer", "worker"):
            with self.subTest(role=role):
                self.setUp()
                config, repo, jobs = self.config, self.repo, []

                class Backend(DemoBackend):
                    def invoke(self, **kwargs):
                        value = super().invoke(**kwargs)
                        if kwargs["role"] != role:
                            return value
                        jobs.append(kwargs["job"])
                        terminal = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
                                    "structured_output": value}
                        program = (f"import json; terminal = json.loads({json.dumps(terminal)!r}); "
                                   "terminal['permission_denials'] = [{'tool_name': 'Edit', 'tool_use_id': 'tool_1', "
                                   f"'tool_input': {{'new_string': 'x' * {provider_failures.LIMIT}}}}}]; "
                                   "print(json.dumps(terminal))")
                        cli = CLIBackend(config, repo / ".harness")
                        cli.command = lambda *args: [sys.executable, "-c", program]
                        return cli.invoke(**kwargs)
                engine = Runtime(self.config, self.repo, backend=Backend(), **({"commander": "fable"} if role == "commander" else {}))
                run = engine.start("Implement greet", *(() if role == "commander" else (plan(),)))
                self.assertEqual((run["status"], run.get("stop")), ("completed", None))
                self.assertTrue(jobs)
                for job in jobs:
                    entry = next(d for d in run["dispatches"] if d["id"] == job.name)
                    self.assertGreater((job / "process.stdout").stat().st_size, provider_failures.LIMIT)
                    self.assertEqual((entry["status"], entry["exit_code"], entry["diagnostics"], "provider_usage" in entry),
                                     ("completed", 0, {"permission_denials": {"status": "unknown"}}, False))


class ReplayedReviewTests(RepoCase):
    """A Claude reviewer's transport failure while ``resume`` replays a sealed, never handed-off worker result."""

    def setUp(self):
        super().setUp()
        stub_provider_clis(self)
        self.config.data["roles"]["reviewer"] = {"cli": "claude", "model": "claude-sonnet-9-1", "effort": "high"}

    def test_replayed_review_transport_failure_reviews_again(self):
        config, repo = self.config, self.repo

        class Backend(DemoBackend):
            fail_review = False

            def invoke(self, **kwargs):
                if kwargs["role"] == "reviewer" and kwargs["kind"] == "review" and self.fail_review:
                    self.fail_review = False
                    cli = CLIBackend(config, repo / ".harness")
                    program = f"import sys; print({json.dumps(REAL_CONNECTION_REFUSED)!r}, flush=True); sys.exit(1)"
                    cli.command = lambda *args: [sys.executable, "-c", program]
                    return cli.invoke(**kwargs)
                return super().invoke(**kwargs)
        backend = Backend()
        publish, fired = TaskLifecycle.publish_worker_result, []

        def interrupted(lifecycle, *args, **kwargs):
            if not fired:
                fired.append(True)
                raise KeyboardInterrupt()
            return publish(lifecycle, *args, **kwargs)
        engine = self.engine(backend)
        with patch.object(TaskLifecycle, "publish_worker_result", interrupted), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        first = saved(engine)
        self.assertEqual([receipt["phase"] for receipt in first["worker_receipts"].values()], ["sealed"])
        backend.fail_review = True
        stopped = walk_exit(self, engine.directory, ("interrupted", "signal"), backend=backend,
                            expect_status="blocked", expect_code=3)
        self.assertEqual((stopped["stop"]["kind"], stopped["stop"]["reason"], stopped["stop"]["data"]["role"]),
                         ("provider_failure", "transport", "reviewer"))
        self.assertFalse(backend.fail_review)
        self.assertFalse(any("attempt_exemption" in d for d in stopped["dispatches"]))
        after = walk_exit(self, engine.directory, ("provider_failure", "transport"), backend=backend,
                          expect_status="completed")
        workers = [d for d in after["dispatches"] if d.get("role") == "worker" and d.get("invoked")]
        reviews = [d["id"] for d in after["dispatches"] if d.get("role") == "reviewer" and d.get("kind") == "review"]
        self.assertEqual(len(workers), 1)   # the sealed result is reused, the worker never dispatched again
        self.assertGreaterEqual(len(reviews), 2)
        self.assertEqual(len(reviews), len(set(reviews)))   # the failed review and the next one are new dispatches
        self.assertEqual(work_attempts(after), {"greet": 1})


if __name__ == "__main__":
    unittest.main()
