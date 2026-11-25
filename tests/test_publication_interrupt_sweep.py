"""Deliver real SIGINT at the executable lines of the stage publication paths."""

import ast
from collections import Counter
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import inspect
import json
import os
from pathlib import Path
import selectors
from textwrap import dedent
from types import CodeType, SimpleNamespace
import shutil
import signal
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
from unittest.mock import patch

from harness import cli, dispatch, git, interrupts, stops, task_lifecycle
from harness.decisions import DecisionRecovery
from harness.adapters import CLIBackend
from harness.budgets import AttemptAccounting, work_attempts
from harness.continuation import apply_continuation, prepare_continuation
from harness.dispatch import Dispatcher
from harness.finalization import FinalAcceptance
from harness.planning import Planner
from harness.progress import ProgressRecovery
from harness.quota import ProbeProcess
from harness.quota_wait import QuotaWait
from harness.receipts import ResultReceipts
from harness.recovery import acceptance_review_records
from harness.run_context import RunContext
from harness.run_lifecycle import RunLifecycle
from harness.scheduler import Scheduler
from harness.verification import Verification
from harness.task_lifecycle import TaskLifecycle
from harness.util import HarnessError, read_json, run_process
from harness.worker_receipts import WorkerReceipts
from harness.workflow import Workflow
from test_harness import FakeClock, RepoCase, crash_after_save
import test_decide_recovery as decide_fixtures
import test_quota_wait as quota_fixtures
from test_review_batches import plan
from test_review_transitions import evidence_plan, ledger
from test_runtime_changes import ScriptedBackend, revised, trigger_kind, update

START = RunLifecycle.start.__wrapped__.__code__
START_LOCKED, = (code for code in START.co_consts
                 if isinstance(code, CodeType) and code.co_name == "start_locked")
ABANDON = RunLifecycle.abandon.__wrapped__.__code__
ABANDON_LOCKED, = (code for code in ABANDON.co_consts
                   if isinstance(code, CodeType) and code.co_name == "abandon_locked")
SCHEDULING = Scheduler._execute_until_terminal.__code__
RESUME_LOCKED, = (code for code in RunLifecycle.resume.__wrapped__.__code__.co_consts
                  if isinstance(code, CodeType) and code.co_name == "resume_locked")
PREFLIGHT = {"RunLifecycle.resume.<locals>.resume_locked", ABANDON_LOCKED.co_qualname}
WAIT = {"_launch", "QuotaWait._sleep_until", "ProbeSignals.checkpoint"}
NONWAIT = {
    "Scheduler._execute_until_terminal", "Dispatcher.dispatch_once",
    "TaskLifecycle.continue_task", "TaskLifecycle.finish_post_accept", "FinalAcceptance.final_review",
    "Verification.run_verification_commands", "FinalAcceptance.publish_written_leave", "read_worktree",
}
REAL_GIT = shutil.which("git")
PUBLISH = RunContext.publish.__wrapped__
STOP = inspect.unwrap(Workflow.stop)
FINAL_RECEIPT = "reviewer:final_review:final"

# A single session leader with no descendants. Announce readiness after setting
# SIGTERM and keep the pipes open for Git supervision. Exit if delivery is lost.
READY_GIT = """\
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_DFL)
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
os.write(1, b"R")
time.sleep(3)
raise SystemExit(27)
"""



def source_point(function, source):
    lines, first = inspect.getsourcelines(function)
    offset, = (offset for offset, line in enumerate(lines) if source in line)
    return function.__qualname__, first + offset


def branch_point(function, condition, statement, following):
    lines, first = inspect.getsourcelines(function)
    tree = ast.parse(dedent("".join(lines)))
    branch, = (node for node in ast.walk(tree)
               if isinstance(node, ast.If) and ast.unparse(node.test) == condition
               and any(ast.unparse(item) == following for item in node.body))
    anchor, = (item for item in branch.body if ast.unparse(item) == statement)
    return function.__qualname__, first + anchor.lineno - 1


def final_receipt_point():
    return branch_point(Dispatcher.dispatch_once, "role == 'reviewer'", "self.context.save()",
                        "return self.services.receipts.finish_review_receipt(key, schema)")


def final_receipt_trial(trials):
    source = final_receipt_point()
    matches = [(at, point) for at, point in trials
               if point[:2] == source and point[3][0] == Dispatcher.dispatch.__qualname__
               and point[4:-1] == ("reviewer", "final_review", False, False)]
    if len(matches) != 1:
        raise AssertionError(f"expected one final review receipt event, found {len(matches)}")
    return matches[0]


PUBLICATION_SAVE = source_point(PUBLISH, "self.save(terminal=terminal)")[1]


# Keep the inventory tied to §3.7. Root calls delimit business windows;
# targeted B/C/D fixtures and the fault controls use their own trace targets.
def code_of(function):
    return function if isinstance(function, CodeType) else inspect.unwrap(function).__code__


STAGE_TARGETS = {
    "verification and review preparation": (TaskLifecycle.continue_task,),
    "worker handoff and routing": (TaskLifecycle.publish_worker_result, TaskLifecycle.replay_worker,
                                  Workflow.synchronize_decision_requests, DecisionRecovery.register_decision_request),
    "sealed verdict": (WorkerReceipts._finish,),
    "vetoed blocker": (TaskLifecycle.publish_invalid_blocker,),
    "review invalidation": (acceptance_review_records,),
    "worker receipt consumption": (WorkerReceipts.prepare_consume, WorkerReceipts.apply_consume),
    "worker rejection": (WorkerReceipts.reject,),
    "dispatch registration and accounting": (Dispatcher.dispatch_once, WorkerReceipts.publish_inputs,
                                             WorkerReceipts.prepare_intent),
    "revision": (Planner.land_revision, Planner.apply_pending_revision),
    "initial plan": (Planner.record_plan,),
    "checkpoint registration": (ProgressRecovery._register_checkpoint_batch,),
    "blocked attempt exemption": (AttemptAccounting.prepare_attempt_exemption, AttemptAccounting.apply_attempt_exemption),
    "continuation handoff": (prepare_continuation, apply_continuation),
    "scheduler exit": (Scheduler.execute, Scheduler._execute_until_terminal),
    "stop": (STOP, Workflow._save_stop),
    "abandon": (ABANDON, ABANDON_LOCKED),
}
STAGE_CODES = {code_of(fn) for functions in STAGE_TARGETS.values() for fn in functions}


class StageWindow:
    def __init__(self, roots, *, required=(), landmarks=(), boundaries=()):
        # A root may admit descendants or leave them to a separate window.
        # Matching roots take precedence, allowing several independent windows
        # in one control execution. Use call inputs to distinguish invocations.
        self.roots = {code_of(fn): (matches, follow) for fn, matches, follow in roots}
        self.required = {code_of(fn) for fn in required} | self.roots.keys()
        self.boundaries = {code_of(fn) for fn in boundaries}
        self.landmarks = tuple(source_point(fn, source) for fn, source in landmarks)
        if self.required - STAGE_CODES:
            raise AssertionError("window contains an unregistered stage target")

    def admits(self, frame, active_frames):
        for ancestor in stack_from(frame):
            code = ancestor.f_code
            if code in self.boundaries:
                return False
            if code in self.roots:
                matches, follow = self.roots[code]
                if ancestor is frame:
                    return matches is None or matches(frame.f_locals)
                return follow and ancestor in active_frames
        return False

    def verify(self, test, control):
        test.assertFalse(self.required - control.frames.keys(), "required window frames never entered")
        observed = {event[:2] for event in control.events}
        for point in self.landmarks:
            test.assertIn(point, observed, "required business branch never executed")
        test.assertEqual(control.events, control.witness, "sweep omitted a witnessed line occurrence")


def git_read(path, *argv):
    return subprocess.check_output([REAL_GIT, "--no-optional-locks", "-C", str(path), *argv])


