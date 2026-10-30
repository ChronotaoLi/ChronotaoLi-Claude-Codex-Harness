"""Native, read-only run notifications. Subscribe before taking the initial snapshot.

Notifications are invalidations, not a history: callers reread their projection after
True and may emit a heartbeat after False without reading state. No state polling,
process inspection, or writes occur here. One watcher belongs to one calling thread.
"""
from __future__ import annotations

import ctypes
import errno
import math
import os
import select
import struct
import sys
import time

from .observe import NotFound, ObserveError, validate_run_id


class WatchError(ObserveError):
    """Native filesystem notifications are unavailable or failed."""


class _Inotify:
    label = "inotify"

    def __init__(self):
        self.lib = ctypes.CDLL(None, use_errno=True)
        self.lib.inotify_init1.argtypes = [ctypes.c_int]
        self.lib.inotify_init1.restype = ctypes.c_int
        self.lib.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.lib.inotify_add_watch.restype = ctypes.c_int
        self.lib.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self.fd = self.lib.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
        if self.fd < 0:
            raise WatchError(f"inotify_init1: {os.strerror(ctypes.get_errno())}")

    def add(self, fd):
        # /proc resolves only our already verified, held descriptor. No user path
        # is followed; inotify has no descriptor-based registration syscall.
        mask = 0x2 | 0x4 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800
        wd = self.lib.inotify_add_watch(self.fd, f"/proc/self/fd/{fd}".encode(), mask)
        if wd < 0:
            raise WatchError(f"inotify_add_watch: {os.strerror(ctypes.get_errno())}")
        return wd

    def remove(self, token):
        result = self.lib.inotify_rm_watch(self.fd, token)
        if result < 0 and ctypes.get_errno() != errno.EINVAL:
            raise WatchError(f"inotify_rm_watch: {os.strerror(ctypes.get_errno())}")

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            ready, _, _ = select.select([self.fd], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                return False
            data = os.read(self.fd, 65536)  # bounded; queued excess wakes next call
            offset, changed = 0, False
            while offset + 16 <= len(data):
                _, mask, _, length = struct.unpack_from("iIII", data, offset)
                offset += 16 + length
                changed |= bool(mask & ~0x8000)  # rm_watch's IN_IGNORED is bookkeeping
            if changed:
                return True  # includes IN_Q_OVERFLOW: force a full projection refresh
            if time.monotonic() >= deadline:
                return False

    def close(self):
        os.close(self.fd)


class _Kqueue:
    label = "kqueue"

    def __init__(self):
        self.queue = select.kqueue()

    def add(self, fd):
        flags = (select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME
                 | select.KQ_NOTE_ATTRIB | select.KQ_NOTE_EXTEND | select.KQ_NOTE_REVOKE)
        self.queue.control([select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                                         flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                         fflags=flags)], 0, 0)
        return fd

    def remove(self, token):
        try:
            self.queue.control([select.kevent(token, filter=select.KQ_FILTER_VNODE,
                                             flags=select.KQ_EV_DELETE)], 0, 0)
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise

    def wait(self, timeout):
        return bool(self.queue.control(None, 32, timeout))

    def close(self):
        self.queue.close()


class RunWatcher:
    """Bounded subscriptions anchored to StateReader's fixed root descriptor.

    Root-to-leaf registration happens before the initial caller snapshot. Parent
    subscriptions remain installed while changed children are rebound, covering
    atomic file replacement, run removal/recreation, and runs directory replacement.
    Missing files are allowed; a missing initial run or any symlink is refused.
    """

    def __init__(self, reader, run_id):
        validate_run_id(run_id)
        self.reader, self.run_id = reader, run_id
        self._entries = {}
        self._native = None
        self._closed = False
        try:
            if sys.platform == "darwin" and hasattr(select, "kqueue"):
                self._native = _Kqueue()
            elif sys.platform.startswith("linux"):
                self._native = _Inotify()
            else:
                raise WatchError(f"Native filesystem watching unsupported on {sys.platform}")
            self.backend = self._native.label
            self._refresh(initial=True)
        except BaseException:
            self.close()
            raise

    def _refresh(self, *, initial=False):
        paths = [(), ("runs",), ("runs", self.run_id),
                 ("runs", self.run_id, "state.json"), ("runs", self.run_id, "events.jsonl")]
        for path in paths:
            try:
                if len(path) < 3:
                    fd = self.reader.open_directory(list(path))
                else:
                    fd, _ = self.reader.open_file(list(path))
            except NotFound:
                if initial and len(path) == 2:
                    raise
                self._drop(path)
                continue
            info = os.fstat(fd)
            identity = (info.st_dev, info.st_ino)
            old = self._entries.get(path)
            if old and old[0] == identity:
                os.close(fd)
                continue
            try:
                token = self._native.add(fd)
            except BaseException:
                os.close(fd)
                raise
            self._drop(path)
            self._entries[path] = (identity, fd, token)

    def _drop(self, path):
        old = self._entries.pop(path, None)
        if old:
            try:
                # inotify shares a watch token when two names are hard links.
                if not any(entry[2] == old[2] for entry in self._entries.values()):
                    self._native.remove(old[2])
            finally:
                os.close(old[1])

    def wait(self, timeout_seconds):
        if self._closed:
            raise WatchError("Watcher is closed")
        if not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError("timeout_seconds must be finite and nonnegative")
        try:
            changed = self._native.wait(timeout_seconds)
            if changed:
                self._refresh()
            return changed
        except OSError as error:
            raise WatchError(f"Native filesystem watch failed: {error}") from error

    def close(self):
        if self._closed:
            return
        self._closed = True
        for _, fd, _ in self._entries.values():
            os.close(fd)
        self._entries.clear()
        if self._native is not None:
            self._native.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
