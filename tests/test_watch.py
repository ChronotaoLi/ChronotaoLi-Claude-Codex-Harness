"""Real filesystem lifecycle checks; no model calls are exercised."""
import errno
import os
from pathlib import Path
import shutil
import signal
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from harness import state_files
from harness.observe import InvalidPath, NotFound, PathRefused, StateReader
from harness.watch import RunWatcher, WatchError, _Inotify, watching


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

    def test_signal_after_descriptor_handoff_during_construction_and_refresh(self):
        real_add = state_files.Owned.add
        for operation in ("construct", "refresh"):
            with self.subTest(operation=operation):
                before = len(os.listdir("/dev/fd"))
                handed = []

                def interrupt_after_add(owner, fd):
                    real_add(owner, fd)
                    handed.append(fd)
                    signal.pthread_kill(threading.get_ident(), signal.SIGINT)

                with self.assertRaises(KeyboardInterrupt):
                    if operation == "construct":
                        with patch.object(state_files.Owned, "add", interrupt_after_add):
                            RunWatcher(self.reader, "run_test")
                    else:
                        with RunWatcher(self.reader, "run_test") as watcher:
                            (self.run / "state.json").write_text('{"changed":true}')
                            with patch.object(state_files.Owned, "add", interrupt_after_add):
                                watcher.wait(1)

                self.assertEqual((len(handed), len(os.listdir("/dev/fd"))), (1, before))
                with self.assertRaises(OSError) as failure:
                    os.fstat(handed[0])
                self.assertEqual(failure.exception.errno, errno.EBADF)

    def test_single_signal_at_close_entry_releases_native_and_held_descriptors(self):
        for owned in (False, True):
            for body_fails in (False, True):
                with self.subTest(owned=owned, body_fails=body_fails):
                    before = len(os.listdir("/dev/fd"))
                    mask = signal.pthread_sigmask(signal.SIG_BLOCK, ())
                    body_error = ValueError("body")
                    watcher = None
                    fds = []
                    signals = []

                    def open_watcher():
                        nonlocal watcher
                        watcher = self.watcher()
                        native = watcher._native
                        native_fd = native.queue.fileno() if hasattr(native, "queue") else native.fd
                        fds[:] = [native_fd, *(entry[1] for entry in watcher._entries.values())]
                        return watcher

                    def inject(frame, event, arg):
                        if event == "line" and not signals:
                            signals.append(signal.SIGINT)
                            signal.raise_signal(signal.SIGINT)
                        return inject

                    def trace(frame, event, arg):
                        if event == "call" and frame.f_code is RunWatcher.close.__code__:
                            return inject

                    def body(watcher):
                        if body_fails:
                            raise body_error

                    previous_trace = sys.gettrace()
                    expected = ValueError if body_fails else KeyboardInterrupt
                    try:
                        with self.assertRaises(expected) as caught:
                            sys.settrace(trace)
                            if owned:
                                watching(open_watcher, body)
                            else:
                                with open_watcher() as watcher:
                                    body(watcher)
                    finally:
                        sys.settrace(previous_trace)

                    if body_fails:
                        self.assertIs(caught.exception, body_error)
                    self.assertEqual(signals, [signal.SIGINT])
                    self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, ()), mask)
                    self.assertEqual(len(os.listdir("/dev/fd")), before)
                    self.assertTrue(watcher._closed)
                    self.assertEqual(len(fds), 6)
                    for fd in fds:
                        with self.assertRaises(OSError) as released:
                            os.fstat(fd)
                        self.assertEqual(released.exception.errno, errno.EBADF)

    def test_close_unmasks_after_both_owners_are_closed(self):
        before = len(os.listdir("/dev/fd"))
        watcher = self.watcher()
        native = watcher._native
        native_fd = native.queue.fileno() if hasattr(native, "queue") else native.fd
        fds = [native_fd, *(entry[1] for entry in watcher._entries.values())]
        real_mask = signal.pthread_sigmask
        signalled = []

        def interrupt_at_restore(how, mask):
            if how == signal.SIG_SETMASK and watcher._closed and not signalled:
                signalled.append(True)
                signal.pthread_kill(threading.get_ident(), signal.SIGINT)
            return real_mask(how, mask)

        with patch.object(signal, "pthread_sigmask", interrupt_at_restore), self.assertRaises(KeyboardInterrupt):
            watcher.close()
        self.assertEqual((signalled, len(os.listdir("/dev/fd"))), ([True], before))
        for fd in fds:
            with self.assertRaises(OSError) as failure:
                os.fstat(fd)
            self.assertEqual(failure.exception.errno, errno.EBADF)
        with patch.object(os, "close", side_effect=AssertionError("descriptor closed twice")):
            watcher.close()

    def test_context_close_releases_descriptors_when_entering_the_mask_fails(self):
        real_mask = signal.pthread_sigmask
        original = real_mask(signal.SIG_BLOCK, ())
        self.addCleanup(real_mask, signal.SIG_SETMASK, original)
        for phase in ("query", "block"):
            for interrupt in (False, True):
                for body_fails in (False, True):
                    with self.subTest(phase=phase, interrupt=interrupt, body_fails=body_fails):
                        before = len(os.listdir("/dev/fd"))
                        watcher = self.watcher()
                        native = watcher._native
                        native_fd = native.queue.fileno() if hasattr(native, "queue") else native.fd
                        fds = [native_fd, *(entry[1] for entry in watcher._entries.values())]
                        fault = OSError(errno.EIO, "enter close mask")
                        body_error = ValueError("body")
                        injected = False
                        restored = []

                        def fail_once(how, mask):
                            nonlocal injected
                            if how == signal.SIG_SETMASK:
                                restored.append(set(mask))
                            if not injected and how == signal.SIG_BLOCK and bool(mask) == (phase == "block"):
                                injected = True
                                if interrupt:
                                    signal.pthread_kill(threading.get_ident(), signal.SIGINT)
                                else:
                                    if phase == "block":
                                        # Applying the block can succeed before the call raises.
                                        real_mask(how, mask)
                                    raise fault
                            return real_mask(how, mask)

                        expected = ValueError if body_fails else KeyboardInterrupt if interrupt else OSError
                        caught = None
                        with patch.object(signal, "pthread_sigmask", fail_once):
                            try:
                                with watcher:
                                    if body_fails:
                                        raise body_error
                            except BaseException as error:
                                caught = error
                        self.assertTrue(injected)
                        self.assertIsInstance(caught, expected)
                        if body_fails or not interrupt:
                            self.assertIs(caught, body_error if body_fails else fault)
                        self.assertEqual(real_mask(signal.SIG_BLOCK, ()), original)
                        self.assertEqual(len(os.listdir("/dev/fd")), before)
                        self.assertTrue(watcher._closed)
                        # Owned restores its own mask. A failed watcher query adds no restore of its own.
                        self.assertEqual(restored, [original] if phase == "query"
                                         else [original | state_files.SIGNALS, original] if not interrupt
                                         else [original, original])
                        for fd in fds:
                            with self.assertRaises(OSError) as released:
                                os.fstat(fd)
                            self.assertEqual(released.exception.errno, errno.EBADF)

    def test_close_keeps_first_error_and_does_not_close_a_reused_native_fd(self):
        before = len(os.listdir("/dev/fd"))
        watcher = self.watcher()
        native = watcher._native
        native_fd = native.queue.fileno() if hasattr(native, "queue") else native.fd
        owned_fd = watcher._entries[()][1]
        close = os.close
        native_error = OSError(errno.EIO, "native close")
        reused = []

        def close_native():
            self.assertFalse(reused, "retried a native close")
            native_close()
            fd = os.open(os.devnull, os.O_RDONLY)
            reused.append(fd)
            self.addCleanup(close, fd)
            self.assertEqual(fd, native_fd)
            raise native_error

        def close_owned(fd):
            close(fd)
            if fd == owned_fd:
                raise OSError(errno.EIO, "owned close")

        native_close = native.close
        with patch.object(native, "close", close_native), patch.object(os, "close", close_owned):
            with self.assertRaises(OSError) as caught:
                watcher.close()
            self.assertIs(caught.exception, native_error)
            watcher.close()
        self.assertEqual(len(os.listdir("/dev/fd")), before + 1)
        self.assertEqual(os.fstat(reused[0]).st_ino, os.stat(os.devnull).st_ino)

    def test_refresh_releases_unchanged_replaced_and_failed_registrations(self):
        watcher = self.watcher()
        prior = dict(watcher._entries)
        state = self.run / "state.json"
        path = ("runs", "run_test", "state.json")
        replacement = self.run / "next.tmp"
        replacement.write_text("{}")
        replacement.replace(state)
        with patch.object(watcher._owned, "release", wraps=watcher._owned.release) as released:
            self.assertTrue(watcher.wait(1))
            # Four unchanged subscriptions release their new descriptors; the replaced file releases the old one.
            self.assertEqual(released.call_count, len(prior))
            released.assert_any_call(prior[path][1])
            self.assertNotEqual(watcher._entries[path][0], prior[path][0])

            replacement.write_text("{}")
            replacement.replace(state)
            with patch.object(watcher._native, "add", side_effect=WatchError("registration failed")):
                with self.assertRaisesRegex(WatchError, "registration failed"):
                    watcher.wait(1)
            self.assertEqual(released.call_count, len(prior) + 4)
            failed_fd = released.call_args.args[0]
            with self.assertRaises(OSError):
                os.fstat(failed_fd)
        watcher.close()

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
        watcher._owned = state_files.Owned()
        watcher._owned.add(100)
        watcher._owned.add(101)
        with patch('harness.state_files.os.close') as close:
            watcher._drop('state')
            watcher._native.remove.assert_not_called()
            watcher._drop('events')
            watcher._native.remove.assert_called_once_with(7)
            self.assertEqual(close.call_count, 2)
