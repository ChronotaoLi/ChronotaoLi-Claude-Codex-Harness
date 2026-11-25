"""The stop tripwire (tests/stop_tripwire.py, design 5 3.6): what counts as a publication, merging, exemptions, report."""
import contextlib
import copy
import ctypes
import errno
import gc
import io
import json
import os
import pickle
import queue
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run_shards
import stop_tripwire
from harness import cli, operations, run_store, stops, workspace_inputs
from harness.adapters import DemoBackend
from harness.operations import OperationRecord
from harness.receipts import SuppliedPlanHold
from harness.run_lifecycle import RunLifecycle
from harness.run_store import RunStore
from harness.scheduler import Scheduler
from harness.scope import operator_entries
from harness.state_files import LockedDirectory
from harness.util import EvidenceError, HarnessError, ProcessStartError, read_json, write_json
from harness.workflow import Workflow
from test_harness import ROOT, RepoCase, crash_after_save, run_cli, scratch


def raised(function):
    try:
        function()
    except BaseException as error:  # the exception and its traceback are the subject
        return error
    raise AssertionError("nothing raised")


def run_one(case):
    """Run ``case``: its result, and what the run raised (None when it returned)."""
    result = unittest.TestResult()
    try:
        case.run(result)
    except BaseException as error:  # the interrupt a test's run ends with is the subject
        return result, error
    return result, None


def signal_main(signum):
    """Send ``signum`` to the main thread, where the tests run: while it blocks the signal, the signal waits there."""
    signal.pthread_kill(threading.main_thread().ident, signum)


def mask():
    return signal.pthread_sigmask(signal.SIG_BLOCK, ())


def runtime_error():
    """An untyped error whose innermost frame is the runtime's (harness/stops.py): not raised by test code."""
    return raised(lambda: stops.validate({}))


def test_error():
    def fail():
        raise HarnessError("raised by the test itself")
    return raised(fail)


def from_the_library(*args, **kwargs):
    """A patched step that fails inside the standard library (json), outside the injection rule."""
    return json.loads("{not json")


def publish(error):
    """The CLI's stop line for ``error``: one cli publication."""
    with contextlib.redirect_stderr(io.StringIO()):
        return cli.report_stop(error)


def summary(findings):
    return [(sorted(finding.channels), finding.kind, finding.reason) for finding in findings]


