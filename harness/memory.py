"""Project-scoped, versioned memory. Retrieval is lexical, not semantic."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .contracts import PROPOSALS, REVIEW, validate
from .util import HarnessError, uid


MAX_SOURCE_BYTES = 2 * 1024 * 1024
KIND_WEIGHTS = {"operator": 1.15, "project_knowledge": 1.1, "accepted_task": 0.9}


def terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9_./-]+", text.lower()))
    for chunk in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(chunk[i:i + 2] for i in range(max(1, len(chunk) - 1)))
    return words


def _source_path(root, name):
    relative = PurePosixPath(name)
    if (not name or not relative.parts or "\\" in name or "\0" in name or relative.is_absolute()
            or ".." in relative.parts or relative.parts[0] in (".git", ".harness")):
        raise HarnessError(f"无效记忆来源路径：{name}")
    try:
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise HarnessError(f"记忆来源必须是工作树内文件：{name}")
        if path.relative_to(root).parts[0] in (".git", ".harness"):
            raise HarnessError(f"记忆来源不能读取私有状态：{name}")
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise HarnessError(f"记忆来源文件过大：{name}")
        return path
    except (OSError, RuntimeError) as error:
        raise HarnessError(f"无法读取记忆来源：{name}") from error


def _source_hash(root, name):
    path = _source_path(root, name)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise HarnessError(f"无法读取记忆来源：{name}") from error
    if len(data) > MAX_SOURCE_BYTES:
        raise HarnessError(f"记忆来源文件过大：{name}")
    return hashlib.sha256(data).hexdigest()


def validate_proposals(proposals, worktree):
    """Bind bounded, source-backed proposals to the files independently reviewed."""
    validate(proposals, PROPOSALS, "proposed_memories")
    keys = [proposal["key"] for proposal in proposals]
    if len(keys) != len(set(keys)):
        raise HarnessError("记忆提案 key 必须唯一")
    root = Path(worktree).resolve()
    return [{**proposal, "source_paths": list(proposal["source_paths"]),
             "source_hashes": {name: _source_hash(root, name) for name in proposal["source_paths"]}}
            for proposal in proposals]


class Memory:
    def __init__(self, path: Path, repo=None):
        self.path = Path(path)
        self.repo = Path(repo).resolve() if repo is not None else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY, key TEXT NOT NULL, abstract TEXT NOT NULL,
                overview TEXT NOT NULL, detail TEXT NOT NULL, source TEXT NOT NULL,
                kind TEXT NOT NULL, created REAL NOT NULL, expires REAL,
                supersedes TEXT, active INTEGER NOT NULL DEFAULT 1,
                provenance TEXT NOT NULL DEFAULT '{}')""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(memories)")}
            if "provenance" not in columns:
                db.execute("ALTER TABLE memories ADD COLUMN provenance TEXT NOT NULL DEFAULT '{}'")
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

    @staticmethod
    def _validate_record(key, abstract, overview, detail, source, ttl_days):
        if not key or not abstract or not source:
            raise HarnessError("记忆必须有 key、摘要和来源")
        if len(detail) > 100000 or len(overview) > 8000 or len(abstract) > 500:
            raise HarnessError("记忆超过字段大小上限")
        if ttl_days is not None and (isinstance(ttl_days, bool) or not isinstance(ttl_days, (int, float))
                                     or not math.isfinite(ttl_days) or ttl_days <= 0):
            raise HarnessError("ttl_days 必须为正数")

    @staticmethod
    def _insert(db, *, key, abstract, overview, detail, source, kind, ttl_days, provenance):
        now = time.time()
        old = db.execute("SELECT id FROM memories WHERE key=? AND active=1", (key,)).fetchone()
        db.execute("UPDATE memories SET active=0 WHERE key=?", (key,))
        ident = uid("mem")
        db.execute("""INSERT INTO memories
            (id,key,abstract,overview,detail,source,kind,created,expires,supersedes,active,provenance)
            VALUES (?,?,?,?,?,?,?,?,?,?,1,?)""",
                   (ident, key, abstract, overview, detail, source, kind, now,
                    now + ttl_days * 86400 if ttl_days else None, old[0] if old else None,
                    json.dumps(provenance or {}, ensure_ascii=False)))
        return ident

    def remember(self, *, key, abstract, overview, detail, source, kind="operator", ttl_days=None,
                 provenance=None):
        self._validate_record(key, abstract, overview, detail, source, ttl_days)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._insert(db, key=key, abstract=abstract, overview=overview, detail=detail,
                                source=source, kind=kind, ttl_days=ttl_days, provenance=provenance)

    def _check_proposals(self, db, proposals, approved_keys, commit):
        if not isinstance(proposals, list):
            raise HarnessError("记忆提案必须为列表")
        raw = []
        for proposal in proposals:
            if not isinstance(proposal, dict) or set(proposal) != set(PROPOSALS["items"]["required"]) | {"source_hashes"}:
                raise HarnessError("记忆提案必须先验证来源文件")
            raw.append({key: value for key, value in proposal.items() if key != "source_hashes"})
        validate(raw, PROPOSALS, "proposed_memories")
        validate(approved_keys, REVIEW["properties"]["approved_memory_keys"], "approved_memory_keys")
        keys = [proposal["key"] for proposal in proposals]
        if len(keys) != len(set(keys)) or not set(approved_keys) <= set(keys):
            raise HarnessError("记忆提案 key 重复或批准了不存在的提案")
        approved = [proposal for proposal in proposals if proposal["key"] in approved_keys]
        if approved and self.repo is None:
            raise HarnessError("提升项目知识需要工作树以复核来源")
        if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", commit)):
            raise HarnessError("记忆来源提交必须为完整 Git hash")
        root = Path(self.repo).resolve() if self.repo is not None else None
        for proposal in approved:
            hashes = proposal["source_hashes"]
            if (not isinstance(hashes, dict) or set(hashes) != set(proposal["source_paths"])
                    or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                           for value in hashes.values())):
                raise HarnessError("记忆提案缺少有效来源摘要")
            key = "knowledge:" + proposal["key"]
            if db.execute("SELECT 1 FROM memories WHERE key=? AND kind!='project_knowledge' LIMIT 1", (key,)).fetchone():
                raise HarnessError(f"自动知识不得覆盖操作者记录：{key}")
            for name, expected in hashes.items():
                if _source_hash(root, name) != expected:
                    raise HarnessError(f"记忆来源在审阅后改变：{name}")
                if commit is not None:
                    try:
                        blob = subprocess.run(["git", "-C", str(root), "show", f"{commit}:{name}"],
                                              capture_output=True, timeout=10)
                    except (OSError, subprocess.TimeoutExpired) as error:
                        raise HarnessError(f"无法核对记忆来源提交：{name}") from error
                    if blob.returncode or hashlib.sha256(blob.stdout).hexdigest() != expected:
                        raise HarnessError(f"记忆来源未包含在接受的提交中：{name}")
        return approved

    def check_proposals(self, proposals, approved_keys, commit=None):
        """Preflight optional promotion; remember_proposals rechecks under a write lock."""
        with self.connect() as db:
            return self._check_proposals(db, proposals, approved_keys, commit)

    def remember_proposals(self, proposals, approved_keys, commit, run_id, task_id, ttl_days=180):
        """Atomically promote only independently approved keys; preserve operator ownership."""
        self._validate_record("knowledge", "knowledge", "", "", "review", ttl_days)
        if commit is None:
            raise HarnessError("提升项目知识必须提供接受的提交")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            approved = self._check_proposals(db, proposals, approved_keys, commit)
            identifiers = []
            for proposal in approved:
                provenance = {"commit": commit, "run_id": run_id, "task_id": task_id,
                              "source_hashes": proposal["source_hashes"]}
                identifiers.append(self._insert(db, key="knowledge:" + proposal["key"],
                    abstract=proposal["abstract"], overview=proposal["content"], detail=proposal["content"],
                    source=f"git:{commit};run:{run_id};task:{task_id}", kind="project_knowledge",
                    ttl_days=ttl_days, provenance=provenance))
            return identifiers

    @staticmethod
    def _decode(row):
        result = dict(row)
        try:
            result["provenance"] = json.loads(result.get("provenance") or "{}")
        except (ValueError, TypeError):
            # Keep malformed historical data visible for audit; retrieval flags it.
            pass
        return result

    def _staleness(self, item, source_cache=None, commit_cache=None):
        provenance = item["provenance"]
        invalid = {"possibly_stale": True, "stale_reasons": ["invalid_provenance"]}
        if not isinstance(provenance, dict):
            return invalid
        if not provenance:
            if item["kind"] == "project_knowledge":
                return invalid
            return {"possibly_stale": False, "stale_reasons": []}
        hashes = provenance.get("source_hashes", {})
        commit = provenance.get("commit")
        if (not isinstance(hashes, dict) or len(hashes) > 10
                or any(not isinstance(name, str) or not isinstance(value, str)
                       or not re.fullmatch(r"[a-f0-9]{64}", value) for name, value in hashes.items())
                or commit is not None and (not isinstance(commit, str)
                    or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", commit))):
            return invalid
        if item["kind"] == "project_knowledge" and (not commit or not hashes):
            return invalid
        if self.repo is None:
            return {"possibly_stale": True, "stale_reasons": ["source_not_checked"]}
        reasons = []
        root = Path(self.repo).resolve()
        source_cache = source_cache if source_cache is not None else {}
        commit_cache = commit_cache if commit_cache is not None else {}
        for name, expected in hashes.items():
            if name not in source_cache:
                try:
                    source_cache[name] = _source_hash(root, name)
                except HarnessError:
                    source_cache[name] = None
            if source_cache[name] is None:
                reasons.append(f"source_unavailable:{name}")
            elif source_cache[name] != expected:
                reasons.append(f"source_changed:{name}")
        if commit:
            # Accepted commits may live on a separate worktree branch. Ancestor-of-HEAD
            # checks would incorrectly invalidate their provenance in the main checkout.
            if commit not in commit_cache:
                try:
                    result = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{commit}^{{commit}}"],
                                            capture_output=True, timeout=10)
                    commit_cache[commit] = "commit_unavailable" if result.returncode else None
                except (OSError, subprocess.TimeoutExpired):
                    commit_cache[commit] = "commit_not_checked"
            if commit_cache[commit]:
                reasons.append(commit_cache[commit])
        return {"possibly_stale": bool(reasons), "stale_reasons": reasons}

    def read(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id=?", (ident,)).fetchone()
        if not row:
            raise HarnessError(f"没有此记忆：{ident}")
        result = self._decode(row)
        result["expired"] = result["expires"] is not None and result["expires"] < time.time()
        result.update(self._staleness(result))
        return result

    def search(self, query, limit=6, level=1):
        if level not in (0, 1, 2) or not 1 <= limit <= 20:
            raise HarnessError("level 应为 0/1/2，limit 应为 1..20")
        now, q = time.time(), terms(query)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM memories WHERE active=1 AND (expires IS NULL OR expires>?)", (now,)).fetchall()
        found, source_cache, commit_cache = [], {}, {}
        for row in rows:
            item = self._decode(row)
            tokens = terms(item["abstract"] + " " + item["overview"] + " " + item["detail"])
            overlap = len(q & tokens)
            if q and not overlap:
                continue
            age = max(0, (now - item["created"]) / 86400)
            score = ((overlap / max(1, len(q))) * 10 + math.exp(-age / 90)) * KIND_WEIGHTS.get(item["kind"], 1)
            item.update(self._staleness(item, source_cache, commit_cache))
            if item["possibly_stale"]:
                score *= 0.7
            found.append((score, item))
        results = []
        for _, item in sorted(found, key=lambda pair: pair[0], reverse=True)[:limit]:
            selected = {key: item[key] for key in ("id", "key", "abstract", "source", "kind", "created", "provenance")}
            if level >= 1:
                selected["overview"] = item["overview"]
            if level >= 2:
                selected["detail"] = item["detail"]
            selected.update({key: item[key] for key in ("possibly_stale", "stale_reasons")})
            results.append(selected)
        return results

    def export(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM memories ORDER BY created").fetchall()
        return [self._decode(row) for row in rows]
