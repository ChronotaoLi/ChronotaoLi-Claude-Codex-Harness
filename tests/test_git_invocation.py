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
import fcntl
import hashlib
import http.client
import io
import json
import os
import random
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import git_isolation
import run_shards
import stop_tripwire
from acceptance_support import accept_now, acceptance_checkout
from harness import (cleanup, cli, command_server, dashboard, finalization, git, git_query, interrupts, memory, operations,
                     permission_smoke, private_state, stops, tool_smoke, workspace_inputs)
from harness.config import Config
from harness.util import HarnessError, read_json
from test_decide import Script, block_first, blocked_by_task, describe, revise, task
from test_harness import ROOT, RepoCase, cli_notice, write_project_config

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
        # Protocol v2 greeting followed by an empty list of filter capabilities.
        self.process = self.script("process.sh", record.replace('"$1"', "process")
                                   + "printf '0016git-filter-server\\n000eversion=2\\n00000000'\ncat >/dev/null\n")
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

    def configured(self, scope, events=EVENTS):
        return "".join(f'[hook "{scope}-{event}"]\n\tcommand = \'{self.hook}\' config:{scope}-{event}\n'
                       f"\tevent = {event}\n" for event in events)

    def arm(self, repo, *, events=EVENTS, programs=True, scopes=("repo", "glob"), hook_directory=True):
        common = self.common(repo)
        (common / "hooks").mkdir(exist_ok=True)
        for event in events if hook_directory else ():
            hook = common / "hooks" / event
            hook.write_text(f"#!/bin/sh\nexec '{self.hook}' hookdir:{event} \"$@\"\n")
            hook.chmod(0o755)

        text = self.configured("repo", events) if "repo" in scopes else ""
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
            Path(os.environ["GIT_CONFIG_GLOBAL"]).write_text(self.configured("glob", events))
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

    def cli(self, *argv, serve=None):
        """The real CLI with a bounded dashboard service interval."""
        def stop_at_once(server, poll_interval=0.5):
            raise KeyboardInterrupt()
        output, errors = io.StringIO(), io.StringIO()
        previous = signal.getsignal(signal.SIGTERM)
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                    patch.object(dashboard.DashboardServer, "serve_forever", serve or stop_at_once):
                code = cli.main(["--repo", str(self.repo), "--config", str(ROOT / "harness.toml"), *argv])
        finally:
            signal.signal(signal.SIGTERM, previous)
        return code, output.getvalue(), errors.getvalue()

    def files(self, *tops, skip=()):
        """Fingerprint each entry without following symlinks or opening special files."""
        state = {}

        def visit(path, *, root=False):
            try:
                info = path.lstat()
            except FileNotFoundError:
                if root:
                    return
                raise
            mode = info.st_mode
            mtime = info.st_mtime_ns
            payload = None
            if stat.S_ISLNK(mode):
                payload = os.readlink(os.fsencode(path))
            elif stat.S_ISREG(mode):
                payload = hashlib.sha256(path.read_bytes()).hexdigest()
                if ".git" in path.parts and path.name.startswith("sharedindex."):
                    mtime = None
            elif stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
                payload = info.st_rdev
            state[str(path)] = (mode, info.st_uid, info.st_gid, mtime, payload)
            if stat.S_ISDIR(mode):
                for child in path.iterdir():
                    if not root or child.name not in skip:
                        visit(child)

        for top in tops:
            visit(Path(top), root=True)
        return state

    def assert_no_fsmonitor_daemon_spawn(self, trace, command):
        lines = trace.read_text().splitlines()
        self.assertTrue(lines, f"No Git process wrote a trace for {command}")
        spawns = [line for line in lines if "trace: run_command: git fsmonitor--daemon " in line]
        self.assertEqual(spawns, [], f"{command} spawned fsmonitor--daemon")


class PreviewAndObservationTests(IsolatedRepoCase):
    """Case 2: a marked repository, an offline run, preview, observation and removal."""

    def setUp(self):
        super().setUp()
        self.config.data["verification"]["fresh_checkout"] = True

    def test_preview_and_observation_run_no_marked_program_and_change_no_git_bytes(self):
        markers = Markers(self.scratch)
        markers.arm(self.repo)
        real_clean_checkout = cleanup.clean_checkout
        refused = []

        def runtime_reclaim_fails_once(*args, **kwargs):
            if kwargs.get("by") == "runtime" and not refused:
                refused.append(True)
                raise cleanup.CheckoutRemovalIncomplete("simulated reclamation failure")
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
        for tree, binding in ((worktree, result["worktree_binding"]), (checkout, result["verification_checkouts"][0]["binding"])):
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
        before = self.files(self.repo, worktree, checkout)
        dispatch = next(entry["id"] for entry in result["dispatches"] if entry["role"] == "worker")
        invocations, request_probes = [], []
        construct = git.invocation
        route = dashboard.DashboardHandler._route

        def record(repo, args, **kwargs):
            invocations.append((tuple(args), kwargs["effect"], git._read_only_depth()))
            return construct(repo, args, **kwargs)

        def probe_route(handler):
            try:
                git.clean(self.repo)
            except HarnessError as error:
                request_probes.append((threading.current_thread(), stops.of(error)))
            return route(handler)

        def serve_one_request(server, poll_interval=0.5):
            host, port = server.server_address
            client = http.client.HTTPConnection(host, port, timeout=5)
            try:
                client.request("GET", f"/api/runs/{run_id}")
                server.timeout = 5
                server.handle_request()
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response)["id"], run_id)
            finally:
                client.close()
            raise KeyboardInterrupt()

        with patch.object(git, "invocation", side_effect=record), \
                patch.object(dashboard.DashboardHandler, "_route", probe_route):
            for argv in (("cleanup-run", run_id), ("cleanup-run", run_id, "--remove-input-copies"),
                         ("cleanup-run", run_id, "--remove-ignored", "--remove-input-copies"),
                         ("status",), ("status", run_id), ("status", run_id, "--full"), ("watch", run_id),
                         ("dashboard", "--port", "0"), ("permission-diagnostics", run_id, dispatch)):
                with self.subTest(argv=argv):
                    invocations.clear()
                    code, output, errors = self.cli(*argv, serve=serve_one_request)
                    self.assertNotIn("Traceback", errors)
                    self.assertEqual(invocations[0], (("rev-parse", "--show-toplevel"), git.READ, 1))
                    self.assertTrue(all(effect is git.READ and depth >= 1 for _, effect, depth in invocations))
                    self.assertEqual(git._read_only_depth(), 0)
                    if argv[0] == "cleanup-run":
                        self.assertEqual(invocations[1], (("rev-parse", "--show-toplevel"), git.READ, 2))
                        preview = json.loads(output)
                        if "--remove-ignored" in argv:
                            self.assertEqual(code, 0, errors)
                            self.assertEqual(preview["worktree"]["status"], "eligible")
                            self.assertIn("__pycache__/", preview["worktree"]["ignored_paths"]["entries"])
                        else:
                            self.assertEqual(code, 1, errors)
                            self.assertEqual(preview, unmarked)
                    else:
                        self.assertEqual(code, 0, errors)
            invocations.clear()
            preview = cleanup.cleanup_run(self.repo, run_id)
            self.assertEqual(preview, unmarked)
            self.assertEqual(invocations[0], (("rev-parse", "--show-toplevel"), git.READ, 1))
            self.assertTrue(all(effect is git.READ and depth == 1 for _, effect, depth in invocations))

        (thread, stop), = request_probes
        self.assertIsNot(thread, threading.current_thread())
        self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "invariant"))
        self.assertEqual(markers.read(), [])
        self.assertEqual(self.files(self.repo, worktree, checkout), before)
        invocations.clear()
        with patch.object(git, "invocation", side_effect=record):
            code, output, errors = self.cli("cleanup-run", run_id, "--remove-ignored", "--confirm-exited", dispatch)
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["worktree"]["status"], "eligible")
        self.assertTrue(invocations)
        self.assertTrue(all(effect is git.READ and depth == 0 for _, effect, depth in invocations))
        self.assertTrue(next(entry["exit_confirmed"] for entry in read_json(engine.directory / "state.json")["dispatches"]
                             if entry["id"] == dispatch))
        self.assertTrue(checkout.is_dir())
        self.assertEqual(markers.read(), [])
        # Removal passes the real preview gates; Git's own status inside worktree remove may run the clean filter.
        expire_stats(checkout)
        markers.reset()
        invocations.clear()
        with patch.object(git, "invocation", side_effect=record):
            code, output, errors = self.cli("cleanup-run", run_id, "--remove")
        self.assertTrue(invocations)
        self.assertTrue(all(depth == 0 for _, _, depth in invocations))
        self.assertEqual(code, 1, errors)  # the run worktree still has ignored entries
        removal = json.loads(output)
        self.assertEqual([item["status"] for item in removal["checkouts"]], ["removed"], errors)
        self.assertEqual(removal["worktree"]["status"], "blocked", removal["worktree"])
        self.assertIn("__pycache__/", removal["worktree"]["error"])
        self.assertTrue(worktree.is_dir())
        self.assertFalse(checkout.exists())
        self.assertEqual(read_json(artifact.with_name("cleanup.json"))["status"], "removed")
        self.assertEqual(markers.kinds(), {"clean"}, markers.read())
        self.assertEqual({where for _, where in markers.read()}, {str(checkout.resolve())})

    def test_cli_reports_typed_stop_when_the_first_repository_call_would_compare(self):
        def compare(repo):
            git.clean(repo)
            self.fail("the first repository lookup allowed a comparison")

        for argv in (("status",), ("watch", "run_fixture"), ("dashboard", "--port", "0"),
                     ("permission-diagnostics", "run_fixture", "dispatch_fixture"),
                     ("cleanup-run", "run_fixture", "--remove-ignored", "--remove-input-copies")):
            with self.subTest(argv=argv), patch.object(git, "repository", side_effect=compare), \
                    patch.object(git.subprocess, "Popen", wraps=subprocess.Popen) as processes:
                code, output, errors = self.cli(*argv)
                stop = cli_notice(errors)
                self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "invariant"), errors)
                self.assertEqual(code, 1, errors)
                self.assertEqual(output, "")
                processes.assert_not_called()
        self.assertEqual(git._read_only_depth(), 0)


    def test_watch_with_explicit_state_and_no_repository_lookup_still_enters_scope(self):
        def compare(state, run_id, *, follow):
            self.assertEqual(state, self.repo / ".harness")
            self.assertEqual(run_id, "run_fixture")
            self.assertFalse(follow)
            git.clean(self.repo)
            self.fail("watch with explicit state allowed a comparison")

        with patch("harness.watch_cli.watch_run", side_effect=compare), \
                patch.object(git.subprocess, "Popen", wraps=subprocess.Popen) as processes:
            code, output, errors = self.cli("watch", "run_fixture", "--state", str(self.repo / ".harness"))
        stop = cli_notice(errors)
        self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "invariant"), errors)
        self.assertEqual((code, output), (1, ""))
        processes.assert_not_called()
        self.assertEqual(git._read_only_depth(), 0)


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
        real, started, self.finished = git._launch, [], []

        def recording(argv, env, **kwargs):
            started.append(list(argv))
            if fail is not None and "--get-regexp" in argv:
                raise fail
            self.finished.append(real(argv, env, **kwargs))
            return self.finished[-1]
        with patch.object(git, "_launch", side_effect=recording):
            yield started

    def assert_invariant(self, call):
        with self.launches() as started, self.assertRaises(HarnessError) as raised:
            call()
        self.assertEqual(stops.of(raised.exception)["kind"], "runtime_failure")
        self.assertEqual(stops.of(raised.exception)["reason"], "invariant")
        self.assertEqual(started, [])

    def test_read_only_rejects_before_binding_checks_or_hook_reads_and_recovers_from_exceptions(self):
        markers = Markers(self.root)
        markers.arm(self.repo)
        expire_stats(self.repo / "x")
        linked = self.root / "linked"
        binding = git.add_worktree(self.repo, linked, "HEAD")
        markers.reset()
        before = (self.repo / ".git" / "index").read_bytes()
        with git.read_only():
            with self.assertRaisesRegex(ValueError, "nested scope"):
                with git.read_only():
                    self.assert_invariant(lambda: git.clean(self.repo))
                    self.assert_invariant(lambda: git.clean(linked, binding=binding))
                    self.assert_invariant(lambda: git.git(self.repo, "rev-parse", "HEAD"))
                    for effect in (git.COMPARE, git.PRIVATE, git.INDEX, git.REFS, git.INDEX_REFS, git.CHECKOUT, None):
                        with self.subTest(effect=effect):
                            self.assert_invariant(lambda: git.invocation(
                                self.repo, ("rev-parse", "HEAD"), effect=effect, deadline=time.monotonic() + 5))
                    raise ValueError("nested scope")
            self.assert_invariant(lambda: git.clean(self.repo))
            with self.launches() as started:
                self.assertEqual(git.git(self.repo, "ls-files", effect=git.READ), "x\n")
            self.assertEqual(len(started), 1)
            self.assertEqual(markers.read(), [])
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "outer scope"):
            with git.read_only():
                raise ValueError("outer scope")

        with self.launches() as started:
            self.assertTrue(git.clean(self.repo))
        self.assertEqual(len(started), 2)
        self.assertTrue(markers.read())
        self.assertEqual(markers.kinds(), {"clean"})
        with self.launches() as started:
            self.assertTrue(git.git(self.repo, "rev-parse", "HEAD").strip())
        self.assertEqual(len(started), 1)

    def test_read_only_interruptions_release_only_the_callers_layer(self):
        acquire, close, repository = git.ReadOnlyScope.acquire, git.ReadOnlyScope.close, git.repository

        def cli_status():
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["--repo", str(self.repo), "status"]), 130)

        def direct_cleanup():
            with self.assertRaises(KeyboardInterrupt):
                cleanup.cleanup_run(self.repo, "run_missing")

        def direct_scope():
            with self.assertRaises(KeyboardInterrupt), git.read_only():
                git.repository(self.repo)

        for entry in (cli_status, direct_cleanup, direct_scope):
            for point in ("before acquisition", "after acquisition", "in body", "on close"):
                with self.subTest(entry=entry.__name__, point=point):
                    ready, leave = threading.Event(), threading.Event()
                    interrupted = threading.Event()

                    def hold():
                        with git.read_only():
                            ready.set()
                            leave.wait(10)

                    owner = threading.Thread(target=hold)
                    owner.start()
                    try:
                        self.assertTrue(ready.wait(10))

                        def interrupt_here():
                            if not interrupted.is_set():
                                interrupted.set()
                                raise KeyboardInterrupt()

                        def take(scope):
                            if point == "before acquisition":
                                interrupt_here()
                            acquire(scope)
                            if point == "after acquisition":
                                interrupt_here()

                        def use(repo):
                            located = repository(repo)
                            if point == "in body":
                                self.assert_invariant(lambda: git.clean(repo))
                                interrupt_here()
                            return located

                        def release(scope):
                            if point == "on close":
                                interrupt_here()
                            close(scope)

                        with patch.object(git.ReadOnlyScope, "acquire", take), \
                                patch.object(git, "repository", use), \
                                patch.object(git.ReadOnlyScope, "close", release):
                            entry()
                        self.assertTrue(interrupted.is_set())
                        self.assert_invariant(lambda: git.clean(self.repo))
                    finally:
                        leave.set()
                        owner.join(10)
                    self.assertFalse(owner.is_alive())
                    with self.launches() as started:
                        self.assertTrue(git.clean(self.repo))
                    self.assertEqual(len(started), 2)

    def test_read_only_context_can_be_released_after_interrupted_protocol_handoff(self):
        for method, event in (("__enter__", "return"), ("__exit__", "call")):
            with self.subTest(method=method):
                ctx = git.read_only()
                code = getattr(type(ctx), method).__code__
                previous_trace = sys.gettrace()

                def trace(frame, actual_event, arg):
                    if frame.f_code is code and actual_event == event and frame.f_locals["self"] is ctx:
                        raise KeyboardInterrupt
                    return trace

                try:
                    with self.assertRaises(KeyboardInterrupt):
                        sys.settrace(trace)
                        try:
                            with ctx:
                                pass
                        finally:
                            sys.settrace(previous_trace)
                    with self.launches() as started:
                        self.assertEqual(git.git(self.repo, "ls-files", effect=git.READ), "x\n")
                    self.assertEqual(len(started), 1)
                    self.assert_invariant(lambda: git.clean(self.repo))
                finally:
                    with git.read_only():
                        ctx.__exit__(None, None, None)
                        ctx.__exit__(None, None, None)
                        self.assert_invariant(lambda: git.clean(self.repo))
                with self.launches() as started:
                    self.assertTrue(git.clean(self.repo))
                self.assertEqual(len(started), 2)

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
                                                                  result["expected_head"],
                                                                  binding=result["worktree_binding"]))

    def test_working_tree_and_range_diffs_are_the_bytes_of_an_uncoloured_repository(self):
        base = git.head(self.repo)
        (self.repo / "test_hello.py").write_text("print('changed')\n")
        plain_git(self.repo, *IDENTITY, "commit", "-qam", "second")
        (self.repo / "test_hello.py").write_text("print('changed again')\n")
        (self.repo / "new.txt").write_text("new\n")
        self.colour()
        self.assertIn(b"\x1b[", plain_git(self.repo, "diff", "HEAD").stdout)  # the configuration is in effect
        tip = git.head(self.repo)
        coloured = git.diff_bytes(self.repo), git.range_diff_bytes(self.repo, base, tip)
        self.uncolour()
        self.assertEqual(coloured, (git.diff_bytes(self.repo), git.range_diff_bytes(self.repo, base, tip)))
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
            porcelain = plain_git(repo, "diff", *self.PATCH, base, head, "--").stdout
            with self.subTest(base=base, head=head):
                self.assertEqual(git.range_diff_bytes(repo, base, head), porcelain)
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
        tip = git.head(worktree, binding=binding)
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
        self.assertEqual(git.range_diff_bytes(worktree, base, tip, binding=binding), patch_bytes)
        # A real index Git cannot read: porcelain diff between two commits never read it, nor do these.
        (Path(binding["git_dir"]) / "index").write_bytes(b"not an index")
        self.assertEqual(git.range_diff_bytes(worktree, base, tip, binding=binding), patch_bytes)
        self.assertEqual(os.fsencode(git.range_paths(worktree, base, tip, binding=binding)), names)

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
        tip = git.head(worktree, binding=binding)

        def porcelain():
            return (plain_git(worktree, "diff", *self.PATCH, base, "HEAD", "--").stdout,
                    plain_git(worktree, "diff", "--name-only", "--no-renames", "-z", base, "HEAD").stdout)

        def whole_run():
            return (git.range_diff_bytes(worktree, base, tip, binding=binding),
                    os.fsencode(git.range_paths(worktree, base, tip, binding=binding)))
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
        tip = git.head(worktree, binding=binding)
        return base, worktree, binding, lambda: (git.range_diff_bytes(worktree, base, tip, binding=binding),
                                                 git.range_paths(worktree, base, tip, binding=binding))

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
        tip = git.head(worktree, binding=binding)
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
                        compare(worktree, base, tip, binding=binding)
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
        engine = self.engine()
        try:
            engine.start("Implement greet")
        except HarnessError as caught:
            error = caught
        else:
            self.fail("the run started without HEAD's blobs")
        self.assertTrue(self.missing(clone, blob))
        self.assertIn("git reset", str(error))
        stop = stops.of(error)
        self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "io"))
        saved = read_json(engine.directory / "state.json")
        self.assertEqual((saved["status"], saved["stop"], saved["dispatches"]), ("blocked", stop, []))
        self.assertFalse(Path(saved["worktree"]).exists())
        self.assertNotIn(f"worktree {saved['worktree']}\n", git.git(clone, "worktree", "list", "--porcelain"))
        self.assertEqual(git.git(clone, "branch", "--list", saved["branch"]).strip(), saved["branch"])

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
            worktree = self.root / "acceptance"
            binding = acceptance_checkout(repo, worktree)
            repo = worktree
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
            self.assertNotEqual(accept_now(repo, ["a.txt"], "run_isolation", "task", {"subject": "Accept", "body": ""}, binding=binding),
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
        worktree = root / "acceptance"
        binding = acceptance_checkout(repo, worktree)
        repo = worktree
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
            accepted = accept_now(repo, ["a.txt"], "run_hook", "task", {"subject": "Accept", "body": ""}, binding=binding)
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

    def test_smoke_fixture_binding_reads_use_the_same_isolated_config_as_checkout(self):
        with git_isolation.user_config("[broken\n"):
            for name, prepare in (("permission", permission_smoke.prepare_fixture),
                                  ("tool", tool_smoke.prepare_fixture)):
                with self.subTest(name):
                    directory = self.parent / name
                    prepare(directory)
                    repo, worktree = directory / "repo", directory / "worktree"
                    self.assertNotEqual(plain_git(repo, "config", "--list", check=False).returncode, 0)
                    head = git.git(repo, "rev-parse", "HEAD", effect=git.READ, isolated_config=True)
                    self.assertEqual(git.git(worktree, "rev-parse", "HEAD", effect=git.READ, isolated_config=True),
                                     head)

    def test_isolated_init_ignores_legacy_home_and_xdg_configuration(self):
        home, xdg = self.parent / "home", self.parent / "xdg"
        home.mkdir()
        (xdg / "git").mkdir(parents=True)
        for config in (home / ".gitconfig", xdg / "git" / "config"):
            config.write_text("[broken\n")
        for key, directory in (("HOME", home), ("XDG_CONFIG_HOME", xdg)):
            env = dict(os.environ)
            for selector in ("GIT_CONFIG_GLOBAL", "HOME", "XDG_CONFIG_HOME"):
                env.pop(selector, None)
            env[key] = str(directory)
            self.assertEqual(plain_git(self.parent, "config", "--list", env=env, check=False).returncode, 128)

        real = git._launch

        def legacy_isolated_launch(argv, env, **kwargs):
            # Git 2.31 ignores this selector.
            env = dict(env)
            env.pop("GIT_CONFIG_GLOBAL", None)
            return real(argv, env, **kwargs)

        with patch.dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(xdg)), \
                patch.object(git, "_launch", side_effect=legacy_isolated_launch):
            for name, prepare in (("permission", permission_smoke.prepare_fixture),
                                  ("tool", tool_smoke.prepare_fixture)):
                with self.subTest(name):
                    folder = self.parent / name
                    prepare(folder)
                    expected = git.git(folder / "repo", "rev-parse", "HEAD", effect=git.READ, isolated_config=True)
                    self.assertEqual(git.git(folder / "worktree", "rev-parse", "HEAD", effect=git.READ,
                                             isolated_config=True), expected)


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


