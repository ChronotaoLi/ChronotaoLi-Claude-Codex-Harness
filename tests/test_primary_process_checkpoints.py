import json
import os
import signal
import subprocess
import sys
import time
from unittest.mock import patch

from test_harness import ROOT, RepoCase, run_cli
from harness import adapters, interrupts, stops, verification
from harness.adapters import CLIBackend
from harness.util import HarnessError, read_json


SIGNAL_AND_WAIT = "import os, signal, time; os.kill(os.getppid(), signal.SIGINT); time.sleep(30)"


class OfflineModel(CLIBackend):
    def quota_snapshot(self, provider):
        return None

    def command(self, role, model, cwd, job, schema, run_path):
        return self.argv


class PrimaryProcessCheckpointTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.config.data["runtime"].update(agent_timeout_seconds=4, verify_timeout_seconds=4)

    def assert_cancelled(self, engine, role, *, started):
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["stop"]["kind"], "interrupted")
        self.assertTrue(saved["stop"]["data"]["exits_confirmed"])
        entry, = (item for item in saved["dispatches"] if item["role"] == role)
        self.assertEqual(entry["termination_reason"], "cancelled")
        self.assertTrue(entry["exit_confirmed"])
        self.assertIs(entry["process_outcome"]["started"], started)
        if started:
            with self.assertRaises(ProcessLookupError):
                os.killpg(entry["pgid"], 0)
        else:
            self.assertEqual(entry["process_outcome"]["startup_state"], "not_started")
            self.assertIsNone(entry["pgid"])
        return saved, entry

    def test_model_wait_signal_stops_and_confirms_owned_group_exit(self):
        backend = OfflineModel(self.config, self.repo / ".harness")
        backend.argv = [sys.executable, "-c", SIGNAL_AND_WAIT]
        engine = self.engine(backend)
        before = time.monotonic()
        with patch.object(interrupts, "ENABLED", True), interrupts.installed():
            controller = interrupts.active()
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Plan offline")
            self.assertIsNotNone(controller.delivered_from)
        self.assertLess(time.monotonic() - before, 2.5)
        saved, entry = self.assert_cancelled(engine, "commander", started=True)
        self.assertEqual(saved["agent_dispatches"], 1)
        self.assertTrue(entry["invoked"])

    def test_repeated_model_wait_signal_finishes_cli_operation(self):
        backend = OfflineModel(self.config, self.repo / ".harness")
        script = "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); " + SIGNAL_AND_WAIT
        backend.argv = [sys.executable, "-c", script]
        killpg = os.killpg
        sent = []
        repeated_at = None

        def signal_group(pgid, sig):
            nonlocal repeated_at
            at = time.monotonic()
            killpg(pgid, sig)
            if sig:
                sent.append((pgid, sig, at))
            if sig == signal.SIGTERM:
                # The child has already installed SIG_IGN and sent the first
                # interrupt. Request hurry only after real cleanup starts.
                repeated_at = time.monotonic()
                os.kill(os.getpid(), signal.SIGHUP)

        operation_id = "repeat-model-wait"
        with patch.object(interrupts, "ENABLED", True), patch.object(os, "killpg", signal_group):
            code, stderr = run_cli(self, backend, ["run", "Plan offline", "--operation-id", operation_id])

        self.assertEqual(code, 130)
        state = self.repo / ".harness"
        operation = read_json(state / "operations" / f"{operation_id}.json")
        directory = state / "runs" / operation["run_id"]
        saved = read_json(directory / "state.json")
        self.assertEqual((saved["status"], saved["stop"]["kind"]), ("interrupted", "interrupted"))
        self.assertTrue(saved["stop"]["data"]["exits_confirmed"])
        self.assertEqual((operation["status"], operation["exit_status"]), ("finished", code))
        self.assertEqual(operation["stop"]["kind"], "interrupted")
        self.assertTrue(operation["stop"]["data"]["exits_confirmed"])
        repeat_lines = [line for line in stderr.splitlines() if "已收到第 2 次中断" in line]
        self.assertEqual(len(repeat_lines), 1)
        entry, = saved["dispatches"]
        self.assertEqual(saved["agent_dispatches"], 1)
        self.assertTrue(entry["invoked"])
        self.assertTrue(entry["exit_confirmed"])
        self.assertEqual(entry["termination_reason"], "cancelled")
        term, killed = sent
        self.assertEqual([p[:2] for p in sent], [(entry["pgid"], signal.SIGTERM),
                                                  (entry["pgid"], signal.SIGKILL)])
        self.assertLessEqual(term[2], repeated_at)
        self.assertLess(killed[2] - repeated_at, .1)
        outcome = read_json(directory / "dispatches" / entry["id"] / "process.outcome.json")
        self.assertEqual(outcome["termination_reason"], "cancelled")
        self.assertEqual(outcome["exit_code"], -signal.SIGKILL)
        self.assertTrue(outcome["evidence_complete"])
        with self.assertRaises(ProcessLookupError):
            killpg(entry["pgid"], 0)

    def test_model_prelaunch_signal_keeps_charge_but_starts_no_process(self):
        backend = OfflineModel(self.config, self.repo / ".harness")
        witness = self.outside / "model-started"
        backend.argv = [sys.executable, "-c", f"from pathlib import Path; Path({str(witness)!r}).touch()"]
        engine = self.engine(backend)

        def command(*args):
            self.assertTrue(engine.context.store.matches_commit())
            os.kill(os.getpid(), signal.SIGINT)
            return backend.argv

        with patch.object(interrupts, "ENABLED", True), patch.object(backend, "command", command):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Plan offline")
        saved, entry = self.assert_cancelled(engine, "commander", started=False)
        self.assertEqual(saved["agent_dispatches"], 1)
        self.assertTrue(entry["invoked"])
        self.assertFalse(witness.exists())

    def test_verification_wait_signal_uses_existing_bound_record_and_receipt(self):
        self.exercise_verification_wait(signal_before_launch=False)

    def test_verification_prelaunch_interrupt_preserves_registration_without_child(self):
        self.exercise_verification_wait(signal_before_launch=True)

    def exercise_verification_wait(self, *, signal_before_launch):
        script = "import time; time.sleep(30)" if signal_before_launch else SIGNAL_AND_WAIT
        command = {"cwd": ".", "argv": [sys.executable, "-c", script]}
        self.config.data["verification"]["commands"] = [command]
        engine = self.engine()
        run_process = verification.run_process

        def start_check(*args, **kwargs):
            if signal_before_launch:
                self.assertTrue(engine.context.store.matches_commit())
                registered = read_json(engine.directory / "state.json")["dispatches"][-1]
                self.assertEqual(registered["process_state"], "starting")
                self.assertIn("started_at", registered)
                os.kill(os.getpid(), signal.SIGINT)
                self.assertTrue(interrupts.deliverable())
            on_start = kwargs["on_start"]

            def after_start(pgid):
                on_start(pgid)
                self.assertTrue(engine.context.store.matches_commit())
                self.assertFalse(signal_before_launch, "verification started after its prelaunch checkpoint")

            kwargs["on_start"] = after_start
            return run_process(*args, **kwargs)

        with patch.object(interrupts, "ENABLED", True), interrupts.installed(), \
                patch.object(verification, "run_process", start_check):
            controller = interrupts.active()
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
            self.assertIsNotNone(controller.delivered_from)
        _, entry = self.assert_cancelled(engine, "verification", started=not signal_before_launch)
        process_record = engine.directory / entry["process_record"]
        record = read_json(process_record)
        self.assertEqual(record["owner"], entry["id"])
        self.assertEqual(record["termination_reason"], "cancelled")
        self.assertEqual(record["pgid"], entry["pgid"])
        row, = read_json(process_record.parent / "results.json")
        self.assertEqual((row["command"], row["status"], row["termination_reason"]),
                         (command, "interrupted", "cancelled"))
        self.assertEqual(row["started_at"], entry["started_at"])

    def test_model_completion_wins_over_pending_signal_after_timed_out_slice(self):
        backend = OfflineModel(self.config, self.repo / ".harness")
        job = backend.state / "runs" / "run_offline" / "dispatches" / "call"
        job.mkdir(parents=True)
        result_path = job / "response.json"
        payload = {"offline": True}
        script = (f"from pathlib import Path; Path({str(result_path)!r}).write_text({json.dumps(payload)!r}); "
                  "import os, signal; os.kill(os.getppid(), signal.SIGINT)")
        backend.argv = [sys.executable, "-c", script]
        wait = subprocess.Popen.wait

        def timed_out_slice(process, timeout=None):
            # Force the narrow timing deterministically with a real successful
            # child, then let run_process observe the exit through poll().
            wait(process, timeout=2)
            raise subprocess.TimeoutExpired(process.args, timeout)

        with patch.object(interrupts, "ENABLED", True), interrupts.installed(), \
                patch.object(subprocess.Popen, "wait", timed_out_slice):
            result = backend.invoke(role="commander", model={"cli": "codex"}, cwd=self.repo,
                                    job=job, schema={}, prompt="", run_path=None, on_start=None)
            self.assertEqual(result, payload)
            self.assertEqual(read_json(job / "process.outcome.json")["termination_reason"], "normal")
            self.assertIsNone(interrupts.active().delivered_from)
            with self.assertRaises(KeyboardInterrupt):
                interrupts.deliver()

    def test_disabled_controller_preserves_verification_and_model_success(self):
        disabled = patch.object(interrupts, "ENABLED", False)
        disabled.start()
        self.addCleanup(disabled.stop)
        backend = OfflineModel(self.config, self.repo / ".harness")
        job = backend.state / "runs" / "run_offline" / "dispatches" / "call"
        job.mkdir(parents=True)
        result_path = job / "response.json"
        backend.argv = [sys.executable, "-c", f"from pathlib import Path; Path({str(result_path)!r}).write_text('{{}}')"]
        invoke_options = dict(role="commander", model={"cli": "codex"}, cwd=self.repo,
                              job=job, schema={}, prompt="", run_path=None, on_start=None)
        with interrupts.installed():
            self.assertIsNone(interrupts.active())
            with interrupts.publishing(), self.assertRaises(HarnessError) as refused:
                backend.invoke(**invoke_options)
            stop = stops.of(refused.exception)
            self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "invariant"))
            self.assertEqual(list(job.iterdir()), [])

            with patch.object(adapters, "run_process", wraps=adapters.run_process) as model_process:
                self.assertEqual(backend.invoke(**invoke_options), {})
            model_process.assert_called_once()
            self.assertIsNone(model_process.call_args.kwargs["checkpoint"])
            model_record = read_json(job / "process.outcome.json")
            self.assertTrue(model_record["started"])
            self.assertTrue(model_record["exit_confirmed"])
            self.assertEqual((model_record["termination_reason"], model_record["exit_code"]), ("normal", 0))

            engine = self.engine()
            self.config.data["verification"]["commands"] = [
                {"cwd": ".", "argv": [sys.executable, "-c", "print('ordinary verification')"]}]
            with patch.object(verification, "run_process", wraps=verification.run_process) as verify_process:
                saved = engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))

        self.assertEqual(saved["status"], "completed")
        entries = [entry for entry in saved["dispatches"] if entry["role"] == "verification"]
        self.assertTrue(entries)
        self.assertEqual(verify_process.call_count, len(entries))
        for call, entry in zip(verify_process.call_args_list, entries):
            self.assertIsNone(call.kwargs["checkpoint"])
            record = read_json(engine.directory / entry["process_record"])
            self.assertEqual(record["owner"], entry["id"])
            self.assertTrue(record["started"])
            self.assertTrue(record["exit_confirmed"])
            self.assertEqual((record["termination_reason"], record["exit_code"]), ("normal", 0))
