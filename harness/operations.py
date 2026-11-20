"""Automatic operation records for ``run`` and ``resume``.

One CLI invocation owns one record under ``<repo>/.harness/operations/``. The
record is observation only and never a second execution truth: it does not
take ``run.lock``, never writes the run ledger, and no provenance probe or
record update may change the lifecycle, the save order or the exit status.
Only the initial exclusive creation is a gate, so that one operation ID is
owned by exactly one process.
"""
from __future__ import annotations

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
from .util import _cli_arguments, HarnessError, dump, uid, write_json

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


def _git_query(root, *args, effect):
    """Bounded Git query about the installation; None on any failure.

    Provenance must never raise or block an operation, so this does not use
    git.git (HarnessError on failure, 60 second wait). It takes the argv and
    environment from git.invocation; a hook name read shares the same deadline.
    """
    deadline = time.monotonic() + GIT_QUERY_TIMEOUT
    try:
        argv, env = git.invocation(root, args, effect=effect, deadline=deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
    except (HarnessError, OSError, ValueError, subprocess.TimeoutExpired):
        return None
    try:
        output, _ = process.communicate(timeout=remaining)
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
    output = _git_query(root, "rev-parse", "--show-toplevel", "HEAD", effect=git.READ)
    lines = output.splitlines() if output else []
    # Only a checkout whose root is the package's parent describes this runtime.
    if len(lines) == 2 and Path(lines[0]).resolve() == root:
        identity["revision"] = lines[1].strip()
        status = _git_query(root, "status", "--porcelain", "--untracked-files=normal", "--", PACKAGE.name,
                            effect=git.COMPARE)
        identity["runtime_modified"] = None if status is None else bool(status.strip())
    return identity


def config_identity(config):
    """The files this configuration was loaded from, with the digests of the bytes then parsed; never reads them again.

    A provenance probe never raises: anything but a loaded value is recorded as null.
    """
    def digest(name):
        value = getattr(config, name, None)
        return value if isinstance(value, str) else None

    project = getattr(config, "project_path", None)
    return {"path": str(config.path), "sha256": digest("sha256"),
            "project": {"path": str(project), "sha256": digest("project_sha256")} if isinstance(project, Path) else None}


def shared_paths(config):
    return {key: str(config.asset(key)) for key in ("rules", "skills", "mcp")}


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
    """One run/resume invocation's record file: the CLI owns it, the Runtime only links a run."""

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

        Protection and exclusive creation share a common-Git short lock;
        duplicate IDs are rejected before ignore configuration changes.
        """
        if operation_id is None:
            operation_id = uid("op")
        elif not OPERATION_ID.fullmatch(operation_id):
            raise HarnessError("无效 --operation-id：以字母或数字开头，只含字母、数字、_ . -，最长 64 字符", stop=_cli_arguments("--operation-id"))
        started = time.time()
        record = {"operation_id": operation_id, "kind": kind, "status": "prepared",
                  "started_at": started, "started_at_utc": utc_text(started),
                  "arguments": arguments, "repo": str(repo), "repo_head": _repo_head(repo),
                  "harness": harness_identity(), "config": config_identity(config),
                  "asset_digest": digest_assets(config)[0], "models": role_models(config),
                  "cli": {name: probe_cli(name) for name in PROVIDER_CLIS},
                  "run_id": None, "run_linked_at": None,
                  "finished_at": None, "finished_at_utc": None,
                  "exit_status": None, "result_status": None, "error": None, "stop": None,
                  "error_omitted": None}
        directory = Path(state) / "operations"
        path = directory / f"{operation_id}.json"
        from .private_state import private_state_lock, initialize_private_state_locked, _path
        with private_state_lock(repo) as common:
            _path(directory, directory=True)
            if path.exists() or path.is_symlink():
                raise HarnessError(f"操作记录已存在：{path}；每个 --operation-id 只能由一个进程使用",
                                   stop=_cli_arguments("--operation-id"))
            initialize_private_state_locked(repo, state, common)
            directory.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            except FileExistsError as error:
                raise HarnessError(f"操作记录已存在：{path}；每个 --operation-id 只能由一个进程使用",
                                   stop=_cli_arguments("--operation-id")) from error
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

    def finish(self, *, exit_status, result_status=None, error=None, run_id=None, stop=None):
        """Best-effort final update: never raises, never changes the caller's exit status.

        ``stop`` is the projection the CLI reported; with file inputs its text
        is omitted, while kind, reason, exit code and exits are kept.
        """
        try:
            if self.record["run_id"] is None and run_id is not None:
                self.record.update(run_id=run_id, run_linked_at=time.time())
            finished = time.time()
            omitted = error is not None and self.file_inputs
            if stop is not None and omitted:
                stop = {key: value for key, value in stop.items() if key != "detail"}
            self.record.update(status="finished", finished_at=finished, finished_at_utc=utc_text(finished),
                               exit_status=exit_status, result_status=result_status,
                               error=first_line(error) if error is not None and not omitted else None,
                               stop=stop,
                               error_omitted=ERROR_OMITTED if omitted else None)
            write_json(self.path, self.record)
        except Exception as failure:
            warn(f"操作记录更新失败，不影响退出码：{self.path}：{failure}")
