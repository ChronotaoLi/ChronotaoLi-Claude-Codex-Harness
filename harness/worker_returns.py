"""Classify an exited worker's durable output without inventing a return.

Schema 17 has one transport: Claude's JSON output in process.stdout. Schema 15
and 16 ledgers also declared a capture mode before invocation; their records are
checked only by the frozen historical classifier at the end of this module.
These records never bind a candidate.
"""
import hashlib
import json

from .util import HarnessError, strict_json_loads


# Every file schema 17 can write into a worker dispatch directory that bears on
# whether the worker returned. Nothing else is fingerprinted.
SOURCES = (
    'returned-result.json', 'response.json', 'provider-result.json',
    'process.stdout', 'process.outcome.json',
)
STATES = ('retryable', 'invalid', 'returned', 'unknown')
LIMIT = 16 * 1024 * 1024
_EMPTY = object()
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


def _read(job, name):
    # Shared only with the frozen schema 15/16 classifier below; never change it.
    with (job / name).open('rb') as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError('worker return evidence exceeds inspection limit')
    return json.loads(raw) if raw.strip() else _EMPTY


def _provider_error(value):
    # Shared only with the frozen schema 15/16 classifier below; never change it.
    return (isinstance(value, dict) and value.get('type') == 'result'
            and (value.get('is_error') is True or
                 isinstance(value.get('subtype'), str) and value['subtype'].startswith('error_')))


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


# ----- Schema 15 and 16: read-only history, frozen ------------------------------
# Byte-for-byte the bc94b6f classifier, renamed and given its own file list. The
# evidence those versions saved embeds the intent's capture and these eleven
# sources, and cleanup re-derives it exactly; never change it with schema 17.
# An absent file is meaningful only under the transport declared before
# invocation. Stream capture must finish before it can prove absence; a dead
# reader cannot attest that its pipes contained no terminal.

HISTORICAL_SOURCES = (
    'returned-result.json', 'response.json', 'terminal-capture.json',
    'provider-result.json', 'result.json', 'process.stdout', 'process.outcome.json',
    'capture-manifest.json', 'stream-capture.json', 'stream-terminal.json',
    'stream-received-terminal.json',
)


def inspect_historical_return(job, capture):
    """Return retryable/returned/unknown plus the exact evidence used, after exit."""
    sources = fingerprint(job, HISTORICAL_SOURCES)
    def result(state, reason):
        # A capture writer or external edit must not race the classification.
        if fingerprint(job, HISTORICAL_SOURCES) != sources:
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


def validate_historical_return_evidence(job, record, capture):
    if (not isinstance(record, dict) or record.get('version') != 1
            or record.get('capture') != capture or record.get('state') not in ('retryable', 'returned', 'unknown')
            or not isinstance(record.get('reason'), str)
            or not isinstance(record.get('sources'), dict) or set(record['sources']) != set(HISTORICAL_SOURCES)):
        raise HarnessError('invalid worker return evidence record')
    if fingerprint(job, HISTORICAL_SOURCES) != record['sources']:
        raise HarnessError('worker return evidence changed; preserve original dispatch')
    if inspect_historical_return(job, capture) != record:
        raise HarnessError('worker return classification does not match bound evidence')
