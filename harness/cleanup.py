"""The one explicit cleanup entry: retained verification checkouts and the run worktree.

One shared gate (current ledger version, run lock, ledger ownership, command
directories, indexes, worker receipt links, exit evidence) precedes two
concrete resources. Fresh verification checkouts are removable in any run
status; the branch worktree only for completed or abandoned runs. Each
resource keeps its own receipt, so an interrupted removal resumes from the
saved intent. The branch, ledger and run evidence are never removed.

The checkout step (``clean_checkout``) is also the runtime's reclamation of a
verified checkout after the whole-run verdict is saved: same ownership,
identity and removability gates under the caller’s ledger authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from .versions import parse_current_ledger, precheck_run
from . import checkout_identity, git, stops
from .commands import valid_command
from .context import compact_checks
from .input_projection import validate_dispatch_indexes as validate_input_dispatch_indexes
from .path_evidence import validate_dispatch_indexes
from .run_store import RunStore
from .state_files import LockUnavailable, StateFiles, holding
from .worker_receipts import validate_worker_receipt_links
from .util import (HarnessError, append_event, confirm_dispatches_exited,
                   ensure_dispatches_exited, group_alive, strict_json_loads,
                   validate_command_directories, write_json)

TERMINAL = ("completed", "abandoned")


def _canonical(path):
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise HarnessError(f"清理路径含符号链接或不规范路径；保留现场：{path}")


def with_run_lock(files, use, *, write):
    """Use the run lock, or preview without it when only the lock file is absent."""
    entered = False

    def held(locked):
        nonlocal entered
        entered = True
        return use(locked)

    try:
        return holding(files.lock(("run.lock",), mode="exclusive" if write else "shared", create=write), held)
    except LockUnavailable as error:
        if entered:
            raise
        outcome = error.outcome
        if not write and outcome.state == "absent" and outcome.step.leaf:
            return use(None)
        if outcome.state == "busy":
            prefix = "已有进程持有运行锁" if write else "运行正被写入，暂不能预览"
            raise HarnessError(f"{prefix}：{outcome.path}", stop=stops.make("busy", "run_lock")) from None
        outcome.evidence({"artifact": outcome.path})
        raise


def read_run_state(files, locked):
    outcome = (locked.read("state.json", limit=None) if locked is not None
               else files.read(("state.json",), limit=None))
    return outcome.evidence({"artifact": outcome.path})


def _registered(repo):
    registry = git.worktree_registry(repo)
    if registry is None:
        raise HarnessError("无法核实完整的 Git 工作树登记；保留现场")
    return {admin: os.fsdecode(path) for admin, path in registry.items()}


def _ensure_exited(run, directory, *, state):
    ensure_dispatches_exited(run["dispatches"], state=state, directory=directory, observe=group_alive)


def common_dir(repo):
    """The repository's Git common directory, the parent of every linked worktree's admin directory."""
    return Path(git.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", effect=git.READ)
                .removesuffix("\n")).resolve()


def checkout_folder(directory, final, index):
    """The evidence directory of round ``index`` (from 1) of one whole-run attempt."""
    return Path(directory) / "verification" / "final" / str(final["attempt"]) / f"verification-{index}"


def checkout_record_digest(directory, final, index, *, state):
    """SHA256 of a checkout record read as a canonical regular file; the verdict save binds it."""
    folder = checkout_folder(directory, final, index)
    artifact = folder / "checkout.json"
    source = {"artifact": str(artifact)}
    with stops.evidence_reads(source=source):
        if final["verification_rounds"][index - 1]["artifact_dir"] != str(folder):
            raise HarnessError("整体验证证据目录与运行台账不符")
    outcome = StateFiles(state).digest(artifact.relative_to(state).parts)
    if outcome.state != "read":
        outcome.evidence(source)
    return outcome.digest


def recorded_worktree(artifact, *, state):
    """The directory a checkout record names, unverified: for diagnostics, never for removal."""
    raw = StateFiles(state).read(artifact.relative_to(state).parts, limit=None).bytes_or_none()
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return value["worktree"] if isinstance(value, dict) and isinstance(value.get("worktree"), str) else None


def cleanup_run(repo: Path, ident: str, *, remove=False, confirm_exited=(), remove_input_copies=False,
                remove_ignored=False):
    """Preview every retained resource of one run, or remove the eligible ones.

    Gate failures raise before any resource is touched. A refused resource is
    reported as ``blocked`` with its error; the others are still handled. The
    worktree of a non-terminal run is ``retained`` with its reason. Ignored
    files in the worktree are removed only with ``remove_ignored``; those in a
    checkout always go with it.
    """
    write = bool(remove or confirm_exited)
    scope = git.read_only()
    try:
        try:
            if not write:
                scope.__enter__()
            return _cleanup_run(repo, ident, remove=remove, confirm_exited=confirm_exited,
                                remove_input_copies=remove_input_copies, remove_ignored=remove_ignored, write=write)
        finally:
            scope.__exit__(None, None, None)
    finally:
        scope.__exit__(None, None, None)


def _cleanup_run(repo, ident, *, remove, confirm_exited, remove_input_copies, remove_ignored, write):
    repo = git.repository(repo)
    state = repo / ".harness"
    precheck_run(state, ident)
    directory = state / "runs" / ident
    for path in (directory, directory / "state.json", directory / "run.lock", directory / "worktree-cleanup.json"):
        with stops.evidence_reads(source={"artifact": str(path)}):
            _canonical(path)
    if write:
        from .private_state import ensure_private_state
        ensure_private_state(repo)
    files = StateFiles(state).under("runs", ident)

    def clean(locked):
        def read_state():
            return read_run_state(files, locked)

        store = None
        if confirm_exited:
            store = RunStore(state)
            store.bind_directory(locked.identity)
        run, original, common, checkout_rounds = _gate(repo, ident, directory, files, read_state, store, confirm_exited)
        checkouts = []
        def authority():
            if read_state() != original:
                raise HarnessError("运行台账在清理前改变；保留现场")

        for artifact, final, index, entry, verified_worktree in checkout_rounds:
            if entry is None:
                checkouts.append({"artifact": str(artifact), "recorded_worktree": recorded_worktree(artifact, state=state),
                                  "status": "blocked", "error": "该轮没有创建登记；只报告，不删除"})
                continue
            try:
                checkouts.append(clean_checkout(repo, run, directory, final, index, common, remove,
                                                state=state, authority=authority, verified_worktree=verified_worktree))
            except (HarnessError, OSError, ValueError) as error:
                checkouts.append({"artifact": str(artifact), "recorded_worktree": entry["worktree"],
                                  "status": "blocked", "error": str(error)})
        try:
            worktree = _clean_worktree(repo, run, directory, original, remove, remove_input_copies,
                                       remove_ignored, files=files, read_state=read_state)
        except (HarnessError, OSError, ValueError) as error:
            worktree = {"worktree": run["worktree"], "branch": run["branch"],
                        "status": "blocked", "error": str(error)}
        return {"id": ident, "remove": remove, "remove_ignored": remove_ignored,
                "run_status": run.get("status"), "checkouts": checkouts, "worktree": worktree}

    return with_run_lock(files, clean, write=write)


def blocked(result):
    """True when an eligible resource was refused; a policy-retained worktree is not."""
    return (result["worktree"]["status"] == "blocked"
            or any(item["status"] == "blocked" for item in result["checkouts"]))


def _gate(repo, ident, directory, files, read_state, store, confirm_exited):
    """Validate ownership and exit evidence once; return the bound ledger state."""
    source = {"artifact": str(directory / "state.json")}
    original = read_state()
    with stops.evidence_reads(source=source):
        run = parse_current_ledger(original)
    common = common_dir(repo)
    with stops.evidence_reads(source=source):
        rounds = _validate_cleanup_ledger(repo, ident, directory, files.path, run, common)
    if store is not None:
        store.attach(run)
        events = ("events.jsonl",)
        outcome = files.read(events, limit=None)
        if not (outcome.state == "absent" and outcome.step.leaf):
            outcome.evidence({"artifact": outcome.path})

        def audit(event, **fields):
            store.check_current()
            append_event(directory / events[0], ident, event, **fields)

        confirm_dispatches_exited(run["dispatches"], confirm_exited, state=files.path,
                                  audit=audit, save=store.commit, observe=group_alive)
        original = read_state()
    elif read_state() != original:
        raise HarnessError("运行台账在清理前改变；保留现场", stop=stops.make("runtime_failure", "invariant"))
    _ensure_exited(run, directory, state=files.path)
    return run, original, common, rounds


def _published_check_matches(check, dispatch):
    """Compare the result with the exit facts and identity saved by its own dispatch."""
    reason = check.get("termination_reason")
    code = check.get("exit_code")
    outcome = check.get("process_outcome")
    if (not isinstance(outcome, dict) or "exit_code" not in check or "exit_code" not in outcome
            or "exit_code" not in dispatch or check.get("exit_confirmed") is not True
            or check.get("evidence_complete") is not True or check.get("timed_out") is not (reason == "timeout")
            or dispatch.get("process_state") != "exited" or dispatch.get("process_outcome") != outcome):
        return False
    for key in ("exit_code", "termination_reason", "exit_confirmed", "evidence_complete"):
        value = check.get(key)
        if any(type(row.get(key)) is not type(value) or row.get(key) != value for row in (outcome, dispatch)):
            return False
    if outcome.get("started") is True:
        pgid = dispatch.get("pgid")
        if (type(code) is not int or type(pgid) is not int or pgid <= 0
                or outcome.get("startup_state") != "started"
                or outcome.get("pid") != pgid or outcome.get("pgid") != pgid):
            return False
    elif (outcome.get("started") is not False or outcome.get("startup_state") != "not_started"
          or code is not None or any(outcome.get(key) is not None for key in ("pid", "pgid"))
          or dispatch.get("pgid") is not None):
        return False
    status = check.get("status")
    if dispatch.get("status") != ("completed" if status == "passed" else status):
        return False
    if reason == "normal":
        return code is not None and status == ("passed" if code == 0 else "failed")
    if reason == "timeout":
        return status == "timed_out"
    return reason == "start_failed" and code is None and status == "failed"


def _validate_published_verification(files, folder, final, commands, dispatches, seen, *, worktree, fresh_checkout):
    """Check a round's complete result set before any explicit cleanup can touch resources."""
    from .finalization import verification_failure_kind

    artifact = folder / "results.json"
    source = {"artifact": str(artifact)}
    with stops.evidence_reads(source=source):
        def fail():
            raise HarnessError(f"第 {final['attempt']} 次整体验证的轮次结果、派发或结论不符；保留现场")

        results = strict_json_loads(files.read(artifact.relative_to(files.path).parts, limit=None).evidence(source))
        if not isinstance(results, list) or len(results) != len(commands):
            fail()
        verified_worktree = None
        for index, (check, command) in enumerate(zip(results, commands)):
            if (not isinstance(check, dict) or check.get("command") != command
                    or check.get("log") != str(folder / f"check-{index}")
                    or any(not isinstance(check.get(key), str) for key in ("stdout", "stderr"))):
                fail()
            timing = check.get("timing_source")
            ident = timing.get("dispatch_id") if isinstance(timing, dict) else None
            if not isinstance(ident, str) or ident in seen:
                fail()
            dispatch = dispatches.get(ident)
            if (dispatch is None or dispatch.get("role") != "verification" or dispatch.get("task_id") != "final"
                    or "phase" in dispatch or "phase" in check or dispatch.get("command") != command
                    or timing.get("scope") != "formal_verification_command_including_cleanup"
                    or timing.get("scope") != dispatch.get("timing_scope")
                    or any(check.get(key) != dispatch.get(key)
                           for key in ("started_at", "finished_at", "duration_seconds"))
                    or not _published_check_matches(check, dispatch)):
                fail()
            if fresh_checkout:
                if (not isinstance(check.get("worktree"), str) or check["worktree"] != dispatch.get("worktree")
                        or check.get("checkout_artifact") != str(folder / "checkout.json")
                        or (verified_worktree is not None and check["worktree"] != verified_worktree)):
                    fail()
                verified_worktree = check["worktree"]
            elif dispatch.get("worktree") != worktree:
                fail()
            seen.add(ident)
        failure_kind = verification_failure_kind(results)
        if final["status"] == "verification_failed":
            if failure_kind is None or failure_kind != final["failure_kind"]:
                fail()
            saved = final["verification_failures"]
            if len(saved) != len(results):
                fail()
            expected = compact_checks(results)
            for actual, projected in zip(saved, expected):
                # Older summaries may omit display fields. The full round
                # results still supply the logs and outcome for every check.
                for key in ("stdout", "stderr", "log"):
                    if key not in actual:
                        projected.pop(key, None)
                if json.dumps(actual, sort_keys=True, allow_nan=False) != json.dumps(
                        projected, sort_keys=True, allow_nan=False):
                    fail()
        elif failure_kind is not None or "verification_failures" in final or "failure_kind" in final:
            fail()
        return verified_worktree


