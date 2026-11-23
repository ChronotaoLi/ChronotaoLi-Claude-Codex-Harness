from __future__ import annotations

import contextlib
import errno
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path

from . import stops


PROCESS_HEARTBEAT_SECONDS = 60
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_STDERR_JSON_ESCAPES = {code: f"\\u{code:04x}" for code in range(0x7f, 0xa0)}


class HarnessError(Exception):
    """A rejected step; ``stop`` is the stop value it carries (stops.make), or None."""

    stop = None

    def __init__(self, *args, stop=None):
        super().__init__(*args)
        if stop is not None:
            self.stop = stop


class EvidenceError(HarnessError):
    """A runtime-written artifact is missing, replaced or changed: never a model-output verdict.

    Judgment code that catches HarnessError broadly lets this class through.
    """


class ProcessStartError(HarnessError):
    """The process failed to start synchronously."""


class ProgramStartError(ProcessStartError):
    """The child could not execute the requested program."""


class CommandDirectoryError(ProcessStartError):
    """A command's directory could not be opened or entered below its root; nothing started."""


class ProcessTimeout(HarnessError):
    def __init__(self, timeout: int | float, log: Path):
        self.timeout = timeout
        self.log = log
        super().__init__(f"进程超过 {timeout}s；日志：{log}")


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def fold_name(segment: str) -> str:
    """A path segment's comparison key: segments that may name one directory entry fold equal.

    Strict on purpose, for any filesystem Harness may run on: drops format
    characters some filesystems ignore, then applies NFKC, case folding and
    strips trailing dots and spaces. Only compare with it; paths keep their
    original spelling everywhere else.
    """
    kept = "".join(char for char in segment if unicodedata.category(char) != "Cf")
    return unicodedata.normalize("NFKC", kept).casefold().rstrip(". ")


def one_line(value) -> str:
    """Escape non-printable characters and backslashes as repr() writes them."""
    return "".join(char if char.isprintable() and char != "\\" else repr(char)[1:-1] for char in str(value))


def display_text(text) -> str:
    """Render a diagnostic with escaped controls and indented continuation lines."""
    lines = str(text).split("\n")
    if len(lines) > 1 and not lines[-1]:
        lines.pop()
    return "\n".join([one_line(lines[0]), *("  " + one_line(line) for line in lines[1:])])


def escape_json_controls(text: str) -> str:
    """Render serialized JSON for stderr, keeping the Unicode line separators literal."""
    # JSON already escapes C0 controls. DEL and C1 can still be literal in strings.
    return text.translate(_STDERR_JSON_ESCAPES)


def dump(value) -> str:
    try:
        # Never write what strict_json_loads would refuse to read back.
        text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    except ValueError as error:
        raise HarnessError(f"拒绝写出非有限数：严格 JSON 无法读回（{error}）") from error
    # Git paths may contain surrogateescaped filesystem bytes. Preserve their
    # exact identity in JSON while keeping ordinary Unicode readable.
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def json_bytes(value) -> bytes:
    return (dump(value) + "\n").encode("utf-8")


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("wb") as f:
        f.write(json_bytes(value))
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(name):
    raise ValueError(f"non-finite JSON number: {name}")


def _finite_float(text):
    # An ordinary literal such as 1e999 overflows to infinity, at any depth.
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"non-finite JSON number: {text}")
    return value


def strict_json_loads(raw):
    """Decode model or provider JSON exactly: no duplicate keys, no non-finite numbers.

    NaN and Infinity literals and float literals that overflow are all refused.
    Raises ValueError (including JSONDecodeError), UnicodeError or RecursionError.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")
    return json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant,
                      parse_float=_finite_float)


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Lack of permission is not evidence that an old group has exited.
        return True


# pid_t is a 32-bit signed int where Harness runs: os.killpg cannot even take a larger group ID.
PGID_MAX = 2**31 - 1


def observed_alive(observe, pgid):
    """``observe(pgid)``, or None when looking failed: os.killpg's unexpected errors are no evidence of an exit."""
    try:
        return observe(pgid)
    except (OSError, OverflowError):
        return None


