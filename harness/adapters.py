from __future__ import annotations

import json
import math
import os
import shlex
import sys
from copy import deepcopy
from pathlib import Path

from .command_server import create_manifest
from .shared import mcp_configs, toml_value
from .util import HarnessError, read_json, run_process, tail, write_json


_USAGE_MAX_BYTES = 16 * 1024 * 1024
_USAGE_MAX_COUNT = 2**63 - 1


def _usage_counts(value, fields):
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in fields
            if type(value.get(key)) is int and 0 <= value[key] <= _USAGE_MAX_COUNT}


def provider_usage(job, provider):
    """Read bounded diagnostics from the CLI, never from the model's response.

    Missing/invalid metrics stay unknown. The CLI's cost is its reported estimate,
    not an account bill; none of these observations affect execution budgets.
    """
    try:
        with (job / "process.stdout").open("rb") as stream:
            raw = stream.read(_USAGE_MAX_BYTES + 1)
        if len(raw) > _USAGE_MAX_BYTES:
            return None
        if provider == "claude":
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("type") != "result":
                return None
            metrics = _usage_counts(result, ("num_turns",))
            cost = result.get("total_cost_usd")
            if type(cost) in (int, float) and 0 <= cost <= 1_000_000_000 and math.isfinite(cost):
                metrics["reported_cost_usd"] = cost
            tokens = _usage_counts(result.get("usage"), (
                "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        elif provider == "codex":
            turns, tokens = 0, None
            for line in raw.splitlines():
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if not isinstance(event, dict) or event.get("type") != "turn.completed":
                    continue
                turns += 1
                reported = _usage_counts(event.get("usage"), (
                    "input_tokens", "cached_input_tokens", "output_tokens"))
                # Sum only fields reported for every completed turn. A missing
                # field cannot turn into a misleading partial dispatch total.
                tokens = reported if tokens is None else {
                    key: value + reported[key] for key, value in tokens.items()
                    if key in reported and value + reported[key] <= _USAGE_MAX_COUNT}
            metrics = {"completed_turns": turns} if turns else {}
        else:
            return None
        if tokens:
            metrics["tokens"] = tokens
        return {"provider": provider, "source": "process.stdout", **metrics} if metrics else None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def provider_schema(schema):
    """Project our richer local contract onto both CLIs' common schema core."""
    core = {"type", "properties", "required", "additionalProperties", "items", "enum"}
    result = {key: deepcopy(value) for key, value in schema.items() if key in core}
    if "properties" in result:
        result["properties"] = {name: provider_schema(value) for name, value in result["properties"].items()}
    if "items" in result:
        result["items"] = provider_schema(result["items"])
    return result


def claude_literal_worktree(cwd):
    path = cwd.resolve().as_posix().rstrip("/")
    if "\n" in path or "\r" in path:
        raise HarnessError("Claude 工作树路径不能包含换行符，无法生成精确文件权限")
    return "".join("\\" + char if char in "\\*?[]" else char for char in path)


def claude_worktree_edit_rule(cwd):
    """Grant built-in file writes only below this literal, resolved worktree."""
    # Claude uses gitignore patterns: // anchors at the filesystem root and
    # Edit(path) also governs Write. Escape the directory, not the final /**.
    return "Edit(/" + claude_literal_worktree(cwd) + "/**)"


def claude_worktree_git_deny_rules(cwd):
    """Protect Git discovery metadata as well as any replacement directory."""
    path = claude_literal_worktree(cwd)
    return ["Edit(/" + path + suffix + ")" for suffix in ("/.git", "/.git/**")]


class CLIBackend:
    def __init__(self, config, state):
        self.config, self.state = config, state

    def command(self, role, model, cwd, job, schema, run_path):
        if role == "worker" and model["cli"] == "codex":
            raise HarnessError("当前 Codex 适配器仅支持只读角色，不能配置为 worker")
        claude, codex = mcp_configs(self.config, self.state, role, run_path)
        if role == "worker":
            manifest = create_manifest(self.config, cwd, job.resolve(), run_path)
            claude["mcpServers"]["harness_commands"] = {
                "type": "stdio", "command": sys.executable,
                "timeout": int(self.config.data["runtime"]["verify_timeout_seconds"] * 1000) + 10000,
                "args": [str(Path(__file__).with_name("command_entry.py").resolve()),
                         "--manifest", manifest["artifact"], "--sha256", manifest["artifact_digest"]]}
        write_json(job / "mcp.json", claude)
        wire_schema = provider_schema(schema)
        write_json(job / "provider-schema.json", wire_schema)
        if model["cli"] == "codex":
            return ["codex", "exec", "--model", model["model"], "--ephemeral", "--ignore-user-config",
                    "--sandbox", "read-only", "--cd", str(cwd), "--json", "--color", "never",
                    "-c", 'approval_policy="never"', "-c", f'model_reasoning_effort={json.dumps(model["effort"])}',
                    "-c", "features.multi_agent=false", "-c", "features.memories=false",
                    "-c", "features.hooks=false", "-c", "features.plugins=false",
                    "-c", "project_doc_max_bytes=0",
                    "-c", "mcp_servers=" + toml_value(codex),
                    "--output-schema", str(job / "provider-schema.json"),
                    "--output-last-message", str(job / "response.json"), "-"]
        allow = ["Read", "Glob", "Grep"]
        allow += ["Bash(" + shlex.join(cmd) + " *)" for cmd in self.config.readonly_commands()]
        tools = "Read,Glob,Grep,Bash"
        if role == "worker":
            allow.append(claude_worktree_edit_rule(cwd))
            tools = "Read,Edit,Write,Glob,Grep,Bash"
            commands = self.config.data["verification"]["commands"] + self.config.data["permissions"]["worker_commands"]
            # A literal * in an argv becomes a permission wildcard even when
            # shell-quoted. The broker can execute these literal argv safely;
            # they must never become broad Bash grants.
            allow += ["Bash(" + shlex.join(cmd) + ")" for cmd in commands
                      if not any("*" in arg or "\n" in arg or "\r" in arg for arg in cmd)]
        # Only configured MCP servers are exposed. Their permissions are a trusted operator choice.
        allow += ["mcp__" + name for name in claude["mcpServers"]]
        # JSON preserves commas and parentheses inside exact command arguments.
        permissions = {"allow": allow}
        if role == "worker":
            permissions["deny"] = claude_worktree_git_deny_rules(cwd)
        settings = {"autoMemoryEnabled": False, "disableAllHooks": True, "permissions": permissions}
        return ["claude", "--print", "--model", model["model"], "--effort", model["effort"],
                "--no-session-persistence", "--session-id", job.name,
                "--setting-sources", "", "--settings", json.dumps(settings),
                "--strict-mcp-config", "--mcp-config", str(job / "mcp.json"),
                "--output-format", "json", "--json-schema", json.dumps(wire_schema),
                "--max-turns", str(self.config.data["runtime"]["claude_max_turns"]),
                "--permission-mode", "dontAsk", "--disallowedTools", "Agent,Task",
                "--tools", tools]

    def invoke(self, *, role, model, cwd, job, schema, prompt, run_path, on_start, task=None, kind=None):
        write_json(job / "schema.json", schema)
        (job / "prompt.txt").write_text(prompt, encoding="utf-8")
        argv = self.command(role, model, cwd, job, schema, run_path)
        write_json(job / "command.json", argv)
        env = os.environ.copy()
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        code = run_process(argv, cwd, job / "process", stdin=prompt,
                           timeout=self.config.data["runtime"]["agent_timeout_seconds"], env=env, on_start=on_start)
        if code:
            raise HarnessError(f"{role} CLI exit={code}；{job}\n{tail(job / 'process.stderr')}")
        if model["cli"] == "codex":
            if not (job / "response.json").exists():
                raise HarnessError(f"Codex 未输出结构化结果：{job}")
            return self.read_result(job / "response.json", "Codex")
        result = self.read_result(job / "process.stdout", "Claude")
        write_json(job / "provider-result.json", result)
        if result.get("is_error") or result.get("subtype", "success") != "success":
            raise HarnessError(f"Claude 未成功完成：{result.get('subtype')}；日志：{job}")
        value = result.get("structured_output")
        if value is None:
            try:
                value = json.loads(result.get("result", ""))
            except (ValueError, TypeError) as e:
                raise HarnessError(f"Claude 未返回 JSON；日志：{job}") from e
        write_json(job / "response.json", value)
        if not isinstance(value, dict):
            raise HarnessError(f"Claude 结构化结果必须为 JSON 对象；日志：{job}")
        return value

    @staticmethod
    def read_result(path, provider):
        try:
            value = read_json(path)
        except (OSError, ValueError) as e:
            raise HarnessError(f"{provider} 未返回有效 JSON；日志：{path}") from e
        if not isinstance(value, dict):
            raise HarnessError(f"{provider} 结果必须为 JSON 对象；日志：{path}")
        return value


class DemoBackend:
    """Explicit offline simulation, only used by the demo command and tests."""
    def invoke(self, *, role, model, cwd, job, schema, prompt, run_path, on_start, task=None, kind=None):
        ids = {k: schema["properties"][k]["enum"][0] for k in ("task_id", "dispatch_id")}
        if role == "worker":
            (cwd / "hello.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n', encoding="utf-8")
            result = {"status": "completed", "summary": "Implemented greet", "evidence": ["hello.py"],
                      "blockers": [], "blocker_kind": "none", "proposed_memories": []}
        elif role == "reviewer":
            result = {"verdict": "approve", "summary": "Demo simulated review", "findings": [],
                      "approved_memory_keys": []}
        elif kind == "plan":
            result = {"tasks": [{"id": "greet", "title": "Implement greet", "description": "Implement greet(name) returning Hello, name!",
                                  "depends_on": [], "allowed_paths": ["hello.py"], "acceptance": ["test_greet passes"], "skills": []}]}
        elif kind == "replan":
            result = {"action": "stop", "reason": "Demo cannot replan a failed task automatically", "new_tasks": [], "updates": []}
        else:
            result = {"selected_task": task["id"], "instruction": "Complete this one task and satisfy its acceptance criteria."}
        value = {**ids, **result}
        write_json(job / "response.json", value)
        (job / "prompt.txt").write_text(prompt, encoding="utf-8")
        return value
