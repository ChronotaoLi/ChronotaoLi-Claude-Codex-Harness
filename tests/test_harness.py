import git_isolation  # first: no Git process of a test run may read the developer's Git configuration
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

import stop_tripwire  # noqa: F401  importing it installs the stop tripwire in this test process
from stop_tripwire import injected  # one exemption rule for G7 and the tripwire
from harness import git
from harness.adapters import CLIBackend, DemoBackend
from harness.config import Config
from harness.context import compose
from harness.contracts import PLAN, WORK, envelope, validate, validate_plan
from harness.runtime import Runtime
from harness.mcp_server import handle
from harness.memory import Memory
from harness.shared import mcp_configs, toml_value
from harness.util import HarnessError, read_json, run_process, write_json

ROOT = Path(__file__).resolve().parents[1]


def interrupt_started_agent(kwargs):
    """Interrupt a real owned child after its pgid is durably recorded.

    Recovery fixtures need evidence of an exited process; a backend raising
    before on_start instead represents an unknown startup that must stay blocked.
    """
    def interrupt(pgid):
        kwargs["on_start"](pgid)
        raise KeyboardInterrupt()

    run_process([sys.executable, "-c", "import time; time.sleep(30)"], kwargs["cwd"],
                kwargs["job"] / "interrupted-process", on_start=interrupt)


def add_worker_dispatch(run, directory, dispatch_id, task_id, *, required=(), **fields):
    """Register a hand-built, invoked worker dispatch together with its delivered inputs.

    Writes dispatches/<id>/packet.json and schema.json as publish_inputs would
    and records the referencing receipt intent, so checks that read a sealed
    result's delivered contract treat the fixture like a real dispatch.
    """
    import hashlib
    from harness.contracts import with_keys
    from harness.util import write_json
    job = Path(directory) / "dispatches" / dispatch_id
    rows = [dict(row) for row in required]
    inputs = {}
    for field, value in (("packet", {"required_findings": rows} if rows else {}),
                         ("schema", envelope(with_keys(WORK, "repair_claims", rows), task_id, dispatch_id))):
        path = job / f"{field}.json"
        write_json(path, value)
        inputs[field + "_artifact"] = str(path)
        inputs[field + "_artifact_digest"] = hashlib.sha256(path.read_bytes()).hexdigest()
    entry = {"id": dispatch_id, "role": "worker", "kind": "implement", "task_id": task_id, "invoked": True,
             "worker_receipt": {"dispatch_id": dispatch_id, "artifact": str(job / "returned-result.json"),
                                "seal": str(job / "worker-seal.json"), **inputs, "attempt": 1, "phase": "invoking"},
             **fields}
    run.setdefault("dispatches", []).append(entry)
    return entry


def crash_point(engine):
    """A process death at an injection point the test chooses.

    ``die()`` raises KeyboardInterrupt, and from then on every ledger save of
    this process is dropped: finally blocks, stop() and receipt error handlers
    cannot publish past the point. Events and handoff.json are still written.
    Resume with a new Runtime, which reloads the ledger from disk.
    """
    from unittest.mock import patch
    save = engine.context.save
    state = {"dead": False}

    def wrapped(*args, **kwargs):
        if not state["dead"]:
            save(*args, **kwargs)

    def die():
        state["dead"] = True
        raise KeyboardInterrupt()
    return patch.object(engine.context, "save", side_effect=wrapped), die


def crash_after_save(engine, predicate):
    """A process death right after the first save whose ledger satisfies ``predicate``."""
    from unittest.mock import patch
    save = engine.context.save
    state = {"fired": False}

    def wrapped(*args, **kwargs):
        if state["fired"]:
            return None
        save(*args, **kwargs)
        if predicate(engine.run):
            state["fired"] = True
            raise KeyboardInterrupt()
        return None
    return patch.object(engine.context, "save", side_effect=wrapped), state


def after_save(engine, predicate, action):
    """Run ``action`` once, in process, right after the first save satisfying ``predicate``.

    An outside change while the run continues; it does not simulate a crash.
    """
    from unittest.mock import patch
    save = engine.context.save
    state = {"fired": False}

    def wrapped(*args, **kwargs):
        save(*args, **kwargs)
        if not state["fired"] and predicate(engine.run):
            state["fired"] = True
            action()
    return patch.object(engine.context, "save", side_effect=wrapped), state


def stopped(run, status="blocked"):
    """Put a hand-built ledger into a stop status together with its stop value, as a real stop saves it."""
    from harness import stops
    value = (stops.make("interrupted", "signal") if status == "interrupted" else
             stops.make("unclassified", "untyped_harness_error", data={"error_type": "HarnessError"}))
    run.update(status=status, stop=value)
    return run


def commit_recorder():
    """Record a copy of the ledger after every successful RunStore commit; commits are unchanged."""
    import copy
    from unittest.mock import patch
    from harness.run_store import RunStore
    states = []
    original = RunStore.commit

    def recording(store, *args, **kwargs):
        result = original(store, *args, **kwargs)
        states.append(copy.deepcopy(store.run))
        return result
    return patch.object(RunStore, "commit", recording), states


