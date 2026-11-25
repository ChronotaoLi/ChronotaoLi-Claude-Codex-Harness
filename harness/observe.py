"""Read-only observation of `.harness` run evidence for the dashboard and `status`.

Every state file read by this module goes through `harness.state_files` below the
resolved `.harness` directory. Directories on the walk open with search permission
and O_NOFOLLOW; the final file opens with O_NOFOLLOW|O_NONBLOCK and must fstat as
a regular file. This includes the ledger for `status RUN`. Nothing here constructs
a Runtime, loads a run for recovery, takes run.lock, writes files, runs git or
probes processes.

Ledger interpretation stays in `harness.cli.status_summary`; this module only adds
bounded projections (invocation classes, account quota observed during the run, event
tails, files). Quota is read from the samples already saved under `observations/quota/`;
no entry here starts a provider CLI. No money is aggregated: a dispatch's
`provider_usage.reported_cost_usd` is the CLI's nominal estimate, shown per row only.
Only the current ledger version is interpreted: the list names any other version
with its path and version alone, and the detail refuses it.
"""
from __future__ import annotations

from contextlib import suppress
import errno
import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePath

from . import quota, scope, state_files
from .process_runtime import defer_cleanup_signals
from .util import one_line, strict_json_loads
from .versions import unsupported_ledger


RUN_ID = re.compile(r"^run_[A-Za-z0-9]+$")
COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
COMMAND_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_DEPTH = 8
# What the runtime writes at the top of a run directory: every entry is addressable here or
# excluded by name. tests/test_harness.py checks every run directory a repository test leaves
# behind against these lists, so an entry added to the runtime without one fails there.
TOP_LEVEL_FILES = frozenset(("state.json", "events.jsonl", "handoff.json", "worktree-cleanup.json",
                             "workspace-inputs.json", "workspace-inputs-manifest.json"))
ROOT_DIRECTORIES = frozenset(("plans", "dispatches", "verification", "recovery", "decisions", "environment",
                              "checkpoints", "path-indexes", "observations", "preflight", "continuations",
                              "continuation-rejections", "execution-guidance", "proposal-rejections",
                              "input-indexes", "finding-progress"))
# The generated MCP configuration and empty lock files; *.tmp and memory.sqlite3* by pattern.
EXCLUDED_NAMES = frozenset(("mcp.json", "run.lock", "store.lock", "command-admission.lock", "remove.lock"))
DEFAULT_TAIL = 64 * 1024
MAX_TAIL = 1024 * 1024
STATE_MAX_BYTES = 64 * 1024 * 1024
OPERATION_MAX_BYTES = 256 * 1024
CLEANUP_RECEIPT_MAX_BYTES = 64 * 1024
OPERATIONS_MAX_FILES = 1000
FILE_INDEX_MAX = 2000
QUOTA_SAMPLES_MAX = 400       # the newest samples by name (their total order); at most 5 per run/resume
QUOTA_DAMAGED_LISTED = 20
ERROR_CHARS = 200
MAX_NUMBER = 2 ** 63  # Ledger numbers beyond this are treated as absent, never converted to float.
_PARSE_ERRORS = (ValueError, RecursionError)  # json.loads on damaged or absurdly nested input

MODEL_ROLES = ("commander", "worker", "reviewer")
PROVIDERS = ("claude", "codex", "unknown")

class ObserveError(Exception):
    """Base class for refusals the HTTP layer maps to status codes."""


class InvalidPath(ObserveError):
    """Malformed run ID, request path or parameter (HTTP 400)."""


class PathRefused(ObserveError):
    """Well-formed path refused by policy: excluded name, symlink, non-regular file (HTTP 403)."""

    def __init__(self, message, *, found=None, step=None):
        super().__init__(message)
        self.found, self.step = found, step


class ReadFailed(PathRefused):
    """A state read failed at the OS boundary (HTTP 403)."""

    def __init__(self, outcome, *, prefix=""):
        self.errno = outcome.errno
        self.error_type = outcome.error_type
        detail = os.strerror(self.errno) if self.errno is not None else self.error_type
        super().__init__(f"{prefix}无法读取（{outcome.step.stage}：{detail}）", step=outcome.step)


class NotFound(ObserveError):
    """The addressed run or file does not exist (HTTP 404)."""

    def __init__(self, message, *, step=None):
        super().__init__(message)
        self.step = step


class Unsupported(ObserveError):
    """The run's ledger is not the current version and is not interpreted (HTTP 409)."""


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="milliseconds")


def excluded_name(name):
    return name in EXCLUDED_NAMES or name.endswith(".tmp") or name.startswith("memory.sqlite3")


def canonical_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def validate_run_id(run_id):
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise InvalidPath("无效 run ID")
    return run_id


