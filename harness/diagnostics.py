"""Read-only dimensions over recorded facts, separate from budget policy."""
from __future__ import annotations

import json
from pathlib import Path

from .util import ProcessStartError, ProcessTimeout


def _identifier(value):
    return value if isinstance(value, str) and 0 < len(value) <= 160 and all(
        char.isascii() and (char.isalnum() or char in "_-.:") for char in value) else None


def permission_denial_projection(value):
    """Reason is unknown unless a verified provider enum is supported."""
    entries = value.get("permission_denials", []) if isinstance(value, dict) else []
    if not isinstance(entries, list):
        return []
    projected = []
    for entry in entries[:100]:
        if not isinstance(entry, dict):
            continue
        item = {"reason": "unavailable"}
        for source, target in (("tool_name", "tool"), ("tool_use_id", "tool_id")):
            ident = _identifier(entry.get(source))
            if ident:
                item[target] = ident
        projected.append(item)
    return projected


def invocation_diagnostics(job, provider, error=None):
    value = {'failure_class': None, 'permission_denials': {'status': 'unknown'}}
    if isinstance(error, ProcessTimeout):
        value['failure_class'] = 'local_timeout'
    elif isinstance(error, ProcessStartError):
        value['failure_class'] = 'local_start'
    elif error is not None:
        value['failure_class'] = 'unknown'
    if provider == 'claude':
        try:
            with (job / 'process.stdout').open('rb') as stream:
                raw = stream.read(16 * 1024 * 1024 + 1)
            if len(raw) <= 16 * 1024 * 1024:
                envelope = json.loads(raw)
                if isinstance(envelope, dict) and envelope.get('type') == 'result':
                    value['permission_denials'] = permission_denial_projection(envelope)
                    if error is not None and (envelope.get('is_error') is True
                            or envelope.get('subtype') not in (None, 'success')):
                        value['failure_class'] = 'provider_reported'
        except (OSError, ValueError, RuntimeError):
            pass
    return value


def remaining_model_calls(run):
    """Model calls still needed if every later call passes first time; never an over-estimate.

    Counted from the recorded stage only: an approved task awaiting acceptance
    or with a started task review (its returned result may still approve)
    counts 0; a task with a candidate (pending, or a started worker whose
    result may still be usable) needs only its review, 1; any other unaccepted
    task needs a worker and a review, 2. The whole-run review counts 1 unless
    every task is accepted and an approval of the accepted HEAD, or a started
    whole-run review, already exists. Terminal runs return None.
    """
    if not isinstance(run, dict) or run.get('status') in ('completed', 'abandoned'):
        return None
    accepted = run.get('accepted')
    accepted = accepted if isinstance(accepted, dict) else {}
    tasks = run.get('tasks')
    tasks = [task['id'] for task in tasks if isinstance(task, dict) and isinstance(task.get('id'), str)] \
        if isinstance(tasks, list) else []

    def stage_task(field):
        value = run.get(field)
        return value.get('task_id') if isinstance(value, dict) else None

    def keys(field):
        value = run.get(field)
        return set(value) if isinstance(value, dict) else set()

    review_receipts, worker_receipts = keys('review_receipts'), keys('worker_receipts')
    remaining = {}
    for task_id in dict.fromkeys(tasks):
        if task_id in accepted:
            continue
        if stage_task('pending_acceptance') == task_id or f'reviewer:review:{task_id}' in review_receipts:
            remaining[task_id] = 0
        elif stage_task('pending_task') == task_id or f'worker:implement:{task_id}' in worker_receipts:
            remaining[task_id] = 1
        else:
            remaining[task_id] = 2
    final = 1
    if not remaining:
        history = run.get('final_history')
        history = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
        if ('reviewer:final_review:final' in review_receipts
                or any(item.get('status') == 'approved' and item.get('head') == run.get('expected_head')
                       for item in history)):
            final = 0
    bound = sum(remaining.values()) + final
    return {'lower_bound': bound, 'tasks': remaining, 'final_review': final,
            'message': (f'如果之后全部一次通过，至少还需 {bound} 次模型调用（下界：只计 worker、任务审阅与整体审阅，'
                        '不含指挥调用、返工、格式修正与再规划）'),
            'basis': 'recorded_stage_lower_bound; read-only, never changes a budget'}


