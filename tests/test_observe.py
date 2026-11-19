"""Read-only observation data layer: ledger projection, invocation classes, events, operations."""
import contextlib
import errno
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from harness import stops
from harness import cli, git, observe
from harness.cli import status_summary
from harness.util import read_json, write_json
from harness.versions import RUN_SCHEMA_VERSION
from test_harness import ROOT, RepoCase, cli_notice, scratch

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
            for extra in ((), ("--full",)):
                with self.subTest(extra=extra):
                    code, output, errors = self.status(repo, "run_same", *extra)
                    notice = cli_notice(errors)
                    self.assertEqual((code, output, errors.splitlines()[0]), (1, "", location), errors)
                    self.assertEqual((notice["kind"], notice["reason"], notice["exit_code"], notice["data"]["error_type"]),
                                     ("runtime_failure", "io", 1, "PermissionError"), errors)
            code, output, errors = self.status(repo)
        self.assertEqual((code, output, errors.splitlines()), (0, "[]\n", [location]))
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


if __name__ == "__main__":
    unittest.main()
