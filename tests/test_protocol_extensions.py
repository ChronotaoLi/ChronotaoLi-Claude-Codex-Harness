"""Worker extension compatibility and hard protocol boundaries."""
from copy import deepcopy
import unittest

from harness.adapters import provider_schema
from harness.contracts import WORK, envelope, validate, validate_work
from harness.engine import Engine
from harness.util import HarnessError


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
        validate_work(result)
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

    def test_optional_fields_are_strict_when_present(self):
        good = dict(self.result(), checkpoints=[self.checkpoint()],
                    decision_requests=[dict(id='risk', task_id='later', question='Proceed?')])
        validate_work(good)
        for mutation in [
                lambda x: x['checkpoints'][0].update(extra=True),
                lambda x: x['checkpoints'][0].update(max_age_seconds=True),
                lambda x: x['checkpoints'][0].update(max_age_seconds=-1),
                lambda x: x['checkpoints'][0].update(max_age_seconds=86401),
                lambda x: x['checkpoints'][0].update(inputs=[]),
                lambda x: x['checkpoints'][0].update(max_age_seconds=1),
                lambda x: x['checkpoints'][0].update(evidence=[]),
                lambda x: x['decision_requests'].append(x['decision_requests'][0]),
                lambda x: x['decision_requests'][0].update(extra=True),
                lambda x: x.update(status='blocked', blocker_kind='operator', blockers=['Choose'])]:
            value = deepcopy(good)
            mutation(value)
            with self.subTest(value=value), self.assertRaises(HarnessError):
                validate_work(value)

    def test_volatile_zero_and_maximum_ttl_are_valid(self):
        for ttl in (0, 86400):
            checkpoint = self.checkpoint()
            checkpoint.update(freshness='volatile', max_age_seconds=ttl, inputs=[])
            validate_work(dict(self.result(), checkpoints=[checkpoint]))

    def test_handoff_targets_require_unaccepted_transitive_dependency(self):
        engine = Engine.__new__(Engine)
        engine.run = {'tasks': [dict(id='analysis', depends_on=[]),
                                dict(id='middle', depends_on=['analysis']),
                                dict(id='operation', depends_on=['middle']),
                                dict(id='unrelated', depends_on=[])], 'accepted': {}}
        def result(target):
            return dict(self.result(), decision_requests=[dict(id='risk', task_id=target, question='Proceed?')])
        before = deepcopy(engine.run)
        engine.validate_handoff_requests('analysis', result('operation'))
        for target in ('analysis', 'unrelated', 'missing'):
            with self.subTest(target=target), self.assertRaises(HarnessError):
                engine.validate_handoff_requests('analysis', result(target))
        self.assertEqual(engine.run, before)
        engine.run['accepted']['operation'] = {}
        with self.assertRaises(HarnessError):
            engine.validate_handoff_requests('analysis', result('operation'))
