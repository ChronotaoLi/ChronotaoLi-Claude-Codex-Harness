"""Behavior of the dashboard presentation under the simulated DOM (Node, no real browser).

These tests execute the shipped APP_JS against the minimal fake DOM from test_dashboard,
with controlled fetch completions, SSE sources and timers from test_dashboard_page_events.
They check progressive disclosure, tab keyboard navigation, UI-state retention across
same-run event refreshes, prominence of actionable problems and projection semantics.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import observe, quota
from harness.dashboard_page import APP_JS
from harness.versions import RUN_SCHEMA_VERSION
from test_dashboard_page_events import BROWSER_JS

WORKER = "0b7e6d3c-1f0a-4c55-9d43-2f6f0b1c9a01"
REVIEWER = "6a0f8e2d-77b1-4e0c-8f7c-3b5d2a9e4c02"
LATER = "9c4d1e7a-0b2f-4a8e-b6d5-1e3f7a2c8d03"
ENV_DISPATCH = "2d8e4f6a-3c1b-4e7d-9a0f-5b6c7d8e9f04"

HELPERS = r'''
function walk(node, fn) { fn(node); node.childNodes.forEach(function (c) { walk(c, fn); }); }
function all(pred, root) { const out = []; walk(root || byId.main, function (n) { if (pred(n)) { out.push(n); } }); return out; }
function one(pred, root) { const found = all(pred, root); assert.equal(found.length, 1, "expected exactly one match, got " + found.length); return found[0]; }
function attrIs(name, value) { return function (n) { return typeof n.getAttribute === "function" && n.getAttribute(name) === value; }; }
function tagIs(tag) { return function (n) { return n.tagName === tag; }; }
function attached(node) { while (node) { if (node === byId.main) { return true; } node = node.parentNode; } return false; }
// Text a sighted user sees without interacting: skips hidden panels and closed disclosure bodies.
function visibleText(node) {
  if (node.hidden) { return ""; }
  if (node.tagName === "details" && !node.open) {
    const summary = node.childNodes.find(function (c) { return c.tagName === "summary"; });
    return node.ownText + (summary ? visibleText(summary) : "");
  }
  return node.ownText + node.childNodes.map(visibleText).join("");
}
function toggle(details, open) { details.open = open; fire(details, "toggle"); }
function key(name) { return {key: name, preventDefault: function () {}}; }
// Resolve the oldest unresolved request for this path with a JSON body.
async function respond(path, body, ok = true, status = ok ? 200 : 404) {
  const entry = pending.find(function (p) { return !p.done && p.url.split("?")[0] === path; });
  assert.ok(entry, "no pending request for " + path + "; requested: " + JSON.stringify(requested));
  entry.done = true;
  entry.resolve({ok: ok, status: status, text: function () { return Promise.resolve(JSON.stringify(body)); }});
  await flush();
}
const D = fixture.data;
const scrolls = [];
window.scrollY = 0;
window.scrollTo = function (x, y) { scrolls.push(y); window.scrollY = y; };
// Model element scroll offsets as a browser does: without a box (detached, in a hidden panel or in a
// closed disclosure) reads return 0 and writes are ignored, while an element that is merely hidden
// keeps its own offset for when it is shown again.
function hasBox(node) {
  let child = null;
  for (let n = node; n; child = n, n = n.parentNode) {
    if (n.hidden) { return false; }
    if (child && n.tagName === "details" && !n.open && child.tagName !== "summary") { return false; }
    if (n === byId.main) { return true; }
  }
  return false;
}
["scrollTop", "scrollLeft"].forEach(function (name) {
  const slot = Symbol(name);
  Object.defineProperty(Node.prototype, name, {
    configurable: true,
    get() { return hasBox(this) ? (this[slot] || 0) : 0; },
    set(value) { if (hasBox(this)) { this[slot] = Math.max(0, Number(value) || 0); } }
  });
});
// A user scroll: set the offset and deliver the non-bubbling scroll event to capture listeners on main.
function userScroll(node, top, left) {
  if (top !== null) { node.scrollTop = top; }
  if (left !== null) { node.scrollLeft = left; }
  fire(byId.main, "scroll", {target: node});
}
'''


def dispatch(ident, role="worker", task="t2", status="completed", outcome="completed", error=None,
             cost=None, provider="claude", invoked_at=1000.0):
    usage = {"provider": provider, "reported_cost_usd": cost, "input_tokens": 10} if cost is not None else None
    return {"id": ident, "role": role, "kind": "implement" if role == "worker" else "review", "task_id": task,
            "status": status, "process_state": "exited", "invoked": True, "invoked_at": invoked_at,
            "finished_at": invoked_at + 60, "pgid": 4242, "exit_confirmed": True, "error": error,
            "worker_outcome": outcome if role == "worker" else None, "phase": None, "provider": provider,
            "model_id": "model-under-test", "provider_usage": usage,
            "invocation_class": "usage_recorded" if usage else "invoked_no_usage", "invocation_reason": None,
            "startup_unconfirmed": False, "files": ["prompt.txt", "process.stdout"]}


def event(time, name, task="t2", dispatch_id=None, **extra):
    record = {"time": time, "event": name, "run_id": "run_x", "task_id": task}
    if dispatch_id:
        record["dispatch_id"] = dispatch_id
    record.update(extra)
    return record


def stop_value(detail_text, kind="unclassified", reason="untyped_harness_error", title="未分类的停止；不推测恢复动作",
               exit_code=1, exits=()):
    """A stop projection as cli.status_summary() reports it."""
    return {"kind": kind, "reason": reason, "title": title, "exit_code": exit_code, "exits": list(exits),
            "data": {}, "source": {}, "detail": detail_text}


def quota_block(samples=(), *, truncated=False, damaged=0, omitted=0):
    """The run detail's quota block, as observe.Observer.quota_block() builds it from saved samples."""
    view = quota.quota_view(list(samples), truncated)
    return {"notes": view["notes"], "damaged_count": damaged, "omitted": omitted, "providers": view["providers"]}


