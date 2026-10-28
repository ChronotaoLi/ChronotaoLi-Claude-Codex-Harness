"""Static page for the local read-only dashboard, shipped as module strings.

The HTML carries no inline script or style; the JS renders every value through
textContent/createElement and never assigns innerHTML. No external resources.
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
<header>
  <h1><a href="#/">Harness 观察台</a></h1>
  <div class="controls">
    <span id="sampled" class="muted"></span>
    <button id="refresh" type="button">刷新</button>
    <label><input id="auto" type="checkbox"> 每 10 秒自动刷新</label>
  </div>
</header>
<p class="muted note">只读本地视图：不探测进程、不启动或恢复 run、不修改任何文件。各文件分别读取，不是原子快照。</p>
<main id="main"><p class="muted">载入中…</p></main>
<script src="/app.js"></script>
</body>
</html>
"""

APP_CSS = """:root { color-scheme: light dark; --fg: #1f2328; --bg: #ffffff; --muted: #6a737d; --line: #d0d7de;
  --panel: #f6f8fa; --ok: #1a7f37; --warn: #9a6700; --bad: #cf222e; --link: #0969da; }
@media (prefers-color-scheme: dark) { :root { --fg: #e6edf3; --bg: #0d1117; --muted: #8b949e; --line: #30363d;
  --panel: #161b22; --ok: #3fb950; --warn: #d29922; --bad: #f85149; --link: #58a6ff; } }
* { box-sizing: border-box; }
body { margin: 0; padding: 0 16px 48px; font: 14px/1.5 -apple-system, "PingFang SC", "Helvetica Neue", Arial, sans-serif;
  color: var(--fg); background: var(--bg); }
header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px;
  padding: 12px 0; border-bottom: 1px solid var(--line); }
header h1 { font-size: 18px; margin: 0; }
header h1 a { color: inherit; text-decoration: none; }
.controls { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
button { font: inherit; padding: 4px 12px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel);
  color: var(--fg); cursor: pointer; }
input[type=number] { font: inherit; width: 9em; padding: 3px 6px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--bg); color: var(--fg); }
a { color: var(--link); }
main { max-width: 1400px; margin: 0 auto; }
section { margin: 20px 0; }
h2 { font-size: 16px; margin: 0 0 8px; padding-bottom: 4px; border-bottom: 1px solid var(--line); }
h3 { font-size: 14px; margin: 12px 0 6px; }
.muted { color: var(--muted); }
.note { font-size: 13px; }
.error { color: var(--bad); white-space: pre-wrap; }
.warn { color: var(--warn); }
.ok { color: var(--ok); }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 8px 12px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; vertical-align: top; padding: 4px 8px; border-bottom: 1px solid var(--line); }
th { background: var(--panel); position: sticky; top: 0; }
.wrap { overflow-x: auto; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 16px; margin: 0; }
dt { color: var(--muted); }
dd { margin: 0; word-break: break-all; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
pre.log { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; white-space: pre-wrap;
  word-break: break-all; background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 8px;
  max-height: 70vh; overflow: auto; margin: 0; }
.row { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; margin: 6px 0; }
.badge { display: inline-block; padding: 0 6px; border-radius: 10px; border: 1px solid var(--line); font-size: 12px; }
ul.plain { margin: 4px 0; padding-left: 20px; }
@media (max-width: 720px) { dl { grid-template-columns: 1fr; } dt { margin-top: 6px; } }
"""

