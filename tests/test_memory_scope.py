import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from harness.memory import Memory
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
