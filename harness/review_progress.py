"""Source-addressed repair claims and independent dispositions, never inferred closure."""
from pathlib import PurePosixPath
import hashlib
import re

from .util import HarnessError


def finding_references(review):
    """Map immutable original positions to unambiguous protocol references."""
    findings = review.get("findings", [])
    ids = [finding["id"] for finding in findings]
    use_positions = (len(set(ids)) != len(ids) or any(
        re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}", ident) is None
        for ident in ids))
    return [(finding, {"finding_id": f"finding-{index + 1}" if use_positions else finding["id"],
                       "original_finding_id": finding["id"], "finding_index": index})
            for index, finding in enumerate(findings)]


def dispositions_usable(record):
    """Rejected validation is evidence of issues, never authority to close them."""
    return (record.get("status") not in ("invalidated", "invalid_rejected")
            and not record.get("validation_error") and not record.get("disposition_errors"))


def _blocking_requirements(record):
    references = finding_references(record["review"])
    aliases = any(finding["id"] != reference["finding_id"] for finding, reference in references)
    return [{"review_dispatch_id": record["review_source"]["dispatch_id"],
             **(reference if aliases else {"finding_id": reference["finding_id"]}),
             "artifact": record["review_source"]["artifact"], "status": "open"}
            for finding, reference in references if finding.get("severity") == "blocking"]


def required_findings(run, task_id, candidate_digest, *, whole_run=False, records=None):
    issues = {}
    if records is None:
        records = run.get("final_history", []) if whole_run else run.get("review_history", {}).get(task_id, [])
    for record in records:
        if not record.get("review"):
            if record.get("validation_error"):
                raise HarnessError("历史审阅格式无效；必须保留并核对原始阻断证据")
            continue
        validate_review_source(run, task_id, record, kind="final_review" if whole_run else "review")
        review = record["review"]
        for row in _blocking_requirements(record):
            issues[(row["review_dispatch_id"], row["finding_id"])] = row
        for disposition in review.get("finding_dispositions", []):
            key = (disposition["review_dispatch_id"], disposition["finding_id"])
            if (key in issues and disposition["status"] == "resolved"
                    and record.get("snapshot_digest") == candidate_digest
                    and dispositions_usable(record)):
                issues.pop(key)
    if len(issues) > 60:
        raise HarnessError("待核验阻断问题超过单次审阅容量；保留全部来源，需调整任务计划")
    return list(issues.values())


def worker_findings_payload(task_id, dispatch_id, packet):
    """Build the delivered identities and a draft that makes no repair claim."""
    from copy import deepcopy
    rows = deepcopy(packet.get("required_findings", []))
    if len(rows) > 60:
        raise HarnessError("worker 必需问题超过单次审阅容量；保留来源并调整计划")
    sources = packet.get("required_finding_sources", {})
    for row in rows:
        if "artifact" not in row:
            source = sources.get(row["review_dispatch_id"], {})
            if not source.get("artifact"):
                raise HarnessError("worker 必需问题缺少原始来源")
            row["artifact"] = source["artifact"]
    value = {"dispatch_id": dispatch_id, "task_id": task_id, "required_findings": rows}
    if rows:
        value["repair_claims_template"] = [
            {"review_dispatch_id": row["review_dispatch_id"], "finding_id": row["finding_id"],
             "summary": "", "evidence_paths": []} for row in rows]
    return value


def worker_claim_template_reference(context, task_id, dispatch_id, packet):
    """Reference the exact sidecar that bind_worker_findings publishes before invoke."""
    from .util import dump
    value = worker_findings_payload(task_id, dispatch_id, packet)
    if not value["required_findings"]:
        return None
    artifact = context.directory / "dispatches" / dispatch_id / "worker-findings.json"
    return {"artifact": str(artifact),
            "artifact_digest": hashlib.sha256((dump(value) + "\n").encode("utf-8")).hexdigest(),
            "field": "repair_claims_template", "required_count": len(value["required_findings"]),
            "policy": "Read the bound draft; complete every identity once within output_constraints. "
                      "Empty fields prove no repair. evidence_paths name candidate-relative files; "
                      "external logs belong in evidence. Independent review decides resolution."}


