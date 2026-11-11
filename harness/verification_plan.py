"""Finite verification responsibilities and deterministic candidate evidence checks.

Declarations describe obligations; they never authorize commands or establish
semantic coverage. Every accepted candidate still requires full runtime gates.
"""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from pathlib import Path, PurePosixPath

from .context import prompt_json
from .recovery import snapshot_digest
from .review_progress import finding_references, dispositions_usable
from .util import HarnessError, dump, write_json


def recent_costs(context, *, limit=64):
    """Project measured formal commands from the owned in-memory run ledger.

    Nested worker tool time is deliberately excluded: it is already included in
    dispatch wall time. Samples inform planning only, never gate reuse.
    """
    samples = []
    for entry in reversed(context.run.get("dispatches", [])):
        duration = entry.get("duration_seconds")
        if (entry.get("role") != "verification" or not entry.get("argv")
                or not isinstance(duration, (int, float)) or isinstance(duration, bool)
                or not math.isfinite(duration) or duration < 0
                or not entry.get("exit_confirmed") or not entry.get("evidence_complete")):
            continue
        samples.append({"argv": list(entry["argv"]), "seconds": duration,
                        "source": "measured_formal_command",
                        "provenance": {"dispatch_id": entry["id"],
                            "finished_at": entry.get("finished_at"),
                            "scope": entry.get("timing_scope")}})
        if len(samples) >= limit:
            break
    return samples


def _cost_estimate(argv, samples):
    for sample in samples or ():
        seconds = sample.get("seconds")
        if (sample.get("argv") == list(argv) and sample.get("source") == "measured_formal_command"
                and sample.get("provenance", {}).get("dispatch_id")
                and isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
                and math.isfinite(seconds) and seconds >= 0):
            return deepcopy({key: sample[key] for key in ("seconds", "source", "provenance")})
    return {"seconds": None, "source": "unknown"}


def build_verification_plan(config, task=None, *, cost_samples=None):
    verification = config["verification"]
    task = task or {}
    obligations = [{"task_id": task.get("id"), "acceptance_index": i, "text": text}
                   for i, text in enumerate(task.get("acceptance", []))]
    checks = []
    for responsibility, commands in (
        ("worker_self_check", task.get("self_checks", task.get("required_commands", []))),
        ("task_gate", verification["commands"]),
        ("final_setup", verification.get("setup_commands", []) if verification.get("fresh_checkout") else []),
        ("final_gate", verification["commands"]),
    ):
        previous = ("final_setup:" + str(len(verification.get("setup_commands", [])) - 1)
                    if responsibility == "final_gate" and verification.get("fresh_checkout")
                    and verification.get("setup_commands") else None)
        for index, argv in enumerate(commands):
            identity = f"{responsibility}:{index}"
            checks.append({"id": identity, "responsibility": responsibility, "argv": list(argv),
                           "after": [previous] if previous else [],
                           "obligation_refs": [{"task_id": o["task_id"], "acceptance_index": o["acceptance_index"]}
                                               for o in obligations] if responsibility == "task_gate" else [],
                           "cost_estimate": _cost_estimate(argv, cost_samples),
                           "applicability": "current candidate and environment only",
                           "mapping": "configured gate; semantic coverage requires independent review"})
            previous = identity
    return {"version": 1, "task_id": task.get("id"), "obligations": obligations,
            "evidence_requirements": deepcopy(task.get("evidence_requirements", [])), "checks": checks,
            "policy": {"order": "configured", "task_gates": "all", "final_gates": "all",
                       "worker_checks_replace_gates": False, "cache_gate_success": False,
                       "fresh_checkout": verification.get("fresh_checkout", False)}}


def verification_role_view(plan, role):
    result = deepcopy(plan)
    # Common applicability/cost facts are emitted once in role views.
    result["check_defaults"] = {"cost_estimate": {"seconds": None, "source": "unknown"},
                                "after": [], "obligation_refs": [],
                                "applicability": "current candidate/environment only",
                                "mapping": "independent review must establish semantic coverage"}
    for check in result["checks"]:
        for field in ("applicability", "mapping"):
            check.pop(field, None)
        if check.get("cost_estimate", {}).get("source") == "unknown":
            check.pop("cost_estimate", None)
        for field in ("after", "obligation_refs"):
            if not check.get(field):
                check.pop(field, None)
    result["obligations"] = [{"task_id": o["task_id"], "acceptance_index": o["acceptance_index"]}
                             for o in result["obligations"]]
    result["role"] = role
    if role != "reviewer":  # The reviewer skill carries the reviewer's coverage rule.
        result["instruction"] = (
            "Use authorized focused self-checks as needed. Runtime executes every task gate and every final gate; "
            "repeat a full suite only for an explicit diagnostic reason. Declarations grant no command permission."
            if role == "worker" else
            "Independently verify obligation coverage; evidence kinds and command mappings are declarations, not proof.")
    return result


