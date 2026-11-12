"""Small, closed JSON-schema subset; every returned field is validated locally."""
import json
import os
import re
import unicodedata
from copy import deepcopy
from pathlib import PurePosixPath

from . import git
from .config import validate_commands
from .util import HarnessError


def obj(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


TEXT = {"type": "string"}
NONBLANK = {"type": "string", "minLength": 1}
TASK_ID = {"type": "string", "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$"}
SKILL_NAME = {"type": "string", "pattern": r"^[a-z0-9-]+$"}
DEPENDENCIES = {"type": "array", "items": TASK_ID}
OBLIGATIONS = {"type": "array", "items": NONBLANK, "minItems": 1}
SAFE_ID = {"type": "string", "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$"}
DETAIL = {"type": "string", "minLength": 1, "maxLength": 4000}
# The worker's own summary only bounds ledger size: feedback projections excerpt
# it with a digest-bound reference to the full artifact. DETAIL stays unchanged
# for blockers, evidence and findings.
WORK_SUMMARY = {"type": "string", "minLength": 1, "maxLength": 32000}
EVIDENCE = {"type": "array", "items": DETAIL, "maxItems": 20}
PROPOSAL = obj(key=SAFE_ID,
               abstract={"type": "string", "minLength": 1, "maxLength": 500},
               content={"type": "string", "minLength": 1, "maxLength": 8000},
               source_paths={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 500},
                             "minItems": 1, "maxItems": 10, "uniqueItems": True})
PROPOSALS = {"type": "array", "items": PROPOSAL, "maxItems": 8}
TASK = obj(id=TASK_ID, title=NONBLANK, description=NONBLANK, depends_on=DEPENDENCIES,
           allowed_paths=OBLIGATIONS, acceptance=OBLIGATIONS,
           skills={"type": "array", "items": SKILL_NAME})
# Optional for existing plans. These are prerequisites, never command grants.
REQUIRED_COMMANDS = {"type": "array", "items": {"type": "array", "items": TEXT, "minItems": 1},
                     "maxItems": 32, "uniqueItems": True}
TASK["properties"]["required_commands"] = REQUIRED_COMMANDS
TASK["properties"]["self_checks"] = REQUIRED_COMMANDS
EVIDENCE_KIND = {"type": "string", "enum": ["static", "simulation", "host_integration", "device"]}
TASK["properties"]["evidence_requirements"] = {
    "type": "array", "maxItems": 32, "items": obj(
        id=SAFE_ID, acceptance_index={"type": "integer", "minimum": 0},
        kind=EVIDENCE_KIND, path={"type": "string", "minLength": 1, "maxLength": 4096})}
PLAN = obj(tasks={"type": "array", "items": TASK, "minItems": 1})
TASK_UPDATE = obj(id=TASK_ID, description=NONBLANK, add_depends_on=DEPENDENCIES)
# The one commander call. Every field is required; a field the action does not
# use is an empty string or array. Branch rules are local (validate_decision),
# never a provider-side union.
OPTIONAL_TEXT = {"type": "string", "maxLength": 4000}
DECIDE = obj(action={"type": "string", "enum": ["implement", "revise", "ask"]},
             selected_task={"type": "string", "maxLength": 64}, instruction=OPTIONAL_TEXT,
             reason=OPTIONAL_TEXT, new_tasks={"type": "array", "items": TASK},
             updates={"type": "array", "items": TASK_UPDATE})
# "final" is the whole-run decision target and final-review task; "run" is the
# decide call's task ID. Neither may name a planned task.
RESERVED_TASK_IDS = ("final", "run")
WORK = obj(status={"type": "string", "enum": ["completed", "blocked"]},
           summary=WORK_SUMMARY, evidence=EVIDENCE, blockers=EVIDENCE,
           blocker_kind={"type": "string", "enum": ["none", "task", "environment", "operator", "continuation"]},
           proposed_memories=PROPOSALS)
FILE_EVIDENCE = obj(path={"type": "string", "minLength": 1, "maxLength": 4096},
                    sha256={"type": "string", "pattern": r"^[0-9a-f]{64}$"})
