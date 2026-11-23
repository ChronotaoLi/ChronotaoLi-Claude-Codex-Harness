from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections import Counter
from fnmatch import fnmatchcase
from pathlib import Path
from typing import NamedTuple

from . import stops
from .util import DIRECTORY_FLAGS, HarnessError, fold_name, with_descriptors, write_json


PROTECTED_ROOTS = frozenset((".git", ".harness", ".codex", ".claude"))
_PROTECTED_FOLDED = frozenset(map(fold_name, PROTECTED_ROOTS))


def protected_name(segment: str) -> bool:
    """Whether a path segment names a protected root in any spelling that may alias it (fold_name)."""
    return fold_name(segment) in _PROTECTED_FOLDED


class Effect(NamedTuple):
    """The most one Git call may do; a call site declares one of the constants below."""
    name: str
    real_index: bool  # changes the entries of a real index (a worktree's, a fixture repository's)
    refs: bool        # creates, moves or deletes a ref, HEAD, ORIG_HEAD, a reflog or a worktree registration
    worktree: bool    # writes files in a worktree
    filters: bool     # may run clean, smudge or process filters

    @property
    def interruptible(self) -> bool:
        return not (self.real_index or self.refs or self.worktree)


READ = Effect("READ", False, False, False, False)
COMPARE = Effect("COMPARE", False, False, False, True)
PRIVATE = Effect("PRIVATE", False, False, False, True)
INDEX = Effect("INDEX", True, False, False, True)
REFS = Effect("REFS", False, True, False, False)
INDEX_REFS = Effect("INDEX_REFS", True, True, False, True)
CHECKOUT = Effect("CHECKOUT", True, True, True, True)

_read_only_scopes = set()
_read_only_lock = threading.Lock()


class ReadOnlyScope:
    """An independently owned layer, prepared before its cleanup is registered."""

    def acquire(self):
        from .process_runtime import defer_cleanup_signals

        with defer_cleanup_signals(inherit=False):
            with _read_only_lock:
                _read_only_scopes.add(self)

    def close(self):
        from .process_runtime import defer_cleanup_signals

        # A retry, or cleanup before acquisition, can only affect this layer.
        # Each holder acquires and closes its own token.
        if self not in _read_only_scopes:
            return
        with defer_cleanup_signals(inherit=False):
            with _read_only_lock:
                _read_only_scopes.discard(self)


class _ReadOnlyContext:
    def __init__(self):
        self._scope = ReadOnlyScope()
        self._entered = False

    def __enter__(self):
        if self._entered:
            raise RuntimeError("A Git read-only context can only be entered once")
        self._entered = True
        try:
            self._scope.acquire()
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, traceback):
        # A retained context can retry even if an interrupt prevented the first
        # call to __exit__ or arrived before __enter__ returned to its caller.
        try:
            self._scope.close()
        finally:
            self._scope.close()


def read_only():
    """Require explicit READ effects throughout the process, including server threads."""
    return _ReadOnlyContext()


def _read_only_depth():
    with _read_only_lock:
        return len(_read_only_scopes)


def _check_read_only(effect):
    refused = effect is not READ and _read_only_depth() > 0
    if refused:
        raise HarnessError("Git 只读作用域只允许声明为 READ 的调用",
                           stop=stops.make("runtime_failure", "invariant"))


# githooks(5) of Git 2.56. Git without per-event switches ignores these keys; the hook name read covers it.
HOOK_EVENTS = ("applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit", "pre-merge-commit",
               "prepare-commit-msg", "commit-msg", "post-commit", "pre-rebase", "post-checkout", "post-merge",
               "pre-push", "pre-receive", "update", "proc-receive", "post-receive", "post-update",
               "reference-transaction", "push-to-checkout", "pre-auto-gc", "post-rewrite", "sendemail-validate",
               "fsmonitor-watchman", "p4-changelist", "p4-prepare-changelist", "p4-post-changelist",
               "p4-pre-submit", "post-index-change")
# Only these choose which configuration files Git reads; every other GIT_* could redirect the
# repository, index, objects or refs, or set single configuration keys.
_KEPT_ENVIRONMENT = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")
# An empty GIT_ALLOW_PROTOCOL also overrides a repository's protocol.<name>.allow=always; Trace2 needs "0".
_FIXED_ENVIRONMENT = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
                      "GIT_ALLOW_PROTOCOL": "", "GIT_NO_REPLACE_OBJECTS": "1",
                      "GIT_TRACE2": "0", "GIT_TRACE2_EVENT": "0", "GIT_TRACE2_PERF": "0"}


def _fixed_options():
    name, email = COMMIT_IDENTITY
    return ["--no-pager", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
            *(item for event in HOOK_EVENTS for item in ("-c", f"hook.{event}.enabled=false")),
            "-c", "protocol.allow=never", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
            "-c", "color.ui=false", "-c", "color.diff=false", "-c", "log.showSignature=false",
            *(item for role in ("user", "author", "committer")
              for item in ("-c", f"{role}.name={name}", "-c", f"{role}.email={email}")),
            "-c", "commit.gpgsign=false", "-c", "i18n.commitEncoding=UTF-8", "-c", "core.splitIndex=false"]


def invocation(repo, args, *, effect, deadline, binding=None, index_file=None, literal_pathspecs=False,
               isolated_config=False, _config=None, _c_locale=False):
    """The argv and environment of one Harness Git call; ``args`` are passed through unchanged.

    ``_config`` is None or _RANGE_DIFF_CONFIG itself, whose settings follow the fixed ones as ``-c``:
    harness.git's own, which only _range_diff passes (R1); a caller sets no other key.
    A declared call that is not READ first reads the configured hook names in the
    very context of the call (same prefix and environment) and disables each one,
    within ``deadline`` (a time.monotonic() value), which the call itself then shares.
    ``init`` makes ``repo/.git`` rather than finding a repository, so its name read
    chooses its own configuration context, pinned to that directory (_configured_hooks).
    The rollback listing requests ``_c_locale`` to recognize Git's unsupported -z diagnostic.
    """
    _check_read_only(effect)
    _check_config(_config)
    _check_index_file(repo, binding, effect, index_file)
    prefix = ["git", "-C", str(repo)]
    if binding is not None:
        prefix += ["--git-dir=" + binding["git_dir"], "--work-tree=" + binding["worktree"]]
    prefix += _fixed_options()
    if _config is _RANGE_DIFF_CONFIG:  # the module's tuple, never the caller's object
        prefix += [item for setting in _RANGE_DIFF_CONFIG for item in ("-c", setting)]
    if literal_pathspecs:
        prefix.append("--literal-pathspecs")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") or key in _KEPT_ENVIRONMENT}
    env.update(_FIXED_ENVIRONMENT)
    if _c_locale:
        env["LC_ALL"] = "C"
    if isolated_config:
        env.pop("GIT_CONFIG_SYSTEM", None)
        # Git 2.31 finds global config via these variables and ignores GIT_CONFIG_GLOBAL.
        env.pop("HOME", None)
        env.pop("XDG_CONFIG_HOME", None)
        env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    if binding is not None:
        env["GIT_COMMON_DIR"] = binding["common_dir"]
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    if effect is not None and effect is not READ:
        # Configured hooks (Git 2.54+) ignore core.hooksPath, and Git 2.54 has no per-event switch.
        names = _configured_hooks(repo, prefix, env, args, deadline)
        for number, name in enumerate(names):
            env[f"GIT_CONFIG_KEY_{number}"] = f"hook.{name}.enabled"
            env[f"GIT_CONFIG_VALUE_{number}"] = "false"
        if names:
            env["GIT_CONFIG_COUNT"] = str(len(names))
    return prefix + list(args), env


