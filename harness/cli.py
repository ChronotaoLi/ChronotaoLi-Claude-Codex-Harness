from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from . import dashboard, git, observe, quota_wait, scope as boundary, stops
from .adapters import DemoBackend
from .budgets import attempts_total, work_attempts
from .cleanup import blocked, cleanup_run
from .config import Config
from .convergence import convergence_summary, review_rounds
from .memory import Memory, bind_source_files
from .operations import RUN_ID, OperationRecord, config_identity, digest_assets, harness_identity, shared_paths
from .runtime import Runtime
from .shared import mcp_configs, toml_value
from .util import HarnessError, dump, read_json, write_json
from .versions import read_current_ledger, require_current_ledger, unsupported_ledger


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
    # Echo what this process actually loaded so a wrong package, revision or
    # configuration copy is visible before any run.
    result["harness"] = harness_identity()
    result["config"] = config_identity(config)
    result["asset_digest"], asset_error = digest_assets(config)
    if asset_error is not None:
        result["asset_error"] = asset_error
    result["shared"] = shared_paths(config)
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
    print(f"[harness] {'LIVE SMOKE' if live else 'OFFLINE DEMO'} repo={root}", file=sys.stderr, flush=True)
    result = Runtime(config, root, backend=None if live else DemoBackend(), commander=commander).start(
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


def status_work_attempts(run):
    """Reject damaged accounting before deriving a read-only status summary."""
    diagnostic = "运行台账尝试计数字段无效；使用 status --full 查看原始内容"
    attempts = run.get("attempts", {})
    if (not isinstance(attempts, dict)
            or any(not isinstance(key, str) or type(value) is not int or value < 0
                   for key, value in attempts.items())):
        raise HarnessError(diagnostic)
    dispatches = run.get("dispatches", [])
    if not isinstance(dispatches, list):
        raise HarnessError(diagnostic)
    for entry in dispatches:
        if isinstance(entry, dict) and entry.get("attempt_exemption"):
            task_id = entry.get("task_id")
            if (not isinstance(entry["attempt_exemption"], dict)
                    or not isinstance(task_id, str) or task_id not in attempts):
                raise HarnessError(diagnostic)
    result = work_attempts(run)
    if any(value < 0 for value in result.values()):
        raise HarnessError(diagnostic)
    return result


def status_summary(run):
    if not isinstance(run.get("accepted", {}), dict):
        raise HarnessError("运行台账 accepted 字段无效；使用 status --full 查看原始内容")
    result = {key: run.get(key) for key in ("id", "status", "goal", "branch", "worktree", "current_task")}
    result["accepted_tasks"] = list(run.get("accepted", {}))
    result["budgets"] = {key: run.get(key) for key in ("attempts", "agent_dispatches")}
    result["budgets"]["work_attempts"] = status_work_attempts(run)
    # Every worker invocation, summed from the validated per-task counters.
    result["budgets"]["attempts_total"] = attempts_total(run)
    result["convergence"] = convergence_summary(run, result["budgets"]["work_attempts"])
    result["review_rounds"] = review_rounds(run)
    from .diagnostics import budget_explanation, remaining_model_calls
    result["budget_explanation"] = budget_explanation(run, result["budgets"]["work_attempts"])
    remaining = remaining_model_calls(run)
    if remaining is not None:
        result["remaining_model_calls"] = remaining
    dispatches = run.get("dispatches", [])
    dispatches = [entry for entry in dispatches if isinstance(entry, dict)] if isinstance(dispatches, list) else []
    outcomes = dict.fromkeys(("completed", "task_blocked", "environment_blocked", "operator_blocked",
                              "continuation_blocked", "invalid_output", "runtime_failed", "unknown"), 0)
    for entry in dispatches:
        if entry.get("role") == "worker":
            outcome = entry.get("worker_outcome")
            key = outcome if isinstance(outcome, str) and outcome in outcomes else (
                "invalid_output" if entry.get("status") == "invalid" else
                "runtime_failed" if entry.get("status") == "failed" else "unknown")
            outcomes[key] += 1
    result["worker_outcomes"] = outcomes
    # Read-only bindings for the explicit recovery input; this projection does
    # not assert evidence integrity or substitute for resume's source checks.
    continuation_history = run.get("continuation_history", [])
    pending = run.get("pending_continuation")
    if isinstance(continuation_history, list) and pending is not None:
        source = next((row for row in continuation_history if isinstance(row, dict) and row.get("id") == pending), None)
        if source and isinstance(source.get("source"), dict):
            result["continuation"] = {
                "request_id": source["id"], "task_id": source.get("task_id"),
                "source_dispatch_id": source["source"].get("dispatch_id"),
                "source_snapshot_digest": source.get("snapshot_digest"),
                "request": source.get("request"), "state": "awaiting_requirements"}
    directive = run.get("pending_directive")
    if isinstance(directive, dict) and directive.get("continuation_rejection_id"):
        result["continuation"] = {"state": "revision_pending", "task_id": run.get("current_task"),
                                  "rejection_id": directive["continuation_rejection_id"]}
    command_report = run.get("command_requirements", {})
    if not isinstance(command_report, dict) or not isinstance(command_report.get("missing", {}), dict):
        raise HarnessError("运行台账 command_requirements 诊断无效；使用 status --full 查看原始内容")
    missing = {tid: argv for tid, argv in command_report.get("missing", {}).items()
               if tid not in run.get("accepted", {})}
    if missing:
        result["missing_command_requirements"] = missing
    feedback = run.get("feedback")
    if isinstance(feedback, dict):
        worker = feedback.get("worker")
        if isinstance(worker, dict) and worker.get("status") == "blocked" and worker.get("blocker_kind") == "environment":
            artifact = feedback.get("worker_artifact")
            artifact = artifact if isinstance(artifact, str) else None
            source_id = Path(artifact).parent.name if artifact else None
            task_id = feedback.get("task_id")
            source_index = next((i for i, entry in enumerate(dispatches)
                                 if source_id is not None and entry.get("id") == source_id
                                 and entry.get("role") == "worker"
                                 and entry.get("task_id", task_id) == task_id), None)
            stale = (not isinstance(task_id, str) or task_id != run.get("current_task")
                     or task_id in run.get("accepted", {}) or source_index is None
                     or any(entry.get("role") in ("commander", "worker", "reviewer") and entry.get("invoked") is not False
                            for entry in dispatches[source_index + 1:]))
            blockers = worker.get("blockers")
            summary = {"task_id": task_id if isinstance(task_id, str) else None,
                       "dispatch_id": source_id, "source_artifact": artifact,
                       "source_digest": feedback.get("worker_artifact_digest") if isinstance(feedback.get("worker_artifact_digest"), str) else None,
                       "blockers": [value for value in blockers if isinstance(value, str)] if isinstance(blockers, list) else [],
                       "stale": stale}
            attempt = feedback.get("attempt")
            if type(attempt) is int and attempt > 0:
                summary["attempt"] = attempt
            updates = run.get("environment_updates", [])
            updates = [entry for entry in updates if isinstance(entry, dict) and entry.get("task_id") == task_id
                       and entry.get("blocker_dispatch_id") == source_id] if isinstance(updates, list) else []
            if updates:
                latest = updates[-1]
                if isinstance(latest.get("id"), str):
                    summary["latest_update_id"] = latest["id"]
                if isinstance(latest.get("sequence"), int) and not isinstance(latest["sequence"], bool):
                    summary["latest_update_version"] = latest["sequence"]
            result["last_environment_blocker"] = summary
    stop = stops.project_ledger(run)
    if stop is not None:
        result["stop"] = stop
    waiting = quota_wait.project(run)   # an observation: it does not prove the process is alive
    if waiting is not None:
        result["quota_wait"] = waiting
    declared = run.get("scope")
    if isinstance(declared, list):
        # Read-only projections: they authorize nothing and take no part in recovery.
        result["scope"] = {"entries": list(declared), "declared": bool(declared)}
        if not declared:
            result["scope"]["note"] = "未声明：首版计划不受边界约束，修订只能复用已发布计划的模式"
        held = boundary.held_view(run)
        if held is not None:
            result["scope"]["held"] = held
        if run.get("status") == "awaiting_approval":
            result["scope_summary"] = boundary.summary(run)
    responses = run.get("operator_decisions", [])
    requests = run.get("decision_requests", [])
    if not isinstance(responses, list) or not isinstance(requests, list):
        raise HarnessError("操作者决策台账格式无效")
    latest = {r.get("request_id"): r for r in responses if isinstance(r, dict)}
    pending = []
    for request in requests:
        if not isinstance(request, dict):
            raise HarnessError("操作者决策请求格式无效")
        response = latest.get(request.get("id"))
        if request.get("task_id") in run.get("accepted", {}) or (response and response.get("decision") == "continue"):
            continue
        pending.append({key: request.get(key) for key in ("id", "task_id", "kind", "question", "artifact", "artifact_digest")})
    if pending:
        result["pending_decisions"] = pending
    return result


def show_status(args, state):
    if args.run_id:
        if not args.run_id.startswith("run_") or not args.run_id[4:].isalnum():
            raise HarnessError("无效 run ID", stop=stops.make("invalid_input", "cli_arguments", data={"option": "run_id"}))
        ledger = read_current_ledger(state / "runs" / args.run_id / "state.json")
        if args.full:
            print(dump(ledger))
            return
        summary = status_summary(ledger)
        # Read after the ledger's version gate; saved samples only, never a CLI.
        summary["quota"] = observe.quota_status(state, args.run_id)
        print(dump(summary))
        return
    # The read-only observation layer owns the list; stdout fields are unchanged.
    try:
        listing = observe.list_runs(state)
    except observe.ObserveError as error:
        raise HarnessError(f"无法读取运行目录 {state / 'runs'}：{error}") from error
    for item in listing["unsupported"]:
        print(f"harness: 运行目录 {state / item['path']}：{item['reason']}", file=sys.stderr)
    for item in listing["skipped"]:
        print(f"harness: 跳过无法读取的运行台账 {state / item['path']}：{item['reason']}", file=sys.stderr)
    for path in listing["incomplete"]:
        print(f"harness: 运行台账字段不完整 {state / path}", file=sys.stderr)
    print(dump([{key: item[key] for key in ("id", "status", "goal", "branch")} for item in listing["runs"]]))


def manage_memory(args, repo, state):
    path = state / "memory.sqlite3"
    adding = args.memory_command == "add"
    if args.memory_command == "search":
        Memory.validate_search(args.query, key=getattr(args, "key", None), level=args.level)
    if adding:
        source_binding = bind_source_files(repo, getattr(args, "source_file", []))
        run_id = getattr(args, "run", None)
        if run_id:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
                raise HarnessError("无效运行 ID")
            source_status, _ = Memory(path, repo=repo, read_only=True)._source_ledger(run_id)
            if source_status in ("missing", "invalid", "unsupported"):
                raise HarnessError("记忆来源运行缺失、无效、不属于当前项目或台账版本不支持")
        git.ignore_state(repo)
    elif not path.exists():
        if args.memory_command == "read":
            raise HarnessError(f"没有此记忆：{args.id}")
        print(dump([]))
        return
    memory = Memory(path, repo=repo, read_only=not adding)
    if adding:
        print(memory.remember(key=args.key, abstract=args.title, overview=args.text, detail=args.text,
                              source=args.source, ttl_days=args.ttl_days,
                              provenance={"scope": "run" if run_id else "project",
                                          "reference_only": True, **({"run_id": run_id} if run_id else {}),
                                          **source_binding}))
    elif args.memory_command == "search":
        print(dump(memory.search(args.query, key=args.key, level=args.level, run_id=args.run)))
    elif args.memory_command == "history":
        print(dump(memory.history(args.key)))
    elif args.memory_command == "read":
        print(dump(memory.read(args.id)))
    else:
        print(dump(memory.export()))


def exit_status(error):
    """The exit code for an exception: SystemExit keeps its code; every other one comes from its stop."""
    if isinstance(error, SystemExit):
        return error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
    return stops.exit_code(error)


def reported(error):
    """Exceptions the CLI reports as a stop; anything else propagates as a programming error."""
    if isinstance(error, (sqlite3.ProgrammingError, sqlite3.InterfaceError)):
        return False
    return isinstance(error, (HarnessError, OSError, ValueError, subprocess.TimeoutExpired, sqlite3.Error,
                              KeyboardInterrupt))


def stop_notice(value, run_id, operation_id):
    """The machine-readable stderr line; the same projection as status and the operation record."""
    body = {"event": "stop", "run_id": run_id, "operation_id": operation_id,
            "stop": {key: item for key, item in value.items() if key != "detail"}}
    try:
        print("[harness-json] " + json.dumps(body, ensure_ascii=True, separators=(",", ":")),
              file=sys.stderr, flush=True)
    except Exception:
        pass  # Observation output never changes the exit status or the operation record.


def report_stop(error, run_id=None, operation_id=None, value=None):
    """stderr: the full text as before, then the projection; returns the projection."""
    value = value if value is not None else stops.project(stops.of(error), run_id)
    if isinstance(error, KeyboardInterrupt):
        text = "已中断；运行状态和 worktree 已保留"
    elif isinstance(error, sqlite3.Error):
        text = f"SQLite 记忆数据库错误：{error}"
    else:
        text = str(error)
    try:
        print(f"harness: {text}", file=sys.stderr, flush=True)
    except Exception:
        pass
    stop_notice(value, run_id, operation_id)
    return value


def operation_arguments(args):
    """Invocation arguments for the record: file options are recorded as path text only."""
    if args.command == "run":
        return {"goal": args.goal, "plan": str(args.plan) if args.plan is not None else None,
                "commander": args.commander, "pause_after_plan": args.pause_after_plan, "step": args.step,
                "scope": list(args.scope or ())}
    return {"run_id": args.run_id, "approve_plan": args.approve_plan, "step": args.step,
            "reverify": args.reverify,
            "confirm_exited": list(args.confirm_exited or ()),
            "environment_update": str(args.environment_update) if args.environment_update is not None else None,
            "operator_decision": [str(path) for path in args.operator_decision or ()],
            "execution_guidance": str(args.execution_guidance) if getattr(args, "execution_guidance", None) is not None else None,
            "reject_request": str(args.reject_request) if getattr(args, "reject_request", None) is not None else None,
            "scope": list(args.scope or ())}


def resume_options(args):
    options = {}
    if args.confirm_exited:
        options["confirm_exited"] = args.confirm_exited
    if args.environment_update is not None:
        options["environment_update"] = args.environment_update
    if args.operator_decision is not None:
        options["operator_decision"] = (args.operator_decision[0] if len(args.operator_decision) == 1
                                        else args.operator_decision)
    if args.scope:
        options["scope"] = args.scope
    if getattr(args, "execution_guidance", None) is not None:
        options["execution_guidance"] = args.execution_guidance
    if getattr(args, "reject_request", None) is not None:
        options["reject_request"] = args.reject_request
    return options


def file_inputs_present(args):
    if args.command == "run":
        return args.plan is not None
    return (args.environment_update is not None or bool(args.operator_decision)
            or getattr(args, "execution_guidance", None) is not None
            or getattr(args, "reject_request", None) is not None)


def persisted_run_id(args, engine, state):
    """Link only a run whose ledger file exists; never guess from directory listings."""
    if args.command == "run":
        run = getattr(engine, "run", None)
        ident = run.get("id") if isinstance(run, dict) else None
    else:
        ident = args.run_id
    try:
        if isinstance(ident, str) and RUN_ID.fullmatch(ident) and (state / "runs" / ident / "state.json").is_file():
            return ident
    except OSError:
        pass
    return None


def operate(args, config, repo, state):
    """Run or resume under an exclusive operation record; the record never alters the exit code."""
    if args.command == "run":
        print(f"[harness] config={config.path}", file=sys.stderr)
        print(f"[harness] verification={dump(config.data['verification']['commands'])}", file=sys.stderr)
    operation = OperationRecord.create(state, kind=args.command, arguments=operation_arguments(args),
                                       config=config, repo=repo, operation_id=args.operation_id,
                                       file_inputs=file_inputs_present(args))
    engine = None
    # From here every failure, including diagnostic output, finishes the record.
    try:
        print(f"[harness] operation_id={operation.id} record={operation.path}", file=sys.stderr)
        if args.command == "run":
            engine = Runtime(config, repo, commander=args.commander, operation=operation)
            try:
                supplied = read_json(args.plan) if args.plan else None
            except (OSError, ValueError) as error:
                raise HarnessError(f"计划文件无法读取或不是有效 JSON：{error}",
                                   stop=stops.make("invalid_input", "input_schema", data={"option": "--plan"})) from error
            result = engine.start(args.goal, supplied, scope=args.scope or (),
                                  pause_after_plan=args.pause_after_plan, step=args.step)
            keys = ("id", "status", "branch", "worktree", "accepted")
        else:
            engine = Runtime(config, repo, operation=operation)
            result = engine.resume(args.run_id, approve_plan=args.approve_plan, step=args.step,
                                   reverify=args.reverify, **resume_options(args))
            keys = ("id", "status", "branch", "worktree")
        print(dump({**{key: result[key] for key in keys}, "operation_id": operation.id}))
    except BaseException as error:
        run_id = persisted_run_id(args, engine, state)
        code = exit_status(error)
        # Every failure is recorded with its projection; only the reported ones
        # also get the stderr diagnostic and return, the rest propagate as before.
        value = stops.project(stops.of(error), run_id)
        if reported(error):
            report_stop(error, run_id, operation.id, value)
        operation.finish(exit_status=code, error=error, stop=value, run_id=run_id)
        if not reported(error):
            raise
        return code
    saved = None
    if isinstance(result, dict):
        try:
            saved = stops.project_ledger(result)
        except HarnessError:
            saved = None
        if saved is not None:
            stop_notice(saved, result.get("id"), operation.id)
    operation.finish(exit_status=0, result_status=result.get("status") if isinstance(result, dict) else None,
                     stop=saved, run_id=persisted_run_id(args, engine, state))
    return 0


def _command(group, name, text):
    """A subcommand whose entry in the list and whose own help page carry the same sentence."""
    return group.add_parser(name, help=text, description=text)


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Personal Harness — fresh task agents, independent review")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "harness.toml"),
                        help="Harness 配置文件；默认是源码目录下的 harness.toml")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="目标项目的 Git 仓库；默认当前目录")
    sub = parser.add_subparsers(dest="command", required=True)
    commander = "指挥：astra 或 fable，用配置里同名角色的模型；默认 astra"
    run_id = "run ID（status 列出的 run_…）"
    _command(sub, "doctor", "检查工具和登录状态；不调用模型")
    _command(sub, "demo", "离线模拟模型，运行真实 Git 与测试流程")
    smoke = _command(sub, "smoke", "在临时仓库调用真实模型验证最小工作流，会消耗账号额度")
    smoke.add_argument("--commander", choices=["astra", "fable"], default="astra", help=commander)
    run = _command(sub, "run", "调用真实模型，在独立分支执行目标")
    run.add_argument("goal", help="要完成的目标，一段文字")
    run.add_argument("--commander", choices=["astra", "fable"], default="astra", help=commander)
    run.add_argument("--plan", type=Path, help="使用已编写的 JSON 任务计划")
    run.add_argument("--pause-after-plan", action="store_true", help="保存计划并等待显式批准，再开始执行")
    run.add_argument("--step", action="store_true", help="最多接受一个任务后暂停；最后一个任务仍需整体验收")
    run.add_argument("--scope", action="append", metavar="PATTERN",
                     help="声明 run 的范围边界：具体路径、字面子树 p/…/** 或 **；可重复；首版与之后的修订都受它约束")
    run.add_argument("--operation-id", metavar="ID", help="本次操作记录的 ID；默认自动生成，已存在则拒绝")
    resume = _command(sub, "resume", "从停止、中断、暂停或待批准处继续 run：保存的现场与全新 agent，不恢复供应商会话，"
                                     "不清零累计预算")
    resume.add_argument("run_id", help=run_id)
    resume.add_argument("--operation-id", metavar="ID", help="本次操作记录的 ID；默认自动生成，已存在则拒绝")
    resume.add_argument("--approve-plan", action="store_true", help="批准等待确认的计划并继续")
    resume.add_argument("--step", action="store_true", help="最多接受一个任务后暂停")
    resume.add_argument("--reverify", action="store_true", help="仅在本次恢复首次整体验收时重验失败快照；不重抽审阅结论；已返回的指挥决定存在时拒绝")
    resume.add_argument("--confirm-exited", action="append", metavar="DISPATCH_ID",
                        help="确认指定旧 dispatch 已退出并记录证据；可重复指定")
    resume.add_argument("--environment-update", type=Path, metavar="FILE",
                        help="提供操作者环境澄清 JSON 文件；不扩展权限、文件范围或命令授权")
    resume.add_argument("--execution-guidance", type=Path, metavar="FILE",
                        help="在停止边界提供绑定任务及候选的执行约束；不授权命令或放松验收")
    resume.add_argument("--reject-request", type=Path, metavar="FILE",
                        help="拒绝已判定合格而未落地的指挥提案，或当前续作请求（{request_id, reason}）；不授权命令、不重置预算")
    resume.add_argument("--operator-decision", type=Path, action="append", metavar="FILE",
                        help="回答 status 中的操作者决策请求；可重复指定，整批校验保存后继续，不跳过验收或授予命令权限")
    resume.add_argument("--scope", action="append", metavar="PATTERN",
                        help="扩大 run 的范围边界（具体路径、字面子树 p/…/** 或 **）；可重复；持有的提案随后重放")
    status = _command(sub, "status", "列出 run，或显示一个 run 的任务、停止与额度观测；只读，不加载配置")
    status.add_argument("run_id", nargs="?", help="要查看的 run；省略时列出全部 run")
    status.add_argument("--full", action="store_true", help="输出完整运行台账")
    watch = _command(sub, "watch", "用原生文件事件观察 run，输出变化的 JSONL；不轮询台账")
    watch.add_argument("run_id", help=run_id)
    watch.add_argument("--follow", action="store_true", help="在阻塞、暂停或终态之后继续观察，直到中断")
    watch.add_argument("--state", type=Path, help="直接观察指定 .harness 目录，可用于归档后的运行证据")
    dash = _command(sub, "dashboard", "在 127.0.0.1 启动只读观察网页；不加载配置，不探测进程，不修改 run")
    dashboard.add_arguments(dash)
    abandon = _command(sub, "abandon", "放弃运行并保留分支、工作树与证据；之后用 cleanup-run 清理")
    abandon.add_argument("run_id", help=run_id)
    abandon.add_argument("--confirm-exited", action="append", metavar="DISPATCH_ID",
                         help="确认指定旧 dispatch 已退出并记录证据；可重复指定")
    retirement = _command(sub, "cleanup-run", "预览 run 保留的全新验证检出与工作树；保留分支、台账与证据，不加载配置")
    retirement.add_argument("run_id", help=run_id)
    retirement.add_argument("--remove", action="store_true",
                            help="逐项移除通过安全检查的资源：验证检出不限 run 状态，工作树仅限终态")
    retirement.add_argument("--remove-input-copies", action="store_true", help="显式删除登记且未修改的依赖副本；保留源文件")
    retirement.add_argument("--remove-ignored", action="store_true",
                            help="任务工作树：预览，或与 --remove 同用时删除 Git 判定为忽略的文件；验证检出的忽略文件不需要此选项")
    retirement.add_argument("--confirm-exited", action="append", metavar="DISPATCH_ID",
                            help="显式确认旧派发及其命令进程已退出；可重复指定")
    prepare = _command(sub, "prepare-run", "在暂停边界按清单准备不可变依赖副本")
    prepare.add_argument("run_id", help=run_id)
    prepare.add_argument("--manifest", type=Path, required=True, help="依赖副本清单 JSON 文件（必填）")
    supervisor = _command(sub, "supervise", "启动一次 Harness run/resume 并记录真实进程退出；不自动重启")
    supervisor.add_argument("--operation-id", metavar="ID", help="交给这次 run/resume 的操作记录 ID；默认自动生成")
    supervisor.add_argument("arguments", nargs=argparse.REMAINDER,
                            help="-- 之后是要启动的 run 或 resume 命令及其参数")
    permission = _command(sub, "permission-diagnostics", "只读比较供应商拒绝证据与本地精确命令能力")
    permission.add_argument("run_id", help=run_id)
    permission.add_argument("dispatch_id", help="要比较的那次调用的 dispatch ID")
    permission.add_argument("--argv-json", help="要比较的完整 argv JSON 数组；不会执行")
    sync = _command(sub, "sync", "生成共用 MCP 的两种客户端配置，不覆盖个人配置")
    sync.add_argument("--role", choices=["commander", "worker", "reviewer"], default="worker",
                      help="按哪个角色生成配置；默认 worker")
    mem = _command(sub, "memory", "项目记忆：添加、检索、读取、查看版本与导出；记忆只作参考，不授予权限")
    msub = mem.add_subparsers(dest="memory_command", required=True)
    add = _command(msub, "add", "保存操作者事实；带 --run 时保存只用于该 run 的参考说明")
    add.add_argument("--key", required=True, help="记忆的 key，在项目内全局唯一；同一 key 再次保存是新版本（必填）")
    add.add_argument("--title", required=True, help="标题，即 L0 摘要（必填）")
    add.add_argument("--text", required=True, help="正文，同时作为 L1 摘要与 L2 详情（必填）")
    add.add_argument("--source", default="operator", help="来源的说明文字，不作核验；默认 operator")
    add.add_argument("--source-file", action="append", default=[],
                     help="绑定一个已提交且工作区原始字节与 Git 一致的仓库文件作为依据；可重复")
    add.add_argument("--ttl-days", type=float, help="有效天数（正数）；默认不过期")
    add.add_argument("--run", help="只用于这个 run 的参考说明；查询时也要带同一个 --run")
    search = _command(msub, "search", "按本地词法检索记忆，或用 --key 原样等值查询")
    search.add_argument("query", nargs="?", help="检索词；用 --key 时可省略")
    search.add_argument("--key", help="按原样 key 等值查询")
    search.add_argument("--level", type=int, choices=[0, 1, 2], default=1,
                        help="返回详略：0 只有标题，1 加摘要，2 加详情；默认 1")
    search.add_argument("--run", help="同时检索只用于这个 run 的参考说明")
    read = _command(msub, "read", "按 ID 读取一条记忆的完整内容")
    read.add_argument("id", help="记忆 ID（search 结果里的 mem_…）")
    history = _command(msub, "history", "列出一个 key 的所有版本、来源状态与替代关系，供审计")
    history.add_argument("key", help="要查看的记忆 key")
    _command(msub, "export", "以 JSON 导出全部记忆，含旧版本")
    args = parser.parse_args(argv)
    if args.command == "status" and args.full and not args.run_id:
        parser.error("status --full 需要 run_id")
    try:
        if args.command in ("prepare-run", "supervise"):
            repo = git.repository(args.repo)
            if args.command == "prepare-run":
                from .workspace_inputs import prepare_run
                print(dump(prepare_run(repo, args.run_id, args.manifest)))
            else:
                from .supervisor import supervise
                arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
                receipt = supervise(repo, arguments, config_path=args.config, operation_id=args.operation_id)
                print(dump(receipt), file=sys.stderr)
                code = receipt.get("exit_code")
                return (128 - code if code < 0 else code) if type(code) is int else 1
            return 0
        if args.command == "watch":
            from .watch_cli import watch_run
            state = args.state if args.state is not None else git.repository(args.repo) / ".harness"
            return watch_run(state, args.run_id, follow=args.follow)
        if args.command in ("status", "dashboard", "memory", "cleanup-run"):
            repo = git.repository(args.repo)
            state = repo / ".harness"
            if args.command == "status":
                show_status(args, state)
            elif args.command == "dashboard":
                # Serves until SIGINT/SIGTERM (main() maps them to KeyboardInterrupt,
                # which serve() absorbs) and then returns 0; stdout is only the URL.
                return dashboard.serve(state, args.port)
            elif args.command == "cleanup-run":
                result = cleanup_run(repo, args.run_id, remove=args.remove,
                                     remove_input_copies=args.remove_input_copies,
                                     remove_ignored=args.remove_ignored,
                                     confirm_exited=args.confirm_exited or ())
                print(dump(result))
                return 1 if blocked(result) else 0
            else:
                manage_memory(args, repo, state)
            return 0
        try:
            config = Config.load(args.config)
        except FileNotFoundError as e:
            raise HarnessError(f"找不到配置文件 {Path(args.config).resolve()}；请通过 --config 指定配置文件，"
                               "默认 harness.toml 和 shared 资源仅随源码仓库提供") from e
        if args.command == "doctor":
            return doctor(config)
        if args.command == "permission-diagnostics":
            from .permission_diagnostics import permission_diagnostics
            if not RUN_ID.fullmatch(args.run_id) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", args.dispatch_id):
                raise HarnessError("无效 run/dispatch ID", stop=stops.make("invalid_input", "cli_arguments", data={"option": "run_id"}))
            state = git.repository(args.repo) / ".harness"
            try:
                with observe.Observer(state, required=True) as observer:
                    run, error = observer._read_ledger(args.run_id)
                    if error is None and unsupported_ledger(run) is not None:
                        require_current_ledger(run)  # the shared typed refusal (exit 2)
                    if error or run.get("id") != args.run_id:
                        raise HarnessError(error or "运行 ID 不符")
                    entry = next((d for d in run.get("dispatches", []) if d.get("id") == args.dispatch_id), None)
                    if entry is None:
                        raise HarnessError("派发未登记")
                    components = ["runs", args.run_id, "dispatches", args.dispatch_id]
                    terminal = None
                    roles = run.get("config_snapshot", {}).get("roles", {})
                    role_key = "fable" if entry.get("role") == "commander" and run.get("commander") == "fable" else entry.get("role")
                    provider = roles.get(role_key, {}).get("cli")
                    if provider == "claude":
                        # Claude's only output is its JSON terminal in process.stdout.
                        try:
                            source = observer.reader.read(components + ["process.stdout"], limit=16*1024*1024)
                            if not source["too_large"] and not source["changed_while_reading"]:
                                terminal = json.loads(source["data"])
                        except (observe.NotFound, ValueError, RecursionError):
                            pass
                    report = permission_diagnostics(config, terminal,
                        argv=json.loads(args.argv_json) if args.argv_json else None)
                    report.update(run_id=args.run_id, dispatch_id=args.dispatch_id,
                                  local_config_basis="current_configuration_not_historical_provider_policy")
                    print(dump(report))
                    return 0
            except observe.ObserveError as error:
                raise HarnessError(str(error)) from error
        if args.command in ("demo", "smoke"):
            demo(config, live=args.command == "smoke", commander=getattr(args, "commander", "astra"))
            return 0
        repo = git.repository(args.repo)
        state = repo / ".harness"
        if args.command in ("run", "resume"):
            return operate(args, config, repo, state)
        if args.command == "abandon":
            options = {"confirm_exited": args.confirm_exited} if args.confirm_exited else {}
            result = Runtime(config, repo).abandon(args.run_id, **options)
            print(dump(status_summary(result)))
        elif args.command == "sync":
            git.ignore_state(repo)
            claude, codex = mcp_configs(config, state, args.role)
            folder = state / "generated" / args.role
            write_json(folder / "claude.mcp.json", claude)
            (folder / "codex.mcp.toml").write_text("mcp_servers = " + toml_value(codex) + "\n", encoding="utf-8")
            print(str(folder))
        return 0
    except (HarnessError, OSError, ValueError, subprocess.TimeoutExpired, sqlite3.Error, KeyboardInterrupt) as e:
        if not reported(e):
            raise
        value = report_stop(e, getattr(args, "run_id", None))
        if args.command == "cleanup-run" and not isinstance(e, KeyboardInterrupt):
            return 1  # cleanup-run reports per-resource results, not the run stop protocol
        return value["exit_code"]
