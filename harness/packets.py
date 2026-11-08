"""Build role inputs and capacity estimates from the live run; no lifecycle transitions."""
from __future__ import annotations

import sys
from copy import deepcopy

from .command_server import command_id
from .context import (
    capacity_report, compact_checks, compact_plan, compact_reviews, FINAL_REVIEW_INSTRUCTION,
    instruction_file_changes,
)
from .contracts import envelope, output_constraints, REVIEW, review_output_constraints
from .path_evidence import project_paths
from .input_projection import project_history, project_commands
from .services import RuntimeService
from .shared import read_skill, skills_catalog
from .util import HarnessError
from .verification_plan import build_verification_plan, finding_progress_packet, project_finding_sources, verification_role_view, recent_costs
from .continuation import packet as continuation_packet
from .review_progress import required_findings, validate_review_source, worker_claim_template_reference
from .recovery import snapshot_digest


class PacketBuilder(RuntimeService):
    def accepted_context(self):
        # Keep summaries bounded; accepted commits and artifact paths are never removed.
        return {tid: {"commit": item["commit"], "summary": item["summary"][:600],
                      "summary_truncated": len(item["summary"]) > 600,
                      "verification": item["verification"], "details": item["review_artifact"]}
                for tid, item in self.context.run["accepted"].items()}

    def plan_context(self):
        return compact_plan(self.context.run["original_tasks"], self.context.run["tasks"])

    def final_review_packet(self, *, checks, diff_artifact, changed_paths,
                            tasks=None, original_tasks=None, history=None):
        """Build the shared final-review evidence before runtime projections.

        Callers supply only the evidence available at their stage. Capacity
        preflight uses empty future evidence; final acceptance supplies the
        verified artifacts and excludes its active attempt from history.
        """
        tasks = self.context.run["tasks"] if tasks is None else tasks
        original_tasks = self.context.run["original_tasks"] if original_tasks is None else original_tasks
        history = self.context.run.get("final_history", []) if history is None else history
        return {
            "goal": self.context.run["goal"], **compact_plan(original_tasks, tasks),
            "accepted": self.accepted_context(), "base_commit": self.context.run["base_commit"],
            "head": self.context.run["expected_head"], "diff_artifact": diff_artifact,
            "verification": compact_checks(checks),
            "instruction_file_changes": instruction_file_changes(changed_paths),
            "ignored_paths": deepcopy(self.context.run.get("ignored_diagnostics")),
            "tool_constraints": self.tool_constraints(),
            "prior_reviews": compact_reviews([item for item in history if item["status"] != "running"]),
            "required_findings": required_findings(
                self.context.run, "final", snapshot_digest(self.context.snapshot()),
                whole_run=True, records=history) if any(item.get("review") for item in history) else [],
            "instruction": FINAL_REVIEW_INSTRUCTION,
        }

    def role_input(self, *, role, kind, task_id, schema, dispatch_id, packet, task=None):
        """Construct the same mandatory role envelope for dispatch and estimates."""
        packet = project_paths(packet, self.context.directory)
        packet = project_commands(packet, self.context.directory)
        schema = deepcopy(schema)
        if role == "worker":
            for field, needed in (("evidence_files", bool((task or {}).get("evidence_requirements"))),
                                  ("repair_claims", bool(packet.get("required_findings")))):
                if needed:
                    pass  # Required for completed candidates by preflight; blocked workers can yield normally.
                else:
                    schema["properties"].pop(field, None)
        elif role == "reviewer":
            if not packet.get("required_findings"):
                schema["properties"].pop("finding_dispositions", None)
        if role == "worker":
            template = worker_claim_template_reference(self.context, task_id, dispatch_id, packet)
            if template is not None:
                packet["repair_claims_template"] = template
        contract = envelope(schema, task_id, dispatch_id)
        required = {
            "role": role, "kind": kind, "task_id": task_id, "dispatch_id": dispatch_id,
            "rules": self.context.config.asset("rules").read_text(encoding="utf-8"),
            "role_skill": read_skill(self.context.config.asset("skills"), "harness-" + role),
            "packet": packet, "output_constraints": output_constraints(contract),
        }
        if role == "worker":
            required["output_constraints"].append({
                "path": "response.decision_requests[].task_id",
                "enum": [target["id"] for target in self.services.workflow.handoff_targets(task_id)],
            })
        if role == "reviewer":
            required["output_constraints"] = review_output_constraints(
                contract, [proposal["key"] for proposal in packet.get("proposed_memories", [])])
        else:
            required["available_skills"] = skills_catalog(self.context.config.asset("skills"), task_only=True)
        if task and role == "worker":
            required["task_skills"] = {
                skill: read_skill(self.context.config.asset("skills"), skill)
                for skill in task["skills"] if skill != "harness-" + role
            }
        return contract, required

    def final_context_capacity(self, tasks=None, original_tasks=None):
        """Preflight a candidate plan without consuming a dispatch or rejecting estimates.

        Required size excludes unknown future summaries/output/path lengths and
        the revisable, count-dependent decision projection. The
        projection reserves bounded summaries, output excerpts and generous paths;
        only the required lower bound can establish that a plan cannot fit.
        """
        tasks = self.context.run["tasks"] if tasks is None else tasks
        if original_tasks is None:
            original_tasks = self.context.run["original_tasks"] or tasks
        dispatch_id = "00000000-0000-0000-0000-000000000000"
        checks = [{"argv": command, "exit_code": 0, "stdout": "", "stderr": "",
                   "log": "", "timed_out": False}
                  for command in self.context.config.data["verification"]["commands"]]
        packet = self.final_review_packet(checks=checks, diff_artifact="", changed_paths=[],
                                          tasks=tasks, original_tasks=original_tasks)
        packet = self.runtime_packet(packet, role="reviewer", kind="final_review", task_id="final")
        _, required = self.role_input(role="reviewer", kind="final_review", task_id="final", schema=REVIEW,
                                     dispatch_id=dispatch_id, packet=packet)
        projected = deepcopy(required)
        # Decisions are still fully validated and included in the estimate.
        # Answers can be revised and shared excerpts shrink as requests grow,
        # so their current projection cannot establish a hard future lower bound.
        for field in ("operator_decisions", "operator_decision_index", "operator_decision_policy"):
            required["packet"].pop(field, None)
        packet = projected["packet"]
        limits = self.context.config.data["runtime"]
        # Include a UUID-sized suffix as a reserve for distinct recovery artifacts.
        suffix = f"{max(limits['max_attempts'], limits['max_total_attempts'])}-{dispatch_id}"
        for task in tasks:
            if task["id"] not in packet["accepted"]:
                folder = self.context.directory / "verification" / "tasks" / task["id"] / suffix
                packet["accepted"][task["id"]] = {
                    "commit": "0" * len(self.context.run["base_commit"]), "summary": "x" * 600,
                    "summary_truncated": False, "verification": str(folder / "results.json"),
                    "details": str(folder / "review.json"),
                }
        final_suffix = f"{max(limits['max_final_attempts'], self.context.run.get('final_attempts', 0) + 1)}-{dispatch_id}"
        final_folder = self.context.directory / "verification" / "final" / final_suffix
        packet["diff_artifact"] = str(final_folder / "change.diff")
        for index, check in enumerate(packet["verification"]):
            check.update(stdout="x" * 250, stderr="x" * 250,
                         log=str(final_folder / f"check-{index}"), output_truncated=True)
        report = capacity_report(required, projected, limits["context_chars"])
        report["assumptions"] = {
            "pending_summary_chars": 600, "verification_output_chars_per_command": 500,
            "verification_commands": len(checks), "tasks": len(tasks),
            "includes_existing_review_history": True, "includes_future_review_history": False,
            "decisions_in_projection_only": True, "final_checkpoints_index_only": True,
            "projection_is_guarantee": False,
        }
        return report

    def tool_constraints(self):
        return {"readonly_commands": self.context.config.readonly_commands(),
                "worker_commands": self.context.config.data["permissions"]["worker_commands"],
                "verification_commands": self.context.config.data["verification"]["commands"],
                "command_execution": {"worker_only": True, "tool": "mcp__harness_commands__run_command",
                                      "list_tool": "mcp__harness_commands__list_commands",
                                      "worker_command_ids": [command_id(c) for c in self.context.config.data["permissions"]["worker_commands"]],
                                      "verification_command_ids": [command_id(c) for c in self.context.config.data["verification"]["commands"]]},
                "file_tools": ["mcp__harness__json_check", "mcp__harness__file_sha256", "mcp__harness__file_diff"],
                "permission_guidance": "Workers: use aligned command IDs, with no argument overrides. Each call executes anew and saves stdout/stderr. Shell syntax requires its own authorized argv. Use file tools for JSON, hashes and differences. Append readonly Bash paths after --. A denied command does not prove a path unreadable; use Read/Glob/Grep. Command grants do not expand task scope."}

    def runtime_packet(self, packet, *, role, kind, task_id):
        if role != "worker" and "tool_constraints" in packet:
            constraints = {k: v for k, v in packet["tool_constraints"].items() if k != "command_execution"}
            constraints["permission_guidance"] = ("Command broker is worker-only. Use file tools for JSON, hashes and differences; "
                "append readonly Bash paths after --. A denied command does not prove a path unreadable; use Read/Glob/Grep. "
                "Command grants do not expand task scope.")
            packet = {**packet, "tool_constraints": constraints}
        task = next((item for item in self.context.run.get("tasks", []) if item["id"] == task_id), None)
        if role == "worker" and self.context.run.get("review_history", {}).get(task_id):
            packet = {**packet, "required_findings": required_findings(
                self.context.run, task_id, snapshot_digest(self.context.snapshot()))}
        # Synthetic commander/final IDs describe a run-level view, not a task.
        scope = task_id if task is not None else None
        continuation = continuation_packet(self.context, scope)
        if continuation.get("execution_guidance"):
            digest = snapshot_digest(self.context.snapshot())
            dispatches = self.context.run.get("dispatches", [])
            for row in continuation["execution_guidance"]:
                source = row["source"]["dispatch_id"]
                position = next(i for i, item in enumerate(dispatches) if item["id"] == source)
                successors = [item for item in dispatches[position + 1:]
                              if item.get("invoked", True) and item.get("role") == "worker"
                              and item.get("task_id") == row["task_id"]]
                row["applicability"] = {
                    "source_dispatch_id": source,
                    "source_snapshot_digest": row["snapshot_digest"],
                    "current_candidate_matches": digest == row["snapshot_digest"],
                    "superseded_by_worker_dispatches": [item["id"] for item in successors],
                    "active": digest == row["snapshot_digest"] and not successors}
        continuation = project_history(continuation, self.context.directory)
        # Validate every historical body used by progress, including run-wide views.
        for tid, records in self.context.run.get("review_history", {}).items():
            if scope is None or tid == scope:
                for record in records:
                    if record.get("review"):
                        validate_review_source(self.context.run, tid, record)
                    elif record.get("validation_error"):
                        raise HarnessError("历史审阅格式无效；必须核对原始证据")
        if scope is None:
            for record in self.context.run.get("final_history", []):
                if record.get("review"):
                    validate_review_source(self.context.run, "final", record, kind="final_review")
                elif record.get("validation_error"):
                    raise HarnessError("历史整体审阅格式无效；必须核对原始证据")
        packet = {**packet, **{key: value for key, value in continuation.items() if value}}
        self.context.store.check_current()
        progress = finding_progress_packet(self.context.run, task_id, self.context.directory)
        if progress is not None:
            packet = {**packet, "finding_progress": progress}
        packet = {**packet, "verification_plan": verification_role_view(
            build_verification_plan(self.context.config.data, task, cost_samples=recent_costs(self.context)), role)}
        packet = project_finding_sources(packet)
        packet = self.services.environment.environment_packet(packet, role=role, kind=kind, task_id=task_id)
        packet = self.services.decisions.decision_packet(packet, role=role, kind=kind, task_id=task_id)
        return self.services.progress.progress_packet(packet, role=role, kind=kind, task_id=task_id)

    def preflight_final_context(self, tasks=None, original_tasks=None):
        report = self.final_context_capacity(tasks, original_tasks)
        if report != self.context.run.get("context_capacity"):
            self.context.run["context_capacity"] = report
            self.context.save()
            self.context.event("final_context_preflight", report=report)
        if report["status"] == "required_overflow":
            raise HarnessError(f"计划的整体审阅必要上下文 {report['required_chars']} 字符超过预算 "
                               f"{report['budget_chars']}；请增加 context_chars 后恢复，尚未派发 worker")
        if report["status"] == "projected_overflow":
            print(f"[harness] 整体审阅预计上下文 {report['projected_chars']} 字符，超过预算 "
                  f"{report['budget_chars']}；建议提前增加 context_chars。此估计不保证后续历史容量。", file=sys.stderr, flush=True)
