"""Runtime-keyed required findings, repair claims and dispositions; never inferred closure.

A finding's identity is (source review dispatch, position in that review's
findings); its model-written id is only a label. Each dispatch receives keys
f1..fN for its required findings, and the key-to-source mapping is saved before
the dispatch, so a model never writes an identity the runtime already owns.
Two lists derive from the same history: a worker's repair list (findings still
open, whatever the candidate) and a review's batch (findings its candidate still
lacks a usable resolution for, at most BATCH_LIMIT; every batch of a candidate
is a complete review, and only the last one's approval can be accepted).
"""
from copy import deepcopy
from pathlib import PurePosixPath

from . import git, stops
from .contracts import envelope, judge_review, review_approved, review_schema
from .recovery import bound_result, run_files
from .util import EvidenceError, HarnessError


def dispositions_usable(record):
    """Rejected validation is evidence of issues, never authority to close them."""
    return (record.get("status") not in ("invalidated", "invalid_rejected")
            and not record.get("validation_error") and not record.get("disposition_errors"))


def _blocking_requirements(record):
    source = record["review_source"]
    return [{"review_dispatch_id": source["dispatch_id"], "finding_index": index,
             "label": finding["id"], "artifact": source["artifact"]}
            for index, finding in enumerate(record["review"].get("findings", []))
            if finding.get("severity") == "blocking"]


# The most runtime-keyed rows one dispatch carries, for a worker and a review alike.
BATCH_LIMIT = 60


def _keyed(rows):
    return [{"key": f"f{number}", **row} for number, row in enumerate(rows, 1)]


def _holds_no_review(record, whole_run):
    """Whether a record's own state is one its writer leaves without a review; the key's absence never decides.

    A whole-run attempt has none while it runs and after its verification
    failed. A record of either history has none only as an original kept when
    its candidate changed (a task's pending gate, or a whole-run attempt left)
    whose result was unreadable (status invalidated, with its validation
    error). Every other record is written together with its review.
    """
    if whole_run and record.get("status") in ("running", "verification_failed"):
        return True
    return record.get("status") == "invalidated" and "validation_error" in record


def _registered_dispatch(item):
    """The review dispatch a history record or approved batch registers, if it names one.

    Any other value is left to source authentication, which refuses it.
    """
    source = item.get("review_source")
    ident = source.get("dispatch_id") if isinstance(source, dict) else None
    return ident if isinstance(ident, str) else None


def entries(records, whole_run=False):
    """The review entries of a history, in order, each with whether the ledger presents it as an approval.

    An entry is every record that holds a review; presence decides, so an
    emptied review is still authenticated against its original. A whole-run
    attempt first yields its approved batches, each on the attempt's snapshot;
    every one is an approval written with its review, whatever the attempt's
    status. A record that is not an object, or lacks a review its state
    requires, is damaged evidence, never skipped; so is a whole-run record
    whose approved_batches is not such a list, and a review dispatch that
    records and approved batches register more than once, which would let one
    review count under two mappings. The load checks apply this to every
    history, so in a loaded ledger this is the same invariant checked again
    where it is used. Only approved batches and a whole-run record with status
    approved are approvals; a task record never carries one.
    """
    result, registered = [], set()
    for record in records:
        if not isinstance(record, dict):
            raise EvidenceError("审阅历史记录不是对象；保留证据", stop=stops.make("evidence", "runtime_integrity"))
        if "review" not in record and not _holds_no_review(record, whole_run):
            raise EvidenceError("审阅历史记录的状态要求审阅原文，记录中却没有；保留证据",
                                stop=stops.make("evidence", "runtime_integrity"))
        batches = []
        if whole_run:
            batches = record.get("approved_batches")
            if not isinstance(batches, list) or not all(isinstance(batch, dict) and "review" in batch for batch in batches):
                raise EvidenceError("整体记录的已批准批次不是带审阅原文的对象列表；保留证据",
                                    stop=stops.make("evidence", "runtime_integrity"))
        for item in (*batches, record):
            ident = _registered_dispatch(item)
            if ident in registered:
                raise EvidenceError("同一次审阅派发在审阅历史中登记了不止一次；保留证据",
                                    stop=stops.make("evidence", "runtime_integrity"))
            if ident is not None:
                registered.add(ident)
        result.extend(({**batch, "snapshot_digest": record.get("snapshot_digest")}, True) for batch in batches)
        if "review" in record:
            result.append((record, whole_run and record.get("status") == "approved"))
    return result


