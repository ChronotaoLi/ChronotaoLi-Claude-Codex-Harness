"""Project-scoped, versioned memory. Retrieval is lexical, not semantic."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from . import git
from .contracts import PROPOSALS, REVIEW, validate
from .util import HarnessError, uid


MAX_SOURCE_BYTES = 2 * 1024 * 1024
KIND_WEIGHTS = {"operator": 1.15, "project_knowledge": 1.1, "accepted_task": 0.9}


def terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9_./-]+", text.lower()))
    for chunk in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(chunk[i:i + 2] for i in range(max(1, len(chunk) - 1)))
    return words


def normalize_source_path(name):
    """Validate before lexical normalization; never change stored historical paths."""
    if not isinstance(name, str):
        raise HarnessError("无效记忆来源路径")
    relative = PurePosixPath(name)
    if (not name or not relative.parts or "\\" in name or "\0" in name
            or relative.is_absolute() or ".." in relative.parts
            or relative.parts[0] in (".git", ".harness")):
        raise HarnessError(f"无效记忆来源路径：{name}")
    return relative.as_posix()


def bind_source_files(repo, names):
    """Bind a complete batch to one HEAD using raw regular-file blob bytes."""
    root = Path(repo).resolve()
    names = list(dict.fromkeys(normalize_source_path(name) for name in names))
    if len(names) > 10:
        raise HarnessError("记忆来源最多十个不同文件")
    if not names:
        return {}
    commit = git.git(root, "rev-parse", "--verify", "HEAD").strip()
    hashes = {}
    for name in names:
        tree = git.git_process(root, "ls-tree", "-z", commit, "--", ":(literal)" + name)
        entries = tree.stdout.split(b"\0") if not tree.returncode else []
        entry = next((entry for entry in entries if b"\t" in entry
                      and entry.split(b"\t", 1)[1] == name.encode("utf-8")), None)
        if entry is None:
            raise HarnessError(f"HEAD 无此精确路径：{name}")
        metadata = entry.split(b"\t", 1)[0].split()
        if metadata[0] == b"120000":
            raise HarnessError(f"不支持符号链接来源：{name}")
        if metadata[0] not in (b"100644", b"100755") or metadata[1] != b"blob":
            raise HarnessError(f"来源不是普通文件：{name}")
        current = root
        for part in PurePosixPath(name).parts:
            current = current / part
            if current.is_symlink():
                raise HarnessError(f"不支持符号链接来源：{name}")
        path = _source_path(root, name)
        size = git.git(root, "cat-file", "-s", metadata[2].decode("ascii"))
        if int(size) > MAX_SOURCE_BYTES:
            raise HarnessError(f"记忆来源文件过大：{name}")
        blob = git.git_process(root, "cat-file", "blob", metadata[2].decode("ascii"))
        data = path.read_bytes()
        if len(data) > MAX_SOURCE_BYTES:
            raise HarnessError(f"记忆来源文件过大：{name}")
        if blob.returncode or data != blob.stdout:
            raise HarnessError(f"工作区与 blob 原始字节不同：{name}")
        hashes[name] = hashlib.sha256(data).hexdigest()
    return {"commit": commit, "source_hashes": hashes}


def source_status(item):
    codes = {reason.split(":", 1)[0] for reason in item["stale_reasons"]}
    provenance = item.get("provenance")
    if "invalid_provenance" in codes:
        return "invalid"
    if not isinstance(provenance, dict) or not (provenance.get("source_hashes") or provenance.get("commit")):
        return "unbound"
    if "source_changed" in codes:
        return "changed"
    if codes & {"source_unavailable", "commit_unavailable"}:
        return "unavailable"
    if codes - {"missing_provenance"}:
        return "not_checked"
    return "matching"


def ranking_group(item):
    codes = {reason.split(":", 1)[0] for reason in item["stale_reasons"]}
    return "needs_review" if codes - {"missing_provenance", "source_not_checked", "commit_not_checked"} else "preferred"


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
    def __init__(self, path: Path, repo=None, *, read_only=False, binding=None):
        self.path = Path(path)
        self.repo = Path(repo).resolve() if repo is not None else None
        self.binding = binding
        self.read_only = read_only
        if read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # WAL setup and read-then-ALTER migrations need a single initializer.
        # Wait only for this database's short initialization, not another run.
        # Resolve aliases so callers opening the same database share its lock.
        database = self.path.resolve()
        with database.with_name(database.name + ".init.lock").open("a+") as initialization:
            fcntl.flock(initialization, fcntl.LOCK_EX)
            try:
                self._initialize()
            finally:
                fcntl.flock(initialization, fcntl.LOCK_UN)

    def _initialize(self):
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
            db.execute("""CREATE TABLE IF NOT EXISTS memory_operations (
                id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, result TEXT NOT NULL)""")

    @contextmanager
    def connect(self):
        if self.read_only:
            db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        else:
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

    @classmethod
    def _insert(cls, db, *, key, abstract, overview, detail, source, kind, ttl_days, provenance):
        now = time.time()
        old = db.execute("SELECT * FROM memories WHERE key=? AND active=1", (key,)).fetchone()
        if old:
            # Keys remain global. Legacy unscoped records share the project scope;
            # run-bound records can only replace versions from that same run.
            def identity(item):
                scope, source_run = cls._scope(item)
                if scope in ("project", "unscoped"):
                    return ("project", None)
                if scope == "run" and isinstance(source_run, str) and source_run:
                    return ("run", source_run)
                return None

            old_scope = identity(cls._decode(old))
            new_scope = identity({"key": key, "kind": kind, "source": source,
                                  "provenance": provenance})
            if old_scope is None or new_scope != old_scope:
                raise HarnessError(f"记忆 key 已绑定其他作用域，不能替代：{key}")
        db.execute("UPDATE memories SET active=0 WHERE key=?", (key,))
        ident = uid("mem")
        db.execute("""INSERT INTO memories
            (id,key,abstract,overview,detail,source,kind,created,expires,supersedes,active,provenance)
            VALUES (?,?,?,?,?,?,?,?,?,?,1,?)""",
                   (ident, key, abstract, overview, detail, source, kind, now,
                    now + ttl_days * 86400 if ttl_days else None, old[0] if old else None,
                    json.dumps(provenance or {}, ensure_ascii=False)))
        return ident

    @staticmethod
    def _operation_digest(operation_id, request):
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 256:
            raise HarnessError("记忆写入操作 ID 无效")
        data = json.dumps(request, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def _operation_result(self, db, operation_id, request):
        if operation_id is None:
            return None
        digest = self._operation_digest(operation_id, request)
        row = db.execute("SELECT request_digest,result FROM memory_operations WHERE id=?",
                         (operation_id,)).fetchone()
        if row:
            if row["request_digest"] != digest:
                raise HarnessError("记忆写入操作 ID 已绑定其他内容")
            return json.loads(row["result"])
        return None

    def _complete_operation(self, db, operation_id, request, result):
        if operation_id is not None:
            db.execute("INSERT INTO memory_operations (id,request_digest,result) VALUES (?,?,?)",
                       (operation_id, self._operation_digest(operation_id, request), json.dumps(result)))

    def remember(self, *, key, abstract, overview, detail, source, kind="operator", ttl_days=None,
                 provenance=None, operation_id=None):
        self._validate_record(key, abstract, overview, detail, source, ttl_days)
        record = dict(key=key, abstract=abstract, overview=overview, detail=detail,
                      source=source, kind=kind, ttl_days=ttl_days, provenance=provenance)
        request = {"method": "remember", **record}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._operation_result(db, operation_id, request)
            if previous is not None:
                return previous
            ident = self._insert(db, **record)
            self._complete_operation(db, operation_id, request, ident)
            return ident

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
                        blob = git.git_process(root, "show", f"{commit}:{name}",
                                               binding=self.binding, timeout=10)
                    except (OSError, subprocess.TimeoutExpired) as error:
                        raise HarnessError(f"无法核对记忆来源提交：{name}") from error
                    if blob.returncode or hashlib.sha256(blob.stdout).hexdigest() != expected:
                        raise HarnessError(f"记忆来源未包含在接受的提交中：{name}")
        return approved

    def check_proposals(self, proposals, approved_keys, commit=None):
        """Preflight optional promotion; remember_proposals rechecks under a write lock."""
        with self.connect() as db:
            return self._check_proposals(db, proposals, approved_keys, commit)

    def remember_proposals(self, proposals, approved_keys, commit, run_id, task_id, ttl_days=180,
                           *, operation_id=None):
        """Atomically promote only independently approved keys; preserve operator ownership."""
        self._validate_record("knowledge", "knowledge", "", "", "review", ttl_days)
        if commit is None:
            raise HarnessError("提升项目知识必须提供接受的提交")
        request = {"method": "remember_proposals", "proposals": proposals, "approved_keys": approved_keys,
                   "commit": commit, "run_id": run_id, "task_id": task_id, "ttl_days": ttl_days}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._operation_result(db, operation_id, request)
            if previous is not None:
                return previous
            approved = self._check_proposals(db, proposals, approved_keys, commit)
            identifiers = []
            for proposal in approved:
                provenance = {"commit": commit, "run_id": run_id, "task_id": task_id,
                              "source_hashes": proposal["source_hashes"]}
                identifiers.append(self._insert(db, key="knowledge:" + proposal["key"],
                    abstract=proposal["abstract"], overview=proposal["content"], detail=proposal["content"],
                    source=f"git:{commit};run:{run_id};task:{task_id}", kind="project_knowledge",
                    ttl_days=ttl_days, provenance=provenance))
            self._complete_operation(db, operation_id, request, identifiers)
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
            return {"possibly_stale": True, "stale_reasons": ["missing_provenance"]}
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
                    result = git.git_process(root, "cat-file", "-e", f"{commit}^{{commit}}",
                                             binding=self.binding, timeout=10)
                    commit_cache[commit] = "commit_unavailable" if result.returncode else None
                except (OSError, subprocess.TimeoutExpired):
                    commit_cache[commit] = "commit_not_checked"
            if commit_cache[commit]:
                reasons.append(commit_cache[commit])
        return {"possibly_stale": bool(reasons), "stale_reasons": reasons}

    def _source_ledger(self, source_run):
        try:
            ledger_path = self.path.parent / "runs" / source_run / "state.json"
            if not ledger_path.resolve().is_relative_to((self.path.parent / "runs").resolve()):
                raise ValueError("source ledger outside state")
            with ledger_path.open("rb") as stream:
                data = stream.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise ValueError("oversized source ledger")
            ledger = json.loads(data)
            if not isinstance(ledger, dict):
                raise ValueError("invalid source ledger")
            status = ledger.get("status") if ledger.get("id") == source_run else "invalid"
            if not isinstance(status, str) or status not in {
                    "planning", "running", "blocked", "interrupted", "awaiting_approval",
                    "paused", "completed", "abandoned"}:
                status = "invalid"
            if self.path.parent.name == ".harness":
                if not isinstance(ledger.get("repo"), str) or Path(ledger["repo"]).resolve() != self.path.parent.parent.resolve():
                    status = "invalid"
            return status, ledger
        except (OSError, ValueError, TypeError, RecursionError):
            return "missing", {}

    @staticmethod
    def _scope(item):
        """Resolve scope consistently for writes and reads, including legacy tasks."""
        provenance = item.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        source_run = provenance.get("run_id")
        if item["kind"] == "accepted_task" and not source_run:
            match = re.search(r"(?:^|;)run:([^;]+)", item["source"])
            source_run = match.group(1) if match else None
            if not source_run and item["key"].startswith("task:"):
                parts = item["key"].split(":", 2)
                source_run = parts[1] if len(parts) == 3 else None
        scope = provenance.get("scope", ("run" if item["kind"] == "accepted_task"
                                              else "project" if item["kind"] == "project_knowledge"
                                              else "unscoped"))
        if item["kind"] == "accepted_task":
            scope = "run"
        return scope, source_run

    def _applicability(self, item, run_id=None, ledger_cache=None):
        """Project knowledge is reusable evidence; task history is run-bound context."""
        scope, source_run = self._scope(item)
        provenance = item.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        status = None
        if source_run:
            status = "missing"
            if isinstance(source_run, str) and re.fullmatch(r"[A-Za-z0-9_-]+", source_run):
                ledger_cache = ledger_cache if ledger_cache is not None else {}
                if source_run not in ledger_cache:
                    ledger_cache[source_run] = self._source_ledger(source_run)
                status, ledger = ledger_cache[source_run]
                try:
                    if item["kind"] == "accepted_task" and status != "missing":
                        task_id = provenance.get("task_id")
                        if not task_id and item["key"].startswith(f"task:{source_run}:"):
                            task_id = item["key"].split(":", 2)[2]
                        accepted = ledger.get("accepted", {})
                        record = accepted.get(task_id) if isinstance(accepted, dict) and isinstance(task_id, str) else None
                        if not isinstance(record, dict):
                            status = "invalid"
                        elif provenance.get("commit") and record.get("commit") != provenance["commit"]:
                            status = "invalid"
                except (OSError, ValueError, KeyError, TypeError, RecursionError):
                    status = "missing"
        applicable = isinstance(scope, str) and scope in ("project", "unscoped", "run")
        if scope == "run":
            applicable = bool(source_run and source_run == run_id)
            if item["kind"] == "accepted_task":
                applicable = status not in (None, "abandoned", "missing", "invalid")
        return {"scope": scope, "source_run_id": source_run, "source_run_status": status,
                "reference_only": True, "applicable": applicable,
                "historical": bool(source_run and source_run != run_id)}

    def read(self, ident, *, run_id=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id=?", (ident,)).fetchone()
        if not row:
            raise HarnessError(f"没有此记忆：{ident}")
        result = self._decode(row)
        result["expired"] = result["expires"] is not None and result["expires"] < time.time()
        result.update(self._staleness(result))
        result.update(self._applicability(result, run_id))
        result["source_status"] = source_status(result)
        return result

    @staticmethod
    def validate_search(query=None, *, key=None, level=1, limit=6):
        if (query is None) == (key is None):
            raise HarnessError("query 和 key 必须且只能提供一个")
        if query is not None and not isinstance(query, str) or key is not None and (not isinstance(key, str) or not key):
            raise HarnessError("query 必须为字符串，key 必须为非空字符串")
        if isinstance(level, bool) or not isinstance(level, int) or level not in (0, 1, 2) or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise HarnessError("level 应为 0/1/2，limit 应为 1..20")

    def search(self, query=None, limit=6, level=1, *, key=None, run_id=None, diagnostics=None):
        self.validate_search(query, key=key, level=level, limit=limit)
        text = key if key is not None else query
        now, q = time.time(), terms(query) if query is not None else set()
        with self.connect() as db:
            rows = db.execute("SELECT * FROM memories WHERE active=1 AND (expires IS NULL OR expires>?)", (now,)).fetchall()
        found, excluded, source_cache, commit_cache, ledger_cache = [], [], {}, {}, {}
        scoped, unmatched = [], []
        counts = dict(active_candidates=len(rows), matched=0, scope_excluded=0, applicable=0,
                      returned=0, limit_excluded=0)
        for row in rows:
            item = self._decode(row)
            body = terms(item["abstract"] + " " + item["overview"] + " " + item["detail"])
            keys = {item["key"].lower()}
            if item["key"].startswith(("knowledge:", "task:")):
                keys.add(item["key"].split(":", 1)[1].lower())
            paths = set()
            provenance = item.get("provenance")
            hashes = provenance.get("source_hashes", {}) if isinstance(provenance, dict) else {}
            if isinstance(hashes, dict):
                for name in hashes:
                    try:
                        paths.add(normalize_source_path(name).lower())
                    except HarnessError:
                        pass
            matches = {"body": q & body, "key": q & keys, "path": q & paths}
            matched = set().union(*matches.values())
            detail = {"id": item["id"], "key": item["key"][:256],
                      "key_truncated": len(item["key"]) > 256,
                      "match_modes": (["exact_key"] if item["key"] == key else []) if key is not None else [mode for mode, values in matches.items() if values],
                      "matched_terms": [term[:256] for term in sorted(matched)[:10]],
                      "terms_truncated": len(matched) > 10 or any(len(term) > 256 for term in matched),
                      "score": None, "source_status": None, "ranking_group": None,
                      "exclusion_stage": None, "returned": False}
            if key is not None and item["key"] != key or key is None and q and not matched:
                detail["exclusion_stage"] = "match"
                if diagnostics is not None and len(unmatched) < 40:
                    unmatched.append(detail)
                continue
            counts["matched"] += 1
            item.update(self._applicability(item, run_id, ledger_cache))
            if not item["applicable"]:
                counts["scope_excluded"] += 1
                detail["exclusion_stage"] = "scope"
                if diagnostics is not None and len(scoped) < 40:
                    scoped.append(detail)
                continue
            counts["applicable"] += 1
            age = max(0, (now - item["created"]) / 86400)
            overlap = 1 if key is not None else len(matched)
            score = ((overlap / max(1, len(q))) * 10 + math.exp(-age / 90)) * KIND_WEIGHTS.get(item["kind"], 1)
            item.update(self._staleness(item, source_cache, commit_cache))
            item["source_status"] = source_status(item)
            group = ranking_group(item)
            if item["possibly_stale"]:
                score *= 0.7
            detail.update(score=score, source_status=item["source_status"], ranking_group=group)
            found.append((group, score, item, detail))
        found.sort(key=lambda entry: (entry[0] == "preferred", entry[1]), reverse=True)
        results, returned_details = [], []
        counts["returned"] = min(limit, len(found))
        counts["limit_excluded"] = max(0, len(found) - limit)
        for index, (_, _, item, detail) in enumerate(found):
            if index >= limit:
                detail["exclusion_stage"] = "limit"
                if diagnostics is not None and len(excluded) < 40:
                    excluded.append(detail)
                continue
            detail["returned"] = True
            returned_details.append(detail)
            selected = {name: item[name] for name in ("id", "key", "abstract", "source", "kind", "created", "provenance",
                "possibly_stale", "stale_reasons", "source_status", "scope", "source_run_id", "source_run_status",
                "reference_only", "applicable", "historical")}
            if level >= 1:
                selected["overview"] = item["overview"]
            if level >= 2:
                selected["detail"] = item["detail"]
            results.append(selected)
        if diagnostics is not None:
            # Prefer candidates closest to selection over unrelated records.
            details = (returned_details + excluded + scoped + unmatched)[:40]
            diagnostics.clear()
            diagnostics.update(mode="key" if key is not None else "query", query_excerpt=text[:1024],
                query_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), query_truncated=len(text) > 1024,
                counts=counts, candidates=details, details_truncated=counts["active_candidates"] > len(details))
        return results

    def history(self, key):
        if not isinstance(key, str) or not key:
            raise HarnessError("key 必须为非空字符串")
        if not self.path.exists():
            return []
        with self.connect() as db:
            rows = db.execute("SELECT * FROM memories WHERE key=? ORDER BY created, rowid", (key,)).fetchall()
        results, source_cache, commit_cache, ledger_cache = [], {}, {}, {}
        now = time.time()
        for row in rows:
            item = self._decode(row)
            item["expired"] = item["expires"] is not None and item["expires"] < now
            item.update(self._staleness(item, source_cache, commit_cache))
            item.update(self._applicability(item, ledger_cache=ledger_cache))
            item["source_status"] = source_status(item)
            results.append(item)
        return results

    def export(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM memories ORDER BY created").fetchall()
        return [{**self._decode(row), **self._applicability(self._decode(row))} for row in rows]
