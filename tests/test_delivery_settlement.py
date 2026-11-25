"""Cooperative interruption at committed boundaries in ordinary offline runs."""
from pathlib import Path
import json
import os
import signal
import shutil
from unittest.mock import patch

from harness import acceptance, git, interrupts
from harness.util import read_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class DeliverySettlementTests(RepoCase):
    def setUp(self):
        super().setUp()
        enabled = patch.object(interrupts, "ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)
        self.backend = ScriptedBackend()
        self.runtime = self.engine(self.backend)
        self.plan = read_json(ROOT / "examples" / "plan.json")

    @staticmethod
    def request():
        # The native Git calls run normally. Only this application process receives a signal.
        os.kill(os.getpid(), signal.SIGINT)

    def start_interrupted(self, where, *, supplied_plan=True):
        with interrupts.installed(), self.assertRaises(KeyboardInterrupt):
            controller = interrupts.active()
            try:
                self.runtime.start("Implement greet", self.plan if supplied_plan else None)
            finally:
                self.assertEqual(controller.delivered_from, where)
        saved = read_json(self.runtime.directory / "state.json")
        self.assertEqual((saved["status"], saved["stop"]["kind"]), ("interrupted", "interrupted"))
        return saved

    def resume_completed(self, saved):
        resumed = self.engine(self.backend).resume(saved["id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(git.head(Path(resumed["worktree"])), resumed["expected_head"])
        return resumed

    def test_start_persists_binding_before_scheduler_delivery(self):
        add = git.add_worktree

        def add_with_signal(*args, **kwargs):
            binding = add(*args, **kwargs)
            self.assertTrue(interrupts.holding())
            self.request()
            interrupts.deliver()
            return binding

        with patch.object(git, "add_worktree", add_with_signal):
            saved = self.start_interrupted("Scheduler._execute_until_terminal")
        git.check_worktree_binding(Path(saved["worktree"]), saved["worktree_binding"])
        self.assertEqual(saved["dispatches"], [])
        self.resume_completed(saved)

    def test_initial_planning_checks_signal_before_deriving_a_call(self):
        tasks = self.runtime.services.tasks
        replay = tasks.replay_worker

        def signal_after_replay():
            result = replay()
            self.request()
            return result

        with patch.object(tasks, "replay_worker", signal_after_replay):
            saved = self.start_interrupted("Scheduler._execute_until_terminal", supplied_plan=False)
        self.assertEqual(saved["original_tasks"], [])
        self.assertEqual(saved["dispatches"], [])
        self.assertEqual(self.backend.calls, [])
        self.resume_completed(saved)

    def test_commander_delivers_before_charge_on_small_repository(self):
        budgets = self.runtime.services.budgets
        validate = budgets.validate_attempt_accounting

        def validate_with_signal():
            validate()
            self.assertTrue(self.runtime.context.store.matches_commit())
            self.request()

        with patch.object(budgets, "validate_attempt_accounting", validate_with_signal):
            saved = self.start_interrupted("Dispatcher.dispatch_once", supplied_plan=False)
        commander, = saved["dispatches"]
        self.assertEqual((commander["role"], commander["status"], commander["invoked"]),
                         ("commander", "failed", False))
        self.assertNotIn("snapshot_digest", commander)
        self.assertEqual(saved["agent_dispatches"], 0)
        self.assertEqual(saved["commander_receipts"], {})
        self.assertEqual(self.backend.calls, [])
        self.resume_completed(saved)

    def test_worker_precharge_delivery_preserves_uninvoked_entry(self):
        worker_receipts = self.runtime.services.worker_receipts
        publish = worker_receipts.publish_inputs

        def publish_with_signal(*args):
            inputs = publish(*args)
            self.assertTrue(self.runtime.context.store.matches_commit())
            self.request()
            return inputs

        with patch.object(worker_receipts, "publish_inputs", publish_with_signal):
            saved = self.start_interrupted("Dispatcher.dispatch_once")
        worker, = saved["dispatches"]
        self.assertEqual((worker["status"], worker["invoked"]), ("failed", False))
        self.assertNotIn("snapshot_digest", worker)
        self.assertEqual(saved["agent_dispatches"], 0)
        self.assertEqual(saved["attempts"], {})
        self.assertEqual(saved["worker_receipts"], {})
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.resume_completed(saved)["attempts"], {"greet": 1})

    def test_worker_snapshot_seals_before_interrupt_and_explicit_resume_replays_it(self):
        read = git.git_bytes
        injected = []

        def index_read(repo, *args, **kwargs):
            result = read(repo, *args, **kwargs)
            if (args[:3] == ("ls-files", "--stage", "-z")
                    and self.backend.calls and self.backend.calls[-1]["role"] == "worker"
                    and not injected):
                self.assertTrue(interrupts.holding())
                injected.append(args)
                self.request()
                interrupts.deliver()
            return result

        with patch.object(git, "git_bytes", index_read):
            saved = self.start_interrupted("Dispatcher.dispatch_once")
        self.assertEqual(len(injected), 1)
        receipt, = saved["worker_receipts"].values()
        self.assertEqual(receipt["phase"], "sealed")
        self.assertTrue(list(Path(receipt["seal"]).parent.glob("worker-seal.*.json")))
        self.assertEqual([call["role"] for call in self.backend.calls], ["worker"])
        resumed = self.resume_completed(saved)
        self.assertEqual(resumed["worker_receipts"], {})
        self.assertEqual(resumed["attempts"], {"greet": 1})
        self.assertEqual(sum(call["role"] == "worker" for call in self.backend.calls), 1)

    def test_acceptance_finishes_before_post_accept_delivery(self):
        confirm = acceptance.confirm

        def confirm_with_signal(*args):
            confirm(*args)
            self.assertTrue(interrupts.holding())
            self.request()
            interrupts.deliver()

        with patch.object(acceptance, "confirm", confirm_with_signal):
            saved = self.start_interrupted("TaskLifecycle.finish_post_accept")
        self.assertEqual(saved["accepted"]["greet"]["commit"], saved["expected_head"])
        self.assertEqual(saved["pending_post_accept"]["payload"]["commit"], saved["expected_head"])
        self.assertNotIn("pending_acceptance", saved)
        resumed = self.resume_completed(saved)
        self.assertNotIn("pending_post_accept", resumed)
        self.assertEqual(sum(call["role"] == "worker" for call in self.backend.calls), 1)

    def test_verification_interrupt_at_exit_gate_registers_no_command(self):
        service = self.runtime.services.verification
        run = service.run_verification_commands
        ensure = self.runtime.context.ensure_exited

        def ensure_with_signal():
            ensure()
            self.request()

        def verify_with_signal(*args, **kwargs):
            with patch.object(self.runtime.context, "ensure_exited", ensure_with_signal):
                return run(*args, **kwargs)

        with patch.object(service, "run_verification_commands", verify_with_signal):
            saved = self.start_interrupted("Verification.run_verification_commands")
        self.assertFalse(any(entry["role"] == "verification" for entry in saved["dispatches"]))
        self.resume_completed(saved)

    def test_abandon_delivery_after_locked_load_uses_canonical_state(self):
        saved = self.runtime.start("Implement greet", self.plan, pause_after_plan=True)
        state = self.runtime.directory / "state.json"
        reformatted = json.dumps(saved, separators=(",", ":")).encode()
        state.write_bytes(reformatted)
        resumed = self.engine(self.backend)
        lifecycle = resumed.services.lifecycle
        load = lifecycle.load_run
        calls = []

        def load_with_signal(ident):
            load(ident)
            calls.append(ident)
            if len(calls) == 2:
                self.assertTrue(resumed.context.store.matches_commit())
                self.request()

        with interrupts.installed(), patch.object(lifecycle, "load_run", load_with_signal):
            controller = interrupts.active()
            with self.assertRaises(KeyboardInterrupt):
                resumed.abandon(saved["id"])
            self.assertEqual(controller.delivered_from, "RunLifecycle.abandon.<locals>.abandon_locked")
        self.assertEqual(state.read_bytes(), reformatted)
        self.assertEqual(resumed.abandon(saved["id"])["status"], "abandoned")

    def test_abandon_checks_signal_between_confirmation_and_exit_gate(self):
        saved = self.runtime.start("Implement greet", self.plan, pause_after_plan=True)
        state = self.runtime.directory / "state.json"
        before = state.read_bytes()
        resumed = self.engine(self.backend)
        confirm = resumed.context.confirm_exited

        def confirm_with_signal(confirmed):
            confirm(confirmed)
            self.request()

        with interrupts.installed(), patch.object(resumed.context, "confirm_exited", confirm_with_signal), \
                patch.object(resumed.context, "ensure_exited", wraps=resumed.context.ensure_exited) as ensure:
            controller = interrupts.active()
            with self.assertRaises(KeyboardInterrupt):
                resumed.abandon(saved["id"])
            self.assertEqual(controller.delivered_from, "RunLifecycle.abandon.<locals>.abandon_locked")
            ensure.assert_not_called()
        self.assertEqual(state.read_bytes(), before)

    def test_resume_delivery_after_approval_keeps_input_and_original_stop(self):
        saved = self.runtime.start("Implement greet", self.plan, pause_after_plan=True)
        resumed = self.engine(self.backend)
        save = resumed.context.save

        def save_with_signal(*args, **kwargs):
            result = save(*args, **kwargs)
            if resumed.run["plan_approved"]:
                self.request()
            return result

        with interrupts.installed(), patch.object(resumed.context, "save", save_with_signal):
            controller = interrupts.active()
            with self.assertRaises(KeyboardInterrupt):
                resumed.resume(saved["id"], approve_plan=True)
            self.assertEqual(controller.delivered_from, "RunLifecycle.resume.<locals>.resume_locked")
        interrupted = read_json(resumed.directory / "state.json")
        self.assertEqual(interrupted["status"], saved["status"])
        self.assertEqual(interrupted["stop"], saved["stop"])
        self.assertTrue(interrupted["plan_approved"])
        self.assertEqual(self.backend.calls, [])
        self.resume_completed(interrupted)

    def test_snapshot_checks_for_delivery_after_64_hashes(self):
        for index in range(70):
            (self.repo / f"owned-{index:02}").write_text(str(index))
        read = git._entry
        visited = []

        def read_with_signal(*args):
            result = read(*args)
            visited.append(args[-1])
            if len(visited) == 1:
                self.request()
            return result

        with interrupts.installed(), patch.object(git, "_entry", read_with_signal):
            controller = interrupts.active()
            with self.assertRaises(KeyboardInterrupt):
                git.snapshot(self.repo)
            self.assertTrue(controller.delivered_from.startswith("read_worktree."))
        self.assertEqual(len(visited), 64)
        self.assertEqual(len(git.snapshot(self.repo)["files"]), 72)

    def exercise_checkout(self, boundary):
        self.config.data["verification"]["fresh_checkout"] = True
        run = git.git_process
        capture = git.capture_worktree_binding
        injected = []
        roots = set()

        def remember(path):
            checkout = Path(path)
            if checkout.name != "checkout" or not checkout.parents[1].name.startswith("harness-verify-"):
                return False
            root = checkout.parents[1]
            if root not in roots:
                roots.add(root)
                self.addCleanup(shutil.rmtree, root, ignore_errors=True)
            return True

        def at_boundary():
            self.assertTrue(interrupts.holding())
            injected.append(boundary)
            self.request()
            interrupts.deliver()

        def git_process(repo, *args, **kwargs):
            result = run(repo, *args, **kwargs)
            if args[:4] == ("worktree", "add", "--no-checkout", "--detach") and remember(args[4]):
                if boundary == "add":
                    at_boundary()
            elif args[:3] == ("reset", "-q", "--hard") and remember(repo) and boundary == "reset":
                at_boundary()
            return result

        def capture_binding(path, *args, **kwargs):
            binding = capture(path, *args, **kwargs)
            if remember(path) and boundary == "capture":
                at_boundary()
            return binding

        with patch.object(git, "git_process", git_process), patch.object(git, "capture_worktree_binding", capture_binding):
            saved = self.start_interrupted("Verification.run_verification_commands")
        self.assertEqual(injected, [boundary])
        registered, = saved["verification_checkouts"]
        checkout = Path(registered["worktree"])
        git.check_worktree_binding(checkout, registered["binding"])
        self.assertEqual(git.head(checkout), saved["expected_head"])
        self.assertTrue((checkout / "hello.py").is_file())
        self.assertFalse(any(row["role"] == "verification" and row["task_id"] == "final"
                             for row in saved["dispatches"]))
        resumed = self.resume_completed(saved)
        self.assertEqual(resumed["attempts"], {"greet": 1})

    def test_fresh_checkout_settles_signal_at_registration(self):
        self.exercise_checkout("add")

    def test_fresh_checkout_settles_signal_at_reset(self):
        self.exercise_checkout("reset")

    def test_fresh_checkout_settles_signal_at_binding_capture(self):
        self.exercise_checkout("capture")
