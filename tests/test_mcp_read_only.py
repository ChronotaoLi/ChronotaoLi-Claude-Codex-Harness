import contextlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from test_harness import ROOT


class MCPReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / ".harness"
        self.database = self.state / "memory.sqlite3"

    def call(self, requests):
        process = subprocess.run(
            [sys.executable, str(ROOT / "harness" / "mcp_entry.py"), "--state", str(self.state),
             "--skills", str(ROOT / "shared" / "skills")], cwd=ROOT,
            input="".join(json.dumps(request) + "\n" for request in requests),
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        return [json.loads(line) for line in process.stdout.splitlines()]

    @staticmethod
    def request(ident, name, arguments=None):
        return {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}}}

    def test_missing_database_protocol_reads_do_not_initialize_state(self):
        responses = self.call([
            {"jsonrpc": "2.0", "id": 0, "method": "initialize"},
            self.request(1, "memory_search", {"query": "storage"}),
            self.request(2, "memory_read", {"id": "missing"}),
            self.request(3, "skill_list"),
            {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
        ])
        self.assertEqual(json.loads(responses[1]["result"]["content"][0]["text"]), [])
        self.assertFalse(responses[1]["result"]["isError"])
        self.assertTrue(responses[2]["result"]["isError"])
        self.assertFalse(responses[3]["result"]["isError"])
        tools = {tool["name"]: tool for tool in responses[4]["result"]["tools"]}
        for tool in tools.values():
            with self.subTest(tool=tool["name"]):
                schema = tool["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertFalse(set(schema) & {"oneOf", "anyOf", "allOf"})
        search_schema = tools["memory_search"]["inputSchema"]
        self.assertEqual(set(search_schema["properties"]), {"query", "key", "level"})
        self.assertFalse(search_schema["additionalProperties"])
        self.assertFalse(self.state.exists())

    def test_file_tools_protocol_preserves_files_and_missing_state(self):
        source = Path(self.temp.name) / "untracked.json"
        source.write_bytes(b'{"value": 1, "value": 2}')
        before = source.read_bytes()
        responses = self.call([
            {"jsonrpc": "2.0", "id": 0, "method": "tools/list"},
            self.request(1, "file_sha256", {"path": str(source)}),
            self.request(2, "json_check", {"path": str(source)}),
            self.request(3, "file_diff", {"left": str(source), "right": str(source)}),
            self.request(4, "json_check", {"path": str(source), "write": True}),
            self.request(5, "file_sha256", {"path": str(source.parent / "missing")}),
        ])
        tools = {tool["name"]: tool for tool in responses[0]["result"]["tools"]}
        for name in ("file_sha256", "json_check", "file_diff"):
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
            self.assertFalse(tools[name]["inputSchema"]["additionalProperties"])
        self.assertFalse(responses[1]["result"]["isError"])
        invalid = responses[2]["result"]
        self.assertFalse(invalid["isError"])
        self.assertFalse(json.loads(invalid["content"][0]["text"])["valid"])
        self.assertFalse(json.loads(responses[3]["result"]["content"][0]["text"])["different"])
        self.assertTrue(responses[4]["result"]["isError"])
        self.assertTrue(responses[5]["result"]["isError"])
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse(self.state.exists())

    def test_legacy_database_protocol_reads_do_not_migrate_or_create_init_lock(self):
        self.state.mkdir()
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""CREATE TABLE memories (
                id TEXT PRIMARY KEY, key TEXT NOT NULL, abstract TEXT NOT NULL,
                overview TEXT NOT NULL, detail TEXT NOT NULL, source TEXT NOT NULL,
                kind TEXT NOT NULL, created REAL NOT NULL, expires REAL,
                supersedes TEXT, active INTEGER NOT NULL DEFAULT 1)""")
            db.execute("""INSERT INTO memories (id,key,abstract,overview,detail,source,kind,created)
                VALUES ('mem_old','storage','SQLite storage','SQLite','Stored detail','operator','operator',1)""")
        before = self.database.read_bytes()
        responses = self.call([
            self.request(1, "memory_search", {"query": "SQLite"}),
            self.request(2, "memory_read", {"id": "mem_old"}),
        ])
        self.assertTrue(all(not response["result"]["isError"] for response in responses))
        record = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertEqual(record["id"], "mem_old")
        self.assertEqual(record["provenance"], {})
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(list(self.state.iterdir()), [self.database])


if __name__ == "__main__":
    unittest.main()
