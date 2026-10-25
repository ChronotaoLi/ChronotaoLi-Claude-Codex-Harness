"""Small, closed JSON-schema subset; every returned field is validated locally."""
import re
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
PLAN = obj(tasks={"type": "array", "items": TASK, "minItems": 1})
TASK_UPDATE = obj(id=TASK_ID, description=NONBLANK, add_depends_on=DEPENDENCIES)
REPLAN = obj(action={"type": "string", "enum": ["revise", "stop"]},
             reason=DETAIL, new_tasks={"type": "array", "items": TASK},
             updates={"type": "array", "items": TASK_UPDATE})
WORK = obj(status={"type": "string", "enum": ["completed", "blocked"]},
           summary=DETAIL, evidence=EVIDENCE, blockers=EVIDENCE,
           blocker_kind={"type": "string", "enum": ["none", "task", "environment", "operator"]},
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
    decision_requests={"type": "array", "items": obj(id=SAFE_ID, task_id=TASK_ID, question=DETAIL), "maxItems": 16})
FINDING = obj(id=SAFE_ID,
              severity={"type": "string", "enum": ["blocking", "advisory"]},
              category={"type": "string", "enum": ["defect", "evidence", "scope", "preference"]},
              location=DETAIL, message=DETAIL, evidence=EVIDENCE)
REVIEW = obj(verdict={"type": "string", "enum": ["approve", "request_changes"]},
             summary=DETAIL, findings={"type": "array", "items": FINDING, "maxItems": 30},
             approved_memory_keys={"type": "array", "items": SAFE_ID, "maxItems": 8, "uniqueItems": True})


def envelope(schema, task_id, dispatch_id):
    result = obj(task_id={"type": "string", "enum": [task_id]},
                 dispatch_id={"type": "string", "enum": [dispatch_id]}, **schema["properties"])
    result["required"] = ["task_id", "dispatch_id", *schema["required"]]
    return result


class RepairableFormatError(HarnessError):
    """A bounded output formatting defect, never an identity or approval error."""

    def __init__(self, path, message):
        self.path = path
        super().__init__(f"{path}: {message}")


def output_constraints(schema, path="response"):
    """Derive the local constraints omitted from the providers' schema subset."""
    fields = ("minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems", "minimum", "maximum")
    bounds = {key: deepcopy(schema[key]) for key in fields if key in schema}
    if schema.get("minLength", 0):
        bounds["non_blank"] = True
    result = [{"path": path, **bounds}] if bounds else []
    for key, field in schema.get("properties", {}).items():
        result.extend(output_constraints(field, f"{path}.{key}"))
    if "items" in schema:
        result.extend(output_constraints(schema["items"], f"{path}[]"))
    return result


def review_output_constraints(schema, proposed_keys=()):
    """Expose review semantics alongside the schema's ordinary format bounds.

    ``uniqueBy`` names the field that must be unique across array items;
    ``subsetOf`` lists all permitted array values. The conditional rule applies
    independently to every finding, using JSON Schema's if/then structure.
    These input constraints do not extend either provider's wire schema.
    """
    result = output_constraints(schema)
    by_path = {constraint["path"]: constraint for constraint in result}
    by_path["response.findings"]["uniqueBy"] = "id"
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
    """Check all hard schema boundaries before considering a format repair."""
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
            raise RepairableFormatError(path, "string length exceeds permitted bounds")
        if schema.get("minLength", 0) and not value.strip():
            raise RepairableFormatError(path, "string must not be blank")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise RepairableFormatError(path, "invalid identifier")
    if schema["type"] == "object":
        for key, field in schema["properties"].items():
            if key in value:
                _validate_format(value[key], field, f"{path}.{key}")
    if schema["type"] == "integer":
        if not schema.get("minimum", value) <= value <= schema.get("maximum", value):
            raise RepairableFormatError(path, "integer exceeds permitted bounds")
    if schema["type"] == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise RepairableFormatError(path, "array length exceeds permitted bounds")
        if schema.get("uniqueItems") and any(item in value[:i] for i, item in enumerate(value)):
            raise RepairableFormatError(path, "duplicate entries")
        for i, item in enumerate(value):
            _validate_format(item, schema["items"], f"{path}[{i}]")


def validate(value, schema, path="response"):
    validate_structure(value, schema, path)
    _validate_format(value, schema, path)


