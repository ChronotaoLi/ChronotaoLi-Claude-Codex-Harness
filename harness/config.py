import copy
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .util import HarnessError


DEFAULTS = {
    "runtime": {
        "agent_timeout_seconds": 1200, "verify_timeout_seconds": 300,
        "max_attempts": 3, "max_total_attempts": 60, "max_replans": 2,
        "max_tasks": 20, "context_chars": 32000, "claude_max_turns": 50,
    },
    "permissions": {"worker_commands": []},
    "memory": {"task_ttl_days": 180, "knowledge_ttl_days": 180},
    "verification": {"commands": []},
}
RESUME_INCREASES = (
    "agent_timeout_seconds", "verify_timeout_seconds", "context_chars",
    "claude_max_turns", "max_attempts", "max_total_attempts",
)

# Arguments may be appended only after --, where they cannot enable Git's
# external diff drivers or output-file options. This is a CLI permission aid,
# not an operating-system sandbox or an exhaustive allowlist of CLI behavior.
READONLY_COMMANDS = (
    ("git", "--no-pager", "status", "--short", "--"),
    ("git", "--no-pager", "diff", "--no-ext-diff", "--no-textconv", "--"),
    ("git", "--no-pager", "diff", "--cached", "--no-ext-diff", "--no-textconv", "--"),
    ("git", "--no-pager", "log", "--oneline", "--max-count=20", "--no-ext-diff", "--no-textconv", "--"),
    ("git", "--no-pager", "show", "--no-ext-diff", "--no-textconv", "HEAD", "--"),
    ("ls", "--"),
    ("rg", "--"),
    ("rg", "--files", "--"),
    ("wc", "--"),
)


def normalized(data):
    data = copy.deepcopy(data)
    if not isinstance(data, dict):
        raise HarnessError("配置必须为对象")
    for section, defaults in DEFAULTS.items():
        values = data.setdefault(section, {})
        if not isinstance(values, dict):
            raise HarnessError(f"{section} 必须为配置表")
        for key, value in defaults.items():
            values.setdefault(key, copy.deepcopy(value))
    return data


def validate_commands(commands, field, *, exact_permissions=False):
    if not isinstance(commands, list):
        raise HarnessError(f"{field} 必须使用 argv 数组列表")
    for command in commands:
        if (not isinstance(command, list) or not command
                or not all(isinstance(s, str) and "\0" not in s for s in command)
                or not command[0].strip()):
            raise HarnessError(f"{field} 必须使用非空 argv 数组")
        if exact_permissions and any("*" in s or "\n" in s or "\r" in s for s in command):
            raise HarnessError(f"{field} 只接受精确命令，不能包含 * 或换行")


@dataclass
class Config:
    path: Path
    data: dict

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        data = normalized(tomllib.loads(path.read_text()))
        cls.validate(data)
        return cls(path, data)

    @staticmethod
    def validate(data):
        if not isinstance(data.get("roles"), dict):
            raise HarnessError("roles 必须为配置表")
        for name in ("commander", "worker", "reviewer", "fable"):
            role = data.get("roles", {}).get(name, {})
            if (not isinstance(role, dict) or role.get("cli") not in ("codex", "claude")
                    or not isinstance(role.get("model"), str) or not role["model"].strip()
                    or not isinstance(role.get("effort"), str) or not role["effort"].strip()):
                raise HarnessError(f"无效模型配置：{name}")
        if data["roles"]["worker"]["cli"] == "codex":
            raise HarnessError("当前 Codex 适配器仅支持只读角色，不能配置为 worker")
        for key in DEFAULTS["runtime"]:
            value = data["runtime"][key]
            if type(value) is not int or value < 0 or (key != "max_replans" and value == 0):
                if key == "max_replans":
                    raise HarnessError(f"runtime.{key} 必须为非负整数")
                raise HarnessError(f"runtime.{key} 必须为正整数")
        for key in DEFAULTS["memory"]:
            if type(data["memory"][key]) is not int or data["memory"][key] < 0:
                raise HarnessError(f"memory.{key} 必须为非负整数；0 表示不设到期时间")
        verification = data.get("verification", {})
        if not isinstance(verification, dict):
            raise HarnessError("verification 必须为配置表")
        validate_commands(verification.get("commands", []), "verification.commands")
        validate_commands(data["permissions"]["worker_commands"], "permissions.worker_commands", exact_permissions=True)

    def resume_changes(self, snapshot):
        """Return permitted budget increases without mutating either snapshot."""
        previous, current = normalized(snapshot), normalized(self.data)
        self.validate(previous)
        self.validate(current)
        changes = {}
        for key in RESUME_INCREASES:
            old, new = previous["runtime"].pop(key), current["runtime"].pop(key)
            if new < old:
                raise HarnessError(f"配置已改变：恢复时 runtime.{key} 只能保持或增大（{old} → {new}）")
            if new != old:
                changes[key] = {"old": old, "new": new}
        if previous != current:
            raise HarnessError("配置已改变：恢复只允许增大超时、上下文、轮数和尝试预算；其他配置必须保持一致")
        return changes

    @staticmethod
    def readonly_commands():
        """Read-only command examples; append paths/patterns after their --."""
        return [list(command) for command in READONLY_COMMANDS]

    @property
    def root(self):
        return self.path.parent

    def asset(self, key):
        return (self.root / self.data["shared"][key]).resolve()

    def role(self, name, commander="astra"):
        key = "fable" if name == "commander" and commander == "fable" else name
        return self.data["roles"][key]
