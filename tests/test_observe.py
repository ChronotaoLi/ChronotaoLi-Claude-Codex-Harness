"""Read-only observation data layer: ledger projection, invocation classes, events, operations."""
import contextlib
import dis
import errno
import functools
import gc
import inspect
import io
import itertools
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from harness import stops
from harness import cli, dashboard, git, observe, process_runtime, watch_cli
from harness.cli import status_summary
from harness.util import read_json, write_json
from harness.versions import RUN_SCHEMA_VERSION
from test_harness import ROOT, RepoCase, cli_notice, scratch, write_project_config

ROLES = {"commander": {"cli": "codex", "model": "gpt-6.2-astra", "effort": "high"},
         "fable": {"cli": "claude", "model": "claude-fable-5-2", "effort": "high"},
         "worker": {"cli": "claude", "model": "claude-opus-5-5", "effort": "high"},
         "reviewer": {"cli": "codex", "model": "gpt-6.2-sol", "effort": "high"}}


def ledger(run_id, **fields):
    base = {"schema_version": RUN_SCHEMA_VERSION, "id": run_id, "status": "running", "goal": "Implement greet",
            "branch": "harness/" + run_id, "worktree": "/tmp/worktree", "commander": "astra",
            "tasks": [{"id": "greet", "title": "Implement greet", "depends_on": []},
                      {"id": "planning", "title": "A user task that happens to be named planning", "depends_on": ["greet"]}],
            "accepted": {"greet": {"commit": "abc"}}, "attempts": {"greet": 1},
            "dispatches": [], "config_snapshot": {"roles": ROLES, "runtime": {"max_attempts": 3,
                                                                               "max_agent_dispatches": 200}},
            "agent_dispatches": 1, "decision_requests": [], "operator_decisions": [], "created_by_operation": "op_a"}
    base.update(fields)
    if base["status"] in stops.STOP_STATUSES and "stop" not in fields:
        base["stop"] = (stops.make("plan_approval", "initial_plan") if base["status"] == "awaiting_approval" else
                        stops.make("interrupted", "signal") if base["status"] == "interrupted" else
                        stops.make("unclassified", "untyped_harness_error", data={"error_type": "HarnessError"}))
    return base


def dispatch(role="worker", kind="implement", task_id="greet", **fields):
    entry = {"id": str(uuid.uuid4()), "role": role, "kind": kind, "task_id": task_id, "status": "completed",
             "pgid": None, "process_state": "exited", "invoked": True, "exit_confirmed": True}
    entry.update(fields)
    return entry


class StateCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / ".harness"
        self.state.mkdir()

    def save(self, run):
        write_json(self.state / "runs" / run["id"] / "state.json", run)
        return run["id"]

    def rows(self, run):
        detail = observe.run_detail(self.state, self.save(run))
        return detail, {row["id"]: row for row in detail["dispatches"]}


class DemoRunObservationTests(RepoCase):
    def test_demo_run_list_and_detail_match_status_summary(self):
        engine = self.engine()
        result = engine.start("Implement greet")
        state = self.repo / ".harness"
        ledger_value = read_json(engine.directory / "state.json")
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]

        listing = observe.list_runs(state)
        self.assertEqual(listing["skipped"], [])
        self.assertEqual(listing["incomplete"], [])
        self.assertEqual([item["id"] for item in listing["runs"]], [result["id"]])
        item = listing["runs"][0]
        self.assertEqual({key: item[key] for key in ("id", "status", "goal", "branch")},
                         {key: ledger_value[key] for key in ("id", "status", "goal", "branch")})
        self.assertEqual(item["schema_version"], RUN_SCHEMA_VERSION)
        self.assertEqual(item["last_event_time"], events[-1]["time"])

        detail = observe.run_detail(state, result["id"])
        self.assertIsNone(detail["ledger_error"])
        self.assertIsNone(detail["summary_error"])
        self.assertEqual(detail["summary"], status_summary(ledger_value))
        self.assertEqual(detail["run"]["status"], "completed")
        self.assertEqual([task["id"] for task in detail["tasks"]], ["greet"])
        self.assertTrue(detail["tasks"][0]["accepted"])
        self.assertEqual([row["id"] for row in detail["dispatches"]], [d["id"] for d in ledger_value["dispatches"]])
        self.assertEqual(detail["invalid_dispatches"], 0)
        self.assertEqual(("operation" in detail["unavailable"]), "created_by_operation" not in ledger_value)
        self.assertNotIn("decisions", detail["unavailable"])
        self.assertNotIn("model_id", detail["unavailable"])
        worker = next(row for row in detail["dispatches"] if row["role"] == "worker")
        self.assertEqual(worker["provider"], "claude")
        self.assertEqual(worker["model_id"], ledger_value["config_snapshot"]["roles"]["worker"]["model"])
        self.assertEqual(worker["invocation_class"], "invoked_no_usage")
        self.assertIn("prompt.txt", worker["files"])
        self.assertIn("returned-result.json", worker["files"])
        self.assertIsNotNone(worker["invoked_at"])
        self.assertEqual(worker["finished_at"], next(e["time"] for e in events if e["event"] == "dispatch_finished"
                                                     and e["dispatch_id"] == worker["id"]))
        verification = [row for row in detail["dispatches"] if row["role"] == "verification"]
        self.assertEqual(len(verification), 2)
        self.assertTrue(all(row["invocation_class"] == "verification" and row["provider"] is None
                            and row["model_id"] is None and row["kind"] is None for row in verification))
        self.assertEqual(detail["invocations"]["by_group"]["verification"]["dispatches"], 2)
        self.assertEqual(detail["invocations"]["by_group"]["coordination"]["dispatches"], 1)
        self.assertEqual(detail["invocations"]["by_provider"]["claude"]["invoked_no_usage"], 1)
        self.assertNotIn("cost", detail)
        self.assertEqual(detail["quota"]["providers"], {"claude": {"state": "no_samples"},
                                                        "codex": {"state": "no_samples"}})
        self.assertEqual(detail["timing"]["first_event_time"], events[0]["time"])
        self.assertEqual(detail["timing"]["last_event_time"], events[-1]["time"])
        self.assertFalse(detail["events"]["unparsed_tail"])
        self.assertEqual(len(detail["events"]["records"]), len(events))
        self.assertEqual(detail["budgets"]["limits"]["max_attempts"], self.config.data["runtime"]["max_attempts"])
        self.assertEqual(detail["budgets"]["attempts"], ledger_value["attempts"])


class InvocationClassTests(StateCase):
    def test_contradiction_first_table_and_verification_exclusion(self):
        usage = {"provider": "claude", "source": "process.stdout", "num_turns": 3, "reported_cost_usd": 0.5}
        rows = {
            "usage": dispatch(provider_usage=usage),
            "no_usage": dispatch(status="failed", error="worker CLI exit=1\nsecond line"),
            "prepared": dispatch(invoked=False, process_state="prepared", status="running"),
            "gate_failed": dispatch(invoked=False, process_state="exited", status="failed",
                                    error="MCP servers.worker_service.url 必须为非空字符串"),
            "missing_invoked": dispatch(role="reviewer", kind="review"),
            "true_prepared": dispatch(process_state="prepared"),
            "false_running": dispatch(invoked=False, process_state="running", pgid=4242),
            "false_pgid": dispatch(invoked=False, process_state="exited", pgid=4242),
            "false_no_state": dispatch(invoked=False),
            "starting": dispatch(role="commander", kind="plan", task_id="planning", process_state="starting",
                                 status="running"),
            "verification": {"id": str(uuid.uuid4()), "role": "verification", "task_id": "greet",
                             "status": "completed", "pgid": 777, "process_state": "exited", "exit_confirmed": True},
            "odd_role": dispatch(role="auditor"),
        }
        del rows["missing_invoked"]["invoked"]
        del rows["false_no_state"]["process_state"]
        run = ledger("run_classes", dispatches=list(rows.values()) + ["not a dispatch", {"role": "worker"}])
        detail, by_id = self.rows(run)
        expected = {"usage": "usage_recorded", "no_usage": "invoked_no_usage", "prepared": "known_not_invoked",
                    "gate_failed": "known_not_invoked", "missing_invoked": "unknown", "true_prepared": "unknown",
                    "false_running": "unknown", "false_pgid": "unknown", "false_no_state": "unknown",
                    "starting": "invoked_no_usage", "verification": "verification", "odd_role": "unknown"}
        for name, klass in expected.items():
            with self.subTest(name=name):
                self.assertEqual(by_id[rows[name]["id"]]["invocation_class"], klass)
        for name in ("missing_invoked", "true_prepared", "false_running", "false_pgid", "false_no_state", "odd_role"):
            self.assertTrue(by_id[rows[name]["id"]]["invocation_reason"], name)
        self.assertIsNone(by_id[rows["usage"]["id"]]["invocation_reason"])
        self.assertTrue(by_id[rows["starting"]["id"]]["startup_unconfirmed"])
        self.assertFalse(by_id[rows["usage"]["id"]]["startup_unconfirmed"])
        self.assertEqual(by_id[rows["no_usage"]["id"]]["error"], "worker CLI exit=1")
        self.assertIsNone(by_id[rows["missing_invoked"]["id"]]["invoked"])
        self.assertEqual(detail["invalid_dispatches"], 2)
        self.assertEqual(detail["summary_error"], None)
        cost = detail["invocations"]
        self.assertEqual(cost["known_not_invoked"], 2)
        self.assertEqual(cost["unknown_invocation"], 6)
        self.assertEqual(cost["startup_unconfirmed"], 1)
        self.assertEqual(cost["by_group"]["verification"]["dispatches"], 1)
        claude = cost["by_provider"]["claude"]
        self.assertEqual((claude["invoked"], claude["usage_recorded"], claude["invoked_no_usage"]), (2, 1, 1))
        self.assertEqual(cost["by_provider"]["codex"]["invoked"], 1)
        total_model = sum(p["invoked"] for p in cost["by_provider"].values())
        self.assertEqual(total_model + cost["known_not_invoked"] + cost["unknown_invocation"] + 1, cost["counted_dispatches"])
        self.assertEqual(cost["by_group"]["coordination"]["dispatches"], 1)


