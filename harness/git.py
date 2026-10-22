from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

from .util import HarnessError, write_json


PROTECTED_ROOTS = frozenset((".git", ".harness", ".codex", ".claude"))


def _git_environment(binding=None):
    # Ambient Git overrides must not redirect the pinned index, refs, or
    # object database. Keep ordinary process environment such as PATH.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    if binding is not None:
        env["GIT_COMMON_DIR"] = binding["common_dir"]
    return env


def _git_command(repo, args, binding=None):
    argv = ["git", "-C", str(repo)]
    if binding is not None:
        argv += ["--git-dir=" + binding["git_dir"], "--work-tree=" + binding["worktree"]]
    return argv + list(args)


def _run_git(repo, args, binding=None):
    try:
        return subprocess.run(_git_command(repo, args, binding), env=_git_environment(binding),
                              capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HarnessError(f"git {' '.join(args[:3])} failed: {error}") from error


def git_process(repo: Path, *args: str, binding=None, timeout=60):
    """Run a pinned command, preserving exit status for provenance checks."""
    if binding is not None:
        check_worktree_binding(repo, binding)
    p = subprocess.run(_git_command(repo, args, binding), env=_git_environment(binding),
                       capture_output=True, timeout=timeout)
    if binding is not None:
        check_worktree_binding(repo, binding)
    return p


def git_bytes(repo: Path, *args: str, binding=None) -> bytes:
    try:
        p = git_process(repo, *args, binding=binding)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HarnessError(f"git {' '.join(args[:3])} failed: {error}") from error
    if p.returncode:
        raise HarnessError(f"git {' '.join(args[:3])} failed: {p.stderr.decode('utf-8', errors='replace').strip()}")
    return p.stdout


def git(repo: Path, *args: str, binding=None) -> str:
    """Keep filesystem bytes reversible; use diff() for a display-safe preview."""
    return os.fsdecode(git_bytes(repo, *args, binding=binding))


def _binding_file(path):
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise HarnessError(f"worktree Git 绑定文件必须为普通文件：{path}")
        return path.read_bytes()
    except OSError as error:
        raise HarnessError(f"worktree Git 绑定文件无法读取：{path}：{error}") from error


def _binding_path(parent, data):
    # Git's administrative paths are newline-terminated filesystem bytes.
    value = Path(os.fsdecode(data.removesuffix(b"\n")))
    return (parent / value).resolve()


def capture_worktree_binding(worktree: Path, expected_branch: str | None):
    """Persist the newly created linked worktree's actual Git identity.

    A detached binding also pins its commit, for disposable verification
    checkouts. Run branches may advance only on the captured symbolic ref.
    """
    worktree = worktree.resolve()
    gitfile = _binding_file(worktree / ".git")
    git_dir = Path(git(worktree, "rev-parse", "--absolute-git-dir").removesuffix("\n")).resolve()
    common_dir = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
                      .removesuffix("\n")).resolve()
    if git_dir == common_dir:
        raise HarnessError("只能绑定独立的链接 worktree，不能使用主仓库 Git 目录")
    branch = (expected_branch if expected_branch.startswith("refs/heads/") else "refs/heads/" + expected_branch
              ) if expected_branch is not None else None
    binding = {"worktree": str(worktree), "git_dir": str(git_dir), "common_dir": str(common_dir),
               "branch": branch, "gitfile_sha256": hashlib.sha256(gitfile).hexdigest(),
               "backlink_sha256": hashlib.sha256(_binding_file(git_dir / "gitdir")).hexdigest(),
               "commondir_sha256": hashlib.sha256(_binding_file(git_dir / "commondir")).hexdigest()}
    if branch is None:
        binding["detached_head"] = git(worktree, "rev-parse", "HEAD").strip()
    check_worktree_binding(worktree, binding)
    return binding


