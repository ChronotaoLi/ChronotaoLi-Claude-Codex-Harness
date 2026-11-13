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
from harness.versions import RUN_SCHEMA_VERSION
from harness.watch_cli import watch_run
from harness.workspace_inputs import prepare_run
from harness.worker_returns import SOURCES
from test_dashboard import DashboardCase
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
        self.assertEqual(set(evidence), {'version', 'state', 'reason', 'sources'})
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


class UnsupportedLedgerTests(DashboardCase):
    VERSIONS = (17, 19, 1, '18', None)

    def setUp(self):
        super().setUp()
        self.backend = ScriptedBackend()
        self.runtime = self.engine(self.backend)
        run = self.runtime.start('Implement greet', read_json(ROOT / 'examples/plan.json'), pause_after_plan=True)
        self.ident, self.path = run['id'], self.runtime.directory / 'state.json'
        self.current = self.path.read_bytes()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.manifest = Path(folder.name) / 'inputs.json'
        write_json(self.manifest, {'schema_version': 1, 'run_id': self.ident, 'head': run['expected_head'],
                                   'source_root': '.', 'max_files': 1, 'max_bytes': 1, 'files': []})

    def save(self, version, **fields):
        run = json.loads(self.current)
        if version is None:
            run.pop('schema_version')
        else:
            run['schema_version'] = version
        run.update(fields)
        write_json(self.path, run)
        return self.path.read_bytes()

    @staticmethod
    def refusal(version):
        if type(version) is int:
            return f'台账版本 {version} 不支持（当前版本 {RUN_SCHEMA_VERSION}）'
        return f'台账版本缺失或无效，不支持（当前版本 {RUN_SCHEMA_VERSION}）'

    def cli(self, *argv):
        output, errors = io.StringIO(), io.StringIO()
        # The operation record of resume would probe provider CLI versions; keep them unexecuted.
        with patch('harness.operations.probe_cli', return_value={'path': None, 'version': None}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(['--config', str(ROOT / 'harness.toml'), '--repo', str(self.repo), *argv])
        return code, output.getvalue(), errors.getvalue()

    def test_every_entry_refuses_another_version_before_changing_anything(self):
        state = self.repo / '.harness'
        for version in self.VERSIONS:
            with self.subTest(version=version):
                # Terminal or not, nothing is resumed, abandoned, prepared or cleaned.
                for status in ('paused', 'completed'):
                    before = self.save(version, status=status)
                    pattern = re.escape(self.refusal(version))
                    calls = (lambda: self.engine(self.backend).resume(self.ident),
                             lambda: self.engine(self.backend).resume(self.ident, approve_plan=True),
                             lambda: self.engine(self.backend).resume(self.ident, reverify=True),
                             lambda: self.engine(self.backend).abandon(self.ident),
                             lambda: prepare_run(self.repo, self.ident, self.manifest),
                             lambda: cleanup_run(self.repo, self.ident),
                             lambda: cleanup_run(self.repo, self.ident, remove=True),
                             lambda: watch_run(state, self.ident, stdout=io.StringIO()))
                    for call in calls:
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
                        self.assertEqual((code, output), (1, ''), argv)
                        self.assertIn(self.refusal(version), errors)
                    self.assertEqual(self.path.read_bytes(), before)
                    self.assertTrue(self.runtime.worktree.exists())
        self.assertEqual(self.backend.calls, [])
        self.assertFalse((self.runtime.directory / 'worktree-cleanup.json').exists())
        # The current version is still accepted by the same entries.
        self.path.write_bytes(self.current)
        self.assertEqual(self.cli('status', self.ident)[0], 0)
        self.assertEqual(observe.run_detail(state, self.ident)['schema_version'], RUN_SCHEMA_VERSION)

    def test_run_list_names_other_versions_by_path_and_version_only(self):
        current = self.engine(self.backend).start('Implement greet', read_json(ROOT / 'examples/plan.json'),
                                                  pause_after_plan=True)
        # Fields an old ledger might hold in any shape are never read.
        self.save(17, status={'not': 'a status'}, goal=['unreadable'], tasks='damaged', dispatches='damaged',
                  accepted='damaged', attempts=None)
        runs = self.repo / '.harness' / 'runs'
        write_json(runs / 'run_noversion' / 'state.json', {'id': 'run_noversion', 'status': 'running'})
        (runs / 'run_damaged').mkdir()
        (runs / 'run_damaged' / 'state.json').write_text('{not json', encoding='utf-8')
        expected = sorted([{'path': f'runs/{self.ident}', 'schema_version': 17, 'reason': self.refusal(17)},
                           {'path': 'runs/run_noversion', 'schema_version': None, 'reason': self.refusal(None)}],
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
        status, _, body = self.request(server, f'/api/runs/{self.ident}')
        self.assertEqual((status, json.loads(body)['error']), (409, self.refusal(17)))
        status, _, body = self.request(server, f"/api/runs/{current['id']}")
        self.assertEqual((status, json.loads(body)['schema_version']), (200, RUN_SCHEMA_VERSION))


if __name__ == '__main__':
    unittest.main()
