"""The bounded in-process quota wait (spec-8): decision, wait, wake-up checks, observation.

A Claude role's usage-limit failure is a real local child process run through
``CLIBackend`` (the classifier and the immediate judgment are the real ones); the quota
snapshot is the real probe landing on the ``claude``/``codex`` stubs (``quota_stubs``),
whose windows each test sets. ``harness.quota_wait.time`` is a fake clock: no test sleeps.
Every quota number and time is synthetic.
"""
import copy
import json
import math
import os
import re
import signal
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import quota_stubs
from harness import quota, quota_wait, stops
from harness.adapters import CLIBackend, DemoBackend
from harness.cli import status_summary
from harness.config import Config
from harness.quota_wait import QuotaWait, choose, parse_display, parse_model, relation, valid_record
from harness.run_lifecycle import RunLifecycle
from harness.run_store import RunStore
from harness.runtime import Runtime
from harness.util import HarnessError, lock, read_json, write_json
from test_harness import (ROOT, FakeClock, RepoCase, cli_notice, commit_recorder, crash_after_save,
                          install_clock, run_cli, walk_exit)

T0 = 1925000000.0   # a synthetic start, late 2030
QUOTA = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 3,
         "result": "SYNTHETIC: you've hit your limit; resets 2031-01-01T09:00:00Z",
         "terminal_reason": "api_error", "api_error": "usage_limit_reached", "api_error_status": 429}
CODEX_QUOTA_TEXT = {"type": "turn.failed", "error": {"message": "SYNTHETIC usage_limit_reached 429; try again at 09:00"}}
SAMPLE_FIELDS = ("quota_snapshot", "quota_sample", "quota_reason", "full_windows")
WAIT_FIELDS = ("wait_reason", "waits", "window", "resets_at", "wait_seconds", "wait_limit_seconds",
               "uncertain_windows", "uncertain_windows_omitted")


def plan():
    return read_json(ROOT / "examples" / "plan.json")


def window(kind, percent, reset_in=None, model=None, resets_at=None):
    """One Claude window for the stub: ``reset_in`` seconds after the fake clock's now, or an absolute ``resets_at``."""
    return {"kind": kind, "percent": percent, "reset_in": reset_in, "model": model, "resets_at": resets_at}


class QuotaFailingBackend(DemoBackend):
    """Scripted model calls; every other call is the offline demo.

    A script item ``role`` makes that role's next call a real local child that prints
    Claude's usage-limit envelope and exits 1; ``(role, "codex")`` prints a Codex-style
    failure event instead; ``(role, "invalid")`` returns an invalid output. Snapshots
    use the real probe (``CLIBackend.quota_snapshot``), so the stubs answer them.
    """

    def __init__(self, config, repo, script=()):
        self.config, self.state, self.script, self.calls = config, Path(repo) / ".harness", list(script), []

    def quota_snapshot(self, provider):
        return CLIBackend(self.config, self.state).quota_snapshot(provider)

    def invoke(self, **kwargs):
        role = kwargs["role"]
        self.calls.append((role, kwargs["kind"], kwargs["job"].name))
        step = self.script[0] if self.script else None
        name, mode = step if isinstance(step, tuple) else (step, "quota")
        if name != role:
            return super().invoke(**kwargs)
        self.script.pop(0)
        if mode == "invalid":
            return {**super().invoke(**kwargs), "action": "not-an-action"}
        terminal = CODEX_QUOTA_TEXT if mode == "codex" else QUOTA
        program = f"import sys; print({json.dumps(terminal)!r}, flush=True); sys.exit(1)"
        backend = CLIBackend(self.config, self.state)
        backend.command = lambda *args: [sys.executable, "-c", program]
        return backend.invoke(**kwargs)


class WaitCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.clock = install_clock(self, FakeClock(T0))
        # Fresh stubs that also record the ledger each probe saw.
        self.calls = quota_stubs.install(self, state=self.repo / ".harness")
        self.addCleanup(lambda: self.assertEqual(self.calls.unexpected(), []))

    def windows(self, *rows):
        os.environ["QUOTA_STUB_WINDOWS"] = json.dumps(list(rows))

    def backend(self, *script):
        return QuotaFailingBackend(self.config, self.repo, script)

    def engine(self, backend=None, commander="astra", config=None):
        return Runtime(config or self.config, self.repo, backend=backend or self.backend(), commander=commander)

    def start(self, backend, *, commander="astra", tasks=True, expect=None, config=None):
        engine = self.engine(backend, commander, config)
        if expect is None:
            return engine, engine.start("Implement greet", plan() if tasks else None)
        with self.assertRaises(expect) as caught:
            engine.start("Implement greet", plan() if tasks else None)
        self.error = caught.exception
        return engine, read_json(engine.directory / "state.json")

    def samples(self, engine):
        """(provider, trigger, file) of every sample, in the order taken."""
        folder = engine.directory / "observations" / "quota"
        found = [(match.group(3), int(match.group(4)), match.group(5), match.group(6), path.name)
                 for path in (folder.iterdir() if folder.exists() else ())
                 if (match := quota.NAME.fullmatch(path.name))]
        return [(provider, trigger, name) for _, _, provider, trigger, name in sorted(found)]

    def triggers(self, engine, provider="claude"):
        return [trigger for name, trigger, _ in self.samples(engine) if name == provider]

    def events(self, engine, name=None):
        rows = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        return [row for row in rows if name is None or row["event"] == name]

    def declined(self, saved, reason):
        """A usage-limit stop that did not wait: blocked, no record, the reason, the failure sample once."""
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(saved["status"], "blocked")
        self.assertNotIn("quota_wait", saved)
        self.assertEqual((saved["stop"]["kind"], saved["stop"]["reason"]), ("provider_quota", "usage_limit"))
        self.assertEqual((saved["stop"]["data"]["wait_reason"], saved["stop"]["data"]["waits"]), (reason, 0))
        self.assertEqual(stops.exit_code(self.error), 3)
        stops.validate(saved["stop"])


