"""Read-only permission evidence; local grants never explain provider decisions."""
from .commands import command_key, validate_command
from .git_query import parse_git_query
from .diagnostics import permission_denial_projection
from .util import HarnessError

GIT_QUERY_TOOL = "harness.git_query"


def builtin_coverage(command):
    """Built-in capability is reported apart from configured exact commands."""
    if command["cwd"] != ".":
        return {"tool": GIT_QUERY_TOOL, "covered": False, "reason": "git_query runs only at the worktree root"}
    query, reason = parse_git_query(command["argv"])
    if query is None:
        return {"tool": GIT_QUERY_TOOL, "covered": False, "reason": reason}
    return {"tool": GIT_QUERY_TOOL, "covered": True, "form": query["form"],
            "meaning": "capability_available_not_executed"}


def command_capability_projection(config, command):
    try:
        validate_command(command, "--command-json")
    except HarnessError as error:
        raise HarnessError(f"Permission diagnostic command must be {{cwd, argv}}: {error}") from error
    matches = []
    for section, key in (("verification", "commands"), ("permissions", "worker_commands")):
        if any(command_key(grant) == command_key(command) for grant in config.data[section][key]):
            matches.append(section + "." + key)
    return {"local_exact_command_match": bool(matches), "configured_sources": matches,
            "builtin_coverage": builtin_coverage(command),
            "provider_permission_reason": "unavailable", "native_read_tools": ["Read", "Glob", "Grep"],
            "readonly_file_tools": ["harness.file_sha256", "harness.json_check", "harness.file_diff"],
            "readonly_git_tool": GIT_QUERY_TOOL,
            "command_broker": "harness_commands.run_command", "provider_allowance_verified": False}


def permission_diagnostics(config, provider_result=None):
    """Project a private terminal; the CLI adds an exact local command comparison when one is given.

    Absence of a denial is not evidence of permission. Neither an exact local
    grant nor its absence establishes why a provider accepted or denied a tool.
    """
    denials = permission_denial_projection(provider_result)
    result = {"provider_denials": denials, "reported_denial_count": len(denials),
              "provider_denial_reason": "unavailable",
              "native_read_tools": ["Read", "Glob", "Grep"],
              "readonly_file_tools": ["harness.file_sha256", "harness.json_check", "harness.file_diff"],
              "readonly_git_tool": GIT_QUERY_TOOL,
              "command_broker": ("harness_commands.list_commands / harness_commands.run_command / "
                                 "harness_commands.delete_path (worker with a task scope)"),
              "provider_allowance_verified": False}
    return result
