"""A bounded wait inside the same process after a classified subscription-limit failure.

Only Claude roles, only inside the scheduling loop of a ``running`` run, only for a
``provider_quota/usage_limit`` failure whose judgment, accounting and exit are already
committed. The failure original proves the failure; a quota snapshot (harness.quota)
only supplies the reset time, and only for windows that apply to the failed model.

While waiting the process holds ``run.lock`` and never ``store.lock``; the status stays
``running`` and the ledger's ``quota_wait`` is only an observation, published in its own
save and removed in its own save on waking (or with any stop). It is never read to
decide anything: a process that dies never resumes a wait, and the next process removes
a leftover record when it enters execution, abandons or stops. On waking, the read-only
preconditions a manual ``resume`` checks run again (the same RunLifecycle functions),
then every scheduling gate. It is not a resume and never restarts a process.

The sleep uses the wall clock as its target and the monotonic clock as its bound, in
slices of at most a minute: a system suspend ends it once the target has passed, a
clock rollback cannot extend it. Signals interrupt it as they interrupt any call.
"""
from __future__ import annotations

import math
import re
import time

from . import stops
from .quota import EPOCH_MAX, NAME as SAMPLE_NAME, ROLES, _utc, bucket_label
from .services import RuntimeService
from .util import HarnessError

MARGIN_SECONDS = 60     # after the reset time; a design value, not measured
SLICE_SECONDS = 60      # the longest single sleep: a suspend overshoots by at most this much
WINDOW_MAX = 160
UNCERTAIN_MAX = 16
WAIT_KEYS = frozenset(("dispatch_id", "role", "window", "resets_at", "until", "recorded_at", "limit_seconds",
                       "sample"))
NOTE = "记录于 recorded_at；台账不证明 Harness 进程仍在等待（被强杀时遗留，下一次进入执行时清除）"
BUSY_NOTE = "台账记录了额度等待；记录不证明持锁的进程仍在等待"
_WORD = re.compile(r"[a-z]+|[0-9]+")


# ---------- pure: model display names and the window to wait for ----------

def _number(word):
    return word.isdigit() and (word == "0" or not word.startswith("0"))


def parse_display(text):
    """(family, version) for '[Claude] <family> [<n>[.<n>…]]'; None for anything else.

    Words are separated by spaces, dots, hyphens or underscores; every word is letters
    or digits only. Several groups, an unexplained extra part, a leading zero, only
    digits, an empty name: not parsed.
    """
    if type(text) is not str:
        return None
    words = [word for word in re.split(r"[\s._-]+", text.lower()) if word]
    if any(_WORD.fullmatch(word) is None for word in words):
        return None
    if words and words[0] == "claude":
        words = words[1:]
    if not words or not words[0].isalpha() or not all(_number(word) for word in words[1:]):
        return None
    return words[0], tuple(words[1:])


def parse_model(model):
    """(family, version, full) for ``claude-<family>-<n>[-<n>…][-YYYYMMDD]``; an alias has an empty version.

    A full ID drops its date suffix. Without the ``claude`` prefix or without a version
    it is an alias (``opus``): only its family is known. None when it does not parse.
    """
    if type(model) is not str:
        return None
    words = model.lower().split("-")
    if any(_WORD.fullmatch(word) is None for word in words):
        return None
    prefixed = words[0] == "claude"
    rest = words[1:] if prefixed else words
    if not rest or not rest[0].isalpha() or not all(_number(word) for word in rest[1:]):
        return None
    family, version = rest[0], rest[1:]
    if prefixed and len(version) > 1 and len(version[-1]) == 8:
        version = version[:-1]   # the date suffix of a full ID
    if not prefixed or not version:
        return family, (), False
    return family, tuple(version), True


def _applies(shown, model):
    """Same family, and the name has no version (the whole family) or exactly the model's version."""
    return shown[0] == model[0] and (not shown[1] or shown[1] == model[1])


def relation(display, failed, models):
    """How a weekly_scoped window's display name relates to the failed model ID.

    ``("applies", None)`` only for a unique match among the configured Claude models;
    ``("unrelated", None)`` only when the name provably names another family or another
    complete version of a full model ID; otherwise ``("uncertain", why)``.
    """
    shown = parse_display(display)
    if shown is None:
        return "uncertain", "unparsed_name"
    model = parse_model(failed)
    if model is None:
        return "uncertain", "unparsed_model"
    if _applies(shown, model):
        others = [parse_model(other) for other in sorted(set(models) - {failed})]
        if any(other is None or _applies(shown, other) for other in others):
            return "uncertain", "ambiguous"   # another configured model matches, or cannot be ruled out
        return "applies", None
    if not model[2]:
        return "uncertain", "alias"   # an alias proves neither its family's version nor another family
    if shown[0] != model[0]:
        return "unrelated", None
    left, right = shown[1], model[1]
    if left[:len(right)] != right and right[:len(left)] != left:
        return "unrelated", None      # another complete version
    return "uncertain", "version_prefix"


