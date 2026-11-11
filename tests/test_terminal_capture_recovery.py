"""Completed stub CLI decisions survive real descendant cleanup failures."""
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
import time
from unittest.mock import patch

from harness.adapters import CLIBackend
from harness.util import HarnessError, group_alive, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, finding


class LingeringCLI(ScriptedBackend):
    def __init__(self, config, repo, target, *, duration=30, provider="claude", change=None,
                 malformed=False, exit_code=0, leader_delay=0, no_terminal=False):
        super().__init__(change)
        self.config, self.repo, self.target = config, repo, target
        self.duration, self.provider = duration, provider
        self.malformed, self.exit_code = malformed, exit_code
        self.leader_delay, self.no_terminal = leader_delay, no_terminal
        self.jobs = []

    def invoke(self, **kwargs):
        result = super().invoke(**kwargs)
        if kwargs["kind"] != self.target or self.jobs:
            return result
        job = kwargs["job"]
        self.jobs.append(job)
        envelope = {"type": "result", "subtype": "success", "structured_output": result}
        raw = "{broken" if self.malformed else json.dumps(envelope)
        # The actual child publishes the provider terminal and leaves a real
        # same-group descendant. All other model responses are offline fixtures.
        publish = (f'print({raw!r},flush=True)' if self.provider == "claude" else
                   f'open({str(job / "response.json")!r},"w").write({json.dumps(result)!r})')
        if self.no_terminal:
            (job / "response.json").unlink(missing_ok=True)
            publish = "pass"
        script = ("import subprocess,sys; " + publish + "; "
                  "p=subprocess.Popen([sys.executable,'-c',"
                  f"'import time; time.sleep({self.duration})'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                  f"open({str(job / 'descendant.pid')!r},'w').write(str(p.pid)); "
                  f"import time;time.sleep({self.leader_delay});sys.exit({self.exit_code})")
        cli = CLIBackend(self.config, self.repo / ".harness")
        cli.command = lambda *args: [sys.executable, "-c", script]
        return cli.invoke(**{**kwargs, "model": {**kwargs["model"], "cli": self.provider}})

    def release(self):
        for job in self.jobs:
            pid_file = job / "descendant.pid"
            if pid_file.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                pid_file.unlink()
            outcome = read_json(job / "process.outcome.json")
            deadline = time.monotonic() + 2
            while group_alive(outcome["pgid"]) and time.monotonic() < deadline:
                time.sleep(.01)


