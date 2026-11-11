"""Static page for the local read-only dashboard, shipped as module strings.

The HTML carries no inline script or style; the JS renders every value through
textContent/createElement and never assigns markup. No external resources.
"""

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Harness 观察台</title>
<link rel="stylesheet" href="/app.css">
</head>
<body>
<header class="appbar">
  <div class="appbar-inner">
    <h1 class="brand"><a href="#/"><span class="brand-mark" aria-hidden="true"></span>Harness 观察台</a></h1>
    <div class="controls">
      <span id="connection" class="conn" role="status" aria-live="polite"></span>
      <span id="sampled" class="muted small"></span>
      <label class="toggle"><input id="auto" type="checkbox" checked> 跟随运行变化</label>
      <button id="refresh" type="button" class="button">刷新</button>
    </div>
  </div>
</header>
<main id="main" class="page"><p class="muted">载入中…</p></main>
<footer class="page footnote muted small">只读本地视图：各文件分别读取，不是原子快照；事件连接在线不代表模型仍在工作。</footer>
<script src="/app.js"></script>
</body>
</html>
"""

APP_CSS = """:root {
  color-scheme: light dark;
  --bg: #f6f4f0; --surface: #ffffff; --surface-2: #f1ede7; --fg: #23211d; --muted: #6d675e; --line: #e3ddd4;
  --accent: #2f5f8a; --focus: #2f6fb0; --shadow: 0 1px 2px rgba(40, 32, 20, .06), 0 1px 1px rgba(40, 32, 20, .04);
  --ok: #276c45; --ok-bg: #e4f1e8; --warn: #845400; --warn-bg: #faefd8; --bad: #ad2318; --bad-bg: #fbe6e3;
  --info: #2f5f8a; --info-bg: #e3edf7; --neutral: #5f5950; --neutral-bg: #eeeae3;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #171614; --surface: #201f1c; --surface-2: #2a2824; --fg: #ece8e1; --muted: #a59d91; --line: #3a3732;
    --accent: #8db5df; --focus: #8db5df; --shadow: none;
    --ok: #79c795; --ok-bg: #1f3428; --warn: #e3b55c; --warn-bg: #3a2f18; --bad: #f19185; --bad-bg: #3f211d;
    --info: #8db5df; --info-bg: #1e2c3c; --neutral: #c3bbaf; --neutral-bg: #2d2b27;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; font: 14px/1.6 -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
  "Helvetica Neue", Arial, sans-serif; color: var(--fg); background: var(--bg); }
a { color: var(--accent); text-underline-offset: 2px; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 4px; }
.page { max-width: 1180px; margin: 0 auto; padding: 0 20px; }
main.page { padding-top: 20px; padding-bottom: 32px; }
.footnote { padding-bottom: 28px; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.mono { font-family: var(--mono); font-size: 12px; overflow-wrap: anywhere; }
.appbar { position: sticky; top: 0; z-index: 5; background: var(--surface); border-bottom: 1px solid var(--line); }
.appbar-inner { max-width: 1180px; margin: 0 auto; padding: 10px 20px; display: flex; flex-wrap: wrap; gap: 10px 16px;
  align-items: center; justify-content: space-between; }
.brand { margin: 0; font-size: 16px; font-weight: 650; letter-spacing: .01em; }
.brand a { color: inherit; text-decoration: none; display: inline-flex; align-items: center; gap: 8px; }
.brand-mark { width: 14px; height: 14px; border-radius: 4px; background: var(--accent); display: inline-block; }
.controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; }
.conn { font-size: 12px; color: var(--muted); display: inline-flex; align-items: center; gap: 6px; }
.conn::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--line); display: inline-block; }
.conn.tone-ok::before { background: var(--ok); } .conn.tone-warn::before { background: var(--warn); }
.conn.tone-bad::before { background: var(--bad); } .conn.tone-info::before { background: var(--info); }
.toggle { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; }
.button, button { font: inherit; font-size: 13px; padding: 5px 14px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--surface); color: var(--fg); cursor: pointer; }
.button:hover, button:hover { background: var(--surface-2); }
.linkbutton { border: 0; background: none; padding: 0; color: var(--accent); text-decoration: underline;
  text-underline-offset: 2px; font-size: 13px; }
.linkbutton:hover { background: none; }
input[type=number], input[type=search] { font: inherit; font-size: 13px; padding: 5px 8px; border: 1px solid var(--line);
  border-radius: 8px; background: var(--surface); color: var(--fg); }
input[type=number] { width: 9em; }
input[type=search] { width: min(320px, 100%); }
h2, h3 { line-height: 1.35; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow);
  padding: 16px 18px; margin: 0 0 16px; }
.card > h2, .card-head h2 { font-size: 15px; margin: 0; }
.card-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 6px 12px;
  margin-bottom: 10px; }
.crumbs { font-size: 13px; margin: 0 0 10px; }
.hero { padding: 20px 22px; }
.hero-top { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }
.hero h2 { font-size: 20px; margin: 0 0 6px; font-weight: 650; overflow-wrap: anywhere; }
.meta-line { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 12px; color: var(--muted); }
.badge { display: inline-flex; align-items: center; gap: 4px; padding: 1px 9px; border-radius: 999px; font-size: 12px;
  font-weight: 600; line-height: 1.7; white-space: nowrap; }
.chip { display: inline-block; padding: 0 7px; border-radius: 6px; font-size: 12px; background: var(--surface-2);
  color: var(--muted); white-space: nowrap; }
.tone-ok { color: var(--ok); } .badge.tone-ok, .chip.tone-ok { background: var(--ok-bg); }
.tone-warn { color: var(--warn); } .badge.tone-warn, .chip.tone-warn { background: var(--warn-bg); }
.tone-bad { color: var(--bad); } .badge.tone-bad, .chip.tone-bad { background: var(--bad-bg); }
.tone-info { color: var(--info); } .badge.tone-info, .chip.tone-info { background: var(--info-bg); }
.tone-neutral { color: var(--neutral); } .badge.tone-neutral, .chip.tone-neutral { background: var(--neutral-bg); }
.metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin: 0 0 16px; }
.metric { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px;
  box-shadow: var(--shadow); min-width: 0; }
.metric-label { font-size: 12px; color: var(--muted); margin: 0 0 4px; }
.metric-value { font-size: 18px; font-weight: 650; margin: 0; overflow-wrap: anywhere; }
.metric-note { font-size: 12px; color: var(--muted); margin: 4px 0 0; overflow-wrap: anywhere; }
progress { width: 100%; height: 6px; border: 0; border-radius: 999px; background: var(--surface-2); overflow: hidden;
  -webkit-appearance: none; appearance: none; margin-top: 6px; display: block; }
progress::-webkit-progress-bar { background: var(--surface-2); border-radius: 999px; }
progress::-webkit-progress-value { background: var(--accent); border-radius: 999px; }
progress::-moz-progress-bar { background: var(--accent); border-radius: 999px; }
progress.tone-ok::-webkit-progress-value { background: var(--ok); } progress.tone-ok::-moz-progress-bar { background: var(--ok); }
progress.tone-warn::-webkit-progress-value { background: var(--warn); } progress.tone-warn::-moz-progress-bar { background: var(--warn); }
progress.tone-bad::-webkit-progress-value { background: var(--bad); } progress.tone-bad::-moz-progress-bar { background: var(--bad); }
.attention { border-color: var(--warn); }
.attention.has-bad { border-color: var(--bad); }
.alerts { display: grid; gap: 10px; }
.alert { border-left: 4px solid var(--line); background: var(--surface-2); border-radius: 8px; padding: 10px 12px; min-width: 0; }
.alert.tone-bad { border-left-color: var(--bad); background: var(--bad-bg); color: var(--fg); }
.alert.tone-warn { border-left-color: var(--warn); background: var(--warn-bg); color: var(--fg); }
.alert h3 { font-size: 14px; margin: 0 0 4px; }
.alert.tone-bad h3 { color: var(--bad); } .alert.tone-warn h3 { color: var(--warn); }
.alert p { margin: 4px 0; overflow-wrap: anywhere; }
.hint { font-size: 12px; color: var(--muted); }
.hint code { font-family: var(--mono); background: var(--surface); padding: 0 4px; border-radius: 4px; }
.split { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 16px; }
.split > .card { margin-bottom: 16px; }
ul.items { list-style: none; margin: 0; padding: 0; }
ul.items > li { padding: 9px 0; border-top: 1px solid var(--line); min-width: 0; }
ul.items > li:first-child { border-top: 0; padding-top: 2px; }
.item-head { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; }
.item-title { font-weight: 600; overflow-wrap: anywhere; }
.item-sub { font-size: 12px; color: var(--muted); margin-top: 2px; overflow-wrap: anywhere; }
li[data-state=current] { background: linear-gradient(90deg, var(--info-bg), transparent 70%); border-radius: 6px;
  padding-left: 8px; padding-right: 8px; }
.excerpt { margin: 0; overflow-wrap: anywhere; }
details.disclosure { margin: 6px 0 0; }
details.disclosure > summary { cursor: pointer; color: var(--accent); font-size: 13px; width: fit-content; max-width: 100%; }
details.disclosure > summary:hover { text-decoration: underline; text-underline-offset: 2px; }
details.block { border: 1px solid var(--line); border-radius: 10px; padding: 8px 12px; margin: 10px 0 0; }
details.block > summary { color: var(--fg); font-weight: 600; }
details.block[open] > summary { margin-bottom: 8px; }
pre.fulltext, pre.log { font-family: var(--mono); font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere;
  background: var(--surface-2); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; margin: 8px 0 0;
  max-height: 60vh; overflow: auto; }
pre.log { max-height: 70vh; margin: 0; }
.tablist { display: flex; gap: 2px; overflow-x: auto; border-bottom: 1px solid var(--line); margin: -4px -4px 14px;
  padding: 0 4px; scrollbar-width: thin; }
