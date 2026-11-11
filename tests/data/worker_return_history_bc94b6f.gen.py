"""Generate tests/data/worker_return_history_bc94b6f.json from bc94b6f's classifier only.

The data pins the schema 15/16 worker return classification that historical
cleanup re-derives (harness.worker_returns.inspect_historical_return). Expected
records must never come from the code under test, so this script imports only a
bc94b6f export and runs isolated (-I keeps the checkout off sys.path):

    tree=$(mktemp -d) && git archive bc94b6f harness/__init__.py harness/util.py \
        harness/worker_returns.py | tar -x -C "$tree"
    python3 -I tests/data/worker_return_history_bc94b6f.gen.py "$tree" \
        tests/data/worker_return_history_bc94b6f.json

Each row stores the exact bytes of every file it creates and the whole record
bc94b6f's inspect_return returned for that directory: one row per reachable
`return result(...)` and its variants, rows where two checks both apply (their
order), and rows that pin each condition inside a check. Pure file fixtures: no
process, model or CLI runs. The file name does not match the test pattern, so
discovery never collects it.
"""
import base64
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import harness.worker_returns  # noqa: E402  (bc94b6f)
from harness.worker_returns import SOURCES, inspect_return  # noqa: E402

if Path(harness.worker_returns.__file__).resolve().parents[1] != Path(sys.argv[1]).resolve():
    raise SystemExit('expected values must come from the given bc94b6f tree only')

OK = {'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': {'status': 'completed'}}
ERR = {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True}
LEGACY = {'provider': 'claude', 'mode': 'legacy_json'}
STREAM = {'provider': 'claude', 'mode': 'stream_metadata_v1'}
MANIFEST = {'legacy_json': {'version': 1, 'mode': 'legacy_json', 'cli_version': 'not_recorded', 'transport': 'json'},
            'stream_metadata_v1': {'version': 1, 'mode': 'stream_metadata_v1', 'cli_version': 'x', 'transport': 'jsonl'}}
DONE = {'version': 2, 'transport_complete': True, 'damaged': False, 'terminal_count': 1, 'protocol_complete': True}
STARTED = {'started': True, 'startup_state': 'started'}


def W(terminal, version=1):
    """A stream received-terminal witness."""
    return {'version': version, 'terminal': terminal}

