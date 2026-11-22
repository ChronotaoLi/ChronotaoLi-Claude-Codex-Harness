import contextlib
import errno
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import cli, git, state_files, stops
from harness.adapters import DemoBackend
from harness.config import Config
from harness.process_runtime import ProcessOutcome
from harness.run_context import RunContext
from harness.run_store import RunStore
from harness.util import (CommandDirectoryError, HarnessError, ProcessStartError, ProcessTimeout, ProgramStartError,
                          read_json, run_process, tail, write_json)
from harness.verification import Verification, checks_passed
from harness.workflow import Workflow
from test_harness import ROOT, RepoCase, cli_notice, run_cli, walk_exit, write_project_config
from test_provider_failures import FifoWatchdog
from test_runtime_changes import ScriptedBackend
from test_state_files import FakeFcntl, FakeOs


# Rule 10's facts for a command whose directory could not be opened, and the reasons its record tells apart.
NOT_STARTED = {"status": "failed", "termination_reason": "start_failed", "exit_code": None,
               "exit_confirmed": True, "evidence_complete": True}
REASONS = ("不存在", "不是目录", "是符号链接", "无权限", "fchdir 失败")
CHECK_FAILURES = (ProcessTimeout, CommandDirectoryError, ProgramStartError)


def check_failure(error_type, folder):
    if error_type is ProcessTimeout:
        return ProcessTimeout(1, folder / "check-0")
    return error_type("command could not start")


def open_root(held, root, binding):
    # The bound root's descriptor, opened into the holder the code under test closes.
    return held.open(root, os.O_RDONLY | os.O_DIRECTORY)


