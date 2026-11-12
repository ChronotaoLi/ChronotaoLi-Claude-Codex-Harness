"""Durable worker handoff: immutable returned candidates, replayable local gates."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat

from . import git
from .contracts import judge_work
from .recovery import snapshot_digest
from .services import RuntimeService
from .util import HarnessError, dump, read_json, write_json
from .worker_returns import inspect_return, validate_historical_return_evidence, validate_return_evidence

# Worker inputs live beside the dispatch as immutable files; the ledger keeps
# their paths and SHA256 (schema 16 and later). Schema 14 and 15 ledgers embed
# the same inputs; only read-only history checks accept that shape.
INPUTS = {'packet': 'packet.json', 'schema': 'schema.json'}
REFERENCED, EMBEDDED = 'referenced', 'embedded'
# Schema 15 and 16 intents declared the Claude capture mode, which had to match
# the run configuration; their history is checked in that shape. Schema 17 has
# a single output mode and its intent carries no capture contract.
CAPTURE_CONTRACT_VERSIONS = (15, 16)


def receipt_shape(run):
    """The worker intent shape fixed by the ledger's version; never guessed from content."""
    version = run.get('schema_version')
    if type(version) is not int or version < 14:
        raise HarnessError('worker receipts require ledger schema 14 or later')
    return EMBEDDED if version < 16 else REFERENCED


def read_worker_input(directory, entry, intent, field):
    """Read one referenced input after checking its path, file type and SHA256."""
    job = Path(directory) / 'dispatches' / entry['id']
    path = job / INPUTS[field]
    digest = intent.get(field + '_artifact_digest')
    if intent.get(field + '_artifact') != str(path) or not isinstance(digest, str):
        raise HarnessError(f'worker {field} reference invalid; preserve evidence')
    if any(item.is_symlink() for item in (job, job.parent)):
        raise HarnessError(f'worker {field} directory is a symlink; preserve evidence')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise HarnessError(f'worker {field} artifact missing or a symlink; preserve evidence: {path}') from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HarnessError(f'worker {field} artifact must be a regular file; preserve evidence: {path}')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            raw = stream.read()
    except OSError as error:
        raise HarnessError(f'cannot read worker {field} artifact; preserve evidence: {path}') from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise HarnessError(f'worker {field} artifact changed; preserve evidence: {path}')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise HarnessError(f'worker {field} artifact is not valid JSON; preserve evidence') from error
    if not isinstance(value, dict):
        raise HarnessError(f'worker {field} artifact must be an object; preserve evidence')
    return value


