"""Local read-only dashboard over real HTTP: routes, file boundaries, limits, read-only proofs."""
import contextlib
import fcntl
import hashlib
import http.client
import http.server
import io
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from harness import cli, dashboard, observe
from harness.cli import status_summary
from harness.dashboard_page import APP_JS
from harness.util import read_json, write_json
from test_harness import ROOT, RepoCase

# Every module that binds run_process by name; patching harness.util alone misses them.
RUN_PROCESS_SITES = ("harness.adapters.run_process", "harness.verification.run_process",
                     "harness.command_server.run_process", "harness.shared.run_process",
                     "harness.util.run_process")

# Minimal DOM/fetch stand-in that executes the real APP_JS under node and reports what it rendered.
PAGE_HARNESS_JS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const fixture = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const requested = [];
class Node {
  constructor(tag) { this.tagName = tag; this.childNodes = []; this.className = ""; this.ownText = ""; this.listeners = {}; }
  get firstChild() { return this.childNodes.length ? this.childNodes[0] : null; }
  appendChild(child) { this.childNodes.push(child); return child; }
  removeChild(child) { const i = this.childNodes.indexOf(child); if (i >= 0) { this.childNodes.splice(i, 1); } return child; }
  get textContent() { return this.ownText + this.childNodes.map(function (c) { return c.textContent; }).join(""); }
  set textContent(value) { this.ownText = String(value); this.childNodes = []; }
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
}
const byId = {};
["main", "sampled", "refresh", "auto"].forEach(function (id) { byId[id] = new Node("div"); byId[id].id = id; });
const windowListeners = {};
globalThis.Node = Node;
globalThis.document = {
  createElement: function (tag) { return new Node(tag); },
  createTextNode: function (text) { const n = new Node("#text"); n.ownText = String(text); return n; },
  getElementById: function (id) { return byId[id] || null; }
};
globalThis.window = { addEventListener: function (name, fn) { (windowListeners[name] = windowListeners[name] || []).push(fn); } };
globalThis.location = { hash: fixture.initial_hash };
globalThis.fetch = function (url) {
  requested.push(url);
  const entry = fixture.responses[url.split("?")[0]];
  const body = JSON.stringify(entry ? entry : { error: "missing", status: 404 });
  return Promise.resolve({ ok: !!entry, status: entry ? 200 : 404, text: function () { return Promise.resolve(body); } });
};
vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
function settle() { return new Promise(function (resolve) { setTimeout(resolve, 30); }); }
function anchors(node, out) {
  if (node.tagName === "a") { out.push(node.href); }
  node.childNodes.forEach(function (c) { anchors(c, out); });
  return out;
}
function snapshot(hash) {
  return { hash: hash, requested: requested.slice(), text: byId.main.textContent, anchors: anchors(byId.main, []) };
}
(async function () {
  const phases = [];
  await settle();
  phases.push(snapshot(globalThis.location.hash));
  for (const hash of fixture.hashes) {
    requested.length = 0;
    globalThis.location.hash = hash;
    (windowListeners.hashchange || []).forEach(function (fn) { fn(); });
    await settle();
    phases.push(snapshot(hash));
  }
  process.stdout.write(JSON.stringify({ phases: phases, sampled: byId.sampled.textContent }));
})().catch(function (error) {
  process.stdout.write(JSON.stringify({ error: String(error && error.stack || error) }));
  process.exitCode = 1;
});
"""


def http_request(port, path, method="GET", headers=None, host=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        sent = dict(headers or {})
        if host is not None:
            sent["Host"] = host
        connection.request(method, path, headers=sent)
        response = connection.getresponse()
        body = response.read()
        return response.status, {name.lower(): value for name, value in response.getheaders()}, body
    finally:
        connection.close()


def tree_snapshot(root):
    """Path set plus type, size, mtime and content digest (never atime) of every entry."""
    snapshot = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = os.lstat(path)
            entry = [stat.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns]
            if stat.S_ISREG(info.st_mode):
                entry.append(hashlib.sha256(path.read_bytes()).hexdigest())
            elif stat.S_ISLNK(info.st_mode):
                entry.append(os.readlink(path))
            snapshot[str(path.relative_to(root))] = tuple(entry)
    return snapshot


class DashboardCase(RepoCase):
    def start_server(self, **options):
        server = dashboard.create_server(self.repo / ".harness", 0, log=io.StringIO(), **options)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def request(self, server, path, method="GET", headers=None, host=None):
        return http_request(server.server_port, path, method=method, headers=headers, host=host)

    def demo_run(self):
        engine = self.engine()
        result = engine.start("Implement greet")
        return result["id"], engine.directory

    def assert_security_headers(self, headers):
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(headers["content-security-policy"],
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'")


class RouteTests(DashboardCase):
    def test_routes_status_codes_origin_checks_and_security_headers(self):
        run_id, _ = self.demo_run()
        server = self.start_server()
        port = server.server_port
        for path, content_type in (("/", "text/html; charset=utf-8"), ("/app.js", "application/javascript; charset=utf-8"),
                                   ("/app.css", "text/css; charset=utf-8"), ("/api/runs", "application/json; charset=utf-8")):
            with self.subTest(path=path):
                status, headers, body = self.request(server, path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["content-type"], content_type)
                self.assert_security_headers(headers)
                self.assertEqual(int(headers["content-length"]), len(body))
        _, _, html = self.request(server, "/")
        _, _, js = self.request(server, "/app.js")
        self.assertNotIn(b"<script>", html)  # Only the external /app.js reference, no inline code.
        self.assertNotIn(b"https://", html + js)
        self.assertNotIn(b"http://", html + js)
        for forbidden in (b"innerHTML", b"outerHTML", b"document.write", b"insertAdjacentHTML", b"eval("):
            self.assertNotIn(forbidden, js)
        status, headers, body = self.request(server, "/", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertGreater(int(headers["content-length"]), 0)
        self.assert_security_headers(headers)

        status, headers, body = self.request(server, f"/api/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], run_id)
        status, _, _ = self.request(server, f"/api/runs/{run_id}/events?tail=4096")
        self.assertEqual(status, 200)
        status, _, body = self.request(server, f"/api/runs/{run_id}/files")
        self.assertEqual(status, 200)
        self.assertIn("state.json", [item["path"] for item in json.loads(body)["files"]])

        checks = (("/missing", 404), ("/api", 404), ("/api/runs/", 404), ("/api/runs/run_absent", 404),
                  (f"/api/runs/{run_id}/unknown", 404), ("/api/runs/not-a-run", 400), ("/api/runs/run_x;y", 400),
                  (f"/api/runs/{run_id}/events?tail=0", 400), (f"/api/runs/{run_id}/events?tail=abc", 400),
                  (f"/api/runs/{run_id}/files/state.json?format=xml", 400), ("/" + "a" * 2100, 414))
        for path, expected in checks:
            with self.subTest(path=path[:40]):
                status, headers, _ = self.request(server, path)
                self.assertEqual(status, expected)
                self.assert_security_headers(headers)
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            status, headers, _ = self.request(server, "/api/runs", method=method)
            self.assertEqual(status, 405, method)
            self.assertEqual(headers["allow"], "GET, HEAD")
            self.assert_security_headers(headers)
        for host in (f"localhost:{port}", "127.0.0.1", f"127.0.0.1:{port + 1}", f"example.com:{port}", ""):
            status, headers, _ = self.request(server, "/api/runs", host=host)
            self.assertEqual(status, 403, host)
            self.assert_security_headers(headers)
        for origin in ("http://localhost:%d" % port, "https://127.0.0.1:%d" % port, "null", "http://127.0.0.1"):
            status, _, _ = self.request(server, "/api/runs", headers={"Origin": origin})
            self.assertEqual(status, 403, origin)
        status, _, _ = self.request(server, "/api/runs", headers={"Origin": f"http://127.0.0.1:{port}"})
        self.assertEqual(status, 200)

    def test_damaged_ledgers_keep_detail_readable_with_errors(self):
        self.demo_run()
        runs = self.repo / ".harness" / "runs"
        (runs / "run_broken").mkdir()
        (runs / "run_broken" / "state.json").write_bytes(b"{not json")
        (runs / "run_nostate").mkdir()
        write_json(runs / "run_harnesserror" / "state.json", {"id": "run_harnesserror", "accepted": None, "goal": "g"})
        write_json(runs / "run_typeerror" / "state.json",
                   {"id": "run_typeerror", "goal": "g", "operator_decisions": [{"request_id": []}]})
        server = self.start_server()
        status, _, body = self.request(server, "/api/runs")
        self.assertEqual(status, 200)
        listing = json.loads(body)
        self.assertEqual(len(listing["runs"]), 3)
        self.assertEqual(sorted(item["path"] for item in listing["skipped"]),
                         ["runs/run_broken/state.json", "runs/run_nostate/state.json"])
        self.assertEqual(sorted(listing["incomplete"]),
                         ["runs/run_harnesserror/state.json", "runs/run_typeerror/state.json"])
        status, _, body = self.request(server, "/api/runs/run_broken")
        self.assertEqual(status, 200)
        detail = json.loads(body)
        self.assertIn("无法解析", detail["ledger_error"])
        self.assertIsNone(detail["summary"])
        status, _, _ = self.request(server, "/api/runs/run_nostate")
        self.assertEqual(status, 404)
        for run_id, prefix in (("run_harnesserror", "HarnessError: "), ("run_typeerror", "TypeError: ")):
            status, _, body = self.request(server, f"/api/runs/{run_id}")
            self.assertEqual(status, 200)
            detail = json.loads(body)
            self.assertIsNone(detail["summary"])
            self.assertTrue(detail["summary_error"].startswith(prefix), detail["summary_error"])
            self.assertEqual(detail["run"]["goal"], "g")


class FileRouteTests(DashboardCase):
    def test_fixed_roots_readable_and_everything_else_refused(self):
        run_id, run_dir = self.demo_run()
        ledger = read_json(run_dir / "state.json")
        worker = next(d["id"] for d in ledger["dispatches"] if d["role"] == "worker")
        command = uuid.uuid4().hex
        samples = {
            "verification/tasks/greet/1/retry-2/results.json": b'[{"exit_code": 0}]\n',
            "hooks/greet-1/pre_accept-1/hook-pre_accept-0.stdout": b"hook output\n",
            f"dispatches/{worker}/commands/{command}/output.stdout": b"<script>alert(1)</script>\n<b>bold</b>\n",
            f"dispatches/{worker}/process.stderr": b"<html><body onload=alert(1)>stderr</body></html>\n",
            f"dispatches/{worker}/post-agent-1/hook-post_agent-0.stderr": b"nested hook\n",
            "plans/replan-1.json": b"{}\n",
        }
        for relative, content in samples.items():
            path = run_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        big = b"".join(b"line %08d\n" % i for i in range(10_000))
        (run_dir / "dispatches" / worker / "process.stdout").write_bytes(big)
        (run_dir / "dispatches" / worker / "mcp.json").write_bytes(b"{}\n")
        (run_dir / "dispatches" / worker / "scratch.tmp").write_bytes(b"tmp\n")
        outside = self.repo / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"secret\n")
        os.symlink(outside, run_dir / "recovery")  # fixed root replaced by a directory symlink out of the run
        alias = str(uuid.uuid4())
        os.symlink(run_dir / "dispatches" / worker, run_dir / "dispatches" / alias)  # dispatch directory symlink
        os.symlink(run_dir / "state.json", run_dir / "dispatches" / worker / "linked.txt")  # file symlink
        os.symlink("../../state.json", run_dir / "dispatches" / worker / "relative.txt")
        os.mkfifo(run_dir / "dispatches" / worker / "fifo.stdout")
        server = self.start_server()
        base = f"/api/runs/{run_id}/files/"

        for relative, content in samples.items():
            with self.subTest(path=relative):
                status, headers, body = self.request(server, base + relative)
                self.assertEqual(status, 200)
                self.assertEqual(body, content)
                self.assertEqual(headers["content-type"], "text/plain; charset=utf-8")
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(headers["x-harness-source"], relative)
                self.assertEqual(headers["x-harness-size"], str(len(content)))
                self.assertEqual(headers["x-harness-truncated"], "false")
                self.assertIn("x-harness-sampled-at", headers)
                self.assert_security_headers(headers)
        status, headers, body = self.request(server, base + f"dispatches/{worker}/process.stdout")
        self.assertEqual(status, 200)
        self.assertEqual(len(body), observe.DEFAULT_TAIL)
        self.assertEqual(body, big[-observe.DEFAULT_TAIL:])
        self.assertEqual(headers["x-harness-truncated"], "true")
        self.assertEqual(headers["x-harness-size"], str(len(big)))
        status, _, body = self.request(server, base + f"dispatches/{worker}/process.stdout?tail=10")
        self.assertEqual((status, body), (200, big[-10:]))
        status, _, body = self.request(server, base + f"dispatches/{worker}/process.stdout?tail={observe.MAX_TAIL}")
        self.assertEqual((status, body), (200, big))
        status, _, _ = self.request(server, base + f"dispatches/{worker}/process.stdout?tail={observe.MAX_TAIL + 1}")
        self.assertEqual(status, 400)
        status, headers, body = self.request(server, base + f"dispatches/{worker}/process.stderr?format=json&tail=12")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        payload = json.loads(body)
        self.assertEqual(payload["text"], samples[f"dispatches/{worker}/process.stderr"][-12:].decode())
        self.assertEqual(payload["source"], f"dispatches/{worker}/process.stderr")
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["tail_bytes"], 12)
        self.assertFalse(payload["changed_while_reading"])
        status, headers, body = self.request(server, base + "events.jsonl")
        self.assertEqual(status, 200)
        self.assertEqual(body, (run_dir / "events.jsonl").read_bytes())

        refused = {
            "../state.json": 400, f"dispatches/{worker}/../../state.json": 400, "%2e%2e/state.json": 400,
            "/etc/passwd": 400, "worktrees/x": 400, "state.json/x": 400, "dispatches": 400,
            f"dispatches/{worker.upper()}/prompt.txt": 400, "dispatches/not-a-uuid/prompt.txt": 400,
            f"dispatches/{worker}/commands/not-hex/output.stdout": 400,
            "verification/a/b/c/d/e/f/g/h.txt": 400,
            "run.lock": 403, f"dispatches/{worker}/mcp.json": 403, f"dispatches/{worker}/scratch.tmp": 403,
            "recovery/secret.txt": 403, f"dispatches/{alias}/prompt.txt": 403,
            f"dispatches/{worker}/linked.txt": 403, f"dispatches/{worker}/relative.txt": 403,
            f"dispatches/{worker}/fifo.stdout": 403, f"dispatches/{worker}/commands": 403,
            f"dispatches/{worker}/absent.txt": 404, f"dispatches/{uuid.uuid4()}/prompt.txt": 404,
            "plans/missing.json": 404, "checkpoints/x.json": 404,
        }
        for relative, expected in refused.items():
            with self.subTest(path=relative):
                status, headers, body = self.request(server, base + relative)
                self.assertEqual(status, expected, body)
                self.assertNotIn(b"secret", body)
                self.assert_security_headers(headers)
        self.assertEqual((outside / "secret.txt").read_bytes(), b"secret\n")

        status, _, body = self.request(server, f"/api/runs/{run_id}/files")
        index = json.loads(body)
        paths = {item["path"] for item in index["files"]}
        self.assertTrue(set(samples).issubset(paths))
        self.assertIn("state.json", paths)
        self.assertNotIn("run.lock", paths)
        self.assertFalse(any(p.endswith(("mcp.json", ".tmp")) or "linked" in p or "relative" in p
                             or "fifo" in p or p.startswith(("recovery/", f"dispatches/{alias}/")) for p in paths))
        self.assertGreaterEqual(index["skipped"]["symlink"], 4)
        self.assertGreaterEqual(index["skipped"]["other"], 1)
        self.assertGreaterEqual(index["skipped"]["excluded"], 3)
        self.assertFalse(index["truncated"])
        detail = json.loads(self.request(server, f"/api/runs/{run_id}")[2])
        worker_row = next(row for row in detail["dispatches"] if row["id"] == worker)
        self.assertIn("process.stdout", worker_row["files"])
        self.assertNotIn("mcp.json", worker_row["files"])
        self.assertNotIn("linked.txt", worker_row["files"])
        self.assertNotIn("fifo.stdout", worker_row["files"])


class ReadOnlyTests(DashboardCase):
    def test_repeated_reads_leave_state_directory_byte_identical(self):
        run_id, run_dir = self.demo_run()
        ledger = read_json(run_dir / "state.json")
        worker = next(d["id"] for d in ledger["dispatches"] if d["role"] == "worker")
        state = self.repo / ".harness"
        write_json(state / "operations" / "op_demo.json", {"operation_id": "op_demo", "run_id": run_id, "kind": "run"})
        before = tree_snapshot(state)
        server = self.start_server()
        for _ in range(3):
            for path in ("/api/runs", f"/api/runs/{run_id}", f"/api/runs/{run_id}/events",
                         f"/api/runs/{run_id}/events?tail=100", f"/api/runs/{run_id}/files",
                         f"/api/runs/{run_id}/files/state.json", f"/api/runs/{run_id}/files/events.jsonl?format=json",
                         f"/api/runs/{run_id}/files/dispatches/{worker}/prompt.txt?tail=50",
                         f"/api/runs/{run_id}/files/verification/tasks/greet/1/results.json",
                         f"/api/runs/{run_id}/files/run.lock", f"/api/runs/{run_id}/files/../state.json",
                         "/api/runs/run_missing", "/", "/app.js", "/app.css"):
                self.request(server, path)
        self.assertEqual(tree_snapshot(state), before)
        self.assertEqual(read_json(run_dir / "state.json"), ledger)

    def test_observation_never_invokes_backend_or_processes_and_leaves_lock_free(self):
        run_id, run_dir = self.demo_run()
        lock_path = run_dir / "run.lock"
        lock_before = os.stat(lock_path)
        with contextlib.ExitStack() as stack:
            doubles = {site: stack.enter_context(patch(site)) for site in RUN_PROCESS_SITES}
            doubles["harness.adapters.CLIBackend.invoke"] = stack.enter_context(patch("harness.adapters.CLIBackend.invoke"))
            doubles["subprocess.Popen"] = stack.enter_context(patch("subprocess.Popen"))
            server = self.start_server()
            for path in ("/api/runs", f"/api/runs/{run_id}", f"/api/runs/{run_id}/events", f"/api/runs/{run_id}/files",
                         f"/api/runs/{run_id}/files/state.json", f"/api/runs/{run_id}/files/handoff.md"):
                status, _, _ = self.request(server, path)
                self.assertEqual(status, 200, path)
            for site, double in doubles.items():
                self.assertEqual(double.call_count, 0, site)
            lock_after = os.stat(lock_path)
            self.assertEqual((lock_after.st_ino, lock_after.st_size, lock_after.st_mtime_ns),
                             (lock_before.st_ino, lock_before.st_size, lock_before.st_mtime_ns))
            with lock_path.open("a+") as handle:  # Nobody holds run.lock: the server never took it.
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
        self.assertEqual(sorted(p.name for p in run_dir.iterdir() if p.name.startswith("run.lock")), ["run.lock"])


class ConnectionLimitTests(DashboardCase):
    def test_ninth_slow_client_gets_503_and_service_recovers_after_timeout(self):
        self.demo_run()
        server = self.start_server(socket_timeout=0.5)
        port = server.server_port
        slow = []
        for _ in range(dashboard.MAX_CONNECTIONS):
            client = socket.create_connection(("127.0.0.1", port), timeout=5)  # connects, never sends headers
            self.addCleanup(client.close)
            slow.append(client)
        deadline = time.monotonic() + 5
        while server.active_connections() < dashboard.MAX_CONNECTIONS and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(server.active_connections(), dashboard.MAX_CONNECTIONS)
        ninth = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(ninth.close)
        received = b""
        try:
            while True:
                chunk = ninth.recv(4096)
                if not chunk:
                    break
                received += chunk
        except OSError:
            pass
        self.assertTrue(received.startswith(b"HTTP/1.0 503 "), received)
        head, _, body = received.partition(b"\r\n\r\n")
        head += b"\r\n"  # Restore the terminator of the last header line for exact-line matching.
        self.assertIn(b"\r\nRetry-After: 1\r\n", head)
        self.assertIn(b"\r\nConnection: close\r\n", head)
        self.assertIn(b"\r\nContent-Type: text/plain; charset=utf-8\r\n", head)
        for name, value in dashboard.SECURITY_HEADERS:  # The fixed 503 carries every security header.
            self.assertIn(f"\r\n{name}: {value}\r\n".encode(), head)
        self.assertEqual(int(head.split(b"\r\nContent-Length: ")[1].split(b"\r\n")[0]), len(body))
        self.assertEqual(server.active_connections(), dashboard.MAX_CONNECTIONS)
        deadline = time.monotonic() + 5
        while server.active_connections() > 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(server.active_connections(), 0)
        status, _, _ = self.request(server, "/api/runs")
        self.assertEqual(status, 200)
        for client in slow:
            client.settimeout(2)
            self.assertEqual(client.recv(16), b"")  # closed by the server on timeout, no response written


class ServeEntryTests(RepoCase):
    def test_serve_prints_url_then_exits_zero_on_sigterm(self):
        self.engine().start("Implement greet")
        code = ("import sys\nfrom harness.dashboard import serve\n"
                "sys.exit(serve(sys.argv[1], 0))\n")
        process = subprocess.Popen([sys.executable, "-c", code, str(self.repo / ".harness")], cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            url = process.stdout.readline().strip()
            self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/$")
            port = int(url.rsplit(":", 1)[1].rstrip("/"))
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", "/api/runs")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(len(json.loads(response.read())["runs"]), 1)
            connection.close()
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, "")
        self.assertNotIn("Traceback", stderr)

    def test_add_arguments_and_port_validation(self):
        import argparse
        parser = argparse.ArgumentParser()
        dashboard.add_arguments(parser)
        self.assertEqual(parser.parse_args([]).port, dashboard.DEFAULT_PORT)
        self.assertEqual(parser.parse_args(["--port", "0"]).port, 0)
        for port in (-1, 65536, "8765"):
            with self.assertRaises(Exception):
                dashboard.create_server(self.repo / ".harness", port)


class CLIWiringTests(RepoCase):
    def invoke(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(["--repo", str(self.repo), *args])
        return code, output.getvalue(), errors.getvalue()

    def test_dashboard_subcommand_prints_only_url_and_exits_zero_on_sigterm(self):
        run_id = self.engine().start("Implement greet")["id"]
        process = subprocess.Popen([sys.executable, "-m", "harness", "--repo", str(self.repo), "dashboard", "--port", "0"],
                                   cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            url = process.stdout.readline().strip()
            self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/$")
            port = int(url.rsplit(":", 1)[1].rstrip("/"))
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", "/api/runs")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual([item["id"] for item in json.loads(response.read())["runs"]], [run_id])
            connection.close()
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, "")  # Nothing after the URL line already consumed above.
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("已中断", stderr)
        code, output, errors = self.invoke(["dashboard", "--port", "70000"])
        self.assertEqual((code, output), (1, ""))
        self.assertIn("端口", errors)

    def test_status_list_keeps_fields_order_and_stderr_diagnostics(self):
        engine = self.engine()
        engine.start("Implement greet")
        ledger = read_json(engine.directory / "state.json")
        runs = self.repo / ".harness" / "runs"
        write_json(runs / "run_partial" / "state.json", {"goal": "partial"})
        write_json(runs / "run_shape" / "state.json", [])
        (runs / "run_broken").mkdir()
        (runs / "run_broken" / "state.json").write_text("{")
        (runs / "run_missing").mkdir()
        code, output, errors = self.invoke(["status"])
        self.assertEqual(code, 0, errors)
        values = json.loads(output)
        self.assertEqual(values, [{key: ledger[key] for key in ("id", "status", "goal", "branch")},
                                  {"id": "run_partial", "status": "unknown", "goal": "partial", "branch": None}])
        self.assertEqual([list(item) for item in values], [["id", "status", "goal", "branch"]] * 2)
        printed = runs.resolve()  # The CLI reports the git-resolved absolute path, as before.
        for name in ("run_broken", "run_shape", "run_missing"):
            self.assertIn(f"harness: 跳过无法读取的运行台账 {printed / name / 'state.json'}：", errors)
        self.assertIn(f"harness: 运行台账字段不完整 {printed / 'run_partial' / 'state.json'}", errors)
        self.assertNotIn("Traceback", errors)
        self.assertNotIn("run_partial", output.replace("run_partial", "", 1))  # listed once
        code, output, errors = self.invoke(["status", ledger["id"]])
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output), status_summary(ledger))
        code, output, errors = self.invoke(["status", ledger["id"], "--full"])
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output), ledger)
        code, output, errors = self.invoke(["status", "run_broken"])
        self.assertEqual((code, output), (1, ""))
        self.assertNotIn("Traceback", errors)

    def test_dashboard_entry_spawns_nothing_after_service_starts(self):
        run_id = self.engine().start("Implement greet")["id"]
        real_popen = subprocess.Popen
        spawned, started, results = [], [], []

        def recording_popen(*args, **kwargs):
            argv = args[0] if args else kwargs.get("args")
            spawned.append((time.monotonic(), list(argv)))
            return real_popen(*args, **kwargs)

        def fake_serve_forever(server, poll_interval=0.5):
            # Stand-in for the blocking loop: serve on a helper thread, make real requests
            # from here, then end the way SIGTERM does through cli.main.
            started.append(time.monotonic())
            thread = threading.Thread(target=http.server.ThreadingHTTPServer.serve_forever, args=(server,),
                                      kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                for path in ("/api/runs", f"/api/runs/{run_id}", f"/api/runs/{run_id}/files",
                             f"/api/runs/{run_id}/files/events.jsonl", "/app.js"):
                    results.append((path, http_request(server.server_port, path)[0]))
            finally:
                server.shutdown()
                thread.join(5)
            raise KeyboardInterrupt()

        with contextlib.ExitStack() as stack:
            doubles = {site: stack.enter_context(patch(site)) for site in RUN_PROCESS_SITES}
            doubles["harness.adapters.CLIBackend.invoke"] = stack.enter_context(patch("harness.adapters.CLIBackend.invoke"))
            stack.enter_context(patch("subprocess.Popen", side_effect=recording_popen))
            stack.enter_context(patch.object(dashboard.DashboardServer, "serve_forever", fake_serve_forever))
            code, output, errors = self.invoke(["dashboard", "--port", "0"])
        self.assertEqual(code, 0, errors)
        self.assertRegex(output, r"^http://127\.0\.0\.1:\d+/\n$")
        self.assertNotIn("已中断", errors)
        self.assertEqual(len(started), 1)
        self.assertEqual([status for _, status in results], [200] * 5)
        # Startup resolves the repository root once through git; nothing is spawned afterwards.
        self.assertEqual(len(spawned), 1, spawned)
        self.assertLess(spawned[0][0], started[0])
        self.assertEqual(spawned[0][1][0], "git")
        self.assertIn("rev-parse", spawned[0][1])
        for site, double in doubles.items():
            self.assertEqual(double.call_count, 0, site)

    def test_dashboard_refuses_missing_state_directory_while_status_lists_empty(self):
        self.assertFalse((self.repo / ".harness").exists())
        code, output, errors = self.invoke(["status"])
        self.assertEqual((code, output.strip(), errors), (0, "[]", ""))
        code, output, errors = self.invoke(["dashboard", "--port", "0"])
        self.assertEqual((code, output), (1, ""))
        self.assertIn("观察目录不可用", errors)
        self.assertNotIn("Traceback", errors)
        self.assertFalse((self.repo / ".harness").exists())


class PageScriptTests(RepoCase):
    def test_app_js_renders_list_detail_and_file_views_with_dom_only(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        engine = self.engine()
        run_id = engine.start("Implement greet")["id"]
        state = self.repo / ".harness"
        with observe.Observer(state) as observer:
            listing = observer.list_runs()
            detail = observer.run_detail(run_id)
            index = observer.file_index(run_id)
            content = observer.read_file(run_id, "state.json", 200)
        worker = next(row["id"] for row in detail["dispatches"] if row["role"] == "worker")
        fixture = {"initial_hash": "", "responses": {
            "/api/runs": listing, f"/api/runs/{run_id}": detail, f"/api/runs/{run_id}/files": index,
            f"/api/runs/{run_id}/files/state.json": content},
            "hashes": [f"#/runs/{run_id}", f"#/runs/{run_id}/files/state.json?tail=200", "#/", "#", "#/runs/run_absent"]}
        folder = Path(tempfile.mkdtemp(prefix="harness-page-test-"))
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "app.js").write_text(APP_JS, encoding="utf-8")
        (folder / "harness.js").write_text(PAGE_HARNESS_JS, encoding="utf-8")
        (folder / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
        process = subprocess.run([node, str(folder / "harness.js"), str(folder / "app.js"), str(folder / "fixture.json")],
                                 capture_output=True, text=True, timeout=60, cwd=folder)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        report = json.loads(process.stdout)
        self.assertNotIn("error", report, report)
        phases = {phase["hash"]: phase for phase in report["phases"]}
        self.assertIn("采样时间", report["sampled"])

        home = report["phases"][0]
        self.assertEqual(home["hash"], "")
        self.assertEqual(home["requested"], ["/api/runs"])
        self.assertIn("run 列表（1）", home["text"])
        self.assertIn("Implement greet", home["text"])
        self.assertIn(f"#/runs/{run_id}", home["anchors"])
        for forbidden in ("未知视图", "读取失败"):
            self.assertNotIn(forbidden, home["text"])
        for hash_value in ("#/", "#"):
            self.assertEqual(phases[hash_value]["requested"], ["/api/runs"])
            self.assertIn("run 列表（1）", phases[hash_value]["text"])

        shown = phases[f"#/runs/{run_id}"]
        self.assertEqual(shown["requested"], [f"/api/runs/{run_id}", f"/api/runs/{run_id}/files"])
        for expected in (f"run {run_id}", "费用与调用分类", "CLI 报告的估算费用，非账户账单", worker, "dispatch（",
                         "文件索引", "state.json", "不探测进程", "Implement greet"):
            self.assertIn(expected, shown["text"])
        self.assertNotIn("读取失败", shown["text"])
        self.assertIn(f"#/runs/{run_id}/files/dispatches/{worker}/prompt.txt", shown["anchors"])

        viewer = phases[f"#/runs/{run_id}/files/state.json?tail=200"]
        self.assertEqual(viewer["requested"], [f"/api/runs/{run_id}/files/state.json?format=json&tail=200"])
        self.assertIn("文件 state.json", viewer["text"])
        self.assertIn(content["text"], viewer["text"])

        absent = phases["#/runs/run_absent"]
        self.assertEqual(absent["requested"], ["/api/runs/run_absent"])
        self.assertIn("读取失败：HTTP 404", absent["text"])


if __name__ == "__main__":
    unittest.main()