class VerificationOutcomeTests(unittest.TestCase):
    def fixture(self, root):
        context = SimpleNamespace(
            run={"dispatches": []}, config=SimpleNamespace(data={"runtime": {"verify_timeout_seconds": 1}}),
            ensure_exited=lambda: None, save=lambda: None, save_fact=lambda *keys: True)
        return Verification(context), root / "checks"

    def run_fake(self, verification, folder, outcome, error=None, then=lambda: None):
        def process(argv, cwd, log, **kwargs):
            kwargs["on_outcome"](outcome)
            then()
            if error:
                error.outcome = outcome
                raise error
            return outcome.exit_code
        with patch("harness.verification.git.open_bound_root", side_effect=open_root), \
                patch("harness.verification.run_process", process):
            return verification.run_verification_commands("task", [{"cwd": ".", "argv": ["check"]}], folder.parent, folder, {})

    def test_zero_exit_with_unknown_group_cannot_pass_and_leaves_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            verification, folder = self.fixture(Path(directory))
            outcome = ProcessOutcome(started=True, exit_code=0, exit_confirmed=False,
                evidence_complete=False, cleanup_errors=[{"step": "observe_group", "errno": 1}])
            with self.assertRaises(HarnessError):
                self.run_fake(verification, folder, outcome)
            results = json.loads((folder / "results.json").read_text())
            self.assertFalse(checks_passed(results))
            self.assertEqual(results[0]["exit_code"], 0)
            self.assertEqual(results[0]["process_outcome"]["cleanup_errors"][0]["errno"], 1)

    def test_timeout_preserves_observed_signal_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            verification, folder = self.fixture(Path(directory))
            outcome = ProcessOutcome(started=True, exit_code=-15, exit_confirmed=True, termination_reason="timeout")
            results = self.run_fake(verification, folder, outcome, ProcessTimeout(1, folder / "check-0"))
            self.assertEqual(results[0]["exit_code"], -15)
            self.assertEqual(results[0]["status"], "timed_out")
            self.assertFalse(checks_passed(results))

    def test_interrupt_saves_partial_receipt_before_propagation(self):
        with tempfile.TemporaryDirectory() as directory:
            verification, folder = self.fixture(Path(directory))
            outcome = ProcessOutcome(started=True, exit_code=-15, exit_confirmed=True, termination_reason="cancelled")
            with self.assertRaises(KeyboardInterrupt):
                self.run_fake(verification, folder, outcome, KeyboardInterrupt())
            result = json.loads((folder / "results.json").read_text())[0]
            self.assertEqual(result["status"], "interrupted")
            self.assertEqual(verification.context.run["dispatches"][0]["exit_code"], -15)

    def test_ledger_save_failure_still_preserves_command_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            verification, folder = self.fixture(Path(directory))
            count = 0
            def save():
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError("ledger unavailable")
            verification.context.save = save
            outcome = ProcessOutcome(started=True, exit_code=0, exit_confirmed=True)
            with self.assertRaises(OSError):
                self.run_fake(verification, folder, outcome)
            saved = json.loads((folder / "results.json").read_text())[0]
            self.assertEqual(saved["exit_code"], 0)
            self.assertEqual(saved["status"], "failed")
            self.assertFalse(saved["evidence_complete"])

    def test_every_raised_rule_11_error_is_its_own_feedback(self):
        # Each throwing cell of rule 11 (process fact S x verification write error W); a bare OSError()
        # wrapped under S is the confirmation review's case, where the wrapper's text is the type's name.
        fault = {"step": "persist_outcome", "type": "OSError", "message": "outcome fsync", "errno": 5}
        writes = (None, lambda: OSError("results fsync"), OSError, lambda: HarnessError("results serialization"))
        cells = [(error_type, fact, write) for error_type in CHECK_FAILURES
                 for fact in (None, "unconfirmed", "evidence") for write in writes
                 if (fact or write) and (error_type is ProcessTimeout or fact != "unconfirmed")]
        for error_type, fact, write in cells:
            timeout = error_type is ProcessTimeout
            with self.subTest(process=error_type.__name__, fact=fact,
                              write=write and repr(write())), tempfile.TemporaryDirectory() as directory:
                verification, folder = self.fixture(Path(directory))
                outcome = ProcessOutcome(started=timeout, exit_confirmed=fact != "unconfirmed",
                                         termination_reason="timeout" if timeout else "start_failed",
                                         evidence_complete=fact != "evidence", errors=[fault] if fact == "evidence" else [])
                failed = []

                def persist(path, value):
                    if write is not None and value and not failed:
                        failed.append(write())
                        raise failed[0]
                    write_json(path, value)
                process = check_failure(error_type, folder)
                with patch("harness.verification.write_json", side_effect=persist), \
                        self.assertRaises((HarnessError, OSError)) as caught:
                    self.run_fake(verification, folder, outcome, process)
                raised = caught.exception
                if fact is None:
                    self.assertIs(raised, failed[0])
                    # Without a process fact the write error's own type decides the stop.
                    self.assertEqual(stop_of(raised), ("runtime_failure", "io") if isinstance(raised, OSError)
                                     else ("unclassified", "untyped_harness_error"))
                else:
                    self.assertEqual(stop_of(raised), {"unconfirmed": ("process_unconfirmed", "dispatch_exit"),
                                                       "evidence": ("evidence", "process_incomplete")}[fact])
                if write is None and fact == "evidence":
                    self.assertIs(raised, process)
                if write is not None:
                    # The explicit chain: the write error's cause is the process error; wrapped only to carry S over
                    # a write error that is not a HarnessError, raised from it with this command's supervisor facts.
                    self.assertIs(failed[0].__cause__, process)
                    if fact is not None and not isinstance(failed[0], HarnessError):
                        self.assertIs(raised.__cause__, failed[0])
                        self.assertEqual(raised.outcome, outcome)
                        self.assertEqual(str(raised), str(failed[0]) or "OSError")   # its text is the write error's
                    else:
                        self.assertIs(raised, failed[0])
                feedback = verification.context.run["feedback"]["verification_error"]
                self.assertEqual((feedback["command"], feedback["error"]),
                                 ({"cwd": ".", "argv": ["check"]}, str(caught.exception)))
                # The command's record stays as evidence, in results.json and the dispatch entry, never a check.
                recorded = {"command": {"cwd": ".", "argv": ["check"]},
                            "termination_reason": "timeout" if timeout else "start_failed", "exit_code": None,
                            "exit_confirmed": fact != "unconfirmed", "evidence_complete": fact != "evidence" and write is None,
                            "status": "timed_out" if timeout and write is None else "failed",
                            "persistence_error": str(failed[0]) if write is not None else None}
                (row,) = read_json(folder / "results.json")
                (entry,) = verification.context.run["dispatches"]
                for value in (row, entry):
                    self.assertEqual({key: value.get(key) for key in recorded}, recorded)
                self.assertEqual(row["timed_out"], timeout)
                self.assertEqual(entry["process_state"] == "exited", fact != "unconfirmed")

    def test_the_first_of_two_failed_writes_is_the_error_raised(self):
        # Rule 11's W is the first of the three writes (excerpt, results.json, ledger) to fail, not a later one.
        for error_type in CHECK_FAILURES:
            timeout = error_type is ProcessTimeout
            for steps in (("excerpt", "results"), ("results", "ledger"), ("excerpt", "ledger")):
                with self.subTest(process=error_type.__name__, failing=steps), \
                        tempfile.TemporaryDirectory() as directory:
                    verification, folder = self.fixture(Path(directory))
                    errors = {"excerpt": OSError("excerpt read"), "results": HarnessError("results serialization"),
                              "ledger": OSError("ledger fsync")}
                    pending, saves = set(steps), []

                    def fail(step):   # each failing write fails once; its retry succeeds
                        if step in pending:
                            pending.remove(step)
                            raise errors[step]

                    def excerpt(path):
                        fail("excerpt")
                        return tail(path)

                    def persist(path, value):
                        if value:
                            fail("results")
                        write_json(path, value)

                    def save():
                        saves.append(len(saves))
                        if len(saves) > 1:   # every save after the entry's registration
                            fail("ledger")
                    verification.context.save = save
                    outcome = ProcessOutcome(started=timeout, exit_confirmed=True,
                                             termination_reason="timeout" if timeout else "start_failed")
                    process = check_failure(error_type, folder)
                    with patch("harness.verification.tail", side_effect=excerpt), \
                            patch("harness.verification.write_json", side_effect=persist), \
                            self.assertRaises((HarnessError, OSError)) as caught:
                        self.run_fake(verification, folder, outcome, process)
                    first = errors[steps[0]]
                    self.assertEqual(pending, set())   # both writes did fail
                    self.assertIs(caught.exception, first)
                    self.assertIs(first.__cause__, process)
                    self.assertEqual(stop_of(first), ("runtime_failure", "io") if isinstance(first, OSError)
                                     else ("unclassified", "untyped_harness_error"))
                    self.assertEqual(verification.context.run["feedback"]["verification_error"]["error"], str(first))
                    (row,) = read_json(folder / "results.json")
                    (entry,) = verification.context.run["dispatches"]
                    for value in (row, entry):
                        self.assertEqual((value["status"], value["evidence_complete"], value["persistence_error"]),
                                         ("failed", False, str(first)))

    def test_a_directory_that_cannot_open_is_a_failed_check_with_its_own_reason(self):
        # Through the real launch: rule 10's record when cleanup and every write were clean (rule 11, first row).
        reasons = {"missing": "不存在", "file": "不是目录", "link": "是符号链接"}
        if os.geteuid() != 0:   # root opens and enters any directory
            reasons.update(unreadable="无权限", unsearchable="fchdir 失败")
        for name, reason in reasons.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as restore:
                root, marker = Path(directory), Path(directory) / "ran"
                verification, folder = self.fixture(root)
                if name == "file":
                    (root / name).write_text("")
                elif name == "link":
                    (root / "target").mkdir()
                    (root / name).symlink_to("target")
                elif name != "missing":
                    (root / name).mkdir()
                    (root / name).chmod(0 if name == "unreadable" else 0o600)
                    restore.callback((root / name).chmod, 0o700)
                commands = [{"cwd": name, "argv": [sys.executable, "-c", f"open({str(marker)!r}, 'w')"]},
                            {"cwd": ".", "argv": [sys.executable, "-c", "print('next ran')"]}]
                with patch("harness.verification.git.open_bound_root", side_effect=open_root):
                    results = verification.run_verification_commands("task", commands, root, folder, {})
                self.assertEqual(read_json(folder / "results.json"), results)
                first, following = results
                entry = verification.context.run["dispatches"][0]
                for value in (first, entry):
                    self.assertEqual({key: value[key] for key in NOT_STARTED}, NOT_STARTED)
                self.assertEqual((first["command"], first["timed_out"], entry["process_state"]), (commands[0], False, "exited"))
                error = first["process_outcome"]["errors"][0]
                self.assertEqual((error["step"], error["type"]), ("execution", "CommandDirectoryError"))
                for text in (first["stderr"], error["message"]):
                    self.assertIn(repr(name), text)
                    self.assertEqual([known for known in REASONS if known in text], [reason])
                self.assertFalse(marker.exists())
                self.assertEqual((following["command"], following["status"], following["stdout"]),
                                 (commands[1], "passed", "next ran\n"))
                self.assertNotIn("feedback", verification.context.run)
                self.assertFalse(checks_passed(results))

    def test_a_refused_ledger_save_keeps_its_own_stop_unless_a_process_fact_decides(self):
        fault = {"step": "persist_outcome", "type": "OSError", "message": "outcome fsync", "errno": 5}
        cells = [(error_type, fact, refusal) for error_type in CHECK_FAILURES for fact in (None, "evidence")
                 for refusal in ("another writer", "untyped")]
        for error_type, fact, refusal in cells:
            timeout = error_type is ProcessTimeout
            with self.subTest(process=error_type.__name__, fact=fact, refusal=refusal), \
                    tempfile.TemporaryDirectory() as directory:
                verification, folder = self.fixture(Path(directory))
                run = verification.context.run = {"id": "run_save", "store_revision": 0, "dispatches": []}
                store = RunStore(Path(directory))
                store.attach(run)
                store.commit()
                ledger, saves, refused = Path(directory) / "runs" / run["id"] / "state.json", [], []

                def save():
                    saves.append(len(saves))
                    try:
                        if refusal == "untyped" and len(saves) > 1:   # every save after the entry's registration
                            raise HarnessError("ledger unavailable")
                        store.commit()
                    except HarnessError as error:
                        refused.append(error)
                        raise
                verification.context.save = save

                def another_writer():
                    if refusal == "another writer":   # every later save of this ledger is refused (RunStore.commit)
                        ledger.write_bytes(ledger.read_bytes() + b"\n")
                outcome = ProcessOutcome(started=timeout, exit_confirmed=True, evidence_complete=fact is None,
                                         termination_reason="timeout" if timeout else "start_failed",
                                         errors=[fault] if fact else [])
                process = check_failure(error_type, folder)
                with self.assertRaises(HarnessError) as caught:
                    self.run_fake(verification, folder, outcome, process, then=another_writer)
                raised, write = caught.exception, refused[0]
                own = ("runtime_failure", "invariant") if refusal == "another writer" else ("unclassified", "untyped_harness_error")
                self.assertIs(write.__cause__, process)
                self.assertEqual(stop_of(raised), ("evidence", "process_incomplete") if fact else own)
                if fact is not None and refusal == "another writer":
                    # Wrapped from the refusal, with this command's supervisor facts; the refusal keeps its own stop.
                    self.assertIs(raised.__cause__, write)
                    self.assertEqual(raised.outcome, outcome)
                    self.assertEqual((stop_of(write), str(raised)), (own, str(write)))
                else:
                    self.assertIs(raised, write)
                self.assertEqual(verification.context.run["feedback"]["verification_error"]["error"], str(raised))
                (row,) = read_json(folder / "results.json")
                self.assertEqual((row["command"], row["status"], row["persistence_error"]),
                                 ({"cwd": ".", "argv": ["check"]}, "failed", str(write)))

    def test_a_missing_program_is_replaced_by_the_write_error(self):
        with tempfile.TemporaryDirectory() as directory:
            verification, folder = self.fixture(Path(directory))
            failed = []

            def persist(path, value):
                if value and not failed:
                    failed.append(OSError("results fsync"))
                    raise failed[0]
                write_json(path, value)

            with patch("harness.verification.write_json", side_effect=persist), \
                    patch("harness.verification.git.open_bound_root", side_effect=open_root), \
                    self.assertRaises(OSError) as caught:
                verification.run_verification_commands(
                    "task", [{"cwd": ".", "argv": [str(Path(directory) / "absent-executable")]}],
                    Path(directory), folder, {})
            raised = caught.exception
            self.assertIs(raised, failed[0])
            self.assertIsInstance(raised.__cause__, ProgramStartError)
            self.assertIsInstance(raised.__cause__.__cause__, FileNotFoundError)
            self.assertEqual(verification.context.run["feedback"]["verification_error"]["error"], str(raised))

    def test_local_start_failures_survive_result_write_errors_and_use_process_facts_first(self):
        failures = ((errno.EAGAIN, BlockingIOError, "io"), (None, ValueError, "invariant"),
                    (errno.ENOENT, FileNotFoundError, "io"))
        for error_number, cause_type, reason in failures:
            for process_fault in (False, True):
                for write_fault in (False, True):
                    with self.subTest(errno=error_number, process_fault=process_fault, write_fault=write_fault), \
                            tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                        root = Path(directory)
                        verification, folder = self.fixture(root)
                        command = {"cwd": ".", "argv": ["invalid\x00arg" if error_number is None else "check"]}
                        process_errors = []
                        result_write = OSError(errno.EIO, "results fsync")

                        def process(*args, **kwargs):
                            try:
                                return run_process(*args, **kwargs)
                            except ProcessStartError as error:
                                process_errors.append(error)
                                raise

                        def persist_outcome(path, value):
                            if process_fault and path.name == "check-0.outcome.json" and value["errors"]:
                                raise OSError(errno.EIO, "outcome fsync")
                            write_json(path, value)

                        pending = write_fault

                        def persist_results(path, value):
                            nonlocal pending
                            if pending and value:
                                pending = False
                                raise result_write
                            write_json(path, value)

                        if error_number is not None:
                            # A synchronous error with no program filename is a local startup fault,
                            # even if its errno can also occur when exec fails.
                            stack.enter_context(patch("harness.util.subprocess.Popen",
                                                      side_effect=OSError(error_number, os.strerror(error_number))))
                        stack.enter_context(patch("harness.verification.git.open_bound_root", side_effect=open_root))
                        stack.enter_context(patch("harness.verification.run_process", side_effect=process))
                        stack.enter_context(patch("harness.util.write_json", side_effect=persist_outcome))
                        stack.enter_context(patch("harness.verification.write_json", side_effect=persist_results))
                        with self.assertRaises(ProcessStartError) as caught:
                            verification.run_verification_commands("task", [command], root, folder, {})
                        (error,) = process_errors
                        self.assertIs(caught.exception, error)
                        self.assertIs(type(error), ProcessStartError)
                        self.assertIsInstance(error.__cause__, cause_type)
                        self.assertNotIn(result_write, traceback_chain(error))
                        if process_fault:
                            self.assertEqual(stop_of(error), ("evidence", "process_incomplete"))
                        else:
                            self.assertEqual(stop_of(error), ("runtime_failure", reason))
                            if reason == "io":
                                self.assertEqual(error.stop["data"]["error_type"], cause_type.__name__)
                        feedback = verification.context.run["feedback"]["verification_error"]
                        self.assertEqual((feedback["command"], feedback["error"]), (command, str(error)))
                        (row,) = read_json(folder / "results.json")
                        (entry,) = verification.context.run["dispatches"]
                        for record in (row, entry):
                            expected = {**NOT_STARTED, "evidence_complete": not (process_fault or write_fault)}
                            self.assertEqual({key: record[key] for key in expected}, expected)
                            self.assertEqual(record.get("persistence_error"), str(result_write) if write_fault else None)
                            self.assertEqual(record["process_outcome"], error.outcome.to_dict())

    def test_all_formal_commands_execute_after_known_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            verification, folder = self.fixture(Path(directory))
            with patch("harness.verification.git.open_bound_root", side_effect=open_root):
                results = verification.run_verification_commands("task", [
                    {"cwd": ".", "argv": [sys.executable, "-c", "raise SystemExit(3)"]},
                    {"cwd": ".", "argv": [sys.executable, "-c", "print('second gate ran')"]}], Path(directory), folder, {})
            self.assertEqual([row["exit_code"] for row in results], [3, 0])
            self.assertEqual([row["status"] for row in results], ["failed", "passed"])
            self.assertIn("second gate ran", results[1]["stdout"])
            self.assertFalse(checks_passed(results))

    def test_passed_status_requires_consistent_complete_lifecycle(self):
        for result in ({"exit_code": 0}, {"exit_code": None}, {"exit_code": 0, "status": "unknown"}):
            self.assertFalse(checks_passed([result]))
        passed = {"status": "passed", "exit_code": 0, "termination_reason": "normal",
                  "exit_confirmed": True, "evidence_complete": True}
        self.assertTrue(checks_passed([passed]))
        for key, value in (("status", "unknown"), ("exit_code", 1), ("exit_code", False),
                           ("termination_reason", "timeout"), ("exit_confirmed", False),
                           ("evidence_complete", False)):
            with self.subTest(field=key, value=value):
                self.assertFalse(checks_passed([{**passed, key: value}]))
                self.assertFalse(checks_passed([{k: v for k, v in passed.items() if k != key}]))


