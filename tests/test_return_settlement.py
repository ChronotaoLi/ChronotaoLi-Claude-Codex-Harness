"""Recover designated provider bytes after the invoking runtime dies."""
from contextlib import nullcontext
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from unittest.mock import patch

from harness import git, state_files, stops
from harness.adapters import CLIBackend, DemoBackend
from harness.planning import DECIDE_KEY
from harness.util import HarnessError, ProcessTimeout, json_bytes, read_json, write_json
from test_harness import ROOT, RepoCase
from test_readonly_settlement import TerminalCLI, stop_pair, tree_bytes
from test_state_files import FakeOs


KEYS = {"decide": DECIDE_KEY, "review": "reviewer:review:greet",
        "final_review": "reviewer:final_review:final"}


class LocalProvider(DemoBackend):
    """Use the real adapter and supervisor for one kind, with scripted local output."""

    def __init__(self, config, repo, log, kind, provider, outputs, *, change_head=False):
        self.config, self.state, self.log = config, repo / ".harness", log
        self.kind, self.provider, self.outputs = kind, provider, outputs
        self.change_head = change_head
        role = "commander" if kind == "decide" else "reviewer"
        config.data["roles"][role]["cli"] = provider

    def calls(self, kind=None):
        calls = [tuple(json.loads(line)) for line in self.log.read_text().splitlines()]
        return [Path(job) for called, job in calls if kind is None or called == kind]

    def invoke(self, **kwargs):
        job, kind = kwargs["job"], kwargs["kind"]
        with self.log.open("a") as stream:
            stream.write(json.dumps([kind, str(job)]) + "\n")
        if kind != self.kind:
            return super().invoke(**kwargs)
        count = len(self.calls(kind))
        output = self.outputs[min(count, len(self.outputs)) - 1]
        with tempfile.TemporaryDirectory() as temp:
            value = super().invoke(**{**kwargs, "job": Path(temp)})
        if output == "ask":
            value.update(action="ask", reason="Which interface should the task keep?", new_tasks=[])
        if output == "request_changes":
            value.update(verdict="request_changes")
        if self.change_head:
            git.git(kwargs["cwd"], "-c", "user.name=Test", "-c", "user.email=test@localhost",
                    "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Unexpected change")
        if isinstance(output, bytes) or output is None:
            raw = output
        else:
            wire = {"type": "result", "structured_output": value} if self.provider == "claude" else value
            raw = json_bytes(wire)
        if self.provider == "claude":
            program = f"import sys; sys.stdout.buffer.write({raw!r})"
        elif raw is None:
            program = "pass"
        else:
            program = f"from pathlib import Path; Path({str(job / 'response.json')!r}).write_bytes({raw!r})"
        cli = CLIBackend(self.config, self.state)
        cli.command = lambda *args: [sys.executable, "-c", program]
        return cli.invoke(**kwargs)


