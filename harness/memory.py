"""Project-scoped, versioned memory. Retrieval is lexical, not semantic."""
from __future__ import annotations

import math
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .util import HarnessError, uid


def terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9_./-]+", text.lower()))
    for chunk in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(chunk[i:i + 2] for i in range(max(1, len(chunk) - 1)))
    return words


class Memory:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY, key TEXT NOT NULL, abstract TEXT NOT NULL,
                overview TEXT NOT NULL, detail TEXT NOT NULL, source TEXT NOT NULL,
                kind TEXT NOT NULL, created REAL NOT NULL, expires REAL,
                supersedes TEXT, active INTEGER NOT NULL DEFAULT 1)""")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS current_key ON memories(key) WHERE active=1")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def remember(self, *, key, abstract, overview, detail, source, kind="operator", ttl_days=None):
        if not key or not abstract or not source:
            raise HarnessError("记忆必须有 key、摘要和来源")
        if len(detail) > 100000 or len(overview) > 8000 or len(abstract) > 500:
            raise HarnessError("记忆超过字段大小上限")
        now = time.time()
        if ttl_days is not None and ttl_days <= 0:
            raise HarnessError("ttl_days 必须为正数")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT id FROM memories WHERE key=? AND active=1", (key,)).fetchone()
            db.execute("UPDATE memories SET active=0 WHERE key=?", (key,))
            ident = uid("mem")
            db.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                       (ident, key, abstract, overview, detail, source, kind, now,
                        now + ttl_days * 86400 if ttl_days else None, old[0] if old else None))
        return ident

    def read(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id=?", (ident,)).fetchone()
        if not row:
            raise HarnessError(f"没有此记忆：{ident}")
        result = dict(row)
        result["expired"] = result["expires"] is not None and result["expires"] < time.time()
        return result

    def search(self, query, limit=6, level=1):
        if level not in (0, 1, 2) or not 1 <= limit <= 20:
            raise HarnessError("level 应为 0/1/2，limit 应为 1..20")
        now, q = time.time(), terms(query)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM memories WHERE active=1 AND (expires IS NULL OR expires>?)", (now,)).fetchall()
        found = []
        for row in rows:
            item = dict(row)
            tokens = terms(item["abstract"] + " " + item["overview"] + " " + item["detail"])
            overlap = len(q & tokens)
            if q and not overlap:
                continue
            age = (now - item["created"]) / 86400
            score = (overlap / max(1, len(q))) * 10 + math.exp(-age / 90)
            selected = {k: item[k] for k in ("id", "key", "abstract", "source", "kind", "created")}
            if level >= 1:
                selected["overview"] = item["overview"]
            if level >= 2:
                selected["detail"] = item["detail"]
            found.append((score, selected))
        return [r for _, r in sorted(found, key=lambda pair: pair[0], reverse=True)[:limit]]

    def export(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM memories ORDER BY created").fetchall()
        return [dict(r) for r in rows]
