"""Direct-worker routing after a real delivery to the model process wait."""
from copy import deepcopy
import os
import signal
import sys

from harness import stops
from harness.adapters import CLIBackend, DemoBackend
from harness.util import HarnessError, read_json, write_json
from test_harness import RepoCase, ROOT


def billed(run):
    return [(d["role"], d["kind"], d["task_id"], d["status"])
            for d in run["dispatches"] if d.get("invoked") is True]


class FirstCallSubprocess(DemoBackend):
    def __init__(self, config, state, program, *, role="worker"):
        self.cli = CLIBackend(config, state)
        self.cli.command = lambda *args: [sys.executable, "-c", program]
        self.role = role

    def invoke(self, **kwargs):
        if self.role == kwargs["role"]:
            self.role = None
            return self.cli.invoke(**kwargs)
        return super().invoke(**kwargs)


class DirectWorkerCancellationTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def backend(self, signum=signal.SIGINT, *, role="worker", output=None):
        preamble = ""
        if output is not None:
            preamble = f"print({output!r}, flush=True); "
        program = (preamble + "import os, sys, time; sys.stdin.read(); "
                   f"os.kill(os.getppid(), {int(signum)}); time.sleep(20)")
        return FirstCallSubprocess(self.config, self.repo / ".harness", program, role=role)

    def interrupted(self, backend=None, *, plan=True):
        engine = self.engine(backend or self.backend())
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan() if plan is True else plan)
        saved = read_json(engine.directory / "state.json")
        self.assertEqual((saved["status"], saved["stop"]["kind"], saved["stop"]["reason"]),
                         ("interrupted", "interrupted", "signal"))
        self.assertTrue(saved["stop"]["data"]["exits_confirmed"])
        original = saved["dispatches"][-1]
        self.assertEqual(original["termination_reason"], "cancelled")
        self.assertTrue(original["exit_confirmed"])
        with self.assertRaises(ProcessLookupError):
            os.killpg(original["pgid"], 0)
        return engine, saved, original

    def test_sigint_redoes_direct_worker_and_preserves_billing(self):
        baseline = self.engine().start("Implement greet", self.plan())
        engine, saved, original = self.interrupted()
        self.assertEqual((saved["attempts"], saved["agent_dispatches"]), ({"greet": 1}, 1))
        run = self.engine().resume(saved["id"])

        self.assertEqual(run["status"], "completed")
        self.assertEqual(billed(run)[1:], billed(baseline))
        self.assertEqual((run["attempts"], run["agent_dispatches"]), ({"greet": 2}, 4))
        self.assertEqual(run["dispatches"][0]["id"], original["id"])
        self.assertNotIn("attempt_exemption", run["dispatches"][0])
        self.assertNotEqual(run["dispatches"][1]["id"], original["id"])
        self.assertFalse(run["worker_receipts"])
        self.assertIn("greet", run["accepted"])

    def test_sigint_before_launch_redoes_already_billed_direct_call(self):
        backend = self.backend()
        def signal_before_process(*args):
            os.kill(os.getpid(), signal.SIGINT)
            return [sys.executable, "-c", "raise SystemExit('model must not start')"]
        backend.cli.command = signal_before_process
        engine = self.engine(backend)
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        original, = saved["dispatches"]
        self.assertEqual(original["process_outcome"]["startup_state"], "not_started")
        self.assertEqual(original["termination_reason"], "cancelled")
        self.assertTrue(original["exit_confirmed"])
        self.assertIsNone(original["pgid"])
        self.assertEqual((saved["attempts"], saved["agent_dispatches"]), ({"greet": 1}, 1))

        run = self.engine().resume(saved["id"])
        self.assertEqual([row[0] for row in billed(run)],
                         ["worker", "worker", "reviewer", "reviewer"])
        self.assertEqual((run["attempts"], run["agent_dispatches"]), ({"greet": 2}, 4))

    def test_numeric_started_rejected_by_resume_for_direct_cancellation(self):
        for started in (True, False):
            with self.subTest(started=started):
                if started:
                    engine, saved, original = self.interrupted()
                else:
                    backend = self.backend()

                    def signal_before_process(*args):
                        os.kill(os.getpid(), signal.SIGINT)
                        return [sys.executable, "-c", "raise SystemExit('model must not start')"]

                    backend.cli.command = signal_before_process
                    engine = self.engine(backend)
                    with self.assertRaises(KeyboardInterrupt):
                        engine.start("Implement greet", self.plan())
                    saved = read_json(engine.directory / "state.json")
                    original, = saved["dispatches"]

                self.assertIs(original["direct_worker_cancelled"], True)
                outcome = original["process_outcome"]
                self.assertIs(outcome["started"], started)
                self.assertEqual(outcome["startup_state"], "started" if started else "not_started")
                state = engine.directory / "state.json"
                outcome["started"] = int(started)
                write_json(state, saved)
                tampered = state.read_bytes()

                with self.assertRaisesRegex(HarnessError, "invalid direct worker cancellation provenance"):
                    self.engine().resume(saved["id"])
                self.assertEqual(state.read_bytes(), tampered)

                outcome["started"] = started
                write_json(state, saved)
                run = self.engine().resume(saved["id"])
                self.assertEqual(run["status"], "completed")
                self.assertEqual([row[0] for row in billed(run)],
                                 ["worker", "worker", "reviewer", "reviewer"])
                self.assertEqual((run["attempts"], run["agent_dispatches"]), ({"greet": 2}, 4))

    def test_direct_redo_retains_scope_and_both_budget_gates(self):
        limits = self.config.data["runtime"]
        limits.update(max_attempts=1, max_agent_dispatches=1)
        engine, saved, _ = self.interrupted()
        path = engine.worktree / "outside-scope.txt"
        path.write_text("outside scope")
        def refuse(expected):
            with self.assertRaises(HarnessError) as caught:
                self.engine().resume(saved["id"])
            stop = stops.of(caught.exception)
            self.assertEqual((stop["kind"], stop["reason"]), expected)
            current = read_json(engine.directory / "state.json")
            self.assertEqual(billed(current), billed(saved))
            self.assertEqual((current["attempts"], current["agent_dispatches"]), ({"greet": 1}, 1))
        refuse(("scope_violation", "candidate_outside_scope"))
        path.unlink()

        refuse(("budget", "task_attempts"))
        limits["max_attempts"] = 2
        refuse(("budget", "agent_dispatches"))

        limits["max_agent_dispatches"] = 4
        run = self.engine().resume(saved["id"])
        self.assertEqual((run["status"], run["attempts"], run["agent_dispatches"]),
                         ("completed", {"greet": 2}, 4))
        self.assertEqual([row[0] for row in billed(run)],
                         ["worker", "worker", "reviewer", "reviewer"])

    def test_sigterm_and_sighup_follow_same_delivery_route(self):
        for signum in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=signum):
                _, saved, _ = self.interrupted(self.backend(signum))
                run = self.engine().resume(saved["id"])
                self.assertEqual([row[0] for row in billed(run)],
                                 ["worker", "worker", "reviewer", "reviewer"])

    def test_old_ledger_without_direct_cancellation_proof_uses_commander(self):
        engine, saved, original = self.interrupted()
        original.pop("direct_worker_cancelled", None)
        write_json(engine.directory / "state.json", saved)
        run = self.engine().resume(saved["id"])
        self.assertEqual([row[0] for row in billed(run)], ["worker", "commander", "worker", "reviewer", "reviewer"])

    def test_normal_exit_without_result_uses_commander(self):
        backend = FirstCallSubprocess(self.config, self.repo / ".harness",
                                      "import sys; sys.stdin.read(); sys.exit(7)")
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["dispatches"][0]["termination_reason"], "normal")
        run = self.engine().resume(saved["id"])
        self.assertEqual([row[0] for row in billed(run)], ["worker", "commander", "worker", "reviewer", "reviewer"])

    def test_normal_failure_of_fresh_redo_does_not_reuse_the_cancellation(self):
        _, saved, _ = self.interrupted()
        backend = FirstCallSubprocess(self.config, self.repo / ".harness",
                                      "import sys; sys.stdin.read(); sys.exit(7)")
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.resume(saved["id"])
        failed = read_json(engine.directory / "state.json")
        self.assertEqual(billed(failed), [
            ("worker", "implement", "greet", "failed"), ("worker", "implement", "greet", "failed")])
        run = self.engine().resume(saved["id"])
        self.assertEqual([row[0] for row in billed(run)],
                         ["worker", "worker", "commander", "worker", "reviewer", "reviewer"])
        self.assertEqual((run["attempts"], run["agent_dispatches"]), ({"greet": 3}, 6))

    def test_cancelled_commander_still_uses_commander_receipt_recovery(self):
        _, saved, original = self.interrupted(self.backend(role="commander"), plan=None)
        run = self.engine().resume(saved["id"])
        self.assertEqual([row[0] for row in billed(run)], ["commander", "commander", "worker", "reviewer", "reviewer"])
        self.assertNotEqual(run["dispatches"][1]["id"], original["id"])

    def test_cancelled_worker_selected_by_commander_needs_fresh_decision(self):
        plan = self.plan()
        other = deepcopy(plan["tasks"][0])
        other.update(id="other", title="Other", allowed_paths=["other.py"])
        plan["tasks"].append(other)
        _, saved, _ = self.interrupted(plan=plan)
        baseline = self.engine().start("Implement greet", plan, step=True)
        run = self.engine().resume(saved["id"], step=True)
        self.assertEqual(billed(baseline), [
            ("commander", "decide", "run", "completed"), ("worker", "implement", "greet", "completed"),
            ("reviewer", "review", "greet", "completed")])
        self.assertEqual(billed(run), [
            ("commander", "decide", "run", "completed"), ("worker", "implement", "greet", "failed"),
            *billed(baseline)])
        self.assertEqual(run["status"], "paused")

    def test_cancelled_worker_with_return_is_not_redrawn(self):
        output = '{"type":"result","is_error":false,"structured_output":{}}'
        _, saved, _ = self.interrupted(self.backend(output=output))
        with self.assertRaises(HarnessError) as caught:
            self.engine().resume(saved["id"])
        stop = stops.of(caught.exception)
        self.assertEqual((stop["kind"], stop["reason"]), ("evidence", "unsealed_return"))
        self.assertEqual(billed(self.engine_with_run(saved["id"]).run), billed(saved))

    def engine_with_run(self, ident):
        engine = self.engine()
        engine.load_run(ident)
        return engine
