"""Schema 17 runs and read-only schema 15/16 history; every model is simulated offline.

Each version gets its own terminal fixture: a completed run whose first worker
attempt timed out without output, then a command continuation, and a retained
fresh-verification checkout. Historical fixtures are rewritten into the shape the
older code saved: schema 15 also embeds worker inputs, continuation snapshots,
the run-wide counter and a version-1 command manifest; schema 15 and 16 declare a
capture mode in every worker intent and in the configuration snapshot.

The worker return evidence of a historical fixture is never derived by the code
under test. Its directory and record are copied from tests/data, which holds the
records bc94b6f's classifier produced for exactly those bytes; the generator,
tests/data/worker_return_history_bc94b6f.gen.py, imports only a bc94b6f export.
"""
import base64
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from harness import cli, git, observe
from harness.cleanup import cleanup_run
from harness.cli import status_summary
from harness.recovery import snapshot_digest
from harness.util import HarnessError, ProcessTimeout, read_json, write_json
from harness.versions import HISTORICAL_CLEANUP_VERSIONS, RUN_SCHEMA_VERSION
from harness.workspace_inputs import prepare_run
from harness.worker_returns import (HISTORICAL_SOURCES, SOURCES, inspect_historical_return,
                                    validate_historical_return_evidence)
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend

INPUTS = {'packet': 'packet.json', 'schema': 'schema.json'}
REMOVED_KEYS = {'total_attempts', 'attempt_budget_policy'}
HISTORY = json.loads((Path(__file__).with_name('data') / 'worker_return_history_bc94b6f.json').read_text(encoding='utf-8'))
ROWS = {row['name'].split()[0]: row for row in HISTORY['rows']}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def row_bytes(spec):
    return base64.b64decode(spec['base64']) if 'base64' in spec else spec['text'].encode('utf-8')


def write_row(job, row):
    """Make a dispatch directory hold exactly a baseline row's fingerprinted files."""
    for name in HISTORICAL_SOURCES:
        (job / name).unlink(missing_ok=True)
    for name, spec in row['files'].items():
        (job / name).write_bytes(row_bytes(spec))


def adopt_capture_contract(directory, run, mode, rows=()):
    """Rewrite schema 17 worker receipts into the schema 15/16 capture shape.

    rows maps worker dispatch IDs to baseline rows: that dispatch directory gets the
    row's files and its evidence becomes the row's bc94b6f record, never a record
    derived by the code under test.
    """
    capture = {'provider': 'claude', 'mode': mode}
    rows = dict(rows)
    run['config_snapshot']['runtime']['claude_output_mode'] = mode
    for entry in run['dispatches']:
        if entry.get('role') != 'worker' or not entry.get('invoked'):
            continue
        entry['worker_receipt']['capture'] = dict(capture)
        row = rows.pop(entry['id'], None)
        if row is not None:
            assert row['capture'] == capture, row['name']
            write_row(Path(directory) / 'dispatches' / entry['id'], row)
            entry['worker_return_evidence'] = copy.deepcopy(row['record'])
        else:
            assert 'worker_return_evidence' not in entry, 'historical evidence must come from a baseline row'
    for receipt in run['worker_receipts'].values():
        receipt['capture'] = dict(capture)
    assert not rows, rows


def journal(commands, dispatch_id, command_id, *, exited=True):
    """One broker journal record, as run_command or delete_path writes it."""
    folder = Path(commands) / uuid.uuid4().hex
    folder.mkdir()
    entry = {'id': f'{dispatch_id}:command:{folder.name}', 'command_id': command_id, 'pgid': None,
             'process_state': 'exited' if exited else 'running', 'exit_confirmed': exited}
    write_json(folder / 'process.json', entry)
    return folder, entry


def first_worker_times_out(backend, kwargs, packet):
    if kwargs['role'] == 'worker' and sum(call['role'] == 'worker' for call in backend.calls) == 1:
        raise ProcessTimeout(1, kwargs['job'] / 'process')


