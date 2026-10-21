"""Small, closed JSON-schema subset; every returned field is validated locally."""
import re
from pathlib import PurePosixPath

from .util import HarnessError


def obj(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": TEXT}
SAFE_ID = {"type": "string", "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$"}
DETAIL = {"type": "string", "minLength": 1, "maxLength": 4000}
EVIDENCE = {"type": "array", "items": DETAIL, "maxItems": 20}
PROPOSAL = obj(key=SAFE_ID,
               abstract={"type": "string", "minLength": 1, "maxLength": 500},
               content={"type": "string", "minLength": 1, "maxLength": 8000},
               source_paths={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 500},
                             "minItems": 1, "maxItems": 10, "uniqueItems": True})
PROPOSALS = {"type": "array", "items": PROPOSAL, "maxItems": 8}
TASK = obj(id=TEXT, title=TEXT, description=TEXT, depends_on=STRINGS,
           allowed_paths=STRINGS, acceptance=STRINGS, skills=STRINGS)
PLAN = obj(tasks={"type": "array", "items": TASK})
REPLAN = obj(action={"type": "string", "enum": ["revise", "stop"]},
             reason=DETAIL, tasks={"type": "array", "items": TASK})
WORK = obj(status={"type": "string", "enum": ["completed", "blocked"]},
           summary=DETAIL, evidence=EVIDENCE, blockers=EVIDENCE,
           blocker_kind={"type": "string", "enum": ["none", "task", "environment"]},
           proposed_memories=PROPOSALS)
FINDING = obj(id=SAFE_ID,
              severity={"type": "string", "enum": ["blocking", "advisory"]},
              category={"type": "string", "enum": ["defect", "evidence", "scope", "preference"]},
              location=DETAIL, message=DETAIL, evidence=EVIDENCE)
REVIEW = obj(verdict={"type": "string", "enum": ["approve", "request_changes"]},
             summary=DETAIL, findings={"type": "array", "items": FINDING, "maxItems": 30},
             approved_memory_keys={"type": "array", "items": SAFE_ID, "maxItems": 8, "uniqueItems": True})


def envelope(schema, task_id, dispatch_id):
    return obj(task_id={"type": "string", "enum": [task_id]},
               dispatch_id={"type": "string", "enum": [dispatch_id]}, **schema["properties"])


def validate(value, schema, path="response"):
    expected = {"object": dict, "array": list, "string": str}[schema["type"]]
    if not isinstance(value, expected):
        raise HarnessError(f"{path}: expected {schema['type']}")
    if "enum" in schema and value not in schema["enum"]:
        raise HarnessError(f"{path}: value not in permitted set")
    if expected is str:
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise HarnessError(f"{path}: string length exceeds permitted bounds")
        if schema.get("minLength", 0) and not value.strip():
            raise HarnessError(f"{path}: string must not be blank")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise HarnessError(f"{path}: invalid identifier")
    if expected is dict:
        if set(value) != set(schema["required"]):
            raise HarnessError(f"{path}: missing or unexpected fields")
        for key, field in schema["properties"].items():
            validate(value[key], field, f"{path}.{key}")
    if expected is list:
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise HarnessError(f"{path}: array length exceeds permitted bounds")
        if schema.get("uniqueItems") and any(item in value[:i] for i, item in enumerate(value)):
            raise HarnessError(f"{path}: duplicate entries")
        for i, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{i}]")


def validate_work(result):
    validate(result, WORK)
    completed = result["status"] == "completed"
    if completed != (result["blocker_kind"] == "none") or completed != (not result["blockers"]):
        raise HarnessError("worker 状态、blocker_kind 与 blockers 不一致")
    keys = [proposal["key"] for proposal in result["proposed_memories"]]
    if len(keys) != len(set(keys)):
        raise HarnessError("记忆提案 key 必须唯一")
    return result


def validate_review(result, proposed_keys=()):
    validate(result, REVIEW)
    ids = [finding["id"] for finding in result["findings"]]
    if len(ids) != len(set(ids)):
        raise HarnessError("审阅 finding ID 必须唯一")
    for finding in result["findings"]:
        if finding["severity"] == "blocking":
            if finding["category"] == "preference" or not finding["evidence"]:
                raise HarnessError("阻断 finding 必须有证据，且不能仅为偏好")
    if not set(result["approved_memory_keys"]) <= set(proposed_keys):
        raise HarnessError("审阅批准了不存在的记忆提案")
    return result


def review_approved(result):
    return result["verdict"] == "approve" and not any(
        finding["severity"] == "blocking" for finding in result["findings"])


def validate_plan(plan, max_tasks=20):
    validate(plan, PLAN)
    tasks = plan["tasks"]
    ids = [t["id"] for t in tasks]
    if not tasks or len(tasks) > max_tasks or len(set(ids)) != len(ids):
        raise HarnessError("计划必须有 1..max_tasks 个任务，且 ID 唯一")
    for task in tasks:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", task["id"]):
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
        if any(d not in ids or d == task["id"] for d in task["depends_on"]):
            raise HarnessError("任务依赖不存在或依赖自身")
        if any(not re.fullmatch(r"[a-z0-9-]+", s) for s in task["skills"]):
            raise HarnessError("无效 skill 名称")
    done = set()
    while len(done) < len(tasks):
        ready = {t["id"] for t in tasks if set(t["depends_on"]) <= done} - done
        if not ready:
            raise HarnessError("任务依赖包含环")
        done |= ready
    return tasks


def validate_revision(current_tasks, proposed_tasks, accepted_ids, max_tasks=20):
    """Allow bounded refinement while retaining every original obligation."""
    validate_plan({"tasks": current_tasks}, max_tasks)
    tasks = validate_plan({"tasks": proposed_tasks}, max_tasks)
    current = {task["id"]: task for task in current_tasks}
    proposed = {task["id"]: task for task in tasks}
    accepted = set(accepted_ids)
    if not accepted <= current.keys() or not current.keys() <= proposed.keys():
        raise HarnessError("再规划不得移除既有任务或引用未知已接受任务")
    scope = {path for task in current_tasks for path in task["allowed_paths"]}
    for ident, old in current.items():
        new = proposed[ident]
        if ident in accepted and new != old:
            raise HarnessError("再规划不得修改已接受任务")
        if old["acceptance"] != new["acceptance"] or old["allowed_paths"] != new["allowed_paths"]:
            raise HarnessError("再规划不得改变既有验收条件或文件范围")
        if not set(old["depends_on"]) <= set(new["depends_on"]):
            raise HarnessError("再规划不得移除既有依赖")
    for ident in proposed.keys() - current.keys():
        if not set(proposed[ident]["allowed_paths"]) <= scope:
            raise HarnessError("新增任务必须复用既有文件范围，不得扩大范围")
    if proposed == current:
        raise HarnessError("再规划没有产生有效变更")
    return tasks
