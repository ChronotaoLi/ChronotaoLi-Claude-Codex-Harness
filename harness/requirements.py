"""Exact task execution prerequisites, distinct from operator command grants."""
from copy import deepcopy

from .commands import available_through
from .contracts import validate_required_commands


def missing_commands(tasks, accepted, config):
    missing = {}
    for task in tasks:
        commands = validate_required_commands(task)
        if task["id"] in accepted:
            continue
        unmet = [deepcopy(command) for command in commands if available_through(command, config) is None]
        if unmet:
            missing[task["id"]] = unmet
    return missing