def _dispatch_pgid(entry):
    if not isinstance(entry, dict):
        raise HarnessError("运行台账 dispatch 无效")
    pgid = entry.get("pgid")
    if pgid is not None and (type(pgid) is not int or pgid <= 0):
        raise HarnessError("运行台账进程组 ID 无效")
    return pgid


@stops.reads_evidence
def _verification_record_parts(entry):
    if entry.get("role") != "verification":
        if "process_record" in entry:
            raise HarnessError("非验证 dispatch 不得登记进程记录")
        return None
    record = entry.get("process_record")
    if type(record) is not str or len(record) > 4096 or "\\" in record or "\0" in record:
        raise HarnessError("验证进程记录路径无效")
    parts = tuple(record.split("/"))
    if (parts[0] != "verification" or any(part in ("", ".", "..") for part in parts)
            or re.fullmatch(r"check-(0|[1-9][0-9]*)\.outcome\.json", parts[-1]) is None):
        raise HarnessError("验证进程记录路径无效")
    return parts


@stops.reads_evidence
def validate_command_directories(dispatches, run_dir):
    """Reject ledger paths that could redirect child-process supervision."""
    from .state_files import StateFiles  # state_files imports this module
    root = Path(run_dir)
    if not root.is_absolute() or root.resolve() != root:
        raise HarnessError("运行目录不是规范绝对路径")
    files = StateFiles(root)
    for entry in dispatches:
        if not isinstance(entry, dict):
            raise HarnessError("运行台账 dispatch 无效")
        _verification_record_parts(entry)
        directory = entry.get("command_directory")
        if directory is None:
            if "command_directory" in entry:
                raise HarnessError("命令目录无效")
            continue
        ident = entry.get("id")
        if (not isinstance(ident, str) or not ident or ident in (".", "..")
                or "/" in ident or "\\" in ident or "\0" in ident
                or not isinstance(directory, str)):
            raise HarnessError("命令派发身份或目录无效")
        # No level below the run directory is followed; a lookup that fails is local I/O, not a wrong directory.
        if (directory != str(root / "dispatches" / ident / "commands")
                or files.probe(("dispatches", ident, "commands"), expect="directory").or_local_io().state != "present"):
            raise HarnessError("命令目录不属于该 dispatch")
        confirmations = entry.get("command_exit_confirmations", {})
        if not isinstance(confirmations, dict):
            raise HarnessError("命令退出确认无效")
        for key, value in confirmations.items():
            if (not isinstance(key, str) or not key.startswith(ident + ":command:")
                    or not isinstance(value, dict) or "journal_digest" not in value):
                raise HarnessError("命令退出确认身份或摘要无效")
            digest = value["journal_digest"]
            if digest is not None and (not isinstance(digest, str) or len(digest) != 64
                                       or any(c not in "0123456789abcdef" for c in digest)):
                raise HarnessError("命令退出确认身份或摘要无效")


def _cli_arguments(option):
    from . import stops
    return stops.make("invalid_input", "cli_arguments", data={"option": option})


def _exit_unconfirmed(entry):
    from . import stops
    ident = entry.get("id")
    return stops.make("process_unconfirmed", "dispatch_exit", data={"returned_unsealed": False},
                      source={"dispatch_id": ident} if isinstance(ident, str) else {})


def read_process_exit(files, components, *, owner=None):
    """Return (not started, pgid) from a usable supervisor record, or (False, None)."""
    outcome = files.read(components, limit=16 * 1024 * 1024).or_local_io()
    if outcome.state != "read":
        return False, None
    try:
        record = strict_json_loads(outcome.data)
    except (ValueError, RecursionError):
        return False, None
    if not isinstance(record, dict) or (owner is not None and record.get("owner") != owner):
        return False, None
    pgid, pid = record.get("pgid"), record.get("pid")
    if (record.get("started") is False and record.get("startup_state") == "not_started"
            and "pid" in record and pid is None and "pgid" in record and pgid is None
            and record.get("exit_confirmed") is True):
        return True, None
    if (record.get("started") is True and record.get("startup_state") == "started"
            and type(pgid) is int and 0 < pgid <= PGID_MAX and type(pid) is int and pid == pgid):
        return False, pgid
    return False, None


