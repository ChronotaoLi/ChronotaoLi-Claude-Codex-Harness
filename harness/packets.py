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
from .services import RuntimeService
from .shared import read_skill, skills_catalog
from .util import HarnessError


class PacketBuilder(RuntimeService):
    def accepted_context(self):
        # Keep summaries bounded; accepted commits and artifact paths are never removed.
        runtime = self.runtime
        return {tid: {"commit": item["commit"], "summary": item["summary"][:600],
                      "summary_truncated": len(item["summary"]) > 600,
                      "verification": item["verification"], "details": item["review_artifact"]}
                for tid, item in runtime.run["accepted"].items()}

    def plan_context(self):
        runtime = self.runtime
        return compact_plan(runtime.run["original_tasks"], runtime.run["tasks"])

    def final_review_packet(self, *, checks, diff_artifact, changed_paths,
                            tasks=None, original_tasks=None, history=None):
        """Build the shared final-review evidence before runtime projections.

        Callers supply only the evidence available at their stage. Capacity
        preflight uses empty future evidence; final acceptance supplies the
        verified artifacts and excludes its active attempt from history.
        """
        runtime = self.runtime
        tasks = runtime.run["tasks"] if tasks is None else tasks
        original_tasks = runtime.run["original_tasks"] if original_tasks is None else original_tasks
        history = runtime.run.get("final_history", []) if history is None else history
        return {
            "goal": runtime.run["goal"], **compact_plan(original_tasks, tasks),
            "accepted": runtime.accepted_context(), "base_commit": runtime.run["base_commit"],
            "head": runtime.run["expected_head"], "diff_artifact": diff_artifact,
            "verification": compact_checks(checks),
            "instruction_file_changes": instruction_file_changes(changed_paths),
            "ignored_paths": deepcopy(runtime.run.get("ignored_diagnostics")),
            "tool_constraints": runtime.tool_constraints(),
            "prior_reviews": compact_reviews([item for item in history if item["status"] != "running"]),
            "instruction": FINAL_REVIEW_INSTRUCTION,
        }

    def role_input(self, *, role, kind, task_id, schema, dispatch_id, packet, task=None):
        """Construct the same mandatory role envelope for dispatch and estimates."""
        runtime = self.runtime
        contract = envelope(schema, task_id, dispatch_id)
        required = {
            "role": role, "kind": kind, "task_id": task_id, "dispatch_id": dispatch_id,
            "rules": runtime.config.asset("rules").read_text(encoding="utf-8"),
            "role_skill": read_skill(runtime.config.asset("skills"), "harness-" + role),
            "packet": packet, "output_constraints": output_constraints(contract),
        }
        if role == "worker":
            required["output_constraints"].append({
                "path": "response.decision_requests[].task_id",
                "enum": [target["id"] for target in runtime.handoff_targets(task_id)],
            })
        if role == "reviewer":
            required["output_constraints"] = review_output_constraints(
                contract, [proposal["key"] for proposal in packet.get("proposed_memories", [])])
        else:
            required["available_skills"] = skills_catalog(runtime.config.asset("skills"), task_only=True)
        if task and role == "worker":
            required["task_skills"] = {
                skill: read_skill(runtime.config.asset("skills"), skill)
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
        runtime = self.runtime
        tasks = runtime.run["tasks"] if tasks is None else tasks
        if original_tasks is None:
            original_tasks = runtime.run["original_tasks"] or tasks
        dispatch_id = "00000000-0000-0000-0000-000000000000"
        checks = [{"argv": command, "exit_code": 0, "stdout": "", "stderr": "",
                   "log": "", "timed_out": False}
                  for command in runtime.config.data["verification"]["commands"]]
        packet = self.final_review_packet(checks=checks, diff_artifact="", changed_paths=[],
                                          tasks=tasks, original_tasks=original_tasks)
        packet = runtime.runtime_packet(packet, role="reviewer", kind="final_review", task_id="final")
        _, required = self.role_input(role="reviewer", kind="final_review", task_id="final", schema=REVIEW,
                                     dispatch_id=dispatch_id, packet=packet)
        projected = deepcopy(required)
        # Decisions are still fully validated and included in the estimate.
        # Answers can be revised and shared excerpts shrink as requests grow,
        # so their current projection cannot establish a hard future lower bound.
        for field in ("operator_decisions", "operator_decision_index", "operator_decision_policy"):
            required["packet"].pop(field, None)
        packet = projected["packet"]
        limits = runtime.config.data["runtime"]
        # Include a UUID-sized suffix as a reserve for distinct recovery artifacts.
        suffix = f"{max(limits['max_attempts'], limits['max_total_attempts'])}-{dispatch_id}"
        for task in tasks:
            if task["id"] not in packet["accepted"]:
                folder = runtime.directory / "verification" / "tasks" / task["id"] / suffix
                packet["accepted"][task["id"]] = {
                    "commit": "0" * len(runtime.run["base_commit"]), "summary": "x" * 600,
                    "summary_truncated": False, "verification": str(folder / "results.json"),
                    "details": str(folder / "review.json"),
                }
        final_suffix = f"{max(limits['max_final_attempts'], runtime.run.get('final_attempts', 0) + 1)}-{dispatch_id}"
        final_folder = runtime.directory / "verification" / "final" / final_suffix
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
        runtime = self.runtime
        return {"readonly_commands": runtime.config.readonly_commands(),
                "worker_commands": runtime.config.data["permissions"]["worker_commands"],
                "verification_commands": runtime.config.data["verification"]["commands"],
                "command_execution": {"worker_only": True, "tool": "mcp__harness_commands__run_command",
                                      "list_tool": "mcp__harness_commands__list_commands",
                                      "worker_command_ids": [command_id(c) for c in runtime.config.data["permissions"]["worker_commands"]],
                                      "verification_command_ids": [command_id(c) for c in runtime.config.data["verification"]["commands"]]},
                "file_tools": ["mcp__harness__json_check", "mcp__harness__file_sha256", "mcp__harness__file_diff"],
                "permission_guidance": "Workers: use aligned command IDs, with no argument overrides. Each call executes anew and saves stdout/stderr. Shell syntax requires its own authorized argv. Use file tools for JSON, hashes and differences. Append readonly Bash paths after --. A denied command does not prove a path unreadable; use Read/Glob/Grep. Command grants do not expand task scope."}

    def runtime_packet(self, packet, *, role, kind, task_id):
        runtime = self.runtime
        if role != "worker" and "tool_constraints" in packet:
            constraints = {k: v for k, v in packet["tool_constraints"].items() if k != "command_execution"}
            constraints["permission_guidance"] = ("Command broker is worker-only. Use file tools for JSON, hashes and differences; "
                "append readonly Bash paths after --. A denied command does not prove a path unreadable; use Read/Glob/Grep. "
                "Command grants do not expand task scope.")
            packet = {**packet, "tool_constraints": constraints}
        packet = runtime.environment_packet(packet, role=role, kind=kind, task_id=task_id)
        packet = runtime.decision_packet(packet, role=role, kind=kind, task_id=task_id)
        return runtime.progress_packet(packet, role=role, kind=kind, task_id=task_id)

    def preflight_final_context(self, tasks=None, original_tasks=None):
        runtime = self.runtime
        report = runtime.final_context_capacity(tasks, original_tasks)
        if report != runtime.run.get("context_capacity"):
            runtime.run["context_capacity"] = report
            runtime.save()
            runtime.event("final_context_preflight", report=report)
        if report["status"] == "required_overflow":
            raise HarnessError(f"计划的整体审阅必要上下文 {report['required_chars']} 字符超过预算 "
                               f"{report['budget_chars']}；请增加 context_chars 后恢复，尚未派发 worker")
        if report["status"] == "projected_overflow":
            print(f"[harness] 整体审阅预计上下文 {report['projected_chars']} 字符，超过预算 "
                  f"{report['budget_chars']}；建议提前增加 context_chars。此估计不保证后续历史容量。", file=sys.stderr, flush=True)
