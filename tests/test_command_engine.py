from pathlib import Path

from harness.adapters import DemoBackend
from harness.util import HarnessError, read_json, write_json
from test_harness import RepoCase, ROOT


class CommandEngineBackend(DemoBackend):
    def __init__(self, testcase, inject_unknown=False):
        self.testcase = testcase
        self.inject_unknown = inject_unknown
        self.workers = []

    def invoke(self, **kwargs):
        if kwargs['role'] == 'worker':
            state = read_json(kwargs['run_path'])
            entry = state['dispatches'][-1]
            directory = kwargs['job'] / 'commands'
            self.testcase.assertEqual(entry['role'], 'worker')
            self.testcase.assertEqual(entry['id'], kwargs['job'].name)
            self.testcase.assertEqual(entry['command_directory'], str(directory))
            self.testcase.assertTrue(directory.is_dir())
            self.testcase.assertTrue(entry['invoked'])
            self.workers.append(entry['id'])
            if self.inject_unknown:
                folder = directory / ('a' * 32)
                folder.mkdir()
                write_json(folder / 'process.json', {
                    'id': entry['id'] + ':command:' + folder.name,
                    'command_id': 'fixture', 'pgid': None,
                    'process_state': 'starting', 'exit_confirmed': False})
        return super().invoke(**kwargs)


class CommandEngineTests(RepoCase):
    def plan(self):
        return read_json(ROOT / 'examples' / 'plan.json')

    def test_worker_command_directory_is_persisted_before_invoke(self):
        backend = CommandEngineBackend(self)
        result = self.engine(backend).start('Implement greet', self.plan())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(backend.workers), 1)
        worker = next(d for d in result['dispatches'] if d['role'] == 'worker')
        self.assertTrue(Path(worker['command_directory']).is_dir())
        self.assertTrue(worker['exit_confirmed'])

    def test_unknown_child_blocks_completed_worker_and_resume_despite_parent_exit(self):
        backend = CommandEngineBackend(self, inject_unknown=True)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', self.plan())
        saved = read_json(engine.directory / 'state.json')
        worker = next(d for d in saved['dispatches'] if d['role'] == 'worker')
        self.assertTrue(worker['exit_confirmed'])
        self.assertEqual(saved['accepted'], {})
        before = len(saved['dispatches'])
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(saved['id'])
        after = read_json(engine.directory / 'state.json')
        self.assertEqual(len(after['dispatches']), before)
        self.assertEqual(len(backend.workers), 1)
        self.assertEqual(after['accepted'], {})
        self.assertFalse(any(d['role'] in ('verification', 'reviewer') for d in after['dispatches']))

    def test_missing_registered_command_directory_blocks_resume(self):
        backend = CommandEngineBackend(self, inject_unknown=True)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', self.plan())
        saved = read_json(engine.directory / 'state.json')
        worker = next(d for d in saved['dispatches'] if d['role'] == 'worker')
        directory = Path(worker['command_directory'])
        child = directory / ('a' * 32)
        (child / 'process.json').unlink()
        child.rmdir()
        directory.rmdir()
        before = len(saved['dispatches'])
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(saved['id'])
        after = read_json(engine.directory / 'state.json')
        self.assertEqual(len(after['dispatches']), before)
        self.assertEqual(len(backend.workers), 1)
        self.assertEqual(after['accepted'], {})
