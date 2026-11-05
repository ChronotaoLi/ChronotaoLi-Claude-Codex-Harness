"""Behavioral input gates across real run lifecycle with offline agents."""
import hashlib
from unittest.mock import patch
from harness import git
from harness.util import HarnessError, read_json, write_json
from harness.workspace_inputs import prepare_run
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend


class InputDispatchIntegrationTests(RepoCase):
    def prepared(self):
        (self.repo / '.gitignore').write_text('__pycache__/\nvendor/\ndeps/\n')
        git.git(self.repo, 'add', '.gitignore')
        git.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                '-c', 'commit.gpgsign=false', 'commit', '-m', 'ignored dependencies')
        backend = ScriptedBackend()
        engine = self.engine(backend)
        engine.start('Implement greet', read_json(ROOT / 'examples/plan.json'), pause_after_plan=True)
        source = self.repo / 'deps/input.txt'
        source.parent.mkdir()
        source.write_text('immutable dependency')
        manifest = dict(schema_version=1, run_id=engine.run['id'], head=engine.run['expected_head'],
                        source_root='.', max_files=1, max_bytes=100,
                        files=[dict(source='deps/input.txt', destination='vendor/input.txt',
                                    sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                                    mode=0o644, type='file', ownership='input_copy')])
        prepare_run(self.repo, engine.run['id'], manifest)
        return engine, backend

    def test_pre_agent_copy_mutation_blocks_before_worker_charge_and_invocation(self):
        engine, backend = self.prepared()
        resumed = self.engine(backend)
        original = resumed.context.emit_hook
        def mutate(event, entry, job):
            original(event, entry, job)
            if event == 'pre_agent' and entry['role'] == 'worker':
                (resumed.worktree / 'vendor/input.txt').write_text('modified by hook')
        with patch.object(resumed.context, 'emit_hook', side_effect=mutate):
            with self.assertRaises(HarnessError):
                resumed.resume(engine.run['id'], approve_plan=True)
        state = read_json(engine.directory / 'state.json')
        self.assertEqual(state['total_attempts'], 0)
        self.assertEqual(state['agent_dispatches'], 0)
        self.assertFalse(any(call['role'] == 'worker' for call in backend.calls))
        self.assertFalse(state['dispatches'][-1]['invoked'])
        self.assertEqual(state['accepted'], {})

    def test_schema12_missing_input_binding_refuses_resume_without_calls(self):
        engine, backend = self.prepared()
        state = read_json(engine.directory / 'state.json')
        self.assertEqual(state['schema_version'], 13)
        del state['workspace_inputs']
        write_json(engine.directory / 'state.json', state)
        before = (engine.directory / 'state.json').read_bytes()
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'], approve_plan=True)
        self.assertEqual((engine.directory / 'state.json').read_bytes(), before)
        self.assertEqual(backend.calls, [])