class InvocationSummaryTests(StateCase):
    def test_duplicates_null_rule_and_role_based_groups_without_money(self):
        first = dispatch(provider_usage={"provider": "claude", "reported_cost_usd": 0.25, "num_turns": 4})
        rows = [
            first,
            dispatch(status="failed", provider_usage={"provider": "claude", "reported_cost_usd": 0.5}),
            dispatch(kind="format_repair"),  # invoked, no usage
            dispatch(role="reviewer", kind="review", provider_usage={"provider": "codex", "completed_turns": 3}),
            dispatch(role="reviewer", kind="final_review", task_id="final",
                     provider_usage={"provider": "codex", "completed_turns": 2, "tokens": {"input_tokens": 10}}),
            dict(first),  # duplicate ledger ID: counted once
            dispatch(role="commander", kind="plan", task_id="planning",
                     provider_usage={"provider": "claude", "reported_cost_usd": 0.1}),
            dispatch(role="commander", kind="dispatch", task_id="greet"),
            dispatch(role="commander", kind="replan", task_id="greet", invoked=False, process_state="prepared",
                     status="running"),
            dispatch(task_id="planning", provider_usage={"provider": "claude", "reported_cost_usd": 0.3}),
            dispatch(provider_usage={"provider": "claude", "reported_cost_usd": -1}),
            dispatch(provider_usage={"provider": "claude", "reported_cost_usd": True}),
        ]
        detail, _ = self.rows(ledger("run_cost", dispatches=rows))
        calls = detail["invocations"]
        self.assertEqual(calls["duplicate_dispatch_ids"], [first["id"]])
        self.assertEqual(calls["counted_dispatches"], len(rows) - 1)
        claude = calls["by_provider"]["claude"]
        self.assertEqual((claude["invoked"], claude["usage_recorded"], claude["invoked_no_usage"]), (7, 6, 1))
        codex = calls["by_provider"]["codex"]
        self.assertEqual((codex["invoked"], codex["usage_recorded"], codex["invoked_no_usage"]), (3, 2, 1))
        self.assertEqual(calls["by_provider"]["unknown"]["invoked"], 0)
        coordination = calls["by_group"]["coordination"]
        self.assertEqual((coordination["dispatches"], coordination["invoked"], coordination["usage_recorded"],
                          coordination["invoked_no_usage"], coordination["known_not_invoked"]), (3, 2, 1, 1, 1))
        self.assertEqual(calls["by_group"]["tasks"]["planning"]["dispatches"], 1)
        self.assertEqual(calls["by_group"]["tasks"]["greet"]["dispatches"], 6)
        self.assertEqual(calls["by_group"]["tasks"]["final"]["invoked"], 1)
        # No money is aggregated anywhere; each row keeps the CLI's own fields as reported.
        text = json.dumps(calls)
        for word in ("reported_cost", "label", "total"):
            self.assertNotIn(word, text)
        rows_by_id = {row["id"]: row for row in detail["dispatches"]}
        self.assertEqual(rows_by_id[rows[4]["id"]]["provider_usage"]["tokens"], {"input_tokens": 10})
        self.assertEqual(rows_by_id[rows[1]["id"]]["provider_usage"]["reported_cost_usd"], 0.5)


class ProviderModelTests(StateCase):
    def test_provider_and_model_come_from_config_snapshot(self):
        rows = [dispatch(role="commander", kind="plan", task_id="planning"),
                dispatch(), dispatch(role="reviewer", kind="review"),
                dispatch(provider_usage={"provider": "codex", "completed_turns": 1})]
        for commander, expected in (("astra", ("codex", "gpt-6.2-astra")), ("fable", ("claude", "claude-fable-5-2"))):
            with self.subTest(commander=commander):
                detail, by_id = self.rows(ledger("run_" + commander, commander=commander, dispatches=rows))
                self.assertEqual((by_id[rows[0]["id"]]["provider"], by_id[rows[0]["id"]]["model_id"]), expected)
                self.assertEqual((by_id[rows[1]["id"]]["provider"], by_id[rows[1]["id"]]["model_id"]),
                                 ("claude", "claude-opus-5-5"))
                self.assertEqual((by_id[rows[2]["id"]]["provider"], by_id[rows[2]["id"]]["model_id"]),
                                 ("codex", "gpt-6.2-sol"))
                self.assertEqual((by_id[rows[3]["id"]]["provider"], by_id[rows[3]["id"]]["model_id"]),
                                 ("codex", "claude-opus-5-5"))  # usage provider wins; model stays configured
                self.assertNotIn("model_id", detail["unavailable"])
        run = ledger("run_nosnapshot", dispatches=rows)
        del run["config_snapshot"]
        detail, by_id = self.rows(run)
        self.assertIn("model_id", detail["unavailable"])
        self.assertIsNone(by_id[rows[1]["id"]]["provider"])
        self.assertIsNone(by_id[rows[1]["id"]]["model_id"])
        self.assertEqual(by_id[rows[3]["id"]]["provider"], "codex")
        self.assertEqual(by_id[rows[3]["id"]]["invocation_class"], "usage_recorded")
        self.assertEqual(detail["invocations"]["by_provider"]["unknown"]["invoked"], 3)


class EventsAndDamageTests(StateCase):
    def write_events(self, run_id, lines, trailing=b""):
        path = self.state / "runs" / run_id / "events.jsonl"
        path.write_bytes(b"".join(json.dumps(line).encode() + b"\n" for line in lines) + trailing)

    def test_partial_or_invalid_last_line_is_flagged_not_failed(self):
        run_id = self.save(ledger("run_events"))
        records = [{"time": 10.0 + i, "event": "dispatch_started", "run_id": run_id, "task_id": "greet"} for i in range(3)]
        self.write_events(run_id, records, trailing=b'{"time": 13.0, "event": "dispatch_fin')
        detail = observe.run_detail(self.state, run_id)
        self.assertTrue(detail["events"]["unparsed_tail"])
        self.assertEqual(detail["events"]["invalid_lines"], 0)
        self.assertEqual([r["time"] for r in detail["events"]["records"]], [10.0, 11.0, 12.0])
        self.assertEqual(detail["events"]["last_time"], 12.0)
        self.assertEqual(detail["timing"], {"first_event_time": 10.0, "last_event_time": 12.0, "basis": "events.jsonl 事件时间"})
        self.assertEqual(observe.list_runs(self.state)["runs"][0]["last_event_time"], 12.0)
        self.write_events(run_id, records, trailing=b"{broken json}\n")
        block = observe.Observer(self.state).events(run_id)
        self.assertTrue(block["unparsed_tail"])
        self.assertEqual(block["invalid_lines"], 1)
        self.assertEqual(len(block["records"]), 3)
        self.write_events(run_id, records)
        with observe.Observer(self.state) as observer:
            clean = observer.events(run_id)
            self.assertFalse(clean["unparsed_tail"])
            small = observer.events(run_id, tail=len(json.dumps(records[-1])) + 1)
            self.assertTrue(small["truncated"])
            self.assertEqual([r["time"] for r in small["records"]], [12.0])
            with self.assertRaises(observe.InvalidPath):
                observer.events(run_id, tail=0)
            with self.assertRaises(observe.InvalidPath):
                observer.events(run_id, tail=observe.MAX_TAIL + 1)
            with self.assertRaises(observe.NotFound):
                observer.events("run_missing")
            with self.assertRaises(observe.InvalidPath):
                observer.events("../etc")

    def test_damaged_ledgers_are_skipped_in_list_and_summaries_fail_soft(self):
        good = self.save(ledger("run_good"))
        broken = self.state / "runs" / "run_broken" / "state.json"
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"{not json")
        (self.state / "runs" / "run_list").mkdir()
        (self.state / "runs" / "run_list" / "state.json").write_text("[1, 2]\n")
        (self.state / "runs" / "run_nostate").mkdir()
        (self.state / "runs" / "stray.txt").write_text("ignored\n")
        (self.state / "runs" / "not-a-run").mkdir()
        self.save(ledger("run_typeerror", operator_decisions=[{"request_id": []}]))
        self.save(ledger("run_harnesserror", accepted=None))
        listing = observe.list_runs(self.state)
        self.assertEqual([item["id"] for item in listing["runs"]],
                         ["run_good", "run_harnesserror", "run_typeerror"])
        skipped = {item["path"]: item["reason"] for item in listing["skipped"]}
        self.assertIn("无法解析", skipped["runs/run_broken/state.json"])
        # A parsed non-object is not a current-version ledger: named, never interpreted.
        self.assertEqual(listing["unsupported"], [{"path": "runs/run_list", "schema_version": None,
                                                   "reason": f"台账版本缺失或无效，不支持（当前版本 {RUN_SCHEMA_VERSION}）"}])
        self.assertIn("runs/run_nostate/state.json", skipped)
        self.assertIn("runs/not-a-run", skipped)
        detail = observe.run_detail(self.state, "run_broken")
        self.assertIn("无法解析", detail["ledger_error"])
        self.assertIsNone(detail["summary"])
        self.assertIsNone(detail["summary_error"])
        self.assertEqual(detail["dispatches"], [])
        with self.assertRaises(observe.NotFound):
            observe.run_detail(self.state, "run_nostate")
        with self.assertRaises(observe.NotFound):
            observe.run_detail(self.state, "run_absent")
        with self.assertRaises(observe.InvalidPath):
            observe.run_detail(self.state, "not-a-run")
        harness_error = observe.run_detail(self.state, "run_harnesserror")
        self.assertIsNone(harness_error["summary"])
        self.assertTrue(harness_error["summary_error"].startswith("HarnessError: "))
        self.assertEqual([task["id"] for task in harness_error["tasks"]], ["greet", "planning"])
        self.assertFalse(harness_error["tasks"][0]["accepted"])
        type_error = observe.run_detail(self.state, "run_typeerror")
        self.assertIsNone(type_error["summary"])
        self.assertTrue(type_error["summary_error"].startswith("TypeError: "))
        self.assertEqual(type_error["run"]["goal"], "Implement greet")
        self.assertEqual(type_error["budgets"]["agent_dispatches"], 1)
        self.assertIsNotNone(observe.run_detail(self.state, good)["summary"])

