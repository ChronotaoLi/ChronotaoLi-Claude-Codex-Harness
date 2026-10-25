import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from harness import git
from harness.memory import Memory, validate_proposals
from harness.util import HarnessError


class KnowledgeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git.git(self.repo, "init", "-b", "main")
        (self.repo / "storage.py").write_text("DATABASE = 'SQLite'\n")
        self.commit = self.commit_all("initial")
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite3", repo=self.repo)
        self.proposal = {"key": "architecture.storage", "abstract": "Storage convention",
                         "content": "The project uses SQLite storage", "source_paths": ["storage.py"]}

    def commit_all(self, message):
        git.git(self.repo, "add", ".")
        git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                "-c", "commit.gpgsign=false", "commit", "-m", message)
        return git.head(self.repo)

    def validated(self, *proposals):
        return validate_proposals(list(proposals or (self.proposal,)), self.repo)

    def promote(self, proposals=None, approved=None, ttl_days=180):
        return self.memory.remember_proposals(proposals if proposals is not None else self.validated(),
                    approved if approved is not None else [self.proposal["key"]],
                    self.commit, "run_test", "task_test", ttl_days=ttl_days)

    def test_code_acceptance_alone_cannot_promote_knowledge(self):
        self.assertEqual(self.promote(approved=[]), [])
        self.assertEqual(self.memory.export(), [])
        with self.assertRaises(HarnessError):
            self.promote(approved=["unknown"])
        self.assertEqual(self.memory.export(), [])

    def test_promotion_keeps_sources_and_only_approved_keys(self):
        another = {**self.proposal, "key": "architecture.other"}
        ids = self.promote(self.validated(self.proposal, another))
        self.assertEqual(len(ids), 1)
        record = self.memory.read(ids[0])
        self.assertEqual(record["key"], "knowledge:architecture.storage")
        self.assertEqual(record["kind"], "project_knowledge")
        self.assertEqual(record["provenance"]["commit"], self.commit)
        self.assertEqual(record["provenance"]["run_id"], "run_test")
        self.assertEqual(record["provenance"]["source_hashes"]["storage.py"],
                         hashlib.sha256((self.repo / "storage.py").read_bytes()).hexdigest())
        self.assertFalse(record["possibly_stale"])
        self.assertGreater(record["expires"], time.time())

    def test_changed_deleted_and_escaping_sources_mark_retrieved_knowledge_stale(self):
        self.promote()
        source = self.repo / "storage.py"
        source.write_text("DATABASE = 'PostgreSQL'\n")
        self.assertTrue(self.memory.search("SQLite")[0]["possibly_stale"])
        source.unlink()
        self.assertIn("source_unavailable:storage.py", self.memory.search("SQLite")[0]["stale_reasons"])
        outside = Path(self.temp.name) / "outside.py"
        outside.write_text("DATABASE = 'SQLite'\n")
        source.symlink_to(outside)
        self.assertTrue(self.memory.search("SQLite")[0]["possibly_stale"])

    def test_unmerged_worktree_commit_remains_valid_provenance(self):
        worktree = Path(self.temp.name) / "worktree"
        git.git(self.repo, "worktree", "add", "-b", "isolated", str(worktree))
        (worktree / "other.txt").write_text("independent change")
        git.git(worktree, "add", ".")
        git.git(worktree, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                "-c", "commit.gpgsign=false", "commit", "-m", "isolated acceptance")
        self.memory.repo = worktree
        self.memory.remember_proposals(validate_proposals([self.proposal], worktree), [self.proposal["key"]],
                                       git.head(worktree), "run_isolated", "task", ttl_days=None)
        self.memory.repo = self.repo
        record = self.memory.search("SQLite")[0]
        self.assertFalse(record["possibly_stale"])
        self.assertNotEqual(record["provenance"]["commit"], git.head(self.repo))

    def test_missing_commit_is_a_staleness_signal(self):
        ident = self.promote()[0]
        provenance = self.memory.read(ident)["provenance"]
        provenance["commit"] = "a" * 40
        with self.memory.connect() as db:
            db.execute("UPDATE memories SET provenance=? WHERE id=?",
                       (json.dumps(provenance), ident))
        self.assertIn("commit_unavailable", self.memory.read(ident)["stale_reasons"])

    def test_no_checkout_means_provenance_cannot_be_checked(self):
        ident = self.promote()[0]
        reopened = Memory(self.memory.path)
        self.assertTrue(reopened.read(ident)["possibly_stale"])

    def test_validation_rejects_missing_external_private_and_unbounded_sources(self):
        outside = Path(self.temp.name) / "outside.py"
        outside.write_text("secret")
        (self.repo / "escape.py").symlink_to(outside)
        private = self.repo / ".harness"
        private.mkdir()
        (private / "state").write_text("private")
        (self.repo / "alias").symlink_to(private / "state")
        for source in ("../outside.py", str(outside), "missing.py", "escape.py", ".harness/state", "alias", ".", "./", "././"):
            with self.subTest(source=source), self.assertRaises(HarnessError):
                self.validated({**self.proposal, "source_paths": [source]})
        with self.assertRaises(HarnessError):
            self.validated({**self.proposal, "source_paths": ["storage.py"] * 11})

    def test_changed_source_prevents_promotion_after_review(self):
        proposals = self.validated()
        self.memory.check_proposals(proposals, [self.proposal["key"]])
        (self.repo / "storage.py").write_text("changed after review")
        with self.assertRaisesRegex(HarnessError, "审阅后改变"):
            self.promote(proposals)
        self.assertEqual(self.memory.export(), [])

    def test_source_must_belong_to_accepted_commit(self):
        (self.repo / "storage.py").write_text("new version not committed")
        with self.assertRaisesRegex(HarnessError, "接受的提交"):
            self.promote()
        self.assertEqual(self.memory.export(), [])

    def test_promotion_cannot_omit_commit_or_source_validation(self):
        with self.assertRaises(HarnessError):
            self.memory.remember_proposals(self.validated(), [self.proposal["key"]], None, "run", "task")
        with self.assertRaises(HarnessError):
            self.promote([self.proposal])
        self.assertEqual(self.memory.export(), [])

    def test_operator_collision_rejects_entire_promotion_batch(self):
        operator = self.memory.remember(key="knowledge:architecture.storage", abstract="Operator storage",
                    overview="PostgreSQL", detail="Manual verified decision", source="operator")
        unrelated = {**self.proposal, "key": "architecture.allowed"}
        proposals = self.validated(unrelated, self.proposal)
        with self.assertRaisesRegex(HarnessError, "操作者"):
            self.memory.check_proposals(proposals, [p["key"] for p in proposals])
        with self.assertRaisesRegex(HarnessError, "操作者"):
            self.promote(proposals, approved=[p["key"] for p in proposals])
        self.assertEqual([item["id"] for item in self.memory.export()], [operator])
        self.assertTrue(self.memory.read(operator)["active"])

    def test_inactive_operator_history_still_protects_key(self):
        self.memory.remember(key="knowledge:architecture.storage", abstract="Operator storage",
                    overview="PostgreSQL", detail="Manual decision", source="operator")
        self.memory.remember(key="knowledge:architecture.storage", abstract="Migration",
                    overview="SQLite", detail="Historical automated record", source="migration", kind="project_knowledge")
        with self.assertRaises(HarnessError):
            self.promote()
        self.assertEqual(len(self.memory.export()), 2)

    def test_repeated_knowledge_retains_history_and_expiry(self):
        old = self.promote()[0]
        new = self.promote()[0]
        self.assertFalse(self.memory.read(old)["active"])
        self.assertEqual(self.memory.read(new)["supersedes"], old)
        with self.memory.connect() as db:
            db.execute("UPDATE memories SET expires=? WHERE id=?", (time.time() - 1, new))
        self.assertEqual(self.memory.search("SQLite"), [])
        self.assertTrue(self.memory.read(new)["expired"])
        self.assertEqual(len(self.memory.export()), 2)

    def test_source_verified_project_knowledge_remains_reusable_after_run_abandonment(self):
        ident = self.promote()[0]
        directory = self.memory.path.parent / "runs" / "run_test"
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(json.dumps({"id": "run_test", "status": "abandoned"}))
        record = self.memory.search("SQLite", run_id="run_new")[0]
        self.assertEqual(record["id"], ident)
        self.assertEqual(record["scope"], "project")
        self.assertEqual(record["source_run_status"], "abandoned")
        self.assertFalse(record["possibly_stale"])

    def test_kind_weight_prefers_project_knowledge_over_matching_task_history(self):
        directory = self.memory.path.parent / "runs" / "run"
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(json.dumps({"id": "run", "status": "completed", "accepted": {"task": {"commit": self.commit}}}))
        self.memory.remember(key="task:run:task", abstract=self.proposal["abstract"],
                    overview=self.proposal["content"], detail=self.proposal["content"], source="run", kind="accepted_task")
        self.promote()
        found = self.memory.search("SQLite storage")
        self.assertEqual(found[0]["kind"], "project_knowledge")
        self.assertEqual(found[1]["kind"], "accepted_task")

    def test_stale_high_weight_record_cannot_hide_fresh_record_at_limit_one(self):
        directory = self.memory.path.parent / "runs" / "run"
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(json.dumps({"id": "run", "status": "completed", "accepted": {"task": {"commit": self.commit}}}))
        self.promote()
        fresh = self.memory.remember(key="task:run:task", abstract=self.proposal["abstract"],
                    overview=self.proposal["content"], detail=self.proposal["content"], source="run", kind="accepted_task",
                    provenance={"commit": self.commit, "run_id": "run"})
        self.assertEqual(self.memory.search("SQLite storage", limit=1)[0]["kind"], "project_knowledge")
        (self.repo / "storage.py").write_text("DATABASE = 'PostgreSQL'\n")
        self.assertEqual(self.memory.search("SQLite storage", limit=1)[0]["id"], fresh)

    def test_malformed_provenance_is_flagged_without_breaking_retrieval(self):
        ident = self.promote()[0]
        cases = ["not json", "null", "[]", '{"source_hashes":null}',
                 '{"source_hashes":{"storage.py":27}}', '{"commit":"--invalid-option"}', "{}"]
        for provenance in cases:
            with self.subTest(provenance=provenance):
                with self.memory.connect() as db:
                    db.execute("UPDATE memories SET provenance=? WHERE id=?", (provenance, ident))
                record = self.memory.search("SQLite")[0]
                self.assertTrue(record["possibly_stale"])
                self.assertIn("invalid_provenance", record["stale_reasons"])
                self.assertEqual(self.memory.read(ident)["id"], ident)

    def test_storage_error_rolls_back_batch_and_restores_superseded_record(self):
        old = self.promote()[0]
        reject = {**self.proposal, "key": "architecture.reject"}
        with self.memory.connect() as db:
            db.execute("""CREATE TRIGGER reject_insert BEFORE INSERT ON memories
                WHEN NEW.key='knowledge:architecture.reject'
                BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.promote(self.validated(self.proposal, reject), approved=[self.proposal["key"], reject["key"]])
        self.assertEqual([record["id"] for record in self.memory.export()], [old])
        self.assertTrue(self.memory.read(old)["active"])

    def test_existing_database_migration_keeps_records_and_current_key(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        db = sqlite3.connect(path)
        db.execute("""CREATE TABLE memories (id TEXT PRIMARY KEY,key TEXT NOT NULL,abstract TEXT NOT NULL,
                    overview TEXT NOT NULL,detail TEXT NOT NULL,source TEXT NOT NULL,kind TEXT NOT NULL,
                    created REAL NOT NULL,expires REAL,supersedes TEXT,active INTEGER NOT NULL DEFAULT 1)""")
        db.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("mem_old", "storage", "Storage", "SQLite", "SQLite", "operator", "operator", time.time(), None, None, 1))
        db.commit()
        db.close()
        migrated = Memory(path)
        self.assertEqual(migrated.search("SQLite")[0]["id"], "mem_old")
        new = migrated.remember(key="storage", abstract="Storage", overview="PostgreSQL", detail="PostgreSQL", source="operator")
        self.assertEqual(migrated.read(new)["supersedes"], "mem_old")
        self.assertFalse(migrated.read("mem_old")["active"])


if __name__ == "__main__":
    unittest.main()
