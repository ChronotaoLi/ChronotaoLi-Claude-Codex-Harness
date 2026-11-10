import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import run_shards

BASE_SUITE = {
    "fake_support.py": "VALUE = 3\n",
    "test_fake_alpha.py": '''
        import sys
        import unittest
        from fake_support import VALUE

        class Alpha(unittest.TestCase):
            def test_doc(self):
                """A docstring line shown by -v."""
            def test_stderr(self):
                sys.stderr.write("partial stderr line ... ")
            def test_stdout(self):
                print("stdout " * 2000)
                self.assertEqual(VALUE, 3)
            def test_subtests(self):
                for i in range(3):
                    with self.subTest(i=i):
                        self.assertLess(i, 3)
    ''',
    # Imports a sibling test module by top-level name, as test modules in this repository do.
    "test_fake_beta.py": '''
        import unittest
        from test_fake_alpha import VALUE

        class Beta(unittest.TestCase):
            def test_pass(self):
                self.assertEqual(VALUE, 3)
            @unittest.skip("decorated skip")
            def test_skip(self):
                pass
            def test_skip_inside(self):
                self.skipTest("this filesystem says no")
    ''',
    "test_fake_gamma.py": '''
        import unittest

        class Gamma(unittest.TestCase):
            def test_one(self):
                pass
            def test_two(self):
                pass
    ''',
}
ALPHA = [f"test_fake_alpha.Alpha.{name}" for name in ("test_doc", "test_stderr", "test_stdout", "test_subtests")]
BETA = [f"test_fake_beta.Beta.{name}" for name in ("test_pass", "test_skip", "test_skip_inside")]
GAMMA = ["test_fake_gamma.Gamma.test_one", "test_fake_gamma.Gamma.test_two"]


class ShardRunnerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.root = self.base / "repo"
        self.out = self.base / "out"

    def write_suite(self, files):
        tests = self.root / "tests"
        tests.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            (tests / name).write_text(textwrap.dedent(body), encoding="utf-8")

    def run_main(self, *args):
        lines = []
        code = run_shards.main(["--out", str(self.out), *args], root=self.root, echo=lines.append)
        result = json.loads((self.out / "result.json").read_text(encoding="utf-8"))
        return code, result, lines

    def collect(self):
        self.out.mkdir(exist_ok=True)
        return run_shards.collect(self.root, run_shards.DEFAULT_PATTERN, self.out)

    def test_collection_matches_the_single_process_discover_run(self):
        self.write_suite(BASE_SUITE)
        collection = self.collect()
        self.assertEqual(list(collection.modules), ["test_fake_alpha", "test_fake_beta", "test_fake_gamma"])
        self.assertEqual(sorted(collection.ids), sorted(ALPHA + BETA + GAMMA))
        self.assertEqual(collection.failed_modules, [])
        single = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                                cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(single.returncode, 0, single.stderr)
        parsed = run_shards.parse_log(single.stderr)
        self.assertEqual(sorted(execution.test_id for execution in parsed.executions), sorted(collection.ids))
        self.assertEqual(parsed.ran, len(collection.ids))

    def test_split_is_deterministic_complete_and_balanced(self):
        counts = {f"test_m{index:02d}": (index * 7) % 23 + 1 for index in range(30)}
        shuffled = dict(sorted(counts.items(), key=lambda item: item[0][::-1]))
        for shards in (1, 3, 4, 40):
            groups = run_shards.split(counts, shards)
            self.assertEqual(groups, run_shards.split(shuffled, shards))
            self.assertEqual(len(groups), min(shards, len(counts)))
            self.assertEqual(sorted(name for group in groups for name in group), sorted(counts))
            loads = [sum(counts[name] for name in group) for group in groups]
            self.assertLessEqual(max(loads) - min(loads), max(counts.values()))
        with self.assertRaises(ValueError):
            run_shards.split(counts, 0)

    def test_sharded_run_executes_every_collected_test_once_and_counts_skips(self):
        self.write_suite(BASE_SUITE)
        code, result, _ = self.run_main("--shards", "2")
        self.assertEqual(code, 0, result)
        self.assertTrue(result["ok"])
        self.assertEqual((result["collected"], result["executed"]), (9, 9))
        self.assertEqual((result["missing"], result["duplicated"], result["unexpected"]), ([], [], []))
        self.assertEqual(result["outcomes"]["skipped"], 2)
        self.assertEqual(result["outcomes"]["passed"], 7)
        self.assertEqual([shard["exit"] for shard in result["shards"]], [0, 0])
        self.assertEqual([shard["ran"] for shard in result["shards"]], [shard["tests"] for shard in result["shards"]])
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        groups = [shard["modules"] for shard in manifest["shards"]]
        self.assertEqual(sorted(name for group in groups for name in group),
                         ["test_fake_alpha", "test_fake_beta", "test_fake_gamma"])
        # beta imports test_fake_alpha by top-level name from a different shard process
        self.assertFalse(any({"test_fake_alpha", "test_fake_beta"} <= set(group) for group in groups))
        for shard in result["shards"]:
            self.assertTrue(Path(shard["log"]).is_file())

    def test_module_left_out_of_a_shard_is_reported_missing(self):
        self.write_suite(BASE_SUITE)
        original = run_shards.split

        def drop_beta(counts, shards):
            return [[name for name in group if name != "test_fake_beta"] for group in original(counts, shards)]

        with mock.patch.object(run_shards, "split", drop_beta):
            code, result, lines = self.run_main("--shards", "2")
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing"], sorted(BETA))
        self.assertEqual(result["executed"], len(ALPHA + GAMMA))
        self.assertEqual([shard["exit"] for shard in result["shards"]], [0, 0])
        printed = "\n".join(lines)
        for test_id in BETA:
            self.assertIn(test_id, printed)

    def test_module_run_by_two_shards_is_reported_duplicated(self):
        self.write_suite(BASE_SUITE)
        original = run_shards.split

        def alpha_everywhere(counts, shards):
            return [sorted(set(group) | {"test_fake_alpha"}) for group in original(counts, shards)]

        with mock.patch.object(run_shards, "split", alpha_everywhere):
            code, result, _ = self.run_main("--shards", "2")
        self.assertNotEqual(code, 0)
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["duplicated"],
                         [{"test": test_id, "executed": 2, "collected": 1} for test_id in sorted(ALPHA)])

    def test_failing_test_in_any_shard_fails_the_run(self):
        self.write_suite({**BASE_SUITE, "test_fake_broken.py": '''
            import unittest

            class Broken(unittest.TestCase):
                def test_error(self):
                    raise RuntimeError("boom")
                def test_fail(self):
                    self.assertEqual(1, 2)
                def test_subtest_fail(self):
                    for i in range(2):
                        with self.subTest(i=i):
                            self.assertEqual(i, 0)
        '''})
        code, result, _ = self.run_main("--shards", "3")
        self.assertNotEqual(code, 0)
        self.assertEqual((result["missing"], result["duplicated"], result["unexpected"]), ([], [], []))
        self.assertEqual(result["executed"], result["collected"])
        self.assertEqual((result["outcomes"]["failed"], result["outcomes"]["error"]), (2, 1))
        for shard in result["shards"]:
            self.assertEqual(shard["exit"] != 0, "test_fake_broken" in shard["modules"], shard)
        lines = {(item["test"], item["line"]) for item in result["failures"]}
        self.assertIn(("test_fake_broken.Broken.test_error", "RuntimeError: boom"), lines)
        self.assertIn(("test_fake_broken.Broken.test_fail", "AssertionError: 1 != 2"), lines)
        self.assertTrue(any(test.startswith("test_fake_broken.Broken.test_subtest_fail (i=1)") for test, _ in lines))

    def test_log_accounting_survives_noise_repeated_statuses_and_class_fixtures(self):
        self.write_suite({"test_fake_edges.py": '''
            import sys
            import unittest

            class ASkipped(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise unittest.SkipTest("class off")
                def test_a(self):
                    pass
                def test_b(self):
                    pass

            class BNoisy(unittest.TestCase):
                def test_doc(self):
                    """First docstring line."""
                def test_stderr(self):
                    sys.stderr.write("noise ... without newline ")
                def test_stdout(self):
                    print("x" * 10000)

            class CFailures(unittest.TestCase):
                def test_fail_then_cleanup_error(self):
                    self.addCleanup(lambda: 1 / 0)
                    self.fail("first")
                def test_subtests_then_fail(self):
                    """Documented subtests."""
                    for i in range(3):
                        with self.subTest(i=i):
                            self.assertEqual(i, 0)
                    self.fail("after subtests")

            class DBrokenSetup(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise ValueError("setup broke")
                def test_never(self):
                    pass

            class EExpected(unittest.TestCase):
                @unittest.expectedFailure
                def test_xfail(self):
                    self.assertEqual(1, 2)

            class FSkippedAfterXfail(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise unittest.SkipTest("printed without a name")
                def test_c(self):
                    pass
        '''})
        code, result, _ = self.run_main("--shards", "1")
        self.assertNotEqual(code, 0)
        self.assertEqual(result["collected"], 10)
        # a class whose setUpClass skipped is executed-as-skipped; a failing setUpClass leaves its tests
        # missing, and so does a skip that unittest printed without naming the class
        self.assertEqual(result["missing"], ["test_fake_edges.DBrokenSetup.test_never",
                                             "test_fake_edges.FSkippedAfterXfail.test_c"])
        self.assertEqual((result["duplicated"], result["unexpected"]), ([], []))
        shard = result["shards"][0]
        self.assertEqual((shard["ran"], shard["executed"]), (6, 8))
        self.assertEqual(len(result["log_problems"]), 1)
        outcomes = result["outcomes"]
        self.assertEqual((outcomes["passed"], outcomes["failed"], outcomes["error"], outcomes["skipped"],
                          outcomes["expected failure"], outcomes["unknown"]), (3, 1, 1, 2, 1, 0))
        self.assertEqual(len(result["fixture_errors"]), 1)
        self.assertIn("test_fake_edges.DBrokenSetup", result["fixture_errors"][0])

    def test_module_that_fails_to_import_fails_the_run_before_any_shard_starts(self):
        self.write_suite({**BASE_SUITE, "test_fake_bad.py": "import fake_module_that_does_not_exist\n"})
        code, result, _ = self.run_main("--shards", "2")
        self.assertNotEqual(code, 0)
        self.assertEqual(result["import_failures"], ["test_fake_bad"])
        self.assertEqual(list(self.out.glob("shard-*.log")), [])

    def test_duplicate_ids_in_collection_fail_the_run_before_any_shard_starts(self):
        self.write_suite({**BASE_SUITE,
            # A custom load_tests that repeats a test, and a module that re-exports
            # another module's TestCase class: both collect the same ID twice.
            "test_fake_dup.py": '''
                import unittest

                class C(unittest.TestCase):
                    def test_one(self):
                        pass

                def load_tests(loader, tests, pattern):
                    return unittest.TestSuite([C("test_one"), C("test_one")])
            ''',
            "test_fake_reexport.py": "from test_fake_gamma import Gamma\n"})
        code, result, lines = self.run_main("--shards", "2")
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["duplicate_ids"], [{"test": test_id, "collected": 2} for test_id in
                                                   ["test_fake_dup.C.test_one", *GAMMA]])
        self.assertEqual(list(self.out.glob("shard-*.log")), [])
        self.assertFalse((self.out / "manifest.json").exists())
        printed = "\n".join(lines)
        for test_id in ["test_fake_dup.C.test_one", *GAMMA]:
            self.assertIn(f"{test_id}: collected 2 times", printed)

    def test_stop_kills_descendants_that_ignore_sigint_whether_or_not_the_shard_already_exited(self):
        descendant = textwrap.dedent('''
            import os, signal, sys, time
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            with open(sys.argv[1] + ".tmp", "w") as handle:
                handle.write(str(os.getpid()))
            os.replace(sys.argv[1] + ".tmp", sys.argv[1])
            time.sleep(120)
        ''')
        # Stands in for a shard: starts the descendant in its own process group,
        # then either exits at once or waits for the interrupt to end it.
        shard = textwrap.dedent('''
            import os, subprocess, sys, time
            subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]])
            while not os.path.exists(sys.argv[2]):
                time.sleep(0.02)
            if sys.argv[3] == "exit":
                sys.exit(0)
            time.sleep(120)
        ''')
        for mode in ("exited_before_stop", "exits_on_sigint"):
            with self.subTest(mode=mode):
                pid_file = self.base / f"{mode}.pid"
                proc = subprocess.Popen([sys.executable, "-c", shard, descendant, str(pid_file),
                                         "exit" if mode == "exited_before_stop" else "wait"],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
                leftovers = []

                def cleanup(proc=proc, leftovers=leftovers):
                    for pid in leftovers:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            pass
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()

                self.addCleanup(cleanup)
                deadline = time.monotonic() + 30
                while not pid_file.exists():
                    self.assertLess(time.monotonic(), deadline, "descendant did not start")
                    time.sleep(0.02)
                child = int(pid_file.read_text())
                leftovers.append(child)
                if mode == "exited_before_stop":
                    self.assertEqual(proc.wait(timeout=30), 0)
                    os.kill(child, 0)  # the descendant outlived its shard
                run_shards._stop([proc], grace=0.5)
                self.assertIsNotNone(proc.returncode)
                if mode == "exits_on_sigint":
                    self.assertNotEqual(proc.returncode, 0)
                deadline = time.monotonic() + 10
                while True:
                    try:
                        os.kill(child, 0)
                    except ProcessLookupError:
                        break
                    self.assertLess(time.monotonic(), deadline, f"descendant {child} survived the stop")
                    time.sleep(0.05)
                self.assertFalse(run_shards._group_alive(proc.pid))
                leftovers.clear()

    def test_module_that_fails_to_import_inside_a_shard_is_reported(self):
        self.write_suite(BASE_SUITE)
        collection = self.collect()
        groups = run_shards.split({name: len(ids) for name, ids in collection.modules.items()}, 2)
        groups[0].append("test_fake_ghost")
        result = run_shards.run_groups(groups, collection, self.out, root=self.root, echo=lambda line: None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["import_failures"], ["test_fake_ghost (shard 1)"])
        self.assertEqual((result["missing"], result["unexpected"]), ([], []))
        self.assertNotEqual(result["shards"][0]["exit"], 0)
        self.assertEqual(result["shards"][1]["exit"], 0)

    def test_interrupt_stops_shard_processes_and_their_children(self):
        pids = self.base / "pids"
        self.write_suite({"test_fake_slow.py": '''
            import os
            import subprocess
            import sys
            import time
            import unittest
            from pathlib import Path

            class Slow(unittest.TestCase):
                def test_wait(self):
                    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
                    target = Path(os.environ["FAKE_SHARD_PIDS"])
                    target.with_suffix(".tmp").write_text(f"{os.getpid()} {child.pid}")
                    os.replace(target.with_suffix(".tmp"), target)
                    time.sleep(120)
        '''})
        script = ("import sys, run_shards; from pathlib import Path; "
                  "sys.exit(run_shards.main(sys.argv[2:], root=Path(sys.argv[1])))")
        env = {**os.environ, "FAKE_SHARD_PIDS": str(pids), "PYTHONPATH": str(Path(run_shards.__file__).parent)}
        runner = subprocess.Popen([sys.executable, "-c", script, str(self.root), "--shards", "1", "--out", str(self.out)],
                                  env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        leftovers = []

        def cleanup():
            if runner.poll() is None:
                runner.kill()
                runner.wait()
            for pid in leftovers:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

        self.addCleanup(cleanup)
        deadline = time.monotonic() + 60
        while not pids.exists():
            self.assertIsNone(runner.poll(), "runner exited before the shard started")
            self.assertLess(time.monotonic(), deadline, "shard did not start")
            time.sleep(0.05)
        shard_pid, child_pid = map(int, pids.read_text().split())
        leftovers.extend((shard_pid, child_pid))
        runner.send_signal(signal.SIGINT)
        output, _ = runner.communicate(timeout=30)
        self.assertNotEqual(runner.returncode, 0, output)
        for pid in (shard_pid, child_pid):
            deadline = time.monotonic() + 10
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                self.assertLess(time.monotonic(), deadline, f"process {pid} survived the interrupt")
                time.sleep(0.05)
        leftovers.clear()
        self.assertFalse((self.out / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