def _validate_cleanup_ledger(repo, ident, directory, state_root, run, common):
    from .finalization import validate_final_records
    from .review_progress import entries

    checkout = state_root / "worktrees" / ident
    _canonical(checkout)
    branch = "harness/" + ident
    if (run.get("id") != ident or run.get("repo") != str(repo)
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
    validate_worker_receipt_links(run, directory)
    history = run["final_history"]
    entries(history, whole_run=True)
    validate_final_records(history)
    if run.get("status") == "completed" and not any(
            final["status"] == "approved" and final["head"] == run["expected_head"] for final in history):
        raise HarnessError("已完成的 run 缺少对应 HEAD 的整体批准记录；保留现场")
    if not history:
        checkout_identity.validate_checkout_entries(run)
        return []
    config = run.get("config_snapshot")
    verification = config.get("verification") if isinstance(config, dict) else None
    if not isinstance(verification, dict) or type(verification.get("fresh_checkout")) is not bool:
        raise HarnessError("整体验证的全新检出配置快照缺失或无效；保留现场")
    fresh_checkout = verification["fresh_checkout"]
    commands = verification.get("commands")
    dispatches = {}
    for dispatch in run["dispatches"]:
        ident_value = dispatch.get("id")
        if isinstance(ident_value, str):
            dispatches[ident_value] = None if ident_value in dispatches else dispatch
    files = StateFiles(state_root)
    seen_checks = set()
    checkouts = []
    for attempt, final in enumerate(history, 1):
        if final["attempt"] != attempt:
            raise HarnessError(f"整体验证台账的第 {attempt} 次尝试编号不符；保留现场")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", final["head"]):
            raise HarnessError(f"第 {attempt} 次整体记录的 HEAD 无效；保留现场")
        attempt_dir = directory / "verification" / "final" / str(attempt)
        if (final["artifact"] != str(attempt_dir / "final.json")
                or final["snapshot_artifact"] != str(attempt_dir / "snapshot.json")):
            raise HarnessError(f"第 {attempt} 次整体记录的证据路径不符；保留现场")
        rounds = final["verification_rounds"]
        # Publication completes the last round and binds its results and fresh checkout in one save.
        published = final["phase"] != "verifying"
        verdict_fields = ("verification", "verification_failures", "failure_kind", "diff_artifact")
        if (published and not rounds) or (not published and any(key in final for key in verdict_fields)):
            raise HarnessError(f"第 {attempt} 次整体验证的结论与轮次不符；保留现场")
        for index, round_record in enumerate(rounds, 1):
            folder = checkout_folder(directory, final, index)
            if round_record["artifact_dir"] != str(folder):
                raise HarnessError("整体验证证据目录与运行台账不符")
            completed = published and index == len(rounds)
            if (round_record["status"] != ("completed" if completed else "running")
                    or (completed and final["verification"] != str(folder / "results.json"))):
                raise HarnessError(f"第 {attempt} 次整体验证的结论与第 {index} 轮不符；保留现场")
            if completed and fresh_checkout:
                valid_digest = re.fullmatch(r"[0-9a-f]{64}", round_record.get("checkout_digest", "")) is not None
            else:
                valid_digest = "checkout_digest" not in round_record
            if not valid_digest:
                raise HarnessError(f"第 {attempt} 次整体验证第 {index} 轮的检出摘要缺失或无效；保留现场")
            verified_worktree = None
            if completed:
                # Verification settings cannot change across a resume. Use the
                # saved settings, even if the project's configuration changed.
                if not isinstance(commands, list) or not all(map(valid_command, commands)):
                    raise HarnessError("整体验证的命令配置快照缺失或无效；保留现场")
                verified_worktree = _validate_published_verification(
                    files, folder, final, commands, dispatches, seen_checks,
                    worktree=run["worktree"], fresh_checkout=fresh_checkout)
            checkouts.append((folder / "checkout.json", final, index, verified_worktree))
    checkout_identity.validate_checkout_entries(run)
    rounds = []
    for artifact, final, index, verified_worktree in checkouts:
        entry = checkout_identity.entry_for(run, final["attempt"], index)
        if entry is not None or files.probe(artifact.relative_to(state_root).parts).state != "absent":
            rounds.append((artifact, final, index, entry, verified_worktree))
    return rounds


def _check_missing_binding(checkout, binding):
    """A missing checkout can still have an exact, inspectable registration."""
    admin = Path(binding["git_dir"])
    for name, digest_key, target in (
        ("gitdir", "backlink_sha256", checkout / ".git"),
        ("commondir", "commondir_sha256", Path(binding["common_dir"])),
    ):
        data = git._binding_file(admin / name)
        resolved = (admin / Path(os.fsdecode(data.removesuffix(b"\n")))).resolve()
        if hashlib.sha256(data).hexdigest() != binding[digest_key] or resolved != target:
            raise HarnessError("缺失检出的 Git 反向或公共目录绑定已改变；保留注册")
    if git._binding_file(admin / "HEAD").strip() != binding["detached_head"].encode("ascii"):
        raise HarnessError("缺失检出的 detached HEAD 绑定已改变；保留注册")


def _read_receipt(files, components):
    outcome = files.read(components, limit=None)
    if outcome.state == "absent" and outcome.step.leaf:
        return None
    with stops.evidence_reads(source={"artifact": outcome.path}):
        value = json.loads(outcome.evidence({"artifact": outcome.path}))
        if not isinstance(value, dict):
            raise HarnessError(f"清理记录不是对象；保留现场：{outcome.path}")
        return value


class CheckoutRemovalIncomplete(HarnessError):
    """A checkout was preserved, or its removal did not complete."""


def _checkout_status(repo, root, checkout, admin, entry, previous):
    """Classify the current checkout and its exact Git registration."""
    root_now = checkout_identity.directory_identity(root)
    worktree_now = checkout_identity.directory_identity(checkout)
    admin_now = checkout_identity.directory_identity(admin)
    registrations = _registered(repo)
    path = str(checkout)
    registered = path in registrations.values()
    identity = entry["identity"]
    binding = entry["binding"]
    if previous and previous["status"] == "removed":
        if worktree_now is not None or registered:
            raise CheckoutRemovalIncomplete("已清理路径或 Git 注册重新出现；保留现场")
        return "removed"
    if root_now is not None and root_now != identity["root"]:
        raise CheckoutRemovalIncomplete("检出根目录不是创建时那一个；保留现场")
    if registered:
        match = path.lower()
        count = sum(candidate.lower() == match or candidate.lower().endswith("/" + match)
                    for candidate in registrations.values())
        if count != 1:
            raise CheckoutRemovalIncomplete(f"该路径在 Git 里有 {count} 条登记；保留现场")
        if registrations.get(admin) != path:
            raise CheckoutRemovalIncomplete("Git 路径登记不属于创建时的管理目录；保留现场")
    if worktree_now is not None:
        if admin_now is None:
            raise CheckoutRemovalIncomplete("检出目录仍在而创建时的 Git 管理目录已不在；检出可能只删除了一部分。"
                                            "核对后手工删除该目录，再重跑 cleanup-run --remove")
        if worktree_now != identity["worktree"] or admin_now != identity["git_dir"] or not registered:
            raise CheckoutRemovalIncomplete("验证检出或 Git 管理目录与创建登记不符；保留现场")
        git.check_worktree_binding(checkout, binding)
        git.assert_removable_worktree(checkout, binding=binding)
        return "eligible"
    if registered:
        if admin_now != identity["git_dir"]:
            raise CheckoutRemovalIncomplete("缺失检出的 Git 管理目录与创建登记不符；保留注册")
        _check_missing_binding(checkout, binding)
        return "stale_registration"
    if admin_now == identity["git_dir"]:
        raise CheckoutRemovalIncomplete("检出路径和注册已不在，原 Git 管理目录仍在；保留现场")
    return "absent"


def clean_checkout(repo, run, directory, final, index, common, remove, *, state, authority, by="cleanup-run",
                   verified_worktree=None):
    """Clean one registered checkout, reconciling its receipt under the removal lock."""
    folder = checkout_folder(directory, final, index)
    artifact = folder / "checkout.json"
    files = StateFiles(state).under(*folder.relative_to(state).parts)
    entry = checkout_identity.entry_for(run, final["attempt"], index)
    root, checkout = Path(entry["root"]), Path(entry["worktree"])
    binding = entry["binding"]
    admin = Path(binding["git_dir"])
    receipt_path = folder / "cleanup.json"
    receipt = {"run_id": run["id"], "attempt": entry["attempt"], "round": entry["round"],
               "worktree": str(checkout), "identity": entry["identity"]}
    result = {"artifact": str(artifact), "worktree": str(checkout)}
    try:
        if final["verification_rounds"][index - 1].get("artifact_dir") != str(folder):
            raise CheckoutRemovalIncomplete("整体验证证据目录与运行台账不符")
        if binding["common_dir"] != str(common) or admin.parent != common / "worktrees":
            raise CheckoutRemovalIncomplete("验证 worktree Git 管理目录不属于该仓库")
        if verified_worktree is not None and str(checkout) != verified_worktree:
            raise CheckoutRemovalIncomplete("验证检出与该轮检查派发记录不符；保留现场")
        previous = _read_receipt(files, ("cleanup.json",))
        if previous is not None and (any(json.dumps(previous.get(key), sort_keys=True) !=
                                         json.dumps(value, sort_keys=True) for key, value in receipt.items())
                                     or previous.get("status") not in ("pending", "removed")):
            raise CheckoutRemovalIncomplete("清理记录与创建登记不符；保留现场")
    except (HarnessError, OSError, ValueError, RecursionError) as error:
        if isinstance(error, CheckoutRemovalIncomplete):
            raise
        raise CheckoutRemovalIncomplete(str(error)) from error

    def classify():
        try:
            for path in (root, checkout, admin):
                _canonical(path)
            return _checkout_status(repo, root, checkout, admin, entry, previous)
        except (HarnessError, OSError, ValueError, RuntimeError) as error:
            if isinstance(error, CheckoutRemovalIncomplete):
                raise
            raise CheckoutRemovalIncomplete(str(error)) from error

    def remove_locked(locked, status):
        _ensure_exited(run, directory, state=state)
        authority()
        if classify() != status:
            raise CheckoutRemovalIncomplete("清理前现场改变；保留待清理记录")
        try:
            if status in ("eligible", "stale_registration"):
                process = git.git_process(repo, "worktree", "remove", str(checkout),
                                          effect=git.CHECKOUT, pass_fds=(locked.descriptor,))
                if process.returncode:
                    detail = process.stderr.decode("utf-8", errors="replace").strip()
                    raise CheckoutRemovalIncomplete(f"git worktree remove {checkout} failed: {detail}")
            if (checkout_identity.directory_identity(checkout) is not None or str(checkout) in _registered(repo).values()
                    or checkout_identity.directory_identity(admin) == entry["identity"]["git_dir"]):
                raise CheckoutRemovalIncomplete("验证工作树尚未完全移除；保留待清理记录")
            if checkout_identity.directory_identity(root) == entry["identity"]["root"]:
                for wrapper in (checkout.parent, root):
                    try:
                        wrapper.rmdir()
                    except OSError:
                        pass
        except (HarnessError, OSError, ValueError, subprocess.TimeoutExpired) as error:
            if isinstance(error, CheckoutRemovalIncomplete):
                raise
            raise CheckoutRemovalIncomplete(str(error)) from error
        write_json(receipt_path, {**receipt, "status": "removed", "by": by, "time": time.time()})
        return {**result, "status": "removed"}

    def with_remove_lock(use):
        entered = False

        def held(locked):
            nonlocal entered
            entered = True
            return use(locked)

        try:
            return holding(files.lock(("remove.lock",), mode="exclusive" if remove else "shared",
                                      create=remove), held)
        except LockUnavailable as error:
            if entered:
                raise
            if error.outcome.state == "busy":
                raise CheckoutRemovalIncomplete("上一次删除仍在进行；保留待清理记录") from error
            if not remove and error.outcome.state == "absent" and error.outcome.step.leaf:
                return use(None)
            raise CheckoutRemovalIncomplete(str(error)) from error

    def reconcile(locked=None):
        status = classify()
        if status == "removed":
            return {**result, "status": "removed" if remove else "absent", "removed_by": previous.get("by")}
        if not remove:
            return {**result, "status": status}
        if locked is not None:
            return remove_locked(locked, status)
        write_json(receipt_path, {**receipt, "status": "pending", "by": by, "time": time.time()})
        return with_remove_lock(lambda holder: remove_locked(holder, status))

    if previous and previous["status"] == "pending":
        return with_remove_lock(reconcile)
    return reconcile()


def _branch_matches(repo, branch, expected):
    """True when the retained branch still names the run's checkpoint."""
    try:
        return git.git(repo, "rev-parse", "--verify", "refs/heads/" + branch, effect=git.READ).strip() == expected
    except HarnessError:
        return False


def _ignored_gate(checkout, binding, remove_ignored):
    """Without the option any ignored entry refuses removal; with it, the bounded listing Git will delete."""
    names = git.ignored_paths(checkout, binding=binding)
    if names and not remove_ignored:
        raise HarnessError(f"工作树有 {len(names)} 个被 Git 忽略的条目（目录折叠后）；确认可以丢弃后用 "
                           "--remove-ignored 预览、--remove --remove-ignored 删除：" + git.describe_paths(names))
    return git.bounded_paths(names) if remove_ignored else None


def _clean_worktree(repo, run, directory, original, remove, remove_input_copies, remove_ignored, *, files, read_state):
    """The run's branch worktree; its receipt is worktree-cleanup.json in the run directory."""
    checkout, branch = Path(run["worktree"]), run["branch"]
    binding = run["worktree_binding"]
    admin = Path(binding["git_dir"])
    receipt_path = directory / "worktree-cleanup.json"
    result = {"worktree": str(checkout), "branch": branch}
    if run.get("status") not in TERMINAL:
        # Policy, not a refusal: the run can still be finished or abandoned.
        return {**result, "status": "retained", "reason_kind": "finish_or_abandon_first",
                "reason": f"run 状态为 {run.get('status')}；工作树只在 run 完成（completed）"
                          "或放弃（abandoned）之后清理，请先完成或放弃该 run"}
    expected = run["expected_head"]
    # The receipt names the resource, not the whole ledger: an audited
    # --confirm-exited may rewrite the ledger between an interruption and its rerun.
    identity = {"id": run["id"], "worktree": str(checkout), "branch": branch,
                "head": expected, "binding": binding}
    previous = _read_receipt(files, ("worktree-cleanup.json",))
    if previous is not None and (any(previous.get(k) != v for k, v in identity.items())
                                 or previous.get("status") not in ("pending", "removed")):
        raise HarnessError("清理记录与运行证据不符；保留现场")
    exists = checkout.exists()
    input_summary = {"status": "none", "files": []}
    if previous and previous["status"] == "removed":
        # Either path is enough to refuse. A recreated admin may lack its backlink.
        if exists or admin.exists() or str(checkout) in _registered(repo).values():
            raise HarnessError("已清理工作树或注册重新出现；保留现场")
        return {**result, "status": "removed" if remove else "absent", "input_copies": input_summary}
    registered = str(checkout) in _registered(repo).values()
    listing = None
    if not exists:
        if registered or admin.exists() or not previous:
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
        if input_summary["files"] and input_summary["status"] != "removed":
            if not remove:
                return {**result, "status": "input_copies_present", "input_copies": input_summary}
            # Registered copies keep their own gate; --remove-ignored never stands in for it.
            raise HarnessError("登记的依赖副本仍在工作树中；加 --remove-input-copies 删除")
        git.assert_removable_worktree(checkout, binding=binding)
        listing = _ignored_gate(checkout, binding, remove_ignored)
        status = "eligible"
    result = {**result, "status": status, "input_copies": input_summary}
    if listing is not None:
        result["ignored_paths"] = listing
    if not remove:
        return result
    # The ledger digest is kept as a record of the state that authorized the removal.
    record = {**identity, "state_digest": hashlib.sha256(original).hexdigest()}
    if exists:
        # Only this command's option authorizes deleting ignored files; the listing is re-read now.
        record["remove_ignored"] = remove_ignored
        if listing is not None:
            record["ignored_paths"] = listing
    else:
        # Completing a removal an earlier command performed keeps what that command recorded.
        record.update({key: previous[key] for key in ("remove_ignored", "ignored_paths") if key in previous})
    write_json(receipt_path, {**record, "status": "pending", "time": time.time()})
    _ensure_exited(run, directory, state=files.path)
    if read_state() != original:
        raise HarnessError("运行台账在清理前改变；保留现场")
    if exists:
        if not _branch_matches(repo, branch, expected):
            raise HarnessError("保留分支在清理前改变；保留现场")
        git.check_worktree_binding(checkout, binding)
        if git.head(checkout, binding=binding) != expected:
            raise HarnessError("worktree HEAD 在清理前改变；保留现场")
        git.check_worktree_state(checkout, binding=binding)
        git.assert_removable_worktree(checkout, binding=binding)
        _ignored_gate(checkout, binding, remove_ignored)
        git.git(repo, "worktree", "remove", str(checkout), effect=git.CHECKOUT)
    if checkout.exists() or checkout.is_symlink() or admin.exists() or str(checkout) in _registered(repo).values():
        raise HarnessError("工作树尚未完全移除；保留待清理记录")
    write_json(receipt_path, {**record, "status": "removed", "time": time.time()})
    return {**result, "status": "removed"}
