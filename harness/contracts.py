"""Small, closed JSON-schema subset; every returned field is validated locally."""
import re
from pathlib import PurePosixPath

from .util import HarnessError


def obj(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": TEXT}
TASK = obj(id=TEXT, title=TEXT, description=TEXT, depends_on=STRINGS,
           allowed_paths=STRINGS, acceptance=STRINGS, skills=STRINGS)
PLAN = obj(tasks={"type": "array", "items": TASK})
WORK = obj(status={"type": "string", "enum": ["completed", "blocked"]},
           summary=TEXT, evidence=STRINGS, blockers=STRINGS)
REVIEW = obj(verdict={"type": "string", "enum": ["approve", "request_changes"]},
             summary=TEXT, findings=STRINGS)


def envelope(schema, task_id, dispatch_id):
    return obj(task_id={"type": "string", "enum": [task_id]},
               dispatch_id={"type": "string", "enum": [dispatch_id]}, **schema["properties"])


def validate(value, schema, path="response"):
    expected = {"object": dict, "array": list, "string": str}[schema["type"]]
    if not isinstance(value, expected):
        raise HarnessError(f"{path}: expected {schema['type']}")
    if "enum" in schema and value not in schema["enum"]:
        raise HarnessError(f"{path}: value not in permitted set")
    if expected is dict:
        if set(value) != set(schema["required"]):
            raise HarnessError(f"{path}: missing or unexpected fields")
        for key, field in schema["properties"].items():
            validate(value[key], field, f"{path}.{key}")
    if expected is list:
        for i, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{i}]")


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
        for path in task["allowed_paths"]:
            if not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
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
