"""Classify an exited worker's durable output without inventing a return.

There is one transport: Claude's JSON output in process.stdout. A retryable
record also carries the provider classification of the failure (version 3,
``provider``), derived from the same bytes. These records never bind a candidate.
"""
import hashlib
from pathlib import Path

from . import stops
from .state_files import StateFiles
from .util import HarnessError, json_bytes, strict_json_loads


# Every file the runtime can write into a worker dispatch directory that bears on
# whether the worker returned. Nothing else is fingerprinted.
SOURCES = (
    'returned-result.json', 'response.json', 'provider-result.json',
    'process.stdout', 'process.outcome.json',
)
STATES = ('retryable', 'invalid', 'returned', 'unknown')
LIMIT = 16 * 1024 * 1024
_DECODE_ERRORS = (ValueError, UnicodeError, RecursionError)
_INSPECTED = frozenset(('process.stdout', 'process.outcome.json'))


def worker_evidence_files(job):
    """Require the dispatch directory before inspecting any of its evidence."""
    job = Path(job)
    parent = StateFiles.nofollow_directory(job.parent)
    probe = parent.probe((job.name,), expect='directory').or_local_io()
    if probe.state != 'present':
        probe.evidence({'artifact': str(job)})
    return parent.under(job.name)


def _read_sources(job, names, *, keep=()):
    files = worker_evidence_files(job)
    sources = {}
    for name in names:
        outcome = files.digest((name,), keep=LIMIT if name in keep else 0)
        if outcome.state == 'absent' and outcome.step.leaf:
            sources[name] = None
            continue
        outcome.evidence({'artifact': outcome.path})
        sources[name] = outcome
    return sources


def _digests(sources):
    return {name: outcome.digest if outcome is not None else None for name, outcome in sources.items()}


@stops.reads_evidence
def fingerprint(job, names):
    return _digests(_read_sources(job, names))


def decode_terminal(raw):
    """Classify Claude's complete JSON output: the adapter and the classifier share it.

    Returns (kind, envelope, payload, detail). ``absent``: nothing was written.
    ``undecodable``: not strictly decodable JSON, not a result terminal, or a
    successful terminal whose result is not a JSON object. ``error``: an explicit
    provider error terminal. ``object``: a successful terminal with an object result.
    """
    if not raw.strip():
        return 'absent', None, None, ''
    try:
        envelope = strict_json_loads(raw)
    except _DECODE_ERRORS as error:
        return 'undecodable', None, None, type(error).__name__
    if not isinstance(envelope, dict) or envelope.get('type') != 'result':
        return 'undecodable', None, None, 'not a result terminal'
    subtype = envelope.get('subtype')
    # Only an explicit error is a provider failure; a looser flag beside an
    # object result must not discard what may be a binding blocker.
    if envelope.get('is_error') is True or isinstance(subtype, str) and subtype.startswith('error_'):
        return 'error', envelope, None, ''
    payload = envelope.get('structured_output')
    if payload is None:
        text = envelope.get('result', '')
        if not isinstance(text, str):
            return 'undecodable', envelope, None, 'result text is not a string'
        try:
            payload = strict_json_loads(text)
        except _DECODE_ERRORS as error:
            return 'undecodable', envelope, None, 'result text ' + type(error).__name__
    if not isinstance(payload, dict):
        return 'undecodable', envelope, None, 'result is not an object'
    return 'object', envelope, payload, ''


NO_WORKER_OUTPUT = "direct JSON output is absent or empty after exit"


@stops.reads_evidence
def inspect_return(job):
    """Classify an exited, unsealed worker's evidence: retryable, invalid, returned or unknown.

    retryable and invalid both let a new billed attempt start: no result, or a
    complete output that does not decode to an object result. A decodable
    object result without a candidate seal stays returned (it may be a blocker).
    """
    from .provider_failures import classify
    inputs = _read_sources(job, SOURCES, keep=_INSPECTED)
    sources = _digests(inputs)
    provider = None

    def inspected(name, default=None):
        outcome = inputs[name]
        if outcome is None:
            return default
        if len(outcome.data) > LIMIT:
            raise ValueError('worker return evidence exceeds inspection limit')
        return outcome.data

    def result(state, reason):
        # A writer or external edit must not race the classification.
        if fingerprint(job, SOURCES) != sources:
            raise HarnessError('worker return evidence changed during inspection')
        return {'version': 3, 'state': state, 'reason': reason, 'sources': sources,
                'provider': provider if state == 'retryable' else None}

    if sources['returned-result.json'] is not None or sources['response.json'] is not None:
        return result('returned', 'worker result exists without a candidate seal')
    try:
        raw = inspected('process.stdout', b'')
        kind, envelope, _, detail = decode_terminal(raw)
        outcome = inspected('process.outcome.json')
        # Worker calls are Claude only (the configuration refuses a Codex worker).
        provider = classify(outcome, raw, 'claude')
        published = sources['provider-result.json']
        if published is not None:
            if envelope is None or hashlib.sha256(json_bytes(envelope)).hexdigest() != published:
                return result('unknown', 'provider result conflicts with original output')
    except _DECODE_ERRORS as error:
        # Over the limit or not strictly decodable: a property of the bytes. An
        # I/O failure leaves without a verdict.
        return result('unknown', 'unreadable worker return evidence: ' + type(error).__name__)
    if kind == 'absent':
        return result('retryable', NO_WORKER_OUTPUT)
    if kind == 'error':
        return result('retryable', 'provider returned a complete explicit error')
    if kind == 'object':
        return result('returned', 'non-error or ambiguous direct JSON output')
    return result('invalid', 'worker output is not a decodable object result: ' + detail)


def _provider_shape(record):
    from .provider_failures import CODES
    provider = record.get('provider')
    if record.get('state') != 'retryable':
        return provider is None
    return (isinstance(provider, dict) and set(provider) == {'code', 'http_status'}
            and provider['code'] in CODES
            and (provider['http_status'] is None or type(provider['http_status']) is int))


@stops.reads_evidence
def validate_return_evidence(job, record):
    if (not isinstance(record, dict) or set(record) != {'version', 'state', 'reason', 'sources', 'provider'}
            or record['version'] != 3 or record['state'] not in STATES or not _provider_shape(record)
            or not isinstance(record['reason'], str)
            or not isinstance(record['sources'], dict) or set(record['sources']) != set(SOURCES)):
        raise HarnessError('invalid worker return evidence record')
    actual = inspect_return(job)
    if actual['sources'] != record['sources']:
        raise HarnessError('worker return evidence changed; preserve original dispatch')
    if actual != record:
        raise HarnessError('worker return classification does not match bound evidence')
