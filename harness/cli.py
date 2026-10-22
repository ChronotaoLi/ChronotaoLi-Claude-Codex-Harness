from __future__ import annotations

import argparse
import copy
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from . import git
from .adapters import DemoBackend
from .config import Config
from .engine import Engine
from .memory import Memory
from .shared import mcp_configs, toml_value
from .util import HarnessError, dump, read_json, write_json


def doctor(config):
    result = {"python": sys.version.split()[0], "models": config.data["roles"], "checks": {}}
    for cli in sorted({config.data["roles"][name]["cli"] for name in ("commander", "worker", "reviewer", "fable")}):
        if not shutil.which(cli):
            result["checks"][cli] = {"installed": False}
            continue
        version = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=15)
        auth = subprocess.run([cli, "login", "status"] if cli == "codex" else [cli, "auth", "status"],
                              capture_output=True, text=True, timeout=15)
        result["checks"][cli] = {"installed": True, "version": version.stdout.strip(), "logged_in": auth.returncode == 0}
    if shutil.which("orca"):
        result["orca"] = subprocess.run(["orca", "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
    result["note"] = "登录检查不代表指定模型有权限；实际运行不会自动换模型。"
    print(dump(result))
    return 0 if all(c.get("logged_in") for c in result["checks"].values()) else 1


def demo(config, live=False, commander="astra"):
    root = Path(tempfile.mkdtemp(prefix="personal-harness-smoke-" if live else "personal-harness-demo-"))
    (root / ".gitignore").write_text(".harness/\n__pycache__/\n", encoding="utf-8")
    (root / "test_hello.py").write_text('import unittest\nfrom hello import greet\n\nclass TestGreet(unittest.TestCase):\n    def test_greet(self):\n        self.assertEqual(greet("Ada"), "Hello, Ada!")\n', encoding="utf-8")
    git.git(root, "init", "-b", "main")
    git.git(root, "add", ".")
    git.git(root, "-c", "user.name=Harness Demo", "-c", "user.email=demo@localhost", "-c", "commit.gpgsign=false", "commit", "-m", "Demo fixture")
    config = copy.deepcopy(config)
    config.data["verification"]["commands"] = [[sys.executable, "-m", "unittest", "-v"]]
    config.data["runtime"]["agent_timeout_seconds"] = 180 if live else 1200
    config.data["runtime"]["max_attempts"] = 1 if live else 3
    config.data["runtime"]["max_final_attempts"] = 1 if live else 3
    print(f"[harness] {'LIVE SMOKE' if live else 'OFFLINE DEMO'} repo={root}", flush=True)
    result = Engine(config, root, backend=None if live else DemoBackend(), commander=commander).start(
        "Implement greet(name) in hello.py, returning 'Hello, {name}!'. Keep the existing test unchanged.")
    print(dump({"simulation": not live, "models_called": live, "verification": "real unittest + git", "repo": str(root),
                "run": result["id"], "branch": result["branch"], "status": result["status"]}))


def main(argv=None):
    def interrupted(signum, frame):
        raise KeyboardInterrupt()

    previous = {}
    try:
        for name in ("SIGTERM", "SIGHUP"):
            if signum := getattr(signal, name, None):
                previous[signum] = signal.signal(signum, interrupted)
        return _main(argv)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def read_ledger(path):
    value = read_json(path)
    if not isinstance(value, dict):
        raise HarnessError(f"运行台账必须为对象：{path}")
    return value


def status_summary(run):
    if not isinstance(run.get("accepted", {}), dict) or not isinstance(run.get("replan_stopped", {}), dict):
        raise HarnessError("运行台账 accepted 或 replan_stopped 字段无效；使用 status --full 查看原始内容")
    result = {key: run.get(key) for key in ("id", "status", "goal", "branch", "worktree", "current_task")}
    result["accepted_tasks"] = list(run.get("accepted", {}))
    result["budgets"] = {key: run.get(key) for key in
                         ("attempts", "total_attempts", "agent_dispatches", "replan_attempts", "final_attempts")}
    if run.get("error"):
        result["error"] = str(run["error"]).splitlines()[0]
    if run.get("replan_stopped"):
        result["replan_stopped"] = {key: run["replan_stopped"].get(key) for key in ("target", "reason", "artifact")}
    return result


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Personal Harness — fresh task agents, independent review")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "harness.toml"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="检查工具和登录状态；不调用模型")
    sub.add_parser("demo", help="离线模拟模型，运行真实 Git 与测试流程")
    smoke = sub.add_parser("smoke", help="在临时仓库调用真实模型验证最小工作流，会消耗账号额度")
    smoke.add_argument("--commander", choices=["astra", "fable"], default="astra")
    run = sub.add_parser("run", help="调用真实模型，在独立分支执行目标")
    run.add_argument("goal")
    run.add_argument("--commander", choices=["astra", "fable"], default="astra")
    run.add_argument("--plan", type=Path, help="使用已编写的 JSON 任务计划")
    run.add_argument("--pause-after-plan", action="store_true", help="保存计划并等待显式批准，再开始执行")
    run.add_argument("--step", action="store_true", help="最多接受一个任务后暂停；最后一个任务仍需整体验收")
    resume = sub.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--approve-plan", action="store_true", help="批准等待确认的计划并继续")
    resume.add_argument("--step", action="store_true", help="最多接受一个任务后暂停")
    resume.add_argument("--reverify", action="store_true", help="在最终验收预算内重新执行失败的整体验证；不重抽审阅结论")
    resume.add_argument("--retry-stopped", metavar="TASK_ID|final", help="显式重试已被指挥停止的规划请求，仍受累计预算约束")
    status = sub.add_parser("status")
    status.add_argument("run_id", nargs="?")
    status.add_argument("--full", action="store_true", help="输出完整运行台账")
    abandon = sub.add_parser("abandon", help="放弃运行并保留分支与证据")
    abandon.add_argument("run_id")
    abandon.add_argument("--remove-worktree", action="store_true", help="同时移除无未保存文件的隔离工作树；保留分支与运行证据")
    sync = sub.add_parser("sync", help="生成共用 MCP 的两种客户端配置，不覆盖个人配置")
    sync.add_argument("--role", choices=["commander", "worker", "reviewer"], default="worker")
    mem = sub.add_parser("memory")
    msub = mem.add_subparsers(dest="memory_command", required=True)
    add = msub.add_parser("add")
    add.add_argument("--key", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--text", required=True)
    add.add_argument("--source", default="operator")
    add.add_argument("--ttl-days", type=float)
    search = msub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--level", type=int, choices=[0, 1, 2], default=1)
    read = msub.add_parser("read")
    read.add_argument("id")
    msub.add_parser("export")
    args = parser.parse_args(argv)
    if args.command == "status" and args.full and not args.run_id:
        parser.error("status --full 需要 run_id")
    try:
        config = Config.load(args.config)
        if args.command == "doctor":
            return doctor(config)
        if args.command in ("demo", "smoke"):
            demo(config, live=args.command == "smoke", commander=getattr(args, "commander", "astra"))
            return 0
        repo = git.repository(args.repo)
        state = repo / ".harness"
        if args.command == "run":
            print(f"[harness] config={config.path}", file=sys.stderr)
            print(f"[harness] verification={dump(config.data['verification']['commands'])}", file=sys.stderr)
            engine = Engine(config, repo, commander=args.commander)
            result = engine.start(args.goal, read_json(args.plan) if args.plan else None,
                                  pause_after_plan=args.pause_after_plan, step=args.step)
            print(dump({k: result[k] for k in ("id", "status", "branch", "worktree", "accepted")}))
        elif args.command == "resume":
            options = {"retry_stopped": args.retry_stopped} if args.retry_stopped is not None else {}
            result = Engine(config, repo).resume(args.run_id, approve_plan=args.approve_plan, step=args.step,
                                                reverify=args.reverify, **options)
            print(dump({k: result[k] for k in ("id", "status", "branch", "worktree")}))
        elif args.command == "abandon":
            result = Engine(config, repo).abandon(args.run_id, remove_worktree=args.remove_worktree)
            print(dump(status_summary(result)))
        elif args.command == "status":
            if args.run_id:
                if not args.run_id.startswith("run_") or not args.run_id[4:].isalnum():
                    raise HarnessError("无效 run ID")
                ledger = read_ledger(state / "runs" / args.run_id / "state.json")
                print(dump(ledger if args.full else status_summary(ledger)))
            else:
                runs = []
                for directory in sorted((state / "runs").glob("*/")):
                    path = directory / "state.json"
                    try:
                        run = read_ledger(path)
                    except (HarnessError, OSError, ValueError) as e:
                        print(f"harness: 跳过无法读取的运行台账 {path}：{e}", file=sys.stderr)
                        continue
                    fields = ("id", "status", "goal", "branch")
                    if any(key not in run for key in fields):
                        print(f"harness: 运行台账字段不完整 {path}", file=sys.stderr)
                    item = {key: run.get(key) for key in fields}
                    item["id"] = run.get("id", path.parent.name)
                    item["status"] = run.get("status", "unknown")
                    runs.append(item)
                print(dump(runs))
        elif args.command == "sync":
            git.ignore_state(repo)
            claude, codex = mcp_configs(config, state, args.role)
            folder = state / "generated" / args.role
            write_json(folder / "claude.mcp.json", claude)
            (folder / "codex.mcp.toml").write_text("mcp_servers = " + toml_value(codex) + "\n", encoding="utf-8")
            print(str(folder))
        elif args.command == "memory":
            git.ignore_state(repo)
            memory = Memory(state / "memory.sqlite3", repo=repo)
            if args.memory_command == "add":
                print(memory.remember(key=args.key, abstract=args.title, overview=args.text, detail=args.text,
                                      source=args.source, ttl_days=args.ttl_days))
            elif args.memory_command == "search":
                print(dump(memory.search(args.query, level=args.level)))
            elif args.memory_command == "read":
                print(dump(memory.read(args.id)))
            else:
                print(dump(memory.export()))
        return 0
    except (HarnessError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        print(f"harness: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        if isinstance(e, (sqlite3.ProgrammingError, sqlite3.InterfaceError)):
            raise
        print(f"harness: SQLite 记忆数据库错误：{e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("harness: 已中断；运行状态和 worktree 已保留", file=sys.stderr)
        return 130