def worktree(run):
    path = Path(run["worktree"])
    head, branch_head, tree = git_read(path, "rev-parse", "HEAD", run["branch"], "HEAD^{tree}").splitlines()
    index = git_read(path, "ls-files", "--stage", "-z")
    names = set(git_read(path, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0"))
    files = {}
    for name in sorted(names - {b""}):
        entry = path / os.fsdecode(name)
        if entry.is_symlink():
            files[name] = "link", os.readlink(entry)
        elif entry.is_file():
            files[name] = entry.stat().st_mode, hashlib.sha256(entry.read_bytes()).digest()
        else:
            files[name] = None
    branch = git_read(path, "symbolic-ref", "HEAD").strip()
    return head, branch_head, branch, tree, index, files


def same_workspace(left, right):
    """Across independently seeded runs, commit IDs and branch names can differ."""
    return (left[0] == left[1], *left[3:]) == (right[0] == right[1], *right[3:])


def location(frame):
    return frame.f_code.co_qualname, frame.f_lineno


def stack_from(frame):
    while frame is not None:
        yield frame
        frame = frame.f_back


def dispatches(run):
    return {entry["id"] for entry in run["dispatches"]
            if entry.get("invoked") or entry["role"] == "verification"}


def lifecycle(run, without=()):
    """Compare review content and tree IDs with independently created runs."""
    def tree(head):
        return git_read(run["repo"], "rev-parse", f"{head}^{{tree}}").strip()

    histories = [*run["review_history"].values(), run["final_history"]]
    snapshots = []
    for history in histories:
        for row in history:
            snapshot_path = row.get("snapshot_artifact")
            if snapshot_path and (row.get("review_source") or row.get("approved_batches")):
                snapshot = read_json(Path(snapshot_path))
                content = snapshot["index"], snapshot["files"], tree(snapshot["head"])
                snapshots.extend(content for item in (*row.get("approved_batches", []), row)
                                 if item.get("review_source"))
    for item in run["accepted"].values():
        if tree(item["commit"]).decode() != item["tree"]:
            raise AssertionError("actual accepted commit tree differs from ledger")
    invocations = [(row["role"], row.get("kind"), row["task_id"], row["status"],
                    bool(row.get("previous_invalid_omitted")), bool(row.get("continuation_rejection_id")))
                   for row in run["dispatches"] if row.get("invoked") and row["id"] not in without]
    attempts = work_attempts(run)
    for row in run["dispatches"]:
        if row["id"] in without and row["role"] == "worker":
            attempts[row["task_id"]] -= 1
    omitted_charges = sum(row["id"] in without and bool(row.get("invoked")) for row in run["dispatches"])
    return (run["status"], {task: item["tree"] for task, item in run["accepted"].items()}, attempts,
            [row["status"] for row in run["final_history"]], invocations, snapshots,
            run["agent_dispatches"] - omitted_charges,
            [(row["task_id"], row["checkpoint_id"]) for row in run["task_checkpoints"]],
            [row["task_id"] for row in run["continuation_history"]])


class Invocation:
    """Use call ordinals and per-frame line ordinals to identify every observed event."""

    def __init__(self, engine, functions, at=None, *, selected=None, child_release=None, waiting_caller=None, window=None):
        self.engine = engine
        self.functions = STAGE_CODES if window else {code_of(item) for item in functions}
        self.window = window
        self.witness = []
        self.at = at
        self.selected = selected
        self.child_release = child_release
        self.waiting_caller = waiting_caller
        self.frames = Counter()
        self.active_frames = {}
        self.regions = []
        self.first_eligible = None
        self.delivery_checks = 0
        self.terminal_workspace = None
        self.emission = None
        self.emitted_point = None
        self.delivered_error = None
        self.events, self.commits = [], []
        self.delivery = None
        self.deferred = None
        self.publication = None
        self.original_error = None
        self.receipt_process = None
        self.receipt_read = None

    def tracing(self, frame, event, value):
        code = frame.f_code
        if event == "call":
            if code is interrupts.deliver.__code__:
                if self.emission is not None:
                    self.delivery_checks += 1
                    if (self.first_eligible is None and not self.controller.terminal
                            and not interrupts.holding() and not interrupts._publishing()):
                        store = self.controller.store
                        if store is None or store.run is None or store.matches_commit():
                            self.first_eligible = (*location(frame.f_back), self.delivery_checks)
                return self.tracing
            if code is PUBLISH.__code__:
                return self.tracing
            if code is STOP.__code__:
                self.original_error = frame.f_locals["error"]
            if code not in self.functions or (self.window and not self.window.admits(frame, self.active_frames)):
                return None
            self.frames[code] += 1
            if code is run_process.__code__ and self.child_release is not None and (
                    self.waiting_caller is None or frame.f_back.f_code.co_qualname == self.waiting_caller):
                self.child_release.unlink(missing_ok=True)
            role = frame.f_locals.get("role")
            kind = frame.f_locals.get("kind")
            retry = frame.f_locals.get("retry_of") is not None
            caller = location(frame.f_back)
            replay = any(item.f_code is TaskLifecycle.replay_worker.__code__ for item in stack_from(frame))
            identity = self.frames[code], caller, role, kind, retry, replay
            self.active_frames[frame] = identity, Counter()
            return self.tracing
        if code is interrupts.deliver.__code__:
            if event == "exception" and value[0] is KeyboardInterrupt:
                self.record_delivery(frame.f_back)
                self.delivered_error = value[1]
            return self.tracing
        if code is PUBLISH.__code__:
            if (event == "line" and frame.f_lineno == PUBLICATION_SAVE
                    and self.deferred == "publication" and self.publication is None):
                self.publication = len(self.commits), deepcopy(self.engine.run)
            return self.tracing
        if event == "return":
            self.active_frames.pop(frame)
        if event == "line":
            point = location(frame)
            identity, lines = self.active_frames[frame]
            lines[point] += 1
            occurrence = (*point, *identity, lines[point])
            if self.window and self.at is None:
                self.witness.append(occurrence)
            if self.selected is not None and point not in self.selected:
                return self.tracing
            if self.waiting_caller is not None and frame.f_back.f_code.co_qualname != self.waiting_caller:
                return self.tracing
            self.events.append(occurrence)
            if len(self.events) == self.at:
                self.emission = self.boundary(frame)
                self.emitted_point = occurrence
                self.regions_at_send = tuple(self.regions)
                if interrupts._publishing():
                    self.deferred = "publication"
                elif interrupts.holding():
                    self.deferred = "settlement"
                signal.raise_signal(signal.SIGINT)
            elif self.child_release is not None and code is run_process.__code__:
                self.child_release.touch()
        return self.tracing

    def boundary(self, frame):
        if self.controller.terminal:
            return "terminal"
        stack = tuple(stack_from(frame))
        scheduler = next((item for item in stack if item.f_code is SCHEDULING), None)
        if scheduler is not None:
            return "loop" if "accepted_this_call" in scheduler.f_locals else "admission"
        if any(item.f_code.co_qualname in PREFLIGHT for item in stack):
            return "preflight"
        return "entry"

    def record_delivery(self, frame):
        self.delivery = (self.controller.delivered_from, self.boundary(frame),
                         deepcopy(self.engine.run), (*location(frame), self.delivery_checks))
        self.delivery_commit_count = len(self.commits)
        if self.controller.delivered_from == "ProbeSignals.checkpoint":
            probe = frame.f_back.f_locals["self"]
            self.probe_pgid = probe.process.pid if probe.process is not None else None
        if self.controller.delivered_from != "run_process" and self.engine.run.get("worktree_binding"):
            self.delivery_workspace = worktree(self.engine.run)

    def saved(self, row):
        self.commits.append(deepcopy(row))
        if self.controller.terminal and self.terminal_workspace is None:
            self.terminal_workspace = worktree(row)

    @contextmanager
    def hold(self, original):
        owner = sys._getframe(2)
        region = {"owner": owner.f_code.co_qualname, "entry": len(self.commits)}
        self.regions.append(region)
        try:
            with original():
                yield
        finally:
            self.regions.pop()
            if self.emission is not None and any(item is region for item in self.regions_at_send):
                region["exit"] = len(self.commits)
                region["row"] = deepcopy(self.engine.run)
                intent = owner.f_locals.get("intent")
                if intent is not None:
                    region["intent"] = deepcopy(intent)
                    region["workspace"] = worktree(self.engine.run)
                if region["owner"] == "Verification.verify_fresh_checkout":
                    checkout = owner.f_locals["checkout"]
                    region["checkout_head"] = git_read(checkout, "rev-parse", "HEAD").strip()
                    region["checkout_git_dir"] = git_read(checkout, "rev-parse", "--absolute-git-dir").strip()

    def call(self, action, *, cli_call=False):
        stderr = io.StringIO()
        self.engine.context.on_save = self.saved
        original_hold = interrupts.hold
        previous = sys.gettrace()
        try:
            with redirect_stderr(stderr), interrupts.installed(), patch.object(
                    interrupts, "hold", lambda: self.hold(original_hold)):
                self.controller = interrupts.active()
                sys.settrace(self.tracing)
                try:
                    result, error = action(), None
                    if cli_call:
                        self.cli_code, result = result, None
                        saved_stop = self.engine.context.saved_stop
                        if self.cli_code == 130:
                            error = self.delivered_error
                        elif saved_stop is not None:
                            error = saved_stop[0]
                        if self.cli_code == 0:
                            result = self.engine.run
                except (HarnessError, KeyboardInterrupt) as caught:
                    result, error = None, caught
                finally:
                    sys.settrace(previous)
        finally:
            self.engine.context.on_save = None
        return result, error, stderr.getvalue()



class PublicationInterruptSweepTests(RepoCase):
    @contextmanager
    def fresh_trial(self):
        """Give setup, action and recovery one project and one out-of-repo scratch directory."""
        parent = self.repo, self.outside, self.config
        try:
            with ExitStack() as stack:
                self.repo = Path(stack.enter_context(TemporaryDirectory()))
                self.outside = Path(stack.enter_context(TemporaryDirectory()))
                self.config = deepcopy(parent[2])
                stack.callback(self.check_run_entries, self.repo)
                stack.callback(self.check_stop_abandon, self.repo)
                self.init_repo(self.repo)
                yield
        finally:
            self.repo, self.outside, self.config = parent

    def cli_action(self, engine, *args):
        def runtime(config, repo, **options):
            engine.context.operation = options.get("operation")
            return engine

        output = io.StringIO()
        with patch.object(cli, "Runtime", runtime), patch.object(
                cli, "Config", SimpleNamespace(load=lambda *args, **kwargs: self.config)), redirect_stdout(output):
            code = cli.main(["--repo", str(self.repo), *map(str, args)])
        if code == 0:
            keys = ("id", "status", "branch", "worktree")
            actual = json.loads(output.getvalue())
            self.assertEqual({key: actual[key] for key in keys}, {key: engine.run[key] for key in keys})
            if "result" in engine.run:
                self.assertEqual(actual["result"], engine.run["result"])
        return code

    @contextmanager
    def git_wait(self, invocation):
        """Delay an actual read-only Git child until released or its group is cancelled."""
        bin_dir = self.outside / "git-launcher"
        bin_dir.mkdir(exist_ok=True)
        marker, release = bin_dir / "started", bin_dir / "release"
        marker.unlink(missing_ok=True)
        release.unlink(missing_ok=True)
        script = bin_dir / "git"
        script.write_text(f"""#!{sys.executable}
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
marker = Path({str(marker)!r})
if args[-2:] == ["rev-parse", "HEAD"] and not marker.exists():
    started = marker.with_suffix(".tmp")
    started.write_text(json.dumps([os.getpid(), args]))
    started.replace(marker)
    release = Path({str(release)!r})
    while not release.exists():
        time.sleep(0.01)
os.execv({REAL_GIT!r}, [{REAL_GIT!r}, *args])
""")
        script.chmod(0o755)
        observed = []
        launch = git._launch
        communicate = subprocess.Popen.communicate

        def wait_for_start(process, *args, **kwargs):
            # Git already owns the child here. Let the launcher publish its
            # identity before the first interruptible 100 ms wait slice.
            deadline = time.monotonic() + 5
            while not marker.exists():
                self.assertLess(time.monotonic(), deadline, "Git wait launcher did not start")
                time.sleep(.005)
            return communicate(process, *args, **kwargs)

        def launch_git(argv, env, *, effect, **options):
            match = argv[-2:] == ["rev-parse", "HEAD"]
            if not match or marker.exists() or not invocation.events:
                return launch(argv, env, effect=effect, **options)
            self.assertIs(effect, git.READ)
            if invocation.at is None:
                release.touch()
            with patch.object(subprocess.Popen, "communicate", wait_for_start), patch.dict(
                    os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}"):
                value = launch(argv, {**env, "PATH": os.environ["PATH"]}, effect=effect, **options)
            if marker.exists():
                observed.append(value.stdout)
            return value

        with patch.object(git, "_launch", launch_git):
            yield marker, observed

    @contextmanager
    def git_completion(self, invocation, receipt=None):
        """Order natural Git completion ahead of delivery during a line trial."""
        launch = git._launch
        communicate = subprocess.Popen.communicate

        def launch_git(argv, env, *, effect, input, deadline, pass_fds=()):
            if (not invocation.emission or invocation.delivery is not None
                    or interrupts.active() is not invocation.controller
                    or effect is None or not effect.interruptible):
                return launch(argv, env, effect=effect, input=input, deadline=deadline, pass_fds=pass_fds)
            if not any(effect is allowed for allowed in (git.READ, git.COMPARE, git.PRIVATE)):
                self.fail(f"undeclared interruptible Git effect in sweep: {effect!r}")
            receipt_read = (receipt is not None and invocation.receipt_process is None
                            and effect is git.READ and argv[-3:] == ["symbolic-ref", "-q", "HEAD"]
                            and self.in_final_receipt_binding())
            if receipt_read and receipt == "C":
                def ready(process):
                    invocation.receipt_process = process

                invocation.receipt_read = self.launch_waiting_git(
                    launch, env, effect=effect, input=input, deadline=deadline, pass_fds=pass_fds, on_ready=ready)
                return invocation.receipt_read

            def complete(process, *args, **kwargs):
                if (sys._getframe(1).f_code is not launch.__code__
                        or interrupts.active() is not invocation.controller):
                    return communicate(process, *args, **kwargs)
                # Drain both pipes while the child runs. A wait-before-drain can
                # deadlock on a full pipe. Use the original absolute deadline.
                if receipt_read:
                    invocation.receipt_process = process
                return communicate(process, timeout=max(0, deadline - time.monotonic()))

            with patch.object(subprocess.Popen, "communicate", complete):
                result = launch(argv, env, effect=effect, input=input, deadline=deadline, pass_fds=pass_fds)
            if receipt_read:
                invocation.receipt_read = result
            return result

        with patch.object(git, "_launch", launch_git):
            yield

    def launch_waiting_git(self, launch, env, *, effect, input, deadline, pass_fds, on_ready):
        communicate = subprocess.Popen.communicate
        started = False

        def wait(process, *args, **kwargs):
            nonlocal started
            if sys._getframe(1).f_code is launch.__code__ and not started:
                started = True
                with selectors.DefaultSelector() as ready:
                    ready.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(ready.select(max(0, deadline - time.monotonic())),
                                    "Git wait child did not become ready")
                    self.assertEqual(os.read(process.stdout.fileno(), 1), b"R")
                on_ready(process)
                kwargs["timeout"] = min(kwargs["timeout"], max(0, deadline - time.monotonic()))
            return communicate(process, *args, **kwargs)

        argv = [sys.executable, "-I", "-S", "-c", READY_GIT]
        with patch.object(subprocess.Popen, "communicate", wait):
            return launch(argv, env, effect=effect, input=input, deadline=deadline, pass_fds=pass_fds)

    @staticmethod
    def in_final_receipt_binding():
        frames = list(stack_from(sys._getframe(1)))
        return (any(frame.f_code is git._check_binding.__code__ for frame in frames)
                and any(frame.f_code is ResultReceipts.finish_review_receipt.__code__
                        and frame.f_locals["key"] == FINAL_RECEIPT for frame in frames))

    def check_final_receipt(self, invocation, mode, outcome, saved, final):
        category, boundary, cancelled = outcome
        if mode == "A":
            origin = branch_point(FinalAcceptance.final_review, "record['status'] == 'approved'",
                                  "interrupts.deliver()", "return self.completion_gate(digest, commit, tree)")
        else:
            origin = source_point(git._launch, "interrupts.deliver()")
        self.assertEqual((category, invocation.emission, boundary, cancelled), (mode, "loop", "loop", ()))
        _, _, at_delivery, point = invocation.delivery
        self.assertEqual(point[:2], origin)
        adoption = next(row for row in invocation.commits
                        if row["review_receipts"].get(FINAL_RECEIPT, {}).get("artifact_digest"))
        receipt = adoption["review_receipts"][FINAL_RECEIPT]
        ident = receipt["dispatch_id"]
        self.assertEqual(invocation.commits[invocation.delivery_commit_count - 1], at_delivery)
        self.assertEqual(invocation.commits[-1], saved)
        revisions = [row["store_revision"] for row in invocation.commits]
        self.assertEqual(revisions, list(range(revisions[0], revisions[-1] + 1)))
        self.assertEqual((adoption["final_history"][-1]["status"], adoption["final_history"][-1]["phase"]),
                         ("running", "reviewing"))

        process = invocation.receipt_process
        self.assertIsNotNone(process, "final receipt binding read was not observed")
        self.assert_git_reaped(process)
        if mode == "C":
            self.assertEqual(process.returncode, -signal.SIGTERM)
            self.assertIsNone(invocation.receipt_read)
            self.assertEqual(at_delivery, adoption)
            self.assertEqual(saved["review_receipts"][FINAL_RECEIPT], receipt)
            self.assertEqual(saved["final_history"], at_delivery["final_history"])
        else:
            read = invocation.receipt_read
            self.assertEqual(process.returncode, 0)
            branch = os.fsencode(saved["worktree_binding"]["branch"])
            self.assertEqual((read.returncode, read.stdout, read.stderr), (0, branch + b"\n", b""))
            self.assertGreater(at_delivery["store_revision"], adoption["store_revision"])
            self.assertNotIn(FINAL_RECEIPT, saved["review_receipts"])
            self.assertEqual(saved["final_history"][-1]["status"], "approved")
            self.assertEqual(saved["final_history"][-1]["review_source"], receipt)
        for row in (saved, final):
            entry, = (item for item in row["dispatches"] if item.get("kind") == "final_review")
            self.assertEqual((entry["id"], entry["role"], entry["task_id"], entry["invoked"]),
                             (ident, "reviewer", "final", True))
            self.assertEqual(entry["status"], "failed" if row is saved and mode == "C" else "completed")
            self.assertEqual(row["attempts"], at_delivery["attempts"])
            self.assertEqual(row["agent_dispatches"], at_delivery["agent_dispatches"])
            self.assertEqual(dispatches(row), dispatches(at_delivery))
        self.assertEqual([entry["id"] for entry in final["dispatches"]],
                         [entry["id"] for entry in saved["dispatches"]])
        self.assertEqual(len(invocation.engine.backend.calls), saved["agent_dispatches"])
        self.assertNotIn(FINAL_RECEIPT, final["review_receipts"])
        self.assertEqual(final["final_history"][-1]["status"], "approved")
        self.assertEqual(final["final_history"][-1]["review_source"], receipt)

    def assert_git_reaped(self, process):
        self.assertTrue(process.stdout.closed and process.stderr.closed)
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
        with self.assertRaises(ProcessLookupError):
            os.killpg(process.pid, 0)

    def assert_interrupted(self, saved, error):
        self.assertIsInstance(error, KeyboardInterrupt)
        self.assertEqual(stops.exit_code(error), 130)
        self.assertEqual((saved["status"], saved["stop"]["kind"], saved["stop"]["reason"]),
                         ("interrupted", "interrupted", "signal"))

    def check_regions(self, invocation, delivered):
        if invocation.deferred == "publication":
            self.assertIsNotNone(invocation.publication, "the publishing body never reached its own save")
            index, candidate = invocation.publication
            self.assertGreater(len(invocation.commits), index)
            candidate["store_revision"] += 1
            self.assertEqual(invocation.commits[index], candidate)
            if delivered is not None:
                self.assertLess(index, invocation.delivery_commit_count)
        for region in invocation.regions_at_send:
            row = region["row"]
            self.assertGreater(region["exit"], region["entry"])
            self.assertEqual(row, invocation.commits[region["exit"] - 1])
            if delivered is not None:
                self.assertLessEqual(region["exit"], invocation.delivery_commit_count)
            if region["owner"] == START_LOCKED.co_qualname:
                head, ref, branch, _, _, _ = worktree(row)
                self.assertEqual(head, ref)
                self.assertEqual(head, row["expected_head"].encode())
                self.assertEqual(branch, row["worktree_binding"]["branch"].encode())
            elif region["owner"] == "Dispatcher.dispatch_once":
                key, receipt = next(iter(row["worker_receipts"].items()))
                self.assertTrue(key.startswith("worker:implement:"))
                self.assertEqual(receipt["phase"], "sealed")
                entry, = [entry for entry in row["dispatches"] if entry["id"] == receipt["dispatch_id"]]
                seal, = Path(receipt["artifact"]).parent.glob("worker-seal.*.json")
                self.assertEqual(hashlib.sha256(seal.read_bytes()).hexdigest(), receipt["seal_digest"])
                evidence = read_json(seal)
                self.assertEqual(evidence["dispatch_id"], entry["id"])
                self.assertEqual(evidence["head"], row["expected_head"])
                self.assertTrue(entry["invoked"])
            elif region["owner"] == "TaskLifecycle._complete_acceptance":
                intent = region["intent"]
                self.assertEqual(row["expected_head"], intent["commit"])
                accepted, = [item for item in row["accepted"].values() if item["commit"] == intent["commit"]]
                self.assertEqual(accepted["tree"], intent["tree"])
                self.assertNotIn("pending_acceptance", row)
                head, ref, branch, tree, index, _ = region["workspace"]
                self.assertEqual((head, ref, branch, tree, hashlib.sha256(index).hexdigest()),
                                 (intent["commit"].encode(), intent["commit"].encode(), intent["ref"].encode(),
                                  intent["tree"].encode(), intent["index_synced"]))
            elif region["owner"] == "Verification.verify_fresh_checkout":
                entry = row["verification_checkouts"][-1]
                self.assertEqual(region["checkout_head"], entry["binding"]["detached_head"].encode())
                self.assertEqual(region["checkout_git_dir"], os.fsencode(entry["binding"]["git_dir"]))
            else:
                self.fail(f"new settlement region requires a result oracle: {region['owner']}")

    def check_delivery(self, invocation, saved, error, loaded, baseline, stderr, preflight_delta):
        delivered = invocation.controller.delivered_from
        self.assertIsNotNone(invocation.emission)
        self.assertEqual(invocation.controller.count, 1)
        self.assertEqual(invocation.controller.first[0], signal.SIGINT)
        self.check_regions(invocation, delivered)
        if delivered is None:
            self.assertIsNone(invocation.delivery)
            self.assertIsNone(invocation.first_eligible)
            self.assertEqual(saved, invocation.commits[-1])
            self.assertIsNotNone(invocation.terminal_workspace)
            self.assertEqual(worktree(saved), invocation.terminal_workspace)
            self.assertIn("SIGINT", stderr)
            self.assertIn(f"status={saved['status']}", stderr)
            self.assertEqual(type(error), type(baseline[1]))
            if invocation.original_error is not None:
                self.assertIs(error, invocation.original_error)
            if error is not None:
                self.assertEqual(stops.of(error)["kind"], stops.of(baseline[1])["kind"])
                self.assertEqual(stops.of(error)["reason"], stops.of(baseline[1])["reason"])
            return ("E/F" if invocation.deferred else "F"), "terminal", ()
        origin, boundary, at_delivery, point = invocation.delivery
        self.assertEqual(origin, delivered)
        self.assertEqual(point, invocation.first_eligible)
        if boundary == "preflight":
            self.assertIn(delivered, PREFLIGHT | {"_launch"})
            self.assertIs(error, invocation.delivered_error)
            self.assertIsInstance(error, KeyboardInterrupt)
            self.assertEqual(invocation.cli_code, 130)
            expected = deepcopy(loaded)
            if preflight_delta is not None:
                preflight_delta(invocation, expected)
            self.assertEqual(saved, expected)
            return "D", boundary, ()

        self.assertIn(boundary, {"admission", "loop"})
        self.assert_interrupted(saved, error)
        self.assertEqual(worktree(saved), invocation.terminal_workspace)
        if delivered != "run_process":
            self.assertEqual(invocation.terminal_workspace, invocation.delivery_workspace)
        if len(invocation.commits) == 1:
            # Resume may stop during admission before publishing anything.
            # Its initial durable ledger is the fixture's saved input.
            self.assertEqual(boundary, "admission")
            self.assertEqual(invocation.delivery_commit_count, 0)
            self.assertIsNotNone(loaded)
            previous = loaded
        else:
            previous = invocation.commits[-2]
        prior = ledger(previous)
        actual = ledger(saved)
        self.assertEqual(len(prior["dispatches"]), len(actual["dispatches"]))
        for old, new in zip(prior["dispatches"], actual["dispatches"]):
            if old.get("exit_confirmed") is not True and new.get("exit_confirmed") is True:
                # The stop rechecks exit on that same dispatch without a separate commit.
                self.assertEqual(old["id"], new["id"])
                new.pop("exit_confirmed")
                if "exit_confirmed" in old:
                    new["exit_confirmed"] = old["exit_confirmed"]
        self.assertEqual(actual, prior)
        cancelled = ()
        if delivered == "run_process":
            active, = (row["id"] for row in at_delivery["dispatches"] if row["process_state"] == "running")
            row, = (row for row in saved["dispatches"] if row["id"] == active)
            self.assertEqual((row["termination_reason"], row["exit_confirmed"]), ("cancelled", True))
            self.assertIsInstance(row["pgid"], int)
            with self.assertRaises(ProcessLookupError):
                os.killpg(row["pgid"], 0)
            cancelled = (active,)
            category = "B"
        elif delivered in WAIT:
            category = "C"
            self.assertNotIn("quota_wait", saved)
            if delivered == "ProbeSignals.checkpoint" and invocation.probe_pgid is not None:
                with self.assertRaises(ProcessLookupError):
                    os.killpg(invocation.probe_pgid, 0)
        else:
            self.assertIn(delivered, NONWAIT, f"unclassified SIGINT delivery at {point}")
            category = "A"
            self.assertEqual(saved["attempts"], at_delivery["attempts"])
            self.assertEqual(saved["agent_dispatches"], at_delivery["agent_dispatches"])
            self.assertEqual(dispatches(saved), dispatches(at_delivery))
            if delivered == "Dispatcher.dispatch_once":
                for entry in at_delivery["dispatches"]:
                    if not entry.get("invoked") and entry["status"] == "running":
                        row, = (row for row in saved["dispatches"] if row["id"] == entry["id"])
                        self.assertEqual((row["invoked"], row["status"]), (False, "failed"))
        if invocation.deferred:
            category = "E/" + category
        return category, boundary, cancelled

    def sweep(self, functions, setup, expect, *, repeat=None, selected=None, git_wait=False, cli_call=False,
              check_baseline=None, preflight_delta=None, release_file=None, waiting_caller=None,
              window=None, check_final=None, receipt=None):
        trace_options = dict(selected=selected, waiting_caller=waiting_caller, window=window)
        with self.fresh_trial():
            engine, action, _ = setup()
            child_release = self.outside / release_file if release_file else None
            control = Invocation(engine, functions, child_release=child_release, **trace_options)
            with self.git_wait(control) if git_wait else nullcontext() as git_evidence:
                baseline = control.call(action, cli_call=cli_call)
            if git_wait:
                marker, observations = git_evidence
                self.assertTrue(marker.is_file())
                stdout, = observations
                self.assertRegex(stdout, rb"^[0-9a-f]{40,64}\n$")
            expected = read_json(engine.directory / "state.json")
            self.assertEqual(expected["status"], expect, baseline)
            if check_baseline is not None:
                check_baseline(expected)
            self.assertEqual(worktree(expected), control.terminal_workspace)
            if expect == "paused":
                with redirect_stderr(io.StringIO()):
                    expected = self.engine(engine.backend).resume(expected["id"])
            expected_signature = lifecycle(expected)
            expected_workspace = worktree(expected)
            self.assertEqual(expected_workspace[0], expected["expected_head"].encode())
            self.assertEqual(expected_workspace[1], expected_workspace[0])
            self.assertEqual(expected_workspace[2], expected["worktree_binding"]["branch"].encode())
            if window:
                self.assertIsNone(selected, "business windows scan every event in their frames")
                self.assertIsNone(waiting_caller)
                window.verify(self, control)
            else:
                self.assertEqual(set(control.frames), control.functions)
            self.assertTrue(control.events)
            self.assertEqual(len(set(control.events)), len(control.events))

        frames = {code.co_qualname: n for code, n in control.frames.items()}
        trials = list(enumerate(control.events, 1))
        receipt_point = None
        if receipt is not None:
            receipt_trial = final_receipt_trial(trials)
            # The trial still checks the complete identity, including its occurrence.
            receipt_point = receipt_trial[1]
            if receipt == "C":
                trials = [receipt_trial]
                frames = {receipt_point[0]: frames[receipt_point[0]]}

        dedicated_wait = (git_wait or release_file
                          or control.functions & {code_of(ProbeProcess._start), code_of(QuotaWait._sleep_until)})
        counts = Counter()
        categories = set()
        report_matrix = os.environ.get("HARNESS_PUBLICATION_SWEEP_MATRIX")
        if report_matrix:
            print(json.dumps({"test": self._testMethodName, "planned_events": len(trials),
                              "frames": frames, "plan": [point for _, point in trials]}), flush=True)
        for event, (at, point) in enumerate(trials, 1):
            with self.subTest(event=at, point=point), self.fresh_trial():
                engine, action, loaded = setup()
                child_release = self.outside / release_file if release_file else None
                before = worktree(loaded) if loaded is not None else None
                invocation = Invocation(engine, functions, at, child_release=child_release, **trace_options)
                receipt_mode = receipt if point == receipt_point else None
                if git_wait:
                    git_fixture = self.git_wait(invocation)
                elif dedicated_wait:
                    git_fixture = nullcontext()
                else:
                    git_fixture = self.git_completion(invocation, receipt_mode)
                with git_fixture as git_evidence:
                    try:
                        result, error, stderr = invocation.call(action, cli_call=cli_call)
                    finally:
                        if child_release is not None:
                            child_release.touch()
                if cli_call:
                    self.assertEqual(invocation.cli_code, 130 if invocation.delivery else control.cli_code)
                if git_wait:
                    marker, observations = git_evidence
                    group, argv = read_json(marker)
                    self.assertEqual(argv[-2:], ["rev-parse", "HEAD"])
                    self.assertEqual(observations, [])
                    with self.assertRaises(ProcessLookupError):
                        os.killpg(group, 0)
                    self.assertEqual(invocation.controller.delivered_from, "_launch")
                self.assertEqual(invocation.events[:at], control.events[:at])
                self.assertEqual(invocation.emitted_point, point)
                saved = read_json(engine.directory / "state.json")
                recovered = self.engine(engine.backend)
                recovered.load_run(saved["id"])
                self.assertEqual(recovered.run, saved)
                outcome = self.check_delivery(invocation, saved, error, loaded, baseline, stderr, preflight_delta)
                category, boundary, without = outcome
                if category == "D":
                    self.assertEqual(worktree(saved), before)
                    final = repeat(recovered, saved["id"])
                elif category in {"F", "E/F"}:
                    final = saved
                    if error is None:
                        self.assertEqual(result, saved)
                else:
                    try:
                        with redirect_stderr(io.StringIO()):
                            final = recovered.resume(saved["id"])
                    except HarnessError as caught:
                        self.assertIsInstance(baseline[1], HarnessError)
                        self.assertEqual(stops.exit_code(caught), stops.exit_code(baseline[1]))
                        final = read_json(recovered.directory / "state.json")
                if expect == "paused" and final["status"] == "paused":
                    with redirect_stderr(io.StringIO()):
                        final = recovered.resume(saved["id"])
                self.assertEqual(read_json(recovered.directory / "state.json"), final)
                worker_redo = self.check_redo(saved, final, without)
                if report_matrix and worker_redo is not None:
                    print(json.dumps({
                        "test": self._testMethodName, "worker_cancel_snapshot": [
                            (r["id"], r["role"], r.get("kind"), r["task_id"], r["status"],
                             r.get("termination_reason"), r.get("exit_confirmed"))
                            for r in saved["dispatches"]],
                        "worker_resume_snapshot": [
                            (r["id"], r["role"], r.get("kind"), r["task_id"], r["status"],
                             r.get("termination_reason"), r.get("exit_confirmed"))
                            for r in final["dispatches"]],
                        "baseline_attempts": expected_signature[2], "stop_attempts": saved["attempts"],
                        "final_attempts": final["attempts"], "stop_dispatches": saved["agent_dispatches"],
                        "final_dispatches": final["agent_dispatches"],
                    }), flush=True)
                if worker_redo is not None:
                    self.assertEqual(work_attempts(final)[worker_redo],
                                     expected_signature[2][worker_redo] + 1)
                self.assertEqual(lifecycle(final, without), expected_signature)
                if receipt_mode is not None:
                    self.check_final_receipt(invocation, receipt_mode, outcome, saved, final)
                if check_final is not None:
                    check_final(final, loaded)
                final_workspace = worktree(final)
                self.assertEqual(final_workspace[0], final["expected_head"].encode())
                self.assertEqual(final_workspace[2], final["worktree_binding"]["branch"].encode())
                self.assertTrue(same_workspace(final_workspace, expected_workspace))
                counts[(*point, category, invocation.emission, boundary, invocation.controller.delivered_from)] += 1
                categories.add(category)
                if report_matrix:
                    print(json.dumps({"test": self._testMethodName, "event": event,
                                      "result": list(counts)[-1]}), flush=True)
        if len(counts) != len(trials):
            self.fail(f"verified {len(counts)} of {len(trials)} planned SIGINT events")
        return categories

    def check_redo(self, saved, final, cancelled):
        worker_redo = None
        earlier = {entry["id"] for entry in saved["dispatches"]}
        fresh = [entry for entry in final["dispatches"] if entry["id"] not in earlier]
        for ident in cancelled:
            original, = [entry for entry in saved["dispatches"] if entry["id"] == ident]
            role, kind, task = original["role"], original.get("kind"), original["task_id"]
            redo, = [entry for entry in fresh
                     if (entry["role"], entry.get("kind"), entry["task_id"]) == (role, kind, task)]
            self.assertTrue(redo["exit_confirmed"])
            if role == "worker":
                worker_redo = task
        return worker_redo

    def start(self, backend=None, *, supplied=True, pause=False, via_cli=False, step=False, plan_data=None):
        engine = self.engine(backend)
        supplied_plan = (plan() if plan_data is None else plan_data) if supplied else None
        if via_cli:
            args = ["run", "Implement greet"]
            if supplied:
                plan_path = self.outside / "sweep-plan.json"
                plan_path.write_text(json.dumps(supplied_plan))
                args.extend(("--plan", plan_path))
            if pause:
                args.append("--pause-after-plan")
            if step:
                args.append("--step")
            return engine, lambda: self.cli_action(engine, *args), None
        return engine, lambda: engine.start("Implement greet", supplied_plan,
                                            pause_after_plan=pause, step=step), None

    def cli_resume(self, runtime, ident, *options):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.cli_action(runtime, "resume", ident, *options), 0)
        return runtime.run

    def approval_delta(self, invocation, expected):
        self.assertEqual(len(invocation.commits), 2)
        expected["scope"].append("future/**")
        expected["store_revision"] += 1
        self.assertEqual(invocation.commits[0], expected)
        expected.update(plan_approved=True, store_revision=expected["store_revision"] + 1)
        self.assertEqual(invocation.commits[1], expected)

    def test_dispatch(self):
        self.assertTrue({"A", "E/A"} <= self.dispatch_sweep("A"))

    def test_final_review_receipt_git_wait(self):
        self.assertEqual(self.dispatch_sweep("C"), {"C"})

    def dispatch_sweep(self, receipt):
        self.config.data["verification"]["commands"].append(
            {"cwd": ".", "argv": [sys.executable, "-c", "pass  # " + "x" * 4200]})

        def invalid_first_review(backend, kwargs, packet, result):
            if trigger_kind(kwargs, packet) == "plan":
                result["new_tasks"][0]["allowed_paths"].append("notes/**")
            if kwargs["role"] == "worker":
                folder = Path(kwargs["cwd"]) / "notes"
                folder.mkdir(exist_ok=True)
                for number in range(24):
                    (folder / (f"{number:02}-" + "a" * 180)).write_text("review fixture\n")
            if kwargs["kind"] == "review" and sum(call["kind"] == "review" for call in backend.calls) == 1:
                result["summary"] = "x" * 4001

        compose = dispatch.compose

        def bounded(required, memories, limit, **kwargs):
            if required["kind"] == "review" and "previous_invalid" in required["packet"]:
                bare = {**required, "packet": {key: value for key, value in required["packet"].items()
                                              if key != "previous_invalid"}}
                _, report = compose(bare, memories, limit, **kwargs)
                limit = report["required_chars"]
            return compose(required, memories, limit, **kwargs)

        def retried(row):
            roles = [(entry["role"], entry.get("kind")) for entry in row["dispatches"] if entry.get("invoked")]
            self.assertEqual(roles, [("commander", "decide"), ("worker", "implement"), ("reviewer", "review"),
                                     ("reviewer", "review"), ("reviewer", "final_review")])
            first, second = [entry for entry in row["dispatches"] if entry.get("kind") == "review"]
            self.assertEqual(second["retry_of"], first["id"])
            self.assertTrue(second["previous_invalid_omitted"])
            for entry in (first, second):
                path_ref, = entry["path_evidence"]
                self.assertEqual(path_ref["count"], 25)
                self.assertTrue(entry["input_evidence"])

        window = StageWindow([(Dispatcher.dispatch_once, None, True)],
                             boundaries=(WorkerReceipts._finish,),
                             required=(WorkerReceipts.prepare_intent, WorkerReceipts.publish_inputs),
                             landmarks=((Dispatcher.dispatch_once, 'entry["retry_of"] = retry_of'),
                                        (Dispatcher.dispatch_once, 'entry["previous_invalid_omitted"] = True'),
                                        (Dispatcher.dispatch_once, 'entry["path_evidence"] = path_evidence'),
                                        (Dispatcher.dispatch_once, 'entry["input_evidence"] = input_evidence')))
        with patch.object(dispatch, "compose", side_effect=bounded):
            return self.sweep((), lambda: self.start(ScriptedBackend(invalid_first_review), supplied=False),
                              "completed", check_baseline=retried, window=window, receipt=receipt)

    def test_task_handoff_and_review(self):
        def checkpoint(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                digest = hashlib.sha256((Path(kwargs["cwd"]) / "hello.py").read_bytes()).hexdigest()
                reference = {"path": "hello.py", "sha256": digest}
                result["checkpoints"] = [{"id": "greet", "summary": "Implement greeting",
                    "inputs": [reference], "evidence": [reference], "freshness": "content", "max_age_seconds": 0}]

        def registered(run):
            row, = run["task_checkpoints"]
            self.assertEqual((row["task_id"], row["checkpoint_id"]), ("greet", "greet"))

        window = StageWindow([(WorkerReceipts._finish, None, True),
                              (TaskLifecycle.publish_worker_result, None, True)],
                             boundaries=(Dispatcher.dispatch_once,),
                             required=(ProgressRecovery._register_checkpoint_batch, TaskLifecycle.continue_task,
                                       WorkerReceipts.prepare_consume, WorkerReceipts.apply_consume),
                             landmarks=((TaskLifecycle.publish_worker_result, 'runtime.run["pending_task"] = pending'),))
        self.sweep((), lambda: self.start(ScriptedBackend(checkpoint)), "completed",
                   check_baseline=registered, window=window)

    def test_verification_retry_publishes_the_new_round_and_reads_its_evidence(self):
        originals = {}
        write = task_lifecycle.write_json

        def setup():
            seed, start, _ = self.start()

            def before_review(path, value):
                if path.name == "snapshot.json":
                    raise OSError("fixture ends after verification and before review evidence")
                return write(path, value)

            with redirect_stderr(io.StringIO()), patch.object(task_lifecycle, "write_json", before_review):
                with self.assertRaisesRegex(OSError, "fixture ends after verification"):
                    start()
            loaded = read_json(seed.directory / "state.json")
            pending = loaded["pending_task"]
            self.assertEqual((pending["stage"], pending["verification_rounds"]), ("verifying", 1))
            path = Path(pending["verification"])
            self.assertEqual(path.parent.name, "1")
            originals[loaded["id"]] = path.read_bytes()
            runtime = self.engine()
            return runtime, lambda: runtime.resume(loaded["id"]), loaded

        def check_final(row, loaded):
            original = Path(loaded["pending_task"]["verification"])
            self.assertEqual(original.read_bytes(), originals[loaded["id"]])
            accepted = Path(row["accepted"]["greet"]["verification"])
            self.assertEqual(accepted.parent.parent, original.parent)
            self.assertGreaterEqual(int(accepted.parent.name.removeprefix("retry-")), 2)
            review, = row["review_history"]["greet"]
            self.assertEqual(Path(review["verification"]), accepted)
            self.assertEqual(Path(review["snapshot_artifact"]), accepted.with_name("snapshot.json"))
            self.assertEqual(row["attempts"], {"greet": 1})

        def baseline(row):
            accepted = Path(row["accepted"]["greet"]["verification"])
            self.assertEqual(accepted.parent.name, "retry-2")

        window = StageWindow([(TaskLifecycle.continue_task, None, True)],
                             boundaries=(Dispatcher.dispatch_once,),
                             landmarks=((TaskLifecycle.continue_task, 'folder /= f"retry-'),
                                        (TaskLifecycle.continue_task, 'files = files.under(f"retry-')))
        self.sweep((), setup, "completed", check_baseline=baseline, check_final=check_final, window=window)

    def test_failed_preflight_and_verification_publish_feedback_before_rework(self):
        def first_two_attempts_fail(backend, kwargs, packet, result):
            if kwargs["role"] != "worker":
                return
            attempt = sum(call["role"] == "worker" for call in backend.calls)
            cwd = Path(kwargs["cwd"])
            if attempt > 1:
                (cwd / "proof.txt").write_text("required evidence\n")
            if attempt == 2:
                (cwd / "hello.py").write_text("def greet(name):\n    return name\n")

        def reworked(row):
            self.assertEqual(row["attempts"], {"greet": 3})
            review, = row["review_history"]["greet"]
            self.assertEqual(review["attempt"], 3)
            directory = Path(row["plan_history"][0]["artifact"]).parent.parent
            preflight = read_json(directory / "preflight/greet/1/result.json")
            self.assertFalse(preflight["passed"])
            checks = read_json(directory / "verification/tasks/greet/2/results.json")
            self.assertEqual([check["status"] for check in checks], ["failed"])

        window = StageWindow([(TaskLifecycle.continue_task,
                               lambda local: local["self"].context.run["pending_task"]["attempt"] < 3, True)],
                             boundaries=(Dispatcher.dispatch_once,),
                             landmarks=((TaskLifecycle.continue_task, 'self.context.event("candidate_preflight_failed"'),
                                        (TaskLifecycle.continue_task, 'self.context.event("verification_failed"')))
        self.sweep((), lambda: self.start(ScriptedBackend(first_two_attempts_fail), plan_data=evidence_plan()),
                   "completed", check_baseline=reworked, window=window)

    def test_reject_invalid_worker(self):
        def invalid(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(row["role"] == "worker" for row in backend.calls) == 1:
                result["summary"] = "x" * 32001
        def rejected(row):
            workers = [entry for entry in row["dispatches"] if entry["role"] == "worker" and entry["invoked"]]
            self.assertEqual([entry["status"] for entry in workers], ["invalid", "completed"])

        def first_worker(local):
            return local["self"].context.run["attempts"].get("greet") == 1

        window = StageWindow([(WorkerReceipts._finish, first_worker, True)],
                             required=(WorkerReceipts.reject, WorkerReceipts.prepare_consume,
                                       WorkerReceipts.apply_consume),
                             landmarks=((WorkerReceipts._finish, "self.reject(key, judgment.error"),))
        self.sweep((), lambda: self.start(ScriptedBackend(invalid)), "completed",
                   check_baseline=rejected, window=window)

    def test_handoff_rejects_a_decision_for_its_own_task(self):
        def invalid_handoff(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(call["role"] == "worker" for call in backend.calls) == 1:
                result["decision_requests"] = [{"id": "self", "task_id": "greet", "question": "Proceed?"}]

        def rejected(row):
            original, fresh = [entry for entry in row["dispatches"] if entry["role"] == "worker" and entry["invoked"]]
            self.assertEqual((original["status"], original["worker_receipt_consumed"]), ("invalid", "result_rejected"))
            self.assertNotIn("attempt_exemption", original)
            self.assertEqual(fresh["worker_receipt_consumed"], "pending_task")
            self.assertFalse(row["decision_requests"])

        window = StageWindow([(TaskLifecycle.publish_worker_result,
                               lambda local: bool(local["result"].get("decision_requests")), True)],
                             required=(WorkerReceipts.reject, WorkerReceipts.prepare_consume,
                                       WorkerReceipts.apply_consume),
                             landmarks=((TaskLifecycle.publish_worker_result,
                                         'self.services.worker_receipts.reject(key, error, "result_rejected"'),))
        self.sweep((), lambda: self.start(ScriptedBackend(invalid_handoff)), "completed",
                   check_baseline=rejected, window=window)

    def test_invalid_blocker_and_revision(self):
        description = "Implement greet after clarifying the example"
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and kwargs["task"]["description"] != description:
                result.update(status="blocked", blocker_kind="task", blockers=["x" * 4001])
            if trigger_kind(kwargs, packet) == "task":
                revised(result, packet, "Clarified", updates=[update(plan()["tasks"][0], description)])
        def before_revision(local):
            return len(local["self"].context.run["plan_history"]) == 1

        def directed(local):
            return bool(local["self"].context.run.get("pending_directive"))

        def revised_and_directed(row):
            self.assertEqual(len(row["plan_history"]), 2)
            workers = [entry for entry in row["dispatches"] if entry["role"] == "worker" and entry["invoked"]]
            self.assertEqual([entry["status"] for entry in workers], ["invalid_blocked", "completed"])
            self.assertFalse(row.get("pending_directive"))

        window = StageWindow([(WorkerReceipts._finish, before_revision, True),
                              (Planner.land_revision, None, True),
                              (Planner.apply_pending_revision, None, True),
                              (Dispatcher.dispatch_once, directed, True)],
                             required=(TaskLifecycle.publish_invalid_blocker, WorkerReceipts.prepare_intent,
                                       WorkerReceipts.prepare_consume, WorkerReceipts.apply_consume),
                             landmarks=((WorkerReceipts._finish, "return self.services.tasks.publish_invalid_blocker"),
                                        (Dispatcher.dispatch_once, "self.services.planning.check_directive(task_id, before)"),
                                        (Dispatcher.dispatch_once, 'run.pop("pending_directive", None)')))
        self.sweep((), lambda: self.start(ScriptedBackend(change)), "completed",
                   check_baseline=revised_and_directed, window=window)

    def test_revision_archives_dirty_candidate_before_publishing_new_selection(self):
        self.config.data["verification"]["commands"] = [{"cwd": ".", "argv": [sys.executable, "-c", "pass"]}]

        def setup():
            backend = decide_fixtures.RevisionArchiveTests.archive_script(self)
            tasks = {"tasks": [decide_fixtures.task("a")]}
            return self.start(backend, plan_data=tasks)

        def archived(row, loaded=None):
            self.assertEqual(set(row["accepted"]), {"p", "a"})
            workers = [entry["task_id"] for entry in row["dispatches"] if entry["role"] == "worker" and entry["invoked"]]
            self.assertEqual(workers, ["a", "p", "a"])
            revision = Path(row["plan_history"][1]["artifact"])
            self.assertEqual(read_json(revision)["candidate"], "archive")
            recovery = revision.parent.parent / "recovery"
            site, = recovery.glob("revision-1*")
            self.assertEqual(site.name, "revision-1")
            self.assertEqual((site / "files" / "a.txt").read_text(), "partial a, unstaged too\n")
            self.assertTrue((site / "restored.json").is_file())

        window = StageWindow([(Planner.apply_pending_revision, None, True)],
                             landmarks=((Planner.apply_pending_revision, "archive = git.archive_and_reset"),
                                        (Planner.apply_pending_revision, 'run.pop("worker_recovery", None)')))
        self.sweep((), setup, "completed", check_baseline=archived, check_final=archived, window=window)

    def test_window_verification_detects_missing_branch_and_repeated_event(self):
        window = StageWindow([(Scheduler._execute_until_terminal, None, False)],
                             landmarks=((Scheduler._execute_until_terminal, 'self.context.run.update(status="completed"'),))
        engine, action, _ = self.start()
        control = Invocation(engine, (), window=window)
        result, error, _ = control.call(action)
        self.assertIsNone(error)
        self.assertEqual(result["status"], "completed")
        window.verify(self, control)

        missing_repeat = next(index for index, event in enumerate(control.events) if event[-1] == 2)
        occurrence = control.events.pop(missing_repeat)
        with self.assertRaisesRegex(AssertionError, "omitted a witnessed line occurrence"):
            window.verify(self, control)
        control.events.insert(missing_repeat, occurrence)

        caller = source_point(Dispatcher.dispatch, "result = self.dispatch_once(")
        point = (*final_receipt_point(), 5, caller, "reviewer", "final_review", False, False, 1)
        receipt_trials = [(1, point)]
        self.assertEqual(final_receipt_trial(receipt_trials), (1, point))
        receipt_trials.append((2, (*point[:-1], 2)))
        with self.assertRaisesRegex(AssertionError, "expected one final review receipt event, found 2"):
            final_receipt_trial(receipt_trials)

        # Run the same branch check over a different real terminal route.
        engine, action, _ = self.start(pause=True)
        no_completion = Invocation(engine, (), window=window)
        result, error, _ = no_completion.call(action)
        self.assertIsNone(error)
        self.assertEqual(result["status"], "awaiting_approval")
        with self.assertRaisesRegex(AssertionError, "required business branch never executed"):
            window.verify(self, no_completion)

    def test_initial_planning_and_scheduler_terminal(self):
        window = StageWindow([(Scheduler.execute, None, False),
                              (Scheduler._execute_until_terminal, None, False),
                              (Planner.record_plan, None, True),
                              (acceptance_review_records, None, True)],
                             landmarks=((acceptance_review_records,
                                         'if not isinstance(feedback, dict) or not isinstance(feedback.get("acceptance"), dict):'),))
        categories = self.sweep((), lambda: self.start(supplied=False, via_cli=True), "completed",
                                cli_call=True, window=window)
        self.assertTrue({"A", "E/A", "F"} <= categories)

    def test_abandon(self):
        def setup():
            engine, start, _ = self.start(pause=True)
            with redirect_stderr(io.StringIO()):
                loaded = start()
            engine = self.engine()
            return engine, lambda: self.cli_action(engine, "abandon", loaded["id"]), loaded

        def repeat(runtime, ident):
            self.assertEqual(self.cli_action(runtime, "abandon", ident), 0)
            return runtime.run

        window = StageWindow([(ABANDON, None, True)], required=(ABANDON_LOCKED,))
        self.assertEqual(self.sweep((), setup, "abandoned", repeat=repeat, cli_call=True,
                                    window=window), {"D", "F", "E/F"})

    def test_continue_blocked_worker(self):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(row["role"] == "worker" for row in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="continuation", blockers=["Retain the candidate"],
                              continuation_request={"kind": "budget", "commands": [],
                                                    "minimum_timeout_seconds": 1, "reason": "More time"})
        window = StageWindow([(TaskLifecycle.publish_worker_result,
                               lambda local: local["result"].get("blocker_kind") == "continuation", True)],
                             required=(prepare_continuation, apply_continuation,
                                       WorkerReceipts.prepare_consume, WorkerReceipts.apply_consume))

        def continued(row):
            item, = row["continuation_history"]
            self.assertEqual(item["task_id"], "greet")
            self.assertFalse(row.get("pending_continuation"))

        self.sweep((), lambda: self.start(ScriptedBackend(change)), "completed",
                   check_baseline=continued, window=window)

    def stop_after_blocker(self, kind, *, invalid=False):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.update(status="blocked", blocker_kind=kind, blockers=["Choose how to proceed"])
                if invalid:
                    result["summary"] = "x" * 32001

        def blocked(row):
            worker, = (entry for entry in row["dispatches"] if entry["role"] == "worker" and entry["invoked"])
            self.assertEqual(worker["worker_outcome"], kind + "_blocked")
            self.assertEqual(worker["status"], "invalid_blocked" if invalid else "completed")
            self.assertEqual("attempt_exemption" in worker, not invalid)
            self.assertFalse(row["worker_receipts"])
            self.assertEqual(row["feedback"]["worker"]["blocker_kind"], kind)
            if kind == "operator":
                request, = row["decision_requests"]
                self.assertEqual(request["source"]["dispatch_id"], worker["id"])
                self.assertEqual(row["stop"]["reason"], "answer_required")
            else:
                self.assertEqual((row["stop"]["kind"], row["stop"]["reason"]), ("environment", "worker_blocked"))

        required = [WorkerReceipts.prepare_consume, WorkerReceipts.apply_consume]
        if invalid:
            roots = [(WorkerReceipts._finish, None, True)]
            required.append(TaskLifecycle.publish_invalid_blocker)
        else:
            roots = [(TaskLifecycle.publish_worker_result, None, True)]
            required.extend((AttemptAccounting.prepare_attempt_exemption, AttemptAccounting.apply_attempt_exemption))
        if kind == "environment" and not invalid:
            roots += [(STOP, None, True), (Scheduler.execute, None, False),
                      (Scheduler._execute_until_terminal, None, False)]
            required.append(Workflow._save_stop)
        if kind == "operator":
            required.extend((Workflow.synchronize_decision_requests, DecisionRecovery.register_decision_request))
        window = StageWindow(roots, required=required)
        self.sweep((), lambda: self.start(ScriptedBackend(change), via_cli=True), "blocked",
                   cli_call=True, check_baseline=blocked, window=window)

    def test_stop_after_environment_blocker(self):
        self.stop_after_blocker("environment")

    def test_stop_after_operator_blocker(self):
        self.stop_after_blocker("operator")


    def test_stop_after_vetoed_environment_blocker(self):
        self.stop_after_blocker("environment", invalid=True)

    def test_stop_after_vetoed_operator_blocker(self):
        self.stop_after_blocker("operator", invalid=True)

    def test_task_blocker_is_consumed_without_exemption(self):
        def block_once(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(call["role"] == "worker" for call in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="task", blockers=["Clarify the example"])
            if trigger_kind(kwargs, packet) == "task":
                revised(result, packet, "Clarified", updates=[update(plan()["tasks"][0], "Use the greeting example")])

        def blocked(row):
            original, fresh = [entry for entry in row["dispatches"] if entry["role"] == "worker" and entry["invoked"]]
            self.assertEqual(original["worker_receipt_consumed"], "task_blocked")
            self.assertNotIn("attempt_exemption", original)
            self.assertEqual(fresh["worker_receipt_consumed"], "pending_task")

        window = StageWindow([(TaskLifecycle.publish_worker_result,
                               lambda local: local["result"].get("blocker_kind") == "task", True)],
                             required=(AttemptAccounting.prepare_attempt_exemption, AttemptAccounting.apply_attempt_exemption,
                                       WorkerReceipts.prepare_consume, WorkerReceipts.apply_consume))
        self.sweep((), lambda: self.start(ScriptedBackend(block_once)), "completed",
                   check_baseline=blocked, window=window)

    def test_resume_rebuilds_operator_request_after_handoff_save(self):
        def operator_block(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.update(status="blocked", blocker_kind="operator", blockers=["Choose direction"])

        def setup():
            seed, start, _ = self.start(ScriptedBackend(operator_block))
            cut, seen = crash_after_save(seed, lambda row: (
                (row.get("feedback") or {}).get("worker", {}).get("blocker_kind") == "operator"
                and not row["decision_requests"]))
            with cut, redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt):
                start()
            self.assertTrue(seen["fired"])
            loaded = read_json(seed.directory / "state.json")
            self.assertFalse(loaded["decision_requests"])
            self.assertFalse(loaded["worker_receipts"])
            runtime = self.engine()
            return runtime, lambda: self.cli_action(runtime, "resume", loaded["id"]), loaded

        def rebuilt(row):
            request, = row["decision_requests"]
            worker, = (entry for entry in row["dispatches"] if entry["invoked"])
            self.assertEqual(request["source"]["dispatch_id"], worker["id"])
            self.assertEqual(row["stop"]["reason"], "answer_required")

        window = StageWindow([(Workflow.synchronize_decision_requests, None, True)],
                             required=(DecisionRecovery.register_decision_request,))
        self.sweep((), setup, "blocked", cli_call=True, check_baseline=rebuilt, window=window)

    def test_accounting_binds_continuation_rejection(self):
        def request_once(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(call["role"] == "worker" for call in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="continuation", blockers=["More time"],
                              continuation_request={"kind": "budget", "commands": [], "reason": "More time",
                                  "minimum_timeout_seconds": self.config.data["runtime"]["agent_timeout_seconds"] + 60})

        def setup():
            seed, start, _ = self.start(ScriptedBackend(request_once))
            with redirect_stderr(io.StringIO()), self.assertRaises(HarnessError):
                start()
            loaded = read_json(seed.directory / "state.json")
            request, = loaded["continuation_history"]
            rejection = self.outside / "sweep-rejection.json"
            rejection.write_text(json.dumps({"request_id": request["id"], "reason": "Try another approach"}))
            runtime = self.engine(seed.backend)
            return runtime, lambda: runtime.resume(loaded["id"], reject_request=rejection), loaded

        def bound(row):
            rejection, = row["continuation_rejections"]
            original, fresh = [entry for entry in row["dispatches"] if entry["role"] == "worker" and entry["invoked"]]
            self.assertNotIn("continuation_rejection_id", original)
            self.assertEqual(fresh["continuation_rejection_id"], rejection["id"])
            self.assertEqual(row["attempts"], {"greet": 2})

        window = StageWindow([(Dispatcher.dispatch_once, lambda local: local["role"] == "worker", True)],
                             boundaries=(WorkerReceipts._finish,), required=(WorkerReceipts.prepare_intent,),
                             landmarks=((Dispatcher.dispatch_once, 'entry["continuation_rejection_id"] = rejection["id"]'),))
        self.sweep((), setup, "completed", check_baseline=bound, window=window)

    def test_dispatch_budget_guard_stops_without_charging(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 1

        def uncharged(row):
            self.assertEqual(row["stop"]["reason"], "agent_dispatches")
            self.assertEqual(row["agent_dispatches"], 1)
            reviewer, = (entry for entry in row["dispatches"] if entry["role"] == "reviewer")
            self.assertEqual((reviewer["invoked"], reviewer["status"]), (False, "failed"))
            self.assertFalse(row["review_receipts"])

        window = StageWindow([(Dispatcher.dispatch_once, lambda local: local["role"] == "reviewer", True)],
                             landmarks=((Dispatcher.dispatch_once, "remaining = remaining_model_calls"),))
        self.sweep((), lambda: self.start(via_cli=True), "blocked", cli_call=True,
                   check_baseline=uncharged, window=window)

    def test_git_wait_in_loop_and_cli_preflight(self):
        target = source_point(TaskLifecycle.continue_task, 'key = f"reviewer:review:{tid}"')
        self.assertEqual(self.sweep([TaskLifecycle.continue_task], self.start, "completed",
                                    selected={target}, git_wait=True), {"C"})

        def setup():
            _, start, _ = self.start(pause=True)
            with redirect_stderr(io.StringIO()):
                loaded = start()
            engine = self.engine()
            return engine, lambda: self.cli_action(engine, "resume", loaded["id"], "--approve-plan"), loaded

        target = source_point(RunLifecycle.verify_head_and_inputs, 'pending = self.context.run')
        self.assertEqual(self.sweep([RunLifecycle.verify_head_and_inputs], setup, "completed",
                                    repeat=lambda runtime, ident: self.cli_resume(runtime, ident, "--approve-plan"),
                                    selected={target}, git_wait=True, cli_call=True), {"D"})

    def test_compare_git_wait_cancels_acceptance_scope_check(self):
        self.check_acceptance_git_cancel(
            git.COMPARE, ["diff", "--no-ext-diff", "--no-textconv", "--name-only", "--no-renames", "-z", "HEAD"])

    def test_private_git_wait_cleans_acceptance_index(self):
        self.check_acceptance_git_cancel(git.PRIVATE, ["add", "--", "hello.py"])

    def check_acceptance_git_cancel(self, expected_effect, command):
        with self.fresh_trial(), redirect_stderr(io.StringIO()):
            _, action, _ = self.start()
            baseline = action()
            self.assertEqual(baseline["status"], "completed")
            expected, expected_workspace = lifecycle(baseline), worktree(baseline)

        engine, action, _ = self.start()
        launch = git._launch
        children = []
        private_index = pending = before = None

        def ready(process):
            children.append(process)
            signal.raise_signal(signal.SIGINT)

        def launch_git(argv, env, *, effect, **options):
            nonlocal private_index, pending, before
            if (children or argv[-len(command):] != command
                    or not any(frame.f_code is TaskLifecycle.accept_pending.__code__
                               for frame in stack_from(sys._getframe(1)))):
                return launch(argv, env, effect=effect, **options)
            self.assertIs(effect, expected_effect)
            pending = deepcopy(engine.run["pending_acceptance"])
            self.assertNotIn("intent", pending)
            before = worktree(engine.run)
            if effect is git.PRIVATE:
                private_index = Path(env["GIT_INDEX_FILE"])
                self.assertTrue(private_index.is_file())
                self.assertNotEqual(private_index.parent, Path(engine.run["worktree_binding"]["git_dir"]))
            return self.launch_waiting_git(launch, env, effect=effect, on_ready=ready, **options)

        error = None
        with redirect_stderr(io.StringIO()), interrupts.installed(), patch.object(git, "_launch", launch_git):
            controller = interrupts.active()
            try:
                action()
            except (KeyboardInterrupt, HarnessError) as caught:
                error = caught

        process, = children
        self.assert_git_reaped(process)
        self.assertEqual(process.returncode, -signal.SIGTERM, f"{expected_effect.name} Git wait was not cancelled")
        self.assertEqual((controller.count, controller.first[0], controller.delivered_from),
                         (1, signal.SIGINT, "_launch"))
        saved = read_json(engine.directory / "state.json")
        self.assert_interrupted(saved, error)
        self.assertEqual(saved["pending_acceptance"], pending)
        self.assertEqual(saved["accepted"], {})
        self.assertEqual(worktree(saved), before)
        self.assertEqual(saved["expected_head"].encode(), before[0])
        self.assertFalse(list((self.repo / ".git").rglob("index.lock")))
        if private_index is not None:
            self.assertFalse(private_index.parent.exists())

        recovered = self.engine(engine.backend)
        recovered.load_run(saved["id"])
        self.assertEqual(recovered.run, saved)
        with redirect_stderr(io.StringIO()):
            final = recovered.resume(saved["id"])
        self.assertEqual(lifecycle(final), expected)
        self.assertTrue(same_workspace(worktree(final), expected_workspace))
        self.assertEqual(dispatches(final) - dispatches(saved),
                         {row["id"] for row in final["dispatches"] if row.get("kind") == "final_review"
                          or (row["role"] == "verification" and row["task_id"] == "final")})

    @staticmethod
    def process_wait_point():
        lines, first = inspect.getsourcelines(run_process)
        _, wait = (first + index for index, line in enumerate(lines) if line.strip() == "checkpoint()")
        return run_process.__qualname__, wait

    def test_real_verification_wait(self):
        release_file = "release-verification"

        def setup():
            marker = self.outside / release_file
            self.config.data["verification"]["commands"] = [{
                "cwd": ".", "argv": [sys.executable, "-c", self.wait_for(marker)]
            }]
            return self.start()

        self.assertEqual(self.sweep([run_process], setup, "completed",
                                    selected={self.process_wait_point()}, release_file=release_file), {"B"})

    @staticmethod
    def wait_for(marker):
        return (f"import time; from pathlib import Path\n"
                f"while not Path({str(marker)!r}).exists(): time.sleep(.01)\n")

    def delayed_model_wait(self, target_role):
        release_file = "release-model"

        def setup():
            config, state = self.config, self.repo / ".harness"
            delayed = self.wait_for(self.outside / release_file)

            class DelayedModel(ScriptedBackend):
                def invoke(self, **kwargs):
                    value = super().invoke(**kwargs)
                    if kwargs["role"] != target_role:
                        return value
                    (kwargs["job"] / "response.json").unlink()
                    envelope = {"type": "result", "subtype": "success", "structured_output": value}
                    command = delayed + f"print({json.dumps(envelope)!r})"
                    backend = CLIBackend(config, state)
                    backend.command = lambda *args: [sys.executable, "-c", command]
                    return backend.invoke(**{**kwargs, "model": {**kwargs["model"], "cli": "claude"}})

            return self.start(DelayedModel(), supplied=target_role == "worker")

        self.assertEqual(self.sweep([run_process], setup, "completed",
                                    selected={self.process_wait_point()}, release_file=release_file,
                                    waiting_caller="CLIBackend.invoke"), {"B"})

    def test_real_worker_wait(self):
        self.delayed_model_wait("worker")

    def test_real_commander_wait(self):
        self.delayed_model_wait("commander")

    def test_quota_wait(self):
        self.config.data["runtime"]["quota_wait_max_seconds"] = 300

        def setup():
            clock = FakeClock(quota_fixtures.T0)
            backend = quota_fixtures.QuotaFailingBackend(self.config, self.repo, ("worker",))
            engine, start, loaded = self.start(backend)

            def run():
                window = quota_fixtures.window("session", 100, 60)
                with patch.object(quota_fixtures.quota_wait, "time", clock), patch.dict(
                        os.environ, QUOTA_STUB_WINDOWS=json.dumps([window]), QUOTA_STUB_NOW=str(clock.time())):
                    return start()

            return engine, run, loaded

        target = source_point(QuotaWait._sleep_until, "interrupts.sleep(")
        self.assertEqual(self.sweep([QuotaWait._sleep_until], setup, "completed", selected={target}), {"C"})

    def test_quota_probe(self):
        def setup():
            backend = quota_fixtures.QuotaFailingBackend(self.config, self.repo)
            return self.start(backend)

        target = source_point(ProbeProcess._start, "self.signals.checkpoint()")
        self.assertEqual(self.sweep([ProbeProcess._start], setup, "completed", selected={target}), {"C", "F"})

    def test_scheduler_publishes_legacy_acceptance_invalidation(self):
        error = HarnessError("fixture stops before checking approval",
                             stop=stops.make("runtime_failure", "invariant"))

        def setup():
            engine, start, _ = self.start()
            with redirect_stderr(io.StringIO()), patch.object(TaskLifecycle, "accept_pending", side_effect=error):
                with self.assertRaises(HarnessError):
                    start()
            approval = engine.run["pending_acceptance"]
            # Older ledgers recorded invalidation feedback before marking the review record.
            with engine.context.publish():
                engine.run["feedback"] = {"task_id": approval["task_id"], "acceptance": {
                    "error": "Approval was invalidated", "review_artifact": approval["review_artifact"]}}
                del engine.run["pending_acceptance"]
            loaded = read_json(engine.directory / "state.json")
            runtime = self.engine()
            return runtime, lambda: runtime.resume(loaded["id"]), loaded

        def invalidated(row):
            self.assertIn("invalidated", [item.get("status") for item in row["review_history"]["greet"]])

        def invalidated_original(row, loaded):
            artifact = loaded["feedback"]["acceptance"]["review_artifact"]
            before, = [item for item in loaded["review_history"]["greet"] if item["artifact"] == artifact]
            after, = [item for item in row["review_history"]["greet"] if item["artifact"] == artifact]
            self.assertNotEqual(before.get("status"), "invalidated")
            self.assertEqual(after, {**before, "status": "invalidated"})

        window = StageWindow([(Scheduler._execute_until_terminal, None, False),
                              (acceptance_review_records, None, True)],
                             landmarks=((Scheduler._execute_until_terminal, 'record["status"] = "invalidated"'),))
        self.assertIn("E/A", self.sweep((), setup, "completed", check_baseline=invalidated, window=window,
                                       check_final=invalidated_original))

    def test_resume_preflight_preserves_published_approval(self):
        def setup():
            seed = self.engine()
            with redirect_stderr(io.StringIO()):
                loaded = seed.start("Implement greet", plan(), scope=["hello.py"], pause_after_plan=True)
            runtime = self.engine()
            return runtime, lambda: self.cli_action(runtime, "resume", loaded["id"], "--approve-plan", "--scope", "future/**"), loaded

        lines, first = inspect.getsourcelines(RESUME_LOCKED)
        line = max(index for index, source in enumerate(lines) if source.strip() == "interrupts.deliver()")
        target = RESUME_LOCKED.co_qualname, first + line
        self.assertEqual(self.sweep([RESUME_LOCKED], setup, "completed", selected={target},
                                    repeat=lambda runtime, ident: self.cli_resume(runtime, ident, "--approve-plan", "--scope", "future/**"),
                                    cli_call=True, preflight_delta=self.approval_delta), {"D"})

    def test_sealed_worker_replay(self):
        def setup():
            engine, start, _ = self.start()
            cut, seen = crash_after_save(engine, lambda row: any(
                receipt["phase"] == "sealed" for receipt in row["worker_receipts"].values()))
            with cut, redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt):
                start()
            self.assertTrue(seen["fired"])
            loaded = read_json(engine.directory / "state.json")
            replay = self.engine()
            return replay, lambda: replay.resume(loaded["id"]), loaded

        def replayed(row):
            workers = [entry for entry in row["dispatches"] if entry.get("invoked") and entry["role"] == "worker"]
            self.assertEqual(len(workers), 1)

        window = StageWindow([(TaskLifecycle.replay_worker, None, True)],
                             boundaries=(TaskLifecycle.continue_task,),
                             required=(WorkerReceipts._finish, TaskLifecycle.publish_worker_result,
                                       WorkerReceipts.prepare_consume, WorkerReceipts.apply_consume))
        self.sweep((), setup, "completed", check_baseline=replayed, window=window)

    def test_settlement_worktree_binding(self):
        lines, first = inspect.getsourcelines(START_LOCKED)
        inside = next(index for index, line in enumerate(lines) if "binding = git.add_worktree" in line)
        target = START_LOCKED.co_qualname, first + inside
        self.assertEqual(self.sweep([START_LOCKED], self.start, "completed", selected={target}), {"E/A"})

    def test_settlement_acceptance(self):
        self.sweep([TaskLifecycle._complete_acceptance], self.start, "completed")

    def test_settlement_fresh_verification_checkout(self):
        self.config.data["verification"]["fresh_checkout"] = True
        self.sweep([Verification.verify_fresh_checkout], self.start, "completed")

    def test_scheduler_awaiting_approval(self):
        window = StageWindow([(Scheduler.execute, None, False),
                              (Scheduler._execute_until_terminal, None, False)],
                             landmarks=((Scheduler._execute_until_terminal, 'self.context.run["status"] = "awaiting_approval"'),))
        self.sweep((), lambda: self.start(pause=True, via_cli=True),
                   "awaiting_approval", cli_call=True, window=window)

    def test_scheduler_paused(self):
        window = StageWindow([(Scheduler.execute, None, False),
                              (Scheduler._execute_until_terminal, None, False)],
                             landmarks=((Scheduler._execute_until_terminal, 'self.context.run["status"] = "paused"'),))
        tasks = plan()
        tasks["tasks"].append(dict(tasks["tasks"][0], id="followup", depends_on=["greet"]))

        def edit_followup(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and kwargs["task"]["id"] == "followup":
                with (Path(kwargs["cwd"]) / "hello.py").open("a") as stream:
                    stream.write("# followup\n")

        def paused(row):
            self.assertEqual(set(row["accepted"]), {"greet"})

        self.sweep((), lambda: self.start(ScriptedBackend(edit_followup), step=True, via_cli=True, plan_data=tasks),
                   "paused", cli_call=True, check_baseline=paused, window=window)

    def test_resume_preflight_preserves_audited_exit_confirmation(self):
        def setup():
            previous, start, _ = self.start()
            cut, seen = crash_after_save(previous, lambda row: bool(row["dispatches"]) and
                row["dispatches"][-1]["role"] == "worker" and row["dispatches"][-1]["process_state"] == "prepared")
            with cut, redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt):
                start()
            self.assertTrue(seen["fired"])
            loaded = read_json(previous.directory / "state.json")
            ident = loaded["dispatches"][-1]["id"]
            runtime = self.engine()
            return runtime, lambda: self.cli_action(runtime, "resume", loaded["id"], "--confirm-exited", ident), loaded

        def confirmation_delta(invocation, expected):
            entry = expected["dispatches"][-1]
            self.assertIsNot(entry.get("exit_confirmed"), True)
            row, = invocation.commits
            record = row["dispatches"][-1]["operator_exit_confirmation"]
            self.assertIsNone(record["observed"]["pgid"])
            entry.update(exit_confirmed=True, operator_exit_confirmation=record)
            expected["store_revision"] += 1
            self.assertEqual(row, expected)
            events = [json.loads(line) for line in (invocation.engine.directory / "events.jsonl").read_text().splitlines()]
            audit, = [event for event in events if event["event"] == "dispatch_exit_confirmed_by_operator"]
            self.assertEqual(audit["dispatch_id"], entry["id"])
            self.assertEqual(audit["observed"], record["observed"])

        def repeat(runtime, ident):
            return self.cli_resume(runtime, ident)

        lines, first = inspect.getsourcelines(RESUME_LOCKED)
        last = max(index for index, source in enumerate(lines) if source.strip() == "interrupts.deliver()")
        target = RESUME_LOCKED.co_qualname, first + last
        self.assertEqual(self.sweep([RESUME_LOCKED], setup, "completed", cli_call=True,
                                    selected={target}, repeat=repeat, preflight_delta=confirmation_delta), {"D"})

    def test_signal_during_failed_accounting_publication_keeps_rollback(self):
        point = source_point(Dispatcher.dispatch_once, 'entry["snapshot_digest"] = digest')

        def exercise(inject):
            backend = ScriptedBackend()
            engine, action, _ = self.start(backend, via_cli=True)
            publish = engine.context.publish
            rejected = {}
            fault = HarnessError("injected accounting body error",
                                 stop=stops.make("runtime_failure", "invariant"))

            @contextmanager
            def fail_before_commit(**options):
                row = read_json(engine.directory / "state.json")
                with publish(**options):
                    yield
                    if engine.run["agent_dispatches"] != row["agent_dispatches"]:
                        rejected.update(row)
                        raise fault

            invocation = Invocation(engine, [Dispatcher.dispatch_once], int(inject) or None, selected={point})
            with patch.object(engine.context, "publish", fail_before_commit):
                _, error, stderr = invocation.call(action, cli_call=True)
            self.assertEqual(invocation.events[0][:2], point)
            self.assertEqual(len(invocation.events), 1)
            self.assertEqual((invocation.cli_code, backend.calls), (stops.exit_code(fault), []))
            self.assertIs(fault, error if error is fault else error.__context__)
            self.assertIsNone(invocation.controller.delivered_from)
            self.assertIsNone(invocation.first_eligible)
            self.assertIsNone(invocation.publication)
            self.assertEqual(invocation.controller.count, int(inject))
            if inject:
                self.assertEqual(invocation.controller.first[0], signal.SIGINT)
                self.assertEqual(invocation.deferred, "publication")
                self.assertIn("status=blocked", stderr)
            saved = read_json(engine.directory / "state.json")
            loaded = self.engine(backend)
            loaded.load_run(saved["id"])
            self.assertEqual(loaded.run, saved)
            self.assertEqual(ledger(saved), ledger(rejected))
            self.assertEqual(invocation.commits[-2], rejected)
            self.assertEqual(invocation.commits[-1], saved)
            self.assertEqual(worktree(saved), invocation.terminal_workspace)
            prepared, = saved["dispatches"]
            self.assertEqual((prepared["invoked"], saved["agent_dispatches"], saved["attempts"]), (False, 0, {}))
            with redirect_stderr(io.StringIO()):
                final = loaded.resume(saved["id"])
            self.assertEqual(final["attempts"], {"greet": 1})
            worker, = [row for row in final["dispatches"] if row["role"] == "worker" and row["invoked"]]
            self.assertNotEqual(worker["id"], prepared["id"])
            return lifecycle(final), worktree(final)

        with self.fresh_trial():
            original, original_worktree = exercise(False)
        with self.fresh_trial():
            interrupted, interrupted_worktree = exercise(True)
        self.assertEqual(interrupted, original)
        self.assertTrue(same_workspace(interrupted_worktree, original_worktree))

    def test_unrelated_commit_does_not_prove_a_worker_seal(self):
        point = source_point(Dispatcher.dispatch_once, "self.services.worker_receipts.seal(entry, result)")
        seal = WorkerReceipts.seal

        def omit_seal(receipts, entry, result):
            if interrupts.active().first is not None:
                receipts.context.save()
            else:
                seal(receipts, entry, result)

        with patch.object(WorkerReceipts, "seal", omit_seal), patch.object(
                self, "subTest", lambda **unused: nullcontext()):
            with self.assertRaisesRegex(AssertionError, "invoking.*sealed"):
                self.sweep([Dispatcher.dispatch_once], self.start, "completed", selected={point})

    def test_terminal_worktree_drift_is_detected(self):
        point = source_point(Scheduler.execute, "return self.context.run")
        execute = Scheduler.execute

        def corrupt(scheduler, *args, **kwargs):
            result = execute(scheduler, *args, **kwargs)
            if interrupts.active().first is not None:
                (scheduler.context.worktree / "unexpected-untracked").write_text("terminal mutation")
            return result

        # Observe the original function under the wrapper. subTest normally
        # records the expected failure, so this negative control forwards it.
        with patch.object(Scheduler, "execute", corrupt), patch.object(
                self, "subTest", lambda **unused: nullcontext()):
            with self.assertRaisesRegex(AssertionError, "unexpected-untracked"):
                self.sweep([execute], lambda: self.start(via_cli=True), "completed",
                           selected={point}, cli_call=True)
