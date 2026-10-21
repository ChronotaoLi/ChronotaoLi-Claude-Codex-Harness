import tomllib
from dataclasses import dataclass
from pathlib import Path

from .util import HarnessError


@dataclass
class Config:
    path: Path
    data: dict

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        data = tomllib.loads(path.read_text())
        for name in ("commander", "worker", "reviewer", "fable"):
            role = data["roles"][name]
            if role["cli"] not in ("codex", "claude") or not role["model"]:
                raise HarnessError(f"无效模型配置：{name}")
        for key in ("agent_timeout_seconds", "max_attempts", "max_tasks", "context_chars", "verify_timeout_seconds"):
            if not isinstance(data["runtime"][key], int) or data["runtime"][key] <= 0:
                raise HarnessError(f"runtime.{key} 必须为正整数")
        for command in data["verification"]["commands"]:
            if not isinstance(command, list) or not command or not all(isinstance(s, str) for s in command):
                raise HarnessError("验证命令必须使用 argv 数组")
        return cls(path, data)

    @property
    def root(self):
        return self.path.parent

    def asset(self, key):
        return (self.root / self.data["shared"][key]).resolve()

    def role(self, name, commander="astra"):
        key = "fable" if name == "commander" and commander == "fable" else name
        return self.data["roles"][key]
