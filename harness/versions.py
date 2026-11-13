"""Execution protocol version: one ledger shape, never migrated or interpreted across versions.

Every entry that reads a run ledger checks it here before any other field is
read: a value that is not an object, or an object of another version, is refused
with the same sentence.
"""
from .util import HarnessError, read_json

RUN_SCHEMA_VERSION = 18


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
        raise HarnessError(message)


def read_current_ledger(path):
    """Read a run ledger file; refuse any other version before a field is interpreted."""
    run = read_json(path)
    require_current_ledger(run)
    return run