def _regular(path, boundary=None):
    current = path
    while current != current.parent and current != boundary:
        if current.is_symlink():
            return False
        if boundary is None:
            break
        current = current.parent
    return path.is_file()


def candidate_preflight(task, worker_result, *, worktree, candidate_snapshot, current_snapshot,
                        worker_artifact, worker_artifact_digest, worker_dispatch_id=None,
                        prior_findings=()):
    """Return mechanical gaps before expensive gates; never returns acceptance.

    The caller supplies the already-owned candidate snapshot and original worker
    artifact, not paths selected by the worker. Source bytes and declaration
    identity are rechecked here, alongside ordinary file hashes.
    """
    failures, checked = [], []
    digest = snapshot_digest(candidate_snapshot)
    if snapshot_digest(current_snapshot) != digest:
        failures.append({"code": "candidate_changed"})
    source = Path(worker_artifact)
    try:
        if not _regular(source):
            raise ValueError("worker source is not an ordinary file")
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != worker_artifact_digest:
            raise ValueError("worker source hash mismatch")
        saved = json.loads(raw)
        # Adapter artifacts contain the validated response, sometimes wrapped.
        response = saved.get("response", saved) if isinstance(saved, dict) else saved
        if response != worker_result:
            raise ValueError("worker declarations differ from source")
        if worker_dispatch_id is not None and worker_result.get("dispatch_id") != worker_dispatch_id:
            raise ValueError("worker dispatch mismatch")
        if worker_result.get("task_id") != task["id"]:
            raise ValueError("worker task mismatch")
    except (OSError, ValueError, TypeError) as error:
        failures.append({"code": "worker_source_invalid", "detail": str(error)})
    declarations = worker_result.get("evidence_files", [])
    requirements = task.get("evidence_requirements", [])
    seen = set()
    for requirement in requirements:
        identity = requirement["id"]
        rows = [row for row in declarations if row.get("requirement_id") == identity]
        if identity in seen or len(rows) != 1:
            failures.append({"code": "evidence_reference_missing_or_duplicate", "requirement_id": identity})
            continue
        seen.add(identity)
        row = rows[0]
        path = PurePosixPath(row["path"])
        expected = requirement["path"]
        if (path.is_absolute() or ".." in path.parts or not path.parts
                or any(p in {".git", ".harness", ".codex", ".claude"} for p in path.parts)
                or row["path"] != expected or row["kind"] != requirement["kind"]):
            failures.append({"code": "evidence_binding_mismatch", "requirement_id": identity})
            continue
        file = Path(worktree) / path
        try:
            if not _regular(file, Path(worktree)):
                raise ValueError("evidence is not an ordinary file")
            actual = hashlib.sha256(file.read_bytes()).hexdigest()
            if actual != row["sha256"]:
                raise ValueError("evidence hash mismatch")
            snapshot_value = current_snapshot.get("files", {}).get(str(path), "")
            if not snapshot_value.endswith(":" + actual) or snapshot_value.startswith(("link:", "deleted:")):
                raise ValueError("evidence is outside candidate snapshot")
            checked.append({"requirement_id": identity, "path": str(path), "sha256": actual,
                            "kind": row["kind"], "acceptance_index": requirement["acceptance_index"]})
        except (OSError, ValueError) as error:
            failures.append({"code": "evidence_invalid", "requirement_id": identity, "detail": str(error)})
    return {"version": 1, "passed": not failures, "candidate_digest": digest,
            "worker_source": {"artifact": str(source), "sha256": worker_artifact_digest},
            "evidence": checked, "failures": failures,
            "semantic_coverage": "requires independent review"}


