"""Durable, task-scoped operator choices, independent of facts and acceptance."""
from copy import deepcopy
import hashlib
import json
from os import PathLike
from pathlib import Path
import time

from .contracts import WORK, REPLAN, envelope, validate_structure, validate_work_semantics
from .recovery import RECOVERY_BUSY_KEYS, read_bound_result, snapshot_digest
from .util import HarnessError, dump, write_json


DECISION_POLICY = (
    "Operator decisions answer only the referenced question for the named task. They are not "
    "environment facts, memory, completion evidence, acceptance or permission grants. Preserve the "
    "original goal, acceptance, scope, tool permissions and all verification/review gates. Read bound "
    "artifacts and verify their SHA256 digests when details are needed. History is audit evidence."
)


def _text(value, label, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\0' in value:
        raise HarnessError(f"operator decision {label} must be nonblank text, at most {limit} characters")


def validate_handoff_question(question):
    """Reject new invalid handoffs before their source task can be accepted."""
    _text(question, 'question')


def decision_question(question, kind):
    """Keep an attributable decision gate even when its question is malformed.

    Valid questions keep their identities. Invalid operator questions and legacy
    accepted handoffs get a bounded notice; the source remains byte-for-byte
    intact and its digest, not the excerpt, binds the operator's answer.
    """
    limit = 80019 if kind == 'worker' else 4000
    try:
        _text(question, 'question', limit)
    except HarnessError:
        if not isinstance(question, str) or kind not in ('worker', 'handoff'):
            raise
        # A non-BMP character expands to twelve ASCII bytes in JSON. Keeping
        # at most 200 source characters leaves room within the 4000-char gate.
        excerpt = json.dumps(question[:200], ensure_ascii=True)
        return (
            "Operator decision required. The original question is blank, contains NUL, "
            f"or exceeds {limit} characters. Read the bound original worker result "
            "before deciding whether this task may continue. This notice does not "
            "approve any operation or change tool permissions. "
            f"Original question excerpt (JSON-escaped, at most 200 characters): {excerpt}"
        )
    return question


def validate_decision(value):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'request_id', 'task_id', 'decision', 'summary'}:
        raise HarnessError('operator decision fields invalid')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise HarnessError('operator decision schema_version must be 1')
    for key in ('request_id', 'task_id', 'summary'):
        _text(value[key], key, 4000 if key == 'summary' else 120)
    if value['decision'] not in ('continue', 'stop'):
        raise HarnessError('operator decision must be continue or stop')
    return deepcopy(value)


