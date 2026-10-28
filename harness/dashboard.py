"""Local read-only dashboard: a bounded HTTP service over `harness.observe`.

Binds 127.0.0.1 only, accepts GET/HEAD, requires an exact Host (and Origin when
present), caps concurrent connections before a handler thread exists, and times
out slow clients. It never writes, locks, runs git, starts processes or probes
process liveness; every file read goes through the observation layer.
"""
from __future__ import annotations

import argparse
import json
import signal
import socketserver
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import observe
from .dashboard_page import APP_CSS, APP_JS, INDEX_HTML
from .util import HarnessError

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_CONNECTIONS = 8
SOCKET_TIMEOUT = 10.0
MAX_PATH_BYTES = 2048
SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy",
     "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'"),
)
TEXT = "text/plain; charset=utf-8"
JSON = "application/json; charset=utf-8"
_STATUS_CODES = {observe.InvalidPath: 400, observe.PathRefused: 403, observe.NotFound: 404}


def _busy_response():
    """The fixed 503 written before any handler exists carries the same security headers."""
    body = b"503 service unavailable: connection limit reached\n"
    headers = (("Retry-After", "1"), ("Connection", "close"), ("Content-Type", TEXT),
               ("Content-Length", str(len(body))), *SECURITY_HEADERS)
    return (b"HTTP/1.0 503 Service Unavailable\r\n"
            + b"".join(f"{name}: {value}\r\n".encode("ascii") for name, value in headers) + b"\r\n" + body)


