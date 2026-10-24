"""Exact-argv worker commands over a deliberately closed MCP interface.

Each run_command is a new execution, including repeated explicit requests. Receipts
are evidence, never a retry cache. This is capability routing, not OS isolation.
"""
import argparse
import hashlib
import json
import math
import os
import re
import signal
import sys
import uuid
from pathlib import Path

from . import git
from .util import HarnessError, ProcessStartError, ProcessTimeout, group_alive, read_json, run_process, tail, write_json


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


def create_manifest(config, cwd, job, run_path=None):
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
    run_path = _safe(run_path).resolve() if run_path is not None else None
    state = read_json(run_path) if run_path else {}
    manifest = {"version": 1, "role": "worker", "dispatch_id": job.name, "cwd": str(cwd),
                "job": str(job.resolve()), "run_path": str(run_path) if run_path else None,
                "binding": state.get("worktree_binding"), "expected_head": state.get("expected_head"),
                "timeout": timeout, "commands": list(commands.values())}
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
        fields = {"version", "role", "dispatch_id", "cwd", "job", "run_path", "binding", "expected_head", "timeout", "commands"}
        if (not isinstance(m, dict) or set(m) != fields or m["version"] != 1 or m["role"] != "worker"
                or str(self.path) != str(_safe(Path(m["job"]) / "commands.json"))
                or Path(m["job"]).name != m["dispatch_id"]
                or type(m["timeout"]) not in (int, float) or not math.isfinite(m["timeout"])
                or not 0 < m["timeout"] or not isinstance(m["commands"], list)):
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

    def run_command(self, arguments):
        m = self._load()
        if not isinstance(arguments, dict) or set(arguments) != {"command_id"} or not isinstance(arguments["command_id"], str):
            raise HarnessError("run_command accepts only command_id")
        command = next((c for c in m["commands"] if c["command_id"] == arguments["command_id"]), None)
        if command is None:
            raise HarnessError("Unknown command_id")
        folder = _safe(Path(m["job"]) / "commands" / uuid.uuid4().hex)
        folder.mkdir(mode=0o700)
        journal = folder / "process.json"
        entry = {"id": m["dispatch_id"] + ":command:" + folder.name, "command_id": command["command_id"],
                 "pgid": None, "process_state": "prepared", "exit_confirmed": False}
        write_json(journal, entry)
        result = {"call_id": entry["id"], "command_id": command["command_id"], "exit_code": None, "error": None}
        def started(pgid):
            entry.update(pgid=pgid, process_state="running")
            write_json(journal, entry)
        old = {}
        def interrupted(signum, frame):
            raise KeyboardInterrupt(f"Command broker signal {signum}")
        try:
            for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                old[signum] = signal.signal(signum, interrupted)
            entry["process_state"] = "starting"
            write_json(journal, entry)
            result["exit_code"] = run_process(command["argv"], Path(m["cwd"]), folder / "output", timeout=m["timeout"], on_start=started)
        except BaseException as error:
            result["error"] = str(error) or type(error).__name__
            result["timed_out"] = isinstance(error, ProcessTimeout)
            if isinstance(error, ProcessStartError):
                entry.update(process_state="exited", exit_confirmed=True)
        finally:
            for signum, handler in old.items():
                signal.signal(signum, handler)
            if entry["pgid"] is not None and not group_alive(entry["pgid"]):
                entry.update(process_state="exited", exit_confirmed=True)
            write_json(journal, entry)
            for stream in ("stdout", "stderr"):
                path = folder / ("output." + stream)
                if not path.exists():
                    path.write_bytes(b"")
                result[stream] = tail(path, 4000)
                result[stream + "_path"] = str(path)
                result[stream + "_sha256"] = _file_digest(path)
            result["exit_confirmed"] = entry["exit_confirmed"]
            write_json(folder / "receipt.json", result)
        return result


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
        broker._load()
        return {"tools": TOOLS}
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
        else:
            raise HarnessError("Unknown tool")
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=True)}], "isError": False}
    except (HarnessError, ValueError, OSError, TypeError) as error:
        return {"content": [{"type": "text", "text": str(error)}], "isError": True}


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
