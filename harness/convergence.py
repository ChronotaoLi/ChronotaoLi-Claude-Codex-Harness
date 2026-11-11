"""Read-only retry signals; these never grant retries or alter acceptance gates."""
from __future__ import annotations


def _blocking(review):
    """Blocking finding labels (the reviewer's ids) in report order, without duplicates.

    Labels are compared across rounds only as an observation: a finding's
    identity is its source review and position, and labels need not be unique.
    """
    findings = review.get("findings", []) if isinstance(review, dict) else []
    if not isinstance(findings, list):
        return []
    return list(dict.fromkeys(item["id"] for item in findings if isinstance(item, dict)
                              and item.get("severity") == "blocking" and isinstance(item.get("id"), str)))


def blocking_ids(review):
    return set(_blocking(review))


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


def review_rounds(run):
    """Per task, the blocking finding IDs each review round reports for the first time.

    A measurement over review_history in recorded order (round = 1-based record
    position, including invalidated or structurally invalid records): no model
    call, no effect on acceptance, and no inferred cause. Findings are compared
    by label, the reviewer's id, which is not an identity. The candidate is named by its
    recorded snapshot digest and the source by the review dispatch.
    """
    histories = run.get("review_history", {}) if isinstance(run, dict) else {}
    histories = histories if isinstance(histories, dict) else {}
    text = lambda value: value if isinstance(value, str) else None
    result = {}
    for task_id, records in histories.items():
        if not isinstance(task_id, str) or not isinstance(records, list):
            continue
        seen, rounds = set(), []
        for number, record in enumerate((item for item in records if isinstance(item, dict)), 1):
            review = record.get("review") if isinstance(record.get("review"), dict) else None
            source = record.get("review_source") if isinstance(record.get("review_source"), dict) else {}
            reported = _blocking(review)
            row = {"round": number, "attempt": record.get("attempt") if type(record.get("attempt")) is int else None,
                   "review_dispatch_id": text(source.get("dispatch_id")) or text(record.get("dispatch_id")),
                   "snapshot_digest": text(record.get("snapshot_digest")),
                   "verdict": text(review.get("verdict")) if review else None,
                   "new_blocking": [ident for ident in reported if ident not in seen]}
            if text(record.get("status")):
                row["status"] = record["status"]
            seen.update(reported)
            rounds.append(row)
        result[task_id] = rounds
    return {"read_only": True, "cause": "unknown", "tasks": result}
