"""Public CLI recovery after SIGKILL at persistence and native Git boundaries."""
import errno
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from harness import git
from harness.util import read_json
from test_git_invocation import plain_git
from test_harness import ROOT, RepoCase, cli_notice, write_project_config


class AcceptanceProcessRecoveryTests(RepoCase):
    def setUp(self):
        super().setUp()
        write_project_config(self.repo, commands=[
            {"cwd": ".", "argv": [sys.executable, "-B", "-m", "unittest", "-v"]},
        ])

    def command(self, control, boundary, *args):
        return [sys.executable, "-B", str(ROOT / "tests" / "acceptance_process_fixture.py"),
                str(control), boundary, "--repo", str(self.repo), *args]

    def start_args(self):
        return ("run", "Implement greet", "--plan", str(ROOT / "examples" / "plan.json"))

    def run_cli(self, control, *args, code=0):
        result = subprocess.run(self.command(control, "none", *args), cwd=ROOT,
                                text=True, capture_output=True, timeout=30,
                                env=dict(os.environ, PYTHONPATH=str(ROOT)))
        self.assertEqual(result.returncode, code, (args, result.stdout, result.stderr))
        return result

    def control(self, label):
        path = self.outside / label
        path.mkdir()
        return path

    def call_sequence(self, control):
        return [json.loads(line) for line in (control / "calls").read_text().splitlines()]

    def wait_for(self, ready, description):
        deadline = time.monotonic() + 18
        while not ready():
            if time.monotonic() >= deadline:
                self.fail(f"Timed out waiting for {description}")
            time.sleep(0.01)

    def interrupt(self, boundary):
        control = self.control(boundary)
        with (control / "output").open("w+") as log:
            process = subprocess.Popen(self.command(control, boundary, *self.start_args()),
                                       cwd=ROOT, stdout=log, stderr=log, start_new_session=True,
                                       env=dict(os.environ, PYTHONPATH=str(ROOT)))
        self.addCleanup(self.terminate_fixture, process, control)
        marker = control / "ready"
        self.wait_for(lambda: marker.exists() or process.poll() is not None, boundary)
        self.assertIsNone(process.poll(), (control / "output").read_text())
        events = [json.loads(line.removeprefix("[harness-json] "))
                  for line in (control / "output").read_text().splitlines()
                  if line.startswith("[harness-json] ")]
        created, = [event for event in events if event["event"] == "run_created"]
        ledger = self.repo / ".harness" / "runs" / created["run_id"] / "state.json"
        if boundary in ("hold_ref", "hold_index"):
            (control / "lock_path").write_text(str(ledger.with_name("run.lock")))
        process.kill()
        self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
        return control, ledger

    def terminate_fixture(self, process, control):
        # Always release the helper before removing its files, including after a failed assertion.
        (control / "release").touch()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        elif (control / "lock_path").exists():
            lock = Path((control / "lock_path").read_text())
            self.wait_for(lambda: self.lock_available(lock), "inherited run lock cleanup")

    def check_progress(self, ledger, *, has_intent, moved, synced):
        saved = read_json(ledger)
        pending = saved["pending_acceptance"]
        self.assertEqual("intent" in pending, has_intent)
        self.assertEqual(saved["accepted"], {})
        self.assertNotIn("pending_post_accept", saved)
        intent = pending.get("intent")
        binding = saved["worktree_binding"]
        if has_intent:
            self.assertEqual(saved["expected_head"], intent["parent"])
        head = git.head(Path(saved["worktree"]), binding=binding)
        self.assertEqual(head, intent["commit"] if moved else saved["expected_head"])
        if has_intent:
            digest = git.index_digest(Path(saved["worktree"]), binding=binding)
            self.assertNotEqual(intent["index_before"], intent["index_synced"])
            self.assertEqual(digest, intent["index_synced" if synced else "index_before"])
        return saved

    def check_completion(self, control, ledger, saved, baseline):
        result = json.loads(self.run_cli(control, "resume", saved["id"]).stdout)
        after = ledger.read_bytes()
        completed = json.loads(after)
        self.assertEqual(completed["status"], "completed")
        self.assertNotIn("pending_acceptance", completed)
        self.assertNotIn("pending_post_accept", completed)
        parent = saved["expected_head"]
        accepted = completed["accepted"]["greet"]
        commit = accepted["commit"]
        self.assertEqual(completed["expected_head"], commit)
        cwd = Path(completed["worktree"])
        self.assertEqual(plain_git(cwd, "rev-list", "--parents", f"{parent}..HEAD").stdout.split(),
                         [commit.encode(), parent.encode()])
        self.assertEqual(plain_git(cwd, "rev-parse", "HEAD^{tree}").stdout.strip(), accepted["tree"].encode())
        intent = saved["pending_acceptance"].get("intent")
        if intent:
            self.assertEqual((commit, accepted["tree"]), (intent["commit"], intent["tree"]))
        self.assertEqual(self.call_sequence(control), baseline)
        replay = self.run_cli(control, "resume", saved["id"])
        replay_result = json.loads(replay.stdout)
        self.assertNotEqual(replay_result.pop("operation_id"), result.pop("operation_id"))
        self.assertEqual(replay_result, result)
        self.assertEqual(ledger.read_bytes(), after)
        self.assertEqual(self.call_sequence(control), baseline)

    def test_five_sigkill_boundaries_recover_in_new_public_process(self):
        normal = self.control("normal")
        self.run_cli(normal, *self.start_args())
        baseline = self.call_sequence(normal)
        self.assertEqual(baseline, [["worker", "implement"], ["reviewer", "review"], ["reviewer", "final_review"]])
        cases = (
            ("before_intent", False, False, False),
            ("after_intent", True, False, False),
            ("after_ref", True, True, False),
            ("after_index", True, True, True),
            ("before_acceptance", True, True, True),
        )
        for boundary, has_intent, moved, synced in cases:
            with self.subTest(boundary=boundary):
                control, ledger = self.interrupt(boundary)
                saved = self.check_progress(ledger, has_intent=has_intent, moved=moved, synced=synced)
                self.assertEqual(self.call_sequence(control), baseline[:-1])
                self.check_completion(control, ledger, saved, baseline)

    def lock_available(self, path):
        with path.open("rb") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                return False
            return True

    def test_old_git_launcher_excludes_each_public_entry_until_exit(self):
        for boundary, moved in (("hold_ref", False), ("hold_index", True)):
            with self.subTest(boundary=boundary):
                control, ledger = self.interrupt(boundary)
                lock = ledger.with_name("run.lock")
                _, device, inode = read_json(control / "ready")
                self.assertEqual((device, inode), (lock.stat().st_dev, lock.stat().st_ino))
                self.assertFalse(self.lock_available(lock))
                saved = self.check_progress(ledger, has_intent=True, moved=moved, synced=False)
                before = ledger.read_bytes()
                calls = self.call_sequence(control)
                for command, extra, code in (("resume", (), 3), ("abandon", (), 3),
                                             ("cleanup-run", (), 1), ("cleanup-run", ("--remove",), 1)):
                    with self.subTest(command=command, extra=extra):
                        result = self.run_cli(control, command, saved["id"], *extra, code=code)
                        stop = cli_notice(result.stderr)
                        self.assertIsNotNone(stop, result.stderr)
                        self.assertEqual((stop["kind"], stop["reason"]), ("busy", "run_lock"))
                        self.assertEqual(ledger.read_bytes(), before)
                        self.assertEqual(self.call_sequence(control), calls)
                        self.assertEqual(self.check_progress(ledger, has_intent=True, moved=moved, synced=False),
                                         saved)
                (control / "release").touch()
                self.wait_for(lambda: self.lock_available(lock), "old child to exit")
                self.assertEqual(ledger.read_bytes(), before)
                self.check_progress(ledger, has_intent=True, moved=True, synced=moved)
                self.check_completion(control, ledger, saved, [*calls, ["reviewer", "final_review"]])
