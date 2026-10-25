"""Exact task execution prerequisites, distinct from operator command grants."""
from copy import deepcopy

from .contracts import validate_required_commands


def missing_commands(tasks, accepted, config):
    allowed = {tuple(argv) for argv in (
        config["verification"]["commands"] + config["permissions"]["worker_commands"])}
    missing = {}
    for task in tasks:
        commands = validate_required_commands(task)
        if task["id"] in accepted:
            continue
        unmet = [deepcopy(argv) for argv in commands if tuple(argv) not in allowed]
        if unmet:
            missing[task["id"]] = unmet
    return missing
