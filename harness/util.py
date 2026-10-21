from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path


class HarnessError(Exception):
    pass


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


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


def run_process(argv: list[str], cwd: Path, log: Path, *, stdin: str = "",
                timeout: int = 900, env=None, on_start=None) -> int:
    """No shell interpolation; terminate the owned process group on all exits."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.with_suffix(".stdout").open("w") as out, log.with_suffix(".stderr").open("w") as err:
        p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=out,
                             stderr=err, text=True, env=env, start_new_session=True)
        started = time.monotonic()
        try:
            if on_start:
                on_start(p.pid)
            data = stdin
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise HarnessError(f"进程超过 {timeout}s；日志：{log}")
                try:
                    p.communicate(input=data, timeout=min(15, remaining))
                    return p.returncode
                except subprocess.TimeoutExpired:
                    data = None
                    print(f"[harness] {argv[0]} 仍在运行；日志 {log}", file=sys.stderr)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            if p.stdin:
                p.stdin.close()


def tail(path: Path, size=4000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(0, 2)
        length = f.tell()
        f.seek(max(0, length - size))
        value = f.read().decode("utf-8", errors="replace")
    return ("[earlier output omitted; see log]\n" if length > size else "") + value
