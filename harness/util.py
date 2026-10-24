from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


PROCESS_HEARTBEAT_SECONDS = 60


class HarnessError(Exception):
    pass


class ProcessStartError(HarnessError):
    """Popen rejected startup synchronously; no child process was created."""


class ProcessTimeout(HarnessError):
    def __init__(self, timeout: int | float, log: Path):
        self.timeout = timeout
        self.log = log
        super().__init__(f"进程超过 {timeout}s；日志：{log}")


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def dump(value) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    # Git paths may contain surrogateescaped filesystem bytes. Preserve their
    # exact identity in JSON while keeping ordinary Unicode readable.
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        f.write(dump(value) + "\n")
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@contextlib.contextmanager
def lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise HarnessError(f"已有进程持有运行锁：{path}") from e
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Lack of permission is not evidence that an old group has exited.
        return True


def _dispatch_pgid(entry):
    if not isinstance(entry, dict):
        raise HarnessError("运行台账 dispatch 无效")
    pgid = entry.get("pgid")
    if pgid is not None and (type(pgid) is not int or pgid <= 0):
        raise HarnessError("运行台账进程组 ID 无效")
    return pgid


def validate_command_directories(dispatches, run_dir):
    """Reject ledger paths that could redirect child-process supervision."""
    root = Path(run_dir)
    if not root.is_absolute() or root.resolve() != root:
        raise HarnessError("运行目录不是规范绝对路径")
    for entry in dispatches:
        if not isinstance(entry, dict):
            raise HarnessError("运行台账 dispatch 无效")
        directory = entry.get("command_directory")
        if directory is None:
            if "command_directory" in entry:
                raise HarnessError("命令目录无效")
            continue
        ident = entry.get("id")
        if (not isinstance(ident, str) or not ident or ident in (".", "..")
                or "/" in ident or "\\" in ident
                or not isinstance(directory, str)):
            raise HarnessError("命令派发身份或目录无效")
        expected = root / "dispatches" / ident / "commands"
        path = Path(directory)
        if (directory != str(expected) or path.resolve() != path
                or not path.is_dir()
                or any(part.is_symlink() for part in (path, *path.parents))):
            raise HarnessError("命令目录不属于该 dispatch")
        confirmations = entry.get("command_exit_confirmations", {})
        if not isinstance(confirmations, dict):
            raise HarnessError("命令退出确认无效")
        for key, value in confirmations.items():
            if (not isinstance(key, str) or not key.startswith(ident + ":command:")
                    or not isinstance(value, dict) or not isinstance(value.get("journal_digest"), str)
                    or len(value["journal_digest"]) != 64
                    or any(c not in "0123456789abcdef" for c in value["journal_digest"])):
                raise HarnessError("命令退出确认身份或摘要无效")


def ensure_dispatches_exited(dispatches, *, observe=group_alive):
    """Use the same conservative exit gate for execution and cleanup.

    Only a saved preparation state proves that startup has not been attempted.
    An absent pgid after startup may mean the controller died between Popen and
    its callback, including for older ledgers without process_state.
    """
    exited = []
    for entry in dispatches:
        pgid = _dispatch_pgid(entry)
        if entry.get("command_directory") is not None:
            from .command_server import ensure_command_processes_exited
            ensure_command_processes_exited(entry["command_directory"], entry, observe=observe)
        if entry.get("exit_confirmed") is True:
            continue
        hint = ("核实旧进程已退出后，可用 --confirm-exited <dispatch_id> 留痕确认；"
                "已完成或已放弃的 run 使用 cleanup-verification 的同名选项")
        if pgid is not None:
            if observe(pgid):
                raise HarnessError(f"旧 dispatch {entry.get('id', '<unknown>')} 的进程组仍存在；{hint}")
            exited.append(entry)
        elif entry.get("process_state") != "prepared":
            raise HarnessError(f"无法确认旧 dispatch {entry.get('id', '<unknown>')} 已退出；{hint}")
    return exited


