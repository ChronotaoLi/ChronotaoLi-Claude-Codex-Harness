"""Spec 2 rules 4, 6 and 16-20 on the plan side: a command prerequisite is its exact (cwd, argv) pair at every gate.

Plans, continuations, environment checks and held proposals run offline with scripted backends; no model or CLI is called.
Directories compare as strings: none of the case or Unicode variants below has to exist, or be distinct, on disk.
A listed exit runs as the words it lists, a FILE placeholder replaced by one argument; the input files sit in a folder whose
name has a space, so splicing a path into the exit text and splitting it again fails.
"""
import contextlib
import copy
import hashlib
import io
import json
import re
import shlex
import signal
import unittest
from pathlib import Path

from harness import cli, stops
from harness.commands import command_id
from harness.contracts import apply_revision
from harness.permission_smoke import _git
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase, cli_notice, refused_by_cli, run_cli, scratch
from test_runtime_changes import ScriptedBackend, trigger_kind, update
from test_scope_boundary import blocked_then, sources, widen

FLUTTER = ("flutter", "test")
QUERY = ("git", "ls-files", "-z", "--", "src")  # a harness.git_query table form
CAFE, CAFE_NFD = "caf\u00e9", "cafe\u0301"  # one name, precomposed and decomposed: equal under NFC, not as strings
MUNICH, MUNICH_NFD = "m\u00fcnchen", "mu\u0308nchen"
GRANTS = "permissions.worker_commands"


def at(cwd, argv=FLUTTER):
    return {"cwd": cwd, "argv": list(argv)}


def plan(**fields):
    value = read_json(ROOT / "examples" / "plan.json")
    value["tasks"][0].update(copy.deepcopy(fields))
    return value


def saved(engine):
    return read_json(engine.directory / "state.json")


def facts(stop):
    """A stop without its free diagnostic text."""
    return {**stop, "data": {key: value for key, value in stop["data"].items() if key != "detail"}}


def workers(backend):
    return [call["task_id"] for call in backend.calls if call["role"] == "worker"]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def input_file(case, name):
    """A path for the operator input file ``name``, in a private folder whose name has a space."""
    folder = scratch(case) / "operator input"
    folder.mkdir()
    return folder / name


def events(engine, name):
    """The run's ``name`` events in order, whole but for their time stamps."""
    rows = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    return [{key: value for key, value in row.items() if key != "time"} for row in rows if row["event"] == name]


def relaxed(run_id, old, new, added):
    return {"event": "resume_config_changed", "run_id": run_id,
            "changes": {"worker_commands": {"old": old, "new": new, "added": added}}}


