"""The project file gate (spec 2.4) and the commands it never stops (spec 2.3).

Every CLI call here loads its configuration files for real; only the model backend is
offline. run, resume and permission-diagnostics refuse a missing project file before any
write; a broken one fails as a broken operator file does, as invalid_input/config_file naming
the file. abandon, the commands that read no configuration, and the test entry points do not
depend on the target's project file.
"""
import contextlib
import io
import json
import shutil
import signal
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import cli, git, stops
from harness import config as config_module
from harness.adapters import DemoBackend
from harness.config import Config
from harness.runtime import Runtime
from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase, cli_notice, scratch, write_project_config

PLAN = ROOT / "examples" / "plan.json"
OPERATOR = ROOT / "harness.toml"


def replace(path, content):
    """Put bytes at ``path``, or a directory (``None``): a path that exists but cannot be read as a file."""
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    if content is None:
        path.mkdir()
    else:
        path.write_bytes(content)


class RecordingBackend(DemoBackend):
    """The offline demo, recording every model call it receives."""

    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs["role"])
        return super().invoke(**kwargs)


class GateCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.backend, self.engines = RecordingBackend(), []
        self.common = Path(git.git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
        self.project = git.repository(self.repo) / ".harness" / "project.toml"

    def invoke(self, *argv, config=OPERATOR):
        """cli.main with real configuration loading; returns (code, stdout, stderr)."""
        def factory(config, repo, **kwargs):
            engine = Runtime(config, repo, backend=self.backend, **kwargs)
            self.engines.append(engine)
            return engine

        output, errors = io.StringIO(), io.StringIO()
        previous = signal.getsignal(signal.SIGTERM)
        try:
            with patch.object(cli, "Runtime", factory), \
                    patch("harness.operations.probe_cli", return_value={"path": None, "version": None}), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = cli.main(["--config", str(config), "--repo", str(self.repo), *argv])
        finally:
            signal.signal(signal.SIGTERM, previous)
        return code, output.getvalue(), errors.getvalue()

    def paused_run(self):
        engine = Runtime(self.config, self.repo, backend=self.backend)
        engine.start("Implement greet", read_json(PLAN), pause_after_plan=True)
        return engine.run["id"], engine.directory / "state.json"

    def footprint(self):
        """What a refused command must leave as it was, absent paths included: the private tree byte for byte,
        the common-Git exclude file and private-state lock, refs, worktrees and the checkout's status."""
        state = self.repo / ".harness"
        tree = ({str(path.relative_to(state)): path.read_bytes() if path.is_file() and not path.is_symlink() else None
                 for path in sorted(state.rglob("*"))} if state.exists() else None)
        exclude = self.common / "info" / "exclude"
        return {"private": tree, "exclude": exclude.read_bytes() if exclude.exists() else None,
                "lock": (self.common / "harness-private-state.lock").exists(),
                "refs": git.git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)"),
                "worktrees": git.git(self.repo, "worktree", "list", "--porcelain"),
                "status": git.git(self.repo, "status", "--porcelain", "--ignored", "--untracked-files=all")}


class MissingProjectFileTests(GateCase):
    def commands(self, run_id):
        return (["run", "Implement greet", "--plan", str(PLAN)], ["resume", run_id],
                ["permission-diagnostics", run_id, "dispatch-1"])

    def assert_refused(self, argv):
        before = self.footprint()
        code, output, errors = self.invoke(*argv)
        self.assertEqual(code, 2, errors)
        self.assertEqual(output, "")
        stop = cli_notice(errors)
        self.assertEqual((stop["kind"], stop["reason"], stop["exit_code"], stop["data"]),
                         ("invalid_input", "cli_arguments", 2, {"option": "--repo"}))
        # The expected absolute path and the example to start from.
        self.assertIn(str(self.project), errors)
        self.assertIn("examples/project.toml", errors)
        self.assertEqual(self.footprint(), before)

    def test_refused_before_any_write_in_a_fresh_repository(self):
        exclude = self.common / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_bytes(b"# operator exclusion\n/build/\n")
        for round_ in ("exclude present", "exclude absent"):
            for argv in self.commands("run_missing"):
                with self.subTest(round=round_, command=argv[0]):
                    self.assert_refused(argv)
            exclude.unlink(missing_ok=True)
        # Nothing that did not exist before was created.
        self.assertFalse((self.repo / ".harness").exists())
        self.assertFalse(exclude.exists())
        self.assertFalse((self.common / "harness-private-state.lock").exists())
        self.assertEqual((self.engines, self.backend.calls, self.quota_calls.all()), ([], [], []))

    def test_refused_without_touching_an_existing_run_or_its_private_state(self):
        run_id, ledger = self.paused_run()
        saved, calls, probes = ledger.read_bytes(), list(self.backend.calls), self.quota_calls.all()
        for argv in self.commands(run_id):
            with self.subTest(command=argv[0]):
                self.assert_refused(argv)
        self.assertEqual(ledger.read_bytes(), saved)
        self.assertFalse((self.repo / ".harness" / "operations").exists())
        self.assertEqual((self.engines, self.backend.calls, self.quota_calls.all()), ([], calls, probes))
        # The refusal changed nothing a later resume depends on: with the file in place it continues.
        write_project_config(self.repo, self.config.data["verification"]["commands"])
        code, _, errors = self.invoke("resume", run_id, "--approve-plan")
        self.assertEqual(code, 0, errors)
        self.assertEqual(read_json(ledger)["status"], "completed")


