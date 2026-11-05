"""Own final attempt history and the final verification/review acceptance decision."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from . import git
from .context import compact_checks
from .contracts import review_approved
from .recovery import snapshot_digest
from .review_progress import bind_review_source, validate_review_source, required_findings, disposition_gaps
from .services import RuntimeService
from .verification import checks_failed
from .util import HarnessError, read_json, write_json


class FinalAcceptance(RuntimeService):
    def final_review(self, *, reverify=False):
        # Consume the resume flag on first entry, including new snapshots and
        # unfinished attempts that do not need an explicit retry.
        self.context.reverify_requested = False
        git.check_worktree_state(self.context.worktree)
        if not git.clean(self.context.worktree, binding=self.context.run["worktree_binding"]):
            raise HarnessError("整体审阅要求干净的已接受提交；未接受的修改不能用于完成 run")
        snapshot = self.context.snapshot()
        if snapshot["head"] != self.context.run["expected_head"]:
            raise HarnessError("整体审阅前 HEAD 与检查点不同；保留现场")
        digest = snapshot_digest(snapshot)
        history = self.context.run["final_history"]
        same_snapshot = [item for item in history
                         if item["snapshot_digest"] == digest and item["head"] == snapshot["head"]]
        rejected = next((item for item in reversed(same_snapshot)
                         if item["status"] in ("rejected", "invalid_rejected")), None)
        if rejected:
            # A corrective task can validly accept an empty diff. Its acceptance
            # does not erase the earlier rejection; ask for another bounded plan
            # once per newly accepted correction, without calling the reviewer.
            if self.replan_unchanged_corrections(rejected):
                return False
            raise HarnessError("整体审阅已拒绝同一代码快照；必须先通过修正任务，不能恢复后重复抽取审阅")
        for item in same_snapshot:
            if item["status"] == "approved":
                validate_review_source(self.context.run, "final", item, kind="final_review", candidate_digest=digest)
                required = required_findings(self.context.run, "final", digest, whole_run=True,
                                             records=history[:history.index(item)])
                if disposition_gaps(item["review"], required):
                    raise HarnessError("整体批准缺少历史阻断问题的独立解决处置；禁止完成 run")
        if any(item["status"] == "approved" for item in same_snapshot):
            # Approval is persisted only after all validation and snapshot gates.
            return True
        record = history[-1] if history and history[-1] in same_snapshot else None
        if not record or record["status"] != "running":
            if record and record["status"] == "verification_failed" and record["failure_kind"] != "timeout":
                if not reverify:
                    if self.replan_unchanged_corrections(record):
                        return False
                    raise HarnessError("同一代码快照的整体验证曾失败；修复环境后使用 resume --reverify 显式重验")
            if self.context.run["final_attempts"] >= self.context.config.data["runtime"]["max_final_attempts"]:
                raise HarnessError("整体审阅已达尝试次数上限")
            if record and record["status"] == "verification_failed" and record["failure_kind"] != "timeout":
                self.context.event("final_reverification_requested", previous_attempt=record["attempt"])
            self.context.run["final_attempts"] += 1
            attempt = self.context.run["final_attempts"]
            folder = self.services.verification.verification_directory("final", attempt, whole_run=True)
            snapshot_artifact = folder / "snapshot.json"
            record = {"attempt": attempt, "head": self.context.run["expected_head"], "snapshot_digest": digest,
                      "accepted_tasks": sorted(self.context.run["accepted"]),
                      "snapshot_artifact": str(snapshot_artifact), "phase": "verifying",
                      "verification_rounds": [], "status": "running", "artifact": str(folder / "final.json")}
            history.append(record)
            self.context.save()
            self.context.checkpoint()
        attempt = record["attempt"]
        folder = Path(record["artifact"]).parent
        snapshot_artifact = Path(record["snapshot_artifact"])
        if snapshot_artifact.exists() or snapshot_artifact.is_symlink():
            try:
                saved_snapshot = read_json(snapshot_artifact)
            except (OSError, ValueError) as error:
                raise HarnessError("整体快照证据无法读取；保留原证据，禁止覆盖") from error
            if saved_snapshot != snapshot:
                raise HarnessError("整体快照证据与待恢复记录不符；保留原证据，禁止覆盖")
        else:
            # The attempt and its recovery phase are durable before artifact I/O.
            write_json(snapshot_artifact, snapshot)
        if record["phase"] == "verifying":
            # Interrupted checks may have written partial evidence. Never overwrite it.
            rounds = record["verification_rounds"]
            verification_folder = folder / f"verification-{len(rounds) + 1}"
            round_record = {"artifact_dir": str(verification_folder), "status": "running"}
            rounds.append(round_record)
            self.context.save()
            checks = self.services.verification.verify("final", attempt, whole_run=True, artifact_dir=verification_folder, before=snapshot)
            evidence = folder / "change.diff"
            evidence.write_bytes(git.range_diff_bytes(self.context.worktree, self.context.run["base_commit"], binding=self.context.run["worktree_binding"]))
            round_record["status"] = "completed"
            record.update(verification=str(verification_folder / "results.json"), diff_artifact=str(evidence))
            failures = [check for check in checks if checks_failed(check)]
            if failures:
                record.update(status="verification_failed", phase="finished",
                              failure_kind="timeout" if all(check.get("timed_out") for check in failures) else "exit_code",
                              verification_failures=compact_checks(checks))
                if record["failure_kind"] != "timeout":
                    self.context.run["feedback"] = {"final_review": deepcopy(record)}
                    self.context.run["pending_replan"] = {"stage": "pending", "trigger": deepcopy(self.context.run["feedback"])}
            else:
                record["phase"] = "reviewing"
            write_json(folder / "final.json", record)
            self.context.save()
        else:
            checks = read_json(Path(record["verification"]))
        if record["phase"] == "reviewing":
            changed_paths = git.git(self.context.worktree, "diff", "--no-ext-diff", "--no-textconv",
                                    "--name-only", "--no-renames", "-z", self.context.run["base_commit"], "HEAD",
                                    binding=self.context.run["worktree_binding"]).split("\0")
            required = required_findings(self.context.run, "final", digest, whole_run=True, records=history[:-1])
            review, diagnostics = self.services.receipts.dispatch_review("final_review", "final",
                     self.services.packets.final_review_packet(
                         checks=checks, diff_artifact=record["diff_artifact"],
                         changed_paths=changed_paths, history=history[:-1]))
            record["review_source"] = bind_review_source(self.context, "final_review", "final", review, digest)
            disposition_errors = disposition_gaps(review, required)
            if disposition_errors:
                diagnostics = {**diagnostics, "disposition_errors": disposition_errors}
            if self.context.snapshot() != snapshot:
                raise HarnessError("整体审阅后文件发生变化；禁止完成 run")
            record.update(review=review, phase="finished", status="approved" if not diagnostics and review_approved(review) else "rejected")
            record.update(diagnostics)
            self.context.run["review_corrections"].pop("reviewer:final_review:final", None)
            self.context.run.get("review_receipts", {}).pop("reviewer:final_review:final", None)
            if record["status"] == "approved":
                self.context.run["feedback"] = None
            else:
                self.context.run["feedback"] = {"final_review": deepcopy(record)}
                self.context.run["pending_replan"] = {"stage": "pending", "trigger": deepcopy(self.context.run["feedback"])}
        write_json(folder / "final.json", record)
        self.context.save()
        if record["status"] == "approved":
            return True
        self.context.run["feedback"] = {"final_review": deepcopy(record)}
        self.context.save()
        self.context.checkpoint()
        if record["status"] == "verification_failed":
            self.context.event("final_verification_failed", attempt=attempt, failure_kind=record["failure_kind"])
            if record["failure_kind"] == "timeout":
                raise HarnessError("整体验证超时；证据已保存，修复环境或增加验证超时后 resume 可重验")
        else:
            self.context.event("final_review_rejected", attempt=attempt)
        return False

    def replan_unchanged_corrections(self, record):
        """Empty corrections do not erase a failed verification or review."""
        handled = set(record.get("noop_corrections_handled", []))
        corrections = set(self.context.run["accepted"]) - set(record["accepted_tasks"]) - handled
        if not corrections:
            return False
        record["noop_corrections_handled"] = sorted(handled | corrections)
        trigger = {"final_review": deepcopy(record), "unchanged_corrections": sorted(corrections),
                   "reason": "已接受的修正任务未改变未通过整体验收的代码快照，需要新的修正计划"}
        self.context.run["feedback"] = deepcopy(trigger)
        self.context.run["pending_replan"] = {"stage": "pending", "trigger": trigger}
        self.context.save()
        return True