class NestedRuns(unittest.TestCase):
    """A test that publishes, run nested with the tripwire enforcing, as the suite would run it."""

    def run_inner(self, case_class, *, enforce=True):
        report = scratch(self) / "report.jsonl"
        stream = io.StringIO()
        with patch.object(stop_tripwire, "ENFORCE", enforce), patch.dict(os.environ, {stop_tripwire.REPORT: str(report)}):
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(case_class("test"))
        lines = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()] if report.exists() else []
        results = [line for line in stream.getvalue().splitlines() if line.startswith("test (")]
        return result, results, lines

    def test_a_runtime_unclassified_stop_fails_its_test_once_and_only_when_enforcing(self):
        class Publishes(unittest.TestCase):
            def test(inner):
                publish(runtime_error())
        result, results, lines = self.run_inner(Publishes)
        self.assertEqual((result.testsRun, len(result.failures), len(result.errors)), (1, 1, 0))
        self.assertEqual(len(results), 1, results)
        self.assertTrue(results[0].endswith("FAIL"), results)
        self.assertIn("stop tripwire", result.failures[0][1])
        (line,) = lines
        self.assertEqual({key: line[key] for key in ("channels", "kind", "reason", "error_type", "exempt")},
                         {"channels": ["cli"], "kind": "unclassified", "reason": "untyped_harness_error",
                          "error_type": "HarnessError", "exempt": None})
        self.assertTrue(line["test"].endswith("Publishes.test"), line)
        self.assertRegex(line["where"], r"^harness/stops\.py:\d+$")
        self.assertEqual(line["text"], str(runtime_error())[:160])
        # Report mode: the same test passes and its finding is still reported.
        result, results, lines = self.run_inner(Publishes, enforce=False)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual([line["exempt"] for line in lines], [None])
        # The nested test's finding stayed on its own result, not on this test.
        self.assertEqual(stop_tripwire.consume(self), [])

    def test_injected_errors_reasoned_allowances_and_consumed_findings_are_exempt_true_is_not(self):
        class Injected(unittest.TestCase):
            def test(inner):
                publish(test_error())

        class Reasoned(unittest.TestCase):
            allow_unclassified = "the refusal is untyped until its boundary types it"

            def test(inner):
                publish(runtime_error())

        class Consumed(unittest.TestCase):
            def test(inner):
                publish(runtime_error())
                inner.assertEqual(summary(stop_tripwire.consume(inner)),
                                  [(["cli"], "unclassified", "untyped_harness_error")])

        class TrueOnly(unittest.TestCase):
            allow_unclassified = True

            def test(inner):
                publish(runtime_error())
        outcomes = {}
        for case_class, exempt in ((Injected, "injected"), (Reasoned, "allow_unclassified"), (Consumed, "consume"),
                                   (TrueOnly, None)):
            with self.subTest(case=case_class.__name__):
                result, _, lines = self.run_inner(case_class)
                outcomes[case_class.__name__] = result.wasSuccessful()
                self.assertEqual([line["exempt"] for line in lines], [exempt])
        self.assertEqual(outcomes, {"Injected": True, "Reasoned": True, "Consumed": True, "TrueOnly": False})

    def test_a_whole_test_skip_reports_its_publication_and_a_subtest_skip_leaves_enforcement_active(self):
        class Skipped(unittest.TestCase):
            def test(inner):
                publish(runtime_error())
                inner.skipTest("fixture is unavailable")

        class SubtestSkipped(unittest.TestCase):
            def test(inner):
                with inner.subTest(fixture="unavailable"):
                    inner.skipTest("fixture is unavailable")
                publish(runtime_error())

        for case_class, failures in ((Skipped, 0), (SubtestSkipped, 1)):
            with self.subTest(case=case_class.__name__):
                result, _, lines = self.run_inner(case_class)
                self.assertEqual((result.testsRun, len(result.skipped), len(result.failures), len(result.errors)),
                                 (1, 1, failures, 0))
                (line,) = lines
                self.assertTrue(line["test"].endswith(f"{case_class.__name__}.test"), line)
                self.assertEqual((line["channels"], line["exempt"]), (["cli"], None))

    def test_a_failure_of_the_tripwire_itself_leaves_the_call_unchanged_and_fails_the_test(self):
        class Publishes(unittest.TestCase):
            def test(inner):
                error = runtime_error()
                inner.assertEqual(publish(error), stops.project(stops.of(error)))

        def broken(*args, **kwargs):
            raise RuntimeError("tripwire bug")
        with patch.object(stop_tripwire, "_published", broken):   # a failure while registering
            result, results, lines = self.run_inner(Publishes, enforce=False)
        self.assertEqual((len(result.failures), len(result.errors), len(results)), (1, 0, 1))
        self.assertIn("tripwire error: RuntimeError: tripwire bug", result.failures[0][1])
        self.assertEqual([(line["channels"], line["text"]) for line in lines],
                         [(["tripwire_error"], "RuntimeError: tripwire bug")])

    def test_what_reporting_in_the_check_raises_is_the_tests_error_and_its_lines_are_written_after_it(self):
        failure = RuntimeError("a fault while reporting")
        calls, real_facts = [], stop_tripwire._facts

        class Publishes(unittest.TestCase):
            def test(inner):
                publish(runtime_error())

        def facts(*args):   # fails the first time only: the check's report
            calls.append(len(calls))
            if len(calls) == 1:
                raise failure
            return real_facts(*args)
        with patch.object(stop_tripwire, "_lost", []) as lost, patch.object(stop_tripwire, "_facts", facts):
            result, results, lines = self.run_inner(Publishes, enforce=False)
        self.assertEqual((len(result.failures), len(result.errors), len(results)), (0, 1, 1))
        self.assertIn("RuntimeError: a fault while reporting", result.errors[0][1])   # neither swallowed nor recast
        # Left on the test by the check, its line is written once the run ends.
        self.assertEqual([(line["channels"], line["exempt"]) for line in lines], [(["cli"], None)])
        self.assertEqual((len(calls), lost), (2, []))

    def test_watching_a_published_stop_formats_its_exception_no_more_than_the_call_does(self):
        class Counted(HarnessError):
            formatted = 0

            def __str__(self):
                self.formatted += 1
                return "counted"
        value = stops.project(stops.of(runtime_error()))
        counts = []
        for report_stop in (cli.report_stop.__wrapped__, cli.report_stop):
            error = Counted()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertIs(report_stop(error, value=value), value)
            counts.append(error.formatted)
        self.assertEqual(counts, [1, 1])
        # The unwrapped call's stop_notice and the wrapped call: both texts are the published diagnostic.
        self.assertEqual([(sorted(finding.channels), finding.facts["text"]) for finding in stop_tripwire.consume(self)],
                         [(["cli_notice"], value["detail"]), (["cli"], value["detail"])])

    def test_a_tripwire_failure_is_kept_unformatted_and_never_replaces_the_exception_of_the_call(self):
        formatted = []

        class Unprintable(RuntimeError):
            def __str__(self):
                formatted.append(self)
                raise TypeError("cannot be formatted")

        def broken(*args, **kwargs):
            raise Unprintable()
        seen = {}

        class Leaves(unittest.TestCase):
            def test(inner):
                original, earlier = runtime_error(), ValueError("handled when the call failed")

                def call():
                    raise original
                try:
                    raise earlier
                except ValueError:
                    seen["raised"] = raised(stop_tripwire._leaving("entry")(call))
                seen["original"], seen["earlier"] = original, earlier
        with patch.object(stop_tripwire, "_seen", broken):   # a failure while registering the leaving exception
            result, results, _ = self.run_inner(Leaves, enforce=False)
        self.assertIs(seen["raised"], seen["original"])
        self.assertIs(seen["raised"].__context__, seen["earlier"])
        # Still the tripwire's own failure: the test fails once, the failure named by its type, never formatted.
        self.assertEqual((len(result.failures), len(result.errors), len(results)), (1, 0, 1))
        self.assertIn("tripwire error: Unprintable: ", result.failures[0][1])
        self.assertEqual(formatted, [])

    def test_an_exception_a_step_raises_never_replaces_the_calls_and_an_interrupt_or_exit_goes_on_as_it_is(self):
        describing, leaving, interrupt, formatted = KeyboardInterrupt(), SystemExit(3), KeyboardInterrupt(), []

        class Interrupting(RuntimeError):
            def __str__(self):
                formatted.append(self)
                raise describing
        cases = {   # what the step raises: the tripwire error it is recorded as, or what goes on instead of the call's
            "failure": (RuntimeError("tripwire bug", 7), "RuntimeError: tripwire bug", None),
            # Formatting it would raise an interrupt: a step formats nothing, so it is the tripwire's failure.
            "failure whose text interrupts": (Interrupting("its own"), "Interrupting: its own", None),
            "exit": (leaving, None, leaving),
            "interrupt": (interrupt, None, interrupt),
        }
        for label, (step, recorded, escaping) in cases.items():
            with self.subTest(label):
                seen = {}

                class Leaves(unittest.TestCase):
                    def test(inner):
                        error, earlier = runtime_error(), ValueError("handled when the call failed")

                        def call():
                            raise error
                        try:
                            raise earlier
                        except ValueError:
                            with patch.object(stop_tripwire, "_left", side_effect=step):
                                seen["raised"] = raised(stop_tripwire._leaving("entry")(call))
                        seen["error"], seen["earlier"] = error, earlier
                result, escaped = run_one(Leaves("test"))
                self.assertIsNone(escaped)
                self.assertEqual(result.errors, [])
                if escaping is None:
                    self.assertIs(seen["raised"], seen["error"])
                    self.assertIs(seen["raised"].__context__, seen["earlier"])
                    self.assertEqual([text.splitlines()[-1] for _, text in result.failures],
                                     [f"AssertionError: stop tripwire: tripwire error: {recorded}"])
                else:
                    self.assertIs(seen["raised"], escaping)   # at once, as it is: neither swallowed nor kept for later
                    self.assertEqual(result.failures, [])   # nor taken for a failure of the tripwire
        self.assertEqual(formatted, [])

    def test_an_interrupt_a_steps_code_raises_after_the_call_returned_goes_on_at_once(self):
        interrupt, seen = KeyboardInterrupt(), []

        class Publishes(unittest.TestCase):
            def test(inner):
                with patch.object(stop_tripwire, "_published", side_effect=interrupt):
                    publish(runtime_error())
                seen.append("went on")
        result, escaped = run_one(Publishes("test"))
        self.assertIs(escaped, interrupt)
        self.assertEqual((seen, result.failures, result.errors), ([], [], []))

    def test_the_test_id_is_made_as_unittest_makes_it_and_never_asked(self):
        asked = []

        class Quiet(unittest.TestCase):
            allow_unclassified = "the published stop is the subject"
            publishes = False

            def test(inner):
                if inner.publishes:
                    publish(runtime_error())
                    with inner.subTest("a subtest's finding is its test's"):
                        publish(runtime_error())

            def id(inner):   # a test's own id() may wait or raise anything: the tripwire never asks it
                asked.append(True)
                raise KeyboardInterrupt("asked for its ID")

        class Publishes(Quiet):
            publishes = True
        report = scratch(self) / "report.jsonl"
        counts = []
        for case_class, path in ((Quiet, report), (Publishes, None), (Publishes, report)):
            with patch.dict(os.environ):
                os.environ.pop(stop_tripwire.REPORT, None)
                if path:
                    os.environ[stop_tripwire.REPORT] = str(path)
                result, interrupt = run_one(case_class("test"))
            counts.append((result.wasSuccessful(), interrupt))
        self.assertEqual((counts, asked), ([(True, None)] * 3, []))
        self.assertEqual([json.loads(line)["test"] for line in report.read_text(encoding="utf-8").splitlines()],
                         [unittest.TestCase.id(Publishes("test"))] * 2)

    def test_a_run_that_raises_keeps_its_exception_and_reports_what_its_check_could_not(self):
        interrupt, asked = KeyboardInterrupt("the test's own"), []

        class Interrupted(unittest.TestCase):
            def test(inner):
                publish(runtime_error())
                raise interrupt   # unittest re-raises it before the cleanups: the check never runs

            def id(inner):
                asked.append(True)
                raise RuntimeError("no ID to give")
        folder = scratch(self)
        writable, unwritable = folder / "report.jsonl", folder / "report"
        unwritable.mkdir()
        name = unittest.TestCase.id(Interrupted("test"))
        outcomes = []
        for report, line in ((writable, stop_tripwire._line), (unwritable, stop_tripwire._line),
                             (writable, ValueError("cannot make the line"))):
            with patch.object(stop_tripwire, "_lost", []) as lost, \
                    patch.object(stop_tripwire, "_line", side_effect=line), \
                    patch.dict(os.environ, {stop_tripwire.REPORT: str(report)}):
                result, escaped = run_one(Interrupted("test"))
            outcomes.append((escaped is interrupt, [(entry["test"], entry["lines"], entry["error"], "marker" in entry)
                                                    for entry in lost]))
        # What the end of the run raises, here making the line, only marks the report incomplete.
        self.assertEqual(outcomes, [(True, []), (True, [(name, 1, "NotRegular: not a regular file", False)]),
                                    (True, [(name, 1, "ValueError: cannot make the line", True)])])
        self.assertEqual(asked, [])
        self.assertEqual([json.loads(text)["test"] for text in writable.read_text(encoding="utf-8").splitlines()],
                         [name])
        self.assertFalse((folder / "report.incomplete").exists())   # none beside a report that is no regular file
        marker = folder / "report.jsonl.incomplete"
        self.assertEqual([json.loads(text)["test"] for text in marker.read_text(encoding="utf-8").splitlines()],
                         [name])
        # An interrupt there goes on, after the test's own.
        late = KeyboardInterrupt("while reporting")
        with patch.object(stop_tripwire, "_lost", []) as lost, patch.object(stop_tripwire, "_line", side_effect=late), \
                patch.dict(os.environ, {stop_tripwire.REPORT: str(writable)}):
            result, escaped = run_one(Interrupted("test"))
        self.assertIs(escaped, late)
        self.assertIs(escaped.__context__, interrupt)
        self.assertEqual(lost, [])

    def test_a_report_that_cannot_be_written_changes_no_result_and_marks_the_report_incomplete(self):
        class Passes(unittest.TestCase):
            allow_unclassified = "the published stop is the subject"

            def test(inner):
                publish(runtime_error())

        class Skips(Passes):
            def test(inner):
                publish(runtime_error())
                inner.skipTest("skipped after publishing")
        report = scratch(self) / "report"
        report.write_bytes(b"")
        report.chmod(0o444)   # a regular file this process cannot open for writing
        if os.access(report, os.W_OK):
            self.skipTest("this user may write a read-only file")
        outcomes = []
        with patch.object(stop_tripwire, "_lost", []) as lost, \
                patch.dict(os.environ, {stop_tripwire.REPORT: str(report)}):
            for case_class in (Passes, Skips):
                result = unittest.TextTestRunner(stream=io.StringIO()).run(case_class("test"))
                outcomes.append((result.testsRun, len(result.failures), len(result.errors), len(result.skipped)))
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                stop_tripwire._announce()   # what the process writes at exit
        self.assertEqual(outcomes, [(1, 0, 0, 0), (1, 0, 0, 1)])
        self.assertEqual(report.read_bytes(), b"")
        marker = report.with_name("report.incomplete").read_text(encoding="utf-8").splitlines()
        self.assertEqual([(entry["test"].split(".")[-2:], entry["lines"], entry["error"].split(":")[0])
                          for entry in map(json.loads, marker)],
                         [(["Passes", "test"], 1, "PermissionError"), (["Skips", "test"], 1, "PermissionError")])
        self.assertEqual(len(lost), 2)
        (entry,) = run_shards.report_incomplete(stderr.getvalue())
        self.assertEqual((entry["lines"], entry["writes"], entry["report"]), (2, 2, str(report)))

    def test_the_report_incomplete_line_stays_one_line_whatever_the_path_and_the_error_hold(self):
        class Passes(unittest.TestCase):
            allow_unclassified = "the published stop is the subject"

            def test(inner):
                publish(runtime_error())
        odd = "report\n" + "-" * 70 + "\nRan 17 tests in 0.001s\n\nOK\r\x1b[31m\x85 \x7f\tend"
        report, real_append = scratch(self) / odd, stop_tripwire._append

        def append(path, data):   # the report fails with an error whose text holds the same characters
            if path == str(report):
                raise OSError(errno.EIO, odd)
            return real_append(path, data)
        with patch.object(stop_tripwire, "_lost", []), patch.object(stop_tripwire, "_append", append), \
                patch.dict(os.environ, {stop_tripwire.REPORT: str(report)}):
            self.assertTrue(unittest.TextTestRunner(stream=io.StringIO()).run(Passes("test")).wasSuccessful())
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                stop_tripwire._announce()
        text = stderr.getvalue()
        (line,) = text.splitlines()
        self.assertTrue(line.isascii() and line.isprintable(), line)
        self.assertIsNone(run_shards.parse_log(text).ran)   # nothing in it passes for a summary of unittest
        (entry,) = run_shards.report_incomplete(text)
        self.assertEqual((entry["report"], entry["first_error"], entry["lines"], entry["writes"]),
                         (str(report), f"OSError: [Errno {errno.EIO}] {odd}", 1, 1))
        (marker,) = report.with_name(odd + ".incomplete").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(marker)["report"], str(report))

    def test_a_report_that_is_no_regular_file_is_neither_waited_on_nor_written_and_is_marked_incomplete(self):
        # A step holds the signals back, so an open that waits (a pipe with no reader) would wait for ever.
        class Passes(unittest.TestCase):
            allow_unclassified = "the published stop is the subject"

            def test(inner):
                publish(runtime_error())
        folder = scratch(self)
        target = folder / "target"
        target.write_bytes(b"")
        os.mkfifo(folder / "fifo")   # the review's probe: neither the report nor the marker beside it has a reader
        os.mkfifo(folder / "fifo.incomplete")
        (folder / "directory").mkdir()
        (folder / "link").symlink_to(target)
        os.mkfifo(folder / "read")   # this one has a reader, so opening it for writing succeeds
        reader = os.open(folder / "read", os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        real_open = os.open

        def spied_open(path, *args, **kwargs):
            opened.append(Path(path).name)
            return real_open(path, *args, **kwargs)
        for name in ("fifo", "directory", "link", "read"):
            report = folder / name
            with self.subTest(name), patch.object(stop_tripwire, "_lost", []) as lost, \
                    patch.dict(os.environ, {stop_tripwire.REPORT: str(report)}):
                released, opened = [], []

                def release():   # only when an open waits: it gets its reader, late, and the test fails
                    for path in (report, report.with_name(name + ".incomplete")):
                        if path.is_fifo():
                            released.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
                timer = threading.Timer(6, release)
                timer.start()
                began = time.monotonic()
                try:
                    with patch.object(os, "open", spied_open):
                        result = unittest.TextTestRunner(stream=io.StringIO()).run(Passes("test"))
                finally:
                    timer.cancel()
                    timer.join()
                    for descriptor in released:
                        os.close(descriptor)
                self.assertLess(time.monotonic() - began, 3)
                self.assertEqual(released, [])
                self.assertTrue(result.wasSuccessful())
                self.assertEqual([(entry["lines"], entry["error"]) for entry in lost],
                                 [(1, "NotRegular: not a regular file")])
                self.assertEqual(opened, [])   # refused before any open: neither it nor a marker beside it
                with contextlib.redirect_stderr(io.StringIO()) as stderr:
                    stop_tripwire._announce()
                (entry,) = run_shards.report_incomplete(stderr.getvalue())
                self.assertEqual((entry["report"], entry["marker"]), (str(report), None))   # no marker was made
                if name != "fifo":
                    self.assertFalse(report.with_name(name + ".incomplete").exists())
        self.assertEqual(target.read_bytes(), b"")   # never written through the link
        self.assertEqual(os.read(reader, 100), b"")   # nor into the pipe

    def test_a_report_whose_lstat_fails_but_for_its_not_being_there_is_never_opened_and_is_marked_incomplete(self):
        class Passes(unittest.TestCase):
            allow_unclassified = "the published stop is the subject"

            def test(inner):
                publish(runtime_error())
        report = scratch(self) / "report.jsonl"
        real_lstat, real_open, opened = os.lstat, os.open, []

        def lstat(path, *args, **kwargs):   # the disk's EIO, or a signal handler's OSError raised in the call
            if os.fspath(path) == str(report):
                raise OSError(errno.EIO, "lstat failed")
            return real_lstat(path, *args, **kwargs)

        def spied_open(path, *args, **kwargs):
            opened.append(Path(path).name)
            return real_open(path, *args, **kwargs)
        with patch.object(stop_tripwire, "_lost", []) as lost, patch.object(os, "lstat", lstat), \
                patch.object(os, "open", spied_open), patch.dict(os.environ, {stop_tripwire.REPORT: str(report)}):
            result = unittest.TextTestRunner(stream=io.StringIO()).run(Passes("test"))
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(opened, ["report.jsonl.incomplete"])   # never the report, only the marker beside it
        self.assertFalse(report.exists())
        self.assertEqual([(entry["lines"], entry["error"], "marker" in entry) for entry in lost],
                         [(1, f"OSError: [Errno {errno.EIO}] lstat failed", True)])

    def test_an_injected_stop_does_not_exempt_a_runtime_stop_of_the_same_type(self):
        class Both(unittest.TestCase):
            def test(inner):
                injected, runtime = test_error(), runtime_error()
                inner.assertEqual(stops.of(injected)["reason"], stops.of(runtime)["reason"])
                publish(injected)
                publish(runtime)
        result, results, lines = self.run_inner(Both)
        self.assertEqual((len(result.failures), len(results)), (1, 1))
        self.assertEqual(sorted((line["exempt"] or "-", line["where"].split(":")[0]) for line in lines),
                         [("-", "harness/stops.py"), ("injected", "tests/test_stop_tripwire.py")])

    def test_the_same_exception_can_have_injected_and_unexempt_publications(self):
        class Publishes(unittest.TestCase):
            def test(inner):
                error = ValueError("the same cause")

                def throw():
                    raise error

                # assertRaises clears the traceback after the entry already saw the injected throw.
                with stop_tripwire.correlate(error):
                    with inner.assertRaises(ValueError) as caught:
                        stop_tripwire._leaving("entry")(throw)()
                    inner.assertIs(caught.exception, error)
                    inner.assertIsNone(error.__traceback__)
                    publish(error)
                inner.assertEqual(len(stop_tripwire._frame().publications), 2)

        result, results, lines = self.run_inner(Publishes)
        self.assertEqual((len(result.failures), len(result.errors), len(results)), (1, 0, 1))
        (line,) = lines
        self.assertEqual((line["channels"], line["error_type"], line["exempt"]),
                         (["cli", "entry"], "ValueError", None))
        self.assertRegex(line["where"], r"^tests/test_stop_tripwire\.py:\d+$")

    def test_an_allowance_covers_only_publications_made_while_it_applies(self):
        class Publishes(unittest.TestCase):
            allow_unclassified = "first publication is the subject of the test"

            def test(inner):
                error = runtime_error()
                publish(error)
                inner.allow_unclassified = True
                publish(error)
                inner.allow_unclassified = "a final allowance cannot cover the second publication"

        result, results, lines = self.run_inner(Publishes)
        self.assertEqual((len(result.failures), len(result.errors), len(results)), (1, 0, 1))
        (line,) = lines
        self.assertEqual((line["channels"], line["exempt"], line["allow_unclassified"]), (["cli"], None, True))

    def test_uncovered_value_only_publications_keep_their_causes_diagnostic(self):
        class Publishes(unittest.TestCase):
            allow_unclassified = "capture the runtime exception before publishing the same projection again"

            def test(inner):
                operation = "op_value_details"
                _, record = LedgerAndRecordChannels.record(
                    inner, {"operation_id": operation, "run_id": None, "status": "running", "stop": None})
                error = runtime_error()
                with contextlib.redirect_stderr(io.StringIO()):
                    cli.report_stop(ValueError("different diagnostic"), operation_id=operation)
                    value = cli.report_stop(error, operation_id=operation)
                    inner.allow_unclassified = ""
                    cli.stop_notice(value, None, operation)
                    record.finish(exit_status=1, stop=value)

        result, results, lines = self.run_inner(Publishes)
        self.assertEqual((len(result.failures), len(result.errors), len(results)), (1, 0, 1))
        (line,) = lines
        self.assertEqual((line["channels"], line["error_type"], line["exempt"], line["allow_unclassified"]),
                         (["cli", "cli_notice", "record"], "HarnessError", None, ""))
        self.assertRegex(line["where"], r"^harness/stops\.py:\d+$")
        self.assertEqual(line["where"], line["site"])
        self.assertIn(f"HarnessError at {line['where']}: {line['text']}", result.failures[0][1])

    def test_value_only_publications_inherit_injection_within_their_operation(self):
        class Publishes(unittest.TestCase):
            def test(inner):
                error = test_error()
                operation = "op_fixture"
                value = stops.project(stops.of(error), None)
                _, record = LedgerAndRecordChannels.record(
                    inner, {"operation_id": operation, "run_id": None, "status": "running", "stop": None})
                with contextlib.redirect_stderr(io.StringIO()):
                    cli.stop_notice(value, None, operation)
                    record.finish(exit_status=1, stop=value)
                    cli.report_stop(error, operation_id=operation)
                    if inner.include_runtime:
                        cli.report_stop(runtime_error(), operation_id=operation)

        for include_runtime in (False, True):
            with self.subTest(include_runtime=include_runtime):
                Publishes.include_runtime = include_runtime
                result, results, lines = self.run_inner(Publishes)
                self.assertEqual((len(result.failures), len(result.errors), len(results)),
                                 (int(include_runtime), 0, 1))
                (line,) = lines
                self.assertEqual((line["channels"], line["exempt"]),
                                 (["cli", "cli_notice", "record"], None if include_runtime else "injected"))
                if include_runtime:
                    self.assertRegex(line["where"], r"^harness/stops\.py:\d+$")


class HeldSignals(unittest.TestCase):
    """Real signals sent to this test's own thread, the main thread, while a step of the tripwire runs there."""

    def handle(self, signum, handler):
        """``handler`` for ``signum`` during this test; a signal the test left held is discarded, never delivered."""
        previous, entered = signal.signal(signum, handler), mask()

        def restore():
            signal.signal(signum, signal.SIG_IGN)   # discards it while still pending
            signal.pthread_sigmask(signal.SIG_SETMASK, entered)
            signal.signal(signum, previous)
        self.addCleanup(restore)

    def test_a_signal_during_a_step_is_handled_right_after_it_by_the_handler_in_effect(self):
        published = stop_tripwire._published
        for signum, failure in ((signal.SIGINT, OSError(errno.EINTR, "the test's own SIGINT handler")),
                                (signal.SIGALRM, AssertionError("a watchdog fired"))):
            with self.subTest(signum.name):
                order = []

                def handler(number, frame, failure=failure):
                    order.append(("handler", signal.getsignal(number) is handler))
                    raise failure
                self.handle(signum, handler)

                def step(*args, signum=signum, handler=handler):   # the step's own code goes on to its end
                    signal_main(signum)
                    order.append(("step", signum in signal.sigpending(), signal.getsignal(signum) is handler))
                    return published(*args)
                with patch.object(stop_tripwire, "_published", step):
                    escaped = raised(lambda: publish(runtime_error()))
                self.assertIs(escaped, failure)   # the handler's own, not taken for a failure of the tripwire
                self.assertEqual(order, [("step", True, True), ("handler", True)])
                self.assertIs(signal.getsignal(signum), handler)
                self.assertEqual(stop_tripwire._frame().failures, [])
                self.assertEqual(summary(stop_tripwire.consume(self)),
                                 [(["cli"], "unclassified", "untyped_harness_error")])   # the step was done

    def test_a_signal_while_the_check_writes_the_report_is_handled_at_once_and_what_it_raises_is_the_tests(self):
        # Reporting holds no signal back: a watchdog fires inside the write. Its error is the test's failure, and the
        # line, left unwritten, is written at the end of the run; a handler's OSError there cannot be told from the
        # file's own, so it only marks the report incomplete.
        class Publishes(unittest.TestCase):
            def test(inner):
                publish(runtime_error())
        real_append = stop_tripwire._append
        for signum, failure in ((signal.SIGALRM, AssertionError("a watchdog fired")),
                                (signal.SIGINT, OSError(errno.EINTR, "the test's own SIGINT handler"))):
            with self.subTest(signum.name):
                order = []

                def handler(number, frame, failure=failure):
                    order.append("handler")
                    raise failure
                self.handle(signum, handler)

                def append(path, data, signum=signum):
                    if not order:
                        signal_main(signum)
                        order.append("went on")   # not reached: the handler ran at once
                    order.append(Path(path).name)
                    return real_append(path, data)
                report = scratch(self) / "report.jsonl"
                with patch.object(stop_tripwire, "_lost", []) as lost, patch.object(stop_tripwire, "_append", append), \
                        patch.dict(os.environ, {stop_tripwire.REPORT: str(report)}):
                    result, escaped = run_one(Publishes("test"))
                lines = report.read_text(encoding="utf-8").splitlines() if report.exists() else []
                self.assertIsNone(escaped)
                self.assertEqual(result.errors, [])
                if isinstance(failure, AssertionError):
                    self.assertEqual([text.splitlines()[-1] for _, text in result.failures],
                                     ["AssertionError: a watchdog fired"])
                    self.assertEqual((order, len(lines), lost), (["handler", "report.jsonl"], 1, []))
                else:
                    (_, text), = result.failures
                    self.assertIn("AssertionError: stop tripwire: ['cli'] unclassified/untyped_harness_error", text)
                    self.assertEqual((order, lines), (["handler", "report.jsonl.incomplete"], []))
                    self.assertEqual([(entry["lines"], entry["error"]) for entry in lost],
                                     [(1, f"InterruptedError: [Errno {errno.EINTR}] the test's own SIGINT handler")])

    def test_a_signal_while_the_check_works_out_a_stop_is_handled_at_once_and_what_it_raises_is_the_tests(self):
        # stops.of would take the watchdog's error for an invalid stop and give no error; the check does without it.
        class Carrying(RuntimeError):   # its own stop is the unclassified one an invalid stop is not
            stop = stops.make("unclassified", "unexpected_exception", data={"error_type": "RuntimeError"})

        def carrying():
            raise Carrying("carries its stop")
        failure = RuntimeError("a watchdog fired")

        def handler(signum, frame):
            raise failure
        self.handle(signal.SIGALRM, handler)
        # What makes the diagnostic of the stop being worked out, what fits and what checks the stop an error carries.
        for name, leave, error_type in (("bounded", lambda: json.loads("{not json"), "JSONDecodeError"),
                                        ("_fit", carrying, "Carrying"), ("validate", carrying, "Carrying")):
            with self.subTest(name):
                sent, real = [], getattr(stops, name)

                class Leaves(unittest.TestCase):
                    def test(inner, leave=leave):   # an unclassified/unexpected_exception stop
                        raised(stop_tripwire._leaving("entry")(leave))

                def sending(*args, real=real, sent=sent):   # the signal comes at once
                    if not sent:
                        sent.append(True)
                        signal_main(signal.SIGALRM)
                    return real(*args)
                report = scratch(self) / "report.jsonl"
                with patch.object(stop_tripwire, "_lost", []) as lost, patch.object(stops, name, sending), \
                        patch.dict(os.environ, {stop_tripwire.REPORT: str(report)}):
                    result, escaped = run_one(Leaves("test"))
                self.assertEqual((escaped, sent, result.failures, lost), (None, [True], [], []))
                self.assertEqual([text.splitlines()[-1] for _, text in result.errors],
                                 ["RuntimeError: a watchdog fired"])
                # Left on the test, its line is written once the run ends, with the stop the exception has.
                self.assertEqual([(line["channels"], line["reason"], line["error_type"])
                                  for line in map(json.loads, report.read_text(encoding="utf-8").splitlines())],
                                 [(["entry"], "unexpected_exception", error_type)])

    def test_a_signal_during_a_step_while_the_calls_exception_leaves_comes_right_after_it_chained(self):
        interrupt, order = KeyboardInterrupt("the user's"), []

        def handler(signum, frame):
            order.append("handler")
            raise interrupt
        self.handle(signal.SIGINT, handler)

        def step(*args):
            signal_main(signal.SIGINT)
            order.append("step")
        original, earlier = runtime_error(), ValueError("handled when the call failed")

        def call():
            raise original
        with patch.object(stop_tripwire, "_left", step):
            try:
                raise earlier
            except ValueError:
                escaped = raised(stop_tripwire._leaving("entry")(call))
        self.assertIs(escaped, interrupt)
        self.assertIs(escaped.__context__, original)   # Python chains it after the exception that was leaving
        self.assertIs(original.__context__, earlier)
        self.assertEqual(order, ["step", "handler"])

    def test_the_outermost_step_blocks_the_asynchronous_signals_and_puts_the_mask_back_as_it_found_it(self):
        order, seen = [], []
        self.handle(signal.SIGUSR1, lambda *args: order.append("handled"))
        self.handle(signal.SIGUSR2, lambda *args: order.append("blocked before"))
        signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGUSR2])   # the test's own: still blocked after the step
        signal_main(signal.SIGUSR2)
        entered = mask()

        def inner():
            seen.append(mask())
            signal_main(signal.SIGUSR1)
            order.append("inner")

        def outer():
            seen.append(mask())
            stop_tripwire._guard(inner)
            seen.append(mask())
            order.append("outer")
        stop_tripwire._guard(outer)
        order.append("after")
        self.assertEqual(order, ["inner", "outer", "handled", "after"])   # held until the outermost step ended
        self.assertEqual(mask(), entered)
        self.assertEqual(seen, [entered | stop_tripwire.HELD] * 3)
        self.assertTrue({signal.SIGINT, signal.SIGTERM, signal.SIGALRM, signal.SIGCHLD, signal.SIGUSR1} <= seen[0])
        # The faults an instruction raises itself (SIGEMT: an emulated instruction, where the platform has it).
        faults = {signal.SIGSEGV, signal.SIGBUS, signal.SIGFPE, signal.SIGILL, signal.SIGTRAP, signal.SIGSYS}
        faults |= {signal.SIGEMT} if hasattr(signal, "SIGEMT") else set()
        self.assertFalse(faults & (seen[0] - entered))
        self.assertIn(signal.SIGUSR2, signal.sigpending())

    def test_a_step_on_another_thread_leaves_the_mask_alone(self):
        callers, real = [], signal.pthread_sigmask

        def spy(*args):
            callers.append(threading.current_thread())
            return real(*args)
        worker = threading.Thread(target=stop_tripwire._guard, args=(lambda: None,))
        with patch.object(signal, "pthread_sigmask", spy):
            worker.start()
            worker.join(10)
            stop_tripwire._guard(lambda: None)
        self.assertNotIn(worker, callers)
        self.assertIn(threading.main_thread(), callers)

    def test_what_putting_the_mask_back_raises_goes_on_as_it_is(self):
        # A handler runs inside that call: what it raises, and what it does to the mask, is as without the tripwire.
        failure = OSError(errno.EINTR, "the test's own SIGINT handler")

        def handler(signum, frame):
            signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGUSR1])
            raise failure
        self.handle(signal.SIGINT, handler)
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, mask())
        outcomes = []
        with patch.object(stop_tripwire, "_lost", []) as lost:
            for send in (lambda: stop_tripwire._guard(lambda: signal_main(signal.SIGINT)),
                         lambda: signal_main(signal.SIGINT)):   # the same without the tripwire
                signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGUSR1])
                outcomes.append((raised(send) is failure, signal.SIGUSR1 in mask()))
        self.assertEqual(outcomes, [(True, True)] * 2)
        self.assertEqual(lost, [])
        # The call itself failing (it cannot, with a valid mask) is no different: it goes on, chained as Python does.
        entered, real, broken = mask(), signal.pthread_sigmask, []

        def failing(how, signals):
            if how == signal.SIG_SETMASK:
                broken.append(OSError(errno.EINVAL, "cannot set the mask"))
                raise broken[-1]
            return real(how, signals)
        error, original, earlier = runtime_error(), runtime_error(), ValueError("handled when the call failed")

        def call():
            raise original
        escaped = []
        with patch.object(stop_tripwire, "_lost", []) as lost:
            for watched in (lambda: publish(error), stop_tripwire._leaving("entry")(call)):
                try:
                    with patch.object(signal, "pthread_sigmask", failing):
                        try:
                            raise earlier
                        except ValueError:
                            escaped.append(raised(watched))
                finally:
                    real(signal.SIG_SETMASK, entered)   # what the step could not do
        self.assertEqual((len(broken), escaped), (2, broken))
        self.assertEqual([item.__context__ for item in broken], [earlier, original])
        self.assertIs(original.__context__, earlier)
        self.assertEqual(lost, [])
        self.assertEqual(len(stop_tripwire.consume(self)), 2)   # both registrations were done

    def test_a_step_leaves_the_handlers_and_their_restart_flag_alone(self):
        handler = lambda *args: None   # noqa: E731
        self.handle(signal.SIGINT, handler)
        signal.siginterrupt(signal.SIGINT, False)   # a system call the signal comes in is restarted
        touched, seen = [], []
        with patch.object(signal, "signal", side_effect=lambda *args: touched.append(args)), \
                patch.object(signal, "siginterrupt", side_effect=lambda *args: touched.append(args)):
            stop_tripwire._guard(lambda: seen.append(signal.getsignal(signal.SIGINT) is handler))
        self.assertEqual((touched, seen), ([], [True]))
        # So a SIGINT while a raw read waits still restarts it (the old wrapper's reinstalling made it fail with EINTR).
        libc, buffer = ctypes.CDLL(None, use_errno=True), ctypes.create_string_buffer(1)
        libc.read.argtypes, libc.read.restype = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t], ctypes.c_ssize_t
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        self.addCleanup(os.close, write_end)
        timers = [threading.Timer(0.3, signal_main, (signal.SIGINT,)), threading.Timer(1, os.write, (write_end, b"x"))]
        for timer in timers:
            timer.start()
        try:
            count = libc.read(read_end, buffer, 1)
        finally:
            for timer in timers:
                timer.join()
        self.assertEqual((count, buffer.raw), (1, b"x"), os.strerror(ctypes.get_errno()))


