import errno
import io
import json
import os
import signal
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from harness import command_server as cs
from harness.util import json_bytes, write_json
from test_delete_path import BrokerFixture, ECHO, call


OPERATIONS = ('run_command', 'delete_path')
TARGET = 'legacy/old.txt'
WRITE_ERROR = 'lost receipt write acknowledgement'
UNSAFE_ERROR = WRITE_ERROR + '\x1b\x9b\\\n[harness-json] {}'
RENDERED_ERROR = WRITE_ERROR + r'\x1b\x9b\\'


class ReceiptReadbackTests(BrokerFixture):
    def prepare(self, operation):
        (self.repo / TARGET).write_text('old\n')
        broker = self.broker()
        arguments = {'command_id': cs.command_id(ECHO)} if operation == 'run_command' else {'path': TARGET}
        return broker, arguments

    def assert_next_call(self, broker, admitted):
        folders = set((self.job / 'commands').iterdir())
        next_broker = cs.Broker(broker.path, broker.digest)
        tool_error, result = call(next_broker, 'run_command', {'command_id': cs.command_id(ECHO)})
        if admitted:
            self.assertFalse(tool_error, result)
            self.assertEqual(result['exit_code'], 0)
        else:
            self.assertTrue(tool_error, result)
            self.assertIn('Command evidence incomplete', result['error'])
            self.assertEqual(set((self.job / 'commands').iterdir()), folders)

    def test_missing_receipt_keeps_failure_and_blocks_next_call(self):
        for operation in OPERATIONS:
            with self.subTest(operation=operation):
                broker, arguments = self.prepare(operation)
                attempts = []

                def write(path, value):
                    if path.name != 'receipt.json':
                        return write_json(path, value)
                    attempts.append(path)
                    raise OSError(errno.EIO, WRITE_ERROR)

                stderr = io.StringIO()
                with patch.object(cs, 'write_json', side_effect=write), redirect_stderr(stderr):
                    _, result = call(broker, operation, arguments)
                self.assertEqual(len(attempts), 1)
                self.assertFalse(attempts[0].exists())
                self.assertTrue(result['exit_confirmed'])
                self.assertFalse(result['evidence_complete'])
                self.assertIn(WRITE_ERROR, result['persistence_error'])
                self.assertEqual(stderr.getvalue(), '')
                self.assert_next_call(broker, admitted=False)

    def test_written_receipt_survives_write_error_and_late_signal(self):
        for operation in OPERATIONS:
            for interrupted in (False, True):
                with self.subTest(operation=operation, interrupted=interrupted):
                    broker, arguments = self.prepare(operation)
                    attempts, stdout, stderr = [], io.StringIO(), io.StringIO()

                    def write(path, value):
                        write_json(path, value)
                        if path.name == 'receipt.json':
                            attempts.append((path, json_bytes(value)))
                            if interrupted:
                                os.kill(os.getpid(), signal.SIGINT)
                            raise OSError(errno.EIO, UNSAFE_ERROR)

                    with patch.object(cs, 'write_json', side_effect=write), \
                            redirect_stderr(stderr), redirect_stdout(stdout):
                        if interrupted:
                            with self.assertRaises(KeyboardInterrupt):
                                call(broker, operation, arguments)
                        else:
                            tool_error, result = call(broker, operation, arguments)
                            self.assertFalse(tool_error, result)
                    self.assertEqual(len(attempts), 1)
                    path, prepared = attempts[0]
                    self.assertEqual(path.read_bytes(), prepared)
                    receipt = json.loads(prepared)
                    self.assertTrue(receipt['evidence_complete'] and receipt['exit_confirmed'])
                    self.assertNotIn('persistence_error', receipt)
                    if not interrupted:
                        self.assertEqual(result, receipt)
                    self.assertEqual(stdout.getvalue(), '')
                    line, = stderr.getvalue().splitlines()
                    self.assertTrue(line.startswith('[harness-commands] '))
                    self.assertTrue(line.endswith(RENDERED_ERROR), repr(line))
                    self.assert_next_call(broker, admitted=True)

    def test_unconfirmable_receipts_end_the_broker(self):
        for operation in OPERATIONS:
            for failure in ('different bytes', 'file read failed', 'refused leaf', 'missing parent'):
                with self.subTest(operation=operation, failure=failure):
                    broker, arguments = self.prepare(operation)
                    attempts = []
                    original_open = os.open

                    def open_file(path, *args, **kwargs):
                        if failure == 'file read failed' and path == 'receipt.json':
                            raise OSError(errno.EIO, 'receipt read failed')
                        return original_open(path, *args, **kwargs)

                    def write(path, value):
                        if path.name != 'receipt.json':
                            return write_json(path, value)
                        attempts.append(path)
                        if failure == 'different bytes':
                            # Same JSON value is still a different publication.
                            path.write_bytes(json_bytes(value) + b'\n')
                        elif failure == 'file read failed':
                            write_json(path, value)
                        elif failure == 'refused leaf':
                            path.symlink_to('process.json')
                            os.kill(os.getpid(), signal.SIGINT)
                        elif failure == 'missing parent':
                            path.parent.rename(self.job / path.parent.name)
                        raise OSError(errno.EIO, WRITE_ERROR)

                    with patch.object(cs, 'write_json', side_effect=write), patch('os.open', side_effect=open_file):
                        with self.assertRaises(SystemExit) as exited:
                            call(broker, operation, arguments)
                    self.assertEqual(len(attempts), 1)
                    self.assertIn(WRITE_ERROR, exited.exception.code)
                    self.assertIn(attempts[0].parent.name, exited.exception.code)

    def test_failed_evidence_preparation_does_not_attempt_receipt(self):
        for operation, failure in (('run_command', 'journal write'),
                                   ('run_command', 'output digest'),
                                   ('delete_path', 'journal digest')):
            with self.subTest(operation=operation, failure=failure):
                broker, arguments = self.prepare(operation)
                attempts = []
                original_digest = cs._evidence_digest

                def write(path, value):
                    if path.name == 'receipt.json':
                        attempts.append(path)
                    if failure == 'journal write' and path.name == 'process.json' and value['process_state'] == 'exited':
                        raise OSError(errno.EIO, failure)
                    return write_json(path, value)

                def digest(files, components):
                    if ((failure == 'output digest' and components == ('output.stdout',))
                            or (failure == 'journal digest' and components == ('process.json',))):
                        raise OSError(errno.EIO, failure)
                    return original_digest(files, components)

                with patch.object(cs, 'write_json', side_effect=write), patch.object(cs, '_evidence_digest', side_effect=digest):
                    _, result = call(broker, operation, arguments)
                self.assertEqual(attempts, [])
                self.assertEqual(list((self.job / 'commands').glob('*/receipt.json')), [])
                self.assertFalse(result['evidence_complete'])
                self.assertIn(failure, result['persistence_error'])
                self.assert_next_call(broker, admitted=False)

    def test_unconfirmed_publication_exits_stdio_without_response(self):
        wrapper = f"payload = {UNSAFE_ERROR!r}\n" + """
import runpy, sys
from harness import command_server as cs

original = cs.write_json
def write(path, value):
    original(path, value)
    if path.name == 'receipt.json':
        with path.open('ab') as output:
            output.write(b'\\n')
        raise OSError(payload)

cs.write_json = write
sys.argv.pop(0)
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        entry = Path(cs.__file__).with_name('command_entry.py')
        for operation in OPERATIONS:
            with self.subTest(operation=operation):
                broker, arguments = self.prepare(operation)
                argv = ['--manifest', str(broker.path), '--sha256', broker.digest]
                requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                             'params': {'name': operation, 'arguments': arguments}},
                            {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}]
                process = subprocess.run([sys.executable, '-c', wrapper, str(entry), *argv],
                                         input=''.join(json.dumps(request) + '\n' for request in requests),
                                         text=True, capture_output=True, timeout=10)
                self.assertEqual(process.returncode, 1, process.stderr)
                self.assertEqual(process.stdout, '')
                receipts = list((self.job / 'commands').glob('*/receipt.json'))
                self.assertEqual(len(receipts), 1)
                receipt = json.loads(receipts[0].read_bytes())
                line, = process.stderr.splitlines()
                self.assertTrue(line.startswith('Command receipt publication could not be confirmed: '))
                self.assertTrue(line.endswith(f"{receipt['call_id']}: {RENDERED_ERROR}"), repr(line))
                self.assert_next_call(broker, admitted=True)
