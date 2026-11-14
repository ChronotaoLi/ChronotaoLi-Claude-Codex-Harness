"""Stop values: one bounded record of why a run stopped, and its projection.

A stop is ``{"kind", "reason", "data", "source"}``. It is a description, never
a permission or a scheduling input: runtime code only writes and clears it,
and every view (status, watch, the dashboard, run_stopped, operation records,
stderr) shows the same projection. The registry decides the title, the exit
commands and the exit code; nothing parses the diagnostic text.

Exit templates may be conditional: ``(template, "flag")`` applies when
``data.flag`` is true, ``(template, "!flag")`` when it is false. A value whose
applying exits need a field it lacks degrades to ``unclassified`` (exit 1).
"""
from __future__ import annotations

import contextlib
import errno
import json
import re
import sqlite3
import string

STOP_STATUSES = ("awaiting_approval", "blocked", "interrupted")
DETAIL_LIMIT = 2000
LIST_BYTES = 8 * 1024
STOP_LIMIT = 32 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}")
_SHA = re.compile(r"[0-9a-f]{64}")


def _id(v): return type(v) is str and _ID.fullmatch(v) is not None
def _artifact(v): return type(v) is str and v.startswith("/") and 0 < len(v) <= 4096 and "\0" not in v
def _relpath(v): return type(v) is str and 0 < len(v) <= 4096 and "\0" not in v and not v.startswith("/")
def _digest(v): return type(v) is str and _SHA.fullmatch(v) is not None
def _int(v): return type(v) is int and v >= 0
def _bool(v): return type(v) is bool
def _key(v): return type(v) is str and 0 < len(v) <= 120
def _text(v): return type(v) is str and len(v) <= DETAIL_LIMIT
def _enum(*names): return lambda v: type(v) is str and v in names
def _size(v): return len(json.dumps(v, ensure_ascii=True))
def _strings(v): return (type(v) is list and all(type(x) is str and len(x) <= 4096 for x in v)
                         and _size(v) <= LIST_BYTES)
def _argv_list(v): return (type(v) is list and all(type(a) is list and all(type(x) is str for x in a) for a in v)
                           and _size(v) <= LIST_BYTES)
def _argv_map(v): return (type(v) is dict and all(_id(k) and _argv_list(x) for k, x in v.items())
                          and _size(v) <= LIST_BYTES)
def _superseded(v): return type(v) is dict and set(v) == {"kind", "reason"} and all(_id(x) for x in v.values())


SOURCE_TYPES = {"task_id": _id, "dispatch_id": _id, "request_id": _id, "continuation_id": _id,
                "artifact": _artifact, "artifact_digest": _digest}
COMMON_DATA = {"detail": _text, "dropped_fields": _strings, "superseded": _superseded}
LIST_FIELDS = {"patterns", "missing_commands", "options"}

REGISTRY = {}


def _row(kind, reason, exit_code, title, exits=(), source=(), **data):
    REGISTRY[(kind, reason)] = {"exit_code": exit_code, "title": title, "exits": tuple(exits),
                                "data": data, "source": frozenset(source)}


ROLE = _enum("commander", "worker", "reviewer")
_row("plan_approval", "initial_plan", 0, "计划等待明确批准", ("status {run} --full", "resume {run} --approve-plan"),
     ("artifact", "artifact_digest"), revision=_int, task_count=_int)
_row("operator_decision", "answer_required", 3, "等待操作者决定",
     ("resume {run} --operator-decision FILE", ("resume {run} --operator-decision FILE --reverify", "reverify_available")),
     ("request_id", "task_id"), request_kind=_enum("worker", "handoff", "commander"),
     candidate_changed=_bool, reverify_available=_bool)
_row("operator_decision", "operator_stopped", 3, "操作者已决定停止", ("resume {run} --operator-decision FILE",),
     ("request_id", "task_id"), request_kind=_enum("worker", "handoff", "commander"))
_row("scope_extension", "revision", 3, "指挥修订越出已批准的文件范围",
     ("resume {run} --allow-scope 'PATTERN'", "resume {run}"), ("dispatch_id", "artifact", "artifact_digest"),
     patterns=_strings)
for _r, _t, _extra in (("commands_required", "续作需要授权精确命令", {"missing_commands": _argv_list}),
                       ("timeout_required", "续作需要更长的单次时限",
                        {"config_key": _key, "minimum_timeout_seconds": _int, "agent_timeout_seconds": _int})):
    _row("continuation", _r, 3, _t, ("resume {run}", "resume {run} --reject-continuation FILE"),
         ("continuation_id", "task_id", "dispatch_id"), **_extra)
