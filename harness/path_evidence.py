"""Bound path-list inputs without discarding their complete, immutable evidence."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .util import HarnessError, dump, write_json


INLINE_PATH_CHARS = 4000
PATH_EVIDENCE_POLICY = (
    "A path list may be inline or a {path_index: SHA256} reference. Resolve references "
    "through path_evidence_indexes; each artifact contains the complete ordered paths array. "
    "Use bounded reads/searches to locate relevant entries, fetch more when needed, and verify artifact_digest. "
    "Counts and references are not excerpts or proof of acceptance. These indexes enumerate "
    "paths only: the original diff, snapshot, recovery source and instruction-file trust rules "
    "still govern file contents and provenance. An identical index does not mean identical file contents."
)


def _folder(directory):
    folder = Path(directory) / "path-indexes"
    if not folder.is_absolute() or any(p.is_symlink() for p in (folder, *folder.parents)):
        raise HarnessError(f"path evidence index directory is a symlink: {ascii(str(folder))}")
    return folder


def _write_index(directory, paths):
    value = {"version": 1, "paths": paths}
    raw = (dump(value) + "\n").encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    artifact = _folder(directory) / (digest + ".json")
    try:
        if not artifact.exists() and not artifact.is_symlink():
            write_json(artifact, value)
        if artifact.is_symlink() or artifact.read_bytes() != raw:
            raise HarnessError(f"path evidence index changed: {ascii(str(artifact))}; preserve the original evidence")
    except OSError as error:
        raise HarnessError(f"cannot read or write path evidence index: {ascii(str(artifact))}") from error
    return {"artifact": str(artifact), "artifact_digest": digest, "count": len(paths)}


def validate_indexes(directory, references):
    """Verify durable references without recreating missing or damaged evidence."""
    if not isinstance(references, list):
        raise HarnessError("invalid path evidence references")
    for ref in references:
        if not isinstance(ref, dict) or set(ref) != {"artifact", "artifact_digest", "count"}:
            raise HarnessError("invalid path evidence reference")
        digest = ref["artifact_digest"]
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
                or type(ref["count"]) is not int or ref["count"] < 0):
            raise HarnessError("invalid path evidence digest or count")
        artifact = _folder(directory) / (digest + ".json")
        if ref["artifact"] != str(artifact) or artifact.is_symlink():
            raise HarnessError(
                f"path evidence index reference changed: expected {ascii(str(artifact))}, "
                f"actual {ascii(ref['artifact'])}")
        try:
            raw = artifact.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise HarnessError(f"path evidence index changed: {ascii(str(artifact))}")
            value = json.loads(raw)
        except (OSError, ValueError, UnicodeError) as error:
            raise HarnessError(f"path evidence index missing or unreadable: {ascii(str(artifact))}") from error
        if (not isinstance(value, dict) or set(value) != {"version", "paths"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["paths"], list)
                or any(not isinstance(p, str) for p in value["paths"])
                or len(value["paths"]) != ref["count"]):
            raise HarnessError(f"path evidence index content disagrees with reference: {ascii(str(artifact))}")


def validate_dispatch_indexes(directory, dispatches):
    references, seen = [], set()
    for entry in dispatches:
        if "path_evidence" in entry:
            if not isinstance(entry["path_evidence"], list):
                raise HarnessError("invalid path evidence references")
            for ref in entry["path_evidence"]:
                identity = dump(ref)
                if identity not in seen:
                    references.append(ref)
                    seen.add(identity)
    validate_indexes(directory, references)


def project_paths(packet, directory):
    """Project generated path evidence, never acceptance/scope or caller-owned state.

    The allowance is shared across current and nested recovery lists. Lists retain
    their ordering and exact strings in content-addressed artifacts; identical
    lists share an index while their original parent/source fields remain intact.
    """
    result = deepcopy(packet)
    indexes = result.get("path_evidence_indexes", {})
    if not isinstance(indexes, dict) or any(
            not isinstance(ref, dict) or key != ref.get("artifact_digest") for key, ref in indexes.items()):
        raise HarnessError("invalid path evidence index map")
    validate_indexes(directory, list(indexes.values()))
    lists = []

    def collect(value, parent_key=None):
        if isinstance(value, dict):
            for key, child in value.items():
                is_paths = key == "changed_files" or (parent_key == "instruction_file_changes" and key == "paths")
                if is_paths:
                    if isinstance(child, dict) and set(child) == {"path_index"}:
                        if not isinstance(child["path_index"], str) or child["path_index"] not in indexes:
                            raise HarnessError("unresolved path evidence index")
                    elif isinstance(child, list) and all(isinstance(p, str) for p in child):
                        lists.append((len(dump(child)), value, key, child))
                    else:
                        raise HarnessError("invalid generated path evidence list")
                else:
                    collect(child, key)
        elif isinstance(value, list):
            for child in value:
                collect(child, parent_key)

    collect(result)
    remaining = sum(size for size, *_ in lists)
    for size, parent, key, paths in sorted(lists, key=lambda item: item[0], reverse=True):
        if remaining <= INLINE_PATH_CHARS:
            break
        reference = _write_index(directory, paths)
        digest = reference["artifact_digest"]
        indexes[digest] = reference
        parent[key] = {"path_index": digest}
        remaining -= size
    if indexes:
        result["path_evidence_indexes"] = indexes
        result["path_evidence_policy"] = PATH_EVIDENCE_POLICY
    return result
