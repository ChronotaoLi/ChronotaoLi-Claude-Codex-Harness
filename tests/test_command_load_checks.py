"""Spec 2 rule 21: saved commands are checked again at load (21.1) and whenever the published plan is reread (21.2).

Runs stop offline through a scripted backend; the test then rewrites the ledger and the plan files consistently, digests
included, so that only a command's shape is wrong. Each case breaks one place alone: one ledger copy or plan version,
one task, one command column, one way. No model or CLI is called.
"""
import copy
import hashlib
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from harness import stops
from harness.config import Config
from harness.packets import PacketBuilder
from harness.scheduler import Scheduler
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase, crash_after_save, write_project_config
from test_runtime_changes import ScriptedBackend, current_tasks, finding, revised, trigger_kind

ARGV = [sys.executable, "-m", "unittest", "-v"]
UNIT = {"cwd": ".", "argv": ARGV}            # granted: it is the verification command (rule 7)
GRANT = {"cwd": "app", "argv": ["flutter", "test"]}
VALID = [{"cwd": "app", "argv": ARGV}]       # another valid command list: the controls rewrite into it
COLUMNS = ("required_commands", "self_checks")
BAD = {"parent cwd": [{"cwd": "../x", "argv": ARGV}], "argv array": [list(ARGV)], "no cwd": [{"argv": ARGV}],
       "env key": [{"cwd": ".", "argv": ARGV, "env": {}}], "protected cwd": [{"cwd": ".harness/x", "argv": ARGV}],
       "blank segment": [{"cwd": "a/ /b", "argv": ARGV}], "not a list": copy.deepcopy(UNIT)}
# The contract's structure on top; configuration lists may repeat and have no length bound.
TASK_BAD = {**BAD, "duplicate": [UNIT, UNIT], "too many": [{"cwd": f"d{n}", "argv": ARGV} for n in range(33)]}
# The tasks of each published version of the three-version run; tasks mirrors the last, original_tasks the first.
VERSIONS = (("greet",), ("greet", "fix1"), ("greet", "fix1", "fix2"))
# The scheduling steps of a run that completes after two corrective tasks.
STEPS = [("supplied_plan", None), ("worker", "greet"), ("final_review", None), ("decide", "final"),
         ("worker", "fix1"), ("final_review", None), ("decide", "final"), ("worker", "fix2"), ("final_review", None)]


def column(index, name, value):
    """Set one task's one command column in a plan file's content."""
    return lambda content: content["tasks"][index].update({name: copy.deepcopy(value)})


STRUCTURE = {"tasks not a list": lambda content: content.update(tasks={task["id"]: task for task in content["tasks"]}),
             "task not an object": lambda content: content["tasks"].__setitem__(-1, content["tasks"][-1]["id"]),
             "appended task not an object": lambda content: content["tasks"].append(content["tasks"][-1]["id"])}


def plan(*more):
    """The example plan's greet task with commands, and further tasks like it after greet."""
    value = read_json(ROOT / "examples" / "plan.json")
    value["tasks"][0].update(required_commands=[copy.deepcopy(UNIT)], self_checks=[copy.deepcopy(UNIT)])
    value["tasks"] += [{**copy.deepcopy(value["tasks"][0]), "id": ident, "depends_on": ["greet"]} for ident in more]
    return value


def corrective(rejections, worker_hook=None):
    """greet, then one corrective task per whole-run rejection while fewer than two exist (plan revisions 1 and 2).

    Final reviews after ``rejections`` approve; a third rejection reaches the demo commander, which asks the operator.
    """
    def change(backend, kwargs, packet, result):
        finals = sum(call["kind"] == "final_review" for call in backend.calls)
        if kwargs["kind"] == "final_review" and finals <= rejections:
            result.update(verdict="request_changes", findings=[finding()])
        if trigger_kind(kwargs, packet) == "final" and finals <= 2:
            task = copy.deepcopy(current_tasks(packet)[0])
            task.update(id=f"fix{finals}", depends_on=["greet"])
            revised(result, packet, "Address the final finding", new_tasks=[task])
        if kwargs["role"] == "worker" and kwargs["task"]["id"] != "greet":
            with (kwargs["cwd"] / "hello.py").open("a") as file:
                file.write(f"# {kwargs['task']['id']}\n")
            if worker_hook:
                worker_hook(kwargs["task"]["id"])
    return ScriptedBackend(change)


