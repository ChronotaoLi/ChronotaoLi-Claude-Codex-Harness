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

from . import git, stops
from .context import prompt_json
from .recovery import bound_bytes, snapshot_digest
from .review_progress import dispositions_usable, entries
from .runtime_evidence import read_or_write
from .state_files import StateFiles, StateReadError
from .util import EvidenceError, HarnessError, dump


def recent_costs(context, *, limit=64):
    """Project measured formal commands from the owned in-memory run ledger.

    Nested worker tool time is deliberately excluded: it is already included in
    dispatch wall time. Samples inform planning only, never gate reuse.
    """
    samples = []
    for entry in reversed(context.run.get("dispatches", [])):
        duration = entry.get("duration_seconds")
        if (entry.get("role") != "verification" or not entry.get("command")
                or not isinstance(duration, (int, float)) or isinstance(duration, bool)
                or not math.isfinite(duration) or duration < 0
                or not entry.get("exit_confirmed") or not entry.get("evidence_complete")):
            continue
        samples.append({"command": deepcopy(entry["command"]), "seconds": duration,
                        "source": "measured_formal_command",
                        "provenance": {"dispatch_id": entry["id"],
                            "finished_at": entry.get("finished_at"),
                            "scope": entry.get("timing_scope")}})
        if len(samples) >= limit:
            break
    return samples


