"""Own run creation, loading, recovery preflight and abandonment."""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

from .versions import RUN_SCHEMA_VERSION
from .config import normalized
from . import git
from .operations import warn
from .input_projection import validate_dispatch_indexes as validate_input_dispatch_indexes
from .path_evidence import validate_dispatch_indexes
from .services import RuntimeService
from .continuation import packet as continuation_packet, record_execution_guidance, record_continuation_rejection
from .shared import asset_digest, validate_mcp
from .util import HarnessError, lock, read_json, uid, validate_command_directories


class RunLifecycle(RuntimeService):
    def start(self, goal, plan=None, *, pause_after_plan=False, step=False):
        runtime = self.context
        if not goal.strip():
            raise HarnessError("目标不能为空")
        untracked = git.check_starting_checkout(self.context.repo)
        if not self.context.config.data["verification"]["commands"]:
            raise HarnessError("请先配置目标项目的 verification.commands")
        git.reject_submodules(self.context.repo)
        supplied_tasks = self.services.planning.validate_tasks(plan) if plan is not None else []
        validate_mcp(self.context.config)
        if untracked:
            print("[harness] 主检出存在未跟踪文件；保留原处，不进入隔离 worktree："
                  + git.describe_paths(untracked), file=sys.stderr, flush=True)
        ident = uid("run")
        worktree = self.context.state / "worktrees" / ident
        operation = self.context.operation
        self.context.run = {"schema_version": RUN_SCHEMA_VERSION, "workspace_inputs": None, "id": ident, "repo": str(self.context.repo), "goal": goal, "status": "planning", "commander": self.context.commander,
                    "branch": "harness/" + ident, "worktree": str(worktree), "tasks": [], "accepted": {}, "attempts": {},
                    "dispatches": [], "expected_head": git.head(self.context.repo), "feedback": None,
                    "config_path": str(self.context.config.path), "config_snapshot": deepcopy(self.context.config.data),
                    "asset_digest": asset_digest(self.context.config), "base_commit": git.head(self.context.repo),
                    "original_tasks": deepcopy(supplied_tasks),
                    "replan_attempts": 0, "final_attempts": 0,
                    "review_history": {}, "final_history": [], "plan_history": [], "format_repairs": {},
                    "worker_receipts": {}, "review_corrections": {}, "review_receipts": {}, "commander_receipts": {},
                    "invalid_repairs": {}, "approved_scope": [],
                    "environment_updates": [], "environment_checks": [],
                    "decision_requests": [], "operator_decisions": [], "task_checkpoints": [], "checkpoint_batches": [],
                    "continuation_history": [], "execution_guidance": [],
                    "worktree_binding": {}, "ignored_baseline": [],
                    "pending_format_corrections": {}, "agent_dispatches": 0,
                    "plan_approval_required": pause_after_plan, "plan_approved": not pause_after_plan,
                    "created_by_operation": getattr(operation, "id", None) if operation is not None else None}
        self.context.directory.mkdir(parents=True, exist_ok=True)
        self.context.save()
        self.announce("run_created")
        with lock(self.context.directory / "run.lock"):
            try:
                git.git(self.context.repo, "worktree", "add", "-b", self.context.run["branch"], str(worktree), "HEAD")
                self.context.run["worktree_binding"] = git.capture_worktree_binding(worktree, self.context.run["branch"])
                self.context.run["ignored_baseline"] = self.context.ignored_paths()
                self.context.save()
                self.context.record_ignored_paths("initial", self.context.directory)
            except BaseException as error:
                self.services.workflow.stop(error)
                raise
            self.context.memory.repo = self.context.worktree
            self.context.memory.binding = self.context.run["worktree_binding"]
            return self.services.scheduler.execute(plan, step=step)

    def announce(self, event):
        """Observation after a successful save: event, machine-readable stderr line, operation link.

        Ordinary observation errors only emit warnings and do not change the
        lifecycle, save order or exit status. Process interrupts still follow
        the existing recovery limits; the first save precedes worktree binding.
        """
        runtime = self.context
        operation = self.context.operation
        run_id = self.context.run["id"]
        operation_id = getattr(operation, "id", None) if operation is not None else None
        notice = json.dumps({"event": event, "run_id": run_id, "operation_id": operation_id},
                            ensure_ascii=True, separators=(",", ":"))
        steps = [("event", lambda: self.context.event(event, operation_id=operation_id)),
                 ("notify", lambda: print("[harness-json] " + notice, file=sys.stderr, flush=True))]
        if operation is not None:
            steps.append(("link", lambda: operation.link(run_id)))
        for name, step in steps:
            try:
                step()
            except Exception as error:
                warn(f"{event} 的观察步骤 {name} 失败，不影响运行：{error}")

    def load_run(self, ident):
        runtime = self.context
        if not ident.startswith("run_") or not ident[4:].isalnum():
            raise HarnessError("无效 run ID")
        self.context.run = read_json(self.context.state / "runs" / ident / "state.json")
        if not isinstance(self.context.run, dict):
            raise HarnessError("运行台账必须为对象")
        version = self.context.run.get("schema_version")
        if version != RUN_SCHEMA_VERSION:
            shown = version if type(version) is int else "无效"
            raise HarnessError(f"旧 run 仅保留为历史证据（台账版本 {shown}，当前 {RUN_SCHEMA_VERSION}）；"
                               "不迁移、不恢复执行，只能用 status、dashboard 查看，"
                               "或用 cleanup-run 清理；请开始新 run")
        required = {"id": str, "repo": str, "worktree": str, "status": str, "commander": str,
                    "goal": str, "branch": str, "expected_head": str, "base_commit": str,
                    "config_snapshot": dict, "tasks": list, "original_tasks": list,
                    "accepted": dict, "attempts": dict, "dispatches": list,
                    "review_history": dict, "review_corrections": dict, "review_receipts": dict,
                    "worker_receipts": dict, "commander_receipts": dict, "worktree_binding": dict, "ignored_baseline": list,
                    "invalid_repairs": dict, "approved_scope": list,
                    "pending_format_corrections": dict,
                    "final_history": list, "plan_history": list, "format_repairs": dict,
                    "agent_dispatches": int, "final_attempts": int,
                    "replan_attempts": int, "plan_approval_required": bool, "plan_approved": bool,
                    "environment_updates": list, "environment_checks": list,
                    "decision_requests": list, "operator_decisions": list, "task_checkpoints": list, "checkpoint_batches": list,
                    "continuation_history": list, "execution_guidance": list, "store_revision": int}
        if (self.context.run.get("id") != ident or any(type(self.context.run.get(k)) is not t for k, t in required.items())
                or any(not isinstance(d, dict) for d in self.context.run["dispatches"])):
            raise HarnessError("运行台账字段缺失或类型无效；保留原文件并核对证据")
        if self.context.run["repo"] != str(self.context.repo):
            raise HarnessError("Run 属于另一个项目")
        if self.context.run['config_snapshot'] != normalized(self.context.run['config_snapshot']):
            raise HarnessError('运行配置快照字段缺失；当前协议不补写旧版本默认值')
        if ("workspace_inputs" not in self.context.run
                or self.context.run["workspace_inputs"] is not None and not isinstance(self.context.run["workspace_inputs"], dict)):
            raise HarnessError("运行输入绑定字段缺失或无效；保留现场")
        validate_command_directories(self.context.run["dispatches"], self.context.directory)
        self.validate_recovery_links()
        self.services.worker_receipts.validate_links()
        self.services.budgets.validate_attempt_accounting()
        self.services.environment.validate_environment_links()
        self.services.decisions.validate_decision_links()
        self.services.progress.validate_checkpoint_links()
        continuation_packet(runtime)

    def validate_recovery_links(self):
        """Reject inconsistent recovery links before any resumed side effects.

        This checks ledger structure, not artifact integrity or candidate validity;
        the existing phase-specific gates still validate those when consumed.
        """
        runtime = self.context
        def fail(field):
            raise HarnessError(f"运行台账恢复状态无效：{field}；保留原文件并核对证据")

        def mapping(value, field):
            if not isinstance(value, dict):
                fail(field)
            return value

        for field in ("pending_task", "pending_acceptance", "pending_post_accept", "pending_replan",
                      "pending_directive", "worker_recovery", "replan_stopped", "retained_candidate"):
            if field in self.context.run:
                mapping(self.context.run[field], field)
        if "pending_task" in self.context.run and "pending_replan" in self.context.run:
            fail("pending_task 与 pending_replan 不能同时存在")

        task_ids = set()
        for index, task in enumerate(self.context.run["tasks"]):
            field = f"tasks[{index}]"
            ident = mapping(task, field).get("id")
            if not isinstance(ident, str) or not ident or ident in task_ids:
                fail(field + ".id 缺失、类型无效或重复")
            task_ids.add(ident)

        def task_reference(ident, field):
            if not isinstance(ident, str) or ident not in task_ids:
                fail(field + " 必须引用现有任务的字符串 ID")

        for field in ("pending_task", "pending_acceptance"):
            if field in self.context.run:
                task_reference(self.context.run[field].get("task_id"), field + ".task_id")
        if "pending_post_accept" in self.context.run:
            field = "pending_post_accept.payload"
            payload = mapping(self.context.run["pending_post_accept"].get("payload"), field)
            task_reference(payload.get("task_id"), field + ".task_id")
        if self.context.run.get("current_task") is not None:
            task_reference(self.context.run["current_task"], "current_task")

        dispatches = {}
        for entry in self.context.run["dispatches"]:
            if (entry.get('role') in ('worker', 'commander', 'reviewer') or 'invoked' in entry) and type(entry.get("invoked")) is not bool:
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

        for field, role in (("worker_receipts", "worker"), ("review_receipts", "reviewer"), ("commander_receipts", "commander")):
            for key, receipt in self.context.run[field].items():
                if not key.startswith(role + ":"):
                    fail(field + "." + key)
                source(receipt, key, field + "." + key)
        for key, guard in self.context.run["review_corrections"].items():
            field = "review_corrections." + key
            artifact = mapping(guard, field).get("artifact")
            if not key.startswith("reviewer:") or not isinstance(artifact, str):
                fail(field)
            # The guard identifies its original dispatch by the bound artifact path.
            reference(Path(artifact).parent.name, key, field)
        for key, pending in self.context.run["pending_format_corrections"].items():
            field = "pending_format_corrections." + key
            pending = mapping(pending, field)
            if "dispatch_id" in pending:
                reference(pending["dispatch_id"], key, field)
            correction = mapping(pending.get("correction"), field + ".correction")
            if "previous_dispatch" in correction:
                reference(correction["previous_dispatch"], key, field + ".correction")
            if "source" in correction:
                source(correction["source"], key, field + ".correction.source")
        for key, repair in self.context.run["invalid_repairs"].items():
            field = "invalid_repairs." + key
            repair = mapping(repair, field)
            source(repair.get("source"), key, field + ".source")
            if "original" in repair:
                source(repair["original"], key, field + ".original")
            if "corrected_dispatch_id" in repair:
                reference(repair["corrected_dispatch_id"], key, field)
        for tid, recovery in self.context.run.get("worker_recovery", {}).items():
            source(recovery, f"worker:implement:{tid}", "worker_recovery." + tid)
        owners = [("retained_candidate", self.context.run)]
        if "pending_replan" in self.context.run:
            owners.append(("pending_replan.retained_candidate", self.context.run["pending_replan"]))
        for field, owner in owners:
            if "retained_candidate" in owner or owner.get("candidate_policy") == "retain":
                guard = mapping(owner.get("retained_candidate"), field)
                task_reference(guard.get("task_id"), field + ".task_id")
                source(guard, "commander:replan:replanning", field)

    def abandon(self, ident, *, confirm_exited=()):
        """Mark an unfinished run abandoned; the worktree is removed only by cleanup-run."""
        self.load_run(ident)
        with lock(self.context.directory / "run.lock"):
            # Reload under the lock so a concurrent completion cannot be lost.
            self.load_run(ident)
            if self.context.run["status"] == "completed":
                raise HarnessError("已完成的 run 不能标记为放弃")
            self.context.confirm_exited(confirm_exited)
            self.context.ensure_exited()
            if self.context.run["status"] != "abandoned":
                self.context.run["status"] = "abandoned"
                self.context.save()
                self.context.checkpoint()
                self.context.event("run_abandoned")
            return self.context.run

    def resume(self, ident, *, step=False, approve_plan=False, reverify=False, retry_stopped=None, confirm_exited=(),
               repair_invalid=None, allow_scope=(), environment_update=None, operator_decision=None,
               execution_guidance=None, reject_continuation=None):
        runtime = self.context
        self.load_run(ident)
        with lock(self.context.directory / "run.lock"):
            self.load_run(ident)
            if self.context.run["status"] == "abandoned":
                raise HarnessError("该 run 已放弃；保留分支、工作树与证据，不能继续派发")
            self.context.commander = self.context.run["commander"]
            if self.context.run["status"] == "completed":
                if confirm_exited or repair_invalid is not None or allow_scope or environment_update is not None or operator_decision is not None or execution_guidance is not None or reject_continuation is not None:
                    raise HarnessError("恢复修改选项仅用于未完成的 run（含 --reject-continuation 与 --execution-guidance）")
                return self.context.run
            if reject_continuation is not None and (operator_decision is not None or environment_update is not None
                    or execution_guidance is not None or reverify or retry_stopped is not None
                    or repair_invalid is not None or allow_scope):
                raise HarnessError("--reject-continuation 不能与决定、环境更新、执行指导、重验、重规划或修正合用")
            if execution_guidance is not None and (operator_decision is not None or environment_update is not None
                    or reverify or retry_stopped is not None or repair_invalid is not None or allow_scope):
                raise HarnessError("--execution-guidance 不能与决定、环境更新、重验、重规划或修正合用")
            if operator_decision is not None and (environment_update is not None or reverify or retry_stopped is not None
                                                   or repair_invalid is not None or allow_scope):
                raise HarnessError("--operator-decision 不能与环境更新、重验、重试停止、修正或扩展范围合用")
            if environment_update is not None and (reverify or retry_stopped is not None or repair_invalid is not None or allow_scope):
                raise HarnessError("--environment-update 不能与 --reverify、--retry-stopped、--repair-invalid 或 --allow-scope 合用")
            if repair_invalid is not None and (reverify or retry_stopped is not None or allow_scope):
                raise HarnessError("--repair-invalid 不能与 --reverify、--retry-stopped 或 --allow-scope 合用")
            if reverify and retry_stopped is not None:
                raise HarnessError("--reverify 不能与 --retry-stopped 合用")
            self.context.reverify_requested = reverify
            changes = self.context.config.resume_changes(self.context.run["config_snapshot"])
            if self.context.run.get("asset_digest") != asset_digest(self.context.config):
                raise HarnessError("共享规则、skills、MCP 或 hooks 已改变；请恢复原版本后继续")
            self.services.environment.environment_context()
            self.context.confirm_exited(confirm_exited)
            self.context.ensure_exited()
            if not self.context.run["worktree_binding"]:
                raise HarnessError("缺少已保存的 worktree Git 绑定，无法安全恢复；"
                                   f"使用 abandon {ident} 保留证据后重新运行；"
                                   "若工作树或分支已创建，请核对后手工清理")
            validate_dispatch_indexes(self.context.directory, self.context.run["dispatches"])
            validate_input_dispatch_indexes(self.context.directory, self.context.run["dispatches"])
            if git.head(self.context.worktree, binding=self.context.run["worktree_binding"]) != self.context.run["expected_head"]:
                raise HarnessError("worktree HEAD 与检查点不同；请先核对提交，不自动重放")
            from .workspace_inputs import validate_inputs
            validate_inputs(self.context.run, self.context.directory)
            self.services.worker_receipts.validate_links()
            self.services.receipts.validate_pending_terminal_captures()
            self.services.planning.validate_replan_worker_correction()
            update = self.services.environment.prepare_environment_update(environment_update) if environment_update is not None else None
            decision_values, decisions = (self.services.decisions.prepare_operator_decisions(operator_decision)
                                          if operator_decision is not None else ([], []))
            if retry_stopped is not None:
                stopped = self.context.run.get("replan_stopped")
                if not stopped or stopped["target"] != retry_stopped:
                    raise HarnessError("--retry-stopped 必须匹配已停止的任务 ID，或 final")
                # Recover registration if interruption followed the saved stop.
                # Retry must not erase an unanswered, attributable decision gate.
                source_position = next((i for i, d in enumerate(self.context.run["dispatches"])
                                        if d.get("id") == stopped.get("dispatch_id")), None)
                if stopped.get("source_digest") and (source_position is None or not any(
                        d.get("invoked", True) for d in self.context.run["dispatches"][source_position + 1:])):
                    self.services.workflow.synchronize_decision_requests()
                for request in self.services.decisions.decision_context():
                    if (request["kind"] != "replan_stop" or request["task_id"] != stopped["target"]
                            or request["question"] != stopped["reason"]
                            or request["source"]["dispatch_id"] != stopped.get("dispatch_id")
                            or stopped.get("source_digest") not in (None, request["source"]["artifact_digest"])):
                        continue
                    response = request["response"]
                    boundary = response["dispatch_count"] if response else request["dispatch_count"]
                    if any(d.get("invoked", True) for d in self.context.run["dispatches"][boundary:]):
                        continue
                    if response is None:
                        raise self.services.workflow.decision_required(request)
                    if response["decision"] == "stop":
                        raise HarnessError(f"操作者已决定停止：request_id={request['id']}；"
                                           "请使用 --operator-decision 显式更新为 continue")
            if allow_scope:
                self.services.planning.approve_scope_extensions(allow_scope)
            if repair_invalid is not None:
                self.services.recovery.prepare_invalid_repair(repair_invalid)
            self.services.decisions.record_operator_decisions(decisions)
            if operator_decision is not None:
                if any(value["decision"] == "stop" for value in decision_values):
                    error = HarnessError("操作者决定停止；决定与原证据已保存，未派发模型")
                    self.services.workflow.stop(error)
                    raise error
                answered_ids = {value["request_id"] for value in decision_values}
                request = next((r for r in self.services.decisions.decision_context()
                                if r["id"] in answered_ids and r["kind"] == "replan_stop"), None)
                if request is not None and self.context.run.get("replan_stopped"):
                    if self.context.run["replan_stopped"].get("dispatch_id") != request["source"]["dispatch_id"]:
                        raise HarnessError("操作者决定不能重开另一次指挥停止；请回答当前请求")
                    stopped = self.context.run.pop("replan_stopped")
                    self.context.run["pending_replan"] = {"stage": "request", "trigger": deepcopy(stopped["trigger"])}
                    self.context.save()
                    self.context.event("stopped_replan_decision_applied", request_id=request["id"], artifact=stopped["artifact"])
            if retry_stopped is not None:
                stopped = self.context.run.get("replan_stopped")
                # Reopen the saved planning request, not another attempt by the
                # already blocked worker. Preserve the original stop as evidence.
                self.context.run["pending_replan"] = {"stage": "request", "trigger": deepcopy(stopped["trigger"])}
                self.context.run.pop("replan_stopped")
                self.context.save()
                self.context.event("stopped_replan_retry_requested", target=retry_stopped, artifact=stopped["artifact"])
            self.context.memory.repo = self.context.worktree
            self.context.memory.binding = self.context.run["worktree_binding"]
            if changes:
                self.context.run["config_snapshot"] = deepcopy(self.context.config.data)
                self.context.event("resume_config_relaxed", changes=changes)
                self.context.save()
            self.services.environment.record_environment_update(update)
            if execution_guidance is not None:
                record_execution_guidance(runtime, execution_guidance)
            if reject_continuation is not None:
                record_continuation_rejection(runtime, reject_continuation)
            self.services.planning.report_command_requirements()
            if self.context.run["tasks"] and self.context.run["plan_approval_required"] and not self.context.run["plan_approved"]:
                if not approve_plan:
                    raise HarnessError("计划等待明确批准；使用 resume --approve-plan 继续")
                self.context.run["plan_approved"] = True
                self.context.event("plan_approved")
                self.context.save()
            # Announce after resume() gates pass; earlier explicit audit writes
            # survive rejection. Stops inside execute() still emit run_stopped.
            self.announce("run_resumed")
            return self.services.scheduler.execute(step=step)
