"""Compare-and-swap authority for a run's durable ledger.

The process may stage several related mutations in the live dictionary. Only
commit publishes them, under a stable lock and against the exact loaded bytes.
The ledger is read and replaced through the descriptor of the directory whose
store.lock is held; that directory must be the one this process first locked.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from . import stops
from .state_files import LockUnavailable, StateFiles, holding
from .util import HarnessError, dump


LEDGER = "state.json"


def replaced():
    return HarnessError("运行目录在本进程持有期间被替换；保留现场", stop=stops.make("runtime_failure", "invariant"))


class RunStore:
    def __init__(self, state):
        self.files = StateFiles(state)
        self.root = Path(state) / "runs"
        self.run = None
        self._path = None
        self._digest = None
        self._revision = 0
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

    def _locked(self, ident, expected, use, *, new=False):
        """``use(locked)`` under the run's store.lock, in the directory ``expected`` names when one is known.

        A lock not taken: busy keeps its untyped text; another directory than ``expected``, told on its descriptor
        before store.lock is opened or made there, is the replacement stop; a link or a non-regular file at any level
        is damaged evidence, a failed open or flock local I/O. A missing run directory is None with ``new`` (a run not
        on disk yet), otherwise the invariant stop: it is never made again.
        """
        path = self.root / ident / "store.lock"
        try:
            return holding(self.files.lock(("runs", ident, "store.lock"), create=True, identity=expected), use)
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
            self.run = self._path = self._digest = self._directory = None
            self._revision = 0
            return
        ident, revision = self._identity(run)
        path = self.root / ident / LEDGER
        # The same run again keeps the directory it was locked in; another run's is recorded afresh.
        known = self._directory if path == self._path else None
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
        self.run = run
        self._path = path
        self._revision = revision
        self._digest = hashlib.sha256(source).hexdigest() if source is not None else None
        self._directory = directory

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

    @stops.classifies("runtime_failure", "invariant")
    def check_current(self):
        """Reject stale authority before external effects; caller owns run.lock."""
        if self.run is None or self._path is None:
            raise HarnessError("没有已绑定的运行台账")
        ident, revision = self._identity(self.run)
        if ident != self._path.parent.name or revision != self._revision:
            raise HarnessError("运行台账身份或修订被修改")

        def check(locked):
            self._record(locked)
            source = self._source(locked, self._path)
            digest = hashlib.sha256(source).hexdigest() if source is not None else None
            if digest != self._digest:
                raise HarnessError("运行台账已被其他写入者修改；拒绝外部操作")
        self._locked(ident, self._directory, check)

    def matches_commit(self):
        """Whether the live ledger is exactly what this store last committed (no staged change).

        Read-only: it serializes the live dictionary as ``commit`` writes it and compares
        the digest with the last committed source; it neither reads the disk nor locks.
        """
        if self.run is None or self._digest is None:
            return False
        try:
            text = dump(self.run) + "\n"
        except HarnessError:
            return False
        return hashlib.sha256(text.encode("utf-8")).hexdigest() == self._digest

    @stops.classifies("runtime_failure", "invariant")
    def commit(self):
        if self.run is None or self._path is None:
            raise HarnessError("没有已绑定的运行台账")
        ident, revision = self._identity(self.run)
        if ident != self._path.parent.name or revision != self._revision:
            raise HarnessError("运行台账身份或修订被修改")
        if self._directory is None:
            # Only a run never locked in this process makes its directory (runs/ too); a known one is never remade.
            made = self.files.make_directories(("runs", ident))
            if made.state != "present":
                made.evidence({"artifact": str(self._path.parent)})

        def publish(locked):
            self._record(locked)
            source = self._source(locked, self._path)
            digest = hashlib.sha256(source).hexdigest() if source is not None else None
            if digest != self._digest:
                raise HarnessError("运行台账已被其他写入者修改；拒绝覆盖")
            candidate = deepcopy(self.run)
            candidate["store_revision"] = revision + 1
            self._publish_candidate(locked, candidate)
            # Read persisted bytes before adopting the next expected source.
            # A failed publication never advances the in-memory revision.
            self._digest = hashlib.sha256(locked.read(LEDGER, limit=None).evidence({"artifact": str(self._path)})).hexdigest()
            self._revision = candidate["store_revision"]
            self.run["store_revision"] = self._revision
            return self._revision
        return self._locked(ident, self._directory, publish)

    def _publish_candidate(self, locked, candidate):
        """Write ``candidate`` as the ledger in the locked directory; returns once it is written, before any read-back.

        The one step of ``commit`` that publishes, so its observers (the test tripwire) stay put when the write changes.
        """
        written = locked.replace(LEDGER, (dump(candidate) + "\n").encode("utf-8"))
        if written.state != "written":
            written.or_local_io()
            raise HarnessError(f"运行台账写入被拒绝：{self._path}")
