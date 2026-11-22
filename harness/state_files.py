"""Reads of the files Harness writes below a state directory, one reading for every caller.

A call walks from the state directory: the root and each intermediate directory open with search
permission only (O_SEARCH or O_PATH) and O_NOFOLLOW from their parent descriptor, the final
component opens with O_NOFOLLOW|O_NONBLOCK and must fstat as a regular file. A FIFO, a link or a
directory where a file belongs is refused at once, never waited on or followed. Each call returns
an Outcome (a Listing for ``entries``) and never raises OSError: ``read``, ``absent``, ``refused``,
``too_large`` or ``failed``, with the level and stage that decided it. Only an open's errno that
says the name is of another type is looked up again to tell what stands there; any other failure,
EIO above all, stays a failure and is never told as an absence. Only the accessors turn an outcome
into an exception.

Every descriptor is opened and recorded in the call's ``held`` list with SIGINT, SIGTERM and SIGHUP
blocked, so the interrupt that arrives right after an open leaves only once the descriptor is where
the call closes it. The call releases ``held`` inside the same ``try`` as its body and releases it
again on the way out when anything cut it short; a failing mask call is a failed call like any other.
A StateFiles value holds no descriptor: each call opens the root by path and closes everything it
opened before returning.

Three things hold descriptors past one call, each one the closer of its descriptors before it takes any. A
LockedDirectory holds a lock file and the directory it lies in, both its own, for a ``with`` block that
runs only once the lock is taken (LockUnavailable otherwise): the ledger is read and replaced through that
directory's descriptor, and ``identity`` tells it from a directory put at its path since; asked for one identity,
a lock is neither taken nor its file made in another directory. A HeldFiles
(``StateFiles.held``) opens the root when its ``with`` block is entered, for an observation that only
reads below it. ``HeldFiles.open`` hands a descriptor to the caller's Owned. Descriptors are registered in
their closer while the signals are blocked; entering that does not finish closes them and leaves the
holder unusable, and a cleanup an interrupt cuts short runs again until it completes before the interrupt
leaves. A holder's own cleanup cannot see a signal at its entry, so ``holding`` enters a holder inside two
nested cleanups set up beforehand: one signal anywhere leaves nothing open. Nothing is left to garbage collection.
"""
from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import os
import signal
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from . import stops
from .util import EvidenceError, HarnessError


