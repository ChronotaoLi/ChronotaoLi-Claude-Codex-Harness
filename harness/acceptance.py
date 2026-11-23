"""Read and confirm the Git state bound to a saved acceptance intent."""
from __future__ import annotations

import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypedDict

from . import git, stops
from .util import HarnessError


class Intent(TypedDict):
    ref: str
    parent: str
    tree: str
    commit: str
    files: list[str]
    index_before: str
    index_synced: str


@dataclass(frozen=True)
class Ready:
    branch: str
    index: str


@dataclass(frozen=True)
class Rebuild:
    """The unpublished commit or tree is missing and the parent is still current."""


@contextmanager
def _reads():
    """Interpret Git observations, including binding reads around the guarded writes."""
    try:
        yield
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HarnessError(str(error), stop=stops.make("runtime_failure", "io")) from error
    except HarnessError as error:
        result = getattr(error, "binding_git_result", None)
        if result is not None:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise HarnessError(f"git symbolic-ref -q HEAD exited {result.returncode}: {detail}",
                               stop=stops.make("runtime_failure", "io")) from error
        if isinstance(error, git.GitLaunchError) or error.stop is None:
            error.stop = stops.make("runtime_failure", "io")
        raise


def _branch(repo, intent, binding):
    branch = git.branch_state(repo, intent["ref"], binding=binding)
    if branch.symbolic:
        raise HarnessError("接受分支已变为符号引用；保留现场",
                           stop=stops.make("workspace_mismatch", "binding_changed"))
    return branch.oid


def _head_changed(intent, task_id, *, include_commit):
    data = {"expected_head": intent["parent"]}
    if include_commit:
        data["accept_commit"] = intent["commit"]
    return HarnessError("接受分支与意图不符；保留现场", stop=stops.make(
        "workspace_mismatch", "head_changed", data=data, source={"task_id": task_id}))


def _candidate_changed(task_id):
    return HarnessError("真实暂存区与接受意图不符；保留现场", stop=stops.make(
        "workspace_mismatch", "candidate_changed", source={"task_id": task_id}))


def integrity(task_id):
    return HarnessError("接受意图与 Git 对象不符；保留现场", stop=stops.make(
        "evidence", "runtime_integrity", source={"task_id": task_id}))


def state(context, intent: Intent) -> Ready | Rebuild:
    """Evaluate the saved intent without writing Git or the ledger."""
    repo, binding = context.worktree, context.run["worktree_binding"]
    task_id = context.run["pending_acceptance"]["task_id"]
    parent, tree, commit = intent["parent"], intent["tree"], intent["commit"]
    unchanged = commit == parent
    with _reads():
        git.check_worktree_binding(repo, binding)
        branch = _branch(repo, intent, binding)
        if branch not in (parent, commit):
            raise _head_changed(intent, task_id, include_commit=not unchanged)

        kinds = git.object_kinds(repo, (parent, tree) if unchanged else (parent, tree, commit), binding=binding)
        if kinds[parent] != "commit" or kinds[commit] not in ("commit", None) or kinds[tree] not in ("tree", None):
            raise integrity(task_id)
        if branch == commit and (kinds[tree] is None or kinds[commit] is None):
            raise integrity(task_id)

        parent_header = git.commit_header(repo, parent, binding=binding)
        if unchanged and parent_header.tree != tree:
            raise integrity(task_id)
        if not unchanged:
            if kinds[commit] == "commit":
                header = git.commit_header(repo, commit, binding=binding)
                if header.tree != tree or header.parents != (parent,):
                    raise integrity(task_id)
            if kinds[commit] is None or kinds[tree] is None:
                return Rebuild()

        git.tree_readable(repo, tree, binding=binding)
        index = git.index_digest(repo, binding=binding)
        if index not in (intent["index_before"], intent["index_synced"]):
            raise _candidate_changed(task_id)
        return Ready(branch, index)


def _write_failure(command, result):
    detail = result.stderr.decode("utf-8", errors="replace").strip()
    return HarnessError(f"git {command} failed: {detail}", stop=stops.make("runtime_failure", "io"))


def move_branch(context, intent: Intent, task_id):
    repo, binding = context.worktree, context.run["worktree_binding"]
    with _reads():
        result = git.move_branch(repo, intent["ref"], intent["commit"], intent["parent"], task_id,
                                 binding=binding, lock_fd=context.run_lock_descriptor)
        if result.returncode:
            branch = _branch(repo, intent, binding)
            if branch == intent["parent"]:
                raise _write_failure("update-ref", result)
            if branch != intent["commit"]:
                raise _head_changed(intent, task_id, include_commit=True)


def sync_index(context, intent: Intent):
    with _reads():
        result = git.sync_index(context.worktree, intent["commit"], binding=context.run["worktree_binding"],
                                lock_fd=context.run_lock_descriptor)
        if result.returncode:
            raise _write_failure("read-tree --reset", result)


def confirm(context, intent: Intent, task_id):
    repo, binding = context.worktree, context.run["worktree_binding"]
    with _reads():
        git.check_worktree_binding(repo, binding)
        if _branch(repo, intent, binding) != intent["commit"]:
            raise _head_changed(intent, task_id, include_commit=True)
        if git.index_digest(repo, binding=binding) != intent["index_synced"]:
            raise _candidate_changed(task_id)
