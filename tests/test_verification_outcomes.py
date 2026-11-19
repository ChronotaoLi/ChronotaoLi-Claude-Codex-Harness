import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import stops
from harness.process_runtime import ProcessOutcome
from harness.run_store import RunStore
from harness.util import CommandDirectoryError, HarnessError, ProcessStartError, ProcessTimeout, read_json, tail, write_json
from harness.verification import Verification, checks_passed
from harness.workflow import Workflow
from test_harness import ROOT, RepoCase


# Rule 10's facts for a command whose directory could not be opened, and the reasons its record tells apart.
NOT_STARTED = {"status": "failed", "termination_reason": "start_failed", "exit_code": None,
               "exit_confirmed": True, "evidence_complete": True}
REASONS = ("不存在", "不是目录", "是符号链接", "无权限", "fchdir 失败")


def open_root(held, root, binding):
    # The bound root's descriptor, opened into the holder the code under test closes.
    return held.open(root, os.O_RDONLY | os.O_DIRECTORY)


class VerificationOutcomeTests(unittest.TestCase):
    def fixture(self, root):
        context = SimpleNamespace(
            run={"dispatches": []}, config=SimpleNamespace(data={"runtime": {"verify_timeout_seconds": 1}}),
            ensure_exited=lambda: None, save=lambda: None)
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
        cells = [(timeout, fact, write) for timeout in (True, False) for fact in (None, "unconfirmed", "evidence")
                 for write in writes if (fact or write) and (timeout or fact != "unconfirmed")]
        for timeout, fact, write in cells:
            with self.subTest(process="timeout" if timeout else "directory", fact=fact,
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
                process = ProcessTimeout(1, folder / "check-0") if timeout else CommandDirectoryError("missing directory")
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
        for timeout in (True, False):
            for steps in (("excerpt", "results"), ("results", "ledger"), ("excerpt", "ledger")):
                with self.subTest(process="timeout" if timeout else "directory", failing=steps), \
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
                    process = ProcessTimeout(1, folder / "check-0") if timeout else CommandDirectoryError("missing directory")
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
        for timeout, fact, refusal in [(t, f, r) for t in (True, False) for f in (None, "evidence")
                                       for r in ("another writer", "untyped")]:
            with self.subTest(process="timeout" if timeout else "directory", fact=fact, refusal=refusal), \
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
                process = ProcessTimeout(1, folder / "check-0") if timeout else CommandDirectoryError("missing directory")
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

    def test_a_start_failure_or_an_interrupt_with_a_failed_write_is_raised_itself(self):
        for name in ("missing executable", "interrupt"):
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                verification, folder = self.fixture(Path(directory))
                failed = []

                def persist(path, value):
                    if value and not failed:
                        failed.append(OSError("results fsync"))
                        raise failed[0]
                    write_json(path, value)
                with patch("harness.verification.write_json", side_effect=persist), \
                        self.assertRaises(BaseException) as caught:
                    if name == "interrupt":
                        interrupt = KeyboardInterrupt()
                        self.run_fake(verification, folder, ProcessOutcome(started=True, exit_code=-2, exit_confirmed=True,
                                      termination_reason="cancelled"), interrupt)
                    else:
                        with patch("harness.verification.git.open_bound_root", side_effect=open_root):
                            verification.run_verification_commands(
                                "task", [{"cwd": ".", "argv": [str(Path(directory) / "absent-executable")]}],
                                Path(directory), folder, {})
                raised = caught.exception
                if name == "interrupt":
                    self.assertIs(raised, interrupt)
                else:
                    self.assertIs(type(raised), ProcessStartError)
                self.assertNotIn(failed[0], traceback_chain(raised))
                self.assertEqual(verification.context.run["feedback"]["verification_error"]["error"], str(raised))

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
            interrupt, flock = KeyboardInterrupt("at store unlock"), fcntl.flock

            def unlock(fd, operation):
                flock(fd, operation)
                if operation == fcntl.LOCK_UN:
                    raise interrupt
            workflow = Workflow(SimpleNamespace(run=run, save=store.commit),
                                SimpleNamespace(lifecycle=SimpleNamespace(settle_stop=lambda error: run["stop"])))
            original = HarnessError("verification stop")
            with self.assertRaises(KeyboardInterrupt) as caught:
                try:
                    raise original
                except HarnessError as error:
                    with patch("harness.util.fcntl.flock", side_effect=unlock):
                        workflow.stop(error)
            self.assertIs(caught.exception, interrupt)
            self.assertEqual(ledger.read_bytes(), written)
            refusal = interrupt.__context__   # the refused save, unwinding when the interrupt arrived
            self.assertIsInstance(refusal, HarnessError)
            chain = traceback_chain(interrupt)
            self.assertEqual([id(link) for link in chain], [id(interrupt), id(refusal), id(original)])
            self.assertIn(str(refusal), "".join(traceback.format_exception(interrupt)))