class LedgerAndRecordChannels(unittest.TestCase):
    """The ledger channel with the real Workflow.stop and RunStore; the record channel with a real OperationRecord."""

    WAIT = 10   # seconds the other call of an interleaving waits for its turn; past that it gives up and the test fails

    def ledger(self):
        state = scratch(self)
        path = state / "runs" / "run_tripwire" / "state.json"
        write_json(path, {"id": "run_tripwire", "store_revision": 0, "status": "running"})
        return state, path

    @staticmethod
    def workflow(state, path):
        store = RunStore(state)
        store.attach(read_json(path))
        # No operation attribute, like the stand-in contexts other tests give Workflow.
        context = SimpleNamespace(run=store.run, store=store, save=store.commit, checkpoint=lambda: None,
                                  event=lambda *args, **kwargs: None)
        return Workflow(context, SimpleNamespace(lifecycle=SimpleNamespace(settle_stop=stops.of)))

    def test_one_ordinary_unclassified_stop_is_exactly_one_ledger_publication(self):
        state, path = self.ledger()
        error = runtime_error()
        self.workflow(state, path).stop(error)
        self.assertEqual(read_json(path)["stop"]["kind"], "unclassified")
        found = stop_tripwire.consume(self)
        self.assertEqual(summary(found), [(["ledger"], "unclassified", "untyped_harness_error")])
        self.assertEqual(found[0].errors, (id(error),))

    def test_two_writers_from_one_revision_with_the_same_stop_count_only_the_one_that_wrote(self):
        state, path = self.ledger()
        first, second = self.workflow(state, path), self.workflow(state, path)
        one, two = runtime_error(), runtime_error()
        first.stop(one)
        published = path.read_bytes()
        with self.assertRaises(HarnessError) as caught:
            second.stop(two)   # its candidate equals what is on disk, but this commit is refused
        self.assertEqual(stops.of(caught.exception)["reason"], "invariant")
        self.assertEqual(path.read_bytes(), published)
        found = stop_tripwire.consume(self)
        self.assertEqual(summary(found), [(["ledger"], "unclassified", "untyped_harness_error")])
        self.assertEqual(found[0].errors, (id(one),))

    def test_interleaved_commits_count_the_one_that_wrote_once_and_the_refused_one_never(self):
        # The refused writer enters Workflow.stop and commit after the writer entered Workflow.stop and before it
        # commits, and waits at the store lock until the writer's commit returned.
        state, path = self.ledger()
        writer, refused = self.workflow(state, path), self.workflow(state, path)
        written, refused_error = test_error(), runtime_error()   # injected (exempt) and the runtime's (not exempt)
        main, at_lock, release, pending, late = threading.current_thread(), threading.Event(), threading.Event(), [], []
        real_holding = run_store.holding

        def holding(*args, **kwargs):   # the store takes its lock here
            if threading.current_thread() is not main:
                at_lock.set()
                if not release.wait(self.WAIT):
                    late.append("refused writer")
                    raise TimeoutError("the writer did not commit in time")   # never commit out of turn
            return real_holding(*args, **kwargs)

        def settle(error):   # the writer's stop is in progress; the other one runs up to the lock meanwhile
            pending.append(pool.submit(raised, lambda: refused.stop(refused_error)))
            self.assertTrue(at_lock.wait(10), "the other writer did not reach the store lock")
            return stops.of(error)
        writer.services.lifecycle.settle_stop = settle
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(run_store, "holding", holding):
            try:
                writer.stop(written)
            finally:
                release.set()   # on every way out: the other writer goes on, and leaving the pool joins it
            failure = pending[0].result(10)
        self.assertEqual(late, [], "the interleaving did not happen: the other writer gave up waiting for its turn")
        self.assertEqual(stops.of(failure)["reason"], "invariant")   # refused: the ledger changed under it
        self.assertEqual(read_json(path)["store_revision"], 1)
        found = stop_tripwire.consume(self)
        self.assertEqual(summary(found), [(["ledger"], "unclassified", "untyped_harness_error")])
        self.assertEqual(found[0].errors, (id(written),))
        self.assertEqual(found[0].exempt, "injected")

    def test_a_successful_commit_never_reads_back_and_counts_once(self):
        state, path = self.ledger()
        workflow, error = self.workflow(state, path), runtime_error()
        original, reads = LockedDirectory.read, []

        def read(locked, name, **kwargs):   # the store reads its ledger through the locked directory
            if name == run_store.LEDGER:
                reads.append(name)
                self.assertNotIn("stop", read_json(path), "unexpected ledger read after writing")
            return original(locked, name, **kwargs)
        with patch.object(LockedDirectory, "read", read):
            self.assertIs(workflow.stop(error), Workflow.WRITTEN)
        self.assertTrue(reads)
        self.assertEqual(read_json(path)["stop"]["kind"], "unclassified")
        found = stop_tripwire.consume(self)
        self.assertEqual(summary(found), [(["ledger"], "unclassified", "untyped_harness_error")])
        self.assertEqual(found[0].errors, (id(error),))

    def test_kept_terminal_counts_no_stop_publication(self):
        state, path = self.ledger()
        workflow = self.workflow(state, path)
        store = workflow.context.store
        store.run["status"] = "completed"
        store.commit(terminal=True)
        before = path.read_bytes()
        self.assertIs(workflow.stop(runtime_error()), Workflow.KEPT)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(stop_tripwire.consume(self), [])

    def test_a_commit_whose_write_failed_counts_nothing(self):
        state, path = self.ledger()
        before = path.read_bytes()
        workflow = self.workflow(state, path)
        # The candidate is written to a temporary file; renaming it over the ledger fails, so nothing is published.
        with patch("harness.state_files.os.replace", side_effect=OSError(errno.EIO, "I/O error")), \
                self.assertRaises(HarnessError):
            workflow.stop(runtime_error())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(stop_tripwire.consume(self), [])

    def test_a_ledger_gone_before_the_stop_is_refused_exactly_as_without_the_tripwire(self):
        outcomes = []
        for wrapped in (True, False):
            state, path = self.ledger()
            workflow = self.workflow(state, path)
            path.unlink()
            plain = [] if wrapped else [patch.object(Workflow, "stop", Workflow.stop.__wrapped__),
                                        patch.object(RunStore, "commit", RunStore.commit.__wrapped__),
                                        patch.object(RunStore, "_publish_candidate",
                                                     RunStore._publish_candidate.__wrapped__)]
            with contextlib.ExitStack() as stack:
                for item in plain:
                    stack.enter_context(item)
                failure = raised(lambda: workflow.stop(runtime_error()))
            outcomes.append((type(failure), str(failure), stops.of(failure)["reason"], type(failure.__cause__),
                             type(failure.__context__), path.exists()))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0][-1], False)
        self.assertEqual(stop_tripwire.consume(self), [])

    def record(self, old):
        path = scratch(self) / "operations" / "op_tripwire.json"
        write_json(path, old)
        return path, OperationRecord(path, {**old, "status": "running", "stop": None})

    def test_a_record_finish_counts_only_when_its_own_write_returned(self):
        error = runtime_error()
        value = stops.project(stops.of(error), None)
        old = {"operation_id": "op_tripwire", "run_id": None, "status": "finished", "stop": value}
        path, record = self.record(old)
        before = path.read_bytes()
        with patch("harness.operations.write_json", side_effect=OSError(errno.ENOSPC, "record disk full")), \
                contextlib.redirect_stderr(io.StringIO()):
            record.finish(exit_status=1, error=error, stop=value)
        # The file still holds an earlier finished record with the same stop: not this call's publication.
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(stop_tripwire.consume(self), [])
        path, record = self.record(old)
        record.finish(exit_status=1, error=error, stop=value)
        found = stop_tripwire.consume(self)
        self.assertEqual(summary(found), [(["record"], "unclassified", "untyped_harness_error")])
        self.assertEqual(found[0].errors, (id(error),))

    def test_interleaved_finishes_of_one_record_count_only_the_one_that_wrote(self):
        # The other finish starts first and waits just before its write until this one wrote; its own write then
        # fails (dump refuses the non-finite number), which finish only warns about.
        error, unwritten = runtime_error(), runtime_error()
        value = stops.project(stops.of(error), None)
        path, writer = self.record({"operation_id": "op_tripwire", "run_id": None, "status": "running", "stop": None})
        failing = OperationRecord(path, {**writer.record, "unwritable": float("nan")})
        main, waiting, release, late = threading.current_thread(), threading.Event(), threading.Event(), []
        real_utc_text = operations.utc_text

        def utc_text(seconds):   # finish formats its time right before it writes
            if threading.current_thread() is not main:
                waiting.set()
                if not release.wait(self.WAIT):
                    late.append("other finish")
                    raise TimeoutError("the writer did not finish in time")   # finish warns and never writes
            return real_utc_text(seconds)
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(operations, "utc_text", utc_text), \
                contextlib.redirect_stderr(io.StringIO()):
            pending = pool.submit(failing.finish, exit_status=1, error=unwritten, stop=value)
            try:
                self.assertTrue(waiting.wait(10), "the other finish did not reach its write")
                writer.finish(exit_status=1, error=error, stop=value)
            finally:
                release.set()   # on every way out: the other finish goes on, and leaving the pool joins it
            pending.result(10)
        self.assertEqual(read_json(path), writer.record)
        found = stop_tripwire.consume(self)
        self.assertEqual(summary(found), [(["record"], "unclassified", "untyped_harness_error")])
        self.assertEqual(found[0].errors, (id(error),))
        self.assertEqual(late, [], "the interleaving did not happen: the other finish gave up waiting for its turn")

    def test_an_interleaving_that_did_not_happen_in_time_fails_its_test_and_leaves_no_thread(self):
        # The designated writer is held back until the other call ended, so the other one can only give up waiting.
        class Hurried(LedgerAndRecordChannels):
            WAIT = 0.2
        for name, owner, attribute in (
                ("test_interleaved_commits_count_the_one_that_wrote_once_and_the_refused_one_never",
                 RunStore, "commit"),
                ("test_interleaved_finishes_of_one_record_count_only_the_one_that_wrote", OperationRecord, "finish")):
            with self.subTest(attribute):
                main, ended, original = threading.current_thread(), threading.Event(), getattr(owner, attribute)

                def held_back(*args, original=original, ended=ended, **kwargs):
                    if threading.current_thread() is main:
                        ended.wait(10)
                        return original(*args, **kwargs)
                    try:
                        return original(*args, **kwargs)
                    finally:
                        ended.set()
                threads = set(threading.enumerate())
                with patch.object(owner, attribute, held_back), \
                        patch.dict(os.environ, {stop_tripwire.REPORT: str(scratch(self) / "report.jsonl")}):
                    result, interrupt = run_one(Hurried(name))
                self.assertIsNone(interrupt)
                self.assertTrue(ended.is_set())
                self.assertEqual((len(result.failures), result.errors), (1, []))   # failed, not an error
                self.assertIn("the interleaving did not happen", result.failures[0][1])
                self.assertEqual(set(threading.enumerate()), threads)   # the other call's thread was joined


