"""Classify an exited worker's durable output without inventing a return.

An absent file is meaningful only under the transport declared before invocation.
Stream capture must finish before it can prove absence; a dead reader cannot
attest that its pipes contained no terminal. These records never bind a candidate.
"""
import hashlib
import json

from .util import HarnessError


SOURCES = (
    'returned-result.json', 'response.json', 'terminal-capture.json',
    'provider-result.json', 'result.json', 'process.stdout', 'process.outcome.json',
    'capture-manifest.json', 'stream-capture.json', 'stream-terminal.json',
    'stream-received-terminal.json',
)
LIMIT = 16 * 1024 * 1024
_EMPTY = object()


def fingerprint(job):
    if job.is_symlink() or job.resolve() != job or not job.is_dir():
        raise HarnessError('unsafe worker return evidence directory')
    result = {}
    for name in SOURCES:
        path = job / name
        if path.is_symlink() or path.exists() and not path.is_file():
            raise HarnessError('unsafe worker return evidence: ' + name)
        if not path.exists():
            result[name] = None
        else:
            with path.open('rb') as stream:
                result[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


def _read(job, name):
    with (job / name).open('rb') as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError('worker return evidence exceeds inspection limit')
    return json.loads(raw) if raw.strip() else _EMPTY


def _provider_error(value):
    return (isinstance(value, dict) and value.get('type') == 'result'
            and (value.get('is_error') is True or
                 isinstance(value.get('subtype'), str) and value['subtype'].startswith('error_')))


def inspect_return(job, capture):
    """Return retryable/returned/unknown plus the exact evidence used, after exit."""
    sources = fingerprint(job)
    def result(state, reason):
        # A capture writer or external edit must not race the classification.
        if fingerprint(job) != sources:
            raise HarnessError('worker return evidence changed during inspection')
        return {'version': 1, 'capture': capture.copy(), 'state': state,
                'reason': reason, 'sources': sources}

    if capture.get('provider') != 'claude' or capture.get('mode') not in ('legacy_json', 'stream_metadata_v1'):
        raise HarnessError('invalid worker capture contract')
    if any(sources[name] is not None for name in
           ('returned-result.json', 'response.json', 'terminal-capture.json', 'result.json')):
        return result('returned', 'worker result exists without a candidate seal')
    try:
        if sources['capture-manifest.json'] is not None:
            manifest = _read(job, 'capture-manifest.json')
            if (not isinstance(manifest, dict) or manifest.get('version') != 1
                    or manifest.get('mode') != capture['mode']):
                return result('unknown', 'capture manifest does not match invocation')
        if capture['mode'] == 'legacy_json':
            if any(sources[name] is not None for name in
                   ('stream-terminal.json', 'stream-received-terminal.json', 'stream-capture.json')):
                return result('unknown', 'stream evidence conflicts with JSON invocation')
            value = _read(job, 'process.stdout') if sources['process.stdout'] is not None else _EMPTY
            if sources['provider-result.json'] is not None:
                provider = _read(job, 'provider-result.json')
                if provider != value:
                    return result('unknown', 'provider result conflicts with original output')
            if value is _EMPTY:
                return result('retryable', 'direct JSON output is absent or empty after exit')
            if _provider_error(value):
                return result('retryable', 'provider returned a complete explicit error')
            return result('returned' if isinstance(value, dict) and value.get('type') == 'result' else 'unknown',
                          'non-error or ambiguous direct JSON output')

        # A received terminal is refusal evidence even if transport never finished.
        witness = (_read(job, 'stream-received-terminal.json')
                   if sources['stream-received-terminal.json'] is not None else None)
        if sources['stream-received-terminal.json'] is not None and (not isinstance(witness, dict) or witness.get('version') != 1
                                    or not isinstance(witness.get('terminal'), dict)):
            return result('unknown', 'invalid received terminal witness')
        if witness is not None and not _provider_error(witness['terminal']):
            return result('returned', 'stream received a non-error terminal before interruption')
        if sources['stream-capture.json'] is None:
            if any(sources[name] is not None for name in
                   ('stream-terminal.json', 'stream-received-terminal.json', 'provider-result.json')):
                return result('unknown', 'terminal exists but stream completion is unproven')
            outcome = _read(job, 'process.outcome.json') if sources['process.outcome.json'] is not None else None
            if sources['process.outcome.json'] is None or (isinstance(outcome, dict) and outcome.get('started') is False
                                   and outcome.get('startup_state') == 'not_started'):
                return result('retryable', 'stream process was not launched')
            return result('unknown', 'stream reader stopped before durable transport completion')
        protocol = _read(job, 'stream-capture.json')
        if (sources['capture-manifest.json'] is None or not isinstance(protocol, dict) or protocol.get('version') != 2
                or protocol.get('transport_complete') is not True or protocol.get('damaged') is not False
                or type(protocol.get('terminal_count')) is not int or protocol['terminal_count'] < 0):
            return result('unknown', 'stream transport is incomplete or damaged')
        count = protocol['terminal_count']
        if count == 0 and all(sources[name] is None for name in
                             ('stream-terminal.json', 'stream-received-terminal.json', 'provider-result.json')):
            return result('retryable', 'complete stream contains no terminal')
        if count != 1 or protocol.get('protocol_complete') is not True:
            return result('unknown', 'stream terminal identity is ambiguous')
        if sources['stream-terminal.json'] is None:
            return result('returned', 'complete stream recorded a terminal before publication')
        terminal = _read(job, 'stream-terminal.json')
        if witness is None or witness['terminal'] != terminal:
            return result('unknown', 'published terminal differs from received terminal')
        if sources['provider-result.json'] is not None and _read(job, 'provider-result.json') != terminal:
            return result('unknown', 'provider result differs from published terminal')
        if _provider_error(terminal):
            return result('retryable', 'complete stream returned an explicit provider error')
        return result('returned', 'complete stream returned a non-error terminal')
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        return result('unknown', 'unreadable worker return evidence: ' + type(error).__name__)


def validate_return_evidence(job, record, capture):
    if (not isinstance(record, dict) or record.get('version') != 1
            or record.get('capture') != capture or record.get('state') not in ('retryable', 'returned', 'unknown')
            or not isinstance(record.get('reason'), str)
            or not isinstance(record.get('sources'), dict) or set(record['sources']) != set(SOURCES)):
        raise HarnessError('invalid worker return evidence record')
    if fingerprint(job) != record['sources']:
        raise HarnessError('worker return evidence changed; preserve original dispatch')
    if inspect_return(job, capture) != record:
        raise HarnessError('worker return classification does not match bound evidence')