def check_worktree_binding(worktree: Path, binding):
    """Refuse redirected metadata before any run-worktree Git command."""
    fields = ("worktree", "git_dir", "common_dir", "gitfile_sha256", "backlink_sha256", "commondir_sha256")
    if (not isinstance(binding, dict) or any(not isinstance(binding.get(key), str) for key in fields)
            or "branch" not in binding
            or binding["branch"] is not None and not isinstance(binding["branch"], str)
            or binding.get("branch") is None and not isinstance(binding.get("detached_head"), str)):
        raise HarnessError("worktree Git 绑定缺失或无效；保留现场，不能重新推断绑定")
    worktree = worktree.resolve()
    git_dir, common_dir = Path(binding["git_dir"]), Path(binding["common_dir"])
    if (str(worktree) != binding["worktree"] or git_dir.resolve() != git_dir
            or common_dir.resolve() != common_dir or git_dir == common_dir
            or not git_dir.is_dir() or not common_dir.is_dir()):
        raise HarnessError("worktree Git 绑定目录已改变；保留现场")
    gitfile = _binding_file(worktree / ".git")
    if (hashlib.sha256(gitfile).hexdigest() != binding["gitfile_sha256"]
            or not gitfile.startswith(b"gitdir: ")
            or _binding_path(worktree, gitfile[len(b"gitdir: "):]) != git_dir):
        raise HarnessError("worktree .git 绑定已改变；保留现场")
    backlink = _binding_file(git_dir / "gitdir")
    common = _binding_file(git_dir / "commondir")
    if (hashlib.sha256(backlink).hexdigest() != binding["backlink_sha256"]
            or _binding_path(git_dir, backlink) != worktree / ".git"
            or hashlib.sha256(common).hexdigest() != binding["commondir_sha256"]
            or _binding_path(git_dir, common) != common_dir):
        raise HarnessError("worktree Git 反向或公共目录绑定已改变；保留现场")
    _binding_file(git_dir / "HEAD")
    # symbolic-ref follows nested refs as Git commit does. Reading only HEAD
    # would miss a run branch changed into a symbolic ref pointing to main.
    result = _run_git(worktree, ("symbolic-ref", "-q", "HEAD"), binding)
    if binding["branch"] is not None:
        if result.returncode or os.fsdecode(result.stdout).strip() != binding["branch"]:
            raise HarnessError("worktree Git 分支绑定已改变；保留现场")
    elif result.returncode != 1 or _binding_file(git_dir / "HEAD").strip().decode("ascii", errors="replace") != binding["detached_head"]:
        raise HarnessError("验证 worktree 的 detached HEAD 绑定已改变；保留现场")


def repository(path: Path):
    return Path(git(path, "rev-parse", "--show-toplevel").removesuffix("\n")).resolve()


def head(repo: Path, *, binding=None):
    return git(repo, "rev-parse", "HEAD", binding=binding).strip()


def ignore_state(repo: Path):
    """Local Git exclude avoids requiring changes to a target project's tracked files."""
    common = Path(git(repo, "rev-parse", "--git-common-dir").removesuffix("\n"))
    if not common.is_absolute():
        common = repo / common
    path = common / "info" / "exclude"
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_text(encoding="utf-8") if path.exists() else ""
    if "/.harness/" not in previous.splitlines():
        path.write_text(previous.rstrip() + "\n/.harness/\n", encoding="utf-8")


def clean(repo: Path, *, binding=None):
    return not git(repo, "status", "--porcelain", binding=binding).strip()


def reject_submodules(repo: Path, *, binding=None):
    """Reject indexed gitlinks, including uninitialized or missing submodules."""
    _reject_indexed_submodules(git_bytes(repo, "ls-files", "--stage", "-z", binding=binding))


def _reject_indexed_submodules(index):
    for entry in index.split(b"\0"):
        if entry.startswith(b"160000 "):
            name = os.fsdecode(entry.partition(b"\t")[2])
            raise HarnessError(f"v0.1 暂不支持 submodule：{name}")


def check_worktree_state(repo: Path, *, binding=None):
    """A run worktree must never contain the main repository's private state.

    The shared Git exclude hides this root from normal change enumeration.
    Only call this on run worktrees; the main repository owns .harness itself.
    """
    if binding is not None:
        check_worktree_binding(repo, binding)
    private = repo / ".harness"
    if private.exists() or private.is_symlink():
        raise HarnessError("任务修改了受保护路径：.harness")


