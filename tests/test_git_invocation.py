"""The shared Git invocation: one construction of options and environment for every Harness Git call.

The test process runs under git_isolation (HOME, XDG_CONFIG_HOME and GIT_CONFIG_GLOBAL point at a private
folder), and the private global configuration is empty again after every test, so the global configuration
Git sees is only what the test writes there. "Plain" Git is Git as a user runs it in that environment,
without the Harness construction: the positive controls.
"""
import ast
import collections
import contextlib
import functools
import hashlib
import io
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import git_isolation
import run_shards
from harness import cleanup, cli, dashboard, git, operations, permission_smoke, stops, tool_smoke
from harness.config import Config
from harness.util import HarnessError, read_json
from test_decide import Script, block_first, blocked_by_task, describe, revise, task
from test_harness import ROOT, RepoCase, write_project_config

IDENTITY = ("-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false")
# Every event githooks(5) of Git 2.56 lists.
EVENTS = ("applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit", "pre-merge-commit", "prepare-commit-msg",
          "commit-msg", "post-commit", "pre-rebase", "post-checkout", "post-merge", "pre-push", "pre-receive", "update",
          "proc-receive", "post-receive", "post-update", "reference-transaction", "push-to-checkout", "pre-auto-gc",
          "post-rewrite", "sendemail-validate", "fsmonitor-watchman", "p4-changelist", "p4-prepare-changelist",
          "p4-post-changelist", "p4-pre-submit", "post-index-change")


def isolate_git(case):
    """The private global Git configuration of git_isolation, empty for the test and emptied after it."""
    return case.enterContext(git_isolation.user_config(""))


def plain_git(cwd, *args, input=None, env=None, check=True):
    result = subprocess.run(["git", "-C", str(cwd), *args], input=input, capture_output=True, timeout=60,
                            env=os.environ if env is None else env)
    if check and result.returncode:
        raise AssertionError(f"plain git {args}: {result.stderr.decode(errors='replace')}")
    return result


@functools.lru_cache(maxsize=None)
def git_config_help():
    return subprocess.run(["git", "help", "--config"], capture_output=True, text=True, timeout=60).stdout


def configured_hooks_exist():
    """Git 2.54 and later run hooks defined in configuration (hook.<name>.command)."""
    return "hook.<friendly-name>.command" in git_config_help()


def event_switches_exist():
    """Git that knows hook.<event>.enabled (2.56.1 does, 2.54.0 does not)."""
    return "hook.<event>.enabled" in git_config_help()


def expire_stats(top):
    """Move the mtime of a file, or of every file below a tree, so Git's cached stat no longer matches."""
    top = Path(top)
    for path in [top] if top.is_file() else top.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.name != ".git" and "__pycache__" not in path.parts:
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns - 10 ** 10))


class Markers:
    """Programs Git may run; each appends "<label> <physical cwd>" to one log when it runs.

    ``arm`` installs them in a repository: a hook-directory hook and a configured hook (repository
    and isolated global configuration) per event, and with ``programs`` also an fsmonitor hook,
    clean/smudge filters and a diff driver for every path, an external diff, a GPG program, colour,
    status.showUntrackedFiles=no and core.splitIndex=true. The repository part is one included file,
    so ``disarm`` leaves the repository's own configuration as it was.
    """

    def __init__(self, folder):
        self.folder = Path(folder)
        self.log = self.folder / "markers.log"
        self.log.write_text("")
        record = f"printf '%s %s\\n' \"$1\" \"$(pwd -P)\" >> '{self.log}'\n"
        self.hook = self.script("hook.sh", record + "cat >/dev/null 2>&1\nexit 0\n")
        self.filter = self.script("filter.sh", record + "exec cat\n")
        self.textconv = self.script("textconv.sh", record.replace('"$1"', "textconv") + 'cat "$1"\n')
        self.fsmonitor = self.script("fsmonitor.sh", record.replace('"$1"', "fsmonitor") + "exit 1\n")
        self.external = self.script("external.sh", record.replace('"$1"', "external") + "exit 0\n")
        self.gpg = self.script("gpg.sh", record.replace('"$1"', "gpg") + "exit 1\n")
        self.included = self.folder / "armed.gitconfig"
        self.included.write_text("")

    def script(self, name, body):
        path = self.folder / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return path

    def read(self):
        return [tuple(line.split(" ", 1)) for line in self.log.read_text().splitlines()]

    def kinds(self):
        return {label.split(":")[0] for label, _ in self.read()}

    def reset(self):
        self.log.write_text("")

    @staticmethod
    def common(repo):
        return Path(plain_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.decode().strip())

    def arm(self, repo, *, events=EVENTS, programs=True, scopes=("repo", "glob"), hook_directory=True):
        common = self.common(repo)
        (common / "hooks").mkdir(exist_ok=True)
        for event in events if hook_directory else ():
            hook = common / "hooks" / event
            hook.write_text(f"#!/bin/sh\nexec '{self.hook}' hookdir:{event} \"$@\"\n")
            hook.chmod(0o755)

        def hooks(scope):
            return "".join(f'[hook "{scope}-{event}"]\n\tcommand = \'{self.hook}\' config:{scope}-{event}\n'
                           f"\tevent = {event}\n" for event in events)
        text = hooks("repo") if "repo" in scopes else ""
        if programs:
            (common / "info").mkdir(exist_ok=True)
            (common / "info" / "attributes").write_text("* filter=marker diff=marker\n")
            text += (f"[core]\n\tfsmonitor = {self.fsmonitor}\n\tsplitIndex = true\n"
                     f"[filter \"marker\"]\n\tclean = '{self.filter}' clean\n\tsmudge = '{self.filter}' smudge\n"
                     f"[diff \"marker\"]\n\ttextconv = '{self.textconv}'\n[diff]\n\texternal = {self.external}\n"
                     f"[gpg]\n\tprogram = {self.gpg}\n[commit]\n\tgpgsign = true\n"
                     "[color]\n\tui = always\n\tdiff = always\n[status]\n\tshowUntrackedFiles = no\n")
        self.included.write_text(text)
        plain_git(repo, "config", "include.path", str(self.included))
        if "glob" in scopes:
            Path(os.environ["GIT_CONFIG_GLOBAL"]).write_text(hooks("glob"))
        self.armed = (common, events if hook_directory else ())

    def disarm(self):
        common, events = self.armed
        for event in events:
            (common / "hooks" / event).unlink()
        (common / "info" / "attributes").unlink(missing_ok=True)
        self.included.write_text("")
        Path(os.environ["GIT_CONFIG_GLOBAL"]).write_text("")


class IsolatedRepoCase(RepoCase):
    def setUp(self):
        self.global_config = isolate_git(self)
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.scratch = Path(folder.name)

    def events(self, engine):
        return [json.loads(line)["event"] for line in (engine.directory / "events.jsonl").read_text().splitlines()]

    def cli(self, *argv):
        """The real CLI; a dashboard starts and stops at once, as SIGTERM would end it."""
        def stop_at_once(server, poll_interval=0.5):
            raise KeyboardInterrupt()
        output, errors = io.StringIO(), io.StringIO()
        previous = signal.getsignal(signal.SIGTERM)
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                    patch.object(dashboard.DashboardServer, "serve_forever", stop_at_once):
                code = cli.main(["--repo", str(self.repo), "--config", str(ROOT / "harness.toml"), *argv])
        finally:
            signal.signal(signal.SIGTERM, previous)
        return code, output.getvalue(), errors.getvalue()


class PreviewAndObservationTests(IsolatedRepoCase):
    """Case 2 without the read-only scope: a marked repository, an offline run, preview, observation, removal."""

    def setUp(self):
        super().setUp()
        self.config.data["verification"]["fresh_checkout"] = True

    def files(self, *tops, skip=()):
        """Digest and mtime of every file below ``tops``; a split index's shared file only by digest."""
        state = {}
        for top in tops:
            for path in sorted(Path(top).rglob("*")):
                if path.relative_to(top).parts[0] in skip or path.is_symlink() or not path.is_file():
                    continue
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                state[str(path)] = digest if path.name.startswith("sharedindex.") else (digest, path.stat().st_mtime_ns)
        return state

    def test_preview_and_observation_run_no_marked_program_and_change_no_git_bytes(self):
        markers = Markers(self.scratch)
        markers.arm(self.repo)
        real_clean_checkout = cleanup.clean_checkout
        refused = []

        def runtime_reclaim_fails_once(*args, **kwargs):
            if kwargs.get("by") == "runtime" and not refused:
                refused.append(True)
                raise HarnessError("simulated reclamation failure")
            return real_clean_checkout(*args, **kwargs)
        engine = self.engine()
        markers.reset()
        with patch("harness.cleanup.clean_checkout", side_effect=runtime_reclaim_fails_once):
            result = engine.start("Implement greet")
        self.assertEqual(result["status"], "completed")
        run_id, worktree = result["id"], Path(result["worktree"])
        # The run: filters may run; hooks of either kind, fsmonitor and external programs never do.
        self.assertFalse(markers.kinds() & {"hookdir", "config", "fsmonitor", "external", "textconv", "gpg"},
                         markers.read())
        self.assertIn("smudge", markers.kinds())  # the marked repository is in effect
        self.assertIn("verification_checkout_reclaim_failed", self.events(engine))
        artifact, = engine.directory.glob("verification/final/*/verification-*/checkout.json")
        checkout = Path(read_json(artifact)["worktree"])
        self.addCleanup(shutil.rmtree, checkout.parents[1], ignore_errors=True)
        self.assertTrue(checkout.is_dir())
        self.assertIn(f"worktree {checkout}\n", plain_git(self.repo, "worktree", "list", "--porcelain").stdout.decode())
        for tree, binding in ((worktree, result["worktree_binding"]), (checkout, read_json(artifact)["binding"])):
            plain_git(tree, "update-index", "--split-index")
            self.assertTrue(list(Path(binding["git_dir"]).glob("sharedindex.*")))
            expire_stats(tree)
        # An ignored file keeps the run worktree out of the removal below, whether or not verification wrote bytecode.
        (worktree / "__pycache__").mkdir(exist_ok=True)
        (worktree / "__pycache__" / "fixture.pyc").write_bytes(b"ignored fixture\n")
        write_project_config(self.repo)
        # The preview a repository without any marked program gets.
        markers.disarm()
        code, unmarked, errors = self.cli("cleanup-run", run_id)
        unmarked = json.loads(unmarked)
        self.assertEqual([item["status"] for item in unmarked["checkouts"]], ["eligible"], errors)
        markers.arm(self.repo)
        markers.reset()
        before = self.files(self.repo / ".git", worktree, checkout) | self.files(self.repo, skip=(".git", ".harness"))
        dispatch = next(entry["id"] for entry in result["dispatches"] if entry["role"] == "worker")
        for argv in (("cleanup-run", run_id), ("status", run_id), ("status", run_id, "--full"), ("watch", run_id),
                     ("dashboard", "--port", "0"), ("permission-diagnostics", run_id, dispatch)):
            code, output, errors = self.cli(*argv)
            self.assertNotIn("Traceback", errors, argv)
            if argv[0] == "cleanup-run":
                self.assertEqual(json.loads(output), unmarked)
            else:
                self.assertEqual(code, 0, (argv, errors))
        self.assertEqual(markers.read(), [])
        after = self.files(self.repo / ".git", worktree, checkout) | self.files(self.repo, skip=(".git", ".harness"))
        self.assertEqual(after, before)
        # Removal passes the real preview gates; Git's own status inside worktree remove may run the clean filter.
        expire_stats(checkout)
        markers.reset()
        code, output, errors = self.cli("cleanup-run", run_id, "--remove")
        removal = json.loads(output)
        self.assertEqual([item["status"] for item in removal["checkouts"]], ["removed"], errors)
        self.assertEqual(removal["worktree"]["status"], "blocked", removal["worktree"])
        self.assertIn("__pycache__/", removal["worktree"]["error"])
        self.assertTrue(worktree.is_dir())
        self.assertFalse(checkout.exists())
        self.assertEqual(read_json(artifact.with_name("cleanup.json"))["status"], "removed")
        self.assertEqual(markers.kinds(), {"clean"}, markers.read())
        self.assertEqual({where for _, where in markers.read()}, {str(checkout.resolve())})


class Disguised(tuple):
    """Equal to the tuple it is made from, yet iterating to a setting of its own."""

    def __iter__(self):
        return iter(("color.diff=always",))


