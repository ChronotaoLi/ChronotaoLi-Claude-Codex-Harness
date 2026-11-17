"""Quota sampling points, their admission, the sample files and their readers.

Runs use the real synchronous CLIBackend with local fixture processes for model calls
and recording stubs (quota_stubs) for both CLIs; RepoCase installs the default stubs.
All quota numbers are synthetic.
"""
import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import quota_stubs
from harness import cli, observe, quota, stops
from harness.adapters import DemoBackend
from harness.observe import Observer
from harness.run_store import RunStore
from harness.util import HarnessError, read_json
from test_dashboard import DashboardCase
from test_harness import ROOT, RepoCase, walk_exit
from test_provider_usage import FixtureCLIBackend
from test_quota_probes import TrackTemp, gone, group_gone
from test_runtime_changes import ScriptedBackend

QUOTA_ENVELOPE = {"is_error": True, "terminal_reason": "api_error", "api_error": "usage_limit_reached",
                  "api_error_status": 429}
FULL_WINDOW = [{"bucket": "weekly_scoped（Model Z）", "used_percent": 100, "resets_at": "2031-01-06T03:00:00+00:00"}]
QUOTA_FIELDS = ("quota_snapshot", "quota_sample", "quota_reason", "full_windows")
DISPATCH_FACTS = ("role", "kind", "task_id", "status", "invoked", "worker_receipt", "worker_receipt_consumed",
                  "worker_outcome", "attempt_exemption", "worker_return_evidence", "retry_of", "invalid_output")


def plan():
    return read_json(ROOT / "examples" / "plan.json")


def two_tasks():
    value = plan()
    value["tasks"].append(dict(value["tasks"][0], id="farewell", title="Farewell", depends_on=["greet"]))
    return value


def suffix(name):
    """``claude-before_use.json`` from a sample name."""
    return name.split("-", 4)[4]


class DemoResultBackend(FixtureCLIBackend):
    """Real CLIBackend calls (local fixture processes) answering with the offline demo's results, commander included."""

    def command(self, role, model, cwd, job, schema, run_path):
        argv = super().command(role, model, cwd, job, schema, run_path)
        with tempfile.TemporaryDirectory() as scratch:
            value = DemoBackend().invoke(role=role, model=model, cwd=Path(scratch), job=Path(scratch), schema=schema,
                                         prompt=(job / "prompt.txt").read_text(encoding="utf-8"), run_path=run_path,
                                         on_start=None)
        argv[-1] = json.dumps(value)
        return argv


class SamplingCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.probe_dirs = TrackTemp(self)

    def tearDown(self):
        # Whatever path a test took: every probe's group, every descendant a stub logged and
        # every probe directory are gone once the run returned or raised.
        for calls in self.__dict__.get("quota_installs", []):
            for call in calls.probes():
                self.assertTrue(group_gone(call["pid"]), call)
            for pid in calls.children("claude") + calls.children("codex"):
                self.assertTrue(gone(pid), pid)
        self.assertFalse([path for path in self.probe_dirs.paths if os.path.lexists(path)])
        super().tearDown()

    def backend(self, options=None):
        return DemoResultBackend(self.config, self.repo / ".harness", options)

    def folder(self, engine):
        return engine.directory / "observations" / "quota"

    def names(self, engine):
        folder = self.folder(engine)
        return sorted(path.name for path in folder.iterdir()) if folder.exists() else []

    def sample(self, engine, ending):
        return read_json(self.folder(engine) / next(name for name in self.names(engine) if name.endswith(ending)))

    def events(self, engine, event="quota_sampled"):
        return [record for line in (engine.directory / "events.jsonl").read_text().splitlines()
                if (record := json.loads(line))["event"] == event]

    def status(self, run_id, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--repo", str(self.repo), "status", run_id, *extra]), 0)
        return json.loads(output.getvalue())

    @staticmethod
    def decisions(run, directory):
        """What scheduling, budgets, receipts, reviews and acceptance decided, in full.

        Only this run's own identities are replaced: its ID and directory, dispatch IDs (by
        position), SHA-256 digests and commit IDs.
        """
        kept = {"status": run["status"], "attempts": run["attempts"], "agent_dispatches": run["agent_dispatches"],
                "dispatches": [{key: entry.get(key) for key in DISPATCH_FACTS} for entry in run["dispatches"]],
                **{key: run[key] for key in ("worker_receipts", "review_receipts", "commander_receipts", "accepted",
                                             "review_history", "final_history")}}
        text = json.dumps(kept, sort_keys=True).replace(str(directory), "RUN_DIR").replace(run["id"], "RUN")
        for index, entry in enumerate(run["dispatches"]):
            text = text.replace(entry["id"], f"DISPATCH_{index}")
        text = re.sub(r"\b[0-9a-f]{40}\b", "COMMIT", re.sub(r"\b[0-9a-f]{64}\b", "SHA256", text))
        return json.loads(text)

    def assert_probes_gone(self, calls):
        probes = calls.probes()
        self.assertTrue(probes)
        for call in probes:
            self.assertTrue(group_gone(call["pid"]), call)
        for pid in calls.children("claude") + calls.children("codex"):
            self.assertTrue(gone(pid), pid)


class LifecycleTests(SamplingCase):
    def test_a_completed_run_samples_before_first_use_and_after_completion(self):  # L1
        result = self.engine(self.backend()).start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")
        directory = self.repo / ".harness" / "runs" / result["id"]
        names = sorted(path.name for path in (directory / "observations" / "quota").iterdir())
        self.assertEqual([suffix(name) for name in names], ["claude-before_use.json", "codex-before_use.json",
                                                            "claude-completed.json", "codex-completed.json"])
        records = [quota.validate_sample(read_json(directory / "observations" / "quota" / name)) for name in names]
        self.assertEqual([(record["sequence"], record["role"]) for record in records],
                         [(1, "worker"), (2, "reviewer"), (3, None), (4, None)])
        self.assertTrue(all(record["available"] for record in records))
        # The Codex answer carries an account identifier; no sample file stores it.
        for name in names:
            text = (directory / "observations" / "quota" / name).read_text()
            self.assertNotIn(quota_stubs.ACCOUNT_ID, text)
            self.assertNotIn("accountId", text)
        self.assertEqual([record["ledger_revision"] for record in records[2:]], [result["store_revision"]] * 2)
        self.assertEqual(len(self.quota_calls.probes()), 4)
        self.assertEqual(self.quota_calls.unexpected(), [])
        sampled = [record for line in (directory / "events.jsonl").read_text().splitlines()
                   if (record := json.loads(line))["event"] == "quota_sampled"]
        self.assertEqual([record["sample"] for record in sampled], names)
        # Probes are not dispatches and the ledger keeps nothing of them.
        self.assertLessEqual({entry["role"] for entry in result["dispatches"]},
                             {"commander", "worker", "reviewer", "verification"})
        ledger = (directory / "state.json").read_text()
        # The only quota text in the ledger is the configured wait limit inside the configuration snapshot.
        self.assertNotIn("quota", ledger.replace('"quota_wait_max_seconds"', ""))
        self.assertNotIn("observations", ledger)

    def test_offline_backends_never_probe(self):  # L2
        for backend in (DemoBackend(), ScriptedBackend()):
            with self.subTest(backend=type(backend).__name__):
                engine = self.engine(backend)
                self.assertEqual(engine.start("Implement greet", plan())["status"], "completed")
                self.assertFalse((engine.directory / "observations").exists())
                self.assertEqual(self.events(engine), [])
        self.assertEqual(self.quota_calls.all(), [])

    def test_probe_failures_change_no_decision(self):  # L3
        engine = self.engine(self.backend())
        base = self.decisions(engine.start("Implement greet", plan()), engine.directory)
        self.assertEqual([entry["role"] for entry in base["dispatches"]], ["worker", "verification", "reviewer",
                                                                         "verification", "reviewer"])
        for claude, codex in (("hang", "hang"), ("big", "rpc_error"), ("not_zero_call", "shape"), ("exit", "orphan"),
                              ("child_ignores_term_leader_exits", "leader_waits_child_ignores")):
            with self.subTest(claude=claude, codex=codex), patch.object(quota, "QUERY_SECONDS", 1.0), \
                    patch.object(quota, "SETTLE_SECONDS", 0.5):
                calls = quota_stubs.install(self, claude=claude, codex=codex)
                engine = self.engine(self.backend())
                self.assertEqual(self.decisions(engine.start("Implement greet", plan()), engine.directory), base)
                self.assertEqual(len(self.names(engine)), 4)   # every sampling point still wrote its sample
                self.assert_probes_gone(calls)

    def test_a_failed_zero_call_proof_disables_claude_for_the_process(self):  # L4
        calls = quota_stubs.install(self, claude="not_zero_call")
        engine = self.engine(self.backend())
        engine.start("Implement greet", plan())
        self.assertEqual((len(calls.probes("claude")), len(calls.probes("codex"))), (1, 2))
        first = self.sample(engine, "claude-before_use.json")
        self.assertEqual((first["reason"], first["zero_call"]["verified"]), ("not_zero_call", False))
        last = self.sample(engine, "claude-completed.json")
        self.assertEqual((last["reason"], last["detail"], last["process"], last["source"]["command"]),
                         ("probe_disabled", {"disabled_by": quota.sample_name(first)}, None, []))
        self.assertTrue(self.sample(engine, "codex-completed.json")["available"])

    def test_a_claude_cli_that_closes_its_output_late_keeps_probing(self):  # the 2026-11-17 timing
        # The real CLI keeps its output open after the result line. That is a verified answer, not a
        # timeout, even when the CLI outlasts the settle time (shortened here) and the cleanup ends it:
        # the process keeps sampling Claude.
        calls = quota_stubs.install(self, claude="linger_forever")
        engine = self.engine(self.backend())
        with patch.object(quota, "SETTLE_SECONDS", 1.0):
            self.assertEqual(engine.start("Implement greet", plan())["status"], "completed")
        self.assertEqual((len(calls.probes("claude")), len(calls.probes("codex"))), (2, 2))
        for ending in ("claude-before_use.json", "claude-completed.json"):
            sample = self.sample(engine, ending)
            self.assertEqual((sample["available"], sample["zero_call"]["verified"], sample["process"]["termination"]),
                             (True, True, "normal"), ending)
            self.assertIs(sample["process"]["exited_before_signal"], False if hasattr(os, "waitid") else None)
        self.assertEqual(engine.services.quota.disabled, {})
        self.assert_probes_gone(calls)

    def test_a_verified_call_with_an_unreadable_shape_keeps_probing(self):  # L4b
        calls = quota_stubs.install(self, claude="shape")
        engine = self.engine(self.backend())
        engine.start("Implement greet", plan())
        self.assertEqual(len(calls.probes("claude")), 2)
        first = self.sample(engine, "claude-before_use.json")
        self.assertEqual((first["reason"], first["zero_call"]["verified"]), ("shape_mismatch", True))
        self.assertEqual(self.sample(engine, "claude-completed.json")["reason"], "shape_mismatch")

    def test_an_unconfirmed_probe_exit_disables_that_provider(self):  # L5
        calls = quota_stubs.install(self)
        original = quota.ProbeProcess._stop_group

        def unconfirmed(probe, interrupted):
            original(probe, interrupted)
            if probe.argv[0] == "codex":
                probe.facts["exit_confirmed"] = False   # the group was still observed at the deadline
        engine = self.engine(self.backend())
        with patch.object(quota.ProbeProcess, "_stop_group", unconfirmed):
            self.assertEqual(engine.start("Implement greet", plan())["status"], "completed")
        self.assertEqual((len(calls.probes("claude")), len(calls.probes("codex"))), (2, 1))
        self.assertFalse(self.sample(engine, "codex-before_use.json")["process"]["exit_confirmed"])
        self.assertEqual(self.sample(engine, "codex-completed.json")["reason"], "probe_disabled")

    def test_a_sample_write_failure_is_an_observation_gap(self):  # L6
        reference = self.engine(self.backend())
        base = self.decisions(reference.start("Implement greet", plan()), reference.directory)
        engine = self.engine(self.backend())
        with patch("harness.quota.publish_new_json", side_effect=OSError("synthetic")):
            result = engine.start("Implement greet", plan())
        self.assertEqual(self.decisions(result, engine.directory), base)   # the same run, decision for decision
        sampled = self.events(engine)
        self.assertEqual([(record["sample"], record["write_error"]) for record in sampled], [(None, "OSError")] * 4)
        self.assertEqual(self.names(engine), [])
        self.assertEqual(len(self.probe_dirs.paths), 8)   # four probes per run; tearDown checks each left nothing

    def test_an_interrupt_in_the_first_probe_stops_before_the_dispatch_exists(self):  # L7
        calls = quota_stubs.install(self, claude="sigint_parent")
        engine = self.engine(self.backend())
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        ledger = read_json(engine.directory / "state.json")
        self.assertEqual((ledger["status"], ledger["stop"]["kind"], ledger["stop"]["reason"]),
                         ("interrupted", "interrupted", "signal"))
        self.assertEqual((ledger["agent_dispatches"], [entry for entry in ledger["dispatches"]
                                                       if entry["role"] == "worker"]), (0, []))
        self.assertEqual(self.names(engine), [])
        self.assertTrue(group_gone(calls.probes("claude")[-1]["pid"]))
        quota_stubs.install(self)
        self.assertEqual(self.engine(self.backend()).resume(ledger["id"])["status"], "completed")

    def test_an_interrupt_in_an_end_sample_keeps_the_saved_completion(self):  # L8
        calls = quota_stubs.install(self, claude="sigint_after:1")
        engine = self.engine(self.backend())
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        self.assertEqual(read_json(engine.directory / "state.json")["status"], "completed")
        self.assertEqual([suffix(name) for name in self.names(engine)],
                         ["claude-before_use.json", "codex-before_use.json"])
        self.assertTrue(group_gone(calls.probes("claude")[-1]["pid"]))

    def test_the_stop_is_saved_before_its_end_sample_starts(self):  # L9
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        calls = quota_stubs.install(self, claude="hang_after:1", state=self.repo / ".harness")
        engine = self.engine(self.backend())
        with patch.object(quota, "QUERY_SECONDS", 1.0), self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        self.assertEqual(stops.of(caught.exception)["reason"], "agent_dispatches")
        states = [seen[0] for seen in calls.states()]
        self.assertEqual([(state["status"], (state["stop"] or {}).get("kind")) for state in states],
                         [("running", None), ("blocked", "budget")])
        events = [record["event"] for line in (engine.directory / "events.jsonl").read_text().splitlines()
                  if (record := json.loads(line))]
        self.assertLess(events.index("run_stopped"), len(events) - 1 - events[::-1].index("quota_sampled"))
        # Codex was sampled before its first use but never called: no end sample for it.
        self.assertEqual([suffix(name) for name in self.names(engine)],
                         ["claude-before_use.json", "codex-before_use.json", "claude-stop.json"])
        self.assertEqual(self.sample(engine, "claude-stop.json")["reason"], "timeout")

    def test_pause_and_plan_approval_sample_the_providers_used(self):  # L10
        result = self.engine(self.backend()).start("Implement greet", two_tasks(), step=True)
        self.assertEqual(result["status"], "paused")
        folder = self.repo / ".harness" / "runs" / result["id"] / "observations" / "quota"
        self.assertEqual([suffix(path.name) for path in sorted(folder.iterdir())],
                         ["claude-before_use.json", "codex-before_use.json", "claude-paused.json", "codex-paused.json"])
        engine = self.engine(self.backend())
        self.assertEqual(engine.start("Implement greet", None, pause_after_plan=True)["status"], "awaiting_approval")
        # Only the commander (codex) was called before the plan waits for approval.
        self.assertEqual([suffix(name) for name in self.names(engine)],
                         ["codex-before_use.json", "codex-awaiting_approval.json"])

    def test_an_interrupted_stop_takes_no_end_sample(self):  # L11
        backend = self.backend()
        real = backend.invoke

        def invoke(**kwargs):
            value = real(**kwargs)
            if kwargs["role"] == "worker":
                raise KeyboardInterrupt()   # after the call: claude was used
            return value
        backend.invoke = invoke
        engine = self.engine(backend)
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        self.assertEqual(read_json(engine.directory / "state.json")["status"], "interrupted")
        self.assertEqual([suffix(name) for name in self.names(engine)], ["claude-before_use.json"])
        self.assertEqual(len(self.quota_calls.probes()), 1)


class QuotaFailureTests(SamplingCase):
    def quota_failure(self, role="worker", **install):
        calls = quota_stubs.install(self, state=self.repo / ".harness", **install)
        engine = self.engine(self.backend({role: {"envelope": QUOTA_ENVELOPE, "exit_code": 1}}))
        return calls, engine

    def test_a_quota_failure_stop_carries_its_sample_and_full_windows(self):  # L12
        calls, engine = self.quota_failure()
        with self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        ledger = read_json(engine.directory / "state.json")
        stop = ledger["stop"]
        names = self.names(engine)
        self.assertEqual([suffix(name) for name in names], ["claude-before_use.json", "claude-quota_failure.json"])
        self.assertEqual((stop["kind"], stop["reason"]), ("provider_quota", "usage_limit"))
        self.assertEqual({key: stop["data"].get(key) for key in QUOTA_FIELDS},
                         {"quota_snapshot": "available", "quota_sample": names[1], "quota_reason": None,
                          "full_windows": FULL_WINDOW})
        self.assertEqual(stops.of(caught.exception)["data"]["quota_sample"], names[1])
        self.assertEqual(stops.project_ledger(ledger)["exits"], [f"resume {ledger['id']}"])
        failure = self.sample(engine, "claude-quota_failure.json")
        self.assertEqual((failure["dispatch_id"], failure["role"]), (stop["source"]["dispatch_id"], None))
        # The worker's judgment was on disk when the probe started; the stop was not yet.
        seen = calls.states()[-1][0]
        self.assertEqual((seen["status"], seen["stop"]), ("running", None))
        self.assertEqual(seen["dispatches"][-1]["consumed"], "provider_failure")
        self.assertEqual(failure["ledger_revision"], seen["store_revision"])
        self.assertEqual(len(calls.probes("claude")), 2)   # no end sample right after the failure sample
        walk_exit(self, engine.directory, ("provider_quota", "usage_limit"), backend=self.backend(),
                  expect_status="completed")

    def test_a_commander_quota_failure_is_sampled_after_its_error_record(self):  # L12 (commander)
        self.config.data["roles"]["commander"]["cli"] = "claude"
        calls, engine = self.quota_failure("commander")
        with self.assertRaises(HarnessError):
            engine.start("Implement greet")
        stop = read_json(engine.directory / "state.json")["stop"]
        self.assertEqual((stop["kind"], stop["reason"], stop["data"]["role"]), ("provider_quota", "usage_limit",
                                                                               "commander"))
        self.assertEqual(stop["data"]["quota_snapshot"], "available")
        seen = calls.states()[-1][0]
        self.assertEqual((seen["status"], seen["stop"]), ("planning", None))   # the first plan is not recorded yet
        self.assertEqual([(entry["role"], entry["status"]) for entry in seen["dispatches"]],
                         [(entry["role"], entry["status"]) for entry in read_json(
                             engine.directory / "state.json")["dispatches"]])

    def test_an_unavailable_failure_sample_still_lets_the_stop_save(self):  # L13
        calls, engine = self.quota_failure(claude="hang_after:1")
        marks = {}
        # The failure sample is taken first by the quota wait decision (harness.quota_wait), whatever follows.
        annotate, commit = quota.QuotaSampler.failure_sample, RunStore.commit

        def timed_annotate(sampler, error):
            marks.setdefault("raised", time.monotonic())
            return annotate(sampler, error)

        def timed_commit(store):
            value = commit(store)
            if store.run.get("status") == "blocked":
                marks.setdefault("saved", time.monotonic())
            return value
        # A shortened query time keeps the test fast; the bound is that time plus the cleanup's.
        with patch.object(quota, "QUERY_SECONDS", 1.0), \
                patch.object(quota.QuotaSampler, "failure_sample", timed_annotate), \
                patch.object(RunStore, "commit", timed_commit), self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        stop = read_json(engine.directory / "state.json")["stop"]
        self.assertEqual({key: stop["data"].get(key) for key in QUOTA_FIELDS},
                         {"quota_snapshot": "unavailable", "quota_sample": self.names(engine)[-1],
                          "quota_reason": "timeout", "full_windows": None})
        self.assertLess(marks["saved"] - marks["raised"], 1.0 + quota.CLEANUP_SECONDS + 1)

    def test_an_interrupt_in_the_failure_probe_stops_as_an_interrupt(self):  # L14
        calls, engine = self.quota_failure(claude="sigint_after:1")
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        ledger = read_json(engine.directory / "state.json")
        self.assertEqual((ledger["status"], ledger["stop"]["kind"]), ("interrupted", "interrupted"))
        self.assertEqual(ledger["dispatches"][-1]["worker_receipt_consumed"], "provider_failure")   # judgment kept
        self.assertEqual([suffix(name) for name in self.names(engine)], ["claude-before_use.json"])
        self.assertTrue(group_gone(calls.probes("claude")[-1]["pid"]))
        quota_stubs.install(self)
        self.assertEqual(self.engine(self.backend()).resume(ledger["id"])["status"], "completed")

    def test_an_unpublished_failure_sample_is_named_so(self):  # L15
        calls, engine = self.quota_failure()
        real = quota.publish_new_json

        def publish(path, value):
            if value["trigger"] == "quota_failure":
                raise OSError("synthetic")
            return real(path, value)
        with patch("harness.quota.publish_new_json", side_effect=publish), self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        stop = read_json(engine.directory / "state.json")["stop"]
        self.assertEqual({key: stop["data"].get(key) for key in QUOTA_FIELDS},
                         {"quota_snapshot": "not_published", "quota_sample": None, "quota_reason": None,
                          "full_windows": FULL_WINDOW})
        failure = [record for record in self.events(engine) if record["trigger"] == "quota_failure"]
        self.assertEqual([(record["sample"], record["write_error"]) for record in failure], [(None, "OSError")])

    def test_settling_into_a_load_failure_drops_the_quota_fields(self):  # L16
        calls, engine = self.quota_failure()
        annotate = quota.QuotaSampler.annotate_failure

        def annotate_then_damage(sampler, error):
            value = annotate(sampler, error)
            entry = next(entry for entry in sampler.context.run["dispatches"] if entry.get("worker_receipt"))
            Path(entry["worker_receipt"]["packet_artifact"]).write_text("{}\n")   # before Workflow.stop settles
            return value
        with patch.object(quota.QuotaSampler, "annotate_failure", annotate_then_damage), \
                self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        stop = read_json(engine.directory / "state.json")["stop"]
        self.assertEqual((stop["kind"], stop["reason"]), ("evidence", "load_integrity"))
        self.assertEqual(stop["data"]["superseded"], {"kind": "provider_quota", "reason": "usage_limit"})
        self.assertFalse(set(QUOTA_FIELDS) & set(stop["data"]))
        self.assertTrue(any(name.endswith("claude-quota_failure.json") for name in self.names(engine)))


class AdmissionTests(SamplingCase):
    def budget_stop(self, **install):
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        calls = quota_stubs.install(self, **install)
        return calls, self.engine(self.backend())

    def test_a_failed_stop_save_starts_no_end_probe_and_keeps_its_error(self):  # A1
        calls, engine = self.budget_stop(claude="hang_after:1")
        real = RunStore.commit

        def failing(store):
            if store.run.get("status") == "blocked":
                raise OSError("synthetic stop save failure")
            return real(store)
        with patch.object(RunStore, "commit", failing), self.assertRaises(OSError) as caught:
            engine.start("Implement greet", plan())
        self.assertEqual(str(caught.exception), "synthetic stop save failure")
        self.assertEqual(len(calls.probes("claude")), 1)
        self.assertEqual(read_json(engine.directory / "state.json")["status"], "running")

    def test_no_end_probe_beside_a_model_process_that_may_be_alive(self):  # A2
        with self.subTest("the supervisor and the gate both see the group"):
            calls = quota_stubs.install(self)
            engine = self.engine(self.backend())
            alive = lambda pgid: True   # every observer of the worker's group still sees it
            with patch("harness.process_runtime.ProcessSupervisor._observe", lambda supervisor, final=True: True), \
                    patch("harness.util.group_alive", alive), patch("harness.dispatch.group_alive", alive), \
                    patch("harness.run_context.group_alive", alive), self.assertRaises(HarnessError):
                engine.start("Implement greet", plan())
            ledger = read_json(engine.directory / "state.json")
            self.assertEqual(ledger["status"], "blocked")
            self.assertFalse(ledger["dispatches"][-1]["exit_confirmed"])
            self.assertEqual(len(calls.probes()), 1)   # before_use only
        with self.subTest("an ordinary stop while the exit gate fails"):
            calls, engine = self.budget_stop()
            with patch.object(quota.QuotaSampler, "no_live_model_process", return_value=False), \
                    self.assertRaises(HarnessError):
                engine.start("Implement greet", plan())
            self.assertEqual([suffix(name) for name in self.names(engine)],
                             ["claude-before_use.json", "codex-before_use.json"])

    def test_an_exit_unconfirmed_final_stop_is_not_sampled_even_if_the_group_is_gone(self):  # A3
        context = FakeContext(self, status="blocked")
        sampler = quota.QuotaSampler(context)
        sampler.mark_used("claude")
        sampler.terminal_saved()
        unconfirmed = stops.make("process_unconfirmed", "dispatch_exit", data={"returned_unsealed": False},
                                 source={"dispatch_id": "d1"})
        with self.assertRaises(HarnessError):
            try:
                raise HarnessError("exit not confirmed", stop=unconfirmed)
            finally:
                sampler.end_of_call()
        self.assertEqual(context.snapshots, [])
        sampler.mark_used("claude")
        sampler.terminal_saved()
        with self.assertRaises(HarnessError):
            try:
                raise HarnessError("budget", stop=stops.make("budget", "task_attempts"))
            finally:
                sampler.end_of_call()
        self.assertEqual(context.snapshots, ["claude"])   # an ordinary stop after the same save is sampled

    def test_an_ordinary_error_in_end_sampling_never_replaces_the_stop(self):  # A4
        calls, engine = self.budget_stop()
        with patch.object(quota.QuotaSampler, "no_live_model_process", side_effect=RuntimeError("synthetic")), \
                self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        self.assertEqual(stops.of(caught.exception)["reason"], "agent_dispatches")
        self.assertEqual(len(calls.probes("claude")), 1)

    def test_an_interrupt_in_end_sample_cleanup_replaces_the_stop_but_not_the_ledger(self):  # A5
        calls, engine = self.budget_stop(claude="sigint_on_term_after:1")
        with patch.object(quota, "SETTLE_SECONDS", 0.5), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        self.assertEqual(read_json(engine.directory / "state.json")["status"], "blocked")
        self.assertFalse(any(name.endswith("claude-stop.json") for name in self.names(engine)))
        self.assertEqual(len(calls.probes("claude")), 2)
        self.assertTrue(group_gone(calls.probes("claude")[-1]["pid"]))


class ObservationTests(DashboardCase, SamplingCase):
    def completed(self, **install):
        if install:
            quota_stubs.install(self, **install)
        result = self.engine(self.backend()).start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")
        return result["id"], self.repo / ".harness" / "runs" / result["id"] / "observations" / "quota"

    def test_observation_entries_never_start_a_cli(self):  # L17
        run_id, folder = self.completed()
        name = sorted(path.name for path in folder.iterdir())[0]
        calls = quota_stubs.install(self)
        shown = self.status(run_id)["quota"]
        # One projection for status and the page (V-详情): quota and invocation classes, no money.
        detail = observe.run_detail(self.repo / ".harness", run_id)
        self.assertEqual(detail["quota"], shown)
        self.assertEqual(shown["providers"]["codex"]["credits"]["codex"]["latest"]["balance"], "41.25")
        server = self.start_server()
        for _ in range(3):   # every refresh reads the saved samples again, never a CLI
            for path in (f"/api/runs/{run_id}", f"/api/runs/{run_id}/files", f"/api/runs/{run_id}/events",
                         f"/api/runs/{run_id}/files/observations/quota/{name}"):
                self.assertEqual(self.request(server, path)[0], 200, path)
        served = json.loads(self.request(server, f"/api/runs/{run_id}")[2])
        self.assertEqual((served["quota"], "cost" in served, "invocations" in served), (shown, False, True))
        self.assertEqual(calls.all(), [])

    def test_damaged_samples_change_neither_recovery_nor_the_rest_of_status(self):  # L18
        result = self.engine(self.backend()).start("Implement greet", two_tasks(), step=True)
        folder = self.repo / ".harness" / "runs" / result["id"] / "observations" / "quota"
        names = sorted(path.name for path in folder.iterdir())
        (folder / names[0]).unlink()
        (folder / names[1]).write_text("{")
        (folder / names[2]).unlink()
        os.symlink(folder / names[3], folder / names[2])
        mismatched = read_json(folder / names[3])
        (folder / names[3]).write_text(json.dumps({**mismatched, "sequence": 99}))
        (folder / "r0000000000-p0-20310101T000000.000000Z-s0099-claude-stop.json").write_text(
            " " * (quota.SAMPLE_MAX_BYTES + 1))
        shown = self.status(result["id"])
        self.assertEqual(shown["status"], "paused")
        self.assertEqual((shown["quota"]["damaged_count"], shown["quota"]["providers"]),
                         (3, {"claude": {"state": "no_samples"}, "codex": {"state": "no_samples"}}))
        block = Observer(self.repo / ".harness").quota_samples(result["id"])
        self.assertEqual((block["skipped"], len(block["damaged"])), (1, 3))
        server = self.start_server()
        self.assertEqual(self.request(server, f"/api/runs/{result['id']}")[0], 200)
        self.assertEqual(self.engine(self.backend()).resume(result["id"])["status"], "completed")
        self.assertEqual(self.status(result["id"])["quota"]["providers"]["claude"]["state"], "available")

    def test_status_shows_the_latest_observation_per_provider_and_bucket(self):  # L19
        run_id, _ = self.completed(codex="rpc_error")
        shown = self.status(run_id)["quota"]
        self.assertEqual(shown["notes"], list(quota.NOTES) + list(quota.VIEW_NOTES))
        claude, codex = shown["providers"]["claude"], shown["providers"]["codex"]
        self.assertEqual((claude["state"], claude["samples"], claude["unavailable"]), ("available", 2, {}))
        self.assertEqual({key: claude["latest_sample"][key] for key in ("trigger", "available", "reason")},
                         {"trigger": "completed", "available": True, "reason": None})
        self.assertEqual([(bucket["bucket"], bucket["identity"], bucket["latest"]["used_percent"],
                           bucket["latest"]["resets_at"]) for bucket in claude["buckets"]],
                         [("session", ["claude", "session", "null"], 37, "2031-01-02T03:00:00+00:00"),
                          ("weekly_all", ["claude", "weekly_all", "null"], 61, "2031-01-06T03:00:00+00:00"),
                          ("weekly_scoped（Model Z）",
                           ["claude", "weekly_scoped", '{"model":{"display_name":"Model Z"},"surface":null}'],
                           100, "2031-01-06T03:00:00+00:00"),
                          (f"weekly_scoped（{quota_stubs.FAMILY_DISPLAY}）",
                           ["claude", "weekly_scoped",
                            '{"model":{"display_name":"%s"},"surface":null}' % quota_stubs.FAMILY_DISPLAY],
                           12, "2031-01-06T03:00:00+00:00")])
        self.assertFalse(claude["extra_usage"]["is_enabled"])
        self.assertEqual({key: codex[key] for key in ("state", "samples", "unavailable", "buckets", "credits",
                                                      "ordinary_usage_allowed")},
                         {"state": "all_unavailable", "samples": 2, "unavailable": {"provider_error": 2},
                          "buckets": [], "credits": {}, "ordinary_usage_allowed": None})
        self.assertNotIn("total", json.dumps(shown))
        self.assertNotIn("quota", self.status(run_id, "--full"))
        run_id, _ = self.completed(codex="ok")
        codex = self.status(run_id)["quota"]["providers"]["codex"]
        self.assertEqual(codex["credits"]["codex"]["latest"]["balance"], "41.25")
        self.assertEqual(codex["buckets"][0]["bucket"], "codex.primary（4320 分钟）")

    def test_samples_are_listed_and_readable_files(self):  # L20
        run_id, folder = self.completed()
        names = sorted(path.name for path in folder.iterdir())
        (folder / (names[0] + ".0123.tmp")).write_text("partial")
        with Observer(self.repo / ".harness") as observer:
            listed = [item["path"] for item in observer.file_index(run_id)["files"]]
            shown = observer.read_file(run_id, f"observations/quota/{names[0]}")
        self.assertEqual([path for path in listed if path.startswith("observations/")],
                         [f"observations/quota/{name}" for name in names])
        self.assertEqual(json.loads(shown["text"]), read_json(folder / names[0]))


class FakeContext:
    """The parts of RunContext a QuotaSampler uses; the backend records snapshot requests."""

    def __init__(self, case, *, status="blocked", revision=5, observed=None):
        self.directory = Path(tempfile.mkdtemp()).resolve()   # a run directory is a canonical absolute path
        case.addCleanup(shutil.rmtree, self.directory, True)
        self.run = {"status": status, "store_revision": revision, "dispatches": []}
        self.operation, self.events, self.snapshots = None, [], []

        def snapshot(provider):
            self.snapshots.append(provider)
            return observed(provider) if observed else dict(synthetic_observed(provider))
        self.backend = SimpleNamespace(quota_snapshot=snapshot)

    def observe(self, event, **fields):
        self.events.append((event, fields))


def synthetic_observed(provider, percent=10, at=1900000000.0):
    window = {"kind": "weekly_all", "group": "weekly", "scope": None, "scope_model": None, "used_percent": percent,
              "resets_at": 2000000000.0, "resets_at_raw": None, "is_active": True, "severity": None}
    return {"provider": provider, "source": {"command": [], "cli_version": None}, "queried_at": at,
            "observed_at": at, "available": True, "reason": None, "detail": None, "process": None,
            "zero_call": None, "windows": [window], "limits": [], "ordinary_usage_allowed": None,
            "extra_usage": None, "unparsed": []}


def synthetic(revision, observed, sequence, *, trigger="stop", percent=10, started=1900000000.0):
    head = {"version": 1, "provider": "claude", "trigger": trigger, "dispatch_id": None, "role": None,
            "operation_id": None, "ledger_revision": revision, "process_started_at": started, "sequence": sequence}
    record = quota._assemble(head, synthetic_observed("claude", percent, observed))
    assert record["available"], record   # a fixture that failed validation would be an internal_error sample
    return record


class OrderingTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state, True)
        self.folder = self.state / "runs" / "run_abc" / "observations" / "quota"
        self.folder.mkdir(parents=True)

    def write(self, record):
        quota.publish_new_json(self.folder / quota.sample_name(record), record)

    def read(self):
        return Observer(self.state).quota_samples("run_abc")

    def test_truncation_keeps_the_highest_revision_even_when_its_clock_went_back(self):  # O1
        for revision in range(1, 401):
            moment = 1900000000.0 + revision * 60
            self.write(synthetic(revision, moment, 1, started=moment))
        self.write(synthetic(401, 1800000000.0, 1, percent=20, started=1800000000.0))   # the clock went back
        block = self.read()
        self.assertEqual((block["omitted"], len(block["samples"]), block["samples"][-1]["ledger_revision"]),
                         (1, 400, 401))
        latest = quota.quota_view(block["samples"])["providers"]["claude"]["buckets"][0]["latest"]
        self.assertEqual(latest["used_percent"], 20)

    def test_one_revision_orders_by_phase_then_sequence_not_by_the_clock(self):  # O2
        self.write(synthetic(5, 1900000200.0, 7, trigger="stop", percent=1, started=1900000100.0))
        self.write(synthetic(5, 1899999500.0, 1, trigger="before_use", percent=2, started=1899999000.0))
        self.write(synthetic(5, 1899999400.0, 2, trigger="before_use", percent=3, started=1899999000.0))
        self.assertEqual([sample["windows"][0]["used_percent"] for sample in self.read()["samples"]], [1, 2, 3])

    def test_a_sample_is_never_replaced_and_a_name_must_match_its_content(self):  # O3
        record = synthetic(9, 1900000000.0, 1)
        self.write(record)
        with self.assertRaises(FileExistsError):
            self.write({**record, "windows": []})
        self.assertEqual(read_json(self.folder / quota.sample_name(record))["windows"][0]["used_percent"], 10)
        (self.folder / quota.sample_name(synthetic(11, 1900000001.0, 1))).write_text(
            json.dumps(synthetic(10, 1900000001.0, 1)))
        self.assertEqual(self.read()["damaged_count"], 1)
        # Through the sampler: the same name again, and a revision beyond ten digits.
        for revision, error in ((5, "FileExistsError"), (quota.REVISION_MAX + 1, "ValueError")):
            with self.subTest(revision=revision):
                context = FakeContext(self, revision=revision)
                first, second = quota.QuotaSampler(context), quota.QuotaSampler(context)
                second.process_started_at = first.process_started_at
                if revision == 5:
                    self.assertIsNotNone(first.sample("claude", "stop")["file"])
                self.assertIsNone(second.sample("claude", "stop")["file"])
                self.assertEqual(context.events[-1][1]["write_error"], error)


