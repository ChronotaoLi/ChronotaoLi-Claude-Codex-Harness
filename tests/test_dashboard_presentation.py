"""Behavior of the dashboard presentation under the simulated DOM (Node, no real browser).

These tests execute the shipped APP_JS against the minimal fake DOM from test_dashboard,
with controlled fetch completions, SSE sources and timers from test_dashboard_page_events.
They check progressive disclosure, tab keyboard navigation, UI-state retention across
same-run event refreshes, prominence of actionable problems and projection semantics.
"""
import copy
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import observe, quota
from harness.cli import status_summary
from harness.dashboard_page import APP_CSS, APP_JS, INDEX_HTML
from harness.versions import RUN_SCHEMA_VERSION
from test_convergence import approval, rejection
from test_dashboard_page_events import BROWSER_JS

WORKER = "0b7e6d3c-1f0a-4c55-9d43-2f6f0b1c9a01"
REVIEWER = "6a0f8e2d-77b1-4e0c-8f7c-3b5d2a9e4c02"
LATER = "9c4d1e7a-0b2f-4a8e-b6d5-1e3f7a2c8d03"
ENV_DISPATCH = "2d8e4f6a-3c1b-4e7d-9a0f-5b6c7d8e9f04"
# Names a plain object inherits: a ledger value equal to one of them is not an entry of any table.
INHERITED = ("constructor", "toString", "__proto__", "hasOwnProperty")

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
// Resolve the oldest unresolved request for this path with a failed non-JSON body.
async function respondText(path, status, text) {
  const entry = pending.find(function (p) { return !p.done && p.url.split("?")[0] === path; });
  assert.ok(entry, "no pending request for " + path + "; requested: " + JSON.stringify(requested));
  entry.done = true;
  entry.resolve({ok: false, status: status, text: function () { return Promise.resolve(text); }});
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


def observed_at(state, checkout=None):
    """An `observed` value as observe.location() builds it for a state directory that resolved to itself."""
    return observe.location(state, Path(state), checkout=checkout)


OBSERVED = observed_at("/tmp/repo/.harness", "/tmp/repo")


def projected(ledger):
    """The rework hint and the ledger stage as cli.status_summary() projects them from a hand-written ledger."""
    summary = status_summary(ledger)
    return {"convergence": summary["convergence"], "ledger_stage": summary["ledger_stage"]}


def review_id(attempt):
    return f"{attempt}{attempt}{attempt}e2d40-77b1-4e0c-8f7c-3b5d2a9e4c0{attempt}"


def sixth_attempt_ledger(task="t2", label="ds1-write-gate", process_state="starting"):
    """Five rejected attempts, the last two newly reporting one label, while the sixth worker call is recorded."""
    history = [rejection(f"only-{n}", *([label] if n >= 4 else []), attempt=n, dispatch=review_id(n)) for n in range(1, 6)]
    return {"status": "running", "accepted": {}, "attempts": {task: 6}, "review_history": {task: history},
            "dispatches": [{"id": WORKER, "role": "worker", "kind": "implement", "task_id": task, "status": "running",
                            "process_state": process_state, "worker_receipt": {"attempt": 6}}]}


def detail(run_id, goal, *, status="running", summary=None, dispatches=(), events=(), stop=None, quota_view=None,
           observed=OBSERVED):
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
            "notes": {"invoked": "invoked-note", "process": "process-note"}, "observed": observed}


def file_index(run_id, *paths, observed=OBSERVED):
    return {"id": run_id, "sampled_at": 2000.0, "truncated": False, "limit": 2000,
            "skipped": {"symlink": 0, "other": 0, "excluded": 0, "unaddressable": 0},
            "files": [{"path": path, "size": 10, "mtime": 1000.0} for path in paths], "observed": observed}


