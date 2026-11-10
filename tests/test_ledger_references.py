"""The run ledger keeps digests and references; complete bodies stay in immutable files.

Every model is simulated offline. The fixture below is also run against the base
commit (outside the test suite) to compare state.json sizes, so its module-level
imports only use interfaces that exist on both sides.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

from harness import git
from harness.recovery import snapshot_digest
from harness.util import HarnessError, read_json, write_json
import harness.worker_receipts as receipts_module
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend

VENDOR_FILES = 400
INPUTS = ('packet.json', 'schema.json')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ledger_fixture(case, observe=None):
    """Fixed run: same plan and script, one continuation and two worker attempts."""
    vendor = case.repo / 'vendor'
    vendor.mkdir()
    for index in range(VENDOR_FILES):
        (vendor / f'module_{index:04d}.py').write_text(f'VALUE = {index}\n')
    git.git(case.repo, 'add', '.')
    git.git(case.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
            '-c', 'commit.gpgsign=false', 'commit', '-m', 'vendor fixture')
    command = [sys.executable, '-c', "print('focused continuation check')"]

    def script(backend, kwargs, packet, result):
        if kwargs['role'] == 'worker' and sum(call['role'] == 'worker' for call in backend.calls) == 1:
            result.update(status='blocked', blocker_kind='continuation', blockers=['Need a focused check'],
                          continuation_request={'kind': 'commands', 'commands': [command],
                                                'minimum_timeout_seconds': 0, 'reason': 'Finish the retained candidate'})
    backend = ScriptedBackend(script)
    backend.before_result = observe
    engine = case.engine(backend)
    with contextlib.suppress(HarnessError):
        engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
    case.config.data['permissions']['worker_commands'].append(command)
    result = case.engine(backend).resume(engine.run['id'])
    return engine, backend, result


def waiting(case, expect=HarnessError):
    """A run blocked on a pending command continuation, before any grant."""
    command = [sys.executable, '-c', "print('pending continuation check')"]

    def script(backend, kwargs, packet, result):
        if kwargs['role'] == 'worker' and sum(call['role'] == 'worker' for call in backend.calls) == 1:
            result.update(status='blocked', blocker_kind='continuation', blockers=['Need a focused check'],
                          continuation_request={'kind': 'commands', 'commands': [command],
                                                'minimum_timeout_seconds': 0, 'reason': 'Finish the retained candidate'})
    backend = ScriptedBackend(script)
    engine = case.engine(backend)
    with case.assertRaises(expect):
        engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
    return engine, backend, command


def tamper(path, mode):
    """Delete, change one byte, or replace with a symlink to identical bytes."""
    path = Path(path)
    if mode == 'delete':
        path.unlink()
    elif mode == 'byte':
        raw = bytearray(path.read_bytes())
        raw[raw.index(b'"') + 1] ^= 1
        path.write_bytes(bytes(raw))
    else:
        copy = path.with_name(path.name + '.copy')
        copy.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(copy)


def assert_tampered(case, path, mode, original):
    path = Path(path)
    if mode == 'delete':
        case.assertFalse(path.exists() or path.is_symlink())
    elif mode == 'byte':
        case.assertNotEqual(path.read_bytes(), original)
        case.assertFalse(path.is_symlink())
    else:
        case.assertTrue(path.is_symlink())


class LedgerReferenceTests(RepoCase):
    def plan(self):
        return read_json(ROOT / 'examples/plan.json')

    def workers(self, run):
        return [entry for entry in run['dispatches'] if entry['role'] == 'worker' and entry.get('invoked')]

    def assert_referenced_intent(self, directory, entry, intent):
        self.assertFalse({'packet', 'schema', 'packet_digest'} & set(intent))
        job = Path(directory) / 'dispatches' / entry['id']
        for field, name in zip(('packet', 'schema'), INPUTS):
            self.assertEqual(intent[field + '_artifact'], str(job / name))
            self.assertEqual(intent[field + '_artifact_digest'], sha(job / name))
            self.assertFalse((job / name).is_symlink())

    def assert_projection(self, row, category):
        """Row = complete artifact minus snapshot, plus a digest-bound reference."""
        self.assertNotIn('snapshot', row)
        self.assertEqual(sha(row['artifact']), row['artifact_digest'])
        artifact = read_json(Path(row['artifact']))
        self.assertEqual(snapshot_digest(artifact['snapshot']), row['snapshot_digest'])
        self.assertEqual({k: v for k, v in artifact.items() if k != 'snapshot'},
                         {k: v for k, v in row.items() if k not in ('artifact', 'artifact_digest')})
        # Content identity is still computed over the complete artifact.
        self.assertEqual(row['id'], category + '_' + snapshot_digest({k: v for k, v in artifact.items() if k != 'id'}))
        return artifact

    def test_fixture_ledger_keeps_references_and_digests_without_bodies(self):
        active = []

        def observe(backend, kwargs, packet):
            # The ledger is saved before the backend starts; read the durable copy.
            if kwargs['role'] == 'worker':
                active.append(read_json(kwargs['run_path']))
        engine, backend, result = ledger_fixture(self, observe)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts']['greet'], 2)
        workers = self.workers(result)
        self.assertEqual(len(workers), 2)
        self.assertFalse(result['worker_receipts'])
        # Active receipt intents at both invocations.
        self.assertEqual(len(active), 2)
        for saved, entry in zip(active, workers):
            receipt = saved['worker_receipts']['worker:implement:greet']
            self.assertEqual(receipt['dispatch_id'], entry['id'])
            self.assert_referenced_intent(engine.directory, entry, receipt)
        # Dispatch intents and their seals: the seal copy is the same small intent.
        for entry in workers:
            self.assert_referenced_intent(engine.directory, entry, entry['worker_receipt'])
            seal = next((engine.directory / 'dispatches' / entry['id']).glob('worker-seal.*.json'))
            self.assertEqual(read_json(seal)['intent'], entry['worker_receipt'])
        # One continuation, granted and resumed by a fresh worker.
        self.assertEqual(len(result['continuation_history']), 1)
        row = result['continuation_history'][0]
        artifact = self.assert_projection(row, 'continuations')
        self.assertEqual(row['source']['dispatch_id'], workers[0]['id'])
        self.assertIsNone(result['pending_continuation'])
        second = [call for call in backend.calls if call['role'] == 'worker'][1]['packet']
        self.assertEqual(second['continuation_history'], [row])
        # The ledger carries neither the tree snapshot nor packet/contract bodies.
        raw = (engine.directory / 'state.json').read_text(encoding='utf-8')
        self.assertIn('vendor/module_0000.py', artifact['snapshot']['files'])
        self.assertNotIn('vendor/module_0000.py', raw)
        self.assertNotIn('"permission_guidance"', raw)
        self.assertNotIn('"additionalProperties"', raw)
        # Successful context reports keep a bounded breakdown and exact totals.
        reports = [read_json(path) for path in (engine.directory / 'dispatches').glob('*/context-report.json')]
        self.assertTrue(reports)
        for report in reports:
            self.assertLessEqual(sum(len(r['path']) > 1 for r in report['field_sizes']), 30)
            self.assertGreater(report['field_sizes_omitted'], 0)
            self.assertEqual(report['field_sizes'][0], {'path': [], 'chars': report['required_chars'],
                             'overhead_chars': report['field_sizes'][0]['overhead_chars'], 'container': True})
            self.assertEqual(report['used_chars'], report['required_chars'] + report['memory_chars'])

    def test_execution_guidance_is_recorded_by_reference_and_projected_to_next_worker(self):
        engine, backend, command = waiting(self)
        source = engine.run['continuation_history'][-1]
        path = self.repo / 'guidance.json'
        summary = 'Check the rollback path before the formal gates.'
        write_json(path, {'task_id': 'greet', 'source_dispatch_id': source['source']['dispatch_id'],
                          'source_snapshot_digest': source['snapshot_digest'], 'summary': summary})
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], execution_guidance=path)
        self.assertEqual(len(backend.calls), calls)
        saved = read_json(engine.directory / 'state.json')
        self.assertEqual(len(saved['execution_guidance']), 1)
        row = saved['execution_guidance'][0]
        artifact = self.assert_projection(row, 'execution-guidance')
        self.assertEqual(artifact['snapshot_digest'], source['snapshot_digest'])
        self.config.data['permissions']['worker_commands'].append(command)
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        projected = [call for call in backend.calls if call['role'] == 'worker'][-1]['packet']['execution_guidance']
        self.assertEqual(projected[0]['id'], row['id'])
        self.assertEqual(projected[0]['input']['summary'], summary)
        self.assertNotIn('snapshot', projected[0])
        self.assertTrue(projected[0]['applicability']['current_candidate_matches'])

    def test_continuation_rejection_keeps_original_snapshot_and_is_consumed_by_fresh_worker(self):
        engine, backend, _ = waiting(self)
        original = engine.run['continuation_history'][-1]
        path = self.repo / 'rejection.json'
        write_json(path, {'continuation_id': original['id'], 'task_id': 'greet',
                          'source_dispatch_id': original['source']['dispatch_id'],
                          'source_snapshot_digest': original['snapshot_digest'],
                          'reason': 'Complete with another approach.'})
        result = self.engine(backend).resume(engine.run['id'], reject_continuation=path)
        self.assertEqual(result['status'], 'completed')
        rejection = result['continuation_rejections'][0]
        artifact = self.assert_projection(rejection, 'continuation-rejections')
        self.assertEqual(artifact['snapshot'], read_json(Path(original['artifact']))['snapshot'])
        workers = self.workers(result)
        self.assertEqual(len(workers), 2)
        self.assertEqual(workers[1]['continuation_rejection_id'], rejection['id'])
        packet = [call for call in backend.calls if call['role'] == 'worker'][1]['packet']
        self.assertEqual(packet['active_continuation_rejection'], rejection['id'])
        self.assertNotIn('snapshot', packet['continuation_rejections'][0])

    def test_interrupt_after_complete_artifact_before_ledger_save_adopts_same_artifact(self):
        import harness.continuation as continuation_module
        original_write = continuation_module.write_json

        def interrupt_after(category):
            def write(path, value):
                original_write(path, value)
                if path.parent.name == category:
                    raise KeyboardInterrupt()
            return patch.object(continuation_module, 'write_json', write)
        for index, category in enumerate(('continuations', 'execution-guidance', 'continuation-rejections')):
            with self.subTest(category=category):
                if index:
                    self.setUp()
                if category == 'continuations':
                    # The sealed worker handoff publishes the record; replay adopts it.
                    with interrupt_after(category):
                        engine, backend, command = waiting(self, KeyboardInterrupt)
                    saved = read_json(engine.directory / 'state.json')
                    self.assertEqual(saved['continuation_history'], [])
                    self.assertTrue(saved['worker_receipts'])
                    with self.assertRaises(HarnessError):
                        self.engine(backend).resume(engine.run['id'])
                    option = {}
                else:
                    engine, backend, command = waiting(self)
                    row = engine.run['continuation_history'][-1]
                    path = self.repo / 'operator.json'
                    if category == 'execution-guidance':
                        value = {'task_id': 'greet', 'source_dispatch_id': row['source']['dispatch_id'],
                                 'source_snapshot_digest': row['snapshot_digest'], 'summary': 'Keep the retained candidate.'}
                        option = {'execution_guidance': path}
                    else:
                        value = {'continuation_id': row['id'], 'task_id': 'greet',
                                 'source_dispatch_id': row['source']['dispatch_id'],
                                 'source_snapshot_digest': row['snapshot_digest'], 'reason': 'Use another approach.'}
                        option = {'reject_continuation': path}
                    write_json(path, value)
                    key = 'execution_guidance' if category == 'execution-guidance' else 'continuation_rejections'
                    with interrupt_after(category), self.assertRaises(KeyboardInterrupt):
                        self.engine(backend).resume(engine.run['id'], **option)
                    saved = read_json(engine.directory / 'state.json')
                    self.assertFalse(saved.get(key))
                    self.assertEqual(saved['pending_continuation'], row['id'])
                    if category == 'execution-guidance':
                        # Identical input adopts the orphan artifact; the request still waits.
                        with self.assertRaises(HarnessError):
                            self.engine(backend).resume(engine.run['id'], **option)
                        option = {}
                calls = sum(call['role'] == 'worker' for call in backend.calls)
                self.assertEqual(calls, 1)
                files = list((engine.directory / category).glob('*.json'))
                self.assertEqual(len(files), 1)
                if category != 'continuation-rejections':
                    self.config.data['permissions']['worker_commands'].append(command)
                result = self.engine(backend).resume(engine.run['id'], **option)
                self.assertEqual(result['status'], 'completed')
                key = {'continuations': 'continuation_history', 'execution-guidance': 'execution_guidance',
                       'continuation-rejections': 'continuation_rejections'}[category]
                self.assertEqual(len(result[key]), 1)
                self.assertEqual(result[key][0]['artifact'], str(files[0]))
                self.assert_projection(result[key][0], category)
                self.assertEqual(list((engine.directory / category).glob('*.json')), files)
                self.assertEqual(sum(call['role'] == 'worker' for call in backend.calls), 2)

    def interrupted_publish(self, backend):
        engine = self.engine(backend)
        with patch.object(engine.services.tasks, 'publish_worker_result', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine.start('Implement greet', self.plan())
        self.assertTrue(engine.run['worker_receipts'])
        return engine

    def test_sealed_return_replays_with_referenced_contract_and_no_second_worker(self):
        backend = ScriptedBackend()
        engine = self.interrupted_publish(backend)
        worker = self.workers(engine.run)[0]
        self.assert_referenced_intent(engine.directory, worker, engine.run['worker_receipts']['worker:implement:greet'])
        reads = []
        original = receipts_module.read_worker_input

        def spy(directory, entry, intent, field):
            reads.append((entry['id'], field))
            return original(directory, entry, intent, field)
        with patch.object(receipts_module, 'read_worker_input', spy):
            run = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(sum(call['role'] == 'worker' for call in backend.calls), 1)
        self.assertEqual(run['attempts']['greet'], 1)
        self.assertIn((worker['id'], 'schema'), reads)

    def format_invalid_then_interrupted_after_first_seal(self):
        def script(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker' and sum(call['role'] == 'worker' for call in backend.calls) == 1:
                result['summary'] = 'x' * 32001
                write_json(kwargs['job'] / 'response.json', result)
        backend = ScriptedBackend(script)
        engine = self.engine(backend)
        original = receipts_module.write_json

        def write(path, value):
            original(path, value)
            if path.name.startswith('worker-seal.'):
                raise KeyboardInterrupt()
        with patch.object(receipts_module, 'write_json', write), self.assertRaises(KeyboardInterrupt):
            engine.start('Implement greet', self.plan())
        return engine, backend

    def test_automatic_format_correction_after_interruption_reads_original_packet_file(self):
        engine, backend = self.format_invalid_then_interrupted_after_first_seal()
        first = self.workers(engine.run)[0]
        stored = read_json(engine.directory / 'dispatches' / first['id'] / 'packet.json')
        reads = []
        original = receipts_module.read_worker_input

        def spy(directory, entry, intent, field):
            reads.append((entry['id'], field))
            return original(directory, entry, intent, field)
        with patch.object(receipts_module, 'read_worker_input', spy):
            run = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(run['status'], 'completed')
        calls = [call for call in backend.calls if call['role'] == 'worker']
        self.assertEqual(len(calls), 2)
        correction = calls[1]['packet']
        self.assertEqual(correction['format_correction']['previous_dispatch'], first['id'])
        # Resume replays with an empty packet; the correction's task input can
        # only come from the verified original packet file.
        for field in ('goal', 'task', 'instruction', 'dependencies', 'verification_commands'):
            self.assertEqual(correction[field], calls[0]['packet'][field], field)
            self.assertEqual(stored[field], calls[0]['packet'][field], field)
        self.assertIn((first['id'], 'packet'), reads)
        self.assertEqual(run['attempts']['greet'], 2)
        self.assertEqual(self.workers(run)[1]['format_correction'], True)

    def test_format_correction_refuses_changed_original_packet(self):
        engine, backend = self.format_invalid_then_interrupted_after_first_seal()
        first = self.workers(engine.run)[0]
        packet = engine.directory / 'dispatches' / first['id'] / 'packet.json'
        packet.write_bytes(packet.read_bytes().replace(b'Implement greet', b'Implement GREET', 1))
        with self.assertRaisesRegex(HarnessError, 'packet artifact changed'):
            engine.services.worker_receipts.original_packet('worker:implement:greet')
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, 'packet artifact changed'):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)
        self.assertIn(b'Implement GREET', packet.read_bytes())

    def test_changed_worker_inputs_stop_resume_and_preserve_evidence(self):
        cases = [(name, mode) for name in INPUTS for mode in ('delete', 'byte', 'symlink')]
        for index, (name, mode) in enumerate(cases):
            with self.subTest(name=name, mode=mode):
                if index:
                    self.setUp()
                backend = ScriptedBackend()
                engine = self.interrupted_publish(backend)
                worker = self.workers(engine.run)[0]
                path = engine.directory / 'dispatches' / worker['id'] / name
                original = path.read_bytes()
                tamper(path, mode)
                state = (engine.directory / 'state.json').read_bytes()
                calls = len(backend.calls)
                with self.assertRaisesRegex(HarnessError, 'preserve evidence'):
                    self.engine(backend).resume(engine.run['id'])
                self.assertEqual((engine.directory / 'state.json').read_bytes(), state)
                self.assertEqual(len(backend.calls), calls)
                assert_tampered(self, path, mode, original)
                # Restoring the exact bytes restores the original replay path.
                path.unlink(missing_ok=True)
                path.write_bytes(original)
                self.assertEqual(self.engine(backend).resume(engine.run['id'])['status'], 'completed')
                self.assertEqual(sum(call['role'] == 'worker' for call in backend.calls), 1)

    def test_changed_continuation_artifact_stops_resume_and_preserves_evidence(self):
        for index, mode in enumerate(('delete', 'byte', 'symlink')):
            with self.subTest(mode=mode):
                if index:
                    self.setUp()
                engine, backend, command = waiting(self)
                self.config.data['permissions']['worker_commands'].append(command)
                path = Path(engine.run['continuation_history'][-1]['artifact'])
                original = path.read_bytes()
                tamper(path, mode)
                state = (engine.directory / 'state.json').read_bytes()
                calls = len(backend.calls)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run['id'])
                self.assertEqual((engine.directory / 'state.json').read_bytes(), state)
                self.assertEqual(len(backend.calls), calls)
                assert_tampered(self, path, mode, original)
                path.unlink(missing_ok=True)
                path.write_bytes(original)
                self.assertEqual(self.engine(backend).resume(engine.run['id'])['status'], 'completed')

    def test_input_write_failure_charges_nothing_and_later_resume_dispatches(self):
        for index, name in enumerate(INPUTS):
            with self.subTest(name=name):
                if index:
                    self.setUp()
                backend = ScriptedBackend()
                engine = self.engine(backend)
                original = receipts_module.write_json

                def failing(path, value):
                    if path.name == name:
                        raise OSError(28, 'No space left on device')
                    return original(path, value)
                with patch.object(receipts_module, 'write_json', failing):
                    with self.assertRaisesRegex(HarnessError, 'no budget was charged'):
                        engine.start('Implement greet', self.plan())
                saved = read_json(engine.directory / 'state.json')
                self.assertEqual(saved['attempts'].get('greet', 0), 0)
                self.assertEqual(sum(saved['attempts'].values()), 0)
                self.assertEqual(saved['agent_dispatches'], 0)
                self.assertFalse(any(entry.get('invoked') for entry in saved['dispatches']))
                self.assertFalse(saved['worker_receipts'])
                failed = next(entry for entry in saved['dispatches'] if entry['role'] == 'worker')
                self.assertNotIn('worker_receipt', failed)
                self.assertEqual(failed['process_state'], 'exited')
                self.assertFalse(backend.calls)
                run = self.engine(backend).resume(engine.run['id'])
                self.assertEqual(run['status'], 'completed')
                self.assertEqual(run['attempts']['greet'], 1)
                self.assertEqual(sum(run['attempts'].values()), 1)
                self.assertEqual(sum(call['role'] == 'worker' for call in backend.calls), 1)
                self.assertNotEqual(self.workers(run)[0]['id'], failed['id'])

    def test_interrupt_after_inputs_published_before_ledger_save_charges_nothing(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        publish = engine.services.worker_receipts.publish_inputs
        published = []

        def interrupted(*args):
            published.append(publish(*args))
            raise KeyboardInterrupt()
        with patch.object(engine.services.worker_receipts, 'publish_inputs', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                engine.start('Implement greet', self.plan())
        saved = read_json(engine.directory / 'state.json')
        self.assertEqual(saved['status'], 'interrupted')
        self.assertEqual(saved['attempts'].get('greet', 0), 0)
        self.assertEqual((sum(saved['attempts'].values()), saved['agent_dispatches']), (0, 0))
        self.assertFalse(any(entry.get('invoked') for entry in saved['dispatches']))
        self.assertFalse(saved['worker_receipts'])
        orphan = next(entry for entry in saved['dispatches'] if entry['role'] == 'worker')
        for name in INPUTS:
            self.assertTrue((engine.directory / 'dispatches' / orphan['id'] / name).is_file())
        self.assertFalse(backend.calls)
        # No exit confirmation is needed and no unaccounted call exists.
        run = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['attempts']['greet'], 1)
        self.assertEqual(run['agent_dispatches'], sum(1 for entry in run['dispatches'] if entry.get('invoked')))
        self.assertEqual(sum(call['role'] == 'worker' for call in backend.calls), 1)
        self.assertNotEqual(self.workers(run)[0]['id'], orphan['id'])

    def test_interrupt_after_ledger_save_before_backend_start_requires_confirmation(self):
        class StopBeforeStart(ScriptedBackend):
            saved = None

            def invoke(self, **kwargs):
                if kwargs['role'] == 'worker' and self.saved is None:
                    self.saved = read_json(kwargs['run_path'])
                    raise KeyboardInterrupt()
                return super().invoke(**kwargs)
        backend = StopBeforeStart()
        engine = self.engine(backend)
        with self.assertRaises(KeyboardInterrupt):
            engine.start('Implement greet', self.plan())
        durable = backend.saved
        entry = next(item for item in durable['dispatches'] if item['role'] == 'worker')
        # Inputs, budgets, the receipt intent and invoked were saved once, together.
        self.assertEqual((entry['invoked'], entry['process_state']), (True, 'starting'))
        self.assertEqual((durable['attempts']['greet'], sum(durable['attempts'].values()), durable['agent_dispatches']), (1, 1, 1))
        self.assertEqual(durable['worker_receipts']['worker:implement:greet'], {**entry['worker_receipt']})
        self.assert_referenced_intent(engine.directory, entry, entry['worker_receipt'])
        self.assertFalse(backend.calls)
        with self.assertRaisesRegex(HarnessError, '无法确认'):
            self.engine(backend).resume(engine.run['id'])
        run = self.engine(backend).resume(engine.run['id'], confirm_exited=[entry['id']])
        self.assertEqual(run['status'], 'completed')
        first = next(item for item in run['dispatches'] if item['id'] == entry['id'])
        self.assertEqual(first['worker_return_evidence']['state'], 'retryable')
        self.assertEqual(first['worker_receipt_consumed'], 'provider_failure')
        # The interrupted invocation stays charged; only one worker actually ran.
        self.assertEqual(run['attempts']['greet'], 2)
        self.assertEqual(sum(call['role'] == 'worker' for call in backend.calls), 1)

    def test_cli_backend_uses_published_worker_contract_without_rewriting(self):
        from harness.adapters import CLIBackend
        from harness.contracts import WORK, envelope
        from harness.util import dump
        stubs = self.repo / 'stub-bin'
        stubs.mkdir()
        marker = self.repo / 'real-cli-called'
        for name in ('claude', 'codex'):
            stub = stubs / name
            stub.write_text(f'#!/bin/sh\necho {name} >> {marker}\nexit 97\n')
            stub.chmod(0o755)
        schema = envelope(WORK, 'greet', 'dispatch-1')
        value = {'task_id': 'greet', 'dispatch_id': 'dispatch-1', 'status': 'completed', 'summary': 'stub',
                 'evidence': [], 'blockers': [], 'blocker_kind': 'none', 'proposed_memories': []}
        terminal = {'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': value}
        program = 'import json; print(' + repr(json.dumps(terminal)) + ', flush=True)'
        exact = (dump(schema) + '\n').encode()
        for index, mode in enumerate(('missing', 'different', 'symlink', 'exact')):
            with self.subTest(mode=mode):
                job = self.repo / '.harness' / f'job-{index}'
                job.mkdir(parents=True)
                contract = job / 'schema.json'
                if mode == 'different':
                    contract.write_bytes(exact.replace(b'"greet"', b'"other"'))
                elif mode == 'symlink':
                    (job / 'copy.json').write_bytes(exact)
                    contract.symlink_to(job / 'copy.json')
                elif mode == 'exact':
                    contract.write_bytes(exact)
                before = contract.read_bytes() if contract.exists() else None
                inode = os.stat(contract, follow_symlinks=False).st_ino if contract.exists() or contract.is_symlink() else None
                cli = CLIBackend(self.config, self.repo / '.harness')
                cli.command = lambda *args, **kwargs: [sys.executable, '-c', program]
                call = dict(role='worker', model=self.config.role('worker'), cwd=self.repo, job=job, schema=schema,
                            prompt='offline', run_path=None, on_start=lambda pgid: None)
                with patch.dict(os.environ, {'PATH': str(stubs) + os.pathsep + os.environ['PATH']}):
                    if mode == 'exact':
                        self.assertEqual(cli.invoke(**call), value)
                    else:
                        with self.assertRaisesRegex(HarnessError, '契约文件'):
                            cli.invoke(**call)
                        self.assertFalse((job / 'process.stdout').exists())
                if before is not None:
                    self.assertEqual(contract.read_bytes(), before)
                    self.assertEqual(os.stat(contract, follow_symlinks=False).st_ino, inode)
                else:
                    self.assertFalse(contract.exists())
        self.assertFalse(marker.exists())

    def test_successful_context_report_keeps_top_level_and_thirty_largest_fields(self):
        from harness.context import ContextCapacityError, compose, field_size_report, prompt_json
        required = {'role': 'worker', 'kind': 'implement', 'task_id': 'greet', 'dispatch_id': 'd',
                    'packet': {'items': [{'text': 'x' * index, 'index': index} for index in range(60)],
                               'goal': 'goal'}}
        memories = [{'id': 'm1', 'body': 'm' * 50}, {'id': 'm2', 'body': 'n' * 10_000}]
        diagnostics = {'counts': {'active_candidates': 2}, 'candidates': [{'id': 'm1'}, {'id': 'm2'}]}
        budget = len(prompt_json(required)) + 200
        prompt, report = compose(required, memories, budget, memory_diagnostics=diagnostics)
        full = field_size_report(required)
        nested = [row for row in full if len(row['path']) > 1]
        kept = [row for row in report['field_sizes'] if len(row['path']) > 1]
        self.assertEqual(len(kept), 30)
        self.assertEqual(report['field_sizes_omitted'], len(nested) - 30)
        self.assertEqual([row for row in full if len(row['path']) <= 1],
                         [row for row in report['field_sizes'] if len(row['path']) <= 1])
        self.assertEqual(report['field_sizes'], [row for row in full if row in report['field_sizes']])
        self.assertGreaterEqual(min(row['chars'] for row in kept),
                                max(row['chars'] for row in nested if row not in kept))
        # Totals and memory diagnostics are computed exactly as before.
        self.assertEqual(report['required_chars'], len(prompt_json(required)))
        self.assertEqual(report['used_chars'], len(prompt))
        self.assertEqual(report['memory_chars'], len(prompt) - len(prompt_json(required)))
        self.assertEqual((report['memory_ids'], report['omitted_memories']), (['m1'], 1))
        self.assertEqual(report['memory_retrieval']['counts'], {'active_candidates': 2, 'included': 1, 'capacity_excluded': 1})
        self.assertEqual(report['memory_retrieval']['candidates'][1], {'id': 'm2', 'included': False, 'exclusion_stage': 'capacity'})
        with self.assertRaises(ContextCapacityError) as caught:
            compose(required, memories, 10)
        overflow = caught.exception.report
        self.assertEqual(overflow['field_sizes'], full)
        self.assertNotIn('field_sizes_omitted', overflow)
        self.assertEqual(overflow['required_chars'], report['required_chars'])

    def embedded_v15_terminal(self):
        """A completed run rewritten into the pre-change embedded v15 shape."""
        self.config.data['verification'].update(fresh_checkout=True,
            commands=[[sys.executable, '-c', "from pathlib import Path; assert Path('hello.py').is_file()"]])
        engine, backend, run = ledger_fixture(self)
        self.assertEqual(run['status'], 'completed')
        shutil.rmtree(engine.worktree / '__pycache__', ignore_errors=True)
        checkout = Path(read_json(next(engine.directory.glob('verification/final/*/verification-*/checkout.json')))['worktree'])
        self.addCleanup(shutil.rmtree, checkout.parents[1], True)
        for entry in self.workers(run):
            intent, job = entry['worker_receipt'], engine.directory / 'dispatches' / entry['id']
            packet, schema = read_json(job / 'packet.json'), read_json(job / 'schema.json')
            for field in ('packet', 'schema'):
                intent.pop(field + '_artifact')
                intent.pop(field + '_artifact_digest')
            intent.update(schema=schema, packet=packet, packet_digest=snapshot_digest(packet))
            for name in INPUTS:
                (job / name).unlink()
        run['continuation_history'] = [{**read_json(Path(row['artifact'])), 'artifact': row['artifact'],
                                         'artifact_digest': row['artifact_digest']}
                                        for row in run['continuation_history']]
        self.assertEqual(run['schema_version'], 16)
        # Historical schema-15 fields: the saved run-wide counter and budget policy.
        run.update(schema_version=15, total_attempts=sum(run['attempts'].values()), attempt_budget_policy='work')
        write_json(engine.directory / 'state.json', run)
        return engine, backend, run

    def test_completed_embedded_v15_ledger_stays_inspectable_and_cleanable(self):
        from harness import cli
        from harness.cleanup import cleanup_verification
        from harness.cli import status_summary
        from harness.run_cleanup import cleanup_run
        engine, backend, run = self.embedded_v15_terminal()
        path = engine.directory / 'state.json'
        before = path.read_bytes()
        self.assertEqual(receipts_module.receipt_shape(run), receipts_module.EMBEDDED)
        self.assertEqual(status_summary(run)['status'], 'completed')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['--repo', str(self.repo), 'status', run['id']]), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'completed')
        self.assertEqual(cleanup_run(self.repo, run['id'])['status'], 'eligible')
        self.assertEqual([item['status'] for item in cleanup_verification(self.repo, run['id'])['checkouts']], ['eligible'])
        self.assertEqual(path.read_bytes(), before)
        # Execution never adopts the historical shape; nothing is dispatched or saved.
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, '旧 run.*台账版本 15'):
            self.engine(backend).resume(run['id'])
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(path.read_bytes(), before)
        # The embedded shape is still validated, not skipped.
        for change in ('digest', 'mixed'):
            with self.subTest(change=change):
                damaged = json.loads(before)
                worker = self.workers(damaged)[0]
                if change == 'digest':
                    worker['worker_receipt']['packet']['goal'] = 'changed'
                else:
                    job = engine.directory / 'dispatches' / worker['id']
                    intent = worker['worker_receipt']
                    for field, name in zip(('packet', 'schema'), INPUTS):
                        write_json(job / name, intent.pop(field))
                        intent[field + '_artifact'] = str(job / name)
                        intent[field + '_artifact_digest'] = sha(job / name)
                    intent.pop('packet_digest')
                write_json(path, damaged)
                with self.assertRaisesRegex(HarnessError, 'worker receipt linkage'):
                    cleanup_run(self.repo, run['id'])
                with self.assertRaisesRegex(HarnessError, 'worker receipt linkage'):
                    cleanup_verification(self.repo, run['id'])
                path.write_bytes(before)


if __name__ == '__main__':
    import unittest
    unittest.main()