CHECKPOINT = obj(id=SAFE_ID, summary=DETAIL,
                 inputs={"type": "array", "items": FILE_EVIDENCE, "maxItems": 8},
                 evidence={"type": "array", "items": FILE_EVIDENCE, "minItems": 1, "maxItems": 8},
                 freshness={"type": "string", "enum": ["content", "volatile"]},
                 max_age_seconds={"type": "integer", "minimum": 0, "maximum": 86400})
# Optional extensions preserve old returned artifacts and simple worker clients.
# A handoff question gates another task; it is not a blocker of this deliverable.
WORK["properties"].update(
    checkpoints={"type": "array", "items": CHECKPOINT, "maxItems": 16},
    decision_requests={"type": "array", "items": obj(id=SAFE_ID, task_id=TASK_ID, question=DETAIL), "maxItems": 16},
    continuation_request=obj(kind={"type": "string", "enum": ["commands", "budget"]},
        commands=REQUIRED_COMMANDS,
        minimum_timeout_seconds={"type": "integer", "minimum": 0, "maximum": 86400}, reason=DETAIL),
    # Required for completed results (validate_work_semantics); a blocked result
    # never commits, so its value is ignored by every check (work_core_value).
    commit_message=obj(subject={"type": "string", "minLength": 1, "maxLength": 100},
                       body={"type": "string", "maxLength": 4000}),
)
# Lines Git would read as the runtime's own trailers, in any case or spacing.
TRAILER_LINE = re.compile(r"^[ \t]*harness-(run|task)[ \t]*:", re.IGNORECASE | re.MULTILINE)
REPAIR_CLAIM = obj(summary=DETAIL, evidence_paths={"type": "array", "items": NONBLANK, "maxItems": 20})
FINDING = obj(id=SAFE_ID,
              severity={"type": "string", "enum": ["blocking", "advisory"]},
              category={"type": "string", "enum": ["defect", "evidence", "scope", "preference"]},
              location=DETAIL, message=DETAIL, evidence=EVIDENCE)
REVIEW = obj(verdict={"type": "string", "enum": ["approve", "request_changes"]},
             summary=DETAIL, findings={"type": "array", "items": FINDING, "maxItems": 30},
             approved_memory_keys={"type": "array", "items": SAFE_ID, "maxItems": 8, "uniqueItems": True})
DISPOSITION = obj(status={"type": "string", "enum": ["resolved", "open", "not_checked"]}, evidence=EVIDENCE)
# Runtime-keyed fields: one runtime-generated key per required finding of this
# dispatch, so a model can neither add, omit, repeat nor misattribute an entry.
KEYED_ITEMS = {"finding_dispositions": DISPOSITION, "repair_claims": REPAIR_CLAIM}


def fixed_keys(rows, item):
    """An object whose properties are exactly the rows' keys, all required."""
    keys = [row["key"] for row in rows]
    return {"type": "object", "properties": {key: deepcopy(item) for key in keys},
            "required": keys, "additionalProperties": False}


def with_keys(schema, field, rows):
    """Copy a contract and add its runtime-keyed field, absent when nothing is required."""
    result = deepcopy(schema)
    if rows:
        result["properties"][field] = fixed_keys(rows, KEYED_ITEMS[field])
    return result


def review_schema(rows):
    """A review contract for this dispatch: with required findings, every key must be disposed."""
    result = with_keys(REVIEW, "finding_dispositions", rows)
    if rows:
        result["required"] = [*result["required"], "finding_dispositions"]
    return result


# Optional products are checked on their own: an invalid entry is discarded and
# recorded, it never decides whether the whole output is a result (R8).
WORK_OPTIONAL = ("checkpoints", "proposed_memories")
REVIEW_OPTIONAL = ("approved_memory_keys",)


def core_schema(schema, optional):
    """A contract copy without its optional products."""
    result = deepcopy(schema)
    for field in optional:
        result["properties"].pop(field, None)
    result["required"] = [field for field in result["required"] if field not in optional]
    return result


def core_value(value, optional):
    """An output copy without its optional products; non-objects are returned unchanged."""
    return {k: v for k, v in value.items() if k not in optional} if isinstance(value, dict) else value


