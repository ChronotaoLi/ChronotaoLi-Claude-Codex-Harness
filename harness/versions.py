"""Execution protocol version: one ledger shape, never migrated or interpreted across versions.

Every entry that reads a run ledger checks it here before any other field is
read: a value that is not an object, or an object of another version, is refused
with the same sentence.
"""
import json
from pathlib import Path

from . import stops
from .state_files import StateFiles
from .util import EvidenceError, HarnessError

RUN_SCHEMA_VERSION = 19


def unsupported_ledger(run):
    """The one-line refusal for a ledger that is not the current version; None when it is."""
    version = run.get("schema_version") if isinstance(run, dict) else None
    if type(version) is int and version == RUN_SCHEMA_VERSION:
        return None
    if type(version) is int:
        return f"台账版本 {version} 不支持（当前版本 {RUN_SCHEMA_VERSION}）"
    return f"台账版本缺失或无效，不支持（当前版本 {RUN_SCHEMA_VERSION}）"


def require_current_ledger(run):
    message = unsupported_ledger(run)
    if message is not None:
        version = run.get("schema_version") if isinstance(run, dict) else None
        data = {"supported": RUN_SCHEMA_VERSION}
        if type(version) is int and version >= 0:
            data["schema_version"] = version
        raise HarnessError(message, stop=stops.make("unsupported_ledger", "ledger_version", data=data))


def missing_ledger(path):
    """The refusal for a run whose ledger file is not there."""
    return HarnessError(f"run 不存在：{path}", stop=stops.make("invalid_input", "cli_arguments", data={"option": "run_id"}))


def require_run_id(ident):
    if not isinstance(ident, str) or not ident.startswith("run_") or not ident[4:].isalnum():
        raise HarnessError("无效 run ID", stop=stops.make("invalid_input", "cli_arguments", data={"option": "run_id"}))


def precheck_run(state, ident, *, load=False):
    """Reject an unreadable or unsupported ledger before entry writes; callers recheck under lock."""
    require_run_id(ident)
    components = ("runs", ident, "state.json")
    path = Path(state).joinpath(*components)
    source = {"artifact": str(path)}
    outcome = StateFiles(state).read(components, limit=None)
    if outcome.state == "absent":
        raise missing_ledger(path)
    try:
        with stops.evidence_reads(source=source):
            return parse_current_ledger(outcome.evidence(source))
    except EvidenceError as error:
        if load:
            error.stop = stops.load_failure(error)
        raise


def parse_current_ledger(raw):
    """A run ledger from its bytes; refuse any other version before a field is interpreted."""
    run = json.loads(raw.decode("utf-8"))
    require_current_ledger(run)
    return run


def read_current_ledger(path):
    """Read a run ledger file; refuse any other version before a field is interpreted."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise missing_ledger(path) from error
    return parse_current_ledger(raw)
