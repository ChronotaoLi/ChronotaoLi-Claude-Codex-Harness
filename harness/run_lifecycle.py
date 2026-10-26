"""Own run creation, loading, recovery preflight and abandonment."""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

from . import git
from .services import RuntimeService
from .shared import asset_digest, validate_mcp
from .util import HarnessError, lock, read_json, uid, validate_command_directories


class RunLifecycle(RuntimeService):
    def start(self, goal, plan=None, *, pause_after_plan=False, step=False):
        runtime = self.runtime
        if not goal.strip():
            raise HarnessError("目标不能为空")
        untracked = git.check_starting_checkout(runtime.repo)
        if not runtime.config.data["verification"]["commands"]:
            raise HarnessError("请先配置目标项目的 verification.commands")
        git.reject_submodules(runtime.repo)
        supplied_tasks = runtime.validate_tasks(plan) if plan is not None else []
        validate_mcp(runtime.config)
        if untracked:
            print("[harness] 主检出存在未跟踪文件；保留原处，不进入隔离 worktree："
                  + git.describe_paths(untracked), file=sys.stderr, flush=True)
        ident = uid("run")
        worktree = runtime.state / "worktrees" / ident
        runtime.run = {"schema_version": 11, "id": ident, "repo": str(runtime.repo), "goal": goal, "status": "planning", "commander": runtime.commander,
                    "branch": "harness/" + ident, "worktree": str(worktree), "tasks": [], "accepted": {}, "attempts": {},
                    "attempt_budget_policy": "work",
                    "dispatches": [], "expected_head": git.head(runtime.repo), "feedback": None,
                    "config_path": str(runtime.config.path), "config_snapshot": deepcopy(runtime.config.data),
                    "asset_digest": asset_digest(runtime.config), "base_commit": git.head(runtime.repo),
                    "original_tasks": deepcopy(supplied_tasks),
                    "total_attempts": 0, "replan_attempts": 0, "final_attempts": 0,
                    "review_history": {}, "final_history": [], "plan_history": [], "format_repairs": {},
                    "review_corrections": {}, "review_receipts": {}, "commander_receipts": {},
                    "invalid_repairs": {}, "approved_scope": [],
                    "environment_updates": [], "environment_checks": [],
                    "decision_requests": [], "operator_decisions": [], "task_checkpoints": [], "checkpoint_batches": [],
                    "worktree_binding": {}, "ignored_baseline": [],
                    "pending_format_corrections": {}, "agent_dispatches": 0,
                    "plan_approval_required": pause_after_plan, "plan_approved": not pause_after_plan}
        runtime.directory.mkdir(parents=True)
        runtime.save()
        with lock(runtime.directory / "run.lock"):
            try:
                git.git(runtime.repo, "worktree", "add", "-b", runtime.run["branch"], str(worktree), "HEAD")
                runtime.run["worktree_binding"] = git.capture_worktree_binding(worktree, runtime.run["branch"])
                runtime.run["ignored_baseline"] = runtime.ignored_paths()
                runtime.save()
                runtime.record_ignored_paths("initial", runtime.directory)
            except BaseException as error:
                runtime.stop(error)
                raise
            runtime.memory.repo = runtime.worktree
            runtime.memory.binding = runtime.run["worktree_binding"]
            return runtime.execute(plan, step=step)

    def load_run(self, ident):
        runtime = self.runtime
        if not ident.startswith("run_") or not ident[4:].isalnum():
            raise HarnessError("无效 run ID")
        runtime.run = read_json(runtime.state / "runs" / ident / "state.json")
        if not isinstance(runtime.run, dict):
            raise HarnessError("运行台账必须为对象")
        if runtime.run.get("schema_version") not in (8, 9, 10, 11):
            raise HarnessError("旧 run 状态格式无法安全恢复；请保留证据并使用原版本完成，或开始新 run")
        required = {"id": str, "repo": str, "worktree": str, "status": str, "commander": str,
                    "goal": str, "branch": str, "expected_head": str, "base_commit": str,
                    "config_snapshot": dict, "tasks": list, "original_tasks": list,
                    "accepted": dict, "attempts": dict, "dispatches": list,
                    "review_history": dict, "review_corrections": dict, "review_receipts": dict,
                    "commander_receipts": dict, "worktree_binding": dict, "ignored_baseline": list,
                    "invalid_repairs": dict, "approved_scope": list,
                    "pending_format_corrections": dict,
                    "final_history": list, "plan_history": list, "format_repairs": dict,
                    "total_attempts": int, "agent_dispatches": int, "final_attempts": int,
                    "replan_attempts": int, "plan_approval_required": bool, "plan_approved": bool}
        if runtime.run["schema_version"] >= 9:
            required.update(environment_updates=list, environment_checks=list)
        if runtime.run["schema_version"] >= 10:
            required.update(decision_requests=list, operator_decisions=list, task_checkpoints=list)
        if (runtime.run.get("id") != ident or any(type(runtime.run.get(k)) is not t for k, t in required.items())
                or any(not isinstance(d, dict) for d in runtime.run["dispatches"])):
            raise HarnessError("运行台账字段缺失或类型无效；保留原文件并核对证据")
        if runtime.run["repo"] != str(runtime.repo):
            raise HarnessError("Run 属于另一个项目")
        validate_command_directories(runtime.run["dispatches"], runtime.directory)
        runtime.validate_recovery_links()
        runtime.validate_attempt_accounting()
        runtime.validate_environment_links()
        runtime.validate_decision_links()
        runtime.validate_checkpoint_links()

    def validate_recovery_links(self):
        """Reject inconsistent recovery links before any resumed side effects.

        This checks ledger structure, not artifact integrity or candidate validity;
        the existing phase-specific gates still validate those when consumed.
        """
        runtime = self.runtime
        def fail(field):
            raise HarnessError(f"运行台账恢复状态无效：{field}；保留原文件并核对证据")

        def mapping(value, field):
            if not isinstance(value, dict):
                fail(field)
            return value

        for field in ("pending_task", "pending_acceptance", "pending_post_accept", "pending_replan",
                      "pending_directive", "worker_recovery", "replan_stopped"):
            if field in runtime.run:
                mapping(runtime.run[field], field)
        if "pending_task" in runtime.run and "pending_replan" in runtime.run:
            fail("pending_task 与 pending_replan 不能同时存在")

        task_ids = set()
        for index, task in enumerate(runtime.run["tasks"]):
            field = f"tasks[{index}]"
            ident = mapping(task, field).get("id")
            if not isinstance(ident, str) or not ident or ident in task_ids:
                fail(field + ".id 缺失、类型无效或重复")
            task_ids.add(ident)

        def task_reference(ident, field):
            if not isinstance(ident, str) or ident not in task_ids:
                fail(field + " 必须引用现有任务的字符串 ID")

        for field in ("pending_task", "pending_acceptance"):
            if field in runtime.run:
                task_reference(runtime.run[field].get("task_id"), field + ".task_id")
        if "pending_post_accept" in runtime.run:
            field = "pending_post_accept.payload"
            payload = mapping(runtime.run["pending_post_accept"].get("payload"), field)
            task_reference(payload.get("task_id"), field + ".task_id")
        if runtime.run.get("current_task") is not None:
            task_reference(runtime.run["current_task"], "current_task")

        dispatches = {}
        for entry in runtime.run["dispatches"]:
            if "invoked" in entry and type(entry["invoked"]) is not bool:
                fail("dispatches.invoked 必须是布尔值")
            ident = entry.get("id")
            if isinstance(ident, str):
                if ident in dispatches:
                    fail("dispatches 的 ID 重复")
                dispatches[ident] = entry

        def reference(ident, key, field):
            if not isinstance(ident, str) or ident not in dispatches:
                fail(field + " 引用了不存在的 dispatch")
            entry = dispatches[ident]
            if key != f"{entry.get('role')}:{entry.get('kind')}:{entry.get('task_id')}":
                fail(field + " 与 dispatch 的角色、类型或任务不符")

        def source(value, key, field):
            value = mapping(value, field)
            reference(value.get("dispatch_id"), key, field)

        for field, role in (("review_receipts", "reviewer"), ("commander_receipts", "commander")):
            for key, receipt in runtime.run[field].items():
                if not key.startswith(role + ":"):
                    fail(field + "." + key)
                source(receipt, key, field + "." + key)
        for key, guard in runtime.run["review_corrections"].items():
            field = "review_corrections." + key
            artifact = mapping(guard, field).get("artifact")
            if not key.startswith("reviewer:") or not isinstance(artifact, str):
                fail(field)
            # Existing v8 guards identify the original dispatch by its artifact path.
            reference(Path(artifact).parent.name, key, field)
        for key, pending in runtime.run["pending_format_corrections"].items():
            field = "pending_format_corrections." + key
            pending = mapping(pending, field)
            if "dispatch_id" in pending:
                reference(pending["dispatch_id"], key, field)
            correction = mapping(pending.get("correction"), field + ".correction")
            if "previous_dispatch" in correction:
                reference(correction["previous_dispatch"], key, field + ".correction")
            if "source" in correction:
                source(correction["source"], key, field + ".correction.source")
        for key, repair in runtime.run["invalid_repairs"].items():
            field = "invalid_repairs." + key
            repair = mapping(repair, field)
            source(repair.get("source"), key, field + ".source")
            if "original" in repair:
                source(repair["original"], key, field + ".original")
            if "corrected_dispatch_id" in repair:
                reference(repair["corrected_dispatch_id"], key, field)
        for tid, recovery in runtime.run.get("worker_recovery", {}).items():
            source(recovery, f"worker:implement:{tid}", "worker_recovery." + tid)

    def abandon(self, ident, *, remove_worktree=False, confirm_exited=()):
        runtime = self.runtime
        runtime.load_run(ident)
        with lock(runtime.directory / "run.lock"):
            # Reload under the lock so a concurrent completion cannot be lost.
            runtime.load_run(ident)
            if runtime.run["status"] == "completed":
                raise HarnessError("已完成的 run 不能标记为放弃")
            runtime.confirm_exited(confirm_exited)
            runtime.ensure_exited()
            removal_finished = False
            if remove_worktree and not runtime.run.get("worktree_removed"):
                expected = (runtime.state / "worktrees" / ident).resolve()
                if runtime.worktree.resolve() != expected or runtime.worktree.is_symlink():
                    raise HarnessError("只能移除该 run 所有的隔离 worktree")
                if not runtime.worktree.exists() and runtime.run.get("pending_worktree_removal"):
                    registered = {field.removeprefix("worktree ") for field in
                                  git.git(runtime.repo, "worktree", "list", "--porcelain", "-z").split("\0")
                                  if field.startswith("worktree ")}
                    if str(runtime.worktree) in registered:
                        raise HarnessError("工作树路径缺失但 Git 注册仍存在；请核对后再清理")
                    removal_finished = True
                else:
                    if not (runtime.worktree / ".git").is_file():
                        raise HarnessError("只能移除该 run 所有的隔离 worktree")
                    if git.head(runtime.worktree, binding=runtime.run["worktree_binding"]) != runtime.run["expected_head"]:
                        raise HarnessError("worktree HEAD 与检查点不同；保留工作树供核对")
                    git.assert_removable_worktree(runtime.worktree, binding=runtime.run["worktree_binding"])
            if runtime.run["status"] != "abandoned":
                runtime.run["status"] = "abandoned"
                runtime.save()
                runtime.checkpoint()
                runtime.event("run_abandoned")
            if remove_worktree and not runtime.run.get("worktree_removed"):
                if not removal_finished:
                    runtime.run["pending_worktree_removal"] = True
                    runtime.save()
                    git.check_worktree_binding(runtime.worktree, runtime.run["worktree_binding"])
                    git.git(runtime.repo, "worktree", "remove", str(runtime.worktree))
                runtime.run["worktree_removed"] = True
                runtime.run.pop("pending_worktree_removal", None)
                runtime.save()
                runtime.event("worktree_removed")
            return runtime.run

    def resume(self, ident, *, step=False, approve_plan=False, reverify=False, retry_stopped=None, confirm_exited=(),
               repair_invalid=None, allow_scope=(), environment_update=None, operator_decision=None):
        runtime = self.runtime
        runtime.load_run(ident)
        with lock(runtime.directory / "run.lock"):
            runtime.load_run(ident)
            if runtime.run["status"] == "abandoned":
                raise HarnessError("该 run 已放弃；保留分支、工作树与证据，不能继续派发")
            runtime.commander = runtime.run["commander"]
            if runtime.run["status"] == "completed":
                if confirm_exited or repair_invalid is not None or allow_scope or environment_update is not None or operator_decision is not None:
                    raise HarnessError("--confirm-exited、--repair-invalid、--allow-scope、--environment-update 与 --operator-decision 仅用于未完成的 run")
                return runtime.run
            if operator_decision is not None and (environment_update is not None or reverify or retry_stopped is not None
                                                   or repair_invalid is not None or allow_scope):
                raise HarnessError("--operator-decision 不能与环境更新、重验、重试停止、修正或扩展范围合用")
            if environment_update is not None and (reverify or retry_stopped is not None or repair_invalid is not None or allow_scope):
                raise HarnessError("--environment-update 不能与 --reverify、--retry-stopped、--repair-invalid 或 --allow-scope 合用")
            if repair_invalid is not None and (reverify or retry_stopped is not None or allow_scope):
                raise HarnessError("--repair-invalid 不能与 --reverify、--retry-stopped 或 --allow-scope 合用")
            if reverify and retry_stopped is not None:
                raise HarnessError("--reverify 不能与 --retry-stopped 合用")
            runtime.reverify_requested = reverify
            changes = runtime.config.resume_changes(runtime.run["config_snapshot"])
            if runtime.run.get("asset_digest") != asset_digest(runtime.config):
                raise HarnessError("共享规则、skills、MCP 或 hooks 已改变；请恢复原版本后继续")
            runtime.environment_context()
            runtime.confirm_exited(confirm_exited)
            runtime.ensure_exited()
            if not runtime.run["worktree_binding"]:
                raise HarnessError("缺少已保存的 worktree Git 绑定，无法安全恢复；"
                                   f"使用 abandon {ident} 保留证据后重新运行；"
                                   "若工作树或分支已创建，请核对后手工清理")
            if git.head(runtime.worktree, binding=runtime.run["worktree_binding"]) != runtime.run["expected_head"]:
                raise HarnessError("worktree HEAD 与检查点不同；请先核对提交，不自动重放")
            runtime.recover_replan_worker_correction()
            update = runtime.prepare_environment_update(environment_update) if environment_update is not None else None
            decision_values, decisions = (runtime.prepare_operator_decisions(operator_decision)
                                          if operator_decision is not None else ([], []))
            if retry_stopped is not None:
                stopped = runtime.run.get("replan_stopped")
                if not stopped or stopped["target"] != retry_stopped:
                    raise HarnessError("--retry-stopped 必须匹配已停止的任务 ID，或 final")
                if any(r["kind"] == "replan_stop" and r["source"]["dispatch_id"] == stopped.get("dispatch_id")
                       and r["response"] and r["response"]["decision"] == "stop" for r in runtime.decision_context()):
                    raise HarnessError("操作者已决定停止；请使用 --operator-decision 显式更新该决定")
            if allow_scope:
                runtime.approve_scope_extensions(allow_scope)
            if repair_invalid is not None:
                runtime.prepare_invalid_repair(repair_invalid)
            if runtime.run["schema_version"] < 11:
                previous_version = runtime.run["schema_version"]
                for field in ("decision_requests", "operator_decisions", "task_checkpoints", "environment_updates", "environment_checks"):
                    runtime.run.setdefault(field, [])
                runtime.run["schema_version"] = 11
                runtime.event("run_schema_upgraded", previous_version=previous_version, schema_version=11)
                runtime.save()
            runtime.record_operator_decisions(decisions)
            if operator_decision is not None:
                if any(value["decision"] == "stop" for value in decision_values):
                    error = HarnessError("操作者决定停止；决定与原证据已保存，未派发模型")
                    runtime.stop(error)
                    raise error
                answered_ids = {value["request_id"] for value in decision_values}
                request = next((r for r in runtime.decision_context()
                                if r["id"] in answered_ids and r["kind"] == "replan_stop"), None)
                if request is not None and runtime.run.get("replan_stopped"):
                    if runtime.run["replan_stopped"].get("dispatch_id") != request["source"]["dispatch_id"]:
                        raise HarnessError("操作者决定不能重开另一次指挥停止；请回答当前请求")
                    stopped = runtime.run.pop("replan_stopped")
                    runtime.run["pending_replan"] = {"stage": "request", "trigger": deepcopy(stopped["trigger"])}
                    runtime.save()
                    runtime.event("stopped_replan_decision_applied", request_id=request["id"], artifact=stopped["artifact"])
            if retry_stopped is not None:
                stopped = runtime.run.get("replan_stopped")
                # Reopen the saved planning request, not another attempt by the
                # already blocked worker. Preserve the original stop as evidence.
                runtime.run["pending_replan"] = {"stage": "request", "trigger": deepcopy(stopped["trigger"])}
                runtime.run.pop("replan_stopped")
                runtime.save()
                runtime.event("stopped_replan_retry_requested", target=retry_stopped, artifact=stopped["artifact"])
            runtime.memory.repo = runtime.worktree
            runtime.memory.binding = runtime.run["worktree_binding"]
            if changes:
                runtime.run["config_snapshot"] = deepcopy(runtime.config.data)
                runtime.event("resume_config_relaxed", changes=changes)
                runtime.save()
            runtime.record_environment_update(update)
            runtime.report_command_requirements()
            if runtime.run["tasks"] and runtime.run["plan_approval_required"] and not runtime.run["plan_approved"]:
                if not approve_plan:
                    raise HarnessError("计划等待明确批准；使用 resume --approve-plan 继续")
                runtime.run["plan_approved"] = True
                runtime.event("plan_approved")
                runtime.save()
            return runtime.execute(step=step)
