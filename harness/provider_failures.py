"""Classify a failed model call from the bytes its process and CLI left behind.

``classify()`` is a pure function of the process outcome record
(``process.outcome.json``) and Claude's terminal envelope (``process.stdout``):
process facts first, then the envelope's ``subtype``, ``api_error`` (the CLI's
error kind), ``api_error_type`` (the response body's error type),
``api_error_status`` and ``terminal_reason`` fields. The two label fields are
read separately and then combined. Model text (``result``, ``errors``) and
stderr are never read, and nothing here depends on the clock, the account or
the config. ``stop_facts()`` passes the same gate and only describes the
failure for display; ``classify()`` never reads what it shows.
"""
import os
import stat

from .stops import BODY_TYPES, LABEL, LABEL_FIELDS
from .util import strict_json_loads

CODES = ("usage_limit", "rate_limit", "overloaded", "authentication", "transport",
         "agent_turns", "agent_timeout", "local_start", "invocation_failed", "unknown")
# Failures that say nothing about the task's work: they spare a work attempt.
EXEMPT = ("usage_limit", "rate_limit", "overloaded", "authentication", "transport")
STOPS = {
    "usage_limit": ("provider_quota", "usage_limit"),
    "rate_limit": ("provider_quota", "rate_limit"),
    "overloaded": ("provider_failure", "overloaded"),
    "authentication": ("provider_failure", "authentication"),
    "transport": ("provider_failure", "transport"),
    "agent_turns": ("budget", "agent_turns"),
    "agent_timeout": ("budget", "agent_timeout"),
    "local_start": ("provider_failure", "local_start"),
    "invocation_failed": ("provider_failure", "invocation_failed"),
    "unknown": ("provider_failure", "unknown"),
}
# Each label field declares a class on its own: missing or null declares nothing, a listed
# string its class, any other value "other" (never treated as missing, so it cannot unlock 429).
# api_error also keeps recognising the older placement of the body types.
KIND_CLASSES = {"usage_limit_reached": "usage_limit", "connection_failed": "transport",
                "rate_limit_error": "rate_limit", "overloaded_error": "overloaded",
                "authentication_error": "authentication", "permission_error": "authentication"}
TYPE_CLASSES = {"rate_limit_error": "rate_limit", "overloaded_error": "overloaded",
                "authentication_error": "authentication", "permission_error": "authentication"}
# (api_error class, api_error_type class) pairs that agree although they differ, only with an HTTP
# status that is missing, null or 429: a usage-limit refusal is one of the CLI's rate_limit errors.
REFINES = {("usage_limit", "rate_limit")}
LIMIT = 16 * 1024 * 1024
_DECODE_ERRORS = (ValueError, UnicodeError, RecursionError)


def claude_http_status(value):
    """Presence remains distinct even when missing and null share a label."""
    if "api_error_status" not in value:
        return {"state": "missing"}
    status = value["api_error_status"]
    if status is None:
        return {"state": "null"}
    if type(status) is int and 400 <= status <= 599:
        return {"state": "valid", "value": status}
    return {"state": "invalid"}


def _completed_normally(outcome):
    """The record of a process that ran, ended on its own and left complete evidence."""
    return (outcome.get("started") is True and outcome.get("termination_reason") == "normal"
            and type(outcome.get("exit_code")) is int and outcome.get("exit_confirmed") is True
            and outcome.get("evidence_complete") is True
            and outcome.get("cleanup_errors") == [] and outcome.get("errors") == [])


def _envelope_gate(outcome_raw, stdout_raw, provider):
    """``(outcome, code, envelope)``: the decoded process record, the code decided before any label, the error envelope.

    ``outcome`` is None unless the record decodes to an object. Exactly one of ``code`` and
    ``envelope`` is set; ``envelope`` is the explicit error terminal whose fields ``classify``
    reads, and nothing else. ``code="unknown"`` has two branches: the process facts do not allow
    reading the output (no or undecodable record, an abnormal or incomplete end, not Claude), or
    they do and ``process.stdout`` was decoded but holds no explicit error terminal (absent,
    undecodable, an object result). So ``code`` never says whether the output was looked at:
    facts from the envelope come only from ``envelope``, facts from the record only from
    ``outcome``, which is the record in both branches.
    """
    from .worker_returns import decode_terminal
    if outcome_raw is None:
        return None, "unknown", None
    try:
        outcome = strict_json_loads(outcome_raw)
    except _DECODE_ERRORS:
        return None, "unknown", None
    if not isinstance(outcome, dict):
        return None, "unknown", None
    if outcome.get("termination_reason") == "timeout":
        return outcome, "agent_timeout", None
    if outcome.get("termination_reason") == "start_failed" or (
            outcome.get("started") is False and outcome.get("startup_state") == "not_started"):
        return outcome, "local_start", None
    if not _completed_normally(outcome) or provider != "claude":
        return outcome, "unknown", None
    kind, envelope, _, _ = decode_terminal(stdout_raw or b"")
    if kind != "error":
        return outcome, "unknown", None
    return outcome, None, envelope


