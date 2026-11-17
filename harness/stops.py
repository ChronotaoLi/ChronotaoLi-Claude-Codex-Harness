"""Stop values: one bounded record of why a run stopped, and its projection.

A stop is ``{"kind", "reason", "data", "source"}``. It is a description, never
a permission or a scheduling input: runtime code only writes and clears it,
and every view (status, watch, the dashboard, run_stopped, operation records,
stderr) shows the same projection. The registry decides the title, the exit
commands and the exit code; nothing parses the diagnostic text.

Exit templates may be conditional: ``(template, "flag")`` applies when
``data.flag`` is true (or a non-empty list), ``(template, "!flag")`` when it is
false. A value whose applying exits need a field it lacks degrades to
``unclassified`` (exit 1). ``{field:scope_args}`` renders a list of scope entries
as ``--scope=VALUE`` shell words.
"""
from __future__ import annotations

import contextlib
import errno
import json
import math
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
def _enum(*names):
    def check(v): return type(v) is str and v in names
    check.values = names
    return check
def _size(v): return len(json.dumps(v, ensure_ascii=True))


def _bounded(shape, limit=LIST_BYTES, items=None):
    """A list-like type: ``shape`` (every item well formed), at most ``items`` items, at most ``limit`` bytes.

    Marked by ``shape``: an over-long value keeps its longest passing head (``make``, ``_fit``).
    """
    def check(v): return shape(v) and (items is None or len(_items(v)) <= items) and _size(v) <= limit
    check.shape = shape
    return check


def _listed(entry, name):
    """``name`` is a list-like data field of this row: the only fields that are shortened and may carry ``<name>_omitted``."""
    return hasattr(entry["data"].get(name), "shape")


def _strings_shape(v): return type(v) is list and all(type(x) is str and len(x) <= 4096 for x in v)
def _argv_list_shape(v): return type(v) is list and all(type(a) is list and all(type(x) is str for x in a) for a in v)
def _map_shape(v): return (type(v) is dict and all(_id(k) and type(x) is list
                                                    and all(type(p) is str and len(p) <= 4096 for p in x)
                                                    for k, x in v.items()))
def _argv_map_shape(v): return type(v) is dict and all(_id(k) and _argv_list_shape(x) for k, x in v.items())
def _superseded(v): return type(v) is dict and set(v) == {"kind", "reason"} and all(_id(x) for x in v.values())
def _quota_window(v):
    return (type(v) is dict and set(v) == {"bucket", "used_percent", "resets_at"}
            and type(v["bucket"]) is str and 0 < len(v["bucket"]) <= 160
            and (type(v["used_percent"]) is int or (type(v["used_percent"]) is float and math.isfinite(v["used_percent"])))
            and v["used_percent"] >= 0 and (v["resets_at"] is None or _text(v["resets_at"]) and len(v["resets_at"]) <= 40))
_strings = _bounded(_strings_shape)
_argv_list = _bounded(_argv_list_shape)
_argv_map = _bounded(_argv_map_shape)
_strings_map = _bounded(_map_shape)
_quota_windows = _bounded(lambda v: type(v) is list and all(_quota_window(x) for x in v), 4096, items=16)


SOURCE_TYPES = {"task_id": _id, "dispatch_id": _id, "request_id": _id, "continuation_id": _id,
                "artifact": _artifact, "artifact_digest": _digest}
COMMON_DATA = {"detail": _text, "dropped_fields": _strings, "superseded": _superseded}

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
# A held proposal (spec-6 2.3): its receipt is kept; scope widening or a rejection are the exits.
# Data fields are listed in the order they are kept when the whole value must shrink (last shrinks first).
_SCOPE_EXITS = (("resume {run} {suggested_scope:scope_args}", "suggested_scope"),
                "resume {run} --reject-request FILE", "status {run}")
_PROPOSAL_SOURCE = ("request_id", "dispatch_id", "artifact", "artifact_digest")
_row("scope_extension", "revision", 3, "指挥修订有模式无法证明在范围边界内（已持有）", _SCOPE_EXITS, _PROPOSAL_SOURCE,
     suggested_scope=_strings, gaps=_strings_map, missing_commands=_argv_map, scope=_strings, widened=_strings,
     selected_task=_id, config_key=_key)
_row("scope_extension", "initial_plan", 3, "首版计划有模式无法证明在范围边界内（已持有）", _SCOPE_EXITS, _PROPOSAL_SOURCE,
     suggested_scope=_strings, gaps=_strings_map, missing_commands=_argv_map, scope=_strings, widened=_strings,
     config_key=_key)