def stop_of(error):
    value = stops.of(error)
    return value["kind"], value["reason"]


def traceback_chain(error):
    """The exceptions a traceback prints, outermost first: the cause, else the context unless suppressed."""
    chain, seen = [], set()
    while error is not None and id(error) not in seen:
        chain.append(error)
        seen.add(id(error))
        error = error.__cause__ if error.__cause__ is not None else None if error.__suppress_context__ else error.__context__
    return chain


class VerificationStopExitTests(RepoCase):
    """Rule 11 through the scheduler's exit: what the run saves and what leaves the call."""

    def start(self, outcome, *, pgid=None, other_writer=False):
        engine, raised, self.written = self.engine(), [], None

        def process(argv, cwd, log, **kwargs):
            if pgid is not None:
                kwargs["on_start"](pgid)
            kwargs["on_outcome"](outcome)
            if other_writer:   # every later save of this ledger is refused (RunStore.commit)
                ledger = engine.directory / "state.json"
                self.written = ledger.read_bytes() + b"\n"
                ledger.write_bytes(self.written)
            raised.append(ProcessTimeout(1, log))
            raised[-1].outcome = outcome
            raise raised[-1]
        with patch("harness.verification.run_process", side_effect=process), self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        (timeout,) = raised
        return engine, caught.exception, timeout

    def test_an_exit_gate_stop_saves_the_command_and_its_error_as_feedback(self):
        reaped = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        reaped.wait()
        command = self.config.data["verification"]["commands"][0]
        # Unconfirmed exit with no process group recorded, and with a recorded group that is gone.
        for pgid in (None, reaped.pid):
            with self.subTest(pgid=pgid):
                engine, error, _ = self.start(ProcessOutcome(termination_reason="timeout", started=True, exit_confirmed=False),
                                              pgid=pgid)
                self.assertEqual(stop_of(error), ("process_unconfirmed", "dispatch_exit"))
                ledger = read_json(engine.directory / "state.json")
                self.assertEqual((ledger["status"], ledger["stop"]["kind"]), ("blocked", "process_unconfirmed"))
                feedback = ledger["feedback"]["verification_error"]
                self.assertEqual((feedback["command"], feedback["error"]), (command, str(error)))
                results = read_json(Path(feedback["log"]).parent / "results.json")
                self.assertEqual([(row["command"], row["status"]) for row in results], [(command, "timed_out")])

    def test_interrupt_with_a_verification_write_failure_keeps_both_receipts_interrupted(self):
        command, = self.config.data["verification"]["commands"]
        plan = read_json(ROOT / "examples" / "plan.json")
        publish = RunStore._publish_candidate
        for interrupt_type, reason in ((KeyboardInterrupt, "cancelled"), (SystemExit, "interrupted")):
            for step in ("excerpt", "results", "ledger"):
                with self.subTest(interrupt=interrupt_type.__name__, step=step):
                    engine = self.engine()
                    interrupt = interrupt_type("verification interrupted")
                    fault = OSError(errno.EIO, f"{step} write failed")
                    interrupted = failed = False

                    def process(argv, cwd, log, **kwargs):
                        nonlocal interrupted
                        interrupt.outcome = ProcessOutcome(started=True, exit_code=-2, exit_confirmed=True,
                                                           termination_reason=reason)
                        kwargs["on_outcome"](interrupt.outcome)
                        interrupted = True
                        raise interrupt

                    def fail_once(destination):
                        nonlocal failed
                        if interrupted and not failed and destination == step:
                            failed = True
                            raise fault

                    def excerpt(path):
                        fail_once("excerpt")
                        return tail(path)

                    def results_write(path, value):
                        fail_once("results")
                        write_json(path, value)

                    def ledger_write(store, locked, candidate, data):
                        fail_once("ledger")
                        publish(store, locked, candidate, data)

                    with patch("harness.verification.run_process", process), \
                            patch("harness.verification.tail", excerpt), \
                            patch("harness.verification.write_json", results_write), \
                            patch.object(RunStore, "_publish_candidate", ledger_write), \
                            self.assertRaises(interrupt_type) as caught:
                        engine.start("Implement greet", plan)
                    self.assertTrue(failed)
                    self.assertIs(caught.exception, interrupt)
                    self.assertNotIn(fault, traceback_chain(interrupt))
                    ledger = read_json(engine.directory / "state.json")
                    self.assertEqual((ledger["status"], ledger["stop"]["kind"], ledger["stop"]["reason"]),
                                     ("interrupted", "interrupted", "signal"))
                    feedback = ledger["feedback"]["verification_error"]
                    self.assertEqual((feedback["command"], feedback["error"]), (command, str(interrupt)))
                    row, = read_json(Path(feedback["log"]).parent / "results.json")
                    entry, = (item for item in ledger["dispatches"] if item["role"] == "verification")
                    for record in (row, entry):
                        self.assertEqual((record["status"], record["termination_reason"],
                                          record["evidence_complete"], record["persistence_error"]),
                                         ("interrupted", reason, False, str(fault)))

    def test_interrupt_before_supervisor_outcome_keeps_both_receipts_interrupted(self):
        command, = self.config.data["verification"]["commands"]
        plan = read_json(ROOT / "examples" / "plan.json")
        for interrupt in (KeyboardInterrupt("before supervisor outcome"), SystemExit(27)):
            with self.subTest(interrupt=type(interrupt).__name__):
                engine = self.engine()
                with patch("harness.process_runtime.ProcessSupervisor", side_effect=interrupt), \
                        self.assertRaises(type(interrupt)) as caught:
                    engine.start("Implement greet", plan)
                self.assertIs(caught.exception, interrupt)
                ledger = read_json(engine.directory / "state.json")
                self.assertEqual(ledger["status"], "interrupted")
                self.assertEqual((ledger["stop"]["kind"], ledger["stop"]["reason"]), ("interrupted", "signal"))
                feedback = ledger["feedback"]["verification_error"]
                self.assertEqual((feedback["command"], feedback["error"]), (command, str(interrupt)))
                log = Path(feedback["log"])
                self.assertFalse(log.with_suffix(".outcome.json").exists())
                row, = read_json(log.parent / "results.json")
                entry, = (item for item in ledger["dispatches"] if item["role"] == "verification")
                reason = "cancelled" if isinstance(interrupt, KeyboardInterrupt) else "interrupted"
                for record in (row, entry):
                    self.assertEqual((record["status"], record["termination_reason"]), ("interrupted", reason))
                    self.assertIsNone(record["exit_code"])
                    self.assertIs(record["exit_confirmed"], False)
                    self.assertIs(record["evidence_complete"], False)

    def test_sigint_with_supervisor_outcome_write_failure_keeps_exit_130_and_resume(self):
        continue_check = self.outside / "continue-check"
        program = "from pathlib import Path\nimport sys, time\nif not Path(sys.argv[1]).exists(): time.sleep(30)"
        command = {"cwd": ".", "argv": [sys.executable, "-c", program, str(continue_check)]}
        self.config.data["verification"]["commands"] = [command]
        plan_path = self.outside / "plan.json"
        write_json(plan_path, read_json(ROOT / "examples" / "plan.json"))
        fault = OSError(errno.EIO, "supervisor outcome write failed")
        backend = DemoBackend()
        interrupts = []
        failed = False

        def signal_after_start(argv, cwd, log, *, on_start, **kwargs):
            def started(pgid):
                on_start(pgid)
                signal.raise_signal(signal.SIGINT)

            try:
                return run_process(argv, cwd, log, on_start=started, **kwargs)
            except KeyboardInterrupt as error:
                interrupts.append(error)
                raise

        def outcome_write(path, value):
            nonlocal failed
            if not failed and path.name == "check-0.outcome.json" and value["termination_reason"] == "cancelled":
                failed = True
                raise fault
            write_json(path, value)

        with patch("harness.verification.run_process", signal_after_start), \
                patch("harness.util.write_json", outcome_write), \
                patch.object(cli, "report_stop", wraps=cli.report_stop) as report:
            code, stderr = run_cli(self, backend, ["run", "Implement greet", "--plan", str(plan_path)])
        self.assertEqual(code, 130, stderr)
        ledger_path, = (self.repo / ".harness" / "runs").glob("*/state.json")
        notice = cli_notice(stderr)
        self.assertEqual((notice["kind"], notice["reason"], notice["exit_code"]), ("interrupted", "signal", 130))
        self.assertIn(f"resume {ledger_path.parent.name}", notice["exits"])
        ledger = read_json(ledger_path)
        self.assertEqual(ledger["status"], "interrupted")
        self.assertIs(ledger["stop"]["data"]["exits_confirmed"], True)
        feedback = ledger["feedback"]["verification_error"]
        interrupt, = interrupts
        self.assertIs(report.call_args.args[0], interrupt)
        self.assertEqual((feedback["command"], feedback["error"]), (command, str(interrupt)))
        row, = read_json(Path(feedback["log"]).parent / "results.json")
        entry, = (item for item in ledger["dispatches"] if item["role"] == "verification")
        for record in (row, entry):
            self.assertEqual((record["status"], record["termination_reason"], record["exit_code"]),
                             ("interrupted", "cancelled", -signal.SIGTERM))
            self.assertIs(record["exit_confirmed"], True)
            self.assertIs(record["evidence_complete"], False)
            self.assertIn({"step": "persist_outcome", "type": "OSError", "message": str(fault), "errno": errno.EIO},
                          record["process_outcome"]["errors"])
        continue_check.touch()
        walk_exit(self, ledger_path.parent, ("interrupted", "signal"), backend=backend, expect_status="completed")

    def test_a_refused_stop_save_keeps_the_rule_11_error_on_its_traceback_chain(self):
        outcome = ProcessOutcome(termination_reason="timeout", started=True, exit_confirmed=True, evidence_complete=False,
                                 errors=[{"step": "persist_outcome", "type": "OSError", "message": "outcome fsync", "errno": 5}])
        engine, error, timeout = self.start(outcome, other_writer=True)
        # The stop's own save is refused like every save after the other writer: that refusal is what leaves,
        # and the other writer's bytes stay as written.
        self.assertEqual(stop_of(error), ("runtime_failure", "invariant"))
        self.assertEqual((engine.directory / "state.json").read_bytes(), self.written)
        self.assertEqual(json.loads(self.written)["status"], "running")
        chain = traceback_chain(error)
        wrapper = next(i for i, link in enumerate(chain) if stop_of(link) == ("evidence", "process_incomplete"))
        write = next(i for i, link in enumerate(chain) if i > wrapper and stop_of(link) == ("runtime_failure", "invariant"))
        self.assertGreater(wrapper, 0)
        self.assertIsInstance(chain[write], HarnessError)
        self.assertLess(write, len(chain) - 1)
        self.assertIs(chain[-1], timeout)
        self.assertIn(str(timeout), "".join(traceback.format_exception(error)))

    def test_a_stop_that_saves_records_the_process_fact(self):
        outcome = ProcessOutcome(termination_reason="timeout", started=True, exit_confirmed=True, evidence_complete=False,
                                 errors=[{"step": "persist_outcome", "type": "OSError", "message": "outcome fsync", "errno": 5}])
        engine, error, timeout = self.start(outcome)
        self.assertIs(error, timeout)
        self.assertEqual(stop_of(error), ("evidence", "process_incomplete"))
        ledger = read_json(engine.directory / "state.json")
        self.assertEqual((ledger["status"], ledger["stop"]["kind"], ledger["stop"]["reason"]),
                         ("blocked", "evidence", "process_incomplete"))
        feedback = ledger["feedback"]["verification_error"]
        self.assertEqual((feedback["command"], feedback["error"]), (self.config.data["verification"]["commands"][0], str(error)))