CHUNK = 1024 * 1024
# Search permission alone opens a directory to look names up below it (macOS O_SEARCH, Linux O_PATH);
# without either, read permission is the fallback.
SEARCH = (getattr(os, "O_SEARCH", 0) or getattr(os, "O_PATH", 0) or os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
LISTING = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
LEAF = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
# A lock file opens without blocking on a FIFO; written by an exclusive lock (NFS emulates an exclusive flock
# only on a descriptor open for writing), only read by a shared one.
LOCK = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
# Darwin answers ENOENT to an openat(O_CREAT) that loses the race to create the same name to another process (an open
# by path does not), so a lock file being created is opened again at once from the same directory descriptor. With 2 to
# 16 processes racing, the next attempt always opened it, without waiting; the further ones cover a name removed and raced
# for again in between, and the bound ends a directory removed under its descriptor, which answers ENOENT every time.
CREATE_AGAIN = 3
TEMPORARY = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
SIGNALS = frozenset((signal.SIGINT, signal.SIGTERM, signal.SIGHUP))
# What an open reports for a name of another type: a link under O_NOFOLLOW (ELOOP, FreeBSD EMLINK, macOS
# ENOTDIR under O_DIRECTORY), a file under O_DIRECTORY, a socket or device. Values this platform lacks are skipped.
PATH_ERRNOS = frozenset(getattr(errno, name) for name in ("ELOOP", "EMLINK", "ENOTDIR", "EISDIR", "ENXIO", "ENODEV",
                                                          "EOPNOTSUPP", "ENOTSUP") if hasattr(errno, name))


class Step(NamedTuple):
    """The level that decided an outcome: ``"root"`` or the index of the component in the walked path.

    ``stage`` is resolve, open (the table's opening of that level, a probe's stat and the root's
    lookup check included), fstat, read or close; lock, mkdir, write or rename for the calls that
    lock or write. ``leaf`` says the level is the last one the call walks to (the listed directory
    for ``entries``).
    """
    level: object
    name: str
    stage: str
    leaf: bool


@dataclass(frozen=True)
class Outcome:
    """One read's result. ``state``: read, absent, refused, too_large or failed (a probe: present instead of read).

    The calls that hold or write decide their own success instead of read: locked, busy or replaced (a lock), opened
    (``HeldFiles.open``), present (``make_directories``), written (``LockedDirectory.replace``).

    ``found`` is what stood there when refused (symlink, not_directory, fifo, directory, device,
    socket, other, or changed: rewritten in place while read whole); ``errno`` the failure's errno;
    ``data`` the bytes read (a digest's kept head), ``size`` the size when opened, ``changed`` whether
    a head or tail read saw the file change.
    """
    state: str
    path: str
    step: Step
    found: str | None = None
    errno: int | None = None
    data: bytes | None = None
    digest: str | None = None
    size: int | None = None
    changed: bool = False

    @property
    def error_type(self):
        # The class an OSError with this errno has (PermissionError, ...): the type stops keep today.
        return type(OSError(self.errno, "")).__name__ if self.state == "failed" else None

    def evidence(self, source=None):
        """The bytes read; absent, refused or over the limit is damaged evidence, a failed read local I/O."""
        if self.state == "read":
            return self.data
        if self.state == "failed":
            raise StateReadError(self)
        if self.state not in ("absent", "refused", "too_large"):
            raise ValueError(f"evidence() of a {self.state} outcome")
        what = {"absent": "缺失", "refused": f"被拒绝（{self.found}）", "too_large": "超过读取上限"}[self.state]
        raise EvidenceError(f"运行时证据{what}：{self.path}（{self.step.name}）；保留证据",
                            stop=stops.make("evidence", "runtime_integrity", source=source or {}))

    def or_local_io(self):
        """This outcome, unless the read failed: then the StateReadError, before any business meaning is read into it."""
        if self.state == "failed":
            raise StateReadError(self)
        return self

    def bytes_or_none(self):
        """The bytes read or None, whatever went wrong: for the reads made while reporting another failure."""
        return self.data if self.state == "read" else None


@dataclass(frozen=True)
class Listing(Outcome):
    """A directory's entries, ``state`` listed: (name, kind) sorted by name.

    ``kind`` is file, dir, symlink or other (not followed), or absent: the name was listed and gone when its type was read.
    """
    entries: tuple = ()


class StateReadError(HarnessError):
    """A state file could not be read (local I/O, permission included): never told as missing.

    A typed HarnessError, not the OSError again, so no ``except OSError`` that means "absent" can swallow it.
    """

    def __init__(self, outcome):
        self.outcome = outcome
        reason = os.strerror(outcome.errno) if outcome.errno is not None else outcome.error_type
        doing = {"lock": "加锁", "mkdir": "建目录", "write": "写入", "rename": "写入"}.get(outcome.step.stage, "读取")
        super().__init__(f"状态文件{doing}失败：{outcome.path}（{outcome.step.stage} {outcome.step.name}：{reason}）",
                         stop=stops.make("runtime_failure", "io", data={"error_type": outcome.error_type}))


class LockUnavailable(Exception):
    """A lock not taken: ``outcome`` is busy, replaced, absent, refused or failed, and the ``with`` block never ran.

    Not a stop of its own: each entry maps the outcome to its stop (design 4, 3.5). A preview goes on without a lock
    only when the lock file alone is missing: absent with ``outcome.step.leaf`` true.
    """

    def __init__(self, outcome):
        self.outcome = outcome
        super().__init__(f"lock {outcome.state}: {outcome.path} ({outcome.step.stage} {outcome.step.name})")


class _Decided(Exception):
    def __init__(self, outcome):
        self.outcome = outcome


class _MaskFailed(Exception):
    """A pthread_sigmask call failed: the step it guards fails with its errno, never classified like that step's own."""

    def __init__(self, error):
        super().__init__(error)
        self.errno = error.errno


def _masked(work, *args):
    """``work(*args)`` with SIGINT, SIGTERM and SIGHUP blocked; the previous mask is back on every way out.

    The mask is read before it changes, so every change is inside the ``try``: an interrupt raised by the
    blocking call (CPython runs pending handlers once the mask applies), or by a handler that runs right
    after it returns (a signal another, unmasked thread received runs its handler here whatever this
    thread's mask), leaves through the ``finally``. A failing mask call raises _MaskFailed, except that a
    restore failing while the guarded code itself leaves with KeyboardInterrupt or SystemExit lets that go on.
    """
    try:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, ())
    except OSError as error:
        raise _MaskFailed(error) from None
    # This call's own way out, not __context__ or sys.exception(): those also see an interrupt the caller has caught.
    interrupted = False
    try:
        try:
            signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
        except OSError as error:
            raise _MaskFailed(error) from None
        return work(*args)
    except (KeyboardInterrupt, SystemExit):
        interrupted = True
        raise
    finally:
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        except OSError as error:
            if not interrupted:
                raise _MaskFailed(error) from None


def _opened(held, name, flags, dir_fd, level, mode):
    """Run masked: the descriptor is in ``held`` before an interrupt can leave the unmask."""
    fd = os.open(name, flags, mode, dir_fd=dir_fd)
    held.append((fd, level))
    return fd


