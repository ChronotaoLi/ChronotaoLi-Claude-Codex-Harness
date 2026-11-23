import contextlib
import dis
import errno
import gc
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import git, process_runtime, util
from harness.command_server import Broker, command_id, create_manifest
from harness.process_runtime import ProcessOutcome
from harness.util import (DIRECTORY_FLAGS, CommandDirectoryError, HarnessError, ProcessTimeout, read_json,
                          run_process)
from harness.verification import Verification


REPORT = "import os; s = os.stat('.'); print(s.st_dev, s.st_ino)"


def identity(path):
    info = os.stat(path, follow_symlinks=False)
    return info.st_dev, info.st_ino


def closed(descriptor):
    try:
        os.fstat(descriptor)
    except OSError as error:
        return error.errno == errno.EBADF
    return False


def is_directory(descriptor):
    # Linux O_TMPFILE includes O_DIRECTORY but opens a regular file.
    return stat.S_ISDIR(os.fstat(descriptor).st_mode)


@contextlib.contextmanager
def recorded_opens(*, directories_only, into=None):
    """Record descriptors opened or duplicated inside the block."""
    real_open, real_dup = os.open, os.dup
    opened = [] if into is None else into

    def tracked_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if not directories_only or is_directory(descriptor):
            opened.append(descriptor)
        return descriptor

    def tracked_dup(descriptor):
        duplicate = real_dup(descriptor)
        if not directories_only or is_directory(duplicate):
            opened.append(duplicate)
        return duplicate

    with patch("harness.util.os.open", side_effect=tracked_open), \
            patch("harness.util.os.dup", side_effect=tracked_dup):
        yield opened


@contextlib.contextmanager
def interrupting():
    """A real SIGINT raises KeyboardInterrupt in the block, whatever handler the runner installed."""
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


@contextlib.contextmanager
def collector_paused():
    """No garbage collection in the block: nothing is closed by being collected."""
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


def interruption(action):
    """Run ``action``, which an interrupt must end, and return that interrupt.

    Unlike assertRaises, which clears the frames, this keeps the traceback: every
    interrupted frame, and what it holds, stays alive while the caller keeps the result.
    """
    try:
        action()
    except KeyboardInterrupt as error:
        return error
    raise AssertionError("no interrupt ended the action")


@contextlib.contextmanager
def interrupted_cleanup(at):
    """Send one real SIGINT as the first holder cleanup begins, at its entry or just before it defers signals."""
    real_close_all, real_mask, sent = util.Descriptors.close_all, signal.pthread_sigmask, []

    def close_all(held):
        if at == "entry" and not sent:
            sent.append(at)
            signal.raise_signal(signal.SIGINT)
        return real_close_all(held)

    def mask(how, signals):
        if (at == "before the mask" and how == signal.SIG_BLOCK and not sent
                and any(frame.f_code is real_close_all.__code__ for frame, _ in traceback.walk_stack(None))):
            sent.append(at)
            signal.raise_signal(signal.SIGINT)
        return real_mask(how, signals)

    with interrupting(), collector_paused(), patch.object(util.Descriptors, "close_all", close_all), \
            patch.object(signal, "pthread_sigmask", mask):
        yield sent


def holder_codes():
    """The descriptor holder's own steps and the cleanups around it (absent before descriptors had a holder)."""
    holder, entry = getattr(util, "Descriptors", None), getattr(util, "with_descriptors", None)
    codes = set() if holder is None else {value.__code__ for value in vars(holder).values() if hasattr(value, "__code__")}
    return codes | (set() if entry is None else {entry.__code__})


def nested_code(function, name):
    """The code of the function ``name`` that ``function`` defines (none where it has no such function)."""
    return {code for code in function.__code__.co_consts if getattr(code, "co_name", None) == name}


def traced(codes, on_line, action):
    """Run ``action`` calling ``on_line()`` before each line it executes inside ``codes``.

    A signal is acted on only where the interpreter checks for one (calls, loops, function
    entry). Lines that begin with an instruction no check can precede (a ``try:`` NOP, an
    exception handler's entry, a with statement's exit, the binding of a with statement's
    target on 3.11 to 3.13) are skipped: an exception raised there by a trace function
    bypasses handlers in ways no signal can.
    """
    def unchecked(code):
        offsets, previous = set(), None
        for instruction in dis.get_instructions(code):
            if instruction.opname in ("NOP", "PUSH_EXC_INFO", "SWAP") or previous == "BEFORE_WITH":
                offsets.add(instruction.offset)
            previous = instruction.opname
        return offsets

    unchecked = {code: unchecked(code) for code in codes}

    def trace(frame, event, arg):
        if frame.f_code not in codes:
            return None
        if event == "line" and frame.f_lasti not in unchecked[frame.f_code]:
            on_line()
        return trace

    sys.settrace(trace)
    try:
        return action()
    finally:
        sys.settrace(None)


