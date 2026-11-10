"""Worker-return interruptions preserve candidates and invocation accounting."""
import json
from unittest.mock import patch

from test_harness import RepoCase, ROOT
from harness.adapters import DemoBackend
from harness.util import HarnessError, read_json
import harness.worker_receipts as receipts_module


class WorkerReceiptTests(RepoCase):
    def plan(self):
        return read_json(ROOT / 'examples' / 'plan.json')

    def interrupted(self, boundary):
        engine = self.engine()
        self.config.data['runtime']['max_attempts'] = 1
        original_write = receipts_module.write_json
        original_publish = engine.services.tasks.publish_worker_result
        fired = False

        def write(path, value):
            nonlocal fired
            if not fired and boundary == 'before_seal' and path.name.startswith('worker-seal.'):
                fired = True
                raise KeyboardInterrupt()
            original_write(path, value)
            if not fired and boundary == 'after_seal' and path.name.startswith('worker-seal.'):
                fired = True
                raise KeyboardInterrupt()
            if not fired and boundary == 'derived_result' and path.name == 'returned-result.json':
                fired = True
                raise KeyboardInterrupt()

        def publish(*args):
            nonlocal fired
            if not fired and boundary == 'publish':
                fired = True
                raise KeyboardInterrupt()
            return original_publish(*args)

        with patch.object(receipts_module, 'write_json', write), patch.object(engine.services.tasks, 'publish_worker_result', publish):
            with self.assertRaises(KeyboardInterrupt):
                engine.start('Implement greeting', self.plan())
        self.assertTrue(fired)
        return engine

    def test_sealed_boundaries_replay_without_worker_even_at_attempt_limit(self):
        for boundary in ('after_seal', 'derived_result', 'publish'):
            with self.subTest(boundary=boundary):
                # Each case gets an isolated target repository/worktree.
                if boundary != 'after_seal':
                    self.setUp()
                engine = self.interrupted(boundary)
                run = engine.resume(engine.run['id'])
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

    def test_successful_hook_has_separate_candidate_binding(self):
        engine = self.engine()
        original = engine.context.emit_hook
        seen = []

        def hook(event, payload, logs):
            if event == 'post_agent' and payload.get('role') == 'worker':
                (engine.worktree / 'hello.py').write_text((engine.worktree / 'hello.py').read_text() + '\n# hook edit\n')
                seen.append(logs)
            return original(event, payload, logs)

        with patch.object(engine.context, 'emit_hook', hook):
            run = engine.start('Implement greeting', self.plan())
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(len(seen), 1)
        self.assertIn('# hook edit', (engine.worktree / 'hello.py').read_text())

    def test_interrupted_hook_unchanged_retries_with_separate_log(self):
        engine = self.engine()
        original = engine.context.emit_hook
        logs_seen = []
        def hook(event, payload, logs):
            if event == 'post_agent' and payload.get('role') == 'worker':
                logs_seen.append(str(logs))
                if len(logs_seen) == 1:
                    raise KeyboardInterrupt()
            return original(event, payload, logs)
        with patch.object(engine.context, 'emit_hook', hook):
            with self.assertRaises(KeyboardInterrupt):
                engine.start('Implement greeting', self.plan())
            run = engine.resume(engine.run['id'])
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['attempts']['greet'], 1)
        self.assertEqual(len(set(logs_seen)), 2)

    def test_interrupted_hook_changed_candidate_blocks(self):
        engine = self.engine()
        original = engine.context.emit_hook
        def hook(event, payload, logs):
            if event == 'post_agent' and payload.get('role') == 'worker':
                with (engine.worktree / 'hello.py').open('a') as stream:
                    stream.write('\n# incomplete hook\n')
                raise KeyboardInterrupt()
            return original(event, payload, logs)
        with patch.object(engine.context, 'emit_hook', hook):
            with self.assertRaises(KeyboardInterrupt):
                engine.start('Implement greeting', self.plan())
        with self.assertRaisesRegex(HarnessError, 'candidate changed'):
            engine.resume(engine.run['id'])
        self.assertEqual(engine.run['attempts']['greet'], 1)

    def test_bound_derived_artifact_bytes_cannot_be_rebound(self):
        engine = self.interrupted('publish')
        receipt = next(iter(engine.run['worker_receipts'].values()))
        from pathlib import Path
        artifact = Path(receipt['artifact'])
        artifact.write_text(artifact.read_text() + '\n')
        with self.assertRaisesRegex(HarnessError, 'returned result changed'):
            engine.resume(engine.run['id'])

    def test_replay_does_not_charge_exhausted_total_or_dispatch_budget(self):
        engine = self.interrupted('publish')
        self.config.data['runtime']['max_total_attempts'] = engine.run['total_attempts']
        self.config.data['runtime']['max_agent_dispatches'] = engine.run['agent_dispatches']
        # Stop after pending publication: reviewers still require their own budget.
        with patch.object(engine.services.tasks, 'continue_task', return_value=False):
            self.assertFalse(engine.services.tasks.replay_worker())
        self.assertIn('pending_task', engine.run)
        self.assertFalse(engine.run['worker_receipts'])
        self.assertEqual(engine.run['agent_dispatches'], 1)
        self.assertEqual(engine.run['total_attempts'], 1)

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
        engine = self.engine(FailedThenReturned())
        with self.assertRaises(HarnessError):
            engine.start('Implement greeting', self.plan())
        run = engine.resume(engine.run['id'])
        self.assertEqual(run['status'], 'completed')
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
                # Replay only the worker handoff; task replanning remains separate.
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
                    self.assertIn('pending_replan', engine.run)

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

    def test_successful_hook_edit_survives_publication_interruption(self):
        engine = self.engine()
        original = engine.context.emit_hook
        hooks = []
        def hook(event, payload, logs):
            if event == 'post_agent' and payload.get('role') == 'worker':
                hooks.append(str(logs))
                with (engine.worktree / 'hello.py').open('a') as stream:
                    stream.write('\n# completed hook\n')
            return original(event, payload, logs)
        with patch.object(engine.context, 'emit_hook', hook):
            with patch.object(engine.services.tasks, 'publish_worker_result', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    engine.start('Implement greeting', self.plan())
            receipt = next(iter(engine.run['worker_receipts'].values()))
            seal = read_json(next((engine.directory / 'dispatches').glob('*/worker-seal.*.json')))
            self.assertNotEqual(receipt['post_hook_digest'], seal['candidate_digest'])
            run = engine.resume(engine.run['id'])
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(len(hooks), 1)
        self.assertEqual(run['attempts']['greet'], 1)

    def test_changed_provider_source_blocks_replay(self):
        engine = self.interrupted('publish')
        worker = next(e for e in engine.run['dispatches'] if e['role'] == 'worker')
        source = engine.directory / 'dispatches' / worker['id'] / 'response.json'
        source.write_text(source.read_text() + '\n')
        with self.assertRaisesRegex(HarnessError, 'source evidence changed'):
            engine.resume(engine.run['id'])
        self.assertEqual(engine.run['attempts']['greet'], 1)

    def test_sealed_format_correction_replays_without_third_worker(self):
        class Correcting(DemoBackend):
            workers = 0
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs['role'] == 'worker':
                    self.workers += 1
                    if self.workers == 1:
                        result['summary'] = 'x' * 32001
                    from harness.util import write_json
                    write_json(kwargs['job'] / 'response.json', result)
                return result
        backend = Correcting()
        engine = self.engine(backend)
        self.config.data['runtime']['max_attempts'] = 2
        original = receipts_module.write_json
        seals = 0
        def write(path, value):
            nonlocal seals
            original(path, value)
            if path.name.startswith('worker-seal.'):
                seals += 1
                if seals == 2:
                    raise KeyboardInterrupt()
        with patch.object(receipts_module, 'write_json', write):
            with self.assertRaises(KeyboardInterrupt):
                engine.start('Implement greeting', self.plan())
        run = engine.resume(engine.run['id'])
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(backend.workers, 2)
        self.assertEqual(run['attempts']['greet'], 2)
        self.assertFalse(run['pending_format_corrections'])
        self.assertEqual(len({e['id'] for e in run['dispatches'] if e['role'] == 'worker'}), 2)

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
                    result['evidence_files'] = []
                    from harness.util import write_json
                    write_json(kwargs['job'] / 'response.json', result)
                return result
        backend = ExtraField()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greeting', self.plan())
        self.assertNotIn('evidence_files', backend.asserted_contract['properties'])
        receipt = engine.run['dispatches'][-1]['worker_receipt']
        self.assertEqual(receipt['schema'], backend.asserted_contract)
        self.assertNotIn('pending_task', engine.run)
        self.assertFalse(engine.run['accepted'])
        with self.assertRaises(HarnessError):
            engine.resume(engine.run['id'])
        self.assertEqual(backend.workers, 2)
        self.assertEqual(engine.run['attempts']['greet'], 2)
        self.assertFalse(engine.run['worker_receipts'])
        self.assertEqual(engine.run['dispatches'][-1]['worker_receipt_consumed'], 'result_rejected')
        self.assertNotIn('pending_task', engine.run)
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
        run = engine.resume(engine.run['id'])
        self.assertEqual(run['status'], 'completed')
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
        with self.assertRaises(HarnessError):
            engine.start('Implement greeting', self.plan())
        worker = next(e for e in engine.run['dispatches'] if e['role'] == 'worker')
        self.assertEqual(worker['worker_receipt_consumed'], 'handoff_rejected')
        self.assertEqual(engine.run['worker_recovery']['greet']['dispatch_id'], worker['id'])
        self.assertFalse(engine.run['worker_receipts'])
        self.assertNotIn('pending_task', engine.run)
        self.assertNotIn('attempt_exemption', worker)
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
