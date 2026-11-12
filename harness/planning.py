"""Own the single commander decision, plan history and revision publication.

The immutable plan file ``plans/revision-N.json`` is the only authority for a
revision's execution fields (candidate policy, directive, source, bound HEAD and
snapshot). The ledger keeps references and digests, plus the directive copy that
every worker dispatch reads; that copy is compared with the file before use.
"""
from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

from . import git
from .budgets import work_attempts
from .context import compact_feedback
from .contracts import apply_revision, DECIDE, validate_decision, validate_plan
from .recovery import read_bound_result, snapshot_digest, worker_recovery_context
from .requirements import missing_commands
from .services import RuntimeService
from .shared import skills_catalog
from .util import dump, HarnessError, strict_json_loads, uid, write_json

DECIDE_KEY = "commander:decide:run"


def attempt_limit_error(task_id, limit):
    return HarnessError(f"{task_id} 已达尝试次数上限（runtime.max_attempts 当前值 {limit}）；"
                        "先看 status 的 convergence 与审阅轮次，必要时提高 runtime.max_attempts 后普通 resume")


def directive_mismatch():
    return HarnessError("待派发指令与已发布计划或候选不符；保留现场，未启动 worker、未扣预算")


class Planner(RuntimeService):
    def validate_tasks(self, plan):
        tasks = validate_plan(plan)
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

    def decide_returned(self):
        receipt = self.context.run["commander_receipts"].get(DECIDE_KEY)
        return bool(receipt and ("artifact_digest" in receipt or Path(receipt["artifact"]).exists()))

    def approve_scope_extensions(self, patterns):
        """Record explicit operator authority for new tasks of a later decide request."""
        patterns = list(patterns)
        for pattern in patterns:
            if not isinstance(pattern, str):
                raise HarnessError("额外批准的文件范围必须是字符串")
            git.validate_scope_pattern(pattern)
        if not patterns:
            return
        if self.context.run["status"] in ("completed", "abandoned") or self.decide_returned():
            raise HarnessError("--allow-scope 仅用于未完成的 run，且不能有已返回、尚未处理的指挥决定；先普通 resume")
        approved = self.context.run.get("approved_scope", [])
        added = [pattern for pattern in dict.fromkeys(patterns) if pattern not in approved]
        if not added:
            return
        combined = [*approved, *added]
        # Audit first: interruption must not leave an unaudited authorization.
        self.context.event("scope_extension_approved", added_paths=added, approved_scope=combined)
        self.context.run["approved_scope"] = combined
        self.context.save()

    # ---- the decide request -------------------------------------------------
    def gated_tasks(self):
        return {t["id"] for t in self.context.run["tasks"]
                if self.services.workflow.task_decision_gate(t["id"]) is not None}

    def capacity_check(self, tasks, original_tasks=None):
        report = self.services.packets.final_context_capacity(tasks, original_tasks)
        if report["status"] == "required_overflow":
            fields = "、".join(f"{row['field']} {row['chars']}" for row in report.get("largest_fields", []))
            raise HarnessError(f"计划的整体审阅必要上下文 {report['required_chars']} 字符超过预算 "
                               f"{report['budget_chars']}" + (f"；最大的字段：{fields}" if fields else "")
                               + "；请减少或合并任务")
        return report

    def check_decision(self, payload, request_packet):
        """Branch rules against the request's own ready list and the current ledger."""
        trigger = request_packet.get("trigger", {}) if isinstance(request_packet, dict) else {}
        return validate_decision(
            payload, tasks=self.context.run["tasks"], accepted=self.context.run["accepted"],
            ready=trigger.get("ready", []), gated=self.gated_tasks(),
            approved_scope=self.context.run.get("approved_scope", []),
            validate_tasks=self.validate_tasks,
            capacity=lambda tasks, original: self.capacity_check(tasks, original))

    def operator_answer(self):
        """The latest continue answer to a commander question, until a decide lands."""
        dispatches = self.context.run["dispatches"]
        for request in reversed(self.services.decisions.decision_context()):
            if request["kind"] != "commander":
                continue
            response = request["response"]
            if not response or response["decision"] != "continue":
                return None
            if any(d.get("role") == "commander" and d.get("kind") == "decide"
                   and d.get("status") in ("completed", "invalid_asked")
                   for d in dispatches[response["dispatch_count"]:]):
                return None
            return {"request_id": request["id"], "task_id": request["task_id"], "question": request["question"],
                    "decision": response["decision"], "summary": response["summary"],
                    "artifact": response["artifact"], "artifact_digest": response["artifact_digest"]}
        return None

    def task_trigger(self, ready):
        run = self.context.run
        current = run.get("current_task")
        ids = [t["id"] for t in ready]
        return {"kind": "task", "ready": ids, "current_task": current,
                "feedback": compact_feedback(run.get("feedback")),
                "recovery": (worker_recovery_context(run, self.context.directory, current)
                             if current in ids else None)}

    def decide_packet(self, trigger):
        run = self.context.run
        packet = {"goal": run["goal"], "trigger": deepcopy(trigger),
                  "accepted": self.services.packets.accepted_context(),
                  "attempts": deepcopy(run["attempts"]), "work_attempts": work_attempts(run),
                  "approved_scope": deepcopy(run.get("approved_scope", [])),
                  "tool_constraints": self.services.packets.tool_constraints()}
        if run["tasks"]:
            packet.update(self.services.packets.plan_context())
        answer = self.operator_answer()
        if answer:
            packet["operator_answer"] = answer
        return packet

    def decide(self, trigger):
        """One commander call; its result lands as a directive, a revision or a question."""
        decision = self.services.dispatch.dispatch("commander", "decide", "run", DECIDE, self.decide_packet(trigger))
        action = decision.payload["action"]
        if action == "ask":
            return self.land_ask(decision)
        if action == "implement":
            return self.land_implement(decision)
        if not self.context.run["tasks"]:
            return self.land_initial_plan(decision)
        return self.land_revision(decision)

    # ---- landing: one save each, after every interruptible step ---------------
    def land_implement(self, decision):
        run = self.context.run
        receipt = run["commander_receipts"][DECIDE_KEY]
        payload = decision.payload
        run["current_task"] = payload["selected_task"]
        run["pending_directive"] = {"directive": {"selected_task": payload["selected_task"], "action": "implement",
                                                  "instruction": payload["instruction"]},
                                    "snapshot_digest": receipt["snapshot_digest"]}
        run["commander_receipts"].pop(DECIDE_KEY)
        self.context.save()
        return False

    def land_ask(self, decision):
        receipt = self.context.run["commander_receipts"][DECIDE_KEY]
        trigger = receipt["request"]["packet"].get("trigger", {})
        target = trigger.get("current_task") if trigger.get("kind") == "task" and trigger.get("current_task") else "final"
        source = {"artifact": receipt["artifact"], "artifact_digest": receipt["artifact_digest"],
                  "dispatch_id": receipt["dispatch_id"]}
        changed = {"candidate_changed": True} if decision.candidate_changed else None
        request = self.services.decisions.register_decision_request(
            target, "run", "commander", source, decision.payload.get("reason"),
            consume=(DECIDE_KEY, decision.veto, changed))
        error = self.services.workflow.decision_required(request)
        if changed:
            # Reported, never granted: later steps check their own snapshot and HEAD.
            self.context.event("commander_candidate_changed", dispatch_id=receipt["dispatch_id"], request_id=request["id"])
            error = HarnessError(f"{error}；指挥调用期间候选代码或 HEAD 已改变（派发条目 candidate_changed），"
                                 "提问照常登记、不认可这些改动，回答前请核对工作树")
        raise error

    def land_returned_ask(self):
        """Finish and register a returned, unlanded ask before any execution gate.

        Its hook and registration need only the common preconditions, not the
        bound HEAD or candidate; the run then stops at the question and every
        execution step keeps its own HEAD and snapshot gates.
        """
        if not self.decide_returned() or not self.services.receipts.returned_ask(DECIDE_KEY):
            return
        self.land_ask(self.services.receipts.finish_commander_receipt(DECIDE_KEY))

    @staticmethod
    def source(receipt):
        return {"dispatch_id": receipt["dispatch_id"], "artifact": receipt["artifact"],
                "artifact_digest": receipt["artifact_digest"]}

    def write_plan(self, revision, content):
        """Immutable plan file: identical bytes are reused, different bytes stop."""
        artifact = self.context.directory / "plans" / f"revision-{revision}.json"
        raw = (dump(content) + "\n").encode("utf-8")
        if artifact.exists() or artifact.is_symlink():
            if artifact.is_symlink() or artifact.read_bytes() != raw:
                raise HarnessError(f"计划文件已存在且内容不同；保留现场：{artifact}")
        else:
            write_json(artifact, content)
            if artifact.read_bytes() != raw:
                raise HarnessError("计划文件写入后内容不符；保留现场")
        return {"revision": revision, "artifact": str(artifact), "artifact_digest": hashlib.sha256(raw).hexdigest()}

    def read_plan(self, reference, label="计划文件"):
        try:
            raw = Path(reference["artifact"]).read_bytes()
        except OSError as error:
            raise HarnessError(f"{label}缺失或无法读取；保留现场") from error
        if hashlib.sha256(raw).hexdigest() != reference["artifact_digest"]:
            raise HarnessError(f"{label}已改变；保留现场")
        try:
            content = strict_json_loads(raw)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise HarnessError(f"{label}不是有效 JSON；保留现场") from error
        if not isinstance(content, dict) or content.get("revision") != reference["revision"]:
            raise HarnessError(f"{label}与台账引用不符；保留现场")
        return content

    def land_initial_plan(self, decision):
        receipt = self.context.run["commander_receipts"][DECIDE_KEY]
        tasks = self.validate_tasks({"tasks": decision.payload["new_tasks"]})
        return self.record_plan(tasks, decision.payload["reason"], source=self.source(receipt))

    def record_plan(self, tasks, reason, *, source=None):
        """Publish the first plan: the file first, then original_tasks, tasks and history together."""
        run = self.context.run
        reference = self.write_plan(0, {"revision": 0, "reason": reason, "tasks": tasks, "source": source})
        run["original_tasks"] = deepcopy(tasks)
        run["tasks"] = deepcopy(tasks)
        run["plan_history"] = [reference]
        run["commander_receipts"].pop(DECIDE_KEY, None)
        self.context.save()
        self.warn_broad([], tasks, 0)
        self.report_command_requirements()
        return False

    def warn_broad(self, previous_tasks, tasks, revision):
        # Tasks and their scopes are never removed or changed by a revision, so
        # the previous plan already holds every earlier version's patterns.
        previous = {(task["id"], pattern) for task in previous_tasks for pattern in task["allowed_paths"]}
        broad = [{"task_id": task["id"], "pattern": pattern} for task in tasks
                 for pattern in dict.fromkeys(task["allowed_paths"])
                 if (task["id"], pattern) not in previous and git.repository_wide_scope(pattern)]
        if broad:
            self.context.event("broad_scope_warning", revision=revision, scopes=broad)
            print("[harness] 警告：任务范围覆盖全仓库，受保护路径仍禁止修改："
                  + json.dumps(broad, ensure_ascii=True), file=sys.stderr, flush=True)

    def description_revision_task(self, previous, tasks, current):
        """Recognize only an actual description change to the current task."""
        if not current or current in self.context.run["accepted"] or len(previous) != len(tasks):
            return None
        changed = []
        for old, new in zip(previous, tasks):
            if old != new:
                if (old["id"] != current or new.get("id") != current
                        or {k: v for k, v in old.items() if k != "description"}
                        != {k: v for k, v in new.items() if k != "description"}):
                    return None
                changed.append(new)
        return changed[0] if len(changed) == 1 else None

    def retained(self, tasks, directive):
        current = self.context.run.get("current_task")
        return bool(self.description_revision_task(self.context.run["tasks"], tasks, current)
                    and directive["selected_task"] == current)

    def land_revision(self, decision):
        run = self.context.run
        receipt = run["commander_receipts"][DECIDE_KEY]
        payload = decision.payload
        tasks = decision.proposed
        directive = {"selected_task": payload["selected_task"], "action": "implement",
                     "instruction": payload["instruction"]}
        revision = len(run["plan_history"])
        reference = self.write_plan(revision, {
            "revision": revision, "reason": payload["reason"], "trigger": receipt["request"]["packet"].get("trigger"),
            "tasks": tasks, "source": self.source(receipt), "head": run["expected_head"],
            "snapshot_digest": receipt["snapshot_digest"],
            "candidate": "retain" if self.retained(tasks, directive) else "archive", "directive": directive})
        run["pending_revision"] = reference
        run["commander_receipts"].pop(DECIDE_KEY)
        self.context.save()
        return self.apply_pending_revision()

    # ---- publication ---------------------------------------------------------
    def check_revision_file(self, pending):
        """Before any archive, reset or publish: the file, its source and a replay agree."""
        run = self.context.run
        if pending.get("revision") != len(run["plan_history"]):
            raise HarnessError("待应用修订与计划版本不符；保留现场")
        content = self.read_plan(pending, "待应用的计划文件")
        source = content.get("source")
        if not isinstance(source, dict) or set(source) != {"dispatch_id", "artifact", "artifact_digest"}:
            raise HarnessError("待应用修订的来源无效；保留现场")
        entries = [d for d in run["dispatches"] if d.get("id") == source["dispatch_id"]]
        if (len(entries) != 1 or (entries[0].get("role"), entries[0].get("kind"), entries[0].get("task_id"))
                != ("commander", "decide", "run") or entries[0].get("invoked") is not True
                or source["artifact"] != str(self.context.directory / "dispatches" / source["dispatch_id"] / "returned-result.json")):
            raise HarnessError("待应用修订的来源派发不符；保留现场")
        result, _ = read_bound_result(source, "待应用修订的")
        if (not isinstance(result, dict) or result.get("task_id") != "run"
                or result.get("dispatch_id") != source["dispatch_id"] or result.get("action") != "revise"):
            raise HarnessError("待应用修订的原件不是这次修订；保留现场")
        tasks = apply_revision(run["tasks"], result, run["accepted"],
                               additional_allowed_paths=run.get("approved_scope", []), scope_violations=[])
        directive = {"selected_task": result.get("selected_task"), "action": "implement",
                     "instruction": result.get("instruction")}
        if (content.get("tasks") != tasks or content.get("directive") != directive
                or content.get("candidate") != ("retain" if self.retained(tasks, directive) else "archive")):
            raise HarnessError("待应用的计划文件与原件和当前台账不符；保留现场")
        head = git.head(self.context.worktree, binding=run["worktree_binding"])
        if content.get("head") != run["expected_head"] or head != run["expected_head"]:
            raise HarnessError("应用修订前 HEAD 与已校验计划不符；保留现场")
        return content

    def apply_pending_revision(self):
        run = self.context.run
        pending = run["pending_revision"]
        content = self.check_revision_file(pending)
        self.context.ensure_exited()
        directive = content["directive"]
        task = next(t for t in content["tasks"] if t["id"] == directive["selected_task"])
        archive = None
        if content["candidate"] == "retain":
            if snapshot_digest(self.context.snapshot()) != content["snapshot_digest"]:
                raise HarnessError("修订保留候选的代码或暂存区已变化；保留现场，禁止归档重置")
            self.services.tasks.check_task_scope(task)
            bound = content["snapshot_digest"]
        else:
            # A new prerequisite may own another scope. Never carry the blocked
            # task's edits into it: copy them out, then reset.
            if not git.clean(self.context.worktree, binding=run["worktree_binding"]):
                folder = self.context.directory / "recovery" / f"revision-{pending['revision']}"
                if folder.exists():
                    folder = folder.with_name(folder.name + "-" + uid("retry"))
                archive = git.archive_and_reset(self.context.worktree, run["expected_head"],
                                                folder, self.context.state / "worktrees", binding=run["worktree_binding"])
            bound = snapshot_digest(self.context.snapshot())
        previous = run["tasks"]
        run["plan_history"].append({key: pending[key] for key in ("revision", "artifact", "artifact_digest")})
        run["tasks"] = deepcopy(content["tasks"])
        run["current_task"] = directive["selected_task"]
        run["pending_directive"] = {"directive": deepcopy(directive), "snapshot_digest": bound,
                                    "revision": pending["revision"]}
        run["feedback"] = {"revision": pending["revision"], "plan_artifact": pending["artifact"],
                           "trigger": deepcopy(content.get("trigger"))}
        if content["candidate"] != "retain":
            run.pop("worker_recovery", None)
        run.pop("pending_revision")
        self.context.save()
        if archive is not None:
            self.context.event("revision_edits_archived", task_id=directive["selected_task"], artifact=archive)
        self.warn_broad(previous, content["tasks"], pending["revision"])
        self.report_command_requirements()
        self.context.checkpoint()
        self.context.event("plan_revised", revision=pending["revision"])
        return False

    # ---- the saved directive before a worker starts ----------------------------
    def check_directive(self, task_id, snapshot, *, prepared=False):
        """Bind the saved directive to its task, its published plan and its candidate.

        Called before ``pre_agent`` with the current snapshot, and again after
        local preparation (``prepared``) with the snapshot the invocation binds,
        before the directive or any budget is consumed.
        """
        run = self.context.run
        saved = run.get("pending_directive")
        if not saved:
            return
        directive = saved["directive"]
        if directive.get("selected_task") != task_id or run.get("current_task") != task_id:
            raise directive_mismatch()
        retain = False
        if "revision" in saved:
            history = run["plan_history"]
            if saved["revision"] != len(history) - 1 or saved["revision"] < 1:
                raise directive_mismatch()
            content = self.read_plan(history[saved["revision"]])
            if content.get("directive") != directive or content.get("tasks") != run["tasks"]:
                raise directive_mismatch()
            retain = content.get("candidate") == "retain"
            if retain and content.get("snapshot_digest") != saved["snapshot_digest"]:
                raise directive_mismatch()
        if not prepared:
            if snapshot_digest(snapshot) != saved["snapshot_digest"]:
                raise HarnessError("待派发指令的代码快照已变化；请核对并恢复原快照")
        elif retain and snapshot_digest(snapshot) != saved["snapshot_digest"]:
            # A retained candidate is the one the revision bound; a preparation
            # hook must not replace it. Ordinary directives keep current behavior.
            raise directive_mismatch()

    def verify_plan_authority(self):
        """Every published plan version is intact, and the ledger's mirrors equal their files.

        ``plan_history`` holds references only; ``tasks`` mirrors the latest
        version and ``original_tasks`` revision 0. A missing, changed or
        diverging file stops before anything uses the plan. Older versions are
        verified too: they are the bound history each later revision replayed.
        """
        run = self.context.run
        history = run["plan_history"]
        contents = [self.read_plan(row, f"已发布的计划文件（revision-{row['revision']}）") for row in history]
        if history and (contents[-1].get("tasks") != run["tasks"] or contents[0].get("tasks") != run["original_tasks"]):
            raise HarnessError("台账任务表与已发布的计划文件不符；保留现场，恢复计划文件或台账的原字节")

    def published_directive_missing(self):
        """A published revision whose directive vanished before any model call."""
        run = self.context.run
        feedback = run.get("feedback")
        history = run["plan_history"]
        if (run.get("pending_directive") or not isinstance(feedback, dict)
                or feedback.get("revision") != len(history) - 1 or len(history) < 2):
            return False
        content = self.read_plan(history[-1])
        source = (content.get("source") or {}).get("dispatch_id")
        position = next((i for i, d in enumerate(run["dispatches"]) if d.get("id") == source), None)
        if position is None:
            return True
        return not any(d.get("role") in ("commander", "worker", "reviewer") and d.get("invoked") is True
                       for d in run["dispatches"][position + 1:])