class ConstructionTests(unittest.TestCase):
    """Case 5: construction refusals and a failed hook name read; no process starts after either."""

    def setUp(self):
        isolate_git(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        plain_git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "x").write_text("x\n")
        plain_git(self.repo, "add", "x")
        plain_git(self.repo, *IDENTITY, "commit", "-qm", "fixture")

    @contextlib.contextmanager
    def launches(self, fail=None):
        """Record every argv harness.git starts, and in ``self.finished`` what each returned; ``fail`` (an exception)
        is raised by the hook name read."""
        real, started, self.finished = subprocess.run, [], []

        def recording(argv, **kwargs):
            started.append(list(argv))
            if fail is not None and "--get-regexp" in argv:
                raise fail
            self.finished.append(real(argv, **kwargs))
            return self.finished[-1]
        with patch("harness.git.subprocess.run", side_effect=recording):
            yield started

    def assert_invariant(self, call):
        with self.launches() as started, self.assertRaises(HarnessError) as raised:
            call()
        self.assertEqual(stops.of(raised.exception)["kind"], "runtime_failure")
        self.assertEqual(stops.of(raised.exception)["reason"], "invariant")
        self.assertEqual(started, [])

    def test_a_temporary_index_inside_the_worktree_relative_or_for_a_real_write_is_refused_before_any_process(self):
        outside = self.root / "private.index"
        self.assert_invariant(lambda: git.git(self.repo, "ls-files", effect=git.READ, index_file="private.index"))
        self.assert_invariant(lambda: git.git(self.repo, "ls-files", effect=git.PRIVATE,
                                              index_file=self.repo / ".git" / "private.index"))
        self.assert_invariant(lambda: git.git(self.repo, "add", "--", "x", effect=git.INDEX, index_file=outside))
        self.assert_invariant(lambda: git._run_git(self.repo, ("ls-files",), index_file=outside))
        # The same index outside the worktree, for a private call, runs.
        git.git(self.repo, "read-tree", "HEAD", effect=git.PRIVATE, index_file=outside)
        self.assertEqual(git.git(self.repo, "ls-files", effect=git.READ, index_file=outside), "x\n")

    def test_another_name_for_a_real_index_is_refused_and_the_index_keeps_its_bytes(self):
        tree = self.root / "tree"
        plain_git(self.repo, "worktree", "add", "-q", "--detach", str(tree))
        binding = git.capture_worktree_binding(tree, None)
        main, linked = self.repo / ".git" / "index", Path(binding["git_dir"]) / "index"
        os.link(main, self.root / "hard-link.index")
        names = [(None, self.root / "hard-link.index"),
                 (binding, self.root / "hard-link.index"),  # the common (main) index, outside every directory
                 (binding, linked),  # outside the worktree, in its Git directory
                 (binding, Path(binding["common_dir"]) / "index")]
        respelled = self.root / self.repo.name.upper() / ".git" / "index"
        if respelled.exists() and os.path.samefile(respelled, main):  # a case-insensitive file system
            names.append((None, respelled))
        before = main.read_bytes(), linked.read_bytes()
        for bound, index in names:
            location = tree if bound else self.repo
            # git.git refuses before its binding check, itself a process; _run_git has no binding check.
            with self.subTest("git.git", index=str(index)):
                self.assert_invariant(lambda: git.git(location, "read-tree", "--empty", effect=git.PRIVATE,
                                                      binding=bound, index_file=index))
            with self.subTest("_run_git", index=str(index)):
                self.assert_invariant(lambda: git._run_git(location, ("read-tree", "--empty"), bound,
                                                           effect=git.PRIVATE, index_file=index))
        self.assertEqual((main.read_bytes(), linked.read_bytes()), before)
        self.assertEqual(git._run_git(tree, ("read-tree", "HEAD"), binding, effect=git.PRIVATE,
                                      index_file=self.root / "private.index").returncode, 0)
        self.assertEqual(git.git(tree, "ls-files", effect=git.READ, binding=binding,
                                 index_file=self.root / "private.index"), "x\n")

    def test_an_unbound_call_in_a_linked_checkout_refuses_every_name_in_its_git_directories(self):
        tree = self.root / "tree"
        plain_git(self.repo, "worktree", "add", "-q", "--detach", str(tree))
        git_dir = Path(plain_git(tree, "rev-parse", "--absolute-git-dir").stdout.decode().strip())
        main, linked = self.repo / ".git" / "index", git_dir / "index"
        self.assertTrue((tree / ".git").is_file())
        os.link(linked, self.root / "hard-link.index")
        os.link(main, self.root / "main-hard-link.index")
        before = main.read_bytes(), linked.read_bytes()
        # tree/.git is a gitfile: the checkout's own index, the common (main) index, a hard link to each
        # of them, and another file in its Git directory.
        for index in (linked, main, self.root / "hard-link.index", self.root / "main-hard-link.index",
                      git_dir / "other.index"):
            with self.subTest("git.git", index=str(index)):
                self.assert_invariant(lambda: git.git(tree, "read-tree", "--empty", effect=git.PRIVATE,
                                                      index_file=index))
            with self.subTest("_run_git", index=str(index)):
                self.assert_invariant(lambda: git._run_git(tree, ("read-tree", "--empty"), effect=git.PRIVATE,
                                                           index_file=index))
        self.assertEqual((main.read_bytes(), linked.read_bytes()), before)
        self.assertFalse((git_dir / "other.index").exists())
        # An index outside the checkout and every Git directory is still private.
        private = self.root / "private.index"
        git.git(tree, "read-tree", "HEAD", effect=git.PRIVATE, index_file=private)
        self.assertEqual(git.git(tree, "ls-files", effect=git.READ, index_file=private), "x\n")
        self.assertEqual((main.read_bytes(), linked.read_bytes()), before)

    def test_a_failed_hook_name_read_starts_no_call(self):
        with (self.repo / ".git" / "config").open("a") as config:
            config.write("[broken\n")
        calls = {"git": lambda: git.git(self.repo, "add", "--", "x", effect=git.INDEX),
                 "git_process": lambda: git.git_process(self.repo, "add", "--", "x", effect=git.INDEX),
                 "_run_git": lambda: git._run_git(self.repo, ("add", "--", "x"), effect=git.INDEX)}
        for name, call in calls.items():
            with self.subTest(name, exit_code=128):
                with self.launches() as started, self.assertRaises(HarnessError) as raised:
                    call()
                self.assertEqual(len(started), 1)
                self.assertIn("--get-regexp", started[0])
                read, = self.finished
                self.assertNotIn(read.returncode, (0, 1))
                # Git's own words, in whatever language Git writes them here.
                self.assertTrue(read.stderr.strip())
                self.assertIn(read.stderr.decode("utf-8", errors="replace").strip(), str(raised.exception))
                self.assertIn("hook 名字读取失败", str(raised.exception))
        # A READ call reads no names: the one process is the call itself.
        with self.launches() as started:
            self.assertNotEqual(git.git_process(self.repo, "rev-parse", "HEAD", effect=git.READ).returncode, 0)
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0][-2:], ["rev-parse", "HEAD"])
        # A name read that cannot start fails as the call itself would.
        for name, call in calls.items():
            with self.subTest(name, start="OSError"):
                expected = OSError if name == "git_process" else HarnessError
                with self.launches(fail=OSError("cannot start")) as started, self.assertRaises(expected):
                    call()
                self.assertEqual(len(started), 1)

    def test_no_caller_adds_settings_after_the_fixed_ones_but_the_whole_run_comparison(self):
        tree = self.root / "tree"
        plain_git(self.repo, "worktree", "add", "-q", "--detach", str(tree))
        binding = git.capture_worktree_binding(tree, None)
        deadline = time.monotonic() + 60
        # Refused before any process, a bound call's binding check included.
        self.assert_invariant(lambda: git.git_process(self.repo, "commit", "--allow-empty", "-m", "x",
                                                      effect=git.INDEX_REFS, _config=("core.hooksPath=/tmp/hooks",)))
        self.assert_invariant(lambda: git.git_process(tree, "status", effect=git.COMPARE, binding=binding,
                                                      _config=("status.showUntrackedFiles=no",)))
        self.assert_invariant(lambda: git.git_bytes(self.repo, "diff-tree", "--no-ext-diff", "--no-textconv", "HEAD",
                                                    effect=git.READ, _config=list(git._RANGE_DIFF_CONFIG)))
        self.assert_invariant(lambda: git.invocation(self.repo, ("diff-tree", "HEAD"), effect=git.READ, deadline=deadline,
                                                     _config=(*git._RANGE_DIFF_CONFIG, "color.diff=always")))
        # Only None and the module's tuple itself pass: an equal object may iterate to other settings.
        lookalikes = ((), tuple(list(git._RANGE_DIFF_CONFIG)), Disguised(()), Disguised(git._RANGE_DIFF_CONFIG))
        for settings in lookalikes:
            with self.subTest(equal_to=tuple.__repr__(settings)):
                self.assert_invariant(lambda: git.git_process(self.repo, "diff-tree", "HEAD", effect=git.READ,
                                                              _config=settings))
                self.assert_invariant(lambda: git.invocation(self.repo, ("diff-tree", "HEAD"), effect=git.READ,
                                                             deadline=deadline, _config=settings))
        # Behind that check, the settings Git gets are the module's tuple, never the object a caller passed.
        with patch.object(git, "_check_config"):
            for settings in lookalikes:
                argv, _ = git.invocation(self.repo, ("diff-tree", "HEAD"), effect=git.READ, deadline=deadline,
                                         _config=settings)
                self.assertEqual(argv[-2:], ["diff-tree", "HEAD"])
                self.assertNotIn("color.diff=always", argv)
                self.assertFalse(set(git._RANGE_DIFF_CONFIG) & set(argv))
        argv, _ = git.invocation(self.repo, ("diff-tree", "HEAD"), effect=git.READ, deadline=deadline,
                                 _config=git._RANGE_DIFF_CONFIG)
        self.assertEqual(argv[-2 - 2 * len(git._RANGE_DIFF_CONFIG):],
                         [*(item for setting in git._RANGE_DIFF_CONFIG for item in ("-c", setting)), "diff-tree", "HEAD"])


class StandardInputTests(unittest.TestCase):
    """A Git call reads its ``input`` if it has one, else end of file; never the standard input Harness was given."""

    # In a Harness process whose standard input holds b"inherited\n": one Git call, then what is left of that input.
    PROGRAM = ("import sys\nfrom pathlib import Path\nfrom harness import git\n"
               "given = {'given': b'given\\n', 'none': None}[sys.argv[2]]\n"
               "print(git.git(Path(sys.argv[1]), 'hash-object', '--stdin', input=given).strip())\n"
               "print(sys.stdin.read().strip())\n")

    def test_git_reads_the_given_input_or_end_of_file_and_leaves_harness_input_alone(self):
        isolate_git(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)

        def harness_process(given):
            process = subprocess.run([sys.executable, "-c", self.PROGRAM, folder.name, given], cwd=ROOT,
                                     input=b"inherited\n", capture_output=True, timeout=60)
            self.assertEqual(process.returncode, 0, process.stderr)
            return process.stdout.decode().splitlines()

        def oid(data):
            return plain_git(folder.name, "hash-object", "--stdin", input=data).stdout.decode().strip()
        self.assertEqual(harness_process("none"), [oid(b""), "inherited"])
        self.assertEqual(harness_process("given"), [oid(b"given\n"), "inherited"])


class UntrackedFilesTests(IsolatedRepoCase):
    """Case 6: status.showUntrackedFiles=no in the repository configuration hides no untracked file from Harness."""

    def setUp(self):
        super().setUp()
        self.config.data["verification"]["commands"] = [{"cwd": ".", "argv": [sys.executable, "-c", "pass"]}]
        plain_git(self.repo, "config", "status.showUntrackedFiles", "no")

    def test_the_commander_sees_an_untracked_file_and_a_revision_archives_it_before_the_next_task(self):
        seen = {}

        def decide(script, trigger, packet):
            if trigger["kind"] == "task" and blocked_by_task(trigger["feedback"]):
                seen["clean"] = packet["workspace"]["clean"]
                return revise(selected="p", instruction="prepare first", new_tasks=[task("p", paths=["a.txt"])],
                              updates=[describe("a", deps=["p"])])
            return None

        def work(script, tid, n, kwargs, result):
            if tid == "a" and n == 1:
                # The blocked attempt leaves one untracked, unignored file, a.txt, which plain status does not list.
                seen["plain status"] = plain_git(kwargs["cwd"], "status", "--porcelain").stdout
                block_first("a")(script, tid, n, kwargs, result)
            if tid == "p":
                seen["p starts with a.txt"] = (kwargs["cwd"] / "a.txt").exists()
                (kwargs["cwd"] / "p.txt").unlink()
                (kwargs["cwd"] / "a.txt").write_text("prepared\n")
                result["evidence"] = ["a.txt"]
        engine = self.engine(Script(decide, work=work))
        result = engine.start("archive", {"tasks": [task("a")]})
        self.assertEqual(seen["plain status"], b"")  # the configuration is in effect
        self.assertIs(seen["clean"], False)
        archive = engine.directory / "recovery" / "revision-1"
        self.assertEqual((archive / "files" / "a.txt").read_text(), "a attempt 1\n")
        self.assertIn("revision_edits_archived", self.events(engine))
        self.assertIs(seen["p starts with a.txt"], False)
        self.assertEqual(result["status"], "completed")

    def test_an_untracked_file_in_the_installation_marks_the_runtime_modified(self):
        install = self.scratch.resolve() / "install"
        (install / "harness").mkdir(parents=True)
        (install / "harness" / "__init__.py").write_text("")
        plain_git(install, "init", "-q", "-b", "main")
        plain_git(install, "add", ".")
        plain_git(install, *IDENTITY, "commit", "-qm", "installation")
        plain_git(install, "config", "status.showUntrackedFiles", "no")
        with patch.object(operations, "PACKAGE", install / "harness"):
            self.assertIs(operations.harness_identity()["runtime_modified"], False)
            (install / "harness" / "extra.py").write_text("")
            self.assertEqual(plain_git(install, "status", "--porcelain").stdout, b"")  # the configuration is in effect
            self.assertIs(operations.harness_identity()["runtime_modified"], True)


