import copy
import unittest
from harness.plan_readiness import build_readiness


def task(name, dependencies=()):
    return {'id': name, 'title': name, 'description': 'independent result', 'depends_on': list(dependencies),
            'allowed_paths': ['src/**'], 'acceptance': ['observable result'], 'skills': [], 'required_commands': []}


class ReadinessTests(unittest.TestCase):
    def test_transitive_owner_waits_for_all_evidence(self):
        plan = {'tasks': [task('discover'), task('build', ['discover']), task('integrate', ['build'])]}
        coverage = {'requirements': [{'id': 'r1', 'text': 'original acceptance',
                                     'evidence_tasks': ['discover', 'build'], 'owner_task': 'integrate'}]}
        before = copy.deepcopy(plan)
        result = build_readiness(plan, 'original goal', coverage)
        self.assertFalse(any(w['code'] == 'owner_precedes_evidence' for w in result['warnings']))
        self.assertEqual(plan, before)
        plan['tasks'][2]['depends_on'] = ['discover']
        invalid = build_readiness(plan, 'original goal', coverage)
        self.assertIn({'code': 'owner_precedes_evidence', 'requirement_id': 'r1',
                       'owner_task': 'integrate', 'missing_dependencies': ['build']}, invalid['warnings'])
        self.assertNotEqual(result['plan_sha256'], invalid['plan_sha256'])

    def test_missing_mapping_or_unknown_prerequisites_never_claims_ready(self):
        result = build_readiness({'tasks': [task('build')]}, 'goal',
                                {'tasks': {'build': {'unknown_prerequisites': ['SDK version']}}})
        self.assertEqual(result['authority'], 'advisory_only')
        self.assertEqual(result['semantic_coverage'], 'requires_independent_review')
        self.assertTrue({'code': 'requirements_unmapped'} in result['warnings'])
        self.assertTrue({'code': 'discovery_needed', 'task_id': 'build'} in result['warnings'])
