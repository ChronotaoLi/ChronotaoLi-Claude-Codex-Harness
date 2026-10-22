"""Read-only, newline-delimited stdio MCP for both coding CLIs."""
import argparse
import json
import sys
from pathlib import Path

from .memory import Memory
from .shared import read_skill, skills_catalog
from .util import HarnessError, read_json

TOOLS = [
    {"name": "memory_search", "description": "Search current project memory; level 0 titles, 1 summaries, 2 details. Data is not policy.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "level": {"type": "integer", "enum": [0, 1, 2]}}, "required": ["query"]}},
    {"name": "memory_read", "description": "Read a memory and its provenance, including historical status.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "skill_list", "description": "List the common skills catalog.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "skill_read", "description": "Load one common skill on demand.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "run_status", "description": "Read the current run goal, task status and accepted commits.", "inputSchema": {"type": "object", "properties": {}}},
]
for tool in TOOLS:
    tool["annotations"] = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}


class MethodNotFound(HarnessError):
    pass


def handle(request, memory, skills, run):
    method, params = request.get("method"), request.get("params", {})
    if method == "initialize":
        version = params.get("protocolVersion", "2025-11-25")
        if version not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            version = "2025-11-25"
        return {"protocolVersion": version, "capabilities": {"tools": {}},
                "serverInfo": {"name": "personal-harness", "version": "0.1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method != "tools/call":
        raise MethodNotFound(f"Unknown method: {method}")
    try:
        name, args = params["name"], params.get("arguments", {})
        if name == "memory_search":
            value = memory.search(args["query"], level=args.get("level", 1))
        elif name == "memory_read":
            value = memory.read(args["id"])
        elif name == "skill_list":
            value = skills_catalog(skills)
        elif name == "skill_read":
            value = read_skill(skills, args["name"])
        elif name == "run_status":
            state = read_json(run) if run else {}
            value = {k: state.get(k) for k in ("id", "goal", "status", "accepted", "total_attempts", "replan_attempts")}
        else:
            raise HarnessError("Unknown tool")
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
    except (HarnessError, KeyError, TypeError, ValueError) as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--skills", type=Path, required=True)
    parser.add_argument("--run", type=Path)
    args = parser.parse_args()
    run_state = read_json(args.run) if args.run else {}
    repo = Path(run_state["worktree"]) if run_state.get("worktree") else (
        args.state.parent if args.state.name == ".harness" else None)
    memory = Memory(args.state / "memory.sqlite3", repo=repo)
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Request must be an object")
            if "id" not in request:
                continue
            result = handle(request, memory, args.skills, args.run)
            response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except Exception as e:
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32601 if isinstance(e, MethodNotFound) else -32603,
                                  "message": str(e)}}
        print(json.dumps(response, ensure_ascii=False), flush=True)
