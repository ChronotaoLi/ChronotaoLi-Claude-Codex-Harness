"""The one explicit cleanup entry: retained verification checkouts and the run worktree.

One shared gate (run lock, ledger ownership, historical version, command
directories, indexes, worker receipt links, exit evidence) precedes two
concrete resources. Fresh verification checkouts are removable in any run
status; the branch worktree only for completed or abandoned runs. Each
resource keeps its own receipt, so an interrupted removal resumes from the
saved intent. The branch, ledger and run evidence are never removed.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path

from .versions import HISTORICAL_CLEANUP_VERSIONS, RUN_SCHEMA_VERSION
from . import git
from .input_projection import validate_dispatch_indexes as validate_input_dispatch_indexes
from .path_evidence import validate_dispatch_indexes
from .run_store import RunStore
from .worker_receipts import validate_worker_receipt_links
from .util import (HarnessError, append_event, confirm_dispatches_exited,
                   ensure_dispatches_exited, group_alive, lock, readonly_lock,
                   validate_command_directories, write_json)

TERMINAL = ("completed", "abandoned")


def _canonical(path):
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise HarnessError(f"清理路径含符号链接或不规范路径；保留现场：{path}")


def _regular_bytes(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise HarnessError(f"清理证据或 Git 绑定必须为普通文件：{path}")
    return path.read_bytes()


def _registered(repo):
    return {field.removeprefix("worktree ") for field in
            git.git(repo, "worktree", "list", "--porcelain", "-z").split("\0")
            if field.startswith("worktree ")}


def _ensure_exited(run):
    ensure_dispatches_exited(run["dispatches"], observe=group_alive)


def cleanup_run(repo: Path, ident: str, *, remove=False, confirm_exited=(), remove_input_copies=False):
    """Preview every retained resource of one run, or remove the eligible ones.

    Gate failures raise before any resource is touched. A refused resource is
    reported as ``blocked`` with its error; the others are still handled. The
    worktree of a non-terminal run is ``retained`` with its reason.
    """
    repo = git.repository(repo)
    if not ident.startswith("run_") or not ident[4:].isalnum():
        raise HarnessError("无效 run ID")
    directory = repo / ".harness" / "runs" / ident
    _canonical(directory)
    if not directory.is_dir():
        raise HarnessError(f"运行不存在：{ident}")
    for name in ("state.json", "run.lock", "worktree-cleanup.json"):
        _canonical(directory / name)
    if remove or confirm_exited:
        from .private_state import ensure_private_state
        ensure_private_state(repo)
    with (lock if remove or confirm_exited else readonly_lock)(directory / "run.lock"):
        run, original, common, artifacts = _gate(repo, ident, directory, confirm_exited)
        checkouts = []
        for artifact, expected_head in artifacts:
            try:
                checkouts.append(_clean_checkout(repo, run, artifact, expected_head, common, remove))
            except (HarnessError, OSError, ValueError) as error:
                checkouts.append({"artifact": str(artifact), "status": "blocked", "error": str(error)})
        try:
            worktree = _clean_worktree(repo, run, directory, original, remove, remove_input_copies)
        except (HarnessError, OSError, ValueError) as error:
            worktree = {"worktree": run["worktree"], "branch": run["branch"],
                        "status": "blocked", "error": str(error)}
        return {"id": ident, "remove": remove, "run_status": run.get("status"),
                "checkouts": checkouts, "worktree": worktree}


def blocked(result):
    """True when an eligible resource was refused; a policy-retained worktree is not."""
    return (result["worktree"]["status"] == "blocked"
            or any(item["status"] == "blocked" for item in result["checkouts"]))


def _gate(repo, ident, directory, confirm_exited):
    """Validate ownership and exit evidence once; return the bound ledger state."""
    state = directory / "state.json"
    original = _regular_bytes(state)
    run = json.loads(original)
    checkout = repo / ".harness" / "worktrees" / ident
    _canonical(checkout)
    branch = "harness/" + ident
    common = Path(git.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").removesuffix("\n")).resolve()
    if (not isinstance(run, dict) or run.get("schema_version") not in HISTORICAL_CLEANUP_VERSIONS
            or run.get("id") != ident or run.get("repo") != str(repo)
            or run.get("worktree") != str(checkout) or run.get("branch") != branch
            or not isinstance(run.get("expected_head"), str)
            or not isinstance(run.get("dispatches"), list)
            or not isinstance(run.get("final_history"), list)):
        raise HarnessError("运行台账或工作树归属无效；保留现场")
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
    validate_input_dispatch_indexes(directory, run["dispatches"])
    if run["schema_version"] >= 14:
        validate_worker_receipt_links(run, directory)
    if confirm_exited:
        store = RunStore(repo / ".harness")
        store.attach(run)
        events = directory / "events.jsonl"
        _canonical(events)
        if events.exists():
            _regular_bytes(events)
        confirm_dispatches_exited(run["dispatches"], confirm_exited,
            audit=lambda event, **fields: append_event(events, ident, event, **fields),
            save=store.commit, observe=group_alive)
        original = _regular_bytes(state)
    elif _regular_bytes(state) != original:
        raise HarnessError("运行台账在清理前改变；保留现场")
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
    return run, original, common, artifacts


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


def _clean_checkout(repo, run, artifact, expected_head, common, remove):
    """One fresh verification checkout; its receipt is cleanup.json beside the evidence."""
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
        previous = json.loads(_regular_bytes(receipt_path))
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


def _branch_matches(repo, branch, expected):
    """True when the retained branch still names the run's checkpoint."""
    try:
        return git.git(repo, "rev-parse", "--verify", "refs/heads/" + branch).strip() == expected
    except HarnessError:
        return False


