"""Read-only dimensions over recorded facts, separate from budget policy."""
from __future__ import annotations

import json
from pathlib import Path

from .stream_capture import claude_result_path, permission_denial_projection
from .util import ProcessStartError, ProcessTimeout


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
            with claude_result_path(job).open('rb') as stream:
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
    return {'dimensions': {'actual_worker_invocations': started,
                'charged_worker_invocations': len(workers),
                'process_start_unconfirmed': len(workers) - started,
                'reviewed_worker_candidates': len(reviewed),
                'unlinked_review_records': len(unlinked), 'failed_worker_invocations': failures,
                'recorded_exemptions': exemptions,
                'worker_format_corrections': sum(d.get('format_correction') is True for d in workers.values()),
                'historical_invocation_unknown': sum(d.get('role') == 'worker' and 'invoked' not in d for d in unique.values()),
                'duplicate_dispatch_ids': duplicate},
            'limits': {'task_work': {tid: budget(count, 'max_attempts') for tid, count in work.items()},
                       'total_worker': budget(run.get('total_attempts'), 'max_total_attempts'),
                       'agent_dispatches': budget(run.get('agent_dispatches'), 'max_agent_dispatches'),
                       'replans': budget(run.get('replan_attempts'), 'max_replans'),
                       'final_attempts': budget(run.get('final_attempts'), 'max_final_attempts')},
            'evidence_basis': 'recorded_ledger_links_not_revalidated_artifacts',
            'acceptance_basis': 'accepted_tasks', 'recovery_command': 'status ' + str(run.get('id'))}
