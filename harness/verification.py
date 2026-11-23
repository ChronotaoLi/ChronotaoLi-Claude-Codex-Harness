"""Produce verification evidence; acceptance remains owned by task/final lifecycles."""
from __future__ import annotations

import tempfile
import time
from copy import deepcopy
from datetime import datetime, timezone
import uuid
from pathlib import Path

from . import checkout_identity, git, stops
from .context import compact_checks
from .process_runtime import defer_cleanup_signals
from .recovery import snapshot_digest
from .services import RuntimeService
from .util import (CommandDirectoryError, HarnessError, ProcessStartError, ProcessTimeout, ProgramStartError,
                   run_process, tail, with_descriptors, write_json)


def checks_failed(result):
    """Acceptance depends on complete lifecycle evidence, never code truthiness."""
    return not (result.get("status") == "passed"
                and type(result.get("exit_code")) is int and result["exit_code"] == 0
                and result.get("termination_reason") == "normal"
                and result.get("exit_confirmed") is True
                and result.get("evidence_complete") is True)


def checks_passed(results):
    return all(not checks_failed(result) for result in results)


class Verification(RuntimeService):
    def verification_directory(self, task_id, attempt, *, whole_run=False):
        root = self.context.directory / "verification"
        return root / "final" / str(attempt) if whole_run else root / "tasks" / task_id / str(attempt)

    def run_verification_commands(self, task_id, commands, cwd, folder, binding, *, phase=None):
        folder.mkdir(parents=True, exist_ok=True)
        results = []
        write_json(folder / "results.json", results)
        for i, command in enumerate(commands):
            self.context.ensure_exited()
            # The binding is checked on the very root the command starts from (rule 12).
            def run_check(held):
                root_fd = git.open_bound_root(held, cwd, binding)
                log = folder / f"check-{i}"
                record = log.with_suffix(".outcome.json").relative_to(self.context.directory)
                entry = {"id": str(uuid.uuid4()), "role": "verification", "task_id": task_id,
                         "status": "running", "pgid": None, "process_state": "starting",
                         "process_record": record.as_posix()}
                entry["worktree"] = str(cwd)
                if phase:
                    entry["phase"] = phase
                self.context.run["dispatches"].append(entry)
                self.context.save()

                def on_start(pgid):
                    entry["pgid"] = pgid
                    entry["process_state"] = "running"
                    self.context.save()

                started = time.monotonic()
                entry["started_at"] = datetime.now(timezone.utc).isoformat()
                outcome = None
                error = None
                code = None

                def on_outcome(value):
                    nonlocal outcome
                    outcome = value

                def report(failure):
                    # Every error that leaves this command, the exit gate's included, is its feedback (rule 11).
                    self.context.run["feedback"] = {"task_id": task_id,
                        "verification_error": {"command": deepcopy(command), "error": str(failure), "log": str(log)}}
                    self.context.save_fact("feedback")

                try:
                    code = run_process(command["argv"], cwd, log, relative_cwd=command["cwd"], root_fd=root_fd,
                        timeout=self.context.config.data["runtime"]["verify_timeout_seconds"],
                        on_start=on_start, on_outcome=on_outcome, record_owner=entry["id"])
                except BaseException as caught:
                    error = caught
                    outcome = getattr(caught, "outcome", outcome)
                finally:
                    interrupted = isinstance(error, (KeyboardInterrupt, SystemExit))
                    facts = outcome.to_dict() if outcome is not None else {
                        "exit_code": code, "exit_confirmed": False, "evidence_complete": False,
                        "termination_reason": ("cancelled" if isinstance(error, KeyboardInterrupt)
                                               else "interrupted" if interrupted else "error"),
                        "cleanup_errors": [], "cleanup_actions": [], "errors": []}
                    code = facts["exit_code"]
                    reason = facts["termination_reason"]
                    status = ("interrupted" if interrupted or reason in {"cancelled", "interrupted"}
                              else "passed" if code == 0 and reason == "normal"
                              and facts["exit_confirmed"] and facts["evidence_complete"] and error is None
                              else "timed_out" if reason == "timeout"
                              else "failed" if facts["exit_confirmed"] else "unknown")
                    entry.update(status="completed" if status == "passed" else status,
                        command=deepcopy(command), exit_code=code, termination_reason=reason,
                        process_outcome=facts, exit_confirmed=facts["exit_confirmed"],
                        evidence_complete=facts["evidence_complete"],
                        duration_seconds=time.monotonic() - started,
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        timing_scope="formal_verification_command_including_cleanup")
                    if entry["exit_confirmed"]:
                        entry["process_state"] = "exited"
                    result = {"command": deepcopy(command), "exit_code": code, "status": status,
                        "termination_reason": reason, "exit_confirmed": facts["exit_confirmed"],
                        "evidence_complete": facts["evidence_complete"], "process_outcome": facts,
                        "stdout": "", "stderr": "", "log": str(log), "timed_out": reason == "timeout",
                        "duration_seconds": entry["duration_seconds"], "started_at": entry["started_at"],
                        "finished_at": entry["finished_at"],
                        "timing_source": {"dispatch_id": entry["id"], "scope": entry["timing_scope"]}}
                    if phase:
                        result["phase"] = phase
                    results.append(result)
                    # Persist the receipt independently of the run ledger. A failed
                    # save must still leave the owned process and partial results inspectable.
                    persistence_error = None
                    incomplete_status = "interrupted" if status == "interrupted" else "failed"
                    try:
                        result.update(stdout=tail(log.with_suffix(".stdout")), stderr=tail(log.with_suffix(".stderr")))
                    except BaseException as caught:
                        result["evidence_complete"] = False
                        result["status"] = incomplete_status
                        persistence_error = caught
                    try:
                        write_json(folder / "results.json", results)
                    except BaseException as caught:
                        persistence_error = persistence_error or caught
                    try:
                        self.context.save()
                    except BaseException as caught:
                        persistence_error = persistence_error or caught
                    if persistence_error is not None:
                        result.update(status=incomplete_status, evidence_complete=False,
                                      persistence_error=str(persistence_error))
                        entry.update(status=incomplete_status, evidence_complete=False,
                                     persistence_error=str(persistence_error))
                        # Either destination can fail independently. Retry once with
                        # the incomplete-evidence fact, without masking the cause.
                        for persist in (lambda: write_json(folder / "results.json", results), self.context.save):
                            try:
                                persist()
                            except BaseException:
                                pass
                    if error is None:
                        error = persistence_error
                # Interrupts keep their own stop (K6-1). Otherwise process facts take
                # precedence over verification writes and local startup faults.
                from .dispatch import process_stop
                stop = process_stop(error, entry["id"]) if error is not None and not interrupted else None
                check_failure = isinstance(error, (ProcessTimeout, CommandDirectoryError, ProgramStartError))
                replaced = check_failure and persistence_error is not None
                if replaced:
                    persistence_error.__cause__ = error
                    error = persistence_error
                recordable = check_failure and not replaced and (stop is None or stop["kind"] != "evidence")
                if error is not None and not recordable:
                    # The feedback names the very error that leaves, so it is decided first.
                    # A replaced write error carries the process facts' stop even when it has its own.
                    if stop is not None and (replaced or not (isinstance(error, HarnessError) and error.stop is not None)):
                        if isinstance(error, HarnessError) and error.stop is None:
                            error.stop = stop
                        else:
                            typed = HarnessError(str(error) or type(error).__name__, stop=stop)
                            typed.outcome = outcome
                            report(typed)
                            raise typed from error
                    if not check_failure and isinstance(error, ProcessStartError) and error.stop is None:
                        cause = error.__cause__
                        if isinstance(cause, ValueError):
                            error.stop = stops.make("runtime_failure", "invariant")
                        else:
                            error.stop = stops.make("runtime_failure", "io", data={"error_type": type(cause).__name__})
                    report(error)
                    raise error
                try:
                    self.context.ensure_exited()
                    if not result["exit_confirmed"]:
                        raise HarnessError("Verification process exit is unknown; inspect " + str(log), stop=stops.make("process_unconfirmed", "dispatch_exit", data={"returned_unsealed": False}, source={"dispatch_id": entry["id"]}))
                except BaseException as gate:
                    report(gate)
                    raise
                return phase == "setup" and checks_failed(result)

            if with_descriptors(run_check):
                break
        return results

    def verify(self, task_id, attempt, *, whole_run=False, artifact_dir=None, before=None, round_index=None):
        folder = Path(artifact_dir) if artifact_dir is not None else self.verification_directory(task_id, attempt, whole_run=whole_run)
        folder.mkdir(parents=True, exist_ok=True)
        # Callers may pass a just-computed snapshot only when no model or
        # verification process has run since. The exit gate always rehashes files.
        before = self.context.snapshot() if before is None else before
        if whole_run and self.context.config.data["verification"]["fresh_checkout"]:
            return self.verify_fresh_checkout(task_id, folder, before, {
                "attempt": attempt, "round": round_index})
        results = self.run_verification_commands(task_id, self.context.config.data["verification"]["commands"],
                                                  self.context.worktree, folder, self.context.run["worktree_binding"])
        after = self.context.snapshot()
        if after != before:
            changes = git.snapshot_changes(before, after)
            write_json(folder / "file-changes.json", changes)
            self.context.run["feedback"] = {"task_id": task_id, "verification": compact_checks(results),
                "verification_changes": {**changes, "artifact": str(folder / "file-changes.json")}}
            self.context.save_fact("feedback")
            raise HarnessError("验证命令修改了待审阅文件；验证必须为只读检查。"
                               f"新增未跟踪文件：{changes['added']}；修改：{changes['modified']}；删除：{changes['deleted']}。"
                               "请配置构建/测试输出位置或显式忽略生成产物；不自动豁免文件。"
                               "运行中可在公共 Git 目录的 info/exclude 精确忽略新生成文件；"
                               "这会改变后续快照可见范围，不豁免已跟踪文件，请先核对文件内容。", stop=stops.make("verification", "checks_modified_files", data={"scope": "task", "phase": "checks"}, source={"task_id": task_id, "artifact": str(folder / "file-changes.json")}))
        write_json(folder / "results.json", results)
        return results

    def verify_fresh_checkout(self, task_id, folder, source_snapshot, owner):
        """Verify committed bytes in a new external checkout with explicit setup."""
        self.context.store.check_current()
        self.context.ensure_exited()
        git.check_worktree_binding(self.context.worktree, self.context.run["worktree_binding"])
        root = Path(tempfile.mkdtemp(prefix="harness-verify-")).resolve()
        if root.is_relative_to(self.context.repo.resolve()):
            root.rmdir()
            raise HarnessError("全新整体验证要求临时目录位于目标仓库之外；请设置外部 TMPDIR")
        root_id = checkout_identity.directory_identity(root)
        checkout = root / ".harness" / "checkout"
        artifact = folder / "checkout.json"
        run = self.context.run
        commit = run["final_history"][owner["attempt"] - 1]["head"]
        environment = {"mode": "fresh_checkout", "worktree": str(checkout),
                       "head": commit, "status": "preparing",
                       "setup_commands": self.context.config.data["verification"]["setup_commands"]}
        write_json(artifact, environment)
        checkout.parent.mkdir()
        checkout.mkdir()
        checkout_id = checkout_identity.directory_identity(checkout)
        with defer_cleanup_signals(inherit=False):
            binding = git.add_worktree(self.context.worktree, checkout, commit, binding=run["worktree_binding"])
            with stops.classify("workspace_mismatch", "binding_changed"):
                if binding.get("detached_head") != commit:
                    raise HarnessError("全新验证检出未绑定至本次尝试的提交")
                git.check_worktree_binding(checkout, binding)
                identity = {"root": checkout_identity.directory_identity(root),
                            "worktree": checkout_identity.directory_identity(checkout),
                            "git_dir": checkout_identity.directory_identity(Path(binding["git_dir"]))}
                if identity["root"] != root_id or identity["worktree"] != checkout_id:
                    raise HarnessError("全新验证检出的目录身份已改变")
                entry = checkout_identity.build_entry(owner, root, checkout, identity, binding)
                entries = run["verification_checkouts"]
                checkout_identity.validate_checkout_entries({**run, "verification_checkouts": [*entries, entry]})
            entries.append(entry)
            self.context.save()
        baseline = git.snapshot(checkout, binding=binding)
        setup = self.run_verification_commands(task_id, environment["setup_commands"], checkout,
                                               folder / "setup", binding, phase="setup")
        if git.snapshot(checkout, binding=binding) != baseline:
            raise HarnessError(f"依赖准备修改了全新检出的受检文件；证据：{artifact}", stop=stops.make("verification", "checks_modified_files", data={"scope": "final", "phase": "setup"}, source={"artifact": str(artifact)}))
        if not checks_passed(setup):
            environment["status"] = "setup_failed"
            write_json(artifact, environment)
            raise HarnessError(f"整体验证依赖准备失败；修复环境后 resume 将在另一全新检出重试。证据：{folder / 'setup/results.json'}", stop=stops.make("verification", "setup_failed", data={"attempt": owner["attempt"]}, source={"artifact": str(folder / "setup/results.json")}))
        results = self.run_verification_commands(task_id, self.context.config.data["verification"]["commands"],
                                                 checkout, folder, binding)
        for result in results:
            result.update(worktree=str(checkout), checkout_artifact=str(artifact))
        after = git.snapshot(checkout, binding=binding)
        if after != baseline:
            write_json(folder / "file-changes.json", git.snapshot_changes(baseline, after))
            raise HarnessError(f"整体验证修改了全新检出的受检文件；证据：{folder / 'file-changes.json'}", stop=stops.make("verification", "checks_modified_files", data={"scope": "final", "phase": "checks"}, source={"artifact": str(folder / "file-changes.json")}))
        if self.context.snapshot() != source_snapshot:
            raise HarnessError("整体验证期间原工作树发生变化；不能接受旧候选", stop=stops.make("workspace_mismatch", "candidate_changed"))
        environment.update(status="verification_failed" if not checks_passed(results) else "verified",
                           snapshot_digest=snapshot_digest(after),
                           setup_artifact=str(folder / "setup/results.json"))
        write_json(artifact, environment)
        write_json(folder / "results.json", results)
        return results