def _check_config(settings):
    """Refuse, before any process starts, anything but None and harness.git's fixed set for the whole-run comparison.

    By identity: an object equal to either may still iterate to other settings.
    """
    if settings is not None and settings is not _RANGE_DIFF_CONFIG:
        raise HarnessError(f"Git 调用只能附加 harness.git 为整体比较固定的设置：{settings!r}",
                           stop=stops.make("runtime_failure", "invariant"))


def _check_index_file(repo, binding, effect, index_file):
    """Refuse, before any process starts, an ``index_file`` that is not this call's private index."""
    if index_file is not None and (effect not in (READ, PRIVATE) or not _private_index(repo, binding, index_file)):
        raise HarnessError(f"Git 临时索引必须是工作树与 Git 目录之外、不是真实索引的绝对路径，且只用于只读或私有调用：{index_file}",
                           stop=stops.make("runtime_failure", "invariant"))


def _private_index(repo, binding, index_file):
    """Whether ``index_file`` can be this call's private index.

    It is absolute, is no real index (the one in its Git directory, or the main
    checkout's in its common directory), and lies neither in the call's worktree nor
    in its Git or common directory: the binding's, or unbound, those ``repo/.git`` names.
    Compared by file identity, not spelling: on a case-insensitive file system, or
    through a firmlink, another spelling names the same directory. Anything that
    cannot be checked is refused.
    """
    index = Path(index_file)
    if not index.is_absolute():
        return False  # it would resolve against -C, inside the worktree
    try:
        if binding is not None:
            worktree, git_dir, common_dir = binding["worktree"], binding["git_dir"], binding["common_dir"]
        else:
            worktree, (git_dir, common_dir) = repo, _git_directories(Path(repo))
        reals = {Path(git_dir) / "index", Path(common_dir) / "index"}
        closed = {(info.st_dev, info.st_ino) for info in map(os.stat, (worktree, git_dir, common_dir))}
        index = index.resolve()
        for path in (index, *index.parents):
            try:
                info = os.stat(path)
            except (FileNotFoundError, NotADirectoryError):
                continue
            if (info.st_dev, info.st_ino) in closed:
                return False
        for real in reals:
            try:
                if os.path.samefile(index, real):
                    return False
            except (FileNotFoundError, NotADirectoryError):
                pass
        return True
    # RuntimeError: a symbolic link loop on Python 3.11; ValueError: a NUL byte in a path; HarnessError: no
    # readable gitfile; KeyError, TypeError: a malformed binding (git_process checks before the binding check).
    except (OSError, RuntimeError, ValueError, HarnessError, KeyError, TypeError):
        return False


def _git_directories(repo):
    """The Git and common directory that ``git -C repo`` without a binding uses, read as Git reads them.

    ``repo/.git`` is the Git directory or, in a linked checkout, a gitfile naming it;
    a ``commondir`` file there names the common directory. Without ``repo/.git`` Git
    would search further up, so this raises and the call is refused. A limit: a
    ``repo/.git`` directory Git does not accept (an empty one) is still taken here,
    while Git searches further up and may use a parent repository's index.
    """
    dot = repo / ".git"
    if dot.is_dir():
        git_dir = dot.resolve()
    else:
        content = _binding_file(dot)
        if not content.startswith(b"gitdir: "):
            raise ValueError(f"not a gitfile: {dot}")
        git_dir = _binding_path(repo, content[len(b"gitdir: "):].rstrip(b"\r\n"))  # Git trims CR and LF
    commondir = git_dir / "commondir"
    if not os.path.lexists(commondir):
        return git_dir, git_dir
    return git_dir, _binding_path(git_dir, _binding_file(commondir).rstrip(b"\r\n"))


def _configured_hooks(repo, prefix, env, args, deadline):
    """The names of the configured hooks visible to the call that ``prefix`` and ``env`` start."""
    if args and args[0] == "init":
        # init makes repo/.git instead of finding a repository further up, so the read is pinned to that directory:
        # Git reads its configuration if it is a repository already, else only the user and system files (none with
        # isolated_config), and never searches upwards. Right after -C, so a binding's --git-dir would still win.
        prefix = [*prefix[:3], "--git-dir=.git", *prefix[3:]]
    read = _launch(prefix + ["config", "-z", "--name-only", "--get-regexp", r"^hook\..*\.command$"], env,
                   effect=READ, input=None, deadline=deadline)
    if read.returncode == 1:
        return []
    if read.returncode:
        raise HarnessError(f"git {' '.join(args[:3])} failed: hook 名字读取失败："
                           f"{read.stderr.decode('utf-8', errors='replace').strip()}")
    names = []
    for key in os.fsdecode(read.stdout).split("\0"):
        if key.startswith("hook.") and key.endswith(".command"):
            name = key[len("hook."):-len(".command")]
            if name not in names:
                names.append(name)
    return names


