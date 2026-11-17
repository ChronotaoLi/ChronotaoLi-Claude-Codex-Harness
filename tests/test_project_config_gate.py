"""The project file gate (spec 2.4) and the commands it never stops (spec 2.3).

Every CLI call here loads its configuration files for real; only the model backend is
offline. run, resume and permission-diagnostics refuse a missing project file before any
write; a broken one fails as a broken operator file does. abandon, the commands that read
no configuration, and the test entry points do not depend on the target's project file.
"""
import contextlib
import io
import json
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import cli, git
from harness import config as config_module
from harness.adapters import DemoBackend
from harness.runtime import Runtime
from harness.util import read_json
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
                    reference = (code, output, cli_notice(errors))
                    replace(self.project, project)
                    before = self.footprint()
                    code, output, errors = self.invoke(*argv)
                    stop = cli_notice(errors)
                    self.assertEqual((code, output, stop), reference)
                    self.assertNotEqual(stop["kind"], "invalid_input")
                    self.assertEqual(self.footprint(), before)
        self.assertEqual(ledger.read_bytes(), saved)
        self.assertFalse((self.repo / ".harness" / "operations").exists())
        self.assertEqual((self.engines, self.backend.calls, self.quota_calls.all()), ([], calls, probes))


class EmptyVerificationTests(GateCase):
    # run's refusal of an empty verification.commands is untyped, and spec 2.4 keeps it unchanged.
    allow_unclassified = True

    def test_run_is_refused_when_the_valid_project_file_has_no_verification_commands(self):
        state = self.repo / ".harness"
        for case, content in (("no verification section", b"[permissions]\nworker_commands = []\n"),
                              ("empty commands", b"[verification]\ncommands = []\n")):
            with self.subTest(case=case):
                replace(self.project, content)
                before, records = self.footprint(), set(state.glob("operations/*.json"))
                code, output, errors = self.invoke("run", "Implement greet", "--plan", str(PLAN))
                self.assertNotEqual(code, 0, errors)
                self.assertEqual(output, "")
                after = self.footprint()
                self.assertEqual((after["refs"], after["worktrees"]), (before["refs"], before["worktrees"]))
                self.assertFalse((state / "runs").exists())
                self.assertFalse((state / "worktrees").exists())
                # The gate is in the run's start, after the operation record: that record is finished with no run.
                (record,) = set(state.glob("operations/*.json")) - records
                self.assertEqual({key: read_json(record)[key] for key in ("status", "exit_status", "run_id")},
                                 {"status": "finished", "exit_status": code, "run_id": None})
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
        self.assertEqual(code, 1, errors)
        self.assertEqual(ledger.read_bytes(), saved)


class EntryPointTests(RepoCase):
    """demo, smoke and the two standalone smokes set their own commands: the target's project file
    is never read, and the command it names, which would leave a marker, never runs."""

    def setUp(self):
        super().setUp()
        folder = scratch(self)
        self.marker = folder / "project-file-command-ran"
        command = [sys.executable, "-c", f"import pathlib; pathlib.Path({str(self.marker)!r}).write_text('ran')"]
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
                                 [[sys.executable, "-m", "unittest", "-v"]])
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
