import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from harness import state_files, stops, util
from harness.cleanup import cleanup_run
from harness.command_server import Broker, command_id, create_manifest, ensure_command_processes_exited
from harness.process_runtime import ProcessOutcome, ProcessSupervisor
from harness.state_files import StateReadError
from harness.util import EvidenceError, HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_readonly_cleanup import tree_bytes
from test_state_files import FakeOs


NEVER_STARTED = ProcessOutcome(startup_state="not_started", exit_confirmed=True).to_dict()


class SupervisorRecordGateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.state = self.root / "state"
        self.directory = self.state / "runs" / "run_fixture"
        self.job = self.directory / "dispatches" / "model"
        self.job.mkdir(parents=True)
        self.model = {"id": self.job.name, "role": "commander", "pgid": None,
                      "process_state": "starting", "exit_confirmed": False}

    def gate(self, entries=None, **options):
        return util.ensure_dispatches_exited([self.model] if entries is None else entries,
                                            directory=options.pop("directory", self.directory),
                                            state=options.pop("state", self.state), **options)

    def command(self):
        folder = self.job / "commands" / ("a" * 32)
        folder.mkdir(parents=True)
        ident = self.model["id"] + ":command:" + folder.name
        write_json(folder / "process.json", {"id": ident, "command_id": "fixture", "pgid": None,
                                             "process_state": "starting", "exit_confirmed": False})
        return dict(self.model, command_directory=str(folder.parent)), folder, ident

    def assert_stop(self, error, kind, reason):
        self.assertEqual((error.stop["kind"], error.stop["reason"]), (kind, reason))

    def test_uncertain_records_require_confirmation_without_observing_or_writing(self):
        parent, folder, call = self.command()
        for entry, file, reason in (
            (self.model, self.job / "process.outcome.json", "dispatch_exit"),
            (dict(parent, exit_confirmed=True), folder / "output.outcome.json", "command_exit"),
        ):
            started = dict(NEVER_STARTED, started=True, startup_state="started", pid=100, pgid=100)
            cases = {
                "missing": None, "fifo": lambda: os.mkfifo(file),
                "link": lambda: file.symlink_to(self.root / "valid.json"),
                "directory": file.mkdir,
                "bad JSON": b"{", "bad UTF8": b"\xff", "array": b"[]",
                "duplicate": b'{"x": 1, "x": 2}', "non-finite": b'{"x": 1e999}',
                "too large": b" " * (16 * 1024 * 1024 + 1),
                "starting": ProcessOutcome().to_dict(),
                "not started with group": dict(NEVER_STARTED, pgid=100),
                "not started with PID": dict(NEVER_STARTED, pid=100),
                "missing PID": {key: value for key, value in NEVER_STARTED.items() if key != "pid"},
                "unconfirmed not started": dict(NEVER_STARTED, exit_confirmed=False),
                "started without group": dict(started, pgid=None),
                "bool group": dict(started, pid=True, pgid=True),
                "float leader": dict(started, pid=100.0),
                "mismatched leader": dict(started, pid=101),
                "out of range": dict(started, pid=2**31, pgid=2**31),
            }
            write_json(self.root / "valid.json", NEVER_STARTED)
            for name, value in cases.items():
                with self.subTest(reason=reason, case=name):
                    if callable(value):
                        value()
                    elif isinstance(value, bytes):
                        file.write_bytes(value)
                    elif value is not None:
                        write_json(file, value)
                    before = tree_bytes(self.directory)
                    with self.assertRaises(HarnessError) as raised:
                        self.gate([entry], observe=lambda pgid: self.fail(f"unexpected observation {pgid}"))
                    self.assert_stop(raised.exception, "process_unconfirmed", reason)
                    self.assertEqual(raised.exception.stop["source"], {"dispatch_id": self.model["id"]})
                    if reason == "command_exit":
                        self.assertEqual(raised.exception.stop["data"]["command_id"], call)
                    self.assertEqual(tree_bytes(self.directory), before)
                    if file.is_dir() and not file.is_symlink():
                        file.rmdir()
                    else:
                        file.unlink(missing_ok=True)

    def test_verification_record_safe_root_and_copy_precedence(self):
        relative = "verification/tasks/task/check-0.outcome.json"
        entry = dict(self.model, role="verification", process_record=relative)
        path = self.directory / relative
        elsewhere = self.root / "elsewhere"
        write_json(elsewhere / path.name, dict(NEVER_STARTED, owner=entry["id"]))
        path.parent.parent.mkdir(parents=True)
        path.parent.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(HarnessError) as raised:
            self.gate([entry])
        self.assert_stop(raised.exception, "process_unconfirmed", "dispatch_exit")
        path.parent.unlink()
        path.parent.mkdir()
        path.symlink_to(elsewhere / path.name)
        with self.assertRaises(HarnessError) as raised:
            self.gate([entry])
        self.assert_stop(raised.exception, "process_unconfirmed", "dispatch_exit")
        path.unlink()
        write_json(path, dict(NEVER_STARTED, owner=entry["id"]))
        seen = []

        def no_read(real, name, *args, **kwargs):
            self.fail(f"unexpected evidence read: {name}")

        with patch.object(state_files, "os", FakeOs(open=no_read)):
            copied = dict(entry, pgid=301)
            self.assertEqual(self.gate([copied], observe=lambda pgid: seen.append(pgid) or False), [copied])
            self.assertEqual(self.gate([dict(entry, exit_confirmed=True), dict(entry, process_state="prepared")]), [])
        self.assertEqual(seen, [301])
        self.assertEqual(self.gate([entry]), [entry])

    def test_eio_remains_typed_and_child_is_checked_before_its_parent(self):
        parent, folder, _ = self.command()
        model = self.job / "process.outcome.json"
        child = folder / "output.outcome.json"
        for path in (child, model):
            write_json(path, NEVER_STARTED)

        seen = []

        def open_(real, name, *args, **kwargs):
            if name in {child.name, model.name}:
                seen.append(name)
                if name == failure.name:
                    raise OSError(errno.EIO, os.strerror(errno.EIO))
            return real(name, *args, **kwargs)

        for failure, order in ((child, [child.name]), (model, [child.name, model.name])):
            with self.subTest(failure=failure.name), patch.object(state_files, "os", FakeOs(open=open_)):
                seen.clear()
                with self.assertRaises(StateReadError) as raised:
                    self.gate([parent])
                self.assert_stop(raised.exception, "runtime_failure", "io")
                self.assertEqual((raised.exception.outcome.path, raised.exception.outcome.errno), (str(failure), errno.EIO))
                self.assertEqual(seen, order)

    def test_shared_root_is_lazy_and_dispatch_order_is_preserved(self):
        parent, folder, _ = self.command()
        other_job = self.job.with_name("first")
        write_json(other_job / "process.outcome.json", NEVER_STARTED)
        never_read = dict(self.model, id="prepared", process_state="prepared")
        first = dict(self.model, id=other_job.name)
        alias = self.root / "alias"
        alias.symlink_to(self.state)
        write_json(self.job / "process.outcome.json", NEVER_STARTED)
        write_json(folder / "output.outcome.json", NEVER_STARTED)
        seen = []

        def open_(real, name, *args, **kwargs):
            if name in {str(self.state), "process.json", "process.outcome.json", "output.outcome.json"}:
                seen.append(name)
            return real(name, *args, **kwargs)

        with patch.object(state_files, "os", FakeOs(open=open_)):
            self.assertEqual(self.gate([never_read, dict(first, exit_confirmed=True)], state=alias), [])
            self.assertEqual(seen, [])
            self.assertEqual(self.gate(iter([first, parent]), state=alias), [first, parent])
            self.assertEqual(seen, [str(self.state), "process.outcome.json", "process.json",
                                    "output.outcome.json", "process.outcome.json"])
            (other_job / "process.outcome.json").unlink()
            seen.clear()
            with self.assertRaises(HarnessError) as raised:
                self.gate(iter([first, parent]), state=alias)
            self.assertEqual(raised.exception.stop["source"], {"dispatch_id": first["id"]})
            self.assertEqual(seen, [str(self.state), "process.outcome.json"])

    def test_only_evidence_bound_to_the_supplied_run_is_used(self):
        write_json(self.job / "process.outcome.json", NEVER_STARTED)
        for ident in ("", ".", "..", "other/model", "other\\model", "nul\0"):
            with self.subTest(ident=ident), self.assertRaises(EvidenceError):
                self.gate([dict(self.model, id=ident)])
        with self.assertRaises(EvidenceError):
            self.gate(directory=self.root / "elsewhere")
        with self.assertRaises(HarnessError) as raised:
            self.gate(directory=self.directory.with_name("run_other"))
        self.assert_stop(raised.exception, "process_unconfirmed", "dispatch_exit")
        moved = self.root / "other_dispatch"
        self.job.rename(moved)
        self.job.symlink_to(moved)
        with self.assertRaises(HarnessError) as raised:
            self.gate()
        self.assert_stop(raised.exception, "process_unconfirmed", "dispatch_exit")

    def test_supervisor_group_is_observed_even_if_the_record_claims_exit(self):
        # Keep ownership in this process so wait() certainly reaps the controlled child.
        from harness.process_runtime import ProcessSupervisor

        parent, folder, call = self.command()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        try:
            for log in (self.job / "process", folder / "output"):
                supervisor = ProcessSupervisor(log)
                supervisor.started(child)
                record = read_json(supervisor.path)
                self.assertEqual((record["pid"], record["pgid"]), (child.pid, child.pid))
                # Simulate a stale terminal record whose group number is now present.
                write_json(supervisor.path, dict(record, exit_confirmed=True))
            for entries, reason, ident in (([self.model], "dispatch_exit", None),
                                           ([dict(parent, exit_confirmed=True)], "command_exit", call)):
                with self.assertRaises(HarnessError) as raised:
                    self.gate(entries)
                self.assert_stop(raised.exception, "process_unconfirmed", reason)
                self.assertIn(str(child.pid), str(raised.exception))
                if ident:
                    self.assertEqual(raised.exception.stop["data"]["command_id"], ident)
        finally:
            child.terminate()
            child.wait(timeout=5)
        self.assertEqual(self.gate([parent]), [parent])

    def test_interruption_after_the_supervisor_writes_a_started_record(self):
        seen = []

        def before_ledger_update(pgid):
            with self.assertRaises(HarnessError) as raised:
                self.gate()
            self.assert_stop(raised.exception, "process_unconfirmed", "dispatch_exit")
            self.assertIn(str(pgid), str(raised.exception))
            seen.append(pgid)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            util.run_process([sys.executable, "-c", "import time; time.sleep(30)"],
                             self.root, self.job / "process", on_start=before_ledger_update)
        record = read_json(self.job / "process.outcome.json")
        self.assertEqual(seen, [record["pgid"]])
        self.assertEqual(self.gate(), [self.model])
        self.assertFalse(self.model["exit_confirmed"])

    def test_broker_startup_failed_before_journal_finish_and_receipt_is_still_required(self):
        argv = {"cwd": ".", "argv": [str(self.root / "missing-program")]}
        config = {"verification": {"commands": [argv]}, "permissions": {"worker_commands": []},
                  "runtime": {"verify_timeout_seconds": 5}}
        metadata = create_manifest(config, self.root, self.job)
        code = """
import os
import sys
from unittest.mock import patch
from harness.command_server import Broker
from harness.process_runtime import ProcessSupervisor

finish_callback = ProcessSupervisor.__init__
def die_after_supervisor_write(self, log, **kwargs):
    finish_callback(self, log, **{**kwargs, "on_outcome": lambda _: os._exit(67)})

with patch.object(ProcessSupervisor, "__init__", die_after_supervisor_write):
    Broker(sys.argv[1], sys.argv[2]).run_command({"command_id": sys.argv[3]})
"""
        args = {"command_id": command_id(argv)}
        process = subprocess.run([sys.executable, "-c", code, metadata["artifact"],
                                  metadata["artifact_digest"], args["command_id"]], timeout=15)
        self.assertEqual(process.returncode, 67)
        [record] = ensure_command_processes_exited(self.job, state=self.state)
        self.assertEqual((record["process_state"], record["pgid"], record["exit_confirmed"]), ("starting", None, False))
        folder = Path(record["journal"]).parent
        self.assertEqual(read_json(folder / "output.outcome.json")["startup_state"], "not_started")
        self.assertFalse((folder / "receipt.json").exists())
        before = tree_bytes(self.directory)
        with self.assertRaises(HarnessError) as raised:
            Broker(metadata["artifact"], metadata["artifact_digest"]).run_command(args)
        self.assertIsInstance(raised.exception.__cause__, EvidenceError)
        self.assertEqual(tree_bytes(self.directory), before)


