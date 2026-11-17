"""Worker-return interruptions preserve candidates and invocation accounting."""
import json
from pathlib import Path
from unittest.mock import patch

from test_harness import ExitReportingBackend, RepoCase, ROOT, after_save, cli_notice, run_cli, walk_exit
from harness import git, stops
from harness.adapters import DemoBackend
from harness.util import HarnessError, read_json
import harness.worker_receipts as receipts_module


def commit(cwd):
    git.git(cwd, 'add', '-A')
    git.git(cwd, '-c', 'user.name=w', '-c', 'user.email=w@localhost', '-c', 'commit.gpgsign=false',
            'commit', '-m', 'worker commit')


class WorkerReceiptTests(RepoCase):
    def plan(self):
        return read_json(ROOT / 'examples' / 'plan.json')

    def interrupted(self, boundary):
        """A process death at one worker-return boundary; later saves never reach the ledger.

        The backend reports the worker's exit before returning, as a CLI call does,
        because dispatch's finally block (the demo backend's only exit record)
        does not run after a process death.
        """
        engine = self.engine(ExitReportingBackend())
        self.config.data['runtime']['max_attempts'] = 1
        original_write = receipts_module.write_json
        original_publish = engine.services.tasks.publish_worker_result
        original_save = engine.context.save
        state = {'fired': False, 'dead': False}

        def die():
            state['fired'] = state['dead'] = True
            raise KeyboardInterrupt()

        def save(*args, **kwargs):
            if state['dead']:
                return
            original_save(*args, **kwargs)
            receipt = next(iter(engine.run['worker_receipts'].values()), {})
            if state['fired']:
                return
            if boundary == 'sealed_saved' and receipt.get('phase') == 'sealed' and 'artifact_digest' not in receipt:
                die()
            if boundary == 'finish_saved' and 'artifact_digest' in receipt:
                die()

        def write(path, value):
            if not state['fired'] and boundary == 'before_seal' and path.name.startswith('worker-seal.'):
                die()
            original_write(path, value)
            if not state['fired'] and boundary == 'after_seal' and path.name.startswith('worker-seal.'):
                die()
            if not state['fired'] and boundary == 'derived_result' and path.name == 'returned-result.json':
                die()

        def publish(*args):
            if not state['fired'] and boundary == 'publish':
                die()
            return original_publish(*args)

        with patch.object(receipts_module, 'write_json', write), \
                patch.object(engine.services.tasks, 'publish_worker_result', publish), \
                patch.object(engine.context, 'save', side_effect=save):
            with self.assertRaises(KeyboardInterrupt):
                engine.start('Implement greeting', self.plan())
        self.assertTrue(state['fired'])
        return engine

    def test_sealed_boundaries_replay_without_worker_even_at_attempt_limit(self):
        # Seal file written; seal saved before the derived result; derived result
        # written; receipt bound; finish returned before the handoff.
        for boundary in ('after_seal', 'sealed_saved', 'derived_result', 'finish_saved', 'publish'):
            with self.subTest(boundary=boundary):
                # Each case gets an isolated target repository/worktree.
                if boundary != 'after_seal':
                    self.setUp()
                engine = self.interrupted(boundary)
                run = self.engine().resume(engine.run['id'])
                self.assertEqual(run['status'], 'completed')
                self.assertEqual(run['attempts']['greet'], 1)
                self.assertEqual(sum(e['role'] == 'worker' and e['invoked'] for e in run['dispatches']), 1)
                self.assertFalse(run['worker_receipts'])
                self.assertIn('greet', run['accepted'])

    def test_unsealed_return_never_rebinds_or_retries(self):
        engine = self.interrupted('before_seal')
        with self.assertRaisesRegex(HarnessError, 'before immutable seal'):
            engine.resume(engine.run['id'])
        self.assertEqual(engine.run['attempts']['greet'], 1)
        # Row unsealed-return: confirming the exit does not unblock it ...
        state = read_json(engine.directory / 'state.json')
        worker = next(d for d in state['dispatches'] if d['role'] == 'worker')
        code, errors = run_cli(self, ExitReportingBackend(), ['resume', state['id'], '--confirm-exited', worker['id']])
        self.assertEqual((code, cli_notice(errors)['reason']), (1, 'unsealed_return'))
        self.assertEqual(read_json(engine.directory / 'state.json')['attempts'], state['attempts'])
        # ... the listed exit, abandon, is the way out; the evidence stays.
        walk_exit(self, engine.directory, ('evidence', 'unsealed_return'), backend=ExitReportingBackend(),
                  expect_status='abandoned')
        evidence = read_json(engine.directory / 'state.json')['dispatches'][0]['worker_return_evidence']
        job = engine.directory / 'dispatches' / worker['id']
        self.assertTrue(all((job / name).is_file() for name, digest in evidence['sources'].items() if digest))

    def assert_unsealed(self, engine):
        saved = read_json(engine.directory / 'state.json')
        self.assertEqual((saved['stop']['kind'], saved['stop']['reason']), ('evidence', 'unsealed_return'))
        self.assertEqual(stops.project_ledger(saved)['exits'], [f"abandon {saved['id']}"])
        worker = next(d for d in saved['dispatches'] if d['role'] == 'worker')
        self.assertEqual(next(iter(saved['worker_receipts'].values()))['phase'], 'invoking')
        self.assertEqual(list((engine.directory / 'dispatches' / worker['id']).glob('worker-seal.*.json')), [])
        self.assertIn('HEAD different from the checkpoint', worker['error'])
        self.assertEqual(saved['attempts'], {'greet': 1})
        return saved

    def test_head_moved_during_the_worker_call_leaves_the_return_unsealed(self):
        class Committing(DemoBackend):
            calls = 0
            def invoke(self, **kwargs):
                self.calls += 1
                result = super().invoke(**kwargs)
                if kwargs['role'] == 'worker':
                    commit(kwargs['cwd'])
                return result
        backend = Committing()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, 'before immutable seal') as caught:
            engine.start('Implement greeting', self.plan())
        calls, run_id = backend.calls, engine.run['id']
        self.assertEqual(stops.of(caught.exception)['reason'], 'unsealed_return')
        saved = self.assert_unsealed(engine)
        self.assertNotEqual(git.head(engine.worktree), saved['expected_head'])
        # The resume preflight reports the moved HEAD first, without touching the saved stop.
        with self.assertRaises(HarnessError) as moved:
            self.engine(backend).resume(run_id)
        self.assertEqual((stops.of(moved.exception)['kind'], stops.of(moved.exception)['reason']),
                         ('workspace_mismatch', 'head_changed'))
        self.assert_unsealed(engine)
        # Restoring the checkpoint HEAD reaches the same typed stop: the return is never sealed later.
        git.git(engine.worktree, 'reset', '--soft', saved['expected_head'])
        with self.assertRaisesRegex(HarnessError, 'before immutable seal') as restored:
            self.engine(backend).resume(run_id)
        self.assertEqual(stops.of(restored.exception)['reason'], 'unsealed_return')
        self.assert_unsealed(engine)
        self.assertEqual(backend.calls, calls)
        run = walk_exit(self, engine.directory, ('evidence', 'unsealed_return'), backend=backend,
                        expect_status='abandoned')
        self.assertEqual(sum(d['role'] == 'worker' and d['invoked'] for d in run['dispatches']), 1)
        self.assertEqual(backend.calls, calls)

    def test_head_moved_while_the_seal_takes_the_candidate_leaves_the_return_unsealed(self):
        # HEAD is compared after the candidate snapshot, so a commit landing during it is caught too;
        # comparing first would seal a HEAD that the candidate no longer matches.
        engine = self.engine(DemoBackend())
        service, snapshot, entered, moved = engine.services.worker_receipts, engine.context.snapshot, [], []
        seal = service.seal

        def committing_snapshot():
            if not moved:
                commit(engine.worktree)
                moved.append(git.head(engine.worktree))
            return snapshot()

        def sealing(entry, result):
            entered.append(git.head(engine.worktree))
            with patch.object(engine.context, 'snapshot', committing_snapshot):
                return seal(entry, result)
        with patch.object(service, 'seal', sealing), \
                self.assertRaisesRegex(HarnessError, 'before immutable seal') as caught:
            engine.start('Implement greeting', self.plan())
        self.assertEqual(stops.of(caught.exception)['reason'], 'unsealed_return')
        saved = self.assert_unsealed(engine)
        self.assertEqual(entered, [saved['expected_head']])
        self.assertEqual(moved, [git.head(engine.worktree)])
        self.assertNotEqual(moved[0], saved['expected_head'])
        self.assertEqual(sum(d['role'] == 'worker' and d['invoked'] for d in saved['dispatches']), 1)

    def test_changed_candidate_and_tampered_seal_stop(self):
        engine = self.interrupted('after_seal')
        (engine.worktree / 'hello.py').write_text('tampered = True\n')
        with self.assertRaisesRegex(HarnessError, 'candidate changed'):
            engine.resume(engine.run['id'])
        self.assertEqual(engine.run['attempts']['greet'], 1)

    def test_content_address_prevents_unlinked_seal_tampering(self):
        engine = self.interrupted('after_seal')
        seal = next((engine.directory / 'dispatches').glob('*/worker-seal.*.json'))
        record = read_json(seal)
        record['result']['summary'] = 'tampered'
        seal.write_text(json.dumps(record))
        with self.assertRaisesRegex(HarnessError, 'seal.*changed'):
            engine.resume(engine.run['id'])

    def test_missing_active_receipt_cannot_reroll(self):
        engine = self.interrupted('after_seal')
        engine.run['worker_receipts'].clear()
        engine.context.save()
        with self.assertRaisesRegex(HarnessError, 'missing active worker receipt'):
            engine.resume(engine.run['id'])

    def test_handoff_compares_against_the_sealed_candidate(self):
        engine = self.engine()

        def bound(run):
            return any('artifact_digest' in receipt for receipt in run['worker_receipts'].values())

        def edit():
            with (engine.worktree / 'hello.py').open('a') as stream:
                stream.write('\n# edited after the sealed candidate was checked\n')
        change, state = after_save(engine, bound, edit)
        with change, self.assertRaisesRegex(HarnessError, 'worker candidate changed before handoff publication'):
            engine.start('Implement greeting', self.plan())
        self.assertTrue(state['fired'])
        saved = read_json(engine.directory / 'state.json')
        self.assertNotIn('pending_task', saved)
        self.assertEqual(next(iter(saved['worker_receipts'].values()))['phase'], 'sealed')
        self.assertEqual(saved['attempts'], {'greet': 1})

    def test_crash_after_finish_save_then_candidate_change_blocks(self):
        engine = self.interrupted('finish_saved')
        with (engine.worktree / 'hello.py').open('a') as stream:
            stream.write('\n# changed while the controller was down\n')
        resumed = self.engine()
        with self.assertRaisesRegex(HarnessError, 'candidate changed since sealed return'):
            resumed.resume(engine.run['id'])
        self.assertEqual(resumed.run['attempts']['greet'], 1)
        self.assertEqual(next(iter(resumed.run['worker_receipts'].values()))['phase'], 'sealed')

    def test_bound_derived_artifact_bytes_cannot_be_rebound(self):
        engine = self.interrupted('publish')
        receipt = next(iter(engine.run['worker_receipts'].values()))
        from pathlib import Path
        artifact = Path(receipt['artifact'])
        artifact.write_text(artifact.read_text() + '\n')
        with self.assertRaisesRegex(HarnessError, 'returned result changed'):
            engine.resume(engine.run['id'])

    def test_replay_does_not_charge_exhausted_task_or_dispatch_budget(self):
        engine = self.interrupted('publish')
        self.config.data['runtime']['max_attempts'] = engine.run['attempts']['greet']
        self.config.data['runtime']['max_agent_dispatches'] = engine.run['agent_dispatches']
        # Stop after pending publication: reviewers still require their own budget.
        with patch.object(engine.services.tasks, 'continue_task', return_value=False):
            self.assertFalse(engine.services.tasks.replay_worker())
        self.assertIn('pending_task', engine.run)
        self.assertFalse(engine.run['worker_receipts'])
        self.assertEqual(engine.run['agent_dispatches'], 1)
        self.assertEqual(sum(engine.run['attempts'].values()), 1)

    def test_raw_success_followed_by_backend_error_never_retries(self):
        class ReturnedThenFailed(DemoBackend):
            def invoke(self, **kwargs):
                value = super().invoke(**kwargs)
                if kwargs['role'] == 'worker':
                    raise HarnessError('backend failed after capturing success')
                return value
        engine = self.engine(ReturnedThenFailed())
        with self.assertRaises(HarnessError):
            engine.start('Implement greeting', self.plan())
        with self.assertRaisesRegex(HarnessError, 'before immutable seal'):
            engine.resume(engine.run['id'])
        self.assertEqual(engine.run['attempts']['greet'], 1)

    def test_provider_failure_without_result_allows_fresh_counted_retry(self):
        class FailedThenReturned(DemoBackend):
            failed = False
            def invoke(self, **kwargs):
                if kwargs['role'] == 'worker' and not self.failed:
                    self.failed = True
                    raise HarnessError('provider unavailable; no returned result')
                return super().invoke(**kwargs)
        backend = FailedThenReturned()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greeting', self.plan())
        # Row provider-unknown: the listed plain resume is a fresh counted attempt.
        run = walk_exit(self, engine.directory, ('provider_failure', 'unknown'), backend=backend,
                        expect_status='completed')
        workers = [entry for entry in run['dispatches'] if entry['role'] == 'worker']
        self.assertEqual(len(workers), 2)
        self.assertNotEqual(workers[0]['id'], workers[1]['id'])
        self.assertEqual(run['attempts']['greet'], 2)

    def test_blocked_variants_replay_original_gates_once(self):
        for kind in ('environment', 'operator', 'continuation', 'task'):
            with self.subTest(kind=kind):
                if kind != 'environment':
                    self.setUp()
                class Blocked(DemoBackend):
                    def invoke(self, **kwargs):
                        result = super().invoke(**kwargs)
                        if kwargs['role'] == 'worker':
                            result.update(status='blocked', blocker_kind=kind, blockers=['Need prerequisite'])
                            if kind == 'continuation':
                                result['continuation_request'] = {'kind': 'commands', 'commands': [['python3', 'focused.py']],
                                    'minimum_timeout_seconds': 0, 'reason': 'focused check needed'}
                            from harness.util import write_json
                            write_json(kwargs['job'] / 'response.json', result)
                        return result
                engine = self.engine(Blocked())
                original = engine.services.tasks.publish_worker_result
                with patch.object(engine.services.tasks, 'publish_worker_result', side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        engine.start('Implement greeting', self.plan())
                # Replay only the worker handoff; the commander's decision remains separate.
                try:
                    engine.services.tasks.replay_worker()
                except HarnessError:
                    self.assertIn(kind, ('environment', 'operator'))
                self.assertFalse(engine.run['worker_receipts'])
                self.assertEqual(engine.run['attempts']['greet'], 1)
                self.assertIsNone(engine.services.tasks.replay_worker())
                if kind in ('operator', 'environment'):
                    entry = next(e for e in engine.run['dispatches'] if e['role'] == 'worker')
                    self.assertEqual(entry['attempt_exemption']['blocker_kind'], kind)
                    self.assertEqual(engine.services.budgets.task_budget_used('greet'), 0)
                if kind == 'continuation':
                    self.assertEqual(len(engine.run['continuation_history']), 1)
                    self.assertTrue(engine.run['pending_continuation'])
                if kind == 'task':
                    # Nothing persistent marks the decision: the saved blocker derives it.
                    self.assertEqual(engine.run['feedback']['worker']['blocker_kind'], 'task')
                    self.assertNotIn('commander:decide:run', engine.run['commander_receipts'])

    def test_checkpoint_registration_replay_preserves_original_freshness_origin(self):
        import hashlib
        class Checkpoint(DemoBackend):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs['role'] == 'worker':
                    digest = hashlib.sha256((kwargs['cwd'] / 'hello.py').read_bytes()).hexdigest()
                    result['checkpoints'] = [{'id': 'computed', 'summary': 'computation evidence',
                        'inputs': [], 'evidence': [{'path': 'hello.py', 'sha256': digest}],
                        'freshness': 'volatile', 'max_age_seconds': 3600}]
                    from harness.util import write_json
                    write_json(kwargs['job'] / 'response.json', result)
                return result
        engine = self.engine(Checkpoint())
        original = engine.services.progress.record_checkpoints
        def record(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt()
        with patch.object(engine.services.progress, 'record_checkpoints', record):
            with self.assertRaises(KeyboardInterrupt):
                engine.start('Implement greeting', self.plan())
        worker = next(e for e in engine.run['dispatches'] if e['role'] == 'worker')
        before = list(engine.run['task_checkpoints'])
        with patch.object(engine.services.tasks, 'continue_task', return_value=False):
            engine.services.tasks.replay_worker()
        self.assertEqual(engine.run['task_checkpoints'], before)
        self.assertEqual(len(engine.run['checkpoint_batches']), 1)
        packet = engine.services.progress.progress_packet({'task': self.plan()['tasks'][0]})
        self.assertEqual(packet['task_checkpoints'][0]['freshness_origin'], worker['invoked_at'])
        run = engine.resume(engine.run['id'])
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['attempts']['greet'], 1)

    def test_changed_provider_source_blocks_replay(self):
        engine = self.interrupted('publish')
        worker = next(e for e in engine.run['dispatches'] if e['role'] == 'worker')
        source = engine.directory / 'dispatches' / worker['id'] / 'response.json'
        source.write_text(source.read_text() + '\n')
        with self.assertRaisesRegex(HarnessError, 'source evidence changed'):
            engine.resume(engine.run['id'])
        self.assertEqual(engine.run['attempts']['greet'], 1)

    def test_changed_candidate_during_handoff_cannot_be_late_bound(self):
        engine = self.engine()
        original = engine.services.progress.record_checkpoints
        def record(*args, **kwargs):
            result = original(*args, **kwargs)
            with (engine.worktree / 'hello.py').open('a') as stream:
                stream.write('\n# concurrent edit after worker return\n')
            return result
        with patch.object(engine.services.progress, 'record_checkpoints', record):
            with self.assertRaisesRegex(HarnessError, 'candidate changed during handoff'):
                engine.start('Implement greeting', self.plan())
        self.assertNotIn('pending_task', engine.run)
        self.assertTrue(engine.run['worker_receipts'])
        with self.assertRaisesRegex(HarnessError, 'candidate changed'):
            engine.resume(engine.run['id'])
        self.assertEqual(engine.run['attempts']['greet'], 1)

    def test_exact_delivered_contract_rejects_each_fresh_attempt_without_acceptance(self):
        class ExtraField(DemoBackend):
            workers = 0
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs['role'] == 'worker':
                    self.workers += 1
                    self.asserted_contract = kwargs['schema']
                    result['unexpected_field'] = []
                    from harness.util import write_json
                    write_json(kwargs['job'] / 'response.json', result)
                return result
        backend = ExtraField()
        engine = self.engine(backend)
        limit = self.config.data['runtime']['max_attempts']
        # Each invalid result spends one attempt and scheduling continues to the limit.
        with self.assertRaisesRegex(HarnessError, '尝试次数上限'):
            engine.start('Implement greeting', self.plan())
        self.assertNotIn('unexpected_field', backend.asserted_contract['properties'])
        workers = [d for d in engine.run['dispatches'] if d['role'] == 'worker']
        receipt = workers[-1]['worker_receipt']
        # The ledger references the delivered contract; the file holds its body.
        self.assertNotIn('schema', receipt)
        self.assertEqual(read_json(Path(receipt['schema_artifact'])), backend.asserted_contract)
        self.assertEqual(backend.workers, limit)
        self.assertEqual(engine.run['attempts']['greet'], limit)
        self.assertEqual([(d['status'], d['worker_receipt_consumed']) for d in workers],
                         [('invalid', 'result_rejected')] * limit)
        self.assertFalse(engine.run['worker_receipts'])
        self.assertNotIn('pending_task', engine.run)
        self.assertFalse(engine.run['accepted'])
        with self.assertRaisesRegex(HarnessError, '尝试次数上限'):
            engine.resume(engine.run['id'])
        self.assertEqual(backend.workers, limit)
        self.assertFalse(engine.run['accepted'])

    def test_clean_result_free_cancellation_records_retry_fact_after_confirmed_exit(self):
        from test_harness import interrupt_started_agent
        class CancelOnce(DemoBackend):
            workers = 0
            def invoke(self, **kwargs):
                if kwargs['role'] == 'worker':
                    self.workers += 1
                    if self.workers == 1:
                        interrupt_started_agent(kwargs)
                return super().invoke(**kwargs)
        backend = CancelOnce()
        engine = self.engine(backend)
        with self.assertRaises(KeyboardInterrupt):
            engine.start('Implement greeting', self.plan())
        failed = next(e for e in engine.run['dispatches'] if e['role'] == 'worker')
        self.assertTrue(failed['exit_confirmed'])
        run = engine.resume(engine.run['id'])
        failed = next(e for e in run['dispatches'] if e['role'] == 'worker')
        self.assertEqual(failed['worker_return_evidence']['state'], 'retryable')
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['attempts']['greet'], 2)
        self.assertEqual(backend.workers, 2)
        self.assertEqual(len({e['id'] for e in run['dispatches'] if e['role'] == 'worker'}), 2)

    def test_cancelled_unsealed_raw_return_or_unknown_start_never_gains_retry_fact(self):
        for raw_return in (False, True):
            with self.subTest(raw_return=raw_return):
                if raw_return:
                    self.setUp()
                class Cancel(DemoBackend):
                    def invoke(self, **kwargs):
                        if kwargs['role'] == 'worker':
                            if raw_return:
                                super().invoke(**kwargs)
                            raise KeyboardInterrupt()
                        return super().invoke(**kwargs)
                engine = self.engine(Cancel())
                with self.assertRaises(KeyboardInterrupt):
                    engine.start('Implement greeting', self.plan())
                failed = next(e for e in engine.run['dispatches'] if e['role'] == 'worker')
                self.assertIsNot(failed.get('worker_no_result'), True)
                with self.assertRaises(HarnessError):
                    engine.resume(engine.run['id'])
                self.assertEqual(engine.run['attempts']['greet'], 1)

    def test_scope_rejection_retains_recovery_and_requires_cleanup_before_fresh_worker(self):
        class OutsideScope(DemoBackend):
            workers = 0
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs['role'] == 'worker':
                    self.workers += 1
                    if self.workers == 1:
                        (kwargs['cwd'] / 'outside.txt').write_text('out of scope')
                return result
        backend = OutsideScope()
        engine = self.engine(backend)
        from harness.git import ScopeViolation
        with self.assertRaises(ScopeViolation):
            engine.start('Implement greeting', self.plan())
        worker = next(e for e in engine.run['dispatches'] if e['role'] == 'worker')
        self.assertEqual(worker['worker_receipt_consumed'], 'scope_rejected')
        self.assertFalse(engine.run['worker_receipts'])
        self.assertEqual(engine.run['worker_recovery']['greet']['dispatch_id'], worker['id'])
        with self.assertRaises(HarnessError):
            engine.resume(engine.run['id'])
        self.assertEqual(backend.workers, 1)
        (engine.worktree / 'outside.txt').unlink()
        # Row scope-violation: the listed plain resume after the cleanup.
        run = walk_exit(self, engine.directory, ('scope_violation', 'candidate_outside_scope'), backend=backend,
                        expect_status='completed')
        self.assertEqual(run['attempts']['greet'], 2)
        self.assertEqual(backend.workers, 2)

    def test_invalid_handoff_consumes_rejection_without_acceptance_or_exemption(self):
        class Handoff(DemoBackend):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs['role'] == 'worker':
                    result['decision_requests'] = [{'id': 'invalid', 'task_id': 'greet', 'question': 'Proceed?'}]
                    from harness.util import write_json
                    write_json(kwargs['job'] / 'response.json', result)
                return result
        engine = self.engine(Handoff())
        with self.assertRaisesRegex(HarnessError, '尝试次数上限'):
            engine.start('Implement greeting', self.plan())
        workers = [e for e in engine.run['dispatches'] if e['role'] == 'worker']
        self.assertEqual(len(workers), self.config.data['runtime']['max_attempts'])
        for worker in workers:
            self.assertEqual((worker['status'], worker['worker_receipt_consumed']), ('invalid', 'result_rejected'))
            self.assertNotIn('attempt_exemption', worker)
        self.assertEqual(engine.run['worker_recovery']['greet']['dispatch_id'], workers[-1]['id'])
        self.assertFalse(engine.run['worker_receipts'])
        self.assertNotIn('pending_task', engine.run)
        self.assertFalse(engine.run['accepted'])
        self.assertFalse(any(e['role'] == 'verification' for e in engine.run['dispatches']))

    def test_new_worker_keeps_earlier_failure_recovery_until_acceptance(self):
        class FailOnce(DemoBackend):
            failed = False
            def invoke(self, **kwargs):
                if kwargs['role'] == 'worker' and not self.failed:
                    self.failed = True
                    raise HarnessError('first worker unavailable without result')
                return super().invoke(**kwargs)
        engine = self.engine(FailOnce())
        with self.assertRaises(HarnessError):
            engine.start('Implement greeting', self.plan())
        original_recovery = dict(engine.run['worker_recovery']['greet'])
        with patch.object(engine.services.verification, 'verify', side_effect=HarnessError('verification unavailable')):
            with self.assertRaisesRegex(HarnessError, 'verification unavailable'):
                engine.resume(engine.run['id'])
        self.assertEqual(engine.run['worker_recovery']['greet'], original_recovery)
        self.assertIn('pending_task', engine.run)
        self.assertEqual(engine.run['attempts']['greet'], 2)
        run = engine.resume(engine.run['id'])
        self.assertEqual(run['status'], 'completed')
        self.assertNotIn('greet', run.get('worker_recovery', {}))
        self.assertEqual(run['attempts']['greet'], 2)