def _launch(argv, env, *, effect, input, deadline, pass_fds=()):
    """The only place Harness starts a Git process; it gets what is left of the call's deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(argv, 0)
    temporary_fds = []
    inherited_fds = []
    try:
        for fd in pass_fds:
            if fd in (0, 1, 2):
                # Redirection replaces these slots in the child. Preserve the same open file description.
                fd = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3)
                temporary_fds.append(fd)
            inherited_fds.append(fd)
        return subprocess.run(argv, env=env, capture_output=True, timeout=remaining, input=input,
                              stdin=subprocess.DEVNULL if input is None else None, pass_fds=tuple(inherited_fds))
    finally:
        for fd in temporary_fds:
            os.close(fd)


class GitLaunchError(HarnessError):
    """A Git process could not start or did not exit by its deadline."""


def _run_git(repo, args, binding=None, *, effect=None, input=None, index_file=None, literal_pathspecs=False,
             timeout=60, isolated_config=False):
    """One call without binding checks: the binding check itself runs through here, so it cannot recurse."""
    deadline = time.monotonic() + timeout
    try:
        argv, env = invocation(repo, args, effect=effect, deadline=deadline, binding=binding,
                               index_file=index_file, literal_pathspecs=literal_pathspecs,
                               isolated_config=isolated_config)
        return _launch(argv, env, effect=effect, input=input, deadline=deadline)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitLaunchError(f"git {' '.join(args[:3])} failed: {error}") from error


def git_process(repo: Path, *args: str, effect=None, binding=None, timeout=60, input=None, index_file=None,
                literal_pathspecs=False, isolated_config=False, _config=None, _c_locale=False, pass_fds=()):
    """Run a pinned command, preserving exit status for provenance checks."""
    # Before the binding check, which starts a process.
    _check_read_only(effect)
    _check_config(_config)
    _check_index_file(repo, binding, effect, index_file)
    if binding is not None:
        check_worktree_binding(repo, binding, isolated_config=isolated_config)
    # Measured after the check: the binding checks are calls of their own, with their own budget.
    deadline = time.monotonic() + timeout
    argv, env = invocation(repo, args, effect=effect, deadline=deadline, binding=binding, index_file=index_file,
                           literal_pathspecs=literal_pathspecs, isolated_config=isolated_config, _config=_config,
                           _c_locale=_c_locale)
    p = _launch(argv, env, effect=effect, input=input, deadline=deadline, pass_fds=pass_fds)
    if binding is not None:
        check_worktree_binding(repo, binding, isolated_config=isolated_config)
    return p


def git_bytes(repo: Path, *args: str, effect=None, binding=None, input=None, index_file=None,
              literal_pathspecs=False, isolated_config=False, _config=None) -> bytes:
    try:
        p = git_process(repo, *args, effect=effect, binding=binding, input=input, index_file=index_file,
                        literal_pathspecs=literal_pathspecs, isolated_config=isolated_config, _config=_config)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HarnessError(f"git {' '.join(args[:3])} failed: {error}") from error
    if p.returncode:
        raise HarnessError(f"git {' '.join(args[:3])} failed: {p.stderr.decode('utf-8', errors='replace').strip()}")
    return p.stdout


def git(repo: Path, *args: str, effect=None, binding=None, input=None, index_file=None, literal_pathspecs=False,
        isolated_config=False) -> str:
    """Keep filesystem bytes reversible."""
    return os.fsdecode(git_bytes(repo, *args, effect=effect, binding=binding, input=input, index_file=index_file,
                                 literal_pathspecs=literal_pathspecs, isolated_config=isolated_config))


def add_worktree(repo: Path, path: Path, commit: str, *, branch=None, binding=None, isolated_config=False):
    """Register a linked worktree, then populate it in its own pinned Git context."""
    path = Path(repo).absolute() / path
    mode = ["-b", branch] if branch is not None else ["--detach"]
    git(repo, "worktree", "add", "--no-checkout", *mode, str(path), commit,
        effect=CHECKOUT, binding=binding, isolated_config=isolated_config)
    registered_path = None
    try:
        # The registration can be checked even if reading the new checkout's Git binding fails.
        registered_path = os.fsencode(path.resolve())
        created = capture_worktree_binding(path, branch, isolated_config=isolated_config)
        registered_path = os.fsencode(created["worktree"])
        try:
            result = git_process(path, "reset", "-q", "--hard", "--no-recurse-submodules",
                                 effect=CHECKOUT, binding=created, isolated_config=isolated_config)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HarnessError(f"git reset -q --hard failed: {error}") from error
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise HarnessError(f"git reset -q --hard failed: {detail}",
                               stop=stops.make("runtime_failure", "io"))
    except (HarnessError, OSError) as error:
        # Like a failed checkout inside worktree add, this leaves the new branch in place.
        undo = None
        try:
            git(repo, "worktree", "remove", "--force", str(path),
                effect=CHECKOUT, binding=binding, isolated_config=isolated_config)
        except (HarnessError, OSError) as failure:
            undo = failure
        directory, registration = _worktree_remains(repo, path, registered_path, binding=binding,
                                                    isolated_config=isolated_config)
        if undo is None and directory == registration == "absent":
            raise
        detail = f"；{undo}" if undo is not None else ""
        raise HarnessError(f"{error}；新检出未能完全撤回，保留现场：{path}："
                           f"目录 {directory}，登记 {registration}{detail}",
                           stop=stops.of(error)) from error
    return created


def _worktree_remains(repo, path, registered_path, *, binding, isolated_config):
    try:
        path.lstat()
    except FileNotFoundError:
        directory = "absent"
    except OSError:
        directory = "unknown"
    else:
        directory = "present"
    if registered_path is None:
        return directory, "unknown"
    try:
        registration = _worktree_registration(repo, registered_path, binding=binding, isolated_config=isolated_config)
    except (HarnessError, OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        registration = "unknown"
    return directory, registration


def _worktree_registration(repo, path_bytes, *, binding, isolated_config):
    # Use the path recorded before rollback, even if the directory now points elsewhere.
    registry = worktree_registry(repo, binding=binding, isolated_config=isolated_config,
                                 rollback_target=path_bytes)
    if registry is None:
        return "unknown"
    return "present" if path_bytes in registry.values() else "absent"


def worktree_registry(repo, *, binding=None, isolated_config=False, rollback_target=None):
    """Return registered paths keyed by admin directory, with None for the main checkout.

    Rollback retains its set-or-unknown contract. Cleanup needs multiplicity
    and the admin directory of its exact match.
    """
    listing = git_process(repo, "worktree", "list", "--porcelain", "-z",
                          effect=READ, binding=binding, isolated_config=isolated_config, _c_locale=True)
    listed = None
    if listing.returncode:
        # Git 2.31–2.35 reject -z in the option parser, before reading the registry.
        if (listing.returncode != 129 or listing.stdout
                or not listing.stderr.startswith(b"error: unknown switch `z'\nusage: git worktree ")):
            return None
    else:
        if listing.stderr:
            return None
        listed = _worktree_listing_paths(listing.stdout, rollback_target)
        if listed is None:
            return None

    # Git can silently omit or truncate malformed backlinks. Both a hit and a
    # miss in the listing need to agree with the complete administrative paths.
    common = Path(binding["common_dir"] if binding is not None else
                  git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", effect=READ,
                      isolated_config=isolated_config).removesuffix("\n"))
    registered = with_descriptors(
        lambda held: _scan_worktree_registrations(held, common, allow_relative=listed is not None,
                                                  rollback=rollback_target is not None))
    if registered is None or (listed is not None and Counter(listed[1:]) != Counter(registered.values())):
        return None
    if rollback_target is not None:
        return registered
    # The legacy reader derives the main path from the real common directory,
    # stripping a trailing /.git. No legacy porcelain lines are interpreted.
    main = os.fsencode(common.resolve()).removesuffix(b"/.git")
    if not main or (listed is not None and listed[0] != main):
        return None
    return {None: main, **registered}


def _worktree_listing_paths(output, rollback_target):
    """Read NUL-framed path identities and reject contradictory records."""
    if not output.endswith(b"\0\0"):
        return None
    paths = []
    seen = set()
    for index, record in enumerate(output[:-2].split(b"\0\0")):
        field, *fields = record.split(b"\0")
        path = field.removeprefix(b"worktree ")
        if path == field or not path.startswith(b"/"):
            return None
        # A missing record separator can leave another path among the attributes.
        if any(field.startswith(b"worktree ") for field in fields):
            return None
        if rollback_target is not None:
            if path in seen or (path == rollback_target and (index == 0 or not _target_worktree_fields_valid(fields))):
                return None
            seen.add(path)
        elif (index != 0 or fields != [b"bare"]) and not _target_worktree_fields_valid(fields):
            return None
        paths.append(path)
    return paths


def _target_worktree_fields_valid(fields):
    attributes = {}
    for field in fields:
        name, separator, value = field.partition(b" ")
        if name in attributes:
            return False
        attributes[name] = value if separator else None
    head = attributes.pop(b"HEAD", None)
    if head is None or not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", head):
        return False
    if b"detached" in attributes:
        if attributes.pop(b"detached") is not None or b"branch" in attributes:
            return False
    else:
        branch = attributes.pop(b"branch", None)
        if branch is None or not re.fullmatch(rb"refs/[^\x00-\x20\x7f]+", branch):
            return False
    return (not attributes.keys() - {b"locked", b"prunable"}
            and b"" not in attributes.values()
            and not {b"locked", b"prunable"} <= attributes.keys())


def _scan_worktree_registrations(held, common, *, allow_relative, rollback):
    """Read the linked-worktree backlinks without following links in the administrative registry."""
    if not common.is_absolute():
        return None
    root = held.open(common, DIRECTORY_FLAGS)
    try:
        worktrees = held.open("worktrees", DIRECTORY_FLAGS, dir_fd=root)
    except FileNotFoundError:
        return {}
    registry = {}
    seen = set()
    for name in os.listdir(worktrees):
        entry = held.open(name, DIRECTORY_FLAGS, dir_fd=worktrees)
        admin = common / "worktrees" / name
        backlink = _binding_file_at(held, entry, admin / "gitdir")
        held.close(entry)
        checkout = backlink.removesuffix(b"/.git\n")
        if not checkout or checkout == backlink or b"\0" in checkout:
            return None
        if not os.path.isabs(checkout):
            if not allow_relative:
                return None
            # Git writes relative paths from the administrative directory to the
            # registered path. Compare them without accessing the user directory.
            checkout = os.path.normpath(os.path.join(os.fsencode(admin), checkout))
        elif os.path.normpath(checkout) != checkout:
            return None
        if rollback:
            if checkout in seen:
                return None
            seen.add(checkout)
        registry[admin] = checkout
    return registry


def _binding_file(path):
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise HarnessError(f"worktree Git 绑定文件必须为普通文件：{path}")
        return path.read_bytes()
    except OSError as error:
        stops.keep_local_io(error)
        raise HarnessError(f"worktree Git 绑定文件无法读取：{path}：{error}") from error


def _binding_path(parent, data):
    # Git's administrative paths are newline-terminated filesystem bytes.
    value = Path(os.fsdecode(data.removesuffix(b"\n")))
    return (parent / value).resolve()


def capture_worktree_binding(worktree: Path, expected_branch: str | None, *, isolated_config=False):
    """Persist the newly created linked worktree's actual Git identity.

    A detached binding also pins its commit, for disposable verification
    checkouts. Run branches may advance only on the captured symbolic ref.
    """
    worktree = worktree.resolve()
    gitfile = _binding_file(worktree / ".git")
    git_dir = Path(git(worktree, "rev-parse", "--absolute-git-dir", effect=READ,
                       isolated_config=isolated_config).removesuffix("\n")).resolve()
    common_dir = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir", effect=READ,
                          isolated_config=isolated_config)
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
        binding["detached_head"] = git(worktree, "rev-parse", "HEAD", effect=READ,
                                       isolated_config=isolated_config).strip()
    check_worktree_binding(worktree, binding, isolated_config=isolated_config)
    return binding


def _binding_file_at(held, root_fd, path):
    """Read a binding file from its open parent descriptor.

    The file descriptor is opened into ``held`` and closed here once read; after a
    failure the caller's cleanup of ``held`` closes it.
    """
    try:
        # Nonblocking: a FIFO without a writer must not hang the open before fstat refuses it.
        descriptor = held.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=root_fd)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HarnessError(f"worktree Git 绑定文件必须为普通文件：{path}")
        # Not deferred: an interrupt ends a slow read as it would a read by path.
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read()
        held.close(descriptor)
        return content
    except OSError as error:
        stops.keep_local_io(error)
        raise HarnessError(f"worktree Git 绑定文件无法读取：{path}：{error}") from error


def _check_binding_record(binding):
    fields = ("worktree", "git_dir", "common_dir", "gitfile_sha256", "backlink_sha256", "commondir_sha256")
    if (not isinstance(binding, dict) or any(not isinstance(binding.get(key), str) for key in fields)
            or "branch" not in binding
            or binding["branch"] is not None and not isinstance(binding["branch"], str)
            or binding.get("branch") is None and not isinstance(binding.get("detached_head"), str)):
        raise HarnessError("worktree Git 绑定缺失或无效；保留现场，不能重新推断绑定")


@stops.classifies("workspace_mismatch", "binding_changed")
def check_worktree_binding(worktree: Path, binding, *, isolated_config=False):
    """Refuse redirected metadata before any run-worktree Git command."""
    _check_binding_record(binding)
    _check_binding(worktree, binding, lambda checked: _binding_file(checked / ".git"),
                   isolated_config=isolated_config)


@stops.classifies("workspace_mismatch", "binding_changed")
def open_bound_root(held, worktree: Path, binding) -> int:
    """Open the bound root once into ``held`` and check the binding on it.

    The root's .git, the only fact the check takes from the root itself, is read
    through this descriptor, so a command started from it runs below the very
    directory the check accepted. The descriptor stays ``held``'s whatever the
    outcome; the caller's cleanup of ``held`` closes it.
    """
    _check_binding_record(binding)
    try:
        root = held.open(binding["worktree"], DIRECTORY_FLAGS)
    except (OSError, ValueError) as error:
        stops.keep_local_io(error)
        raise HarnessError(f"worktree 根目录无法打开：{binding['worktree']}：{error}") from error
    _check_binding(worktree, binding, lambda checked: _binding_file_at(held, root, checked / ".git"))
    return root


def _check_binding(worktree, binding, read_gitfile, *, isolated_config=False):
    # read_gitfile gets the path resolved and compared here, never a second resolution of the caller's path.
    worktree = worktree.resolve()
    git_dir, common_dir = Path(binding["git_dir"]), Path(binding["common_dir"])
    if (str(worktree) != binding["worktree"] or git_dir.resolve() != git_dir
            or common_dir.resolve() != common_dir or git_dir == common_dir
            or not git_dir.is_dir() or not common_dir.is_dir()):
        raise HarnessError("worktree Git 绑定目录已改变；保留现场")
    gitfile = read_gitfile(worktree)
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
    result = _run_git(worktree, ("symbolic-ref", "-q", "HEAD"), binding, effect=READ,
                      isolated_config=isolated_config)
    if binding["branch"] is not None:
        matches = result.returncode == 0 and os.fsdecode(result.stdout).strip() == binding["branch"]
        message = "worktree Git 分支绑定已改变；保留现场"
    else:
        matches = (result.returncode == 1
                   and _binding_file(git_dir / "HEAD").strip().decode("ascii", errors="replace") == binding["detached_head"])
        message = "验证 worktree 的 detached HEAD 绑定已改变；保留现场"
    if not matches:
        error = HarnessError(message)
        if result.returncode not in (0, 1):
            # Retain the failed Git observation without changing the existing binding stop.
            error.binding_git_result = result
        raise error


def repository(path: Path):
    return Path(git(path, "rev-parse", "--show-toplevel", effect=READ).removesuffix("\n")).resolve()


def head(repo: Path, *, binding=None):
    return git(repo, "rev-parse", "HEAD", effect=READ, binding=binding).strip()


def ignore_state(repo: Path):
    """Install and verify private-root and common-Git ignore protection."""
    from .private_state import ensure_private_state
    ensure_private_state(repo)


def clean(repo: Path, *, binding=None):
    # Explicit, so status.showUntrackedFiles=no cannot hide an untracked file.
    return not git(repo, "status", "--porcelain", "--untracked-files=normal", effect=COMPARE, binding=binding).strip()


def check_starting_checkout(repo: Path):
    """Require committed tracked files; untracked main-checkout files stay local."""
    if git(repo, "status", "--porcelain", "--untracked-files=no", effect=COMPARE).strip():
        raise HarnessError("目标仓库有已跟踪或暂存的未提交变更；请先保存提交，再创建隔离 worktree", stop=stops.make("invalid_input", "cli_arguments", data={"option": "--repo"}))
    return [os.fsdecode(name) for name in
            git_bytes(repo, "ls-files", "--others", "--exclude-standard", "--directory", "-z", effect=READ).split(b"\0")
            if name]


def describe_paths(names):
    """Keep filesystem diagnostics bounded and escape control/non-UTF-8 bytes."""
    shown = [repr(name) for name in names[:10]]
    paths = ", ".join(name if len(name) <= 240 else name[:240] + "…" for name in shown)
    remaining = f"；另有 {len(names) - 10} 项未列出" if len(names) > 10 else ""
    return paths + remaining


def reject_submodules(repo: Path, *, binding=None):
    """Reject indexed gitlinks, including uninitialized or missing submodules."""
    index = git_bytes(repo, "ls-files", "--stage", "-z", effect=READ, binding=binding)
    with stops.classify("invalid_input", "cli_arguments", data={"option": "--repo"}):
        _reject_indexed_submodules(index)


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
        raise HarnessError("任务修改了受保护路径：.harness", stop=stops.make("scope_violation", "protected_path", data={"path": ".harness"}))


def assert_removable_worktree(repo: Path, *, binding=None):
    """Refuse removal unless `git worktree remove` would lose no unignored data.

    All tracked bytes must match HEAD, no index flag may hide data and no
    unignored untracked file may exist. Ignored files are the caller's
    decision: Git removes them with the worktree. Git status may trust cached
    stat fields or index flags. Hash the actual bytes with Git's blob framing,
    without clean filters or external programs. This intentionally refuses
    transformed checkouts (for example autocrlf) when their bytes differ from
    the saved blobs.
    """
    for entry in git_bytes(repo, "ls-files", "-v", "-z", effect=READ, binding=binding).split(b"\0"):
        if entry[:1].islower() or entry.startswith(b"S "):
            raise HarnessError("worktree 索引含 assume-unchanged/skip-worktree 标记；"
                               f"请先核对并清除，不能移除：{os.fsdecode(entry[2:])}")
    untracked = [os.fsdecode(name) for name in
                 git_bytes(repo, "ls-files", "--others", "--exclude-standard", "-z", effect=READ, binding=binding)
                 .split(b"\0") if name]
    if untracked:
        raise HarnessError("worktree 有未被忽略的未跟踪文件；请先归档或清理，不能移除："
                           + describe_paths(untracked))
    tree = {}
    for entry in git_bytes(repo, "ls-tree", "-r", "-z", "HEAD", effect=READ, binding=binding).split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        if kind != b"blob" or mode not in (b"100644", b"100755", b"120000"):
            raise HarnessError(f"worktree 含 submodule 或不支持的文件类型，不能移除：{os.fsdecode(name)}")
        tree[name] = (mode, object_id)
    index = {}
    for entry in git_bytes(repo, "ls-files", "--stage", "-z", effect=READ, binding=binding).split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, object_id, stage = metadata.split()
        if stage != b"0":
            raise HarnessError("worktree 索引含未解决的冲突，不能移除")
        index[name] = (mode, object_id)
    if index != tree:
        raise HarnessError("worktree 索引与 HEAD 不同；请先保存或归档，不能移除")
    object_format = git(repo, "rev-parse", "--show-object-format", effect=READ, binding=binding).strip()
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


IGNORED_LIMIT_ENTRIES = 100
IGNORED_LIMIT_BYTES = 8192


def ignored_paths(repo: Path, *, binding=None):
    """Git's current ignored set: directories folded, empty directories omitted, sorted.

    "Ignored" is what Git decides for this worktree: its .gitignore files, the
    common directory's info/exclude and core.excludesFile. Raises when Git
    cannot list it.
    """
    return sorted(os.fsdecode(name) for name in git_bytes(
        repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory",
        "--no-empty-directory", "-z", effect=READ, binding=binding).split(b"\0") if name)


def bounded_paths(names):
    """A path-only listing: the sorted prefix within 100 entries and 8 KiB; the rest is counted."""
    entries, size = [], 2
    for name in names:
        if len(entries) >= IGNORED_LIMIT_ENTRIES:
            break
        # Compact JSON with filesystem bytes escaped as util.dump writes them.
        added = len(json.dumps(name, ensure_ascii=False).encode("utf-8", errors="backslashreplace")) + bool(entries)
        if size + added > IGNORED_LIMIT_BYTES:
            break
        entries.append(name)
        size += added
    return {"coverage": "paths_only_not_contents", "entries": entries,
            "entry_count": len(names), "omitted_count": len(names) - len(entries)}


# Search permission is enough to open names below a directory: O_SEARCH (macOS), else O_PATH (Linux), else read.
_SEARCH = getattr(os, "O_SEARCH", 0) or getattr(os, "O_PATH", 0) or os.O_RDONLY
# Open failures that may only say what the entry is; its actual type, read without following it, decides.
_TYPE_ERRNOS = frozenset(getattr(errno, name) for name in (
    "ELOOP", "EMLINK", "ENOTDIR", "EISDIR", "ENXIO", "ENODEV", "EOPNOTSUPP", "ENOTSUP") if hasattr(errno, name))


def _type_changed(name):
    return HarnessError(f"快照读取期间路径类型已改变；保留现场：{describe_paths([name])}",
                        stop=stops.make("workspace_mismatch", "candidate_changed"))


def _parents(held, root, names, *, verify_type_changes=True):
    """Yield ``(name, parent, leaf)`` for sorted worktree-relative ``names``.

    ``parent`` is the descriptor of the directory holding ``leaf``, opened level by
    level from ``root`` into ``held`` without following links, or None when an
    ancestor is missing or no directory (a link, a file): Git then lists the name as
    deleted. Sorted names sharing a prefix are adjacent, so each directory opens once
    and at most depth + 1 stay open. Permission and I/O failures propagate.
    Snapshots confirm the type on an ambiguous open failure to detect a directory replacement.
    """
    stack, gone = [((), root)], None
    for name in names:
        *above, leaf = name.split("/")
        above = tuple(above)
        if gone is not None and above[:len(gone)] == gone:
            yield name, None, leaf
            continue
        gone = None
        while len(stack) > 1 and above[:len(stack[-1][0])] != stack[-1][0]:
            held.close(stack.pop()[1])
        for depth in range(len(stack[-1][0]), len(above)):
            segment, parent = above[depth], stack[-1][1]
            try:
                stack.append((above[:depth + 1], held.open(
                    segment, _SEARCH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)))
            except OSError as error:
                if not verify_type_changes:
                    if error.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                        raise
                elif error.errno != errno.ENOENT:
                    if error.errno not in _TYPE_ERRNOS:
                        raise
                    try:
                        found = os.stat(segment, dir_fd=parent, follow_symlinks=False).st_mode
                    except FileNotFoundError:
                        found = None
                    if found is not None and stat.S_ISDIR(found):
                        raise _type_changed(name) from error
                gone = above[:depth + 1]
                break
        yield name, None if gone is not None else stack[-1][1], leaf


def _entry(held, parent, leaf, name):
    """The snapshot mode and bytes of ``leaf`` in the open directory ``parent``: a link's text, a regular file's bytes."""
    try:
        found = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(found.st_mode):
            try:
                return "link", os.fsencode(os.readlink(leaf, dir_fd=parent))
            except OSError as error:
                if error.errno == errno.EINVAL:  # no longer a link
                    raise _type_changed(name) from error
                raise
        if not stat.S_ISREG(found.st_mode):
            # A tracked file replaced by a directory is a deletion of that file; Git enumerates the new
            # descendants separately. Gitlinks are rejected from index modes, including newly staged ones.
            return "deleted", b""
        try:
            # Nonblocking: a FIFO swapped in since the stat must not hang the open before fstat refuses it.
            descriptor = held.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        except OSError as error:
            if error.errno in _TYPE_ERRNOS:
                raise _type_changed(name) from error
            raise
    except FileNotFoundError:
        return "deleted", b""
    opened = os.fstat(descriptor).st_mode
    if not stat.S_ISREG(opened):
        raise _type_changed(name)
    with os.fdopen(descriptor, "rb", closefd=False) as stream:
        data = stream.read()
    held.close(descriptor)
    return str(opened & 0o777), data


@stops.classifies("workspace_mismatch", "binding_changed")
def _open_snapshot_root(held, worktree: Path, binding) -> int:
    """open_bound_root for the snapshot walk, which only opens names below the root: search permission is enough.

    It checks in check_worktree_binding's order, opening the root (not following a link) where that check
    reads .git and reading .git through it. The descriptor stays ``held``'s whatever the outcome.
    """
    _check_binding_record(binding)
    root = None

    def read_gitfile(checked):
        nonlocal root
        try:
            root = held.open(binding["worktree"], _SEARCH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except (OSError, ValueError) as error:
            stops.keep_local_io(error)
            raise HarnessError(f"worktree 根目录无法打开：{binding['worktree']}：{error}") from error
        return _binding_file_at(held, root, checked / ".git")

    _check_binding(worktree, binding, read_gitfile)
    return root


def read_worktree(repo: Path, *, binding=None):
    """Return the snapshot and the exact real-index listing hashed into it."""
    index = git_bytes(repo, "ls-files", "--stage", "-z", effect=READ, binding=binding)
    _reject_indexed_submodules(index)
    result = {"head": head(repo, binding=binding), "index": hashlib.sha256(index).hexdigest(),
              "files": {}}
    names = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard", effect=READ,
                binding=binding).split("\0")

    def read(held):
        # Bound, the binding is checked on the very descriptor the walk starts from; unbound, the root opens by path.
        root = (_open_snapshot_root(held, repo, binding) if binding is not None
                else held.open(repo, _SEARCH | os.O_DIRECTORY | os.O_CLOEXEC))
        for name, parent, leaf in _parents(held, root, sorted(set(names) - {""})):
            mode, data = ("deleted", b"") if parent is None else _entry(held, parent, leaf, name)
            result["files"][name] = mode + ":" + hashlib.sha256(data).hexdigest()
    with_descriptors(read)
    return result, index


def snapshot(repo: Path, *, binding=None):
    observation, _ = read_worktree(repo, binding=binding)
    return observation


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
    tracked = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "--no-renames", "-z", "HEAD",
                  effect=COMPARE, binding=binding).split("\0")
    staged = git(repo, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--name-only", "--no-renames", "-z",
                 "HEAD", effect=COMPARE, binding=binding).split("\0")
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z", effect=READ, binding=binding).split("\0")
    return sorted((set(tracked) | set(staged) | set(untracked)) - {""})


def special_files(repo: Path, paths):
    """Find FIFOs, sockets and devices among the given Git paths using only metadata."""
    def inspect(held):
        expected = os.lstat(repo)
        root = held.open(repo, _SEARCH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        opened = os.fstat(root)
        if not stat.S_ISDIR(opened.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "Worktree root is not a directory", repo)
        if not os.path.samestat(expected, opened):
            raise OSError(errno.ESTALE, "Worktree root changed while opening", repo)
        found = []
        for name, parent, leaf in _parents(held, root, sorted(paths), verify_type_changes=False):
            if parent is None:
                continue
            try:
                mode = os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode
            except (FileNotFoundError, NotADirectoryError):
                continue
            if stat.S_IFMT(mode) in (stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK):
                found.append(name)
        return found

    return with_descriptors(inspect)


def validate_scope_pattern(pattern: str):
    """Require a root-relative pattern without changing literal filename bytes."""
    parts = pattern.split("/")
    if not pattern.strip() or "\0" in pattern or any(part in ("", ".", "..") for part in parts):
        raise HarnessError(f"无效文件范围：{pattern}")
    if protected_name(parts[0]):
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


class ScopeViolation(HarnessError):
    """A Git candidate contains paths forbidden by scope or unrecordable by Git."""


def _require_recordable(repo, path, *, source=None):
    if special_files(repo, (path,)):
        raise ScopeViolation(f"候选含 Git 不能记录的特殊文件：{describe_paths([path])}",
                             stop=stops.make("scope_violation", "special_file", data={"path": path}, source=source))


def check_scope(repo: Path, allowed: list[str], *, binding=None, task_id=None):
    """The candidate's changed paths; a scope stop names ``task_id`` (the task whose candidate it is)."""
    source = {"task_id": task_id} if task_id is not None else {}
    for pattern in allowed:
        validate_scope_pattern(pattern)
    try:
        check_worktree_state(repo, binding=binding)
    except HarnessError as error:
        if source and error.stop is not None and error.stop["kind"] == "scope_violation":
            error.stop = stops.make("scope_violation", error.stop["reason"], data=error.stop["data"], source=source)
        raise
    paths = changed(repo, binding=binding)
    for path in paths:
        if protected_name(path.split("/", 1)[0]):
            raise ScopeViolation(f"任务修改了受保护路径：{path}", stop=stops.make("scope_violation", "protected_path", data={"path": path}, source=source))
        if not any(path_matches(path, pattern) for pattern in allowed):
            raise ScopeViolation(f"任务越界修改：{path}；允许范围：{allowed}", stop=stops.make("scope_violation", "candidate_outside_scope", data={"path": path}, source=source))
        _require_recordable(repo, path, source=source)
    return paths


