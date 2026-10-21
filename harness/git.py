from __future__ import annotations

import hashlib
import os
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

from .util import HarnessError


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise HarnessError(f"git {' '.join(args[:3])} failed: {p.stderr.strip()}")
    return p.stdout


def repository(path: Path):
    return Path(git(path, "rev-parse", "--show-toplevel").strip()).resolve()


def head(repo: Path):
    return git(repo, "rev-parse", "HEAD").strip()


def ignore_state(repo: Path):
    """Local Git exclude avoids requiring changes to a target project's tracked files."""
    common = Path(git(repo, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = repo / common
    path = common / "info" / "exclude"
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_text() if path.exists() else ""
    if "/.harness/" not in previous.splitlines():
        path.write_text(previous.rstrip() + "\n/.harness/\n")


def clean(repo: Path):
    return not git(repo, "status", "--porcelain").strip()


def snapshot(repo: Path):
    result = {"HEAD": head(repo)}
    names = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    for name in sorted(set(names) - {""}):
        path = repo / name
        if path.is_symlink():
            data = os.readlink(path).encode()
            mode = "link"
        elif path.is_file():
            data = path.read_bytes()
            mode = str(path.stat().st_mode & 0o777)
        elif path.is_dir():
            raise HarnessError(f"v0.1 暂不支持 submodule：{name}")
        else:
            data, mode = b"", "deleted"
        result[name] = mode + ":" + hashlib.sha256(data).hexdigest()
    return result


def changed(repo: Path):
    tracked = git(repo, "diff", "--name-only", "--no-renames", "-z", "HEAD").split("\0")
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted((set(tracked) | set(untracked)) - {""})


def check_scope(repo: Path, allowed: list[str]):
    paths = changed(repo)
    forbidden = (".git/", ".harness/", ".codex/", ".claude/")
    for path in paths:
        if path == ".git" or path.startswith(forbidden):
            raise HarnessError(f"任务修改了受保护路径：{path}")
        if not any(fnmatchcase(path, pattern) for pattern in allowed):
            raise HarnessError(f"任务越界修改：{path}；允许范围：{allowed}")
    return paths


def diff(repo: Path):
    value = git(repo, "diff", "--binary", "--no-ext-diff", "--no-renames", "HEAD")
    for name in git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if not name:
            continue
        p = subprocess.run(["git", "diff", "--no-index", "--binary", "--", "/dev/null", name],
                           cwd=repo, capture_output=True, text=True, timeout=60)
        if p.returncode not in (0, 1):
            raise HarnessError(p.stderr)
        value += p.stdout
    return value


def accept(repo: Path, paths: list[str], run_id: str, task_id: str):
    if paths:
        git(repo, "add", "--", *paths)
        git(repo, "-c", "user.name=Personal Harness", "-c", "user.email=harness@localhost",
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m",
            f"harness: {task_id}\n\nHarness-Run: {run_id}\nHarness-Task: {task_id}")
    return head(repo)