def work_core_value(result):
    """The worker result every check sees: no optional products, and no commit message when blocked."""
    core = core_value(result, WORK_OPTIONAL)
    if isinstance(core, dict) and core.get("status") == "blocked":
        core.pop("commit_message", None)
    return core


def envelope(schema, task_id, dispatch_id):
    result = obj(task_id={"type": "string", "enum": [task_id]},
                 dispatch_id={"type": "string", "enum": [dispatch_id]}, **schema["properties"])
    result["required"] = ["task_id", "dispatch_id", *schema["required"]]
    return result


class ScopeExtensionRequired(HarnessError):
    """A revision's only defect is new scope; operator authority decides, never a redo."""

    def __init__(self, patterns):
        self.patterns = list(patterns)
        super().__init__("新增任务必须复用既有文件范围或操作者显式批准的范围，不得自行扩大范围："
                         + json.dumps(self.patterns, ensure_ascii=False))


def output_constraints(schema, path="response"):
    """Derive the local constraints omitted from the providers' schema subset.

    An object whose properties (at least two) share one schema, such as a
    runtime-keyed field, contributes a single ``.*`` path.
    """
    fields = ("minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems", "minimum", "maximum")
    bounds = {key: deepcopy(schema[key]) for key in fields if key in schema}
    if schema.get("minLength", 0):
        bounds["non_blank"] = True
    result = [{"path": path, **bounds}] if bounds else []
    values = list(schema.get("properties", {}).values())
    if len(values) >= 2 and all(value == values[0] for value in values[1:]):
        result.extend(output_constraints(values[0], f"{path}.*"))
    else:
        for key, field in schema.get("properties", {}).items():
            result.extend(output_constraints(field, f"{path}.{key}"))
    if "items" in schema:
        result.extend(output_constraints(schema["items"], f"{path}[]"))
    return result


def review_output_constraints(schema, proposed_keys=()):
    """Expose review semantics alongside the schema's ordinary format bounds.

    ``subsetOf`` lists all permitted array values. The conditional rule applies
    independently to every finding, using JSON Schema's if/then structure.
    These input constraints do not extend either provider's wire schema.
    """
    result = output_constraints(schema)
    by_path = {constraint["path"]: constraint for constraint in result}
    approved = by_path["response.approved_memory_keys"]
    approved["subsetOf"] = list(dict.fromkeys(proposed_keys))
    if not approved["subsetOf"]:
        approved["maxItems"] = 0
    categories = schema["properties"]["findings"]["items"]["properties"]["category"]["enum"]
    result.append({
        "path": "response.findings[]",
        "if": {"properties": {"severity": {"enum": ["blocking"]}}},
        "then": {"properties": {
            "evidence": {"minItems": 1},
            "category": {"enum": [category for category in categories if category != "preference"]},
        }},
    })
    return result


def validate_structure(value, schema, path="response"):
    """Check types, enums and exact object fields; bounds are checked separately."""
    expected = {"object": dict, "array": list, "string": str, "integer": int}[schema["type"]]
    if not isinstance(value, expected) or (expected is int and type(value) is not int):
        raise HarnessError(f"{path}: expected {schema['type']}")
    if "enum" in schema and value not in schema["enum"]:
        raise HarnessError(f"{path}: value not in permitted set")
    if expected is dict:
        if not set(schema["required"]) <= set(value) or set(value) - set(schema["properties"]):
            raise HarnessError(f"{path}: missing or unexpected fields")
        for key, field in schema["properties"].items():
            if key in value:
                validate_structure(value[key], field, f"{path}.{key}")
    if expected is list:
        for i, item in enumerate(value):
            validate_structure(item, schema["items"], f"{path}[{i}]")