def diff_bytes(repo: Path, *, binding=None):
    """Lossless Git evidence, including untracked files and non-UTF-8 content."""
    value = git_bytes(repo, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames", "HEAD",
                      effect=COMPARE, binding=binding)
    for name in git(repo, "ls-files", "--others", "--exclude-standard", "-z", effect=READ, binding=binding).split("\0"):
        if not name:
            continue
        if binding is not None:
            check_worktree_binding(repo, binding)
        p = _run_git(repo, ("diff", "--no-index", "--binary", "--no-ext-diff", "--no-textconv",
                            "--", "/dev/null", name), binding, effect=COMPARE)
        if binding is not None:
            check_worktree_binding(repo, binding)
        if p.returncode not in (0, 1):
            raise HarnessError(p.stderr.decode("utf-8", errors="replace"))
        value += p.stdout
    return value


# Git's defaults for what diff-tree still reads from configuration that would reshape the whole-run comparison:
# blank context lines, hunk boundaries, abbreviated and quoted names, the binary patch's deflate level, and the
# binary guess for a path without a diff attribute; only _range_diff passes them (R1). Still applied, as they were
# to porcelain diff, since no option restores Git's own choice: attributes (.gitattributes, core.attributesFile),
# so the diff attribute's binary or text and a built-in driver's hunk header rule, and the binary, funcname and
# xfuncname of the driver it names; diff.default.(x)funcname too. A pattern's matches follow the locale.
_RANGE_DIFF_CONFIG = ("diff.suppressBlankEmpty=false", "diff.indentHeuristic=true", "core.abbrev=auto",
                      "core.quotePath=true", "core.looseCompression=1", "diff.default.binary=auto")