def review_bound(kind):
    """Predicate: a returned review of this kind is bound by its digest (the receipt save)."""
    def predicate(run):
        return any(key.startswith(f"reviewer:{kind}:") and "artifact_digest" in receipt
                   for key, receipt in run["review_receipts"].items())
    return predicate


def prepared_entry(role=None):
    """Predicate: the last dispatch entry (of ``role``, or any) is saved as prepared, before its local checks."""
    def predicate(run):
        last = run["dispatches"][-1] if run["dispatches"] else {}
        return (role is None or last.get("role") == role) and last.get("process_state") == "prepared"
    return predicate


def decide_bound(run):
    """Predicate: the returned decide original is bound by its digest."""
    receipt = run["commander_receipts"].get("commander:decide:run")
    return bool(receipt and "artifact_digest" in receipt)


class ExitReportingBackend(DemoBackend):
    """DemoBackend whose worker calls report a confirmed normal exit before returning.

    A CLI call saves its exit facts through on_outcome before it returns. The
    plain DemoBackend has no process, so dispatch records its exit only in a
    finally block, which a simulated process death must not run.
    """
    def invoke(self, **kwargs):
        result = super().invoke(**kwargs)
        if kwargs["role"] == "worker" and kwargs.get("on_outcome") is not None:
            from harness.process_runtime import ProcessOutcome
            kwargs["on_outcome"](ProcessOutcome(started=True, startup_state="started", exit_code=0,
                                                exit_confirmed=True))
        return result


