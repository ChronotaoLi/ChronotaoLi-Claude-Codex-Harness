"""The operator's run boundary and a sound, incomplete pattern inclusion check.

Every pattern means what ``git.path_matches`` matches. Inclusion is proved by
three rules only; anything else is "cannot prove", never "outside".
"""
from __future__ import annotations

import hashlib
import json

from . import git
from .util import HarnessError, dump, strict_json_loads

WILDCARDS = frozenset("*?[")


def wild(segment):
    return any(char in WILDCARDS for char in segment)


def literal_subtree_prefix(pattern):
    """``p1/.../pk/**`` with literal prefix segments (k may be 0) -> (p1, ..., pk); else None."""
    parts = pattern.split("/")
    if parts[-1] != "**" or any(wild(part) for part in parts[:-1]):
        return None
    return tuple(parts[:-1])


def operator_entry(pattern):
    """Accept only a concrete path, a literal subtree or a standalone ``**``."""
    if not isinstance(pattern, str):
        raise HarnessError("--scope 必须是字符串")
    git.validate_scope_pattern(pattern)
    if not wild(pattern) or literal_subtree_prefix(pattern) is not None:
        return pattern
    raise HarnessError(f"--scope 只接受具体路径、字面子树 p/…/** 或 **：{pattern}")


def operator_entries(patterns):
    """Validate a whole batch; keep order and drop duplicates."""
    return list(dict.fromkeys(operator_entry(pattern) for pattern in patterns))


def contains(pattern, reusable):
    """Whether every path ``pattern`` matches is matched by some reusable pattern (sound, incomplete)."""
    if pattern in reusable:
        return True
    if not wild(pattern) and any(git.path_matches(pattern, other) for other in reusable):
        return True
    parts = pattern.split("/")
    for other in reusable:
        prefix = literal_subtree_prefix(other)
        if prefix is not None and len(parts) >= len(prefix) and tuple(parts[:len(prefix)]) == prefix:
            return True
    return False


def reusable_patterns(published_tasks, scope):
    """Patterns of the plan published before the revision, plus the operator boundary."""
    return list(dict.fromkeys([*(p for task in published_tasks for p in task["allowed_paths"]), *scope]))


def gaps(new_patterns, reusable):
    """{task_id: [patterns not proved inside]} for {task_id: [new patterns]}."""
    result = {}
    for task_id, patterns in new_patterns.items():
        missing = [p for p in dict.fromkeys(patterns) if not contains(p, reusable)]
        if missing:
            result[task_id] = missing
    return result


def proposal_request_id(run, receipt):
    """Identity of a returned decide proposal; independent of the boundary."""
    history = run["plan_history"]
    value = {"run_id": run["id"], "dispatch_id": receipt["dispatch_id"],
             "artifact_digest": receipt["artifact_digest"],
             "base_revision": len(history) - 1,
             "base_plan_digest": history[-1]["artifact_digest"] if history else None,
             "snapshot_digest": receipt["snapshot_digest"]}
    return "proposal_" + hashlib.sha256(dump(value).encode()).hexdigest()


SUMMARY_PATTERN_LIMIT = 512


def summary(run):
    """Read-only projection for status while the first plan awaits approval."""
    scope = run.get("scope", [])
    tasks, listed, omitted = [], 0, 0
    for task in run.get("tasks", []):
        patterns = task.get("allowed_paths", [])
        groups = {}
        for pattern in patterns:
            if listed >= SUMMARY_PATTERN_LIMIT:
                omitted += 1
                continue
            listed += 1
            top = pattern.split("/", 1)[0] if "/" in pattern else "."
            groups.setdefault(top, []).append(
                {"pattern": pattern, "in_scope": contains(pattern, scope) if scope else None})
        tasks.append({"task_id": task.get("id"), "concrete": sum(1 for p in patterns if not wild(p)),
                      "wildcard": sum(1 for p in patterns if wild(p)), "groups": groups})
    return {"scope": list(scope), "declared": bool(scope), "tasks": tasks, "patterns_omitted": omitted}


def suggest_entry(pattern):
    """The narrowest operator entry this rule set proves covers ``pattern`` (may be wider)."""
    if not wild(pattern) or literal_subtree_prefix(pattern) is not None:
        return pattern
    prefix = []
    for part in pattern.split("/"):
        if part == "**" or wild(part):
            break
        prefix.append(part)
    return "/".join([*prefix, "**"])


