"""Command directories on formal verification's side: task and whole-run gates, fresh checkouts, the bound root."""
import contextlib
import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

from git_isolation import clean_environment, fixture_git as _git
from harness import git, verification
from harness.commands import command_id
from harness.config import Config
from harness.input_projection import validate_indexes
from harness.util import HarnessError, read_json
from test_command_cwd_broker import capture_tail
from test_harness import ROOT, RepoCase, walk_exit, write_project_config
from test_runtime_changes import ScriptedBackend


# Passes only where a pubspec.yaml is: in app/, never at the root.
CHECK = [sys.executable, "-c", "import os, sys; sys.exit(0 if os.path.isfile('pubspec.yaml') else 1)"]
# Writes where it started, its path and identity, to the file named by its argument.
REPORT = "import os, sys; s = os.stat('.'); open(sys.argv[1], 'w').write(f'{os.getcwd()}\\n{s.st_dev} {s.st_ino}')"
# Rule 10's facts for a command whose directory could not be opened, in the results and the ledger.
NOT_STARTED = {"status": "failed", "termination_reason": "start_failed", "exit_code": None,
               "exit_confirmed": True, "evidence_complete": True}


def identity(path):
    info = os.stat(path, follow_symlinks=False)
    return f"{info.st_dev} {info.st_ino}"


def facts(row):
    return {key: row[key] for key in NOT_STARTED}


def head(path):
    # The commit a checkout actually holds, read by Git without a test's GIT_* settings but with the
    # isolated configuration selectors, so no system configuration comes back.
    return subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True, timeout=60, env=clean_environment()).stdout.strip()


def stop_of(error):
    return error.stop["kind"], error.stop["reason"]


