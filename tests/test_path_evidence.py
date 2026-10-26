"""Large changes remain retrievable through dispatch, rejection and recovery gates."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness import git
from harness.path_evidence import project_paths, validate_indexes
from harness.util import HarnessError, dump, read_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend, finding


def resolved(packet, value):
    if isinstance(value, list):
        return value
    ref = packet["path_evidence_indexes"][value["path_index"]]
    raw = Path(ref["artifact"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref["artifact_digest"]:
        raise AssertionError("retrieved evidence digest differs")
    result = read_json(Path(ref["artifact"]))["paths"]
    if len(result) != ref["count"]:
        raise AssertionError("retrieved evidence count differs")
    return result


class PathEvidenceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()

    def paths(self):
        return [f'docs/{i:04}-' + 'x' * 110 + '.md' for i in range(2650)]

    def test_large_current_and_recovery_lists_share_lossless_retrievable_evidence(self):
        paths = self.paths() + ['docs/中文.md', 'docs/a\nb.md', 'docs/quote".md', 'docs/byte-\udcff.md']
        original = {"changed_files": paths, "recovery": {"dispatch_id": "old", "changed_files": paths},
                    "task": {"allowed_paths": ["docs/**"], "acceptance": ["Every change is in scope"]}}
        before = deepcopy(original)
        self.assertGreater(len(dump(original)), 600000)
        projected = project_paths(original, self.directory)
        self.assertEqual(original, before)
        self.assertEqual(projected["task"], original["task"])
        self.assertEqual(projected["recovery"]["dispatch_id"], "old")
        self.assertEqual(len(projected["path_evidence_indexes"]), 1)
        self.assertEqual(resolved(projected, projected["changed_files"]), paths)
        self.assertEqual(resolved(projected, projected["recovery"]["changed_files"]), paths)
        self.assertLess(len(dump(projected)), 2000)
        self.assertEqual(project_paths(projected, self.directory), projected)

    def test_changed_catalog_keeps_previous_index_and_instruction_trust(self):
        paths = self.paths()
        first = project_paths({"changed_files": paths}, self.directory)
        artifact = Path(next(iter(first["path_evidence_indexes"].values()))["artifact"])
        old = artifact.read_bytes()
        instruction_paths = [p + '/AGENTS.md' for p in paths]
        packet = {"changed_files": paths + ["new.md"],
                  "instruction_file_changes": {"paths": instruction_paths, "trust": "untrusted_candidate_instructions"}}
        second = project_paths(packet, self.directory)
        self.assertEqual(artifact.read_bytes(), old)
        self.assertEqual(resolved(second, second["changed_files"]), paths + ["new.md"])
        self.assertEqual(resolved(second, second["instruction_file_changes"]["paths"]), instruction_paths)
        self.assertEqual(second["instruction_file_changes"]["trust"], "untrusted_candidate_instructions")

    def test_small_lists_stay_inline_without_creating_indexes(self):
        packet = {"changed_files": ["hello.py"], "recovery": {"changed_files": ["hello.py"]}}
        self.assertEqual(project_paths(packet, self.directory), packet)
        self.assertFalse((self.directory / 'path-indexes').exists())

    def test_existing_corrupt_index_is_never_overwritten(self):
        packet = {"changed_files": self.paths()}
        projected = project_paths(packet, self.directory)
        ref = next(iter(projected["path_evidence_indexes"].values()))
        artifact = Path(ref['artifact'])
        artifact.write_bytes(b'{}')
        with self.assertRaisesRegex(HarnessError, 'index changed') as raised:
            project_paths(packet, self.directory)
        self.assertIn(ascii(str(artifact)), str(raised.exception))
        self.assertEqual(artifact.read_bytes(), b'{}')
        with self.assertRaises(HarnessError):
            validate_indexes(self.directory, [ref])

    def test_integrity_failures_identify_safe_paths_and_preserve_retrievable_evidence(self):
        directory = self.directory / 'run\nwith\x1b-control-\u202e'
        packet = {"changed_files": self.paths()}
        projected = project_paths(packet, directory)
        ref = next(iter(projected["path_evidence_indexes"].values()))
        artifact = Path(ref['artifact'])
        original = artifact.read_bytes()
        for mutation in ('missing', 'changed', 'symlink', 'reference', 'count'):
            with self.subTest(mutation=mutation):
                changed_ref = dict(ref)
                target = directory / 'preserved'
                if mutation == 'missing':
                    artifact.unlink()
                elif mutation == 'changed':
                    artifact.write_bytes(b'{}')
                elif mutation == 'symlink':
                    target.write_bytes(original)
                    artifact.unlink()
                    artifact.symlink_to(target)
                elif mutation == 'reference':
                    changed_ref['artifact'] = 'wrong\nreference\x1b\u202e'
                else:
                    changed_ref['count'] += 1
                with self.assertRaises(HarnessError) as raised:
                    validate_indexes(directory, [changed_ref])
                message = str(raised.exception)
                self.assertIn(ascii(str(artifact)), message)
                self.assertFalse(any(ord(c) < 32 or ord(c) > 126 for c in message))
                if mutation in ('reference', 'symlink'):
                    self.assertIn('expected ' + ascii(str(artifact)), message)
                    self.assertIn('actual ' + ascii(changed_ref['artifact']), message)
                if mutation == 'missing':
                    self.assertFalse(artifact.exists())
                elif mutation == 'changed':
                    self.assertEqual(artifact.read_bytes(), b'{}')
                elif mutation == 'symlink':
                    self.assertTrue(artifact.is_symlink())
                    self.assertEqual(target.read_bytes(), original)
                    artifact.unlink()
                else:
                    self.assertEqual(artifact.read_bytes(), original)
                artifact.write_bytes(original)
                validate_indexes(directory, [ref])
                self.assertEqual(resolved(projected, projected['changed_files']), packet['changed_files'])

    def test_symlink_cannot_redirect_index_or_its_parent(self):
        packet = {"changed_files": self.paths()}
        projected = project_paths(packet, self.directory)
        ref = next(iter(projected["path_evidence_indexes"].values()))
        artifact = Path(ref['artifact'])
        original = artifact.read_bytes()
        target = self.directory / 'unrelated'
        target.write_bytes(original)
        artifact.unlink()
        artifact.symlink_to(target)
        with self.assertRaises(HarnessError):
            project_paths(packet, self.directory)
        with self.assertRaises(HarnessError):
            validate_indexes(self.directory, [ref])
        self.assertEqual(target.read_bytes(), original)
        alias = self.directory / 'alias'
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(HarnessError):
            project_paths(packet, alias)


class PathEvidenceLifecycleTests(RepoCase):
    def fixture(self):
        folder = self.repo / 'legacy'
        folder.mkdir()
        self.names = [f'legacy/{i:04}-' + 'x' * 110 + '.md' for i in range(1000)]
        for name in self.names:
            (self.repo / name).write_text('historical document\n')
        git.git(self.repo, 'add', 'legacy')
        git.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                '-c', 'commit.gpgsign=false', 'commit', '-m', 'old documents')
        plan = read_json(ROOT / 'examples' / 'plan.json')
        plan['tasks'][0]['allowed_paths'].append('legacy/**')
        self.assertGreater(len(dump(self.names)), self.config.data['runtime']['context_chars'])
        return plan

    def remove_legacy(self, cwd):
        for name in self.names:
            (cwd / name).unlink(missing_ok=True)

    def test_large_deletion_worker_failure_rejection_rework_and_final_acceptance(self):
        plan = self.fixture()
        sizes, models, diffs = [], [], []

        def exercise(backend, kwargs, packet, result):
            sizes.append(len(kwargs['prompt']))
            models.append((kwargs['role'], kwargs['model']))
            if (packet.get('recovery') or {}).get('changed_files'):
                self.assertEqual(set(resolved(packet, packet['recovery']['changed_files'])),
                                 set(self.names) | {'hello.py'})
                self.assertIsInstance(engine.run['worker_recovery']['greet']['changed_files'], list)
            if kwargs['role'] == 'worker':
                number = sum(c['role'] == 'worker' for c in backend.calls)
                self.remove_legacy(kwargs['cwd'])
                if number == 1:
                    raise HarnessError('simulated provider interruption after edits')
                if number == 3:
                    with (kwargs['cwd'] / 'hello.py').open('a') as stream:
                        stream.write('# independently reviewed rework\n')
            elif kwargs['role'] == 'reviewer':
                if kwargs['kind'] == 'review':
                    self.assertEqual(set(resolved(packet, packet['changed_files'])), set(self.names) | {'hello.py'})
                    self.assertEqual(len(packet['path_evidence_indexes']), 1)
                    diffs.append(Path(packet['diff_artifact']).read_bytes())
                    if len(diffs) == 1:
                        result.update(verdict='request_changes', findings=[finding()])
                        self.assertEqual(engine.run['accepted'], {})
                else:
                    diff = Path(packet['diff_artifact']).read_bytes()
                    self.assertTrue(all(name.encode() in diff for name in self.names))

        backend = ScriptedBackend(exercise)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, 'provider interruption'):
            engine.start('Replace legacy docs and implement greet', plan)
        ident = engine.run['id']
        engine = self.engine(backend)
        result = engine.resume(ident)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts'], {'greet': 3})
        self.assertEqual([r['review']['verdict'] for r in result['review_history']['greet']], ['request_changes', 'approve'])
        self.assertNotEqual(diffs[0], diffs[1])
        self.assertLess(max(sizes), 32000)
        self.assertTrue(all(model == self.config.role(role, 'astra') for role, model in models))
        ids = [d['id'] for d in result['dispatches']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all((self.repo / name).exists() for name in self.names))

    def test_missing_or_changed_index_blocks_resume_without_repeating_work(self):
        plan = self.fixture()
        saved = {}

        def unavailable(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker':
                self.remove_legacy(kwargs['cwd'])
            elif kwargs['kind'] == 'review' and not saved:
                ref = next(iter(packet['path_evidence_indexes'].values()))
                saved.update(path=Path(ref['artifact']), raw=Path(ref['artifact']).read_bytes())
                raise HarnessError('review service unavailable')

        backend = ScriptedBackend(unavailable)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, 'review service'):
            engine.start('Replace legacy docs', plan)
        checks = [d['id'] for d in engine.run['dispatches'] if d['role'] == 'verification']
        calls = len(backend.calls)
        for mutation in ('missing', 'changed'):
            if mutation == 'missing':
                saved['path'].unlink()
            else:
                saved['path'].write_bytes(b'{}')
            with self.assertRaisesRegex(HarnessError, 'path evidence index') as raised:
                self.engine(backend).resume(engine.run['id'])
            self.assertIn(ascii(str(saved['path'])), str(raised.exception))
            self.assertEqual(len(backend.calls), calls)
            saved['path'].write_bytes(saved['raw'])
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts'], {'greet': 1})
        self.assertEqual([d['id'] for d in result['dispatches'] if d['role'] == 'verification' and d['task_id'] == 'greet'], checks)

    def test_index_tamper_after_return_preserves_review_without_redrawing_verdict(self):
        plan = self.fixture()
        saved = {}

        def corrupt(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker':
                self.remove_legacy(kwargs['cwd'])
            elif kwargs['kind'] == 'review':
                ref = next(iter(packet['path_evidence_indexes'].values()))
                saved.update(path=Path(ref['artifact']), raw=Path(ref['artifact']).read_bytes())
                saved['path'].write_bytes(b'{}')

        backend = ScriptedBackend(corrupt)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, 'path evidence index changed'):
            engine.start('Replace legacy docs', plan)
        self.assertEqual(engine.run['accepted'], {})
        self.assertEqual(engine.run['pending_task']['stage'], 'reviewing')
        saved['path'].write_bytes(saved['raw'])
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(sum(c['kind'] == 'review' for c in backend.calls), 1)
        self.assertEqual(result['attempts'], {'greet': 1})

    def test_index_write_failure_resumes_saved_review_stage(self):
        plan = self.fixture()

        def delete(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker':
                self.remove_legacy(kwargs['cwd'])

        backend = ScriptedBackend(delete)
        engine = self.engine(backend)
        with patch('harness.path_evidence.write_json', side_effect=OSError('disk unavailable')):
            with self.assertRaisesRegex(HarnessError, 'path evidence index'):
                engine.start('Replace legacy docs', plan)
        self.assertEqual(engine.run['pending_task']['stage'], 'reviewing')
        self.assertEqual(engine.run['agent_dispatches'], 1)
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts'], {'greet': 1})

    def test_pre_accept_index_damage_blocks_commit_and_preserves_approved_review(self):
        plan = self.fixture()
        saved = {}

        def delete(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker':
                self.remove_legacy(kwargs['cwd'])

        backend = ScriptedBackend(delete)
        engine = self.engine(backend)
        emit = engine.emit_hook

        def corrupt(event, payload, logs):
            emit(event, payload, logs)
            if event == 'pre_accept' and not saved:
                entry = next(d for d in reversed(engine.run['dispatches']) if d['role'] == 'reviewer')
                path = Path(entry['path_evidence'][0]['artifact'])
                saved.update(path=path, raw=path.read_bytes())
                path.write_bytes(b'{}')

        with patch.object(engine, 'emit_hook', side_effect=corrupt):
            with self.assertRaisesRegex(HarnessError, 'path evidence index changed'):
                engine.start('Replace legacy docs', plan)
        self.assertEqual(engine.run['accepted'], {})
        self.assertIn('pending_acceptance', engine.run)
        saved['path'].write_bytes(saved['raw'])
        result = self.engine(backend).resume(engine.run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['attempts'], {'greet': 1})
        self.assertEqual(sum(c['kind'] == 'review' for c in backend.calls), 1)

    def test_large_change_does_not_hide_out_of_scope_edits(self):
        plan = self.fixture()

        def escape(backend, kwargs, packet, result):
            if kwargs['role'] == 'worker':
                self.remove_legacy(kwargs['cwd'])
                (kwargs['cwd'] / 'outside.txt').write_text('not authorized')

        backend = ScriptedBackend(escape)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Replace legacy docs', plan)
        calls = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run['id'])
        self.assertEqual(len(backend.calls), calls)
        self.assertFalse(any(c['role'] == 'reviewer' for c in backend.calls))
        self.assertEqual(engine.run['accepted'], {})