class ColourTests(IsolatedRepoCase):
    """Case 7: color.ui=always in the repository configuration never reaches diff evidence."""

    def colour(self):
        plain_git(self.repo, "config", "color.ui", "always")
        plain_git(self.repo, "config", "color.diff", "always")

    def uncolour(self):
        plain_git(self.repo, "config", "--unset", "color.ui")
        plain_git(self.repo, "config", "--unset", "color.diff")

    def test_task_and_whole_run_diff_evidence_has_no_escape_bytes(self):
        self.colour()
        engine = self.engine()
        result = engine.start("Implement greet")
        self.assertEqual(result["status"], "completed")
        evidence = sorted(engine.directory.rglob("change.diff"))
        self.assertGreaterEqual(len(evidence), 2, evidence)
        for path in evidence:
            self.assertNotIn(b"\x1b", path.read_bytes(), path)
            self.assertIn(b"hello.py", path.read_bytes(), path)
        final, = [path for path in evidence if "final" in path.relative_to(engine.directory).parts]
        self.uncolour()
        self.assertEqual(final.read_bytes(), git.range_diff_bytes(Path(result["worktree"]), result["base_commit"],
                                                                  binding=result["worktree_binding"]))

    def test_working_tree_and_range_diffs_are_the_bytes_of_an_uncoloured_repository(self):
        base = git.head(self.repo)
        (self.repo / "test_hello.py").write_text("print('changed')\n")
        plain_git(self.repo, *IDENTITY, "commit", "-qam", "second")
        (self.repo / "test_hello.py").write_text("print('changed again')\n")
        (self.repo / "new.txt").write_text("new\n")
        self.colour()
        self.assertIn(b"\x1b[", plain_git(self.repo, "diff", "HEAD").stdout)  # the configuration is in effect
        coloured = git.diff_bytes(self.repo), git.range_diff_bytes(self.repo, base)
        self.uncolour()
        self.assertEqual(coloured, (git.diff_bytes(self.repo), git.range_diff_bytes(self.repo, base)))
        self.assertTrue(all(b"\x1b" not in value and b"test_hello.py" in value for value in coloured))


class TreeDiffTests(IsolatedRepoCase):
    """Section 3.7: the whole-run comparison is plumbing, the bytes porcelain diff gives under default configuration."""

    PATCH = ("--binary", "--no-ext-diff", "--no-textconv", "--no-renames")

    def setUp(self):
        super().setUp()
        self.config.data["verification"]["commands"] = [{"cwd": ".", "argv": [sys.executable, "-c", "pass"]}]

    def commit(self, message, *, add=True):
        if add:
            plain_git(self.repo, "add", "-A")
        plain_git(self.repo, *IDENTITY, "commit", "-q", "-m", message)
        return plain_git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()

    def test_the_whole_run_patch_is_the_porcelain_bytes_for_every_kind_of_change(self):
        repo = self.repo
        c0 = git.head(repo)
        (repo / "keep.txt").write_bytes(b"".join(b"line %d\n" % i for i in range(40)))
        for name, data in (("gone.txt", b"deleted later\n"), ("noeol.txt", b"no newline"), ("crlf.txt", b"a\r\nb\r\n"),
                           ("moved.py", b"def f():\n    return 1\n"), ("blob.bin", bytes(range(256))),
                           ("run.sh", b"#!/bin/sh\n"), ("folder/inner.txt", b"inner\n"), ("typed.txt", b"a file\n")):
            (repo / name).parent.mkdir(exist_ok=True)
            (repo / name).write_bytes(data)
        (repo / "link").symlink_to("keep.txt")
        c1 = self.commit("base")
        (repo / "keep.txt").write_bytes(b"".join(b"line %d\n" % i if i % 13 else b"edited %d\n" % i for i in range(40)))
        (repo / "gone.txt").unlink()
        (repo / "noeol.txt").write_bytes(b"still no newline")
        (repo / "crlf.txt").write_bytes(b"a\r\nc\r\n")
        plain_git(repo, "mv", "moved.py", "renamed.py")
        (repo / "renamed.py").write_bytes(b"def f():\n    return 2\n")
        (repo / "blob.bin").write_bytes(bytes(range(256))[::-1])
        (repo / "added.bin").write_bytes(b"\0new\xff")
        (repo / "run.sh").chmod(0o755)
        shutil.rmtree(repo / "folder")
        (repo / "folder").write_bytes(b"a file where a folder was\n")
        (repo / "typed.txt").unlink()
        (repo / "typed.txt").symlink_to("keep.txt")
        (repo / "link").unlink()
        (repo / "link").symlink_to("noeol.txt")
        c2 = self.commit("every kind of file change")
        # Names this file system may refuse and a submodule pointer go in through the index.
        blob = plain_git(repo, "hash-object", "-w", "--stdin", input=b"bytes \xfe\n").stdout.decode().strip()
        name = os.fsdecode(b"name_\xff.txt")
        plain_git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
        plain_git(repo, "update-index", "--add", "--cacheinfo", f"160000,{c0},vendor/sub")
        c3 = self.commit("non-UTF-8 name and gitlink", add=False)
        kinds = {(c1, c2): (b"GIT binary patch", b"old mode 100644\nnew mode 100755", b"new file mode 120000",
                            b"\\ No newline at end of file", b"\r\n", b"deleted file mode 100644\nindex"),
                 (c2, c3): (b"Subproject commit", b'"a/name_\\377.txt"'), (c0, c3): (b"renamed.py",), (c3, c3): ()}
        for (base, head), expected in kinds.items():
            plain_git(repo, "update-ref", "--no-deref", "HEAD", head)  # the runtime compares base with HEAD
            porcelain = plain_git(repo, "diff", *self.PATCH, base, head, "--").stdout
            with self.subTest(base=base, head=head):
                self.assertEqual(git.range_diff_bytes(repo, base), porcelain)
                self.assertTrue(all(kind in porcelain for kind in expected), expected)
                self.assertEqual(porcelain == b"", base == head)

    def test_diff_interface_configuration_no_longer_shapes_the_whole_run_evidence(self):
        order = self.scratch / "order"
        order.write_text("b.txt\na.txt\n")
        interface = {"diff.noprefix": "true", "diff.context": "1", "diff.orderFile": str(order)}
        for key, value in interface.items():
            plain_git(self.repo, "config", key, value)
        engine = self.engine(Script())
        listed = []
        real = engine.services.packets.final_review_packet

        def recording(**kwargs):
            if kwargs["diff_artifact"]:  # not the capacity estimate's empty projection
                listed.append(kwargs["changed_paths"])
            return real(**kwargs)
        with patch.object(engine.services.packets, "final_review_packet", side_effect=recording):
            result = engine.start("Two files", {"tasks": [task("a"), task("b")]})
        self.assertEqual(result["status"], "completed")
        worktree, base = Path(result["worktree"]), result["base_commit"]

        def porcelain():
            return (plain_git(worktree, "diff", "--name-only", "--no-renames", "-z", base, "HEAD").stdout,
                    plain_git(worktree, "diff", *self.PATCH, base, "HEAD", "--").stdout)
        configured = porcelain()
        for key in interface:
            plain_git(self.repo, "config", "--unset", key)
        names, patch_bytes = porcelain()
        self.assertNotEqual(configured, (names, patch_bytes))  # the configuration is in effect for porcelain diff
        self.assertEqual(names, b"a.txt\0b.txt\0")
        self.assertEqual(listed, [os.fsdecode(names).split("\0")])
        final, = [path for path in engine.directory.rglob("change.diff")
                  if "final" in path.relative_to(engine.directory).parts]
        self.assertEqual(final.read_bytes(), patch_bytes)

    def run_worktree(self):
        worktree = self.scratch.resolve() / "run"
        plain_git(self.repo, "worktree", "add", "-q", "-b", "harness/run", str(worktree))
        return worktree, git.capture_worktree_binding(worktree, "harness/run")

    def test_the_whole_run_comparison_reads_the_two_commits_not_the_work_tree_or_its_index(self):
        plain_git(self.repo, "config", "core.trustctime", "false")
        base = git.head(self.repo)
        (self.repo / "story.txt").write_text("head\n")
        self.commit("story")
        worktree, binding = self.run_worktree()
        patch_bytes = plain_git(worktree, "diff", *self.PATCH, base, "HEAD", "--").stdout  # the form before diff-tree
        names = plain_git(worktree, "diff", "--name-only", "--no-renames", "-z", base, "HEAD").stdout
        self.assertIn(b"+head\n", patch_bytes)
        story = worktree / "story.txt"
        os.utime(story, ns=(1_600_000_000_000_000_000,) * 2)
        plain_git(worktree, "update-index", "--refresh")
        kept = story.stat()
        story.write_text("live\n")  # same size, same inode; the old mtime back: the index entry still matches
        os.utime(story, ns=(kept.st_atime_ns, kept.st_mtime_ns))
        self.assertTrue(git.clean(worktree, binding=binding))  # Git's own stat check does not see the edit
        self.assertEqual(git.range_diff_bytes(worktree, base, binding=binding), patch_bytes)
        # A real index Git cannot read: porcelain diff between two commits never read it, nor do these.
        (Path(binding["git_dir"]) / "index").write_bytes(b"not an index")
        self.assertEqual(git.range_diff_bytes(worktree, base, binding=binding), patch_bytes)
        self.assertEqual(os.fsencode(git.range_paths(worktree, base, binding=binding)), names)

    def test_no_configuration_diff_tree_reads_reshapes_the_whole_run_comparison(self):
        # Edits on which other algorithms choose other hunks than Git's default: patience on driven.txt, histogram
        # and minimal on driven.big.
        rng = random.Random(0)
        lines = [b"%d\n" % rng.randrange(50) for _ in range(4000)]
        edited = [b"%d\n" % rng.randrange(50) if rng.random() < .5 else line for line in lines]
        files = {"blank.txt": (b"heading\n\nold\n\nfooter\n", b"heading\n\nnew\n\nfooter\n"),
                 "slider.txt": (b"1\n2\na\n\nb\n3\n4\n", b"1\n2\na\n\nb\na\n\nb\n3\n4\n"),
                 "café.txt": (b"accented name\n", b"accented name, edited\n"),
                 "blob.bin": (bytes(range(256)) * 2, bytes(range(256))[::-1] * 2),
                 "driven.txt": (b"}\nb\n{\n{\n", b"a\n{\nc\nb\n"), "driven.big": (b"".join(lines), b"".join(edited)),
                 ".gitattributes": (b"driven.* diff=sample\n", b"driven.* diff=sample\n")}
        for name, (old, _) in files.items():
            (self.repo / name).write_bytes(old)
        base = self.commit("fixture")
        for name, (_, new) in files.items():
            (self.repo / name).write_bytes(new)
        (self.repo / ".gitmodules").write_text('[submodule "vendor"]\n\tpath = vendor/sub\n\turl = ./nowhere\n')
        plain_git(self.repo, "add", "-A")
        plain_git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{base},vendor/sub")
        self.commit("edits", add=False)
        worktree, binding = self.run_worktree()

        def porcelain():
            return (plain_git(worktree, "diff", *self.PATCH, base, "HEAD", "--").stdout,
                    plain_git(worktree, "diff", "--name-only", "--no-renames", "-z", base, "HEAD").stdout)

        def whole_run():
            return (git.range_diff_bytes(worktree, base, binding=binding),
                    os.fsencode(git.range_paths(worktree, base, binding=binding)))
        default = porcelain()
        self.assertEqual(whole_run(), default)
        for key, value in (("diff.suppressBlankEmpty", "true"), ("diff.indentHeuristic", "false"),
                           ("core.abbrev", "12"), ("core.quotePath", "false"), ("core.compression", "0"),
                           ("core.looseCompression", "9"), ("diff.default.binary", "true"),
                           ("diff.default.binary", "false"), ("submodule.vendor.ignore", "all"),
                           # The driver .gitattributes names for driven.*.
                           *(("diff.sample.algorithm", name) for name in ("histogram", "patience", "minimal"))):
            plain_git(self.repo, "config", key, value)
            try:
                with self.subTest(key=key, value=value):
                    self.assertNotEqual(porcelain(), default)  # the setting is in effect
                    self.assertEqual(whole_run(), default)
            finally:
                plain_git(self.repo, "config", "--unset", key)
        # The work tree's .gitmodules is no more read than its other files.
        (worktree / ".gitmodules").write_text('[submodule "vendor"]\n\tpath = vendor/sub\n\turl = ./nowhere\n'
                                              '\tignore = all\n')
        self.assertNotEqual(porcelain(), default)
        self.assertEqual(whole_run(), default)

    def story(self):
        """A run worktree one commit past ``base``, and the whole-run comparison as it reads."""
        base = git.head(self.repo)
        (self.repo / "story.txt").write_text("head\n")
        self.commit("story")
        worktree, binding = self.run_worktree()
        return base, worktree, binding, lambda: (git.range_diff_bytes(worktree, base, binding=binding),
                                                 git.range_paths(worktree, base, binding=binding))

    def test_the_comparison_names_a_new_absent_index_and_makes_nothing_whatever_tmpdir_says(self):
        base, worktree, binding, whole_run = self.story()
        default = whole_run()
        self.assertIn(b"+head\n", default[0])
        places = [worktree, Path(binding["git_dir"]), Path(binding["common_dir"])]
        launch, indexes = git._launch, []

        def recording(argv, env, **options):
            if "diff-tree" in argv:
                indexes.append(Path(env["GIT_INDEX_FILE"]))
            return launch(argv, env, **options)

        def entries(*tops):
            return {path for top in tops for path in top.rglob("*")}
        makers = ("mkdtemp", "mkstemp", "mktemp", "gettempdir", "gettempdirb", "TemporaryDirectory",
                  "NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile")
        # A TMPDIR in the worktree and in each Git directory, and an empty one elsewhere.
        for folder in (*(place / "tmp" for place in places), self.scratch / "empty"):
            folder.mkdir()
            with self.subTest(TMPDIR=str(folder)), patch.dict(os.environ, TMPDIR=str(folder)), \
                    patch.object(tempfile, "tempdir", None), contextlib.ExitStack() as stack:
                # What Git itself leaves in this TMPDIR (Apple's git shim may cache there): the rest would be Harness's.
                plain_git(worktree, "diff-tree", "-r", base, "HEAD")
                before = entries(folder, *places)
                used = [stack.enter_context(patch.object(tempfile, name, wraps=getattr(tempfile, name)))
                        for name in makers]
                with patch.object(git, "_launch", side_effect=recording):
                    self.assertEqual(whole_run(), default)
                self.assertEqual([call for maker in used for call in maker.mock_calls], [])
                self.assertEqual(entries(folder, *places), before)
        self.assertEqual(len(indexes), 2 * (len(places) + 1))
        self.assertEqual(len(set(indexes)), len(indexes))  # a new name for every call
        for index in indexes:
            self.assertEqual(index.parent, Path(os.path.dirname(os.devnull)))
            self.assertRegex(index.name, r"^harness-index-[0-9a-f]{32,}$")  # at least 128 random bits
            self.assertFalse(os.path.lexists(index))

    def test_an_index_name_that_exists_after_the_call_stops_the_comparison(self):
        base, worktree, binding, whole_run = self.story()
        folder = self.scratch / "absent"  # in place of the null device's folder, where no test may write
        folder.mkdir()
        launch = git._launch

        def created_by_git(argv, env, **options):
            result = launch(argv, env, **options)
            if "diff-tree" in argv:
                Path(env["GIT_INDEX_FILE"]).write_bytes(b"")
            return result
        with patch.object(git, "_ABSENT_INDEX_FOLDER", str(folder)):
            self.assertIn(b"+head\n", whole_run()[0])
            self.assertEqual(list(folder.iterdir()), [])
            with patch.object(git, "_launch", side_effect=created_by_git):
                for compare in (git.range_diff_bytes, git.range_paths):
                    with self.subTest(compare.__name__), self.assertRaises(HarnessError) as raised:
                        compare(worktree, base, binding=binding)
                    stop = stops.of(raised.exception)
                    self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "invariant"))
        self.assertEqual(len(list(folder.iterdir())), 2)


