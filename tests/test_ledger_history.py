"""Only the current ledger version is run, cleaned or interpreted; every model is simulated offline.

CurrentLedgerTests use a completed run whose first worker attempt timed out
without output, then a command continuation, with a retained fresh-verification
checkout: cleanup, its exit confirmation, damaged-shape refusals and the
observation projections. UnsupportedLedgerTests save a paused run under another
schema_version: every entry refuses it before anything is changed, and the run
list names it by path and version alone.
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from harness import cli, git, observe
from harness.cleanup import cleanup_run
from harness.cli import status_summary
from harness.command_server import Broker, create_manifest
from harness.mcp_server import handle
from harness.memory import Memory
from harness.recovery import snapshot_digest
from harness.util import HarnessError, ProcessTimeout, read_json, write_json
from harness.versions import RUN_SCHEMA_VERSION
from harness.watch_cli import watch_run
from harness.workspace_inputs import prepare_run
from harness.worker_returns import SOURCES
from test_dashboard import DashboardCase, tree_snapshot
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend

INPUTS = {'packet': 'packet.json', 'schema': 'schema.json'}
# Ledger fields of earlier versions; a new ledger and its projections have none of them.
REMOVED_KEYS = {'total_attempts', 'attempt_budget_policy', 'replan_attempts', 'final_attempts', 'replan_stopped'}


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


def first_worker_times_out(backend, kwargs, packet):
    if kwargs['role'] == 'worker' and sum(call['role'] == 'worker' for call in backend.calls) == 1:
        raise ProcessTimeout(1, kwargs['job'] / 'process')


class CurrentLedgerTests(RepoCase):
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
        # Keep the verified checkout as a reclamation failure before its intent would.
        self.enterContext(patch("harness.finalization.FinalAcceptance.reclaim_verified_checkout"))
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
        folder, _ = journal(worker['command_directory'], worker['id'], 'delete_path')
        write_json(folder / 'delete-intent.json', {'path': 'obsolete.txt', 'sha256': '0' * 64})
        write_json(folder / 'receipt.json', {'operation': 'delete_path', 'outcome': 'deleted',
                                             'intent_path': str(folder / 'delete-intent.json'),
                                             'intent_sha256': sha(folder / 'delete-intent.json')})
        write_json(engine.directory / 'state.json', run)
        return engine, backend, run

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

    def test_confirm_exited_records_audit_without_rewriting_the_ledger(self):
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
        self.assertEqual((saved['schema_version'], saved['status']), (RUN_SCHEMA_VERSION, 'completed'))
        original = json.loads(self.before)
        # Only the confirmation and its store revision changed; no defaults were filled in.
        self.assertEqual(set(saved) - set(original), {'store_revision'} - set(original))
        events = [json.loads(line) for line in (self.directory / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'command_exit_confirmed_by_operator')
        self.assertEqual(events[-1]['command_id'], child['id'])

    def test_damaged_ledgers_are_refused(self):
        def evidence_reason(state, worker):
            evidence = self.workers(state)[0]['worker_return_evidence']
            evidence['reason'] = 'provider returned a complete explicit error'
        self.damaged(evidence_reason)
        self.assert_cleanup_refuses('classification does not match bound evidence')
        self.path.write_bytes(self.before)
        packet = self.directory / 'dispatches' / self.worker['id'] / 'packet.json'

        def changed_packet():
            original = packet.read_bytes()
            packet.write_bytes(original.replace(b'"goal"', b'"Goal"', 1))
            return lambda: packet.write_bytes(original)

        def embedded_intent():
            # Inputs copied into the intent instead of referenced by path and digest.
            def change(state, worker):
                job = self.directory / 'dispatches' / worker['id']
                intent = worker['worker_receipt']
                for field, name in INPUTS.items():
                    intent.pop(field + '_artifact')
                    intent.pop(field + '_artifact_digest')
                    intent[field] = read_json(job / name)
                intent['packet_digest'] = snapshot_digest(intent['packet'])
            self.damaged(change)
        for change, pattern in ((changed_packet, 'worker packet artifact changed'),
                                (embedded_intent, 'worker packet reference invalid')):
            with self.subTest(pattern=pattern):
                undo = change()
                self.assert_cleanup_refuses(pattern)
                self.path.write_bytes(self.before)
                if undo is not None:
                    undo()
        self.assertEqual(self.statuses(cleanup_run(self.repo, self.run['id'])), (['eligible'], 'eligible'))

    def test_status_dashboard_and_observation_read_the_ledger(self):
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
                         [(self.run['id'], RUN_SCHEMA_VERSION)])
        self.assertEqual(listing['unsupported'], [])
        detail = observe.run_detail(state, self.run['id'])
        self.assertIsNone(detail['ledger_error'])
        self.assertIsNone(detail['summary_error'])
        self.assertEqual(detail['schema_version'], RUN_SCHEMA_VERSION)
        self.assertEqual(detail['budgets']['attempts_total'], sum(self.run['attempts'].values()))
        self.assertEqual(len(detail['dispatches']), len(self.run['dispatches']))
        self.assertFalse(REMOVED_KEYS & set(summary['budgets']))
        self.assertFalse(REMOVED_KEYS & set(detail['budgets']))
        self.assertEqual(set(detail['budgets']['limits']), {'max_attempts', 'max_agent_dispatches', 'agent_timeout_seconds'})
        self.assertEqual(set(summary['budget_explanation']['limits']), {'task_work', 'agent_dispatches'})
        with observe.Observer(state) as observer:
            files = {item['path'] for item in observer.file_index(self.run['id'])['files']}
            self.assertIn('handoff.json', files)
            self.assertNotIn('handoff.md', files)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_new_ledger_has_no_removed_fields_and_status_sums_attempts(self):
        self.assertEqual(self.run['schema_version'], 18)
        self.assertFalse(REMOVED_KEYS & set(self.run))
        self.assertFalse({'max_total_attempts', 'claude_output_mode'} & set(self.run['config_snapshot']['runtime']))
        self.assertFalse(any('capture' in entry['worker_receipt'] for entry in self.workers(self.run)))
        evidence = self.workers(self.run)[0]['worker_return_evidence']
        self.assertEqual(set(evidence), {'version', 'state', 'reason', 'sources', 'provider'})
        self.assertEqual(evidence['version'], 2)
        self.assertEqual(set(evidence['sources']), set(SOURCES))
        self.assertEqual(self.run['attempts'], {'greet': 3})
        handoff = read_json(self.directory / 'handoff.json')
        self.assertNotIn('total_attempts', handoff)
        self.assertEqual(handoff['attempts'], {'greet': 3})
        budgets = status_summary(self.run)['budgets']
        self.assertEqual(budgets['attempts_total'], 3)
        self.assertFalse(REMOVED_KEYS & set(budgets))
        # A completed run's resume returns the saved result without dispatching.
        calls = len(self.backend.calls)
        self.assertEqual(self.engine(self.backend).resume(self.run['id'])['status'], 'completed')
        self.assertEqual(len(self.backend.calls), calls)
        self.assertEqual(self.path.read_bytes(), self.before)


# Whole-ledger shapes that are not an object, and a ledger whose version key is absent.
MISSING, ARRAY, NULL = 'missing', 'array', 'null'


def refusal(version):
    if type(version) is int:
        return f'台账版本 {version} 不支持（当前版本 {RUN_SCHEMA_VERSION}）'
    return f'台账版本缺失或无效，不支持（当前版本 {RUN_SCHEMA_VERSION}）'


def ledger_bytes(current, version, **fields):
    """The current ledger saved under another version, or replaced by a non-object."""
    if version in (ARRAY, NULL):
        return json.dumps([] if version == ARRAY else None).encode()
    run = json.loads(current)
    if version == MISSING:
        run.pop('schema_version')
    else:
        run['schema_version'] = version
    run.update(fields)
    return json.dumps(run).encode()


class UnsupportedLedgerTests(DashboardCase):
    # Object ledgers of other versions (including a string and a boolean), then non-objects.
    VERSIONS = (17, 19, 1, '18', True, MISSING, ARRAY, NULL)

    def setUp(self):
        super().setUp()
        # An ignored destination and a declared source make the input manifest landable.
        (self.repo / '.gitignore').write_text('__pycache__/\nvendor/\n')
        git.git(self.repo, 'add', '.gitignore')
        git.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', '-c', 'commit.gpgsign=false',
                'commit', '-m', 'ignore vendor')
        source = self.repo / 'deps' / 'input.dat'
        source.parent.mkdir()
        source.write_bytes(b'dependency bytes\n')
        source.chmod(0o644)
        self.backend = ScriptedBackend()
        self.runtime = self.engine(self.backend)
        run = self.runtime.start('Implement greet', read_json(ROOT / 'examples/plan.json'), pause_after_plan=True)
        self.ident, self.path = run['id'], self.runtime.directory / 'state.json'
        self.current = self.path.read_bytes()
        self.runs = self.repo / '.harness' / 'runs'
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.manifest = Path(folder.name).resolve() / 'inputs.json'
        write_json(self.manifest, {'schema_version': 1, 'run_id': self.ident, 'head': run['expected_head'],
                                   'source_root': '.', 'max_files': 1, 'max_bytes': 1024,
                                   'files': [{'source': 'deps/input.dat', 'destination': 'vendor/input.dat',
                                              'sha256': sha(source), 'mode': 0o644, 'type': 'file',
                                              'ownership': 'input_copy'}]})

    def save(self, version, **fields):
        self.path.write_bytes(ledger_bytes(self.current, version, **fields))
        return self.path.read_bytes()

    def tree(self):
        """Every run directory and the worktree: ledgers, locks, receipts and files."""
        return tree_snapshot(self.runs), tree_snapshot(self.runtime.worktree)

    def cli(self, *argv):
        output, errors = io.StringIO(), io.StringIO()
        # The operation record of resume would probe provider CLI versions; keep them unexecuted.
        with patch('harness.operations.probe_cli', return_value={'path': None, 'version': None}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(['--config', str(ROOT / 'harness.toml'), '--repo', str(self.repo), *argv])
        return code, output.getvalue(), errors.getvalue()

    def entries(self):
        state = self.repo / '.harness'
        return ((lambda: self.engine(self.backend).resume(self.ident)),
                (lambda: self.engine(self.backend).resume(self.ident, approve_plan=True)),
                (lambda: self.engine(self.backend).resume(self.ident, reverify=True)),
                (lambda: self.engine(self.backend).abandon(self.ident)),
                (lambda: prepare_run(self.repo, self.ident, self.manifest)),
                (lambda: cleanup_run(self.repo, self.ident)),
                (lambda: cleanup_run(self.repo, self.ident, remove=True)),
                (lambda: watch_run(state, self.ident, stdout=io.StringIO())))

    def test_every_entry_refuses_another_version_before_changing_anything(self):
        state = self.repo / '.harness'
        for version in self.VERSIONS:
            with self.subTest(version=version):
                # Terminal or not, nothing is resumed, abandoned, prepared or cleaned.
                for status in ('paused', 'completed'):
                    self.save(version, status=status)
                    before = self.tree()
                    pattern = re.escape(refusal(version))
                    for call in self.entries():
                        with self.assertRaisesRegex(HarnessError, pattern):
                            call()
                    with self.assertRaisesRegex(observe.Unsupported, pattern):
                        observe.run_detail(state, self.ident)
                    for argv in (('resume', self.ident), ('abandon', self.ident), ('status', self.ident),
                                 ('status', self.ident, '--full'), ('watch', self.ident),
                                 ('permission-diagnostics', self.ident, 'dispatch-1'),
                                 ('cleanup-run', self.ident), ('cleanup-run', self.ident, '--remove'),
                                 ('prepare-run', self.ident, '--manifest', str(self.manifest))):
                        code, output, errors = self.cli(*argv)
                        # Version refusals are input errors (2); cleanup-run keeps its own 0/1 results.
                        self.assertEqual((code, output), (1 if argv[0] == 'cleanup-run' else 2, ''), argv)
                        self.assertIn(refusal(version), errors)
                    # No ledger, lock, receipt, input copy or worktree file was written or removed.
                    self.assertEqual(self.tree(), before)
        self.assertEqual(self.backend.calls, [])
        # The same entries accept the current version, and the input manifest does land.
        self.path.write_bytes(self.current)
        self.assertEqual(self.cli('status', self.ident)[0], 0)
        self.assertEqual(observe.run_detail(state, self.ident)['schema_version'], RUN_SCHEMA_VERSION)
        self.assertEqual(prepare_run(self.repo, self.ident, self.manifest)['status'], 'prepared')
        self.assertEqual((self.runtime.worktree / 'vendor' / 'input.dat').read_bytes(), b'dependency bytes\n')

    def test_damaged_identity_of_another_version_gets_the_same_refusal_and_creates_nothing(self):
        for identity in ([], 'run_unexpected', {'id': 'nested'}):
            with self.subTest(identity=identity):
                self.save(17, id=identity)
                before = self.tree()
                for call in self.entries():
                    with self.assertRaisesRegex(HarnessError, re.escape(refusal(17))):
                        call()
                self.assertFalse((self.runs / 'run_unexpected').exists())
                self.assertEqual(self.tree(), before)
        # A current-version ledger naming another run is refused before the store
        # locks the directory that name points to.
        self.save(RUN_SCHEMA_VERSION, id='run_unexpected')
        before = self.tree()
        for call in (lambda: self.engine(self.backend).resume(self.ident),
                     lambda: self.engine(self.backend).abandon(self.ident)):
            with self.assertRaisesRegex(HarnessError, '运行台账字段缺失或类型无效'):
                call()
        self.assertFalse((self.runs / 'run_unexpected').exists())
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.backend.calls, [])

    def test_run_list_names_other_versions_by_path_and_version_only(self):
        current = self.engine(self.backend).start('Implement greet', read_json(ROOT / 'examples/plan.json'),
                                                  pause_after_plan=True)
        # Fields an old ledger might hold in any shape are never read.
        self.save(17, status={'not': 'a status'}, goal=['unreadable'], tasks='damaged', dispatches='damaged',
                  accepted='damaged', attempts=None)
        write_json(self.runs / 'run_noversion' / 'state.json', {'id': 'run_noversion', 'status': 'running'})
        write_json(self.runs / 'run_array' / 'state.json', [])
        write_json(self.runs / 'run_null' / 'state.json', None)
        (self.runs / 'run_damaged').mkdir()
        (self.runs / 'run_damaged' / 'state.json').write_text('{not json', encoding='utf-8')
        expected = sorted([{'path': f'runs/{self.ident}', 'schema_version': 17, 'reason': refusal(17)}]
                          + [{'path': f'runs/{name}', 'schema_version': None, 'reason': refusal(None)}
                             for name in ('run_noversion', 'run_array', 'run_null')],
                          key=lambda item: item['path'])

        listing = observe.list_runs(self.repo / '.harness')
        self.assertEqual([item['id'] for item in listing['runs']], [current['id']])
        self.assertEqual(sorted(listing['unsupported'], key=lambda item: item['path']), expected)
        self.assertEqual([item['path'] for item in listing['skipped']], ['runs/run_damaged/state.json'])
        self.assertEqual(listing['incomplete'], [])

        code, output, errors = self.cli('status')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), [{key: current[key] for key in ('id', 'status', 'goal', 'branch')}])
        for item in expected:
            self.assertIn(f"/.harness/{item['path']}：{item['reason']}", errors)

        server = self.start_server()
        status, _, body = self.request(server, '/api/runs')
        self.assertEqual(status, 200)
        self.assertEqual(sorted(json.loads(body)['unsupported'], key=lambda item: item['path']), expected)
        for run_id, version in ((self.ident, 17), ('run_array', None), ('run_null', None)):
            status, _, body = self.request(server, f'/api/runs/{run_id}')
            self.assertEqual((status, json.loads(body)['error']), (409, refusal(version)), run_id)
        status, _, body = self.request(server, f"/api/runs/{current['id']}")
        self.assertEqual((status, json.loads(body)['schema_version']), (200, RUN_SCHEMA_VERSION))
        status, _, body = self.request(server, '/api/runs/run_damaged')
        self.assertEqual(status, 200)
        self.assertIn('无法解析', json.loads(body)['ledger_error'])


class UnsupportedLedgerBrokerTests(RepoCase):
    """The command broker reads the ledger on its own; it refuses another version too."""

    def test_manifest_and_deletion_refuse_another_version_before_any_effect(self):
        (self.repo / 'obsolete.txt').write_text('old\n')
        git.git(self.repo, 'add', 'obsolete.txt')
        git.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', '-c', 'commit.gpgsign=false',
                'commit', '-m', 'deletable fixture')
        plan = read_json(ROOT / 'examples/plan.json')
        plan['tasks'][0]['allowed_paths'].append('obsolete.txt')
        seen = {}

        def during_worker(backend, kwargs, packet, result):
            if kwargs['role'] != 'worker' or seen:
                return
            path, job, target = engine.directory / 'state.json', kwargs['job'], kwargs['cwd'] / 'obsolete.txt'
            current = path.read_bytes()
            old = ledger_bytes(current, 17)
            try:
                # 1. No manifest, command directory or record is created for another version.
                fresh = job.parent / (job.name + '-fresh')
                path.write_bytes(old)
                seen['manifest'] = outcome(lambda: create_manifest(self.config, kwargs['cwd'], fresh, path))
                seen['manifest_created'] = fresh.exists()
                # 2. A broker bound while the ledger was current stops at its live-source check ...
                path.write_bytes(current)
                metadata = create_manifest(self.config, kwargs['cwd'], job, path)
                broker = Broker(metadata['artifact'], metadata['artifact_digest'])
                path.write_bytes(old)
                seen['source_delete'] = outcome(lambda: broker.delete_path({'path': 'obsolete.txt'}))
                seen['source_command'] = outcome(lambda: broker.list_commands())
                # 3. ... and at the task-scope check when the ledger changes after the source check.
                path.write_bytes(current)
                load = Broker._load

                def load_then_replace(self_):
                    loaded = load(self_)
                    path.write_bytes(old)
                    return loaded
                with patch.object(Broker, '_load', load_then_replace):
                    seen['scope_delete'] = outcome(lambda: broker.delete_path({'path': 'obsolete.txt'}))
                seen['file_kept'] = target.is_file() and target.read_text() == 'old\n'
                seen['records'] = sorted(item.name for item in (job / 'commands').iterdir())
            finally:
                path.write_bytes(current)
        engine = self.engine(ScriptedBackend(during_worker))
        run = engine.start('Implement greet', plan)
        self.assertEqual(run['status'], 'completed')
        for key in ('manifest', 'source_delete', 'source_command', 'scope_delete'):
            self.assertEqual(seen[key], refusal(17), key)
        self.assertFalse(seen['manifest_created'])
        self.assertTrue(seen['file_kept'])
        self.assertEqual(seen['records'], [])  # No call was published, so none was recorded.


def outcome(call):
    try:
        call()
    except HarnessError as error:
        return str(error)
    return 'accepted'


class UnsupportedLedgerMcpTests(unittest.TestCase):
    """The read-only MCP server reads the bound ledger for its own tools."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / '.harness'
        self.run = self.state / 'runs' / 'run_old' / 'state.json'
        (self.root / 'file.txt').write_text('content\n')
        self.memory = Memory(self.state / 'memory.sqlite3')
        self.memory.remember(key='fact', abstract='fact', overview='fact', detail='fact', source='operator')

    def test_each_ledger_reading_tool_refuses_another_version(self):
        calls = {'run_status': {}, 'file_sha256': {'path': 'file.txt'}, 'json_check': {'path': 'file.txt'},
                 'file_diff': {'left': 'file.txt', 'right': 'file.txt'},
                 'git_query': {'argv': ['git', 'ls-files', '--', 'file.txt']},
                 'memory_search': {'query': 'fact'}, 'memory_read': {'id': self.memory.export()[0]['id']}}
        for version in (17, MISSING, ARRAY):
            write_json(self.run, json.loads(ledger_bytes(json.dumps(
                {'schema_version': RUN_SCHEMA_VERSION, 'id': 'run_old', 'goal': {'interpreted': True},
                 'worktree': str(self.root), 'worktree_binding': {}}).encode(), version)))
            for name, arguments in calls.items():
                with self.subTest(version=version, tool=name):
                    request = {'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}
                    result = handle(request, self.memory, None, self.run)
                    self.assertEqual((result['isError'], result['content'][0]['text']), (True, refusal(
                        17 if version == 17 else None)))
        # The same tools answer for the current version.
        write_json(self.run, {'schema_version': RUN_SCHEMA_VERSION, 'id': 'run_old', 'goal': 'g',
                              'worktree': str(self.root)})
        request = {'method': 'tools/call', 'params': {'name': 'run_status', 'arguments': {}}}
        self.assertFalse(handle(request, self.memory, None, self.run)['isError'])

    def test_server_does_not_start_on_another_version(self):
        write_json(self.run, {'schema_version': 17, 'id': 'run_old', 'worktree': str(self.root)})
        request = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                              'params': {'name': 'run_status', 'arguments': {}}}) + '\n'
        result = subprocess.run([sys.executable, str(ROOT / 'harness' / 'mcp_entry.py'), '--state', str(self.state),
                                 '--skills', str(ROOT / 'shared' / 'skills'), '--run', str(self.run)],
                                input=request, capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertIn(refusal(17), result.stderr)

if __name__ == '__main__':
    unittest.main()
