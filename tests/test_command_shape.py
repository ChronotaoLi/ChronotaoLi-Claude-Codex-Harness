import hashlib
import json
import unittest

from harness import commands, contracts
from harness.util import HarnessError


def command(cwd, argv=("flutter", "test")):
    return {"cwd": cwd, "argv": list(argv)}


VALID_CWDS = (".", "a", "a/b", "my app/src", "spikes/sp-2-export/app", "x" * commands.CWD_MAX_CHARS)
INVALID_CWDS = ("", " ", "a/ /b", "/abs", "a/", "./a", "a/./b", "a//b", "..", "a/../b", "a\\b", "a\0b",
                "\ud800", "app/\udcff", ".git", ".GIT/x", "a/.Git/b", ".harness/x", ".Claude", ".codex",
                1, None, ["app"], "x" * (commands.CWD_MAX_CHARS + 1))


class CommandShapeTests(unittest.TestCase):
    def test_relative_directories_inside_the_worktree_are_accepted(self):
        for cwd in VALID_CWDS:
            with self.subTest(cwd=cwd[:20]):
                commands.validate_command(command(cwd), "permissions.worker_commands[0]")
                self.assertTrue(commands.valid_command(command(cwd)))
        commands.validate_commands([command(cwd) for cwd in VALID_CWDS], "verification.commands")

    def test_each_invalid_cwd_is_refused_with_its_field_path(self):
        for cwd in INVALID_CWDS:
            with self.subTest(cwd=repr(cwd)[:40]):
                self.assertFalse(commands.valid_command(command(cwd)))
                with self.assertRaises(HarnessError) as caught:
                    commands.validate_commands([command("."), command(cwd)], "permissions.worker_commands")
                self.assertIn("permissions.worker_commands[1].cwd", str(caught.exception))

    def test_object_shape_and_argv_rules_are_refused_with_their_field_path(self):
        cases = {
            "missing cwd": ({"argv": ["make"]}, "verification.setup_commands[1]"),
            "env key": ({"cwd": ".", "argv": ["make"], "env": {"A": "1"}}, "verification.setup_commands[1]"),
            "bare argv": (["make"], "verification.setup_commands[1]"),
            "empty argv": ({"cwd": ".", "argv": []}, "verification.setup_commands[1].argv"),
            "blank first item": ({"cwd": ".", "argv": [" ", "x"]}, "verification.setup_commands[1].argv"),
            "NUL in argv": ({"cwd": ".", "argv": ["make", "a\0b"]}, "verification.setup_commands[1].argv"),
            "argv not a list": ({"cwd": ".", "argv": "make"}, "verification.setup_commands[1].argv"),
        }
        for name, (value, field) in cases.items():
            with self.subTest(name):
                self.assertFalse(commands.valid_command(value))
                with self.assertRaises(HarnessError) as caught:
                    commands.validate_commands([command("."), value], "verification.setup_commands")
                self.assertIn(field, str(caught.exception))
        with self.assertRaisesRegex(HarnessError, "verification.commands"):
            commands.validate_commands({"cwd": ".", "argv": ["make"]}, "verification.commands")

    def test_command_id_covers_the_directory_and_ignores_key_order(self):
        argv = ["flutter", "test", "--name", "a b"]
        root, app = commands.command_id(command(".", argv)), commands.command_id(command("app", argv))
        self.assertNotEqual(root, app)
        self.assertEqual(commands.command_id({"argv": argv, "cwd": "app"}), app)
        self.assertEqual(commands.command_id(command("app", list(argv))), app)
        # Not the old argv-only identity, even at the root.
        argv_only = hashlib.sha256(json.dumps(argv, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        self.assertNotEqual(root, argv_only)
        self.assertEqual(len({root, app, argv_only}), 3)
        self.assertEqual(commands.command_key({"argv": argv, "cwd": "app"}), ("app", tuple(argv)))

    def test_command_id_is_the_fixed_digest_of_ascii_escaped_compact_sorted_json(self):
        # sha256 of {"argv":["flutter","test","--name","é ü"],"cwd":"应用/src"}: non-ASCII
        # text escaped, no spaces after separators; a raw UTF-8 or spaced encoding gives another digest.
        value = {"cwd": "应用/src", "argv": ["flutter", "test", "--name", "é ü"]}
        self.assertEqual(commands.command_id(value), "33fdab98a1644a39decf4e257e3778ad7e6c2553a7c8b0b23921d2294bb2c808")

    def test_grants_match_only_the_exact_pair_from_verification_and_worker_commands(self):
        config = {"verification": {"commands": [command(".", ["make", "test"])],
                                   "setup_commands": [command("app", ["npm", "ci"])]},
                  "permissions": {"worker_commands": [command("app", ["flutter", "test"])]}}
        self.assertTrue(commands.granted(command("app", ["flutter", "test"]), config))
        self.assertTrue(commands.granted({"argv": ["flutter", "test"], "cwd": "app"}, config))
        self.assertTrue(commands.granted(command(".", ["make", "test"]), config))
        for refused in (command("App", ["flutter", "test"]), command(".", ["flutter", "test"]),
                        command("app/", ["flutter", "test"]), command("app", ["flutter"]),
                        command("app", ["flutter", "test", "-v"]), command("app", ["make", "test"]),
                        command("app", ["npm", "ci"])):
            with self.subTest(refused=refused):
                self.assertFalse(commands.granted(refused, config))
        upper = {"verification": {"commands": [], "setup_commands": []},
                 "permissions": {"worker_commands": [command("App", ["flutter", "test"])]}}
        self.assertTrue(commands.granted(command("App", ["flutter", "test"]), upper))
        self.assertFalse(commands.granted(command("app", ["flutter", "test"]), upper))

    def test_git_query_covers_only_table_forms_at_the_worktree_root(self):
        query = ["git", "ls-files", "-z", "--", "src"]
        self.assertTrue(commands.git_query_covered(command(".", query)))
        self.assertFalse(commands.git_query_covered(command("app", query)))
        self.assertFalse(commands.git_query_covered(command(".", ["git", "status"])))
        self.assertFalse(commands.git_query_covered(command(".", ["flutter", "test"])))


class ContractCommandShapeTests(unittest.TestCase):
    """The plan contract and validate_commands reach the same verdict on one command's directory."""

    def task(self, *required):
        return {"id": "t1", "required_commands": list(required)}

    def test_a_subdirectory_command_passes_the_task_contract(self):
        app = command("app")
        self.assertEqual(contracts.validate_required_commands(self.task(app, command("."))), [app, command(".")])
        self.assertEqual(contracts.validate_required_commands({"id": "t1", "self_checks": [app]}), [app])
        for cwd in VALID_CWDS:
            with self.subTest(cwd=cwd[:20]):
                contracts.validate(cwd, contracts.CWD, "cwd")
                self.assertEqual(contracts.validate_required_commands(self.task(command(cwd))), [command(cwd)])

    def test_each_invalid_cwd_is_refused_by_the_task_contract_at_that_cwd(self):
        for cwd in INVALID_CWDS:
            for field in ("required_commands", "self_checks"):
                with self.subTest(cwd=repr(cwd)[:40], field=field):
                    with self.assertRaises(HarnessError) as caught:
                        contracts.validate_required_commands({"id": "t1", field: [command("."), command(cwd)]})
                    # Refused for the directory, never for the object's shape.
                    self.assertIn(f"task.{field}[1].cwd", str(caught.exception))

    def test_a_blank_cwd_is_refused_by_both_the_format_check_and_validate_commands(self):
        for cwd in (" ", "\t", "\u3000"):
            with self.subTest(cwd=repr(cwd)):
                with self.assertRaisesRegex(HarnessError, "cwd"):
                    contracts.validate(cwd, contracts.CWD, "cwd")
                with self.assertRaisesRegex(HarnessError, r"\[0\]\.cwd"):
                    commands.validate_commands([command(cwd)], "task.required_commands")

    def test_missing_cwd_extra_keys_and_bare_argv_are_refused_by_the_task_contract(self):
        for value in ({"argv": ["flutter", "test"]}, {"cwd": "app", "argv": ["flutter", "test"], "env": {}},
                      ["flutter", "test"]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(HarnessError, r"task\.required_commands\[0\]"):
                    contracts.validate_required_commands(self.task(value))

    def test_one_argv_in_two_directories_is_two_plan_commands_and_one_pair_twice_is_a_duplicate(self):
        plan = {"tasks": [{"id": "t1", "title": "T", "description": "D", "depends_on": [], "allowed_paths": ["src/**"],
                           "acceptance": ["A"], "skills": [], "required_commands": [command("a/app"), command("b/app")]}]}
        contracts.validate_plan(plan)
        plan["tasks"][0]["required_commands"].append(command("a/app"))
        with self.assertRaisesRegex(HarnessError, "重复|duplicate"):
            contracts.validate_plan(plan)


if __name__ == "__main__":
    unittest.main()