def validate_path(path):
    """Split a request path into components inside the fixed roots, or raise."""
    if not isinstance(path, str) or not path:
        raise InvalidPath("路径为空")
    components = path.split("/")
    if len(components) > MAX_DEPTH:
        raise InvalidPath("路径深度超过上限")
    if any(not COMPONENT.fullmatch(part) for part in components):
        raise InvalidPath("路径组件无效")
    if excluded_name(components[-1]):
        raise PathRefused("该文件在排除清单内")
    root = components[0]
    if root in TOP_LEVEL_FILES:
        if len(components) != 1:
            raise InvalidPath("固定文件下没有子路径")
    elif root in ROOT_DIRECTORIES:
        if len(components) < 2:
            raise InvalidPath("固定根目录本身不是文件")
        if root == "dispatches":
            if not canonical_uuid(components[1]):
                raise InvalidPath("dispatch ID 必须是小写规范 UUID")
            if len(components) >= 4 and components[2] == "commands" and not COMMAND_ID.fullmatch(components[3]):
                raise InvalidPath("命令记录目录名无效")
    else:
        raise InvalidPath("不在固定根目录内")
    return components


def _check(outcome, *, prefix=""):
    if outcome.state == "absent":
        raise NotFound(f"{prefix}文件不存在", step=outcome.step)
    if outcome.state == "failed":
        raise ReadFailed(outcome, prefix=prefix)
    if outcome.state == "refused":
        reason = {"symlink": "拒绝符号链接", "not_directory": "不是目录",
                  "changed": "读取时文件被改写"}.get(outcome.found, "拒绝非普通文件")
        raise PathRefused(f"{prefix}{reason}", found=outcome.found, step=outcome.step)


def error_type(error, *, ledger=False):
    if isinstance(error, ReadFailed):
        return error.error_type
    if isinstance(error, NotFound):
        return "FileNotFoundError"
    if ledger and isinstance(error, PathRefused):
        return {"directory": "IsADirectoryError", "not_directory": "NotADirectoryError"}.get(
            error.found, "PathRefused")
    return type(error).__name__


def location(given, resolved, *, checkout=None):
    """Which state directory an observation entry opened, and whose checkout it is. No git, no file access.

    ``resolved`` is the directory StateReader opened (None when the path could not be resolved).
    A checkout is known only when the caller passes it (the CLI's ``git.repository(--repo)``);
    otherwise a resolved directory named `.harness` only suggests its parent, and anything else is unknown.
    """
    state = str(resolved) if resolved is not None else str(given)
    if checkout is not None:
        checkout, basis = str(checkout), "git"
    elif resolved is not None and resolved.name == ".harness":
        checkout, basis = str(resolved.parent), "path"
    else:
        checkout, basis = None, None
    text = checkout if checkout is not None else state
    named = PurePath(text)
    parts = named.parts[1:] if named.anchor else named.parts
    return {"state": state, "checkout": checkout, "checkout_basis": basis, "name": "/".join(parts[-2:]) or text}


def location_text(observed, *, opened=True):
    """The one stderr wording of an entry's location, always one line; an inferred checkout says so."""
    if observed["checkout_basis"] == "git":
        checkout = f"检出 {one_line(observed['checkout'])}"
    elif observed["checkout_basis"] == "path":
        checkout = f"检出（按状态目录路径推定，未核实）{one_line(observed['checkout'])}"
    else:
        checkout = "检出 未知"
    return f"{checkout}；状态目录 {one_line(observed['state'])}" + ("" if opened else "（无法打开）")