class CandidateFifoVerificationTests(RepoCase):
    pipe_name = "pipe.txt"

    def supplied_plan(self, *, implemented=False):
        (self.repo / self.pipe_name).write_text("Tracked evidence\n")
        paths = [self.pipe_name]
        if implemented:
            # The scripted worker writes these same bytes, so only the FIFO changes the candidate.
            (self.repo / "hello.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n')
            paths.append("hello.py")
        git.git(self.repo, "add", "--", *paths)
        git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                "-c", "commit.gpgsign=false", "commit", "-m", "Track FIFO fixture inputs")
        plan = read_json(ROOT / "examples" / "plan.json")
        plan["tasks"][0]["allowed_paths"].append(self.pipe_name)
        return plan

    def watch_fifo(self, path, checking):
        watchdog = FifoWatchdog(path, bound=5)
        checking.callback(lambda: self.assertFalse(watchdog.stop(), f"Blocked on FIFO {path}"))

    def test_worker_fifo_on_evidence_path_stops_before_preflight_and_recovers(self):
        plan = self.supplied_plan(implemented=True)
        plan["tasks"][0]["evidence_requirements"] = [
            {"id": "evidence", "acceptance_index": 0, "kind": "static", "path": self.pipe_name}]
        self.config.data["runtime"]["max_attempts"] = 2

        with contextlib.ExitStack() as checking:
            def replace_evidence(backend, kwargs, packet, result):
                if kwargs["role"] == "worker" and len(backend.calls) == 1:
                    path = kwargs["cwd"] / self.pipe_name
                    path.unlink()
                    os.mkfifo(path)
                    self.watch_fifo(path, checking)

            backend = ScriptedBackend(replace_evidence)
            engine = self.engine(backend)
            with self.assertRaises(git.ScopeViolation):
                engine.start("Implement greet", plan)

        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["status"], "blocked")
        stop = saved["stop"]
        row = ("scope_violation", "special_file")
        self.assertEqual((stop["kind"], stop["reason"]), row)
        self.assertEqual((stop["data"]["path"], stop["source"]["task_id"]), (self.pipe_name, "greet"))
        (rejected,) = saved["dispatches"]
        self.assertEqual((rejected["role"], rejected["worker_receipt_consumed"]), ("worker", "scope_rejected"))
        self.assertEqual(saved["worker_receipts"], {})
        self.assertEqual(saved["worker_recovery"]["greet"]["dispatch_id"], rejected["id"])
        self.assertEqual([call["role"] for call in backend.calls], ["worker"])
        self.assertFalse(list((engine.directory / "preflight").rglob("result.json")))

        path = engine.worktree / self.pipe_name
        path.unlink()
        path.write_text("Tracked evidence restored\n")
        resumed = walk_exit(self, engine.directory, row, backend=backend, expect_status="completed")
        first, fresh = [entry for entry in resumed["dispatches"] if entry["role"] == "worker"]
        self.assertEqual(first["id"], rejected["id"])
        self.assertNotEqual(fresh["id"], rejected["id"])
        _, recovered = [call for call in backend.calls if call["role"] == "worker"]
        self.assertEqual(recovered["packet"]["recovery"]["source"]["dispatch_id"], rejected["id"])

    def test_verification_command_fifo_stops_at_snapshot_comparison_before_diff(self):
        plan = self.supplied_plan()
        script = "import os, sys; os.unlink(sys.argv[1]); os.mkfifo(sys.argv[1])"
        command = {"cwd": ".", "argv": [sys.executable, "-c", script, self.pipe_name]}
        self.config = Config.load(ROOT / "harness.toml", write_project_config(self.repo, [command]))
        backend = ScriptedBackend()
        engine = self.engine(backend)
        verification = engine.services.verification
        run_commands = verification.run_verification_commands

        with contextlib.ExitStack() as checking:
            def watch_before_commands(*args, **kwargs):
                # Cover everything after the child creates the FIFO, including command finalization.
                self.watch_fifo(engine.worktree / self.pipe_name, checking)
                return run_commands(*args, **kwargs)

            checking.enter_context(patch.object(verification, "run_verification_commands", watch_before_commands))
            diff = checking.enter_context(patch.object(git, "diff_bytes", wraps=git.diff_bytes))
            with self.assertRaises(HarnessError):
                engine.start("Implement greet", plan)
            self.assertTrue((engine.worktree / self.pipe_name).is_fifo())
            diff.assert_not_called()

        saved = read_json(engine.directory / "state.json")
        stop = saved["stop"]
        self.assertEqual((saved["status"], stop["kind"], stop["reason"]),
                         ("blocked", "verification", "checks_modified_files"))
        self.assertEqual([entry["role"] for entry in saved["dispatches"]], ["worker", "verification"])
        self.assertEqual(stop["source"]["task_id"], "greet")
        artifact = Path(stop["source"]["artifact"])
        self.assertEqual(artifact.name, "file-changes.json")
        self.assertEqual(read_json(artifact)["deleted"], [self.pipe_name])
        (check,) = read_json(artifact.with_name("results.json"))
        self.assertTrue(checks_passed([check]))


