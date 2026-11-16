"""Recording `claude` and `codex` stand-ins for every test that could reach a provider CLI by PATH.

``install(case, claude="ok", codex="ok", state=None)`` writes both stubs into a new
temporary directory, puts it first on PATH for the test (subprocesses inherit it), checks
that both names resolve to the stubs before anything starts, and returns a ``Calls``
reader of the call log. The quota probe argv and ``--version`` are expected; any other
argv is logged as unexpected and exits 97, so a model call that reached a stub fails at
once and leaves a trace. Every number below is invented; only field names, nesting and
value types follow the CLIs.

``QUOTA_STUB_WINDOWS`` (JSON rows ``{kind, percent, model?, reset_in? | resets_at?}``) replaces
Claude's windows, a reset_in counted from ``QUOTA_STUB_NOW`` (a test's fake clock publishes it).
Modes (the environment variables ``QUOTA_STUB_CLAUDE`` and ``QUOTA_STUB_CODEX``) shape one
probe's behaviour; ``…_after:N`` modes apply only once more than N probes of that CLI were
logged. Stubs signal nobody but their own parent (the probing test process). A descendant
that ignores SIGTERM writes a ready marker after installing SIG_IGN and the stub waits for
it, so cleanup is tested against a child that really ignores the signal.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

STUB_PREFIX = "harness-quota-stub-"

COMMON = r'''
import json, os, signal, subprocess, sys, time
LOG = os.environ["QUOTA_STUB_LOG"]
def record(**fields):
    with open(LOG, "a") as handle:
        handle.write(json.dumps(fields) + "\n")
def probes(cli):
    with open(LOG) as handle:
        return sum(1 for line in handle if json.loads(line).get("cli") == cli and json.loads(line).get("probe"))
def after(mode, prefix, cli):
    return mode.startswith(prefix) and probes(cli) > int(mode.split(":")[1])
def stubborn_child(cli):
    ready = os.path.join(os.getcwd(), "child-ready")
    child = subprocess.Popen([sys.executable, "-c",
        "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').close(); time.sleep(60)", ready, "harness-quota-stub-child"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    record(cli=cli + "-child", probe=False, pid=child.pid)
    until = time.monotonic() + 10
    while not os.path.exists(ready) and time.monotonic() < until:
        time.sleep(0.01)
def interrupt_parent_on_term():
    # The cleanup's SIGTERM makes this stub send the user's Ctrl-C to its parent; it then runs until SIGKILL.
    signal.signal(signal.SIGTERM, lambda *_: os.kill(os.getppid(), signal.SIGINT))
def close_stdout():
    os.close(1)
    sys.stdout = open(os.devnull, "w")
'''

CLAUDE = COMMON + r'''
mode = os.environ.get("QUOTA_STUB_CLAUDE", "ok")
argv = sys.argv[1:]
if argv == ["--version"]:
    record(cli="claude", probe=False, version=True, argv=argv, pid=os.getpid())
    print("9.9.901 (Claude Code, synthetic)")
    sys.exit(0)
probe = argv == ["-p", "/usage", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
                 "--setting-sources", "", "--strict-mcp-config", "--max-turns", "1"]
record(cli="claude", probe=probe, argv=argv, pid=os.getpid(), cwd=os.getcwd(),
       auto_memory=os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"),
       retry_watchdog=os.environ.get("CLAUDE_CODE_RETRY_WATCHDOG"))
if not probe:
    sys.exit(97)
state = os.environ.get("QUOTA_STUB_STATE")
if state:
    import glob
    seen = []
    for path in glob.glob(os.path.join(state, "runs", "*", "state.json")):
        with open(path) as handle:
            ledger = json.load(handle)
        seen.append({"status": ledger.get("status"), "stop": ledger.get("stop"),
                     "store_revision": ledger.get("store_revision"), "quota_wait": "quota_wait" in ledger,
                     "dispatches": [{"role": entry.get("role"), "status": entry.get("status"),
                                     "consumed": entry.get("worker_receipt_consumed")}
                                    for entry in ledger.get("dispatches", [])]})
    record(cli="claude-state", probe=False, state=seen)
if mode == "hang_sigint_on_term":
    interrupt_parent_on_term()
    time.sleep(60)
if mode == "badline_sigint_on_term":
    interrupt_parent_on_term()
    print("not json", flush=True)
    time.sleep(60)
if mode == "sigint_parent" or after(mode, "sigint_after:", "claude"):
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(60)
if mode == "hang" or after(mode, "hang_after:", "claude"):
    time.sleep(60)
if mode == "exit":
    sys.exit(3)
if mode == "big":
    sys.stdout.write("x" * (2 * 1024 * 1024))
    sys.exit(0)
if mode in ("child_ignores_term_leader_exits", "child_ignores_term_leader_waits"):
    stubborn_child("claude")
init = {"type": "system", "subtype": "init", "claude_code_version": "9.9.901", "session_id": "s", "tools": [],
        "mcp_servers": [], "plugins": []}
limits = [
    {"kind": "session", "group": "session", "percent": 37, "resets_at": "2031-01-02T03:00:00.000000+00:00",
     "scope": None, "severity": "sev-1", "is_active": True},
    {"kind": "weekly_all", "group": "weekly", "percent": 61, "resets_at": "2031-01-06T03:00:00.000000+00:00",
     "scope": None, "severity": "sev-2", "is_active": False},
    {"kind": "weekly_scoped", "group": "weekly", "percent": 100, "resets_at": "2031-01-06T03:00:00Z",
     "scope": {"model": {"display_name": "Model Z"}, "surface": None}, "severity": "sev-2", "is_active": False}]
custom = os.environ.get("QUOTA_STUB_WINDOWS")
if custom:
    # The test's own windows: reset_in counts from QUOTA_STUB_NOW (the test's fake clock), resets_at is absolute.
    from datetime import datetime, timezone
    now = float(os.environ.get("QUOTA_STUB_NOW") or time.time())
    limits = []
    for row in json.loads(custom):
        reset = row.get("resets_at")
        if reset is None and row.get("reset_in") is not None:
            reset = datetime.fromtimestamp(now + row["reset_in"], timezone.utc).isoformat()
        limits.append({"kind": row["kind"], "group": "synthetic", "percent": row["percent"], "resets_at": reset,
                       "scope": ({"model": {"display_name": row["model"]}, "surface": None}
                                 if row.get("model") is not None else None),
                       "severity": "sev-1", "is_active": True})
if mode == "shape":
    limits[0]["percent"] = "37"
if mode == "unknown_kind":
    limits.append({"kind": "monthly_new", "group": "monthly", "percent": 5, "resets_at": None,
                   "scope": None, "severity": "sev-9", "is_active": False})
report = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "usage"}]},
          "local_command_run": {"command": "usage", "args": ""},
          "usage_report": {"session": {"total_cost_usd": 0, "total_api_duration_ms": 0},
                           "rate_limits": {"limits": limits, "extra_usage": {
                               "is_enabled": False, "monthly_limit": None, "used_credits": None,
                               "utilization": None, "currency": None}}}}
result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 0, "total_cost_usd": 0,
          "duration_api_ms": 0, "duration_ms": 15, "local_command": "usage", "result": "text"}
if mode == "not_zero_call":
    result["num_turns"] = 1
if mode == "no_local_command":
    del result["local_command"]
if mode == "provider_error":
    result.update(is_error=True, subtype="error_during_execution")
for line in (init, report, result):
    print(json.dumps(line), flush=True)
if after(mode, "sigint_on_term_after:", "claude"):
    interrupt_parent_on_term()
    close_stdout()
    time.sleep(60)
if mode in ("child_ignores_term_leader_waits", "eof_then_sigint"):
    close_stdout()                      # EOF for the reader while the leader stays alive
    if mode == "eof_then_sigint":
        os.kill(os.getppid(), signal.SIGINT)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
'''

CODEX = COMMON + r'''
mode = os.environ.get("QUOTA_STUB_CODEX", "ok")
argv = sys.argv[1:]
if argv == ["--version"]:
    record(cli="codex", probe=False, version=True, argv=argv, pid=os.getpid())
    print("codex-cli 0.900.1 (synthetic)")
    sys.exit(0)
probe = argv == ["app-server"]
record(cli="codex", probe=probe, argv=argv, pid=os.getpid(), cwd=os.getcwd(),
       auto_memory=os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"))
if not probe:
    sys.exit(97)
if mode in ("child_ignores_term", "leader_waits_child_ignores"):
    stubborn_child("codex")
if mode == "eof_then_sigint":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)   # survives SIGTERM, so the interrupt lands in the grace
if mode == "orphan":
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "harness-quota-stub-child"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def snapshot(limit_id, percent, balance):
    return {"limitId": limit_id, "limitName": None,
            "primary": {"usedPercent": percent, "windowDurationMins": 4320, "resetsAt": 1925000000},
            "secondary": None, "credits": {"hasCredits": True, "unlimited": False, "balance": balance},
            "planType": "synthetic", "rateLimitReachedType": None, "spendControlReached": False}
answer = {"rateLimits": snapshot("codex", 73, "41.25"), "ordinaryUsageAllowed": True}
if mode == "full_window":
    answer["rateLimits"]["primary"]["usedPercent"] = 100
if mode == "by_limit_id":
    answer["rateLimitsByLimitId"] = {"codex": snapshot("codex", 73, "41.25"),
                                     "codex_other": snapshot("codex_other", 12, None)}
if mode == "by_limit_id_bad":
    answer["rateLimitsByLimitId"] = {"codex_other": {"limitId": "codex_other", "primary": {"usedPercent": "12"}}}
for line in sys.stdin:
    message = json.loads(line)
    record(cli="codex-rpc", probe=False, method=message.get("method"), id=message.get("id"), keys=sorted(message))
    if message.get("method") == "initialize":
        if mode == "hang":
            time.sleep(60)
        if mode == "sigint_parent":
            os.kill(os.getppid(), signal.SIGINT)
            time.sleep(60)
        print(json.dumps({"id": message["id"], "result": {"userAgent": "codex_cli_rs/0.900.1 (synthetic)",
                                                         "codexHome": "/nonexistent", "platformOs": "x"}}), flush=True)
        print(json.dumps({"method": "account/rateLimits/updated", "params": {}}), flush=True)
        if mode == "server_request":
            print(json.dumps({"id": 0, "method": "item/tool/requestUserInput", "params": {}}), flush=True)
    elif message.get("method") == "account/rateLimits/read":
        if mode == "rpc_error":
            print(json.dumps({"id": message["id"], "error": {"code": -32600, "message": "synthetic"}}), flush=True)
        elif mode == "shape":
            answer["rateLimits"]["primary"]["usedPercent"] = "73"
            print(json.dumps({"id": message["id"], "result": answer}), flush=True)
        elif mode == "server_request_only":
            print(json.dumps({"id": 1, "method": "item/tool/requestUserInput", "params": {}}), flush=True)
        else:
            print(json.dumps({"id": message["id"], "result": answer}), flush=True)
# stdin reached EOF: the client closed its input.
if mode == "eof_then_sigint":
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(60)
if mode in ("ignore_eof", "orphan", "leader_waits_child_ignores", "server_request_only"):
    time.sleep(60)
'''


class Calls:
    """Reads the call log of one install."""

    def __init__(self, log):
        self.log = log

    def all(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def probes(self, cli=None):
        return [call for call in self.all() if call.get("probe") and call["cli"] in ("claude", "codex")
                and (cli is None or call["cli"] == cli)]

    def unexpected(self):
        return [call for call in self.all() if call["cli"] in ("claude", "codex") and not call.get("probe")
                and not call.get("version")]

    def children(self, cli):
        return [call["pid"] for call in self.all() if call["cli"] == cli + "-child"]

    def rpc(self):
        return [(call["method"], call["id"]) for call in self.all() if call["cli"] == "codex-rpc"]

    def states(self):
        return [call["state"] for call in self.all() if call["cli"] == "claude-state"]


def install(case, claude="ok", codex="ok", state=None):
    folder = Path(tempfile.mkdtemp(prefix=STUB_PREFIX))
    case.addCleanup(shutil.rmtree, folder, True)
    for name, body in (("claude", CLAUDE), ("codex", CODEX)):
        path = folder / name
        path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        path.chmod(0o755)
    log = folder / "calls.jsonl"
    environment = patch.dict(os.environ, {"PATH": str(folder) + os.pathsep + os.environ.get("PATH", ""),
                                          "QUOTA_STUB_LOG": str(log), "QUOTA_STUB_CLAUDE": claude,
                                          "QUOTA_STUB_CODEX": codex, "QUOTA_STUB_STATE": str(state or "")})
    environment.start()
    case.addCleanup(environment.stop)
    # Before anything starts: both names resolve to the stubs, never to an installed CLI.
    for name in ("claude", "codex"):
        case.assertEqual(shutil.which(name), str(folder / name))
    calls = Calls(log)
    case.__dict__.setdefault("quota_installs", []).append(calls)   # every install of this test, for leftover checks
    return calls