class OperatorPathTests(GateCase):
    """The refusal names the layer load was reading, never one guessed from the path that failed."""
    COMMANDS = (["run", "Implement greet", "--plan", str(PLAN)], ["resume", "run_missing"],
                ["permission-diagnostics", "run_missing", "dispatch-1"])

    def refusals(self, config):
        """(exit code, kind, reason, data) of each command loading ``config``; each refused before any write."""
        results = []
        for argv in self.COMMANDS:
            before = self.footprint()
            code, output, errors = self.invoke(*argv, config=config)
            stop = cli_notice(errors)
            self.assertEqual(output, "", errors)
            self.assertEqual(self.footprint(), before)
            results.append((code, stop["kind"], stop["reason"], stop["data"]))
        self.assertEqual((self.engines, self.backend.calls, self.quota_calls.all()), ([], [], []))
        return results

    def test_an_operator_path_at_or_leading_to_the_missing_project_file_is_the_missing_operator_file(self):
        link = scratch(self) / "harness.toml"
        link.symlink_to(self.project)
        for case, config in (("the project file's place", self.project), ("a dangling link to it", link)):
            with self.subTest(case=case):
                self.assertEqual(self.refusals(config), [(2, "invalid_input", "cli_arguments", {"option": "--config"})] * 3)
        # The same missing file, read as the project layer under a present operator file, is the project's.
        self.assertEqual(self.refusals(OPERATOR), [(2, "invalid_input", "cli_arguments", {"option": "--repo"})] * 3)
        self.assertFalse(self.project.exists() or (self.repo / ".harness").exists())

    def test_an_operator_path_in_a_symlink_loop_is_local_io_on_every_python(self):
        # Python 3.11's Path.resolve raised RuntimeError for the loop, which escaped the CLI; from 3.13 resolve
        # leaves it to the read, which fails with ELOOP. Both now give that result: a file there that cannot be read.
        write_project_config(self.repo, self.config.data["verification"]["commands"])
        link = scratch(self) / "harness.toml"
        link.symlink_to(link)
        self.assertEqual(self.refusals(link), [(1, "runtime_failure", "io", {"error_type": "OSError"})] * 3)