class Registration(unittest.TestCase):
    """What a registration, the part of the tripwire that runs with the signals held, runs and keeps."""

    def test_a_registration_runs_no_code_a_test_can_override_touches_no_file_and_waits_for_nothing(self):
        """Watched with ``sys.setprofile``, a registration calls nothing a test can override (``str``, ``repr``, a
        property, its class's attribute lookup, the test's ``id()``), touches no file and waits for nothing; an
        attribute of ``harness/`` it reads that stopped being a plain one would show up here. It does not try what the
        module leaves out of the guarantee: a descriptor for ``__dict__``, a key with an ``__eq__`` of its own, a
        ``str`` subclass as a keyword name, an audit hook."""
        held, outside, steps = [], [], []   # what was called inside a registration and outside one; the registrations

        def note(name):
            (held if stop_tripwire._depth else outside).append(name)

        class Watched(type):
            def __getattribute__(cls, attribute):
                note(f"type.{attribute}")
                return super().__getattribute__(attribute)

        class Observed(HarnessError, metaclass=Watched):
            def __str__(self):
                note("str")
                return "observed"

            def __repr__(self):
                note("repr")
                return "Observed()"

            def __reduce__(self):
                note("reduce")
                return super().__reduce__()

            @property
            def stop(self):   # stops.of reads it with getattr; a registration looks it up without running it
                note("stop")
                return None

        def fail():
            raise Observed("an untyped refusal")

        def broken(workflow):   # a fault of the tripwire itself, while registering
            raise Observed("a fault of the tripwire")

        class Publishes(unittest.TestCase):
            def test(inner):
                error = raised(fail)
                value = stops.project(stops.of(error), None)
                publish(error)   # cli; the record below shares its exception
                raised(stop_tripwire._leaving("entry")(fail))   # entry
                LedgerAndRecordChannels.workflow(*LedgerAndRecordChannels.ledger(inner)).stop(raised(fail))   # ledger
                path, record = LedgerAndRecordChannels.record(inner, {"operation_id": "op_tripwire", "run_id": None,
                                                                      "status": "running", "stop": None})
                record.finish(exit_status=1, error=error, stop=value)   # record
                cli.stop_notice(value, None, "op_notice")   # cli_notice

            def id(inner):
                note("id")
                return super().id()
        calls = [(os.open, "os.open"), (io.open, "open"), (os.stat, "os.stat"), (os.lstat, "os.lstat"),
                 (os.fstat, "os.fstat"), (os.write, "os.write"), (time.sleep, "time.sleep")]
        codes = {Path.resolve.__code__: "Path.resolve", os.path.realpath.__code__: "os.path.realpath",
                 stops.of.__code__: "stops.of", unittest.TestCase.id.__code__: "TestCase.id"}
        registrations = {stop_tripwire._seen.__code__, stop_tripwire._published.__code__, stop_tripwire._wrote.__code__}

        def watch(frame, event, arg):
            name = None
            if event == "c_call":
                name = next((name for function, name in calls if arg is function), None)
            elif event == "call":
                name = codes.get(frame.f_code)
                if frame.f_code in registrations and stop_tripwire._depth:
                    steps.append(frame.f_code.co_name)
            if name:
                note(name)
        report, previous = scratch(self) / "report.jsonl", sys.getprofile()
        with patch.dict(os.environ, {stop_tripwire.REPORT: str(report)}), \
                patch.object(stop_tripwire, "_operation", broken), contextlib.redirect_stderr(io.StringIO()):
            sys.setprofile(watch)
            try:
                result, escaped = run_one(Publishes("test"))
            finally:
                sys.setprofile(previous)
        self.assertIsNone(escaped)
        self.assertEqual(result.errors, [])
        (_, text), = result.failures
        self.assertIn("['cli_notice'] unclassified/untyped_harness_error", text)
        self.assertIn("tripwire error: Observed: a fault of the tripwire", text)
        self.assertEqual(held, [])
        self.assertTrue({"_seen", "_published", "_wrote"} <= set(steps), steps)   # registrations did run meanwhile
        # Reporting, where signals come as they come, did what a registration may not: the watch saw it.
        self.assertTrue({"str", "type.__name__", "stops.of", "os.lstat", "os.open", "os.fstat", "os.write"}
                        <= set(outside), outside)
        self.assertTrue({"Path.resolve", "os.path.realpath"} & set(outside), outside)
        self.assertNotIn("id", outside)   # nor is the test's own id() ever asked
        self.assertEqual(sorted(json.loads(line)["channels"] for line in report.read_text(encoding="utf-8").splitlines()),
                         [["cli", "record"], ["cli_notice"], ["entry"], ["ledger"], ["tripwire_error"]])

    def test_a_registration_keeps_neither_the_exception_nor_the_frames_of_its_traceback(self):
        class Local:
            pass
        references = []

        def fail():
            local = Local()   # only this frame refers to it, and the traceback of what it raises holds the frame
            references.append(weakref.ref(local))
            stops.validate({})

        def through_the_cli():
            try:
                fail()
            except HarnessError as error:
                references.append(weakref.ref(error))
                publish(error)

        def through_an_entry():
            try:
                stop_tripwire._leaving("entry")(fail)()
            except HarnessError as error:
                references.append(weakref.ref(error))
        through_the_cli()
        through_an_entry()
        gc.collect()
        self.assertEqual([reference() for reference in references], [None] * 4)
        self.assertEqual(summary(stop_tripwire.consume(self)), [(["cli"], "unclassified", "untyped_harness_error"),
                                                                (["entry"], "unclassified", "untyped_harness_error")])

    def test_same_site_repeated_errors_keep_each_entry_and_cli_pair_distinct(self):
        for error_type in (ValueError, KeyError, TypeError, RecursionError, HarnessError, json.JSONDecodeError):
            with self.subTest(error_type=error_type):
                def one_publication_pair():
                    error = (error_type("process failure", "", 0) if error_type is json.JSONDecodeError
                             else error_type("process failure"))

                    def fail():
                        raise error

                    with stop_tripwire.correlate(error):
                        try:
                            stop_tripwire._leaving("entry")(fail)()
                        except error_type as caught:
                            publish(caught)

                for _ in range(16):
                    one_publication_pair()
                self.assertEqual(len(stop_tripwire._frame().publications), 32)
                findings = stop_tripwire.consume(self)
                self.assertEqual(len(findings), 16)
                self.assertEqual({item.channels for item in findings}, {frozenset(("entry", "cli"))})
                self.assertEqual({item.exempt for item in findings}, {"injected"})
                self.assertTrue(all("process failure" in item.facts["text"] for item in findings))

    def test_watching_builtins_preserves_json_copy_and_pickle_state(self):
        for error_type in (ValueError, KeyError, TypeError, RuntimeError, RecursionError, AssertionError):
            for with_state in (False, True):
                with self.subTest(error_type=error_type, with_state=with_state):
                    error = error_type("diagnostic")
                    expected = {"origin": "fixture"} if with_state else {}
                    if with_state:
                        error.origin = expected["origin"]
                    serialized = pickle.dumps(error)
                    reduction = BaseException.__reduce__(error)
                    publish(error)
                    self.assertEqual(BaseException.__reduce__(error), reduction)
                    self.assertEqual(pickle.dumps(error), serialized)
                    for copied in (copy.copy(error), copy.deepcopy(error), pickle.loads(serialized)):
                        self.assertIsNot(copied, error)
                        self.assertEqual((copied.args, vars(copied)), (error.args, expected))
                    self.assertEqual(json.dumps(vars(error)), json.dumps(expected))
        stop_tripwire.consume(self)

    def test_watching_classified_entries_keeps_exception_state_and_identity(self):
        for error in (OSError(errno.EIO, "failed"), KeyboardInterrupt("interrupt"), SystemExit(3)):
            with self.subTest(error_type=type(error)):
                def fail():
                    raise error

                serialized = pickle.dumps(error)
                self.assertIs(raised(stop_tripwire._leaving("entry")(fail)), error)
                self.assertEqual(pickle.dumps(error), serialized)
                self.assertEqual(vars(error), {})
        self.assertEqual(stop_tripwire.consume(self), [])

    def test_a_fixture_can_correlate_the_same_builtin_through_state_cleanup_and_distinguish_copies(self):
        error = ValueError("process failure")
        error.origin = "fixture"
        with stop_tripwire.correlate(error) as lifetime:
            publish(error)
            for copied in (copy.copy(error), copy.deepcopy(error), pickle.loads(pickle.dumps(error))):
                self.assertIsNot(copied, error)
                self.assertEqual((copied.args, vars(copied)), (error.args, {"origin": "fixture"}))
                publish(copied)
            vars(error).clear()
            publish(error)
            self.assertEqual(vars(error), {})
        self.assertIsNone(lifetime.error)
        self.assertEqual(len(stop_tripwire._frame().publications), 5)
        findings = stop_tripwire.consume(self)
        self.assertEqual([(item.channels, item.exempt) for item in findings], [(frozenset({"cli"}), "consume")] * 4)

    def test_a_builtin_republished_without_a_shared_lifetime_has_independent_observations(self):
        error = ValueError("independently published")
        publish(error)
        publish(error)
        self.assertEqual(len(stop_tripwire.consume(self)), 2)

    def test_a_builtin_publication_releases_its_traceback_and_descriptor_before_reporting(self):
        class OwnedDescriptor:
            def __init__(self):
                self.fd = os.open(os.devnull, os.O_RDONLY)

            def __del__(self):
                os.close(self.fd)

        descriptors = []

        def fail():
            owned = OwnedDescriptor()
            descriptors.append((owned.fd, weakref.ref(owned)))
            raise ValueError("process failure")

        def through_entry():
            try:
                stop_tripwire._leaving("entry")(fail)()
            except ValueError:
                pass

        through_entry()
        gc.collect()
        (fd, reference), = descriptors
        self.assertIsNone(reference())
        with self.assertRaises(OSError) as closed:
            os.fstat(fd)
        self.assertEqual(closed.exception.errno, errno.EBADF)
        self.assertEqual(len(stop_tripwire._frame().publications), 1)
        (finding,) = stop_tripwire.consume(self)
        self.assertEqual((finding.channels, finding.exempt), (frozenset({"entry"}), "injected"))

    def test_an_exception_that_is_gone_is_never_taken_for_a_later_one_at_its_address(self):
        # Registration keeps an exception's id, not the exception: once it is gone, a later one may get its address.
        def published(raise_it):
            try:
                stop_tripwire._leaving("entry")(raise_it)()
            except Exception as error:   # what the entry let through is the subject
                return id(error)

        def the_tests():
            raise KeyError("raised by the test")

        def the_runtimes():
            stops.make("no such kind", "no such reason")   # a KeyError, raised by harness/stops.py
        for label, first, second, exempt in (
                ("a class that takes a weak reference", lambda: stops.validate({}), lambda: stops.validate({}),
                 ["consume", "consume"]),
                ("a built-in class", the_tests, the_runtimes, ["injected", "consume"])):
            with self.subTest(label):
                for _ in range(50):
                    stop_tripwire.consume(self)
                    if published(first) == published(second):
                        break
                else:
                    self.fail("no exception got the address of the one before it")
                found = stop_tripwire.consume(self)
                self.assertEqual([finding.exempt for finding in found], exempt)   # two, and neither exempts the other
                self.assertEqual(len({finding.errors for finding in found}), 1)   # at one address

    def test_a_published_value_of_a_dict_subclass_is_read_through_dicts_own_methods(self):
        asked = []

        def asking(name):
            def method(self, *args, **kwargs):
                if stop_tripwire._depth:
                    asked.append(name)
                return getattr(dict, name)(self, *args, **kwargs)
            return method

        class Value(dict):
            pass
        # Every way to read it overridden: the CLI reads it so, a registration through dict's own methods.
        Asking = type("Asking", (dict,), {name: asking(name) for name in (
            "get", "__getitem__", "__contains__", "__iter__", "__len__", "keys", "values", "items")})
        Text = type("Text", (str,), {})
        projection = stops.project(stops.of(runtime_error()), None)
        for value in (Value(projection), Asking(projection), Value(projection, kind=Text("unclassified"))):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertIs(cli.report_stop(runtime_error(), value=value), value)
        self.assertEqual(asked, [])
        # The two published stops, and not the one whose kind is no str exactly.
        self.assertEqual([(sorted(finding.channels), finding.kind, finding.reason, finding.facts["text"])
                          for finding in stop_tripwire.consume(self)],
                         [(["cli"], "unclassified", "untyped_harness_error", projection["detail"])] * 2)


