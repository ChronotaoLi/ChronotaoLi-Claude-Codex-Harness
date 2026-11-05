import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.stream_capture import (run_stream_process, freeze_capture, claude_result_path,
                                    permission_denial_projection)
from harness.adapters import provider_usage, cli_error_summary
from harness.util import HarnessError, ProcessTimeout, read_json


class StreamCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.job = Path(self.temp.name)

    def run_cli(self, program, **options):
        return run_stream_process([sys.executable, '-c', program], self.job,
                                  self.job / 'process', timeout=3, **options)

    def test_real_sigint_after_popen_creation_retains_ownership_and_cleans_child(self):
        original = subprocess.Popen
        children = []
        def interrupted(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            os.kill(os.getpid(), signal.SIGINT)
            return child
        try:
            with patch('harness.stream_capture.subprocess.Popen', side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    self.run_cli('import time; time.sleep(30)')
            facts = caught.exception.outcome
            self.assertTrue(facts.started)
            self.assertEqual(facts.pid, children[0].pid)
            self.assertEqual(facts.termination_reason, 'cancelled')
            self.assertTrue(facts.exit_confirmed)
            self.assertIsNotNone(children[0].poll())
            saved = read_json(self.job / 'process.outcome.json')
            self.assertTrue(saved['started'])
            self.assertTrue(saved['exit_confirmed'])
            self.assertEqual(saved['termination_reason'], 'cancelled')
        finally:
            for child in children:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()

    def test_tool_metadata_filters_bodies_and_preserves_terminal_and_usage(self):
        freeze_capture(self.job, 'stream_metadata_v1', 'synthetic')
        records = [
            {'type': 'assistant', 'message': {'content': [
                {'type': 'thinking', 'thinking': 'SECRET_THOUGHT'},
                {'type': 'text', 'text': 'SECRET_TEXT'},
                {'type': 'tool_use', 'id': 'call_1', 'name': 'Read', 'input': {'path': 'SECRET_ARG'}}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'call_1', 'content': 'SECRET_BODY'}]}},
            {'type': 'result', 'subtype': 'success', 'structured_output': {'ok': True},
             'total_cost_usd': .5, 'num_turns': 1, 'usage': {'input_tokens': 2}}]
        self.assertEqual(self.run_cli('import sys; sys.stdout.write(' + repr(
            ''.join(json.dumps(r) + '\n' for r in records)) + ')'), 0)
        metadata = (self.job / 'stream-metadata.jsonl').read_text()
        self.assertNotIn('SECRET', metadata)
        self.assertIn('tool_start', metadata)
        self.assertIn('tool_end', metadata)
        self.assertEqual(read_json(claude_result_path(self.job)), records[-1])
        self.assertEqual(provider_usage(self.job, 'claude')['reported_cost_usd'], .5)
        self.assertFalse((self.job / 'process.stdout').exists())

    def test_protocol_tail_cannot_be_hidden_by_metadata_limit(self):
        for tail in ('{bad}\n', '{"type":"result"}\n', '{"type":"result"}'):
            with self.subTest(tail=tail):
                with self.assertRaises(HarnessError):
                    self.run_cli('import sys; sys.stdout.write(' + repr(
                        '{"type":"result"}\n' + tail) + ')', metadata_limit=0)
                self.assertFalse((self.job / 'stream-terminal.json').exists())
                (self.job / 'stream-metadata.jsonl').unlink()

    def test_diagnostic_drop_is_advisory_and_stderr_is_drained_privately(self):
        self.assertEqual(self.run_cli(
            'import sys; sys.stderr.write("SECRET" * 100000); '
            'print("{\\"type\\":\\"result\\",\\"subtype\\":\\"success\\"}")', metadata_limit=0), 0)
        capture = read_json(self.job / 'stream-capture.json')
        self.assertTrue(capture['diagnostics_incomplete'])
        self.assertTrue(capture['protocol_complete'])
        self.assertFalse((self.job / 'process.stderr').exists())

    def test_nonzero_after_stdout_eof_stays_nonzero(self):
        self.assertEqual(self.run_cli(
            'import os,sys,time; print("{\\"type\\":\\"result\\"}",flush=True); '
            'os.close(1); time.sleep(.05); sys.exit(7)'), 7)

    def test_large_transport_record_invalidates_even_after_terminal(self):
        with self.assertRaises(HarnessError):
            self.run_cli('print("{\\"type\\":\\"result\\"}"); print("x"*1000)', record_limit=100)
        self.assertFalse((self.job / 'stream-terminal.json').exists())

    def test_hard_timeout_does_not_extend_for_activity(self):
        started = time.monotonic()
        with self.assertRaises(ProcessTimeout):
            run_stream_process([sys.executable, '-c',
                'import time;\nwhile True: print("{\\"type\\":\\"system\\"}",flush=True); time.sleep(.01)'],
                self.job, self.job / 'process', timeout=.15)
        self.assertLess(time.monotonic() - started, 2)

    def test_complete_large_backlog_is_processed_before_result(self):
        self.assertEqual(self.run_cli(
            'print("{\\"type\\":\\"system\\"}\\n" * 10000,end=""); '
            'print("{\\"type\\":\\"result\\"}")', metadata_limit=0), 0)
        self.assertEqual(read_json(self.job / 'stream-capture.json')['terminal_count'], 1)

    def test_metadata_storage_failure_is_advisory_but_does_not_hide_bad_tail(self):
        original = Path.open
        def failing_open(path, *args, **kwargs):
            if path.name == 'stream-metadata.jsonl':
                raise OSError('synthetic disk failure')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', failing_open):
            self.assertEqual(self.run_cli('print("{\\"type\\":\\"result\\"}")'), 0)
        self.assertTrue(read_json(self.job / 'stream-capture.json')['diagnostics_incomplete'])
        (self.job / 'stream-terminal.json').unlink()
        with patch.object(Path, 'open', failing_open):
            with self.assertRaises(HarnessError):
                self.run_cli('print("{\\"type\\":\\"result\\"}"); print("{bad}")')

    def test_blocked_metadata_writer_cannot_return_success(self):
        original = Path.open
        release = threading.Event()
        timer = threading.Timer(2.2, release.set)
        self.addCleanup(timer.cancel)
        # A rejected, deliberately blocked daemon can outlive the bounded API.
        # Release and join the fixture before deleting its output directory.
        def join_fixture_writer():
            for worker in threading.enumerate():
                if worker.name == "harness-metadata-writer":
                    worker.join(1)
        self.addCleanup(join_fixture_writer)
        self.addCleanup(release.set)
        def slow_open(path, *args, **kwargs):
            if path.name == 'stream-metadata.jsonl':
                release.wait(3)
            return original(path, *args, **kwargs)
        timer.start()
        with patch.object(Path, 'open', slow_open):
            with self.assertRaisesRegex(HarnessError, 'writer did not converge'):
                self.run_cli('print("{\\"type\\":\\"result\\"}")')
        self.assertFalse((self.job / 'stream-terminal.json').exists())

    def test_nested_terminal_persists_exact_bounded_compact_bytes(self):
        nested = 'leaf'
        for _ in range(100):
            nested = {'x': nested}
        candidate = {'type': 'result', 'structured_output': {'nested': nested}}
        wire = json.dumps(candidate, separators=(',', ':')) + '\n'
        self.assertLess(len(wire.encode()), 1024)
        self.assertGreater(len(json.dumps(candidate, indent=2).encode()), 1024)
        self.assertEqual(self.run_cli('import sys; sys.stdout.write(' + repr(wire) + ')',
                                     terminal_limit=1024), 0)
        raw = (self.job / 'stream-terminal.json').read_bytes()
        self.assertLessEqual(len(raw), 1024)
        self.assertTrue(raw.endswith(b'\n'))
        self.assertEqual(json.loads(raw), candidate)
        self.assertEqual(raw, (json.dumps(candidate, ensure_ascii=True,
                                         separators=(',', ':')) + '\n').encode())

    def test_unicode_serialization_expansion_cannot_exceed_artifact_limit(self):
        candidate = {'type': 'result', 'structured_output': {'text': '\u4e00' * 250}}
        wire = json.dumps(candidate, ensure_ascii=False, separators=(',', ':')) + '\n'
        self.assertLess(len(wire.encode()), 1024)
        self.assertGreater(len((json.dumps(candidate, ensure_ascii=True,
                                           separators=(',', ':')) + '\n').encode()), 1024)
        with self.assertRaisesRegex(HarnessError, 'terminal exceeds capture limit'):
            self.run_cli('import sys; sys.stdout.write(' + repr(wire) + ')', terminal_limit=1024)
        self.assertFalse((self.job / 'stream-terminal.json').exists())
        self.assertEqual(list(self.job.glob('.stream-terminal-*')), [])

    def test_terminal_publication_never_replaces_concurrent_authority(self):
        original = os.link
        terminal = self.job / 'stream-terminal.json'
        def race(source, destination):
            terminal.write_bytes(b'{"existing":true}\n')
            original(source, destination)
        with patch('harness.stream_capture.os.link', side_effect=race):
            with self.assertRaisesRegex(HarnessError, 'publish Claude terminal safely'):
                self.run_cli('print("{\\"type\\":\\"result\\"}")')
        self.assertEqual(terminal.read_bytes(), b'{"existing":true}\n')
        self.assertEqual(list(self.job.glob('.stream-terminal-*')), [])

    def test_manifest_is_immutable_and_absent_is_legacy(self):
        self.assertEqual(claude_result_path(self.job).name, 'process.stdout')
        freeze_capture(self.job, 'stream_metadata_v1', 'fixture')
        with self.assertRaises(FileExistsError):
            freeze_capture(self.job, 'legacy_json', 'fixture')
        self.assertEqual(claude_result_path(self.job).name, 'stream-terminal.json')

    def test_denials_never_copy_args_or_invent_reason(self):
        projected = permission_denial_projection({'permission_denials': [
            {'tool_name': 'Bash', 'tool_use_id': 'id1', 'tool_input': 'SECRET', 'reason': 'SECRET'}]})
        self.assertEqual(projected, [{'tool': 'Bash', 'tool_id': 'id1', 'reason': 'unavailable'}])
