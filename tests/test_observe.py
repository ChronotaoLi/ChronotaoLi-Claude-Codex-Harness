"""Read-only observation data layer: ledger projection, cost classes, events, operations."""
import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from harness import cli, observe
from harness.cli import status_summary
from harness.util import read_json, write_json
from test_harness import RepoCase

ROLES = {"commander": {"cli": "codex", "model": "gpt-6-astra", "effort": "high"},
         "fable": {"cli": "claude", "model": "claude-fable-5-1", "effort": "high"},
         "worker": {"cli": "claude", "model": "claude-opus-5-5", "effort": "high"},
         "reviewer": {"cli": "codex", "model": "gpt-6.1-sol", "effort": "high"}}


def ledger(run_id, **fields):
    base = {"schema_version": 11, "id": run_id, "status": "running", "goal": "Implement greet",
            "branch": "harness/" + run_id, "worktree": "/tmp/worktree", "commander": "astra",
            "tasks": [{"id": "greet", "title": "Implement greet", "depends_on": []},
                      {"id": "planning", "title": "A user task that happens to be named planning", "depends_on": ["greet"]}],
            "accepted": {"greet": {"commit": "abc"}}, "attempts": {"greet": 1}, "attempt_budget_policy": "work",
            "dispatches": [], "config_snapshot": {"roles": ROLES, "runtime": {"max_attempts": 3, "max_total_attempts": 60,
                                                                               "max_agent_dispatches": 200, "max_replans": 2,
                                                                               "max_final_attempts": 3}},
            "total_attempts": 1, "agent_dispatches": 1, "replan_attempts": 0, "final_attempts": 0,
            "replan_stopped": {}, "decision_requests": [], "operator_decisions": [], "created_by_operation": "op_a"}
    base.update(fields)
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
        self.assertEqual(item["schema_version"], 11)
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
        self.assertEqual(detail["cost"]["by_group"]["verification"]["dispatches"], 2)
        self.assertEqual(detail["cost"]["by_group"]["coordination"]["dispatches"], 1)
        self.assertEqual(detail["cost"]["by_provider"]["claude"]["invoked_no_usage"], 1)
        self.assertIsNone(detail["cost"]["by_provider"]["claude"]["reported_cost_usd"])
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
                                    error="hook pre_agent 拒绝继续"),
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
        cost = detail["cost"]
        self.assertEqual(cost["known_not_invoked"], 2)
        self.assertEqual(cost["unknown_invocation"], 6)
        self.assertEqual(cost["startup_unconfirmed"], 1)
        self.assertEqual(cost["by_group"]["verification"]["dispatches"], 1)
        claude = cost["by_provider"]["claude"]
        self.assertEqual((claude["invoked"], claude["usage_recorded"], claude["invoked_no_usage"]), (2, 1, 1))
        self.assertEqual(claude["reported_cost_usd"], 0.5)
        self.assertEqual(claude["reported_cost_dispatches"], 1)
        self.assertEqual(cost["by_provider"]["codex"]["invoked"], 1)
        total_model = sum(p["invoked"] for p in cost["by_provider"].values())
        self.assertEqual(total_model + cost["known_not_invoked"] + cost["unknown_invocation"] + 1, cost["counted_dispatches"])
        self.assertEqual(cost["by_group"]["coordination"]["dispatches"], 1)


