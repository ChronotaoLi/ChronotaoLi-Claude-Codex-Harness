"""Durable local observations. Delivery and acknowledgement never authorize work."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import sqlite3
import stat
import sys
import time

from .observe import StateReader, NotFound, ObserveError, validate_run_id
from .private_state import ensure_private_state, _path
from .util import HarnessError
from .watch import RunWatcher

EVENTS = frozenset(('run_stopped', 'run_completed', 'run_abandoned', 'plan_awaiting_approval',
                    'operator_decision_requested', 'run_paused'))
MAX_LINE = 256 * 1024
SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z')


def _id(value):
    return value if isinstance(value, str) and SAFE_ID.fullmatch(value) else None


def _number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) else None
    except OverflowError:
        return None


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def terminal_sink(payload):
    """Local terminal only. No raw source text is included in the payload."""
    print(_encoded(payload), file=sys.stderr, flush=True)


def _terminal_receipt(value):
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1
            or not isinstance(value.get('supervision_id'), str)
            or not re.fullmatch(r'supervision_[0-9a-f]{16}', value['supervision_id'])
            or _id(value.get('operation_id')) is None
            or value.get('source') != 'harness_supervisor_wait'
            or _number(value.get('observed_at')) is None):
        return False
    run_id = value.get('run_id')
    if run_id is not None and (not isinstance(run_id, str) or not re.fullmatch(r'run_[A-Za-z0-9]+', run_id)):
        return False
    if value.get('process_observation') == 'launch_failed':
        return value.get('exit_code') is None and value.get('pid') is None
    return (value.get('process_observation') == 'exited'
            and type(value.get('exit_code')) is int and -(2 ** 31) <= value['exit_code'] < 2 ** 31
            and type(value.get('pid')) is int and value['pid'] > 0)


class NotificationStore:
    def __init__(self, repo, *, readonly=False):
        self.repo = Path(os.path.abspath(repo))
        self.state = self.repo / '.harness'
        self.readonly = readonly
        directory = self.state / 'notifications'
        if not readonly:
            ensure_private_state(self.repo)
            _path(directory, directory=True).mkdir(exist_ok=True)
        path = _path(directory / 'inbox.sqlite3')
        # SQLite must not follow sidecar links either.
        for suffix in ('-wal', '-shm', '-journal'):
            _path(Path(str(path) + suffix))
        if readonly:
            if not path.exists():
                self.db = None
            else:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise HarnessError('通知只读数据库必须为普通文件')
                    header = os.read(descriptor, 100)
                finally:
                    os.close(descriptor)
                wal = Path(str(path) + '-wal')
                try:
                    descriptor = os.open(wal, os.O_RDONLY | os.O_NOFOLLOW)
                except FileNotFoundError:
                    wal_nonempty = False
                else:
                    try:
                        info = os.fstat(descriptor)
                        if not stat.S_ISREG(info.st_mode):
                            raise HarnessError('通知 WAL 必须为普通文件')
                        wal_nonempty = info.st_size > 0
                    finally:
                        os.close(descriptor)
                # mode=ro may create WAL/SHM sidecars. This runtime uses DELETE
                # journaling; externally changed WAL stores require explicit repair.
                if len(header) >= 20 and 2 in header[18:20] or wal_nonempty:
                    raise HarnessError('通知只读查询不支持 WAL 数据库；保留现场，不创建 sidecar')
                self.db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        else:
            self.db = sqlite3.connect(path)
        if self.db is not None:
            self.db.row_factory = sqlite3.Row
        if not readonly:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS cursors (
                    run_id TEXT PRIMARY KEY, source TEXT NOT NULL, offset INTEGER NOT NULL,
                    anchor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY, run_id TEXT, operation_id TEXT, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, created REAL NOT NULL, acknowledged REAL,
                    delivery TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT);
                CREATE TABLE IF NOT EXISTS anomalies (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, source TEXT NOT NULL,
                    start INTEGER NOT NULL, end INTEGER NOT NULL, digest TEXT NOT NULL,
                    reason TEXT NOT NULL, excerpt TEXT NOT NULL);
            ''')
            # A crash after external receipt but before sent bookkeeping can retry.
            with self.db:
                self.db.execute("UPDATE observations SET delivery='failed', next_attempt=0, last_error='interrupted_attempt' WHERE delivery='attempting'")
            try:
                self.reconcile_process_receipts()
            except BaseException:
                self.db.close()
                raise

    def close(self):
        if self.db is not None:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _writable(self):
        if self.readonly:
            raise HarnessError('通知查询为只读')

    def _insert(self, ident, run_id, operation_id, kind, payload):
        self.db.execute('INSERT OR IGNORE INTO observations(id,run_id,operation_id,kind,payload,created,delivery) VALUES(?,?,?,?,?,?,?)',
                        (ident, run_id, operation_id, kind, _encoded(payload), time.time(), 'pending'))

    def add_process_observation(self, receipt):
        self._writable()
        if not _terminal_receipt(receipt):
            raise HarnessError('监督收据不是有效的终态进程观察')
        payload = {key: receipt.get(key) for key in ('supervision_id', 'operation_id', 'run_id',
                   'exit_code', 'observed_at', 'process_observation', 'source')}
        payload.update(kind='process_exited', reason='supervised_process_exit_result_unknown',
                       broker_processes='unknown', action='status')
        ident = _digest(_encoded(['process_exited', payload]).encode())
        with self.db:
            self._insert(ident, _id(payload['run_id']), _id(payload['operation_id']), 'process_exited', payload)
        return ident

    def reconcile_process_receipts(self):
        """Replay durable terminal receipts without changing their original bytes.

        This is an outbox handoff recovery path, never process inspection or a
        ledger poll. Invalid/unavailable sources are skipped and retained.
        """
        self._writable()
        imported = 0
        with _reader(self.state) as reader:
            try:
                directory = reader.open_directory(['supervision'])
            except NotFound:
                return 0
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if not re.fullmatch(r'supervision_[0-9a-f]{16}\.json', entry.name):
                            continue
                        try:
                            value = reader.read(['supervision', entry.name], limit=64 * 1024)
                            if value['too_large'] or value['changed_while_reading']:
                                continue
                            receipt = json.loads(value['data'])
                            if not _terminal_receipt(receipt) or receipt['supervision_id'] + '.json' != entry.name:
                                continue
                        except (OSError, ValueError, TypeError, RecursionError, ObserveError):
                            continue
                        # Let database failures propagate. The unchanged source
                        # remains available for any later restart/delivery wake.
                        self.add_process_observation(receipt)
                        imported += 1
            finally:
                os.close(directory)
        return imported

    def _anomaly(self, run_id, source, start, end, digest, reason, excerpt=''):
        ident = _digest(_encoded([run_id, source, start, end, digest, reason]).encode())
        self.db.execute('INSERT OR IGNORE INTO anomalies VALUES(?,?,?,?,?,?,?,?)',
                        (ident, run_id, source, start, end, digest, reason, excerpt))
        self._insert(ident, run_id, None, 'evidence_anomaly',
                     dict(kind='evidence_anomaly', run_id=run_id, reason=reason,
                          evidence=f'runs/{run_id}/events.jsonl', start=start, end=end,
                          sha256=digest, action='status'))

    def ingest_run(self, run_id):
        """Transactionally consume complete lines; never rewrite the event log."""
        self._writable()
        validate_run_id(run_id)
        with _reader(self.state) as reader:
            try:
                fd, info = reader.open_file(['runs', run_id, 'events.jsonl'])
            except NotFound:
                row = self.db.execute('SELECT * FROM cursors WHERE run_id=?', (run_id,)).fetchone()
                if row:
                    with self.db:
                        self._anomaly(run_id, row['source'], row['offset'], row['offset'],
                                      row['anchor'], 'log_missing')
                return 0
            try:
                source = f'{info.st_dev}:{info.st_ino}'
                with self.db:
                    self.db.execute('BEGIN IMMEDIATE')
                    row = self.db.execute('SELECT * FROM cursors WHERE run_id=?', (run_id,)).fetchone()
                    offset = row['offset'] if row else 0
                    anchor = _digest(os.pread(fd, min(offset, 4096), max(0, offset - 4096)))
                    if row and (row['source'] != source or info.st_size < offset or row['anchor'] != anchor):
                        self._anomaly(run_id, source, 0, info.st_size, anchor, 'log_replaced_truncated_or_changed')
                        return 0
                    if not row:
                        self.db.execute('INSERT INTO cursors VALUES(?,?,?,?)', (run_id, source, 0, _digest(b'')))
                    consumed = 0
                    # Each line has bounded memory, including maliciously long records.
                    with os.fdopen(os.dup(fd), 'rb') as stream:
                        stream.seek(offset)
                        while True:
                            start = stream.tell()
                            line = stream.readline(MAX_LINE + 1)
                            if not line:
                                break
                            digest = hashlib.sha256(line)
                            length = len(line)
                            excerpt = _encoded(line[:80].decode('utf-8', errors='replace'))
                            too_long = length > MAX_LINE
                            ended = line.endswith(b'\n')
                            while too_long and not ended:
                                chunk = stream.readline(MAX_LINE + 1)
                                if not chunk:
                                    break
                                digest.update(chunk)
                                length += len(chunk)
                                ended = chunk.endswith(b'\n')
                            if not ended:
                                break
                            end = start + length
                            value = None
                            reason = 'line_too_long' if too_long else None
                            if reason is None:
                                try:
                                    value = json.loads(line)
                                except (ValueError, UnicodeError, RecursionError):
                                    reason = 'invalid_json'
                                if reason is None and (not isinstance(value, dict) or _id(value.get('event')) is None
                                                       or value.get('run_id') != run_id):
                                    reason = 'invalid_event_structure'
                            sha = digest.hexdigest()
                            if reason:
                                self._anomaly(run_id, source, start, end, sha, reason, excerpt)
                            else:
                                kind = value['event']
                                ident = _digest(_encoded([run_id, source, start, end, sha]).encode())
                                payload = dict(kind=kind if kind in EVENTS else 'unsupported_event', run_id=run_id,
                                    operation_id=_id(value.get('operation_id')), occurred_at=_number(value.get('time')),
                                    request_id=_id(value.get('request_id', value.get('id'))), reason=kind if kind in EVENTS else 'unsupported_event',
                                    evidence=f'runs/{run_id}/events.jsonl', start=start, end=end, sha256=sha,
                                    action='status')
                                self._insert(ident, run_id, payload['operation_id'], payload['kind'], payload)
                                # Unknown events are retained locally, but do not flood the terminal.
                                if kind not in EVENTS:
                                    self.db.execute("UPDATE observations SET delivery='local_only' WHERE id=?", (ident,))
                            offset = end
                            consumed += 1
                    anchor = _digest(os.pread(fd, min(offset, 4096), max(0, offset - 4096)))
                    self.db.execute('UPDATE cursors SET offset=?,anchor=? WHERE run_id=?', (offset, anchor, run_id))
                return consumed
            finally:
                os.close(fd)

    def _reconcile(self, payload):
        run_id = payload.get('run_id')
        result = dict(ledger_status='unknown', relevance='unverified', action='status',
                      process_observation=payload.get('process_observation', 'unknown'),
                      observer_connection='query_snapshot')
        if not run_id:
            return result
        try:
            validate_run_id(run_id)
            with _reader(self.state) as reader:
                raw = reader.read(['runs', run_id, 'state.json'], limit=16 * 1024 * 1024)
            if raw['too_large'] or raw['changed_while_reading']:
                return result
            ledger = json.loads(raw['data'])
            if not isinstance(ledger, dict) or ledger.get('id') != run_id or ledger.get('repo') != str(self.repo.resolve()):
                return result
            status = ledger.get('status')
            result['ledger_status'] = status if status in ('running', 'blocked', 'stopped', 'completed', 'abandoned', 'paused', 'awaiting_approval') else 'unknown'
            if status in ('completed', 'abandoned'):
                result['relevance'] = 'historical'
            elif payload.get('kind') == 'operator_decision_requested':
                request_id = payload.get('request_id')
                requests = ledger.get('decision_requests', [])
                responses = ledger.get('operator_decisions', [])
                if not isinstance(requests, list) or not isinstance(responses, list):
                    return result
                requests = [r for r in requests if isinstance(r, dict) and r.get('id') == request_id]
                if not request_id or len(requests) != 1:
                    result['relevance'] = 'request_not_confirmed_registered'
                elif requests[0].get('task_id') in ledger.get('accepted', {}) or any(
                        isinstance(r, dict) and r.get('request_id') == request_id for r in responses):
                    result['relevance'] = 'historical'
                else:
                    # Registration is observable; complete source/recovery qualification
                    # remains the status/recovery protocol's responsibility.
                    result['relevance'] = 'registered_request_requires_verification'
            else:
                result['relevance'] = 'recorded_observation'
        except (OSError, ValueError, TypeError, RecursionError, ObserveError):
            pass
        return result

    def list_notifications(self, *, unacknowledged=False, run_id=None, reconcile=True):
        if self.db is None:
            return []
        clauses, params = [], []
        if unacknowledged:
            clauses.append('acknowledged IS NULL')
        if run_id is not None:
            validate_run_id(run_id)
            clauses.append('run_id=?')
            params.append(run_id)
        query = 'SELECT * FROM observations' + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY created,id'
        results = []
        for row in self.db.execute(query, params):
            payload = json.loads(row['payload'])
            result = dict(id=row['id'], run_id=row['run_id'], operation_id=row['operation_id'],
                          payload=payload, acknowledged_at=row['acknowledged'], delivery_status=row['delivery'],
                          attempts=row['attempts'], next_attempt=row['next_attempt'], last_error=row['last_error'])
            if reconcile:
                result['current'] = self._reconcile(payload)
            results.append(result)
        return results

    def acknowledge(self, notification_id, *, run_id=None, operation_id=None):
        self._writable()
        if (run_id is None) == (operation_id is None):
            raise HarnessError('ACK 必须绑定一个 run 或 operation')
        row = self.db.execute('SELECT * FROM observations WHERE id=?', (notification_id,)).fetchone()
        if row is None or (run_id is not None and row['run_id'] != run_id) or (operation_id is not None and row['operation_id'] != operation_id):
            raise HarnessError('ACK 通知身份不匹配')
        with self.db:
            self.db.execute('UPDATE observations SET acknowledged=COALESCE(acknowledged,?) WHERE id=?', (time.time(), notification_id))
        return True

    def deliver_due(self, sink=terminal_sink, *, now=None):
        self._writable()
        self.reconcile_process_receipts()
        now = time.time() if now is None else now
        rows = self.db.execute("SELECT * FROM observations WHERE delivery IN ('pending','failed') AND next_attempt<=? ORDER BY created,id", (now,)).fetchall()
        for row in rows:
            with self.db:
                changed = self.db.execute("UPDATE observations SET delivery='attempting',attempts=attempts+1 WHERE id=? AND delivery IN ('pending','failed')", (row['id'],)).rowcount
            if not changed:
                continue
            try:
                # Retries reuse the immutable original projection. Never read ledger here.
                sink(dict(notification_id=row['id'], **json.loads(row['payload'])))
            except Exception as error:
                delay = min(3600, 2 ** min(row['attempts'], 11))
                with self.db:
                    self.db.execute("UPDATE observations SET delivery='failed',next_attempt=?,last_error=? WHERE id=?",
                                    (now + delay, type(error).__name__, row['id']))
            else:
                with self.db:
                    self.db.execute("UPDATE observations SET delivery='sent',last_error=NULL WHERE id=?", (row['id'],))
        return len(rows)

    def next_retry_delay(self):
        if self.db is None:
            return None
        row = self.db.execute("SELECT MIN(next_attempt) FROM observations WHERE delivery IN ('pending','failed')").fetchone()
        return None if row[0] is None else max(0, row[0] - time.time())


from contextlib import contextmanager


@contextmanager
def _reader(state):
    reader = StateReader(state, required=True)
    try:
        yield reader
    finally:
        reader.close()


class _SupervisionWatcher:
    """Native receipt invalidation; root subscription covers directory creation."""
    def __init__(self, reader):
        from .watch import _Inotify, _Kqueue, WatchError
        self.reader, self.entries = reader, {}
        if sys.platform == 'darwin' and hasattr(select, 'kqueue'):
            self._native = _Kqueue()
        elif sys.platform.startswith('linux'):
            self._native = _Inotify()
        else:
            raise WatchError('Native supervision notifications unavailable')
        self.backend = self._native.label
        try:
            self._refresh()
        except BaseException:
            self.close()
            raise

    def _refresh(self):
        for components in ((), ('supervision',)):
            try:
                fd = self.reader.open_directory(list(components))
            except NotFound:
                old = self.entries.pop(components, None)
                if old:
                    self._native.remove(old[2])
                    os.close(old[1])
                continue
            info = os.fstat(fd)
            identity = (info.st_dev, info.st_ino)
            old = self.entries.get(components)
            if old and old[0] == identity:
                os.close(fd)
                continue
            try:
                token = self._native.add(fd)
            except BaseException:
                os.close(fd)
                raise
            if old:
                self._native.remove(old[2])
                os.close(old[1])
            self.entries[components] = (identity, fd, token)

    def wait(self, timeout):
        changed = self._native.wait(timeout)
        if changed:
            self._refresh()
        return changed

    def close(self):
        for _, fd, _ in self.entries.values():
            os.close(fd)
        self.entries.clear()
        self._native.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def observe_notifications(repo, run_ids, *, once=False, stop_event=None, sink=terminal_sink):
    """Subscribe first. Timer wakes retry delivery, without rereading ledgers."""
    from contextlib import ExitStack
    run_ids = list(run_ids)
    with NotificationStore(repo) as store, ExitStack() as stack:
        reader = stack.enter_context(_reader(store.state))
        watchers = [stack.enter_context(RunWatcher(reader, run_id)) for run_id in run_ids]
        watchers.append(stack.enter_context(_SupervisionWatcher(reader)))
        for run_id in run_ids:
            store.ingest_run(run_id)
        while True:
            store.deliver_due(sink)
            if once or stop_event is not None and stop_event.is_set():
                return store.list_notifications()
            delay = store.next_retry_delay()
            timeout = min(1.0 if stop_event is not None else 60.0, delay if delay is not None else 60.0)
            if not watchers:
                raise HarnessError('通知观察至少需要一个 run')
            # Wait across all native queues; no per-run ledger polling or first-run starvation.
            descriptors = [getattr(w._native, 'fd', None) if w.backend == 'inotify'
                           else w._native.queue.fileno() for w in watchers]
            ready, _, _ = select.select(descriptors, [], [], timeout)
            changed = [i for i, watcher in enumerate(watchers)
                       if descriptors[i] in ready and watcher.wait(0)]
            for index in changed:
                if index == len(run_ids):
                    continue  # receipt recovery happens before the next delivery
                store.ingest_run(run_ids[index])
                # Reconcile on filesystem invalidations, independently of delivery.
                store.list_notifications(run_id=run_ids[index])
