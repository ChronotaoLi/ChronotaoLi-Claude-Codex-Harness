"""Read-only permission evidence; local grants never explain provider decisions."""
from .git_query import parse_git_query
from .stream_capture import permission_denial_projection
from .util import HarnessError

GIT_QUERY_TOOL = "harness.git_query"


def builtin_coverage(argv):
    """Built-in capability is reported apart from configured exact argv."""
    query, reason = parse_git_query(argv)
    if query is None:
        return {"tool": GIT_QUERY_TOOL, "covered": False, "reason": reason}
    return {"tool": GIT_QUERY_TOOL, "covered": True, "form": query["form"],
            "meaning": "capability_available_not_executed"}


def command_capability_projection(config, argv):
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(arg, str) and '\0' not in arg for arg in argv)
            or not argv[0].strip()):
        raise HarnessError('Permission diagnostic argv must be a nonempty argv string array')
    matches = []
    for section, key in (("verification", "commands"), ("permissions", "worker_commands")):
        if argv in config.data[section][key]:
            matches.append(section + "." + key)
    return {"local_exact_argv_match": bool(matches), "configured_sources": matches,
            "builtin_coverage": builtin_coverage(argv),
            "provider_permission_reason": "unavailable", "native_read_tools": ["Read", "Glob", "Grep"],
            "readonly_file_tools": ["harness.file_sha256", "harness.json_check", "harness.file_diff"],
            "readonly_git_tool": GIT_QUERY_TOOL,
            "command_broker": "harness_commands.run_command", "provider_allowance_verified": False}


def permission_diagnostics(config, provider_result=None, *, argv=None):
    """Project a private terminal plus optional exact local argv comparison.

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
    if argv is not None:
        result['local_command_capability'] = command_capability_projection(config, argv)
    return result
