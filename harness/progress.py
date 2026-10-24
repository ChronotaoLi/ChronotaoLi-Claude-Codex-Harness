"""Bound worker evidence for fresh dispatches; never an acceptance checkpoint."""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from .contracts import WORK, envelope, validate_structure, validate_work_semantics
from .util import HarnessError, write_json

CHECKPOINT_POLICY = (
    "Checkpoints are untrusted worker evidence awaiting independent verification and review, never accepted work. "
    "Continue remaining work from the task and feedback; no checkpoint skips verification or review. "
    "Content freshness is suitable only for immutable file computations and must bind every input. "
    "External device, service and network facts must be volatile and rechecked before side effects. "
    "Runtime checks only declared file hashes and time, not completeness or correctness. "
    "Volatile age starts conservatively at source worker invocation, never artifact recording time; "
    "missing or invalid invocation timestamps make volatile evidence stale. "
    "Read raw evidence and bound source results before relying on summaries. No tool permissions are added. "
    "Summaries and inline file lists may be compacted explicitly. Retrieve the full declaration, original paths, "
    "input SHA256 values and raw evidence references from the bound manifest artifact; input failure indices "
    "refer to its ordered inputs list. Runtime status reflects every declared input even when details are omitted."
)
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
_RESERVED = {".git", ".harness", ".codex", ".claude"}


def validate_checkpoints(items):
    if not isinstance(items, list) or len(items) > 16:
        raise HarnessError("checkpoints must be an array of at most 16 entries")
    ids = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "summary", "inputs", "evidence", "freshness", "max_age_seconds"}:
            raise HarnessError("invalid checkpoint fields")
        if not isinstance(item["id"], str) or not _SAFE.fullmatch(item["id"]) or item["id"] in ids:
            raise HarnessError("invalid or duplicate checkpoint id")
        ids.add(item["id"])
        if not isinstance(item["summary"], str) or not item["summary"].strip() or len(item["summary"]) > 4000 or "\0" in item["summary"]:
            raise HarnessError("invalid checkpoint summary")
        age = item["max_age_seconds"]
        if type(age) is not int or not 0 <= age <= 86400 or item["freshness"] not in ("content", "volatile"):
            raise HarnessError("invalid checkpoint freshness")
        if item["freshness"] == "content" and (age != 0 or not item["inputs"]):
            raise HarnessError("content checkpoint needs inputs and zero max_age_seconds")
        for field in ("inputs", "evidence"):
            refs = item[field]
            if not isinstance(refs, list) or len(refs) > 8 or field == "evidence" and not refs:
                raise HarnessError("checkpoint file lists must have at most 8 entries and nonempty evidence")
            for ref in refs:
                if (not isinstance(ref, dict) or set(ref) != {"path", "sha256"}
                        or not isinstance(ref["path"], str) or not ref["path"] or len(ref["path"]) > 4096
                        or "\0" in ref["path"] or not isinstance(ref["sha256"], str) or not _SHA.fullmatch(ref["sha256"])):
                    raise HarnessError("invalid checkpoint file reference")
    return items


def _regular_file(path):
    path = Path(path)
    # Reject symlinks in every component, including dangling links.
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise HarnessError(f"checkpoint path is not a regular non-symlink file: {path}")
    return path


def _file_digest(path):
    path = _regular_file(path)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
    except OSError as error:
        raise HarnessError(f"cannot read checkpoint file: {path}") from error
    return digest.hexdigest()