_row("missing_commands", "selected_task", 3, "选中任务所需命令尚未授权", ("resume {run}",), ("task_id",),
     config_key=_key, missing=_argv_map)
_row("missing_commands", "all_ready_tasks", 3, "全部就绪任务所需命令尚未授权", ("resume {run}",), (),
     config_key=_key, missing=_argv_map)
_row("environment", "worker_blocked", 3, "worker 报告环境受阻", ("resume {run}", "resume {run} --environment-update FILE"),
     ("task_id", "dispatch_id", "artifact", "artifact_digest"), invalid_output=_bool)
_row("environment", "checks_failed", 3, "环境前置检查未通过", ("resume {run}",), ("task_id", "artifact"))
_row("budget", "task_attempts", 3, "任务工作尝试达到上限", ("resume {run}", "abandon {run}"), ("task_id",),
     config_key=_key, limit=_int)
_row("budget", "agent_dispatches", 3, "模型派发次数达到上限", ("resume {run}", "abandon {run}"), (),
     config_key=_key, used=_int, limit=_int, minimum_remaining_calls=_int)
_row("budget", "context_capacity", 3, "必要上下文超过预算", ("resume {run}", "abandon {run}"), ("artifact",),
     config_key=_key, required_chars=_int, budget_chars=_int, scope=_enum("dispatch", "final_review"))
_row("budget", "review_findings", 3, "待核验阻断问题超过单次审阅容量", ("abandon {run}",), ("task_id",),
     open_findings=_int, limit=_int)
_row("budget", "agent_timeout", 3, "调用超过单次时限", ("resume {run}",), ("dispatch_id", "task_id"),
     role=ROLE, config_key=_key, limit_seconds=_int)
_row("budget", "agent_turns", 3, "Claude 轮数用尽", ("resume {run}",), ("dispatch_id", "task_id"), role=ROLE, config_key=_key)
_row("verification", "final_timeout", 3, "整体验证超时", ("resume {run}",), ("artifact",), config_key=_key, attempt=_int)
_row("verification", "setup_failed", 3, "全新检出的依赖准备失败", ("resume {run}",), ("artifact",), attempt=_int)
_row("verification", "checks_modified_files", 3, "验证修改了受检文件", ("resume {run}",), ("task_id", "artifact"),
     scope=_enum("task", "final"), phase=_enum("setup", "checks"))
_row("process_unconfirmed", "dispatch_exit", 3, "旧派发的退出无法确认",
     ("resume {run} --confirm-exited {dispatch_id}", "abandon {run} --confirm-exited {dispatch_id}"), ("dispatch_id",))
_row("process_unconfirmed", "command_exit", 3, "命令子进程的退出无法确认",
     ("resume {run} --confirm-exited {dispatch_id}", "abandon {run} --confirm-exited {dispatch_id}"), ("dispatch_id",),
     command_id=_id)
for _r, _t in (("invocation_failed", "模型调用失败（供应商报告错误）"), ("local_start", "模型 CLI 未能启动"),
               ("unknown", "模型调用失败（原因未识别）")):
    _row("provider_failure", _r, 3, _t, ("resume {run}",), ("dispatch_id", "task_id"),
         role=ROLE, provider=_enum("claude", "codex"))
_row("invalid_output", "redo_exhausted", 3, "输出连续两次无效", ("resume {run}", "abandon {run}"),
     ("dispatch_id", "task_id", "artifact", "artifact_digest"), role=ROLE)
_row("workspace_mismatch", "head_changed", 1, "工作树 HEAD 与检查点不同",
     ("resume {run}", "abandon {run}", ("resume {run} --operator-decision FILE", "question_registered")),
     ("task_id", "request_id"), expected_head=_id, question_registered=_bool)
_row("workspace_mismatch", "candidate_changed", 1, "候选代码或暂存区与绑定快照不同", ("resume {run}", "abandon {run}"),
     ("task_id", "dispatch_id"))
_row("workspace_mismatch", "directive_mismatch", 1, "待派发指令与已发布计划或候选不符", ("resume {run}", "abandon {run}"),
     ("task_id",))
_row("workspace_mismatch", "dirty_final_workspace", 1, "整体验收要求干净的已接受提交", ("resume {run}", "abandon {run}"))
_row("workspace_mismatch", "binding_missing", 1, "缺少已保存的工作树 Git 绑定", ("abandon {run}",))
_row("workspace_mismatch", "binding_changed", 1, "工作树 Git 绑定已改变", ("abandon {run}",))
for _r, _t in (("candidate_outside_scope", "候选越出任务范围"), ("protected_path", "候选修改了受保护路径")):
    _row("scope_violation", _r, 1, _t, ("resume {run}",), ("task_id",), path=_relpath)