class BrokenProjectFileTests(GateCase):
    # (case, project file bytes, operator file bytes broken the same way); None is a directory at the path.
    CASES = (
        ("not TOML", b"[runtime\n", b"[runtime\n"),
        ("not UTF-8", b"\xff[runtime]\n", b"\xff[runtime]\n"),
        ("unknown key", b"[runtime]\nnot_a_budget = 1\n", b"[runtime]\nnot_a_budget = 1\n"),
        ("invalid value", b'[verification]\ncommands = "make test"\n', b"[runtime]\nmax_attempts = 0\n"),
        ("wrong file", b"[memory]\ntask_ttl_days = 1\n", b"[verification]\ncommands = []\n"),
        ("unreadable", None, None),
    )

    def test_fails_as_a_broken_operator_file_does_before_any_write(self):
        run_id, ledger = self.paused_run()
        saved, calls, probes = ledger.read_bytes(), list(self.backend.calls), self.quota_calls.all()
        operator = scratch(self) / "harness.toml"
        valid = write_project_config(self.repo, self.config.data["verification"]["commands"]).read_bytes()
        for case, project, broken in self.CASES:
            for argv in (["run", "Implement greet", "--plan", str(PLAN)], ["resume", run_id],
                         ["permission-diagnostics", run_id, "dispatch-1"]):
                with self.subTest(case=case, command=argv[0]):
                    replace(self.project, valid)
                    replace(operator, broken)
                    code, output, errors = self.invoke(*argv, config=operator)
                    stop = cli_notice(errors)
                    reference = (code, output, stop["kind"], stop["reason"])
                    replace(self.project, project)
                    before = self.footprint()
                    code, output, errors = self.invoke(*argv)
                    stop = cli_notice(errors)
                    # The same type and exit code; the stop names the file that was refused.
                    self.assertEqual((code, output, stop["kind"], stop["reason"]), reference)
                    if project is None:  # a path that exists but cannot be read is local I/O
                        self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "io"))
                    else:
                        self.assertEqual((stop["kind"], stop["reason"], stop["data"], stop["source"]),
                                         ("invalid_input", "config_file", {"file": "project"},
                                          {"artifact": str(self.project)}))
                    self.assertEqual(self.footprint(), before)
        self.assertEqual(ledger.read_bytes(), saved)
        self.assertFalse((self.repo / ".harness" / "operations").exists())
        self.assertEqual((self.engines, self.backend.calls, self.quota_calls.all()), ([], calls, probes))


def nested_arrays(depth):
    """``runtime.max_attempts`` as ``depth`` nested arrays: deeper than the TOML parser can finish."""
    return ("[runtime]\nmax_attempts = " + "[" * depth + "]" * depth + "\n").encode()


def deep_dotted(parts=700):
    """``runtime.max_attempts`` followed by ``parts`` dotted key parts: the parser finishes it, a copy would not."""
    return ("[runtime]\nmax_attempts" + ".x" * parts + " = 1\n").encode()


def deep_header(parts=999):
    """A table header of ``parts`` parts under ``runtime.max_attempts`` holding a key of ``parts`` dotted parts."""
    header = ".".join(["runtime", "max_attempts"] + ["h"] * (parts - 2))
    return f"[{header}]\n{'.'.join(['k'] * parts)} = 1\n".encode()


class ConfigFileBoundaryTests(GateCase):
    """K22: every refusal of a configuration file's content is invalid_input/config_file (exit 2), before any write.

    One file's own check names that file (data.file, source.artifact); the merged validation cannot tell which
    file a value came from and names neither. Nesting the parser cannot finish and nesting it finishes but no
    configuration key accepts are both refused while the file is checked, before anything copies it.
    """
    COMMON = (("not UTF-8", b"\xff[runtime]\n"), ("not TOML", b"[runtime\n"),
              ("5,000 nested arrays", nested_arrays(5000)), ("700 dotted parts", deep_dotted()),
              ("999-part header and key", deep_header()), ("unknown key", b"[runtime]\nnot_a_budget = 1\n"))
    OPERATOR_FILES = COMMON + (("wrong file", b"[verification]\ncommands = []\n"),)
    PROJECT_FILES = COMMON + (("wrong file", b"[memory]\ntask_ttl_days = 1\n"),
                              ("command shape", b'[verification]\ncommands = [["make", "test"]]\n'))
    COMMANDS = (["run", "Implement greet", "--plan", str(PLAN)], ["resume", "run_missing"],
                ["permission-diagnostics", "run_missing", "dispatch-1"])

    def assert_refused(self, argv, config, data, source):
        before = self.footprint()
        code, output, errors = self.invoke(*argv, config=config)
        stop = cli_notice(errors)
        self.assertEqual((code, output), (2, ""), errors)
        self.assertEqual((stop["kind"], stop["reason"], stop["exit_code"], stop["data"], stop["source"]),
                         ("invalid_input", "config_file", 2, data, source))
        self.assertEqual(self.footprint(), before)
        return errors

    def test_the_deep_forms_are_one_the_parser_cannot_finish_and_two_it_finishes(self):
        with self.assertRaises((RecursionError, tomllib.TOMLDecodeError)):
            tomllib.loads(nested_arrays(5000).decode())
        for content in (deep_dotted(), deep_header()):
            self.assertIsInstance(tomllib.loads(content.decode()), dict)

    def test_a_broken_operator_file_is_refused_naming_it_by_every_command_that_loads_it(self):
        write_project_config(self.repo, self.config.data["verification"]["commands"])
        operator = scratch(self) / "harness.toml"
        for case, content in self.OPERATOR_FILES:
            operator.write_bytes(content)
            for argv in self.COMMANDS + (["doctor"],):
                with self.subTest(case=case, command=argv[0]):
                    self.assert_refused(argv, operator, {"file": "operator"}, {"artifact": str(operator.resolve())})
        self.assertEqual((self.engines, self.backend.calls, self.quota_calls.all()), ([], [], []))

    def test_a_broken_project_file_is_refused_naming_it_by_every_command_that_loads_it(self):
        for case, content in self.PROJECT_FILES:
            replace(self.project, content)
            for argv in self.COMMANDS:
                with self.subTest(case=case, command=argv[0]):
                    self.assert_refused(argv, OPERATOR, {"file": "project"}, {"artifact": str(self.project)})
        self.assertEqual((self.engines, self.backend.calls, self.quota_calls.all()), ([], [], []))

    def test_the_merged_validation_names_no_file_and_depth_is_refused_only_beyond_eight_levels(self):
        # Eight levels reach the merged validation, which refuses the value at its key; nine never get that far.
        cases = (("an invalid value", b"[runtime]\nmax_attempts = 0\n", {}, {}),
                 ("eight levels", b"[runtime]\nmax_attempts = [[[[[[1]]]]]]\n", {}, {}),
                 ("nine levels", b"[runtime]\nmax_attempts = [[[[[[[1]]]]]]]\n", {"file": "project"},
                  {"artifact": str(self.project)}))
        for case, content, data, source in cases:
            replace(self.project, content)
            for argv in self.COMMANDS:
                with self.subTest(case=case, command=argv[0]):
                    errors = self.assert_refused(argv, OPERATOR, data, source)
                    if not data:
                        self.assertIn("runtime.max_attempts", errors)
        self.assertEqual((self.engines, self.backend.calls, self.quota_calls.all()), ([], [], []))


