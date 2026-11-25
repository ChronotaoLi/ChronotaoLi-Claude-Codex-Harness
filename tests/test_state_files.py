"""The state-file reading primitive (design 4, 3.1, 3.2, 3.5) and the structural check over migrated modules (3.7).

Everything runs in temporary directories: no model, CLI or Git. Every primitive call goes through
``StateCase.call``, which fails the test if an OSError leaves it. A FIFO case ends through
FifoWatchdog, so a read that blocks fails instead of hanging. Faults are injected by replacing
``state_files.os``, ``state_files.signal`` or ``state_files.fcntl`` with a proxy; only the root's resolution,
which goes through pathlib, is reached by patching ``os.lstat`` itself. Interrupts land at chosen lines
through a ``sys.settrace`` hook. Permission-bit cases are skipped as root. ``held_lock`` is the other
holder of a lock for any test that needs one busy.
"""
import ast
import collections
import contextlib
import errno
import fcntl
import hashlib
import importlib.util
import itertools
import multiprocessing
import os
import shutil
import signal
import socket
import stat
import symtable
import sys
import tempfile
import threading
import time
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import state_files, stops
from harness.state_files import HeldFiles, LockedDirectory, LockUnavailable, Owned, StateFiles, StateReadError
from harness.util import EvidenceError, write_json
from test_harness import ROOT
from test_provider_failures import FifoWatchdog


LEDGER = b'{"id": "run_a"}\n'
UNLESS_ROOT = unittest.skipIf(os.geteuid() == 0, "permission bits do not bind root")


@contextlib.contextmanager
def held_lock(path):
    """Another holder of the lock at ``path``.

    Opened ``a+`` (made, with its directory, when missing), then flocked exclusively without waiting. flock
    conflicts across descriptors, so every lock the primitive takes on the same file is busy meanwhile.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield stream


class FakeOs:
    """``os`` as ``state_files`` sees it: a call named in ``hooks`` runs ``hook(real, *args, **kwargs)``; the rest is ``os``."""

    def __init__(self, **hooks):
        self.hooks, self.calls = hooks, []

    def __getattr__(self, name):
        real, hook = getattr(os, name), self.hooks.get(name)
        if hook is None:
            return real

        def call(*args, **kwargs):
            self.calls.append((name, args[0] if args else None))
            return hook(real, *args, **kwargs)
        return call

    def count(self, name, target):
        return self.calls.count((name, target))


def passing(real, *args, **kwargs):
    return real(*args, **kwargs)


def failing(code, target=None):
    """A hook raising ``code`` for ``target`` (its first argument), or for every call when None."""
    def hook(real, *args, **kwargs):
        if target is None or args[0] == target:
            raise OSError(code, os.strerror(code))
        return real(*args, **kwargs)
    return hook


def once(action):
    """A hook running ``action()`` before the first real call only."""
    done = []

    def hook(real, *args, **kwargs):
        if not done:
            done.append(True)
            action()
        return real(*args, **kwargs)
    return hook


def interrupting(target):
    """An open that sends this thread SIGINT right after the descriptor for ``target`` exists."""
    def hook(real, *args, **kwargs):
        fd = real(*args, **kwargs)
        if args[0] == target:
            signal.pthread_kill(threading.get_ident(), signal.SIGINT)
        return fd
    return hook


def open_descriptors():
    return len(os.listdir("/dev/fd"))


def blocked():
    return signal.pthread_sigmask(signal.SIG_BLOCK, ()) & state_files.SIGNALS


class Mask:
    """``signal`` for ``state_files``, counting its pthread_sigmask calls and its blockings of the three signals.

    The ``fail``-th call raises OSError(``code``) and changes nothing; the ``interrupt``-th blocking applies and
    then raises ``raising``, as a pending handler does; ``armed`` is true right after the ``arm``-th blocking.
    """

    def __init__(self, *, fail=None, code=errno.EIO, interrupt=None, raising=KeyboardInterrupt, arm=None):
        self.fail, self.code, self.interrupt, self.raising, self.arm = fail, code, interrupt, raising, arm
        self.calls = self.blocks = 0
        self.armed = False

    def __getattr__(self, name):
        return getattr(signal, name)

    def pthread_sigmask(self, how, mask):
        self.calls += 1
        if self.calls == self.fail:
            raise OSError(self.code, os.strerror(self.code))
        previous = signal.pthread_sigmask(how, mask)
        if how == signal.SIG_BLOCK and set(mask) == set(state_files.SIGNALS):
            self.blocks += 1
            self.armed = self.blocks == self.arm
            if self.blocks == self.interrupt:
                raise self.raising
        return previous


def state_lines(call):
    """The line events ``call`` runs in ``state_files`` frames, in order."""
    lines = []

    def trace(frame, event, arg):
        if frame.f_globals is not vars(state_files):
            return None
        if event == "line":
            lines.append((frame.f_code.co_name, frame.f_lineno))
        return trace
    sys.settrace(trace)
    try:
        call()
    finally:
        sys.settrace(None)
    return lines


def tracing(due, send):
    """A trace running ``send()`` once, at the first line event of a ``state_files`` frame for which ``due()`` is true."""
    def trace(frame, event, arg):
        if frame.f_globals is not vars(state_files):
            return None
        if event == "line" and due():
            sys.settrace(None)
            send()
        return trace
    return trace


class UnmaskedThread:
    """A thread with the signals unblocked that receives SIGINT on request: Python then runs the handler on the main thread."""

    def __enter__(self):
        self.requested, self.request, self.delivered = False, threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.serve)
        self.thread.start()
        return self

    def serve(self):
        self.request.wait()
        if self.requested:
            signal.pthread_kill(threading.get_ident(), signal.SIGINT)
            self.delivered.set()

    def send(self):
        self.requested = True
        self.request.set()
        self.delivered.wait(5)

    def __exit__(self, *exc):
        self.request.set()
        self.thread.join(5)


def without_search_flags():
    """A fresh copy of ``state_files`` built where ``os`` has neither O_SEARCH nor O_PATH: the design's O_RDONLY fallback."""
    spec = importlib.util.spec_from_file_location("harness.state_files_without_search", state_files.__file__)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.__dict__), patch.dict(sys.modules, {spec.name: module}):  # dataclasses looks the module up
        for name in ("O_SEARCH", "O_PATH"):
            os.__dict__.pop(name, None)
        spec.loader.exec_module(module)
    return module


class StateCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.state = self.base / ".harness"
        self.run = self.state / "runs" / "run_a"
        self.run.mkdir(parents=True)
        (self.run / "state.json").write_bytes(LEDGER)
        self.files = StateFiles(self.state)

    def call(self, method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except OSError as error:  # the primitive never raises OSError
            self.fail(f"{method.__name__} raised {error!r}")

    def chmod(self, path, mode):
        os.chmod(path, mode)
        self.addCleanup(os.chmod, path, 0o755)

    def injected(self, **hooks):
        fake = FakeOs(**hooks)
        return fake, patch.object(state_files, "os", fake)

    def assertOutcome(self, outcome, state, *, level=None, leaf=None, found=None, code=None, stage=None):
        self.assertEqual(outcome.state, state, outcome)
        if level is not None:
            self.assertEqual(outcome.step.level, level, outcome)
        if leaf is not None:
            self.assertEqual(outcome.step.leaf, leaf, outcome)
        if stage is not None:
            self.assertEqual(outcome.step.stage, stage, outcome)
        self.assertEqual(outcome.found, found, outcome)
        if code is not None:
            self.assertEqual(outcome.errno, code, outcome)
        if state != "read":
            self.assertIsNone(outcome.data, outcome)
            self.assertIsNone(outcome.digest, outcome)


class LayoutTests(StateCase):
    """What stands at each level decides the outcome; nothing is followed or waited on."""

    def test_a_regular_file_reads_whole_with_its_size(self):
        outcome = self.call(self.files.read, ("runs", "run_a", "state.json"), limit=None)
        self.assertOutcome(outcome, "read", level=2, leaf=True)
        self.assertEqual((outcome.data, outcome.size, outcome.changed), (LEDGER, len(LEDGER), False))
        self.assertEqual(outcome.path, str(self.run / "state.json"))
        self.assertEqual(outcome.bytes_or_none(), LEDGER)
        self.assertEqual(outcome.evidence({"artifact": outcome.path}), LEDGER)

    def test_a_fifo_leaf_is_refused_without_waiting_for_a_writer(self):
        os.mkfifo(self.run / "fifo")
        for name, call in (("read", lambda: self.files.read(("runs", "run_a", "fifo"), limit=None)),
                           ("digest", lambda: self.files.digest(("runs", "run_a", "fifo"), keep=8)),
                           ("tail", lambda: self.files.read(("runs", "run_a", "fifo"), limit=8, part="tail"))):
            with self.subTest(call=name):
                watchdog = FifoWatchdog(self.run / "fifo", bound=2)
                try:
                    outcome = self.call(call)
                finally:
                    hung = watchdog.stop()
                self.assertFalse(hung, "the read waited on the FIFO")
                self.assertOutcome(outcome, "refused", level=2, leaf=True, found="fifo")

    def test_a_socket_a_directory_and_a_link_at_the_leaf_are_refused(self):
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        with contextlib.chdir(self.run):  # a socket path must stay short; bind the relative name
            sock.bind("sock")
        (self.run / "dir").mkdir()
        os.symlink("state.json", self.run / "link")
        for name, found in (("sock", "socket"), ("dir", "directory"), ("link", "symlink")):
            with self.subTest(leaf=name):
                for outcome in (self.call(self.files.read, ("runs", "run_a", name), limit=None),
                                self.call(self.files.digest, ("runs", "run_a", name))):
                    self.assertOutcome(outcome, "refused", level=2, leaf=True, found=found)

    def test_a_link_or_a_file_in_place_of_a_directory_is_refused_at_that_level(self):
        os.symlink("run_a", self.state / "runs" / "run_link")
        (self.state / "runs" / "plain").write_bytes(b"x")
        for name, found in (("run_link", "symlink"), ("plain", "not_directory")):
            with self.subTest(intermediate=name):
                for outcome in (self.call(self.files.read, ("runs", name, "state.json"), limit=None),
                                self.call(self.files.probe, ("runs", name, "state.json")),
                                self.call(self.files.entries, ("runs", name))):
                    self.assertOutcome(outcome, "refused", level=1, found=found)
                    self.assertEqual(outcome.step.name, name)

    def test_a_replaced_parent_directory_is_refused_not_followed(self):
        elsewhere = self.base / "elsewhere"
        shutil.move(str(self.run), str(elsewhere))
        os.symlink(elsewhere, self.run)
        outcome = self.call(self.files.read, ("runs", "run_a", "state.json"), limit=None)
        self.assertOutcome(outcome, "refused", level=1, leaf=False, found="symlink")
        self.assertIsNone(outcome.bytes_or_none())

    def test_an_absent_leaf_and_an_absent_parent_differ_only_in_their_step(self):
        leaf = self.call(self.files.read, ("runs", "run_a", "missing.json"), limit=None)
        parent = self.call(self.files.read, ("runs", "run_b", "state.json"), limit=None)
        root = self.call(StateFiles(self.base / "nowhere").read, ("runs", "run_a", "state.json"), limit=None)
        self.assertOutcome(leaf, "absent", level=2, leaf=True, stage="open")
        self.assertOutcome(parent, "absent", level=1, leaf=False, stage="open")
        self.assertOutcome(root, "absent", level="root", leaf=False)
        for outcome in (leaf, parent, root):
            self.assertIs(outcome.or_local_io(), outcome)
            with self.assertRaises(EvidenceError) as raised:
                outcome.evidence({"artifact": outcome.path, "dispatch_id": "d1"})
            self.assertNotIsInstance(raised.exception, StateReadError)
            self.assertEqual((raised.exception.stop["kind"], raised.exception.stop["reason"]), ("evidence", "runtime_integrity"))
            self.assertEqual(raised.exception.stop["source"], {"artifact": outcome.path, "dispatch_id": "d1"})

    def test_probe_tells_a_file_from_a_directory_without_reading(self):
        (self.run / "dir").mkdir()
        os.mkfifo(self.run / "fifo")
        os.symlink("state.json", self.run / "link")
        cases = ((("state.json",), "file", "present", None), (("dir",), "directory", "present", None),
                 (("dir",), "file", "refused", "directory"), (("state.json",), "directory", "refused", "not_directory"),
                 (("fifo",), "file", "refused", "fifo"), (("link",), "file", "refused", "symlink"),
                 (("missing",), "file", "absent", None))
        fake, injected = self.injected(open=passing, pread=passing)
        with injected:
            for name, expect, state, found in cases:
                with self.subTest(name=name, expect=expect):
                    outcome = self.call(self.files.probe, ("runs", "run_a") + name, expect=expect)
                    self.assertOutcome(outcome, state, level=2, leaf=True, found=found)
        # Only the root and the two directories above the leaf were opened; nothing was read.
        self.assertEqual({call for call in fake.calls}, {("open", str(self.state)), ("open", "runs"), ("open", "run_a")})

    def test_entries_lists_names_with_their_unfollowed_kind(self):
        (self.run / "dir").mkdir()
        os.mkfifo(self.run / "fifo")
        os.symlink("state.json", self.run / "link")
        listing = self.call(self.files.entries, ("runs", "run_a"))
        self.assertOutcome(listing, "listed", level=1, leaf=True)
        self.assertEqual(listing.entries, (("dir", "dir"), ("fifo", "other"), ("link", "symlink"), ("state.json", "file")))
        root = self.call(self.files.entries)
        self.assertEqual((root.state, root.step.level, root.entries), ("listed", "root", (("runs", "dir"),)))
        self.assertOutcome(self.call(self.files.entries, ("runs", "run_b")), "absent", level=1, leaf=True)
        self.assertOutcome(self.call(self.files.entries, ("runs", "run_a", "state.json")), "refused", level=2,
                           found="not_directory")

    def test_a_name_gone_before_its_type_is_read_stays_in_the_listing(self):
        # A published call directory removed between the enumeration and its fstatat: the listing keeps the name,
        # so a caller requiring every entry to be a directory (3.3.2) sees the call instead of an empty listing.
        call = self.run / "commands" / ("a" * 32)
        call.mkdir(parents=True)
        (call / "process.json").write_bytes(b'{"process_state": "running"}')

        def remove_first(real, name, **kwargs):
            if name == call.name and call.exists():
                shutil.rmtree(call)
            return real(name, **kwargs)
        _, injected = self.injected(stat=remove_first)
        with injected:
            listing = self.call(self.files.entries, ("runs", "run_a", "commands"))
        self.assertOutcome(listing, "listed", level=2, leaf=True)
        self.assertEqual(listing.entries, ((call.name, "absent"),))
        # Any other failure to type an entry still fails the whole listing.
        call.mkdir()
        _, injected = self.injected(stat=failing(errno.EIO, call.name))
        with injected:
            failed = self.call(self.files.entries, ("runs", "run_a", "commands"))
        self.assertOutcome(failed, "failed", level=2, stage="read", code=errno.EIO)
        self.assertEqual(failed.entries, ())

    def test_under_prefixes_components_and_recorded_compares_without_normalizing(self):
        run = self.files.under("runs", "run_a")
        self.assertEqual(self.call(run.read, ("state.json",), limit=None).data, LEDGER)
        self.assertEqual(self.call(run.probe, (), expect="directory").state, "present")
        self.assertEqual(self.files.recorded(str(self.run / "state.json")), ("runs", "run_a", "state.json"))
        self.assertEqual(run.recorded(str(self.run / "dispatches" / "d1" / "response.json")),
                         ("dispatches", "d1", "response.json"))
        for path in (str(self.base / "elsewhere" / "state.json"), str(self.state), str(self.state) + "-copy/runs",
                     str(self.state) + "/runs//run_a", str(self.state) + "/runs/./run_a", str(self.state) + "/runs/../runs",
                     str(self.state) + "/runs/run_a/", "runs/run_a", None, str(self.state) + "/runs/\0"):
            with self.subTest(path=path):
                self.assertIsNone(self.files.recorded(path))
        self.assertIsNone(run.recorded(str(self.state / "runs" / "run_b" / "state.json")))

    def test_a_root_that_cannot_be_resolved_is_a_failed_read(self):
        outcome = self.call(StateFiles(str(self.state) + "\0").read, ("runs",), limit=None)
        self.assertOutcome(outcome, "failed", level="root", code=errno.EINVAL, stage="resolve")

    def test_the_root_resolution_per_python_version(self):
        # Path.resolve() is not strict. A link loop at the root fails the resolution before 3.13 and is refused by
        # the root's own no-follow open from 3.13 on; either way nothing below it is read.
        os.symlink("loop", self.base / "loop")
        loop = self.call(StateFiles(self.base / "loop").read, ("runs", "run_a", "state.json"), limit=None)
        if sys.version_info < (3, 13):
            self.assertOutcome(loop, "failed", level="root", code=errno.ELOOP, stage="resolve")
        else:
            self.assertOutcome(loop, "refused", level="root", found="symlink", stage="open")
        # An lstat error inside the resolution is swallowed by it on both versions; the walk itself then reads.
        real, looked = os.lstat, []

        def lstat(path, *args, **kwargs):
            if os.fspath(path) == str(self.state):
                looked.append(path)
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            return real(path, *args, **kwargs)
        with patch.object(os, "lstat", lstat):
            outcome = self.call(self.files.read, ("runs", "run_a", "state.json"), limit=None)
        self.assertEqual(len(looked), 1)
        self.assertEqual((outcome.state, outcome.data), ("read", LEDGER))


class LimitTests(StateCase):
    def setUp(self):
        super().setUp()
        self.path = self.run / "original.json"

    def test_a_bounded_whole_read_past_its_limit_is_too_large(self):
        self.path.write_bytes(b"0123456789")
        self.assertOutcome(self.call(self.files.read, ("runs", "run_a", "original.json"), limit=9), "too_large",
                           level=2, leaf=True)
        self.assertEqual(self.call(self.files.read, ("runs", "run_a", "original.json"), limit=10).data, b"0123456789")
        with self.assertRaises(EvidenceError):
            self.call(self.files.read, ("runs", "run_a", "original.json"), limit=9).evidence()

    def test_a_file_that_grows_past_the_limit_while_read_is_too_large(self):
        self.path.write_bytes(b"01234567")

        def grow():
            with open(self.path, "ab") as stream:
                stream.write(b"89abcdef")
        _, injected = self.injected(pread=once(grow))
        with injected:
            outcome = self.call(self.files.read, ("runs", "run_a", "original.json"), limit=10)
        self.assertOutcome(outcome, "too_large", level=2)
        self.assertEqual(outcome.size, 8)

    def test_an_unbounded_read_reads_to_the_end(self):
        content = bytes(range(256)) * (3 * 4096 + 17)  # more than three 1 MiB chunks
        self.path.write_bytes(content)
        outcome = self.call(self.files.read, ("runs", "run_a", "original.json"), limit=None)
        self.assertEqual((outcome.state, outcome.data), ("read", content))
        with self.assertRaises(ValueError):
            self.files.read(("runs", "run_a", "original.json"), limit=None, part="head")

    def test_head_and_tail_are_never_too_large(self):
        self.path.write_bytes(b"0123456789")
        head = self.call(self.files.read, ("runs", "run_a", "original.json"), limit=3, part="head")
        tail = self.call(self.files.read, ("runs", "run_a", "original.json"), limit=3, part="tail")
        self.assertEqual((head.state, head.data, head.size), ("read", b"012", 10))
        self.assertEqual((tail.state, tail.data, tail.size), ("read", b"789", 10))

    def test_a_digest_streams_the_whole_file_and_keeps_its_head(self):
        content = os.urandom(2 * state_files.CHUNK + 5)
        self.path.write_bytes(content)
        outcome = self.call(self.files.digest, ("runs", "run_a", "original.json"), keep=16)
        self.assertEqual((outcome.state, outcome.digest, outcome.data, outcome.size),
                         ("read", hashlib.sha256(content).hexdigest(), content[:17], len(content)))
        bare = self.call(self.files.digest, ("runs", "run_a", "original.json"))
        self.assertEqual((bare.state, bare.digest, bare.data), ("read", hashlib.sha256(content).hexdigest(), None))


class ChangeTests(StateCase):
    """Only size and modification time tell a change: an in-place rewrite does, the runtime's atomic replacement does not."""

    def setUp(self):
        super().setUp()
        self.path = self.run / "original.json"
        write_json(self.path, {"v": "A" * (2 * state_files.CHUNK)})
        self.old = self.path.read_bytes()
        os.utime(self.path, ns=(10 ** 9, 10 ** 9))  # any rewrite now moves the modification time

    def rewrite(self):
        with open(self.path, "r+b") as stream:
            stream.write(b"{" + b" " * (len(self.old) - 3) + b"}\n")

    def test_an_in_place_rewrite_refuses_a_whole_read_and_a_digest(self):
        for name, call in (("read", lambda: self.files.read(("runs", "run_a", "original.json"), limit=None)),
                           ("bounded", lambda: self.files.read(("runs", "run_a", "original.json"), limit=len(self.old))),
                           ("digest", lambda: self.files.digest(("runs", "run_a", "original.json"), keep=10))):
            with self.subTest(call=name):
                self.path.write_bytes(self.old)
                os.utime(self.path, ns=(10 ** 9, 10 ** 9))
                _, injected = self.injected(pread=once(self.rewrite))
                with injected:
                    outcome = self.call(call)
                self.assertOutcome(outcome, "refused", level=2, found="changed")

    def test_an_atomic_replacement_while_read_leaves_the_old_bytes_whole(self):
        _, injected = self.injected(pread=once(lambda: write_json(self.path, {"v": "B"})))
        with injected:
            outcome = self.call(self.files.read, ("runs", "run_a", "original.json"), limit=None)
        self.assertEqual((outcome.state, outcome.changed, outcome.data), ("read", False, self.old))
        self.assertEqual(self.path.read_bytes(), b'{\n  "v": "B"\n}\n')

    def test_a_tail_read_reports_a_change_and_keeps_its_bytes(self):
        _, injected = self.injected(pread=once(self.rewrite))
        with injected:
            outcome = self.call(self.files.read, ("runs", "run_a", "original.json"), limit=4, part="tail")
        self.assertEqual((outcome.state, outcome.changed, len(outcome.data)), ("read", True, 4))


class FailureTests(StateCase):
    """A failure is never told as an absence; only an errno that names a wrong type is looked up again."""

    def assertLocalIo(self, outcome, stage, code=errno.EIO, error_type="OSError"):
        self.assertOutcome(outcome, "failed", stage=stage, code=code)
        self.assertEqual(outcome.error_type, error_type)
        self.assertIsNone(outcome.bytes_or_none())
        for accessor in (outcome.evidence, outcome.or_local_io):
            with self.assertRaises(StateReadError) as raised:
                accessor()
            self.assertEqual(raised.exception.stop, stops.make("runtime_failure", "io", data={"error_type": error_type}))
            self.assertIs(raised.exception.outcome, outcome)
            self.assertIn(outcome.path, str(raised.exception))
        self.assertEqual(stops.of(raised.exception)["kind"], "runtime_failure")

    def test_eio_at_an_open_is_failed_and_never_looked_up_again(self):
        for target, level in (("state.json", 2), ("run_a", 1)):
            with self.subTest(level=target):
                fake, injected = self.injected(open=failing(errno.EIO, target), stat=failing(errno.ENOENT, target))
                with injected:
                    outcome = self.call(self.files.read, ("runs", "run_a", "state.json"), limit=None)
                self.assertLocalIo(outcome, "open")
                self.assertEqual(outcome.step.level, level)
                self.assertEqual(fake.count("stat", target), 0)

    def test_a_wrong_type_errno_is_looked_up_once_and_a_vanished_name_is_absent(self):
        for code in (errno.ELOOP, errno.ENOTDIR):
            with self.subTest(errno=errno.errorcode[code]):
                fake, injected = self.injected(open=failing(code, "state.json"), stat=failing(errno.ENOENT, "state.json"))
                with injected:
                    outcome = self.call(self.files.read, ("runs", "run_a", "state.json"), limit=None)
                self.assertOutcome(outcome, "absent", level=2, leaf=True)
                self.assertEqual(fake.count("stat", "state.json"), 1)
                # The name is still a regular file: the open's errno stands as a failure.
                fake, injected = self.injected(open=failing(code, "state.json"), stat=passing)
                with injected:
                    outcome = self.call(self.files.read, ("runs", "run_a", "state.json"), limit=None)
                self.assertOutcome(outcome, "failed", level=2, code=code)

    def test_eio_from_a_probe_stat_is_failed_without_a_second_look(self):
        fake, injected = self.injected(stat=failing(errno.EIO, "state.json"))
        with injected:
            outcome = self.call(self.files.probe, ("runs", "run_a", "state.json"))
        self.assertLocalIo(outcome, "open")
        self.assertEqual(fake.count("stat", "state.json"), 1)

    def test_eio_at_each_stage_is_failed_with_that_stage(self):
        def close_then_fail(real, fd):
            real(fd)
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        stages = (("open", dict(open=failing(errno.EIO, "state.json"))), ("fstat", dict(fstat=failing(errno.EIO))),
                  ("read", dict(pread=failing(errno.EIO))), ("close", dict(close=close_then_fail)))
        for stage, hooks in stages:
            for name, call in (("read", lambda: self.files.read(("runs", "run_a", "state.json"), limit=None)),
                               ("tail", lambda: self.files.read(("runs", "run_a", "state.json"), limit=4, part="tail")),
                               ("digest", lambda: self.files.digest(("runs", "run_a", "state.json"), keep=4))):
                if stage == "close" and name != "read":
                    continue
                with self.subTest(stage=stage, call=name):
                    before = open_descriptors()
                    _, injected = self.injected(**hooks)
                    with injected:
                        outcome = self.call(call)
                    self.assertLocalIo(outcome, stage)
                    self.assertEqual(open_descriptors(), before)
        _, injected = self.injected(close=close_then_fail)
        with injected:
            self.assertLocalIo(self.call(self.files.entries, ("runs", "run_a")), "close")
            self.assertLocalIo(self.call(self.files.probe, ("runs", "run_a", "state.json")), "close")

    def test_a_failing_mask_call_fails_the_step_it_guards_and_closes_everything(self):
        # A read makes three mask calls around each of its four opens (root, runs, run_a, the leaf), then three
        # around the release. Whichever fails, the read is failed at that step with that errno, never classified
        # like the open's own errno (no lookup, ENOENT not absent), and no descriptor stays open.
        original = signal.pthread_sigmask(signal.SIG_BLOCK, ())
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, original)
        names = (str(self.state), "runs", "run_a", "state.json")
        for code in (errno.EIO, errno.ENOENT, errno.ELOOP):
            for at in range(1, 16):
                with self.subTest(errno=errno.errorcode[code], call=at):
                    before = open_descriptors()
                    fake, injected = self.injected(stat=passing)
                    with injected, patch.object(state_files, "signal", Mask(fail=at, code=code)):
                        outcome = self.call(self.files.read, ("runs", "run_a", "state.json"), limit=None)
                    signal.pthread_sigmask(signal.SIG_SETMASK, original)  # a failed restore left the signals blocked
                    stage, level = ("open", ("root", 0, 1, 2)[(at - 1) // 3]) if at <= 12 else ("close", 2)
                    self.assertLocalIo(outcome, stage, code=code, error_type=type(OSError(code, "")).__name__)
                    self.assertEqual(outcome.step.level, level)
                    self.assertEqual(sum(fake.count("stat", name) for name in names), 0)
                    self.assertEqual(open_descriptors(), before)

    @UNLESS_ROOT
    def test_a_listed_directory_without_search_permission_cannot_type_its_entries(self):
        # Read permission enumerates the names; typing each one with fstatat needs search permission on it too.
        empty = self.state / "runs" / "empty"
        empty.mkdir()
        self.chmod(self.run, 0o444)
        self.chmod(empty, 0o444)
        listing = self.call(self.files.entries, ("runs", "run_a"))
        self.assertLocalIo(listing, "read", code=errno.EACCES, error_type="PermissionError")
        self.assertEqual((listing.step.level, listing.step.leaf, listing.entries), (1, True, ()))
        nothing = self.call(self.files.entries, ("runs", "empty"))
        self.assertEqual((nothing.state, nothing.entries), ("listed", ()))

    @UNLESS_ROOT
    def test_without_o_search_or_o_path_every_walked_level_needs_read_permission(self):
        fallback = without_search_flags()
        self.assertEqual(fallback.SEARCH, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        files = fallback.StateFiles(self.state)
        self.assertEqual(self.call(files.read, ("runs", "run_a", "state.json"), limit=None).data, LEDGER)
        for directory, level in ((self.state, "root"), (self.state / "runs", 0), (self.run, 1)):
            with self.subTest(search_only=directory.name):
                self.chmod(directory, 0o111)
                outcome = self.call(files.read, ("runs", "run_a", "state.json"), limit=None)
                os.chmod(directory, 0o755)
                self.assertOutcome(outcome, "failed", level=level, stage="open", code=errno.EACCES)
                self.assertEqual(outcome.error_type, "PermissionError")
                self.chmod(directory, 0o555)
                self.assertEqual(self.call(files.read, ("runs", "run_a", "state.json"), limit=None).data, LEDGER)
                os.chmod(directory, 0o755)

    @UNLESS_ROOT
    @unittest.skipIf(state_files.SEARCH == state_files.LISTING, "without O_SEARCH or O_PATH directories open for reading")
    def test_search_permission_alone_reads_by_name(self):
        self.chmod(self.state, 0o111)
        self.assertEqual(self.call(self.files.read, ("runs", "run_a", "state.json"), limit=None).data, LEDGER)
        self.assertEqual(self.call(self.files.probe, ("runs", "run_a"), expect="directory").state, "present")
        os.chmod(self.state, 0o755)
        self.chmod(self.state / "runs", 0o111)
        self.assertEqual(self.call(self.files.read, ("runs", "run_a", "state.json"), limit=None).data, LEDGER)
        listing = self.call(self.files.entries, ("runs",))
        self.assertLocalIo(listing, "open", code=errno.EACCES, error_type="PermissionError")
        self.assertEqual((listing.step.level, listing.found), (0, None))

    @UNLESS_ROOT
    def test_a_root_without_search_permission_is_a_permission_failure(self):
        for mode in (0o444, 0o000):
            with self.subTest(mode=oct(mode)):
                self.chmod(self.state, mode)
                for outcome in (self.call(self.files.read, ("runs", "run_a", "state.json"), limit=None),
                                self.call(self.files.probe, ("runs", "run_a", "state.json")),
                                self.call(self.files.entries)):
                    self.assertLocalIo(outcome, "open", code=errno.EACCES, error_type="PermissionError")
                    self.assertEqual(outcome.step.level, "root")


class SignalCase(StateCase):
    """SIGINT raises KeyboardInterrupt; the handler and the signal mask are restored after each test."""

    def setUp(self):
        super().setUp()
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, previous)
        self.mask = signal.pthread_sigmask(signal.SIG_BLOCK, ())
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, self.mask)
        self.addCleanup(sys.settrace, None)

    def interrupted(self, call, trace, *patches):
        """``call`` under ``trace`` raises KeyboardInterrupt and leaves no descriptor open and no signal blocked."""
        before = open_descriptors()
        try:
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                sys.settrace(trace)
                with self.assertRaises(KeyboardInterrupt):
                    call()
        finally:
            sys.settrace(None)
            leftover = blocked()
            signal.pthread_sigmask(signal.SIG_SETMASK, self.mask)
        self.assertEqual(open_descriptors(), before)
        self.assertFalse(leftover)

    def every_line(self, call):
        """One SIGINT at each line ``call`` runs in ``state_files`` in turn: each time it leaves, leaking nothing."""
        for index, line in enumerate(state_lines(call), 1):
            with self.subTest(event=index, line=line):
                seen = itertools.count(1)
                self.interrupted(call, tracing(lambda: next(seen) == index, lambda: signal.raise_signal(signal.SIGINT)))


class InterruptTests(SignalCase):
    """SIGINT right after an open: the interrupt leaves, the descriptor does not stay open."""

    def calls(self):
        return (("read", lambda: self.files.read(("runs", "run_a", "state.json"), limit=None)),
                ("digest", lambda: self.files.digest(("runs", "run_a", "state.json"), keep=4)),
                ("probe", lambda: self.files.probe(("runs", "run_a", "state.json"))),
                ("entries", lambda: self.files.entries(("runs", "run_a"))))

    def test_one_interrupt_at_any_line_of_a_call_leaks_nothing(self):
        # One SIGINT at each line a call runs in state_files in turn, the boundary between the body and the release
        # and the release's entry included. Inside the mask it waits for the unmask; either way it leaves the call.
        for name, call in self.calls():
            lines = state_lines(call)
            for index, line in enumerate(lines, 1):
                with self.subTest(call=name, event=index, line=line):
                    seen = itertools.count(1)
                    self.interrupted(call, tracing(lambda: next(seen) == index,
                                                   lambda: signal.raise_signal(signal.SIGINT)))

    def test_an_interrupt_another_thread_received_leaves_no_mask(self):
        # SIGINT received by a thread that does not block it: Python runs the handler on this thread whatever its
        # mask, here at the first line after each blocking call returns (the four opens, then the release).
        for at in range(1, 6):
            with self.subTest(blocking=at), UnmaskedThread() as elsewhere:
                mask = Mask(arm=at)
                self.interrupted(lambda: self.files.read(("runs", "run_a", "state.json"), limit=None),
                                 tracing(lambda: mask.armed, elsewhere.send), patch.object(state_files, "signal", mask))

    def test_an_interrupt_after_any_open_leaks_no_descriptor(self):
        calls = (("read", "state.json", lambda: self.files.read(("runs", "run_a", "state.json"), limit=None)),
                 ("digest", "state.json", lambda: self.files.digest(("runs", "run_a", "state.json"), keep=4)),
                 ("probe", "run_a", lambda: self.files.probe(("runs", "run_a", "state.json"))),
                 ("entries", "run_a", lambda: self.files.entries(("runs", "run_a"))),
                 ("root", str(self.state), lambda: self.files.read(("runs", "run_a", "state.json"), limit=None)))
        for name, target, call in calls:
            with self.subTest(call=name):
                before = open_descriptors()
                _, injected = self.injected(open=interrupting(target))
                with injected, self.assertRaises(KeyboardInterrupt):
                    call()
                self.assertEqual(open_descriptors(), before)
                # The signals are unblocked again once the interrupt has left.
                self.assertFalse(signal.pthread_sigmask(signal.SIG_BLOCK, ()) & state_files.SIGNALS)

    def test_an_interrupt_raised_while_blocking_leaves_no_mask_and_no_descriptor(self):
        # pthread_sigmask runs a pending handler after applying the mask: the blocking call itself may raise.
        # Opens 1-4 are the root, runs, run_a and the leaf; block 5 is the release that closes them.
        for at in (1, 3, 5):
            with self.subTest(block=at):
                self.interrupted(lambda: self.files.read(("runs", "run_a", "state.json"), limit=None), None,
                                 patch.object(state_files, "signal", Mask(interrupt=at)))

    def test_an_interrupt_is_not_turned_into_a_failure_by_a_failing_restore(self):
        # The release's blocking (fifth, mask call 14) raises KeyboardInterrupt and its restore (call 15) fails:
        # the interrupt leaves, not a failed outcome, and the second release still closes every descriptor.
        before = open_descriptors()
        try:
            with patch.object(state_files, "signal", Mask(interrupt=5, fail=15)), self.assertRaises(KeyboardInterrupt):
                self.files.read(("runs", "run_a", "state.json"), limit=None)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, self.mask)  # the failed restore left the signals blocked
        self.assertEqual(open_descriptors(), before)

    def test_an_interrupt_the_caller_already_caught_does_not_hide_a_failing_restore(self):
        # The read runs inside the caller's own handler of an interrupt, which is not this read's: a restore failing
        # at the root's open (mask call 3), at the leaf's open that raised its own ENOENT (call 12) or at the release
        # (call 15) fails the read at that step. Only an interrupt this read raises itself (the release's blocking,
        # call 14) leaves past the same failing restore.
        def handling(caught, read):
            before = open_descriptors()
            try:
                raise caught
            except caught:
                return read()
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, self.mask)  # a failed restore left the signals blocked
                self.assertEqual(open_descriptors(), before)
        leaf_enoent = dict(open=failing(errno.ENOENT, "state.json"))
        for caught in (KeyboardInterrupt, SystemExit):
            for at, stage, level, hooks in ((3, "open", "root", {}), (12, "open", 2, leaf_enoent), (15, "close", 2, {})):
                _, injected = self.injected(**hooks)
                with self.subTest(caught=caught.__name__, fail=at), injected, \
                        patch.object(state_files, "signal", Mask(fail=at)):
                    outcome = handling(caught, lambda: self.call(self.files.read, ("runs", "run_a", "state.json"), limit=None))
                    self.assertOutcome(outcome, "failed", level=level, stage=stage, code=errno.EIO)
            for raised in (KeyboardInterrupt, SystemExit):
                with self.subTest(caught=caught.__name__, raised=raised.__name__), self.assertRaises(raised), \
                        patch.object(state_files, "signal", Mask(interrupt=5, raising=raised, fail=15)):
                    handling(caught, lambda: self.files.read(("runs", "run_a", "state.json"), limit=None))


