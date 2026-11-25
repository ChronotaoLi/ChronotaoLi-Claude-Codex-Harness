"""Run publication barriers must keep incomplete mutations away from durable state."""

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch
import signal
import subprocess
import sys

from harness import git, interrupts, stops, util
from harness.workflow import Workflow
from test_harness import RepoCase
from test_review_transitions import ledger
import test_review_batches as batches


class PublicationGuardTests(RepoCase):
    def test_effects_are_refused_and_stop_uses_the_prepublication_ledger(self):
        engine = self.engine()
        saved = engine.start("Implement greet", batches.plan(), pause_after_plan=True)
        active = self.engine()
        context = active.context
        ident = saved["id"]
        target = self.outside / "forbidden" / "note.json"
        log = self.outside / "forbidden" / "process"
        events = engine.directory / "events.jsonl"
        state = engine.directory / "state.json"
        original = state.read_bytes()
        original_events = events.read_bytes()

        def take(manager):
            with manager:
                self.fail("forbidden context was entered")

        effects = {
            "commit": context.store.commit,
            "write_json": lambda: util.write_json(target, {}),
            "read_json": lambda: util.read_json(target),
            "append_event": lambda: util.append_event(events, ident, "forbidden"),
            "event": lambda: context.event("forbidden"),
            "observe": lambda: context.observe("forbidden"),
            "checkpoint": context.checkpoint,
            "git._launch": lambda: git.head(self.repo),
            "run_process": lambda: util.run_process([sys.executable, "-c", "pass"], self.repo, log),
            "deliver": interrupts.deliver,
            "sleep": lambda: interrupts.sleep(0),
            "hold": lambda: take(interrupts.hold()),
            "publish": lambda: take(context.publish()),
        }
        for name, effect in effects.items():
            with self.subTest(effect=name):
                state.write_bytes(original)
                events.write_bytes(original_events)
                active.services.lifecycle.load_run(ident)
                baseline = deepcopy(context.run)
                with (patch.object(context.store, "_locked") as lock,
                      patch.object(Path, "read_text") as read,
                      patch.object(subprocess, "Popen") as spawn,
                      self.assertRaises(util.HarnessError) as refused):
                    try:
                        with context.publish():
                            context.run["feedback"] = {"partial": True}
                            effect()
                    finally:
                        context.save()
                first = refused.exception.__context__
                self.assertIsInstance(first, util.HarnessError)
                self.assertIn(name, str(first))
                for error in (first, refused.exception):
                    stop = stops.of(error)
                    self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "invariant"))
                lock.assert_not_called()
                read.assert_not_called()
                spawn.assert_not_called()
                self.assertFalse(target.parent.exists())
                self.assertEqual(events.read_bytes(), original_events)
                self.assertEqual(state.read_bytes(), original)

                self.assertIs(active.services.workflow.stop(refused.exception), Workflow.WRITTEN)
                stopped = util.read_json(state)
                self.assertEqual(stopped["status"], "blocked")
                self.assertEqual(stopped["store_revision"], baseline["store_revision"] + 1)
                self.assertEqual(stopped["stop"]["kind"], "runtime_failure")
                self.assertEqual(stopped["stop"]["reason"], "invariant")
                self.assertEqual(ledger(stopped), ledger(baseline))

    def test_dirty_waits_fail_the_test_after_it_finishes_even_with_delivery_disabled(self):
        engine = self.engine()
        saved = engine.start("Implement greet", batches.plan(), pause_after_plan=True)
        context = self.engine().context
        context.run = saved
        finished = []

        def wait():
            git.head(self.repo)
            util.run_process([sys.executable, "-c", "pass"], self.repo, self.outside / "process")
            context.ensure_exited()
            interrupts.deliver()

        def probe(dirty):
            with patch.object(interrupts, "ENABLED", False), interrupts.installed():
                interrupts.bind(context.store)
                wait()
                context.run["feedback"] = {"staged": dirty}
                with interrupts.hold():
                    wait()
                if dirty:
                    wait()
                context.save()
                wait()
            finished.append(dirty)

        class Probe(unittest.TestCase):
            def runTest(self):
                probe(self.dirty)

        for dirty in (False, True):
            with self.subTest(dirty=dirty):
                result = unittest.TestResult()
                case = Probe()
                case.dirty = dirty
                case.run(result)
                self.assertEqual(result.errors, [])
                self.assertEqual(result.testsRun, 1)
                if dirty:
                    (_, failure), = result.failures
                    self.assertIn("Dirty ledger at a wait boundary", failure)
                    for action in ("git._launch", "run_process", "ensure_exited", "deliver"):
                        self.assertIn(f"  {action} (1):", failure)
                else:
                    self.assertTrue(result.wasSuccessful(), result.failures)
        self.assertEqual(finished, [False, True])

    def test_stop_exit_gate_allows_only_the_rebased_candidate(self):
        engine = self.engine()
        saved = engine.start("Implement greet", batches.plan(), pause_after_plan=True)
        state = engine.directory / "state.json"
        original = state.read_bytes()

        class Probe(unittest.TestCase):
            def runTest(self):
                active = engine_for_probe()
                context = active.context
                context.run = saved.copy()
                context.store.carry("feedback", {"verification_error": "settled fact"})
                rebase = context.store.rebase_for_stop

                def rebase_with_unrelated_change():
                    rebase()
                    context.run["goal"] = "unfinished edit"

                change = patch.object(context.store, "rebase_for_stop", rebase_with_unrelated_change)
                with interrupts.installed(), change if self.dirty else nullcontext():
                    interrupts.bind(context.store)
                    error = util.HarnessError("controlled stop", stop=stops.make("runtime_failure", "io"))
                    self.assertIs(active.services.workflow.stop(error), Workflow.WRITTEN)

        engine_for_probe = self.engine
        for dirty in (False, True):
            with self.subTest(dirty=dirty):
                state.write_bytes(original)
                result = unittest.TestResult()
                case = Probe()
                case.dirty = dirty
                case.run(result)
                self.assertEqual(result.errors, [])
                if dirty:
                    (_, failure), = result.failures
                    self.assertIn("ensure_exited (1):", failure)
                else:
                    self.assertTrue(result.wasSuccessful(), result.failures)

    def test_signal_inside_abandon_publication_keeps_the_single_terminal_commit(self):
        engine = self.engine()
        saved = engine.start("Implement greet", batches.plan(), pause_after_plan=True)
        active = self.engine()
        context = active.context
        publish = context.publish
        state = engine.directory / "state.json"
        original = state.read_bytes()

        @contextmanager
        def signal_in_body(**options):
            self.assertEqual(options, {"terminal": True})
            with publish(**options):
                yield
                self.assertEqual(state.read_bytes(), original)
                signal.raise_signal(signal.SIGINT)

        with interrupts.installed(), patch.object(context, "publish", signal_in_body):
            controller = interrupts.active()
            result = active.abandon(saved["id"])
            self.assertIsNone(controller.delivered_from)
            self.assertTrue(controller.terminal)
            self.assertEqual(controller.first[0], signal.SIGINT)
        self.assertEqual(result["status"], "abandoned")
        self.assertEqual(result["store_revision"], saved["store_revision"] + 1)
        self.assertEqual(util.read_json(state), result)