def detail(run_id, goal, *, status="running", summary=None, dispatches=(), events=(), stop=None, quota_view=None):
    rows = list(dispatches)
    records = list(events)
    last = records[-1]["time"] if records else None
    base = {"id": run_id, "status": status, "goal": goal, "branch": "harness/" + run_id, "worktree": "/tmp/wt",
            "current_task": "t2", "accepted_tasks": ["t1"],
            "budgets": {"work_attempts": {"t1": 1, "t2": 2}},
            "convergence": {"advisory_only": True, "warnings": [], "truncated": False, "suggestion": "advisory"},
            "worker_outcomes": {"completed": 1, "task_blocked": 0, "environment_blocked": 0, "operator_blocked": 0,
                                "continuation_blocked": 0, "runtime_failed": 0, "unknown": 0}}
    if stop is not None:
        base["stop"] = stop
    base.update(summary or {})
    run = {"id": run_id, "status": status, "goal": goal, "branch": "harness/" + run_id, "worktree": "/tmp/wt",
           "repo": "/tmp/repo", "commander": "fable", "current_task": "t2",
           "created_by_operation": "op_1", "config_path": "/tmp/harness.toml", "expected_head": "abc",
           "base_commit": "def", "worktree_removed": False}
    return {"id": run_id, "sampled_at": 2000.0, "sampled_at_utc": "2026-11-09T00:00:00Z", "ledger_error": None,
            "summary": base, "summary_error": None, "schema_version": RUN_SCHEMA_VERSION, "unavailable": [], "run": run,
            "budgets": {"agent_dispatches": 5, "attempts": {"t1": 1, "t2": 2}, "attempts_total": 3,
                        "limits": {"max_attempts": 4, "max_agent_dispatches": 40, "agent_timeout_seconds": 600}},
            "tasks": [{"id": "t1", "title": "Prepare schema", "depends_on": [], "accepted": True},
                      {"id": "t2", "title": "Build service", "depends_on": ["t1"], "accepted": False}],
            "invalid_tasks": 0, "dispatches": rows, "invalid_dispatches": 0,
            "invocations": observe.invocation_summary(rows),
            "quota": quota_view if quota_view is not None else quota_block(),
            "timing": {"first_event_time": 900.0, "last_event_time": last, "basis": "events"},
            "events": {"exists": True, "tail_bytes": 65536, "size": 4096, "truncated": False,
                       "changed_while_reading": False, "records": records, "unparsed_tail": False,
                       "invalid_lines": 0, "last_time": last},
            "operations": {"records": [], "scanned": 0, "invalid": 0, "truncated": False},
            "notes": {"invoked": "invoked-note", "process": "process-note"}}


def file_index(run_id, *paths):
    return {"id": run_id, "sampled_at": 2000.0, "truncated": False, "limit": 2000,
            "skipped": {"symlink": 0, "other": 0, "excluded": 0, "unaddressable": 0},
            "files": [{"path": path, "size": 10, "mtime": 1000.0} for path in paths]}


LONG_GOAL = ("Migrate the billing service <img src=x onerror=alert(1)><script>alert(2)</script>\n"
             + "详细要求" * 900 + "\nGOAL-END-MARKER")


