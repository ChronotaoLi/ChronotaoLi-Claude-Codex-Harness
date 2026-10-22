import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from harness import git
from harness.cli import main
from harness.memory import Memory


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
                    self.assertEqual(code, 1)
                    self.assertEqual(output, "")
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

    def test_existing_legacy_database_reads_do_not_migrate_or_change_git_exclusions(self):
        self.state.mkdir()
        with sqlite3.connect(self.path) as db:
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


if __name__ == "__main__":
    unittest.main()
