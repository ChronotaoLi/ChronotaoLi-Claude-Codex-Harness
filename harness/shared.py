from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from .util import HarnessError, read_json, run_process


def asset_digest(config):
    digest = hashlib.sha256()
    files = [config.asset(key) for key in ("rules", "mcp", "hooks")]
    files += sorted(config.asset("skills").glob("*/SKILL.md"))
    for path in files:
        digest.update(str(path).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {toml_value(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def skills_catalog(root: Path):
    result = []
    for path in sorted(root.glob("*/SKILL.md")):
        if path.is_symlink() or path.parent.is_symlink():
            raise HarnessError("shared skills 不能使用符号链接")
        match = re.search(r"^description:\s*(.+)$", path.read_text(), re.M)
        result.append({"name": path.parent.name, "description": match.group(1) if match else path.parent.name})
    return result


def read_skill(root: Path, name: str):
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise HarnessError("无效 skill 名称")
    path = root / name / "SKILL.md"
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise HarnessError(f"skill 不存在或为链接：{name}")
    return path.read_text()


def mcp_configs(config, state: Path, role: str, run_path: Path | None = None):
    root = Path(__file__).resolve().parent.parent
    builtin = {"command": sys.executable, "args": [str(root / "harness" / "mcp_entry.py"),
               "--state", str(state), "--skills", str(config.asset("skills"))]}
    if run_path:
        builtin["args"] += ["--run", str(run_path)]
    claude, codex = {"harness": {"type": "stdio", **builtin}}, {"harness": {**builtin, "required": True}}
    for name, spec in read_json(config.asset("mcp")).get("servers", {}).items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name) or name == "harness":
            raise HarnessError(f"无效或保留 MCP 名称：{name}")
        if not spec.get("enabled", True) or role not in spec.get("roles", ["worker"]):
            continue
        if role != "worker" and not spec.get("read_only", False):
            raise HarnessError(f"{role} 只能连接声明 read_only=true 的 MCP：{name}")
        if spec["transport"] == "stdio":
            common = {"command": spec["command"], "args": spec.get("args", [])}
            keys = spec.get("env_vars", [])
            claude[name] = {"type": "stdio", **common, "env": {k: "${" + k + "}" for k in keys}}
            codex[name] = {**common, "env_vars": keys, "required": True}
        elif spec["transport"] == "http":
            claude[name] = {"type": "http", "url": spec["url"]}
            codex[name] = {"url": spec["url"], "required": True}
            if token := spec.get("bearer_token_env_var"):
                claude[name]["headers"] = {"Authorization": "Bearer ${" + token + "}"}
                codex[name]["bearer_token_env_var"] = token
        else:
            raise HarnessError(f"不支持的 MCP transport：{spec['transport']}")
    return {"mcpServers": claude}, codex


class Hooks:
    EVENTS = {"pre_agent", "post_agent", "pre_verify", "post_verify", "pre_accept", "post_accept", "on_error",
              "context_prepared", "context_compacted"}

    def __init__(self, config):
        self.config = config
        self.spec = read_json(config.asset("hooks"))
        if set(self.spec) - self.EVENTS:
            raise HarnessError("hooks.json 含未知事件")
        if self.spec.get("context_prepared") and self.spec.get("context_compacted"):
            raise HarnessError("context_prepared 与旧名 context_compacted 只能配置一个，避免重复执行")

    def emit(self, event, payload, logs: Path):
        if event == "context_prepared" and not self.spec.get(event) and self.spec.get("context_compacted"):
            event = "context_compacted"
        for i, hook in enumerate(self.spec.get(event, [])):
            argv = hook["argv"]
            if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
                raise HarnessError("hook.argv 必须为非空字符串数组")
            log = logs / f"hook-{event}-{i}"
            code = run_process(argv, self.config.root, log,
                               stdin=json.dumps({"event": event, **payload}), timeout=hook.get("timeout_seconds", 30))
            if code:
                raise HarnessError(f"hook {event} 拒绝继续，exit={code}；日志：{log}")
