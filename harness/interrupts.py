"""Process-level signal recording and cooperative delivery for a run invocation."""
from __future__ import annotations

from contextlib import contextmanager, suppress
import os
import select
import signal
import sys
import threading
import time


ENABLED = True
_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
_controller = None
_depth = threading.local()
_wait_reporter = None  # Installed only by the offline test suite.


def _notice(message):
    with suppress(BaseException):
        from .util import one_line
        print("[harness] " + one_line(message), file=sys.stderr, flush=True)


class _Controller:
    def __init__(self):
        self.pid = os.getpid()
        self.first = None
        self.count = self.reported = 0
        self.delivered_from = None
        self.terminal = False
        self.store = self.dirty = None
        self.reader = self.writer = None
        self.previous = {}

    def remember(self, signum, frame):
        if self.first is None:
            self.first = (signum, time.monotonic())
        self.count += 1
        if self.writer is not None:
            with suppress(OSError):
                os.write(self.writer, b"\0")

    def collect_pending(self):
        for signum in signal.sigpending().intersection(_SIGNALS):
            self.remember(signal.sigwait({signum}), None)

    def restore(self, *, handoff=False):
        ignored = []
        for signum, handler in self.previous.items():
            if handoff and handler == signal.SIG_IGN:
                ignored.append(signum)
            else:
                signal.signal(signum, handler)
        if handoff:
            # This snapshot is the ownership boundary on the masked thread.
            # Restore SIG_IGN afterward because it discards pending signals.
            try:
                self.collect_pending()
            finally:
                for signum in ignored:
                    signal.signal(signum, signal.SIG_IGN)

    def close(self):
        for fd in (self.reader, self.writer):
            if fd is not None:
                with suppress(OSError):
                    os.close(fd)
        self.reader = self.writer = None

    def report_repeat(self):
        count = self.count
        if count >= 2 and count != self.reported:
            self.reported = count
            _notice(f"已收到第 {count} 次中断，正在收尾；要立即终止请发送 SIGKILL（之后按强杀后的恢复处理）")

    def finish(self):
        if self.first is not None and self.delivered_from is None:
            run = self.store.run if self.store is not None else None
            status = run.get("status", "unknown") if run is not None else "unknown"
            name = signal.Signals(self.first[0]).name
            _notice(f"收到 {name}（共 {self.count} 次）时本次调用已到终态，按台账结束：status={status}")
        else:
            self.report_repeat()


def _main_thread():
    return threading.current_thread() is threading.main_thread()


def _local_controller():
    return _controller if _main_thread() else None


def active():
    """Return the controller that owns signals in the current thread, if any."""
    controller = _local_controller()
    return controller if controller is not None and controller.writer is not None else None


@contextmanager
def installed():
    """Reuse the main thread's outer invocation, including with delivery disabled."""
    global _controller
    if not _main_thread() or _controller is not None:
        yield
        return

    controller = _Controller()
    _controller = controller
    try:
        if ENABLED:
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, _SIGNALS)
            try:
                controller.reader, controller.writer = os.pipe()
                for fd in (controller.reader, controller.writer):
                    os.set_blocking(fd, False)
                for signum in _SIGNALS:
                    controller.previous[signum] = signal.signal(signum, controller.remember)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        yield
    finally:
        # A forked child has already disowned the inherited controller.
        if os.getpid() == controller.pid:
            mask = None
            try:
                if controller.reader is not None:
                    mask = signal.pthread_sigmask(signal.SIG_BLOCK, _SIGNALS)
                    try:
                        controller.restore(handoff=True)
                    finally:
                        controller.close()
                    controller.finish()
            finally:
                _controller = None
                if mask is not None:
                    # Old dispositions own delivery from here, including exceptions.
                    signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def _after_fork():
    global _controller, _depth
    controller, _controller = _controller, None
    _depth = threading.local()
    if controller is not None:
        controller.restore()
        controller.close()


os.register_at_fork(after_in_child=_after_fork)


def bind(store):
    if (controller := _local_controller()) is not None:
        controller.store = store


def terminal(store=None):
    if (controller := _local_controller()) is not None:
        if store is None or controller.store is store:
            controller.terminal = True


def holding():
    return bool(getattr(_depth, "holding", 0))


def _publishing():
    return bool(getattr(_depth, "publishing", 0))


def require_outside_publication(action):
    if _publishing():
        from . import stops
        from .util import HarnessError
        raise HarnessError(f"发布段里不得 {action}", stop=stops.make("runtime_failure", "invariant"))


@contextmanager
def _count_depth(name):
    local = _depth
    before = getattr(local, name, 0)
    setattr(local, name, before + 1)
    try:
        yield
    finally:
        setattr(local, name, before)


def publishing():
    return _count_depth("publishing")


@contextmanager
def hold():
    require_outside_publication("hold")
    if active() or holding():
        with _count_depth("holding"):
            yield
    else:
        from .process_runtime import defer_cleanup_signals
        with defer_cleanup_signals(inherit=False), _count_depth("holding"):
            yield


def deliverable():
    controller = active()
    if (controller is None or controller.first is None or controller.delivered_from is not None
            or holding() or _publishing() or controller.terminal):
        return False
    store = controller.store
    if store is None or store.run is None:
        return True
    baseline = (store, store.adopted_baseline)
    if controller.dirty != baseline and not store.matches_commit():
        controller.dirty = baseline
    return controller.dirty != baseline


def check_wait(action):
    """Report a staged ledger at a wait boundary in tests, even with delivery disabled."""
    reporter = _wait_reporter
    if reporter is None or holding():
        return
    controller = _local_controller()
    if controller is not None and (store := controller.store) is not None and store.run is not None:
        if not store.matches_commit():
            reporter(action)


def deliver():
    require_outside_publication("deliver")
    check_wait("deliver")
    controller = active()
    if controller is None:
        return
    controller.report_repeat()
    if deliverable():
        frame = sys._getframe(1)
        while frame.f_globals is globals():
            frame = frame.f_back
        controller.delivered_from = frame.f_code.co_qualname
        raise KeyboardInterrupt()


def hurry():
    controller = active()
    if controller is None:
        return False
    controller.report_repeat()
    return controller.count >= 2


def sleep(seconds, *, clock=time):
    require_outside_publication("sleep")
    controller = active()
    if controller is None or clock is not time:
        clock.sleep(seconds)
        deliver()
        return

    deadline = time.monotonic() + seconds
    deliver()
    while True:
        readable, _, _ = select.select([controller.reader], [], [], max(0, deadline - time.monotonic()))
        if readable:
            # One bounded read is enough to clear a full pipe on common platforms.
            with suppress(BlockingIOError):
                os.read(controller.reader, 65536)
        deliver()
        if not readable or time.monotonic() >= deadline:
            return


def abortable(effect):
    return bool(active() and effect is not None and effect.interruptible and not holding() and not _publishing())