class ProcessRecordLifecycleTests(RepoCase):
    def interrupted_model(self):
        engine = self.engine()
        engine.start("Implement greet", None, pause_after_plan=True)
        entry = engine.run["dispatches"][0]
        directory = engine.directory
        job = directory / "dispatches" / entry["id"]
        entry.update(pgid=None, process_state="starting", exit_confirmed=False)
        write_json(directory / "state.json", engine.run)
        code = """
import os
from pathlib import Path
import sys
from harness.util import run_process
run_process([sys.argv[2]], Path.cwd(), Path(sys.argv[1]), on_outcome=lambda _: os._exit(67))
"""
        process = subprocess.run([sys.executable, "-c", code, str(job / "process"),
                                  str(self.outside / "missing-program")], timeout=15)
        self.assertEqual(process.returncode, 67)
        self.assertEqual(read_json(job / "process.outcome.json")["startup_state"], "not_started")
        return engine, entry["id"]

    def test_resume_accepts_startup_failure_before_ledger_update(self):
        engine, ident = self.interrupted_model()
        resumed = self.engine().resume(engine.run["id"], approve_plan=True)
        self.assertEqual(resumed["status"], "completed")
        entry = next(entry for entry in resumed["dispatches"] if entry["id"] == ident)
        self.assertIs(entry["exit_confirmed"], True)
        self.assertNotIn("operator_exit_confirmation", entry)

    def test_abandon_and_cleanup_preview_accept_startup_failure_without_confirmation(self):
        engine, ident = self.interrupted_model()
        abandoned = self.engine().abandon(engine.run["id"])
        self.assertEqual(abandoned["status"], "abandoned")
        entry = next(entry for entry in abandoned["dispatches"] if entry["id"] == ident)
        self.assertIs(entry["exit_confirmed"], True)
        self.assertNotIn("operator_exit_confirmation", entry)
        # Recreate the stale exit copy on the abandoned ledger to exercise preview independently.
        entry["exit_confirmed"] = False
        write_json(engine.directory / "state.json", abandoned)
        before = tree_bytes(engine.directory)
        self.assertEqual(cleanup_run(self.repo, abandoned["id"])["worktree"]["status"], "eligible")
        self.assertEqual(tree_bytes(engine.directory), before)

    def test_run_context_registers_only_on_a_clean_entry(self):
        engine, ident = self.interrupted_model()
        reader = self.engine()
        reader.load_run(engine.run["id"])
        path = reader.directory / "state.json"
        before = path.read_bytes()
        entry = next(entry for entry in reader.run["dispatches"] if entry["id"] == ident)
        goal = reader.run["goal"]

        def check():
            reader.run["goal"] = "an unrelated staged change"
            reader.context.ensure_exited()
            self.assertIs(entry["exit_confirmed"], True)
            self.assertEqual(path.read_bytes(), before)

            reader.run["goal"] = goal
            entry["exit_confirmed"] = False
            reader.context.ensure_exited()
            after = read_json(path)
            self.assertIs(next(row for row in after["dispatches"] if row["id"] == ident)["exit_confirmed"], True)
            self.assertEqual(after["goal"], goal)

        reader.context.run_lock(stops.make("busy", "run_lock"), check)

    def test_quota_read_gate_and_wake_checks_leave_the_stale_copy_unchanged(self):
        engine, ident = self.interrupted_model()
        reader = self.engine()
        reader.load_run(engine.run["id"])
        before = tree_bytes(reader.directory)
        self.assertTrue(reader.services.quota.no_live_model_process())
        reader.services.lifecycle.wake_checks()
        self.assertIs(next(entry for entry in reader.run["dispatches"] if entry["id"] == ident)["exit_confirmed"], False)
        self.assertEqual(tree_bytes(reader.directory), before)



