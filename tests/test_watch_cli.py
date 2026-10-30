import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import cli
from harness.util import HarnessError, write_json
from harness.watch_cli import watch_run


class WatchCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / ".harness"
        self.path = self.state / "runs/run_test/state.json"
        self.run = {"id": "run_test", "status": "running", "current_task": "t", "accepted": {},
                    "attempts": {"t": 1}, "dispatches": []}
        write_json(self.path, self.run)

    def test_timeouts_do_not_read_state_and_only_changed_projections_are_emitted(self):
        path, run = self.path, self.run
        class Watcher:
            backend = "fixture"
            def __init__(self, *args): self.calls = 0
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def wait(self, seconds):
                self.calls += 1
                if self.calls == 1:
                    path.write_text("not JSON")
                    return False
                if self.calls == 2:
                    write_json(path, run)
                    return True
                write_json(path, {**run, "status": "blocked", "error": "budget reached"})
                return True
        output = io.StringIO()
        with patch("harness.watch_cli.RunWatcher", Watcher):
            self.assertEqual(watch_run(self.state, "run_test", stdout=output), 0)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row["status"] for row in rows], ["running", "blocked"])
        self.assertEqual(rows[-1]["error"], "budget reached")

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
        write_json(self.path, {**self.run, "status": "blocked"})
        closed = []
        class Watcher:
            backend = "fixture"
            def __init__(self, *args): pass
            def __enter__(self): return self
            def __exit__(self, *args): closed.append(True)
            def wait(self, seconds): raise KeyboardInterrupt()
        with patch("harness.watch_cli.RunWatcher", Watcher), self.assertRaises(KeyboardInterrupt):
            watch_run(self.state, "run_test", follow=True, stdout=io.StringIO())
        self.assertEqual(closed, [True])

    def test_missing_or_mismatched_run_is_not_reported_as_success(self):
        with self.assertRaises(HarnessError):
            watch_run(self.state, "run_missing", stdout=io.StringIO())
        write_json(self.path, {**self.run, "id": "run_other"})
        with self.assertRaisesRegex(HarnessError, "ID"):
            watch_run(self.state, "run_test", stdout=io.StringIO())
