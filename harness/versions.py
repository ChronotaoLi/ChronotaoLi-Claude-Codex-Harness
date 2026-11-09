"""Execution protocol versions; historical inspection never upgrades a run."""

RUN_SCHEMA_VERSION = 15
HISTORICAL_CLEANUP_VERSIONS = tuple(range(7, RUN_SCHEMA_VERSION + 1))