class ReturnSettlementTests(RepoCase):
    def provider(self, kind, provider, outputs, **kwargs):
        with tempfile.NamedTemporaryFile(dir=self.outside, delete=False) as log:
            return LocalProvider(self.config, self.repo, Path(log.name), kind, provider, outputs, **kwargs)

    def start(self, backend, *, pause=False):
        plan = None if backend.kind == "decide" else read_json(ROOT / "examples" / "plan.json")
        return self.engine(backend).start("Implement greet", plan, pause_after_plan=pause)

    def killed(self, action, window):
        # Separate process so SIGKILL leaves only writes that actually reached disk.
        pid = os.fork()
        if not pid:
            try:
                with window:
                    action()
            except BaseException:
                import traceback
                traceback.print_exc()
                os._exit(2)
            os._exit(3)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFSIGNALED(status), status)
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)

    def kill_before_result(self, backend, *, call=1, pause=False):
        complete = CLIBackend.completed_result

        def interrupt(cli, job, provider, raw=None):
            calls = backend.calls(backend.kind)
            if len(calls) == call and job == calls[-1]:
                os.kill(os.getpid(), signal.SIGKILL)
            return complete(cli, job, provider, raw)

        self.killed(lambda: self.start(backend, pause=pause),
                    patch.object(CLIBackend, "completed_result", interrupt))
        job = backend.calls(backend.kind)[-1]
        saved = read_json(job.parents[1] / "state.json")
        self.assertTrue(read_json(job / "process.outcome.json")["evidence_complete"])
        self.assertFalse((job / "returned-result.json").exists())
        return self.loaded(backend, saved["id"]), job

    def loaded(self, backend, ident):
        runtime = self.engine(backend)
        runtime.load_run(ident)
        return runtime

    def settled(self, action, expected=None):
        try:
            return action()
        except HarnessError as error:
            self.assertIsNotNone(expected, error)
            self.assertEqual(stop_pair(error), expected)

    def saved(self, runtime):
        return read_json(runtime.directory / "state.json")

    def test_real_kill_recovers_all_four_results_without_a_new_call(self):
        cases = [
            ("decide", "codex", "ask", False, ("operator_decision", "answer_required")),
            ("decide", "claude", "revise", True, None),
            ("review", "codex", "request_changes", False, ("budget", "agent_dispatches")),
            ("final_review", "codex", "approve", False, None),
        ]
        for kind, provider, verdict, pause, stop in cases:
            with self.subTest(kind=kind, provider=provider, verdict=verdict):
                self.config.data["runtime"]["max_agent_dispatches"] = 2 if kind == "review" else 8
                baseline = self.provider(kind, provider, [verdict])
                self.settled(lambda: self.start(baseline, pause=pause), stop)
                baseline_job = baseline.calls(kind)[0]
                original = (baseline_job / "returned-result.json").read_bytes()
                backend = self.provider(kind, provider, [verdict])
                runtime, job = self.kill_before_result(backend, pause=pause)
                calls = backend.calls()
                self.settled(lambda: runtime.resume(runtime.run["id"]), stop)
                self.assertEqual(backend.calls(), calls)
                expected = original.replace(baseline_job.name.encode(), job.name.encode())
                self.assertEqual((job / "returned-result.json").read_bytes(), expected)
                if provider == "claude":
                    for name in ("provider-result.json", "response.json"):
                        expected = (baseline_job / name).read_bytes().replace(baseline_job.name.encode(), job.name.encode())
                        self.assertEqual((job / name).read_bytes(), expected)
                saved = self.saved(runtime)
                if verdict == "ask":
                    self.assertEqual(saved["decision_requests"][0]["source"]["dispatch_id"], job.name)
                    self.assertEqual(saved["commander_receipts"], {})
                elif verdict == "revise":
                    self.assertEqual(len(saved["plan_history"]), 1)
                    plan = read_json(Path(saved["plan_history"][0]["artifact"]))
                    self.assertEqual(plan["source"]["dispatch_id"], job.name)
                elif verdict == "request_changes":
                    review = saved["review_history"]["greet"][0]
                    self.assertEqual((review["dispatch_id"], review["review"]["verdict"]), (job.name, verdict))
                else:
                    self.assertEqual(saved["status"], "completed")
                    source = saved["final_history"][0]["review_source"]
                    self.assertEqual(source["dispatch_id"], job.name)

    def test_invalid_output_replays_the_same_single_redo_as_a_live_call(self):
        for provider, raw in (("codex", b""), ("claude", b'{"type":"result","result":"broken"}')):
            for kill_at in (1, 2):
                with self.subTest(provider=provider, kill_at=kill_at):
                    baseline = self.provider("review", provider, [raw, raw])
                    self.settled(lambda: self.start(baseline), ("invalid_output", "redo_exhausted"))
                    backend = self.provider("review", provider, [raw, raw])
                    runtime, job = self.kill_before_result(backend, call=kill_at)
                    self.settled(lambda: runtime.resume(runtime.run["id"]), ("invalid_output", "redo_exhausted"))
                    jobs = backend.calls("review")
                    self.assertEqual(len(jobs), len(baseline.calls("review")))
                    saved = self.saved(runtime)
                    self.assertEqual(saved["review_receipts"], {})
                    first, redo = [d for d in saved["dispatches"] if d["role"] == "reviewer"]
                    self.assertNotIn("retry_of", first)
                    self.assertEqual(redo["retry_of"], first["id"])
                    for entry, attempted in zip((first, redo), jobs):
                        source = entry["invalid_output"]
                        self.assertEqual(entry["status"], "invalid")
                        self.assertEqual(source["artifact_digest"], hashlib.sha256(raw).hexdigest())
                        self.assertEqual(Path(source["artifact"]).parent, attempted)
                        self.assertFalse((attempted / "returned-result.json").exists())
                    self.assertEqual(job, jobs[kill_at - 1])

    def test_invocation_failures_discard_intent_and_redispatch_as_live_failures_do(self):
        cases = [("codex", None), ("claude", b" \n"), ("claude", b'{"type":"result","is_error":true}')]
        for provider, raw in cases:
            with self.subTest(provider=provider, raw=raw):
                baseline = self.provider("review", provider, [raw, "approve"])
                with self.assertRaises(HarnessError):
                    self.start(baseline)
                job = baseline.calls("review")[0]
                baseline_run = read_json(job.parents[1] / "state.json")["id"]
                self.assertEqual(self.engine(baseline).resume(baseline_run)["status"], "completed")
                backend = self.provider("review", provider, [raw, "approve"])
                runtime, job = self.kill_before_result(backend)
                self.assertEqual(runtime.resume(runtime.run["id"])["status"], "completed")
                jobs = backend.calls("review")
                self.assertEqual(len(jobs), len(baseline.calls("review")))
                self.assertEqual(len(set(jobs)), 2)
                first, second = [d for d in runtime.run["dispatches"] if d.get("kind") == "review"]
                self.assertEqual(first["id"], job.name)
                self.assertNotIn("invalid_output", first)
                self.assertNotIn("retry_of", second)

    def test_corrupt_process_or_original_leaves_receipt_and_makes_no_call(self):
        cases = {
            "original": ("evidence", "runtime_integrity"),
            "directory": ("evidence", "runtime_integrity"),
            "io": ("runtime_failure", "io"),
            "fifo": ("evidence", "runtime_integrity"),
            "mismatch": ("evidence", "process_incomplete"),
        }
        for damage, expected in cases.items():
            with self.subTest(damage=damage):
                backend = self.provider("review", "claude", ["approve"])
                runtime, job = self.kill_before_result(backend)
                key = KEYS["review"]
                receipt = self.saved(runtime)["review_receipts"][key]
                record = job / "process.outcome.json"
                calls = backend.calls()
                window = nullcontext()
                if damage == "original":
                    (job / "process.stdout").unlink()
                elif damage == "directory":
                    job.rename(runtime.directory.parent / job.name)
                elif damage == "fifo":
                    record.unlink()
                    os.mkfifo(record)
                elif damage == "mismatch":
                    write_json(record, {**read_json(record), "exit_code": 9})
                else:
                    def fault(real, name, flags, *args, **kwargs):
                        if name == record.name:
                            raise OSError(errno.EIO, "synthetic read failure")
                        return real(name, flags, *args, **kwargs)
                    window = patch.object(state_files, "os", FakeOs(open=fault))
                with window, self.assertRaises(HarnessError) as stopped:
                    runtime.resume(runtime.run["id"])
                self.assertEqual(stop_pair(stopped.exception), expected)
                self.assertEqual(self.saved(runtime)["review_receipts"][key], receipt)
                self.assertEqual(backend.calls(), calls)
                self.assertFalse((job / "returned-result.json").exists())

    def test_invalid_final_output_is_consumed_when_leaving_its_candidate(self):
        backend = self.provider("final_review", "codex", [b"", "approve"])
        runtime, job = self.kill_before_result(backend)
        attempt = self.saved(runtime)["final_history"][-1]
        os.chmod(runtime.worktree / "hello.py", 0o664)
        result = runtime.resume(runtime.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(backend.calls("final_review")), 2)
        self.assertEqual(result["final_history"][0], attempt)
        self.assertNotIn("review", attempt)
        self.assertEqual(result["final_history"][1]["status"], "approved")
        self.assertNotEqual(attempt["snapshot_digest"], result["final_history"][1]["snapshot_digest"])
        self.assertNotIn(KEYS["final_review"], result["review_receipts"])
        invalid, new = [entry for entry in result["dispatches"] if entry.get("kind") == "final_review"]
        self.assertEqual((invalid["id"], invalid["status"]), (job.name, "invalid"))
        self.assertEqual(invalid["invalid_output"]["artifact_digest"], hashlib.sha256(b"").hexdigest())
        self.assertNotIn("retry_of", new)

    def test_recoverable_task_review_is_retained_when_candidate_changes(self):
        backend = self.provider("review", "codex", ["request_changes"])
        runtime, job = self.kill_before_result(backend)
        candidate = runtime.worktree / "hello.py"
        candidate.write_text(candidate.read_text() + "# changed after review\n")
        raw = (job / "response.json").read_bytes()
        calls = backend.calls()
        self.assertFalse(runtime.services.tasks.continue_task())
        self.assertEqual(backend.calls(), calls)
        self.assertEqual((job / "returned-result.json").read_bytes(), raw)
        saved = self.saved(runtime)
        record, = saved["review_history"]["greet"]
        self.assertEqual(record["status"], "invalidated")
        self.assertEqual(record["review_source"]["dispatch_id"], job.name)
        self.assertEqual(record["review_source"]["artifact_digest"], hashlib.sha256(raw).hexdigest())
        self.assertNotIn(KEYS["review"], saved["review_receipts"])
        self.assertNotIn("pending_task", saved)

    def test_capture_without_result_cannot_be_discarded_on_candidate_change(self):
        self.config.data["roles"]["reviewer"]["cli"] = "codex"
        self.config.data["runtime"]["agent_timeout_seconds"] = 1
        backend = TerminalCLI(self.config, self.repo / ".harness", "reviewer", "codex", timeout=True)
        runtime = self.engine(backend)

        def fail_result(path, value):
            if path.name == "returned-result.json":
                raise OSError(errno.EIO, "synthetic terminal publication failure")
            write_json(path, value)

        with patch("harness.adapters.write_json", side_effect=fail_result), self.assertRaises(ProcessTimeout):
            runtime.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        initial = self.saved(runtime)
        key = KEYS["review"]
        receipt = initial["review_receipts"][key]
        self.assertIn("terminal_capture_digest", receipt)
        self.assertNotIn("artifact_digest", receipt)
        job = Path(receipt["artifact"]).parent
        candidate = runtime.worktree / "hello.py"
        candidate.write_text(candidate.read_text() + "# changed after review\n")
        calls = list(backend.calls)
        for case in ("marker present", "marker removed", "terminal changed"):
            with self.subTest(case=case):
                if case == "marker removed":
                    (job / "terminal-capture.json").unlink()
                if case == "terminal changed":
                    (job / "response.json").write_bytes(b'{"verdict":"approve"}')
                expected = ("evidence", "runtime_integrity" if case == "marker present" else "process_incomplete")
                before = tree_bytes(job)
                loaded = self.loaded(backend, initial["id"])
                with self.assertRaises(HarnessError) as stopped:
                    loaded.services.tasks.continue_task()
                self.assertEqual(stop_pair(stopped.exception), expected)
                for state in (loaded.run, self.saved(loaded)):
                    for field in ("pending_task", "review_receipts", "review_history", "feedback"):
                        self.assertEqual(state.get(field), initial.get(field))
                    self.assertNotIn("greet", state["review_history"])
                with self.assertRaises(HarnessError) as stopped:
                    self.engine(backend).resume(initial["id"])
                self.assertEqual(stop_pair(stopped.exception), expected)
                self.assertEqual(tree_bytes(job), before)
                self.assertEqual(backend.calls, calls)

    def test_recovered_ask_lands_before_head_gate_after_either_kill_sequence(self):
        for kill_during_resume in (False, True):
            with self.subTest(kill_during_resume=kill_during_resume):
                backend = self.provider("decide", "codex", ["ask"], change_head=True)
                runtime, job = self.kill_before_result(backend)
                ident = runtime.run["id"]
                calls = backend.calls()
                if kill_during_resume:
                    def publish_then_kill(path, value):
                        write_json(path, value)
                        if path == job / "returned-result.json":
                            os.kill(os.getpid(), signal.SIGKILL)

                    self.killed(lambda: runtime.resume(ident),
                                patch("harness.receipts.write_json", side_effect=publish_then_kill))
                    self.assertEqual(self.saved(runtime)["decision_requests"], [])
                    self.assertTrue((job / "returned-result.json").is_file())
                with self.assertRaises(HarnessError) as stopped:
                    runtime.resume(ident)
                self.assertEqual(stop_pair(stopped.exception), ("workspace_mismatch", "head_changed"))
                self.assertTrue(stops.of(stopped.exception)["data"]["question_registered"])
                saved = self.saved(runtime)
                request, = saved["decision_requests"]
                self.assertEqual(request["source"]["dispatch_id"], job.name)
                self.assertNotIn(DECIDE_KEY, saved["commander_receipts"])
                answer = self.outside / "resume-answer.json"
                write_json(answer, {"schema_version": 1, "request_id": request["id"], "task_id": request["task_id"],
                                    "decision": "continue", "summary": "Continue"})
                with self.assertRaises(HarnessError) as stopped:
                    self.engine(backend).resume(ident, operator_decision=[str(answer)])
                self.assertEqual(stop_pair(stopped.exception), ("workspace_mismatch", "head_changed"))
                self.assertEqual(self.saved(runtime)["accepted"], {})
                self.assertEqual(backend.calls(), calls)