class FakeFcntl:
    """``fcntl`` for ``state_files``: ``flock`` runs ``hook(real, fd, operation)``, the rest is ``fcntl``."""

    def __init__(self, hook):
        self.hook = hook

    def __getattr__(self, name):
        return getattr(fcntl, name)

    def flock(self, fd, operation):
        return self.hook(fcntl.flock, fd, operation)


NEW_LEDGER = b'{"id": "run_a", "store_revision": 1}\n'


def racing_for_a_lock(state, rounds, barrier, start, results):
    """One process of a race: each round, at the instant every other one does, create and take the same new lock.

    The winner holds it until every racer has its outcome. Reports the outcome states, the ENOENTs the real
    openat(O_CREAT) answered, and the traceback of anything else that left.
    """
    found, missing, files = collections.Counter(), [], StateFiles(state)
    try:
        for index in range(rounds):
            name, synced = f"race-{index}.lock", []

            def open_racing(real, path, flags, *args, **kwargs):
                if path == name and flags & os.O_CREAT and not synced:
                    synced.append(True)
                    if barrier.wait() == 0:  # every racer has walked to the directory: all open at one instant
                        start.value = time.monotonic_ns() + 5_000_000
                    barrier.wait()
                    while time.monotonic_ns() < start.value:
                        pass
                try:
                    return real(path, flags, *args, **kwargs)
                except FileNotFoundError:
                    missing.append(path)
                    raise

            def held(locked):
                found[locked.outcome.state] += 1
                barrier.wait()  # keeps the lock until every racer has its outcome
            try:
                with patch.object(state_files, "os", FakeOs(open=open_racing)):
                    state_files.holding(files.lock(("runs", "run_a", name), create=True), held)
            except LockUnavailable as unavailable:
                found[unavailable.outcome.state] += 1
                barrier.wait()
        results.put((found, len(missing), None))
    except BaseException:
        results.put((found, len(missing), traceback.format_exc()))