def _approval_holds(entry, task_id):
    """Whether an entry presented as an approval is one under its own saved mapping.

    The review is the authenticated original without its envelope, so the
    contract is judged by the same function and mapping its registration used.
    """
    review, rows = entry["review"], entry.get("required_findings")
    dispatch_id = entry["review_source"]["dispatch_id"]
    try:
        return (isinstance(rows, list)
                and judge_review({"task_id": task_id, "dispatch_id": dispatch_id, **review},
                                 envelope(review_schema(rows), task_id, dispatch_id)).state == "valid"
                and review_approved(review) and not disposition_gaps(review, rows)
                and not entry.get("validation_error") and not entry.get("disposition_errors"))
    except (KeyError, TypeError, AttributeError):
        return False


def _history(run, task_id, whole_run, records=None):
    """The records a traversal reads: those given, else the task's or the whole run's history."""
    if records is not None:
        return records
    return run.get("final_history", []) if whole_run else run.get("review_history", {}).get(task_id, [])


def _batch(issues, status, done):
    """A candidate's next review batch: the findings it lacks a usable resolution for, keyed in source order.

    When they do not all fit, open ones are chosen first, then the rest in
    source order; the chosen rows keep source order, so a list that fits is
    the whole list, unchanged.
    """
    missing = [ident for ident in issues if ident not in done]
    if len(missing) > BATCH_LIMIT:
        chosen = set(([ident for ident in missing if status[ident] == "open"]
                      + [ident for ident in missing if status[ident] != "open"])[:BATCH_LIMIT])
        missing = [ident for ident in missing if ident in chosen]
    return _keyed([issues[ident] for ident in missing])


def _progress(run, task_id, *, files, whole_run=False, records=None):
    """One pass over a history: every blocking finding, its repair status, the
    findings each candidate holds a usable resolution for, and the checked
    mapping and dispositions of every entry that has them, in history order.

    An entry presented as an approval contributes only if it passes the
    approval contract under its own mapping. An entry's dispositions act only
    through its saved copy of the mapping its dispatch received, and that copy
    must equal, row by row, the batch recomputed from the entries before it for
    the entry's own candidate, whether or not the entry is usable; any
    difference stops instead of guessing. Then resolved counts only from a
    usable entry and closes the finding only on that candidate; open from any
    entry is a kept veto; not_checked changes nothing.
    """
    issues, status, closed, keyed = {}, {}, {}, []
    for entry, approval in entries(_history(run, task_id, whole_run, records), whole_run):
        validate_review_source(run, task_id, entry, files=files, kind="final_review" if whole_run else "review")
        if approval and not _approval_holds(entry, task_id):
            raise HarnessError("批准条目不满足批准契约；保留证据")
        # The field's presence, not its truth, decides the check: an empty
        # mapping must still equal the batch its dispatch received.
        if "finding_dispositions" in entry["review"]:
            dispositions = entry["review"]["finding_dispositions"]
            done = closed.setdefault(entry["snapshot_digest"], set())
            copy = entry.get("required_findings")
            if (not isinstance(dispositions, dict) or copy != _batch(issues, status, done)
                    or set(dispositions) != {row["key"] for row in copy}
                    or any(not isinstance(value, dict) or "status" not in value for value in dispositions.values())):
                raise HarnessError("审阅处置与键对照不符；保留证据")
            usable = dispositions_usable(entry)
            for row in copy:
                ident = (row["review_dispatch_id"], row["finding_index"])
                outcome = dispositions[row["key"]]["status"]
                if outcome == "resolved" and usable:
                    status[ident] = "resolved"
                    done.add(ident)
                elif outcome == "open":
                    status[ident] = "open"
            source = entry["review_source"]
            keyed.append({"review_dispatch_id": source["dispatch_id"], "snapshot_digest": entry["snapshot_digest"],
                          "artifact": source["artifact"], "artifact_digest": source["artifact_digest"],
                          "required_findings": deepcopy(copy), "finding_dispositions": deepcopy(dispositions)})
        for row in _blocking_requirements(entry):
            ident = (row["review_dispatch_id"], row["finding_index"])
            issues[ident], status[ident] = row, "open"
    return issues, status, closed, keyed


@stops.reads_evidence
def required_findings(run, task_id, candidate_digest, *, files, whole_run=False, records=None):
    """A review's required findings on one candidate: its next batch, empty once every blocking finding has a usable resolution there."""
    issues, status, closed, _ = _progress(run, task_id, files=files, whole_run=whole_run, records=records)
    return _batch(issues, status, closed.get(candidate_digest, set()))


