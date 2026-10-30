"""Execute the dashboard script with a deterministic browser lifecycle under Node."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.dashboard_page import APP_JS
from test_dashboard import PAGE_HARNESS_JS


# Reuse the existing minimal DOM, but control network completions and browser timers.
BROWSER_JS = PAGE_HARNESS_JS.split('vm.runInThisContext(')[0] + r'''
const assert = require("assert").strict;
byId.connection = new Node("div");
byId.auto.checked = false;
const pending = [];
const timers = new Map();
const sources = [];
let timerId = 0;
let clock = 0;
let intervalCalls = 0;
globalThis.setTimeout = function (fn, delay) {
  const id = ++timerId; timers.set(id, {fn, at: clock + delay}); return id;
};
globalThis.clearTimeout = function (id) { timers.delete(id); };
globalThis.setInterval = function () { intervalCalls++; return ++timerId; };
globalThis.clearInterval = function () {};
globalThis.fetch = function (url) {
  requested.push(url);
  return new Promise(function (resolve) { pending.push({url, resolve}); });
};
globalThis.EventSource = class {
  constructor(url) { this.url = url; this.closed = false; this.listeners = {}; sources.push(this); }
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
  close() { this.closed = true; }
  emit(name) { (this.listeners[name] || []).forEach(fn => fn({})); }
};
function fire(target, name, event = {}) {
  const listeners = target === window ? windowListeners : target.listeners;
  (listeners[name] || []).forEach(fn => fn(event));
}
function navigate(hash) { location.hash = hash; fire(window, "hashchange"); }
function follow(checked) { byId.auto.checked = checked; fire(byId.auto, "change", {target: byId.auto}); }
async function flush() { for (let i = 0; i < 20; i++) { await Promise.resolve(); } }
async function advance(ms) {
  const end = clock + ms;
  for (let i = 0; i < 100; i++) {
    const due = Array.from(timers).filter(([id, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
    if (!due) { clock = end; await flush(); return; }
    clock = due[1].at; timers.delete(due[0]); due[1].fn(); await flush();
  }
  throw Error("unbounded timer loop");
}
function answer(index, body, ok = true) {
  pending[index].resolve({ok, status: ok ? 200 : 404, text: () => Promise.resolve(JSON.stringify(body))});
}
vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
(async function () {
SCENARIO
  assert.equal(intervalCalls, 0, "dashboard must not install interval polling");
})().catch(function (error) { console.error(error.stack); process.exitCode = 1; });
'''


class DashboardPageEventTests(unittest.TestCase):
    def run_browser(self, scenario):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "app.js").write_text(APP_JS, encoding="utf-8")
            (folder / "browser.js").write_text(BROWSER_JS.replace("SCENARIO", scenario), encoding="utf-8")
            (folder / "fixture.json").write_text('{"initial_hash":"#/","responses":{}}', encoding="utf-8")
            result = subprocess.run(
                [node, str(folder / "browser.js"), str(folder / "app.js"), str(folder / "fixture.json")],
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_detail_follow_registration_coalesces_changes_without_polling(self):
        self.run_browser(r'''
  assert.equal(sources.length, 0);
  follow(true);
  assert.equal(sources.length, 0, "list is manual even when follow is checked");
  navigate("#/runs/run_one/files/events.jsonl");
  assert.equal(sources.length, 0, "file is manual");
  follow(false);
  navigate("#/runs/run_one");
  assert.equal(sources.length, 0, "unchecked detail is manual");
  follow(true);
  assert.equal(sources.length, 1);
  assert.equal(sources[0].url, "/api/runs/run_one/stream");
  const before = requested.length;
  await advance(60000);
  assert.equal(requested.length, before, "idle stream must not poll");
  sources[0].emit("ready");
  sources[0].emit("change"); sources[0].emit("change");
  await advance(99);
  assert.equal(requested.length, before);
  await advance(1);
  assert.equal(requested.length, before + 1, "ready and changes share one refresh");
  assert.equal(requested.at(-1), "/api/runs/run_one");
  assert.equal(sources.length, 1, "refresh keeps registered stream");
  await advance(60000);
  assert.equal(requested.length, before + 1);
''')

    def test_route_change_disconnect_and_explicit_reconnect(self):
        self.run_browser(r'''
  follow(true); navigate("#/runs/run_one");
  const old = sources[0];
  old.emit("change");
  navigate("#/runs/run_two");
  assert.equal(old.closed, true);
  assert.equal(sources.length, 2);
  const before = requested.length;
  old.emit("ready"); old.emit("change");
  await advance(1000);
  assert.equal(requested.length, before, "closed source and canceled timer cannot refresh");
  sources[1].onerror();
  assert.equal(sources[1].closed, true);
  await advance(60000);
  assert.equal(sources.length, 2, "disconnect cannot start reconnect loop");
  assert.equal(requested.length, before);
  fire(byId.refresh, "click");
  assert.equal(sources.length, 3, "explicit refresh reconnects");
  sources[2].emit("unavailable");
  assert.equal(sources[2].closed, true);
  await advance(60000);
  assert.equal(sources.length, 3);
  fire(byId.refresh, "click");
  follow(false);
  assert.equal(sources[3].closed, true);
''')

    def test_page_cache_restore_resubscribes_and_refreshes(self):
        self.run_browser(r'''
  follow(true); navigate("#/runs/run_one");
  sources[0].emit("ready");
  fire(window, "pagehide");
  assert.equal(sources[0].closed, true);
  const before = requested.length;
  await advance(1000);
  assert.equal(requested.length, before, "pagehide cancels pending refresh");
  fire(window, "pageshow", {persisted: false});
  assert.equal(sources.length, 1);
  fire(window, "pageshow", {persisted: true});
  assert.equal(sources.length, 2, "back-forward cache restore must resubscribe");
  assert.equal(requested.length, before + 1);
  sources[1].emit("ready");
  await advance(100);
  assert.equal(requested.length, before + 2, "restored subscription receives ready refresh");
''')

    def test_stale_fetch_success_and_error_do_not_replace_new_route(self):
        self.run_browser(r'''
  navigate("#/runs/run_one/files/events.jsonl");
  navigate("#/runs/run_two/files/events.jsonl");
  answer(2, {source: "newest-file", text: "newest-content"});
  await flush();
  assert.ok(byId.main.textContent.includes("newest-content"));
  answer(1, {source: "stale-file", text: "stale-content"});
  answer(0, {error: "stale-error"}, false);
  await flush();
  assert.ok(byId.main.textContent.includes("newest-content"));
  assert.ok(!byId.main.textContent.includes("stale-content"));
  assert.ok(!byId.main.textContent.includes("stale-error"));
''')