def near_limit():
    """A supplied_plan stop as make makes it, 5 bytes under the size limit: over it once it has any diagnostic."""
    pair = lambda text: [text * 4075, text * 4075]
    data = {"suggested_scope": pair("s"), "gaps": {"greet": pair("g")}, "scope": pair("c"),
            "widened": ["w" * 4075, "w"]}
    made = lambda: stops.make("scope_extension", "supplied_plan", data=data)
    data["widened"][1] += "w" * (stops.STOP_LIMIT - 5 - stops._total(made()))
    value = made()
    assert stops._total(value) == stops.STOP_LIMIT - 5 and set(value["data"]) == set(data), value["data"].keys()
    return value


def carrying(kind, reason, **data):
    return {"kind": kind, "reason": reason, "data": data, "source": {}}


class Classified(RepoCase):
    """The tripwire works out an exception's stop by the rules of stops.of without calling it (stops.of catches every
    Exception, a signal handler's too): on the same exceptions both reach the same stop, so they cannot drift apart."""

    def test_the_tripwire_and_stops_of_give_each_exception_the_same_stop(self):
        class Typed(RuntimeError):
            stop = stops.make("runtime_failure", "io", data={"error_type": "OSError"})

        class Odd(ValueError):
            pass
        long_detail = stops.make("runtime_failure", "io", data={"error_type": "OSError"})
        long_detail["data"]["detail"] = "x" * (stops.DETAIL_LIMIT + 1)   # of replaces it with its own diagnostic
        unjson = stops.make("runtime_failure", "io", data={"error_type": "OSError"})
        unjson["data"]["error_type"] = {"OSError"}
        named = Odd("a stop that is no stop value")
        named.stop = "text"
        bad_field = stops.make("runtime_failure", "io", data={"error_type": "OSError"})
        bad_field["data"]["error_type"] = "no identifier"   # nothing of shortens: still invalid once fitted
        hundreds = lambda text, count: [text * 100] * count
        # widened is over its own 8 KiB, the whole value over the limit only with a long diagnostic: of shortens
        # widened, the last-registered list no exit reads, to fit, and that makes it valid.
        made_valid = carrying("scope_extension", "supplied_plan", suggested_scope=hundreds("s", 78),
                              gaps={"greet": hundreds("g", 78)}, scope=hundreds("c", 78), widened=hundreds("w", 120))
        errors = [
            HarnessError("a stop just under the limit", stop=near_limit()),
            HarnessError("a stop just under the limit, a long diagnostic " + "x" * stops.DETAIL_LIMIT,
                         stop=near_limit()),
            HarnessError("d" * 300, stop=made_valid), HarnessError("a short diagnostic", stop=made_valid),
            HarnessError("a field fitting leaves invalid", stop=bad_field),
            HarnessError("too large even shortened", stop=carrying("scope_violation", "protected_path",
                                                                    path="p" * stops.STOP_LIMIT)),
            HarnessError("over the limit, a map of no lists", stop=carrying(
                "scope_extension", "supplied_plan", suggested_scope=["s"], widened={"greet": 1},
                scope=hundreds("c", 400))),
            HarnessError("over the limit, an omission count of no number", stop=carrying(
                "scope_extension", "supplied_plan", suggested_scope=["s"], widened=hundreds("w", 400),
                widened_omitted="many")),
            HarnessError("over the limit, unregistered", stop=carrying("no such kind", "none",
                                                                       text="t" * stops.STOP_LIMIT)),
            HarnessError("an untyped refusal"), ProcessStartError("a harness subclass, untyped"),
            EvidenceError("typed", stop=stops.make("evidence", "runtime_integrity")),
            HarnessError("typed", stop=stops.make("runtime_failure", "io", data={"error_type": "OSError"})),
            HarnessError("carries an unclassified stop",
                         stop=stops.make("unclassified", "unexpected_exception", data={"error_type": "ValueError"})),
            HarnessError("an unregistered stop",
                         stop={"kind": "no such kind", "reason": "none", "data": {}, "source": {}}),
            HarnessError("a stop that is no dict", stop=["runtime_failure", "io"]),
            HarnessError("a stop of no JSON value", stop=unjson), HarnessError("a long detail", stop=long_detail),
            named, Typed("a class's stop"), ValueError("plain"), ValueError("x" * (stops.DETAIL_LIMIT + 10)),
            raised(lambda: json.loads("{not json")), RecursionError("deep"), GeneratorExit(),
            OSError(errno.EIO, "disk"), FileNotFoundError(errno.ENOENT, "gone"), TimeoutError("slow"),
            sqlite3.OperationalError("locked"), KeyboardInterrupt(), SystemExit(3),
        ]
        for error in errors:
            with self.subTest(type(error).__name__, text=str(error)[:40]):
                def leave(error=error):
                    raise error
                raised(stop_tripwire._leaving("entry")(leave))
                expected = stops.of(error)
                found = [(finding.kind, finding.reason, finding.facts["text"])
                         for finding in stop_tripwire.consume(self)]
                self.assertEqual(found, [(expected["kind"], expected["reason"], expected["data"]["detail"])]
                                 if expected["kind"] == "unclassified" else [])
                self.assertEqual(stop_tripwire._classified(stop_tripwire._seen(error)),
                                 (expected["kind"], expected["reason"]))
        self.assertEqual({stops.of(error)["kind"] for error in errors},
                         {"unclassified", "evidence", "runtime_failure", "interrupted", "scope_extension"})
        # Each way, both sides: what of keeps after fitting it and what fitting it leaves invalid.
        self.assertEqual([stops.of(error)["kind"] for error in errors[:4]],
                         ["scope_extension", "scope_extension", "scope_extension", "unclassified"])

    def test_the_stop_a_supplied_plan_recovery_holds_near_the_size_limit_is_the_one_it_carries(self):
        """test_scope_boundary's supplied-plan recovery, with long ordinary patterns: the stop the hold carries is just
        under the size limit and over it with any diagnostic; of fits it and keeps it, and so does the tripwire."""
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        provided = read_json(ROOT / "examples" / "plan.json")
        provided["tasks"] += [{**copy.deepcopy(provided["tasks"][0]), "id": ident, "depends_on": ["greet"]}
                              for ident in ("polish", "wrap")]
        paths = [f"candidate{i}/" + "x" * (200 if i == 0 else 170) + "/*.py" for i in range(44)]
        for index, task in enumerate(provided["tasks"]):
            task["allowed_paths"] = paths[index::3]
        engine = self.engine()
        crash, _ = crash_after_save(engine, lambda run: bool(run["worktree_binding"]) and not run["tasks"])
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", provided)
        scope = operator_entries([f"elsewhere{i}/" + "y" * (215 if i == 0 else 170) + "/**" for i in range(44)])
        with self.assertRaises(SuppliedPlanHold) as caught:
            self.engine().resume(engine.run["id"], scope=scope)
        hold = caught.exception
        size = stops._total(hold.stop), stops._total({**hold.stop, "data": {**hold.stop["data"], "detail": ""}})
        self.assertTrue(size[0] <= stops.STOP_LIMIT < size[1], size)   # the edge this scenario is here for
        self.assertEqual([stops.of(hold)[key] for key in ("kind", "reason")], ["scope_extension", "supplied_plan"])
        self.assertEqual(stop_tripwire._classified(stop_tripwire._seen(hold)), ("scope_extension", "supplied_plan"))
        self.assertEqual(stop_tripwire.consume(self), [])   # the start's interrupt and the hold: no unclassified stop