@stops.reads_evidence
def _dispatch_process_exit(entry, directory, files, verification_record):
    ident = entry.get("id")
    if (not isinstance(ident, str) or ident in ("", ".", "..")
            or any(char in ident for char in ("/", "\\", "\0"))):
        raise HarnessError("运行台账 dispatch 无效")
    if not files.opened:
        root = files.entries()
        if root.step.stage == "resolve":
            root.or_local_io()
    parts = files.recorded(os.fspath(directory))
    if parts is None or len(parts) != 2 or parts[0] != "runs" or "\\" in parts[1]:
        raise HarnessError("运行目录不属于状态目录")
    if verification_record is not None:
        return read_process_exit(files, (*parts, *verification_record), owner=ident)
    return read_process_exit(files, (*parts, "dispatches", ident, "process.outcome.json"))


def _check_dispatch_records(dispatches, state, needs_files, check):
    """Check in dispatch order, holding a shared root from the first evidence read onward."""
    from .state_files import StateFiles, holding

    pending = ((entry, _dispatch_pgid(entry)) for entry in dispatches)
    for entry, pgid in pending:
        if not needs_files(entry, pgid):
            check(entry, pgid, None)
            continue

        def check_remaining(files):
            check(entry, pgid, files)
            for next_entry, next_pgid in pending:
                check(next_entry, next_pgid, files)

        holding(StateFiles.held(state), check_remaining)
        return


def ensure_dispatches_exited(dispatches, *, state, directory, observe=group_alive):
    """Check exits without changing ledger or journal, using one held state root for evidence."""
    from .command_server import ensure_command_processes_exited

    exited = []

    def needs_record(entry, pgid):
        return (entry.get("exit_confirmed") is not True and pgid is None
                and entry.get("process_state") != "prepared")

    def check(entry, pgid, files):
        commands = entry.get("command_directory")
        if commands is not None:
            ensure_command_processes_exited(commands, entry, state=files, observe=observe)
        verification_record = _verification_record_parts(entry)
        if entry.get("exit_confirmed") is True:
            return
        from_record = needs_record(entry, pgid)
        if from_record:
            not_started, pgid = _dispatch_process_exit(entry, directory, files, verification_record)
            if not_started:
                exited.append(entry)
                return
        hint = ("核实旧进程已退出后，可用 --confirm-exited <dispatch_id> 留痕确认；"
                "已完成或已放弃的 run 使用 cleanup-run 的同名选项")
        if pgid is not None:
            if from_record:
                remaining = observed_alive(observe, pgid)
                if remaining is not False:
                    raise HarnessError(
                        f"无法确认旧 dispatch {entry['id']} 的进程组 {pgid} 已退出；{hint}",
                        stop=_exit_unconfirmed(entry))
            elif observe(pgid):
                raise HarnessError(f"旧 dispatch {entry.get('id', '<unknown>')} 的进程组仍存在；{hint}",
                                   stop=_exit_unconfirmed(entry))
            exited.append(entry)
        elif entry.get("process_state") != "prepared":
            raise HarnessError(f"无法确认旧 dispatch {entry.get('id', '<unknown>')} 已退出；{hint}",
                               stop=_exit_unconfirmed(entry))

    _check_dispatch_records(dispatches, state,
                            lambda entry, pgid: entry.get("command_directory") is not None or needs_record(entry, pgid),
                            check)
    return exited


