import errno
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import command_server, state_files
from harness.command_server import Broker, command_id, command_processes, create_manifest, handle
from harness.util import HarnessError, read_json, with_descriptors, write_json
from harness.versions import RUN_SCHEMA_VERSION
from test_provider_failures import FifoWatchdog
from test_state_files import FakeOs, failing, held_lock, open_descriptors

ADMISSION_LOCK = 'command-admission.lock'


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
        ready, release = self.root / 'ready', self.root / 'release'
        command = {'cwd': '.', 'argv': [sys.executable, '-c', """
from pathlib import Path
import time

ready, release = Path('ready'), Path('release')
ready.touch()
deadline = time.monotonic() + 30
while not release.exists():
    if not ready.exists() or time.monotonic() >= deadline:
        raise SystemExit('command was not released')
    time.sleep(.01)
print('done')
"""]}
        config = {'verification': {'commands': [command]}, 'permissions': {'worker_commands': []},
                  'runtime': {'verify_timeout_seconds': 35, 'agent_timeout_seconds': 45}}
        job = self.root / 'overlap-worker'
        meta = create_manifest(config, self.root, job)
        entry = Path(__file__).resolve().parents[1] / 'harness' / 'command_entry.py'
        argv = [sys.executable, str(entry), '--manifest', str(meta['artifact']), '--sha256', meta['artifact_digest']]
        request = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                              'params': {'name': 'run_command', 'arguments': {'command_id': command_id(command)}}}) + '\n'

        def reap(caller):
            try:
                caller.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                if caller.poll() is None:
                    caller.kill()
                caller.communicate(timeout=5)

        def start_call():
            caller = subprocess.Popen(argv, cwd=self.root, text=True, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(reap, caller)
            caller.stdin.write(request)
            caller.stdin.flush()
            return caller

        def finish_call(caller):
            stdout, stderr = caller.communicate(timeout=10)
            self.assertEqual(caller.returncode, 0, stderr)
            response = json.loads(stdout)['result']
            return response['isError'], json.loads(response['content'][0]['text'])

        try:
            first = start_call()
            deadline = time.monotonic() + 10
            # The command signals readiness, and its supervisor must have committed the start.
            while first.poll() is None and time.monotonic() < deadline:
                if ready.exists():
                    journal_path = next((job / 'commands').glob('*/process.json'), None)
                    if journal_path and read_json(journal_path)['process_state'] == 'running':
                        break
                time.sleep(.01)
            else:
                self.fail('first command did not signal readiness with a running journal')
            running = read_json(journal_path)
            second = start_call()
            self.assertNotEqual(first.pid, second.pid)
            refused, result = finish_call(second)
            self.assertTrue(refused, result)
            self.assertEqual(result, {'status': 'blocked', 'started': False,
                                      'error': 'Cannot confirm command process exited: ' + running['id']})
            self.assertIsNone(first.poll())
            self.assertEqual(list((job / 'commands').iterdir()), [journal_path.parent])
            self.assertEqual(read_json(journal_path), running)

            release.touch()
            refused, result = finish_call(first)
            self.assertFalse(refused, result)
            self.assertEqual(result['call_id'], running['id'])
            self.assertEqual(result['exit_code'], 0, result)
            self.assertEqual(result['termination_reason'], 'normal', result)
            self.assertTrue(result['lifecycle']['started'], result)
            self.assertEqual(result['stdout'], 'done\n')
            self.assertTrue(result['exit_confirmed'], result)
            self.assertTrue(result['evidence_complete'], result)
            self.assertEqual(read_json(journal_path.with_name('receipt.json')), result)
        finally:
            # Release the supervised command before the registered caller cleanups.
            release.touch()

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

    def test_manifest_lock_creation_preserves_signal_handlers_and_closes_the_descriptor(self):
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, ())
        for sig in (signal.SIGINT, signal.SIGHUP):
            for behavior in ('ignore', 'record', 'interrupt'):
                with self.subTest(signal=sig.name, handler=behavior):
                    job = self.root / f'{sig.name}-{behavior}'
                    opened_with = []
                    received = []

                    def receive(signum, _frame):
                        received.append(signum)
                        if behavior == 'interrupt':
                            raise KeyboardInterrupt

                    def after_open(real, path, flags, *args, **kwargs):
                        fd = real(path, flags, *args, **kwargs)
                        if path == job / ADMISSION_LOCK and flags & os.O_EXCL:
                            opened_with.append(signal.getsignal(sig))
                            signal.raise_signal(sig)
                        return fd

                    handler = signal.SIG_IGN if behavior == 'ignore' else receive
                    previous = signal.signal(sig, handler)
                    before = open_descriptors()
                    result = None
                    try:
                        with patch.object(command_server, 'os', FakeOs(open=after_open)):
                            try:
                                result = create_manifest(self.config, self.root, job)
                            except KeyboardInterrupt:
                                if behavior != 'interrupt':
                                    self.fail('The caller handler allows manifest creation to continue')
                        self.assertIs(signal.getsignal(sig), handler)
                        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, ()), mask)
                    finally:
                        signal.signal(sig, previous)

                    self.assertEqual(open_descriptors(), before)
                    self.assertEqual(opened_with, [handler])
                    self.assertEqual(received, [] if behavior == 'ignore' else [sig])
                    self.assertEqual((job / ADMISSION_LOCK).stat().st_size, 0)
                    if behavior == 'interrupt':
                        self.assertIsNone(result)
                        self.assertFalse((job / 'commands.json').exists())
                    else:
                        broker = Broker(result['artifact'], result['artifact_digest'])
                        self.assertEqual(broker.list_commands()[0]['command'], self.COMMAND)

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
                        if tamper:
                            tamper(job, run_path, state, broker)
                        with patch('harness.command_server.git.head', return_value=head):
                            payload = self.refused_without_a_file(job, broker, name, arguments)
                        self.assertIn(reason, payload['error'])

    def test_a_missing_or_unusable_lock_is_a_tool_error_that_creates_nothing(self):
        dangling = self.root / 'dangling'
        target = self.root / 'target'
        target.write_bytes(b'untouched')
        def missing(lock):
            lock.unlink()
        def link(lock, destination):
            lock.unlink()
            lock.symlink_to(destination)
        def directory(lock):
            lock.unlink()
            lock.mkdir()
        def fifo(lock):
            lock.unlink()
            os.mkfifo(lock)
        cases = (('missing', missing), ('link', lambda lock: link(lock, target)),
                 ('dangling', lambda lock: link(lock, dangling)), ('directory', directory), ('fifo', fifo))
        for label, replace in cases:
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
                    self.assertFalse(os.path.lexists(dangling))
                    self.assertEqual(target.read_bytes(), b'untouched')
                    self.assertEqual(command_processes(job, state=self.root), [])

    def test_a_busy_lock_and_a_failed_lock_open_refuse_both_tools(self):
        job, broker = self.unbound('unavailable')
        with held_lock(job / ADMISSION_LOCK):
            for name, arguments in self.CALLS:
                with self.subTest(tool=name, failure='busy'):
                    self.refused_without_a_file(job, broker, name, arguments)
        for name, arguments in self.CALLS:
            with self.subTest(tool=name, failure='EIO'):
                reads = FakeOs(open=failing(errno.EIO, ADMISSION_LOCK))
                with patch.object(state_files, 'os', reads):
                    self.refused_without_a_file(job, broker, name, arguments)
                self.assertEqual(reads.count('open', ADMISSION_LOCK), 1)

    def test_interrupt_at_lock_open_releases_the_lock_and_descriptors(self):
        for name, arguments in self.CALLS:
            with self.subTest(tool=name):
                job, broker = self.unbound(name)
                before = tree(job), open_descriptors()
                acquired = []

                def interrupt(real, leaf, *args, **kwargs):
                    fd = real(leaf, *args, **kwargs)
                    if leaf == ADMISSION_LOCK:
                        acquired.append(fd)
                        signal.raise_signal(signal.SIGINT)
                    return fd

                with patch.object(state_files, 'os', FakeOs(open=interrupt)), self.assertRaises(KeyboardInterrupt):
                    handle({'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}, broker)
                self.assertEqual(len(acquired), 1)
                self.assertEqual((tree(job), open_descriptors()), before)
                with held_lock(job / ADMISSION_LOCK):
                    pass

    def test_manifest_and_live_ledger_refuse_unreadable_evidence(self):
        with (patch('harness.command_server.git.check_worktree_binding'),
              patch('harness.command_server.git.head', return_value='head')):
            job, run_path, _, broker = self.bound('authority')
            for path in (broker.path, run_path):
                with self.subTest(artifact=path.name):
                    reads = FakeOs(open=failing(errno.EIO, path.name))
                    with patch.object(state_files, 'os', reads):
                        for name, arguments in self.CALLS:
                            self.refused_without_a_file(job, broker, name, arguments)
                    self.assertEqual(reads.count('open', path.name), len(self.CALLS))

                    raw = path.read_bytes()
                    target = self.root / ('copy-' + path.name)
                    path.replace(target)
                    path.symlink_to(target)
                    for name, arguments in self.CALLS:
                        self.refused_without_a_file(job, broker, name, arguments)
                    path.unlink()
                    os.mkfifo(path)
                    watchdog = FifoWatchdog(path, bound=3)
                    try:
                        for name, arguments in self.CALLS:
                            self.refused_without_a_file(job, broker, name, arguments)
                    finally:
                        self.assertFalse(watchdog.stop())
                    path.unlink()
                    target.replace(path)
                    self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(broker.list_commands()[0]['command'], self.COMMAND)

    def test_delete_scope_rechecks_the_ledger_and_blocks_on_read_failure(self):
        with (patch('harness.command_server.git.check_worktree_binding'),
              patch('harness.command_server.git.head', return_value='head')):
            job, run_path, _, broker = self.bound('scope')
            opened = []

            def fail_second(real, leaf, *args, **kwargs):
                if leaf == run_path.name:
                    opened.append(leaf)
                    if len(opened) == 2:
                        raise OSError(errno.EIO, os.strerror(errno.EIO))
                return real(leaf, *args, **kwargs)

            with patch.object(state_files, 'os', FakeOs(open=fail_second)):
                self.refused_without_a_file(job, broker, *self.CALLS[1])
            self.assertEqual(opened, [run_path.name, run_path.name])

    def test_worktree_ancestor_lookup_failure_refuses_both_tools(self):
        job, broker = self.unbound('worktree-ancestor')
        original = Path.lstat
        lookups = []

        def fail_ancestor(path, *args, **kwargs):
            if path == self.root.parent:
                lookups.append(path)
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            return original(path, *args, **kwargs)

        with patch.object(Path, 'lstat', fail_ancestor):
            for name, arguments in self.CALLS:
                self.refused_without_a_file(job, broker, name, arguments)
        self.assertEqual(lookups, [self.root.parent] * len(self.CALLS))

    def test_a_deletion_refused_after_admission_still_leaves_its_journal_and_receipt(self):
        job, broker = self.unbound('admitted')
        result = broker.delete_path({'path': 'elsewhere/file.txt'})
        self.assertEqual(result['outcome'], 'refused')
        folder = job / 'commands' / result['call_id'].rsplit(':', 1)[1]
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ['process.json', 'receipt.json'])
        self.assertEqual(read_json(folder / 'receipt.json'), result)
