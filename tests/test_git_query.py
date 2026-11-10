"""Offline behavior of the built-in read-only git_query tool on real temporary repositories."""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

from harness import git
from harness import git_query
from harness.config import Config
from harness.git_query import covers, parse_git_query, run_git_query
from harness.mcp_server import handle
from harness.shared import mcp_configs
from harness.util import HarnessError, write_json
from test_harness import ROOT

SHA = "0123456789abcdef0123456789abcdef01234567"
USER = ("-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false")


def clean_env():
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def run_git(cwd, *args, check=True):
    process = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, env=clean_env(), timeout=60)
    if check and process.returncode:
        raise AssertionError(process.stderr.decode(errors="replace"))
    return process


def stdout_bytes(result):
    if result["stdout_encoding"] == "base64":
        return base64.b64decode(result["stdout"])
    return result["stdout"].encode("utf-8")


# Acceptance item 3 and the R1 additions: each is rejected and never covered.
REJECTED = {
    "output_option": ["git", "ls-files", "--output=marker", "--", "a"],
    "log_output_option": ["git", "log", "-1", "--format=%H", "--output=marker", "--", "a"],
    "config_option": ["git", "-c", "core.pager=cat", "ls-files", "--", "a"],
    "ext_diff": ["git", "log", "-1", "--format=%H", "--ext-diff", "--", "a"],
    "reflog_rev": ["git", "rev-parse", "@{-1}"],
    "reflog_suffix": ["git", "ls-tree", "-r", "HEAD@{1}", "--", "a"],
    "parent_segment": ["git", "ls-files", "--", "../outside"],
    "inner_parent_segment": ["git", "ls-files", "--", "d/../a"],
    "blob_parent_segment": ["git", "show", "HEAD:../outside"],
    "glob_magic": ["git", "ls-files", "--", ":(glob)*.py"],
    "absolute_path": ["git", "ls-files", "--", "/etc/passwd"],
    "absolute_hash_path": ["git", "hash-object", "--no-filters", "--", "/etc/passwd"],
    "shell_pipeline": ["sh", "-c", "git ls-files | head"],
    "argv_pipeline": ["git", "ls-files", "--", "a", "|", "head"],
    "argv_redirect": ["git", "ls-files", "--", "a", ">", "out"],
    "argv_stderr_redirect": ["git", "ls-files", "--", "a", "2>&1"],
    "extra_option_before_separator": ["git", "ls-files", "-z", "--cached", "--", "a"],
    "extra_ls_tree_option": ["git", "ls-tree", "-r", "--name-only", "HEAD", "--", "a"],
    "dash_path": ["git", "ls-files", "--", "-z"],
    "trailing_option": ["git", "ls-files", "--", "a", "--deleted"],
    "nul_argument": ["git", "ls-files", "--", "a\0b"],
    "too_many_paths": ["git", "ls-files", "--", *[f"p{i}" for i in range(129)]],
    "empty_path": ["git", "ls-files", "--", ""],
    "empty_segment": ["git", "ls-files", "--", "d//f"],
    "trailing_slash": ["git", "ls-files", "--", "d/"],
    "dot_segment": ["git", "ls-files", "--", "./a"],
    "duplicate_separator": ["git", "ls-files", "--", "--", "a"],
    "misplaced_separator": ["git", "ls-tree", "--", "-r", "HEAD", "a"],
    "separator_in_rev_parse": ["git", "rev-parse", "--", "HEAD"],
    "missing_separator": ["git", "ls-files", "a"],
    "rev_suffix_junk": ["git", "rev-parse", "HEAD~1junk"],
    "rev_with_path": ["git", "ls-tree", "-r", "HEAD:a", "--", "a"],
    "rev_parse_with_path": ["git", "rev-parse", "HEAD:a"],
    "rev_five_digits": ["git", "rev-parse", "HEAD~12345"],
    "rev_without_count": ["git", "rev-parse", "HEAD~"],
    "branch_name": ["git", "rev-parse", "main"],
    "range": ["git", "rev-parse", "HEAD..HEAD~1"],
    "symmetric_range": ["git", "ls-tree", "-r", "HEAD...HEAD~1", "--", "a"],
    "short_hex": ["git", "rev-parse", "abcdef"],
    "long_hex": ["git", "rev-parse", "a" * 65],
    "rev_newline": ["git", "rev-parse", "HEAD\n"],
    "absolute_git": ["/usr/bin/git", "ls-files", "--", "a"],
    "flag_order": ["git", "ls-files", "--deleted", "-z", "--", "a"],
    "log_two_paths": ["git", "log", "-1", "--format=%H", "--", "a", "b"],
    "log_other_format": ["git", "log", "-1", "--format=%an", "--", "a"],
    "show_without_path": ["git", "show", "HEAD"],
    "show_textconv": ["git", "show", "--textconv", "HEAD:a"],
    "show_empty_path": ["git", "show", "HEAD:"],
    "cat_file_pretty": ["git", "cat-file", "-p", "HEAD"],
    "cat_file_blob": ["git", "cat-file", "blob", "HEAD:a"],
    "hash_object_write": ["git", "hash-object", "-w", "--", "a"],
    "hash_object_stdin": ["git", "hash-object", "--no-filters", "--stdin"],
    "hash_object_two_paths": ["git", "hash-object", "--no-filters", "--", "a", "b"],
    "lone_surrogate": ["git", "ls-files", "--", "\ud800"],
    "subcommand_missing": ["git"],
    "unknown_subcommand": ["git", "grep", "-e", "x", "--", "a"],
}


