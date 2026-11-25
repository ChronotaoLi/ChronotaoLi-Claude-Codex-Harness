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
            child, _, _ = read_json(marker)
            self.assertEqual(os.getsid(child), child)
            self.assertNotEqual(child, process.pid)
        process.kill()
        self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
        return control, ledger

    def terminate_fixture(self, process, control):
        # Reap the only process we signal before releasing any independent writer.
        try:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        finally:
            (control / "release").touch()
        lifetime = control / "git-lifetime.lock"
        if lifetime.exists():
            self.wait_for(lambda: self.lock_available(lifetime), f"fixture Git descriptors to close ({control})")
        pid_path = control / "git-pid"
        if pid_path.exists():
            child = int(pid_path.read_text())

            def exited():
                try:
                    os.getpgid(child)
                except ProcessLookupError:
                    return True
                return False

            self.wait_for(exited, f"fixture Git process {child} to exit ({control})")

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

    def test_missing_supervisor_and_dispatch_finally_facts_require_exit_gate(self):
        for state in ("starting", "running"):
            with self.subTest(state=state):
                control, ledger = self.interrupt(f"dispatch_{state}")
                before = ledger.read_bytes()
                saved = json.loads(before)
                worker, = saved["dispatches"]
                self.assertEqual(saved["attempts"], {"greet": 1})
                self.assertEqual(saved["agent_dispatches"], 1)
                self.assertTrue(worker["invoked"])
                self.assertEqual(worker["status"], "running")
                self.assertEqual(worker["process_state"], state)
                self.assertNotIn("process_outcome", worker)
                self.assertNotIn("exit_code", worker)
                extra = ()
                if state == "starting":
                    self.assertIsNone(worker["pgid"])
                    blocked = self.run_cli(control, "resume", saved["id"], code=3)
                    stop = cli_notice(blocked.stderr)
                    self.assertEqual((stop["kind"], stop["reason"]), ("process_unconfirmed", "dispatch_exit"))
                    self.assertEqual(ledger.read_bytes(), before)
                    self.assertEqual(self.call_sequence(control), [["worker", "implement"]])
                    extra = ("--confirm-exited", worker["id"])
                else:
                    # The supervisor observed a real child leave, but had not stored its outcome.
                    outcome = read_json(control / "outcome")
                    self.assertEqual((outcome["pgid"], outcome["exit_code"]), (worker["pgid"], 0))
                    self.assertTrue(outcome["exit_confirmed"])
                self.run_cli(control, "resume", saved["id"], *extra)
                completed = read_json(ledger)
                self.assertEqual(completed["status"], "completed")
                self.assertEqual(completed["attempts"], {"greet": 2})
                self.assertEqual(completed["agent_dispatches"], 5)
                self.assertEqual(self.call_sequence(control), [
                    ["worker", "implement"], ["commander", "decide"], ["worker", "implement"],
                    ["reviewer", "review"], ["reviewer", "final_review"],
                ])
                first, fresh = [row for row in completed["dispatches"] if row["role"] == "worker"]
                self.assertEqual(first["id"], worker["id"])
                self.assertNotEqual(first["id"], fresh["id"])
                self.assertNotIn("process_outcome", first)
                self.assertNotIn("exit_code", first)

    def test_returned_worker_result_is_preserved_but_resume_cannot_seal_it(self):
        control, ledger = self.interrupt("before_seal")
        saved = read_json(ledger)
        worker, = saved["dispatches"]
        self.assertEqual(worker["process_state"], "exited")
        self.assertEqual(worker["process_outcome"]["exit_code"], 0)
        job = ledger.parent / "dispatches" / worker["id"]
        response = (job / "response.json").read_bytes()
        self.assertEqual(json.loads(response)["dispatch_id"], worker["id"])
        self.assertFalse((job / "worker-seal.json").exists())
        result = self.run_cli(control, "resume", saved["id"], code=1)
        stop = cli_notice(result.stderr)
        self.assertEqual((stop["kind"], stop["reason"]), ("evidence", "unsealed_return"))
        stopped = read_json(ledger)
        first, = stopped["dispatches"]
        self.assertEqual(first["process_outcome"], worker["process_outcome"])
        self.assertEqual(first["worker_return_evidence"]["state"], "returned")
        self.assertEqual(stopped["worker_receipts"], saved["worker_receipts"])
        self.assertEqual(stopped["attempts"], {"greet": 1})
        self.assertEqual(stopped["agent_dispatches"], 1)
        self.assertEqual(stopped["accepted"], {})
        self.assertEqual(self.call_sequence(control), [["worker", "implement"]])
        self.assertEqual((job / "response.json").read_bytes(), response)
        self.assertFalse((job / "worker-seal.json").exists())

    def test_registered_checkout_without_saved_binding_is_not_adopted(self):
        control, ledger = self.interrupt("before_binding")
        before = ledger.read_bytes()
        saved = json.loads(before)
        self.assertEqual(saved["worktree_binding"], {})
        self.assertTrue(saved["original_tasks"])
        self.assertEqual(saved["agent_dispatches"], 0)
        worktree = Path(saved["worktree"])
        self.assertTrue((worktree / "test_hello.py").is_file())
        registrations = plain_git(self.repo, "worktree", "list", "--porcelain").stdout
        self.assertIn(f"branch refs/heads/{saved['branch']}\n".encode(), registrations)
        result = self.run_cli(control, "resume", saved["id"], code=1)
        stop = cli_notice(result.stderr)
        self.assertEqual((stop["kind"], stop["reason"]), ("workspace_mismatch", "binding_missing"))
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(plain_git(self.repo, "worktree", "list", "--porcelain").stdout, registrations)
        self.assertFalse((control / "calls").exists())

    def test_unreplaced_ledger_temp_is_not_used_on_resume(self):
        control, ledger = self.interrupt("ledger_tmp")
        saved = read_json(ledger)
        temporary = ledger.with_name((control / "temporary").read_text())
        raw = temporary.read_bytes()
        staged = json.loads(raw)
        old, = saved["dispatches"]
        candidate, = staged["dispatches"]
        self.assertEqual(staged["store_revision"], saved["store_revision"] + 1)
        self.assertEqual((saved["agent_dispatches"], saved["attempts"]), (0, {}))
        self.assertEqual((staged["agent_dispatches"], staged["attempts"]), (1, {"greet": 1}))
        self.assertEqual((old["id"], old["process_state"], old["invoked"]), (candidate["id"], "prepared", False))
        self.assertEqual((candidate["process_state"], candidate["invoked"]), ("starting", True))
        self.assertFalse((control / "calls").exists())
        self.run_cli(control, "resume", saved["id"])
        completed = read_json(ledger)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["attempts"], {"greet": 1})
        self.assertEqual(completed["agent_dispatches"], 3)
        invoked_workers = [row for row in completed["dispatches"] if row["role"] == "worker" and row["invoked"]]
        fresh, = invoked_workers
        self.assertNotEqual(fresh["id"], old["id"])
        self.assertEqual(temporary.read_bytes(), raw)
