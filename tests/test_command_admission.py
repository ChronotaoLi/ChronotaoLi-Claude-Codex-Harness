import multiprocessing
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.command_server import Broker, command_id, create_manifest
from harness.util import HarnessError, write_json


def invoke(path, digest, identifier, queue):
    try:
        queue.put(Broker(path, digest).run_command({'command_id': identifier}))
    except HarnessError as error:
        queue.put({'blocked': str(error)})


class CommandAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.job = self.root / 'worker'
        self.argv = [sys.executable, '-c', 'import time;time.sleep(.3);print("done")']
        config = {'verification': {'commands': [self.argv]}, 'permissions': {'worker_commands': []},
                  'runtime': {'verify_timeout_seconds': 2, 'agent_timeout_seconds': 10}}
        self.meta = create_manifest(config, self.root, self.job)
        self.broker = Broker(self.meta['artifact'], self.meta['artifact_digest'])

    def journal(self, letter, state='starting', confirmed=False):
        folder = self.job / 'commands' / (letter * 32)
        folder.mkdir()
        write_json(folder / 'process.json', {'id': 'worker:command:' + folder.name,
                   'command_id': command_id(self.argv), 'pgid': None,
                   'process_state': state, 'exit_confirmed': confirmed})
        return folder

    def test_all_prior_journals_checked(self):
        self.journal('a')
        self.journal('b', 'prepared')
        with self.assertRaises(HarnessError):
            self.broker.run_command({'command_id': command_id(self.argv)})
        self.assertEqual(len(list((self.job / 'commands').iterdir())), 2)

    def test_missing_receipt_blocks_even_confirmed_exit(self):
        self.journal('a', 'exited', True)
        with self.assertRaisesRegex(HarnessError, 'evidence incomplete'):
            self.broker.run_command({'command_id': command_id(self.argv)})

    def test_prepared_crash_did_not_start(self):
        self.journal('a', 'prepared')
        result = self.broker.run_command({'command_id': command_id(self.argv)})
        self.assertEqual(result['exit_code'], 0)

    def test_short_lived_descendant_drain_allows_following_commands(self):
        argv = [sys.executable, '-c', 'import subprocess,sys; '
                'subprocess.Popen([sys.executable,"-c","import time;time.sleep(.1)"],'
                'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);print("done")']
        config = {'verification': {'commands': [argv]}, 'permissions': {'worker_commands': []},
                  'runtime': {'verify_timeout_seconds': 2, 'agent_timeout_seconds': 10}}
        meta = create_manifest(config, self.root, self.root / 'short-child-worker')
        broker = Broker(meta['artifact'], meta['artifact_digest'])
        for _ in range(3):
            result = broker.run_command({'command_id': command_id(argv)})
            self.assertEqual(result['exit_code'], 0)
            self.assertTrue(result['exit_confirmed'])
            self.assertTrue(result['evidence_complete'])

    def test_parent_budget_rejects_without_intent(self):
        with patch('harness.command_server.time.monotonic', return_value=self.broker.manifest['parent_deadline']):
            with self.assertRaisesRegex(HarnessError, 'budget exhausted'):
                self.broker.run_command({'command_id': command_id(self.argv)})
        self.assertEqual(list((self.job / 'commands').iterdir()), [])

    def test_intent_write_failure_never_starts_or_publishes(self):
        with patch('harness.command_server.write_json', side_effect=OSError('disk full')), patch('harness.command_server.run_process') as runner:
            with self.assertRaises(OSError):
                self.broker.run_command({'command_id': command_id(self.argv)})
            runner.assert_not_called()
        # No journal-less folder was published, so the next call is admitted.
        self.assertEqual(list((self.job / 'commands').iterdir()), [])
        self.assertEqual(self.broker.run_command({'command_id': command_id(self.argv)})['exit_code'], 0)

    def test_receipt_write_failure_blocks_next_call(self):
        original = write_json
        def fail_receipt(path, value):
            if Path(path).name == 'receipt.json':
                raise OSError('receipt disk full')
            return original(path, value)
        with patch('harness.command_server.write_json', side_effect=fail_receipt):
            result = self.broker.run_command({'command_id': command_id(self.argv)})
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(result['evidence_complete'])
        with self.assertRaisesRegex(HarnessError, 'evidence incomplete'):
            self.broker.run_command({'command_id': command_id(self.argv)})

    def test_output_tampering_blocks_next_call(self):
        result = self.broker.run_command({'command_id': command_id(self.argv)})
        Path(result['stdout_path']).write_text('changed')
        with self.assertRaisesRegex(HarnessError, 'evidence incomplete'):
            self.broker.run_command({'command_id': command_id(self.argv)})

    def test_frozen_parent_bounds_effective_command_timeout(self):
        deadline = self.broker.manifest['parent_deadline']
        with patch('harness.command_server.time.monotonic', return_value=deadline - 2.1):
            _, _, timeout, _, _ = self.broker._admit({'command_id': command_id(self.argv)})
        self.assertAlmostEqual(timeout, .1)

    def test_cleanup_uncertainty_preserves_wait_code_and_blocks(self):
        from harness.process_runtime import ProcessOutcome
        facts = ProcessOutcome(started=True, startup_state='started', exit_code=0,
                               exit_confirmed=False, evidence_complete=False,
                               cleanup_errors=[{'step': 'observe_group', 'errno': 1}])
        def runner(argv, cwd, log, **kwargs):
            log.with_suffix('.stdout').write_text('done')
            log.with_suffix('.stderr').write_text('')
            kwargs['on_start'](123)
            kwargs['on_outcome'](facts)
            error = HarnessError('EPERM')
            error.outcome = facts
            raise error
        with patch('harness.command_server.run_process', side_effect=runner):
            result = self.broker.run_command({'command_id': command_id(self.argv)})
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['termination_reason'], 'normal')
        self.assertFalse(result['exit_confirmed'])
        with patch('harness.command_server.group_alive', return_value=False):
            with self.assertRaises(HarnessError):
                self.broker.run_command({'command_id': command_id(self.argv)})

    def test_two_process_callers_cannot_overlap(self):
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        args = (self.meta['artifact'], self.meta['artifact_digest'], command_id(self.argv), queue)
        first = context.Process(target=invoke, args=args)
        first.start()
        until = time.monotonic() + 3
        while not list((self.job / 'commands').iterdir()) and time.monotonic() < until:
            time.sleep(.005)
        second = context.Process(target=invoke, args=args)
        second.start()
        first.join(5)
        second.join(5)
        results = [queue.get(timeout=2), queue.get(timeout=2)]
        self.assertEqual(sum('blocked' in result for result in results), 1, results)
        self.assertEqual(sum(result.get('exit_code') == 0 for result in results), 1, results)

    def test_slow_admission_intent_exhausts_parent_budget_without_start(self):
        witness = self.root / 'started'
        argv = [sys.executable, '-c', f'open({str(witness)!r},"w").write("started")']
        config = {'verification': {'commands': [argv]}, 'permissions': {'worker_commands': []},
                  'runtime': {'verify_timeout_seconds': 2, 'agent_timeout_seconds': 10}}
        meta = create_manifest(config, self.root, self.root / 'slow', parent_deadline=time.monotonic() + 2.1)
        broker = Broker(meta['artifact'], meta['artifact_digest'])
        def slow(path, value):
            if Path(path).name == 'process.json' and value['process_state'] == 'starting':
                time.sleep(.2)
            write_json(path, value)
        with patch('harness.command_server.write_json', side_effect=slow):
            result = broker.run_command({'command_id': command_id(argv)})
        self.assertFalse(witness.exists())
        self.assertTrue(result['timed_out'])
        self.assertFalse(result['lifecycle']['started'])
        self.assertTrue(result['exit_confirmed'])

    def test_broker_late_signal_restores_handlers_and_retains_committed_lifecycle(self):
        import os
        import signal
        original = signal.signal
        previous = {sig: signal.getsignal(sig) for sig in
                    (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        injected = False
        def handoff(sig, handler):
            nonlocal injected
            result = original(sig, handler)
            # The inner supervisor restores the broker handler before receipt
            # creation; inject only at the broker's later cleanup handoff.
            receipts = list((self.job / 'commands').glob('*/receipt.json'))
            if (sig == signal.SIGINT and getattr(handler, '__name__', '') == 'interrupted'
                    and receipts and not injected):
                injected = True
                os.kill(os.getpid(), signal.SIGINT)
            return result
        with patch('harness.process_runtime.signal.signal', side_effect=handoff):
            result = self.broker.run_command({'command_id': command_id(self.argv)})
        self.assertTrue(injected)
        self.assertTrue(result['response_cancelled'])
        self.assertTrue(result['error'])
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['lifecycle']['termination_reason'], 'normal')
        for sig, handler in previous.items():
            self.assertIs(signal.getsignal(sig), handler)
        from harness.util import read_json
        receipt = next((self.job / 'commands').glob('*/receipt.json'))
        self.assertTrue(read_json(receipt)['response_cancelled'])

    def test_receipt_real_interrupt_records_cancellation_and_completed_wait(self):
        import os
        import signal
        argv = self.argv
        original = write_json
        def interrupt(path, value):
            if Path(path).name == 'receipt.json':
                os.kill(os.getpid(), signal.SIGINT)
            original(path, value)
        with patch('harness.command_server.write_json', side_effect=interrupt):
            result = self.broker.run_command({'command_id': command_id(argv)})
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(result['evidence_complete'])
        self.assertEqual(result['termination_reason'], 'cancelled')
        self.assertTrue(result['error'])
        from harness.util import read_json
        receipt = next((self.job / 'commands').glob('*/receipt.json'))
        self.assertEqual(read_json(receipt)['termination_reason'], 'cancelled')
        self.assertEqual(read_json(receipt.parent / 'output.outcome.json')['termination_reason'], 'cancelled')

    def test_persistence_fault_keeps_execution_failure(self):
        from harness.util import ProcessTimeout
        def timeout_runner(argv, cwd, log, **kwargs):
            log.with_suffix('.stdout').write_text('')
            log.with_suffix('.stderr').write_text('')
            raise ProcessTimeout(.1, log)
        def fail(path, value):
            if Path(path).name == 'receipt.json':
                raise KeyboardInterrupt('injected persistence interrupt')
            write_json(path, value)
        with patch('harness.command_server.run_process', side_effect=timeout_runner), \
                patch('harness.command_server.write_json', side_effect=fail):
            result = self.broker.run_command({'command_id': command_id(self.argv)})
        self.assertTrue(result['timed_out'])
        self.assertIn('injected persistence interrupt', result['persistence_error'])
        self.assertNotIn('injected persistence interrupt', result['error'])
        self.assertFalse(result['evidence_complete'])