class InterruptSweep:
    def sweep(self, codes, scenario):
        """Send a real SIGINT before each line ``scenario`` runs in ``codes``, one run per line.

        ``scenario(opened)`` collects the descriptors it opens. Whether the interrupt ends the
        scenario or is absorbed and recorded by it, none of them may stay open once it returns,
        while the caller still holds the interrupt and nothing has been collected.
        """
        lines = []
        traced(codes, lambda: lines.append(None), lambda: scenario([]))
        self.assertGreater(len(lines), 0)
        for moment in range(len(lines)):
            seen, opened, retained = [], [], None

            def on_line():
                seen.append(None)
                if len(seen) == moment + 1:
                    signal.raise_signal(signal.SIGINT)

            # On 3.11 an interrupt just after a capture file opens, before its with statement takes
            # it, leaves the file to the collector; files are not what this sweep checks.
            with interrupting(), warnings.catch_warnings(), collector_paused():
                warnings.simplefilter("ignore", ResourceWarning)
                try:
                    traced(codes, on_line, lambda: scenario(opened))
                except KeyboardInterrupt as error:
                    # Kept through the checks: no interrupted frame, nor what it holds, is released.
                    retained = error
                self.assertIsNone(sys.exc_info()[1])
                self.assertGreater(len(seen), moment, f"line {moment} of {len(lines)} was not reached")
                self.assertEqual([descriptor for descriptor in opened if not closed(descriptor)], [],
                                 f"open after an interrupt at line {moment} of {len(lines)}")
                retained = None