class StateReader:
    """Adapt one held state root to observations and their public errors."""

    def __init__(self, state, *, required=False):
        self._held = state_files.StateFiles.held(state)
        try:
            with defer_cleanup_signals(inherit=False):
                self._held.__enter__()
            resolved = self._held.resolved_path
            self.state = Path(resolved) if resolved is not None else None
            if required and not self.opened:
                _check(self._held.opening_failure, prefix=f"观察目录不可用：{state}；")
        except BaseException:
            with suppress(state_files.StateReadError):
                self.close()
            raise

    @property
    def opened(self):
        return self._held.opened

    def close(self):
        self._held.close(checked=True)

    def open_directory(self, components, *, into):
        outcome, fd = self._held.open(components, "directory", into=into)
        _check(outcome)
        return fd

    def open_file(self, components, *, into):
        outcome, fd = self._held.open(components, "file", into=into)
        _check(outcome)
        return fd

    def probe(self, components, *, expect):
        _check(self._held.probe(components, expect=expect))

    def read(self, components, *, limit, mode="whole"):
        outcome = self._held.read(components, limit=limit, part=mode)
        _check(outcome)
        too_large = outcome.state == "too_large"
        return {"source": "/".join(components), "size": outcome.size, "too_large": too_large,
                "truncated": too_large or (mode != "whole" and outcome.size > limit),
                "data": outcome.data if not too_large else b"", "changed_while_reading": outcome.changed}

    def scandir(self, components):
        listing = self._held.entries(components, include_file_stats=True)
        _check(listing)
        file_stats = dict(listing.file_stats)
        return [(name, "other" if kind == "absent" else kind, file_stats.get(name))
                for name, kind in listing.entries]

    def empty_run_listing(self, error):
        # Preserve the list's empty result for a regular file in place of runs/
        # and for an unresolvable state-root link.
        if isinstance(error, ReadFailed):
            return error.errno == errno.ELOOP and error.step.level == "root" and error.step.stage == "resolve"
        step, found = error.step, error.found
        return step is not None and ((step.level == 0 and found == "not_directory")
                                     or (step.level == "root" and found == "symlink"))

    def walk(self, components, *, limit, max_depth):
        """List addressable regular files below a directory, depth-first and bounded."""
        files, skipped = [], {"symlink": 0, "other": 0, "excluded": 0, "unaddressable": 0}
        truncated = self._walk(self.scandir(components), list(components), files, skipped, limit, max_depth)
        return files, skipped, truncated

    def _walk(self, entries, components, files, skipped, limit, max_depth):
        for name, kind, info in entries:
            if len(files) >= limit:
                return True
            if kind in ("symlink", "other"):
                skipped[kind] += 1
                continue
            path = components + [name]
            if kind == "file":
                if excluded_name(name):
                    skipped["excluded"] += 1
                elif len(path) > max_depth or not COMPONENT.fullmatch(name):
                    skipped["unaddressable"] += 1
                else:
                    files.append((path, info))
                continue
            if len(path) >= max_depth or not COMPONENT.fullmatch(name):
                skipped["unaddressable"] += 1
                continue
            try:
                children = self.scandir(path)
            except ObserveError:
                skipped["other"] += 1
                continue
            if self._walk(children, path, files, skipped, limit, max_depth):
                return True
        return False


def _string(value):
    return value if isinstance(value, str) else None


def _integer(value):
    return value if type(value) is int else None


def _number(value):
    if type(value) is int:
        return value if -MAX_NUMBER < value < MAX_NUMBER else None
    return value if type(value) is float and math.isfinite(value) else None


def _boolean(value):
    return value if type(value) is bool else None


def _first_line(value, limit=ERROR_CHARS):
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    line = text.splitlines()[0] if text.splitlines() else ""
    return line[:limit]


def _tail_limit(tail):
    if tail is None:
        return DEFAULT_TAIL
    if type(tail) is not int or not 1 <= tail <= MAX_TAIL:
        raise InvalidPath(f"tail 必须是 1 到 {MAX_TAIL} 之间的整数")
    return tail


def classify_invocation(entry):
    """Apply the contradiction-first table from the specification to one ledger row."""
    role = entry.get("role")
    if role == "verification":
        return "verification", None
    if role not in MODEL_ROLES:
        return "unknown", "角色不是 commander、worker、reviewer 或 verification"
    invoked = entry.get("invoked")
    if type(invoked) is not bool:
        return "unknown", "invoked 字段缺失或不是布尔值"
    pgid, state = entry.get("pgid"), entry.get("process_state")
    if invoked and state == "prepared":
        return "unknown", "invoked=true 与 process_state=prepared 矛盾"
    if not invoked and (pgid is not None or state in ("starting", "running")):
        return "unknown", "invoked=false 与 pgid 或 process_state 表示的已启动状态矛盾"
    if invoked:
        return ("usage_recorded" if isinstance(entry.get("provider_usage"), dict) else "invoked_no_usage"), None
    if pgid is None and state in ("prepared", "exited"):
        return "known_not_invoked", None
    return "unknown", f"invoked=false 但 process_state={state!r} 无法归类"


def _provider_and_model(entry, roles, commander):
    usage = entry.get("provider_usage")
    provider = usage.get("provider") if isinstance(usage, dict) and isinstance(usage.get("provider"), str) else None
    role = entry.get("role")
    if role == "commander":
        key = {"astra": "commander", "fable": "fable"}.get(commander) if isinstance(commander, str) else None
    else:
        key = role if role in ("worker", "reviewer") else None
    spec = roles.get(key) if isinstance(roles, dict) and key else None
    spec = spec if isinstance(spec, dict) else None
    model_id = _string(spec.get("model")) if spec else None
    if provider is None and spec:
        provider = _string(spec.get("cli"))
    return provider, model_id


def _group():
    return {"dispatches": 0, "invoked": 0, "usage_recorded": 0, "invoked_no_usage": 0,
            "known_not_invoked": 0, "unknown": 0}