.tab { border: 0; border-bottom: 2px solid transparent; border-radius: 6px 6px 0 0; background: none; padding: 8px 12px;
  color: var(--muted); white-space: nowrap; display: inline-flex; align-items: center; gap: 6px; }
.tab:hover { background: var(--surface-2); color: var(--fg); }
.tab.selected { color: var(--fg); border-bottom-color: var(--accent); font-weight: 600; }
.count { font-size: 11px; padding: 0 6px; border-radius: 999px; background: var(--surface-2); color: var(--muted);
  font-weight: 600; }
.count.tone-bad { background: var(--bad-bg); color: var(--bad); } .count.tone-warn { background: var(--warn-bg); color: var(--warn); }
.tabpanel:focus-visible { outline-offset: 4px; }
.wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; vertical-align: top; padding: 6px 10px; border-bottom: 1px solid var(--line); }
th { font-weight: 600; color: var(--muted); font-size: 12px; background: var(--surface); }
tbody tr:hover { background: var(--surface-2); }
dl.kv { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 4px 18px; margin: 0; }
dl.kv dt { color: var(--muted); }
dl.kv dd { margin: 0; overflow-wrap: anywhere; }
.grid-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
.mini { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; min-width: 0; }
.mini h3 { font-size: 13px; margin: 0 0 4px; }
.mini .metric-value { font-size: 16px; }
details.dispatch { margin: 0; }
details.dispatch > summary { list-style: none; cursor: pointer; padding: 8px 4px; border-top: 1px solid var(--line);
  color: var(--fg); font-size: 13px; width: auto; }
details.dispatch > summary:hover { text-decoration: none; }
.dispatch > summary::-webkit-details-marker { display: none; }
.dispatch:first-child > summary { border-top: 0; }
.dispatch > summary:hover { background: var(--surface-2); }
.dispatch > summary::before { content: "▸"; color: var(--muted); display: inline-block; width: 1em; }
.dispatch[open] > summary::before { content: "▾"; }
.dispatch-line { display: inline-flex; flex-wrap: wrap; gap: 4px 10px; align-items: center; vertical-align: top;
  max-width: calc(100% - 1.2em); }
.dispatch-body { padding: 4px 4px 12px 1.2em; }
.error-line { color: var(--bad); font-size: 12px; overflow-wrap: anywhere; display: block; width: 100%; }
.runs { display: grid; gap: 12px; }
.run-card { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px;
  box-shadow: var(--shadow); min-width: 0; }
