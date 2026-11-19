"""Read-only dimensions over recorded facts, separate from budget policy."""
from __future__ import annotations

import json
from pathlib import Path

from .provider_failures import read_original


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


def invocation_diagnostics(job, provider):
    """Provider-reported permission denials; why a call failed is its stop and judgment record."""
    value = {'permission_denials': {'status': 'unknown'}}
    if provider == 'claude':
        try:
            state, raw = read_original(job / 'process.stdout', lambda stream: stream.read(16 * 1024 * 1024 + 1))
            if state == 'read' and len(raw) <= 16 * 1024 * 1024:
                envelope = json.loads(raw)
                if isinstance(envelope, dict) and envelope.get('type') == 'result':
                    value['permission_denials'] = permission_denial_projection(envelope)
        except (ValueError, RuntimeError):
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
                        '不含指挥决定、返工与重做）'),
            'basis': 'recorded_stage_lower_bound; read-only, never changes a budget'}


def ledger_stage(run):
    """The step a planning or running ledger last recorded; a record only, never a sign the process is alive.

    Later sources override earlier keys: the task or whole-run stage, then the
    last running dispatch entry, then a recorded quota wait.
    """
    if not isinstance(run, dict) or run.get('status') not in ('planning', 'running'):
        return None
    def text(value):
        return value if isinstance(value, str) else None
    def number(value):
        return value if type(value) is int else None
    stage = dict.fromkeys(('step', 'task_id', 'attempt', 'role', 'kind', 'dispatch_id', 'process_state'))
    stage['step'] = 'between_steps'
    phases = {'verifying': 'verification', 'reviewing': 'review'}
    pending, accepting, post = (run.get(field) for field in ('pending_task', 'pending_acceptance', 'pending_post_accept'))
    history = run.get('final_history')
    history = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
    final = history[-1] if history else {}
    if isinstance(pending, dict) and text(pending.get('stage')) in phases:
        stage.update(step='task_' + phases[pending['stage']], task_id=text(pending.get('task_id')),
                     attempt=number(pending.get('attempt')))
    elif isinstance(accepting, dict):
        stage.update(step='task_acceptance', task_id=text(accepting.get('task_id')), attempt=number(accepting.get('attempt')))
    elif isinstance(post, dict):
        payload = post.get('payload')
        stage.update(step='task_acceptance', task_id=text(payload.get('task_id')) if isinstance(payload, dict) else None)
    elif final.get('status') == 'running' and text(final.get('phase')) in phases:
        stage.update(step='final_' + phases[final['phase']], task_id='final', attempt=number(final.get('attempt')))
    entries = run.get('dispatches')
    running = [entry for entry in entries if isinstance(entry, dict) and entry.get('status') == 'running'] \
        if isinstance(entries, list) else []
    if running:
        entry = running[-1]
        task_id = text(entry.get('task_id'))
        if entry.get('role') == 'worker':
            receipt = entry.get('worker_receipt')
            attempt = number(receipt.get('attempt')) if isinstance(receipt, dict) else None
        else:
            # Another role's entry carries no attempt; it shares the stage's only when it is that task's call.
            attempt = stage['attempt'] if task_id is not None and task_id == stage['task_id'] else None
        stage.update(step='dispatch', task_id=task_id, attempt=attempt, role=text(entry.get('role')),
                     kind=text(entry.get('kind')), dispatch_id=text(entry.get('id')),
                     process_state=text(entry.get('process_state')))
    waiting = run.get('quota_wait')
    if isinstance(waiting, dict):
        stage.update(step='quota_wait', role=text(waiting.get('role')), dispatch_id=text(waiting.get('dispatch_id')),
                     kind=None, process_state=None)
    return stage


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
    from .provider_failures import CODES, EXEMPT
    # Judged worker failures, by the provider code their judgment record carries.
    verdicts = dict.fromkeys((*CODES, 'malformed'), 0)
    exemptions = dict.fromkeys(('environment', 'operator', *EXEMPT, 'unknown'), 0)
    for entry in workers.values():
        if entry.get('worker_receipt_consumed') == 'provider_failure':
            evidence = entry.get('worker_return_evidence')
            provider = evidence.get('provider') if isinstance(evidence, dict) else None
            code = provider.get('code') if isinstance(provider, dict) else None
            verdicts[code if isinstance(code, str) and code in CODES else 'malformed'] += 1
        exempt = entry.get('attempt_exemption')
        if exempt:
            kind = None
            if isinstance(exempt, dict):
                kind = exempt.get('code') if exempt.get('basis') == 'provider_error' else exempt.get('blocker_kind')
            bound = (isinstance(exempt, dict) and isinstance(exempt.get('artifact_digest'), str)
                     and len(exempt['artifact_digest']) == 64 and isinstance(exempt.get('artifact'), str)
                     and Path(exempt['artifact']).parent.name == entry['id'])
            known = bound and isinstance(kind, str) and kind in exemptions and kind != 'unknown'
            exemptions[kind if known else 'unknown'] += 1
    config = run.get('config_snapshot', {})
    limits = config.get('runtime', {}) if isinstance(config, dict) else {}
    limits = limits if isinstance(limits, dict) else {}
    def budget(used, key):
        limit = limits.get(key)
        valid = type(used) is int and type(limit) is int
        return {'used': used, 'limit': limit, 'remaining': max(0, limit-used) if valid else None}
    started = sum(type(d.get('pgid')) is int and d['pgid'] > 0 for d in workers.values())
    shown = {'task_work': {tid: budget(count, 'max_attempts') for tid, count in work.items()},
             'agent_dispatches': budget(run.get('agent_dispatches'), 'max_agent_dispatches')}
    dimensions = {'actual_worker_invocations': started,
                  'charged_worker_invocations': len(workers),
                  'process_start_unconfirmed': len(workers) - started,
                  'reviewed_worker_candidates': len(reviewed),
                  'unlinked_review_records': len(unlinked), 'worker_failure_verdicts': verdicts,
                  'recorded_exemptions': exemptions,
                  'duplicate_dispatch_ids': duplicate}
    invalid = dict.fromkeys(('commander', 'worker', 'reviewer'), 0)
    for entry in unique.values():
        if entry.get('status') == 'invalid' and entry.get('role') in invalid:
            invalid[entry['role']] += 1
    dimensions['invalid_outputs'] = invalid
    return {'dimensions': dimensions,
            'limits': shown,
            'evidence_basis': 'recorded_ledger_links_not_revalidated_artifacts',
            'acceptance_basis': 'accepted_tasks', 'recovery_command': 'status ' + str(run.get('id'))}