class VerificationFeedbackCommitTests(unittest.TestCase):
    command = {"cwd": ".", "argv": ["check"]}
    supervisor_fault = {"step": "persist_outcome", "type": "OSError", "message": "outcome fsync", "errno": 5}

    def context_and_workflow(self, root, dispatches=()):
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve()
        context = object.__new__(RunContext)
        context.state = root
        context.store = RunStore(root)
        context.run = {"id": "run_feedback", "dispatches": list(dispatches), "status": "running"}
        context.config = SimpleNamespace(data={"runtime": {"verify_timeout_seconds": 1}})
        context.on_save = None
        context.checkpoint = lambda: None
        context.event = lambda *args, **kwargs: None
        context.save()
        lifecycle = SimpleNamespace(settle_stop=lambda error: stops.of(error))
        return context, Workflow(context, SimpleNamespace(lifecycle=lifecycle))

    def exercise(self, root, process_type, fact, write, fault=None, followup=None):
        context, workflow = self.context_and_workflow(root)
        root = context.state
        verification = Verification(context)
        folder = root / "checks"
        if process_type in CHECK_FAILURES:
            process_error = check_failure(process_type, folder)
        elif process_type is not None:
            process_error = process_type("process failure")
        else:
            process_error = None
        if isinstance(process_error, ProcessStartError) and not isinstance(process_error, CHECK_FAILURES):
            process_error.__cause__ = (ValueError("local startup argument") if fact in {"invalid_argument", "invalid_argument_evidence"}
                                       else OSError(errno.EAGAIN, "local startup failure"))
        write_step, write_type = write if write is not None else (None, None)
        write_error = write_type(f"{write_step} failed") if write_type else None
        timeout = process_type is ProcessTimeout
        startup = isinstance(process_error, (ProcessStartError, CommandDirectoryError))
        interrupted = isinstance(process_error, (KeyboardInterrupt, SystemExit))
        evidence_fault = fact in {"evidence", "invalid_argument_evidence"}
        reason = ("timeout" if timeout else "start_failed" if startup else
                  "cancelled" if process_type is KeyboardInterrupt else "interrupted" if interrupted else "normal")
        outcome = ProcessOutcome(
            started=not startup, exit_confirmed=fact != "unconfirmed", termination_reason=reason,
            exit_code=None if startup or timeout else 0, evidence_complete=not evidence_fault,
            errors=[self.supervisor_fault] if evidence_fault else [],
        )

        def process(*args, **kwargs):
            if fact != "no_outcome":
                kwargs["on_outcome"](outcome)
                if process_error is not None:
                    process_error.outcome = outcome
            if process_error is not None:
                raise process_error
            return 0

        write_faults, feedback_faults = [], []
        feedback_error = (KeyboardInterrupt("feedback interrupted") if followup == "interrupt"
                          else OSError(errno.EIO, "feedback write failed"))
        stop_error = OSError(errno.EIO, "stop write failed")
        replace = state_files.LockedDirectory.replace

        def fail_write(step):
            if write_step == step and not write_faults:
                write_faults.append(write_error)
                raise write_error

        def excerpt(path):
            fail_write("excerpt")
            return tail(path)

        def write_results(path, value):
            if value:
                fail_write("results")
            write_json(path, value)

        def fail_feedback():
            if "feedback" in context.run and not feedback_faults:
                feedback_faults.append(feedback_error)
                raise feedback_error

        def read_ledger(real, fd, size, offset):
            if fault == "F1":
                fail_feedback()
            return real(fd, size, offset)

        def write_temp(real, fd, data):
            if fault == "F2":
                fail_feedback()
            return real(fd, data)

        def write_ledger(locked, name, data):
            if context.run["dispatches"] and "process_outcome" in context.run["dispatches"][-1]:
                fail_write("ledger")
            if followup == "stop_failure" and context.run["status"] == "blocked":
                raise stop_error
            return replace(locked, name, data)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                patch("harness.verification.git.open_bound_root", side_effect=open_root), \
                patch("harness.verification.run_process", process), \
                patch("harness.verification.tail", excerpt), \
                patch("harness.verification.write_json", write_results), \
                patch.object(state_files.LockedDirectory, "replace", write_ledger), \
                patch.object(state_files, "os", FakeOs(pread=read_ledger, write=write_temp)):
            try:
                verification.run_verification_commands("task", [self.command], root, folder, {})
            except BaseException as error:
                selected = error
                if followup == "replace_feedback":
                    context.run["feedback"] = {"task_id": "task", "verification_error": {
                        "error": "new feedback", "command": self.command}}
                    context.save()
                if followup == "stop_failure":
                    with self.assertRaises(OSError) as caught:
                        workflow.stop(error)
                    self.assertIs(caught.exception, stop_error)
                else:
                    self.assertIs(workflow.stop(error), Workflow.WRITTEN)
            else:
                selected = None

        self.assertEqual(write_faults, [write_error] if write_error is not None else [])
        if write_step == "ledger":
            self.assertEqual(stop_of(write_error), ("runtime_failure", "invariant"))
        ledger = read_json(context.directory / "state.json")
        row, = read_json(folder / "results.json")
        entry, = ledger["dispatches"]
        if interrupted:
            self.assertIs(selected, process_error)
            self.assertEqual(stop_of(selected), ("interrupted", "signal"))
            self.assertEqual(ledger["stop"]["data"]["exits_confirmed"], fact not in {"no_outcome", "unconfirmed"})
            self.assertNotIn(write_error, traceback_chain(selected))
            for record in (row, entry):
                self.assertEqual(record["status"], "interrupted")
                self.assertEqual(record.get("persistence_error"), str(write_error) if write is not None else None)
                self.assertEqual(record["termination_reason"], reason)
                if fact == "no_outcome":
                    self.assertFalse(record["exit_confirmed"])
                    self.assertFalse(record["evidence_complete"])
                    self.assertIsNone(record["exit_code"])
        if selected is None or followup == "stop_failure":
            self.assertEqual(ledger["status"], "running")
            self.assertNotIn("feedback", ledger)
            self.assertNotIn("stop", ledger)
        else:
            feedback = ledger["feedback"]["verification_error"]
            self.assertEqual(feedback["command"], self.command)
            self.assertEqual(feedback["error"],
                             "new feedback" if followup == "replace_feedback" else
                             str(process_error) if followup == "interrupt" else str(selected))
            self.assertEqual((ledger["stop"]["kind"], ledger["stop"]["reason"]), stop_of(selected))
        injected = selected is not None and fault is not None
        self.assertEqual(feedback_faults, [feedback_error] if injected else [])
        self.assertEqual("验证反馈的提交失败" in stderr.getvalue(), injected and followup != "interrupt")
        if followup == "interrupt":
            self.assertIs(selected, feedback_error)
            self.assertEqual(ledger["status"], "interrupted")
        elif selected is not None:
            links = [selected]
            for link in links:
                for related in (link.__cause__, link.__context__):
                    if related is not None and all(related is not item for item in links):
                        links.append(related)
            self.assertNotIn(feedback_error, links)

        def identity(error):
            if error is None:
                return None
            if error is process_error:
                return "process"
            if error is write_error:
                return "write"
            return type(error), str(error).replace(str(folder), "<checks>").replace(entry["id"], "<dispatch>")

        error_record = ((identity(selected), stop_of(selected), identity(selected.__cause__),
                         identity(selected.__context__), selected.__suppress_context__) if selected is not None else None)
        return error_record, [
            {key: value for key, value in record.items()
             if key not in {"started_at", "finished_at", "duration_seconds", "log", "timing_source", "id", "worktree"}}
            for record in (row, entry)]

    def test_feedback_failure_at_read_or_before_replace_preserves_the_k6_matrix(self):
        writes = (None, ("results", OSError), ("results", HarnessError), ("results", lambda _: OSError()),
                  ("excerpt", OSError), ("ledger", HarnessError))
        process_facts = [(None, None)]
        process_facts += [(process, fact) for process in CHECK_FAILURES for fact in (None, "evidence")]
        process_facts.append((ProcessTimeout, "unconfirmed"))
        process_facts += [(ProcessStartError, fact)
                          for fact in (None, "evidence", "invalid_argument", "invalid_argument_evidence")]
        process_facts += [(process, fact) for process in (KeyboardInterrupt, SystemExit, ValueError, HarnessError)
                          for fact in (None, "evidence", "unconfirmed")]
        process_facts += [(process, "no_outcome") for process in (KeyboardInterrupt, SystemExit)]
        cases = [(process, fact, write) for process, fact in process_facts for write in writes]
        for process, fact, write in cases:
            with self.subTest(process=process, fact=fact, write=write), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                baseline = self.exercise(root / "baseline", process, fact, write)
                for fault in ("F1", "F2"):
                    with self.subTest(fault=fault):
                        self.assertEqual(self.exercise(root / fault, process, fact, write, fault), baseline)

    def test_clean_exit_gate_publishes_and_dirty_exit_gate_is_recomputed_on_the_stop_base(self):
        for dirty, live, interrupt in ((False, False, False), (True, False, False),
                                       (True, False, True), (True, True, False)):
            with self.subTest(dirty=dirty, live=live, interrupt=interrupt), tempfile.TemporaryDirectory() as directory:
                entries = [{"id": f"dispatch-{pgid}", "pgid": pgid, "process_state": "running", "exit_confirmed": False}
                           for pgid in (12345, 12346)]
                context, workflow = self.context_and_workflow(Path(directory), entries)
                ledger = context.directory / "state.json"
                if dirty:
                    context.run["feedback"] = {"partway": "unsaved"}
                stop = KeyboardInterrupt("interrupted") if interrupt else OSError(errno.EIO, "stage failed")
                with patch("harness.run_context.group_alive", side_effect=lambda pgid: live and pgid == 12346):
                    if live:
                        with self.assertRaises(HarnessError):
                            context.ensure_exited()
                    else:
                        context.ensure_exited()
                    self.assertEqual([item["exit_confirmed"] for item in context.run["dispatches"]], [not live] * 2)
                    in_gate = read_json(ledger)
                    self.assertEqual([item["exit_confirmed"] for item in in_gate["dispatches"]], [not dirty] * 2)
                    self.assertEqual(in_gate["store_revision"], 1 if dirty else 2)
                    self.assertIs(workflow.stop(stop), Workflow.WRITTEN)
                saved = read_json(ledger)
                self.assertEqual([item["exit_confirmed"] for item in saved["dispatches"]], [not live] * 2)
                self.assertNotIn("feedback", saved)
                self.assertEqual(saved["store_revision"], 2 if dirty else 3)
                if interrupt:
                    self.assertTrue(saved["stop"]["data"]["exits_confirmed"])

    def test_process_fact_commit_contributes_only_a_registered_candidate_to_the_stop(self):
        for failure, later_commit in (("replace", False), ("read", False), ("read", True)):
            with self.subTest(failure=failure, later_commit=later_commit), tempfile.TemporaryDirectory() as directory:
                entry = {"id": "dispatch-running", "pgid": None, "process_state": "starting", "exit_confirmed": False}
                context, workflow = self.context_and_workflow(Path(directory), [entry])
                ledger = context.directory / "state.json"
                error = OSError(errno.EIO, "process fact commit failed")
                entry.update(process_state="exited", exit_confirmed=True)
                with patch.object(state_files.LockedDirectory, failure, side_effect=error), self.assertRaises(OSError):
                    context.save()
                if later_commit:
                    context.save()
                context.run["feedback"] = {"half": "step"}
                self.assertIs(workflow.stop(error), Workflow.WRITTEN)
                saved = read_json(ledger)
                published = failure == "replace" or later_commit
                self.assertEqual(saved["dispatches"][0]["exit_confirmed"], published)
                self.assertEqual(saved["dispatches"][0]["process_state"], "exited" if published else "starting")
                self.assertNotIn("feedback", saved)
                self.assertEqual(saved["store_revision"], 3 if later_commit else 2)
                if not published:
                    with self.assertRaises(HarnessError) as unconfirmed:
                        context.ensure_exited()
                    self.assertEqual(stop_of(unconfirmed.exception), ("process_unconfirmed", "dispatch_exit"))
                    context.confirm_exited([entry["id"]])
                    self.assertTrue(read_json(ledger)["dispatches"][0]["exit_confirmed"])

    def test_feedback_failure_followed_by_stop_failure_update_or_signal(self):
        for fault, followup in (("F1", "stop_failure"), ("F2", "stop_failure"),
                                ("F1", "replace_feedback"), ("F1", "interrupt")):
            with self.subTest(fault=fault, followup=followup), tempfile.TemporaryDirectory() as directory:
                self.exercise(Path(directory), ValueError, None, None, fault, followup)