class LockTests(StateCase):
    """A lock file opens in the directory it locks and is flocked there; nothing is followed, waited on or made unasked."""

    def setUp(self):
        super().setUp()
        self.lock = self.run / "run.lock"

    def entered(self, components=("runs", "run_a", "run.lock"), **kwargs):
        """Enter a lock and leave it at once: its outcome, and the descriptors it held in its block (None: never ran)."""
        before, holding = open_descriptors(), None
        try:
            with self.files.lock(components, **kwargs) as locked:
                holding = open_descriptors() - before
            outcome = locked.outcome
        except LockUnavailable as unavailable:
            outcome = unavailable.outcome
            self.assertNotEqual(outcome.state, "locked")
        except OSError as error:  # the primitive never raises OSError
            self.fail(f"lock raised {error!r}")
        self.assertEqual(open_descriptors(), before)
        return outcome, holding

    def test_an_exclusive_lock_makes_a_0600_regular_file_and_holds_only_it_and_its_directory(self):
        previous = os.umask(0)  # the mode comes from the open, not from the umask
        self.addCleanup(os.umask, previous)
        before = open_descriptors()
        with self.files.lock(("runs", "run_a", "run.lock"), create=True) as locked:
            self.assertOutcome(locked.outcome, "locked", level=2, leaf=True, stage="lock")
            self.assertEqual(locked.outcome.path, str(self.lock))
            info = os.lstat(self.lock)
            self.assertEqual((stat.S_ISREG(info.st_mode), stat.S_IMODE(info.st_mode)), (True, 0o600))
            directory = os.stat(self.run)
            self.assertEqual(locked.identity, (directory.st_dev, directory.st_ino))
            # The root and runs were closed on entering; the lock file and run_a stay open for the block.
            self.assertEqual(open_descriptors(), before + 2)
            with self.assertRaises(BlockingIOError), held_lock(self.lock):
                pass
        self.assertEqual(open_descriptors(), before)
        with held_lock(self.lock):  # released when the block ended
            pass

    def test_a_lock_held_with_a_plus_makes_the_primitive_busy(self):
        with held_lock(self.lock):
            for kwargs in (dict(create=True), dict(create=False), dict(mode="shared", create=False),
                           dict(mode="shared", create=True)):
                with self.subTest(**kwargs):
                    outcome, holding = self.entered(**kwargs)
                    self.assertOutcome(outcome, "busy", level=2, leaf=True, stage="lock")
                    self.assertIsNone(holding)  # the block never ran
        # The primitive's own locks: shared beside shared, never exclusive beside shared.
        with self.files.lock(("runs", "run_a", "run.lock"), mode="shared", create=False) as shared:
            self.assertEqual(shared.outcome.state, "locked")
            self.assertEqual(self.entered(mode="shared", create=False)[0].state, "locked")
            self.assertEqual(self.entered(create=True)[0].state, "busy")
        self.assertEqual(self.entered(create=True)[0].state, "locked")

    def test_a_missing_lock_is_absent_unless_created(self):
        for kwargs in (dict(mode="shared", create=False), dict(create=False)):
            with self.subTest(**kwargs):
                outcome, holding = self.entered(**kwargs)
                self.assertOutcome(outcome, "absent", level=2, leaf=True, stage="open")
                self.assertIsNone(holding)  # the block never ran
                self.assertFalse(os.path.lexists(self.lock))
        # A missing directory is absent above the lock, created or not: no directory is ever made for a lock.
        outcome, _ = self.entered(("runs", "run_b", "run.lock"), create=True)
        self.assertOutcome(outcome, "absent", level=1, leaf=False)
        self.assertFalse(os.path.lexists(self.state / "runs" / "run_b"))

    def test_a_preview_goes_on_without_a_lock_only_when_the_lock_file_alone_is_missing(self):
        def preview(run):
            """The cleanup preview: the ledger under a shared lock, or by path when only the lock file is missing."""
            files = self.files.under("runs", run)
            try:
                with files.lock(("run.lock",), mode="shared", create=False) as locked:
                    return "locked", locked.read("state.json", limit=None).data
            except LockUnavailable as unavailable:
                if not (unavailable.outcome.state == "absent" and unavailable.outcome.step.leaf):
                    raise
            return "unlocked", files.read(("state.json",), limit=None).data
        self.assertEqual(preview("run_a"), ("unlocked", LEDGER))
        self.assertFalse(os.path.lexists(self.lock))  # the preview made no lock
        self.lock.write_bytes(b"")
        self.assertEqual(preview("run_a"), ("locked", LEDGER))
        # Anything else stops the preview: the run directory missing above the lock, the lock busy or not a file.
        with self.assertRaises(LockUnavailable) as raised:
            preview("run_b")
        self.assertOutcome(raised.exception.outcome, "absent", level=1, leaf=False)
        with held_lock(self.lock), self.assertRaises(LockUnavailable) as raised:
            preview("run_a")
        self.assertOutcome(raised.exception.outcome, "busy", level=2, leaf=True, stage="lock")
        self.lock.unlink()
        os.mkfifo(self.lock)
        with self.assertRaises(LockUnavailable) as raised:
            preview("run_a")
        self.assertOutcome(raised.exception.outcome, "refused", level=2, leaf=True, found="fifo")

    def test_a_lock_that_is_not_a_regular_file_is_refused_at_once(self):
        target = self.base / "target"
        target.write_bytes(b"kept")
        layouts = (("fifo", lambda: os.mkfifo(self.lock), "fifo"),
                   ("link", lambda: os.symlink(target, self.lock), "symlink"),
                   ("dangling", lambda: os.symlink(self.base / "nowhere", self.lock), "symlink"),
                   ("directory", self.lock.mkdir, "directory"))
        for name, make, found in layouts:
            make()
            for kwargs in (dict(create=True), dict(create=False), dict(mode="shared", create=False)):
                with self.subTest(layout=name, **kwargs):
                    watchdog = FifoWatchdog(self.lock, bound=2) if name == "fifo" else None
                    try:
                        outcome, holding = self.entered(**kwargs)
                    finally:
                        hung = watchdog.stop() if watchdog else False
                    self.assertFalse(hung, "the lock waited on the FIFO")
                    self.assertOutcome(outcome, "refused", level=2, leaf=True, found=found)
                    self.assertIsNone(holding)  # the block never ran
            # Nothing was made through the link, nor written to its target.
            self.assertEqual(target.read_bytes(), b"kept")
            self.assertFalse(os.path.lexists(self.base / "nowhere"))
            (self.lock.rmdir if name == "directory" else self.lock.unlink)()

    def test_a_failing_flock_is_failed_and_holds_nothing(self):
        def refuse(real, fd, operation):
            raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))
        with patch.object(state_files, "fcntl", FakeFcntl(refuse)):
            outcome, holding = self.entered(create=True)
        self.assertOutcome(outcome, "failed", level=2, stage="lock", code=errno.ENOLCK)
        self.assertIsNone(holding)  # the block never ran
        with self.assertRaises(StateReadError) as raised:
            outcome.or_local_io()
        self.assertEqual(raised.exception.stop, stops.make("runtime_failure", "io", data={"error_type": "OSError"}))

    def test_waiting_blocks_until_the_holder_lets_go(self):
        holder = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        operations = []

        def record(real, fd, operation):
            operations.append(operation)
            return real(fd, operation)
        timer = threading.Timer(0.3, os.close, (holder,))  # closing the holder's descriptor releases its lock
        timer.start()
        try:
            with patch.object(state_files, "fcntl", FakeFcntl(record)), \
                    self.files.lock(("runs", "run_a", "run.lock"), create=True, wait=True) as locked:
                self.assertEqual(locked.outcome.state, "locked")
        finally:
            timer.join()
        self.assertEqual(operations, [fcntl.LOCK_EX])

    def test_read_and_replace_go_through_the_locked_directory(self):
        components = ("runs", "run_a", "store.lock")
        with self.files.lock(components, create=True) as locked:
            self.assertEqual(self.call(locked.read, "state.json", limit=None).data, LEDGER)
            identity = locked.identity
            # Another directory is put at the path: the locked one moves away, a copy with the same bytes takes its name.
            moved = self.base / "moved"
            os.rename(self.run, moved)
            self.run.mkdir()
            (self.run / "state.json").write_bytes(LEDGER)
            written = self.call(locked.replace, "state.json", NEW_LEDGER)
            self.assertOutcome(written, "written", level=2, leaf=True, stage="rename")
            self.assertEqual(written.size, len(NEW_LEDGER))
            self.assertEqual((moved / "state.json").read_bytes(), NEW_LEDGER)
            self.assertEqual((self.run / "state.json").read_bytes(), LEDGER)
            self.assertEqual(self.call(locked.read, "state.json", limit=None).data, NEW_LEDGER)
            self.assertEqual(sorted(os.listdir(moved)), ["state.json", "store.lock"])  # no temporary file stays
        # The next lock at the path finds the other directory.
        with self.files.lock(components, create=True) as again:
            self.assertNotEqual(again.identity, identity)
            self.assertEqual(again.read("state.json", limit=None).data, LEDGER)

    def test_replace_renames_a_new_file_over_the_name(self):
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        write_json(self.base / "reference.json", {})
        ledger = self.run / "state.json"
        reader = os.open(ledger, os.O_RDONLY)
        self.addCleanup(os.close, reader)
        with self.files.lock(("runs", "run_a", "store.lock"), create=True) as locked:
            self.assertEqual(locked.replace("state.json", NEW_LEDGER).state, "written")
        # A new inode took the name, with the mode write_json gives; the old one was never written in place.
        self.assertNotEqual(os.stat(ledger).st_ino, os.fstat(reader).st_ino)
        self.assertEqual(os.pread(reader, 100, 0), LEDGER)
        self.assertEqual(ledger.read_bytes(), NEW_LEDGER)
        self.assertEqual(stat.S_IMODE(os.stat(ledger).st_mode), stat.S_IMODE(os.stat(self.base / "reference.json").st_mode))

    def test_a_failed_replace_leaves_the_name_as_it_was(self):
        def close_then_fail(real, fd):
            real(fd)
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        stages = (("write", errno.ENOSPC, dict(write=failing(errno.ENOSPC))),
                  ("write", errno.EIO, dict(fsync=failing(errno.EIO))),
                  ("close", errno.EIO, dict(close=close_then_fail)),
                  ("rename", errno.EIO, dict(replace=failing(errno.EIO))))
        with self.files.lock(("runs", "run_a", "store.lock"), create=True) as locked:
            for stage, code, hooks in stages:
                with self.subTest(stage=stage, hook=next(iter(hooks))):
                    before = open_descriptors()
                    _, injected = self.injected(**hooks)
                    with injected:
                        outcome = self.call(locked.replace, "state.json", NEW_LEDGER)
                    self.assertOutcome(outcome, "failed", level=2, stage=stage, code=code)
                    self.assertEqual(open_descriptors(), before)
                    self.assertEqual((self.run / "state.json").read_bytes(), LEDGER)
                    with self.assertRaises(StateReadError):
                        outcome.or_local_io()

    def test_a_lock_is_used_only_while_held(self):
        uses = (lambda lock: lock.identity, lambda lock: lock.read("state.json", limit=None),
                lambda lock: lock.replace("state.json", NEW_LEDGER))
        # Not taken: the block never runs, and the lock offers nothing to code that never looked at the outcome.
        busy, ran = self.files.lock(("runs", "run_a", "run.lock"), create=True), []
        with held_lock(self.lock), self.assertRaises(LockUnavailable) as raised:
            with busy:
                ran.append(True)
        self.assertEqual((ran, raised.exception.outcome.state), ([], "busy"))
        # Left: nothing either, and a lock is entered once.
        with self.files.lock(("runs", "run_a", "run.lock"), create=True) as locked:
            pass
        for lock in (busy, locked):
            for use in uses:
                with self.subTest(lock=lock.outcome.state), self.assertRaises(RuntimeError):
                    use(lock)
        with self.assertRaises(RuntimeError):
            locked.__enter__()
        # A shared lock reads and never replaces.
        with self.files.lock(("runs", "run_a", "run.lock"), mode="shared", create=False) as shared:
            self.assertEqual(shared.read("state.json", limit=None).data, LEDGER)
            with self.assertRaises(RuntimeError):
                shared.replace("state.json", NEW_LEDGER)
        self.assertEqual((self.run / "state.json").read_bytes(), LEDGER)
        self.assertEqual(sorted(os.listdir(self.run)), ["run.lock", "state.json"])  # no temporary file either

    def test_a_failing_mask_call_or_close_while_leaving_is_reported_unless_an_exception_leaves(self):
        # Entering makes three mask calls around each of its four opens (root, runs, run_a, the lock file) and three
        # around closing the root and runs; leaving makes three more. A failure while entering: the lock is not taken,
        # failed at that step. A failure while leaving: everything is closed and the block's exit fails at the close,
        # unless an exception already leaves the block, which goes on unchanged.
        original = signal.pthread_sigmask(signal.SIG_BLOCK, ())
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, original)
        leaving = []

        def close_then_fail(real, fd):
            real(fd)
            if leaving:  # only the closes leaving the block makes
                raise OSError(errno.EIO, os.strerror(errno.EIO))
        faults = [(at, lambda at=at: patch.object(state_files, "signal", Mask(fail=at))) for at in range(1, 19)]
        faults.append(("close", lambda: self.injected(close=close_then_fail)[1]))
        for fault, patcher in faults:
            for raised in (None, LookupError, KeyboardInterrupt):
                with self.subTest(fault=fault, raised=raised and raised.__name__):
                    before, outcome, error = open_descriptors(), None, None
                    leaving.clear()
                    try:
                        with patcher(), self.files.lock(("runs", "run_a", "run.lock"), create=True):
                            leaving.append(True)
                            if raised is not None:
                                raise raised("the block's own")
                    except LockUnavailable as unavailable:
                        outcome = unavailable.outcome
                    except (StateReadError, LookupError, KeyboardInterrupt) as caught:
                        error = caught
                    finally:  # a failed restore left the signals blocked
                        signal.pthread_sigmask(signal.SIG_SETMASK, original)
                    self.assertEqual(open_descriptors(), before)
                    if fault != "close" and fault <= 15:
                        self.assertEqual(leaving, [])  # the block never ran
                        self.assertOutcome(outcome, "failed", stage="open" if fault <= 12 else "close", code=errno.EIO)
                    elif raised is not None:
                        self.assertEqual((type(error), str(error)), (raised, "the block's own"))
                    else:
                        self.assertIsInstance(error, StateReadError)
                        self.assertOutcome(error.outcome, "failed", level=2, leaf=True, stage="close", code=errno.EIO)
                        self.assertEqual(error.stop, stops.make("runtime_failure", "io",
                                                                data={"error_type": "OSError"}))
                    with held_lock(self.lock):  # nothing kept the lock
                        pass

    @UNLESS_ROOT
    def test_a_new_lock_is_opened_0600_and_the_umask_narrows_it(self):
        # The new lock is never chmodded: the open's 0600 less the process umask is its mode, so under umask 0200 it
        # is 0400, which a shared lock still opens and an exclusive one, needing to write, does not.
        for umask in (0o000, 0o022, 0o077, 0o200):
            with self.subTest(umask=oct(umask)):
                previous = os.umask(umask)
                try:
                    with self.files.lock(("runs", "run_a", "run.lock"), create=True) as locked:
                        self.assertEqual(locked.outcome.state, "locked")
                finally:
                    os.umask(previous)
                self.assertEqual(stat.S_IMODE(os.lstat(self.lock).st_mode), 0o600 & ~umask)
                if umask == 0o200:
                    self.assertEqual(self.entered(mode="shared", create=False)[0].state, "locked")
                    outcome, holding = self.entered(create=True)
                    self.assertOutcome(outcome, "failed", level=2, leaf=True, stage="open", code=errno.EACCES)
                    self.assertIsNone(holding)
                self.lock.unlink()

    def test_processes_racing_to_create_one_lock_either_take_it_or_find_it_busy(self):
        # Darwin answers ENOENT to the openat(O_CREAT) of a process that loses the race to create the name; told as an
        # absent lock, it stopped Harness as a missing run directory or damaged evidence. One racer takes each lock.
        context = multiprocessing.get_context("spawn")
        count, rounds = 4, 20
        barrier, start, results = context.Barrier(count, timeout=30), context.Value("q"), context.Queue()
        processes = [context.Process(target=racing_for_a_lock, args=(str(self.state), rounds, barrier, start, results))
                     for _ in range(count)]
        try:
            for process in processes:
                process.start()
            reports = [results.get(timeout=120) for _ in processes]
            for process in processes:
                process.join(timeout=10)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                if process.pid is not None:
                    process.join(timeout=5)
            results.close()
            results.join_thread()
        self.assertEqual([error for _, _, error in reports if error], [])
        found = sum((counts for counts, _, _ in reports), collections.Counter())
        missing = sum(count for _, count, _ in reports)
        self.assertEqual(found, collections.Counter(locked=rounds, busy=rounds * (count - 1)),
                         f"the race's openat(O_CREAT) answered ENOENT {missing} times")

    def test_a_lock_file_reported_missing_while_created_is_opened_again_from_its_directory(self):
        # The creating open answers ENOENT once, as the loser of that race does, while run_a is moved away and another
        # directory put at its path: it is opened again from the descriptor of the directory walked, never by path.
        moved, opens = self.base / "moved", []

        def losing_once(real, name, flags, *args, **kwargs):
            opens.append((name, kwargs.get("dir_fd")))
            if name == "run.lock" and [each for each, _ in opens].count("run.lock") == 1:
                os.rename(self.run, moved)
                self.run.mkdir()
                raise OSError(errno.ENOENT, os.strerror(errno.ENOENT))
            return real(name, flags, *args, **kwargs)
        before = open_descriptors()
        with self.injected(open=losing_once)[1], self.files.lock(("runs", "run_a", "run.lock"), create=True) as locked:
            identity = locked.identity
        self.assertEqual(open_descriptors(), before)
        info = os.stat(moved)
        self.assertEqual(identity, (info.st_dev, info.st_ino))
        self.assertEqual((sorted(os.listdir(moved)), os.listdir(self.run)), (["run.lock", "state.json"], []))
        (_, directory), (_, again) = [entry for entry in opens if entry[0] == "run.lock"]
        self.assertEqual(again, directory)
        self.assertEqual([name for name, _ in opens].count("run_a"), 1)  # walked once
        # Bounded: a directory that keeps answering ENOENT leaves the lock absent. Only a created lock opens again.
        for create, attempts in ((True, 1 + state_files.CREATE_AGAIN), (False, 1)):
            with self.subTest(create=create):
                fake, injected = self.injected(open=failing(errno.ENOENT, "run.lock"))
                with injected:
                    outcome, holding = self.entered(create=create)
                self.assertOutcome(outcome, "absent", level=2, leaf=True, stage="open")
                self.assertIsNone(holding)
                self.assertEqual(fake.count("open", "run.lock"), attempts)
        fake, injected = self.injected(open=failing(errno.ENOENT, "state.json"))
        with injected:
            self.assertOutcome(self.call(self.files.read, ("runs", "run_a", "state.json"), limit=None), "absent", level=2)
        self.assertEqual(fake.count("open", "state.json"), 1)

    def test_a_lock_asked_for_its_directory_is_neither_taken_nor_made_in_another(self):
        info = os.stat(self.run)
        identity = (info.st_dev, info.st_ino)
        with self.files.lock(("runs", "run_a", "run.lock"), create=True, identity=identity) as locked:
            self.assertEqual(locked.identity, identity)
        # run_a is moved away and a copy without its lock file put at its path.
        moved = self.base / "moved"
        os.rename(self.run, moved)
        self.run.mkdir()
        (self.run / "state.json").write_bytes(LEDGER)
        for kwargs in (dict(create=True), dict(create=False), dict(mode="shared", create=False)):
            with self.subTest(**kwargs):
                outcome, holding = self.entered(identity=identity, **kwargs)
                self.assertOutcome(outcome, "replaced", level=1, leaf=False, stage="fstat")
                self.assertIsNone(holding)  # the block never ran
                self.assertEqual(os.listdir(self.run), ["state.json"])
        self.assertEqual(sorted(os.listdir(moved)), ["run.lock", "state.json"])
        copy = os.stat(self.run)
        self.assertEqual(self.entered(create=True, identity=(copy.st_dev, copy.st_ino))[0].state, "locked")