class CostSummaryTests(StateCase):
    def test_by_provider_sums_null_rule_duplicates_and_role_based_groups(self):
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
            dispatch(provider_usage={"provider": "claude", "reported_cost_usd": -1}),  # invalid cost ignored
            dispatch(provider_usage={"provider": "claude", "reported_cost_usd": True}),  # bool is not a cost
        ]
        detail, _ = self.rows(ledger("run_cost", dispatches=rows))
        cost = detail["cost"]
        self.assertEqual(cost["duplicate_dispatch_ids"], [first["id"]])
        self.assertEqual(cost["counted_dispatches"], len(rows) - 1)
        claude = cost["by_provider"]["claude"]
        self.assertEqual((claude["invoked"], claude["usage_recorded"], claude["invoked_no_usage"]), (7, 6, 1))
        self.assertAlmostEqual(claude["reported_cost_usd"], 1.15)
        self.assertEqual(claude["reported_cost_dispatches"], 4)
        self.assertEqual(claude["label"], "CLI 报告的估算费用，非账户账单")
        codex = cost["by_provider"]["codex"]
        self.assertEqual((codex["invoked"], codex["usage_recorded"], codex["invoked_no_usage"]), (3, 2, 1))
        self.assertIsNone(codex["reported_cost_usd"])
        self.assertEqual(codex["reported_cost_dispatches"], 0)
        self.assertEqual(codex["label"], "供应商未报告美元费用")
        self.assertIsNone(cost["by_provider"]["unknown"]["reported_cost_usd"])
        self.assertEqual(cost["by_provider"]["unknown"]["invoked"], 0)
        self.assertNotIn("total", cost)
        coordination = cost["by_group"]["coordination"]
        self.assertEqual((coordination["dispatches"], coordination["invoked"], coordination["usage_recorded"],
                          coordination["invoked_no_usage"], coordination["known_not_invoked"]), (3, 2, 1, 1, 1))
        self.assertAlmostEqual(coordination["reported_cost_usd"]["claude"], 0.1)
        self.assertIsNone(coordination["reported_cost_usd"]["codex"])
        planning = cost["by_group"]["tasks"]["planning"]
        self.assertEqual(planning["dispatches"], 1)
        self.assertAlmostEqual(planning["reported_cost_usd"]["claude"], 0.3)
        greet = cost["by_group"]["tasks"]["greet"]
        self.assertEqual(greet["dispatches"], 6)
        self.assertAlmostEqual(greet["reported_cost_usd"]["claude"], 0.75)
        self.assertIsNone(greet["reported_cost_usd"]["codex"])
        self.assertEqual(greet["reported_cost_dispatches"]["codex"], 0)
        self.assertEqual(cost["by_group"]["tasks"]["final"]["invoked"], 1)
        rows_by_id = {row["id"]: row for row in detail["dispatches"]}
        self.assertEqual(rows_by_id[rows[4]["id"]]["provider_usage"]["tokens"], {"input_tokens": 10})


