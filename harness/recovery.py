"""Shared recovery reads: pending-stage gates, bound original bytes, worker failure context."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from . import stops
from .contracts import review_approved
from .state_files import StateFiles
from .util import EvidenceError, HarnessError, strict_json_loads


# Shared pending lifecycle gates; each recovery entry keeps its own extra checks.
RECOVERY_BUSY_KEYS = (
    "pending_task", "pending_acceptance", "pending_post_accept",
    "pending_revision", "pending_directive", "review_receipts",
    "worker_receipts", "commander_receipts",
)


def snapshot_digest(snapshot):
    encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def acceptance_review_records(run, feedback=None, *, attempt=None, candidate_digest=None):
    """The review records a known acceptance invalidation voids that are not yet marked; the ledger is unchanged."""
    feedback = run.get("feedback") if feedback is None else feedback
    if not isinstance(feedback, dict) or not isinstance(feedback.get("acceptance"), dict):
        return []
    artifact = feedback["acceptance"].get("review_artifact")
    if not artifact:
        return []
    return [record for record in run.get("review_history", {}).get(feedback.get("task_id"), [])
            if record.get("artifact") == artifact
            and (attempt is None or record.get("attempt") == attempt)
            and (candidate_digest is None or record.get("snapshot_digest") == candidate_digest)
            and record.get("status") != "invalidated"]

def worker_recovery_context(run, directory, task_id, *, feedback=None):
    """Separate a retained failure from the latest evidenced task stages.

    This is navigation over persisted records, never an acceptance decision or
    a claim that the original failure's cause was fixed. Only one worker and
    its matching stages are projected; full histories remain in the ledger.
    The failure's text appears once per packet: on the failure source, unless
    ``feedback`` (the same packet's feedback) already carries it.
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

    def source(entry, *, failure=False):
        value = {"dispatch_id": entry["id"], "status": entry.get("status", "unknown"),
                 "artifact": str(Path(directory) / "dispatches" / entry["id"])}
        text = entry.get("error")
        carried = (isinstance(feedback, dict) and feedback.get("task_id") == task_id
                   and feedback.get("output_error") == text)
        if failure and isinstance(text, str) and not carried:
            # The retained failure's text, read from its dispatch entry: once, on
            # the failure source, never again on the latest worker.
            value["error"] = stops.bounded(text)
        for key in ("invoked_at", "worker_outcome"):
            if key in entry:
                value[key] = entry[key]
        evidence = entry.get("worker_return_evidence")
        provider = evidence.get("provider") if isinstance(evidence, dict) else None
        if entry.get("worker_receipt_consumed") == "provider_failure" and isinstance(provider, dict):
            # The judged failure's type, so the next worker knows what stopped it.
            from .provider_failures import STOPS
            if isinstance(provider.get("code"), str) and provider["code"] in STOPS:
                kind, reason = STOPS[provider["code"]]
                value["failure"] = {"kind": kind, "reason": reason}
        if entry["id"] in attempts:
            value["attempt"] = attempts[entry["id"]]
        return value

    original = next((entry for entry in workers if entry["id"] == recovery.get("dispatch_id")), None)
    latest = next((entry for entry in reversed(workers) if entry.get("invoked") is True), None)
    result["source"] = source(original, failure=True) if original else {"status": "unknown"}
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


def _evidence(message, receipt):
    return EvidenceError(message, stop=stops.make("evidence", "runtime_integrity", source=_evidence_source(receipt)))


def _evidence_source(receipt):
    return {key: receipt[key] for key in ("artifact", "artifact_digest", "dispatch_id")
            if isinstance(receipt.get(key), str)}


def run_files(context):
    return StateFiles(context.state).under("runs", context.run["id"])


def returned_result_files(context, dispatch_id):
    return run_files(context).under("dispatches", dispatch_id, "returned-result.json")


def evidence_path(files, source):
    """Check a ledger reference against the file selected by the caller."""
    path = Path(files.path).joinpath(*files.prefix)
    if source.get("artifact") != str(path):
        raise _evidence("证据路径与登记身份不符；保留证据", source)
    return path


def bound_bytes(files, source, label, *, bind=None):
    """Read the caller-selected file and authenticate its bytes before interpreting them."""
    evidence_path(files, source)
    outcome = files.read((), limit=None)
    try:
        raw = outcome.evidence(_evidence_source(source))
    except EvidenceError as error:
        raise EvidenceError(
            f"{label}原始结果缺失或无法读取；保留证据，禁止重新抽取结论。{error}",
            stop=error.stop,
        ) from error
    digest = hashlib.sha256(raw).hexdigest()
    if source.get("artifact_digest", digest if bind is not None else None) != digest:
        raise _evidence(f"{label}原始结果已改变；保留证据，禁止重新抽取结论", source)
    if "artifact_digest" not in source and bind is not None:
        source["artifact_digest"] = digest
        bind()
    return raw, digest


def bound_result(files, source, label, *, bind=None):
    raw, digest = bound_bytes(files, source, label, bind=bind)
    return _decode_result(raw, digest, source, label)


def evidence_json(files, artifact):
    """Decode a runtime-written JSON artifact after a descriptor-relative read."""
    source = {"artifact": artifact}
    evidence_path(files, source)
    raw = files.read((), limit=None).evidence(source)
    return decode_evidence_json(raw, source)


def decode_evidence_json(raw, source, *, message=None):
    with stops.evidence_reads(source=source, message=message):
        return strict_json_loads(raw.decode("utf-8"))


def read_bound_bytes(receipt, label, *, bind=None):
    """Read and bind original bytes before parsing or any later lifecycle gate.

    Path loss and changed bytes are EvidenceError; other local I/O stays itself.
    The ``bind`` callback (a save) runs outside the evidence read.
    """
    try:
        raw = Path(receipt["artifact"]).read_bytes()
    except OSError as error:
        stops.keep_local_io(error)
        raise _evidence(f"{label}原始结果缺失或无法读取；保留证据，禁止重新抽取结论", receipt) from error
    digest = hashlib.sha256(raw).hexdigest()
    if receipt.get("artifact_digest", digest) != digest:
        raise _evidence(f"{label}原始结果已改变；保留证据，禁止重新抽取结论", receipt)
    if "artifact_digest" not in receipt and bind is not None:
        receipt["artifact_digest"] = digest
        bind()
    return raw, digest


def read_bound_result(receipt, label, *, bind=None):
    raw, digest = read_bound_bytes(receipt, label, bind=bind)
    return _decode_result(raw, digest, receipt, label)


def _decode_result(raw, digest, receipt, label):
    try:
        # The runtime wrote this file; anything not strictly decodable was altered.
        result = strict_json_loads(raw)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise _evidence(f"{label}原始结果不是有效 JSON", receipt) from error
    return result, digest
