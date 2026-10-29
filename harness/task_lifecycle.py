"""Own task verification/review stages and the atomic acceptance/post-accept handoffs."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from . import git
from .budgets import work_attempts
from .context import compact_checks, compact_feedback, compact_reviews, instruction_file_changes
from .contracts import (
    envelope, REVIEW, review_approved, task_directive, validate_review, validate_structure, validate_work,
    WORK,
)
from .memory import validate_proposals
from .path_evidence import validate_dispatch_indexes
from .progress import CheckpointDeclarationError
from .recovery import invalidate_acceptance_review, snapshot_digest, worker_recovery_context
from .services import RuntimeService
from .shared import HookError
from .util import dump, HarnessError, read_json, write_json


class TaskLifecycle(RuntimeService):
    def finish_post_accept(self):
        runtime = self.runtime
        pending = runtime.run.get("pending_post_accept")
        if not pending:
            return False
        completion = pending["completion"]
        payload = pending["payload"]
        tid, commit = payload["task_id"], payload["commit"]
        # These IDs are committed in the same SQLite transaction as the records.
        # A crash after a successful write but before save must not create a version.
        if not completion.get("task_memory_id"):
            completion["task_memory_id"] = runtime.memory.remember(
                **completion["task_memory"], operation_id=completion["id"] + ":task")
            runtime.save()
        if completion["knowledge"]["status"] == "pending":
            try:
                identifiers = runtime.memory.remember_proposals(
                    completion["proposals"], completion["approved_keys"], commit, runtime.run["id"], tid,
                    completion["knowledge_ttl_days"], operation_id=completion["id"] + ":knowledge")
            except HarnessError as error:
                completion["knowledge"] = {"status": "discarded", "reason": str(error)}
            else:
                completion["knowledge"] = {"status": "promoted", "memory_ids": identifiers}
            runtime.save()
        if completion["knowledge"]["status"] == "discarded":
            runtime.acceptance_event_once("memory_proposals_discarded", completion["id"] + ":discarded",
                                       task_id=tid, reason=completion["knowledge"]["reason"])
        runtime.acceptance_event_once("task_accepted", completion["id"], task_id=tid, commit=commit)
        # A crash after hook success but before saving can retry the hook.
        # Memory completion and this gate both precede any subsequent dispatch.
        pending["hook_attempts"] = pending.get("hook_attempts", 0) + 1
        runtime.save()
        runtime.emit_hook("post_accept", payload, Path(pending["logs"]) / f"post_accept-{pending['hook_attempts']}")
        runtime.run.pop("pending_post_accept")
        runtime.save()
        runtime.synchronize_decision_requests()
        return True

    def acceptance_event_once(self, event, event_id, **fields):
        """Recover the event-append/state-save boundary using the stable acceptance ID."""
        runtime = self.runtime
        path = runtime.directory / "events.jsonl"
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
        runtime.event(event, event_id=event_id, **fields)

    def task_attempt(self, ready):
        runtime = self.runtime
        current = runtime.run.get("current_task")
        if current:
            task = next(t for t in runtime.run["tasks"] if t["id"] == current)
            runtime.check_task_scope(task)
        if current and runtime.task_budget_used(current) >= runtime.config.data["runtime"]["max_attempts"]:
            raise HarnessError(f"{current} 已达尝试次数上限")
        if runtime.run["total_attempts"] >= runtime.config.data["runtime"]["max_total_attempts"]:
            raise HarnessError("已达 run 总尝试次数上限")
        # A task's prerequisites must not prevent selecting other ready work.
        # The selected worker is checked again before its invocation is counted.
        if current:
            runtime.check_environment(current)
        saved_directive = runtime.run.get("pending_directive")
        pristine = len(ready) == 1 and not runtime.run.get("feedback") and not runtime.run["attempts"].get(ready[0]["id"])
        if saved_directive:
            if snapshot_digest(runtime.snapshot()) != saved_directive["snapshot_digest"]:
                raise HarnessError("待派发指令的代码快照已变化；请核对并恢复原快照")
            directive = saved_directive["directive"]
        elif pristine:
            directive = {"selected_task": ready[0]["id"],
                         "instruction": "Implement this task within its complete stated scope and acceptance. Inspect dependency evidence and follow the supplied tool and verification constraints."}
        else:
            schema = task_directive(t["id"] for t in ready)
            directive = runtime.dispatch("commander", "dispatch", "dispatching", schema,
                        {"goal": runtime.run["goal"], "ready": ready, "accepted": runtime.accepted_context(),
                         "feedback": compact_feedback(runtime.run.get("feedback")), "attempts": runtime.run["attempts"],
                         "work_attempts": work_attempts(runtime.run),
                         "recovery": worker_recovery_context(runtime.run, runtime.directory, current) if current in {t["id"] for t in ready} else None,
                         "tool_constraints": runtime.tool_constraints(),
                         "instruction": "Select one ready task and return action=implement for a feasible implementation or repair. Return action=replan when feedback exposes missing prerequisites or acceptance that cannot be met within current scope and decisions; explain the unmet requirement in instruction. Replanning can stop without another worker. Retain the original acceptance: completing a report of unresolved failures is not completing the task."}, task=ready[0])
        task = next(t for t in ready if t["id"] == directive["selected_task"])
        tid = task["id"]
        if directive.get("action", "implement") == "replan":
            receipt = runtime.run["commander_receipts"]["commander:dispatch:dispatching"]
            trigger = {"task_id": tid, "feedback": deepcopy(runtime.run.get("feedback")),
                       "coordination": {**{key: receipt[key] for key in
                           ("artifact", "artifact_digest", "dispatch_id", "snapshot_digest")},
                           "instruction": directive["instruction"]}}
            # Consume the selection and publish its successor in one save. Resume
            # must replan even if the next call's budget or preparation fails.
            runtime.run["current_task"] = tid
            runtime.run["pending_replan"] = {"stage": "pending", "trigger": trigger}
            runtime.run["commander_receipts"].pop("commander:dispatch:dispatching")
            runtime.run.pop("pending_directive", None)
            runtime.run["pending_format_corrections"].pop(f"worker:implement:{tid}", None)
            runtime.save()
            runtime.replan(trigger)
            return False
        if runtime.task_budget_used(tid) >= runtime.config.data["runtime"]["max_attempts"]:
            raise HarnessError(f"{tid} 已达尝试次数上限")
        runtime.run["current_task"] = tid
        if not saved_directive and not pristine:
            receipt = runtime.run["commander_receipts"].pop("commander:dispatch:dispatching")
            runtime.run["pending_directive"] = {"directive": deepcopy(directive),
                                             "snapshot_digest": receipt["snapshot_digest"]}
        runtime.save()
        runtime.checkpoint()
        prior_reviews = compact_reviews(runtime.run["review_history"].get(tid, []))
        result = runtime.dispatch("worker", "implement", tid, WORK,
                 {"goal": runtime.run["goal"], "task": task, "instruction": directive["instruction"],
                  "feedback": compact_feedback(runtime.run.get("feedback"), prior_reviews), "prior_reviews": prior_reviews,
                  "recovery": worker_recovery_context(runtime.run, runtime.directory, tid),
                  "dependencies": {d: runtime.accepted_context()[d] for d in task["depends_on"]},
                  "handoff_targets": runtime.handoff_targets(tid),
                  "verification_commands": runtime.config.data["verification"]["commands"],
                  "tool_constraints": runtime.tool_constraints()}, task=task)
        validate_work(result)
        attempt = runtime.run["attempts"][tid]
        paths = git.check_scope(runtime.worktree, task["allowed_paths"], binding=runtime.run["worktree_binding"])
        worker_dispatch = runtime.run["dispatches"][-1]
        worker_artifact = runtime.directory / "dispatches" / worker_dispatch["id"] / "returned-result.json"
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
            runtime.event("decision_requests_discarded", task_id=tid, dispatch_id=worker_dispatch["id"],
                          reason="handoff requests do not apply to blocked worker results",
                          count=len(result["decision_requests"]), **worker_source)
        checkpoints_discarded = None
        try:
            discarded = runtime.record_checkpoints(tid, worker_dispatch["id"], result,
                                    {"artifact": str(worker_artifact), "artifact_digest": worker_source["worker_artifact_digest"]})
            if discarded:
                reason = "; ".join(f"checkpoint[{item['index']}]: {item['reason']}" for item in discarded)
                runtime.event("checkpoints_discarded", task_id=tid, dispatch_id=worker_dispatch["id"],
                           reason=reason, discarded=discarded)
                checkpoints_discarded = {"task_id": tid, "dispatch_id": worker_dispatch["id"],
                                         "reason": reason[:2000], "count": len(discarded)}
        except CheckpointDeclarationError as error:
            runtime.event("checkpoints_discarded", task_id=tid, dispatch_id=worker_dispatch["id"], reason=str(error))
            checkpoints_discarded = {"task_id": tid, "dispatch_id": worker_dispatch["id"],
                                     "reason": str(error)[:2000]}
        try:
            runtime.validate_handoff_requests(tid, result)
        except HarnessError as error:
            worker_dispatch.update(status="failed", error=str(error))
            runtime.run["last_runtime_error"] = {"role": "worker", "kind": "implement", "task_id": tid,
                                             "error": str(error), "artifact": str(worker_artifact.parent)}
            runtime.record_worker_recovery(tid, worker_dispatch["id"], worker_artifact.parent, error)
            runtime.save()
            runtime.event("worker_result_rejected", task_id=tid, dispatch_id=worker_dispatch["id"], reason=str(error))
            raise
        runtime.record_ignored_paths("worker", worker_artifact.parent)
        if result["status"] == "blocked":
            runtime.run["feedback"] = {"task_id": tid, "worker": result, "attempt": attempt, **worker_source}
            runtime.record_attempt_exemption(worker_dispatch, result, worker_source)
            if checkpoints_discarded is not None:
                runtime.run["feedback"]["checkpoints_discarded"] = checkpoints_discarded
            runtime.save()
            if result["blocker_kind"] == "operator":
                runtime.synchronize_decision_requests()
                raise runtime.decision_required(runtime.task_decision_gate(tid))
            if result["blocker_kind"] != "task":
                raise HarnessError(f"worker 环境受阻：{result['blockers']} {result['summary']}。"
                                   "若必要命令被拒绝，可在 permissions.worker_commands 追加精确 argv 后 resume；"
                                   "环境事实有变化可使用 --environment-update FILE 记录补充说明与本地检查；"
                                   "原尝试和派发预算仍保留。")
            runtime.replan(deepcopy(runtime.run["feedback"]))
            return False
        runtime.run["pending_task"] = {"task_id": tid, "attempt": attempt, "stage": "verifying",
            "snapshot_digest": snapshot_digest(runtime.snapshot()),
            "worker_result": result, **worker_source, "prior_reviews": prior_reviews, "verification_rounds": 0}
        runtime.save()
        return runtime.continue_task()

    def check_task_scope(self, task):
        runtime = self.runtime
        try:
            return git.check_scope(runtime.worktree, task["allowed_paths"], binding=runtime.run["worktree_binding"])
        except HarnessError as error:
            raise HarnessError(f"{error}。工作树：{runtime.worktree}；请先核对并清理越界修改，再 resume。"
                               "现场与证据已保留，本次预检不会派发 worker。") from error

    def preserve_invalidated_reviews(self, pending, key):
        """Keep evidence when a changed candidate invalidates its pending gate."""
        runtime = self.runtime
        history = runtime.run["review_history"].setdefault(pending["task_id"], [])
        sources = [runtime.run["review_corrections"].get(key), runtime.run["review_receipts"].get(key)]
        repair = runtime.active_invalid_repair(key)
        if repair:
            sources.append(repair["source"])
        for source in sources:
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
                runtime.save()
            if digest != source["artifact_digest"]:
                raise HarnessError("审阅原始结果已改变；禁止通过候选变化丢弃证据")
            dispatch_id = source.get("dispatch_id", artifact.parent.name)
            if any(item.get("dispatch_id") == dispatch_id for item in history):
                continue
            result = json.loads(raw)
            record = {"status": "invalidated", "attempt": pending["attempt"],
                      "dispatch_id": dispatch_id, "artifact": str(artifact),
                      "artifact_digest": digest, "snapshot_digest": source["snapshot_digest"],
                      "verification": pending.get("verification"),
                      "diff_artifact": pending.get("diff_artifact"),
                      "reason": "候选已变化；原结果仅作待复核证据，不构成当前候选的批准"}
            try:
                validate_structure(result, envelope(REVIEW, pending["task_id"], dispatch_id))
            except HarnessError as error:
                # Unattributable or malformed data remains inspectable, but is not
                # presented as a structurally valid review of this task.
                record["validation_error"] = str(error)
            else:
                record["review"] = {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
                try:
                    validate_review(record["review"], [p["key"] for p in pending.get("proposals", [])])
                except HarnessError as error:
                    record["validation_error"] = str(error)
            history.append(record)

    def continue_task(self):
        runtime = self.runtime
        pending = runtime.run["pending_task"]
        tid, attempt = pending["task_id"], pending["attempt"]
        task = next(t for t in runtime.run["tasks"] if t["id"] == tid)
        runtime.ensure_exited()
        if git.head(runtime.worktree, binding=runtime.run["worktree_binding"]) != runtime.run["expected_head"]:
            raise HarnessError("任务恢复前 HEAD 与检查点不同；保留现场")
        paths = runtime.check_task_scope(task)
        key = f"reviewer:review:{tid}"
        snapshot = runtime.snapshot()
        if snapshot_digest(snapshot) != pending["snapshot_digest"]:
            runtime.preserve_invalidated_reviews(pending, key)
            if runtime.active_invalid_repair(key):
                runtime.run["invalid_repairs"][key]["status"] = "invalidated"
            runtime.run.pop("pending_task")
            runtime.run["review_receipts"].pop(key, None)
            runtime.run["review_corrections"].pop(key, None)
            runtime.run["pending_format_corrections"].pop(key, None)
            runtime.run["pending_format_corrections"].pop(f"worker:implement:{tid}", None)
            runtime.run["feedback"] = {"task_id": tid, "recovery": {
                "error": "任务候选已变化；旧验证及审阅失效，重新执行任务",
                "verification": pending.get("verification")}}
            runtime.save()
            runtime.event("pending_task_invalidated", task_id=tid)
            return False
        if pending["stage"] == "verifying":
            pending["verification_rounds"] += 1
            folder = runtime.verification_directory(tid, attempt)
            if pending["verification_rounds"] > 1:
                folder /= f"retry-{pending['verification_rounds']}"
            pending["verification"] = str(folder / "results.json")
            runtime.save()
            checks = runtime.verify(tid, attempt, artifact_dir=folder, before=snapshot)
            if any(c["exit_code"] for c in checks):
                runtime.run["feedback"] = {"task_id": tid, "verification": compact_checks(checks)}
                runtime.run.pop("pending_task")
                runtime.save()
                runtime.event("verification_failed", task_id=tid)
                return False
            paths = runtime.check_task_scope(task)
            evidence = folder / "change.diff"
            evidence.write_bytes(git.diff_bytes(runtime.worktree, binding=runtime.run["worktree_binding"]))
            snapshot_artifact = folder / "snapshot.json"
            write_json(snapshot_artifact, snapshot)
            try:
                proposals = validate_proposals(pending["worker_result"]["proposed_memories"], runtime.worktree)
            except HarnessError as error:
                proposals = []
                runtime.event("memory_proposals_discarded", task_id=tid, reason=str(error))
            pending.update(stage="reviewing", proposals=proposals,
                           diff_artifact=str(evidence), snapshot_artifact=str(snapshot_artifact))
            runtime.save()
        evidence = Path(pending["diff_artifact"])
        snapshot_artifact = Path(pending["snapshot_artifact"])
        reviewed_digest = pending["snapshot_digest"]
        checks = read_json(Path(pending["verification"]))
        proposals = pending["proposals"]
        prior_reviews = pending["prior_reviews"]
        review, diagnostics = runtime.dispatch_review("review", tid,
                 {"goal": runtime.run["goal"], "task": task, "base_commit": runtime.run["expected_head"], "changed_files": paths,
                  "instruction_file_changes": instruction_file_changes(paths),
                  "diff_artifact": str(evidence), "verification": compact_checks(checks),
                  "ignored_paths": deepcopy(runtime.run.get("ignored_diagnostics")),
                  "prior_reviews": prior_reviews,
                  "recovery": worker_recovery_context(runtime.run, runtime.directory, tid),
                  "proposed_memories": proposals,
                  "decision_requests": deepcopy(pending["worker_result"].get("decision_requests", [])),
                  "tool_constraints": runtime.tool_constraints(),
                  "instruction": "Independently inspect the actual diff, relevant code and acceptance. Prior findings are evidence to recheck, not a complete checklist. Separate blocking defects from advisory preferences. Approve only with no unresolved blocking findings; approve memory keys only when their claims and cited sources are independently supported. Your summary becomes the accepted task record: describe only independently verified outcomes and limits. An empty diff can pass only when current evidence already satisfies the task. Do not edit files."}, task=task)
        review_artifact = evidence.parent / "review.json"
        record = {"attempt": attempt, "review": review, "diff_artifact": str(evidence),
                  "verification": str(evidence.parent / "results.json"), "artifact": str(review_artifact),
                  "snapshot_digest": reviewed_digest, "snapshot_artifact": str(snapshot_artifact), **diagnostics}
        write_json(review_artifact, record)
        approved_keys = [] if diagnostics else review["approved_memory_keys"]
        if not diagnostics and review_approved(review):
            try:
                runtime.memory.check_proposals(proposals, approved_keys)
            except HarnessError as error:
                approved_keys = []
                runtime.event("memory_proposals_discarded", task_id=tid, reason=str(error))
        if not self._finish_review(pending, record, paths, approved_keys, diagnostics):
            runtime.event("review_rejected", task_id=tid)
            return False
        return runtime.accept_pending()

    def _require_handoff(self, field, pending, *destinations):
        """Validate the live source before replacing any durable task phase."""
        run = self.runtime.run
        if (run.get(field) is not pending or pending["task_id"] in run["accepted"]
                or any(destination in run for destination in destinations)):
            raise HarnessError("任务阶段转换冲突；保留原阶段和证据")

    def _finish_review(self, pending, record, paths, approved_keys, diagnostics):
        """Save review history and its successor phase together, before acceptance."""
        runtime = self.runtime
        self._require_handoff("pending_task", pending, "pending_acceptance", "pending_post_accept")
        if pending["stage"] != "reviewing":
            raise HarnessError("任务必须完成验证后才能登记审阅结果")
        tid, review = pending["task_id"], record["review"]
        approved = not diagnostics and review_approved(review)
        if approved:
            successor = {"task_id": tid, "attempt": pending["attempt"], "files": paths,
                "review": review, "review_artifact": record["artifact"],
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
        runtime.run["review_history"].setdefault(tid, []).append(record)
        runtime.run["review_corrections"].pop(key, None)
        runtime.run["review_receipts"].pop(key, None)
        runtime.run.pop("pending_task")
        runtime.run["pending_acceptance" if approved else "feedback"] = successor
        runtime.save()
        return approved

    def _record_accepted(self, pending, accepted, completion):
        """Publish the commit and its remaining work at the same durable barrier."""
        runtime = self.runtime
        self._require_handoff("pending_acceptance", pending, "pending_task", "pending_post_accept")
        tid, attempt = pending["task_id"], pending["attempt"]
        successor = {"payload": {"task_id": tid, **accepted},
                     "logs": str(runtime.directory / "hooks" / f"{tid}-{attempt}"),
                     "completion": completion}
        runtime.run["expected_head"] = accepted["commit"]
        runtime.run["accepted"][tid] = accepted
        runtime.run["feedback"] = None
        runtime.run["current_task"] = None
        runtime.run.get("worker_recovery", {}).pop(tid, None)
        runtime.run.pop("pending_acceptance")
        runtime.run["pending_post_accept"] = successor
        runtime.save()

    def accept_pending(self):
        runtime = self.runtime
        pending = runtime.run["pending_acceptance"]
        self._require_handoff("pending_acceptance", pending, "pending_task", "pending_post_accept")
        tid, attempt = pending["task_id"], pending["attempt"]
        task = next(task for task in runtime.run["tasks"] if task["id"] == tid)
        runtime.ensure_exited()
        if snapshot_digest(runtime.snapshot()) != pending["snapshot_digest"]:
            feedback = {"task_id": tid, "acceptance": {
                "error": "待接受文件已变化，旧审阅失效；需要重新验证和独立审阅",
                "review_artifact": pending["review_artifact"]}}
            invalidate_acceptance_review(runtime.run, feedback, attempt=attempt,
                                         candidate_digest=pending["snapshot_digest"])
            runtime.run.pop("pending_acceptance")
            runtime.run["feedback"] = feedback
            runtime.save()
            runtime.event("pending_acceptance_invalidated", task_id=tid)
            return False
        pending["hook_attempts"] = pending.get("hook_attempts", 0) + 1
        logs = runtime.directory / "hooks" / f"{tid}-{attempt}" / f"pre_accept-{pending['hook_attempts']}"
        runtime.save()
        try:
            runtime.emit_hook("pre_accept", {"task_id": tid, "review": pending["review"]}, logs)
        except Exception as error:
            detail = {"error": str(error), "logs": str(logs), "review_artifact": pending["review_artifact"]}
            if isinstance(error, HookError):
                detail.update(exit_code=error.code, log=str(error.log))
            runtime.run["feedback"] = {"task_id": tid, "pre_accept": detail}
            runtime.save()
            raise
        if snapshot_digest(runtime.snapshot()) != pending["snapshot_digest"]:
            raise HarnessError("审阅后文件发生变化；禁止接受旧审阅结果")
        validate_dispatch_indexes(runtime.directory, runtime.run["dispatches"])
        paths = git.check_scope(runtime.worktree, task["allowed_paths"], binding=runtime.run["worktree_binding"])
        commit = git.accept(runtime.worktree, paths, runtime.run["id"], tid, binding=runtime.run["worktree_binding"])
        accepted = {"commit": commit, "summary": pending["review"]["summary"], "summary_source": "reviewer",
                    "files": paths, "verification": pending["verification"], "review": pending["review"],
                    "review_artifact": pending["review_artifact"]}
        if pending.get("worker_source"):
            accepted.update(worker_source=deepcopy(pending["worker_source"]),
                            decision_requests=deepcopy(pending.get("decision_requests", [])))
        detail = {"commit": commit, "summary": accepted["summary"], "summary_source": "reviewer",
                  "changed_file_count": len(paths), "finding_count": len(pending["review"]["findings"]),
                  "verification": accepted["verification"], "review_artifact": accepted["review_artifact"],
                  "snapshot_artifact": pending["snapshot_artifact"]}
        completion = {
            "id": f"acceptance:{runtime.run['id']}:{tid}",
            "task_memory": {"key": f"task:{runtime.run['id']}:{tid}", "abstract": task["title"][:500],
                            "overview": accepted["summary"], "detail": dump(detail),
                            "source": f"git:{commit};run:{runtime.run['id']}", "kind": "accepted_task",
                            "provenance": {"commit": commit, "run_id": runtime.run["id"],
                                           "task_id": tid, "scope": "run", "reference_only": True},
                            "ttl_days": runtime.config.data["memory"]["task_ttl_days"] or None},
            "proposals": pending["proposals"], "approved_keys": pending["approved_keys"],
            "knowledge_ttl_days": runtime.config.data["memory"]["knowledge_ttl_days"] or None,
            "knowledge": {"status": "pending"},
        }
        self._record_accepted(pending, accepted, completion)
        runtime.finish_post_accept()
        runtime.checkpoint()
        return True