def validate_work_semantics(result):
    """Check worker completion and proposal identity after structural validation."""
    completed = result["status"] == "completed"
    if completed != (result["blocker_kind"] == "none") or completed != (not result["blockers"]):
        raise HarnessError("worker 状态、blocker_kind 与 blockers 不一致")
    keys = [proposal["key"] for proposal in result["proposed_memories"]]
    if len(keys) != len(set(keys)):
        raise HarnessError("记忆提案 key 必须唯一")
    # Optional checkpoint declarations are resolved by progress.record_checkpoints:
    # a bad item or duplicate batch must not erase completion or a blocker.
    ids = [item["id"] for item in result.get("decision_requests", [])]
    if len(ids) != len(set(ids)):
        raise HarnessError("worker decision_requests 的 id 必须唯一")
    if result.get("decision_requests") and not completed:
        raise HarnessError("后续任务的决策请求只允许随已完成的任务结果提交")


def validate_work(result):
    validate_structure(result, WORK)
    validate_work_semantics(result)
    _validate_format(result, WORK, "response")
    return result


def validate_review_semantics(result, proposed_keys=()):
    """Keep finding identity and memory approvals outside format correction."""
    ids = [finding["id"] for finding in result["findings"]]
    if len(ids) != len(set(ids)):
        raise HarnessError("审阅 finding ID 必须唯一")
    for finding in result["findings"]:
        if finding["severity"] == "blocking":
            if finding["category"] == "preference" or not finding["evidence"]:
                raise HarnessError("阻断 finding 必须有证据，且不能仅为偏好")
    if not set(result["approved_memory_keys"]) <= set(proposed_keys):
        raise HarnessError("审阅批准了不存在的记忆提案")


def validate_review(result, proposed_keys=()):
    validate_structure(result, REVIEW)
    validate_review_semantics(result, proposed_keys)
    _validate_format(result, REVIEW, "response")
    return result


def validate_immutable_findings(findings):
    """Check finding bounds that neither automatic nor explicit repair may change."""
    immutable = deepcopy(REVIEW["properties"]["findings"])
    immutable["items"]["properties"]["id"] = TEXT
    validate(findings, immutable, "response.findings")


def validate_review_correction(previous, current):
    """A fully validated correction must preserve the first review's decision.

    Only its summary and malformed finding IDs may change. In particular, an
    invalidly long finding cannot be shortened by losing its evidence, and an
    overfull findings array cannot be repaired by dropping a reported defect.
    """
    if (current["verdict"] != previous["verdict"]
            or set(current["approved_memory_keys"]) != set(previous["approved_memory_keys"])
            or len(current["findings"]) != len(previous["findings"])):
        raise HarnessError("审阅格式修复不得改变结论、记忆批准或 findings 数量")
    fields = ("severity", "category", "location", "message", "evidence")
    for old, new in zip(previous["findings"], current["findings"]):
        if any(new[field] != old[field] for field in fields):
            raise HarnessError("审阅格式修复不得改变 finding 内容、严重级别或顺序")
        if re.fullmatch(SAFE_ID["pattern"], old["id"]) and new["id"] != old["id"]:
            raise HarnessError("审阅格式修复不得改变已有合法 finding ID")
    return current


def review_approved(result):
    return result["verdict"] == "approve" and not any(
        finding["severity"] == "blocking" for finding in result["findings"])


def validate_approval_repair(previous, current, proposed_keys):
    """Repair approval metadata without replacing the underlying review."""
    permitted = set(previous["approved_memory_keys"]) & set(proposed_keys)
    if (not review_approved(previous) or not review_approved(current)
            or set(current["approved_memory_keys"]) != permitted
            or len(previous["findings"]) != len(current["findings"])):
        raise HarnessError("显式审阅修正必须保留批准结论、有效记忆批准和全部 findings")
    ids = [finding["id"] for finding in previous["findings"]]
    for old, new in zip(previous["findings"], current["findings"]):
        if any(old[field] != new[field] for field in ("severity", "category", "location", "message", "evidence")):
            raise HarnessError("显式审阅修正不得改变 finding 内容、严重级别或顺序")
        if ids.count(old["id"]) == 1 and re.fullmatch(SAFE_ID["pattern"], old["id"]) and new["id"] != old["id"]:
            raise HarnessError("显式审阅修正不得改变已有合法且唯一的 finding ID")


def validate_commander_format_correction(kind, previous, current):
    """Text formatting cannot replace an already selected task or revision."""
    fields = ("selected_task",) if kind == "dispatch" else ("action", "new_tasks", "updates")
    if kind not in ("dispatch", "replan") or any(previous[field] != current[field] for field in fields):
        raise HarnessError("指挥格式修正不得改变原任务选择或修订操作")