APP_JS = r""""use strict";
(function () {
  var UNAVAILABLE = "不可用";
  var DEFAULT_TAIL = 65536;
  var MAX_TAIL = 1048576;
  var main = document.getElementById("main");
  var sampledLabel = document.getElementById("sampled");
  var timer = null;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }
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
    return Math.floor(seconds / 60) + " 分 " + Math.round(seconds % 60) + " 秒";
  }
  function shortId(value) {
    return typeof value === "string" && value.length > 12 ? value.slice(0, 8) + "…" : show(value);
  }
  function link(text, hash) {
    var node = el("a", null, text);
    node.href = hash;
    return node;
  }
  function cell(value) {
    var td = el("td");
    if (value instanceof Node) { td.appendChild(value); } else { td.textContent = show(value); }
    return td;
  }
  function table(headers, rows) {
    var wrap = el("div", "wrap");
    var node = el("table");
    var thead = el("thead");
    var tr = el("tr");
    headers.forEach(function (name) { tr.appendChild(el("th", null, name)); });
    thead.appendChild(tr);
    node.appendChild(thead);
    var tbody = el("tbody");
    rows.forEach(function (cells) {
      var row = el("tr");
      cells.forEach(function (value) { row.appendChild(cell(value)); });
      tbody.appendChild(row);
    });
    node.appendChild(tbody);
    wrap.appendChild(node);
    return wrap;
  }
  function kv(pairs) {
    var dl = el("dl");
    pairs.forEach(function (pair) {
      dl.appendChild(el("dt", null, pair[0]));
      var dd = el("dd");
      if (pair[1] instanceof Node) { dd.appendChild(pair[1]); } else { dd.textContent = show(pair[1]); }
      dl.appendChild(dd);
    });
    return dl;
  }
  function section(title) {
    var node = el("section");
    node.appendChild(el("h2", null, title));
    return node;
  }
  function notice(text, cls) {
    return el("p", cls || "muted", text);
  }
  function mono(text) {
    return el("span", "mono", show(text));
  }
  function list(items) {
    var ul = el("ul", "plain");
    items.forEach(function (item) {
      var li = el("li");
      if (item instanceof Node) { li.appendChild(item); } else { li.textContent = show(item); }
      ul.appendChild(li);
    });
    return ul;
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

  function replaceMain(nodes) {
    while (main.firstChild) { main.removeChild(main.firstChild); }
    nodes.forEach(function (node) { main.appendChild(node); });
  }

  function render() {
    var current = route();
    var promise;
    if (current.view === "list") {
      promise = getJSON("/api/runs").then(renderList);
    } else if (current.view === "detail") {
      promise = getJSON("/api/runs/" + encodeURIComponent(current.runId)).then(renderDetail);
    } else if (current.view === "file") {
      var url = "/api/runs/" + encodeURIComponent(current.runId) + "/files/" + current.path +
        "?format=json&tail=" + current.tail;
      promise = getJSON(url).then(function (data) { renderFile(current, data); });
    } else {
      promise = Promise.resolve().then(function () { replaceMain([notice("未知视图", "error")]); });
    }
    promise.catch(function (error) {
      replaceMain([notice("读取失败：" + error.message, "error"),
        link("返回 run 列表", "#/")]);
    });
  }

  function renderList(data) {
    sampledLabel.textContent = "采样时间 " + fmtTime(data.sampled_at);
    var nodes = [];
    var runs = section("run 列表（" + data.runs.length + "）");
    if (data.runs.length === 0) {
      runs.appendChild(notice("没有可读取的 run。"));
    } else {
      runs.appendChild(table(["run", "台账状态", "目标", "分支", "最后事件时间", "台账版本"],
        data.runs.map(function (run) {
          return [link(run.id, "#/runs/" + encodeURIComponent(run.run_dir)), run.status, run.goal, mono(run.branch),
            fmtTime(run.last_event_time), run.schema_version];
        })));
    }
    nodes.push(runs);
    if (data.skipped.length || data.incomplete.length) {
      var diag = section("诊断");
      if (data.skipped.length) {
        diag.appendChild(el("h3", null, "已跳过的台账"));
        diag.appendChild(list(data.skipped.map(function (item) { return item.path + "：" + item.reason; })));
      }
      if (data.incomplete.length) {
        diag.appendChild(el("h3", null, "字段不完整的台账"));
        diag.appendChild(list(data.incomplete));
      }
      nodes.push(diag);
    }
    replaceMain(nodes);
  }

  function providerTable(cost) {
    var rows = ["claude", "codex", "unknown"].map(function (name) {
      var item = cost.by_provider[name];
      return [name, item.invoked, item.usage_recorded, item.invoked_no_usage, fmtCost(item.reported_cost_usd),
        item.reported_cost_dispatches, item.label];
    });
    return table(["供应商", "已调用", "有用量记录", "已调用无用量", "报告费用 (USD)", "计费 dispatch 数", "说明"], rows);
  }

  function groupRow(name, group) {
    return [name, group.dispatches, group.invoked, group.usage_recorded, group.invoked_no_usage,
      group.known_not_invoked, group.unknown, fmtCost(group.reported_cost_usd.claude),
      fmtCost(group.reported_cost_usd.codex), fmtCost(group.reported_cost_usd.unknown)];
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

  function renderDetail(data) {
    sampledLabel.textContent = "采样时间 " + fmtTime(data.sampled_at);
    var nodes = [];
    var run = data.run || {};
    var summary = data.summary || {};
    var head = section("run " + data.id);
    head.appendChild(notice("台账状态与进程是否仍在工作是两回事：本页不探测进程，只显示 process_state、exit_confirmed、pgid 与最后观测时间。" +
      "没有新日志不等于卡死；worker completed 不等于任务 accepted。"));
    if (data.ledger_error) {
      head.appendChild(notice("台账无法读取：" + data.ledger_error, "error"));
    }
    if (data.summary_error) {
      head.appendChild(notice("status_summary 失败：" + data.summary_error + "（其余区块按原始字段尽量给出）", "error"));
    }
    head.appendChild(kv([
      ["目标", run.goal], ["台账状态", run.status], ["分支", mono(run.branch)], ["工作树（仅显示）", mono(run.worktree)],
      ["指挥", run.commander], ["当前任务", run.current_task], ["已接受任务", summary.accepted_tasks ? summary.accepted_tasks.join("、") || "无" : UNAVAILABLE],
      ["台账版本", data.schema_version], ["创建该 run 的操作记录", run.created_by_operation],
      ["首个事件时间", fmtTime(data.timing && data.timing.first_event_time)],
      ["最后事件时间", fmtTime(data.timing && data.timing.last_event_time)],
      ["事件时间跨度", fmtDuration(data.timing && data.timing.first_event_time, data.timing && data.timing.last_event_time)],
      ["运行错误", run.error], ["不可用区块", data.unavailable && data.unavailable.length ? data.unavailable.join("、") : "无"]
    ]));
    nodes.push(head);

    var budgets = section("预算");
    var b = data.budgets || {};
    var limits = b.limits || {};
    var attempts = summary.budgets && summary.budgets.work_attempts ? summary.budgets.work_attempts : b.attempts;
    budgets.appendChild(kv([
      ["任务尝试（work_attempts）", attempts && Object.keys(attempts).length ? Object.keys(attempts).map(function (key) {
        return key + "=" + attempts[key] + "/" + show(limits.max_attempts); }).join("，") : UNAVAILABLE],
      ["总尝试", show(b.total_attempts) + " / " + show(limits.max_total_attempts)],
      ["模型派发", show(b.agent_dispatches) + " / " + show(limits.max_agent_dispatches)],
      ["再规划", show(b.replan_attempts) + " / " + show(limits.max_replans)],
      ["整体验收", show(b.final_attempts) + " / " + show(limits.max_final_attempts)],
      ["计数策略", b.attempt_budget_policy]
    ]));
    nodes.push(budgets);

    var gates = section("等待与阻塞");
    if (summary.pending_decisions && summary.pending_decisions.length) {
      gates.appendChild(el("h3", null, "等待操作者决定"));
      gates.appendChild(table(["请求", "任务", "类型", "问题", "产物"], summary.pending_decisions.map(function (item) {
        return [item.id, item.task_id, item.kind, item.question, mono(item.artifact)];
      })));
    } else {
      gates.appendChild(notice(data.unavailable && data.unavailable.indexOf("decisions") >= 0 ? "操作者决定：不可用（台账版本过旧）" : "没有等待中的操作者决定。"));
    }
    if (summary.last_environment_blocker) {
      var blocker = summary.last_environment_blocker;
      gates.appendChild(el("h3", null, "环境阻塞" + (blocker.stale ? "（历史）" : "（当前）")));
      gates.appendChild(kv([["任务", blocker.task_id], ["dispatch", mono(blocker.dispatch_id)],
        ["阻塞项", blocker.blockers && blocker.blockers.length ? blocker.blockers.join("；") : UNAVAILABLE],
        ["最新环境更新", blocker.latest_update_id]]));
    } else {
      gates.appendChild(notice(data.unavailable && data.unavailable.indexOf("environment") >= 0 ? "环境阻塞：不可用（台账版本过旧）" : "没有记录的环境阻塞。"));
    }
    if (summary.missing_command_requirements) {
      gates.appendChild(el("h3", null, "缺失命令授权"));
      gates.appendChild(list(Object.keys(summary.missing_command_requirements).map(function (task) {
        return task + "：" + JSON.stringify(summary.missing_command_requirements[task]);
      })));
    }
    if (summary.worker_outcomes) {
      gates.appendChild(el("h3", null, "worker 结果计数（观测值，不等于接受）"));
      gates.appendChild(kv(Object.keys(summary.worker_outcomes).map(function (key) { return [key, summary.worker_outcomes[key]]; })));
    }
    nodes.push(gates);

    var tasks = section("任务（" + data.tasks.length + "）");
    tasks.appendChild(table(["任务", "标题", "依赖", "已接受", "当前"], data.tasks.map(function (task) {
      return [task.id, task.title, task.depends_on.join("、") || "无", task.accepted, run.current_task === task.id ? "是" : ""];
    })));
    if (data.invalid_tasks) { tasks.appendChild(notice("跳过 " + data.invalid_tasks + " 个非对象任务项", "warn")); }
    nodes.push(tasks);

    var cost = data.cost;
    var costs = section("费用与调用分类");
    costs.appendChild(notice("费用以台账 dispatch ID 为唯一累计单位；Claude 费用是 CLI 报告的估算，不是账户账单；token 不跨 dispatch 或供应商相加。"));
    costs.appendChild(providerTable(cost));
    costs.appendChild(kv([["已知未调用", cost.known_not_invoked], ["调用阶段未知", cost.unknown_invocation],
      ["启动未确认（starting）", cost.startup_unconfirmed], ["计入 dispatch 数", cost.counted_dispatches],
      ["重复 dispatch ID", cost.duplicate_dispatch_ids.length ? cost.duplicate_dispatch_ids.join("、") : "无"]]));
    costs.appendChild(el("h3", null, "按组"));
    var groups = [groupRow("协调（指挥）", cost.by_group.coordination)];
    Object.keys(cost.by_group.tasks).sort().forEach(function (task) {
      groups.push(groupRow("任务 " + (task || "（无 task_id）"), cost.by_group.tasks[task]));
    });
    costs.appendChild(table(["组", "dispatch", "已调用", "有用量", "无用量", "已知未调用", "未知", "Claude 费用", "Codex 费用", "未知供应商费用"], groups));
    costs.appendChild(notice("验证进程：" + cost.by_group.verification.dispatches + " 次，不计入模型费用。"));
    nodes.push(costs);

    var dispatches = section("dispatch（" + data.dispatches.length + "）");
    dispatches.appendChild(notice(data.notes.invoked));
    dispatches.appendChild(table(["dispatch", "角色", "类型", "任务", "状态", "进程状态", "invoked", "调用时间", "结束事件时间", "pgid", "退出已确认",
      "调用分类", "供应商", "模型", "用量（原字段）", "错误", "文件"],
      data.dispatches.map(function (row) {
        var files = el("span");
        row.files.forEach(function (name, index) {
          if (index) { files.appendChild(document.createTextNode(" ")); }
          files.appendChild(link(name, "#/runs/" + encodeURIComponent(data.id) + "/files/dispatches/" + row.id + "/" + name));
        });
        var klass = el("span", null, row.invocation_class + (row.startup_unconfirmed ? "（启动未确认）" : ""));
        if (row.invocation_reason) { klass.title = row.invocation_reason; klass.className = "warn"; }
        var idNode = el("span", "mono", row.id);
        idNode.title = row.id;
        return [idNode, row.role, row.kind, row.task_id, row.status, row.process_state, row.invoked, fmtTime(row.invoked_at),
          fmtTime(row.finished_at), row.pgid, row.exit_confirmed, klass, row.provider, row.model_id, usageText(row.provider_usage),
          row.error, files];
      })));
    if (data.invalid_dispatches) { dispatches.appendChild(notice("跳过 " + data.invalid_dispatches + " 个没有字符串 ID 的 dispatch 项", "warn")); }
    nodes.push(dispatches);

    var ops = section("操作记录（" + data.operations.records.length + "）");
    if (data.operations.records.length) {
      ops.appendChild(table(["操作", "类型", "状态", "开始", "结束", "退出码", "结果", "错误", "Harness 修订", "文件"],
        data.operations.records.map(function (op) {
          return [op.operation_id, op.kind, op.status, fmtTime(op.started_at), fmtTime(op.finished_at), op.exit_status,
            op.result_status, op.error, op.harness && op.harness.revision, mono(op.file)];
        })));
    } else {
      ops.appendChild(notice("没有关联到本 run 的操作记录。"));
    }
    if (data.operations.invalid || data.operations.truncated) {
      ops.appendChild(notice("扫描 " + data.operations.scanned + " 个文件，非法 " + data.operations.invalid + " 个" +
        (data.operations.truncated ? "，已达扫描上限" : ""), "warn"));
    }
    nodes.push(ops);

    var events = section("事件尾部（" + data.events.records.length + " 条）");
    if (!data.events.exists) {
      events.appendChild(notice("events.jsonl 不存在。"));
    } else {
      events.appendChild(notice("读取尾部 " + fmtBytes(data.events.tail_bytes) + "，文件 " + fmtBytes(data.events.size) +
        (data.events.truncated ? "，已截断" : "") + (data.events.unparsed_tail ? "；最后一行未完成或无法解析（运行中属正常）" : "") +
        (data.events.invalid_lines ? "；无法解析 " + data.events.invalid_lines + " 行" : "")));
      events.appendChild(table(["时间", "事件", "任务", "dispatch", "其他字段"], data.events.records.map(function (record) {
        var extra = {};
        Object.keys(record).forEach(function (key) {
          if (["time", "event", "run_id", "task_id", "dispatch_id"].indexOf(key) < 0) { extra[key] = record[key]; }
        });
        var extraText = JSON.stringify(extra);
        return [fmtTime(record.time), record.event, record.task_id, mono(shortId(record.dispatch_id)),
          extraText.length > 300 ? extraText.slice(0, 300) + "…" : extraText];
      })));
      events.appendChild(link("查看 events.jsonl 全文（有界）", "#/runs/" + encodeURIComponent(data.id) + "/files/events.jsonl"));
    }
    nodes.push(events);

    var files = section("文件索引");
    var holder = el("div");
    holder.appendChild(notice("载入中…"));
    files.appendChild(holder);
    nodes.push(files);
    replaceMain(nodes);
    getJSON("/api/runs/" + encodeURIComponent(data.id) + "/files").then(function (index) {
      while (holder.firstChild) { holder.removeChild(holder.firstChild); }
      holder.appendChild(notice("共 " + index.files.length + " 个可读文件" + (index.truncated ? "（已达 " + index.limit + " 上限）" : "") +
        "；跳过符号链接 " + index.skipped.symlink + "、排除 " + index.skipped.excluded + "、不可寻址 " + index.skipped.unaddressable + "、其他 " + index.skipped.other));
      holder.appendChild(table(["路径", "大小", "修改时间"], index.files.map(function (file) {
        return [link(file.path, "#/runs/" + encodeURIComponent(data.id) + "/files/" + file.path), fmtBytes(file.size), fmtTime(file.mtime)];
      })));
    }).catch(function (error) {
      while (holder.firstChild) { holder.removeChild(holder.firstChild); }
      holder.appendChild(notice("文件索引读取失败：" + error.message, "error"));
    });
  }

  function renderFile(current, data) {
    sampledLabel.textContent = "采样时间 " + fmtTime(data.sampled_at);
    var nodes = [];
    var head = section("文件 " + data.source);
    head.appendChild(link("返回 run " + current.runId, "#/runs/" + encodeURIComponent(current.runId)));
    head.appendChild(kv([["来源", mono(data.source)], ["大小", fmtBytes(data.size)], ["读取尾部", fmtBytes(data.tail_bytes)],
      ["已截断", data.truncated], ["读取期间文件变化", data.changed_while_reading], ["采样时间", fmtTime(data.sampled_at)]]));
    var controls = el("div", "row");
    var label = el("label", null, "尾部字节数 ");
    var input = el("input");
    input.type = "number";
    input.min = "1";
    input.max = String(MAX_TAIL);
    input.value = String(current.tail);
    label.appendChild(input);
    controls.appendChild(label);
    var apply = el("button", null, "重新读取");
    apply.type = "button";
    apply.addEventListener("click", function () {
      var value = parseInt(input.value, 10);
      if (!isFinite(value) || value < 1 || value > MAX_TAIL) { value = DEFAULT_TAIL; }
      location.hash = "#/runs/" + encodeURIComponent(current.runId) + "/files/" + current.path + "?tail=" + value;
    });
    controls.appendChild(apply);
    head.appendChild(controls);
    nodes.push(head);
    var body = section("内容（text/plain，按 UTF-8 解码，无法解码的字节已替换）");
    var pre = el("pre", "log");
    pre.textContent = data.text;
    body.appendChild(pre);
    nodes.push(body);
    replaceMain(nodes);
  }

  document.getElementById("refresh").addEventListener("click", render);
  document.getElementById("auto").addEventListener("change", function (event) {
    if (timer) { clearInterval(timer); timer = null; }
    if (event.target.checked) { timer = setInterval(render, 10000); }
  });
  window.addEventListener("hashchange", render);
  render();
})();
"""