def confirm_dispatches_exited(dispatches, identifiers, *, state, audit, save, observe=group_alive):
    """Audit explicit assertions before saving them; never send process signals. Journals are read from ``state``."""
    identifiers = list(dict.fromkeys(identifiers))
    if not identifiers:
        return
    entries = {}
    for entry in dispatches:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise HarnessError("运行台账 dispatch 身份无效")
        if entry["id"] in entries:
            raise HarnessError("运行台账 dispatch ID 重复；不能确认退出")
        entries[entry["id"]] = entry
    unknown = set(identifiers) - entries.keys()
    if unknown:
        raise HarnessError(f"未知 dispatch ID：{sorted(unknown)}", stop=_cli_arguments("--confirm-exited"))
    # Read and validate every selected journal before audit or mutation.
    from .command_server import command_exit_settled, command_processes
    children = {}

    def read_journals(entry, _pgid, files):
        directory = entry.get("command_directory") or None
        children[entry["id"]] = command_processes(directory, state=files) if directory is not None else []
        confirmations = entry.get("command_exit_confirmations", {})
        if (not isinstance(confirmations, dict)
                or any(not isinstance(value, dict) for value in confirmations.values())):
            raise HarnessError("命令退出确认无效")

    _check_dispatch_records((entries[ident] for ident in identifiers), state,
                            lambda entry, _pgid: bool(entry.get("command_directory")), read_journals)
    # The whole batch to write, decided before the first audit; the gate's own rules say what is settled.
    pending = {ident: [child for child in children[ident]
                       if not command_exit_settled(child, entries[ident].get("command_exit_confirmations", {}))]
               for ident in identifiers}

    def observation(pgid):
        observed = {"pgid": pgid, "group_alive": observed_alive(observe, pgid) if pgid is not None else None}
        if pgid is not None:
            try:
                process = subprocess.run(
                    ["ps", "-p", str(pgid), "-o", "pid=,ppid=,pgid=,lstart=,comm="],
                    capture_output=True, text=True, errors="replace", timeout=5, check=False)
                observed.update(leader=process.stdout.strip(), query_exit_code=process.returncode)
            except (OSError, subprocess.TimeoutExpired) as error:
                observed["query_error"] = str(error)
        return observed

    # Every confirmation of the batch, its observation included, exists before the first audit: whatever fails
    # while observing leaves before anything is written, and writing calls only audit and save.
    writes = []
    for ident in identifiers:
        for child in pending[ident]:
            confirmation = {"time": time.time(), "journal_digest": child["journal_digest"],
                            "observed": observation(child["pgid"])}
            writes.append((ident, child["id"], child["journal_state"], confirmation))
        if entries[ident].get("exit_confirmed") is not True:
            confirmation = {"time": time.time(), "observed": observation(entries[ident].get("pgid"))}
            writes.append((ident, None, None, confirmation))
    for ident, call, journal_state, confirmation in writes:
        entry = entries[ident]
        if call is not None:
            audit("command_exit_confirmed_by_operator", dispatch_id=ident, command_id=call,
                  journal_state=journal_state, **confirmation)
            entry.setdefault("command_exit_confirmations", {})[call] = confirmation
        else:
            audit("dispatch_exit_confirmed_by_operator", dispatch_id=ident, **confirmation)
            entry.update(exit_confirmed=True, operator_exit_confirmation=confirmation)
        save()


def append_event(path, run_id, event, **fields):
    record = {"time": time.time(), "event": event, "run_id": run_id, **fields}
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


# Failures that describe the directory itself; other errors (descriptors, devices) stay local I/O.
_DIRECTORY_ERRNOS = frozenset((errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EACCES, errno.EPERM, errno.ENAMETOOLONG))
_PROGRAM_ERRNOS = _DIRECTORY_ERRNOS | {errno.ENOEXEC}


def _directory_failure(name, parent, error):
    if isinstance(error, ValueError):
        return "无法编码为路径"
    try:
        mode = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return "不存在"
    except PermissionError:
        return "无权限"
    except OSError:
        return str(error)
    if stat.S_ISLNK(mode):
        return "是符号链接"
    if not stat.S_ISDIR(mode):
        return "不是目录"
    return "无权限" if isinstance(error, PermissionError) else str(error)