def confirm_dispatches_exited(dispatches, identifiers, *, audit, save, observe=group_alive):
    """Audit explicit assertions before saving them; never send process signals."""
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
        raise HarnessError(f"未知 dispatch ID：{sorted(unknown)}")
    # Read and validate every selected journal before audit or mutation.
    from .command_server import command_processes
    children = {}
    for ident in identifiers:
        entry = entries[ident]
        _dispatch_pgid(entry)
        children[ident] = command_processes(entry["command_directory"]) if entry.get("command_directory") else []
        confirmations = entry.get("command_exit_confirmations", {})
        if (not isinstance(confirmations, dict)
                or any(not isinstance(value, dict) for value in confirmations.values())):
            raise HarnessError("命令退出确认无效")

    def observation(pgid):
        observed = {"pgid": pgid, "group_alive": observe(pgid) if pgid is not None else None}
        if pgid is not None:
            try:
                process = subprocess.run(
                    ["ps", "-p", str(pgid), "-o", "pid=,ppid=,pgid=,lstart=,comm="],
                    capture_output=True, text=True, errors="replace", timeout=5, check=False)
                observed.update(leader=process.stdout.strip(), query_exit_code=process.returncode)
            except (OSError, subprocess.TimeoutExpired) as error:
                observed["query_error"] = str(error)
        return observed

    for ident in identifiers:
        entry = entries[ident]
        for child in children[ident]:
            previous = entry.get("command_exit_confirmations", {}).get(child["id"], {})
            if child["exit_confirmed"] or previous.get("journal_digest") == child["journal_digest"]:
                continue
            confirmation = {"time": time.time(), "journal_digest": child["journal_digest"],
                            "observed": observation(child["pgid"])}
            audit("command_exit_confirmed_by_operator", dispatch_id=ident,
                  command_id=child["id"], **confirmation)
            entry.setdefault("command_exit_confirmations", {})[child["id"]] = confirmation
            save()
        if entry.get("exit_confirmed") is True:
            continue
        confirmation = {"time": time.time(), "observed": observation(entry.get("pgid"))}
        audit("dispatch_exit_confirmed_by_operator", dispatch_id=ident, **confirmation)
        entry.update(exit_confirmed=True, operator_exit_confirmation=confirmation)
        save()


def append_event(path, run_id, event, **fields):
    record = {"time": time.time(), "event": event, "run_id": run_id, **fields}
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


def run_process(argv: list[str], cwd: Path, log: Path, *, stdin: str = "",
                timeout: int | float = 900, env=None, on_start=None) -> int:
    """No shell interpolation; terminate the owned process group on all exits."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.with_suffix(".stdout").open("w", encoding="utf-8") as out, \
            log.with_suffix(".stderr").open("w", encoding="utf-8", errors="backslashreplace") as err, \
            tempfile.TemporaryFile(dir=log.parent) as source:
        # A regular file delivers the whole input even if startup spans several
        # heartbeat windows. Retrying communicate(input=None) can strand a
        # partially written pipe on Python 3.11.
        source.write(stdin.encode("utf-8"))
        source.seek(0)
        p = None
        try:
            try:
                p = subprocess.Popen(argv, cwd=cwd, stdin=source, stdout=out,
                                     stderr=err, text=True, env=env, start_new_session=True)
            except (OSError, ValueError) as e:
                message = f"无法启动进程 {argv[0] if argv else '<empty argv>'}：{e}；日志：{log}"
                err.write(message + "\n")
                raise ProcessStartError(message) from e
            started = time.monotonic()
            if on_start:
                on_start(p.pid)
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise ProcessTimeout(timeout, log)
                try:
                    p.wait(timeout=min(PROCESS_HEARTBEAT_SECONDS, remaining))
                    return p.returncode
                except subprocess.TimeoutExpired:
                    print(f"[harness] {argv[0]} 仍在运行；日志 {log}", file=sys.stderr)
        finally:
            if p is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGKILL)
                p.wait()


def tail(path: Path, size=4000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(0, 2)
        length = f.tell()
        f.seek(max(0, length - size))
        value = f.read().decode("utf-8", errors="replace")
    return ("[earlier output omitted; see log]\n" if length > size else "") + value
