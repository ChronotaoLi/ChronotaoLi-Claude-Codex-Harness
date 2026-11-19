"""Exact commands: a command is {"cwd", "argv"}; the directory is part of its identity and grant."""
from __future__ import annotations

import hashlib
import json

from .git import protected_name
from .git_query import covers
from .util import HarnessError, fold_name


CWD_MAX_CHARS = 4096


def _validate_cwd(cwd, field):
    if not isinstance(cwd, str):
        raise HarnessError(f"{field} 必须为字符串")
    if len(cwd) > CWD_MAX_CHARS:
        raise HarnessError(f"{field} 超过 {CWD_MAX_CHARS} 个字符")
    if "\0" in cwd or "\\" in cwd:
        raise HarnessError(f"{field} 不能含 NUL 或反斜杠")
    try:
        cwd.encode("utf-8")
    except UnicodeEncodeError as error:
        # A surrogate can never name a directory, so no grant could ever match it.
        raise HarnessError(f"{field} 含无法编码的代理码点") from error
    if cwd == ".":
        return
    if cwd.startswith("/"):
        raise HarnessError(f"{field} 必须是工作树内的相对目录，不能是绝对路径：{cwd[:160]!r}")
    segments = cwd.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise HarnessError(f"{field} 不能有空段、. 或 .. 段（根目录写作 \".\"）：{cwd[:160]!r}")
    if any(not segment.strip() for segment in segments):
        raise HarnessError(f"{field} 的每段都必须含非空白字符：{cwd[:160]!r}")
    # Same refusal as delete_path: protected roots, and .git anywhere, in any spelling that may alias them.
    if protected_name(segments[0]) or ".git" in map(fold_name, segments):
        raise HarnessError(f"{field} 不能进入受保护目录（.git、.harness、.codex、.claude，不分大小写）：{cwd[:160]!r}")


def validate_command(value, field):
    """Rules 1-3 for one command; the error names the offending field path."""
    if not isinstance(value, dict) or set(value) != {"cwd", "argv"}:
        raise HarnessError(f"{field} 必须是只含 cwd 与 argv 两个键的对象")
    argv = value["argv"]
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(item, str) and "\0" not in item for item in argv)
            or not argv[0].strip()):
        raise HarnessError(f"{field}.argv 必须是非空 argv 数组：每项是不含 NUL 的字符串，首项不是空白")
    _validate_cwd(value["cwd"], f"{field}.cwd")


def validate_commands(value, field):
    if not isinstance(value, list):
        raise HarnessError(f"{field} 必须是命令对象 {{cwd, argv}} 的列表")
    for index, command in enumerate(value):
        validate_command(command, f"{field}[{index}]")


def valid_command(value) -> bool:
    try:
        validate_command(value, "command")
    except HarnessError:
        return False
    return True


def command_id(command) -> str:
    """Canonical JSON digest of the pair: one argv in two directories has two IDs."""
    canonical = json.dumps({"argv": command["argv"], "cwd": command["cwd"]},
                           ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def command_key(command):
    return command["cwd"], tuple(command["argv"])


def granted(command, config_data) -> bool:
    """Exact (cwd, argv) membership in verification.commands plus permissions.worker_commands.

    Strings compare as written, without case or Unicode folding; setup_commands grant nothing.
    """
    key = command_key(command)
    return any(command_key(grant) == key for grant in (
        *config_data["verification"]["commands"], *config_data["permissions"]["worker_commands"]))


def git_query_covered(command) -> bool:
    # harness.git_query runs only at the worktree root.
    return command["cwd"] == "." and covers(command["argv"])


def available_through(command, config_data):
    """How a worker can run ``command``: "grant", else "builtin_git_query", else None (missing).

    The one availability verdict for plan prerequisites, continuation requests and environment checks.
    Built-in coverage is capability, not a grant, so callers that report it keep the two apart.
    """
    if granted(command, config_data):
        return "grant"
    return "builtin_git_query" if git_query_covered(command) else None