.run-card h3 { font-size: 15px; margin: 6px 0 4px; font-weight: 600; overflow-wrap: anywhere; }
.run-card h3 a { color: inherit; text-decoration: none; }
.run-card h3 a:hover { color: var(--accent); text-decoration: underline; }
.stats { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 14px; }
.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; margin: 10px 0; }
.empty { text-align: center; padding: 28px 12px; color: var(--muted); }
.error-text { color: var(--bad); white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 860px) { .split { grid-template-columns: 1fr; } }
@media (max-width: 600px) {
  .page, .appbar-inner { padding-left: 12px; padding-right: 12px; }
  .card, .hero { padding: 14px; border-radius: 10px; }
  .hero h2 { font-size: 17px; }
  dl.kv { grid-template-columns: 1fr; } dl.kv dt { margin-top: 6px; }
  .metrics { grid-template-columns: 1fr 1fr; }
  .controls { width: 100%; justify-content: space-between; }
}
@media (max-width: 380px) { .metrics { grid-template-columns: 1fr; } }
@media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto !important; transition: none !important; } }
"""

APP_JS = r""""use strict";
(function () {
  var UNAVAILABLE = "不可用";
  var DEFAULT_TAIL = 65536;
  var MAX_TAIL = 1048576;
  var GOAL_EXCERPT = 180;
  var TEXT_EXCERPT = 140;
  var RECENT_EVENTS = 8;
  var main = document.getElementById("main");
  var sampledLabel = document.getElementById("sampled");
  var connectionLabel = document.getElementById("connection");
  var stream = null;
  var streamRun = null;
  var refreshTimer = null;
  var renderSequence = 0;
  // Identity of the view currently shown in main; same identity means an in-place refresh.
  var shownKey = null;
  // Disclosure/tab/filter state for one run only; replaced when another run is shown.
  var ui = freshUi(null);
  // Focusable controls of the latest render, by stable key, for focus restoration.
  var keyed = {};
  // Inner scroll positions of the shown view by data-scroll-key; reset whenever another view is shown.
  var scrollMemory = Object.create(null);

  var RUN_STATUS = {
    planning: ["规划中", "info"], running: ["运行中（台账）", "info"], awaiting_approval: ["等待批准计划", "warn"],
    paused: ["已暂停", "warn"], blocked: ["已阻断", "bad"], interrupted: ["已中断", "warn"],
    completed: ["已完成", "ok"], abandoned: ["已放弃", "neutral"]
  };
  var DISPATCH_STATUS = {
    prepared: ["准备中", "neutral"], running: ["调用中（台账）", "info"], completed: ["已返回", "info"],
    failed: ["失败", "bad"], invalidated: ["已作废", "neutral"], interrupted: ["已中断", "warn"]
  };
  var PROCESS_STATE = {
    prepared: "未启动", starting: "启动未确认", running: "运行中（台账）", exited: "已退出"
  };
  var INVOCATION = {
    usage_recorded: ["有用量记录", "neutral"], invoked_no_usage: ["已调用无用量", "neutral"],
    known_not_invoked: ["已知未调用", "neutral"], unknown: ["调用阶段未知", "warn"], verification: ["验证进程", "neutral"]
  };
  var ROLE = { commander: "指挥", worker: "worker", reviewer: "审阅", verification: "验证" };
  var OUTCOME = {
    completed: "worker 报告完成", task_blocked: "任务阻断", environment_blocked: "环境阻断",
    operator_blocked: "等待操作者", continuation_blocked: "等待续作授权", runtime_failed: "运行失败", unknown: "未知"
  };
  var EVENT = {
    dispatch_prepared: "准备派发", dispatch_started: "开始调用", dispatch_finished: "派发结束", task_accepted: "任务已接受",
    review_rejected: "审阅要求修改", verification_failed: "验证失败", worker_result_rejected: "worker 结果被拒绝",
    run_completed: "运行完成", run_paused: "运行暂停", run_stopped: "运行停止", run_abandoned: "运行已放弃",
    plan_awaiting_approval: "计划等待批准", plan_approved: "计划已批准", plan_revised: "计划已修订",
    final_verification_failed: "整体验证失败", final_review_rejected: "整体审阅拒绝",
    final_reverification_requested: "请求重新整体验证", environment_update_recorded: "登记环境更新",
    environment_checked: "环境检查", command_requirements_checked: "命令授权检查",
    task_checkpoint_recorded: "记录检查点", output_format_repair: "输出格式修正", output_invalid: "输出无效",
    invalid_output_repair_requested: "请求修正非法输出", candidate_preflight_failed: "候选预检失败",
    pending_task_invalidated: "待处理任务作废", pending_acceptance_invalidated: "待接受结果作废",
    worktree_removed: "工作树已删除", broad_scope_warning: "范围过宽提示", scope_extension_approved: "范围扩展已批准",
    error_hook_failed: "错误 hook 失败", resume_config_relaxed: "恢复时放宽配置"
  };

  function freshUi(runId) {
    return { runId: runId, tab: "dispatches", open: {}, fileFilter: "", files: null };
  }

  function connectionText(text, tone) {
    if (connectionLabel) {
      connectionLabel.textContent = text;
      connectionLabel.className = "conn" + (tone ? " tone-" + tone : "");
    }
  }

  function closeStream() {
    if (stream) { stream.close(); stream = null; }
    streamRun = null;
    if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null; }
  }

  function subscription(current) {
    if (!document.getElementById("auto").checked || current.view !== "detail") {
      closeStream();
      connectionText(current.view === "detail" ? "自动更新已关闭" : "列表与文件使用手动刷新");
      return;
    }
    if (typeof EventSource === "undefined") {
      connectionText("浏览器不支持事件订阅，请手动刷新", "warn");
      return;
    }
    if (stream && streamRun === current.runId) { return; }
    closeStream();
    var source = new EventSource("/api/runs/" + encodeURIComponent(current.runId) + "/stream");
    stream = source;
    streamRun = current.runId;
    connectionText("正在连接观察台…", "info");
    function changed() {
      if (stream !== source) { return; }
      connectionText("已连接，等待运行变化", "ok");
      if (!refreshTimer) {
        refreshTimer = setTimeout(function () { refreshTimer = null; render(); }, 100);
      }
    }
    // ready arrives after registration; this reread closes the initial fetch race.
    source.addEventListener("ready", changed);
    source.addEventListener("change", changed);
    function unavailable() {
      if (stream !== source) { return; }
      closeStream();
      connectionText("事件订阅已断开；点击刷新重新连接", "bad");
    }
    source.addEventListener("unavailable", unavailable);
    source.onerror = unavailable;
  }

  // ----- DOM helpers (textContent/createElement only) -----

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }
  // Missing values render as UNAVAILABLE; callers skip optional parts explicitly.
  function put(parent, child) {
    if (child instanceof Node) { parent.appendChild(child); } else { parent.appendChild(document.createTextNode(show(child))); }
    return parent;
  }
  function optional(value) {
    return value === null || value === undefined || value === "" ? el("span", "muted", "—") : value;
  }
  // Bounded single-line excerpt; returns null when the text already fits on one line.
  function clip(value, limit) {
    var flat = value.replace(/\s+/g, " ").trim();
    var characters = Array.from(flat);
    var cut = characters.length > limit;
    return { text: cut ? characters.slice(0, limit).join("") + "…" : flat, more: cut || /\n/.test(value.trim()) };
  }
  function attr(node, name, value) { node.setAttribute(name, String(value)); return node; }
  function show(value) {
    if (value === null || value === undefined || value === "") { return UNAVAILABLE; }
    if (typeof value === "boolean") { return value ? "是" : "否"; }
    if (typeof value === "object") { return JSON.stringify(value); }
    return String(value);
  }
  function fmtTime(value) {
    if (typeof value !== "number" || !isFinite(value)) { return UNAVAILABLE; }
    var date = new Date(value * 1000);
    if (isNaN(date.getTime())) { return UNAVAILABLE; }
    return date.toLocaleString("zh-CN", { hour12: false });
  }
  function fmtAgo(value, base) {
    if (typeof value !== "number" || typeof base !== "number" || !isFinite(value) || !isFinite(base)) { return UNAVAILABLE; }
    var seconds = Math.max(0, base - value);
    if (seconds < 60) { return Math.floor(seconds) + " 秒前"; }
    if (seconds < 3600) { return Math.floor(seconds / 60) + " 分钟前"; }
    if (seconds < 86400) { return Math.floor(seconds / 3600) + " 小时前"; }
    return Math.floor(seconds / 86400) + " 天前";
  }
  function fmtCost(value) {
    return typeof value === "number" ? "$" + value.toFixed(4) : UNAVAILABLE;
  }
  function fmtBytes(value) {
    if (typeof value !== "number") { return UNAVAILABLE; }
    if (value < 1024) { return value + " B"; }
    if (value < 1048576) { return (value / 1024).toFixed(1) + " KiB"; }
    return (value / 1048576).toFixed(2) + " MiB";
  }
  function fmtDuration(start, end) {
    if (typeof start !== "number" || typeof end !== "number") { return UNAVAILABLE; }
    var seconds = Math.max(0, end - start);
    if (seconds < 60) { return seconds.toFixed(1) + " 秒"; }
    if (seconds < 3600) { return Math.floor(seconds / 60) + " 分 " + Math.round(seconds % 60) + " 秒"; }
    return Math.floor(seconds / 3600) + " 小时 " + Math.floor((seconds % 3600) / 60) + " 分";
  }
  function shortId(value) {
    return typeof value === "string" && value.length > 12 ? value.slice(0, 8) + "…" : show(value);
  }
  function link(text, hash) {
    var node = el("a", null, text);
    node.href = hash;
    return node;
  }
  function runHash(runId) { return "#/runs/" + encodeURIComponent(runId); }
  function fileHash(runId, path) { return runHash(runId) + "/files/" + path; }
  function table(headers, rows, scrollKey) {
    var wrap = el("div", "wrap");
    if (scrollKey) { attr(wrap, "data-scroll-key", "table:" + scrollKey); }
    var node = el("table");
    var thead = el("thead");
    var tr = el("tr");
    headers.forEach(function (name) { attr(tr.appendChild(el("th", null, name)), "scope", "col"); });
    thead.appendChild(tr);
    node.appendChild(thead);
    var tbody = el("tbody");
    rows.forEach(function (cells) {
      var row = el("tr");
      cells.forEach(function (value) { put(row.appendChild(el("td")), value); });
      tbody.appendChild(row);
    });
    node.appendChild(tbody);
    wrap.appendChild(node);
    return wrap;
  }
  function kv(pairs) {
    var dl = el("dl", "kv");
    pairs.forEach(function (pair) {
      dl.appendChild(el("dt", null, pair[0]));
      put(dl.appendChild(el("dd")), pair[1]);
    });
    return dl;
  }
  function card(title, cls, extra) {
    var node = el("section", "card" + (cls ? " " + cls : ""));
    var head = el("div", "card-head");
    head.appendChild(el("h2", null, title));
    if (extra) { put(head, extra); }
    node.appendChild(head);
    return node;
  }
  function notice(text, cls) {
    return el("p", cls || "muted", text);
  }
  function mono(text) {
    return el("span", "mono", show(text));
  }
  function list(items) {
    var ul = el("ul", "items");
    items.forEach(function (item) { put(ul.appendChild(el("li")), item); });
    return ul;
  }
  function badge(value, map, cls) {
    var entry = map[value];
    var node = el("span", (cls || "badge") + " tone-" + (entry ? entry[1] : "neutral"), entry ? entry[0] : show(value));
    if (typeof value === "string") { node.title = "原始值：" + value; }
    return node;
  }
  function chip(text, tone) { return el("span", "chip" + (tone ? " tone-" + tone : ""), text); }
  function line(cls) {
    var node = el("div", cls);
    for (var i = 1; i < arguments.length; i++) {
      if (arguments[i] === null || arguments[i] === undefined) { continue; }
      put(node, arguments[i]);
    }
    return node;
  }

  // A disclosure whose open state is remembered per run under a stable key.
  function disclosure(key, title, cls) {
    var state = ui;
    var node = el("details", "disclosure" + (cls ? " " + cls : ""));
    attr(node, "data-ui-key", key);
    var summary = el("summary");
    if (title !== null) { put(summary, title); }
    attr(summary, "data-ui-key", "summary:" + key);
    keyed["summary:" + key] = summary;
    node.appendChild(summary);
    if (state.open[key]) { node.open = true; }
    node.addEventListener("toggle", function () {
      state.open[key] = !!node.open;
      // Content rebuilt while closed could not take its position; apply it once it has a box.
      if (node.open) { restoreScroll(node); }
    });
    return node;
  }

  // Bounded excerpt by default; the original text stays available, unmodified, behind a disclosure.
  function longText(key, value, limit, cls) {
    var wrap = el("div", "longtext");
    if (typeof value !== "string" || value === "") {
      wrap.appendChild(el("p", (cls || "excerpt") + " muted", UNAVAILABLE));
      return wrap;
    }
    var cut = clip(value, limit);
    wrap.appendChild(el("p", cls || "excerpt", cut.text));
    if (cut.more) { wrap.appendChild(fullText(key, "查看完整原文", value)); }
    return wrap;
  }
  function fullText(key, title, value) {
    var box = disclosure(key, title + "（共 " + Array.from(value).length + " 字）", "fulltext");
    var pre = el("pre", "fulltext");
    attr(pre, "data-scroll-key", "text:" + key);
    pre.textContent = value;
    box.appendChild(pre);
    return box;
  }

  function getJSON(url) {
    return fetch(url, { cache: "no-store", credentials: "omit" }).then(function (response) {
      return response.text().then(function (text) {
        var body = null;
        try { body = JSON.parse(text); } catch (error) { body = null; }
        if (!response.ok) {
          var message = body && body.error ? body.error : text.slice(0, 200);
          throw new Error("HTTP " + response.status + "：" + message);
        }
        return body;
      });
    });
  }

  function route() {
    var hash = location.hash || "#/";
    // "#", "#/" and "#//" all normalize to the list; addressable path components are never empty.
    var parts = hash.slice(1).split("/").filter(function (part) { return part !== ""; });
    if (parts.length === 0) { return { view: "list" }; }
    if (parts[0] === "runs" && parts.length === 2) { return { view: "detail", runId: parts[1] }; }
    if (parts[0] === "runs" && parts.length >= 4 && parts[2] === "files") {
      var rest = parts.slice(3).join("/");
      var tail = DEFAULT_TAIL;
      var query = rest.indexOf("?");
      if (query >= 0) {
        var value = parseInt(rest.slice(query + 1).replace(/^tail=/, ""), 10);
        rest = rest.slice(0, query);
        if (isFinite(value) && value >= 1 && value <= MAX_TAIL) { tail = value; }
      }
      return { view: "file", runId: parts[1], path: rest, tail: tail };
    }
    return { view: "unknown" };
  }

  function viewKey(current) {
    if (current.view === "detail") { return "detail:" + current.runId; }
    if (current.view === "file") { return "file:" + current.runId + "/" + current.path + "?" + current.tail; }
    return current.view;
  }

  function scrollY() { return typeof window.scrollY === "number" ? window.scrollY : 0; }
  function scrollTo(y) { if (typeof window.scrollTo === "function") { window.scrollTo(0, y); } }
  function within(node, parent) {
    while (node) { if (node === parent) { return true; } node = node.parentNode; }
    return false;
  }
  function captureFocus(region) {
    var active = document.activeElement;
    if (!active || typeof active.getAttribute !== "function" || (region && !within(active, region))) { return null; }
    var panel = active;
    while (panel && !(panel.getAttribute && panel.getAttribute("data-panel"))) { panel = panel.parentNode; }
    var saved = { key: active.getAttribute("data-ui-key"),
      fallback: panel ? "panel:" + panel.getAttribute("data-panel") : null };
    if (typeof active.selectionStart === "number" && typeof active.selectionEnd === "number") {
      saved.selection = [active.selectionStart, active.selectionEnd, active.selectionDirection || "none"];
    }
    return saved;
  }
  function refocus(saved) {
    if (!saved) { return; }
    var node = saved.key ? keyed[saved.key] : null;
    if (!node || !within(node, main)) { node = keyed[saved.fallback]; }
    if (!node || typeof node.focus !== "function") { return; }
    node.focus({ preventScroll: true });
    if (saved.selection && node.getAttribute("data-ui-key") === saved.key && typeof node.setSelectionRange === "function") {
      var length = String(node.value).length;
      try { node.setSelectionRange(Math.min(saved.selection[0], length), Math.min(saved.selection[1], length),
        saved.selection[2]); } catch (error) { /* unsupported type */ }
    }
  }

  function scrollContainers(root, visit) {
    var key = root.getAttribute && root.getAttribute("data-scroll-key");
    if (key) { visit(root, key); }
    Array.prototype.forEach.call(root.childNodes, function (child) { scrollContainers(child, visit); });
  }
  function tagOf(node) { return String(node.tagName || "").toLowerCase(); }
  // Only a box shown inside main has a real scroll position; hidden panels and closed disclosures read 0.
  function rendered(node) {
    var child = null;
    for (var n = node; n; child = n, n = n.parentNode) {
      if (n.hidden) { return false; }
      if (child && tagOf(n) === "details" && !n.open && tagOf(child) !== "summary") { return false; }
      if (n === main) { return typeof node.getClientRects !== "function" || node.getClientRects().length > 0; }
    }
    return false;
  }
  // Last known positions of the shown view's containers by stable key. Values of containers that are
  // currently hidden are kept, so a refresh or a deferred rebuild does not discard them.
  function captureScroll(root) {
    scrollContainers(root, function (node, key) {
      if (rendered(node)) { scrollMemory[key] = { top: node.scrollTop || 0, left: node.scrollLeft || 0 }; }
    });
  }
  function restoreScroll(root) {
    scrollContainers(root, function (node, key) {
      var saved = scrollMemory[key];
      if (saved && rendered(node)) { node.scrollTop = saved.top; node.scrollLeft = saved.left; }
    });
  }
  // Scroll events do not bubble; listen in the capture phase to track the user's latest position.
  main.addEventListener("scroll", function (event) {
    var node = event && event.target;
    var key = node && node.getAttribute ? node.getAttribute("data-scroll-key") : null;
    if (key && rendered(node)) { scrollMemory[key] = { top: node.scrollTop || 0, left: node.scrollLeft || 0 }; }
  }, true);

  // Preserve the reading position of the page and its independent scroll containers.
  function replaceMain(nodes, key) {
    var same = key !== undefined && key === shownKey;
    var y = scrollY();
    var focused = same ? captureFocus() : null;
    if (same) { captureScroll(main); } else if (key !== undefined) { scrollMemory = Object.create(null); }
    while (main.firstChild) { main.removeChild(main.firstChild); }
    nodes.forEach(function (node) { main.appendChild(node); });
    if (key !== undefined) {
      shownKey = key;
      scrollTo(same ? y : 0);
    }
    refocus(focused);
    if (same) { restoreScroll(main); }
  }

  function render() {
    var current = route();
    var sequence = ++renderSequence;
    var key = viewKey(current);
    subscription(current);
    if (key !== shownKey) {
      // A different view: never leave the previous route's content looking current.
      replaceMain([notice("载入中…")]);
      shownKey = "loading:" + key;
    }
    function currentResult(callback) {
      return function (data) { if (sequence === renderSequence) { callback(data); } };
    }
    var promise;
    if (current.view === "list") {
      promise = getJSON("/api/runs").then(currentResult(function (data) { renderList(data, key); }));
    } else if (current.view === "detail") {
      promise = getJSON("/api/runs/" + encodeURIComponent(current.runId)).then(currentResult(function (data) {
        renderDetail(current, data, sequence, key);
      }));
    } else if (current.view === "file") {
      var url = "/api/runs/" + encodeURIComponent(current.runId) + "/files/" + current.path +
        "?format=json&tail=" + current.tail;
      promise = getJSON(url).then(currentResult(function (data) { renderFile(current, data, key); }));
    } else {
      promise = Promise.resolve().then(currentResult(function () {
        replaceMain([errorCard("未知视图", "地址不是运行列表、运行详情或文件查看。")], key);
      }));
    }
    promise.catch(function (error) {
      if (sequence !== renderSequence) { return; }
      replaceMain([errorCard("读取失败：" + error.message, "可稍后点击刷新重试。")], "error:" + key);
    });
  }

  function errorCard(message, hint) {
    var node = el("section", "card");
    attr(node, "role", "alert");
    node.appendChild(el("p", "error-text", message));
    if (hint) { node.appendChild(notice(hint, "hint")); }
    node.appendChild(link("返回运行列表", "#/"));
    return node;
  }

  // ----- list -----

  function statusGroup(status) {
    if (status === "planning" || status === "running") { return "active"; }
    if (status === "blocked" || status === "interrupted" || status === "awaiting_approval" || status === "paused") {
      return "attention";
    }
    if (status === "completed" || status === "abandoned") { return "ended"; }
    return "other";
  }

  function renderList(data, key) {
    keyed = {};
    sampledLabel.textContent = "采样时间 " + fmtTime(data.sampled_at);
    var nodes = [];
    var runs = data.runs.slice().sort(function (a, b) {
      var x = typeof a.last_event_time === "number" ? a.last_event_time : -Infinity;
      var y = typeof b.last_event_time === "number" ? b.last_event_time : -Infinity;
      return y - x;
    });
    var head = el("section", "card");
    var top = el("div", "card-head");
    top.appendChild(el("h2", null, "运行列表（" + runs.length + "）"));
    top.appendChild(el("span", "small muted", "按最近事件排序；状态为台账记录值"));
    head.appendChild(top);
    if (runs.length === 0) {
      var empty = el("div", "empty");
      empty.appendChild(el("p", null, "还没有可读取的运行。"));
      empty.appendChild(el("p", "small", "在目标项目执行 run 后会出现在这里。"));
      head.appendChild(empty);
      nodes.push(head);
    } else {
      var counts = { attention: 0, active: 0, ended: 0, other: 0 };
      runs.forEach(function (run) { counts[statusGroup(run.status)] += 1; });
      var stats = el("div", "stats");
      stats.appendChild(chip("需要关注 " + counts.attention, counts.attention ? "warn" : null));
      stats.appendChild(chip("进行中（台账） " + counts.active, counts.active ? "info" : null));
      stats.appendChild(chip("已结束 " + counts.ended));
      if (counts.other) { stats.appendChild(chip("其他状态 " + counts.other)); }
      head.appendChild(stats);
      var grid = el("div", "runs");
      runs.forEach(function (run) { grid.appendChild(runCard(run, data.sampled_at)); });
      head.appendChild(grid);
      nodes.push(head);
    }
    if (data.skipped.length || data.incomplete.length) {
      var diag = el("section", "card");
      var box = disclosure("list:diagnostics", "台账诊断：跳过 " + data.skipped.length + " 个，字段不完整 " +
        data.incomplete.length + " 个", "block");
      if (data.skipped.length) {
        box.appendChild(el("h3", null, "已跳过的台账"));
        box.appendChild(list(data.skipped.map(function (item) { return item.path + "：" + item.reason; })));
      }
      if (data.incomplete.length) {
        box.appendChild(el("h3", null, "字段不完整的台账"));
        box.appendChild(list(data.incomplete));
      }
      diag.appendChild(box);
      nodes.push(diag);
    }
    replaceMain(nodes, key);
  }

  function runCard(run, sampled) {
    var node = el("article", "run-card");
    attr(node, "data-status", typeof run.status === "string" ? run.status : "");
    node.appendChild(line("item-head", badge(run.status, RUN_STATUS), mono(run.id)));
    var title = el("h3");
    var goal = typeof run.goal === "string" && run.goal.trim() ? run.goal : null;
    var cut = goal ? clip(goal, TEXT_EXCERPT) : null;
    title.appendChild(link(cut ? cut.text : "（目标不可用）" + show(run.id), runHash(run.run_dir)));
    node.appendChild(title);
    var meta = el("div", "meta-line");
    var when = el("span", null, "最近事件 " + fmtAgo(run.last_event_time, sampled));
    when.title = fmtTime(run.last_event_time);
    meta.appendChild(when);
    meta.appendChild(line(null, "分支 ", mono(run.branch)));
    meta.appendChild(el("span", null, "台账版本 " + show(run.schema_version)));
    node.appendChild(meta);
    if (cut && cut.more) { node.appendChild(fullText("list:goal:" + run.run_dir, "查看完整目标", goal)); }
    return node;
  }

  // ----- detail -----

  function lastDispatchFor(dispatches, taskId) {
    for (var i = dispatches.length - 1; i >= 0; i--) {
      if (dispatches[i].task_id === taskId && dispatches[i].role !== "verification") { return dispatches[i]; }
    }
    return null;
  }

  function dispatchSummary(row, accepted) {
    var parts = [(ROLE[row.role] || show(row.role)) + (row.kind ? " · " + row.kind : "")];
    var status = DISPATCH_STATUS[row.status] ? DISPATCH_STATUS[row.status][0] : show(row.status);
    parts.push("调用状态 " + status);
    if (row.process_state) { parts.push("进程记录 " + (PROCESS_STATE[row.process_state] || row.process_state)); }
    if (row.role === "worker" && row.worker_outcome) {
      parts.push((OUTCOME[row.worker_outcome] || row.worker_outcome) + (accepted ? "" : "（尚未接受）"));
    }
    return parts.join(" · ");
  }

  function resumeHint(runId, args) {
    var node = el("p", "hint", "常用出口：");
    node.appendChild(el("code", null, "resume " + runId + " " + args));
    return node;
  }

  function attentionItems(data, run, summary, current) {
    var items = [];
    function add(tone, title, body) { items.push({ tone: tone, title: title, body: body }); }
    if (data.ledger_error) { add("bad", "台账无法读取", [notice(data.ledger_error, "error-text")]); }
    if (data.summary_error) {
      add("bad", "状态摘要失败", [notice(data.summary_error, "error-text"), notice("其余区块按原始字段尽量给出。", "hint")]);
    }
    if (run.error) {
      add("bad", "运行错误" + (run.error_type ? "（" + run.error_type + "）" : ""), [notice(run.error, "error-text")]);
    } else if (run.status === "blocked") {
      add("bad", "运行已阻断", [notice("台账状态为 blocked，可在调用记录和事件日志中查看原因。")]);
    }
    if (run.status === "interrupted") { add("warn", "运行已中断", [notice("不会自动继续，需要显式恢复。")]); }
    if (run.status === "paused") { add("warn", "运行已暂停", [notice("不会自动继续，需要显式恢复。")]); }
    if (run.status === "awaiting_approval") {
      add("warn", "计划等待批准", [notice("普通恢复不会批准计划。"), resumeHint(data.id, "--approve-plan")]);
    }
    (summary.pending_decisions || []).forEach(function (item, index) {
      add("warn", "等待操作者决定" + (item.task_id ? "：" + item.task_id : ""), [
        longText("decision:" + show(item.id), item.question, TEXT_EXCERPT),
        line("item-sub", "请求 ", mono(item.id), " · 类型 " + show(item.kind) + " · 产物 ", mono(item.artifact)),
        resumeHint(data.id, "--operator-decision FILE")]);
    });
    var blocker = summary.last_environment_blocker;
    if (blocker && !blocker.stale) {
      add("bad", "当前环境阻断" + (blocker.task_id ? "：" + blocker.task_id : ""), [
        blocker.blockers && blocker.blockers.length ? list(blocker.blockers.map(function (text, index) {
          return longText("env:" + show(blocker.dispatch_id) + ":" + index, text, TEXT_EXCERPT);
        })) : notice("阻断项不可用"),
        line("item-sub", "来源调用 ", mono(blocker.dispatch_id),
          blocker.latest_update_id ? " · 最新环境更新 " + blocker.latest_update_id : null),
        resumeHint(data.id, "--environment-update FILE")]);
    }
    if (summary.missing_command_requirements) {
      add("warn", "缺失命令授权", [list(Object.keys(summary.missing_command_requirements).map(function (task) {
        return line(null, task + "：", mono(summary.missing_command_requirements[task]));
      })), notice("追加精确授权后普通 resume 即可重检。", "hint")]);
    }
    if (summary.continuation) {
      var cont = summary.continuation;
      add("warn", cont.state === "revision_pending" ? "续作请求已拒绝，等待修订" : "worker 请求续作授权", [
        line("item-sub", "任务 " + show(cont.task_id) + " · 状态 " + show(cont.state)),
        cont.request ? longText("continuation:request", JSON.stringify(cont.request), TEXT_EXCERPT) : null,
        cont.state === "revision_pending" ? null : notice("满足请求（追加精确授权或调整超时）后普通 resume 继续；需要先修正时可拒绝该请求。", "hint"),
        cont.state === "revision_pending" ? null : resumeHint(data.id, "--reject-continuation FILE")]);
    }
    if (summary.replan_stopped) {
      var stopped = summary.replan_stopped;
      add("warn", "指挥停止再规划", [line("item-sub", "范围 " + show(stopped.target)),
        longText("replan:reason", stopped.reason, TEXT_EXCERPT), line("item-sub", "产物 ", mono(stopped.artifact))]);
    }
    if (summary.convergence && summary.convergence.warnings && summary.convergence.warnings.length) {
      add("warn", "返工提示（仅供判断）", [list(summary.convergence.warnings.map(function (item) {
        var scope = item.kind === "whole_run_rework" ? "整体验收" : "任务 " + show(item.task_id);
        var text = scope + (item.work_attempts !== undefined ? "：工作尝试 " + item.work_attempts + " 次" : "：") +
          "，审阅拒绝 " + show(item.review_rejections) + " 次";
        if (item.recurring_findings && item.recurring_findings.length) {
          text += "，连续出现的阻断 " + item.recurring_findings.join("、");
        }
        return text;
      })), notice(summary.convergence.suggestion, "hint")]);
    }
    if (current && current.error && !current.accepted) {
      add("bad", "当前任务最近一次调用出错", [line("item-sub", dispatchSummary(current, false) + " · ", mono(current.id)),
        notice(current.error, "error-text")]);
    }
    items.sort(function (a, b) { return (a.tone === "bad" ? 0 : 1) - (b.tone === "bad" ? 0 : 1); });
    return items;
  }

  function metric(label, value, note, extra) {
    var node = el("div", "metric");
    node.appendChild(el("p", "metric-label", label));
    var holder = el("div", "metric-value");
    put(holder, value);
    node.appendChild(holder);
    if (extra) { node.appendChild(extra); }
    if (note) { put(node.appendChild(el("p", "metric-note")), note); }
    return node;
  }

  // Event names are observations; invoked comes from the ledger write they follow.
  // Older dispatch_started records (no invoked field) were written before the budget check.
  function eventLabel(record) {
    if (record.event === "dispatch_started" && record.invoked !== true) { return "派发准备（旧版事件，不表示已调用）"; }
    if (record.event === "dispatch_finished" && record.invoked === true) { return "调用结束"; }
    if (record.event === "dispatch_finished" && record.invoked === false) { return "派发结束（未调用）"; }
    return EVENT[record.event] || show(record.event);
  }

  function renderDetail(current, data, sequence, key) {
    if (ui.runId !== current.runId) { ui = freshUi(current.runId); }
    var state = ui;
    keyed = {};
    sampledLabel.textContent = "采样时间 " + fmtTime(data.sampled_at);
    var nodes = [];
    var run = data.run || {};
    var summary = data.summary || {};
    var tasks = data.tasks || [];
    var dispatches = data.dispatches || [];
    var records = data.events && data.events.records ? data.events.records : [];
    var acceptedCount = tasks.filter(function (task) { return task.accepted; }).length;
    var currentTask = null;
    tasks.forEach(function (task) { if (task.id === run.current_task) { currentTask = task; } });
    var currentDispatch = run.current_task ? lastDispatchFor(dispatches, run.current_task) : null;
    if (currentDispatch) {
      currentDispatch = Object.assign({}, currentDispatch, { accepted: !!(currentTask && currentTask.accepted) });
    }

    // Hero: what this run is, in one glance.
    var hero = el("section", "card hero");
    var crumbs = el("p", "crumbs");
    crumbs.appendChild(link("← 全部运行", "#/"));
    hero.appendChild(crumbs);
    hero.appendChild(line("hero-top", data.run ? badge(run.status, RUN_STATUS) : chip("台账不可用", "bad"),
      mono(data.id)));
    var goal = typeof run.goal === "string" && run.goal.trim() ? run.goal : null;
    var goalCut = goal ? clip(goal, GOAL_EXCERPT) : null;
    hero.appendChild(el("h2", null, goalCut ? goalCut.text : "运行 " + data.id));
    if (goalCut && goalCut.more) { hero.appendChild(fullText("goal", "查看完整目标原文", goal)); }
    var meta = el("div", "meta-line");
    meta.appendChild(line(null, "分支 ", mono(run.branch)));
    meta.appendChild(el("span", null, "指挥 " + show(run.commander)));
    meta.appendChild(el("span", null, "运行时长（按事件） " + fmtDuration(data.timing && data.timing.first_event_time,
      data.timing && data.timing.last_event_time)));
    hero.appendChild(meta);
    nodes.push(hero);

    // Attention first: errors, decisions and active blockers are never behind a closed control.
    var items = attentionItems(data, run, summary, currentDispatch);
    if (items.length) {
      var attention = card("需要处理（" + items.length + "）", "attention" +
        (items.some(function (item) { return item.tone === "bad"; }) ? " has-bad" : ""));
      attr(attention, "data-attention", "true");
      attr(attention, "aria-label", "需要处理的问题");
      var alerts = el("div", "alerts");
      items.forEach(function (item) {
        var box = el("div", "alert tone-" + item.tone);
        box.appendChild(el("h3", null, item.title));
        item.body.forEach(function (part) { if (part !== null && part !== undefined) { put(box, part); } });
        alerts.appendChild(box);
      });
      attention.appendChild(alerts);
      nodes.push(attention);
    }

    // Overview metrics.
    var metrics = el("div", "metrics");
    attr(metrics, "data-overview", "true");
    metrics.appendChild(metric("台账状态", data.run ? badge(run.status, RUN_STATUS) : UNAVAILABLE,
      "台账记录值，未探测进程"));
    var bar = el("progress", tasks.length && acceptedCount === tasks.length ? "tone-ok" : null);
    bar.max = Math.max(1, tasks.length);
    bar.value = acceptedCount;
    attr(bar, "aria-label", "已接受任务 " + acceptedCount + " / " + tasks.length);
    attr(bar, "data-progress", "tasks");
    metrics.appendChild(metric("任务进度", acceptedCount + " / " + tasks.length + " 已接受",
      tasks.length ? null : "台账中还没有任务", bar));
    metrics.appendChild(metric("当前任务", run.current_task ? run.current_task : "无",
      currentTask ? (currentTask.title ? clip(currentTask.title, 60).text : null) : (run.current_task ? "任务不在列表中" : null)));
    var last = records.length ? records[records.length - 1] : null;
    var lastNode = metric("最近观测", fmtAgo(data.timing && data.timing.last_event_time, data.sampled_at),
      last ? eventLabel(last) + (last.task_id ? " · " + last.task_id : "") : "events.jsonl 中没有可读事件");
    lastNode.title = fmtTime(data.timing && data.timing.last_event_time);
    metrics.appendChild(lastNode);
    metrics.appendChild(metric("待处理", items.length ? el("span", items.some(function (item) {
      return item.tone === "bad"; }) ? "tone-bad" : "tone-warn", items.length + " 项") : el("span", "tone-ok", "无"),
      items.length ? "见上方“需要处理”" : null));
    nodes.push(metrics);

    var explain = disclosure("explain", "如何理解这些状态", "small");
    explain.appendChild(list([
      data.notes && data.notes.process ? data.notes.process : "台账状态与进程是否仍在工作是两回事。",
      "本页不探测进程，只显示 process_state、exit_confirmed、pgid 与事件时间；没有新日志不等于卡死。",
      "worker completed 只表示 worker 返回了结果，不等于任务 accepted；接受以任务进度为准。",
      data.notes && data.notes.invoked ? data.notes.invoked : "invoked=true 不证明供应商已收到请求。",
      data.notes && data.notes.events ? data.notes.events : "事件只是观察，调用计数以台账为准。",
      "各文件分别读取，显示采样时间而不是原子快照。"]));
    nodes.push(explain);

    // Tasks and recent activity side by side.
    var split = el("div", "split");
    var taskCard = card("任务", null, chip(acceptedCount + " / " + tasks.length + " 已接受", acceptedCount && acceptedCount === tasks.length ? "ok" : null));
    if (!tasks.length) {
      taskCard.appendChild(notice(data.run ? "台账中还没有任务。" : "台账不可用，无法列出任务。"));
    } else {
      var ul = el("ul", "items");
      tasks.forEach(function (task) {
        var stateName = task.accepted ? "accepted" : (task.id === run.current_task ? "current" : "pending");
        var li = el("li");
        attr(li, "data-state", stateName);
        attr(li, "data-task", task.id);
        li.appendChild(line("item-head",
          stateName === "accepted" ? chip("已接受", "ok") : stateName === "current" ? chip("当前", "info") : chip("未接受"),
          el("span", "item-title", task.title || task.id)));
        li.appendChild(line("item-sub", mono(task.id),
          task.depends_on && task.depends_on.length ? " · 依赖 " + task.depends_on.join("、") : null));
        if (stateName === "current" && currentDispatch) {
          li.appendChild(el("div", "item-sub", "最近调用：" + dispatchSummary(currentDispatch, false)));
        }
        ul.appendChild(li);
      });
      taskCard.appendChild(ul);
    }
    if (data.invalid_tasks) { taskCard.appendChild(notice("跳过 " + data.invalid_tasks + " 个非对象任务项", "tone-warn small")); }
    split.appendChild(taskCard);

    var selectTab = null;
    var activity = card("最近活动");
    if (!records.length) {
      activity.appendChild(notice(data.events && data.events.exists === false ? "events.jsonl 不存在。" : "暂无可读事件。"));
    } else {
      activity.appendChild(list(records.slice(-RECENT_EVENTS).reverse().map(function (record) {
        var row = el("div");
        row.appendChild(line("item-head", el("span", "item-title", eventLabel(record)),
          record.task_id ? chip(record.task_id) : null));
        var sub = line("item-sub", fmtTime(record.time));
        if (record.dispatch_id) { sub.appendChild(document.createTextNode(" · 调用 ")); sub.appendChild(mono(shortId(record.dispatch_id))); }
        row.appendChild(sub);
        return row;
      })));
      var more = el("button", "linkbutton", "查看完整事件日志");
      more.type = "button";
      more.addEventListener("click", function () { if (selectTab) { selectTab("events", true); } });
      activity.appendChild(line("toolbar", more));
    }
    split.appendChild(activity);
    nodes.push(split);

    // Secondary detail grouped into tabs.
    var filesCount = el("span", "count", state.files ? state.files.files.length : "…");
    var filesPanel = el("div");
    var errored = dispatches.filter(function (row) { return row.error; }).length;
    var tabNode = tabs(state, [
      { id: "dispatches", label: "调用记录", badge: errored ? dispatches.length + " · " + errored + " 错误" : dispatches.length,
        tone: errored ? "bad" : null, node: dispatchPanel(data, dispatches, tasks) },
      { id: "events", label: "事件日志", badge: records.length, node: eventsPanel(data, records) },
      { id: "cost", label: "预算与费用", node: costPanel(data, summary) },
      { id: "operations", label: "操作记录", badge: data.operations ? data.operations.records.length : 0,
        node: operationsPanel(data) },
      { id: "meta", label: "运行信息", node: metaPanel(data, run, summary) },
      { id: "files", label: "文件", badgeNode: filesCount, node: filesPanel }
    ]);
    selectTab = tabNode.select;
    nodes.push(tabNode.node);

    function fillFiles(index, error) {
      captureScroll(filesPanel);
      Object.keys(keyed).forEach(function (name) {
        if (name.indexOf("file:") === 0 || name === "files:filter") { delete keyed[name]; }
      });
      while (filesPanel.firstChild) { filesPanel.removeChild(filesPanel.firstChild); }
      if (error) {
        filesPanel.appendChild(notice("文件索引读取失败：" + error, "error-text"));
        return;
      }
      if (!index) { filesPanel.appendChild(notice("载入中…")); return; }
      filesCount.textContent = String(index.files.length);
      filesPanel.appendChild(filesView(data.id, index, state));
      restoreScroll(filesPanel);
    }
    fillFiles(state.files);
    replaceMain(nodes, key);
    getJSON("/api/runs/" + encodeURIComponent(data.id) + "/files").then(function (index) {
      if (sequence !== renderSequence || state !== ui) { return; }
      state.files = index;
      var focused = captureFocus(filesPanel);
      fillFiles(index);
      refocus(focused);
    }).catch(function (error) {
      if (sequence !== renderSequence || state !== ui) { return; }
      var focused = captureFocus(filesPanel);
      fillFiles(null, error.message);
      refocus(focused);
    });
  }

  // Accessible tabs: roving tabindex, arrow/Home/End navigation, selection remembered per run.
  function tabs(state, panels) {
    var wrap = el("section", "card tabs");
    var bar = el("div", "tablist");
    attr(bar, "data-scroll-key", "tabs");
    attr(bar, "role", "tablist");
    attr(bar, "aria-label", "运行详细信息");
    var ids = panels.map(function (panel) { return panel.id; });
    if (ids.indexOf(state.tab) < 0) { state.tab = ids[0]; }
    var buttons = {};
    var bodies = {};
    panels.forEach(function (panel) {
      var button = el("button", "tab");
      button.type = "button";
      button.id = "tab-" + panel.id;
      attr(button, "role", "tab");
      attr(button, "aria-controls", "panel-" + panel.id);
      attr(button, "data-ui-key", "tab:" + panel.id);
      attr(button, "data-tab", panel.id);
      keyed["tab:" + panel.id] = button;
      button.appendChild(el("span", null, panel.label));
      if (panel.badgeNode) { button.appendChild(panel.badgeNode); }
      else if (panel.badge !== undefined) { button.appendChild(el("span", "count" + (panel.tone ? " tone-" + panel.tone : ""), panel.badge)); }
      button.addEventListener("click", function () { select(panel.id, false); });
      button.addEventListener("keydown", function (event) {
        var index = ids.indexOf(panel.id);
        var next = null;
        if (event.key === "ArrowRight") { next = ids[(index + 1) % ids.length]; }
        else if (event.key === "ArrowLeft") { next = ids[(index - 1 + ids.length) % ids.length]; }
        else if (event.key === "Home") { next = ids[0]; }
        else if (event.key === "End") { next = ids[ids.length - 1]; }
        if (next === null) { return; }
        if (event.preventDefault) { event.preventDefault(); }
        select(next, true);
      });
      bar.appendChild(button);
      var body = el("div", "tabpanel");
      body.id = "panel-" + panel.id;
      attr(body, "role", "tabpanel");
      attr(body, "aria-labelledby", button.id);
      attr(body, "tabindex", "0");
      attr(body, "data-panel", panel.id);
      attr(body, "data-ui-key", "panel:" + panel.id);
      keyed["panel:" + panel.id] = body;
      body.appendChild(panel.node);
      buttons[panel.id] = button;
      bodies[panel.id] = body;
    });
    wrap.appendChild(bar);
    ids.forEach(function (id) { wrap.appendChild(bodies[id]); });
    function select(id, focus) {
      if (!buttons[id]) { return; }
      // Remember the panel being hidden while it still has a box; reapply on the one being shown.
      if (bodies[state.tab] && state.tab !== id) { captureScroll(bodies[state.tab]); }
      state.tab = id;
      ids.forEach(function (other) {
        var on = other === id;
        attr(buttons[other], "aria-selected", on ? "true" : "false");
        buttons[other].tabIndex = on ? 0 : -1;
        buttons[other].className = "tab" + (on ? " selected" : "");
        bodies[other].hidden = !on;
      });
      restoreScroll(bodies[id]);
      if (focus && typeof buttons[id].focus === "function") { buttons[id].focus(); }
    }
    select(state.tab, false);
    return { node: wrap, select: select };
  }

  function dispatchPanel(data, dispatches, tasks) {
    var panel = el("div");
    var accepted = {};
    tasks.forEach(function (task) { if (task.accepted) { accepted[task.id] = true; } });
    if (data.invalid_dispatches) { panel.appendChild(notice("跳过 " + data.invalid_dispatches + " 个没有字符串 ID 的 dispatch 项", "tone-warn")); }
    if (!dispatches.length) {
      panel.appendChild(notice("台账中还没有调用记录。"));
      return panel;
    }
    panel.appendChild(notice("最新在上；展开一行查看进程、用量与产物文件。" + (data.notes && data.notes.invoked ? data.notes.invoked + "。" : ""), "hint"));
    var holder = el("div", "dispatches");
    dispatches.slice().reverse().forEach(function (row) {
      var box = disclosure("dispatch:" + row.id, null, "dispatch");
      var summary = box.firstChild;
      var head = el("span", "dispatch-line");
      head.appendChild(el("span", "item-title", (ROLE[row.role] || show(row.role)) + (row.kind ? " · " + row.kind : "")));
      if (row.task_id) { head.appendChild(chip(row.task_id)); }
      head.appendChild(badge(row.status, DISPATCH_STATUS));
      if (row.invocation_class === "unknown" || row.startup_unconfirmed) {
        var klass = badge(row.invocation_class, INVOCATION, "chip");
        if (row.startup_unconfirmed) { klass.textContent = klass.textContent + "（启动未确认）"; }
        if (row.invocation_reason) { klass.title = row.invocation_reason; }
        head.appendChild(klass);
      }
      if (row.role === "worker" && row.worker_outcome) {
        head.appendChild(chip((OUTCOME[row.worker_outcome] || row.worker_outcome) +
          (row.task_id && accepted[row.task_id] ? "" : "（尚未接受）")));
      }
      head.appendChild(el("span", "small muted", fmtTime(row.invoked_at)));
      head.appendChild(mono(shortId(row.id)));
      if (row.error) { head.appendChild(el("span", "error-line", "错误：" + row.error)); }
      summary.appendChild(head);
      var body = el("div", "dispatch-body");
      var files = el("span");
      row.files.forEach(function (name, index) {
        if (index) { files.appendChild(document.createTextNode(" ")); }
        files.appendChild(link(name, fileHash(data.id, "dispatches/" + row.id + "/" + name)));
      });
      var klassText = (INVOCATION[row.invocation_class] ? INVOCATION[row.invocation_class][0] : show(row.invocation_class)) +
        " (" + show(row.invocation_class) + ")" + (row.invocation_reason ? "：" + row.invocation_reason : "");
      body.appendChild(kv([
        ["dispatch", mono(row.id)], ["角色 / 类型", show(row.role) + " / " + show(row.kind)], ["任务", row.task_id],
        ["台账状态", row.status], ["进程状态", row.process_state], ["阶段", row.phase], ["invoked", row.invoked],
        ["调用时间", fmtTime(row.invoked_at)], ["结束事件时间", fmtTime(row.finished_at)],
        ["耗时（按台账与事件）", fmtDuration(row.invoked_at, row.finished_at)],
        ["pgid", row.pgid], ["退出已确认", row.exit_confirmed], ["调用分类", klassText],
        ["worker 结果", row.worker_outcome], ["供应商", row.provider], ["模型", row.model_id],
        ["用量（原字段）", usageText(row.provider_usage)], ["错误", row.error],
        ["文件", row.files.length ? files : "无"]
      ]));
      box.appendChild(body);
      holder.appendChild(box);
    });
    panel.appendChild(holder);
    return panel;
  }

  function usageText(usage) {
    if (!usage || typeof usage !== "object") { return UNAVAILABLE; }
    var parts = [];
    Object.keys(usage).forEach(function (key) {
      if (key === "provider" || key === "source") { return; }
      parts.push(key + "=" + show(usage[key]));
    });
    return parts.length ? parts.join("; ") : UNAVAILABLE;
  }

  function eventsPanel(data, records) {
    var panel = el("div");
    var events = data.events || {};
    if (events.error) { panel.appendChild(notice("事件读取失败：" + events.error, "error-text")); }
    if (!events.exists) {
      panel.appendChild(notice("events.jsonl 不存在。"));
      return panel;
    }
    panel.appendChild(notice("最新在上；读取尾部 " + fmtBytes(events.tail_bytes) + "，文件 " + fmtBytes(events.size) +
      (events.truncated ? "，较早事件未包含" : "") +
      (events.unparsed_tail ? "；最后一行未完成或无法解析（运行中属正常）" : "") +
      (events.invalid_lines ? "；无法解析 " + events.invalid_lines + " 行" : ""), "hint"));
    panel.appendChild(table(["时间", "事件", "任务", "dispatch", "其他字段"], records.slice().reverse().map(function (record) {
      var extra = {};
      Object.keys(record).forEach(function (key) {
        if (["time", "event", "run_id", "task_id", "dispatch_id"].indexOf(key) < 0) { extra[key] = record[key]; }
      });
      var extraText = JSON.stringify(extra);
      var name = el("span", null, eventLabel(record));
      name.title = show(record.event);
      var eventKey = "event:" + show(record.event_id || (String(record.time) + ":" + record.event + ":" + show(record.dispatch_id)));
      return [fmtTime(record.time), name, optional(record.task_id),
        record.dispatch_id ? mono(shortId(record.dispatch_id)) : optional(null),
        extraText === "{}" ? optional(null) : longText(eventKey, extraText, 160, "excerpt mono")];
    }), "events"));
    panel.appendChild(line("toolbar", link("查看 events.jsonl 原文（有界尾部）", fileHash(data.id, "events.jsonl"))));
    return panel;
  }

  function budgetMeter(label, used, limit) {
    var node = el("div", "mini");
    node.appendChild(el("h3", null, label));
    node.appendChild(el("div", "metric-value", show(used) + " / " + show(limit)));
    if (typeof used === "number" && typeof limit === "number" && limit > 0) {
      var ratio = used / limit;
      var bar = el("progress", ratio >= 1 ? "tone-bad" : ratio >= 0.8 ? "tone-warn" : null);
      bar.max = limit;
      bar.value = Math.min(used, limit);
      attr(bar, "aria-label", label + " " + used + " / " + limit);
      node.appendChild(bar);
      node.appendChild(el("p", "metric-note", "剩余 " + Math.max(0, limit - used)));
    } else {
      node.appendChild(el("p", "metric-note", "已用或上限不可用"));
    }
    return node;
  }

  function groupRow(name, group) {
    return [name, group.dispatches, group.invoked, group.usage_recorded, group.invoked_no_usage,
      group.known_not_invoked, group.unknown, fmtCost(group.reported_cost_usd.claude),
      fmtCost(group.reported_cost_usd.codex), fmtCost(group.reported_cost_usd.unknown)];
  }

  function costPanel(data, summary) {
    var panel = el("div");
    var b = data.budgets || {};
    var limits = b.limits || {};
    var attempts = summary.budgets && summary.budgets.work_attempts ? summary.budgets.work_attempts : b.attempts;
    panel.appendChild(el("h3", null, "预算"));
    var meters = el("div", "grid-cards");
    // Schema 16 has no run-wide worker limit: show the sum of task attempts. The
    // saved total_attempts / max_total_attempts pair exists only in older ledgers.
    if (typeof b.total_attempts === "number") {
      meters.appendChild(budgetMeter("总尝试（历史台账）", b.total_attempts, limits.max_total_attempts));
    } else {
      var total = el("div", "mini");
      total.appendChild(el("h3", null, "worker 尝试总数"));
      total.appendChild(el("div", "metric-value", show(b.attempts_total)));
      total.appendChild(el("p", "metric-note", "各任务 attempts 之和；全 run 上限由模型派发数约束"));
      meters.appendChild(total);
    }
    meters.appendChild(budgetMeter("模型派发", b.agent_dispatches, limits.max_agent_dispatches));
    meters.appendChild(budgetMeter("再规划", b.replan_attempts, limits.max_replans));
    meters.appendChild(budgetMeter("整体验收", b.final_attempts, limits.max_final_attempts));
    panel.appendChild(meters);
    var remaining = summary.remaining_model_calls;
    if (remaining && typeof remaining.message === "string") {
      var bound = notice(remaining.message + "；只读投影，不改变预算。", "hint");
      attr(bound, "data-remaining-calls", show(remaining.lower_bound));
      panel.appendChild(bound);
    }
    panel.appendChild(kv([
      ["任务尝试（work_attempts）", attempts && Object.keys(attempts).length ? Object.keys(attempts).map(function (key) {
        return key + "=" + attempts[key] + "/" + show(limits.max_attempts); }).join("，") : UNAVAILABLE],
      ["计数策略", typeof b.attempt_budget_policy === "string" ? b.attempt_budget_policy + "（历史台账保存）"
        : typeof data.schema_version === "number" && data.schema_version >= 16 ? "work（台账 16 起固定）"
        : b.attempt_budget_policy],
      ["单次调用超时（秒）", limits.agent_timeout_seconds]
    ]));

    var cost = data.cost;
    panel.appendChild(el("h3", null, "费用（按供应商分列）"));
    panel.appendChild(notice("以台账 dispatch ID 为唯一累计单位；不提供跨供应商合计；token 不跨 dispatch 或供应商相加；没有合法费用字段时显示“不可用”。", "hint"));
    var providers = el("div", "grid-cards");
    ["claude", "codex", "unknown"].forEach(function (name) {
      var item = cost.by_provider[name];
      var box = el("div", "mini");
      attr(box, "data-provider", name);
      box.appendChild(el("h3", null, name === "unknown" ? "未知供应商" : name));
      box.appendChild(el("div", "metric-value", fmtCost(item.reported_cost_usd)));
      box.appendChild(el("p", "metric-note", item.label));
      box.appendChild(el("p", "metric-note", "已调用 " + item.invoked + " · 有用量 " + item.usage_recorded +
        " · 无用量 " + item.invoked_no_usage + " · 计费 dispatch " + item.reported_cost_dispatches));
      providers.appendChild(box);
    });
    panel.appendChild(providers);
    panel.appendChild(kv([["已知未调用", cost.known_not_invoked], ["调用阶段未知", cost.unknown_invocation],
      ["启动未确认（starting）", cost.startup_unconfirmed], ["计入 dispatch 数", cost.counted_dispatches],
      ["重复 dispatch ID", cost.duplicate_dispatch_ids.length ? cost.duplicate_dispatch_ids.join("、") : "无"],
      ["验证进程", cost.by_group.verification.dispatches + " 次，不计入模型费用"]]));
    var groups = disclosure("cost:groups", "按组明细（协调与各任务）", "block");
    var rows = [groupRow("协调（指挥）", cost.by_group.coordination)];
    Object.keys(cost.by_group.tasks).sort().forEach(function (task) {
      rows.push(groupRow("任务 " + (task || "（无 task_id）"), cost.by_group.tasks[task]));
    });
    groups.appendChild(table(["组", "dispatch", "已调用", "有用量", "无用量", "已知未调用", "未知", "Claude 费用", "Codex 费用", "未知供应商费用"], rows, "cost-groups"));
    panel.appendChild(groups);
    if (summary.budget_explanation) {
      var explanation = disclosure("cost:dimensions", "调用与候选事实（独立计数维度）", "block");
      var dimensions = summary.budget_explanation.dimensions || {};
      explanation.appendChild(notice("计数来自台账关联；完成调用不代表接受，来源缺失保留未知。无效输出与失败分开计数。", "hint"));
      explanation.appendChild(kv(Object.keys(dimensions).map(function (key) {
        return [key, typeof dimensions[key] === "object" ? JSON.stringify(dimensions[key]) : dimensions[key]];
      })));
      explanation.appendChild(el("h3", null, "预算已用 / 上限 / 剩余"));
      explanation.appendChild(kv(Object.keys(summary.budget_explanation.limits || {}).map(function (key) {
        return [key, JSON.stringify(summary.budget_explanation.limits[key])];
      })));
      panel.appendChild(explanation);
    }
    return panel;
  }

  function operationsPanel(data) {
    var panel = el("div");
    var operations = data.operations || { records: [], scanned: 0, invalid: 0, truncated: false };
    if (!operations.records.length) {
      panel.appendChild(notice("没有关联到本 run 的操作记录。"));
    } else {
      panel.appendChild(table(["操作", "类型", "状态", "开始", "耗时", "结果", "错误", "详情"],
        operations.records.map(function (op) {
          var more = disclosure("operation:" + show(op.file), "技术字段");
          more.appendChild(kv([["记录文件", mono(op.file)], ["退出码", op.exit_status], ["结束", fmtTime(op.finished_at)],
            ["Harness 修订", op.harness && op.harness.revision], ["参数", op.arguments]]));
          return [mono(op.operation_id), op.kind, op.status, fmtTime(op.started_at), fmtDuration(op.started_at, op.finished_at),
            op.result_status, op.error ? el("span", "error-text", op.error) : optional(null), more];
        }), "operations"));
    }
    if (operations.invalid || operations.truncated) {
      panel.appendChild(notice("扫描 " + operations.scanned + " 个文件，非法 " + operations.invalid + " 个" +
        (operations.truncated ? "，已达扫描上限" : ""), "tone-warn"));
    }
    return panel;
  }

  function metaPanel(data, run, summary) {
    var panel = el("div");
    var unavailable = data.unavailable || [];
    panel.appendChild(kv([
      ["run", mono(data.id)], ["台账状态（原始值）", run.status], ["分支", mono(run.branch)], ["工作树（仅显示）", mono(run.worktree)], ["工作树已删除", run.worktree_removed],
      ["仓库（仅显示）", mono(run.repo)], ["配置（仅显示）", mono(run.config_path)], ["指挥", run.commander],
      ["当前任务", run.current_task],
      ["已接受任务", summary.accepted_tasks ? summary.accepted_tasks.join("、") || "无" : UNAVAILABLE],
      ["基线提交", mono(run.base_commit)], ["期望 HEAD", mono(run.expected_head)],
      ["台账版本", data.schema_version],
      ["创建该 run 的操作记录", unavailable.indexOf("operation") >= 0 ? "不可用（台账未记录）" : run.created_by_operation],
      ["首个事件时间", fmtTime(data.timing && data.timing.first_event_time)],
      ["最后事件时间", fmtTime(data.timing && data.timing.last_event_time)],
      ["事件时间跨度", fmtDuration(data.timing && data.timing.first_event_time, data.timing && data.timing.last_event_time)],
      ["错误类型", run.error_type], ["运行错误", run.error],
      ["不可用区块", unavailable.length ? unavailable.join("、") : "无"]
    ]));
    if (unavailable.indexOf("decisions") >= 0) { panel.appendChild(notice("操作者决定：不可用（台账版本过旧）", "hint")); }
    if (unavailable.indexOf("environment") >= 0) { panel.appendChild(notice("环境阻断：不可用（台账版本过旧）", "hint")); }
    if (summary.worker_outcomes) {
      panel.appendChild(el("h3", null, "worker 结果计数（观测值，不等于接受）"));
      panel.appendChild(kv(Object.keys(summary.worker_outcomes).map(function (key) {
        return [(OUTCOME[key] || key) + "（" + key + "）", summary.worker_outcomes[key]];
      })));
    }
    var blocker = summary.last_environment_blocker;
    if (blocker && blocker.stale) {
      panel.appendChild(el("h3", null, "历史环境阻断（已不再是当前阻断）"));
      panel.appendChild(kv([["任务", blocker.task_id], ["dispatch", mono(blocker.dispatch_id)],
        ["阻断项", blocker.blockers && blocker.blockers.length ? blocker.blockers.join("；") : UNAVAILABLE],
        ["最新环境更新", blocker.latest_update_id]]));
    }
    if (summary.continuation) {
      panel.appendChild(el("h3", null, "续作绑定"));
      panel.appendChild(kv(Object.keys(summary.continuation).map(function (key) { return [key, summary.continuation[key]]; })));
    }
    var rounds = summary.review_rounds && summary.review_rounds.tasks ? summary.review_rounds.tasks : null;
    if (rounds && Object.keys(rounds).length) {
      panel.appendChild(el("h3", null, "审阅逐轮首次出现的阻断问题（只读度量，不推断成因）"));
      var rows = [];
      Object.keys(rounds).forEach(function (task) {
        (Array.isArray(rounds[task]) ? rounds[task] : []).forEach(function (item) {
          var ids = Array.isArray(item.new_blocking) ? item.new_blocking : [];
          rows.push([task, show(item.round) + (item.status ? "（" + item.status + "）" : ""), item.verdict,
            ids.length ? ids.join("、") : optional(null), mono(item.review_dispatch_id), mono(item.snapshot_digest)]);
        });
      });
      panel.appendChild(table(["任务", "轮次", "结论", "首次出现的阻断", "审阅 dispatch", "候选摘要"], rows, "review-rounds"));
    }
    return panel;
  }

  function filesView(runId, index, state) {
    var view = el("div");
    view.appendChild(notice("共 " + index.files.length + " 个可读文件" + (index.truncated ? "（已达 " + index.limit + " 上限）" : "") +
      "；跳过符号链接 " + index.skipped.symlink + "、排除 " + index.skipped.excluded + "、不可寻址 " +
      index.skipped.unaddressable + "、其他 " + index.skipped.other, "hint"));
    var tools = el("div", "toolbar");
    var label = el("label", null, "筛选路径 ");
    var input = el("input");
    input.type = "search";
    input.value = state.fileFilter;
    attr(input, "data-ui-key", "files:filter");
    attr(input, "aria-label", "按路径筛选文件");
    keyed["files:filter"] = input;
    label.appendChild(input);
    tools.appendChild(label);
    var count = el("span", "small muted");
    tools.appendChild(count);
    view.appendChild(tools);
    var holder = el("div");
    view.appendChild(holder);
    function draw() {
      var needle = state.fileFilter.trim().toLowerCase();
      var shown = index.files.filter(function (file) { return !needle || file.path.toLowerCase().indexOf(needle) >= 0; });
      count.textContent = needle ? "匹配 " + shown.length + " 个" : "";
      while (holder.firstChild) { holder.removeChild(holder.firstChild); }
      if (!shown.length) { holder.appendChild(notice(needle ? "没有匹配的文件。" : "没有可读文件。")); return; }
      holder.appendChild(table(["路径", "大小", "修改时间"], shown.map(function (file) {
        var anchor = link(file.path, fileHash(runId, file.path));
        attr(anchor, "data-ui-key", "file:" + file.path);
        keyed["file:" + file.path] = anchor;
        return [anchor, fmtBytes(file.size), fmtTime(file.mtime)];
      }), "files"));
    }
    input.addEventListener("input", function () { state.fileFilter = String(input.value || ""); draw(); });
    draw();
    return view;
  }

  // ----- file -----

  function renderFile(current, data, key) {
    keyed = {};
    sampledLabel.textContent = "采样时间 " + fmtTime(data.sampled_at);
    var nodes = [];
    var head = el("section", "card");
    var crumbs = el("p", "crumbs");
    crumbs.appendChild(link("← 全部运行", "#/"));
    crumbs.appendChild(document.createTextNode(" / "));
    crumbs.appendChild(link("返回运行 " + current.runId, runHash(current.runId)));
    head.appendChild(crumbs);
    var title = el("h2", null, "文件 " + data.source);
    head.appendChild(title);
    head.appendChild(line("meta-line", "大小 " + fmtBytes(data.size), "　读取尾部 " + fmtBytes(data.tail_bytes),
      "　采样时间 " + fmtTime(data.sampled_at)));
    if (data.truncated) { head.appendChild(notice("只显示文件末尾 " + fmtBytes(data.tail_bytes) + "，较早内容未包含；可调大尾部字节数。", "hint tone-warn")); }
    if (data.changed_while_reading) { head.appendChild(notice("读取期间文件发生变化，内容可能不完整一致。", "hint tone-warn")); }
    var controls = el("div", "toolbar");
    var label = el("label", null, "尾部字节数 ");
    var input = el("input");
    input.type = "number";
    input.min = "1";
    input.max = String(MAX_TAIL);
    input.value = String(current.tail);
    label.appendChild(input);
    controls.appendChild(label);
    var apply = el("button", "button", "重新读取");
    apply.type = "button";
    apply.addEventListener("click", function () {
      var value = parseInt(input.value, 10);
      if (!isFinite(value) || value < 1 || value > MAX_TAIL) { value = DEFAULT_TAIL; }
      location.hash = fileHash(current.runId, current.path) + "?tail=" + value;
    });
    controls.appendChild(apply);
    head.appendChild(controls);
    head.appendChild(kv([["来源", mono(data.source)], ["已截断", data.truncated], ["读取期间文件变化", data.changed_while_reading]]));
    nodes.push(head);
    var body = card("内容", null, el("span", "small muted", "text/plain，按 UTF-8 解码，无法解码的字节已替换"));
    var pre = el("pre", "log");
    attr(pre, "data-scroll-key", "file-body");
    attr(pre, "data-ui-key", "file-body");
    keyed["file-body"] = pre;
    attr(pre, "tabindex", "0");
    attr(pre, "aria-label", "文件内容");
    pre.textContent = data.text;
    body.appendChild(pre);
    nodes.push(body);
    replaceMain(nodes, key);
  }

  document.getElementById("refresh").addEventListener("click", render);
  document.getElementById("auto").addEventListener("change", function (event) {
    subscription(route());
  });
  window.addEventListener("pagehide", closeStream);
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) { render(); }
  });
  window.addEventListener("hashchange", render);
  render();
})();
"""
