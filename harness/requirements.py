"""Exact task execution prerequisites, distinct from operator command grants."""
from copy import deepcopy

from .contracts import validate_required_commands
from .git_query import covers as git_query_covers


def missing_commands(tasks, accepted, config):
    allowed = {tuple(argv) for argv in (
        config["verification"]["commands"] + config["permissions"]["worker_commands"])}
    missing = {}
    for task in tasks:
        commands = validate_required_commands(task)
        if task["id"] in accepted:
            continue
        # Exact table forms are available through the built-in read-only
        # harness.git_query tool; that is capability, not a command grant.
        unmet = [deepcopy(argv) for argv in commands
                 if tuple(argv) not in allowed and not git_query_covers(argv)]
        if unmet:
            missing[task["id"]] = unmet
    return missing
