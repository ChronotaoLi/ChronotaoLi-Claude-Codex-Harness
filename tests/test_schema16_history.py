"""Schema 16 runs and read-only schema 15 history; every model is simulated offline.

Each version gets its own terminal fixture: a completed run with one command
continuation and a retained fresh-verification checkout. The schema 15 fixture is
rewritten into the shape the schema 15 code saved (embedded worker inputs,
embedded continuation snapshots, the run-wide counter and budget policy, a
version-1 command manifest). The schema 16 fixture keeps the shape the current
code writes, plus a delete_path journal record.
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
from unittest.mock import patch
import uuid

from harness import cli, git, observe
from harness.cleanup import cleanup_run
from harness.cli import status_summary
from harness.recovery import snapshot_digest
from harness.util import HarnessError, read_json, write_json
from harness.versions import HISTORICAL_CLEANUP_VERSIONS, RUN_SCHEMA_VERSION
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend

INPUTS = {'packet': 'packet.json', 'schema': 'schema.json'}
REMOVED_KEYS = {'total_attempts', 'attempt_budget_policy'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def journal(commands, dispatch_id, command_id, *, exited=True):
    """One broker journal record, as run_command or delete_path writes it."""
    folder = Path(commands) / uuid.uuid4().hex
    folder.mkdir()
    entry = {'id': f'{dispatch_id}:command:{folder.name}', 'command_id': command_id, 'pgid': None,
             'process_state': 'exited' if exited else 'running', 'exit_confirmed': exited}
    write_json(folder / 'process.json', entry)
    return folder, entry


class HistoryCase(RepoCase):
    VERSION = None

    def setUp(self):
        super().setUp()
        self.engine_, self.backend, self.run = self.terminal()
        self.directory = self.engine_.directory
        self.path = self.directory / 'state.json'
        self.worker = self.workers(self.run)[-1]
        self.before = self.path.read_bytes()

    def workers(self, run):
        return [entry for entry in run['dispatches'] if entry['role'] == 'worker' and entry.get('invoked')]

    def terminal(self):
        self.config.data['verification'].update(fresh_checkout=True,
            commands=[[sys.executable, '-c', "from pathlib import Path; assert Path('hello.py').is_file()"]])
        command = [sys.executable, '-c', "print('focused continuation check')"]

        def script(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker' and sum(call['role'] == 'worker' for call in backend.calls) == 1:
                result.update(status='blocked', blocker_kind='continuation', blockers=['Need a focused check'],
                              continuation_request={'kind': 'commands', 'commands': [command],
                                                    'minimum_timeout_seconds': 0, 'reason': 'Finish the candidate'})
        backend = ScriptedBackend(script)
        engine = self.engine(backend)
        with contextlib.suppress(HarnessError):
            engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
        self.config.data['permissions']['worker_commands'].append(command)
        run = self.engine(backend).resume(engine.run['id'])
        self.assertEqual((run['status'], run['schema_version']), ('completed', RUN_SCHEMA_VERSION))
        self.assertTrue(run['continuation_history'])
        shutil.rmtree(engine.worktree / '__pycache__', ignore_errors=True)
        checkout = Path(read_json(next(engine.directory.glob('verification/final/*/verification-*/checkout.json')))['worktree'])
        self.addCleanup(shutil.rmtree, checkout.parents[1], True)
        worker = self.workers(run)[-1]
        if self.VERSION == 15:
            self.rewrite_as_schema15(engine, run, worker)
        else:
            folder, _ = journal(worker['command_directory'], worker['id'], 'delete_path')
            write_json(folder / 'delete-intent.json', {'path': 'obsolete.txt', 'sha256': '0' * 64})
            write_json(folder / 'receipt.json', {'operation': 'delete_path', 'outcome': 'deleted',
                                                 'intent_path': str(folder / 'delete-intent.json'),
                                                 'intent_sha256': sha(folder / 'delete-intent.json')})
        write_json(engine.directory / 'state.json', run)
        return engine, backend, run

    def rewrite_as_schema15(self, engine, run, worker):
        for entry in self.workers(run):
            intent, job = entry['worker_receipt'], engine.directory / 'dispatches' / entry['id']
            packet, schema = read_json(job / 'packet.json'), read_json(job / 'schema.json')
            for field in INPUTS:
                intent.pop(field + '_artifact')
                intent.pop(field + '_artifact_digest')
            intent.update(schema=schema, packet=packet, packet_digest=snapshot_digest(packet))
            for name in INPUTS.values():
                (job / name).unlink()
        run['continuation_history'] = [{**read_json(Path(row['artifact'])), 'artifact': row['artifact'],
                                         'artifact_digest': row['artifact_digest']}
                                        for row in run['continuation_history']]
        # Historical schema-15 fields, as that code saved them.
        run.update(schema_version=15, total_attempts=sum(run['attempts'].values()), attempt_budget_policy='work')
        run['config_snapshot']['runtime']['max_total_attempts'] = 60
        job = engine.directory / 'dispatches' / worker['id']
        write_json(job / 'commands.json', {
            'version': 1, 'role': 'worker', 'dispatch_id': worker['id'], 'cwd': run['worktree'], 'job': str(job),
            'run_path': str(engine.directory / 'state.json'), 'binding': run['worktree_binding'],
            'expected_head': run['expected_head'], 'timeout': 300, 'parent_deadline': 0.0,
            'cleanup_margin': 2.0, 'commands': []})
        journal(worker['command_directory'], worker['id'], 'a' * 64)
        (engine.directory / 'handoff.md').write_text('# Run handoff\n\nschema 15\n', encoding='utf-8')

    def damaged(self, change):
        state = json.loads(self.before)
        change(state, self.workers(state)[-1])
        write_json(self.path, state)

    def assert_cleanup_refuses(self, pattern):
        for remove in (False, True):
            with self.assertRaisesRegex(HarnessError, pattern):
                cleanup_run(self.repo, self.run['id'], remove=remove)
        self.assertTrue(self.engine_.worktree.exists())
        self.assertTrue(self.checkout().exists())

    def checkout(self):
        return Path(read_json(next(self.directory.glob('verification/final/*/verification-*/checkout.json')))['worktree'])

    def statuses(self, result):
        return [item['status'] for item in result['checkouts']], result['worktree']['status']

    # ----- the four cleanup cases, run for each version -----

    def test_cleanup_previews_leave_everything_unchanged(self):
        self.assertEqual(self.statuses(cleanup_run(self.repo, self.run['id'])), (['eligible'], 'eligible'))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['--repo', str(self.repo), 'cleanup-run', self.run['id']]), 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result['remove'])
        self.assertEqual(self.statuses(result), (['eligible'], 'eligible'))
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertTrue(self.engine_.worktree.exists())
        self.assertTrue(self.checkout().exists())

    def test_cleanup_remove_retires_checkouts_and_keeps_ledger_and_branch(self):
        checkout = self.checkout()
        result = cleanup_run(self.repo, self.run['id'], remove=True)
        self.assertEqual(self.statuses(result), (['removed'], 'removed'))
        self.assertFalse(checkout.exists())
        self.assertFalse(self.engine_.worktree.exists())
        self.assertEqual(git.git(self.repo, 'rev-parse', self.run['branch']).strip(), self.run['expected_head'])
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(read_json(self.directory / 'worktree-cleanup.json')['status'], 'removed')

    def test_confirm_exited_records_audit_without_upgrading_the_ledger(self):
        _, child = journal(self.worker['command_directory'], self.worker['id'], 'b' * 64, exited=False)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(HarnessError, 'Cannot confirm command process exited'):
            cleanup_run(self.repo, self.run['id'])
        self.assertEqual(self.path.read_bytes(), before)
        result = cleanup_run(self.repo, self.run['id'], confirm_exited=[self.worker['id']])
        self.assertEqual(self.statuses(result), (['eligible'], 'eligible'))
        saved = read_json(self.path)
        entry = next(d for d in saved['dispatches'] if d['id'] == self.worker['id'])
        self.assertIn(child['id'], entry['command_exit_confirmations'])
        self.assertEqual((saved['schema_version'], saved['status']), (self.VERSION, 'completed'))
        original = json.loads(self.before)
        # Only the confirmation and its store revision changed; no defaults were filled in.
        self.assertEqual(set(saved) - set(original), {'store_revision'} - set(original))
        self.assertEqual(REMOVED_KEYS & set(saved), REMOVED_KEYS & set(original))
        events = [json.loads(line) for line in (self.directory / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'command_exit_confirmed_by_operator')
        self.assertEqual(events[-1]['command_id'], child['id'])

    def test_damaged_ledgers_are_refused_in_their_own_version_shape(self):
        def without_capture(state, worker):
            worker['worker_receipt'].pop('capture')
        # Raising the version constant must not skip the capture contract on schema 15.
        self.damaged(without_capture)
        self.assert_cleanup_refuses('capture contract')
        self.path.write_bytes(self.before)
        for change, pattern in self.shape_damage():
            with self.subTest(pattern=pattern):
                undo = change()
                self.assert_cleanup_refuses(pattern)
                self.path.write_bytes(self.before)
                if undo is not None:
                    undo()
        self.assertEqual(self.statuses(cleanup_run(self.repo, self.run['id'])), (['eligible'], 'eligible'))

    # ----- observation -----

    def test_status_dashboard_and_observation_read_the_ledger(self):
        self.assert_version_budgets(*self.observed())

    def observed(self):
        summary = status_summary(self.run)
        self.assertEqual(summary['status'], 'completed')
        self.assertEqual(summary['budgets']['attempts_total'], sum(self.run['attempts'].values()))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['--repo', str(self.repo), 'status', self.run['id']]), 0)
        self.assertEqual(json.loads(output.getvalue()), json.loads(json.dumps(summary)))
        state = self.repo / '.harness'
        listing = observe.list_runs(state)
        self.assertEqual([(item['id'], item['schema_version']) for item in listing['runs']],
                         [(self.run['id'], self.VERSION)])
        detail = observe.run_detail(state, self.run['id'])
        self.assertIsNone(detail['ledger_error'])
        self.assertIsNone(detail['summary_error'])
        self.assertEqual(detail['schema_version'], self.VERSION)
        self.assertEqual(detail['budgets']['attempts_total'], sum(self.run['attempts'].values()))
        self.assertEqual(len(detail['dispatches']), len(self.run['dispatches']))
        with observe.Observer(state) as observer:
            files = {item['path'] for item in observer.file_index(self.run['id'])['files']}
            self.assertIn('handoff.json', files)
            if self.VERSION == 15:
                self.assertIn('schema 15', observer.read_file(self.run['id'], 'handoff.md')['text'])
            else:
                self.assertNotIn('handoff.md', files)
        self.assertEqual(self.path.read_bytes(), self.before)
        return summary, detail


class Schema15HistoryTests(HistoryCase):
    VERSION = 15

    def shape_damage(self):
        def changed_packet():
            self.damaged(lambda state, worker: worker['worker_receipt']['packet'].update(goal='changed'))

        def referenced_intent():
            def change(state, worker):
                job = self.directory / 'dispatches' / worker['id']
                intent = worker['worker_receipt']
                for field, name in INPUTS.items():
                    write_json(job / name, intent.pop(field))
                    intent[field + '_artifact'] = str(job / name)
                    intent[field + '_artifact_digest'] = sha(job / name)
                intent.pop('packet_digest')
            self.damaged(change)
        return [(changed_packet, 'worker receipt linkage'), (referenced_intent, 'worker receipt linkage')]

    def test_resume_is_refused_without_writing_or_dispatching(self):
        calls = len(self.backend.calls)
        for options in ({}, {'reverify': True}):
            with self.subTest(options=options):
                with self.assertRaisesRegex(HarnessError, '旧 run.*台账版本 15.*status.*cleanup-run'):
                    self.engine(self.backend).resume(self.run['id'], **options)
        errors = io.StringIO()
        # The operation record would probe provider CLI versions; keep them unexecuted.
        with patch('harness.operations.probe_cli', return_value={'path': None, 'version': None}), \
                contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--repo', str(self.repo), 'resume', self.run['id']]), 1)
        self.assertIn('只能用 status、dashboard 查看', errors.getvalue())
        self.assertEqual(len(self.backend.calls), calls)
        self.assertEqual(self.path.read_bytes(), self.before)

    def assert_version_budgets(self, summary, detail):
        # Historical display branch: the saved counter and policy are shown as saved.
        self.assertEqual(summary['budgets']['total_attempts'], self.run['total_attempts'])
        self.assertEqual(summary['budgets']['attempt_budget_policy'], 'work')
        self.assertEqual(detail['budgets']['total_attempts'], self.run['total_attempts'])
        self.assertEqual(detail['budgets']['limits']['max_total_attempts'], 60)
        self.assertIn('snapshot', self.run['continuation_history'][0])
        self.assertEqual(summary['budget_explanation']['limits']['total_worker']['used'], self.run['total_attempts'])


class Schema16HistoryTests(HistoryCase):
    VERSION = 16

    def shape_damage(self):
        packet = self.directory / 'dispatches' / self.worker['id'] / 'packet.json'

        def changed_packet():
            original = packet.read_bytes()
            packet.write_bytes(original.replace(b'"goal"', b'"Goal"', 1))
            return lambda: packet.write_bytes(original)

        def embedded_intent():
            def change(state, worker):
                job = self.directory / 'dispatches' / worker['id']
                intent = worker['worker_receipt']
                for field, name in INPUTS.items():
                    intent.pop(field + '_artifact')
                    intent.pop(field + '_artifact_digest')
                    intent[field] = read_json(job / name)
                intent['packet_digest'] = snapshot_digest(intent['packet'])
            self.damaged(change)
        return [(changed_packet, 'worker packet artifact changed'), (embedded_intent, 'worker receipt linkage')]

    def assert_version_budgets(self, summary, detail):
        self.assertFalse(REMOVED_KEYS & set(summary['budgets']))
        self.assertIsNone(detail['budgets']['total_attempts'])
        self.assertIsNone(detail['budgets']['limits']['max_total_attempts'])
        self.assertNotIn('total_worker', summary['budget_explanation']['limits'])

    def test_new_ledger_has_no_removed_fields_and_status_sums_attempts(self):
        self.assertEqual(self.run['schema_version'], 16)
        self.assertFalse(REMOVED_KEYS & set(self.run))
        self.assertNotIn('max_total_attempts', self.run['config_snapshot']['runtime'])
        self.assertEqual(self.run['attempts'], {'greet': 2})
        handoff = read_json(self.directory / 'handoff.json')
        self.assertNotIn('total_attempts', handoff)
        self.assertEqual(handoff['attempts'], {'greet': 2})
        budgets = status_summary(self.run)['budgets']
        self.assertEqual(budgets['attempts_total'], 2)
        self.assertFalse(REMOVED_KEYS & set(budgets))
        self.assertEqual(HISTORICAL_CLEANUP_VERSIONS, tuple(range(7, 17)))
        # A completed schema 16 run's resume returns the saved result without dispatching.
        calls = len(self.backend.calls)
        self.assertEqual(self.engine(self.backend).resume(self.run['id'])['status'], 'completed')
        self.assertEqual(len(self.backend.calls), calls)
        self.assertEqual(self.path.read_bytes(), self.before)


del HistoryCase


if __name__ == '__main__':
    import unittest
    unittest.main()