class PureTests(unittest.TestCase):
    """T8-1: the display-name relation and the window choice, as pure functions."""
    MODELS = {"claude-opus-5-5", "claude-fable-5-2"}

    def test_relation_table(self):
        rows = [
            ("Opus", "claude-opus-5-5", self.MODELS, "applies"),
            ("Opus 5.5", "claude-opus-5-5", self.MODELS, "applies"),
            ("Claude Opus 5.5", "claude-opus-5-5", self.MODELS, "applies"),
            ("OPUS 5-5", "claude-opus-5-5", self.MODELS, "applies"),
            ("Opus 5.5", "claude-opus-5-5-20311231", {"claude-opus-5-5-20311231"}, "applies"),
            ("Opus", "opus", {"opus"}, "applies"),
            ("Opus 5", "claude-opus-5-5", self.MODELS, "uncertain"),                         # review counterexample 1
            ("Opus 5.5 / Opus 5.6", "claude-opus-5-6", {"claude-opus-5-6"}, "uncertain"),    # review counterexample 2
            ("Opus 5.5 (1M)", "claude-opus-5-5", self.MODELS, "uncertain"),
            ("Opus 5.5 20311231", "claude-opus-5-5", self.MODELS, "uncertain"),
            ("Opus 5.05", "claude-opus-5-5", self.MODELS, "uncertain"),
            ("Opus 4", "claude-opus-4-1", {"claude-opus-4-1"}, "uncertain"),
            ("Opus 5.5", "claude-opus-5", {"claude-opus-5"}, "uncertain"),
            ("Model Z", "claude-opus-5-5", self.MODELS, "uncertain"),
            ("Opus", "claude-opus-5-5", {"claude-opus-5-5", "claude-opus-4-1"}, "uncertain"),
            ("Opus", "claude-opus-5-5", {"claude-opus-5-5", "claude-opus-5-5[1m]"}, "uncertain"),  # cannot rule out
            ("Opus 5.5", "opus", {"opus"}, "uncertain"),
            ("Sonnet", "opus", {"opus"}, "uncertain"),
            ("Sonnet", "claude-opus-5-5[1m]", {"claude-opus-5-5[1m]"}, "uncertain"),
            ("Opus 5.5 ★", "claude-opus-5-5", self.MODELS, "uncertain"),
            ("Claude", "claude-opus-5-5", self.MODELS, "uncertain"),
            ("5.5", "claude-opus-5-5", self.MODELS, "uncertain"),
            (None, "claude-opus-5-5", self.MODELS, "uncertain"),
            ("", "claude-opus-5-5", self.MODELS, "uncertain"),
            ("Sonnet", "claude-opus-5-5", self.MODELS, "unrelated"),
            ("Sonnet 4.5", "claude-opus-5-5", self.MODELS, "unrelated"),
            ("Op", "claude-opus-5-5", self.MODELS, "unrelated"),
            ("Opus 4.1", "claude-opus-5-5", self.MODELS, "unrelated"),
            ("Opus 4.1", "claude-opus-4-10", {"claude-opus-4-10"}, "unrelated"),
            ("Opus 5.6", "claude-opus-5-5-20311231", {"claude-opus-5-5-20311231"}, "unrelated"),
        ]
        for display, failed, models, expected in rows:
            with self.subTest(display=display, failed=failed, models=sorted(models)):
                self.assertEqual(relation(display, failed, models)[0], expected)
        self.assertEqual((parse_display("Claude Opus 5.5"), parse_model("claude-opus-5-5-20311231")),
                         (("opus", ("5", "5")), ("opus", ("5", "5"), True)))
        self.assertEqual(parse_model("opus"), ("opus", (), False))

    @staticmethod
    def full(kind, percent, resets_at, model=None):
        return {"kind": kind, "scope_model": model, "used_percent": percent, "resets_at": resets_at}

    def decide(self, *windows, available=True, limit=21600):
        return choose({"available": available, "windows": list(windows)}, "claude-opus-5-5", self.MODELS, T0, limit)

    def test_choose_boundaries(self):
        full = self.full
        cases = [
            ("latest of two", (full("session", 100, T0 + 3600), full("weekly_all", 100, T0 + 10800)),
             {"wait": True, "window": "weekly_all", "until": T0 + 10860, "delay": 10860}),
            ("weekly in the limit", (full("weekly_all", 100, T0 + 7200),), {"wait": True, "delay": 7260}),
            ("over 100 counts", (full("session", 105, T0 + 60),), {"wait": True}),
            ("99.9 does not", (full("session", 99.9, T0 + 60),), {"wait": False, "reason": "no_blocking_window"}),
            ("reset unknown", (full("session", 100, None),), {"wait": False, "reason": "reset_unknown",
                                                              "window": "session"}),
            ("reset passed", (full("session", 100, T0 - 60),), {"wait": False, "reason": "reset_passed"}),
            ("equal to the limit", (full("session", 100, T0 + 21600 - 60),), {"wait": True, "delay": 21600}),
            ("one second over", (full("session", 100, T0 + 21600 - 59),), {"wait": False, "reason": "exceeds_limit"}),
            ("unknown kind", (full("monthly", 100, T0 + 60),), {"wait": False, "reason": "window_uncertain",
                                                                "uncertain": ["monthly: unknown_kind"]}),
            ("uncertain beats a certain one", (full("session", 100, T0 + 60),
                                               full("weekly_scoped", 100, T0 + 60, "Opus 5")),
             {"wait": False, "reason": "window_uncertain", "uncertain": ["weekly_scoped（Opus 5）: version_prefix"]}),
            ("unrelated is ignored", (full("session", 100, T0 + 3600), full("weekly_scoped", 100, T0 + 18000, "Sonnet")),
             {"wait": True, "window": "session", "delay": 3660}),
            ("matching scoped", (full("session", 40, T0 + 600), full("weekly_scoped", 100, T0 + 7200, "Opus")),
             {"wait": True, "window": "weekly_scoped（Opus）", "delay": 7260}),
            ("equal resets keep the first", (full("weekly_all", 100, T0 + 600), full("session", 100, T0 + 600)),
             {"wait": True, "window": "weekly_all"}),
        ]
        for name, windows, expected in cases:
            with self.subTest(case=name):
                decision = self.decide(*windows)
                self.assertEqual({key: decision.get(key) for key in expected}, expected)
        self.assertEqual(self.decide(full("session", 100, T0 + 60), available=False)["reason"], "snapshot_unavailable")
        self.assertEqual(choose(None, "claude-opus-5-5", self.MODELS, T0, 21600)["reason"], "snapshot_unavailable")
        many = [full("weekly_scoped", 100, T0 + 60, f"Model {chr(65 + i)}") for i in range(20)]
        self.assertEqual(len(self.decide(*many)["uncertain"]), 16)

    def test_the_record_shape_is_one_function(self):
        record = {"dispatch_id": "d-1", "role": "worker", "window": "w" * 160, "resets_at": T0, "until": T0 + 60,
                  "recorded_at": T0 - 1.5, "limit_seconds": 21600, "sample": None}
        self.assertTrue(valid_record(record))
        for change in ({"window": "w" * 161}, {"window": ""}, {"until": quota.EPOCH_MAX + 1}, {"resets_at": -1},
                       {"recorded_at": float("nan")}, {"recorded_at": True}, {"role": "operator"},
                       {"dispatch_id": "bad id"}, {"limit_seconds": 0}, {"limit_seconds": 1.0},
                       {"sample": "not-a-sample.json"}, {"extra": 1}):
            with self.subTest(change=change):
                self.assertFalse(valid_record({**record, **change}))
        self.assertFalse(valid_record({key: value for key, value in record.items() if key != "sample"}))


