"""Real filesystem lifecycle checks; no model calls are exercised."""
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from harness.observe import InvalidPath, NotFound, PathRefused, StateReader
from harness.watch import RunWatcher, WatchError, _Inotify


@unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'native backend unavailable')
class RunWatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '.harness'
        self.run = self.root / 'runs' / 'run_test'
        self.run.mkdir(parents=True)
        (self.run / 'state.json').write_text('{}')
        (self.run / 'events.jsonl').write_text('')
        self.reader = StateReader(self.root, required=True)
        self.addCleanup(self.reader.close)

    def watcher(self):
        watcher = RunWatcher(self.reader, 'run_test')
        self.addCleanup(watcher.close)
        return watcher

    def drain(self, watcher):
        for _ in range(20):
            if not watcher.wait(0):
                return
        self.fail('notification queue did not settle')

    def test_timeout_does_not_read_or_reopen_state(self):
        watcher = self.watcher()
        with patch.object(self.reader, 'open_directory', side_effect=AssertionError('polling')), patch.object(
                self.reader, 'open_file', side_effect=AssertionError('polling')):
            self.assertFalse(watcher.wait(0.02))

    def test_atomic_replace_rebind_and_append(self):
        watcher = self.watcher()
        replacement = self.run / 'next.tmp'
        replacement.write_text('{"status":"running"}')
        replacement.replace(self.run / 'state.json')
        self.assertTrue(watcher.wait(1))
        self.drain(watcher)
        (self.run / 'state.json').write_text('{"status":"done"}')
        self.assertTrue(watcher.wait(1))
        self.drain(watcher)
        with (self.run / 'events.jsonl').open('a') as stream:
            stream.write('{"event":"finished"}\n')
        self.assertTrue(watcher.wait(1))

    def test_blocking_wait_receives_write(self):
        watcher = self.watcher()
        started, finished = threading.Event(), threading.Event()
        outcome = []
        def wait():
            started.set()
            outcome.append(watcher.wait(2))
            finished.set()
        thread = threading.Thread(target=wait)
        thread.start()
        self.assertTrue(started.wait(1))
        (self.run / 'events.jsonl').write_text('{}\n')
        self.assertTrue(finished.wait(3))
        thread.join(1)
        self.assertEqual(outcome, [True])

    def test_run_removal_and_recreation(self):
        watcher = self.watcher()
        shutil.rmtree(self.run)
        self.assertTrue(watcher.wait(1))
        self.drain(watcher)
        self.run.mkdir()
        (self.run / 'state.json').write_text('{}')
        self.assertTrue(watcher.wait(1))
        self.drain(watcher)
        (self.run / 'state.json').write_text('{"new":true}')
        self.assertTrue(watcher.wait(1))

    def test_runs_directory_replacement(self):
        watcher = self.watcher()
        self.root.joinpath('runs').rename(self.root / 'old-runs')
        self.run.mkdir(parents=True)
        (self.run / 'state.json').write_text('{}')
        self.assertTrue(watcher.wait(1))
        self.drain(watcher)
        (self.run / 'state.json').write_text('{"new":true}')
        self.assertTrue(watcher.wait(1))

    def test_subscribe_before_snapshot_retains_queued_change(self):
        watcher = self.watcher()
        (self.run / 'state.json').write_text('{"new":true}')
        self.reader.read(['runs', 'run_test', 'state.json'], limit=100)
        self.assertTrue(watcher.wait(1))

    def test_missing_initial_run_and_invalid_id(self):
        with self.assertRaises(NotFound):
            RunWatcher(self.reader, 'run_missing')
        with self.assertRaises(InvalidPath):
            RunWatcher(self.reader, '../escape')

    def test_symlink_run_file_and_replacement_refused(self):
        watcher = self.watcher()
        state = self.run / 'state.json'
        state.unlink()
        state.symlink_to('/etc/passwd')
        with self.assertRaises(PathRefused):
            watcher.wait(1)
        with self.assertRaises(PathRefused):
            RunWatcher(self.reader, 'run_test')
        state.unlink()
        shutil.rmtree(self.run)
        self.run.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(PathRefused):
            RunWatcher(self.reader, 'run_test')

    def test_close_releases_owned_descriptors_and_preserves_reader(self):
        watcher = self.watcher()
        native = watcher._native
        native_fd = native.queue.fileno() if hasattr(native, 'queue') else native.fd
        fds = [native_fd, *(entry[1] for entry in watcher._entries.values())]
        watcher.close()
        watcher.close()
        for fd in fds:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertEqual(self.reader.read(['runs', 'run_test', 'state.json'], limit=10)['data'], b'{}')
        with self.assertRaises(WatchError):
            watcher.wait(0)

    def test_missing_events_file_creation_and_fifo_refusal(self):
        events = self.run / 'events.jsonl'
        events.unlink()
        watcher = self.watcher()
        events.write_text('{}\n')
        self.assertTrue(watcher.wait(1))
        self.drain(watcher)
        events.unlink()
        os.mkfifo(events)
        with self.assertRaises(PathRefused):
            watcher.wait(1)

    def test_hardlinked_files_remain_watched_after_one_replaced(self):
        events = self.run / 'events.jsonl'
        events.unlink()
        os.link(self.run / 'state.json', events)
        watcher = self.watcher()
        replacement = self.run / 'next.tmp'
        replacement.write_text('{}')
        replacement.replace(self.run / 'state.json')
        self.assertTrue(watcher.wait(1))
        self.drain(watcher)
        events.write_text('{"event":"hardlink"}\n')
        self.assertTrue(watcher.wait(1))

    def test_bad_timeouts(self):
        watcher = self.watcher()
        for value in (-1, float('inf'), float('nan'), '1', None):
            with self.assertRaises(ValueError):
                watcher.wait(value)


class UnsupportedWatchTests(unittest.TestCase):
    def test_unsupported_platform_fails_explicitly(self):
        with patch('harness.watch.sys.platform', 'unsupported'):
            with self.assertRaisesRegex(WatchError, 'unsupported'):
                RunWatcher(None, 'run_test')


class InotifyBookkeepingTests(unittest.TestCase):
    """Portable mocked syscall coverage; this does not prove Linux native delivery."""

    def test_overflow_invalidates_and_read_is_bounded(self):
        native = _Inotify.__new__(_Inotify)
        native.fd = 123
        with patch('harness.watch.select.select', return_value=([123], [], [])), patch(
                'harness.watch.os.read', return_value=struct.pack('iIII', -1, 0x4000, 0, 0)) as read:
            self.assertTrue(native.wait(0))
        read.assert_called_once_with(123, 65536)

    def test_ignored_removal_is_not_a_change(self):
        native = _Inotify.__new__(_Inotify)
        native.fd = 123
        with patch('harness.watch.select.select', return_value=([123], [], [])), patch(
                'harness.watch.os.read', return_value=struct.pack('iIII', 1, 0x8000, 0, 0)):
            self.assertFalse(native.wait(0))

    def test_shared_token_removed_only_after_last_reference(self):
        watcher = RunWatcher.__new__(RunWatcher)
        watcher._entries = {'state': ((1, 1), 100, 7), 'events': ((1, 1), 101, 7)}
        from unittest.mock import Mock
        watcher._native = Mock()
        with patch('harness.watch.os.close') as close:
            watcher._drop('state')
            watcher._native.remove.assert_not_called()
            watcher._drop('events')
            watcher._native.remove.assert_called_once_with(7)
            self.assertEqual(close.call_count, 2)
