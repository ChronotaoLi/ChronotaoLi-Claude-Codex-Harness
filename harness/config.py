import copy
import hashlib
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .util import HarnessError


DEFAULTS = {
    "runtime": {
        "agent_timeout_seconds": 3600, "verify_timeout_seconds": 300,
        "max_attempts": 5, "max_agent_dispatches": 200,
        "context_chars": 100000, "claude_max_turns": 50,
        "claude_max_turns_by_role": {},
        "quota_wait_max_seconds": 21600,
    },
    "permissions": {"worker_commands": []},
    "memory": {"task_ttl_days": 180, "knowledge_ttl_days": 180},
    "verification": {"commands": [], "fresh_checkout": False, "setup_commands": []},
}
RESUME_INCREASES = (
    "agent_timeout_seconds", "verify_timeout_seconds", "context_chars",
    "claude_max_turns", "max_attempts", "max_agent_dispatches",
)
DISPATCH_ROLES = ("commander", "worker", "reviewer")
ROLE_NAMES = ("commander", "worker", "reviewer", "fable")
# A wait policy, not a budget: a resume may raise, lower or disable it (0).
QUOTA_WAIT_KEY = "quota_wait_max_seconds"
# The top-level sections each file may hold: the operator's file is the same for
# every project; the project file lives at <Git root>/.harness/project.toml.
OPERATOR_SECTIONS = ("roles", "runtime", "memory", "shared")
PROJECT_SECTIONS = ("verification", "permissions", "runtime")
OPERATOR_LABEL, PROJECT_LABEL = "操作者配置", "项目配置"
# Operator-facing hints that ask for a verification or permissions change name the file holding them.
PROJECT_FILE = f"{PROJECT_LABEL}（目标仓库的 .harness/project.toml）"


def claude_turn_limit(runtime, role):
    return runtime.get("claude_max_turns_by_role", {}).get(role, runtime["claude_max_turns"])

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


def unknown_keys(data):
    """Full paths of keys no section allows; sections absent from ``data`` are skipped."""
    unknown = []

    def check_keys(values, allowed, prefix=""):
        if isinstance(values, dict):
            unknown.extend(prefix + key for key in values if key not in allowed)

    check_keys(data, {*DEFAULTS, "shared", "roles"})
    for section, defaults in DEFAULTS.items():
        check_keys(data.get(section), defaults, section + ".")
    check_keys(data.get("shared"), ("rules", "skills", "mcp"), "shared.")
    roles = data.get("roles")
    check_keys(roles, ROLE_NAMES, "roles.")
    if isinstance(roles, dict):
        for name in ROLE_NAMES:
            check_keys(roles.get(name), ("cli", "model", "effort"), f"roles.{name}.")
    return unknown


def read_layer(path):
    """One configuration file read once: (bytes, sha256). Whatever is parsed is these very bytes."""
    raw = path.read_bytes()
    return raw, hashlib.sha256(raw).hexdigest()


def parse_layer(raw, path, label, sections):
    """One file's bytes as read, checked on their own.

    Only the file's own sections are accepted and each must be a table; those refusals
    and unknown keys name the file. UTF-8 and TOML errors propagate unchanged and do not name it.
    """
    data = tomllib.loads(raw.decode("utf-8"))
    for section, values in data.items():
        if section not in sections and section in (*OPERATOR_SECTIONS, *PROJECT_SECTIONS):
            home = (f"{PROJECT_LABEL}（目标仓库 Git 根目录下的 .harness/project.toml）" if label == OPERATOR_LABEL
                    else f"{OPERATOR_LABEL}（--config 指定的文件）")
            raise HarnessError(f"{label} {path} 不能包含 {section}：它应写在{home}中")
        if section in sections and not isinstance(values, dict):
            raise HarnessError(f"{section} 必须为配置表（{label} {path}）")
    unknown = unknown_keys(data)
    limits = data.get("runtime", {}).get("claude_max_turns_by_role")
    if isinstance(limits, dict):
        unknown.extend(f"runtime.claude_max_turns_by_role.{role}" for role in limits if role not in DISPATCH_ROLES)
    if unknown:
        raise HarnessError("未知配置键：" + "、".join(sorted(unknown)) + f"（{label} {path}）")
    return data


