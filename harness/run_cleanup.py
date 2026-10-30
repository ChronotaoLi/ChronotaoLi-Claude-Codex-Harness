"""Explicit removal of a terminal run's clean worktree, retaining its branch."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from . import git
from .cleanup import _canonical, _regular_bytes, _registered
from .path_evidence import validate_dispatch_indexes
from .util import (HarnessError, append_event, confirm_dispatches_exited,
                   ensure_dispatches_exited, group_alive, lock,
                   validate_command_directories, write_json)


def cleanup_run(repo: Path, ident: str, *, remove=False, confirm_exited=()):
    """Preview or remove one completed/abandoned worktree without changing status.

    A durable receipt records intent before removal. Missing paths are accepted
    only with prior intent and no remaining registration or administrative dir.
    """
    repo = git.repository(repo)
    if not ident.startswith("run_") or not ident[4:].isalnum():
        raise HarnessError("无效 run ID")
    directory = repo / ".harness" / "runs" / ident
    _canonical(directory)
    if not directory.is_dir():
        raise HarnessError(f"运行不存在：{ident}")
    state = directory / "state.json"
    receipt_path = directory / "worktree-cleanup.json"
    for path in (state, receipt_path, directory / "run.lock"):
        _canonical(path)
    with lock(directory / "run.lock"):
        original = _regular_bytes(state)
        run = json.loads(original)
        checkout = repo / ".harness" / "worktrees" / ident
        _canonical(checkout)
        branch = "harness/" + ident
        common = Path(git.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").removesuffix("\n")).resolve()
        if (not isinstance(run, dict) or run.get("schema_version") not in (7, 8, 9, 10, 11)
                or run.get("id") != ident or run.get("repo") != str(repo)
                or run.get("worktree") != str(checkout) or run.get("branch") != branch
                or not isinstance(run.get("expected_head"), str)
                or not isinstance(run.get("dispatches"), list)):
            raise HarnessError("运行台账或工作树归属无效；保留现场")
        if run.get("status") not in ("completed", "abandoned"):
            raise HarnessError("只能清理 completed 或 abandoned 的终态 run")
        binding = run.get("worktree_binding")
        if (not isinstance(binding, dict) or binding.get("worktree") != str(checkout)
                or binding.get("common_dir") != str(common)
                or binding.get("branch") != "refs/heads/" + branch
                or not isinstance(binding.get("git_dir"), str)):
            raise HarnessError("运行 worktree Git 绑定无效；不能重新推断绑定")
        admin = Path(binding["git_dir"])
        _canonical(admin)
        if admin.parent != common / "worktrees":
            raise HarnessError("运行 worktree Git 管理目录不属于该仓库")
        validate_command_directories(run["dispatches"], directory)
        validate_dispatch_indexes(directory, run["dispatches"])
        if confirm_exited:
            events = directory / "events.jsonl"
            _canonical(events)
            if events.exists():
                _regular_bytes(events)
            confirm_dispatches_exited(run["dispatches"], confirm_exited,
                audit=lambda event, **fields: append_event(events, ident, event, **fields),
                save=lambda: write_json(state, run), observe=group_alive)
        if confirm_exited:
            original = _regular_bytes(state)
        elif _regular_bytes(state) != original:
            raise HarnessError("运行台账在清理前改变；保留现场")
        ensure_dispatches_exited(run["dispatches"], observe=group_alive)
        expected = run["expected_head"]
        # Refuse losing the last named reference to the delivered checkpoint.
        if git.git(repo, "rev-parse", "--verify", "refs/heads/" + branch).strip() != expected:
            raise HarnessError("保留分支与运行检查点不同；保留工作树供核对")
        identity = {"id": ident, "worktree": str(checkout), "branch": branch,
                    "head": expected, "state_digest": hashlib.sha256(original).hexdigest(),
                    "binding": binding}
        previous = None
        if receipt_path.exists():
            previous = json.loads(_regular_bytes(receipt_path))
            if (not isinstance(previous, dict)
                    or any(previous.get(k) != v for k, v in identity.items())
                    or previous.get("status") not in ("pending", "removed")):
                raise HarnessError("清理记录与运行证据不符；保留现场")
        exists = checkout.exists()
        registered = str(checkout) in _registered(repo)
        if previous and previous["status"] == "removed":
            if exists or registered or admin.exists():
                raise HarnessError("已清理工作树或注册重新出现；保留现场")
            status = "removed" if remove else "absent"
        elif not exists:
            if registered or admin.exists() or not (previous or run.get("worktree_removed")):
                raise HarnessError("工作树缺失但清理意图或 Git 注册不符；保留现场")
            status = "absent"
        else:
            git.check_worktree_binding(checkout, binding)
            if git.head(checkout, binding=binding) != expected:
                raise HarnessError("worktree HEAD 与检查点不同；保留工作树供核对")
            git.check_worktree_state(checkout, binding=binding)
            git.assert_removable_worktree(checkout, binding=binding)
            status = "eligible"
        result = {"id": ident, "remove": remove, "run_status": run["status"],
                  "branch": branch, "worktree": str(checkout), "status": status}
        if not remove or status == "removed":
            return result
        write_json(receipt_path, {**identity, "status": "pending", "time": time.time()})
        ensure_dispatches_exited(run["dispatches"], observe=group_alive)
        if _regular_bytes(state) != original:
            raise HarnessError("运行台账在清理前改变；保留现场")
        if git.git(repo, "rev-parse", "--verify", "refs/heads/" + branch).strip() != expected:
            raise HarnessError("保留分支在清理前改变；保留现场")
        if exists:
            git.check_worktree_binding(checkout, binding)
            if git.head(checkout, binding=binding) != expected:
                raise HarnessError("worktree HEAD 在清理前改变；保留现场")
            git.check_worktree_state(checkout, binding=binding)
            git.assert_removable_worktree(checkout, binding=binding)
            git.git(repo, "worktree", "remove", str(checkout))
        if checkout.exists() or checkout.is_symlink() or admin.exists() or str(checkout) in _registered(repo):
            raise HarnessError("工作树尚未完全移除；保留待清理记录")
        write_json(receipt_path, {**identity, "status": "removed", "time": time.time()})
        return {**result, "status": "removed"}