class CommandShapeFileTests(GateCase):
    """A command list of the wrong shape is refused as an unknown key is: before any write, naming its field and its file."""
    COMMANDS = b'commands = [{cwd = ".", argv = ["make", "test"]}]\n'

    def assert_refused_naming_the_file(self, content, field):
        for argv in (["run", "Implement greet", "--plan", str(PLAN)], ["resume", "run_missing"],
                     ["permission-diagnostics", "run_missing", "dispatch-1"]):
            with self.subTest(command=argv[0]):
                replace(self.project, b"[runtime]\nnot_a_budget = 1\n")
                code, output, errors = self.invoke(*argv)
                reference = (code, output, cli_notice(errors))
                replace(self.project, content)
                before = self.footprint()
                code, output, errors = self.invoke(*argv)
                self.assertEqual((code, output, cli_notice(errors)), reference)
                self.assertIn(field, errors)
                self.assertIn(str(self.project), errors)
                self.assertEqual(self.footprint(), before)
        self.assertEqual((self.engines, self.backend.calls), ([], []))

    def test_an_argv_array_in_verification_commands_names_the_project_file(self):
        self.assert_refused_naming_the_file(b'[verification]\ncommands = [["make", "test"]]\n', "verification.commands[0]")

    def test_an_argv_array_in_setup_commands_names_the_project_file(self):
        self.assert_refused_naming_the_file(
            b"[verification]\n" + self.COMMANDS + b'fresh_checkout = true\nsetup_commands = [["make", "deps"]]\n',
            "verification.setup_commands[0]")

    def test_an_argv_array_in_worker_commands_names_the_project_file(self):
        self.assert_refused_naming_the_file(
            b"[verification]\n" + self.COMMANDS + b'\n[permissions]\nworker_commands = [["npm", "run", "build"]]\n',
            "permissions.worker_commands[0]")

    def test_a_command_object_with_an_invalid_cwd_names_the_project_file(self):
        self.assert_refused_naming_the_file(
            b'[verification]\ncommands = [{cwd = "../outside", argv = ["make", "test"]}]\n', "verification.commands[0].cwd")