class ProviderModelTests(StateCase):
    def test_provider_and_model_come_from_config_snapshot(self):
        rows = [dispatch(role="commander", kind="plan", task_id="planning"),
                dispatch(), dispatch(role="reviewer", kind="review"),
                dispatch(provider_usage={"provider": "codex", "completed_turns": 1})]
        for commander, expected in (("astra", ("codex", "gpt-6-astra")), ("fable", ("claude", "claude-fable-5-1"))):
            with self.subTest(commander=commander):
                detail, by_id = self.rows(ledger("run_" + commander, commander=commander, dispatches=rows))
                self.assertEqual((by_id[rows[0]["id"]]["provider"], by_id[rows[0]["id"]]["model_id"]), expected)
                self.assertEqual((by_id[rows[1]["id"]]["provider"], by_id[rows[1]["id"]]["model_id"]),
                                 ("claude", "claude-opus-5-5"))
                self.assertEqual((by_id[rows[2]["id"]]["provider"], by_id[rows[2]["id"]]["model_id"]),
                                 ("codex", "gpt-6.1-sol"))
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
        self.assertEqual(detail["cost"]["by_provider"]["unknown"]["invoked"], 3)


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
        self.assertIn("对象", skipped["runs/run_list/state.json"])
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
        self.assertEqual(type_error["budgets"]["total_attempts"], 1)
        self.assertIsNotNone(observe.run_detail(self.state, good)["summary"])

    def test_old_schema_versions_mark_unavailable_blocks(self):
        for version, unavailable in ((9, {"decisions", "operation"}), (8, {"decisions", "environment", "operation"})):
            run = ledger(f"run_v{version}", schema_version=version)
            del run["created_by_operation"]
            detail = observe.run_detail(self.state, self.save(run))
            self.assertEqual(set(detail["unavailable"]), unavailable)
            self.assertEqual(detail["schema_version"], version)
            self.assertIsNotNone(detail["summary"])
            self.assertEqual(detail["summary"]["accepted_tasks"], ["greet"])
        detail = observe.run_detail(self.state, self.save(ledger("run_v11", created_by_operation=None)))
        self.assertEqual(detail["unavailable"], [])
        self.assertIsNone(detail["run"]["created_by_operation"])
        missing = ledger("run_noversion")
        del missing["schema_version"]
        detail = observe.run_detail(self.state, self.save(missing))
        self.assertIsNone(detail["schema_version"])
        self.assertIn("decisions", detail["unavailable"])


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
        run_id = self.save(ledger("run_huge", schema_version=huge, total_attempts=huge, dispatches=[row]))
        (self.state / "runs" / run_id / "events.jsonl").write_text(
            f'{{"time": {huge}, "event": "dispatch_started", "run_id": "{run_id}"}}\n'
            f'{{"time": {huge}, "event": "dispatch_finished", "run_id": "{run_id}", "dispatch_id": "{row["id"]}"}}\n')
        listing = observe.list_runs(self.state)
        self.assertEqual([item["id"] for item in listing["runs"]], [run_id])
        self.assertIsNone(listing["runs"][0]["last_event_time"])
        self.assertEqual(listing["runs"][0]["schema_version"], huge)
        detail = observe.run_detail(self.state, run_id)
        self.assertEqual(detail["timing"], {"first_event_time": None, "last_event_time": None, "basis": "events.jsonl 事件时间"})
        self.assertEqual(len(detail["events"]["records"]), 2)
        projected = detail["dispatches"][0]
        self.assertIsNone(projected["invoked_at"])
        self.assertIsNone(projected["finished_at"])
        self.assertEqual(projected["pgid"], huge)
        self.assertEqual(projected["invocation_class"], "usage_recorded")
        self.assertIsNone(detail["cost"]["by_provider"]["claude"]["reported_cost_usd"])
        self.assertEqual(detail["cost"]["by_provider"]["claude"]["usage_recorded"], 1)
        self.assertEqual(detail["budgets"]["total_attempts"], huge)
        self.assertEqual(detail["schema_version"], huge)  # Compared as an integer, never via float.
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
                self.assertEqual(detail["cost"]["by_group"]["coordination"]["dispatches"], 2)
                self.assertAlmostEqual(detail["cost"]["by_provider"]["claude"]["reported_cost_usd"], 0.2)


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


class PathPolicyTests(unittest.TestCase):
    def test_fixed_roots_and_refusals(self):
        dispatch_id = str(uuid.uuid4())
        for path in ("state.json", "events.jsonl", "plans/revision-0.json",
                     f"dispatches/{dispatch_id}/process.stdout",
                     f"dispatches/{dispatch_id}/commands/{'a' * 32}/output.stdout",
                     "verification/tasks/greet/1/retry-2/results.json",
                     "hooks/greet-1/pre_accept-1/hook-pre_accept-0.stdout", "path-indexes/abc.json"):
            self.assertEqual("/".join(observe.validate_path(path)), path)
        for path in ("", "/etc/passwd", "../state.json", "state.json/x", "plans", "worktrees/x", ".hidden",
                     "dispatches/not-a-uuid/prompt.txt", f"dispatches/{dispatch_id.upper()}/prompt.txt",
                     f"dispatches/{dispatch_id}/commands/XYZ/output.stdout",
                     "verification/" + "/".join(["d"] * 8), "hooks/" + "a" * 97):
            with self.subTest(path=path), self.assertRaises(observe.InvalidPath):
                observe.validate_path(path)
        for path in ("run.lock", f"dispatches/{dispatch_id}/mcp.json", "plans/revision-0.json.abc.tmp",
                     "checkpoints/memory.sqlite3-journal"):
            with self.subTest(path=path), self.assertRaises(observe.PathRefused):
                observe.validate_path(path)


if __name__ == "__main__":
    unittest.main()