_row("evidence", "load_integrity", 1, "加载核验发现证据不符", (), ("artifact", "artifact_digest", "dispatch_id", "task_id"))
_row("evidence", "runtime_integrity", 1, "证据有问题，但加载核验此刻通过", ("resume {run}", "abandon {run}"),
     ("artifact", "artifact_digest", "dispatch_id", "task_id"))
_row("evidence", "process_incomplete", 1, "进程证据不完整",
     (("abandon {run}", "!exit_unconfirmed"), ("abandon {run} --confirm-exited {dispatch_id}", "exit_unconfirmed")),
     ("dispatch_id", "task_id", "artifact"), exit_unconfirmed=_bool)
_row("evidence", "unsealed_return", 1, "worker 结果已返回但未封存", ("abandon {run}",), ("dispatch_id", "task_id"),
     return_reason=_text)
_row("evidence", "unsealed_unknown", 1, "worker 返回证据冲突或不可读", ("abandon {run}",), ("dispatch_id", "task_id"),
     return_reason=_text)
_row("runtime_failure", "io", 1, "本地 I/O 失败", (), (), error_type=_id)
_row("runtime_failure", "database", 1, "记忆数据库失败", (), (), error_type=_id)
_row("runtime_failure", "invariant", 1, "运行时一致性检查失败")
_row("unclassified", "untyped_harness_error", 1, "未分类的停止；不推测恢复动作", (), (), error_type=_id)
_row("unclassified", "unexpected_exception", 1, "未预期的异常；不推测恢复动作", (), (), error_type=_id)
# Abandon is listed only when the stop knows every started process's exit was confirmed;
# an interrupt reported without a ledger (or with an unconfirmed exit) lists resume only,
# which reports the exact dispatch IDs to confirm.
_row("interrupted", "signal", 130, "运行被中断", ("resume {run}", ("abandon {run}", "exits_confirmed")),
     exits_confirmed=_bool)
for _r, _t in (("cli_arguments", "命令参数无效"), ("input_schema", "输入文件不合格"), ("conflicting_retry", "重试与已写的不可变文件冲突")):
    _row("invalid_input", _r, 2, _t, (), (), option=_key)
_row("invalid_input", "incompatible_options", 2, "选项不能同用", (), (), options=_strings)
_row("invalid_input", "stale_request", 2, "输入针对的请求或来源已失效", ("status {run}",),
     ("request_id", "continuation_id", "dispatch_id"), option=_key)
_row("invalid_input", "run_state", 2, "该命令或选项不适用于 run 的当前状态", ("status {run}",), (), status=_key)
_row("config_mismatch", "immutable_config", 2, "配置与创建时不一致", ("resume {run}",))
_row("config_mismatch", "shared_assets", 2, "共享资产与创建时不一致", ("resume {run}",))
_row("unsupported_ledger", "ledger_version", 2, "台账版本不受支持", (), (), schema_version=_int, supported=_int)
_row("busy", "run_lock", 3, "另一个进程持有运行锁", ())


def bounded(text):
    """The diagnostic excerpt: at most DETAIL_LIMIT characters in total, the note included."""
    text = str(text)
    if len(text) <= DETAIL_LIMIT:
        return text
    note = f"…（已截断，原文 {len(text)} 字符）"
    return text[:DETAIL_LIMIT - len(note)] + note


def active_exits(entry, data):
    """The exit templates that apply: unconditional ones and those whose condition holds."""
    for item in entry["exits"]:
        template, flag = item if isinstance(item, tuple) else (item, None)
        if flag is None or (data.get(flag[1:]) is False if flag.startswith("!") else data.get(flag) is True):
            yield template


def required_fields(kind, reason, data):
    """Fields the applying exits need; for a negated condition the condition fact itself."""
    entry = REGISTRY[(kind, reason)]
    negated = {item[1][1:] for item in entry["exits"] if isinstance(item, tuple) and item[1].startswith("!")}
    missing = sorted(flag for flag in negated if type(data.get(flag)) is not bool)
    if missing:
        return missing  # without the fact no exit can be chosen; never read it as false
    names = set()
    for template in active_exits(entry, data):
        names |= {field for _, field, _, _ in string.Formatter().parse(template) if field and field != "run"}
    return sorted(names)


