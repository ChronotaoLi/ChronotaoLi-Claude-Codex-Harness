"""Sourced, permission-neutral continuation and operator execution constraints.

The scheduler owns exit confirmation, budget accounting and fresh dispatches.
These functions retain evidence and gate eligibility; they never call a model.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from . import git
from .contracts import validate_structure, validate_work_semantics
from .worker_receipts import delivered_work_contract
from .git_query import covers as git_query_covers
from .recovery import RECOVERY_BUSY_KEYS, read_bound_result, snapshot_digest
from .util import HarnessError, dump, write_json

def _snapshot(context):
    if hasattr(context, 'snapshot'):
        return context.snapshot()
    return git.snapshot(context.worktree, binding=context.run.get('worktree_binding'))


def _config(context):
    return getattr(context.config, 'data', context.config)


def _identity(category, record):
    """Content identity of a complete artifact without its own ID (algorithm unchanged)."""
    return category + '_' + snapshot_digest(record)


def _projection(artifact):
    """Ledger projection of a complete artifact: everything except the whole snapshot.

    The row keeps snapshot_digest; artifact and artifact_digest reference the file
    that alone holds the snapshot.
    """
    return {key: deepcopy(value) for key, value in artifact.items() if key != 'snapshot'}


def _write(context, category, record):
    """Publish the complete immutable artifact, then return its ledger projection."""
    ident = _identity(category, record)
    value = {**deepcopy(record), 'id': ident}
    path = Path(context.directory) / category / (ident + '.json')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise HarnessError('immutable evidence path is a symlink')
    raw = (dump(value) + '\n').encode()
    if not path.exists():
        write_json(path, value)
    if path.read_bytes() != raw:
        raise HarnessError('immutable evidence artifact conflict')
    return {**_projection(value), 'artifact': str(path), 'artifact_digest': hashlib.sha256(raw).hexdigest()}


def _complete(context, category, row):
    """Validate a ledger projection together with its referenced complete artifact."""
    if not isinstance(row, dict) or not isinstance(row.get('id'), str):
        raise HarnessError('invalid evidence identity')
    suffix = row['id'].removeprefix(category + '_')
    if row['id'] != category + '_' + suffix or len(suffix) != 64 or any(c not in '0123456789abcdef' for c in suffix):
        raise HarnessError('invalid evidence identity')
    path = Path(context.directory) / category / (row['id'] + '.json')
    if row.get('artifact') != str(path) or not row.get('artifact_digest') or any(p.is_symlink() for p in (path, *path.parents)):
        raise HarnessError('invalid evidence reference')
    record, _ = read_bound_result(row, category)
    projection = {k: v for k, v in row.items() if k not in ('artifact', 'artifact_digest')}
    if (not isinstance(record, dict) or 'snapshot' in projection or 'snapshot' not in record
            or _projection(record) != projection
            or row['id'] != _identity(category, {k: v for k, v in record.items() if k != 'id'})):
        raise HarnessError('evidence artifact disagrees with ledger')
    if snapshot_digest(record['snapshot']) != record.get('snapshot_digest'):
        raise HarnessError('evidence candidate digest invalid')
    return record


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
    validate_structure(result, delivered_work_contract(context.run, context.directory, dispatch_id))
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
        _complete(context, category, row)
        seen.add(row['id'])
        if row.get('run_id') != context.run['id'] or row.get('sequence') != sequence:
            raise HarnessError('evidence artifact disagrees with ledger')
        if row.get('task_id') not in {t['id'] for t in context.run['tasks']}:
            raise HarnessError('evidence target task unknown')
        result = _source(context, row['task_id'], row['source'])
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


def record_continuation(context, task_id, result, worker_source, snapshot, *, save=True):
    history = _records(context, 'continuations', 'continuation_history')
    rejections, _ = _rejections(context, history)
    stored = _source(context, task_id, worker_source)
    content = lambda value: {k: v for k, v in value.items() if k not in ('task_id', 'dispatch_id')}
    if content(stored) != content(result) or result.get('status') != 'blocked' or result.get('blocker_kind') != 'continuation' or not result.get('continuation_request'):
        raise HarnessError('continuation must bind the original blocked worker result')
    if context.run.get('current_task') != task_id or task_id in context.run['accepted']:
        raise HarnessError('continuation requires current unaccepted task')
    for old in history:
        if old['source'] == worker_source:
            if any(row['continuation_id'] == old['id'] for row in rejections):
                raise HarnessError('rejected continuation source cannot be replayed')
            if old['snapshot_digest'] != snapshot_digest(snapshot):
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
    if save:
        context.save()
    return deepcopy(row)


def continuation_gate(context, current_snapshot=None):
    history = _records(context, 'continuations', 'continuation_history')
    rejections, _ = _rejections(context, history)
    pending = context.run.get('pending_continuation')
    if pending is None:
        return None
    rows = [r for r in history if r['id'] == pending]
    if len(rows) != 1:
        raise HarnessError('pending continuation pointer invalid')
    row = rows[0]
    if any(r['continuation_id'] == pending for r in rejections):
        raise HarnessError('rejected continuation cannot become pending again')
    if (context.run.get('current_task') != row['task_id'] or row['task_id'] in context.run['accepted']
            or row['snapshot_digest'] != snapshot_digest(current_snapshot if current_snapshot is not None else _snapshot(context))):
        raise HarnessError('pending continuation candidate or task changed')
    source_index = next(i for i, d in enumerate(context.run['dispatches']) if d['id'] == row['source']['dispatch_id'])
    if any(d.get('invoked', True) for d in context.run['dispatches'][source_index + 1:]):
        raise HarnessError('pending continuation superseded by a model invocation')
    config, request = _config(context), row['request']
    if request['kind'] == 'commands':
        allowed = config['verification']['commands'] + config['permissions']['worker_commands']
        # Built-in harness.git_query forms need no grant; everything else stays exact.
        missing = [argv for argv in request['commands'] if argv not in allowed and not git_query_covers(argv)]
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


def _read_input(path, keys, text_key, label):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise HarnessError(f'duplicate {label} field')
            value[key] = item
        return value
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise HarnessError(f'{label} exceeds 64 KiB')
        value = json.loads(raw, object_pairs_hook=unique)
    except (OSError, ValueError, UnicodeError) as error:
        raise HarnessError(f'cannot read {label} JSON') from error
    if not isinstance(value, dict) or set(value) != keys:
        raise HarnessError(f'{label} fields invalid')
    for key in keys:
        if not isinstance(value[key], str) or not value[key].strip() or '\0' in value[key] or len(value[key]) > (4000 if key == text_key else 120):
            raise HarnessError(f'{label} text invalid')
    if len(value['source_snapshot_digest']) != 64 or any(c not in '0123456789abcdef' for c in value['source_snapshot_digest']):
        raise HarnessError(f'{label} snapshot digest invalid')
    return value


def read_execution_guidance(path):
    return _read_input(path, {'task_id', 'source_dispatch_id', 'source_snapshot_digest', 'summary'},
                       'summary', 'execution guidance')


def _revision_directive(row):
    return {'directive': deepcopy(row['directive']),
            'snapshot_digest': row['snapshot_digest'], 'continuation_rejection_id': row['id']}


def _rejections(context, history=None):
    """Validate resolutions and derive the guard from immutable, unconsumed history."""
    history = _records(context, 'continuations', 'continuation_history') if history is None else history
    records = _records(context, 'continuation-rejections', 'continuation_rejections')
    originals = {row['id']: row for row in history}
    seen, active = set(), []
    entries = context.run['dispatches']
    for row in records:
        original = originals.get(row.get('continuation_id'))
        value = row.get('input')
        directive = row.get('directive')
        # Both complete artifacts were checked against their snapshot digests.
        if (original is None or original['id'] in seen
                or any(row.get(key) != original[key] for key in ('task_id', 'source', 'snapshot_digest'))
                or not isinstance(value, dict)
                or set(value) != {'continuation_id', 'task_id', 'source_dispatch_id', 'source_snapshot_digest', 'reason'}
                or value.get('continuation_id') != original['id'] or value.get('task_id') != row['task_id']
                or value.get('source_dispatch_id') != row['source']['dispatch_id']
                or value.get('source_snapshot_digest') != row['snapshot_digest']
                or not isinstance(value.get('reason'), str) or not value['reason'].strip()
                or len(value['reason']) > 4000 or '\0' in value['reason']
                or not isinstance(directive, dict) or set(directive) != {'selected_task', 'action', 'instruction'}
                or directive.get('selected_task') != row['task_id'] or directive.get('action') != 'implement'
                or not isinstance(directive.get('instruction'), str) or not directive['instruction'].strip()):
            raise HarnessError('continuation rejection source bindings invalid')
        seen.add(original['id'])
        source_position = next(i for i, entry in enumerate(entries) if entry['id'] == row['source']['dispatch_id'])
        consumers = [(i, entry) for i, entry in enumerate(entries)
                     if entry.get('continuation_rejection_id') == row['id']]
        if len(consumers) > 1:
            raise HarnessError('continuation rejection consumed more than once')
        if consumers:
            position, entry = consumers[0]
            if (position <= source_position or entry.get('invoked') is not True
                    or entry.get('role') != 'worker' or entry.get('task_id') != row['task_id']
                    or entry.get('snapshot_digest') != row['snapshot_digest']):
                raise HarnessError('continuation rejection consumption invalid')
        else:
            position = len(entries)
            active.append(row)
        if any(entry.get('invoked', True) for entry in entries[source_position + 1:position]):
            raise HarnessError('continuation rejection source superseded')
        if context.run.get('pending_continuation') == original['id']:
            raise HarnessError('rejected continuation cannot become pending again')
    identifiers = {row['id'] for row in records}
    if any('continuation_rejection_id' in entry and entry['continuation_rejection_id'] not in identifiers for entry in entries):
        raise HarnessError('continuation rejection consumption lacks evidence')
    directive = context.run.get('pending_directive') or {}
    if len(active) > 1:
        raise HarnessError('multiple continuation revisions pending')
    if active:
        row = active[0]
        if (context.run.get('current_task') != row['task_id'] or row['task_id'] in context.run['accepted']
                or context.run.get('pending_continuation') is not None or directive != _revision_directive(row)
                or any(context.run.get(key) for key in RECOVERY_BUSY_KEYS if key != 'pending_directive')
                or context.run.get('replan_stopped')):
            raise HarnessError('continuation revision task or directive changed')
    elif 'continuation_rejection_id' in directive:
        raise HarnessError('continuation revision directive lacks active evidence')
    return records, active[0] if active else None


def validate_continuation_rejection(context, current_snapshot=None, worker_task=None):
    _, active = _rejections(context)
    if active:
        snapshot = current_snapshot if current_snapshot is not None else _snapshot(context)
        if active['snapshot_digest'] != snapshot_digest(snapshot):
            raise HarnessError('continuation revision candidate changed')
        if worker_task is not None and worker_task != active['task_id']:
            raise HarnessError('continuation revision requires its named worker')
    return active


def record_continuation_rejection(context, path, current_snapshot=None):
    value = _read_input(path, {'continuation_id', 'task_id', 'source_dispatch_id', 'source_snapshot_digest', 'reason'},
                        'reason', 'continuation rejection')
    history = _records(context, 'continuations', 'continuation_history')
    records, active = _rejections(context, history)
    if context.run.get('status') not in ('blocked', 'interrupted', 'paused'):
        raise HarnessError('continuation rejection requires a safe pending task boundary')
    old = next((row for row in records if row['continuation_id'] == value['continuation_id']), None)
    if old is not None:
        if old['input'] != value or active is None or active['id'] != old['id']:
            raise HarnessError('continuation rejection conflicts with prior input or was already consumed')
        validate_continuation_rejection(context, current_snapshot)
        return deepcopy(old)
    if active or any(context.run.get(key) for key in RECOVERY_BUSY_KEYS) or context.run.get('replan_stopped'):
        raise HarnessError('continuation rejection requires a safe pending task boundary')
    pending = context.run.get('pending_continuation')
    row = next((row for row in history if row['id'] == pending), None)
    if (row is None or value['continuation_id'] != pending or value['task_id'] != row['task_id']
            or value['source_dispatch_id'] != row['source']['dispatch_id']
            or value['source_snapshot_digest'] != row['snapshot_digest']):
        raise HarnessError('continuation rejection must bind the current request and candidate')
    # Check identity, source, candidate and dispatch order, without granting its requirements.
    continuation_gate(context, current_snapshot)
    # The complete rejection artifact keeps the original's whole snapshot.
    original = _complete(context, 'continuations', row)
    record = dict(run_id=context.run['id'], sequence=len(records) + 1,
                  continuation_id=pending, input=value,
                  **{key: deepcopy(original[key]) for key in ('task_id', 'source', 'snapshot', 'snapshot_digest')})
    # Freeze the actual instruction with the evidence. Recovery must not depend
    # on the current runtime's prompt wording when the instruction was issued.
    record['directive'] = {'selected_task': row['task_id'], 'action': 'implement',
        'instruction': 'Revise the retained candidate following the operator rejection. '
                       'The rejected continuation is no longer a prerequisite. '
                       'Use only existing permissions; request any new exact commands separately. '
                       'Preserve the original task scope and acceptance. Reason: ' + value['reason']}
    rejection = _write(context, 'continuation-rejections', record)
    context.run['continuation_rejections'] = [*records, rejection]
    context.run['pending_continuation'] = None
    context.run['pending_directive'] = _revision_directive(rejection)
    # Publish the resolution and candidate guard together. An orphan immutable file
    # from a failed save is harmless; an identical retry adopts the same bytes.
    context.save()
    return deepcopy(rejection)


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
    rejections, active = _rejections(context, continuations)
    guidance = _records(context, 'execution-guidance', 'execution_guidance')
    pending = context.run.get('pending_continuation')
    if pending is not None and pending not in {r['id'] for r in continuations}:
        raise HarnessError('pending continuation pointer invalid')
    relevant = lambda row: task_id is None or row['task_id'] == task_id
    def reference(row):
        return {k: deepcopy(v) for k, v in row.items() if k != 'directive'}
    relevant_guidance = [reference(r) for r in guidance if relevant(r)]
    resolutions = {r['continuation_id']: {'status': 'rejected', 'rejection_id': r['id']} for r in rejections}
    return {'continuation_history': [{**reference(r), **({'resolution': resolutions[r['id']]} if r['id'] in resolutions else {})}
                                    for r in continuations if relevant(r)],
            'pending_continuation': context.run.get('pending_continuation'),
            **({'continuation_rejections': [reference(r) for r in rejections if relevant(r)],
                'active_continuation_rejection': active['id'] if active and relevant(active) else None} if rejections else {}),
            'execution_guidance': relevant_guidance}
