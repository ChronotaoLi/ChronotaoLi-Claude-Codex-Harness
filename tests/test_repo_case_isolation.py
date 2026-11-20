"""What a repository case writes outside its repository stays its own, whatever runs beside it."""
import unittest

from test_harness import RepoCase


class OutsideFolderTests(unittest.TestCase):
    def test_a_file_kept_outside_the_repository_is_private_to_its_case(self):
        class Case(RepoCase):
            def runTest(self):
                pass
        # Two cases set up side by side under one temporary root, as two test processes are.
        first, second = Case(), Case()
        self.addCleanup(first.doCleanups)
        first.setUp()
        self.addCleanup(second.doCleanups)
        second.setUp()
        self.assertNotEqual(first.outside.resolve(), second.outside.resolve())
        for case in (first, second):
            self.assertTrue(case.outside.is_dir())
            for repo in (first.repo.resolve(), second.repo.resolve()):
                outside = case.outside.resolve()
                self.assertFalse(outside.is_relative_to(repo) or repo.is_relative_to(outside))
        # The second case writes the same name after the first, before the first reads it back.
        name = "environment-update.json"
        (first.outside / name).write_text("first")
        (second.outside / name).write_text("second")
        self.assertEqual((first.outside / name).read_text(), "first")
        self.assertTrue(second.doCleanups())
        self.assertFalse(second.outside.exists())
        self.assertEqual((first.outside / name).read_text(), "first")
        self.assertTrue(first.doCleanups())
        self.assertFalse(first.outside.exists())


if __name__ == "__main__":
    unittest.main()