class HeldRootTests(StateCase):
    """An observation reads below the root it opened once; descriptors it is handed belong to its Owned."""

    def test_a_held_root_is_read_from_until_closed(self):
        before = open_descriptors()
        held = StateFiles.held(self.state)
        # Nothing is open until a with block takes it, so nothing can leak between held() and the with statement.
        self.assertFalse(held.opened)
        self.assertEqual(open_descriptors(), before)
        for use in (lambda: held.read(("runs", "run_a", "state.json"), limit=None),
                    lambda: held.recorded(str(self.run))):
            with self.assertRaises(ValueError):
                use()
        with held:
            self.assertTrue(held.opened)
            self.assertEqual(open_descriptors(), before + 1)
            # The state directory is replaced at its path: the observation keeps reading the one it opened.
            os.rename(self.state, self.base / "moved")
            (self.state / "runs" / "run_a").mkdir(parents=True)
            (self.state / "runs" / "run_a" / "state.json").write_bytes(b'{"id": "other"}\n')
            self.assertEqual(self.call(held.read, ("runs", "run_a", "state.json"), limit=None).data, LEDGER)
            self.assertEqual(self.call(held.under("runs", "run_a").read, ("state.json",), limit=None).data, LEDGER)
            self.assertEqual(self.call(held.probe, ("runs", "run_a"), expect="directory").state, "present")
            self.assertEqual(self.call(StateFiles(self.state).read, ("runs", "run_a", "state.json"), limit=None).data,
                             b'{"id": "other"}\n')
            self.assertEqual(open_descriptors(), before + 1)
            self.assertIsNone(held.close())
            self.assertFalse(held.opened)
            self.assertEqual(open_descriptors(), before)
            with self.assertRaises(ValueError):
                held.read(("runs", "run_a", "state.json"), limit=None)
        self.assertEqual(open_descriptors(), before)
        with self.assertRaises(RuntimeError):
            held.__enter__()

    def test_a_held_root_only_reads(self):
        # No lock, no replacement and no directory made through a held root: once it is closed and its number is
        # handed out again, nothing written can land in another directory.
        with StateFiles.held(self.state) as held:
            for view in (held, held.under("runs", "run_a")):
                for name in ("lock", "replace", "make_directories"):
                    self.assertFalse(hasattr(view, name), name)
        self.assertFalse(issubclass(HeldFiles, StateFiles))

    def test_recorded_compares_with_the_root_bound_when_opened(self):
        other = self.base / "other"
        (other / "runs" / "run_a").mkdir(parents=True)
        recorded, elsewhere = str(self.run / "state.json"), str(other / "runs" / "run_a" / "state.json")
        with StateFiles.held(self.state) as held:
            self.assertEqual(held.recorded(recorded), ("runs", "run_a", "state.json"))
            # The root moves away and a link to another directory takes its path: the held root is still the one read.
            os.rename(self.state, self.base / "moved")
            os.symlink(other, self.state)
            self.assertEqual(held.recorded(recorded), ("runs", "run_a", "state.json"))
            self.assertEqual(held.under("runs").recorded(recorded), ("run_a", "state.json"))
            self.assertIsNone(held.recorded(elsewhere))
            self.assertEqual(self.call(held.read, held.recorded(recorded), limit=None).data, LEDGER)
        # A StateFiles holds nothing and resolves the path each time.
        self.assertEqual(StateFiles(self.state).recorded(elsewhere), ("runs", "run_a", "state.json"))
        with StateFiles.held(self.base / "nowhere") as missing:
            self.assertFalse(missing.opened)
            self.assertEqual(missing.recorded(str(self.base / "nowhere" / "x")), ("x",))

    def test_nothing_is_handed_to_a_closed_owner(self):
        with StateFiles.held(self.state) as held:
            owner = Owned()
            self.assertIsNone(owner.close())
            before = open_descriptors()
            for components, kind in ((("runs", "run_a", "state.json"), "file"), (("runs", "run_a"), "directory")):
                with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                    held.open(components, kind, into=owner)
                self.assertEqual(open_descriptors(), before)
            spare = os.open(os.devnull, os.O_RDONLY)
            try:
                with self.assertRaises(RuntimeError):
                    owner.add(spare)
                self.assertIsNone(owner.close())
                self.assertTrue(stat.S_ISCHR(os.fstat(spare).st_mode))  # never taken, so never closed by the owner
            finally:
                os.close(spare)

    @UNLESS_ROOT
    @unittest.skipIf(state_files.SEARCH == state_files.LISTING, "without O_SEARCH or O_PATH directories open for reading")
    def test_a_root_with_search_permission_alone_is_opened(self):
        self.chmod(self.state, 0o111)
        with StateFiles.held(self.state) as held:
            self.assertTrue(held.opened)
            self.assertEqual(self.call(held.read, ("runs", "run_a", "state.json"), limit=None).data, LEDGER)
            self.assertOutcome(self.call(held.entries), "failed", level="root", code=errno.EACCES)

    @UNLESS_ROOT
    def test_a_root_without_search_permission_is_not_opened(self):
        for mode in (0o444, 0o000):
            with self.subTest(mode=oct(mode)):
                self.chmod(self.state, mode)
                before = open_descriptors()
                with StateFiles.held(self.state) as held, Owned() as owner:
                    self.assertFalse(held.opened)
                    self.assertEqual(open_descriptors(), before)
                    os.chmod(self.state, 0o755)  # what opening the root decided stands for the whole observation
                    outcomes = (self.call(held.read, ("runs", "run_a", "state.json"), limit=None),
                                self.call(held.probe, ("runs", "run_a"), expect="directory"), held.entries(),
                                held.open(("runs", "run_a"), "directory", into=owner)[0])
                    for outcome in outcomes:
                        self.assertOutcome(outcome, "failed", level="root", stage="open", code=errno.EACCES)
                        self.assertEqual(outcome.error_type, "PermissionError")
                self.assertEqual(open_descriptors(), before)

    def test_a_missing_root_is_not_opened_and_every_read_is_absent(self):
        with StateFiles.held(self.base / "nowhere") as held:
            self.assertFalse(held.opened)
            self.assertOutcome(self.call(held.read, ("runs", "run_a", "state.json"), limit=None), "absent", level="root",
                               leaf=False)
            self.assertOutcome(self.call(held.entries), "absent", level="root", leaf=True)

    def test_open_hands_a_directory_or_a_regular_file_to_the_owner(self):
        os.mkfifo(self.run / "fifo")
        os.symlink("state.json", self.run / "link")
        with StateFiles.held(self.state) as held, Owned() as owner:
            before = open_descriptors()
            for components, kind, path, level in ((("runs", "run_a", "state.json"), "file", self.run / "state.json", 2),
                                                  (("runs", "run_a"), "directory", self.run, 1),
                                                  ((), "directory", self.state, "root")):
                with self.subTest(components=components):
                    outcome, fd = self.call(held.open, components, kind, into=owner)
                    self.assertOutcome(outcome, "opened", level=level, leaf=True, stage="open")
                    expected = os.stat(path)
                    self.assertEqual((os.fstat(fd).st_dev, os.fstat(fd).st_ino), (expected.st_dev, expected.st_ino))
            self.assertEqual(open_descriptors(), before + 3)
            refusals = ((("runs", "run_a", "fifo"), "file", "refused", "fifo"),
                        (("runs", "run_a", "link"), "file", "refused", "symlink"),
                        (("runs", "run_a", "state.json"), "directory", "refused", "not_directory"),
                        (("runs", "run_b"), "directory", "absent", None),
                        (("runs", "run_a", "missing"), "file", "absent", None))
            for components, kind, state, found in refusals:
                with self.subTest(components=components, kind=kind):
                    watchdog = FifoWatchdog(self.run / "fifo", bound=2)
                    try:
                        outcome, fd = self.call(held.open, components, kind, into=owner)
                    finally:
                        self.assertFalse(watchdog.stop(), "the open waited on the FIFO")
                    self.assertOutcome(outcome, state, level=len(components) - 1, found=found)
                    self.assertIsNone(fd)
            self.assertEqual(open_descriptors(), before + 3)
            self.assertIsNone(owner.close())
            self.assertEqual(open_descriptors(), before)

    def test_a_released_descriptor_is_never_closed_again(self):
        with StateFiles.held(self.state) as held, Owned() as owner:
            _, first = held.open(("runs", "run_a"), "directory", into=owner)
            _, second = held.open(("runs", "run_a", "state.json"), "file", into=owner)
            self.assertIsNone(owner.release(first))
            # The system hands the released number out again; neither a second release nor close() touches it.
            spare = os.open(os.devnull, os.O_RDONLY)
            os.dup2(spare, first)
            os.close(spare)
            self.addCleanup(os.close, first)
            with self.assertRaises(ValueError):
                owner.release(first)
            self.assertIsNone(owner.close())
            self.assertTrue(stat.S_ISCHR(os.fstat(first).st_mode))
            with self.assertRaises(OSError):
                os.fstat(second)
            self.assertIsNone(owner.close())

    def test_a_failing_mask_call_while_handing_over_leaves_the_owner_empty(self):
        # The file's open makes three mask calls around each of runs, run_a and state.json, three around the hand-over
        # and three around closing runs and run_a. Whichever fails, the open fails and the owner holds nothing:
        # a descriptor handed before the failure is taken back.
        original = signal.pthread_sigmask(signal.SIG_BLOCK, ())
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, original)
        with StateFiles.held(self.state) as held:
            before = open_descriptors()
            for at in range(1, 16):
                with self.subTest(call=at), Owned() as owner:
                    with patch.object(state_files, "signal", Mask(fail=at)):
                        outcome, fd = self.call(held.open, ("runs", "run_a", "state.json"), "file", into=owner)
                    signal.pthread_sigmask(signal.SIG_SETMASK, original)  # a failed restore left the signals blocked
                    self.assertOutcome(outcome, "failed", stage="open" if at <= 12 else "close", code=errno.EIO)
                    self.assertIsNone(fd)
                    self.assertEqual(open_descriptors(), before)