class StagedPatchTests(IsolatedRepoCase):
    """Case 17: index-to-tree comparisons stay porcelain diff --cached, for intent-to-add entries and renames."""

    def test_the_archived_staged_patch_is_porcelain_diff_cached_and_changed_lists_intent_to_add_once(self):
        owned = self.scratch.resolve() / "worktrees"
        worktree = owned / "run"
        plain_git(self.repo, "worktree", "add", "-q", "-b", "harness/run", str(worktree))
        binding = git.capture_worktree_binding(worktree, "harness/run")
        head = git.head(worktree, binding=binding)
        (worktree / "intent.txt").write_text("intent to add\n")
        plain_git(worktree, "add", "-N", "intent.txt")
        plain_git(worktree, "mv", "test_hello.py", "moved_hello.py")
        staged = plain_git(worktree, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "HEAD").stdout
        self.assertIn(b"rename from test_hello.py\nrename to moved_hello.py\n", staged)
        self.assertNotIn(b"intent.txt", staged)
        # intent.txt comes from the work tree against HEAD only; the rename's two paths from both comparisons.
        self.assertEqual(git.changed(worktree, binding=binding), ["intent.txt", "moved_hello.py", "test_hello.py"])
        archive = self.scratch / "archive"
        git.archive_and_reset(worktree, head, archive, owned, binding=binding)
        self.assertEqual((archive / "staged.diff").read_bytes(), staged)
        self.assertEqual((archive / "files" / "intent.txt").read_text(), "intent to add\n")
        self.assertTrue(git.clean(worktree, binding=binding))

    def test_changed_lists_an_intent_to_add_path_only_while_its_file_exists(self):
        worktree = self.scratch.resolve() / "run"
        plain_git(self.repo, "worktree", "add", "-q", "-b", "harness/run", str(worktree))
        binding = git.capture_worktree_binding(worktree, "harness/run")
        (worktree / "intent.txt").write_text("intent to add\n")
        plain_git(worktree, "add", "-N", "intent.txt")
        self.assertEqual(git.changed(worktree, binding=binding), ["intent.txt"])
        # Porcelain diff --cached does not see the entry; diff-index --cached would list it as a new file.
        (worktree / "intent.txt").unlink()
        self.assertEqual(git.changed(worktree, binding=binding), [])


class ReplaceRefTests(IsolatedRepoCase):
    """Case 8: a replacement object for the run's HEAD commit never stands in for the real tree."""

    def test_removability_is_judged_on_the_real_tree(self):
        result = self.engine().start("Implement greet")
        self.assertEqual(result["status"], "completed")
        run_id, worktree = result["id"], Path(result["worktree"])
        for cache in list(worktree.rglob("__pycache__")):
            shutil.rmtree(cache)
        head = plain_git(worktree, "rev-parse", "HEAD").stdout.decode().strip()
        # A replacement commit whose tree differs from HEAD's only in hello.py.
        blob = plain_git(self.repo, "hash-object", "-w", "--stdin", input=b"replaced\n").stdout.decode().strip()
        listing = plain_git(self.repo, "ls-tree", head).stdout.decode()
        entries = [line if not line.endswith("\thello.py") else f"100644 blob {blob}\thello.py"
                   for line in listing.splitlines()]
        self.assertNotEqual(entries, listing.splitlines())
        tree = plain_git(self.repo, "mktree", input=("\n".join(entries) + "\n").encode()).stdout.decode().strip()
        replacement = plain_git(self.repo, *IDENTITY, "commit-tree", tree, "-p", result["base_commit"],
                                "-m", "replacement").stdout.decode().strip()
        plain_git(self.repo, "replace", head, replacement)
        # Positive control: plain Git now reads the replacement's tree for HEAD.
        self.assertEqual(plain_git(worktree, "rev-parse", "HEAD:hello.py").stdout.decode().strip(), blob)
        # The worktree and its index are the real tree: removable, although the replacement differs.
        self.assertEqual(cleanup.cleanup_run(self.repo, run_id)["worktree"]["status"], "eligible")
        # The worktree and its index are the replacement's tree: not removable, although the replacement matches.
        (worktree / "hello.py").write_bytes(b"replaced\n")
        plain_git(worktree, "add", "hello.py")
        preview = cleanup.cleanup_run(self.repo, run_id)["worktree"]
        self.assertEqual(preview["status"], "blocked", preview)
        self.assertIn("HEAD", preview["error"])


class PartialCloneTests(IsolatedRepoCase):
    """Case 9: Harness Git never fetches a missing object, even where the repository allows its protocol."""

    def clone(self, *, checkout):
        server = self.repo
        plain_git(server, "config", "uploadpack.allowFilter", "true")
        plain_git(server, "config", "uploadpack.allowAnySHA1InWant", "true")
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        clone = Path(folder.name).resolve() / "clone"
        options = [] if checkout else ["--no-checkout"]
        plain_git(folder.name, "clone", "-q", "--filter=blob:none", *options, f"file://{server}", str(clone))
        plain_git(clone, "config", "protocol.file.allow", "always")
        if not checkout:
            # The working files and an index from the tree alone: HEAD's blobs stay missing in the clone.
            offline = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
            for name in (".gitignore", "test_hello.py"):
                (clone / name).write_bytes((server / name).read_bytes())
            plain_git(clone, "read-tree", "HEAD", env=offline)
            plain_git(clone, "update-index", "-q", "--refresh", env=offline)
            self.assertEqual(plain_git(clone, "status", "--porcelain", "--untracked-files=no", env=offline).stdout, b"")
        self.repo = clone  # the case's repository from here on, so its guards look at this one
        return clone

    def missing(self, clone, oid):
        offline = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
        return plain_git(clone, "cat-file", "-e", oid, env=offline, check=False).returncode != 0

    def test_a_missing_head_blob_fails_the_run_and_is_not_fetched(self):
        clone = self.clone(checkout=False)
        blob = plain_git(clone, "rev-parse", "HEAD:test_hello.py").stdout.decode().strip()
        self.assertTrue(self.missing(clone, blob))
        try:  # not assertRaises, which drops the traceback the guard's entry is made from
            self.engine().start("Implement greet")
        except HarnessError as caught:
            error = caught
        else:
            self.fail("the run started without HEAD's blobs")
        self.assertTrue(self.missing(clone, blob))
        # A Git failure at worktree creation is an untyped Git error by design. The guard sees this one stop
        # twice, saved by Workflow.stop and raised out of Runtime.start; nothing else may be in its list.
        self.assertIn("worktree add", str(error))
        innermost = traceback.extract_tb(error.__traceback__)[-1]
        this_stop = ("untyped_harness_error", "HarnessError", str(error)[:160],
                     f"{innermost.filename}:{innermost.lineno}")
        unclassified = self.stop_guard_findings["unclassified"]
        self.assertEqual(unclassified, [this_stop, this_stop])
        del unclassified[:2]

    def test_with_head_blobs_present_the_run_completes(self):
        clone = self.clone(checkout=True)
        blob = plain_git(clone, "rev-parse", "HEAD:test_hello.py").stdout.decode().strip()
        self.assertFalse(self.missing(clone, blob))
        self.assertEqual(self.engine().start("Implement greet")["status"], "completed")


class MainCheckoutTests(IsolatedRepoCase):
    """Case 10: starting a run neither rewrites the main checkout's index nor runs its hooks; no hook runs in the run."""

    def test_run_start_leaves_the_main_index_and_its_hooks_alone(self):
        markers = Markers(self.scratch)
        markers.arm(self.repo, events=("post-index-change",), programs=False, scopes=("repo",))
        expire_stats(self.repo / "test_hello.py")
        index = self.repo / ".git" / "index"
        before = index.read_bytes(), index.stat().st_mtime_ns
        markers.reset()
        self.assertEqual(self.engine().start("Implement greet")["status"], "completed")
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)
        main = str(self.repo.resolve())
        # In any checkout: every runtime call declares its effect, so the name read turns the configured hook
        # off where Git has no per-event switch, including in the new run worktree's index writes.
        self.assertEqual(markers.read(), [])
        # Positive control: plain Git's status rewrites that index and runs both hooks there.
        plain_git(self.repo, "status", "--porcelain", "--untracked-files=no")
        self.assertNotEqual(index.stat().st_mtime_ns, before[1])
        here = {label for label, where in markers.read() if where == main}
        self.assertIn("hookdir:post-index-change", here)
        if configured_hooks_exist():
            self.assertIn("config:repo-post-index-change", here)


# Runs in a fresh process: a plain-Git fixture (init, add, commit) and what that process's Git configuration holds.
ISOLATION_PROBE = r'''
import json, os, subprocess
from pathlib import Path

def isolation_probe():
    folder = Path(os.environ["ISOLATION_PROBE"])
    repo = folder / "repo"
    repo.mkdir()
    def run(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    run("init", "-q")
    for name in ("ignored.txt", "kept.txt"):
        (repo / name).write_text(name + "\n")
    run("add", ".")
    commit = run("-c", "user.name=Probe", "-c", "user.email=probe@localhost", "commit", "-qm", "fixture")
    (folder / "result.json").write_text(json.dumps({
        "environment": {key: value for key, value in os.environ.items()
                        if key in ("HOME", "XDG_CONFIG_HOME") or key.startswith("GIT_")},
        "exists": {key: Path(value).exists() for key, value in os.environ.items()
                   if key in ("HOME", "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL")},
        "commit": commit.returncode, "stderr": commit.stderr,
        "files": run("ls-files").stdout.split(), "branch": run("symbolic-ref", "--short", "HEAD").stdout.strip(),
        "developer": sorted(run("config", "--name-only", "--get-regexp", r"^developer\.").stdout.split())}))
'''


def private_environment(folder):
    """The HOME, XDG_CONFIG_HOME and GIT_* variables git_isolation sets in a process whose private folder is ``folder``."""
    paths = {"HOME": git_isolation.HOME, "XDG_CONFIG_HOME": git_isolation.XDG_CONFIG_HOME,
             "GIT_CONFIG_GLOBAL": git_isolation.GLOBAL_CONFIG}
    return {**{key: str(folder / path.relative_to(git_isolation.FOLDER)) for key, path in paths.items()},
            "GIT_CONFIG_NOSYSTEM": "1"}


@functools.lru_cache(maxsize=None)
def config_selectors_work():
    """Whether plain Git reads the files GIT_CONFIG_GLOBAL and GIT_CONFIG_SYSTEM name (Git 2.32 and later)."""
    with tempfile.TemporaryDirectory() as folder:
        # HOME and XDG_CONFIG_HOME hold no configuration, so Git that ignores the selectors reads neither probe file.
        env = {key: value for key, value in os.environ.items() if key not in git_isolation.SELECTORS}
        env.update(HOME=folder, XDG_CONFIG_HOME=folder)
        for scope in ("global", "system"):
            Path(folder, scope).write_text(f"[selectorprobe]\n\t{scope} = yes\n")
            env[f"GIT_CONFIG_{scope.upper()}"] = str(Path(folder, scope))
        read = subprocess.run(["git", "config", "--get-regexp", r"^selectorprobe\."], cwd=folder, env=env,
                              capture_output=True, text=True, timeout=60)
    return sorted(read.stdout.splitlines()) == ["selectorprobe.global yes", "selectorprobe.system yes"]