class WorkerReturnHistoryTableTests(unittest.TestCase):
    """bc94b6f's whole records, for every reachable classification of schema 15/16."""

    def directory(self, row):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        job = Path(temporary.name).resolve()
        write_row(job, row)
        return job

    def test_every_baseline_record_is_reproduced_and_accepted(self):
        self.assertEqual(len(ROWS), 64)
        self.assertEqual(len({(row['record']['state'], row['record']['reason']) for row in ROWS.values()}), 22)
        for name, row in ROWS.items():
            with self.subTest(row=row['name']):
                job = self.directory(row)
                self.assertEqual(inspect_historical_return(job, row['capture']), row['record'])
                validate_historical_return_evidence(job, row['record'], row['capture'])

    def test_bound_record_cannot_change_capture_files_or_classification(self):
        legacy, stream, dead_end = ROWS['L5'], ROWS['S9'], ROWS['S7']
        job = self.directory(legacy)
        with self.assertRaisesRegex(HarnessError, 'invalid worker return evidence record'):
            validate_historical_return_evidence(job, legacy['record'], stream['capture'])
        job = self.directory(stream)
        (job / 'stream-capture.json').write_bytes(row_bytes(stream['files']['stream-capture.json']) + b'\n')
        with self.assertRaisesRegex(HarnessError, 'worker return evidence changed'):
            validate_historical_return_evidence(job, stream['record'], stream['capture'])
        job = self.directory(dead_end)
        changed = {**dead_end['record'], 'reason': 'stream process was not launched'}
        with self.assertRaisesRegex(HarnessError, 'classification does not match bound evidence'):
            validate_historical_return_evidence(job, changed, dead_end['capture'])