def invocation_summary(rows):
    """Invocation classes, once per ledger dispatch ID; never event telemetry, never money."""
    providers = {name: {"invoked": 0, "usage_recorded": 0, "invoked_no_usage": 0} for name in PROVIDERS}
    result = {"by_provider": providers, "known_not_invoked": 0, "unknown_invocation": 0, "startup_unconfirmed": 0,
              "by_group": {"coordination": _group(), "tasks": {}, "verification": {"dispatches": 0}},
              "duplicate_dispatch_ids": [], "counted_dispatches": 0}
    seen = set()
    for row in rows:
        if row["id"] in seen:
            result["duplicate_dispatch_ids"].append(row["id"])
            continue
        seen.add(row["id"])
        result["counted_dispatches"] += 1
        klass = row["invocation_class"]
        if row["startup_unconfirmed"]:
            result["startup_unconfirmed"] += 1
        if klass == "verification":
            result["by_group"]["verification"]["dispatches"] += 1
            continue
        if row["role"] == "commander":
            group = result["by_group"]["coordination"]
        elif row["role"] in ("worker", "reviewer"):
            group = result["by_group"]["tasks"].setdefault(row["task_id"] if row["task_id"] is not None else "", _group())
        else:
            group = None
        if group is not None:
            group["dispatches"] += 1
            group[klass if klass in ("usage_recorded", "invoked_no_usage", "known_not_invoked") else "unknown"] += 1
        if klass == "known_not_invoked":
            result["known_not_invoked"] += 1
            continue
        if klass == "unknown":
            result["unknown_invocation"] += 1
            continue
        bucket = row["provider"] if row["provider"] in ("claude", "codex") else "unknown"
        providers[bucket]["invoked"] += 1
        providers[bucket][klass] += 1
        if group is not None:
            group["invoked"] += 1
    return result


def dispatch_rows(run):
    """Project ledger dispatches; returns (rows, invalid_count, model_id_unavailable)."""
    entries = run.get("dispatches")
    entries = entries if isinstance(entries, list) else []
    snapshot = run.get("config_snapshot")
    roles = snapshot.get("roles") if isinstance(snapshot, dict) else None
    commander = run.get("commander")
    rows, invalid, unavailable = [], 0, False
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            invalid += 1
            continue
        klass, reason = classify_invocation(entry)
        provider, model_id = _provider_and_model(entry, roles, commander)
        role = _string(entry.get("role"))
        if role in MODEL_ROLES and model_id is None:
            unavailable = True
        pgid = entry.get("pgid")
        usage = entry.get("provider_usage")
        rows.append({
            "id": entry["id"], "role": role, "kind": _string(entry.get("kind")),
            "task_id": _string(entry.get("task_id")), "status": _string(entry.get("status")),
            "process_state": _string(entry.get("process_state")), "invoked": _boolean(entry.get("invoked")),
            "invoked_at": _number(entry.get("invoked_at")), "finished_at": None,
            "pgid": pgid if type(pgid) is int and pgid > 0 else None,
            "exit_confirmed": _boolean(entry.get("exit_confirmed")),
            "error": _first_line(entry.get("error")),
            "worker_outcome": _string(entry.get("worker_outcome")),
            "phase": _string(entry.get("phase")),
            "provider": provider if role in MODEL_ROLES else None,
            "model_id": model_id if role in MODEL_ROLES else None,
            "provider_usage": usage if isinstance(usage, dict) else None,
            "invocation_class": klass, "invocation_reason": reason,
            "startup_unconfirmed": entry.get("process_state") == "starting",
            "files": []})
    return rows, invalid, unavailable


def task_rows(run):
    tasks = run.get("tasks")
    accepted = run.get("accepted")
    accepted = accepted if isinstance(accepted, dict) else {}
    rows, invalid = [], 0
    for task in (tasks if isinstance(tasks, list) else []):
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            invalid += 1
            continue
        depends = task.get("depends_on")
        rows.append({"id": task["id"], "title": _string(task.get("title")),
                     "depends_on": [d for d in depends if isinstance(d, str)] if isinstance(depends, list) else [],
                     "accepted": task["id"] in accepted})
    return rows, invalid


def parse_event_tail(data, partial_first):
    """Parse newline-delimited JSON; the final unterminated line is in progress, not an error."""
    block = {"records": [], "unparsed_tail": False, "invalid_lines": 0, "last_time": None}
    lines = data.split(b"\n")
    if partial_first and lines:
        lines = lines[1:]
    remainder = lines.pop() if lines else b""
    if remainder.strip():
        block["unparsed_tail"] = True
    last_parsed = None
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except _PARSE_ERRORS:
            record = None
        if isinstance(record, dict):
            block["records"].append(record)
            last_parsed = True
        else:
            block["invalid_lines"] += 1
            last_parsed = False
    if last_parsed is False:
        block["unparsed_tail"] = True
    if block["records"]:
        block["last_time"] = _number(block["records"][-1].get("time"))
    return block


