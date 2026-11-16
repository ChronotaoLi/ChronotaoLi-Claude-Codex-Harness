"""Account quota snapshots: two model-free CLI probes, their samples and the latest view.

Only observation. A probe is not a ledger dispatch: it runs in an empty temporary
directory, executes no tools and its result is read once in this process. Every
failure is "unavailable + reason"; nothing here changes scheduling, budgets,
receipts, acceptance or recovery. A user interrupt always propagates.

A probe owns its process group. On every exit path the whole group is sent SIGTERM
and, after a bounded grace (cut short by a pending interrupt), SIGKILL while the
leader is still unreaped, so the group ID cannot have been reused; after reaping the
group is only observed. Samples are separate, exclusively published files under
``runs/<id>/observations/quota/`` whose names are their total order: ledger revision,
phase, this process's start time, sequence.

Both interfaces are experimental (``claude -p /usage`` and ``codex app-server``'s
``account/rateLimits/read``); a changed interface is "unavailable", never a guess.
"""
from __future__ import annotations

import contextlib
import decimal
import json
import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import stops, util
from .services import RuntimeService
from .util import dump, strict_json_loads

QUERY_SECONDS = 5.0             # start to EOF (Claude) or to the id 1 answer (Codex)
CLEANUP_SECONDS = 3.0           # the timed part of cleanup: grace, reaping wait, group observation
TERM_GRACE_SECONDS = 0.5
OUTPUT_MAX_BYTES = 1024 * 1024
SAMPLE_MAX_BYTES = 64 * 1024
SAMPLE_VERSION = 1
REVISION_MAX = 9_999_999_999    # ten digits in the sample name
SEQUENCE_MAX = 9999
EPOCH_MAX = 253_402_300_799     # 9999-12-31T23:59:59Z
PERCENT_MAX = 10_000
WINDOWS_MAX, LIMITS_MAX, UNPARSED_MAX = 32, 16, 8

CLAUDE_ARGV = ("claude", "-p", "/usage", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
               "--setting-sources", "", "--strict-mcp-config", "--max-turns", "1")
CODEX_ARGV = ("codex", "app-server")
ARGV = {"claude": CLAUDE_ARGV, "codex": CODEX_ARGV}
PROVIDERS = ("claude", "codex")
ROLES = ("commander", "worker", "reviewer")
# Samples taken after a saved stop, pause, completion or plan approval wait, by the saved status.
END_TRIGGERS = {"blocked": "stop", "paused": "paused", "completed": "completed",
                "awaiting_approval": "awaiting_approval"}
# ``wake``: after a quota wait's clearing save (harness.quota_wait), before the wake-up checks.
TRIGGERS = ("before_use", "quota_failure", "wake", *END_TRIGGERS.values())
REASONS = stops.QUOTA_REASONS
TERMINATIONS = ("start_failed", "normal", "timeout", "output_limit", "interrupted", "error")
NAME = re.compile(r"^r([0-9]{10})-p([01])-([0-9]{8}T[0-9]{6}\.[0-9]{6}Z)-s([0-9]{4})-(claude|codex)-([a-z_]{1,24})\.json$")
QUOTA_DIR = ("observations", "quota")
NOTES = ("账号级观测：包含本 run 以外的使用（含驱动 Harness 的会话），不是本 run 的消耗",
         "无法识别账号是否换过；样本里没有可比较的账号标识")
_DECIMAL = re.compile(r"^-?[0-9]{1,30}(\.[0-9]{1,30})?$")
_AGENT = re.compile(r"^[A-Za-z0-9_.-]{1,64}/([0-9][0-9A-Za-z.+-]{0,40})")


class Shape(Exception):
    """A required structure is missing or a read field has the wrong type: the whole sample is unusable."""

    def __init__(self, field):
        self.field = str(field)[:200]
        super().__init__(self.field)


class Unavailable(Exception):
    """A well-formed answer that is not a usable snapshot (``reason`` and a bounded ``detail``)."""

    def __init__(self, reason, **detail):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


class _StartFailed(Exception):
    pass


class _QueryTimeout(Exception):
    pass


class _OutputLimit(Exception):
    pass


# ---------- small typed readers ----------

def _finite(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _epoch(value):
    return _finite(value) and 0 <= value <= EPOCH_MAX


def _text(value, limit):
    """A non-empty string of at most ``limit`` characters (identities)."""
    return type(value) is str and 0 < len(value) <= limit


def _string(value, limit):
    return type(value) is str and len(value) <= limit


def _utc(epoch, timespec="microseconds"):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec=timespec)


def _name_of(error):
    name = type(error).__name__
    return name if _text(name, 64) else "Exception"


def _optional(value, check, field):
    if value is None or check(value):
        return value
    raise Shape(field)


def _percent(value, field):
    if not _finite(value) or not 0 <= value <= PERCENT_MAX:
        raise Shape(field)
    return value


def _iso_epoch(value, field):
    """An ISO 8601 time with an explicit offset (``Z`` is ``+00:00``) as epoch seconds; None stays unknown."""
    if value is None:
        return None
    if type(value) is not str or len(value) > 64:
        raise Shape(field)
    try:
        moment = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise Shape(field) from None
    if moment.tzinfo is None or not _epoch(moment.timestamp()):
        raise Shape(field)
    return moment.timestamp()


def _scalar(value):
    if value is None or type(value) is bool or _finite(value) or _string(value, 64):
        return value
    return None


# ---------- normalization (pure) ----------

_ZERO_CALL = (("num_turns", lambda v: type(v) is int and v == 0),
              ("total_cost_usd", lambda v: _finite(v) and v == 0),
              ("duration_api_ms", lambda v: type(v) is int and v == 0),
              ("local_command", lambda v: v == "usage"))
_EXTRA_USAGE = ("is_enabled", "monthly_limit", "used_credits", "utilization", "currency")