class GitIsolationTests(unittest.TestCase):
    """Case 12a: tests and the processes they start read only the private Git configuration of git_isolation."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()

    def test_private_configuration_reaches_runtime_calls_and_fixed_switches_win(self):
        self.assertNotEqual(os.environ["HOME"], git_isolation.ORIGINAL_HOME)
        self.assertEqual(os.environ["GIT_CONFIG_GLOBAL"], str(git_isolation.GLOBAL_CONFIG))
        self.assertEqual(os.environ["GIT_CONFIG_NOSYSTEM"], "1")
        ignore = self.root / "ignore"
        ignore.write_text("build/\n")
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "pre-commit").write_text("#!/bin/sh\necho 'global pre-commit refused' >&2\nexit 1\n")
        (hooks / "pre-commit").chmod(0o755)
        repo, elsewhere = self.root / "repo", self.root / "elsewhere"
        repo.mkdir()
        elsewhere.mkdir()
        with git_isolation.user_config(f"[core]\n\texcludesFile = {ignore}\n\thooksPath = {hooks}\n") as config:
            # RepoCase's fixture: calls with no declared effect, so only the fixed switches apply; the commit succeeds.
            git.git(repo, "init", "-b", "main")
            (repo / "a.txt").write_text("a\n")
            git.git(repo, "add", ".")
            git.git(repo, *IDENTITY, "commit", "-m", "fixture")
            (repo / "a.txt").write_text("changed\n")
            (repo / "build").mkdir()
            (repo / "build" / "out.txt").write_text("built\n")
            # Git 2.32 and later find the global file through GIT_CONFIG_GLOBAL; every Git finds it through HOME.
            for route in ("GIT_CONFIG_GLOBAL", "HOME") if config_selectors_work() else ("HOME",):
                with self.subTest(route=route), patch.dict(os.environ):
                    if route == "GIT_CONFIG_GLOBAL":
                        os.environ.update(HOME=str(elsewhere), XDG_CONFIG_HOME=str(elsewhere))
                    else:
                        del os.environ["GIT_CONFIG_GLOBAL"]
                    self.assertEqual(git.changed(repo), ["a.txt"])
                    self.assertEqual(sorted(git.snapshot(repo)["files"]), ["a.txt"])
                    self.assertEqual(git.git(repo, "config", "--show-origin", "--get-regexp", r"^core\.excludesfile$",
                                             effect=git.READ), f"file:{config}\tcore.excludesfile {ignore}\n")
            before = git.head(repo)
            self.assertNotEqual(git.accept(repo, ["a.txt"], "run_isolation", "task", {"subject": "Accept", "body": ""}),
                                before)
            self.assertEqual(git.changed(repo), [])
            (repo / "a.txt").write_text("plain\n")
            plain = plain_git(repo, *IDENTITY, "commit", "-qam", "plain", check=False)
            self.assertNotEqual(plain.returncode, 0)  # positive control: plain Git runs the global hook
            self.assertIn(b"global pre-commit refused", plain.stderr)
        self.assertEqual(git.changed(repo), ["a.txt", "build/out.txt"])  # positive control: the ignore was global
        self.assertEqual(run_shards.child_env(ROOT)["HOME"], str(git_isolation.HOME))

    def developer_environment(self):
        """A developer's shell: their own HOME and XDG configuration, a system file, a stray GIT_* variable."""
        home = self.root / "developer"
        hooks, xdg = home / "hooks", home / "xdg"
        hooks.mkdir(parents=True)
        (xdg / "git").mkdir(parents=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
        (hooks / "pre-commit").chmod(0o755)
        (home / ".gitconfig").write_text(f"[developer]\n\thome = seen\n[core]\n\thooksPath = {hooks}\n"
                                         "[init]\n\tdefaultBranch = developer\n[commit]\n\tgpgsign = true\n"
                                         "[gpg]\n\tprogram = false\n")
        (xdg / "git" / "config").write_text("[developer]\n\txdg = seen\n")
        (xdg / "git" / "ignore").write_text("ignored.txt\n")  # Git's default core.excludesFile
        (home / "system.gitconfig").write_text("[developer]\n\tsystem = seen\n")
        env = {key: value for key, value in os.environ.items() if key not in git_isolation.SELECTORS}
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(xdg), GIT_CONFIG_SYSTEM=str(home / "system.gitconfig"),
                   GIT_CONFIG_PARAMETERS="'developer.parameters=seen'", PYTHONPATH=str(ROOT / "tests"))
        return env

    def probe(self, name, argv, env):
        folder = self.root / name
        folder.mkdir()
        process = subprocess.run(argv, cwd=ROOT, env={**env, "ISOLATION_PROBE": str(folder)}, capture_output=True,
                                 text=True, timeout=300, stdin=subprocess.DEVNULL)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        return json.loads((folder / "result.json").read_text())

    def test_fresh_test_processes_never_read_the_developer_configuration(self):
        env = self.developer_environment()
        exposed = self.probe("plain", [sys.executable, "-c", ISOLATION_PROBE + "isolation_probe()"], env)
        # Positive control: a process that imports neither entry point reads all of it; the system file only through
        # GIT_CONFIG_SYSTEM, which Git before 2.32 ignores.
        scopes = ("home", "parameters", "system", "xdg") if config_selectors_work() else ("home", "parameters", "xdg")
        self.assertEqual(exposed["developer"], [f"developer.{scope}" for scope in scopes])
        self.assertEqual((exposed["files"], exposed["branch"]), (["kept.txt"], "developer"))
        self.assertNotEqual(exposed["commit"], 0)
        suite = self.root / "suite"
        (suite / "tests").mkdir(parents=True)
        # A shard module that imports nothing of the Harness tests: it relies on what run_shards passes down.
        (suite / "tests" / "test_isolation_probe.py").write_text(
            "import unittest\n" + ISOLATION_PROBE
            + "\nclass Probe(unittest.TestCase):\n    def test_probe(self):\n        isolation_probe()\n")
        runs = {"unittest": [sys.executable, "-c", "import test_harness\n" + ISOLATION_PROBE + "isolation_probe()"],
                "shards": [sys.executable, "-c", "import sys, run_shards; from pathlib import Path; "
                           "sys.exit(run_shards.main(sys.argv[2:], root=Path(sys.argv[1])))",
                           str(suite), "--shards", "1", "--out", str(self.root / "shard-logs")]}
        for name, argv in runs.items():
            with self.subTest(name):
                result = self.probe(name, argv, env)
                # The child got git_isolation's variables for a private folder, which exists, and no other GIT_*.
                folder = Path(result["environment"].get("HOME", "")).parent
                self.assertEqual(result["environment"], private_environment(folder))
                self.assertEqual(result["exists"], dict.fromkeys(("HOME", "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL"), True))
                self.assertEqual(result["developer"], [])
                self.assertEqual(result["commit"], 0, result["stderr"])
                self.assertEqual(result["files"], ["ignored.txt", "kept.txt"])
                self.assertNotEqual(result["branch"], "developer")