class Descriptors:
    """Descriptors a caller owns from the step that opens each one until its cleanup closes them.

    Get one from with_descriptors, which closes it. Opening and registering are one step,
    so no descriptor is ever held by a return value alone. Only that step and closing
    defer signals; whatever runs between them (reads, later levels of a walk, Git) stays
    interruptible. Each descriptor leaves the holder before it is closed, so closing
    again never reaches a number reused since; nothing closes them on collection.
    """

    def __init__(self):
        self._held = []

    def open(self, path, flags, *, dir_fd=None) -> int:
        return self._take(os.open, path, flags, dir_fd=dir_fd)

    def dup(self, descriptor) -> int:
        return self._take(os.dup, descriptor)

    def _take(self, opener, *args, **kwargs):
        from .process_runtime import defer_cleanup_signals
        # Not inherit: a caller handling another exception must still see a request deferred here.
        with defer_cleanup_signals(inherit=False):
            descriptor = opener(*args, **kwargs)
            self._held.append(descriptor)
        return descriptor

    def close(self, descriptor):
        """Close one held descriptor now; it is dropped first, so a failed close is never retried."""
        from .process_runtime import defer_cleanup_signals
        with defer_cleanup_signals(inherit=False):
            self._held.remove(descriptor)
            os.close(descriptor)

    def close_all(self):
        """Close what is still held, each once; a failed close never replaces the error in flight."""
        from .process_runtime import defer_cleanup_signals
        if not self._held:
            return
        with defer_cleanup_signals(inherit=False):
            while self._held:
                with contextlib.suppress(OSError):
                    os.close(self._held.pop())


def with_descriptors(use):
    """Return ``use(held)`` for a new holder, closing it in two nested cleanups.

    A signal can land at any bytecode, a cleanup's entry before it defers signals
    included; the outer cleanup then closes what the inner one did not. Only two
    signals, one on each cleanup's entry, can leave descriptors open.
    """
    held = Descriptors()
    try:
        try:
            return use(held)
        finally:
            held.close_all()
    finally:
        held.close_all()


def open_command_directory(held: Descriptors, root_fd: int, relative: str) -> int:
    """Open a worktree-relative directory level by level below an open root, following no link.

    Each level is registered in ``held`` as it opens and the level above it is closed; the
    returned descriptor stays ``held``'s ("." gives a dup of the root). root_fd is never closed.
    """
    # Entry validation already refused these; a stale or hand-made value must still not escape.
    if relative != "." and (not isinstance(relative, str) or relative.startswith("/")
                            or any(segment in ("", ".", "..") for segment in relative.split("/"))):
        raise CommandDirectoryError(f"命令目录 {relative!r} 无效：不能是绝对路径，不能有空段、. 或 .. 段")
    if relative == ".":
        return held.dup(root_fd)
    segments = relative.split("/")
    current = root_fd
    for index, segment in enumerate(segments):
        try:
            following = held.open(segment, DIRECTORY_FLAGS, dir_fd=current)
        except (OSError, ValueError) as error:
            if isinstance(error, OSError) and error.errno not in _DIRECTORY_ERRNOS:
                raise
            shown = "/".join(segments[:index + 1])
            raise CommandDirectoryError(
                f"命令目录 {relative!r} 无法打开：{shown!r} {_directory_failure(segment, current, error)}") from error
        if current != root_fd:
            held.close(current)
        current = following
    return current


