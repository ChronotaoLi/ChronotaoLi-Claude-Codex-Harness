"""Classify a failed model call from the bytes its process and CLI left behind.

``classify()`` is a pure function of the process outcome record
(``process.outcome.json``) and Claude's terminal envelope (``process.stdout``):
process facts first, then the envelope's ``subtype``, ``api_error`` and
``api_error_status`` fields. Model text (``result``, ``errors``) and stderr are
never read, and nothing here depends on the clock, the account or the config.
"""
from .util import strict_json_loads

CODES = ("usage_limit", "rate_limit", "overloaded", "authentication",
         "agent_turns", "agent_timeout", "local_start", "invocation_failed", "unknown")
# Failures that say nothing about the task's work: they spare a work attempt.
EXEMPT = ("usage_limit", "rate_limit", "overloaded", "authentication")
STOPS = {
    "usage_limit": ("provider_quota", "usage_limit"),
    "rate_limit": ("provider_quota", "rate_limit"),
    "overloaded": ("provider_failure", "overloaded"),
    "authentication": ("provider_failure", "authentication"),
    "agent_turns": ("budget", "agent_turns"),
    "agent_timeout": ("budget", "agent_timeout"),
    "local_start": ("provider_failure", "local_start"),
    "invocation_failed": ("provider_failure", "invocation_failed"),
    "unknown": ("provider_failure", "unknown"),
}
# Only labels the CLI diagnostics already recognise (adapters.claude_error_summary).
API_ERRORS = {"usage_limit_reached": "usage_limit", "rate_limit_error": "rate_limit",
              "overloaded_error": "overloaded", "authentication_error": "authentication",
              "permission_error": "authentication"}
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


def classify(outcome_raw, stdout_raw, provider):
    """``{"code", "http_status"}`` for a failed call (spec-5 2.2).

    ``outcome_raw`` is None when no process record exists; ``stdout_raw`` is the
    complete output (empty when absent). Timeouts and start failures come from the
    process record; the envelope is read only after a normal, complete process end.
    """
    from .worker_returns import decode_terminal
    unknown = {"code": "unknown", "http_status": None}
    if outcome_raw is None:
        return unknown
    try:
        outcome = strict_json_loads(outcome_raw)
    except _DECODE_ERRORS:
        return unknown
    if not isinstance(outcome, dict):
        return unknown
    if outcome.get("termination_reason") == "timeout":
        return {"code": "agent_timeout", "http_status": None}
    if outcome.get("termination_reason") == "start_failed" or (
            outcome.get("started") is False and outcome.get("startup_state") == "not_started"):
        return {"code": "local_start", "http_status": None}
    if not _completed_normally(outcome) or provider != "claude":
        return unknown
    kind, envelope, _, _ = decode_terminal(stdout_raw or b"")
    if kind != "error":
        return unknown
    status = claude_http_status(envelope)
    http = status["value"] if status["state"] == "valid" else None
    if envelope.get("subtype") == "error_max_turns":
        return {"code": "agent_turns", "http_status": http}
    label = envelope.get("api_error")
    if isinstance(label, str) and label in API_ERRORS:
        return {"code": API_ERRORS[label], "http_status": http}
    if label is None and http == 429:
        return {"code": "rate_limit", "http_status": http}
    return {"code": "invocation_failed", "http_status": http}


def read_for_boundary(path):
    """Bytes for the commander/reviewer call boundary: None when absent or unreadable.

    That boundary only chooses the type of a stop it is already reporting; a read
    failure there must not replace the original failure (no record is saved).
    """
    try:
        with path.open("rb") as stream:
            raw = stream.read(LIMIT + 1)
    except OSError:
        return None
    return raw if len(raw) <= LIMIT else None
