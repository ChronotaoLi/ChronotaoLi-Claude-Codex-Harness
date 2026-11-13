"""Execution protocol version: one ledger shape, never migrated or interpreted across versions."""
from .util import HarnessError

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