class GitQueryParserTests(unittest.TestCase):
    def test_every_table_form_parses_to_a_normalized_description(self):
        accepted = {
            ("git", "ls-files", "--", "a"): {"form": "ls-files", "options": [], "paths": ["a"]},
            ("git", "ls-files", "-z", "--", "x"): {"form": "ls-files", "options": ["-z"], "paths": ["x"]},
            ("git", "ls-files", "-z", "--deleted", "--", "a", "d/f"):
                {"form": "ls-files", "options": ["-z", "--deleted"], "paths": ["a", "d/f"]},
            ("git", "ls-files", "--deleted", "--", "a*b"): {"form": "ls-files", "options": ["--deleted"], "paths": ["a*b"]},
            ("git", "ls-tree", "-r", SHA, "--", "a"): {"form": "ls-tree", "rev": SHA, "paths": ["a"]},
            ("git", "rev-parse", "HEAD"): {"form": "rev-parse", "verify": False, "rev": "HEAD"},
            ("git", "rev-parse", "--verify", "HEAD~1"): {"form": "rev-parse", "verify": True, "rev": "HEAD~1"},
            ("git", "rev-parse", "HEAD^2"): {"form": "rev-parse", "verify": False, "rev": "HEAD^2"},
            ("git", "rev-parse", "ABCDEF0"): {"form": "rev-parse", "verify": False, "rev": "ABCDEF0"},
            ("git", "log", "-1", "--format=%H", "--", "d/f"): {"form": "log", "path": "d/f"},
            ("git", "show", "HEAD~9999:d/f"): {"form": "show", "rev": "HEAD~9999", "path": "d/f"},
            ("git", "show", "HEAD:a:b"): {"form": "show", "rev": "HEAD", "path": "a:b"},
            ("git", "hash-object", "--no-filters", "--", "d/f"): {"form": "hash-object", "path": "d/f"},
            ("git", "cat-file", "-e", SHA): {"form": "cat-file", "mode": "-e", "rev": SHA, "path": None},
            ("git", "cat-file", "-t", "HEAD:d"): {"form": "cat-file", "mode": "-t", "rev": "HEAD", "path": "d"},
            ("git", "cat-file", "-s", "HEAD:d/f"): {"form": "cat-file", "mode": "-s", "rev": "HEAD", "path": "d/f"},
        }
        for argv, expected in accepted.items():
            with self.subTest(argv=argv):
                query, reason = parse_git_query(list(argv))
                self.assertIsNone(reason)
                self.assertEqual(query, {**expected, "argv": list(argv)})
                self.assertTrue(covers(list(argv)))
        limit = ["git", "ls-tree", "-r", "HEAD", "--", *[f"p{i}" for i in range(128)]]
        self.assertTrue(covers(limit))

    def test_out_of_table_argv_is_rejected_with_reason_and_never_covered(self):
        for name, argv in REJECTED.items():
            with self.subTest(case=name):
                query, reason = parse_git_query(argv)
                self.assertIsNone(query)
                self.assertTrue(reason)
                self.assertFalse(covers(argv))
        for value in (None, "git ls-files -- a", [], ["git", 1], [["git"]]):
            self.assertFalse(covers(value))


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run_git(self.repo, "init", "-q", "-b", "main")
        self.files = {"a*b": b"star\n", "axb": b"x\n", "d/f": b"first\n", "d/g.txt": b"line\r\n",
                      "t.bin": b"RAW bytes\n", "bin.dat": bytes(range(256)) * 4,
                      ".gitattributes": b"t.bin diff=upper\nd/g.txt text eol=crlf\n"}
        for name, data in self.files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_bytes(data)
        run_git(self.repo, "add", ".")
        run_git(self.repo, *USER, "commit", "-qm", "base")
        (self.repo / "d" / "f").write_bytes(b"second\n")
        run_git(self.repo, *USER, "commit", "-qam", "change")
        self.worktree = self.root / "wt"
        run_git(self.repo, "worktree", "add", "-q", "-b", "run", str(self.worktree))
        self.binding = git.capture_worktree_binding(self.worktree, "run")
        self.head = run_git(self.worktree, "rev-parse", "HEAD").stdout.decode().strip()

    def query(self, argv, **kwargs):
        parsed, reason = parse_git_query(argv)
        self.assertIsNone(reason, argv)
        return run_git_query(parsed, self.worktree, self.binding, **kwargs)

    def direct(self, argv):
        return run_git(self.worktree, *argv, check=False)

    def sentinel_script(self, name):
        marker = self.root / f"{name}-ran"
        script = self.root / f"{name}.sh"
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        script.chmod(0o755)
        return script, marker


