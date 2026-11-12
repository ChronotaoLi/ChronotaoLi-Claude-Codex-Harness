"""Facts and bounded cleanup for a newly owned POSIX process group.

A leader exit is never evidence that its descendants exited. Groups which
outlive a reaped leader are ambiguous and require explicit recovery.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
import contextlib
import threading
import signal
import subprocess
import time


class DeferredSignals(list):
    handled = False


@contextlib.contextmanager
def defer_cleanup_signals(*, primary=None):
    """Defer cancellation through cleanup, including the handler handoff.

    Each scope retains the first signal it receives. An existing exception always
    wins; otherwise a deferred request must prevent a normal return.
    """
    import sys
    captured = DeferredSignals()
    def remember(signum, frame):
        if not captured:
            captured.append(signum)
    old = {}
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    main = threading.current_thread() is threading.main_thread()
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals) if main else None
    try:
        if main:
            for sig in signals:
                old[sig] = signal.signal(sig, remember)
    finally:
        if main:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    try:
        yield captured
    finally:
        pending = sys.exc_info()[1]
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals) if main else None
        try:
            for sig, handler in old.items():
                signal.signal(sig, handler)
            # A request arriving while handlers were being restored belongs to
            # this cleanup too. Consume it before exposing default TERM/HUP.
            if main:
                waiting = signal.sigpending().intersection(signals)
                for sig in waiting:
                    remember(signal.sigwait({sig}), None)
        finally:
            if main:
                try:
                    signal.pthread_sigmask(signal.SIG_SETMASK, mask)
                except KeyboardInterrupt:
                    if primary is None and pending is None:
                        raise
        if captured and not getattr(captured, "handled", False) and primary is None and pending is None:
            raise KeyboardInterrupt(f"Process cleanup signal {captured[0]}")


@dataclass
class ProcessOutcome:
    started: bool = False
    startup_state: str = "starting"
    pid: int | None = None
    pgid: int | None = None
    exit_code: int | None = None
    termination_reason: str = "normal"
    exit_confirmed: bool = False
    cleanup_errors: list = field(default_factory=list)
    cleanup_actions: list = field(default_factory=list)
    evidence_complete: bool = True
    errors: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def failure(step, error):
    return {"step": step, "type": type(error).__name__, "message": str(error),
            "errno": getattr(error, "errno", None)}


class ProcessSupervisor:
    def __init__(self, log, *, on_outcome=None, cleanup_deadline=None):
        from .util import write_json
        self.log = log
        self.path = log.with_suffix(".outcome.json")
        self.on_outcome = on_outcome
        self.cleanup_deadline = cleanup_deadline
        self.fatal = None
        self.process = None
        self.launch_pending = False
        self.outcome = ProcessOutcome()
        # Before Popen: inability to save intent must prevent startup.
        write_json(self.path, self.outcome.to_dict())

    def begin_launch(self):
        # If Popen itself raises after creating a child, its identity may never
        # reach us. Keep that attempt unknown rather than asserting no startup.
        self.launch_pending = True

    def started(self, process):
        from .util import write_json
        self.process = process  # Ownership is assigned before persistence/callbacks.
        self.launch_pending = False
        self.outcome.started = True
        self.outcome.startup_state = "started"
        self.outcome.pid = self.outcome.pgid = process.pid
        write_json(self.path, self.outcome.to_dict())

    def _attempt(self, step, action):
        try:
            self.outcome.cleanup_actions.append(step)
            return action()
        except ProcessLookupError:
            return None
        except BaseException as error:
            if not isinstance(error, (Exception, KeyboardInterrupt, SystemExit)):
                self.fatal = self.fatal or error
            self.outcome.cleanup_errors.append(failure(step, error))
            return None

    def _observe(self, *, final=True):
        try:
            os.killpg(self.process.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError as error:
            # XNU answers EPERM while every remaining member is a zombie awaiting
            # its new parent's reap. That is presence, never exit; keep observing
            # and treat only a denial at the final observation as a fault.
            if not final:
                return True
            self.outcome.cleanup_errors.append(failure("observe_group", error))
            return None
        except BaseException as error:
            if not isinstance(error, (Exception, KeyboardInterrupt, SystemExit)):
                self.fatal = self.fatal or error
            self.outcome.cleanup_errors.append(failure("observe_group", error))
            return None

    def finish(self, primary=None):
        try:
            self._finish_deferred(primary)
        except KeyboardInterrupt as error:
            if primary is not None:
                return
            if getattr(error, "outcome", None) is None:
                self.record_cancellation(error)
            raise

    def record_cancellation(self, error):
        """Persist a late cancellation without repeating process cleanup."""
        from .util import write_json
        out = self.outcome
        out.termination_reason = "cancelled"
        out.errors.append(failure("execution", error))
        error.outcome = out
        with defer_cleanup_signals(primary=error):
            try:
                write_json(self.path, out.to_dict())
                if self.on_outcome:
                    self.on_outcome(out)
            except BaseException as persistence_error:
                out.evidence_complete = False
                out.errors.append(failure("persist_cancellation", persistence_error))

    def _finish_deferred(self, primary=None):
        from .util import HarnessError
        with defer_cleanup_signals(primary=primary) as deferred:
            try:
                self._finish(primary)
            except HarnessError:
                if deferred and primary is None and not self.outcome.cleanup_errors and not self.outcome.errors:
                    error = KeyboardInterrupt(f"Process cleanup signal {deferred[0]}")
                    self.record_cancellation(error)
                    raise error
                raise

    def _finish(self, primary=None):
        from .util import HarnessError, ProcessStartError, ProcessTimeout, write_json
        out = self.outcome
        if primary is not None:
            out.termination_reason = ("timeout" if isinstance(primary, ProcessTimeout) else
                                      "cancelled" if isinstance(primary, KeyboardInterrupt) else
                                      "interrupted" if isinstance(primary, SystemExit) else
                                      "start_failed" if isinstance(primary, ProcessStartError) else "error")
            out.errors.append(failure("execution", primary))
        if not out.started:
            if self.launch_pending and not isinstance(primary, ProcessStartError):
                out.startup_state = "starting"
                out.exit_confirmed = False
            else:
                out.startup_state = "not_started"
                out.exit_confirmed = True
        p = self.process
        if p is not None:
            # poll reaps a finished leader; never signal a live numeric group
            # after that observation, since continuity of its identity is unknown.
            code = self._attempt("poll_leader", p.poll)
            if code is not None:
                out.exit_code = code
            group_present = self._observe(final=False)
            if code is not None and primary is None and group_present is True:
                # A normal CLI may close its MCP pipes just before its children
                # exit. Observe their drain, but never signal after reaping the
                # leader: ownership of the numeric group is no longer assured.
                drain_end = min(time.monotonic() + 2, self.cleanup_deadline
                                if self.cleanup_deadline is not None else float("inf"))
                def drain():
                    while self._observe(final=False) is True and time.monotonic() < drain_end:
                        time.sleep(min(.01, max(0, drain_end - time.monotonic())))
                self._attempt("drain_descendants", drain)
            if code is None:
                cleanup_end = min(time.monotonic() + 2, self.cleanup_deadline if self.cleanup_deadline is not None else float("inf"))
                self._attempt("sigterm", lambda: os.killpg(p.pid, signal.SIGTERM))
                deadline = min(time.monotonic() + 1, cleanup_end)
                while time.monotonic() < deadline and self._attempt("poll_cleanup", p.poll) is None:
                    self._attempt("cleanup_pause", lambda: time.sleep(.01))
                # Keep the unreaped leader as ownership anchor until signalling
                # is complete. If it exited, a remaining group is ambiguous.
                if p.returncode is None:
                    self._attempt("sigkill", lambda: os.killpg(p.pid, signal.SIGKILL))
            if p.returncode is None:
                self._attempt("wait_leader", lambda: p.wait(timeout=max(0, cleanup_end - time.monotonic())))
            if p.returncode is not None:
                out.exit_code = p.returncode
            out.exit_confirmed = self._observe() is False and out.exit_code is not None
        if (out.cleanup_errors or not out.exit_confirmed
                or (primary is not None and not isinstance(
                    primary, (ProcessStartError, ProcessTimeout, KeyboardInterrupt, SystemExit)))):
            out.evidence_complete = False
        secondary = None
        try:
            write_json(self.path, out.to_dict())
        except (Exception, KeyboardInterrupt, SystemExit) as error:
            out.evidence_complete = False
            out.errors.append(failure("persist_outcome", error))
            secondary = error
        if self.on_outcome:
            try:
                self.on_outcome(out)
            except (Exception, KeyboardInterrupt, SystemExit) as error:
                out.evidence_complete = False
                out.errors.append(failure("on_outcome", error))
                secondary = secondary or error
                try:
                    write_json(self.path, out.to_dict())
                except (Exception, KeyboardInterrupt, SystemExit) as persistence_error:
                    out.errors.append(failure("persist_callback_failure", persistence_error))
        if primary is not None:
            primary.outcome = out
        if self.fatal is not None:
            self.fatal.outcome = out
            raise self.fatal
        if primary is not None:
            return
        if secondary is not None:
            secondary.outcome = out
            raise secondary
        if not out.evidence_complete:
            error = HarnessError("Process evidence incomplete; inspect process outcome: " + str(self.path))
            error.outcome = out
            raise error
