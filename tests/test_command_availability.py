"""Spec 2 rules 6, 7, 16 and 17: one availability verdict, the same at the plan, continuation and environment gates.

One matrix of exact commands goes through commands.available_through and through each gate's run lifecycle
with scripted backends; no model or CLI is called.
"""
import copy
import unittest
from pathlib import Path

from harness import commands
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase, scratch
from test_runtime_changes import ScriptedBackend

FLUTTER, NPM = ("flutter", "test"), ("npm", "ci")
QUERY, OTHER_QUERY = ("git", "ls-files", "-z", "--", "src"), ("git", "ls-files", "-z", "--", "lib")  # git_query table forms


def at(cwd, argv=FLUTTER):
    return {"cwd": cwd, "argv": list(argv)}


GRANTS = [at("app"), at("café"), at(".", QUERY)]  # permissions.worker_commands
SETUP = [at("app", NPM)]                                # verification.setup_commands
MATRIX = (
    (at("app"), "grant"),
    (at("café"), "grant"),
    (at(".", QUERY), "grant"),                 # granted and also a table form: the grant answers
    (at(".", OTHER_QUERY), "builtin_git_query"),
    (at("app", OTHER_QUERY), None),            # git_query runs only at the worktree root
    (at("."), None),                           # the granted argv in another directory
    (at("App"), None),                         # no case folding
    (at("café"), None),                  # no Unicode normalization: NFD against an NFC grant
    (at("app", NPM), None),                    # setup_commands grant nothing
    (at(".", ("git", "status")), None),        # not a table form
)
COMMANDS = [command for command, _ in MATRIX]
MISSING = [at("app", OTHER_QUERY), at("."), at("App"), at("café"), at("app", NPM), at(".", ("git", "status"))]


class AvailabilityVerdictTests(unittest.TestCase):
    def test_each_command_gets_its_verdict(self):
        config = {"verification": {"commands": [at(".", ("make", "test"))], "fresh_checkout": True, "setup_commands": SETUP},
                  "permissions": {"worker_commands": GRANTS}}
        cases = MATRIX + ((at(".", ("make", "test")), "grant"), (at("app", ("make", "test")), None))
        for command, verdict in cases:
            with self.subTest(command=command):
                self.assertEqual(commands.available_through(command, config), verdict)
        self.assertEqual([command for command, verdict in MATRIX if verdict is None], MISSING)


class GateAvailabilityTests(RepoCase):
    """Each gate answers the matrix exactly as the verdicts above; nothing is executed or granted."""

    def setUp(self):
        super().setUp()
        self.config.data["verification"].update(fresh_checkout=True, setup_commands=copy.deepcopy(SETUP))
        self.config.data["permissions"]["worker_commands"] = copy.deepcopy(GRANTS)

    def plan(self, **fields):
        value = read_json(ROOT / "examples" / "plan.json")
        value["tasks"][0].update(copy.deepcopy(fields))
        return value

    def stopped(self, engine, call, kind, reason):
        with self.assertRaises(HarnessError):
            call()
        run = read_json(engine.directory / "state.json")
        self.assertEqual((run["status"], run["stop"]["kind"], run["stop"]["reason"]), ("blocked", kind, reason))
        return run

    def workers(self, backend):
        return sum(call["role"] == "worker" for call in backend.calls)

    def test_the_plan_gate_holds_back_exactly_the_unavailable_commands(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        run = self.stopped(engine, lambda: engine.start("Implement greet", self.plan(required_commands=COMMANDS)),
                           "missing_commands", "all_ready_tasks")
        self.assertEqual((run["stop"]["data"]["missing"], run["command_requirements"]), ({"greet": MISSING}, {"missing": {"greet": MISSING}}))
        self.assertEqual(backend.calls, [])

    def test_a_continuation_request_waits_for_exactly_the_unavailable_commands(self):
        def request(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.update(status="blocked", blocker_kind="continuation", blockers=["Run the app's tests"],
                              continuation_request={"kind": "commands", "commands": copy.deepcopy(COMMANDS),
                                                    "minimum_timeout_seconds": 0, "reason": "Run the app's tests"})
        backend = ScriptedBackend(request)
        engine = self.engine(backend)
        run = self.stopped(engine, lambda: engine.start("Implement greet", self.plan()), "continuation", "commands_required")
        self.assertEqual(run["stop"]["data"]["missing_commands"], MISSING)
        self.assertEqual(self.workers(backend), 1)

    def test_an_environment_check_passes_exactly_the_available_commands_and_names_builtin_coverage(self):
        def block_first(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and self.workers(backend) == 1:
                result.update(status="blocked", blocker_kind="environment", blockers=["The app toolchain is missing"])
        backend = ScriptedBackend(block_first)
        engine = self.engine(backend)
        run = self.stopped(engine, lambda: engine.start("Implement greet", self.plan()), "environment", "worker_blocked")
        checks = [{"kind": "worker_command", "command": command} for command in COMMANDS]
        path = scratch(self) / "update.json"
        write_json(path, {"schema_version": 1, "task_id": "greet", "summary": "Installed the app toolchain", "checks": checks,
                          "blocker_dispatch_id": next(row["id"] for row in run["dispatches"] if row["role"] == "worker")})
        failed = self.stopped(engine, lambda: self.engine(backend).resume(run["id"], environment_update=path),
                              "environment", "checks_failed")
        report = read_json(Path(failed["environment_checks"][-1]["artifact"]))
        expected = [{"check": check, "passed": verdict is not None, **({"coverage": verdict} if verdict == "builtin_git_query" else {})}
                    for check, (_, verdict) in zip(checks, MATRIX)]
        self.assertEqual((report["passed"], report["results"]), (False, expected))
        self.assertEqual(self.workers(backend), 1)
        self.assertEqual(self.config.data["permissions"]["worker_commands"], GRANTS)


if __name__ == "__main__":
    unittest.main()