def class_rules(css):
    """Declarations of the stylesheet's rules outside media queries whose selector is a bare `.class`, per class."""
    rules = {}
    unconditional = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css)
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", unconditional):
        declarations = {name.strip(): value.strip() for name, _, value in
                        (item.partition(":") for item in body.split(";")) if value.strip()}
        for selector in selectors.split(","):
            if re.fullmatch(r"\.[\w-]+", selector.strip()):
                rules.setdefault(selector.strip()[1:], {}).update(declarations)
    return rules


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
        # Two rejected attempts that both newly report F-17; a third work attempt has started since. The hint is the
        # same for a blocked ledger, which status_summary() accepts only with a stop record.
        ledger = {"status": "running", "accepted": {}, "attempts": {"t2": 3}, "review_history": {"t2": [
            rejection("F-17", attempt=1, dispatch=REVIEWER), rejection("F-17", attempt=2, dispatch=LATER)]}}
        problems = detail("run_bad", "Ship it", status="blocked", stop=stop_value("boom: provider failure"),
                          dispatches=[dispatch(WORKER, status="failed", outcome=None, error="worker crashed")],
                          summary={
                              "pending_decisions": [{"id": "req-1", "task_id": "t2", "kind": "operator",
                                                     "question": question, "artifact": "/x/decisions/req-1.json",
                                                     "artifact_digest": "0" * 64}],
                              "last_environment_blocker": {"task_id": "t2", "dispatch_id": ENV_DISPATCH,
                                                           "blockers": ["缺少 DATABASE_URL"], "stale": False},
                              "convergence": projected(ledger)["convergence"]})
        hint = problems["summary"]["convergence"]["warnings"][0]
        self.assertEqual((hint["repeated_labels"], [ref["review_dispatch_id"] for ref in hint["compared_reviews"]],
                          hint["latest_review"]["attempt"], hint["latest_attempt"], hint["attempts_since_latest_review"]),
                         (["F-17"], [REVIEWER, LATER], 2, 3, 1))
        calm = detail("run_ok", "Ship it", summary={
            "last_environment_blocker": {"task_id": "t1", "dispatch_id": ENV_DISPATCH,
                                         "blockers": ["HISTORIC-BLOCKER"], "stale": True}})
        data = {"problems": problems, "calm": calm, "question": question, "reviews": [REVIEWER, LATER]}
        self.run_browser(data, r'''
  navigate("#/runs/run_bad");
  await respond("/api/runs/run_bad", D.problems);
  const attention = one(attrIs("data-attention", "true"));
  const rework = one(attrIs("data-rework", "true"));
  const order = byId.main.childNodes;
  const tabsSection = order.find(function (n) { return all(attrIs("role", "tablist"), n).length; });
  assert.deepEqual([order.indexOf(attention), order.indexOf(rework)], [1, 2],
    "the run header, then what needs the operator, then the rework history");
  assert.ok(order.indexOf(rework) < order.indexOf(tabsSection), "both come before secondary detail");
  const shown = visibleText(attention);
  for (const expected of ["boom: provider failure", "缺少 DATABASE_URL", "req-1", "run_bad"]) {
    assert.ok(shown.includes(expected), "visible without interaction: " + expected);
  }
  const history = visibleText(rework);
  for (const expected of ["F-17"].concat(D.reviews.map(function (id) { return id.slice(0, 8) + "…"; }))) {
    assert.ok(history.includes(expected), "the rework section shows " + expected);
    assert.ok(!shown.includes(expected), "the attention section does not show " + expected);
  }
  const failure = one(attrIs("data-call-error", "true"));
  assert.ok(attached(failure) && one(attrIs("data-task", "t2")) === failure.parentNode, "the error is beside its call");
  assert.ok(visibleText(failure).includes("worker crashed"));
  assert.ok(!shown.includes("worker crashed") && !history.includes("worker crashed"), "the call error is shown once");
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
  assert.equal(all(attrIs("data-rework", "true")).length, 0, "no empty rework section");
  one(attrIs("data-pending-count", "0"));
  assert.ok(byId.main.textContent.includes("HISTORIC-BLOCKER"), "historic blocker stays discoverable");
  assert.ok(!visibleText(byId.main).includes("HISTORIC-BLOCKER"), "historic blocker is secondary");
''')

    def test_rework_hint_is_a_dated_observation_outside_attention(self):
        summary = projected(sixth_attempt_ledger())
        data = {"detail": detail("run_six", "Ship it", summary=summary,
                                 dispatches=[dispatch(WORKER, status="running", outcome=None)]),
                "reviews": [review_id(4), review_id(5)], "worker": WORKER}
        self.run_browser(data, r'''
  navigate("#/runs/run_six");
  await respond("/api/runs/run_six", D.detail);
  assert.equal(all(attrIs("data-attention", "true")).length, 0, "a rework hint alone asks nothing of the operator");
  one(attrIs("data-pending-count", "0"));
  const order = byId.main.childNodes;
  const rework = one(attrIs("data-rework", "true"));
  const tabsSection = order.find(function (n) { return all(attrIs("role", "tablist"), n).length; });
  assert.equal(order.indexOf(rework), 1, "without attention items the rework history follows the run header");
  assert.ok(order.indexOf(rework) < order.indexOf(tabsSection), "and precedes secondary detail");
  const item = one(attrIs("data-rework-item", "task:t2"), rework);
  assert.deepEqual(["data-compared-rounds", "data-latest-attempt", "data-since-review", "data-latest-outcome"].map(
    function (name) { return item.getAttribute(name); }), ["4,5", "6", "1", "rejected"]);
  const basis = one(attrIs("data-basis", "label"), item);
  const shownBasis = visibleText(basis);
  for (const id of D.reviews) {
    assert.ok(shownBasis.includes(id.slice(0, 8) + "…"), "the compared review is named: " + id);
    one(function (n) { return n.title === id; }, basis);
  }
  assert.ok(shownBasis.includes("ds1-write-gate"), shownBasis);
  assert.ok(!/only-[45]/.test(shownBasis), "only the label both rounds reported");
  // The run header names the recorded step on the status line, next to the run ID.
  const stage = one(attrIs("data-stage", "dispatch"));
  assert.equal(stage.parentNode.className, "hero-top");
  assert.equal(stage.parentNode.parentNode, order[0]);
  assert.ok(visibleText(stage.parentNode).includes("run_six"));
  assert.deepEqual(["data-stage-task", "data-stage-attempt", "data-stage-process"].map(function (name) {
    return stage.getAttribute(name); }), ["t2", "6", "starting"]);
  const marker = visibleText(stage);
  for (const value of ["t2", "6", D.worker.slice(0, 8) + "…"]) { assert.ok(marker.includes(value), value + " in " + marker); }
''')

    def test_attempts_only_hint_names_no_review_source(self):
        # Three work attempts with no review conclusion; the whole run's later rejection was left behind by a
        # candidate change (recorded invalid_rejected/finished) and a third whole-run attempt is being verified.
        ledger = {"status": "running", "accepted": {}, "attempts": {"t2": 3}, "review_history": {}, "final_history": [
            {"attempt": 1, "status": "rejected", "phase": "finished", "snapshot_digest": "f1",
             "review_source": {"dispatch_id": REVIEWER}},
            {"attempt": 2, "status": "invalid_rejected", "phase": "finished", "snapshot_digest": "f2",
             "review_source": {"dispatch_id": LATER}},
            {"attempt": 3, "status": "running", "phase": "verifying"}]}
        summary = projected(ledger)
        self.assertEqual([item["kind"] for item in summary["convergence"]["warnings"]], ["task_rework", "whole_run_rework"])
        data = {"detail": detail("run_retry", "Ship it", summary=summary), "rejection": LATER}
        self.run_browser(data, r'''
  navigate("#/runs/run_retry");
  await respond("/api/runs/run_retry", D.detail);
  const rework = one(attrIs("data-rework", "true"));
  const task = one(attrIs("data-rework-item", "task:t2"), rework);
  assert.deepEqual(["data-compared-rounds", "data-latest-outcome", "data-latest-attempt", "data-since-review"].map(
    function (name) { return task.getAttribute(name); }), ["", "", "3", "3"]);
  assert.equal(all(function (n) { return typeof n.getAttribute === "function" && n.getAttribute("data-basis") !== null; }).length,
    0, "a hint triggered by the attempt count alone names no review source");
  const whole = one(attrIs("data-rework-item", "final"), rework);
  assert.deepEqual(["data-latest-rejection-attempt", "data-latest-final-attempt"].map(function (name) {
    return whole.getAttribute(name); }), ["2", "3"]);
  assert.ok(visibleText(whole).includes(D.rejection.slice(0, 8) + "…"), "the latest rejection's review is named");
  // The ledger's recorded status and phase of the latest whole-run attempt, verbatim.
  for (const value of ["running", "verifying"]) {
    one(function (n) { return n.className === "mono" && n.textContent === value; }, whole);
  }
  assert.equal(all(attrIs("data-attention", "true")).length, 0);
  assert.equal(one(attrIs("data-stage", "final_verification")).getAttribute("data-stage-attempt"), "3");
''')

    def test_stage_marker_follows_the_recorded_step(self):
        from harness.diagnostics import ledger_stage
        worker = {"id": WORKER, "role": "worker", "kind": "implement", "task_id": "t2", "status": "running",
                  "worker_receipt": {"attempt": 2}}
        reviewer = {"id": REVIEWER, "role": "reviewer", "kind": "review", "task_id": "t2", "status": "running",
                    "process_state": "running"}
        verifier = {"id": LATER, "role": "verification", "task_id": "t2", "status": "running", "process_state": "prepared"}
        verifying, reviewing = ({"task_id": "t2", "attempt": 2, "stage": stage} for stage in ("verifying", "reviewing"))
        ledgers = {
            "between": {}, "verify": {"pending_task": verifying}, "review": {"pending_task": reviewing},
            "accept": {"pending_acceptance": {"task_id": "t2", "attempt": 2}},
            "post_accept": {"pending_post_accept": {"payload": {"task_id": "t2"}}},
            "final_verify": {"final_history": [{"attempt": 4, "status": "running", "phase": "verifying"}]},
            "final_review": {"final_history": [{"attempt": 4, "status": "running", "phase": "reviewing"}]},
            **{f"worker_{state}": {"dispatches": [{**worker, "process_state": state}]}
               for state in ("prepared", "starting", "running", "exited", None)},
            "reviewer": {"pending_task": reviewing, "dispatches": [worker, reviewer]},
            "verification": {"pending_task": verifying, "dispatches": [verifier]},
            "quota": {"pending_task": verifying, "dispatches": [worker], "quota_wait": {"role": "worker", "dispatch_id": WORKER}}}
        stages = {name: ledger_stage({"status": "running", **fields}) for name, fields in ledgers.items()}
        stages["blocked"] = ledger_stage({"status": "blocked", "dispatches": [worker]})
        self.assertEqual({value["step"] for value in stages.values() if value}, {
            "between_steps", "task_verification", "task_review", "task_acceptance", "final_verification",
            "final_review", "dispatch", "quota_wait"})
        self.assertIsNone(stages["blocked"])
        wait = {"source_dispatch_id": WORKER, "role": "worker", "window": "session", "until": "2026-11-09T01:00:00Z",
                "resets_at": "2026-11-09T00:55:00Z", "recorded_at": "2026-11-09T00:10:00Z", "limit_seconds": 3600}
        # Every page also carries a rework hint, whose note points to the marker: neither claims a live process.
        rework = projected({"status": "running", "accepted": {}, "attempts": {"t2": 3}, "review_history": {"t2": [
            rejection("F-1", attempt=n, dispatch=review_id(n)) for n in (1, 2, 3)]}})["convergence"]
        details = {name: detail("run_" + name, "Ship it", status="blocked" if name == "blocked" else "running",
                                summary={"ledger_stage": value, "convergence": rework,
                                         **({"quota_wait": wait} if name == "quota" else {})})
                   for name, value in stages.items()}
        self.run_browser({"stages": stages, "details": details, "wait": wait}, r'''
  const liveness = ["调用中", "运行中", "执行中", "正在", "进行中"];
  for (const name of Object.keys(D.stages)) {
    const value = D.stages[name];
    navigate("#/runs/run_" + name);
    await respond("/api/runs/run_" + name, D.details[name]);
    const history = visibleText(one(attrIs("data-rework", "true")));
    for (const word of liveness.concat(["此刻"])) {
      assert.ok(!history.includes(word), name + ": the rework section says " + word + ": " + history);
    }
    const markers = all(function (n) { return typeof n.getAttribute === "function" && n.getAttribute("data-stage") !== null; });
    if (value === null) { assert.equal(markers.length, 0, name + ": no marker without a recorded stage"); continue; }
    assert.equal(markers.length, 1, name);
    const marker = markers[0];
    assert.equal(marker.parentNode.className, "hero-top", name);
    assert.deepEqual(["data-stage", "data-stage-task", "data-stage-attempt", "data-stage-process"].map(function (attribute) {
      return marker.getAttribute(attribute); }), [value.step, value.task_id, value.attempt, value.process_state].map(function (v) {
      return v === null ? "" : String(v); }), name);
    const text = visibleText(marker);
    const expected = [value.task_id, value.attempt === null ? null : String(value.attempt),
                      value.dispatch_id === null ? null : value.dispatch_id.slice(0, 8) + "…"];
    if (value.step === "quota_wait") { expected.push(D.wait.window, D.wait.until); }
    for (const part of expected.filter(function (v) { return v !== null; })) {
      assert.ok(text.includes(part), name + ": " + part + " in " + text);
    }
    // A record of the ledger, never a claim that a process is alive now.
    for (const word of liveness) { assert.ok(!(text + marker.title).includes(word), name + " says " + word + ": " + text); }
  }
''')

    def test_quota_wait_and_call_errors_are_not_attention_items(self):
        error = "invalid output: missing result.json\nTraceback (most recent call last): SECOND-LINE"
        rows = [dispatch(WORKER, status="failed", outcome=None, error=error)]
        wait = {"source_dispatch_id": REVIEWER, "role": "worker", "window": "weekly", "until": "2026-11-10T08:00:00Z",
                "resets_at": "2026-11-10T07:59:00Z", "recorded_at": "2026-11-09T00:10:00Z", "limit_seconds": 3600}
        data = {"running": detail("run_live", "Ship it", dispatches=rows),
                "blocked": detail("run_dead", "Ship it", status="blocked", dispatches=rows,
                                  stop=stop_value("STOP-DETAIL-MARKER", exits=["resume run_dead"])),
                "waiting": detail("run_wait", "Ship it", summary={"quota_wait": wait}), "wait": wait, "worker": WORKER}
        self.run_browser(data, r'''
  async function open(runId, body) { navigate("#/runs/" + runId); await respond("/api/runs/" + runId, body); }
  function callError() {
    const failure = one(attrIs("data-call-error", "true"));
    assert.equal(failure.parentNode, one(attrIs("data-task", "t2")), "the error is in the task card, at its call");
    const text = visibleText(failure);
    assert.ok(text.includes("invalid output: missing result.json") && !text.includes("SECOND-LINE"), "its first line: " + text);
    one(function (n) { return n.className === "mono" && n.textContent === D.worker; }, failure);
    return failure;
  }
  await open("run_live", D.running);
  assert.equal(all(attrIs("data-attention", "true")).length, 0, "a call error of a running run asks nothing of the operator");
  one(attrIs("data-pending-count", "0"));
  callError();
  await open("run_dead", D.blocked);
  const attention = one(attrIs("data-attention", "true"));
  assert.equal(all(tagIs("h3"), attention).length, 1, "the stop is the only attention item");
  assert.ok(visibleText(attention).includes("STOP-DETAIL-MARKER"));
  assert.ok(!attention.textContent.includes("invalid output"), "the call error is not repeated as an item");
  one(attrIs("data-pending-count", "1"));
  callError();
  await open("run_wait", D.waiting);
  assert.equal(all(attrIs("data-attention", "true")).length, 0, "a recorded quota wait asks nothing of the operator");
  one(attrIs("data-pending-count", "0"));
  const wait = one(attrIs("data-quota-wait", "true"));
  assert.equal(wait.parentNode, byId.main.childNodes[0], "the wait is in the run header");
  const shown = visibleText(wait);
  for (const value of [D.wait.window, D.wait.until, D.wait.resets_at, D.wait.source_dispatch_id, D.wait.recorded_at]) {
    assert.ok(shown.includes(value), value + " in " + shown);
  }
''')

    def test_rework_and_stage_values_are_inserted_as_text(self):
        task = 't<img src=x onerror=alert(1)>"&amp;'
        label = 'L<script>alert(2)</script>"&lt;'
        data = {"detail": detail("run_text", "Ship it", summary=projected(sixth_attempt_ledger(task=task, label=label))),
                "task": task, "label": label}
        self.run_browser(data, r'''
  navigate("#/runs/run_text");
  await respond("/api/runs/run_text", D.detail);
  const item = one(attrIs("data-rework-item", "task:" + D.task));
  const shown = visibleText(item);
  assert.ok(shown.includes(D.task) && shown.includes(D.label), shown);
  one(function (n) { return n.className === "mono" && n.textContent === D.label; }, one(attrIs("data-basis", "label"), item));
  const stage = one(attrIs("data-stage", "dispatch"));
  assert.equal(stage.getAttribute("data-stage-task"), D.task);
  assert.ok(visibleText(stage).includes(D.task));
  assert.equal(all(tagIs("img")).length + all(tagIs("script")).length, 0, "no value is parsed as markup");
''')

    def test_inherited_property_names_in_the_stage_are_shown_as_recorded(self):
        # The marker looks the role, the process record and the step up in tables; a ledger value naming an
        # inherited member of a plain object is not an entry. role, kind and process_state are projected verbatim;
        # step has a fixed vocabulary in ledger_stage, so a summary carrying another one is written by hand.
        cases = []
        for name in INHERITED:
            role = projected({"status": "running", "dispatches": [
                {"id": WORKER, "role": name, "kind": name, "task_id": "t2", "status": "running",
                 "process_state": "running"}]})["ledger_stage"]
            process = projected({"status": "running", "dispatches": [
                {"id": WORKER, "role": "worker", "kind": "implement", "task_id": "t2", "status": "running",
                 "process_state": name, "worker_receipt": {"attempt": 2}}]})["ledger_stage"]
            verifying = projected({"status": "running", "pending_task": {"task_id": "t2", "attempt": 2, "stage": "verifying"}})
            step = {**verifying["ledger_stage"], "step": name}
            self.assertEqual((role["role"], role["kind"], process["process_state"]), (name, name, name))
            for lookup, stage, count in (("role", role, 2), ("process", process, 1), ("step", step, 1)):
                run_id = f"run_{lookup}_{len(cases)}"
                cases.append({"run": run_id, "name": name, "stage": stage, "count": count,
                              "detail": detail(run_id, "Ship it", summary={"ledger_stage": stage})})
        self.run_browser({"cases": cases}, r'''
  for (const item of D.cases) {
    navigate("#/runs/" + item.run);
    await respond("/api/runs/" + item.run, item.detail);
    one(attrIs("data-overview", "true"));
    const marker = one(function (n) { return typeof n.getAttribute === "function" && n.getAttribute("data-stage") !== null; });
    assert.deepEqual(["data-stage", "data-stage-task", "data-stage-attempt", "data-stage-process"].map(function (name) {
      return marker.getAttribute(name); }), [item.stage.step, item.stage.task_id, item.stage.attempt,
      item.stage.process_state].map(function (v) { return v === null ? "" : String(v); }), item.run);
    const text = visibleText(marker) + " " + marker.title;
    assert.equal(text.split(item.name).length - 1, item.count, item.run + ": the value as recorded: " + text);
    assert.ok(!/function|native code|\[object /.test(text), item.run + ": no inherited member: " + text);
  }
''')

    def test_inherited_property_names_in_rework_hints_are_shown_as_recorded(self):
        # The hints compare outcomes and show status and phase verbatim; no ledger value is a table key there.
        cases = []
        for index, name in enumerate(INHERITED):
            ledger = {"status": "running", "accepted": {}, "attempts": {"t2": 3},
                      "review_history": {"t2": [rejection("F-1", attempt=n, dispatch=review_id(n), verdict=name)
                                                for n in (1, 2, 3)]},
                      "final_history": [{"attempt": n, "status": "rejected", "phase": "finished",
                                         "review_source": {"dispatch_id": review_id(n)}} for n in (1, 2)]
                      + [{"attempt": 3, "status": name, "phase": name}]}
            summary = projected(ledger)
            task, whole = summary["convergence"]["warnings"]
            self.assertEqual((task["latest_review"]["verdict"], whole["latest_final_attempt"]["status"],
                              whole["latest_final_attempt"]["phase"]), (name, name, name))
            # convergence_summary writes three outcomes; the page gets whatever the summary carries.
            task["latest_review"]["outcome"] = name
            cases.append({"run": f"run_hint_{index}", "name": name,
                          "detail": detail(f"run_hint_{index}", "Ship it", summary=summary)})
        self.run_browser({"cases": cases}, r'''
  for (const item of D.cases) {
    navigate("#/runs/" + item.run);
    await respond("/api/runs/" + item.run, item.detail);
    const rework = one(attrIs("data-rework", "true"));
    const task = one(attrIs("data-rework-item", "task:t2"), rework);
    assert.equal(task.getAttribute("data-latest-outcome"), item.name);
    const when = visibleText(task.childNodes[task.childNodes.length - 1]);
    assert.equal(when.split(item.name).length - 1, 1, item.run + ": the outcome as recorded: " + when);
    const whole = one(attrIs("data-rework-item", "final"), rework);
    assert.equal(all(function (n) { return n.className === "mono" && n.textContent === item.name; }, whole).length, 2,
      item.run + ": status and phase as recorded");
    assert.ok(!/function|native code|\[object /.test(visibleText(rework)), item.run + ": " + visibleText(rework));
  }
''')

    def test_inherited_property_names_in_the_other_lookups_are_shown_as_recorded(self):
        # Every other place the page looks a ledger or projection value up, each given all four names: run, call and
        # process status, role, invocation class, worker outcome, event name and field name, HTTP status state, quota
        # segment markers, outcome counts. Each shows what it shows for an ordinary unknown value: the value itself.
        from test_quota_view import T, WEEK, codex
        lists = {"sampled_at": 2000.0, "skipped": [], "incomplete": [], "unsupported": [], "observed": OBSERVED,
                 "runs": [{"id": f"run_list_{index}", "status": name, "goal": "Goal", "branch": "b",
                           "run_dir": f"run_list_{index}", "schema_version": RUN_SCHEMA_VERSION,
                           "last_event_time": 1000.0 + index} for index, name in enumerate(INHERITED)]}
        cases = []
        for index, name in enumerate(INHERITED):
            other = INHERITED[(index + 1) % len(INHERITED)]
            # Role, status, process state and worker outcome are projected verbatim from ledger entries.
            rows, invalid, _ = observe.dispatch_rows({"commander": "fable", "dispatches": [
                {"id": WORKER, "role": name, "kind": "implement", "task_id": "t2", "status": name, "process_state": name,
                 "invoked": True, "pgid": 4242, "exit_confirmed": True},
                {"id": REVIEWER, "role": "worker", "kind": "implement", "task_id": "t2", "status": "completed",
                 "process_state": "exited", "invoked": True, "pgid": 4243, "exit_confirmed": True,
                 "worker_outcome": name}]})
            self.assertEqual((invalid, rows[0]["role"], rows[0]["status"], rows[0]["process_state"],
                              rows[1]["worker_outcome"]), (0, name, name, name, name))
            view = quota_block([codex(T, [("primary", WEEK, 70, T + 90000)]), codex(T + 60, [("primary", WEEK, 73, T + 90000)])])
            for variant, current in (("role", 0), ("outcome", 1)):
                start, end = (name, other) if current == 0 else (other, name)
                segments = view["providers"]["codex"]["buckets"][0]["segments"]
                segments[0].update(incomplete_start=start, incomplete_end=end, flags=[start, end])
                facts = {"role": "worker", "provider": "claude", "http_status_state": name,
                         **({"http_status": 503} if current == 0 else {})}
                stop = stop_value("CLI-EXCERPT-MARKER", kind="provider_failure", reason="transport",
                                  title="与供应商的连接失败", exit_code=3, exits=["resume run"])
                stop["data"] = facts
                run_id = f"run_{variant}_{index}"
                ordered = [dict(row) for row in (rows[1 - current], rows[current])]
                data = detail(run_id, "Ship it", status=name, stop=stop, dispatches=ordered, quota_view=copy.deepcopy(view),
                              summary={"worker_outcomes": {name: 3, "completed": 1}},
                              events=[event(1980.0, "dispatch_prepared", **{name: "V"}),
                                      event(1990.0, name, dispatch_id=WORKER)])
                # The invocation class has a fixed vocabulary in observe; a projection carrying another is written by hand.
                outcome_row = next(row for row in data["dispatches"] if row["id"] == REVIEWER)
                outcome_row.update(invocation_class=name, invocation_reason=None, startup_unconfirmed=True)
                cases.append({"run": run_id, "name": name, "current": current, "start": start, "end": end,
                              "detail": data})
        self.run_browser({"list": lists, "cases": cases, "worker": WORKER, "reviewer": REVIEWER}, r'''
  function clean(text, where) { assert.ok(!/function|native code|\[object /.test(text), where + ": " + text); }
  function badgeOf(node, name, cls) {
    assert.deepEqual([node.className, node.textContent, node.title], [cls + " tone-neutral",
      cls === "chip" ? name + "（启动未确认）" : name, "原始值：" + name]);
  }
  function after(node) { const siblings = node.parentNode.childNodes; return siblings[siblings.indexOf(node) + 1]; }
  function described(label, root) { return after(one(function (n) { return n.tagName === "dt" && n.textContent === label; }, root)); }
  await respond("/api/runs", D.list);
  D.list.runs.forEach(function (run) {
    const card = one(attrIs("data-status", run.status));
    badgeOf(card.childNodes[0].childNodes[0], run.status, "badge");
  });
  clean(byId.main.textContent, "list");
  for (const item of D.cases) {
    const name = item.name, where = item.run;
    navigate("#/runs/" + item.run);
    await respond("/api/runs/" + item.run, item.detail);
    // Run status: the header and the overview.
    badgeOf(one(function (n) { return n.className === "hero-top"; }).childNodes[0], name, "badge");
    badgeOf(one(attrIs("data-overview", "true")).childNodes[0].childNodes[1].childNodes[0], name, "badge");
    // The current task's latest call, as the task card summarizes it.
    const summary = one(function (n) { return n.className === "item-sub" && n.ownText.startsWith("最近调用："); });
    assert.equal(summary.textContent, "最近调用：" + (item.current === 0
      ? name + " · implement · 调用状态 " + name + " · 进程记录 " + name
      : "worker · implement · 调用状态 已返回 · 进程记录 已退出 · " + name + "（尚未接受）"), where);
    // Call records: role and status of one call; outcome and invocation class of the other.
    const role = one(attrIs("data-ui-key", "dispatch:" + D.worker));
    assert.equal(one(function (n) { return n.className === "item-title"; }, role).textContent, name + " · implement", where);
    badgeOf(one(function (n) { return n.className === "badge tone-neutral"; }, role), name, "badge");
    const outcome = one(attrIs("data-ui-key", "dispatch:" + D.reviewer));
    one(function (n) { return n.className === "chip" && n.textContent === name + "（尚未接受）"; }, outcome);
    badgeOf(one(function (n) { return n.className === "chip tone-neutral"; }, outcome), name, "chip");
    assert.equal(described("调用分类", outcome).textContent, name + " (" + name + ")", where);
    // Event name in the overview, the recent activity and the log; a field of that name in the log.
    const latest = one(function (n) { return n.className === "metric" && n.childNodes[0].textContent === "最近观测"; });
    assert.equal(one(function (n) { return n.className === "metric-note"; }, latest).textContent, name + " · t2", where);
    const activity = one(function (n) { return n.tagName === "h2" && n.textContent === "最近活动"; }).parentNode.parentNode;
    assert.equal(all(function (n) { return n.className === "item-title"; }, activity)[0].textContent, name, where);
    const log = one(attrIs("data-panel", "events"));
    assert.equal(one(function (n) { return n.tagName === "span" && n.title === name; }, log).textContent, name, where);
    one(function (n) { return n.tagName === "td" && n.textContent === JSON.stringify({[name]: "V"}); }, log);
    // HTTP status state: the number when there is one, otherwise no HTTP part.
    const attention = one(attrIs("data-attention", "true"));
    const excerpt = one(function (n) { return n.textContent === "CLI-EXCERPT-MARKER"; }, attention);
    if (item.current === 0) { assert.equal(after(excerpt).textContent, "HTTP 状态：503", where); }
    else { assert.ok(after(excerpt).textContent.startsWith("出口：") && !attention.textContent.includes("HTTP 状态"), where); }
    // Quota segment markers and flags.
    const cells = all(tagIs("td"), one(attrIs("data-quota-provider", "codex")));
    assert.ok(cells[cells.length - 1].textContent.endsWith("（不完整：" + item.start + "／" + item.end + "）［" + item.start + "、" +
      item.end + "］"), where + ": " + cells[cells.length - 1].textContent);
    // Worker outcome counts by outcome.
    assert.equal(described(name + "（" + name + "）", one(attrIs("data-panel", "meta"))).textContent, "3", where);
    clean(byId.main.textContent, where);
  }
''')

    def test_a_task_named_like_an_inherited_member_is_accepted_as_the_ledger_records(self):
        cases = []
        for name in ("constructor", "__proto__"):
            for accepted in (False, True):
                tasks, _ = observe.task_rows({"tasks": [{"id": "t1", "title": "Prepare"}, {"id": name, "title": "Named"}],
                                              "accepted": {name: {"attempt": 1}} if accepted else {}})
                self.assertEqual([task["accepted"] for task in tasks], [False, accepted])
                run_id = f"run_{len(cases)}"
                data = detail(run_id, "Ship it", dispatches=[dispatch(WORKER, task=name)])
                data["tasks"] = tasks
                cases.append({"run": run_id, "name": name, "accepted": accepted, "detail": data})
        self.run_browser({"cases": cases, "worker": WORKER}, r'''
  for (const item of D.cases) {
    navigate("#/runs/" + item.run);
    await respond("/api/runs/" + item.run, item.detail);
    const where = item.name + (item.accepted ? " accepted" : " pending");
    assert.equal(one(attrIs("data-task", item.name)).getAttribute("data-state"), item.accepted ? "accepted" : "pending", where);
    const call = one(attrIs("data-ui-key", "dispatch:" + D.worker));
    const chips = all(function (n) { return n.className === "chip" && n.textContent.startsWith("worker 报告完成"); }, call);
    assert.deepEqual(chips.map(function (n) { return n.textContent; }),
      [item.accepted ? "worker 报告完成" : "worker 报告完成（尚未接受）"], where);
  }
''')

    def test_rework_time_line_names_the_latest_conclusion(self):
        # Attempt 1 has an approving intermediate batch before its rejection, so each later round is its attempt + 1.
        def ledger(last, attempts=3, history=None, **fields):
            records = history or [approval(1, review_id(1)), rejection("F-1", attempt=1, dispatch=review_id(1)),
                                  rejection("F-2", attempt=2, dispatch=review_id(2)), last]
            return {"status": "running", "accepted": {}, "attempts": {"t2": attempts}, "review_history": {"t2": records},
                    **fields}
        ledgers = {
            "approve_rejected": ledger(rejection("F-3", attempt=3, dispatch=review_id(3), verdict="approve")),
            "rejected": ledger(rejection("F-3", attempt=3, dispatch=review_id(3))),
            "approved": ledger(approval(3, review_id(3)), pending_acceptance={
                "task_id": "t2", "attempt": 3, "review_source": {"dispatch_id": review_id(3), "snapshot_digest": "s3"}}),
            "invalidated": ledger(approval(3, review_id(3), status="invalidated"), attempts=4),
            # A counter behind its review records leaves the number of attempts since unknown.
            "unknown": ledger(None, attempts=2, history=[rejection("F-9", attempt=n, dispatch=review_id(n)) for n in (2, 3)])}
        cases = []
        for name, value in ledgers.items():
            summary = projected(value)
            hint, = summary["convergence"]["warnings"]
            latest, since = hint["latest_review"], hint["attempts_since_latest_review"]
            numbers = ([] if since is None else [hint["latest_attempt"], latest["round"]] if since == 0
                       else [latest["attempt"], latest["round"], hint["latest_attempt"], since])
            cases.append({"run": "run_" + name, "case": name, "since": since, "outcome": latest["outcome"],
                          "numbers": [str(number) for number in numbers],
                          "detail": detail("run_" + name, "Ship it", summary=summary)})
        self.assertEqual({case["case"]: (case["since"], case["outcome"], case["numbers"]) for case in cases}, {
            "approve_rejected": (0, "rejected", ["3", "4"]), "rejected": (0, "rejected", ["3", "4"]),
            "approved": (0, "approved", ["3", "4"]), "invalidated": (1, "invalidated", ["3", "4", "4", "1"]),
            "unknown": (None, "rejected", [])})
        self.run_browser({"cases": cases}, r'''
  // Rule 17's conclusions; the reviewer's verdict is named only for an approve that was rejected.
  const CONCLUSION = {rejected: "否决", approved: "批准，待接受", invalidated: "审阅记录已失效（候选变化）"};
  for (const item of D.cases) {
    navigate("#/runs/" + item.run);
    await respond("/api/runs/" + item.run, item.detail);
    const task = one(attrIs("data-rework-item", "task:t2"));
    assert.deepEqual([task.getAttribute("data-since-review"), task.getAttribute("data-latest-outcome")],
      [item.since === null ? "" : String(item.since), item.outcome], item.case);
    const when = visibleText(task.childNodes[task.childNodes.length - 1]);
    assert.deepEqual(when.match(/\d+/g) || [], item.numbers, item.case + ": " + when);
    if (item.since === null) { assert.ok(when.includes("尝试编号不可用"), when); continue; }
    for (const outcome of Object.keys(CONCLUSION)) {
      assert.equal(when.includes(CONCLUSION[outcome]), outcome === item.outcome, item.case + " / " + outcome + ": " + when);
    }
    assert.equal(when.includes("approve"), item.case === "approve_rejected", item.case + ": " + when);
  }
''')

    def test_rework_time_line_counts_attempt_numbers_apart_from_work_attempts(self):
        # Attempt 3 was rejected; the fourth worker call then failed on the usage limit and was exempted, so it took
        # attempt number 4 without counting as a work attempt (hand-written ledger).
        exempt = {"id": LATER, "role": "worker", "kind": "implement", "task_id": "t2", "status": "failed",
                  "process_state": "exited", "invoked": True,
                  "attempt_exemption": {"basis": "provider_error", "code": "usage_limit",
                                        "artifact": "/tmp/repo/.harness/runs/run_x/dispatches/" + LATER + "/process.stdout",
                                        "artifact_digest": "0" * 64}}
        ledger = {"status": "running", "accepted": {}, "attempts": {"t2": 4}, "dispatches": [exempt],
                  "review_history": {"t2": [rejection(f"F-{n}", attempt=n, dispatch=review_id(n)) for n in (1, 2, 3)]}}
        summary = projected(ledger)
        hint, = summary["convergence"]["warnings"]
        self.assertEqual((hint["work_attempts"], hint["reasons"], hint["latest_review"]["attempt"],
                          hint["latest_review"]["round"], hint["latest_attempt"], hint["attempts_since_latest_review"]),
                         (3, ["work_attempts"], 3, 3, 4, 1))
        self.run_browser({"detail": detail("run_exempt", "Ship it", summary=summary)}, r'''
  navigate("#/runs/run_exempt");
  await respond("/api/runs/run_exempt", D.detail);
  const rework = one(attrIs("data-rework", "true"));
  const task = one(attrIs("data-rework-item", "task:t2"), rework);
  assert.deepEqual(["data-latest-attempt", "data-since-review"].map(function (name) { return task.getAttribute(name); }),
    ["4", "1"]);
  // The first line counts work attempts: the exempt call is not one.
  const first = visibleText(task.childNodes[1]);
  assert.ok(first.startsWith("工作尝试 3 次"), first);
  // The time line names attempt numbers (concluded 3 in round 3, now number 4, one more) and not work attempts.
  const when = visibleText(task.childNodes[task.childNodes.length - 1]);
  assert.deepEqual(when.match(/\d+/g), ["3", "3", "4", "1"], when);
  assert.ok(when.includes("第 4 次") && when.includes("尝试编号"), when);
  assert.ok(!when.includes("工作尝试"), when);
  for (const word of ["此刻", "正在"]) { assert.ok(!visibleText(rework).includes(word), word); }
''')

    def test_pending_count_and_order_with_a_decision_a_rework_hint_and_a_quota_wait(self):
        wait = {"dispatch_id": WORKER, "role": "worker", "window": "session", "resets_at": 1762650000,
                "until": 1762650300, "recorded_at": 1762640000, "limit_seconds": 3600, "sample": None}
        request = {"id": "req-7", "task_id": "t2", "kind": "scope", "question": "QUESTION-7",
                   "artifact": "/tmp/repo/.harness/request.json", "artifact_digest": "0" * 64}
        def summary(ledger):
            value = status_summary(ledger)
            return {key: value[key] for key in ("convergence", "ledger_stage", "quota_wait", "pending_decisions")
                    if key in value}
        busy = summary({**sixth_attempt_ledger(), "quota_wait": wait, "decision_requests": [request]})
        quiet = summary({**sixth_attempt_ledger(), "quota_wait": wait})
        self.assertEqual((len(busy["pending_decisions"]), "pending_decisions" in quiet, busy["ledger_stage"]["step"]),
                         (1, False, "quota_wait"))
        data = {"run_busy": detail("run_busy", "Ship it", summary=busy), "run_quiet": detail("run_quiet", "Ship it",
                summary=quiet), "suggestion": busy["convergence"]["suggestion"]}
        self.run_browser(data, r'''
  for (const [runId, pending] of [["run_busy", 1], ["run_quiet", 0]]) {
    navigate("#/runs/" + runId);
    await respond("/api/runs/" + runId, D[runId]);
    const order = byId.main.childNodes;
    const attention = all(attrIs("data-attention", "true"));
    assert.equal(attention.length, pending, runId);
    one(attrIs("data-pending-count", String(pending)));
    if (pending) {
      assert.equal(all(tagIs("h3"), attention[0]).length, 1, "the decision is the only attention item");
      assert.ok(visibleText(attention[0]).includes("req-7"));
      assert.equal(order.indexOf(attention[0]), 1);
    }
    const rework = one(attrIs("data-rework", "true"));
    assert.deepEqual([order.indexOf(rework), order.indexOf(one(attrIs("data-overview", "true")))], [1 + pending, 2 + pending],
      runId + ": the rework history directly precedes the overview");
    const closing = rework.childNodes[rework.childNodes.length - 1];
    assert.equal(closing.textContent, D.suggestion, runId + ": the suggestion closes the rework section");
    let holder = one(attrIs("data-quota-wait", "true"));
    while (holder.parentNode !== byId.main) { holder = holder.parentNode; }
    assert.equal(holder, order[0], runId + ": the wait is in the run header");
    assert.equal(one(attrIs("data-stage", "quota_wait")).getAttribute("data-stage-attempt"), "6");
  }
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

    def test_call_failure_stop_shows_its_facts_after_the_excerpt(self):
        def failure(run_id, reason, data, title="与供应商的连接失败"):
            value = stop_value("CLI-EXCERPT-MARKER", kind="provider_failure", reason=reason, title=title,
                               exit_code=3, exits=["resume " + run_id])
            value["data"] = {"role": "worker", "provider": "claude", **data}
            return detail(run_id, "Ship it", status="blocked", stop=value)
        transport = failure("run_net", "transport", {
            "api_error": "connection_failed", "terminal_reason": "api_error", "http_status_state": "null",
            "exit_confirmed": True, "attempt_charged": False})
        hidden = failure("run_odd", "invocation_failed", {
            "api_error": "brand_new_kind", "unrecognized_labels": ["api_error_type", "terminal_reason"],
            "http_status_state": "valid", "http_status": 503, "exit_confirmed": False, "attempt_charged": True},
            title="模型调用失败（CLI 报告错误）")
        # Facts could not be read again: the judged status and the ledger's count still show.
        judged = failure("run_old", "invocation_failed", {"http_status": 429, "attempt_charged": True},
                         title="模型调用失败（CLI 报告错误）")
        plain = failure("run_bare", "unknown", {}, title="模型调用失败（原因未识别）")
        self.run_browser({"transport": transport, "hidden": hidden, "judged": judged, "plain": plain}, r'''
  async function facts(runId, body) {
    navigate("#/runs/" + runId);
    await respond("/api/runs/" + runId, body);
    const attention = one(attrIs("data-attention", "true"));
    const shown = visibleText(attention);
    const excerpt = one(function (n) { return n.textContent === "CLI-EXCERPT-MARKER"; }, attention);
    const row = excerpt.parentNode.childNodes;
    const next = row[row.indexOf(excerpt) + 1];
    return {shown: shown, next: next ? next.textContent : ""};
  }
  let seen = await facts("run_net", D.transport);
  for (const part of ["api_error：connection_failed", "terminal_reason：api_error", "HTTP 状态：null",
                      "进程退出：已确认", "工作尝试：未计"]) {
    assert.ok(seen.next.includes(part), "the line after the excerpt shows " + part + ": " + seen.next);
  }
  assert.ok(!seen.next.includes("api_error_type"), "an absent label is not shown");
  seen = await facts("run_odd", D.hidden);
  for (const part of ["api_error：brand_new_kind", "api_error_type：未识别", "terminal_reason：未识别",
                      "HTTP 状态：503", "进程退出：未确认", "工作尝试：已计"]) {
    assert.ok(seen.next.includes(part), "shows " + part + ": " + seen.next);
  }
  seen = await facts("run_old", D.judged);
  assert.equal(seen.next, "HTTP 状态：429 · 工作尝试：已计");
  seen = await facts("run_bare", D.plain);
  assert.ok(seen.next.startsWith("出口："), "no facts line without facts: " + seen.next);
  for (const word of ["HTTP 状态", "进程退出", "工作尝试", "api_error"]) {
    assert.ok(!seen.shown.includes(word), "nothing of the facts line: " + word);
  }
''')

    def test_damaged_ledger_and_pending_grants_stay_prominent_with_diagnostics_reachable(self):
        damaged = detail("run_dmg", "unused")
        damaged.update(run=None, summary=None, budgets=None, tasks=[], dispatches=[], schema_version=None,
                       ledger_error="state.json 无法解析：JSONDecodeError: LEDGER-BROKEN",
                       events={"exists": True, "tail_bytes": 65536, "size": 10, "truncated": False,
                               "changed_while_reading": False, "records": [], "unparsed_tail": True,
                               "invalid_lines": 0, "last_time": None})
        grants = detail("run_cmd", "Needs grants", summary={
            "missing_command_requirements": {"t2": [{"cwd": "APP-DIR", "argv": ["make", "MISSING-ARGV"]}]},
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
    skipped: {symlink: 0, other: 0, excluded: 0, unaddressable: 0}, files: [{path: "state.json", size: 9, mtime: 1}],
    observed: D.damaged.observed});
  assert.ok(all(function (n) { return n.tagName === "a" && n.href === "#/runs/run_dmg/files/state.json"; }).length >= 1);
  navigate("#/runs/run_cmd");
  await respond("/api/runs/run_cmd", D.grants);
  const shown = visibleText(one(attrIs("data-attention", "true")));
  for (const expected of ["APP-DIR", "MISSING-ARGV", "CONTINUATION-REASON", "--reject-request"]) {
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
  // No available sample reported credits: the card says so instead of leaving it blank.
  assert.equal(visibleText(one(attrIs("data-credits", "unreported"))), "credits：余额未报告");
  section = await panelOf("all_unavailable");
  const text = visibleText(section);
  assert.ok(text.includes("全部 3 个样本不可用：timeout×2、exited×1"));
  assert.ok(text.includes("2 个样本损坏或超大，已跳过") && text.includes("较早的 3 个样本未读取"));
  assert.ok(text.includes("较早样本未读取"), "the first segment's start is incomplete");
  section = await panelOf("unknown");
  const change = one(attrIs("data-net-change", "unknown"));
  assert.equal(one(attrIs("data-balance", "net"), change).textContent, "变化未知", "unknown is never shown as zero");
  assert.equal(all(attrIs("data-credits", "unreported")).length, 0, "credits were reported here");
''')

    def test_high_precision_balances_are_shown_verbatim_element_by_element(self):
        from test_quota_view import T, codex
        small = "0.123456789012345678901234567891"                                  # 30 decimals
        negative = "-999999999999999999999999999999.999999999999999999999999999999"  # 30 + 30 digits, other sign
        large = "123456789012345678901234567890.000000000000000000000000000001"
        view = quota_block([codex(T, credits=(False, small)), codex(T + 10, credits=(False, negative)),
                            codex(T + 20, credits=(True, "5")), codex(T + 30, credits=(False, large))])
        runs = view["providers"]["codex"]["credits"]["codex"]["runs"]
        expected = {"latest": large, "first": small, "last": negative, "net": runs[0]["net_change"], "single": large}
        self.assertEqual(expected["net"], "-1000000000000000000000000000000.123456789012345678901234567890")
        self.assertEqual([(run["observations"], run["net_change"]) for run in runs], [(2, expected["net"]), (1, None)])
        data = {"detail": detail("run_precise", "Precise", quota_view=view), "expected": expected}
        self.run_browser(data, r'''
  navigate("#/runs/run_precise");
  await respond("/api/runs/run_precise", D.detail);
  fire(one(attrIs("data-tab", "budget")), "click");
  const credits = one(attrIs("data-credits", "codex"));
  // Each value is compared verbatim in its own element, never searched for in the card's text.
  assert.equal(one(attrIs("data-balance", "latest"), credits).textContent, D.expected.latest);
  const rows = all(function (n) { return typeof n.getAttribute === "function" && n.getAttribute("data-net-change") !== null; },
                   credits);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].getAttribute("data-net-change"), D.expected.net);
  assert.equal(one(attrIs("data-balance", "net"), rows[0]).textContent, D.expected.net);
  assert.equal(one(attrIs("data-balance", "first"), rows[0]).textContent, D.expected.first);
  assert.equal(one(attrIs("data-balance", "last"), rows[0]).textContent, D.expected.last);
  // A run of one observation shows that balance and no change.
  assert.equal(rows[1].getAttribute("data-net-change"), "unknown");
  assert.equal(one(attrIs("data-balance", "first"), rows[1]).textContent, D.expected.single);
  assert.equal(all(attrIs("data-balance", "net"), rows[1]).length, 0);
  assert.ok(visibleText(rows[1]).startsWith("运行期间只有一个余额观测："));
''')

    def test_balances_stay_text_in_the_page_script(self):
        # The quota part of the page never turns a balance or a change into a number.
        start = APP_JS.index("var QUOTA_START")
        script = APP_JS[start:APP_JS.index("function budgetPanel")]
        for conversion in ("Number(", "parseFloat(", "parseInt(", "+run.", "+latest.", "toFixed("):
            self.assertNotIn(conversion, script)
        # A supplement only: the element-by-element test above is what catches any conversion.
        self.assertIsNone(re.search(r"(Number|parseFloat|parseInt)\s*\(|(balance|first|last|net_change)\s*[-*/]", script))

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
        unsupported = [{"path": "runs/run_old", "schema_version": 18,
                        "reason": f"台账版本 18 不支持（当前版本 {RUN_SCHEMA_VERSION}）"}]
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

    def test_header_and_title_follow_the_current_response(self):
        a = observed_at("/work/a/project/.harness", "/work/a/project")
        b = observed_at("/work/b/project/.harness", "/work/b/project")
        inferred = observed_at("/archive/copy/.harness")
        unknown = observed_at("/archive/state-copy")
        self.assertEqual((a["name"], b["name"], inferred["checkout_basis"], unknown["checkout"]),
                         ("a/project", "b/project", "path", None))
        listing = {"sampled_at": 2000.0, "runs": [], "skipped": [], "incomplete": [], "unsupported": []}
        data = {"a": a, "b": b, "inferred": inferred, "unknown": unknown,
                "list_a": dict(listing, observed=a),
                "detail_a": detail("run_one", "Goal", observed=a), "files_a": file_index("run_one", "state.json", observed=a),
                "detail_b": detail("run_one", "Goal", observed=b), "files_b": file_index("run_one", "state.json", observed=b),
                "detail_unknown": detail("run_one", "STALE-GOAL", observed=unknown),
                "file_inferred": {"source": "state.json", "size": 4, "tail_bytes": 65536, "truncated": False,
                                  "changed_while_reading": False, "sampled_at": 2000.0, "text": "FILE-BODY",
                                  "observed": inferred}}
        self.run_browser(data, r'''
  function header(o) {
    const shown = byId.binding;
    assert.equal(shown.getAttribute("data-state"), o.state);
    assert.equal(shown.getAttribute("data-checkout"), o.checkout === null ? "" : o.checkout);
    assert.equal(shown.getAttribute("data-checkout-basis"), o.checkout_basis === null ? "" : o.checkout_basis);
    assert.ok(shown.textContent.includes(o.state), shown.textContent);
    if (o.checkout !== null) { assert.ok(shown.textContent.includes(o.checkout), shown.textContent); }
    assert.ok(String(document.title).startsWith(o.name + " "), String(document.title));
  }
  // List, detail and file views each take the header and the title from their own response.
  await respond("/api/runs", D.list_a);
  header(D.a);
  navigate("#/runs/run_one");
  await respond("/api/runs/run_one", D.detail_a);
  await respond("/api/runs/run_one/files", D.files_a);
  header(D.a);
  const titleA = document.title;
  // The port's server now reads another checkout whose last segment is the same: the next render follows it.
  fire(byId.refresh, "click");
  await respond("/api/runs/run_one", D.detail_b);
  header(D.b);
  assert.notEqual(document.title, titleA, "same last segment, different title prefix");
  await respond("/api/runs/run_one/files", D.files_b);
  header(D.b);
  navigate("#/runs/run_one/files/state.json");
  await respond("/api/runs/run_one/files/state.json", D.file_inferred);
  assert.ok(byId.main.textContent.includes("FILE-BODY"));
  header(D.inferred);
  // Of two navigations, the earlier one's late response (a third directory) changes nothing.
  navigate("#/runs/run_one");
  navigate("#/");
  await respond("/api/runs", D.list_a);
  header(D.a);
  await respond("/api/runs/run_one", D.detail_unknown);
  assert.ok(!byId.main.textContent.includes("STALE-GOAL"));
  header(D.a);
  // A 404 JSON error body names its directory; the hint says the run is not in it.
  navigate("#/runs/run_gone");
  await respond("/api/runs/run_gone", {error: "run 不存在：run_gone", status: 404, observed: D.unknown}, false, 404);
  header(D.unknown);
  const locatedDetail = one(attrIs("data-not-found", "true")).textContent;
  navigate("#/runs/run_gone/files/state.json");
  await respond("/api/runs/run_gone/files/state.json", {error: "run 不存在：run_gone", status: 404, observed: D.b}, false, 404);
  header(D.b);
  const locatedFile = one(attrIs("data-not-found", "true")).textContent;
  // A plain-text 500 names no directory: the header keeps the last one shown.
  navigate("#/runs/run_one");
  await respondText("/api/runs/run_one", 500, "500 internal error\n");
  assert.ok(byId.main.textContent.includes("HTTP 500"));
  assert.equal(all(attrIs("data-not-found", "true")).length, 0);
  header(D.b);
  // A 404 that names no directory keeps the earlier header, so its hint does not place the miss in that directory.
  navigate("#/runs/run_lost");
  await respond("/api/runs/run_lost", {error: "missing", status: 404}, false, 404);
  header(D.b);
  assert.notEqual(one(attrIs("data-not-found", "true")).textContent, locatedDetail);
  navigate("#/runs/run_lost/files/state.json");
  await respond("/api/runs/run_lost/files/state.json", {error: "missing", status: 404}, false, 404);
  header(D.b);
  assert.notEqual(one(attrIs("data-not-found", "true")).textContent, locatedFile);
''')

    def test_file_index_is_attached_only_to_a_detail_of_the_same_state_directory(self):
        a = observed_at("/work/a/project/.harness", "/work/a/project")
        b = observed_at("/work/b/project/.harness", "/work/b/project")
        data = {"detail_a": detail("run_one", "Goal", observed=a), "files_a": file_index("run_one", "a-only.txt", observed=a),
                "detail_b": detail("run_one", "Goal", observed=b),
                "files_b": file_index("run_one", "b-only.txt", "b-second.txt", observed=b)}
        self.run_browser(data, r'''
  function linked(path) {
    return all(function (n) { return n.tagName === "a" && n.href === "#/runs/run_one/files/" + path; }).length > 0;
  }
  function count() { return one(function (n) { return n.className === "count"; }, one(attrIs("data-tab", "files"))).textContent; }
  function mismatched() { return all(attrIs("data-files-mismatch", "true")).length; }
  navigate("#/runs/run_one");
  await respond("/api/runs/run_one", D.detail_a);
  await respond("/api/runs/run_one/files", D.files_a);
  assert.ok(linked("a-only.txt"));
  assert.equal(count(), "1");
  // Order one: after a refresh the same run ID comes from B first; A's cached index is not shown meanwhile.
  fire(byId.refresh, "click");
  await respond("/api/runs/run_one", D.detail_b);
  assert.ok(!linked("a-only.txt"), "the cached index of another state directory is dropped");
  assert.equal(count(), "…");
  await respond("/api/runs/run_one/files", D.files_b);
  assert.ok(linked("b-only.txt"));
  assert.equal(count(), "2");
  assert.equal(mismatched(), 0);
  // Order two: the detail comes from A, its index from B; the index is not attached and not cached.
  fire(byId.refresh, "click");
  await respond("/api/runs/run_one", D.detail_a);
  assert.ok(!linked("b-only.txt"));
  await respond("/api/runs/run_one/files", D.files_b);
  assert.ok(!linked("b-only.txt"), "an index of another state directory is never attached");
  assert.equal(count(), "…");
  assert.equal(mismatched(), 1);
  const note = one(attrIs("data-files-mismatch", "true")).textContent;
  assert.ok(note.includes(D.detail_a.observed.state) && note.includes(D.files_b.observed.state), note);
  // The refused index was not cached: once the detail comes from B as well, nothing of B is listed or counted
  // until B's own index arrives.
  fire(byId.refresh, "click");
  await respond("/api/runs/run_one", D.detail_b);
  assert.ok(!linked("b-only.txt"), "the refused index was not cached");
  assert.equal(count(), "…");
  await respond("/api/runs/run_one/files", D.files_b);
  assert.ok(linked("b-only.txt"));
  assert.equal(count(), "2");
  fire(byId.refresh, "click");
  await respond("/api/runs/run_one", D.detail_a);
  assert.ok(!linked("b-only.txt"));
  await respond("/api/runs/run_one/files", D.files_a);
  assert.ok(linked("a-only.txt"));
  assert.equal(count(), "1");
  assert.equal(mismatched(), 0);
''')

    def test_files_badge_and_panel_always_show_the_same_index_outcome(self):
        a = observed_at("/work/a/project/.harness", "/work/a/project")
        b = observed_at("/work/b/project/.harness", "/work/b/project")
        data = {"a": a, "b": b, "detail_a": detail("run_one", "Goal", observed=a),
                "files_a": file_index("run_one", "a-only.txt", observed=a),
                "files_a2": file_index("run_one", "a-only.txt", "a-second.txt", observed=a),
                "files_b": file_index("run_one", "b-only.txt", observed=b)}
        self.run_browser(data, r'''
  const prefix = "#/runs/run_one/files/";
  // What the files tab shows; the badge counts exactly the files the panel lists, and shows "…" when it lists none.
  function outcome(expected) {
    const panel = one(attrIs("data-panel", "files"));
    const shown = {
      count: one(function (n) { return n.className === "count"; }, one(attrIs("data-tab", "files"))).textContent,
      links: all(function (n) { return n.tagName === "a" && String(n.href).indexOf(prefix) === 0; }, panel)
        .map(function (n) { return n.href.slice(prefix.length); }),
      failed: all(function (n) { return n.className === "error-text"; }, panel).length > 0,
      mismatched: all(attrIs("data-files-mismatch", "true"), panel).length > 0
    };
    assert.equal(shown.count, shown.links.length ? String(shown.links.length) : "…", JSON.stringify(shown));
    assert.deepEqual(shown, expected);
  }
  const loading = {count: "…", links: [], failed: false, mismatched: false};
  const listedA = {count: "1", links: ["a-only.txt"], failed: false, mismatched: false};
  const failed = {count: "…", links: [], failed: true, mismatched: false};
  navigate("#/runs/run_one");
  await respond("/api/runs/run_one", D.detail_a);
  outcome(loading);
  await respond("/api/runs/run_one/files", D.files_a);
  outcome(listedA);
  // The review's order: the detail still comes from A and shows A's cached index; the index request then reaches
  // a server restarted on B, which answers 404 naming its own directory.
  fire(byId.refresh, "click");
  await respond("/api/runs/run_one", D.detail_a);
  outcome(listedA);
  await respond("/api/runs/run_one/files", {error: "run 不存在：run_one", status: 404, observed: D.b}, false, 404);
  outcome(failed);
  assert.equal(byId.binding.getAttribute("data-state"), D.a.state, "an index response never moves the header");
  // A failure that names no directory.
  fire(byId.refresh, "click");
  await respond("/api/runs/run_one", D.detail_a);
  outcome(listedA);
  await respondText("/api/runs/run_one/files", 500, "500 internal error\n");
  outcome(failed);
  // An index read from another directory.
  fire(byId.refresh, "click");
  await respond("/api/runs/run_one", D.detail_a);
  await respond("/api/runs/run_one/files", D.files_b);
  outcome({count: "…", links: [], failed: false, mismatched: true});
  // Superseded: the older render's index fails after a newer render has shown its detail; the newer render keeps
  // its own panel and badge until its own index arrives.
  fire(byId.refresh, "click");
  await respond("/api/runs/run_one", D.detail_a);
  fire(byId.refresh, "click");
  await respond("/api/runs/run_one", D.detail_a);
  outcome(listedA);
  await respond("/api/runs/run_one/files", {error: "run 不存在：run_one", status: 404, observed: D.b}, false, 404);
  outcome(listedA);
  await respond("/api/runs/run_one/files", D.files_a2);
  outcome({count: "2", links: ["a-only.txt", "a-second.txt"], failed: false, mismatched: false});
''')

    def test_long_paths_wrap_in_the_files_note_and_the_header_is_bounded_with_full_paths_kept(self):
        def segment(tag):
            # One legal path segment of 249 characters with no break opportunity, as in the review.
            return (tag + "Segment0123456789" * 20)[:249]
        checkouts = {name: "/work/" + segment(name + "1") + "/" + segment(name + "2") + "/project" for name in "AB"}
        a = observed_at(checkouts["A"] + "/.harness", checkouts["A"])
        b = observed_at(checkouts["B"] + "/.harness", checkouts["B"])
        rules = class_rules(APP_CSS)
        header = re.search(r'<p id="binding" class="([^"]*)"', INDEX_HTML).group(1).split()
        # The header wraps anywhere and its height is bounded; what does not fit scrolls or is cut, not pushed down.
        self.assertTrue(any(rules.get(name, {}).get("overflow-wrap") == "anywhere" for name in header), header)
        self.assertTrue(any(rules.get(name, {}).get("max-height", "none") != "none" and
                            rules[name].get("overflow-y", rules[name].get("overflow")) in ("auto", "scroll", "hidden")
                            for name in header), header)
        breaking = sorted(name for name, declarations in rules.items()
                          if declarations.get("overflow-wrap") == "anywhere" or declarations.get("word-break") == "break-all")
        data = {"a": a, "b": b, "breaking": breaking, "detail_a": detail("run_one", "Goal", observed=a),
                "files_b": file_index("run_one", "b-only.txt", observed=b)}
        self.run_browser(data, r'''
  navigate("#/runs/run_one");
  await respond("/api/runs/run_one", D.detail_a);
  // The full paths stay reachable from the bounded header: its tooltip is the whole text, its attributes the values.
  const shown = byId.binding;
  assert.equal(shown.getAttribute("title"), shown.textContent);
  assert.ok(shown.textContent.includes(D.a.checkout) && shown.textContent.includes(D.a.state), shown.textContent);
  assert.equal(shown.getAttribute("data-checkout"), D.a.checkout);
  assert.equal(shown.getAttribute("data-state"), D.a.state);
  await respond("/api/runs/run_one/files", D.files_b);
  const note = one(attrIs("data-files-mismatch", "true"));
  assert.ok(note.textContent.includes(D.a.state) && note.textContent.includes(D.b.state), note.textContent);
  assert.ok(note.className.split(" ").some(function (name) { return D.breaking.indexOf(name) >= 0; }),
            "the note naming both paths breaks long words: " + note.className);
''')


if __name__ == "__main__":
    unittest.main()