class WaitTests(WaitCase):
    def test_wait_then_every_gate_derives_a_commander_decision(self):  # T8-2
        seen = {}

        def during(clock, seconds):
            if seen:
                return
            disk = read_json(engine.directory / "state.json")
            seen.update(status=disk["status"], wait=disk.get("quota_wait"), summary=status_summary(disk).get("quota_wait"))
            with lock(engine.directory / "store.lock"):
                seen["store_free"] = True
            with self.assertRaises(HarnessError):
                with lock(engine.directory / "run.lock"):
                    pass
        self.clock.on_sleep = during
        self.windows(window("session", 100, 3600))
        backend = self.backend("worker")
        engine = self.engine(backend)
        recorder, states = commit_recorder()
        with recorder:
            result = engine.start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("quota_wait", result)
        self.assertEqual(sum(self.clock.sleeps), 3660)
        self.assertTrue(all(0 < seconds <= 60 for seconds in self.clock.sleeps))
        self.assertEqual((seen["status"], seen["wait"]["window"], seen["wait"]["until"]), ("running", "session", T0 + 3660))
        self.assertEqual((seen["wait"]["role"], seen["wait"]["limit_seconds"]), ("worker", 21600))
        self.assertTrue(seen["store_free"])
        self.assertIn("不证明", seen["summary"]["note"])
        self.assertEqual(seen["summary"]["until"], quota._utc(T0 + 3660, "seconds"))
        # Every gate ran again: the failed worker is followed by a new commander decision, then a new worker.
        self.assertEqual([call[0] for call in backend.calls][:3], ["worker", "commander", "worker"])
        self.assertEqual(len({call[2] for call in backend.calls}), len(backend.calls))
        failed = next(entry for entry in result["dispatches"] if entry["id"] == backend.calls[0][2])
        self.assertEqual((failed["status"], failed["worker_receipt_consumed"]), ("failed", "provider_failure"))
        self.assertEqual(seen["wait"]["dispatch_id"], failed["id"])
        # The record's save changes nothing else; the next save removes it.
        index = next(i for i, state in enumerate(states) if "quota_wait" in state)
        before, after = copy.deepcopy(states[index - 1]), copy.deepcopy(states[index])
        after.pop("quota_wait")
        before.pop("store_revision"), after.pop("store_revision")
        self.assertEqual(before, after)
        self.assertNotIn("quota_wait", states[index + 1])
        self.assertEqual(sum("quota_wait" in state for state in states), 1)
        names = [event["event"] for event in self.events(engine)]
        self.assertLess(names.index("quota_wait_started"), names.index("quota_wait_finished"))
        started = self.events(engine, "quota_wait_started")[0]
        self.assertEqual((started["window"], started["wait_seconds"], started["dispatch_id"]), ("session", 3660, failed["id"]))
        self.assertEqual(self.triggers(engine), ["before_use", "quota_failure", "wake", "completed"])
        # The wake probe saw the clearing save on disk; the failure probe saw the judgment, not a record.
        views = self.calls.states()
        self.assertEqual([(view[0]["status"], view[0]["quota_wait"]) for view in views][1:3],
                         [("running", False), ("running", False)])
        self.assertEqual(views[1][0]["dispatches"][-1]["consumed"], "provider_failure")
        self.assertEqual(engine.services.quota_wait.waits, 1)

    def test_latest_blocking_window_is_the_target(self):  # T8-3
        self.windows(window("session", 100, 3600), window("weekly_all", 100, 10800))
        engine, result = self.start(self.backend("worker"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(self.clock.sleeps), 10860)
        self.assertEqual(self.events(engine, "quota_wait_started")[0]["window"], "weekly_all")

    def test_unrelated_model_weekly_window_neither_blocks_nor_targets(self):  # T8-4
        self.windows(window("session", 100, 3600), window("weekly_scoped", 100, 18000, "Sonnet"))
        engine, result = self.start(self.backend("worker"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(self.clock.sleeps), 3660)
        # Still an account observation of the failure sample.
        sample = read_json(engine.directory / "observations" / "quota" / next(
            name for _, trigger, name in self.samples(engine) if trigger == "quota_failure"))
        self.assertEqual([w["used_percent"] for w in sample["windows"]], [100, 100])

    def test_versioned_counterexamples_do_not_wait(self):  # T8-4 (review counterexamples)
        for display in ("Opus 5", "Opus 5.5 / Opus 5.6"):
            with self.subTest(display=display):
                self.clock.sleeps.clear()
                self.windows(window("session", 100, 3600), window("weekly_scoped", 100, 18000, display))
                engine, saved = self.start(self.backend("worker"), expect=HarnessError)
                self.declined(saved, "window_uncertain")
                self.assertEqual(len(saved["stop"]["data"]["uncertain_windows"]), 1)
                self.assertTrue(saved["stop"]["data"]["uncertain_windows"][0].startswith(f"weekly_scoped（{display}）: "))

    def test_matching_scoped_window_is_waited_for(self):  # T8-5
        self.windows(window("session", 40, 600), window("weekly_scoped", 100, 7200, "Opus"))
        engine, result = self.start(self.backend("worker"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(self.clock.sleeps), 7260)
        self.assertEqual(self.events(engine, "quota_wait_started")[0]["window"], "weekly_scoped（Opus）")

    def test_declines(self):  # T8-6
        full = window("session", 100, 3600)
        cases = [
            ("disabled", {"quota_wait_max_seconds": 0}, (full,), "ok"),
            ("dispatch_budget", {"max_agent_dispatches": 1}, (full,), "ok"),
            ("snapshot_unavailable", {}, (full,), "shape"),
            ("window_uncertain", {}, (full, window("weekly_scoped", 100, 3600)), "ok"),
            ("no_blocking_window", {}, (window("session", 99, 3600),), "ok"),
            ("reset_unknown", {}, (window("session", 100, None),), "ok"),
            ("reset_passed", {}, (window("session", 100, -60),), "ok"),
            ("exceeds_limit", {}, (window("session", 100, 21600 - 59),), "ok"),
        ]
        for reason, runtime, rows, mode in cases:
            with self.subTest(reason=reason):
                config = copy.deepcopy(self.config)
                config.data["runtime"].update(runtime)
                os.environ["QUOTA_STUB_CLAUDE"] = mode
                self.windows(*rows)
                engine, saved = self.start(self.backend("worker"), expect=HarnessError, config=config)
                self.declined(saved, reason)
                data = saved["stop"]["data"]
                # One failure sample: the decision's, or the stop boundary's when it declined before sampling.
                failures = [name for _, trigger, name in self.samples(engine) if trigger == "quota_failure"]
                self.assertEqual(len(failures), 1)
                self.assertEqual(data["quota_sample"], failures[0])
                self.assertNotIn("wake", self.triggers(engine))
                self.assertEqual(data["wait_limit_seconds"], config.data["runtime"]["quota_wait_max_seconds"])
                if reason == "exceeds_limit":
                    self.assertEqual((data["wait_seconds"], data["window"]), (21601, "session"))
                    self.assertEqual(data["resets_at"], quota._utc(T0 + 21600 - 59, "seconds"))
                if reason == "reset_passed":
                    self.assertNotIn("wait_seconds", data)
                if reason == "snapshot_unavailable":
                    self.assertEqual((data["quota_snapshot"], data["quota_reason"]), ("unavailable", "shape_mismatch"))
                os.environ["QUOTA_STUB_CLAUDE"] = "ok"

    def test_delay_equal_to_the_limit_waits(self):  # T8-7
        self.windows(window("session", 100, 21600 - 60))
        engine, result = self.start(self.backend("worker"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(self.clock.sleeps), 21600)

    def test_failure_text_never_supplies_a_reset(self):  # T8-8
        os.environ["QUOTA_STUB_CLAUDE"] = "shape"   # every Claude snapshot is unavailable
        engine, saved = self.start(self.backend("worker"), expect=HarnessError)
        self.declined(saved, "snapshot_unavailable")
        self.assertIn("09:00", (engine.directory / "dispatches" / saved["stop"]["source"]["dispatch_id"]
                                / "process.stdout").read_text())
        self.assertNotIn("resets_at", saved["stop"]["data"])


class InterruptionTests(WaitCase):
    def judged(self, saved):
        """The failed call's judgment and counts, which an interrupt never changes."""
        entry = next(entry for entry in saved["dispatches"] if entry.get("role") == "worker")
        return (entry["status"], entry["worker_receipt_consumed"], entry["attempt_exemption"]["code"],
                saved["attempts"], saved["agent_dispatches"])

    def test_interrupt_during_wait_is_recorded_interrupted(self):  # T8-9 (SIGINT)
        def interrupt(clock, seconds):
            if len(clock.sleeps) == 3:
                raise KeyboardInterrupt()
        self.clock.on_sleep = interrupt
        self.windows(window("session", 100, 3600))
        engine, saved = self.start(self.backend("worker"), expect=KeyboardInterrupt)
        self.assertEqual((saved["status"], saved["stop"]["kind"], stops.exit_code(self.error)),
                         ("interrupted", "interrupted", 130))
        self.assertNotIn("quota_wait", saved)
        self.assertEqual(self.judged(saved), ("failed", "provider_failure", "usage_limit", {"greet": 1}, 1))
        self.assertEqual(self.events(engine, "quota_wait_finished"), [])
        self.clock.on_sleep, self.clock.sleeps = None, []
        result = self.engine(self.backend()).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.clock.sleeps, [])

    def test_sigterm_during_wait_through_the_cli(self):  # T8-9 (SIGTERM, cli.main's handler)
        def terminate(clock, seconds):
            if len(clock.sleeps) == 2:
                os.kill(os.getpid(), signal.SIGTERM)   # only this test process
        self.clock.on_sleep = terminate
        self.windows(window("session", 100, 3600))
        code, errors = run_cli(self, self.backend("worker"), ["run", "Implement greet", "--plan",
                                                              str(ROOT / "examples" / "plan.json")])
        self.assertEqual(code, 130, errors)
        self.assertEqual(cli_notice(errors)["kind"], "interrupted")
        (run_id,) = [path.name for path in (self.repo / ".harness" / "runs").iterdir()]
        saved = read_json(self.repo / ".harness" / "runs" / run_id / "state.json")
        self.assertEqual((saved["status"], saved["stop"]["kind"]), ("interrupted", "interrupted"))
        self.assertNotIn("quota_wait", saved)
        self.assertEqual(self.judged(saved), ("failed", "provider_failure", "usage_limit", {"greet": 1}, 1))
        self.clock.on_sleep, self.clock.sleeps = None, []
        self.assertEqual(self.engine(self.backend()).resume(run_id)["status"], "completed")
        self.assertEqual(self.clock.sleeps, [])

    def killed(self, rows=(window("session", 100, 3600),), **runtime):
        """A process death right after the wait record's save: the ledger keeps running + quota_wait."""
        config = copy.deepcopy(self.config)
        config.data["runtime"].update(runtime)
        self.windows(*rows)
        engine = self.engine(self.backend("worker"), config=config)
        crash, state = crash_after_save(engine, lambda run: "quota_wait" in run)
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        disk = read_json(engine.directory / "state.json")
        self.assertEqual(disk["status"], "running")
        self.assertIn("quota_wait", disk)
        self.assertEqual(self.clock.sleeps, [])
        return engine, disk, config

    def test_killed_during_wait_is_not_resumed_as_a_wait(self):  # T8-10
        engine, disk, _ = self.killed()
        shown = status_summary(disk)["quota_wait"]
        self.assertEqual((shown["window"], shown["source_dispatch_id"]), ("session", disk["quota_wait"]["dispatch_id"]))
        self.assertIn("不证明", shown["note"])
        resumed = self.engine(self.backend())
        recorder, states = commit_recorder()
        with recorder:
            result = resumed.resume(disk["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.clock.sleeps, [])
        self.assertNotIn("quota_wait", states[0])
        self.assertEqual(states[0]["status"], "running")
        self.assertFalse(any("quota_wait" in state for state in states))

    def test_abandon_after_kill_clears_the_record(self):  # T8-11
        engine, disk, _ = self.killed()
        result = self.engine(self.backend()).abandon(disk["id"])
        self.assertEqual(result["status"], "abandoned")
        self.assertNotIn("quota_wait", read_json(engine.directory / "state.json"))

    def test_stop_before_running_after_kill_drops_the_record(self):  # T8-11
        engine, disk, _ = self.killed()
        refusal = HarnessError("synthetic admission stop", stop=stops.make("runtime_failure", "invariant"))
        with patch("harness.scheduler.Scheduler.admission", side_effect=refusal), self.assertRaises(HarnessError):
            self.engine(self.backend()).resume(disk["id"])
        stopped = read_json(engine.directory / "state.json")
        self.assertEqual((stopped["status"], stopped["stop"]["kind"]), ("blocked", "runtime_failure"))
        self.assertNotIn("quota_wait", stopped)
        self.assertEqual(self.engine(self.backend()).resume(disk["id"])["status"], "completed")
        self.assertEqual(self.clock.sleeps, [])

    def test_record_with_a_stop_status_or_a_bad_shape_is_refused_at_load(self):  # T8-12
        engine, disk, _ = self.killed()
        path = engine.directory / "state.json"
        record = disk["quota_wait"]
        changes = {
            "stop status": {"status": "blocked", "stop": stops.make("runtime_failure", "invariant")},
            "extra key": {"quota_wait": {**record, "extra": 1}},
            "missing key": {"quota_wait": {key: value for key, value in record.items() if key != "sample"}},
            "time out of range": {"quota_wait": {**record, "until": quota.EPOCH_MAX + 1}},
            "time not a number": {"quota_wait": {**record, "resets_at": "soon"}},
            "role": {"quota_wait": {**record, "role": "operator"}},
        }
        for name, change in changes.items():
            with self.subTest(case=name):
                write_json(path, {**disk, **change})
                raw = path.read_bytes()
                for operation in ("resume", "abandon"):
                    with self.assertRaises(HarnessError) as caught:
                        getattr(self.engine(self.backend()), operation)(disk["id"])
                    value = stops.of(caught.exception)
                    self.assertEqual((value["kind"], value["reason"], stops.exit_code(caught.exception)),
                                     ("evidence", "load_integrity", 1))
                    self.assertEqual(path.read_bytes(), raw)
        write_json(path, disk)   # the leftover record itself loads, and the next execution removes it
        self.assertEqual(self.engine(self.backend()).resume(disk["id"])["status"], "completed")

    def test_extreme_but_valid_record_survives_a_kill(self):  # T8-30
        # The longest label the sampler can produce (a 64-character display name) and the latest valid wake-up.
        name = "Opus" + " " * 60
        cases = [("label", (window("weekly_scoped", 100, 600, name),), {}),
                 ("until", (window("session", 100, resets_at="9999-12-31T23:58:59+00:00"),),
                  {"quota_wait_max_seconds": 10 ** 12})]
        for case, rows, runtime in cases:
            with self.subTest(case=case):
                engine, disk, config = self.killed(rows, **runtime)
                record = disk["quota_wait"]
                if case == "label":
                    self.assertEqual(record["window"], f"weekly_scoped（{name}）")
                else:
                    self.assertEqual(record["until"], quota.EPOCH_MAX)
                self.assertTrue(valid_record(record))
                self.assertEqual(self.engine(self.backend(), config=config).resume(disk["id"])["status"], "completed")

    def test_save_failures_report_the_new_error(self):  # T8-27
        for name, predicate in (("record save", lambda run: "quota_wait" in run), ("clearing save", None)):
            with self.subTest(case=name):
                self.clock.sleeps.clear()
                self.windows(window("session", 100, 600))
                engine = self.engine(self.backend("worker"))
                commit, state = RunStore.commit, {"armed": False, "failed": False}

                def failing(store):
                    run = store.run
                    if predicate is not None and predicate(run) and not state["failed"]:
                        state["failed"] = True
                        raise OSError(28, "synthetic disk full")
                    if predicate is None and state["armed"] and not state["failed"]:
                        state["failed"] = True
                        raise OSError(28, "synthetic disk full")
                    return commit(store)

                def arm(clock, seconds):
                    state["armed"] = True
                self.clock.on_sleep = arm if predicate is None else None
                with patch.object(RunStore, "commit", failing), self.assertRaises(OSError) as caught:
                    engine.start("Implement greet", plan())
                saved = read_json(engine.directory / "state.json")
                self.assertEqual((saved["status"], saved["stop"]["kind"], saved["stop"]["reason"]),
                                 ("blocked", "runtime_failure", "io"))
                self.assertEqual(stops.exit_code(caught.exception), 1)
                self.assertNotIn("quota_wait", saved)
                worker = next(entry for entry in saved["dispatches"] if entry["role"] == "worker")
                self.assertEqual(worker["worker_receipt_consumed"], "provider_failure")
                self.assertEqual(len(self.clock.sleeps) > 0, predicate is None)
        self.clock.on_sleep = None
        self.clock.sleeps.clear()
        # A wake sample that cannot be written is only an observation gap.
        real = quota.publish_new_json

        def publish(path, value):
            if value["trigger"] == "wake":
                raise OSError("synthetic")
            return real(path, value)
        with patch("harness.quota.publish_new_json", side_effect=publish):
            engine, result = self.start(self.backend("worker"))
        self.assertEqual(result["status"], "completed")
        wake = [event for event in self.events(engine, "quota_sampled") if event["trigger"] == "wake"]
        self.assertEqual([(event["sample"], event["write_error"]) for event in wake], [(None, "OSError")])


class GateTests(WaitCase):
    def test_gates_run_again_after_waking(self):  # T8-13
        def plan_file(engine):
            path = next((engine.directory / "plans").glob("revision-*.json"))
            path.write_text(path.read_text() + " ")

        def outside(engine):
            (Path(engine.run["worktree"]) / "outside.txt").write_text("not in the task's allowed paths\n")
        cases = [("published plan changed (wake-up checks)", plan_file, ("evidence", "runtime_integrity")),
                 ("candidate outside the task scope (scheduling gate)", outside,
                  ("scope_violation", "candidate_outside_scope"))]
        for name, change, expected in cases:
            with self.subTest(case=name):
                self.clock.sleeps.clear()
                self.windows(window("session", 100, 600))
                backend = self.backend("worker")
                engine = self.engine(backend)
                self.clock.on_sleep = lambda clock, seconds: change(engine) if len(clock.sleeps) == 1 else None
                with self.assertRaises(HarnessError) as caught:
                    engine.start("Implement greet", plan())
                saved = read_json(engine.directory / "state.json")
                self.assertEqual((saved["status"], saved["stop"]["kind"], saved["stop"]["reason"]), ("blocked", *expected))
                self.assertEqual(stops.exit_code(caught.exception), 1)
                self.assertEqual([call[0] for call in backend.calls], ["worker"])   # no model call after waking
                self.assertEqual(len(self.events(engine, "quota_wait_finished")), 1)

    def test_head_changed_during_wait_stops_before_any_model_call(self):  # T8-31
        from harness import git

        def commit(clock, seconds):
            if len(clock.sleeps) == 1:
                git.git(Path(engine.run["worktree"]), "-c", "user.name=Operator", "-c", "user.email=op@localhost",
                        "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "outside change")
        self.clock.on_sleep = commit
        self.windows(window("session", 100, 600))
        backend = self.backend("worker")
        engine = self.engine(backend)
        with self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual((saved["status"], saved["stop"]["kind"], saved["stop"]["reason"]),
                         ("blocked", "workspace_mismatch", "head_changed"))
        self.assertEqual(stops.exit_code(caught.exception), 1)
        self.assertEqual([call[0] for call in backend.calls], ["worker"])

    def test_shared_skill_changed_during_wait_stops_before_any_model_call(self):  # T8-32
        import shutil
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        shutil.copytree(ROOT / "shared", Path(folder.name) / "shared")
        config = copy.deepcopy(self.config)
        for key, value in (("rules", "shared/RULES.md"), ("skills", "shared/skills"), ("mcp", "shared/mcp.json")):
            config.data["shared"][key] = str(Path(folder.name) / value)
        skill = next((Path(folder.name) / "shared" / "skills").glob("*/SKILL.md"))
        original = skill.read_bytes()
        self.clock.on_sleep = lambda clock, seconds: (skill.write_bytes(original + b"\nSYNTHETIC EDIT\n")
                                                      if len(clock.sleeps) == 1 else None)
        self.windows(window("session", 100, 600))
        backend = self.backend("worker")
        engine = self.engine(backend, config=config)
        with self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual((saved["status"], saved["stop"]["kind"], saved["stop"]["reason"]),
                         ("blocked", "config_mismatch", "shared_assets"))
        self.assertEqual(stops.exit_code(caught.exception), 2)
        self.assertEqual([call[0] for call in backend.calls], ["worker"])
        # The row's exit: restore the shared assets, then the listed resume continues (a new commander decision).
        skill.write_bytes(original)
        self.clock.on_sleep = None
        self.config = config
        walk_exit(self, engine.directory, ("config_mismatch", "shared_assets"), backend=self.backend(),
                  expect_status="completed")

    def test_repeated_failures_are_bounded_by_the_dispatch_budget(self):  # T8-14
        self.config.data["runtime"]["max_agent_dispatches"] = 5
        self.windows(window("session", 100, 3600))   # each failure's snapshot: an hour after its own now
        backend = self.backend("worker", "worker", "worker")
        engine, saved = self.start(backend, expect=HarnessError)
        self.assertEqual(sum(self.clock.sleeps), 2 * 3660)
        self.assertEqual((saved["agent_dispatches"], saved["attempts"]), (5, {"greet": 3}))
        from harness.budgets import work_attempts
        self.assertEqual(work_attempts(saved), {"greet": 0})
        self.assertEqual((saved["stop"]["kind"], saved["stop"]["reason"]), ("provider_quota", "usage_limit"))
        self.assertEqual((saved["stop"]["data"]["wait_reason"], saved["stop"]["data"]["waits"]), ("dispatch_budget", 2))
        self.assertEqual([call[0] for call in backend.calls], ["worker", "commander", "worker", "commander", "worker"])
        self.assertEqual(self.triggers(engine).count("quota_failure"), 3)
        self.assertEqual(self.triggers(engine).count("wake"), 2)
        # A plain resume without a higher budget stops before any model call.
        later = self.backend()
        with self.assertRaises(HarnessError) as caught:
            self.engine(later).resume(saved["id"])
        self.assertEqual((stops.of(caught.exception)["kind"], stops.of(caught.exception)["reason"]),
                         ("budget", "agent_dispatches"))
        self.assertEqual(later.calls, [])

    def test_suspend_ends_the_wait_at_the_wall_clock_target(self):  # T8-15
        def suspend(clock, seconds):
            if len(clock.sleeps) == 1:
                clock.wall += 5 * 3600   # the monotonic clock stood still while the system slept
        self.clock.on_sleep = suspend
        self.windows(window("session", 100, 3 * 3600))
        engine, result = self.start(self.backend("worker"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.clock.sleeps), 1)

    def test_clock_rollback_cannot_extend_the_wait(self):  # T8-15
        def rollback(clock, seconds):
            if len(clock.sleeps) == 1:
                clock.wall -= 2 * 3600
        self.clock.on_sleep = rollback
        self.windows(window("session", 100, 3600))
        engine, result = self.start(self.backend("worker"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(self.clock.sleeps), 3660)

    def test_clock_jump_forward_wakes_early(self):  # T8-15
        def jump(clock, seconds):
            if len(clock.sleeps) == 1:
                clock.wall += 3000   # a clock correction: the target comes 3000 s early
        self.clock.on_sleep = jump
        self.windows(window("session", 100, 3600))
        backend = self.backend("worker", "worker")   # the quota has not come back yet: it fails once more
        engine, result = self.start(backend)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.events(engine, "quota_wait_finished")), 2)
        self.assertEqual([event["wait_seconds"] for event in self.events(engine, "quota_wait_started")], [3660, 3660])
        # The first wait ended at its wall-clock target, 3000 s early by the monotonic clock.
        self.assertEqual(sum(self.clock.sleeps), (3660 - 3000) + 3660)
        self.assertEqual([call[0] for call in backend.calls][:5], ["worker", "commander", "worker", "commander", "worker"])

    def test_load_check_failure_declines(self):  # T8-16
        real = quota.QuotaSampler.failure_sample

        def sample_then_damage(sampler, error):
            record = real(sampler, error)
            entry = next(entry for entry in sampler.context.run["dispatches"] if entry.get("worker_receipt"))
            Path(entry["worker_receipt"]["packet_artifact"]).write_text("{}\n")   # a load-checked original
            return record
        reasons, annotate = [], QuotaWait._annotate

        def record_reason(waiter, error, decision):
            reasons.append(decision["reason"])
            return annotate(waiter, error, decision)
        self.windows(window("session", 100, 600))
        with patch.object(quota.QuotaSampler, "failure_sample", sample_then_damage), \
                patch.object(QuotaWait, "_annotate", record_reason):
            engine, saved = self.start(self.backend("worker"), expect=HarnessError)
        self.assertEqual(reasons, ["load_checks_failed"])
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual((saved["stop"]["kind"], saved["stop"]["reason"]), ("evidence", "load_integrity"))
        self.assertEqual(saved["stop"]["data"]["superseded"], {"kind": "provider_quota", "reason": "usage_limit"})
        self.assertEqual(stops.exit_code(self.error), 1)
        self.assertNotIn("quota_wait", saved)
        self.assertEqual(self.events(engine, "quota_wait_started"), [])

    def test_unsaved_ledger_change_declines(self):  # T8-16
        self.windows(window("session", 100, 600))
        engine = self.engine(self.backend("worker"))
        original = engine.services.quota_wait.after_failure

        def staged(error):
            engine.run["synthetic_unsaved"] = True   # a staged change nobody committed
            return original(error)
        with patch.object(engine.services.quota_wait, "after_failure", side_effect=staged), \
                self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        self.assertEqual(self.clock.sleeps, [])
        value = stops.of(caught.exception)
        self.assertEqual((value["kind"], value["data"]["wait_reason"], value["data"]["waits"]),
                         ("provider_quota", "unsaved_state", 0))
        self.assertNotIn("wait_limit_seconds", value["data"])
        self.assertEqual(self.triggers(engine).count("quota_failure"), 1)   # the stop boundary's own sample


class RoleTests(WaitCase):
    def test_claude_commander_failure_waits_and_a_fresh_decision_follows(self):  # T8-17
        self.windows(window("session", 100, 600))
        backend = self.backend("worker", "commander")
        engine, result = self.start(backend, commander="fable")
        self.assertEqual(result["status"], "completed")
        self.assertEqual([call[0] for call in backend.calls][:4], ["worker", "commander", "commander", "worker"])
        first, second = (next(entry for entry in result["dispatches"] if entry["id"] == call[2])
                         for call in backend.calls[1:3])
        self.assertEqual(first["status"], "failed")
        self.assertNotIn("retry_of", second)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(engine.services.quota_wait.waits, 2)
        roles = [event["role"] for event in self.events(engine, "quota_wait_started")]
        self.assertEqual(roles, ["worker", "commander"])

    def test_claude_reviewer_failures_wait_and_a_fresh_review_follows(self):  # T8-18
        self.config.data["roles"]["reviewer"] = {"cli": "claude", "model": "claude-sonnet-9-1", "effort": "high"}
        self.windows(window("session", 100, 600))
        backend = self.backend("reviewer", "reviewer")
        engine, result = self.start(backend)
        self.assertEqual(result["status"], "completed")
        reviews = [call for call in backend.calls if call[0] == "reviewer"]
        self.assertEqual([call[1] for call in reviews], ["review", "review", "review", "final_review"])
        self.assertEqual(len({call[2] for call in reviews}), 4)
        self.assertEqual(engine.services.quota_wait.waits, 2)
        # A whole-run review failure waits too: its record was saved as reviewing before the call.
        self.clock.sleeps.clear()
        backend = QuotaFailingBackend(self.config, self.repo, [])
        backend.script = []
        real = backend.invoke

        def final_fails_once(**kwargs):
            if kwargs["kind"] == "final_review" and not getattr(backend, "failed", False):
                backend.failed = True
                backend.script = ["reviewer"]
            return real(**kwargs)
        backend.invoke = final_fails_once
        engine, result = self.start(backend)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([call[1] for call in backend.calls if call[0] == "reviewer"], ["review", "final_review", "final_review"])
        self.assertEqual(engine.services.quota_wait.waits, 1)

    def test_planning_phase_failure_does_not_wait(self):  # T8-19
        self.windows(window("session", 100, 600))
        engine, saved = self.start(self.backend("commander"), commander="fable", tasks=False, expect=HarnessError)
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual((saved["stop"]["kind"], saved["stop"]["data"]["wait_reason"]), ("provider_quota", "not_running"))
        self.assertNotIn("wait_limit_seconds", saved["stop"]["data"])
        self.assertEqual(stops.exit_code(self.error), 3)
        self.assertEqual(self.triggers(engine).count("quota_failure"), 1)   # the stop boundary's sample

    def test_codex_failure_never_waits(self):  # T8-20
        self.windows(window("session", 100, 600))
        backend = self.backend(("reviewer", "codex"))
        engine, saved = self.start(backend, expect=HarnessError)
        self.assertEqual((saved["stop"]["kind"], saved["stop"]["reason"]), ("provider_failure", "unknown"))
        self.assertFalse(set(WAIT_FIELDS) & set(saved["stop"]["data"]))
        self.assertEqual(self.clock.sleeps, [])
        self.assertNotIn("quota_failure", self.triggers(engine, "claude") + self.triggers(engine, "codex"))
        # A typed Codex usage-limit stop (none is produced today) is still never waited for.

        class CodexQuota(QuotaFailingBackend):
            def invoke(self, **kwargs):
                if kwargs["role"] == "reviewer":
                    self.calls.append((kwargs["role"], kwargs["kind"], kwargs["job"].name))
                    raise HarnessError("SYNTHETIC codex usage limit", stop=stops.make(
                        "provider_quota", "usage_limit", data={"role": "reviewer", "provider": "codex"},
                        source={"dispatch_id": kwargs["job"].name}))
                return super().invoke(**kwargs)
        engine, saved = self.start(CodexQuota(self.config, self.repo), expect=HarnessError)
        self.declined(saved, "provider")
        self.assertNotIn("wait_limit_seconds", saved["stop"]["data"])
        self.assertEqual(self.triggers(engine, "claude").count("quota_failure"), 0)

    def test_redo_that_hits_the_quota_is_followed_by_a_fresh_request(self):  # T8-34
        self.windows(window("session", 100, 600))
        backend = self.backend("worker", ("commander", "invalid"), "commander")
        engine, result = self.start(backend, commander="fable")
        self.assertEqual(result["status"], "completed")
        commanders = [entry for entry in result["dispatches"] if entry["role"] == "commander"]
        self.assertEqual([entry["status"] for entry in commanders[:2]], ["invalid", "failed"])
        self.assertEqual(commanders[1].get("retry_of"), commanders[0]["id"])
        self.assertNotIn("retry_of", commanders[2])

        def packet(entry):
            prompt = (engine.directory / "dispatches" / entry["id"] / "prompt.txt").read_text()
            return json.JSONDecoder().raw_decode(prompt)[0]["packet"]
        self.assertIn("previous_invalid", packet(commanders[1]))      # the automatic redo carried the hint
        self.assertNotIn("previous_invalid", packet(commanders[2]))   # after waking: a new request
        self.assertEqual(engine.services.quota_wait.waits, 2)


class ConfigTests(WaitCase):
    def test_resume_may_raise_lower_or_disable_the_wait(self):  # T8-21
        previous = copy.deepcopy(self.config.data)
        for value in (43200, 600, 0):
            current = copy.deepcopy(previous)
            current["runtime"]["quota_wait_max_seconds"] = value
            self.assertEqual(Config(self.config.path, current).resume_changes(previous),
                             {"quota_wait_max_seconds": {"old": 21600, "new": value}})
        for bad in (-1, True, 1.5, "60"):
            current = copy.deepcopy(previous)
            current["runtime"]["quota_wait_max_seconds"] = bad
            with self.subTest(value=bad), self.assertRaises(HarnessError):
                Config(self.config.path, current).resume_changes(previous)
        # Lowering a budget is still refused: only the wait policy may go down.
        current = copy.deepcopy(previous)
        current["runtime"]["max_agent_dispatches"] -= 1
        with self.assertRaises(HarnessError):
            Config(self.config.path, current).resume_changes(previous)

    def test_resume_with_disabled_wait_stops_at_the_next_failure(self):  # T8-21
        config = copy.deepcopy(self.config)
        config.data["runtime"]["quota_wait_max_seconds"] = 3600
        self.windows(window("session", 100, 7200))
        engine, saved = self.start(self.backend("worker"), expect=HarnessError, config=config)
        self.declined(saved, "exceeds_limit")
        self.assertEqual(saved["stop"]["data"]["wait_limit_seconds"], 3600)
        config.data["runtime"]["quota_wait_max_seconds"] = 0
        self.windows(window("session", 100, 60))
        resumed = self.engine(self.backend("worker"), config=config)
        with self.assertRaises(HarnessError) as caught:
            resumed.resume(saved["id"])
        self.error = caught.exception
        saved = read_json(resumed.directory / "state.json")
        self.assertEqual(saved["config_snapshot"]["runtime"]["quota_wait_max_seconds"], 0)
        self.declined(saved, "disabled")
        changed = self.events(resumed, "resume_config_changed")
        self.assertEqual([event["changes"] for event in changed], [{"quota_wait_max_seconds": {"old": 3600, "new": 0}}])

    def test_config_is_not_reloaded_while_running(self):  # T8-21
        import shutil
        import tempfile
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "harness.toml"
        text = (ROOT / "harness.toml").read_text(encoding="utf-8").replace(
            'rules = "shared/RULES.md"', f'rules = "{ROOT}/shared/RULES.md"').replace(
            'skills = "shared/skills"', f'skills = "{ROOT}/shared/skills"').replace(
            'mcp = "shared/mcp.json"', f'mcp = "{ROOT}/shared/mcp.json"')
        path.write_text(text, encoding="utf-8")
        config = Config.load(path)
        config.data["verification"]["commands"] = self.config.data["verification"]["commands"]

        def rewrite(clock, seconds):
            if len(clock.sleeps) == 1:
                path.write_text(text.replace("quota_wait_max_seconds = 21600", "quota_wait_max_seconds = 0"))
        self.clock.on_sleep = rewrite
        self.windows(window("session", 100, 600))
        engine, result = self.start(self.backend("worker", "worker"), config=config)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(engine.services.quota_wait.waits, 2)   # the second decision used the loaded value
        self.assertEqual(Config.load(path).data["runtime"]["quota_wait_max_seconds"], 0)

    def test_inherited_retry_watchdog_is_removed_from_cli_environments(self):  # T8-22
        captured = {}

        def fake_run_process(argv, cwd, base, **kwargs):
            captured.update(kwargs["env"])
            raise HarnessError("stop before any process", stop=stops.make("runtime_failure", "invariant"))
        backend = CLIBackend(self.config, self.repo / ".harness")
        job = self.repo / ".harness" / "job"
        job.mkdir(parents=True)
        with patch.dict(os.environ, {"CLAUDE_CODE_RETRY_WATCHDOG": "1"}):
            with patch("harness.adapters.run_process", side_effect=fake_run_process), \
                    patch.object(CLIBackend, "command", return_value=["claude"]), self.assertRaises(HarnessError):
                backend.invoke(role="commander", model=self.config.role("commander", "fable"), cwd=self.repo, job=job,
                               schema={}, prompt="x", run_path=job / "state.json", on_start=lambda pgid: None)
            record = backend.quota_snapshot("claude")
        self.assertNotIn("CLAUDE_CODE_RETRY_WATCHDOG", captured)
        self.assertEqual(captured.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")
        self.assertTrue(record["available"])
        probe = self.calls.probes("claude")[-1]
        self.assertEqual((probe["retry_watchdog"], probe["auto_memory"]), (None, "1"))


class ObservationTests(WaitCase):
    def recorded_states(self):
        self.windows(window("session", 100, 600))
        engine = self.engine(self.backend("worker"))
        recorder, states = commit_recorder()
        with recorder:
            engine.start("Implement greet", plan())
        index = next(i for i, state in enumerate(states) if "quota_wait" in state)
        return engine, states, index

    def test_watch_snapshot_shows_the_wait_and_keeps_watching(self):  # T8-23
        from harness.watch_cli import watch_run
        engine, states, index = self.recorded_states()
        sequence = [states[index - 1], states[index], states[index + 1], states[-1]]
        path = engine.directory / "state.json"

        class Watcher:
            backend = "fixture"

            def __init__(self, *args):
                self.calls = 0
                write_json(path, sequence[0])

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def wait(self, seconds):
                self.calls += 1
                write_json(path, sequence[min(self.calls, len(sequence) - 1)])
                return True
        import io
        output = io.StringIO()
        with patch("harness.watch_cli.RunWatcher", Watcher):
            self.assertEqual(watch_run(self.repo / ".harness", engine.run["id"], stdout=output), 0)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row["status"] for row in rows], ["running", "running", "running", "completed"])
        waits = [row["quota_wait"] for row in rows]
        self.assertEqual((waits[0], waits[2], waits[3]), (None, None, None))
        self.assertEqual(set(waits[1]), {"window", "until", "recorded_at"})
        self.assertEqual((waits[1]["window"], waits[1]["until"]), ("session", quota._utc(T0 + 660, "seconds")))

    def test_dashboard_shows_the_wait(self):  # T8-24
        import test_dashboard_presentation as presentation
        engine, states, index = self.recorded_states()
        shown = status_summary(states[index])["quota_wait"]
        data = {"detail": presentation.detail("run_wait", "Ship it", summary={"quota_wait": shown}, events=[
                    presentation.event(1990.0, "resume_config_changed"), presentation.event(1995.0, "quota_wait_started")]),
                "calm": presentation.detail("run_calm", "Ship it")}
        presentation.DashboardPresentationTests.run_browser(self, data, r'''
  navigate("#/runs/run_wait");
  await respond("/api/runs/run_wait", D.detail);
  const attention = one(attrIs("data-attention", "true"));
  const shown = visibleText(attention);
  assert.ok(shown.includes("等待额度恢复：session"), "the wait is an attention item: " + shown);
  assert.ok(shown.includes(D.detail.summary.quota_wait.until), "it says until when");
  assert.ok(shown.includes("不证明 Harness 进程仍在等待"), "a record, not a liveness proof");
  assert.ok(shown.includes("Ctrl-C"), "how to cancel it");
  assert.ok(byId.main.textContent.includes("开始等待额度"), "the started event has its label");
  assert.ok(byId.main.textContent.includes("恢复时调整配置"), "the renamed event has its label");
  navigate("#/runs/run_calm");
  await respond("/api/runs/run_calm", D.calm);
  assert.ok(!byId.main.textContent.includes("等待额度恢复"), "no record, no item");
''')

    def test_busy_while_waiting_points_to_the_record(self):  # T8-35
        from test_harness import refused_by_cli, walk_notice_exit
        seen = {}

        def during(clock, seconds):
            if seen:
                return
            path = engine.directory / "state.json"
            raw = path.read_bytes()
            run_id = engine.run["id"]
            for operation in ("resume", "abandon"):
                with self.assertRaises(HarnessError) as caught:
                    getattr(self.engine(self.backend()), operation)(run_id)
                seen[operation] = stops.project(stops.of(caught.exception), run_id)
            notice = refused_by_cli(self, self.backend(), ["resume", run_id], ("busy", "run_lock"), 3)
            seen["cli"] = notice
            # The listed exit reads the record; it does not prove the holder is alive.
            walk_notice_exit(self, notice, ("busy", "run_lock"), backend=self.backend(), directory=engine.directory,
                             expect_status="running")
            seen["cleanup"] = run_cli(self, self.backend(), ["cleanup-run", run_id])[0]
            seen["unchanged"] = path.read_bytes() == raw
        self.clock.on_sleep = during
        self.windows(window("session", 100, 600))
        engine = self.engine(self.backend("worker"))
        self.assertEqual(engine.start("Implement greet", plan())["status"], "completed")
        record = next(event for event in self.events(engine, "quota_wait_started"))
        for name in ("resume", "abandon", "cli"):
            value = seen[name]
            self.assertEqual((value["kind"], value["reason"], value["exit_code"]), ("busy", "run_lock", 3), name)
            self.assertEqual(value["exits"], [f"status {engine.run['id']}"], name)
            self.assertEqual(value["data"]["quota_wait_until"], record["until"], name)
            self.assertEqual(value["data"]["quota_wait_note"], quota_wait.BUSY_NOTE, name)
            self.assertIn("quota_wait_recorded_at", value["data"])
        self.assertEqual((seen["cleanup"], seen["unchanged"]), (1, True))


class ContractTests(WaitCase):
    def test_wait_fields_are_registered(self):  # T8-28
        base = {"role": "worker", "provider": "claude"}
        label = "weekly_scoped（" + "x" * 1900 + "）"
        value = stops.make("provider_quota", "usage_limit", data={
            **base, "wait_reason": "exceeds_limit", "waits": 2, "window": label,
            "resets_at": "2031-01-01T00:00:00+00:00", "wait_seconds": 21601, "wait_limit_seconds": 21600,
            "uncertain_windows": ["session: unknown_kind"]})
        stops.validate(value)
        self.assertEqual(value["data"]["window"], label)   # kept whole within the text limit
        self.assertEqual(stops.project(value, "run_x")["exits"], ["resume run_x"])
        dropped = stops.make("provider_quota", "usage_limit",
                             data={**base, "wait_reason": "tired", "waits": -1, "wait_seconds": 1.5})
        self.assertEqual(dropped["data"]["dropped_fields"], ["wait_reason", "wait_seconds", "waits"])
        stops.validate(dropped)
        listed = stops.make("provider_quota", "usage_limit",
                            data={**base, "uncertain_windows": [f"weekly_scoped（M{i}）: " + "y" * 400 for i in range(40)]})
        stops.validate(listed)
        self.assertGreater(listed["data"]["uncertain_windows_omitted"], 0)
        self.assertEqual((listed["kind"], stops.project(listed, "run_x")["exits"]), ("provider_quota", ["resume run_x"]))
        for name in ("wait_reason", "waits", "window"):
            with self.subTest(field=name), self.assertRaises(KeyError):
                stops.make("provider_quota", "rate_limit", data={**base, name: "x"})
        plain, pointed = stops.make("busy", "run_lock"), quota_wait.busy_stop({"status": "blocked"})
        self.assertEqual((plain, stops.project(plain)["exits"]), (pointed, []))
        record = {"dispatch_id": "d-1", "role": "worker", "window": "session", "resets_at": T0, "until": T0 + 60,
                  "recorded_at": T0, "limit_seconds": 21600, "sample": None}
        pointed = quota_wait.busy_stop({"status": "running", "quota_wait": record})
        stops.validate(pointed)
        self.assertEqual(stops.project(pointed, "run_x")["exits"], ["status run_x"])
        self.assertEqual(quota_wait.busy_stop({"status": "running", "quota_wait": {**record, "role": "x"}}), plain)

    def test_record_outside_the_load_contract_is_not_published(self):  # T8-29
        cases = [
            # The latest reset the sampler accepts, a limit large enough: the wake-up time leaves the record's range.
            ("until", (window("session", 100, resets_at="9999-12-31T23:59:59+00:00"),),
             {"quota_wait_max_seconds": 10 ** 12}, "record_invalid"),
            # A display name too long for a record never reaches one: the sampler refuses the whole snapshot.
            ("label", (window("weekly_scoped", 100, 600, "Opus" + " " * 160),), {}, "snapshot_unavailable"),
        ]
        for name, rows, runtime, reason in cases:
            with self.subTest(case=name):
                config = copy.deepcopy(self.config)
                config.data["runtime"].update(runtime)
                self.windows(*rows)
                recorder, states = commit_recorder()
                with recorder:
                    engine, saved = self.start(self.backend("worker"), expect=HarnessError, config=config)
                self.declined(saved, reason)
                self.assertFalse(any("quota_wait" in state for state in states))
                if reason == "record_invalid":
                    data = saved["stop"]["data"]
                    self.assertEqual((data["window"], data["resets_at"]), ("session", "9999-12-31T23:59:59+00:00"))
                    self.assertEqual(data["wait_seconds"], math.ceil(quota.EPOCH_MAX + 60 - T0))
                self.assertEqual(self.engine(self.backend(), config=config).abandon(saved["id"])["status"], "abandoned")
        # Whatever the window choice returns, a record outside the contract is refused, never shortened into one.
        label = "weekly_scoped（" + "w" * 160 + "）"
        decision = {"wait": True, "window": label, "resets_at": T0 + 600, "until": T0 + 660, "delay": 660}
        self.windows(window("session", 100, 600))
        recorder, states = commit_recorder()
        with patch("harness.quota_wait.choose", return_value=decision), recorder:
            engine, saved = self.start(self.backend("worker"), expect=HarnessError)
        self.declined(saved, "record_invalid")
        self.assertEqual(saved["stop"]["data"]["window"], label)
        self.assertFalse(any("quota_wait" in state for state in states))

    def test_quota_wait_literal_guard(self):  # T8-26 (GQ)
        import ast
        allowed = {("quota_wait.py", "validate_ledger"), ("quota_wait.py", "clear"), ("quota_wait.py", "project"),
                   ("quota_wait.py", "_wait"), ("cli.py", "status_summary"), ("watch_cli.py", "watch_run")}
        found = set()
        for path in sorted((ROOT / "harness").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(function):
                    if isinstance(node, ast.Constant) and node.value == "quota_wait":
                        found.add((path.name, function.name))
        self.assertEqual(found, allowed)
        # Outside the module only the projection keys use it; inside, only these four touch the record.
        source = (ROOT / "harness" / "quota_wait.py").read_text(encoding="utf-8")
        writes = re.findall(r'run\["quota_wait"\]\s*=', source)
        self.assertEqual(len(writes), 1)

    def test_not_waiting_matches_the_settled_result_without_the_waiter(self):  # T8-25
        """Every way of not waiting ends as the waiter-less code path would, through the real CLI."""
        import test_quota_sampling
        commit = RunStore.commit

        def damaging(store):   # a load-checked original damaged right after the judgment's save
            result = commit(store)
            entry = next((entry for entry in store.run["dispatches"]
                          if entry.get("worker_receipt_consumed") == "provider_failure"), None)
            if entry is not None and not getattr(store, "damaged", False):
                store.damaged = True
                Path(entry["worker_receipt"]["packet_artifact"]).write_text("{}\n")
            return result
        scenarios = [
            ("exceeds_limit", {}, (window("session", 100, 21600 + 600),), False),
            ("window_uncertain", {}, (window("session", 100, 600), window("weekly_scoped", 100, 600, "Opus 5")), False),
            ("disabled", {"quota_wait_max_seconds": 0}, (window("session", 100, 600),), False),
            ("dispatch_budget", {"max_agent_dispatches": 1}, (window("session", 100, 600),), False),
            ("evidence damaged after the judgment", {}, (window("session", 100, 600),), True),
        ]

        def outcome(runtime, rows, damage, bypass):
            config, original = copy.deepcopy(self.config), self.config
            config.data["runtime"].update(runtime)
            self.config = config
            self.windows(*rows)
            runs = self.repo / ".harness" / "runs"
            before = set(runs.iterdir()) if runs.exists() else set()
            patches = [patch.object(QuotaWait, "after_failure", return_value=False)] if bypass else []
            patches += [patch.object(RunStore, "commit", damaging)] if damage else []
            for item in patches:
                item.start()
            try:
                code, errors = run_cli(self, self.backend("worker"),
                                       ["run", "Implement greet", "--plan", str(ROOT / "examples" / "plan.json")])
            finally:
                for item in reversed(patches):
                    item.stop()
                self.config = original
            (directory,) = set(runs.iterdir()) - before
            saved = read_json(directory / "state.json")
            records = [read_json(path) for path in (self.repo / ".harness" / "operations").glob("*.json")]
            record = next(record for record in records if record.get("run_id") == saved["id"])
            stop = copy.deepcopy(saved["stop"])
            for key in (*WAIT_FIELDS, *SAMPLE_FIELDS, "detail"):
                stop["data"].pop(key, None)
            text = json.dumps(stop).replace(saved["id"], "RUN").replace(str(directory), "RUN_DIR")
            for position, entry in enumerate(saved["dispatches"]):
                text = text.replace(entry["id"], f"DISPATCH_{position}")
            text = re.sub(r"\b[0-9a-f]{64}\b", "SHA256", text)
            return {"decisions": test_quota_sampling.SamplingCase.decisions(saved, directory),
                    "stop": json.loads(text), "exit": code, "status": saved["status"],
                    "record": (record["exit_status"], record["stop"]["kind"], record["stop"]["reason"])}

        for name, runtime, rows, damage in scenarios:
            with self.subTest(case=name):
                waited, bypassed = outcome(runtime, rows, damage, False), outcome(runtime, rows, damage, True)
                self.assertEqual(waited, bypassed)
                self.assertEqual(self.clock.sleeps, [])
                expected = (("evidence", "load_integrity", 1) if damage else ("provider_quota", "usage_limit", 3))
                self.assertEqual((waited["stop"]["kind"], waited["stop"]["reason"], waited["exit"]), expected)
                self.assertEqual(waited["record"], (expected[2], *expected[:2]))
                if damage:
                    self.assertEqual(waited["stop"]["data"]["superseded"],
                                     {"kind": "provider_quota", "reason": "usage_limit"})


class WakeCheckTests(WaitCase):
    def waking_into(self, change, *, at_wake_sample=False):
        """A worker failure whose wait sees ``change`` (during the sleep, or once the clearing save is
        on disk, at the wake sample); returns the run, its stop and exit code after zero further model calls."""
        self.windows(window("session", 100, 600))
        backend = self.backend("worker")
        engine = self.engine(backend)
        sample = quota.QuotaSampler.sample

        def changed_at_wake(sampler, provider, trigger, **kwargs):
            record = sample(sampler, provider, trigger, **kwargs)
            if trigger == "wake":
                change(engine)
            return record
        if at_wake_sample:
            patcher = patch.object(quota.QuotaSampler, "sample", changed_at_wake)
            patcher.start()
            self.addCleanup(patcher.stop)
        else:
            self.clock.on_sleep = lambda clock, seconds: change(engine) if len(clock.sleeps) == 1 else None
        with self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        if at_wake_sample:
            patcher.stop()
        self.assertEqual([call[0] for call in backend.calls], ["worker"])   # zero model calls after waking
        return engine, stops.of(caught.exception), stops.exit_code(caught.exception)

    def test_wake_rechecks_share_resume_code(self):  # T8-33
        from harness import git

        def ledger(engine):     # another writer
            path = engine.directory / "state.json"
            run = read_json(path)
            run["goal"] = "rewritten by another writer"
            write_json(path, run)

        def binding(engine):    # the worktree's Git link now points elsewhere
            gitfile = Path(engine.run["worktree"]) / ".git"
            gitfile.write_text(gitfile.read_text().rstrip("\n") + "-moved\n")

        def original(engine):   # a load-checked original
            entry = next(entry for entry in engine.run["dispatches"] if entry.get("worker_receipt"))
            Path(entry["worker_receipt"]["packet_artifact"]).write_text("{}\n")

        damaged = {"index": False}

        def index(engine):      # what the path-index validator reports for a damaged index
            damaged["index"] = True

        def validate_indexes(directory, dispatches):
            if damaged["index"]:
                from harness.util import EvidenceError
                raise EvidenceError("路径证据索引已改变（模拟）", stop=stops.make("evidence", "runtime_integrity"))
        cases = [("ledger rewritten after the clearing save", ledger, ("runtime_failure", "invariant"), True),
                 ("git binding changed", binding, ("workspace_mismatch", "binding_changed"), False),
                 ("load-checked original damaged", original, ("evidence", "load_integrity"), False),
                 ("path index damaged", index, ("evidence", "runtime_integrity"), False)]
        for name, change, expected, at_wake in cases:
            with self.subTest(case=name), patch("harness.run_lifecycle.validate_dispatch_indexes", validate_indexes):
                self.clock.sleeps.clear()
                self.clock.on_sleep = None
                engine, value, exit_code = self.waking_into(change, at_wake_sample=at_wake)
                self.assertEqual(((value["kind"], value["reason"]), exit_code), (expected, 1))
                if name.startswith("load-checked"):
                    self.assertNotIn("superseded", value["data"])   # the load failure is the settled value itself
                if name.startswith("ledger"):
                    # The other writer's ledger is never overwritten, not even by the stop.
                    self.assertEqual(read_json(engine.directory / "state.json")["goal"], "rewritten by another writer")
                damaged["index"] = False

    def test_wake_checks_write_nothing(self):  # T8-33 (read-only)
        import ast
        import inspect
        import textwrap
        # Directly: none of them saves, locks for writing, records an event or writes a file.
        writers = {"save", "commit", "event", "observe", "checkpoint", "write_json", "write_text", "write_bytes",
                   "confirm_exited", "ensure_exited", "land_returned_ask", "publish_new_json", "mkdir", "unlink"}
        for name in ("wake_checks", "verify_assets_and_plan", "verify_binding_and_indexes", "verify_head_and_inputs"):
            tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(RunLifecycle, name))))
            called = {node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                      for node in ast.walk(tree) if isinstance(node, ast.Call)}
            self.assertFalse(called & writers, name)
        # And in effect, on a leftover waiting run: the run directory and the worktree are unchanged.
        self.windows(window("session", 100, 600))
        engine = self.engine(self.backend("worker"))
        crash, state = crash_after_save(engine, lambda run: "quota_wait" in run)
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())

        def files():
            return {str(path): path.read_bytes() for path in sorted(engine.directory.rglob("*")) if path.is_file()}
        from harness import git
        checker = self.engine(self.backend())
        checker.load_run(engine.run["id"])
        before, status = files(), git.git(Path(checker.run["worktree"]), "status", "--porcelain", "--ignored")
        checker.services.lifecycle.wake_checks()
        self.assertEqual(files(), before)
        self.assertEqual(git.git(Path(checker.run["worktree"]), "status", "--porcelain", "--ignored"), status)

    def test_wake_and_resume_call_the_same_checks(self):  # T8-33
        calls = []
        names = ("verify_assets_and_plan", "verify_binding_and_indexes", "verify_head_and_inputs")
        originals = {name: getattr(RunLifecycle, name) for name in names}

        def recorder(name):
            def wrapped(lifecycle):
                calls.append(name)
                return originals[name](lifecycle)
            return wrapped
        self.windows(window("session", 100, 3600))
        with patch.multiple(RunLifecycle, **{name: recorder(name) for name in names}):
            # A failure that does not wait (over the limit), then a wait on resume: resume's preflight, then the wake-up.
            config = copy.deepcopy(self.config)
            config.data["runtime"]["quota_wait_max_seconds"] = 600
            engine, saved = self.start(self.backend("worker"), expect=HarnessError, config=config)
            self.assertEqual(calls, [])
            result = self.engine(self.backend("worker")).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls, list(names) * 2)
        # Replacing one shared check changes both paths: resume refuses, and a wake-up stops, with its type.
        refusal = HarnessError("synthetic HEAD refusal", stop=stops.make("workspace_mismatch", "head_changed"))
        self.clock.sleeps.clear()
        replaced = patch.object(RunLifecycle, "verify_head_and_inputs", side_effect=refusal)
        self.addCleanup(replaced.stop)
        engine, value, _ = self.waking_into(lambda engine: replaced.start())
        replaced.stop()
        self.assertEqual((value["kind"], value["reason"]), ("workspace_mismatch", "head_changed"))
        with patch.object(RunLifecycle, "verify_head_and_inputs", side_effect=refusal), \
                self.assertRaises(HarnessError) as caught:
            self.engine(self.backend()).resume(engine.run["id"])
        self.assertEqual(stops.of(caught.exception)["reason"], "head_changed")


if __name__ == "__main__":
    unittest.main()