class GitQueryExecutionTests(Fixture):
    def test_each_form_matches_direct_git_output(self):
        (self.worktree / "axb").unlink()
        cases = [
            (["git", "ls-files", "--", "d", "axb"], ["ls-files", "--", "d", "axb"]),
            (["git", "ls-files", "-z", "--", "d"], ["ls-files", "-z", "--", "d"]),
            (["git", "ls-files", "-z", "--deleted", "--", "axb", "d"], ["ls-files", "-z", "--deleted", "--", "axb", "d"]),
            (["git", "ls-tree", "-r", self.head, "--", "d", "t.bin"], ["ls-tree", "-r", self.head, "--", "d", "t.bin"]),
            (["git", "rev-parse", "HEAD"], ["rev-parse", "HEAD"]),
            (["git", "rev-parse", "--verify", "HEAD~1"], ["rev-parse", "--verify", "HEAD~1"]),
            (["git", "rev-parse", "--verify", self.head[:7]], ["rev-parse", "--verify", self.head[:7]]),
            (["git", "rev-parse", "--verify", "HEAD~40"], ["rev-parse", "--verify", "HEAD~40"]),
            (["git", "log", "-1", "--format=%H", "--", "t.bin"], ["log", "-1", "--format=%H", "--", "t.bin"]),
            (["git", "show", "HEAD~1:d/f"], ["show", "HEAD~1:d/f"]),
            (["git", "show", f"{self.head}:bin.dat"], ["show", f"{self.head}:bin.dat"]),
            (["git", "hash-object", "--no-filters", "--", "d/g.txt"], ["hash-object", "--no-filters", "--", "d/g.txt"]),
            (["git", "cat-file", "-e", "HEAD:d/f"], ["cat-file", "-e", "HEAD:d/f"]),
            (["git", "cat-file", "-e", "HEAD:missing"], ["cat-file", "-e", "HEAD:missing"]),
            (["git", "cat-file", "-t", "HEAD:d"], ["cat-file", "-t", "HEAD:d"]),
            (["git", "cat-file", "-t", self.head], ["cat-file", "-t", self.head]),
            (["git", "cat-file", "-s", "HEAD:bin.dat"], ["cat-file", "-s", "HEAD:bin.dat"]),
        ]
        forms = set()
        for argv, plain in cases:
            with self.subTest(argv=argv):
                result = self.query(argv)
                expected = self.direct(plain)
                forms.add(result["form"])
                self.assertEqual(result["exit_code"], expected.returncode)
                self.assertEqual(stdout_bytes(result), expected.stdout)
                self.assertTrue(result["observed_complete"])
                self.assertFalse(result["stdout_truncated"])
                self.assertEqual(result["stdout_bytes"], len(expected.stdout))
                self.assertEqual(result["stdout_sha256"], hashlib.sha256(expected.stdout).hexdigest())
        self.assertEqual(forms, {"ls-files", "ls-tree", "rev-parse", "log", "show", "hash-object", "cat-file"})
        self.assertEqual(self.query(["git", "rev-parse", "--verify", "HEAD~40"])["exit_code"], 128)

    def test_paths_are_literal_even_with_glob_characters(self):
        globbed = self.direct(["ls-files", "--", "a*b"]).stdout
        self.assertEqual(globbed, b"a*b\naxb\n")
        self.assertEqual(stdout_bytes(self.query(["git", "ls-files", "--", "a*b"])), b"a*b\n")
        self.assertEqual(stdout_bytes(self.query(["git", "ls-files", "--", "a?b"])), b"")
        self.assertEqual(stdout_bytes(self.query(["git", "ls-files", "--", "[a]xb"])), b"")
        tree = stdout_bytes(self.query(["git", "ls-tree", "-r", "HEAD", "--", "a*b"]))
        self.assertTrue(tree.endswith(b"\ta*b\n") and tree.count(b"\n") == 1)
        base = run_git(self.worktree, "rev-parse", "HEAD~1").stdout
        self.assertEqual(stdout_bytes(self.query(["git", "log", "-1", "--format=%H", "--", "a*b"])), base)

    def test_show_returns_raw_blob_bytes_without_running_textconv(self):
        script, marker = self.sentinel_script("textconv")
        run_git(self.repo, "config", "diff.upper.textconv", str(script))
        run_git(self.worktree, "cat-file", "--textconv", "HEAD:t.bin", check=False)
        self.assertTrue(marker.exists(), "fixture textconv must be active")
        marker.unlink()
        result = self.query(["git", "show", "HEAD:t.bin"])
        self.assertEqual(stdout_bytes(result), self.files["t.bin"])
        self.assertEqual(result["git_args"], ["cat-file", "blob", "HEAD:t.bin"])
        self.assertFalse(marker.exists())

    def test_output_over_limit_is_truncated_with_full_size_and_digest(self):
        text = "€".encode() * 400000
        binary = bytes(range(256)) * 5000
        (self.repo / "big.txt").write_bytes(text)
        (self.repo / "big.dat").write_bytes(binary)
        run_git(self.repo, "add", "big.txt", "big.dat")
        run_git(self.repo, *USER, "commit", "-qm", "big")
        run_git(self.worktree, "merge", "-q", "--ff-only", "main")
        self.binding = git.capture_worktree_binding(self.worktree, "run")
        for name, content, encoding in (("big.txt", text, "utf-8"), ("big.dat", binary, "base64")):
            with self.subTest(name=name):
                result = self.query(["git", "show", f"HEAD:{name}"])
                self.assertEqual(result["exit_code"], 0)
                # Past the retain limit the stream is still read to its end, so
                # size and digest identify the complete output.
                self.assertTrue(result["observed_complete"])
                self.assertIsNone(result["terminated"])
                self.assertTrue(result["stdout_truncated"])
                self.assertEqual((result["retain_limit_bytes"], result["observe_limit_bytes"]),
                                 (1024 * 1024, 64 * 1024 * 1024))
                self.assertEqual(result["stdout_bytes"], len(content))
                self.assertEqual(result["stdout_sha256"], hashlib.sha256(content).hexdigest())
                self.assertEqual(result["stdout_encoding"], encoding)
                returned = stdout_bytes(result)
                self.assertLessEqual(len(returned), git_query.MAX_OUTPUT_BYTES)
                self.assertGreater(len(returned), git_query.MAX_OUTPUT_BYTES - 4)
                self.assertEqual(returned, content[:len(returned)])
                self.assertEqual(result["stdout_returned_bytes"], len(returned))

    def project(self, streams, code, terminated):
        query = parse_git_query(["git", "rev-parse", "HEAD"])[0]
        return git_query._project(query, "git", ["rev-parse", "HEAD"], streams, code, terminated)

    def assert_exits(self, pid):
        deadline = time.monotonic() + 10
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            self.assertLess(time.monotonic(), deadline, f"process {pid} survived the process-group kill")
            time.sleep(0.05)

    def test_output_past_the_retain_limit_keeps_being_read_until_the_timeout(self):
        # The review probe: a little more than the retain limit, then flush, a
        # marker and sleep. Reading continues past 1 MiB, so the marker is
        # written and the query ends at the timeout, observed but incomplete.
        size = git_query.MAX_OUTPUT_BYTES + 65536
        marker = self.root / "after-output"
        script = ("import sys, time; sys.stdout.write('z' * %d); sys.stdout.flush(); "
                  "open(sys.argv[1], 'w').close(); time.sleep(30)" % size)
        began = time.monotonic()
        streams, code, terminated = git_query._capture([sys.executable, "-c", script, str(marker)],
                                                       self.root, clean_env(), 2)
        self.assertGreaterEqual(time.monotonic() - began, 2)
        self.assertEqual((code, terminated), (None, "timeout"))
        self.assertTrue(marker.exists())
        result = self.project(streams, code, terminated)
        self.assertFalse(result["observed_complete"])
        self.assertTrue(result["stdout_truncated"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["stdout_bytes"], size)
        self.assertEqual(result["stdout_sha256"], hashlib.sha256(b"z" * size).hexdigest())
        self.assertEqual(result["stdout_returned_bytes"], git_query.MAX_OUTPUT_BYTES)
        self.assertEqual(result["retain_limit_bytes"], git_query.MAX_OUTPUT_BYTES)

    def test_output_past_the_observe_limit_kills_the_process_group_without_waiting_for_the_timeout(self):
        pid_file = self.root / "descendant.pid"
        script = textwrap.dedent("""
            import os, subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with open(sys.argv[1] + ".tmp", "w") as handle:
                handle.write(str(child.pid))
            os.replace(sys.argv[1] + ".tmp", sys.argv[1])
            while True:
                sys.stdout.write("y" * 65536)
                sys.stdout.flush()
        """)
        descendants = []

        def cleanup():
            for pid in descendants:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        self.addCleanup(cleanup)
        with patch.object(git_query, "MAX_OUTPUT_BYTES", 100000), patch.object(git_query, "MAX_OBSERVED_BYTES", 300000):
            began = time.monotonic()
            streams, code, terminated = git_query._capture([sys.executable, "-c", script, str(pid_file)],
                                                           self.root, clean_env(), 20)
            elapsed = time.monotonic() - began
            descendants.append(int(pid_file.read_text()))
            result = self.project(streams, code, terminated)
            # Reaching the limit ends the query at once, without waiting for the
            # end of stream: exactly the limit on either pipe, followed by a normal
            # exit or by a sleep (the review probe), is reported as output_limit.
            exact = {}
            for pipe in ("stdout", "stderr"):
                for after in ("exit", "sleep"):
                    code_text = (f"import sys, time; sys.{pipe}.write('w' * 300000); sys.{pipe}.flush()"
                                 + ("; time.sleep(30)" if after == "sleep" else ""))
                    began = time.monotonic()
                    captured = git_query._capture([sys.executable, "-c", code_text], self.root, clean_env(), 20)
                    exact[pipe, after] = (time.monotonic() - began, captured)
        self.assertLess(elapsed, 10)
        self.assertEqual((code, terminated), (None, "output_limit"))
        self.assert_exits(descendants[0])
        self.assertFalse(result["observed_complete"])
        self.assertTrue(result["stdout_truncated"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["stdout_bytes"], 300000)
        self.assertEqual(result["stdout_sha256"], hashlib.sha256(b"y" * 300000).hexdigest())
        self.assertEqual(result["stdout_returned_bytes"], 100000)
        self.assertEqual((result["retain_limit_bytes"], result["observe_limit_bytes"]), (100000, 300000))
        for (pipe, after), (seconds, (streams, code, terminated)) in exact.items():
            with self.subTest(pipe=pipe, after=after):
                self.assertLess(seconds, 10)
                self.assertEqual((code, terminated), (None, "output_limit"))
                self.assertEqual(streams[pipe].size, 300000)
                self.assertEqual(streams[pipe].digest.hexdigest(), hashlib.sha256(b"w" * 300000).hexdigest())
                exact_result = self.project(streams, code, terminated)
                self.assertFalse(exact_result["observed_complete"])
                self.assertIsNone(exact_result["exit_code"])
                self.assertTrue(exact_result["stdout_truncated"])
                if pipe == "stderr":
                    self.assertTrue(exact_result["stderr_truncated"])

    def test_ambient_git_environment_and_repository_programs_do_not_apply(self):
        argvs = [["git", "ls-files", "-z", "--", "d"], ["git", "ls-tree", "-r", "HEAD", "--", "d"],
                 ["git", "rev-parse", "HEAD"], ["git", "log", "-1", "--format=%H", "--", "d/f"],
                 ["git", "show", "HEAD:d/f"], ["git", "hash-object", "--no-filters", "--", "d/f"],
                 ["git", "cat-file", "-s", "HEAD:d/f"]]
        baseline = [stdout_bytes(self.query(argv)) for argv in argvs]
        other = self.root / "other"
        other.mkdir()
        run_git(other, "init", "-q", "-b", "main")
        (other / "d").mkdir()
        (other / "d" / "f").write_bytes(b"other\n")
        run_git(other, "add", ".")
        run_git(other, *USER, "commit", "-qm", "other")
        markers = []
        for key in ("fsmonitor", "external", "pager", "alias", "envdiff", "envpager"):
            script, marker = self.sentinel_script(key)
            markers.append((key, script, marker))
        scripts = {key: script for key, script, _ in markers}
        run_git(self.repo, "config", "core.fsmonitor", str(scripts["fsmonitor"]))
        run_git(self.repo, "config", "diff.external", str(scripts["external"]))
        run_git(self.repo, "config", "core.pager", str(scripts["pager"]))
        for command in ("ls-files", "ls-tree", "rev-parse", "log", "show", "cat-file", "hash-object"):
            run_git(self.repo, "config", f"alias.{command}", f"!{scripts['alias']}")
        run_git(self.worktree, "ls-files", "--", "d")
        fsmonitor = dict((key, marker) for key, _, marker in markers)["fsmonitor"]
        self.assertTrue(fsmonitor.exists(), "fixture fsmonitor hook must be active for plain Git")
        fsmonitor.unlink()
        ambient = {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                   "GIT_EXTERNAL_DIFF": str(scripts["envdiff"]), "GIT_PAGER": str(scripts["envpager"]),
                   "GIT_INDEX_FILE": str(other / ".git" / "index"),
                   "GIT_CONFIG_PARAMETERS": f"'core.fsmonitor={scripts['fsmonitor']}'"}
        with patch.dict(os.environ, ambient):
            results = [self.query(argv) for argv in argvs]
        self.assertEqual([stdout_bytes(result) for result in results], baseline)
        self.assertTrue(all(result["exit_code"] == 0 for result in results))
        for key, _, marker in markers:
            self.assertFalse(marker.exists(), key)

    def test_queries_write_no_objects_index_or_lock_files(self):
        (self.worktree / "untracked.txt").write_bytes(b"new content\n")
        expected_oid = run_git(self.worktree, "hash-object", "--no-filters", "--", "untracked.txt").stdout.decode().strip()
        common, private = Path(self.binding["common_dir"]), Path(self.binding["git_dir"])
        def state():
            files = {}
            for top in (common, private):
                for path in sorted(top.rglob("*")):
                    if path.is_file():
                        info = path.stat()
                        files[str(path)] = (info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
            return files
        before = state()
        for argv in (["git", "ls-files", "-z", "--deleted", "--", "d"], ["git", "ls-tree", "-r", "HEAD", "--", "d"],
                     ["git", "rev-parse", "--verify", "HEAD"], ["git", "log", "-1", "--format=%H", "--", "d"],
                     ["git", "show", "HEAD:d/f"], ["git", "cat-file", "-t", "HEAD"],
                     ["git", "hash-object", "--no-filters", "--", "untracked.txt"]):
            result = self.query(argv)
            self.assertEqual(result["exit_code"], 0, argv)
        self.assertEqual(stdout_bytes(result).decode().strip(), expected_oid)
        self.assertEqual(state(), before)
        self.assertNotEqual(run_git(self.worktree, "cat-file", "-e", expected_oid, check=False).returncode, 0)

    def test_hash_object_refuses_links_fifos_directories_and_link_ancestors(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"outside\n")
        (self.worktree / "file-link").symlink_to("d/f")
        (self.worktree / "dir-link").symlink_to(outside)
        os.mkfifo(self.worktree / "pipe")
        for path in ("file-link", "dir-link/secret", "pipe", "d", "missing", "d/f/below"):
            with self.subTest(path=path):
                outcome = {}
                def target():
                    try:
                        self.query(["git", "hash-object", "--no-filters", "--", path])
                    except HarnessError as error:
                        outcome["error"] = error
                worker = threading.Thread(target=target, daemon=True)
                worker.start()
                worker.join(10)
                self.assertFalse(worker.is_alive(), "hash-object must not block")
                self.assertIn("error", outcome)

    def test_executor_requires_parser_output_and_the_recorded_binding(self):
        with self.assertRaises(HarnessError):
            run_git_query({"form": "show", "rev": "HEAD", "path": "d/f", "argv": ["git", "show", "--textconv", "HEAD:d/f"]},
                          self.worktree, self.binding)
        query = parse_git_query(["git", "rev-parse", "HEAD"])[0]
        with self.assertRaises(HarnessError):
            run_git_query(query, self.worktree, None)
        with self.assertRaises(HarnessError):
            run_git_query(query, self.worktree, dict(self.binding, gitfile_sha256="0" * 64))


class GitQueryMCPTests(Fixture):
    def run_state(self):
        path = self.root / "run" / "state.json"
        write_json(path, {"id": "run_fixture", "worktree": self.binding["worktree"], "worktree_binding": self.binding})
        return path

    @staticmethod
    def call(ident, arguments):
        return {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                "params": {"name": "git_query", "arguments": arguments}}

    def test_tool_is_visible_to_every_role_and_rejects_out_of_table_argv_without_execution(self):
        config = Config.load(ROOT / "harness.toml")
        run = self.run_state()
        marker = self.worktree / "marker"
        for role in ("commander", "worker", "reviewer"):
            with self.subTest(role=role):
                claude, codex = mcp_configs(config, self.root / ".harness", role, run)
                server = claude["mcpServers"]["harness"]
                self.assertEqual(codex["harness"]["args"], server["args"])
                self.assertTrue(codex["harness"]["required"])
                requests = [{"jsonrpc": "2.0", "id": 0, "method": "tools/list"},
                            self.call(1, {"argv": ["git", "show", "HEAD:d/f"]}),
                            self.call(2, {"argv": REJECTED["output_option"]}),
                            self.call(3, {"argv": ["git", "rev-parse", "HEAD"], "cwd": "/"})]
                process = subprocess.run([server["command"], *server["args"]], cwd=self.worktree,
                                         input="".join(json.dumps(r) + "\n" for r in requests),
                                         capture_output=True, text=True, timeout=60)
                self.assertEqual(process.returncode, 0, process.stderr)
                responses = [json.loads(line) for line in process.stdout.splitlines()]
                tools = {tool["name"]: tool for tool in responses[0]["result"]["tools"]}
                self.assertTrue(tools["git_query"]["annotations"]["readOnlyHint"])
                self.assertFalse(tools["git_query"]["inputSchema"]["additionalProperties"])
                success = responses[1]["result"]
                self.assertFalse(success["isError"])
                self.assertEqual(json.loads(success["content"][0]["text"])["stdout"], "second\n")
                self.assertTrue(responses[2]["result"]["isError"])
                self.assertIn("rejected without execution", responses[2]["result"]["content"][0]["text"])
                self.assertTrue(responses[3]["result"]["isError"])
                self.assertFalse(marker.exists())

    def test_every_rejected_argv_is_a_tool_error_before_any_process_starts(self):
        run = self.run_state()
        with patch("harness.git_query.subprocess.Popen", side_effect=AssertionError("executed")):
            for name, argv in REJECTED.items():
                with self.subTest(case=name):
                    response = handle(self.call(1, {"argv": argv}), None, ROOT / "shared" / "skills", run)
                    self.assertTrue(response["isError"])
            response = handle(self.call(1, {"argv": ["git", "show", "HEAD:d/f"]}), None, ROOT / "shared" / "skills", None)
            self.assertTrue(response["isError"])


class GitQueryToolSmokeFixtureTests(unittest.TestCase):
    """The live probe's expectations are checked offline; no CLI or model is invoked."""

    def test_fixture_expectations_match_offline_tool_results(self):
        from harness.tool_smoke import bind_readonly_run, prepare_fixture
        with tempfile.TemporaryDirectory() as tmp:
            fixture = prepare_fixture(Path(tmp).resolve() / "fixture")
            state = json.loads(Path(fixture["run_state"]).read_text())
            cases = [case for case in fixture["cases"] if case["tool"] == "mcp__harness__git_query"]
            self.assertEqual({case["name"] for case in cases},
                             {"git_query_ls_tree", "git_query_hash_object", "git_query_rejected"})
            for case in cases:
                with self.subTest(case=case["name"]):
                    response = handle({"method": "tools/call", "params": {"name": "git_query", "arguments": case["input"]}},
                                      None, ROOT / "shared" / "skills", Path(fixture["run_state"]))
                    self.assertEqual(response["isError"], case["error"])
                    if not case["error"]:
                        value = json.loads(response["content"][0]["text"])
                        self.assertEqual({key: value[key] for key in case["expected"]}, case["expected"])
            self.assertFalse((Path(fixture["worktree"]) / "marker").exists())
            mcp = Path(tmp) / "mcp.json"
            write_json(mcp, {"mcpServers": {"harness": {"args": ["entry"]}, "external": {"args": []}}})
            document = bind_readonly_run(mcp, fixture["run_state"])
            self.assertEqual(document["mcpServers"]["harness"]["args"], ["entry", "--run", fixture["run_state"]])
            self.assertEqual(document["mcpServers"]["external"]["args"], [])
            self.assertEqual(state["worktree"], str(Path(fixture["worktree"]).resolve()))
            with self.assertRaises(HarnessError):
                bind_readonly_run(mcp, fixture["run_state"])


if __name__ == "__main__":
    unittest.main()
