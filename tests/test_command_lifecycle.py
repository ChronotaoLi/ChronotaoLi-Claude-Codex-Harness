import tempfile
import unittest
from pathlib import Path

from harness.util import (HarnessError, confirm_dispatches_exited, ensure_dispatches_exited,
                          validate_command_directories, write_json)


class CommandLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'dispatches' / 'parent' / 'commands'
        folder = self.directory / ('a' * 32)
        folder.mkdir(parents=True)
        self.path = folder / 'process.json'
        self.child = {'id': 'parent:command:' + folder.name, 'command_id': 'fixture',
                      'pgid': None, 'process_state': 'starting', 'exit_confirmed': False}
        self.parent = {'id': 'parent', 'pgid': None, 'process_state': 'exited', 'exit_confirmed': True,
                       'command_directory': str(self.directory)}
        self.save_child()

    def save_child(self):
        write_json(self.path, self.child)

    def test_parent_confirmation_never_hides_live_or_unknown_child(self):
        with self.assertRaises(HarnessError):
            ensure_dispatches_exited([self.parent])
        self.child.update(pgid=99, process_state='running')
        self.save_child()
        with self.assertRaises(HarnessError):
            ensure_dispatches_exited([self.parent], observe=lambda _: True)
        ensure_dispatches_exited([self.parent], observe=lambda _: False)

    def test_audited_child_assertion_is_digest_bound(self):
        original = self.path.read_bytes()
        audits, saves = [], []
        confirm_dispatches_exited([self.parent], ['parent'], audit=lambda *args, **kw: audits.append((args, kw)),
                                  save=lambda: saves.append(True))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(audits), 1)
        self.assertEqual(len(saves), 1)
        ensure_dispatches_exited([self.parent])
        self.child['pgid'] = 100
        self.save_child()
        with self.assertRaises(HarnessError):
            ensure_dispatches_exited([self.parent], observe=lambda _: True)

    def test_entire_confirmation_batch_validated_before_mutation(self):
        second = dict(self.parent, id='other', exit_confirmed=False, command_directory=str(self.root / 'other' / 'commands'))
        corrupt = Path(second['command_directory']) / ('b' * 32)
        corrupt.mkdir(parents=True)
        (corrupt / 'process.json').write_text('{}')
        audits = []
        with self.assertRaises(HarnessError):
            confirm_dispatches_exited([self.parent, second], ['parent', 'other'],
                audit=lambda *args, **kw: audits.append(args), save=lambda: None)
        self.assertEqual(audits, [])
        self.assertNotIn('command_exit_confirmations', self.parent)

    def test_prepared_and_legacy_dispatches_are_safe(self):
        self.child['process_state'] = 'prepared'
        self.save_child()
        ensure_dispatches_exited([self.parent, {'id': 'legacy', 'process_state': 'prepared', 'pgid': None}])
        validate_command_directories([self.parent, {'id': 'legacy'}], self.root)

    def test_invalid_directory_and_symlink_rejected(self):
        for path in (str(self.root), str(self.directory) + '/.', 'relative', str(self.root / 'dispatches' / '..' / 'commands')):
            with self.assertRaises(HarnessError):
                validate_command_directories([dict(self.parent, command_directory=path)], self.root)
        link = self.root / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(HarnessError):
            validate_command_directories([dict(self.parent, command_directory=str(link / 'commands'))], self.root)

    def test_registered_missing_directory_blocks_but_legacy_absence_is_safe(self):
        missing = self.root / 'dispatches' / 'missing' / 'commands'
        entry = {'id': 'missing', 'command_directory': str(missing), 'exit_confirmed': True}
        with self.assertRaises(HarnessError):
            validate_command_directories([entry], self.root)
        validate_command_directories([{'id': 'legacy', 'exit_confirmed': True}], self.root)

    def test_synchronous_start_failure_does_not_require_operator_confirmation(self):
        from harness.command_server import Broker, create_manifest, command_id
        job = self.root / 'dispatches' / 'missing-binary'
        argv = [str(self.root / 'no-such-binary')]
        config = {'verification': {'commands': [argv]}, 'permissions': {'worker_commands': []},
                  'runtime': {'verify_timeout_seconds': 1}}
        metadata = create_manifest(config, self.root, job)
        broker = Broker(metadata['artifact'], metadata['artifact_digest'])
        result = broker.run_command({'command_id': command_id(argv)})
        self.assertTrue(result['exit_confirmed'])
        self.assertIsNotNone(result['error'])
        parent = {'id': job.name, 'command_directory': str(job / 'commands'), 'exit_confirmed': True}
        validate_command_directories([parent], self.root)
        ensure_dispatches_exited([parent])
