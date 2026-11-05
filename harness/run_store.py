"""Compare-and-swap authority for a run's durable ledger.

The process may stage several related mutations in the live dictionary. Only
commit publishes them, under a stable lock and against the exact loaded bytes.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .util import HarnessError, lock, write_json


class RunStore:
    def __init__(self, state):
        self.root = Path(state) / "runs"
        self.run = None
        self._path = None
        self._digest = None
        self._revision = 0

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

    def attach(self, run):
        """Select newly created state or the exact state read from disk.

        Replacing the selected run deliberately resets the baseline. Existing
        ledgers must match their source, so a caller cannot attach stale state
        and silently adopt a newer disk revision.
        """
        if run is None:
            self.run = self._path = self._digest = None
            self._revision = 0
            return
        ident, revision = self._identity(run)
        path = self.root / ident / "state.json"
        with lock(path.parent / "store.lock"):
            source = path.read_bytes() if path.exists() else None
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

    def check_current(self):
        """Reject stale authority before external effects; caller owns run.lock."""
        if self.run is None or self._path is None:
            raise HarnessError("没有已绑定的运行台账")
        ident, revision = self._identity(self.run)
        if ident != self._path.parent.name or revision != self._revision:
            raise HarnessError("运行台账身份或修订被修改")
        with lock(self._path.parent / "store.lock"):
            source = self._path.read_bytes() if self._path.exists() else None
            digest = hashlib.sha256(source).hexdigest() if source is not None else None
            if digest != self._digest:
                raise HarnessError("运行台账已被其他写入者修改；拒绝外部操作")

    def commit(self):
        if self.run is None or self._path is None:
            raise HarnessError("没有已绑定的运行台账")
        ident, revision = self._identity(self.run)
        if ident != self._path.parent.name or revision != self._revision:
            raise HarnessError("运行台账身份或修订被修改")
        with lock(self._path.parent / "store.lock"):
            source = self._path.read_bytes() if self._path.exists() else None
            digest = hashlib.sha256(source).hexdigest() if source is not None else None
            if digest != self._digest:
                raise HarnessError("运行台账已被其他写入者修改；拒绝覆盖")
            candidate = deepcopy(self.run)
            candidate["store_revision"] = revision + 1
            write_json(self._path, candidate)
            # Read persisted bytes before adopting the next expected source.
            # A failed publication never advances the in-memory revision.
            self._digest = hashlib.sha256(self._path.read_bytes()).hexdigest()
            self._revision = candidate["store_revision"]
            self.run["store_revision"] = self._revision
            return self._revision