class CommandCase(RepoCase):
    def grant(self, *commands):
        self.config.data["permissions"]["worker_commands"] = [copy.deepcopy(command) for command in commands]

    def same_stop(self, value, run, row, data, source):
        """``value`` (a raised stop or a CLI notice) and the saved stop are both exactly ``row``, ``data`` and ``source``.

        ``source`` may be a function of the saved run, for the IDs and artifacts the runtime generates.
        """
        expected = {"kind": row[0], "reason": row[1], "data": data, "source": source(run) if callable(source) else source}
        self.assertEqual({key: facts(value)[key] for key in expected}, expected)
        self.assertEqual((run["status"], facts(run["stop"])), ("blocked", expected))

    def stopped(self, engine, call, row, data, source):
        """``call`` raises the stop ``row`` with exit code 3; the raised stop is the saved one, both exactly ``data`` and ``source``."""
        with self.assertRaises(HarnessError) as caught:
            call()
        run = saved(engine)
        self.same_stop(stops.of(caught.exception), run, row, data, source)
        self.assertEqual(facts(stops.of(caught.exception)), facts(run["stop"]))
        self.assertEqual(stops.exit_code(caught.exception), 3)
        return run

    def reported(self, engine, backend, argv, row, data, source):
        """``argv`` through the real CLI stops at ``row`` and exits 3; its stderr [harness-json] notice and the
        saved stop hold exactly ``data`` and ``source``. Returns the saved run and the notice."""
        code, errors = run_cli(self, backend, argv)
        notice = cli_notice(errors)
        self.assertEqual((code, notice["exit_code"]), (3, 3), errors)
        run = saved(engine)
        self.same_stop(notice, run, row, data, source)
        return run, notice

    def words(self, text, **fill):
        """A listed exit ``text`` as CLI words, each ``fill`` placeholder (a whole word, listed once) replaced by its value
        as one argument."""
        words = shlex.split(text)
        self.assertEqual([words.count(name) for name in fill], [1] * len(fill), text)
        return [str(fill[word]) if word in fill else word for word in words]

    def listed(self, engine, choose=0, **fill):
        """The saved stop's listed exit number ``choose``, as CLI words."""
        return self.words(stops.project_ledger(saved(engine))["exits"][choose], **fill)

    def status(self, run_id):
        output, previous = io.StringIO(), signal.getsignal(signal.SIGTERM)
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["--repo", str(self.repo), "status", run_id])
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def walk(self, engine, backend, row, *, expect_status, choose=0, **fill):
        """The saved stop is ``row``; its listed exit ``choose``, filled by ``listed``, exits 0 at ``expect_status``."""
        self.assertEqual((saved(engine)["stop"]["kind"], saved(engine)["stop"]["reason"]), row)
        code, errors = run_cli(self, backend, self.listed(engine, choose, **fill))
        self.assertEqual((code, saved(engine)["status"]), (0, expect_status), errors)
        return saved(engine)