class DashboardPresentationTests(unittest.TestCase):
    def run_browser(self, data, scenario):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "app.js").write_text(APP_JS, encoding="utf-8")
            (folder / "browser.js").write_text(BROWSER_JS.replace("SCENARIO", HELPERS + scenario), encoding="utf-8")
            (folder / "fixture.json").write_text(json.dumps({"initial_hash": "#/", "responses": {}, "data": data}),
                                                 encoding="utf-8")
            result = subprocess.run(
                [node, str(folder / "browser.js"), str(folder / "app.js"), str(folder / "fixture.json")],
                capture_output=True, text=True, timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_long_goal_is_bounded_by_default_with_full_text_on_demand(self):
        data = {"goal": LONG_GOAL,
                "list": {"sampled_at": 2000.0, "runs": [{"id": "run_long", "status": "running", "goal": LONG_GOAL,
                                                         "branch": "b", "run_dir": "run_long",
                                                         "schema_version": RUN_SCHEMA_VERSION,
                                                         "last_event_time": 1990.0}],
                         "skipped": [], "incomplete": [], "unsupported": []},
                "detail": detail("run_long", LONG_GOAL, events=[event(1990.0, "dispatch_started")]),
                "files": file_index("run_long", "state.json")}
        self.run_browser(data, r'''
  await respond("/api/runs", D.list);
  for (const view of ["list", "detail"]) {
    if (view === "detail") {
      navigate("#/runs/run_long");
      await respond("/api/runs/run_long", D.detail);
      await respond("/api/runs/run_long/files", D.files);
    }
    const before = visibleText(byId.main);
    assert.ok(before.includes("Migrate the billing service"), view + ": excerpt is visible");
    assert.ok(!before.includes("GOAL-END-MARKER"), view + ": full goal is not shown by default");
    assert.ok(before.length < 2000, view + ": default view stays bounded, got " + before.length);
    // The original text is kept intact, unparsed, behind a native keyboard-operable disclosure.
    const full = one(function (n) { return n.tagName === "pre" && n.textContent === D.goal; });
    let box = full.parentNode;
    assert.equal(box.tagName, "details");
    assert.ok(!box.open, view + ": disclosure starts closed");
    assert.equal(box.childNodes[0].tagName, "summary");
    assert.equal(all(tagIs("img")).length + all(tagIs("script")).length, 0, "goal text is never parsed as markup");
    toggle(box, true);
    assert.ok(visibleText(byId.main).includes("GOAL-END-MARKER"), view + ": opened disclosure shows full text");
  }
''')

    def test_tabs_keyboard_and_view_state_survive_same_run_refresh_only(self):
        first = detail("run_one", LONG_GOAL, dispatches=[dispatch(WORKER)],
                       events=[event(1990.0, "dispatch_started", dispatch_id=WORKER)])
        second = detail("run_one", LONG_GOAL, dispatches=[dispatch(WORKER), dispatch(LATER, invoked_at=1995.0)],
                        events=[event(1990.0, "dispatch_started", dispatch_id=WORKER),
                                event(1995.0, "task_accepted", task="t1", marker="SECOND-REFRESH")])
        other = detail("run_two", "Other goal", dispatches=[dispatch(REVIEWER, role="reviewer")])
        data = {"first": first, "second": second, "other": other,
                "files": file_index("run_one", "state.json", "events.jsonl", "plans/replan-1.json"),
                "other_files": file_index("run_two", "state.json")}
        self.run_browser(data, r'''
  follow(true);
  navigate("#/runs/run_one");
  await respond("/api/runs/run_one", D.first);
  await respond("/api/runs/run_one/files", D.files);
  const source = sources.at(-1);
  let tabs = all(attrIs("role", "tab"));
  assert.ok(tabs.length >= 5, "secondary detail is grouped into tabs");
  const panels = all(attrIs("role", "tabpanel"));
  assert.equal(panels.length, tabs.length);
  assert.equal(panels.filter(function (p) { return !p.hidden; }).length, 1, "one panel visible at a time");
  assert.equal(tabs.filter(function (t) { return t.getAttribute("aria-selected") === "true"; }).length, 1);
  assert.deepEqual(tabs.map(function (t) { return t.tabIndex; }).filter(function (i) { return i === 0; }), [0], "roving tabindex");
  // Keyboard navigation moves selection and focus.
  fire(tabs[0], "keydown", key("ArrowRight"));
  assert.equal(tabs[1].getAttribute("aria-selected"), "true");
  assert.equal(document.activeElement, tabs[1]);
  fire(tabs[1], "keydown", key("End"));
  assert.equal(document.activeElement, tabs.at(-1));
  fire(tabs.at(-1), "keydown", key("ArrowRight"));
  assert.equal(document.activeElement, tabs[0], "arrow navigation wraps");
  fire(tabs[0], "keydown", key("Home"));
  const cost = one(attrIs("data-tab", "budget"));
  fire(cost, "click");
  const costPanel = one(attrIs("data-panel", "budget"));
  assert.equal(costPanel.hidden, false);
  assert.equal(cost.getAttribute("aria-controls"), costPanel.id);
  // Open disclosures, type a file filter, scroll, and keep focus on a tab.
  toggle(one(attrIs("data-ui-key", "dispatch:" + D.first.dispatches[0].id)), true);
  toggle(one(attrIs("data-ui-key", "goal")), true);
  const filter = one(attrIs("data-ui-key", "files:filter"));
  filter.value = "replan"; fire(filter, "input");
  assert.ok(one(attrIs("data-panel", "files")).textContent.includes("plans/replan-1.json"));
  assert.ok(!one(attrIs("data-panel", "files")).textContent.includes("events.jsonl"));
  window.scrollY = 640;
  cost.focus();
  // A same-run change event refreshes in place.
  const before = requested.length;
  source.emit("change");
  await advance(100);
  assert.equal(requested.length, before + 1);
  await respond("/api/runs/run_one", D.second);
  assert.equal(attached(cost), false, "content was re-rendered");
  assert.ok(visibleText(byId.main).includes("任务已接受"), "new activity is visible");
  const cost2 = one(attrIs("data-tab", "budget"));
  assert.equal(cost2.getAttribute("aria-selected"), "true", "selected tab retained");
  assert.equal(one(attrIs("data-panel", "budget")).hidden, false);
  assert.equal(document.activeElement, cost2, "keyboard focus retained");
  assert.equal(one(attrIs("data-ui-key", "dispatch:" + D.first.dispatches[0].id)).open, true, "open disclosure retained");
  assert.equal(one(attrIs("data-ui-key", "goal")).open, true);
  assert.notEqual(one(attrIs("data-ui-key", "dispatch:" + D.second.dispatches[1].id)).open, true, "new rows start closed");
  assert.equal(scrolls.at(-1), 640, "scroll position retained");
  // The cached file index renders immediately with the retained filter, then the fresh one replaces it.
  assert.equal(one(attrIs("data-ui-key", "files:filter")).value, "replan");
  await respond("/api/runs/run_one/files", D.files);
  assert.equal(one(attrIs("data-ui-key", "files:filter")).value, "replan");
  assert.ok(!one(attrIs("data-panel", "files")).textContent.includes("events.jsonl"));
  // Another run never inherits this run's view state, and stale content is not shown while loading.
  navigate("#/runs/run_two");
  assert.ok(!byId.main.textContent.includes("run_one"), "previous run content cleared on route change");
  await respond("/api/runs/run_two", D.other);
  await respond("/api/runs/run_two/files", D.other_files);
  assert.equal(scrolls.at(-1), 0, "new run starts at top");
  const selected = one(function (n) { return n.getAttribute && n.getAttribute("role") === "tab" && n.getAttribute("aria-selected") === "true"; });
  assert.equal(selected, all(attrIs("role", "tab"))[0], "default tab for another run");
  assert.equal(all(function (n) { return n.tagName === "details" && n.open; }).length, 0, "no disclosure carried over");
  assert.equal(one(attrIs("data-ui-key", "files:filter")).value, "");
''')

    def test_refresh_preserves_offscreen_focus_selection_and_missing_file_fallback(self):
        data = {"detail": detail("run_one", "Goal"),
                "files": file_index("run_one", "state.json", "events.jsonl"),
                "removed": file_index("run_one", "state.json")}
        self.run_browser(data, r'''
  // Model the browser's default focus scrolling, which the basic DOM omits.
  let focusCalls = 0;
  Node.prototype.focus = function (options) {
    focusCalls++;
    document.activeElement = this;
    if (!options || !options.preventScroll) { window.scrollY = 900; }
  };
  Node.prototype.setSelectionRange = function (start, end, direction) {
    this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction;
  };
  follow(true); navigate("#/runs/run_one");
  await respond("/api/runs/run_one", D.detail);
  await respond("/api/runs/run_one/files", D.files);
  const source = sources.at(-1);
  async function refreshDetail() {
    source.emit("change"); await advance(100);
    await respond("/api/runs/run_one", D.detail);
  }
  fire(one(attrIs("data-tab", "files")), "click");
  one(attrIs("data-panel", "files")).focus();
  window.scrollY = 0;
  await refreshDetail();
  assert.equal(document.activeElement, one(attrIs("data-panel", "files")), "panel focus retained");
  assert.equal(window.scrollY, 0, "restoring offscreen focus does not move reading position");
  const beforeIndex = focusCalls;
  await respond("/api/runs/run_one/files", D.files);
  assert.equal(focusCalls, beforeIndex, "index update does not refocus outside its replaced region");

  one(attrIs("data-ui-key", "file:events.jsonl")).focus();
  window.scrollY = 123;
  await refreshDetail();
  assert.equal(document.activeElement, one(attrIs("data-ui-key", "file:events.jsonl")), "file link focus retained");
  assert.equal(window.scrollY, 123);
  await respond("/api/runs/run_one/files", D.removed);
  assert.equal(document.activeElement, one(attrIs("data-panel", "files")), "removed file falls back to its panel");
  assert.equal(window.scrollY, 123);

  let filter = one(attrIs("data-ui-key", "files:filter"));
  filter.value = "state"; fire(filter, "input"); filter.focus();
  filter.setSelectionRange(1, 3, "backward"); window.scrollY = 45;
  await refreshDetail();
  filter = one(attrIs("data-ui-key", "files:filter"));
  assert.equal(document.activeElement, filter);
  assert.deepEqual([filter.selectionStart, filter.selectionEnd, filter.selectionDirection], [1, 3, "backward"]);
  await respond("/api/runs/run_one/files", D.files);
  filter = one(attrIs("data-ui-key", "files:filter"));
  assert.equal(document.activeElement, filter);
  assert.deepEqual([filter.selectionStart, filter.selectionEnd, filter.selectionDirection], [1, 3, "backward"]);
  assert.equal(window.scrollY, 45);
  await refreshDetail();
  await respond("/api/runs/run_one/files", {error: "unavailable"}, false);
  assert.equal(document.activeElement, one(attrIs("data-panel", "files")), "index error keeps focus in a visible panel");
  assert.equal(window.scrollY, 45);
''')

    def test_nested_scroll_survives_refresh_and_delayed_index_without_cross_run_leak(self):
        data = {"detail": detail("run_one", LONG_GOAL),
                "other": detail("run_two", LONG_GOAL),
                "files": file_index("run_one", "state.json", "events.jsonl"),
                "other_files": file_index("run_two", "state.json")}
        self.run_browser(data, r'''
  follow(true); navigate("#/runs/run_one");
  await respond("/api/runs/run_one", D.detail);
  await respond("/api/runs/run_one/files", D.files);
  toggle(one(attrIs("data-ui-key", "goal")), true);
  fire(one(attrIs("data-tab", "files")), "click");
  const original = one(attrIs("data-scroll-key", "text:goal"));
  original.scrollTop = 540;
  one(attrIs("data-scroll-key", "tabs")).scrollLeft = 137;
  one(attrIs("data-scroll-key", "table:files")).scrollLeft = 83;
  window.scrollY = 260;
  sources.at(-1).emit("change"); await advance(100);
  await respond("/api/runs/run_one", D.detail);
  assert.equal(attached(original), false, "new content replaced the old scroller");
  assert.equal(one(attrIs("data-scroll-key", "text:goal")).scrollTop, 540);
  assert.equal(one(attrIs("data-scroll-key", "tabs")).scrollLeft, 137);
  assert.equal(one(attrIs("data-scroll-key", "table:files")).scrollLeft, 83);
  assert.equal(window.scrollY, 260);
  // Use the user's latest position when the deferred index replaces the cached table.
  one(attrIs("data-scroll-key", "table:files")).scrollLeft = 94;
  await respond("/api/runs/run_one/files", D.files);
  assert.equal(one(attrIs("data-scroll-key", "table:files")).scrollLeft, 94);
  assert.equal(one(attrIs("data-scroll-key", "text:goal")).scrollTop, 540);
  assert.equal(one(attrIs("data-scroll-key", "tabs")).scrollLeft, 137);
  navigate("#/runs/run_two");
  await respond("/api/runs/run_two", D.other);
  await respond("/api/runs/run_two/files", D.other_files);
  assert.equal(one(attrIs("data-scroll-key", "text:goal")).scrollTop || 0, 0);
  assert.equal(one(attrIs("data-scroll-key", "tabs")).scrollLeft || 0, 0);
  assert.equal(one(attrIs("data-scroll-key", "table:files")).scrollLeft || 0, 0);
''')

    def test_hidden_panel_and_closed_disclosure_positions_survive_refresh_until_shown(self):
        data = {"detail": detail("run_one", LONG_GOAL),
                "other": detail("run_two", LONG_GOAL),
                "files": file_index("run_one", "state.json", "events.jsonl"),
                "other_files": file_index("run_two", "state.json")}
        self.run_browser(data, r'''
  follow(true); navigate("#/runs/run_one");
  await respond("/api/runs/run_one", D.detail);
  await respond("/api/runs/run_one/files", D.files);
  // Read the file table sideways, then leave its tab: the hidden table has no box and reads 0.
  fire(one(attrIs("data-tab", "files")), "click");
  userScroll(one(attrIs("data-scroll-key", "table:files")), null, 70);
  fire(one(attrIs("data-tab", "events")), "click");
  assert.equal(one(attrIs("data-scroll-key", "table:files")).scrollLeft, 0, "hidden container reads 0");
  // Read into the full goal, then close it again.
  toggle(one(attrIs("data-ui-key", "goal")), true);
  userScroll(one(attrIs("data-scroll-key", "text:goal")), 300, null);
  toggle(one(attrIs("data-ui-key", "goal")), false);
  // A same-run change and the deferred index rebuild both containers while they are not shown.
  sources.at(-1).emit("change"); await advance(100);
  await respond("/api/runs/run_one", D.detail);
  await respond("/api/runs/run_one/files", D.files);
  assert.equal(one(attrIs("data-panel", "events")).hidden, false, "selected tab retained");
  fire(one(attrIs("data-tab", "files")), "click");
  assert.equal(one(attrIs("data-scroll-key", "table:files")).scrollLeft, 70, "tab shows its last position");
  toggle(one(attrIs("data-ui-key", "goal")), true);
  assert.equal(one(attrIs("data-scroll-key", "text:goal")).scrollTop, 300, "disclosure shows its last position");
  // A newer position read while shown replaces the remembered one across the next refresh.
  userScroll(one(attrIs("data-scroll-key", "text:goal")), 120, null);
  fire(byId.refresh, "click");
  await respond("/api/runs/run_one", D.detail);
  assert.equal(one(attrIs("data-scroll-key", "text:goal")).scrollTop, 120);
  assert.equal(one(attrIs("data-scroll-key", "table:files")).scrollLeft, 70);
  await respond("/api/runs/run_one/files", D.files);
  // Another run starts fresh even when the same tab and disclosure are opened there.
  navigate("#/runs/run_two");
  await respond("/api/runs/run_two", D.other);
  await respond("/api/runs/run_two/files", D.other_files);
  fire(one(attrIs("data-tab", "files")), "click");
  toggle(one(attrIs("data-ui-key", "goal")), true);
  assert.equal(one(attrIs("data-scroll-key", "table:files")).scrollLeft, 0);
  assert.equal(one(attrIs("data-scroll-key", "text:goal")).scrollTop, 0);
''')

    def test_same_file_refresh_keeps_content_scroll_and_focus_but_other_file_resets(self):
        data = {"file": {"source": "state.json", "size": 12000, "tail_bytes": 12000,
                         "sampled_at": 2000, "truncated": False, "changed_while_reading": False,
                         "text": "line\n" * 1000}}
        self.run_browser(data, r'''
  navigate("#/runs/run_one/files/state.json");
  await respond("/api/runs/run_one/files/state.json", D.file);
  const old = one(attrIs("data-scroll-key", "file-body"));
  old.focus(); old.scrollTop = 430; old.scrollLeft = 17; window.scrollY = 100;
  fire(byId.refresh, "click");
  await respond("/api/runs/run_one/files/state.json", D.file);
  const refreshed = one(attrIs("data-scroll-key", "file-body"));
  assert.notEqual(refreshed, old);
  assert.equal(document.activeElement, refreshed);
  assert.equal(refreshed.scrollTop, 430); assert.equal(refreshed.scrollLeft, 17);
  assert.equal(window.scrollY, 100);
  navigate("#/runs/run_one/files/events.jsonl");
  await respond("/api/runs/run_one/files/events.jsonl", Object.assign({}, D.file, {source: "events.jsonl"}));
  const other = one(attrIs("data-scroll-key", "file-body"));
  assert.equal(other.scrollTop || 0, 0); assert.equal(other.scrollLeft || 0, 0);
  assert.equal(window.scrollY, 0);
''')

    def test_actionable_problems_lead_the_detail_and_vanish_when_none(self):
        question = "是否允许扩大到 config 目录？" + "背景说明" * 200 + "QUESTION-END-MARKER"
        problems = detail("run_bad", "Ship it", status="blocked", stop=stop_value("boom: provider failure"),
                          dispatches=[dispatch(WORKER, status="failed", outcome=None, error="worker crashed")],
                          summary={
                              "pending_decisions": [{"id": "req-1", "task_id": "t2", "kind": "operator",
                                                     "question": question, "artifact": "/x/decisions/req-1.json",
                                                     "artifact_digest": "0" * 64}],
                              "last_environment_blocker": {"task_id": "t2", "dispatch_id": ENV_DISPATCH,
                                                           "blockers": ["缺少 DATABASE_URL"], "stale": False},
                              "convergence": {"advisory_only": True, "truncated": False, "suggestion": "advisory",
                                              "warnings": [{"kind": "task_rework", "task_id": "t2", "work_attempts": 3,
                                                            "review_rejections": 2, "recurring_findings": ["F-17"]}]}})
        calm = detail("run_ok", "Ship it", summary={
            "last_environment_blocker": {"task_id": "t1", "dispatch_id": ENV_DISPATCH,
                                         "blockers": ["HISTORIC-BLOCKER"], "stale": True}})
        data = {"problems": problems, "calm": calm, "question": question}
        self.run_browser(data, r'''
  navigate("#/runs/run_bad");
  await respond("/api/runs/run_bad", D.problems);
  const attention = one(attrIs("data-attention", "true"));
  const order = byId.main.childNodes;
  const tabsSection = order.find(function (n) { return all(attrIs("role", "tablist"), n).length; });
  assert.ok(order.indexOf(attention) >= 0 && order.indexOf(attention) < order.indexOf(tabsSection),
    "problems come before secondary detail");
  assert.ok(order.indexOf(attention) <= 1, "problems directly follow the run header");
  const shown = visibleText(attention);
  for (const expected of ["boom: provider failure", "缺少 DATABASE_URL", "req-1", "F-17", "worker crashed", "run_bad"]) {
    assert.ok(shown.includes(expected), "visible without interaction: " + expected);
  }
  assert.ok(shown.includes("是否允许扩大到 config 目录？"), "question excerpt visible");
  assert.ok(!shown.includes("QUESTION-END-MARKER"), "long question bounded");
  one(function (n) { return n.tagName === "pre" && n.textContent === D.question; }, attention);
  const tones = attention.childNodes.at(-1).childNodes.map(function (n) { return n.className; });
  const firstWarn = tones.findIndex(function (c) { return c.includes("tone-warn"); });
  assert.ok(firstWarn > 0 && tones.slice(firstWarn).every(function (c) { return c.includes("tone-warn"); }),
    "errors and active blockers precede advisories: " + tones.join(","));
  navigate("#/runs/run_ok");
  await respond("/api/runs/run_ok", D.calm);
  assert.equal(all(attrIs("data-attention", "true")).length, 0, "no empty problem section");
  assert.ok(byId.main.textContent.includes("HISTORIC-BLOCKER"), "historic blocker stays discoverable");
  assert.ok(!visibleText(byId.main).includes("HISTORIC-BLOCKER"), "historic blocker is secondary");
''')

    def test_stop_attention_item_and_meta(self):
        interrupted = detail("run_int", "Ship it", status="interrupted", stop=stop_value(
            "STOP-DETAIL-MARKER", kind="interrupted", reason="signal", title="运行被中断", exit_code=130,
            exits=["resume run_int", "abandon run_int"]))
        waiting = detail("run_wait", "Ship it", status="awaiting_approval", stop=stop_value(
            None, kind="plan_approval", reason="initial_plan", title="计划等待明确批准", exit_code=0,
            exits=["status run_wait --full", "resume run_wait --approve-plan"]))
        self.run_browser({"interrupted": interrupted, "waiting": waiting}, r'''
  navigate("#/runs/run_int");
  await respond("/api/runs/run_int", D.interrupted);
  const attention = one(attrIs("data-attention", "true"));
  const shown = visibleText(attention);
  for (const expected of ["停止：运行被中断（interrupted/signal）", "STOP-DETAIL-MARKER", "resume run_int", "abandon run_int"]) {
    assert.ok(shown.includes(expected), "stop item shows: " + expected);
  }
  // One interrupt, one attention item: the typed stop replaces the generic interrupted notice.
  assert.equal(all(tagIs("h3"), attention).length, 1, "an interrupt is shown once");
  assert.ok(!shown.includes("运行已中断"), "no second, untyped interrupt item");
  assert.ok(byId.main.textContent.includes("停止类型"), "meta panel names the stop type");
  assert.ok(byId.main.textContent.includes("interrupted/signal"), "meta panel shows kind/reason");
  navigate("#/runs/run_wait");
  await respond("/api/runs/run_wait", D.waiting);
  const waitingText = visibleText(one(attrIs("data-attention", "true")));
  assert.ok(waitingText.includes("计划等待批准"), "the approval item stays");
  assert.ok(!waitingText.includes("停止："), "plan approval is not shown twice");
''')

    def test_damaged_ledger_and_pending_grants_stay_prominent_with_diagnostics_reachable(self):
        damaged = detail("run_dmg", "unused")
        damaged.update(run=None, summary=None, budgets=None, tasks=[], dispatches=[], schema_version=None,
                       ledger_error="state.json 无法解析：JSONDecodeError: LEDGER-BROKEN",
                       events={"exists": True, "tail_bytes": 65536, "size": 10, "truncated": False,
                               "changed_while_reading": False, "records": [], "unparsed_tail": True,
                               "invalid_lines": 0, "last_time": None})
        grants = detail("run_cmd", "Needs grants", summary={
            "missing_command_requirements": {"t2": ["make", "MISSING-ARGV"]},
            "continuation": {"state": "awaiting_requirements", "task_id": "t2", "continuation_id": "c-1",
                             "request": {"kind": "commands", "reason": "CONTINUATION-REASON"}}})
        data = {"damaged": damaged, "grants": grants}
        self.run_browser(data, r'''
  navigate("#/runs/run_dmg");
  await respond("/api/runs/run_dmg", D.damaged);
  const attention = one(attrIs("data-attention", "true"));
  assert.ok(visibleText(attention).includes("LEDGER-BROKEN"), "ledger error is visible without interaction");
  assert.equal(all(attrIs("role", "tabpanel")).filter(function (p) { return !p.hidden; }).length, 1);
  // Secondary diagnostics still render behind tabs and the file index stays reachable.
  fire(one(attrIs("data-tab", "files")), "click");
  await respond("/api/runs/run_dmg/files", {id: "run_dmg", sampled_at: 2000.0, truncated: false, limit: 2000,
    skipped: {symlink: 0, other: 0, excluded: 0, unaddressable: 0}, files: [{path: "state.json", size: 9, mtime: 1}]});
  assert.ok(all(function (n) { return n.tagName === "a" && n.href === "#/runs/run_dmg/files/state.json"; }).length >= 1);
  navigate("#/runs/run_cmd");
  await respond("/api/runs/run_cmd", D.grants);
  const shown = visibleText(one(attrIs("data-attention", "true")));
  for (const expected of ["MISSING-ARGV", "CONTINUATION-REASON", "--reject-request"]) {
    assert.ok(shown.includes(expected), "visible without interaction: " + expected);
  }
''')

    def test_ledger_and_quota_semantics_are_not_overstated(self):
        from test_quota_view import T, WEEK, claude, codex
        rows = [dispatch(WORKER, cost=0.25, provider="claude"),
                dispatch(REVIEWER, role="reviewer", provider="codex")]
        samples = [codex(T, [("primary", WEEK, 70, T + 90000)], (False, "41.25")),
                   claude(T + 1, [("weekly_all", 42, T + 90000), ("weekly_scoped", 100, T + 90000, "Model Y")]),
                   codex(T + 60, [("primary", WEEK, 73, T + 90000)], (False, "38.75")),
                   claude(T + 61, [("weekly_all", 47, T + 90000), ("weekly_scoped", 100, T + 90000, "Model Y")])]
        data = {"detail": detail("run_cost", "Cost goal", dispatches=rows, quota_view=quota_block(samples),
                                 events=[event(1995.0, "dispatch_finished", dispatch_id=REVIEWER)])}
        self.run_browser(data, r'''
  navigate("#/runs/run_cost");
  await respond("/api/runs/run_cost", D.detail);
  // A worker that completed its turn does not make the task accepted.
  assert.equal(one(attrIs("data-task", "t2")).getAttribute("data-state"), "current");
  assert.equal(one(attrIs("data-task", "t1")).getAttribute("data-state"), "accepted");
  const progress = one(attrIs("data-progress", "tasks"));
  assert.deepEqual([progress.value, progress.max], [1, 2]);
  // The quota lives in its tab, not in the default view; no money anywhere.
  assert.ok(!visibleText(byId.main).includes("运行期间账号额度的观测变化"));
  fire(one(attrIs("data-tab", "budget")), "click");
  const panel = visibleText(one(attrIs("data-panel", "budget")));
  assert.ok(!byId.main.textContent.includes("$"), "no currency anywhere");
  // Codex first, its credits balance change first in its card, as the decimal strings given.
  const cards = all(function (n) { return typeof n.getAttribute === "function" && n.getAttribute("data-quota-provider"); });
  assert.deepEqual(cards.map(function (n) { return n.getAttribute("data-quota-provider"); }), ["codex", "claude"]);
  const credits = one(attrIs("data-credits", "codex"));
  assert.equal(cards[0].childNodes[1], credits, "credits lead the Codex card");
  assert.ok(credits.textContent.includes("38.75") && credits.textContent.includes("41.25 → 38.75"));
  one(attrIs("data-net-change", "-2.50"));
  assert.ok(credits.textContent.includes("-2.50"));
  // Latest observation, never "current"; percentage points per bucket, never summed.
  assert.ok(panel.includes("最近观测"));
  assert.ok(!panel.includes("当前"));
  assert.ok(panel.includes("42% → 47%（增加 5 个百分点）"));
  assert.ok(panel.includes("weekly_scoped（Model Y）"), "an unrelated model's full window is its own row");
  for (const word of ["合计", "总计", "total"]) { assert.ok(!panel.includes(word), word); }
  for (const note of D.detail.quota.notes) { assert.ok(panel.includes(note), note); }
  // Invocation classes stay; the CLI's nominal cost is not summed.
  assert.ok(one(attrIs("data-provider", "claude")).textContent.includes("已调用 1"));
''')

    def test_quota_empty_states_and_unknown_changes(self):
        from test_quota_view import T, claude, codex
        unknown = quota_block([codex(T, credits=(False, "5.00")), codex(T + 10, credits=(False, "4.00"))])
        unknown["providers"]["codex"]["credits"]["codex"]["runs"][0]["net_change"] = None   # a trapped difference
        cases = {
            "none": quota_block(),
            "codex_only": quota_block([codex(T, [("primary", 300, 10, T + 600)])]),
            "all_unavailable": quota_block([codex(T, available=False, reason="timeout"),
                                            codex(T + 1, available=False, reason="timeout"),
                                            codex(T + 2, available=False, reason="exited"),
                                            claude(T + 3, [("session", 5, T + 600)])], damaged=2, omitted=3,
                                           truncated=True),
            "unknown": unknown,
        }
        data = {name: detail("run_" + name.replace("_", ""), "Quota", quota_view=value) for name, value in cases.items()}
        self.run_browser(data, r'''
  async function panelOf(name) {
    const id = "run_" + name.replace(/_/g, "");
    navigate("#/runs/" + id);
    await respond("/api/runs/" + id, D[name]);
    fire(one(attrIs("data-tab", "budget")), "click");
    return one(attrIs("data-quota", "panel"));
  }
  let section = await panelOf("none");
  assert.ok(visibleText(section).includes("没有额度样本"));
  section = await panelOf("codex_only");
  assert.ok(visibleText(one(attrIs("data-quota-provider", "claude"))).includes("没有样本"));
  assert.ok(visibleText(one(attrIs("data-quota-provider", "codex"))).includes("10%（只有一个观测）"));
  section = await panelOf("all_unavailable");
  const text = visibleText(section);
  assert.ok(text.includes("全部 3 个样本不可用：timeout×2、exited×1"));
  assert.ok(text.includes("2 个样本损坏或超大，已跳过") && text.includes("较早的 3 个样本未读取"));
  assert.ok(text.includes("较早样本未读取"), "the first segment's start is incomplete");
  section = await panelOf("unknown");
  const change = one(attrIs("data-net-change", "unknown"));
  assert.ok(change.textContent.includes("变化未知"));
  assert.ok(!/净变化 0/.test(change.textContent), "unknown is never shown as zero");
''')

    def test_balances_stay_text_in_the_page_script(self):
        # The quota part of the page never turns a balance or a change into a number.
        start = APP_JS.index("var QUOTA_START")
        script = APP_JS[start:APP_JS.index("function budgetPanel")]
        for conversion in ("Number(", "parseFloat(", "parseInt(", "+run.", "+latest.", "toFixed("):
            self.assertNotIn(conversion, script)

    def test_budget_panel_shows_attempt_sum_without_run_wide_limit(self):
        data = {"current": detail("run_new", "Current")}
        self.run_browser(data, r'''
  navigate("#/runs/run_new");
  await respond("/api/runs/run_new", D.current);
  fire(one(attrIs("data-tab", "budget")), "click");
  const panel = visibleText(one(attrIs("data-panel", "budget")));
  assert.ok(panel.includes("worker 尝试总数"), "the summed attempts are shown");
  assert.ok(panel.includes("各任务 attempts 之和"));
  assert.ok(panel.includes("5 / 40"), "model dispatches against their limit");
  assert.ok(!panel.includes("历史台账"), "no saved counter or policy of another ledger version");
''')

    def test_other_ledger_versions_are_listed_by_path_and_refused_in_detail(self):
        unsupported = [{"path": "runs/run_old", "schema_version": 17,
                        "reason": f"台账版本 17 不支持（当前版本 {RUN_SCHEMA_VERSION}）"}]
        data = {"list": {"sampled_at": 2000.0, "runs": [], "skipped": [], "incomplete": [], "unsupported": unsupported},
                "refusal": {"error": unsupported[0]["reason"], "status": 409}}
        self.run_browser(data, r'''
  await respond("/api/runs", D.list);
  const box = one(attrIs("data-ui-key", "list:diagnostics"));
  assert.ok(visibleText(box).includes("不支持的台账版本 1 个"));
  toggle(box, true);
  const shown = visibleText(byId.main);
  assert.ok(shown.includes("runs/run_old：" + D.list.unsupported[0].reason));
  assert.ok(!all(function (n) { return n.tagName === "a" && (n.href || "").includes("run_old"); }).length,
            "an unsupported run is not linked to a detail view");
  navigate("#/runs/run_old");
  await respond("/api/runs/run_old", D.refusal, false, 409);
  const refused = visibleText(byId.main);
  assert.ok(refused.includes("HTTP 409：" + D.refusal.error));
  assert.ok(refused.includes("当前版本不解释该 run 的台账"));
  assert.ok(!refused.includes("可稍后点击刷新重试"));
''')

    def test_dispatch_event_meanings_review_rounds_and_lower_bound_are_projected(self):
        rounds = {"read_only": True, "cause": "unknown", "tasks": {"t2": [
            {"round": 1, "attempt": 1, "review_dispatch_id": REVIEWER, "snapshot_digest": "a" * 64,
             "verdict": "request_changes", "new_blocking": ["OLD-001"]},
            {"round": 2, "attempt": 2, "review_dispatch_id": LATER, "snapshot_digest": "b" * 64,
             "verdict": "request_changes", "new_blocking": ["ROUND-TWO-NEW"], "status": "invalidated"}]}}
        remaining = {"lower_bound": 2, "tasks": {"t2": 1}, "final_review": 1,
                     "message": "如果之后全部一次通过，至少还需 2 次模型调用（下界：BOUND-TEXT）"}
        records = [event(1990.0, "dispatch_prepared", dispatch_id=WORKER),
                   event(1991.0, "dispatch_prepared", dispatch_id=LATER),
                   event(1992.0, "dispatch_finished", dispatch_id=LATER, invoked=False),
                   event(1993.0, "dispatch_started", dispatch_id=REVIEWER, invoked=True),
                   event(1994.0, "dispatch_finished", dispatch_id=REVIEWER, invoked=True)]
        data = {"detail": detail("run_obs", "Observe", dispatches=[dispatch(WORKER)], events=records,
                                 summary={"review_rounds": rounds, "remaining_model_calls": remaining})}
        self.run_browser(data, r'''
  navigate("#/runs/run_obs");
  await respond("/api/runs/run_obs", D.detail);
  fire(one(attrIs("data-tab", "events")), "click");
  const shown = visibleText(byId.main);
  for (const label of ["准备派发", "派发结束（未调用）", "开始调用", "调用结束"]) {
    assert.ok(shown.includes(label), "event label shown: " + label);
  }
  fire(one(attrIs("data-tab", "budget")), "click");
  const bound = one(attrIs("data-remaining-calls", "2"));
  assert.ok(visibleText(bound).includes("BOUND-TEXT"));
  fire(one(attrIs("data-tab", "meta")), "click");
  const meta = visibleText(byId.main);
  for (const expected of ["OLD-001", "ROUND-TWO-NEW", "invalidated", "b".repeat(64)]) {
    assert.ok(meta.includes(expected), "review round shown: " + expected);
  }
''')

    def test_stale_detail_and_index_responses_never_render_on_newer_views(self):
        data = {"one": detail("run_one", "GOAL-ONE"), "two": detail("run_two", "GOAL-TWO"),
                "stale_index": file_index("run_two", "STALE-INDEX-PATH.json"),
                "fresh_index": file_index("run_two", "FRESH-INDEX-PATH.json"),
                "file": {"source": "state.json", "size": 4, "tail_bytes": 65536, "truncated": False,
                         "changed_while_reading": False, "sampled_at": 2000.0, "text": "FILE-BODY"}}
        self.run_browser(data, r'''
  navigate("#/runs/run_one");
  navigate("#/runs/run_two");
  await respond("/api/runs/run_one", D.one);
  assert.ok(!byId.main.textContent.includes("GOAL-ONE"), "stale detail ignored");
  await respond("/api/runs/run_two", D.two);
  assert.ok(byId.main.textContent.includes("GOAL-TWO"));
  // Manual refresh of the same run: the older index response must not overwrite the newer one.
  fire(byId.refresh, "click");
  await respond("/api/runs/run_two", D.two);
  await respond("/api/runs/run_two/files", D.stale_index);
  assert.ok(!byId.main.textContent.includes("STALE-INDEX-PATH"));
  await respond("/api/runs/run_two/files", D.fresh_index);
  assert.ok(byId.main.textContent.includes("FRESH-INDEX-PATH"));
  // A pending index from the detail view cannot leak into the file view.
  fire(byId.refresh, "click");
  await respond("/api/runs/run_two", D.two);
  navigate("#/runs/run_two/files/state.json");
  await respond("/api/runs/run_two/files", D.stale_index);
  assert.ok(!byId.main.textContent.includes("STALE-INDEX-PATH"));
  await respond("/api/runs/run_two/files/state.json", D.file);
  assert.ok(byId.main.textContent.includes("FILE-BODY"));
  assert.ok(!byId.main.textContent.includes("GOAL-TWO"));
  assert.equal(sources.length, 0, "follow is off: no subscription was opened");
''')


if __name__ == "__main__":
    unittest.main()
