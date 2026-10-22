import contextlib
import multiprocessing
import sqlite3
import tempfile
import traceback
import unittest
from pathlib import Path

from harness.memory import Memory


def initialize_and_remember(path, index, ready, results):
    try:
        ready.wait(timeout=15)
        memory = Memory(Path(path))
        memory.remember(key=f"concurrent:{index}", abstract=f"Writer {index}",
                        overview="Concurrent initialization", detail="Stored after initialization",
                        source="test", operation_id=f"initialize:{index}")
        results.put((index, None))
    except BaseException:
        results.put((index, traceback.format_exc()))


class MemoryInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "memory.sqlite3"

    def initialize_concurrently(self):
        context = multiprocessing.get_context("spawn")
        count = 8
        ready = context.Barrier(count)
        results = context.Queue()
        processes = [context.Process(target=initialize_and_remember,
                                     args=(str(self.path), index, ready, results))
                     for index in range(count)]
        try:
            for process in processes:
                process.start()
            reports = [results.get(timeout=30) for _ in processes]
            self.assertEqual({index for index, _ in reports}, set(range(count)))
            self.assertEqual([error for _, error in reports if error], [])
            for process in processes:
                process.join(timeout=5)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                if process.pid is not None:
                    process.join(timeout=5)
            results.close()
            results.join_thread()
        memory = Memory(self.path)
        records = {record["key"]: record for record in memory.export()}
        for index in range(count):
            key = f"concurrent:{index}"
            self.assertIn(key, records)
            # Initialization must also install the idempotent-operation table.
            repeated = memory.remember(key=key, abstract=f"Writer {index}",
                                       overview="Concurrent initialization", detail="Stored after initialization",
                                       source="test", operation_id=f"initialize:{index}")
            self.assertEqual(repeated, records[key]["id"])
        with memory.connect() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        return memory

    def test_processes_initialize_a_new_database_and_preserve_all_writes(self):
        memory = self.initialize_concurrently()
        self.assertEqual(len(memory.export()), 8)

    def test_processes_migrate_a_legacy_database_without_losing_history(self):
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE memories (
                id TEXT PRIMARY KEY, key TEXT NOT NULL, abstract TEXT NOT NULL,
                overview TEXT NOT NULL, detail TEXT NOT NULL, source TEXT NOT NULL,
                kind TEXT NOT NULL, created REAL NOT NULL, expires REAL,
                supersedes TEXT, active INTEGER NOT NULL DEFAULT 1)""")
            db.execute("""INSERT INTO memories
                (id,key,abstract,overview,detail,source,kind,created)
                VALUES ('mem_old','storage','Storage','SQLite','Historical record','operator','operator',1)""")
        memory = self.initialize_concurrently()
        self.assertEqual(len(memory.export()), 9)
        self.assertEqual(memory.read("mem_old")["provenance"], {})
        new = memory.remember(key="storage", abstract="Storage", overview="New storage",
                              detail="Updated record", source="operator")
        self.assertEqual(memory.read(new)["supersedes"], "mem_old")
        self.assertFalse(memory.read("mem_old")["active"])


if __name__ == "__main__":
    unittest.main()
