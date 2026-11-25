"""Exercise cooperative interruption at the CLI boundary with offline backends."""
from contextlib import redirect_stderr, redirect_stdout, suppress
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from unittest.mock import patch

from harness import cli, interrupts
from harness.adapters import DemoBackend
from harness.operations import OperationRecord
from harness.run_lifecycle import RunLifecycle
from harness.runtime import Runtime
from harness.util import read_json
from test_harness import ROOT, RepoCase, write_project_config


class CLIInterruptControllerTests(RepoCase):
    def setUp(self):
        super().setUp()
        write_project_config(self.repo)
        self.args = ["--config", str(ROOT / "harness.toml"), "--repo", str(self.repo)]
        self.plan = ["--plan", str(ROOT / "examples" / "plan.json")]

    @staticmethod
    def offline_runtime(*args, **kwargs):
        return Runtime(*args, backend=DemoBackend(), **kwargs)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli, "Runtime", self.offline_runtime), redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main([*self.args, *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_owns_signals_through_operation_creation_finish_and_abandon(self):
        signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        original = tuple(signal.getsignal(sig) for sig in signals)
        create, finish, precheck = OperationRecord.create, OperationRecord.finish, cli.precheck_run
        created, finished = [], []

        def create_operation(*args, **kwargs):
            controller = interrupts.active()
            self.assertIsNotNone(controller)
            self.assertTrue(all(signal.getsignal(sig) == controller.remember for sig in signals))
            created.append(controller)
            return create(*args, **kwargs)

        def finish_operation(operation, **kwargs):
            controller = interrupts.active()
            self.assertIs(controller, created[-1])
            self.assertTrue(controller.terminal)
            finished.append(controller)
            signal.raise_signal(signal.SIGHUP)
            return finish(operation, **kwargs)

        with patch.object(OperationRecord, "create", create_operation), \
                patch.object(OperationRecord, "finish", finish_operation):
            code, output, errors = self.invoke("run", "Implement greet", *self.plan, "--pause-after-plan")
            self.assertEqual(code, 0, errors)
            pending = json.loads(output)
            self.assertEqual(pending["status"], "awaiting_approval")
            self.assertIn("status=awaiting_approval", errors)
            code, output, errors = self.invoke("resume", pending["id"])
            self.assertEqual(code, 0, errors)
            self.assertEqual(json.loads(output)["status"], "awaiting_approval")
            self.assertIn("status=awaiting_approval", errors)
        self.assertEqual(created, finished)
        self.assertEqual(len(set(created)), 2)

        path = self.repo / ".harness" / "runs" / pending["id"] / "state.json"
        before = path.read_bytes()
        abandon_controllers = []

        def precheck_then_signal(*args, **kwargs):
            controller = interrupts.active()
            self.assertIsNotNone(controller)
            abandon_controllers.append(controller)
            result = precheck(*args, **kwargs)
            signal.raise_signal(signal.SIGTERM)
            return result

        with patch.object(cli, "precheck_run", precheck_then_signal):
            code, _, errors = self.invoke("abandon", pending["id"])
        self.assertEqual(code, 130, errors)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(abandon_controllers), 1)

        output_controller = []
        print_result = cli.print_saved_result

        def signal_during_output(*args, **kwargs):
            controller = interrupts.active()
            self.assertIsNotNone(controller)
            self.assertTrue(controller.terminal)
            output_controller.append(controller)
            signal.raise_signal(signal.SIGINT)
            return print_result(*args, **kwargs)

        with patch.object(cli, "print_saved_result", signal_during_output):
            code, output, errors = self.invoke("abandon", pending["id"])
        self.assertEqual(code, 0, errors)
        self.assertIn("status=abandoned", errors)
        self.assertEqual(json.loads(output)["status"], "abandoned")
        self.assertEqual(read_json(path)["status"], "abandoned")
        self.assertEqual(len(output_controller), 1)
        self.assertIsNot(output_controller[0], abandon_controllers[0])
        self.assertEqual(tuple(signal.getsignal(sig) for sig in signals), original)

    def test_resume_completed_delivers_at_locked_load_and_defers_after_terminal_mark(self):
        code, output, errors = self.invoke("run", "Implement greet", *self.plan)
        self.assertEqual(code, 0, errors)
        completed = json.loads(output)
        self.assertEqual(completed["status"], "completed")
        state = self.repo / ".harness"
        ledger = state / "runs" / completed["id"] / "state.json"
        before = ledger.read_bytes()
        load = RunLifecycle.load_run
        mark = interrupts.terminal
        seen = []

        def load_then_signal(lifecycle, ident):
            load(lifecycle, ident)
            if lifecycle.context.run_lock_descriptor is not None:
                controller = interrupts.active()
                self.assertFalse(controller.terminal)
                self.assertEqual(lifecycle.context.run["status"], "completed")
                seen.append(controller)
                signal.raise_signal(signal.SIGINT)
                self.assertTrue(interrupts.deliverable())

        def mark_then_signal(*args, **kwargs):
            mark(*args, **kwargs)
            controller = interrupts.active()
            self.assertTrue(controller.terminal)
            seen.append(controller)
            signal.raise_signal(signal.SIGINT)
            self.assertFalse(interrupts.deliverable())

        with patch.object(DemoBackend, "invoke", side_effect=AssertionError("completed resume called a model")):
            with patch.object(RunLifecycle, "load_run", load_then_signal):
                code, output, errors = self.invoke("resume", completed["id"], "--operation-id", "early-completed")
            self.assertEqual((code, output), (130, ""), errors)
            self.assertEqual(seen[0].delivered_from, "RunLifecycle.resume.<locals>.resume_locked")
            early = read_json(state / "operations" / "early-completed.json")
            self.assertEqual((early["exit_status"], early["stop"]["kind"]), (130, "interrupted"))
            self.assertEqual(ledger.read_bytes(), before)

            with patch.object(interrupts, "terminal", mark_then_signal):
                code, output, errors = self.invoke("resume", completed["id"], "--operation-id", "late-completed")
            self.assertEqual(code, 0, errors)
            self.assertEqual(json.loads(output)["status"], "completed")
            self.assertIn("status=completed", errors)
            late = read_json(state / "operations" / "late-completed.json")
            self.assertEqual((late["exit_status"], late["result_status"]), (0, "completed"))
            self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(len(seen), 2)
        self.assertIsNone(seen[1].delivered_from)

    def test_ctrl_c_to_cli_group_cancels_git_via_supervision_and_resume_completes(self):
        shim, marker, received = (self.outside / name for name in ("git", "git-started", "git-signal"))
        real_git = shutil.which("git")
        wait_for_signal = (
            "import os, pathlib, signal, sys, time\n"
            f"marker = pathlib.Path({str(marker)!r})\n"
            f"received = pathlib.Path({str(received)!r})\n"
            "def record(signum, frame):\n"
            "    received.write_text(signal.Signals(signum).name)\n"
            "    sys.exit(128 + signum)\n"
            "signal.signal(signal.SIGINT, record)\n"
            "signal.signal(signal.SIGTERM, record)\n"
            "marker.write_text(f'{os.getpid()} {os.getpgrp()}')\n"
            "time.sleep(30)\n"
        )
        slow_git = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(wait_for_signal)}"
        shim.write_text(
            '#!/bin/sh\ncase " $* " in\n'
            '  *" --work-tree="*" ls-files --stage -z ")\n'
            f"    if [ ! -e {shlex.quote(str(marker))} ]; then\n      {slow_git}\n    fi;;\n"
            'esac\n'
            f'exec {shlex.quote(real_git)} "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o700)
        driver = (
            "import sys\n"
            "from harness import cli\n"
            "from harness.adapters import DemoBackend\n"
            "from harness.runtime import Runtime\n"
            "cli.Runtime = lambda *args, **kwargs: Runtime(*args, backend=DemoBackend(), **kwargs)\n"
            "sys.exit(cli.main(sys.argv[1:]))\n"
        )
        env = {**os.environ, "PATH": str(self.outside) + os.pathsep + os.environ["PATH"]}
        command = [sys.executable, "-c", driver, *self.args]
        with subprocess.Popen([*command, "run", "Implement greet", *self.plan], cwd=ROOT, env=env,
                              start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) as process:
            git_pid = None
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and process.poll() is None:
                    if marker.exists() and len(facts := marker.read_text().split()) == 2:
                        git_pid, git_pgid = map(int, facts)
                        break
                    time.sleep(.01)
                self.assertIsNotNone(git_pid, "Git stand-in did not enter the wait")
                self.assertEqual(git_pid, git_pgid)
                self.assertNotEqual(git_pgid, process.pid)
                os.killpg(process.pid, signal.SIGINT)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual((process.returncode, stdout), (130, ""), stderr)
                self.assertEqual(received.read_text(), "SIGTERM")
                with self.assertRaises(ProcessLookupError):
                    os.kill(git_pid, 0)
                git_pid = None
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                if git_pid is not None:
                    with suppress(ProcessLookupError):
                        os.kill(git_pid, signal.SIGKILL)

        path, = (self.repo / ".harness" / "runs").glob("*/state.json")
        saved = read_json(path)
        self.assertEqual((saved["status"], saved["stop"]["kind"], saved["stop"]["reason"]),
                         ("interrupted", "interrupted", "signal"))
        prepared, = saved["dispatches"]
        self.assertEqual((prepared["role"], prepared["invoked"], prepared["status"]), ("worker", False, "failed"))
        self.assertEqual(saved["agent_dispatches"], 0)
        self.assertEqual(saved["attempts"], {})
        # Wall-clock watchdog for the entire recovery, including all roles.
        resumed = subprocess.run([*command, "resume", saved["id"]], cwd=ROOT, env=env, timeout=60,
                                 capture_output=True, text=True)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["status"], "completed")
        completed = read_json(path)
        self.assertEqual(completed["attempts"], {"greet": 1})
        self.assertEqual([row["kind"] for row in completed["dispatches"] if row["role"] != "verification" and row["invoked"]],
                         ["implement", "review", "final_review"])