def normalize_claude(messages, verdict):
    """Check the zero-call proof, then normalize ``/usage``'s stream-json lines.

    ``verdict`` receives ``cli_version`` and ``zero_call`` (with ``verified``: this
    check's own result) even when the probe is then unavailable. Raises
    ``Unavailable`` or ``Shape``.
    """
    version = None
    for message in messages:
        if message.get("type") == "system" and message.get("subtype") == "init":
            found = message.get("claude_code_version")
            version = found if _text(found, 64) else None
            break
    verdict["cli_version"] = version
    results = [message for message in messages if message.get("type") == "result"]
    if len(results) != 1:
        raise Unavailable("exited" if not results else "shape_mismatch", field="result")
    result = results[0]
    verdict["zero_call"] = {"verified": False, **{key: _scalar(result.get(key)) for key, _ in _ZERO_CALL}}
    for key, check in _ZERO_CALL:
        if not check(result.get(key)):
            raise Unavailable("not_zero_call", field="result." + key)
    verdict["zero_call"]["verified"] = True
    if result.get("is_error") is not False or result.get("subtype") != "success":
        raise Unavailable("provider_error")
    reports = [message["usage_report"] for message in messages if "usage_report" in message]
    if len(reports) != 1 or not isinstance(reports[0], dict):
        raise Shape("usage_report")
    rate = reports[0].get("rate_limits")
    if not isinstance(rate, dict):
        raise Shape("usage_report.rate_limits")
    if not isinstance(rate.get("limits"), list) or len(rate["limits"]) > WINDOWS_MAX:
        raise Shape("usage_report.rate_limits.limits")
    windows, seen = [], set()
    for index, row in enumerate(rate["limits"]):
        at = f"usage_report.rate_limits.limits[{index}]"
        if not isinstance(row, dict):
            raise Shape(at)
        if not _text(row.get("kind"), 64):
            raise Shape(at + ".kind")
        if "percent" not in row:
            raise Shape(at + ".percent")
        scope = row.get("scope")
        if scope is not None and (not isinstance(scope, dict)
                                  or len(json.dumps(scope, sort_keys=True, ensure_ascii=True)) > 512):
            raise Shape(at + ".scope")
        model = None
        if isinstance(scope, dict) and isinstance(scope.get("model"), dict):
            model = _optional(scope["model"].get("display_name"), lambda v: _text(v, 64),
                              at + ".scope.model.display_name")
        window = {"kind": row["kind"], "group": _optional(row.get("group"), lambda v: _string(v, 64), at + ".group"),
                  "scope": scope, "scope_model": model, "used_percent": _percent(row["percent"], at + ".percent"),
                  "resets_at": _iso_epoch(row.get("resets_at"), at + ".resets_at"),
                  "resets_at_raw": row.get("resets_at"),
                  "is_active": _optional(row.get("is_active"), lambda v: type(v) is bool, at + ".is_active"),
                  "severity": _optional(row.get("severity"), lambda v: _string(v, 32), at + ".severity")}
        key = bucket_key("claude", window)
        if key in seen:
            raise Shape(at + " duplicate bucket")
        seen.add(key)
        windows.append(window)
    extra = rate.get("extra_usage")
    if extra is not None:
        if not isinstance(extra, dict):
            raise Shape("usage_report.rate_limits.extra_usage")
        checks = {"is_enabled": lambda v: type(v) is bool, "currency": lambda v: _string(v, 64)}
        amount = lambda v: _finite(v) or _string(v, 64)
        extra = {key: _optional(extra.get(key), checks.get(key, amount), "usage_report.rate_limits.extra_usage." + key)
                 for key in _EXTRA_USAGE}
    return {"cli_version": version, "windows": windows, "limits": [], "ordinary_usage_allowed": None,
            "extra_usage": extra, "unparsed": []}


def _reply(message, method):
    """The ``result`` object of one JSON-RPC answer; an ``error`` object is a provider error."""
    if isinstance(message.get("error"), dict):
        code = message["error"].get("code")
        raise Unavailable("provider_error", method=method,
                          code=code if type(code) is int and -2**63 <= code < 2**63 else None)
    if not isinstance(message.get("result"), dict):
        raise Shape(method + ".result")
    return message["result"]


def _codex_snapshot(value, at):
    if not isinstance(value, dict):
        raise Shape(at)
    limit_id = value.get("limitId")
    if not _text(limit_id, 64):
        raise Shape(at + ".limitId")
    windows = []
    for slot in ("primary", "secondary"):
        window, where = value.get(slot), at + "." + slot
        if window is None:
            continue   # no window in this slot: "not reported", not a mismatch
        if not isinstance(window, dict) or "usedPercent" not in window:
            raise Shape(where)
        minutes = _optional(window.get("windowDurationMins"), lambda v: type(v) is int and 0 < v <= 10**8,
                            where + ".windowDurationMins")
        resets = _optional(window.get("resetsAt"), _epoch, where + ".resetsAt")
        windows.append({"limit_id": limit_id, "slot": slot, "window_minutes": minutes,
                        "used_percent": _percent(window["usedPercent"], where + ".usedPercent"),
                        "resets_at": resets, "resets_at_raw": resets})
    credits = value.get("credits")
    if credits is not None:
        if not isinstance(credits, dict):
            raise Shape(at + ".credits")
        flag = lambda v: type(v) is bool
        credits = {"has_credits": _optional(credits.get("hasCredits"), flag, at + ".credits.hasCredits"),
                   "unlimited": _optional(credits.get("unlimited"), flag, at + ".credits.unlimited"),
                   "balance": _optional(credits.get("balance"), lambda v: type(v) is str and _DECIMAL.fullmatch(v),
                                        at + ".credits.balance")}
    limit = {"limit_id": limit_id,
             "limit_name": _optional(value.get("limitName"), lambda v: _string(v, 128), at + ".limitName"),
             "rate_limit_reached_type": _optional(value.get("rateLimitReachedType"), lambda v: _string(v, 64),
                                                  at + ".rateLimitReachedType"),
             "spend_control_reached": _optional(value.get("spendControlReached"), lambda v: type(v) is bool,
                                                at + ".spendControlReached"),
             "credits": credits}
    return limit, windows


