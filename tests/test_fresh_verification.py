from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from harness import git
from harness.config import Config
from harness.util import HarnessError, read_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend


class FreshVerificationTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.engines = []
        self.addCleanup(self.cleanup_checkouts)
        self.config.data["verification"]["fresh_checkout"] = True
        (self.repo / ".gitignore").write_text("__pycache__/\nignored-helper\n")
        git.git(self.repo, "add", ".gitignore")
        git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                "-c", "commit.gpgsign=false", "commit", "-m", "Ignore fixture helper")

    def engine(self, backend=None):
        engine = super().engine(backend)
        self.engines.append(engine)
        return engine

    def cleanup_checkouts(self):
        roots = set()
        for engine in self.engines:
            if not getattr(engine, "directory", None):
                continue
            for artifact in engine.directory.glob("verification/final/*/verification-*/checkout.json"):
                checkout = Path(read_json(artifact)["worktree"])
                root = checkout.parents[1]
                if root.name.startswith("harness-verify-") and not root.is_relative_to(self.repo):
                    roots.add(root)
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)

    @staticmethod
    def plan():
        return read_json(ROOT / "examples" / "plan.json")

    @staticmethod
    def checkout_artifacts(engine):
        return sorted(engine.directory.glob("verification/final/*/verification-*/checkout.json"))

    @staticmethod
    def final_checks(run):
        return [entry for entry in run["dispatches"]
                if entry["role"] == "verification" and entry["task_id"] == "final"]

    def need_ignored_helper(self):
        self.config.data["verification"]["commands"] = [[sys.executable, "-c",
            "from pathlib import Path; from hello import greet; "
            "assert greet('Ada') == 'Hello, Ada!'; assert Path('ignored-helper').read_text() == 'ready'"]]

    @staticmethod
    def helper_worker(backend, kwargs, packet, result):
        if kwargs["role"] == "worker":
            (kwargs["cwd"] / "ignored-helper").write_text("ready")

    def test_ignored_worker_helper_passes_task_but_fails_fresh_final_verification(self):
        self.need_ignored_helper()
        self.config.data["runtime"]["max_replans"] = 0
        backend = ScriptedBackend(self.helper_worker)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "整体验证未通过"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(set(engine.run["accepted"]), {"greet"})
        self.assertEqual(engine.run["status"], "blocked")
        self.assertEqual(engine.run["final_history"][-1]["status"], "verification_failed")
        self.assertFalse(any(call["kind"] == "final_review" for call in backend.calls))
        artifacts = self.checkout_artifacts(engine)
        self.assertEqual(len(artifacts), 1)
        environment = read_json(artifacts[0])
        self.assertEqual(environment["status"], "verification_failed")
        checkout = Path(environment["worktree"])
        self.assertTrue(checkout.is_dir())
        self.assertFalse(checkout.is_relative_to(self.repo))
        self.assertFalse((checkout / "ignored-helper").exists())
        self.assertTrue((engine.worktree / "ignored-helper").exists())
        self.assertEqual(git.head(checkout), engine.run["expected_head"])
        self.assertEqual(git.git(checkout, "branch", "--show-current"), "")

    def test_explicit_setup_builds_ignored_dependency_and_fresh_review_gets_evidence(self):
        self.need_ignored_helper()
        self.config.data["verification"]["setup_commands"] = [[sys.executable, "-c",
            "from pathlib import Path; Path('ignored-helper').write_text('ready')"]]
        backend = ScriptedBackend(self.helper_worker)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        artifact = self.checkout_artifacts(engine)[0]
        environment = read_json(artifact)
        self.assertEqual(environment["status"], "verified")
        self.assertEqual((Path(environment["worktree"]) / "ignored-helper").read_text(), "ready")
        self.assertEqual(read_json(Path(environment["setup_artifact"]))[0]["exit_code"], 0)
        review = next(call["packet"] for call in backend.calls if call["kind"] == "final_review")
        self.assertEqual(review["verification"][0]["checkout_artifact"], str(artifact))
        self.assertEqual(review["verification"][0]["worktree"], environment["worktree"])
        reviewed_environment = read_json(Path(review["verification"][0]["checkout_artifact"]))
        setup_evidence = read_json(Path(reviewed_environment["setup_artifact"]))
        self.assertEqual(setup_evidence[0]["argv"], self.config.data["verification"]["setup_commands"][0])
        self.assertEqual(setup_evidence[0]["exit_code"], 0)
        self.assertEqual(setup_evidence[0]["phase"], "setup")
        self.assertEqual(len(self.final_checks(result)), 2)

    def test_setup_cannot_modify_tracked_checkout_files(self):
        self.config.data["verification"]["setup_commands"] = [[sys.executable, "-c",
            "from pathlib import Path; Path('hello.py').write_text('tampered')"]]
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "依赖准备.*修改.*受检文件"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["status"], "blocked")
        self.assertEqual(engine.run["final_history"][-1]["phase"], "verifying")
        self.assertFalse(any(call["kind"] == "final_review" for call in backend.calls))
        checkout = Path(read_json(self.checkout_artifacts(engine)[0])["worktree"])
        self.assertEqual((checkout / "hello.py").read_text(), "tampered")
        self.assertNotEqual((engine.worktree / "hello.py").read_text(), "tampered")

    def test_failed_setup_resume_uses_new_checkout_same_attempt_and_no_extra_worker(self):
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        marker = Path(external.name) / "environment-ready"
        self.config.data["verification"]["setup_commands"] = [[sys.executable, "-c",
            f"from pathlib import Path; assert Path({str(marker)!r}).exists(), 'environment unavailable'"]]
        self.config.data["runtime"]["max_final_attempts"] = 1
        self.config.data["runtime"]["max_attempts"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "依赖准备失败"):
            engine.start("Implement greet", self.plan())
        first_artifact = self.checkout_artifacts(engine)[0]
        first = read_json(first_artifact)
        self.assertEqual(first["status"], "setup_failed")
        self.assertEqual(engine.run["final_attempts"], 1)
        self.assertEqual(engine.run["final_history"][-1]["phase"], "verifying")
        marker.write_text("ready")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_attempts"], 1)
        self.assertEqual(result["total_attempts"], 1)
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)
        artifacts = self.checkout_artifacts(engine)
        self.assertEqual(len(artifacts), 2)
        second = read_json(artifacts[1])
        self.assertNotEqual(first["worktree"], second["worktree"])
        self.assertTrue(Path(first["worktree"]).is_dir())
        self.assertTrue(Path(second["worktree"]).is_dir())
        self.assertEqual(read_json(first_artifact), first)

    def test_final_reviewer_service_recovery_reuses_finished_fresh_verification(self):
        def fail_once(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and sum(call["kind"] == "final_review" for call in backend.calls) == 1:
                raise HarnessError("Reviewer service unavailable")

        backend = ScriptedBackend(fail_once)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "Reviewer service unavailable"):
            engine.start("Implement greet", self.plan())
        before = self.final_checks(engine.run)
        artifacts = self.checkout_artifacts(engine)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(engine.run["final_history"][-1]["phase"], "reviewing")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_attempts"], 1)
        self.assertEqual(result["total_attempts"], 1)
        self.assertEqual(self.final_checks(result), before)
        self.assertEqual(self.checkout_artifacts(engine), artifacts)

    def test_setup_failure_stops_later_steps_then_resume_starts_setup_on_new_checkout(self):
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        ready = Path(external.name) / "ready"
        sentinel = Path(external.name) / "second-step"
        self.config.data["verification"]["setup_commands"] = [
            [sys.executable, "-c", f"from pathlib import Path; assert Path({str(ready)!r}).exists()"],
            [sys.executable, "-c", f"from pathlib import Path; Path({str(sentinel)!r}).write_text(str(Path.cwd()))"],
        ]
        self.config.data["runtime"]["max_final_attempts"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "依赖准备失败"):
            engine.start("Implement greet", self.plan())
        self.assertFalse(sentinel.exists())
        self.assertFalse(any(call["kind"] == "final_review" for call in backend.calls))
        first_artifact = self.checkout_artifacts(engine)[0]
        first_checkout = read_json(first_artifact)["worktree"]
        failed_setup = read_json(first_artifact.parent / "setup" / "results.json")
        self.assertEqual(len(failed_setup), 1)
        self.assertNotEqual(failed_setup[0]["exit_code"], 0)
        ready.write_text("ready")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_attempts"], 1)
        self.assertEqual(result["total_attempts"], 1)
        second_checkout = read_json(self.checkout_artifacts(engine)[1])["worktree"]
        self.assertNotEqual(first_checkout, second_checkout)
        self.assertEqual(sentinel.read_text(), second_checkout)
        self.assertEqual(read_json(first_artifact.parent / "setup" / "results.json"), failed_setup)

    def test_fresh_git_pointer_tampering_blocks_without_changing_main_branch(self):
        main_head = git.head(self.repo)
        main_git = str(self.repo / ".git")
        self.config.data["verification"]["setup_commands"] = [[sys.executable, "-c",
            f"from pathlib import Path; Path('.git').write_text('gitdir: ' + {main_git!r} + '\\n')"]]
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["status"], "blocked")
        self.assertEqual(git.head(self.repo), main_head)
        self.assertEqual(git.git(self.repo, "branch", "--show-current").strip(), "main")
        self.assertFalse(any(call["kind"] == "final_review" for call in backend.calls))

    def test_fresh_pre_verify_hook_cannot_change_source_run_candidate(self):
        engine = self.engine(ScriptedBackend())
        emit = engine.hooks.emit

        def mutate_source(event, payload, logs):
            emit(event, payload, logs)
            if event == "pre_verify" and payload.get("mode") == "fresh_checkout":
                with (Path(payload["source_worktree"]) / "hello.py").open("a") as stream:
                    stream.write("# source changed during fresh check\n")

        with patch.object(engine.hooks, "emit", side_effect=mutate_source):
            with self.assertRaisesRegex(HarnessError, "原工作树发生变化"):
                engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["status"], "blocked")
        self.assertFalse(any(call["kind"] == "final_review" for call in engine.backend.calls))

    def test_setup_cannot_change_source_run_candidate(self):
        run_root = str(self.repo.resolve() / ".harness" / "worktrees")
        self.config.data["verification"]["setup_commands"] = [[sys.executable, "-c",
            "from pathlib import Path; import subprocess; "
            "trees = subprocess.check_output(['git', 'worktree', 'list', '--porcelain'], text=True); "
            f"source = next(Path(line[9:]) for line in trees.splitlines() if line.startswith('worktree ') and line[9:].startswith({run_root!r})); "
            "file = source / 'hello.py'; file.write_text(file.read_text() + '# source modified by setup\\n')"]]
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "原工作树发生变化"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["status"], "blocked")
        self.assertFalse(any(call["kind"] == "final_review" for call in backend.calls))

    def test_resume_rejects_changes_to_fresh_verification_environment(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        engine.start("Implement greet", self.plan(), pause_after_plan=True)
        saved = deepcopy(self.config.data["verification"])
        alternatives = [{**saved, "fresh_checkout": False},
                        {**saved, "setup_commands": [[sys.executable, "-c", "pass"]]}]
        for verification in alternatives:
            self.config.data["verification"] = verification
            with self.subTest(verification=verification), self.assertRaisesRegex(HarnessError, "配置已改变"):
                self.engine(backend).resume(engine.run["id"], approve_plan=True)
            self.assertEqual(backend.calls, [])

    def test_normal_verification_exposes_ignored_path_inventory_without_claiming_content_coverage(self):
        self.config.data["verification"]["fresh_checkout"] = False
        self.need_ignored_helper()
        backend = ScriptedBackend(self.helper_worker)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.checkout_artifacts(engine), [])
        reviews = [call["packet"] for call in backend.calls if call["role"] == "reviewer"]
        self.assertEqual(len(reviews), 2)
        for packet in reviews:
            summary = packet["ignored_paths"]
            self.assertGreaterEqual(summary["added_count"], 1)
            self.assertEqual(summary["coverage"], "paths_only_not_contents")
            artifact = Path(summary["artifact"])
            inventory = read_json(artifact)
            self.assertIn("ignored-helper", inventory["paths"])
            self.assertIn("ignored-helper", inventory["added"])
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), summary["artifact_digest"])
            self.assertIn("not hashed", inventory["limitation"])


class FreshVerificationConfigTests(unittest.TestCase):
    def test_fresh_checkout_and_setup_require_valid_explicit_configuration(self):
        config = Config.load(ROOT / "harness.toml")
        self.assertIs(config.data["verification"]["fresh_checkout"], False)
        self.assertEqual(config.data["verification"]["setup_commands"], [])
        invalid = [
            {"fresh_checkout": "true"}, {"fresh_checkout": 1},
            {"fresh_checkout": True, "setup_commands": "npm install"},
            {"fresh_checkout": True, "setup_commands": [[]]},
            {"fresh_checkout": True, "setup_commands": [["python3", None]]},
            {"fresh_checkout": False, "setup_commands": [["python3", "-c", "pass"]]},
        ]
        for updates in invalid:
            candidate = deepcopy(config.data)
            candidate["verification"].update(updates)
            with self.subTest(updates=updates), self.assertRaisesRegex(HarnessError, "verification"):
                Config.validate(candidate)


if __name__ == "__main__":
    unittest.main()