def rewrite(directory, revision, change):
    """Rewrite one published plan file with ``change``; return what points a ledger at the new bytes.

    The returned function sets the version's digest in ``plan_history`` and, for the first and the last version, the
    ledger copy that mirrors it (``original_tasks``, ``tasks``), so that only the command shape is wrong.
    """
    path = directory / "plans" / f"revision-{revision}.json"
    content = read_json(path)
    change(content)
    write_json(path, content)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    def follow(run):
        run["plan_history"][revision]["artifact_digest"] = digest
        if revision == 0:
            run["original_tasks"] = copy.deepcopy(content["tasks"])
        if revision == len(run["plan_history"]) - 1:
            run["tasks"] = copy.deepcopy(content["tasks"])
    return follow


def files(directory):
    """Every file under the run directory with the digest of its bytes."""
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob("*")) if path.is_file()}


def layout(top):
    """Each entry under ``top``, ``top`` included, from its own lstat: links are recorded, never followed.

    An entry has its type, permission bits and flags (copy2 carries flags, and an immutable one stops the next removal);
    a file adds its size and digest, a link its target string. Another type, or an entry that cannot be read, gives None.
    """
    entries, pending = [], [top]
    try:
        while pending:
            path = pending.pop()
            info = os.lstat(path)
            entry = (str(path.relative_to(top)), stat.S_IMODE(info.st_mode), getattr(info, "st_flags", 0))
            if stat.S_ISDIR(info.st_mode):
                entries.append((*entry, "directory"))
                pending += path.iterdir()
            elif stat.S_ISREG(info.st_mode):
                entries.append((*entry, "file", info.st_size, hashlib.sha256(path.read_bytes()).hexdigest()))
            elif stat.S_ISLNK(info.st_mode):
                entries.append((*entry, "link", os.readlink(path)))
            else:
                return None
    except OSError:
        return None
    return tuple(sorted(entries))


def snapshot(section, key):
    def change(directory, run, bad):
        run["config_snapshot"][section][key] = copy.deepcopy(bad)
    return change


def saved_copy(revision, index, name):
    """One task's column in the ledger copy mirroring ``revision``, changed together with that plan file."""
    return lambda directory, run, bad: rewrite(directory, revision, column(index, name, bad))(run)


def every_copy(directory, run, bad):
    for revision in range(3):
        rewrite(directory, revision, column(0, "required_commands", bad))(run)


# Each ledger copy with the plan file it mirrors: tasks is revision 2 (greet, fix1, fix2), original_tasks revision 0.
LOCATIONS = {**{f"config_snapshot.{section}.{key}": snapshot(section, key)
                for section, key in (("verification", "commands"), ("verification", "setup_commands"),
                                     ("permissions", "worker_commands"))},
             **{f"{field} {task} {name}": saved_copy(revision, index, name)
                for field, revision in (("tasks", 2), ("original_tasks", 0))
                for index, task in enumerate(VERSIONS[revision]) for name in COLUMNS},
             "every copy": every_copy}


def pending(run):
    return [task["id"] for task in run["tasks"] if task["id"] not in run["accepted"]]


def settled(run):
    """A registered plan with neither a landed revision nor a task in progress (next_step derives those first)."""
    return bool(run["tasks"]) and not run.get("pending_revision") and not run.get("pending_task")


def reviewed(run, *statuses):
    """Every task accepted, and the whole-run records of this checkpoint (``expected_head``) have ``statuses``."""
    return (settled(run) and not pending(run)
            and [row["status"] for row in run["final_history"] if row["head"] == run["expected_head"]] == list(statuses))


# Where a scheduling case changes the plan: the first next_step call whose ledger holds the facts, not a count of the
# steps before it. From them next_step derives the named step: a pending task is worked; with every task accepted, a
# checkpoint without a whole-run record is reviewed, and one whose review was rejected goes to a decision.
AT = {"worker greet": lambda run, options: settled(run) and pending(run) == ["greet"],
      "first final_review": lambda run, options: reviewed(run) and not run["final_history"],
      "first decide": lambda run, options: reviewed(run, "rejected") and len(run["final_history"]) == 1,
      "worker fix1": lambda run, options: settled(run) and pending(run) == ["fix1"],
      "worker fix2": lambda run, options: settled(run) and pending(run) == ["fix2"],
      "third final_review": lambda run, options: reviewed(run) and len(run["final_history"]) == 2,
      "third decide": lambda run, options: reviewed(run, "rejected") and len(run["final_history"]) == 3}