def _clean_worktree(repo, run, directory, original, remove, remove_input_copies):
    """The run's branch worktree; its receipt is worktree-cleanup.json in the run directory."""
    checkout, branch = Path(run["worktree"]), run["branch"]
    binding = run["worktree_binding"]
    admin = Path(binding["git_dir"])
    state, receipt_path = directory / "state.json", directory / "worktree-cleanup.json"
    result = {"worktree": str(checkout), "branch": branch}
    if run.get("status") not in TERMINAL:
        # Policy, not a refusal. Only the current ledger version can still be
        # finished or abandoned; an older one leaves the worktree to the operator.
        if run.get("schema_version") == RUN_SCHEMA_VERSION:
            return {**result, "status": "retained", "reason_kind": "finish_or_abandon_first",
                    "reason": f"run 状态为 {run.get('status')}；工作树只在 run 完成（completed）"
                              "或放弃（abandoned）之后清理，请先完成或放弃该 run"}
        return {**result, "status": "retained", "reason_kind": "manual_removal",
                "reason": f"run 状态为 {run.get('status')}，台账版本 {run.get('schema_version')} "
                          "不能由当前版本恢复或放弃；cleanup-run 不删除这个工作树，请人工核对后自行移除"}
    expected = run["expected_head"]
    # The receipt names the resource, not the whole ledger: an audited
    # --confirm-exited may rewrite the ledger between an interruption and its rerun.
    identity = {"id": run["id"], "worktree": str(checkout), "branch": branch,
                "head": expected, "binding": binding}
    previous = None
    if receipt_path.exists():
        previous = json.loads(_regular_bytes(receipt_path))
        if (not isinstance(previous, dict)
                or any(previous.get(k) != v for k, v in identity.items())
                or previous.get("status") not in ("pending", "removed")):
            raise HarnessError("清理记录与运行证据不符；保留现场")
    exists = checkout.exists()
    input_summary = {"status": "none", "files": []}
    registered = str(checkout) in _registered(repo)
    if previous and previous["status"] == "removed":
        # Finished stays finished, even after the operator deletes or moves the branch.
        if exists or registered or admin.exists():
            raise HarnessError("已清理工作树或注册重新出现；保留现场")
        return {**result, "status": "removed" if remove else "absent", "input_copies": input_summary}
    if not exists:
        # Ledgers from before the single entry recorded removal by abandon
        # --remove-worktree: an intent (pending_worktree_removal) or its completion.
        legacy = run.get("worktree_removed") is True or run.get("pending_worktree_removal") is True
        if registered or admin.exists() or not (previous or legacy):
            raise HarnessError("工作树缺失但清理意图或 Git 注册不符；保留现场")
        status = "absent"
    else:
        # Removing the worktree must not lose the last named reference to the checkpoint.
        if not _branch_matches(repo, branch, expected):
            raise HarnessError("保留分支与运行检查点不同；保留工作树供核对")
        git.check_worktree_binding(checkout, binding)
        if git.head(checkout, binding=binding) != expected:
            raise HarnessError("worktree HEAD 与检查点不同；保留工作树供核对")
        git.check_worktree_state(checkout, binding=binding)
        from .workspace_inputs import cleanup_inputs
        input_summary = cleanup_inputs(run, directory, remove=remove and remove_input_copies)
        if input_summary["files"] and not remove and input_summary["status"] != "removed":
            return {**result, "status": "input_copies_present", "input_copies": input_summary}
        git.assert_removable_worktree(checkout, binding=binding)
        status = "eligible"
    result = {**result, "status": status, "input_copies": input_summary}
    if not remove:
        return result
    # The ledger digest is kept as a record of the state that authorized the removal.
    record = {**identity, "state_digest": hashlib.sha256(original).hexdigest()}
    write_json(receipt_path, {**record, "status": "pending", "time": time.time()})
    _ensure_exited(run)
    if _regular_bytes(state) != original:
        raise HarnessError("运行台账在清理前改变；保留现场")
    if exists:
        if not _branch_matches(repo, branch, expected):
            raise HarnessError("保留分支在清理前改变；保留现场")
        git.check_worktree_binding(checkout, binding)
        if git.head(checkout, binding=binding) != expected:
            raise HarnessError("worktree HEAD 在清理前改变；保留现场")
        git.check_worktree_state(checkout, binding=binding)
        git.assert_removable_worktree(checkout, binding=binding)
        git.git(repo, "worktree", "remove", str(checkout))
    if checkout.exists() or checkout.is_symlink() or admin.exists() or str(checkout) in _registered(repo):
        raise HarnessError("工作树尚未完全移除；保留待清理记录")
    write_json(receipt_path, {**record, "status": "removed", "time": time.time()})
    return {**result, "status": "removed"}
