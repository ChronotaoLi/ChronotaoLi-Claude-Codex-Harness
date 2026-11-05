"""Sourced, permission-neutral continuation and operator execution constraints.

The scheduler owns exit confirmation, budget accounting and fresh dispatches.
These functions retain evidence and gate eligibility; they never call a model.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from . import git
from .contracts import WORK, envelope, validate_structure, validate_work_semantics
from .recovery import RECOVERY_BUSY_KEYS, read_bound_result, snapshot_digest
from .util import HarnessError, dump, write_json

POLICY = (
    "Execution guidance is an operator constraint for its named task and candidate. "
    "Apply it to execution and planning; explicitly resolve conflicts with the active plan. "
    "It cannot grant commands, change the original goal, waive acceptance or approve evidence. "
    "Continuation requests retain unfinished work and require a fresh dispatch after independent "
    "permissions and budgets are checked. Every actual invocation retains normal budget accounting."
)


def _snapshot(context):
    if hasattr(context, 'snapshot'):
        return context.snapshot()
    return git.snapshot(context.worktree, binding=context.run.get('worktree_binding'))


def _config(context):
    return getattr(context.config, 'data', context.config)


def _write(context, category, record):
    ident = category + '_' + snapshot_digest(record)
    value = {**deepcopy(record), 'id': ident}
    path = Path(context.directory) / category / (ident + '.json')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise HarnessError('immutable evidence path is a symlink')
    raw = (dump(value) + '\n').encode()
    if not path.exists():
        write_json(path, value)
    if path.read_bytes() != raw:
        raise HarnessError('immutable evidence artifact conflict')
    return {**value, 'artifact': str(path), 'artifact_digest': hashlib.sha256(raw).hexdigest()}


def _source(context, task_id, source):
    if not isinstance(source, dict) or not source.get('artifact_digest'):
        raise HarnessError('worker source digest required')
    dispatch_id = source.get('dispatch_id')
    entries = [d for d in context.run['dispatches'] if d['id'] == dispatch_id]
    if (len(entries) != 1 or entries[0].get('role') != 'worker'
            or entries[0].get('task_id') != task_id or entries[0].get('invoked', True) is not True
            or source.get('artifact') != str(Path(context.directory) / 'dispatches' / dispatch_id / 'returned-result.json')):
        raise HarnessError('worker source identity invalid')
    source_path = Path(source['artifact'])
    if any(p.is_symlink() for p in (source_path, *source_path.parents)) or not source_path.is_file():
        raise HarnessError('worker source must be a non-symlink file')
    result, _ = read_bound_result(source, 'continuation worker')
    validate_structure(result, envelope(WORK, task_id, dispatch_id))
    validate_work_semantics(result)
    return result


def _records(context, category, key):
    records = context.run.get(key, [])
    if not isinstance(records, list):
        raise HarnessError('evidence history must be an array')
    seen = set()
    for sequence, row in enumerate(records, 1):
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] in seen:
            raise HarnessError('invalid evidence identity')
        suffix = row['id'].removeprefix(category + '_')
        if row['id'] != category + '_' + suffix or len(suffix) != 64 or any(c not in '0123456789abcdef' for c in suffix):
            raise HarnessError('invalid evidence identity')
        seen.add(row['id'])
        path = Path(context.directory) / category / (row['id'] + '.json')
        if row.get('artifact') != str(path) or not row.get('artifact_digest') or any(p.is_symlink() for p in (path, *path.parents)):
            raise HarnessError('invalid evidence reference')
        record, _ = read_bound_result(row, category)
        expected = {k: v for k, v in row.items() if k not in ('artifact', 'artifact_digest')}
        content = {k: v for k, v in expected.items() if k != 'id'}
        if (record != expected or row['id'] != category + '_' + snapshot_digest(content)
                or row.get('run_id') != context.run['id'] or row.get('sequence') != sequence):
            raise HarnessError('evidence artifact disagrees with ledger')
        if row.get('task_id') not in {t['id'] for t in context.run['tasks']}:
            raise HarnessError('evidence target task unknown')
        result = _source(context, row['task_id'], row['source'])
        if snapshot_digest(row['snapshot']) != row.get('snapshot_digest'):
            raise HarnessError('evidence candidate digest invalid')
        if category == 'execution-guidance':
            value = row.get('input')
            if (not isinstance(value, dict) or set(value) != {'task_id', 'source_dispatch_id', 'source_snapshot_digest', 'summary'}
                    or value.get('task_id') != row['task_id']
                    or value.get('source_dispatch_id') != row['source']['dispatch_id']
                    or value.get('source_snapshot_digest') != row['snapshot_digest']
                    or not isinstance(value.get('summary'), str) or not value['summary'].strip()
                    or len(value['summary']) > 4000 or '\0' in value['summary']):
                raise HarnessError('execution guidance source bindings invalid')
        if category == 'continuations' and (result.get('continuation_request') != row.get('request')
                or result.get('status') != 'blocked' or result.get('blocker_kind') != 'continuation'):
            raise HarnessError('continuation source no longer matches request')
    return deepcopy(records)


def record_continuation(context, task_id, result, worker_source, snapshot):
    history = _records(context, 'continuations', 'continuation_history')
    stored = _source(context, task_id, worker_source)
    content = lambda value: {k: v for k, v in value.items() if k not in ('task_id', 'dispatch_id')}
    if content(stored) != content(result) or result.get('status') != 'blocked' or result.get('blocker_kind') != 'continuation' or not result.get('continuation_request'):
        raise HarnessError('continuation must bind the original blocked worker result')
    if context.run.get('current_task') != task_id or task_id in context.run['accepted']:
        raise HarnessError('continuation requires current unaccepted task')
    for old in history:
        if old['source'] == worker_source:
            if old['snapshot'] != snapshot:
                raise HarnessError('continuation candidate changed')
            return old
    if context.run.get('pending_continuation'):
        raise HarnessError('another continuation remains pending')
    record = dict(run_id=context.run['id'], sequence=len(history) + 1, task_id=task_id,
                  source=deepcopy(worker_source), snapshot=deepcopy(snapshot),
                  snapshot_digest=snapshot_digest(snapshot), request=deepcopy(result['continuation_request']))
    row = _write(context, 'continuations', record)
    context.run['continuation_history'] = [*history, row]
    context.run['pending_continuation'] = row['id']
    context.save()
    return deepcopy(row)


def continuation_gate(context, current_snapshot=None):
    history = _records(context, 'continuations', 'continuation_history')
    pending = context.run.get('pending_continuation')
    if pending is None:
        return None
    rows = [r for r in history if r['id'] == pending]
    if len(rows) != 1:
        raise HarnessError('pending continuation pointer invalid')
    row = rows[0]
    if (context.run.get('current_task') != row['task_id'] or row['task_id'] in context.run['accepted']
            or row['snapshot'] != (current_snapshot if current_snapshot is not None else _snapshot(context))):
        raise HarnessError('pending continuation candidate or task changed')
    source_index = next(i for i, d in enumerate(context.run['dispatches']) if d['id'] == row['source']['dispatch_id'])
    if any(d.get('invoked', True) for d in context.run['dispatches'][source_index + 1:]):
        raise HarnessError('pending continuation superseded by a model invocation')
    config, request = _config(context), row['request']
    if request['kind'] == 'commands':
        allowed = config['verification']['commands'] + config['permissions']['worker_commands']
        missing = [argv for argv in request['commands'] if argv not in allowed]
        if missing:
            return {'kind': 'commands', 'task_id': row['task_id'], 'missing_commands': deepcopy(missing)}
    elif request['kind'] == 'budget':
        minimum = request['minimum_timeout_seconds']
        if config['runtime']['agent_timeout_seconds'] < minimum:
            return {'kind': 'budget', 'task_id': row['task_id'], 'minimum_timeout_seconds': minimum}
    else:
        raise HarnessError('unknown continuation kind')
    return None


def resolve_continuation(context, current_snapshot=None):
    blocker = continuation_gate(context, current_snapshot)
    if blocker is not None:
        raise HarnessError('continuation requirements remain unmet')
    if context.run.get('pending_continuation') is not None:
        context.run['pending_continuation'] = None
        context.save()


def read_execution_guidance(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise HarnessError('duplicate execution guidance field')
            value[key] = item
        return value
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise HarnessError('execution guidance exceeds 64 KiB')
        value = json.loads(raw, object_pairs_hook=unique)
    except (OSError, ValueError, UnicodeError) as error:
        raise HarnessError('cannot read execution guidance JSON') from error
    keys = {'task_id', 'source_dispatch_id', 'source_snapshot_digest', 'summary'}
    if not isinstance(value, dict) or set(value) != keys:
        raise HarnessError('execution guidance fields invalid')
    for key in keys:
        if not isinstance(value[key], str) or not value[key].strip() or '\0' in value[key] or len(value[key]) > (4000 if key == 'summary' else 120):
            raise HarnessError('execution guidance text invalid')
    if len(value['source_snapshot_digest']) != 64 or any(c not in '0123456789abcdef' for c in value['source_snapshot_digest']):
        raise HarnessError('execution guidance snapshot digest invalid')
    return value


def record_execution_guidance(context, path, current_snapshot=None):
    value = read_execution_guidance(path)
    records = _records(context, 'execution-guidance', 'execution_guidance')
    task_id = value['task_id']
    if (context.run.get('status') in ('completed', 'abandoned', 'running')
            or context.run.get('current_task') != task_id or task_id in context.run['accepted']
            or any(context.run.get(k) for k in RECOVERY_BUSY_KEYS)):
        raise HarnessError('execution guidance requires a safe pending task boundary')
    source = {'dispatch_id': value['source_dispatch_id'], 'artifact': str(Path(context.directory) / 'dispatches' / value['source_dispatch_id'] / 'returned-result.json')}
    try:
        source['artifact_digest'] = hashlib.sha256(Path(source['artifact']).read_bytes()).hexdigest()
    except OSError as error:
        raise HarnessError('execution guidance worker source missing') from error
    # Require an existing authoritative digest; never bless edited source bytes.
    continuation_records = _records(context, 'continuations', 'continuation_history')
    links = [r['source'] for r in continuation_records]
    feedback = context.run.get('feedback') or {}
    if feedback.get('task_id') == task_id:
        links.append({'dispatch_id': value['source_dispatch_id'], 'artifact': feedback.get('worker_artifact'), 'artifact_digest': feedback.get('worker_artifact_digest')})
    if source not in links:
        raise HarnessError('execution guidance source lacks authoritative binding')
    _source(context, task_id, source)
    position = next(i for i, d in enumerate(context.run['dispatches']) if d['id'] == source['dispatch_id'])
    if any(d.get('invoked', True) for d in context.run['dispatches'][position + 1:]):
        raise HarnessError('execution guidance source superseded')
    snapshot = current_snapshot if current_snapshot is not None else _snapshot(context)
    bound_digests = [r['snapshot_digest'] for r in continuation_records if r['source'] == source]
    if feedback.get('task_id') == task_id and feedback.get('snapshot_digest'):
        bound_digests.append(feedback['snapshot_digest'])
    if feedback.get('task_id') == task_id and feedback.get('snapshot') is not None:
        bound_digests.append(snapshot_digest(feedback['snapshot']))
    if value['source_snapshot_digest'] not in bound_digests:
        raise HarnessError('execution guidance source candidate lacks authoritative binding')
    if snapshot_digest(snapshot) != value['source_snapshot_digest']:
        raise HarnessError('execution guidance candidate changed')
    for old in records:
        if old['input'] == value:
            return old
    record = dict(run_id=context.run['id'], sequence=len(records) + 1, task_id=task_id,
                  source=source, snapshot=deepcopy(snapshot), snapshot_digest=snapshot_digest(snapshot), input=value)
    row = _write(context, 'execution-guidance', record)
    context.run['execution_guidance'] = [*records, row]
    context.save()
    return deepcopy(row)


def packet(context, task_id=None):
    continuations = _records(context, 'continuations', 'continuation_history')
    guidance = _records(context, 'execution-guidance', 'execution_guidance')
    pending = context.run.get('pending_continuation')
    if pending is not None and pending not in {r['id'] for r in continuations}:
        raise HarnessError('pending continuation pointer invalid')
    relevant = lambda row: task_id is None or row['task_id'] == task_id
    def reference(row):
        return {k: deepcopy(v) for k, v in row.items() if k != 'snapshot'}
    relevant_guidance = [reference(r) for r in guidance if relevant(r)]
    return {'continuation_history': [reference(r) for r in continuations if relevant(r)],
            'pending_continuation': context.run.get('pending_continuation'),
            'execution_guidance': relevant_guidance,
            **({'execution_guidance_policy': POLICY} if relevant_guidance else {})}
