"""Shared recovery reads: pending-stage gates, bound original bytes, worker failure context."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .contracts import review_approved
from .util import HarnessError, strict_json_loads


# Shared pending lifecycle gates; each recovery entry keeps its own extra checks.
RECOVERY_BUSY_KEYS = (
    "pending_task", "pending_acceptance", "pending_post_accept",
    "pending_revision", "pending_directive", "review_receipts",
    "worker_receipts", "commander_receipts",
)


def snapshot_digest(snapshot):
    encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def invalidate_acceptance_review(run, feedback=None, *, attempt=None, candidate_digest=None):
    """Persist a known acceptance invalidation without changing its evidence.

    Feedback binds the exact review artifact. Call before replacing it, so a
    later plan cannot make the invalidated approval look current again.
    """
    feedback = run.get("feedback") if feedback is None else feedback
    if not isinstance(feedback, dict) or not isinstance(feedback.get("acceptance"), dict):
        return False
    artifact = feedback["acceptance"].get("review_artifact")
    if not artifact:
        return False
    changed = False
    for record in run.get("review_history", {}).get(feedback.get("task_id"), []):
        if (record.get("artifact") == artifact
                and (attempt is None or record.get("attempt") == attempt)
                and (candidate_digest is None or record.get("snapshot_digest") == candidate_digest)
                and record.get("status") != "invalidated"):
            record["status"] = "invalidated"
            changed = True
    return changed


def worker_recovery_context(run, directory, task_id):
    """Separate a retained failure from the latest evidenced task stages.

    This is navigation over persisted records, never an acceptance decision or
    a claim that the original failure's cause was fixed. Only one worker and
    its matching stages are projected; full histories remain in the ledger.
    """
    recovery = run.get("worker_recovery", {}).get(task_id)
    if not recovery:
        return None
    result = deepcopy(recovery)
    workers = [entry for entry in run.get("dispatches", [])
               if entry.get("role") == "worker" and entry.get("task_id") == task_id]
    invoked = [entry for entry in workers if entry.get("invoked") is True]
    if (any(type(entry.get("invoked")) is not bool for entry in workers)
            or len(invoked) != run.get("attempts", {}).get(task_id)):
        raise HarnessError("worker recovery invocation accounting is invalid")
    attempts = {entry["id"]: index for index, entry in enumerate(invoked, 1)}

    def source(entry):
        value = {"dispatch_id": entry["id"], "status": entry.get("status", "unknown"),
                 "artifact": str(Path(directory) / "dispatches" / entry["id"])}
        for key in ("invoked_at", "worker_outcome"):
            if key in entry:
                value[key] = entry[key]
        if entry["id"] in attempts:
            value["attempt"] = attempts[entry["id"]]
        return value

    original = next((entry for entry in workers if entry["id"] == recovery.get("dispatch_id")), None)
    latest = next((entry for entry in reversed(workers) if entry.get("invoked") is True), None)
    result["source"] = source(original) if original else {"status": "unknown"}
    result["relation"] = "unknown"
    if latest is None:
        return result
    result["latest"] = {"worker": source(latest)}
    if original is latest:
        result["relation"] = "latest_worker_failure"
    elif original is not None and workers.index(original) < workers.index(latest):
        result["relation"] = "earlier_worker_failure"
        # A subsequent invocation alone does not supersede the failed result.
        if latest.get("status") == "completed" and latest.get("worker_outcome"):
            result["superseded_by"] = latest["id"]

    attempt = attempts.get(latest["id"])
    if attempt is None or latest.get("status") != "completed" or latest.get("worker_outcome") != "completed":
        return result
    pending = run.get("pending_task", {})
    if pending.get("task_id") == task_id and pending.get("attempt") == attempt:
        if pending.get("verification"):
            result["latest"]["verification"] = {
                "attempt": attempt, "artifact": pending["verification"],
                "status": "passed" if pending["stage"] == "reviewing" else "pending"}
        if pending["stage"] == "reviewing":
            result["latest"]["review"] = {"attempt": attempt, "status": "pending"}
        return result
    record = next((item for item in reversed(run.get("review_history", {}).get(task_id, []))
                   if item.get("attempt") == attempt), None)
    if record:
        feedback = run.get("feedback") or {}
        acceptance = feedback.get("acceptance", {}) if feedback.get("task_id") == task_id else {}
        acceptance_invalidated = (bool(record.get("artifact"))
                                  and acceptance.get("review_artifact") == record["artifact"])
        invalidated = record.get("status") == "invalidated" or acceptance_invalidated
        if record.get("verification"):
            result["latest"]["verification"] = {
                "attempt": attempt, "artifact": record["verification"],
                "status": "invalidated" if invalidated else "passed"}
        review = {key: record[key] for key in ("attempt", "artifact", "snapshot_digest", "dispatch_id")
                  if key in record}
        review["status"] = ("invalidated" if invalidated else record["status"] if "status" in record else
                            "approved" if review_approved(record["review"]) else "rejected")
        if acceptance_invalidated:
            review["invalidation_source"] = "feedback.acceptance"
        if "review" in record:
            review["verdict"] = record["review"]["verdict"]
        result["latest"]["review"] = review
    return result


def read_bound_bytes(receipt, label, *, bind=None):
    """Read and bind original bytes before parsing or any later lifecycle gate."""
    try:
        raw = Path(receipt["artifact"]).read_bytes()
    except OSError as error:
        raise HarnessError(f"{label}原始结果缺失或无法读取；保留证据，禁止重新抽取结论") from error
    digest = hashlib.sha256(raw).hexdigest()
    if receipt.get("artifact_digest", digest) != digest:
        raise HarnessError(f"{label}原始结果已改变；保留证据，禁止重新抽取结论")
    if "artifact_digest" not in receipt and bind is not None:
        receipt["artifact_digest"] = digest
        bind()
    return raw, digest


def read_bound_result(receipt, label, *, bind=None):
    raw, digest = read_bound_bytes(receipt, label, bind=bind)
    try:
        # The runtime wrote this file; anything not strictly decodable was altered.
        result = strict_json_loads(raw)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise HarnessError(f"{label}原始结果不是有效 JSON") from error
    return result, digest
