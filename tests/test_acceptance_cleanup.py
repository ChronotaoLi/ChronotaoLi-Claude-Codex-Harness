"""Public cleanup results for an unfinished acceptance with a saved intent."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from test_harness import ROOT, RepoCase, write_project_config
from harness import git
from harness.config import Config
from harness.util import read_json


class AcceptanceCleanupTests(RepoCase):
    def fixture(self, *, zero_diff=False, moved=False):
        # Let a disposable run complete normally. Restore the exact ledger bytes
        # emitted at the pending-intent boundary and position Git for this case.
        if zero_diff:
            (self.repo / "hello.py").write_bytes(b'def greet(name):\n    return f"Hello, {name}!"\n')
            git.git(self.repo, "add", "hello.py")
            git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                    "-c", "commit.gpgsign=false", "commit", "-m", "Already implemented")
        project = write_project_config(self.repo, commands=[
            {"cwd": ".", "argv": [sys.executable, "-B", "-m", "unittest", "-v"]},
        ])
        self.config = Config.load(ROOT / "harness.toml", project)
        engine = self.engine()
        saved = []

        def capture(run):
            if not saved and "intent" in run.get("pending_acceptance", {}):
                saved.append((engine.directory / "state.json").read_bytes())

        engine.context.on_save = capture
        run = engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(saved), 1)
        ledger = engine.directory / "state.json"
        ledger.write_bytes(saved[0])
        state = json.loads(saved[0])
        intent = state["pending_acceptance"]["intent"]
        self.assertEqual(intent["parent"] == intent["commit"], zero_diff)
        self.assertEqual(state["expected_head"], intent["parent"])
        self.assertEqual(git.head(engine.worktree), intent["commit"])
        if not moved and not zero_diff:
            git.git(self.repo, "update-ref", intent["ref"], intent["parent"], intent["commit"])
            git.git(engine.worktree, "read-tree", "--reset", intent["parent"])
        self.assertEqual(git.head(engine.worktree), intent["commit" if moved else "parent"])
        status = git.git(engine.worktree, "--no-optional-locks", "status", "--porcelain=v1")
        self.assertEqual(status.strip(), "" if zero_diff or moved else "?? hello.py")
        self.assertEqual((engine.worktree / "hello.py").read_text(),
                         git.git(self.repo, "show", f"{intent['commit']}:hello.py"))
        return state

    def cleanup(self, state, status, *, remove=False):
        command = [sys.executable, "-B", "-m", "harness", "--repo", str(self.repo),
                   "cleanup-run", state["id"]]
        if remove:
            command.append("--remove")
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, int(status == "blocked"), (result.stdout, result.stderr))
        report = json.loads(result.stdout)
        self.assertEqual((report["id"], report["run_status"], report["remove"]),
                         (state["id"], state["status"], remove))
        self.assertEqual(report["checkouts"], [])
        row = report["worktree"]
        self.assertEqual((row["status"], row["worktree"], row["branch"]),
                         (status, state["worktree"], state["branch"]))

    def git_scene(self, state):
        worktree = Path(state["worktree"])
        return (
            git.git(self.repo, "show-ref", "--heads"),
            git.git(self.repo, "worktree", "list", "--porcelain"),
            (worktree / "hello.py").read_bytes(),
            (Path(state["worktree_binding"]["git_dir"]) / "index").read_bytes(),
        )

    def evidence(self, state):
        directory = self.repo / ".harness" / "runs" / state["id"]
        return {p.relative_to(directory).as_posix(): p.read_bytes()
                for p in directory.rglob("*") if p.is_file()}

    def abandon(self, state):
        scene = self.git_scene(state)
        abandoned = self.engine().abandon(state["id"])
        self.assertEqual(abandoned["status"], "abandoned")
        self.assertEqual(abandoned["pending_acceptance"], state["pending_acceptance"])
        self.assertEqual(abandoned["expected_head"], state["expected_head"])
        self.assertEqual(self.git_scene(abandoned), scene)
        return abandoned

    def preserved(self, state, status):
        scene = self.git_scene(state)
        evidence = self.evidence(state)
        self.assertNotIn("worktree-cleanup.json", evidence)
        for remove in (False, True):
            self.cleanup(state, status, remove=remove)
            self.assertEqual(self.git_scene(state), scene)
            self.assertEqual(self.evidence(state), evidence)

    def test_pending_intent_retains_worktree_even_with_remove(self):
        self.preserved(self.fixture(), "retained")

    def test_abandon_at_parent_preserves_dirty_candidate(self):
        self.preserved(self.abandon(self.fixture()), "blocked")

    def test_abandon_at_parent_removes_clean_zero_diff_worktree_only(self):
        state = self.abandon(self.fixture(zero_diff=True))
        worktree = Path(state["worktree"])
        admin = Path(state["worktree_binding"]["git_dir"])
        sibling = self.outside / "unrelated"
        git.git(self.repo, "worktree", "add", "--detach", str(sibling), "main")
        sentinel = sibling / "keep.txt"
        sentinel.write_bytes(b"unrelated checkout\n")
        before = self.evidence(state)
        scene = self.git_scene(state)
        refs, registrations, _, _ = scene
        intent = state["pending_acceptance"]["intent"]
        self.assertIn(f"{intent['parent']} {intent['ref']}", refs.splitlines())
        self.assertIn(f"worktree {worktree}\n", registrations)
        self.assertNotIn("worktree-cleanup.json", before)

        self.cleanup(state, "eligible")
        self.assertEqual(self.git_scene(state), scene)
        self.assertEqual(self.evidence(state), before)

        self.cleanup(state, "removed", remove=True)
        self.assertFalse(worktree.exists())
        self.assertFalse(admin.exists())
        self.assertEqual(git.git(self.repo, "show-ref", "--heads"), refs)
        after = git.git(self.repo, "worktree", "list", "--porcelain")
        expected = [entry for entry in registrations.split("\n\n")
                    if not entry.startswith(f"worktree {worktree}\n")]
        self.assertEqual(after.split("\n\n"), expected)
        self.assertEqual(sentinel.read_bytes(), b"unrelated checkout\n")
        remaining = self.evidence(state)
        receipt = json.loads(remaining.pop("worktree-cleanup.json"))
        self.assertEqual((receipt["status"], receipt["head"], receipt["worktree"], receipt["branch"]),
                         ("removed", state["expected_head"], str(worktree), state["branch"]))
        self.assertEqual(receipt["state_digest"], hashlib.sha256(before["state.json"]).hexdigest())
        self.assertEqual(remaining, before)

    def test_abandon_at_intended_commit_preserves_clean_worktree(self):
        self.preserved(self.abandon(self.fixture(moved=True)), "blocked")
