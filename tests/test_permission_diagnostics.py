import copy
import unittest
from types import SimpleNamespace

from harness.permission_diagnostics import permission_diagnostics
from harness.util import HarnessError


class PermissionDiagnosticsTests(unittest.TestCase):
    def test_exact_argv_is_independent_from_unavailable_provider_reason(self):
        config = SimpleNamespace(data={'verification': {'commands': [['python3', '-m', 'unittest']]},
                                      'permissions': {'worker_commands': []}})
        original = copy.deepcopy(config.data)
        terminal = {'permission_denials': [{'tool_name': 'Bash', 'tool_use_id': 'tool_1',
                    'tool_input': {'command': 'PRIVATE'}, 'reason': 'PRIVATE'}]}
        result = permission_diagnostics(config, terminal, argv=['python3', '-m', 'unittest'])
        self.assertTrue(result['local_command_capability']['local_exact_argv_match'])
        self.assertEqual(result['provider_denials'][0]['reason'], 'unavailable')
        self.assertFalse(result['provider_allowance_verified'])
        self.assertNotIn('PRIVATE', repr(result))
        self.assertIn('Read', result['native_read_tools'])
        self.assertIn('harness.file_sha256', result['readonly_file_tools'])
        self.assertEqual(config.data, original)
        for argv in (['python3'], ['python3', '-m', 'unittest', '-v'],
                     ['sh', '-c', 'python3 -m unittest']):
            result = permission_diagnostics(config, terminal, argv=argv)
            self.assertFalse(result['local_command_capability']['local_exact_argv_match'])

    def test_unknown_evidence_and_invalid_argv_never_grant_permission(self):
        config = SimpleNamespace(data={'verification': {'commands': []},
                                      'permissions': {'worker_commands': []}})
        self.assertEqual(permission_diagnostics(config)['reported_denial_count'], 0)
        for argv in ('python3', [], [True], ['']):
            with self.assertRaises(HarnessError):
                permission_diagnostics(config, argv=argv)