def choose(record, failed, models, now, limit, margin=MARGIN_SECONDS):
    """When to wake, or why not, from a normalized Claude quota sample. Pure.

    Only windows at 100 % or more count. ``session`` and ``weekly_all`` apply;
    ``weekly_scoped`` by ``relation``; any other kind is uncertain. Any uncertain full
    window: no wait. Otherwise the latest reset of the applying full windows, plus the
    margin, must lie in (now, now + limit].
    """
    if not isinstance(record, dict) or record.get("available") is not True:
        return {"wait": False, "reason": "snapshot_unavailable"}
    blocking, uncertain = [], []
    for window in record.get("windows") or []:
        if not window["used_percent"] >= 100:
            continue
        kind = window["kind"]
        if kind in ("session", "weekly_all"):
            blocking.append(window)
            continue
        related, why = relation(window.get("scope_model"), failed, models) if kind == "weekly_scoped" else (
            "uncertain", "unknown_kind")
        if related == "applies":
            blocking.append(window)
        elif related == "uncertain":
            uncertain.append(f"{bucket_label('claude', window)}: {why}")
    if uncertain:
        return {"wait": False, "reason": "window_uncertain", "uncertain": uncertain[:UNCERTAIN_MAX]}
    if not blocking:
        return {"wait": False, "reason": "no_blocking_window"}
    missing = next((window for window in blocking if window["resets_at"] is None), None)
    if missing is not None:
        return {"wait": False, "reason": "reset_unknown", "window": bucket_label("claude", missing)}
    chosen = max(blocking, key=lambda window: window["resets_at"])   # the first of equal ones
    until = chosen["resets_at"] + margin
    decision = {"window": bucket_label("claude", chosen), "resets_at": chosen["resets_at"], "until": until,
                "delay": until - now}
    if decision["delay"] <= 0:
        return {"wait": False, "reason": "reset_passed", **decision}
    if decision["delay"] > limit:
        return {"wait": False, "reason": "exceeds_limit", **decision}
    return {"wait": True, **decision}


# ---------- the ledger record: one shape check for publishing and loading ----------

def _epoch(value):
    return (type(value) is int or (type(value) is float and math.isfinite(value))) and 0 <= value <= EPOCH_MAX


def valid_record(value):
    """The only shape check of ``quota_wait``: run before it is published and when a ledger is loaded."""
    return (isinstance(value, dict) and set(value) == WAIT_KEYS
            and stops.SOURCE_TYPES["dispatch_id"](value["dispatch_id"])
            and type(value["role"]) is str and value["role"] in ROLES
            and type(value["window"]) is str and 0 < len(value["window"]) <= WINDOW_MAX
            and all(_epoch(value[key]) for key in ("resets_at", "until", "recorded_at"))
            and type(value["limit_seconds"]) is int and value["limit_seconds"] > 0
            and (value["sample"] is None or (type(value["sample"]) is str
                                             and SAMPLE_NAME.fullmatch(value["sample"]) is not None)))


def validate_ledger(run):
    """``quota_wait`` exists only while the run is ``running`` and has exactly the record shape."""
    if "quota_wait" in run and (run.get("status") != "running" or not valid_record(run["quota_wait"])):
        raise HarnessError("运行台账额度等待记录无效或与状态不符；保留原文件并核对证据")


def clear(run):
    """Remove a wait record (entering execution, abandoning, any stop); never read it first."""
    run.pop("quota_wait", None)


def project(run):
    """What status, watch, the dashboard and a busy lock show; never a proof of a live process."""
    if "quota_wait" not in run:
        return None
    value = run["quota_wait"]
    if run.get("status") != "running" or not valid_record(value):
        raise HarnessError("运行台账额度等待记录无效；使用 status --full 查看原始内容")
    return {"source_dispatch_id": value["dispatch_id"], "role": value["role"], "window": value["window"],
            "resets_at": _utc(value["resets_at"], "seconds"), "until": _utc(value["until"], "seconds"),
            "recorded_at": _utc(value["recorded_at"], "seconds"), "limit_seconds": value["limit_seconds"],
            "sample": value["sample"], "note": NOTE}


def busy_stop(run):
    """The ``busy/run_lock`` stop for resume/abandon, pointing to a recorded wait when the ledger has one."""
    try:
        view = project(run) if isinstance(run, dict) else None
    except HarnessError:
        view = None
    if view is None:
        return stops.make("busy", "run_lock")
    return stops.make("busy", "run_lock", data={"quota_wait_recorded": True, "quota_wait_until": view["until"],
                                                "quota_wait_recorded_at": view["recorded_at"],
                                                "quota_wait_note": BUSY_NOTE})


def _usage_limit(error):
    value = error.stop if isinstance(error, HarnessError) else None   # the attribute only (no "stop" literal, G6)
    return isinstance(value, dict) and (value.get("kind"), value.get("reason")) == ("provider_quota", "usage_limit")