class TerminalCaptureRecoveryTests(RepoCase):
    def backend(self, kind="review", **kwargs):
        role = "worker" if kind == "implement" else "reviewer" if kind in ("review", "final_review") else "commander"
        self.config.data["roles"][role]["cli"] = kwargs.get("provider", "claude")
        backend = LingeringCLI(self.config, self.repo, kind, **kwargs)
        self.addCleanup(backend.release)
        return backend

    @staticmethod
    def reject(backend, kwargs, packet, result):
        if kwargs["kind"] == "review" and not backend.jobs:
            result.update(verdict="request_changes", findings=[finding()])

    def start_failed(self, backend, *, plan=True, **options):
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "Process evidence incomplete"):
            engine.start("Implement greet", read_json(ROOT / "examples/plan.json") if plan else None, **options)
        return engine

    def test_short_descendant_drain_preserves_rejection_and_requires_rework(self):
        backend = self.backend(duration=.15, change=self.reject)
        result = self.engine(backend).start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(result["attempts"].values()), 2)
        self.assertEqual([item["review"]["verdict"] for item in result["review_history"]["greet"]],
                         ["request_changes", "approve"])
        entry = next(d for d in result["dispatches"] if d["id"] == backend.jobs[0].name)
        self.assertTrue(entry["exit_confirmed"])
        self.assertEqual(entry["process_outcome"]["cleanup_actions"].count("drain_descendants"), 1)
        self.assertNotIn("sigterm", entry["process_outcome"]["cleanup_actions"])

    def test_short_worker_verification_and_hook_descendants_finish_normally(self):
        spawn = ("import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
                 "'import time;time.sleep(.1)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)")
        self.config.data["verification"]["commands"] = [[sys.executable, "-c",
            "from hello import greet; assert greet('Ada') == 'Hello, Ada!'; " + spawn]]
        backend = self.backend("implement", duration=.1)
        engine = self.engine(backend)
        engine.hooks.spec = {"pre_accept": [{"argv": [sys.executable, "-c", spawn]}]}
        try:
            result = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        except HarnessError as error:
            facts = {str(path.relative_to(engine.directory)): read_json(path)
                     for path in engine.directory.rglob("*.outcome.json")}
            error.add_note("Observed process outcomes: " + json.dumps(facts))
            raise
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(result["attempts"].values()), 1)
        for entry in result["dispatches"]:
            if entry["role"] in ("worker", "verification"):
                self.assertTrue(entry["evidence_complete"])
                self.assertTrue(entry["exit_confirmed"])
                self.assertEqual(entry["exit_code"], 0)

    def test_long_descendant_blocks_then_same_rejection_is_consumed_without_reroll(self):
        backend = self.backend(change=self.reject)
        engine = self.start_failed(backend)
        calls = len(backend.calls)
        receipt = engine.run["review_receipts"]["reviewer:review:greet"]
        self.assertEqual(read_json(Path(receipt["artifact"]))["verdict"], "request_changes")
        self.assertIn("terminal_capture_digest", receipt)
        with self.assertRaisesRegex(HarnessError, "进程组仍存在"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), calls)
        backend.release()
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(result["attempts"].values()), 2)
        self.assertEqual([item["review"]["verdict"] for item in result["review_history"]["greet"]],
                         ["request_changes", "approve"])
        original = next(d for d in result["dispatches"] if d["id"] == backend.jobs[0].name)
        self.assertEqual(original["exit_code"], 0)
        self.assertTrue(original["exit_confirmed"])
        self.assertFalse(original["process_outcome"]["exit_confirmed"])
        self.assertFalse(original["evidence_complete"])

    def test_commander_plan_replays_original_with_exhausted_dispatch_budget(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        backend = self.backend("plan", provider="codex")
        engine = self.start_failed(backend, plan=False, pause_after_plan=True)
        backend.release()
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(result["agent_dispatches"], 1)
        self.assertEqual(len(backend.calls), 1)

    def test_cancellation_during_descendant_drain_never_rerolls_or_accepts(self):
        backend = self.backend()
        original = time.sleep
        cancelled = False
        def interrupt(seconds):
            nonlocal cancelled
            if backend.jobs and (backend.jobs[0] / "descendant.pid").exists() and not cancelled:
                cancelled = True
                os.kill(os.getpid(), signal.SIGINT)
            original(seconds)
        engine = self.engine(backend)
        with patch("harness.process_runtime.time.sleep", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        backend.release()
        calls = len(backend.calls)
        self.assertTrue(cancelled)
        entry = engine.run["dispatches"][-1]
        self.assertEqual(entry["exit_code"], 0)
        self.assertEqual(entry["termination_reason"], "cancelled")
        with self.assertRaisesRegex(HarnessError, "终端结果"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), calls)
        self.assertFalse(engine.run["accepted"])

    def test_commander_stop_remains_stop_on_repeated_resume(self):
        def blocked(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.update(status="blocked", blocker_kind="task", blockers=["Need a prerequisite"])
        backend = self.backend("replan", change=blocked)
        engine = self.start_failed(backend)
        backend.release()
        calls = len(backend.calls)
        for _ in range(2):
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run["id"])
            self.assertEqual(len(backend.calls), calls)
        saved = read_json(engine.directory / "state.json")
        self.assertIn("replan_stopped", saved)
        self.assertEqual(saved["replan_attempts"], 1)

    def test_missing_tampered_terminal_or_changed_candidate_never_rerolls(self):
        backend = self.backend(change=self.reject)
        engine = self.start_failed(backend)
        backend.release()
        calls = len(backend.calls)
        job = backend.jobs[0]
        capture = read_json(job / "terminal-capture.json")
        self.assertEqual((capture["provider"], capture["source"]), ("claude", "process.stdout"))
        self.assertNotIn("support_digests", capture)
        self.assertFalse((job / "capture-manifest.json").exists())
        for filename in ("terminal-capture.json", "process.stdout", "returned-result.json", "process.outcome.json"):
            path = job / filename
            original = path.read_bytes()
            for mutation in ("missing", "changed", "symlink"):
                with self.subTest(filename=filename, mutation=mutation):
                    path.unlink()
                    if mutation == "changed":
                        path.write_bytes(original + b"\n")
                    elif mutation == "symlink":
                        target = job / "evidence-copy"
                        target.write_bytes(original)
                        path.symlink_to(target)
                    try:
                        with self.assertRaisesRegex(HarnessError, "终端结果"):
                            self.engine(backend).resume(engine.run["id"])
                        self.assertEqual(len(backend.calls), calls)
                    finally:
                        path.unlink(missing_ok=True)
                        path.write_bytes(original)
        (engine.worktree / "hello.py").write_text("# changed candidate\n")
        with self.assertRaisesRegex(HarnessError, "代码快照已变化"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), calls)

    def test_malformed_terminal_and_nonzero_exit_never_become_approval(self):
        for options in ({"malformed": True}, {"exit_code": 7}):
            with self.subTest(options=options):
                backend = self.backend(**options)
                engine = self.start_failed(backend)
                backend.release()
                calls = len(backend.calls)
                for _ in range(2):
                    with self.assertRaisesRegex(HarnessError, "终端结果"):
                        self.engine(backend).resume(engine.run["id"])
                    self.assertEqual(len(backend.calls), calls)
                self.assertFalse(engine.run["accepted"])

    def test_interrupted_capture_publication_fails_closed_without_result(self):
        backend = self.backend()
        original = write_json
        def interrupted(path, value):
            if path.name == "returned-result.json":
                raise OSError("capture publication failed")
            original(path, value)
        with patch("harness.adapters.write_json", side_effect=interrupted):
            engine = self.start_failed(backend)
        backend.release()
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "终端结果"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), calls)

    def test_complete_terminal_before_timeout_blocks_reroll(self):
        self.config.data["runtime"]["agent_timeout_seconds"] = 1
        backend = self.backend(duration=.01, leader_delay=30, change=self.reject)
        engine = self.engine(backend)
        from harness.util import ProcessTimeout
        with self.assertRaises(ProcessTimeout):
            engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        backend.release()
        calls = len(backend.calls)
        entry = engine.run["dispatches"][-1]
        self.assertEqual(entry["exit_code"], -signal.SIGTERM)
        self.assertEqual(entry["termination_reason"], "timeout")
        for attempt in range(2):
            if attempt:
                # Deleting all captured bytes cannot turn an observed
                # terminal into a retryable call with no terminal.
                (backend.jobs[0] / "terminal-capture.json").unlink()
                (backend.jobs[0] / "process.stdout").unlink()
            with self.assertRaisesRegex(HarnessError, "终端结果"):
                self.engine(backend).resume(engine.run["id"])
            self.assertEqual(len(backend.calls), calls)
        self.assertFalse(engine.run["accepted"])

    def test_no_terminal_or_partial_terminal_timeout_remains_retryable(self):
        from harness.util import ProcessTimeout
        self.config.data["runtime"]["agent_timeout_seconds"] = 1
        for options in ({"no_terminal": True}, {"malformed": True}):
            with self.subTest(options=options):
                backend = self.backend(duration=.01, leader_delay=30, **options)
                engine = self.engine(backend)
                with self.assertRaises(ProcessTimeout):
                    engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
                backend.release()
                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(sum(result["attempts"].values()), 1)
                self.assertEqual(sum(c["kind"] == "review" for c in backend.calls), 2)

    def test_nonzero_after_terminal_without_live_descendants_never_rerolls(self):
        backend = self.backend(duration=.01, leader_delay=.15, exit_code=7, change=self.reject)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "CLI exit=7"):
            engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        entry = engine.run["dispatches"][-1]
        self.assertTrue(entry["exit_confirmed"])
        self.assertTrue(entry["evidence_complete"])
        calls = len(backend.calls)
        for _ in range(2):
            with self.assertRaisesRegex(HarnessError, "终端结果"):
                self.engine(backend).resume(engine.run["id"])
            self.assertEqual(len(backend.calls), calls)
        self.assertFalse(engine.run["accepted"])
