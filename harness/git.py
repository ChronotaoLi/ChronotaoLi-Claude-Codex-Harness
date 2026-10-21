from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

from .util import HarnessError, write_json


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
    result = {"head": head(repo), "index": hashlib.sha256(git(repo, "ls-files", "--stage", "-z").encode()).hexdigest(),
              "files": {}}
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
        result["files"][name] = mode + ":" + hashlib.sha256(data).hexdigest()
    return result


def changed(repo: Path):
    tracked = git(repo, "diff", "--name-only", "--no-renames", "-z", "HEAD").split("\0")
    staged = git(repo, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD").split("\0")
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted((set(tracked) | set(staged) | set(untracked)) - {""})


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
    value = git(repo, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames", "HEAD")
    for name in git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if not name:
            continue
        p = subprocess.run(["git", "diff", "--no-index", "--binary", "--", "/dev/null", name],
                           cwd=repo, capture_output=True, text=True, timeout=60)
        if p.returncode not in (0, 1):
            raise HarnessError(p.stderr)
        value += p.stdout
    return value


def range_diff(repo: Path, base: str):
    """The whole committed result, independent of task boundaries."""
    return git(repo, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames", base, "HEAD", "--")


def archive_and_reset(repo: Path, expected_head: str, archive: Path, owned_root: Path):
    """Archive rejected edits before restoring an owned run worktree checkpoint."""
    repo = repo.resolve()
    if not repo.is_relative_to(owned_root.resolve()) or not (repo / ".git").is_file():
        raise HarnessError("只能重置 Harness 所有的隔离 worktree")
    if head(repo) != expected_head:
        raise HarnessError("重置前 HEAD 与检查点不同；保留现场")
    paths = changed(repo)
    # Refuse obstructed tracked descendants instead of following a replacement
    # symlink out of the worktree while archiving or creating such a link in it.
    for name in paths:
        for parent in (repo / name).parents:
            if parent == repo:
                break
            if parent.is_symlink():
                raise HarnessError(f"归档路径有符号链接祖先；保留现场，不重置：{name}")
    before = snapshot(repo)
    archive.mkdir(parents=True, exist_ok=False)
    (archive / "change.diff").write_text(diff(repo))
    (archive / "staged.diff").write_text(git(repo, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"))
    write_json(archive / "snapshot.json", before)
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    for name in paths:
        source, target = repo / name, archive / "files" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        elif source.is_file():
            shutil.copy2(source, target)
    if snapshot(repo) != before:
        raise HarnessError("归档期间文件发生变化；保留现场，不重置")
    git(repo, "reset", "--hard", expected_head)
    # Remove only the recorded, untracked files, never ignored files or arbitrary directories.
    for name in filter(None, untracked):
        path = repo / name
        if path.is_symlink() or path.is_file():
            path.unlink()
    if not clean(repo) or head(repo) != expected_head:
        raise HarnessError("重置未恢复到干净检查点；归档已保留")
    write_json(archive / "restored.json", {"head": expected_head, "paths": paths})
    return str(archive)


def accept(repo: Path, paths: list[str], run_id: str, task_id: str):
    # The worker does not own the acceptance index. Rebuild it from the reviewed
    # worktree so hidden staged content can never enter the runtime's commit.
    git(repo, "reset", "--mixed", "HEAD")
    if paths:
        git(repo, "add", "--", *paths)
    if git(repo, "diff", "--cached", "--name-only", "HEAD").strip():
        git(repo, "-c", "user.name=Personal Harness", "-c", "user.email=harness@localhost",
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m",
            f"harness: {task_id}\n\nHarness-Run: {run_id}\nHarness-Task: {task_id}")
    return head(repo)
