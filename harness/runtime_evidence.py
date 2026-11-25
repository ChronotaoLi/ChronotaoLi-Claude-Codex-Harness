"""Read runtime-written evidence before reusing or publishing an artifact."""

from .recovery import evidence_path
from .util import write_json


def optional_bytes(files, components, source):
    """An unpublished artifact or its enclosing subdirectory may be missing."""
    evidence_path(files.under(*components), source)
    outcome = files.probe(components).or_local_io()
    if outcome.state == "absent" and isinstance(outcome.step.level, int) and outcome.step.level >= len(files.prefix):
        return None
    if outcome.state != "present":
        outcome.evidence(source)
    return files.read(components, limit=None).evidence(source)


def read_or_write(files, components, artifact, value):
    """Reuse existing bytes or publish an absent artifact and read it back."""
    source = {"artifact": str(artifact)}
    raw = optional_bytes(files, components, source)
    if raw is None:
        write_json(artifact, value)
        raw = files.read(components, limit=None).evidence(source)
    return raw
