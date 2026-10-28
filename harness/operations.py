"""Automatic operation records for ``run`` and ``resume``.

One CLI invocation owns one record under ``<repo>/.harness/operations/``. The
record is observation only and never a second execution truth: it does not
take ``run.lock``, never writes the run ledger, and no provenance probe or
record update may change the lifecycle, the save order or the exit status.
Only the initial exclusive creation is a gate, so that one operation ID is
owned by exactly one process.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import git
from .shared import asset_digest
from .util import HarnessError, dump, uid, write_json

OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
RUN_ID = re.compile(r"run_[A-Za-z0-9]+")
CLI_VERSION_TIMEOUT = 5
GIT_QUERY_TIMEOUT = 15
PACKAGE = Path(__file__).resolve().parent
PROVIDER_CLIS = ("claude", "codex")
ROLE_NAMES = ("commander", "fable", "worker", "reviewer")
ERROR_OMITTED = "file inputs present; see stderr"


def warn(message):
    """Diagnostic output that never raises: observation must not alter lifecycle or exit status."""
    try:
        if sys.stderr is not None:
            print(f"[harness] 警告：{message}", file=sys.stderr, flush=True)
    except Exception:
        pass


def utc_text(seconds):
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def first_line(error):
    lines = str(error).splitlines()
    return lines[0] if lines else type(error).__name__


def _git_query(root, *args):
    """Bounded read-only Git query about the installation; None on any failure.

    Provenance must never raise or block an operation, so this does not use
    git.git (HarnessError on failure, 60 second wait). It reuses the same argv
    construction and GIT_* environment hygiene.
    """
    try:
        process = subprocess.Popen(git._git_command(root, args), env=git._git_environment(),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, ValueError):
        return None
    try:
        output, _ = process.communicate(timeout=GIT_QUERY_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return None
    return os.fsdecode(output) if process.returncode == 0 else None


def harness_identity():
    """Describe the loaded runtime: package path, checkout revision and local modifications."""
    root = PACKAGE.parent
    identity = {"package": str(PACKAGE), "revision": None, "runtime_modified": None,
                "python": sys.version.split()[0], "executable": sys.executable}
    output = _git_query(root, "rev-parse", "--show-toplevel", "HEAD")
    lines = output.splitlines() if output else []
    # Only a checkout whose root is the package's parent describes this runtime.
    if len(lines) == 2 and Path(lines[0]).resolve() == root:
        identity["revision"] = lines[1].strip()
        status = _git_query(root, "status", "--porcelain", "--", PACKAGE.name)
        identity["runtime_modified"] = None if status is None else bool(status.strip())
    return identity


def config_identity(config):
    identity = {"path": str(config.path), "sha256": None}
    try:
        identity["sha256"] = hashlib.sha256(Path(config.path).read_bytes()).hexdigest()
    except (OSError, TypeError, ValueError):
        pass
    return identity


def shared_paths(config):
    return {key: str(config.asset(key)) for key in ("rules", "skills", "mcp", "hooks")}


def digest_assets(config):
    """Return (digest, None) or (None, "Type: first line"); a provenance probe never raises."""
    try:
        return asset_digest(config), None
    except Exception as error:
        return None, f"{type(error).__name__}: {first_line(error)}"


def role_models(config):
    data = getattr(config, "data", None)
    roles = data.get("roles") if isinstance(data, dict) else None
    roles = roles if isinstance(roles, dict) else {}
    result = {}
    for name in ROLE_NAMES:
        role = roles.get(name)
        result[name] = ({key: role.get(key) for key in ("cli", "model", "effort")}
                        if isinstance(role, dict) else None)
    return result


def probe_cli(name):
    """Locate a provider CLI and read its --version with a bounded wait; never raises."""
    path = shutil.which(name)
    version = None
    if path:
        try:
            process = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                     text=True, errors="replace", timeout=CLI_VERSION_TIMEOUT)
            if process.returncode == 0:
                version = next((line.strip() for line in process.stdout.splitlines() if line.strip()), None)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return {"path": path, "version": version}


def _repo_head(repo):
    try:
        return git.head(repo)
    except HarnessError:
        return None


class OperationRecord:
    """One run/resume invocation's record file: the CLI owns it, the Engine only links a run."""

    def __init__(self, path, record, *, file_inputs=False):
        self.path = path
        self.record = record
        # Error text from a failed validation can quote values from --plan,
        # --environment-update or --operator-decision files; omit it then.
        self.file_inputs = file_inputs

    @property
    def id(self):
        return self.record["operation_id"]

    @property
    def run_id(self):
        return self.record["run_id"]

    @classmethod
    def create(cls, state, *, kind, arguments, config, repo, operation_id=None, file_inputs=False):
        """Gather provenance read-only, then create the record exclusively.

        Creation precedes git.ignore_state and every other write of the
        invocation; an existing file means another process owns this ID.
        """
        if operation_id is None:
            operation_id = uid("op")
        elif not OPERATION_ID.fullmatch(operation_id):
            raise HarnessError("无效 --operation-id：以字母或数字开头，只含字母、数字、_ . -，最长 64 字符")
        started = time.time()
        record = {"operation_id": operation_id, "kind": kind, "status": "prepared",
                  "started_at": started, "started_at_utc": utc_text(started),
                  "arguments": arguments, "repo": str(repo), "repo_head": _repo_head(repo),
                  "harness": harness_identity(), "config": config_identity(config),
                  "asset_digest": digest_assets(config)[0], "models": role_models(config),
                  "cli": {name: probe_cli(name) for name in PROVIDER_CLIS},
                  "run_id": None, "run_linked_at": None,
                  "finished_at": None, "finished_at_utc": None,
                  "exit_status": None, "result_status": None, "error": None, "error_type": None,
                  "error_omitted": None}
        directory = Path(state) / "operations"
        path = directory / f"{operation_id}.json"
        directory.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError as error:
            raise HarnessError(f"操作记录已存在：{path}；每个 --operation-id 只能由一个进程使用") from error
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(dump(record) + "\n")
            output.flush()
            os.fsync(output.fileno())
        return cls(path, record, file_inputs=file_inputs)

    def link(self, run_id):
        """Bind the persisted run identity right after the ledger's first save."""
        if self.record["run_id"] is not None:
            if self.record["run_id"] != run_id:
                raise HarnessError(f"操作记录已关联 {self.record['run_id']}，不能改绑 {run_id}")
            return
        self.record.update(run_id=run_id, run_linked_at=time.time(), status="running")
        write_json(self.path, self.record)

    def finish(self, *, exit_status, result_status=None, error=None, run_id=None):
        """Best-effort final update: never raises, never changes the caller's exit status."""
        try:
            if self.record["run_id"] is None and run_id is not None:
                self.record.update(run_id=run_id, run_linked_at=time.time())
            finished = time.time()
            omitted = error is not None and self.file_inputs
            self.record.update(status="finished", finished_at=finished, finished_at_utc=utc_text(finished),
                               exit_status=exit_status, result_status=result_status,
                               error=first_line(error) if error is not None and not omitted else None,
                               error_type=type(error).__name__ if error is not None else None,
                               error_omitted=ERROR_OMITTED if omitted else None)
            write_json(self.path, self.record)
        except Exception as failure:
            warn(f"操作记录更新失败，不影响退出码：{self.path}：{failure}")
