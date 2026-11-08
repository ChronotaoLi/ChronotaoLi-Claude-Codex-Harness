"""Durable worker handoff: immutable returned candidates, replayable local gates."""
from copy import deepcopy
import hashlib
from pathlib import Path

from . import git
from .contracts import RepairableFormatError, validate, validate_structure, validate_work_semantics
from .recovery import snapshot_digest
from .services import RuntimeService
from .util import HarnessError, read_json, write_json


class WorkerReceipts(RuntimeService):
    def failed_without_result(self, job, provider):
        """Only absence or a complete explicit provider error permits fresh retry.

        A captured success, unreadable terminal or ambiguous terminal remains an
        unsealed return, even when a backend subsequently raises an exception.
        """
        from .stream_capture import claude_result_path
        for name in ('returned-result.json', 'response.json', 'terminal-capture.json', 'stream-incomplete-terminal.json'):
            path = job / name
            if path.exists() or path.is_symlink():
                return False
        if (job / 'capture-manifest.json').is_symlink():
            return False
        if provider != 'claude':
            return True
        try:
            source = claude_result_path(job)
            if source.is_symlink():
                return False
            if not source.exists():
                return True
            if not source.is_file():
                return False
            # Legacy stdout can exist without a terminal (ordinary progress or
            # a CLI error). Distinguish that from a complete result envelope.
            raw = source.read_bytes()
            if not raw.strip():
                return True
            import json
            value = json.loads(raw)
        except (OSError, ValueError, HarnessError):
            return False
        return (isinstance(value, dict) and value.get('type') == 'result'
                and (value.get('is_error') is True or
                     isinstance(value.get('subtype'), str) and value['subtype'].startswith('error_')))

    def intent(self, entry, schema, packet):
        key = f"worker:{entry['kind']}:{entry['task_id']}"
        if key in self.context.run['worker_receipts']:
            raise HarnessError('worker receipt already active')
        job = self.context.directory / 'dispatches' / entry['id']
        receipt = {'dispatch_id': entry['id'], 'artifact': str(job / 'returned-result.json'),
                   'seal': str(job / 'worker-seal.json'), 'schema': deepcopy(schema),
                   'packet': deepcopy(packet), 'packet_digest': snapshot_digest(packet),
                   'snapshot_digest': entry['snapshot_digest'],
                   'attempt': self.context.run['attempts'][entry['task_id']], 'phase': 'invoking'}
        entry['worker_receipt'] = deepcopy(receipt)
        self.context.run['worker_receipts'][key] = receipt

    def seal(self, entry, result):
        self.context.ensure_exited()
        outcome = entry.get('process_outcome')
        if outcome and (outcome.get('exit_code') != 0 or not outcome.get('evidence_complete') or not outcome.get('exit_confirmed') or outcome.get('termination_reason') != 'normal'):
            raise HarnessError('worker process did not return complete normal exit evidence')
        key = f"worker:{entry['kind']}:{entry['task_id']}"
        receipt = self.context.run['worker_receipts'][key]
        job = Path(receipt['artifact']).parent
        sources = {}
        names = ['response.json', 'process.stdout', 'process.stderr', 'provider-result.json', 'result.json', 'stream-terminal.json', 'terminal-capture.json', 'process.outcome.json', 'capture-manifest.json', 'stream-capture.json']
        command_dir = job / 'commands'
        if command_dir.exists():
            if command_dir.is_symlink():
                raise HarnessError('unsafe worker command evidence')
            names.extend(str(path.relative_to(job)) for path in command_dir.rglob('*') if not path.is_dir())
        for name in names:
            path = job / name
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_file():
                    raise HarnessError('unsafe worker source evidence')
                sources[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        record = {'dispatch_id': entry['id'], 'task_id': entry['task_id'],
                  'intent': deepcopy(entry['worker_receipt']), 'result': deepcopy(result),
                  'result_digest': snapshot_digest(result), 'sources': sources,
                  'candidate_digest': snapshot_digest(self.context.snapshot()),
                  'head': git.head(self.context.worktree, binding=self.context.run['worktree_binding']),
                  'git_binding': deepcopy(self.context.run['worktree_binding']),
                  'process': {field: deepcopy(entry.get(field)) for field in
                              ('exit_confirmed', 'process_state', 'process_outcome', 'pgid')}}
        path = Path(receipt['seal']).with_name('worker-seal.' + snapshot_digest(record) + '.json')
        if self.seal_path(receipt) is not None:
            raise HarnessError('worker seal already exists; refusing replacement')
        write_json(path, record)
        receipt['seal_digest'] = hashlib.sha256(path.read_bytes()).hexdigest()
        receipt['phase'] = 'sealed'
        self.context.save()

    def seal_path(self, receipt):
        paths = list(Path(receipt['seal']).parent.glob('worker-seal.*.json'))
        if len(paths) > 1:
            raise HarnessError('conflicting immutable worker seals')
        return paths[0] if paths else None

    def entry(self, receipt):
        matches = [e for e in self.context.run['dispatches'] if e['id'] == receipt.get('dispatch_id')]
        if len(matches) != 1:
            raise HarnessError('worker receipt dispatch identity missing or ambiguous')
        return matches[0]

    def validate_links(self):
        receipts = self.context.run['worker_receipts']
        if not isinstance(receipts, dict) or len(receipts) > 1:
            raise HarnessError('invalid/conflicting worker receipts')
        if receipts and any(self.context.run.get(field) for field in ('pending_task', 'pending_acceptance', 'pending_post_accept', 'pending_continuation', 'pending_replan', 'review_receipts', 'commander_receipts')):
            raise HarnessError('active worker receipt conflicts with another handoff')
        for entry in self.context.run['dispatches']:
            if not isinstance(entry, dict):
                raise HarnessError('invalid worker dispatch entry')
            if entry.get('role') != 'worker' or not entry.get('invoked'):
                continue
            intent = entry.get('worker_receipt')
            if (not isinstance(intent, dict) or intent.get('dispatch_id') != entry['id']
                    or not isinstance(intent.get('schema'), dict)
                    or not isinstance(intent.get('packet'), dict)
                    or type(intent.get('attempt')) is not int or intent['attempt'] < 1
                    or intent.get('phase') != 'invoking'
                    or intent.get('packet_digest') != snapshot_digest(intent['packet'])):
                raise HarnessError('missing mandatory worker receipt linkage')
            job = self.context.directory / 'dispatches' / entry['id']
            if intent.get('artifact') != str(job / 'returned-result.json') or intent.get('seal') != str(job / 'worker-seal.json'):
                raise HarnessError('worker intent paths invalid')
            properties = intent['schema'].get('properties', {})
            if (not isinstance(properties, dict)
                    or properties.get('task_id') != {'type': 'string', 'enum': [entry['task_id']]}
                    or properties.get('dispatch_id') != {'type': 'string', 'enum': [entry['id']]}):
                raise HarnessError('worker delivered contract identity invalid')
            key = f"worker:{entry['kind']}:{entry['task_id']}"
            active = receipts.get(key)
            if not entry.get('worker_receipt_consumed') and (not active or active.get('dispatch_id') != entry['id']):
                raise HarnessError('missing active worker receipt')
        for key, receipt in receipts.items():
            if not isinstance(key, str) or not isinstance(receipt, dict):
                raise HarnessError('invalid worker receipt')
            entry = self.entry(receipt)
            if entry.get('role') != 'worker' or entry.get('invoked') is not True:
                raise HarnessError('worker receipt references non-invoked worker')
            job = self.context.directory / 'dispatches' / entry['id']
            if (any(receipt.get(field) != entry['worker_receipt'].get(field) for field in entry['worker_receipt'] if field != 'phase')
                    or key != f"worker:{entry['kind']}:{entry['task_id']}" or
                    receipt.get('artifact') != str(job / 'returned-result.json') or
                    receipt.get('seal') != str(job / 'worker-seal.json') or
                    entry.get('worker_receipt_consumed')):
                raise HarnessError('invalid worker receipt identity')

    def pending(self):
        self.validate_links()
        if not self.context.run['worker_receipts']:
            return None
        key, receipt = next(iter(self.context.run['worker_receipts'].items()))
        entry = self.entry(receipt)
        if self.seal_path(receipt) is None:
            if (entry.get('worker_no_result') is True and not Path(receipt['artifact']).exists()
                    and self.failed_without_result(Path(receipt['artifact']).parent, self.context.config.role('worker', self.context.commander)['cli'])):
                self.consume(key, 'provider_failure')
                self.context.save()
                return None
            raise HarnessError('worker return interrupted before immutable seal; preserve evidence; automatic retry forbidden')
        return key

    def finish(self, key, task):
        self.context.ensure_exited()
        receipt = self.context.run['worker_receipts'][key]
        entry = self.entry(receipt)
        path = self.seal_path(receipt)
        if Path(receipt['artifact']).parent.is_symlink():
            raise HarnessError('unsafe worker dispatch evidence directory')
        if path is None or path.is_symlink() or not path.is_file():
            raise HarnessError('worker seal missing or unsafe')
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if receipt.get('seal_digest', digest) != digest:
            raise HarnessError('worker seal changed')
        record = read_json(path)
        if (not isinstance(record, dict) or not isinstance(record.get('sources'), dict)
                or not isinstance(record.get('process'), dict) or not isinstance(record.get('result'), dict)
                or not isinstance(record.get('candidate_digest'), str)):
            raise HarnessError('invalid worker seal structure')
        if path.name != 'worker-seal.' + snapshot_digest(record) + '.json':
            raise HarnessError('immutable worker seal content digest changed')
        if (record.get('intent') != entry['worker_receipt'] or record.get('dispatch_id') != entry['id']
                or record.get('task_id') != task['id'] or record.get('git_binding') != self.context.run['worktree_binding']
                or record.get('head') != self.context.run['expected_head']
                or record.get('process', {}).get('exit_confirmed') is not True
                or record.get('result_digest') != snapshot_digest(record.get('result'))):
            raise HarnessError('worker seal identity/process binding invalid')
        for field in ('exit_confirmed', 'process_state', 'process_outcome', 'pgid'):
            if record['process'].get(field) != entry.get(field):
                raise HarnessError('worker original process facts changed')
        outcome = record['process'].get('process_outcome')
        if outcome and (outcome.get('termination_reason') != 'normal' or outcome.get('exit_code') != 0 or not outcome.get('evidence_complete') or not outcome.get('exit_confirmed')):
            raise HarnessError('worker original outcome is not normal complete success')
        for name, expected in record['sources'].items():
            if (not isinstance(name, str) or Path(name).is_absolute() or any(part in ('..', '.') for part in Path(name).parts)
                    or not isinstance(expected, str)):
                raise HarnessError('unsafe worker source reference')
            source = path.parent / name
            if any(parent.is_symlink() for parent in [source, *source.parents] if parent != path.parent and path.parent in parent.parents):
                raise HarnessError('unsafe worker source evidence path')
            if source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                raise HarnessError('worker source evidence changed')
        current = snapshot_digest(self.context.snapshot())
        expected = receipt.get('post_hook_digest', record['candidate_digest'])
        if current != expected:
            raise HarnessError('worker candidate changed since sealed return or interrupted hook')
        receipt.update(seal_digest=digest, phase='sealed')
        result = record['result']
        artifact = Path(receipt['artifact'])
        if artifact.is_symlink():
            raise HarnessError('unsafe worker returned artifact')
        if artifact.exists():
            if (artifact.is_symlink() or read_json(artifact) != result or
                    receipt.get('artifact_digest', hashlib.sha256(artifact.read_bytes()).hexdigest()) != hashlib.sha256(artifact.read_bytes()).hexdigest()):
                raise HarnessError('worker returned result changed')
        else:
            if 'artifact_digest' in receipt:
                raise HarnessError('bound worker returned artifact missing')
            write_json(artifact, result)
        receipt['artifact_digest'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.context.save()
        if git.head(self.context.worktree, binding=self.context.run['worktree_binding']) != record['head']:
            raise HarnessError('worker HEAD changed')
        try:
            git.check_scope(self.context.worktree, task['allowed_paths'], binding=self.context.run['worktree_binding'])
        except git.ScopeViolation as error:
            self.reject(key, error, 'scope_rejected')
            raise
        contract = receipt['schema']
        try:
            validate_structure(result, contract)
            validate_work_semantics(result)
        except RepairableFormatError:
            raise
        except HarnessError as error:
            if entry.get('format_correction') is True:
                self.context.run['pending_format_corrections'].pop(key, None)
                self.reject(key, error, 'correction_rejected')
            raise
        entry['worker_outcome'] = ('completed' if result['status'] == 'completed' else result['blocker_kind'] + '_blocked')
        validate(result, contract)
        if 'post_hook_digest' not in receipt:
            receipt['hook_attempts'] = receipt.get('hook_attempts', 0) + 1
            self.context.save()
            self.context.emit_hook('post_agent', {**entry, 'result': result}, path.parent / f"post-agent-{receipt['hook_attempts']}")
            if git.head(self.context.worktree, binding=self.context.run['worktree_binding']) != record['head']:
                raise HarnessError('worker hook changed HEAD')
            git.check_scope(self.context.worktree, task['allowed_paths'], binding=self.context.run['worktree_binding'])
            receipt['post_hook_digest'] = snapshot_digest(self.context.snapshot())
            self.context.save()
        entry['status'] = 'completed'
        return {k: v for k, v in result.items() if k not in ('task_id', 'dispatch_id')}

    def reject(self, key, error, reason):
        """Retire a deterministically rejected result without accepting it."""
        receipt = self.context.run['worker_receipts'][key]
        entry = self.entry(receipt)
        entry.update(status='failed', error=str(error))
        job = Path(receipt['artifact']).parent
        self.context.run['last_runtime_error'] = {'role': 'worker', 'kind': entry['kind'],
            'task_id': entry['task_id'], 'error': str(error), 'artifact': str(job)}
        self.services.workflow.record_worker_recovery(entry['task_id'], entry['id'], job, error)
        self.consume(key, reason)
        self.context.save()

    def consume(self, key, reason):
        receipt = self.context.run['worker_receipts'].pop(key)
        self.entry(receipt)['worker_receipt_consumed'] = reason
        task_id = self.entry(receipt)['task_id']
        recovery = self.context.run.get('worker_recovery', {}).get(task_id)
        rejection = reason in ('provider_failure', 'handoff_rejected', 'scope_rejected',
                               'format_exhausted', 'correction_rejected', 'invalid_blocker')
        if not rejection and recovery and recovery.get('dispatch_id') == receipt['dispatch_id']:
            self.context.run['worker_recovery'].pop(task_id)


def validate_worker_receipt_links(run, directory):
    """Read-only structural check for execution and guarded historical cleanup."""
    from types import SimpleNamespace
    if not isinstance(run.get('worker_receipts'), dict):
        raise HarnessError('missing mandatory worker receipts')
    WorkerReceipts(SimpleNamespace(run=run, directory=Path(directory))).validate_links()
