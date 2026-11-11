"""Worker extension compatibility and hard protocol boundaries."""
from copy import deepcopy
import unittest
from types import SimpleNamespace
from harness.services import Services

from harness.adapters import provider_schema
from harness.contracts import WORK, envelope, validate
from harness.util import HarnessError
from test_runtime_changes import validate_work_contract


class ProtocolExtensionTests(unittest.TestCase):
    def result(self):
        return dict(status='completed', summary='Done', evidence=['Observed'], blockers=[],
                    blocker_kind='none', proposed_memories=[])

    def checkpoint(self):
        ref = dict(path='input.txt', sha256='a' * 64)
        return dict(id='calculation', summary='Computed', inputs=[ref], evidence=[ref],
                    freshness='content', max_age_seconds=0)

    def test_old_worker_results_remain_valid_through_envelope_and_provider(self):
        result = self.result()
        validate_work_contract(result)
        wrapped = dict(result, task_id='task', dispatch_id='dispatch')
        schema = envelope(WORK, 'task', 'dispatch')
        validate(wrapped, schema)
        wire = provider_schema(schema)
        self.assertNotIn('checkpoints', wire['required'])
        self.assertNotIn('decision_requests', wire['required'])
        validate(wrapped, wire)
        for field in result:
            missing = deepcopy(wrapped)
            missing.pop(field)
            with self.subTest(field=field), self.assertRaises(HarnessError):
                validate(missing, schema)
        with self.assertRaises(HarnessError):
            validate(dict(wrapped, surprise=[]), schema)

    def test_handoffs_are_strict_and_checkpoints_are_optional_products(self):
        good = dict(self.result(), checkpoints=[self.checkpoint()],
                    decision_requests=[dict(id='risk', task_id='later', question='Proceed?')])
        validate_work_contract(good)
        for mutation in [
                lambda x: x['decision_requests'].append(x['decision_requests'][0]),
                lambda x: x['decision_requests'][0].update(extra=True)]:
            value = deepcopy(good)
            mutation(value)
            with self.subTest(value=value), self.assertRaises(HarnessError):
                validate_work_contract(value)
        # Checkpoints are an optional product: a malformed entry is discarded at
        # registration and never makes the worker result itself invalid.
        for mutation in [
                lambda x: x['checkpoints'][0].update(extra=True),
                lambda x: x['checkpoints'][0].update(max_age_seconds=True),
                lambda x: x['checkpoints'][0].update(max_age_seconds=-1),
                lambda x: x['checkpoints'][0].update(max_age_seconds=86401),
                lambda x: x['checkpoints'][0].update(evidence=[]),
                lambda x: x.update(checkpoints='not a list')]:
            value = deepcopy(good)
            mutation(value)
            with self.subTest(value=value):
                self.assertIs(validate_work_contract(value), value)

        # A valid blocker survives inapplicable handoff declarations unchanged;
        # their registration and diagnostics belong to the lifecycle boundary.
        blocked = dict(deepcopy(good), status='blocked', blocker_kind='operator', blockers=['Choose'])
        original = deepcopy(blocked)
        validate_work_contract(blocked)
        self.assertEqual(blocked, original)

    def test_volatile_zero_and_maximum_ttl_are_valid(self):
        for ttl in (0, 86400):
            checkpoint = self.checkpoint()
            checkpoint.update(freshness='volatile', max_age_seconds=ttl, inputs=[])
            validate_work_contract(dict(self.result(), checkpoints=[checkpoint]))

    def test_handoff_targets_require_unaccepted_transitive_dependency(self):
        engine = SimpleNamespace()
        engine.run = {'tasks': [dict(id='analysis', depends_on=[]),
                                dict(id='middle', depends_on=['analysis']),
                                dict(id='operation', depends_on=['middle']),
                                dict(id='unrelated', depends_on=[])], 'accepted': {}}
        engine.services = Services(engine)
        def result(target):
            return dict(self.result(), decision_requests=[dict(id='risk', task_id=target, question='Proceed?')])
        before = deepcopy(engine.run)
        engine.services.workflow.validate_handoff_requests('analysis', result('operation'))
        for target in ('analysis', 'unrelated', 'missing'):
            with self.subTest(target=target), self.assertRaises(HarnessError):
                engine.services.workflow.validate_handoff_requests('analysis', result(target))
        self.assertEqual(engine.run, before)
        engine.run['accepted']['operation'] = {}
        with self.assertRaises(HarnessError):
            engine.services.workflow.validate_handoff_requests('analysis', result('operation'))
