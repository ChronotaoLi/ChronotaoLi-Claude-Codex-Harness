"""Real HTTP subscribers and native file notifications; no model calls."""
import errno
import http.client
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import dashboard
from harness.watch import RunWatcher, WatchError


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / '.harness'
        self.run = self.state / 'runs' / 'run_stream'
        self.run.mkdir(parents=True)
        (self.run / 'state.json').write_text(json.dumps({'id': 'run_stream', 'status': 'blocked'}))
        (self.run / 'events.jsonl').write_text('')
        self.server = dashboard.create_server(self.state, 0, stream_heartbeat=.08,
                                              stream_wait=.02, socket_timeout=.3, log=io.StringIO())
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def connect(self, method='GET', path='/api/runs/run_stream/stream', headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        self.addCleanup(conn.close)
        conn.request(method, path, headers=headers or {})
        return conn, conn.getresponse()

    def frame(self, response):
        lines = []
        while True:
            line = response.fp.readline()
            self.assertTrue(line, lines)
            if line == b'\n':
                return b''.join(lines)
            lines.append(line)

    def event(self, response, name):
        for _ in range(100):
            frame = self.frame(response)
            if frame.startswith(('event: ' + name + '\n').encode()):
                return json.loads(frame.split(b'data: ', 1)[1])
        self.fail('missing event ' + name)

    def eventually(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_ready_atomic_replace_append_and_heartbeat_without_projection_reads(self):
        with patch.object(self.server.observer, 'run_detail', side_effect=AssertionError('projection read')), \
             patch.object(self.server.observer.reader, 'read', side_effect=AssertionError('state read')):
            _, response = self.connect()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Content-Type'), 'text/event-stream; charset=utf-8')
            ready = self.event(response, 'ready')
            self.assertEqual(ready['id'], 'run_stream')
            self.assertIn(ready['backend'], ('kqueue', 'inotify'))
            self.assertIn('sampled_at_utc', ready)
            self.assertEqual(self.frame(response), b': heartbeat\n')
            staged = self.run / 'next.json'
            staged.write_text('{"id":"run_stream","status":"running"}')
            os.replace(staged, self.run / 'state.json')
            self.assertEqual(self.event(response, 'change')['id'], 'run_stream')
            # Drain any batched replacement notification before a distinct append.
            while self.frame(response) != b': heartbeat\n':
                pass
            with (self.run / 'events.jsonl').open('a') as handle:
                handle.write('{"event":"resumed"}\n')
            self.assertEqual(self.event(response, 'change')['id'], 'run_stream')

    def test_capacity_regular_api_disconnect_and_shutdown(self):
        streams = [self.connect() for _ in range(2)]
        for _, response in streams:
            self.event(response, 'ready')
        _, excess = self.connect()
        self.assertEqual(excess.status, 503)
        excess.read()
        _, normal = self.connect(path='/api/runs')
        self.assertEqual(normal.status, 200)
        normal.read()
        conn, response = streams.pop()
        response.close()
        conn.close()
        self.eventually(lambda: self.server.active_streams() == 1)
        self.server.server_close()
        self.eventually(lambda: self.server.active_streams() == 0)
        self.eventually(lambda: self.server.active_connections() == 0)

    def assert_single_ready_response(self, after_ready):
        port = self.server.server_port
        with socket.create_connection(('127.0.0.1', port), timeout=2) as conn:
            conn.sendall((f'GET /api/runs/run_stream/stream HTTP/1.0\r\n'
                          f'Host: 127.0.0.1:{port}\r\n\r\n').encode())
            with conn.makefile('rb') as wire:
                prefix = b''
                while not prefix.endswith(b'\n\n') or b'event: ready\n' not in prefix:
                    line = wire.readline()
                    self.assertTrue(line, prefix)
                    prefix += line
                after_ready(conn)
                response = prefix + wire.read()
        self.assertEqual([line for line in response.splitlines() if line.startswith(b'HTTP/')],
                         [b'HTTP/1.0 200 OK'])
        self.assertNotIn(b'500 internal error', response)
        self.assertEqual(self.server.active_streams(), 0)

    def test_owned_close_failure_after_ready_sends_one_http_response(self):
        watched_fds = []
        failures = []
        real_close = os.close

        def create(reader, run_id):
            watcher = RunWatcher(reader, run_id)
            watched_fds.append(watcher._entries[('runs', run_id, 'state.json')][1])
            return watcher

        def close(fd):
            real_close(fd)
            if watched_fds and fd == watched_fds[0] and not failures:
                failures.append(fd)
                raise OSError(errno.EIO, 'injected watcher-owned close failure')

        with patch.object(dashboard, 'RunWatcher', side_effect=create), patch.object(os, 'close', close):
            self.assert_single_ready_response(lambda conn: conn.shutdown(socket.SHUT_WR))
        self.assertEqual(failures, watched_fds)
        self.assertIn('internal error: StateReadError', self.server.log.getvalue())

    def test_ready_frame_failure_sends_one_http_response(self):
        stream_frame = dashboard.DashboardHandler._stream_frame

        def fail_after_frame(handler, event, data):
            stream_frame(handler, event, data)
            raise RuntimeError('injected frame failure')

        with patch.object(dashboard.DashboardHandler, '_stream_frame', fail_after_frame):
            self.assert_single_ready_response(lambda conn: None)
        self.assertIn('internal error: RuntimeError: injected frame failure', self.server.log.getvalue())

    def test_security_head_and_native_unavailable(self):
        for method, path, headers, expected in (
            ('HEAD', '/api/runs/run_stream/stream', {}, 405),
            ('POST', '/api/runs/run_stream/stream', {}, 405),
            ('GET', '/api/runs/run_stream/stream', {'Host': 'evil'}, 403),
            ('GET', '/api/runs/run_stream/stream', {'Origin': 'https://evil'}, 403),
            ('GET', '/api/runs/invalid/stream', {}, 400),
            ('GET', '/api/runs/run_missing/stream', {}, 404),
            ('GET', '/api/runs/run_stream%2f..%2fsecret/stream', {}, 404),
        ):
            with self.subTest(method=method, path=path, headers=headers):
                _, response = self.connect(method, path, headers)
                self.assertEqual(response.status, expected)
                response.read()
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (self.state / 'runs' / 'run_link').symlink_to(outside, target_is_directory=True)
        _, response = self.connect(path='/api/runs/run_link/stream')
        self.assertEqual(response.status, 403)
        response.read()
        with patch.object(dashboard, 'RunWatcher', side_effect=RuntimeError('injected setup failure')):
            _, response = self.connect()
            self.assertEqual(response.status, 500)
            self.assertEqual(response.read(), b'500 internal error\n')
        with patch('harness.dashboard.RunWatcher', side_effect=WatchError('native unsupported')):
            _, response = self.connect()
            self.assertEqual(response.status, 200)
            self.assertEqual(self.event(response, 'unavailable')['error'], 'native unsupported')
            self.assertEqual(response.read(), b'')
        self.eventually(lambda: self.server.active_streams() == 0)

    def test_slow_receiver_times_out_and_releases_capacity_without_client_close(self):
        def congested_frame(handler, event, data):
            # Force transport backpressure deterministically; the peer remains open
            # and does not read the body until the bounded server write has exited.
            handler.wfile.write(b": " + b"x" * (8 * 1024 * 1024) + b"\n\n")
        with patch.object(dashboard.DashboardHandler, '_stream_frame', congested_frame):
            _, response = self.connect()
            self.assertEqual(response.status, 200)
            self.eventually(lambda: self.server.active_streams() == 0)
            self.eventually(lambda: self.server.active_connections() == 0)
        _, normal = self.connect(path='/api/runs')
        self.assertEqual(normal.status, 200)
        normal.read()

    def test_shutdown_closes_watch_descriptors_and_stops_reader_access(self):
        watchers = []
        def create(reader, run_id):
            watcher = RunWatcher(reader, run_id)
            watchers.append(watcher)
            return watcher
        with patch('harness.dashboard.RunWatcher', side_effect=create):
            _, response = self.connect()
            self.event(response, 'ready')
            self.assertTrue(watchers[0]._entries)
            self.server.server_close()
            self.eventually(lambda: self.server.active_streams() == 0)
            self.assertTrue(watchers[0]._closed)
            self.assertEqual(watchers[0]._entries, {})
            self.assertEqual(response.read(), b'')

    def test_watcher_failure_closes_and_releases_subscription(self):
        original = RunWatcher.wait
        real_close = RunWatcher.close
        failed = threading.Event()
        def wait(watcher, timeout):
            if failed.is_set():
                raise WatchError('watch lost')
            return original(watcher, timeout)
        with patch.object(RunWatcher, 'wait', wait), patch.object(
                RunWatcher, 'close', autospec=True, side_effect=real_close) as close:
            _, response = self.connect()
            self.event(response, 'ready')
            failed.set()
            self.assertEqual(self.event(response, 'unavailable')['error'], 'watch lost')
            self.assertEqual(response.read(), b'')
            self.eventually(lambda: self.server.active_streams() == 0)
            self.assertGreaterEqual(close.call_count, 1)


if __name__ == '__main__':
    unittest.main()
