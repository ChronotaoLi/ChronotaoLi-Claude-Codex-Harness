"""Opt-in integration probe of actual Claude Code permission decisions.

This is deliberately separate from offline unit tests and the workflow smoke.
It uses one real worker session and keeps its disposable Git fixture and raw
provider evidence under .harness/permission-smoke/.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import uuid
from pathlib import Path

from .adapters import CLIBackend
from .config import Config
from .util import HarnessError, dump, run_process, write_json


def _git(cwd, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    result = subprocess.run(["git", "-C", str(cwd), "-c", "core.hooksPath=" + os.devnull, *args],
                            capture_output=True, text=True, timeout=60, env=env)
    if result.returncode:
        raise HarnessError(f"权限 smoke Git 准备失败：{result.stderr.strip()}")


def prepare_fixture(directory):
    directory.mkdir(parents=True, mode=0o700)
    repo, worktree = directory / "repo", directory / "worktree"
    repo.mkdir()
    original, changed = "permission control before\n", "permission control after\n"
    (repo / "control.txt").write_text(original, encoding="utf-8")
    (repo / "permission_control.py").write_text(
        'from pathlib import Path\nPath("command-control.txt").write_text("command allowed\\n")\n'
        'print("permission command control succeeded")\n', encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Harness Permission Smoke", "-c", "user.email=smoke@localhost",
         "-c", "commit.gpgsign=false", "commit", "-qm", "Permission fixture")
    _git(repo, "worktree", "add", "--detach", str(worktree))
    outside = directory / "outside.txt"
    outside_text = "outside read control " + uuid.uuid4().hex + "\n"
    outside.write_text(outside_text, encoding="utf-8")
    git_file = worktree / ".git"
    git_text = git_file.read_text(encoding="utf-8")
    command = shlex.join([sys.executable, "permission_control.py"])

    def case(name, tool, tool_input, allowed, **extra):
        return {"name": name, "tool": tool, "input": tool_input, "allowed": allowed, **extra}

    cases = [
        case("outside_read", "Read", {"file_path": str(outside)}, True, contains=outside_text.strip()),
        case("inside_read", "Read", {"file_path": str(worktree / "control.txt")}, True),
        case("git_read", "Read", {"file_path": str(git_file)}, True),
        case("inside_edit", "Edit", {"file_path": str(worktree / "control.txt"),
                                    "old_string": original, "new_string": changed}, True),
        case("inside_write", "Write", {"file_path": str(worktree / "write-control.txt"),
                                      "content": "write allowed\n"}, True),
        case("exact_command", "Bash", {"command": command}, True,
             contains="permission command control succeeded"),
        case("extended_command", "Bash", {"command": command + " --ungranted"}, False),
        case("unlisted_command", "Bash", {"command": shlex.join(["touch", str(directory / "bash-outside.txt")])}, False),
        case("outside_edit", "Edit", {"file_path": str(outside), "old_string": outside_text,
                                     "new_string": "outside edit unexpectedly allowed\n"}, False),
        case("outside_write", "Write", {"file_path": str(directory / "write-outside.txt"),
                                       "content": "outside write unexpectedly allowed\n"}, False),
        case("git_edit", "Edit", {"file_path": str(git_file), "old_string": git_text,
                                 "new_string": git_text + "# permission smoke\n"}, False),
    ]
    expected_files = {
        str(worktree / "control.txt"): changed,
        str(worktree / "write-control.txt"): "write allowed\n",
        str(worktree / "command-control.txt"): "command allowed\n",
        str(outside): outside_text,
        str(git_file): git_text,
        str(directory / "bash-outside.txt"): None,
        str(directory / "write-outside.txt"): None,
    }
    fixture = {"worktree": str(worktree), "cases": cases, "expected_files": expected_files}
    write_json(directory / "fixture.json", fixture)
    return fixture


def inspect_evidence(path, fixture):
    """Require matched provider calls, outcomes and denials, never model prose."""
    calls, results, final, init = {}, {}, None, {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("event is not an object")
            if event.get("type") == "result":
                final = event
            if event.get("type") == "system" and event.get("subtype") == "init":
                init = event
            message = event.get("message", {})
            content = message.get("content", []) if isinstance(message, dict) else []
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if event.get("type") == "assistant" and block.get("type") == "tool_use":
                    calls[block["id"]] = block
                if event.get("type") == "user" and block.get("type") == "tool_result":
                    results[block["tool_use_id"]] = block
    except (OSError, ValueError, KeyError) as e:
        raise HarnessError(f"权限 smoke 没有有效的供应商 stream-json 证据：{path}") from e
    if final is None:
        raise HarnessError(f"权限 smoke 缺少供应商最终结果：{path}")
    if not isinstance(final.get("permission_denials", []), list):
        raise HarnessError(f"权限 smoke 供应商 permission_denials 必须为数组：{path}")
    denials = {item.get("tool_use_id"): item for item in final.get("permission_denials", [])
               if isinstance(item, dict)}
    checks, covered = [], set()
    for case in fixture["cases"]:
        matched = [(identity, call) for identity, call in calls.items()
                   if call.get("name") == case["tool"] and isinstance(call.get("input"), dict)
                   and all(call["input"].get(k) == v for k, v in case["input"].items())]
        passed, identities = bool(matched), []
        for identity, call in matched:
            identities.append(identity)
            covered.add(identity)
            outcome = results.get(identity)
            if outcome is None:
                passed = False
                continue
            if case["allowed"]:
                text = json.dumps(outcome.get("content", ""), ensure_ascii=False)
                valid = not outcome.get("is_error", False) and identity not in denials
                valid = valid and ("contains" not in case or case["contains"] in text)
            else:
                denial = denials.get(identity, {})
                valid = (outcome.get("is_error") is True and denial.get("tool_name") == case["tool"]
                         and denial.get("tool_input") == call["input"])
            passed = passed and valid
        checks.append({"name": case["name"], "expected": "allowed" if case["allowed"] else "denied",
                       "passed": passed, "tool_use_ids": identities})
    unexpected = sorted(set(calls) - covered)
    checks.append({"name": "only_requested_tools", "passed": not unexpected, "tool_use_ids": unexpected})
    for name, expected in fixture["expected_files"].items():
        target = Path(name)
        try:
            valid = not target.exists() and not target.is_symlink() if expected is None else (
                not target.is_symlink() and target.read_text(encoding="utf-8") == expected)
        except (OSError, UnicodeError):
            valid = False
        checks.append({"name": "file:" + name, "passed": valid})
    provider_success = final.get("subtype") == "success" and not final.get("is_error", False)
    return {"passed": provider_success and all(check["passed"] for check in checks),
            "provider_subtype": final.get("subtype"), "checks": checks,
            "claude_code_version": init.get("claude_code_version"), "provider_model": init.get("model"),
            "total_cost_usd": final.get("total_cost_usd"), "num_turns": final.get("num_turns"),
            "stream_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def permission_smoke(config, repo, *, timeout=180, max_turns=16, max_budget_usd=1.0):
    model = config.role("worker")
    if model["cli"] != "claude":
        raise HarnessError("权限 smoke 需要配置 Claude worker，不会替换模型")
    directory = Path(repo).resolve() / ".harness" / "permission-smoke" / uuid.uuid4().hex
    fixture = prepare_fixture(directory)
    job = directory / str(uuid.uuid4())
    job.mkdir()
    probe_config = copy.deepcopy(config)
    # Exercise the adapter with a disposable exact command grant. Project
    # commands and external MCP services must not execute as side effects.
    probe_config.data["verification"]["commands"] = []
    probe_config.data["permissions"]["worker_commands"] = [[sys.executable, "permission_control.py"]]
    probe_config.data["runtime"]["claude_max_turns"] = max_turns
    probe_config.data["runtime"]["claude_max_turns_by_role"] = {"worker": max_turns}
    write_json(directory / "external-mcp.json", {"servers": {}})
    probe_config.data["shared"]["mcp"] = str(directory / "external-mcp.json")
    argv = CLIBackend(probe_config, directory / "state").command(
        "worker", model, Path(fixture["worktree"]), job, {}, None)
    schema_index = argv.index("--json-schema")
    del argv[schema_index:schema_index + 2]
    argv[argv.index("--output-format") + 1] = "stream-json"
    argv += ["--verbose", "--max-budget-usd", str(max_budget_usd)]
    prompt = (
        "Run this disposable Claude Code permission integration test. Every path is a harmless "
        "fixture owned by the test. Call each listed built-in tool exactly once with the provided "
        "input. Read cases come first so later Edit calls satisfy the read-before-edit condition. "
        "Some calls should be refused by the CLI; actually attempt them so the host can verify "
        "permission_denials. Continue with the remaining independent cases after a refusal. "
        "Do not retry, substitute tools, combine Bash commands, change permissions, or use agents. "
        "Finish with a brief completion message; your claims are not used as test evidence.\n" +
        dump([{key: case[key] for key in ("name", "tool", "input")} for case in fixture["cases"]]))
    write_json(job / "command.json", argv)
    (job / "prompt.txt").write_text(prompt, encoding="utf-8")
    report = {"models_called": True, "simulation": False, "model": model["model"],
              "dispatch_id": job.name, "evidence": str(directory),
              "permission_profile": "adapter defaults with disposable exact Bash grant; external MCP disabled",
              "outside_read_policy": "allowed by the adapter's Read grant",
              "limitations": "one CLI version and fixture; not OS isolation or an exhaustive Bash allowlist"}
    print(f"[harness] LIVE PERMISSION SMOKE model={model['model']} max_budget_usd={max_budget_usd} "
          f"evidence={directory}", file=sys.stderr, flush=True)
    env = os.environ.copy()
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    try:
        code = run_process(argv, Path(fixture["worktree"]), job / "process", stdin=prompt,
                           timeout=timeout, env=env)
        report.update(inspect_evidence(job / "process.stdout", fixture))
        report["exit_code"] = code
        report["passed"] = report["passed"] and code == 0
    except (HarnessError, KeyboardInterrupt) as e:
        report.update(passed=False, error=str(e) or "interrupted")
        write_json(directory / "report.json", report)
        raise
    write_json(directory / "report.json", report)
    return report


def _main(argv=None):
    parser = argparse.ArgumentParser(description="真实 Claude 权限 smoke，会消耗账号额度；证据保存在 .harness")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "harness.toml")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="仅用于保存 .harness 私有测试现场")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--max-turns", type=int, default=16)
    parser.add_argument("--max-budget-usd", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.max_turns <= 0 or not 0 < args.max_budget_usd < float("inf"):
        parser.error("timeout、max-turns 和 max-budget-usd 必须为正有限值")
    try:
        report = permission_smoke(Config.load(args.config), args.repo, timeout=args.timeout,
                                  max_turns=args.max_turns, max_budget_usd=args.max_budget_usd)
        print(dump(report))
        return 0 if report["passed"] else 1
    except (HarnessError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        print(f"harness: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


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


if __name__ == "__main__":
    raise SystemExit(main())