def read_decision(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise HarnessError('duplicate operator decision field')
            result[key] = value
        return result
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise HarnessError('operator decision exceeds 64 KiB')
        return validate_decision(json.loads(raw, object_pairs_hook=unique))
    except (OSError, ValueError, UnicodeError) as error:
        raise HarnessError(f'cannot read operator decision JSON: {error}') from error


class DecisionRecovery:
    def _decision_source(self, request):
        source = request['source']
        if not isinstance(source, dict) or set(source) != {'artifact', 'artifact_digest', 'dispatch_id'}:
            raise HarnessError('decision source fields invalid')
        dispatch_id = source.get('dispatch_id')
        entries = [d for d in self.run['dispatches'] if d.get('id') == dispatch_id]
        if len(entries) != 1:
            raise HarnessError('decision source dispatch unknown')
        entry = entries[0]
        if entry.get('invoked', True) is not True:
            raise HarnessError('decision source model was not invoked')
        expected = self.directory / 'dispatches' / dispatch_id / 'returned-result.json'
        if source.get('artifact') != str(expected) or not source.get('artifact_digest'):
            raise HarnessError('decision source reference invalid')
        result, _ = read_bound_result(source, 'decision source ')
        if request['kind'] in ('worker', 'handoff'):
            if entry.get('role') != 'worker' or entry.get('kind') != 'implement' or entry.get('task_id') != request['source_task_id']:
                raise HarnessError('decision worker source identity invalid')
            validate_structure(result, envelope(WORK, request['source_task_id'], dispatch_id))
            validate_work_semantics(result)
            if request['kind'] == 'worker':
                if (request['task_id'] != request['source_task_id'] or result['status'] != 'blocked'
                        or result['blocker_kind'] != 'operator'
                        or request['question'] != decision_question('\n'.join(result['blockers']), 'worker')):
                    raise HarnessError('decision request does not match operator blocker')
            else:
                matches = [r for r in result.get('decision_requests', []) if r.get('id') == request['request_key']]
                if (result['status'] != 'completed' or request['source_task_id'] not in self.run['accepted']
                        or self.run['accepted'][request['source_task_id']].get('worker_source') != source
                        or self.run['accepted'][request['source_task_id']].get('decision_requests') != result.get('decision_requests', [])
                        or len(matches) != 1 or matches[0].get('task_id') != request['task_id']
                        or decision_question(matches[0].get('question'), 'handoff') != request['question']):
                    raise HarnessError('handoff decision is not bound to accepted worker request')
                tasks = {t['id']: t for t in self.run['tasks']}
                seen, pending = set(), list(tasks[request['task_id']]['depends_on'])
                while pending:
                    ident = pending.pop()
                    if ident not in seen:
                        seen.add(ident)
                        pending.extend(tasks[ident]['depends_on'])
                if request['source_task_id'] not in seen:
                    raise HarnessError('handoff target must depend on source task')
        elif request['kind'] == 'replan_stop':
            if entry.get('role') != 'commander' or entry.get('kind') != 'replan' or entry.get('task_id') != request['source_task_id']:
                raise HarnessError('decision replan source identity invalid')
            validate_structure(result, envelope(REPLAN, request['source_task_id'], dispatch_id))
            if result['action'] != 'stop' or result['reason'] != request['question']:
                raise HarnessError('decision source is not commander stop')
        else:
            raise HarnessError('unknown decision request kind')
        return result

    def _decision_identity(self, request):
        return 'request_' + snapshot_digest({k: request[k] for k in
            ('run_id', 'task_id', 'source_task_id', 'kind', 'source', 'question', 'request_key')})

    def _decision_artifact(self, record, category):
        artifact = self.directory / 'decisions' / (record['id'] + '.json')
        if artifact.exists():
            try:
                old = json.loads(artifact.read_bytes())
            except (OSError, ValueError, UnicodeError) as error:
                raise HarnessError('unregistered decision artifact damaged') from error
            if not isinstance(old, dict) or {k: v for k, v in old.items() if k != 'recorded_at'} != {k: v for k, v in record.items() if k != 'recorded_at'}:
                raise HarnessError('immutable decision artifact conflict')
            record = old
        else:
            write_json(artifact, record)
        row = {**deepcopy(record), 'artifact': str(artifact),
               'artifact_digest': hashlib.sha256(artifact.read_bytes()).hexdigest()}
        self.event(category, **{k: v for k, v in row.items() if k != 'run_id'})
        return row

    def register_decision_request(self, task_id, source_task_id, kind, source, question, request_key=''):
        self.decision_context()
        question = decision_question(question, kind)
        _text(question, 'question', 80019 if kind == 'worker' else 4000)
        if not isinstance(request_key, str) or len(request_key) > 96 or '\0' in request_key:
            raise HarnessError('decision request key invalid')
        tasks = {t['id'] for t in self.run['tasks']}
        if task_id not in tasks | {'final'}:
            raise HarnessError('decision target must be an unaccepted task or final')
        record = dict(run_id=self.run['id'], task_id=task_id, source_task_id=source_task_id,
                      kind=kind, source=deepcopy(source), question=question, request_key=request_key)
        self._decision_source(record)
        record['id'] = self._decision_identity(record)
        for old in self.run.get('decision_requests', []):
            if old['id'] == record['id']:
                return deepcopy(old)
        if task_id in self.run['accepted']:
            raise HarnessError('decision target already accepted')
        if kind == 'worker':
            feedback = self.run.get('feedback') or {}
            if (self.run.get('current_task') != task_id or feedback.get('task_id') != task_id
                    or feedback.get('worker_artifact') != source['artifact']
                    or feedback.get('worker_artifact_digest') != source['artifact_digest']):
                raise HarnessError('operator request does not match current worker feedback')
        if kind == 'replan_stop':
            stopped = self.run.get('replan_stopped') or {}
            if (stopped.get('target') != task_id or stopped.get('reason') != question
                    or stopped.get('dispatch_id') != source['dispatch_id']):
                raise HarnessError('operator request does not match stopped replan')
        if kind in ('worker', 'replan_stop'):
            position = next(i for i, d in enumerate(self.run['dispatches']) if d['id'] == source['dispatch_id'])
            if any(d.get('invoked', True) for d in self.run['dispatches'][position + 1:]):
                raise HarnessError('cannot register stale decision source after model invocation')
        if self.run.get('status') in ('completed', 'abandoned'):
            raise HarnessError('terminal run cannot request decisions')
        record.update(sequence=len(self.run.get('decision_requests', [])) + 1,
                      dispatch_count=len(self.run['dispatches']), recorded_at=time.time())
        row = self._decision_artifact(record, 'operator_decision_requested')
        self.run.setdefault('decision_requests', []).append(row)
        self.run.setdefault('operator_decisions', [])
        self.run.setdefault('task_checkpoints', [])
        self.run['schema_version'] = max(self.run['schema_version'], 10)
        self.save()
        return deepcopy(row)

    def decision_context(self):
        try:
            return self._decision_context()
        except (KeyError, TypeError, IndexError, AttributeError) as error:
            raise HarnessError('malformed operator decision ledger; preserve evidence') from error

    def _decision_context(self):
        requests, responses = self.run.get('decision_requests', []), self.run.get('operator_decisions', [])
        if not isinstance(requests, list) or not isinstance(responses, list):
            raise HarnessError('decision ledger must contain arrays')
        if (requests or responses) and self.run['schema_version'] < 10:
            raise HarnessError('decision ledger requires schema 10')
        records, ids = [], set()
        for category, rows in (('request', requests), ('response', responses)):
            for sequence, row in enumerate(rows, 1):
                if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] in ids:
                    raise HarnessError('decision ledger identity invalid')
                ids.add(row['id'])
                if row.get('artifact') != str(self.directory / 'decisions' / (row['id'] + '.json')) or not row.get('artifact_digest'):
                    raise HarnessError('decision artifact reference invalid')
                record, _ = read_bound_result(row, 'operator decision ')
                expected = {k: v for k, v in row.items() if k not in ('artifact', 'artifact_digest')}
                if record != expected or type(record.get('sequence')) is not int or record.get('sequence') != sequence:
                    raise HarnessError('decision artifact disagrees with ledger')
                if category == 'request':
                    if record.get('run_id') != self.run['id'] or record['id'] != self._decision_identity(record):
                        raise HarnessError('decision request identity invalid')
                    if record['task_id'] not in {t['id'] for t in self.run['tasks']} | {'final'}:
                        raise HarnessError('decision target unknown')
                    count = record.get('dispatch_count')
                    if type(count) is not int or not 0 <= count <= len(self.run['dispatches']):
                        raise HarnessError('decision dispatch boundary invalid')
                    if not isinstance(record['request_key'], str) or len(record['request_key']) > 96:
                        raise HarnessError('decision request key invalid')
                    _text(record['question'], 'question', 80019 if record['kind'] == 'worker' else 4000)
                    self._decision_source(record)
                    records.append(deepcopy(row))
                else:
                    value = validate_decision({k: record[k] for k in ('schema_version', 'request_id', 'task_id', 'decision', 'summary')})
                    request = next((r for r in records if r['id'] == value['request_id']), None)
                    if (request is None or request['task_id'] != value['task_id'] or record['id'] != self._response_id(value)
                            or type(record.get('dispatch_count')) is not int
                            or not request['dispatch_count'] <= record['dispatch_count'] <= len(self.run['dispatches'])):
                        raise HarnessError('operator response request linkage invalid')
        latest = {r['request_id']: r for r in responses}
        return [{**r, 'response': deepcopy(latest.get(r['id']))} for r in records]

    def validate_decision_links(self):
        self.decision_context()

    def pending_decision(self, task_id):
        return next((r for r in self.decision_context() if r['task_id'] == task_id
                     and (r['response'] is None or r['response']['decision'] == 'stop')), None)

    def _response_id(self, value):
        return 'decision_' + snapshot_digest({'run_id': self.run['id'], 'response': value})

    def _prepare_decision(self, value):
        records = self.decision_context()
        if self.run.get('status') in ('completed', 'abandoned'):
            raise HarnessError('terminal run rejects operator decisions')
        request = next((r for r in records if r['id'] == value['request_id']), None)
        if request is None or request['task_id'] != value['task_id'] or value['task_id'] in self.run['accepted']:
            raise HarnessError('operator decision request unknown or task accepted')
        active = [key for key in RECOVERY_BUSY_KEYS if self.run.get(key)]
        if any(r.get('status') == 'pending' for r in self.run.get('invalid_repairs', {}).values()):
            active.append('invalid_repairs')
        if active:
            raise HarnessError('pending lifecycle gates reject operator decisions: ' + ', '.join(active))
        stopped = self.run.get('replan_stopped')
        if stopped and (request['kind'] != 'replan_stop' or stopped.get('target') != request['task_id']
                        or stopped.get('reason') != request['question']
                        or stopped.get('dispatch_id') != request['source']['dispatch_id']):
            raise HarnessError('operator decision does not match stopped replan')
        boundary = request['response']['dispatch_count'] if request['response'] else request['dispatch_count']
        if any(d.get('invoked', True) and (request['kind'] != 'handoff'
                   or d.get('task_id') == request['task_id'])
               for d in self.run['dispatches'][boundary:]):
            raise HarnessError('operator decision request stale after subsequent model invocation')
        ident = self._response_id(value)
        if any(r['id'] == ident for r in self.run.get('operator_decisions', [])):
            if request['response'] and request['response']['id'] == ident:
                return None
            raise HarnessError('operator decision was superseded; provide a new explicit summary')
        return {**value, 'id': ident, 'sequence': len(self.run.get('operator_decisions', [])) + 1,
                'dispatch_count': len(self.run['dispatches']), 'recorded_at': time.time()}

    def prepare_operator_decision(self, path):
        return self._prepare_decision(read_decision(path))

    def prepare_operator_decisions(self, paths):
        """Validate an entire submission before creating any decision artifacts."""
        if isinstance(paths, (str, PathLike)):
            paths = [paths]
        values = [read_decision(path) for path in paths]
        if not values:
            raise HarnessError('operator decision batch must not be empty')
        if len({value['request_id'] for value in values}) != len(values):
            raise HarnessError('duplicate operator decision request in batch')
        records = []
        for value in values:
            record = self._prepare_decision(value)
            if record is not None:
                record['sequence'] += len(records)
                records.append(record)
        return values, records

    def record_operator_decision(self, record):
        self.record_operator_decisions([] if record is None else [record])

    def record_operator_decisions(self, records):
        """Prewrite immutable evidence, then publish all answers in one ledger save.

        Orphan artifacts from an interrupted prewrite are harmless and are reused
        on retry. No model may run before this method has returned successfully.
        """
        prepared_records = []
        seen = set()
        for record in records:
            value = validate_decision({k: record[k] for k in
                ('schema_version', 'request_id', 'task_id', 'decision', 'summary')})
            if value['request_id'] in seen:
                raise HarnessError('duplicate operator decision request in batch')
            seen.add(value['request_id'])
            prepared = self._prepare_decision(value)
            if prepared is None:
                continue
            prepared['sequence'] += len(prepared_records)
            if any(record.get(k) != prepared[k] for k in prepared if k != 'recorded_at'):
                raise HarnessError('prepared operator decision changed')
            prepared_records.append(record)
        if not prepared_records:
            return
        rows = [self._decision_artifact(record, 'operator_decision_recorded')
                for record in prepared_records]
        old = self.run.get('operator_decisions', [])
        self.run['operator_decisions'] = [*old, *rows]
        try:
            self.save()
        except BaseException:
            self.run['operator_decisions'] = old
            raise

    def _decision_index(self, records):
        # Navigation only: validation of every ledger/source precedes projection.
        # Content addressing preserves references from earlier dispatches.
        value = {'version': 1, 'requests': records,
                 'responses': self.run.get('operator_decisions', [])}
        raw = (dump(value) + '\n').encode('utf-8')
        digest = hashlib.sha256(raw).hexdigest()
        folder = self.directory / 'decisions' / 'indexes'
        if any(p.is_symlink() for p in (folder, *folder.parents)):
            raise HarnessError('decision index directory is a symlink')
        artifact = folder / (digest + '.json')
        try:
            if not artifact.exists() and not artifact.is_symlink():
                write_json(artifact, value)
            if artifact.is_symlink() or artifact.read_bytes() != raw:
                raise HarnessError('decision index changed')
        except OSError as error:
            raise HarnessError('cannot read or write decision index') from error
        return {'artifact': str(artifact), 'artifact_digest': digest, 'count': len(records)}

    def decision_packet(self, packet, *, task_id=None, role=None, kind=None):
        records = self.decision_context()
        if not records:
            return packet
        task = packet.get('task', {})
        task_id = task_id or packet.get('task_id') or (task.get('id') if isinstance(task, dict) else None)
        relevant = None if kind in ('replan', 'final_review') or task_id == 'final' else {task_id}
        if kind == 'dispatch':
            relevant = {t['id'] for t in packet.get('ready', [])}
        selected = [r for r in records if relevant is None or r['task_id'] in relevant]
        # Share an excerpt allowance instead of multiplying full questions and
        # 4000-character answers by the number of decisions. Choices and IDs
        # remain explicit; all originals and superseded answers are retrievable.
        limit = min(500, 4000 // max(1, 2 * len(selected)))
        decisions = []
        for request in selected:
            projected = {k: deepcopy(request[k]) for k in
                         ('id', 'task_id', 'source_task_id', 'kind', 'source')}
            projected.update(question=request['question'][:limit],
                             question_truncated=len(request['question']) > limit,
                             response=None)
            if request['response'] is not None:
                response = request['response']
                projected['response'] = {k: response[k] for k in ('id', 'decision')}
                projected['response'].update(summary=response['summary'][:limit],
                                             summary_truncated=len(response['summary']) > limit)
            decisions.append(projected)
        return {**packet, 'operator_decisions': decisions,
                'operator_decision_index': self._decision_index(records),
                'operator_decision_policy': DECISION_POLICY +
                ' Question and summary excerpts share a bounded allowance. Before acting on a truncated '
                'question or answer, read the SHA256-bound operator_decision_index: requests are keyed '
                'by id, responses by request_id; it retains full text, latest answers, all answer history '
                'and original artifact/digest references. A null response means unanswered.'}
