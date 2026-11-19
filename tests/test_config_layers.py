import contextlib
import copy
import hashlib
import io
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness import cli, git
from harness.config import DEFAULTS, Config
from harness.operations import first_line
from harness.shared import toml_value
from harness.util import HarnessError
from test_harness import ROOT, RepoCase, scratch, write_project_config


def write_toml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{key} = {toml_value(value)}" for key, value in data.items()) + "\n", encoding="utf-8")
    return path


class LayerMergeTests(unittest.TestCase):
    """Effective configuration = built-in defaults <- operator file <- project file."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.operator_path = self.root / "operator" / "harness.toml"
        self.project_path = self.root / "repo" / ".harness" / "project.toml"
        self.operator = tomllib.loads((ROOT / "harness.toml").read_text(encoding="utf-8"))
        self.project = {"verification": {"commands": [{"cwd": ".", "argv": ["make", "check"]}]}}

    def load(self, operator=None, project=None, *, with_project=True):
        write_toml(self.operator_path, self.operator if operator is None else operator)
        if not with_project:
            return Config.load(self.operator_path)
        write_toml(self.project_path, self.project if project is None else project)
        return Config.load(self.operator_path, self.project_path)

    def test_project_runtime_overrides_operator_key_by_key_over_defaults(self):
        self.operator["runtime"] = {"max_attempts": 7, "context_chars": 50000,
                                    "claude_max_turns_by_role": {"worker": 200, "reviewer": 30}}
        self.project["runtime"] = {"max_attempts": 9, "claude_max_turns_by_role": {"commander": 40}}
        runtime = self.load().data["runtime"]
        self.assertEqual(runtime["max_attempts"], 9)                       # project over operator
        self.assertEqual(runtime["context_chars"], 50000)                  # operator over default
        self.assertEqual(runtime["agent_timeout_seconds"], 3600)           # default where neither sets it
        # The role table is one value: replaced whole, never merged per role.
        self.assertEqual(runtime["claude_max_turns_by_role"], {"commander": 40})
        operator_only = self.load(with_project=False).data["runtime"]
        self.assertEqual((operator_only["max_attempts"], operator_only["claude_max_turns_by_role"]),
                         (7, {"worker": 200, "reviewer": 30}))

    def test_verification_and_permissions_come_only_from_the_project_file(self):
        self.project = {"verification": {"commands": [{"cwd": ".", "argv": ["make", "check"]}, {"cwd": ".", "argv": ["echo", "*", "a\nb"]}],
                                         "fresh_checkout": True, "setup_commands": [{"cwd": ".", "argv": ["make", "deps"]}]},
                        "permissions": {"worker_commands": [{"cwd": ".", "argv": ["npm", "run", "build"]}]}}
        config = self.load()
        self.assertEqual(config.data["verification"], self.project["verification"])
        self.assertEqual(config.data["permissions"], self.project["permissions"])
        # A project file without them, and the operator file alone, give the built-in defaults.
        for config in (self.load(project={"runtime": {"max_attempts": 2}}), self.load(with_project=False)):
            self.assertEqual(config.data["verification"], DEFAULTS["verification"])
            self.assertEqual(config.data["permissions"], DEFAULTS["permissions"])
        # Arrays replace the defaults; nothing is appended.
        self.project = {"verification": {"commands": [{"cwd": ".", "argv": ["make", "check"]}]}}
        self.assertEqual(self.load().data["verification"]["commands"], [{"cwd": ".", "argv": ["make", "check"]}])

    def test_effective_configuration_is_validated_with_the_existing_rules(self):
        invalid = [
            ({}, {"runtime": {"max_attempts": 0}}),
            ({}, {"runtime": {"claude_max_turns_by_role": {"worker": True}}}),
            ({}, {"verification": {"commands": "npm test"}}),
            ({}, {"verification": {"commands": [{"cwd": ".", "argv": ["true"]}], "setup_commands": [{"cwd": ".", "argv": ["make", "deps"]}]}}),
            ({}, {"permissions": {"worker_commands": [{"cwd": ".", "argv": ["npm", 7]}]}}),
            ({"worker": "codex"}, {}),
        ]
        for operator_change, project in invalid:
            operator = copy.deepcopy(self.operator)
            if operator_change:
                operator["roles"]["worker"]["cli"] = operator_change["worker"]
            with self.subTest(operator=operator_change, project=project), self.assertRaises(HarnessError):
                self.load(operator, {**self.project, **project})
        config = self.load()
        self.assertEqual(config.resume_changes(copy.deepcopy(config.data)), {})

    def test_a_section_in_the_wrong_file_is_rejected_naming_where_it_belongs(self):
        for section, value in (("verification", {"commands": [{"cwd": ".", "argv": ["true"]}]}), ("permissions", {"worker_commands": []})):
            for with_project in (True, False):
                with self.subTest(section=section, with_project=with_project):
                    with self.assertRaises(HarnessError) as raised:
                        self.load({**self.operator, section: value}, with_project=with_project)
                    message = str(raised.exception)
                    self.assertIn(str(self.operator_path.resolve()), message)
                    self.assertIn(section, message)
                    self.assertIn(".harness/project.toml", message)
        for section in ("roles", "memory", "shared"):
            with self.subTest(section=section):
                with self.assertRaises(HarnessError) as raised:
                    self.load(project={**self.project, section: copy.deepcopy(self.operator[section])})
                message = str(raised.exception)
                self.assertIn(str(self.project_path), message)
                self.assertIn(section, message)
                self.assertIn("--config", message)

    def test_unknown_sections_and_keys_report_their_full_path_and_their_file(self):
        project_cases = [
            ("verificaton", {"verificaton": {"commands": [{"cwd": ".", "argv": ["true"]}]}}),
            ("runtime.max_attempt", {"runtime": {"max_attempt": 3}}),
            ("runtime.claude_max_turns_by_role.fable", {"runtime": {"claude_max_turns_by_role": {"fable": 3}}}),
            ("verification.command", {"verification": {"command": [{"cwd": ".", "argv": ["true"]}]}}),
            ("permissions.worker_command", {"permissions": {"worker_command": []}}),
        ]
        for field, extra in project_cases:
            with self.subTest(file="project", field=field), self.assertRaises(HarnessError) as raised:
                project = copy.deepcopy(self.project)
                for section, values in extra.items():
                    project.setdefault(section, {}).update(values)
                self.load(project=project)
            self.assertIn(field, str(raised.exception))
            self.assertIn(str(self.project_path), str(raised.exception))
            self.assertNotIn(str(self.operator_path.resolve()), str(raised.exception))
        operator_cases = [("runtim", ("runtim",), {}), ("runtime.max_attempt", ("runtime", "max_attempt"), 1),
                          ("roles.worker.modle", ("roles", "worker", "modle"), "exact-unintended-model")]
        for field, keys, value in operator_cases:
            operator = copy.deepcopy(self.operator)
            target = operator
            for key in keys[:-1]:
                target = target[key]
            target[keys[-1]] = value
            with self.subTest(file="operator", field=field), self.assertRaises(HarnessError) as raised:
                self.load(operator)
            self.assertIn(field, str(raised.exception))
            self.assertIn(str(self.operator_path.resolve()), str(raised.exception))
            self.assertNotIn(str(self.project_path), str(raised.exception))
        with self.assertRaises(HarnessError) as raised:
            self.load(project={**self.project, "runtime": 5})
        self.assertIn(str(self.project_path), str(raised.exception))

    def load_text(self, text):
        """The project file written as TOML text, beside the checked-in operator file."""
        write_toml(self.operator_path, self.operator)
        self.project_path.parent.mkdir(parents=True, exist_ok=True)
        self.project_path.write_text(text, encoding="utf-8")
        return Config.load(self.operator_path, self.project_path)

    def test_tables_and_inline_tables_load_the_same_commands(self):
        tables = ('[[verification.commands]]\ncwd = "."\nargv = ["make", "check"]\n\n'
                  '[[permissions.worker_commands]]\ncwd = "spikes/sp-2-export/app"\nargv = ["flutter", "test"]\n\n'
                  '[[permissions.worker_commands]]\ncwd = "my app/src"\nargv = ["npm", "run", "build"]\n')
        inline = ('[verification]\ncommands = [{ cwd = ".", argv = ["make", "check"] }]\n\n[permissions]\n'
                  'worker_commands = [{ cwd = "spikes/sp-2-export/app", argv = ["flutter", "test"] },\n'
                  '                   { cwd = "my app/src", argv = ["npm", "run", "build"] }]\n')
        for name, text in (("tables", tables), ("inline", inline)):
            with self.subTest(name):
                config = self.load_text(text)
                self.assertEqual(config.data["verification"]["commands"], [{"cwd": ".", "argv": ["make", "check"]}])
                self.assertEqual(config.data["permissions"]["worker_commands"],
                                 [{"cwd": "spikes/sp-2-export/app", "argv": ["flutter", "test"]},
                                  {"cwd": "my app/src", "argv": ["npm", "run", "build"]}])

    def test_a_command_without_cwd_with_env_or_with_a_bad_cwd_names_its_field_and_file(self):
        good = '{ cwd = ".", argv = ["make", "check"] }'
        cases = {"no cwd": ('{ argv = ["make", "x"] }', ""),
                 "env": ('{ cwd = ".", argv = ["make", "x"], env = { A = "1" } }', ""),
                 "parent segment": ('{ cwd = "app/../x", argv = ["make", "x"] }', ".cwd"),
                 "protected": ('{ cwd = "app/.Git", argv = ["make", "x"] }', ".cwd"),
                 "blank": ('{ cwd = "  ", argv = ["make", "x"] }', ".cwd"),
                 "blank segment": ('{ cwd = "a/ /b", argv = ["make", "x"] }', ".cwd"),
                 "empty argv": ('{ cwd = "app", argv = [] }', ".argv")}
        layouts = {"verification.commands": "[verification]\ncommands = [{good}, {bad}]\n",
                   "verification.setup_commands": ("[verification]\ncommands = [{good}]\nfresh_checkout = true\n"
                                                   "setup_commands = [{good}, {bad}]\n"),
                   "permissions.worker_commands": "[verification]\ncommands = [{good}]\n\n[permissions]\nworker_commands = [{good}, {bad}]\n"}
        for key, layout in layouts.items():
            for name, (bad, suffix) in cases.items():
                with self.subTest(key=key, case=name), self.assertRaises(HarnessError) as raised:
                    self.load_text(layout.format(good=good, bad=bad))
                message = str(raised.exception)
                self.assertIn(f"{key}[1]{suffix}", message)
                self.assertNotIn(f"{key}[0]", message)
                for part in (".cwd", ".argv"):
                    self.assertEqual(f"{key}[1]{part}" in message, suffix == part)
                self.assertIn(str(self.project_path), message)

    def test_exact_model_ids_survive_and_shared_paths_resolve_beside_the_operator_file(self):
        self.operator["roles"]["worker"]["model"] = "exact-custom-worker-model-v42[1m]"
        config = self.load()
        self.assertEqual(config.role("worker")["model"], "exact-custom-worker-model-v42[1m]")
        self.assertEqual(config.path, self.operator_path.resolve())
        self.assertEqual(config.root, self.operator_path.resolve().parent)
        for key in ("rules", "skills", "mcp"):
            self.assertEqual(config.asset(key), (self.operator_path.parent / self.operator["shared"][key]).resolve())

    def test_each_file_is_digested_from_the_bytes_that_were_parsed(self):
        config = self.load()
        self.assertEqual(config.sha256, hashlib.sha256(self.operator_path.read_bytes()).hexdigest())
        # The path as given: the project file's place is fixed, so it is never resolved.
        self.assertEqual(config.project_path, self.project_path)
        self.assertEqual(config.project_sha256, hashlib.sha256(self.project_path.read_bytes()).hexdigest())
        alone = self.load(with_project=False)
        self.assertEqual((alone.project_path, alone.project_sha256), (None, None))
        self.assertEqual(alone.sha256, config.sha256)
        constructed = Config(config.path, copy.deepcopy(config.data))
        self.assertEqual((constructed.sha256, constructed.project_path, constructed.project_sha256), (None, None, None))

    def test_doctor_layers_the_project_file_over_its_loaded_operator_configuration_as_run_loads_both(self):
        # doctor holds the operator file already loaded, defaults filled in; the project file over it gives
        # what run's Config.load(operator, project) gives: the same effective configuration, or the same error.
        repo = self.project_path.parent.parent
        repo.mkdir(parents=True)
        git.git(repo, "init", "-b", "main")
        place = git.repository(repo) / ".harness" / "project.toml"   # where run and doctor both look
        tuned = {**self.operator, "runtime": {"max_attempts": 7, "context_chars": 50000,
                                              "claude_max_turns_by_role": {"worker": 200, "reviewer": 30}}}
        bare = {key: value for key, value in self.operator.items() if key != "runtime"}
        commands = {"verification": {"commands": [{"cwd": ".", "argv": ["make", "check"]}]}}
        cases = [  # name, operator file, project file (a table or raw bytes), loads
            ("runtime key by key, role table whole", tuned,
             {**commands, "runtime": {"max_attempts": 9, "claude_max_turns_by_role": {"commander": 40}}}, True),
            ("operator runtime kept where the project sets none", tuned, commands, True),
            ("project runtime over an operator file without one", bare, {**commands, "runtime": {"max_attempts": 2}}, True),
            ("verification and permissions only from the project", self.operator,
             {"verification": {"commands": [{"cwd": ".", "argv": ["make", "check"]}], "fresh_checkout": True, "setup_commands": [{"cwd": ".", "argv": ["make", "deps"]}]},
              "permissions": {"worker_commands": [{"cwd": ".", "argv": ["npm", "run", "build"]}]}}, True),
            ("no verification or permissions", tuned, {"runtime": {"quota_wait_max_seconds": 0}}, True),
            ("empty project file", self.operator, {}, True),
            ("invalid effective value", tuned, {**commands, "runtime": {"max_attempts": 0}}, False),
            ("invalid role table", tuned, {"runtime": {"claude_max_turns_by_role": {"worker": True}}}, False),
            ("setup commands without a fresh checkout", self.operator,
             {"verification": {"commands": [{"cwd": ".", "argv": ["true"]}], "setup_commands": [{"cwd": ".", "argv": ["make", "deps"]}]}}, False),
            ("operator section", self.operator, {**commands, "roles": copy.deepcopy(self.operator["roles"])}, False),
            ("unknown key", self.operator, {"verification": {"command": [{"cwd": ".", "argv": ["true"]}]}}, False),
            ("not TOML", self.operator, b"[verification\n", False),
            ("not UTF-8", self.operator, b"\xff\xfe", False),
        ]
        for name, operator, project, loads in cases:
            with self.subTest(case=name):
                write_toml(self.operator_path, operator)
                if isinstance(project, bytes):
                    place.write_bytes(project)
                else:
                    write_toml(place, project)
                try:
                    expected, expected_error = Config.load(self.operator_path, place).data, None
                except (HarnessError, ValueError) as error:
                    expected, expected_error = None, first_line(error)
                self.assertEqual(expected_error is None, loads, expected_error)
                loaded = Config.load(self.operator_path)   # what doctor holds
                before = copy.deepcopy(loaded.data)
                layered, real = [], Config.effective

                def effective(*args):
                    layered.append(real(*args))
                    return layered[-1]
                with patch.object(Config, "effective", side_effect=effective):
                    report = cli.project_report(loaded, repo)
                self.assertEqual(report["error"], expected_error)
                self.assertEqual(report["sha256"], hashlib.sha256(place.read_bytes()).hexdigest())
                self.assertEqual(layered, [] if expected is None else [expected])
                self.assertEqual(loaded.data, before)   # doctor's own configuration is left as loaded

    def test_example_project_file_loads_with_the_checked_in_operator_file(self):
        operator = Config.load(ROOT / "harness.toml")
        self.assertEqual(operator.data["verification"], DEFAULTS["verification"])
        self.assertEqual(operator.data["permissions"], DEFAULTS["permissions"])
        config = Config.load(ROOT / "harness.toml", ROOT / "examples" / "project.toml")
        self.assertTrue(config.data["verification"]["commands"])
        self.assertEqual(config.data["roles"], operator.data["roles"])
        self.assertEqual(config.data["runtime"], operator.data["runtime"])


class ProjectFileLocationTests(RepoCase):
    """The CLI reads <Git root>/.harness/project.toml: no upward search, no fallback."""

    def setUp(self):
        super().setUp()
        self.commands = [{"cwd": ".", "argv": ["python3", "-c", "print('root project')"]}]
        self.project = write_project_config(self.repo, self.commands)
        self.deep = self.repo / "pkg" / "deep"
        self.deep.mkdir(parents=True)
        # A project file below the Git root is never the one loaded.
        write_project_config(self.repo / "pkg", [{"cwd": ".", "argv": ["python3", "-c", "print('decoy')"]}])
        self.outside = scratch(self)

    def invoke(self, argv, cwd):
        loaded, output, errors = [], io.StringIO(), io.StringIO()

        def operate(args, config, repo, state):
            loaded.append(config)
            return 0

        with patch("harness.cli.operate", side_effect=operate), contextlib.chdir(cwd), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(argv)
        return code, loaded, errors.getvalue()

    def test_every_working_directory_finds_the_same_file_at_the_git_root(self):
        variants = [
            (self.outside, ["--repo", str(self.repo)]),
            (self.outside, ["--repo", str(self.deep)]),
            (self.deep, []),
            (self.repo, []),
        ]
        for cwd, prefix in variants:
            for command in (["run", "goal"], ["resume", "run_test"]):
                with self.subTest(cwd=cwd, prefix=prefix, command=command[0]):
                    code, loaded, errors = self.invoke([*prefix, *command], cwd)
                    self.assertEqual(code, 0, errors)
                    self.assertEqual(len(loaded), 1)
                    self.assertEqual(loaded[0].project_path, self.project.resolve())
                    self.assertEqual(loaded[0].data["verification"]["commands"], self.commands)

    def test_a_repository_without_its_own_file_never_uses_one_above_or_below_its_root(self):
        inner = self.repo / "vendor" / "inner"
        inner.mkdir(parents=True)
        git.git(inner, "init", "-b", "main")
        (inner / "sub").mkdir()
        write_project_config(inner / "sub", [{"cwd": ".", "argv": ["python3", "-c", "print('decoy')"]}])
        expected = (git.repository(inner) / ".harness" / "project.toml").resolve()
        for cwd, prefix in ((self.outside, ["--repo", str(inner)]), (inner / "sub", [])):
            with self.subTest(cwd=cwd):
                code, loaded, errors = self.invoke([*prefix, "run", "goal"], cwd)
                self.assertNotEqual(code, 0)
                self.assertEqual(loaded, [])
                self.assertIn(str(expected), errors)
        self.assertFalse(expected.exists())

    def test_a_linked_worktree_reads_the_file_at_its_own_root_not_the_main_checkout(self):
        tree = self.outside / "linked"
        git.git(self.repo, "worktree", "add", "-b", "linked", str(tree))
        expected = (tree / ".harness" / "project.toml").resolve()
        # Without a file of its own the worktree is refused, although the main checkout has one.
        code, loaded, errors = self.invoke(["--repo", str(tree), "run", "goal"], self.outside)
        self.assertNotEqual(code, 0)
        self.assertEqual(loaded, [])
        self.assertIn(str(expected), errors)
        self.assertNotIn(str(self.project.resolve()), errors)
        commands = [{"cwd": ".", "argv": ["python3", "-c", "print('linked worktree')"]}]
        write_project_config(tree, commands)
        for cwd, prefix in ((self.outside, ["--repo", str(tree)]), (tree, [])):
            with self.subTest(cwd=cwd):
                code, loaded, errors = self.invoke([*prefix, "run", "goal"], cwd)
                self.assertEqual(code, 0, errors)
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0].project_path, expected)
                self.assertEqual(loaded[0].data["verification"]["commands"], commands)

    def test_a_project_file_that_links_to_a_regular_file_loads(self):
        commands = [{"cwd": ".", "argv": ["python3", "-c", "print('linked file')"]}]
        target = write_project_config(self.outside / "kept", commands)
        self.project.unlink()
        self.project.symlink_to(target)
        code, loaded, errors = self.invoke(["--repo", str(self.repo), "run", "goal"], self.outside)
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].data["verification"]["commands"], commands)
        self.assertEqual(loaded[0].project_sha256, hashlib.sha256(target.read_bytes()).hexdigest())
        # The link is followed when read; the place shown is the fixed one, never the link's target.
        self.assertEqual(loaded[0].project_path, git.repository(self.repo) / ".harness" / "project.toml")

    def test_a_project_file_that_cannot_be_read_is_not_reported_as_a_missing_operator_file(self):
        # The missing-operator-file error names the operator file; it exists here, so naming it would be wrong.
        operator = (ROOT / "harness.toml").resolve()
        self.assertTrue(operator.is_file())
        self.project.unlink()

        def directory(path):
            path.mkdir()
            return path.rmdir

        def unreadable(path):
            write_project_config(self.repo, self.commands).chmod(0)
            return lambda: (path.chmod(0o600), path.unlink())

        def dangling(path):
            path.symlink_to(self.outside / "missing.toml")
            return path.unlink

        for name, place in (("directory", directory), ("unreadable file", unreadable), ("dangling link", dangling)):
            with self.subTest(case=name):
                if place is unreadable and os.geteuid() == 0:
                    self.skipTest("root reads a file whatever its mode")
                undo = place(self.project)
                try:
                    code, loaded, errors = self.invoke(["--repo", str(self.repo), "run", "goal"], self.outside)
                    self.assertNotEqual(code, 0)
                    self.assertNotIn(str(operator), errors)
                    # Refused before anything is written: no run, no operation record.
                    self.assertEqual(loaded, [])
                    self.assertEqual([entry.name for entry in (self.repo / ".harness").iterdir()], ["project.toml"])
                    if place is dangling:
                        # Refused as a missing file at the expected place, not at the link's target.
                        self.assertEqual(code, 2)
                        self.assertIn(str(git.repository(self.repo) / ".harness" / "project.toml"), errors)
                        self.assertNotIn("missing.toml", errors)
                finally:
                    undo()


class CommandConfigurationTests(RepoCase):
    """Which file each command reads (spec 2.3): only run, resume and permission-diagnostics read the project file."""

    def setUp(self):
        super().setUp()
        self.commands = [{"cwd": ".", "argv": ["python3", "-c", "print('project')"]}]
        self.project = write_project_config(self.repo, self.commands, [{"cwd": ".", "argv": ["make", "deploy"]}])

    def loads(self, argv):
        loaded, real = [], Config.load

        def spy(*args):
            config = real(*args)
            loaded.append(config)
            return config

        engine = Mock()
        engine.abandon.return_value = {"id": "run_test", "status": "abandoned", "branch": "b", "worktree": "w"}
        with patch("harness.cli.Config.load", side_effect=spy), \
                patch("harness.cli.operate", return_value=0), patch("harness.cli.doctor", return_value=0), \
                patch("harness.cli.demo"), patch("harness.cli.Runtime", return_value=engine), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--config", str(ROOT / "harness.toml"), "--repo", str(self.repo), *argv])
        self.assertEqual(len(loaded), 1, argv)
        return loaded[0]

    def test_only_the_commands_acting_on_the_target_load_its_project_file(self):
        for argv in (["run", "goal"], ["resume", "run_test"], ["permission-diagnostics", "run_test", "dispatch-1"]):
            with self.subTest(command=argv[0]):
                config = self.loads(argv)
                self.assertEqual(config.project_path, self.project.resolve())
                self.assertEqual(config.data["verification"]["commands"], self.commands)
                self.assertEqual(config.data["permissions"]["worker_commands"], [{"cwd": ".", "argv": ["make", "deploy"]}])
        for argv in (["abandon", "run_test"], ["doctor"], ["demo"], ["smoke"], ["sync"]):
            with self.subTest(command=argv[0]):
                config = self.loads(argv)
                self.assertIsNone(config.project_path)
                self.assertEqual(config.data["verification"], DEFAULTS["verification"])
                self.assertEqual(config.data["permissions"], DEFAULTS["permissions"])


if __name__ == "__main__":
    unittest.main()