class MakeDirectoriesTests(StateCase):
    def test_missing_levels_are_made_and_existing_ones_pass(self):
        before = open_descriptors()
        components = ("runs", "run_b", "verification", "final", "1")
        self.assertOutcome(self.call(self.files.make_directories, components), "present", level=4, leaf=True)
        self.assertTrue(self.state.joinpath(*components).is_dir())
        self.assertOutcome(self.call(self.files.make_directories, components), "present", level=4)
        self.assertOutcome(self.call(self.files.make_directories, ("runs", "run_a")), "present", level=1)
        self.assertEqual(self.call(self.files.under("runs", "run_a").make_directories, ("verification",)).state, "present")
        self.assertTrue((self.run / "verification").is_dir())
        self.assertEqual(open_descriptors(), before)

    def test_a_link_or_a_file_at_a_level_is_refused_and_nothing_is_made_through_it(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, self.state / "runs" / "run_link")
        (self.state / "runs" / "plain").write_bytes(b"x")
        for name, found in (("run_link", "symlink"), ("plain", "not_directory")):
            with self.subTest(level=name):
                outcome = self.call(self.files.make_directories, ("runs", name, "verification"))
                self.assertOutcome(outcome, "refused", level=1, leaf=False, found=found)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_a_level_made_meanwhile_is_opened_as_it_is_found(self):
        target = self.state / "runs" / "run_b"
        for name, put, state, found in (("directory", target.mkdir, "present", None),
                                        ("link", lambda: os.symlink(self.base, target), "refused", "symlink")):
            with self.subTest(meanwhile=name):
                fake, injected = self.injected(mkdir=once(put))  # the mkdir then finds the name taken
                with injected:
                    outcome = self.call(self.files.make_directories, ("runs", "run_b"))
                self.assertOutcome(outcome, state, level=1, found=found)
                self.assertEqual(fake.count("mkdir", "run_b"), 1)
                (shutil.rmtree if name == "directory" else os.unlink)(target)

    @UNLESS_ROOT
    def test_a_level_that_cannot_be_made_is_failed(self):
        self.chmod(self.state / "runs", 0o555)
        outcome = self.call(self.files.make_directories, ("runs", "run_b"))
        self.assertOutcome(outcome, "failed", level=1, stage="mkdir", code=errno.EACCES)
        self.assertEqual(outcome.error_type, "PermissionError")
        with self.assertRaises(StateReadError):
            outcome.or_local_io()


class HoldingInterruptTests(SignalCase):
    """An interrupt anywhere while a lock, a held root or a hand-over opens, is used or closes leaks no descriptor.

    Every holder stays referenced in ``self.kept`` while the descriptors are counted: nothing waits for collection.
    """

    def setUp(self):
        super().setUp()
        self.components = ("runs", "run_a", "store.lock")
        self.kept = []

    def locking(self, **kwargs):
        def call():
            self.kept.append(self.files.lock(self.components, create=True, **kwargs))
            with self.kept[-1]:
                self.fail("entered a lock that was interrupted")
        return call

    def assertUnusable(self, holder):
        """A lock not held, a held root not open, a closed Owned: every use is a programming error."""
        if isinstance(holder, LockedDirectory):
            uses = (lambda: holder.identity, lambda: holder.read("state.json", limit=None),
                    lambda: holder.replace("state.json", NEW_LEDGER))
            error = RuntimeError
        elif isinstance(holder, Owned):
            uses, error = (lambda: holder.add(-1),), RuntimeError
        else:
            self.assertFalse(holder.opened)
            uses = (lambda: holder.read(("runs", "run_a", "state.json"), limit=None),)
            error = ValueError
        for use in uses:
            with self.assertRaises(error):
                use()

    def test_an_interrupt_after_any_open_while_entering_leaks_nothing(self):
        # The signal arrives inside the mask and is raised at the unmask: __enter__ closes what it registered.
        for target in (str(self.state), "runs", "run_a", "store.lock"):
            with self.subTest(open=target):
                self.interrupted(self.locking(), None, self.injected(open=interrupting(target))[1])

        def holding():
            self.kept.append(StateFiles.held(self.state))
            with self.kept[-1]:
                self.fail("entered a held root that was interrupted")
        with self.subTest(open="held root"):
            self.interrupted(holding, None, self.injected(open=interrupting(str(self.state)))[1])
        for holder in self.kept:
            self.assertUnusable(holder)

    def test_an_interrupt_while_waiting_for_a_lock_leaks_nothing(self):
        main, timers = threading.get_ident(), []

        def interrupted_wait(real, fd, operation):
            if not operation & fcntl.LOCK_NB:  # interrupt the wait once it blocks
                timers.append(threading.Timer(0.2, signal.pthread_kill, (main, signal.SIGINT)))
                timers[-1].start()
            return real(fd, operation)
        with held_lock(self.run / "store.lock"):
            self.interrupted(self.locking(wait=True), None, patch.object(state_files, "fcntl", FakeFcntl(interrupted_wait)))
        for timer in timers:
            timer.join()
        self.assertEqual(len(timers), 1)
        self.assertUnusable(self.kept[0])
        with self.files.lock(self.components, create=True) as again:  # nothing kept the lock
            self.assertEqual(again.outcome.state, "locked")

    def test_an_interrupt_between_held_and_its_with_block_leaks_nothing(self):
        # SIGINT as StateFiles.held returns, before the with statement takes what it returned: nothing is open yet.
        returned = []

        def trace(frame, event, arg):
            if event == "return" and frame.f_code is StateFiles.held.__code__ and not returned:
                returned.append(arg)
                signal.raise_signal(signal.SIGINT)
            return trace

        def call():
            with StateFiles.held(self.state):
                self.fail("the with statement took a held root it was interrupted before")
        self.interrupted(call, trace)
        held, = returned
        self.assertFalse(held.opened)
        with held:  # still the closer of a root it has not opened
            self.assertTrue(held.opened)
        self.assertFalse(held.opened)

    def test_a_lock_interrupted_while_entering_is_never_used_after(self):
        # SIGINT at each line __enter__ runs, the last (the lock already usable) included. The lock stays referenced and
        # its numbers open another directory again: it neither reads nor writes there, nor tells that one's identity.
        other = self.base / "other"
        other.mkdir()
        (other / "state.json").write_bytes(b"other")

        def call():
            self.kept.append(self.files.lock(self.components, create=True))
            with self.kept[-1]:
                pass
        entering = [index for index, (name, _) in enumerate(state_lines(call), 1) if name == "__enter__"]
        self.assertGreater(len(entering), 10)
        for index in entering:
            with self.subTest(event=index):
                seen = itertools.count(1)
                self.interrupted(call, tracing(lambda: next(seen) == index, lambda: signal.raise_signal(signal.SIGINT)))
                spares = [os.open(other, os.O_RDONLY | os.O_DIRECTORY) for _ in range(4)]
                try:
                    self.assertUnusable(self.kept[-1])
                finally:
                    for spare in spares:
                        os.close(spare)
        self.assertEqual(os.listdir(other), ["state.json"])
        self.assertEqual((other / "state.json").read_bytes(), b"other")

    def test_an_interrupt_inside_a_cleanup_is_settled_before_it_leaves(self):
        # SIGINT as __exit__ or close calls _shut, inside its try, and once more while its handler closes again (one
        # descriptor closed, then interrupted): _settle runs _shut until one run completes. The holders stay referenced;
        # the first interrupt leaves with every descriptor closed and the lock free. A signal before that try is set up
        # skips the cleanup (test_a_bare_with_misses_a_signal_at_its_cleanups_entry): only holding covers that one.
        def lock_exit():
            lock = self.files.lock(self.components, create=True)
            self.kept.append(lock)
            with lock:
                pass

        def held_exit():
            held = StateFiles.held(self.state)
            self.kept.append(held)
            with held:
                pass

        def held_close():
            held = StateFiles.held(self.state)
            self.kept.append(held)
            held.__enter__()
            held.close()

        def handed(owner):
            with StateFiles.held(self.state) as held:
                held.open(("runs", "run_a", "state.json"), "file", into=owner)
                held.open(("runs", "run_a"), "directory", into=owner)

        def owner_close():
            owner = Owned()
            self.kept.append(owner)
            handed(owner)
            owner.close()

        def owner_exit():
            owner = Owned()
            self.kept.append(owner)
            with owner:
                handed(owner)
        # HeldFiles and Owned share their close and __exit__: the holder is told by the frame's self.
        cases = (("lock exit", LockedDirectory, LockedDirectory.__exit__, lock_exit),
                 ("held exit", HeldFiles, HeldFiles.__exit__, held_exit),
                 ("held close", HeldFiles, HeldFiles.close, held_close),
                 ("owner close", Owned, Owned.close, owner_close), ("owner exit", Owned, Owned.__exit__, owner_exit))
        real_close = state_files._close
        for name, kind, entry, call in cases:
            for again in (False, True):
                with self.subTest(entry=name, again=again):
                    armed = []

                    def trace(frame, event, arg):
                        caller = frame.f_back
                        if event == "call" and frame.f_code is kind._shut.__code__ \
                                and caller.f_code is entry.__code__ and isinstance(caller.f_locals["self"], kind):
                            sys.settrace(None)
                            armed.append(again)
                            signal.raise_signal(signal.SIGINT)
                        return trace

                    def closing(held, keep=0):
                        if not any(armed):
                            return real_close(held, keep)
                        armed.clear()
                        real_close([held.pop()])
                        raise KeyboardInterrupt("again, one descriptor closed")
                    self.interrupted(call, trace, patch.object(state_files, "_close", closing))
                    self.assertEqual(armed, [False] if not again else [])
                    with held_lock(self.run / "store.lock"):
                        pass
        for holder in self.kept:
            self.assertUnusable(holder)

    def test_an_interrupt_at_the_hand_over_finds_the_descriptor_with_the_owner(self):
        real = Owned.add

        def add(owner, fd):
            real(owner, fd)
            signal.pthread_kill(threading.get_ident(), signal.SIGINT)  # pending until the hand-over's unmask
        held = self.enterContext(StateFiles.held(self.state))
        for components, kind in ((("runs", "run_a", "state.json"), "file"), (("runs", "run_a"), "directory"),
                                 ((), "directory")):
            with self.subTest(components=components):
                before, owner = open_descriptors(), Owned()
                with patch.object(Owned, "add", add), self.assertRaises(KeyboardInterrupt):
                    held.open(components, kind, into=owner)
                self.assertEqual(open_descriptors(), before + 1)  # the owner has it
                self.assertIsNone(owner.close())
                self.assertEqual(open_descriptors(), before)
                self.assertFalse(blocked())

        class Returning(Owned):
            """The second version's hand-over: out of the call's list inside the mask, then carried by the return value only."""

            def add(self, fd):
                self.returned = fd
                signal.pthread_kill(threading.get_ident(), signal.SIGINT)
        before, owner = open_descriptors(), Returning()
        with self.assertRaises(KeyboardInterrupt):
            held.open(("runs", "run_a", "state.json"), "file", into=owner)
        owner.close()
        self.assertEqual(open_descriptors(), before + 1)  # the injection point is real: that hand-over leaks it
        os.close(owner.returned)

    def test_an_interrupt_after_an_open_while_handing_over_leaks_nothing(self):
        held = self.enterContext(StateFiles.held(self.state))
        for components, kind, target in ((("runs", "run_a", "state.json"), "file", "state.json"),
                                         (("runs", "run_a"), "directory", "run_a"), ((), "directory", ".")):
            with self.subTest(components=components):
                def call():
                    with Owned() as owner:
                        held.open(components, kind, into=owner)
                self.interrupted(call, None, self.injected(open=interrupting(target))[1])

    def test_one_interrupt_at_any_line_of_a_lock_leaks_nothing(self):
        # Taken through holding: its own lines, __enter__, the reads and writes in use, and both closes, each line.
        def use(locked):
            locked.read("state.json", limit=None)
            locked.replace("state.json", LEDGER)

        def call():
            self.kept.append(self.files.lock(self.components, create=True))
            state_files.holding(self.kept[-1], use)
        self.every_line(call)
        for lock in self.kept:
            self.assertUnusable(lock)
        self.assertEqual((self.run / "state.json").read_bytes(), LEDGER)
        with self.files.lock(self.components, create=True) as again:
            self.assertEqual(again.outcome.state, "locked")

    def test_one_interrupt_at_any_line_of_a_held_root_and_its_hand_overs_leaks_nothing(self):
        # held(), and the root and an owner each through holding: entering, the reads and hand-overs, both closes of
        # each, and the explicit closes in between. A signal at holding's first line finds a root not yet entered:
        # nothing is open and it reads nothing.
        def call(closing):
            held, owner = StateFiles.held(self.state), Owned()
            self.kept.append(held)

            def use(owner):
                held.read(("runs", "run_a", "state.json"), limit=None)
                held.open(("runs", "run_a", "state.json"), "file", into=owner)
                held.open(("runs", "run_a"), "directory", into=owner)
                if closing:
                    owner.close()
                    held.close()
            state_files.holding(held, lambda held: state_files.holding(owner, use))
        for closing in (False, True):
            with self.subTest(closing=closing):
                self.every_line(lambda: call(closing))
        for held in self.kept:
            self.assertUnusable(held)

    def test_one_interrupt_at_any_line_of_making_directories_leaks_nothing(self):
        def call():
            shutil.rmtree(self.state / "runs" / "run_b", ignore_errors=True)  # every run makes both levels
            self.files.make_directories(("runs", "run_b", "verification"))
        self.every_line(call)

    def holder(self, kind, root):
        """A new holder of ``kind``, kept referenced, and the ``use`` that takes descriptors through it.

        The lock reads and replaces in its directory, the held root reads below it, the owner is handed a file and a
        directory by ``root``, a held root the test entered beforehand.
        """
        if kind == "lock":
            holder = self.files.lock(self.components, create=True)

            def use(locked):
                locked.read("state.json", limit=None)
                locked.replace("state.json", LEDGER)
        elif kind == "held root":
            holder = StateFiles.held(self.state)

            def use(held):
                held.read(("runs", "run_a", "state.json"), limit=None)
        else:
            holder = Owned()

            def use(owner):
                root.open(("runs", "run_a", "state.json"), "file", into=owner)
                root.open(("runs", "run_a"), "directory", into=owner)
        self.kept.append(holder)
        return holder, use

    def assertClosed(self, holder):
        """Unusable, never entered again, and the lock free."""
        self.assertUnusable(holder)
        if not isinstance(holder, Owned):
            with self.assertRaises(RuntimeError):
                holder.__enter__()
        with held_lock(self.run / "store.lock"):
            pass

    def test_one_interrupt_where_a_holder_cannot_see_it_is_closed_by_holding(self):
        # One real SIGINT as entering starts or returns (where a bare with has not yet taken the exit), inside use, at
        # the entry of holding's inner close (before any of its code runs) and inside it (its first line, its release
        # starting). The interrupt leaves, the descriptors are back to the count before with the holder still
        # referenced, the holder is unusable and the lock free.
        root = self.enterContext(StateFiles.held(self.state))
        holding = state_files.holding.__code__
        release = state_files._release.__code__
        for kind in ("lock", "held root", "owner"):
            for point in ("enter call", "enter return", "use", "close call", "close line", "release call"):
                with self.subTest(holder=kind, point=point):
                    holder, use = self.holder(kind, root)
                    enter, shut = type(holder).__enter__.__code__, type(holder)._shut.__code__
                    due = {"enter call": lambda frame, event: frame.f_code is enter and event == "call",
                           "enter return": lambda frame, event: frame.f_code is enter and event == "return",
                           "use": lambda frame, event: False,
                           "close call": lambda frame, event: (frame.f_code is shut and event == "call"
                                                               and frame.f_back.f_code is holding),
                           "close line": lambda frame, event: (frame.f_code is shut and event == "line"
                                                               and frame.f_back.f_code is holding),
                           "release call": lambda frame, event: (frame.f_code is release and event == "call"
                                                                 and frame.f_back.f_code is shut
                                                                 and frame.f_back.f_back.f_code is holding)}[point]
                    sent = []

                    def trace(frame, event, arg):
                        if due(frame, event):
                            sys.settrace(None)
                            sent.append(point)
                            signal.raise_signal(signal.SIGINT)
                        return trace

                    def using(holder):
                        use(holder)
                        if point == "use":
                            sent.append(point)
                            signal.raise_signal(signal.SIGINT)
                    self.interrupted(lambda: state_files.holding(holder, using), trace)
                    self.assertEqual(sent, [point])
                    self.assertClosed(holder)

    def test_a_bare_with_misses_a_signal_at_its_cleanups_entry(self):
        # Why a bare with, or a bare close, belongs only inside holding: a signal at __exit__'s or close's entry, or as
        # __enter__ returns, lands before any code of the holder's own cleanup runs. The descriptors stay open, the
        # holder usable and the lock busy until the test closes them itself; holding closes them at the same points.
        root = self.enterContext(StateFiles.held(self.state))
        cases = (("lock", "__exit__", "call", 2), ("lock", "__enter__", "return", 2),
                 ("held root", "__exit__", "call", 1), ("held root", "__enter__", "return", 1),
                 ("owner", "close", "call", 2))
        for kind, method, wanted, leaked in cases:
            with self.subTest(holder=kind, at=f"{method} {wanted}"):
                holder, use = self.holder(kind, root)
                code, before, sent = getattr(type(holder), method).__code__, open_descriptors(), []

                def trace(frame, event, arg):
                    if frame.f_code is code and event == wanted and isinstance(frame.f_locals.get("self"), type(holder)):
                        sys.settrace(None)
                        sent.append(True)
                        signal.raise_signal(signal.SIGINT)
                    return trace

                def bare():
                    if kind == "owner":
                        use(holder)
                        holder.close()
                    else:
                        with holder:
                            use(holder)
                sys.settrace(trace)
                try:
                    with self.assertRaises(KeyboardInterrupt):
                        bare()
                finally:
                    sys.settrace(None)
                self.assertEqual(sent, [True])
                self.assertEqual(open_descriptors(), before + leaked)
                if kind == "lock":
                    self.assertEqual(len(holder.identity), 2)  # still usable
                    with self.assertRaises(BlockingIOError), held_lock(self.run / "store.lock"):
                        pass
                    holder.__exit__(None, None, None)
                else:
                    if kind == "held root":
                        self.assertTrue(holder.opened)
                    holder.close()
                self.assertEqual(open_descriptors(), before)

    def test_holding_reports_a_failing_close_once_and_never_over_an_exception(self):
        # As holding leaves, each descriptor's close fails once it has closed it (EIO), or the mask restore after
        # closing them fails. Leaving normally, that is one StateReadError at the close of the level that failed; an
        # exception already leaving goes on unchanged. Either way each descriptor is closed once, never again.
        root = self.enterContext(StateFiles.held(self.state))
        original = signal.pthread_sigmask(signal.SIG_BLOCK, ())
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, original)
        leaving, closes = [], collections.Counter()
        failed = {"lock": (2, str(self.run / "store.lock"), 2), "held root": ("root", str(self.state), 1),
                  "owner": ("owned", "", 2)}

        def counted(fault):
            def close(real, fd):
                if leaving:
                    closes[fd] += 1
                real(fd)
                if leaving and fault == "close":
                    raise OSError(errno.EIO, os.strerror(errno.EIO))
            return close

        class RestoreFails:
            def __getattr__(self, name):
                return getattr(signal, name)

            def pthread_sigmask(self, how, mask):
                if how == signal.SIG_SETMASK and leaving:
                    raise OSError(errno.EIO, os.strerror(errno.EIO))
                return signal.pthread_sigmask(how, mask)
        for kind, (level, path, count) in failed.items():
            for fault in (None, "close", "mask"):
                for raised in (None, LookupError, KeyboardInterrupt):
                    with self.subTest(holder=kind, fault=fault, raised=raised and raised.__name__):
                        holder, use = self.holder(kind, root)
                        leaving.clear()
                        closes.clear()
                        before, result, error = open_descriptors(), None, None

                        def using(holder):
                            use(holder)
                            leaving.append(True)
                            if raised is not None:
                                raise raised("the use's own")
                            return "returned"
                        try:
                            with contextlib.ExitStack() as stack:
                                stack.enter_context(self.injected(close=counted(fault))[1])
                                if fault == "mask":
                                    stack.enter_context(patch.object(state_files, "signal", RestoreFails()))
                                result = state_files.holding(holder, using)
                        except (StateReadError, LookupError, KeyboardInterrupt) as caught:
                            error = caught
                        finally:  # a failed restore left the signals blocked
                            signal.pthread_sigmask(signal.SIG_SETMASK, original)
                        self.assertEqual(open_descriptors(), before)
                        self.assertEqual(sorted(closes.values()), [1] * count)
                        self.assertClosed(holder)
                        if raised is not None:
                            self.assertEqual((type(error), str(error)), (raised, "the use's own"))
                        elif fault is None:
                            self.assertEqual((result, error), ("returned", None))
                        else:
                            self.assertIsInstance(error, StateReadError)
                            self.assertIsNone(error.__context__)
                            self.assertOutcome(error.outcome, "failed", level=level, leaf=True, stage="close",
                                               code=errno.EIO)
                            self.assertEqual(error.outcome.path, path)
                            self.assertEqual(error.stop, stops.make("runtime_failure", "io",
                                                                    data={"error_type": "OSError"}))

    def test_holding_runs_use_only_while_the_lock_is_taken(self):
        # Taken: use's value comes back and the lock is closed after. Busy, or absent and not created: LockUnavailable
        # with that outcome leaves, use never runs, nothing stays open and the lock is unusable.
        before = open_descriptors()
        lock = self.files.lock(self.components, create=True)
        self.assertEqual(state_files.holding(lock, lambda locked: locked.read("state.json", limit=None).data), LEDGER)
        self.assertEqual(open_descriptors(), before)
        self.assertClosed(lock)
        for state, components, create in (("busy", self.components, True),
                                          ("absent", ("runs", "run_a", "missing.lock"), False)):
            with self.subTest(state=state):
                ran, lock = [], self.files.lock(components, create=create)
                with held_lock(self.run / "store.lock"), self.assertRaises(LockUnavailable) as raised:
                    state_files.holding(lock, ran.append)
                self.assertEqual((ran, raised.exception.outcome.state), ([], state))
                self.assertEqual(open_descriptors(), before)
                self.assertClosed(lock)
        self.assertFalse(os.path.lexists(self.run / "missing.lock"))


