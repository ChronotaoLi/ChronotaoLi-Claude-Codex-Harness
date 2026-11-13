"""Stop values: one bounded record of why a run stopped, and its projection.

A stop is ``{"kind", "reason", "data", "source"}``. It is a description, never
a permission or a scheduling input: runtime code only writes and clears it,
and every view (status, watch, the dashboard, run_stopped, operation records,
stderr) shows the same projection. The registry decides the title, the exit
commands and the exit code; nothing parses the diagnostic text.
"""
from __future__ import annotations

import json
import re

STOP_STATUSES = ("awaiting_approval", "blocked", "interrupted")
DETAIL_LIMIT = 2000
STOP_LIMIT = 32 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}")


def _id(value):
    return type(value) is str and _ID.fullmatch(value) is not None


def _int(value):
    return type(value) is int and value >= 0


def _text(value):
    return type(value) is str and len(value) <= DETAIL_LIMIT


def _strings(value):
    return (type(value) is list and all(type(item) is str and len(item) <= 4096 for item in value)
            and len(json.dumps(value, ensure_ascii=True)) <= 8192)


def _artifact(value):
    return type(value) is str and value.startswith("/") and 0 < len(value) <= 4096 and "\0" not in value


def _digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


SOURCE_TYPES = {"task_id": _id, "dispatch_id": _id, "request_id": _id, "continuation_id": _id,
                "artifact": _artifact, "artifact_digest": _digest}
COMMON_DATA = {"detail": _text, "dropped_fields": _strings}

REGISTRY = {}


def _row(kind, reason, exit_code, title, exits, source=(), **data):
    REGISTRY[(kind, reason)] = {"exit_code": exit_code, "title": title, "exits": tuple(exits),
                                "data": data, "source": frozenset(source)}


_row("plan_approval", "initial_plan", 0, "计划等待明确批准",
     ("status {run} --full", "resume {run} --approve-plan"), ("artifact", "artifact_digest"),
     revision=_int, task_count=_int)
_row("unclassified", "untyped_harness_error", 1, "未分类的停止；不推测恢复动作", (), error_type=_id)
_row("unclassified", "unexpected_exception", 1, "未预期的异常；不推测恢复动作", (), error_type=_id)
_row("interrupted", "signal", 130, "运行被中断", ("resume {run}", "abandon {run}"))


def bounded(text):
    """The diagnostic excerpt: at most DETAIL_LIMIT characters in total, the note included."""
    text = str(text)
    if len(text) <= DETAIL_LIMIT:
        return text
    note = f"…（已截断，原文 {len(text)} 字符）"
    return text[:DETAIL_LIMIT - len(note)] + note


def make(kind, reason, *, data=None, source=None):
    """Build a registered stop. Unknown combinations or field names are programming errors
    (KeyError); an unfit field value is dropped and named in ``data.dropped_fields``."""
    entry = REGISTRY[(kind, reason)]
    clean_data, clean_source, dropped = {}, {}, []
    for name, value in (data or {}).items():
        check = entry["data"].get(name) or COMMON_DATA.get(name)
        if check is None:
            raise KeyError(f"{kind}/{reason} has no data field {name}")
        if name == "detail" and type(value) is str:
            value = bounded(value)
        if check(value):
            clean_data[name] = value
        else:
            dropped.append(name)
    for name, value in (source or {}).items():
        if name not in entry["source"]:
            raise KeyError(f"{kind}/{reason} has no source field {name}")
        if SOURCE_TYPES[name](value):
            clean_source[name] = value
        else:
            dropped.append(name)
    if dropped:
        clean_data["dropped_fields"] = sorted(set(dropped))
    return {"kind": kind, "reason": reason, "data": clean_data, "source": clean_source}