@stops.reads_evidence
def batch_after(run, task_id, candidate_digest, approved, *, files, whole_run=False, records):
    """The next batch after an approved one is registered: it holds none of the approved batch's findings.

    An approved batch closes every row it was given on its candidate, so a row
    that comes back means the approval closed less than it is registered for;
    that is damaged evidence, never a batch to dispatch again.
    """
    following = required_findings(run, task_id, candidate_digest, files=files, whole_run=whole_run, records=records)
    if ({(row["review_dispatch_id"], row["finding_index"]) for row in following}
            & {(row["review_dispatch_id"], row["finding_index"]) for row in approved}):
        raise EvidenceError("已批准的审阅批没有关闭本批的全部问题；保留证据", stop=stops.make("evidence", "runtime_integrity"))
    return following


@stops.reads_evidence
def require_saved_batch(run, task_id, candidate_digest, saved, *, files, whole_run=False, records=None, receipt=None):
    """Before a saved, unregistered batch is dispatched, replayed or preserved: it is the batch recomputed now.

    A receipt to replay must not be a dispatch the history already registered:
    bound to this batch, one review would count under two mappings.
    """
    if required_findings(run, task_id, candidate_digest, files=files, whole_run=whole_run, records=records) != saved:
        raise EvidenceError("审阅处置与键对照不符；保留证据", stop=stops.make("evidence", "runtime_integrity"))
    ident = receipt.get("dispatch_id") if isinstance(receipt, dict) else None
    if ident is not None and any(_registered_dispatch(item) == ident for record in _history(run, task_id, whole_run, records)
                                 for item in (*(record["approved_batches"] if whole_run else ()), record)):
        raise EvidenceError("审阅回执的派发已登记在审阅历史中；保留证据，禁止登记到当前批",
                            stop=stops.make("evidence", "runtime_integrity"))


@stops.reads_evidence
def verify_history(run, task_id, *, files, whole_run=False):
    """One read-only traversal: every source authenticated, every saved mapping and approval checked.

    Returns each disposition entry's mapping and dispositions, the records a
    packet's prior_review_keys_index is published from, so that index holds
    only what this traversal checked.
    """
    return _progress(run, task_id, files=files, whole_run=whole_run)[3]


@stops.reads_evidence
def repair_findings(run, task_id, *, files):
    """A worker's required findings: the first BATCH_LIMIT still open, in source order, whatever the candidate."""
    issues, status, _, _ = _progress(run, task_id, files=files)
    return _keyed([row for ident, row in issues.items() if status[ident] == "open"][:BATCH_LIMIT])


