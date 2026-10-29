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
        runtime = self.runtime
        tasks = validate_plan(plan, runtime.config.data["runtime"]["max_tasks"])
        available = {item["name"] for item in skills_catalog(runtime.config.asset("skills"))}
        for task in tasks:
            unknown = set(task["skills"]) - available
            if unknown:
                raise HarnessError(f"计划引用不存在的 skill：{sorted(unknown)}")
        return tasks

    def report_command_requirements(self):
        # Recompute from the plan and current grants; the saved report is only
        # an operator diagnostic and must never authorize a dispatch.
        runtime = self.runtime
        missing = missing_commands(runtime.run["tasks"], runtime.run["accepted"], runtime.config.data)
        report = {"missing": missing}
        if report != runtime.run.get("command_requirements"):
            runtime.run["command_requirements"] = report
            runtime.save()
            runtime.event("command_requirements_checked", **report)
            if missing:
                print("[harness] 计划存在未授权的任务命令：" + json.dumps(missing, ensure_ascii=True)
                      + "；对应 worker 派发前必须在 permissions.worker_commands 追加精确 argv。",
                      file=sys.stderr, flush=True)
        return missing

    def require_task_commands(self, task_ids):
        runtime = self.runtime
        missing = runtime.report_command_requirements()
        unmet = {tid: missing[tid] for tid in task_ids if tid in missing}
        if unmet:
            raise HarnessError("任务所需命令尚未授权：" + json.dumps(unmet, ensure_ascii=True)
                               + "；在 permissions.worker_commands 追加这些精确 argv 后普通 resume；"
                                 "计划批准不授予命令权限，本检查不消耗 worker 尝试或模型派发额度")

    def approve_scope_extensions(self, patterns):
        """Record explicit operator authority for new tasks in a pending replan."""
        runtime = self.runtime
        patterns = list(patterns)
        for pattern in patterns:
            if not isinstance(pattern, str):
                raise HarnessError("额外批准的文件范围必须是字符串")
            git.validate_scope_pattern(pattern)
        if not patterns:
            return
        pending = runtime.run.get("pending_replan")
        if (runtime.run["status"] in ("completed", "abandoned")
                or not ((pending and pending["stage"] in ("pending", "request"))
                        or runtime.run.get("replan_stopped"))):
            raise HarnessError("--allow-scope 仅用于尚未校验的待再规划或已停止的再规划")
        approved = runtime.run.get("approved_scope", [])
        added = [pattern for pattern in dict.fromkeys(patterns) if pattern not in approved]
        if not added:
            return
        combined = [*approved, *added]
        # Audit first: interruption must not leave an unaudited authorization.
        runtime.event("scope_extension_approved", added_paths=added, approved_scope=combined)
        runtime.run["approved_scope"] = combined
        runtime.save()

    def record_plan(self, tasks, reason, trigger=None, *, finish_replan=False):
        runtime = self.runtime
        revision = len(runtime.run["plan_history"])
        previous = {(task["id"], pattern) for record in runtime.run["plan_history"]
                    for task in record["tasks"] for pattern in task["allowed_paths"]}
        broad = [{"task_id": task["id"], "pattern": pattern} for task in tasks
                 for pattern in dict.fromkeys(task["allowed_paths"])
                 if (task["id"], pattern) not in previous and git.repository_wide_scope(pattern)]
        artifact = runtime.directory / "plans" / f"revision-{revision}.json"
        record = {"revision": revision, "reason": reason, "trigger": trigger,
                  "tasks": deepcopy(tasks), "artifact": str(artifact)}
        retained = (runtime.run.get("pending_replan", {}).get("retained_candidate")
                    if finish_replan else None)
        if retained:
            record["retained_candidate"] = deepcopy(retained)
        write_json(artifact, record)
        runtime.run["plan_history"].append(record)
        runtime.run["tasks"] = deepcopy(tasks)
        if revision == 0:
            runtime.run["commander_receipts"].pop("commander:plan:planning", None)
        if finish_replan:
            runtime.run["current_task"] = retained["task_id"] if retained else None
            if retained:
                runtime.run["retained_candidate"] = deepcopy(retained)
            else:
                runtime.run.pop("retained_candidate", None)
            runtime.run.pop("pending_replan", None)
        runtime.save()
        if broad:
            runtime.event("broad_scope_warning", revision=revision, scopes=broad)
            print("[harness] 警告：任务范围覆盖全仓库，受保护路径仍禁止修改："
                  + json.dumps(broad, ensure_ascii=True), file=sys.stderr, flush=True)
        runtime.report_command_requirements()

    def description_revision_task(self, previous, tasks, trigger):
        """Recognize only an actual description change to the current task."""
        runtime = self.runtime
        tid = trigger.get("task_id")
        if (not tid or tid != runtime.run.get("current_task") or tid in runtime.run["accepted"]
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
        Old validated revisions without the policy keep their archive behavior.
        """
        runtime = self.runtime
        owner = runtime.run if pending is None else pending
        if pending is not None and pending.get("candidate_policy") != "retain":
            if "candidate_policy" in pending or "retained_candidate" in pending:
                raise HarnessError("再规划候选保留策略缺失；保留现场")
            return
        guard = owner.get("retained_candidate")
        if guard is None and pending is None:
            history = runtime.run.get("plan_history", [])
            retained = history[-1].get("retained_candidate") if history else None
            if retained and not any(
                    entry.get("role") == "worker" and entry.get("task_id") == retained["task_id"]
                    and entry.get("invoked") is True
                    and entry.get("retained_candidate_revision") == retained["plan_revision"]
                    for entry in runtime.run["dispatches"]):
                raise HarnessError("再规划候选保留依据缺失且未交给 worker；保留现场")
            return
        if (not isinstance(guard, dict)
                or any(not isinstance(guard.get(k), str) or not guard[k] for k in
                       ("task_id", "head", "snapshot_digest", "artifact", "artifact_digest", "dispatch_id"))
                or type(guard.get("plan_revision")) is not int):
            raise HarnessError("再规划候选保留依据缺失或无效；保留现场")
        revision = guard["plan_revision"]
        history = runtime.run["plan_history"]
        if revision < 1 or revision != len(history) - (1 if pending is None else 0):
            raise HarnessError("再规划候选与计划版本不符；保留现场")
        previous = history[revision - 1]["tasks"]
        target = pending if pending is not None else history[revision]
        if pending is None and target.get("retained_candidate") != guard:
            raise HarnessError("再规划候选与计划记录不符；保留现场")
        tasks, trigger = target["tasks"], target["trigger"]
        task = self.description_revision_task(previous, tasks, trigger)
        if (not task or task["id"] != guard["task_id"]
                or runtime.run["tasks"] != (previous if pending is not None else tasks)
                or runtime.run.get("pending_task") or runtime.run.get("pending_acceptance")
                or worker_task is not None and worker_task != guard["task_id"]):
            raise HarnessError("再规划候选与当前任务或说明修订不符；保留现场")
        entry = next((d for d in runtime.run["dispatches"] if d["id"] == guard["dispatch_id"]), None)
        if (not entry or (entry["role"], entry["kind"], entry["task_id"]) !=
                ("commander", "replan", "replanning") or not entry.get("invoked", True)
                or entry.get("snapshot_digest") != guard["snapshot_digest"]
                or guard["artifact"] != str(runtime.directory / "dispatches" / entry["id"] / "returned-result.json")):
            raise HarnessError("再规划候选与原派发不符；保留现场")
        result, _ = read_bound_result(guard, "再规划候选依据的")
        validate(result, envelope(REPLAN, "replanning", guard["dispatch_id"]))
        payload = {key: value for key, value in result.items() if key not in ("task_id", "dispatch_id")}
        if (result["action"] != "revise" or apply_revision(previous, payload, runtime.run["accepted"],
                runtime.config.data["runtime"]["max_tasks"],
                additional_allowed_paths=runtime.run.get("approved_scope", [])) != tasks):
            raise HarnessError("再规划候选与原计划结果不符；保留现场")
        candidate = runtime.snapshot() if before is None else before
        if (guard["head"] != runtime.run["expected_head"] or candidate["head"] != guard["head"]
                or snapshot_digest(candidate) != guard["snapshot_digest"]):
            raise HarnessError("再规划保留候选的代码或暂存区已变化；保留现场，禁止归档重置")
        runtime.check_task_scope(task)

    def validate_replan_selection(self, trigger):
        """Recheck the immutable selection that routed this task to replanning."""
        runtime = self.runtime
        if trigger.get("coordination"):
            source = trigger["coordination"]
            if (not isinstance(source, dict)
                    or any(not isinstance(source.get(key), str) or not source[key] for key in
                           ("artifact", "artifact_digest", "dispatch_id", "snapshot_digest", "instruction"))):
                raise HarnessError("再规划选择来源无效；保留证据")
            entry = next((d for d in runtime.run["dispatches"] if d["id"] == source.get("dispatch_id")), None)
            if (not entry or (entry["role"], entry["kind"], entry["task_id"]) !=
                    ("commander", "dispatch", "dispatching") or not entry.get("invoked", True)
                    or source.get("artifact") != str(runtime.directory / "dispatches" / entry["id"] / "returned-result.json")
                    or entry.get("snapshot_digest") != source["snapshot_digest"]
                    or snapshot_digest(runtime.snapshot()) != source["snapshot_digest"]):
                raise HarnessError("再规划选择的来源或代码快照已变化；保留原选择和证据")
            result, _ = read_bound_result(source, "再规划选择")
            validate(result, envelope(task_directive([trigger["task_id"]]), "dispatching", source["dispatch_id"]))
            if result.get("action") != "replan" or result["instruction"] != source["instruction"]:
                raise HarnessError("再规划选择与原指挥结果不符；保留原选择和证据")

    def recover_replan_worker_correction(self):
        """Retire only a worker correction superseded by a saved replan selection.

        Older runs could retain it at a pending or stopped replan boundary. This
        is recovery bookkeeping, not approval to restart a stopped request.
        """
        runtime = self.runtime
        pending = runtime.run.get("pending_replan")
        stopped = runtime.run.get("replan_stopped")
        if pending and pending.get("stage") not in ("pending", "request"):
            return  # A validated revision owns its existing archive/reset recovery.
        boundary = pending or stopped
        if not boundary:
            return
        trigger = boundary.get("trigger", {})
        if not isinstance(trigger, dict) or not trigger.get("coordination"):
            return
        tid = trigger.get("task_id")
        key = f"worker:implement:{tid}"
        correction = runtime.run["pending_format_corrections"].get(key)
        if correction is None:
            return
        if (pending and stopped or runtime.run.get("current_task") != tid
                or tid in runtime.run["accepted"] or stopped and stopped.get("target") != tid):
            raise HarnessError("待修正 worker 与再规划任务边界不符；保留原状态")
        self.validate_replan_selection(trigger)
        selection_id = trigger["coordination"]["dispatch_id"]
        entries = runtime.run["dispatches"]
        positions = {entry["id"]: i for i, entry in enumerate(entries)}
        selection_position = positions[selection_id]
        origins = [correction["correction"].get("previous_dispatch")]
        if correction.get("dispatch_id"):
            origins.append(correction["dispatch_id"])
        if (any(origin not in positions or positions[origin] >= selection_position for origin in origins)
                or any(entry["role"] == "worker" and entry["task_id"] == tid and entry.get("invoked", True)
                       for entry in entries[selection_position + 1:])):
            raise HarnessError("待修正 worker 未被原再规划选择取代；保留原状态")
        # Audit before mutation; an interrupted save can safely repeat cleanup.
        runtime.event("replan_worker_correction_retired", task_id=tid, selection_dispatch_id=selection_id,
                      correction=deepcopy(correction))
        runtime.run["pending_format_corrections"].pop(key)
        runtime.save()

    def replan(self, trigger):
        runtime = self.runtime
        pending = runtime.run.get("pending_replan")
        if pending and pending["stage"] == "validated":
            return runtime.apply_pending_revision()
        self.validate_replan_selection(trigger)
        # Persist the route before any budget guard. Increasing a budget must
        # continue this request, never rerun the worker that reported the blocker.
        if not pending:
            runtime.run["pending_replan"] = {"stage": "pending", "trigger": deepcopy(trigger)}
            runtime.save()
        final = trigger.get("final_review")
        if final:
            if not git.clean(runtime.worktree, binding=runtime.run["worktree_binding"]):
                raise HarnessError("整体审阅要求干净的已接受提交；请核对并清理未接受修改")
            if snapshot_digest(runtime.snapshot()) != final["snapshot_digest"]:
                raise HarnessError("待再规划的整体审阅快照已变化；请核对检查点")
        if runtime.run["total_attempts"] >= runtime.config.data["runtime"]["max_total_attempts"]:
            raise HarnessError("已达 run 总尝试次数上限；不通过新增任务绕过预算")
        tid = trigger.get("task_id")
        if tid and runtime.task_budget_used(tid) >= runtime.config.data["runtime"]["max_attempts"]:
            raise HarnessError(f"{tid} 已达尝试次数上限；不通过重新命名任务绕过预算")
        if final and runtime.run["final_attempts"] >= runtime.config.data["runtime"]["max_final_attempts"]:
            raise HarnessError("整体审阅已达尝试次数上限；不再派发修正任务")
        if runtime.run["replan_attempts"] >= runtime.config.data["runtime"]["max_replans"]:
            prefix = ""
            if final and final["status"] in ("rejected", "invalid_rejected"):
                prefix = "整体审阅已拒绝同一代码快照；修正计划"
            elif final and final["status"] == "verification_failed":
                prefix = "整体验证未通过；可显式使用 resume --reverify 重验，修正计划"
            raise HarnessError(prefix + "已达重新规划次数上限；可增大 max_replans 后恢复，反馈与证据已保留")
        attempt = runtime.run["replan_attempts"] + 1
        runtime.run["pending_replan"] = {"stage": "request", "trigger": deepcopy(trigger), "attempt": attempt,
            "request_id": runtime.run["pending_replan"].get("request_id") or str(uuid.uuid4())}
        runtime.save()
        result = runtime.dispatch("commander", "replan", "replanning", REPLAN,
            {"goal": runtime.run["goal"], **runtime.plan_context(),
             "accepted": runtime.accepted_context(), "trigger": compact_feedback(trigger),
             "approved_scope": deepcopy(runtime.run.get("approved_scope", [])),
             "attempts": runtime.run["attempts"], "work_attempts": work_attempts(runtime.run),
             "total_attempts": runtime.run["total_attempts"],
             "max_tasks": runtime.config.data["runtime"]["max_tasks"],
             "remaining_replans": runtime.config.data["runtime"]["max_replans"] - attempt,
             "tool_constraints": runtime.tool_constraints(),
             "instruction": "Propose new_tasks and updates only when prerequisites or corrective tasks can resolve the blocker. Each output update has id, description and add_depends_on; the runtime preserves all other fields. Accepted tasks cannot be updated. Never remove dependencies or weaken the goal. New task allowed_paths must reuse existing scope patterns or exact patterns explicitly authorized in approved_scope. Existing task scope and acceptance remain unchanged. Input tasks_ref names the unchanged current list; otherwise reconstruct current tasks from tasks_base_ref and tasks_delta, applying supplied description/dependency updates and appending new_tasks. Return empty operations when stopping. No file edits."})
        artifact = runtime.directory / "plans" / f"replan-{attempt}.json"
        write_json(artifact, {"trigger": trigger, "result": result})
        tasks = apply_revision(runtime.run["tasks"], result, runtime.run["accepted"],
                               runtime.config.data["runtime"]["max_tasks"],
                               additional_allowed_paths=runtime.run.get("approved_scope", []))
        runtime.validate_tasks({"tasks": tasks})
        if result["action"] == "stop":
            runtime.record_replan_stop(trigger, result, attempt)
            raise HarnessError(f"指挥停止重新规划：{result['reason']}")
        runtime.run["pending_replan"].update(stage="validated", tasks=tasks, reason=result["reason"],
                                          artifact=str(artifact), head=runtime.run["expected_head"])
        if self.description_revision_task(runtime.run["tasks"], tasks, trigger):
            receipt = runtime.run["commander_receipts"]["commander:replan:replanning"]
            guard = {key: receipt[key] for key in
                     ("artifact", "artifact_digest", "dispatch_id", "snapshot_digest")}
            guard.update(task_id=trigger["task_id"], head=runtime.run["expected_head"],
                         plan_revision=len(runtime.run["plan_history"]))
            runtime.run["pending_replan"].update(candidate_policy="retain", retained_candidate=guard)
        runtime.run["replan_attempts"] = attempt
        runtime.run["commander_receipts"].pop("commander:replan:replanning", None)
        runtime.save()
        runtime.apply_pending_revision()

    def record_replan_stop(self, trigger, result, attempt, **diagnostics):
        runtime = self.runtime
        artifact = runtime.directory / "plans" / f"replan-{attempt}.json"
        write_json(artifact, {"trigger": trigger, "result": result, **diagnostics})
        runtime.run["replan_attempts"] = attempt
        runtime.run["replan_stopped"] = {"target": trigger.get("task_id") or "final",
            "trigger": deepcopy(trigger), "attempt": attempt, "artifact": str(artifact),
            "reason": result["reason"], **diagnostics}
        receipt = runtime.run["commander_receipts"].get("commander:replan:replanning")
        if receipt:
            runtime.run["replan_stopped"].update(dispatch_id=receipt["dispatch_id"],
                                              source_digest=hashlib.sha256(Path(receipt["artifact"]).read_bytes()).hexdigest())
        runtime.run.pop("pending_replan", None)
        runtime.run["commander_receipts"].pop("commander:replan:replanning", None)
        runtime.run["pending_format_corrections"].pop("commander:replan:replanning", None)
        runtime.save()
        runtime.synchronize_decision_requests()

    def apply_pending_revision(self):
        runtime = self.runtime
        pending = runtime.run["pending_replan"]
        if runtime.run["expected_head"] != pending["head"] or git.head(runtime.worktree, binding=runtime.run["worktree_binding"]) != pending["head"]:
            raise HarnessError("应用再规划前 HEAD 与已校验计划不符；保留现场")
        runtime.preflight_final_context(pending["tasks"], runtime.run["original_tasks"])
        runtime.ensure_exited()
        self.validate_retained_candidate()
        self.validate_retained_candidate(pending=pending)
        retain = pending.get("candidate_policy") == "retain"
        # A new prerequisite may own another scope. Never carry the blocked task's edits into it.
        if not retain and not git.clean(runtime.worktree, binding=runtime.run["worktree_binding"]):
            folder = runtime.directory / "recovery" / f"replan-{pending['attempt']}"
            if folder.exists():
                folder = folder.with_name(folder.name + "-" + uid("retry"))
            archive = git.archive_and_reset(runtime.worktree, runtime.run["expected_head"],
                                            folder, runtime.state / "worktrees", binding=runtime.run["worktree_binding"])
            runtime.event("replan_edits_archived", task_id=pending["trigger"].get("task_id"), artifact=archive)
        runtime.run["feedback"] = {"replan": pending["artifact"], "trigger": compact_feedback(pending["trigger"])}
        if not retain:
            runtime.run.pop("worker_recovery", None)
        runtime.run["pending_format_corrections"].clear()
        runtime.record_plan(pending["tasks"], pending["reason"], pending["trigger"], finish_replan=True)
        runtime.checkpoint()
        runtime.event("plan_revised", revision=len(runtime.run["plan_history"]) - 1)
