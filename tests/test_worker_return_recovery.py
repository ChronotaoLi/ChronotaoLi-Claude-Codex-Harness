"""Exited-worker recovery uses durable transport facts; every model is offline."""
import json
from copy import deepcopy
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from harness.adapters import DemoBackend
from harness.util import HarnessError, ProcessTimeout, read_json, write_json
from test_harness import RepoCase, ROOT
import test_worker_receipts as receipt_tests
import test_terminal_capture_recovery as terminal_tests


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
        with self.assertRaisesRegex(HarnessError, '不一致'):
            engine.start('Implement greet', self.plan())
        for attempt in (2, 3):
            with self.assertRaisesRegex(HarnessError, '不一致'):
                engine.resume(engine.run['id'])
            self.assertEqual(engine.run['attempts']['greet'], attempt)
            self.assertFalse(engine.run['worker_receipts'])
            self.assertFalse(engine.run['accepted'])
        with self.assertRaisesRegex(HarnessError, '尝试次数上限'):
            engine.resume(engine.run['id'])
        workers = [d for d in engine.run['dispatches'] if d['role'] == 'worker']
        self.assertEqual(len(workers), 3)
        self.assertTrue(all(d['worker_receipt_consumed'] == 'result_rejected' for d in workers))
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
        for mode in ('legacy_json', 'stream_metadata_v1'):
            with self.subTest(mode=mode):
                if mode != 'legacy_json':
                    self.setUp()
                self.config.data['runtime']['claude_output_mode'] = mode
                backend = terminal_tests.LingeringCLI(self.config, self.repo, 'implement',
                                                     no_terminal=True, exit_code=1)
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

    def crash_controller(self, mode, boundary):
        """Run only Python stubs and kill their Harness controller at a real boundary."""
        self.config.data['runtime']['claude_output_mode'] = mode
        script = r'''
import json, os, signal, sys
from pathlib import Path
from harness.adapters import CLIBackend, DemoBackend
from harness.config import Config
from harness.runtime import Runtime
from harness.util import HarnessError, read_json
import harness.stream_capture as capture
from harness.process_runtime import ProcessSupervisor
root, repo, mode, boundary = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
config = Config.load(root / 'harness.toml')
config.data['runtime']['claude_output_mode'] = mode
config.data['verification']['commands'] = [[sys.executable, '-m', 'unittest', '-v']]
publish = capture._publish_terminal
write = capture.write_json
def interrupted_publish(path, value, limit):
    publish(path, value, limit)
    if boundary == 'received' and path.name == 'stream-received-terminal.json':
        os.kill(os.getpid(), signal.SIGKILL)
def interrupted_write(path, value):
    write(path, value)
    if boundary == 'completed_capture' and path.name == 'stream-capture.json':
        os.kill(os.getpid(), signal.SIGKILL)
capture._publish_terminal = interrupted_publish
capture.write_json = interrupted_write
started = ProcessSupervisor.started
def interrupted_start(self, process):
    started(self, process)
    if boundary == 'reader_crash':
        os.kill(os.getpid(), signal.SIGKILL)
ProcessSupervisor.started = interrupted_start
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
        cli.command = lambda *args: [sys.executable, '-c', program]
        try:
            return cli.invoke(**kwargs)
        except HarnessError:
            if boundary == 'no_output':
                os.kill(os.getpid(), signal.SIGKILL)
            raise
Runtime(config, repo, backend=Stub()).start('Implement greet', read_json(root / 'examples/plan.json'))
'''
        process = subprocess.run([sys.executable, '-c', script, str(ROOT), str(self.repo), mode, boundary],
                                 env={**os.environ, 'PYTHONPATH': str(ROOT)}, capture_output=True, timeout=20)
        self.assertEqual(process.returncode, -signal.SIGKILL, process.stderr.decode())
        path = next((self.repo / '.harness' / 'runs').glob('*/state.json'))
        run = read_json(path)
        self.assertNotIn('worker_recovery', run)
        return run, path.parent

    def test_controller_death_after_no_output_failure_recovers_original_provenance(self):
        for mode in ('legacy_json', 'stream_metadata_v1'):
            with self.subTest(mode=mode):
                if mode != 'legacy_json':
                    self.setUp()
                run, directory = self.crash_controller(mode, 'no_output')
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

    def test_stream_success_survives_sigkill_before_terminal_publication(self):
        for boundary in ('received', 'completed_capture'):
            with self.subTest(boundary=boundary):
                if boundary != 'received':
                    self.setUp()
                run, directory = self.crash_controller('stream_metadata_v1', boundary)
                worker = run['dispatches'][0]
                job = directory / 'dispatches' / worker['id']
                self.assertFalse((job / 'stream-terminal.json').exists())
                self.assertFalse((job / 'returned-result.json').exists())
                self.assertEqual(read_json(job / 'stream-received-terminal.json')['terminal']['subtype'], 'success')
                engine = self.engine()
                with self.assertRaisesRegex(HarnessError, 'before immutable seal'):
                    engine.resume(run['id'], confirm_exited=[worker['id']])
                self.assertEqual(engine.run['attempts']['greet'], 1)
                self.assertEqual(engine.run['dispatches'][0]['worker_return_evidence']['state'], 'returned')
                self.assertFalse(engine.run['accepted'])

    def test_stream_reader_death_without_capture_remains_unknown_after_confirmation(self):
        run, directory = self.crash_controller('stream_metadata_v1', 'reader_crash')
        worker = run['dispatches'][0]
        job = directory / 'dispatches' / worker['id']
        self.assertFalse((job / 'stream-terminal.json').exists())
        self.assertFalse((job / 'stream-capture.json').exists())
        self.assertTrue(read_json(job / 'process.outcome.json')['started'])
        engine = self.engine()
        with self.assertRaisesRegex(HarnessError, 'transport completion'):
            engine.resume(run['id'], confirm_exited=[worker['id']])
        self.assertEqual(engine.run['dispatches'][0]['worker_return_evidence']['state'], 'unknown')
        self.assertEqual(engine.run['attempts']['greet'], 1)
        self.assertTrue(engine.run['worker_receipts'])
        self.assertFalse(engine.run['accepted'])

    def test_explicit_stream_provider_error_allows_counted_retry(self):
        from harness.adapters import CLIBackend
        self.config.data['runtime']['claude_output_mode'] = 'stream_metadata_v1'
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