def _declared(envelope, field, classes):
    value = envelope.get(field)
    if value is None:
        return None
    return classes.get(value, "other") if isinstance(value, str) else "other"


def _labelled(envelope, status):
    """The class the two label fields declare together; conflicts and "other" are ``invocation_failed``."""
    kind = _declared(envelope, "api_error", KIND_CLASSES)
    body = _declared(envelope, "api_error_type", TYPE_CLASSES)
    no_status = status["state"] in ("missing", "null")
    if kind is None and body is None:
        code = "rate_limit" if status.get("value") == 429 else "invocation_failed"
    elif kind is None or body is None or kind == body:
        code = kind or body
    elif (kind, body) in REFINES and (no_status or status.get("value") == 429):
        code = kind
    else:
        code = "invocation_failed"
    # Spared only in the shape of the one real sample; any other shape stays a counted failure.
    if code == "transport" and not (body is None and no_status and envelope.get("terminal_reason") == "api_error"):
        code = "invocation_failed"
    return "invocation_failed" if code == "other" else code


def classify(outcome_raw, stdout_raw, provider):
    """``{"code", "http_status"}`` for a failed call (spec-5 2.2).

    ``outcome_raw`` is None when no process record exists; ``stdout_raw`` is the
    complete output (empty when absent). Timeouts and start failures come from the
    process record; the envelope is read only after a normal, complete process end.
    """
    _, code, envelope = _envelope_gate(outcome_raw, stdout_raw, provider)
    if code is not None:
        return {"code": code, "http_status": None}
    status = claude_http_status(envelope)
    http = status["value"] if status["state"] == "valid" else None
    if envelope.get("subtype") == "error_max_turns":
        return {"code": "agent_turns", "http_status": http}
    return {"code": _labelled(envelope, status), "http_status": http}


def display_label(field, value):
    """``value`` when the envelope's ``field`` may show it, else None (spec-3 rules 8, 9).

    ``subtype``, ``terminal_reason`` and ``api_error`` are constants the CLI writes itself: any
    value of the identifier shape shows, new ones included. ``api_error_type`` is copied from the
    upstream response body: only the CLI's standard types show.
    """
    if type(value) is not str:
        return None
    if field == "api_error_type":
        return value if value in BODY_TYPES else None
    return value if LABEL.fullmatch(value) else None


def stop_facts(outcome_raw, stdout_raw, provider):
    """The facts a failure stop shows from the two originals' bytes (spec-3 rule 8); display only.

    ``exit_confirmed`` comes from any decoded record; the labels, the names of those present but
    not shown (``unrecognized_labels``) and ``http_status_state`` only from the error envelope
    ``classify`` reads, behind the same gate.
    """
    outcome, _, envelope = _envelope_gate(outcome_raw, stdout_raw, provider)
    facts = {}
    if outcome is not None and type(outcome.get("exit_confirmed")) is bool:
        facts["exit_confirmed"] = outcome["exit_confirmed"]
    if envelope is None:
        return facts
    hidden = []
    for field in LABEL_FIELDS:
        if field in envelope:
            shown = display_label(field, envelope[field])
            if shown is None:
                hidden.append(field)
            else:
                facts[field] = shown
    if hidden:
        facts["unrecognized_labels"] = hidden
    facts["http_status_state"] = claude_http_status(envelope)["state"]
    return facts


def read_original(path, read):
    """``(state, value)``: ``read(stream)`` on ``path``, one of a call's originals, never blocking or raising OSError.

    The adapter's and the call boundary's reads of the originals once the process ended all come
    here: opened without blocking or following a symlink, read only once ``fstat`` shows a regular
    file, so neither a FIFO without a writer nor a failed close can hold or replace the failure
    being reported. ``("read", what read returned)``; ``("absent", error)`` when the file does not
    exist; ``("unreadable", error)`` for any other OSError of the open, the read or the close, and
    ``("unreadable", None)`` for anything but a regular file (a FIFO, device or directory). ``read``
    keeps each caller's own bound, the caller its own fallback.
    """
    try:
        # Never blocks: a FIFO without a writer must not hold the stop of the failure being reported.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as error:
        return "absent", error
    except OSError as error:
        return "unreadable", error
    try:
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return "unreadable", None
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                return "read", read(stream)
        finally:
            if descriptor is not None:
                os.close(descriptor)
    except OSError as error:  # a failed close too: it must not replace the failure being reported
        return "unreadable", error


def read_for_boundary(path):
    """``(state, raw)`` for the commander/reviewer call boundary and a failure stop's facts.

    ``("read", bytes)``, ``("absent", None)`` when the file does not exist, ``("unreadable", None)``
    for whatever else ``read_original`` cannot read or over ``LIMIT``. A failed read is never told as
    an absence, so it cannot match a digest recorded as None. The boundary only chooses the type of a
    stop it is already reporting; a read failure there must not replace the original failure (no
    record is saved).
    """
    state, raw = read_original(path, lambda stream: stream.read(LIMIT + 1))
    if state == "read" and len(raw) <= LIMIT:
        return "read", raw
    return ("absent" if state == "absent" else "unreadable"), None
