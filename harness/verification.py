"""Produce verification evidence; acceptance remains owned by task/final lifecycles."""
from __future__ import annotations

import tempfile
import time
from datetime import datetime, timezone
import uuid
from pathlib import Path

from . import git
from .context import compact_checks
from .recovery import snapshot_digest
from .services import RuntimeService
from .util import HarnessError, ProcessTimeout, run_process, tail, write_json


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
            git.check_worktree_binding(cwd, binding)
            log = folder / f"check-{i}"
            entry = {"id": str(uuid.uuid4()), "role": "verification", "task_id": task_id,
                     "status": "running", "pgid": None, "process_state": "starting"}
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

            try:
                code = run_process(command, cwd, log,
                    timeout=self.context.config.data["runtime"]["verify_timeout_seconds"],
                    on_start=on_start, on_outcome=on_outcome)
            except BaseException as caught:
                error = caught
                outcome = getattr(caught, "outcome", outcome)
            finally:
                facts = outcome.to_dict() if outcome is not None else {
                    "exit_code": code, "exit_confirmed": False, "evidence_complete": False,
                    "termination_reason": "cancelled" if isinstance(error, KeyboardInterrupt) else "error",
                    "cleanup_errors": [], "cleanup_actions": [], "errors": []}
                code = facts["exit_code"]
                reason = facts["termination_reason"]
                status = ("passed" if code == 0 and reason == "normal"
                          and facts["exit_confirmed"] and facts["evidence_complete"] and error is None
                          else "timed_out" if reason == "timeout"
                          else "interrupted" if reason in {"cancelled", "interrupted"}
                          else "failed" if facts["exit_confirmed"] else "unknown")
                entry.update(status="completed" if status == "passed" else status,
                    argv=list(command), exit_code=code, termination_reason=reason,
                    process_outcome=facts, exit_confirmed=facts["exit_confirmed"],
                    evidence_complete=facts["evidence_complete"],
                    duration_seconds=time.monotonic() - started,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    timing_scope="formal_verification_command_including_cleanup")
                if entry["exit_confirmed"]:
                    entry["process_state"] = "exited"
                result = {"argv": list(command), "exit_code": code, "status": status,
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
                try:
                    result.update(stdout=tail(log.with_suffix(".stdout")), stderr=tail(log.with_suffix(".stderr")))
                except BaseException as caught:
                    result["evidence_complete"] = False
                    result["status"] = "failed"
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
                    result.update(status="failed", evidence_complete=False,
                                  persistence_error=str(persistence_error))
                    entry.update(status="failed", evidence_complete=False,
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
            if error is not None and not isinstance(error, ProcessTimeout):
                self.context.run["feedback"] = {"task_id": task_id,
                    "verification_error": {"argv": command, "error": str(error), "log": str(log)}}
                raise error
            self.context.ensure_exited()
            if not result["exit_confirmed"]:
                raise HarnessError("Verification process exit is unknown; inspect " + str(log))
            if phase == "setup" and checks_failed(result):
                break
        return results

    def verify(self, task_id, attempt, *, whole_run=False, artifact_dir=None, before=None):
        folder = Path(artifact_dir) if artifact_dir is not None else self.verification_directory(task_id, attempt, whole_run=whole_run)
        folder.mkdir(parents=True, exist_ok=True)
        # Callers may pass a just-computed snapshot only when no hook, model or
        # verification process has run since. The exit gate always rehashes files.
        before = self.context.snapshot() if before is None else before
        if whole_run and self.context.config.data["verification"]["fresh_checkout"]:
            return self.verify_fresh_checkout(task_id, folder, before)
        self.context.emit_hook("pre_verify", {"task_id": task_id, "worktree": str(self.context.worktree),
                                      "scope": "run" if whole_run else "task"}, folder)
        results = self.run_verification_commands(task_id, self.context.config.data["verification"]["commands"],
                                                  self.context.worktree, folder, self.context.run["worktree_binding"])
        self.context.emit_hook("post_verify", {"task_id": task_id, "results": results}, folder)
        after = self.context.snapshot()
        if after != before:
            changes = git.snapshot_changes(before, after)
            write_json(folder / "file-changes.json", changes)
            self.context.run["feedback"] = {"task_id": task_id, "verification": compact_checks(results),
                "verification_changes": {**changes, "artifact": str(folder / "file-changes.json")}}
            raise HarnessError("验证命令修改了待审阅文件；验证必须为只读检查。"
                               f"新增未跟踪文件：{changes['added']}；修改：{changes['modified']}；删除：{changes['deleted']}。"
                               "请配置构建/测试输出位置或显式忽略生成产物；不自动豁免文件。"
                               "运行中可在公共 Git 目录的 info/exclude 精确忽略新生成文件；"
                               "这会改变后续快照可见范围，不豁免已跟踪文件，请先核对文件内容。")
        self.context.record_ignored_paths("verification", folder)
        write_json(folder / "results.json", results)
        return results

    def verify_fresh_checkout(self, task_id, folder, source_snapshot):
        """Verify committed bytes in a new external checkout with explicit setup."""
        self.context.store.check_current()
        self.context.ensure_exited()
        git.check_worktree_binding(self.context.worktree, self.context.run["worktree_binding"])
        root = Path(tempfile.mkdtemp(prefix="harness-verify-")).resolve()
        if root.is_relative_to(self.context.repo.resolve()):
            root.rmdir()
            raise HarnessError("全新整体验证要求临时目录位于目标仓库之外；请设置外部 TMPDIR")
        checkout = root / ".harness" / "checkout"
        artifact = folder / "checkout.json"
        environment = {"mode": "fresh_checkout", "worktree": str(checkout),
                       "head": self.context.run["expected_head"], "status": "preparing",
                       "setup_commands": self.context.config.data["verification"]["setup_commands"]}
        write_json(artifact, environment)
        git.git(self.context.worktree, "worktree", "add", "--detach", str(checkout), environment["head"],
                binding=self.context.run["worktree_binding"])
        binding = git.capture_worktree_binding(checkout, None)
        environment["binding"] = binding
        write_json(artifact, environment)
        baseline = git.snapshot(checkout, binding=binding)
        self.context.emit_hook("pre_verify", {"task_id": task_id, "worktree": str(checkout),
                                      "source_worktree": str(self.context.worktree), "scope": "run",
                                      "mode": "fresh_checkout"}, folder)
        setup = self.run_verification_commands(task_id, environment["setup_commands"], checkout,
                                               folder / "setup", binding, phase="setup")
        if git.snapshot(checkout, binding=binding) != baseline:
            raise HarnessError(f"依赖准备或 pre_verify 修改了全新检出的受检文件；证据：{artifact}")
        if not checks_passed(setup):
            environment["status"] = "setup_failed"
            write_json(artifact, environment)
            raise HarnessError(f"整体验证依赖准备失败；修复环境后 resume 将在另一全新检出重试。证据：{folder / 'setup/results.json'}")
        results = self.run_verification_commands(task_id, self.context.config.data["verification"]["commands"],
                                                 checkout, folder, binding)
        for result in results:
            result.update(worktree=str(checkout), checkout_artifact=str(artifact))
        self.context.emit_hook("post_verify", {"task_id": task_id, "results": results,
                                       "worktree": str(checkout), "source_worktree": str(self.context.worktree)}, folder)
        after = git.snapshot(checkout, binding=binding)
        if after != baseline:
            write_json(folder / "file-changes.json", git.snapshot_changes(baseline, after))
            raise HarnessError(f"整体验证修改了全新检出的受检文件；证据：{folder / 'file-changes.json'}")
        if self.context.snapshot() != source_snapshot:
            raise HarnessError("整体验证期间原工作树发生变化；不能接受旧候选")
        environment.update(status="verification_failed" if not checks_passed(results) else "verified",
                           snapshot_digest=snapshot_digest(after),
                           setup_artifact=str(folder / "setup/results.json"))
        write_json(artifact, environment)
        write_json(folder / "results.json", results)
        self.context.record_ignored_paths("final_source", folder)
        return results