def run_process(argv: list[str], cwd: Path, log: Path, *, stdin: str = "",
                timeout: int | float = 900, env=None, on_start=None, on_outcome=None,
                execution_deadline=None, cleanup_deadline=None, relative_cwd=None, root_fd=None,
                record_owner=None) -> int:
    """No shell interpolation; terminate the owned process group on all exits.

    With relative_cwd the child starts in that directory, opened from root_fd (the
    caller's verified root, which stays open and the caller's); cwd is then unused.
    """
    from .process_runtime import ProcessSupervisor, defer_cleanup_signals
    if (relative_cwd is None) != (root_fd is None):
        raise ValueError("relative_cwd and root_fd go together")
    execution_deadline = min(time.monotonic() + timeout, execution_deadline if execution_deadline is not None else float("inf"))
    log.parent.mkdir(parents=True, exist_ok=True)
    supervision = ProcessSupervisor(log, on_outcome=on_outcome, cleanup_deadline=cleanup_deadline,
                                    owner=record_owner)
    # Its exit is handed the exception leaving this block, never one the caller is handling.
    with supervision:
        with log.with_suffix(".stdout").open("w", encoding="utf-8") as out, \
                log.with_suffix(".stderr").open("w", encoding="utf-8", errors="backslashreplace") as err, \
                tempfile.TemporaryFile(dir=log.parent) as source:
            # A regular file delivers the whole input even if startup spans several
            # heartbeat windows. Retrying communicate(input=None) can strand a
            # partially written pipe on Python 3.11.
            source.write(stdin.encode("utf-8"))
            source.seek(0)
            if time.monotonic() >= execution_deadline:
                raise ProcessTimeout(timeout, log)

            def start(held):
                directory = None
                if relative_cwd is not None:
                    try:
                        directory = open_command_directory(held, root_fd, relative_cwd)
                    except CommandDirectoryError as e:
                        err.write(f"{e}\n")
                        raise
                    # Opening may have blocked; nothing starts after the deadline.
                    if time.monotonic() >= execution_deadline:
                        raise ProcessTimeout(timeout, log)
                # The child enters the opened directory itself, so no path is resolved
                # again; preexec_fn assumes this process is single-threaded.
                launch = {"cwd": cwd} if directory is None else {
                    "cwd": None, "preexec_fn": lambda fd=directory: os.fchdir(fd)}
                # Retain cancellation until the supervisor owns the new process.
                # Popen can have created its child before returning to this frame.
                # Not inherit: an exception the caller is handling must not hide the request.
                with defer_cleanup_signals(inherit=False):
                    supervision.begin_launch()
                    try:
                        p = subprocess.Popen(argv, stdin=source, stdout=out, stderr=err, text=True,
                                             env=env, start_new_session=True, **launch)
                    except subprocess.SubprocessError as e:
                        if directory is None:
                            raise
                        # preexec_fn failed before exec; the child reports no errno.
                        message = f"无法进入命令目录 {relative_cwd!r}（fchdir 失败，例如没有搜索权限）：{e}；日志：{log}"
                        err.write(message + "\n")
                        raise CommandDirectoryError(message) from e
                    except (OSError, ValueError) as e:
                        message = f"无法启动进程 {argv[0] if argv else '<empty argv>'}：{e}；日志：{log}"
                        err.write(message + "\n")
                        # Exec errors name the requested program; local startup faults need not.
                        if isinstance(e, OSError) and e.errno in _PROGRAM_ERRNOS and argv and e.filename == argv[0]:
                            raise ProgramStartError(message) from e
                        raise ProcessStartError(message) from e
                    # Ownership first: a failing close below must not leave the child unsupervised.
                    supervision.started(p)
                    if directory is not None:
                        held.close(directory)
                return p

            # A start that failed or was interrupted leaves its directory to the holder's cleanup.
            p = with_descriptors(start)
            started = time.monotonic()
            if on_start:
                on_start(p.pid)
            while True:
                remaining = execution_deadline - time.monotonic()
                if remaining <= 0:
                    raise ProcessTimeout(timeout, log)
                try:
                    p.wait(timeout=min(PROCESS_HEARTBEAT_SECONDS, remaining))
                    return p.returncode
                except subprocess.TimeoutExpired:
                    print(f"[harness] {one_line(argv[0])} 仍在运行；日志 {one_line(log)}", file=sys.stderr)


def tail(path: Path, size=4000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as f:
        return tail_text(f, size)


def tail_text(f, size=4000) -> str:
    f.seek(0, 2)
    length = f.tell()
    f.seek(max(0, length - size))
    # At most size bytes: the file can grow after the seek.
    value = f.read(size).decode("utf-8", errors="replace")
    return ("[earlier output omitted; see log]\n" if length > size else "") + value