def normalize_codex(initialize, answer):
    """Normalize the ``initialize`` and ``account/rateLimits/read`` answers. Raises ``Unavailable`` or ``Shape``."""
    started = _reply(initialize, "initialize")
    result = _reply(answer, "account/rateLimits/read")
    agent = started.get("userAgent")
    match = _AGENT.match(agent) if type(agent) is str else None
    if not isinstance(result.get("rateLimits"), dict):
        raise Shape("result.rateLimits")
    limit, windows = _codex_snapshot(result["rateLimits"], "result.rateLimits")
    limits, unparsed = [limit], []
    more = result.get("rateLimitsByLimitId")
    if more is not None:
        # Shape not verified on a real CLI: a section that does not read is ignored, the main snapshot stays.
        try:
            if not isinstance(more, dict) or len(more) > LIMITS_MAX:
                raise Shape("result.rateLimitsByLimitId")
            extra_limits, extra_windows = [], []
            for key, item in more.items():
                other, other_windows = _codex_snapshot(item, "result.rateLimitsByLimitId")
                if other["limit_id"] != limit["limit_id"]:
                    extra_limits.append(other)
                    extra_windows.extend(other_windows)
            keys = [bucket_key("codex", window) for window in windows + extra_windows]
            if (len(set(keys)) != len(keys) or len(limits) + len(extra_limits) > LIMITS_MAX
                    or len(keys) > WINDOWS_MAX):
                raise Shape("result.rateLimitsByLimitId")
            limits += extra_limits
            windows += extra_windows
        except Shape:
            unparsed.append("result.rateLimitsByLimitId")
    return {"cli_version": match.group(1) if match else None, "windows": windows, "limits": limits,
            "ordinary_usage_allowed": _optional(result.get("ordinaryUsageAllowed"), lambda v: type(v) is bool,
                                                "result.ordinaryUsageAllowed"),
            "extra_usage": None, "unparsed": unparsed}


# ---------- the probe process ----------

class ProbeSignals:
    """Record SIGINT/SIGTERM/SIGHUP for the whole life of a probe's resources; raise them only where safe.

    Entered before the temporary directory is created and left after the cleanup
    removed it, so no signal can land between owning a resource (directory, process
    group) and the cleanup knowing about it, nor skip any step of the cleanup. Inside,
    a signal only sets ``captured``: ``checkpoint()`` turns it into ``KeyboardInterrupt``
    at the points the probe chooses (before starting the process, while reading or
    writing), and a pending one cuts the cleanup's grace short. On leaving, handlers
    are restored with the signals blocked and signals pending at the restore are
    collected too (the deferral of ``process_runtime.defer_cleanup_signals()``); then,
    unless the exception leaving is already a user interrupt (its own object
    propagates), a captured signal is raised as ``KeyboardInterrupt``: neither the
    probe's ordinary error (timeout, shape) nor an outer exception being propagated
    can swallow it. Main thread only; elsewhere nothing is deferred.
    """
    SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def __init__(self):
        self.captured, self.old = [], {}
        self.main = threading.current_thread() is threading.main_thread()

    def _remember(self, signum, frame):
        if not self.captured:
            self.captured.append(signum)

    def pending(self):
        return bool(self.captured)

    def checkpoint(self):
        if self.captured:
            raise KeyboardInterrupt(f"quota probe signal {self.captured[0]}")

    def __enter__(self):
        if self.main:
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, self.SIGNALS)
            try:
                for sig in self.SIGNALS:
                    self.old[sig] = signal.signal(sig, self._remember)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        return self

    def __exit__(self, kind, error, traceback):
        interrupted = isinstance(error, (KeyboardInterrupt, SystemExit))
        if self.main:
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, self.SIGNALS)
            try:
                for sig, handler in self.old.items():
                    signal.signal(sig, handler)
                for sig in signal.sigpending().intersection(self.SIGNALS):
                    self._remember(signal.sigwait({sig}), None)
            finally:
                try:
                    signal.pthread_sigmask(signal.SIG_SETMASK, mask)
                except KeyboardInterrupt:
                    if not interrupted:  # a later request, after the restore; an existing interrupt is kept
                        raise
        if self.captured and not interrupted:
            raise KeyboardInterrupt(f"quota probe signal {self.captured[0]}")
        return False


def _termination(primary):
    if primary is None or isinstance(primary, (Unavailable, Shape)):
        return "normal"
    if isinstance(primary, _QueryTimeout):
        return "timeout"
    if isinstance(primary, _OutputLimit):
        return "output_limit"
    if isinstance(primary, (KeyboardInterrupt, SystemExit)):
        return "interrupted"
    return "error"