def assert_removable_worktree(repo: Path, *, binding=None):
    """Refuse removal unless all tracked bytes match HEAD and no data is hidden.

    Git status may trust cached stat fields or index flags. Hash the actual
    bytes with Git's blob framing, without clean filters or external programs.
    This intentionally refuses transformed checkouts (for example autocrlf)
    when their bytes differ from the saved blobs.
    """
    for entry in git_bytes(repo, "ls-files", "-v", "-z", binding=binding).split(b"\0"):
        if entry[:1].islower() or entry.startswith(b"S "):
            raise HarnessError("worktree 索引含 assume-unchanged/skip-worktree 标记；"
                               f"请先核对并清除，不能移除：{os.fsdecode(entry[2:])}")
    untracked = [os.fsdecode(name) for name in
                 git_bytes(repo, "ls-files", "--others", "-z", binding=binding).split(b"\0") if name]
    if untracked:
        shown = [repr(name) for name in untracked[:10]]
        paths = ", ".join(name if len(name) <= 240 else name[:240] + "…" for name in shown)
        remaining = f"；另有 {len(untracked) - 10} 项未列出" if len(untracked) > 10 else ""
        raise HarnessError("worktree 有未保存文件（包括忽略文件）；请先归档或清理，不能移除："
                           f"{paths}{remaining}")
    tree = {}
    for entry in git_bytes(repo, "ls-tree", "-r", "-z", "HEAD", binding=binding).split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        if kind != b"blob" or mode not in (b"100644", b"100755", b"120000"):
            raise HarnessError(f"worktree 含 submodule 或不支持的文件类型，不能移除：{os.fsdecode(name)}")
        tree[name] = (mode, object_id)
    index = {}
    for entry in git_bytes(repo, "ls-files", "--stage", "-z", binding=binding).split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, object_id, stage = metadata.split()
        if stage != b"0":
            raise HarnessError("worktree 索引含未解决的冲突，不能移除")
        index[name] = (mode, object_id)
    if index != tree:
        raise HarnessError("worktree 索引与 HEAD 不同；请先保存或归档，不能移除")
    object_format = git(repo, "rev-parse", "--show-object-format", binding=binding).strip()
    if object_format not in ("sha1", "sha256"):
        raise HarnessError(f"不支持的 Git 对象格式，不能移除：{object_format}")
    for name, (mode, object_id) in tree.items():
        path = repo / os.fsdecode(name)
        for parent in path.parents:
            if parent == repo:
                break
            if parent.is_symlink():
                raise HarnessError(f"worktree 路径有符号链接祖先，不能移除：{os.fsdecode(name)}")
        try:
            metadata = path.lstat()
            if mode == b"120000":
                if not stat.S_ISLNK(metadata.st_mode):
                    raise HarnessError(f"worktree 文件类型与 HEAD 不同，不能移除：{os.fsdecode(name)}")
                data = os.fsencode(os.readlink(path))
            else:
                if (not stat.S_ISREG(metadata.st_mode)
                        or bool(metadata.st_mode & 0o111) != (mode == b"100755")):
                    raise HarnessError(f"worktree 文件类型或执行位与 HEAD 不同，不能移除：{os.fsdecode(name)}")
                data = path.read_bytes()
        except OSError as error:
            raise HarnessError(f"worktree 已跟踪文件无法读取，不能移除：{os.fsdecode(name)}：{error}") from error
        digest = hashlib.new(object_format, b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
        if digest.encode("ascii") != object_id:
            raise HarnessError(f"worktree 文件内容与 HEAD 不同；请先保存或归档，不能移除：{os.fsdecode(name)}")


def snapshot(repo: Path, *, binding=None):
    index = git_bytes(repo, "ls-files", "--stage", "-z", binding=binding)
    _reject_indexed_submodules(index)
    result = {"head": head(repo, binding=binding), "index": hashlib.sha256(index).hexdigest(),
              "files": {}}
    names = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard", binding=binding).split("\0")
    for name in sorted(set(names) - {""}):
        path = repo / name
        if path.is_symlink():
            data = os.fsencode(os.readlink(path))
            mode = "link"
        elif path.is_file():
            data = path.read_bytes()
            mode = str(path.stat().st_mode & 0o777)
        elif path.is_dir():
            # A tracked file replaced by a directory is a deletion of that
            # file; Git enumerates the new descendants separately. Gitlinks
            # are rejected from index modes above, including newly staged ones.
            data, mode = b"", "deleted"
        else:
            data, mode = b"", "deleted"
        result["files"][name] = mode + ":" + hashlib.sha256(data).hexdigest()
    return result


def snapshot_changes(before, after):
    """Describe a failed equality gate without relaxing its content checks."""
    old = {name: value for name, value in before["files"].items() if not value.startswith("deleted:")}
    new = {name: value for name, value in after["files"].items() if not value.startswith("deleted:")}
    return {
        "added": sorted(new.keys() - old.keys()),
        "modified": sorted(name for name in old.keys() & new.keys() if old[name] != new[name]),
        "deleted": sorted(old.keys() - new.keys()),
        "index_changed": before["index"] != after["index"],
        "head_changed": before["head"] != after["head"],
    }


def changed(repo: Path, *, binding=None):
    tracked = git(repo, "diff", "--name-only", "--no-renames", "-z", "HEAD", binding=binding).split("\0")
    staged = git(repo, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD", binding=binding).split("\0")
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z", binding=binding).split("\0")
    return sorted((set(tracked) | set(staged) | set(untracked)) - {""})


def validate_scope_pattern(pattern: str):
    """Require a root-relative pattern without changing literal filename bytes."""
    parts = pattern.split("/")
    if not pattern.strip() or "\0" in pattern or any(part in ("", ".", "..") for part in parts):
        raise HarnessError(f"无效文件范围：{pattern}")
    if parts[0] in PROTECTED_ROOTS:
        raise HarnessError(f"文件范围包含受保护路径：{pattern}")


def repository_wide_scope(pattern: str) -> bool:
    """Recognize common whole-repository globs without inferring glob containment."""
    parts = pattern.split("/")
    return "**" in parts and all(part in ("*", "**") for part in parts) and parts.count("*") <= 1


def path_matches(name: str, pattern: str) -> bool:
    """Match from the repository root; only a full ** segment crosses /.

    Other segments use shell-style *, ?, and [] character classes. A **
    segment matches zero or more path segments, including at the root.
    """
    names = name.split("/")
    positions = {0}
    for part in pattern.split("/"):
        if part == "**":
            positions = set(range(min(positions), len(names) + 1))
        else:
            positions = {index + 1 for index in positions
                         if index < len(names) and fnmatchcase(names[index], part)}
        if not positions:
            return False
    return len(names) in positions


def check_scope(repo: Path, allowed: list[str], *, binding=None):
    for pattern in allowed:
        validate_scope_pattern(pattern)
    check_worktree_state(repo, binding=binding)
    paths = changed(repo, binding=binding)
    for path in paths:
        if path.split("/", 1)[0] in PROTECTED_ROOTS:
            raise HarnessError(f"任务修改了受保护路径：{path}")
        if not any(path_matches(path, pattern) for pattern in allowed):
            raise HarnessError(f"任务越界修改：{path}；允许范围：{allowed}")
    return paths


def diff_bytes(repo: Path, *, binding=None):
    """Lossless Git evidence, including untracked files and non-UTF-8 content."""
    value = git_bytes(repo, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames", "HEAD", binding=binding)
    for name in git(repo, "ls-files", "--others", "--exclude-standard", "-z", binding=binding).split("\0"):
        if not name:
            continue
        if binding is not None:
            check_worktree_binding(repo, binding)
        p = _run_git(repo, ("diff", "--no-index", "--binary", "--no-ext-diff", "--no-textconv",
                            "--", "/dev/null", name), binding)
        if binding is not None:
            check_worktree_binding(repo, binding)
        if p.returncode not in (0, 1):
            raise HarnessError(p.stderr.decode("utf-8", errors="replace"))
        value += p.stdout
    return value


def diff(repo: Path, *, binding=None):
    return diff_bytes(repo, binding=binding).decode("utf-8", errors="replace")


def range_diff_bytes(repo: Path, base: str, *, binding=None):
    """The whole committed result, independent of task boundaries."""
    return git_bytes(repo, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames", base, "HEAD", "--", binding=binding)


def range_diff(repo: Path, base: str, *, binding=None):
    return range_diff_bytes(repo, base, binding=binding).decode("utf-8", errors="replace")


def archive_and_reset(repo: Path, expected_head: str, archive: Path, owned_root: Path, *, binding=None):
    """Archive rejected edits before restoring an owned run worktree checkpoint."""
    repo = repo.resolve()
    if not repo.is_relative_to(owned_root.resolve()) or not (repo / ".git").is_file():
        raise HarnessError("只能重置 Harness 所有的隔离 worktree")
    if head(repo, binding=binding) != expected_head:
        raise HarnessError("重置前 HEAD 与检查点不同；保留现场")
    paths = changed(repo, binding=binding)
    # Refuse obstructed tracked descendants instead of following a replacement
    # symlink out of the worktree while archiving or creating such a link in it.
    for name in paths:
        for parent in (repo / name).parents:
            if parent == repo:
                break
            if parent.is_symlink():
                raise HarnessError(f"归档路径有符号链接祖先；保留现场，不重置：{name}")
    before = snapshot(repo, binding=binding)
    archive.mkdir(parents=True, exist_ok=False)
    (archive / "change.diff").write_bytes(diff_bytes(repo, binding=binding))
    (archive / "staged.diff").write_bytes(git_bytes(repo, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", binding=binding))
    write_json(archive / "snapshot.json", before)
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z", binding=binding).split("\0")
    for name in paths:
        source, target = repo / name, archive / "files" / name
        if source.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.readlink(source))
        elif source.is_file():
            # Deleted descendants may now lie beneath a replacement file.
            # Do not create archive directories for paths with no bytes left.
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    if snapshot(repo, binding=binding) != before:
        raise HarnessError("归档期间文件发生变化；保留现场，不重置")
    git(repo, "reset", "--hard", expected_head, binding=binding)
    # Remove only the recorded, untracked files, never ignored files or arbitrary directories.
    for name in filter(None, untracked):
        path = repo / name
        if path.is_symlink() or path.is_file():
            path.unlink()
    if not clean(repo, binding=binding) or head(repo, binding=binding) != expected_head:
        raise HarnessError("重置未恢复到干净检查点；归档已保留")
    write_json(archive / "restored.json", {"head": expected_head, "paths": paths})
    return str(archive)


def accept(repo: Path, paths: list[str], run_id: str, task_id: str, *, binding=None):
    # The worker does not own the acceptance index. Rebuild it from the reviewed
    # worktree so hidden staged content can never enter the runtime's commit.
    git(repo, "reset", "--mixed", "HEAD", binding=binding)
    tracked = set(git(repo, "ls-files", "-z", binding=binding).split("\0"))
    # A worker may stage a new file and then delete it. Reset already removes
    # that index-only addition; git add cannot match it afterward. Keep real
    # tracked deletions and dangling symlinks in the reviewed acceptance set.
    stage = [name for name in paths
             if name in tracked or (repo / name).exists() or (repo / name).is_symlink()]
    if stage:
        git(repo, "--literal-pathspecs", "add", "--", *stage, binding=binding)
    if git(repo, "diff", "--cached", "--name-only", "HEAD", binding=binding).strip():
        git(repo, "-c", "user.name=Personal Harness", "-c", "user.email=harness@localhost",
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-m",
            f"harness: {task_id}\n\nHarness-Run: {run_id}\nHarness-Task: {task_id}", binding=binding)
    return head(repo, binding=binding)
