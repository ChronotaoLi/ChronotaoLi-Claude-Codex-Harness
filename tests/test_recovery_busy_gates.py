"""Exercise actual recovery preparation boundaries with isolated busy state."""
import copy
import unittest

import test_environment_updates as environments
from test_harness import RepoCase
from harness.util import HarnessError


class RecoveryBusyGateTests(RepoCase):
    def environment(self):
        return environments.EnvironmentUpdateTests.blocked(self)

    def assert_preparation(self, engine, prepare, blocked):
        before = copy.deepcopy(engine.run)
        files = {str(p): p.read_bytes() for p in engine.directory.rglob('*') if p.is_file()}
        if blocked:
            with self.assertRaisesRegex(HarnessError, "pending lifecycle gates|已有待处理阶段"):
                prepare()
        else:
            self.assertIsNotNone(prepare())
        self.assertEqual(engine.run, before)
        self.assertEqual({str(p): p.read_bytes() for p in engine.directory.rglob('*') if p.is_file()}, files)