def _read(path, limit=None):
    path = _regular_file(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read() if limit is None else stream.read(limit + 1)
    except OSError as error:
        raise HarnessError(f"cannot read checkpoint file: {path}") from error
    if limit is not None and len(raw) > limit:
        raise HarnessError("checkpoint evidence exceeds 1 MiB")
    return raw


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _valid_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class ProgressRecovery:
    def _checkpoint_path(self, path):
        source = Path(path)
        try:
            checked = source.relative_to(self.worktree) if source.is_absolute() else source
        except ValueError:
            checked = source
        if ".." in checked.parts or any(p in _RESERVED for p in checked.parts):
            raise HarnessError("checkpoint source uses reserved or unsafe path")
        return source if source.is_absolute() else Path(self.worktree) / source

    def _checkpoint_source(self, task_id, dispatch_id, source):
        expected = self.directory / "dispatches" / dispatch_id / "returned-result.json"
        if (not isinstance(source, dict) or source.get("artifact") != str(expected)
                or not isinstance(source.get("artifact_digest"), str) or not _SHA.fullmatch(source["artifact_digest"])):
            raise HarnessError("checkpoint requires bound original result SHA256")
        raw = _read(expected)
        if _digest(raw) != source["artifact_digest"]:
            raise HarnessError("checkpoint original result changed")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise HarnessError("checkpoint original result is invalid JSON") from error
        if not isinstance(result, dict) or result.get("task_id") != task_id or result.get("dispatch_id") != dispatch_id:
            raise HarnessError("checkpoint source identity mismatch")
        if not any(d["id"] == dispatch_id and d["role"] == "worker" and d["task_id"] == task_id for d in self.run["dispatches"]):
            raise HarnessError("checkpoint source is not a known worker dispatch")
        validate_structure(result, envelope(WORK, task_id, dispatch_id))
        validate_work_semantics(result)
        validate_checkpoints(result.get("checkpoints", []))
        return result

    def record_checkpoints(self, task_id, dispatch_id, result, source):
        items = validate_checkpoints(result.get("checkpoints", []))
        if not items:
            return
        original = self._checkpoint_source(task_id, dispatch_id, source)
        if {k: v for k, v in original.items() if k not in ("task_id", "dispatch_id")} != result:
            raise HarnessError("checkpoint result differs from bound original")
        self.validate_checkpoint_links()
        existing = [r for r in self.run.get("task_checkpoints", []) if r["dispatch_id"] == dispatch_id]
        if existing:
            if len(existing) != len(items):
                raise HarnessError("checkpoint dispatch ledger is incomplete")
            return
        prepared, total = [], 0
        folder = self.directory / "checkpoints" / dispatch_id
        for item in items:
            artifact = folder / (item["id"] + ".json")
            if artifact.exists():
                try:
                    record = json.loads(_read(artifact))
                except (ValueError, UnicodeError) as error:
                    raise HarnessError("checkpoint recovery artifact corrupt") from error
                if (record.get("checkpoint") != item or record.get("source") != source
                        or record.get("task_id") != task_id or record.get("dispatch_id") != dispatch_id):
                    raise HarnessError("checkpoint recovery artifact conflict")
                self._validate_checkpoint_record(record, folder)
                total += sum(len(_read(ref["artifact"], 1024 * 1024)) for ref in record["evidence"])
                if total > 16 * 1024 * 1024:
                    raise HarnessError("checkpoint batch exceeds 16 MiB")
            else:
                for ref in item["inputs"]:
                    if _file_digest(self._checkpoint_path(ref["path"])) != ref["sha256"]:
                        raise HarnessError("checkpoint declared input SHA256 mismatch")
                evidence = []
                for index, ref in enumerate(item["evidence"]):
                    raw = _read(self._checkpoint_path(ref["path"]), 1024 * 1024)
                    total += len(raw)
                    if total > 16 * 1024 * 1024:
                        raise HarnessError("checkpoint batch exceeds 16 MiB")
                    if _digest(raw) != ref["sha256"]:
                        raise HarnessError("checkpoint declared evidence SHA256 mismatch")
                    target = folder / f"{item['id']}-{index}.bin"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        if _read(target) != raw:
                            raise HarnessError("checkpoint evidence recovery conflict")
                    else:
                        with target.open("xb") as stream:
                            stream.write(raw)
                            stream.flush()
                            os.fsync(stream.fileno())
                    evidence.append({**ref, "artifact": str(target), "artifact_digest": _digest(raw)})
                recorded_at = time.time()
                dispatch = next(d for d in self.run["dispatches"] if d["id"] == dispatch_id)
                origin = dispatch.get("invoked_at")
                if not _valid_time(origin) or origin > recorded_at:
                    origin = None
                record = {"task_id": task_id, "dispatch_id": dispatch_id, "checkpoint": deepcopy(item),
                          "source": deepcopy(source), "recorded_at": recorded_at,
                          "freshness_origin": origin, "evidence": evidence}
                write_json(artifact, record)
            prepared.append({"task_id": task_id, "dispatch_id": dispatch_id, "checkpoint_id": item["id"],
                             "artifact": str(artifact), "artifact_digest": _digest(_read(artifact))})
        for row in prepared:
            self.event("task_checkpoint_recorded", **row)
        self.run.setdefault("task_checkpoints", []).extend(prepared)
        for field in ("decision_requests", "operator_decisions", "environment_updates", "environment_checks"):
            self.run.setdefault(field, [])
        self.run["schema_version"] = max(10, self.run["schema_version"])
        self.save()

    def _validate_checkpoint_record(self, record, folder):
        item = record.get("checkpoint")
        validate_checkpoints([item])
        if type(record.get("recorded_at")) not in (int, float) or not 0 <= record["recorded_at"] < float("inf"):
            raise HarnessError("checkpoint timestamp invalid")
        original = self._checkpoint_source(record["task_id"], record["dispatch_id"], record.get("source"))
        origin = record.get("freshness_origin")
        if origin is not None:
            dispatch = next(d for d in self.run["dispatches"] if d["id"] == record["dispatch_id"])
            invoked_at = dispatch.get("invoked_at")
            if (not _valid_time(origin) or not _valid_time(invoked_at) or origin != invoked_at
                    or origin > record["recorded_at"]):
                raise HarnessError("checkpoint freshness origin differs from source invocation")
        if item not in original.get("checkpoints", []):
            raise HarnessError("checkpoint declaration not bound to source")
        evidence = record.get("evidence")
        if not isinstance(evidence, list) or len(evidence) != len(item["evidence"]):
            raise HarnessError("checkpoint evidence references invalid")
        for index, (ref, declared) in enumerate(zip(evidence, item["evidence"])):
            expected = folder / f"{item['id']}-{index}.bin"
            if (ref != {**declared, "artifact": str(expected), "artifact_digest": declared["sha256"]}
                    or _digest(_read(expected, 1024 * 1024)) != declared["sha256"]):
                raise HarnessError("checkpoint cached evidence changed")

    def validate_checkpoint_links(self):
        rows = self.run.get("task_checkpoints", [])
        if not isinstance(rows, list) or rows and self.run["schema_version"] < 10:
            raise HarnessError("invalid checkpoint ledger schema")
        seen = set()
        tasks = {t["id"] for t in self.run["tasks"]}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"task_id", "dispatch_id", "checkpoint_id", "artifact", "artifact_digest"}:
                raise HarnessError("invalid checkpoint ledger row")
            if not all(isinstance(row[k], str) for k in row) or not _SAFE.fullmatch(row["dispatch_id"]) or not _SAFE.fullmatch(row["checkpoint_id"]):
                raise HarnessError("invalid checkpoint ledger identity")
            key = row["dispatch_id"], row["checkpoint_id"]
            folder = self.directory / "checkpoints" / row["dispatch_id"]
            if key in seen or row["task_id"] not in tasks or row["artifact"] != str(folder / (row["checkpoint_id"] + ".json")):
                raise HarnessError("checkpoint ledger reference invalid")
            seen.add(key)
            raw = _read(row["artifact"])
            if _digest(raw) != row["artifact_digest"]:
                raise HarnessError("checkpoint manifest changed")
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeError) as error:
                raise HarnessError("checkpoint manifest invalid JSON") from error
            if not isinstance(record, dict) or record.get("task_id") != row["task_id"] or record.get("dispatch_id") != row["dispatch_id"] or record.get("checkpoint", {}).get("id") != row["checkpoint_id"]:
                raise HarnessError("checkpoint manifest identity mismatch")
            self._validate_checkpoint_record(record, folder)

    def progress_packet(self, packet, *, task_id=None, role=None, kind=None):
        self.validate_checkpoint_links()
        rows = self.run.get("task_checkpoints", [])
        if not rows:
            return packet
        task = packet.get("task", {})
        task_id = task_id or packet.get("task_id") or (task.get("id") if isinstance(task, dict) else None)
        trigger = packet.get("trigger")
        if task_id in (None, "replanning") and isinstance(trigger, dict):
            task_id = trigger.get("task_id")
        relevant = {t["id"] for t in packet.get("ready", [])} if kind == "dispatch" or "ready" in packet else None
        if relevant is None and task_id and task_id not in ("final", "dispatching", "replanning"):
            relevant = {task_id}
        latest = {(r["task_id"], r["checkpoint_id"]): r for r in rows}
        current, history = [], []
        relevant_count = sum(1 for (tid, _), row in latest.items() if relevant is None or tid in relevant)
        summary_limit = min(500, 4000 // max(1, relevant_count))
        inline_limit = 4000 // max(1, relevant_count)
        for row in rows:
            selected = latest[row["task_id"], row["checkpoint_id"]] == row
            if relevant is None or row["task_id"] in relevant:
                history.append({**row, "latest": selected})
            if not selected or relevant is not None and row["task_id"] not in relevant:
                continue
            record = json.loads(_read(row["artifact"]))
            item = record["checkpoint"]
            reasons, inputs, changed_indices = [], [], []
            for index, ref in enumerate(item["inputs"]):
                try:
                    actual = _file_digest(self._checkpoint_path(ref["path"]))
                except HarnessError:
                    actual = None
                inputs.append({**ref, "current_sha256": actual})
                if actual != ref["sha256"]:
                    changed_indices.append(index)
            if changed_indices:
                reasons.append("declared inputs changed or missing at indices: " + ",".join(map(str, changed_indices)))
            origin, now = record.get("freshness_origin"), time.time()
            if item["freshness"] == "volatile":
                if not _valid_time(origin) or origin > now:
                    reasons.append("volatile evidence has no valid source invocation time")
                elif item["max_age_seconds"] == 0 or now - origin >= item["max_age_seconds"]:
                    reasons.append("volatile evidence expired")
            entry = {**row, "id": item["id"], "summary": item["summary"][:summary_limit],
                     "summary_truncated": len(item["summary"]) > summary_limit, "freshness": item["freshness"],
                     "max_age_seconds": item["max_age_seconds"], "recorded_at": record["recorded_at"],
                     "freshness_origin": record.get("freshness_origin"),
                     "status": "stale" if reasons else "reusable", "reasons": reasons,
                     "input_count": len(inputs), "evidence_count": len(record["evidence"]),
                     "changed_input_indices": changed_indices, "source": record["source"]}
            details = {"inputs": inputs, "evidence": record["evidence"]}
            if len(json.dumps(details, ensure_ascii=True)) <= inline_limit:
                entry.update(details, file_details_omitted=False)
            else:
                # Every original path/hash and copied raw-output reference stays
                # in the SHA-bound manifest. Keep all validity reasons in view.
                entry["file_details_omitted"] = True
            current.append(entry)
        return {**packet, "task_checkpoints": current, "checkpoint_history": history, "checkpoint_policy": CHECKPOINT_POLICY}