def finding_progress(run, task_id, *, limit=60):
    """History projection only; a worker claim or an absent finding never closes it.

    Explicit reviewer dispositions remain scoped to their recorded candidate.
    Missing source identity stays unknown rather than inventing a global ID.
    """
    task_ids = [task_id] if task_id in run.get("review_history", {}) else (
        list(run.get("review_history", {})) if task_id in {"plan", "planning", "replanning", "dispatching", "final", "commander"} else [task_id])
    rows = {}
    for tid in task_ids:
        for record in run.get("review_history", {}).get(tid, []):
            review = record.get("review") or {}
            source = record.get("review_source") or {}
            dispatch = source.get("dispatch_id") or record.get("dispatch_id") or review.get("dispatch_id")
            artifact = source.get("artifact") or record.get("artifact")
            candidate = source.get("snapshot_digest") or record.get("snapshot_digest")
            for finding, reference in finding_references(review):
                key = (tid, dispatch, reference["finding_id"], artifact if dispatch is None else None)
                rows[key] = {"task_id": tid, "review_dispatch_id": dispatch, **reference,
                    "severity": finding["severity"], "message": finding.get("message", ""),
                    "artifact": artifact, "source_artifact_digest": source.get("artifact_digest"),
                    "snapshot_digest": candidate,
                    "status": "open" if dispatch else "unknown", "disposition_source": None}
            if not dispositions_usable(record):
                continue
            for disposition in review.get("finding_dispositions", []):
                key = (tid, disposition.get("review_dispatch_id"), disposition.get("finding_id"), None)
                row = rows.get(key)
                if row is not None:
                    row["status"] = disposition["status"]
                    row["disposition_source"] = {"review_dispatch_id": dispatch, "artifact": artifact,
                        "source_artifact_digest": source.get("artifact_digest"),
                        "snapshot_digest": candidate,
                        "evidence": deepcopy(disposition.get("evidence", []))}
    items = list(rows.values())
    selected = items if limit is None else items[-limit:] if limit > 0 else []
    return {"findings": selected,
            "omitted_count": len(items) - len(selected),
            "history_task_ids": task_ids,
            "policy": "History facts only. Missing findings and worker repair claims do not establish resolution. "
                      "Resolved dispositions apply only to their recorded candidate; recheck changed candidates. "
                      "If omitted_count is nonzero, retrieve full review_history before assessing issue coverage."}


def finding_progress_packet(run, task_id, directory):
    """Retrieve the complete progress matrix through immutable private evidence.

    Callers authenticate original review sources before projecting this index.
    This summary is input context, never authority to close a finding.
    """
    progress = finding_progress(run, task_id, limit=None)
    if not progress["findings"]:
        return None
    value = {"version": 1, "progress": progress}
    raw = (dump(value) + "\n").encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    folder = Path(directory) / "finding-progress" / "indexes"
    artifact = folder / (digest + ".json")
    if not folder.is_absolute() or any(p.is_symlink() for p in (artifact, *artifact.parents)):
        raise HarnessError("finding progress index path is a symlink or not absolute")
    try:
        if not artifact.exists():
            write_json(artifact, value)
        if not artifact.is_file() or artifact.read_bytes() != raw:
            raise HarnessError("finding progress index changed; preserve original evidence")
    except OSError as error:
        raise HarnessError("cannot read or write finding progress index") from error
    status_counts, severity_counts = {}, {}
    for row in progress["findings"]:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        severity_counts[row["severity"]] = severity_counts.get(row["severity"], 0) + 1
    return {"count": len(progress["findings"]), "status_counts": status_counts,
            "severity_counts": severity_counts, "history_task_ids": progress["history_task_ids"],
            "index": {"artifact": str(artifact), "artifact_digest": digest},
            "policy": progress["policy"] + " Read the SHA256-bound index for the complete ordered findings "
                      "matrix, original IDs, source artifacts and candidate-scoped dispositions before "
                      "acting on an issue. Summary counts do not prove resolution or acceptance."}


def project_finding_sources(packet):
    """Factor repeated source paths while preserving every required reference."""
    if not packet.get('required_findings'):
        return packet
    sources = deepcopy(packet.get('required_finding_sources', {}))
    rows = []
    for original in packet['required_findings']:
        row = deepcopy(original)
        dispatch = row['review_dispatch_id']
        artifact = row.pop('artifact', None)
        if artifact is not None:
            reference = {'artifact': artifact}
            if dispatch in sources and sources[dispatch] != reference:
                raise HarnessError('required finding source artifacts disagree')
            sources[dispatch] = reference
        if dispatch not in sources:
            raise HarnessError('required finding source artifact missing')
        rows.append(row)
    result = {**packet, 'required_findings': rows, 'required_finding_sources': sources,
              'required_finding_source_policy': 'Every required_findings row remains binding. Retrieve its original '
                  'review at required_finding_sources[review_dispatch_id].artifact; finding_id identifies the '
                  'required reference, and original_finding_id/finding_index identify the original position when present.'}
    if packet.get('repair_claims'):
        indexes = {(row['review_dispatch_id'], row['finding_id']): index for index, row in enumerate(rows)}
        claims = []
        for original in packet['repair_claims']:
            claim = deepcopy(original)
            key = claim.get('review_dispatch_id'), claim.get('finding_id')
            if key in indexes:
                claim.pop('review_dispatch_id')
                claim.pop('finding_id')
                claim['required_finding_index'] = indexes[key]
            claims.append(claim)
        result['repair_claims'] = claims
        result['required_finding_source_policy'] += (
            ' A repair_claim with required_finding_index uses the review_dispatch_id and finding_id of '
            'required_findings at that zero-based index; its summary and evidence_paths remain the worker claim.')
    # Small packets need no reference scaffolding. Keep the inline form when
    # adding a source table and reconstruction policy would cost more context.
    return result if len(prompt_json(result)) < len(prompt_json(packet)) else packet