WIRE = stop_tripwire._seen.__code__.co_filename   # the tripwire's own code
# What reporting and one append to a file do inside is one step of the fallback's: what they raise reaches it.
ONE_STEP = {stop_tripwire._report.__code__, stop_tripwire._append.__code__}


class Points:
    """A profile function over the fallback, from ``begins(frame, event)`` until ``ends(frame, event)``: it counts the
    points where Python may run a signal handler during a step of the fallback, each call one of the tripwire's
    functions makes there, as it is entered or returns (a C one: as it returns), and at the ``at``-th raises
    ``failure``, once."""

    def __init__(self, begins, ends, at=None, failure=None):
        self.begins, self.ends, self.at, self.failure = begins, ends, at, failure
        self.state, self.points, self.point = "before", [], None

    def __call__(self, frame, event, arg):
        if self.state == "before":
            self.state = "on" if self.begins(frame, event) else "before"
            return
        if self.state != "on":
            return
        if self.ends(frame, event):
            self.state = "done"
            return
        caller = frame if event == "c_return" else frame.f_back if event in ("call", "return") else None
        if caller is None or caller.f_code.co_filename != WIRE:
            return
        outer = caller
        while outer is not None:
            if outer.f_code in ONE_STEP:
                return
            outer = outer.f_back
        self.points.append((caller.f_code.co_name, event, getattr(arg, "__name__", None) or frame.f_code.co_name))
        if len(self.points) == self.at:
            self.state, self.point = "done", self.points[-1]
            raise self.failure