def _operation_summary(name, record):
    summary = {"file": name}
    for key in ("operation_id", "kind", "status", "started_at", "started_at_utc", "finished_at", "finished_at_utc",
                "run_id", "run_linked_at", "exit_status", "result_status", "repo", "repo_head",
                "arguments", "harness", "config", "models", "cli", "stop"):
        summary[key] = record.get(key)
    summary["error"] = _first_line(record.get("error"))
    return summary


class Observer:
    """Pure read projections over one `.harness` directory."""

    def __init__(self, state, reader=None, *, required=False, checkout=None):
        # A reader the caller passes stays the caller's; one built here is closed on any failure below,
        # including the signal its deferral raises once the assignment is done.
        self.reader, owned = reader, None
        try:
            if reader is None:
                with defer_cleanup_signals(inherit=False):
                    owned = self.reader = StateReader(state, required=required)
            # Fixed with the root descriptor: every view names the directory it reads, never recomputed.
            self.observed = location(state, self.reader.state, checkout=checkout)
        except BaseException:
            if owned is not None:
                with suppress(state_files.StateReadError):
                    owned.close()
            raise

    def close(self):
        self.reader.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        _closing(self.close, suppress_io=kind is not None)

    # ----- ledger -----

    def _read_ledger(self, run_id):
        """Return (value, error); error is None exactly when state.json parsed.

        A parsed value is returned as is, object or not: callers refuse anything
        but a current-version object (``unsupported_ledger``) before reading a field.
        A missing run directory or state.json raises NotFound.
        """
        read = self.reader.read(["runs", run_id, "state.json"], limit=STATE_MAX_BYTES)
        if read["too_large"]:
            return None, f"state.json 超过 {STATE_MAX_BYTES} 字节上限，未解析"
        try:
            value = json.loads(read["data"])
        except _PARSE_ERRORS as error:
            return None, f"state.json 无法解析：{type(error).__name__}: {_first_line(str(error))}"
        return value, None

    def list_runs(self):
        sampled = time.time()
        result = {"sampled_at": sampled, "sampled_at_utc": utc(sampled), "runs": [], "skipped": [], "incomplete": [],
                  "unsupported": [], "observed": self.observed}
        try:
            entries = self.reader.scandir(["runs"])
        except NotFound:
            return result
        except PathRefused as error:
            if self.reader.empty_run_listing(error):
                return result
            raise
        for name, kind, _ in entries:
            if kind == "symlink":
                result["skipped"].append({"path": f"runs/{name}", "reason": "符号链接"})
                continue
            if kind != "dir":
                continue
            path = f"runs/{name}/state.json"
            if not RUN_ID.fullmatch(name):
                result["skipped"].append({"path": f"runs/{name}", "reason": "目录名不是合法 run ID"})
                continue
            try:
                run, error = self._read_ledger(name)
            except ObserveError as failure:
                run, error = None, str(failure)
            if error is not None:
                result["skipped"].append({"path": path, "reason": error})
                continue
            refusal = unsupported_ledger(run)
            if refusal is not None:
                # Not a current-version object: its path and version only; no other field is read.
                version = run.get("schema_version") if isinstance(run, dict) else None
                result["unsupported"].append({"path": f"runs/{name}", "schema_version": _integer(version),
                                              "reason": refusal})
                continue
            fields = ("id", "status", "goal", "branch")
            if any(key not in run for key in fields):
                result["incomplete"].append(path)
            # Same projection as the CLI status list: raw ledger values, directory
            # name and "unknown" only when the key is absent.
            item = {key: run.get(key) for key in fields}
            item["id"] = run.get("id", name)
            item["status"] = run.get("status", "unknown")
            item["run_dir"] = name
            item["schema_version"] = _integer(run.get("schema_version"))
            try:
                item["last_event_time"] = self._events(name, DEFAULT_TAIL)["last_time"]
            except ObserveError:
                item["last_event_time"] = None
            result["runs"].append(item)
        return result

    def run_summary(self, run, run_id, *, integrity_stop=None):
        from .cli import status_summary  # resolved at call time to avoid importing CLI through dashboard

        summary = status_summary(run, integrity_stop=integrity_stop)
        if "scope" in summary:
            held = scope.held_view(run, self.reader, run_id=run_id)
            if held is not None:
                summary["scope"]["held"] = held
        return summary

    def run_detail(self, run_id):
        validate_run_id(run_id)
        sampled = time.time()
        run, ledger_error = self._read_ledger(run_id)
        if ledger_error is None and (refusal := unsupported_ledger(run)) is not None:
            raise Unsupported(refusal)  # Nothing else of this run is read.
        detail = {"id": run_id, "sampled_at": sampled, "sampled_at_utc": utc(sampled),
                  "ledger_error": ledger_error, "summary": None, "summary_error": None,
                  "schema_version": None, "unavailable": [], "run": None, "budgets": None,
                  "tasks": [], "invalid_tasks": 0, "dispatches": [], "invalid_dispatches": 0,
                  "invocations": invocation_summary([]), "quota": None, "timing": None, "events": None,
                  "operations": None,
                  "notes": {"invoked": "invoked=true 在进程启动前写入；它不证明 CLI 已启动或供应商已收到请求",
                            "process": "首版不探测进程；台账记为 running 与进程是否仍在工作是两回事",
                            "events": ("事件只是观察，计数以台账为准：dispatch_prepared 在预算检查前写入，"
                                       "dispatch_started 在台账保存 invoked=true 之后写入")},
                  "observed": self.observed}
        try:
            detail["events"] = self._events(run_id, DEFAULT_TAIL)
        except ObserveError as error:
            detail["events"] = {"records": [], "exists": False, "error": str(error), "unparsed_tail": False,
                                "truncated": False, "invalid_lines": 0, "last_time": None, "tail_bytes": DEFAULT_TAIL}
        detail["timing"] = {"first_event_time": self._first_event_time(run_id),
                            "last_event_time": detail["events"]["last_time"], "basis": "events.jsonl 事件时间"}
        detail["operations"] = self._operations(run_id)
        if ledger_error is not None:
            return detail
        try:
            detail["summary"] = self.run_summary(run, run_id)
        except Exception as error:  # Any ledger damage: HarnessError, TypeError, KeyError, ...
            detail["summary_error"] = f"{type(error).__name__}: {_first_line(str(error))}"
        detail["schema_version"] = run["schema_version"]
        unavailable = detail["unavailable"]
        if (detail["summary"] is not None and "result" not in detail["summary"]
                and (run.get("status") == "completed" or "result" in run)):
            unavailable.append("result")
        detail["run"] = {key: _string(run.get(key)) for key in
                         ("id", "status", "goal", "branch", "worktree", "repo", "commander", "current_task",
                          "created_by_operation", "config_path", "expected_head", "base_commit")}
        detail["run"]["worktree_removed"] = self._worktree_cleanup_removed(run_id)
        detail["budgets"] = self._budgets(run)
        detail["tasks"], detail["invalid_tasks"] = task_rows(run)
        rows, detail["invalid_dispatches"], model_unavailable = dispatch_rows(run)
        if model_unavailable:
            unavailable.append("model_id")
        # Events only add an end time, at most one per ledger dispatch ID; invocation
        # facts stay with the ledger row, never with event counts.
        finished = {}
        for record in detail["events"]["records"]:
            if record.get("event") == "dispatch_finished" and isinstance(record.get("dispatch_id"), str):
                finished.setdefault(record["dispatch_id"], _number(record.get("time")))
        for row in rows:
            row["finished_at"] = finished.get(row["id"])
            row["files"] = self._dispatch_files(run_id, row["id"])
        detail["dispatches"] = rows
        detail["invocations"] = invocation_summary(rows)
        detail["quota"] = self.quota_block(run_id)
        return detail

    # ----- quota samples -----

    def quota_samples(self, run_id):
        """The saved quota samples, validated, in their total order (the file names): the newest QUOTA_SAMPLES_MAX.

        Symbolic links, other entries and names outside the sample pattern are
        ``skipped``; a sample that cannot be read, is too large, fails the schema, does
        not match its own name or changes while read is ``damaged`` (names listed up
        to QUOTA_DAMAGED_LISTED). Neither raises.
        """
        validate_run_id(run_id)
        block = {"samples": [], "omitted": 0, "skipped": 0, "damaged": [], "damaged_count": 0}
        try:
            entries = self.reader.scandir(["runs", run_id, *quota.QUOTA_DIR])
        except NotFound:
            return block
        names = []
        for name, kind, _ in entries:
            if kind == "file" and quota.NAME.fullmatch(name):
                names.append(name)
            else:
                block["skipped"] += 1
        block["omitted"] = max(0, len(names) - QUOTA_SAMPLES_MAX)
        for name in names[block["omitted"]:]:
            try:
                read = self.reader.read(["runs", run_id, *quota.QUOTA_DIR, name], limit=quota.SAMPLE_MAX_BYTES)
                if read["too_large"] or read["changed_while_reading"]:
                    raise ValueError("unreadable sample")
                sample = quota.validate_sample(strict_json_loads(read["data"]))
                if quota.sample_name(sample) != name:
                    raise ValueError("sample name differs from its content")
            except Exception:
                block["damaged_count"] += 1
                if len(block["damaged"]) < QUOTA_DAMAGED_LISTED:
                    block["damaged"].append(name)
                continue
            block["samples"].append(sample)
        return block

    def quota_block(self, run_id):
        """The observed account quota from saved samples (``quota.quota_view``); a read failure is ``{"error": type}``."""
        try:
            block = self.quota_samples(run_id)
            view = quota.quota_view(block["samples"], truncated=block["omitted"] > 0)
            return {"notes": view["notes"], "damaged_count": block["damaged_count"], "omitted": block["omitted"],
                    "providers": view["providers"]}
        except Exception as error:
            return {"error": type(error).__name__}

    def _worktree_cleanup_removed(self, run_id):
        """cleanup-run's receipt: true once it records removal; null when unreadable."""
        try:
            read = self.reader.read(["runs", run_id, "worktree-cleanup.json"], limit=CLEANUP_RECEIPT_MAX_BYTES)
        except NotFound:
            return False
        except ObserveError:
            return None
        if read["too_large"] or read["changed_while_reading"]:
            return None
        try:
            receipt = json.loads(read["data"])
        except _PARSE_ERRORS:
            return None
        return isinstance(receipt, dict) and receipt.get("status") == "removed"

    @staticmethod
    def _budgets(run):
        budgets = {"agent_dispatches": _integer(run.get("agent_dispatches"))}
        attempts = run.get("attempts")
        budgets["attempts"] = ({key: value for key, value in attempts.items() if type(value) is int}
                               if isinstance(attempts, dict) else None)
        # Every worker invocation, derived from the per-task counters.
        budgets["attempts_total"] = (_integer(sum(budgets["attempts"].values()))
                                     if budgets["attempts"] is not None else None)
        snapshot = run.get("config_snapshot")
        runtime = snapshot.get("runtime") if isinstance(snapshot, dict) else None
        runtime = runtime if isinstance(runtime, dict) else {}
        budgets["limits"] = {key: _integer(runtime.get(key)) for key in
                             ("max_attempts", "max_agent_dispatches", "agent_timeout_seconds")}
        return budgets

    def _dispatch_files(self, run_id, dispatch_id):
        if not canonical_uuid(dispatch_id):
            return []
        try:
            entries = self.reader.scandir(["runs", run_id, "dispatches", dispatch_id])
        except ObserveError:
            return []
        return [name for name, kind, _ in entries if kind == "file" and not excluded_name(name)]

    # ----- events -----

    def _events(self, run_id, tail):
        tail = _tail_limit(tail)
        block = {"exists": True, "tail_bytes": tail, "size": None, "truncated": False,
                 "changed_while_reading": False}
        try:
            # One extra leading byte shows whether the first tail line is whole.
            read = self.reader.read(["runs", run_id, "events.jsonl"], limit=tail + 1, mode="tail")
        except NotFound:
            block.update(exists=False, records=[], unparsed_tail=False, invalid_lines=0, last_time=None)
            return block
        block.update(size=read["size"], truncated=read["truncated"],
                     changed_while_reading=read["changed_while_reading"])
        data, partial_first = read["data"], False
        if read["truncated"]:
            if data[:1] == b"\n":
                data = data[1:]
            else:
                partial_first = True
        block.update(parse_event_tail(data, partial_first))
        return block

    def _first_event_time(self, run_id):
        """Time of the first parsable record within the bounded head; null when undeterminable."""
        try:
            read = self.reader.read(["runs", run_id, "events.jsonl"], limit=DEFAULT_TAIL, mode="head")
        except ObserveError:
            return None
        lines = read["data"].split(b"\n")
        if read["truncated"]:
            lines.pop()  # The chunk ends mid-line; that line cannot be judged.
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except _PARSE_ERRORS:
                continue
            if isinstance(record, dict):
                return _number(record.get("time"))
        return None

    def events(self, run_id, tail=None):
        validate_run_id(run_id)
        sampled = time.time()
        self.reader.probe(["runs", run_id], expect="directory")  # Missing run: 404 even without events.jsonl
        return {"id": run_id, "sampled_at": sampled, "sampled_at_utc": utc(sampled), **self._events(run_id, tail),
                "observed": self.observed}

    # ----- operations -----

    def _operations(self, run_id):
        block = {"records": [], "scanned": 0, "invalid": 0, "truncated": False}
        try:
            entries = self.reader.scandir(["operations"])
        except ObserveError:
            return block
        names = [name for name, kind, _ in entries
                 if kind == "file" and name.endswith(".json") and COMPONENT.fullmatch(name)]
        if len(names) > OPERATIONS_MAX_FILES:
            block["truncated"] = True
            names = names[:OPERATIONS_MAX_FILES]
        for name in names:
            block["scanned"] += 1
            try:
                read = self.reader.read(["operations", name], limit=OPERATION_MAX_BYTES)
                if read["too_large"]:
                    raise ValueError("too large")
                record = json.loads(read["data"])
                if not isinstance(record, dict):
                    raise ValueError("not an object")
            except (ObserveError, *_PARSE_ERRORS):
                block["invalid"] += 1
                continue
            if record.get("run_id") != run_id:
                continue
            block["records"].append(_operation_summary(name, record))
        block["records"].sort(key=lambda item: (item["started_at"] if type(item["started_at"]) in (int, float)
                                                else 0, item["file"]))
        return block

    # ----- files -----

    def file_index(self, run_id):
        validate_run_id(run_id)
        sampled = time.time()
        entries = self.reader.scandir(["runs", run_id])
        files, skipped, truncated = [], {"symlink": 0, "other": 0, "excluded": 0, "unaddressable": 0}, False
        for name, kind, info in entries:
            if truncated:
                break
            if kind == "file":
                if excluded_name(name):
                    skipped["excluded"] += 1
                elif name in TOP_LEVEL_FILES:
                    if len(files) >= FILE_INDEX_MAX:
                        truncated = True  # One shared cap for every entry source.
                        break
                    files.append(([name], info))
                else:
                    skipped["unaddressable"] += 1
            elif kind == "dir":
                if name not in ROOT_DIRECTORIES:
                    skipped["unaddressable"] += 1
                    continue
                found, found_skipped, found_truncated = self.reader.walk(
                    ["runs", run_id, name], limit=FILE_INDEX_MAX - len(files), max_depth=MAX_DEPTH + 2)
                for path, file_info in found:
                    relative = path[2:]
                    try:
                        validate_path("/".join(relative))
                    except ObserveError:
                        skipped["unaddressable"] += 1
                        continue
                    files.append((relative, file_info))
                for key, value in found_skipped.items():
                    skipped[key] += value
                truncated = truncated or found_truncated
            elif kind == "symlink":
                skipped["symlink"] += 1
            else:
                skipped["other"] += 1
        return {"id": run_id, "sampled_at": sampled, "sampled_at_utc": utc(sampled), "truncated": truncated,
                "limit": FILE_INDEX_MAX, "skipped": skipped,
                "files": [{"path": "/".join(path), "size": info.st_size, "mtime": info.st_mtime}
                          for path, info in files], "observed": self.observed}

    def read_file(self, run_id, path, tail=None):
        validate_run_id(run_id)
        components = validate_path(path)
        tail = _tail_limit(tail)
        sampled = time.time()
        read = self.reader.read(["runs", run_id, *components], limit=tail, mode="tail")
        return {"id": run_id, "source": "/".join(components), "size": read["size"], "tail_bytes": tail,
                "truncated": read["truncated"], "changed_while_reading": read["changed_while_reading"],
                "sampled_at": sampled, "sampled_at_utc": utc(sampled),
                "text": read["data"].decode("utf-8", errors="replace"), "observed": self.observed}


