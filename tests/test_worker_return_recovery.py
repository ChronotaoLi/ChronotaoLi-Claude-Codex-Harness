"""Exited-worker recovery uses durable transport facts; every model is offline."""
import hashlib
import json
from copy import deepcopy
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.adapters import CLIBackend, DemoBackend
from harness.util import HarnessError, ProcessTimeout, read_json, write_json
from harness.worker_returns import SOURCES, inspect_return, validate_return_evidence
from test_harness import RepoCase, ROOT
import test_worker_receipts as receipt_tests
import test_terminal_capture_recovery as terminal_tests


def stub_provider_clis(case):
    """Put recording claude/codex stubs first on PATH and require that neither runs."""
    folder = Path(tempfile.mkdtemp())
    log = folder / 'calls.log'
    for name in ('claude', 'codex'):
        stub = folder / name
        stub.write_text(f'#!/bin/sh\necho {name} "$@" >> "{log}"\nexit 97\n', encoding='utf-8')
        stub.chmod(0o755)
    case.addCleanup(shutil.rmtree, folder, True)
    case.addCleanup(lambda: case.assertFalse(log.exists(), log.read_text() if log.exists() else ''))
    environment = patch.dict(os.environ, {'PATH': str(folder) + os.pathsep + os.environ.get('PATH', '')})
    environment.start()
    case.addCleanup(environment.stop)


OK = {'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': {'status': 'completed'}}
ERROR = {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True}
INVALID = 'worker output is not a decodable object result: '


class WorkerReturnClassificationTests(unittest.TestCase):
    """Schema 17: one row per step of the no-result classification (spec-B1 3.4)."""

    ROWS = [
        ('1a returned result', {'returned-result.json': {}}, 'returned', 'worker result exists without a candidate seal'),
        ('1b response', {'response.json': {}, 'process.stdout': OK}, 'returned', 'worker result exists without a candidate seal'),
        ('2 provider result conflict', {'process.stdout': OK, 'provider-result.json': ERROR},
         'unknown', 'provider result conflicts with original output'),
        # A provider result beside empty output is a conflict, never an absent return.
        ('2b provider result without output', {'process.stdout': '', 'provider-result.json': OK},
         'unknown', 'provider result conflicts with original output'),
        ('3a stdout absent', {}, 'retryable', 'direct JSON output is absent or empty after exit'),
        ('3b stdout blank', {'process.stdout': ' \n'}, 'retryable', 'direct JSON output is absent or empty after exit'),
        ('4a explicit error', {'process.stdout': ERROR}, 'retryable', 'provider returned a complete explicit error'),
        ('4b error subtype', {'process.stdout': {'type': 'result', 'subtype': 'error_max_turns'}},
         'retryable', 'provider returned a complete explicit error'),
        # Only is_error true or an error_ subtype is an explicit error; nothing looser is retried.
        # Without an object result such a terminal is an invalid delivery (spec-B3 2.6).
        ('4c truthy is_error', {'process.stdout': {'type': 'result', 'is_error': 1}},
         'invalid', INVALID + 'result text JSONDecodeError'),
        ('4c2 truthy is_error beside an object', {'process.stdout': {**OK, 'is_error': 1}},
         'returned', 'non-error or ambiguous direct JSON output'),
        ('4d errorish subtype', {'process.stdout': {'type': 'result', 'subtype': 'errorish'}},
         'invalid', INVALID + 'result text JSONDecodeError'),
        ('4d2 errorish subtype beside an object', {'process.stdout': {**OK, 'subtype': 'errorish'}},
         'returned', 'non-error or ambiguous direct JSON output'),
        ('4e is_error without a result type', {'process.stdout': {'is_error': True}},
         'invalid', INVALID + 'not a result terminal'),
        ('5a success', {'process.stdout': OK}, 'returned', 'non-error or ambiguous direct JSON output'),
        ('5b success and equal provider result', {'process.stdout': OK, 'provider-result.json': OK},
         'returned', 'non-error or ambiguous direct JSON output'),
        ('5c non-result object', {'process.stdout': {'type': 'assistant'}}, 'invalid', INVALID + 'not a result terminal'),
        ('5d array', {'process.stdout': '[]'}, 'invalid', INVALID + 'not a result terminal'),
        ('5e prose result text', {'process.stdout': {'type': 'result', 'subtype': 'success', 'result': 'Done.'}},
         'invalid', INVALID + 'result text JSONDecodeError'),
        ('5f duplicate key', {'process.stdout': '{"type":"result","subtype":"success",'
                                                '"structured_output":{"status":"blocked","status":"completed"}}'},
         'invalid', INVALID + 'ValueError'),
        ('5g array result', {'process.stdout': {'type': 'result', 'subtype': 'success', 'structured_output': []}},
         'invalid', INVALID + 'result is not an object'),
        ('5h non-finite number', {'process.stdout': '{"type":"result","subtype":"success","structured_output":{"n":NaN}}'},
         'invalid', INVALID + 'ValueError'),
        ('6a truncated', {'process.stdout': '{"type":"result","subtype":"succ'},
         'invalid', INVALID + 'JSONDecodeError'),
        ('6b invalid UTF-8', {'process.stdout': b'\xff\xfe{'}, 'invalid', INVALID + 'UnicodeDecodeError'),
        ('7 files schema 17 never writes', {'capture-manifest.json': {'version': 1, 'mode': 'stream_metadata_v1'},
                                            'stream-terminal.json': OK, 'result.json': OK, 'terminal-capture.json': {}},
         'retryable', 'direct JSON output is absent or empty after exit'),
    ]

    def test_each_step_classifies_and_binds_exactly_its_files(self):
        for name, files, state, reason in self.ROWS:
            with self.subTest(row=name), tempfile.TemporaryDirectory() as temporary:
                job = Path(temporary).resolve()
                for filename, value in files.items():
                    data = value if isinstance(value, (str, bytes)) else json.dumps(value)
                    (job / filename).write_bytes(data if isinstance(data, bytes) else data.encode())
                record = inspect_return(job)
                self.assertEqual((record['state'], record['reason']), (state, reason))
                self.assertEqual(set(record), {'version', 'state', 'reason', 'sources'})
                self.assertEqual(record['sources'], {
                    source: hashlib.sha256((job / source).read_bytes()).hexdigest() if (job / source).exists() else None
                    for source in SOURCES})
                validate_return_evidence(job, record)
                with self.assertRaisesRegex(HarnessError, 'invalid worker return evidence record'):
                    validate_return_evidence(job, {**record, 'capture': {'provider': 'claude', 'mode': 'legacy_json'}})