def _validate_format(value, schema, path):
    if schema["type"] == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise HarnessError(f"{path}: string length exceeds permitted bounds")
        if schema.get("minLength", 0) and not value.strip():
            raise HarnessError(f"{path}: string must not be blank")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise HarnessError(f"{path}: invalid identifier")
    if schema["type"] == "object":
        for key, field in schema["properties"].items():
            if key in value:
                _validate_format(value[key], field, f"{path}.{key}")
    if schema["type"] == "integer":
        if not schema.get("minimum", value) <= value <= schema.get("maximum", value):
            raise HarnessError(f"{path}: integer exceeds permitted bounds")
    if schema["type"] == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise HarnessError(f"{path}: array length exceeds permitted bounds")
        if schema.get("uniqueItems") and any(item in value[:i] for i, item in enumerate(value)):
            raise HarnessError(f"{path}: duplicate entries")
        for i, item in enumerate(value):
            _validate_format(item, schema["items"], f"{path}[{i}]")


def validate(value, schema, path="response"):
    validate_structure(value, schema, path)
    _validate_format(value, schema, path)


def validate_commit_message(message, state_root):
    """The deterministic part of a completed result's commit message; the reviewer judges the rest."""
    roots = {str(state_root), os.path.realpath(state_root)}
    for field, allowed in (("subject", ""), ("body", "\n\t")):
        text = message[field]
        if any(unicodedata.category(char) in ("Cc", "Cs") and char not in allowed for char in text):
            raise HarnessError(f"commit_message.{field} 含不允许的控制字符或代理码点")
        if TRAILER_LINE.search(text):
            raise HarnessError(f"commit_message.{field} 不得含 Harness-Run／Harness-Task 行")
        if any(root in text for root in roots):
            raise HarnessError(f"commit_message.{field} 不得含运行状态目录路径")


def validate_work_semantics(result, state_root):
    """Check worker completion, blocker and continuation consistency after structural validation."""
    completed = result["status"] == "completed"
    if completed:
        if "commit_message" not in result:
            raise HarnessError("completed 结果必须有 commit_message")
        validate_commit_message(result["commit_message"], state_root)
    if completed != (result["blocker_kind"] == "none") or completed != (not result["blockers"]):
        raise HarnessError("worker 状态、blocker_kind 与 blockers 不一致")
    continuation = result.get("continuation_request")
    if (result["blocker_kind"] == "continuation") != (continuation is not None):
        raise HarnessError("续作请求必须使用 blocked/continuation，且必须提供结构化请求")
    if continuation is not None:
        validate_commands(continuation["commands"], "continuation_request.commands")
        if ((continuation["kind"] == "commands" and (not continuation["commands"] or continuation["minimum_timeout_seconds"] != 0))
                or (continuation["kind"] == "budget" and (continuation["commands"] or continuation["minimum_timeout_seconds"] <= 0))):
            raise HarnessError("续作请求必须明确缺少的精确命令或所需的派发时间预算")
    # Optional checkpoints and memory proposals are checked on their own
    # (progress.record_checkpoints, memory.validate_proposals): an invalid entry
    # or batch is discarded and must not erase completion or a blocker.
    # Handoffs have no effect on a blocked result. Keep their original bytes
    # for source validation, but never let their semantics erase the blocker.
    # Structure and format are still checked by the caller for every result.
    if completed:
        ids = [item["id"] for item in result.get("decision_requests", [])]
        if len(ids) != len(set(ids)):
            raise HarnessError("worker decision_requests 的 id 必须唯一")


def validate_review_semantics(result):
    """Blocking findings need evidence; resolved dispositions need verification evidence."""
    for finding in result["findings"]:
        if finding["severity"] == "blocking":
            if finding["category"] == "preference" or not finding["evidence"]:
                raise HarnessError("阻断 finding 必须有证据，且不能仅为偏好")
    validate_finding_dispositions(result)


def validate_finding_dispositions(result):
    if any(r["status"] == "resolved" and not r["evidence"] for r in (result.get("finding_dispositions") or {}).values()):
        raise HarnessError("解决问题的审阅处置必须包含核验依据")


def validate_review(result, schema=REVIEW):
    """The whole review contract except the optional memory approvals."""
    core, contract = core_value(result, REVIEW_OPTIONAL), core_schema(schema, REVIEW_OPTIONAL)
    validate_structure(core, contract)
    validate_review_semantics(core)
    _validate_format(core, contract, "response")
    return result