class WorkerReceipts(RuntimeService):
    def publish_inputs(self, entry, schema, packet):
        """Write and verify the immutable worker inputs before any accounting.

        The caller mutates budgets, the receipt intent and invoked only after this
        returns, then saves them together. A failed write therefore charges nothing;
        an unreferenced file left by an interruption is ignored by later dispatches.
        """
        key = f"worker:{entry['kind']}:{entry['task_id']}"
        if key in self.context.run['worker_receipts']:
            raise HarnessError('worker receipt already active')
        job = self.context.directory / 'dispatches' / entry['id']
        inputs = {}
        for field, value in (('packet', packet), ('schema', schema)):
            path = job / INPUTS[field]
            raw = (dump(value) + '\n').encode('utf-8')
            try:
                if any(item.is_symlink() for item in (path, job, job.parent)):
                    raise HarnessError(f'worker {field} path is a symlink')
                if not path.exists():
                    write_json(path, value)
                if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
                    raise HarnessError(f'worker {field} artifact conflict; preserve evidence')
            except OSError as error:
                raise HarnessError(f'cannot publish worker {field} before invocation; no budget was charged: {error}') from error
            inputs[field + '_artifact'] = str(path)
            inputs[field + '_artifact_digest'] = hashlib.sha256(raw).hexdigest()
        return inputs

    def intent(self, entry, inputs):
        """Stage the receipt intent; the dispatcher saves it with budgets and invoked."""
        key = f"worker:{entry['kind']}:{entry['task_id']}"
        job = self.context.directory / 'dispatches' / entry['id']
        receipt = {'dispatch_id': entry['id'], 'artifact': str(job / 'returned-result.json'),
                   'seal': str(job / 'worker-seal.json'), **deepcopy(inputs),
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
        names = ['response.json', 'process.stdout', 'process.stderr', 'provider-result.json', 'process.outcome.json']
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

    def delivered_contract(self, entry, intent, shape=REFERENCED):
        """Return the exact contract delivered to a worker, verifying its inputs.

        The referenced shape reads both immutable files and checks their SHA256;
        the embedded shape exists only in completed historical ledgers.
        """
        if shape == EMBEDDED:
            if (any(field + '_artifact' in intent for field in INPUTS)
                    or not isinstance(intent.get('schema'), dict) or not isinstance(intent.get('packet'), dict)
                    or intent.get('packet_digest') != snapshot_digest(intent['packet'])):
                raise HarnessError('missing mandatory worker receipt linkage')
            return intent['schema']
        if shape != REFERENCED or any(field in intent for field in ('packet', 'schema', 'packet_digest')):
            raise HarnessError('missing mandatory worker receipt linkage')
        read_worker_input(self.context.directory, entry, intent, 'packet')
        return read_worker_input(self.context.directory, entry, intent, 'schema')

    def validate_links(self, shape=REFERENCED):
        """Execution accepts only referenced inputs; history passes its own shape."""
        receipts = self.context.run['worker_receipts']
        if not isinstance(receipts, dict) or len(receipts) > 1:
            raise HarnessError('invalid/conflicting worker receipts')
        if receipts and any(self.context.run.get(field) for field in ('pending_task', 'pending_acceptance', 'pending_post_accept', 'pending_continuation', 'pending_revision', 'review_receipts', 'commander_receipts')):
            raise HarnessError('active worker receipt conflicts with another handoff')
        for entry in self.context.run['dispatches']:
            if not isinstance(entry, dict):
                raise HarnessError('invalid worker dispatch entry')
            if entry.get('role') != 'worker' or not entry.get('invoked'):
                continue
            intent = entry.get('worker_receipt')
            if (not isinstance(intent, dict) or intent.get('dispatch_id') != entry['id']
                    or type(intent.get('attempt')) is not int or intent['attempt'] < 1
                    or intent.get('phase') != 'invoking'):
                raise HarnessError('missing mandatory worker receipt linkage')
            contract = self.delivered_contract(entry, intent, shape)
            job = self.context.directory / 'dispatches' / entry['id']
            version = self.context.run['schema_version']
            if version >= CAPTURE_CONTRACT_VERSIONS[0]:
                evidence = entry.get('worker_return_evidence')
                if version in CAPTURE_CONTRACT_VERSIONS:
                    capture = intent.get('capture')
                    if (not isinstance(capture, dict) or set(capture) != {'provider', 'mode'}
                            or capture['provider'] != 'claude'
                            or capture['mode'] not in ('legacy_json', 'stream_metadata_v1')
                            or 'worker_no_result' in entry):
                        raise HarnessError('missing/invalid mandatory worker capture contract')
                    snapshot = self.context.run.get('config_snapshot', {})
                    if (capture['provider'] != snapshot.get('roles', {}).get('worker', {}).get('cli')
                            or capture['mode'] != snapshot.get('runtime', {}).get('claude_output_mode')):
                        raise HarnessError('worker capture contract differs from run configuration')
                    if evidence is not None:
                        validate_historical_return_evidence(job, evidence, capture)
                else:
                    # Schema 17 has no capture contract; a capture here is not
                    # ignored or defaulted, it is refused like any foreign shape.
                    if 'capture' in intent:
                        raise HarnessError('worker receipt carries a capture contract removed in schema 17')
                    if 'worker_no_result' in entry:
                        raise HarnessError('worker dispatch carries a worker_no_result record from an older protocol')
                    if evidence is not None:
                        validate_return_evidence(job, evidence)
                if entry.get('worker_receipt_consumed') == 'provider_failure' and (
                        evidence is None or evidence['state'] != 'retryable'):
                    raise HarnessError('worker provider failure lacks retry evidence')
                if evidence is not None and evidence['state'] == 'retryable' and entry.get('worker_receipt_consumed') not in (None, 'provider_failure'):
                    raise HarnessError('worker retry evidence has another outcome')
                if evidence is not None and evidence['state'] == 'invalid' and (
                        entry.get('worker_receipt_consumed') != 'result_rejected' or entry.get('status') != 'invalid'):
                    raise HarnessError('worker invalid-output evidence lacks its rejection')
            if intent.get('artifact') != str(job / 'returned-result.json') or intent.get('seal') != str(job / 'worker-seal.json'):
                raise HarnessError('worker intent paths invalid')
            properties = contract.get('properties', {})
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
        try:
            return self._pending()
        except HarnessError as error:
            receipts = self.context.run['worker_receipts']
            if isinstance(receipts, dict) and len(receipts) == 1:
                receipt = next(iter(receipts.values()))
                self.services.receipts.record_receipt_error(receipt, error)
            raise

    def _pending(self):
        self.context.ensure_exited()
        self.validate_links()
        if not self.context.run['worker_receipts']:
            return None
        key, receipt = next(iter(self.context.run['worker_receipts'].items()))
        entry = self.entry(receipt)
        if self.seal_path(receipt) is None:
            job = Path(receipt['artifact']).parent
            # Classify first; the entry changes only when the verdict is published.
            evidence = entry.get('worker_return_evidence')
            if evidence is None:
                evidence = inspect_return(job)
            else:
                validate_return_evidence(job, evidence)
            if evidence['state'] == 'retryable':
                error = HarnessError(entry.get('error') or 'Worker invocation requires a fresh attempt: ' + evidence['reason'])
                # Classification, failure provenance and receipt retirement share
                # one commit, including when a crash skipped dispatch.finally.
                self.reject(key, error, 'provider_failure', evidence=evidence)
                return None
            if evidence['state'] == 'invalid':
                # A complete output that is not a decodable object result is an
                # invalid delivery, whether or not the controller crashed first.
                self.reject(key, HarnessError('worker output invalid: ' + evidence['reason']), 'result_rejected',
                            status='invalid', evidence=evidence, invalid_output={
                                'artifact': str(job / 'process.stdout'),
                                'artifact_digest': evidence['sources']['process.stdout']})
                return None
            # The stop reason is saved with this evidence (pending's error handler).
            entry['worker_return_evidence'] = evidence
            raise HarnessError('worker return interrupted before immutable seal; ' + evidence['reason'] +
                               '; preserve evidence; automatic retry forbidden')
        return key

    def finish(self, key, task):
        try:
            return self._finish(key, task)
        except HarnessError as error:
            self.services.receipts.record_receipt_error(self.context.run['worker_receipts'].get(key), error)
            raise

    def _finish(self, key, task):
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
        # The delivered contract is read back from its immutable file and digest.
        judgment = judge_work(result, self.delivered_contract(entry, entry['worker_receipt']))
        if judgment.state == 'veto':
            # A blocked report stays binding; no post_agent, no exemption.
            return self.services.tasks.publish_invalid_blocker(task, key, result, judgment)
        if judgment.state == 'invalid':
            self.reject(key, judgment.error, 'result_rejected', status='invalid',
                        invalid_output={'artifact': receipt['artifact'], 'artifact_digest': receipt['artifact_digest']})
            return None
        worker_outcome = 'completed' if result['status'] == 'completed' else result['blocker_kind'] + '_blocked'
        if 'post_hook_digest' not in receipt:
            receipt['hook_attempts'] = receipt.get('hook_attempts', 0) + 1
            self.context.save()
            self.context.emit_hook('post_agent', {**entry, 'worker_outcome': worker_outcome, 'result': result},
                                   path.parent / f"post-agent-{receipt['hook_attempts']}")
            if git.head(self.context.worktree, binding=self.context.run['worktree_binding']) != record['head']:
                raise HarnessError('worker hook changed HEAD')
            git.check_scope(self.context.worktree, task['allowed_paths'], binding=self.context.run['worktree_binding'])
            receipt['post_hook_digest'] = snapshot_digest(self.context.snapshot())
            self.context.save()
        # The entry records its outcome only after the last interruptible step.
        entry.update(worker_outcome=worker_outcome, status='completed')
        return {k: v for k, v in result.items() if k not in ('task_id', 'dispatch_id')}

    def reject(self, key, error, reason, *, status='failed', invalid_output=None, evidence=None):
        """Retire a deterministically rejected result without accepting it.

        An invalid output (status ``invalid``) is one spent work attempt: the
        receipt is consumed and scheduling continues within max_attempts.
        Every interruptible step (the Git inspection of the recovery record)
        comes first; the verdict, its return evidence, the recovery record and
        the consumption are then published together in one save.
        """
        receipt = self.context.run['worker_receipts'][key]
        entry = self.entry(receipt)
        job = Path(receipt['artifact']).parent
        previous = self.context.run.get('worker_recovery', {}).get(entry['task_id'])
        recovery = None
        if reason != 'provider_failure' or not previous or previous.get('dispatch_id') != entry['id']:
            # A later classification must not replace an observed timeout or
            # cancellation with the generic recovery error used to retire it.
            recovery = self.services.workflow.worker_recovery(entry['task_id'], entry['id'], job, error)
        if evidence is not None:
            entry['worker_return_evidence'] = evidence
        entry.update(status=status, error=str(error))
        if invalid_output is not None:
            entry['invalid_output'] = invalid_output
        self.context.run['last_runtime_error'] = {'role': 'worker', 'kind': entry['kind'],
            'task_id': entry['task_id'], 'error': str(error), 'artifact': str(job)}
        if recovery is not None:
            self.context.run.setdefault('worker_recovery', {})[entry['task_id']] = recovery
        self.consume(key, reason)
        self.context.save()
        if status == 'invalid':
            self.services.receipts.observe_invalid(entry)

    def consume(self, key, reason):
        receipt = self.context.run['worker_receipts'].pop(key)
        self.entry(receipt)['worker_receipt_consumed'] = reason
        task_id = self.entry(receipt)['task_id']
        recovery = self.context.run.get('worker_recovery', {}).get(task_id)
        rejection = reason in ('provider_failure', 'scope_rejected', 'result_rejected', 'invalid_blocker')
        if not rejection and recovery and recovery.get('dispatch_id') == receipt['dispatch_id']:
            self.context.run['worker_recovery'].pop(task_id)


def validate_worker_receipt_links(run, directory, shape=None):
    """Read-only structural check for input preparation and guarded historical cleanup.

    Without an explicit shape the ledger's version selects it: schema 14 and 15
    use their embedded inputs, schema 16 and later the referenced files. Old ledgers are
    checked in their own shape; no default is filled in.
    """
    from types import SimpleNamespace
    if not isinstance(run.get('worker_receipts'), dict):
        raise HarnessError('missing mandatory worker receipts')
    WorkerReceipts(SimpleNamespace(run=run, directory=Path(directory))).validate_links(
        receipt_shape(run) if shape is None else shape)


def delivered_work_contract(run, directory, dispatch_id):
    """The exact contract an invoked worker dispatch received, read back by digest.

    Later checks of a sealed worker result (exemptions, continuations, environment
    and decision sources, checkpoints) validate against this, never the static
    WORK: a worker with required findings received runtime-keyed repair_claims.
    """
    entries = [e for e in run.get('dispatches', []) if isinstance(e, dict) and e.get('id') == dispatch_id]
    if (len(entries) != 1 or entries[0].get('role') != 'worker' or entries[0].get('invoked') is not True
            or not isinstance(entries[0].get('worker_receipt'), dict)):
        raise HarnessError('worker dispatch lacks its delivered contract; preserve evidence')
    return read_worker_input(directory, entries[0], entries[0]['worker_receipt'], 'schema')