class RegistrationTests(unittest.TestCase):
    def failure(self, **data):
        return stops.make("provider_quota", "usage_limit",
                          data={"role": "worker", "provider": "claude", "http_status": 429, **data},
                          source={"dispatch_id": "d1", "task_id": "greet"})

    def test_quota_fields_are_registered_bounded_and_list_no_exit(self):  # L21
        window = {"bucket": "weekly_scoped（Model Z）", "used_percent": 100, "resets_at": "2031-01-06T03:00:00+00:00"}
        value = self.failure(quota_snapshot="available", quota_sample="r0000000003-p0-20310101T000000.000000Z-"
                             "s0003-claude-quota_failure.json", full_windows=[window])
        stops.validate(value)
        self.assertEqual(stops.project(value, "run_x")["exits"], ["resume run_x"])
        self.assertEqual(stops.project(value, "run_x")["data"]["full_windows"], [window])
        many = self.failure(quota_snapshot="available", full_windows=[dict(window, bucket=f"b{i}") for i in range(20)])
        stops.validate(many)
        self.assertEqual((len(many["data"]["full_windows"]), many["data"]["full_windows_omitted"]), (16, 4))
        # Like every list field, an unfit item ends the kept head; the stop itself stays valid.
        for unfit in (dict(window, used_percent=float("inf")), dict(window, extra=1), dict(window, bucket="x" * 161)):
            with self.subTest(unfit=sorted(unfit)):
                value = self.failure(full_windows=[window, unfit, window])
                stops.validate(value)
                self.assertEqual((value["data"]["full_windows"], value["data"]["full_windows_omitted"]), ([window], 2))
        wide = self.failure(full_windows=[dict(window, bucket="长" * 160)] * 8)   # over 4 KiB escaped
        stops.validate(wide)
        self.assertLessEqual(len(json.dumps(wide["data"]["full_windows"])), 4096)
        self.assertGreater(wide["data"]["full_windows_omitted"], 0)
        dropped = self.failure(quota_snapshot="guessed", quota_reason="busy", quota_sample="../x")
        self.assertEqual(dropped["data"]["dropped_fields"], ["quota_reason", "quota_sample", "quota_snapshot"])
        self.assertEqual((dropped["kind"], stops.project(dropped, "run_x")["exits"]),
                         ("provider_quota", ["resume run_x"]))
        with self.assertRaises(KeyError):
            stops.make("provider_failure", "unknown", data={"quota_snapshot": "available"})

    def test_a_large_quota_stop_keeps_its_type_and_exit_after_the_diagnostic(self):  # review suggestion 1
        # A long path of CJK characters, a full and wide list of windows, a sample reference, then a
        # 2,000-character diagnostic outside the BMP (twelve bytes each, escaped): all synthetic.
        source = {"artifact": "/tmp/" + "/".join(["长" * 50] * 15) + "/process.stdout", "artifact_digest": "a" * 64,
                  "dispatch_id": "d" * 36, "task_id": "greet"}
        windows = [{"bucket": "窗" * 160, "used_percent": 100, "resets_at": None}] * 16
        value = stops.make("provider_quota", "usage_limit", source=source, data={
            "role": "worker", "provider": "claude", "http_status": 429, "quota_snapshot": "available",
            "quota_sample": "r0000000003-p0-20310101T000000.000000Z-s0003-claude-quota_failure.json",
            "full_windows": windows})
        error = HarnessError("\U0001f600" * 2000, stop=value)
        final = stops.of(error)
        stops.validate(final)
        self.assertEqual((final["kind"], final["reason"], stops.exit_code(error)), ("provider_quota", "usage_limit", 3))
        self.assertEqual(stops.project(final, "run_x")["exits"], ["resume run_x"])
        self.assertEqual((final["data"]["quota_snapshot"], final["data"]["quota_sample"], final["source"]),
                         ("available", value["data"]["quota_sample"], source))
        self.assertTrue(final["data"]["full_windows"])
        self.assertEqual(len(final["data"]["full_windows"]) + final["data"]["full_windows_omitted"], 16)


if __name__ == "__main__":
    unittest.main()
