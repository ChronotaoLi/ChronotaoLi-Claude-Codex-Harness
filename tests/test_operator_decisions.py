"""Operator choices retain source evidence and never bypass recovery gates."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from harness.decisions import DecisionRecovery, read_decision
from harness.util import HarnessError, write_json


class Fixture(DecisionRecovery):
    def __init__(self, directory):
        self.directory = Path(directory)
        self.events = []
        self.saves = 0
        self.run = {'id': 'run_test', 'schema_version': 10, 'status': 'blocked',
                    'tasks': [{'id': 'one', 'depends_on': []}, {'id': 'two', 'depends_on': ['one']}],
                    'accepted': {}, 'dispatches': [{'id': 'dispatch_one', 'role': 'commander',
                    'kind': 'replan', 'task_id': 'replanning', 'invoked': True}],
                    'decision_requests': [], 'operator_decisions': []}
        source = self.directory / 'dispatches' / 'dispatch_one' / 'returned-result.json'
        write_json(source, {'task_id': 'replanning', 'dispatch_id': 'dispatch_one', 'action': 'stop',
                            'reason': 'Choose whether to continue', 'new_tasks': [], 'updates': []})
        self.source = {'artifact': str(source), 'artifact_digest': hashlib.sha256(source.read_bytes()).hexdigest(),
                       'dispatch_id': 'dispatch_one'}
        self.run['replan_stopped'] = {'target': 'one', 'reason': 'Choose whether to continue', 'dispatch_id': 'dispatch_one'}

    def save(self):
        self.saves += 1

    def event(self, event, **data):
        self.events.append((event, data))

    def request(self, target='one'):
        return self.register_decision_request(target, 'replanning', 'replan_stop', self.source,
                                              'Choose whether to continue')

    def response(self, request, decision='continue', summary='Proceed within original scope'):
        path = self.directory / 'input.json'
        write_json(path, {'schema_version': 1, 'request_id': request['id'], 'task_id': request['task_id'],
                          'decision': decision, 'summary': summary})
        return path


class OperatorDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Fixture(self.temp.name)

    def test_stop_then_continue_is_explicit_and_idempotent(self):
        engine = self.engine
        request = engine.request()
        self.assertEqual(engine.request()['id'], request['id'])
        engine.record_operator_decision(engine.prepare_operator_decision(engine.response(request, 'stop')))
        self.assertEqual(engine.pending_decision('one')['response']['decision'], 'stop')
        path = engine.response(request)
        engine.record_operator_decision(engine.prepare_operator_decision(path))
        self.assertIsNone(engine.pending_decision('one'))
        self.assertIsNone(engine.prepare_operator_decision(path))
        self.assertEqual(len(engine.run['operator_decisions']), 2)
        self.assertEqual(engine.run['accepted'], {})
        engine.validate_decision_links()

    def test_unknown_scope_and_busy_rejected_without_writes(self):
        engine = self.engine
        request = engine.request()
        path = engine.response(request)
        baseline = engine.saves, len(engine.events), len(list(engine.directory.rglob('*')))
        value = json.loads(path.read_text())
        value['task_id'] = 'two'
        write_json(path, value)
        with self.assertRaises(HarnessError):
            engine.prepare_operator_decision(path)
        path = engine.response(request)
        for gate in ('pending_task', 'pending_acceptance', 'pending_post_accept', 'pending_replan',
                     'pending_directive', 'review_receipts', 'commander_receipts', 'review_corrections'):
            engine.run[gate] = {'blocked': True}
            with self.assertRaises(HarnessError):
                engine.prepare_operator_decision(path)
            engine.run.pop(gate)
        self.assertEqual((engine.saves, len(engine.events), len(list(engine.directory.rglob('*')))), baseline)

    def test_source_tamper_blocks_read_and_response(self):
        engine = self.engine
        request = engine.request()
        path = engine.response(request)
        Path(engine.source['artifact']).write_text('{}')
        with self.assertRaises(HarnessError):
            engine.prepare_operator_decision(path)
        with self.assertRaises(HarnessError):
            engine.decision_packet({}, task_id='one')
        self.assertFalse(engine.run['operator_decisions'])

    def test_stale_terminal_and_accepted_rejected(self):
        engine = self.engine
        request = engine.request()
        path = engine.response(request)
        engine.run['dispatches'].append({'id': 'later', 'invoked': True})
        with self.assertRaises(HarnessError):
            engine.prepare_operator_decision(path)
        engine.run['dispatches'].pop()
        for status in ('completed', 'abandoned'):
            engine.run['status'] = status
            with self.assertRaises(HarnessError):
                engine.prepare_operator_decision(path)
        engine.run['status'] = 'blocked'
        engine.run['accepted']['one'] = {'commit': 'abc'}
        with self.assertRaises(HarnessError):
            engine.prepare_operator_decision(path)

    def test_packet_scopes_latest_answers_and_preserves_references(self):
        engine = self.engine
        request = engine.request()
        engine.record_operator_decision(engine.prepare_operator_decision(engine.response(request)))
        self.assertEqual(engine.decision_packet({}, task_id='two')['operator_decisions'], [])
        packet = engine.decision_packet({}, task_id='one')['operator_decisions']
        self.assertEqual(packet[0]['response']['decision'], 'continue')
        self.assertEqual(packet[0]['source'], engine.source)
        self.assertEqual(len(engine.decision_packet({}, kind='replan')['operator_decisions']), 1)

    def test_worker_question_must_match_bound_blocker(self):
        engine = self.engine
        engine.run.pop('replan_stopped')
        engine.run['dispatches'][0].update(role='worker', kind='implement', task_id='one')
        artifact = Path(engine.source['artifact'])
        write_json(artifact, {'task_id': 'one', 'dispatch_id': 'dispatch_one', 'status': 'blocked',
                             'summary': 'Need operator choice', 'evidence': [], 'blockers': ['Select the option'],
                             'blocker_kind': 'operator', 'proposed_memories': []})
        engine.source['artifact_digest'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        engine.run['current_task'] = 'one'
        engine.run['feedback'] = {'task_id': 'one', 'worker_artifact': str(artifact),
                                  'worker_artifact_digest': engine.source['artifact_digest']}
        with self.assertRaises(HarnessError):
            engine.register_decision_request('one', 'one', 'worker', engine.source, 'Fabricated question')
        request = engine.register_decision_request('one', 'one', 'worker', engine.source, 'Select the option')
        engine.record_operator_decision(engine.prepare_operator_decision(engine.response(request)))
        self.assertIsNone(engine.pending_decision('one'))

    def test_handoff_binds_exact_accepted_worker_and_dependency(self):
        engine = self.engine
        engine.run.pop('replan_stopped')
        engine.run['dispatches'][0].update(role='worker', kind='implement', task_id='one')
        artifact = Path(engine.source['artifact'])
        requests = [{'id': 'choice', 'task_id': 'two', 'question': 'Proceed with dependent work?'}]
        write_json(artifact, {'task_id': 'one', 'dispatch_id': 'dispatch_one', 'status': 'completed',
                             'summary': 'Completed source work', 'evidence': [], 'blockers': [],
                             'blocker_kind': 'none', 'proposed_memories': [], 'decision_requests': requests})
        engine.source['artifact_digest'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        engine.run['accepted']['one'] = {'worker_source': {**engine.source, 'dispatch_id': 'wrong'},
                                         'decision_requests': requests}
        with self.assertRaises(HarnessError):
            engine.register_decision_request('two', 'one', 'handoff', engine.source, requests[0]['question'], 'choice')
        engine.run['accepted']['one']['worker_source'] = engine.source.copy()
        request = engine.register_decision_request('two', 'one', 'handoff', engine.source, requests[0]['question'], 'choice')
        engine.run['dispatches'].append({'id': 'unrelated', 'task_id': 'other', 'invoked': True})
        engine.record_operator_decision(engine.prepare_operator_decision(engine.response(request)))
        engine.run['accepted']['two'] = {'commit': 'accepted'}
        engine.validate_decision_links()
        with self.assertRaises(HarnessError):
            engine.prepare_operator_decision(engine.response(request, 'stop'))

    def test_registration_requires_current_stop_but_existing_request_survives_advance(self):
        engine = self.engine
        engine.run['replan_stopped']['dispatch_id'] = 'other'
        with self.assertRaises(HarnessError):
            engine.request()
        self.assertFalse(engine.run['decision_requests'])
        engine.run['replan_stopped']['dispatch_id'] = 'dispatch_one'
        request = engine.request()
        engine.run.pop('replan_stopped')
        engine.run['dispatches'].append({'id': 'later', 'invoked': True})
        engine.run['accepted']['one'] = {'commit': 'later-accepted'}
        self.assertEqual(engine.request()['id'], request['id'])

    def test_response_artifact_before_ledger_recovery_is_immutable(self):
        engine = self.engine
        request = engine.request()
        path = engine.response(request)
        prepared = engine.prepare_operator_decision(path)
        engine.record_operator_decision(prepared)
        row = engine.run['operator_decisions'].pop()
        original = Path(row['artifact']).read_bytes()
        engine.record_operator_decision(engine.prepare_operator_decision(path))
        self.assertEqual(Path(row['artifact']).read_bytes(), original)
        self.assertEqual(len(engine.run['operator_decisions']), 1)
        self.assertEqual(engine.run['operator_decisions'][0]['id'], row['id'])

    def test_malformed_ledger_raises_harness_error(self):
        request = self.engine.request()
        self.engine.run['decision_requests'][0]['source'] = []
        with self.assertRaises(HarnessError):
            self.engine.decision_context()

    def test_strict_input_and_bounded_read(self):
        path = self.engine.directory / 'input.json'
        for raw in ('{"schema_version":1,"schema_version":1}', '[' + ' ' * 65536 + ']'):
            path.write_text(raw)
            with self.assertRaises(HarnessError):
                read_decision(path)
        request = self.engine.request()
        path = self.engine.response(request)
        value = json.loads(path.read_text())
        value['permissions'] = ['write']
        write_json(path, value)
        with self.assertRaises(HarnessError):
            self.engine.prepare_operator_decision(path)

    def test_prepared_record_rechecks_boundary_before_write(self):
        request = self.engine.request()
        prepared = self.engine.prepare_operator_decision(self.engine.response(request))
        self.engine.run['pending_acceptance'] = {'task_id': 'one'}
        with self.assertRaises(HarnessError):
            self.engine.record_operator_decision(prepared)
        self.assertFalse(self.engine.run['operator_decisions'])


if __name__ == '__main__':
    unittest.main()
