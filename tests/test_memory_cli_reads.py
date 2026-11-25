import contextlib
import errno
import io
import json
import os
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from harness import git, state_files, stops
from harness.cli import main, manage_memory
from harness.mcp_server import handle
from harness.memory import Memory
from harness.util import HarnessError
from harness.versions import RUN_SCHEMA_VERSION
from test_harness import cli_notice


class MemoryCLIReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # SQLite read-only URIs must escape real filenames, including ? and #.
        self.repo = Path(self.temp.name) / "repo #? %"
        self.repo.mkdir()
        git.git(self.repo, "init", "-b", "main")
        self.state = self.repo / ".harness"
        self.path = self.state / "memory.sqlite3"
        self.exclude = self.repo / ".git" / "info" / "exclude"
        self.exclude.write_bytes(b"# Operator exclusion\n/build/\n")

    def invoke(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["--repo", str(self.repo), "--config", str(self.repo / "absent.toml"),
                         "memory", *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_missing_database_queries_do_not_create_state_or_change_git_exclusions(self):
        before = self.exclude.read_bytes()
        for arguments in (("search", "SQLite"), ("export",), ("read", "mem_missing")):
            with self.subTest(arguments=arguments):
                code, output, errors = self.invoke(*arguments)
                if arguments[0] == "read":
                    stop = cli_notice(errors)
                    self.assertEqual((code, output, stop["kind"], stop["reason"], stop["data"]["option"]),
                                     (2, "", "invalid_input", "cli_arguments", "id"))
                    self.assertIn("mem_missing", errors)
                else:
                    self.assertEqual(code, 0, errors)
                    self.assertEqual(json.loads(output), [])
                self.assertFalse(self.state.exists())
                self.assertEqual(self.exclude.read_bytes(), before)
        self.exclude.unlink()
        self.assertEqual(self.invoke("export")[0], 0)
        self.assertFalse(self.exclude.exists())
        self.assertFalse(self.state.exists())

    def test_database_probe_io_failure_is_not_an_empty_result(self):
        self.state.mkdir()
        original_exclude = self.exclude.read_bytes()
        memory = Memory(self.path, read_only=True)
        request = {"method": "tools/call", "params": {"name": "memory_search", "arguments": {"query": "storage"}}}
        self.assertEqual(json.loads(handle(request, memory, None, None)["content"][0]["text"]), [])
        self.assertEqual(memory.history("storage"), [])
        real_stat = os.stat

        def denied(path, *args, **kwargs):
            if path == self.path.name and kwargs.get("dir_fd") is not None:
                raise PermissionError(errno.EACCES, "memory probe denied")
            return real_stat(path, *args, **kwargs)

        with patch.object(state_files.os, "stat", denied):
            code, output, errors = self.invoke("export")
            stop = cli_notice(errors)
            self.assertNotEqual(code, 0)
            self.assertEqual((output, stop["kind"], stop["reason"], stop["data"]["error_type"]),
                             ("", "runtime_failure", "io", "PermissionError"))
            self.assertTrue(handle(request, memory, None, None)["isError"])
            with self.assertRaises(state_files.StateReadError):
                memory.history("storage")
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertEqual(self.exclude.read_bytes(), original_exclude)

    def test_existing_legacy_database_reads_do_not_migrate_or_change_git_exclusions(self):
        self.state.mkdir()
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE memories (
                id TEXT PRIMARY KEY, key TEXT NOT NULL, abstract TEXT NOT NULL,
                overview TEXT NOT NULL, detail TEXT NOT NULL, source TEXT NOT NULL,
                kind TEXT NOT NULL, created REAL NOT NULL, expires REAL,
                supersedes TEXT, active INTEGER NOT NULL DEFAULT 1)""")
            db.execute("""INSERT INTO memories
                (id,key,abstract,overview,detail,source,kind,created)
                VALUES ('mem_old','storage','SQLite storage','SQLite','Legacy detail','operator','operator',1)""")
        database_before, exclude_before = self.path.read_bytes(), self.exclude.read_bytes()
        for arguments in (("search", "SQLite"), ("export",), ("read", "mem_old")):
            with self.subTest(arguments=arguments):
                code, output, errors = self.invoke(*arguments)
                self.assertEqual(code, 0, errors)
                record = json.loads(output) if arguments[0] == "read" else json.loads(output)[0]
                self.assertEqual(record["id"], "mem_old")
                self.assertEqual(record["provenance"], {})
                self.assertEqual(self.path.read_bytes(), database_before)
                self.assertEqual(self.exclude.read_bytes(), exclude_before)
                self.assertEqual(list(self.state.iterdir()), [self.path])

    def test_add_creates_ignored_memory_and_queries_preserve_exclusions(self):
        code, output, errors = self.invoke("add", "--key", "storage", "--title", "Storage",
                                           "--text", "SQLite project storage")
        self.assertEqual(code, 0, errors)
        ident = output.strip()
        self.assertTrue(self.path.is_file())
        self.assertIn("/.harness/", self.exclude.read_text().splitlines())
        self.assertEqual(git.git(self.repo, "status", "--porcelain"), "")
        self.exclude.unlink()
        for arguments in (("read", ident), ("search", "SQLite"), ("export",)):
            with self.subTest(arguments=arguments):
                code, output, errors = self.invoke(*arguments)
                self.assertEqual(code, 0, errors)
                record = json.loads(output) if arguments[0] == "read" else json.loads(output)[0]
                self.assertEqual(record["id"], ident)
                self.assertFalse(self.exclude.exists())

    def test_read_only_memory_connection_cannot_write_records(self):
        Memory(self.path)
        memory = Memory(self.path, read_only=True)
        with self.assertRaises(sqlite3.OperationalError):
            memory.remember(key="blocked", abstract="Blocked write", overview="", detail="", source="test")
        self.assertEqual(memory.export(), [])

    def test_add_run_binding_and_search_run_option(self):
        ledger = self.state / "runs" / "run_current" / "state.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({"schema_version": RUN_SCHEMA_VERSION, "id": "run_current", "status": "paused",
                                      "repo": str(self.repo.resolve())}))
        code, output, errors = self.invoke("add", "--key", "decision", "--title", "Upgrade",
                                           "--text", "SQLite reference", "--run", "run_current")
        self.assertEqual(code, 0, errors)
        ident = output.strip()
        self.assertEqual(json.loads(self.invoke("search", "SQLite")[1]), [])
        records = json.loads(self.invoke("search", "SQLite", "--run", "run_current")[1])
        self.assertEqual(records[0]["id"], ident)
        self.assertEqual(records[0]["scope"], "run")
        self.assertTrue(records[0]["reference_only"])

    def test_add_rejects_invalid_source_run_before_writing_memory_or_exclusions(self):
        ledger = self.state / "runs" / "run_current" / "state.json"
        ledger.parent.mkdir(parents=True)
        original_exclude = self.exclude.read_bytes()
        current = {"schema_version": RUN_SCHEMA_VERSION, "id": "run_current", "status": "paused",
                   "repo": str(self.repo.resolve())}
        for value in ([], {**current, "repo": "/another/repo"}, {**current, "status": "unknown"},
                      # Another ledger version is not interpreted, however valid its fields look.
                      {**current, "schema_version": 18}, {key: current[key] for key in ("id", "status", "repo")}):
            ledger.write_text(json.dumps(value))
            with self.subTest(value=value):
                code, output, errors = self.invoke("add", "--key", "decision", "--title", "Upgrade",
                                                  "--text", "Reference", "--run", "run_current")
                stop = cli_notice(errors)
                self.assertEqual((code, output, stop["kind"], stop["reason"], stop["data"]["option"]),
                                 (2, "", "invalid_input", "cli_arguments", "--run"))
                self.assertIn("记忆来源运行", errors)
                self.assertFalse(self.path.exists())
                self.assertEqual(self.exclude.read_bytes(), original_exclude)

    def test_shared_read_and_search_failures_are_typed_only_at_the_memory_cli(self):
        Memory(self.path)
        memory = Memory(self.path, read_only=True)
        for method, arguments, options, message in (
            ("read", ("mem_missing",), {}, "没有此记忆"),
            ("search", (), {"key": ""}, "key 必须为非空字符串"),
        ):
            with self.subTest(method=method):
                with self.assertRaisesRegex(HarnessError, message) as caught:
                    getattr(memory, method)(*arguments, **options)
                self.assertIs(type(caught.exception), HarnessError)
                self.assertIsNone(caught.exception.stop)
                self.assertEqual(stops.of(caught.exception)["data"]["error_type"], "HarnessError")
        for argv, option in ((("read", "mem_missing"), "id"), (("search", "--key", ""), "--key")):
            code, output, errors = self.invoke(*argv)
            stop = cli_notice(errors)
            self.assertEqual((code, output, stop["kind"], stop["reason"], stop["data"]),
                             (2, "", "invalid_input", "cli_arguments", {"option": option}))

        mcp_errors = []

        def read(*args, **kwargs):
            try:
                return real_read(*args, **kwargs)
            except HarnessError as error:
                mcp_errors.append(error)
                raise

        real_read = memory.read
        request = {"id": 1, "method": "tools/call",
                   "params": {"name": "memory_read", "arguments": {"id": "mem_missing"}}}
        with patch.object(memory, "read", side_effect=read):
            response = handle(request, memory, None, None)
        self.assertEqual(response, {"isError": True,
                                   "content": [{"type": "text", "text": "没有此记忆：mem_missing"}]})
        error, = mcp_errors
        self.assertIs(type(error), HarnessError)
        self.assertIsNone(error.stop)
        self.assertEqual(stops.of(error)["data"]["error_type"], "HarnessError")

        def wrapped_read(memory, ident):
            try:
                return real_read(ident)
            except HarnessError as cause:
                raise HarnessError("wrapped memory failure") from cause

        # The CLI checks the error received from the call, even when a marked
        # input error appears deeper in its cause chain.
        with patch.object(Memory, "read", wrapped_read):
            with self.assertRaisesRegex(HarnessError, "wrapped memory failure") as caught:
                manage_memory(Namespace(memory_command="read", id="mem_missing"), self.repo, self.state)
        self.assertIsNone(caught.exception.stop)
        self.assertIsNone(caught.exception.__cause__.stop)

    def test_run_note_cannot_hide_same_key_project_fact(self):
        ledger = self.state / "runs" / "run_current" / "state.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({"schema_version": RUN_SCHEMA_VERSION, "id": "run_current", "status": "paused",
                                      "repo": str(self.repo.resolve())}))
        arguments = ("add", "--key", "storage", "--title", "Storage", "--text", "SQLite project fact")
        code, output, errors = self.invoke(*arguments)
        self.assertEqual(code, 0, errors)
        original_id = output.strip()
        before = json.loads(self.invoke("export")[1])
        code, output, errors = self.invoke(*arguments, "--run", "run_current")
        stop = cli_notice(errors)
        self.assertEqual((code, output, stop["kind"], stop["reason"], stop["data"]["option"]),
                         (2, "", "invalid_input", "cli_arguments", "--key"))
        self.assertIn("作用域", errors)
        self.assertEqual(json.loads(self.invoke("export")[1]), before)
        for extra in ((), ("--run", "run_current"), ("--run", "run_other")):
            code, output, errors = self.invoke("search", "SQLite", *extra)
            self.assertEqual(code, 0, errors)
            self.assertEqual([item["id"] for item in json.loads(output)], [original_id])


if __name__ == "__main__":
    unittest.main()