# Modules whose every state-file read goes through harness.state_files, each with the raw filesystem
# calls its functions may still make, as (function, call) pairs (design 4, 3.7). The task that migrates
# a module's last read and deletes the old reading helpers it defines adds it here; state_files.py never.
MIGRATED = {
    "run_store.py": [], "worker_returns.py": [], "path_evidence.py": [], "input_projection.py": [],
    "diagnostics.py": [], "provider_failures.py": [],
    "receipts.py": [], "runtime_evidence.py": [],
    "versions.py": [], "mcp_server.py": [],
    "scope.py": [], "recovery.py": [], "observe.py": [], "watch_cli.py": [],
    "cli.py": [("_main", "resolve"), ("operate", "util.read_json"), ("print_saved_result", "os.open")],
    "supervisor.py": [
        ("_linked_run", "resolve"),  # Only a trusted Darwin system alias above private state.
        # Pre-launch collision checks, receipt/claim creation and the child repository argument.
        ("supervise", "exists"), ("supervise", "is_symlink"), ("supervise", "os.open"),
        ("supervise", "os.open"), ("supervise", "resolve"),
    ],
    "memory.py": [
        # Repository identity, provenance files outside state and the SQLite connection.
        ("__init__", "resolve"), ("_check_proposals", "resolve"), ("_source_hash", "read_bytes"),
        ("_source_ledger", "resolve"), ("_source_ledger", "resolve"), ("_source_path", "is_file"),
        ("_source_path", "resolve"), ("_source_path", "stat"), ("_staleness", "resolve"),
        ("bind_source_files", "is_symlink"), ("bind_source_files", "read_bytes"),
        ("bind_source_files", "resolve"), ("connect", "resolve"), ("validate_proposals", "resolve"),
    ],
    # Explicit operator inputs, write preparation and worker-declared candidate inputs.
    "continuation.py": [("_read_input", "open")],
    "decisions.py": [("read_decision", "open")],
    "environment.py": [("read_update", "open")],
    "planning.py": [("apply_pending_revision", "exists")],
    "progress.py": [
        ("_checkpoint_path", "resolve"), ("_checkpoint_path", "resolve"),
        ("_regular_file", "is_symlink"), ("_regular_file", "is_file"),
        ("_file_digest", "open"), ("_read", "open"), ("record_checkpoints", "open"),
    ],
    "command_server.py": [
        # Worktree identity and traversal, path checks before writes, lock creation and directory fsync.
        ("_safe", "lstat"), ("create_manifest", "resolve"), ("create_manifest", "os.stat"),
        ("create_manifest", "os.open"), ("_open_root", "open"), ("_openat", "os.open"),
        ("_lstatat", "os.stat"), ("_chain_change", "os.stat"), ("_fsync_directory", "os.open"),
    ],
    "cleanup.py": [
        ("_canonical", "is_symlink"), ("_canonical", "resolve"), ("common_dir", "resolve"),
        ("_check_missing_binding", "resolve"),
        ("_clean_worktree", "exists"), ("_clean_worktree", "exists"), ("_clean_worktree", "exists"),
        ("_clean_worktree", "exists"), ("_clean_worktree", "exists"), ("_clean_worktree", "is_symlink"),
    ],
    "workspace_inputs.py": [
        ("_parent", "os.open"), ("_directories", "os.open"), ("_identity", "os.open"), ("_identity", "stat"),
        ("_read", "lstat"), ("_read", "read_bytes"), ("validate_inputs", "exists"),
        ("prepare_locked", "open"), ("prepare_locked", "exists"), ("prepare_locked", "exists"),
        ("prepare_locked", "exists"), ("prepare_locked", "exists"),
        ("prepare_locked", "lstat"), ("prepare_locked", "lstat"),
        ("prepare_locked", "os.open"), ("prepare_locked", "os.open"),
        ("cleanup_inputs", "exists"), ("cleanup_inputs", "exists"),
    ],
}

METHODS = frozenset(("read_text", "read_bytes", "open", "exists", "is_file", "is_dir", "is_symlink", "stat", "lstat",
                     "iterdir", "glob", "rglob", "readlink", "resolve"))
FUNCTIONS = frozenset(("builtins.open", "os.open", "os.stat", "os.lstat", "os.scandir", "os.listdir", "os.path.exists",
                       "os.path.isfile", "os.path.isdir", "os.path.islink", "fcntl.flock", "json.load",
                       "hashlib.file_digest"))