def budget_explanation(run, work):
    entries = run.get('dispatches', [])
    entries = entries if isinstance(entries, list) else []
    unique, duplicate = {}, 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('id'), str):
            continue
        if entry['id'] in unique:
            duplicate += 1
        else:
            unique[entry['id']] = entry
    workers = {key: d for key, d in unique.items() if d.get('role') == 'worker' and d.get('invoked') is True}
    reviewed, unlinked = set(), set()
    history = run.get('review_history', {})
    for task_id, records in (history.items() if isinstance(history, dict) else []):
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            source = record.get('worker_source', {})
            ident = source.get('dispatch_id') if isinstance(source, dict) else None
            if (ident in workers and workers[ident].get('task_id') == task_id
                    and isinstance(source.get('artifact_digest'), str) and len(source['artifact_digest']) == 64):
                reviewed.add(ident)
            else:
                unlinked.add((task_id, record.get('artifact')))
    failures = dict.fromkeys(('provider_reported', 'local_timeout', 'local_start', 'unknown'), 0)
    exemptions = dict.fromkeys(('environment', 'operator', 'unknown'), 0)
    for entry in workers.values():
        diag = entry.get('diagnostics') or {}
        if entry.get('status') == 'failed':
            kind = diag.get('failure_class') if isinstance(diag, dict) else None
            failures[kind if kind in failures else 'unknown'] += 1
        exempt = entry.get('attempt_exemption')
        if exempt:
            kind = exempt.get('blocker_kind') if isinstance(exempt, dict) else None
            bound = (isinstance(exempt, dict) and isinstance(exempt.get('artifact_digest'), str)
                     and len(exempt['artifact_digest']) == 64 and isinstance(exempt.get('artifact'), str)
                     and Path(exempt['artifact']).parent.name == entry['id'])
            exemptions[kind if bound and kind in ('environment', 'operator') else 'unknown'] += 1
    config = run.get('config_snapshot', {})
    limits = config.get('runtime', {}) if isinstance(config, dict) else {}
    limits = limits if isinstance(limits, dict) else {}
    def budget(used, key):
        limit = limits.get(key)
        valid = type(used) is int and type(limit) is int
        return {'used': used, 'limit': limit, 'remaining': max(0, limit-used) if valid else None}
    started = sum(type(d.get('pgid')) is int and d['pgid'] > 0 for d in workers.values())
    shown = {'task_work': {tid: budget(count, 'max_attempts') for tid, count in work.items()},
             'agent_dispatches': budget(run.get('agent_dispatches'), 'max_agent_dispatches'),
             'replans': budget(run.get('replan_attempts'), 'max_replans'),
             'final_attempts': budget(run.get('final_attempts'), 'max_final_attempts')}
    if 'total_attempts' in run:
        # Historical display branch: schema 15 and earlier saved a run-wide
        # worker counter and limit; schema 16 and later have neither.
        shown['total_worker'] = budget(run.get('total_attempts'), 'max_total_attempts')
    return {'dimensions': {'actual_worker_invocations': started,
                'charged_worker_invocations': len(workers),
                'process_start_unconfirmed': len(workers) - started,
                'reviewed_worker_candidates': len(reviewed),
                'unlinked_review_records': len(unlinked), 'failed_worker_invocations': failures,
                'recorded_exemptions': exemptions,
                'worker_format_corrections': sum(d.get('format_correction') is True for d in workers.values()),
                'historical_invocation_unknown': sum(d.get('role') == 'worker' and 'invoked' not in d for d in unique.values()),
                'duplicate_dispatch_ids': duplicate},
            'limits': shown,
            'evidence_basis': 'recorded_ledger_links_not_revalidated_artifacts',
            'acceptance_basis': 'accepted_tasks', 'recovery_command': 'status ' + str(run.get('id'))}
