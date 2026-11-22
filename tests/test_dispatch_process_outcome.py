"""Local process lifecycle facts survive dispatch bookkeeping; no model calls."""
import errno
import os
import signal
import subprocess
import sys
from unittest.mock import patch

from harness.adapters import CLIBackend
from harness.util import ProcessTimeout, read_json
from test_harness import RepoCase, ROOT


class DispatchProcessOutcomeTests(RepoCase):
    def backend(self):
        command = [sys.executable, '-c', 'import time; time.sleep(30)']
        backend = CLIBackend(self.config, self.repo / '.harness')
        backend.command = lambda *args: command
        return backend, command

    def test_startup_signal_before_on_start_preserves_real_exited_process(self):
        backend, command = self.backend()
        engine = self.engine(backend)
        original = subprocess.Popen
        children = []
        def interrupt(argv, *args, **kwargs):
            child = original(argv, *args, **kwargs)
            if argv == command:
                children.append(child)
                os.kill(os.getpid(), signal.SIGINT)
            return child
        try:
            with patch('harness.util.subprocess.Popen', side_effect=interrupt), \
                    patch('harness.dispatch.group_alive', side_effect=AssertionError('must use outcome')), \
                    self.assertRaises(KeyboardInterrupt):
                engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
            entry = read_json(engine.directory / 'state.json')['dispatches'][-1]
            self.assertTrue(entry['started'])
            self.assertEqual(entry['pgid'], children[0].pid)
            self.assertTrue(entry['exit_confirmed'])
            self.assertEqual(entry['process_state'], 'exited')
            self.assertEqual(entry['termination_reason'], 'cancelled')
            self.assertEqual(entry['exit_code'], children[0].returncode)
            engine.context.ensure_exited()
        finally:
            for child in children:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()

    def test_cleanup_observation_failure_does_not_replace_timeout(self):
        backend, _ = self.backend()
        self.config.data['runtime']['agent_timeout_seconds'] = .1
        engine = self.engine(backend)
        original = os.killpg
        def denied(pgid, sig):
            if sig == 0:
                raise PermissionError(errno.EPERM, 'group observation denied')
            return original(pgid, sig)
        with patch('harness.process_runtime.os.killpg', side_effect=denied), \
                patch('harness.dispatch.group_alive', side_effect=PermissionError('secondary probe')), \
                self.assertRaises(ProcessTimeout) as caught:
            engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
        entry = read_json(engine.directory / 'state.json')['dispatches'][-1]
        self.assertEqual(entry['termination_reason'], 'timeout')
        self.assertEqual(entry['exit_code'], -signal.SIGTERM)
        self.assertFalse(entry['exit_confirmed'])
        self.assertFalse(entry['evidence_complete'])
        self.assertEqual(entry['process_outcome']['cleanup_errors'][0]['errno'], errno.EPERM)
        self.assertEqual(caught.exception.outcome.termination_reason, 'timeout')

    def test_backend_without_outcome_keeps_timeout_when_fallback_probe_fails(self):
        class Backend:
            def invoke(self, **kwargs):
                kwargs['on_start'](123456)
                raise ProcessTimeout(.1, kwargs['job'] / 'process')
        engine = self.engine(Backend())
        from copy import deepcopy
        from harness.run_store import RunStore
        commits, original = [], RunStore.commit

        def record(store, **kwargs):
            revision = original(store, **kwargs)
            commits.append(deepcopy(store.run))
            return revision
        # The fallback probe fails; the judgment's own exit check is a separate, later observation.
        with patch('harness.dispatch.group_alive', side_effect=PermissionError(errno.EPERM, 'probe')), \
                patch('harness.run_context.group_alive', return_value=False), \
                patch.object(RunStore, 'commit', record), self.assertRaises(ProcessTimeout):
            engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'))
        recorded = next(run['dispatches'][-1] for run in commits
                        if run['dispatches'] and run['dispatches'][-1].get('status') == 'failed')
        self.assertFalse(recorded['exit_confirmed'])  # the failed probe manufactured no confirmation
        self.assertEqual(recorded['process_observation_error']['errno'], errno.EPERM)
        entry = read_json(engine.directory / 'state.json')['dispatches'][-1]
        self.assertTrue(entry['exit_confirmed'])  # confirmed only by the judgment's own observation
        self.assertEqual(entry['process_observation_error']['errno'], errno.EPERM)
        self.assertEqual(entry['worker_receipt_consumed'], 'provider_failure')
