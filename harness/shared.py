from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from . import stops
from .util import HarnessError, read_json


def asset_digest(config):
    # Logical identities keep a byte-identical installation portable; framing
    # binds each identity to its bytes without ambiguous concatenation.
    digest = hashlib.sha256(b"personal-harness/shared-assets/v2\0")
    files = [(key, config.asset(key)) for key in ("rules", "mcp")]
    files += [(f"skills/{path.parent.name}/SKILL.md", path)
              for path in sorted(config.asset("skills").glob("*/SKILL.md"))]
    for identity, path in files:
        for value in (identity.encode("utf-8"), path.read_bytes()):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {toml_value(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


ROLE_SKILLS = frozenset({"harness-commander", "harness-worker", "harness-reviewer"})


def skills_catalog(root: Path, *, task_only=False):
    """List readable skills, optionally excluding automatically injected role skills.

    The full catalog remains available for validating existing task plans and
    explicit MCP retrieval, including plans that reference a builtin role skill.
    """
    result = []
    for path in sorted(root.glob("*/SKILL.md")):
        if path.is_symlink() or path.parent.is_symlink():
            raise HarnessError("shared skills 不能使用符号链接")
        if task_only and path.parent.name in ROLE_SKILLS:
            continue
        match = re.search(r"^description:\s*(.+)$", path.read_text(encoding="utf-8"), re.M)
        result.append({"name": path.parent.name, "description": match.group(1) if match else path.parent.name})
    return result


def read_skill(root: Path, name: str):
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise HarnessError("无效 skill 名称")
    path = root / name / "SKILL.md"
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise HarnessError(f"skill 不存在或为链接：{name}")
    return path.read_text(encoding="utf-8")


@stops.classifies("invalid_input", "input_schema", data={"option": "config"})
def validate_mcp(config, roles=("commander", "worker", "reviewer")):
    """Read and validate active external servers without connecting or writing state."""
    path = config.asset("mcp")
    try:
        document = read_json(path)
    except (OSError, ValueError) as error:
        raise HarnessError(f"无法读取 MCP JSON：{path}：{error}") from error
    if not isinstance(document, dict):
        raise HarnessError("MCP 配置必须为对象")
    servers = document.get("servers", {})
    if not isinstance(servers, dict):
        raise HarnessError("MCP servers 必须为对象")

    def text(value):
        return isinstance(value, str) and bool(value.strip()) and "\0" not in value

    selected = {}
    for name, spec in servers.items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name) or name in ("harness", "harness_commands"):
            raise HarnessError(f"无效或保留 MCP 名称：{name}")
        field = f"MCP servers.{name}"
        if not isinstance(spec, dict):
            raise HarnessError(f"{field} 必须为对象")
        if type(spec.get("enabled", True)) is not bool:
            raise HarnessError(f"{field}.enabled 必须为布尔值")
        if not spec.get("enabled", True):
            # Disabled examples may intentionally leave connection fields unset.
            continue
        visible = spec.get("roles", ["worker"])
        if (not isinstance(visible, list)
                or any(role not in ("commander", "worker", "reviewer") for role in visible)):
            raise HarnessError(f"{field}.roles 必须为 commander、worker、reviewer 的数组")
        active_roles = set(roles).intersection(visible)
        if not active_roles:
            continue
        if type(spec.get("read_only", False)) is not bool:
            raise HarnessError(f"{field}.read_only 必须为布尔值")
        if active_roles - {"worker"} and not spec.get("read_only", False):
            raise HarnessError(f"{field}.read_only：指挥和审阅只能连接声明 read_only=true 的 MCP")
        transport = spec.get("transport")
        if transport not in ("stdio", "http"):
            raise HarnessError(f"{field}.transport 必须为 stdio 或 http")
        if transport == "stdio":
            if not text(spec.get("command")):
                raise HarnessError(f"{field}.command 必须为非空字符串")
            args = spec.get("args", [])
            if (not isinstance(args, list)
                    or any(not isinstance(arg, str) or "\0" in arg for arg in args)):
                raise HarnessError(f"{field}.args 必须为字符串数组，不能包含 NUL")
            keys = spec.get("env_vars", [])
            if not isinstance(keys, list) or any(not text(key) for key in keys):
                raise HarnessError(f"{field}.env_vars 必须为非空环境变量名称的数组")
        else:
            if not text(spec.get("url")):
                raise HarnessError(f"{field}.url 必须为非空字符串")
            if "bearer_token_env_var" in spec and not text(spec["bearer_token_env_var"]):
                raise HarnessError(f"{field}.bearer_token_env_var 必须为非空环境变量名称字符串")
        selected[name] = spec
    return selected


def mcp_configs(config, state: Path, role: str, run_path: Path | None = None):
    servers = validate_mcp(config, roles=(role,))
    root = Path(__file__).resolve().parent.parent
    builtin = {"command": sys.executable, "args": [str(root / "harness" / "mcp_entry.py"),
               "--state", str(state), "--skills", str(config.asset("skills"))]}
    if run_path:
        builtin["args"] += ["--run", str(run_path)]
    claude, codex = {"harness": {"type": "stdio", **builtin}}, {"harness": {**builtin, "required": True}}
    for name, spec in servers.items():
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
    return {"mcpServers": claude}, codex