@stops.reads_evidence
def delivered_worker_findings(context, task_id, dispatch_id):
    """The required findings one worker dispatch received, read from its published packet.json.

    The packet and contract are the files whose SHA256 the receipt intent saved
    with invoked=true. Every row must name a blocking finding of an authenticated
    review of this task, and the keys must equal the contract's repair_claims.
    """
    from .worker_receipts import read_worker_input
    entries = [d for d in context.run.get("dispatches", []) if d.get("id") == dispatch_id]
    if (len(entries) != 1 or entries[0].get("role") != "worker"
            or entries[0].get("task_id") != task_id or entries[0].get("invoked") is not True):
        raise HarnessError("worker 必需问题的派发身份无效；保留证据")
    try:
        intent = entries[0].get("worker_receipt")
        if not isinstance(intent, dict):
            raise ValueError("missing worker intent")
        packet = read_worker_input(context.directory, entries[0], intent, "packet")
        schema = read_worker_input(context.directory, entries[0], intent, "schema")
        rows = packet.get("required_findings", [])
        sources = packet.get("required_finding_sources", {})
        # The runtime never publishes more rows; more is damaged evidence, not a capacity limit.
        if not isinstance(rows, list) or len(rows) > BATCH_LIMIT:
            raise ValueError("required findings capacity invalid")
        keys = [row["key"] for row in rows]
        claims = schema["properties"].get("repair_claims")
        if keys != [f"f{n}" for n in range(1, len(rows) + 1)] or (
                claims["required"] if claims else []) != keys:
            raise ValueError("delivered keys differ from delivered contract")
        canonical = {}
        records = context.run.get("review_history", {}).get(task_id, [])
        result, seen = [], set()
        for row in rows:
            source = row["review_dispatch_id"]
            if source not in canonical:
                matches = [r for r in records if (r.get("review_source") or {}).get("dispatch_id") == source]
                if len(matches) != 1:
                    raise ValueError("review source missing or ambiguous")
                validate_review_source(context.run, task_id, matches[0], files=run_files(context))
                canonical[source] = {r["finding_index"]: r for r in _blocking_requirements(matches[0])}
            original = canonical[source].get(row["finding_index"])
            artifact = row.get("artifact", sources.get(source, {}).get("artifact"))
            if (original is None or original["label"] != row["label"] or artifact != original["artifact"]
                    or (source, row["finding_index"]) in seen):
                raise ValueError("required finding differs from original source")
            seen.add((source, row["finding_index"]))
            result.append({"key": row["key"], **original})
        return result
    except (HarnessError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise EvidenceError("worker 实际交付的必需问题证据损坏；保留证据，禁止推断清单", stop=stops.make("evidence", "runtime_integrity")) from error


def claim_gaps(result, delivered, snapshot):
    """Mechanical repair-claim gaps; claims never close a finding."""
    claims = result.get("repair_claims")
    if delivered and claims is None:
        return [{"code": "repair_claim_missing", "key": row["key"], "review_dispatch_id": row["review_dispatch_id"],
                 "finding_index": row["finding_index"]} for row in delivered]
    failures = []
    for key, claim in (claims or {}).items():
        for path in claim["evidence_paths"]:
            parts = PurePosixPath(path).parts
            if (not parts or PurePosixPath(path).is_absolute() or ".." in parts
                    or any(git.protected_name(p) for p in parts)
                    or path not in snapshot.get("files", {})):
                failures.append({"code": "repair_evidence_outside_candidate", "key": key, "path": path})
    return failures


def disposition_gaps(review, required):
    """Required keys left undisposed, or, for an approval, not resolved."""
    dispositions = review.get("finding_dispositions") or {}
    return [{"code": "finding_not_independently_resolved", "key": row["key"],
             "review_dispatch_id": row["review_dispatch_id"], "finding_index": row["finding_index"]}
            for row in required
            if row["key"] not in dispositions
            or (review["verdict"] == "approve" and dispositions[row["key"]]["status"] != "resolved")]


def claims_for_review(required, delivered, claims):
    """Re-key the worker's claims onto this review's keys by source and position."""
    worker = {(row["review_dispatch_id"], row["finding_index"]): row["key"] for row in delivered}
    result = {}
    for row in required:
        key = worker.get((row["review_dispatch_id"], row["finding_index"]))
        if key is not None and key in (claims or {}):
            result[row["key"]] = deepcopy(claims[key])
    return result


@stops.reads_evidence
def validate_review_source(run, task_id, record, *, files, kind="review", candidate_digest=None):
    """Authenticate the original returned body before trusting ledger copies."""
    source = record.get("review_source")
    if not isinstance(source, dict) or not all(source.get(k) for k in
            ("dispatch_id", "artifact", "artifact_digest", "snapshot_digest")):
        raise HarnessError("审阅缺少原始来源绑定；禁止批准或遗忘阻断问题")
    ident = source["dispatch_id"]
    entries = [d for d in run.get("dispatches", []) if d.get("id") == ident]
    if (len(entries) != 1 or entries[0].get("role") != "reviewer"
            or entries[0].get("kind") != kind or entries[0].get("task_id") != task_id
            or entries[0].get("snapshot_digest") != source["snapshot_digest"]
            or source["snapshot_digest"] != record.get("snapshot_digest")
            or (candidate_digest is not None and source["snapshot_digest"] != candidate_digest)):
        raise HarnessError("审阅来源身份或候选绑定无效")
    result, _ = bound_result(files.under("dispatches", ident, "returned-result.json"), source, "审阅历史")
    if (not isinstance(result, dict) or result.get("task_id") != task_id or result.get("dispatch_id") != ident
            or {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")} != record.get("review")):
        raise HarnessError("审阅台账与原始结果不同")
    return source


def bind_review_source(context, kind, task_id, review, candidate_digest):
    key = f"reviewer:{kind}:{task_id}"
    receipt = context.run.get("review_receipts", {}).get(key)
    source = dict(receipt) if isinstance(receipt, dict) else None
    if source is not None:
        from pathlib import Path
        expected = Path(context.directory) / "dispatches" / source.get("dispatch_id", "") / "returned-result.json"
        if source.get("artifact") != str(expected):
            raise HarnessError("审阅来源不属于当前运行目录")
    record = {"review": review, "snapshot_digest": candidate_digest, "review_source": source}
    validate_review_source(context.run, task_id, record, files=run_files(context), kind=kind, candidate_digest=candidate_digest)
    return {key: source[key] for key in ("dispatch_id", "artifact", "artifact_digest", "snapshot_digest")}