class VerificationRecordLifecycleTests(RepoCase):
    def paused(self):
        engine = self.engine()
        engine.start("Implement greet", None, pause_after_plan=True)
        return engine

    def interrupt_check(self, engine, mode, attempt=1):
        release = self.outside / "release-verification"
        run_id = engine.run["id"]
        code = """
from contextlib import nullcontext
import errno
import os
from pathlib import Path
import sys
from unittest.mock import patch
from harness import verification
from harness.adapters import DemoBackend
from harness.config import Config
from harness.runtime import Runtime

repo, run_id, mode, release, attempt = sys.argv[1:]
config = Config.load(Path("harness.toml"))
engine = Runtime(config, Path(repo), backend=DemoBackend())
engine.load_run(run_id)
service = engine.services.verification
folder = service.verification_directory("greet", int(attempt))
run_process = verification.run_process

def die(*args):
    os._exit(67)

def command(argv, cwd, log, **kwargs):
    if mode == "reused":
        die()
    kwargs["on_start" if mode == "live" else "on_outcome"] = die
    return run_process(argv, cwd, log, **kwargs)

def fail_open(*args, **kwargs):
    raise OSError(errno.EIO, "local command directory read")

argv = [str(Path(release).with_name("no-such-executable"))]
if mode == "live":
    argv = [sys.executable, "-c", '''
import sys
import time
from pathlib import Path

release = Path(sys.argv[1])
deadline = time.monotonic() + 30
while not release.exists() and time.monotonic() < deadline:
    time.sleep(.02)
''', release]
fault = patch("harness.util.open_command_directory", fail_open) if mode == "local_io" else nullcontext()
with patch.object(verification, "run_process", command), fault:
    service.run_verification_commands("greet", [{"cwd": ".", "argv": argv}],
                                       engine.worktree, folder, engine.run["worktree_binding"])
"""
        parent = subprocess.run(
            [sys.executable, "-c", code, str(self.repo), run_id, mode, str(release), str(attempt)], timeout=15)
        self.assertEqual(parent.returncode, 67)
        entry = read_json(engine.directory / "state.json")["dispatches"][-1]
        path = engine.directory / entry["process_record"]
        self.assertEqual(entry["process_state"], "starting")
        self.assertIsNone(entry["pgid"])
        self.assertNotIn("process_outcome", entry)
        self.assertEqual(entry["process_record"], f"verification/tasks/greet/{attempt}/check-0.outcome.json")
        return entry, path, release

    def gate(self, engine, entry):
        return util.ensure_dispatches_exited([entry], state=engine.context.state, directory=engine.directory)

    def assert_stop(self, expected, use):
        with self.assertRaises(HarnessError) as caught:
            use()
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), expected)
        return caught.exception

    def test_native_startup_failure_passes_readonly_wake_and_resumes(self):
        engine = self.paused()
        entry, path, _ = self.interrupt_check(engine, "missing")
        record = read_json(path)
        self.assertEqual((record["owner"], record["startup_state"], record["errors"][0]["type"]),
                         (entry["id"], "not_started", "ProgramStartError"))
        reader = self.engine()
        reader.load_run(engine.run["id"])
        before = tree_bytes(engine.directory)
        self.assertTrue(reader.services.quota.no_live_model_process())
        reader.services.lifecycle.wake_checks()
        saved = next(row for row in reader.run["dispatches"] if row["id"] == entry["id"])
        self.assertIsNot(saved.get("exit_confirmed"), True)
        self.assertEqual(tree_bytes(engine.directory), before)
        resumed = self.engine().resume(engine.run["id"], approve_plan=True)
        self.assertEqual(resumed["status"], "completed")
        saved = next(row for row in resumed["dispatches"] if row["id"] == entry["id"])
        self.assertIs(saved["exit_confirmed"], True)
        self.assertNotIn("operator_exit_confirmation", saved)

    def test_parent_exit_before_start_callback_refuses_live_group_then_passes_after_exit(self):
        engine = self.paused()
        entry, path, release = self.interrupt_check(engine, "live")
        record = read_json(path)
        pgid = record["pgid"]
        try:
            self.assertEqual((record["owner"], record["startup_state"], record["pid"]),
                             (entry["id"], "started", pgid))
            self.assertTrue(util.group_alive(pgid))
            before = tree_bytes(engine.directory)
            error = self.assert_stop(("process_unconfirmed", "dispatch_exit"), lambda: self.gate(engine, entry))
            self.assertIn(str(pgid), str(error))
            reader = self.engine()
            reader.load_run(engine.run["id"])
            self.assertFalse(reader.services.quota.no_live_model_process())
            reader.services.lifecycle.wake_checks()
            self.assert_stop(("process_unconfirmed", "dispatch_exit"), reader.context.ensure_exited)
            self.assertEqual(tree_bytes(engine.directory), before)
        finally:
            release.touch()
            deadline = time.monotonic() + 10
            while util.group_alive(pgid) and time.monotonic() < deadline:
                time.sleep(.02)
        self.assertEqual(self.gate(engine, entry), [entry])

    def test_reused_path_and_another_round_require_current_owner(self):
        engine = self.paused()
        other, other_path, _ = self.interrupt_check(engine, "missing", attempt=2)
        old, path, _ = self.interrupt_check(engine, "missing")
        new, reused_path, _ = self.interrupt_check(engine, "reused")
        self.assertEqual(path, reused_path)
        self.assertNotEqual(new["id"], old["id"])
        old_record = read_json(path)
        self.assertEqual((old_record["owner"], old_record["startup_state"]), (old["id"], "not_started"))
        other_record = read_json(other_path)
        self.assertEqual((other_record["owner"], other_record["startup_state"]), (other["id"], "not_started"))
        for entry in (new, dict(new, process_record=other["process_record"])):
            self.assert_stop(("process_unconfirmed", "dispatch_exit"), lambda: self.gate(engine, entry))
        for owner in (None, True, 1, [new["id"]]):
            write_json(path, dict(old_record, owner=owner))
            self.assert_stop(("process_unconfirmed", "dispatch_exit"), lambda: self.gate(engine, new))
        write_json(path, dict(old_record, owner=new["id"]))
        self.assertEqual(self.gate(engine, new), [new])

    def test_local_io_is_not_a_check_failure_and_record_reads_preserve_eio(self):
        engine = self.paused()
        entry, path, _ = self.interrupt_check(engine, "local_io")
        record = read_json(path)
        self.assertEqual(record["owner"], entry["id"])
        self.assertEqual((record["termination_reason"], record["evidence_complete"]), ("error", False))
        self.assertEqual(record["errors"][0]["errno"], errno.EIO)

        def fail_open(real, name, *args, **kwargs):
            if name == path.name:
                raise OSError(errno.EIO, "record I/O")
            return real(name, *args, **kwargs)

        with patch.object(state_files, "os", FakeOs(open=fail_open)):
            error = self.assert_stop(("runtime_failure", "io"), lambda: self.gate(engine, entry))
            self.assertIsInstance(error, StateReadError)
            self.assertEqual(error.outcome.path, str(path))

    def test_load_and_cleanup_reject_invalid_shapes_without_writes(self):
        engine = self.paused()
        entry, _, _ = self.interrupt_check(engine, "missing")
        abandoned = self.engine().abandon(engine.run["id"])
        ledger = engine.directory / "state.json"
        path = entry["process_record"]
        stale = dict(entry)
        verification = abandoned["dispatches"].pop()
        self.assertEqual(verification["id"], entry["id"])
        bad = (None, True, [], 0, "", "/" + path, "Verification/check-0.outcome.json",
               "verification/../check-0.outcome.json", "verification/./check-0.outcome.json",
               "verification//check-0.outcome.json", "verification/a\\b/check-0.outcome.json",
               "verification/a\0b/check-0.outcome.json", "verification/check-00.outcome.json",
               "verification/check-٠.outcome.json", "verification/check-0.outcome.json/",
               "verification/check-0.outcome.json\n", "verification/" + "a" * 4096 + "/check-0.outcome.json")
        cases = [dict(stale, process_record=value) for value in bad]
        cases.extend([dict(stale, role="reviewer"), {key: value for key, value in stale.items() if key != "process_record"}])
        for invalid in cases:
            with self.subTest(record=invalid.get("process_record"), role=invalid.get("role")):
                write_json(ledger, dict(abandoned, dispatches=[*abandoned["dispatches"], invalid]))
                before = tree_bytes(engine.directory)
                self.assert_stop(("evidence", "load_integrity"), lambda: self.engine().load_run(engine.run["id"]))
                self.assert_stop(("evidence", "runtime_integrity"), lambda: cleanup_run(self.repo, engine.run["id"]))
                self.assertEqual(tree_bytes(engine.directory), before)
        write_json(ledger, dict(abandoned, dispatches=[*abandoned["dispatches"], stale]))
        before = tree_bytes(engine.directory)
        self.assertEqual(cleanup_run(self.repo, engine.run["id"])["worktree"]["status"], "eligible")
        self.assertEqual(tree_bytes(engine.directory), before)

    def test_real_task_setup_and_final_records_use_the_run_directory_and_own_log(self):
        command = {"cwd": ".", "argv": [sys.executable, "-c", "pass"]}
        for fresh in (False, True):
            with self.subTest(fresh=fresh):
                self.config.data["verification"].update(commands=[command], fresh_checkout=fresh,
                                                         setup_commands=[command] if fresh else [])
                engine = self.engine()
                engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
                reader = self.engine()
                reader.load_run(engine.run["id"])
                entries = [entry for entry in reader.run["dispatches"] if entry["role"] == "verification"]
                self.assertEqual(len(entries), 3 if fresh else 2)
                self.assertEqual(len({entry["id"] for entry in entries}), len(entries))
                for entry in entries:
                    file = reader.directory / entry["process_record"]
                    record = read_json(file)
                    self.assertEqual(record.pop("owner"), entry["id"])
                    self.assertEqual(record, entry["process_outcome"])
                    [result] = read_json(file.with_name("results.json"))
                    self.assertEqual(result["process_outcome"], record)
                    self.assertEqual(Path(result["log"]).with_suffix(".outcome.json"), file)
                self.assertEqual(sum("/setup/" in entry["process_record"] for entry in entries), int(fresh))


