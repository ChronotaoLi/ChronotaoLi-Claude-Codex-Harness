import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_harness import stopped
from harness import stops
from harness import cli, observe
from harness.util import HarnessError, write_json
from harness.versions import RUN_SCHEMA_VERSION
from harness.watch_cli import watch_run


class WatchCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / ".harness"
        self.path = self.state / "runs/run_test/state.json"
        self.run = {"schema_version": RUN_SCHEMA_VERSION, "id": "run_test", "status": "running", "current_task": "t", "accepted": {},
                    "attempts": {"t": 1}, "dispatches": []}
        write_json(self.path, self.run)

    def test_timeouts_do_not_read_state_and_only_changed_projections_are_emitted(self):
        path, run = self.path, self.run
        class Watcher:
            backend = "fixture"
            def __init__(self, *args): self.calls = 0
            def close(self): pass
            def wait(self, seconds):
                self.calls += 1
                if self.calls == 1:
                    path.write_text("not JSON")
                    return False
                if self.calls == 2:
                    write_json(path, run)
                    return True
                write_json(path, {**run, "status": "blocked",
                                  "stop": stops.make("unclassified", "untyped_harness_error",
                                                     data={"error_type": "HarnessError", "detail": "budget reached"})})
                return True
        output = io.StringIO()
        with patch("harness.watch_cli.RunWatcher", Watcher):
            self.assertEqual(watch_run(self.state, "run_test", stdout=output), 0)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row["status"] for row in rows], ["running", "blocked"])
        self.assertEqual(rows[-1]["stop"], {"kind": "unclassified", "reason": "untyped_harness_error",
                                            "title": stops.REGISTRY[("unclassified", "untyped_harness_error")]["title"],
                                            "exit_code": 1})
        self.assertNotIn("error", rows[-1])

    def test_a_registered_review_conclusion_is_one_more_line(self):
        def rejected(attempt):
            return {"attempt": attempt, "snapshot_digest": f"s{attempt}", "review_source": {"dispatch_id": f"d{attempt}"},
                    "review": {"verdict": "request_changes", "findings": [{"id": "gate", "severity": "blocking"}]}}

        five = {**self.run, "attempts": {"t": 6}, "review_history": {"t": [rejected(n) for n in range(1, 6)]}}
        six = {**five, "review_history": {"t": [rejected(n) for n in range(1, 7)]}}
        ledgers = [six, {**six, "goal": "not projected"},
                   {**six, "status": "blocked", "stop": stops.make("unclassified", "untyped_harness_error",
                                                                   data={"error_type": "HarnessError", "detail": "x"})}]
        write_json(self.path, five)
        path = self.path
        class Watcher:
            backend = "fixture"
            def __init__(self, *args): pass
            def close(self): pass
            def wait(self, seconds):
                write_json(path, ledgers.pop(0))
                return True
        output = io.StringIO()
        with patch("harness.watch_cli.RunWatcher", Watcher):
            self.assertEqual(watch_run(self.state, "run_test", stdout=output), 0)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row["status"] for row in rows], ["running", "running", "blocked"])
        first, second = rows[0], rows[1]
        self.assertEqual({key: value for key, value in first.items() if key != "convergence"},
                         {key: value for key, value in second.items() if key != "convergence"})
        before, after = first["convergence"]["warnings"][0], second["convergence"]["warnings"][0]
        self.assertEqual((before["latest_review"]["attempt"], before["attempts_since_latest_review"],
                          [item["round"] for item in before["compared_reviews"]]), (5, 1, [4, 5]))
        self.assertEqual((after["latest_review"]["attempt"], after["attempts_since_latest_review"],
                          [item["round"] for item in after["compared_reviews"]]), (6, 0, [5, 6]))
        self.assertEqual(rows[2]["convergence"], second["convergence"])

    def test_explicit_state_command_needs_no_git_repository_or_config(self):
        write_json(self.path, {**self.run, "status": "completed"})
        output = io.StringIO()
        with patch("harness.cli.Config.load", side_effect=AssertionError("config loaded")), \
                patch("harness.cli.git.repository", side_effect=AssertionError("git probed")), \
                contextlib.redirect_stdout(output):
            code = cli.main(["--config", "/missing", "watch", "run_test", "--state", str(self.state)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "completed")

    def test_follow_continues_past_blocked_and_closes_on_interrupt(self):
        write_json(self.path, stopped(dict(self.run)))
        closed = []
        class Watcher:
            backend = "fixture"
            def __init__(self, *args): pass
            def close(self):
                if not closed:
                    closed.append(True)
            def wait(self, seconds): raise KeyboardInterrupt()
        with patch("harness.watch_cli.RunWatcher", Watcher), self.assertRaises(KeyboardInterrupt):
            watch_run(self.state, "run_test", follow=True, stdout=io.StringIO())
        self.assertEqual(closed, [True])

    def test_missing_run_identifies_the_run_argument(self):
        with self.assertRaises(HarnessError) as caught:
            watch_run(self.state, "run_missing", stdout=io.StringIO())
        stop = stops.of(caught.exception)
        self.assertEqual((stop["kind"], stop["reason"], stop["data"]["option"]),
                         ("invalid_input", "cli_arguments", "run_id"))

    def test_only_missing_initial_run_directory_identifies_the_argument(self):
        self.path.unlink()
        for state, run_id, missing in (
            (self.state, "run_test", "state.json"),
            (self.state / "absent", "run_test", "absent"),
        ):
            with self.subTest(missing=missing):
                output = io.StringIO()
                with self.assertRaises(HarnessError) as caught:
                    watch_run(state, run_id, stdout=output)
                self.assertIsNone(caught.exception.stop)
                cause = caught.exception.__cause__
                self.assertIs(type(cause), observe.NotFound)
                self.assertEqual(Path(cause.step.name).name, missing)
                self.assertEqual(output.getvalue(), "")

    def test_watch_keeps_shared_ledger_refusals_untyped(self):
        for fields, message in ((None, "无法解析"), ({"id": "run_other"}, "ID"),
                                ({"accepted": []}, "accepted"), ({"attempts": {"t": -1}}, "尝试计数")):
            with self.subTest(fields=fields):
                if fields is None:
                    self.path.write_bytes(b"{")
                else:
                    write_json(self.path, {**self.run, **fields})
                output = io.StringIO()
                with self.assertRaisesRegex(HarnessError, message) as caught:
                    watch_run(self.state, "run_test", stdout=output)
                self.assertIsNone(caught.exception.stop)
                self.assertEqual(output.getvalue(), "")