class QuotaWait(RuntimeService):
    """The wait decision and the wait itself; ``waits`` (completed waits) lives in this process only."""

    def __init__(self, context, services=None):
        super().__init__(context, services)
        self.waits = 0

    def decline(self, error, reason):
        """A usage-limit stop raised where no wait is considered records why (``not_running``)."""
        if _usage_limit(error):
            self._annotate(error, {"reason": reason})

    def after_failure(self, error):
        """True when this failure was waited out and the wake-up checks passed; the loop then starts over.

        Otherwise False, with the decision written into the usage-limit stop's data (any
        other error is left untouched); the caller raises the same error object.
        """
        if not _usage_limit(error):
            return False
        decision = self._decide(error)
        record = None
        if decision["wait"]:
            record = self._record(error.stop, decision)
            if not valid_record(record):
                # Checked by the function the load uses; never shortened into a valid-looking value.
                decision = {**decision, "wait": False, "reason": "record_invalid"}
        if decision["wait"]:
            try:
                # The checks a stop is settled with: a run that would stop on its evidence does not wait.
                self.services.lifecycle.load_checks()
            except Exception:
                decision = {**decision, "wait": False, "reason": "load_checks_failed"}
        if not decision["wait"]:
            self._annotate(error, decision)
            return False
        self._wait(record, decision)
        return True

    def _decide(self, error):
        data = error.stop["data"]
        limit = self.context.config.data["runtime"]["quota_wait_max_seconds"]
        if data.get("provider") != "claude" or data.get("role") not in ROLES:
            return {"wait": False, "reason": "provider"}
        if not self.context.store.matches_commit():
            # Every fact a wait relies on is committed; the record's save then carries nothing else.
            return {"wait": False, "reason": "unsaved_state"}
        if limit == 0:
            return {"wait": False, "reason": "disabled", "limit": limit}
        if self.context.run["agent_dispatches"] >= self.context.config.data["runtime"]["max_agent_dispatches"]:
            return {"wait": False, "reason": "dispatch_budget", "limit": limit}
        sampler = self.services.quota
        if not sampler.enabled():
            return {"wait": False, "reason": "no_sampler", "limit": limit}
        if not sampler.no_live_model_process():
            return {"wait": False, "reason": "exit_unconfirmed", "limit": limit}
        record = sampler.failure_sample(error)   # attaches the sample fields to the stop; interrupts propagate
        commander = self.context.commander
        roles = [self.context.config.role(role, commander) for role in ROLES]
        models = {role["model"] for role in roles if role["cli"] == "claude"}
        failed = self.context.config.role(data["role"], commander)["model"]
        decision = choose(record, failed, models, time.time(), limit)
        return {**decision, "limit": limit, "sample": (record or {}).get("file")}

    def _record(self, value, decision):
        return {"dispatch_id": value["source"].get("dispatch_id"), "role": value["data"].get("role"),
                "window": decision["window"], "resets_at": decision["resets_at"], "until": decision["until"],
                "recorded_at": time.time(), "limit_seconds": decision["limit"], "sample": decision["sample"]}

    def _annotate(self, error, decision):
        value = error.stop
        data = {**value["data"], "wait_reason": decision["reason"], "waits": self.waits}
        if "window" in decision:
            data["window"] = decision["window"]
        if "resets_at" in decision:
            data["resets_at"] = _utc(decision["resets_at"], "seconds")
        if decision.get("delay", 0) > 0:
            data["wait_seconds"] = math.ceil(decision["delay"])
        if "limit" in decision:
            data["wait_limit_seconds"] = decision["limit"]
        if decision.get("uncertain"):
            data["uncertain_windows"] = decision["uncertain"]
        error.stop = stops.make(value["kind"], value["reason"], data=data, source=value["source"])

    def _wait(self, record, decision):
        run = self.context.run
        run["quota_wait"] = record
        try:
            self.context.save()   # only the observation record changes in this save
            self.context.observe("quota_wait_started", dispatch_id=record["dispatch_id"], role=record["role"],
                                 window=record["window"], resets_at=_utc(record["resets_at"], "seconds"),
                                 until=_utc(record["until"], "seconds"), wait_seconds=math.ceil(decision["delay"]),
                                 limit_seconds=record["limit_seconds"], sample=record["sample"])
            self._sleep_until(record["until"], decision["delay"])
        finally:
            # Never left in memory: an interrupt's stop is saved without it.
            clear(run)
        self.context.save()
        self.waits += 1
        self.context.observe("quota_wait_finished", dispatch_id=record["dispatch_id"], waits=self.waits,
                             woke_at=_utc(time.time(), "seconds"))
        sampler = self.services.quota
        if sampler.no_live_model_process():
            sampler.sample("claude", "wake")
        self.services.lifecycle.wake_checks()

    @staticmethod
    def _sleep_until(until, delay):
        """The wall-clock target ends it, the monotonic bound caps it; slices of at most a minute."""
        deadline = time.monotonic() + delay
        while True:
            remaining = min(deadline - time.monotonic(), until - time.time())
            if remaining <= 0:
                return
            time.sleep(min(SLICE_SECONDS, remaining))
