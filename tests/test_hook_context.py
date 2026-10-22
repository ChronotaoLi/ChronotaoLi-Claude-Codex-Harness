import json
from pathlib import Path
import shutil
import sys
import tempfile

from harness.shared import HookError, Hooks
from harness.util import read_json, write_json
from test_harness import RepoCase
from test_runtime_changes import ScriptedBackend


HOOK_SCRIPT = """
import json
from pathlib import Path
import sys

event = json.load(sys.stdin)
run_dir = Path(event['run_dir'])
state = json.loads((run_dir / 'state.json').read_text())
assert event['run_id'] == state['id'] == run_dir.name
assert event['repo'] == state['repo'] == sys.argv[3]
assert Path(event['worktree']).is_absolute()
assert (Path(event['worktree']) / 'test_hello.py').is_file()
if event['event'] in ('pre_accept', 'post_accept'):
    assert 'def greet(name)' in (Path(event['worktree']) / 'hello.py').read_text()
if 'source_worktree' in event:
    assert event['source_worktree'] == state['worktree']
    assert event['worktree'] != event['source_worktree']
    assert not Path(event['worktree']).is_relative_to(Path(event['repo']))
with Path(sys.argv[1]).open('a') as output:
    output.write(json.dumps({'payload': event, 'cwd': str(Path.cwd())}) + '\\n')
print(event['event'], event['run_id'], event['worktree'])
block = Path(sys.argv[2])
if block.exists() and all(event.get(key) == value for key, value in json.loads(block.read_text()).items()):
    raise SystemExit(7)
"""


class HookContextTests(RepoCase):
    def setUp(self):
        super().setUp()
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        root = Path(external.name)
        self.events_path = root / 'events.jsonl'
        self.block = root / 'block.json'
        self.hooks_path = root / 'hooks.json'
        self.config.data['shared']['hooks'] = str(self.hooks_path)
        self.configure_hooks()
        self.engines = []
        self.addCleanup(self.cleanup_checkouts)

    def configure_hooks(self, *, legacy_context=False):
        events = Hooks.EVENTS - {'context_compacted' if not legacy_context else 'context_prepared'}
        argv = [sys.executable, '-c', HOOK_SCRIPT, str(self.events_path), str(self.block),
                str(self.repo.resolve())]
        write_json(self.hooks_path, {event: [{'argv': argv}] for event in events})

    def engine(self, backend=None):
        engine = super().engine(backend)
        self.engines.append(engine)
        return engine

    def cleanup_checkouts(self):
        for engine in self.engines:
            if engine.run is None:
                continue
            for artifact in engine.directory.glob('verification/final/*/verification-*/checkout.json'):
                root = Path(read_json(artifact)['worktree']).parents[1]
                if root.name.startswith('harness-verify-') and not root.is_relative_to(self.repo.resolve()):
                    shutil.rmtree(root, ignore_errors=True)

    def events(self, engine):
        records = [json.loads(line) for line in self.events_path.read_text().splitlines()]
        records = [record for record in records if record['payload']['run_id'] == engine.run['id']]
        self.assertTrue(records)
        for record in records:
            payload = record['payload']
            self.assertEqual(record['cwd'], str(self.config.root))
            self.assertEqual(payload['repo'], str(engine.repo))
            self.assertEqual(payload['run_dir'], str(engine.directory))
            if 'source_worktree' not in payload:
                self.assertEqual(payload['worktree'], str(engine.worktree))
        return [record['payload'] for record in records]

    def test_all_lifecycle_hooks_locate_run_across_tasks_recovery_and_fresh_verification(self):
        self.configure_hooks(legacy_context=True)
        self.config.data['verification']['fresh_checkout'] = True
        write_json(self.block, {'event': 'pre_accept', 'task_id': 'greet'})

        def two_tasks(backend, kwargs, packet, result):
            if kwargs['kind'] == 'plan':
                result['tasks'].append({'id': 'document', 'title': 'Document greet',
                    'description': 'Document greet in usage.txt', 'depends_on': ['greet'],
                    'allowed_paths': ['usage.txt'], 'acceptance': ['usage.txt explains greet'], 'skills': []})
            if kwargs['role'] == 'worker' and kwargs['task']['id'] == 'document':
                (kwargs['cwd'] / 'usage.txt').write_text('Use greet(name).')

        backend = ScriptedBackend(two_tasks)
        engine = self.engine(backend)
        with self.assertRaises(HookError):
            engine.start('Implement and document greet')
        failed = engine.directory / 'hooks/greet-1/pre_accept-1/hook-pre_accept-0.stdout'
        failed_log = failed.read_bytes()
        self.assertEqual([event['event'] for event in self.events(engine)][-1], 'on_error')
        self.block.unlink()
        resumed = self.engine(backend)
        result = resumed.resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(set(result['accepted']), {'greet', 'document'})
        self.assertEqual(sum(call['role'] == 'worker' for call in backend.calls), 2)
        self.assertEqual(failed.read_bytes(), failed_log)
        self.assertTrue((engine.directory / 'hooks/greet-1/pre_accept-2/hook-pre_accept-0.stdout').is_file())
        events = self.events(resumed)
        self.assertEqual({event['event'] for event in events}, Hooks.EVENTS - {'context_prepared'})
        self.assertEqual(sum(event['event'] == 'context_compacted' for event in events), len(backend.calls))
        self.assertEqual([event['task_id'] for event in events if event['event'] == 'pre_accept'],
                         ['greet', 'greet', 'document'])
        self.assertEqual({event['role'] for event in events if event['event'] == 'post_agent'},
                         {'commander', 'worker', 'reviewer'})
        checkout = read_json(next(engine.directory.glob('verification/final/*/verification-*/checkout.json')))
        final_hooks = [event for event in events if event['event'] in ('pre_verify', 'post_verify')
                       and event['task_id'] == 'final']
        self.assertEqual(len(final_hooks), 2)
        for event in final_hooks:
            self.assertEqual(event['worktree'], checkout['worktree'])
            self.assertEqual(event['source_worktree'], str(engine.worktree))

    def test_recovered_commander_and_reviewer_receipts_keep_hook_paths_and_separate_logs(self):
        for role in ('commander', 'reviewer'):
            with self.subTest(role=role):
                write_json(self.block, {'event': 'post_agent', 'role': role})
                backend = ScriptedBackend()
                engine = self.engine(backend)
                with self.assertRaises(HookError):
                    engine.start('Implement greet')
                failed_entry = next(entry for entry in engine.run['dispatches'] if entry['role'] == role)
                job = engine.directory / 'dispatches' / failed_entry['id']
                first_log = job / 'post-agent-1/hook-post_agent-0.stdout'
                before = first_log.read_bytes()
                self.block.unlink()
                resumed = self.engine(backend)
                self.assertEqual(resumed.resume(engine.run['id'])['status'], 'completed')
                self.assertEqual(first_log.read_bytes(), before)
                self.assertTrue((job / 'post-agent-2/hook-post_agent-0.stdout').is_file())
                events = self.events(resumed)
                retries = [event for event in events if event['event'] == 'post_agent'
                           and event['id'] == failed_entry['id']]
                self.assertEqual(len(retries), 2)
                self.assertEqual(sum(call['role'] == role for call in backend.calls),
                                 1 if role == 'commander' else 2)
