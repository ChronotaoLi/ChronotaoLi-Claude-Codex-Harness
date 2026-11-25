"""Compare-and-swap authority for a run's durable ledger.

The process may stage several related mutations in the live dictionary. Only
commit publishes them, under a stable lock and against the exact loaded bytes.
The ledger is read and replaced through the descriptor of the directory whose
store.lock is held; that directory must be the one this process first locked.
"""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import NamedTuple

from . import interrupts, stops
from .operations import first_line, warn, warn_kept_terminal
from .process_runtime import defer_cleanup_signals
from .state_files import LockUnavailable, StateFiles, StateReadError, holding
from .util import HarnessError, dump


LEDGER = "state.json"


def replaced():
    return HarnessError("运行目录在本进程持有期间被替换；保留现场", stop=stops.make("runtime_failure", "invariant"))


def _ledger_bytes(run):
    return (dump(run) + "\n").encode("utf-8")


class _PendingCommit(NamedTuple):
    digest: str
    revision: int
    terminal: bool
    candidate: dict


class RunStore:
    KEPT = object()

    def __init__(self, state):
        self.files = StateFiles(state)
        self.root = Path(state) / "runs"
        self.run = None
        self._path = None
        self._digest = None
        self._baseline = None
        self._revision = 0
        self._pending = None
        self._attempted = None
        self._accepted = None
        self._carried = {}
        self._terminal = None
        self._poisoned = False
        # (st_dev, st_ino) of the run directory this process first locked; every later lock must find it again.
        self._directory = None

    @staticmethod
    def _identity(run):
        if not isinstance(run, dict):
            raise HarnessError("运行台账必须为对象")
        ident = run.get("id")
        if not isinstance(ident, str) or not ident.startswith("run_") or not ident[4:].isalnum():
            raise HarnessError("无效 run ID")
        revision = run.get("store_revision", 0)
        if type(revision) is not int or revision < 0:
            raise HarnessError("运行台账修订无效")
        return ident, revision

    def _locked(self, ident, expected, use, *, new=False, create=True):
        """``use(locked)`` under the run's store.lock, in the directory ``expected`` names when one is known.

        A lock not taken: busy keeps its untyped text; another directory than ``expected``, told on its descriptor
        before store.lock is opened or made there, is the replacement stop; a link or a non-regular file at any level
        is damaged evidence, a failed open or flock local I/O. A missing run directory is None with ``new`` (a run not
        on disk yet), otherwise the invariant stop: it is never made again.
        """
        path = self.root / ident / "store.lock"
        try:
            return holding(self.files.lock(("runs", ident, "store.lock"), create=create, identity=expected), use)
        except LockUnavailable as error:
            outcome = error.outcome
            if outcome.state == "busy":
                raise HarnessError(f"已有进程持有运行锁：{path}") from None
            if outcome.state == "replaced":
                raise replaced() from None
            if outcome.state == "absent":
                if new:
                    return None
                raise HarnessError(f"运行目录不存在：{path.parent}；不重建",
                                   stop=stops.make("runtime_failure", "invariant")) from None
            outcome.evidence({"artifact": str(path)})
            raise

    @staticmethod
    def _source(locked, path):
        """The ledger's bytes in the locked directory, or None when it is not there."""
        outcome = locked.read(LEDGER, limit=None)
        return None if outcome.state == "absent" else outcome.evidence({"artifact": str(path)})

    def _disk_digest(self, locked):
        source = self._source(locked, self._path)
        return hashlib.sha256(source).hexdigest() if source is not None else None

    @stops.classifies("runtime_failure", "invariant")
    def attach(self, run):
        """Select newly created state or the exact state read from disk.

        Replacing the selected run deliberately resets the baseline. Existing
        ledgers must match their source, so a caller cannot attach stale state
        and silently adopt a newer disk revision. A new run not on disk yet
        (never committed, no directory bound) is selected without a lock: its
        first commit makes its directory. Any other run's directory must be
        there: it is never made again.
        """
        if run is None:
            self.run = self._path = self._digest = self._baseline = self._directory = None
            self._revision = 0
            self._pending = self._attempted = self._accepted = self._terminal = None
            self._carried.clear()
            self._poisoned = False
            return
        ident, revision = self._identity(run)
        path = self.root / ident / LEDGER
        # Keep a directory bound before the first attach, or already selected for this run.
        known = self._directory if self._path in (None, path) else None
        taken = self._locked(ident, known, lambda locked: (locked.identity, self._source(locked, path)),
                             new=revision == 0 and known is None)
        directory, source = taken if taken is not None else (known, None)
        if source is not None:
            try:
                saved = json.loads(source)
            except (ValueError, UnicodeError) as error:
                raise HarnessError("运行台账来源损坏") from error
            if saved != run:
                raise HarnessError("运行台账与磁盘来源不一致；拒绝绑定旧状态")
        elif revision:
            raise HarnessError("已修订运行台账缺少磁盘来源")
        baseline = None
        if source is not None:
            try:
                baseline = hashlib.sha256(_ledger_bytes(run)).hexdigest()
            except HarnessError:
                pass
        self.run = run
        self._path = path
        self._revision = revision
        self._digest = hashlib.sha256(source).hexdigest() if source is not None else None
        self._baseline = baseline
        self._directory = directory
        self._pending = self._terminal = None
        self._accepted = self._attempted = deepcopy(run)
        self._carried.clear()
        self._poisoned = False

    @property
    def directory_identity(self):
        """The run directory's (st_dev, st_ino) this process first locked or bound, or None."""
        return self._directory

    def bind_directory(self, identity):
        """Record the run directory a run.lock holder locked, or require it to be the one already recorded."""
        if self._directory is None:
            self._directory = identity
        elif identity != self._directory:
            raise replaced()

    def _record(self, locked):
        if self._directory is None:
            self._directory = locked.identity

    @property
    def terminal_committed(self):
        """Whether a terminal commit was adopted since this run was attached."""
        return self._terminal is not None

    def restore_terminal(self):
        """Restore an adopted terminal snapshot while preserving the live dictionary's identity."""
        saved = deepcopy(self._terminal)
        self.run.clear()
        self.run.update(saved)

    def carry(self, key, value):
        """Preserve an explicit fact until it enters a ledger candidate."""
        self._carried[key] = deepcopy(value)

    def discard_attempt(self):
        """Exclude a caller's rolled-back changes from the base of a later stop."""
        self._attempted = self._accepted

    @property
    def poisoned(self):
        """Whether a failed publication body left an unusable live ledger."""
        return self._poisoned

    def poison(self):
        """Refuse to commit any partially applied publication body."""
        self._poisoned = True

    def rebase_for_stop(self):
        """Restore the last complete candidate and explicitly carried facts in place."""
        base = deepcopy(self._attempted)
        base["store_revision"] = self._revision
        base.update(deepcopy(self._carried))
        self.run.clear()
        self.run.update(base)
        self._poisoned = False

    def _adopt(self, pending):
        self._accepted = pending.candidate
        previous = self._revision
        self._digest = self._baseline = pending.digest
        self._revision = pending.revision
        live_revision = self.run.get("store_revision", 0)
        if type(live_revision) is int and live_revision == previous:
            self.run["store_revision"] = self._revision
        if pending.terminal:
            self._terminal = pending.candidate
            interrupts.terminal(self)
        self._pending = None

    def _reconcile(self, digest):
        """Settle an ambiguous write against a digest read under the store lock."""
        if self._pending is not None and digest == self._pending.digest:
            self._adopt(self._pending)
        elif digest == self._digest:
            self._pending = None
        else:
            return False
        return True

    def resolve(self):
        """Try to reconcile with disk without writing a ledger or reporting a read failure."""
        if self._path is None:
            return

        def check(locked):
            self._reconcile(self._disk_digest(locked))

        try:
            self._locked(self._path.parent.name, self._directory, check, create=False)
        except (HarnessError, OSError):
            pass

    def keep_terminal(self, error):
        """Recover a terminal result at the error boundary, if established."""
        with defer_cleanup_signals(inherit=False) as signals:
            self.resolve()
            if not self.terminal_committed:
                return False
            self.restore_terminal()
            warn_kept_terminal(error)
            signals.handled = True
            return True

    def _require_identity(self):
        ident, revision = self._identity(self.run)
        if ident != self._path.parent.name or revision != self._revision:
            raise HarnessError("运行台账身份或修订被修改")

    @stops.classifies("runtime_failure", "invariant")
    def check_current(self):
        """Reject stale authority before external effects; caller owns run.lock."""
        if self.run is None or self._path is None:
            raise HarnessError("没有已绑定的运行台账")

        def check(locked):
            self._record(locked)
            if not self._reconcile(self._disk_digest(locked)):
                raise HarnessError("运行台账已被其他写入者修改；拒绝外部操作")
            self._require_identity()
        self._locked(self._path.parent.name, self._directory, check)

    @property
    def adopted_baseline(self):
        """Identity of the selected ledger and its last adopted revision."""
        return self._path, self._revision

    def matches_commit(self):
        """Whether the live ledger is exactly what this store last committed (no staged change).

        Read-only: it compares canonical content, including for a source with different formatting.
        """
        if self.run is None or self._baseline is None:
            return False
        try:
            return hashlib.sha256(_ledger_bytes(self.run)).hexdigest() == self._baseline
        except HarnessError:
            return False

    @stops.classifies("runtime_failure", "invariant")
    def commit(self, *, terminal=False, stop=False):
        """Return the written revision or KEPT; refused and unconfirmed writes raise."""
        interrupts.require_outside_publication("commit")
        if self.run is None or self._path is None:
            raise HarnessError("没有已绑定的运行台账")
        if self._poisoned:
            raise HarnessError("发布段中途失败；拒绝提交半步")
        ident = self._path.parent.name
        if self._directory is None:
            # Only a run never locked in this process makes its directory (runs/ too); a known one is never remade.
            made = self.files.make_directories(("runs", ident))
            if made.state != "present":
                made.evidence({"artifact": str(self._path.parent)})

        # This call starts unconfirmed. Only an accepted keep or this call's
        # adopted write can turn a later signal or lock close into success.
        outcome = None

        def publish(locked):
            nonlocal outcome
            self._record(locked)
            digest = self._disk_digest(locked)
            # If a stop adopts an earlier terminal write, finish restoring it and
            # record KEPT before a signal can reach the caller.
            with defer_cleanup_signals(inherit=False) if stop else nullcontext():
                if not self._reconcile(digest):
                    raise HarnessError("运行台账已被其他写入者修改；拒绝覆盖")
                self._require_identity()
                if stop and self.terminal_committed:
                    self.restore_terminal()
                    outcome = self.KEPT
                    return outcome
            candidate = deepcopy(self.run)
            revision = candidate["store_revision"] = self._revision + 1
            data = _ledger_bytes(candidate)
            pending = _PendingCommit(hashlib.sha256(data).hexdigest(), revision, terminal or stop, candidate)
            self._attempted = candidate
            self._pending = pending
            self._carried.clear()
            with defer_cleanup_signals(inherit=False):
                try:
                    self._publish_candidate(locked, candidate, data)
                except BaseException as error:
                    try:
                        self._reconcile(self._disk_digest(locked))
                    except (HarnessError, OSError):
                        pass  # Keep the pending candidate and the original write error.
                    if self._revision != revision:
                        raise
                    outcome = revision
                    if not isinstance(error, Exception):
                        raise
                    warn(f"台账写入报告了错误但已落盘，按已写入处理：{first_line(error)}")
                else:
                    self._adopt(pending)
                    outcome = revision
            return outcome

        try:
            return self._locked(ident, self._directory, publish)
        except (StateReadError, KeyboardInterrupt, SystemExit) as error:
            # A deferred signal or lock close failure can follow the decision
            # to keep a prior terminal or the adoption of this call's write.
            if outcome is self.KEPT:
                return outcome
            if outcome is not None and (terminal or stop):
                warn_kept_terminal(error)
                return outcome
            raise

    def _publish_candidate(self, locked, candidate, data):
        """Write the serialized candidate in the locked directory, before adopting it.

        The one step of ``commit`` that publishes, so its observers (the test tripwire) stay put when the write changes.
        """
        written = locked.replace(LEDGER, data)
        if written.state != "written":
            written.or_local_io()
            raise HarnessError(f"运行台账写入被拒绝：{self._path}")