def review_approved(result):
    return result["verdict"] == "approve" and not any(
        finding["severity"] == "blocking" for finding in result["findings"])


def review_veto(result):
    """A structurally valid review vetoes when it rejects, blocks or leaves a required finding unresolved."""
    return (not review_approved(result)
            or any(item["status"] != "resolved" for item in (result.get("finding_dispositions") or {}).values()))


def validate_work_core(result, schema, state_root):
    """Structure and semantics of a worker result's core (work_core_value)."""
    core = work_core_value(result)
    validate_structure(core, core_schema(schema, WORK_OPTIONAL))
    validate_work_semantics(core, state_root)


def validate_work_bounds(result, schema):
    """Length, count, pattern and range bounds of a worker result's core (work_core_value)."""
    _validate_format(work_core_value(result), core_schema(schema, WORK_OPTIONAL), "response")


class Judgment:
    """One delivery's verdict: valid, veto (binding despite the invalid output) or invalid.

    ``readable`` says whether the core structure and semantics held, so the
    original output can still be routed as written.
    """

    def __init__(self, state, error=None, route=None, readable=True):
        self.state, self.error, self.route, self.readable = state, error, route, readable


def _envelope_matches(result, schema):
    """A structurally invalid output may still veto only under the dispatch's own identity."""
    properties = schema.get("properties", {})
    return all(field not in properties or result.get(field) in properties[field].get("enum", [])
               for field in ("task_id", "dispatch_id"))


def judge_review(result, schema):
    """Classify a returned review. A veto needs a structurally valid core review."""
    core, contract = core_value(result, REVIEW_OPTIONAL), core_schema(schema, REVIEW_OPTIONAL)
    try:
        validate_structure(core, contract)
    except HarnessError as error:
        return Judgment("invalid", error, readable=False)
    try:
        validate_review(result, schema)
    except HarnessError as error:
        return Judgment("veto" if review_veto(result) else "invalid", error)
    return Judgment("valid")


def judge_work(result, schema, state_root):
    """Classify a sealed worker result; handoff targets are checked later, at publication.

    A blocked result keeps its veto: a readable environment, operator or task
    blocker keeps its route; any other blocked output is routed as a task blocker.
    """
    try:
        validate_work_core(result, schema, state_root)
    except HarnessError as error:
        if isinstance(result, dict) and result.get("status") == "blocked" and _envelope_matches(result, schema):
            return Judgment("veto", error, "task", readable=False)
        return Judgment("invalid", error, readable=False)
    try:
        validate_work_bounds(result, schema)
    except HarnessError as error:
        if result["status"] == "blocked":
            route = result["blocker_kind"] if result["blocker_kind"] in ("environment", "operator", "task") else "task"
            return Judgment("veto", error, route)
        return Judgment("invalid", error)
    return Judgment("valid")


def validate_required_commands(task):
    result = []
    for field in ("required_commands", "self_checks"):
        commands = task.get(field, [])
        validate_structure(commands, REQUIRED_COMMANDS, "task." + field)
        validate_commands(commands, "task." + field)
        _validate_format(commands, REQUIRED_COMMANDS, "task." + field)
        result.extend(argv for argv in commands if argv not in result)
    return result