class OperationsTests(StateCase):
    def test_operations_filtered_by_run_with_invalid_count(self):
        run_id = self.save(ledger("run_ops"))
        folder = self.state / "operations"
        write_json(folder / "op_this.json", {"operation_id": "op_this", "kind": "run", "status": "finished",
                                             "run_id": run_id, "started_at": 5.0, "exit_status": 0,
                                             "error": "first line\nsecond", "harness": {"revision": "abc"}})
        write_json(folder / "op_resume.json", {"operation_id": "op_resume", "kind": "resume", "status": "running",
                                               "run_id": run_id, "started_at": 9.0})
        write_json(folder / "op_other.json", {"operation_id": "op_other", "kind": "run", "run_id": "run_other"})
        write_json(folder / "op_none.json", {"operation_id": "op_none", "kind": "run", "run_id": None})
        write_json(folder / "op_list.json", [1, 2])
        (folder / "op_broken.json").write_bytes(b"{")
        (folder / "op_tmp.json.deadbeef.tmp").write_bytes(b"{}")
        big = {"operation_id": "op_big", "run_id": run_id, "padding": "x" * (observe.OPERATION_MAX_BYTES + 1)}
        write_json(folder / "op_big.json", big)
        detail = observe.run_detail(self.state, run_id)
        operations = detail["operations"]
        self.assertEqual([op["operation_id"] for op in operations["records"]], ["op_this", "op_resume"])
        self.assertEqual(operations["records"][0]["error"], "first line")
        self.assertEqual(operations["records"][0]["harness"], {"revision": "abc"})
        self.assertEqual(operations["records"][0]["file"], "op_this.json")
        self.assertEqual(operations["invalid"], 3)
        self.assertEqual(operations["scanned"], 7)
        self.assertFalse(operations["truncated"])
        self.assertEqual(observe.run_detail(self.state, self.save(ledger("run_lonely")))["operations"]["records"], [])
        self.assertEqual(observe.run_detail(self.state, run_id)["unavailable"], [])


def overflowing_depth():
    """Nesting depth at which this interpreter's json.loads raises RecursionError, or None."""
    for depth in (1100, 20_000, 200_000, 2_000_000):
        try:
            json.loads("[" * depth + "]" * depth)
        except RecursionError:
            return depth
        except (ValueError, MemoryError):
            return None
    return None


class DamageIsolationTests(StateCase):
    def test_deeply_nested_state_json_is_skipped_not_fatal(self):
        depth = overflowing_depth()
        if depth is None:
            self.skipTest("json.loads does not raise RecursionError on this interpreter")
        good = self.save(ledger("run_good"))
        deep = self.state / "runs" / "run_deep" / "state.json"
        deep.parent.mkdir(parents=True)
        deep.write_bytes(b'{"id": "run_deep", "nest": ' + b"[" * depth + b"]" * depth + b"}")
        listing = observe.list_runs(self.state)
        self.assertEqual([item["id"] for item in listing["runs"]], [good])
        self.assertEqual(listing["skipped"][0]["path"], "runs/run_deep/state.json")
        self.assertIn("RecursionError", listing["skipped"][0]["reason"])
        detail = observe.run_detail(self.state, "run_deep")
        self.assertIn("RecursionError", detail["ledger_error"])
        self.assertIsNone(detail["summary"])
        events = self.state / "runs" / good / "events.jsonl"
        events.write_bytes(b'{"time": 1.0, "event": "a"}\n'
                           + b'{"time": 2.0, "event": "deep", "nest": ' + b"[" * depth + b"]" * depth + b"}\n"
                           + b'{"time": 3.0, "event": "b"}\n')
        detail = observe.run_detail(self.state, good)
        self.assertEqual(detail["timing"]["first_event_time"], 1.0)
        self.assertEqual(detail["events"]["last_time"], 3.0)
        with observe.Observer(self.state) as observer:
            block = observer.events(good, tail=observe.MAX_TAIL)  # Large enough to hold the deep line whole.
        self.assertFalse(block["truncated"])
        self.assertEqual([r["time"] for r in block["records"]], [1.0, 3.0])
        self.assertEqual(block["invalid_lines"], 1)
        folder = self.state / "operations"
        folder.mkdir()
        (folder / "op_deep.json").write_bytes(b'{"run_id": "' + good.encode() + b'", "nest": ' + b"[" * depth + b"]" * depth + b"}")
        write_json(folder / "op_ok.json", {"operation_id": "op_ok", "run_id": good})
        operations = observe.run_detail(self.state, good)["operations"]
        self.assertEqual(([op["operation_id"] for op in operations["records"]], operations["invalid"]), (["op_ok"], 1))

    def test_huge_integers_become_absent_values_not_errors(self):
        huge = int("9" * 400)
        row = dispatch(invoked_at=huge, pgid=huge, provider_usage={"provider": "claude", "reported_cost_usd": huge,
                                                                   "num_turns": huge})
        run_id = self.save(ledger("run_huge", agent_dispatches=huge, dispatches=[row]))
        self.save(ledger("run_hugeversion", schema_version=huge))
        (self.state / "runs" / run_id / "events.jsonl").write_text(
            f'{{"time": {huge}, "event": "dispatch_started", "run_id": "{run_id}"}}\n'
            f'{{"time": {huge}, "event": "dispatch_finished", "run_id": "{run_id}", "dispatch_id": "{row["id"]}"}}\n')
        listing = observe.list_runs(self.state)
        self.assertEqual([item["id"] for item in listing["runs"]], [run_id])
        self.assertIsNone(listing["runs"][0]["last_event_time"])
        # Another version is named only; its huge integer stays an integer.
        self.assertEqual(listing["unsupported"], [{"path": "runs/run_hugeversion", "schema_version": huge,
                                                   "reason": f"台账版本 {huge} 不支持（当前版本 {RUN_SCHEMA_VERSION}）"}])
        detail = observe.run_detail(self.state, run_id)
        self.assertEqual(detail["timing"], {"first_event_time": None, "last_event_time": None, "basis": "events.jsonl 事件时间"})
        self.assertEqual(len(detail["events"]["records"]), 2)
        projected = detail["dispatches"][0]
        self.assertIsNone(projected["invoked_at"])
        self.assertIsNone(projected["finished_at"])
        self.assertEqual(projected["pgid"], huge)
        self.assertEqual(projected["invocation_class"], "usage_recorded")
        self.assertEqual(detail["invocations"]["by_provider"]["claude"]["usage_recorded"], 1)
        self.assertEqual(detail["budgets"]["agent_dispatches"], huge)  # Compared as an integer, never via float.
        self.assertIsNotNone(detail["summary"])
        json.dumps(detail)  # The whole projection stays serializable.

    def test_non_string_commander_keeps_detail_readable(self):
        rows = [dispatch(role="commander", kind="plan", task_id="planning"),
                dispatch(role="commander", kind="dispatch", task_id="greet",
                         provider_usage={"provider": "claude", "reported_cost_usd": 0.2}),
                dispatch()]
        for commander in ([], {"cli": "codex"}, 7, None, "other"):
            with self.subTest(commander=commander):
                detail, by_id = self.rows(ledger("run_cmd", commander=commander, dispatches=rows))
                self.assertIsNone(detail["summary_error"])
                self.assertIsNone(by_id[rows[0]["id"]]["provider"])
                self.assertIsNone(by_id[rows[0]["id"]]["model_id"])
                self.assertEqual(by_id[rows[1]["id"]]["provider"], "claude")
                self.assertIsNone(by_id[rows[1]["id"]]["model_id"])
                self.assertEqual(by_id[rows[2]["id"]]["model_id"], "claude-opus-5-5")
                self.assertIn("model_id", detail["unavailable"])
                self.assertEqual(detail["invocations"]["by_group"]["coordination"]["dispatches"], 2)
                # The commander row's provider comes from its own usage; the worker's from the config.
                self.assertEqual(detail["invocations"]["by_provider"]["claude"]["invoked"], 2)


class FileIndexCapTests(StateCase):
    def index(self, run_id, limit):
        with patch.object(observe, "FILE_INDEX_MAX", limit):
            result = observe.Observer(self.state).file_index(run_id)
        return [item["path"] for item in result["files"]], result["truncated"], result["limit"]

    def test_cap_is_shared_by_every_entry_source(self):
        run_id = self.save(ledger("run_cap"))
        run_dir = self.state / "runs" / run_id
        (run_dir / "events.jsonl").write_text("")
        (run_dir / "plans").mkdir()
        for name in ("a.json", "b.json"):
            (run_dir / "plans" / name).write_text("{}")
        # Directory fills the remaining quota exactly, then a later top-level file follows.
        self.assertEqual(self.index(run_id, 3), (["events.jsonl", "plans/a.json", "plans/b.json"], True, 3))
        # Exactly at the cap with nothing left over: not truncated.
        (run_dir / "state.json").rename(run_dir / "zz-not-indexed.json")
        self.assertEqual(self.index(run_id, 3), (["events.jsonl", "plans/a.json", "plans/b.json"], False, 3))
        # Directory overflows the remaining quota.
        (run_dir / "plans" / "c.json").write_text("{}")
        self.assertEqual(self.index(run_id, 3), (["events.jsonl", "plans/a.json", "plans/b.json"], True, 3))
        self.assertEqual(self.index(run_id, 10)[1:], (False, 10))
        self.assertEqual(len(self.index(run_id, 10)[0]), 4)

    def test_admission_locks_leave_all_dispatch_evidence_visible_at_the_cap(self):
        rows = [dispatch(), dispatch()]
        run_id = self.save(ledger("run_locks", dispatches=rows))
        expected = {"state.json"}
        for row in rows:
            relative = Path("dispatches") / row["id"]
            job = self.state / "runs" / run_id / relative
            write_json(job / "packet.json", {})
            (job / "command-admission.lock").touch()
            expected.add((relative / "packet.json").as_posix())

        paths, truncated, _ = self.index(run_id, len(expected))
        self.assertEqual(set(paths), expected)
        self.assertFalse(truncated)
        detail = observe.run_detail(self.state, run_id)
        self.assertEqual([row["files"] for row in detail["dispatches"]], [["packet.json"], ["packet.json"]])