@unittest.skipUnless(configured_hooks_exist(), "configured hooks start with Git 2.54")
class GlobalConfiguredHookTests(unittest.TestCase):
    """Case 12b: a refusing configured hook in the global configuration, written after the fixture commit."""

    def test_declared_calls_turn_it_off_by_name_and_commit(self):
        isolate_git(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        repo = root / "repo"
        repo.mkdir()
        plain_git(repo, "init", "-q", "-b", "main")
        (repo / "a.txt").write_text("a\n")
        plain_git(repo, "add", "a.txt")
        plain_git(repo, *IDENTITY, "commit", "-qm", "fixture")
        markers = Markers(root)
        refuse = markers.script("refuse.sh", f"printf '%s %s\\n' \"$1\" \"$(pwd -P)\" >> '{markers.log}'\nexit 1\n")
        hook = f"[hook \"refuse\"]\n\tcommand = '{refuse}' refuse\n" + "".join(
            f"\tevent = {event}\n" for event in ("pre-commit", "reference-transaction", "post-index-change"))
        # As on Git without per-event switches (2.54.0): only the name read a declared effect starts can turn it off.
        with git_isolation.user_config(hook), patch("harness.git.HOOK_EVENTS", ()):
            (repo / "a.txt").write_text("accepted\n")
            plain = plain_git(repo, *IDENTITY, "commit", "-qam", "plain", check=False)
            self.assertNotEqual(plain.returncode, 0)  # positive control: plain Git runs the hook
            self.assertEqual(markers.kinds(), {"refuse"})
            markers.reset()
            with self.assertRaises(HarnessError):  # a fixture call that declares nothing reads no names
                git.git(repo, *IDENTITY, "commit", "-qam", "undeclared")
            self.assertEqual(markers.kinds(), {"refuse"})
            markers.reset()
            before = git.head(repo)
            accepted = git.accept(repo, ["a.txt"], "run_hook", "task", {"subject": "Accept", "body": ""})
            (repo / "b.txt").write_text("b\n")
            git.git(repo, "add", "b.txt", effect=git.INDEX)
            git.git(repo, *IDENTITY, "commit", "-qm", "declared", effect=git.INDEX_REFS)
            self.assertEqual(markers.read(), [])
        self.assertEqual(plain_git(repo, "rev-parse", "HEAD^", "HEAD~2").stdout.decode().split(), [accepted, before])
        self.assertEqual(plain_git(repo, "show", "-s", "--format=%s", accepted).stdout, b"Accept\n")
        self.assertEqual(plain_git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout, b"a.txt\nb.txt\n")
        self.assertEqual(plain_git(repo, "show", "HEAD:a.txt").stdout, b"accepted\n")


class InitNameReadTests(unittest.TestCase):
    """init reads hook names where it makes its repository: that one's, if it exists, and the user and system files.

    Never a repository further up, however the path to the new one is spelt.
    """

    def setUp(self):
        isolate_git(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.parent = Path(folder.name).resolve() / "parent"
        self.parent.mkdir()
        plain_git(self.parent, "init", "-q", "-b", "main")

    def below(self, name):
        """An empty folder in the parent repository where plain Git, asked for hook names, finds that repository."""
        folder = self.parent / name
        folder.mkdir()
        return folder, plain_git(folder, "config", "--name-only", "--get-regexp", r"^hook\..*\.command$", check=False)

    def test_demo_and_smoke_fixtures_are_made_below_a_repository_with_a_broken_configuration(self):
        with (self.parent / ".git" / "config").open("a") as config:
            config.write("[broken\n")
        for name in ("plain", "with:colon"):  # ':' separates the entries of Git's path lists
            demos, found = self.below(name)
            self.assertEqual(found.returncode, 128)  # positive control: Git reads the broken file from there
            (demos / "control").mkdir()
            plain_git(demos / "control", "init", "-q", "-b", "main")  # and plain Git still makes a repository there
            with self.subTest(name, fixture="demo"):
                output = io.StringIO()
                with patch.object(tempfile, "tempdir", str(demos)), contextlib.redirect_stdout(output), \
                        contextlib.redirect_stderr(io.StringIO()):
                    cli.demo(Config.load(ROOT / "harness.toml"))
                demo = json.loads(output.getvalue())
                self.assertEqual((demo["status"], Path(demo["repo"]).parent), ("completed", demos))
            for fixture, prepare in (("permission", permission_smoke.prepare_fixture),
                                     ("tool", tool_smoke.prepare_fixture)):
                with self.subTest(name, fixture=fixture):
                    folder = demos / fixture
                    prepare(folder)
                    head = plain_git(folder / "repo", "rev-parse", "HEAD").stdout
                    self.assertEqual(plain_git(folder / "worktree", "rev-parse", "HEAD").stdout, head)
                    self.assertEqual(plain_git(folder / "worktree", "symbolic-ref", "-q", "HEAD", check=False).returncode, 1)

    def test_a_hook_configured_in_a_repository_further_up_is_not_read_for_init(self):
        plain_git(self.parent, "config", "hook.parent.command", "true")
        plain_git(self.parent, "config", "hook.parent.event", "reference-transaction")
        demo, _ = self.below("demo")
        smoke, _ = self.below("smoke")
        colon = self.below("with:colon")[0] / "x"
        colon.mkdir()
        mixed, _ = self.below("mixed-case")
        # On a case-insensitive file system another spelling names the same folder.
        spelt = self.parent.with_name(self.parent.name.upper()) / mixed.name.upper()
        link = self.parent.parent / "link"
        link.symlink_to(self.below("target")[0], target_is_directory=True)
        again, _ = self.below("again")
        plain_git(again, "init", "-q", "-b", "main")
        plain_git(again, "config", "hook.again.command", "true")
        inside, _ = self.below("inside")
        (inside / "f").write_text("f\n")
        # A linked checkout below the parent, of another repository, whose gitfile names its Git directory relatively.
        main = self.parent.parent / "main"
        main.mkdir()
        plain_git(main, "init", "-q", "-b", "main")
        plain_git(main, *IDENTITY, "commit", "--allow-empty", "-qm", "fixture")
        plain_git(main, "config", "extensions.worktreeConfig", "true")
        plain_git(main, "config", "hook.common.command", "true")
        linked = self.parent / "linked"
        plain_git(main, "worktree", "add", "-q", "--detach", str(linked))
        plain_git(linked, "config", "--worktree", "hook.linked.command", "true")
        admin = Path(plain_git(linked, "rev-parse", "--absolute-git-dir").stdout.decode().strip())
        (linked / ".git").write_text(f"gitdir: {os.path.relpath(admin, linked)}\n")
        for path in (demo, colon, link, spelt) if spelt.is_dir() else (demo, colon, link):
            # Positive control: from each of these, Git finds the parent repository and its hook.
            read = plain_git(path, "config", "--name-only", "--get-regexp", r"^hook\..*\.command$")
            self.assertEqual(read.stdout, b"hook.parent.command\n")
        # Positive control: through the gitfile Git reads the shared configuration and the checkout's config.worktree.
        read = plain_git(linked, "config", "--name-only", "--get-regexp", r"^hook\..*\.command$")
        self.assertEqual(read.stdout, b"hook.common.command\nhook.linked.command\n")
        user, own = "hook.user.enabled", "hook.again.enabled"
        inits = {"right below the parent's worktree root": (demo, {}, [user]),
                 "isolated": (smoke, {"isolated_config": True}, []), "':' in the path": (colon, {}, [user]),
                 "through a symbolic link": (link, {}, [user]), "again": (again, {}, [own, user]),
                 "again, isolated": (again, {"isolated_config": True}, [own]),
                 "again, in a linked checkout": (linked, {}, ["hook.common.enabled", "hook.linked.enabled", user])}
        if spelt.is_dir():
            inits["another spelling"] = (spelt, {}, [user])
        git_dirs = {linked: admin}  # the others' Git directory is their own .git
        real, turned_off = git._launch, []

        def record(argv, env, **kwargs):
            if "--get-regexp" not in argv:
                turned_off.append(sorted(value for key, value in env.items() if key.startswith("GIT_CONFIG_KEY_")))
            return real(argv, env, **kwargs)
        with git_isolation.user_config('[hook "user"]\n\tcommand = true\n\tevent = reference-transaction\n'), \
                patch.object(git, "_launch", side_effect=record):
            for label, (repo, options, expected) in inits.items():
                with self.subTest(label):
                    turned_off.clear()
                    git.git(repo, "init", "-q", "-b", "main", effect=git.CHECKOUT, **options)
                    self.assertEqual(turned_off, [expected])
                    made = plain_git(repo, "rev-parse", "--absolute-git-dir").stdout.decode().strip()
                    self.assertTrue(os.path.samefile(made, git_dirs.get(repo, repo / ".git")))
            # Any other call below the parent works in the parent repository, and its read sees the parent's hook.
            turned_off.clear()
            git.git(inside, "add", "--", "f", effect=git.INDEX)
        self.assertEqual(turned_off, [["hook.parent.enabled", user]])


@unittest.skipUnless(configured_hooks_exist(), "configured hooks start with Git 2.54")
class ConfiguredHookTests(unittest.TestCase):
    """Case 15a: configured hooks are off through the hook name read and, where Git has them, per-event switches."""

    def setUp(self):
        isolate_git(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        plain_git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "x").write_text("x\n")
        plain_git(self.repo, "add", "x")
        plain_git(self.repo, *IDENTITY, "commit", "-qm", "fixture")
        self.head = plain_git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()
        self.markers = Markers(self.root)
        self.markers.arm(self.repo, events=("post-checkout", "reference-transaction"), programs=False,
                         hook_directory=False)
        self.count = 0

    def labels(self):
        return {label for label, _ in self.markers.read()}

    def harness_calls(self):
        """worktree add and update-ref through the Harness construction, each declaring its effect."""
        self.count += 1
        self.markers.reset()
        git.git(self.repo, "worktree", "add", "--detach", str(self.root / f"tree-{self.count}"), "HEAD",
                effect=git.CHECKOUT)
        git.git(self.repo, "update-ref", "--no-deref", f"refs/heads/ref-{self.count}", self.head, "", effect=git.REFS)
        return self.labels()

    def test_name_read_and_event_switches_each_turn_configured_hooks_off(self):
        everything = {f"config:{scope}-{event}" for scope in ("repo", "glob")
                      for event in ("post-checkout", "reference-transaction")}
        plain_git(self.repo, "worktree", "add", "--detach", str(self.root / "plain"), "HEAD")
        plain_git(self.repo, "update-ref", "refs/heads/plain", self.head)
        self.assertEqual(self.labels(), everything)  # positive control: plain Git runs all four
        with patch("harness.git.HOOK_EVENTS", ()):
            self.assertEqual(self.harness_calls(), set())  # the name read alone
            with patch("harness.git._configured_hooks", return_value=[]):
                self.assertEqual(self.harness_calls(), everything)  # neither: positive control
            # A configuration that enables a hook by name still loses to the name read.
            plain_git(self.repo, "config", "hook.repo-post-checkout.enabled", "true")
            plain_git(self.repo, "config", "hook.glob-reference-transaction.enabled", "true")
            self.assertEqual(self.harness_calls(), set())
        if event_switches_exist():
            with patch("harness.git._configured_hooks", return_value=[]):
                self.assertEqual(self.harness_calls(), set())  # the per-event switches alone


class Trace2Tests(IsolatedRepoCase):
    """Case 16: Trace2 targets in the global configuration receive nothing from Harness Git calls."""

    def test_status_preview_and_a_run_write_no_trace2_log(self):
        logs = {key: self.scratch / f"{key}.log" for key in ("event", "normal", "perf")}
        for path in logs.values():
            path.write_text("")
        self.global_config.write_text("[trace2]\n" + "".join(f"\t{key}Target = {path}\n" for key, path in logs.items()))
        run = self.engine().start("Implement greet")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(self.cli("status", run["id"])[0], 0)
        code, output, errors = self.cli("cleanup-run", run["id"])
        self.assertEqual(json.loads(output)["id"], run["id"], errors)
        self.assertEqual({key: path.read_bytes() for key, path in logs.items()}, dict.fromkeys(logs, b""))
        plain_git(self.repo, "rev-parse", "HEAD")
        self.assertTrue(all(path.stat().st_size for path in logs.values()))  # positive control


class DeadlineTests(unittest.TestCase):
    """Case 18: the hook name read and the call share one monotonic deadline."""

    def setUp(self):
        isolate_git(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Path(folder.name).resolve()
        self.now = 1000.0
        clock = SimpleNamespace(monotonic=lambda: self.now)
        for target in ("harness.git.time", "harness.operations.time"):
            patcher = patch(target, clock)
            patcher.start()
            self.addCleanup(patcher.stop)

    @contextlib.contextmanager
    def launches(self, spend=0.0, read_raises=None, passthrough=("symbolic-ref",), check_spend=0.0):
        """Record (argv, timeout); the name read spends ``spend`` seconds of the fake clock, or raises.

        A binding check (``symbolic-ref``) runs for real and spends ``check_spend`` seconds.
        """
        real, started = subprocess.run, []

        def fake(argv, **kwargs):
            started.append((list(argv), kwargs.get("timeout")))
            if any(word in argv for word in passthrough):
                result = real(argv, **kwargs)
                self.now += check_spend
                return result
            if "--get-regexp" in argv:
                if read_raises is not None:
                    raise read_raises
                self.now += spend
                return subprocess.CompletedProcess(argv, 1, b"", b"")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        with patch("harness.git.subprocess.run", side_effect=fake):
            yield started

    def call(self, layer):
        return {"git": lambda: git.git(self.repo, "add", "--", "x", effect=git.INDEX),
                "git_process": lambda: git.git_process(self.repo, "add", "--", "x", effect=git.INDEX),
                "_run_git": lambda: git._run_git(self.repo, ("add", "--", "x"), effect=git.INDEX)}[layer]

    def test_the_call_gets_what_the_name_read_left(self):
        with self.launches(spend=20) as started:
            git.git(self.repo, "add", "--", "x", effect=git.INDEX)
        self.assertEqual(len(started), 2)
        self.assertIn("--get-regexp", started[0][0])
        self.assertEqual(started[0][1], 60)
        self.assertLessEqual(started[1][1], 40)
        with self.launches(spend=20) as started:
            git.git(self.repo, "rev-parse", "HEAD", effect=git.READ)
        self.assertEqual([timeout for _, timeout in started], [60])

    def test_a_name_read_that_uses_the_budget_or_times_out_starts_no_call(self):
        for case, options in (("budget spent", {"spend": 60}),
                              ("read timed out", {"read_raises": subprocess.TimeoutExpired(["git"], 60)})):
            for layer in ("git", "_run_git", "git_process"):
                with self.subTest(case, layer=layer):
                    expected = subprocess.TimeoutExpired if layer == "git_process" else HarnessError
                    with self.launches(**options) as started, self.assertRaises(expected) as raised:
                        self.call(layer)()
                    self.assertEqual(len(started), 1)
                    if expected is HarnessError:
                        self.assertTrue(str(raised.exception).startswith("git add -- x failed"), raised.exception)

    def test_installation_status_shares_its_fifteen_seconds(self):
        for spend, expected in ((15, None), (10, 5)):
            with self.subTest(spend=spend):
                waits = []

                class Process:
                    returncode = 0

                    def communicate(self, timeout=None):
                        waits.append(timeout)
                        return b"", None
                with self.launches(spend=spend), \
                        patch("harness.operations.subprocess.Popen", return_value=Process()) as popen:
                    output = operations._git_query(self.repo, "status", "--porcelain", "--", "harness",
                                                   effect=git.COMPARE)
                if expected is None:
                    self.assertIsNone(output)
                    popen.assert_not_called()
                else:
                    self.assertEqual(output, "")
                    self.assertEqual(len(waits), 1)
                    self.assertLessEqual(waits[0], expected)

    def test_binding_checks_have_their_own_budget(self):
        repo, tree = self.repo / "repo", self.repo / "tree"
        repo.mkdir()
        plain_git(repo, "init", "-q", "-b", "main")
        (repo / "x").write_text("x\n")
        plain_git(repo, "add", "x")
        plain_git(repo, *IDENTITY, "commit", "-qm", "fixture")
        plain_git(repo, "worktree", "add", "-q", "-b", "harness/run", str(tree))
        binding = git.capture_worktree_binding(tree, "harness/run")
        # Each check spends 30 seconds: the call's budget starts only after the first one.
        with self.launches(spend=20, check_spend=30) as started:
            git.git_process(tree, "add", "--", "x", effect=git.INDEX, binding=binding)
        self.assertEqual([("symbolic-ref" in argv, "--get-regexp" in argv) for argv, _ in started],
                         [(True, False), (False, True), (False, False), (True, False)])
        self.assertEqual([timeout for _, timeout in started], [60, 60, 40, 60])


# The Git runners by qualified name, with the short name the rules use: harness.git's, and the installation query.
RUNNERS = {**{f"harness.git.{name}": name for name in ("git", "git_bytes", "git_process", "_run_git", "invocation")},
           "harness.operations._git_query": "_git_query"}
# A runner's module may be bound only under its own name, so ``git.<runner>`` and ``operations._git_query`` are the forms.
RUNNER_MODULES = ("harness.git", "harness.operations")
EFFECTS = {f"harness.git.{name}" for name in ("READ", "COMPARE", "PRIVATE", "INDEX", "REFS", "INDEX_REFS", "CHECKOUT")}
REQUIRED_OPTIONS = {**dict.fromkeys(("diff", "diff-tree", "diff-index", "log", "show"), ("--no-ext-diff", "--no-textconv")),
                    "update-ref": ("--no-deref",)}
# The launch layer's forwarding calls, (module, top-level function, runner): its two positional arguments, and its
# effect. "repo" and "root" are its own first parameter; "worktree" is run_git_query's parameter, which it sets to its
# binding's worktree. "*args" and "args" are its own ``args`` parameter, starred or not; "parsed" is the one local
# ``args = _git_args(...)``. The effect is its own ``effect`` parameter or the constant READ. Parameters are never
# bound again, and ``args`` is never changed in place. Nothing else is exempt.
FORWARDS = {("git", "git", "git_bytes"): (("repo", "*args"), "effect"),
            ("git", "git_bytes", "git_process"): (("repo", "*args"), "effect"),
            ("git", "git_process", "invocation"): (("repo", "args"), "effect"),
            ("git", "_run_git", "invocation"): (("repo", "args"), "effect"),
            ("operations", "_git_query", "invocation"): (("root", "args"), "effect"),
            ("git_query", "run_git_query", "invocation"): (("worktree", "parsed"), "READ")}
# harness.git's fixed settings for the whole-run comparison: only _range_diff passes them, as the module constant,
# and only these two forwards pass them on, as their own parameter of that name. No call passes keywords through **.
CONFIG, CONFIG_CONSTANT = "_config", "_RANGE_DIFF_CONFIG"
CONFIG_FORWARDS = {("git_bytes", "git_process"), ("git_process", "invocation")}
STARTERS = {f"subprocess.{name}" for name in ("run", "Popen", "call", "check_call", "check_output")}


def call_shape_problems(sources):
    """R1 over {module: source} of harness/: the problems as (place, what), and how many Git calls each module has."""
    problems, checked = [], collections.Counter()
    for module, source in sources.items():
        tree = ast.parse(source)
        imports = [(node, *bound_by(node, alias)) for node in ast.walk(tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names]
        imported = {local: target for _, local, target in imports}

        def qualified(node):
            """The dotted name ``node`` stands for: imports resolved, any other bare name taken as this module's own."""
            if isinstance(node, ast.Name):
                return imported.get(node.id, f"harness.{module}.{node.id}")
            if isinstance(node, ast.Attribute):
                base = qualified(node.value)
                return base and f"{base}.{node.attr}"
            return None

        def word(node):
            return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

        def config_problems(function, scope, call, name):
            """Who passes harness.git's fixed settings to ``name``, and in which form."""
            found = ["passes keywords through **"] if any(keyword.arg is None for keyword in call.keywords) else []
            value = next((keyword.value for keyword in call.keywords if keyword.arg == CONFIG), None)
            if value is None:
                return found
            if module != "git":
                return [*found, f"passes {CONFIG} outside harness.git"]
            if (function, name) in CONFIG_FORWARDS:
                return found if own_parameter(scope, value, CONFIG) else [*found, f"forwards a {CONFIG} other than its own"]
            if function != "_range_diff":
                return [*found, f"passes {CONFIG} outside _range_diff"]
            if (qualified(value) != f"harness.git.{CONFIG_CONSTANT}" or bindings(scope, CONFIG_CONSTANT)
                    or CONFIG_CONSTANT in parameters(scope)):  # a parameter of any kind shadows the constant too
                return [*found, f"passes a {CONFIG} other than {CONFIG_CONSTANT}"]
            return found

        def nodes(parent, scope):
            """Every node with its innermost enclosing function or lambda."""
            for child in ast.iter_child_nodes(parent):
                yield child, scope
                yield from nodes(child, child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                                 else scope)
        for node, local, target in imports:
            if target in RUNNERS or target in RUNNER_MODULES and local != target.rsplit(".", 1)[1]:
                problems.append((f"harness/{module}.py:{node.lineno}", "imports a Git runner under another name"))
        tops = {node: node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        called = set()
        for node, scope in nodes(tree, None):
            place = f"harness/{module}.py:{getattr(node, 'lineno', 0)}"
            function = tops.get(scope)  # forwards and _launch are top-level functions, never nested ones
            if not isinstance(node, ast.Call):
                continue
            name = RUNNERS.get(qualified(node.func))
            if name is not None:
                checked[module] += 1
                called.add(node.func)
                problems += [(place, what) for what in config_problems(function, scope, node, name)]
                effect = next((keyword.value for keyword in node.keywords if keyword.arg == "effect"), None)
                forward = FORWARDS.get((module, function, name))
                if forward is not None:
                    passing, passed = forward
                    if not forwards_args(scope, node, passing):
                        problems.append((place, "forwards something other than its repository and args"))
                    if not (passed == "effect" and own_parameter(scope, effect, "effect")
                            or passed == "READ" and qualified(effect) == "harness.git.READ"):
                        problems.append((place, f"forwards an effect other than {passed}"))
                    continue
                if qualified(effect) not in EFFECTS:
                    problems.append((place, "declares no effect constant"))
                listed = node.args[1] if len(node.args) > 1 else None
                if name in ("_run_git", "invocation"):
                    words = listed.elts if isinstance(listed, (ast.List, ast.Tuple)) else []
                else:
                    words = node.args[1:]
                subcommand = word(words[0]) if words else None
                if subcommand is None or subcommand.startswith("-"):
                    problems.append((place, "has no literal subcommand first"))
                    continue
                literals = set(map(word, words))
                for option in REQUIRED_OPTIONS.get(subcommand, ()):
                    if option not in literals:
                        problems.append((place, f"runs {subcommand} without {option}"))
            starter = qualified(node.func) or ""
            if starter in STARTERS or starter.startswith(("os.exec", "os.posix_spawn")):
                if module == "git":
                    if (function, starter) != ("_launch", "subprocess.run"):
                        problems.append((place, "starts a process outside _launch"))
                    continue
                for argument in [*node.args, *(keyword.value for keyword in node.keywords if keyword.arg == "args")]:
                    first = argument.elts[0] if isinstance(argument, (ast.List, ast.Tuple)) and argument.elts else argument
                    if word(first) == "git":
                        problems.append((place, "starts git directly"))
        for node in ast.walk(tree):
            if (isinstance(node, (ast.Name, ast.Attribute)) and qualified(node) in RUNNERS
                    and isinstance(node.ctx, ast.Load) and node not in called):
                problems.append((f"harness/{module}.py:{node.lineno}", "uses a Git runner other than in a call"))
    return problems, checked


def bound_by(node, alias):
    """The local name an import binds and the qualified name it stands for; relative imports are inside harness."""
    if isinstance(node, ast.ImportFrom):
        base = ".".join(filter(None, ("harness", node.module))) if node.level else node.module
        return alias.asname or alias.name, f"{base}.{alias.name}"
    if alias.asname:
        return alias.asname, alias.name
    return alias.name.split(".")[0], alias.name.split(".")[0]  # import a.b binds a


def bindings(function, name):
    """Every node in ``function``, nested scopes included, that binds ``name`` or declares it nonlocal or global.

    A Name that is not loaded covers assignment, augmented and annotated assignment, for, with ... as, :=,
    del and comprehension targets; the rest bind by a bare string. A binding in a nested scope that does
    not reach ``function``'s own name still counts: a strange forward is reported rather than let through.
    """
    def binds(node):
        if isinstance(node, ast.Name):
            return node.id == name and not isinstance(node.ctx, ast.Load)
        if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            return node.name == name
        if isinstance(node, ast.MatchMapping):
            return node.rest == name  # case {**rest}
        if isinstance(node, ast.alias):
            return (node.asname or node.name.split(".")[0]) == name  # import a.b binds a
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            return name in node.names
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return node is not function and node.name == name
        return False
    return [node for node in ast.walk(function) if binds(node)]


def parameters(function):
    """Every parameter name of ``function``, of every kind: positional-only, ordinary, *args, keyword-only, **kwargs."""
    spec = function.args
    return {item.arg for item in (*spec.posonlyargs, *spec.args, spec.vararg, *spec.kwonlyargs, spec.kwarg)
            if item is not None}


def takes(function, name):
    """Whether ``function``'s parameter ``name`` takes the one value passed in its place or under its name.

    ``*name`` holds the caller's other positions and ``**name`` its other keywords, so neither does; the one exception
    is the forwards' own ``*args``, which holds the Git arguments they pass on.
    """
    spec = function.args
    return (name in {item.arg for item in (*spec.posonlyargs, *spec.args, *spec.kwonlyargs)}
            or name == "args" and spec.vararg is not None and spec.vararg.arg == name)


def own_parameter(function, node, name):
    """Whether ``node`` is the plain name of ``function``'s own parameter ``name``, which it never binds again."""
    return isinstance(node, ast.Name) and node.id == name and takes(function, name) and not bindings(function, name)


def forwards_args(function, call, passing):
    """Whether the positional arguments of ``call`` are exactly the two FORWARDS gives for ``function``.

    Compared by shape, not counted: ``*(repo, "status")`` is one node, yet it adds a subcommand.
    """
    repository, forwarded = passing
    if len(call.args) != 2 or changes_args(function):
        return False
    first, second = call.args
    if repository == "worktree":
        if not (isinstance(first, ast.Name) and first.id == "worktree" and takes(function, "worktree")):
            return False
    elif ([item.arg for item in (*function.args.posonlyargs, *function.args.args)][:1] != [repository]
          or not own_parameter(function, first, repository)):
        return False
    if forwarded == "*args":
        return isinstance(second, ast.Starred) and own_parameter(function, second.value, "args")
    if forwarded == "args":
        return own_parameter(function, second, "args")
    # "parsed": no parameter, bound once, by args = _git_args(...)
    assigned = [node for node in ast.walk(function) if isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "args"]
    return (isinstance(second, ast.Name) and second.id == "args" and "args" not in parameters(function)
            and len(assigned) == 1 and bindings(function, "args") == assigned[0].targets
            and isinstance(assigned[0].value, ast.Call) and isinstance(assigned[0].value.func, ast.Name)
            and assigned[0].value.func.id == "_git_args")


def changes_args(function):
    """Whether ``function``, nested scopes included, may change ``args`` in place, as it may a list from _git_args.

    Any attribute of ``args`` counts, so no list of methods is needed, and so does an item or slice of it that is
    assigned, augmented or deleted; reading an item (``args[:3]``) does not.
    """
    return any(isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.value, ast.Name)
               and node.value.id == "args" and (isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load))
               for node in ast.walk(function))


class CallShapeTripwireTests(unittest.TestCase):
    """Case 13, R1: every Harness Git call declares its effect, names its subcommand first and starts in harness.git."""

    def test_every_runtime_git_call_declares_an_effect_and_starts_through_the_launch_layer(self):
        sources = {path.stem: path.read_text(encoding="utf-8") for path in sorted((ROOT / "harness").glob("*.py"))}
        problems, checked = call_shape_problems(sources)
        self.assertEqual(problems, [])
        self.assertGreater(checked.pop("git"), 0)
        self.assertGreater(sum(checked.values()), 0)

    def test_the_tripwire_finds_each_kind_of_breach(self):
        sources = {
            "git": ("import subprocess\n"
                    "def git(repo, *args, effect=None):\n    return git_bytes(repo, *args, effect=READ)\n"
                    "def git_bytes(repo, *args, effect=None):\n    return git_process(repo, 'status', effect=effect)\n"
                    "def head(repo):\n    return git(repo, 'rev-parse', 'HEAD')\n"
                    "def launch(argv):\n    return subprocess.run(argv)\n"),
            "memory": ("from . import git\n"
                       "def bind(root):\n"
                       "    git.git_process(root, 'show', 'HEAD:x', effect=git.READ)\n"
                       "    git.git(root, '--literal-pathspecs', 'add', '--', 'x', effect=git.INDEX)\n"
                       "    git.git(root, 'update-ref', 'refs/heads/x', 'y', effect=git.REFS)\n"
                       "    git.git(root, 'status', effect=None)\n"
                       "    git._run_git(root, ('diff', '--no-ext-diff', 'HEAD'), effect=git.COMPARE)\n"
                       "    run = git.git_bytes\n"),
            "other": ("import os, subprocess\nfrom . import git as g\nfrom .git import git_process\n"
                      "subprocess.Popen(('git', 'status'))\nos.execvp('git', ['git', 'status'])\n"),
            "operations": "def harness_identity(root):\n    return _git_query(root, 'status', effect=READ)\n",
        }
        problems, checked = call_shape_problems(sources)
        self.assertEqual(problems, [
            ("harness/git.py:3", "forwards an effect other than effect"),
            ("harness/git.py:5", "forwards something other than its repository and args"),
            ("harness/git.py:7", "declares no effect constant"),
            ("harness/git.py:9", "starts a process outside _launch"),
            ("harness/memory.py:3", "runs show without --no-ext-diff"),
            ("harness/memory.py:3", "runs show without --no-textconv"),
            ("harness/memory.py:4", "has no literal subcommand first"),
            ("harness/memory.py:5", "runs update-ref without --no-deref"),
            ("harness/memory.py:6", "declares no effect constant"),
            ("harness/memory.py:7", "runs diff without --no-textconv"),
            ("harness/memory.py:8", "uses a Git runner other than in a call"),
            ("harness/other.py:2", "imports a Git runner under another name"),
            ("harness/other.py:3", "imports a Git runner under another name"),
            ("harness/other.py:4", "starts git directly"),
            ("harness/other.py:5", "starts git directly"),
            ("harness/other.py:5", "starts git directly"),
            ("harness/operations.py:2", "declares no effect constant"),
        ])
        self.assertEqual(checked, {"git": 3, "memory": 5, "operations": 1})

    def test_only_the_exact_forwards_of_the_launch_layer_are_exempt(self):
        forwards = {
            "git": ("import os\n"
                    "def _run_git(repo, args, binding=None, *, effect=None):\n"
                    "    return invocation(repo, args, effect=effect, deadline=0, binding=binding)\n"
                    "def git_process(repo, *args, effect=None, binding=None):\n"
                    "    return invocation(repo, args, effect=effect, deadline=0, binding=binding)\n"
                    "def git_bytes(repo, *args, effect=None, binding=None):\n"
                    "    return git_process(repo, *args, effect=effect, binding=binding)\n"
                    "def git(repo, *args, effect=None, binding=None):\n"
                    "    return os.fsdecode(git_bytes(repo, *args, effect=effect, binding=binding))\n"),
            "operations": ("from . import git\ndef _git_query(root, *args, effect):\n"
                           "    return git.invocation(root, args, effect=effect, deadline=0)\n"),
            "git_query": ("from . import git\ndef run_git_query(query, worktree):\n    worktree = Path(worktree)\n"
                          "    args = _git_args(query)\n"
                          "    return git.invocation(worktree, args, effect=git.READ, deadline=0, literal_pathspecs=True)\n"),
        }
        self.assertEqual(call_shape_problems(forwards), ([], {"git": 4, "operations": 1, "git_query": 1}))
        args, effect = "forwards something other than its repository and args", "forwards an effect other than effect"
        breaches = {
            "args not starred": ("git", "def git(repo, *args, effect=None):\n    return git_bytes(repo, args, effect=effect)\n",
                                 [(2, args)]),
            "a subcommand spread in with the repository": (
                "git", "def git(repo, *args, effect=None):\n    return git_bytes(*(repo, 'status'), *args, effect=effect)\n",
                [(2, args)]),
            "the effect captured by a match": (
                "git", "def git(repo, *args, effect=None):\n    match None:\n        case effect:\n"
                       "            return git_bytes(repo, *args, effect=effect)\n", [(4, effect)]),
            "another parameter as the repository": (
                "git", "def git(repo, *args, effect=None, where=None):\n    return git_bytes(where, *args, effect=effect)\n",
                [(2, args)]),
            "args spread twice": ("git", "def git(repo, *args, effect=None):\n"
                                         "    return git_bytes(repo, *args, *args, effect=effect)\n", [(2, args)]),
            "query repository other than the worktree": (
                "git_query", "from . import git\ndef run_git_query(query, worktree, binding):\n    args = _git_args(query)\n"
                             "    return git.invocation(binding['worktree'], args, effect=git.READ, deadline=0)\n",
                [(4, args)]),
            "an argument added": ("git", "def git_bytes(repo, *args, effect=None):\n"
                                         "    return git_process(repo, *args, '--patch', effect=effect)\n", [(2, args)]),
            "args rebound": ("git", "def _run_git(repo, args, *, effect=None):\n    args = ('status', *args)\n"
                                    "    return invocation(repo, args, effect=effect, deadline=0)\n", [(3, args)]),
            "args put in a list": ("operations", "from . import git\ndef _git_query(root, *args, effect):\n"
                                                 "    return git.invocation(root, ['status', *args], effect=effect, deadline=0)\n",
                                   [(3, args)]),
            "no effect parameter": ("git", "def git(repo, *args):\n    effect = None\n"
                                           "    return git_bytes(repo, *args, effect=effect)\n", [(3, effect)]),
            "effect rebound": ("git", "def git_process(repo, *args, effect=None):\n    effect = effect or CHECKOUT\n"
                                      "    return invocation(repo, args, effect=effect, deadline=0)\n", [(3, effect)]),
            "a nested function of the same name": (
                "git", "def other(repo, effect):\n    def git(*args):\n        return git_bytes(repo, *args, effect=effect)\n",
                [(3, "declares no effect constant"), (3, "has no literal subcommand first")]),
            "query args not from the parser": (
                "git_query", "from . import git\ndef run_git_query(query, worktree):\n    args = ['diff', 'HEAD']\n"
                             "    return git.invocation(worktree, args, effect=git.COMPARE, deadline=0)\n",
                [(4, args), (4, "forwards an effect other than READ")]),
            "an item of args augmented": ("git", "def _run_git(repo, args, *, effect=None):\n    args[0] += '-tree'\n"
                                                 "    return invocation(repo, args, effect=effect, deadline=0)\n",
                                          [(3, args)]),
            # A parameter that gathers the caller's other keywords or positions is not the one forwarded.
            "the effect gathered from other keywords": (
                "git", "def git(repo, *args, **effect):\n    return git_bytes(repo, *args, effect=effect)\n", [(2, effect)]),
            "the effect gathered from other positions": (
                "git", "def git_bytes(repo, args, *effect):\n    return git_process(repo, *args, effect=effect)\n",
                [(2, effect)]),
            "args gathered from keywords and spread": (
                "git", "def git(repo, *, effect=None, **args):\n    return git_bytes(repo, *args, effect=effect)\n",
                [(2, args)]),
            "args gathered from keywords": (
                "git", "def git_process(repo, *, effect=None, **args):\n"
                       "    return invocation(repo, args, effect=effect, deadline=0)\n", [(2, args)]),
            "query args also a parameter gathering keywords": (
                "git_query", "from . import git\ndef run_git_query(query, worktree, **args):\n    args = _git_args(query)\n"
                             "    return git.invocation(worktree, args, effect=git.READ, deadline=0)\n", [(4, args)]),
        }
        for gathered in ("*worktree", "**worktree"):
            breaches[f"the query worktree gathered as {gathered}"] = (
                "git_query", f"from . import git\ndef run_git_query(query, {gathered}):\n    args = _git_args(query)\n"
                             "    return git.invocation(worktree, args, effect=git.READ, deadline=0)\n", [(4, args)])
        for change in ("args.insert(0, 'status')", "args[0] = 'status'", "del args[0]"):  # _git_args gives a list
            breaches[f"query args changed in place: {change}"] = (
                "git_query", "from . import git\ndef run_git_query(query, worktree):\n    args = _git_args(query)\n"
                             f"    {change}\n    return git.invocation(worktree, args, effect=git.READ, deadline=0)\n",
                [(5, args)])
        for name, (module, source, expected) in breaches.items():
            with self.subTest(name):
                self.assertEqual(call_shape_problems({module: source})[0],
                                 [(f"harness/{module}.py:{line}", what) for line, what in expected])

    def test_a_forward_that_binds_its_repository_args_or_effect_again_in_any_form_is_not_exempt(self):
        statements = {
            "assignment": "NAME = None", "augmented assignment": "NAME += ()",
            "annotated assignment": "NAME: object = None", "for": "for NAME in ():\n        pass",
            "with": "with open(repo) as NAME:\n        pass",
            "except": "try:\n        pass\n    except OSError as NAME:\n        pass",
            "import": "import NAME", "import of a dotted name": "import NAME.sub", "from import": "from os import sep as NAME",
            "walrus": "(NAME := None)", "match capture": "match None:\n        case NAME:\n            pass",
            "match as": "match None:\n        case None as NAME:\n            pass",
            "match star": "match ():\n        case [*NAME]:\n            pass",
            "match mapping rest": "match {}:\n        case {**NAME}:\n            pass", "del": "del NAME",
            "def": "def NAME():\n        pass", "class": "class NAME:\n        pass",
            "nonlocal in a nested function": "def reset():\n        nonlocal NAME\n        NAME = None\n    reset()",
            "global in a nested function": "def reset():\n        global NAME\n    reset()",
        }
        untouched = "def git(repo, *args, effect=None):\n    pass\n    return git_bytes(repo, *args, effect=effect)\n"
        self.assertEqual(call_shape_problems({"git": untouched}), ([], {"git": 1}))
        for name, what in (("repo", "forwards something other than its repository and args"),
                           ("args", "forwards something other than its repository and args"),
                           ("effect", "forwards an effect other than effect")):
            for form, statement in statements.items():
                with self.subTest(name, form=form):
                    source = untouched.replace("pass", statement.replace("NAME", name))
                    line = 3 + statement.count("\n")  # the forward follows the statement
                    self.assertEqual(call_shape_problems({"git": source})[0], [(f"harness/git.py:{line}", what)])

    def test_only_range_diff_passes_the_fixed_settings_and_only_two_forwards_pass_them_on(self):
        diff_tree = "'diff-tree', '--no-ext-diff', '--no-textconv', base"
        allowed = ("def git_process(repo, *args, effect=None, _config=()):\n"
                   "    return invocation(repo, args, effect=effect, deadline=0, _config=_config)\n"
                   "def git_bytes(repo, *args, effect=None, _config=()):\n"
                   "    return git_process(repo, *args, effect=effect, _config=_config)\n"
                   "def _range_diff(repo, base):\n"
                   f"    return git_bytes(repo, {diff_tree}, effect=READ, _config=_RANGE_DIFF_CONFIG)\n")
        self.assertEqual(call_shape_problems({"git": allowed}), ([], {"git": 3}))
        outside, elsewhere = "passes _config outside harness.git", "passes _config outside _range_diff"
        other, forward, spread = ("passes a _config other than _RANGE_DIFF_CONFIG", "forwards a _config other than its own",
                                  "passes keywords through **")
        breaches = {
            "any setting from another module": (
                "memory", "from . import git\n"
                          "git.git_process(repo, 'commit', effect=git.INDEX_REFS, _config=('core.hooksPath=/tmp/hooks',))\n",
                [(2, outside)]),
            "the constant itself from another module": (
                "finalization", f"from . import git\ngit.git_bytes(repo, {diff_tree}, effect=git.READ, "
                                "_config=git._RANGE_DIFF_CONFIG)\n", [(2, outside)]),
            "a construction from another module": (
                "other", "from . import git\ngit.invocation(repo, ('diff-tree', '--no-ext-diff', '--no-textconv'), "
                         "effect=git.READ, deadline=60, _config=('core.quotePath=false',))\n", [(2, outside)]),
            "keywords through ** from another module": (
                "other", "from . import git\ngit.git_process(repo, 'status', effect=git.READ, **options)\n", [(2, spread)]),
            "keywords through ** in a forward": (
                "git", "def git_bytes(repo, *args, effect=None, **options):\n"
                       "    return git_process(repo, *args, effect=effect, **options)\n", [(2, spread)]),
            "another function of harness.git": (
                "git", "def clean(repo):\n    return git(repo, 'status', effect=COMPARE, _config=_RANGE_DIFF_CONFIG)\n",
                [(2, elsewhere)]),
            "a forward that does not lead to _range_diff": (
                "git", "def git(repo, *args, effect=None, _config=()):\n"
                       "    return git_bytes(repo, *args, effect=effect, _config=_config)\n", [(2, elsewhere)]),
            "a function nested in _range_diff": (
                "git", "def _range_diff(repo, base):\n"
                       f"    run = lambda: git_bytes(repo, {diff_tree}, effect=READ, _config=_RANGE_DIFF_CONFIG)\n"
                       "    return run()\n", [(2, elsewhere)]),
            "_range_diff with settings of its own": (
                "git", f"def _range_diff(repo, base):\n    return git_bytes(repo, {diff_tree}, effect=READ, "
                       "_config=('core.quotePath=false',))\n", [(2, other)]),
            "_range_diff binding the constant again": (
                "git", "def _range_diff(repo, base):\n    _RANGE_DIFF_CONFIG = ('core.hooksPath=/tmp/hooks',)\n"
                       f"    return git_bytes(repo, {diff_tree}, effect=READ, _config=_RANGE_DIFF_CONFIG)\n", [(3, other)]),
            "a forward passing the constant": (
                "git", "def git_bytes(repo, *args, effect=None, _config=()):\n"
                       "    return git_process(repo, *args, effect=effect, _config=_RANGE_DIFF_CONFIG)\n", [(2, forward)]),
            "a forward without the parameter": (
                "git", "def git_process(repo, *args, effect=None):\n"
                       "    return invocation(repo, args, effect=effect, deadline=0, _config=settings)\n", [(2, forward)]),
            "a forward adding to it": (
                "git", "def git_process(repo, *args, effect=None, _config=()):\n"
                       "    _config = (*_config, 'core.hooksPath=/tmp/hooks')\n"
                       "    return invocation(repo, args, effect=effect, deadline=0, _config=_config)\n", [(3, forward)]),
            "a forward gathering other keywords under the parameter's name": (
                "git", "def git_process(repo, *args, effect=None, **_config):\n"
                       "    return invocation(repo, args, effect=effect, deadline=0, _config=_config)\n", [(2, forward)]),
            "a forward gathering other positions under the parameter's name": (
                "git", "def git_process(repo, args, *_config, effect=None):\n"
                       "    return invocation(repo, args, effect=effect, deadline=0, _config=_config)\n", [(2, forward)]),
        }
        for kind, parameter in (("positional-only", "_RANGE_DIFF_CONFIG, /"), ("ordinary", "_RANGE_DIFF_CONFIG=()"),
                                ("*args", "*_RANGE_DIFF_CONFIG"), ("keyword-only", "*, _RANGE_DIFF_CONFIG=()"),
                                ("**kwargs", "**_RANGE_DIFF_CONFIG")):
            breaches[f"_range_diff with a {kind} parameter of the constant's name"] = (
                "git", f"def _range_diff(repo, base, {parameter}):\n"
                       f"    return git_bytes(repo, {diff_tree}, effect=READ, _config=_RANGE_DIFF_CONFIG)\n", [(2, other)])
        for name, (module, source, expected) in breaches.items():
            with self.subTest(name):
                self.assertEqual(call_shape_problems({module: source})[0],
                                 [(f"harness/{module}.py:{line}", what) for line, what in expected])

    def test_runners_are_found_under_relative_and_absolute_names(self):
        sources = {
            "a": "from . import operations\noperations._git_query(root, 'status')\n",
            "b": "from harness import git, operations\noperations._git_query(root, '--porcelain', effect=git.COMPARE)\n",
            "c": "from harness.git import git as run\nrun(root, 'status')\n",
            "d": "from harness.git import git_process\nsubmit(git_process)\n",
            "e": "import harness.git as hg\nhg.git_bytes(root, 'diff', effect=hg.COMPARE)\n",
            "f": "import harness.git\nharness.git.git(root, 'status')\n",
            "g": "from .operations import _git_query as query\nquery(root, 'status', effect=READ)\n",
            "h": "from subprocess import run\nrun(['git', 'status'])\n",
            "i": "from harness import git\ngit.git(root, 'status', effect=git.READ)\n",  # the plain form
        }
        problems, checked = call_shape_problems(sources)
        self.assertEqual(problems, [
            ("harness/a.py:2", "declares no effect constant"),
            ("harness/b.py:2", "has no literal subcommand first"),
            ("harness/c.py:1", "imports a Git runner under another name"),
            ("harness/c.py:2", "declares no effect constant"),
            ("harness/d.py:1", "imports a Git runner under another name"),
            ("harness/d.py:2", "uses a Git runner other than in a call"),
            ("harness/e.py:1", "imports a Git runner under another name"),
            ("harness/e.py:2", "runs diff without --no-ext-diff"),
            ("harness/e.py:2", "runs diff without --no-textconv"),
            ("harness/f.py:2", "declares no effect constant"),
            ("harness/g.py:1", "imports a Git runner under another name"),
            ("harness/g.py:2", "declares no effect constant"),
            ("harness/h.py:2", "starts git directly"),
        ])
        self.assertEqual(checked, dict.fromkeys("abcefgi", 1))


if __name__ == "__main__":
    unittest.main()