def validate_plan(plan):
    validate_structure(plan, PLAN)
    tasks = plan["tasks"]
    ids = [t["id"] for t in tasks]
    if not tasks or len(set(ids)) != len(ids):
        raise HarnessError("计划必须至少有 1 个任务，且 ID 唯一")
    reserved = sorted(set(ids) & set(RESERVED_TASK_IDS))
    if reserved:
        raise HarnessError(f"任务 ID 不得使用保留名：{reserved}")
    for task in tasks:
        commands = task.get("required_commands", [])
        validate_commands(commands, "task.required_commands")
        # Changing prerequisites is a plan correction, not a text format repair.
        if len(commands) > REQUIRED_COMMANDS["maxItems"]:
            raise HarnessError(f"任务 {task['id']} 的 required_commands 最多 {REQUIRED_COMMANDS['maxItems']} 条")
        if len({tuple(argv) for argv in commands}) != len(commands):
            raise HarnessError(f"任务 {task['id']} 的 required_commands 不得重复")
        validate_required_commands(task)
        requirement_ids = set()
        for requirement in task.get("evidence_requirements", []):
            path = requirement["path"]
            parts = PurePosixPath(path).parts
            if (requirement["id"] in requirement_ids
                    or not 0 <= requirement["acceptance_index"] < len(task["acceptance"])
                    or not parts or PurePosixPath(path).is_absolute() or ".." in parts
                    or path != str(PurePosixPath(path)) or "\\" in path
                    or any(p in (".git", ".harness", ".codex", ".claude") for p in parts)):
                raise HarnessError("任务证据要求必须有唯一 ID、有效验收索引和普通相对文件路径")
            requirement_ids.add(requirement["id"])
        if not re.fullmatch(TASK_ID["pattern"], task["id"]):
            raise HarnessError("任务 ID 只能含字母、数字、下划线和连字符")
        if not task["description"].strip() or not task["title"].strip():
            raise HarnessError("任务标题和描述不能为空")
        if not task["acceptance"] or not task["allowed_paths"]:
            raise HarnessError("每个任务必须指定验收条件和文件范围")
        if any(not criterion.strip() for criterion in task["acceptance"]):
            raise HarnessError("验收条件不能为空白")
        for path in task["allowed_paths"]:
            if not path.strip() or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
                raise HarnessError(f"无效文件范围：{path}")
            git.validate_scope_pattern(path)
        if any(d not in ids or d == task["id"] for d in task["depends_on"]):
            raise HarnessError("任务依赖不存在或依赖自身")
        if any(not re.fullmatch(SKILL_NAME["pattern"], s) for s in task["skills"]):
            raise HarnessError("无效 skill 名称")
    done = set()
    while len(done) < len(tasks):
        ready = {t["id"] for t in tasks if set(t["depends_on"]) <= done} - done
        if not ready:
            raise HarnessError("任务依赖包含环")
        done |= ready
    _validate_format(plan, PLAN, "response")
    return tasks


def apply_revision(current_tasks, result, accepted_ids, *, additional_allowed_paths=(), scope_violations=None):
    """Apply only explicit additions and brief updates to runtime-owned tasks.

    ``result`` carries ``new_tasks`` and ``updates`` (a decide payload). New scope
    raises ScopeExtensionRequired after every other check, or, when a list is
    given, is collected there so the caller can finish its own checks first.
    """
    # Only these two fields are operations; their shape is checked here too, so a
    # replay of a saved original cannot apply fields the contract never allowed.
    for field in ("new_tasks", "updates"):
        validate_structure(result.get(field), DECIDE["properties"][field], f"response.{field}")
    validate_plan({"tasks": current_tasks})
    accepted = set(accepted_ids)
    current = {task["id"]: task for task in current_tasks}
    if not accepted <= current.keys():
        raise HarnessError("修订引用未知已接受任务")
    proposed = deepcopy(current_tasks)
    by_id = {task["id"]: task for task in proposed}
    updated = set()
    for update in result["updates"]:
        ident = update["id"]
        if ident not in current or ident in accepted or ident in updated:
            raise HarnessError("修订只能更新未接受的既有任务，且每个任务仅更新一次")
        updated.add(ident)
        by_id[ident]["description"] = update["description"]
        for dependency in update["add_depends_on"]:
            if dependency not in by_id[ident]["depends_on"]:
                by_id[ident]["depends_on"].append(dependency)
    proposed.extend(deepcopy(result["new_tasks"]))
    return validate_revision(current_tasks, proposed, accepted, additional_allowed_paths=additional_allowed_paths,
                             scope_violations=scope_violations)