class SubprojectGrantTests(CommandCase):
    """a/app and b/app both hold pubspec.yaml, the root does not; one argv, two commands (rules 6, 17, 18, 20)."""

    def setUp(self):
        super().setUp()
        for name in ("a/app", "b/app"):
            (self.repo / name).mkdir(parents=True)
            (self.repo / name / "pubspec.yaml").write_text("name: app\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-q", "-m", "two apps")

    def test_only_the_exact_directory_grant_lets_the_worker_start(self):
        self.grant(at("a/app"))
        backend = ScriptedBackend()
        engine = self.engine(backend)
        row = ("missing_commands", "all_ready_tasks")
        missing = {"greet": [at("b/app")]}
        data = {"config_key": GRANTS, "missing": missing}
        run = self.stopped(engine, lambda: engine.start("Implement greet", plan(required_commands=[at("b/app")])), row, data, {})
        self.assertEqual(run["command_requirements"], {"missing": missing})
        self.assertEqual(self.status(run["id"])["missing_command_requirements"], missing)
        # One round each through the listed exit: the same argv at the root, then in the directory spelled with another case.
        rounds = ([at("a/app"), at(".")], [at("a/app"), at("."), at("B/app")])
        for grants in rounds:
            self.grant(*grants)
            again, _ = self.reported(engine, backend, self.listed(engine), row, data, {})
            self.assertEqual(again["config_snapshot"]["permissions"]["worker_commands"], grants)
        # Moving a saved grant to another directory is no relaxation: refused before any write.
        before = (engine.directory / "state.json").read_bytes()
        self.grant(at("b/app"), at("."), at("B/app"))
        notice = refused_by_cli(self, backend, ["resume", run["id"]], ("config_mismatch", "immutable_config"), 2)
        self.assertEqual((notice["exit_code"], notice["data"], notice["source"]), (2, {}, {}))
        self.assertEqual(((engine.directory / "state.json").read_bytes(), backend.calls), (before, []))
        # Adding the exact pair, even first in the list, is one; the worker starts.
        final = [at("b/app"), *rounds[1]]
        self.grant(*final)
        done = self.walk(engine, backend, row, expect_status="completed")
        self.assertEqual((done["command_requirements"], workers(backend)), ({"missing": {}}, ["greet"]))
        # Each relaxation is published once, whole and in order: the saved grants, the current ones, the added pairs.
        self.assertEqual(events(engine, "resume_config_changed"),
                         [relaxed(run["id"], [at("a/app")], rounds[0], [at(".")]),
                          relaxed(run["id"], rounds[0], rounds[1], [at("B/app")]),
                          relaxed(run["id"], rounds[1], final, [at("b/app")])])
        # The ledger's report and its event agree: the pair was missing, then nothing was.
        self.assertEqual(events(engine, "command_requirements_checked"),
                         [{"event": "command_requirements_checked", "run_id": run["id"], "missing": value} for value in (missing, {})])

    def test_a_git_query_form_is_covered_only_at_the_worktree_root(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        head = ("git", "rev-parse", "--verify", "HEAD")
        task = plan(required_commands=[at(".", QUERY), at("a/app", QUERY)], self_checks=[at(".", head), at("a/app", head)])
        self.stopped(engine, lambda: engine.start("Implement greet", task), ("missing_commands", "all_ready_tasks"),
                     {"config_key": GRANTS, "missing": {"greet": [at("a/app", QUERY), at("a/app", head)]}}, {})
        self.grant(at("a/app", QUERY), at("a/app", head))
        self.walk(engine, backend, ("missing_commands", "all_ready_tasks"), expect_status="completed")
        self.assertEqual(workers(backend), ["greet"])


class ExactDirectoryStringTests(CommandCase):
    def test_another_case_unicode_form_or_level_of_a_granted_directory_is_missing_both_ways(self):
        self.grant(at("app"), at("Lib"), at(CAFE), at(MUNICH_NFD))
        # Upper over lower, lower over upper, decomposed over precomposed, precomposed over decomposed, the root over a
        # subdirectory: each is missing; the identical strings are met.
        other = [at("App"), at("lib"), at(CAFE_NFD), at(MUNICH), at(".")]
        engine = self.engine(ScriptedBackend())
        required = [*other, at("app"), at(CAFE)]
        self.stopped(engine, lambda: engine.start("Implement greet", plan(required_commands=required)),
                     ("missing_commands", "all_ready_tasks"), {"config_key": GRANTS, "missing": {"greet": other}}, {})
        # So the plan holds both forms of one name as two commands, with two IDs.
        self.assertEqual(len({command_id(command) for command in required}), len(required))


class ModelPlanCommandTests(CommandCase):
    """A commander's command whose directory breaks rule 3 is invalid output, redone once (rule 4)."""

    def redone(self, first, corrected):
        def declare(backend, kwargs, packet, result):
            if trigger_kind(kwargs, packet) == "plan":
                commands = corrected if "previous_invalid" in packet else first
                result["new_tasks"][0]["required_commands"] = copy.deepcopy(commands)
        run = self.engine(ScriptedBackend(declare)).start("Implement greet")
        decides = [entry for entry in run["dispatches"] if entry["role"] == "commander"]
        self.assertEqual([entry["status"] for entry in decides[:2]], ["invalid", "completed"])
        self.assertEqual(decides[1]["retry_of"], decides[0]["id"])
        self.assertTrue(Path(decides[0]["invalid_output"]["artifact"]).is_file())
        self.assertEqual((run["status"], run["original_tasks"][0]["required_commands"]), ("completed", corrected))
        return decides[0]

    def test_a_first_plan_with_an_invalid_cwd_is_invalid_output(self):
        self.grant(at("app"))
        for cwd in ("\ud800", "  ", "../app", "app/.git"):
            with self.subTest(cwd=repr(cwd)):
                self.assertIn("required_commands[0].cwd", self.redone([at(cwd)], [at("app")])["error"])

    def test_one_pair_twice_is_invalid_but_one_argv_in_two_directories_is_two_commands(self):
        self.grant(at("a/app"), at("b/app"))
        self.assertIn("required_commands", self.redone([at("a/app"), at("a/app")], [at("a/app"), at("b/app")])["error"])


class RevisionCommandTests(unittest.TestCase):
    def test_a_revision_appends_each_new_pair_once_in_order(self):
        task = plan(required_commands=[at("a/app")])["tasks"][0]
        verbose = at("a/app", FLUTTER + ("-v",))
        revision = {"new_tasks": [], "updates": [update(task, commands=[at("b/app"), at("a/app"), at("b/app"), verbose])]}
        proposed = apply_revision([task], revision, set(), scope=["**"])
        self.assertEqual(proposed[0]["required_commands"], [at("a/app"), at("b/app"), verbose])
        self.assertEqual(task["required_commands"], [at("a/app")])


class ContinuationCommandTests(CommandCase):
    MARK = "# partial work the continuation keeps\n"

    def test_a_continuation_names_its_directories_and_resumes_the_same_candidate(self):
        request = [at("app"), at(".", QUERY), at("App", QUERY), at(CAFE), at(MUNICH_NFD)]
        seen = []  # hello.py as each worker found it

        def before(backend, kwargs, packet):
            if kwargs["role"] == "worker":
                path = kwargs["cwd"] / "hello.py"
                seen.append(path.read_text(encoding="utf-8") if path.exists() else "")

        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and len(seen) == 1:
                with (kwargs["cwd"] / "hello.py").open("a", encoding="utf-8") as stream:
                    stream.write(self.MARK)
                result.update(status="blocked", blocker_kind="continuation", blockers=["Run the app's tests"],
                              continuation_request={"kind": "commands", "commands": copy.deepcopy(request),
                                                    "minimum_timeout_seconds": 0, "reason": "Run the app's tests"})
        backend = ScriptedBackend(change)
        backend.before_result = before
        engine = self.engine(backend)
        row = ("continuation", "commands_required")
        missing = [at("app"), at("App", QUERY), at(CAFE), at(MUNICH_NFD)]  # the root git_query form needs no grant

        def source(run):  # the pending request, and the worker dispatch that returned it, which the request's record names
            record = run["continuation_history"][-1]
            worker = next(entry["id"] for entry in run["dispatches"] if entry["role"] == "worker")
            self.assertEqual((record["task_id"], record["source"]["dispatch_id"]), ("greet", worker))
            return {"continuation_id": record["id"], "task_id": "greet", "dispatch_id": worker}
        run = self.stopped(engine, lambda: engine.start("Implement greet", plan()), row, {"missing_commands": missing}, source)
        summary = self.status(run["id"])
        self.assertEqual((summary["continuation"]["state"], summary["continuation"]["request"]["commands"]),
                         ("awaiting_requirements", request))
        # Another spelling of each missing pair grants nothing: the root, another case either way, another Unicode form.
        others = [at("."), at("App"), at("app", QUERY), at(CAFE_NFD), at(MUNICH)]
        self.grant(*others)
        self.reported(engine, backend, self.listed(engine), row, {"missing_commands": missing}, source)
        self.grant(*others, *missing)
        done = self.walk(engine, backend, row, expect_status="completed")
        self.assertEqual((workers(backend), done["pending_continuation"]), (["greet", "greet"], None))
        self.assertIn(self.MARK, seen[1])  # the second worker started from the first one's candidate
        # A first grant and one beside saved grants are both published whole, in order.
        self.assertEqual(events(engine, "resume_config_changed"),
                         [relaxed(run["id"], [], others, others), relaxed(run["id"], others, others + missing, missing)])


class EnvironmentCommandCheckTests(CommandCase):
    def blocked(self):
        def block_first(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and len(workers(backend)) == 1:
                result.update(status="blocked", blocker_kind="environment", blockers=["The app toolchain is missing"])

        def source(run):  # the blocked worker's dispatch and its returned result
            entry = next(entry for entry in run["dispatches"] if entry["role"] == "worker")
            artifact = entry["worker_receipt"]["artifact"]
            return {"task_id": "greet", "dispatch_id": entry["id"], "artifact": artifact, "artifact_digest": digest(artifact)}
        backend = ScriptedBackend(block_first)
        engine = self.engine(backend)
        run = self.stopped(engine, lambda: engine.start("Implement greet", plan()), ("environment", "worker_blocked"),
                           {"invalid_output": False}, source)
        return engine, backend, run

    def update_file(self, run, *checks):
        path = input_file(self, "update.json")
        source = next(entry["id"] for entry in run["dispatches"] if entry["role"] == "worker")
        write_json(path, {"schema_version": 1, "task_id": "greet", "blocker_dispatch_id": source,
                          "summary": "Installed the app toolchain", "checks": list(checks)})
        return path

    def test_the_same_argv_in_another_directory_fails_the_check(self):
        self.grant(at("app"), at("Lib"), at(CAFE), at(MUNICH_NFD))
        engine, backend, run = self.blocked()
        # The root, another case either way and the other Unicode form either way of a granted pair each fail.
        checks = [{"kind": "worker_command", "command": command}
                  for command in (at("."), at("app", QUERY), at("App"), at("lib"), at(CAFE_NFD), at(MUNICH))]
        path = self.update_file(run, *checks)
        failed, _ = self.reported(engine, backend, ["resume", run["id"], "--environment-update", str(path)],
                                  ("environment", "checks_failed"), {},
                                  lambda run: {"task_id": "greet", "artifact": run["environment_checks"][-1]["artifact"]})
        report = read_json(Path(failed["environment_checks"][-1]["artifact"]))
        self.assertEqual((report["passed"], report["results"]), (False, [{"check": check, "passed": False} for check in checks]))
        self.assertIs(failed["environment_checks"][-1]["passed"], False)
        self.assertEqual(workers(backend), ["greet"])

    def test_an_argv_check_or_a_blank_cwd_is_an_input_refusal_and_the_granted_pair_passes(self):
        self.grant(at("app"))
        engine, backend, run = self.blocked()
        before = (engine.directory / "state.json").read_bytes()
        # Each refusal names the field it rejects (the old argv form is outside the accepted fields altogether).
        for check, field in (({"kind": "worker_command", "argv": list(FLUTTER)}, None),
                             ({"kind": "worker_command", "command": at(" ")}, "checks.command.cwd"),
                             ({"kind": "worker_command", "command": at("app", ())}, "checks.command.argv"),
                             ({"kind": "worker_command", "command": list(FLUTTER)}, "checks.command")):
            with self.subTest(check=check):
                path = self.update_file(run, check)
                code, errors = run_cli(self, backend, ["resume", run["id"], "--environment-update", str(path)])
                notice = cli_notice(errors)
                self.assertEqual((code, notice["exit_code"], notice["kind"], notice["reason"], notice["data"], notice["source"]),
                                 (2, 2, "invalid_input", "input_schema", {"option": "--environment-update"}, {}), errors)
                if field is not None:
                    self.assertEqual(re.findall(r"\bchecks\.command(?:\.cwd|\.argv)?\b(?!\.)", errors), [field], errors)
                self.assertEqual((engine.directory / "state.json").read_bytes(), before)
        self.assertEqual(workers(backend), ["greet"])
        check = {"kind": "worker_command", "command": at("app")}
        done = self.walk(engine, backend, ("environment", "worker_blocked"), choose=1, FILE=self.update_file(run, check),
                         expect_status="completed")
        report = read_json(Path(done["environment_checks"][-1]["artifact"]))
        self.assertEqual((report["passed"], report["results"]), (True, [{"check": check, "passed": True}]))
        self.assertEqual((done["environment_checks"][-1]["passed"], workers(backend)), (True, ["greet", "greet"]))
        # The refused files left no record: the one update is the accepted file's.
        self.assertEqual([read_json(Path(row["artifact"]))["checks"] for row in done["environment_updates"]], [[check]])


class HeldProposalCommandTests(CommandCase):
    """Both held stops carry every registered field (rule 18). Widened at once, the held proposal itself lands and its
    command gates; otherwise the CLI reports the hold again unchanged, under the request ID that rejects the proposal."""
    REASON = "Keep the declared boundary"

    def held(self, run):
        """A held proposal's source: its request, and the commander result it holds, by dispatch and digest.

        The request ID is only shaped here; ``rejected`` proves it is the proposal's own."""
        receipt = run["commander_receipts"]["commander:decide:run"]
        self.assertRegex(run["stop"]["source"]["request_id"], "^proposal_[0-9a-f]{64}$")
        return {"request_id": run["stop"]["source"]["request_id"], "dispatch_id": receipt["dispatch_id"],
                "artifact": receipt["artifact"], "artifact_digest": digest(receipt["artifact"])}

    def rejected(self, engine, backend, notice, row, data):
        """The request ID of the CLI's held ``notice`` is the one status shows and, through the notice's own exit, rejects
        the proposal; an ID one digit off is stale and changes nothing. Told of the rejection, the scripted commander
        proposes the same again: a new hold ``row`` with ``data``, from another dispatch under another ID."""
        run, request_id = saved(engine), notice["source"]["request_id"]
        self.assertEqual(self.status(run["id"])["scope"]["held"]["request_id"], request_id)

        def reject(value):  # the listed "resume RUN --reject-request FILE"
            path = input_file(self, "reject.json")
            write_json(path, {"request_id": value, "reason": self.REASON})
            return self.words(notice["exits"][1], FILE=path)
        before, calls = (engine.directory / "state.json").read_bytes(), len(backend.calls)
        other = request_id[:-1] + ("1" if request_id.endswith("0") else "0")
        refusal = refused_by_cli(self, backend, reject(other), ("invalid_input", "stale_request"), 2)
        self.assertEqual((refusal["exit_code"], refusal["source"]), (2, {"request_id": other}))
        self.assertEqual(((engine.directory / "state.json").read_bytes(), len(backend.calls)), (before, calls))
        code, errors = run_cli(self, backend, reject(request_id))
        after, again = saved(engine), cli_notice(errors)
        self.assertEqual((code, again["exit_code"], again["kind"]), (3, 3, "scope_extension"), errors)
        # Accepted: the proposal's dispatch records the rejection, the next commander call is told of it, nothing landed.
        dispatch = notice["source"]["dispatch_id"]
        entry = next(entry for entry in after["dispatches"] if entry["id"] == dispatch)
        record = read_json(Path(entry["proposal_rejection"]["artifact"]))
        self.assertEqual((record["request_id"], record["kind"], record["reason"]), (request_id, row[1], self.REASON))
        told = [call for call in backend.calls if call["role"] == "commander"][-1]["packet"]["rejected_revision"]
        self.assertEqual((told["request_id"], told["dispatch_id"], told["reason"]), (request_id, dispatch, self.REASON))
        self.assertEqual(len(after["plan_history"]), len(run["plan_history"]))
        self.same_stop(again, after, row, data, self.held)
        self.assertNotEqual(again["source"]["dispatch_id"], dispatch)
        self.assertNotEqual(again["source"]["request_id"], request_id)

    def landed(self, engine, backend, run, row, data, source):
        """The held ``run``'s listed widening exit lands the held proposal itself, with no model call, as the next plan
        version, then stops at the command gate ``row``; returns the saved run."""
        calls = len(backend.calls)
        after, _ = self.reported(engine, backend, self.listed(engine), row, data, source)
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(sources(after), [*sources(run), run["stop"]["source"]["dispatch_id"]])
        return after

    def held_first_plan(self):
        """The commander's first plan needs docs/*.md and b/app's command; started inside hello.py, it is held."""
        def declare(backend, kwargs, packet, result):
            if trigger_kind(kwargs, packet) == "plan":
                result["new_tasks"][0].update(allowed_paths=["hello.py", "docs/*.md"],
                                              required_commands=[at("b/app"), at(".", QUERY)])
        backend = ScriptedBackend(declare)
        engine = self.engine(backend)
        row = ("scope_extension", "initial_plan")
        data = {"suggested_scope": ["docs/**"], "gaps": {"greet": ["docs/*.md"]}, "missing_commands": {"greet": [at("b/app")]},
                "scope": ["hello.py"], "widened": ["docs/**"], "config_key": GRANTS}
        run = self.stopped(engine, lambda: engine.start("Implement greet", scope=["hello.py"]), row, data, self.held)
        return engine, backend, run, row, data

    def test_a_held_first_plan_widened_at_once_lands_and_its_command_gates_every_ready_task(self):
        engine, backend, run, _, _ = self.held_first_plan()
        gate = ("missing_commands", "all_ready_tasks")
        landed = self.landed(engine, backend, run, gate, {"config_key": GRANTS, "missing": {"greet": [at("b/app")]}}, {})
        self.assertEqual((landed["scope"], landed["tasks"][0]["allowed_paths"], landed["tasks"][0]["required_commands"]),
                         (["hello.py", "docs/**"], ["hello.py", "docs/*.md"], [at("b/app"), at(".", QUERY)]))
        self.grant(at("b/app"))
        self.walk(engine, backend, gate, expect_status="completed")
        self.assertEqual(workers(backend), ["greet"])

    def test_a_held_first_plan_names_its_missing_commands_with_their_directories(self):
        engine, backend, run, row, data = self.held_first_plan()
        _, notice = self.reported(engine, backend, ["resume", run["id"]], row, data, self.held)
        self.rejected(engine, backend, notice, row, data)
        # Widening the boundary lands the new plan; its command then gates every ready task.
        self.reported(engine, backend, self.listed(engine), ("missing_commands", "all_ready_tasks"),
                      {"config_key": GRANTS, "missing": {"greet": [at("b/app")]}}, {})

    def test_a_held_revision_widened_at_once_lands_and_its_command_gates_the_selected_task(self):
        backend = blocked_then(widen("support.txt", commands=[at("app"), at(".", QUERY)]))
        engine = self.engine(backend)
        # A literal path is suggested as itself: nothing is widened beyond what the revision asks.
        data = {"suggested_scope": ["support.txt"], "gaps": {"greet": ["support.txt"]}, "missing_commands": {"greet": [at("app")]},
                "scope": ["hello.py"], "widened": [], "config_key": GRANTS, "selected_task": "greet"}
        run = self.stopped(engine, lambda: engine.start("Implement greet", plan(), scope=["hello.py"]),
                           ("scope_extension", "revision"), data, self.held)
        gate = ("missing_commands", "selected_task")
        landed = self.landed(engine, backend, run, gate, {"config_key": GRANTS, "missing": {"greet": [at("app")]}},
                             {"task_id": "greet"})
        self.assertEqual((landed["scope"], landed["tasks"][0]["allowed_paths"], landed["tasks"][0]["required_commands"]),
                         (["hello.py", "support.txt"], ["hello.py", "support.txt"], [at("app"), at(".", QUERY)]))
        self.grant(at("app"))
        self.walk(engine, backend, gate, expect_status="completed")
        self.assertEqual(workers(backend), ["greet", "greet"])
        self.assertEqual(events(engine, "resume_config_changed"), [relaxed(run["id"], [], [at("app")], [at("app")])])

    def test_a_held_revision_names_its_missing_commands_with_their_directories(self):
        backend = blocked_then(widen("docs/*.md", commands=[at("app"), at(".", QUERY)]))
        engine = self.engine(backend)
        row = ("scope_extension", "revision")
        data = {"suggested_scope": ["docs/**"], "gaps": {"greet": ["docs/*.md"]}, "missing_commands": {"greet": [at("app")]},
                "scope": ["hello.py"], "widened": ["docs/**"], "config_key": GRANTS, "selected_task": "greet"}
        run = self.stopped(engine, lambda: engine.start("Implement greet", plan(), scope=["hello.py"]), row, data, self.held)
        _, notice = self.reported(engine, backend, ["resume", run["id"]], row, data, self.held)
        self.rejected(engine, backend, notice, row, data)
        # Widening the boundary lands the new revision; its command then gates the selected task.
        self.reported(engine, backend, self.listed(engine), ("missing_commands", "selected_task"),
                      {"config_key": GRANTS, "missing": {"greet": [at("app")]}}, {"task_id": "greet"})
        self.grant(at("app"))
        self.walk(engine, backend, ("missing_commands", "selected_task"), expect_status="completed")
        self.assertEqual(events(engine, "resume_config_changed"), [relaxed(run["id"], [], [at("app")], [at("app")])])


class ImmutableCommandListTests(CommandCase):
    def test_another_argv_in_the_same_directory_is_refused_for_verification_and_setup_commands(self):
        verification = self.config.data["verification"]
        verification.update(commands=[*verification["commands"], at("app")], fresh_checkout=True,
                            setup_commands=[at("app", ("flutter", "pub", "get"))])
        backend = ScriptedBackend()
        engine = self.engine(backend)
        row, data = ("missing_commands", "all_ready_tasks"), {"config_key": GRANTS, "missing": {"greet": [at("b/app")]}}
        run = self.stopped(engine, lambda: engine.start("Implement greet", plan(required_commands=[at("b/app")])), row, data, {})
        before = (engine.directory / "state.json").read_bytes()
        for key, index in (("commands", 1), ("setup_commands", 0)):
            kept = copy.deepcopy(verification[key])
            verification[key][index]["argv"].append("-v")
            with self.subTest(key=key):
                notice = refused_by_cli(self, backend, ["resume", run["id"]], ("config_mismatch", "immutable_config"), 2)
                self.assertEqual((notice["exit_code"], notice["data"], notice["source"]), (2, {}, {}))
                self.assertEqual(((engine.directory / "state.json").read_bytes(), backend.calls), (before, []))
            verification[key] = kept
        # Restored, the same resume passes the comparison and reaches the plan's own stop again.
        self.reported(engine, backend, ["resume", run["id"]], row, data, {})


class CommandStopShapeTests(unittest.TestCase):
    """Rule 18: the command fields of these rows hold {cwd, argv} objects, never a bare argv."""
    ROWS = ((("missing_commands", "selected_task"), "missing", True),
            (("missing_commands", "all_ready_tasks"), "missing", True),
            (("continuation", "commands_required"), "missing_commands", False),
            (("scope_extension", "revision"), "missing_commands", True),
            (("scope_extension", "initial_plan"), "missing_commands", True))

    def test_command_objects_pass_and_a_bare_argv_or_another_shape_does_not(self):
        commands = [at("a/app"), at("b/app"), at(".", QUERY)]
        bad = ([list(FLUTTER)], [{**at("app"), "env": {}}], [{"argv": list(FLUTTER)}], [{"cwd": 1, "argv": list(FLUTTER)}])
        for row, field, mapped in self.ROWS:
            shape = (lambda items: {"t1": items}) if mapped else (lambda items: items)
            with self.subTest(row=row):
                made = stops.make(*row, data={field: shape(commands)})
                stops.validate(made)
                self.assertEqual(made["data"], {field: shape(commands)})
                for items in bad:
                    with self.assertRaisesRegex(HarnessError, f"data\\.{field}$"):
                        stops.validate({**made, "data": {field: shape(items)}})
                    remade = stops.make(*row, data={field: shape(items)})
                    stops.validate(remade)
                    # A map is dropped whole; a list keeps its passing head, here empty, and counts the rest.
                    self.assertEqual(remade["data"], {"dropped_fields": [field]} if mapped
                                     else {field: [], f"{field}_omitted": 1})

    def test_an_over_long_command_map_keeps_its_head_with_directories_and_counts_the_rest(self):
        offered = [at(f"apps/{index}", ("cmd", "x" * 300)) for index in range(40)]
        made = stops.make("missing_commands", "all_ready_tasks", data={"missing": {"t1": offered}})
        stops.validate(made)
        kept = made["data"]["missing"]["t1"]
        self.assertTrue(0 < len(kept) < 40)
        self.assertEqual((kept, made["data"]["missing_omitted"]), (offered[:len(kept)], 40 - len(kept)))
        self.assertEqual(stops.project(made, "run_x")["exits"], ["resume run_x"])


if __name__ == "__main__":
    unittest.main()
