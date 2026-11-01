"""Advisory, digest-bound plan coverage; never grants execution or acceptance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .contracts import validate_plan
from .config import validate_commands
from .util import HarnessError, write_json


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def build_readiness(plan, goal, coverage=None):
    if not isinstance(goal, str) or not goal.strip():
        raise HarnessError('Readiness requires the original goal text')
    tasks = validate_plan(plan, max_tasks=max(20, len(plan.get('tasks', []))))
    by_id = {task['id']: task for task in tasks}
    coverage = {} if coverage is None else coverage
    if not isinstance(coverage, dict) or coverage.get('schema_version', 1) != 1:
        raise HarnessError('Invalid readiness coverage version')
    requirements = coverage.get('requirements', [])
    details = coverage.get('tasks', {})
    if not isinstance(requirements, list) or not isinstance(details, dict) or set(details) - by_id.keys():
        raise HarnessError('Invalid readiness coverage tasks/requirements')
    warnings, mapping, seen = [], [], set()

    def closure(ident):
        result, pending = set(), [ident]
        while pending:
            current = pending.pop()
            if current not in result:
                result.add(current)
                pending.extend(by_id[current]['depends_on'])
        return result

    for item in requirements:
        if not isinstance(item, dict):
            raise HarnessError('Requirement must be an object')
        ident, original = item.get('id'), item.get('text')
        evidence, owner = item.get('evidence_tasks'), item.get('owner_task')
        if (not isinstance(ident, str) or not ident.strip() or ident in seen
                or not isinstance(original, str) or not original.strip()
                or not isinstance(evidence, list) or not evidence
                or any(not isinstance(x, str) or x not in by_id for x in evidence)
                or not isinstance(owner, str) or owner not in by_id):
            raise HarnessError('Requirement needs unique ID, original text, known evidence tasks and owner')
        seen.add(ident)
        missing = sorted(set(evidence) - closure(owner))
        if missing:
            warnings.append({'code': 'owner_precedes_evidence', 'requirement_id': ident,
                             'owner_task': owner, 'missing_dependencies': missing})
        mapping.append({**item, 'text_sha256': hashlib.sha256(original.encode()).hexdigest()})
    if not mapping:
        warnings.append({'code': 'requirements_unmapped'})
    reports = []
    fields = ('deliverables', 'input_refs', 'unknown_prerequisites', 'permission_operations')
    for task in tasks:
        detail = details.get(task['id'], {})
        if not isinstance(detail, dict):
            raise HarnessError('Task readiness details must be objects')
        record = {'task_id': task['id'], 'acceptance': task['acceptance'],
                  'depends_on': task['depends_on'], 'required_commands': task.get('required_commands', [])}
        for field in fields:
            value = detail.get(field, [])
            if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
                raise HarnessError('Readiness ' + field + ' must contain nonempty strings')
            record[field] = value
        commands = detail.get('verification_commands', [])
        validate_commands(commands, 'readiness.verification_commands')
        record['verification_commands'] = commands
        for field in ('cost_basis', 'not_split_reason'):
            value = detail.get(field, '')
            if not isinstance(value, str):
                raise HarnessError('Readiness ' + field + ' must be text')
            record[field] = value
        absent = [f for f in ('deliverables', 'input_refs', 'verification_commands', 'cost_basis') if not record[f]]
        if absent:
            warnings.append({'code': 'task_evidence_incomplete', 'task_id': task['id'], 'missing': absent})
        if record['unknown_prerequisites']:
            warnings.append({'code': 'discovery_needed', 'task_id': task['id']})
        reports.append(record)
    return {'schema_version': 1, 'authority': 'advisory_only', 'goal': goal,
            'goal_sha256': hashlib.sha256(goal.encode()).hexdigest(), 'plan_sha256': digest(plan),
            'coverage_sha256': digest(coverage), 'requirements': mapping, 'tasks': reports,
            'warnings': warnings, 'semantic_coverage': 'requires_independent_review',
            'verification_policy': 'run_global_commands_unchanged'}


def save_readiness(repo, plan, goal, coverage=None, *, run_id=None):
    from .private_state import ensure_private_state, _path
    from .util import uid
    report = build_readiness(plan, goal, coverage)
    ensure_private_state(repo)
    parent = _path(Path(repo) / '.harness' / 'plans', directory=True)
    directory = parent / uid('readiness')
    directory.mkdir(parents=True, exist_ok=False)
    if run_id is not None:
        report['run_id'] = run_id
    path = directory / 'plan-readiness.json'
    write_json(path, report)
    return {'report': str(path), 'authority': report['authority'], 'warnings': report['warnings']}