# Where _range_diff's private index is named, never made: the null device's folder always exists, lies in no worktree
# or Git directory, and only root writes there, so an unguessable name in it is absent when Git opens it.
_ABSENT_INDEX_FOLDER = os.path.dirname(os.devnull)


def _range_diff(repo, base, commit, *output, binding):
    """diff-tree from ``base`` to ``commit``: the bytes of porcelain diff under default configuration.

    An absent private index, a new unguessable name for each call, keeps Git from
    reusing a work tree file whose stat data matches a real index entry, so the result
    comes from the two commits' objects only, as porcelain diff between two commits
    does; it also spares a real index Git cannot read. Git must not create it either.
    --ignore-submodules=none overrides submodule.<name>.ignore and .gitmodules;
    --diff-algorithm=myers a diff.<driver>.algorithm, for any driver an attribute names.
    A driver's command and textconv are off; its wordRegex shapes only word diffs.
    """
    index = Path(_ABSENT_INDEX_FOLDER) / f"harness-index-{os.urandom(16).hex()}"
    value = git_bytes(repo, "diff-tree", "-r", "--no-ext-diff", "--no-textconv", "--no-renames",
                      "--ignore-submodules=none", "--diff-algorithm=myers", *output, base, commit, "--",
                      effect=READ, binding=binding, index_file=index, _config=_RANGE_DIFF_CONFIG)
    if os.path.lexists(index):
        raise HarnessError(f"整体比较的私有索引应当不存在，Git 调用之后却存在：{index}",
                           stop=stops.make("runtime_failure", "invariant"))
    return value