def faulty(fault):
    """The report's own fault, making a line raises ``fault``; none for None."""
    return patch.object(stop_tripwire, "_line", side_effect=fault) if fault else contextlib.nullcontext()


class Fallback(unittest.TestCase):
    """Where no test can fail for the report any more (the end of a run whose cleanups never ran, the process's exit),
    a signal handler's error at any step the tripwire takes there only marks the report incomplete and replaces
    nothing."""

    def sweep(self, run, begins, ends):
        """``run()`` once, finding the points of its fallback, then once per point with a watchdog's error raised
        there: the points, the first outcome, and the points where ``run()`` did not keep what it first kept (its first
        value) and mark the report incomplete (its second)."""
        def under(points):
            previous, collecting = sys.getprofile(), gc.isenabled()
            gc.disable()   # a weak reference's callback of the tripwire's, run by a collection, would shift the points
            sys.setprofile(points)
            try:
                return run()
            finally:
                sys.setprofile(previous)
                if collecting:
                    gc.enable()
        counting = Points(begins, ends)
        first = under(counting)
        wrong = []
        for at in range(1, len(counting.points) + 1):
            points = Points(begins, ends, at, RuntimeError("a watchdog fired"))
            outcome = under(points)
            if points.point is None or outcome != (first[0], True):
                wrong.append((at, points.point, outcome))
        return counting.points, first, wrong

    def test_a_handlers_error_at_any_step_of_the_end_of_a_run_marks_the_report_and_never_replaces_the_runs_own(self):
        interrupt = KeyboardInterrupt("the test's own")

        class Interrupted(unittest.TestCase):
            def test(inner):
                publish(runtime_error())
                raise interrupt   # unittest re-raises it before the cleanups: the check never runs

        def begins(frame, event):   # unittest's own run returned (here: raised) into the tripwire's
            return event == "return" and frame.f_code is unittest.TestCase.run.__wrapped__.__code__

        def ends(frame, event):
            return event == "return" and frame.f_code is unittest.TestCase.run.__code__
        for fault in (None, ValueError("cannot make the line")):   # the report's own fault, then a handler's
            with self.subTest(fault=fault):
                def run():
                    report = scratch(self) / "report.jsonl"
                    with patch.object(stop_tripwire, "_lost", []) as lost, faulty(fault), \
                            patch.dict(os.environ, {stop_tripwire.REPORT: str(report)}):
                        result, escaped = run_one(Interrupted("test"))
                    return escaped is interrupt, bool(lost)
                points, first, wrong = self.sweep(run, begins, ends)
                self.assertEqual((first, wrong), ((True, fault is not None), []))
                # The steps the review found outside the protection: the count of what is left, the configuration read.
                self.assertTrue({("run", "c_return", "remove"), ("run", "call", "_unchecked")} <= set(points), points)
                if fault:
                    self.assertTrue({("_lose", "c_return", "len"), ("_lose", "call", "get")} <= set(points), points)

    def test_a_handlers_error_at_any_step_of_the_exits_flush_marks_the_report_and_goes_no_further(self):
        def begins(frame, event):
            return event == "call" and frame.f_code is stop_tripwire._at_exit.__code__

        def ends(frame, event):   # the exit line, after the flush
            return event == "call" and frame.f_code is stop_tripwire._announce.__code__
        for fault in (None, ValueError("cannot make the line")):
            with self.subTest(fault=fault):
                def run():
                    left = stop_tripwire.Frame(None)
                    left.publications.append(stop_tripwire.Publication(
                        "cli", ("unclassified", "untyped_harness_error", "no test took it"), None, None, None))
                    report = scratch(self) / "report.jsonl"
                    with patch.object(stop_tripwire, "_lost", []) as lost, faulty(fault), \
                            patch.object(stop_tripwire, "_outside", left), \
                            patch.dict(os.environ, {stop_tripwire.REPORT: str(report)}), \
                            contextlib.redirect_stderr(io.StringIO()) as stderr:
                        escaped = None
                        try:
                            stop_tripwire._at_exit()
                        except BaseException as error:   # what leaves the exit callback is the subject
                            escaped = error
                    return escaped is None, bool(lost) and len(run_shards.report_incomplete(stderr.getvalue())) == 1
                points, first, wrong = self.sweep(run, begins, ends)
                self.assertEqual((first, wrong), ((True, fault is not None), []))
                self.assertIn(("_at_exit", "call", "_unchecked"), points)
                if fault:
                    self.assertTrue({("_lose", "c_return", "len"), ("_lose", "call", "get")} <= set(points), points)


# A test process (``python -c INTERRUPTED <mode> wire|plain``, cwd the repository root, tests/ on the path), with the
# tripwire (wire) or without it (plain). Its step prints "step" and waits, never blocking, for the signal the parent
# sends: in a step of the tripwire the signal is held, so the step sees it pending ("held") and ends; without the
# tripwire the same wait is plain code, and the handler runs as the signal comes. The mode places the step and picks
# the handler. Lines on stdout tell how far the process went, the two exit callbacks that each of them ran, in order.
INTERRUPTED = r'''
import atexit, contextlib, errno, io, signal, sys, time, unittest
atexit.register(print, "earlier callback", flush=True)
mode, wired = sys.argv[1], sys.argv[2] == "wire"
if wired:
    import stop_tripwire
from harness import cli, stops
atexit.register(print, "later callback", flush=True)
waited = signal.SIGALRM if mode == "alarm" else signal.SIGINT


def say(*words):
    print(*words, flush=True)


def raising(failure):
    def handler(signum, frame):
        raise failure
    return handler


if mode == "alarm":   # a watchdog
    signal.signal(signal.SIGALRM, raising(AssertionError("a watchdog fired")))
elif mode == "oserror":
    signal.signal(signal.SIGINT, raising(OSError(errno.EINTR, "the test's own SIGINT handler")))
elif mode == "exit":
    signal.signal(signal.SIGINT, raising(SystemExit(7)))


def waiting():
    say("step")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if waited in signal.sigpending():
            say("held")
            return
        time.sleep(0.01)


def runtime_error():
    try:
        stops.validate({})
    except Exception as error:   # an untyped refusal of the runtime: an unclassified stop
        return error


def in_step(call):
    """``call()``, whose registration by the tripwire waits first."""
    seen = stop_tripwire._seen

    def observing(*args):
        waiting()
        return seen(*args)
    stop_tripwire._seen = observing
    try:
        return call()
    finally:
        stop_tripwire._seen = seen


def step():   # report_stop returned its value: nothing leaves while the step runs
    if not wired:
        return waiting()
    with contextlib.redirect_stderr(io.StringIO()):
        in_step(lambda: cli.report_stop(runtime_error()))


def leave():   # the watched call raised: its exception leaves while the step runs
    original = runtime_error()

    def call():
        raise original
    try:
        in_step(stop_tripwire._leaving("entry")(call))
    except KeyboardInterrupt as interrupt:
        say("chained", interrupt.__context__ is original)
        raise


class Result(unittest.TextTestResult):
    def stop(self):   # what unittest's --catch handler calls at the first signal
        say("first handled")
        super().stop()


class Steps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if mode == "set_up_class":
            leave()
            raise unittest.SkipTest("every test of the class is skipped")

    def test_one(self):
        self.allow_unclassified = "Use a runtime refusal as the signal rendezvous and measure the handler's result."
        if mode == "body_pending":
            leave()
        elif mode != "tear_down_class":
            step()
        say("test went on")
        if mode == "catch_twice":
            step()
            say("test went on again")
        time.sleep(20 if mode == "body" else 0)

    def test_two(self):
        say("next test ran")
        time.sleep(20)

    @classmethod
    def tearDownClass(cls):
        if mode == "tear_down_class":
            step()
            say("fixture went on")


if mode in ("collecting", "exit", "interrupt"):   # no test runs in this process, as in run_shards' collection
    leave() if mode == "collecting" else step()
    say("collected")
    sys.exit(0)
if mode.startswith("catch"):
    unittest.installHandler()
names = ["test_one"] if mode in ("tear_down_class", "alarm", "oserror") else ["test_one", "test_two"]
result = unittest.TextTestRunner(resultclass=Result).run(unittest.TestSuite(Steps(name) for name in names))
say("runner returned", result.shouldStop, result.testsRun)
sys.exit(not result.wasSuccessful())
'''

# A test process (``python -c BLOCKING <mode>``) whose test's id() (mode "id") or whose runtime exception's __str__
# (mode "str") prints "step" and then waits for ever on a pipe nothing writes. The tripwire asks neither; wherever they
# run, a signal ends the process at once. In "str" the exception leaves a watched entry and the runtime then formats it
# for its stop line; in "id" only the process itself asks, once the run is over.
BLOCKING = r'''
import atexit, contextlib, io, os, sys, unittest
mode = sys.argv[1]
import stop_tripwire
from harness import cli, stops
atexit.register(print, "exit callback", flush=True)
never = os.pipe()[0]


def wait():
    print("step", flush=True)
    os.read(never, 1)


class Blocking(RuntimeError):
    def __str__(self):
        wait()
        return "never"


class Case(unittest.TestCase):
    def test_one(self):
        def call():
            if mode == "str":
                raise Blocking("the runtime's")
            stops.validate({})   # an untyped refusal of the runtime: an unclassified stop
        try:
            stop_tripwire._leaving("entry")(call)()
        except Exception as error:   # the CLI's way with what an entry let through
            with contextlib.redirect_stderr(io.StringIO()):
                cli.report_stop(error)

    def id(self):
        wait()
        return super().id()


try:
    unittest.TextTestRunner(stream=io.StringIO()).run(Case("test_one"))
except KeyboardInterrupt as interrupt:
    print("chained", isinstance(interrupt.__context__, Blocking), flush=True)
    Blocking.__str__ = RuntimeError.__str__   # the interpreter shows the chain as the process ends
    raise
print("ran", flush=True)
Case("test_one").id()
'''