def validate(value):
    """Shape, registration, per-row fields and their types, total size."""
    from .util import HarnessError
    if not isinstance(value, dict) or set(value) != {"kind", "reason", "data", "source"}:
        raise HarnessError("停止记录形状无效")
    entry = REGISTRY.get((value["kind"], value["reason"])) if all(
        type(value[key]) is str for key in ("kind", "reason")) else None
    if entry is None:
        raise HarnessError("停止记录类型未登记")
    if not isinstance(value["data"], dict) or not isinstance(value["source"], dict):
        raise HarnessError("停止记录字段无效")
    for name, item in value["data"].items():
        check = entry["data"].get(name) or COMMON_DATA.get(name)
        if check is None or not check(item):
            raise HarnessError(f"停止记录字段无效：data.{name}")
    for name, item in value["source"].items():
        if name not in entry["source"] or not SOURCE_TYPES[name](item):
            raise HarnessError(f"停止记录字段无效：source.{name}")
    if len(json.dumps(value, ensure_ascii=True)) > STOP_LIMIT:
        raise HarnessError("停止记录过大")


def _fallback(error):
    from .util import HarnessError
    name = type(error).__name__
    name = name if _id(name) else "Exception"
    reason = "untyped_harness_error" if isinstance(error, HarnessError) else "unexpected_exception"
    return make("unclassified", reason, data={"error_type": name})


def of(error):
    """The stop an exception carries, or the fallback, with the bounded diagnostic. Never raises."""
    try:
        text = str(error) or type(error).__name__
    except Exception:
        text = type(error).__name__
    try:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            value = make("interrupted", "signal")
        else:
            carried = getattr(error, "stop", None)
            value = ({**carried, "data": dict(carried["data"]), "source": dict(carried["source"])}
                     if carried is not None else _fallback(error))
        value["data"]["detail"] = bounded(text)
        validate(value)
    except Exception:
        # Only a programming error gets here; the boundary still reports a valid stop.
        value = make("unclassified", "untyped_harness_error", data={"error_type": "InvalidStop", "detail": bounded(text)})
    return value


def validate_ledger(run):
    """``stop`` exists exactly while the status is a stop, matches it, and is a registered value."""
    from .util import HarnessError
    message = "运行台账停止记录与状态不符；保留原文件并核对证据"
    if ("stop" in run) != (run.get("status") in STOP_STATUSES):
        raise HarnessError(message)
    if "stop" not in run:
        return
    try:
        validate(run["stop"])
    except HarnessError as error:
        raise HarnessError(message) from error
    kind, status = run["stop"]["kind"], run["status"]
    expected = {"awaiting_approval": "plan_approval", "interrupted": "interrupted"}.get(status)
    if (kind != expected) if expected else kind in ("plan_approval", "interrupted"):
        raise HarnessError(message)


def project(value, run_id=None, *, omit_detail=False):
    """What every view shows: title, exit code and exit commands come from the registry."""
    entry = REGISTRY[(value["kind"], value["reason"])]
    fields = {**value["data"], **value["source"], "run": run_id or "RUN"}
    result = {"kind": value["kind"], "reason": value["reason"], "title": entry["title"],
              "exit_code": entry["exit_code"], "exits": [template.format(**fields) for template in entry["exits"]],
              "data": {key: item for key, item in value["data"].items() if key != "detail"},
              "source": dict(value["source"])}
    if not omit_detail:
        result["detail"] = value["data"].get("detail")
    return result


def project_ledger(run, *, omit_detail=False):
    """The projection of a ledger's stop for observers, or None when the run is not stopped."""
    from .util import HarnessError
    if "stop" not in run and run.get("status") not in STOP_STATUSES:
        return None
    try:
        validate_ledger(run)
    except HarnessError as error:
        raise HarnessError("运行台账停止记录无效；使用 status --full 查看原始内容") from error
    return project(run["stop"], run.get("id"), omit_detail=omit_detail)


def exit_code(error):
    value = of(error)
    return REGISTRY[(value["kind"], value["reason"])]["exit_code"]