def holding(use, close, *, keep=False):
    """Return ``use()``; ``close()`` runs as it leaves (with ``keep``, only when an exception does), in two nested cleanups.

    Both are set up before ``use`` opens anything, and each runs ``close`` inside a deferral of the signals. So one
    SIGINT or SIGTERM anywhere (while ``use`` opens or uses its descriptors, at the entry of the first cleanup, where
    no owner's own ``__exit__`` or ``close`` can see it, or while it closes) leaves with every descriptor closed: one
    kept while closing is raised once the close is done, and the outer cleanup closes what the inner one did not
    reach. A second signal while the first one's cleanup runs is not covered. ``use`` stores each descriptor where
    ``close`` finds it inside a deferral of its own, so a signal as it is taken is raised inside these cleanups;
    ``close`` closes what is still open, once, and is safe to call again or before anything is open. With ``keep``,
    what ``use`` opened is its caller's once it returns.
    """
    kept = completed = False
    try:
        try:
            result = use()
            kept, completed = keep, True
            return result
        finally:
            if not kept:
                _closing(close, suppress_io=not completed)
    finally:
        if not kept:
            _closing(close, suppress_io=not completed)


def _closing(close, *, suppress_io):
    # Kept while closing, a signal is raised only once everything is closed.
    with defer_cleanup_signals(inherit=False):
        try:
            close()
        except (state_files.StateReadError, OSError):
            if not suppress_io:
                raise


def observing(state, use, **options):
    """``use(observer)`` for an Observer of ``state`` built here, closed however this leaves (``holding``)."""
    held = []

    def opened():
        with defer_cleanup_signals(inherit=False):
            held.append(Observer(state, **options))
        return use(held[0])

    def close():
        for observer in held:
            observer.close()
    return holding(opened, close)


def list_runs(state):
    return observing(state, Observer.list_runs)


def run_detail(state, run_id):
    return observing(state, lambda observer: observer.run_detail(run_id))
