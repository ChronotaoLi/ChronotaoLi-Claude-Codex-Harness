"""Offline startup recovery uses real Git and simulated model responses."""
import copy
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.util import HarnessError, read_json, write_json
from test_engine_repairs import RecordingBackend
from test_harness import ROOT, RepoCase


class StartupRecoveryTests(RepoCase):
    def plan(self):
        plan = read_json(ROOT / "examples" / "plan.json")
        command = copy.deepcopy(self.config.data["verification"]["commands"][0])
        plan["tasks"][0]["required_commands"] = [command]
        for ident, dependency, filename in (("farewell", "greet", "farewell.py"),
                                             ("message", "farewell", "message.py")):
            plan["tasks"].append({
                "id": ident, "title": f"Implement {ident}",
                "description": f"Implement and test {ident} after {dependency}",
                "depends_on": [dependency], "allowed_paths": [filename, f"test_{filename}"],
                "acceptance": [f"{ident}('Ada') returns the expected message", "All unittest checks pass"],
                "skills": [], "required_commands": [] if ident == "farewell" else [command],
            })
        return plan

    @staticmethod
    def implement_tasks(backend, kwargs, packet, result):
        if kwargs["role"] != "worker":
            return
        ident = kwargs["task"]["id"]
        if ident == "greet":
            return
        cwd = kwargs["cwd"]
        if ident == "farewell":
            code = 'def farewell(name):\n    return f"Goodbye, {name}!"\n'
            expected = "Goodbye, Ada!"
        else:
            code = ('from hello import greet\nfrom farewell import farewell\n'
                    'def message(name):\n    return greet(name) + " " + farewell(name)\n')
            expected = "Hello, Ada! Goodbye, Ada!"
        (cwd / f"{ident}.py").write_text(code)
        (cwd / f"test_{ident}.py").write_text(
            f"import unittest\nfrom {ident} import {ident}\n"
            f"class Tests(unittest.TestCase):\n    def test_{ident}(self):\n"
            f"        self.assertEqual({ident}('Ada'), {expected!r})\n")
        result.update(summary=f"Implemented and tested {ident}", evidence=[f"{ident}.py", f"test_{ident}.py"])

    def saved(self, engine):
        return read_json(engine.directory / "state.json")

    def interrupt_save(self, engine, predicate, plan, **kwargs):
        save = engine.save
        interrupted = False

        def interrupt():
            nonlocal interrupted
            save()
            if not interrupted and predicate(engine.run):
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(engine, "save", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greeting, farewell, and combined message", plan, **kwargs)
        self.assertTrue(interrupted)
        return self.saved(engine)

    def assert_registered(self, result, plan, backend):
        self.assertEqual(result["tasks"], plan["tasks"])
        self.assertEqual(result["original_tasks"], plan["tasks"])
        self.assertEqual(len(result["plan_history"]), 1)
        self.assertEqual(result["plan_history"][0]["tasks"], plan["tasks"])
        self.assertEqual(read_json(Path(result["plan_history"][0]["artifact"]))["tasks"], plan["tasks"])
        self.assertFalse(any(call["kind"] == "plan" for call in backend.calls))

    def test_supplied_multitask_plan_recovers_after_binding_and_diagnostic_saves(self):
        for stage in ("binding", "diagnostics"):
            with self.subTest(stage=stage):
                plan = self.plan()
                backend = RecordingBackend(self.implement_tasks)
                engine = self.engine(backend)
                saved = self.interrupt_save(engine, lambda run: bool(run["worktree_binding"])
                    and not run["tasks"] and (stage == "binding" or "ignored_diagnostics" in run), plan)
                self.assertEqual(saved["tasks"], [])
                self.assertEqual(saved["original_tasks"], plan["tasks"])
                self.assertEqual(backend.calls, [])
                resumed = self.engine(backend)
                result = resumed.resume(saved["id"])
                self.assertEqual(result["status"], "completed")
                self.assert_registered(result, plan, backend)
                self.assertEqual(list(result["accepted"]), [task["id"] for task in plan["tasks"]])
                self.assertEqual([call["task_id"] for call in backend.calls if call["role"] == "worker"],
                                 [task["id"] for task in plan["tasks"]])
                self.assertEqual(sum(call["kind"] == "review" for call in backend.calls), 3)
                self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 1)
                for accepted in result["accepted"].values():
                    checks = read_json(Path(accepted["verification"]))
                    self.assertTrue(checks)
                    self.assertTrue(all(check["exit_code"] == 0 for check in checks))
                self.assertEqual(result["final_history"][-1]["status"], "approved")
                self.assertEqual(result["expected_head"], git.head(resumed.worktree, binding=result["worktree_binding"]))
                self.assertFalse((self.repo / "hello.py").exists())

    def test_first_ignored_path_interrupt_preserves_plan_and_approval_pause(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        plan = self.plan()
        with patch.object(engine, "ignored_paths", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement messages", plan, pause_after_plan=True)
        saved = self.saved(engine)
        self.assertTrue(saved["worktree_binding"])
        self.assertEqual(saved["tasks"], [])
        self.assertEqual(saved["original_tasks"], plan["tasks"])
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertFalse(result["plan_approved"])
        self.assert_registered(result, plan, backend)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(saved["id"])
        self.assertEqual(backend.calls, [])

    def fail_plan_artifact(self, engine, plan=None):
        def fail(path, value):
            if Path(path).name == "revision-0.json":
                raise OSError("injected plan artifact write failure")
            write_json(path, value)

        with patch("harness.planning.write_json", side_effect=fail), self.assertRaises(OSError):
            engine.start("Implement messages", plan, pause_after_plan=True)
        saved = self.saved(engine)
        self.assertEqual(saved["tasks"], [])
        self.assertEqual(saved["plan_history"], [])
        self.assertTrue(saved["worktree_binding"])
        self.assertEqual(saved["status"], "blocked")
        return saved

    def test_supplied_plan_artifact_write_failure_recovers_without_planning(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        plan = self.plan()
        saved = self.fail_plan_artifact(engine, plan)
        self.assertEqual(saved["original_tasks"], plan["tasks"])
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "awaiting_approval")
        self.assert_registered(result, plan, backend)
        self.assertEqual(backend.calls, [])

    def test_without_supplied_plan_startup_recovery_still_plans_once(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        saved = self.interrupt_save(engine, lambda run: "ignored_diagnostics" in run, None)
        self.assertEqual(saved["original_tasks"], [])
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(call["kind"] == "plan" for call in backend.calls), 1)
        self.assertEqual(len(result["plan_history"]), 1)

    def test_model_plan_artifact_write_failure_reuses_and_revalidates_receipt(self):
        for mode in ("intact", "changed_result", "missing_result", "changed_candidate"):
            with self.subTest(mode=mode):
                backend = RecordingBackend()
                engine = self.engine(backend)
                saved = self.fail_plan_artifact(engine)
                self.assertTrue(saved["original_tasks"])
                receipt = saved["commander_receipts"]["commander:plan:planning"]
                artifact = Path(receipt["artifact"])
                if mode == "changed_result":
                    value = read_json(artifact)
                    value["tasks"][0]["acceptance"].append("Changed after receipt validation")
                    write_json(artifact, value)
                elif mode == "missing_result":
                    artifact.unlink()
                elif mode == "changed_candidate":
                    (engine.worktree / "test_hello.py").write_text("# changed since planning\n")
                calls = copy.deepcopy(backend.calls)
                if mode == "intact":
                    result = self.engine(backend).resume(saved["id"])
                    self.assertEqual(result["status"], "awaiting_approval")
                    self.assertEqual(result["tasks"], saved["original_tasks"])
                    self.assertEqual(len(result["plan_history"]), 1)
                    self.assertEqual(result["commander_receipts"], {})
                else:
                    with self.assertRaises(HarnessError):
                        self.engine(backend).resume(saved["id"])
                    result = self.saved(engine)
                    self.assertEqual(result["tasks"], [])
                    self.assertEqual(result["plan_history"], [])
                    self.assertEqual(result["commander_receipts"], saved["commander_receipts"])
                self.assertEqual(backend.calls, calls)
                self.assertEqual(sum(call["kind"] == "plan" for call in backend.calls), 1)

    def assert_unbound_resume_rejected(self, engine, backend):
        saved = self.saved(engine)
        with patch("harness.git.capture_worktree_binding", side_effect=AssertionError("must not infer a new binding")):
            with self.assertRaises(HarnessError) as caught:
                self.engine(backend).resume(saved["id"])
        self.assertIn("abandon", str(caught.exception))
        self.assertEqual(self.saved(engine), saved)
        with self.assertRaises(HarnessError):
            self.engine(backend).abandon(saved["id"], remove_worktree=True)
        self.assertEqual(self.saved(engine), saved)
        result = self.engine(backend).abandon(saved["id"])
        self.assertEqual(result["status"], "abandoned")
        self.assertEqual(result["original_tasks"], saved["original_tasks"])
        self.assertEqual(backend.calls, [])

    def test_first_save_preserves_independent_plan_but_cannot_resume_without_binding(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        plan = self.plan()
        original = copy.deepcopy(plan)
        refs = git.git(self.repo, "show-ref")
        saved = self.interrupt_save(engine, lambda run: not run["worktree_binding"], plan)
        plan["tasks"][0]["acceptance"].append("caller mutation")
        self.assertEqual(saved["original_tasks"], original["tasks"])
        self.assertEqual(engine.run["original_tasks"], original["tasks"])
        self.assertFalse(engine.worktree.exists())
        self.assertEqual(git.git(self.repo, "show-ref"), refs)
        self.assert_unbound_resume_rejected(engine, backend)

    def test_created_worktree_without_captured_binding_is_preserved_for_manual_cleanup(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        plan = self.plan()
        with patch("harness.git.capture_worktree_binding", side_effect=HarnessError("injected binding capture failure")):
            with self.assertRaises(HarnessError):
                engine.start("Implement messages", plan)
        saved = self.saved(engine)
        self.assertEqual(saved["worktree_binding"], {})
        self.assertEqual(saved["original_tasks"], plan["tasks"])
        before = (engine.worktree / "test_hello.py").read_bytes()
        refs = git.git(self.repo, "show-ref")
        registered = git.git(self.repo, "worktree", "list", "--porcelain")
        self.assert_unbound_resume_rejected(engine, backend)
        self.assertEqual((engine.worktree / "test_hello.py").read_bytes(), before)
        self.assertEqual(git.git(self.repo, "show-ref"), refs)
        self.assertEqual(git.git(self.repo, "worktree", "list", "--porcelain"), registered)
