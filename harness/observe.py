"""Read-only observation of `.harness` run evidence for the dashboard and `status`.

Every byte this module reads comes through one descriptor walk below the resolved
`.harness` directory: each intermediate directory opens with O_DIRECTORY|O_NOFOLLOW
from its parent descriptor, the final component opens with O_NOFOLLOW|O_NONBLOCK and
must fstat as a regular file. Nothing here constructs an Engine, loads a run for
recovery, takes run.lock, writes files, runs git or probes processes.

Ledger interpretation stays in `harness.cli.status_summary`; this module only adds
bounded projections (invocation classes, per-provider cost, event tails, files).
"""
from __future__ import annotations

import errno
import json
import math
import os
import re
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


RUN_ID = re.compile(r"^run_[A-Za-z0-9]+$")
COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
COMMAND_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_DEPTH = 8
TOP_LEVEL_FILES = frozenset(("state.json", "events.jsonl", "handoff.json", "handoff.md",
                             "ignored-baseline.json", "ignored-paths.json"))
ROOT_DIRECTORIES = frozenset(("plans", "dispatches", "verification", "hooks", "recovery", "decisions",
                              "environment", "checkpoints", "path-indexes", "indexes", "batches"))
EXCLUDED_NAMES = frozenset(("mcp.json", "run.lock"))
DEFAULT_TAIL = 64 * 1024
MAX_TAIL = 1024 * 1024
STATE_MAX_BYTES = 64 * 1024 * 1024
OPERATION_MAX_BYTES = 256 * 1024
OPERATIONS_MAX_FILES = 1000
FILE_INDEX_MAX = 2000
ERROR_CHARS = 200
MAX_NUMBER = 2 ** 63  # Ledger numbers beyond this are treated as absent, never converted to float.
MAX_COST_USD = 1_000_000_000
_PARSE_ERRORS = (ValueError, RecursionError)  # json.loads on damaged or absurdly nested input

MODEL_ROLES = ("commander", "worker", "reviewer")
PROVIDERS = ("claude", "codex", "unknown")
PROVIDER_LABELS = {"claude": "CLI 报告的估算费用，非账户账单",
                   "codex": "供应商未报告美元费用",
                   "unknown": "无法确定供应商"}
INVOCATION_CLASSES = ("usage_recorded", "invoked_no_usage", "known_not_invoked", "unknown", "verification")

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CHUNK = 1024 * 1024


class ObserveError(Exception):
    """Base class for refusals the HTTP layer maps to status codes."""


class InvalidPath(ObserveError):
    """Malformed run ID, request path or parameter (HTTP 400)."""


class PathRefused(ObserveError):
    """Well-formed path refused by policy: excluded name, symlink, non-regular file (HTTP 403)."""


class NotFound(ObserveError):
    """The addressed run or file does not exist (HTTP 404)."""


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


def _translate(error, dir_fd, name):
    """Classify a refused open; O_NOFOLLOW already guaranteed no link was followed."""
    if error.errno == errno.ELOOP:
        return PathRefused("拒绝符号链接")
    if error.errno in (errno.ENOENT, errno.ENOTDIR):
        try:  # macOS reports ENOTDIR for a directory symlink under O_DIRECTORY|O_NOFOLLOW.
            if stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                return PathRefused("拒绝符号链接")
        except OSError:
            pass
        return NotFound("文件不存在")
    return PathRefused(f"无法打开：{error.strerror}")