def range_diff_bytes(repo: Path, base: str, commit: str, *, binding=None):
    """The whole committed result, independent of task boundaries."""
    return _range_diff(repo, base, commit, "-p", "--binary", binding=binding)


def range_paths(repo: Path, base: str, commit: str, *, binding=None) -> str:
    """The paths of the whole committed result, NUL-terminated as Git lists them."""
    return os.fsdecode(_range_diff(repo, base, commit, "--name-only", "-z", binding=binding))


def archive_and_reset(repo: Path, expected_head: str, archive: Path, owned_root: Path, *, binding=None):
    """Archive rejected edits before restoring an owned run worktree checkpoint."""
    repo = repo.resolve()
    if not repo.is_relative_to(owned_root.resolve()) or not (repo / ".git").is_file():
        raise HarnessError("只能重置 Harness 所有的隔离 worktree")
    if head(repo, binding=binding) != expected_head:
        raise HarnessError("重置前 HEAD 与检查点不同；保留现场")
    paths = changed(repo, binding=binding)
    for name in paths:
        _require_recordable(repo, name)
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
    (archive / "staged.diff").write_bytes(git_bytes(repo, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv",
                                                 "HEAD", effect=COMPARE, binding=binding))
    write_json(archive / "snapshot.json", before)
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z", effect=READ, binding=binding).split("\0")
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
    git(repo, "reset", "--hard", expected_head, effect=CHECKOUT, binding=binding)
    # Remove only the recorded, untracked files, never ignored files or arbitrary directories.
    for name in filter(None, untracked):
        path = repo / name
        if path.is_symlink() or path.is_file():
            path.unlink()
    if not clean(repo, binding=binding) or head(repo, binding=binding) != expected_head:
        raise HarnessError("重置未恢复到干净检查点；归档已保留")
    write_json(archive / "restored.json", {"head": expected_head, "paths": paths})
    return str(archive)


