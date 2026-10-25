"""Offline lifecycle coverage for declared exact command prerequisites."""
import copy
from unittest.mock import patch

from harness.contracts import WORK, validate_plan, validate_revision
from harness.cli import status_summary
from harness.requirements import missing_commands
from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class CommandRequirementsTests(RepoCase):
    command = ['required-tool', 'run', '--exact']

    def plan(self, commands=None):
        plan = read_json(ROOT / 'examples' / 'plan.json')
        if commands is not None:
            plan['tasks'][0]['required_commands'] = copy.deepcopy(commands)
        return plan

    def saved(self, engine):
        return read_json(engine.directory / 'state.json')

    def test_invalid_supplied_commands_fail_before_run_or_model(self):
        backend = ScriptedBackend()
        invalid = [None, 'tool', ['tool'], [[]], [['']], [['  ']], [[True]], [['tool', None]], [['tool', 'a\0b']]]
        for commands in invalid:
            plan = self.plan()
            plan['tasks'][0]['required_commands'] = commands
            with self.subTest(commands=commands), self.assertRaises(HarnessError):
                self.engine(backend).start('Implement greet', plan)
        self.assertEqual(backend.calls, [])
        self.assertFalse((self.repo / '.harness' / 'runs').exists())

    def test_exact_arguments_and_only_explicit_execution_grants_satisfy(self):
        exact = ['tool', '*', 'line\nbreak', '', ' spaced ']
        task = self.plan([exact])['tasks'][0]
        validate_plan({'tasks': [task]})
        config = copy.deepcopy(self.config.data)
        config['verification']['setup_commands'] = [exact]
        config['permissions']['worker_commands'] = [['tool'], ['tool', '*', 'line\nbreak', '', 'spaced']]
        self.assertEqual(missing_commands([task], {}, config), {'greet': [exact]})
        config['permissions']['worker_commands'].append(exact)
        self.assertEqual(missing_commands([task], {}, config), {})
        config['permissions']['worker_commands'] = []
        config['verification']['commands'].append(exact)
        self.assertEqual(missing_commands([task], {}, config), {})

    def test_blocked_resumes_spend_no_budgets_and_append_grant_recovers(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError) as error:
            engine.start('Implement greet', self.plan([self.command]))
        self.assertIn('permissions.worker_commands', str(error.exception))
        self.assertIn('resume', str(error.exception))
        before = self.saved(engine)
        for _ in range(2):
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run['id'])
            saved = self.saved(engine)
            for key in ('attempts', 'total_attempts', 'agent_dispatches'):
                self.assertEqual(saved[key], before[key])
        self.assertEqual(backend.calls, [])
        self.assertEqual(self.config.data['permissions']['worker_commands'], [])
        self.config.data['permissions']['worker_commands'].append(self.command)
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts'], {'greet': 1})
        self.assertEqual(result['command_requirements']['missing'], {})

    def test_plan_approval_does_not_grant_commands(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        result = engine.start('Implement greet', self.plan([self.command]), pause_after_plan=True)
        self.assertEqual(result['status'], 'awaiting_approval')
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], approve_plan=True)
        self.assertTrue(self.saved(engine)['plan_approved'])
        self.assertEqual(backend.calls, [])
        self.assertEqual(self.config.data['permissions']['worker_commands'], [])

    def test_appending_grant_refreshes_status_while_plan_still_awaits_approval(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        result = engine.start('Implement greet', self.plan([self.command]), pause_after_plan=True)
        self.assertEqual(status_summary(result)['missing_command_requirements'], {'greet': [self.command]})
        self.config.data['permissions']['worker_commands'].append(self.command)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(result['id'])
        saved = self.saved(engine)
        self.assertFalse(saved['plan_approved'])
        self.assertNotIn('missing_command_requirements', status_summary(saved))
        self.assertEqual(backend.calls, [])

    def test_model_plan_is_saved_without_replanning_to_avoid_missing_grant(self):
        def declare(backend, kwargs, packet, result):
            if kwargs['kind'] == 'plan':
                result['tasks'][0]['required_commands'] = [self.command]
        backend = ScriptedBackend(declare)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet')
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual([call['kind'] for call in backend.calls], ['plan'])
        self.assertEqual(self.saved(engine)['total_attempts'], 0)
        self.config.data['permissions']['worker_commands'].append(self.command)
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(sum(call['kind'] == 'plan' for call in backend.calls), 1)
        self.assertEqual(result['original_tasks'][0]['required_commands'], [self.command])

    def test_selected_directive_survives_worker_preparation_failure(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        self.config.data['permissions']['worker_commands'].append(self.command)
        plan = self.plan([self.command])
        plan['tasks'].append({**copy.deepcopy(plan['tasks'][0]), 'id': 'other'})

        def fail_worker(event, payload, logs):
            if event == 'pre_agent' and payload.get('role') == 'worker':
                raise HarnessError('worker preparation failed')

        with patch.object(engine, 'emit_hook', side_effect=fail_worker), self.assertRaises(HarnessError):
            engine.start('Implement greet', plan)
        saved = self.saved(engine)
        directive = saved['pending_directive']['directive']
        self.assertEqual(saved['current_task'], directive['selected_task'])
        self.assertEqual(saved['total_attempts'], 0)
        self.assertEqual([call['kind'] for call in backend.calls], ['dispatch'])
        result = self.engine(backend).resume(saved['id'], step=True)
        self.assertEqual(set(result['accepted']), {directive['selected_task']})
        self.assertNotIn('pending_directive', result)
        self.assertEqual(sum(call['kind'] == 'dispatch' for call in backend.calls), 1)
        worker = next(call for call in backend.calls if call['role'] == 'worker')
        self.assertEqual(worker['packet']['instruction'], directive['instruction'])

    def test_independent_ready_work_can_be_accepted(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        plan = self.plan([self.command])
        other = copy.deepcopy(plan['tasks'][0])
        other.update(id='other', required_commands=[])
        plan['tasks'].append(other)
        result = engine.start('Implement greet', plan, step=True)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(set(result['accepted']), {'other'})
        self.assertEqual([c['task_id'] for c in backend.calls if c['role'] == 'worker'], ['other'])
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(self.saved(engine)['attempts'], {'other': 1})

    def test_pending_preparation_rechecks_without_consuming_attempt(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        self.config.data['permissions']['worker_commands'].append(self.command)
        def fail_preparation(event, *args):
            if event == 'pre_agent':
                raise HarnessError('prepare failed')

        with patch.object(engine, 'emit_hook', side_effect=fail_preparation):
            with self.assertRaises(HarnessError):
                engine.start('Implement greet', self.plan([self.command]))
        self.assertEqual(backend.calls, [])
        before = self.saved(engine)
        # Exercise the last dispatch gate directly: persisted diagnostics cannot
        # authorize a worker if its current execution grants are absent.
        self.config.data['permissions']['worker_commands'] = []
        with self.assertRaises(HarnessError):
            engine.dispatch_once('worker', 'implement', 'greet', WORK, {}, task=engine.run['tasks'][0])
        for key in ('attempts', 'total_attempts', 'agent_dispatches'):
            self.assertEqual(engine.run[key], before[key])
        self.assertEqual(backend.calls, [])

    def test_requirements_immutable_for_existing_tasks(self):
        old = self.plan([self.command])['tasks']
        for changed in ([], [['different']], [self.command, ['additional']]):
            proposed = copy.deepcopy(old)
            proposed[0]['description'] += ' Refined.'
            proposed[0]['required_commands'] = changed
            with self.subTest(changed=changed), self.assertRaises(HarnessError):
                validate_revision(old, proposed, [])
        proposed = copy.deepcopy(old)
        proposed[0]['description'] += ' Refined.'
        self.assertEqual(validate_revision(old, proposed, []), proposed)

    def test_legacy_plan_stays_unchanged_and_new_task_can_declare(self):
        old = self.plan()['tasks']
        original = copy.deepcopy(old)
        new = copy.deepcopy(old[0])
        new.update(id='new', required_commands=[self.command])
        validate_revision(old, old + [new], [])
        self.assertEqual(old, original)
        self.assertNotIn('required_commands', old[0])
