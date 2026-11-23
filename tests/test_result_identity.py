"""Completion identity at the terminal-save and ordinary lifecycle boundaries."""

import json
from copy import deepcopy
from unittest.mock import patch

from git_isolation import fixture_git
from harness import git
from harness.run_store import LEDGER, RunStore
from harness.state_files import LockedDirectory
from harness.util import HarnessError, read_json, write_json
from test_harness import RepoCase, ROOT, stopped
from test_runtime_changes import ScriptedBackend


class ResultIdentityTests(RepoCase):
    def test_first_approval_saves_identity_with_completion_in_both_verification_modes(self):
        for fresh_checkout in (False, True):
            with self.subTest(fresh_checkout=fresh_checkout):
                self.config.data["verification"]["fresh_checkout"] = fresh_checkout
                backend = ScriptedBackend()
                engine = self.engine(backend)
                writes = []
                commit = RunStore.commit

                def record_commit(store, **kwargs):
                    writes.append((store.run["status"], deepcopy(store.run.get("result")), kwargs))
                    return commit(store, **kwargs)

                with patch.object(RunStore, "commit", record_commit):
                    completed = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
                attempt, = completed["final_history"]
                head = completed["expected_head"]
                expected = {
                    "commit": head,
                    "tree": fixture_git(engine.worktree, "rev-parse", f"{head}^{{tree}}").strip(),
                    "snapshot_digest": attempt["snapshot_digest"],
                    "attempts": [1],
                    "verification_mode": "fresh_checkout" if fresh_checkout else "run_worktree",
                }
                self.assertEqual(attempt["head"], head)
                self.assertEqual(attempt["tree"], expected["tree"])
                terminal, = [(status, result, options) for status, result, options in writes
                             if status == "completed" or result is not None]
                self.assertEqual(terminal[:2], ("completed", expected))
                self.assertIs(terminal[2]["terminal"], True)
                ledger = engine.directory / "state.json"
                self.assertEqual(read_json(ledger)["result"], expected)
                if fresh_checkout:
                    checkout, = engine.directory.glob("verification/final/*/verification-*/checkout.json")
                    self.assertEqual(read_json(checkout)["head"], head)
                else:
                    self.assertEqual(completed["verification_checkouts"], [])
                original = ledger.read_bytes()
                with patch.object(git, "_run_git", side_effect=AssertionError("completed load ran Git")):
                    self.assertEqual(self.engine(backend).resume(completed["id"])["result"], expected)
                self.assertEqual(ledger.read_bytes(), original)

    def test_load_checks_final_record_then_result_then_checkout_registration(self):
        engine = self.engine(ScriptedBackend())
        completed = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        saved = deepcopy(completed)
        saved["result"] = None
        saved["verification_checkouts"] = None
        tree = saved["final_history"][0].pop("tree")
        path = engine.directory / "state.json"

        def rejection():
            write_json(path, saved)
            before = path.read_bytes()
            with self.assertRaises(HarnessError) as error:
                self.engine().load_run(completed["id"])
            self.assertEqual((error.exception.stop["kind"], error.exception.stop["reason"]),
                             ("evidence", "load_integrity"))
            self.assertEqual(path.read_bytes(), before)
            return str(error.exception)

        self.assertIn("final_history[0].tree", rejection())
        saved["final_history"][0]["tree"] = tree
        self.assertIn("result", rejection())
        saved["result"] = completed["result"]
        self.assertIn("verification_checkouts", rejection())

    def test_resume_rejects_completion_identity_on_interrupted_run(self):
        engine = self.engine(ScriptedBackend())
        run = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.assertIn("result", run)
        stopped(run, "interrupted")
        engine.context.save()
        ledger = engine.directory / LEDGER
        before = ledger.read_bytes()

        with self.assertRaisesRegex(HarnessError, "result 只适用于 completed") as error:
            self.engine().resume(run["id"])
        stop = error.exception.stop
        self.assertEqual((stop["kind"], stop["reason"]), ("evidence", "load_integrity"))
        self.assertEqual(ledger.read_bytes(), before)

    def test_interrupt_before_terminal_replace_publishes_reloadable_stop(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        original_replace = LockedDirectory.replace
        attempted = []

        def interrupt(locked, name, data):
            if name == LEDGER:
                candidate = json.loads(data)
                if candidate["status"] == "completed":
                    attempted.append(candidate)
                    raise KeyboardInterrupt()
            return original_replace(locked, name, data)

        with patch.object(LockedDirectory, "replace", interrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        candidate, = attempted
        saved = read_json(engine.directory / LEDGER)
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual((saved["stop"]["kind"], saved["stop"]["reason"]), ("interrupted", "signal"))
        self.assertNotIn("result", saved)
        self.assertEqual(saved["final_history"], candidate["final_history"])
        self.assertEqual(saved["store_revision"], candidate["store_revision"])
        calls = list(backend.calls)
        resumed = self.engine(backend)
        resumed.load_run(saved["id"])
        completed = resumed.resume(saved["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"], candidate["result"])
        self.assertEqual(backend.calls, calls)