def validate_required_commands(task):
    commands = task.get("required_commands", [])
    validate_structure(commands, REQUIRED_COMMANDS, "task.required_commands")
    validate_commands(commands, "task.required_commands")
    _validate_format(commands, REQUIRED_COMMANDS, "task.required_commands")
    return commands


def validate_plan(plan, max_tasks=20):
    validate_structure(plan, PLAN)
    tasks = plan["tasks"]
    ids = [t["id"] for t in tasks]
    if not tasks or len(tasks) > max_tasks or len(set(ids)) != len(ids):
        raise HarnessError("计划必须有 1..max_tasks 个任务，且 ID 唯一")
    for task in tasks:
        commands = task.get("required_commands", [])
        validate_commands(commands, "task.required_commands")
        # Changing prerequisites is a plan correction, not a text format repair.
        if len(commands) > REQUIRED_COMMANDS["maxItems"]:
            raise HarnessError(f"任务 {task['id']} 的 required_commands 最多 {REQUIRED_COMMANDS['maxItems']} 条")
        if len({tuple(argv) for argv in commands}) != len(commands):
            raise HarnessError(f"任务 {task['id']} 的 required_commands 不得重复")
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


def apply_revision(current_tasks, result, accepted_ids, max_tasks=20, *, additional_allowed_paths=()):
    """Apply only explicit additions and brief updates to runtime-owned tasks."""
    validate_structure(result, REPLAN)
    validate_plan({"tasks": current_tasks}, max_tasks)
    accepted = set(accepted_ids)
    current = {task["id"]: task for task in current_tasks}
    if not accepted <= current.keys():
        raise HarnessError("再规划引用未知已接受任务")
    if result["action"] == "stop":
        if result["new_tasks"] or result["updates"]:
            raise HarnessError("停止再规划时不得包含新增任务或更新")
        _validate_format(result, REPLAN, "response")
        return deepcopy(current_tasks)
    proposed = deepcopy(current_tasks)
    by_id = {task["id"]: task for task in proposed}
    updated = set()
    for update in result["updates"]:
        ident = update["id"]
        if ident not in current or ident in accepted or ident in updated:
            raise HarnessError("再规划只能更新未接受的既有任务，且每个任务仅更新一次")
        updated.add(ident)
        by_id[ident]["description"] = update["description"]
        for dependency in update["add_depends_on"]:
            if dependency not in by_id[ident]["depends_on"]:
                by_id[ident]["depends_on"].append(dependency)
    proposed.extend(deepcopy(result["new_tasks"]))
    tasks = validate_revision(current_tasks, proposed, accepted, max_tasks,
                              additional_allowed_paths=additional_allowed_paths)
    _validate_format(result, REPLAN, "response")
    return tasks


def validate_revision(current_tasks, proposed_tasks, accepted_ids, max_tasks=20, *, additional_allowed_paths=()):
    """Allow bounded refinement while retaining every original obligation."""
    validate_plan({"tasks": current_tasks}, max_tasks)
    tasks = validate_plan({"tasks": proposed_tasks}, max_tasks)
    current = {task["id"]: task for task in current_tasks}
    proposed = {task["id"]: task for task in tasks}
    accepted = set(accepted_ids)
    if not accepted <= current.keys() or not current.keys() <= proposed.keys():
        raise HarnessError("再规划不得移除既有任务或引用未知已接受任务")
    scope = {path for task in current_tasks for path in task["allowed_paths"]}
    for path in additional_allowed_paths:
        if not isinstance(path, str):
            raise HarnessError("额外批准的文件范围必须是字符串")
        git.validate_scope_pattern(path)
        scope.add(path)
    for ident, old in current.items():
        new = proposed[ident]
        if ident in accepted and new != old:
            raise HarnessError("再规划不得修改已接受任务")
        if old["acceptance"] != new["acceptance"] or old["allowed_paths"] != new["allowed_paths"]:
            raise HarnessError("再规划不得改变既有验收条件或文件范围")
        if old.get("required_commands", []) != new.get("required_commands", []):
            raise HarnessError("再规划不得改变既有任务的 required_commands")
        if not set(old["depends_on"]) <= set(new["depends_on"]):
            raise HarnessError("再规划不得移除既有依赖")
    for ident in proposed.keys() - current.keys():
        if not set(proposed[ident]["allowed_paths"]) <= scope:
            raise HarnessError("新增任务必须复用既有文件范围或操作者显式批准的范围，不得自行扩大范围")
    if proposed == current:
        raise HarnessError("再规划没有产生有效变更")
    return tasks
