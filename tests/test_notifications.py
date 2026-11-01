"""Offline inbox lifecycle tests; no provider or model calls."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from harness import git
from harness.notifications import NotificationStore, observe_notifications
from harness.private_state import ensure_private_state
from harness.util import HarnessError


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        git.git(self.repo, 'init', '-q')
        self.state = ensure_private_state(self.repo)
        self.run_id = 'run_123abc'
        self.directory = self.state / 'runs' / self.run_id
        self.directory.mkdir(parents=True)
        self.events = self.directory / 'events.jsonl'
        self.ledger = self.directory / 'state.json'
        self.save_ledger(status='blocked', decision_requests=[], operator_decisions=[], accepted={})

    def save_ledger(self, **fields):
        self.ledger.write_text(json.dumps(dict(id=self.run_id, repo=str(self.repo), **fields)))

    def event(self, kind='run_stopped', **fields):
        return (json.dumps(dict(event=kind, run_id=self.run_id, time=1, **fields)) + '\n').encode()

    def test_partial_corrupt_repaired_tail_then_valid_replays_once(self):
        self.events.write_bytes(b'{broken')
        with NotificationStore(self.repo) as store:
            self.assertEqual(store.ingest_run(self.run_id), 0)
            self.assertEqual(store.list_notifications(), [])
        with self.events.open('ab') as output:
            output.write(b'\n' + self.event(error='secret prompt'))
        with NotificationStore(self.repo) as store:
            self.assertEqual(store.ingest_run(self.run_id), 2)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM anomalies').fetchone()[0], 1)
            rows = store.list_notifications()
            self.assertEqual({r['payload']['kind'] for r in rows}, {'evidence_anomaly', 'run_stopped'})
            self.assertNotIn('secret prompt', json.dumps(rows))
            before = self.events.read_bytes()
        with NotificationStore(self.repo) as store:
            self.assertEqual(store.ingest_run(self.run_id), 0)
            self.assertEqual(len(store.list_notifications()), 2)
        self.assertEqual(self.events.read_bytes(), before)

    def test_transaction_failure_rolls_back_cursor_and_outbox(self):
        self.events.write_bytes(self.event())
        with NotificationStore(self.repo) as store:
            original = store._insert
            def fail(*args):
                original(*args)
                raise RuntimeError('simulated disk transaction interruption')
            with patch.object(store, '_insert', side_effect=fail):
                with self.assertRaises(RuntimeError):
                    store.ingest_run(self.run_id)
            self.assertEqual(store.list_notifications(), [])
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM cursors').fetchone()[0], 0)
            self.assertEqual(store.ingest_run(self.run_id), 1)

    def test_log_replacement_and_truncation_preserve_cursor(self):
        self.events.write_bytes(self.event())
        with NotificationStore(self.repo) as store:
            store.ingest_run(self.run_id)
            old = tuple(store.db.execute('SELECT * FROM cursors').fetchone())
            self.events.unlink()
            self.events.write_bytes(self.event('run_completed'))
            self.assertEqual(store.ingest_run(self.run_id), 0)
            self.assertEqual(tuple(store.db.execute('SELECT * FROM cursors').fetchone()), old)
            self.assertEqual(store.list_notifications()[-1]['payload']['kind'], 'evidence_anomaly')
            self.events.write_bytes(b'')
            store.ingest_run(self.run_id)
            self.assertEqual(tuple(store.db.execute('SELECT * FROM cursors').fetchone()), old)

    def test_long_line_does_not_block_next_complete_event(self):
        self.events.write_bytes(b'x' * 300000 + b'\n' + self.event())
        with NotificationStore(self.repo) as store:
            self.assertEqual(store.ingest_run(self.run_id), 2)
            anomaly = store.db.execute('SELECT * FROM anomalies').fetchone()
            self.assertEqual(anomaly['reason'], 'line_too_long')
            self.assertLess(len(anomaly['excerpt']), 100)
            self.assertEqual(anomaly['digest'], hashlib.sha256(b'x' * 300000 + b'\n').hexdigest())

    def test_ack_is_specific_idempotent_and_does_not_deliver_or_resume(self):
        self.events.write_bytes(self.event() + self.event('run_completed'))
        before = self.ledger.read_bytes()
        with NotificationStore(self.repo) as store:
            store.ingest_run(self.run_id)
            first, second = store.list_notifications()
            with self.assertRaises(HarnessError):
                store.acknowledge(first['id'], run_id='run_other')
            store.acknowledge(first['id'], run_id=self.run_id)
            timestamp = store.list_notifications()[0]['acknowledged_at']
            store.acknowledge(first['id'], run_id=self.run_id)
            self.assertEqual(store.list_notifications()[0]['acknowledged_at'], timestamp)
            self.assertEqual([r['id'] for r in store.list_notifications(unacknowledged=True)], [second['id']])
            self.assertEqual(first['delivery_status'], 'pending')
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_request_not_registered_historical_and_expired_only_offer_status(self):
        self.events.write_bytes(self.event('operator_decision_requested', id='request_a'))
        with NotificationStore(self.repo) as store:
            store.ingest_run(self.run_id)
            self.assertEqual(store.list_notifications()[0]['current']['relevance'], 'request_not_confirmed_registered')
            self.save_ledger(status='blocked', decision_requests=[dict(id='request_a', task_id='task')], operator_decisions=[], accepted={})
            self.assertEqual(store.list_notifications()[0]['current']['relevance'], 'registered_request_requires_verification')
            self.save_ledger(status='completed')
            row = store.list_notifications()[0]
            self.assertEqual(row['current']['relevance'], 'historical')
            self.assertEqual(row['current']['action'], 'status')
            self.save_ledger(status='running', decision_requests=[], operator_decisions=[], accepted={})
            self.assertEqual(store.list_notifications()[0]['current']['relevance'], 'request_not_confirmed_registered')

    def test_failed_delivery_backoff_restart_and_immutable_payload(self):
        self.events.write_bytes(self.event())
        with NotificationStore(self.repo) as store:
            store.ingest_run(self.run_id)
            def fail(payload):
                raise OSError('private error details')
            store.deliver_due(fail, now=10)
            row = store.list_notifications()[0]
            self.assertEqual(row['delivery_status'], 'failed')
            self.assertEqual(row['attempts'], 1)
            self.assertEqual(row['last_error'], 'OSError')
        self.save_ledger(status='completed')
        with NotificationStore(self.repo) as store:
            seen = []
            with patch.object(store, '_reconcile', side_effect=AssertionError('timer must not read ledger')):
                self.assertEqual(store.deliver_due(seen.append, now=10), 0)
                self.assertEqual(store.deliver_due(seen.append, now=12), 1)
            self.assertEqual(seen[0]['kind'], 'run_stopped')
            self.assertEqual(store.list_notifications()[0]['delivery_status'], 'sent')
            self.assertIsNone(store.list_notifications()[0]['acknowledged_at'])

    def test_crash_after_external_send_is_retried_with_same_id(self):
        self.events.write_bytes(self.event())
        seen = []
        with NotificationStore(self.repo) as store:
            store.ingest_run(self.run_id)
            def crash(payload):
                seen.append(payload['notification_id'])
                raise KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                store.deliver_due(crash)
        with NotificationStore(self.repo) as store:
            store.deliver_due(lambda p: seen.append(p['notification_id']))
            self.assertEqual(seen, [seen[0], seen[0]])

    def test_native_subscription_precedes_initial_sample_and_handles_append(self):
        self.events.write_bytes(b'')
        seen = []
        stop = threading.Event()
        from harness.watch import RunWatcher
        original = RunWatcher.__init__
        def subscribe(watcher, reader, run_id):
            original(watcher, reader, run_id)
            self.events.write_bytes(self.event())
        def sink(payload):
            seen.append(payload)
            stop.set()
        with patch('harness.notifications.RunWatcher.__init__', subscribe):
            observe_notifications(self.repo, [self.run_id], stop_event=stop, sink=sink)
        self.assertEqual(len(seen), 1)

    def test_readonly_queries_do_not_initialize_or_ack(self):
        directory = self.state / 'supervision'
        directory.mkdir()
        receipt = self.terminal_receipt()
        (directory / (receipt['supervision_id'] + '.json')).write_text(json.dumps(receipt))
        with NotificationStore(self.repo, readonly=True) as store:
            self.assertEqual(store.list_notifications(), [])
        self.assertFalse((self.state / 'notifications').exists())

    def test_oversized_timestamp_is_unknown_and_next_completion_survives_restart(self):
        self.events.write_bytes(
            (json.dumps(dict(event='run_stopped', run_id=self.run_id, time=10 ** 400)) + '\n').encode()
            + self.event('run_completed'))
        with NotificationStore(self.repo) as store:
            self.assertEqual(store.ingest_run(self.run_id), 2)
            rows = store.list_notifications()
            self.assertIsNone(rows[0]['payload']['occurred_at'])
            self.assertEqual(rows[1]['payload']['kind'], 'run_completed')
        with NotificationStore(self.repo) as store:
            self.assertEqual(store.ingest_run(self.run_id), 0)
            self.assertEqual(len(store.list_notifications()), 2)

    def terminal_receipt(self, suffix='a'):
        return dict(schema_version=1, supervision_id='supervision_' + suffix * 16,
                    operation_id='op_receipt_' + suffix, run_id=None,
                    source='harness_supervisor_wait', process_observation='exited',
                    observed_at=1.0, pid=123, exit_code=7)

    def test_receipt_reconciliation_skips_unsafe_invalid_and_pending_sources(self):
        import os
        from harness.util import write_json
        directory = self.state / 'supervision'
        directory.mkdir()
        valid = self.terminal_receipt()
        write_json(directory / (valid['supervision_id'] + '.json'), valid)
        for suffix, value in [('b', []), ('c', {'schema_version': 1}),
                              ('d', {**self.terminal_receipt('d'), 'process_observation': 'running'}),
                              ('e', {**self.terminal_receipt('e'), 'exit_code': True}),
                              ('f', {**self.terminal_receipt('f'), 'observed_at': 10 ** 400})]:
            write_json(directory / ('supervision_' + suffix * 16 + '.json'), value)
        outside = self.repo.parent / 'external-receipt'
        write_json(outside, self.terminal_receipt('1'))
        (directory / ('supervision_' + '1' * 16 + '.json')).symlink_to(outside)
        os.mkfifo(directory / ('supervision_' + '2' * 16 + '.json'))
        (directory / ('supervision_' + '3' * 16 + '.json')).write_bytes(b'x' * (64 * 1024 + 1))
        before = (directory / (valid['supervision_id'] + '.json')).read_bytes()
        with NotificationStore(self.repo) as store:
            rows = store.list_notifications()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['payload']['supervision_id'], valid['supervision_id'])
            store.reconcile_process_receipts()
            self.assertEqual(len(store.list_notifications()), 1)
            with self.assertRaises(HarnessError):
                store.add_process_observation({**valid, 'process_observation': 'launching'})
        self.assertEqual((directory / (valid['supervision_id'] + '.json')).read_bytes(), before)

    def test_active_observer_wakes_for_receipt_without_reading_run_ledger(self):
        from harness.util import write_json
        self.events.write_bytes(b'')
        waiting, done, stop = threading.Event(), threading.Event(), threading.Event()
        seen, errors = [], []
        import select
        native_select = select.select
        def wait(*args):
            waiting.set()
            return native_select(*args)
        def sink(payload):
            seen.append(payload)
            stop.set()
            done.set()
        def observe():
            try:
                observe_notifications(self.repo, [self.run_id], stop_event=stop, sink=sink)
            except BaseException as error:
                errors.append(error)
                done.set()
        with patch('harness.notifications.select.select', side_effect=wait):
            thread = threading.Thread(target=observe)
            thread.start()
            try:
                self.assertTrue(waiting.wait(2))
                directory = self.state / 'supervision'
                directory.mkdir()
                receipt = self.terminal_receipt()
                write_json(directory / (receipt['supervision_id'] + '.json'), receipt)
                prompt = done.wait(0.75)
            finally:
                stop.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(prompt, 'native receipt change did not wake active observer')
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['kind'], 'process_exited')