class RealInterrupts(unittest.TestCase):
    """A real signal to a child test process while a step of its tripwire runs: the user's Ctrl-C, or a watchdog."""

    START = 60   # seconds a child may take to reach its step on a loaded machine
    LIMIT = 8    # seconds from the last signal to the exit of a child it ends; the rest of the test would sleep 20
    TAIL = ["later callback", "earlier callback"]   # every exit callback ran, in the usual order

    def interrupted(self, mode, wired=True, steps=1, signum=signal.SIGINT, script=INTERRUPTED, report=None):
        """Run ``mode`` of ``script``, sending ``signum`` at each of its first ``steps`` "step" lines: exit status,
        stdout lines, stderr."""
        env = run_shards.child_env(ROOT)
        env.pop(stop_tripwire.REPORT, None)
        if report is not None:
            env[stop_tripwire.REPORT] = str(report)
        errors = scratch(self) / "stderr"
        with errors.open("wb") as stderr:
            child = subprocess.Popen([sys.executable, "-c", script, mode, "wire" if wired else "plain"],
                                     cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr,
                                     text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        lines = queue.Queue()

        def read():
            for line in child.stdout:
                lines.put(line.rstrip("\n"))
            lines.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        out, sent = [], 0
        while sent < steps:
            try:
                line = lines.get(timeout=self.START)
            except queue.Empty:
                line = None
            if line is None:
                self.fail(f"{mode}: {sent} of {steps} steps reached; stdout {out}, stderr {errors.read_text()!r}")
            out.append(line)
            if line == "step":
                child.send_signal(signum)
                sent += 1
        try:
            child.wait(self.LIMIT)
        except subprocess.TimeoutExpired:
            self.fail(f"{mode}: still running {self.LIMIT} s after the signal; stdout so far {out}")
        reader.join(10)
        while (line := lines.get(timeout=1)) is not None:
            out.append(line)
        return child.returncode, out, errors.read_text()

    def assert_interrupted(self, code, stderr):
        self.assertEqual(code, -signal.SIGINT, stderr)   # ended by the signal, as an uncaught Ctrl-C ends Python
        self.assertEqual(stderr.rstrip().splitlines()[-1], "KeyboardInterrupt", stderr)

    def test_a_signal_during_a_step_of_a_long_test_ends_the_process_at_once(self):
        code, out, stderr = self.interrupted("body")
        self.assert_interrupted(code, stderr)
        self.assertEqual(out, ["step", "held"] + self.TAIL)   # neither the rest of the test (20 s) nor the next one ran

    def test_a_signal_during_a_step_of_the_last_tear_down_class_ends_the_process_interrupted(self):
        code, out, stderr = self.interrupted("tear_down_class")
        self.assert_interrupted(code, stderr)
        self.assertEqual(out, ["test went on", "step", "held"] + self.TAIL)

    def test_a_signal_during_a_step_in_set_up_class_ends_the_process_though_every_test_would_be_skipped(self):
        code, out, stderr = self.interrupted("set_up_class")
        self.assert_interrupted(code, stderr)
        self.assertEqual(out, ["step", "held", "chained True"] + self.TAIL)

    def test_a_signal_during_a_step_while_the_calls_exception_leaves_ends_the_process_at_once(self):
        code, out, stderr = self.interrupted("body_pending")
        self.assert_interrupted(code, stderr)
        self.assertEqual(out, ["step", "held", "chained True"] + self.TAIL)   # the next test (20 s) did not run

    def test_a_signal_during_a_step_while_collecting_ends_the_process_at_once(self):
        code, out, stderr = self.interrupted("collecting")
        self.assert_interrupted(code, stderr)
        self.assertEqual(out, ["step", "held", "chained True"] + self.TAIL)

    def test_under_catch_a_first_signal_in_a_step_stops_the_run_after_the_test_and_a_second_ends_it_at_once(self):
        # unittest's own behaviour: its handler runs after the step, so the first Ctrl-C lets the test finish.
        code, out, stderr = self.interrupted("catch")
        self.assertEqual((code, out), (0, ["step", "held", "first handled", "test went on", "runner returned True 1"]
                                       + self.TAIL), stderr)
        # The second signal is sent once the first was handled (TestResult.stop), during the next step.
        code, out, stderr = self.interrupted("catch_twice", steps=2)
        self.assert_interrupted(code, stderr)
        self.assertEqual(out, ["step", "held", "first handled", "test went on", "step", "held"] + self.TAIL)

    def test_a_test_id_or_an_exception_text_that_waits_for_ever_holds_no_signal_back(self):
        for mode, out, channels in (("id", ["ran", "step"], ["cli", "entry"]),
                                    ("str", ["step", "chained True"], ["entry"])):
            with self.subTest(mode):
                report = scratch(self) / "report.jsonl"
                code, lines, stderr = self.interrupted(mode, script=BLOCKING, report=report)
                self.assert_interrupted(code, stderr)
                self.assertEqual(lines, out + ["exit callback"])
                # Reported by the check ("id") or at the end of the run the signal cut short ("str"), ID unasked.
                self.assertEqual([(line["test"], line["channels"], line["exempt"]) for line in
                                  map(json.loads, report.read_text(encoding="utf-8").splitlines())],
                                 [("__main__.Case.test_one", channels, None)])

    def test_what_a_handler_raises_after_a_step_ends_the_process_as_it_does_without_the_tripwire(self):
        for mode, signum, code, text in (
                ("oserror", signal.SIGINT, 1, f"[Errno {errno.EINTR}] the test's own SIGINT handler"),
                ("alarm", signal.SIGALRM, 1, "AssertionError: a watchdog fired"),   # a watchdog in a test
                ("exit", signal.SIGINT, 7, None),
                ("interrupt", signal.SIGINT, -signal.SIGINT, "KeyboardInterrupt")):
            with self.subTest(mode):
                wired = self.interrupted(mode, signum=signum)
                plain = self.interrupted(mode, wired=False, signum=signum)
                self.assertIn("held", wired[1])   # the signal came during the step
                self.assertEqual((wired[0], [line for line in wired[1] if line != "held"]), plain[:2])
                self.assertEqual((wired[0], wired[1][-2:]), (code, self.TAIL))
                for stderr in (wired[2], plain[2]):
                    self.assertNotIn("tripwire error", stderr)
                    if text:
                        self.assertIn(text, stderr)


class Flows(RepoCase):
    """Real CLI and Runtime calls; the unclassified stop comes from a patched step failing in the standard library."""

    def paused(self):
        engine = self.engine(DemoBackend())
        engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"), pause_after_plan=True)
        return engine

    def test_demo_builtin_stop_is_one_finding_across_the_nested_cli_call(self):
        def fail_step(*args, **kwargs):
            Path("left").relative_to("right")

        stderr = io.StringIO()
        with patch.object(tempfile, "tempdir", str(self.outside)), patch.object(Scheduler, "next_step", fail_step), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = cli.main(["--repo", str(self.repo), "demo"])
        self.assertEqual(code, 1, stderr.getvalue())
        ledger, = self.outside.glob("personal-harness-demo-*/.harness/runs/*/state.json")
        self.assertEqual(read_json(ledger)["status"], "blocked")
        findings = stop_tripwire.consume(self)
        self.assertEqual(summary(findings), [(["cli", "entry", "ledger"], "unclassified", "unexpected_exception")])
        self.assertEqual(findings[0].facts["error_type"], "ValueError")
        self.assertEqual(len(findings[0].errors), 1)

    def test_one_cli_operation_is_one_finding_and_two_operations_are_two(self):
        engine = self.paused()
        with patch.object(Scheduler, "next_step", from_the_library):
            for argv in (["resume", engine.run["id"], "--approve-plan"], ["resume", engine.run["id"]]):
                code, errors = run_cli(self, DemoBackend(), argv)
                self.assertEqual(code, 1, errors)
        self.assertEqual(read_json(engine.directory / "state.json")["stop"]["reason"], "unexpected_exception")
        found = stop_tripwire.consume(self)
        # Each operation: the ledger commit, Runtime.resume, the stderr line and the record, joined by the exception
        # and the operation ID; the two operations stop the same run with the same type and stay two.
        self.assertEqual(summary(found), [(["cli", "entry", "ledger", "record"], "unclassified",
                                           "unexpected_exception")] * 2)
        self.assertEqual([finding.facts["error_type"] for finding in found], ["JSONDecodeError"] * 2)

    def test_two_direct_resumes_are_two_findings(self):
        engine = self.paused()
        with patch.object(Scheduler, "next_step", from_the_library):
            for approve in (True, False):
                with self.assertRaises(ValueError):
                    self.engine().resume(engine.run["id"], approve_plan=approve)
        self.assertEqual(summary(stop_tripwire.consume(self)),
                         [(["entry", "ledger"], "unclassified", "unexpected_exception")] * 2)

    def test_native_builtin_stops_keep_the_same_cause_through_cli_and_direct_calls(self):
        engine = self.paused()

        def from_pathlib(*args, **kwargs):
            Path("left").relative_to("right")

        def from_stops(*args, **kwargs):
            stops.make("no such kind", "no such reason")

        ident = engine.run["id"]
        with patch.object(Scheduler, "next_step", from_pathlib):
            with self.assertRaises(ValueError):
                self.engine().resume(ident, approve_plan=True)
            for _ in range(2):
                code, errors = run_cli(self, DemoBackend(), ["resume", ident])
                self.assertEqual(code, 1, errors)
        with patch.object(Scheduler, "next_step", from_stops), self.assertRaises(KeyError):
            run_cli(self, DemoBackend(), ["resume", ident])

        found = stop_tripwire.consume(self)
        self.assertEqual([sorted(item.channels) for item in found],
                         [["entry", "ledger"], ["cli", "entry", "ledger", "record"],
                          ["cli", "entry", "ledger", "record"], ["entry", "escape", "ledger", "record"]])
        self.assertEqual([len(item.errors) for item in found], [1] * 4)
        self.assertEqual([item.facts["error_type"] for item in found], ["ValueError"] * 3 + ["KeyError"])
        self.assertEqual(stop_tripwire._calls("causes"), [])

    def test_builtin_prepare_run_entry_reaches_the_cli_without_an_operation_id(self):
        engine = self.paused()

        def fail_precheck(*args, **kwargs):
            Path("left").relative_to("right")

        argv = ["prepare-run", engine.run["id"], "--manifest", str(engine.directory / "manifest.json")]
        with patch.object(workspace_inputs, "precheck_run", fail_precheck):
            for _ in range(2):
                code, errors = run_cli(self, DemoBackend(), argv)
                self.assertEqual(code, 1, errors)
        findings = stop_tripwire.consume(self)
        self.assertEqual(summary(findings), [(["cli", "entry"], "unclassified", "unexpected_exception")] * 2)
        self.assertEqual([len(item.errors) for item in findings], [1, 1])

    def test_one_exception_through_the_entry_and_the_cli_is_one_finding(self):
        engine = self.paused()
        with patch.object(RunLifecycle, "abandon", from_the_library):
            code, errors = run_cli(self, DemoBackend(), ["abandon", engine.run["id"]])
        self.assertEqual(code, 1, errors)
        self.assertEqual(summary(stop_tripwire.consume(self)),
                         [(["cli", "entry"], "unclassified", "unexpected_exception")])

    def test_an_unclassified_exception_escaping_the_cli_is_a_finding(self):
        deep = []
        for _ in range(5 * sys.getrecursionlimit()):
            deep = [deep]
        # copy.deepcopy recurses in Python per nested list (the K22 path): it fails inside the standard library.
        with patch.object(cli, "show_status", lambda args, repo: copy.deepcopy(deep)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(RecursionError):
            cli.main(["--repo", str(self.repo), "status"])
        found = stop_tripwire.consume(self)
        self.assertEqual(summary(found), [(["escape"], "unclassified", "unexpected_exception")])
        self.assertEqual(found[0].facts["error_type"], "RecursionError")

    def test_operate_reports_a_saved_unclassified_stop_without_an_exception(self):
        engine = self.paused()
        with patch.object(Scheduler, "next_step", from_the_library), self.assertRaises(ValueError):
            self.engine().resume(engine.run["id"], approve_plan=True)
        stop_tripwire.consume(self)
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["stop"]["kind"], "unclassified")
        # resume returns that saved ledger: operate's success branch prints its stop line and records it.
        with patch.object(RunLifecycle, "resume", lambda *args, **kwargs: saved):
            code, errors = run_cli(self, DemoBackend(), ["resume", engine.run["id"]])
        self.assertEqual(code, 0, errors)
        found = stop_tripwire.consume(self)
        self.assertEqual(summary(found), [(["cli_notice", "record"], "unclassified", "unexpected_exception")])
        self.assertEqual(found[0].errors, ())


if __name__ == "__main__":
    unittest.main()
