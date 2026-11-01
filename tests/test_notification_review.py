"""Independent adversarial review probes for complete malformed event recovery."""
import json
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch
from harness.supervisor import supervise
from harness.util import write_json, HarnessError
from pathlib import Path

from harness import git
from harness.notifications import NotificationStore
from harness.private_state import ensure_private_state


class NotificationReviewTests(unittest.TestCase):
    def test_deeply_nested_complete_bad_record_does_not_strand_following_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            git.git(repo, 'init', '-q')
            state = ensure_private_state(repo)
            directory = state / 'runs/run_review'
            directory.mkdir(parents=True)
            bad = b'[' * 2000 + b'0' + b']' * 2000 + b'\n'
            good = json.dumps({'event': 'run_completed', 'run_id': 'run_review'}).encode() + b'\n'
            (directory / 'events.jsonl').write_bytes(bad + good)
            with NotificationStore(repo) as store:
                self.assertEqual(store.ingest_run('run_review'), 2)
                rows = store.list_notifications(reconcile=False)
                self.assertEqual({row['payload']['kind'] for row in rows},
                                 {'evidence_anomaly', 'run_completed'})
                self.assertEqual(store.ingest_run('run_review'), 0)

    def test_readonly_inbox_does_not_create_wal_sidecars(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            git.git(repo, 'init', '-q')
            with NotificationStore(repo) as store:
                store.db.execute('PRAGMA journal_mode=WAL')
            directory = repo / '.harness/notifications'
            before = {path.name: path.read_bytes() for path in directory.iterdir()}
            with self.assertRaisesRegex(HarnessError, 'WAL'):
                with NotificationStore(repo, readonly=True):
                    self.fail('WAL readonly connection should fail closed')
            after = {path.name: path.read_bytes() for path in directory.iterdir()}
            self.assertEqual(before, after)

    def test_supervisor_storage_failure_after_launch_reaps_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            git.git(repo, 'init', '-q')
            children = []
            def launch(*args, **kwargs):
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
                children.append(child)
                return child
            calls = 0
            def fail_first_write(*args):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError('synthetic receipt disk failure')
                return write_json(*args)
            try:
                with patch('harness.supervisor.write_json', side_effect=fail_first_write):
                    with self.assertRaises(OSError):
                        supervise(repo, ['run', 'fixture'], popen=launch)
                self.assertIsNotNone(children[0].poll(), 'supervised child leaked after receipt failure')
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.wait()