def _pread_exact(fd, length, offset):
    chunks = []
    while length > 0:
        chunk = os.pread(fd, min(length, _CHUNK), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
        length -= len(chunk)
    return b"".join(chunks)


def _scan(fd):
    entries = []
    with os.scandir(fd) as iterator:
        for entry in iterator:
            try:
                if entry.is_symlink():
                    kind, info = "symlink", None
                elif entry.is_dir(follow_symlinks=False):
                    kind, info = "dir", None
                elif entry.is_file(follow_symlinks=False):
                    kind, info = "file", entry.stat(follow_symlinks=False)
                else:
                    kind, info = "other", None
            except OSError:
                kind, info = "other", None
            entries.append((entry.name, kind, info))
    entries.sort(key=lambda item: item[0])
    return entries


class StateReader:
    """Descriptor-anchored reads below one resolved `.harness` directory."""

    def __init__(self, state, *, required=False):
        """Fix the root descriptor now; it is never reopened or replaced later.

        With required=False a missing `.harness` yields empty listings and NotFound
        reads (the `status` list stays usable); required=True raises NotFound so a
        long-lived service refuses to start against a directory it could not fix.
        """
        self.state = Path(state).resolve()
        try:
            self._root = os.open(str(self.state), _DIR_FLAGS)
        except OSError as error:
            if required:
                raise NotFound(f"观察目录不可用：{self.state}") from error
            self._root = None

    def close(self):
        if self._root is not None:
            os.close(self._root)
            self._root = None

    def _root_fd(self):
        if self._root is None:
            raise NotFound("观察目录不可用")
        return self._root

    def open_directory(self, components):
        """Open each component with O_DIRECTORY|O_NOFOLLOW from its parent; caller closes."""
        fd = os.dup(self._root_fd())
        os.set_inheritable(fd, False)
        try:
            for name in components:
                try:
                    child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                except OSError as error:
                    raise _translate(error, fd, name) from error
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def open_file(self, components):
        *parents, name = components
        parent = self.open_directory(parents)
        try:
            try:
                fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
            except OSError as error:
                raise _translate(error, parent, name) from error
        finally:
            os.close(parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise PathRefused("拒绝非普通文件")
        except BaseException:
            os.close(fd)
            raise
        return fd, info

    def read(self, components, *, limit, mode="whole"):
        """Read a regular file: whole (refused above limit), head or tail of at most limit bytes."""
        fd, info = self.open_file(components)
        try:
            size = info.st_size
            result = {"source": "/".join(components), "size": size, "truncated": False,
                      "too_large": False, "data": b""}
            if mode == "tail":
                offset = max(0, size - limit)
                result["truncated"] = offset > 0
                result["data"] = _pread_exact(fd, size - offset, offset)
            elif mode == "head":
                result["truncated"] = size > limit
                result["data"] = _pread_exact(fd, min(size, limit), 0)
            elif size > limit:
                result.update(truncated=True, too_large=True)
            else:
                result["data"] = _pread_exact(fd, size, 0)
            after = os.fstat(fd)
            result["changed_while_reading"] = ((after.st_size, after.st_mtime_ns, after.st_ino)
                                               != (info.st_size, info.st_mtime_ns, info.st_ino))
        finally:
            os.close(fd)
        return result

    def scandir(self, components):
        fd = self.open_directory(components)
        try:
            return _scan(fd)
        finally:
            os.close(fd)

    def walk(self, components, *, limit, max_depth):
        """List addressable regular files below a directory, depth-first and bounded."""
        files, skipped, truncated = [], {"symlink": 0, "other": 0, "excluded": 0, "unaddressable": 0}, False
        fd = self.open_directory(components)
        try:
            truncated = self._walk(fd, list(components), files, skipped, limit, max_depth)
        finally:
            os.close(fd)
        return files, skipped, truncated

    def _walk(self, fd, components, files, skipped, limit, max_depth):
        for name, kind, info in _scan(fd):
            if len(files) >= limit:
                return True
            if kind == "symlink":
                skipped["symlink"] += 1
                continue
            if kind == "other":
                skipped["other"] += 1
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
                child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except OSError:
                skipped["other"] += 1
                continue
            try:
                if self._walk(child, path, files, skipped, limit, max_depth):
                    return True
            finally:
                os.close(child)
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


def _status_summary(run):
    from .cli import status_summary  # resolved at call time; cli may import dashboard later
    return status_summary(run)


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


def _reported_cost(usage):
    if not isinstance(usage, dict):
        return None
    cost = usage.get("reported_cost_usd")
    if type(cost) is int:
        return cost if 0 <= cost <= MAX_COST_USD else None
    return cost if type(cost) is float and math.isfinite(cost) and 0 <= cost <= MAX_COST_USD else None


def _group():
    return {"dispatches": 0, "invoked": 0, "usage_recorded": 0, "invoked_no_usage": 0,
            "known_not_invoked": 0, "unknown": 0,
            "reported_cost_usd": dict.fromkeys(PROVIDERS),
            "reported_cost_dispatches": dict.fromkeys(PROVIDERS, 0)}


def _add_cost(target, bucket, cost):
    target["reported_cost_usd"][bucket] = (target["reported_cost_usd"][bucket] or 0) + cost
    target["reported_cost_dispatches"][bucket] += 1


def cost_summary(rows):
    """Accumulate once per ledger dispatch ID; never add event telemetry on top."""
    providers = {name: {"invoked": 0, "usage_recorded": 0, "invoked_no_usage": 0, "reported_cost_usd": None,
                        "reported_cost_dispatches": 0, "label": PROVIDER_LABELS[name]} for name in PROVIDERS}
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
        cost = _reported_cost(row["provider_usage"])
        if cost is not None:
            providers[bucket]["reported_cost_usd"] = (providers[bucket]["reported_cost_usd"] or 0) + cost
            providers[bucket]["reported_cost_dispatches"] += 1
            if group is not None:
                _add_cost(group, bucket, cost)
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
                "arguments", "harness", "config", "models", "cli"):
        summary[key] = record.get(key)
    summary["error"] = _first_line(record.get("error"))
    return summary


class Observer:
    """Pure read projections over one `.harness` directory."""

    def __init__(self, state, reader=None, *, required=False):
        self.reader = reader or StateReader(state, required=required)

    def close(self):
        self.reader.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ----- ledger -----

    def _read_ledger(self, run_id):
        """Return (ledger, error). Missing run directory or state.json raises NotFound."""
        read = self.reader.read(["runs", run_id, "state.json"], limit=STATE_MAX_BYTES)
        if read["too_large"]:
            return None, f"state.json 超过 {STATE_MAX_BYTES} 字节上限，未解析"
        try:
            value = json.loads(read["data"])
        except _PARSE_ERRORS as error:
            return None, f"state.json 无法解析：{type(error).__name__}: {_first_line(str(error))}"
        if not isinstance(value, dict):
            return None, "运行台账必须为对象"
        return value, None

    def list_runs(self):
        sampled = time.time()
        result = {"sampled_at": sampled, "sampled_at_utc": utc(sampled), "runs": [], "skipped": [], "incomplete": []}
        try:
            entries = self.reader.scandir(["runs"])
        except NotFound:
            return result
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
            if run is None:
                result["skipped"].append({"path": path, "reason": error})
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

    def run_detail(self, run_id):
        validate_run_id(run_id)
        sampled = time.time()
        run, ledger_error = self._read_ledger(run_id)
        detail = {"id": run_id, "sampled_at": sampled, "sampled_at_utc": utc(sampled),
                  "ledger_error": ledger_error, "summary": None, "summary_error": None,
                  "schema_version": None, "unavailable": [], "run": None, "budgets": None,
                  "tasks": [], "invalid_tasks": 0, "dispatches": [], "invalid_dispatches": 0,
                  "cost": cost_summary([]), "timing": None, "events": None, "operations": None,
                  "notes": {"invoked": "invoked=true 在进程启动前写入；它不证明 CLI 已启动或供应商已收到请求",
                            "process": "首版不探测进程；台账记为 running 与进程是否仍在工作是两回事"}}
        try:
            detail["events"] = self._events(run_id, DEFAULT_TAIL)
        except ObserveError as error:
            detail["events"] = {"records": [], "exists": False, "error": str(error), "unparsed_tail": False,
                                "truncated": False, "invalid_lines": 0, "last_time": None, "tail_bytes": DEFAULT_TAIL}
        detail["timing"] = {"first_event_time": self._first_event_time(run_id),
                            "last_event_time": detail["events"]["last_time"], "basis": "events.jsonl 事件时间"}
        detail["operations"] = self._operations(run_id)
        if run is None:
            return detail
        try:
            detail["summary"] = _status_summary(run)
        except Exception as error:  # Any ledger damage: HarnessError, TypeError, KeyError, ...
            detail["summary_error"] = f"{type(error).__name__}: {_first_line(str(error))}"
        version = _integer(run.get("schema_version"))
        detail["schema_version"] = version
        unavailable = detail["unavailable"]
        if version is None or version < 10:
            unavailable.append("decisions")
        if version is None or version < 9:
            unavailable.append("environment")
        if "created_by_operation" not in run:
            unavailable.append("operation")
        detail["run"] = {key: _string(run.get(key)) for key in
                         ("id", "status", "goal", "branch", "worktree", "repo", "commander", "current_task",
                          "error_type", "created_by_operation", "config_path", "expected_head", "base_commit")}
        detail["run"]["error"] = _first_line(run.get("error"))
        detail["run"]["worktree_removed"] = _boolean(run.get("worktree_removed"))
        detail["budgets"] = self._budgets(run)
        detail["tasks"], detail["invalid_tasks"] = task_rows(run)
        rows, detail["invalid_dispatches"], model_unavailable = dispatch_rows(run)
        if model_unavailable:
            unavailable.append("model_id")
        finished = {}
        for record in detail["events"]["records"]:
            if record.get("event") == "dispatch_finished" and isinstance(record.get("dispatch_id"), str):
                finished[record["dispatch_id"]] = _number(record.get("time"))
        for row in rows:
            row["finished_at"] = finished.get(row["id"])
            row["files"] = self._dispatch_files(run_id, row["id"])
        detail["dispatches"] = rows
        detail["cost"] = cost_summary(rows)
        return detail

    @staticmethod
    def _budgets(run):
        budgets = {key: _integer(run.get(key)) for key in
                   ("total_attempts", "agent_dispatches", "replan_attempts", "final_attempts")}
        attempts = run.get("attempts")
        budgets["attempts"] = ({key: value for key, value in attempts.items() if type(value) is int}
                               if isinstance(attempts, dict) else None)
        budgets["attempt_budget_policy"] = _string(run.get("attempt_budget_policy"))
        snapshot = run.get("config_snapshot")
        runtime = snapshot.get("runtime") if isinstance(snapshot, dict) else None
        runtime = runtime if isinstance(runtime, dict) else {}
        budgets["limits"] = {key: _integer(runtime.get(key)) for key in
                             ("max_attempts", "max_total_attempts", "max_agent_dispatches", "max_replans",
                              "max_final_attempts", "agent_timeout_seconds")}
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
        os.close(self.reader.open_directory(["runs", run_id]))  # Missing run: 404 even without events.jsonl
        return {"id": run_id, "sampled_at": sampled, "sampled_at_utc": utc(sampled), **self._events(run_id, tail)}

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
                if name in TOP_LEVEL_FILES and not excluded_name(name):
                    if len(files) >= FILE_INDEX_MAX:
                        truncated = True  # One shared cap for every entry source.
                        break
                    files.append(([name], info))
                else:
                    skipped["excluded"] += 1
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
                          for path, info in files]}

    def read_file(self, run_id, path, tail=None):
        validate_run_id(run_id)
        components = validate_path(path)
        tail = _tail_limit(tail)
        sampled = time.time()
        read = self.reader.read(["runs", run_id, *components], limit=tail, mode="tail")
        return {"id": run_id, "source": "/".join(components), "size": read["size"], "tail_bytes": tail,
                "truncated": read["truncated"], "changed_while_reading": read["changed_while_reading"],
                "sampled_at": sampled, "sampled_at_utc": utc(sampled),
                "text": read["data"].decode("utf-8", errors="replace")}


def list_runs(state):
    with Observer(state) as observer:
        return observer.list_runs()


def run_detail(state, run_id):
    with Observer(state) as observer:
        return observer.run_detail(run_id)
