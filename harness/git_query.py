"""Fixed-form, read-only Git queries shared by the MCP tool and coverage checks.

One parser decides both what the ``harness.git_query`` tool executes and which
declared argv count as available without an operator grant. Anything outside
the table, including shells, pipelines, redirections, global options and extra
switches, is rejected without execution.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time

from . import git
from .util import HarnessError

TOOL = "mcp__harness__git_query"
MAX_PATHS = 128
MAX_ARG_BYTES = 4096
MAX_ARGV = 2 + 3 + 1 + MAX_PATHS
# Two separate stdout limits. The retain limit bounds what is returned; reading,
# counting and hashing continue past it up to the observe limit (or the
# timeout), at which point the whole process group is killed and size and
# digest describe only the observed prefix.
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_OBSERVED_BYTES = 64 * 1024 * 1024
MAX_HASH_BYTES = 1024 ** 3
TIMEOUT_SECONDS = 30
FORMS = (
    "git ls-files [-z] [--deleted] -- <path>...",
    "git ls-tree -r <rev> -- <path>...",
    "git rev-parse [--verify] <rev>",
    "git log -1 --format=%H -- <path>",
    "git show <rev>:<path>",
    "git hash-object --no-filters -- <path>",
    "git cat-file -e|-t|-s <rev>[:<path>]",
)
RULES = ("argv must equal one form exactly, options in the listed order. <rev>: 7-64 hex digits, HEAD, "
         "HEAD~N or HEAD^N with N of 1-4 digits. Paths are literal repository-relative names (no glob or "
         ":(magic)), at most 128, without empty, '.' or '..' segments, a leading '-' or ':', NUL or shell "
         "operator tokens. show returns the raw blob (cat-file blob, no textconv); hash-object is computed "
         "without filters or object writes and refuses links, FIFOs and devices. stdout returns at most the "
         "first 1 MiB; stdout_truncated=true means the returned stdout is not the whole output. Reading, "
         "counting and hashing continue to 64 MiB or 30 seconds: observed_complete=true means stdout_bytes and "
         "stdout_sha256 cover the complete output. Once 64 MiB of stdout or stderr have been read (an output of "
         "exactly 64 MiB included), or after 30 seconds, the query is killed (terminated "
         "output_limit or timeout, exit_code null, observed_complete=false) and stdout_bytes and stdout_sha256 "
         "cover only the observed prefix.")
_REV = re.compile(r"[0-9a-fA-F]{7,64}|HEAD(?:[~^][0-9]{1,4})?")
# Literal argv never reaches a shell; rejecting operator-looking paths keeps a
# declared pipeline or redirection from being counted as an available query.
_SHELL_TOKEN = re.compile(r"[0-9]*[<>]|[|&;()`]")
# Global options precede the subcommand. They keep every query local and free
# of external programs configured in the repository or user config. An empty
# core.fsmonitor disables both hook paths and the builtin daemon on old and new
# Git; lazy fetching is disabled through the environment, which old Git ignores.
_GLOBAL = ("--no-pager", "--literal-pathspecs",
           "-c", "core.fsmonitor=", "-c", "log.showSignature=false",
           "-c", "color.ui=false", "-c", "protocol.allow=never")


class _Reject(Exception):
    pass


def _path(value, form):
    if not value:
        raise _Reject(f"{form}: empty path")
    if value.startswith("/"):
        raise _Reject(f"{form}: absolute path {value[:80]!r}; use a repository-relative path")
    if value[0] in "-:":
        raise _Reject(f"{form}: path must not start with '-' or ':' {value[:80]!r}")
    if _SHELL_TOKEN.match(value):
        raise _Reject(f"{form}: shell operator or redirection {value[:80]!r} is not a query path")
    segments = value.split("/")
    if "" in segments:
        raise _Reject(f"{form}: empty path segment in {value[:80]!r}")
    if "." in segments or ".." in segments:
        raise _Reject(f"{form}: '.' or '..' segment in {value[:80]!r}")
    return value


def _rev(value, form):
    if not _REV.fullmatch(value):
        raise _Reject(f"{form}: unsupported revision {value[:80]!r}; use 7-64 hex digits, HEAD, HEAD~N or HEAD^N")
    return value


def _object(value, form, *, require_path):
    rev, colon, path = value.partition(":")
    _rev(rev, form)
    if colon:
        return rev, _path(path, form)
    if require_path:
        raise _Reject(f"{form}: expected <rev>:<path>")
    return rev, None


def _paths(rest, form, *, maximum):
    if not rest:
        raise _Reject(f"{form}: missing -- before paths")
    if rest[0] != "--":
        raise _Reject(f"{form}: unsupported argument before --: {rest[0][:80]!r}")
    paths = rest[1:]
    if "--" in paths:
        raise _Reject(f"{form}: -- must appear exactly once")
    if not paths:
        raise _Reject(f"{form}: at least one path is required after --")
    if len(paths) > maximum:
        raise _Reject(f"{form}: at most {maximum} path(s) after --")
    return [_path(path, form) for path in paths]


def _ls_files(rest):
    options = []
    for flag in ("-z", "--deleted"):
        if rest[:1] == [flag]:
            options.append(flag)
            rest = rest[1:]
    return {"form": "ls-files", "options": options, "paths": _paths(rest, "ls-files", maximum=MAX_PATHS)}


def _ls_tree(rest):
    if len(rest) < 2 or rest[0] != "-r":
        raise _Reject("ls-tree: only ls-tree -r <rev> -- <path>... is supported")
    return {"form": "ls-tree", "rev": _rev(rest[1], "ls-tree"),
            "paths": _paths(rest[2:], "ls-tree", maximum=MAX_PATHS)}


def _rev_parse(rest):
    verify = rest[:1] == ["--verify"]
    if verify:
        rest = rest[1:]
    if len(rest) != 1:
        raise _Reject("rev-parse: only rev-parse [--verify] <rev> is supported")
    return {"form": "rev-parse", "verify": verify, "rev": _rev(rest[0], "rev-parse")}


def _log(rest):
    if rest[:2] != ["-1", "--format=%H"]:
        raise _Reject("log: only log -1 --format=%H -- <path> is supported")
    return {"form": "log", "path": _paths(rest[2:], "log", maximum=1)[0]}


def _show(rest):
    if len(rest) != 1:
        raise _Reject("show: only show <rev>:<path> is supported")
    rev, path = _object(rest[0], "show", require_path=True)
    return {"form": "show", "rev": rev, "path": path}


def _hash_object(rest):
    if rest[:1] != ["--no-filters"]:
        raise _Reject("hash-object: only hash-object --no-filters -- <path> is supported")
    return {"form": "hash-object", "path": _paths(rest[1:], "hash-object", maximum=1)[0]}


def _cat_file(rest):
    if len(rest) != 2 or rest[0] not in ("-e", "-t", "-s"):
        raise _Reject("cat-file: only cat-file -e|-t|-s <rev>[:<path>] is supported")
    rev, path = _object(rest[1], "cat-file", require_path=False)
    return {"form": "cat-file", "mode": rest[0], "rev": rev, "path": path}


_PARSERS = {"ls-files": _ls_files, "ls-tree": _ls_tree, "rev-parse": _rev_parse, "log": _log,
            "show": _show, "hash-object": _hash_object, "cat-file": _cat_file}


def _parse(argv):
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
        raise _Reject("argv must be a nonempty array of strings")
    if len(argv) > MAX_ARGV:
        raise _Reject(f"argv has more than {MAX_ARGV} items")
    for arg in argv:
        if "\0" in arg:
            raise _Reject("arguments must not contain NUL")
        try:
            size = len(arg.encode("utf-8"))
        except UnicodeEncodeError:
            raise _Reject("arguments must be valid Unicode text") from None
        if size > MAX_ARG_BYTES:
            raise _Reject(f"argument exceeds {MAX_ARG_BYTES} bytes")
    if argv[0] != "git":
        raise _Reject("only fixed git query argv are supported; shells, pipelines and other programs are not")
    if len(argv) < 2 or argv[1] not in _PARSERS:
        sub = argv[1][:80] if len(argv) > 1 else None
        raise _Reject(f"unsupported git subcommand or global option: {sub!r}")
    query = _PARSERS[argv[1]](argv[2:])
    query["argv"] = list(argv)
    return query


def parse_git_query(argv):
    """Return ``(query, None)`` for an exact table form, else ``(None, reason)``."""
    try:
        return _parse(argv), None
    except _Reject as error:
        return None, str(error)


def covers(argv):
    """True when the built-in tool provides this exact argv without a grant.

    Coverage states that the capability exists; it never claims the query ran
    or succeeded, and it is not a Bash or command-broker grant.
    """
    return parse_git_query(argv)[0] is not None


def _git_args(query):
    form = query["form"]
    if form == "ls-files":
        return ["ls-files", *query["options"], "--", *query["paths"]]
    if form == "ls-tree":
        return ["ls-tree", "-r", query["rev"], "--", *query["paths"]]
    if form == "rev-parse":
        return ["rev-parse", *(["--verify"] if query["verify"] else []), query["rev"]]
    if form == "log":
        return ["log", "-1", "--format=%H", "--no-show-signature", "--no-ext-diff", "--no-textconv",
                "--", query["path"]]
    if form == "show":
        return ["cat-file", "blob", f"{query['rev']}:{query['path']}"]
    if form == "cat-file":
        spec = query["rev"] if query["path"] is None else f"{query['rev']}:{query['path']}"
        return ["cat-file", query["mode"], spec]
    raise HarnessError(f"git_query form has no Git execution: {form}")


def _environment(binding):
    env = git._git_environment(binding)
    env.update(GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    return env


class _Stream:
    """Retain at most ``keep`` bytes; count and hash at most ``observe`` bytes."""

    def __init__(self, keep, observe):
        self.keep, self.observe = keep, observe
        self.size, self.digest, self.data = 0, hashlib.sha256(), bytearray()

    def feed(self, chunk):
        """Observe ``chunk`` up to the observe limit; True once the limit is reached.

        Reaching the limit ends observation without probing for end of stream,
        so an output of exactly the limit is also reported as cut off.
        """
        observed = chunk[:max(self.observe - self.size, 0)]
        self.size += len(observed)
        self.digest.update(observed)
        room = self.keep - len(self.data)
        if room > 0:
            self.data += observed[:room]
        return self.size >= self.observe


def _terminate(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _capture(argv, cwd, env, timeout):
    """Stream both pipes: retain bounded prefixes, count and hash up to the observe limit.

    Reaching ``MAX_OBSERVED_BYTES`` on either pipe, or the timeout, kills the
    process group: ``terminated`` is then ``output_limit`` or ``timeout`` and the
    exit code is unknown.
    """
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except OSError as error:
        raise HarnessError(f"git_query could not start git: {error}") from error
    streams = {"stdout": _Stream(MAX_OUTPUT_BYTES, MAX_OBSERVED_BYTES),
               "stderr": _Stream(MAX_STDERR_BYTES, MAX_OBSERVED_BYTES)}
    deadline, terminated = time.monotonic() + timeout, None
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map() and terminated is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminated = "timeout"
                break
            for key, _ in selector.select(min(remaining, 1.0)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if streams[key.data].feed(chunk):
                    terminated = "output_limit"
                    break
        if terminated is None:
            try:
                process.wait(timeout=max(deadline - time.monotonic(), 0.001))
            except subprocess.TimeoutExpired:
                terminated = "timeout"
        if terminated is not None:
            _terminate(process)
    finally:
        selector.close()
        if process.poll() is None:
            _terminate(process)
        process.stdout.close()
        process.stderr.close()
    return streams, (None if terminated else process.returncode), terminated


def _project(query, implementation, git_args, streams, exit_code, terminated, **extra):
    out, err = streams["stdout"], streams["stderr"]
    # Only a stream that ended on its own was observed whole; after a kill the
    # size and digest describe the observed prefix of an unknown output.
    complete = terminated is None
    data = bytes(out.data)
    # The returned text is a strict prefix when the retain limit cut it or the
    # process was killed before its output ended.
    truncated = not complete or len(data) < out.size
    text, encoding = None, "utf-8"
    # A truncated prefix may end inside a UTF-8 sequence; return whole characters.
    for cut in (range(4) if truncated else (0,)):
        try:
            text = data[:len(data) - cut].decode("utf-8")
            data = data[:len(data) - cut]
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text, encoding = base64.b64encode(data).decode("ascii"), "base64"
    # Size and digest precede the bulky text, so a client that clips a long
    # tool result still keeps the output identity and whether it is complete.
    result = {"argv": query["argv"], "form": query["form"], "implementation": implementation,
              "git_args": git_args, "exit_code": exit_code, "terminated": terminated,
              "observed_complete": complete, "stdout_bytes": out.size, "stdout_sha256": out.digest.hexdigest(),
              "stdout_truncated": truncated, "stdout_returned_bytes": len(data), "stdout_encoding": encoding,
              "retain_limit_bytes": out.keep, "observe_limit_bytes": out.observe, **extra,
              "stderr": bytes(err.data).decode("utf-8", errors="replace"),
              "stderr_truncated": not complete or len(err.data) < err.size, "stdout": text}
    return result


def _object_format(worktree, binding, timeout):
    streams, code, terminated = _capture(
        git._git_command(worktree, [*_GLOBAL, "rev-parse", "--show-object-format"], binding),
        worktree, _environment(binding), timeout)
    value = bytes(streams["stdout"].data).decode("ascii", errors="replace").strip()
    if value == "--show-object-format":
        # Git releases that echo the unknown option predate SHA-256 repositories.
        value = "sha1"
    if terminated or code != 0 or value not in ("sha1", "sha256"):
        raise HarnessError("git_query cannot determine the repository object format")
    return value


def _blob_hash(worktree, path, algorithm):
    """Hash one regular file like ``git hash-object --no-filters``, without Git.

    Every ancestor is opened relative to its parent without following links;
    the final entry must be a regular file before and after a nonblocking
    open, so FIFOs, devices, directories and links are refused unread.
    """
    segments = path.split("/")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    descriptors = []
    try:
        descriptors.append(os.open(worktree, flags | os.O_DIRECTORY))
        for segment in segments[:-1]:
            try:
                descriptors.append(os.open(segment, flags | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[-1]))
            except OSError as error:
                raise HarnessError(f"hash-object: ancestor {segment!r} is not a directory or is a link: {error.strerror}") from error
        try:
            before = os.stat(segments[-1], dir_fd=descriptors[-1], follow_symlinks=False)
        except OSError as error:
            raise HarnessError(f"hash-object: cannot inspect {path!r}: {error.strerror}") from error
        if not stat.S_ISREG(before.st_mode):
            raise HarnessError(f"hash-object: {path!r} is not a regular file")
        if before.st_size > MAX_HASH_BYTES:
            raise HarnessError(f"hash-object: {path!r} exceeds {MAX_HASH_BYTES} bytes")
        try:
            descriptors.append(os.open(segments[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptors[-1]))
        except OSError as error:
            raise HarnessError(f"hash-object: cannot open {path!r} without following links: {error.strerror}") from error
        opened = os.fstat(descriptors[-1])
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if not stat.S_ISREG(opened.st_mode) or identity(opened) != identity(before):
            raise HarnessError(f"hash-object: {path!r} changed before it was read")
        digest, content, size = hashlib.new(algorithm), hashlib.sha256(), 0
        digest.update(b"blob %d\0" % opened.st_size)
        while chunk := os.read(descriptors[-1], min(1024 * 1024, opened.st_size + 1 - size)):
            size += len(chunk)
            if size > opened.st_size:
                break
            digest.update(chunk)
            content.update(chunk)
        after = os.fstat(descriptors[-1])
        if size != opened.st_size or identity(after) != identity(opened):
            raise HarnessError(f"hash-object: {path!r} changed while it was read")
        return digest.hexdigest(), size, content.hexdigest()
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def run_git_query(query, worktree, binding, *, timeout=TIMEOUT_SECONDS):
    """Execute a parsed query on the bound run worktree; never writes objects or the index."""
    if not isinstance(query, dict) or parse_git_query(query.get("argv"))[0] != query:
        raise HarnessError("git_query requires a description produced by parse_git_query")
    if binding is None:
        raise HarnessError("git_query requires the run worktree Git binding")
    worktree = Path(worktree)
    try:
        git.check_worktree_binding(worktree, binding)
        worktree = Path(binding["worktree"])
        if query["form"] == "hash-object":
            algorithm = _object_format(worktree, binding, timeout)
            oid, size, content = _blob_hash(worktree, query["path"], algorithm)
            stream = _Stream(MAX_OUTPUT_BYTES, MAX_OBSERVED_BYTES)
            stream.feed((oid + "\n").encode("ascii"))
            result = _project(query, "python_blob_hash", None, {"stdout": stream, "stderr": _Stream(0, 0)}, 0, None,
                              object_format=algorithm, file_bytes=size, file_sha256=content)
        else:
            args = _git_args(query)
            streams, code, terminated = _capture(git._git_command(worktree, [*_GLOBAL, *args], binding),
                                                 worktree, _environment(binding), timeout)
            result = _project(query, "git", args, streams, code, terminated)
        git.check_worktree_binding(worktree, binding)
    except OSError as error:
        raise HarnessError(f"git_query failed: {error}") from error
    return result
