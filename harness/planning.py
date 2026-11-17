"""Own the single commander decision, plan history and revision publication.

The immutable plan file ``plans/revision-N.json`` is the only authority for a
revision's execution fields (candidate policy, directive, source, bound HEAD and
snapshot). The ledger keeps references and digests, plus the directive copy that
every worker dispatch reads; that copy is compared with the file before use.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

from . import git, stops
from .budgets import work_attempts
from .config import PROJECT_FILE
from .context import compact_feedback
from .contracts import apply_revision, DECIDE, validate_decision, validate_plan
from .receipts import judged_decide, ProposalCapacityHold, ScopeHold, SuppliedPlanHold
from .recovery import read_bound_result, snapshot_digest, worker_recovery_context
from .requirements import missing_commands
from .scope import gaps as scope_gaps, proposal_gaps, proposal_request_id, scope_args, suggestions
from .services import RuntimeService
from .shared import skills_catalog
from .util import dump, HarnessError, strict_json_loads, uid, write_json

DECIDE_KEY = "commander:decide:run"


def attempt_limit_error(task_id, limit):
    return HarnessError(f"{task_id} 已达尝试次数上限（runtime.max_attempts 当前值 {limit}）；"
                        "先看 status 的 convergence 与审阅轮次，必要时提高 runtime.max_attempts 后普通 resume", stop=stops.make("budget", "task_attempts", data={"config_key": "runtime.max_attempts", "limit": limit}, source={"task_id": task_id}))


def directive_mismatch(task_id=None):
    """The saved directive (for ``task_id``, the task it is checked against) disagrees with the plan or candidate."""
    return HarnessError("待派发指令与已发布计划或候选不符；保留现场，未启动 worker、未扣预算",
                        stop=stops.make("workspace_mismatch", "directive_mismatch",
                                        source={"task_id": task_id} if task_id is not None else None))


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
                      + f"；对应 worker 派发前必须在{PROJECT_FILE}的 permissions.worker_commands 追加精确 argv。",
                      file=sys.stderr, flush=True)
        return missing

    def require_task_commands(self, task_ids, reason):
        """``reason`` is ``selected_task`` (one chosen task) or ``all_ready_tasks`` (none can run)."""
        missing = self.report_command_requirements()
        unmet = {tid: missing[tid] for tid in task_ids if tid in missing}
        if unmet:
            source = {"task_id": task_ids[0]} if reason == "selected_task" else {}
            raise HarnessError("任务所需命令尚未授权：" + json.dumps(unmet, ensure_ascii=True)
                               + f"；在{PROJECT_FILE}的 permissions.worker_commands 追加这些精确 argv 后普通 resume；"
                                 "计划批准不授予命令权限，本检查不消耗 worker 尝试或模型派发额度",
                               stop=stops.make("missing_commands", reason, source=source,
                                               data={"config_key": "permissions.worker_commands", "missing": unmet}))

    def decide_returned(self):
        receipt = self.context.run["commander_receipts"].get(DECIDE_KEY)
        return bool(receipt and ("artifact_digest" in receipt or Path(receipt["artifact"]).exists()))

    # ---- the decide request -------------------------------------------------
    def gated_tasks(self):
        return {t["id"] for t in self.context.run["tasks"]
                if self.services.workflow.task_decision_gate(t["id"]) is not None}

    def capacity_report(self, tasks, original_tasks=None):
        """A running condition (spec-6 2.3.1): the final review's required input under current configuration and records."""
        return self.services.packets.final_context_capacity(tasks, original_tasks)

    @staticmethod
    def capacity_error(report):
        fields = "、".join(f"{row['field']} {row['chars']}" for row in report.get("largest_fields", []))
        return (f"计划的整体审阅必要上下文 {report['required_chars']} 字符超过预算 {report['budget_chars']}"
                + (f"；最大的字段：{fields}" if fields else "") + "；请减少或合并任务")

    def check_decision(self, payload, request_packet):
        """The original's contract against its frozen baseline, and its boundary gaps: (action, proposed, gaps).

        No running condition (capacity) is consulted here; the request's own ready list is the baseline.
        """
        trigger = request_packet.get("trigger", {}) if isinstance(request_packet, dict) else {}
        return validate_decision(
            payload, tasks=self.context.run["tasks"], accepted=self.context.run["accepted"],
            ready=trigger.get("ready", []), gated=self.gated_tasks(),
            scope=self.context.run["scope"], validate_tasks=self.validate_tasks)

    def decide_landed(self, entry):
        """Whether this decide landed: its receipt was consumed by a landing save (spec-6 2.7).

        A rejection is not a landing; a held, kept or not yet landed original still has its receipt.
        """
        if entry.get("role") != "commander" or entry.get("kind") != "decide" or entry.get("invoked") is not True:
            return False
        if entry.get("status") == "invalid_asked":
            return True
        receipt = self.context.run["commander_receipts"].get(DECIDE_KEY) or {}
        return (entry.get("status") == "completed" and "proposal_rejection" not in entry
                and receipt.get("dispatch_id") != entry.get("id"))

    def operator_answer(self):
        """The latest continue answer to a commander question, until a decide actually lands."""
        dispatches = self.context.run["dispatches"]
        for request in reversed(self.services.decisions.decision_context()):
            if request["kind"] != "commander":
                continue
            response = request["response"]
            if not response or response["decision"] != "continue":
                return None
            if any(self.decide_landed(d) for d in dispatches[response["dispatch_count"]:]):
                return None
            return {"request_id": request["id"], "task_id": request["task_id"], "question": request["question"],
                    "decision": response["decision"], "summary": response["summary"],
                    "artifact": response["artifact"], "artifact_digest": response["artifact_digest"]}
        return None

    def task_trigger(self, ready):
        run = self.context.run
        current = run.get("current_task")
        ids = [t["id"] for t in ready]
        feedback = compact_feedback(run.get("feedback"))
        return {"kind": "task", "ready": ids, "current_task": current, "feedback": feedback,
                "recovery": (worker_recovery_context(run, self.context.directory, current, feedback=feedback)
                             if current in ids else None)}

    def decide_packet(self, trigger):
        run = self.context.run
        packet = {"goal": run["goal"], "trigger": deepcopy(trigger),
                  "accepted": self.services.packets.accepted_context(),
                  "attempts": deepcopy(run["attempts"]), "work_attempts": work_attempts(run),
                  "scope": deepcopy(run["scope"]),
                  "tool_constraints": self.services.packets.tool_constraints()}
        if run["tasks"]:
            packet.update(self.services.packets.plan_context())
        answer = self.operator_answer()
        if answer:
            packet["operator_answer"] = answer
        rejected = self.rejected_revision()
        if rejected:
            packet["rejected_revision"] = rejected
        return packet

    def rejected_revision(self):
        """The latest rejected proposal, shown until a later decide actually lands (spec-6 2.5.1)."""
        last = None
        for entry in self.context.run["dispatches"]:
            if "proposal_rejection" in entry:
                last = entry
            elif last is not None and self.decide_landed(entry):
                last = None
        if last is None:
            return None
        reference = last["proposal_rejection"]
        with stops.evidence_reads(source={"artifact": reference["artifact"], "dispatch_id": last["id"]}):
            record, _ = read_bound_result(reference, "提案拒绝")
            source = record.get("source") if isinstance(record, dict) else None
            if (not isinstance(source, dict) or source.get("dispatch_id") != last["id"]
                    or record.get("run_id") != self.context.run["id"]):
                raise HarnessError("提案拒绝文件与派发条目不符；保留现场")
        return {"request_id": record["request_id"], "kind": record["kind"], "dispatch_id": last["id"],
                "artifact": source["artifact"], "artifact_digest": source["artifact_digest"],
                "reason": record["reason"], "rejection_artifact": reference["artifact"],
                "rejection_artifact_digest": reference["artifact_digest"]}

    # ---- a judged proposal that cannot land now (spec-6 2.3) -------------------
    def hold_stop(self, receipt, payload, gaps, proposed):
        """The stop of a judged original missing only scope; reads nothing, writes nothing."""
        run = self.context.run
        initial = not run["tasks"]
        request_id = proposal_request_id(run, receipt)
        entries, widened = suggestions(gaps)
        missing = missing_commands(proposed, run["accepted"], self.context.config.data)
        data = {"suggested_scope": entries, "gaps": gaps, "missing_commands": missing,
                "scope": list(run["scope"]), "widened": widened}
        if missing:
            data["config_key"] = "permissions.worker_commands"
        if not initial:
            data["selected_task"] = payload["selected_task"]
        stop = stops.make("scope_extension", "initial_plan" if initial else "revision", data=data,
                          source={"request_id": request_id, "dispatch_id": receipt["dispatch_id"],
                                  "artifact": receipt["artifact"], "artifact_digest": receipt["artifact_digest"]})
        label = "首版计划" if initial else "修订"
        return ScopeHold(f"指挥{label}有模式无法证明在范围边界内（当前规则无法证明包含）："
                         + json.dumps(gaps, ensure_ascii=False) + "；提案已持有，未调用模型。核对后扩大边界："
                         + f"resume RUN {scope_args(entries)}（建议条目可能比缺口宽，见 widened），"
                         + f"或拒绝：resume RUN --reject-request FILE（request_id={request_id}）；完整缺口见 status RUN",
                         stop=stop)

    def capacity_hold(self, receipt, report):
        """The stop of a judged original over the final review's capacity on a replay; the original is kept."""
        request_id = proposal_request_id(self.context.run, receipt)
        stop = stops.make("budget", "proposal_capacity",
                          data={"config_key": "runtime.context_chars", "required_chars": report["required_chars"],
                                "budget_chars": report["budget_chars"]},
                          source={"request_id": request_id, "dispatch_id": receipt["dispatch_id"],
                                  "artifact": receipt["artifact"], "artifact_digest": receipt["artifact_digest"]})
        return ProposalCapacityHold(self.capacity_error(report) + f"；已判定合格的提案保留（request_id={request_id}），"
                                    "未调用模型；提高 runtime.context_chars 后普通 resume，或 resume RUN --reject-request FILE",
                                    stop=stop)

    def returned_proposal(self):
        """The returned, judged, unlanded revise original: (receipt, payload, request_id, gaps), or None.

        Read-only: the original is read by its bound digest and its gaps are computed by
        pure functions; nothing else is read or written (status and the rejection preflight).
        """
        run = self.context.run
        receipt = run["commander_receipts"].get(DECIDE_KEY)
        if receipt is None or "artifact_digest" not in receipt:
            return None
        entry = next((d for d in run["dispatches"] if d.get("id") == receipt["dispatch_id"]), None)
        if entry is None or not judged_decide(entry) or "proposal_rejection" in entry:
            return None
        result, _ = read_bound_result(receipt, "指挥")
        if not isinstance(result, dict) or result.get("action") != "revise":
            return None
        payload = {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
        with stops.classify("runtime_failure", "invariant"):
            gaps = proposal_gaps(run, payload)
        return receipt, payload, proposal_request_id(run, receipt), gaps

    def prepare_proposal_rejection(self, value):
        """Every check of a proposal rejection, before any write (resume step 9); returns what to publish."""
        run = self.context.run
        found = self.returned_proposal()
        if found is None or found[2] != value["request_id"]:
            raise HarnessError("拒绝的 request_id 不是已判定合格、尚未落地的指挥提案；用 status RUN 核对",
                               stop=stops.make("invalid_input", "stale_request", data={"option": "--reject-request"},
                                               source={"request_id": value["request_id"]}))
        receipt, payload, request_id, gaps = found
        if self.services.receipts.candidate_changed(receipt):
            raise HarnessError("指挥提案绑定的候选或 HEAD 已改变；恢复原状后再拒绝",
                               stop=stops.make("workspace_mismatch", "candidate_changed",
                                               source={"dispatch_id": receipt["dispatch_id"]}))
        # A plan file written for this proposal before an interruption: the only way to tell it is
        # this proposal's own is to rebuild its bytes from the original and the frozen baseline.
        orphan = self.context.directory / "plans" / f"revision-{len(run['plan_history'])}.json"
        prewritten = None
        if orphan.exists() or orphan.is_symlink():
            with stops.classify("runtime_failure", "invariant"):
                expected = (dump(self.plan_content(receipt, payload)) + "\n").encode("utf-8")
            with stops.evidence_reads(source={"artifact": str(orphan)}):
                if orphan.is_symlink() or orphan.read_bytes() != expected:
                    raise HarnessError("未登记的计划文件不是被拒提案的预写文件；保留现场")
            prewritten = {"artifact": str(orphan), "artifact_digest": hashlib.sha256(expected).hexdigest()}
        record = {"run_id": run["id"], "request_id": request_id, "kind": "revision" if run["tasks"] else "initial_plan",
                  "reason": value["reason"], "gaps": gaps, "source": self.source(receipt),
                  "base_revision": len(run["plan_history"]) - 1, "snapshot_digest": receipt["snapshot_digest"]}
        return {"record": record, "prewritten": prewritten}

    def record_proposal_rejection(self, prepared):
        """Publish a prepared rejection: its file, the archive of the proposal's plan file, then one save."""
        run = self.context.run
        record = prepared["record"]
        raw = (dump(record) + "\n").encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        artifact = self.context.directory / "proposal-rejections" / f"{digest}.json"
        if not artifact.exists() and not artifact.is_symlink():
            write_json(artifact, record)
        with stops.evidence_reads(source={"artifact": str(artifact)}):
            if artifact.is_symlink() or artifact.read_bytes() != raw:
                raise HarnessError("提案拒绝文件内容不符；保留现场")
        prewritten = prepared["prewritten"]
        if prewritten is not None:
            # The bytes the preflight attributed to this proposal, read again right before the rename.
            orphan = Path(prewritten["artifact"])
            with stops.evidence_reads(source={"artifact": str(orphan)}):
                if orphan.is_symlink() or hashlib.sha256(orphan.read_bytes()).hexdigest() != prewritten["artifact_digest"]:
                    raise HarnessError("未登记的计划文件在拒绝预检之后改变；保留现场")
            target = self.context.directory / "plans" / "rejected" / f"{prewritten['artifact_digest']}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(orphan, target)
            self.context.observe("proposal_plan_archived", artifact=str(target),
                                 revision=len(run["plan_history"]), request_id=record["request_id"])
        receipts = run["commander_receipts"]
        entry = next(d for d in run["dispatches"] if d.get("id") == receipts[DECIDE_KEY]["dispatch_id"])
        entry["proposal_rejection"] = {"artifact": str(artifact), "artifact_digest": digest}
        run["commander_receipts"] = {key: value for key, value in receipts.items() if key != DECIDE_KEY}
        try:
            self.context.save()
        except BaseException:
            run["commander_receipts"] = receipts
            entry.pop("proposal_rejection", None)
            raise
        self.context.observe("proposal_rejected", request_id=record["request_id"], dispatch_id=entry["id"],
                             artifact=str(artifact))

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
            head = git.head(self.context.worktree, binding=self.context.run["worktree_binding"])
            if head != self.context.run["expected_head"]:
                # The answer must wait for HEAD: the resume gate refuses a moved HEAD before landing it.
                stop = stops.make("workspace_mismatch", "head_changed",
                                  data={"expected_head": self.context.run["expected_head"], "question_registered": True},
                                  source={"request_id": request["id"], "task_id": request["task_id"]})
            else:
                stop = stops.make("operator_decision", "answer_required",
                                  data={**{k: v for k, v in error.stop["data"].items() if k != "detail"},
                                        "candidate_changed": True},
                                  source=error.stop["source"])
            error = HarnessError(f"{error}；指挥调用期间候选代码或 HEAD 已改变（派发条目 candidate_changed），"
                                 "提问照常登记、不认可这些改动，回答前请核对工作树", stop=stop)
        raise error

    def land_returned_ask(self):
        """Finish and register a returned, unlanded ask before any execution gate.

        Its registration needs only the common preconditions, not the
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

    @stops.reads_evidence
    def read_plan(self, reference, label="计划文件"):
        try:
            raw = Path(reference["artifact"]).read_bytes()
        except OSError as error:
            stops.keep_local_io(error)
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

    @staticmethod
    def first_plan(tasks, reason, source):
        return {"revision": 0, "reason": reason, "tasks": tasks, "source": source}

    def plan_content(self, receipt, payload):
        """The plan file a returned revise original publishes, rebuilt from it and the frozen baseline.

        Landing writes exactly these bytes, and a rejection recognizes a plan file written
        before an interruption by them (spec-6 2.5).
        """
        run = self.context.run
        if not run["tasks"]:
            return self.first_plan(self.validate_tasks({"tasks": payload["new_tasks"]}), payload["reason"],
                                   self.source(receipt))
        tasks = apply_revision(run["tasks"], payload, run["accepted"], scope=run["scope"], gaps={})
        directive = {"selected_task": payload["selected_task"], "action": "implement",
                     "instruction": payload["instruction"]}
        revision = len(run["plan_history"])
        return {"revision": revision, "reason": payload["reason"], "trigger": receipt["request"]["packet"].get("trigger"),
                "tasks": tasks, "source": self.source(receipt), "head": run["expected_head"],
                "snapshot_digest": receipt["snapshot_digest"],
                "candidate": "retain" if self.retained(tasks, directive) else "archive", "directive": directive}

    def land_initial_plan(self, decision):
        receipt = self.context.run["commander_receipts"][DECIDE_KEY]
        content = self.plan_content(receipt, decision.payload)
        return self.record_plan(content["tasks"], content["reason"], source=content["source"])

    def record_plan(self, tasks, reason, *, source=None):
        """Publish the first plan: the boundary first, then the file, then original_tasks, tasks and history together.

        Both first plans pass here: a model's (``source``) and a supplied one (``source`` None), so a
        supplied plan published on resume is checked against the boundary declared since its start.
        """
        run = self.context.run
        missing = scope_gaps({t["id"]: t["allowed_paths"] for t in tasks}, run["scope"]) if run["scope"] else {}
        if missing:
            if source is not None:
                raise HarnessError("首版发布前的边界复核失败：判定刚放行的原件越出范围边界；保留现场",
                                   stop=stops.make("runtime_failure", "invariant"))
            entries, widened = suggestions(missing)
            raise SuppliedPlanHold(
                "供给的首版计划越出范围边界（当前规则无法证明包含）：" + json.dumps(missing, ensure_ascii=False)
                + f"；未发布、未调用模型；扩大边界 resume RUN {scope_args(entries)}，或 abandon RUN",
                stop=stops.make("scope_extension", "supplied_plan",
                                data={"suggested_scope": entries, "gaps": missing, "scope": list(run["scope"]),
                                      "widened": widened}))
        reference = self.write_plan(0, self.first_plan(tasks, reason, source))
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
        """Recognize a change to the current task's description, scope or commands only (all append-only but the first)."""
        if not current or current in self.context.run["accepted"] or len(previous) != len(tasks):
            return None
        changed = []
        mutable = ("description", "allowed_paths", "required_commands")
        for old, new in zip(previous, tasks):
            if old != new:
                if (old["id"] != current or new.get("id") != current
                        or {k: v for k, v in old.items() if k not in mutable}
                        != {k: v for k, v in new.items() if k not in mutable}):
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
        content = self.plan_content(receipt, decision.payload)
        reference = self.write_plan(content["revision"], content)
        run["pending_revision"] = reference
        run["commander_receipts"].pop(DECIDE_KEY)
        self.context.save()
        return self.apply_pending_revision()

    # ---- publication ---------------------------------------------------------
    @stops.reads_evidence
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
        # The boundary only grows: a revision admitted inside it is still inside (spec-6 2.6).
        tasks = apply_revision(run["tasks"], result, run["accepted"], scope=run["scope"])
        directive = {"selected_task": result.get("selected_task"), "action": "implement",
                     "instruction": result.get("instruction")}
        if (content.get("tasks") != tasks or content.get("directive") != directive
                or content.get("candidate") != ("retain" if self.retained(tasks, directive) else "archive")):
            raise HarnessError("待应用的计划文件与原件和当前台账不符；保留现场")
        head = git.head(self.context.worktree, binding=run["worktree_binding"])
        if content.get("head") != run["expected_head"] or head != run["expected_head"]:
            raise HarnessError("应用修订前 HEAD 与已校验计划不符；保留现场", stop=stops.make("workspace_mismatch", "head_changed", data={"expected_head": run["expected_head"]}))
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
                raise HarnessError("修订保留候选的代码或暂存区已变化；保留现场，禁止归档重置", stop=stops.make("workspace_mismatch", "candidate_changed"))
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
    def check_directive(self, task_id, snapshot):
        """Bind the saved directive to its task, its published plan and its candidate.

        Called once, after local preparation, with the snapshot the invocation
        binds, before the directive or any budget is consumed. Every directive,
        retained or not, requires that snapshot to equal the saved one.
        """
        run = self.context.run
        saved = run.get("pending_directive")
        if not saved:
            return
        directive = saved["directive"]
        if directive.get("selected_task") != task_id or run.get("current_task") != task_id:
            raise directive_mismatch(task_id)
        if "revision" in saved:
            history = run["plan_history"]
            if saved["revision"] != len(history) - 1 or saved["revision"] < 1:
                raise directive_mismatch(task_id)
            content = self.read_plan(history[saved["revision"]])
            if content.get("directive") != directive or content.get("tasks") != run["tasks"]:
                raise directive_mismatch(task_id)
            if content.get("candidate") == "retain" and content.get("snapshot_digest") != saved["snapshot_digest"]:
                raise directive_mismatch(task_id)
        if snapshot_digest(snapshot) != saved["snapshot_digest"]:
            raise HarnessError("待派发指令的代码快照已变化；请核对并恢复原快照", stop=stops.make("workspace_mismatch", "candidate_changed", source={"task_id": saved["directive"]["selected_task"]}))

    @stops.reads_evidence
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