class FirstEventTimeTests(StateCase):
    def first(self, run_id, data):
        (self.state / "runs" / run_id / "events.jsonl").write_bytes(data)
        return observe.run_detail(self.state, run_id)["timing"]["first_event_time"]

    def test_first_parsable_record_within_bounded_head(self):
        run_id = self.save(ledger("run_first"))
        self.assertEqual(self.first(run_id, b'bad\n{"time": 7}\n'), 7)
        self.assertEqual(self.first(run_id, b'\n[1]\n"text"\n{"time": 8.5, "event": "e"}\n'), 8.5)
        self.assertIsNone(self.first(run_id, b'{"x": 1}\n{"time": 9}\n'))  # First record parses; its time is absent.
        self.assertIsNone(self.first(run_id, b"bad\n"))
        self.assertIsNone(self.first(run_id, b""))
        filler = b"bad line\n" * (observe.DEFAULT_TAIL // 9 + 10)
        self.assertGreater(len(filler), observe.DEFAULT_TAIL)
        self.assertIsNone(self.first(run_id, filler + b'{"time": 9}\n'))  # Beyond the head: undeterminable.
        self.assertEqual(self.first(run_id, b'bad\n{"time": 7}\n' + filler), 7)  # Found inside the head.
        self.assertIsNone(self.first(run_id, b'{"time": 1'))  # Unterminated single line.


class RootDescriptorTests(StateCase):
    def test_missing_state_directory_lists_empty_unless_required(self):
        missing = Path(self.temp.name) / "absent" / ".harness"
        with observe.Observer(missing) as observer:
            self.assertEqual(observer.list_runs()["runs"], [])
            with self.assertRaises(observe.NotFound):
                observer.run_detail("run_x")
            with self.assertRaises(observe.NotFound):
                observer.file_index("run_x")
        with self.assertRaises(observe.NotFound):
            observe.Observer(missing, required=True)
        self.assertFalse(missing.exists())  # Nothing was created either way.

    def test_root_descriptor_is_fixed_at_construction(self):
        old = self.save(ledger("run_old"))
        observer = observe.Observer(self.state, required=True)
        self.addCleanup(observer.close)
        self.assertEqual([item["id"] for item in observer.list_runs()["runs"]], [old])
        moved = self.state.with_name("harness-moved")
        self.state.rename(moved)
        self.state.mkdir()
        write_json(self.state / "runs" / "run_new" / "state.json", ledger("run_new"))
        self.assertEqual([item["id"] for item in observer.list_runs()["runs"]], [old])
        self.assertEqual(observer.run_detail(old)["run"]["goal"], "Implement greet")
        with self.assertRaises(observe.NotFound):
            observer.run_detail("run_new")
        self.assertEqual([item["id"] for item in observe.list_runs(self.state)["runs"]], ["run_new"])


class LocationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        # Resolved, so a given absolute path and the directory the reader resolved compare as text.
        self.base = Path(temp.name).resolve()

    def test_each_layout_names_what_is_opened(self):
        b, top = self.base, self.base.name
        for folder in ("repo/.harness/runs", "archive/.harness", "archive/state-copy", "store/h1", "store/.harness",
                       "linked", "linked2", "deep/inner", "deep/.harness"):
            (b / folder).mkdir(parents=True)
        (b / "linked" / ".harness").symlink_to(b / "store" / "h1", target_is_directory=True)
        (b / "linked2" / ".harness").symlink_to(b / "store" / ".harness", target_is_directory=True)
        (b / "jump").symlink_to(b / "deep" / "inner", target_is_directory=True)
        (b / "loop-a").symlink_to(b / "loop-b")
        (b / "loop-b").symlink_to(b / "loop-a")

        def observed(state, checkout, basis, name):
            return {"state": str(state), "checkout": None if checkout is None else str(checkout),
                    "checkout_basis": basis, "name": name}

        dotdot = str(b / "jump") + "/../.harness"
        layouts = (  # (given, checkout passed by the caller, expected)
            (b / "repo" / ".harness", b / "repo", observed(b / "repo" / ".harness", b / "repo", "git", f"{top}/repo")),
            (b / "linked" / ".harness", b / "linked", observed(b / "store" / "h1", b / "linked", "git", f"{top}/linked")),
            (b / "archive" / ".harness", None, observed(b / "archive" / ".harness", b / "archive", "path", f"{top}/archive")),
            (b / "archive" / "state-copy", None, observed(b / "archive" / "state-copy", None, None, "archive/state-copy")),
            (b / "linked" / ".harness", None, observed(b / "store" / "h1", None, None, "store/h1")),
            (b / "linked2" / ".harness", None, observed(b / "store" / ".harness", b / "store", "path", f"{top}/store")),
            (dotdot, None, observed(b / "deep" / ".harness", b / "deep", "path", f"{top}/deep")))
        with patch("harness.git.git", side_effect=AssertionError("an observation entry never runs git")):
            for given, checkout, expected in layouts:
                with self.subTest(given=str(given), checkout=checkout), \
                        observe.Observer(given, required=True, checkout=checkout) as observer:
                    self.assertTrue(observer.reader.opened)
                    self.assertEqual(observer.observed, expected)
                    # The location names what the reader opened, and the reader opens what it always did.
                    self.assertEqual(observer.observed["state"], str(observer.reader.state))
                    self.assertEqual(observer.reader.state, Path(given).resolve())
            self.assertNotEqual(os.path.abspath(dotdot), str(b / "deep" / ".harness"))  # a lexical fold would name another
            # A symbolic link loop (resolution fails on 3.11, opening fails from 3.13) and a NUL byte: not openable.
            for given in (str(b / "loop-a"), str(b / "bad") + "\x00x"):
                with self.subTest(given=given):
                    with observe.Observer(given) as observer:
                        self.assertFalse(observer.reader.opened)
                        self.assertEqual(observer.list_runs()["runs"], [])
                        with self.assertRaises(observe.NotFound):
                            observer.run_detail("run_x")
                        self.assertEqual(observer.observed, observed(given, None, None, f"{top}/{Path(given).name}"))
                    with self.assertRaises(observe.NotFound):
                        observe.Observer(given, required=True)

    def test_every_json_view_carries_the_fixed_location(self):
        checkout = self.base / "repo"
        state = checkout / ".harness"
        write_json(state / "runs" / "run_good" / "state.json", ledger("run_good"))
        (state / "runs" / "run_good" / "events.jsonl").write_text('{"time": 1.0, "event": "run_started"}\n')
        (state / "runs" / "run_broken").mkdir()
        (state / "runs" / "run_broken" / "state.json").write_bytes(b"{not json")
        observer = observe.Observer(state, required=True, checkout=checkout)
        self.addCleanup(observer.close)
        fixed = {"state": str(state), "checkout": str(checkout), "checkout_basis": "git", "name": f"{self.base.name}/repo"}

        def views():
            return {"list": observer.list_runs(), "detail": observer.run_detail("run_good"),
                    "damaged detail": observer.run_detail("run_broken"), "events": observer.events("run_good"),
                    "file index": observer.file_index("run_good"), "file": observer.read_file("run_good", "state.json")}

        shown = views()
        self.assertIsNotNone(shown["damaged detail"]["ledger_error"])
        for name, view in shown.items():
            self.assertEqual(view["observed"], fixed, name)
        # Renamed and rebuilt while served: like the root descriptor, the location stays what was opened.
        state.rename(state.with_name("harness-moved"))
        write_json(state / "runs" / "run_good" / "state.json", ledger("run_good", goal="rebuilt"))
        shown = views()
        self.assertEqual(shown["detail"]["run"]["goal"], "Implement greet")
        for name, view in shown.items():
            self.assertEqual(view["observed"], fixed, name)


class StatusListDiagnosticsTests(RepoCase):
    def snapshot(self, root):
        result = {}
        for path in [root, *root.rglob("*")]:
            info = path.lstat()
            content = (os.readlink(path) if path.is_symlink() else
                       path.read_bytes() if path.is_file() else None)
            result[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, content)
        return result

    def test_status_list_refuses_symlinked_runs_with_path_diagnostic(self):
        state = self.repo / ".harness"
        state.mkdir()
        external = self.repo / "external-runs"
        write_json(external / "run_hidden" / "state.json", ledger("run_hidden"))
        runs = state / "runs"
        runs.symlink_to(external, target_is_directory=True)
        before = self.snapshot(state), self.snapshot(external)

        result = subprocess.run([sys.executable, "-m", "harness", "--repo", str(self.repo), "status"],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn(str(runs), result.stderr)
        self.assertIn("符号链接", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual((self.snapshot(state), self.snapshot(external)), before)

    def test_status_list_reports_unreadable_runs_without_empty_success_or_writes(self):
        state = self.repo / ".harness"
        runs = state / "runs"
        write_json(runs / "run_unreadable" / "state.json", ledger("run_unreadable"))
        before = self.snapshot(state)
        real_open = os.open
        denied = []

        def unreadable_open(path, flags, mode=0o777, *, dir_fd=None):
            if path == "runs" and dir_fd is not None:
                denied.append(path)
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(runs))
            return real_open(path, flags, mode, dir_fd=dir_fd)

        output, errors = io.StringIO(), io.StringIO()
        # Inject at the OS boundary so EACCES is exercised even under root.
        with patch("harness.observe.os.open", side_effect=unreadable_open), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(["--repo", str(self.repo), "status"])

        self.assertEqual(denied, ["runs"])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn(str(runs), errors.getvalue())
        self.assertIn(os.strerror(errno.EACCES), errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertEqual(self.snapshot(state), before)


class StatusBindingTests(RepoCase):
    def test_status_run_reads_ledger_and_quota_from_the_located_directory(self):
        base = repo = self.repo.resolve()
        for name in ("a", "b"):
            write_json(base / f"state-{name}" / "runs" / "run_same" / "state.json", ledger("run_same", goal=f"goal {name}"))
        # Only b has a (damaged) quota sample, so the quota block shows which directory it came from.
        samples = base / "state-b" / "runs" / "run_same" / "observations" / "quota"
        samples.mkdir(parents=True)
        (samples / "r0000000001-p0-20261118T000000.000000Z-s0001-claude-run_start.json").write_text("{}")
        link = self.repo / ".harness"
        real_init, repointed = observe.StateReader.__init__, []

        def point(target):
            if link.is_symlink():
                link.unlink()
            link.symlink_to(target, target_is_directory=True)

        def init_then_repoint(reader, state, **options):
            real_init(reader, state, **options)
            if not repointed:  # Once, after the root is fixed and before anything below it is read.
                repointed.append(reader.state)
                point(base / "state-b")

        expected = f"harness: 检出 {repo}；状态目录 {base / 'state-a'}"
        for extra in ([], ["--full"]):
            point(base / "state-a")
            repointed.clear()
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(observe.StateReader, "__init__", init_then_repoint), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = cli.main(["--repo", str(self.repo), "status", "run_same", *extra])
            with self.subTest(extra=extra):
                self.assertEqual(code, 0, errors.getvalue())
                self.assertEqual(repointed, [base / "state-a"])
                self.assertEqual(errors.getvalue().splitlines()[0], expected)
                shown = json.loads(output.getvalue())
                self.assertEqual(shown["goal"], "goal a")
                if not extra:
                    self.assertEqual(shown["quota"]["damaged_count"], 0)
        # The repointed link really leads elsewhere: a fresh call reads b, ledger and quota alike.
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--repo", str(self.repo), "status", "run_same"]), 0)
        self.assertEqual((json.loads(output.getvalue())["goal"], json.loads(output.getvalue())["quota"]["damaged_count"]),
                         ("goal b", 1))


def printed(value):
    """What status prints for a value: indented JSON, non-ASCII kept, one newline."""
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def written_summary(goal):
    """`status run_same`'s summary of ``ledger("run_same", goal=goal)``, written out rather than computed."""
    return {"id": "run_same", "status": "running", "goal": goal, "branch": "harness/run_same", "worktree": "/tmp/worktree",
            "current_task": None, "accepted_tasks": ["greet"],
            "budgets": {"attempts": {"greet": 1}, "agent_dispatches": 1, "work_attempts": {"greet": 1}, "attempts_total": 1},
            "convergence": {"advisory_only": True, "warnings": [], "truncated": False,
                            "suggestion": "核对反复出现的问题、任务范围及方案复杂度，再决定是否继续；提示不改变预算、权限或验收。"},
            "review_rounds": {"read_only": True, "cause": "unknown", "tasks": {}},
            "ledger_stage": {"step": "between_steps", "task_id": None, "attempt": None, "role": None, "kind": None,
                             "dispatch_id": None, "process_state": None},
            "budget_explanation": {
                "dimensions": {"actual_worker_invocations": 0, "charged_worker_invocations": 0,
                               "process_start_unconfirmed": 0, "reviewed_worker_candidates": 0, "unlinked_review_records": 0,
                               "worker_failure_verdicts": {"usage_limit": 0, "rate_limit": 0, "overloaded": 0,
                                                           "authentication": 0, "transport": 0, "agent_turns": 0,
                                                           "agent_timeout": 0, "local_start": 0, "invocation_failed": 0,
                                                           "unknown": 0, "malformed": 0},
                               "recorded_exemptions": {"environment": 0, "operator": 0, "usage_limit": 0, "rate_limit": 0,
                                                       "overloaded": 0, "authentication": 0, "transport": 0, "unknown": 0},
                               "duplicate_dispatch_ids": 0, "invalid_outputs": {"commander": 0, "worker": 0, "reviewer": 0}},
                "limits": {"task_work": {"greet": {"used": 1, "limit": 3, "remaining": 2}},
                           "agent_dispatches": {"used": 1, "limit": 200, "remaining": 199}},
                "evidence_basis": "recorded_ledger_links_not_revalidated_artifacts", "acceptance_basis": "accepted_tasks",
                "recovery_command": "status run_same"},
            "remaining_model_calls": {"lower_bound": 3, "tasks": {"planning": 2}, "final_review": 1,
                                      "message": ("如果之后全部一次通过，至少还需 3 次模型调用（下界：只计 worker、任务审阅与整体审阅，"
                                                  "不含指挥决定、返工与重做）"),
                                      "basis": "recorded_stage_lower_bound; read-only, never changes a budget"},
            "worker_outcomes": {"completed": 0, "task_blocked": 0, "environment_blocked": 0, "operator_blocked": 0,
                                "continuation_blocked": 0, "invalid_output": 0, "runtime_failed": 0, "unknown": 0}}


class StatusLayoutTests(RepoCase):
    """`status RUN` reads below the root it names, yet as the path read it replaced did (guarantee 5.1)."""

    def setUp(self):
        super().setUp()
        self.base = scratch(self).resolve()
        self.elsewhere = ledger("run_same", goal="elsewhere")
        write_json(self.base / "elsewhere" / "run_same" / "state.json", self.elsewhere)
        self.ordinary = ledger("run_same", goal="ordinary")

    def repository(self, name):
        repo = self.base / name
        repo.mkdir()
        git.git(repo, "init", "-b", "main")
        return repo

    def restrict(self, path, mode):
        path.chmod(mode)
        self.addCleanup(path.chmod, 0o755)  # before the folder is removed

    def status(self, repo, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(["--repo", str(repo), "status", *args])
        return code, output.getvalue(), errors.getvalue()

    def test_layouts_the_path_read_could_read_print_the_same_bytes(self):
        if os.geteuid() == 0:
            self.skipTest("permission bits do not stop root")
        # Written out: no samples found, or the quota walk refused (it follows no link and needs read permission).
        no_samples = {"notes": ["账号级观测：包含本 run 以外的使用（含驱动 Harness 的会话），不是本 run 的消耗",
                                "无法识别账号是否换过；样本里没有可比较的账号标识", "单位是百分点；重叠的额度桶不相加，两家不合成总数"],
                      "damaged_count": 0, "omitted": 0,
                      "providers": {"claude": {"state": "no_samples"}, "codex": {"state": "no_samples"}}}
        refused = {"error": "PathRefused"}
        cases = {}
        repo = self.repository("run-link")
        (repo / ".harness" / "runs").mkdir(parents=True)
        (repo / ".harness" / "runs" / "run_same").symlink_to(self.base / "elsewhere" / "run_same")
        cases["run-link"] = (repo, self.elsewhere, refused)
        repo = self.repository("ledger-link")
        (repo / ".harness" / "runs" / "run_same").mkdir(parents=True)
        (repo / ".harness" / "runs" / "run_same" / "state.json").symlink_to(
            self.base / "elsewhere" / "run_same" / "state.json")
        cases["ledger-link"] = (repo, self.elsewhere, no_samples)
        for name, folder, quota_block in (("search-only-runs", "runs", refused), ("search-only-state", None, no_samples)):
            repo = self.repository(name)
            write_json(repo / ".harness" / "runs" / "run_same" / "state.json", self.ordinary)
            self.restrict(repo / ".harness" / folder if folder else repo / ".harness", 0o111)
            cases[name] = (repo, self.ordinary, quota_block)
        for name, (repo, value, quota_block) in cases.items():
            location = f"harness: 检出 {repo}；状态目录 {repo / '.harness'}"
            for extra, expected in (((), printed({**written_summary(value["goal"]), "quota": quota_block})),
                                    (("--full",), printed(value))):
                with self.subTest(layout=name, extra=extra):
                    code, output, errors = self.status(repo, "run_same", *extra)
                    self.assertEqual((code, errors.splitlines()), (0, [location]), errors)
                    self.assertEqual(output, expected)

    def test_unreadable_layouts_keep_the_path_reads_stop_and_exit(self):
        if os.geteuid() == 0:
            self.skipTest("permission bits do not stop root")
        cases = {}
        repo = self.repository("unreadable-ledger")
        write_json(repo / ".harness" / "runs" / "run_same" / "state.json", self.ordinary)
        self.restrict(repo / ".harness" / "runs" / "run_same" / "state.json", 0)
        cases["unreadable-ledger"] = (repo, "PermissionError", True)
        repo = self.repository("directory-ledger")
        (repo / ".harness" / "runs" / "run_same" / "state.json").mkdir(parents=True)
        cases["directory-ledger"] = (repo, "IsADirectoryError", True)
        repo = self.repository("runs-file")
        (repo / ".harness").mkdir()
        (repo / ".harness" / "runs").write_text("")
        cases["runs-file"] = (repo, "NotADirectoryError", True)
        repo = self.repository("unreadable-state")
        write_json(repo / ".harness" / "runs" / "run_same" / "state.json", self.ordinary)
        self.restrict(repo / ".harness", 0)
        cases["unreadable-state"] = (repo, "PermissionError", False)
        for name, (repo, error_type, opened) in cases.items():
            location = f"harness: 检出 {repo}；状态目录 {repo / '.harness'}" + ("" if opened else "（无法打开）")
            for extra in ((), ("--full",)):
                with self.subTest(layout=name, extra=extra):
                    code, output, errors = self.status(repo, "run_same", *extra)
                    notice = cli_notice(errors)
                    self.assertEqual((code, output, errors.splitlines()[0]), (1, "", location), errors)
                    self.assertEqual((notice["kind"], notice["reason"], notice["exit_code"], notice["data"]["error_type"]),
                                     ("runtime_failure", "io", 1, error_type), errors)
        # Only an absent run or ledger is "no such run".
        repo = self.repository("absent")
        (repo / ".harness" / "runs" / "run_same").mkdir(parents=True)
        for run_id in ("run_same", "run_other"):
            code, output, errors = self.status(repo, run_id)
            self.assertEqual((code, output, cli_notice(errors)["kind"], cli_notice(errors)["reason"]),
                             (2, "", "invalid_input", "cli_arguments"), errors)

    def test_a_search_only_descriptor_that_cannot_search_binds_nothing(self):
        if os.geteuid() == 0:
            self.skipTest("permission bits do not stop root")
        repo = self.repository("descriptor-only")
        state = repo / ".harness"
        write_json(state / "runs" / "run_same" / "state.json", self.ordinary)
        held = os.open(state, os.O_RDONLY | os.O_DIRECTORY)  # taken while the directory still has permissions
        self.addCleanup(os.close, held)
        self.restrict(state, 0)
        real_open, handed = os.open, []

        def descriptor_for_any_mode(path, flags, mode=0o777, *, dir_fd=None):
            # Linux O_PATH on every platform: the search-only open yields a descriptor whatever the directory's mode,
            # while a lookup through it is still checked against the directory's search permission.
            if path == str(state) and flags == observe._SEARCH_FLAGS:
                handed.append(os.dup(held))
                return handed[-1]
            return real_open(path, flags, mode, dir_fd=dir_fd)

        location = f"harness: 检出 {repo}；状态目录 {state}（无法打开）"
        with patch("harness.observe.os.open", side_effect=descriptor_for_any_mode):
            for args in ((), ("run_same",), ("run_same", "--full")):
                with self.subTest(args=args):
                    code, output, errors = self.status(repo, *args)
                    notice = cli_notice(errors)
                    self.assertEqual((code, output, errors.splitlines()[0]), (1, "", location), errors)
                    self.assertEqual((notice["kind"], notice["reason"], notice["exit_code"], notice["data"]["error_type"]),
                                     ("runtime_failure", "io", 1, "PermissionError"), errors)
        self.assertEqual(len(handed), 3)  # each call was handed a descriptor and gave it back
        for fd in handed:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_fifo_ledger_is_refused_without_blocking(self):
        repo = self.repository("fifo")
        folder = repo / ".harness" / "runs" / "run_same"
        folder.mkdir(parents=True)
        os.mkfifo(folder / "state.json")
        for extra in ((), ("--full",)):
            with self.subTest(extra=extra):
                # A separate process, so a blocking open fails this test by its timeout instead of hanging the suite.
                result = subprocess.run([sys.executable, "-m", "harness", "--repo", str(repo), "status", "run_same", *extra],
                                        cwd=ROOT, capture_output=True, text=True, timeout=60)
                notice = cli_notice(result.stderr)
                self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
                self.assertEqual((notice["kind"], notice["reason"], notice["data"]["error_type"]),
                                 ("runtime_failure", "io", "PathRefused"), result.stderr)
        self.assertTrue(stat.S_ISFIFO((folder / "state.json").lstat().st_mode))

    def test_location_line_stays_one_line_for_any_path(self):
        repo = self.repository("odd")
        target = self.base / 'a\n[harness-json] {"event":"stop","stop":{"kind":"bogus"}}\u2028\x1b[31mz\\'
        write_json(target / "runs" / "run_same" / "state.json", self.ordinary)
        (repo / ".harness").symlink_to(target, target_is_directory=True)
        shown = f'{self.base}/a\\n[harness-json] {{"event":"stop","stop":{{"kind":"bogus"}}}}\\u2028\\x1b[31mz\\\\'
        for extra, expected in (((), None), (("--full",), printed(self.ordinary))):
            with self.subTest(extra=extra):
                code, output, errors = self.status(repo, "run_same", *extra)
                self.assertEqual((code, errors.splitlines()), (0, [f"harness: 检出 {repo}；状态目录 {shown}"]), errors)
                self.assertIsNone(cli_notice(errors))
                self.assertEqual(json.loads(output)["goal"], "ordinary")
                if expected is not None:
                    self.assertEqual(output, expected)
        # A real stop still comes after the location line and is the one a reader of `[harness-json]` lines finds.
        code, output, errors = self.status(repo, "run_absent")
        lines = errors.splitlines()
        self.assertEqual((code, lines[0]), (2, f"harness: 检出 {repo}；状态目录 {shown}"))
        self.assertEqual([json.loads(line.split(" ", 1)[1])["stop"]["reason"] for line in lines
                          if line.startswith("[harness-json] ")], ["cli_arguments"])


class PathPolicyTests(unittest.TestCase):
    def test_fixed_roots_and_refusals(self):
        dispatch_id = str(uuid.uuid4())
        for path in ("state.json", "events.jsonl", "plans/revision-0.json",
                     f"dispatches/{dispatch_id}/process.stdout",
                     f"dispatches/{dispatch_id}/commands/{'a' * 32}/output.stdout",
                     "verification/tasks/greet/1/retry-2/results.json",
                     "path-indexes/abc.json"):
            self.assertEqual("/".join(observe.validate_path(path)), path)
        for path in ("", "/etc/passwd", "../state.json", "state.json/x", "plans", "worktrees/x", ".hidden",
                     "dispatches/not-a-uuid/prompt.txt", f"dispatches/{dispatch_id.upper()}/prompt.txt",
                     f"dispatches/{dispatch_id}/commands/XYZ/output.stdout",
                     "verification/" + "/".join(["d"] * 8), "hooks/greet-1/log.stdout",
                     "indexes/abc.json", "batches/abc.json"):   # never written at the top of a run
            with self.subTest(path=path), self.assertRaises(observe.InvalidPath):
                observe.validate_path(path)
        for path in ("run.lock", "store.lock", f"dispatches/{dispatch_id}/mcp.json", "plans/revision-0.json.abc.tmp",
                     "checkpoints/memory.sqlite3-journal"):
            with self.subTest(path=path), self.assertRaises(observe.PathRefused):
                observe.validate_path(path)


# One file in every place the runtime writes evidence at the top of a run directory.
RUN_EVIDENCE = ("workspace-inputs.json", "workspace-inputs-manifest.json", "continuations/c1.json",
                "continuation-rejections/r1.json", "execution-guidance/g1.json", "proposal-rejections/p1.json",
                "input-indexes/i1.json", "finding-progress/indexes/f1.json", "preflight/greet/1/result.json",
                "checkpoints/batches/b1.json", "checkpoints/indexes/x1.json", "environment/indexes/e1.json")


class RunEvidenceIndexTests(StateCase):
    def test_every_evidence_location_is_listed_and_readable(self):
        run_id = self.save(ledger("run_evidence"))
        folder = self.state / "runs" / run_id
        for path in RUN_EVIDENCE:
            write_json(folder / path, {"path": path})
        (folder / "store.lock").write_text("")
        (folder / "unknown-top.json").write_text("{}")
        with observe.Observer(self.state) as observer:
            index = observer.file_index(run_id)
            listed = {item["path"] for item in index["files"]}
            self.assertLessEqual(set(RUN_EVIDENCE), listed)
            for path in RUN_EVIDENCE:
                with self.subTest(path=path):
                    self.assertEqual(json.loads(observer.read_file(run_id, path)["text"]), {"path": path})
        # Named exclusions are counted as such; anything else at the top is unaddressable, never "excluded".
        self.assertEqual((index["skipped"]["excluded"], index["skipped"]["unaddressable"]), (1, 1))


class RunFileGuardTests(unittest.TestCase):
    """The repository tests' guard: an unregistered entry the runtime writes into a run directory fails the test."""

    def outcome(self, plant):
        class Planted(RepoCase):
            def runTest(inner):
                result = inner.engine().start("Implement greet")
                plant(inner.repo / ".harness" / "runs" / result["id"])
        result = unittest.TestResult()
        Planted().run(result)
        return [text for _, text in result.failures + result.errors]

    def test_an_unregistered_run_directory_fails_the_test_that_left_it(self):
        self.assertEqual(self.outcome(lambda folder: None), [])

        def new_directory(folder):
            (folder / "new-evidence").mkdir()
            (folder / "new-evidence" / "record.json").write_text("{}")
        (failure,) = self.outcome(new_directory)
        self.assertIn("/new-evidence/", failure)
        (failure,) = self.outcome(lambda folder: (folder / "new-top.json").write_text("{}"))
        self.assertIn("new-top.json", failure)


# ----- K11: one signal anywhere in an entry's construction, hand-off or cleanup leaves no descriptor open -----

SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def open_descriptors():
    return len(os.listdir("/dev/fd"))


def functions(module, *names):
    return {(module.__file__, name) for name in names}


# From each open to its owner, and the cleanups that close it. Each of their line, call and return events counts: a
# call event is a function's entry, where a pending handler runs before any of its code (so before any try of its
# own); a return event is where a built value is not stored yet.
CHAIN = (functions(observe, "StateReader.__init__", "StateReader.close", "Observer.__init__", "Observer.close",
                   "observing", "observing.<locals>.opened", "observing.<locals>.close", "holding", "_closing")
         | functions(dashboard, "DashboardServer.__init__", "DashboardServer.__init__.<locals>.build",
                     "DashboardServer._discard", "DashboardServer.server_close", "create_server", "serve",
                     "serve.<locals>.serving", "serve.<locals>.restore", "serve.<locals>.close"))
# A deferral's entry and exit (call events only): before it holds the handlers, a signal leaves its with statement.
DEFERRAL = (functions(contextlib, "_GeneratorContextManager.__enter__", "_GeneratorContextManager.__exit__")
            | functions(process_runtime, "defer_cleanup_signals"))
CONSTRUCTORS = {"StateReader.__init__", "Observer.__init__", "observing.<locals>.opened"}
CLEANUPS = {"holding", "_closing", "Observer.close", "StateReader.close"}


def lines_between(function, first, last):
    """``function``'s line numbers from the first line holding ``first`` through the next one holding ``last``."""
    source, start = inspect.getsourcelines(function)
    begin = next(index for index, line in enumerate(source) if first in line)
    end = next(index for index, line in enumerate(source) if index >= begin and last in line)
    return set(range(start + begin, start + end + 1))


@functools.lru_cache(maxsize=None)
def bare_lines(code):
    """Lines of ``code`` that are a NOP alone (a bare ``try:``). A signal handler runs only where the interpreter
    checks for one, never at a NOP; an interrupt a tracer raises there is outside every exception-table range."""
    kinds = {}
    for instruction in dis.get_instructions(code):
        if instruction.positions is not None and instruction.positions.lineno is not None:
            kinds.setdefault(instruction.positions.lineno, set()).add(instruction.opname)
    return {line for line, names in kinds.items() if names == {"NOP"}}


@contextlib.contextmanager
def one_signal(index, lines, signum, stop, once=frozenset()):
    """Send ``signum`` to this process once, at the ``index``-th counted trace event; yield where it was sent.

    Counted until ``stop`` is non-empty: every line, call and return event of CHAIN, the call events of a cleanup
    deferral's entry and exit, and the line events of the functions ``lines`` maps ((file, qualified name) -> None
    for every line, or the line numbers counted). A line in ``once`` ((key, line) pairs) counts at its first event in
    a frame only; a bare ``try:`` never counts. ``at`` stays None when fewer were counted.
    """
    sent = {"at": None, "count": 0}
    seen = {}
    deferral = process_runtime.defer_cleanup_signals.__wrapped__.__code__

    def counted(frame, event, key):
        if key in DEFERRAL:
            # The deferral's own context manager, not any other one built by contextmanager.
            made = getattr(frame.f_locals.get("self"), "gen", None)
            return event == "call" and deferral in (frame.f_code, getattr(made, "gi_code", None))
        if event in ("call", "return"):
            return key in CHAIN
        if event != "line" or frame.f_lineno in bare_lines(frame.f_code):
            return False
        if key not in CHAIN and (key not in lines or lines[key] is not None and frame.f_lineno not in lines[key]):
            return False
        if (key, frame.f_lineno) not in once:
            return True
        first = frame.f_lineno not in seen.setdefault(frame, set())
        seen[frame].add(frame.f_lineno)
        return first

    def send(frame, event):
        key = (frame.f_code.co_filename, frame.f_code.co_qualname)
        if sent["at"] is None and not stop and counted(frame, event, key):
            if sent["count"] == index:
                sent["at"] = f"{key[1]}:{frame.f_lineno}:{event}"
                os.kill(os.getpid(), signum)
            sent["count"] += 1
        return key

    def local(frame, event, arg):
        send(frame, event)
        return local

    def calls(frame, event, arg):
        key = send(frame, event)  # a call event reaches only this function
        return local if key in lines or key in CHAIN else None

    previous = sys.gettrace()
    sys.settrace(calls)
    try:
        yield sent
    finally:
        sys.settrace(previous)


def outcome(call):
    """``call``'s value, or what left it: an error's type, or an interrupt ("deferred" when a deferral raised it)."""
    try:
        return call()
    except KeyboardInterrupt as error:
        innermost = error.__traceback__
        while innermost.tb_next is not None:
            innermost = innermost.tb_next
        deferral = process_runtime.defer_cleanup_signals.__wrapped__.__code__
        return "deferred interrupt" if innermost.tb_frame.f_code is deferral else "interrupt"
    except Exception as error:
        return type(error).__name__


def isolated(call):
    """``outcome(call)`` in a generator's frame, which has an exception state of its own: what ``call`` leaves handled
    stays there as the generator yields, not on this thread. A tracer's interrupt at an except block's POP_EXCEPT (the
    line of a ``pass``) is outside every exception-table range: it leaves the block's exception handled."""
    def frame():
        yield outcome(call)
    return next(frame())


class InterruptedConstructionTests(RepoCase):
    """K11: one SIGINT or SIGTERM at any line of an observation entry's construction, hand-off or cleanup leaves the
    process's descriptors as they were, and the interruption still leaves the call."""

    RUN, DISPATCH = "run_k11", "dispatch-1"

    def setUp(self):
        super().setUp()
        self.state = self.repo / ".harness"
        write_json(self.state / "runs" / self.RUN / "state.json",
                   ledger(self.RUN, status="completed", dispatches=[dispatch(id=self.DISPATCH)]))

    def sweep(self, call, lines, *, signum=signal.SIGINT, stop=None, once=frozenset()):
        """Call ``call`` with one ``signum`` at each counted event in turn, then once with none left to count.

        Every call leaves the descriptor count and the signal handlers as it found them and no exception handled on
        this thread, and no signalled call goes on to ``stop`` (set where an unsignalled call would block). Returns
        [(where, outcome)] of the signalled calls and the last call's outcome.
        """
        stop = [] if stop is None else stop
        handlers = [signal.getsignal(number) for number in SIGNALS]
        runs, leaks = [], []
        gc.collect()  # an earlier test's garbage closing its sockets must not move the count mid-sweep
        for index in itertools.count():
            stop.clear()
            before = open_descriptors()
            with one_signal(index, lines, signum, stop, once) as sent:
                result = isolated(call)
            # Left handled, it would be the __context__ of every exception raised later in this process.
            self.assertIsNone(sys.exception(), sent["at"])
            if open_descriptors() != before:
                leaks.append((sent["at"], open_descriptors() - before))
            self.assertEqual([signal.getsignal(number) for number in SIGNALS], handlers, sent["at"])
            if sent["at"] is None:
                break
            self.assertEqual(stop, [], f"{sent['at']}: the call went on past its signal")
            runs.append((sent["at"], result))
        self.assertEqual(leaks, [])
        return runs, result

    def cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(list(argv))

    def assertReached(self, runs, names):
        self.assertLessEqual(set(names), {at.split(":")[0] for at, _ in runs})

    def assertCleanupEntered(self, runs, expected):
        """A signal as holding enters a cleanup (its call of _closing, before any deferral) left as ``expected``."""
        entry = f"_closing:{observe._closing.__code__.co_firstlineno}:call"
        self.assertIn((entry, expected), runs)

    def entries(self, run_id, dispatch_id):
        """Each CLI caller of the observer: its lines and its use's, every event (the watcher's with line once), and argv."""
        watching = (watch_cli.__file__, "watch_run.<locals>.watching")
        # The watcher's descriptors, closed by its own exit, are design 4's (D4-T12): its line counts as entered only.
        handed = {(watching, line) for line in lines_between(watch_cli.watch_run, "with RunWatcher(", "with RunWatcher(")}
        status = {(cli.__file__, "show_status"): None, (cli.__file__, "show_status.<locals>.show"): None}
        diagnostics = {(cli.__file__, "_main"): lines_between(cli._main, "from .permission_diagnostics import",
                                                              'data={"error_type": type(failure).__name__})) from error'),
                       (cli.__file__, "_main.<locals>.diagnose"): None}
        return {"status RUN": (status, (), ("--repo", str(self.repo), "status", run_id)),
                "permission-diagnostics": (diagnostics, (), (
                    "--config", str(ROOT / "harness.toml"), "--repo", str(self.repo), "permission-diagnostics",
                    self.RUN, dispatch_id)),
                "watch": ({(watch_cli.__file__, "watch_run"): None, watching: None}, handed,
                          ("watch", run_id, "--state", str(self.state)))}

    def test_cli_entries_exit_130_with_nothing_left_open(self):
        # Every line of each caller and of its use of the observer, through the cleanups of its normal exit.
        write_project_config(self.repo)
        entries = {"status": ({(cli.__file__, "show_status"): None, (cli.__file__, "show_status.<locals>.show"): None},
                              (), ("--repo", str(self.repo), "status")), **self.entries(self.RUN, self.DISPATCH)}
        for name, (lines, once, argv) in entries.items():
            for signum in (signal.SIGINT, signal.SIGTERM):
                with self.subTest(entry=name, signal=signum.name):
                    runs, last = self.sweep(lambda: self.cli(*argv), lines, signum=signum, once=once)
                    self.assertEqual(last, 0)
                    self.assertEqual({result for _, result in runs}, {130})
                    self.assertReached(runs, CONSTRUCTORS | CLEANUPS)
                    self.assertCleanupEntered(runs, 130)

    def test_a_signal_as_the_cleanup_after_a_failure_starts_still_leaves_nothing_open(self):
        # Each use fails (a run or a dispatch that is not there), so its cleanup starts with an error leaving: one
        # signal from the start through the end of that cleanup leaves nothing open and leaves in the error's place.
        write_project_config(self.repo)
        for name, (lines, once, argv) in self.entries("run_absent", "dispatch-absent").items():
            for signum in (signal.SIGINT, signal.SIGTERM):
                with self.subTest(entry=name, signal=signum.name):
                    runs, last = self.sweep(lambda: self.cli(*argv), lines, signum=signum, once=once)
                    self.assertEqual(last, 2)
                    self.assertEqual({result for _, result in runs}, {130})
                    self.assertReached(runs, CLEANUPS)
                    self.assertCleanupEntered(runs, 130)
        lines = {(observe.__file__, "run_detail"): None, (observe.__file__, "run_detail.<locals>.<lambda>"): None}
        runs, last = self.sweep(lambda: observe.run_detail(self.state, "run_absent"), lines)
        self.assertEqual(last, "NotFound")
        self.assertLessEqual({result for _, result in runs}, {"interrupt", "deferred interrupt"})
        self.assertCleanupEntered(runs, "interrupt")

    def test_a_signal_kept_in_a_deferral_is_raised_where_its_owner_closes_it(self):
        callers = {"list_runs": (lambda: observe.list_runs(self.state), {(observe.__file__, "list_runs"): None}),
                   "run_detail": (lambda: observe.run_detail(self.state, self.RUN),
                                  {(observe.__file__, "run_detail"): None,
                                   (observe.__file__, "run_detail.<locals>.<lambda>"): None})}
        for name, (call, lines) in callers.items():
            with self.subTest(caller=name):
                runs, last = self.sweep(call, lines)
                self.assertIsInstance(last, dict)
                self.assertLessEqual({result for _, result in runs}, {"interrupt", "deferred interrupt"})
                # Kept while a constructor or the hand-off stores what it built, or while a cleanup closes it: raised
                # as that deferral ends, inside the cleanups that close it, or once the close is done.
                deferred = {at.split(":")[0] for at, result in runs if result == "deferred interrupt"}
                self.assertLessEqual(CONSTRUCTORS | {"observing.<locals>.close", "Observer.close", "StateReader.close"},
                                     deferred)
                self.assertCleanupEntered(runs, "interrupt")

    def test_dashboard_serve_owns_the_server_from_the_moment_it_is_built_until_it_is_closed(self):
        class Unbound(socket.socket):
            """A real socket that binds and listens to nothing: no port's state is involved."""

            def bind(self, address):
                pass

            def listen(self, backlog=0):
                pass
        served = []

        def stopped(server, poll_interval=0.5):
            served.append(server)
            raise KeyboardInterrupt()  # what the first Ctrl-C does while serving

        def returned(server, poll_interval=0.5):
            served.append(server)  # shut down from another thread

        stopping = lines_between(dashboard.serve, "server.serve_forever(", "server.serve_forever(")
        calls = {"serve": (lambda: dashboard.serve(self.state, 0, stdout=io.StringIO(), stderr=io.StringIO()),
                           (signal.SIGINT,), 0, "interrupt"),
                 "dashboard": (lambda: self.cli("--repo", str(self.repo), "dashboard", "--port", "0"),
                               (signal.SIGTERM,), 0, 130)}
        for (name, (call, signums, done, interrupted)), serving in itertools.product(calls.items(), (stopped, returned)):
            for signum in signums:
                with self.subTest(entry=name, serving=serving.__name__, signal=signum.name), \
                        patch.object(socket, "socket", Unbound), \
                        patch.object(dashboard.DashboardServer, "serve_forever", serving):
                    runs, last = self.sweep(call, {}, signum=signum)
                    self.assertEqual(last, done)
                    for server in served:  # each server built, closed: its socket and its directory
                        self.assertEqual(server.socket.fileno(), -1)
                        self.assertFalse(server.observer.reader.opened)
                    served.clear()
                    # A signal as the serving starts is serve's own stop; every other one leaves, the handlers serve
                    # swapped restored (sweep) even when it cut their restore short.
                    self.assertEqual({at for at, result in runs if result == done},
                                     {f"serve.<locals>.serving:{line}:line" for line in stopping})
                    self.assertLessEqual({result for _, result in runs}, {done, interrupted, "deferred interrupt"})
                    self.assertReached(runs, {"serve.<locals>.restore", "serve.<locals>.close",
                                              "DashboardServer.server_close"} | CLEANUPS)
                    self.assertCleanupEntered(runs, interrupted)
                    if name == "serve":
                        deferred = {at for at, result in runs if result == "deferred interrupt"}
                        # Built and returned, not yet stored by serve: kept until it is, then raised inside holding.
                        self.assertTrue(any(at.startswith("create_server:") and at.endswith(":return") for at in deferred))
                        self.assertLessEqual({"DashboardServer.__init__.<locals>.build", "Observer.__init__",
                                              "StateReader.__init__", "DashboardServer.server_close", "Observer.close"},
                                             {at.split(":")[0] for at in deferred})

    def socket_refused(self, refusal, expected, made=()):
        """The constructor and serve with ``refusal`` in force: each fails with errno ``expected`` and leaves nothing
        open, without a signal and with one at each event; each socket in ``made`` was closed."""
        entries = {"DashboardServer": lambda: dashboard.DashboardServer(self.state, 0, log=io.StringIO()),
                   "serve": lambda: dashboard.serve(self.state, 0, stdout=io.StringIO(), stderr=io.StringIO())}
        gc.collect()
        for entry, start in entries.items():
            with self.subTest(entry=entry), refusal:
                before = open_descriptors()
                with self.assertRaises(OSError) as raised:
                    start()
                self.assertEqual((raised.exception.errno, open_descriptors()), (expected, before))
                runs, last = self.sweep(start, {})
                self.assertEqual(last, "OSError")
                # Kept in a deferral an error leaves, a signal gives way to it (defer_cleanup_signals).
                self.assertLessEqual({result for _, result in runs}, {"OSError", "interrupt", "deferred interrupt"})
                self.assertReached(runs, {"DashboardServer._discard"} | CLEANUPS)
                # As the constructor's own cleanup starts: holding's, before any deferral.
                self.assertCleanupEntered(runs, "interrupt")
        self.assertTrue(all(each.fileno() == -1 for each in made))

    def test_dashboard_socket_that_cannot_be_created(self):
        # socket() fails before TCPServer's own cleanup: only the constructor's closes the observer.
        emfile = OSError(errno.EMFILE, "Too many open files (test)")
        self.socket_refused(patch("socket.socket", side_effect=emfile), errno.EMFILE)

    def test_dashboard_port_already_in_use(self):
        # A real socket, created and closed; its bind fails as on a port another socket holds, whatever the ports are.
        made = []

        class Taken(socket.socket):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                made.append(self)

            def bind(self, address):
                raise OSError(errno.EADDRINUSE, "Address already in use (test)")
        self.socket_refused(patch.object(socket, "socket", Taken), errno.EADDRINUSE, made)
        self.assertTrue(made)

    def test_search_only_and_refused_layouts(self):
        if os.geteuid() == 0:
            self.skipTest("permission bits do not stop root")
        lines = {(observe.__file__, "list_runs"): None}
        state = scratch(self).resolve() / ".harness"  # the path the reader opens
        write_json(state / "runs" / self.RUN / "state.json", ledger(self.RUN))
        held = os.open(state, os.O_RDONLY | os.O_DIRECTORY)  # taken while the directory still has permissions
        self.addCleanup(os.close, held)
        real_open, handed = os.open, []

        def search_descriptor_for_any_mode(path, flags, mode=0o777, *, dir_fd=None):
            # Linux O_PATH: a descriptor whatever the mode; a lookup through it still needs search permission.
            if path == str(state) and flags == observe._SEARCH_FLAGS:
                handed.append(os.dup(held))
                return handed[-1]
            return real_open(path, flags, mode, dir_fd=dir_fd)

        layouts = {"search permission only": (0o111, contextlib.nullcontext(), True),
                   "search descriptor that cannot search": (0, patch("harness.observe.os.open",
                                                                      side_effect=search_descriptor_for_any_mode), False)}
        for name, (mode, opening, bound) in layouts.items():
            state.chmod(mode)
            self.addCleanup(state.chmod, 0o755)
            with self.subTest(layout=name), opening:
                runs, last = self.sweep(lambda: observe.list_runs(state), lines)
                self.assertEqual(last["runs"], [])
                self.assertEqual({result for _, result in runs}, {"interrupt", "deferred interrupt"})
                with observe.Observer(state) as observer:
                    self.assertIs(observer.reader.opened, bound)
        self.assertTrue(handed)  # the second layout got as far as the refused lookup
        state.chmod(0o755)
        # location() failing after the reader is built: the error leaves, the reader does not stay open.
        with patch("harness.observe.location", side_effect=RuntimeError("location (test)")):
            before = open_descriptors()
            self.assertEqual((outcome(lambda: observe.list_runs(state)), open_descriptors()), ("RuntimeError", before))


class InterruptedConstructionProcessTests(unittest.TestCase):
    def test_tests_after_the_sweeps_in_their_process_chain_only_their_own_exceptions(self):
        # The sweeps, then in the same process two tests that assert an exception chain's exact length: an exception
        # a sweep left handled would be the __context__ of what they raise.
        later = ("test_verification_outcomes.StopSaveChainTests."
                 "test_an_interrupt_while_a_refused_stop_save_unlocks_keeps_the_refusal_in_its_traceback",
                 "test_verification_outcomes.VerificationStopExitTests."
                 "test_a_refused_stop_save_keeps_the_rule_11_error_on_its_traceback_chain")
        swept = unittest.defaultTestLoader.loadTestsFromTestCase(InterruptedConstructionTests).countTestCases()
        proc = subprocess.run([sys.executable, "-m", "unittest", "test_observe.InterruptedConstructionTests", *later],
                              cwd=ROOT, env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT), str(ROOT / "tests")))),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stderr[-8000:])
        self.assertIn(f"Ran {swept + len(later)} tests", proc.stderr)


if __name__ == "__main__":
    unittest.main()
