"""Compose the acceptance Git primitives for tests that do not drive a run."""
from harness import git


def acceptance_checkout(repo, path):
    branch = "harness/fixture-acceptance"
    git.git(repo, "worktree", "add", "-b", branch, str(path))
    return git.capture_worktree_binding(path, branch)


def accept_now(repo, paths, run_id, task_id, message, *, binding):
    parent = git.head(repo, binding=binding)
    built = git.build_commit(repo, parent, paths, ["**"], task_id,
                             git.commit_message_text(message, run_id, task_id), binding=binding)
    if built.commit != parent:
        git._require_git_result(git.move_branch(repo, binding["branch"], built.commit, parent, task_id, binding=binding), 0)
    if git.index_digest(repo, binding=binding) != built.index_synced:
        git._require_git_result(git.sync_index(repo, built.commit, binding=binding), 0)
    return built.commit