class CommandDirectoryVerificationTests(RepoCase):
    def setUp(self):
        super().setUp()
        (self.repo / "app").mkdir()
        (self.repo / "app" / "pubspec.yaml").write_text("name: app\n")
        (self.repo / ".gitignore").write_text("__pycache__/\nbuild/\n")
        # Git isolated from the user's configuration and hooks.
        _git(self.repo, "add", ".")
        _git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-q", "-m", "app")
        self.marker = self.outside / "ran"
        self.engines = []
        self.addCleanup(self.remove_checkouts)

    def configure(self, commands, setup=None):
        self.config = Config.load(ROOT / "harness.toml", write_project_config(self.repo, commands))
        if setup is not None:
            self.config.data["verification"].update(fresh_checkout=True, setup_commands=setup)
            Config.validate(self.config.data)

    def report(self, cwd):
        return {"cwd": cwd, "argv": [sys.executable, "-c", REPORT, str(self.marker)]}

    def start(self, backend=None):
        engine = self.engine(backend)
        self.engines.append(engine)
        return engine, engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))

    def stopped(self, backend=None):
        with self.assertRaises(HarnessError) as caught:
            self.start(backend)
        engine = self.engines[-1]
        return engine, caught.exception, read_json(engine.directory / "state.json")

    def remove_checkouts(self):
        for engine in self.engines:
            for artifact in engine.directory.glob("verification/final/*/verification-*/checkout.json"):
                shutil.rmtree(Path(read_json(artifact)["worktree"]).parents[1], ignore_errors=True)

    @staticmethod
    def events(engine):
        return [json.loads(line)["event"] for line in (engine.directory / "events.jsonl").read_text().splitlines()]

    @staticmethod
    def checks(ledger, **fields):
        return [entry for entry in ledger["dispatches"] if entry["role"] == "verification"
                and all(entry.get(key) == value for key, value in fields.items())]

    # ---- rule 10: the directory is part of the command; a missing one is a failed check ------------

    def test_a_command_runs_in_its_directory_and_its_results_name_it(self):
        command = {"cwd": "app", "argv": CHECK}
        self.configure([command])
        engine, result = self.start()
        self.assertEqual(result["status"], "completed")
        rows = read_json(engine.directory / "verification" / "tasks" / "greet" / "1" / "results.json")
        self.assertEqual([(row["command"], row["status"], row["exit_code"]) for row in rows], [(command, "passed", 0)])
        ledger = read_json(engine.directory / "state.json")
        self.assertEqual([entry["command"] for entry in self.checks(ledger)], [command, command])

    def test_the_same_argv_at_the_root_is_a_failed_check_and_the_task_is_reworked(self):
        command = {"cwd": ".", "argv": CHECK}
        self.configure([command])
        self.config.data["runtime"]["max_attempts"] = 2
        backend = ScriptedBackend()
        engine, error, ledger = self.stopped(backend)
        self.assertEqual(self.events(engine).count("verification_failed"), 2)
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 2)
        self.assertEqual([(entry["command"], entry["status"], entry["exit_code"]) for entry in self.checks(ledger)],
                         [(command, "failed", 1)] * 2)
        # Rework until the attempts run out: never a missing grant.
        self.assertEqual((ledger["stop"]["kind"], ledger["stop"]["reason"]), ("budget", "task_attempts"))

    def test_a_missing_directory_fails_the_task_check_and_the_rest_of_its_commands_still_run(self):
        missing = {"cwd": "build", "argv": ["python3", "-c", "pass"]}
        _, rework = self.check_missing_directory_rework(missing)
        self.assertEqual(rework["feedback"]["verification"][0]["command"], missing)

    def test_a_missing_directory_reports_the_exact_command_by_reference_on_rework(self):
        missing = {"cwd": "build", "argv": ["python3", "-c", "pass  # " + "x" * 2000]}
        engine, rework = self.check_missing_directory_rework(missing)
        expected_id = command_id(missing)
        self.assertEqual(rework["feedback"]["verification"][0]["command"], {"exact_command_ref": expected_id})

        # The worker resolves the ID in this packet's catalog, validating an indexed catalog before reading it.
        catalog = rework["exact_command_catalog"]
        if isinstance(catalog, dict):
            digest = catalog["input_index"]
            index = rework["input_evidence_indexes"][digest]
            self.assertEqual(catalog, {"input_index": index["artifact_digest"], "count": index["count"]})
            validate_indexes(engine.directory, [index])
            catalog = read_json(Path(index["artifact"]))["records"]
        records = [record for record in catalog if record["id"] == expected_id]
        self.assertEqual(records, [{"id": expected_id, "command": missing}])

    def check_missing_directory_rework(self, missing):
        # Keep both configured commands independent of the interpreter's installation path.
        check = {"cwd": "app", "argv": ["python3", *CHECK[1:]]}
        self.configure([missing, check])

        def second_attempt_builds(backend, kwargs, packet, result):
            # The rework creates the (ignored) directory the first attempt lacked.
            if kwargs["role"] == "worker" and sum(call["role"] == "worker" for call in backend.calls) == 2:
                (kwargs["cwd"] / "build").mkdir()
        backend = ScriptedBackend(second_attempt_builds)
        engine, result = self.start(backend)
        self.assertEqual(result["status"], "completed")
        ledger = read_json(engine.directory / "state.json")
        self.assertNotIn("stop", ledger)
        self.assertEqual(self.events(engine).count("verification_failed"), 1)
        rows = read_json(engine.directory / "verification" / "tasks" / "greet" / "1" / "results.json")
        self.assertEqual([row["command"] for row in rows], [missing, check])
        self.assertEqual((facts(rows[0]), rows[0]["timed_out"]), (NOT_STARTED, False))
        self.assertIn("'build' 不存在", rows[0]["stderr"])
        self.assertEqual(rows[0]["process_outcome"]["errors"][0]["type"], "CommandDirectoryError")
        self.assertIn("'build' 不存在", rows[0]["process_outcome"]["errors"][0]["message"])
        self.assertEqual((rows[1]["status"], rows[1]["exit_code"]), ("passed", 0))
        entry = self.checks(ledger, command=missing)[0]
        self.assertEqual((facts(entry), entry["process_state"]), (NOT_STARTED, "exited"))
        # The rework was told which check failed and why.
        _, rework = [call["packet"] for call in backend.calls if call["role"] == "worker"]
        told = rework["feedback"]["verification"][0]
        self.assertEqual(facts(told), NOT_STARTED)
        self.assertIn("'build' 不存在", told["stderr"])
        retried = read_json(engine.directory / "verification" / "tasks" / "greet" / "2" / "results.json")
        self.assertEqual([(row["command"], row["status"], row["exit_code"]) for row in retried],
                         [(missing, "passed", 0), (check, "passed", 0)])
        self.assertEqual(self.events(engine).count("task_accepted"), 1)
        return engine, rework

    def test_a_check_whose_stderr_tail_is_cut_inside_a_character_is_a_failed_check_and_the_task_is_reworked(self):
        # 4,201 bytes of stderr, the 4,000-byte tail starting on the last byte of a three-byte character; fails until build/ exists.
        written = b"x" + "汉".encode() * 1400
        command = {"cwd": ".", "argv": [sys.executable, "-c", "import os, sys; "
                                        "sys.stderr.buffer.write(b'x' + '\\u6c49'.encode() * 1400); "
                                        "sys.exit(0 if os.path.isdir('build') else 1)"]}
        self.configure([command])

        def second_attempt_builds(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(call["role"] == "worker" for call in backend.calls) == 2:
                (kwargs["cwd"] / "build").mkdir()
        backend = ScriptedBackend(second_attempt_builds)
        engine, result = self.start(backend)
        self.assertEqual(result["status"], "completed")
        ledger = read_json(engine.directory / "state.json")
        self.assertNotIn("stop", ledger)
        self.assertEqual(self.events(engine).count("verification_failed"), 1)
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 2)
        # Valid JSON in strict UTF-8, carrying the cut tail as a failed check with complete evidence.
        path = engine.directory / "verification" / "tasks" / "greet" / "1" / "results.json"
        (row,) = json.loads(path.read_bytes().decode("utf-8"))
        self.assertEqual((row["status"], row["exit_code"], row["evidence_complete"]), ("failed", 1, True))
        self.assertNotIn("persistence_error", row)
        self.assertEqual(Path(row["log"]).with_suffix(".stderr").read_bytes(), written)
        self.assertEqual(row["stderr"], capture_tail(written))
        self.assertTrue(row["stderr"].startswith("[earlier output omitted; see log]\n�汉"))
        entry = self.checks(ledger, command=command)[0]
        self.assertEqual((entry["status"], entry["evidence_complete"]), ("failed", True))

    # ---- rule 13: the fresh checkout's root -----------------------------------------------------------

    def test_setup_runs_in_its_directory_of_the_fresh_checkout_or_one_an_earlier_setup_created(self):
        printed = [sys.executable, "-c", "import os; print(os.getcwd())"]
        self.configure([{"cwd": "app", "argv": CHECK}], setup=[
            {"cwd": "app", "argv": printed}, {"cwd": ".", "argv": [sys.executable, "-c", "import os; os.mkdir('build')"]},
            {"cwd": "build", "argv": printed}])
        engine, result = self.start()
        self.assertEqual(result["status"], "completed")
        (artifact,) = engine.directory.glob("verification/final/*/verification-*/checkout.json")
        checkout = read_json(artifact)["worktree"]
        setup = read_json(artifact.parent / "setup" / "results.json")
        self.assertEqual([row["stdout"] for row in setup], [checkout + "/app\n", "", checkout + "/build\n"])

    def test_a_setup_directory_missing_from_head_stops_every_fresh_checkout_at_setup_failed(self):
        command = {"cwd": "build", "argv": [sys.executable, "-c", "pass"]}
        self.configure([{"cwd": "app", "argv": CHECK}], setup=[command])
        backend = ScriptedBackend()
        engine, error, ledger = self.stopped(backend)
        self.assertEqual(stop_of(error), ("verification", "setup_failed"))
        walk_exit(self, engine.directory, ("verification", "setup_failed"), backend=backend,
                  expect_status="blocked", expect_code=3)
        ledger = read_json(engine.directory / "state.json")
        self.assertEqual((ledger["stop"]["kind"], ledger["stop"]["reason"]), ("verification", "setup_failed"))
        artifacts = sorted(engine.directory.glob("verification/final/*/verification-*/checkout.json"))
        self.assertEqual(len({read_json(artifact)["worktree"] for artifact in artifacts}), 2)
        # Each new checkout holds the same HEAD, the ledger's expected one.
        self.assertEqual([head(read_json(artifact)["worktree"]) for artifact in artifacts], [ledger["expected_head"]] * 2)
        for artifact in artifacts:
            (row,) = read_json(artifact.parent / "setup" / "results.json")
            self.assertEqual((row["command"], facts(row)), (command, NOT_STARTED))
            self.assertIn("'build' 不存在", row["stderr"])
        entries = self.checks(ledger, phase="setup")
        self.assertEqual([(entry["command"], facts(entry)) for entry in entries], [(command, NOT_STARTED)] * 2)

    def test_a_whole_run_command_whose_directory_is_not_in_head_fails_final_verification(self):
        command = {"cwd": "build", "argv": [sys.executable, "-c", "pass"]}
        self.configure([command], setup=[])

        def builds(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                (kwargs["cwd"] / "build").mkdir(exist_ok=True)   # ignored: in the run worktree, never in HEAD
        backend = ScriptedBackend(builds)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定：task_id=final"):
            self.start(backend)
        engine = self.engines[-1]
        record = engine.run["final_history"][-1]
        self.assertEqual((record["status"], record["failure_kind"]), ("verification_failed", "exit_code"))
        self.assertEqual([(row["command"], facts(row)) for row in record["verification_failures"]],
                         [(command, NOT_STARTED)])
        trigger = [call for call in backend.calls if call["kind"] == "decide"][-1]["packet"]["trigger"]
        self.assertEqual(trigger["final_review"]["status"], "verification_failed")

    def program_failure(self, error_number):
        program = self.outside / f"check-{errno.errorcode[error_number]}"
        program.unlink(missing_ok=True)
        if error_number != errno.ENOENT:
            program.write_text("exit 0\n")
            program.chmod(0o755 if error_number == errno.ENOEXEC else 0o644)
        return program

    def test_program_start_failure_reaches_task_rework_and_final_decision(self):
        for number in (errno.ENOENT, errno.EACCES, errno.ENOEXEC):
            with self.subTest(errno=number):
                program = self.program_failure(number)
                command = {"cwd": ".", "argv": [str(program)]}
                self.configure([command])

                def change(backend, kwargs, packet, result):
                    if kwargs["role"] == "worker" and (packet["feedback"] or {}).get("verification"):
                        program.write_text("#!/bin/sh\nexit 0\n")
                        program.chmod(0o755)
                    if kwargs["kind"] == "review":
                        self.program_failure(number)

                backend = ScriptedBackend(change)
                engine, error, ledger = self.stopped(backend)
                self.assertEqual(stop_of(error), ("operator_decision", "answer_required"))
                _, retry = (call for call in backend.calls if call["role"] == "worker")
                told, = retry["packet"]["feedback"]["verification"]
                self.assertEqual((told["command"], facts(told)), (command, NOT_STARTED))
                self.assertIn(str(program), told["stderr"])
                self.assertIn(os.strerror(number), told["stderr"])
                record = ledger["final_history"][-1]
                self.assertEqual((record["status"], record["failure_kind"]), ("verification_failed", "exit_code"))
                final, = record["verification_failures"]
                self.assertEqual((final["command"], facts(final)), (command, NOT_STARTED))
                trigger = [call["packet"]["trigger"] for call in backend.calls if call["kind"] == "decide"][-1]
                self.assertEqual(trigger["final_review"]["status"], "verification_failed")
                self.assertEqual([entry["status"] for entry in self.checks(ledger)], ["failed", "completed", "failed"])
                row, = read_json(engine.directory / "verification/tasks/greet/1/results.json")
                self.assertEqual(row["process_outcome"]["errors"][0]["type"], "ProgramStartError")

    def test_program_start_failure_in_fresh_checkout_setup_is_a_setup_failure(self):
        for number in (errno.ENOENT, errno.EACCES, errno.ENOEXEC):
            with self.subTest(errno=number):
                program = self.program_failure(number)
                command = {"cwd": ".", "argv": [str(program)]}
                self.configure([{"cwd": "app", "argv": CHECK}], setup=[command])
                engine, error, ledger = self.stopped()
                self.assertEqual(stop_of(error), ("verification", "setup_failed"))
                self.assertEqual((ledger["stop"]["kind"], ledger["stop"]["reason"]), stop_of(error))
                artifact, = engine.directory.glob("verification/final/*/verification-*/checkout.json")
                self.assertEqual(read_json(artifact)["status"], "setup_failed")
                row, = read_json(artifact.parent / "setup/results.json")
                self.assertEqual((row["command"], facts(row)), (command, NOT_STARTED))
                self.assertEqual(row["process_outcome"]["errors"][0]["type"], "ProgramStartError")
                self.assertIn(str(program), row["stderr"])

    # ---- rule 12: the binding is checked on the root the command starts from ---------------------------

    @contextlib.contextmanager
    def at_open(self, replace, when=lambda binding: True):
        """For the first open_bound_root call of a check ``when`` selects, ``replace(path)`` holds during that call only."""
        real, calls, check = git.open_bound_root, [], verification.Verification.run_verification_commands.__code__

        def wrapped(held, worktree, binding):
            if (calls or not when(binding)
                    or not any(frame.f_code is check for frame, _ in traceback.walk_stack(None))):
                return real(held, worktree, binding)
            calls.append(binding["worktree"])
            with replace(Path(binding["worktree"])):
                return real(held, worktree, binding)
        with patch("harness.git.open_bound_root", side_effect=wrapped):
            yield calls

    @contextlib.contextmanager
    def unbound_on_path(self, path):
        # The reviewed timing: an unbound directory A (with an app/ of its own) is on the path as the root
        # opens; the bound one is there only while the path-based part of the check runs.
        moved, parked, real = path.with_name(path.name + "-moved"), path.with_name(path.name + "-impostor"), git._check_binding
        os.rename(path, moved)
        (path / "app").mkdir(parents=True)

        def bound_during_check(*args):
            os.rename(path, parked)
            os.rename(moved, path)
            try:
                return real(*args)
            finally:
                os.rename(path, moved)
                os.rename(parked, path)
        try:
            with patch("harness.git._check_binding", side_effect=bound_during_check):
                yield
        finally:
            shutil.rmtree(path)
            os.rename(moved, path)

    @contextlib.contextmanager
    def fifo_gitfile(self, path):
        saved = self.outside / "gitfile"
        os.replace(path / ".git", saved)
        os.mkfifo(path / ".git")
        fifo, real_open, real_fdopen, real_read = os.stat(path / ".git"), os.open, os.fdopen, os.read
        self.fifo_uses = []

        def use(step, descriptor):   # what the FIFO itself went through, whichever descriptor number it had
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) == (fifo.st_dev, fifo.st_ino):
                self.fifo_uses.append(step)

        def opened(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            use("open", descriptor)
            return descriptor

        def reading(read):
            def read_from(descriptor, *args, **kwargs):
                use("read", descriptor)
                return read(descriptor, *args, **kwargs)
            return read_from

        def blocked(signum, frame):
            raise AssertionError("the bound root's check blocked on a .git FIFO without a writer")
        previous = signal.signal(signal.SIGALRM, blocked)
        signal.alarm(20)
        try:
            with patch("os.open", opened), patch("os.fdopen", reading(real_fdopen)), patch("os.read", reading(real_read)):
                yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
            os.unlink(path / ".git")
            os.replace(saved, path / ".git")

    def assert_refused_before_registration(self, engine, error, ledger, folder):
        self.assertEqual(stop_of(error), ("workspace_mismatch", "binding_changed"))
        self.assertEqual((ledger["status"], ledger["stop"]["kind"], ledger["stop"]["reason"]),
                         ("blocked", "workspace_mismatch", "binding_changed"))
        self.assertFalse(self.marker.exists())
        self.assertEqual(read_json(engine.directory / folder / "results.json"), [])

    def test_a_task_check_whose_opened_root_is_not_the_bound_worktree_never_starts(self):
        self.configure([self.report("app")])
        for name, replace in (("unbound directory on the path", self.unbound_on_path), (".git FIFO", self.fifo_gitfile)):
            with self.subTest(name), self.at_open(replace) as calls:
                engine, error, ledger = self.stopped()
                self.assertEqual(calls, [engine.run["worktree_binding"]["worktree"]])
                self.assert_refused_before_registration(engine, error, ledger, "verification/tasks/greet/1")
                self.assertEqual(self.checks(ledger), [])
                self.assertNotIn("verification_failed", self.events(engine))
                if name == ".git FIFO":   # opened without blocking, refused by its type before any read (rule 12)
                    self.assertEqual(self.fifo_uses, ["open"])

    def test_a_fresh_checkout_whose_opened_root_is_not_the_bound_checkout_runs_no_setup(self):
        self.configure([{"cwd": "app", "argv": CHECK}], setup=[self.report("app")])
        with self.at_open(self.unbound_on_path, when=lambda binding: binding["branch"] is None) as calls:
            engine, error, ledger = self.stopped()
        (artifact,) = engine.directory.glob("verification/final/*/verification-*/checkout.json")
        self.assertEqual(calls, [read_json(artifact)["worktree"]])
        self.assert_refused_before_registration(engine, error, ledger, artifact.parent.relative_to(engine.directory) / "setup")
        self.assertEqual(self.checks(ledger, task_id="final"), [])
        self.assertEqual(read_json(artifact)["status"], "preparing")

    def test_a_root_replaced_after_it_was_opened_and_checked_does_not_redirect_the_check(self):
        self.configure([self.report("app")])
        real, seen = verification.run_process, {}

        def replaced_then_started(argv, cwd, log, **kwargs):
            if not seen:
                seen.update(moved=cwd.with_name(cwd.name + "-moved"), app=identity(cwd / "app"))
                os.rename(cwd, seen["moved"])
                cwd.mkdir()
            return real(argv, cwd, log, **kwargs)
        with patch("harness.verification.run_process", side_effect=replaced_then_started):
            engine, error, ledger = self.stopped()
        # The child started in the checked root's app/, now moved aside, not below the new directory.
        self.assertEqual(self.marker.read_text(), f"{seen['moved'] / 'app'}\n{seen['app']}")
        self.assertEqual(os.listdir(engine.run["worktree_binding"]["worktree"]), [])
        # The path-based snapshot after the check found the replacement.
        self.assertEqual(stop_of(error), ("workspace_mismatch", "binding_changed"))
        self.assertNotIn("verification_failed", self.events(engine))


if __name__ == "__main__":
    import unittest
    unittest.main()