def _close(held, keep=0):
    """Close all but the last ``keep`` entries of ``held``, the innermost first."""
    failure = None
    while len(held) > keep:
        fd, level = held.pop(len(held) - keep - 1)
        try:
            os.close(fd)
        except OSError as error:
            failure = failure or (level, error.errno)
    return failure


def _release(held, keep=0):
    """Close every descriptor in ``held`` (all but the last ``keep``), signals blocked; ``(level, errno)`` of the first failure.

    A mask call's failure counts as one. Safe to call again: ``held`` keeps only what is still open, so a release an interrupt cut short (at
    its entry or anywhere inside) is finished by the next. A failing mask call still closes everything.
    """
    if len(held) <= keep:
        return None
    level = held[-1 - keep][1]
    try:
        return _masked(_close, held, keep)
    except _MaskFailed as error:
        _close(held, keep)
        return level, error.errno


def _close_one(held, fd):
    """Take ``fd`` out of ``held`` and close it; the errno of a failing close, or None."""
    for index, (each, _) in enumerate(held):
        if each == fd:
            del held[index]
            break
    else:
        raise ValueError(f"descriptor {fd} is not held here")
    try:
        os.close(fd)
    except OSError as error:
        return error.errno
    return None


def _release_one(held, fd):
    """``_close_one`` with the signals blocked; a failing mask call still closes it and is the errno returned."""
    try:
        return _masked(_close_one, held, fd)
    except _MaskFailed as error:
        if any(each == fd for each, _ in held):
            _close_one(held, fd)
        return error.errno


def _hand(held, fd, owner):
    """Run masked: ``fd`` moves from the call's ``held`` to ``owner``, so an interrupt at the unmask finds it there."""
    index = next(index for index, (each, _) in enumerate(held) if each == fd)
    owner.add(fd)
    del held[index]


def _settle(shut):
    """``shut()`` until one run of it completes: in the handler of the interrupt that cut it short, which then leaves.

    ``shut`` makes a holder unusable and releases what it still holds, so running it again finishes it; an interrupt
    inside this loop's ``try`` only starts it over. Only one signal per call is covered: a second one landing at this
    function's entry, before its ``try``, leaves with the holder as the first one left it.
    """
    while True:
        try:
            shut()
            return
        except (KeyboardInterrupt, SystemExit):
            pass


def _run(walk, body):
    """``body(walk)``'s outcome, every descriptor the walk opened closed; a failing close fails a call that had not."""
    try:
        try:
            outcome = body(walk)
        except _Decided as decided:
            outcome = decided.outcome
        closed = _release(walk.held)
    except BaseException:
        # The release runs inside the same try as the body: an interrupt between them, or at the release's
        # entry, comes here, and this release closes what the first one did not reach.
        _release(walk.held)
        raise
    if closed is not None and outcome.state != "failed":
        level, code = closed
        return walk.outcome("failed", walk.step(level, "close"), errno=code)
    return outcome


def _found(mode, required):
    """What stands where a ``required`` ("file" or "directory") was expected; None when it is one."""
    if stat.S_ISREG(mode):
        return None if required == "file" else "not_directory"
    if stat.S_ISDIR(mode):
        return None if required == "directory" else "directory"
    for test, name in ((stat.S_ISLNK, "symlink"), (stat.S_ISFIFO, "fifo"), (stat.S_ISSOCK, "socket"),
                       (stat.S_ISCHR, "device"), (stat.S_ISBLK, "device")):
        if test(mode):
            return name
    return "other"


def _kind(mode):
    return ("file" if stat.S_ISREG(mode) else "dir" if stat.S_ISDIR(mode)
            else "symlink" if stat.S_ISLNK(mode) else "other")


def _components(components):
    """Names built from verified identifiers and fixed file names; anything else is a programming error."""
    if isinstance(components, (str, bytes)):
        raise TypeError("state path components are a sequence of names, not one string")
    checked = tuple(components)
    for name in checked:
        if type(name) is not str or name in ("", ".", "..") or "/" in name or "\0" in name:
            raise ValueError(f"invalid state path component: {name!r}")
    return checked


