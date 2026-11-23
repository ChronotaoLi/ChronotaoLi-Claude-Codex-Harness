"""Record the identity of a verification checkout at creation."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from . import git
from .util import HarnessError


def directory_identity(path):
    """Read the directory's identity without following its last component."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise HarnessError(f"{path} 不是目录")
    return [info.st_dev, info.st_ino]


def build_entry(owner, root, worktree, identity, binding):
    return {"attempt": owner["attempt"], "round": owner["round"],
            "root": str(root), "worktree": str(worktree),
            "identity": identity, "binding": binding}


def entry_for(run, attempt, round):
    """Find a registration by attempt and round in an already validated ledger."""
    for entry in run["verification_checkouts"]:
        if (entry["attempt"], entry["round"]) == (attempt, round):
            return entry
    return None


def validate_checkout_entries(run):
    """Validate ledger records without consulting the live filesystem or Git."""
    entries = run.get("verification_checkouts")
    if not isinstance(entries, list):
        raise HarnessError("verification_checkouts 缺失或不是列表")
    history = run.get("final_history")
    if not isinstance(history, list):
        raise HarnessError("verification_checkouts 要求 final_history 为列表")
    owner_binding = run.get("worktree_binding")
    seen = set()
    for index, entry in enumerate(entries):
        field = f"verification_checkouts[{index}]"
        if not isinstance(entry, dict) or entry.keys() != {
                "attempt", "round", "root", "worktree", "identity", "binding"}:
            raise HarnessError(f"{field} 登记字段无效")
        attempt, round_number = entry["attempt"], entry["round"]
        if type(attempt) is not int or not 1 <= attempt <= len(history):
            raise HarnessError(f"{field}.attempt 无效")
        record = history[attempt - 1]
        if (not isinstance(record, dict) or type(record.get("attempt")) is not int
                or record["attempt"] != attempt or not isinstance(record.get("head"), str)):
            raise HarnessError(f"{field}.attempt 指向无效的整体验证尝试")
        rounds = record.get("verification_rounds")
        if (type(round_number) is not int or not isinstance(rounds, list)
                or not 1 <= round_number <= len(rounds)):
            raise HarnessError(f"{field}.round 无效")
        key = (attempt, round_number)
        if key in seen:
            raise HarnessError(f"{field} 的 attempt 和 round 重复")
        seen.add(key)

        root, worktree = entry["root"], entry["worktree"]
        if (not isinstance(root, str) or not os.path.isabs(root) or os.path.normpath(root) != root
                or not Path(root).name.startswith("harness-verify-")
                or Path(root).is_relative_to(run["repo"])):
            raise HarnessError(f"{field}.root 无效")
        if worktree != root + "/.harness/checkout" or worktree == run["worktree"]:
            raise HarnessError(f"{field}.worktree 无效")
        identity = entry["identity"]
        if not isinstance(identity, dict) or identity.keys() != {"root", "worktree", "git_dir"}:
            raise HarnessError(f"{field}.identity 字段无效")
        for name, value in identity.items():
            if (not isinstance(value, list) or len(value) != 2
                    or any(type(number) is not int or number < 0 for number in value)):
                raise HarnessError(f"{field}.identity.{name} 无效")

        binding = entry["binding"]
        if not isinstance(binding, dict) or binding.keys() != {
                "worktree", "git_dir", "common_dir", "branch", "gitfile_sha256",
                "backlink_sha256", "commondir_sha256", "detached_head"}:
            raise HarnessError(f"{field}.binding 字段无效")
        try:
            git._check_binding_record(binding)
        except HarnessError as error:
            raise HarnessError(f"{field}.binding 无效：{error}") from error
        if binding["branch"] is not None or binding["detached_head"] != record["head"]:
            raise HarnessError(f"{field}.binding 的 HEAD 与尝试不符")
        if binding["worktree"] != worktree:
            raise HarnessError(f"{field}.binding.worktree 与检出路径不符")
        if not isinstance(owner_binding, dict) or binding["common_dir"] != owner_binding.get("common_dir"):
            raise HarnessError(f"{field}.binding.common_dir 与 run 绑定不符")
        if Path(binding["git_dir"]).parent != Path(binding["common_dir"]) / "worktrees":
            raise HarnessError(f"{field}.binding.git_dir 无效")
