import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import git_isolation
from harness import git, stops, workspace_input_names
from harness.cleanup import cleanup_run
from harness.private_state import ensure_private_state
from harness.run_store import RunStore
from harness.util import HarnessError, write_json
from harness.workspace_inputs import prepare_run, validate_inputs, cleanup_inputs
from harness.versions import RUN_SCHEMA_VERSION
from test_readonly_cleanup import nonregular, tree_bytes
from test_run_store import swap

class WorkspaceInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve() / 'repo'
        self.repo.mkdir()
        git.git(self.repo, 'init')
        git.git(self.repo, 'config', 'user.email', 'test@example.com')
        git.git(self.repo, 'config', 'user.name', 'Test')
        (self.repo / '.gitignore').write_text('.harness/\nvendor/\ndeps/\n')
        git.git(self.repo, 'add', '.gitignore')
        git.git(self.repo, 'commit', '-m', 'initial')
        self.ident = 'run_test'
        self.directory = self.repo / '.harness/runs' / self.ident
        self.directory.mkdir(parents=True)
        self.checkout = self.repo / '.harness/worktrees' / self.ident
        git.git(self.repo, 'worktree', 'add', '-b', 'harness/' + self.ident, str(self.checkout))
        self.run = dict(schema_version=RUN_SCHEMA_VERSION, workspace_inputs=None, id=self.ident,
                        repo=str(self.repo), worktree=str(self.checkout), status='paused',
                        expected_head=git.head(self.checkout), dispatches=[], worker_receipts={},
                        verification_checkouts=[],
                        worktree_binding=git.capture_worktree_binding(self.checkout, 'harness/' + self.ident))
        self.save()
        (self.repo / 'deps').mkdir()
        self.source = self.repo / 'deps/input.txt'
        self.source.write_text('dependency')
        self.source.chmod(0o644)
        self.manifest = dict(schema_version=1, run_id=self.ident, head=self.run['expected_head'],
                             source_root='.', max_files=1, max_bytes=100,
                             files=[dict(source='deps/input.txt', destination='vendor/input.txt',
                                         sha256=hashlib.sha256(b'dependency').hexdigest(),mode=0o644,
                                         type='file', ownership='input_copy')])
    def save(self):
        write_json(self.directory / 'state.json', self.run)
    def prepare(self):
        result = prepare_run(self.repo, self.ident, self.manifest)
        import json
        self.run = json.loads((self.directory / 'state.json').read_text())
        return result
    def test_copy_and_terminal_cleanup(self):
        self.prepare()
        target = self.checkout / 'vendor/input.txt'
        self.assertNotEqual(target.stat().st_ino, self.source.stat().st_ino)
        self.prepare()
        validate_inputs(self.run, self.directory)
        self.run['status'] = 'completed'
        cleanup_inputs(self.run, self.directory)
        self.assertTrue(target.exists())
        cleanup_inputs(self.run, self.directory, remove=True)
        cleanup_inputs(self.run, self.directory, remove=True)
        self.assertFalse(target.exists())
        self.assertTrue(self.source.exists())
    def test_name_check_uses_each_target_parent_rule(self):
        distinct = self.checkout / 'vendor' / 'distinct'
        unknown = self.checkout / 'vendor' / 'unknown'
        for directory in (distinct, unknown):
            directory.mkdir(parents=True)
        distinct_id = distinct.stat().st_ino

        def rules(fd):
            return 'byte_exact' if os.fstat(fd).st_ino == distinct_id else 'unobserved'

        destinations = [distinct / 'Data', distinct / 'data' / 'child', unknown / 'Data']
        with patch.object(workspace_input_names, '_name_rules', side_effect=rules):
            evidence = workspace_input_names.check_destination_layout(
                destinations, self.checkout, self.directory, '.harness-input-test', run_id=self.ident)
            self.assertEqual([row['comparison'] for row in evidence], ['target_byte_exact', 'unobserved'])
            with self.assertRaisesRegex(HarnessError, 'naming semantics are unobserved'):
                workspace_input_names.check_destination_layout(
                    [*destinations, unknown / 'data' / 'child'],
                    self.checkout, self.directory, '.harness-input-test', run_id=self.ident)
        self.assertEqual(list(distinct.iterdir()), [])
        self.assertEqual(list(unknown.iterdir()), [])

    def test_same_device_with_different_directory_rules_is_unobserved(self):
        private_id = self.directory.parent.parent.stat().st_ino
        self.assertEqual(self.directory.parent.parent.stat().st_dev, self.checkout.stat().st_dev)

        def rules(fd):
            return 'apfs_sensitive' if os.fstat(fd).st_ino == private_id else 'apfs_insensitive'

        name = 'vendor/résumé'
        self.manifest['files'][0]['destination'] = name
        with patch.object(workspace_input_names, '_name_rules', side_effect=rules), \
                patch.object(workspace_input_names, '_private_cache_fd') as probe:
            self.prepare()
        probe.assert_not_called()
        self.assertEqual((self.checkout / name).read_bytes(), self.source.read_bytes())
        journal = json.loads((self.directory / 'workspace-inputs.json').read_text())
        self.assertEqual(journal['destination_name_observations'][0]['comparison'], 'unobserved')

    def test_unobserved_alias_checks_children_of_existing_directory(self):
        existing = self.checkout / 'vendor' / 'Package'
        existing.mkdir(parents=True)
        exact, alias = existing / 'leaf', existing.with_name('package') / 'leaf'
        lookup = workspace_input_names._lookup

        def lower_spelling_is_missing(name, fd=None):
            if name == 'package' or isinstance(name, Path) and 'package' in name.parts:
                return None
            return lookup(name, fd)

        with patch.object(workspace_input_names, '_name_rules', return_value='unobserved'), \
                patch.object(workspace_input_names, '_lookup', side_effect=lower_spelling_is_missing):
            with self.assertRaisesRegex(HarnessError, 'naming semantics are unobserved'):
                workspace_input_names.check_destination_layout(
                    [exact, alias], self.checkout, self.directory, '.harness-input-test', run_id=self.ident)
            exact.write_text('unowned')
            with self.assertRaisesRegex(HarnessError, 'naming semantics are unobserved'):
                workspace_input_names.check_destination_layout(
                    [alias], self.checkout, self.directory, '.harness-input-test', run_id=self.ident)
            workspace_input_names.check_destination_layout(
                [alias.with_name('sibling')], self.checkout, self.directory, '.harness-input-test', run_id=self.ident)
        self.assertEqual(list(existing.iterdir()), [exact])
        self.assertEqual(exact.read_text(), 'unowned')

    def test_unobserved_layout_checks_two_spellings_of_the_same_bytes(self):
        first = self.checkout / 'vendor' / 'é'
        second = first.with_name('\udcc3\udca9') / 'leaf'
        self.assertEqual(os.fsencode(first.name), os.fsencode(second.parent.name))
        with patch.object(workspace_input_names, '_name_rules', return_value='unobserved'):
            with self.assertRaisesRegex(HarnessError, 'destination layout may conflict'):
                workspace_input_names.check_destination_layout(
                    [first, second], self.checkout, self.directory, '.harness-input-test', run_id=self.ident)
        self.assertFalse(first.parent.exists())

    def test_private_name_records_are_bounded_and_reused(self):
        state = self.directory.parent.parent
        target = self.checkout / 'résumé'

        def check(name, run_id):
            row, = workspace_input_names.check_destination_layout(
                [name], self.checkout, state, '.harness-input-test', run_id=run_id)
            return row

        with patch.object(workspace_input_names, '_name_rules', return_value='apfs_insensitive'), \
                patch.object(workspace_input_names, '_PROBE_LIMIT', 2), \
                patch.object(os, 'unlink') as unlink, patch.object(os, 'rmdir') as rmdir:
            first = check(target, self.ident)
            second = check(self.checkout / 'café', 'run_next')
            full = check(self.checkout / 'déjà', 'run_next')
            reuse = check(target, 'run_next')
            unlink.assert_not_called()
            rmdir.assert_not_called()

        scratch, = state.glob('input-names-*')
        self.assertEqual(first['comparison'], 'private_apfs_volume')
        self.assertEqual(first['private_observation'], {
            'directory': {'device': scratch.stat().st_dev, 'inode': scratch.stat().st_ino},
            'rule': 'apfs_insensitive', 'retention': 'bounded',
        })
        self.assertEqual(second['private_observation'], first['private_observation'])
        self.assertEqual(first, reuse)
        self.assertEqual(full['comparison'], 'unobserved')
        self.assertNotIn('private_observation', full)
        self.assertEqual({entry.name for entry in scratch.iterdir()}, {'résumé', 'café'})
        self.assertTrue((scratch / target.name).read_bytes().endswith(b'run_test\n'))
        self.assertTrue((scratch / 'café').read_bytes().endswith(b'run_next\n'))
        self.assertFalse(target.exists())

    def test_private_probe_unavailable_leaves_observation_unknown(self):
        name = 'vendor/résumé'
        self.manifest['files'][0]['destination'] = name
        with patch.object(workspace_input_names, '_name_rules', return_value='apfs_insensitive'), \
                patch.object(workspace_input_names.fcntl, 'flock',
                             side_effect=BlockingIOError(errno.EAGAIN, 'probe cache busy')) as flock:
            # Exercise the observation directly because prepare-run also uses flock.
            rows = workspace_input_names.check_destination_layout(
                [self.checkout / name], self.checkout, self.directory.parent.parent,
                '.harness-input-test', run_id=self.ident)
            self.assertEqual(rows[0]['comparison'], 'unobserved')
            self.assertNotIn('private_observation', rows[0])
            flock.assert_called_once()
        self.assertFalse((self.checkout / 'vendor').exists())

    def test_incomplete_retained_record_is_preserved_and_publication_proceeds(self):
        state = self.directory.parent.parent
        scratch = state / f'input-names-{state.stat().st_dev}-apfs_insensitive'
        scratch.mkdir(mode=0o700)
        name = 'résumé'
        record = scratch / name
        partial = workspace_input_names._PROBE_HEADER + b'run_other'
        record.write_bytes(partial)
        self.manifest['files'][0]['destination'] = f'vendor/{name}'
        with patch.object(workspace_input_names, '_name_rules', return_value='apfs_insensitive'):
            self.prepare()
        self.assertEqual(record.read_bytes(), partial)
        self.assertEqual(list(scratch.iterdir()), [record])
        self.assertEqual((self.checkout / 'vendor' / name).read_bytes(), self.source.read_bytes())
        journal = json.loads((self.directory / 'workspace-inputs.json').read_text())
        self.assertEqual(journal['destination_name_observations'][0]['comparison'], 'unobserved')

    def test_known_apfs_creation_rejection_precedes_freezing(self):
        name = 'résumé'
        self.manifest['files'][0]['destination'] = f'vendor/{name}'
        before = (self.directory / 'state.json').read_bytes()
        real_open = os.open

        def reject_probe(path, flags, mode=0o777, *, dir_fd=None):
            if path == name and flags & os.O_EXCL:
                raise OSError(errno.EILSEQ, 'injected filename rejection')
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with patch.object(workspace_input_names, '_name_rules', return_value='apfs_insensitive'), \
                patch.object(os, 'open', side_effect=reject_probe):
            with self.assertRaisesRegex(HarnessError, 'invalid destination on this volume'):
                self.prepare()
        self.assertEqual((self.directory / 'state.json').read_bytes(), before)
        self.assertFalse((self.directory / 'workspace-inputs.json').exists())
        self.assertFalse((self.checkout / 'vendor').exists())

        self.manifest['files'][0]['destination'] = 'vendor/corrected'
        self.prepare()

    def test_unavailable_apfs_probe_preserves_formal_publication_intent(self):
        from harness import workspace_inputs

        name = 'vendor/résumé'
        self.manifest['files'][0]['destination'] = name
        with patch.object(workspace_input_names, '_name_rules', return_value='apfs_insensitive'), \
                patch.object(workspace_input_names, '_private_cache_fd', return_value=None), \
                patch.object(workspace_inputs, '_publish',
                             side_effect=OSError(errno.EILSEQ, 'injected filename rejection')):
            with self.assertRaises(OSError) as stopped:
                self.prepare()
        self.assertEqual(stopped.exception.errno, errno.EILSEQ)
        self.assertEqual(json.loads((self.directory / 'workspace-inputs-manifest.json').read_text()),
                         self.manifest)
        journal = json.loads((self.directory / 'workspace-inputs.json').read_text())
        self.assertEqual(journal['destination_name_observations'][0]['comparison'], 'unobserved')
        receipt, = journal['files']
        self.assertEqual(receipt['stage'], 'published_intent')
        self.assertEqual(Path(receipt['temp']).read_bytes(), self.source.read_bytes())
        self.assertFalse((self.checkout / name).exists())
        self.prepare()
        self.assertEqual((self.checkout / name).read_bytes(), self.source.read_bytes())

    def test_manifest_preflight_performs_no_worktree_mutations(self):
        import harness.workspace_inputs as inputs
        before = {str(item.relative_to(self.checkout)) for item in self.checkout.rglob('*')}
        check = inputs.check_destination_layout
        checkout_identity = self.checkout.stat()

        def preflight(*args, **kwargs):
            original_open = os.open

            def read_only_open(path, flags, mode=0o777, *, dir_fd=None):
                if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                    if dir_fd is not None:
                        self.assertFalse(os.path.samestat(os.fstat(dir_fd), checkout_identity))
                    else:
                        self.assertFalse(Path(path).is_relative_to(self.checkout))
                return original_open(path, flags, mode, dir_fd=dir_fd)

            # This batch needs no private sibling comparisons; any mkdir,
            # publication or cleanup here would be a worktree mutation.
            with patch.object(os, 'open', side_effect=read_only_open), \
                    patch.object(os, 'mkdir') as mkdir, patch.object(os, 'link') as link, \
                    patch.object(os, 'unlink') as unlink, patch.object(os, 'rmdir') as rmdir:
                evidence = check(*args, **kwargs)
                for mutation in (mkdir, link, unlink, rmdir):
                    mutation.assert_not_called()
            self.assertEqual({str(item.relative_to(self.checkout)) for item in self.checkout.rglob('*')}, before)
            self.assertIsNone(json.loads((self.directory / 'state.json').read_text())['workspace_inputs'])
            self.assertFalse((self.directory / 'workspace-inputs.json').exists())
            return evidence

        with patch.object(inputs, 'check_destination_layout', side_effect=preflight) as inspect:
            self.prepare()
        inspect.assert_called_once()
        self.assertEqual((self.checkout / 'vendor/input.txt').read_bytes(), self.source.read_bytes())

    def test_unknown_collision_and_escape(self):
        target = self.checkout / 'vendor/input.txt'
        target.parent.mkdir()
        target.write_text('dependency')
        with self.assertRaises(HarnessError) as caught:
            self.prepare()
        stop = stops.of(caught.exception)
        self.assertEqual((stop['kind'], stop['reason'], stop['data']['option']),
                         ('invalid_input', 'input_schema', 'manifest'))
        self.assertEqual(target.read_text(), 'dependency')
        target.unlink()
        self.manifest['files'][0]['source'] = '../outside'
        with self.assertRaises(HarnessError): self.prepare()
    def test_modified_copy_and_source_retained(self):
        self.prepare()
        target = self.checkout / 'vendor/input.txt'
        target.write_text('changed')
        with self.assertRaises(HarnessError): validate_inputs(self.run, self.directory)
        self.run['status'] = 'abandoned'
        with self.assertRaises(HarnessError): cleanup_inputs(self.run, self.directory, remove=True)
        self.assertTrue(target.exists())
    def test_missing_authority_blocks(self):
        self.prepare()
        (self.directory / 'workspace-inputs-manifest.json').unlink()
        with self.assertRaises((HarnessError, OSError)): validate_inputs(self.run, self.directory)
    def test_source_hardlink_rejected(self):
        os.link(self.source, self.repo / 'deps/alias')
        with self.assertRaises(HarnessError): self.prepare()
    def test_pending_stage_rejected(self):
        self.run['pending_task'] = {'stage':'review'}
        self.save()
        with self.assertRaises(HarnessError): self.prepare()
    def test_source_change_blocks_cleanup(self):
        self.prepare()
        self.source.write_text('new dependency')
        self.run['status'] = 'completed'
        with self.assertRaises(HarnessError): cleanup_inputs(self.run, self.directory, remove=True)
        self.assertTrue((self.checkout / 'vendor/input.txt').exists())
    def test_publish_crash_reconciles_saved_inode(self):
        import harness.workspace_inputs as inputs
        original = inputs._unlink
        def interrupted(path):
            if path.name.startswith('.harness-input-'):
                raise RuntimeError('crash after publication')
            return original(path)
        with patch.object(inputs, '_unlink', side_effect=interrupted):
            with self.assertRaises(RuntimeError): self.prepare()
        journal = self.directory / 'workspace-inputs.json'
        legacy = json.loads(journal.read_text())
        legacy.pop('destination_name_observations')
        write_json(journal, legacy)
        self.prepare()
        validate_inputs(self.run, self.directory)
    def test_unknown_replacement_after_publish_not_claimed(self):
        import harness.workspace_inputs as inputs
        with patch.object(inputs, '_publish', side_effect=RuntimeError('crash before publication')):
            with self.assertRaises(RuntimeError): self.prepare()
        target = self.checkout / 'vendor/input.txt'
        target.write_text('dependency')
        with self.assertRaises(HarnessError): self.prepare()
        self.assertTrue(target.exists())
    def test_source_symlink_rejected(self):
        self.source.rename(self.source.with_name('real'))
        self.source.symlink_to('real')
        with self.assertRaises(HarnessError): self.prepare()

    def test_cleanup_selection_flag_alone_is_readonly_preview(self):
        self.prepare()
        # cleanup-run also enumerates retained verification checkouts.
        self.run.update(status='abandoned', branch='harness/' + self.ident, final_history=[])
        self.save()
        target = self.checkout / 'vendor/input.txt'
        state_bytes = (self.directory / 'state.json').read_bytes()
        receipt_bytes = (self.directory / 'workspace-inputs.json').read_bytes()
        result = cleanup_run(self.repo, self.ident, remove_input_copies=True)['worktree']
        self.assertEqual(result['status'], 'input_copies_present')
        self.assertTrue(target.exists())
        self.assertEqual((self.directory / 'state.json').read_bytes(), state_bytes)
        self.assertEqual((self.directory / 'workspace-inputs.json').read_bytes(), receipt_bytes)
        result = cleanup_run(self.repo, self.ident, remove=True, remove_input_copies=True)['worktree']
        self.assertEqual(result['status'], 'removed')
        self.assertFalse(self.checkout.exists())
        self.assertEqual(self.source.read_text(), 'dependency')

    def test_input_evidence_special_files_are_refused_without_writes(self):
        ensure_private_state(self.repo)
        for name in ('run.lock', 'store.lock'):
            (self.directory / name).touch()
        evidence = ('workspace-inputs-manifest.json', 'workspace-inputs.json')

        for registered in (False, True):
            if registered:
                self.prepare()
            for name in evidence:
                for form in ('fifo', 'symlink'):
                    with self.subTest(registered=registered, name=name, form=form), \
                            nonregular(self, self.directory / name, form):
                        before = tree_bytes(self.repo / '.harness')
                        with self.assertRaises(HarnessError) as caught:
                            self.prepare()
                        stop = stops.of(caught.exception)
                        self.assertEqual((stop['kind'], stop['reason']), ('evidence', 'runtime_integrity'))
                        self.assertEqual(tree_bytes(self.repo / '.harness'), before)
                        if registered:
                            self.run.update(status='abandoned', branch='harness/' + self.ident, final_history=[])
                            self.save()
                            before = tree_bytes(self.repo / '.harness')
                            result = cleanup_run(self.repo, self.ident)
                            self.assertEqual(result['worktree']['status'], 'blocked')
                            self.assertIn(name, result['worktree']['error'])
                            self.assertEqual(tree_bytes(self.repo / '.harness'), before)
                            self.run['status'] = 'paused'
                            self.save()

    def test_prepare_refuses_replaced_directory_before_attach_or_commit(self):
        ensure_private_state(self.repo)
        for name in ('run.lock', 'store.lock'):
            (self.directory / name).touch()
        for boundary in ('attach', 'commit'):
            with self.subTest(boundary=boundary):
                moved = self.repo.with_name(self.repo.name + '-moved')
                original = getattr(RunStore, boundary)
                snapshots = []

                def replaced(store, *args):
                    swap(self.directory, moved)
                    (self.directory / 'store.lock').unlink()
                    snapshots.append(tree_bytes(self.directory))
                    return original(store, *args)

                before = tree_bytes(self.directory)
                candidate = tree_bytes(self.checkout)
                try:
                    with patch.object(RunStore, boundary, replaced), self.assertRaises(HarnessError) as caught:
                        self.prepare()
                    stop = stops.of(caught.exception)
                    self.assertEqual((stop['kind'], stop['reason']), ('runtime_failure', 'invariant'))
                    self.assertEqual(len(snapshots), 1)
                    self.assertEqual(tree_bytes(self.directory), snapshots[0])
                    self.assertEqual(tree_bytes(moved), before)
                    self.assertEqual(tree_bytes(self.checkout), candidate)
                finally:
                    if moved.exists():
                        shutil.rmtree(self.directory)
                        moved.rename(self.directory)

    def test_recovery_preserves_replaced_temp_and_destination_hardlinks(self):
        import harness.workspace_inputs as inputs
        import json
        with patch.object(inputs, '_unlink', side_effect=RuntimeError('after link')):
            with self.assertRaises(RuntimeError): self.prepare()
        receipt = json.loads((self.directory / 'workspace-inputs.json').read_text())['files'][0]
        temp = Path(receipt['temp'])
        target = self.checkout / 'vendor/input.txt'
        # Allocate the replacement while the published inode still has both
        # links. Unlinking them first permits the filesystem to reuse its ID.
        replacement = temp.with_name('replacement')
        replacement.write_text('dependency')
        replacement.chmod(receipt['identity']['mode'])
        replacement_stat = replacement.stat()
        self.assertNotEqual((replacement_stat.st_dev, replacement_stat.st_ino),
                            (receipt['identity']['device'], receipt['identity']['inode']))
        replacement.replace(temp)
        target.unlink()
        os.link(temp, target)
        ledger = self.directory / 'state.json'
        before = ledger.read_bytes()
        with self.assertRaises(HarnessError) as caught:
            self.prepare()
        stop = stops.of(caught.exception)
        self.assertEqual((stop['kind'], stop['reason']), ('evidence', 'runtime_integrity'))
        self.assertEqual(ledger.read_bytes(), before)
        self.assertTrue(temp.exists(), 'unknown temporary file must be preserved')
        self.assertTrue(target.exists())

    def test_recovery_observes_identity_after_both_links_are_replaced(self):
        import harness.workspace_inputs as inputs
        with patch.object(inputs, '_unlink', side_effect=RuntimeError('after link')):
            with self.assertRaisesRegex(RuntimeError, '^after link$'):
                self.prepare()
        receipt = json.loads((self.directory / 'workspace-inputs.json').read_text())['files'][0]
        temp = Path(receipt['temp'])
        target = self.checkout / 'vendor/input.txt'
        old_identity = receipt['identity']

        # V1 releases both links before creating the replacement. A filesystem
        # can reuse the inode number here, and the receipt cannot distinguish
        # file generations when all five recorded fields match.
        temp.unlink()
        target.unlink()
        temp.write_text('dependency')
        temp.chmod(old_identity['mode'])
        os.link(temp, target)
        replacement = temp.stat()
        new_identity = dict(device=replacement.st_dev, inode=replacement.st_ino,
                            size=replacement.st_size, mode=stat.S_IMODE(replacement.st_mode),
                            sha256=hashlib.sha256(temp.read_bytes()).hexdigest())
        same_identity = old_identity == new_identity
        old_dev_ino = [old_identity['device'], old_identity['inode']]
        new_dev_ino = [new_identity['device'], new_identity['inode']]
        observation = dict(old_dev_ino=old_dev_ino, new_dev_ino=new_dev_ino,
                           same_dev_ino=old_dev_ino == new_dev_ino, same_identity=same_identity)
        identities = f'old_identity={old_identity}, new_identity={new_identity}'
        try:
            result = self.prepare()
        except Exception as error:
            if (not isinstance(error, HarnessError)
                    or str(error) != 'workspace inputs: published temporary inode is unknown or modified'):
                error.add_note(f'{identities}, prepare_result={type(error).__name__}')
                raise
            prepare_result = 'rejected_unknown_or_modified'
        else:
            self.assertEqual(result['status'], 'prepared', f'{identities}, prepare_result={result!r}')
            prepare_result = 'prepared'

        temp_preserved, destination_preserved = temp.exists(), target.exists()
        observation.update(prepare_result=prepare_result, temp_preserved=temp_preserved,
                           destination_preserved=destination_preserved)
        if os.environ.get('GITHUB_ACTIONS') == 'true':
            print('\nLC-IDENTITY ' + json.dumps(observation, sort_keys=True), file=sys.stderr, flush=True)
        if not same_identity:
            self.assertEqual((prepare_result, temp_preserved, destination_preserved),
                             ('rejected_unknown_or_modified', True, True),
                             f'{identities}, observation={observation}')

    def test_prepare_refuses_symlink_run_lock_before_copy(self):
        outside = self.repo.parent / 'outside-lock'
        outside.write_text('keep')
        (self.directory / 'run.lock').symlink_to(outside)
        with self.assertRaises(HarnessError): self.prepare()
        self.assertFalse((self.checkout / 'vendor/input.txt').exists())
        self.assertEqual(outside.read_text(), 'keep')

    def test_fifo_source_is_rejected_without_waiting_for_writer(self):
        import subprocess
        import sys
        self.source.unlink()
        os.mkfifo(self.source)
        code = ('from pathlib import Path; from harness.workspace_inputs import _identity; '
                '_identity(Path(__import__("sys").argv[1]))')
        result = subprocess.run([sys.executable, '-c', code, str(self.source)],
                                capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ordinary independent file required', result.stderr)

    def test_fifo_replaces_registered_copy_and_resume_gate_returns(self):
        import subprocess
        import sys
        self.prepare()
        target = self.checkout / 'vendor/input.txt'
        target.unlink()
        os.mkfifo(target)
        code = ('import json,sys; from pathlib import Path; '
                'from harness.workspace_inputs import validate_inputs; p=Path(sys.argv[1]); '
                'validate_inputs(json.loads((p/"state.json").read_text()),p)')
        result = subprocess.run([sys.executable, '-c', code, str(self.directory)],
                                capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ordinary independent file required', result.stderr)