_row("scope_extension", "supplied_plan", 3, "供给的首版计划越出启动后声明的范围边界",
     (("resume {run} {suggested_scope:scope_args}", "suggested_scope"), "abandon {run}"), (),
     suggested_scope=_strings, gaps=_strings_map, scope=_strings, widened=_strings)
for _r, _t, _extra in (("commands_required", "续作需要授权精确命令", {"missing_commands": _argv_list}),
                       ("timeout_required", "续作需要更长的单次时限",
                        {"config_key": _key, "minimum_timeout_seconds": _int, "agent_timeout_seconds": _int})):
    _row("continuation", _r, 3, _t, ("resume {run}", "resume {run} --reject-request FILE"),
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
_row("budget", "proposal_capacity", 3, "已判定合格的提案在当前条件下超过整体审阅容量（提案保留）",
     ("resume {run}", "resume {run} --reject-request FILE"), _PROPOSAL_SOURCE,
     config_key=_key, required_chars=_int, budget_chars=_int)
_row("budget", "review_findings", 3, "待核验阻断问题超过单次审阅容量", ("abandon {run}",), ("task_id",),
     open_findings=_int, limit=_int)
_row("budget", "agent_timeout", 3, "调用超过单次时限", ("resume {run}",),
     ("dispatch_id", "task_id", "artifact", "artifact_digest"), role=ROLE, config_key=_key, limit_seconds=_int)
_row("budget", "agent_turns", 3, "Claude 轮数用尽", ("resume {run}",),
     ("dispatch_id", "task_id", "artifact", "artifact_digest"), role=ROLE, config_key=_key, http_status=_int)
_row("verification", "final_timeout", 3, "整体验证超时", ("resume {run}",), ("artifact",), config_key=_key, attempt=_int)
_row("verification", "setup_failed", 3, "全新检出的依赖准备失败", ("resume {run}",), ("artifact",), attempt=_int)
_row("verification", "checks_modified_files", 3, "验证修改了受检文件", ("resume {run}",), ("task_id", "artifact"),
     scope=_enum("task", "final"), phase=_enum("setup", "checks"))
# A worker that already returned an object result but was never sealed cannot be
# continued after confirming its exit (an unsealed result is never adopted later):
# then only abandon is listed (``returned_unsealed``, spec-5 16.3).
_UNCONFIRMED_EXITS = (("resume {run} --confirm-exited {dispatch_id}", "!returned_unsealed"),
                      "abandon {run} --confirm-exited {dispatch_id}")
_row("process_unconfirmed", "dispatch_exit", 3, "旧派发的退出无法确认", _UNCONFIRMED_EXITS, ("dispatch_id",),
     returned_unsealed=_bool)
_row("process_unconfirmed", "command_exit", 3, "命令子进程的退出无法确认", _UNCONFIRMED_EXITS, ("dispatch_id",),
     command_id=_id, returned_unsealed=_bool)
# Classified call failures (spec-5): the failure original's path and digest in source.
_FAILURE_SOURCE = ("dispatch_id", "task_id", "artifact", "artifact_digest")
# A quota failure's account snapshot (spec-7 3.6), all optional: whether a sample was taken and
# published, its file name, why it is unavailable, and the snapshot's windows at 100 % or more as
# listed (an observation, not the failure's cause; no exit reads them).
QUOTA_REASONS = ("not_installed", "timeout", "output_limit", "exited", "provider_error", "shape_mismatch",
                 "not_zero_call", "probe_disabled", "internal_error")
_QUOTA_DATA = {"quota_snapshot": _enum("available", "unavailable", "not_published"), "quota_sample": _id,
               "quota_reason": _enum(*QUOTA_REASONS), "full_windows": _quota_windows}
# Why a usage-limit failure was not waited out in its process (harness.quota_wait), all optional:
# the reason, the waits this process completed, and how far the decision got (the chosen window,
# its reset, the seconds a wait would have needed, the configured limit, the uncertain windows).
WAIT_REASONS = ("not_running", "provider", "unsaved_state", "disabled", "dispatch_budget", "no_sampler",
                "exit_unconfirmed", "snapshot_unavailable", "window_uncertain", "no_blocking_window",
                "reset_unknown", "reset_passed", "exceeds_limit", "record_invalid", "load_checks_failed")
_WAIT_DATA = {"wait_reason": _enum(*WAIT_REASONS), "waits": _int, "window": _text, "resets_at": _text,
              "wait_seconds": _int, "wait_limit_seconds": _int, "uncertain_windows": _strings}
for _k, _r, _t in (("provider_quota", "usage_limit", "供应商额度用尽"), ("provider_quota", "rate_limit", "供应商限速"),
                   ("provider_failure", "overloaded", "供应商过载"),
                   ("provider_failure", "invocation_failed", "模型调用失败（CLI 报告错误）"),
                   ("provider_failure", "local_start", "模型 CLI 未能启动"),
                   ("provider_failure", "unknown", "模型调用失败（原因未识别）")):
    _row(_k, _r, 3, _t, ("resume {run}",), _FAILURE_SOURCE,
         role=ROLE, provider=_enum("claude", "codex"), http_status=_int,
         **(_QUOTA_DATA if _k == "provider_quota" else {}), **(_WAIT_DATA if _r == "usage_limit" else {}))
_row("provider_failure", "authentication", 3, "账号认证或权限失败", ("doctor", "resume {run}"), _FAILURE_SOURCE,
     role=ROLE, provider=_enum("claude", "codex"), http_status=_int)
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
_row("invalid_input", "plan_outside_scope", 2, "供给的计划越出声明的范围边界", (), (),
     option=_key, suggested_scope=_strings, gaps=_strings_map, scope=_strings)
_row("invalid_input", "scope_pattern", 2, "范围边界条目形式不合法", (), (), option=_key, patterns=_strings)
_row("invalid_input", "plan_unpublished", 2, "首版计划尚未发布，不能批准", ("status {run}",), (),
     option=_key, status=_key)
_row("invalid_input", "stale_request", 2, "输入针对的请求或来源已失效", ("status {run}",),
     ("request_id", "continuation_id", "dispatch_id"), option=_key)
_row("invalid_input", "run_state", 2, "该命令或选项不适用于 run 的当前状态", ("status {run}",), (), status=_key)
_row("config_mismatch", "immutable_config", 2, "配置与创建时不一致", ("resume {run}",))
_row("config_mismatch", "shared_assets", 2, "共享资产与创建时不一致", ("resume {run}",))
_row("unsupported_ledger", "ledger_version", 2, "台账版本不受支持", (), (), schema_version=_int, supported=_int)
# While a quota wait holds the lock, resume/abandon point to its record (which proves no live process).
_row("busy", "run_lock", 3, "另一个进程持有运行锁", (("status {run}", "quota_wait_recorded"),), (),
     quota_wait_recorded=_bool, quota_wait_until=_text, quota_wait_recorded_at=_text, quota_wait_note=_text)


def bounded(text):
    """The diagnostic excerpt: at most DETAIL_LIMIT characters in total, the note included."""
    text = str(text)
    if len(text) <= DETAIL_LIMIT:
        return text
    note = f"…（已截断，原文 {len(text)} 字符）"
    return text[:DETAIL_LIMIT - len(note)] + note


def _holds(data, flag):
    if flag.startswith("!"):
        return data.get(flag[1:]) is False
    value = data.get(flag)
    return value is True or (type(value) is list and len(value) > 0)


def active_exits(entry, data):
    """The exit templates that apply: unconditional ones and those whose condition holds."""
    for item in entry["exits"]:
        template, flag = item if isinstance(item, tuple) else (item, None)
        if flag is None or _holds(data, flag):
            yield template


class _ExitFormatter(string.Formatter):
    def format_field(self, value, format_spec):
        if format_spec == "scope_args":
            from .scope import scope_args
            return scope_args(value)
        return super().format_field(value, format_spec)


def _items(value):
    """A list's items, or a map of lists flattened in key order, then item order."""
    if type(value) is list:
        return list(value)
    return [(key, item) for key, items in value.items() for item in items]


def _build(value, items):
    """A head of ``value`` from a prefix of its ``_items``."""
    if type(value) is list:
        return list(items)
    head = {}
    for key, item in items:
        head.setdefault(key, []).append(item)
    return head


def _longest(count, fits, minimum=0):
    """The largest k in [minimum, count] with ``fits(k)``, for ``fits`` true up to some k; None when not even ``minimum``."""
    if not fits(minimum):
        return None
    low, high = minimum, count
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    return low


def _head(value, check):
    """The longest head of a list, or of a map of lists (key order, then item order), that passes ``check``."""
    items = _items(value)
    return _build(value, items[:_longest(len(items), lambda k: check(_build(value, items[:k]))) or 0])


def _total(value):
    """The measure of the whole stop value: the same serialization ``validate`` limits."""
    return len(json.dumps(value, ensure_ascii=True))


def _fit(value, original_length=None):
    """Shrink a stop value in place until its whole serialization fits ``STOP_LIMIT``; return it.

    The measure is the complete value (diagnostic, omission counts and every other field
    included), never an estimate. Order: list fields no exit reads, the later-registered
    first (each keeps its longest head and records ``<field>_omitted``); then the diagnostic
    ``detail`` (shortened, finally removed) and other free texts no exit reads; last the list
    fields an exit reads or is conditioned on, which keep at least one item so their exit
    stays listed. Raises ValueError when even that cannot fit (identity fields alone too large).
    """
    if _total(value) <= STOP_LIMIT:
        return value
    entry = REGISTRY[(value["kind"], value["reason"])]
    data = value["data"]
    used = set(required_fields(value["kind"], value["reason"], data))
    used |= {item[1].lstrip("!") for item in entry["exits"] if isinstance(item, tuple)}
    lists = [name for name in reversed(list(entry["data"]))
             if _listed(entry, name) and type(data.get(name)) in (list, dict) and data[name]]

    def shrink(name, minimum):
        original, items = data[name], _items(data[name])
        prior = data.pop(name + "_omitted", 0)

        def fits(k):
            data[name] = _build(original, items[:k])
            if prior + len(items) - k:
                data[name + "_omitted"] = prior + len(items) - k
            else:
                data.pop(name + "_omitted", None)
            return _total(value) <= STOP_LIMIT
        kept = _longest(len(items), fits, minimum)
        fits(minimum if kept is None else kept)
        return kept is not None

    def shorten(name, length):
        text = data[name]
        note = f"…（已截断，原文 {length if length is not None else len(text)} 字符）"
        kept = _longest(max(0, len(text) - len(note)), lambda k: data.__setitem__(name, text[:k] + note) or
                        _total(value) <= STOP_LIMIT)
        if kept is None and name == "detail":
            del data[name]  # the diagnostic only; the full text is on stderr
            return _total(value) <= STOP_LIMIT
        data[name] = text[:kept or 0] + note
        return kept is not None

    for name in [name for name in lists if name not in used]:
        if shrink(name, 0):
            return value
    texts = ["detail"] + [name for name in reversed(list(entry["data"]))
                          if entry["data"][name] is _text and name not in used]
    for name in [name for name in texts if type(data.get(name)) is str]:
        if shorten(name, original_length if name == "detail" else None):
            return value
    for name in [name for name in lists if name in used]:
        if shrink(name, 1):
            return value
    raise ValueError("stop value does not fit even with every list and text shortened")


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
    over-long list keeps its head and records ``<field>_omitted``, added to the count the
    input already carries, so a made value can be made again. A value that cannot fill
    the exits it would list becomes ``unclassified`` instead."""
    entry = REGISTRY[(kind, reason)]
    clean_data, clean_source, dropped = {}, {}, []
    for name, value in (data or {}).items():
        check = entry["data"].get(name) or COMMON_DATA.get(name)
        if check is None and name.endswith("_omitted") and _listed(entry, name[:-len("_omitted")]):
            if _int(value):
                clean_data[name] = clean_data.get(name, 0) + value
            else:
                dropped.append(name)
            continue
        if check is None:
            raise KeyError(f"{kind}/{reason} has no data field {name}")
        if name == "detail" and type(value) is str:
            value = bounded(value)
        # A list keeps its longest head that passes (an unfit item ends it); a map of lists does
        # only when every key and item is well formed, else it is dropped like any unfit value.
        if _listed(entry, name) and not check(value) and (type(value) is list or check.shape(value)):
            kept = _head(value, check)
            clean_data[name + "_omitted"] = (clean_data.get(name + "_omitted", 0)
                                             + len(_items(value)) - len(_items(kept)))
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
    try:
        # Several bounded fields may still exceed the whole value's limit together.
        return _fit({"kind": kind, "reason": reason, "data": clean_data, "source": clean_source})
    except ValueError:
        return {"kind": "unclassified", "reason": "untyped_harness_error", "source": {},
                "data": {"error_type": "OversizedStop"}}


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
        if check is None and name.endswith("_omitted") and _listed(entry, name[:-len("_omitted")]):
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
        # The diagnostic is the last field added: fit the complete value, then check it.
        value = _fit(value, len(text))
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
              "exits": [_ExitFormatter().format(template, **fields) for template in active_exits(entry, value["data"])],
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