def suggestions(gap_map):
    """Operator entries covering every gap: (entries, widened), minimal under ``contains``."""
    entries = list(dict.fromkeys(suggest_entry(p) for patterns in gap_map.values() for p in patterns))
    entries = [e for e in entries if not any(other != e and contains(e, [other]) for other in entries)]
    widened = [e for e in entries if not any(p == e for patterns in gap_map.values() for p in patterns)]
    return entries, widened


def scope_args(entries):
    """Shell words for ``resume``: always ``--scope=VALUE`` so a leading '-' stays a value."""
    import shlex
    return " ".join("--scope=" + shlex.quote(entry) for entry in entries)


def proposal_gaps(run, result):
    """Gaps of a returned decide original under the current boundary; pure (no I/O)."""
    from .contracts import apply_revision, validate_plan
    if not run["tasks"]:
        tasks = validate_plan({"tasks": result.get("new_tasks")})
        return gaps({t["id"]: t["allowed_paths"] for t in tasks}, list(run["scope"])) if run["scope"] else {}
    missing = {}
    apply_revision(run["tasks"], result, run["accepted"], scope=run["scope"], gaps=missing)
    return missing


def held_view(run, reader, *, run_id):
    """Project the judged proposal from the original in this observer's held root."""
    from . import observe, provider_failures

    if not isinstance(run.get("scope"), list):
        return None
    receipts = run.get("commander_receipts")
    receipt = receipts.get("commander:decide:run") if isinstance(receipts, dict) else None
    if not isinstance(receipt, dict) or "artifact_digest" not in receipt:
        return None
    dispatch_id = receipt.get("dispatch_id")
    entry = next((d for d in run.get("dispatches", []) if isinstance(d, dict) and d.get("id") == dispatch_id), None)
    if (not entry or (entry.get("role"), entry.get("kind"), entry.get("status")) != ("commander", "decide", "completed")
            or "proposal_rejection" in entry):
        return None
    try:
        if not observe.canonical_uuid(dispatch_id):
            raise HarnessError("台账记录的指挥派发 ID 无效")
        observe.validate_run_id(run_id)
        if run.get("id") != run_id:
            raise HarnessError("运行台账 ID 与观察目录不符")
        components = ("runs", run_id, "dispatches", dispatch_id, "returned-result.json")
        artifact = receipt.get("artifact")
        if not isinstance(artifact, str) or tuple(artifact.rsplit("/", 5)[-5:]) != components:
            raise HarnessError("台账记录的原件路径不在该 run 的派发目录")
        original = reader.read(components, limit=provider_failures.LIMIT)
        if original["too_large"]:
            raise HarnessError("指挥原始结果超过 16 MiB")
        raw = original["data"]
        if hashlib.sha256(raw).hexdigest() != receipt["artifact_digest"]:
            raise HarnessError("指挥原始结果摘要与台账不符")
        result = strict_json_loads(raw)
        if not isinstance(result, dict):
            raise HarnessError("指挥原始结果不是 JSON 对象")
        if result.get("action") != "revise":
            return None
        missing = proposal_gaps(run, result)
        entries, widened = suggestions(missing)
        return {"request_id": proposal_request_id(run, receipt), "kind": "revision" if run["tasks"] else "initial_plan",
                "dispatch_id": receipt["dispatch_id"], "gaps": missing, "suggested_scope": entries, "widened": widened}
    except Exception as error:
        return {"error": str(error)[:500]}

def declared(patterns):
    """One ``--scope`` batch: operator entries in order without duplicates; refused whole, naming each refused entry."""
    from . import stops
    refused = []
    for pattern in patterns:
        try:
            operator_entry(pattern)
        except HarnessError:
            refused.append(pattern if isinstance(pattern, str) else repr(pattern))
    if refused:
        raise HarnessError("--scope 只接受具体路径、字面子树 p/…/** 或 **，且不得以受保护根开头："
                           + json.dumps(refused, ensure_ascii=False),
                           stop=stops.make("invalid_input", "scope_pattern", data={"option": "--scope", "patterns": refused}))
    return operator_entries(patterns)