class CommandDirectoryLaunchTests(InterruptSweep, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root, self.outside = self.base / "root", self.base / "outside"
        (self.root / "sub").mkdir(parents=True)
        (self.root / "a" / "b").mkdir(parents=True)
        self.outside.mkdir()
        self.root_fd = os.open(self.root, DIRECTORY_FLAGS)
        self.addCleanup(os.close, self.root_fd)
        self.marker = self.base / "ran"

    def launch(self, relative, name="command", *, script=REPORT, cwd=None, **kwargs):
        log = self.base / "logs" / name
        code = run_process([sys.executable, "-c", script], cwd or self.root, log, relative_cwd=relative,
                           root_fd=self.root_fd, timeout=30, **kwargs)
        return code, log

    def reported(self, log):
        dev, ino = log.with_suffix(".stdout").read_text().split()
        return int(dev), int(ino)

    def refused(self, relative, name):
        """Launch a marker-writing child that must not start; return the error and its log."""
        with self.assertRaises(CommandDirectoryError) as caught:
            self.launch(relative, name, script=f"open({str(self.marker)!r}, 'w')")
        self.assertFalse(self.marker.exists())
        facts = caught.exception.outcome
        self.assertFalse(facts.started)
        self.assertEqual(facts.startup_state, "not_started")
        self.assertEqual(facts.termination_reason, "start_failed")
        self.assertIsNone(facts.exit_code)
        self.assertTrue(facts.exit_confirmed)
        self.assertTrue(facts.evidence_complete)
        log = self.base / "logs" / name
        saved = read_json(log.with_suffix(".outcome.json"))
        self.assertEqual((saved["started"], saved["termination_reason"], saved["exit_confirmed"],
                          saved["evidence_complete"]), (False, "start_failed", True, True))
        self.assertEqual(saved["errors"][0]["type"], "CommandDirectoryError")
        self.assertIn(str(caught.exception), log.with_suffix(".stderr").read_text())
        return caught.exception, log

    def test_child_starts_in_the_directory_opened_from_the_root_and_the_root_stays_open(self):
        # The path argument is not used for the directory: the child enters what was opened from root_fd.
        code, log = self.launch("sub", "sub", cwd=self.outside)
        self.assertEqual(code, 0)
        self.assertEqual(self.reported(log), identity(self.root / "sub"))
        code, log = self.launch("a/b", "nested")
        self.assertEqual(code, 0)
        self.assertEqual(self.reported(log), identity(self.root / "a" / "b"))
        code, log = self.launch(".", "root")
        self.assertEqual(code, 0)
        self.assertEqual(self.reported(log), identity(self.root))
        info = os.fstat(self.root_fd)
        self.assertEqual((info.st_dev, info.st_ino), identity(self.root))

    def test_relative_cwd_and_root_fd_alone_are_programming_errors_with_no_side_effects(self):
        log = self.base / "logs" / "alone"
        for arguments in ({"relative_cwd": "sub"}, {"root_fd": self.root_fd}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                run_process([sys.executable, "-c", "pass"], self.root, log, **arguments)
        self.assertFalse(log.with_suffix(".outcome.json").exists())
        self.assertFalse(log.with_suffix(".stdout").exists())

    def test_missing_file_and_symlinked_directories_do_not_start_and_record_the_failing_segment(self):
        (self.root / "file").write_text("not a directory\n")
        (self.root / "inner_link").symlink_to(self.root / "sub")
        (self.root / "outer_link").symlink_to(self.outside)
        (self.root / "via").symlink_to(self.root / "a")
        cases = {"absent": "absent", "file": "file", "inner_link": "inner_link", "outer_link": "outer_link",
                 "via/b": "via", "sub/absent": "sub/absent", "a/b/c": "a/b/c"}
        for index, (relative, segment) in enumerate(cases.items()):
            with self.subTest(relative=relative):
                error, log = self.refused(relative, f"bad{index}")
                self.assertIn(repr(relative), str(error))
                self.assertIn(repr(segment), str(error))

    def test_runtime_refuses_escaping_or_unencodable_directories_even_without_entry_checks(self):
        for index, relative in enumerate(("..", "../outside", "/abs", str(self.outside), "", "a//b", "sub/",
                                          "./sub", "a/./b", "a/../sub", "\ud800", "sub/\0")):
            with self.subTest(relative=repr(relative)):
                self.refused(relative, f"escape{index}")

    def test_directory_without_search_permission_fails_in_preexec_and_does_not_start(self):
        if os.geteuid() == 0:
            self.skipTest("root bypasses directory permissions")
        locked = self.root / "a" / "b"
        os.chmod(locked, 0o600)
        self.addCleanup(os.chmod, locked, 0o700)
        error, _ = self.refused("a/b", "locked")
        self.assertIsInstance(error.__cause__, subprocess.SubprocessError)

    def test_swapping_the_opened_directory_for_an_outside_symlink_does_not_redirect_the_child(self):
        verified = identity(self.root / "sub")
        real = subprocess.Popen

        def swap_then_start(*args, **kwargs):
            os.rename(self.root / "sub", self.root / "sub-moved")
            os.symlink(self.outside, self.root / "sub")
            return real(*args, **kwargs)

        with patch("harness.util.subprocess.Popen", side_effect=swap_then_start):
            code, log = self.launch("sub")
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "sub").is_symlink())
        self.assertEqual(self.reported(log), verified)
        self.assertNotEqual(self.reported(log), identity(self.outside))

    def test_deadline_passing_while_the_directory_opens_starts_nothing(self):
        real_open, real_clock = util.open_command_directory, time.monotonic
        skew, held, launches = [0.0], [], []

        def slow_open(*args):
            held.append(real_open(*args))
            skew[0] = 3600.0
            return held[-1]

        with patch("harness.util.open_command_directory", side_effect=slow_open), \
                patch("harness.util.time.monotonic", side_effect=lambda: real_clock() + skew[0]), \
                patch("harness.util.subprocess.Popen", side_effect=lambda *a, **k: launches.append(a)):
            with self.assertRaises(ProcessTimeout) as caught:
                self.launch("sub")
        facts = caught.exception.outcome
        self.assertEqual(launches, [])
        self.assertFalse(facts.started)
        self.assertEqual(facts.termination_reason, "timeout")
        self.assertTrue(facts.exit_confirmed)
        self.assertTrue(closed(held[0]))

    def test_descriptors_opened_for_a_command_are_closed_on_success_and_on_each_failure(self):
        real = subprocess.Popen
        at_launch = []

        def observe_then_start(*args, **kwargs):
            at_launch.extend(closed(descriptor) for descriptor in opened)
            return real(*args, **kwargs)

        with recorded_opens(directories_only=True) as opened, \
                patch("harness.util.subprocess.Popen", side_effect=observe_then_start):
            code, _ = self.launch("a/b", "nested")
        self.assertEqual(code, 0)
        # Intermediate levels close as the walk descends; the last is held only until Popen returns.
        self.assertEqual(len(opened), 2)
        self.assertEqual(at_launch, [True, False])
        self.assertTrue(all(map(closed, opened)))

        with recorded_opens(directories_only=True) as opened:
            self.launch(".", "root")
        self.assertEqual(len(opened), 1)
        self.assertTrue(closed(opened[0]))

        with recorded_opens(directories_only=True) as opened, self.assertRaises(CommandDirectoryError):
            self.launch("a/absent", "segment")
        self.assertEqual(len(opened), 1)
        self.assertTrue(closed(opened[0]))

        if os.geteuid() != 0:
            os.chmod(self.root / "a" / "b", 0o600)
            self.addCleanup(os.chmod, self.root / "a" / "b", 0o700)
            with recorded_opens(directories_only=True) as opened, self.assertRaises(CommandDirectoryError):
                self.launch("a/b", "preexec")
            self.assertEqual(len(opened), 2)
            self.assertTrue(all(map(closed, opened)))
        info = os.fstat(self.root_fd)
        self.assertEqual((info.st_dev, info.st_ino), identity(self.root))

    @contextlib.contextmanager
    def failing_directory_close(self):
        """The command directory's first close releases it and then fails; yields (opened, closes)."""
        real_open, real_close = util.open_command_directory, os.close
        opened, closes = [], []

        def tracked_open(*args):
            opened.append(real_open(*args))
            return opened[-1]

        def failing_close(descriptor):
            if descriptor not in opened:
                return real_close(descriptor)
            closes.append(descriptor)
            real_close(descriptor)
            if len(closes) == 1:
                raise OSError(errno.EINTR, "injected close failure after release")

        with patch("harness.util.open_command_directory", side_effect=tracked_open), \
                patch("harness.util.os.close", side_effect=failing_close):
            yield opened, closes

    def test_a_directory_close_failing_after_start_leaves_the_child_supervised(self):
        real, children = subprocess.Popen, []

        def start(*args, **kwargs):
            children.append(real(*args, **kwargs))
            return children[-1]

        try:
            with self.failing_directory_close() as (opened, closes), \
                    patch("harness.util.subprocess.Popen", side_effect=start), \
                    self.assertRaises(OSError) as caught:
                self.launch("sub", script="import time; time.sleep(30)")
        finally:
            for child in children:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
        # The close's own error, raised once: the descriptor is not closed a second time.
        self.assertEqual(caught.exception.errno, errno.EINTR)
        self.assertEqual(closes, opened)
        # The supervisor owned the child before the close and ended its group.
        facts = caught.exception.outcome
        self.assertTrue(facts.started)
        self.assertEqual(facts.pid, children[0].pid)
        self.assertTrue(facts.exit_confirmed)
        self.assertIsNotNone(children[0].returncode)

    def test_a_directory_close_failing_after_a_launch_error_keeps_that_error(self):
        def refused(*args, **kwargs):
            raise subprocess.SubprocessError("Exception occurred in preexec_fn.")

        with self.failing_directory_close() as (opened, closes), \
                patch("harness.util.subprocess.Popen", side_effect=refused), \
                self.assertRaises(CommandDirectoryError) as caught:
            self.launch("sub")
        self.assertEqual(closes, opened)
        facts = caught.exception.outcome
        self.assertFalse(facts.started)
        self.assertEqual(facts.termination_reason, "start_failed")
        self.assertTrue(facts.exit_confirmed)

    def test_an_interrupt_as_each_directory_descriptor_is_handed_over_leaves_none_open(self):
        real_open, real_dup = os.open, os.dup
        for relative, levels in ((".", 1), ("sub", 1), ("a/b", 2)):
            for level in range(levels):
                with self.subTest(relative=relative, level=level):
                    opened = []

                    def handed(descriptor):
                        # The descriptor exists but its caller has not stored it yet.
                        opened.append(descriptor)
                        if len(opened) == level + 1:
                            signal.raise_signal(signal.SIGINT)
                        return descriptor

                    with interrupting(), \
                            patch("harness.util.os.open", side_effect=lambda *a, **k: handed(real_open(*a, **k))), \
                            patch("harness.util.os.dup", side_effect=lambda d: handed(real_dup(d))), \
                            self.assertRaises(KeyboardInterrupt):
                        util.with_descriptors(lambda held: util.open_command_directory(held, self.root_fd, relative))
                    # The interrupt takes effect as that level is registered: no later level opens.
                    self.assertEqual(len(opened), level + 1)
                    self.assertTrue(all(map(closed, opened)))
                    self.assertFalse(closed(self.root_fd))

    def test_an_interrupt_at_each_launch_handoff_leaves_no_directory_descriptor_open(self):
        real_open, real_popen = util.open_command_directory, subprocess.Popen
        opened, children = [], []

        def interrupted_after_open(*args):
            opened.append(real_open(*args))
            signal.raise_signal(signal.SIGINT)
            return opened[-1]

        def interrupted_after_start(*args, **kwargs):
            children.append(real_popen(*args, **kwargs))
            signal.raise_signal(signal.SIGINT)
            return children[-1]

        with self.subTest("opened, not yet held by run_process"):
            with interrupting(), patch("harness.util.open_command_directory", side_effect=interrupted_after_open), \
                    patch("harness.util.subprocess.Popen", side_effect=lambda *a, **k: children.append(a)), \
                    self.assertRaises(KeyboardInterrupt) as caught:
                self.launch("sub", "after-open")
            self.assertEqual(children, [])
            self.assertTrue(closed(opened[0]))
            facts = caught.exception.outcome
            self.assertEqual((facts.started, facts.startup_state, facts.termination_reason, facts.exit_confirmed),
                             (False, "not_started", "cancelled", True))
        opened.clear()
        with self.subTest("child created, not yet owned by the supervisor"):
            try:
                with interrupting(), recorded_opens(directories_only=True) as opened, \
                        patch("harness.util.subprocess.Popen", side_effect=interrupted_after_start), \
                        self.assertRaises(KeyboardInterrupt) as caught:
                    self.launch("sub", "after-start", script="import time; time.sleep(30)")
            finally:
                for child in children:
                    if child.poll() is None:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=5)
            self.assertEqual(len(opened), 1)
            self.assertTrue(closed(opened[0]))
            facts = caught.exception.outcome
            self.assertEqual((facts.started, facts.termination_reason, facts.exit_confirmed), (True, "cancelled", True))
        self.assertFalse(closed(self.root_fd))

    def test_an_interrupt_at_any_line_of_a_directory_walk_leaves_nothing_open(self):
        # The walk, the holder's steps, its cleanups and their signal deferral, line by line.
        codes = holder_codes() | {util.open_command_directory.__code__,
                                  process_runtime.defer_cleanup_signals.__wrapped__.__code__}
        for relative in (".", "a/b"):
            def scenario(opened, relative=relative):
                with recorded_opens(directories_only=True, into=opened):
                    util.with_descriptors(lambda held: util.open_command_directory(held, self.root_fd, relative))

            with self.subTest(relative=relative):
                self.sweep(codes, scenario)
        self.assertFalse(closed(self.root_fd))

    def test_an_interrupt_at_any_line_of_a_launch_or_its_cleanup_leaves_no_directory_open(self):
        # Through run_process alone, whose interface did not change: a started child, and a
        # start that fails after the directory opened, whose cleanup is then interrupted.
        codes = holder_codes() | nested_code(util.run_process, "start") | {
            util.run_process.__code__, util.open_command_directory.__code__}
        runs = iter(range(1 << 30))
        for name, argv in (("started", [sys.executable, "-c", "pass"]), ("start failed", [str(self.base / "missing")])):
            def scenario(opened, argv=argv):
                with recorded_opens(directories_only=True, into=opened), contextlib.suppress(util.ProcessStartError):
                    run_process(argv, self.root, self.base / "logs" / f"sweep{next(runs)}", relative_cwd="a/b",
                                root_fd=self.root_fd, timeout=30)

            with self.subTest(name):
                self.sweep(codes, scenario)
        self.assertFalse(closed(self.root_fd))

    def test_an_interrupt_while_the_directory_opens_takes_effect_before_the_next_level(self):
        real_open, opened, launches = os.open, [], []

        def interrupted_at_first_level(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if is_directory(descriptor):
                opened.append((path, descriptor))
            if path == "a":
                signal.raise_signal(signal.SIGINT)
            return descriptor

        with interrupting(), patch("harness.util.os.open", side_effect=interrupted_at_first_level), \
                patch("harness.util.subprocess.Popen", side_effect=lambda *a, **k: launches.append(a)), \
                self.assertRaises(KeyboardInterrupt) as caught:
            self.launch("a/b", "walk")
        # As before directories were opened at all: cancellation is not held back by the walk.
        self.assertEqual([path for path, _ in opened], ["a"])
        self.assertTrue(closed(opened[0][1]))
        self.assertEqual(launches, [])
        facts = caught.exception.outcome
        self.assertEqual((facts.started, facts.termination_reason, facts.exit_confirmed), (False, "cancelled", True))

    def test_a_launch_inside_an_except_block_is_still_cancelled_by_an_interrupt_held_while_opening(self):
        real_open, opened = os.open, []

        def interrupted_at_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if is_directory(descriptor):
                opened.append(descriptor)
            if path == "sub":
                signal.raise_signal(signal.SIGINT)
            return descriptor

        with interrupting(), patch("harness.util.os.open", side_effect=interrupted_at_open):
            try:
                raise ValueError("an error the caller is handling")
            except ValueError:
                with self.assertRaises(KeyboardInterrupt) as caught:
                    self.launch("sub", "inside-except", script=f"open({str(self.marker)!r}, 'w')")
        self.assertFalse(self.marker.exists())
        self.assertEqual(len(opened), 1)
        self.assertTrue(closed(opened[0]))
        facts = caught.exception.outcome
        self.assertEqual((facts.started, facts.termination_reason, facts.exit_confirmed), (False, "cancelled", True))

    def test_an_interrupt_as_a_launch_s_cleanup_begins_leaves_no_directory_open_while_the_caller_holds_it(self):
        def refused(*args, **kwargs):
            raise FileNotFoundError(errno.ENOENT, "injected launch failure")

        # A failed start leaves its directory to the cleanup; a started child's directory closed before it.
        for outcome, at, launcher in (("start failed", "entry", refused), ("start failed", "before the mask", refused),
                                      ("started", "entry", subprocess.Popen)):
            with self.subTest(outcome, at=at), interrupted_cleanup(at) as sent, \
                    recorded_opens(directories_only=True) as opened, \
                    patch("harness.util.subprocess.Popen", side_effect=launcher):
                caught = interruption(lambda: self.launch("a/b", f"{outcome}-{at}"))
                self.assertEqual(sent, [at])
                self.assertEqual(len(opened), 2)
                self.assertEqual([descriptor for descriptor in opened if not closed(descriptor)], [])
                facts = caught.outcome
                self.assertEqual((facts.started, facts.termination_reason), (outcome == "started", "cancelled"))
                if outcome == "started":
                    self.assertTrue(facts.exit_confirmed)
                else:
                    self.assertIsInstance(caught.__context__, util.ProcessStartError)
        self.assertFalse(closed(self.root_fd))


class DescriptorHolderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name

    def test_closing_again_never_closes_a_number_reused_since(self):
        for first in ("close", "close_all"):
            with self.subTest(first=first):
                reused = []

                def use(held):
                    descriptor = held.open(self.directory, DIRECTORY_FLAGS)
                    if first == "close":
                        held.close(descriptor)
                    else:
                        held.close_all()
                    # The lowest free number: the one just closed.
                    reused.append(os.open(self.directory, DIRECTORY_FLAGS))
                    self.assertEqual(reused[0], descriptor)
                    held.close_all()
                    self.assertFalse(closed(reused[0]))

                try:
                    # Its two cleanups close nothing more either.
                    util.with_descriptors(use)
                    self.assertFalse(closed(reused[0]))
                finally:
                    for descriptor in reused:
                        os.close(descriptor)

    def test_a_holder_dropped_without_its_cleanup_closes_nothing(self):
        # Only misuse leaves a holder uncleaned, so this one is made without with_descriptors.
        held, revived = util.Descriptors(), []
        descriptor = held.open(self.directory, DIRECTORY_FLAGS)
        self.addCleanup(lambda: closed(descriptor) or os.close(descriptor))

        class Peer:
            def __del__(self):
                revived.append(self.holder)

        # A cycle the collector finalizes while its other member revives the holder.
        peer = Peer()
        peer.holder, held.peer = held, peer
        del peer, held
        gc.collect()
        self.assertEqual(len(revived), 1)
        self.assertFalse(closed(descriptor))
        # Still registered, so its own cleanup closes it, once: a number reused since stays open.
        revived[0].close_all()
        self.assertTrue(closed(descriptor))
        reused = os.open(self.directory, DIRECTORY_FLAGS)
        self.addCleanup(os.close, reused)
        self.assertEqual(reused, descriptor)
        revived[0].close_all()
        self.assertFalse(closed(reused))

    def test_closing_finishes_every_descriptor_once_and_then_raises_the_interrupt_it_held(self):
        root = os.open(self.directory, DIRECTORY_FLAGS)
        self.addCleanup(os.close, root)
        descriptors = []
        real_close, closes = os.close, []

        def interrupted_close(descriptor):
            closes.append(descriptor)
            real_close(descriptor)
            if len(closes) == 1:
                signal.raise_signal(signal.SIGINT)
            elif len(closes) == 2:
                raise OSError(errno.EIO, "injected close failure after release")

        with interrupting(), patch("harness.util.os.close", side_effect=interrupted_close):
            try:
                raise ValueError("an error the caller is handling")
            except ValueError:
                # The close completed, so the interrupt it held surfaces; the failed close does not,
                # and the second cleanup closes nothing again.
                with self.assertRaises(KeyboardInterrupt):
                    util.with_descriptors(lambda held: descriptors.extend(held.dup(root) for _ in range(3)))
        self.assertEqual(len(descriptors), 3)
        self.assertEqual(sorted(closes), sorted(descriptors))
        self.assertTrue(all(map(closed, descriptors)))
        self.assertFalse(closed(root))


class BoundRootTests(InterruptSweep, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        repo = self.base / "repo"
        repo.mkdir()
        git.git(repo, "init", "-b", "main")
        (repo / "a.txt").write_text("original\n")
        git.git(repo, "add", "a.txt")
        git.git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        run, checkout = self.base / "worktrees" / "run", self.base / "checkouts" / "verify"
        git.git(repo, "worktree", "add", "-b", "harness/run", str(run))
        git.git(repo, "worktree", "add", "--detach", str(checkout), "HEAD")
        self.cases = {"run branch": (run, git.capture_worktree_binding(run, "harness/run")),
                      "fresh checkout": (checkout, git.capture_worktree_binding(checkout, None))}

    def assert_binding_changed(self, path, binding):
        with recorded_opens(directories_only=False) as opened, self.assertRaises(HarnessError) as caught:
            util.with_descriptors(lambda held: git.open_bound_root(held, path, binding))
        stop = caught.exception.stop
        self.assertEqual((stop["kind"], stop["reason"]), ("workspace_mismatch", "binding_changed"))
        self.assertTrue(all(map(closed, opened)))

    def verify(self, path, binding, name, started):
        """Run one formal check on ``path``; the stand-in for its process records the root it was given."""
        context = SimpleNamespace(directory=self.base, run={"dispatches": []}, ensure_exited=lambda: None, save=lambda: None,
                                  save_fact=lambda *keys: True,
                                  config=SimpleNamespace(data={"runtime": {"verify_timeout_seconds": 5}}))

        def process(argv, cwd, log, **kwargs):
            started.append(kwargs["root_fd"])
            kwargs["on_outcome"](ProcessOutcome(started=True, startup_state="started", exit_code=0, exit_confirmed=True))
            return 0

        with patch("harness.verification.run_process", process):
            return Verification(context).run_verification_commands(
                "task", [{"cwd": ".", "argv": ["check"]}], path, self.base / "verification" / "checks" / name, binding)

    @contextlib.contextmanager
    def displaced(self, path):
        """Move the bound directory aside for the block; the caller may put anything on its path."""
        moved = path.with_name(path.name + "-moved")
        os.rename(path, moved)
        try:
            yield moved
        finally:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                os.rmdir(path)
            os.rename(moved, path)

    def test_open_bound_root_returns_the_checked_root(self):
        for name, (path, binding) in self.cases.items():
            with self.subTest(name):
                def check(held):
                    with recorded_opens(directories_only=False) as opened:
                        root = git.open_bound_root(held, path, binding)
                    info = os.fstat(root)
                    self.assertEqual((info.st_dev, info.st_ino), identity(path))
                    self.assertEqual(opened[0], root)
                    self.assertTrue(all(map(closed, opened[1:])))
                    return root

                self.assertTrue(closed(util.with_descriptors(check)))

    def test_plain_symlinked_or_missing_root_is_binding_changed(self):
        for name, (path, binding) in self.cases.items():
            for replacement in ("plain directory", "symlink to the bound directory", "missing"):
                with self.subTest(name, replacement=replacement), self.displaced(path) as moved:
                    if replacement == "plain directory":
                        path.mkdir()
                    elif replacement == "symlink to the bound directory":
                        path.symlink_to(moved)
                    self.assert_binding_changed(path, binding)

    def test_gitfile_that_is_not_a_regular_file_is_refused_without_blocking(self):
        path, binding = self.cases["run branch"]
        os.unlink(path / ".git")
        os.mkfifo(path / ".git")

        def blocked(signum, frame):
            raise AssertionError("open_bound_root blocked on a FIFO without a writer")

        previous = signal.signal(signal.SIGALRM, blocked)
        signal.alarm(10)
        try:
            self.assert_binding_changed(path, binding)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        os.unlink(path / ".git")
        (path / ".git").mkdir()
        self.assert_binding_changed(path, binding)

    def test_check_and_launch_share_one_opened_root(self):
        real = git._check_binding
        for name, (path, binding) in self.cases.items():
            bound = identity(path)
            # The reviewed timing: an unbound directory sits on the path when the root is opened and
            # the bound one only while the path-based part of the check runs.
            with self.subTest(name, timing="bound only during the check"), self.displaced(path) as moved:
                path.mkdir()
                parked = path.with_name(path.name + "-impostor")

                def bound_during_check(*args):
                    os.rename(path, parked)
                    os.rename(moved, path)
                    try:
                        return real(*args)
                    finally:
                        os.rename(path, moved)
                        os.rename(parked, path)

                with patch("harness.git._check_binding", side_effect=bound_during_check):
                    self.assert_binding_changed(path, binding)
            # The reverse: the bound directory is opened, an unbound one sits on the path during the check.
            with self.subTest(name, timing="unbound only during the check"):
                parked = path.with_name(path.name + "-moved")
                impostor = path.with_name(path.name + "-impostor")
                impostor.mkdir()

                def unbound_during_check(*args):
                    os.rename(path, parked)
                    os.rename(impostor, path)
                    try:
                        return real(*args)
                    finally:
                        os.rename(path, impostor)
                        os.rename(parked, path)

                def check(held):
                    with patch("harness.git._check_binding", side_effect=unbound_during_check):
                        root = git.open_bound_root(held, path, binding)
                    info = os.fstat(root)
                    self.assertEqual((info.st_dev, info.st_ino), bound)
                    self.assertNotEqual((info.st_dev, info.st_ino), identity(impostor))

                try:
                    util.with_descriptors(check)
                finally:
                    os.rmdir(impostor)

    def test_an_interrupt_as_the_root_or_gitfile_descriptor_is_handed_over_leaves_none_open(self):
        real_open, real_git = os.open, git._run_git
        for name, (path, binding) in self.cases.items():
            for moment in ("root opened", ".git opened", "during the Git check"):
                with self.subTest(name, moment=moment):
                    opened = []

                    def handed(target, flags, *args, **kwargs):
                        # The descriptor exists but its caller has not stored it yet.
                        opened.append(real_open(target, flags, *args, **kwargs))
                        if (target, moment) in ((binding["worktree"], "root opened"), (".git", ".git opened")):
                            signal.raise_signal(signal.SIGINT)
                        return opened[-1]

                    def interrupted_git(*args, **kwargs):
                        if moment == "during the Git check":
                            signal.raise_signal(signal.SIGINT)
                        return real_git(*args, **kwargs)

                    with interrupting(), patch("harness.git.os.open", side_effect=handed), \
                            patch("harness.git._run_git", side_effect=interrupted_git), \
                            self.assertRaises(KeyboardInterrupt):
                        util.with_descriptors(lambda held: git.open_bound_root(held, path, binding))
                    self.assertGreaterEqual(len(opened), 1)
                    self.assertTrue(all(map(closed, opened)))

    def test_check_worktree_binding_reads_the_gitfile_its_first_resolution_found(self):
        # As before open_bound_root existed: the caller's link changing after the first
        # resolve() does not change what is read, so the check still accepts.
        real_resolve = Path.resolve

        def open_bound_root(path, binding):
            util.with_descriptors(lambda held: git.open_bound_root(held, path, binding))

        for index, (name, (path, binding)) in enumerate(self.cases.items()):
            for check in (git.check_worktree_binding, open_bound_root):
                with self.subTest(name, check=check.__name__):
                    alias, empty = self.base / f"alias-{index}-{check.__name__}", self.base / f"empty-{index}-{check.__name__}"
                    empty.mkdir()
                    alias.symlink_to(path)
                    retargeted = []

                    def retarget_after_first(own, *args, **kwargs):
                        resolved = real_resolve(own, *args, **kwargs)
                        if own == alias and not retargeted:
                            alias.unlink()
                            alias.symlink_to(empty)
                            retargeted.append(resolved)
                        return resolved

                    with patch.object(Path, "resolve", retarget_after_first):
                        check(alias, binding)
                    self.assertEqual(retargeted, [path])
                    self.assertEqual(alias.resolve(), empty)

    def test_an_interrupt_at_any_line_of_opening_the_root_for_a_check_leaves_it_closed(self):
        # Through formal verification, whose interface did not change: the root's open, the
        # check on it, the hand-over to the command and the cleanup, line by line.
        commands = Verification.run_verification_commands
        codes = holder_codes() | nested_code(commands, "run_check") | {
            git.open_bound_root.__wrapped__.__code__, git._binding_file_at.__code__, git._check_binding.__code__,
            commands.__code__}
        runs = iter(range(1 << 30))
        for name, (path, binding) in self.cases.items():
            def scenario(opened, path=path, binding=binding):
                with recorded_opens(directories_only=False, into=opened):
                    self.verify(path, binding, f"sweep{next(runs)}", [])

            with self.subTest(name):
                self.sweep(codes, scenario)

    def test_an_interrupt_as_a_passed_check_s_cleanup_begins_leaves_its_root_closed_while_the_caller_holds_it(self):
        runs = iter(range(1 << 30))
        for name, (path, binding) in self.cases.items():
            for at in ("entry", "before the mask"):
                with self.subTest(name, at=at), interrupted_cleanup(at) as sent, \
                        recorded_opens(directories_only=False) as opened:
                    started = []
                    caught = interruption(lambda: self.verify(path, binding, f"cleanup{next(runs)}", started))
                    self.assertEqual(sent, [at])
                    # The command ran from the root, which only the cleanup closes.
                    self.assertEqual(len(started), 1)
                    self.assertIn(started[0], opened)
                    self.assertEqual([descriptor for descriptor in opened if not closed(descriptor)], [])
                    self.assertIsNotNone(caught.__traceback__)

    def test_an_interrupt_while_the_gitfile_is_read_takes_effect_at_once_as_in_a_read_by_path(self):
        path, binding = self.cases["run branch"]
        real_fdopen, real_read_bytes, went_on, started = os.fdopen, Path.read_bytes, [], []

        class InterruptedRead:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self.stream.__exit__(*exc)

            def read(self):
                signal.raise_signal(signal.SIGINT)
                # Reached only if the interrupt was held back and the read went on.
                went_on.append("through the root")
                return self.stream.read()

        def read_bytes(own):
            if own.name == ".git":
                signal.raise_signal(signal.SIGINT)
                went_on.append("by path")
            return real_read_bytes(own)

        with interrupting(), recorded_opens(directories_only=False) as opened:
            with patch("harness.git.os.fdopen", side_effect=lambda *a, **k: InterruptedRead(real_fdopen(*a, **k))), \
                    self.assertRaises(KeyboardInterrupt):
                self.verify(path, binding, "read", started)
            # The path-based check, unchanged, is the baseline.
            with patch.object(Path, "read_bytes", read_bytes), self.assertRaises(KeyboardInterrupt):
                git.check_worktree_binding(path, binding)
        self.assertEqual(went_on, [])
        self.assertEqual(started, [])
        self.assertTrue(all(map(closed, opened)))

    def test_a_check_inside_an_except_block_is_still_cancelled_by_an_interrupt_held_while_opening_the_root(self):
        path, binding = self.cases["run branch"]
        real_open, opened, started = os.open, [], []

        def interrupted_at_root(target, flags, *args, **kwargs):
            opened.append(real_open(target, flags, *args, **kwargs))
            if target == binding["worktree"]:
                signal.raise_signal(signal.SIGINT)
            return opened[-1]

        with interrupting(), patch("harness.util.os.open", side_effect=interrupted_at_root):
            try:
                raise ValueError("an error the caller is handling")
            except ValueError:
                with self.assertRaises(KeyboardInterrupt):
                    self.verify(path, binding, "inside-except", started)
        self.assertEqual(started, [])
        self.assertTrue(opened)
        self.assertTrue(all(map(closed, opened)))


class AdmittedRootTests(InterruptSweep, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        (self.base / "root").mkdir()
        self.command = {"cwd": ".", "argv": ["check"]}
        self.runs = iter(range(1 << 30))

    def call(self, opened, started=None):
        """One broker call that succeeds; it collects the directories it opens and the root it starts from."""
        config = {"verification": {"commands": [self.command]}, "permissions": {"worker_commands": []},
                  "runtime": {"verify_timeout_seconds": 5}}

        def process(argv, cwd, log, **kwargs):
            # Stands in for the child: its captures and clean process facts.
            if started is not None:
                started.append(kwargs["root_fd"])
            for stream in ("stdout", "stderr"):
                log.with_suffix("." + stream).write_text("")
            kwargs["on_outcome"](ProcessOutcome(started=True, startup_state="started", exit_code=0, exit_confirmed=True))
            return 0

        metadata = create_manifest(config, self.base / "root", self.base / f"job{next(self.runs)}", None)
        broker = Broker(metadata["artifact"], metadata["artifact_digest"])
        with recorded_opens(directories_only=True, into=opened), \
                patch("harness.command_server.run_process", process):
            return broker.run_command({"command_id": command_id(self.command)})

    def test_an_interrupt_at_any_line_of_a_broker_call_leaves_its_admitted_root_closed(self):
        # From admission, where the root opens, through the call and its cleanup.
        codes = holder_codes() | {function.__code__ for function in (
            Broker.run_command, getattr(Broker, "_run_command", Broker.run_command), Broker._admit, Broker._open_root)}
        self.sweep(codes, self.call)

    def test_an_interrupt_as_a_finished_call_s_cleanup_begins_leaves_its_root_closed_while_the_caller_holds_it(self):
        for at in ("entry", "before the mask"):
            with self.subTest(at=at), interrupted_cleanup(at) as sent:
                opened, started = [], []
                caught = interruption(lambda: self.call(opened, started))
                self.assertEqual(sent, [at])
                # The command ran from the admitted root, which only the cleanup closes.
                self.assertEqual(len(started), 1)
                self.assertIn(started[0], opened)
                self.assertEqual([descriptor for descriptor in opened if not closed(descriptor)], [])
                self.assertIsNotNone(caught.__traceback__)


if __name__ == "__main__":
    unittest.main()