def validate_revision(current_tasks, proposed_tasks, accepted_ids, *, additional_allowed_paths=(), scope_violations=None):
    """Allow bounded refinement while retaining every original obligation."""
    validate_plan({"tasks": current_tasks})
    tasks = validate_plan({"tasks": proposed_tasks})
    current = {task["id"]: task for task in current_tasks}
    proposed = {task["id"]: task for task in tasks}
    accepted = set(accepted_ids)
    if not accepted <= current.keys() or not current.keys() <= proposed.keys():
        raise HarnessError("修订不得移除既有任务或引用未知已接受任务")
    scope = {path for task in current_tasks for path in task["allowed_paths"]}
    for path in additional_allowed_paths:
        if not isinstance(path, str):
            raise HarnessError("额外批准的文件范围必须是字符串")
        git.validate_scope_pattern(path)
        scope.add(path)
    for ident, old in current.items():
        new = proposed[ident]
        if ident in accepted and new != old:
            raise HarnessError("修订不得修改已接受任务")
        if old["acceptance"] != new["acceptance"] or old["allowed_paths"] != new["allowed_paths"]:
            raise HarnessError("修订不得改变既有验收条件或文件范围")
        if old.get("required_commands", []) != new.get("required_commands", []):
            raise HarnessError("修订不得改变既有任务的 required_commands")
        for field in ("self_checks", "evidence_requirements"):
            if old.get(field, []) != new.get(field, []):
                raise HarnessError(f"修订不得改变既有任务的 {field}")
        if not set(old["depends_on"]) <= set(new["depends_on"]):
            raise HarnessError("修订不得移除既有依赖")
    if proposed == current:
        raise HarnessError("修订没有产生有效变更")
    outside = list(dict.fromkeys(path for task in tasks if task["id"] not in current
                                 for path in task["allowed_paths"] if path not in scope))
    if outside:
        if scope_violations is None:
            raise ScopeExtensionRequired(outside)
        scope_violations.extend(outside)
    return tasks


def _blank(value):
    return not isinstance(value, str) or not value.strip() or "\0" in value


def validate_decision(result, *, tasks, accepted, ready, gated=(), approved_scope=(),
                      validate_tasks=None, capacity=None):
    """Branch rules of one structurally valid, in-bounds decide payload.

    Returns (action, proposed_tasks). ``ready`` is the request's own ready list;
    a revise selects from its revised DAG. New scope is checked last: it stops
    for operator authority only when it is the revision's only defect.
    """
    action = result["action"]
    if action == "ask":
        if result["selected_task"] or result["instruction"] or result["new_tasks"] or result["updates"]:
            raise HarnessError("ask 的 selected_task、instruction、new_tasks、updates 必须为空")
        if _blank(result["reason"]):
            raise HarnessError("ask 的 reason 必须是非空问题")
        return action, None
    if action == "implement":
        if not tasks:
            raise HarnessError("初始规划只能 revise 或 ask")
        # reason is free text here: recorded with the original, never used.
        if result["new_tasks"] or result["updates"]:
            raise HarnessError("implement 的 new_tasks、updates 必须为空")
        if _blank(result["instruction"]):
            raise HarnessError("implement 必须给出非空 instruction")
        if result["selected_task"] not in list(ready):
            raise HarnessError("implement 的 selected_task 必须是当前就绪任务")
        return action, None
    if _blank(result["reason"]):
        raise HarnessError("revise 必须给出非空 reason")
    if not tasks:
        if result["selected_task"] or result["instruction"] or result["updates"]:
            raise HarnessError("初始规划的 revise 不带 selected_task、instruction 或 updates")
        proposed = validate_plan({"tasks": result["new_tasks"]})
        if validate_tasks is not None:
            validate_tasks({"tasks": proposed})
        if capacity is not None:
            capacity(proposed, proposed)
        return action, proposed
    if _blank(result["instruction"]):
        raise HarnessError("revise 必须同时给出非空 instruction")
    outside = []
    proposed = apply_revision(tasks, result, accepted, additional_allowed_paths=approved_scope,
                              scope_violations=outside)
    if validate_tasks is not None:
        validate_tasks({"tasks": proposed})
    done = set(accepted)
    revised_ready = [t["id"] for t in proposed if t["id"] not in done
                     and set(t["depends_on"]) <= done and t["id"] not in set(gated)]
    if result["selected_task"] not in revised_ready:
        raise HarnessError("revise 的 selected_task 必须是修订后计划中的就绪任务")
    if capacity is not None:
        capacity(proposed, None)
    if outside:
        raise ScopeExtensionRequired(outside)
    return action, proposed