def validate_commands(commands, field):
    if not isinstance(commands, list):
        raise HarnessError(f"{field} 必须使用 argv 数组列表")
    for command in commands:
        if (not isinstance(command, list) or not command
                or not all(isinstance(s, str) and "\0" not in s for s in command)
                or not command[0].strip()):
            raise HarnessError(f"{field} 必须使用非空 argv 数组")


@dataclass
class Config:
    path: Path
    data: dict
    # What load() read: each file's digest is of the very bytes it parsed. None when not loaded from files.
    sha256: str | None = None
    project_path: Path | None = None
    project_sha256: str | None = None

    @classmethod
    def load(cls, path, project_path=None):
        """Effective configuration: built-in defaults <- operator file <- project file (when given)."""
        path = Path(path).resolve()
        raw, digest = read_layer(path)
        data = parse_layer(raw, path, OPERATOR_LABEL, OPERATOR_SECTIONS)
        project = project_digest = None
        if project_path is not None:
            # Its place is fixed and shown as it is: a link there is followed when read, never displayed.
            project_path = Path(project_path).absolute()
            raw, project_digest = read_layer(project_path)
            project = parse_layer(raw, project_path, PROJECT_LABEL, PROJECT_SECTIONS)
        return cls(path, cls.effective(data, project), digest, project_path, project_digest)

    @classmethod
    def effective(cls, operator, project):
        """Built-in defaults <- operator layer <- project layer (None when there is none), checked by validate.

        The operator layer may already be an effective configuration, as doctor's is: defaults only fill
        what nothing set, so filling them before the project layer gives the same result as after it.
        """
        if project is not None:
            # runtime is replaced key by key, each value whole (tables and arrays are not merged);
            # verification and permissions come only from the project file.
            operator = {**operator, **project, "runtime": {**operator.get("runtime", {}), **project.get("runtime", {})}}
        data = normalized(operator)
        cls.validate(data)
        return data

    @staticmethod
    def validate(data):
        unknown = unknown_keys(data)
        if unknown:
            raise HarnessError("未知配置键：" + "、".join(sorted(unknown)))
        shared = data.get("shared")
        if not isinstance(shared, dict):
            raise HarnessError("shared 必须为配置表，包含 rules、skills、mcp")
        for key in ("rules", "skills", "mcp"):
            value = shared.get(key)
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                raise HarnessError(f"shared.{key} 必须为非空路径字符串")
        if not isinstance(data.get("roles"), dict):
            raise HarnessError("roles 必须为配置表")
        for name in ROLE_NAMES:
            role = data.get("roles", {}).get(name, {})
            if (not isinstance(role, dict) or role.get("cli") not in ("codex", "claude")
                    or not isinstance(role.get("model"), str) or not role["model"].strip()
                    or not isinstance(role.get("effort"), str) or not role["effort"].strip()):
                raise HarnessError(f"无效模型配置：{name}")
        if data["roles"]["worker"]["cli"] == "codex":
            raise HarnessError("当前 Codex 适配器仅支持只读角色，不能配置为 worker")
        for key in DEFAULTS["runtime"]:
            if key in ("claude_max_turns_by_role", QUOTA_WAIT_KEY):
                continue
            value = data["runtime"][key]
            if type(value) is not int or value <= 0:
                raise HarnessError(f"runtime.{key} 必须为正整数")
        value = data["runtime"][QUOTA_WAIT_KEY]
        if type(value) is not int or value < 0:
            raise HarnessError(f"runtime.{QUOTA_WAIT_KEY} 必须为非负整数；0 表示不等待")
        limits = data["runtime"].get("claude_max_turns_by_role", {})
        if not isinstance(limits, dict):
            raise HarnessError("runtime.claude_max_turns_by_role 必须为角色配置表")
        for role, value in limits.items():
            field = f"runtime.claude_max_turns_by_role.{role}"
            if role not in DISPATCH_ROLES:
                raise HarnessError(f"未知配置键：{field}；角色为 commander、worker、reviewer")
            if type(value) is not int or value <= 0:
                raise HarnessError(f"{field} 必须为正整数")
        for key in DEFAULTS["memory"]:
            if type(data["memory"][key]) is not int or data["memory"][key] < 0:
                raise HarnessError(f"memory.{key} 必须为非负整数；0 表示不设到期时间")
        verification = data.get("verification", {})
        if not isinstance(verification, dict):
            raise HarnessError("verification 必须为配置表")
        validate_commands(verification.get("commands", []), "verification.commands")
        if type(verification.get("fresh_checkout", False)) is not bool:
            raise HarnessError("verification.fresh_checkout 必须为布尔值")
        validate_commands(verification.get("setup_commands", []), "verification.setup_commands")
        if verification.get("setup_commands") and not verification.get("fresh_checkout"):
            raise HarnessError("verification.setup_commands 需要开启 fresh_checkout")
        validate_commands(data["permissions"]["worker_commands"], "permissions.worker_commands")

    def resume_changes(self, snapshot):
        """Return budget increases, exact command grants and quota wait changes without mutating inputs."""
        previous, current = copy.deepcopy(snapshot), normalized(self.data)
        if previous != normalized(previous):
            raise HarnessError('运行配置快照字段缺失；不补写旧版本默认值，请开始新 run')
        self.validate(previous)
        self.validate(current)
        changes = {}
        # Compare effective limits before removing the scalar fallback. A removed
        # override must not silently lower a role's budget during recovery.
        for role in DISPATCH_ROLES:
            old = claude_turn_limit(previous["runtime"], role)
            new = claude_turn_limit(current["runtime"], role)
            if new < old:
                raise HarnessError(f"配置已改变：恢复时 {role} 的 Claude 轮数上限只能保持或增大（{old} → {new}）")
        old_limits = previous["runtime"].pop("claude_max_turns_by_role")
        new_limits = current["runtime"].pop("claude_max_turns_by_role")
        if new_limits != old_limits:
            changes["claude_max_turns_by_role"] = {"old": old_limits, "new": new_limits}
        old, new = previous["runtime"].pop(QUOTA_WAIT_KEY), current["runtime"].pop(QUOTA_WAIT_KEY)
        if new != old:
            changes[QUOTA_WAIT_KEY] = {"old": old, "new": new}   # raised, lowered or disabled alike
        for key in RESUME_INCREASES:
            old, new = previous["runtime"].pop(key), current["runtime"].pop(key)
            if new < old:
                raise HarnessError(f"配置已改变：恢复时 runtime.{key} 只能保持或增大（{old} → {new}）")
            if new != old:
                changes[key] = {"old": old, "new": new}
        old_commands = previous["permissions"].pop("worker_commands")
        new_commands = current["permissions"].pop("worker_commands")
        remaining = Counter(tuple(command) for command in old_commands)
        added = []
        for command in new_commands:
            exact = tuple(command)
            if remaining[exact]:
                remaining[exact] -= 1
            else:
                added.append(command)
        if any(remaining.values()):
            raise HarnessError("配置已改变：恢复时 permissions.worker_commands 只允许增加精确 argv，不能删除或修改已有命令")
        if added:
            changes["worker_commands"] = {"old": old_commands, "new": new_commands, "added": added}
        if previous != current:
            raise HarnessError("配置已改变：恢复只允许增大超时、上下文、轮数、尝试与派发预算、追加精确 worker 命令"
                               "或调整额度等待上限；其他配置必须保持一致")
        return changes

    @staticmethod
    def readonly_commands():
        """Read-only command examples; append paths/patterns after their --."""
        return [list(command) for command in READONLY_COMMANDS]

    def claude_max_turns(self, role):
        return claude_turn_limit(self.data["runtime"], role)

    @property
    def root(self):
        return self.path.parent

    def asset(self, key):
        return (self.root / self.data["shared"][key]).resolve()

    def role(self, name, commander="astra"):
        key = "fable" if name == "commander" and commander == "fable" else name
        return self.data["roles"][key]
