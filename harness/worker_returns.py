"""Classify an exited worker's durable output without inventing a return.

There is one transport: Claude's JSON output in process.stdout. These records
never bind a candidate.
"""
import hashlib

from .util import HarnessError, strict_json_loads


# Every file the runtime can write into a worker dispatch directory that bears on
# whether the worker returned. Nothing else is fingerprinted.
SOURCES = (
    'returned-result.json', 'response.json', 'provider-result.json',
    'process.stdout', 'process.outcome.json',
)
STATES = ('retryable', 'invalid', 'returned', 'unknown')
LIMIT = 16 * 1024 * 1024
_DECODE_ERRORS = (ValueError, UnicodeError, RecursionError)


def fingerprint(job, names):
    if job.is_symlink() or job.resolve() != job or not job.is_dir():
        raise HarnessError('unsafe worker return evidence directory')
    result = {}
    for name in names:
        path = job / name
        if path.is_symlink() or path.exists() and not path.is_file():
            raise HarnessError('unsafe worker return evidence: ' + name)
        if not path.exists():
            result[name] = None
        else:
            with path.open('rb') as stream:
                result[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


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


def _read_limited(job, name):
    with (job / name).open('rb') as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError('worker return evidence exceeds inspection limit')
    return raw


def inspect_return(job):
    """Classify an exited, unsealed worker's evidence: retryable, invalid, returned or unknown.

    retryable and invalid both let a new billed attempt start: no result, or a
    complete output that does not decode to an object result. A decodable
    object result without a candidate seal stays returned (it may be a blocker).
    """
    sources = fingerprint(job, SOURCES)

    def result(state, reason):
        # A writer or external edit must not race the classification.
        if fingerprint(job, SOURCES) != sources:
            raise HarnessError('worker return evidence changed during inspection')
        return {'version': 1, 'state': state, 'reason': reason, 'sources': sources}

    if sources['returned-result.json'] is not None or sources['response.json'] is not None:
        return result('returned', 'worker result exists without a candidate seal')
    try:
        raw = _read_limited(job, 'process.stdout') if sources['process.stdout'] is not None else b''
        kind, envelope, _, detail = decode_terminal(raw)
        if sources['provider-result.json'] is not None:
            provider = strict_json_loads(_read_limited(job, 'provider-result.json'))
            if envelope is None or provider != envelope:
                return result('unknown', 'provider result conflicts with original output')
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        return result('unknown', 'unreadable worker return evidence: ' + type(error).__name__)
    if kind == 'absent':
        return result('retryable', 'direct JSON output is absent or empty after exit')
    if kind == 'error':
        return result('retryable', 'provider returned a complete explicit error')
    if kind == 'object':
        return result('returned', 'non-error or ambiguous direct JSON output')
    return result('invalid', 'worker output is not a decodable object result: ' + detail)


def validate_return_evidence(job, record):
    if (not isinstance(record, dict) or set(record) != {'version', 'state', 'reason', 'sources'}
            or record['version'] != 1 or record['state'] not in STATES
            or not isinstance(record['reason'], str)
            or not isinstance(record['sources'], dict) or set(record['sources']) != set(SOURCES)):
        raise HarnessError('invalid worker return evidence record')
    if fingerprint(job, SOURCES) != record['sources']:
        raise HarnessError('worker return evidence changed; preserve original dispatch')
    if inspect_return(job) != record:
        raise HarnessError('worker return classification does not match bound evidence')
