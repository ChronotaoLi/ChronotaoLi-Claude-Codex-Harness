"""Own plan history and pending/stopped replanning; preserve original task obligations."""
from __future__ import annotations

import hashlib
import json
import sys
import uuid
from copy import deepcopy
from pathlib import Path

from . import git
from .budgets import work_attempts
from .context import compact_feedback
from .contracts import apply_revision, envelope, REPLAN, task_directive, validate, validate_plan
from .recovery import read_bound_result, snapshot_digest
from .requirements import missing_commands
from .services import RuntimeService
from .shared import skills_catalog
from .util import HarnessError, uid, write_json


class Planner(RuntimeService):
    def validate_tasks(self, plan):
        tasks = validate_plan(plan, self.context.config.data["runtime"]["max_tasks"])
        available = {item["name"] for item in skills_catalog(self.context.config.asset("skills"))}
        for task in tasks:
            unknown = set(task["skills"]) - available
            if unknown:
                raise HarnessError(f"计划引用不存在的 skill：{sorted(unknown)}")
        return tasks

    def report_command_requirements(self):
        # Recompute from the plan and current grants; the saved report is only
        # an operator diagnostic and must never authorize a dispatch.
        missing = missing_commands(self.context.run["tasks"], self.context.run["accepted"], self.context.config.data)
        report = {"missing": missing}
        if report != self.context.run.get("command_requirements"):
            self.context.run["command_requirements"] = report
            self.context.save()
            self.context.event("command_requirements_checked", **report)
            if missing:
                print("[harness] 计划存在未授权的任务命令：" + json.dumps(missing, ensure_ascii=True)
                      + "；对应 worker 派发前必须在 permissions.worker_commands 追加精确 argv。",
                      file=sys.stderr, flush=True)
        return missing

    def require_task_commands(self, task_ids):
        missing = self.report_command_requirements()
        unmet = {tid: missing[tid] for tid in task_ids if tid in missing}
        if unmet:
            raise HarnessError("任务所需命令尚未授权：" + json.dumps(unmet, ensure_ascii=True)
                               + "；在 permissions.worker_commands 追加这些精确 argv 后普通 resume；"
                                 "计划批准不授予命令权限，本检查不消耗 worker 尝试或模型派发额度")

    def approve_scope_extensions(self, patterns):
        """Record explicit operator authority for new tasks in a pending replan."""
        patterns = list(patterns)
        for pattern in patterns:
            if not isinstance(pattern, str):
                raise HarnessError("额外批准的文件范围必须是字符串")
            git.validate_scope_pattern(pattern)
        if not patterns:
            return
        pending = self.context.run.get("pending_replan")
        if (self.context.run["status"] in ("completed", "abandoned")
                or not ((pending and pending["stage"] in ("pending", "request"))
                        or self.context.run.get("replan_stopped"))):
            raise HarnessError("--allow-scope 仅用于尚未校验的待再规划或已停止的再规划")
        approved = self.context.run.get("approved_scope", [])
        added = [pattern for pattern in dict.fromkeys(patterns) if pattern not in approved]
        if not added:
            return
        combined = [*approved, *added]
        # Audit first: interruption must not leave an unaudited authorization.
        self.context.event("scope_extension_approved", added_paths=added, approved_scope=combined)
        self.context.run["approved_scope"] = combined
        self.context.save()

    def record_plan(self, tasks, reason, trigger=None, *, finish_replan=False):
        revision = len(self.context.run["plan_history"])
        previous = {(task["id"], pattern) for record in self.context.run["plan_history"]
                    for task in record["tasks"] for pattern in task["allowed_paths"]}
        broad = [{"task_id": task["id"], "pattern": pattern} for task in tasks
                 for pattern in dict.fromkeys(task["allowed_paths"])
                 if (task["id"], pattern) not in previous and git.repository_wide_scope(pattern)]
        artifact = self.context.directory / "plans" / f"revision-{revision}.json"
        record = {"revision": revision, "reason": reason, "trigger": trigger,
                  "tasks": deepcopy(tasks), "artifact": str(artifact)}
        retained = (self.context.run.get("pending_replan", {}).get("retained_candidate")
                    if finish_replan else None)
        if retained:
            record["retained_candidate"] = deepcopy(retained)
        write_json(artifact, record)
        self.context.run["plan_history"].append(record)
        self.context.run["tasks"] = deepcopy(tasks)
        if revision == 0:
            self.context.run["commander_receipts"].pop("commander:plan:planning", None)
        if finish_replan:
            self.context.run["current_task"] = retained["task_id"] if retained else None
            if retained:
                self.context.run["retained_candidate"] = deepcopy(retained)
            else:
                self.context.run.pop("retained_candidate", None)
            self.context.run.pop("pending_replan", None)
        self.context.save()
        if broad:
            self.context.event("broad_scope_warning", revision=revision, scopes=broad)
            print("[harness] 警告：任务范围覆盖全仓库，受保护路径仍禁止修改："
                  + json.dumps(broad, ensure_ascii=True), file=sys.stderr, flush=True)
        self.report_command_requirements()

    def description_revision_task(self, previous, tasks, trigger):
        """Recognize only an actual description change to the current task."""
        tid = trigger.get("task_id")
        if (not tid or tid != self.context.run.get("current_task") or tid in self.context.run["accepted"]
                or trigger.get("final_review") or len(previous) != len(tasks)):
            return None
        changed = []
        for old, new in zip(previous, tasks):
            if old != new:
                if (old["id"] != tid or new.get("id") != tid
                        or {k: v for k, v in old.items() if k != "description"}
                        != {k: v for k, v in new.items() if k != "description"}):
                    return None
                changed.append(new)
        return changed[0] if len(changed) == 1 else None

    def validate_retained_candidate(self, *, pending=None, before=None, worker_task=None):
        """Keep a saved candidate bound to its task until a fresh worker starts.

        In particular, an unexplained change is not permission to archive/reset.
        """
        owner = self.context.run if pending is None else pending
        if pending is not None and pending.get("candidate_policy") != "retain":
            if pending.get("candidate_policy") != "archive" or "retained_candidate" in pending:
                raise HarnessError("再规划候选保留策略缺失；保留现场")
            return
        guard = owner.get("retained_candidate")
        if guard is None and pending is None:
            history = self.context.run.get("plan_history", [])
            retained = history[-1].get("retained_candidate") if history else None
            if retained and not any(
                    entry.get("role") == "worker" and entry.get("task_id") == retained["task_id"]
                    and entry.get("invoked") is True
                    and entry.get("retained_candidate_revision") == retained["plan_revision"]
                    for entry in self.context.run["dispatches"]):
                raise HarnessError("再规划候选保留依据缺失且未交给 worker；保留现场")
            return
        if (not isinstance(guard, dict)
                or any(not isinstance(guard.get(k), str) or not guard[k] for k in
                       ("task_id", "head", "snapshot_digest", "artifact", "artifact_digest", "dispatch_id"))
                or type(guard.get("plan_revision")) is not int):
            raise HarnessError("再规划候选保留依据缺失或无效；保留现场")
        revision = guard["plan_revision"]
        history = self.context.run["plan_history"]
        if revision < 1 or revision != len(history) - (1 if pending is None else 0):
            raise HarnessError("再规划候选与计划版本不符；保留现场")
        previous = history[revision - 1]["tasks"]
        target = pending if pending is not None else history[revision]
        if pending is None and target.get("retained_candidate") != guard:
            raise HarnessError("再规划候选与计划记录不符；保留现场")
        tasks, trigger = target["tasks"], target["trigger"]
        task = self.description_revision_task(previous, tasks, trigger)
        if (not task or task["id"] != guard["task_id"]
                or self.context.run["tasks"] != (previous if pending is not None else tasks)
                or self.context.run.get("pending_task") or self.context.run.get("pending_acceptance")
                or worker_task is not None and worker_task != guard["task_id"]):
            raise HarnessError("再规划候选与当前任务或说明修订不符；保留现场")
        entry = next((d for d in self.context.run["dispatches"] if d["id"] == guard["dispatch_id"]), None)
        if (not entry or (entry["role"], entry["kind"], entry["task_id"]) !=
                ("commander", "replan", "replanning") or not entry.get("invoked", True)
                or entry.get("snapshot_digest") != guard["snapshot_digest"]
                or guard["artifact"] != str(self.context.directory / "dispatches" / entry["id"] / "returned-result.json")):
            raise HarnessError("再规划候选与原派发不符；保留现场")
        result, _ = read_bound_result(guard, "再规划候选依据的")
        validate(result, envelope(REPLAN, "replanning", guard["dispatch_id"]))
        payload = {key: value for key, value in result.items() if key not in ("task_id", "dispatch_id")}
        if (result["action"] != "revise" or apply_revision(previous, payload, self.context.run["accepted"],
                self.context.config.data["runtime"]["max_tasks"],
                additional_allowed_paths=self.context.run.get("approved_scope", [])) != tasks):
            raise HarnessError("再规划候选与原计划结果不符；保留现场")
        candidate = self.context.snapshot() if before is None else before
        if (guard["head"] != self.context.run["expected_head"] or candidate["head"] != guard["head"]
                or snapshot_digest(candidate) != guard["snapshot_digest"]):
            raise HarnessError("再规划保留候选的代码或暂存区已变化；保留现场，禁止归档重置")
        self.services.tasks.check_task_scope(task)

    def validate_replan_selection(self, trigger):
        """Recheck the immutable selection that routed this task to replanning."""
        if trigger.get("coordination"):
            source = trigger["coordination"]
            if (not isinstance(source, dict)
                    or any(not isinstance(source.get(key), str) or not source[key] for key in
                           ("artifact", "artifact_digest", "dispatch_id", "snapshot_digest", "instruction"))):
                raise HarnessError("再规划选择来源无效；保留证据")
            entry = next((d for d in self.context.run["dispatches"] if d["id"] == source.get("dispatch_id")), None)
            if (not entry or (entry["role"], entry["kind"], entry["task_id"]) !=
                    ("commander", "dispatch", "dispatching") or not entry.get("invoked", True)
                    or source.get("artifact") != str(self.context.directory / "dispatches" / entry["id"] / "returned-result.json")
                    or entry.get("snapshot_digest") != source["snapshot_digest"]
                    or snapshot_digest(self.context.snapshot()) != source["snapshot_digest"]):
                raise HarnessError("再规划选择的来源或代码快照已变化；保留原选择和证据")
            result, _ = read_bound_result(source, "再规划选择")
            validate(result, envelope(task_directive([trigger["task_id"]]), "dispatching", source["dispatch_id"]))
            if result.get("action") != "replan" or result["instruction"] != source["instruction"]:
                raise HarnessError("再规划选择与原指挥结果不符；保留原选择和证据")

    def validate_replan_worker_correction(self):
        """Current transitions atomically retire superseded worker corrections."""
        for field in ('pending_replan', 'replan_stopped'):
            boundary = self.context.run.get(field)
            if not boundary:
                continue
            trigger = boundary.get('trigger', {})
            if isinstance(trigger, dict) and trigger.get('coordination'):
                key = f"worker:implement:{trigger.get('task_id')}"
                if key in self.context.run['pending_format_corrections']:
                    raise HarnessError('superseded worker correction conflicts with replan; '
                                       'current protocol does not repair legacy state')

    def replan(self, trigger):
        pending = self.context.run.get("pending_replan")
        if pending and pending["stage"] == "validated":
            return self.apply_pending_revision()
        self.validate_replan_selection(trigger)
        # Persist the route before any budget guard. Increasing a budget must
        # continue this request, never rerun the worker that reported the blocker.
        if not pending:
            self.context.run["pending_replan"] = {"stage": "pending", "trigger": deepcopy(trigger)}
            self.context.save()
        final = trigger.get("final_review")
        if final:
            if not git.clean(self.context.worktree, binding=self.context.run["worktree_binding"]):
                raise HarnessError("整体审阅要求干净的已接受提交；请核对并清理未接受修改")
            if snapshot_digest(self.context.snapshot()) != final["snapshot_digest"]:
                raise HarnessError("待再规划的整体审阅快照已变化；请核对检查点")
        if self.context.run["total_attempts"] >= self.context.config.data["runtime"]["max_total_attempts"]:
            raise HarnessError("已达 run 总尝试次数上限；不通过新增任务绕过预算")
        tid = trigger.get("task_id")
        if tid and self.services.budgets.task_budget_used(tid) >= self.context.config.data["runtime"]["max_attempts"]:
            raise HarnessError(f"{tid} 已达尝试次数上限；不通过重新命名任务绕过预算")
        if final and self.context.run["final_attempts"] >= self.context.config.data["runtime"]["max_final_attempts"]:
            raise HarnessError("整体审阅已达尝试次数上限；不再派发修正任务")
        if self.context.run["replan_attempts"] >= self.context.config.data["runtime"]["max_replans"]:
            prefix = ""
            if final and final["status"] in ("rejected", "invalid_rejected"):
                prefix = "整体审阅已拒绝同一代码快照；修正计划"
            elif final and final["status"] == "verification_failed":
                prefix = "整体验证未通过；可显式使用 resume --reverify 重验，修正计划"
            raise HarnessError(prefix + "已达重新规划次数上限；可增大 max_replans 后恢复，反馈与证据已保留")
        attempt = self.context.run["replan_attempts"] + 1
        self.context.run["pending_replan"] = {"stage": "request", "trigger": deepcopy(trigger), "attempt": attempt,
            "request_id": self.context.run["pending_replan"].get("request_id") or str(uuid.uuid4())}
        self.context.save()
        result = self.services.dispatch.dispatch("commander", "replan", "replanning", REPLAN,
            {"goal": self.context.run["goal"], **self.services.packets.plan_context(),
             "accepted": self.services.packets.accepted_context(), "trigger": compact_feedback(trigger),
             "approved_scope": deepcopy(self.context.run.get("approved_scope", [])),
             "attempts": self.context.run["attempts"], "work_attempts": work_attempts(self.context.run),
             "total_attempts": self.context.run["total_attempts"],
             "max_tasks": self.context.config.data["runtime"]["max_tasks"],
             "remaining_replans": self.context.config.data["runtime"]["max_replans"] - attempt,
             "tool_constraints": self.services.packets.tool_constraints(),
             "instruction": "Apply the replan contract in role_skill to the bound trigger. Additional scope is limited to approved_scope. No file edits."})
        artifact = self.context.directory / "plans" / f"replan-{attempt}.json"
        write_json(artifact, {"trigger": trigger, "result": result})
        tasks = apply_revision(self.context.run["tasks"], result, self.context.run["accepted"],
                               self.context.config.data["runtime"]["max_tasks"],
                               additional_allowed_paths=self.context.run.get("approved_scope", []))
        self.validate_tasks({"tasks": tasks})
        if result["action"] == "stop":
            self.record_replan_stop(trigger, result, attempt)
            raise HarnessError(f"指挥停止重新规划：{result['reason']}")
        self.context.run["pending_replan"].update(stage="validated", tasks=tasks, reason=result["reason"],
                                          artifact=str(artifact), head=self.context.run["expected_head"], candidate_policy="archive")
        if self.description_revision_task(self.context.run["tasks"], tasks, trigger):
            receipt = self.context.run["commander_receipts"]["commander:replan:replanning"]
            guard = {key: receipt[key] for key in
                     ("artifact", "artifact_digest", "dispatch_id", "snapshot_digest")}
            guard.update(task_id=trigger["task_id"], head=self.context.run["expected_head"],
                         plan_revision=len(self.context.run["plan_history"]))
            self.context.run["pending_replan"].update(candidate_policy="retain", retained_candidate=guard)
        self.context.run["replan_attempts"] = attempt
        self.context.run["commander_receipts"].pop("commander:replan:replanning", None)
        self.context.save()
        self.apply_pending_revision()

    def record_replan_stop(self, trigger, result, attempt, **diagnostics):
        artifact = self.context.directory / "plans" / f"replan-{attempt}.json"
        write_json(artifact, {"trigger": trigger, "result": result, **diagnostics})
        self.context.run["replan_attempts"] = attempt
        self.context.run["replan_stopped"] = {"target": trigger.get("task_id") or "final",
            "trigger": deepcopy(trigger), "attempt": attempt, "artifact": str(artifact),
            "reason": result["reason"], **diagnostics}
        receipt = self.context.run["commander_receipts"].get("commander:replan:replanning")
        if receipt:
            self.context.run["replan_stopped"].update(dispatch_id=receipt["dispatch_id"],
                                              source_digest=hashlib.sha256(Path(receipt["artifact"]).read_bytes()).hexdigest())
        self.context.run.pop("pending_replan", None)
        self.context.run["commander_receipts"].pop("commander:replan:replanning", None)
        self.context.run["pending_format_corrections"].pop("commander:replan:replanning", None)
        self.context.save()
        self.services.workflow.synchronize_decision_requests()

    def apply_pending_revision(self):
        pending = self.context.run["pending_replan"]
        if self.context.run["expected_head"] != pending["head"] or git.head(self.context.worktree, binding=self.context.run["worktree_binding"]) != pending["head"]:
            raise HarnessError("应用再规划前 HEAD 与已校验计划不符；保留现场")
        self.services.packets.preflight_final_context(pending["tasks"], self.context.run["original_tasks"])
        self.context.ensure_exited()
        self.validate_retained_candidate()
        self.validate_retained_candidate(pending=pending)
        retain = pending.get("candidate_policy") == "retain"
        # A new prerequisite may own another scope. Never carry the blocked task's edits into it.
        if not retain and not git.clean(self.context.worktree, binding=self.context.run["worktree_binding"]):
            folder = self.context.directory / "recovery" / f"replan-{pending['attempt']}"
            if folder.exists():
                folder = folder.with_name(folder.name + "-" + uid("retry"))
            archive = git.archive_and_reset(self.context.worktree, self.context.run["expected_head"],
                                            folder, self.context.state / "worktrees", binding=self.context.run["worktree_binding"])
            self.context.event("replan_edits_archived", task_id=pending["trigger"].get("task_id"), artifact=archive)
        self.context.run["feedback"] = {"replan": pending["artifact"], "trigger": compact_feedback(pending["trigger"])}
        if not retain:
            self.context.run.pop("worker_recovery", None)
        self.context.run["pending_format_corrections"].clear()
        self.record_plan(pending["tasks"], pending["reason"], pending["trigger"], finish_replan=True)
        self.context.checkpoint()
        self.context.event("plan_revised", revision=len(self.context.run["plan_history"]) - 1)