BUSY_RESPONSE = _busy_response()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=1) + "\n").encode("utf-8", errors="backslashreplace")


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "HarnessDashboard/1"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.timeout = self.server.socket_timeout
        super().setup()

    def __getattr__(self, name):
        # Any method other than GET/HEAD (which are defined below) answers 405.
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def _method_not_allowed(self):
        self._send(405, b"405 method not allowed\n", TEXT, (("Allow", "GET, HEAD"),))

    def log_message(self, format, *args):
        self.server.log.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(),
                                                   format % args))

    def send_error(self, code, message=None, explain=None):
        # Used by the base parser for malformed requests; never echo request text.
        try:
            phrase = self.responses[code][0]
        except KeyError:
            phrase = "error"
        self._send(code, f"{code} {phrase}\n".encode("utf-8"), TEXT)

    def _send(self, status, body, content_type, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (*SECURITY_HEADERS, *extra):
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _origin_allowed(self):
        expected = f"{HOST}:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin == "http://" + expected

    def _handle(self):
        try:
            if len(self.path) > MAX_PATH_BYTES:
                self._send(414, b"414 request path too long\n", TEXT)
                return
            if not self._origin_allowed():
                self._send(403, b"403 forbidden: Host or Origin does not match this server\n", TEXT)
                return
            status, body, content_type, extra = self._route()
        except observe.ObserveError as error:
            status = next((code for kind, code in _STATUS_CODES.items() if isinstance(error, kind)), 400)
            body, content_type, extra = _json_bytes({"error": str(error), "status": status}), JSON, ()
        except Exception as error:  # Never leak a traceback to the client.
            self.log_error("internal error: %s: %s", type(error).__name__, error)
            status, body, content_type, extra = 500, b"500 internal error\n", TEXT, ()
        self._send(status, body, content_type, extra)

    @staticmethod
    def _tail(query):
        values = query.get("tail")
        if not values:
            return None
        value = values[-1]
        if not value.isdigit():
            raise observe.InvalidPath("tail 参数必须是正整数")
        return int(value)

    @staticmethod
    def _format(query):
        values = query.get("format")
        if not values or values[-1] == "":
            return "text"
        if values[-1] != "json":
            raise observe.InvalidPath("format 参数只接受 json")
        return "json"

    def _route(self):
        parts = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parts.path)
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        observer = self.server.observer
        if path == "/":
            return 200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8", ()
        if path == "/app.js":
            return 200, APP_JS.encode("utf-8"), "application/javascript; charset=utf-8", ()
        if path == "/app.css":
            return 200, APP_CSS.encode("utf-8"), "text/css; charset=utf-8", ()
        if path == "/api/runs":
            return 200, _json_bytes(observer.list_runs()), JSON, ()
        if not path.startswith("/api/runs/"):
            raise observe.NotFound("未知路由")
        run_id, _, remainder = path[len("/api/runs/"):].partition("/")
        if not run_id:
            raise observe.NotFound("未知路由")
        if remainder == "":
            return 200, _json_bytes(observer.run_detail(run_id)), JSON, ()
        if remainder == "events":
            return 200, _json_bytes(observer.events(run_id, self._tail(query))), JSON, ()
        if remainder == "files":
            return 200, _json_bytes(observer.file_index(run_id)), JSON, ()
        if remainder.startswith("files/"):
            output = self._format(query)
            read = observer.read_file(run_id, remainder[len("files/"):], self._tail(query))
            headers = (("X-Harness-Source", read["source"]), ("X-Harness-Size", str(read["size"])),
                       ("X-Harness-Truncated", "true" if read["truncated"] else "false"),
                       ("X-Harness-Changed-While-Reading", "true" if read["changed_while_reading"] else "false"),
                       ("X-Harness-Sampled-At", read["sampled_at_utc"]))
            if output == "json":
                return 200, _json_bytes(read), JSON, headers
            return 200, read["text"].encode("utf-8"), TEXT, headers
        raise observe.NotFound("未知路由")


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, state, port=DEFAULT_PORT, *, max_connections=MAX_CONNECTIONS,
                 socket_timeout=SOCKET_TIMEOUT, log=None):
        # The `.harness` descriptor is fixed here, before the socket exists.
        self.observer = observe.Observer(state, required=True)
        self.max_connections = max_connections
        self.socket_timeout = socket_timeout
        self.log = log if log is not None else sys.stderr
        self._lock = threading.Lock()
        self._active = 0
        super().__init__((HOST, port), DashboardHandler)

    def server_bind(self):
        # Skip HTTPServer's getfqdn(): no DNS lookups for a loopback service.
        socketserver.TCPServer.server_bind(self)
        self.server_name = HOST
        self.server_port = self.socket.getsockname()[1]

    @property
    def url(self):
        return f"http://{HOST}:{self.server_port}/"

    def active_connections(self):
        with self._lock:
            return self._active

    def _release(self):
        with self._lock:
            self._active -= 1

    def process_request(self, request, client_address):
        # Count before any handler thread exists; a surplus connection gets a
        # fixed 503 and is closed without parsing its request line or headers.
        with self._lock:
            busy = self._active >= self.max_connections
            if not busy:
                self._active += 1
        if busy:
            try:
                request.settimeout(1.0)
                request.sendall(BUSY_RESPONSE)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release()

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        self.log.write(f"harness dashboard: request from {client_address[0]} failed: "
                       f"{type(error).__name__}: {error}\n")

    def server_close(self):
        try:
            super().server_close()
        finally:
            self.observer.close()


def create_server(state, port=DEFAULT_PORT, **options):
    if type(port) is not int or not 0 <= port <= 65535:
        raise HarnessError("端口必须是 0 到 65535 之间的整数")
    try:
        return DashboardServer(state, port, **options)
    except observe.NotFound as error:
        raise HarnessError(f"{error}；没有可观察的 run 目录时请先执行 run 或 status") from error


def add_arguments(parser):
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"只绑定 127.0.0.1 的端口；默认 {DEFAULT_PORT}，0 由系统分配")


def serve(state, port=DEFAULT_PORT, *, stdout=None, stderr=None):
    """Print the URL on stdout, serve until SIGINT/SIGTERM, return 0."""
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    server = create_server(state, port, log=stderr)
    previous = {}
    try:
        print(server.url, file=stdout, flush=True)
        if threading.current_thread() is threading.main_thread():
            def interrupted(signum, frame):
                raise KeyboardInterrupt()
            for signum in (signal.SIGTERM, signal.SIGINT):
                if signal.getsignal(signum) in (signal.SIG_DFL, signal.default_int_handler):
                    previous[signum] = signal.signal(signum, interrupted)
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            print("harness dashboard: 已停止", file=stderr, flush=True)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        server.server_close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Personal Harness 只读观察网页（本机）")
    parser.add_argument("--state", required=True, help="目标项目的 .harness 目录")
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        return serve(args.state, args.port)
    except (HarnessError, OSError) as error:
        print(f"harness: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