class WorkerReturnRecoveryTests(RepoCase):
    def plan(self):
        return read_json(ROOT / 'examples/plan.json')

    def test_unknown_start_requires_confirmation_then_fresh_counted_attempt(self):
        class Interrupted(DemoBackend):
            calls = 0
            def invoke(self, **kwargs):
                if kwargs['role'] == 'worker':
                    self.calls += 1
                    if self.calls == 1:
                        raise KeyboardInterrupt()
                return super().invoke(**kwargs)
        backend = Interrupted()
        engine = self.engine(backend)
        with self.assertRaises(KeyboardInterrupt):
            engine.start('Implement greet', self.plan())
        first = engine.run['dispatches'][-1]
        with self.assertRaisesRegex(HarnessError, '无法确认'):
            engine.resume(engine.run['id'])
        self.assertEqual(backend.calls, 1)
        run = engine.resume(engine.run['id'], confirm_exited=[first['id']])
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['attempts'], {'greet': 2})
        workers = [d for d in run['dispatches'] if d['role'] == 'worker']
        self.assertEqual(len({d['id'] for d in workers}), 2)
        self.assertEqual(workers[0]['worker_return_evidence']['state'], 'retryable')
        self.assertEqual(workers[0]['worker_receipt_consumed'], 'provider_failure')

    def test_semantic_rejections_retire_receipts_and_exhaust_budget(self):
        class Invalid(DemoBackend):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs['role'] == 'worker':
                    result['blockers'] = ['Contradicts completed status']
                    write_json(kwargs['job'] / 'response.json', result)
                return result
        engine = self.engine(Invalid())
        # Each semantic rejection spends one attempt; scheduling continues to the limit.
        with self.assertRaisesRegex(HarnessError, '尝试次数上限'):
            engine.start('Implement greet', self.plan())
        limit = self.config.data['runtime']['max_attempts']
        self.assertEqual(engine.run['attempts']['greet'], limit)
        self.assertFalse(engine.run['worker_receipts'])
        self.assertFalse(engine.run['accepted'])
        with self.assertRaisesRegex(HarnessError, '尝试次数上限'):
            engine.resume(engine.run['id'])
        workers = [d for d in engine.run['dispatches'] if d['role'] == 'worker']
        self.assertEqual(len(workers), limit)
        self.assertTrue(all(d['worker_receipt_consumed'] == 'result_rejected' and d['status'] == 'invalid'
                            and '不一致' in d['error'] for d in workers))
        self.assertEqual(engine.run['worker_recovery']['greet']['dispatch_id'], workers[-1]['id'])

    def test_retry_classification_preserves_original_timeout_provenance(self):
        class TimeoutOnce(DemoBackend):
            first = True
            def invoke(self, **kwargs):
                if kwargs['role'] == 'worker' and self.first:
                    self.first = False
                    raise ProcessTimeout(1, kwargs['job'] / 'process')
                return super().invoke(**kwargs)
        engine = self.engine(TimeoutOnce())
        with self.assertRaises(ProcessTimeout):
            engine.start('Implement greet', self.plan())
        original = deepcopy(engine.run['worker_recovery']['greet'])
        with patch.object(engine.services.tasks, 'task_attempt', side_effect=HarnessError('stop before new work')):
            with self.assertRaisesRegex(HarnessError, 'stop before new work'):
                engine.resume(engine.run['id'])
        self.assertEqual(engine.run['worker_recovery']['greet'], original)
        self.assertEqual(original['error_type'], 'ProcessTimeout')
        result = engine.resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts']['greet'], 2)

    def test_lingering_failure_is_retryable_only_after_descendants_exit(self):
        backend = terminal_tests.LingeringCLI(self.config, self.repo, 'implement', no_terminal=True, exit_code=1)
        self.addCleanup(backend.release)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', self.plan())
        first = engine.run['dispatches'][-1]
        self.assertFalse(first['exit_confirmed'])
        with self.assertRaisesRegex(HarnessError, '进程组仍存在'):
            engine.resume(engine.run['id'])
        self.assertEqual(engine.run['attempts']['greet'], 1)
        backend.release()
        run = engine.resume(engine.run['id'])
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['attempts']['greet'], 2)
        self.assertEqual(run['dispatches'][0]['worker_return_evidence']['state'], 'retryable')

    def test_worker_timeout_with_empty_output_is_retried_by_plain_resume(self):
        """The JSON-mode counterpart of the stream timeout dead end: a real child times out."""
        stub_provider_clis(self)
        self.config.data['runtime']['agent_timeout_seconds'] = 1
        config, repo = self.config, self.repo

        class TimeoutOnce(DemoBackend):
            first = True
            def invoke(self, **kwargs):
                if kwargs['role'] == 'worker' and self.first:
                    self.first = False
                    cli = CLIBackend(config, repo / '.harness')
                    cli.command = lambda *args: [sys.executable, '-c', 'import time; time.sleep(30)']
                    return cli.invoke(**kwargs)
                return super().invoke(**kwargs)
        engine = self.engine(TimeoutOnce())
        with self.assertRaises(ProcessTimeout):
            engine.start('Implement greet', self.plan())
        first = engine.run['dispatches'][-1]
        job = engine.directory / 'dispatches' / first['id']
        self.assertEqual((job / 'process.stdout').read_bytes(), b'')
        self.assertFalse((job / 'capture-manifest.json').exists())
        run = engine.resume(engine.run['id'])
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['attempts'], {'greet': 2})
        entry = run['dispatches'][0]
        self.assertEqual((entry['worker_return_evidence']['state'], entry['worker_return_evidence']['reason']),
                         ('retryable', 'direct JSON output is absent or empty after exit'))
        self.assertEqual(entry['worker_receipt_consumed'], 'provider_failure')
        # Every invoked Claude dispatch now records diagnostics; the timeout is counted as one.
        self.assertEqual(entry['diagnostics']['failure_class'], 'local_timeout')
        from harness.cli import status_summary
        failures = status_summary(run)['budget_explanation']['dimensions']['failed_worker_invocations']
        self.assertEqual(failures['local_timeout'], 1)

    def test_unsealed_result_cannot_be_deleted_to_turn_into_retry(self):
        class ReturnedThenInterrupted(DemoBackend):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs['role'] == 'worker':
                    raise KeyboardInterrupt()
                return result
        engine = self.engine(ReturnedThenInterrupted())
        with self.assertRaises(KeyboardInterrupt):
            engine.start('Implement greet', self.plan())
        first = engine.run['dispatches'][-1]
        with self.assertRaisesRegex(HarnessError, 'before immutable seal'):
            engine.resume(engine.run['id'], confirm_exited=[first['id']])
        entry = engine.run['dispatches'][0]
        self.assertEqual(entry['worker_return_evidence']['state'], 'returned')
        self.assertIn('before immutable seal', entry['error'])
        self.assertEqual(entry['error'], engine.run['last_runtime_error']['error'])
        (engine.directory / 'dispatches' / first['id'] / 'response.json').unlink()
        with self.assertRaisesRegex(HarnessError, 'return evidence changed'):
            engine.resume(engine.run['id'])
        self.assertEqual(engine.run['attempts']['greet'], 1)

    def test_sealed_replay_records_candidate_error_without_new_dispatch(self):
        engine = receipt_tests.WorkerReceiptTests.interrupted(self, 'after_seal')
        (engine.worktree / 'hello.py').write_text('tampered = True\n')
        with self.assertRaisesRegex(HarnessError, 'candidate changed'):
            engine.resume(engine.run['id'])
        worker = engine.run['dispatches'][-1]
        self.assertIn('candidate changed', worker['error'])
        self.assertEqual(worker['error'], engine.run['last_runtime_error']['error'])
        self.assertEqual(engine.run['attempts']['greet'], 1)

    def crash_controller(self, boundary):
        """Run only Python stubs and kill their Harness controller at a real boundary."""
        script = r'''
import json, os, signal, sys
from pathlib import Path
from harness.adapters import CLIBackend, DemoBackend
from harness.config import Config
from harness.runtime import Runtime
from harness.util import HarnessError, read_json
root, repo, boundary = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
config = Config.load(root / 'harness.toml')
config.data['verification']['commands'] = [[sys.executable, '-m', 'unittest', '-v']]
if boundary == 'before_publication':
    # The child's complete terminal is in process.stdout; nothing derived from it is published.
    def killed(self, job, provider):
        os.kill(os.getpid(), signal.SIGKILL)
    CLIBackend.completed_result = killed
class Stub(DemoBackend):
    def invoke(self, **kwargs):
        if kwargs['role'] != 'worker':
            return super().invoke(**kwargs)
        cli = CLIBackend(config, repo / '.harness')
        terminal = {'type': 'result', 'subtype': 'success', 'is_error': False,
                    'structured_output': {'status': 'completed'}}
        program = ('import json; print(' + repr(json.dumps(terminal)) + ', flush=True)')
        if boundary == 'no_output':
            program = 'import sys; sys.exit(1)'
        if boundary == 'truncated':
            program = 'print(\'{"type":"result","subtype":"succ\', end="", flush=True)'
        cli.command = lambda *args: [sys.executable, '-c', program]
        try:
            return cli.invoke(**kwargs)
        except HarnessError:
            # Killed after the adapter's verdict, before any judgment is saved.
            if boundary in ('no_output', 'truncated'):
                os.kill(os.getpid(), signal.SIGKILL)
            raise
Runtime(config, repo, backend=Stub()).start('Implement greet', read_json(root / 'examples/plan.json'))
'''
        process = subprocess.run([sys.executable, '-c', script, str(ROOT), str(self.repo), boundary],
                                 env={**os.environ, 'PYTHONPATH': str(ROOT)}, capture_output=True, timeout=20)
        self.assertEqual(process.returncode, -signal.SIGKILL, process.stderr.decode())
        path = next((self.repo / '.harness' / 'runs').glob('*/state.json'))
        run = read_json(path)
        self.assertNotIn('worker_recovery', run)
        return run, path.parent

    def test_controller_death_after_no_output_failure_recovers_original_provenance(self):
        stub_provider_clis(self)
        run, directory = self.crash_controller('no_output')
        engine = self.engine()
        # Observe the atomic successor before the next task can erase recovery.
        with patch.object(engine.services.tasks, 'task_attempt', side_effect=HarnessError('stop before new work')):
            with self.assertRaisesRegex(HarnessError, 'stop before new work'):
                engine.resume(run['id'])
        saved = read_json(directory / 'state.json')
        self.assertFalse(saved['worker_receipts'])
        self.assertEqual(saved['worker_recovery']['greet']['dispatch_id'], run['dispatches'][0]['id'])
        self.assertEqual(saved['dispatches'][0]['worker_return_evidence']['state'], 'retryable')
        result = engine.resume(run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts']['greet'], 2)

    def test_success_terminal_survives_sigkill_before_publication(self):
        stub_provider_clis(self)
        run, directory = self.crash_controller('before_publication')
        worker = run['dispatches'][0]
        job = directory / 'dispatches' / worker['id']
        self.assertEqual(read_json(job / 'process.stdout')['type'], 'result')
        self.assertFalse((job / 'response.json').exists())
        self.assertFalse((job / 'provider-result.json').exists())
        engine = self.engine()
        for confirm in ([worker['id']], ()):
            with self.assertRaisesRegex(HarnessError, 'before immutable seal'):
                engine.resume(run['id'], confirm_exited=confirm)
        entry = engine.run['dispatches'][0]
        self.assertEqual((entry['worker_return_evidence']['state'], entry['worker_return_evidence']['reason']),
                         ('returned', 'non-error or ambiguous direct JSON output'))
        self.assertEqual(engine.run['attempts'], {'greet': 1})
        self.assertEqual(list(engine.run['worker_receipts']), ['worker:implement:greet'])
        self.assertEqual(sum(d['role'] == 'worker' for d in engine.run['dispatches']), 1)
        self.assertFalse(engine.run['accepted'])

    def test_truncated_output_is_an_invalid_delivery_and_the_next_attempt_runs(self):
        stub_provider_clis(self)
        config, repo = self.config, self.repo

        class Truncated(DemoBackend):
            workers = 0

            def invoke(self, **kwargs):
                if kwargs['role'] != 'worker':
                    return super().invoke(**kwargs)
                self.workers += 1
                if self.workers > 1:
                    return super().invoke(**kwargs)
                cli = CLIBackend(config, repo / '.harness')
                cli.command = lambda *args: [sys.executable, '-c',
                                             'print(\'{"type":"result","subtype":"succ\', end="", flush=True)']
                return cli.invoke(**kwargs)
        engine = self.engine(Truncated())
        run = engine.start('Implement greet', self.plan())
        self.assertEqual(run['status'], 'completed')
        entry = run['dispatches'][0]
        evidence = entry['worker_return_evidence']
        self.assertEqual((evidence['state'], evidence['reason']),
                         ('invalid', 'worker output is not a decodable object result: JSONDecodeError'))
        self.assertEqual(set(evidence), {'version', 'state', 'reason', 'sources'})
        self.assertEqual(set(evidence['sources']), set(SOURCES))
        stdout = engine.directory / 'dispatches' / entry['id'] / 'process.stdout'
        self.assertEqual(evidence['sources']['process.stdout'], hashlib.sha256(stdout.read_bytes()).hexdigest())
        self.assertEqual((entry['status'], entry['worker_receipt_consumed']), ('invalid', 'result_rejected'))
        self.assertEqual(entry['invalid_output']['artifact'], str(stdout))
        self.assertEqual(run['attempts'], {'greet': 2})
        self.assertEqual(sum(d['role'] == 'worker' for d in run['dispatches']), 2)

    def test_controller_death_after_undecodable_output_reaches_the_same_verdict(self):
        stub_provider_clis(self)
        run, directory = self.crash_controller('truncated')
        worker = run['dispatches'][0]
        self.assertIn('worker:implement:greet', run['worker_receipts'])
        self.assertNotIn('worker_return_evidence', worker)
        engine = self.engine()
        result = engine.resume(run['id'])
        self.assertEqual(result['status'], 'completed')
        entry = result['dispatches'][0]
        self.assertEqual((entry['worker_return_evidence']['state'], entry['worker_return_evidence']['reason']),
                         ('invalid', 'worker output is not a decodable object result: JSONDecodeError'))
        self.assertEqual((entry['status'], entry['worker_receipt_consumed']), ('invalid', 'result_rejected'))
        self.assertEqual(result['attempts'], {'greet': 2})

    def test_explicit_provider_error_allows_counted_retry(self):
        stub_provider_clis(self)
        config, repo = self.config, self.repo
        class ErrorOnce(DemoBackend):
            first = True
            def invoke(self, **kwargs):
                if kwargs['role'] == 'worker' and self.first:
                    self.first = False
                    cli = CLIBackend(config, repo / '.harness')
                    code = 'import json,sys; print(json.dumps({"type":"result","is_error":True,"subtype":"error_during_execution"})); sys.exit(1)'
                    cli.command = lambda *args: [sys.executable, '-c', code]
                    return cli.invoke(**kwargs)
                return super().invoke(**kwargs)
        engine = self.engine(ErrorOnce())
        with self.assertRaisesRegex(HarnessError, 'CLI exit=1'):
            engine.start('Implement greet', self.plan())
        run = engine.resume(engine.run['id'])
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['attempts']['greet'], 2)
        self.assertEqual(run['dispatches'][0]['worker_return_evidence']['state'], 'retryable')