class HistoryCase(RepoCase):
    VERSION = None
    MODE = None
    ROW = None

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
            if kwargs['role'] == 'worker' and sum(call['role'] == 'worker' for call in backend.calls) == 2:
                result.update(status='blocked', blocker_kind='continuation', blockers=['Need a focused check'],
                              continuation_request={'kind': 'commands', 'commands': [command],
                                                    'minimum_timeout_seconds': 0, 'reason': 'Finish the candidate'})
        backend = ScriptedBackend(script)
        backend.before_result = first_worker_times_out
        engine = self.engine(backend)
        with self.assertRaises(ProcessTimeout):
            engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
        with contextlib.suppress(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        self.config.data['permissions']['worker_commands'].append(command)
        run = self.engine(backend).resume(engine.run['id'])
        self.assertEqual((run['status'], run['schema_version']), ('completed', RUN_SCHEMA_VERSION))
        self.assertTrue(run['continuation_history'])
        timed_out = self.workers(run)[0]
        self.assertEqual((timed_out['worker_receipt_consumed'], timed_out['worker_return_evidence']['state']),
                         ('provider_failure', 'retryable'))
        shutil.rmtree(engine.worktree / '__pycache__', ignore_errors=True)
        checkout = Path(read_json(next(engine.directory.glob('verification/final/*/verification-*/checkout.json')))['worktree'])
        self.addCleanup(shutil.rmtree, checkout.parents[1], True)
        worker = self.workers(run)[-1]
        if self.VERSION != RUN_SCHEMA_VERSION:
            adopt_capture_contract(engine.directory, run, self.MODE, {timed_out['id']: ROWS[self.ROW]})
            run['schema_version'] = self.VERSION
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
        run.update(total_attempts=sum(run['attempts'].values()), attempt_budget_policy='work')
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
        def capture_contract(state, worker):
            # Raising the version constant must neither skip the schema 15/16
            # capture contract nor let schema 17 accept one.
            if self.VERSION == RUN_SCHEMA_VERSION:
                worker['worker_receipt']['capture'] = {'provider': 'claude', 'mode': 'legacy_json'}
            else:
                worker['worker_receipt'].pop('capture')
        self.damaged(capture_contract)
        self.assert_cleanup_refuses('capture contract')
        self.path.write_bytes(self.before)

        def evidence_reason(state, worker):
            evidence = self.workers(state)[0]['worker_return_evidence']
            evidence['reason'] = 'provider returned a complete explicit error'
        self.damaged(evidence_reason)
        self.assert_cleanup_refuses('classification does not match bound evidence')
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
            timed_out = self.workers(self.run)[0]['id']
            for name in ROWS[self.ROW]['files'] if self.ROW else ():
                self.assertIn(f'dispatches/{timed_out}/{name}', files)
                self.assertEqual(observer.read_file(self.run['id'], f'dispatches/{timed_out}/{name}')['text'],
                                 ROWS[self.ROW]['files'][name]['text'])
        self.assertEqual(self.path.read_bytes(), self.before)
        return summary, detail


class HistoricalCase(HistoryCase):
    def test_resume_is_refused_without_writing_or_dispatching(self):
        calls = len(self.backend.calls)
        for options in ({}, {'reverify': True}):
            with self.subTest(options=options):
                with self.assertRaisesRegex(HarnessError, f'旧 run.*台账版本 {self.VERSION}.*status.*cleanup-run'):
                    self.engine(self.backend).resume(self.run['id'], **options)
        errors = io.StringIO()
        # The operation record would probe provider CLI versions; keep them unexecuted.
        with patch('harness.operations.probe_cli', return_value={'path': None, 'version': None}), \
                contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--repo', str(self.repo), 'resume', self.run['id']]), 1)
        self.assertIn('只能用 status、dashboard 查看', errors.getvalue())
        # abandon shares the version gate; prepare-run has its own.
        with self.assertRaisesRegex(HarnessError, f'旧 run.*台账版本 {self.VERSION}'):
            self.engine(self.backend).abandon(self.run['id'])
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        manifest = Path(folder.name) / 'inputs.json'
        write_json(manifest, {'schema_version': 1, 'run_id': self.run['id'], 'head': self.run['expected_head'],
                              'source_root': '.', 'max_files': 1, 'max_bytes': 1, 'files': []})
        with self.assertRaisesRegex(HarnessError, 'historical run is read-only'):
            prepare_run(self.repo, self.run['id'], manifest)
        self.assertEqual(len(self.backend.calls), calls)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_saved_evidence_is_the_baseline_record(self):
        entry = self.workers(self.run)[0]
        self.assertEqual(entry['worker_return_evidence'], ROWS[self.ROW]['record'])
        self.assertEqual(set(entry['worker_return_evidence']['sources']), set(HISTORICAL_SOURCES))
        self.assertEqual(self.run['config_snapshot']['runtime']['claude_output_mode'], self.MODE)


class Schema15HistoryTests(HistoricalCase):
    VERSION, MODE, ROW = 15, 'legacy_json', 'L5'

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

    def assert_version_budgets(self, summary, detail):
        # Historical display branch: the saved counter and policy are shown as saved.
        self.assertEqual(summary['budgets']['total_attempts'], self.run['total_attempts'])
        self.assertEqual(summary['budgets']['attempt_budget_policy'], 'work')
        self.assertEqual(detail['budgets']['total_attempts'], self.run['total_attempts'])
        self.assertEqual(detail['budgets']['limits']['max_total_attempts'], 60)
        self.assertIn('snapshot', self.run['continuation_history'][0])
        self.assertEqual(summary['budget_explanation']['limits']['total_worker']['used'], self.run['total_attempts'])


class ReferencedInputsCase(HistoryCase):
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


class Schema16HistoryTests(ReferencedInputsCase, HistoricalCase):
    VERSION, MODE, ROW = 16, 'legacy_json', 'L5'


class Schema16StreamHistoryTests(ReferencedInputsCase, HistoricalCase):
    VERSION, MODE, ROW = 16, 'stream_metadata_v1', 'S9'

    def test_capture_mode_must_match_the_saved_configuration(self):
        def legacy_intent(state, worker):
            self.workers(state)[0]['worker_receipt']['capture']['mode'] = 'legacy_json'
        self.damaged(legacy_intent)
        self.assert_cleanup_refuses('differs from run configuration')


class Schema17Tests(ReferencedInputsCase):
    VERSION = RUN_SCHEMA_VERSION

    def test_new_ledger_has_no_removed_fields_and_status_sums_attempts(self):
        self.assertEqual(self.run['schema_version'], 17)
        self.assertFalse(REMOVED_KEYS & set(self.run))
        self.assertFalse({'max_total_attempts', 'claude_output_mode'} & set(self.run['config_snapshot']['runtime']))
        self.assertFalse(any('capture' in entry['worker_receipt'] for entry in self.workers(self.run)))
        evidence = self.workers(self.run)[0]['worker_return_evidence']
        self.assertEqual(set(evidence), {'version', 'state', 'reason', 'sources'})
        self.assertEqual(set(evidence['sources']), set(SOURCES))
        self.assertEqual(self.run['attempts'], {'greet': 3})
        handoff = read_json(self.directory / 'handoff.json')
        self.assertNotIn('total_attempts', handoff)
        self.assertEqual(handoff['attempts'], {'greet': 3})
        budgets = status_summary(self.run)['budgets']
        self.assertEqual(budgets['attempts_total'], 3)
        self.assertFalse(REMOVED_KEYS & set(budgets))
        self.assertEqual(HISTORICAL_CLEANUP_VERSIONS, tuple(range(7, 18)))
        # A completed run's resume returns the saved result without dispatching.
        calls = len(self.backend.calls)
        self.assertEqual(self.engine(self.backend).resume(self.run['id'])['status'], 'completed')
        self.assertEqual(len(self.backend.calls), calls)
        self.assertEqual(self.path.read_bytes(), self.before)


class AbandonedStreamTimeoutHistoryTests(RepoCase):
    """The leftover of a schema 16 stream-mode worker timeout: resume was blocked, then abandoned.

    Its active worker receipt and `unknown` evidence (baseline row S7) must not make the
    run uncleanable; cleanup is the only exit an old ledger has.
    """

    def setUp(self):
        super().setUp()
        backend = ScriptedBackend()
        backend.before_result = first_worker_times_out
        engine = self.engine(backend)
        with self.assertRaises(ProcessTimeout):
            engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
        run = self.engine(backend).abandon(engine.run['id'])
        self.assertEqual(run['status'], 'abandoned')
        self.directory, self.worktree = engine.directory, engine.worktree
        entry = run['dispatches'][-1]
        self.assertEqual(entry['role'], 'worker')
        self.assertNotIn('worker_return_evidence', entry)
        self.dispatch = entry['id']
        adopt_capture_contract(self.directory, run, 'stream_metadata_v1', {entry['id']: ROWS['S7']})
        (self.directory / 'dispatches' / entry['id'] / 'stream-metadata.jsonl').write_text('', encoding='utf-8')
        run['schema_version'] = 16
        self.run = run
        self.path = self.directory / 'state.json'
        write_json(self.path, run)
        self.before = self.path.read_bytes()

    def test_ledger_has_the_dead_end_shape(self):
        entry = self.run['dispatches'][-1]
        self.assertEqual(list(self.run['worker_receipts']), ['worker:implement:greet'])
        self.assertNotIn('worker_receipt_consumed', entry)
        evidence = entry['worker_return_evidence']
        self.assertEqual((evidence['state'], evidence['reason']),
                         ('unknown', 'stream reader stopped before durable transport completion'))
        self.assertEqual(evidence['capture'], {'provider': 'claude', 'mode': 'stream_metadata_v1'})
        self.assertEqual(set(evidence['sources']), set(HISTORICAL_SOURCES))
        self.assertIsNone(evidence['sources']['stream-capture.json'])

    def test_preview_observation_and_removal(self):
        result = cleanup_run(self.repo, self.run['id'])
        self.assertEqual((result['checkouts'], result['worktree']['status']), ([], 'eligible'))
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(status_summary(self.run)['status'], 'abandoned')
        state = self.repo / '.harness'
        detail = observe.run_detail(state, self.run['id'])
        self.assertIsNone(detail['ledger_error'])
        with observe.Observer(state) as observer:
            files = {item['path'] for item in observer.file_index(self.run['id'])['files']}
            for name in ('capture-manifest.json', 'process.outcome.json', 'stream-metadata.jsonl'):
                self.assertIn(f'dispatches/{self.dispatch}/{name}', files)
        removed = cleanup_run(self.repo, self.run['id'], remove=True)
        self.assertEqual(removed['worktree']['status'], 'removed')
        self.assertFalse(self.worktree.exists())
        self.assertEqual(git.git(self.repo, 'rev-parse', self.run['branch']).strip(), self.run['expected_head'])
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_capture_contract_is_still_checked(self):
        state = json.loads(self.before)
        state['worker_receipts']['worker:implement:greet'].pop('capture')
        write_json(self.path, state)
        with self.assertRaisesRegex(HarnessError, 'invalid worker receipt identity'):
            cleanup_run(self.repo, self.run['id'])
        state['dispatches'][-1]['worker_receipt'].pop('capture')
        write_json(self.path, state)
        with self.assertRaisesRegex(HarnessError, 'capture contract'):
            cleanup_run(self.repo, self.run['id'])
        self.assertTrue(self.worktree.exists())


del HistoryCase, HistoricalCase, ReferencedInputsCase


if __name__ == '__main__':
    unittest.main()