def make(kind, reason, *, data=None, source=None):
    """Build a registered stop. Unknown combinations or field names are programming errors
    (KeyError); an unfit field value is dropped and named in ``data.dropped_fields``, an
    over-long list keeps its head and records ``<field>_omitted``. A value that cannot fill
    the exits it would list becomes ``unclassified`` instead."""
    entry = REGISTRY[(kind, reason)]
    clean_data, clean_source, dropped = {}, {}, []
    for name, value in (data or {}).items():
        check = entry["data"].get(name) or COMMON_DATA.get(name)
        if check is None:
            raise KeyError(f"{kind}/{reason} has no data field {name}")
        if name == "detail" and type(value) is str:
            value = bounded(value)
        if name in LIST_FIELDS and type(value) is list and not check(value):
            kept = list(value)
            while kept and not check(kept):
                kept.pop()
            clean_data[name + "_omitted"] = len(value) - len(kept)
            value = kept
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
    missing = [name for name in required_fields(kind, reason, clean_data)
               if name not in clean_data and name not in clean_source]
    if missing:
        return {"kind": "unclassified", "reason": "untyped_harness_error", "source": {},
                "data": {"error_type": "IncompleteStop", "dropped_fields": sorted(set(dropped) | set(missing))}}
    return {"kind": kind, "reason": reason, "data": clean_data, "source": clean_source}


def missing_or_replaced(error):
    """A read failed because the path is gone or no longer what was written: evidence, not local I/O."""
    return (isinstance(error, (FileNotFoundError, NotADirectoryError, IsADirectoryError))
            or getattr(error, "errno", None) == errno.ELOOP)


def keep_local_io(error):
    """Called before wrapping an OSError from an evidence read: local I/O leaves as itself."""
    if isinstance(error, OSError) and not missing_or_replaced(error):
        raise error


@contextlib.contextmanager
def classify(kind, reason, *, data=None, source=None):
    """Give an untyped HarnessError leaving the block this stop; typed ones and others pass unchanged."""
    from .util import HarnessError
    try:
        yield
    except HarnessError as error:
        if error.stop is None:
            error.stop = make(kind, reason, data=data, source=source)
        raise


@contextlib.contextmanager
def evidence_reads(*, source=None, message=None):
    """Wrap only reading, decoding and content checks of runtime-written evidence.

    Untyped HarnessErrors, path loss and decoding failures leave as EvidenceError
    (runtime_integrity); other OSErrors (permission, descriptors, devices, a full
    disk) leave unchanged as local I/O; typed errors keep their type.
    """
    from .util import EvidenceError, HarnessError

    def typed(error):
        text = f"{message}：{error}" if message else str(error)
        return EvidenceError(text, stop=make("evidence", "runtime_integrity", source=source or {}))
    try:
        yield
    except EvidenceError:
        raise
    except HarnessError as error:
        if error.stop is not None:
            raise
        raise typed(error) from error
    except OSError as error:
        if not missing_or_replaced(error):
            raise
        raise typed(error) from error
    except (ValueError, RecursionError) as error:  # JSON and Unicode decoding errors are ValueErrors
        raise typed(error) from error


def reads_evidence(function):
    """Run ``function`` inside ``evidence_reads()``: for readers and validators of runtime evidence."""
    import functools

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with evidence_reads():
            return function(*args, **kwargs)
    return wrapper


def classifies(kind, reason, **fields):
    """Run the decorated function inside ``classify(kind, reason, ...)``."""
    import functools

    def decorate(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            with classify(kind, reason, **fields):
                return function(*args, **kwargs)
        return wrapper
    return decorate


def load_failure(error):
    """The type of a failed load or settle check (spec 2.2): evidence, local I/O, an inner type, or the fallback."""
    from .util import EvidenceError, HarnessError
    carried = getattr(error, "stop", None)
    if isinstance(error, EvidenceError):
        source = dict(carried["source"]) if isinstance(carried, dict) else {}
        return make("evidence", "load_integrity", source=source)
    if isinstance(error, HarnessError) and isinstance(carried, dict):
        return {**carried, "data": dict(carried["data"]), "source": dict(carried["source"])}
    return _fallback(error)


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
        if check is None and name.endswith("_omitted") and name[:-len("_omitted")] in LIST_FIELDS:
            check = _int
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
    if isinstance(error, HarnessError):
        return make("unclassified", "untyped_harness_error", data={"error_type": name})
    if isinstance(error, sqlite3.Error):
        return make("runtime_failure", "database", data={"error_type": name})
    if isinstance(error, OSError):
        return make("runtime_failure", "io", data={"error_type": name})
    return make("unclassified", "unexpected_exception", data={"error_type": name})


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
              "exit_code": entry["exit_code"],
              "exits": [template.format(**fields) for template in active_exits(entry, value["data"])],
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
