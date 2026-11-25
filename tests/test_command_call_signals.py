import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import command_server as cs
from harness.process_runtime import ProcessSupervisor
from harness.util import HarnessError, ProcessTimeout, read_json, write_json
from test_delete_path import BrokerFixture, ECHO, receipt_of


def send():
    os.kill(os.getpid(), signal.SIGINT)


def handlers():
    return {sig: signal.getsignal(sig) for sig in cs.SIGNALS}


class CommandCallSignalsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def broker(self, source='print("done")', timeout=4):
        command = {'cwd': '.', 'argv': [sys.executable, '-c', source]}
        config = {'verification': {'commands': [command, ECHO]}, 'permissions': {'worker_commands': []},
                  'runtime': {'verify_timeout_seconds': timeout, 'agent_timeout_seconds': 15}}
        self.job = self.root / ('worker-' + str(len(list(self.root.iterdir()))))
        self.manifest = cs.create_manifest(config, self.root, self.job)
        return cs.Broker(self.manifest['artifact'], self.manifest['artifact_digest']), self.arguments(command)

    def arguments(self, command):
        return {'command_id': cs.command_id(command)}

    def assert_next_admitted(self, broker):
        self.assertEqual(broker.run_command(self.arguments(ECHO))['exit_code'], 0)

    def assert_receipt(self, writes, before, reason, result=None):
        self.assertEqual(handlers(), before)
        self.assertFalse(any(path.name == 'output.outcome.json' for path in writes))
        receipts = [path for path in writes if path.name == 'receipt.json']
        self.assertEqual(len(receipts), 1)
        receipt = read_json(receipts[0])
        self.assertTrue(receipt['evidence_complete'])
        self.assertTrue(receipt['exit_confirmed'])
        self.assertEqual(receipt['termination_reason'], reason)
        self.assertEqual(read_json(receipts[0].with_name('output.outcome.json')), receipt['lifecycle'])
        if result is not None:
            self.assertEqual(result, receipt)
        return receipt

    def test_admission_signal_beats_success_or_ordinary_refusal(self):
        for refused, sig in ((False, signal.SIGTERM), (True, signal.SIGHUP)):
            with self.subTest(refused=refused):
                broker, arguments = self.broker()
                original = broker._prior_evidence
                refusal = HarnessError('injected refusal')
                before = handlers()

                def prior(manifest):
                    original(manifest)
                    os.kill(os.getpid(), sig)
                    if refused:
                        raise refusal

                with patch.object(broker, '_prior_evidence', side_effect=prior), \
                        patch.object(cs, 'write_json', wraps=write_json) as writer, \
                        self.assertRaises(KeyboardInterrupt) as interrupted:
                    broker.run_command(arguments)
                self.assertEqual(handlers(), before)
                writer.assert_not_called()
                self.assertEqual(list((self.job / 'commands').iterdir()), [])
                self.assertIs(interrupted.exception.__cause__, refusal if refused else None)
                self.assert_next_admitted(broker)

    def test_signal_after_journal_cancels_before_launch(self):
        marker = self.root / 'started'
        broker, arguments = self.broker(f'from pathlib import Path; Path({str(marker)!r}).touch()')
        before, writes = handlers(), []
        publish = broker._publish_journal

        def journal(*args, **kwargs):
            result = publish(*args, **kwargs)
            send()
            return result

        def write(path, value):
            writes.append(path)
            write_json(path, value)

        with patch.object(broker, '_publish_journal', side_effect=journal), \
                patch.object(cs, 'write_json', side_effect=write):
            result = broker.run_command(arguments)
        self.assert_receipt(writes, before, 'cancelled', result)
        self.assertFalse(marker.exists())
        self.assertFalse(result['lifecycle']['started'])
        self.assertEqual(result['lifecycle']['startup_state'], 'not_started')
        self.assert_next_admitted(broker)

    def test_waiting_signal_before_deadline_cancels_during_checkpoint(self):
        self.check_wait_or_cleanup('wait', 'cancelled')

    def test_waiting_signal_in_last_slice_preserves_timeout(self):
        self.check_wait_or_cleanup('last slice', 'timeout')

    def test_timeout_cleanup_signal_preserves_timeout(self):
        self.check_wait_or_cleanup('timeout cleanup', 'timeout')

    def test_normal_cleanup_signal_remains_supervisor_cancellation(self):
        self.check_wait_or_cleanup('normal cleanup', 'cancelled')

    def check_wait_or_cleanup(self, phase, reason):
        during_cleanup = phase.endswith('cleanup')
        source = 'pass' if phase == 'normal cleanup' else 'import time; time.sleep(3)'
        timeout = .28 if phase in ('last slice', 'timeout cleanup') else 4
        broker, arguments = self.broker(source, timeout)
        before, writes = handlers(), []
        original_wait, original_finish = subprocess.Popen.wait, ProcessSupervisor._finish
        injected = False

        def wait(process, timeout=None):
            nonlocal injected
            if not during_cleanup and not injected and timeout is not None and (
                    phase == 'wait' or timeout < .1):
                injected = True
                send()
            return original_wait(process, timeout=timeout)

        def finish(supervisor, primary=None):
            nonlocal injected
            if during_cleanup:
                if phase == 'timeout cleanup':
                    self.assertIsInstance(primary, ProcessTimeout)
                else:
                    self.assertIsNone(primary)
                injected = True
                send()
            return original_finish(supervisor, primary)

        def write(path, value):
            writes.append(path)
            write_json(path, value)

        with patch.object(subprocess.Popen, 'wait', wait), patch.object(ProcessSupervisor, '_finish', finish), \
                patch.object(cs, 'write_json', side_effect=write):
            if phase == 'last slice':
                with self.assertRaises(KeyboardInterrupt):
                    broker.run_command(arguments)
                result = None
            else:
                result = broker.run_command(arguments)
        self.assertTrue(injected)
        receipt = self.assert_receipt(writes, before, reason, result)
        self.assertTrue(receipt['lifecycle']['started'])
        self.assertIs(receipt['timed_out'], reason == 'timeout')
        if reason == 'timeout':
            execution_error = receipt['lifecycle']['errors'][0]
            self.assertEqual(execution_error['type'], 'ProcessTimeout')
            self.assertEqual(receipt['error'], execution_error['message'])
        self.assert_next_admitted(broker)

    def test_terminal_journal_signal_keeps_normal_receipt_inside_or_outside_except(self):
        for in_except in (False, True):
            with self.subTest(in_except=in_except):
                broker, arguments = self.broker()
                before, writes = handlers(), []

                def write(path, value):
                    writes.append(path)
                    if path.name == 'process.json' and value['process_state'] == 'exited':
                        send()
                    write_json(path, value)

                with patch.object(cs, 'write_json', side_effect=write), self.assertRaises(KeyboardInterrupt):
                    if in_except:
                        try:
                            raise RuntimeError('unrelated')
                        except RuntimeError:
                            broker.run_command(arguments)
                    else:
                        broker.run_command(arguments)
                result = self.assert_receipt(writes, before, 'normal')
                self.assertEqual(result['stdout'], 'done\n')
                self.assertIsNone(result['error'])
                self.assert_next_admitted(broker)

    def test_late_signal_leaves_no_stdio_response_and_restart_reads_receipt(self):
        _, arguments = self.broker()
        entry = Path(cs.__file__).with_name('command_entry.py')
        argv = ['--manifest', str(self.manifest['artifact']), '--sha256', self.manifest['artifact_digest']]
        request = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                              'params': {'name': 'run_command', 'arguments': arguments}}) + '\n'
        wrapper = """
import os, runpy, signal, sys
from harness import command_server as cs

original = cs.write_json
def write(path, value):
    original(path, value)
    if path.name == 'receipt.json':
        os.kill(os.getpid(), signal.SIGINT)

cs.write_json = write
sys.argv.pop(0)
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        first = subprocess.run([sys.executable, '-c', wrapper, str(entry), *argv],
                               input=request, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, '')
        self.assertIn('KeyboardInterrupt', first.stderr)
        paths = list((self.job / 'commands').glob('*/receipt.json'))
        self.assertEqual(len(paths), 1)
        receipt = read_json(paths[0])
        self.assertTrue(receipt['evidence_complete'])
        self.assertEqual(receipt['termination_reason'], 'normal')
        restart = subprocess.run([sys.executable, str(entry), *argv], input=request,
                                 text=True, capture_output=True, timeout=10)
        self.assertEqual(restart.returncode, 0, restart.stderr)
        payload = json.loads(restart.stdout)['result']
        self.assertFalse(payload['isError'], payload)
        self.assertEqual(json.loads(payload['content'][0]['text'])['exit_code'], 0)


class DeleteCallSignalsTests(BrokerFixture):
    def test_admission_signal_keeps_target_and_publishes_nothing(self):
        broker = self.broker()
        prior = broker._prior_evidence
        before = handlers()

        def check(manifest):
            prior(manifest)
            send()

        arguments = {'path': 'legacy/old.txt'}
        with patch.object(broker, '_prior_evidence', side_effect=check), \
                patch.object(cs, 'write_json', wraps=write_json) as writer, self.assertRaises(KeyboardInterrupt):
            broker.delete_path(arguments)
        self.assertEqual(handlers(), before)
        writer.assert_not_called()
        self.assertTrue((self.repo / arguments['path']).is_file())
        self.assertEqual(list((self.job / 'commands').iterdir()), [])
        self.assertEqual(broker.delete_path(arguments)['outcome'], 'deleted')

    def test_signal_during_removal_is_late_even_in_except(self):
        broker = self.broker()
        writes, before = [], handlers()

        def remove(original, name, dir_fd):
            send()
            return original(name, dir_fd)

        def write(path, value):
            writes.append(path)
            write_json(path, value)

        with self.interpose('_unlinkat', remove), patch.object(cs, 'write_json', side_effect=write), \
                self.assertRaises(KeyboardInterrupt):
            try:
                raise RuntimeError('unrelated')
            except RuntimeError:
                broker.delete_path({'path': 'legacy/old.txt'})

        self.assertEqual(handlers(), before)
        self.assertFalse(any(path.name == 'output.outcome.json' for path in writes))
        receipts = [path for path in writes if path.name == 'receipt.json']
        self.assertEqual(len(receipts), 1)
        receipt = read_json(receipts[0])
        self.assertTrue(receipt['evidence_complete'])
        self.assertFalse((self.repo / 'legacy/old.txt').exists())
        self.assertEqual(receipt['outcome'], 'deleted')
        self.assert_journal_digest(receipts[0].parent, receipt)
        self.assert_followups(broker)

    def test_complete_delete_response_matches_receipt_and_admits_both_operations(self):
        broker = self.broker()
        result = broker.delete_path({'path': 'legacy/old.txt'})
        self.assertTrue(result['evidence_complete'])
        self.assertEqual(result, receipt_of(self.job, result))
        self.assert_journal_digest(Path(result['intent_path']).parent, result)
        self.assert_followups(broker)

    def assert_journal_digest(self, folder, result):
        self.assertEqual(result['journal_sha256'], hashlib.sha256((folder / 'process.json').read_bytes()).hexdigest())

    def assert_followups(self, broker):
        self.assertEqual(broker.run_command({'command_id': cs.command_id(ECHO)})['exit_code'], 0)
        self.assertEqual(broker.delete_path({'path': 'legacy/tool.sh'})['outcome'], 'deleted')