# Every Harness Git call pins this identity and the commit encoding through _fixed_options.
COMMIT_IDENTITY = ("Personal Harness", "harness@localhost")


def commit_message_text(message, run_id, task_id):
    """The exact commit message: the worker's subject and body verbatim, then two runtime trailers."""
    body = message["body"] + "\n\n" if message["body"] else ""
    return f"{message['subject']}\n\n{body}Harness-Run: {run_id}\nHarness-Task: {task_id}\n"


class BuiltCommit(NamedTuple):
    tree: str
    commit: str
    index_synced: str


class ImageProof(NamedTuple):
    paths: tuple[str, ...]
    detail: str | None

    @property
    def equal(self) -> bool:
        return not self.paths and self.detail is None


def prove_image(repo: Path, commit: str, tree: str, *, binding) -> ImageProof:
    """Compare the image Git would stage against the caller's tree for commit."""
    with tempfile.TemporaryDirectory(prefix="harness-image-") as directory:
        index = Path(directory) / "index"
        git_bytes(repo, "read-tree", commit, effect=PRIVATE, binding=binding, index_file=index)
        added = git_process(repo, "add", "-A", effect=PRIVATE, binding=binding, index_file=index)
        if added.returncode:
            lines = added.stderr.decode("utf-8", errors="replace").splitlines()
            first_line = lines[0] if lines else ""
            return ImageProof((), first_line or f"git add -A exited with status {added.returncode}")
        image = git(repo, "write-tree", effect=PRIVATE, binding=binding, index_file=index).strip()
    if image == tree:
        return ImageProof((), None)
    raw = git_bytes(repo, "diff-tree", "-r", "--name-only", "--no-renames", "--no-ext-diff", "--no-textconv", "-z",
                    tree, image, effect=READ, binding=binding)
    paths = tuple(os.fsdecode(path) for path in raw.split(b"\0") if path)
    # A recursive diff omits empty subtree entries; tree identity still decides the proof.
    return ImageProof(paths, None if paths else f"Git tree differs ({tree} != {image}) without changed leaf paths")


class BranchState(NamedTuple):
    symbolic: bool
    oid: str | None


class CommitHeader(NamedTuple):
    tree: str
    parents: tuple[str, ...]


def _accept_paths(repo, files, tracked, task_id, binding):
    candidates = {name.rstrip("/"): name for name in files}
    source = {"task_id": task_id}

    def inspect(held):
        root = _open_snapshot_root(held, repo, binding)
        remove, add = [], []
        for name, parent, leaf in _parents(held, root, sorted(candidates)):
            mode = None
            if parent is not None:
                try:
                    mode = os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode
                except FileNotFoundError:
                    pass
            if mode is None or stat.S_ISDIR(mode):
                if name in tracked:
                    remove.append(name)
                elif mode is not None:
                    path = candidates[name]
                    raise ScopeViolation(f"候选含未跟踪的目录：{describe_paths([path])}", stop=stops.make(
                        "scope_violation", "candidate_outside_scope", data={"path": path}, source=source))
            elif stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                add.append(name)
            else:
                raise ScopeViolation(f"候选含 Git 不能记录的特殊文件：{describe_paths([name])}", stop=stops.make(
                    "scope_violation", "special_file", data={"path": name}, source=source))
        return remove, add

    return with_descriptors(inspect)