# The old reading helpers, by defining module, whether or not they still exist.
HELPERS = frozenset(("util.read_json", "util.tail", "util.lock", "util.readonly_lock", "versions.read_current_ledger",
                     "recovery.read_bound_bytes", "recovery.read_bound_result", "recovery.read_evidence_json",
                     "provider_failures.read_original", "provider_failures.read_for_boundary",
                     "adapters._original_bytes", "cleanup._regular_bytes", "finalization.regular_bytes",
                     "worker_returns._read_limited", "command_server._file_digest"))


DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
RESOLVE_DISPATCH_NAMES = frozenset(("resolve", "__getattr__", "__getattribute__", "__setattr__", "__delattr__"))


def binds_name(node, names):
    # Some binding targets are stored as strings rather than ast.Name nodes.
    if isinstance(node, ast.Name):
        return isinstance(node.ctx, (ast.Store, ast.Del)) and node.id in names
    if isinstance(node, ast.alias):
        return (node.asname or node.name.split(".")[0]) in names
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return any(name in names for name in node.names)
    named_bindings = DEFINITIONS + (ast.ClassDef, ast.ExceptHandler, ast.MatchAs, ast.MatchStar)
    if isinstance(node, named_bindings):
        return node.name in names
    return isinstance(node, ast.MatchMapping) and node.rest in names


def class_body_nodes(node):
    """Walk a class namespace, including control-flow branches and definition headers."""
    yield node
    for field, value in ast.iter_fields(node):
        if field == "body" and isinstance(node, DEFINITIONS + (ast.ClassDef, ast.Lambda)):
            continue
        for child in value if isinstance(value, list) else (value,):
            if isinstance(child, ast.AST):
                yield from class_body_nodes(child)


def plain_run_store(tree):
    """Return the class when its resolve method has no conflicting visible dispatch."""
    stores = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RunStore"]
    if len(stores) != 1:
        return None
    store, = stores
    if store.bases or store.keywords or store.decorator_list:
        return None

    methods = [node for node in store.body
               if isinstance(node, ast.FunctionDef) and node.name == "resolve" and not node.decorator_list]
    if len(methods) != 1 or not instance_method(methods[0]):
        return None
    if any(binds_name(node, RESOLVE_DISPATCH_NAMES)
           for statement in store.body for node in class_body_nodes(statement) if node is not methods[0]):
        return None

    # An explicit attribute write can reach the class or an instance through an alias.
    # The source check deliberately does not infer aliases to classes or interpret reflection.
    for node in ast.walk(tree):
        if node is not store and binds_name(node, {"RunStore"}):
            return None
        if (isinstance(node, ast.Attribute) and node.attr in RESOLVE_DISPATCH_NAMES
                and isinstance(node.ctx, (ast.Store, ast.Del))):
            return None
        if isinstance(node, ast.ClassDef) and any(
                isinstance(part, ast.Name) and part.id == "RunStore"
                for base in node.bases for part in ast.walk(base)):
            return None
    return store


def instance_method(node):
    if not isinstance(node, DEFINITIONS) or node.decorator_list:
        return False
    parameters = node.args.posonlyargs + node.args.args
    return bool(parameters) and parameters[0].arg == "self"


def store_resolution_calls(source, tree):
    """Find direct calls on RunStore's own instance in the run_store module."""
    calls = set()

    class MethodBody(ast.NodeVisitor):
        def visit_Call(self, node):
            target = node.func
            if (isinstance(target, ast.Attribute) and target.attr == "resolve"
                    and isinstance(target.value, ast.Name) and target.value.id == "self"
                    and not node.args and not node.keywords):
                calls.add(node)
            self.generic_visit(node)

        def skip_scope(self, node):
            pass

        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = visit_Lambda = skip_scope
        visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_TypeAlias = skip_scope

    store = plain_run_store(tree)
    if store is None:
        return calls

    def scope_for(parent, node):
        scope = next(child for child in parent.get_children()
                     if (child.get_name(), child.get_lineno()) == (node.name, node.lineno))
        # Generic classes and methods have an enclosing type parameter scope on Python 3.12+.
        if getattr(node, "type_params", ()):
            scope = next(child for child in scope.get_children() if child.get_name() == node.name)
        return scope

    def rebinds_nonlocal_receiver(scope):
        if "self" in scope.get_identifiers():
            receiver = scope.lookup("self")
            if receiver.is_nonlocal() and (receiver.is_assigned() or receiver.is_imported()):
                return True
            if receiver.is_local() and scope.get_type() == "function":
                return False
        return any(rebinds_nonlocal_receiver(child) for child in scope.get_children())

    module_scope = symtable.symtable(source, "run_store.py", "exec")
    visitor = MethodBody()
    class_scope = scope_for(module_scope, store)
    for node in store.body:
        if not instance_method(node):
            continue
        # The symbol table tracks rebindings including imports, match captures and
        # assignment expressions. Closures can also write to the receiver via nonlocal.
        method_scope = scope_for(class_scope, node)
        receiver = method_scope.lookup("self")
        if (receiver.is_assigned() or receiver.is_imported()
                or any(rebinds_nonlocal_receiver(child) for child in method_scope.get_children())):
            continue
        for statement in node.body:
            visitor.visit(statement)
    return calls


def raw_calls(source, module):
    """Counter of (function, call) for each raw filesystem call and old reading helper in one module's source.

    Imports are followed to the names they bind (``from .util import lock as held`` makes ``held()``
    ``util.lock``, ``import harness.util as util`` makes ``util.tail()`` ``util.tail``); an unbound bare
    name is the module's own. A method is matched by its name alone, except that an ``open`` passing
    ``into=`` is the primitive's hand-over (``held.open(..., into=owner)``), not a raw open. A call to
    RunStore's own commit reconciliation method is also excluded.
    """
    tree = ast.parse(source)
    store_calls = store_resolution_calls(source, tree) if module == "run_store" else set()
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            package = node.module or ""
            if node.level == 0 and package.split(".")[0] == "harness":
                package = package[len("harness"):].lstrip(".")
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{package}.{alias.name}" if package else alias.name

    def qualified(target):
        parts = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if not isinstance(target, ast.Name):
            return None
        if not parts:
            name = bound.get(target.id, "builtins.open" if target.id == "open" else f"{module}.{target.id}")
        else:
            name = ".".join([bound.get(target.id, target.id)] + parts[::-1])
        return name.removeprefix("harness.")

    def method(call):
        if call in store_calls or not isinstance(call.func, ast.Attribute) or call.func.attr not in METHODS:
            return None
        if call.func.attr == "open" and any(keyword.arg == "into" for keyword in call.keywords):
            return None
        return call.func.attr

    found, functions = collections.Counter(), []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            functions.append(node.name)
            self.generic_visit(node)
            functions.pop()
        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            name = qualified(node.func)
            if name not in FUNCTIONS and name not in HELPERS:
                name = method(node)
            if name is not None:
                found[(functions[-1] if functions else "<module>", name)] += 1
            self.generic_visit(node)
    Visitor().visit(tree)
    return found


def guard(directory, table):
    """``{module: (calls not allowed, allowed calls not made)}`` for the modules of ``table`` that differ."""
    differences = {}
    for name, allowed in table.items():
        path = Path(directory) / name
        found, allowed = raw_calls(path.read_text(encoding="utf-8"), path.stem), collections.Counter(allowed)
        if found != allowed:
            differences[name] = (sorted((found - allowed).elements()), sorted((allowed - found).elements()))
    return differences


WEAK_MODULE = '''
import harness.util
import harness.util as util
from pathlib import Path
from . import provider_failures
from .util import read_json as load


def ledger(path):
    return Path(path).read_bytes()


def original(path):
    return provider_failures.read_original(path, lambda stream: stream.read())


def journal(path):
    return load(path)


def aliased(path):
    return util.read_json(path)


def dotted(path):
    return harness.util.read_json(path)


def canonical(path):
    return path.resolve() if path.exists() and path.exists() else path


def stream(path):
    return path.open("rb")


def through_the_primitive(files, held, owner):
    held.open(("runs", "run_a"), kind="directory", into=owner)
    with files.lock(("runs", "run_a", "run.lock")):
        return files.read(("runs", "run_a", "state.json"), limit=None).evidence()
'''


class MigratedModuleGuard(unittest.TestCase):
    def test_migrated_modules_make_exactly_their_listed_raw_calls(self):
        self.assertNotIn("state_files.py", MIGRATED)
        self.assertEqual(guard(ROOT / "harness", MIGRATED), {})
        source = (ROOT / "harness" / "run_store.py").read_text(encoding="utf-8")
        self.assertIsNotNone(plain_run_store(ast.parse(source)))
        self.assertEqual(raw_calls(source, "other_module"), {("keep_terminal", "resolve"): 1})

    def test_only_direct_run_store_instance_calls_are_excluded(self):
        source = """
from pathlib import Path

class RunStore:
    def resolve(self, strict=False):
        pass

    def keep_terminal(self, path):
        self.resolve()
        self.path.resolve()
        Path(path).resolve()
        self.resolve(strict=True)
        self.resolve(True)

    def outer(self):
        def inner():
            self.resolve()
        lambda: self.resolve()
        [self.resolve() for item in ()]
        class Nested:
            def call(self):
                self.resolve()

    def rebound(self, path):
        self = path
        self.resolve()

    def rebound_by_import(self):
        from .paths import root as self
        self.resolve()

    def rebound_in_inner(self, path):
        def rebind():
            nonlocal self
            self = path
        rebind()
        self.resolve()

    def shadowed_in_inner(self, path):
        def inner():
            self = path
            def rebind():
                nonlocal self
                self = self.parent
            rebind()
            self.resolve()
        self.resolve()

    def different_receiver(receiver, self):
        self.resolve()

    @staticmethod
    def static(self):
        self.resolve()

class Other:
    def resolve(self):
        pass

    def ordinary(self):
        self.resolve()
"""
        expected = collections.Counter({
            ("keep_terminal", "resolve"): 4,
            ("outer", "resolve"): 2,
            ("inner", "resolve"): 2,
            ("call", "resolve"): 1,
            ("rebound", "resolve"): 1,
            ("rebound_by_import", "resolve"): 1,
            ("rebound_in_inner", "resolve"): 1,
            ("different_receiver", "resolve"): 1,
            ("static", "resolve"): 1,
            ("ordinary", "resolve"): 1,
        })
        self.assertEqual(raw_calls(source, "run_store"), expected)
        expected.update([("keep_terminal", "resolve"), ("shadowed_in_inner", "resolve")])
        self.assertEqual(raw_calls(source, "another_module"), expected)

    def test_run_store_requires_a_top_level_class_and_its_own_resolve_definition(self):
        source = """
class Resolver:
    def resolve(self):
        pass

class RunStore(Resolver):
    def inherited(self):
        self.resolve()

class Outer:
    class RunStore:
        def resolve(self):
            pass

        def nested(self):
            self.resolve()
"""
        self.assertEqual(raw_calls(source, "run_store"), {("inherited", "resolve"): 1, ("nested", "resolve"): 1})

    def test_class_and_instance_replacements_restore_the_path_call_count(self):
        original = """
from pathlib import Path

class RunStore:
    def resolve(self):
        pass

    def keep_terminal(self):
        self.resolve()
"""
        self.assertEqual(raw_calls(original, "run_store"), {})
        replacements = (
            "    resolve = Path('.').resolve\n",
            "    def __init__(self):\n        self.resolve = Path('.').resolve\n",
        )
        for replacement in replacements:
            with self.subTest(replacement=replacement):
                source = original + replacement
                self.assertIsNone(plain_run_store(ast.parse(source)))
                self.assertEqual(raw_calls(source, "run_store"), {("keep_terminal", "resolve"): 1})

    def test_visible_dispatch_changes_keep_only_actual_calls_in_the_count(self):
        original = """
class RunStore:
    def resolve(self):
        pass

    def keep_terminal(self):
        self.resolve()
"""
        changes = (
            "    if configured:\n        from paths import resolve\n",
            "    if configured:\n        del resolve\n",
            "    if configured:\n        def resolve(self):\n            pass\n",
            "    match options:\n        case {'resolve': resolve}:\n            pass\n",
            "    def __getattribute__(self, name):\n        return lookup(name)\n",
            "RunStore.resolve = alternate.resolve\n",
            "del RunStore.resolve\n",
            "class Child(RunStore):\n    resolve = alternate.resolve\n",
            "class RunStore:\n    pass\n",
            "from paths import Store as RunStore\n",
        )
        for change in changes:
            with self.subTest(change=change):
                source = original + change
                self.assertIsNone(plain_run_store(ast.parse(source)))
                self.assertEqual(raw_calls(source, "run_store"), {("keep_terminal", "resolve"): 1})

    def test_the_guard_fails_on_a_weak_read_and_on_a_stale_entry(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        (directory / "weak.py").write_text(WEAK_MODULE, encoding="utf-8")
        # The old helper under every import form, resolve() and a raw open count; the primitive's held.open does not.
        weak = [("aliased", "util.read_json"), ("canonical", "exists"), ("canonical", "exists"), ("canonical", "resolve"),
                ("dotted", "util.read_json"), ("journal", "util.read_json"), ("ledger", "read_bytes"),
                ("original", "provider_failures.read_original"), ("stream", "open")]
        self.assertEqual(guard(directory, {"weak.py": []}), {"weak.py": (weak, [])})
        self.assertEqual(guard(directory, {"weak.py": weak}), {})
        stale = weak + [("ledger", "exists")]
        self.assertEqual(guard(directory, {"weak.py": stale}), {"weak.py": ([], [("ledger", "exists")])})
        # Calls are compared by count: a call allowed once and made twice fails.
        once = [entry for entry in weak if entry != ("canonical", "exists")] + [("canonical", "exists")]
        self.assertEqual(guard(directory, {"weak.py": once}), {"weak.py": ([("canonical", "exists")], [])})


if __name__ == "__main__":
    unittest.main()