# ---- exit-table rows (spec 14.3): a stop, the exit its projection lists, the next state -----
# A row test binds itself to its row by calling one of these with the row's (kind, reason);
# the exit-table guard (G8, test_stop_guards) checks the binding with the AST.
def run_cli(case, backend, argv, *, load=False):
    """The real CLI path with a scripted backend; returns (code, stderr text).

    Only ``cli.Runtime`` (to pass the backend) and ``cli.Config.load`` (the case's
    configuration) are replaced; the parser, ``operate()`` and the operation record are real.
    With ``load`` the CLI loads its configuration files itself: the operator file
    ``--config`` names in ``argv`` and the repository's ``.harness/project.toml``.
    """
    import contextlib
    import io
    import signal
    from types import SimpleNamespace
    from unittest.mock import patch
    from harness import cli
    runtime = lambda config, repo, **kwargs: Runtime(config, repo, backend=backend, **kwargs)
    loader = Config if load else SimpleNamespace(load=lambda path, project=None: case.config)
    errors = io.StringIO()
    previous = signal.getsignal(signal.SIGTERM)
    try:
        with patch.object(cli, "Runtime", runtime), patch.object(cli, "Config", loader), \
                patch("harness.operations.probe_cli", return_value={"path": None, "version": None}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            code = cli.main(["--repo", str(case.repo), *argv])
    finally:
        signal.signal(signal.SIGTERM, previous)
    return code, errors.getvalue()


def write_project_config(repo, commands=None, worker_commands=()):
    """The target repository's project file, which the CLI loads for run, resume and permission-diagnostics."""
    from harness.shared import toml_value
    path = Path(repo) / ".harness" / "project.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    commands = [{"cwd": ".", "argv": [sys.executable, "-m", "unittest", "-v"]}] if commands is None else commands
    path.write_text(f"[verification]\ncommands = {toml_value(commands)}\n\n"
                    f"[permissions]\nworker_commands = {toml_value(list(worker_commands))}\n", encoding="utf-8")
    return path


def write_operator_config(path):
    """The source tree's operator file copied to ``path``, its shared paths made absolute so it works from any folder."""
    text = (ROOT / "harness.toml").read_text(encoding="utf-8")
    for key, value in tomllib.loads(text)["shared"].items():
        text = text.replace(f'{key} = "{value}"', f"{key} = {toml_value(str(ROOT / value))}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def scratch(case):
    """A private folder outside the repository for this test's input files (never the shared temp root)."""
    folder = tempfile.TemporaryDirectory()
    case.addCleanup(folder.cleanup)
    return Path(folder.name)


def cli_notice(errors):
    """The last ``[harness-json]`` stop line of a CLI call's stderr."""
    lines = [line for line in errors.splitlines() if line.startswith("[harness-json] ")]
    body = json.loads(lines[-1].split(" ", 1)[1]) if lines else {}
    return body.get("stop")


def run_exit(case, text, *, backend, directory, expect_status, expect_code, fill):
    import shlex
    # Split first, then fill whole words: a value with spaces stays one argument, and a placeholder
    # inside a word (or one the exit no longer names) fails here instead of running another command.
    fill, words = {name: str(value) for name, value in (fill or {}).items()}, shlex.split(text)
    case.assertEqual(sorted(set(fill) - set(words)), [], f"placeholders not whole words of {text!r}")
    code, errors = run_cli(case, backend, [fill.get(word, word) for word in words])
    case.assertEqual(code, expect_code, text + "\n" + errors)
    after = read_json(Path(directory) / "state.json")
    case.assertEqual(after["status"], expect_status, text + "\n" + errors)
    return after


def walk_exit(case, directory, row, *, backend, expect_status, expect_code=0, choose=0, fill=None):
    """Assert the saved stop is ``row``, execute its listed exit number ``choose``, assert the next state.

    The exit text comes from the ledger's projection, so an exit removed from the
    registry, or changed to one that does not reach the next state, fails here.
    """
    from harness import stops
    saved = read_json(Path(directory) / "state.json")
    case.assertEqual((saved["stop"]["kind"], saved["stop"]["reason"]), tuple(row))
    exits = stops.project_ledger(saved)["exits"]
    case.assertTrue(len(exits) > choose, f"{row} lists no exit number {choose}: {exits}")
    return run_exit(case, exits[choose], backend=backend, directory=directory, expect_status=expect_status,
                    expect_code=expect_code, fill=fill)


def refused_by_cli(case, backend, argv, row, code, *, load=False):
    """An operation refusal through the real CLI: its exit code and stderr projection; returns the projection.

    ``load`` is ``run_cli``'s: without it the case's configuration replaces loading, so a broken file is never read.
    """
    result, errors = run_cli(case, backend, argv, load=load)
    notice = cli_notice(errors)
    case.assertIsNotNone(notice, errors)
    case.assertEqual((result, notice["kind"], notice["reason"]), (code, *row), errors)
    return notice


def walk_notice_exit(case, notice, row, *, backend, directory, expect_status, expect_code=0, choose=0, fill=None):
    """Execute exit number ``choose`` of a refusal's projection (stderr, operation record); assert the next state."""
    case.assertEqual((notice["kind"], notice["reason"]), tuple(row))
    case.assertTrue(len(notice["exits"]) > choose, f"{row} lists no exit number {choose}: {notice['exits']}")
    return run_exit(case, notice["exits"][choose], backend=backend, directory=directory,
                    expect_status=expect_status, expect_code=expect_code, fill=fill)


def no_command_exit(case, value, row):
    """A row without a command exit: the projection names ``row`` and lists nothing to run."""
    from harness import stops
    case.assertEqual((value["kind"], value["reason"]), tuple(row))
    case.assertEqual(value["exits"], [])
    case.assertEqual(value["exit_code"], stops.REGISTRY[tuple(row)]["exit_code"])
    return value


def continues_without_stop(case, directory, *, events_from=0):
    """A non-stop row: the run never saved a stop and no ``run_stopped`` event was written."""
    path = Path(directory)
    saved = read_json(path / "state.json")
    case.assertNotIn("stop", saved)
    lines = (path / "events.jsonl").read_text().splitlines()[events_from:]
    case.assertNotIn("run_stopped", [json.loads(line)["event"] for line in lines])
    return saved


# ---- stop guards (spec 14.4, G7 and G10), installed on every RepoCase ---------------------
def install_stop_guards(case):
    """G7: no runtime stop is unclassified. G10: a published stop's listed exits are not blocked by the load checks.

    What they see, and no more:

    - G7 wraps ``Workflow.stop``, ``Runtime.start``/``resume``/``abandon`` and the module function
      ``workspace_inputs.prepare_run`` (the CLI looks it up at call time; a test that imported the
      function by name calls it unwrapped). An ``unclassified`` stop whose innermost frame is in the
      runtime or the standard library fails the test, unless the class sets ``allow_unclassified``;
      one raised by test code (a frame under ``tests/``, or a patched ``side_effect`` in
      ``unittest.mock``) is exempt (``injected()``, self-tested in test_stop_guards.InjectionRule).
    - G10 checks every stop saved through ``Workflow.stop`` and every plan awaiting approval that
      ``start``/``resume`` return (saved, or reported again): a fresh ``Runtime`` loads the run, and
      listed ``resume``/``abandon`` exits imply the load passes, ``load_integrity`` that it is
      refused. In ``addCleanup`` (after the body and tearDown, before the temporary directory is
      removed), if the run still holds that saved stop and still loads, the first listed ``abandon``
      exit is executed on the original fixture. It does not see operation refusals (G1 and the row
      tests check those), CLI projections (G11), or a stop whose evidence the test changed later.
    """
    import shlex
    from unittest.mock import patch
    from harness import stops
    from harness.workflow import Workflow
    unclassified, mismatches, seen = [], [], {}
    original_stop = Workflow.stop

    def note(error):
        value = stops.of(error)
        if value["kind"] == "unclassified" and not injected(error):
            tb, where = error.__traceback__, None
            while tb is not None:
                where = f"{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}"
                tb = tb.tb_next
            unclassified.append((value["reason"], type(error).__name__, str(error)[:160], where))

    def published(run, directory):
        value = run.get("stop") or {}
        path = directory / "state.json"
        if value.get("kind") and path.exists():
            try:
                Runtime(case.config, case.repo, backend=DemoBackend()).load_run(run["id"])
                loads = True
            except HarnessError:
                loads = False
            exits = stops.project(value, run["id"])["exits"]
            if not loads and any(text.split()[0] in ("resume", "abandon") for text in exits):
                mismatches.append(("exit listed but the load fails", value["kind"], value["reason"]))
            if value["reason"] == "load_integrity" and loads:
                mismatches.append(("load_integrity but the load passes", value["kind"], value["reason"]))
            seen[run["id"]] = (value, path.read_bytes(), directory, case.config, case.repo)

    def stop(workflow, error):
        result = original_stop(workflow, error)
        note(error)
        published(workflow.context.run, workflow.context.directory)
        return result

    def operation(name):
        original = getattr(Runtime, name)

        def wrapped(runtime, *args, **kwargs):
            try:
                result = original(runtime, *args, **kwargs)
            except BaseException as error:
                note(error)
                raise
            if isinstance(result, dict) and result.get("status") == "awaiting_approval":
                published(result, runtime.directory)  # the approval stop: saved, or reported again
            return result
        return wrapped

    def guarded(function):
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except BaseException as error:
                note(error)
                raise
        return wrapped

    def walk_abandon():
        for run_id, (value, saved, directory, config, repo) in seen.items():
            state = directory / "state.json"
            if not state.exists() or state.read_bytes() != saved:
                continue
            listed = [shlex.split(text) for text in stops.project(value, run_id)["exits"] if text.startswith("abandon ")]
            runtime = Runtime(config, repo, backend=DemoBackend())
            if listed:
                try:
                    runtime.load_run(run_id)
                except HarnessError:
                    continue  # the test changed evidence after the stop; settling held when it was saved
            if not listed:
                if value["reason"] == "load_integrity":
                    try:
                        runtime.abandon(run_id)
                        mismatches.append(("abandon of load_integrity succeeded", value["kind"], value["reason"]))
                    except HarnessError:
                        pass
                continue
            words = listed[0]
            confirmed = tuple(words[i + 1] for i, word in enumerate(words) if word == "--confirm-exited")
            try:
                status = runtime.abandon(run_id, confirm_exited=confirmed)["status"]
            except HarnessError as error:
                status = str(error)[:160]
            if status != "abandoned":
                mismatches.append(("listed abandon failed", value["kind"], value["reason"], " ".join(words), status))

    case.stop_guard_findings = {"unclassified": unclassified, "mismatches": mismatches}  # for the guards' self-tests
    from harness import workspace_inputs
    patches = [patch.object(Workflow, "stop", stop),
               patch.object(workspace_inputs, "prepare_run", guarded(workspace_inputs.prepare_run))] + [
        patch.object(Runtime, name, operation(name)) for name in ("start", "resume", "abandon")]
    for item in patches:
        item.start()
    case.addCleanup(lambda: case.assertEqual(
        [] if getattr(case, "allow_unclassified", False) else unclassified, [], "G7: unclassified stops"))
    case.addCleanup(lambda: case.assertEqual(mismatches, [], "G10: exits blocked by the load checks"))
    case.addCleanup(walk_abandon)
    for item in patches:
        case.addCleanup(item.stop)



class FakeClock:
    """Stands in for ``harness.quota_wait``'s ``time`` module, so no test ever sleeps for real.

    ``time()`` and ``monotonic()`` read two counters; ``sleep(seconds)`` records the slice, runs
    ``on_sleep(clock, seconds)`` when set (raise an interrupt, read the disk, try a lock, change
    a file, jump the wall clock) and then advances both clocks. With ``expect_sleep`` false any
    sleep fails the test at once. The wall clock is published as ``QUOTA_STUB_NOW`` (inside a
    stub install's environment) so a stub's ``reset_in`` windows follow this clock.
    """
    RUNAWAY = 20000

    def __init__(self, start=None, *, expect_sleep=True):
        self.mono, self.sleeps, self.on_sleep, self.expect_sleep = 1000.0, [], None, expect_sleep
        self.wall = time.time() if start is None else start

    @property
    def wall(self):
        return self._wall

    @wall.setter
    def wall(self, value):
        self._wall = value
        os.environ["QUOTA_STUB_NOW"] = repr(value)

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        if not self.expect_sleep:
            raise AssertionError(f"a quota wait slept {seconds}s in a test that expects none")
        if not seconds > 0:
            raise AssertionError(f"a quota wait slept {seconds}s")
        self.sleeps.append(seconds)
        if len(self.sleeps) > self.RUNAWAY:
            raise AssertionError("fake clock: runaway quota wait")
        if self.on_sleep is not None:
            self.on_sleep(self, seconds)
        self.mono += seconds
        self.wall += seconds


def install_clock(case, clock):
    """Replace ``harness.quota_wait.time`` with ``clock`` for the rest of the test."""
    from unittest.mock import patch
    patcher = patch("harness.quota_wait.time", clock)
    patcher.start()
    case.addCleanup(patcher.stop)
    return clock


def unregistered_run_entries(state):
    """Entries at the top of each run directory under ``state`` that the observer neither addresses nor excludes.

    A regular file must be one of ``harness.observe.TOP_LEVEL_FILES`` or excluded by name,
    a directory one of ``ROOT_DIRECTORIES``. Below a registered directory the observer's
    general path rules apply (they are rules, not a list that can drift). Links are never
    followed and only directories named like a run ID are read.
    """
    from harness import observe
    runs, found = Path(state) / "runs", []
    if runs.is_symlink() or not runs.is_dir():
        return found
    for run in sorted(runs.iterdir()):
        if run.is_symlink() or not run.is_dir() or not observe.RUN_ID.fullmatch(run.name):
            continue
        for entry in sorted(run.iterdir()):
            if entry.is_symlink():
                continue
            if entry.is_dir() and entry.name not in observe.ROOT_DIRECTORIES:
                found.append(f"{run.name}/{entry.name}/")
            elif entry.is_file() and entry.name not in observe.TOP_LEVEL_FILES and not observe.excluded_name(entry.name):
                found.append(f"{run.name}/{entry.name}")
    return found


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        # Files a case keeps outside its repository: the repository's parent is the temporary root every test process shares.
        self.outside = scratch(self)
        # After every other cleanup (cleanups run last-in first-out): whatever the runtime wrote into a
        # run directory, the observer can address it or excludes it by name.
        self.addCleanup(lambda: self.assertEqual(
            unregistered_run_entries(self.repo / ".harness"), [],
            "run evidence the observer cannot address: register it in harness.observe or exclude it by name"))
        git.git(self.repo, "init", "-b", "main")
        (self.repo / ".gitignore").write_text("__pycache__/\n")
        (self.repo / "test_hello.py").write_text('import unittest\nfrom hello import greet\nclass Tests(unittest.TestCase):\n    def test_greet(self):\n        self.assertEqual(greet("Ada"), "Hello, Ada!")\n')
        git.git(self.repo, "add", ".")
        git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        self.config = Config.load(ROOT / "harness.toml")
        self.config.data["verification"]["commands"] = [{"cwd": ".", "argv": [sys.executable, "-m", "unittest", "-v"]}]
        install_stop_guards(self)
        # Every PATH lookup of claude or codex in a repository test lands on a recording stub;
        # quota probes are expected, any other call to either CLI fails the test.
        import quota_stubs
        self.quota_calls = quota_stubs.install(self)
        self.addCleanup(lambda: self.assertEqual(self.quota_calls.unexpected(), []))
        # A quota wait never sleeps for real: a test that expects one installs its own clock.
        self.quota_clock = install_clock(self, FakeClock(expect_sleep=False))

    def engine(self, backend=None):
        return Runtime(self.config, self.repo, backend=backend or DemoBackend())


class ContractTests(unittest.TestCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def test_rejects_cycle_unknown_dependency_duplicate_id(self):
        for mode in ("cycle", "unknown", "duplicate", "folded"):
            plan = self.plan()
            second = copy.deepcopy(plan["tasks"][0])
            second["id"] = "other"
            plan["tasks"].append(second)
            if mode == "cycle":
                plan["tasks"][0]["depends_on"] = ["other"]
                second["depends_on"] = ["greet"]
            elif mode == "unknown":
                second["depends_on"] = ["missing"]
            elif mode == "duplicate":
                second["id"] = "greet"
            else:  # one evidence directory on a case-insensitive filesystem
                second.update(id="GREET", depends_on=["greet"])
            with self.subTest(mode=mode), self.assertRaises(HarnessError):
                validate_plan(plan)

    def test_rejects_escape_and_empty_acceptance(self):
        for path in ("../secret", "/absolute", "a/../../x"):
            plan = self.plan()
            plan["tasks"][0]["allowed_paths"] = [path]
            with self.assertRaises(HarnessError):
                validate_plan(plan)
        plan = self.plan()
        plan["tasks"][0]["acceptance"] = []
        with self.assertRaises(HarnessError):
            validate_plan(plan)

    def test_stale_dispatch_cannot_complete_new_attempt(self):
        schema = envelope(WORK, "task1", "new")
        value = {"task_id": "task1", "dispatch_id": "old", "status": "completed", "summary": "done", "evidence": [],
                 "blockers": [], "blocker_kind": "none", "proposed_memories": []}
        with self.assertRaises(HarnessError):
            validate(value, schema)

    def test_invalid_structured_output_rejected(self):
        with self.assertRaises(HarnessError):
            validate({"tasks": "all done"}, PLAN)


class ContextMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite3")

    def remember(self, key="model", text="Use gpt-6.2-astra", ttl=None):
        return self.memory.remember(key=key, abstract="模型选择", overview=text, detail="证据 " + text, source="operator", ttl_days=ttl)

    def test_supersession_retains_history_and_retrieves_current(self):
        old = self.remember()
        new = self.remember(text="Use claude-fable-5-2")
        self.assertFalse(self.memory.read(old)["active"])
        self.assertEqual(self.memory.read(new)["supersedes"], old)
        self.assertEqual(self.memory.search("模型选择")[0]["id"], new)
        self.assertEqual(self.memory.search("gpt-6.2-astra"), [])

    def test_expiry_filters_recall_without_deleting_history(self):
        ident = self.remember(ttl=1)
        with self.memory.connect() as db:
            db.execute("UPDATE memories SET expires=? WHERE id=?", (time.time() - 1, ident))
        self.assertEqual(self.memory.search("模型"), [])
        self.assertTrue(self.memory.read(ident)["expired"])

    def test_layers_and_project_isolation(self):
        self.remember()
        self.assertNotIn("overview", self.memory.search("模型", level=0)[0])
        self.assertNotIn("detail", self.memory.search("模型", level=1)[0])
        self.assertIn("detail", self.memory.search("模型", level=2)[0])
        other = Memory(Path(self.temp.name) / "other.sqlite3")
        self.assertEqual(other.search("模型"), [])

    def test_context_preserves_invariants_and_drops_whole_optional_records(self):
        required = {"acceptance": ["Never lose this exact criterion"], "task_id": "abc"}
        memory = [{"id": "m", "overview": "x" * 1000}]
        prompt, report = compose(required, memory, 150)
        self.assertEqual(json.loads(prompt), required)
        self.assertEqual(report["omitted_memories"], 1)
        with self.assertRaises(HarnessError):
            compose(required, [], 5)

    def test_mcp_can_read_but_cannot_write_memory(self):
        ident = self.remember()
        response = handle({"method": "tools/call", "params": {"name": "memory_read", "arguments": {"id": ident}}}, self.memory, ROOT / "shared/skills", None)
        self.assertFalse(response["isError"])
        refused = handle({"method": "tools/call", "params": {"name": "memory_write", "arguments": {}}}, self.memory, ROOT / "shared/skills", None)
        self.assertTrue(refused["isError"])

    def test_mcp_stdio_protocol(self):
        data = '\n'.join(json.dumps(x) for x in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]) + '\n'
        p = subprocess.run([sys.executable, str(ROOT / "harness/mcp_entry.py"), "--state", self.temp.name,
                            "--skills", str(ROOT / "shared/skills")], input=data, text=True, capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        results = [json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual([r["id"] for r in results], [1, 2])
        self.assertEqual(results[0]["result"]["protocolVersion"], "2025-11-25")


class RuntimeTests(RepoCase):
    def test_full_pipeline_isolated_commit_and_verified_memory(self):
        original = git.head(self.repo)
        engine = self.engine()
        result = engine.start("Implement greet")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(git.head(self.repo), original)
        self.assertFalse((self.repo / "hello.py").exists())
        self.assertTrue(git.clean(self.repo))
        self.assertNotEqual(result["accepted"]["greet"]["commit"], original)
        self.assertEqual(len(engine.memory.export()), 1)
        self.assertTrue((engine.directory / "handoff.json").exists())
        ids = [d["id"] for d in result["dispatches"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_failed_review_uses_new_worker_and_reviewer(self):
        class RejectOnce(DemoBackend):
            count = 0
            def invoke(self, **kwargs):
                value = super().invoke(**kwargs)
                if kwargs["role"] == "reviewer" and kwargs["kind"] == "review":
                    self.count += 1
                    if self.count == 1:
                        value.update(verdict="request_changes", findings=[{
                            "id": "edge-case", "severity": "blocking", "category": "evidence",
                            "location": "hello.py", "message": "Please inspect edge case",
                            "evidence": ["The first review requests an additional check."]}])
                return value
        engine = self.engine(RejectOnce())
        result = engine.start("Implement greet")
        self.assertEqual(result["attempts"]["greet"], 2)
        for role in ("worker", "reviewer"):
            ids = [d["id"] for d in result["dispatches"] if d["role"] == role and d["task_id"] == "greet"]
            self.assertEqual(len(ids), 2)
            self.assertNotEqual(ids[0], ids[1])
        self.assertEqual(len(engine.memory.export()), 1)

    def test_bad_tests_never_reach_reviewer_or_memory(self):
        class Broken(DemoBackend):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs["role"] == "worker":
                    (kwargs["cwd"] / "hello.py").write_text('def greet(name):\n    return "wrong"\n')
                return result
        self.config.data["runtime"]["max_attempts"] = 1
        engine = self.engine(Broken())
        with self.assertRaises(HarnessError):
            engine.start("Implement greet")
        self.assertFalse(any(d["role"] == "reviewer" for d in engine.run["dispatches"]))
        self.assertEqual(engine.memory.export(), [])
        self.assertEqual(engine.run["accepted"], {})

    def test_readonly_agent_mutation_stops_run(self):
        class MutatingReviewer(DemoBackend):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs["role"] == "reviewer":
                    (kwargs["cwd"] / "oops.txt").write_text("should not write")
                return result
        engine = self.engine(MutatingReviewer())
        with self.assertRaisesRegex(HarnessError, "修改了代码"):
            engine.start("Implement greet")
        self.assertEqual(engine.memory.export(), [])

    def test_out_of_scope_changes_stop_run(self):
        class Escaping(DemoBackend):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs["role"] == "worker":
                    (kwargs["cwd"] / "unrelated.py").write_text("pass")
                return result
        with self.assertRaisesRegex(HarnessError, "越界"):
            self.engine(Escaping()).start("Implement greet")

    def test_dirty_original_is_not_stashed_or_modified(self):
        (self.repo / "test_hello.py").write_text("user work")
        with self.assertRaises(HarnessError):
            self.engine().start("Implement greet")
        self.assertEqual((self.repo / "test_hello.py").read_text(), "user work")

    def test_resume_completed_run_is_idempotent(self):
        engine = self.engine()
        result = engine.start("Implement greet")
        count = len(result["dispatches"])
        result2 = self.engine().resume(result["id"])
        self.assertEqual(len(result2["dispatches"]), count)

    def test_resume_interrupted_worker_creates_new_dispatch(self):
        class InterruptOnce(DemoBackend):
            def invoke(self, **kwargs):
                if kwargs["role"] == "worker":
                    interrupt_started_agent(kwargs)
                return super().invoke(**kwargs)
        engine = self.engine(InterruptOnce())
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet")
        resumed = self.engine().resume(engine.run["id"])
        self.assertEqual(resumed["status"], "completed")
        workers = [d for d in resumed["dispatches"] if d["role"] == "worker"]
        self.assertEqual(len(workers), 2)
        self.assertNotEqual(workers[0]["id"], workers[1]["id"])

    def test_resume_refuses_live_old_process(self):
        engine = self.engine()
        engine.start("Implement greet")
        stopped(engine.run, "interrupted")
        engine.run["dispatches"][-1]["pgid"] = os.getpgrp()
        engine.run["dispatches"][-1]["exit_confirmed"] = False
        engine.context.save()
        with self.assertRaisesRegex(HarnessError, "进程组仍存在"):
            self.engine().resume(engine.run["id"])

    def test_verification_must_not_rewrite_review_candidate(self):
        self.config.data["verification"]["commands"] = [{"cwd": ".", "argv": [sys.executable, "-c", "from pathlib import Path; Path('hello.py').write_text('bad')"]}]
        engine = self.engine()
        with self.assertRaisesRegex(HarnessError, "验证命令修改"):
            engine.start("Implement greet")
        self.assertEqual(engine.run["accepted"], {})

    def test_dependent_tasks_receive_separate_dispatches_and_bounded_handoffs(self):
        class TwoTasks(DemoBackend):
            def invoke(self, **kwargs):
                if kwargs["role"] == "worker" and kwargs["task"]["id"] == "document":
                    ids = {k: kwargs["schema"]["properties"][k]["enum"][0] for k in ("task_id", "dispatch_id")}
                    packet = json.loads(kwargs["prompt"].split("\n\nREFERENCE MEMORY")[0])["packet"]
                    assert "greet" in packet["dependencies"]
                    (kwargs["cwd"] / "usage.txt").write_text("Use greet(name)")
                    return {**ids, "status": "completed", "summary": "Documented greet", "evidence": ["usage.txt"],
                            "blockers": [], "blocker_kind": "none", "proposed_memories": [],
                            "commit_message": {"subject": "Document greet", "body": ""}}
                return super().invoke(**kwargs)
        plan = read_json(ROOT / "examples/plan.json")
        plan["tasks"].append({"id": "document", "title": "Document greet", "description": "Document greet in usage.txt",
                              "depends_on": ["greet"], "allowed_paths": ["usage.txt"], "acceptance": ["usage.txt explains greet"], "skills": []})
        engine = self.engine(TwoTasks())
        result = engine.start("Implement and document greet", plan)
        workers = [d for d in result["dispatches"] if d["role"] == "worker"]
        self.assertEqual([d["task_id"] for d in workers], ["greet", "document"])
        self.assertEqual(len({d["id"] for d in workers}), 2)
        self.assertEqual(len(engine.memory.export()), 2)

    def test_mutation_after_review_prevents_acceptance(self):
        engine = self.engine()

        def mutate():
            with (engine.worktree / "hello.py").open("a") as stream:
                stream.write("# changed after review\n")
        change, state = after_save(engine, lambda run: "pending_acceptance" in run, mutate)
        with change:
            result = engine.start("Implement greet")
        self.assertTrue(state["fired"])
        self.assertEqual(result["status"], "completed")
        # The approval of the reviewed candidate is void; a new attempt is verified and reviewed.
        self.assertEqual(result["review_history"]["greet"][0].get("status"), "invalidated")
        invoked = [entry for entry in result["dispatches"] if entry.get("invoked")]
        self.assertEqual(sum(entry["role"] == "worker" for entry in invoked), 2)
        self.assertEqual(sum(entry.get("kind") == "review" for entry in invoked), 2)
        events = [json.loads(line)["event"] for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        self.assertIn("pending_acceptance_invalidated", events)
        committed = git.git(engine.worktree, "show", result["accepted"]["greet"]["commit"] + ":hello.py")
        self.assertNotIn("changed after review", committed)

    def test_resume_rejects_changed_configuration(self):
        engine = self.engine()
        engine.start("Implement greet", pause_after_plan=True)
        self.assertEqual(engine.run["status"], "awaiting_approval")
        self.config.data["roles"]["reviewer"]["model"] = "changed-model"
        with self.assertRaisesRegex(HarnessError, "配置已改变"):
            self.engine().resume(engine.run["id"])
        # Row config-changed: refused before any write (2); restoring the value, the listed resume works.
        before = (engine.directory / "state.json").read_bytes()
        notice = refused_by_cli(self, DemoBackend(), ["resume", engine.run["id"]],
                                ("config_mismatch", "immutable_config"), 2)
        self.assertEqual((engine.directory / "state.json").read_bytes(), before)
        self.config.data["roles"]["reviewer"]["model"] = engine.run["config_snapshot"]["roles"]["reviewer"]["model"]
        walk_notice_exit(self, notice, ("config_mismatch", "immutable_config"), backend=DemoBackend(),
                         directory=engine.directory, expect_status="awaiting_approval")

    def test_listed_exit_takes_an_answer_path_with_spaces(self):
        from unittest.mock import patch
        from harness import stops

        class AskOperatorOnce(DemoBackend):
            workers = 0
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs["role"] == "worker":
                    self.workers += 1
                    if self.workers == 1:
                        result.update(status="blocked", blocker_kind="operator", blockers=["Which greeting?"])
                return result
        backend = AskOperatorOnce()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        answer = scratch(self) / "operator answers" / "answer.json"
        write_json(answer, {"schema_version": 1, "request_id": engine.run["decision_requests"][-1]["id"],
                            "task_id": "greet", "decision": "continue", "summary": "Use the documented greeting."})
        # A placeholder inside a word, or one the exit does not name, fails before the CLI runs.
        listed = stops.project_ledger(read_json(engine.directory / "state.json"))["exits"][0]
        with patch.object(sys.modules[__name__], "run_cli", wraps=run_cli) as reached:
            for text, fill in ((listed.replace(" FILE", "=FILE"), {"FILE": answer}),
                               (listed, {"FILE": answer, "ANSWER": answer})):
                with self.subTest(text=text, fill=sorted(fill)), self.assertRaises(self.failureException):
                    run_exit(self, text, backend=backend, directory=engine.directory, expect_status="completed",
                             expect_code=0, fill=fill)
        self.assertEqual(reached.call_count, 0)
        walk_exit(self, engine.directory, ("operator_decision", "answer_required"), backend=backend,
                  fill={"FILE": answer}, expect_status="completed")


class IntegrationTests(RepoCase):
    def test_adapter_commands_never_resume_or_bypass_permissions(self):
        backend = CLIBackend(self.config, self.repo / ".harness")
        for role in ("commander", "worker", "reviewer"):
            for commander in ("astra", "fable"):
                job = self.repo / (role + "-" + commander)
                job.mkdir()
                argv = backend.command(role, self.config.role(role, commander), self.repo, job, WORK, None)
                self.assertFalse(any(x in ("resume", "--resume", "--continue", "--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox") for x in argv))
                if argv[0] == "codex":
                    self.assertIn("read-only", argv)
                    self.assertIn("--ephemeral", argv)
                else:
                    self.assertIn("--no-session-persistence", argv)
                    self.assertIn("--strict-mcp-config", argv)

    def test_generated_mcp_toml_is_parseable_and_shared(self):
        claude, codex = mcp_configs(self.config, self.repo / ".harness", "worker")
        parsed = tomllib.loads("mcp_servers = " + toml_value(codex))
        self.assertEqual(parsed["mcp_servers"]["harness"]["args"], claude["mcpServers"]["harness"]["args"])

    def test_timeout_kills_owned_child(self):
        pgids = []
        with self.assertRaises(HarnessError):
            run_process([sys.executable, "-c", "import time; time.sleep(30)"], self.repo,
                        self.repo / "timeout", timeout=1, on_start=pgids.append)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgids[0], 0)


if __name__ == "__main__":
    unittest.main()
