import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import git, mcp_server
from harness.adapters import DemoBackend
from harness.config import Config
from harness.engine import Engine
from harness.util import HarnessError


ROOT = Path(__file__).resolve().parents[1]


class BoundaryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git.git(self.repo, "init", "-b", "main")
        (self.repo / "base.txt").write_text("fixture\n", encoding="utf-8")
        git.git(self.repo, "add", ".")
        self.commit("fixture")
        self.config = Config.load(ROOT / "harness.toml")
        self.config.data["verification"]["commands"] = [[sys.executable, "-c", "pass"]]

    def commit(self, message):
        git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                "-c", "commit.gpgsign=false", "commit", "-m", message)

    def plan(self):
        return {"tasks": [{"id": "greet", "title": "Implement greet", "description": "Implement greet",
                           "depends_on": [], "allowed_paths": ["hello.py"],
                           "acceptance": ["Greet a supplied name"], "skills": []}]}

    def test_scope_rejects_ignored_private_state_in_run_worktree(self):
        git.ignore_state(self.repo)
        private = self.repo / ".harness"
        private.mkdir()
        (private / "operator-state").write_text("preserve\n", encoding="utf-8")
        before = git.snapshot(self.repo)
        worktree = private / "worktrees" / "run_test"
        git.git(self.repo, "worktree", "add", "-b", "harness/test", str(worktree), "HEAD")
        hidden = worktree / ".harness"
        hidden.mkdir()
        (hidden / "output").write_text("not reviewed\n", encoding="utf-8")
        self.assertEqual(git.changed(worktree), [])
        with self.assertRaisesRegex(HarnessError, "受保护路径.*harness"):
            git.check_scope(worktree, ["**"])
        self.assertEqual(git.snapshot(self.repo), before)
        self.assertEqual((private / "operator-state").read_text(encoding="utf-8"), "preserve\n")

    def test_private_state_guard_rejects_files_and_live_or_dangling_symlinks(self):
        hidden = self.repo / ".harness"
        hidden.write_text("protected\n", encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "受保护"):
            git.check_worktree_state(self.repo)
        hidden.unlink()
        for target in (self.root, self.root / "missing"):
            with self.subTest(target=target):
                hidden.symlink_to(target, target_is_directory=True)
                try:
                    with self.assertRaisesRegex(HarnessError, "受保护"):
                        git.check_worktree_state(self.repo)
                finally:
                    hidden.unlink()

    def test_snapshot_hashes_same_size_content_even_with_restored_mtime(self):
        path = self.repo / "base.txt"
        previous = path.stat()
        before = git.snapshot(self.repo)
        path.write_text("changed\n", encoding="utf-8")
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assertEqual(path.stat().st_size, previous.st_size)
        self.assertEqual(path.stat().st_mtime_ns, previous.st_mtime_ns)
        after = git.snapshot(self.repo)
        self.assertEqual(after["head"], before["head"])
        self.assertEqual(after["index"], before["index"])
        self.assertNotEqual(after["files"]["base.txt"], before["files"]["base.txt"])

    def test_assume_unchanged_flag_cannot_hide_snapshot_content_change(self):
        git.git(self.repo, "update-index", "--assume-unchanged", "base.txt")
        before = git.snapshot(self.repo)
        (self.repo / "base.txt").write_text("changed\n", encoding="utf-8")
        # Scope uses Git change enumeration, which honors this flag. Content
        # snapshot gates must independently read every tracked file regardless.
        self.assertEqual(git.check_scope(self.repo, ["base.txt"]), [])
        after = git.snapshot(self.repo)
        self.assertEqual(after["index"], before["index"])
        self.assertNotEqual(after["files"]["base.txt"], before["files"]["base.txt"])

    def test_removal_rejects_hidden_index_flags_before_touching_files(self):
        for flag in ("assume-unchanged", "skip-worktree"):
            with self.subTest(flag=flag):
                git.git(self.repo, "update-index", "--" + flag, "base.txt")
                try:
                    with self.assertRaisesRegex(HarnessError, "assume-unchanged/skip-worktree"):
                        git.assert_removable_worktree(self.repo)
                    (self.repo / "base.txt").write_text("unsaved\n", encoding="utf-8")
                    self.assertTrue(git.clean(self.repo))
                    with self.assertRaisesRegex(HarnessError, "assume-unchanged/skip-worktree"):
                        git.assert_removable_worktree(self.repo)
                    self.assertEqual((self.repo / "base.txt").read_text(encoding="utf-8"), "unsaved\n")
                finally:
                    git.git(self.repo, "update-index", "--no-" + flag, "base.txt")
                    git.git(self.repo, "reset", "--hard", "HEAD")

    def test_removal_hashes_same_size_bytes_with_restored_mtime(self):
        git.assert_removable_worktree(self.repo)
        path = self.repo / "base.txt"
        previous = path.stat()
        path.write_text("changed\n", encoding="utf-8")
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assertEqual(path.stat().st_size, previous.st_size)
        self.assertEqual(path.stat().st_mtime_ns, previous.st_mtime_ns)
        with self.assertRaisesRegex(HarnessError, "内容与 HEAD 不同"):
            git.assert_removable_worktree(self.repo)
        self.assertEqual(path.read_text(encoding="utf-8"), "changed\n")

    def test_removal_rejects_ignored_untracked_and_staged_data(self):
        exclude = self.repo / ".git/info/exclude"
        exclude.write_text("ignored.log\n", encoding="utf-8")
        for name in ("ignored.log", "untracked.log"):
            path = self.repo / name
            path.write_text("keep this evidence\n", encoding="utf-8")
            try:
                with self.subTest(name=name), self.assertRaisesRegex(HarnessError, "未保存文件"):
                    git.assert_removable_worktree(self.repo)
                self.assertTrue(path.is_file())
            finally:
                path.unlink()
        path = self.repo / "base.txt"
        original = path.read_bytes()
        path.write_text("staged content\n", encoding="utf-8")
        git.git(self.repo, "add", "base.txt")
        path.write_bytes(original)
        with self.assertRaisesRegex(HarnessError, "索引与 HEAD 不同"):
            git.assert_removable_worktree(self.repo)

    def test_removal_rejects_deleted_files_executable_bits_and_replacement_symlinks(self):
        path = self.repo / "base.txt"
        for change in ("delete", "execute", "symlink"):
            with self.subTest(change=change):
                if change == "delete":
                    path.unlink()
                elif change == "execute":
                    path.chmod(path.stat().st_mode | 0o100)
                else:
                    path.unlink()
                    path.symlink_to(self.root / "outside")
                try:
                    with self.assertRaisesRegex(HarnessError, "不能移除"):
                        git.assert_removable_worktree(self.repo)
                finally:
                    git.git(self.repo, "reset", "--hard", "HEAD")

    def test_removal_reads_dangling_link_targets_and_binary_blob_bytes(self):
        link = self.repo / "link"
        link.symlink_to("missing-original")
        (self.repo / "base.txt").write_bytes(b"binary\x00\xff\xfe\n")
        git.git(self.repo, "add", ".")
        self.commit("binary content and dangling link")
        git.assert_removable_worktree(self.repo)
        link.unlink()
        link.symlink_to("missing-changed")
        with self.assertRaisesRegex(HarnessError, "内容与 HEAD 不同"):
            git.assert_removable_worktree(self.repo)

    def test_removal_rejects_gitlinks_even_when_submodule_directory_is_absent(self):
        git.git(self.repo, "update-index", "--add", "--cacheinfo", "160000", git.head(self.repo), "vendor/lib")
        self.commit("uninitialized submodule")
        with self.assertRaisesRegex(HarnessError, "submodule"):
            git.assert_removable_worktree(self.repo)

    def test_worker_private_state_stops_before_review_or_acceptance(self):
        class HiddenStateBackend(DemoBackend):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs["role"] == "worker":
                    hidden = kwargs["cwd"] / ".harness"
                    hidden.mkdir()
                    (hidden / "output").write_text("hidden\n", encoding="utf-8")
                return result

        engine = Engine(self.config, self.repo, backend=HiddenStateBackend())
        with self.assertRaisesRegex(HarnessError, "受保护"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual([d["role"] for d in engine.run["dispatches"]], ["worker"])
        self.assertEqual(git.head(engine.worktree), engine.run["base_commit"])
        self.assertTrue((engine.worktree / ".harness/output").is_file())

    def assert_final_private_state_blocked(self, engine):
        with self.assertRaisesRegex(HarnessError, "受保护"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(set(engine.run["accepted"]), {"greet"})
        self.assertNotEqual(engine.run["status"], "completed")
        self.assertIn("reviewer:final_review:final", engine.run["review_receipts"])
        dispatch_count = len(engine.run["dispatches"])
        resumed = Engine(self.config, self.repo, backend=DemoBackend())
        with self.assertRaisesRegex(HarnessError, "受保护"):
            resumed.resume(engine.run["id"])
        self.assertEqual(len(resumed.run["dispatches"]), dispatch_count)
        self.assertEqual(resumed.run["final_attempts"], 1)

    def test_final_reviewer_private_state_cannot_complete_or_reroll(self):
        class HiddenFinalBackend(DemoBackend):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs["kind"] == "final_review":
                    (kwargs["cwd"] / ".harness").mkdir()
                return result

        self.assert_final_private_state_blocked(Engine(self.config, self.repo, backend=HiddenFinalBackend()))

    def test_final_review_hook_private_state_cannot_complete_or_reroll(self):
        engine = Engine(self.config, self.repo, backend=DemoBackend())
        original = engine.hooks.emit

        def emit(event, payload, logs):
            original(event, payload, logs)
            if event == "post_agent" and payload.get("kind") == "final_review":
                (engine.worktree / ".harness").mkdir()

        with patch.object(engine.hooks, "emit", side_effect=emit):
            self.assert_final_private_state_blocked(engine)

    def test_submodule_repository_rejected_before_worktree_creation(self):
        # A gitlink with no directory or .gitmodules still represents a submodule.
        git.git(self.repo, "update-index", "--add", "--cacheinfo", "160000", git.head(self.repo), "vendor/lib")
        self.commit("uninitialized submodule")
        self.assertFalse((self.repo / "vendor/lib").exists())
        with self.assertRaisesRegex(HarnessError, "submodule.*vendor/lib"):
            git.reject_submodules(self.repo)
        # Git reports a missing gitlink as deleted; an empty placeholder is a
        # clean, uninitialized submodule and reaches the start preflight gate.
        (self.repo / "vendor/lib").mkdir(parents=True)
        self.assertTrue(git.clean(self.repo))
        engine = Engine(self.config, self.repo, backend=DemoBackend())
        with self.assertRaisesRegex(HarnessError, "submodule.*vendor/lib"):
            engine.start("Implement greet", self.plan())
        self.assertIsNone(engine.run)
        self.assertFalse((engine.state / "worktrees").exists())
        self.assertFalse((engine.state / "runs").exists())
        self.assertEqual(git.git(self.repo, "branch", "--list", "harness/*"), "")

    def test_gitmodules_without_gitlinks_can_start_a_run(self):
        (self.repo / ".gitmodules").write_text("# Historical configuration only\n", encoding="utf-8")
        git.git(self.repo, "add", ".gitmodules")
        self.commit("retain old submodule configuration")
        engine = Engine(self.config, self.repo, backend=DemoBackend())
        result = engine.start("Implement greet", self.plan(), pause_after_plan=True)
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(result["dispatches"], [])
        self.assertTrue(engine.worktree.is_dir())


class ConfigurationDiagnosticsTests(unittest.TestCase):
    def test_shared_configuration_is_validated_before_asset_access(self):
        original = Config.load(ROOT / "harness.toml").data
        for invalid in (None, [], "shared"):
            data = copy.deepcopy(original)
            data["shared"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(HarnessError, "shared"):
                Config.validate(data)
        data = copy.deepcopy(original)
        del data["shared"]
        with self.assertRaisesRegex(HarnessError, "shared"):
            Config.validate(data)
        for key in ("rules", "skills", "mcp", "hooks"):
            for invalid in (None, [], {}, 3, False, "", " ", "path\0suffix"):
                data = copy.deepcopy(original)
                data["shared"][key] = invalid
                with self.subTest(key=key, invalid=invalid), self.assertRaisesRegex(HarnessError, f"shared\\.{key}"):
                    Config.validate(data)
            data = copy.deepcopy(original)
            del data["shared"][key]
            with self.subTest(missing=key), self.assertRaisesRegex(HarnessError, f"shared\\.{key}"):
                Config.validate(data)


class MCPDiagnosticsTests(unittest.TestCase):
    def exchange(self, requests):
        with tempfile.TemporaryDirectory() as folder:
            output = io.StringIO()
            argv = ["mcp_entry.py", "--state", folder, "--skills", str(ROOT / "shared/skills")]
            source = io.StringIO("".join(json.dumps(request) + "\n" for request in requests))
            with patch.object(sys, "argv", argv), patch.object(sys, "stdin", source), patch.object(sys, "stdout", output):
                mcp_server.main()
            return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_unknown_method_returns_method_not_found_and_server_continues(self):
        responses = self.exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "unknown/method"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ])
        self.assertEqual(responses[0]["id"], 1)
        self.assertEqual(responses[0]["error"]["code"], -32601)
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 2, "result": {}})

    def test_internal_fault_does_not_become_method_not_found(self):
        with patch("harness.mcp_server.handle", side_effect=RuntimeError("unexpected fault")):
            responses = self.exchange([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        self.assertEqual(responses[0]["error"]["code"], -32603)


if __name__ == "__main__":
    unittest.main()
