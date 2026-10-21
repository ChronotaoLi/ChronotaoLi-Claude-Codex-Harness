from __future__ import annotations

import json
import os
import shlex

from .shared import mcp_configs, toml_value
from .util import HarnessError, read_json, run_process, tail, write_json


class CLIBackend:
    def __init__(self, config, state):
        self.config, self.state = config, state

    def command(self, role, model, cwd, job, schema, run_path):
        claude, codex = mcp_configs(self.config, self.state, role, run_path)
        write_json(job / "mcp.json", claude)
        if model["cli"] == "codex":
            return ["codex", "exec", "--model", model["model"], "--ephemeral", "--ignore-user-config",
                    "--sandbox", "read-only", "--cd", str(cwd), "--json", "--color", "never",
                    "-c", 'approval_policy="never"', "-c", f'model_reasoning_effort={json.dumps(model["effort"])}',
                    "-c", "features.multi_agent=false", "-c", "features.memories=false",
                    "-c", "features.hooks=false", "-c", "features.plugins=false",
                    "-c", "mcp_servers=" + toml_value(codex),
                    "--output-schema", str(job / "schema.json"),
                    "--output-last-message", str(job / "response.json"), "-"]
        settings = {"autoMemoryEnabled": False, "disableAllHooks": True}
        argv = ["claude", "--print", "--model", model["model"], "--effort", model["effort"],
                "--no-session-persistence", "--session-id", job.name,
                "--setting-sources", "", "--settings", json.dumps(settings),
                "--strict-mcp-config", "--mcp-config", str(job / "mcp.json"),
                "--output-format", "json", "--json-schema", json.dumps(schema),
                "--max-turns", str(self.config.data["runtime"]["claude_max_turns"]),
                "--permission-mode", "dontAsk", "--disallowedTools", "Agent,Task"]
        if role == "worker":
            allow = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(git status *)", "Bash(git diff *)",
                     "Bash(git log *)", "Bash(rg *)", "Bash(ls *)"]
            allow += ["Bash(" + shlex.join(cmd) + ")" for cmd in self.config.data["verification"]["commands"]]
            argv += ["--tools", "Read,Edit,Write,Glob,Grep,Bash"]
        else:
            allow = ["Read", "Glob", "Grep"]
            argv += ["--tools", "Read,Glob,Grep"]
        # Only configured MCP servers are exposed. Their permissions are a trusted operator choice.
        allow += ["mcp__" + name for name in claude["mcpServers"]]
        argv += ["--allowedTools", ",".join(allow)]
        return argv

    def invoke(self, *, role, model, cwd, job, schema, prompt, run_path, on_start, task=None, kind=None):
        write_json(job / "schema.json", schema)
        (job / "prompt.txt").write_text(prompt)
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
            return read_json(job / "response.json")
        result = read_json(job / "process.stdout")
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
        return value


class DemoBackend:
    """Explicit offline simulation, only used by the demo command and tests."""
    def invoke(self, *, role, model, cwd, job, schema, prompt, run_path, on_start, task=None, kind=None):
        ids = {k: schema["properties"][k]["enum"][0] for k in ("task_id", "dispatch_id")}
        if role == "worker":
            (cwd / "hello.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n')
            result = {"status": "completed", "summary": "Implemented greet", "evidence": ["hello.py"], "blockers": []}
        elif role == "reviewer":
            result = {"verdict": "approve", "summary": "Demo simulated review", "findings": []}
        elif kind == "plan":
            result = {"tasks": [{"id": "greet", "title": "Implement greet", "description": "Implement greet(name) returning Hello, name!",
                                  "depends_on": [], "allowed_paths": ["hello.py"], "acceptance": ["test_greet passes"], "skills": []}]}
        else:
            result = {"selected_task": task["id"], "instruction": "Complete this one task and satisfy its acceptance criteria."}
        value = {**ids, **result}
        write_json(job / "response.json", value)
        (job / "prompt.txt").write_text(prompt)
        return value
