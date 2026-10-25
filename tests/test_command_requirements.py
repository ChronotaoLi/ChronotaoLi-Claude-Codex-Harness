"""Offline lifecycle coverage for declared exact command prerequisites."""
import copy
from pathlib import Path
from unittest.mock import patch

from harness.adapters import CLIBackend, DemoBackend
from harness.contracts import WORK, validate_plan, validate_revision
from harness.cli import status_summary
from harness.engine import Engine
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
        invalid.extend([[self.command, self.command], [['tool', str(i)] for i in range(33)]])
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

    def command_repair_cases(self):
        yield 'duplicate', [self.command, self.command], [self.command]
        commands = [['required-tool', 'run', str(i)] for i in range(33)]
        yield 'too_many', commands, commands[:32]

    def assert_explicit_command_repair(self, engine, backend, kind):
        key = f"commander:{kind}:{'planning' if kind == 'plan' else 'replanning'}"
        before = self.saved(engine)
        source = before['commander_receipts'][key]
        original = Path(source['artifact']).read_bytes()
        self.assertEqual(sum(call['kind'] == kind for call in backend.calls), 1)
        self.assertNotIn(key, before['format_repairs'])
        self.assertNotIn(key, before['pending_format_corrections'])
        self.assertEqual(before['invalid_repairs'], {})
        calls = copy.deepcopy(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(backend.calls, calls)
        saved = self.saved(engine)
        self.assertEqual(saved['commander_receipts'][key], source)
        for field in ('agent_dispatches', 'attempts', 'total_attempts', 'replan_attempts'):
            self.assertEqual(saved[field], before[field])

        result = self.engine(backend).resume(engine.run['id'], repair_invalid=source['dispatch_id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(sum(call['kind'] == kind for call in backend.calls), 2)
        self.assertEqual(Path(source['artifact']).read_bytes(), original)
        repair = result['invalid_repairs'][key]
        self.assertEqual(repair['mode'], 'semantic')
        self.assertEqual(repair['status'], 'completed')
        self.assertEqual(repair['source']['dispatch_id'], source['dispatch_id'])
        self.assertNotEqual(repair['corrected_dispatch_id'], source['dispatch_id'])
        self.assertNotIn(key, result['format_repairs'])
        self.assertNotIn(key, result['pending_format_corrections'])
        self.assertEqual(len({entry['id'] for entry in result['dispatches']}), len(result['dispatches']))
        self.assertEqual(sum(call['kind'] == 'review' for call in backend.calls), len(result['accepted']))
        self.assertEqual(sum(call['kind'] == 'final_review' for call in backend.calls), 1)
        return result

    def test_invalid_model_commands_require_explicit_plan_repair(self):
        for mode, invalid, corrected in self.command_repair_cases():
            with self.subTest(mode=mode):
                self.config.data['permissions']['worker_commands'] = copy.deepcopy(corrected)
                def declare(backend, kwargs, packet, result):
                    if kwargs['kind'] == 'plan':
                        commands = corrected if 'invalid_output_correction' in packet else invalid
                        result['tasks'][0]['required_commands'] = copy.deepcopy(commands)

                backend = ScriptedBackend(declare)
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start('Implement greet')
                self.assertEqual(self.saved(engine)['tasks'], [])
                result = self.assert_explicit_command_repair(engine, backend, 'plan')
                self.assertEqual(result['original_tasks'][0]['required_commands'], corrected)
                self.assertEqual(result['tasks'], result['original_tasks'])
                self.assertEqual(result['attempts'], {'greet': 1})
                self.assertEqual(result['total_attempts'], 1)
                self.assertEqual(result['agent_dispatches'], 5)

    def test_invalid_new_task_commands_require_explicit_replan_repair(self):
        for mode, invalid, corrected in self.command_repair_cases():
            for long_reason in (False, True):
                with self.subTest(mode=mode, long_reason=long_reason):
                    self.config.data['permissions']['worker_commands'] = copy.deepcopy(corrected)
                    plan = self.plan()
                    def declare(backend, kwargs, packet, result):
                        if kwargs['role'] == 'worker' and sum(call['role'] == 'worker' for call in backend.calls) == 1:
                            result.update(status='blocked', blocker_kind='task', blockers=['Need prerequisite'])
                        if kwargs['kind'] == 'replan':
                            repairing = 'invalid_output_correction' in packet
                            commands = corrected if repairing else invalid
                            original = copy.deepcopy(plan['tasks'][0])
                            result.update(action='revise',
                                reason='x' * 4001 if long_reason and not repairing else 'Prepare prerequisite',
                                new_tasks=[{**original, 'id': 'prep', 'required_commands': copy.deepcopy(commands)}],
                                updates=[{'id': original['id'], 'description': original['description'],
                                          'add_depends_on': ['prep']}])

                    backend = ScriptedBackend(declare)
                    engine = self.engine(backend)
                    with self.assertRaises(HarnessError):
                        engine.start('Implement greet', plan)
                    self.assertEqual(self.saved(engine)['tasks'], plan['tasks'])
                    result = self.assert_explicit_command_repair(engine, backend, 'replan')
                    self.assertEqual(result['original_tasks'], plan['tasks'])
                    self.assertEqual(result['tasks'][1]['required_commands'], corrected)
                    self.assertEqual(list(result['accepted']), ['prep', 'greet'])
                    self.assertEqual(result['attempts'], {'greet': 2, 'prep': 1})
                    self.assertEqual(result['total_attempts'], 3)
                    self.assertEqual(result['replan_attempts'], 1)
                    # Blocker feedback and the greet retry each require selection.
                    self.assertEqual(sum(call['kind'] == 'dispatch' for call in backend.calls), 2)
                    self.assertEqual(result['agent_dispatches'], 10)

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


class WireSchemaBackend(CLIBackend):
    """Builds each real provider command, then answers like the demo backend."""
    def __init__(self, config, state):
        super().__init__(config, state)
        self.wire = []

    def invoke(self, **kwargs):
        self.command(kwargs['role'], kwargs['model'], kwargs['cwd'], kwargs['job'],
                     kwargs['schema'], kwargs['run_path'])
        self.wire.append((kwargs['role'], kwargs['kind'], kwargs['model']['cli'],
                          read_json(kwargs['job'] / 'provider-schema.json')))
        return DemoBackend().invoke(**kwargs)


class ProviderWireSchemaTests(RepoCase):
    def objects(self, schema):
        if 'properties' in schema:
            yield schema
            for value in schema['properties'].values():
                yield from self.objects(value)
        if 'items' in schema:
            yield from self.objects(schema['items'])

    def test_codex_outputs_require_every_key_while_local_plan_field_stays_optional(self):
        backend = WireSchemaBackend(self.config, self.repo / '.harness')
        engine = Engine(self.config, self.repo, backend=backend)
        self.assertEqual(engine.start('Implement greet')['status'], 'completed')
        codex = [(kind, schema) for role, kind, cli, schema in backend.wire if cli == 'codex']
        self.assertIn('plan', [kind for kind, _ in codex])
        self.assertIn('final_review', [kind for kind, _ in codex])
        for kind, schema in codex:
            for node in self.objects(schema):
                with self.subTest(kind=kind, keys=sorted(node['properties'])):
                    self.assertEqual(sorted(node['required']), sorted(node['properties']))
        plan = next(schema for kind, schema in codex if kind == 'plan')
        self.assertIn('required_commands', plan['properties']['tasks']['items']['required'])
        # The demo plan omits the field; local validation still accepts it.
        self.assertNotIn('required_commands', engine.run['original_tasks'][0])
        worker = [schema for role, _, cli, schema in backend.wire if role == 'worker']
        self.assertTrue(worker)
        self.assertNotIn('checkpoints', worker[0]['required'])