class EmptyVerificationTests(GateCase):
    """K23: run without verification commands is refused as configuration, invalid_input/config_file (exit 2),
    before any write: no operation record, private state or memory database; Runtime.start refuses the same."""

    def test_run_is_refused_when_the_valid_project_file_has_no_verification_commands(self):
        state = self.repo / ".harness"
        cases = (("no verification section", b"[permissions]\nworker_commands = []\n"),
                 ("empty commands", b"[verification]\ncommands = []\n"))
        notices = {}
        for case, content in cases:
            with self.subTest(case=case):
                replace(self.project, content)
                before = self.footprint()
                code, output, errors = self.invoke("run", "Implement greet", "--plan", str(PLAN))
                notice = notices[case] = cli_notice(errors)
                self.assertEqual((code, output), (2, ""), errors)
                self.assertEqual((notice["kind"], notice["reason"], notice["exit_code"], notice["data"], notice["source"]),
                                 ("invalid_input", "config_file", 2,
                                  {"config_key": "verification.commands", "file": "project"},
                                  {"artifact": str(self.project)}))
                # Refused while the configuration was loaded: the private tree, the common Git directory's
                # exclude file and private-state lock, refs and worktrees are as they were.
                self.assertEqual(self.footprint(), before)
                self.assertFalse((state / "operations").exists())
                self.assertFalse((state / "memory.sqlite3").exists())
        self.assertEqual((self.engines, self.backend.calls, self.quota_calls.all()), ([], [], []))
        # A caller that bypasses the CLI meets the same refusal in start, before any run exists
        # (constructing the runtime itself sets up private state, which the CLI never reached).
        for case, content in cases:
            with self.subTest(case=case, entry="Runtime.start"):
                replace(self.project, content)
                engine = Runtime(Config.load(OPERATOR, self.project), self.repo, backend=self.backend)
                with self.assertRaises(HarnessError) as caught:
                    engine.start("Implement greet", read_json(PLAN))
                projection = stops.project(stops.of(caught.exception))
                self.assertEqual({key: value for key, value in projection.items() if key != "detail"}, notices[case])
        # A programmatic entry without a project file: the same stop names neither a file nor an artifact.
        engine = Runtime(Config.load(OPERATOR), self.repo, backend=self.backend)
        with self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", read_json(PLAN))
        value = stops.project(stops.of(caught.exception))
        self.assertEqual((value["kind"], value["reason"], value["exit_code"], value["data"], value["source"]),
                         ("invalid_input", "config_file", 2, {"config_key": "verification.commands"}, {}))
        self.assertFalse((state / "runs").exists())
        self.assertEqual((self.backend.calls, self.quota_calls.all()), ([], []))
        # The same fixture with a command runs: the refusal came from the empty list.
        write_project_config(self.repo, self.config.data["verification"]["commands"])
        code, output, errors = self.invoke("run", "Implement greet", "--plan", str(PLAN))
        self.assertEqual(code, 0, errors)
        self.assertEqual(read_json(state / "runs" / json.loads(output)["id"] / "state.json")["status"], "completed")


class CommandsWithoutTheProjectFileTests(GateCase):
    def test_abandon_works_when_the_project_file_is_missing_or_broken(self):
        variants = [("missing", False, None)] + [(case, True, project) for case, project, _ in BrokenProjectFileTests.CASES]
        for case, present, content in variants:
            with self.subTest(case=case):
                run_id, ledger = self.paused_run()
                if present:
                    replace(self.project, content)
                else:
                    self.assertFalse(self.project.exists())
                code, output, errors = self.invoke("abandon", run_id)
                self.assertEqual(code, 0, errors)
                self.assertEqual(json.loads(output)["status"], "abandoned")
                self.assertEqual(read_json(ledger)["status"], "abandoned")

    def test_commands_that_read_no_configuration_work_with_both_files_broken(self):
        run_id, ledger = self.paused_run()
        operator = scratch(self) / "harness.toml"
        operator.write_bytes(b"[runtime\n")
        replace(self.project, b"[runtime\n")
        saved = ledger.read_bytes()
        checks = (
            (["status"], lambda out: self.assertIn(run_id, [item["id"] for item in json.loads(out)])),
            (["status", run_id], lambda out: self.assertEqual(json.loads(out)["status"], "awaiting_approval")),
            (["watch", run_id], lambda out: self.assertEqual(json.loads(out.splitlines()[0])["status"],
                                                             "awaiting_approval")),
            (["memory", "search", "greet"], lambda out: self.assertIsInstance(json.loads(out), list)),
            (["memory", "export"], lambda out: self.assertIsInstance(json.loads(out), list)),
            (["cleanup-run", run_id], lambda out: self.assertEqual(json.loads(out)["id"], run_id)),
        )
        for argv, check in checks:
            with self.subTest(command=argv):
                code, output, errors = self.invoke(*argv, config=operator)
                self.assertEqual(code, 0, errors)
                check(output)
        self.assertEqual(ledger.read_bytes(), saved)
        # The same two files do stop a command that loads them.
        code, _, errors = self.invoke("resume", run_id, config=operator)
        self.assertEqual(code, 2, errors)
        self.assertEqual(ledger.read_bytes(), saved)


