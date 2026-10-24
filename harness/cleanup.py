"""Explicit, conservative cleanup of retained final-verification checkouts."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path

from . import git
from .util import (HarnessError, append_event, confirm_dispatches_exited,
                   ensure_dispatches_exited, group_alive, lock, read_json, validate_command_directories, write_json)


def _canonical(path):
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise HarnessError(f"清理路径含符号链接或不规范路径；保留现场：{path}")


def _regular_bytes(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise HarnessError(f"清理证据或 Git 绑定必须为普通文件：{path}")
    return path.read_bytes()


def _ensure_exited(run):
    ensure_dispatches_exited(run["dispatches"], observe=group_alive)


def _checkout_identity(repo, run, environment, expected_head, common):
    if (not isinstance(environment, dict) or environment.get("mode") != "fresh_checkout"
            or not isinstance(environment.get("worktree"), str)
            or environment.get("head") != expected_head):
        raise HarnessError("全新验证检出的记录或提交与运行台账不符")
    checkout = Path(environment["worktree"])
    _canonical(checkout)
    if (checkout.name != "checkout" or checkout.parent.name != ".harness"
            or not checkout.parents[1].name.startswith("harness-verify-")
            or checkout.is_relative_to(repo) or checkout == Path(run["worktree"])):
        raise HarnessError("只能清理该 run 记录的外部全新验证检出")
    binding = environment.get("binding")
    fields = ("worktree", "git_dir", "common_dir", "gitfile_sha256", "backlink_sha256", "commondir_sha256")
    if (not isinstance(binding, dict) or any(not isinstance(binding.get(key), str) for key in fields)
            or binding.get("branch", "missing") is not None
            or binding.get("detached_head") != expected_head
            or binding["worktree"] != str(checkout) or binding["common_dir"] != str(common)):
        raise HarnessError("验证 worktree Git 绑定缺失或不属于该运行；不能重新推断绑定")
    admin = Path(binding["git_dir"])
    _canonical(admin)
    if admin.parent != common / "worktrees":
        raise HarnessError("验证 worktree Git 管理目录不属于该仓库")
    return checkout, binding


def _check_missing_binding(checkout, binding):
    """A missing checkout can still have an exact, inspectable registration."""
    admin = Path(binding["git_dir"])
    for name, digest_key, target in (
        ("gitdir", "backlink_sha256", checkout / ".git"),
        ("commondir", "commondir_sha256", Path(binding["common_dir"])),
    ):
        data = _regular_bytes(admin / name)
        resolved = (admin / Path(os.fsdecode(data.removesuffix(b"\n")))).resolve()
        if hashlib.sha256(data).hexdigest() != binding[digest_key] or resolved != target:
            raise HarnessError("缺失检出的 Git 反向或公共目录绑定已改变；保留注册")
    if _regular_bytes(admin / "HEAD").strip() != binding["detached_head"].encode("ascii"):
        raise HarnessError("缺失检出的 detached HEAD 绑定已改变；保留注册")


def _registered(repo):
    return {field.removeprefix("worktree ") for field in
            git.git(repo, "worktree", "list", "--porcelain", "-z").split("\0")
            if field.startswith("worktree ")}


def _remove_checkout(repo, run, artifact, expected_head, common, remove):
    original = _regular_bytes(artifact)
    environment = json.loads(original)
    checkout, binding = _checkout_identity(repo, run, environment, expected_head, common)
    receipt_path = artifact.with_name("cleanup.json")
    _canonical(receipt_path)
    digest = hashlib.sha256(original).hexdigest()
    receipt = {"run_id": run["id"], "artifact": str(artifact), "artifact_digest": digest,
               "worktree": str(checkout), "head": expected_head}
    previous = None
    if receipt_path.exists():
        _regular_bytes(receipt_path)
        previous = read_json(receipt_path)
        if (not isinstance(previous, dict) or any(previous.get(key) != value for key, value in receipt.items())
                or previous.get("status") not in ("pending", "removed")):
            raise HarnessError("清理记录与原验证证据不符；保留现场")
    exists = checkout.exists()
    registered = str(checkout) in _registered(repo)
    if previous and previous["status"] == "removed":
        if exists or registered:
            raise HarnessError("已清理路径或 Git 注册重新出现；保留现场")
        # Git may reuse the old administrative directory name for another
        # checkout. A completed receipt never authorizes touching that directory.
        return {"artifact": str(artifact), "worktree": str(checkout),
                "status": "removed" if remove else "absent"}
    if exists:
        git.check_worktree_binding(checkout, binding)
        git.assert_removable_worktree(checkout, binding=binding)
        status = "eligible"
    elif registered:
        _check_missing_binding(checkout, binding)
        status = "stale_registration"
    else:
        if Path(binding["git_dir"]).exists():
            raise HarnessError("检出及注册缺失但 Git 管理目录仍存在；保留现场")
        status = "absent"
    result = {"artifact": str(artifact), "worktree": str(checkout), "status": status}
    if not remove:
        return result
    # Persist intent before Git changes anything, without altering raw evidence.
    write_json(receipt_path, {**receipt, "status": "pending", "time": time.time()})
    _ensure_exited(run)
    if _regular_bytes(artifact) != original:
        raise HarnessError("验证检出证据在清理前改变；保留现场")
    if exists:
        git.check_worktree_binding(checkout, binding)
        git.assert_removable_worktree(checkout, binding=binding)
    elif registered:
        _check_missing_binding(checkout, binding)
    if exists or registered:
        # Exact removal also unregisters an absent checkout; never prune other runs.
        git.git(repo, "worktree", "remove", str(checkout))
    if checkout.exists() or checkout.is_symlink() or str(checkout) in _registered(repo):
        raise HarnessError("验证工作树尚未完全移除；保留待清理记录")
    # Empty wrappers belong to this checkout; unexpected siblings are preserved.
    for parent in (checkout.parent, checkout.parents[1]):
        try:
            parent.rmdir()
        except OSError:
            pass
    write_json(receipt_path, {**receipt, "status": "removed", "time": time.time()})
    return {**result, "status": "removed"}


def cleanup_verification(repo: Path, ident: str, *, remove=False, confirm_exited=()):
    """Preview all saved checkouts, or remove eligible ones and report refusals."""
    repo = git.repository(repo)
    if not ident.startswith("run_") or not ident[4:].isalnum():
        raise HarnessError("无效 run ID")
    directory = repo / ".harness" / "runs" / ident
    _canonical(directory)
    if not directory.is_dir():
        raise HarnessError(f"运行不存在：{ident}")
    _canonical(directory / "run.lock")
    with lock(directory / "run.lock"):
        _regular_bytes(directory / "state.json")
        run = read_json(directory / "state.json")
        if (not isinstance(run, dict) or run.get("schema_version") not in (7, 8, 9, 10, 11) or run.get("id") != ident
                or run.get("repo") != str(repo) or not isinstance(run.get("dispatches"), list)
                or not isinstance(run.get("final_history"), list)
                or not isinstance(run.get("worktree_binding"), dict)
                or run.get("worktree") != str(repo / ".harness" / "worktrees" / ident)):
            raise HarnessError("运行台账或工作树归属无效；保留现场")
        validate_command_directories(run["dispatches"], directory)
        common = Path(git.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").removesuffix("\n")).resolve()
        if run["worktree_binding"].get("common_dir") != str(common):
            raise HarnessError("运行台账的 Git 公共目录绑定已改变")
        if confirm_exited:
            events = directory / "events.jsonl"
            _canonical(events)
            if events.exists():
                _regular_bytes(events)
            confirm_dispatches_exited(run["dispatches"], confirm_exited,
                audit=lambda event, **fields: append_event(events, ident, event, **fields),
                save=lambda: write_json(directory / "state.json", run), observe=group_alive)
        _ensure_exited(run)
        artifacts = []
        for final in run["final_history"]:
            if (not isinstance(final, dict) or type(final.get("attempt")) is not int
                    or final["attempt"] < 1 or not isinstance(final.get("head"), str)
                    or not isinstance(final.get("verification_rounds"), list)):
                raise HarnessError("整体验证台账无效；保留现场")
            for index, round_record in enumerate(final["verification_rounds"], 1):
                folder = directory / "verification" / "final" / str(final["attempt"]) / f"verification-{index}"
                if not isinstance(round_record, dict) or round_record.get("artifact_dir") != str(folder):
                    raise HarnessError("整体验证证据目录与运行台账不符")
                artifact = folder / "checkout.json"
                _canonical(artifact)
                if artifact.exists():
                    artifacts.append((artifact, final["head"]))
        if len({artifact for artifact, _ in artifacts}) != len(artifacts):
            raise HarnessError("整体验证台账重复引用检出证据")
        results = []
        for artifact, expected_head in artifacts:
            try:
                results.append(_remove_checkout(repo, run, artifact, expected_head, common, remove))
            except (HarnessError, OSError, ValueError) as error:
                results.append({"artifact": str(artifact), "status": "blocked", "error": str(error)})
        return {"id": ident, "remove": remove, "checkouts": results}
