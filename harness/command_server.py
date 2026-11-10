"""Exact-argv worker commands over a deliberately closed MCP interface.

Each run_command is a new execution, including repeated explicit requests. Receipts
are evidence, never a retry cache. This is capability routing, not OS isolation.
delete_path unlinks one name that, at its final check, is a tracked regular file
inside the current task scope. It shares the admission checks, lock, journal and
receipts, but starts no process. It does not isolate the worktree from other
processes running concurrently; see _remove_tracked_file.
"""
import argparse
import hashlib
import json
import math
import os
import re
import signal
import stat
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .process_runtime import defer_cleanup_signals
from . import git
from .util import HarnessError, ProcessStartError, ProcessTimeout, group_alive, lock, read_json, run_process, tail, write_json


# Journal identity of delete_path records. Configured commands use 64-hex IDs.
DELETE_COMMAND_ID = "delete_path"
MAX_DELETE_PATH_CHARS = 4096
# Call folders are prepared here, beside commands/, and renamed into it once
# their journal is complete. Nothing enumerates this directory.
STAGING_DIRECTORY = "command-staging"
_PROTECTED_FOLDED = frozenset(root.casefold() for root in git.PROTECTED_ROOTS)


def _file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_id(argv):
    return hashlib.sha256(json.dumps(argv, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _safe(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise HarnessError(f"Command artifact symlink refused: {path}")
    return path


def _argv(value):
    return (isinstance(value, list) and bool(value) and all(isinstance(x, str) and "\0" not in x for x in value)
            and bool(value[0].strip()))


def _valid_delete_scope(scope):
    if (not isinstance(scope, dict) or set(scope) != {"task_id", "allowed_paths", "root_identity"}
            or not isinstance(scope["task_id"], str) or not scope["task_id"]
            or not isinstance(scope["allowed_paths"], list) or not scope["allowed_paths"]
            or not all(isinstance(pattern, str) for pattern in scope["allowed_paths"])
            or not isinstance(scope["root_identity"], list) or len(scope["root_identity"]) != 2
            or not all(type(value) is int and value >= 0 for value in scope["root_identity"])):
        return False
    try:
        for pattern in scope["allowed_paths"]:
            git.validate_scope_pattern(pattern)
    except HarnessError:
        return False
    return True


def _ledger_delete_scope(state, dispatch_id):
    """The live worker dispatch's task scope, read from the run ledger."""
    entries = [d for d in state.get("dispatches", []) if isinstance(d, dict) and d.get("id") == dispatch_id]
    if len(entries) != 1 or entries[0].get("role") != "worker":
        return None
    tasks = [t for t in state.get("tasks", []) if isinstance(t, dict) and t.get("id") == entries[0].get("task_id")]
    return {"task_id": tasks[0]["id"], "allowed_paths": tasks[0].get("allowed_paths")} if len(tasks) == 1 else None


def create_manifest(config, cwd, job, run_path=None, *, parent_deadline=None, delete_scope=None):
    """Write the per-dispatch closed command manifest.

    A run-bound manifest takes delete_path's task scope from the ledger's live
    worker dispatch. Without a ledger (probes and tests) only an explicit
    ``delete_scope`` of {task_id, allowed_paths} enables the tool.
    """
    data = config.data if hasattr(config, "data") else config
    cwd, job = _safe(Path(cwd).resolve()), _safe(job)
    job.mkdir(parents=True, exist_ok=True)
    _safe(job / "commands").mkdir(exist_ok=True)
    commands = {}
    for argv in data["verification"]["commands"] + data["permissions"]["worker_commands"]:
        if not _argv(argv):
            raise HarnessError("Invalid exact command argv")
        commands.setdefault(command_id(argv), {"command_id": command_id(argv), "argv": argv})
    timeout = data["runtime"]["verify_timeout_seconds"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout:
        raise HarnessError("Command timeout must be finite and positive")
    if parent_deadline is None:
        parent_deadline = time.monotonic() + data["runtime"].get("agent_timeout_seconds", timeout + 5)
    run_path = _safe(run_path).resolve() if run_path is not None else None
    state = read_json(run_path) if run_path else {}
    if run_path is not None:
        if delete_scope is not None:
            raise HarnessError("A run-bound worker takes its delete scope from the run ledger")
        delete_scope = _ledger_delete_scope(state, job.name)
    if delete_scope is not None:
        if not isinstance(delete_scope, dict):
            raise HarnessError("Invalid delete_path task scope")
        # Bind the worktree root by inode so a later path swap cannot redirect it.
        root = os.stat(cwd, follow_symlinks=False)
        delete_scope = {"task_id": delete_scope.get("task_id"), "allowed_paths": delete_scope.get("allowed_paths"),
                        "root_identity": [root.st_dev, root.st_ino]}
        if not _valid_delete_scope(delete_scope):
            raise HarnessError("Invalid delete_path task scope")
    manifest = {"version": 2, "role": "worker", "dispatch_id": job.name, "cwd": str(cwd),
                "job": str(job.resolve()), "run_path": str(run_path) if run_path else None,
                "binding": state.get("worktree_binding"), "expected_head": state.get("expected_head"),
                "timeout": timeout, "parent_deadline": parent_deadline, "cleanup_margin": 2.0,
                "commands": list(commands.values()), "delete_scope": delete_scope}
    path = _safe(job / "commands.json")
    if path.exists():
        raise HarnessError("Command manifest already exists")
    write_json(path, manifest)
    return {"artifact": str(path), "artifact_digest": _file_digest(path),
            "commands": manifest["commands"]}


def command_processes(job_or_dir):
    directory = _safe(job_or_dir)
    if directory.name != "commands":
        directory = _safe(directory / "commands")
    if not directory.exists():
        return []
    records = []
    for folder in sorted(directory.iterdir()):
        _safe(folder)
        if not folder.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", folder.name):
            raise HarnessError("Invalid command journal directory")
        path = _safe(folder / "process.json")
        try:
            entry = read_json(path)
        except (OSError, ValueError) as error:
            raise HarnessError(f"Unreadable command process journal: {path}") from error
        expected_id = directory.parent.name + ":command:" + folder.name
        if (not isinstance(entry, dict) or set(entry) != {"id", "command_id", "pgid", "process_state", "exit_confirmed"}
                or not isinstance(entry.get("command_id"), str)
                or entry.get("id") != expected_id
                or entry.get("process_state") not in ("prepared", "starting", "running", "exited")
                or type(entry.get("exit_confirmed")) is not bool
                or entry.get("pgid") is not None and (type(entry["pgid"]) is not int or entry["pgid"] <= 0)):
            raise HarnessError("Invalid command process journal")
        entry["journal"] = str(path)
        entry["journal_digest"] = _file_digest(path)
        records.append(entry)
    return records


def ensure_command_processes_exited(directory, parent_entry=None, *, observe=group_alive):
    records = command_processes(directory)
    for entry in records:
        confirmation = (parent_entry or {}).get("command_exit_confirmations", {}).get(entry["id"], {})
        if entry["exit_confirmed"] or confirmation.get("journal_digest") == entry["journal_digest"]:
            continue
        pgid = entry["pgid"]
        if (pgid is not None and observe(pgid)) or (pgid is None and entry["process_state"] != "prepared"):
            raise HarnessError(f"Cannot confirm command process exited: {entry['id']}")
    return records


class Broker:
    def __init__(self, manifest, digest):
        self.path, self.digest = _safe(manifest), digest
        self.manifest = None
        self._load()

    def _load(self):
        raw = _safe(self.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.digest:
            raise HarnessError("Command manifest digest mismatch")
        m = json.loads(raw)
        fields = {"version", "role", "dispatch_id", "cwd", "job", "run_path", "binding", "expected_head", "timeout", "parent_deadline", "cleanup_margin", "commands", "delete_scope"}
        if (not isinstance(m, dict) or set(m) != fields or m["version"] != 2 or m["role"] != "worker"
                or m["delete_scope"] is not None and not _valid_delete_scope(m["delete_scope"])
                or str(self.path) != str(_safe(Path(m["job"]) / "commands.json"))
                or Path(m["job"]).name != m["dispatch_id"]
                or type(m["timeout"]) not in (int, float) or not math.isfinite(m["timeout"])
                or not 0 < m["timeout"] or not isinstance(m["commands"], list)
                or any(type(m[k]) not in (int, float) or not math.isfinite(m[k]) or m[k] <= 0
                       for k in ("parent_deadline", "cleanup_margin"))):
            raise HarnessError("Invalid closed command manifest")
        ids = set()
        for c in m["commands"]:
            if (not isinstance(c, dict) or set(c) != {"command_id", "argv"} or not _argv(c["argv"])
                    or c["command_id"] != command_id(c["argv"]) or c["command_id"] in ids):
                raise HarnessError("Invalid or duplicate command ID")
            ids.add(c["command_id"])
        _safe(m["cwd"])
        _safe(Path(m["job"]) / "commands")
        if self.manifest is not None and m != self.manifest:
            raise HarnessError("Command manifest changed")
        self.manifest = m
        self._source()
        return m

    def _source(self):
        m = self.manifest
        if m["run_path"] is None:
            return
        state = read_json(_safe(m["run_path"]))
        entries = [d for d in state.get("dispatches", []) if d.get("id") == m["dispatch_id"]]
        if (state.get("status") != "running" or len(entries) != 1
                or state.get("dispatches", [])[-1].get("id") != m["dispatch_id"]
                or entries[0].get("role") != "worker" or entries[0].get("kind") not in ("implement", "format_repair")
                or entries[0].get("command_directory") != str(Path(m["job"]) / "commands")
                or entries[0].get("status") != "running" or entries[0].get("invoked") is not True
                or state.get("worktree") != m["cwd"] or state.get("worktree_binding") != m["binding"]
                or state.get("expected_head") != m["expected_head"]):
            raise HarnessError("Command source dispatch is no longer live")
        git.check_worktree_binding(Path(m["cwd"]), m["binding"])
        if git.head(Path(m["cwd"]), binding=m["binding"]) != m["expected_head"]:
            raise HarnessError("Command source HEAD changed")

    def list_commands(self):
        return self._load()["commands"]

    def tools(self):
        # delete_path exists only for a worker dispatch with a current task scope.
        return TOOLS + ([DELETE_TOOL] if self._load()["delete_scope"] is not None else [])

    def _prior_evidence(self, m):
        """Every earlier command or deletion must have exited with complete evidence."""
        ensure_command_processes_exited(m["job"])
        for prior in command_processes(m["job"]):
            if prior["process_state"] == "prepared":
                continue  # No start intent was ever committed.
            receipt_path = Path(prior["journal"]).with_name("receipt.json")
            try:
                receipt = read_json(_safe(receipt_path))
                if (receipt["call_id"] != prior["id"] or receipt["command_id"] != prior["command_id"]
                        or receipt.get("evidence_complete") is not True
                        or receipt.get("manifest_sha256") != self.digest
                        or receipt.get("journal_sha256") != prior["journal_digest"]
                        or receipt.get("exit_confirmed") != prior["exit_confirmed"]):
                    raise ValueError("receipt identity or completeness")
                if prior["command_id"] == DELETE_COMMAND_ID:
                    if receipt.get("operation") != "delete_path":
                        raise ValueError("deletion receipt kind")
                    if receipt["intent_path"] is not None:
                        expected = receipt_path.with_name("delete-intent.json")
                        if receipt["intent_path"] != str(expected) or _file_digest(_safe(expected)) != receipt["intent_sha256"]:
                            raise ValueError("deletion intent digest")
                    continue
                for stream in ("stdout", "stderr"):
                    expected = receipt_path.with_name("output." + stream)
                    if receipt[stream + "_path"] != str(expected) or _file_digest(_safe(expected)) != receipt[stream + "_sha256"]:
                        raise ValueError("capture digest")
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise HarnessError("Command evidence incomplete: " + prior["id"]) from error

    def _admit(self, arguments):
        # Reload authority, inspect every prior child, and durably register intent
        # under one dispatch lock. The active parent delegates this resource.
        m = self._load()
        if not isinstance(arguments, dict) or set(arguments) != {"command_id"} or not isinstance(arguments["command_id"], str):
            raise HarnessError("run_command accepts only command_id")
        command = next((c for c in m["commands"] if c["command_id"] == arguments["command_id"]), None)
        if command is None:
            raise HarnessError("Unknown command_id")
        self._prior_evidence(m)
        remaining = m["parent_deadline"] - time.monotonic()
        timeout = min(m["timeout"], remaining - m["cleanup_margin"])
        if timeout <= 0:
            raise HarnessError(f"Command budget exhausted: remaining={remaining:.3f}s cleanup_margin={m['cleanup_margin']:.3f}s; not started")
        folder, entry = self._publish_journal(m, command["command_id"], process_state="starting", exit_confirmed=False)
        return m, command, timeout, folder, entry

    def _publish_journal(self, m, identifier, *, process_state, exit_confirmed):
        """Make one call's folder visible in commands/ only with its whole journal.

        Every exit gate reads every folder in commands/, so a folder there
        without a readable journal would block resume, confirmation and abandon
        alike. The folder and journal are written in STAGING_DIRECTORY, which
        nothing enumerates, and published by one rename. A write failure or a
        kill before the rename leaves at most an ignored staged folder; callers
        start no child and delete nothing until this returns.
        """
        name = uuid.uuid4().hex
        staging = _safe(Path(m["job"]) / STAGING_DIRECTORY)
        staging.mkdir(mode=0o700, exist_ok=True)
        staged = _safe(staging / name)
        staged.mkdir(mode=0o700)
        entry = {"id": m["dispatch_id"] + ":command:" + name, "command_id": identifier,
                 "pgid": None, "process_state": process_state, "exit_confirmed": exit_confirmed}
        write_json(staged / "process.json", entry)
        _fsync_directory(staged)
        folder = _safe(Path(m["job"]) / "commands" / name)
        _rename(staged, folder)
        return folder, entry

    def run_command(self, arguments):
        with lock(_safe(Path(self.manifest["job"]) / "command-admission.lock")):
            m, command, timeout, folder, entry = self._admit(arguments)
        journal = folder / "process.json"
        result = {"call_id": entry["id"], "command_id": command["command_id"], "exit_code": None,
                  "error": None, "evidence_complete": False, "effective_timeout": timeout}
        def started(pgid):
            entry.update(pgid=pgid, process_state="running")
            write_json(journal, entry)
        def outcome(value):
            facts = value.to_dict()
            result.update(lifecycle=facts, exit_code=facts["exit_code"],
                          termination_reason=facts["termination_reason"],
                          evidence_complete=facts["evidence_complete"])
            entry.update(exit_confirmed=facts["exit_confirmed"])
            if facts["exit_confirmed"]:
                entry["process_state"] = "exited"
        started_clock = time.monotonic()
        result["started_at"] = datetime.now(timezone.utc).isoformat()
        old = {}
        fatal_error = None
        def interrupted(signum, frame):
            raise KeyboardInterrupt(f"Command broker signal {signum}")
        try:
            for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                old[signum] = signal.signal(signum, interrupted)
            result["exit_code"] = run_process(command["argv"], Path(m["cwd"]), folder / "output",
                                               timeout=timeout, on_start=started, on_outcome=outcome,
                                               execution_deadline=m["parent_deadline"] - m["cleanup_margin"],
                                               cleanup_deadline=m["parent_deadline"])
        except BaseException as error:
            if not isinstance(error, (Exception, KeyboardInterrupt, SystemExit)):
                fatal_error = error
            if getattr(error, "outcome", None) is not None:
                outcome(error.outcome)
            result["error"] = str(error) or type(error).__name__
            result["timed_out"] = isinstance(error, ProcessTimeout)
            if isinstance(error, ProcessStartError):
                entry.update(process_state="exited", exit_confirmed=True)
        finally:
            try:
                with defer_cleanup_signals(primary=fatal_error or result["error"]) as deferred:
                    result["finished_at"] = datetime.now(timezone.utc).isoformat()
                    result["duration_seconds"] = max(0.0, time.monotonic() - started_clock)
                    result["timing_scope"] = "worker_command_nested_in_dispatch"
                    # Never replace known wait/timeout facts with a teardown observation.
                    try:
                        write_json(journal, entry)
                        for stream in ("stdout", "stderr"):
                            path = folder / ("output." + stream)
                            if not path.exists():
                                raise HarnessError("Missing command capture: " + stream)
                            result[stream] = tail(path, 4000)
                            result[stream + "_path"] = str(path)
                            result[stream + "_sha256"] = _file_digest(path)
                        result["exit_confirmed"] = entry["exit_confirmed"]
                        result["journal_sha256"] = _file_digest(journal)
                        result["manifest_sha256"] = self.digest
                        write_json(folder / "receipt.json", result)
                    except (Exception, KeyboardInterrupt, SystemExit) as error:
                        result.update(evidence_complete=False, persistence_error="Command evidence persistence failed: " + str(error))
                        result["exit_confirmed"] = entry["exit_confirmed"]
                    if deferred and result["error"] is None and fatal_error is None:
                        deferred.handled = True
                        result.update(error=f"Command broker signal {deferred[0]}",
                                      termination_reason="cancelled", timed_out=False)
                        if "lifecycle" in result:
                            result["lifecycle"]["termination_reason"] = "cancelled"
                            result["lifecycle"]["errors"].append({
                                "step": "execution", "type": "KeyboardInterrupt",
                                "message": result["error"], "errno": None})
                        try:
                            if "lifecycle" in result:
                                write_json(folder / "output.outcome.json", result["lifecycle"])
                            write_json(folder / "receipt.json", result)
                        except (Exception, KeyboardInterrupt, SystemExit) as error:
                            result.update(evidence_complete=False, persistence_error="Command evidence persistence failed: " + str(error))
            except KeyboardInterrupt as error:
                # The command receipt may already be committed. Record a broker
                # cancellation without rewriting its completed lifecycle facts.
                if fatal_error is None and result["error"] is None:
                    result.update(error=str(error) or "Command broker cancelled",
                                  response_cancelled=True)
                    with defer_cleanup_signals(primary=error):
                        try:
                            write_json(folder / "receipt.json", result)
                        except (Exception, KeyboardInterrupt, SystemExit) as persistence_error:
                            result.update(evidence_complete=False,
                                          persistence_error=str(persistence_error))
            finally:
                for signum, handler in old.items():
                    signal.signal(signum, handler)
        if fatal_error is not None:
            raise fatal_error
        return result

    def _delete_scope(self, m):
        scope = m["delete_scope"]
        if scope is None:
            raise HarnessError("delete_path is unavailable: this dispatch has no current task scope")
        if m["run_path"] is not None:
            # _load already proved the dispatch live; its task scope must still match.
            state = read_json(_safe(m["run_path"]))
            if _ledger_delete_scope(state, m["dispatch_id"]) != {k: scope[k] for k in ("task_id", "allowed_paths")}:
                raise HarnessError("delete_path task scope no longer matches the run ledger")
        return scope

    def _delete_eligibility(self, m, scope, path):
        _delete_syntax(path)
        if not any(git.path_matches(path, pattern) for pattern in scope["allowed_paths"]):
            raise _DeleteRefused(f"{path[:160]!r} is outside the task scope {scope['allowed_paths']}")
        cwd = Path(m["cwd"])
        # A run-bound broker already proved HEAD equals the ledger's expected head.
        head = m["expected_head"] if m["run_path"] is not None else git.head(cwd)
        target = os.fsencode(path)
        for record in git.git_bytes(cwd, "ls-tree", "-r", "-z", "--full-tree", head, binding=m["binding"]).split(b"\0"):
            metadata, _, name = record.partition(b"\t")
            if name == target:
                mode, kind, _ = metadata.split()
                if kind != b"blob" or mode not in (b"100644", b"100755"):
                    raise _DeleteRefused(f"{path[:160]!r} is not a regular file in HEAD (mode {mode.decode()}); "
                                         "links and submodules are refused")
                return head, mode.decode()
        raise _DeleteRefused(f"{path[:160]!r} is not a file tracked in HEAD; untracked files and directories are refused")

    def delete_path(self, arguments):
        """Delete one in-scope tracked regular file and record it like a command.

        Admission is the command broker's own: current dispatch identity, ledger
        source and HEAD, confirmed exit and complete evidence of every earlier
        call, all under the admission lock. There is no child process, so the
        journal is published already exited; it can never become an unknown
        exit. Nothing is inspected or unlinked before that journal is published,
        and if publication fails nothing was done and nothing is recorded.
        Every published call, refused or not, leaves a receipt. Signals wait
        until that receipt is durable. Nothing replays a call; receipts are
        evidence. What the deletion itself guarantees is in _remove_tracked_file.
        """
        if (not isinstance(arguments, dict) or set(arguments) != {"path"} or not isinstance(arguments["path"], str)
                or len(arguments["path"]) > MAX_DELETE_PATH_CHARS):
            raise HarnessError(f"delete_path accepts only path, a string of at most {MAX_DELETE_PATH_CHARS} characters")
        path = arguments["path"]
        fatal_error = None
        with lock(_safe(Path(self.manifest["job"]) / "command-admission.lock")):
            m = self._load()
            scope = self._delete_scope(m)
            self._prior_evidence(m)
            with defer_cleanup_signals():
                folder, entry = self._publish_journal(m, DELETE_COMMAND_ID, process_state="exited",
                                                      exit_confirmed=True)
                journal = folder / "process.json"
                started_clock = time.monotonic()
                result = {"call_id": entry["id"], "command_id": DELETE_COMMAND_ID, "operation": "delete_path",
                          "path": path, "task_id": scope["task_id"], "head": None, "head_mode": None,
                          "outcome": None, "deleted": False, "sha256": None, "bytes": None, "error": None,
                          "concurrent_change": None, "intent_path": None, "intent_sha256": None,
                          "started_at": datetime.now(timezone.utc).isoformat()}
                facts = {"deleted": False}
                def intent(found):
                    # Written before the final check: the verified file that is
                    # about to be removed. A refusal at that check keeps the
                    # intent and records deleted=false.
                    result.update(sha256=found["sha256"], bytes=found["bytes"])
                    value = {"call_id": entry["id"], "path": path, "head": result["head"],
                             "head_mode": result["head_mode"], "sha256": found["sha256"],
                             "bytes": found["bytes"], "time": datetime.now(timezone.utc).isoformat()}
                    write_json(folder / "delete-intent.json", value)
                    result.update(intent_path=str(folder / "delete-intent.json"),
                                  intent_sha256=_file_digest(folder / "delete-intent.json"))
                try:
                    result["head"], result["head_mode"] = self._delete_eligibility(m, scope, path)
                    _remove_tracked_file(Path(m["cwd"]), scope["root_identity"], path, facts, intent)
                    result["concurrent_change"] = facts.get("concurrent_change")
                    result["outcome"] = "concurrent_change" if result["concurrent_change"] else "deleted"
                    if result["concurrent_change"]:
                        result["error"] = result["concurrent_change"]
                except _DeleteRefused as error:
                    result.update(outcome="refused", error=str(error))
                except BaseException as error:
                    if not isinstance(error, (Exception, KeyboardInterrupt, SystemExit)):
                        fatal_error = error
                    result.update(outcome="failed", error=str(error) or type(error).__name__)
                finally:
                    result["deleted"] = bool(facts["deleted"])
                    result.update(finished_at=datetime.now(timezone.utc).isoformat(),
                                  duration_seconds=max(0.0, time.monotonic() - started_clock),
                                  exit_confirmed=True, evidence_complete=True, manifest_sha256=self.digest)
                    try:
                        result["journal_sha256"] = _file_digest(journal)
                        write_json(folder / "receipt.json", result)
                    except (Exception, KeyboardInterrupt, SystemExit) as error:
                        result.update(evidence_complete=False,
                                      persistence_error="Deletion evidence persistence failed: " + str(error))
        if fatal_error is not None:
            raise fatal_error
        return result


class _DeleteRefused(Exception):
    """An admitted delete_path call refused before anything was unlinked."""


def _fold(segment):
    # Compare protected names strictly: ignore format characters that some
    # filesystems drop, compatibility forms, letter case and trailing dots.
    kept = "".join(char for char in segment if unicodedata.category(char) != "Cf")
    return unicodedata.normalize("NFKC", kept).casefold().rstrip(". ")


def _delete_syntax(path):
    shown = repr(path[:160])
    if not path:
        raise _DeleteRefused("empty path")
    if "\0" in path:
        raise _DeleteRefused(f"path contains NUL: {shown}")
    if path.startswith("/"):
        raise _DeleteRefused(f"absolute path {shown}; use a literal repository-relative path")
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise _DeleteRefused(f"empty, '.' or '..' segment in {shown}")
    folded = [_fold(segment) for segment in segments]
    if folded[0] in _PROTECTED_FOLDED or ".git" in folded:
        raise _DeleteRefused(f"protected path {shown}: .git, .harness, .codex and .claude are refused in any letter case")


# Filesystem primitives, kept separate so tests can interpose concurrent changes.
def _openat(name, flags, dir_fd=None):
    return os.open(name, flags, dir_fd=dir_fd)


def _lstatat(name, dir_fd):
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _unlinkat(name, dir_fd):
    os.unlink(name, dir_fd=dir_fd)


def _rename(source, target):
    os.rename(source, target)


def _fsync_directory(path):
    # The staged journal's name must be durable before the folder is published.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _identity(value):
    return [value.st_dev, value.st_ino]


def _chain_change(root, root_identity, held, segments, final=None):
    """Describe how the held descriptors no longer name root/segments, if they do not."""
    try:
        current = os.stat(root, follow_symlinks=False)
    except OSError as error:
        return f"worktree root path changed ({error.strerror})"
    if _identity(current) != root_identity:
        return "worktree root path no longer names the bound root"
    for index, segment in enumerate(segments[:-1]):
        try:
            current = _lstatat(segment, held[index])
        except OSError as error:
            return f"parent {segment!r} was replaced or moved ({error.strerror})"
        if not stat.S_ISDIR(current.st_mode) or _identity(current) != _identity(os.fstat(held[index + 1])):
            return f"parent {segment!r} was replaced or moved"
    if final is not None:
        try:
            current = _lstatat(segments[-1], held[-1])
        except OSError as error:
            return f"{segments[-1]!r} was replaced or removed ({error.strerror})"
        if not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(final):
            return f"{segments[-1]!r} was replaced"
    return None


def _remove_tracked_file(root, root_identity, path, facts, before_unlink):
    """Unlink one regular file below the worktree root without following links.

    The root is opened without following a final link and must be the inode
    bound when the manifest was written. Each parent is opened relative to the
    previous descriptor with O_NOFOLLOW. The final entry must be a regular file;
    it is hashed through a no-follow descriptor, the intent is recorded, the
    whole descriptor chain is checked again, and unlinkat() removes the name
    relative to its parent. No directory is removed or created.

    Guaranteed: every parent is opened level by level from the worktree root
    descriptor with O_NOFOLLOW; no symbolic link is followed, unlinkat
    included; at the final check the entry is the tracked, in-scope regular
    file that was hashed; every admitted call leaves a record (written by
    Broker.delete_path).

    Not guaranteed: this is not isolation from concurrently running processes.
    Between the final check and unlinkat, a process that can rename entries in
    the worktree can make the removed file be one that lives elsewhere,
    including outside the worktree. Only adjacent system calls separate the
    check from unlinkat, which narrows but does not close that window. The
    after-the-fact concurrent-change report is best effort and can miss such
    a rename, for example when the directory is moved back. Nothing undoes a
    deletion; the acceptance gate decides only whether the candidate is
    accepted.
    """
    segments = path.split("/")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    full = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    held, file_fd, opened = [], None, None
    try:
        try:
            held.append(_openat(str(root), flags | os.O_DIRECTORY | os.O_NOFOLLOW))
        except OSError as error:
            raise _DeleteRefused(f"worktree root cannot be opened without following links: {error.strerror}") from error
        if _identity(os.fstat(held[0])) != root_identity:
            raise _DeleteRefused("worktree root is not the directory bound when this dispatch started")
        for segment in segments[:-1]:
            try:
                held.append(_openat(segment, flags | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=held[-1]))
            except OSError as error:
                raise _DeleteRefused(f"parent {segment!r} is missing, not a directory, or a link: {error.strerror}") from error
        name = segments[-1]
        try:
            before = _lstatat(name, held[-1])
        except OSError as error:
            raise _DeleteRefused(f"cannot inspect {path[:160]!r}: {error.strerror}") from error
        if not stat.S_ISREG(before.st_mode):
            raise _DeleteRefused(f"{path[:160]!r} is not a regular file in the worktree; "
                                 "directories, links and special files are refused")
        try:
            file_fd = _openat(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=held[-1])
        except OSError as error:
            raise _DeleteRefused(f"cannot open {path[:160]!r} without following links: {error.strerror}") from error
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode) or full(opened) != full(before):
            raise _DeleteRefused(f"{path[:160]!r} changed before it was read")
        digest, size = hashlib.sha256(), 0
        while chunk := os.read(file_fd, 1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
        if size != opened.st_size or full(os.fstat(file_fd)) != full(opened):
            raise _DeleteRefused(f"{path[:160]!r} changed while it was read")
        # Record the intent before the final check, so that unlinkat directly
        # follows the check's last system call.
        before_unlink({"sha256": digest.hexdigest(), "bytes": size})
        change = _chain_change(root, root_identity, held, segments, opened)
        if change:
            raise _DeleteRefused(change + "; nothing was deleted")
        _unlinkat(name, held[-1])
        # The verified inode lost a link only if its own name was the one removed.
        facts["deleted"] = os.fstat(file_fd).st_nlink < opened.st_nlink
        # Best effort only: a directory moved out and back again is not seen.
        change = _chain_change(root, root_identity, held, segments)
        if not facts["deleted"]:
            change = f"the removed entry {name!r} was not the verified file; it was replaced concurrently"
        elif change:
            change = "deleted the verified file, but " + change + " during the deletion"
        facts["concurrent_change"] = change
    except BaseException:
        if file_fd is not None and opened is not None:
            try:
                facts["deleted"] = os.fstat(file_fd).st_nlink < opened.st_nlink
            except OSError:
                pass
        raise
    finally:
        for descriptor in ([file_fd] if file_fd is not None else []) + held[::-1]:
            os.close(descriptor)


DELETE_TOOL = {
    "name": "delete_path",
    "description": (
        "Delete one file of the current task. path is a literal repository-relative path that must exactly "
        "equal a regular file (mode 100644 or 100755) tracked in HEAD and match the task's allowed_paths. "
        "Refused: absolute paths, empty, '.' or '..' segments, .git/.harness/.codex/.claude in any letter case, "
        "untracked files, directories, symlinks and submodules. Never removes directories or empty parents and "
        "never moves files. Guaranteed: parents are opened one level at a time from the worktree root without "
        "following links, no symlink is ever followed, at the final check the entry is that tracked in-scope "
        "regular file from HEAD, and it is then unlinked relative to its parent. Not guaranteed: this is not "
        "isolation from concurrently running processes; one that renames directories in the worktree between "
        "the final check and the unlink can make the removed file live elsewhere, including outside the "
        "worktree, and the after-the-fact concurrent_change report is best effort and can miss it. Every "
        "admitted call is recorded with path, prior SHA256 and bytes, outcome and time. The deletion is "
        "immediate; review and acceptance decide the candidate but cannot undo it."),
    "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
                    "additionalProperties": False}}


TOOLS = [{"name": "list_commands", "description": "List exact authorized command IDs and argv.",
          "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
         {"name": "run_command", "description": "Execute one exact authorized argv. Each explicit call executes anew.",
          "inputSchema": {"type": "object", "properties": {"command_id": {"type": "string"}}, "required": ["command_id"], "additionalProperties": False}}]


def handle(request, broker):
    method, params = request.get("method"), request.get("params", {})
    if method == "initialize":
        version = params.get("protocolVersion", "2025-11-25")
        if version not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            version = "2025-11-25"
        return {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": {"name": "harness-commands", "version": "1"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": broker.tools()}
    if method != "tools/call":
        raise HarnessError("Unknown method")
    try:
        if not isinstance(params, dict):
            raise HarnessError("Invalid tool fields: expected object")
        unknown = set(params) - {"name", "arguments", "_meta"}
        if unknown:
            raise HarnessError("Invalid tool fields: " + ", ".join(sorted(unknown)))
        args = params.get("arguments", {})
        if params.get("name") == "list_commands":
            if args != {}:
                raise HarnessError("list_commands accepts no fields")
            value = broker.list_commands()
        elif params.get("name") == "run_command":
            value = broker.run_command(args)
        elif params.get("name") == "delete_path":
            value = broker.delete_path(args)
            # A recorded refusal or failure is a tool error; its receipt is the payload.
            return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=True)}],
                    "isError": value["outcome"] != "deleted"}
        else:
            raise HarnessError("Unknown tool")
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=True)}], "isError": False}
    except (HarnessError, ValueError, OSError, TypeError) as error:
        return {"content": [{"type": "text", "text": json.dumps({"status": "blocked", "started": False, "error": str(error)}, ensure_ascii=True)}], "isError": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    broker = Broker(args.manifest, args.sha256)
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise HarnessError("Request must be object")
            if "id" not in request:
                continue
            response = {"jsonrpc": "2.0", "id": request["id"], "result": handle(request, broker)}
        except Exception as error:
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": {"code": -32603, "message": str(error)}}
        print(json.dumps(response, ensure_ascii=True), flush=True)
