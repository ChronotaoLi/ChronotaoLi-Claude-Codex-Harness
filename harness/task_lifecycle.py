"""Own task verification/review stages and the atomic acceptance/post-accept handoffs."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from . import git
from .budgets import work_attempts
from .context import compact_checks, compact_feedback, compact_reviews, instruction_file_changes
from .contracts import envelope, judge_review, review_approved, review_schema, task_directive, WORK
from .memory import Memory, validate_proposals
from .input_projection import validate_dispatch_indexes as validate_input_dispatch_indexes
from .path_evidence import validate_dispatch_indexes
from .progress import CheckpointDeclarationError
from .recovery import invalidate_acceptance_review, snapshot_digest, worker_recovery_context
from .services import RuntimeService
from .continuation import record_continuation
from .review_progress import (
    bind_review_source, validate_review_source, claim_gaps, claims_for_review, disposition_gaps,
    required_findings, delivered_worker_findings,
)
from .verification_plan import candidate_preflight
from .verification import checks_passed
from .shared import HookError
from .util import dump, HarnessError, read_json, write_json

# The environment-blocked stop message reaches the ledger error, run_stopped and
# the on_error hook; it carries an excerpt and the digest-bound full artifact.
ENVIRONMENT_BLOCKER_EXCERPT_CHARS = 2000


def environment_blocker_excerpt(result):
    """Blockers then summary, together at most the excerpt bound, truncation stated."""
    text = f"{'；'.join(result['blockers'])} {result['summary']}"
    if len(text) <= ENVIRONMENT_BLOCKER_EXCERPT_CHARS:
        return text
    return (text[:ENVIRONMENT_BLOCKER_EXCERPT_CHARS]
            + f"……（已截断：共 {len(text)} 字符，保留前 {ENVIRONMENT_BLOCKER_EXCERPT_CHARS} 字符）")


class TaskLifecycle(RuntimeService):
    def finish_post_accept(self):
        runtime = self.context
        pending = self.context.run.get("pending_post_accept")
        if not pending:
            return False
        completion = pending["completion"]
        payload = pending["payload"]
        tid, commit = payload["task_id"], payload["commit"]
        # These IDs are committed in the same SQLite transaction as the records.
        # A crash after a successful write but before save must not create a version.
        if not completion.get("task_memory_id"):
            self.context.store.check_current()
            completion["task_memory_id"] = self.context.memory.remember(
                **completion["task_memory"], operation_id=completion["id"] + ":task")
            self.context.save()
        if completion["knowledge"]["status"] == "pending":
            self.context.store.check_current()
            try:
                identifiers = self.context.memory.remember_proposals(
                    completion["proposals"], completion["approved_keys"], commit, self.context.run["id"], tid,
                    completion["knowledge_ttl_days"], operation_id=completion["id"] + ":knowledge")
            except HarnessError as error:
                completion["knowledge"] = {"status": "discarded", "reason": str(error)}
            else:
                completion["knowledge"] = {"status": "promoted", "memory_ids": identifiers}
            self.context.save()
        if completion["knowledge"]["status"] == "discarded":
            self.acceptance_event_once("memory_proposals_discarded", completion["id"] + ":discarded",
                                       task_id=tid, reason=completion["knowledge"]["reason"])
        self.acceptance_event_once("task_accepted", completion["id"], task_id=tid, commit=commit)
        # A crash after hook success but before saving can retry the hook.
        # Memory completion and this gate both precede any subsequent dispatch.
        pending["hook_attempts"] = pending.get("hook_attempts", 0) + 1
        self.context.save()
        self.context.emit_hook("post_accept", payload, Path(pending["logs"]) / f"post_accept-{pending['hook_attempts']}")
        self.context.run.pop("pending_post_accept")
        self.context.save()
        self.services.workflow.synchronize_decision_requests()
        return True

    def acceptance_event_once(self, event, event_id, **fields):
        """Recover the event-append/state-save boundary using the stable acceptance ID."""
        runtime = self.context
        path = self.context.directory / "events.jsonl"
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        for line in content.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("event_id") == event_id:
                return
        if content and not content.endswith("\n"):
            # Preserve a partial final append as evidence without joining the retry to it.
            with path.open("a", encoding="utf-8") as stream:
                stream.write("\n")
        self.context.event(event, event_id=event_id, **fields)

    def task_attempt(self, ready):
        runtime = self.context
        current = self.context.run.get("current_task")
        if current:
            task = next(t for t in self.context.run["tasks"] if t["id"] == current)
            self.check_task_scope(task)
        if current and self.services.budgets.task_budget_used(current) >= self.context.config.data["runtime"]["max_attempts"]:
            raise HarnessError(f"{current} 已达尝试次数上限")
        # A task's prerequisites must not prevent selecting other ready work.
        # The selected worker is checked again before its invocation is counted.
        if current:
            self.services.environment.check_environment(current)
        saved_directive = self.context.run.get("pending_directive")
        pristine = len(ready) == 1 and not self.context.run.get("feedback") and not self.context.run["attempts"].get(ready[0]["id"])
        if saved_directive:
            if snapshot_digest(self.context.snapshot()) != saved_directive["snapshot_digest"]:
                raise HarnessError("待派发指令的代码快照已变化；请核对并恢复原快照")
            directive = saved_directive["directive"]
        elif pristine:
            directive = {"selected_task": ready[0]["id"], "action": "implement",
                         "instruction": "Implement this task within its complete stated scope and acceptance. Inspect dependency evidence and follow the supplied tool and verification constraints."}
        else:
            schema = task_directive(t["id"] for t in ready)
            directive = self.services.dispatch.dispatch("commander", "dispatch", "dispatching", schema,
                        {"goal": self.context.run["goal"], "ready": ready, "accepted": self.services.packets.accepted_context(),
                         "feedback": compact_feedback(self.context.run.get("feedback")), "attempts": self.context.run["attempts"],
                         "work_attempts": work_attempts(self.context.run),
                         "recovery": worker_recovery_context(self.context.run, self.context.directory, current) if current in {t["id"] for t in ready} else None,
                         "tool_constraints": self.services.packets.tool_constraints(),
                         "instruction": "Select one ready task and return action=implement for a feasible implementation or repair. Return action=replan when feedback exposes missing prerequisites or acceptance that cannot be met within current scope and decisions; explain the unmet requirement in instruction. Replanning can stop without another worker. Retain the original acceptance: completing a report of unresolved failures is not completing the task."}, task=ready[0])
        task = next(t for t in ready if t["id"] == directive["selected_task"])
        tid = task["id"]
        if directive["action"] == "replan":
            receipt = self.context.run["commander_receipts"]["commander:dispatch:dispatching"]
            trigger = {"task_id": tid, "feedback": deepcopy(self.context.run.get("feedback")),
                       "coordination": {**{key: receipt[key] for key in
                           ("artifact", "artifact_digest", "dispatch_id", "snapshot_digest")},
                           "instruction": directive["instruction"]}}
            # Consume the selection and publish its successor in one save. Resume
            # must replan even if the next call's budget or preparation fails.
            self.context.run["current_task"] = tid
            self.context.run["pending_replan"] = {"stage": "pending", "trigger": trigger}
            self.context.run["commander_receipts"].pop("commander:dispatch:dispatching")
            self.context.run.pop("pending_directive", None)
            self.context.save()
            return False
        if self.services.budgets.task_budget_used(tid) >= self.context.config.data["runtime"]["max_attempts"]:
            raise HarnessError(f"{tid} 已达尝试次数上限")
        self.context.run["current_task"] = tid
        if not saved_directive and not pristine:
            receipt = self.context.run["commander_receipts"].pop("commander:dispatch:dispatching")
            self.context.run["pending_directive"] = {"directive": deepcopy(directive),
                                             "snapshot_digest": receipt["snapshot_digest"]}
        self.context.save()
        self.context.checkpoint()
        prior_reviews = compact_reviews(self.context.run["review_history"].get(tid, []))
        result = self.services.dispatch.dispatch("worker", "implement", tid, WORK,
                 {"goal": self.context.run["goal"], "task": task, "instruction": directive["instruction"],
                  "feedback": compact_feedback(self.context.run.get("feedback"), prior_reviews), "prior_reviews": prior_reviews,
                  "recovery": worker_recovery_context(self.context.run, self.context.directory, tid),
                  "dependencies": {d: self.services.packets.accepted_context()[d] for d in task["depends_on"]},
                  "handoff_targets": self.services.workflow.handoff_targets(tid),
                  "verification_commands": self.context.config.data["verification"]["commands"],
                  "tool_constraints": self.services.packets.tool_constraints()}, task=task)
        if result is None:
            # An invalid output was one spent work attempt; scheduling continues.
            return False
        return self.publish_worker_result(task, result, prior_reviews)

    def replay_worker(self):
        key = self.services.worker_receipts.pending()
        if key is None:
            return None
        receipt = self.context.run["worker_receipts"][key]
        entry = self.services.worker_receipts.entry(receipt)
        task = next(t for t in self.context.run["tasks"] if t["id"] == entry["task_id"])
        result = self.services.dispatch.dispatch("worker", "implement", task["id"], WORK, {}, task=task)
        if result is None:
            return False
        prior_reviews = compact_reviews(self.context.run["review_history"].get(task["id"], []))
        return self.publish_worker_result(task, result, prior_reviews)

    def publish_worker_result(self, task, result, prior_reviews):
        runtime = self.context
        tid = task["id"]
        key = f"worker:implement:{tid}"
        receipt = self.context.run["worker_receipts"][key]
        candidate_digest = receipt.get("post_hook_digest")
        if not candidate_digest or snapshot_digest(self.context.snapshot()) != candidate_digest:
            raise HarnessError("worker candidate changed before handoff publication")
        attempt = self.context.run["worker_receipts"][key]["attempt"]
        paths = git.check_scope(self.context.worktree, task["allowed_paths"], binding=self.context.run["worktree_binding"])
        worker_dispatch = self.services.worker_receipts.entry(self.context.run["worker_receipts"][key])
        worker_artifact = self.context.directory / "dispatches" / worker_dispatch["id"] / "returned-result.json"
        raw_worker = worker_artifact.read_bytes()
        stored_worker = json.loads(raw_worker)
        if (worker_dispatch["role"] != "worker" or worker_dispatch["task_id"] != tid
                or stored_worker.get("dispatch_id") != worker_dispatch["id"]
                or stored_worker.get("task_id") != tid
                or {key: value for key, value in stored_worker.items()
                    if key not in ("task_id", "dispatch_id")} != result):
            raise HarnessError("worker 返回产物与当前任务结果不一致；保留现场")
        worker_source = {"worker_artifact": str(worker_artifact),
                         "worker_artifact_digest": hashlib.sha256(raw_worker).hexdigest()}
        if result["status"] == "blocked" and result.get("decision_requests"):
            self.context.event("decision_requests_discarded", task_id=tid, dispatch_id=worker_dispatch["id"],
                          reason="handoff requests do not apply to blocked worker results",
                          count=len(result["decision_requests"]), **worker_source)
        checkpoints_discarded = None
        try:
            discarded = self.services.progress.record_checkpoints(tid, worker_dispatch["id"], result,
                                    {"artifact": str(worker_artifact), "artifact_digest": worker_source["worker_artifact_digest"]})
            if discarded:
                reason = "; ".join(f"checkpoint[{item['index']}]: {item['reason']}" for item in discarded)
                self.context.event("checkpoints_discarded", task_id=tid, dispatch_id=worker_dispatch["id"],
                           reason=reason, discarded=discarded)
                checkpoints_discarded = {"task_id": tid, "dispatch_id": worker_dispatch["id"],
                                         "reason": reason[:2000], "count": len(discarded)}
        except CheckpointDeclarationError as error:
            self.context.event("checkpoints_discarded", task_id=tid, dispatch_id=worker_dispatch["id"], reason=str(error))
            checkpoints_discarded = {"task_id": tid, "dispatch_id": worker_dispatch["id"],
                                     "reason": str(error)[:2000]}
        try:
            self.services.workflow.validate_handoff_requests(tid, result)
        except HarnessError as error:
            # An invalid output like any other: one spent attempt, no stop. Its
            # valid checkpoints stay registered for the next worker (R8).
            self.services.worker_receipts.reject(key, error, "result_rejected", status="invalid", invalid_output={
                "artifact": str(worker_artifact), "artifact_digest": worker_source["worker_artifact_digest"]})
            return False
        self.context.record_ignored_paths("worker", worker_artifact.parent)
        if snapshot_digest(self.context.snapshot()) != candidate_digest:
            raise HarnessError("worker candidate changed during handoff preparation")
        if result["status"] == "blocked":
            snapshot = self.context.snapshot()
            if snapshot_digest(snapshot) != candidate_digest:
                raise HarnessError("worker candidate changed before blocker publication")
            self.context.run["feedback"] = {"task_id": tid, "worker": result, "attempt": attempt,
                                       "snapshot_digest": snapshot_digest(snapshot), **worker_source}
            if result["blocker_kind"] == "continuation":
                record_continuation(runtime, tid, result,
                    {"dispatch_id": worker_dispatch["id"], "artifact": str(worker_artifact),
                     "artifact_digest": worker_source["worker_artifact_digest"]}, snapshot, save=False)
                self.services.worker_receipts.consume(key, "continuation")
                self.context.save()
                return False
            self.services.budgets.record_attempt_exemption(worker_dispatch, result, worker_source)
            if checkpoints_discarded is not None:
                self.context.run["feedback"]["checkpoints_discarded"] = checkpoints_discarded
            if result["blocker_kind"] == "task":
                self.context.run["pending_replan"] = {"stage": "pending", "trigger": deepcopy(self.context.run["feedback"])}
            self.services.worker_receipts.consume(key, result["blocker_kind"] + "_blocked")
            self.context.save()
            if result["blocker_kind"] == "operator":
                self.services.workflow.synchronize_decision_requests()
                raise self.services.workflow.decision_required(self.services.workflow.task_decision_gate(tid))
            if result["blocker_kind"] != "task":
                raise HarnessError(f"worker 环境受阻：{environment_blocker_excerpt(result)}。"
                                   f"完整 worker 结果：{worker_artifact}（SHA256 {worker_source['worker_artifact_digest']}）。"
                                   "若必要命令被拒绝，可在 permissions.worker_commands 追加精确 argv 后 resume；"
                                   "环境事实有变化可使用 --environment-update FILE 记录补充说明与本地检查；"
                                   "原尝试和派发预算仍保留。")
            return False
        self.context.run["pending_task"] = {"task_id": tid, "attempt": attempt, "stage": "verifying",
            "snapshot_digest": candidate_digest,
            "worker_result": result, **worker_source, "prior_reviews": prior_reviews, "verification_rounds": 0}
        self.services.worker_receipts.consume(key, "pending_task")
        self.context.save()
        return self.continue_task()

    def publish_invalid_blocker(self, task, key, result, judgment):
        """Keep an invalid blocked report binding: feedback, route and consumption in one save.

        A readable environment, operator or task blocker keeps its route with the
        original result in feedback; any other blocked output goes to replanning
        as a task blocker, without a ``worker`` entry. Neither earns an exemption.
        """
        tid = task["id"]
        receipt = self.context.run["worker_receipts"][key]
        entry = self.services.worker_receipts.entry(receipt)
        artifact = Path(receipt["artifact"])
        digest = receipt["artifact_digest"]
        error, route = judgment.error, judgment.route
        feedback = {"task_id": tid, "attempt": receipt["attempt"],
                    "snapshot_digest": snapshot_digest(self.context.snapshot()),
                    "worker_artifact": str(artifact), "worker_artifact_digest": digest, "output_error": str(error)}
        if judgment.readable and route == result.get("blocker_kind"):
            feedback["worker"] = result
        # Inspect Git before any handoff field changes: an interruption inside it
        # must not leave a pending replan beside the unconsumed receipt.
        recovery = self.services.workflow.worker_recovery(tid, entry["id"], artifact.parent, error)
        self.context.run["feedback"] = feedback
        if route == "task":
            self.context.run["pending_replan"] = {"stage": "pending", "trigger": deepcopy(feedback)}
        entry.update(status="invalid_blocked", error=str(error), worker_outcome=route + "_blocked")
        self.context.run["last_runtime_error"] = {"role": "worker", "kind": entry["kind"], "task_id": tid,
                                                  "error": str(error), "artifact": str(artifact.parent)}
        self.context.run.setdefault("worker_recovery", {})[tid] = recovery
        self.services.worker_receipts.consume(key, "invalid_blocker")
        self.context.save()
        self.services.receipts.observe_invalid(entry)
        if route == "operator":
            self.services.workflow.synchronize_decision_requests()
            raise self.services.workflow.decision_required(self.services.workflow.task_decision_gate(tid))
        if route == "environment":
            raise HarnessError(f"worker 环境受阻：{environment_blocker_excerpt(result)}。"
                               f"完整 worker 结果：{artifact}（SHA256 {digest}）。"
                               "若必要命令被拒绝，可在 permissions.worker_commands 追加精确 argv 后 resume；"
                               "环境事实有变化可使用 --environment-update FILE 记录补充说明与本地检查；"
                               "原尝试和派发预算仍保留。")
        return None

    def check_task_scope(self, task):
        runtime = self.context
        try:
            return git.check_scope(self.context.worktree, task["allowed_paths"], binding=self.context.run["worktree_binding"])
        except HarnessError as error:
            raise HarnessError(f"{error}。工作树：{self.context.worktree}；请先核对并清理越界修改，再 resume。"
                               "现场与证据已保留，本次预检不会派发 worker。") from error

    def preserve_invalidated_reviews(self, pending, key):
        """Keep evidence when a changed candidate invalidates its pending gate."""
        runtime = self.context
        history = self.context.run["review_history"].setdefault(pending["task_id"], [])
        for source in [self.context.run["review_receipts"].get(key)]:
            if not source:
                continue
            artifact = Path(source["artifact"])
            if not artifact.exists() and "artifact_digest" not in source:
                continue  # A provider invocation that never returned a result.
            try:
                raw = artifact.read_bytes()
            except OSError as error:
                raise HarnessError("审阅原始结果缺失或无法读取；禁止通过候选变化丢弃证据") from error
            digest = hashlib.sha256(raw).hexdigest()
            if "artifact_digest" not in source:
                source["artifact_digest"] = digest
                self.context.save()
            if digest != source["artifact_digest"]:
                raise HarnessError("审阅原始结果已改变；禁止通过候选变化丢弃证据")
            dispatch_id = source.get("dispatch_id", artifact.parent.name)
            if any(item.get("dispatch_id") == dispatch_id for item in history):
                continue
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                result = None
            record = {"status": "invalidated", "attempt": pending["attempt"],
                      "dispatch_id": dispatch_id, "artifact": str(artifact),
                      "artifact_digest": digest, "snapshot_digest": source["snapshot_digest"],
                      "verification": pending.get("verification"),
                      "diff_artifact": pending.get("diff_artifact"),
                      "reason": "候选已变化；原结果仅作待复核证据，不构成当前候选的批准"}
            record["review_source"] = {"dispatch_id": dispatch_id, "artifact": str(artifact),
                "artifact_digest": digest, "snapshot_digest": source["snapshot_digest"]}
            # A returned review exists only in the reviewing stage, whose saved
            # mapping produced its schema; never validate it against a default.
            if "required_findings" not in pending:
                raise HarnessError("审阅阶段缺少必需问题对照；保留证据")
            record["required_findings"] = deepcopy(pending["required_findings"])
            schema = review_schema(pending["required_findings"])
            if not isinstance(result, dict):
                record["validation_error"] = "review result is not a JSON object"
            else:
                judgment = judge_review(result, envelope(schema, pending["task_id"], dispatch_id))
                if judgment.state == "invalid" and not judgment.readable:
                    # Malformed data remains inspectable, but is not presented
                    # as a structurally valid review of this task.
                    record["validation_error"] = str(judgment.error)
                else:
                    record["review"] = {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
                    if judgment.error is not None:
                        record["validation_error"] = str(judgment.error)
            history.append(record)

    def continue_task(self):
        runtime = self.context
        pending = self.context.run["pending_task"]
        tid, attempt = pending["task_id"], pending["attempt"]
        task = next(t for t in self.context.run["tasks"] if t["id"] == tid)
        self.context.ensure_exited()
        if git.head(self.context.worktree, binding=self.context.run["worktree_binding"]) != self.context.run["expected_head"]:
            raise HarnessError("任务恢复前 HEAD 与检查点不同；保留现场")
        paths = self.check_task_scope(task)
        key = f"reviewer:review:{tid}"
        snapshot = self.context.snapshot()
        if snapshot_digest(snapshot) != pending["snapshot_digest"]:
            self.preserve_invalidated_reviews(pending, key)
            self.context.run.pop("pending_task")
            self.context.run["review_receipts"].pop(key, None)
            self.context.run["feedback"] = {"task_id": tid, "recovery": {
                "error": "任务候选已变化；旧验证及审阅失效，重新执行任务",
                "verification": pending.get("verification")}}
            self.context.save()
            self.context.event("pending_task_invalidated", task_id=tid)
            return False
        if pending["stage"] == "verifying":
            worker_id = Path(pending["worker_artifact"]).parent.name
            full_result = {"task_id": tid, "dispatch_id": worker_id, **pending["worker_result"]}
            required = required_findings(self.context.run, tid, pending["snapshot_digest"])
            delivered = delivered_worker_findings(self.context, tid, worker_id)
            # Only source reads occurred since this snapshot; verification will
            # independently rehash after its hooks and commands have run.
            preflight = candidate_preflight(task, full_result, worktree=self.context.worktree,
                candidate_snapshot=snapshot, current_snapshot=snapshot,
                worker_artifact=pending["worker_artifact"],
                worker_artifact_digest=pending["worker_artifact_digest"], worker_dispatch_id=worker_id)
            preflight["failures"].extend(claim_gaps(full_result, delivered, snapshot))
            preflight["passed"] = not preflight["failures"]
            preflight_path = self.context.directory / "preflight" / tid / str(attempt) / "result.json"
            write_json(preflight_path, preflight)
            if not preflight["passed"]:
                self.context.run["feedback"] = {"task_id": tid, "preflight": preflight,
                                           "artifact": str(preflight_path)}
                self.context.run.pop("pending_task")
                self.context.save()
                self.context.event("candidate_preflight_failed", task_id=tid, artifact=str(preflight_path))
                return False
            pending["required_findings"] = required
            pending["verification_rounds"] += 1
            folder = self.services.verification.verification_directory(tid, attempt)
            if pending["verification_rounds"] > 1:
                folder /= f"retry-{pending['verification_rounds']}"
            pending["verification"] = str(folder / "results.json")
            self.context.save()
            checks = self.services.verification.verify(tid, attempt, artifact_dir=folder, before=snapshot)
            if not checks_passed(checks):
                self.context.run["feedback"] = {"task_id": tid, "verification": compact_checks(checks)}
                self.context.run.pop("pending_task")
                self.context.save()
                self.context.event("verification_failed", task_id=tid)
                return False
            paths = self.check_task_scope(task)
            evidence = folder / "change.diff"
            evidence.write_bytes(git.diff_bytes(self.context.worktree, binding=self.context.run["worktree_binding"]))
            snapshot_artifact = folder / "snapshot.json"
            write_json(snapshot_artifact, snapshot)
            try:
                proposals = validate_proposals(pending["worker_result"].get("proposed_memories", []), self.context.worktree)
            except HarnessError as error:
                proposals = []
                self.context.event("memory_proposals_discarded", task_id=tid, reason=str(error))
            pending.update(stage="reviewing", proposals=proposals,
                           diff_artifact=str(evidence), snapshot_artifact=str(snapshot_artifact))
            self.context.save()
        evidence = Path(pending["diff_artifact"])
        snapshot_artifact = Path(pending["snapshot_artifact"])
        reviewed_digest = pending["snapshot_digest"]
        checks = read_json(Path(pending["verification"]))
        proposals = pending["proposals"]
        prior_reviews = pending["prior_reviews"]
        if "required_findings" not in pending:
            raise HarnessError("审阅阶段缺少必需问题对照；保留证据")
        required = pending["required_findings"]
        delivered = delivered_worker_findings(self.context, tid, Path(pending["worker_artifact"]).parent.name)
        review, diagnostics = self.services.receipts.dispatch_review("review", tid,
                 {"goal": self.context.run["goal"], "task": task, "base_commit": self.context.run["expected_head"], "changed_files": paths,
                  "instruction_file_changes": instruction_file_changes(paths),
                  "diff_artifact": str(evidence), "verification": compact_checks(checks),
                  "ignored_paths": deepcopy(self.context.run.get("ignored_diagnostics")),
                  "prior_reviews": prior_reviews,
                  "required_findings": required,
                  "repair_claims": claims_for_review(required, delivered, pending["worker_result"].get("repair_claims")),
                  "recovery": worker_recovery_context(self.context.run, self.context.directory, tid),
                  "proposed_memories": proposals,
                  "decision_requests": deepcopy(pending["worker_result"].get("decision_requests", [])),
                  "tool_constraints": self.services.packets.tool_constraints()}, task=task, required=required)
        review_artifact = evidence.parent / "review.json"
        record = {"attempt": attempt, "review": review, "diff_artifact": str(evidence),
                  "verification": str(evidence.parent / "results.json"), "artifact": str(review_artifact),
                  "snapshot_digest": reviewed_digest, "snapshot_artifact": str(snapshot_artifact),
                  "required_findings": deepcopy(required), **diagnostics}
        record["review_source"] = bind_review_source(self.context, "review", tid, review, reviewed_digest)
        record["dispatch_id"] = record["review_source"]["dispatch_id"]
        disposition_errors = disposition_gaps(review, required)
        if disposition_errors:
            diagnostics = {**diagnostics, "disposition_errors": disposition_errors}
            record.update(diagnostics)
        if pending.get("worker_artifact"):
            record["worker_source"] = {"dispatch_id": Path(pending["worker_artifact"]).parent.name,
                                       "artifact": pending["worker_artifact"],
                                       "artifact_digest": pending["worker_artifact_digest"]}
        write_json(review_artifact, record)
        approved_keys = [] if diagnostics else self.approved_memory_keys(tid, review, proposals)
        if not diagnostics and review_approved(review):
            try:
                self.context.memory.check_proposals(proposals, approved_keys)
            except HarnessError as error:
                approved_keys = []
                self.context.event("memory_proposals_discarded", task_id=tid, reason=str(error))
        if not self._finish_review(pending, record, paths, approved_keys, diagnostics):
            self.context.event("review_rejected", task_id=tid)
            return False
        return self.accept_pending()

    def approved_memory_keys(self, task_id, review, proposals):
        """Keep only approvals of this candidate's proposals; discard the rest on their own (R8)."""
        raw = review.get("approved_memory_keys")
        known = {proposal["key"] for proposal in proposals}
        kept = (list(dict.fromkeys(key for key in raw if isinstance(key, str) and key in known))
                if isinstance(raw, list) else [])
        discarded = (([key for key in raw if not isinstance(key, str) or key not in known]
                      + [key for index, key in enumerate(raw) if isinstance(key, str) and key in raw[:index]])
                     if isinstance(raw, list) else [raw])
        if discarded:
            # Observation only; an interruption before the review save may repeat it.
            self.context.event("memory_proposals_discarded", task_id=task_id,
                               reason="approved keys that are not proposals of this candidate were discarded",
                               discarded_keys=[key for key in discarded if isinstance(key, str)])
        return kept

    def _require_handoff(self, field, pending, *destinations):
        """Validate the live source before replacing any durable task phase."""
        run = self.context.run
        if (run.get(field) is not pending or pending["task_id"] in run["accepted"]
                or any(destination in run for destination in destinations)):
            raise HarnessError("任务阶段转换冲突；保留原阶段和证据")

    def _finish_review(self, pending, record, paths, approved_keys, diagnostics):
        """Save review history and its successor phase together, before acceptance."""
        runtime = self.context
        self._require_handoff("pending_task", pending, "pending_acceptance", "pending_post_accept")
        if pending["stage"] != "reviewing":
            raise HarnessError("任务必须完成验证后才能登记审阅结果")
        tid, review = pending["task_id"], record["review"]
        approved = not diagnostics and review_approved(review)
        if approved:
            successor = {"task_id": tid, "attempt": pending["attempt"], "files": paths,
                "review": review, "review_artifact": record["artifact"],
                "review_source": deepcopy(record["review_source"]),
                "snapshot_digest": record["snapshot_digest"], "snapshot_artifact": record["snapshot_artifact"],
                "verification": record["verification"], "proposals": pending["proposals"],
                "approved_keys": approved_keys}
            if pending.get("worker_artifact"):
                successor.update(
                    worker_source={"artifact": pending["worker_artifact"],
                                   "artifact_digest": pending["worker_artifact_digest"],
                                   "dispatch_id": Path(pending["worker_artifact"]).parent.name},
                    decision_requests=deepcopy(pending["worker_result"].get("decision_requests", [])))
        else:
            successor = {"task_id": tid, "review": review, "artifact": record["artifact"], **diagnostics}
        key = f"reviewer:review:{tid}"
        self.context.run["review_history"].setdefault(tid, []).append(record)
        self.context.run["review_receipts"].pop(key, None)
        self.context.run.pop("pending_task")
        self.context.run["pending_acceptance" if approved else "feedback"] = successor
        self.context.save()
        return approved

    def _record_accepted(self, pending, accepted, completion):
        """Publish the commit and its remaining work at the same durable barrier."""
        runtime = self.context
        self._require_handoff("pending_acceptance", pending, "pending_task", "pending_post_accept")
        tid, attempt = pending["task_id"], pending["attempt"]
        successor = {"payload": {"task_id": tid, **accepted},
                     "logs": str(self.context.directory / "hooks" / f"{tid}-{attempt}"),
                     "completion": completion}
        self.context.run["expected_head"] = accepted["commit"]
        self.context.run["accepted"][tid] = accepted
        self.context.run["feedback"] = None
        self.context.run["current_task"] = None
        self.context.run.get("worker_recovery", {}).pop(tid, None)
        self.context.run.pop("pending_acceptance")
        self.context.run["pending_post_accept"] = successor
        self.context.save()

    def task_memory_record(self, task, pending, commit, paths):
        """The accepted task's memory record fields; the preflight uses a same-length placeholder commit."""
        run_id, tid = self.context.run["id"], task["id"]
        detail = {"commit": commit, "summary": pending["review"]["summary"], "summary_source": "reviewer",
                  "changed_file_count": len(paths), "finding_count": len(pending["review"]["findings"]),
                  "verification": pending["verification"], "review_artifact": pending["review_artifact"],
                  "snapshot_artifact": pending["snapshot_artifact"]}
        record = {"key": f"task:{run_id}:{tid}", "abstract": task["title"][:500],
                  "overview": pending["review"]["summary"], "detail": dump(detail),
                  "source": f"git:{commit};run:{run_id}",
                  "ttl_days": self.context.config.data["memory"]["task_ttl_days"] or None}
        provenance = {"commit": commit, "run_id": run_id, "task_id": tid, "scope": "run", "reference_only": True}
        return record, provenance

    def accept_pending(self):
        runtime = self.context
        pending = self.context.run["pending_acceptance"]
        self._require_handoff("pending_acceptance", pending, "pending_task", "pending_post_accept")
        tid, attempt = pending["task_id"], pending["attempt"]
        task = next(task for task in self.context.run["tasks"] if task["id"] == tid)
        self.context.ensure_exited()
        if snapshot_digest(self.context.snapshot()) != pending["snapshot_digest"]:
            feedback = {"task_id": tid, "acceptance": {
                "error": "待接受文件已变化，旧审阅失效；需要重新验证和独立审阅",
                "review_artifact": pending["review_artifact"]}}
            invalidate_acceptance_review(self.context.run, feedback, attempt=attempt,
                                         candidate_digest=pending["snapshot_digest"])
            self.context.run.pop("pending_acceptance")
            self.context.run["feedback"] = feedback
            self.context.save()
            self.context.event("pending_acceptance_invalidated", task_id=tid)
            return False
        # A predictable record error must stop before the commit, not after it.
        try:
            Memory.validate_record(**self.task_memory_record(task, pending, "0" * len(self.context.run["expected_head"]),
                                                             pending["files"])[0])
        except HarnessError as error:
            raise HarnessError(f"任务记忆记录不合格；未提交：{error}") from error
        pending["hook_attempts"] = pending.get("hook_attempts", 0) + 1
        logs = self.context.directory / "hooks" / f"{tid}-{attempt}" / f"pre_accept-{pending['hook_attempts']}"
        self.context.save()
        try:
            self.context.emit_hook("pre_accept", {"task_id": tid, "review": pending["review"]}, logs)
        except Exception as error:
            detail = {"error": str(error), "logs": str(logs), "review_artifact": pending["review_artifact"]}
            if isinstance(error, HookError):
                detail.update(exit_code=error.code, log=str(error.log))
            self.context.run["feedback"] = {"task_id": tid, "pre_accept": detail}
            self.context.save()
            raise
        if snapshot_digest(self.context.snapshot()) != pending["snapshot_digest"]:
            raise HarnessError("审阅后文件发生变化；禁止接受旧审阅结果")
        validate_dispatch_indexes(self.context.directory, self.context.run["dispatches"])
        validate_input_dispatch_indexes(self.context.directory, self.context.run["dispatches"])
        paths = git.check_scope(self.context.worktree, task["allowed_paths"], binding=self.context.run["worktree_binding"])
        validate_review_source(self.context.run, tid, pending, candidate_digest=pending["snapshot_digest"])
        commit = git.accept(self.context.worktree, paths, self.context.run["id"], tid, binding=self.context.run["worktree_binding"])
        accepted = {"commit": commit, "summary": pending["review"]["summary"], "summary_source": "reviewer",
                    "files": paths, "verification": pending["verification"], "review": pending["review"],
                    "review_artifact": pending["review_artifact"] ,
                    "review_source": deepcopy(pending["review_source"])}
        if pending.get("worker_source"):
            accepted.update(worker_source=deepcopy(pending["worker_source"]),
                            decision_requests=deepcopy(pending.get("decision_requests", [])))
        task_memory, provenance = self.task_memory_record(task, pending, commit, paths)
        completion = {
            "id": f"acceptance:{self.context.run['id']}:{tid}",
            "task_memory": {**task_memory, "kind": "accepted_task", "provenance": provenance},
            "proposals": pending["proposals"], "approved_keys": pending["approved_keys"],
            "knowledge_ttl_days": self.context.config.data["memory"]["knowledge_ttl_days"] or None,
            "knowledge": {"status": "pending"},
        }
        self._record_accepted(pending, accepted, completion)
        self.finish_post_accept()
        self.context.checkpoint()
        return True
