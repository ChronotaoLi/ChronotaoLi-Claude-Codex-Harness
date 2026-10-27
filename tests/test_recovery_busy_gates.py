"""Exercise actual recovery preparation boundaries with isolated busy state."""
import copy
import unittest

import test_environment_updates as environments
from test_harness import RepoCase
from test_operator_decisions import Fixture
from harness.util import HarnessError


class RecoveryBusyGateTests(RepoCase):
    def environment(self):
        return environments.EnvironmentUpdateTests.blocked(self)

    def decision(self):
        engine = Fixture(self.repo / 'decision-fixture')
        request = engine.request()
        return engine, engine.response(request)

    def assert_preparation(self, engine, prepare, blocked):
        before = copy.deepcopy(engine.run)
        files = {str(p): p.read_bytes() for p in engine.directory.rglob('*') if p.is_file()}
        if blocked:
            with self.assertRaisesRegex(HarnessError, "pending lifecycle gates|已有待处理阶段"):
                prepare()
        else:
            self.assertIsNotNone(prepare())
        self.assertEqual(engine.run, before)
        self.assertEqual({str(p): p.read_bytes() for p in engine.directory.rglob('*') if p.is_file()}, files)

    def environment_repair(self, status, receipt):
        engine, backend, path, _ = self.environment()
        key = 'commander:replan:replanning'
        engine.run['invalid_repairs'][key] = {'status': status, 'corrected_dispatch_id': 'corrected'}
        if receipt:
            engine.run['commander_receipts'][key] = {'dispatch_id': 'corrected'}
        calls = len(backend.calls)
        self.assert_preparation(engine, lambda: engine.prepare_environment_update(path), status == 'pending' or receipt)
        self.assertEqual(len(backend.calls), calls)

    def decision_repair(self, status, receipt):
        engine, path = self.decision()
        key = 'commander:replan:replanning'
        engine.run['invalid_repairs'] = {key: {'status': status, 'corrected_dispatch_id': 'corrected'}}
        if receipt:
            engine.run['commander_receipts'] = {key: {'dispatch_id': 'corrected'}}
        saves = engine.saves
        self.assert_preparation(engine, lambda: engine.prepare_operator_decision(path), status == 'pending' or receipt)
        self.assertEqual(engine.saves, saves)

    def test_environment_replan_stopped_rejects_valid_source(self):
        engine, backend, path, _ = self.environment()
        # No subsequent invocation: this must be rejected by the busy gate.
        self.assertIsNotNone(engine.prepare_environment_update(path))
        engine.run['replan_stopped'] = {'target': 'greet'}
        self.assert_preparation(engine, lambda: engine.prepare_environment_update(path), True)
        self.assertEqual(len(backend.calls), 1)

    def test_environment_pending_repair_rejects_update(self):
        self.environment_repair('pending', False)

    def test_decision_pending_repair_rejects_answer(self):
        self.decision_repair('pending', False)

    def test_environment_completed_repair_with_receipt_rejects_update(self):
        self.environment_repair('completed', True)

    def test_decision_completed_repair_with_receipt_rejects_answer(self):
        self.decision_repair('completed', True)

    def test_environment_completed_repair_after_receipt_consumption_allows_update(self):
        self.environment_repair('completed', False)

    def test_decision_completed_repair_after_receipt_consumption_allows_answer(self):
        self.decision_repair('completed', False)