def _cost_estimate(command, samples):
    # Only the same command, directory included, estimates a check's time.
    for sample in samples or ():
        seconds = sample.get("seconds")
        if (sample.get("command") == command and sample.get("source") == "measured_formal_command"
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
        for index, command in enumerate(commands):
            identity = f"{responsibility}:{index}"
            checks.append({"id": identity, "responsibility": responsibility, "command": deepcopy(command),
                           "after": [previous] if previous else [],
                           "obligation_refs": [{"task_id": o["task_id"], "acceptance_index": o["acceptance_index"]}
                                               for o in obligations] if responsibility == "task_gate" else [],
                           "cost_estimate": _cost_estimate(command, cost_samples),
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
    # What a plan entry does and does not prove is shared protocol (RULES).
    result["check_defaults"] = {"cost_estimate": {"seconds": None, "source": "unknown"},
                                "after": [], "obligation_refs": []}
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
                        prior_findings=(), worker_files=None):
    """Return mechanical gaps before expensive gates; never returns acceptance.

    The caller supplies the already-owned candidate snapshot and original worker
    artifact. An owned worker_files handle binds the registered artifact to its
    dispatch. Required evidence files come from the task's evidence_requirements:
    each must be a regular, non-symlink file inside the candidate snapshot, and
    its SHA256 is computed here; the worker declares nothing.
    """
    failures, checked = [], []
    digest = snapshot_digest(candidate_snapshot)
    if snapshot_digest(current_snapshot) != digest:
        failures.append({"code": "candidate_changed"})
    source = Path(worker_artifact)
    reference = {"artifact": str(worker_artifact), "artifact_digest": worker_artifact_digest}
    try:
        if worker_files is None:
            path = source.absolute()
            files = StateFiles(path.parent).under(path.name)
            raw, _ = bound_bytes(files, {**reference, "artifact": str(path)}, "worker")
        else:
            raw, _ = bound_bytes(worker_files, reference, "worker")
    except (EvidenceError, StateReadError, ValueError) as error:
        if worker_files is not None:
            raise
        failures.append({"code": "worker_source_invalid", "detail": str(error)})
    else:
        try:
            if worker_files is None:
                saved = json.loads(raw)
            else:
                with stops.evidence_reads(source=reference):
                    saved = json.loads(raw)
            # Adapter artifacts contain the validated response, sometimes wrapped.
            response = saved.get("response", saved) if isinstance(saved, dict) else saved
            if response != worker_result:
                raise ValueError("worker declarations differ from source")
            if worker_dispatch_id is not None and worker_result.get("dispatch_id") != worker_dispatch_id:
                raise ValueError("worker dispatch mismatch")
            if worker_result.get("task_id") != task["id"]:
                raise ValueError("worker task mismatch")
        except (ValueError, TypeError) as error:
            failures.append({"code": "worker_source_invalid", "detail": str(error)})
    for requirement in task.get("evidence_requirements", []):
        identity, expected = requirement["id"], requirement["path"]
        path = PurePosixPath(expected)
        failure = {"code": "evidence_invalid", "requirement_id": identity, "path": expected}
        if (path.is_absolute() or ".." in path.parts or not path.parts
                or any(git.protected_name(p) for p in path.parts)):
            failures.append({**failure, "reason": "unsafe_path"})
            continue
        root = Path(worktree)
        file = root / path
        try:
            # The plan owns the path and kind; the runtime computes the digest.
            if any((root / Path(*path.parts[:depth])).is_symlink() for depth in range(1, len(path.parts) + 1)):
                failures.append({**failure, "reason": "symlink"})
                continue
            if not file.exists():
                failures.append({**failure, "reason": "missing"})
                continue
            if not _regular(file, root):
                failures.append({**failure, "reason": "not_regular_file"})
                continue
            actual = hashlib.sha256(file.read_bytes()).hexdigest()
            snapshot_value = current_snapshot.get("files", {}).get(str(path), "")
            if not snapshot_value.endswith(":" + actual) or snapshot_value.startswith(("link:", "deleted:")):
                failures.append({**failure, "reason": "outside_candidate"})
                continue
            checked.append({"requirement_id": identity, "path": str(path), "sha256": actual,
                            "kind": requirement["kind"], "acceptance_index": requirement["acceptance_index"]})
        except OSError as error:
            failures.append({**failure, "reason": "unreadable", "detail": str(error)})
    return {"version": 1, "passed": not failures, "candidate_digest": digest,
            "worker_source": {"artifact": str(source), "sha256": worker_artifact_digest},
            "evidence": checked, "failures": failures,
            "semantic_coverage": "requires independent review"}


def finding_progress(run, task_ids, *, whole_run=False, limit=60):
    """History projection only; a worker claim or an absent finding never closes it.

    Explicit reviewer dispositions remain scoped to their recorded candidate.
    Missing source identity stays unknown rather than inventing a global ID.
    It indexes each finding's latest status, entry by entry as review_progress
    walks them, over exactly the histories its caller verified: the named tasks'
    and, for a run-level view, the whole-run entries, approved batches included,
    after every task's. No task name widens it.
    """
    task_ids = list(task_ids)
    scopes = [(tid, run.get("review_history", {}).get(tid, []), False) for tid in task_ids]
    if whole_run and run.get("final_history"):
        scopes.append(("final", run["final_history"], True))
        task_ids.append("final")
    rows = {}
    for tid, records, whole in scopes:
        for record, _ in entries(records, whole):
            review = record.get("review") or {}
            source = record.get("review_source") or {}
            dispatch = source.get("dispatch_id") or record.get("dispatch_id") or review.get("dispatch_id")
            artifact = source.get("artifact") or record.get("artifact")
            candidate = source.get("snapshot_digest") or record.get("snapshot_digest")
            for index, finding in enumerate(review.get("findings", [])):
                key = (tid, dispatch, index, artifact if dispatch is None else None)
                rows[key] = {"task_id": tid, "review_dispatch_id": dispatch, "finding_index": index,
                    "label": finding.get("id"),
                    "severity": finding["severity"], "message": finding.get("message", ""),
                    "artifact": artifact, "source_artifact_digest": source.get("artifact_digest"),
                    "snapshot_digest": candidate,
                    "status": "open" if dispatch else "unknown", "disposition_source": None}
            usable = dispositions_usable(record)
            mapping = {item.get("key"): (item.get("review_dispatch_id"), item.get("finding_index"))
                       for item in record.get("required_findings") or [] if isinstance(item, dict)}
            for name, disposition in (review.get("finding_dispositions") or {}).items():
                # The repair status rule of review_progress: not_checked records no judgment,
                # open from any review is a kept veto, only a usable review resolves.
                if (name not in mapping or disposition.get("status") == "not_checked"
                        or (disposition.get("status") == "resolved" and not usable)):
                    continue
                row = rows.get((tid, *mapping[name], None))
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
            "history_task_ids": task_ids}


def finding_progress_packet(run, task_ids, directory, *, whole_run=False, files=None):
    """Retrieve the complete progress matrix through immutable private evidence.

    Callers authenticate original review sources before projecting this index,
    and name the histories they authenticated. This summary is input context,
    never authority to close a finding.
    """
    progress = finding_progress(run, task_ids, whole_run=whole_run, limit=None)
    if not progress["findings"]:
        return None
    value = {"version": 1, "progress": progress}
    raw = (dump(value) + "\n").encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    folder = Path(directory) / "finding-progress" / "indexes"
    artifact = folder / (digest + ".json")
    if files is None:
        if not folder.is_absolute():
            raise HarnessError("finding progress index path is not absolute")
        files = StateFiles.nofollow_directory(directory)
    try:
        actual = read_or_write(files, ("finding-progress", "indexes", artifact.name), artifact, value)
    except OSError as error:
        raise HarnessError("cannot write finding progress index",
                           stop=stops.make("runtime_failure", "io", data={"error_type": type(error).__name__})) from error
    with stops.evidence_reads(source={"artifact": str(artifact)}):
        if actual != raw:
            raise HarnessError("finding progress index changed; preserve original evidence")
    status_counts, severity_counts = {}, {}
    for row in progress["findings"]:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        severity_counts[row["severity"]] = severity_counts.get(row["severity"], 0) + 1
    return {"count": len(progress["findings"]), "status_counts": status_counts,
            "severity_counts": severity_counts, "history_task_ids": progress["history_task_ids"],
            "index": {"artifact": str(artifact), "artifact_digest": digest}}


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
    # How to read a row and its source table is shared protocol (RULES).
    result = {**packet, 'required_findings': rows, 'required_finding_sources': sources}
    # Small packets need no reference scaffolding. Keep the inline form when
    # adding a source table would cost more context.
    return result if len(prompt_json(result)) < len(prompt_json(packet)) else packet
