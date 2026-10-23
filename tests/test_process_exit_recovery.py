import contextlib
import os
import signal
import subprocess
import sys
from unittest.mock import patch

from harness.adapters import CLIBackend
from harness.cleanup import cleanup_verification
from harness.util import HarnessError, group_alive, read_json, write_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend


class ProcessExitRecoveryTests(RepoCase):
    def test_unknown_startup_blocks_both_resume_and_cleanup_without_dispatch(self):
        engine = self.engine()
        run = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"), pause_after_plan=True)
        entry = {"id": "unknown", "role": "worker", "status": "running", "pgid": None}
        run["dispatches"].append(entry)
        backend = ScriptedBackend()
        for process_state in (None, "starting", "running", "exited"):
            with self.subTest(process_state=process_state):
                if process_state is None:
                    entry.pop("process_state", None)
                else:
                    entry["process_state"] = process_state
                write_json(engine.directory / "state.json", run)
                before = (engine.directory / "state.json").read_bytes()
                with self.assertRaisesRegex(HarnessError, "无法确认.*unknown"):
                    self.engine(backend).resume(run["id"], approve_plan=True)
                with self.assertRaisesRegex(HarnessError, "无法确认.*unknown"):
                    cleanup_verification(self.repo, run["id"], remove=True)
                self.assertEqual((engine.directory / "state.json").read_bytes(), before)
                self.assertEqual(backend.calls, [])
        result = self.engine(backend).resume(run["id"], approve_plan=True, confirm_exited=[entry["id"]])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_attempts"], 1)
        self.assertTrue(result["dispatches"][0]["exit_confirmed"])

    def test_pre_agent_hook_can_check_exit_gate_before_process_start(self):
        engine = self.engine()
        script = ("import json,sys; from pathlib import Path; "
                  "sys.path.insert(0,sys.argv[1]); "
                  "from harness.util import ensure_dispatches_exited,read_json; "
                  "data=json.load(sys.stdin); "
                  "run=read_json(Path(data['run_dir'])/'state.json'); "
                  "assert run['dispatches'][-1]['process_state']=='prepared'; "
                  "ensure_dispatches_exited(run['dispatches'])")
        engine.hooks.spec = {"pre_agent": [{"argv": [sys.executable, "-c", script, str(ROOT)]}]}
        result = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.assertEqual(result["status"], "completed")
        self.assertTrue(all(entry["exit_confirmed"] for entry in result["dispatches"]))
        self.assertTrue(all(entry["process_state"] == "exited" for entry in result["dispatches"]))

    def test_controller_death_after_real_popen_before_pgid_save_stays_unknown(self):
        # A separate controller exits without running finally blocks immediately
        # after a real local subprocess starts. No provider account is involved.
        script = '''
import json, os, subprocess, sys
from pathlib import Path
from harness.adapters import CLIBackend, DemoBackend
from harness.config import Config
from harness.engine import Engine
from harness.util import read_json
root, repo, mode, witness = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
config = Config.load(root / "harness.toml")
command = [sys.executable, "-c", "import time; time.sleep(60)", "startup-gap"]
config.data["verification"]["commands"] = ([command] if mode == "verification" else
    [[sys.executable, "-m", "unittest", "-v"]])
backend = CLIBackend(config, repo / ".harness") if mode == "worker" else DemoBackend()
if mode == "worker":
    backend.command = lambda *args: command
engine = Engine(config, repo, backend=backend)
original_popen = subprocess.Popen
def die_after_start(argv, *args, **kwargs):
    process = original_popen(argv, *args, **kwargs)
    if argv == command:
        witness.write_text(json.dumps({"pid": process.pid, "run_id": engine.run["id"]}))
        os._exit(83)
    return process
subprocess.Popen = die_after_start
engine.start("Implement greet", read_json(root / "examples/plan.json"))
'''
        for mode in ("worker", "verification"):
            with self.subTest(mode=mode):
                witness = self.repo / ".harness" / f"startup-{mode}.json"
                process = subprocess.run([sys.executable, "-c", script, str(ROOT), str(self.repo), mode,
                                          str(witness)], cwd=ROOT, capture_output=True, text=True, timeout=30)
                self.assertEqual(process.returncode, 83, process.stderr)
                launched = read_json(witness)
                pgid = launched["pid"]
                try:
                    self.assertTrue(group_alive(pgid))
                    directory = self.repo / ".harness" / "runs" / launched["run_id"]
                    run = read_json(directory / "state.json")
                    entry = run["dispatches"][-1]
                    self.assertEqual(entry["role"], mode)
                    self.assertEqual(entry["process_state"], "starting")
                    self.assertIsNone(entry["pgid"])
                    self.assertFalse(entry.get("exit_confirmed", False))
                    resumed = self.engine()
                    # Gate directly as verification mode intentionally uses a
                    # different fixture command from this test's configuration.
                    resumed.load_run(run["id"])
                    with self.assertRaisesRegex(HarnessError, "无法确认"):
                        resumed.ensure_exited()
                    with self.assertRaisesRegex(HarnessError, "无法确认"):
                        cleanup_verification(self.repo, run["id"], remove=True)
                finally:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(pgid, signal.SIGKILL)

    def test_interrupted_popen_return_does_not_claim_unknown_child_exited(self):
        command = [sys.executable, "-c", "import time; time.sleep(60)", "interrupt-startup"]
        original_popen = subprocess.Popen
        for mode in ("worker", "verification"):
            with self.subTest(mode=mode):
                started = []

                def interrupted_start(argv, *args, **kwargs):
                    process = original_popen(argv, *args, **kwargs)
                    if argv == command:
                        started.append(process)
                        raise KeyboardInterrupt("interrupted before Popen returned")
                    return process

                if mode == "worker":
                    backend = CLIBackend(self.config, self.repo / ".harness")
                    backend.command = lambda *args: command
                    engine = self.engine(backend)
                else:
                    self.config.data["verification"]["commands"] = [command]
                    engine = self.engine()
                try:
                    with patch("harness.util.subprocess.Popen", side_effect=interrupted_start), \
                            self.assertRaises(KeyboardInterrupt):
                        engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
                    self.assertEqual(len(started), 1)
                    self.assertTrue(group_alive(started[0].pid))
                    run = read_json(engine.directory / "state.json")
                    entry = run["dispatches"][-1]
                    self.assertEqual(entry["process_state"], "starting")
                    self.assertIsNone(entry["pgid"])
                    self.assertFalse(entry["exit_confirmed"])
                    with self.assertRaisesRegex(HarnessError, "无法确认"):
                        self.engine().resume(run["id"])
                    with self.assertRaisesRegex(HarnessError, "无法确认"):
                        cleanup_verification(self.repo, run["id"])
                finally:
                    for process in started:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        if process.stdin:
                            process.stdin.close()