class _Walk:
    """One call: the descriptors it holds and the outcome it decides (raised as _Decided).

    ``anchor`` is a held root the walk starts from instead of opening the path; ``below`` a held directory
    the last component opens from (a LockedDirectory's), with ``root`` the resolved root it was walked from.
    Neither is in ``held``: the call never closes them.
    """

    def __init__(self, given, components, result, *, anchor=None, below=None, root=None):
        self.given, self.components, self.result = given, components, result
        self.anchor, self.below = anchor, below
        self.held, self.root = [], root

    def text(self):
        return os.path.join(self.root if self.root is not None else str(self.given), *self.components)

    def step(self, level, stage):
        if level == "root":
            return Step("root", self.root if self.root is not None else str(self.given), stage, not self.components)
        return Step(level, self.components[level], stage, level == len(self.components) - 1)

    def outcome(self, state, step, **fields):
        return self.result(state, self.text(), step, **fields)

    def decide(self, state, step, **fields):
        raise _Decided(self.outcome(state, step, **fields))

    def fail(self, step, code):
        self.decide("failed", step, errno=code)

    def open(self, name, flags, dir_fd, level, required, mode=0o777, again=0):
        """One level opened from ``dir_fd``; its own ENOENT is tried ``again`` more times from the same descriptor."""
        try:
            return _masked(_opened, self.held, name, flags, dir_fd, level, mode)
        except _MaskFailed as error:
            self.fail(self.step(level, "open"), error.errno)
        except OSError as error:
            step, code = self.step(level, "open"), error.errno
            if code == errno.ENOENT:
                if again:
                    return self.open(name, flags, dir_fd, level, required, mode, again - 1)
                self.decide("absent", step)
            if code in PATH_ERRNOS:
                try:
                    mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
                except OSError as again:
                    if again.errno == errno.ENOENT:
                        self.decide("absent", step)
                else:
                    found = _found(mode, required)
                    if found is not None:
                        self.decide("refused", step, found=found)
            self.fail(step, code)

    def open_root(self):
        if self.anchor is not None:
            return self.anchor.descriptor(self)
        try:
            self.root = str(Path(self.given).resolve())
        except RuntimeError:  # 3.11 resolves a symbolic link loop with this
            self.fail(self.step("root", "resolve"), errno.ELOOP)
        except ValueError:    # a NUL byte
            self.fail(self.step("root", "resolve"), errno.EINVAL)
        except OSError as error:
            self.fail(self.step("root", "resolve"), error.errno)
        fd = self.open(self.root, SEARCH, None, "root", "directory")
        try:
            # O_PATH opens with no permission at all: the root counts only once a lookup below it works.
            os.stat(".", dir_fd=fd)
        except OSError as error:
            self.lookup_failure(error, self.step("root", "open"))
        return fd

    def lookup_failure(self, error, step):
        """A stat that is itself the check: absent or failed, never looked up again."""
        if error.errno == errno.ENOENT:
            self.decide("absent", step)
        self.fail(step, error.errno)

    def parent(self):
        """The directory holding the last component: the root and every intermediate level, opened by search."""
        if self.below is not None:
            return self.below
        fd = self.open_root()
        for level, name in enumerate(self.components[:-1]):
            fd = self.open(name, SEARCH, fd, level, "directory")
        return fd

    def last(self):
        return len(self.components) - 1, self.components[-1]

    def leaf(self):
        parent = self.parent()
        level, name = self.last()
        fd = self.open(name, LEAF, parent, level, "file")
        info = self.fstat(fd, level)
        found = _found(info.st_mode, "file")
        if found is not None:
            self.decide("refused", self.step(level, "fstat"), found=found)
        return fd, info, level

    def fstat(self, fd, level):
        try:
            return os.fstat(fd)
        except OSError as error:
            self.fail(self.step(level, "fstat"), error.errno)

    def changed(self, fd, info, level):
        # Size and modification time only: the runtime's atomic replacement changes ctime and nlink of the
        # inode being read, never its bytes, and must not count as a change.
        after = self.fstat(fd, level)
        return (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns)

    def chunks(self, fd, level, offset=0, length=None):
        """The bytes from ``offset`` to the end of file, or at most ``length`` of them."""
        while length is None or length > 0:
            try:
                chunk = os.pread(fd, CHUNK if length is None else min(CHUNK, length), offset)
            except OSError as error:
                self.fail(self.step(level, "read"), error.errno)
            if not chunk:
                return
            yield chunk
            offset += len(chunk)
            if length is not None:
                length -= len(chunk)

    def read(self, limit, part):
        fd, info, level = self.leaf()
        size, step = info.st_size, self.step(level, "read")
        if part == "whole":
            if limit is not None and size > limit:
                return self.outcome("too_large", step, size=size)
            # One byte past the limit tells a file that grew while read from one that fits.
            data = b"".join(self.chunks(fd, level, 0, None if limit is None else limit + 1))
            if limit is not None and len(data) > limit:
                return self.outcome("too_large", step, size=size)
        elif part == "head":
            data = b"".join(self.chunks(fd, level, 0, limit))
        else:
            offset = max(0, size - limit)
            data = b"".join(self.chunks(fd, level, offset, size - offset))
        changed = self.changed(fd, info, level)
        if changed and part == "whole":  # possibly mixed bytes: neither parsed nor bound
            return self.outcome("refused", step, found="changed", size=size)
        return self.outcome("read", step, data=data, size=size, changed=changed)

    def digest(self, keep):
        fd, info, level = self.leaf()
        hasher, kept = hashlib.sha256(), bytearray()
        for chunk in self.chunks(fd, level):
            hasher.update(chunk)
            if keep and len(kept) <= keep:
                kept += chunk[:keep + 1 - len(kept)]
        step = self.step(level, "read")
        if self.changed(fd, info, level):
            return self.outcome("refused", step, found="changed", size=info.st_size)
        return self.outcome("read", step, data=bytes(kept) if keep else None, digest=hasher.hexdigest(),
                            size=info.st_size)

    def probe(self, expect):
        parent = self.parent()
        level, name = self.last()
        step = self.step(level, "open")
        try:
            mode = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
        except OSError as error:
            self.lookup_failure(error, step)
        found = _found(mode, expect)
        return self.outcome("present", step) if found is None else self.outcome("refused", step, found=found)

    def entries(self):
        if self.components:
            parent = self.parent()
            level, name = self.last()
        else:
            parent, level, name = self.open_root(), "root", "."
        fd = self.open(name, LISTING, parent, level, "directory")
        try:
            with os.scandir(fd) as iterator:
                names = sorted(entry.name for entry in iterator)
        except OSError as error:
            self.fail(self.step(level, "read"), error.errno)
        entries = []
        for entry in names:
            try:
                kind = _kind(os.stat(entry, dir_fd=fd, follow_symlinks=False).st_mode)
            except OSError as error:
                if error.errno != errno.ENOENT:
                    self.fail(self.step(level, "read"), error.errno)
                kind = "absent"  # gone since it was listed: kept, so a caller requiring every entry sees it
            entries.append((entry, kind))
        return self.outcome("listed", self.step(level, "read"), entries=tuple(entries))

    def lock(self, mode, create, wait, identity):
        """The lock file opened in its directory, then flocked; ``locked`` is the directory's identity and descriptor.

        The identity is read from the directory's descriptor before the lock file is opened in it: another than
        ``identity``, when given, is ``replaced``, with nothing opened or made there.
        """
        parent = self.parent()
        level, name = self.last()
        above = level - 1 if level else "root"
        info = self.fstat(parent, above)
        directory = info.st_dev, info.st_ino
        if identity is not None and directory != identity:
            self.decide("replaced", self.step(above, "fstat"))
        flags = (os.O_RDWR if mode == "exclusive" else os.O_RDONLY) | (os.O_CREAT if create else 0) | LOCK
        fd = self.open(name, flags, parent, level, "file", 0o600, CREATE_AGAIN if create else 0)
        found = _found(self.fstat(fd, level).st_mode, "file")
        if found is not None:
            self.decide("refused", self.step(level, "fstat"), found=found)
        step = self.step(level, "lock")
        try:
            # With ``wait`` this blocks outside the mask: an interrupt while waiting leaves through the holder's cleanup.
            fcntl.flock(fd, (fcntl.LOCK_EX if mode == "exclusive" else fcntl.LOCK_SH) | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            self.decide("busy", step)
        except OSError as error:
            self.fail(step, error.errno)
        self.locked = directory, parent
        return self.outcome("locked", step)

    def make_directories(self):
        fd = self.open_root()
        for level, name in enumerate(self.components):
            try:
                fd = self.open(name, SEARCH, fd, level, "directory")
                continue
            except _Decided as decided:
                if decided.outcome.state != "absent":
                    raise
            try:
                os.mkdir(name, 0o777, dir_fd=fd)
            except FileExistsError:
                pass  # made since the open failed: whatever stands there now is opened as any level is
            except OSError as error:
                self.fail(self.step(level, "mkdir"), error.errno)
            fd = self.open(name, SEARCH, fd, level, "directory")
        return self.outcome("present", self.step(len(self.components) - 1, "open"))

    def replace(self, data):
        parent = self.parent()
        level, name = self.last()
        temporary = f"{name}.{uuid.uuid4().hex}.tmp"
        fd = self.open(temporary, TEMPORARY, parent, level, "file", 0o666)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except OSError as error:
            self.fail(self.step(level, "write"), error.errno)
        # Closed before the rename: once the name is replaced, nothing may turn the call into a failure.
        code = _release_one(self.held, fd)
        if code is not None:
            self.fail(self.step(level, "close"), code)
        try:
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        except OSError as error:
            self.fail(self.step(level, "rename"), error.errno)
        return self.outcome("written", self.step(level, "rename"), size=len(data))

    def hand_over(self, kind, owner, handed):
        size = None
        if kind == "file":
            fd, info, level = self.leaf()
            size = info.st_size
        elif self.components:
            parent = self.parent()
            level, name = self.last()
            fd = self.open(name, SEARCH, parent, level, "directory")
        else:
            level = "root"
            fd = self.open(".", SEARCH, self.open_root(), level, "directory")
        try:
            _masked(_hand, self.held, fd, owner)
        except _MaskFailed as error:
            if not any(each == fd for each, _ in self.held):  # handed before the restore failed: taken back
                owner.release(fd)
            self.fail(self.step(level, "open"), error.errno)
        handed.append(fd)
        return self.outcome("opened", self.step(level, "open"), size=size)


class _Root:
    """The root a HeldFiles opens when entered, shared by its ``under`` copies: new, entering, entered, then closed.

    Entered, ``held`` has the root or ``failure`` what opening it decided; ``text`` is the path it resolved to.
    """

    def __init__(self):
        self.held, self.text, self.failure, self.state = [], None, None, "new"

    def descriptor(self, walk):
        if self.state != "entered":
            raise ValueError("the held state root is not open")
        walk.root = self.text
        if self.failure is not None:  # every call decides what opening the root decided
            walk.decide(self.failure.state, walk.step("root", self.failure.step.stage), found=self.failure.found,
                        errno=self.failure.errno)
        return self.held[0][0]


class _Files:
    """The reads below a state directory, each walking from the root the class gives: opened by path, or held."""

    _anchor = None

    def __init__(self, path, *, prefix=()):
        self.path, self.prefix = path, _components(prefix)

    def under(self, *components):
        """The same root with ``components`` before every path: a value, nothing is opened."""
        files = copy.copy(self)
        files.prefix = self.prefix + _components(components)
        return files

    def _walk(self, components, result=Outcome, *, named=True):
        walk = _Walk(self.path, self.prefix + _components(components), result, anchor=self._anchor)
        if named and not walk.components:
            raise ValueError("a state file read needs at least one component")
        return walk

    def _call(self, components, body, result=Outcome, *, named=True):
        return _run(self._walk(components, result, named=named), body)

    def read(self, components, *, limit, part="whole"):
        """A regular file: whole (``too_large`` past ``limit``; ``limit=None`` reads to the end), or its head or tail."""
        if part not in ("whole", "head", "tail"):
            raise ValueError(f"unknown part: {part!r}")
        if limit is None and part != "whole":
            raise ValueError("only a whole read may be unbounded")
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError(f"invalid limit: {limit!r}")
        return self._call(components, lambda walk: walk.read(limit, part))

    def digest(self, components, *, keep=0):
        """SHA-256 of the whole file, streamed in 1 MiB chunks; with ``keep``, its first ``keep + 1`` bytes too. Never too_large."""
        if type(keep) is not int or keep < 0:
            raise ValueError(f"invalid keep: {keep!r}")
        return self._call(components, lambda walk: walk.digest(keep))

    def probe(self, components, *, expect="file"):
        """``present``, ``absent``, ``refused`` or ``failed`` for a regular file or a directory; nothing is read."""
        if expect not in ("file", "directory"):
            raise ValueError(f"unknown expectation: {expect!r}")
        return self._call(components, lambda walk: walk.probe(expect))

    def entries(self, components=()):
        """The Listing of a directory (the root itself for no components); needs read permission on it."""
        return self._call(components, lambda walk: walk.entries(), Listing, named=False)

    def recorded(self, path):
        """The components of a recorded absolute path below this root and prefix, or None; the path is never opened.

        None as well for a path with an empty, ``.`` or ``..`` segment: a recorded path is compared, never normalized.
        """
        if type(path) is not str or "\0" in path:
            return None
        root = self._bound()
        if root is None:
            return None
        head = os.path.join(root, *self.prefix).rstrip("/") + "/"
        if not path.startswith(head):
            return None
        parts = tuple(path[len(head):].split("/"))
        return None if any(part in ("", ".", "..") for part in parts) else parts


class StateFiles(_Files):
    """Reads below one state directory, its locks and the directories made in it.

    Holds no descriptor: each call opens the root by path and closes what it opened.
    """

    @classmethod
    def nofollow_directory(cls, path):
        """Walk an absolute evidence directory too, so it cannot redirect reads by being a link."""
        directory = Path(path)
        if not directory.is_absolute():
            raise ValueError("evidence directory must be absolute")
        return cls(directory.anchor, prefix=directory.parts[1:])

    @staticmethod
    def held(path):
        """For an observation: the HeldFiles that opens the root once, when its ``with`` block is entered."""
        return HeldFiles(path)

    def lock(self, components, *, mode="exclusive", create, wait=False, identity=None):
        """The LockedDirectory of the lock file at ``components``, taken when its ``with`` block is entered.

        Exclusive opens the file for writing, shared only for reading; ``create`` makes a missing one a regular file
        opened with mode 0600, which the umask still narrows (never the directories above it, never through a link);
        ``wait`` blocks for the lock. ``identity``, the (st_dev, st_ino) the lock file's directory must have, is checked
        on that directory's descriptor before the lock file is opened or made there.
        """
        if mode not in ("exclusive", "shared"):
            raise ValueError(f"unknown lock mode: {mode!r}")
        return LockedDirectory(self._walk(components), mode, create, wait, identity)

    def make_directories(self, components):
        """``present`` once every level is a directory: each opened by search, a missing one made in its parent first.

        A link or a non-directory at any level is refused as a read refuses it; nothing is followed.
        """
        return self._call(components, lambda walk: walk.make_directories())

    def _bound(self):
        try:
            return str(Path(self.path).resolve())
        except (OSError, RuntimeError, ValueError):
            return None


def holding(holder, use):
    """Return ``use(holder)`` for a LockedDirectory, HeldFiles or Owned entered here, closing it in two nested cleanups.

    Both are set up before entering takes any descriptor, and each closes what is still held: one signal anywhere, at
    a cleanup's entry included, where the holder's own ``__exit__`` or ``close`` cannot see it, leaves with every
    descriptor closed and the holder unusable; the outer cleanup closes what the inner one did not reach. A second
    signal while the first one's cleanup runs is not covered. A lock not taken raises LockUnavailable and ``use`` never
    runs. Leaving without an exception, a failing close or mask call raises StateReadError (stage close) once; an
    exception already leaving is never replaced. ``holder`` must be a lock or held root not yet entered, or an Owned
    not yet handed any descriptor: what it took before this call is outside both cleanups.
    """
    try:
        try:
            holder.__enter__()
            result = use(holder)
        finally:
            closed = holder._shut()
    finally:
        holder._shut()
    if closed is not None:  # reached only when nothing is leaving
        raise holder._failure(closed)
    return result


class LockedDirectory:
    """A lock held for a ``with`` block (``StateFiles.lock``): its own descriptors of the lock file and its directory.

    Entering takes the lock or raises LockUnavailable with the outcome: busy (another holder, never with ``wait``),
    replaced (the directory is not the ``identity`` asked for; no lock file was opened or made in it), or absent,
    refused or failed as a read decides them, a lock file not created included. So the block runs only while
    the lock is held, and only then are ``identity`` (the directory's (st_dev, st_ino)), ``read`` and, for an exclusive
    lock, ``replace`` usable; they go through the directory's descriptor whatever has been put at its path since.
    Entering registers each descriptor here inside the mask and, when it does not finish (an interrupt at an unmask or
    while ``wait`` blocks, a lock not taken), closes them and leaves the lock unusable: ``__exit__`` never runs for an
    ``__enter__`` that did not return. Leaving reports a failing close or mask call as StateReadError (stage close),
    unless an exception already leaves the block. A lock is taken through ``holding``: a signal at ``__exit__``'s entry
    or as ``__enter__`` returns skips this object's own cleanup, so a bare ``with`` belongs only inside such a scope.
    """

    def __init__(self, walk, mode, create, wait, identity):
        self._walk, self._mode, self._create, self._wait, self._expected = walk, mode, create, wait, identity
        # The closer before any descriptor: whatever the walk opens is in it.
        self._held, self._entered, self._directory, self._identity = walk.held, False, None, None
        self.outcome = None

    def __enter__(self):
        if self._entered:
            raise RuntimeError("a lock is entered once")
        self._entered = True
        walk = self._walk
        try:
            try:
                outcome = walk.lock(self._mode, self._create, self._wait, self._expected)
            except _Decided as decided:
                outcome = decided.outcome
            # Locked, it keeps the directory and the lock file, the last two opened; otherwise nothing.
            closed = _release(self._held, 2 if outcome.state == "locked" else 0)
            if closed is not None:
                _release(self._held)
                if outcome.state != "failed":
                    outcome = walk.outcome("failed", walk.step(closed[0], "close"), errno=closed[1])
            self.outcome = outcome
            if outcome.state != "locked":
                raise LockUnavailable(outcome)
            self._identity, self._directory = walk.locked  # usable from here on: the last thing entering does
            return self
        except BaseException:
            _settle(self._shut)
            raise

    def __exit__(self, kind, value, traceback):
        try:
            closed = self._shut()
        except BaseException:
            _settle(self._shut)
            raise
        if closed is not None and kind is None:  # an exception already leaving the block is never replaced
            raise self._failure(closed)

    def _shut(self):
        # Unusable, and never entered again, before anything closes: a closed number may be handed out again.
        self._directory, self._entered = None, True
        return _release(self._held)

    def _failure(self, closed):
        walk = self._walk
        return StateReadError(walk.outcome("failed", walk.step(closed[0], "close"), errno=closed[1]))

    def _locked(self):
        if self._directory is None:
            raise RuntimeError("the lock is not held")
        return self._directory

    @property
    def identity(self):
        self._locked()
        return self._identity

    def _below(self, name):
        walk = self._walk
        return _Walk(walk.given, walk.components[:-1] + _components((name,)), Outcome, below=self._locked(),
                     root=walk.root)

    def read(self, name, *, limit):
        """A regular file in the locked directory, read whole as ``StateFiles.read`` reads it."""
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError(f"invalid limit: {limit!r}")
        return _run(self._below(name), lambda walk: walk.read(limit, "whole"))

    def replace(self, name, data):
        """``data`` written to a new temporary file in the locked directory, fsynced and closed, then renamed over ``name``.

        Only under an exclusive lock. ``written``, or the step that failed; a failure leaves ``name`` as it was (a
        temporary file may stay).
        """
        below = self._below(name)
        if self._mode != "exclusive":
            raise RuntimeError("only an exclusive lock replaces a file")
        return _run(below, lambda walk: walk.replace(data))


class _Holder:
    """Descriptors held past one call, closed by ``close()`` or when the ``with`` block ends, each once.

    An interrupt inside a cleanup's ``try`` is caught, ``_shut`` runs again until it completes (_settle), and then the
    interrupt leaves. One landing at the cleanup's entry, before its ``try`` is set up, skips the cleanup: only a scope
    set up before the descriptors were taken closes them then (``holding``, or the owner's own two nested cleanups).
    """

    def close(self):
        """Close what is held; the errno of a failing close or mask call, or None. Safe to call again."""
        try:
            closed = self._shut()
        except BaseException:
            _settle(self._shut)
            raise
        return closed and closed[1]

    def __exit__(self, *exc):
        try:
            self._shut()
        except BaseException:
            _settle(self._shut)
            raise


class Owned(_Holder):
    """A caller's descriptors, handed over by ``HeldFiles.open``; only the primitive adds, inside the mask.

    ``release`` takes one out and closes it, ``close`` the rest, both inside the mask: each descriptor is closed once,
    never again after its number is reused. Both return the errno of a failing close, or None. Handing a descriptor
    to an Owned that was closed is a programming error.
    """

    def __init__(self):
        self._held, self._closed = [], False

    def add(self, fd):
        if self._closed:
            raise RuntimeError("a closed Owned takes no descriptor")
        self._held.append((fd, "owned"))

    def release(self, fd):
        return _release_one(self._held, fd)

    def _shut(self):
        self._closed = True
        return _release(self._held)

    def _failure(self, closed):
        # A handed descriptor keeps no path: the failure names only its holder.
        return StateReadError(Outcome("failed", "", Step("owned", "owned", "close", True), errno=closed[1]))

    def __enter__(self):
        return self


class HeldFiles(_Files, _Holder):
    """Reads below a root opened once (``StateFiles.held``) for one observation, from entering its ``with`` block on.

    ``opened`` is true only while the root, opened by search, is held and a lookup below it worked; when opening it
    did not, every call decides what that opening decided (absent, failed, refused). ``under`` copies share the root,
    and ``recorded`` compares with the path the root resolved to when opened. Reads only: no lock, no replacement and
    no directory made through a held root. Entering registers the root here inside the mask and, when it does not
    finish, closes it and leaves the root unusable; a read before entering or after ``close`` is a programming error.
    """

    def __init__(self, path):
        super().__init__(path)
        self._anchor = _Root()  # the closer, before the root is opened

    def __enter__(self):
        root = self._anchor
        if root.state != "new":
            raise RuntimeError("a held root is entered once")
        root.state = "entering"
        walk = _Walk(self.path, (), Outcome)
        walk.held = root.held
        try:
            try:
                walk.open_root()
            except _Decided as decided:
                root.failure = decided.outcome
                _release(root.held)
            root.text, root.state = walk.root, "entered"
            return self
        except BaseException:
            _settle(self._shut)
            raise

    def _shut(self):
        self._anchor.state = "closed"
        return _release(self._anchor.held)

    def _failure(self, closed):
        text = self._anchor.text
        return StateReadError(Outcome("failed", text, Step("root", text, "close", True), errno=closed[1]))

    @property
    def opened(self):
        return self._anchor.state == "entered" and bool(self._anchor.held)

    def _bound(self):
        if self._anchor.state == "new":
            raise ValueError("the held state root is not open")
        return self._anchor.text

    def open(self, components, kind, *, into):
        """A directory (opened by search) or a regular file below the held root, its descriptor handed to ``into``.

        ``(outcome, fd)``: ``opened`` and the descriptor, which ``into`` held before the signals were unblocked;
        otherwise None. The caller closes it through ``into`` (``release`` or ``close``), never with os.close.
        """
        if kind not in ("directory", "file"):
            raise ValueError(f"unknown kind: {kind!r}")
        if not isinstance(into, Owned):
            raise TypeError("descriptors are handed to an Owned")
        handed = []
        outcome = self._call(components, lambda walk: walk.hand_over(kind, into, handed), named=kind == "file")
        if outcome.state == "opened":
            return outcome, handed[0]
        if handed:  # a later close failed the call: the caller never learns of this descriptor
            into.release(handed[0])
        return outcome, None