class WorktreeCreationTests(IsolatedRepoCase):
    """Case 15c: populate only the newly bound tree, or account for both parts of its rollback."""

    def assert_registration(self, path, *, present):
        backlinks = (self.repo / ".git" / "worktrees").glob("*/gitdir")
        expected = os.fsencode(path.resolve() / ".git") + b"\n"
        self.assertEqual(expected in {backlink.read_bytes() for backlink in backlinks}, present)

    def test_shared_core_worktree_cannot_redirect_checkout_to_main(self):
        head = git.head(self.repo)
        tracked = self.repo / "test_hello.py"
        material = b"content unique to C\n"
        tracked.write_bytes(material)
        plain_git(self.repo, "add", "test_hello.py")
        plain_git(self.repo, *IDENTITY, "commit", "-qm", "C")
        commit = git.head(self.repo)
        plain_git(self.repo, "reset", "--hard", head)
        plain_git(self.repo, "config", "extensions.worktreeConfig", "true")
        plain_git(self.repo, "config", "core.worktree", str(self.repo.resolve()))
        index = self.repo / ".git" / "index"
        before = tracked.read_bytes(), index.read_bytes()

        checkout = self.scratch / "linked tree"
        binding = git.add_worktree(self.repo, checkout, commit)

        self.assertEqual((tracked.read_bytes(), index.read_bytes()), before)
        self.assertEqual(git.head(self.repo), head)
        self.assertEqual((checkout / tracked.name).read_bytes(), material)
        self.assertEqual(binding["worktree"], str(checkout.resolve()))
        self.assertIsNone(binding["branch"])
        self.assertEqual(binding["detached_head"], commit)
        self.assertEqual(git.head(checkout, binding=binding), commit)
        self.assertTrue(git.clean(checkout, binding=binding))

    def fail_smudge(self, command):
        (self.repo / ".git" / "info" / "attributes").write_text("*.txt filter=fixture\n")
        (self.repo / "fixture.txt").write_text("tracked content\n")
        plain_git(self.repo, "add", "fixture.txt")
        plain_git(self.repo, *IDENTITY, "commit", "-qm", "File requiring smudge")
        plain_git(self.repo, "config", "filter.fixture.clean", "cat")
        plain_git(self.repo, "config", "filter.fixture.smudge", command)
        plain_git(self.repo, "config", "filter.fixture.required", "true")

    def failed_run(self, *, fails_during_reset=True, expect_rollback_error=False):
        engine = self.engine()
        real_process = git.git_process
        results = []

        def process(repo, *args, **kwargs):
            result = real_process(repo, *args, **kwargs)
            if args == ("reset", "-q", "--hard", "--no-recurse-submodules"):
                results.append(result)
            return result

        with patch.object(git, "git_process", side_effect=process):
            try:
                engine.start("Implement greet")
            except HarnessError as failure:
                error = failure
            else:
                self.fail("the run started despite a failed checkout")

        message = str(error)
        state = read_json(engine.directory / "state.json")
        if fails_during_reset:
            result, = results
            self.assertNotEqual(result.returncode, 0)
            expected = "git reset -q --hard failed: " + result.stderr.decode("utf-8", errors="replace").strip()
            if expect_rollback_error:
                self.assertEqual(str(error.__cause__), expected)
                self.assertTrue(message.startswith(expected + "；"))
            else:
                self.assertEqual(message, expected)
            value = stops.of(error)
            self.assertEqual((value["kind"], value["reason"]), ("runtime_failure", "io"))
            self.assertEqual(state["stop"], value)
        else:
            self.assertEqual(results, [])
            finding, = stop_tripwire.consume(self)
            self.assertEqual((finding.channels, finding.kind, finding.reason, finding.facts["error_type"],
                              finding.facts["text"], finding.exempt),
                             ({"entry", "ledger"}, "unclassified", "untyped_harness_error", "HarnessError",
                              message[:stop_tripwire.TEXT_LIMIT], "consume"))
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["worktree_binding"], {})
        self.assertEqual(state["dispatches"], [])
        self.assertEqual(git.head(self.repo),
                         plain_git(self.repo, "rev-parse", state["branch"]).stdout.decode().strip())
        return Path(state["worktree"]), message

    def test_failed_smudge_removes_worktree_and_registration_but_keeps_branch(self):
        self.fail_smudge("false")
        control = self.scratch / "control"
        ordinary = plain_git(self.repo, "worktree", "add", "-b", "control", str(control), "HEAD", check=False)
        self.assertNotEqual(ordinary.returncode, 0)
        self.assertFalse(os.path.lexists(control))
        self.assert_registration(control, present=False)
        self.assertEqual(plain_git(self.repo, "rev-parse", "control").stdout.decode().strip(), git.head(self.repo))

        path, message = self.failed_run()
        self.assertIn("git reset -q --hard failed", message)
        self.assertIn("fixture.txt", message)
        self.assertNotIn("未能完全撤回", message)
        self.assertFalse(os.path.lexists(path))
        self.assert_registration(path, present=False)

    def test_rollback_with_a_native_relative_registration(self):
        volume = self.scratch.resolve() / "other disk"
        volume.mkdir()
        other = volume / "another\ncheckout"
        result = plain_git(self.repo, "worktree", "add", "--detach", "--relative-paths",
                           str(other), "HEAD", check=False)
        if result.returncode == 129 and b"unknown option" in result.stderr:
            self.skipTest("Git does not support native relative worktree registration")
        self.assertEqual(result.returncode, 0, result.stderr)

        registry = self.repo / ".git" / "worktrees"
        backlink, = registry.glob("*/gitdir")
        original = backlink.read_bytes()
        expected = os.path.relpath(other.resolve() / ".git", backlink.parent.resolve())
        self.assertEqual(original, os.fsencode(expected) + b"\n")
        self.assertEqual(plain_git(self.repo, "config", "extensions.relativeWorktrees").stdout, b"true\n")

        plain_git(self.repo, "config", "worktree.useRelativePaths", "true")
        self.fail_smudge("false")
        path = self.scratch / "failing\ncheckout"
        offline = self.scratch / "offline disk"
        real_launch = git._launch
        scenarios = [(False, False, False), (False, True, False), (True, True, False)]
        if os.geteuid() != 0:
            scenarios += [(False, False, True), (True, False, True)]
        for locked, missing, inaccessible in scenarios:
            if locked:
                plain_git(self.repo, "worktree", "lock", "--reason", "offline disk", str(other))
            if missing:
                volume.rename(offline)
            try:
                with self.subTest(locked=locked, missing=missing, inaccessible=inaccessible):
                    before = plain_git(self.repo, "worktree", "list", "--porcelain", "-z").stdout
                    self.assertIn(b"worktree " + os.fsencode(other), before.split(b"\0"))
                    self.assertEqual(b"locked offline disk" in before.split(b"\0"), locked)
                    self.assertEqual(b"\0prunable " in before, missing and not locked)

                    def launch(argv, env, **kwargs):
                        result = real_launch(argv, env, **kwargs)
                        if inaccessible and argv[-4:] == ["worktree", "list", "--porcelain", "-z"]:
                            self.assertEqual(result.stdout, before)
                            volume.chmod(0o600)
                            with self.assertRaises(PermissionError):
                                other.lstat()
                        return result

                    mode = volume.stat().st_mode if inaccessible else None
                    try:
                        with patch.object(git, "_launch", side_effect=launch), \
                                self.assertRaisesRegex(HarnessError, "git reset -q --hard failed") as error:
                            git.add_worktree(self.repo, path, "HEAD")
                    finally:
                        if mode is not None:
                            volume.chmod(mode)
                    self.assertFalse(os.path.lexists(path))
                    self.assertEqual(list(registry.glob("*/gitdir")), [backlink])
                    self.assertEqual(backlink.read_bytes(), original)
                    self.assertEqual(plain_git(self.repo, "worktree", "list", "--porcelain", "-z").stdout, before)
                    self.assertNotIn("未能完全撤回", str(error.exception))
            finally:
                if missing:
                    offline.rename(volume)
                if locked:
                    plain_git(self.repo, "worktree", "unlock", str(other))

    def test_failed_remove_reports_its_error_and_the_remaining_checkout(self):
        self.fail_smudge("false")
        real = git.git

        def fail_remove(repo, *args, **kwargs):
            if args[:3] == ("worktree", "remove", "--force"):
                raise HarnessError("fixture remove failure")
            return real(repo, *args, **kwargs)

        with patch.object(git, "git", side_effect=fail_remove):
            path, message = self.failed_run(expect_rollback_error=True)
        self.assertIn("git reset -q --hard failed", message)
        self.assertIn("fixture remove failure", message)
        self.assertIn(f"{path}：目录 present，登记 present", message)
        self.assertTrue(path.is_dir())
        self.assert_registration(path, present=True)

    def test_rollback_checks_ordinary_and_multiline_registrations_with_and_without_nul_listing(self):
        source = (self.scratch / "source").resolve()
        binding = git.add_worktree(self.repo, source, "HEAD")
        self.fail_smudge("false")
        head = git.head(self.repo)
        real_launch, real_git = git._launch, git.git

        def without_nul_listing(argv, env, **kwargs):
            if argv[-4:] == ["worktree", "list", "--porcelain", "-z"]:
                self.assertEqual(env["LC_ALL"], "C")
                return subprocess.CompletedProcess(argv, 129, b"", b"error: unknown switch `z'\n"
                                                   b"usage: git worktree add [<options>] <path> [<commit-ish>]\n")
            return real_launch(argv, env, **kwargs)

        for launch in (real_launch, without_nul_listing):
            for name, repo, origin in (("ordinary path", self.repo, None), ("multiline\npath\n", source, binding)):
                path = self.scratch / name
                for retained in (True, False):
                    def remove(repo, *args, **kwargs):
                        if retained and args[:3] == ("worktree", "remove", "--force"):
                            raise HarnessError("fixture remove failure")
                        return real_git(repo, *args, **kwargs)

                    with self.subTest(launch=launch.__name__, name=name, retained=retained):
                        with patch.object(git, "_launch", side_effect=launch), \
                                patch.object(git, "git", side_effect=remove), \
                                self.assertRaisesRegex(HarnessError, "git reset -q --hard failed") as error:
                            git.add_worktree(repo, path, head, binding=origin)
                        self.assertEqual(path.is_dir(), retained)
                        self.assert_registration(path, present=retained)
                        if retained:
                            self.assertIn("目录 present，登记 present", str(error.exception))
                            plain_git(self.repo, "worktree", "remove", "--force", str(path))
                        else:
                            self.assertNotIn("未能完全撤回", str(error.exception))

    def test_a_relative_backlink_is_ambiguous_without_nul_listing(self):
        path = self.scratch / "retained checkout"
        native_nul = plain_git(self.repo, "worktree", "list", "--porcelain", "-z", check=False).returncode == 0
        real_process, real_launch = git.git_process, git._launch
        reset_bindings = []

        def fail_checkout(repo, *args, **kwargs):
            if args == ("reset", "-q", "--hard", "--no-recurse-submodules"):
                binding = kwargs["binding"]
                reset_bindings.append(binding["worktree"])
                admin = Path(binding["git_dir"])
                relative = os.path.relpath(Path(binding["worktree"]) / ".git", admin)
                (admin / "gitdir").write_bytes(os.fsencode(relative) + b"\n")
                raise HarnessError("fixture reset failure")
            return real_process(repo, *args, **kwargs)

        def launch(argv, env, **kwargs):
            # Exercise real removal and option parsing on old Git. Reproduce those
            # outcomes on newer Git, which can recognize relative registrations.
            if native_nul:
                if argv[-4:-1] == ["worktree", "remove", "--force"]:
                    return subprocess.CompletedProcess(argv, 128, b"", b"fixture remove failure")
                if argv[-4:] == ["worktree", "list", "--porcelain", "-z"]:
                    return subprocess.CompletedProcess(argv, 129, b"", b"error: unknown switch `z'\n"
                                                       b"usage: git worktree add [<options>] <path> [<commit-ish>]\n")
            return real_launch(argv, env, **kwargs)

        with patch.object(git, "git_process", side_effect=fail_checkout), patch.object(git, "_launch", side_effect=launch), \
                self.assertRaisesRegex(HarnessError, "fixture reset failure") as error:
            git.add_worktree(self.repo, path, "HEAD")
        self.assertEqual(reset_bindings, [str(path.resolve())])
        self.assertTrue(path.is_dir())
        if not native_nul:
            listing = plain_git(self.repo, "worktree", "list", "--porcelain")
            self.assertIn(b"\nprunable ", listing.stdout)
        self.assertIn("git worktree remove --force failed", str(error.exception))
        self.assertIn(f"{path}：目录 present，登记 unknown", str(error.exception))

    def test_failed_binding_capture_still_checks_ordinary_and_multiline_registrations(self):
        real = git._launch
        alias = self.scratch / "parent alias"
        alias.symlink_to(self.scratch, target_is_directory=True)
        for path in (self.scratch / "ordinary path", alias / "multiline\npath\n"):
            for retained in (False, True):
                def fail_capture(argv, env, **kwargs):
                    if argv[-2:] == ["rev-parse", "--absolute-git-dir"] and Path(argv[2]) == path.resolve():
                        return subprocess.CompletedProcess(argv, 128, b"", b"fixture binding capture failure")
                    if retained and argv[-4:-1] == ["worktree", "remove", "--force"]:
                        return subprocess.CompletedProcess(argv, 128, b"", b"fixture remove failure")
                    return real(argv, env, **kwargs)

                with self.subTest(path=path, retained=retained):
                    with patch.object(git, "_launch", side_effect=fail_capture), \
                            self.assertRaisesRegex(HarnessError, "fixture binding capture failure") as error:
                        git.add_worktree(self.repo, path, "HEAD")
                    self.assertEqual(path.is_dir(), retained)
                    self.assert_registration(path, present=retained)
                    if retained:
                        self.assertIn("目录 present，登记 present", str(error.exception))
                        self.assertIn("fixture remove failure", str(error.exception))
                        plain_git(self.repo, "worktree", "remove", "--force", str(path))
                    else:
                        self.assertNotIn("未能完全撤回", str(error.exception))

    def test_rollback_uses_the_creation_binding_for_the_registration_query(self):
        canonical_parent = self.scratch.resolve()
        path = canonical_parent / "multiline\ncheckout\n"
        changed_target = canonical_parent / "other checkout"
        registered = os.fsencode(path)
        head_field = b"\0HEAD " + git.head(self.repo).encode("ascii")
        main = b"worktree " + os.fsencode(self.repo.resolve()) + head_field + b"\0branch refs/heads/main\0\0"
        real_git, real_process, real_resolve = git.git, git.git_process, Path.resolve

        for supports_nul in (True, False):
            with self.subTest(supports_nul=supports_nul):
                reset_failed = False

                def resolve(checkout, *args, **kwargs):
                    if reset_failed and checkout == path:
                        return changed_target
                    return real_resolve(checkout, *args, **kwargs)

                def command(repo, *args, **kwargs):
                    if args[:3] == ("worktree", "remove", "--force"):
                        raise HarnessError("fixture remove failure")
                    return real_git(repo, *args, **kwargs)

                def process(repo, *args, **kwargs):
                    nonlocal reset_failed
                    if args == ("reset", "-q", "--hard", "--no-recurse-submodules"):
                        self.assertEqual(kwargs["binding"]["worktree"], str(path))
                        reset_failed = True
                        raise HarnessError("fixture reset failure")
                    if args == ("worktree", "list", "--porcelain", "-z"):
                        if supports_nul:
                            return subprocess.CompletedProcess(
                                args, 0, main + b"worktree " + registered + head_field + b"\0detached\0\0", b"")
                        return subprocess.CompletedProcess(
                            args, 129, b"", b"error: unknown switch `z'\nusage: git worktree add ...\n")
                    return real_process(repo, *args, **kwargs)

                with patch.object(Path, "resolve", resolve), patch.object(git, "git", side_effect=command), \
                        patch.object(git, "git_process", side_effect=process), \
                        self.assertRaisesRegex(HarnessError, "fixture reset failure") as error:
                    git.add_worktree(self.repo, path, "HEAD")
                self.assertTrue(reset_failed)
                self.assertIn(f"{path}：目录 present，登记 present", str(error.exception))
                self.assert_registration(path, present=True)
                plain_git(self.repo, "worktree", "remove", "--force", str(path))

    def test_a_failed_new_checkout_hook_read_rolls_back_without_starting_reset(self):
        real, resets = git._launch, []
        parent = self.repo.resolve() / ".harness" / "worktrees"

        def launch(argv, env, **kwargs):
            if Path(argv[2]).parent == parent:
                if "--get-regexp" in argv:
                    return subprocess.CompletedProcess(argv, 128, b"", b"fixture hook read failure")
                if "reset" in argv:
                    resets.append(argv)
            return real(argv, env, **kwargs)

        with patch.object(git, "_launch", side_effect=launch):
            path, message = self.failed_run(fails_during_reset=False)
        self.assertEqual(resets, [])
        self.assertIn("fixture hook read failure", message)
        self.assertFalse(os.path.lexists(path))
        self.assert_registration(path, present=False)

    def test_a_failed_registration_query_does_not_hide_the_directory_result(self):
        self.fail_smudge("false")
        real = git._launch

        def fail_list(argv, env, **kwargs):
            if argv[-4:] == ["worktree", "list", "--porcelain", "-z"]:
                return subprocess.CompletedProcess(argv, 129, b"", b"error: fixture list I/O failure\n")
            return real(argv, env, **kwargs)

        with patch.object(git, "_launch", side_effect=fail_list):
            path, message = self.failed_run(expect_rollback_error=True)
        self.assertIn("git reset -q --hard failed", message)
        self.assertIn(f"{path}：目录 absent，登记 unknown", message)
        self.assertFalse(os.path.lexists(path))
        self.assert_registration(path, present=False)

    def test_incomplete_or_contradictory_nul_records_leave_registration_unknown(self):
        self.fail_smudge("false")
        real = git._launch
        path = self.scratch / "multiline\ncheckout"
        head_field = b"\0HEAD " + git.head(self.repo).encode("ascii")
        main = b"worktree " + os.fsencode(self.repo.resolve()) + head_field + b"\0branch refs/heads/main\0\0"
        checkout = b"worktree " + os.fsencode(path.resolve())
        records = {
            "path only": checkout,
            "missing HEAD": checkout + b"\0detached",
            "missing branch or detached": checkout + head_field,
            "branch and detached": checkout + head_field + b"\0branch refs/heads/fixture\0detached",
            "duplicate branch": checkout + head_field + b"\0branch refs/heads/fixture\0branch refs/heads/other",
            "malformed record after a match": checkout + head_field + b"\0detached\0\0worktree /incomplete",
        }
        for case, record in records.items():
            def launch(argv, env, **kwargs):
                if argv[-4:] == ["worktree", "list", "--porcelain", "-z"]:
                    return subprocess.CompletedProcess(argv, 0, main + record + b"\0\0", b"")
                return real(argv, env, **kwargs)

            with self.subTest(case=case):
                with patch.object(git, "_launch", side_effect=launch), \
                        self.assertRaisesRegex(HarnessError, "git reset -q --hard failed") as error:
                    git.add_worktree(self.repo, path, "HEAD")
                self.assertIn(f"{path}：目录 absent，登记 unknown", str(error.exception))
                self.assertFalse(os.path.lexists(path))
                self.assert_registration(path, present=False)

    def test_a_missing_nul_between_worktree_records_leaves_registration_unknown(self):
        listing_args = ("worktree", "list", "--porcelain", "-z")
        probe = plain_git(self.repo, *listing_args, env={**os.environ, "LC_ALL": "C"}, check=False)
        if probe.returncode == 129 and probe.stderr.startswith(b"error: unknown switch `z'\n"):
            self.skipTest("Git does not support NUL worktree listings")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.fail_smudge("false")
        real = git._launch
        path = self.scratch / "multiline\ncheckout"
        before_remove = None

        def launch(argv, env, **kwargs):
            nonlocal before_remove
            if argv[-4:-1] == ["worktree", "remove", "--force"]:
                before_remove = plain_git(self.repo, *listing_args).stdout
            result = real(argv, env, **kwargs)
            if tuple(argv[-4:]) == listing_args:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNotNone(before_remove)
                main, separator, checkout = before_remove.partition(b"\0\0")
                self.assertEqual(result.stdout, main + separator)
                self.assertTrue(checkout.startswith(b"worktree " + os.fsencode(path.resolve()) + b"\0"))
                # Drop one NUL so the stale linked record is merged into the main one.
                return subprocess.CompletedProcess(argv, 0, main + b"\0" + checkout, result.stderr)
            return result

        with patch.object(git, "_launch", side_effect=launch), \
                self.assertRaisesRegex(HarnessError, "git reset -q --hard failed") as error:
            git.add_worktree(self.repo, path, "HEAD")
        self.assertFalse(os.path.lexists(path))
        self.assert_registration(path, present=False)
        self.assertIn(f"{path}：目录 absent，登记 unknown", str(error.exception))

    def test_a_locked_record_also_marked_prunable_leaves_registration_unknown(self):
        listing_args = ("worktree", "list", "--porcelain", "-z")
        probe = plain_git(self.repo, *listing_args, env={**os.environ, "LC_ALL": "C"}, check=False)
        if probe.returncode == 129 and probe.stderr.startswith(b"error: unknown switch `z'\n"):
            self.skipTest("Git does not support NUL worktree listings")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.fail_smudge("false")
        real = git._launch
        path = self.scratch / "multiline\ncheckout"
        locked = b"\0locked offline disk\0\0"

        def launch(argv, env, **kwargs):
            if argv[-4:-1] == ["worktree", "remove", "--force"]:
                plain_git(self.repo, "worktree", "lock", "--reason", "offline disk", str(path))
                return subprocess.CompletedProcess(argv, 128, b"", b"fixture remove failure")
            listing = real(argv, env, **kwargs)
            if tuple(argv[-4:]) == listing_args:
                self.assertEqual(listing.returncode, 0, listing.stderr)
                self.assertEqual(listing.stdout.count(locked), 1)
                return subprocess.CompletedProcess(argv, 0, listing.stdout.replace(
                    locked, b"\0locked offline disk\0prunable fixture reason\0\0"), listing.stderr)
            return listing

        with patch.object(git, "_launch", side_effect=launch), \
                self.assertRaisesRegex(HarnessError, "git reset -q --hard failed") as error:
            git.add_worktree(self.repo, path, "HEAD")
        self.assertTrue(path.is_dir())
        self.assert_registration(path, present=True)
        self.assertIn(locked, plain_git(self.repo, *listing_args).stdout)
        self.assertIn("fixture remove failure", str(error.exception))
        self.assertIn(f"{path}：目录 present，登记 unknown", str(error.exception))

    @unittest.skipIf(os.geteuid() == 0, "Git needs to run without root to observe an unreadable backlink")
    def test_an_unreadable_registration_omitted_by_git_is_unknown(self):
        real = git.git_process
        path = self.scratch / "retained checkout"
        backlinks = []

        def fail_checkout(repo, *args, **kwargs):
            if args == ("reset", "-q", "--hard", "--no-recurse-submodules"):
                backlink = Path(kwargs["binding"]["git_dir"]) / "gitdir"
                backlinks.append(backlink)
                self.addCleanup(backlink.chmod, 0o644)
                backlink.chmod(0)
                raise HarnessError("fixture reset failure")
            return real(repo, *args, **kwargs)

        with patch.object(git, "git_process", side_effect=fail_checkout), self.assertRaises(HarnessError) as error:
            git.add_worktree(self.repo, path, "HEAD")
        self.assertTrue(path.is_dir())
        backlink, = backlinks
        self.assertTrue(backlink.is_file())
        self.assertIn("fixture reset failure", str(error.exception))
        self.assertIn("git worktree remove --force failed", str(error.exception))
        self.assertIn("目录 present，登记 unknown", str(error.exception))
        listing = plain_git(self.repo, "worktree", "list", "--porcelain", "-z", check=False)
        if listing.returncode == 0:
            self.assertEqual(listing.stderr, b"")
            self.assertNotIn(b"worktree " + os.fsencode(path.resolve()), listing.stdout.split(b"\0"))

    def test_a_truncated_listing_hit_does_not_confirm_registration(self):
        listing_args = ("worktree", "list", "--porcelain", "-z")
        probe = plain_git(self.repo, *listing_args, env={**os.environ, "LC_ALL": "C"}, check=False)
        if probe.returncode == 129 and probe.stderr.startswith(b"error: unknown switch `z'\n"):
            self.skipTest("Git does not support NUL worktree listings")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.fail_smudge("false")
        real = git.git
        path = self.scratch / "retained\ncheckout"
        original = None

        def fail_remove(repo, *args, **kwargs):
            nonlocal original
            if args[:3] == ("worktree", "remove", "--force"):
                backlink, = (self.repo / ".git" / "worktrees").glob("*/gitdir")
                original = backlink.read_bytes()
                self.addCleanup(backlink.write_bytes, original)
                backlink.write_bytes(original.removesuffix(b"/.git\n") + b"\0trailing bytes/.git\n")
                raise HarnessError("fixture remove failure")
            return real(repo, *args, **kwargs)

        with patch.object(git, "git", side_effect=fail_remove), \
                self.assertRaisesRegex(HarnessError, "git reset -q --hard failed") as error:
            git.add_worktree(self.repo, path, "HEAD")
        listing = plain_git(self.repo, *listing_args)
        self.assertEqual(listing.stderr, b"")
        self.assertIn(b"worktree " + original.removesuffix(b"/.git\n"), listing.stdout.split(b"\0"))
        self.assertTrue(path.is_dir())
        self.assertIn("fixture remove failure", str(error.exception))
        self.assertIn(f"{path}：目录 present，登记 unknown", str(error.exception))

    def test_a_listing_that_disagrees_with_the_registry_is_unknown(self):
        self.fail_smudge("false")
        real = git._launch
        path = self.scratch / "retained checkout"
        head_field = b"\0HEAD " + git.head(self.repo).encode("ascii")
        main_only = b"worktree " + os.fsencode(self.repo.resolve()) + head_field + b"\0branch refs/heads/main\0\0"

        def launch(argv, env, **kwargs):
            if argv[-4:] == ["worktree", "list", "--porcelain", "-z"]:
                return subprocess.CompletedProcess(argv, 0, main_only, b"")
            if "worktree" in argv and "remove" in argv:
                return subprocess.CompletedProcess(argv, 128, b"", b"fixture remove failure")
            return real(argv, env, **kwargs)

        with patch.object(git, "_launch", side_effect=launch), self.assertRaises(HarnessError) as error:
            git.add_worktree(self.repo, path, "HEAD")
        self.assertIn("目录 present，登记 unknown", str(error.exception))
        self.assert_registration(path, present=True)

    @unittest.skipIf(os.geteuid() == 0, "Git needs to run without root to observe a directory deletion failure")
    def test_partially_undone_checkout_is_reported_and_the_path_can_be_reused(self):
        script = self.scratch / "failed-smudge"
        script.write_text("#!/bin/sh\nmkdir retained\nprintf retained > retained/entry\nchmod a-w retained\nexit 1\n")
        script.chmod(0o755)
        self.fail_smudge(f"'{script}'")
        self.addCleanup(self.restore_permissions)
        path, message = self.failed_run(expect_rollback_error=True)
        retained = path / "retained"
        self.assertEqual((retained / "entry").read_text(), "retained")
        self.assertIn("git reset -q --hard failed", message)
        self.assertIn("git worktree remove --force failed", message)
        self.assertIn(f"{path}：目录 present，登记 absent", message)
        self.assert_registration(path, present=False)

        retained.chmod(0o755)
        shutil.rmtree(path)
        plain_git(self.repo, "config", "filter.fixture.smudge", "cat")
        binding = git.add_worktree(self.repo, path, "HEAD")
        self.assertEqual((path / "fixture.txt").read_text(), "tracked content\n")
        self.assertEqual(binding["worktree"], str(path))

    def restore_permissions(self):
        for path in (self.repo / ".harness" / "worktrees").glob("*/retained"):
            path.chmod(0o755)

    def test_interrupt_before_population_preserves_unbound_registration(self):
        engine = self.engine()
        with patch.object(git, "capture_worktree_binding", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet")
        state = read_json(engine.directory / "state.json")
        path = Path(state["worktree"])
        self.assertEqual((state["status"], state["worktree_binding"], state["dispatches"]), ("interrupted", {}, []))
        self.assert_registration(path, present=True)
        self.assertEqual({p.name for p in path.iterdir()}, {".git"})
        admin = Path(plain_git(path, "rev-parse", "--absolute-git-dir").stdout.decode().strip())
        self.assertFalse((admin / "index").exists())
        with self.assertRaises(HarnessError) as resumed:
            self.engine().resume(state["id"])
        stop = stops.of(resumed.exception)
        self.assertEqual((stop["kind"], stop["reason"]), ("workspace_mismatch", "binding_missing"))
        self.assertEqual(self.engine().abandon(state["id"])["status"], "abandoned")


@unittest.skipUnless(configured_hooks_exist(), "configured hooks start with Git 2.54")
class NewCheckoutHookTests(IsolatedRepoCase):
    """Case 15c: configuration visible only inside a new checkout or newly initialized repository."""

    def setUp(self):
        super().setUp()
        self.markers = Markers(self.scratch)
        self.plan = read_json(ROOT / "examples" / "plan.json")

    def include(self, condition, label):
        self.markers.included.write_text(self.markers.configured(label))
        plain_git(self.repo, "config", f"includeIf.{condition}.path", str(self.markers.included))

    def test_run_creation_reads_hooks_from_its_new_branch(self):
        self.include("onbranch:harness/**", "onbranch")
        control = self.scratch / "control"
        plain_git(self.repo, "worktree", "add", "-b", "harness/control", str(control), "HEAD")
        self.assertTrue({"config:onbranch-post-index-change", "config:onbranch-reference-transaction"}
                        <= {label for label, _ in self.markers.read()})
        self.markers.reset()

        # Prove the name read suffices even where Git also supports per-event switches.
        with patch.object(git, "HOOK_EVENTS", ()):
            result = self.engine().start("Implement greet", self.plan, pause_after_plan=True)
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(self.markers.read(), [])
        worktree = Path(result["worktree"])
        self.assertEqual(plain_git(worktree, "symbolic-ref", "HEAD").stdout,
                         f"refs/heads/{result['branch']}\n".encode())
        for args in (("ls-files", "--stage", "-z"), ("status", "--porcelain")):
            self.assertEqual(plain_git(worktree, *args).stdout, plain_git(control, *args).stdout)
        paths = plain_git(control, "ls-files", "-z").stdout.rstrip(b"\0").split(b"\0")
        for path in map(os.fsdecode, paths):
            self.assertEqual((worktree / path).read_bytes(), (control / path).read_bytes())

    def test_fresh_verification_reads_hooks_from_its_own_gitdir(self):
        self.include("gitdir:worktrees/checkout*", "gitdir")
        self.config.data["verification"]["fresh_checkout"] = True
        engine = self.engine()
        with patch.object(tempfile, "tempdir", str(self.scratch)), patch.object(git, "HOOK_EVENTS", ()), \
                patch.object(cleanup, "clean_checkout", side_effect=cleanup.CheckoutRemovalIncomplete("retain fixture for inspection")):
            result = engine.start("Implement greet", self.plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.markers.read(), [])
        artifact, = engine.directory.glob("verification/final/*/verification-*/checkout.json")
        environment = read_json(artifact)
        checkout = Path(environment["worktree"])
        head = result["expected_head"]
        self.assertNotEqual(head, result["base_commit"])
        self.assertEqual((environment["status"], environment["head"]), ("verified", head))
        self.assertEqual(engine.run["verification_checkouts"][0]["binding"]["detached_head"], head)
        self.assertEqual(git.head(checkout, binding=engine.run["verification_checkouts"][0]["binding"]), head)
        self.assertEqual(plain_git(checkout, "symbolic-ref", "-q", "HEAD", check=False).returncode, 1)
        # Only this Git directory sees the include; it was absent in the source run worktree.
        key = "hook.gitdir-post-index-change.command"
        self.assertEqual(plain_git(engine.worktree, "config", "--get", key, check=False).returncode, 1)
        self.assertIn(b"config:gitdir-post-index-change", plain_git(checkout, "config", "--get", key).stdout)

    def test_demo_init_excludes_template_and_globals_visible_only_after_init(self):
        template = self.scratch / "template"
        template.mkdir()
        (template / "config").write_text(self.markers.configured("template"))
        self.markers.included.write_text(self.markers.configured("new-gitdir"))
        isolated_global = (f'[init]\n\ttemplateDir = "{template}"\n'
                           f'[includeIf "gitdir:{self.scratch.resolve()}/"]\n\tpath = "{self.markers.included}"\n')
        with git_isolation.user_config(isolated_global):
            for name, options in (("with-template", ()), ("without-template", ("--template=",))):
                control = self.scratch / name
                control.mkdir()
                plain_git(control, "init", "-b", "main", *options)
                labels = {label for label, _ in self.markers.read()}
                self.assertIn("config:new-gitdir-reference-transaction", labels)
                if not options:
                    self.assertIn("config:template-reference-transaction", labels)
                self.markers.reset()
            output = io.StringIO()
            with patch.object(tempfile, "tempdir", str(self.scratch)), patch.object(git, "HOOK_EVENTS", ()), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                cli.demo(self.config, live=False)
            result = json.loads(output.getvalue())
            self.assertEqual((result["status"], result["models_called"]), ("completed", False))
            self.assertEqual(self.markers.read(), [])
            demo = Path(result["repo"])
            self.assertEqual(plain_git(demo, "config", "--local", "--name-only", "--get-regexp",
                                       r"^hook\..*\.command$", check=False).returncode, 1)
            self.assertFalse((demo / ".git" / "description").exists())
            self.assertIn(b"config:new-gitdir-reference-transaction",
                          plain_git(demo, "config", "--get", "hook.new-gitdir-reference-transaction.command").stdout)


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


# R2 and case 1 share this table: (module, Python qualified name) maps to (declaration count, trigger).
READ_SITES = {
    ("git", "_configured_hooks"): (1, lambda c: git.invocation(
        c.repo, ("status",), effect=git.COMPARE, deadline=time.monotonic() + 60)),
    ("git", "worktree_registry"): (2, lambda c: c.registered_worktrees()),
    ("git", "capture_worktree_binding"): (3, lambda c: git.capture_worktree_binding(c.detached, None)),
    ("git", "_check_binding"): (1, lambda c: git.check_worktree_binding(c.linked, c.binding)),
    ("git", "repository"): (1, lambda c: git.repository(c.linked)),
    ("git", "head"): (1, lambda c: git.head(c.linked, binding=c.binding)),
    ("git", "check_starting_checkout"): (1, lambda c: git.check_starting_checkout(c.repo)),
    ("git", "reject_submodules"): (1, lambda c: git.reject_submodules(c.linked, binding=c.binding)),
    ("git", "assert_removable_worktree"): (5, lambda c: git.assert_removable_worktree(c.linked, binding=c.binding)),
    ("git", "ignored_paths"): (1, lambda c: git.ignored_paths(c.linked, binding=c.binding)),
    ("git", "read_worktree"): (2, lambda c: git.read_worktree(c.linked, binding=c.binding)),
    ("finalization", "FinalAcceptance.final_review"): (2, lambda c: c.final_entry()),
    ("git", "prove_image"): (1, lambda c: c.prove_candidate_image()),
    ("git", "changed"): (1, lambda c: git.changed(c.linked, binding=c.binding)),
    ("git", "diff_bytes"): (1, lambda c: git.diff_bytes(c.linked, binding=c.binding)),
    ("git", "_range_diff"): (1, lambda c: c.range_comparison()),
    ("git", "archive_and_reset"): (1, lambda c: c.archive_candidate()),
    ("git", "build_commit"): (3, lambda c: c.build_candidate()),
    ("git", "_check_candidate_tree"): (1, lambda c: c.build_candidate()),
    ("git", "branch_state"): (2, lambda c: git.branch_state(c.linked, c.binding["branch"], binding=c.binding)),
    ("git", "object_kinds"): (1, lambda c: git.object_kinds(c.linked, [c.tip], binding=c.binding)),
    ("git", "commit_header"): (1, lambda c: git.commit_header(c.linked, c.tip, binding=c.binding)),
    ("git", "tree_readable"): (1, lambda c: git.tree_readable(c.linked, c.tip_tree, binding=c.binding)),
    ("git", "index_digest"): (1, lambda c: git.index_digest(c.linked, binding=c.binding)),
    ("cleanup", "common_dir"): (1, lambda c: cleanup.common_dir(c.repo)),
    ("cleanup", "_branch_matches"): (1, lambda c: cleanup._branch_matches(c.repo, "main", c.tip)),
    ("private_state", "private_state_lock"): (1, lambda c: c.lock_private_state()),
    ("private_state", "_initialize"): (4, lambda c: private_state.ensure_private_state(c.repo)),
    ("memory", "bind_source_files"): (4, lambda c: memory.bind_source_files(c.linked, ["test_hello.py"])),
    ("memory", "Memory._check_proposals"): (1, lambda c: c.memory.check_proposals(c.proposals, ["fixture"], c.tip)),
    ("memory", "Memory._staleness"): (1, lambda c: c.memory.read(c.memory_id)),
    ("command_server", "Broker._delete_eligibility"): (1, lambda c: c.broker._delete_eligibility(
        c.broker.manifest, c.broker.manifest["delete_scope"], "test_hello.py")),
    ("workspace_inputs", "_destination"): (2, lambda c: workspace_inputs._destination(
        {"worktree": str(c.linked), "worktree_binding": c.binding}, "__pycache__/input.pyc")),
    ("operations", "harness_identity"): (1, lambda c: c.installation_identity()),
    ("git_query", "_object_format"): (1, lambda c: c.query("hash-object", "--no-filters", "--", "test_hello.py")),
    ("git_query", "run_git_query"): (1, lambda c: c.query("show", "HEAD:test_hello.py")),
}
MIXED_READ_SITES = {("git", name) for name in ("check_starting_checkout", "changed", "diff_bytes",
                                              "archive_and_reset", "build_commit",
                                              "_check_candidate_tree", "prove_image")} | {("operations", "harness_identity"),
                                                                       ("finalization", "FinalAcceptance.final_review")}


class ReadSiteTests(IsolatedRepoCase):
    """Case 1: execute every registered READ in a marked repository with stale stat data."""

    def setUp(self):
        super().setUp()
        self.base = git.head(self.repo)
        name = "test_hello.py"
        source = self.repo / name
        source.write_bytes(source.read_bytes() + b"\n# committed fixture change\n")
        (self.repo / "added.txt").write_text("added range fixture\n")
        plain_git(self.repo, "add", ".")
        plain_git(self.repo, *IDENTITY, "commit", "-qm", "READ fixture")
        self.tip = git.head(self.repo)
        self.tip_tree = plain_git(self.repo, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
        self.linked = (self.scratch / "linked").resolve()
        self.detached = (self.scratch / "detached").resolve()
        self.binding = git.add_worktree(self.repo, self.linked, self.tip, branch="linked")
        detached_binding = git.add_worktree(self.repo, self.detached, self.tip)
        listing = plain_git(self.repo, "worktree", "list", "--porcelain", "-z",
                            env=dict(os.environ, LC_ALL="C"), check=False)
        if listing.returncode:
            self.assertEqual((listing.returncode, listing.stdout), (129, b""), listing.stderr)
            self.assertTrue(listing.stderr.startswith(b"error: unknown switch `z'\nusage: git worktree "), listing.stderr)
        private_state.ensure_private_state(self.repo)
        self.memory = memory.Memory(self.scratch / "memories" / "memory.sqlite3", self.linked, binding=self.binding)
        self.proposals = memory.validate_proposals([{"key": "fixture", "abstract": "Source fixture",
            "content": "A tracked source file", "source_paths": [name]}], self.linked)
        self.memory_id, = self.memory.remember_proposals(self.proposals, ["fixture"], self.tip, "fixture", "task")
        manifest = command_server.create_manifest(
            self.config, self.linked, self.scratch.resolve() / "broker",
            delete_scope={"task_id": "fixture", "allowed_paths": [name]})
        self.broker = command_server.Broker(manifest["artifact"], manifest["artifact_digest"])

        plain_git(self.repo, "replace", self.tip, self.base)
        plain_git(self.repo, "config", "extensions.worktreeConfig", "true")
        self.markers = Markers(self.scratch)
        self.markers.arm(self.repo)
        # The main checkout has the fsmonitor hook, the linked checkout selects the built-in monitor.
        plain_git(self.linked, "config", "--worktree", "core.fsmonitor", "true")
        self.read_trace = self.outside / "read-git.trace"
        builtins = plain_git(self.linked, "--list-cmds=builtins").stdout.splitlines()
        self.has_fsmonitor_daemon = (b"fsmonitor--daemon" in builtins and
                                     plain_git(self.linked, "fsmonitor--daemon", "status", check=False).returncode in (0, 1))
        if self.has_fsmonitor_daemon:
            self.addCleanup(plain_git, self.linked, "fsmonitor--daemon", "stop", check=False)
        admins = ((self.repo, self.repo / ".git"), (self.linked, Path(self.binding["git_dir"])),
                  (self.detached, Path(detached_binding["git_dir"])))
        self.read_trees = tuple(tree for tree, _ in admins)
        tracked = plain_git(self.repo, "ls-files", "-z").stdout
        self.tracked_paths = tuple(os.fsdecode(path) for path in tracked.split(b"\0") if path)
        for tree, admin in admins:
            plain_git(tree, "-c", "core.fsmonitor=false", "update-index", "--split-index")
            self.assertTrue(list(admin.glob("sharedindex.*")), admin)
        trace = self.scratch / "trace"
        trace.mkdir()
        with self.global_config.open("a") as config:
            config.write("[trace2]\n")
            for kind in ("event", "normal", "perf"):
                path = trace / kind
                path.write_bytes(b"")
                config.write(f"\t{kind}Target = {path}\n")
        plain_git(self.repo, "rev-parse", "HEAD")
        self.assertTrue(all(path.stat().st_size for path in trace.iterdir()))

    def registered_worktrees(self):
        self.assertEqual(git._worktree_registration(
            self.repo, os.fsencode(self.linked), binding=None, isolated_config=False), "present")
        self.assertEqual(set(cleanup._registered(self.repo).values()),
                         {str(self.repo.resolve()), str(self.linked), str(self.detached)})

    def lock_private_state(self):
        with private_state.private_state_lock(self.repo):
            pass

    def query(self, *argv):
        query, _ = git_query.parse_git_query(["git", *argv])
        result = git_query.run_git_query(query, self.linked, self.binding)
        self.assertEqual(result["exit_code"], 0, result)

    def prove_candidate_image(self):
        name = "image-proof-untracked"
        probe = self.linked / name
        probe.write_bytes(b"private index proof\n")
        try:
            result = git.prove_image(self.linked, self.tip, self.tip_tree, binding=self.binding)
            self.assertIn(name, result.paths)
            self.assertIsNone(result.detail)
        finally:
            probe.unlink()

    def final_entry(self):
        changed = self.linked / "dirty-final-entry"
        changed.write_text("untracked\n")
        context = SimpleNamespace(
            worktree=self.linked, run={"expected_head": self.tip, "worktree_binding": self.binding, "final_history": []},
            read_worktree=lambda: git.read_worktree(self.linked, binding=self.binding))
        try:
            with self.assertRaises(HarnessError) as caught:
                finalization.FinalAcceptance(context).final_review()
            self.assertEqual(stops.of(caught.exception)["reason"], "dirty_final_workspace")
        finally:
            changed.unlink()

    def range_comparison(self):
        self.assertIn(b"committed fixture change", git.range_diff_bytes(self.linked, self.base, self.tip, binding=self.binding))
        self.assertIn("added.txt\0", git.range_paths(self.linked, self.base, self.tip, binding=self.binding))

    def installation_identity(self):
        with patch.object(operations, "PACKAGE", self.repo.resolve() / "harness"):
            self.assertEqual(operations.harness_identity()["revision"], self.tip)

    def archive_candidate(self):
        (self.linked / "draft.txt").write_text("discarded draft\n")
        archive = self.scratch / "archive"
        git.archive_and_reset(self.linked, self.tip, archive, self.scratch, binding=self.binding)
        self.assertEqual((archive / "files" / "draft.txt").read_text(), "discarded draft\n")
        self.assertFalse((self.linked / "draft.txt").exists())

    def build_candidate(self):
        path = "new-candidate.txt"
        (self.linked / path).write_text("candidate-only content")
        result = git.build_commit(self.linked, self.tip, [path], [path], "task", "Candidate\n", binding=self.binding)
        self.assertNotEqual(result.tree, self.tip_tree)

    def fingerprint(self):
        return self.files(self.repo, self.global_config) | self.files(self.scratch, skip=("memories",))

    def test_read_fingerprint_covers_links_and_special_entries_without_reading_targets(self):
        outside_file = self.outside / "target.txt"
        outside_dir = self.outside / "target-dir"
        outside_dir.mkdir()
        outside_file.write_text("outside\n")
        (outside_dir / "child").write_text("outside child\n")
        checkout = self.scratch / self.linked.name
        file_link = checkout / "file-link"
        dir_link = checkout / "dir-link"
        file_link.symlink_to(outside_file)
        dir_link.symlink_to(outside_dir, target_is_directory=True)
        pipe = checkout / "pipe"
        os.mkfifo(pipe)

        initial = self.fingerprint()
        self.assertEqual(initial[str(file_link)][-1], os.fsencode(outside_file))
        self.assertEqual(initial[str(dir_link)][-1], os.fsencode(outside_dir))
        self.assertNotIn(str(dir_link / "child"), initial)
        self.assertTrue(stat.S_ISFIFO(initial[str(pipe)][0]))
        outside_file.write_text("changed target\n")
        (outside_dir / "child").write_text("changed child\n")
        self.assertEqual(self.fingerprint(), initial)

        info = dir_link.lstat()
        os.utime(dir_link, ns=(info.st_atime_ns, info.st_mtime_ns - 10**9), follow_symlinks=False)
        self.assertEqual(self.fingerprint()[str(dir_link)][3], info.st_mtime_ns - 10**9)
        info = file_link.lstat()
        file_link.unlink()
        self.assertNotIn(str(file_link), self.fingerprint())
        file_link.symlink_to(self.outside / "missing")
        os.utime(file_link, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=False)
        changed = self.fingerprint()
        self.assertEqual(changed[str(file_link)][-1], os.fsencode(self.outside / "missing"))
        self.assertNotEqual(changed[str(file_link)], initial[str(file_link)])
        pipe.unlink()
        pipe.symlink_to(outside_file)
        self.assertNotEqual(self.fingerprint()[str(pipe)][0], initial[str(pipe)][0])

    def trace_read(self, env):
        # Use the classic trace outside the fingerprint and leave the configured Trace2 targets in place.
        return dict(env, GIT_TRACE=str(self.read_trace))

    def test_every_registered_read_preserves_git_and_worktree_files_and_runs_no_programs(self):
        self.assert_plain_git_runs_filters(clean="clean", smudge="smudge")
        self.assert_registered_reads(READ_SITES)

    def test_every_registered_read_runs_no_programs_with_a_process_filter_on_all_paths(self):
        # Git gives process precedence over clean and smudge for the same path.
        # Run the same sites in a fresh fixture with process added to the wildcard driver.
        with self.markers.included.open("a") as config:
            config.write(f'[filter "marker"]\n\tprocess = \'{self.markers.process}\'\n')
        self.assert_plain_git_runs_filters(clean="process", smudge="process")
        self.assert_registered_reads(READ_SITES)

    def assert_plain_git_runs_filters(self, *, clean, smudge):
        options = ("--no-replace-objects", "-c", "core.fsmonitor=", "-c", "diff.autoRefreshIndex=false")
        for tree in self.read_trees:
            cwd = str(tree.resolve())
            for path in self.tracked_paths:
                with self.subTest(checkout=tree, path=path):
                    self.markers.reset()
                    expire_stats(tree / path)
                    plain_git(tree, *options, "diff", "--no-ext-diff", "--no-textconv", "--", path)
                    self.assertEqual(set(self.markers.read()), {(clean, cwd)})
                    self.markers.reset()
                    plain_git(tree, *options, "cat-file", "--filters", f"HEAD:{path}")
                    self.assertEqual(set(self.markers.read()), {(smudge, cwd)})

    def test_a_registered_nested_read_passes_the_tripwire_and_the_runtime_checks(self):
        module = "read_site_fixture"
        source = '''
from harness import git

def read_head(repo):
    def nested():
        return git.git(repo, "rev-parse", "HEAD", effect=git.READ)
    return nested()
'''
        namespace = {"__name__": f"harness.{module}"}
        exec(source, namespace)

        def trigger(case):
            case.assertEqual(namespace["read_head"](case.linked).strip(), case.tip)

        site = (module, "read_head.<locals>.nested")
        self.assert_registered_reads({site: (1, trigger)})
        reads = collections.Counter()
        problems, _ = call_shape_problems({module: source}, reads=reads)
        self.assertEqual(problems, [])
        self.assertEqual(reads, {site: 1})

    def assert_registered_reads(self, read_sites):
        # Run the pure queries on the original fixture before commands that reset or commit.
        ordered = sorted(read_sites, key=lambda site: site in MIXED_READ_SITES)
        invocation, launch, capture, install_query = git.invocation, git._launch, git_query._capture, operations._git_query
        forwarded = {git.git.__code__, git.git_bytes.__code__, git.git_process.__code__,
                     git._run_git.__code__, install_query.__code__}
        observed = collections.defaultdict(set)

        def record(frame, effect):
            if effect is not git.READ:
                return
            while frame.f_code in forwarded:
                frame = frame.f_back
            site = (frame.f_globals["__name__"].removeprefix("harness."), frame.f_code.co_qualname)
            # A loop or repeated call to the trigger may reach one declaration many times.
            observed[site].add(frame.f_lasti)

        def unchanged(function, *args, **kwargs):
            before = self.fingerprint()
            try:
                return function(*args, **kwargs)
            finally:
                self.assertEqual(self.fingerprint(), before, function.__qualname__)

        def construct(*args, effect, **kwargs):
            record(sys._getframe(1), effect)
            if effect is git.READ:
                argv, env = unchanged(invocation, *args, effect=effect, **kwargs)
                return argv, self.trace_read(env)
            return invocation(*args, effect=effect, **kwargs)

        def launch_checked(argv, env, *, effect, **kwargs):
            if sys._getframe(1).f_code is git._configured_hooks.__code__:
                record(sys._getframe(1), effect)
                if effect is git.READ:
                    env = self.trace_read(env)
            if effect is git.READ:
                return unchanged(launch, argv, env, effect=effect, **kwargs)
            return launch(argv, env, effect=effect, **kwargs)

        def install_query_checked(*args, effect):
            if effect is git.READ:
                return unchanged(install_query, *args, effect=effect)
            return install_query(*args, effect=effect)

        forwarded.update((unchanged.__code__, install_query_checked.__code__))
        with patch.object(git, "invocation", construct), patch.object(git, "_launch", launch_checked), \
                patch.object(git_query, "_capture", functools.partial(unchanged, capture)), \
                patch.object(operations, "_git_query", install_query_checked):
            for site in ordered:
                with self.subTest(site=".".join(site)):
                    for tree in self.read_trees:
                        for path in self.tracked_paths:
                            expire_stats(tree / path)
                    self.markers.reset()
                    self.read_trace.write_text("")
                    observed.clear()
                    declarations, trigger = read_sites[site]
                    if site in MIXED_READ_SITES:
                        trigger(self)
                    else:
                        unchanged(trigger, self)
                        self.assertEqual(self.markers.read(), [])
                    self.assert_no_fsmonitor_daemon_spawn(self.read_trace, "READ")
                    self.assertEqual(len(observed[site]), declarations)

    def test_read_trace_detects_daemon_spawn_with_one_override_removed(self):
        if not self.has_fsmonitor_daemon:
            self.skipTest("Git has no built-in fsmonitor daemon on this platform")
        launch = git._launch
        mutated = []

        def remove_override(argv, env, *, effect, **kwargs):
            if effect is git.READ and argv[-4:] == ["ls-files", "--others", "--exclude-standard", "-z"]:
                argv = list(argv)
                index = argv.index("core.fsmonitor=")
                self.assertEqual(argv[index - 1], "-c")
                del argv[index - 1:index + 1]
                env = self.trace_read(env)
                mutated.append(argv)
            return launch(argv, env, effect=effect, **kwargs)

        self.read_trace.write_text("")
        with patch.object(git, "_launch", remove_override):
            git.assert_removable_worktree(self.linked, binding=self.binding)
        self.assertEqual(len(mutated), 1)
        with self.assertRaisesRegex(AssertionError, "READ spawned fsmonitor--daemon"):
            self.assert_no_fsmonitor_daemon_spawn(self.read_trace, "READ")


class LifecycleEffectTests(IsolatedRepoCase):
    """Cases 3 and 15b: observe actual effects during checkout, archive/reset and acceptance."""

    def setUp(self):
        super().setUp()
        self.markers = Markers(self.scratch)
        self.markers.arm(self.repo)
        self.config.data["verification"]["commands"] = [{"cwd": ".", "argv": [sys.executable, "-c", "pass"]}]
        self.config.data["verification"]["fresh_checkout"] = True

    def preserved_state(self, effect):
        """Observe the index entries, refs and worktree files that this call must preserve."""
        admin = (self.repo / ".git").resolve()
        linked = sorted(path for path in (admin / "worktrees").glob("*") if (path / "gitdir").is_file())
        state = {}
        if not effect.real_index:
            state["index"] = {path: plain_git(self.repo, "--git-dir=" + str(path), "-c", "core.fsmonitor=false",
                                             "ls-files", "--stage", "-z").stdout for path in (admin, *linked)}
        if not effect.refs:
            state["refs"] = plain_git(self.repo, "for-each-ref", "--format=%(refname) %(objectname) %(symref)").stdout
            state["heads"] = {path: path.read_bytes() for directory in (admin, *linked)
                              for path in (directory / "HEAD", directory / "ORIG_HEAD", directory / "gitdir")
                              if path.is_file()}
            state["reflogs"] = self.files(*(directory / "logs" for directory in (admin, *linked)))
        if not effect.worktree:
            worktrees = [Path(os.fsdecode((path / "gitdir").read_bytes().removesuffix(b"\n"))).parent for path in linked]
            state["files"] = self.files(self.repo, *worktrees, skip=(".git", ".harness"))
        return state

    def test_the_run_obeys_each_git_effect_across_two_step_checkout_archive_and_acceptance(self):
        self.assert_lifecycle_effects()

    def test_the_run_suppresses_builtin_fsmonitor_across_two_step_checkouts(self):
        with self.markers.included.open("a") as config:
            config.write("[core]\n\tfsmonitor = true\n")
        self.assert_lifecycle_effects(builtin_fsmonitor=True)

    def assert_lifecycle_effects(self, *, builtin_fsmonitor=False):
        # A plain checkout proves the hooks in both config sources are live on this Git.
        if configured_hooks_exist():
            control = self.scratch / "control"
            plain_git(self.repo, "-c", "core.fsmonitor=false", "worktree", "add", "-b", "control", str(control), "HEAD")
            labels = {label for label, _ in self.markers.read()}
            for scope in ("repo", "glob"):
                for event in ("post-checkout", "reference-transaction"):
                    self.assertIn(f"config:{scope}-{event}", labels)
            plain_git(self.repo, "-c", "core.fsmonitor=false", "worktree", "remove", str(control))
        self.markers.reset()

        def decide(script, trigger, packet):
            if trigger["kind"] == "task" and blocked_by_task(trigger["feedback"]):
                return revise(selected="p", instruction="prepare first", new_tasks=[task("p", paths=["a.txt"])],
                              updates=[describe("a", deps=["p"])])
            return None

        def work(script, tid, n, kwargs, result):
            if tid == "a" and n == 1:
                (kwargs["cwd"] / "test_hello.py").write_text("# discarded tracked change\n")
                block_first("a")(script, tid, n, kwargs, result)
            elif tid == "p":
                self.assertFalse((kwargs["cwd"] / "a.txt").exists())
                (kwargs["cwd"] / "p.txt").rename(kwargs["cwd"] / "a.txt")
                result["evidence"] = ["a.txt"]

        launch = git._launch
        calls = []
        trace = self.scratch / "lifecycle-git.trace"
        populate = ("reset", "-q", "--hard", "--no-recurse-submodules")

        def observe(argv, env, *, effect, **kwargs):
            self.assertIsNotNone(effect, argv)
            first = next(i for i in range(3, len(argv)) if not argv[i].startswith("-") and argv[i - 1] != "-c")
            command = tuple(argv[first:])
            path = Path(argv[2]).resolve()
            if builtin_fsmonitor and command == populate:
                self.addCleanup(plain_git, path, "fsmonitor--daemon", "stop", check=False)
            marker_count = len(self.markers.read())
            before = self.preserved_state(effect)
            trace.write_text("")
            result = launch(argv, dict(env, GIT_TRACE=str(trace)), effect=effect, **kwargs)
            self.assert_no_fsmonitor_daemon_spawn(trace, f"{command!r} in {path}")
            after = self.preserved_state(effect)
            new_marks = {label.split(":")[0] for label, _ in self.markers.read()[marker_count:]}
            self.assertLessEqual(new_marks, {"clean", "smudge"} if effect.filters else set(), (command, effect))
            for field in before.keys() | after.keys():
                self.assertEqual(before.get(field), after.get(field), (command, effect.name, field))
            calls.append((command, effect, path, new_marks))
            return result

        engine = self.engine(Script(decide, work=work))
        # Name-based suppression must cover the whole run, including on Git with per-event switches.
        # The test owns the fresh checkout's root even if runtime reclamation fails.
        with patch.object(git, "_launch", observe), patch.object(git, "HOOK_EVENTS", ()), \
                patch.object(tempfile, "tempdir", str(self.scratch)):
            result = engine.start("Archive a rejected candidate, then accept changes",
                                  {"tasks": [task("a", paths=["a.txt", "test_hello.py"])]})
        self.assertEqual(result["status"], "completed")
        artifact, = engine.directory.glob("verification/final/*/verification-*/checkout.json")
        checkout = Path(read_json(artifact)["worktree"]).resolve()
        self.assertTrue(checkout.is_relative_to(self.scratch.resolve()), checkout)
        self.assertIn("revision_edits_archived", self.events(engine))
        archive = engine.directory / "recovery" / "revision-1" / "files"
        self.assertEqual((archive / "test_hello.py").read_text(), "# discarded tracked change\n")
        self.assertEqual((archive / "a.txt").read_text(), "a attempt 1\n")
        worktree = Path(result["worktree"]).resolve()
        self.assertEqual((worktree / "a.txt").read_text(), "a attempt 2\n")
        self.assertEqual((worktree / "test_hello.py").read_bytes(), (self.repo / "test_hello.py").read_bytes())
        self.assertEqual(set().union(*(marks for _, _, _, marks in calls)), {"clean", "smudge"})
        self.assertEqual(self.markers.kinds(), {"clean", "smudge"}, self.markers.read())
        # Check that all relevant phases actually ran, in order and in the expected Git context.
        registered = [(i, path, Path(cmd[-2]).resolve()) for i, (cmd, effect, path, _) in enumerate(calls)
                      if cmd[:3] == ("worktree", "add", "--no-checkout") and effect is git.CHECKOUT]
        populated = [(i, path) for i, (cmd, effect, path, _) in enumerate(calls)
                     if cmd == populate and effect is git.CHECKOUT]
        self.assertEqual([(source, target) for _, source, target in registered],
                         [(self.repo.resolve(), worktree), (worktree, checkout)])
        self.assertEqual([path for _, path in populated], [worktree, checkout])
        reset = next(i for i, (cmd, effect, path, _) in enumerate(calls)
                     if cmd[:2] == ("reset", "--hard") and effect is git.CHECKOUT and path == worktree)
        accepted = next(i for i, (cmd, effect, path, _) in enumerate(calls)
                        if cmd[0] == "update-ref" and effect is git.REFS and path == worktree)
        phases = [registered[0][0], populated[0][0], reset, accepted, registered[1][0], populated[1][0]]
        self.assertEqual(sorted(set(phases)), phases)

    def test_acceptance_disables_automatic_maintenance_with_a_traced_positive_control(self):
        launch = git._launch
        repo = self.scratch / "maintenance-acceptance"
        binding = acceptance_checkout(self.repo, repo)
        path = repo / "accepted.txt"
        path.write_text("candidate content\n")
        trace = self.scratch / "maintenance-accept.log"

        def trace_acceptance(argv, env, *, effect, **kwargs):
            if "commit-tree" in argv or "update-ref" in argv or "--reset" in argv:
                for setting in ("maintenance.auto=false", "gc.auto=0"):
                    self.assertEqual(argv[argv.index(setting) - 1], "-c")
                env = dict(env, GIT_TRACE=str(trace))
            return launch(argv, env, effect=effect, **kwargs)

        before = git.head(repo)
        with patch.object(git, "_launch", trace_acceptance):
            commit = accept_now(repo, [path.name], "fixture", "task", {"subject": "Accept", "body": ""}, binding=binding)
        self.assertNotEqual(commit, before)
        lines = trace.read_text().splitlines()
        for command in ("commit-tree", "update-ref", "read-tree"):
            self.assertTrue(any(f"built-in: git {command}" in line for line in lines), lines)
        self.assertFalse(any("run_command:" in line and "maintenance run --auto" in line for line in lines), lines)

        # git commit invokes automatic maintenance and provides a positive control
        # even on versions whose plumbing never attempts to launch maintenance.
        for enabled in (False, True):
            with self.subTest(automatic_maintenance=enabled):
                control = self.scratch / f"maintenance-control-{enabled}.log"
                path.write_text(f"control content {enabled}\n")
                plain_git(repo, "add", path.name)
                deadline = time.monotonic() + 60
                argv, env = git.invocation(repo, ("commit", "-m", "Maintenance control"),
                                           effect=git.INDEX_REFS, deadline=deadline)
                if enabled:
                    for setting in ("maintenance.auto=false", "gc.auto=0"):
                        index = argv.index(setting)
                        del argv[index - 1:index + 1]
                result = launch(argv, dict(env, GIT_TRACE=str(control)), effect=git.INDEX_REFS,
                                input=None, deadline=deadline)
                self.assertEqual(result.returncode, 0, result)
                lines = control.read_text().splitlines()
                self.assertTrue(any("built-in: git commit" in line for line in lines), lines)
                self.assertEqual(any("run_command:" in line and "maintenance run --auto" in line for line in lines),
                                 enabled, lines)


class AcceptanceGitFormsTests(IsolatedRepoCase):
    """Design 1 §3.6, as called through the Git layer (design 6 case 11)."""

    def setUp(self):
        super().setUp()
        self.worktree = self.scratch / "candidate"
        self.ref = "refs/heads/harness/run"
        self.parent = plain_git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()
        plain_git(self.repo, "worktree", "add", "-b", self.ref.removeprefix("refs/heads/"), str(self.worktree))
        self.binding = git.capture_worktree_binding(self.worktree, self.ref.removeprefix("refs/heads/"))
        self.common = Path(self.binding["common_dir"])
        self.branch_log = self.common / "logs" / self.ref
        self.head_log = Path(self.binding["git_dir"]) / "logs" / "HEAD"
        self.other = self.scratch / "other"
        plain_git(self.repo, "worktree", "add", "-b", "other", str(self.other), self.parent)
        self.other_admin = self.common / "worktrees" / self.other.name
        self.private_index = self.scratch / "private.index"
        self.literal = "new[1].txt"
        (self.worktree / self.literal).write_bytes(b"candidate\r\n")
        (self.worktree / "new1.txt").write_bytes(b"glob decoy\n")
        (self.worktree / "real-only").write_bytes(b"real index sentinel\n")
        plain_git(self.worktree, "add", "--", "real-only")
        (self.worktree / "__pycache__").mkdir()
        (self.worktree / "__pycache__" / "ignored").write_bytes(b"ignored file\n")
        self.alias = "refs/heads/alias"
        plain_git(self.worktree, "symbolic-ref", self.alias, self.ref)
        plain_git(self.repo, "pack-refs", "--all", "--no-prune")
        self.assertIn(f"{self.parent} refs/heads/main".encode(),
                      (self.common / "packed-refs").read_bytes().splitlines())
        for linked in (self.worktree, self.other):
            for ref in ("refs/worktree/independent", "refs/bisect/sentinel"):
                plain_git(linked, "update-ref", "--create-reflog", ref, self.parent)
        self.markers = Markers(self.scratch)
        self.markers.arm(self.repo, scopes=("repo",))
        with (self.common / "info" / "attributes").open("a") as attributes:
            attributes.write("*.txt text eol=lf\n")
        with self.markers.included.open("a") as config:
            config.write("[author]\n\tname = Other Author\n\temail = other@author.invalid\n"
                         "[committer]\n\tname = Other Committer\n\temail = other@committer.invalid\n"
                         "[i18n]\n\tcommitEncoding = ISO-8859-1\n")

    def objects(self):
        return set(plain_git(self.repo, "cat-file", "--batch-all-objects",
                             "--batch-check=%(objectname)").stdout.decode().splitlines())

    def run_form(self, row, *args, effect=git.READ, private=False, input=None, literal=False, code=0,
                 objects_written=0, ref_moved=False, index_written=False, clean_required=False,
                 worktree=None, binding=None):
        worktree = self.worktree if worktree is None else worktree
        binding = self.binding if binding is None else binding
        private_index = self.private_index if private else None
        admin = Path(binding["git_dir"])
        real_index = admin / "index"

        def preserved():
            # Moving the checked-out branch appends both reflogs (design 1 §9).
            moved = (self.common / self.ref, self.branch_log, self.head_log)
            omitted = {str(path) for path in moved} if ref_moved else set()
            admins = dict.fromkeys((self.common, Path(self.binding["git_dir"]), self.other_admin, admin))
            refs = self.files(self.common / "packed-refs",
                              *(directory / name for directory in admins
                                for name in ("refs", "logs", "HEAD", "ORIG_HEAD")))
            # A failed CAS may briefly create a lock file, changing the containing directory's mtime.
            return ({path: value[:3] if stat.S_ISDIR(value[0]) else value
                     for path, value in refs.items() if path not in omitted},
                    self.files(self.repo, self.other, worktree, skip=(".git", ".harness")),
                    self.files(self.common / "index", self.other_admin / "index"),
                    {} if index_written else self.files(real_index),
                    {} if private and effect is git.PRIVATE else self.files(self.private_index))

        with self.subTest(row=row, argv=args):
            before = preserved()
            old_objects = self.objects()
            self.markers.reset()
            launch = git._launch
            seen = []

            def observe(argv, env, *, effect, **kwargs):
                result = launch(argv, env, effect=effect, **kwargs)
                seen.append((effect, argv, env, result, kwargs.get("pass_fds", ())))
                return result

            with patch.object(git, "_launch", observe):
                if row == "G10":  # The binding check itself uses _run_git to avoid recursion.
                    git.check_worktree_binding(worktree, binding)
                    result, = (record[3] for record in seen)
                elif row in ("G16", "G17"):
                    with (self.scratch / "matrix-run.lock").open("a+b") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fd = lock.fileno()
                        if row == "G16":
                            ref, commit, parent = args[-3:]
                            result = git.move_branch(worktree, ref, commit, parent, "task", binding=binding, lock_fd=fd)
                        else:
                            result = git.sync_index(worktree, args[-1], binding=binding, lock_fd=fd)
                    self.assertEqual([record[4] for record in seen if record[4]], [(fd,)])
                else:
                    result = git.git_process(worktree, *args, effect=effect, binding=binding,
                                             index_file=private_index, input=input, literal_pathspecs=literal)
            (launched_effect, argv, env, _, passed), = (record for record in seen if record[3] is result)
            self.assertEqual(tuple(argv[-len(args):]), args)
            self.assertIn("--git-dir=" + binding["git_dir"], argv)
            self.assertIn("--work-tree=" + binding["worktree"], argv)
            self.assertEqual(env.get("GIT_COMMON_DIR"), binding["common_dir"])
            self.assertEqual(env.get("GIT_INDEX_FILE"), str(private_index) if private else None)
            self.assertEqual("--literal-pathspecs" in argv[:-len(args)], literal)
            self.assertIs(launched_effect, effect)
            self.assertEqual(passed, (fd,) if row in ("G16", "G17") else ())
            self.assertEqual(launched_effect.interruptible, row not in ("G16", "G17"))
            if code is None:
                self.assertNotEqual(result.returncode, 0, result.stderr)
            else:
                self.assertEqual(result.returncode, code, result.stderr)
            after = preserved()
            for key in before[0].keys() | after[0].keys():
                self.assertEqual(before[0].get(key), after[0].get(key), key)
            self.assertEqual(before[1:], after[1:])
            after_objects = self.objects()
            self.assertLessEqual(old_objects, after_objects)
            self.assertEqual(len(after_objects - old_objects), objects_written)
            marks = self.markers.read()
            clean = ("clean", str(worktree.resolve()))
            # Git can recheck stat data and run clean during write-tree and read-tree --reset.
            allowed = {clean} if row in ("G4", "G5", "G17", "G21") and effect.filters else set()
            self.assertLessEqual(set(marks), allowed, marks)
            if clean_required:
                self.assertIn(clean, marks)
        return result.stdout

    def test_acceptance_and_completion_git_forms(self):
        run = self.run_form
        parent = self.parent
        # Check that plain Git reaches the installed hook and signing programs.
        plain_git(self.worktree, "update-ref", "refs/heads/control", parent)
        control_env = dict(os.environ, GIT_INDEX_FILE=str(self.scratch / "control.index"))
        plain_git(self.worktree, "-c", "core.fsmonitor=false", "read-tree", parent, env=control_env)
        labels = {label for label, _ in self.markers.read()}
        for event in ("reference-transaction", "post-index-change"):
            self.assertIn(f"hookdir:{event}", labels)
            if configured_hooks_exist():
                self.assertIn(f"config:repo-{event}", labels)
        signed = plain_git(self.worktree, *IDENTITY, "commit-tree", "-S", f"{parent}^{{tree}}",
                           "-m", "signing control", check=False)
        self.assertNotEqual(signed.returncode, 0, signed.stderr)
        self.assertIn("gpg", self.markers.kinds())
        parent_tree = run("G7", "rev-parse", "--verify", f"{parent}^{{tree}}").decode().strip()
        real_before = run("G15", "ls-files", "--stage", "-z")
        self.assertIn(b"\treal-only\0", real_before)

        run("G1", "read-tree", parent, private=True, effect=git.PRIVATE)
        self.assertTrue(self.private_index.is_file())
        self.assertEqual(run("G2", "ls-files", "-z", private=True), b".gitignore\0test_hello.py\0")
        run("G3", "update-index", "-z", "--force-remove", "--stdin", private=True, effect=git.PRIVATE,
            input=b"test_hello.py\0")
        self.assertTrue((self.worktree / "test_hello.py").is_file())
        self.assertEqual(run("G2", "ls-files", "-z", private=True), b".gitignore\0")
        run("G4", "add", "--", self.literal, private=True, effect=git.PRIVATE, literal=True,
            objects_written=1, clean_required=True)
        staged = run("G6", "ls-files", "--stage", "-z", private=True)
        staged_names = [entry.split(b"\t", 1)[1] for entry in staged.split(b"\0")[:-1]]
        self.assertEqual(staged_names, [b".gitignore", os.fsencode(self.literal)])
        blob = staged.split(b"\0")[1].split()[1].decode()
        self.assertEqual(plain_git(self.repo, "cat-file", "blob", blob).stdout, b"candidate\n")
        tree = run("G5", "write-tree", private=True, effect=git.PRIVATE, objects_written=1).decode().strip()
        self.assertNotEqual(tree, parent_tree)
        raw = run("G8", "diff-tree", "-r", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", parent, tree)
        added, literal_name, removed, removed_name, end = raw.split(b"\0")
        self.assertEqual((literal_name, removed_name, end), (os.fsencode(self.literal), b"test_hello.py", b""))
        self.assertEqual((added.split()[-1], removed.split()[-1]), (b"A", b"D"))

        message = "Accept café\n\n# retained\n  indented  \n\n".encode()
        commit = run("G9", "commit-tree", tree, "-p", parent, "-F", "-", effect=git.PRIVATE,
                     input=message, objects_written=1).decode().strip()
        self.assertEqual(run("G7", "rev-parse", "--verify", f"{commit}^{{tree}}").decode().strip(), tree)
        self.assertEqual(run("G10", "symbolic-ref", "-q", "HEAD"), self.ref.encode() + b"\n")
        self.assertEqual(run("G11", "symbolic-ref", "-q", self.alias), self.ref.encode() + b"\n")
        self.assertEqual(run("G11", "symbolic-ref", "-q", self.ref, code=1), b"")
        self.assertEqual(run("G12", "rev-parse", "--verify", "-q", self.ref), parent.encode() + b"\n")
        self.assertEqual(run("G12", "rev-parse", "--verify", "-q", "refs/heads/absent", code=1), b"")
        missing = "0" * len(parent)
        kinds = run("G13", "cat-file", "--batch-check", input=f"{parent}\n{commit}\n{tree}\n{missing}\n".encode())
        self.assertEqual([line.decode().split()[:2] for line in kinds.splitlines()],
                         [[parent, "commit"], [commit, "commit"], [tree, "tree"], [missing, "missing"]])
        original = run("G14", "cat-file", "commit", parent)
        self.assertEqual(original.splitlines()[0], f"tree {parent_tree}".encode())
        raw_commit = run("G14", "cat-file", "commit", commit)
        headers, body = raw_commit.split(b"\n\n", 1)
        tree_header, parent_header, author, committer = headers.splitlines()
        self.assertEqual((tree_header, parent_header), (f"tree {tree}".encode(), f"parent {parent}".encode()))
        self.assertEqual(author.rsplit(b" ", 2)[0], b"author Personal Harness <harness@localhost>")
        self.assertEqual(committer.rsplit(b" ", 2)[0], b"committer Personal Harness <harness@localhost>")
        self.assertEqual(body, message)
        run("G25", "cat-file", "tree", tree)

        self.assertEqual(run("G18", "ls-files", "--stage", "-z"), real_before)
        self.assertEqual(run("G18", "rev-parse", "HEAD"), parent.encode() + b"\n")
        listing = run("G18", "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        self.assertCountEqual(listing.split(b"\0"), [b".gitignore", b"real-only", b"test_hello.py",
                                                     os.fsencode(self.literal), b"new1.txt", b""])
        entries = run("G19", "ls-tree", "-r", "-z", commit)
        self.assertEqual([entry.split(b"\t", 1)[1] for entry in entries.split(b"\0")[:-1]], staged_names)
        self.assertIn(b"100644 blob " + blob.encode() + b"\t" + os.fsencode(self.literal) + b"\0", entries)
        commit_entries = plain_git(self.worktree, "ls-tree", "-r", "-z", commit).stdout
        expected_stage = b""
        for entry in commit_entries.split(b"\0")[:-1]:
            header, path = entry.split(b"\t", 1)
            mode, kind, oid = header.split()
            self.assertEqual(kind, b"blob")
            expected_stage += mode + b" " + oid + b" 0\t" + path + b"\0"

        private_env = dict(os.environ, GIT_INDEX_FILE=str(self.private_index))
        plain_git(self.worktree, "-c", "core.fsmonitor=false", "-c", "core.splitIndex=false",
                  "read-tree", parent, env=private_env)
        parent_stage = plain_git(self.worktree, "ls-files", "--stage", "-z", env=private_env).stdout
        self.assertIn(b"\ttest_hello.py\0", parent_stage)
        self.assertNotEqual(parent_stage, expected_stage)
        self.assertEqual(plain_git(self.worktree, "ls-files", "--stage", "-z").stdout, real_before)
        run("G20", "read-tree", commit, private=True, effect=git.PRIVATE)
        self.assertEqual(plain_git(self.worktree, "ls-files", "--stage", "-z", env=private_env).stdout,
                         expected_stage)
        self.assertEqual(run("G6", "ls-files", "--stage", "-z", private=True), staged)
        lock = self.private_index.with_name(self.private_index.name + ".lock")
        lock.touch()
        run("G21", "add", "-A", private=True, effect=git.PRIVATE, code=None)
        self.assertTrue(lock.is_file())
        lock.unlink()
        self.assertEqual(run("G6", "ls-files", "--stage", "-z", private=True), staged)
        run("G21", "add", "-A", private=True, effect=git.PRIVATE, objects_written=1, clean_required=True)
        alternate_tree = run("G5", "write-tree", private=True, effect=git.PRIVATE, objects_written=1).decode().strip()
        self.assertEqual(run("G22", "diff-tree", "-r", "--name-only", "--no-renames", "-z", tree, alternate_tree),
                         b"new1.txt\0real-only\0test_hello.py\0")
        comparison = run("G23", "diff-tree", "-r", "-p", "--binary", "--no-ext-diff", "--no-textconv",
                         "--no-renames", parent, commit, "--")
        self.assertIn(b"+candidate\n", comparison)
        self.assertIn(b"deleted file mode", comparison)
        self.assertEqual(run("G24", "diff-tree", "-r", "--no-ext-diff", "--no-textconv", "--name-only",
                             "--no-renames", "-z", parent, commit), os.fsencode(self.literal) + b"\0test_hello.py\0")

        args = ("update-ref", "--no-deref", "-m", "harness accept task", self.ref, commit)
        run("G16", *args, commit, effect=git.REFS, code=None)  # This old value is wrong.
        branch_before = self.branch_log.read_bytes()
        head_before = self.head_log.read_bytes()
        run("G16", *args, parent, effect=git.REFS, ref_moved=True)
        self.assertEqual(run("G12", "rev-parse", "--verify", "-q", self.ref), commit.encode() + b"\n")
        branch_after = self.branch_log.read_bytes()
        self.assertTrue(branch_after.startswith(branch_before))
        entry, = branch_after[len(branch_before):].splitlines(keepends=True)
        self.assertTrue(entry.startswith(f"{parent} {commit} ".encode()), entry)
        self.assertTrue(entry.endswith(b"\tharness accept task\n"), entry)
        # When Git also logs the movement via HEAD, only this bound worktree's log can get the same entry.
        self.assertIn(self.head_log.read_bytes(), (head_before, head_before + entry))
        self.assertEqual(run("G15", "ls-files", "--stage", "-z"), real_before)
        run("G17", "read-tree", "--reset", commit, effect=git.INDEX, index_written=True)
        self.assertEqual(run("G15", "ls-files", "--stage", "-z"), staged)

    def test_symbolic_ref_binding_check_accepts_detached_head_status_one(self):
        detached = self.scratch / "detached"
        plain_git(self.repo, *IDENTITY, "worktree", "add", "--detach", str(detached), self.parent)
        binding = git.capture_worktree_binding(detached, None)
        self.assertEqual(self.run_form("G10", "symbolic-ref", "-q", "HEAD", code=1,
                                       worktree=detached, binding=binding), b"")

    def test_hash_object_without_write_leaves_the_object_store_untouched(self):
        # Design 6 case 11 independently of the final G table.
        payload = b"contents that were never added\n"
        oid = self.run_form("case 11", "hash-object", "--stdin", effect=git.COMPARE, input=payload)
        expected = plain_git(self.worktree, "hash-object", "--stdin", input=payload).stdout
        self.assertEqual(oid, expected)
        self.assertNotIn(oid.decode().strip(), self.objects())


class GitSupervisionTests(IsolatedRepoCase):
    """Run sleeping Git stand-ins in private groups, with real signal delivery."""

    @contextlib.contextmanager
    def interpose(self, suffix, program, *, after_spawn=None, only_linked=False):
        spawn = subprocess.Popen
        processes = []

        def start(argv, **kwargs):
            if argv[0] != "git" or argv[-len(suffix):] != suffix:
                return spawn(argv, **kwargs)
            if only_linked and not any(arg.startswith("--work-tree=") for arg in argv):
                return spawn(argv, **kwargs)
            proc = spawn([sys.executable, "-c", program, *argv], **kwargs)
            processes.append(proc)
            if after_spawn is not None:
                after_spawn(proc)
            return proc

        with patch.object(git.subprocess, "Popen", side_effect=start):
            yield processes

    def assert_reaped(self, processes, code):
        self.assertEqual(len(processes), 1)
        process, = processes
        self.assertEqual(process.returncode, code)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        with self.assertRaises(ProcessLookupError):
            os.killpg(process.pid, 0)

    def test_interrupt_during_snapshot_preserves_run_and_reaps_reader(self):
        engine = self.engine()
        program = "import os, signal, time; os.kill(os.getppid(), signal.SIGINT); time.sleep(30)"
        with patch.object(interrupts, "ENABLED", True), interrupts.installed(), self.interpose(
                ["ls-files", "--stage", "-z"], program, only_linked=True) as processes:
            start = time.monotonic()
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet")
            self.assertLess(time.monotonic() - start, 2)
            self.assertEqual(interrupts.active().delivered_from, "_launch")
        self.assert_reaped(processes, -signal.SIGTERM)
        state = read_json(engine.directory / "state.json")
        self.assertEqual(state["status"], "interrupted")
        self.assertFalse(any(dispatch["invoked"] for dispatch in state["dispatches"]))
        self.assertFalse(list((self.repo / ".git").rglob("index.lock")))

    def test_interrupt_during_population_saves_checkout_and_binding(self):
        engine = self.engine()
        program = ("import os, signal, sys, time; os.kill(os.getppid(), signal.SIGINT); "
                   "time.sleep(.35); os.execvp(sys.argv[1], sys.argv[1:])")
        with patch.object(interrupts, "ENABLED", True), interrupts.installed(), self.interpose(
                ["reset", "-q", "--hard", "--no-recurse-submodules"], program) as processes:
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet")
            self.assertNotEqual(interrupts.active().delivered_from, "_launch")
        self.assert_reaped(processes, 0)
        state = read_json(engine.directory / "state.json")
        checkout = Path(state["worktree"])
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["worktree_binding"], git.capture_worktree_binding(checkout, state["branch"]))
        self.assertTrue((checkout / "test_hello.py").is_file())
        self.assertEqual(state["dispatches"], [])

    def test_write_and_undeclared_effects_finish_before_interrupt(self):
        program = ("import os, signal, sys, time; os.kill(os.getppid(), signal.SIGINT); "
                   "time.sleep(.35); os.execvp(sys.argv[1], sys.argv[1:])")
        for effect, enabled in ((git.REFS, True), (None, True), (git.REFS, False), (None, False)):
            with self.subTest(effect=effect, controller=enabled):
                ref = f"refs/heads/fixture-{effect is None}-{enabled}"
                args = ["update-ref", ref, "HEAD"]
                with patch.object(interrupts, "ENABLED", enabled), interrupts.installed(), self.interpose(
                        args, program) as processes:
                    with self.assertRaises(KeyboardInterrupt):
                        result = git.git_process(self.repo, *args, effect=effect)
                        self.assertTrue(enabled)
                        self.assertEqual(result.returncode, 0)
                        interrupts.deliver()
                self.assert_reaped(processes, 0)
                self.assertEqual(git.git(self.repo, "rev-parse", ref, effect=git.READ),
                                 git.git(self.repo, "rev-parse", "HEAD", effect=git.READ))

    def test_reaped_leader_with_inherited_output_pipe_never_signals_group(self):
        args = ["rev-parse", "HEAD"]
        descendant = self.scratch / "descendant.pid"
        program = (
            "import os, pathlib, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    time.sleep(10)\n"
            "    os._exit(0)\n"
            f"pathlib.Path({str(descendant)!r}).write_text(str(pid))\n"
        )

        def leader_exited(process):
            self.assertEqual(process.wait(timeout=3), 0)
            signal.raise_signal(signal.SIGINT)

        with patch.object(interrupts, "ENABLED", True), interrupts.installed(), self.interpose(
                args, program, after_spawn=leader_exited) as processes:
            try:
                with patch.object(git.os, "killpg", wraps=os.killpg) as killpg, self.assertRaises(KeyboardInterrupt):
                    start = time.monotonic()
                    git.git_process(self.repo, *args, effect=git.READ, timeout=3)
                self.assertLess(time.monotonic() - start, 2)
                killpg.assert_not_called()
                self.assertEqual(processes[0].returncode, 0)
                self.assertTrue(processes[0].stdout.closed)
                self.assertTrue(processes[0].stderr.closed)
                os.kill(int(descendant.read_text()), 0)
            finally:
                if descendant.exists():
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(int(descendant.read_text()), signal.SIGTERM)

    def test_fully_drained_exit_precedes_pending_interrupt(self):
        args = ["rev-parse", "HEAD"]
        program = "import sys; sys.stdout.buffer.write(b'complete\\x00\\xff')"

        def finished_at_slice_boundary(process):
            self.assertEqual(process.wait(timeout=3), 0)
            communicate = process.communicate

            def first_slice_times_out(**kwargs):
                process.communicate = communicate
                signal.raise_signal(signal.SIGINT)
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])

            process.communicate = first_slice_times_out

        with patch.object(interrupts, "ENABLED", True), interrupts.installed(), self.interpose(
                args, program, after_spawn=finished_at_slice_boundary) as processes:
            result = git.git_process(self.repo, *args, effect=git.READ)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"complete\x00\xff", b""))
            self.assertIsNone(interrupts.active().delivered_from)
            with self.assertRaises(KeyboardInterrupt):
                interrupts.deliver()
        self.assert_reaped(processes, 0)

    def test_timeout_preserves_partial_bytes_and_kills_child(self):
        args = ["rev-parse", "HEAD"]
        program = ("import os, time; os.write(1, b'out\\x00\\xff'); os.write(2, b'err\\xff'); time.sleep(30)")
        with self.interpose(args, program) as processes, self.assertRaises(subprocess.TimeoutExpired) as timed_out:
            git.git_process(self.repo, *args, effect=git.READ, timeout=.5)
        self.assert_reaped(processes, -signal.SIGKILL)
        self.assertEqual(timed_out.exception.output, b"out\x00\xff")
        self.assertEqual(timed_out.exception.stderr, b"err\xff")
        self.assertGreater(timed_out.exception.timeout, .4)

    def test_io_failure_preserves_original_exception_and_reaps_child(self):
        args = ["rev-parse", "HEAD"]
        failure = OSError("fixture pipe error")

        def communication_failure(process):
            def fail(**kwargs):
                raise failure
            process.communicate = fail

        with self.interpose(args, "import time; time.sleep(30)", after_spawn=communication_failure) as processes:
            with self.assertRaises(OSError) as raised:
                git.git_process(self.repo, *args, effect=git.READ)
        self.assertIs(raised.exception, failure)
        self.assert_reaped(processes, -signal.SIGKILL)

    def test_input_and_output_are_lossless_across_slices(self):
        args = ["hash-object", "--stdin"]
        payload = b"line\x00\xff" * (1024 * 1024)
        expected = plain_git(self.repo, *args, input=payload).stdout
        program = ("import os, sys, time; time.sleep(.22); os.write(2, b'\\xff' * (1024 * 1024)); "
                   "os.execvp(sys.argv[1], sys.argv[1:])")
        with self.interpose(args, program) as processes:
            result = git.git_process(self.repo, *args, input=payload, effect=git.READ)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, expected, b"\xff" * (1024 * 1024)))
        self.assert_reaped(processes, 0)


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
        real, started = git._launch, []

        def fake(argv, env, *, deadline, **kwargs):
            remaining = deadline - self.now
            if remaining <= 0:
                return real(argv, env, deadline=deadline, **kwargs)
            started.append((list(argv), remaining))
            if any(word in argv for word in passthrough):
                result = real(argv, env, deadline=deadline, **kwargs)
                self.now += check_spend
                return result
            if "--get-regexp" in argv:
                if read_raises is not None:
                    raise read_raises
                self.now += spend
                return subprocess.CompletedProcess(argv, 1, b"", b"")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        with patch.object(git, "_launch", side_effect=fake):
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