class StopSaveChainTests(unittest.TestCase):
    def test_an_interrupt_while_a_refused_stop_save_unlocks_keeps_the_refusal_in_its_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"id": "run_chain", "store_revision": 0, "dispatches": []}
            store = RunStore(Path(directory))
            store.attach(run)
            store.commit()
            ledger = Path(directory) / "runs" / run["id"] / "state.json"
            written = ledger.read_bytes() + b"\n"
            ledger.write_bytes(written)   # another writer: the stop's save is refused
            interrupt, locked = KeyboardInterrupt("at store unlock"), []

            def lock(real, fd, operation):
                real(fd, operation)
                if run.get("status") == "blocked":
                    locked.append(fd)

            def unlock(real, fd):  # store.lock is released when its descriptor closes
                real(fd)
                if fd in locked:
                    locked.clear()
                    raise interrupt
            workflow = Workflow(SimpleNamespace(run=run, store=store, save=store.commit),
                                SimpleNamespace(lifecycle=SimpleNamespace(settle_stop=lambda error: run["stop"])))
            original = HarnessError("verification stop")
            with self.assertRaises(KeyboardInterrupt) as caught:
                try:
                    raise original
                except HarnessError as error:
                    with patch.object(state_files, "fcntl", FakeFcntl(lock)), \
                            patch.object(state_files, "os", FakeOs(close=unlock)):
                        workflow.stop(error)
            self.assertIs(caught.exception, interrupt)
            self.assertEqual(ledger.read_bytes(), written)
            refusal = interrupt.__context__   # the refused save, unwinding when the interrupt arrived
            self.assertIsInstance(refusal, HarnessError)
            chain = traceback_chain(interrupt)
            self.assertEqual([id(link) for link in chain], [id(interrupt), id(refusal), id(original)])
            self.assertIn(str(refusal), "".join(traceback.format_exception(interrupt)))
