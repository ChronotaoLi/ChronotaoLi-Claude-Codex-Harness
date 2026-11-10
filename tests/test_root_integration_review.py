"""Independent behavioral checks for advisory reports and read-only telemetry."""
import copy
from pathlib import Path
import tempfile
import unittest

from harness import git
from harness.diagnostics import budget_explanation
from harness.plan_readiness import build_readiness, save_readiness
from harness.private_state import ensure_private_state
from harness.util import HarnessError


def task(ident, dependencies=()):
    return dict(id=ident, title=ident, description='implement ' + ident, acceptance=['verified output'],
                allowed_paths=['src/**'], depends_on=list(dependencies), skills=[])


class RootIntegrationReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve() / 'repo'
        self.repo.mkdir()
        git.git(self.repo, 'init', '-q')

    def test_readiness_refuses_symlink_output_ancestor(self):
        ensure_private_state(self.repo)
        outside = self.repo.parent / 'outside'
        outside.mkdir()
        (self.repo / '.harness/plans').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(HarnessError):
            save_readiness(self.repo, {'tasks': [task('work')]}, 'original goal')
        self.assertEqual(list(outside.iterdir()), [])

    def test_transitive_evidence_owner_report_remains_advisory(self):
        plan = {'tasks': [task('source'), task('evidence', ['source']), task('owner', ['evidence'])]}
        coverage = {'schema_version': 1, 'requirements': [dict(id='R1', text='original requirement',
                    evidence_tasks=['source', 'evidence'], owner_task='owner')]}
        original = copy.deepcopy(plan)
        report = build_readiness(plan, 'original goal', coverage)
        self.assertFalse(any(w['code'] == 'owner_precedes_evidence' for w in report['warnings']))
        self.assertEqual(report['authority'], 'advisory_only')
        self.assertEqual(report['semantic_coverage'], 'requires_independent_review')
        self.assertEqual(plan, original)
        plan['tasks'][-1]['depends_on'] = []
        report = build_readiness(plan, 'original goal', coverage)
        warning = next(w for w in report['warnings'] if w['code'] == 'owner_precedes_evidence')
        self.assertEqual(warning['missing_dependencies'], ['evidence', 'source'])

    def test_dimensions_deduplicate_and_preserve_unknown_candidates(self):
        workers = [dict(id=f'w{i}', role='worker', invoked=True, pgid=100+i, task_id='task', status='finished') for i in range(5)]
        for worker in workers[3:]:
            worker.update(status='failed', diagnostics={'failure_class': 'provider_reported'})
        workers[0]['format_correction'] = True
        run = dict(id='run_abc', dispatches=workers + [copy.deepcopy(workers[0]),
                   dict(id='legacy', role='worker', task_id='task')],
                   review_history={'task': [dict(artifact=f'review{i}', worker_source={
                     'dispatch_id': f'w{i}', 'artifact': f'/dispatches/w{i}/returned-result.json',
                     'artifact_digest': 'a' * 64}) for i in range(3)] + [dict(artifact='legacy-review')]},
                   # Historical schema-15 shape: it saved a run-wide worker counter and limit.
                   schema_version=15, total_attempts=5, agent_dispatches=9, replan_attempts=0, final_attempts=0,
                   config_snapshot={'runtime': {'max_attempts': 5, 'max_total_attempts': 10,
                                    'max_agent_dispatches': 12, 'max_replans': 2, 'max_final_attempts': 3}})
        before = copy.deepcopy(run)
        report = budget_explanation(run, {'task': 5})
        dims = report['dimensions']
        self.assertEqual(dims['actual_worker_invocations'], 5)
        self.assertEqual(dims['reviewed_worker_candidates'], 3)
        self.assertEqual(dims['failed_worker_invocations']['provider_reported'], 2)
        self.assertEqual(dims['worker_format_corrections'], 1)
        self.assertEqual(dims['historical_invocation_unknown'], 1)
        self.assertEqual(dims['unlinked_review_records'], 1)
        self.assertEqual(report['limits']['task_work']['task']['remaining'], 0)
        # The historical counter is shown as saved, never recomputed.
        self.assertEqual(report['limits']['total_worker'], {'used': 5, 'limit': 10, 'remaining': 5})
        self.assertEqual(run, before)