def call_shape_problems(sources, *, reads=None):
    """R1 over {module: source} of harness/, optionally collecting the READ declarations for R2."""
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

        def nodes(parent, scope, owner=None):
            """Every node with its enclosing scope and that scope's Python qualified name."""
            for child in ast.iter_child_nodes(parent):
                yield child, scope, owner
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    name = "<lambda>" if isinstance(child, ast.Lambda) else child.name
                    if owner:
                        separator = "." if isinstance(scope, ast.ClassDef) else ".<locals>."
                        name = f"{owner}{separator}{name}"
                    yield from nodes(child, child, name)
                else:
                    yield from nodes(child, scope, owner)
        for node, local, target in imports:
            if target in RUNNERS or target in RUNNER_MODULES and local != target.rsplit(".", 1)[1]:
                problems.append((f"harness/{module}.py:{node.lineno}", "imports a Git runner under another name"))
        tops = {node: node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        called = set()
        for node, scope, owner in nodes(tree, None):
            place = f"harness/{module}.py:{getattr(node, 'lineno', 0)}"
            function = tops.get(scope)  # forwards and _launch are top-level functions, never nested ones
            if not isinstance(node, ast.Call):
                continue
            effect = next((keyword.value for keyword in node.keywords if keyword.arg == "effect"), None)
            if reads is not None and qualified(effect) == "harness.git.READ":
                reads[module, owner] += 1
            name = RUNNERS.get(qualified(node.func))
            if name is not None:
                checked[module] += 1
                called.add(node.func)
                problems += [(place, what) for what in config_problems(function, scope, node, name)]
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
                if subcommand == "worktree" and len(words) > 1:
                    action = word(words[1])
                    if action == "add":
                        if (module, function) != ("git", "add_worktree"):
                            problems.append((place, "runs worktree add outside harness.git.add_worktree"))
                        if "--no-checkout" not in literals:
                            problems.append((place, "runs worktree add without --no-checkout"))
                    if action == "remove" and "--force" in literals and (module, function) != ("git", "add_worktree"):
                        problems.append((place, "forces worktree removal outside harness.git.add_worktree"))
                if subcommand == "init":
                    if "--template=" not in literals:
                        problems.append((place, "runs init without --template="))
                    isolated = next((k.value for k in node.keywords if k.arg == "isolated_config"), None)
                    if not isinstance(isolated, ast.Constant) or isolated.value is not True:
                        problems.append((place, "runs init without isolated_config=True"))
            starter = qualified(node.func) or ""
            if starter in STARTERS or starter.startswith(("os.exec", "os.posix_spawn")):
                if module == "git":
                    if (function, starter) != ("_launch", "subprocess.Popen"):
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

    def test_strictly_read_only_git_calls_are_all_registered(self):
        sources = {path.stem: path.read_text(encoding="utf-8") for path in sorted((ROOT / "harness").glob("*.py"))}
        reads = collections.Counter()
        call_shape_problems(sources, reads=reads)
        registered = collections.Counter({site: declarations for site, (declarations, _) in READ_SITES.items()})
        self.assertEqual((reads - registered, registered - reads), (collections.Counter(), collections.Counter()),
                         "Unregistered READ declarations and registrations without a declaration")

    def test_the_read_site_tripwire_finds_new_callers_and_the_hook_name_read(self):
        sources = {
            "git": '''
def _configured_hooks(repo):
    _launch([], {}, effect=READ)
def head(repo):
    def nested():
        git(repo, "rev-parse", "HEAD", effect=READ)
    if repo is None:
        return git(repo, "rev-parse", "--show-toplevel", effect=READ)
    return git(repo, "rev-parse", "HEAD", effect=READ)
''',
            "memory": '''
from .git import READ as READ_EFFECT
from . import git
class Other:
    def _staleness(self, repo):
        return git.git(repo, "rev-parse", "HEAD", effect=READ_EFFECT)
''',
        }
        reads = collections.Counter()
        problems, _ = call_shape_problems(sources, reads=reads)
        self.assertEqual(problems, [])
        registered = collections.Counter({site: declarations for site, (declarations, _) in READ_SITES.items()})
        self.assertEqual(reads[("git", "_configured_hooks")], 1)
        self.assertEqual(reads - registered, {("git", "head"): 1, ("git", "head.<locals>.nested"): 1,
                                             ("memory", "Other._staleness"): 1})

    def test_only_two_step_checkout_and_isolated_init_are_allowed(self):
        sources = {
            "git": ("def add_worktree(repo, path):\n"
                    "    git(repo, 'worktree', 'add', '--no-checkout', path, effect=CHECKOUT)\n"
                    "    git(repo, 'worktree', 'remove', '--force', path, effect=CHECKOUT)\n"
                    "    git(repo, 'worktree', 'add', path, effect=CHECKOUT)\n"),
            "other": ("from . import git\n"
                      "def add_worktree(repo, path):\n"
                      "    git.git(repo, 'worktree', 'add', '--no-checkout', path, effect=git.CHECKOUT)\n"
                      "    git._run_git(repo, ('worktree', 'remove', '--force', path), effect=git.CHECKOUT)\n"
                      "    git.git(repo, 'worktree', 'remove', path, effect=git.CHECKOUT)\n"
                      "    git.git(repo, 'init', '--template=', effect=git.CHECKOUT, isolated_config=True)\n"
                      "    git.git(repo, 'init', '--template=', effect=git.CHECKOUT, isolated_config=1)\n"
                      "    git.git(repo, 'init', '--template=somewhere', effect=git.CHECKOUT, isolated_config=True)\n"),
        }
        problems, checked = call_shape_problems(sources)
        self.assertEqual(problems, [
            ("harness/git.py:4", "runs worktree add without --no-checkout"),
            ("harness/other.py:3", "runs worktree add outside harness.git.add_worktree"),
            ("harness/other.py:4", "forces worktree removal outside harness.git.add_worktree"),
            ("harness/other.py:7", "runs init without isolated_config=True"),
            ("harness/other.py:8", "runs init without --template="),
        ])
        self.assertEqual(checked, {"git": 3, "other": 6})

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