ROWS = [
    ('legacy', 'L1 response.json exists', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'response.json': {}}),
    ('legacy', 'L2 manifest declares stream', LEGACY, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'process.stdout': ''}),
    ('legacy', 'L2b manifest is a list', LEGACY, {'capture-manifest.json': [], 'process.stdout': ''}),
    ('legacy', 'L3 stream-terminal.json present', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': '', 'stream-terminal.json': OK}),
    ('legacy', 'L4 provider-result differs from stdout', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': OK, 'provider-result.json': ERR}),
    # Rows where two checks both apply pin the order between them.
    ('legacy', 'L1b response.json with a stream manifest', LEGACY, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'response.json': {}}),
    ('legacy', 'L3b stream file with a stream manifest', LEGACY, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'process.stdout': '', 'stream-terminal.json': OK}),
    ('legacy', 'L4b provider-result with empty stdout', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': '', 'provider-result.json': OK}),
    ('legacy', 'L5 stdout empty', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': ''}),
    ('legacy', 'L5b stdout absent, no manifest', LEGACY, {}),
    ('legacy', 'L6 explicit error', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': ERR}),
    ('legacy', 'L6b error_max_turns without is_error', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': {'type': 'result', 'subtype': 'error_max_turns'}}),
    ('legacy', 'L7 success terminal', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': OK}),
    ('legacy', 'L7b success terminal + equal provider-result', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': OK, 'provider-result.json': OK}),
    ('legacy', 'L8 non-result JSON', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': {'type': 'assistant'}}),
    ('legacy', 'L8b JSON list', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': '[]'}),
    ('legacy', 'L9 truncated JSON', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': '{"type":"result","subtype":"succ'}),
    ('legacy', 'L9b invalid UTF-8', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': b'\xff\xfe{'}),
    ('legacy', 'L9c manifest unreadable', LEGACY, {'capture-manifest.json': '{bad', 'process.stdout': ''}),
    ('stream', 'S1 response.json exists', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'response.json': {}}),
    ('stream', 'S2 manifest declares legacy', STREAM, {'capture-manifest.json': MANIFEST['legacy_json']}),
    ('stream', 'S3 invalid witness', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-received-terminal.json': {'version': 1}}),
    ('stream', 'S4 non-error witness', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-received-terminal.json': {'version': 1, 'terminal': OK}}),
    ('stream', 'S5 error witness, no capture', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'process.outcome.json': STARTED, 'stream-received-terminal.json': {'version': 1, 'terminal': ERR}}),
    ('stream', 'S6 no outcome', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1']}),
    ('stream', 'S6b outcome not started', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'process.outcome.json': {'started': False, 'startup_state': 'not_started'}}),
    ('stream', 'S7 outcome started, no capture (the timeout dead end)', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'process.outcome.json': STARTED}),
    ('stream', 'S8 damaged', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': {**DONE, 'damaged': True}}),
    ('stream', 'S8b complete capture but manifest missing', STREAM, {'stream-capture.json': {**DONE, 'terminal_count': 0, 'protocol_complete': False}}),
    ('stream', 'S9 complete, zero terminals', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': {**DONE, 'terminal_count': 0, 'protocol_complete': False}}),
    ('stream', 'S10 two terminals', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': {**DONE, 'terminal_count': 2, 'protocol_complete': False},
                                             'stream-received-terminal.json': {'version': 1, 'terminal': ERR}}),
    ('stream', 'S11 one terminal recorded, not published, no witness', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': DONE}),
    ('stream', 'S12 published differs from witness', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': DONE,
                                                             'stream-terminal.json': ERR, 'stream-received-terminal.json': {'version': 1, 'terminal': {**ERR, 'x': 1}}}),
    ('stream', 'S13 provider-result differs from published', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': DONE,
                                                                     'stream-terminal.json': ERR, 'stream-received-terminal.json': {'version': 1, 'terminal': ERR},
                                                                     'provider-result.json': OK}),
    ('stream', 'S14 complete explicit error', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': DONE,
                                                       'stream-terminal.json': ERR, 'stream-received-terminal.json': {'version': 1, 'terminal': ERR}}),
    ('stream', 'S16 capture unreadable', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': '{bad'}),
    # Rows that pin each condition inside a check (added by the B1 code review).
    ('legacy', 'E1 only terminal-capture.json', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': '', 'terminal-capture.json': {}}),
    ('legacy', 'E2 only result.json', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': '', 'result.json': {}}),
    ('legacy', 'E3 only returned-result.json', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': '', 'returned-result.json': {}}),
    ('legacy', 'E4 stream-received-terminal.json beside JSON invocation', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': '', 'stream-received-terminal.json': W(OK)}),
    ('legacy', 'E5 stream-capture.json beside JSON invocation', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': '', 'stream-capture.json': DONE}),
    ('legacy', 'E6 is_error truthy but not true', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': {'type': 'result', 'is_error': 1}}),
    ('legacy', 'E7 subtype starts with error but not error_', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': {'type': 'result', 'subtype': 'errorish'}}),
    ('legacy', 'E7b is_error true without type result', LEGACY, {'capture-manifest.json': MANIFEST['legacy_json'], 'process.stdout': {'is_error': True}}),
    ('legacy', 'E7c manifest version 2', LEGACY, {'capture-manifest.json': {**MANIFEST['legacy_json'], 'version': 2}, 'process.stdout': ''}),
    ('stream', 'E8 published terminal without capture', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'process.outcome.json': STARTED, 'stream-terminal.json': ERR}),
    ('stream', 'E9 provider-result without capture', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'process.outcome.json': STARTED, 'provider-result.json': ERR}),
    ('stream', 'E10 outcome not started but startup_state starting', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'process.outcome.json': {'started': False, 'startup_state': 'starting'}}),
    ('stream', 'E11 outcome started with startup_state not_started', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'process.outcome.json': {'started': True, 'startup_state': 'not_started'}}),
    ('stream', 'E11b outcome without started key, not_started', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'process.outcome.json': {'startup_state': 'not_started'}}),
    ('stream', 'E11c outcome is a list', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'process.outcome.json': []}),
    ('stream', 'E12 capture version 1', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': {**DONE, 'version': 1}}),
    ('stream', 'E13 transport incomplete', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': {**DONE, 'transport_complete': False}}),
    ('stream', 'E14 terminal_count is a string', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': {**DONE, 'terminal_count': '1'}}),
    ('stream', 'E14b terminal_count negative', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': {**DONE, 'terminal_count': -1}}),
    ('stream', 'E15 capture is a list', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': []}),
    ('stream', 'E16 zero terminals but a published terminal', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': {**DONE, 'terminal_count': 0, 'protocol_complete': False}, 'stream-terminal.json': ERR}),
    ('stream', 'E17 zero terminals but a provider-result', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': {**DONE, 'terminal_count': 0, 'protocol_complete': False}, 'provider-result.json': ERR}),
    ('stream', 'E17b zero terminals but an error witness', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': {**DONE, 'terminal_count': 0, 'protocol_complete': False}, 'stream-received-terminal.json': W(ERR)}),
    ('stream', 'E18 one terminal, protocol incomplete', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': {**DONE, 'protocol_complete': False}}),
    ('stream', 'E19 two terminals, protocol complete', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': {**DONE, 'terminal_count': 2}}),
    ('stream', 'E20 witness version 2', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-received-terminal.json': W(OK, 2)}),
    ('stream', 'E21 witness is a list', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-received-terminal.json': []}),
    ('stream', 'E22 damaged is null', STREAM, {'capture-manifest.json': MANIFEST['stream_metadata_v1'], 'stream-capture.json': {**DONE, 'damaged': None}}),
]


def raw(value):
    data = value if isinstance(value, (str, bytes)) else json.dumps(value)
    return data if isinstance(data, bytes) else data.encode()


def stored(data):
    try:
        return {'text': data.decode('utf-8')}
    except UnicodeDecodeError:
        return {'base64': base64.b64encode(data).decode('ascii')}


rows = []
for mode, name, capture, files in ROWS:
    contents = {filename: raw(value) for filename, value in files.items()}
    with tempfile.TemporaryDirectory() as temporary:
        job = Path(temporary).resolve()
        for filename, data in contents.items():
            (job / filename).write_bytes(data)
        record = inspect_return(job, capture)
    assert set(record) == {'version', 'capture', 'state', 'reason', 'sources'} and set(record['sources']) == set(SOURCES)
    rows.append({'mode': mode, 'name': name, 'capture': capture,
                 'files': {filename: stored(data) for filename, data in contents.items()}, 'record': record})
    print(f"{mode:6} | {name:55} | {record['state']:9} | {record['reason']}")
document = {'generator': 'tests/data/worker_return_history_bc94b6f.gen.py', 'baseline': 'bc94b6f harness/worker_returns.py inspect_return',
            'rows': rows}
Path(sys.argv[2]).write_text(json.dumps(document, indent=1, sort_keys=True, ensure_ascii=True) + '\n', encoding='utf-8')
print(len(rows), 'rows;', len({(r['record']['state'], r['record']['reason']) for r in rows}), 'distinct outcomes')