class EntryPointTests(RepoCase):
    """demo, smoke and the two standalone smokes set their own commands: the target's project file
    is never read, and the command it names, which would leave a marker, never runs."""

    def setUp(self):
        super().setUp()
        folder = scratch(self)
        self.marker = folder / "project-file-command-ran"
        command = {"cwd": ".", "argv": [sys.executable, "-c", f"import pathlib; pathlib.Path({str(self.marker)!r}).write_text('ran')"]}
        self.project = write_project_config(self.repo, [command], [command])
        self.read = []
        real = config_module.read_layer

        def spy(path, *args):
            self.read.append(Path(path))
            return real(path, *args)
        reader = patch.object(config_module, "read_layer", side_effect=spy)
        reader.start()
        self.addCleanup(reader.stop)

    def test_demo_and_smoke_never_read_or_run_the_target_project_file(self):
        def offline(config, repo, backend=None, **kwargs):
            # smoke asks for the real CLIs (backend None); here every model call is the offline demo.
            return Runtime(config, repo, backend=backend or DemoBackend(), **kwargs)

        for command in ("demo", "smoke"):
            with self.subTest(command=command):
                folder = scratch(self)
                output, errors = io.StringIO(), io.StringIO()
                previous = signal.getsignal(signal.SIGTERM)
                try:
                    # The fixture repository lands in this test's folder, not the shared temp root.
                    with patch.object(tempfile, "tempdir", str(folder)), patch.object(cli, "Runtime", offline), \
                            contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                        code = cli.main(["--config", str(OPERATOR), "--repo", str(self.repo), command])
                finally:
                    signal.signal(signal.SIGTERM, previous)
                self.assertEqual(code, 0, errors.getvalue())
                result = json.loads(output.getvalue())
                self.assertEqual(result["status"], "completed")
                self.assertTrue(Path(result["repo"]).is_relative_to(folder))
                saved = read_json(Path(result["repo"]) / ".harness" / "runs" / result["run"] / "state.json")
                self.assertEqual(saved["config_snapshot"]["verification"]["commands"],
                                 [{"cwd": ".", "argv": [sys.executable, "-m", "unittest", "-v"]}])
                self.assertEqual(saved["config_snapshot"]["permissions"]["worker_commands"], [])
        self.assertEqual(self.read, [OPERATOR.resolve()] * 2)
        self.assertFalse(self.marker.exists())

    def test_standalone_smokes_never_read_or_grant_the_target_project_file(self):
        from harness import permission_smoke, tool_smoke

        def process(argv, cwd, log, **kwargs):
            # No model runs: an empty successful stream, which is not passing evidence.
            log.with_suffix(".stdout").write_text(json.dumps({"type": "result", "subtype": "success",
                                                              "permission_denials": []}), encoding="utf-8")
            log.with_suffix(".stderr").write_text("", encoding="utf-8")
            return 0

        for module, folder in ((permission_smoke, "permission-smoke"), (tool_smoke, "tool-smoke")):
            with self.subTest(module=module.__name__):
                previous = signal.getsignal(signal.SIGTERM)
                try:
                    with patch.object(module, "run_process", side_effect=process), \
                            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        code = module.main(["--config", str(OPERATOR), "--repo", str(self.repo)])
                finally:
                    signal.signal(signal.SIGTERM, previous)
                self.assertEqual(code, 1)
                evidence = [path for path in (self.repo / ".harness" / folder).rglob("*")
                            if path.is_file() and not path.is_symlink()]
                self.assertIn("command.json", [path.name for path in evidence])
                # Neither the CLI argv and its settings nor the command broker's files grant the project's command.
                self.assertEqual([path for path in evidence if self.marker.name.encode() in path.read_bytes()], [])
        self.assertEqual(self.read, [OPERATOR.resolve()] * 2)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
