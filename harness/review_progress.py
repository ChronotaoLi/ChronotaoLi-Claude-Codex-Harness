"""Runtime-keyed required findings, repair claims and dispositions; never inferred closure.

A finding's identity is (source review dispatch, position in that review's
findings); its model-written id is only a label. Each dispatch receives keys
f1..fN for its required findings, and the key-to-source mapping is saved before
the dispatch, so a model never writes an identity the runtime already owns.
"""
from copy import deepcopy
from pathlib import PurePosixPath

from .util import HarnessError


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


def _keyed(issues):
    return [{"key": f"f{number}", **row} for number, row in enumerate(issues.values(), 1)]


def required_findings(run, task_id, candidate_digest, *, whole_run=False, records=None):
    """Open blocking findings for one candidate, keyed f1..fN in history order.

    A usable record's resolved dispositions close findings only on the candidate
    it reviewed, and only through its saved copy of the mapping its dispatch
    received. That copy must equal, row by row, the list recomputed from the same
    history prefix and candidate; any difference stops instead of guessing.
    """
    issues = {}
    if records is None:
        records = run.get("final_history", []) if whole_run else run.get("review_history", {}).get(task_id, [])
    for record in records:
        if not record.get("review"):
            if record.get("validation_error"):
                raise HarnessError("历史审阅格式无效；必须保留并核对原始阻断证据")
            continue
        validate_review_source(run, task_id, record, kind="final_review" if whole_run else "review")
        dispositions = record["review"].get("finding_dispositions")
        closed = []
        if (dispositions and record.get("snapshot_digest") == candidate_digest
                and dispositions_usable(record)):
            copy = record.get("required_findings")
            if copy != _keyed(issues) or set(dispositions) != {row["key"] for row in copy}:
                raise HarnessError("审阅处置与键对照不符；保留证据")
            mapping = {row["key"]: (row["review_dispatch_id"], row["finding_index"]) for row in copy}
            closed = [mapping[key] for key, row in dispositions.items() if row["status"] == "resolved"]
        for row in _blocking_requirements(record):
            issues[(row["review_dispatch_id"], row["finding_index"])] = row
        for reference in closed:
            issues.pop(reference, None)
    if len(issues) > 60:
        raise HarnessError("待核验阻断问题超过单次审阅容量；保留全部来源，需调整任务计划")
    return _keyed(issues)


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
        if not isinstance(rows, list) or len(rows) > 60:
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
                validate_review_source(context.run, task_id, matches[0])
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
        raise HarnessError("worker 实际交付的必需问题证据损坏；保留证据，禁止推断清单") from error


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
                    or any(p in (".git", ".harness", ".codex", ".claude") for p in parts)
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


def active_review_findings(run, kind, task_id):
    """The saved key mapping of the review currently in progress; never a default."""
    if kind == "review":
        pending = run.get("pending_task") or {}
        if pending.get("task_id") != task_id or pending.get("stage") != "reviewing" or "required_findings" not in pending:
            raise HarnessError("当前任务审阅缺少必需问题对照；保留证据")
        return pending["required_findings"]
    if kind != "final_review":
        raise HarnessError("不支持此类审阅修正")
    history = run.get("final_history") or []
    record = history[-1] if history else {}
    if record.get("status") != "running" or record.get("phase") != "reviewing" or "required_findings" not in record:
        raise HarnessError("当前整体审阅缺少必需问题对照；保留证据")
    return record["required_findings"]


def validate_review_source(run, task_id, record, *, kind="review", candidate_digest=None):
    """Authenticate the original returned body before trusting ledger copies."""
    from pathlib import Path
    from .recovery import read_bound_result
    source = record.get("review_source")
    if not isinstance(source, dict) or not all(source.get(k) for k in
            ("dispatch_id", "artifact", "artifact_digest", "snapshot_digest")):
        raise HarnessError("审阅缺少原始来源绑定；禁止批准或遗忘阻断问题")
    ident = source["dispatch_id"]
    entries = [d for d in run.get("dispatches", []) if d.get("id") == ident]
    path = Path(source["artifact"])
    if (len(entries) != 1 or entries[0].get("role") != "reviewer"
            or entries[0].get("kind") != kind or entries[0].get("task_id") != task_id
            or entries[0].get("snapshot_digest") != source["snapshot_digest"]
            or path.parts[-5:] != ("runs", run["id"], "dispatches", ident, "returned-result.json")
            or any(p.is_symlink() for p in (path, *path.parents))
            or source["snapshot_digest"] != record.get("snapshot_digest")
            or (candidate_digest is not None and source["snapshot_digest"] != candidate_digest)):
        raise HarnessError("审阅来源身份或候选绑定无效")
    result, _ = read_bound_result(source, "审阅历史")
    if (result.get("task_id") != task_id or result.get("dispatch_id") != ident
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
    validate_review_source(context.run, task_id, record, kind=kind, candidate_digest=candidate_digest)
    return {key: source[key] for key in ("dispatch_id", "artifact", "artifact_digest", "snapshot_digest")}