def _check_candidate_tree(repo, parent, tree, files, allowed, task_id, binding):
    raw = git_bytes(repo, "diff-tree", "-r", "-z", "--no-renames", "--no-ext-diff", "--no-textconv",
                    parent, tree, effect=READ, binding=binding)
    source = {"task_id": task_id}
    reviewed = set(files)
    fields = raw.split(b"\0")
    if fields[-1] or len(fields) % 2 != 1:
        raise HarnessError("git diff-tree 返回无效的路径记录")
    for header, path in zip(fields[0:-1:2], fields[1:-1:2]):
        metadata = header.split()
        if len(metadata) != 5 or not header.startswith(b":") or not path:
            raise HarnessError("git diff-tree 返回无效的路径记录")
        new_mode = metadata[1]
        name = os.fsdecode(path)
        if protected_name(name.split("/", 1)[0]):
            reason = "protected_path"
        elif not any(path_matches(name, pattern) for pattern in allowed):
            reason = "candidate_outside_scope"
        elif new_mode not in (b"000000", b"100644", b"100755", b"120000"):
            reason = "candidate_outside_scope"
        elif name not in reviewed:
            raise HarnessError(f"候选路径已改变；保留现场：{describe_paths([name])}",
                               stop=stops.make("workspace_mismatch", "candidate_changed", source=source))
        else:
            continue
        raise ScopeViolation(f"候选树含不允许的路径：{describe_paths([name])}", stop=stops.make(
            "scope_violation", reason, data={"path": name}, source=source))


def build_commit(repo: Path, parent, files, allowed, task_id, message: str, *, binding):
    """Build a candidate from reviewed paths. The message is the complete commit text."""
    with tempfile.TemporaryDirectory(prefix="harness-accept-") as directory:
        index = Path(directory) / "index"
        git_bytes(repo, "read-tree", parent, effect=PRIVATE, binding=binding, index_file=index)
        tracked = set(git(repo, "ls-files", "-z", effect=READ, binding=binding, index_file=index).split("\0"))
        remove, add = _accept_paths(repo, files, tracked, task_id, binding)
        if remove:
            git_bytes(repo, "update-index", "-z", "--force-remove", "--stdin", effect=PRIVATE, binding=binding,
                      index_file=index, input=b"".join(os.fsencode(name) + b"\0" for name in remove))
        if add:
            git_bytes(repo, "add", "--", *add, effect=PRIVATE, binding=binding, index_file=index,
                      literal_pathspecs=True)
        tree = git(repo, "write-tree", effect=PRIVATE, binding=binding, index_file=index).strip()
        stage = git_bytes(repo, "ls-files", "--stage", "-z", effect=READ, binding=binding, index_file=index)
        synced = hashlib.sha256(stage).hexdigest()
        parent_tree = git(repo, "rev-parse", "--verify", f"{parent}^{{tree}}", effect=READ, binding=binding).strip()
        if tree == parent_tree:
            return BuiltCommit(tree, parent, synced)
        _check_candidate_tree(repo, parent, tree, files, allowed, task_id, binding)
        commit = git(repo, "commit-tree", tree, "-p", parent, "-F", "-", effect=PRIVATE,
                     binding=binding, input=message.encode("utf-8")).strip()
        return BuiltCommit(tree, commit, synced)


def _require_git_result(result, *allowed):
    if result.returncode not in allowed:
        raise HarnessError(result.stderr.decode("utf-8", errors="replace").strip())


def branch_state(repo: Path, ref, *, binding):
    symbolic = git_process(repo, "symbolic-ref", "-q", ref, effect=READ, binding=binding)
    _require_git_result(symbolic, 0, 1)
    if symbolic.returncode == 0:
        return BranchState(True, None)
    value = git_process(repo, "rev-parse", "--verify", "-q", ref, effect=READ, binding=binding)
    _require_git_result(value, 0, 1)
    if value.returncode == 1:
        return BranchState(False, None)
    oid = value.stdout.removesuffix(b"\n")
    if re.fullmatch(rb"([0-9a-f]{40}|[0-9a-f]{64})", oid) is None:
        raise HarnessError("git rev-parse 返回无效的分支对象名")
    return BranchState(False, oid.decode("ascii"))


def object_kinds(repo: Path, oids, *, binding):
    requested = tuple(oids)
    data = b"".join(oid.encode("ascii") + b"\n" for oid in requested)
    result = git_bytes(repo, "cat-file", "--batch-check", effect=READ, binding=binding, input=data)
    lines = result.splitlines()
    if len(lines) != len(requested):
        raise HarnessError("git cat-file --batch-check 返回的行数不符")
    kinds = {}
    for oid, line in zip(requested, lines):
        fields = line.split(b" ")
        if fields == [oid.encode("ascii"), b"missing"]:
            kinds[oid] = None
        elif (len(fields) == 3 and fields[0] == oid.encode("ascii") and fields[2].isdigit()
              and fields[1] in (b"blob", b"tree", b"commit", b"tag")):
            kinds[oid] = fields[1].decode("ascii")
        else:
            raise HarnessError(f"git cat-file --batch-check 返回无效对象记录：{line!r}")
    return kinds


def commit_header(repo: Path, oid, *, binding):
    raw = git_bytes(repo, "cat-file", "commit", oid, effect=READ, binding=binding)
    header, separator, _ = raw.partition(b"\n\n")
    trees, parents = [], []
    for line in header.split(b"\n"):
        key, _, value = line.partition(b" ")
        if key in (b"tree", b"parent"):
            if len(value) != len(oid) or re.fullmatch(rb"[0-9a-f]+", value) is None:
                raise HarnessError(f"git cat-file commit 返回无效的对象头：{oid}")
            (trees if key == b"tree" else parents).append(value.decode("ascii"))
    if len(trees) != 1 or not separator:
        raise HarnessError(f"git cat-file commit 返回无效的树头：{oid}")
    return CommitHeader(trees[0], tuple(parents))


def tree_readable(repo: Path, tree, *, binding):
    result = git_process(repo, "cat-file", "tree", tree, effect=READ, binding=binding)
    _require_git_result(result, 0)


def index_digest(repo: Path, *, binding):
    stage = git_bytes(repo, "ls-files", "--stage", "-z", effect=READ, binding=binding)
    return hashlib.sha256(stage).hexdigest()


def move_branch(repo: Path, ref, commit, parent, task_id, *, binding, lock_fd: int | None = None):
    """Compare-and-swap the run ref. The caller interprets a nonzero Git status."""
    return git_process(repo, "update-ref", "--no-deref", "-m", f"harness accept {task_id}", ref, commit, parent,
                       effect=REFS, binding=binding, pass_fds=() if lock_fd is None else (lock_fd,))


def sync_index(repo: Path, commit, *, binding, lock_fd: int | None = None):
    """Synchronize the real index while preserving worktree files."""
    return git_process(repo, "read-tree", "--reset", commit, effect=INDEX, binding=binding,
                       pass_fds=() if lock_fd is None else (lock_fd,))
