"""Offline rejection recovery, immutable evidence and normal acceptance gates."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harness import continuation
from harness.cli import status_summary
from harness.recovery import snapshot_digest
from harness.util import HarnessError, read_json, write_json
from test_harness import RepoCase, ROOT, add_worker_dispatch
from test_runtime_changes import ScriptedBackend


class RejectionEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.candidate = {'head': 'head', 'index': 'index', 'files': {'code': 'candidate'}}
        self.saved = []
        self.context = SimpleNamespace(directory=self.root / 'run',
            run={'id': 'run', 'tasks': [{'id': 'task'}], 'accepted': {}, 'dispatches': [],
                 'current_task': 'task', 'status': 'blocked', 'attempts': {'task': 1},
                 'agent_dispatches': 1, 'rounds': 3},
            config={'runtime': {'agent_timeout_seconds': 30}, 'permissions': {'worker_commands': []},
                    'verification': {'commands': [['python3', '-m', 'unittest']]}},
            snapshot=lambda: deepcopy(self.candidate), save=lambda: self.saved.append(deepcopy(self.context.run)))
        self.result = dict(task_id='task', dispatch_id='worker1', status='blocked', blocker_kind='continuation',
            summary='remaining edits', evidence=[], blockers=['need exact test'], proposed_memories=[],
            continuation_request={'kind': 'commands', 'commands': [['python3', 'focused.py']],
                                  'minimum_timeout_seconds': 0, 'reason': 'focused regression'})
        source_path = self.context.directory / 'dispatches/worker1/returned-result.json'
        write_json(source_path, self.result)
        self.source = {'dispatch_id': 'worker1', 'artifact': str(source_path),
                       'artifact_digest': hashlib.sha256(source_path.read_bytes()).hexdigest()}
        add_worker_dispatch(self.context.run, self.context.directory, 'worker1', 'task')
        self.row = continuation.record_continuation(self.context, 'task', self.result, self.source, self.candidate)
        self.value = {'continuation_id': self.row['id'], 'task_id': 'task', 'source_dispatch_id': 'worker1',
                      'source_snapshot_digest': snapshot_digest(self.candidate), 'reason': 'Use an alternative implementation.'}
        self.path = self.root / 'rejection.json'
        write_json(self.path, self.value)

    def reject(self):
        return continuation.record_continuation_rejection(self.context, self.path)

    def test_atomic_permission_neutral_record_and_idempotent_unconsumed_retry(self):
        before, config = deepcopy(self.context.run), deepcopy(self.context.config)
        rejection = self.reject()
        self.assertIsNone(self.context.run['pending_continuation'])
        self.assertTrue(self.context.run['pending_directive'])
        self.assertEqual(len(self.context.run['continuation_rejections']), 1)
        self.assertEqual(self.saved[-1]['continuation_rejections'], self.context.run['continuation_rejections'])
        self.assertIsNone(self.saved[-1]['pending_continuation'])
        self.assertTrue(self.saved[-1]['pending_directive'])
        self.assertEqual(self.context.config, config)
        for key in ('attempts', 'agent_dispatches', 'rounds', 'accepted', 'continuation_history'):
            self.assertEqual(self.context.run[key], before[key])
        self.assertEqual(self.reject()['id'], rejection['id'])
        self.assertEqual(len(self.context.run['continuation_rejections']), 1)
        packet = continuation.packet(self.context, 'task')
        self.assertEqual(packet['active_continuation_rejection'], rejection['id'])
        self.assertEqual(packet['continuation_history'][0]['resolution'], {'status': 'rejected', 'rejection_id': rejection['id']})
        self.assertEqual(packet['continuation_rejections'][0]['id'], rejection['id'])

    def test_strict_input_rejects_malformed_oversize_and_wrong_bindings_without_save(self):
        malformed = ['[]', '{}', '{', json.dumps({**self.value, 'extra': True}),
                     json.dumps({**self.value, 'reason': ' '}), json.dumps({**self.value, 'reason': 'x' * 4001}),
                     json.dumps({**self.value, 'reason': '\0'}),
                     json.dumps(self.value)[:-1] + ',"reason":"duplicate"}', ' ' * 65537]
        for key in ('continuation_id', 'task_id', 'source_dispatch_id', 'source_snapshot_digest'):
            malformed.append(json.dumps({**self.value, key: 'wrong'}))
        baseline = deepcopy(self.context.run)
        saves = len(self.saved)
        for raw in malformed:
            with self.subTest(raw=raw[:100]):
                self.path.write_text(raw)
                with self.assertRaises(HarnessError):
                    self.reject()
                self.assertEqual(self.context.run, baseline)
                self.assertEqual(len(self.saved), saves)

    def test_guard_survives_missing_directive_and_rejects_task_snapshot_and_source_changes(self):
        self.reject()
        directive = self.context.run.pop('pending_directive')
        with self.assertRaises(HarnessError):
            continuation.validate_continuation_rejection(self.context)
        self.context.run['pending_directive'] = directive
        continuation.validate_continuation_rejection(self.context)
        for change in ('candidate', 'task', 'source'):
            with self.subTest(change=change):
                original_run, original_candidate = deepcopy(self.context.run), deepcopy(self.candidate)
                source = Path(self.source['artifact'])
                original_source = source.read_bytes()
                if change == 'candidate':
                    self.candidate['files']['code'] = 'tampered'
                elif change == 'task':
                    self.context.run['current_task'] = 'another'
                else:
                    source.write_text('{}')
                with self.assertRaises(HarnessError):
                    continuation.validate_continuation_rejection(self.context)
                self.context.run, self.candidate = original_run, original_candidate
                source.write_bytes(original_source)

    def test_conflicting_and_consumed_replays_fail(self):
        rejection = self.reject()
        self.path.write_text(json.dumps({**self.value, 'reason': 'Different instruction'}))
        with self.assertRaises(HarnessError):
            self.reject()
        write_json(self.path, self.value)
        self.context.run['dispatches'].append({'id': 'worker2', 'role': 'worker', 'task_id': 'task',
                                             'invoked': True, 'snapshot_digest': self.row['snapshot_digest'],
                                             'continuation_rejection_id': rejection['id']})
        self.context.run.pop('pending_directive', None)
        with self.assertRaises(HarnessError):
            self.reject()
        self.assertIsNone(continuation.packet(self.context)['active_continuation_rejection'])

    def test_immutable_rejection_evidence_corruption_blocks_projection_and_guard(self):
        rejection = self.reject()
        Path(rejection['artifact']).write_text('{}')
        for operation in (lambda: continuation.packet(self.context),
                          lambda: continuation.validate_continuation_rejection(self.context)):
            with self.assertRaises(HarnessError):
                operation()


    def test_status_projects_pending_source_bindings_then_saved_revision(self):
        self.assertEqual(status_summary(self.context.run)['continuation'], {
            'state': 'awaiting_requirements', 'continuation_id': self.row['id'], 'task_id': 'task',
            'source_dispatch_id': 'worker1', 'source_snapshot_digest': self.row['snapshot_digest'],
            'request': self.row['request']})
        rejection = self.reject()
        self.assertEqual(status_summary(self.context.run)['continuation'], {
            'state': 'revision_pending', 'task_id': 'task', 'rejection_id': rejection['id']})

    def test_guard_rejects_other_worker_or_intervening_model_before_consumption(self):
        self.reject()
        with self.assertRaises(HarnessError):
            continuation.validate_continuation_rejection(self.context, worker_task='other')
        self.context.run['dispatches'].append({'id': 'commander2', 'role': 'commander', 'invoked': True})
        with self.assertRaises(HarnessError):
            continuation.validate_continuation_rejection(self.context)

    def test_forged_consumption_without_rejection_evidence_cannot_disable_guard(self):
        self.context.run['dispatches'].append({'id': 'worker2', 'role': 'worker', 'task_id': 'task',
            'invoked': True, 'continuation_rejection_id': 'forged'})
        with self.assertRaises(HarnessError):
            continuation.validate_continuation_rejection(self.context)
        with self.assertRaises(HarnessError):
            continuation.packet(self.context)

    def test_rejection_requires_original_pending_unaccepted_task(self):
        baseline = deepcopy(self.context.run)
        mutations = (lambda: self.context.run.update(pending_continuation=None),
                     lambda: self.context.run['accepted'].update(task={'commit': 'accepted'}),
                     lambda: self.context.run.update(current_task='other'),
                     lambda: self.context.run.update(status='completed'),
                     lambda: self.context.run.update(status='running'))
        for mutate in mutations:
            self.context.run = deepcopy(baseline)
            mutate()
            with self.assertRaises(HarnessError):
                self.reject()
            self.assertFalse(self.context.run.get('continuation_rejections'))


class RejectionLifecycleTests(RepoCase):
    def waiting(self, *, second_request=False, kind='commands', required_command=None):
        command = [sys.executable, '-c', "print('old continuation')"]
        new_command = [sys.executable, '-c', "print('new exact continuation')"]
        def change(backend, kwargs, packet, result):
            if kwargs['role'] != 'worker':
                return
            count = sum(call['role'] == 'worker' for call in backend.calls)
            if count == 2:
                with (kwargs['cwd'] / 'hello.py').open('a') as stream:
                    stream.write('# revised after operator rejection\n')
            if count == 1 or second_request and count == 2:
                result.update(status='blocked', blocker_kind='continuation', blockers=['need prerequisite'],
                    continuation_request={'kind': kind, 'commands': [command if count == 1 else new_command] if kind == 'commands' else [],
                                          'minimum_timeout_seconds': 0 if kind == 'commands' else self.config.data['runtime']['agent_timeout_seconds'] + 60,
                                          'reason': 'finish candidate'})
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        plan = read_json(ROOT / 'examples/plan.json')
        if required_command is not None:
            plan['tasks'][0]['required_commands'] = [required_command]
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', plan)
        row = engine.run['continuation_history'][-1]
        path = self.repo / 'rejection.json'
        write_json(path, {'continuation_id': row['id'], 'task_id': 'greet',
                         'source_dispatch_id': row['source']['dispatch_id'],
                         'source_snapshot_digest': row['snapshot_digest'], 'reason': 'Complete with another approach.'})
        return engine, backend, path, command, new_command

    def test_rejection_fresh_worker_new_request_requires_new_exact_grant_and_all_gates(self):
        engine, backend, path, old_command, new_command = self.waiting(second_request=True)
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)
        resumed = self.engine(backend)
        with self.assertRaises(HarnessError):
            resumed.resume(engine.run['id'], reject_continuation=path)
        self.assertEqual(resumed.run['attempts']['greet'], 2)
        self.assertFalse(any(c['role'] == 'commander' for c in backend.calls))
        self.assertNotIn(old_command, self.config.data['permissions']['worker_commands'])
        self.assertNotIn(new_command, self.config.data['permissions']['worker_commands'])
        self.assertIn('revised after operator rejection', (resumed.worktree / 'hello.py').read_text())
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)
        self.config.data['permissions']['worker_commands'].append(new_command)
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(sum(result['attempts'].values()), 3)
        workers = [d for d in result['dispatches'] if d['role'] == 'worker' and d.get('invoked')]
        self.assertEqual(len({d['id'] for d in workers}), 3)
        self.assertEqual(workers[1]['continuation_rejection_id'], result['continuation_rejections'][0]['id'])
        self.assertEqual([d['task_id'] for d in result['dispatches'] if d['role'] == 'verification'], ['greet', 'final'])
        self.assertEqual([c['kind'] for c in backend.calls if c['role'] == 'reviewer'], ['review', 'final_review'])
        self.assertEqual(backend.calls[1]['packet']['active_continuation_rejection'], result['continuation_rejections'][0]['id'])

    def test_saved_rejection_plain_resume_survives_preparation_failure_without_budget_charge(self):
        engine, backend, path, _, _ = self.waiting(kind='budget')
        before = deepcopy(engine.run)
        timeout = self.config.data['runtime']['agent_timeout_seconds']
        resumed = self.engine(backend)
        def fail(event, payload, logs):
            if event == 'pre_agent' and payload['role'] == 'worker':
                raise HarnessError('fixture preparation failure')
        with patch.object(resumed.hooks, 'emit', side_effect=fail), self.assertRaisesRegex(HarnessError, 'fixture preparation'):
            resumed.resume(engine.run['id'], reject_continuation=path)
        saved = read_json(engine.directory / 'state.json')
        self.assertIsNone(saved['pending_continuation'])
        self.assertEqual(len(saved['continuation_rejections']), 1)
        for key in ('attempts', 'agent_dispatches'):
            self.assertEqual(saved[key], before[key])
        self.assertEqual(len(backend.calls), 1)
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(sum(result['attempts'].values()), 2)
        self.assertEqual(self.config.data['runtime']['agent_timeout_seconds'], timeout)

    def test_pre_agent_candidate_mutation_blocks_before_invocation_and_guard_stays_active(self):
        engine, backend, path, _, _ = self.waiting()
        resumed = self.engine(backend)
        original = (engine.worktree / 'hello.py').read_bytes()
        def mutate(event, payload, logs):
            if event == 'pre_agent' and payload['role'] == 'worker':
                (engine.worktree / 'hello.py').write_text('# changed by preparation\n')
        with patch.object(resumed.hooks, 'emit', side_effect=mutate), self.assertRaises(HarnessError):
            resumed.resume(engine.run['id'], reject_continuation=path)
        saved = read_json(engine.directory / 'state.json')
        self.assertEqual(len(backend.calls), 1)
        for key in ('attempts', 'agent_dispatches'):
            self.assertEqual(saved[key], engine.run[key])
        self.assertFalse(any(d.get('continuation_rejection_id') and d.get('invoked') for d in saved['dispatches']))
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        (engine.worktree / 'hello.py').write_bytes(original)
        self.assertEqual(self.engine(backend).resume(engine.run['id'])['status'], 'completed')

    def test_crash_after_atomic_rejection_save_plain_resume_uses_retained_guard(self):
        engine, backend, path, _, _ = self.waiting()
        resumed = self.engine(backend)
        save = resumed.context.save
        def interrupt_after_save():
            save()
            if resumed.run.get('continuation_rejections'):
                raise KeyboardInterrupt()
        with patch.object(resumed.context, 'save', side_effect=interrupt_after_save), self.assertRaises(KeyboardInterrupt):
            resumed.resume(engine.run['id'], reject_continuation=path)
        saved = read_json(engine.directory / 'state.json')
        self.assertIsNone(saved['pending_continuation'])
        self.assertEqual(sum(saved['attempts'].values()), 1)
        self.assertEqual(len(backend.calls), 1)
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(sum(result['attempts'].values()), 2)
        self.assertFalse(any(c['role'] == 'commander' for c in backend.calls))

    def test_rejection_does_not_reset_exhausted_worker_budget(self):
        self.config.data['runtime']['max_attempts'] = 1
        engine, backend, path, _, _ = self.waiting()
        resumed = self.engine(backend)
        with self.assertRaises(HarnessError):
            resumed.resume(engine.run['id'], reject_continuation=path)
        saved = read_json(engine.directory / 'state.json')
        self.assertEqual(saved['attempts']['greet'], 1)
        self.assertEqual(sum(saved['attempts'].values()), 1)
        self.assertEqual(len(backend.calls), 1)
        self.assertIsNone(saved['pending_continuation'])
        self.assertEqual(len(saved['continuation_rejections']), 1)
        self.config.data['runtime']['max_attempts'] = 2
        self.assertEqual(self.engine(backend).resume(engine.run['id'])['status'], 'completed')

    def test_resume_mutual_exclusions_do_not_register_rejection_or_dispatch(self):
        engine, backend, path, _, _ = self.waiting()
        options = ({'execution_guidance': path}, {'environment_update': path},
                   {'operator_decision': [path]}, {'reverify': True},
                   {'retry_stopped': 'greet'}, {'allow_scope': ['extra/**']})
        before = (engine.directory / 'state.json').read_bytes()
        for extra in options:
            with self.subTest(extra=extra), self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'], reject_continuation=path, **extra)
            self.assertEqual((engine.directory / 'state.json').read_bytes(), before)
            self.assertEqual(len(backend.calls), 1)

    def test_rejection_does_not_allow_permission_removal_on_resume(self):
        extra = [sys.executable, '-c', "print('retained grant')"]
        self.config.data['permissions']['worker_commands'].append(extra)
        engine, backend, path, _, _ = self.waiting()
        self.config.data['permissions']['worker_commands'].remove(extra)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], reject_continuation=path)
        self.assertEqual(len(backend.calls), 1)
        self.assertFalse(read_json(engine.directory / 'state.json').get('continuation_rejections'))

    def test_budget_request_rejection_completes_without_timeout_increase(self):
        timeout = self.config.data['runtime']['agent_timeout_seconds']
        engine, backend, path, _, _ = self.waiting(kind='budget')
        minimum = engine.run['continuation_history'][0]['request']['minimum_timeout_seconds']
        self.assertGreater(minimum, timeout)
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)
        result = self.engine(backend).resume(engine.run['id'], reject_continuation=path)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.config.data['runtime']['agent_timeout_seconds'], timeout)
        self.assertEqual(result['attempts']['greet'], 2)
        self.assertEqual(sum(result['attempts'].values()), 2)
        self.assertFalse(any(call['role'] == 'commander' for call in backend.calls))
        self.assertEqual([d['task_id'] for d in result['dispatches'] if d['role'] == 'verification'], ['greet', 'final'])

    def test_correction_still_checks_task_command_prerequisites_before_dispatch(self):
        required = [sys.executable, '-c', "print('task prerequisite')"]
        self.config.data['permissions']['worker_commands'].append(required)
        engine, backend, path, _, _ = self.waiting(required_command=required)
        resumed = self.engine(backend)
        record = continuation.record_continuation_rejection
        def revoke_after_record(context, rejection_path):
            row = record(context, rejection_path)
            # Model an in-process grant becoming unavailable after resume's
            # config compatibility check; task admission must still recheck.
            context.config.data['permissions']['worker_commands'].remove(required)
            return row
        with patch('harness.run_lifecycle.record_continuation_rejection', side_effect=revoke_after_record), \
                self.assertRaises(HarnessError):
            resumed.resume(engine.run['id'], reject_continuation=path)
        saved = read_json(engine.directory / 'state.json')
        self.assertEqual(saved['command_requirements']['missing'], {'greet': [required]})
        self.assertEqual(sum(saved['attempts'].values()), 1)
        self.assertEqual(saved['agent_dispatches'], engine.run['agent_dispatches'])
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(status_summary(saved)['continuation']['state'], 'revision_pending')
        self.assertFalse(any(d.get('continuation_rejection_id') and d.get('invoked') for d in saved['dispatches']))
        self.config.data['permissions']['worker_commands'].append(required)
        self.assertEqual(self.engine(backend).resume(engine.run['id'])['status'], 'completed')
