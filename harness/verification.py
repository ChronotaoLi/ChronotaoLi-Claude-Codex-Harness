"""Produce verification evidence; acceptance remains owned by task/final lifecycles."""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from . import git
from .context import compact_checks
from .recovery import snapshot_digest
from .services import RuntimeService
from .util import group_alive, HarnessError, ProcessTimeout, run_process, tail, write_json


class Verification(RuntimeService):
    def verification_directory(self, task_id, attempt, *, whole_run=False):
        runtime = self.runtime
        root = runtime.directory / "verification"
        return root / "final" / str(attempt) if whole_run else root / "tasks" / task_id / str(attempt)

    def run_verification_commands(self, task_id, commands, cwd, folder, binding, *, phase=None):
        runtime = self.runtime
        folder.mkdir(parents=True, exist_ok=True)
        results = []
        write_json(folder / "results.json", results)
        for i, command in enumerate(commands):
            runtime.ensure_exited()
            git.check_worktree_binding(cwd, binding)
            log = folder / f"check-{i}"
            entry = {"id": str(uuid.uuid4()), "role": "verification", "task_id": task_id,
                     "status": "running", "pgid": None, "process_state": "starting"}
            entry["worktree"] = str(cwd)
            if phase:
                entry["phase"] = phase
            runtime.run["dispatches"].append(entry)
            runtime.save()

            def on_start(pgid):
                entry["pgid"] = pgid
                entry["process_state"] = "running"
                runtime.save()

            timed_out = False
            startup_interrupted = False
            try:
                code = run_process(command, cwd, log, timeout=runtime.config.data["runtime"]["verify_timeout_seconds"], on_start=on_start)
                entry["status"] = "completed" if code == 0 else "failed"
            except ProcessTimeout:
                code, timed_out = 124, True
                entry["status"] = "timed_out"
            except BaseException as error:
                startup_interrupted = (isinstance(error, (KeyboardInterrupt, SystemExit))
                                       and entry["pgid"] is None)
                entry["status"] = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
                runtime.run["feedback"] = {"task_id": task_id,
                    "verification_error": {"argv": command, "error": str(error), "log": str(log)}}
                raise
            finally:
                entry["exit_confirmed"] = (not startup_interrupted
                                           and (not entry["pgid"] or not group_alive(entry["pgid"])))
                if entry["exit_confirmed"]:
                    entry["process_state"] = "exited"
                runtime.save()
            results.append({"argv": command, "exit_code": code, "stdout": tail(log.with_suffix(".stdout")),
                            "stderr": tail(log.with_suffix(".stderr")), "log": str(log), "timed_out": timed_out})
            if phase:
                results[-1]["phase"] = phase
            write_json(folder / "results.json", results)
            runtime.ensure_exited()
            if phase == "setup" and code:
                break
        return results

    def verify(self, task_id, attempt, *, whole_run=False, artifact_dir=None, before=None):
        runtime = self.runtime
        folder = Path(artifact_dir) if artifact_dir is not None else runtime.verification_directory(task_id, attempt, whole_run=whole_run)
        folder.mkdir(parents=True, exist_ok=True)
        # Callers may pass a just-computed snapshot only when no hook, model or
        # verification process has run since. The exit gate always rehashes files.
        before = runtime.snapshot() if before is None else before
        if whole_run and runtime.config.data["verification"]["fresh_checkout"]:
            return runtime.verify_fresh_checkout(task_id, folder, before)
        runtime.emit_hook("pre_verify", {"task_id": task_id, "worktree": str(runtime.worktree),
                                      "scope": "run" if whole_run else "task"}, folder)
        results = runtime.run_verification_commands(task_id, runtime.config.data["verification"]["commands"],
                                                  runtime.worktree, folder, runtime.run["worktree_binding"])
        runtime.emit_hook("post_verify", {"task_id": task_id, "results": results}, folder)
        after = runtime.snapshot()
        if after != before:
            changes = git.snapshot_changes(before, after)
            write_json(folder / "file-changes.json", changes)
            runtime.run["feedback"] = {"task_id": task_id, "verification": compact_checks(results),
                "verification_changes": {**changes, "artifact": str(folder / "file-changes.json")}}
            raise HarnessError("验证命令修改了待审阅文件；验证必须为只读检查。"
                               f"新增未跟踪文件：{changes['added']}；修改：{changes['modified']}；删除：{changes['deleted']}。"
                               "请配置构建/测试输出位置或显式忽略生成产物；不自动豁免文件。"
                               "运行中可在公共 Git 目录的 info/exclude 精确忽略新生成文件；"
                               "这会改变后续快照可见范围，不豁免已跟踪文件，请先核对文件内容。")
        runtime.record_ignored_paths("verification", folder)
        write_json(folder / "results.json", results)
        return results

    def verify_fresh_checkout(self, task_id, folder, source_snapshot):
        """Verify committed bytes in a new external checkout with explicit setup."""
        runtime = self.runtime
        runtime.ensure_exited()
        git.check_worktree_binding(runtime.worktree, runtime.run["worktree_binding"])
        root = Path(tempfile.mkdtemp(prefix="harness-verify-")).resolve()
        if root.is_relative_to(runtime.repo.resolve()):
            root.rmdir()
            raise HarnessError("全新整体验证要求临时目录位于目标仓库之外；请设置外部 TMPDIR")
        checkout = root / ".harness" / "checkout"
        artifact = folder / "checkout.json"
        environment = {"mode": "fresh_checkout", "worktree": str(checkout),
                       "head": runtime.run["expected_head"], "status": "preparing",
                       "setup_commands": runtime.config.data["verification"]["setup_commands"]}
        write_json(artifact, environment)
        git.git(runtime.worktree, "worktree", "add", "--detach", str(checkout), environment["head"],
                binding=runtime.run["worktree_binding"])
        binding = git.capture_worktree_binding(checkout, None)
        environment["binding"] = binding
        write_json(artifact, environment)
        baseline = git.snapshot(checkout, binding=binding)
        runtime.emit_hook("pre_verify", {"task_id": task_id, "worktree": str(checkout),
                                      "source_worktree": str(runtime.worktree), "scope": "run",
                                      "mode": "fresh_checkout"}, folder)
        setup = runtime.run_verification_commands(task_id, environment["setup_commands"], checkout,
                                               folder / "setup", binding, phase="setup")
        if git.snapshot(checkout, binding=binding) != baseline:
            raise HarnessError(f"依赖准备或 pre_verify 修改了全新检出的受检文件；证据：{artifact}")
        if any(result["exit_code"] for result in setup):
            environment["status"] = "setup_failed"
            write_json(artifact, environment)
            raise HarnessError(f"整体验证依赖准备失败；修复环境后 resume 将在另一全新检出重试。证据：{folder / 'setup/results.json'}")
        results = runtime.run_verification_commands(task_id, runtime.config.data["verification"]["commands"],
                                                 checkout, folder, binding)
        for result in results:
            result.update(worktree=str(checkout), checkout_artifact=str(artifact))
        runtime.emit_hook("post_verify", {"task_id": task_id, "results": results,
                                       "worktree": str(checkout), "source_worktree": str(runtime.worktree)}, folder)
        after = git.snapshot(checkout, binding=binding)
        if after != baseline:
            write_json(folder / "file-changes.json", git.snapshot_changes(baseline, after))
            raise HarnessError(f"整体验证修改了全新检出的受检文件；证据：{folder / 'file-changes.json'}")
        if runtime.snapshot() != source_snapshot:
            raise HarnessError("整体验证期间原工作树发生变化；不能接受旧候选")
        environment.update(status="verification_failed" if any(result["exit_code"] for result in results) else "verified",
                           snapshot_digest=snapshot_digest(after),
                           setup_artifact=str(folder / "setup/results.json"))
        write_json(artifact, environment)
        write_json(folder / "results.json", results)
        runtime.record_ignored_paths("final_source", folder)
        return results