class CommandLoadCheckTests(RepoCase):
    template = None   # what make_three_versions returned, kept for the class: paths, IDs and digests only

    def setUp(self):
        super().setUp()
        # The target repository's project file holds the verification command and the grant (AGENTS.md).
        self.config = Config.load(ROOT / "harness.toml", write_project_config(self.repo, [UNIT], [GRANT]))

    def three_versions(self):
        """A run stopped at an operator question with three published plan versions.

        The saved ledger leaves the commander's exit unconfirmed and ``self.confirm`` names it: a resume that got past
        its checks would write that confirmation to the ledger and the events. The run is made once per class and its
        repository (worktree, Git store and run directory) copied aside as a template that no test writes to. Each test
        first replaces the run's repository with the template, whatever an earlier test did there, and gets its own
        configuration, backend and engine; a template whose ``layout`` is no longer the one it was made with is made
        again. The ledger holds the repository's absolute path, so the copy stays where the run was made.
        """
        cls = type(self)
        if cls.template is None or layout(cls.template[0] / "template") != cls.template[3]:
            cls.template = self.make_three_versions()
        folder, ident, commander, entries = cls.template
        self.repo = folder / "repo"
        if self.repo.is_dir() and not self.repo.is_symlink():
            shutil.rmtree(self.repo)
        elif os.path.lexists(self.repo):   # a link or other entry left at the path goes itself, never followed
            self.repo.unlink()
        shutil.copytree(folder / "template", self.repo, symlinks=True)
        self.assertEqual(layout(self.repo), entries)
        self.config = Config.load(ROOT / "harness.toml", self.repo / ".harness" / "project.toml")
        backend = corrective(3)
        engine = self.engine(backend)
        engine.run = read_json(self.repo / ".harness" / "runs" / ident / "state.json")
        self.confirm = [commander]
        self.plans = {path: path.read_bytes() for path in sorted((engine.directory / "plans").glob("revision-*.json"))}
        self.assertEqual(len(self.plans), 3)
        return engine, backend, copy.deepcopy(engine.run)

    def make_three_versions(self):
        temp = tempfile.TemporaryDirectory()
        type(self).addClassCleanup(temp.cleanup)
        type(self).addClassCleanup(setattr, type(self), "template", None)
        folder = Path(temp.name)
        repo = folder / "repo"
        shutil.copytree(self.repo, repo, symlinks=True)   # this test's new repository and project file, before any run
        self.repo, self.config = repo, Config.load(ROOT / "harness.toml", repo / ".harness" / "project.toml")
        backend = corrective(3)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        path, events = engine.directory / "state.json", engine.directory / "events.jsonl"
        run = read_json(path)
        value = stops.of(caught.exception)
        self.assertEqual(((value["kind"], value["reason"]), run["status"], (run["stop"]["kind"], run["stop"]["reason"])),
                         (("operator_decision", "answer_required"), "blocked", ("operator_decision", "answer_required")))
        published = [[task["id"] for task in read_json(engine.directory / "plans" / f"revision-{revision}.json")["tasks"]]
                     for revision in range(len(run["plan_history"]))]
        self.assertEqual((published, [t["id"] for t in run["tasks"]], [t["id"] for t in run["original_tasks"]],
                          [(t["required_commands"], t["self_checks"]) for t in run["tasks"]]),
                         ([["greet"], ["greet", "fix1"], ["greet", "fix1", "fix2"]], ["greet", "fix1", "fix2"], ["greet"],
                          [([UNIT], [UNIT])] * 3))
        commander = run["dispatches"][-1]
        self.assertEqual((commander["role"], commander["exit_confirmed"]), ("commander", True))
        commander["exit_confirmed"] = False
        confirm = [commander["id"]]
        write_json(path, run)
        raw, logged, before = path.read_bytes(), events.read_bytes(), files(engine.directory)
        probe = self.engine(backend)
        probe.load_run(run["id"])
        probe.context.confirm_exited(confirm)
        after = files(engine.directory)
        self.assertEqual(sorted(name for name in after if after[name] != before.get(name)), ["events.jsonl", "state.json"])
        path.write_bytes(raw)
        events.write_bytes(logged)
        shutil.copytree(repo, folder / "template", symlinks=True)
        return folder, run["id"], commander["id"], layout(folder / "template")

    def restore_plans(self):
        for path, raw in self.plans.items():
            path.write_bytes(raw)

    def assert_stop(self, error, reason):
        value = stops.of(error)
        self.assertEqual(((value["kind"], value["reason"]), stops.exit_code(error)), (("evidence", reason), 1))

    def refused(self, engine, backend, state, reason, operations=("resume", "abandon"), confirm=()):
        """Each operation is refused with evidence/``reason``, exit 1 and no model call, and writes nothing.

        ``confirm`` is an exit confirmation the operation would write once past its checks.
        """
        write_json(engine.directory / "state.json", state)
        before, calls = files(engine.directory), len(backend.calls)
        for name in operations:
            with self.subTest(operation=name):
                with self.assertRaises(HarnessError) as caught:
                    getattr(self.engine(backend), name)(state["id"], confirm_exited=confirm)
                self.assert_stop(caught.exception, reason)
                # No command_requirements_checked event, nor any other write: the refusal precedes them all.
                self.assertEqual((files(engine.directory), len(backend.calls)), (before, calls))

    def test_saved_commands_in_the_ledger_are_checked_at_load(self):  # 21.1
        engine, backend, saved = self.three_versions()
        for location, change in LOCATIONS.items():
            for shape, bad in (BAD if location.startswith("config_snapshot") else TASK_BAD).items():
                with self.subTest(location=location, shape=shape):
                    self.restore_plans()
                    state = copy.deepcopy(saved)
                    change(engine.directory, state, bad)
                    self.refused(engine, backend, state, "load_integrity", confirm=self.confirm)
        for field, revision in (("tasks", 2), ("original_tasks", 0)):
            for name, change in STRUCTURE.items():
                with self.subTest(location=field, shape=name):
                    self.restore_plans()
                    state = copy.deepcopy(saved)
                    rewrite(engine.directory, revision, change)(state)
                    self.refused(engine, backend, state, "load_integrity", confirm=self.confirm)

    def test_unregistered_supplied_plan_is_checked_at_load(self):  # 21.1: tasks and plan_history still empty
        backend = ScriptedBackend()
        engine = self.engine(backend)
        crash, _ = crash_after_save(engine, lambda run: bool(run["worktree_binding"]) and not run["tasks"])
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan("polish", "wrap"))
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"], scope=["src/**"])   # held before it is published
        saved = read_json(engine.directory / "state.json")
        self.assertEqual((saved["stop"]["kind"], saved["stop"]["reason"], saved["tasks"], saved["plan_history"],
                          [task["id"] for task in saved["original_tasks"]]),
                         ("scope_extension", "supplied_plan", [], [], ["greet", "polish", "wrap"]))
        for index, task in enumerate(("greet", "polish", "wrap")):
            for name in COLUMNS:
                for shape, bad in TASK_BAD.items():
                    with self.subTest(task=task, column=name, shape=shape):
                        state = copy.deepcopy(saved)
                        state["original_tasks"][index][name] = copy.deepcopy(bad)
                        self.refused(engine, backend, state, "load_integrity")
        with self.subTest(shape="element not an object"):
            self.refused(engine, backend, {**copy.deepcopy(saved), "original_tasks": [*saved["original_tasks"], "greet"]},
                         "load_integrity")

    def woken(self, waking, backend, reason):
        """wake_checks refuses with evidence/``reason``, no model call, and writes nothing (no event, no new file)."""
        before, calls = files(waking.context.directory), len(backend.calls)
        with self.assertRaises(HarnessError) as caught:
            waking.services.lifecycle.wake_checks()
        self.assert_stop(caught.exception, reason)
        self.assertEqual((files(waking.context.directory), len(backend.calls)), (before, calls))

    def test_wake_checks_rerun_the_load_and_the_plan_reread(self):  # 21.1 and 21.2 after a quota wait
        engine, backend, saved = self.three_versions()
        path = engine.directory / "state.json"
        original = path.read_bytes()

        def committed(change):
            """A process that loaded the run, changed its ledger with ``change`` and committed it, as before a wait."""
            path.write_bytes(original)
            self.restore_plans()
            waking = self.engine(backend)
            waking.load_run(saved["id"])
            change(waking.run)
            waking.context.save()
            self.assertEqual(read_json(path), waking.run)   # what the wake checks meet is exactly the last commit
            return waking
        for location, change in LOCATIONS.items():
            with self.subTest(location=location):
                self.woken(committed(lambda run: change(engine.directory, run, BAD["parent cwd"])), backend, "load_integrity")
        # The intermediate version has no ledger copy: its file and digest committed, the load passes and only the plan
        # reread sees it, although the ledger in memory is the one on disk.
        middle = {**{f"{VERSIONS[1][index]} {name} {shape}": column(index, name, TASK_BAD[shape])
                     for index, name, shape in ((0, "self_checks", "duplicate"), (0, "required_commands", "no cwd"),
                                                (0, "required_commands", "parent cwd"), (1, "self_checks", "argv array"),
                                                (1, "required_commands", "parent cwd"))},
                  **STRUCTURE}
        for name, change in middle.items():
            with self.subTest(revision=1, change=name):
                self.woken(committed(lambda run: rewrite(engine.directory, 1, change)(run)), backend, "runtime_integrity")
        with self.subTest(revision=1, change="valid"):   # the control: the same commit of a valid command wakes
            waking = committed(lambda run: rewrite(engine.directory, 1, column(1, "required_commands", VALID))(run))
            before, calls = files(engine.directory), len(backend.calls)
            waking.services.lifecycle.wake_checks()
            self.assertEqual((files(engine.directory), len(backend.calls)), (before, calls))

    def test_intermediate_plan_version_is_checked_on_resume_and_abandon_stays_available(self):  # 21.2
        engine, backend, saved = self.three_versions()
        changes = {**{f"{task} {name} {shape}": column(index, name, bad) for index, task in enumerate(VERSIONS[1])
                      for name in COLUMNS for shape, bad in TASK_BAD.items()}, **STRUCTURE}
        for name, change in changes.items():
            with self.subTest(change=name):
                self.restore_plans()
                state = copy.deepcopy(saved)
                rewrite(engine.directory, 1, change)(state)
                self.refused(engine, backend, state, "runtime_integrity", operations=("resume",), confirm=self.confirm)
        self.restore_plans()
        state = copy.deepcopy(saved)
        rewrite(engine.directory, 1, column(1, "required_commands", BAD["parent cwd"]))(state)
        write_json(engine.directory / "state.json", state)
        calls = len(backend.calls)
        self.assertEqual(self.engine(backend).abandon(saved["id"], confirm_exited=self.confirm)["status"], "abandoned")
        self.assertEqual(len(backend.calls), calls)

    def test_first_and_last_plan_versions_are_checked_by_the_plan_reread(self):  # 21.2, the reread function itself
        """The plan reread (``verify_plan_authority``) checks the versions a ledger copy mirrors.

        A valid ledger is loaded; then a version's file, its digest and its ledger copy (``original_tasks``, ``tasks``)
        are changed together in memory, so that only a command's shape is wrong, and the reread is called directly. No
        entry that loads a ledger hands the reread this state: resume and abandon load the consistent copy first and stop
        with load_integrity (``test_saved_commands_in_the_ledger_are_checked_at_load``), and a wake meets the same load
        checks, or ``check_current`` when the ledger on disk changed during the wait. A running process does meet it:
        the scheduling cases change the first and the last version too (and the intermediate one) right before a step,
        where the reread's stop is then settled into load_integrity naming it as superseded
        (``test_every_scheduling_step_rereads_the_plan``). That resume and a wake call the reread is shown with the
        intermediate version, which no ledger copy mirrors.
        """
        engine, backend, saved = self.three_versions()
        for revision in (0, 2):
            ids = VERSIONS[revision]
            changes = {"valid": column(len(ids) - 1, "self_checks", [GRANT]),   # the control: the same pointing passes
                       **{f"{task} {name} {shape}": column(index, name, bad) for index, task in enumerate(ids)
                          for name in COLUMNS for shape, bad in TASK_BAD.items()}, **STRUCTURE}
            for name, change in changes.items():
                with self.subTest(revision=revision, change=name):
                    self.restore_plans()
                    reader = self.engine(backend)
                    reader.load_run(saved["id"])
                    rewrite(engine.directory, revision, change)(reader.run)
                    before, calls = files(engine.directory), len(backend.calls)
                    if name == "valid":
                        reader.services.planning.verify_plan_authority()
                    else:
                        with self.assertRaises(HarnessError) as caught:
                            reader.services.planning.verify_plan_authority()
                        self.assert_stop(caught.exception, "runtime_integrity")
                    self.assertEqual((files(engine.directory), len(backend.calls)), (before, calls))

    def stepped(self, backend, at, revision, change, start):
        """Run ``start`` with ``revision`` changed (and followed) at the first next_step call whose ledger and options
        hold the facts ``at``, before that step is derived.

        Returns what ``start`` returned or raised; what held at the change (the ledger, the number of model calls, the
        options); and each derived step (kind, task or decision kind) with the ``AT`` facts that first held before it.
        """
        next_step, seen, steps, held = Scheduler.next_step, {}, [], set()

        def changed_then_derived(scheduler, **options):
            run = scheduler.context.run
            labels = [label for label, facts in AT.items() if label not in held and facts(run, options)]
            held.update(labels)
            if not seen and at(run, options):
                seen.update(run=copy.deepcopy(run), calls=len(backend.calls), options=options)
                rewrite(scheduler.context.directory, revision, change)(run)
            result = next_step(scheduler, **options)
            steps.append((result.kind, result.data[0]["id"] if result.kind == "worker"
                          else result.data.get("kind") if result.kind == "decide" else None, labels))
            return result
        with patch.object(Scheduler, "next_step", changed_then_derived):
            try:
                outcome = start()
            except HarnessError as error:
                outcome = error
        return outcome, seen, steps

    def stopped_before(self, engine, backend, error, seen, revision):
        """The run stopped at the reread: no model call after the change, the stop saved as the error carries it, and
        the acceptance records saved are the ones the change met.

        The change is made in the running process right before the step: a step's own reread is seen only through the
        scheduling loop. A change to the middle version stops as evidence/runtime_integrity. The first and the last
        version have a ledger copy, changed with them after the load: the stop the reread raised is settled by the load
        checks into evidence/load_integrity, which names the reread's stop as superseded.
        """
        self.assertIsInstance(error, HarnessError)
        middle = 0 < revision < len(seen["run"]["plan_history"]) - 1
        self.assert_stop(error, "runtime_integrity" if middle else "load_integrity")
        run = read_json(engine.directory / "state.json")
        self.assertEqual((run["status"], run["stop"]["kind"], run["stop"]["reason"], len(backend.calls), run["accepted"]),
                         ("blocked", "evidence", "runtime_integrity" if middle else "load_integrity", seen["calls"],
                          seen["run"]["accepted"]))
        if not middle:
            self.assertEqual(run["stop"]["data"]["superseded"], {"kind": "evidence", "reason": "runtime_integrity"})
        self.assertNotIn('"run_completed"', (engine.directory / "events.jsonl").read_text())
        return run

    def test_every_scheduling_step_rereads_the_plan(self):  # 21.2 at next_step
        # The control: rewriting revision 1 into another valid command where the third whole-run review is due stops
        # nothing (the run goes on to that review and the operator question after a third rejection), and in that run
        # the facts of each case first hold right before the step named for them.
        backend = corrective(3)
        engine = self.engine(backend)
        error, seen, steps = self.stepped(backend, AT["third final_review"], 1, column(1, "required_commands", VALID),
                                          lambda: engine.start("Implement greet", plan()))
        value = stops.of(error)
        firsts = {1: "worker greet", 2: "first final_review", 3: "first decide", 4: "worker fix1", 7: "worker fix2",
                  8: "third final_review", 9: "third decide"}
        self.assertEqual(((value["kind"], value["reason"]), len(seen["run"]["plan_history"]), steps),
                         (("operator_decision", "answer_required"), 3,
                          [(*step, [firsts[index]] if index in firsts else [])
                           for index, step in enumerate([*STEPS, ("decide", "final")])]))
        self.assertEqual([(call["role"], call["kind"]) for call in backend.calls[seen["calls"]:]],
                         [("reviewer", "final_review"), ("commander", "decide")])
        # (step, revision, task index, column, shape, rejections); the third decision needs a third rejection. Rows of
        # one step change the plan under the same facts, each in its own run: before fix1 the last version two ways;
        # before the third whole-run review the middle version (it stops as itself) and the first (settled, naming the
        # reread's stop as superseded).
        cases = [("worker greet", 0, 0, "self_checks", "duplicate", 2),
                 ("first final_review", 0, 0, "required_commands", "too many", 2),
                 ("first decide", 0, 0, "self_checks", "blank segment", 2),
                 ("worker fix1", 1, 1, "required_commands", "argv array", 2),
                 ("worker fix1", 1, 1, "required_commands", "parent cwd", 2),
                 ("worker fix2", 1, 1, "self_checks", "no cwd", 2),
                 ("third final_review", 1, 1, "required_commands", "parent cwd", 2),
                 ("third final_review", 0, 0, "required_commands", "argv array", 2),
                 ("third decide", 2, 2, "self_checks", "not a list", 3)]
        held = {"worker greet": (1, [], 0), "first final_review": (1, ["greet"], 0), "first decide": (1, ["greet"], 1),
                "worker fix1": (2, ["greet"], 1), "worker fix2": (3, ["fix1", "greet"], 2),
                "third final_review": (3, ["fix1", "fix2", "greet"], 2), "third decide": (3, ["fix1", "fix2", "greet"], 3)}
        for label, revision, index, name, shape, rejections in cases:
            with self.subTest(step=label, revision=revision, task=VERSIONS[revision][index], column=name, shape=shape):
                backend = corrective(rejections)
                engine = self.engine(backend)
                error, seen, _ = self.stepped(backend, AT[label], revision, column(index, name, TASK_BAD[shape]),
                                              lambda: engine.start("Implement greet", plan()))
                self.assertEqual((len(seen["run"]["plan_history"]), sorted(seen["run"]["accepted"]),
                                  len(seen["run"]["final_history"])), held[label])
                if (label, revision) == ("third final_review", 1):   # the calls themselves: nothing followed fix2's review
                    self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 2)
                    self.assertEqual(backend.calls[-1]["task_id"], "fix2")
                self.stopped_before(engine, backend, error, seen, revision)
        with self.subTest(step="pause"):   # --step after an acceptance with work pending
            backend = corrective(2)
            engine = self.engine(backend)
            error, seen, _ = self.stepped(
                backend, lambda run, options: bool(options.get("step") and options.get("accepted_this_call")
                                                   and settled(run) and pending(run)),
                0, column(0, "self_checks", TASK_BAD["env key"]), lambda: engine.start("Implement greet", plan(), step=True))
            self.assertEqual((seen["options"], len(seen["run"]["plan_history"]), sorted(seen["run"]["accepted"])),
                             ({"step": True, "accepted_this_call": 1}, 2, ["greet"]))
            self.stopped_before(engine, backend, error, seen, 0)
            self.assertNotIn('"run_paused"', (engine.directory / "events.jsonl").read_text())
        # Steps only a resumed process derives first: a revision landed but not applied, and a task between its
        # verification and its review (spec: there is no scheduling step between a review and the acceptance). The
        # facts that stopped the first process are the ones its resume meets.
        # Applying a revision replays it on tasks, which would see a changed last version itself: change the first.
        crashes = {"apply_revision": (lambda run: bool(run.get("pending_revision")) and len(run["plan_history"]) == 2,
                                      0, column(0, "self_checks", TASK_BAD["protected cwd"])),
                   "continue_task": (lambda run: (run.get("pending_task") or {}).get("task_id") == "fix2"
                                     and run["pending_task"]["stage"] == "reviewing",
                                     1, column(0, "required_commands", TASK_BAD["blank segment"]))}
        for label, (moment, revision, change) in crashes.items():
            with self.subTest(step=label):
                backend = corrective(2)
                engine = self.engine(backend)
                crash, _ = crash_after_save(engine, moment)
                with crash, self.assertRaises(KeyboardInterrupt):
                    engine.start("Implement greet", plan())
                error, seen, _ = self.stepped(backend, lambda run, options, moment=moment: moment(run), revision, change,
                                              lambda: self.engine(backend).resume(engine.run["id"]))
                run = self.stopped_before(engine, backend, error, seen, revision)
                if label == "apply_revision":
                    self.assertEqual((bool(seen["run"]["pending_revision"]), bool(run.get("pending_revision")),
                                      len(run["plan_history"])), (True, True, 2))
                else:
                    self.assertEqual((seen["run"]["pending_task"]["stage"], run["pending_task"]["stage"],
                                      "fix2" in run["accepted"]), ("reviewing", "reviewing", False))

    def test_completion_rereads_the_plan_and_abandon_stays_available(self):  # 21.2 before status completed
        results = {}
        for valid in (True, False):
            change = column(1, "required_commands", VALID if valid else BAD["parent cwd"])
            backend = corrective(2)
            engine = self.engine(backend)
            final_review, next_step, steps = engine.services.finalization.final_review, Scheduler.next_step, []

            def approved_then_changed(engine=engine, final_review=final_review, change=change, **options):
                done = final_review(**options)
                if done:   # the last whole-run review approved; the run is not yet marked completed
                    rewrite(engine.directory, 1, change)(engine.run)
                return done

            def recorded(scheduler, steps=steps, **options):
                result = next_step(scheduler, **options)
                steps.append((result.kind, result.data[0]["id"] if result.kind == "worker"
                              else result.data.get("kind") if result.kind == "decide" else None))
                return result
            with patch.object(engine.services.finalization, "final_review", side_effect=approved_then_changed), \
                    patch.object(Scheduler, "next_step", recorded):
                try:
                    results[valid] = (engine, backend, steps, engine.start("Implement greet", plan()))
                except HarnessError as error:
                    results[valid] = (engine, backend, steps, error)
        engine, backend, steps, result = results[True]
        # The control: the same rewrite into another valid command is accepted and the run completes.
        self.assertEqual((result["status"], steps), ("completed", STEPS))
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 3)
        self.assertIn('"run_completed"', (engine.directory / "events.jsonl").read_text())
        engine, backend, steps, error = results[False]
        self.assertIsInstance(error, HarnessError)
        self.assert_stop(error, "runtime_integrity")
        run = read_json(engine.directory / "state.json")
        self.assertEqual((run["status"], run["stop"]["kind"], run["stop"]["reason"], run["final_history"][-1]["status"], steps),
                         ("blocked", "evidence", "runtime_integrity", "approved", STEPS))
        self.assertNotIn('"run_completed"', (engine.directory / "events.jsonl").read_text())
        calls = len(backend.calls)
        self.assertEqual(self.engine(backend).abandon(engine.run["id"])["status"], "abandoned")
        self.assertEqual(len(backend.calls), calls)

    def test_settling_a_published_stop_checks_the_commands(self):  # 21.1 through settle_publication
        # A plan awaiting approval is settled by the load checks before its save; a bad command stops the run instead.
        # A raised stop's settling (settle_stop) meets one in the scheduling cases that change the first or last version.
        backend = ScriptedBackend()
        engine = self.engine(backend)
        preflight = PacketBuilder.preflight_final_context

        def preflight_then_changed(builder, *args, **kwargs):
            result = preflight(builder, *args, **kwargs)
            rewrite(builder.context.directory, 0, column(0, "self_checks", BAD["parent cwd"]))(builder.context.run)
            return result
        with patch.object(PacketBuilder, "preflight_final_context", preflight_then_changed), \
                self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan(), pause_after_plan=True)
        self.assert_stop(caught.exception, "load_integrity")
        run = read_json(engine.directory / "state.json")
        self.assertEqual((run["status"], run["stop"]["kind"], run["stop"]["reason"], run["stop"]["data"]["superseded"],
                          backend.calls), ("blocked", "evidence", "load_integrity",
                                           {"kind": "plan_approval", "reason": "initial_plan"}, []))
        self.assertNotIn('"plan_awaiting_approval"', (engine.directory / "events.jsonl").read_text())
