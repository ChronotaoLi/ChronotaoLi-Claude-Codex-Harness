"""Offline recovery tests for explicit operator environment updates."""
import copy
import hashlib
import sys
from pathlib import Path
from unittest.mock import patch

from harness.cli import status_summary
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class EnvironmentUpdateTests(RepoCase):
    def test_invalid_invocation_marker_cannot_reopen_old_environment_source(self):
        engine, backend, path, _ = self.blocked()
        before = self.saved(engine)
        for marker in (0, [], "false"):
            state = copy.deepcopy(before)
            state['dispatches'][-1]['invoked'] = marker
            write_json(engine.directory / 'state.json', state)
            raw = (engine.directory / 'state.json').read_bytes()
            with self.subTest(marker=marker), self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'], environment_update=path)
            self.assertEqual((engine.directory / 'state.json').read_bytes(), raw)
            self.assertEqual(len(backend.calls), 1)

    def blocked(self):
        def block_first(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker' and sum(c['role'] == 'worker' for c in backend.calls) == 1:
                result.update(status='blocked', blocker_kind='environment', blockers=['Dependency unavailable'])
        backend = ScriptedBackend(block_first)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', read_json(ROOT / 'examples' / 'plan.json'))
        source = next(d['id'] for d in engine.run['dispatches'] if d['role'] == 'worker')
        value = {'schema_version': 1, 'task_id': 'greet', 'blocker_dispatch_id': source,
                 'summary': 'Operator installed the required dependency', 'checks': []}
        path = self.repo / 'operator-update.json'
        write_json(path, value)
        return engine, backend, path, value

    def saved(self, engine):
        return read_json(engine.directory / 'state.json')

    def replanned_environment_task(self, selected_task):
        engine, backend, path, value = self.blocked()
        marker = self.repo / 'ready-for-greet'
        marker.touch()
        value['checks'] = [{'kind': 'path', 'path': str(marker), 'path_type': 'file'}]
        write_json(path, value)

        def add_ready_work(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker' and sum(c['role'] == 'worker' for c in backend.calls) == 2:
                result.update(status='blocked', blocker_kind='task', blockers=['Additional work is required'])
            elif kwargs['kind'] == 'replan':
                task = copy.deepcopy(packet['original_tasks'][0])
                task.update(id='other', title='Additional independent work')
                result.update(action='revise', reason='Add independent work', new_tasks=[task], updates=[])
            elif kwargs['kind'] == 'dispatch' and len(packet['ready']) > 1:
                result.update(selected_task=selected_task, instruction='Use this saved selection after recovery')

        backend.change = add_ready_work
        resumed = self.engine(backend)
        task_attempt = resumed.task_attempt

        def interrupt_after_replan(ready):
            if len(ready) > 1:
                raise KeyboardInterrupt()
            return task_attempt(ready)

        with patch.object(resumed, 'task_attempt', side_effect=interrupt_after_replan):
            with self.assertRaises(KeyboardInterrupt):
                resumed.resume(engine.run['id'], environment_update=path)
        saved = self.saved(engine)
        self.assertIsNone(saved['current_task'])
        self.assertEqual([task['id'] for task in saved['tasks']], ['greet', 'other'])
        self.assertTrue(all(not task['depends_on'] for task in saved['tasks']))
        marker.unlink()
        return engine, backend, marker

    def test_ready_task_check_does_not_block_selection_of_other_work(self):
        engine, backend, _ = self.replanned_environment_task('other')
        before = self.saved(engine)
        calls = len(backend.calls)
        result = self.engine(backend).resume(engine.run['id'], step=True)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(set(result['accepted']), {'other'})
        self.assertEqual(result['attempts'], {'greet': 2, 'other': 1})
        self.assertEqual(result['environment_checks'], before['environment_checks'])
        self.assertEqual([call['task_id'] for call in backend.calls[calls:] if call['role'] == 'worker'], ['other'])

    def test_selected_task_check_preserves_directive_without_worker_budget(self):
        engine, backend, marker = self.replanned_environment_task('greet')
        before = self.saved(engine)
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], step=True)
        saved = self.saved(engine)
        self.assertEqual([(call['role'], call['kind']) for call in backend.calls[calls:]], [('commander', 'dispatch')])
        for key in ('attempts', 'total_attempts'):
            self.assertEqual(saved[key], before[key])
        self.assertEqual(saved['agent_dispatches'], before['agent_dispatches'] + 1)
        self.assertFalse(saved['environment_checks'][-1]['passed'])
        directive = saved['pending_directive']
        self.assertEqual(directive['directive']['selected_task'], 'greet')
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], step=True)
        self.assertEqual(len(backend.calls), calls)
        retried = self.saved(engine)
        self.assertEqual(retried['pending_directive'], directive)
        for key in ('attempts', 'total_attempts', 'agent_dispatches'):
            self.assertEqual(retried[key], saved[key])
        marker.touch()
        result = self.engine(backend).resume(engine.run['id'], step=True)
        self.assertEqual(set(result['accepted']), {'greet'})
        self.assertEqual(result['attempts'], {'greet': 3})
        self.assertNotIn('pending_directive', result)
        later = backend.calls[calls:]
        self.assertFalse(any(call['role'] == 'commander' for call in later))
        worker = next(call for call in later if call['role'] == 'worker')
        self.assertEqual(worker['packet']['instruction'], directive['directive']['instruction'])

    def test_pre_invocation_interruption_preserves_environment_update_eligibility(self):
        for abrupt in (False, True):
            with self.subTest(abrupt=abrupt):
                engine, backend, path, value = self.blocked()
                before = self.saved(engine)
                resumed = self.engine(backend)
                # This boundary follows the durable running status but precedes
                # any subsequent model call. Suppress stop to model a hard crash.
                stop = (lambda error: None) if abrupt else resumed.stop
                with patch.object(resumed, 'finish_post_accept', side_effect=KeyboardInterrupt()), \
                        patch.object(resumed, 'stop', side_effect=stop):
                    with self.assertRaises(KeyboardInterrupt):
                        resumed.resume(engine.run['id'])
                saved = self.saved(engine)
                self.assertEqual(saved['status'], 'running' if abrupt else 'interrupted')
                for key in ('dispatches', 'attempts', 'total_attempts', 'agent_dispatches', 'feedback'):
                    self.assertEqual(saved[key], before[key])
                self.assertFalse(status_summary(saved)['last_environment_blocker']['stale'])
                result = self.engine(backend).resume(engine.run['id'], environment_update=path)
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(result['attempts'], {'greet': 2})
                self.assertEqual(len(result['environment_updates']), 1)
                self.assertEqual(result['environment_updates'][0]['blocker_dispatch_id'], value['blocker_dispatch_id'])
                for call in backend.calls[1:]:
                    self.assertEqual(call['packet']['environment_updates'][0]['summary'], value['summary'])

    def test_interrupted_commander_receipt_still_rejects_environment_update(self):
        engine, backend, path, _ = self.blocked()
        resumed = self.engine(backend)
        with patch.object(resumed, 'finish_commander_receipt', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                resumed.resume(engine.run['id'])
        before = self.saved(engine)
        self.assertEqual(before['status'], 'interrupted')
        self.assertTrue(before['commander_receipts'])
        self.assertTrue(status_summary(before)['last_environment_blocker']['stale'])
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], environment_update=path)
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(self.saved(engine), before)

    def test_abandoned_run_rejects_environment_update(self):
        engine, backend, path, _ = self.blocked()
        self.engine(backend).abandon(engine.run['id'])
        before = self.saved(engine)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], environment_update=path)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(self.saved(engine), before)

    def test_large_saved_update_uses_compact_command_references(self):
        engine, backend, path, value = self.blocked()
        argv = [sys.executable, '-c', '#' + ' ' * 3900]
        value['checks'] = [{'kind': 'worker_command', 'argv': argv}] * 12
        write_json(path, value)
        self.config.data['permissions']['worker_commands'].append(argv)
        result = self.engine(backend).resume(engine.run['id'], environment_update=path)
        self.assertEqual(result['status'], 'completed')
        artifact = Path(result['environment_updates'][0]['artifact'])
        self.assertEqual(read_json(artifact)['checks'], value['checks'])
        for call in backend.calls[1:]:
            update = call['packet']['environment_updates'][0]
            self.assertEqual(update['check_count'], 12)
            self.assertNotIn('checks', update)
            self.assertEqual(update['artifact'], str(artifact))

    def test_update_reaches_every_later_role_and_preserves_task_contract(self):
        engine, backend, path, value = self.blocked()
        before = copy.deepcopy(engine.run)
        calls = len(backend.calls)
        result = self.engine(backend).resume(engine.run['id'], environment_update=path)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['schema_version'], 11)
        self.assertEqual(result['tasks'], before['tasks'])
        self.assertEqual(result['config_snapshot']['roles'], before['config_snapshot']['roles'])
        self.assertEqual(result['original_tasks'], before['original_tasks'])
        self.assertEqual(result['attempts'], {'greet': 2})
        self.assertEqual(len(result['accepted']), 1)
        records = result['environment_updates']
        self.assertEqual(len(records), 1)
        reference = records[0]
        self.assertEqual(reference['task_id'], 'greet')
        self.assertEqual(reference['blocker_dispatch_id'], value['blocker_dispatch_id'])
        artifact = Path(reference['artifact'])
        self.assertTrue(artifact.is_relative_to(engine.directory))
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), reference['artifact_digest'])
        for call in backend.calls[calls:]:
            with self.subTest(kind=call['kind']):
                record = call['packet']['environment_updates'][0]
                self.assertEqual(record['summary'], value['summary'])
                self.assertEqual(record['checks'], [])
        self.assertTrue(any(c['kind'] == 'final_review' for c in backend.calls[calls:]))
        workers = [d for d in result['dispatches'] if d['role'] == 'worker']
        self.assertEqual(len({d['id'] for d in workers}), 2)

    def test_failed_check_is_durable_idempotent_and_spends_no_model_budget(self):
        engine, backend, path, value = self.blocked()
        marker = self.repo / 'installed-dependency'
        value['checks'] = [{'kind': 'path', 'path': str(marker), 'path_type': 'file'}]
        write_json(path, value)
        before = self.saved(engine)
        for _ in range(2):
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'], environment_update=path)
            saved = self.saved(engine)
            for key in ('attempts', 'total_attempts', 'agent_dispatches', 'tasks', 'feedback', 'accepted'):
                self.assertEqual(saved[key], before[key])
            self.assertEqual(len(saved['environment_updates']), 1)
            self.assertTrue(saved['environment_checks'])
            self.assertEqual(len(backend.calls), 1)
        path.unlink()
        marker.write_text('installed')
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['environment_updates']), 1)

    def test_saved_update_is_hash_bound_on_ordinary_resume(self):
        engine, backend, path, value = self.blocked()
        value['checks'] = [{'kind': 'path', 'path': str(self.repo / 'missing'), 'path_type': 'any'}]
        write_json(path, value)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], environment_update=path)
        artifact = Path(self.saved(engine)['environment_updates'][0]['artifact'])
        original = artifact.read_bytes()
        for mutation in ('tamper', 'delete'):
            if mutation == 'tamper':
                artifact.write_bytes(original + b' ')
            else:
                artifact.unlink()
            with self.subTest(mutation=mutation), self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'])
            self.assertEqual(len(backend.calls), 1)

    def test_invalid_operator_inputs_never_dispatch(self):
        engine, backend, path, original = self.blocked()
        invalid = [dict(original, schema_version=2), dict(original, task_id='other'),
                   dict(original, blocker_dispatch_id='wrong'), dict(original, summary=' '),
                   dict(original, summary='x' * 4001), dict(original, checks=[{}]),
                   dict(original, checks=[{'kind': 'path', 'path': 'relative', 'path_type': 'any'}]),
                   dict(original, checks=[{'kind': 'executable', 'name': 'relative/tool'}]),
                   dict(original, checks=[{'kind': 'executable', 'name': 'python3'}] * 17)]
        before = self.saved(engine)
        for value in invalid:
            write_json(path, value)
            with self.subTest(value=value), self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'], environment_update=path)
            self.assertEqual(len(backend.calls), 1)
            saved = self.saved(engine)
            for key in ('attempts', 'total_attempts', 'agent_dispatches', 'tasks', 'accepted'):
                self.assertEqual(saved[key], before[key])
            self.assertFalse(saved.get('environment_updates'))

    def test_exact_command_check_is_a_declaration_without_execution(self):
        engine, backend, path, value = self.blocked()
        sentinel = self.repo / 'command-executed'
        argv = [sys.executable, '-c', f'from pathlib import Path; Path({str(sentinel)!r}).touch()']
        self.config.data['permissions']['worker_commands'].append(argv)
        value['checks'] = [{'kind': 'worker_command', 'argv': argv},
                           {'kind': 'executable', 'name': sys.executable},
                           {'kind': 'path', 'path': str(self.repo), 'path_type': 'directory'}]
        write_json(path, value)
        result = self.engine(backend).resume(engine.run['id'], environment_update=path)
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(sentinel.exists())

    def test_source_worker_artifact_must_remain_authentic(self):
        engine, backend, path, _ = self.blocked()
        source = Path(engine.run['feedback']['worker_artifact'])
        source.write_bytes(source.read_bytes() + b' ')
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], environment_update=path)
        self.assertEqual(len(backend.calls), 1)

    def test_conflicting_recovery_options_reject_before_dispatch(self):
        engine, backend, path, value = self.blocked()
        for options in ({'reverify': True}, {'retry_stopped': 'greet'},
                        {'allow_scope': ['extra.py']}, {'repair_invalid': value['blocker_dispatch_id']}):
            with self.subTest(options=options), self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'], environment_update=path, **options)
            self.assertEqual(len(backend.calls), 1)

    def test_completed_run_rejects_update(self):
        engine, backend, path, _ = self.blocked()
        self.engine(backend).resume(engine.run['id'])
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], environment_update=path)
        self.assertEqual(len(backend.calls), calls)

    def test_version_eight_ordinary_resume_and_update_promotion(self):
        engine, backend, path, _ = self.blocked()
        state = self.saved(engine)
        state['schema_version'] = 8
        state.pop('environment_updates', None)
        state.pop('environment_checks', None)
        write_json(engine.directory / 'state.json', state)
        result = self.engine(backend).resume(engine.run['id'], environment_update=path)
        self.assertEqual(result['schema_version'], 11)
        self.assertEqual(result['status'], 'completed')

    def test_check_does_not_reblock_completed_worker_pending_review(self):
        engine, backend, path, value = self.blocked()
        marker = self.repo / 'dependency-ready'
        marker.write_text('installed')
        value['checks'] = [{'kind': 'path', 'path': str(marker), 'path_type': 'file'}]
        write_json(path, value)
        resumed = self.engine(backend)
        with patch.object(resumed, 'dispatch_review', side_effect=HarnessError('Review unavailable')):
            with self.assertRaises(HarnessError):
                resumed.resume(engine.run['id'], environment_update=path)
        self.assertEqual(self.saved(engine)['pending_task']['stage'], 'reviewing')
        marker.unlink()
        calls = len(backend.calls)
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(any(c['role'] == 'worker' for c in backend.calls[calls:]))

    def test_version_eight_ordinary_resume_remains_supported(self):
        engine, backend, _, _ = self.blocked()
        state = self.saved(engine)
        state['schema_version'] = 8
        state.pop('environment_updates', None)
        state.pop('environment_checks', None)
        write_json(engine.directory / 'state.json', state)
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts'], {'greet': 2})

    def test_unauthorized_command_is_a_saved_failing_gate(self):
        engine, backend, path, value = self.blocked()
        value['checks'] = [{'kind': 'worker_command', 'argv': ['unapproved-tool', '--write']}]
        write_json(path, value)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], environment_update=path)
        saved = self.saved(engine)
        self.assertEqual(len(saved['environment_updates']), 1)
        self.assertFalse(saved['environment_checks'][-1]['passed'])
        self.assertEqual(len(backend.calls), 1)

    def test_pending_commander_receipt_cannot_be_replaced_by_update(self):
        engine, backend, path, _ = self.blocked()
        resumed = self.engine(backend)
        with patch.object(resumed, 'finish_commander_receipt', side_effect=HarnessError('Receipt gate unavailable')):
            with self.assertRaises(HarnessError):
                resumed.resume(engine.run['id'])
        before = self.saved(engine)
        self.assertTrue(before['commander_receipts'])
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], environment_update=path)
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(self.saved(engine)['commander_receipts'], before['commander_receipts'])
        self.assertFalse(self.saved(engine).get('environment_updates'))

    def test_pending_directive_cannot_be_replaced_by_update(self):
        engine, backend, path, _ = self.blocked()
        resumed = self.engine(backend)
        dispatch = resumed.dispatch

        def stop_before_worker(role, *args, **kwargs):
            if role == 'worker':
                raise HarnessError('Worker preparation unavailable')
            return dispatch(role, *args, **kwargs)

        with patch.object(resumed, 'dispatch', side_effect=stop_before_worker):
            with self.assertRaises(HarnessError):
                resumed.resume(engine.run['id'])
        before = self.saved(engine)
        self.assertTrue(before['pending_directive'])
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], environment_update=path)
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(self.saved(engine)['pending_directive'], before['pending_directive'])
        self.assertFalse(self.saved(engine).get('environment_updates'))

    def test_budget_exhaustion_retains_update_without_authorizing_an_attempt(self):
        self.config.data['runtime']['max_total_attempts'] = 1
        engine, backend, path, _ = self.blocked()
        before = self.saved(engine)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], environment_update=path)
        saved = self.saved(engine)
        self.assertEqual(len(saved['environment_updates']), 1)
        for key in ('attempts', 'total_attempts', 'agent_dispatches', 'feedback', 'tasks', 'accepted'):
            self.assertEqual(saved[key], before[key])
        self.assertEqual(len(backend.calls), 1)
        self.config.data['runtime']['max_total_attempts'] = 2
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['environment_updates']), 1)
        self.assertEqual(result['attempts'], {'greet': 2})

    def test_newer_update_replaces_checks_but_preserves_history_in_all_packets(self):
        engine, backend, path, value = self.blocked()
        value['checks'] = [{'kind': 'path', 'path': str(self.repo / 'obsolete-dependency'), 'path_type': 'file'}]
        write_json(path, value)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], environment_update=path)
        first = self.saved(engine)['environment_updates'][0]
        value['summary'] = 'Dependency is no longer needed; use the configured verifier'
        value['checks'] = []
        write_json(path, value)
        calls = len(backend.calls)
        result = self.engine(backend).resume(engine.run['id'], environment_update=path)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['environment_updates']), 2)
        self.assertEqual(result['environment_updates'][0], first)
        self.assertEqual([r['sequence'] for r in result['environment_updates']], [1, 2])
        for call in backend.calls[calls:]:
            records = call['packet']['environment_updates']
            self.assertEqual(len(records), 1)
            self.assertEqual(len(call['packet']['environment_history']), 2)
            self.assertEqual(records[-1]['summary'], value['summary'])
            self.assertEqual(records[-1]['checks'], [])

    def test_worker_format_correction_rechecks_prerequisite_before_spending_budget(self):
        engine, backend, path, value = self.blocked()
        marker = self.repo / 'ready-for-worker'
        marker.write_text('ready')
        value['checks'] = [{'kind': 'path', 'path': str(marker), 'path_type': 'file'}]
        write_json(path, value)

        def remove_prerequisite(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker' and sum(c['role'] == 'worker' for c in backend.calls) == 2:
                marker.unlink()
                result['summary'] = 'x' * 4001

        backend.change = remove_prerequisite
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], environment_update=path)
        saved = self.saved(engine)
        workers = [c for c in backend.calls if c['role'] == 'worker']
        self.assertEqual(len(workers), 2)
        self.assertEqual(saved['attempts'], {'greet': 2})
        self.assertEqual(saved['total_attempts'], 2)
        self.assertEqual(saved['agent_dispatches'], len(backend.calls))
        self.assertIn('worker:implement:greet', saved['pending_format_corrections'])
        before = copy.deepcopy(saved)
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)
        for key in ('attempts', 'total_attempts', 'agent_dispatches'):
            self.assertEqual(self.saved(engine)[key], before[key])
        marker.write_text('ready again')
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts'], {'greet': 3})
        workers = [c for c in backend.calls if c['role'] == 'worker']
        self.assertEqual(len(workers), 3)
        self.assertIn('format_correction', workers[-1]['packet'])
        self.assertEqual(result['format_repairs']['worker:implement:greet'], 1)

    def test_interrupted_update_recording_reuses_saved_artifact_without_duplicate(self):
        for boundary in ('event', 'ledger'):
            with self.subTest(boundary=boundary):
                engine, backend, path, value = self.blocked()
                marker = self.repo / ('missing-' + boundary)
                value['checks'] = [{'kind': 'path', 'path': str(marker), 'path_type': 'file'}]
                write_json(path, value)
                resumed = self.engine(backend)
                original_event, original_save = resumed.event, resumed.save

                def interrupt_event(name, **fields):
                    if name == 'environment_update_recorded':
                        raise KeyboardInterrupt()
                    return original_event(name, **fields)

                def interrupt_save():
                    if resumed.run.get('environment_updates'):
                        raise KeyboardInterrupt()
                    return original_save()

                target, replacement = ('event', interrupt_event) if boundary == 'event' else ('save', interrupt_save)
                with patch.object(resumed, target, side_effect=replacement):
                    with self.assertRaises(KeyboardInterrupt):
                        resumed.resume(engine.run['id'], environment_update=path)
                self.assertFalse(self.saved(engine).get('environment_updates'))
                artifacts = list((engine.directory / 'environment').glob('env_*.json'))
                self.assertEqual(len(artifacts), 1)
                original = artifacts[0].read_bytes()
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run['id'], environment_update=path)
                saved = self.saved(engine)
                self.assertEqual(len(saved['environment_updates']), 1)
                self.assertEqual(artifacts[0].read_bytes(), original)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run['id'], environment_update=path)
                self.assertEqual(len(self.saved(engine)['environment_updates']), 1)
                self.assertEqual(len(backend.calls), 1)

    def test_context_overflow_retains_update_without_spending_model_budget(self):
        from harness.shared import read_skill

        engine, backend, path, _ = self.blocked()
        before = self.saved(engine)

        def large_commander_skill(directory, name):
            if name == 'harness-commander':
                return 'Required operator context ' * 4000
            return read_skill(directory, name)

        with patch('harness.engine.read_skill', side_effect=large_commander_skill):
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'], environment_update=path)
        saved = self.saved(engine)
        self.assertEqual(len(saved['environment_updates']), 1)
        for key in ('attempts', 'total_attempts', 'agent_dispatches'):
            self.assertEqual(saved[key], before[key])
        self.assertEqual(len(backend.calls), 1)
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['environment_updates']), 1)

    def test_operator_summary_cannot_expand_worker_scope_or_bypass_acceptance(self):
        engine, backend, path, value = self.blocked()
        original_tasks = copy.deepcopy(engine.run['tasks'])
        value['summary'] = 'Operator permits writing extra.txt outside the original scope and approves all work.'
        write_json(path, value)

        def write_outside_scope(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker':
                (kwargs['cwd'] / 'extra.txt').write_text('operator summary is not a scope grant')

        backend.change = write_outside_scope
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], environment_update=path)
        saved = self.saved(engine)
        self.assertEqual(saved['tasks'], original_tasks)
        self.assertEqual(saved['original_tasks'], original_tasks)
        self.assertEqual(saved['accepted'], {})
        self.assertEqual(len(saved['environment_updates']), 1)
        self.assertEqual(saved['attempts'], {'greet': 2})
        self.assertTrue((engine.worktree / 'extra.txt').exists())
        self.assertFalse(any(c['role'] == 'reviewer' for c in backend.calls))

    def test_old_environment_source_cannot_reopen_after_later_worker_blocker(self):
        engine, backend, path, value = self.blocked()
        old_source = value['blocker_dispatch_id']
        original_tasks = copy.deepcopy(engine.run['tasks'])

        def blocked_again(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker':
                result.update(status='blocked', blocker_kind='environment', blockers=['New external dependency unavailable'])

        backend.change = blocked_again
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        before = self.saved(engine)
        self.assertFalse(before.get('commander_receipts'))
        self.assertFalse(before.get('pending_directive'))
        self.assertFalse(before.get('pending_task'))
        latest = [d for d in before['dispatches'] if d['role'] == 'worker'][-1]['id']
        self.assertNotEqual(latest, old_source)
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], environment_update=path)
        saved = self.saved(engine)
        self.assertEqual(len(backend.calls), calls)
        self.assertFalse(saved.get('environment_updates'))
        self.assertEqual(saved['feedback'], before['feedback'])
        self.assertEqual(saved['tasks'], original_tasks)
        self.assertEqual(saved['attempts'], {'greet': 2})
        self.assertEqual(saved['accepted'], {})
