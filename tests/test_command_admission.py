import json
import multiprocessing
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.command_server import Broker, command_id, command_processes, create_manifest, handle
from harness.util import HarnessError, read_json, with_descriptors, write_json
from harness.versions import RUN_SCHEMA_VERSION
from test_provider_failures import FifoWatchdog

ADMISSION_LOCK = 'command-admission.lock'


def invoke(path, digest, identifier, queue):
    try:
        queue.put(Broker(path, digest).run_command({'command_id': identifier}))
    except HarnessError as error:
        queue.put({'blocked': str(error)})


class CommandAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.job = self.root / 'worker'
        self.command = {'cwd': '.', 'argv': [sys.executable, '-c', 'import time;time.sleep(.3);print("done")']}
        config = {'verification': {'commands': [self.command]}, 'permissions': {'worker_commands': []},
                  'runtime': {'verify_timeout_seconds': 2, 'agent_timeout_seconds': 10}}
        self.meta = create_manifest(config, self.root, self.job)
        self.broker = Broker(self.meta['artifact'], self.meta['artifact_digest'])

    def journal(self, letter, state='starting', confirmed=False):
        folder = self.job / 'commands' / (letter * 32)
        folder.mkdir()
        write_json(folder / 'process.json', {'id': 'worker:command:' + folder.name,
                   'command_id': command_id(self.command), 'pgid': None,
                   'process_state': state, 'exit_confirmed': confirmed})
        return folder

    def test_all_prior_journals_checked(self):
        self.journal('a')
        self.journal('b', 'prepared')
        with self.assertRaises(HarnessError):
            self.broker.run_command({'command_id': command_id(self.command)})
        self.assertEqual(len(list((self.job / 'commands').iterdir())), 2)

    def test_missing_receipt_blocks_even_confirmed_exit(self):
        self.journal('a', 'exited', True)
        with self.assertRaisesRegex(HarnessError, 'evidence incomplete'):
            self.broker.run_command({'command_id': command_id(self.command)})

    def test_prepared_crash_did_not_start(self):
        self.journal('a', 'prepared')
        result = self.broker.run_command({'command_id': command_id(self.command)})
        self.assertEqual(result['exit_code'], 0)

    def test_short_lived_descendant_drain_allows_following_commands(self):
        argv = {'cwd': '.', 'argv': [sys.executable, '-c', 'import subprocess,sys; '
                'subprocess.Popen([sys.executable,"-c","import time;time.sleep(.1)"],'
                'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);print("done")']}
        config = {'verification': {'commands': [argv]}, 'permissions': {'worker_commands': []},
                  'runtime': {'verify_timeout_seconds': 2, 'agent_timeout_seconds': 10}}
        meta = create_manifest(config, self.root, self.root / 'short-child-worker')
        broker = Broker(meta['artifact'], meta['artifact_digest'])
        for _ in range(3):
            result = broker.run_command({'command_id': command_id(argv)})
            self.assertEqual(result['exit_code'], 0)
            self.assertTrue(result['exit_confirmed'])
            self.assertTrue(result['evidence_complete'])

    def test_parent_budget_rejects_without_intent(self):
        with patch('harness.command_server.time.monotonic', return_value=self.broker.manifest['parent_deadline']):
            with self.assertRaisesRegex(HarnessError, 'budget exhausted'):
                self.broker.run_command({'command_id': command_id(self.command)})
        self.assertEqual(list((self.job / 'commands').iterdir()), [])

    def test_intent_write_failure_never_starts_or_publishes(self):
        with patch('harness.command_server.write_json', side_effect=OSError('disk full')), patch('harness.command_server.run_process') as runner:
            with self.assertRaises(OSError):
                self.broker.run_command({'command_id': command_id(self.command)})
            runner.assert_not_called()
        # No journal-less folder was published, so the next call is admitted.
        self.assertEqual(list((self.job / 'commands').iterdir()), [])
        self.assertEqual(self.broker.run_command({'command_id': command_id(self.command)})['exit_code'], 0)

    def test_receipt_write_failure_blocks_next_call(self):
        original = write_json
        def fail_receipt(path, value):
            if Path(path).name == 'receipt.json':
                raise OSError('receipt disk full')
            return original(path, value)
        with patch('harness.command_server.write_json', side_effect=fail_receipt):
            result = self.broker.run_command({'command_id': command_id(self.command)})
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(result['evidence_complete'])
        with self.assertRaisesRegex(HarnessError, 'evidence incomplete'):
            self.broker.run_command({'command_id': command_id(self.command)})

    def test_output_tampering_blocks_next_call(self):
        result = self.broker.run_command({'command_id': command_id(self.command)})
        Path(result['stdout_path']).write_text('changed')
        with self.assertRaisesRegex(HarnessError, 'evidence incomplete'):
            self.broker.run_command({'command_id': command_id(self.command)})

    def test_frozen_parent_bounds_effective_command_timeout(self):
        deadline = self.broker.manifest['parent_deadline']
        with patch('harness.command_server.time.monotonic', return_value=deadline - 2.1):
            _, _, timeout, _, _, root = with_descriptors(
                lambda held: self.broker._admit({'command_id': command_id(self.command)}, held))
        # The admitted root is opened into the caller's holder, whose cleanup closes it.
        with self.assertRaises(OSError):
            os.fstat(root)
        self.assertAlmostEqual(timeout, .1)

    def test_cleanup_uncertainty_preserves_wait_code_and_blocks(self):
        from harness.process_runtime import ProcessOutcome
        facts = ProcessOutcome(started=True, startup_state='started', exit_code=0,
                               exit_confirmed=False, evidence_complete=False,
                               cleanup_errors=[{'step': 'observe_group', 'errno': 1}])
        def runner(argv, cwd, log, **kwargs):
            log.with_suffix('.stdout').write_text('done')
            log.with_suffix('.stderr').write_text('')
            kwargs['on_start'](123)
            kwargs['on_outcome'](facts)
            error = HarnessError('EPERM')
            error.outcome = facts
            raise error
        with patch('harness.command_server.run_process', side_effect=runner):
            result = self.broker.run_command({'command_id': command_id(self.command)})
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['termination_reason'], 'normal')
        self.assertFalse(result['exit_confirmed'])
        with patch('harness.command_server.group_alive', return_value=False):
            with self.assertRaises(HarnessError):
                self.broker.run_command({'command_id': command_id(self.command)})

    def test_two_process_callers_cannot_overlap(self):
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        args = (self.meta['artifact'], self.meta['artifact_digest'], command_id(self.command), queue)
        first = context.Process(target=invoke, args=args)
        first.start()
        until = time.monotonic() + 3
        while not list((self.job / 'commands').iterdir()) and time.monotonic() < until:
            time.sleep(.005)
        second = context.Process(target=invoke, args=args)
        second.start()
        first.join(5)
        second.join(5)
        results = [queue.get(timeout=2), queue.get(timeout=2)]
        self.assertEqual(sum('blocked' in result for result in results), 1, results)
        self.assertEqual(sum(result.get('exit_code') == 0 for result in results), 1, results)

    def test_slow_admission_intent_exhausts_parent_budget_without_start(self):
        witness = self.root / 'started'
        argv = {'cwd': '.', 'argv': [sys.executable, '-c', f'open({str(witness)!r},"w").write("started")']}
        config = {'verification': {'commands': [argv]}, 'permissions': {'worker_commands': []},
                  'runtime': {'verify_timeout_seconds': 2, 'agent_timeout_seconds': 10}}
        meta = create_manifest(config, self.root, self.root / 'slow', parent_deadline=time.monotonic() + 2.1)
        broker = Broker(meta['artifact'], meta['artifact_digest'])
        def slow(path, value):
            if Path(path).name == 'process.json' and value['process_state'] == 'starting':
                time.sleep(.2)
            write_json(path, value)
        with patch('harness.command_server.write_json', side_effect=slow):
            result = broker.run_command({'command_id': command_id(argv)})
        self.assertFalse(witness.exists())
        self.assertTrue(result['timed_out'])
        self.assertFalse(result['lifecycle']['started'])
        self.assertTrue(result['exit_confirmed'])

    def test_broker_late_signal_restores_handlers_and_retains_committed_lifecycle(self):
        import os
        import signal
        original = signal.signal
        previous = {sig: signal.getsignal(sig) for sig in
                    (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        injected = False
        def handoff(sig, handler):
            nonlocal injected
            result = original(sig, handler)
            # The inner supervisor restores the broker handler before receipt
            # creation; inject only at the broker's later cleanup handoff.
            receipts = list((self.job / 'commands').glob('*/receipt.json'))
            if (sig == signal.SIGINT and getattr(handler, '__name__', '') == 'interrupted'
                    and receipts and not injected):
                injected = True
                os.kill(os.getpid(), signal.SIGINT)
            return result
        with patch('harness.process_runtime.signal.signal', side_effect=handoff):
            result = self.broker.run_command({'command_id': command_id(self.command)})
        self.assertTrue(injected)
        self.assertTrue(result['response_cancelled'])
        self.assertTrue(result['error'])
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['lifecycle']['termination_reason'], 'normal')
        for sig, handler in previous.items():
            self.assertIs(signal.getsignal(sig), handler)
        from harness.util import read_json
        receipt = next((self.job / 'commands').glob('*/receipt.json'))
        self.assertTrue(read_json(receipt)['response_cancelled'])

    def test_receipt_real_interrupt_records_cancellation_and_completed_wait(self):
        import os
        import signal
        argv = self.command
        original = write_json
        def interrupt(path, value):
            if Path(path).name == 'receipt.json':
                os.kill(os.getpid(), signal.SIGINT)
            original(path, value)
        with patch('harness.command_server.write_json', side_effect=interrupt):
            result = self.broker.run_command({'command_id': command_id(argv)})
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(result['evidence_complete'])
        self.assertEqual(result['termination_reason'], 'cancelled')
        self.assertTrue(result['error'])
        from harness.util import read_json
        receipt = next((self.job / 'commands').glob('*/receipt.json'))
        self.assertEqual(read_json(receipt)['termination_reason'], 'cancelled')
        self.assertEqual(read_json(receipt.parent / 'output.outcome.json')['termination_reason'], 'cancelled')

    def test_persistence_fault_keeps_execution_failure(self):
        from harness.util import ProcessTimeout
        def timeout_runner(argv, cwd, log, **kwargs):
            log.with_suffix('.stdout').write_text('')
            log.with_suffix('.stderr').write_text('')
            raise ProcessTimeout(.1, log)
        def fail(path, value):
            if Path(path).name == 'receipt.json':
                raise KeyboardInterrupt('injected persistence interrupt')
            write_json(path, value)
        with patch('harness.command_server.run_process', side_effect=timeout_runner), \
                patch('harness.command_server.write_json', side_effect=fail):
            result = self.broker.run_command({'command_id': command_id(self.command)})
        self.assertTrue(result['timed_out'])
        self.assertIn('injected persistence interrupt', result['persistence_error'])
        self.assertNotIn('injected persistence interrupt', result['error'])
        self.assertFalse(result['evidence_complete'])


def tree(root):
    """Every entry below root, links listed but not followed."""
    found = []
    for directory, folders, names in os.walk(root):
        found += [os.path.relpath(os.path.join(directory, name), root) for name in folders + names]
    return sorted(found)


class AdmissionLockTests(unittest.TestCase):
    """The admission lock is created with the manifest; a refused call creates no file."""

    COMMAND = {'cwd': '.', 'argv': [sys.executable, '-c', 'pass']}
    SCOPE = {'task_id': 'cleanup', 'allowed_paths': ['legacy/**']}
    CALLS = (('run_command', {'command_id': command_id(COMMAND)}), ('delete_path', {'path': 'legacy/old.txt'}))

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.config = {'verification': {'commands': [self.COMMAND]}, 'permissions': {'worker_commands': []},
                       'runtime': {'verify_timeout_seconds': 5, 'agent_timeout_seconds': 30}}

    def unbound(self, name):
        job = self.root / 'dispatches' / name
        meta = create_manifest(self.config, self.root, job, None, delete_scope=self.SCOPE)
        return job, Broker(meta['artifact'], meta['artifact_digest'])

    def bound(self, name):
        job = self.root / 'dispatches' / name
        run_path = self.root / 'runs' / name / 'state.json'
        run_path.parent.mkdir(parents=True)
        state = {'schema_version': RUN_SCHEMA_VERSION, 'status': 'running', 'worktree': str(self.root),
                 'expected_head': 'head', 'worktree_binding': {'fixture': True},
                 'tasks': [{'id': 'cleanup', 'allowed_paths': ['legacy/**']}],
                 'dispatches': [{'id': name, 'role': 'worker', 'kind': 'implement', 'status': 'running', 'task_id': 'cleanup',
                                 'invoked': True, 'command_directory': str(job / 'commands')}]}
        write_json(run_path, state)
        meta = create_manifest(self.config, self.root, job, run_path)
        return job, run_path, state, Broker(meta['artifact'], meta['artifact_digest'])

    def refused_without_a_file(self, job, broker, name, arguments):
        before = tree(job)
        response = handle({'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}, broker)
        payload = json.loads(response['content'][0]['text'])
        self.assertTrue(response['isError'], payload)
        self.assertEqual((payload.get('status'), payload.get('started')), ('blocked', False), payload)
        self.assertEqual(tree(job), before)
        return payload

    def test_manifest_creates_a_private_empty_lock_and_refuses_an_existing_one(self):
        job, _ = self.unbound('fresh')
        lock = os.lstat(job / ADMISSION_LOCK)
        self.assertTrue(stat.S_ISREG(lock.st_mode))
        self.assertEqual((stat.S_IMODE(lock.st_mode), lock.st_size), (0o600, 0))
        stale = self.root / 'dispatches' / 'stale'
        stale.mkdir(parents=True)
        (stale / ADMISSION_LOCK).write_text('')
        with self.assertRaisesRegex(HarnessError, 'admission lock already exists'):
            create_manifest(self.config, self.root, stale, None)
        self.assertFalse((stale / 'commands.json').exists())

    def test_every_admission_refusal_on_a_first_call_leaves_the_dispatch_files_unchanged(self):
        def version(job, run_path, state, broker):
            write_json(run_path, {**state, 'schema_version': 18})
        def manifest(job, run_path, state, broker):
            write_json(broker.path, {**broker.manifest, 'timeout': 1})
        def source(job, run_path, state, broker):
            write_json(run_path, {**state, 'dispatches': [{**state['dispatches'][0], 'status': 'exited'}]})
        def evidence(job, run_path, state, broker):
            folder = job / 'commands' / ('a' * 32)
            folder.mkdir()
            write_json(folder / 'process.json', {'id': job.name + ':command:' + folder.name, 'command_id': command_id(self.COMMAND),
                                                 'pgid': None, 'process_state': 'exited', 'exit_confirmed': True})
        cases = (('ledger version', version, 'head', '18'), ('manifest changed', manifest, 'head', 'digest mismatch'),
                 ('source not live', source, 'head', 'no longer live'), ('HEAD changed', None, 'moved', 'HEAD changed'),
                 ('evidence incomplete', evidence, 'head', 'evidence incomplete'))
        with patch('harness.command_server.git.check_worktree_binding'):
            for label, tamper, head, reason in cases:
                for name, arguments in self.CALLS:
                    with self.subTest(refusal=label, tool=name):
                        with patch('harness.command_server.git.head', return_value='head'):
                            job, run_path, state, broker = self.bound(f'{name}-{label.replace(" ", "-")}')
                        self.assertIn(ADMISSION_LOCK, tree(job))
                        if tamper:
                            tamper(job, run_path, state, broker)
                        with patch('harness.command_server.git.head', return_value=head):
                            payload = self.refused_without_a_file(job, broker, name, arguments)
                        self.assertIn(reason, payload['error'])

    def test_a_missing_or_unusable_lock_is_a_tool_error_that_creates_nothing(self):
        target = self.root / 'through-link'
        def missing(lock):
            lock.unlink()
        def link(lock):
            lock.unlink()
            lock.symlink_to(target)
        def directory(lock):
            lock.unlink()
            lock.mkdir()
        def fifo(lock):
            lock.unlink()
            os.mkfifo(lock)
        for label, replace in (('missing', missing), ('link', link), ('directory', directory), ('fifo', fifo)):
            for name, arguments in self.CALLS:
                with self.subTest(lock=label, tool=name):
                    job, broker = self.unbound(f'{name}-{label}')
                    replace(job / ADMISSION_LOCK)
                    # A FIFO without a writer must not block the open; the watchdog ends a regression instead of hanging.
                    watchdog = FifoWatchdog(job / ADMISSION_LOCK, bound=10) if label == 'fifo' else None
                    try:
                        self.refused_without_a_file(job, broker, name, arguments)
                    finally:
                        self.assertFalse(watchdog and watchdog.stop())
                    self.assertFalse(os.path.lexists(target))
                    self.assertEqual(command_processes(job, state=self.root), [])

    def test_a_deletion_refused_after_admission_still_leaves_its_journal_and_receipt(self):
        job, broker = self.unbound('admitted')
        result = broker.delete_path({'path': 'elsewhere/file.txt'})
        self.assertEqual(result['outcome'], 'refused')
        folder = job / 'commands' / result['call_id'].rsplit(':', 1)[1]
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ['process.json', 'receipt.json'])
        self.assertEqual(read_json(folder / 'receipt.json'), result)
