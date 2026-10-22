import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.adapters import CLIBackend
from harness.config import Config
from harness.util import HarnessError


ROOT = Path(__file__).resolve().parents[1]


class AdapterPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = Config.load(ROOT / "harness.toml")

    def command(self, cwd, role="worker"):
        job = self.root / "dispatch"
        job.mkdir(exist_ok=True)
        argv = CLIBackend(self.config, self.root / ".harness").command(
            role, self.config.role("commander", "fable"), cwd, job, {}, None)
        settings = json.loads(argv[argv.index("--settings") + 1])
        return argv, settings["permissions"]["allow"]

    def edit_rule(self, cwd):
        argv, allow = self.command(cwd)
        self.assertNotIn("Edit", allow)
        self.assertNotIn("Write", allow)
        self.assertFalse(any(rule.startswith("Write(") for rule in allow))
        edits = [rule for rule in allow if rule.startswith("Edit(")]
        self.assertEqual(len(edits), 1)
        self.assertTrue(edits[0].startswith("Edit(//"))
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Edit,Write,Glob,Grep,Bash")
        self.assertTrue({"Read", "Glob", "Grep"}.issubset(allow))
        return edits[0]

    def test_write_rule_anchors_to_resolved_worktree_through_symlink(self):
        worktree = self.root / "worktree"
        worktree.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(worktree, target_is_directory=True)
        self.assertEqual(self.edit_rule(alias), "Edit(/" + worktree.as_posix() + "/**)")

    def test_literal_path_patterns_do_not_grant_writes_to_siblings(self):
        # Git exercises the documented gitignore syntax independently of our
        # escaping code. This checks emitted rules, not a live Claude process.
        matcher = self.root / "matcher"
        matcher.mkdir()
        subprocess.run(["git", "init", "-q", str(matcher)], check=True, capture_output=True)
        names = ("work[ab]", "work*", "work?", "work\\tree", "work (a,b)", "#work! ")
        for name in names:
            with self.subTest(name=name):
                worktree = self.root / name
                worktree.mkdir()
                rule = self.edit_rule(worktree)
                # Strip only the extra slash that denotes Claude's filesystem
                # anchor, leaving a normal root-anchored gitignore pattern.
                (matcher / ".gitignore").write_text(rule[len("Edit(/"):-1] + "\n")
                candidates = [worktree / "a.py", worktree / "nested" / "b.py",
                              self.root / "worka" / "a.py", self.root / "work-tree" / "a.py",
                              self.root / (name + "-other") / "a.py", self.root / "a.py"]
                for index, candidate in enumerate(candidates):
                    relative = candidate.as_posix().lstrip("/")
                    result = subprocess.run(["git", "-C", str(matcher), "check-ignore", "--no-index",
                                             "-q", "--", relative], capture_output=True)
                    self.assertEqual(result.returncode, 0 if index < 2 else 1,
                                     (name, relative, result.stderr))

    def test_newline_paths_cannot_inject_permission_patterns(self):
        for name in ("work\n**", "work\r**"):
            with self.subTest(name=name), self.assertRaisesRegex(HarnessError, "路径不能包含换行符"):
                self.command(self.root / name)

    def test_readonly_roles_never_receive_scoped_or_bare_write_grants(self):
        for role in ("commander", "reviewer"):
            with self.subTest(role=role):
                argv, allow = self.command(self.root, role)
                self.assertFalse(any(rule == "Edit" or rule.startswith("Edit(") or
                                     rule == "Write" or rule.startswith("Write(") for rule in allow))
                self.assertTrue({"Read", "Glob", "Grep"}.issubset(allow))
                self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep,Bash")


if __name__ == "__main__":
    unittest.main()