class ProbeProcess:
    """One owned probe process: empty temporary cwd, its own process group, a query deadline and an output cap.

    Before the SIGKILL of cleanup nothing here may reap the leader: no ``poll``,
    ``wait``, ``communicate``, ``send_signal``, ``terminate``, ``kill``, ``with Popen``
    or ``os.waitpid``. An unreaped leader holds its PID, so the group ID it names
    cannot have been reused while the group is signalled.
    """

    def __init__(self, argv, env, *, interactive):
        self.argv, self.env, self.interactive = list(argv), env, interactive
        self.root = self.process = self.selector = self.signals = None
        self.buffer, self.received, self.eof = b"", 0, False
        self.started_at = time.monotonic()
        self.deadline = self.started_at + QUERY_SECONDS
        self.facts = {"started": False, "exit_code": None, "exit_confirmed": True, "kill_sent": False,
                      "temp_removed": True, "termination": "start_failed", "duration_ms": 0}

    def run(self, exchange):
        # One signal boundary from before the directory exists until the cleanup removed it:
        # a resource is recorded on ``self`` in the same uninterruptible step that acquires it.
        with ProbeSignals() as self.signals:
            primary = None
            try:
                self.root = Path(tempfile.mkdtemp(prefix="harness-quota-"))
                (self.root / "cwd").mkdir()
                self.signals.checkpoint()   # an interrupt so far: no process is started
                self._start()
                return exchange(self)
            except BaseException as error:
                primary = error
                raise
            finally:
                self._cleanup(primary)

    def _start(self):
        try:
            process = subprocess.Popen(
                self.argv, cwd=self.root / "cwd", env=self.env,
                stdin=subprocess.PIPE if self.interactive else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        except (OSError, ValueError) as error:
            raise _StartFailed(_name_of(error)) from error
        self.process = process
        self.facts.update(started=True, exit_confirmed=False, termination="normal")
        os.set_blocking(self.process.stdout.fileno(), False)
        self.signals.checkpoint()

    def send(self, message):
        self.signals.checkpoint()
        try:
            self.process.stdin.write((json.dumps(message, ensure_ascii=True) + "\n").encode("ascii"))
            self.process.stdin.flush()
        except BrokenPipeError:
            raise Unavailable("exited") from None

    def line(self):
        """The next stdout line, or None at EOF; raises at the query deadline or the output cap."""
        while True:
            self.signals.checkpoint()
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                return line
            if self.eof:
                line, self.buffer = self.buffer, b""
                return line or None
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise _QueryTimeout()
            if self.selector is None:
                self.selector = selectors.DefaultSelector()
                self.selector.register(self.process.stdout.fileno(), selectors.EVENT_READ)
            if not self.selector.select(min(remaining, 0.1)):
                continue
            try:
                chunk = os.read(self.process.stdout.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                self.eof = True
                continue
            self.received += len(chunk)
            if self.received > OUTPUT_MAX_BYTES:
                raise _OutputLimit()
            self.buffer += chunk

    def _cleanup(self, primary):
        """Input closed, group signalled, leader reaped, group observed, descriptors and directory gone.

        Runs inside the probe's signal boundary (``run``), from its first line.
        """
        if self.process is not None:
            self.facts["termination"] = _termination(primary)
        try:
            if self.process is not None:
                self._stop_group(lambda: self.signals.pending() or isinstance(primary, (KeyboardInterrupt, SystemExit)))
        finally:
            if self.selector is not None:
                self.selector.close()
            if self.process is not None:
                for stream in (self.process.stdin, self.process.stdout):
                    if stream is not None:
                        with contextlib.suppress(OSError):
                            stream.close()
            if self.root is not None:
                shutil.rmtree(self.root, ignore_errors=True)
                self.facts["temp_removed"] = not os.path.lexists(self.root)
            self.facts["duration_ms"] = int((time.monotonic() - self.started_at) * 1000)

    def _stop_group(self, interrupted):
        pgid = self.process.pid
        deadline = time.monotonic() + CLEANUP_SECONDS

        def send(sig):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                pass  # EPERM: only zombies waiting to be reaped are left in the group
        if self.process.stdin is not None:
            with contextlib.suppress(OSError):
                self.process.stdin.close()
        send(signal.SIGTERM)
        grace = min(time.monotonic() + TERM_GRACE_SECONDS, deadline)
        while time.monotonic() < grace and not interrupted():
            time.sleep(0.01)
        # Unconditional: the data is read, the query is read-only, nothing needs saving;
        # descendants that ignore SIGTERM end here.
        send(signal.SIGKILL)
        self.facts["kill_sent"] = True
        try:
            self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        self.facts["exit_code"] = self.process.returncode
        # From here on only observe: the group ID is never signalled again.
        while True:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                self.facts["exit_confirmed"] = self.process.returncode is not None
                return
            except PermissionError:
                pass  # still present
            if time.monotonic() >= deadline:
                self.facts["exit_confirmed"] = False
                return
            time.sleep(0.01)


def _claude_exchange(probe):
    messages = []
    while (line := probe.line()) is not None:
        if not line.strip():
            continue
        try:
            value = strict_json_loads(line)
        except (ValueError, UnicodeError, RecursionError):
            raise Shape("stdout line") from None  # stream-json is JSON on every line
        if not isinstance(value, dict):
            raise Shape("stdout line")
        messages.append(value)
    return messages


def _codex_exchange(probe):
    """Exactly three messages; notifications, server requests and other lines are ignored."""
    def answer(ident, method):
        while True:
            line = probe.line()
            if line is None:
                raise Unavailable("exited")
            try:
                value = strict_json_loads(line)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if (isinstance(value, dict) and "method" not in value
                    and type(value.get("id")) is int and value["id"] == ident):
                _reply(value, method)
                return value
    probe.send({"id": 0, "method": "initialize",
                "params": {"clientInfo": {"name": "personal-harness", "title": "Personal Harness quota probe",
                                          "version": "0"}}})
    initialize = answer(0, "initialize")
    probe.send({"method": "initialized"})
    probe.send({"id": 1, "method": "account/rateLimits/read"})
    return initialize, answer(1, "account/rateLimits/read")


def _blank(provider, command):
    return {"provider": provider, "source": {"command": list(command), "cli_version": None},
            "available": False, "reason": None, "detail": None, "process": None, "zero_call": None,
            "windows": [], "limits": [], "ordinary_usage_allowed": None, "extra_usage": None, "unparsed": []}


def snapshot(provider, env):
    """Run one probe and normalize it: the observed part of a sample.

    Never raises except a user interrupt (``KeyboardInterrupt``/``SystemExit``);
    every other failure is ``available=false`` with a reason.
    """
    record = _blank(provider, ARGV[provider])
    record["queried_at"] = time.time()
    verdict, probe = {}, None
    try:
        probe = ProbeProcess(ARGV[provider], env, interactive=provider == "codex")
        if provider == "claude":
            data = normalize_claude(probe.run(_claude_exchange), verdict)
        else:
            data = normalize_codex(*probe.run(_codex_exchange))
        record["source"]["cli_version"] = data.pop("cli_version")
        record.update(data, available=True)
    except _StartFailed:
        record["reason"] = "not_installed"
    except _QueryTimeout:
        record["reason"] = "timeout"
    except _OutputLimit:
        record["reason"] = "output_limit"
    except Unavailable as error:
        record.update(reason=error.reason, detail=dict(error.detail) or None)
    except Shape as error:
        record.update(reason="shape_mismatch", detail={"field": error.field})
    except Exception as error:
        record.update(reason="internal_error", detail={"error_type": _name_of(error)})
    record["process"] = dict(probe.facts) if probe is not None else None
    if provider == "claude":
        record["zero_call"] = verdict.get("zero_call")
        record["source"]["cli_version"] = record["source"]["cli_version"] or verdict.get("cli_version")
    if record["reason"] == "exited" and record["process"] and type(record["process"]["exit_code"]) is int:
        record["detail"] = {**(record["detail"] or {}), "exit_code": record["process"]["exit_code"]}
    record["observed_at"] = time.time()
    return record


# ---------- samples ----------

def sample_name(record):
    """The file name that is the sample's total order; ValueError when a field does not fit it."""
    revision, sequence = record["ledger_revision"], record["sequence"]
    if type(revision) is not int or not 0 <= revision <= REVISION_MAX:
        raise ValueError("ledger revision does not fit the ten-digit sample name")
    if type(sequence) is not int or not 1 <= sequence <= SEQUENCE_MAX:
        raise ValueError("sequence does not fit the sample name")
    if not _epoch(record["process_started_at"]):
        raise ValueError("process start time out of range")
    stamp = datetime.fromtimestamp(record["process_started_at"], tz=timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    phase = 1 if record["trigger"] == "before_use" else 0
    name = f"r{revision:010d}-p{phase}-{stamp}-s{sequence:04d}-{record['provider']}-{record['trigger']}.json"
    if not NAME.fullmatch(name):
        raise ValueError("sample name")
    return name


def publish_new_json(path, value):
    """Atomic and exclusive: written and synced under a temporary name, then linked; never replaces a file."""
    data = (dump(value) + "\n").encode("utf-8")
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    # The temporary name is removed on every path; an interrupt is raised after that.
    with ProbeSignals():
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temp, path)   # FileExistsError when the name exists
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)


def bucket_label(provider, window):
    if provider == "claude":
        return window["kind"] + (f"（{window['scope_model']}）" if window.get("scope_model") else "")
    minutes = window.get("window_minutes")
    return f"{window['limit_id']}.{window['slot']}" + (f"（{minutes} 分钟）" if minutes else "")


def bucket_key(provider, window):
    """A quota bucket's identity: Claude (kind, canonical scope), Codex (limit, slot, window length)."""
    if provider == "claude":
        return ("claude", window["kind"],
                json.dumps(window.get("scope"), sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return ("codex", window["limit_id"], window["slot"], window.get("window_minutes"))


def full_windows(record):
    """The snapshot's windows at or above 100 %, as listed: an account observation, not the failure's cause."""
    if not record.get("available"):
        return []
    return [{"bucket": bucket_label(record["provider"], window), "used_percent": window["used_percent"],
             "resets_at": _utc(window["resets_at"], "seconds") if window["resets_at"] is not None else None}
            for window in record["windows"] if window["used_percent"] >= 100]


def stop_fields(record):
    """The optional ``provider_quota`` stop data for a failure sample."""
    published = record.get("file")
    fields = {"quota_snapshot": ("not_published" if published is None
                                 else "available" if record["available"] else "unavailable")}
    if published is not None:
        fields["quota_sample"] = published
    if not record["available"]:
        fields["quota_reason"] = record["reason"]
    full = full_windows(record)
    if full:
        fields["full_windows"] = full
    return fields


def _exit_unconfirmed(value):
    return value["kind"] == "process_unconfirmed" or (
        (value["kind"], value["reason"]) == ("evidence", "process_incomplete")
        and value["data"].get("exit_unconfirmed") is True)


class QuotaSampler(RuntimeService):
    """The sampling points of one run/resume process. Its state is in memory only.

    Only a backend with ``quota_snapshot`` (the real ``CLIBackend``) samples; for any
    other backend every entry point does nothing. At most five probes per process:
    before each provider's first use, one for a quota failure, and after a saved stop,
    pause, completion or plan approval wait for each provider this process used; a
    quota wait (harness.quota_wait) adds one failure sample per waited-out failure and
    one wake sample per wait, both bounded by the dispatch budget.
    """

    def __init__(self, context, services=None):
        super().__init__(context, services)
        self.before, self.used, self.disabled = set(), set(), {}
        self.sequence, self.final = 0, None
        self.process_started_at = time.time()

    def enabled(self):
        return callable(getattr(self.context.backend, "quota_snapshot", None))

    def no_live_model_process(self):
        """The runtime's own exit gate on the in-memory ledger, read-only; any failure means 'not confirmed'."""
        try:
            util.validate_command_directories(self.context.run["dispatches"], self.context.directory)
            util.ensure_dispatches_exited(self.context.run["dispatches"], observe=util.group_alive)
            return True
        except Exception:
            return False

    def before_use(self, provider, role):
        """Before this process's first call to ``provider`` (its exit gate has just passed)."""
        if not self.enabled() or provider not in PROVIDERS or provider in self.before:
            return
        self.before.add(provider)
        self.sample(provider, "before_use", role=role)

    def mark_used(self, provider):
        if self.enabled() and provider in PROVIDERS:
            self.used.add(provider)

    def terminal_saved(self):
        """Called right after a stop, pause, completion or approval wait was committed."""
        self.final = (self.context.run.get("status"), self.context.run.get("store_revision"))

    def annotate_failure(self, error):
        """Give a provider_quota stop its failure sample, before the stop is settled and saved.

        Returns the error to stop with: the same object (its ``stop`` rebuilt with the
        sample fields when one was taken), or the user interrupt that arrived during
        the probe.
        """
        try:
            self.failure_sample(error)
        except (KeyboardInterrupt, SystemExit) as interrupt:
            return interrupt
        return error

    def failure_sample(self, error):
        """Take a provider_quota failure's sample once and attach its fields to ``error.stop``.

        The record (with ``file``), or None when the error does not qualify: not a
        provider_quota stop of a known provider, already sampled (``quota_snapshot`` in
        its data), no sampler, or a model process still alive. A user interrupt raises.
        """
        if not self.enabled() or not isinstance(error, util.HarnessError):
            return None
        value = error.stop
        if not isinstance(value, dict) or value.get("kind") != "provider_quota":
            return None
        data, source = dict(value.get("data") or {}), dict(value.get("source") or {})
        provider = data.get("provider")
        if provider not in PROVIDERS or "quota_snapshot" in data or not self.no_live_model_process():
            return None
        dispatch_id = source.get("dispatch_id")
        record = self.sample(provider, "quota_failure", dispatch_id=dispatch_id if type(dispatch_id) is str else None)
        self.used.discard(provider)  # the stop that follows does not sample this provider again
        try:
            error.stop = stops.make(value["kind"], value["reason"], data={**data, **stop_fields(record)}, source=source)
        except Exception:
            pass  # the stop stays as constructed; the sample file is an unreferenced observation
        return record

    def end_of_call(self):
        """From ``Scheduler.execute``'s finally: sample each used provider once if the terminal state was saved.

        Only user interrupts leave; any other failure here is an observation gap and
        never replaces the exception being propagated.
        """
        final, self.final = self.final, None
        used, self.used = sorted(self.used), set()
        if final is None or not used or not self.enabled():
            return
        try:
            current = sys.exc_info()[1]
            if isinstance(current, (KeyboardInterrupt, SystemExit)):
                return
            status, revision = final
            if (status not in END_TRIGGERS or status != self.context.run.get("status")
                    or revision != self.context.run.get("store_revision")):
                return
            if current is not None and _exit_unconfirmed(stops.of(current)):
                return
            if not self.no_live_model_process():
                return
            for provider in used:
                self.sample(provider, END_TRIGGERS[status])
        except Exception:
            return

    def sample(self, provider, trigger, *, dispatch_id=None, role=None):
        """Take, publish and announce one sample; the record with ``file`` (published name or None).

        The caller has checked that no model process is alive. Only a user interrupt raises.
        """
        self.sequence += 1
        if provider in self.disabled:
            observed = _fallback(provider, "probe_disabled", {"disabled_by": self.disabled[provider]})
        else:
            try:
                observed = self.context.backend.quota_snapshot(provider)
                if not isinstance(observed, dict):
                    raise TypeError("quota snapshot is not an object")
            except Exception as error:
                observed = _fallback(provider, "internal_error", {"error_type": _name_of(error)})
        operation = getattr(self.context.operation, "id", None) if self.context.operation is not None else None
        head = {"version": SAMPLE_VERSION, "provider": provider, "trigger": trigger, "dispatch_id": dispatch_id,
                "role": role, "operation_id": operation, "ledger_revision": self.context.run.get("store_revision"),
                "process_started_at": self.process_started_at, "sequence": self.sequence}
        record = _assemble(head, observed)
        try:
            name = sample_name(record)
        except (KeyError, TypeError, ValueError, OverflowError):
            name = None
        # Disabling reads what the probe observed, whatever happens to the record afterwards.
        process = observed.get("process") if isinstance(observed.get("process"), dict) else {}
        zero = observed.get("zero_call") if isinstance(observed.get("zero_call"), dict) else {}
        if process.get("started") is True and (process.get("exit_confirmed") is not True
                                               or (provider == "claude" and zero.get("verified") is not True)):
            self.disabled.setdefault(provider, name)
        file, failure = None, None
        try:
            if name is None:
                raise ValueError("sample name")
            publish_new_json(self._folder() / name, record)
            file = name
        except Exception as error:
            failure = _name_of(error)
        self.context.observe("quota_sampled", provider=provider, trigger=trigger, available=record["available"],
                             reason=record["reason"], sample=file, dispatch_id=dispatch_id,
                             **({"write_error": failure} if failure else {}))
        return {**record, "file": file}

    def _folder(self):
        folder = self.context.directory
        for part in QUOTA_DIR:
            folder = folder / part
            with contextlib.suppress(FileExistsError):
                folder.mkdir()
            if not stat.S_ISDIR(os.lstat(folder).st_mode):
                raise NotADirectoryError(str(folder))
        return folder


def _fallback(provider, reason, detail):
    now = time.time()
    return {**_blank(provider, ()), "queried_at": now, "observed_at": now, "reason": reason, "detail": detail}


def _record(head, observed):
    record = {**head, "queried_at": observed.get("queried_at"), "observed_at": observed.get("observed_at"),
              "observed_at_utc": None, **{key: observed.get(key) for key in _BODY}}
    if _epoch(record["observed_at"]):
        record["observed_at_utc"] = _utc(record["observed_at"])
    return record


def _assemble(head, observed):
    """The full record in schema order. One that would not read back becomes ``internal_error``
    (``InvalidSample``); one over the size cap keeps its facts as ``shape_mismatch``."""
    record = _record(head, observed)
    try:
        validate_sample(record)
    except (TypeError, ValueError):
        return _record(head, _fallback(head["provider"], "internal_error", {"error_type": "InvalidSample"}))
    if len((dump(record) + "\n").encode("utf-8")) > SAMPLE_MAX_BYTES:
        record = _record(head, {**_blank(head["provider"], ()), "source": record["source"],
                                "process": record["process"], "zero_call": record["zero_call"],
                                "queried_at": record["queried_at"], "observed_at": record["observed_at"],
                                "reason": "shape_mismatch", "detail": {"field": "sample_too_large"}})
    return record


_BODY = ("source", "available", "reason", "detail", "process", "zero_call", "windows", "limits",
         "ordinary_usage_allowed", "extra_usage", "unparsed")
_KEYS = ("version", "provider", "trigger", "dispatch_id", "role", "operation_id", "ledger_revision",
         "process_started_at", "sequence", "queried_at", "observed_at", "observed_at_utc", *_BODY)
_DETAIL = {"field": lambda v: _string(v, 200), "exit_code": lambda v: type(v) is int and -2**31 <= v < 2**31,
           "method": lambda v: _text(v, 64), "code": lambda v: v is None or (type(v) is int and -2**63 <= v < 2**63),
           "disabled_by": lambda v: v is None or (type(v) is str and NAME.fullmatch(v) is not None),
           "error_type": lambda v: _text(v, 64)}
_PROCESS = {"started": lambda v: type(v) is bool, "exit_code": lambda v: v is None or type(v) is int and -2**31 <= v < 2**31,
            "exit_confirmed": lambda v: type(v) is bool, "kill_sent": lambda v: type(v) is bool,
            "temp_removed": lambda v: type(v) is bool, "termination": lambda v: v in TERMINATIONS,
            "duration_ms": lambda v: type(v) is int and 0 <= v <= 10**9}
_ZERO = ("verified", "num_turns", "total_cost_usd", "duration_api_ms", "local_command")
_CLAUDE_WINDOW = {"kind": lambda v: _text(v, 64), "group": lambda v: v is None or _string(v, 64),
                  "scope": lambda v: v is None or (isinstance(v, dict)
                                                   and len(json.dumps(v, sort_keys=True, ensure_ascii=True)) <= 512),
                  "scope_model": lambda v: v is None or _text(v, 64),
                  "used_percent": lambda v: _finite(v) and 0 <= v <= PERCENT_MAX,
                  "resets_at": lambda v: v is None or _epoch(v),
                  "resets_at_raw": lambda v: v is None or _string(v, 64),
                  "is_active": lambda v: v is None or type(v) is bool,
                  "severity": lambda v: v is None or _string(v, 32)}
_CODEX_WINDOW = {"limit_id": lambda v: _text(v, 64), "slot": lambda v: v in ("primary", "secondary"),
                 "window_minutes": lambda v: v is None or (type(v) is int and 0 < v <= 10**8),
                 "used_percent": _CLAUDE_WINDOW["used_percent"], "resets_at": _CLAUDE_WINDOW["resets_at"],
                 "resets_at_raw": lambda v: v is None or _epoch(v)}
_CREDITS = {"has_credits": lambda v: v is None or type(v) is bool, "unlimited": lambda v: v is None or type(v) is bool,
            "balance": lambda v: v is None or (type(v) is str and _DECIMAL.fullmatch(v) is not None)}
_LIMIT = {"limit_id": lambda v: _text(v, 64), "limit_name": lambda v: v is None or _string(v, 128),
          "rate_limit_reached_type": lambda v: v is None or _string(v, 64),
          "spend_control_reached": lambda v: v is None or type(v) is bool,
          "credits": lambda v: v is None or _fields(v, _CREDITS)}
_EXTRA = {"is_enabled": lambda v: v is None or type(v) is bool,
          "monthly_limit": lambda v: v is None or _finite(v) or _string(v, 64),
          "used_credits": lambda v: v is None or _finite(v) or _string(v, 64),
          "utilization": lambda v: v is None or _finite(v) or _string(v, 64),
          "currency": lambda v: v is None or _string(v, 64)}


def _fields(value, checks, *, exact=True):
    return (isinstance(value, dict) and (set(value) == set(checks) if exact else set(value) <= set(checks))
            and all(checks[key](item) for key, item in value.items()))


def validate_sample(value):
    """Every field of a sample against the schema (the reader's check); ValueError names the first failure."""
    def require(condition, field):
        if not condition:
            raise ValueError(field)
    require(isinstance(value, dict) and set(value) == set(_KEYS), "keys")
    provider, available = value["provider"], value["available"]
    require(type(value["version"]) is int and value["version"] == SAMPLE_VERSION, "version")
    require(type(provider) is str and provider in PROVIDERS, "provider")
    require(type(value["trigger"]) is str and value["trigger"] in TRIGGERS, "trigger")
    require(value["dispatch_id"] is None or _text(value["dispatch_id"], 128), "dispatch_id")
    require(value["role"] is None or (type(value["role"]) is str and value["role"] in ROLES), "role")
    require(value["operation_id"] is None or _text(value["operation_id"], 128), "operation_id")
    require(type(value["ledger_revision"]) is int and 0 <= value["ledger_revision"] <= REVISION_MAX, "ledger_revision")
    require(type(value["sequence"]) is int and 1 <= value["sequence"] <= SEQUENCE_MAX, "sequence")
    for key in ("process_started_at", "queried_at", "observed_at"):
        require(_epoch(value[key]), key)
    require(_text(value["observed_at_utc"], 40), "observed_at_utc")
    source = value["source"]
    require(isinstance(source, dict) and set(source) == {"command", "cli_version"}
            and type(source["command"]) is list and len(source["command"]) <= 16
            and all(_string(item, 64) for item in source["command"])
            and (source["cli_version"] is None or _text(source["cli_version"], 64)), "source")
    require(type(available) is bool, "available")
    require(value["reason"] is None if available else (type(value["reason"]) is str and value["reason"] in REASONS),
            "reason")
    require(value["detail"] is None or _fields(value["detail"], _DETAIL, exact=False), "detail")
    require(value["process"] is None or _fields(value["process"], _PROCESS), "process")
    zero = value["zero_call"]
    require(zero is None or (provider == "claude" and isinstance(zero, dict) and set(zero) == set(_ZERO)
                             and type(zero["verified"]) is bool
                             and all(_scalar(zero[key]) is zero[key] for key in _ZERO[1:])), "zero_call")
    windows, limits = value["windows"], value["limits"]
    window_checks = _CLAUDE_WINDOW if provider == "claude" else _CODEX_WINDOW
    require(type(windows) is list and len(windows) <= WINDOWS_MAX
            and all(_fields(window, window_checks) for window in windows), "windows")
    keys = [bucket_key(provider, window) for window in windows]
    require(len(set(keys)) == len(keys), "windows duplicate bucket")
    require(type(limits) is list and len(limits) <= LIMITS_MAX and (provider == "codex" or not limits)
            and all(_fields(limit, _LIMIT) for limit in limits), "limits")
    require(value["ordinary_usage_allowed"] is None or type(value["ordinary_usage_allowed"]) is bool,
            "ordinary_usage_allowed")
    require(value["extra_usage"] is None or (provider == "claude" and _fields(value["extra_usage"], _EXTRA)),
            "extra_usage")
    require(type(value["unparsed"]) is list and len(value["unparsed"]) <= UNPARSED_MAX
            and all(_string(item, 200) for item in value["unparsed"]), "unparsed")
    if not available:
        require(not windows and not limits and value["extra_usage"] is None
                and value["ordinary_usage_allowed"] is None and not value["unparsed"], "unavailable data")
    return value


# ---------- the observed change during a run (pure; the observation layer reads saved samples only) ----------

RESET_TOLERANCE_CAP_SECONDS = 120     # design values, to be checked against real CLIs
RESET_TOLERANCE_UNKNOWN_SECONDS = 60
# Its own context: never the process default (28 digits). Balances have at most 30+30 digits, so a
# difference has at most 61 and is always exact; a trap gives "unknown", never an approximation.
DECIMAL_CONTEXT = decimal.Context(prec=64, rounding=decimal.ROUND_HALF_EVEN,
                                  traps=[decimal.InvalidOperation, decimal.Inexact, decimal.Rounded,
                                         decimal.Overflow, decimal.DivisionByZero])
VIEW_NOTES = ("单位是百分点；重叠的额度桶不相加，两家不合成总数",)
TRUNCATED_NOTE = "较早的样本未读取，第一段的开头不完整"


def reset_tolerance(window):
    """How far a reported reset may move and still be the same cycle: 1/60 of the window, at most 120 s."""
    minutes = window.get("window_minutes")
    if type(minutes) is int and minutes > 0:
        return min(RESET_TOLERANCE_CAP_SECONDS, minutes)   # minutes * 60 s / 60
    return RESET_TOLERANCE_UNKNOWN_SECONDS


def effective_reset(observation):
    """A reported reset counts only while it is later than its own observation; else unknown."""
    _, sample, window = observation
    reset = window["resets_at"]
    return reset if reset is not None and reset > sample["observed_at"] else None


def decimal_difference(last, first):
    """``last - first`` of two balance strings, exact, as a fixed-point string; None if not exact."""
    try:
        value = DECIMAL_CONTEXT.subtract(decimal.Decimal(last), decimal.Decimal(first))
    except (decimal.DecimalException, TypeError, ValueError):
        return None
    if not value.is_finite():
        return None
    return format(value.copy_abs() if value.is_zero() else value, "f")


def _points(last, first):
    """Percentage points between two reported values; floats subtract as their decimal text."""
    if type(last) is int and type(first) is int:
        return last - first
    value = decimal.Decimal(repr(last)) - decimal.Decimal(repr(first))
    return int(value) if value == value.to_integral_value() else float(value)


def quota_view(samples, truncated=False):
    """The ``quota`` block of ``status RUN`` and the run detail, from samples in their total order.

    Per provider and bucket: the latest observation and the observed change split into reset
    cycles; Codex credits as exact balance changes. Nothing is summed across buckets, segments
    or providers. ``truncated``: older samples were not read.
    """
    view = {"notes": list(NOTES) + list(VIEW_NOTES) + ([TRUNCATED_NOTE] if truncated else []), "providers": {}}
    for provider in PROVIDERS:
        rows = [sample for sample in samples if sample["provider"] == provider]
        if not rows:
            view["providers"][provider] = {"state": "no_samples"}
            continue
        usable = [(position, sample) for position, sample in enumerate(rows) if sample["available"]]
        unavailable = {}
        for sample in rows:
            if not sample["available"]:
                unavailable[sample["reason"]] = unavailable.get(sample["reason"], 0) + 1
        last, latest = rows[-1], (usable[-1][1] if usable else None)
        entry = {"state": "available" if usable else "all_unavailable", "samples": len(rows),
                 "unavailable": unavailable,
                 "latest_sample": {"observed_at": _utc(last["observed_at"], "seconds"), "trigger": last["trigger"],
                                   "available": last["available"], "reason": last["reason"]},
                 "clock_rollback": any(later["observed_at"] < earlier["observed_at"]
                                       for earlier, later in zip(rows, rows[1:])),
                 "buckets": _buckets(provider, usable, len(rows) - 1, truncated)}
        if provider == "claude":
            entry["extra_usage"] = latest["extra_usage"] if latest else None
        else:
            entry["credits"] = _credits(usable)
            entry["ordinary_usage_allowed"] = latest["ordinary_usage_allowed"] if latest else None
            entry["limits"] = ({limit["limit_id"]: {key: limit[key] for key in
                                                     ("limit_name", "rate_limit_reached_type", "spend_control_reached")}
                                for limit in latest["limits"]} if latest else {})
        view["providers"][provider] = entry
    return view


def _buckets(provider, usable, last_position, truncated):
    found = {}
    for position, sample in usable:
        for window in sample["windows"]:
            found.setdefault(bucket_key(provider, window), []).append((position, sample, window))
    result = []
    for key, observations in found.items():
        _, sample, window = observations[-1]
        result.append({"bucket": bucket_label(provider, window), "identity": list(key),
                       "latest": {"used_percent": window["used_percent"],
                                  "observed_at": _utc(sample["observed_at"], "seconds"),
                                  "resets_at": _iso(window["resets_at"])},
                       "segments": _segments(observations, last_position, truncated)})
    return result


def _iso(epoch):
    return _utc(epoch, "seconds") if epoch is not None else None


def _split(segment, observation):
    """None when ``observation`` continues ``segment``'s cycle, else why a new cycle starts."""
    anchor, previous, current = effective_reset(segment[0]), effective_reset(segment[-1]), effective_reset(observation)
    if anchor is None or current is None:
        return None if anchor is None and current is None else "reset_unknown"
    if observation[1]["observed_at"] >= min(anchor, previous):
        return "reset_reached"
    if abs(current - anchor) > reset_tolerance(observation[2]):
        return "reset_changed"
    return None


def _segments(observations, last_position, truncated):
    """One bucket's observations ``(position, sample, window)`` split into reset cycles (spec-7 3.8)."""
    groups, reasons = [], []
    for observation in observations:
        reason = _split(groups[-1], observation) if groups else "first"
        if reason is None:
            groups[-1].append(observation)
        else:
            groups.append([observation])
            reasons.append(reason)
    result = []
    for index, group in enumerate(groups):
        anchored = effective_reset(group[0]) is not None
        values = [window["used_percent"] for _, _, window in group]
        if index > 0:
            start = "after_reset"
        elif group[0][0] != 0:
            start = "first_available_observation"
        elif truncated:
            start = "older_samples_not_read"
        else:
            start = None
        end = ("before_reset" if index < len(groups) - 1 else
               "later_samples_missing_bucket_or_unavailable" if group[-1][0] != last_position else None)
        flags = []
        if anchored and any(later < earlier for earlier, later in zip(values, values[1:])):
            flags.append("decrease")
        if reasons[index] == "reset_changed":
            flags.append("reset_changed_within_cycle")
        if any(item[2]["resets_at"] is not None and effective_reset(item) is None for item in group):
            flags.append("reset_already_passed")
        elif not anchored:
            flags.append("resets_at_unknown")
        result.append({
            "first": {"used_percent": values[0], "observed_at": _utc(group[0][1]["observed_at"], "seconds")},
            "last": {"used_percent": values[-1], "observed_at": _utc(group[-1][1]["observed_at"], "seconds")},
            "observations": len(group),
            "resets_at": _iso(effective_reset(group[-1])),
            # Unknown is never zero: a single observation or an unknown cycle has no change.
            "change_points": _points(values[-1], values[0]) if anchored and len(group) > 1 else None,
            "complete": start is None and end is None,
            "incomplete_start": start, "incomplete_end": end, "flags": flags})
    return result


def _credits(usable):
    """Per limit: the latest credits and runs of known balances with their exact net change.

    A run is consecutive observations with ``unlimited`` false and a balance; ``unlimited``
    true or unknown, or no balance, ends it. A sample that does not report the limit's
    credits neither ends nor joins a run.
    """
    found = {}
    for _, sample in usable:
        for limit in sample["limits"]:
            if limit["credits"] is not None:
                found.setdefault(limit["limit_id"], []).append((sample, limit["credits"]))
    result = {}
    for limit_id, observations in found.items():
        runs, current = [], None
        for sample, credits in observations:
            if credits["unlimited"] is not False or credits["balance"] is None:
                current = None
                continue
            if current is None:
                current = {"first": credits["balance"], "first_at": _utc(sample["observed_at"], "seconds"),
                           "observations": 0}
                runs.append(current)
            current.update(last=credits["balance"], last_at=_utc(sample["observed_at"], "seconds"))
            current["observations"] += 1
        for run in runs:
            run["net_change"] = decimal_difference(run["last"], run["first"]) if run["observations"] > 1 else None
        sample, credits = observations[-1]
        result[limit_id] = {"latest": {**credits, "observed_at": _utc(sample["observed_at"], "seconds")},
                            "runs": runs}
    return result
