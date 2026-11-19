"""Read-only retry signals; these never grant retries or alter acceptance gates."""
from __future__ import annotations

from .review_progress import dispositions_usable


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


def _text(value):
    return value if isinstance(value, str) else None


def _number(value):
    return value if type(value) is int else None


def _reference(number, record):
    """How a review round and a rework hint name one review record."""
    review = record.get("review") if isinstance(record.get("review"), dict) else None
    source = record.get("review_source") if isinstance(record.get("review_source"), dict) else {}
    return {"round": number, "attempt": _number(record.get("attempt")),
            "review_dispatch_id": _text(source.get("dispatch_id")) or _text(record.get("dispatch_id")),
            "snapshot_digest": _text(record.get("snapshot_digest")),
            "verdict": _text(review.get("verdict")) if review else None}


def _outcome(record):
    """The acceptance gate's decision on a record (_finish_review), not its raw verdict."""
    if record.get("status") == "invalidated":
        return "invalidated"
    review = record.get("review")
    if not isinstance(review, dict) or not dispositions_usable(record):
        return "rejected"
    findings = review.get("findings")
    blocked = isinstance(findings, list) and any(
        isinstance(item, dict) and item.get("severity") == "blocking" for item in findings)
    return "approved" if review.get("verdict") == "approve" and not blocked else "rejected"


def _conclusions(run, task_id, records):
    """(round, record, outcome) per work attempt whose stage has ended, in round order.

    Only an attempt's last record can conclude it, since every batch before it
    is an approving intermediate one: an attempt still in its stage has none,
    and an approval concludes only as the one awaiting acceptance.
    """
    pending = run.get("pending_task") if isinstance(run.get("pending_task"), dict) else {}
    accepting = run.get("pending_acceptance") if isinstance(run.get("pending_acceptance"), dict) else {}
    open_attempt = _number(pending.get("attempt")) if pending.get("task_id") == task_id else None
    last = {}
    for number, record in enumerate(records, 1):
        attempt = _number(record.get("attempt"))
        last[("record", number) if attempt is None else attempt] = (number, record)
    result = []
    for attempt, (number, record) in last.items():
        if attempt == open_attempt:
            continue
        outcome = _outcome(record)
        source = record.get("review_source")
        if outcome == "approved" and not (
                accepting.get("task_id") == task_id and _number(accepting.get("attempt")) == attempt
                and isinstance(source, dict) and accepting.get("review_source") == source):
            continue
        result.append((number, record, outcome))
    return sorted(result, key=lambda item: item[0])


def convergence_summary(run, work_counts):
    run = run if isinstance(run, dict) else {}
    accepted = run.get("accepted") if isinstance(run.get("accepted"), dict) else {}
    histories = run.get("review_history", {})
    histories = histories if isinstance(histories, dict) else {}
    attempts = run.get("attempts") if isinstance(run.get("attempts"), dict) else {}
    warnings = []
    terminal = run.get("status") in ("completed", "abandoned")
    reference = lambda item: {**_reference(item[0], item[1]), "outcome": item[2]}
    for task_id, count in work_counts.items():
        if terminal or task_id in accepted:
            continue
        history = histories.get(task_id, [])
        history = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
        conclusions = _conclusions(run, task_id, history)
        repeated = []
        if len(conclusions) >= 2 and conclusions[-1][2] == conclusions[-2][2] == "rejected":
            repeated = sorted(blocking_ids(conclusions[-1][1].get("review"))
                              & blocking_ids(conclusions[-2][1].get("review")))
        reasons = [name for name, holds in (("work_attempts", count >= 3), ("repeated_labels", bool(repeated))) if holds]
        if not reasons:
            continue
        latest = reference(conclusions[-1]) if conclusions else None
        latest_attempt = _number(attempts.get(task_id))
        since = None
        if latest is None:
            since = latest_attempt
        elif latest_attempt is not None and latest["attempt"] is not None and latest_attempt >= latest["attempt"]:
            since = latest_attempt - latest["attempt"]
        warnings.append({"kind": "task_rework", "task_id": task_id, "work_attempts": count,
                         "review_rejections": sum(item[2] == "rejected" for item in conclusions),
                         "repeated_labels": repeated[:20], "repeated_labels_truncated": len(repeated) > 20,
                         "reasons": reasons,
                         "compared_reviews": [reference(item) for item in conclusions[-2:]] if repeated else [],
                         "latest_review": latest, "latest_attempt": latest_attempt,
                         "attempts_since_latest_review": since})
    final_history = run.get("final_history", [])
    final_history = [item for item in final_history if isinstance(item, dict)] if isinstance(final_history, list) else []
    final_rejections = [item for item in final_history if item.get("status") in ("rejected", "invalid_rejected")]
    if not terminal and len(final_rejections) >= 2 and final_history[-1].get("status") != "approved":
        rejection, last = final_rejections[-1], final_history[-1]
        source = rejection.get("review_source") if isinstance(rejection.get("review_source"), dict) else {}
        warnings.append({"kind": "whole_run_rework", "review_rejections": len(final_rejections),
                         "latest_rejection": {"attempt": _number(rejection.get("attempt")),
                                              "review_dispatch_id": _text(source.get("dispatch_id")),
                                              "snapshot_digest": _text(rejection.get("snapshot_digest"))},
                         "latest_final_attempt": {"attempt": _number(last.get("attempt")),
                                                  "status": _text(last.get("status")), "phase": _text(last.get("phase"))}})
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
    result = {}
    for task_id, records in histories.items():
        if not isinstance(task_id, str) or not isinstance(records, list):
            continue
        seen, rounds = set(), []
        for number, record in enumerate((item for item in records if isinstance(item, dict)), 1):
            reported = _blocking(record.get("review"))
            row = {**_reference(number, record), "new_blocking": [ident for ident in reported if ident not in seen]}
            if _text(record.get("status")):
                row["status"] = record["status"]
            seen.update(reported)
            rounds.append(row)
        result[task_id] = rounds
    return {"read_only": True, "cause": "unknown", "tasks": result}
