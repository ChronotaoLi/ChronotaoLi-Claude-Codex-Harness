"""Terminal ledger results survive observation and stdout failures with an offline backend."""

import contextlib
import errno
import io
import json
import os
import signal
import subprocess
import sys
import time
from unittest.mock import patch

from harness import interrupts, quota, stops
from harness.adapters import DemoBackend
from harness.operations import OperationRecord
from harness.run_context import RunContext
from harness.run_store import RunStore
from harness.runtime import Runtime
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, write_project_config
from test_operations import OperationCase
from test_quota_sampling import synthetic_observed, two_tasks


class DemoWithQuota(DemoBackend):
    def quota_snapshot(self, provider):
        return synthetic_observed(provider)


class TerminalPublicationTests(OperationCase):
    def check_observations(self, status, event_name, args, exit_code=0):
        checkpoint, event = RunContext.checkpoint, RunContext.event
        mark, sample = quota.QuotaSampler.terminal_saved, quota.QuotaSampler.sample
        commit = RunStore.commit
        steps = ["checkpoint", "event"]
        if status != "abandoned":
            steps += ["quota marker", "quota sample"]
        for step in steps:
            for fault in (None, OSError(errno.EIO, "observation I/O")):
                with self.subTest(status=status, observation=step,
                                  fault="SIGINT" if fault is None else type(fault).__name__):
                    published, fired = [], []

                    def record(store, **kwargs):
                        revision = commit(store, **kwargs)
                        if store.run["status"] == status:
                            path = store.root / store.run["id"] / "state.json"
                            published.append((path, path.read_bytes()))
                        return revision

                    def fail():
                        if not fired:
                            fired.append(len(published))
                            if fault is None:
                                self.assertTrue(interrupts.active().terminal)
                                signal.raise_signal(signal.SIGINT)
                            else:
                                raise fault

                    def write_checkpoint(context):
                        if step == "checkpoint" and context.run["status"] == status:
                            fail()
                        return checkpoint(context)

                    def write_event(context, name, **fields):
                        if step == "event" and name == event_name:
                            fail()
                        return event(context, name, **fields)

                    def mark_terminal(sampler):
                        if step == "quota marker":
                            fail()
                        return mark(sampler)

                    def take_sample(sampler, provider, trigger, **fields):
                        if step == "quota sample" and trigger == quota.END_TRIGGERS[status]:
                            fail()
                        return sample(sampler, provider, trigger, **fields)

                    command = list(args)
                    if status == "abandoned":
                        with contextlib.redirect_stderr(io.StringIO()):
                            waiting = self.engine().start("Implement greet", pause_after_plan=True)
                        command.append(waiting["id"])
                    else:
                        operation_id = f"{status}-{step.replace(' ', '-')}-{type(fault).__name__}"
                        command += ["--operation-id", operation_id]
                    with patch.object(RunStore, "commit", record), patch.object(RunContext, "checkpoint", write_checkpoint), \
                            patch.object(RunContext, "event", write_event), \
                            patch.object(quota.QuotaSampler, "terminal_saved", mark_terminal), \
                            patch.object(quota.QuotaSampler, "sample", take_sample):
                        code, output, errors = self.invoke(command, DemoWithQuota())

                    self.assertEqual(fired, [1], errors)
                    self.assertEqual(len(published), 1, errors)
                    path, data = published[0]
                    self.assertEqual(path.read_bytes(), data)
                    saved = json.loads(data)
                    self.assertEqual((code, saved["status"]), (exit_code, status), errors)
                    if fault is None:
                        self.assertIn(f"status={status}", errors)
                    else:
                        self.assertIn("警告", errors)
                        self.assertIn(str(fault), errors)
                    self.assertEqual(len(self.events(saved["id"], event_name)), int(step != "event" or fault is None))
                    if exit_code == 0:
                        self.assertEqual(json.loads(output)["status"], status)
                    if status != "abandoned":
                        record = self.record(operation_id)
                        self.assertEqual((record["status"], record["exit_status"], record["run_id"]),
                                         ("finished", exit_code, saved["id"]))
                        self.assertEqual(record["result_status"], status if exit_code == 0 else None)
                        self.assertEqual(record["stop"], stops.project_ledger(saved))
                        if status == "blocked":
                            self.assertEqual((saved["stop"]["kind"], saved["stop"]["reason"]),
                                             ("budget", "agent_dispatches"))

    def test_completion_survives_each_observation_failure(self):
        self.check_observations("completed", "run_completed", ["run", "Implement greet"])

    def test_pause_survives_each_observation_failure(self):
        plan = self.outside / "two-tasks.json"
        write_json(plan, two_tasks())
        self.check_observations("paused", "run_paused", ["run", "Implement greet", "--plan", str(plan), "--step"])

    def test_plan_wait_survives_each_observation_failure(self):
        self.check_observations("awaiting_approval", "plan_awaiting_approval",
                                ["run", "Implement greet", "--pause-after-plan"])

    def test_stopped_run_keeps_its_budget_exit_after_each_observation_failure(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        self.check_observations("blocked", "run_stopped", ["run", "Implement greet"], exit_code=3)

    def test_signal_during_stop_settling_preserves_the_budget_stop_through_quota_cleanup(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        engine = self.engine()
        settle = engine.services.lifecycle.settle_stop
        sampler = engine.services.quota
        end_of_call = sampler.end_of_call
        observed = []

        def observe_cleanup(primary):
            observed.append((primary, sampler.final))
            end_of_call(primary)

        def signal_then_settle(error):
            signal.raise_signal(signal.SIGINT)
            # Stop's load checks have no delivery point. Inspect the staged
            # terminal candidate without introducing an artificial wait.
            self.assertFalse(interrupts.deliverable())
            self.assertIsNone(interrupts.active().delivered_from)
            return settle(error)

        with patch.object(engine.services.lifecycle, "settle_stop", signal_then_settle), \
                patch.object(sampler, "end_of_call", observe_cleanup), \
                contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet")
        saved = self.ledger(engine.run["id"])
        self.assertEqual((saved["status"], saved["stop"]["kind"], saved["stop"]["reason"]),
                         ("blocked", "budget", "agent_dispatches"))
        self.assertEqual(saved["stop"], stops.of(caught.exception))
        self.assertEqual(observed, [(caught.exception, ("blocked", saved["store_revision"]))])
        self.assertIn("status=blocked", errors.getvalue())

    def test_abandonment_survives_each_observation_failure(self):
        self.check_observations("abandoned", "run_abandoned", ["abandon"])

    def test_runtime_returns_saved_completion_after_checkpoint_interrupt(self):
        engine = self.engine()
        path_bytes = []
        checkpoint = engine.context.checkpoint

        def interrupt():
            if engine.run["status"] == "completed":
                path_bytes.append((engine.directory / "state.json").read_bytes())
                raise KeyboardInterrupt("checkpoint interrupted")
            return checkpoint()

        with patch.object(engine.context, "checkpoint", interrupt), contextlib.redirect_stderr(io.StringIO()) as errors:
            result = engine.start("Implement greet")
        self.assertEqual(path_bytes, [(engine.directory / "state.json").read_bytes()])
        self.assertEqual(result, json.loads(path_bytes[0]))
        self.assertEqual(result["status"], "completed")
        self.assertIn("checkpoint interrupted", errors.getvalue())
        self.assertEqual(len(self.events(result["id"], "run_completed")), 1)


RESULT_DRIVER = """
import os
import sys
import time
from pathlib import Path

from harness import cli, operations, run_context
from harness.adapters import DemoBackend

fault, *args = sys.argv[1:]
run_context.CLIBackend = lambda *args, **kwargs: DemoBackend()
operations.probe_cli = lambda name: {'path': None, 'version': None}

if fault == 'pipe':
    ready, release = map(Path, args[:2])
    args = args[2:]

def fail_at_stdout(*values, **kwargs):
    if kwargs.get('file', sys.stdout) is sys.stdout:
        if fault == 'closed-fd':
            os.close(sys.stdout.fileno())
        else:
            ready.touch()
            deadline = time.monotonic() + 20
            while not release.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError('parent did not close the pipe')
                time.sleep(0.01)
    print(*values, **kwargs)

if fault == 'closed-at-start':
    assert sys.stdout is None
else:
    cli.print = fail_at_stdout
raise SystemExit(cli.main(args))
"""


class ResultOutputTests(OperationCase):
    def assert_completed_record(self, operation_id, error, *, error_omitted=None):
        record = self.record(operation_id)
        self.assertEqual((record["status"], record["exit_status"], record["result_status"], record["stop"]),
                         ("finished", 0, "completed", None))
        self.assertEqual(record["error_omitted"], error_omitted)
        if error is None:
            self.assertIsNone(record["error"])
        else:
            self.assertIn(error, record["error"])
            self.assertEqual(len(record["error"].splitlines()), 1)
        self.assertEqual(self.ledger(record["run_id"])["status"], "completed")

    def test_write_failure_keeps_completion_in_the_operation_record(self):
        class BrokenOutput(io.StringIO):
            def write(self, value):
                raise BrokenPipeError("stdout write failed\nadditional detail")

        code, output, errors = self.invoke(["run", "Implement greet", "--operation-id", "write"], output=BrokenOutput())
        self.assertEqual((code, output), (0, ""), errors)
        self.assertIn("stdout write failed", errors)
        self.assert_completed_record("write", "stdout write failed")

    def test_flush_failure_keeps_completion_in_the_operation_record(self):
        class BufferedOutput(io.StringIO):
            def flush(self):
                raise BrokenPipeError("stdout flush failed")

        code, output, errors = self.invoke(["run", "Implement greet", "--operation-id", "flush"], output=BufferedOutput())
        self.assertEqual((code, json.loads(output)["status"]), (0, "completed"), errors)
        self.assertIn("stdout flush failed", errors)
        self.assert_completed_record("flush", "stdout flush failed")

    def test_closed_stdout_stream_finishes_the_record(self):
        class ClosedOutput(io.StringIO):
            def getvalue(self):
                return ""

        stream = ClosedOutput()
        stream.close()
        code, output, errors = self.invoke(["run", "Implement greet", "--operation-id", "closed-stream"],
                                           output=stream)
        self.assertEqual((code, output), (0, ""), errors)
        self.assertIn("closed file", errors)
        self.assert_completed_record("closed-stream", "closed file")

    def test_approval_projection_interrupt_keeps_saved_result_and_finishes_once(self):
        finish = OperationRecord.finish
        finished = []
        published = []
        project_ledger = stops.project_ledger

        def interrupt(ledger):
            if not published:
                path = self.state / "runs" / ledger["id"] / "state.json"
                published.append((path, path.read_bytes()))
                raise KeyboardInterrupt("approval projection interrupted")
            return project_ledger(ledger)

        def record_finish(operation, **kwargs):
            finished.append(operation.id)
            finish(operation, **kwargs)

        with patch.object(stops, "project_ledger", interrupt), patch.object(OperationRecord, "finish", record_finish):
            code, output, errors = self.invoke(["run", "Implement greet", "--pause-after-plan",
                                               "--operation-id", "approval-projection"])
        self.assertEqual((code, output), (0, ""), errors)
        self.assertIn("approval projection interrupted", errors)
        self.assertEqual(finished, ["approval-projection"])
        self.assertEqual(len(published), 1)
        path, ledger = published[0]
        self.assertEqual(path.read_bytes(), ledger)
        saved = json.loads(ledger)
        self.assertEqual(saved["status"], "awaiting_approval")
        record = self.record("approval-projection")
        self.assertEqual((record["status"], record["exit_status"], record["result_status"], record["error"]),
                         ("finished", 0, "awaiting_approval", "approval projection interrupted"))
        self.assertEqual(record["stop"], stops.project_ledger(saved))
        code, output, errors = self.invoke(["status", record["run_id"]])
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["stop"], record["stop"])
        code, output, errors = self.invoke(["resume", record["run_id"], "--operation-id", "approval-reported"])
        self.assertEqual((code, json.loads(output)["status"]), (0, "awaiting_approval"), errors)
        self.assertEqual(self.record("approval-reported")["stop"], record["stop"])
        self.assertEqual(path.read_bytes(), ledger)

    def test_abandon_projection_interrupt_keeps_saved_result(self):
        code, output, errors = self.invoke(["run", "Implement greet", "--pause-after-plan"])
        self.assertEqual(code, 0, errors)
        run_id = json.loads(output)["id"]
        path = self.state / "runs" / run_id / "state.json"
        published = []

        def interrupt(ledger):
            self.assertEqual(ledger["status"], "abandoned")
            published.append(path.read_bytes())
            raise KeyboardInterrupt("abandon projection interrupted")

        with patch("harness.cli.status_summary", interrupt):
            code, output, errors = self.invoke(["abandon", run_id])
        self.assertEqual((code, output), (0, ""), errors)
        self.assertIn("abandon projection interrupted", errors)
        self.assertEqual(published, [path.read_bytes()])
        self.assertEqual(self.ledger(run_id)["status"], "abandoned")

    def test_stdout_encoding_failure_finishes_the_record_with_existing_redaction(self):
        repo = self.outside / "项目"
        self.repo.rename(repo)
        self.repo, self.state = repo, repo / ".harness"
        self.operations = self.state / "operations"
        plan = self.outside / "plan.json"
        write_json(plan, read_json(ROOT / "examples" / "plan.json"))

        class ASCIIOutput(io.TextIOWrapper):
            def getvalue(self):
                return self.buffer.getvalue().decode("ascii")

        for operation_id, options in (("ascii", []), ("ascii-with-plan", ["--plan", str(plan)])):
            with self.subTest(operation_id=operation_id), \
                    ASCIIOutput(io.BytesIO(), encoding="ascii", errors="strict") as stream:
                code, output, errors = self.invoke(["run", "Implement greet", "--operation-id", operation_id, *options],
                                                   output=stream)
                self.assertEqual((code, output), (0, ""), errors)
                self.assertIn("结果输出失败", errors)
                self.assertIn("'ascii' codec can't encode", errors)
                if options:
                    self.assert_completed_record(operation_id, None, error_omitted="file inputs present; see stderr")
                else:
                    self.assert_completed_record(operation_id, "'ascii' codec can't encode")

    def test_approval_stop_notice_interrupt_keeps_saved_result_and_finishes_the_record(self):
        state = self.state
        attempted = []

        class InterruptedNotice(io.StringIO):
            def write(self, value):
                if value.startswith("[harness-json] "):
                    body = json.loads(value.split(" ", 1)[1])
                    if body["event"] == "stop":
                        path = state / "runs" / body["run_id"] / "state.json"
                        attempted.append((path, path.read_bytes()))
                        raise KeyboardInterrupt("approval notice interrupted")
                return super().write(value)

        code, output, errors = self.invoke(["run", "Implement greet", "--pause-after-plan",
                                            "--operation-id", "approval-notice"], errors=InterruptedNotice())
        result = json.loads(output)
        self.assertEqual((code, result["status"]), (0, "awaiting_approval"), errors)
        self.assertEqual(len(attempted), 1)
        path, published = attempted[0]
        self.assertEqual(path.read_bytes(), published)
        saved = json.loads(published)
        self.assertEqual((saved["status"], saved["id"]), ("awaiting_approval", result["id"]))
        self.assertIn("终态已保存", errors)
        self.assertIn("approval notice interrupted", errors)
        record = self.record("approval-notice")
        self.assertEqual((record["status"], record["exit_status"], record["result_status"], record["run_id"]),
                         ("finished", 0, "awaiting_approval", saved["id"]))
        self.assertEqual(record["stop"], stops.project_ledger(saved))

    def test_budget_stop_report_interrupt_keeps_saved_stop_and_finishes_once(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        finish = OperationRecord.finish
        for channel in ("diagnostic", "notice"):
            with self.subTest(channel=channel):
                attempted, finished = [], []
                operation_id = f"budget-{channel}"
                operation_path = self.operations / f"{operation_id}.json"
                runs = self.state / "runs"

                class InterruptedReport(io.StringIO):
                    def write(self, value):
                        if channel == "diagnostic":
                            interrupt = value.startswith("harness: ")
                        else:
                            interrupt = (value.startswith("[harness-json] ")
                                         and json.loads(value.split(" ", 1)[1])["event"] == "stop")
                        if interrupt:
                            run_id = read_json(operation_path)["run_id"]
                            path = runs / run_id / "state.json"
                            attempted.append((path, path.read_bytes()))
                            if len(attempted) == 1:
                                raise KeyboardInterrupt("budget report interrupted")
                        return super().write(value)

                def record_finish(operation, **kwargs):
                    finished.append(operation.id)
                    finish(operation, **kwargs)

                with patch.object(OperationRecord, "finish", record_finish):
                    code, output, errors = self.invoke(["run", "Implement greet", "--operation-id", operation_id],
                                                       errors=InterruptedReport())
                self.assertEqual((code, output), (3, ""), errors)
                self.assertIn("budget report interrupted", errors)
                self.assertEqual(finished, [operation_id])
                self.assertEqual(len(attempted), 1)
                path, published = attempted[0]
                self.assertEqual(path.read_bytes(), published)
                saved = json.loads(published)
                self.assertEqual((saved["status"], saved["stop"]["kind"], saved["stop"]["reason"]),
                                 ("blocked", "budget", "agent_dispatches"))
                record = self.record(operation_id)
                self.assertEqual((record["status"], record["exit_status"], record["result_status"], record["run_id"]),
                                 ("finished", 3, None, saved["id"]))
                self.assertEqual(record["stop"], stops.project_ledger(saved))
                self.assertEqual(record["error"], "budget report interrupted")

    def test_budget_stop_classification_and_projection_keep_the_saved_exit(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        start, finish = Runtime.start, OperationRecord.finish
        plan = self.outside / "plan.json"
        write_json(plan, read_json(ROOT / "examples" / "plan.json"))
        for stage, file_input in (("of", False), ("project", False), ("project", True)):
            with self.subTest(stage=stage, file_input=file_input):
                published, interrupted, finished = [], [], []
                operation_id = f"budget-{stage}" + ("-file" if file_input else "")
                args = ["run", "Implement greet", "--operation-id", operation_id]
                if file_input:
                    args += ["--plan", str(plan)]
                original = getattr(stops, stage)

                def capture_stop(runtime, *args, **kwargs):
                    try:
                        return start(runtime, *args, **kwargs)
                    except HarnessError:
                        path = runtime.directory / "state.json"
                        published.append((path, path.read_bytes()))
                        raise

                def interrupt(*args, **kwargs):
                    if published and not interrupted:
                        interrupted.append(stage)
                        raise KeyboardInterrupt(f"budget {stage} interrupted")
                    return original(*args, **kwargs)

                def record_finish(operation, **kwargs):
                    finished.append(operation.id)
                    finish(operation, **kwargs)

                with patch.object(Runtime, "start", capture_stop), patch.object(stops, stage, interrupt), \
                        patch.object(OperationRecord, "finish", record_finish):
                    code, output, errors = self.invoke(args)
                self.assertEqual((code, output), (3, ""), errors)
                self.assertEqual(finished, [operation_id])
                self.assertEqual(len(published), 1)
                path, ledger = published[0]
                self.assertEqual(path.read_bytes(), ledger)
                saved = json.loads(ledger)
                self.assertEqual((saved["status"], saved["stop"]["kind"], saved["stop"]["reason"]),
                                 ("blocked", "budget", "agent_dispatches"))
                record = self.record(operation_id)
                self.assertEqual((record["status"], record["exit_status"], record["run_id"]),
                                 ("finished", 3, saved["id"]))
                self.assertEqual(record["stop"], stops.project_ledger(saved, omit_detail=file_input))
                if stage == "project":
                    self.assertEqual(interrupted, [stage])
                    self.assertIn(f"budget {stage} interrupted", errors)
                    if file_input:
                        self.assertEqual((record["error"], record["error_omitted"]),
                                         (None, "file inputs present; see stderr"))
                    else:
                        self.assertEqual(record["error"], f"budget {stage} interrupted")

    def test_resume_output_interrupt_keeps_completion_in_the_operation_record(self):
        code, output, errors = self.invoke(["run", "Implement greet", "--pause-after-plan"])
        self.assertEqual(code, 0, errors)
        run_id = json.loads(output)["id"]

        class InterruptedOutput(io.StringIO):
            def flush(self):
                raise KeyboardInterrupt("stdout flush interrupted")

        code, output, errors = self.invoke(["resume", run_id, "--approve-plan", "--operation-id", "resume"],
                                           output=InterruptedOutput())
        self.assertEqual((code, json.loads(output)["status"]), (0, "completed"), errors)
        self.assertIn("stdout flush interrupted", errors)
        self.assert_completed_record("resume", "stdout flush interrupted")

    def run_with_closed_pipe(self, *args):
        ready, release = self.outside / "printing", self.outside / "pipe_closed"
        env = dict(os.environ)
        env.pop("PYTHONUNBUFFERED", None)
        driver_args = [sys.executable, "-c", RESULT_DRIVER, "pipe", str(ready), str(release),
                       "--repo", str(self.repo), *args]
        with (self.outside / "stderr.txt").open("w+") as stderr, \
                subprocess.Popen(driver_args, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=stderr) as process:
            try:
                deadline = time.monotonic() + 20
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                process.stdout.close()
                release.touch()
                code = process.wait(timeout=20)
            finally:
                if process.poll() is None:
                    process.kill()
            stderr.seek(0)
            errors = stderr.read()
        self.assertTrue(ready.exists(), errors)
        self.assertEqual(code, 0, errors)
        self.assertIn("结果输出失败", errors)
        self.assertIn(os.strerror(errno.EPIPE), errors)

    def test_real_broken_pipe_preserves_run_exit_and_record(self):
        write_project_config(self.repo)
        self.run_with_closed_pipe("run", "Implement greet", "--operation-id", "pipe")
        self.assert_completed_record("pipe", os.strerror(errno.EPIPE))

    def test_closed_stdout_descriptor_preserves_run_exit_and_record(self):
        write_project_config(self.repo)
        env = dict(os.environ)
        env.pop("PYTHONUNBUFFERED", None)
        result = subprocess.run([sys.executable, "-c", RESULT_DRIVER, "closed-fd", "--repo", str(self.repo),
                                 "run", "Implement greet", "--operation-id", "closed-stdout"],
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertIn("结果输出失败", result.stderr)
        self.assertIn(os.strerror(errno.EBADF), result.stderr)
        self.assert_completed_record("closed-stdout", os.strerror(errno.EBADF))

    def test_stdout_closed_at_process_start_preserves_run_and_abandon(self):
        write_project_config(self.repo)

        def invoke(*args):
            return subprocess.run(
                ["/bin/sh", "-c", 'exec "$@" 1>&-', "closed-stdout",
                 sys.executable, "-c", RESULT_DRIVER, "closed-at-start", "--repo", str(self.repo), *args],
                cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)

        result = invoke("run", "Implement greet", "--operation-id", "closed-at-start")
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertIn("stdout is unavailable", result.stderr)
        self.assert_completed_record("closed-at-start", "stdout is unavailable")

        code, output, errors = self.invoke(["run", "Implement greet", "--pause-after-plan"])
        self.assertEqual(code, 0, errors)
        run_id = json.loads(output)["id"]
        result = invoke("abandon", run_id)
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertIn("stdout is unavailable", result.stderr)
        self.assertEqual(self.ledger(run_id)["status"], "abandoned")

    def test_real_broken_pipe_preserves_abandon_exit(self):
        code, output, errors = self.invoke(["run", "Implement greet", "--pause-after-plan"])
        self.assertEqual(code, 0, errors)
        run_id = json.loads(output)["id"]
        self.run_with_closed_pipe("abandon", run_id)
        self.assertEqual(self.ledger(run_id)["status"], "abandoned")
