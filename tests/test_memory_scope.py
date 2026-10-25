import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from harness.memory import Memory
from harness.util import HarnessError
from harness.mcp_server import handle


class MemoryScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.memory = Memory(self.state / 'memory.sqlite3')

    def ledger(self, run, status):
        path = self.state / 'runs' / run / 'state.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'id': run, 'status': status, 'accepted': {'task': {}}}))
        return path

    def remember(self, key, **kwargs):
        return self.memory.remember(key=key, abstract='upgrade fact', overview='upgrade reference',
                                    detail='upgrade historical evidence', source='operator', **kwargs)

    def test_abandoned_and_missing_task_sources_are_only_available_for_audit(self):
        for run, status in [('run_old', 'abandoned'), ('run_lost', None)]:
            if status:
                self.ledger(run, status)
            ident = self.remember('task:' + run + ':task', kind='accepted_task')
            self.assertEqual(self.memory.search('upgrade', run_id='run_new'), [])
            self.assertEqual(self.memory.read(ident)['source_run_status'], status or 'missing')
            self.assertTrue(self.memory.read(ident)['reference_only'])
        self.assertEqual(len(self.memory.export()), 2)

    def test_active_run_task_history_is_marked_and_missing_provenance_is_unverified(self):
        self.ledger('run_old', 'paused')
        self.remember('task:run_old:task', kind='accepted_task')
        item = self.memory.search('upgrade', run_id='run_new')[0]
        self.assertEqual(item['source_run_status'], 'paused')
        self.assertTrue(item['historical'])
        self.assertTrue(item['possibly_stale'])

    def test_operator_run_scope_is_filtered_by_engine_search_and_mcp_run_identity(self):
        ident = self.remember('operator', provenance={'scope': 'run', 'run_id': 'run_old'})
        self.assertEqual(self.memory.search('upgrade', run_id='run_new'), [])
        self.assertEqual(self.memory.search('upgrade', run_id='run_old')[0]['id'], ident)
        request = {'method': 'tools/call', 'params': {'name': 'memory_search',
                                                    'arguments': {'query': 'upgrade'}}}
        new = self.ledger('run_new', 'paused')
        old = self.ledger('run_old', 'paused')
        self.assertEqual(json.loads(handle(request, self.memory, None, new)['content'][0]['text']), [])
        result = json.loads(handle(request, self.memory, None, old)['content'][0]['text'])
        self.assertEqual(result[0]['id'], ident)

    def test_legacy_operator_is_unscoped_reference_and_not_fresh(self):
        self.remember('operator')
        item = self.memory.search('upgrade')[0]
        self.assertEqual(item['scope'], 'unscoped')
        self.assertTrue(item['reference_only'])
        self.assertTrue(item['possibly_stale'])

    def test_malformed_scope_and_unaccepted_task_are_not_recalled(self):
        self.remember('bad-scope', provenance={'scope': []})
        ledger = self.ledger('run_old', 'paused')
        ledger.write_text(json.dumps({'id': 'run_old', 'status': 'paused', 'accepted': {}}))
        ident = self.remember('task:run_old:task', kind='accepted_task')
        self.assertEqual(self.memory.search('upgrade', run_id='run_new'), [])
        self.assertEqual(self.memory.read(ident)['source_run_status'], 'invalid')

    def test_search_caches_source_reads_only_within_one_request(self):
        self.ledger('run_old', 'paused')
        for index in range(3):
            self.remember('history' + str(index), kind='accepted_task',
                          provenance={'run_id': 'run_old', 'task_id': 'task'})
        with patch.object(self.memory, '_source_ledger', wraps=self.memory._source_ledger) as read:
            self.assertEqual(len(self.memory.search('upgrade')), 3)
            self.assertEqual(read.call_count, 1)
            self.ledger('run_old', 'abandoned')
            self.assertEqual(self.memory.search('upgrade'), [])
            self.assertEqual(read.call_count, 2)
            self.assertEqual(self.memory.search('unmatchedword'), [])
            self.assertEqual(read.call_count, 2)

    def test_unknown_source_run_status_is_invalid(self):
        self.ledger('run_old', 'invented_status')
        ident = self.remember('task:run_old:task', kind='accepted_task')
        self.assertEqual(self.memory.search('upgrade'), [])
        self.assertEqual(self.memory.read(ident)['source_run_status'], 'invalid')

    def test_cross_scope_key_collisions_leave_records_and_operations_unchanged(self):
        project = {'scope': 'project'}
        first_run = {'scope': 'run', 'run_id': 'run_a'}
        second_run = {'scope': 'run', 'run_id': 'run_b'}
        for index, (original, replacement) in enumerate([
                (project, first_run), (first_run, project),
                (first_run, second_run), (second_run, first_run),
                (None, first_run), (first_run, None)]):
            with self.subTest(original=original, replacement=replacement):
                key = 'run:run_a:arbitrary:' + str(index)
                ident = self.remember(key, provenance=original)
                before = self.memory.export()
                with self.assertRaisesRegex(HarnessError, '作用域'):
                    self.remember(key, provenance=replacement, operation_id='rejected-' + str(index))
                self.assertEqual(self.memory.export(), before)
                self.assertTrue(self.memory.read(ident)['active'])
                with self.memory.connect() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_operations').fetchone()[0], 0)

    def test_same_effective_scope_replaces_versions_and_preserves_idempotency(self):
        for index, (original, replacement) in enumerate([
                ({'scope': 'project'}, {'scope': 'project'}),
                ({'scope': 'run', 'run_id': 'run_a'}, {'scope': 'run', 'run_id': 'run_a'}),
                (None, {'scope': 'project'}), ({'scope': 'project'}, None),
                ({'scope': 'unscoped'}, {'scope': 'project'})]):
            with self.subTest(original=original, replacement=replacement):
                key = 'versions:' + str(index)
                old = self.remember(key, provenance=original)
                operation = 'replace-' + str(index)
                new = self.remember(key, provenance=replacement, operation_id=operation)
                self.assertFalse(self.memory.read(old)['active'])
                self.assertEqual(self.memory.read(new)['supersedes'], old)
                latest = self.remember(key, provenance=replacement)
                self.assertEqual(self.remember(key, provenance=replacement, operation_id=operation), new)
                self.assertTrue(self.memory.read(latest)['active'])
                self.assertFalse(self.memory.read(new)['active'])

    def test_legacy_task_scope_is_inferred_for_key_replacement(self):
        key = 'task:run_a:task'
        old = self.remember(key, kind='accepted_task')
        before = self.memory.export()
        with self.assertRaisesRegex(HarnessError, '作用域'):
            self.remember(key, provenance={'scope': 'project'})
        self.assertEqual(self.memory.export(), before)
        new = self.remember(key, kind='accepted_task', provenance={'run_id': 'run_a', 'task_id': 'task'})
        self.assertEqual(self.memory.read(new)['supersedes'], old)
        with self.assertRaisesRegex(HarnessError, '作用域'):
            self.remember(key, kind='accepted_task', provenance={'run_id': 'run_b', 'task_id': 'task'})

    def test_mcp_read_and_search_use_same_current_run_metadata(self):
        current = self.ledger('run_a', 'paused')
        ids = [self.remember('run-note', provenance={'scope': 'run', 'run_id': 'run_a'}),
               self.remember('task:run_a:task', kind='accepted_task')]
        search = {'method': 'tools/call', 'params': {'name': 'memory_search',
                                                   'arguments': {'query': 'upgrade'}}}
        found = json.loads(handle(search, self.memory, None, current)['content'][0]['text'])
        self.assertEqual({item['id'] for item in found}, set(ids))
        metadata = ('scope', 'source_run_id', 'source_run_status', 'reference_only', 'applicable', 'historical')
        for item in found:
            request = {'method': 'tools/call', 'params': {'name': 'memory_read',
                                                        'arguments': {'id': item['id']}}}
            response = handle(request, self.memory, None, current)
            self.assertFalse(response['isError'])
            read = json.loads(response['content'][0]['text'])
            self.assertEqual({key: read[key] for key in metadata}, {key: item[key] for key in metadata})
            self.assertTrue(read['applicable'])
            self.assertFalse(read['historical'])
            self.assertTrue(self.memory.read(item['id'])['historical'])
        self.assertFalse(self.memory.read(ids[0])['applicable'])
        other = self.memory.read(ids[0], run_id='run_b')
        self.assertFalse(other['applicable'])
        self.assertTrue(other['historical'])