class OwnedRecordWritesTests(unittest.TestCase):
    def test_owner_on_all_existing_writes_and_callback_facts(self):
        callback_outcomes = []

        def fail_callback(value):
            callback_outcomes.append(value.to_dict())
            raise OSError(errno.EIO, "callback failed")

        for owner in (None, "verification-dispatch"):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as directory:
                calls = []

                def record(path, value):
                    calls.append(value.copy())
                    write_json(path, value)

                log = Path(directory) / "check-0"
                child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
                try:
                    child.wait(timeout=5)
                    with patch("harness.util.write_json", record):
                        supervisor = ProcessSupervisor(log, owner=owner, on_outcome=fail_callback)
                        supervisor.started(child)
                        with self.assertRaises(OSError):
                            supervisor.finish()
                        supervisor.record_cancellation(KeyboardInterrupt())
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
                self.assertEqual([call["startup_state"] for call in calls], ["starting"] + ["started"] * 4)
                self.assertEqual([call["termination_reason"] for call in calls], ["normal"] * 4 + ["cancelled"])
                for call in calls:
                    self.assertEqual(call.get("owner"), owner)
                    self.assertEqual("owner" in call, owner is not None)
                self.assertEqual(read_json(supervisor.path), calls[-1])
                self.assertTrue(all("owner" not in facts for facts in callback_outcomes))