def bind_worker_findings(context, entry, packet):
    """Save the exact delivered obligations, before hooks can change the candidate."""
    from .util import write_json
    artifact = context.directory / "dispatches" / entry["id"] / "worker-findings.json"
    value = worker_findings_payload(entry["task_id"], entry["id"], packet)
    write_json(artifact, value)
    entry["worker_findings"] = {"artifact": str(artifact),
        "artifact_digest": hashlib.sha256(artifact.read_bytes()).hexdigest()}


def delivered_worker_findings(context, task_id, dispatch_id):
    """Authenticate delivered requirements independently of the post-hook snapshot."""
    from .recovery import read_bound_result
    entries = [d for d in context.run.get("dispatches", []) if d.get("id") == dispatch_id]
    if (len(entries) != 1 or entries[0].get("role") != "worker"
            or entries[0].get("task_id") != task_id):
        raise HarnessError("worker 必需问题的派发身份无效；保留证据")
    binding = entries[0].get("worker_findings")
    expected = context.directory / "dispatches" / dispatch_id / "worker-findings.json"
    if (not isinstance(binding, dict) or binding.get("artifact") != str(expected)
            or not binding.get("artifact_digest")):
        raise HarnessError("worker 缺少实际交付的必需问题绑定；禁止推断清单，请保留证据后 abandon")
    try:
        if any(p.is_symlink() for p in (expected, *expected.parents)):
            raise ValueError("symbolic link")
        value, _ = read_bound_result(binding, "worker 必需问题")
        if (not isinstance(value, dict) or value.get("dispatch_id") != dispatch_id or value.get("task_id") != task_id):
            raise ValueError("dispatch or task binding changed")
        rows = value["required_findings"]
        if not isinstance(rows, list) or len(rows) > 60:
            raise ValueError("required findings capacity invalid")
        references = set()
        canonical_sources = {}
        records = context.run.get("review_history", {}).get(task_id, [])
        for row in rows:
            source = row["review_dispatch_id"]
            if source not in canonical_sources:
                matches = [r for r in records if (r.get("review_source") or {}).get("dispatch_id") == source]
                if len(matches) != 1:
                    raise ValueError("review source missing or ambiguous")
                validate_review_source(context.run, task_id, matches[0])
                canonical_sources[source] = _blocking_requirements(matches[0])
            key = (source, row["finding_id"])
            if row not in canonical_sources[source] or key in references:
                raise ValueError("required finding differs from original source")
            references.add(key)
        return rows
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HarnessError("worker 实际交付的必需问题证据损坏；保留证据，禁止推断清单") from error


def claim_gaps(result, required, snapshot, *, known=()):
    claims = {(item["review_dispatch_id"], item["finding_id"]): item
              for item in result.get("repair_claims", [])}
    references = {(item["review_dispatch_id"], item["finding_id"]) for item in required}
    known_references = references | {(item["review_dispatch_id"], item["finding_id"]) for item in known}
    failures = [{"code": "repair_claim_missing", "review_dispatch_id": source, "finding_id": finding}
                for source, finding in sorted(references - claims.keys())]
    for key, claim in claims.items():
        if key not in known_references:
            failures.append({"code": "repair_claim_unknown_source", "reference": list(key)})
        for path in claim["evidence_paths"]:
            parts = PurePosixPath(path).parts
            if (not parts or PurePosixPath(path).is_absolute() or ".." in parts
                    or any(p in (".git", ".harness", ".codex", ".claude") for p in parts)
                    or path not in snapshot.get("files", {})):
                failures.append({"code": "repair_evidence_outside_candidate", "path": path})
    return failures


def disposition_gaps(review, required):
    rows = {(item["review_dispatch_id"], item["finding_id"]): item
            for item in review.get("finding_dispositions", [])}
    refs = {(item["review_dispatch_id"], item["finding_id"]) for item in required}
    failures = [{"code": "finding_disposition_unknown", "reference": list(key)}
                for key in rows.keys() - refs]
    for key in refs:
        row = rows.get(key)
        if row is None or (review["verdict"] == "approve" and row["status"] != "resolved"):
            failures.append({"code": "finding_not_independently_resolved", "reference": list(key)})
    return failures


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
