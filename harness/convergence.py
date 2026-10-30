"""Read-only retry signals; these never grant retries or alter acceptance gates."""
from __future__ import annotations


def convergence_summary(run, work_counts):
    accepted = run.get("accepted", {})
    histories = run.get("review_history", {})
    histories = histories if isinstance(histories, dict) else {}
    warnings = []
    terminal = run.get("status") in ("completed", "abandoned")
    for task_id, count in work_counts.items():
        if terminal or task_id in accepted:
            continue
        history = histories.get(task_id, [])
        history = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
        reviews = [item["review"] for item in history if isinstance(item.get("review"), dict)]
        rejected = [item for item in reviews if item.get("verdict") == "request_changes"]
        recurring = []
        if len(reviews) >= 2 and all(item.get("verdict") == "request_changes" for item in reviews[-2:]):
            def blocking_ids(review):
                findings = review.get("findings", [])
                if not isinstance(findings, list):
                    return set()
                return {item["id"] for item in findings if isinstance(item, dict)
                        and item.get("severity") == "blocking" and isinstance(item.get("id"), str)}
            recurring = sorted(blocking_ids(reviews[-1]) & blocking_ids(reviews[-2]))
        if count >= 3 or recurring:
            warnings.append({"kind": "task_rework", "task_id": task_id, "work_attempts": count,
                             "review_rejections": len(rejected), "recurring_findings": recurring[:20],
                             "recurring_findings_truncated": len(recurring) > 20})
    final_history = run.get("final_history", [])
    final_history = [item for item in final_history if isinstance(item, dict)] if isinstance(final_history, list) else []
    final_rejections = sum(item.get("status") in ("rejected", "invalid_rejected") for item in final_history)
    if not terminal and final_rejections >= 2 and final_history[-1].get("status") != "approved":
        warnings.append({"kind": "whole_run_rework", "review_rejections": final_rejections})
    return {"advisory_only": True, "warnings": warnings[:20], "truncated": len(warnings) > 20,
            "suggestion": "核对反复出现的问题、任务范围及方案复杂度，再决定是否继续；提示不改变预算、权限或验收。"}
