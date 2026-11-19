"""The run's scope boundary (spec-6): inclusion, held proposals, rejection, the combined commit and their windows.

Every model call is the scripted offline backend; no test calls a real model or CLI.
"""
import copy
import itertools
import json
import os
import random
import shlex
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import git, scope as boundary, stops
from harness.command_server import Broker, create_manifest
from harness.context import compact_plan
from harness.contracts import apply_revision, ScopeExtensionRequired, validate_plan
from harness.progress import ProgressRecovery
from harness.recovery import read_bound_result
from harness.review_progress import claim_gaps
from harness.run_store import RunStore
from harness.scope import proposal_request_id
from harness.util import HarnessError, read_json, write_json
from harness.verification_plan import candidate_preflight
from test_harness import (ROOT, RepoCase, commit_recorder, crash_after_save, crash_point, decide_bound, refused_by_cli,
                          run_cli, scratch, walk_exit, walk_notice_exit)
from test_runtime_changes import ScriptedBackend, decided, revised, trigger_kind, update

DECIDE_KEY = "commander:decide:run"


def plan():
    return read_json(ROOT / "examples" / "plan.json")


def decides(backend):
    return [call for call in backend.calls if call["role"] == "commander"]


def saved(engine):
    return read_json(engine.directory / "state.json")


def receipt_entry(run):
    """The dispatch entry the decide receipt names."""
    receipt = run["commander_receipts"][DECIDE_KEY]
    return next(d for d in run["dispatches"] if d["id"] == receipt["dispatch_id"])


def request_of(run):
    return proposal_request_id(run, run["commander_receipts"][DECIDE_KEY])


def invalid_entries(run):
    return [d for d in run["dispatches"] if d.get("status") == "invalid"]


def source_of(run, revision):
    """The commander dispatch a published plan version came from (None for a supplied plan)."""
    source = read_json(Path(run["plan_history"][revision]["artifact"]))["source"]
    return source["dispatch_id"] if source else None


def sources(run):
    return [source_of(run, revision) for revision in range(len(run["plan_history"]))]


def after_landing(backend, calls):
    """Every commander call since ``calls`` came after the held proposal landed (its prerequisite accepted)."""
    return all("prepare" in call["packet"]["accepted"] for call in decides(backend)[calls:])


def rejection(case, request_id, reason="Stay inside the declared boundary"):
    path = scratch(case) / "reject.json"
    write_json(path, {"request_id": request_id, "reason": reason})
    return path


def answer(case, request, summary="Yes, try it"):
    path = scratch(case) / "answer.json"
    write_json(path, {"schema_version": 1, "request_id": request["id"], "task_id": request["task_id"],
                      "decision": "continue", "summary": summary})
    return path


def blocked_then(decide, *, blocked_workers=(1,)):
    """The listed worker calls block on their task; each task decide with worker feedback runs ``decide``."""
    def change(backend, kwargs, packet, result):
        workers = [c for c in backend.calls if c["role"] == "worker"]
        if kwargs["role"] == "worker" and len(workers) in blocked_workers:
            result.update(status="blocked", blocker_kind="task", blockers=["Need support"])
        if trigger_kind(kwargs, packet) == "task" and (packet["trigger"].get("feedback") or {}).get("worker"):
            decide(backend, packet, result)
    return ScriptedBackend(change)


def new_task(backend, packet, result, path="support.txt", description=None):
    """Revise: a new prerequisite ``prepare`` owning hello.py and ``path``, before greet."""
    original = packet["original_tasks"][0]
    task = copy.deepcopy(original)
    task.update(id="prepare", allowed_paths=["hello.py", path])
    if description:
        task["description"] = description
    revised(result, packet, "Add prerequisite", new_tasks=[task], selected="prepare",
            updates=[update(original, deps=["prepare"])])


def widen(*paths, commands=()):
    """Revise: the current task gains ``paths`` (and ``commands``) and stays selected."""
    def decide(backend, packet, result):
        revised(result, packet, "Widen greet", selected="greet",
                updates=[update(packet["original_tasks"][0], paths=paths, commands=commands)])
    return decide


def implement_after_rejection(then=None):
    """Once a rejection reaches the commander, implement greet inside the boundary; else run ``then``."""
    def decide(backend, packet, result):
        if "rejected_revision" in packet:
            decided(result, "implement", selected_task="greet", instruction="Stay inside hello.py")
        else:
            (then or new_task)(backend, packet, result)
    return decide


def ask_then(then):
    """The first task decide asks the operator; later ones run ``then``."""
    state = {"asked": False}

    def decide(backend, packet, result):
        if not state["asked"]:
            state["asked"] = True
            decided(result, "ask", reason="May I add support.txt?")
            return
        then(backend, packet, result)
    return decide


class HeldCase(RepoCase):
    def held(self, decide=new_task, scope=("hello.py",), plan_value=None, **kwargs):
        backend = blocked_then(decide, **kwargs)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "无法证明"):
            engine.start("Implement greet", plan_value or plan(), scope=list(scope))
        return engine, backend

    def asked(self, then, scope=("hello.py",)):
        backend = blocked_then(ask_then(then))
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            engine.start("Implement greet", plan(), scope=list(scope))
        return engine, backend, answer(self, saved(engine)["decision_requests"][-1])

    def required_for(self, engine):
        """The held proposal's final-review required input in this repository."""
        probe = self.engine()
        probe.load_run(engine.run["id"])
        result, _ = read_bound_result(probe.run["commander_receipts"][DECIDE_KEY], "held")
        proposed = apply_revision(probe.run["tasks"], result, probe.run["accepted"], scope=["**"])
        return probe.services.planning.capacity_report(proposed)["required_chars"]

    BIG = staticmethod(implement_after_rejection(lambda b, p, r: new_task(b, p, r, description="Prepare. " + "z" * 10000)))
    COMMAND = {"cwd": ".", "argv": ["python3", "-c", "print(123)"]}

    def capacity_tight(self, scope=("hello.py",), *, held=True):
        """A run whose budget fits the proposal's final review by 12 characters (spec-6 T6-24).

        Appending ``COMMAND`` to the grants later lengthens that review's required input past the
        budget: only a running condition changes (the budget itself can only grow on resume).
        """
        probe, _ = self.held(self.BIG)
        self.config.data["runtime"]["context_chars"] = self.required_for(probe) + 12
        if held:
            return self.held(self.BIG, scope)
        return None, blocked_then(self.BIG)

    def tighten(self):
        self.config.data["permissions"]["worker_commands"].append(self.COMMAND)

    def write_plan_then_die(self, engine, predicate):
        crash, die = crash_point(engine)
        original = engine.services.planning.write_plan

        def wrapper(revision, content):
            reference = original(revision, content)
            if predicate(revision):
                die()
            return reference
        return crash, patch.object(engine.services.planning, "write_plan", side_effect=wrapper)

    def prewritten_and_kept(self):
        """A judged revision whose plan file was written before the landing save, then kept for capacity."""
        engine, backend = self.capacity_tight()
        resumed = self.engine(backend)
        crash, wrapped = self.write_plan_then_die(resumed, lambda revision: revision >= 1)
        with crash, wrapped, self.assertRaises(KeyboardInterrupt):
            resumed.resume(engine.run["id"], scope=["support.txt"])  # landable now: the file is written, then death
        self.tighten()  # now kept for capacity: the written file stays unregistered
        with self.assertRaisesRegex(HarnessError, "提案保留"):
            self.engine(backend).resume(engine.run["id"])
        return engine, backend, engine.directory / "plans" / "revision-1.json"


# ---- T6-1, T6-2: the inclusion rules ------------------------------------------------------------
class InclusionPropertyTests(unittest.TestCase):
    SEGMENTS = ["app", "a", "b", "src", "x.py", "[ab]", "a*"]
    GLOB = ["app", "a", "b", "src", "x.py", "*", "?", "[ab]", "a*", "*.py", "**"]

    @staticmethod
    def universe(segments, depth):
        return ["/".join(p) for d in range(1, depth + 1) for p in itertools.product(segments, repeat=d)]

    def check(self, rule, cases, seed, depth=3):
        """(cases judged inside, false positives) with the runtime's own path_matches as the criterion."""
        rng = random.Random(seed)
        literal = [s for s in self.SEGMENTS if not boundary.wild(s)]
        universe = self.universe(self.SEGMENTS, depth)

        def entry():
            roll = rng.random()
            if roll < 0.08:
                return "**"
            if roll < 0.5:
                return "/".join(rng.choice(literal) for _ in range(rng.randint(1, depth - 1))) + "/**"
            return "/".join(rng.choice(literal) for _ in range(rng.randint(1, depth)))

        def glob():
            return "/".join(rng.choice(self.GLOB) for _ in range(rng.randint(1, depth + 1)))
        judged, false = 0, []
        for _ in range(cases):
            reusable = [entry() for _ in range(rng.randint(1, 3))] + [glob() for _ in range(rng.randint(0, 3))]
            pattern = rng.choice([glob(), entry(), rng.choice(universe)])
            if not rule(pattern, reusable):
                continue
            judged += 1
            for path in universe:
                if git.path_matches(path, pattern) and not any(git.path_matches(path, p) for p in reusable):
                    false.append((pattern, reusable, path))
                    break
        return judged, false

    def test_no_false_positive_and_prefix_mutation_is_caught(self):
        self.assertEqual((len(self.universe(self.SEGMENTS, 3)), len(self.universe(self.SEGMENTS, 4))), (399, 2800))
        for seed, cases, depth in ((20261112, 10000, 3), (20261113, 10000, 3), (20261114, 2000, 4)):
            with self.subTest(seed=seed):
                judged, false = self.check(boundary.contains, cases, seed, depth)
                self.assertGreater(judged, 0)
                self.assertEqual(false, [])

        def prefix_by_string(pattern, reusable):
            if pattern in reusable or (not boundary.wild(pattern) and any(git.path_matches(pattern, p) for p in reusable)):
                return True
            return any(boundary.literal_subtree_prefix(p) is not None
                       and pattern.startswith("/".join(boundary.literal_subtree_prefix(p))) for p in reusable)
        _, caught = self.check(prefix_by_string, 5000, 20261112)
        self.assertTrue(caught)


class InclusionTests(unittest.TestCase):
    def test_rules_examples_false_negatives_and_operator_forms(self):
        contains = boundary.contains
        self.assertTrue(contains("src/a.py", ["lib/x", "src/a.py"]))           # 1: equal
        self.assertTrue(contains("src/x.py", ["src/*.py"]))                    # 2: a concrete path matched
        self.assertTrue(contains("app/*/x.py", ["app/**"]))                    # 3: under a literal subtree
        self.assertTrue(contains("app", ["app/**"]))
        self.assertFalse(contains("app2/x.py", ["app/**"]))                    # by segment, not by string
        self.assertFalse(contains("a*/x", ["a/**"]))
        self.assertFalse(contains("app/[ab]/**", ["app/a/**", "app/b/**"]))    # false negatives allowed
        self.assertFalse(contains("src/x*.py", ["src/*.py"]))
        for pattern in ("x", "a/b/c.py", "**/*.py", "*", "docs/**"):
            self.assertTrue(contains(pattern, ["**"]), pattern)
        for good in ("hello.py", "-file", "a b/c", "src/**", "**", "app/x/**"):
            self.assertEqual(boundary.operator_entry(good), good)
        for bad in ("src/*.py", "a*/**", "**/x.py", ".git/**", ".harness/x", "a//b", "../x", "", None, 3):
            with self.subTest(bad=bad), self.assertRaises(HarnessError):
                boundary.operator_entry(bad)
        self.assertEqual(boundary.operator_entries(["b", "a", "b"]), ["b", "a"])


# ---- T6-3: a revision missing only scope is held -------------------------------------------------
class RevisionHoldTests(HeldCase):
    def test_hold_keeps_receipt_replays_without_model_and_scope_lands_same_original(self):
        engine, backend = self.held()
        run = saved(engine)
        source = run["commander_receipts"][DECIDE_KEY]["dispatch_id"]
        self.assertEqual(receipt_entry(run)["status"], "completed")
        self.assertNotIn("invalid_output", receipt_entry(run))
        self.assertNotIn("pending_revision", run)
        self.assertFalse((engine.directory / "plans" / "revision-1.json").exists())
        data = run["stop"]["data"]
        self.assertEqual((data["gaps"], data["suggested_scope"], data["widened"], data["scope"], data["selected_task"]),
                         ({"prepare": ["support.txt"]}, ["support.txt"], [], ["hello.py"], "prepare"))
        self.assertEqual(run["stop"]["source"]["request_id"], request_of(run))
        calls = len(backend.calls)
        for _ in range(2):  # an ordinary resume replays the same original: same stop, same request, no model
            with self.assertRaisesRegex(HarnessError, "无法证明"):
                self.engine(backend).resume(engine.run["id"])
            again = saved(engine)
            self.assertEqual((len(backend.calls), again["stop"]["source"]["request_id"]),
                             (calls, run["stop"]["source"]["request_id"]))
        after = walk_exit(self, engine.directory, ("scope_extension", "revision"), backend=backend,
                          expect_status="completed")
        self.assertEqual(after["scope"], ["hello.py", "support.txt"])
        self.assertEqual(source_of(after, 1), source)
        self.assertEqual(invalid_entries(after), [])


# ---- T6-5, T6-6: the first plan --------------------------------------------------------------------
class InitialPlanHoldTests(RepoCase):
    def test_model_first_plan_outside_scope_is_held_then_lands_and_pauses(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "首版计划.*无法证明"):
            engine.start("Implement greet", scope=["src/**"], pause_after_plan=True)
        run = saved(engine)
        self.assertEqual((run["stop"]["reason"], run["tasks"], run["stop"]["data"]["gaps"]),
                         ("initial_plan", [], {"greet": ["hello.py"]}))
        self.assertFalse((engine.directory / "plans").exists())
        raw, calls = (engine.directory / "state.json").read_bytes(), len(backend.calls)
        notice = refused_by_cli(self, backend, ["resume", engine.run["id"], "--approve-plan"],
                                ("invalid_input", "plan_unpublished"), 2)
        self.assertEqual((engine.directory / "state.json").read_bytes(), raw)
        walk_notice_exit(self, notice, ("invalid_input", "plan_unpublished"), backend=backend,
                         directory=engine.directory, expect_status="blocked")
        after = walk_exit(self, engine.directory, ("scope_extension", "initial_plan"), backend=backend,
                          expect_status="awaiting_approval")
        self.assertEqual((len(backend.calls), after["scope"]), (calls, ["src/**", "hello.py"]))
        from harness.cli import status_summary
        summary = status_summary(after)["scope_summary"]
        self.assertEqual(summary["tasks"][0]["groups"], {".": [{"pattern": "hello.py", "in_scope": True}]})
        self.assertEqual(run_cli(self, backend, ["resume", engine.run["id"], "--approve-plan"])[0], 0)
        self.assertEqual(saved(engine)["status"], "completed")

    def test_large_non_ascii_gaps_keep_the_typed_hold_and_its_exits(self):
        """Code-6 review: 90 paths of 50 emoji each still hold as initial_plan with its listed exits (exit 3)."""
        paths = [f"docs/{i}_" + "\U0001F600" * 50 + ".md" for i in range(90)]

        def change(backend, kwargs, packet, result):
            if trigger_kind(kwargs, packet) == "plan":
                result["new_tasks"][0]["allowed_paths"] = ["hello.py", *paths]
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "首版计划"):
            engine.start("Implement greet", scope=["hello.py"], pause_after_plan=True)
        run = saved(engine)
        stop = run["stop"]
        self.assertEqual((stop["kind"], stop["reason"]), ("scope_extension", "initial_plan"))
        self.assertLessEqual(len(json.dumps(stop, ensure_ascii=True)), 32 * 1024)
        suggested = stop["data"]["suggested_scope"]  # concrete paths: each suggestion is the path itself
        self.assertEqual(suggested, paths[:len(suggested)])
        self.assertGreater(len(suggested), 0)
        kept = stop["data"]["gaps"].get("greet", [])
        self.assertEqual(kept, paths[:len(kept)])
        self.assertEqual(len(kept) + stop["data"].get("gaps_omitted", 0), 90)
        projected = stops.project(stop, run["id"])
        self.assertEqual((projected["exit_code"], projected["exits"][1:]),
                         (3, [f"resume {run['id']} --reject-request FILE", f"status {run['id']}"]))
        words = shlex.split(projected["exits"][0])
        self.assertEqual([w[len("--scope="):] for w in words if w.startswith("--scope=")], suggested)
        # The complete gaps stay readable in status, recomputed from the original.
        from harness.cli import status_summary
        self.assertEqual(status_summary(run)["scope"]["held"]["gaps"], {"greet": paths})
        source, calls = run["commander_receipts"][DECIDE_KEY]["dispatch_id"], len(backend.calls)
        result, errors = run_cli(self, backend, ["resume", run["id"]])
        again = saved(engine)
        self.assertEqual((result, again["stop"]["kind"], again["stop"]["reason"], len(backend.calls)),
                         (3, "scope_extension", "initial_plan", calls))
        # The listed exit widens by the suggested entries: the rest is still held, the same request, no call.
        after = walk_exit(self, engine.directory, ("scope_extension", "initial_plan"), backend=backend,
                          expect_code=3, expect_status="blocked")
        self.assertEqual((after["stop"]["reason"], after["stop"]["source"]["request_id"], len(backend.calls)),
                         ("initial_plan", stop["source"]["request_id"], calls))
        self.assertEqual(status_summary(after)["scope"]["held"]["gaps"], {"greet": paths[len(suggested):]})
        self.assertEqual(run_cli(self, backend, ["resume", run["id"], "--scope=docs/**"])[0], 0)
        published = saved(engine)
        self.assertEqual((published["status"], source_of(published, 0), len(backend.calls)),
                         ("awaiting_approval", source, calls))
        self.assertEqual(published["tasks"][0]["allowed_paths"], ["hello.py", *paths])

    def test_undeclared_scope_does_not_constrain_first_plan(self):
        run = self.engine(ScriptedBackend()).start("Implement greet", pause_after_plan=True)
        self.assertEqual((run["status"], run["scope"]), ("awaiting_approval", []))
        from harness.cli import status_summary
        summary = status_summary(run)
        self.assertEqual((summary["scope"]["declared"], summary["scope_summary"]["declared"]), (False, False))
        self.assertIn("note", summary["scope"])
        self.assertIsNone(summary["scope_summary"]["tasks"][0]["groups"]["."][0]["in_scope"])


# ---- T6-7, T6-8: refused at the input boundary -------------------------------------------------------
class StartBoundaryTests(RepoCase):
    def runs(self):
        folder = self.repo / ".harness" / "runs"
        return sorted(folder.iterdir()) if folder.exists() else []

    def test_supplied_plan_outside_scope_creates_nothing(self):
        worktrees, branches = git.git(self.repo, "worktree", "list"), git.git(self.repo, "branch", "--list")
        path = scratch(self) / "plan.json"
        write_json(path, plan())
        notice = refused_by_cli(self, ScriptedBackend(), ["run", "Implement greet", "--plan", str(path), "--scope", "src/**"],
                                ("invalid_input", "plan_outside_scope"), 2)
        self.assertEqual((notice["data"]["gaps"], notice["data"]["suggested_scope"]), ({"greet": ["hello.py"]}, ["hello.py"]))
        self.assertEqual(self.runs(), [])
        self.assertEqual((git.git(self.repo, "worktree", "list"), git.git(self.repo, "branch", "--list")),
                         (worktrees, branches))
        self.assertEqual(run_cli(self, ScriptedBackend(), ["run", "Implement greet", "--plan", str(path),
                                                            "--scope", "src/**", "--scope", "hello.py"])[0], 0)

    def test_scope_pattern_forms_rejected_before_anything(self):
        notice = refused_by_cli(self, ScriptedBackend(), ["run", "Implement greet", "--scope", "src/*.py", "--scope", "ok.py"],
                                ("invalid_input", "scope_pattern"), 2)
        self.assertEqual(notice["data"]["patterns"], ["src/*.py"])
        self.assertEqual(self.runs(), [])
        backend = ScriptedBackend()
        engine = self.engine(backend)
        engine.start("Implement greet", plan(), pause_after_plan=True)
        raw = (engine.directory / "state.json").read_bytes()
        for bad in (".git/**", "a//b", "**/x.py"):
            with self.subTest(pattern=bad):
                refused_by_cli(self, backend, ["resume", engine.run["id"], "--scope", "fine.py", f"--scope={bad}"],
                               ("invalid_input", "scope_pattern"), 2)
                self.assertEqual((engine.directory / "state.json").read_bytes(), raw)


# ---- T6-9 .. T6-12: rejecting a proposal -----------------------------------------------------------
class RejectRequestTests(HeldCase):
    def test_reject_held_revision_consumes_once_and_never_revives(self):
        engine, backend = self.held(implement_after_rejection())
        run = saved(engine)
        source, request_id = run["commander_receipts"][DECIDE_KEY]["dispatch_id"], request_of(run)
        after = walk_exit(self, engine.directory, ("scope_extension", "revision"), backend=backend, choose=1,
                          fill={"FILE": rejection(self, request_id, "Keep hello.py only")}, expect_status="completed")
        entry = next(d for d in after["dispatches"] if d["id"] == source)
        record = read_json(Path(entry["proposal_rejection"]["artifact"]))
        self.assertEqual((record["request_id"], record["kind"], record["reason"], record["gaps"]),
                         (request_id, "revision", "Keep hello.py only", {"prepare": ["support.txt"]}))
        packet = decides(backend)[-1]["packet"]
        self.assertEqual((packet["rejected_revision"]["request_id"], packet["rejected_revision"]["dispatch_id"]),
                         (request_id, source))
        self.assertEqual(len(after["plan_history"]), 1)  # the rejected original never landed
        # Widening the boundary afterwards revives nothing.
        self.assertEqual(self.engine(backend).resume(engine.run["id"])["status"], "completed")
        self.assertNotIn(source, sources(after))

    def test_reject_held_initial_plan_replans_with_reason(self):
        def change(backend, kwargs, packet, result):
            if trigger_kind(kwargs, packet) == "plan" and "rejected_revision" in packet:
                result["new_tasks"][0]["allowed_paths"] = ["src/**"]
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "首版计划"):
            engine.start("Implement greet", scope=["src/**"], pause_after_plan=True)
        run = self.engine(backend).resume(engine.run["id"], reject_request=rejection(self, request_of(saved(engine)),
                                                                                         "Plan inside src"))
        self.assertEqual((run["status"], run["tasks"][0]["allowed_paths"]), ("awaiting_approval", ["src/**"]))
        rejected = decides(backend)[-1]["packet"]["rejected_revision"]
        self.assertEqual((rejected["kind"], rejected["reason"]), ("initial_plan", "Plan inside src"))

    def test_unlanded_in_scope_proposal_can_be_rejected_and_unknown_cannot(self):
        engine, backend = self.held(implement_after_rejection())
        run = saved(engine)
        source, request_id = run["commander_receipts"][DECIDE_KEY]["dispatch_id"], request_of(run)
        resumed = self.engine(backend)
        crash, fired = crash_after_save(resumed, lambda ledger: "support.txt" in ledger["scope"])
        with crash, self.assertRaises(KeyboardInterrupt):
            resumed.resume(engine.run["id"], scope=["support.txt"])
        self.assertTrue(fired["fired"])
        self.assertIn(DECIDE_KEY, saved(engine)["commander_receipts"])  # inside the boundary now, not landed
        raw = (engine.directory / "state.json").read_bytes()
        for value, row in ((rejection(self, "proposal_" + "0" * 64), ("invalid_input", "stale_request")),
                           (rejection(self, "request_" + "0" * 64), ("invalid_input", "input_schema"))):
            with self.subTest(row=row):
                refused_by_cli(self, backend, ["resume", engine.run["id"], "--reject-request", str(value)], row, 2)
                self.assertEqual((engine.directory / "state.json").read_bytes(), raw)
        calls = len(backend.calls)
        after = self.engine(backend).resume(engine.run["id"], reject_request=rejection(self, request_id))
        self.assertEqual(after["status"], "completed")
        self.assertEqual(decides(backend)[-1]["packet"]["rejected_revision"]["request_id"], request_id)
        self.assertGreater(len(backend.calls), calls)
        self.assertEqual(len(after["plan_history"]), 1)
        self.assertIn("proposal_rejection", next(d for d in after["dispatches"] if d["id"] == source))
        # An original never judged (bound, then the process died) cannot be rejected: resume judges it first.
        self.setUp()
        backend = blocked_then(new_task)
        engine = self.engine(backend)
        crash, fired = crash_after_save(engine, decide_bound)
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan(), scope=["hello.py"])
        run = saved(engine)
        self.assertNotEqual(receipt_entry(run)["status"], "completed")
        raw = (engine.directory / "state.json").read_bytes()
        refused_by_cli(self, backend, ["resume", engine.run["id"], "--reject-request", str(rejection(self, request_of(run)))],
                       ("invalid_input", "stale_request"), 2)
        self.assertEqual((engine.directory / "state.json").read_bytes(), raw)

    def test_only_the_proposals_own_plan_file_is_archived(self):
        """An unregistered plan file is archived only when it is this proposal's bytes, checked again at the rename."""
        engine, backend, orphan = self.prewritten_and_kept()
        raw, state = orphan.read_bytes(), (engine.directory / "state.json").read_bytes()
        path = rejection(self, request_of(saved(engine)))
        orphan.write_bytes(raw.replace(b'"reason"', b'"reason" ', 1))  # not the bytes this proposal publishes
        refused_by_cli(self, backend, ["resume", engine.run["id"], "--reject-request", str(path)],
                       ("evidence", "runtime_integrity"), 1)
        # Refused by the preflight: no rejection file, no archive, the ledger unchanged.
        self.assertEqual((engine.directory / "state.json").read_bytes(), state)
        self.assertFalse((engine.directory / "proposal-rejections").exists())
        self.assertFalse((engine.directory / "plans" / "rejected").exists())
        orphan.write_bytes(raw)
        from harness.planning import Planner
        publish = Planner.record_proposal_rejection

        def changed_after_the_check(planner, prepared):
            orphan.write_bytes(raw + b" ")
            return publish(planner, prepared)
        with patch.object(Planner, "record_proposal_rejection", changed_after_the_check), \
                self.assertRaises(HarnessError) as caught:
            self.engine(backend).resume(engine.run["id"], reject_request=path)
        self.assertEqual(caught.exception.stop["kind"], "evidence")
        self.assertFalse((engine.directory / "plans" / "rejected").exists())
        self.assertIn(DECIDE_KEY, saved(engine)["commander_receipts"])
        orphan.write_bytes(raw)
        run = self.engine(backend).resume(engine.run["id"], reject_request=path)
        self.assertEqual(run["status"], "completed")
        self.assertEqual([p.read_bytes() for p in (engine.directory / "plans" / "rejected").glob("*.json")], [raw])

    def test_reject_request_exclusions_write_nothing(self):
        engine, backend = self.held()
        run = saved(engine)
        path = rejection(self, request_of(run))
        update_file, guidance, decision = (scratch(self) / name for name in ("update.json", "guidance.json", "answer.json"))
        for item in (update_file, guidance, decision):
            write_json(item, {})
        raw, calls = (engine.directory / "state.json").read_bytes(), len(backend.calls)
        for extra in (["--scope", "support.txt"], ["--operator-decision", str(decision)],
                      ["--environment-update", str(update_file)], ["--execution-guidance", str(guidance)], ["--reverify"]):
            with self.subTest(option=extra[0]):
                refused_by_cli(self, backend, ["resume", engine.run["id"], "--reject-request", str(path), *extra],
                               ("invalid_input", "incompatible_options"), 2)
                self.assertEqual(((engine.directory / "state.json").read_bytes(), len(backend.calls)), (raw, calls))


# ---- T6-13, T6-14: append-only task updates ----------------------------------------------------------
class AppendFieldsTests(HeldCase):
    def test_update_appends_paths_and_commands_in_order_without_duplicates(self):
        tasks = plan()["tasks"]
        tasks[0]["allowed_paths"] = ["hello.py", "a.py"]
        first = {"new_tasks": [], "updates": [update(tasks[0], paths=["b.py", "hello.py", "c.py", "b.py"],
                                                     commands=[{"cwd": ".", "argv": ["python3", "-V"]}, {"cwd": ".", "argv": ["python3", "-V"]}])]}
        revised_tasks = apply_revision(tasks, first, set(), scope=["**"])
        self.assertEqual(revised_tasks[0]["allowed_paths"], ["hello.py", "a.py", "b.py", "c.py"])
        self.assertEqual(revised_tasks[0]["required_commands"], [{"cwd": ".", "argv": ["python3", "-V"]}])  # created when absent
        second = {"new_tasks": [], "updates": [update(revised_tasks[0], commands=[{"cwd": ".", "argv": ["git", "status"]}, {"cwd": ".", "argv": ["python3", "-V"]}])]}
        again = apply_revision(revised_tasks, second, set(), scope=["**"])
        self.assertEqual(again[0]["required_commands"], [{"cwd": ".", "argv": ["python3", "-V"]}, {"cwd": ".", "argv": ["git", "status"]}])
        for fields in ({"paths": ["hello.py", "z.py"]}, {"commands": [{"cwd": ".", "argv": ["python3", "-V"]}]}):
            with self.subTest(accepted=fields), self.assertRaisesRegex(HarnessError, "未接受"):
                apply_revision(tasks, {"new_tasks": [], "updates": [update(tasks[0], **fields)]}, {"greet"}, scope=["**"])
        # The compact plan context represents the appended fields losslessly.
        compact = compact_plan(tasks, again)
        rebuilt = copy.deepcopy(tasks)
        for row in compact["tasks_delta"]["updates"]:
            task = next(t for t in rebuilt if t["id"] == row["id"])
            task["description"] = row.get("description", task["description"])
            task["depends_on"] += row.get("add_depends_on", [])
            task["allowed_paths"] += row.get("add_allowed_paths", [])
            if row.get("add_required_commands"):
                task.setdefault("required_commands", []).extend(row["add_required_commands"])
        self.assertEqual(rebuilt, again)

    def test_current_task_widening_retains_candidate_and_new_commands_gate_dispatch(self):
        command = {"cwd": ".", "argv": ["python3", "-c", "print('support')"]}
        backend = blocked_then(widen("support.txt", commands=[command]))
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "所需命令尚未授权"):
            engine.start("Implement greet", plan(), scope=["hello.py", "support.txt"])
        run = saved(engine)
        self.assertEqual((run["stop"]["kind"], run["stop"]["reason"]), ("missing_commands", "selected_task"))
        self.assertEqual(read_json(Path(run["plan_history"][1]["artifact"]))["candidate"], "retain")
        self.assertEqual(run["tasks"][0]["allowed_paths"], ["hello.py", "support.txt"])
        attempts = copy.deepcopy(run["attempts"])
        with self.assertRaisesRegex(HarnessError, "所需命令尚未授权"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(saved(engine)["attempts"], attempts)
        self.config.data["permissions"]["worker_commands"].append(command)
        self.assertEqual(self.engine(backend).resume(engine.run["id"])["status"], "completed")


# ---- T6-15, T6-20, T6-21: what the boundary never grants ---------------------------------------------
class MoreGuaranteesTests(RepoCase):
    def test_proposal_never_authorizes_itself(self):
        original = plan()["tasks"]
        first = copy.deepcopy(original[0])
        first.update(id="a", allowed_paths=["support/**"])
        second = copy.deepcopy(original[0])
        second.update(id="b", allowed_paths=["support/x.py"])
        revision = {"new_tasks": [first, second], "updates": [update(original[0], paths=["support/y.py"])]}
        with self.assertRaises(ScopeExtensionRequired) as caught:
            apply_revision(original, revision, set(), scope=["hello.py"])
        self.assertEqual(caught.exception.gaps, {"greet": ["support/y.py"], "a": ["support/**"], "b": ["support/x.py"]})
        self.assertEqual(apply_revision(original, revision, set(), scope=["support/**"])[0]["allowed_paths"],
                         ["hello.py", "support/y.py"])

    def test_boundary_never_blesses_an_out_of_scope_candidate(self):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                (Path(kwargs["cwd"]) / "extra.py").write_text("x = 1\n")
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "越界"):
            engine.start("Implement greet", plan(), scope=["**"])
        run = saved(engine)
        worker = next(d for d in run["dispatches"] if d["role"] == "worker")
        self.assertEqual(worker["worker_receipt_consumed"], "scope_rejected")
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "越界"):
            self.engine(backend).resume(engine.run["id"], scope=["extra.py"])
        after = saved(engine)
        self.assertEqual((after["stop"]["kind"], after["stop"]["reason"], len(backend.calls)),
                         ("scope_violation", "candidate_outside_scope", calls))

    def test_protected_roots_stay_protected_under_double_star(self):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                (Path(kwargs["cwd"]) / ".codex").mkdir(exist_ok=True)
                (Path(kwargs["cwd"]) / ".codex" / "x").write_text("x\n")
        tasks = plan()
        tasks["tasks"][0]["allowed_paths"] = ["**"]
        engine = self.engine(ScriptedBackend(change))
        with self.assertRaisesRegex(HarnessError, "受保护路径"):
            engine.start("Implement greet", tasks, scope=["**"])
        run = saved(engine)
        self.assertEqual((run["stop"]["kind"], run["stop"]["reason"]), ("scope_violation", "protected_path"))


# ---- Protected roots are judged by folded name, on every platform -------------------------------------
# Spellings some filesystem may resolve to a protected root: letter case, a trailing dot or space,
# a format character, a compatibility form.
ALIASES = (".CLAUDE", ".Codex", ".GIT", ".Harness", ".claude.", ".codex ", ".cl​aude", "．claude")


def case_insensitive(folder):
    marker = Path(folder) / "CaSe-PrObE"
    marker.write_text("x")
    try:
        return (Path(folder) / "case-probe").exists()
    finally:
        marker.unlink()


class ProtectedAliasTests(RepoCase):
    def runs(self):
        folder = self.repo / ".harness" / "runs"
        return sorted(folder.iterdir()) if folder.exists() else []

    def test_alias_names_are_refused_by_name_and_wildcards_fall_to_the_candidate_check(self):
        def with_evidence(folder):
            tasks = plan()
            tasks["tasks"][0]["evidence_requirements"] = [{"id": "settings", "acceptance_index": 0, "kind": "static",
                                                           "path": "src/" + folder + "/settings.json"}]
            return tasks
        validate_plan(with_evidence("settings"))
        for root in ALIASES:
            with self.subTest(root=root):
                with self.assertRaisesRegex(HarnessError, "受保护路径"):
                    git.validate_scope_pattern(root + "/**")
                with self.assertRaises(HarnessError):
                    validate_plan(with_evidence(root))
        # A wildcard first segment is not inferred at the pattern level; the candidate check refuses
        # what it matches, under the original spelling.
        (self.repo / ".Claude").mkdir()
        (self.repo / ".Claude" / "settings.json").write_text("{}\n")
        for pattern in ("**", "*/**", ".[cC]laude/**"):
            with self.subTest(pattern=pattern):
                git.validate_scope_pattern(pattern)
                with self.assertRaises(git.ScopeViolation) as caught:
                    git.check_scope(self.repo, [pattern])
                self.assertEqual((caught.exception.stop["reason"], caught.exception.stop["data"]),
                                 ("protected_path", {"path": ".Claude/settings.json"}))
        (self.repo / ".Claude" / "settings.json").unlink()
        (self.repo / ".Claude").rmdir()
        for root in (".claudes", "claude", "x.git"):  # not a protected name in any spelling
            (self.repo / root).mkdir()
            (self.repo / root / "settings.json").write_text("{}\n")
        self.assertEqual(git.check_scope(self.repo, ["**"]),
                         [".claudes/settings.json", "claude/settings.json", "x.git/settings.json"])

    def test_alias_entries_are_refused_before_a_run_or_a_revision_exists(self):
        path = scratch(self) / "plan.json"
        for root in ALIASES:
            with self.subTest(entry="supplied plan", root=root):
                tasks = plan()
                tasks["tasks"][0]["allowed_paths"] = ["hello.py", root + "/**"]
                write_json(path, tasks)
                notice = refused_by_cli(self, ScriptedBackend(), ["run", "Implement greet", "--plan", str(path)],
                                        ("invalid_input", "input_schema"), 2)
                self.assertEqual(notice["data"]["option"], "--plan")
                self.assertEqual(self.runs(), [])
            with self.subTest(entry="run --scope", root=root):
                notice = refused_by_cli(self, ScriptedBackend(), ["run", "Implement greet", f"--scope={root}/**"],
                                        ("invalid_input", "scope_pattern"), 2)
                self.assertEqual(notice["data"]["patterns"], [root + "/**"])
                self.assertEqual(self.runs(), [])
        backend = ScriptedBackend()
        engine = self.engine(backend)
        engine.start("Implement greet", plan(), pause_after_plan=True)
        raw = (engine.directory / "state.json").read_bytes()
        for root in ALIASES:
            with self.subTest(entry="resume --scope", root=root):
                refused_by_cli(self, backend, ["resume", engine.run["id"], f"--scope={root}/settings.json"],
                               ("invalid_input", "scope_pattern"), 2)
                self.assertEqual((engine.directory / "state.json").read_bytes(), raw)

    def test_revision_appending_an_alias_is_invalid_and_publishes_no_revision(self):
        backend = blocked_then(widen(".Claude/**"))
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "连续两次无效"):
            engine.start("Implement greet", plan(), scope=["**"])
        run = saved(engine)
        self.assertEqual((run["stop"]["kind"], run["stop"]["reason"]), ("invalid_output", "redo_exhausted"))
        replans = [d for d in run["dispatches"] if (d["role"], d["kind"]) == ("commander", "decide")]
        self.assertEqual([d["status"] for d in replans], ["invalid", "invalid"])
        self.assertTrue(all("受保护路径" in d["error"] for d in replans))
        self.assertEqual((len(run["plan_history"]), run["tasks"][0]["allowed_paths"]), (1, ["hello.py"]))
        self.assertFalse((engine.directory / "plans" / "revision-1.json").exists())

    def write_then_refused(self, target, check=None):
        """A ``**`` task whose worker writes ``target``: refused at acceptance under that spelling, nothing accepted."""
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                written = Path(kwargs["cwd"]) / target
                written.parent.mkdir(parents=True, exist_ok=True)
                written.write_text("{}\n")
                if check:
                    check(Path(kwargs["cwd"]))
        tasks = plan()
        tasks["tasks"][0]["allowed_paths"] = ["**"]
        head = git.head(self.repo)
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "受保护路径"):
            engine.start("Implement greet", tasks, scope=["**"])
        run = saved(engine)
        self.assertEqual((run["stop"]["kind"], run["stop"]["reason"], run["stop"]["data"]["path"]),
                         ("scope_violation", "protected_path", target))
        self.assertEqual((run["accepted"], git.head(engine.worktree, binding=run["worktree_binding"])), ({}, head))
        worker = next(d for d in run["dispatches"] if d["role"] == "worker")
        self.assertEqual(worker["worker_receipt_consumed"], "scope_rejected")
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "受保护路径"):
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual((len(backend.calls), saved(engine)["accepted"]), (calls, {}))

    def test_double_star_candidate_writing_an_alias_is_refused_by_name(self):
        self.write_then_refused(".CLAUDE/settings.json")

    def test_alias_that_is_the_same_file_is_refused(self):
        if not case_insensitive(self.repo):
            self.skipTest("needs a case-insensitive filesystem")
        seen = []

        def same_file(cwd):
            seen.append((cwd / ".CLAUDE" / "settings.json").samefile(cwd / ".claude" / "settings.json"))
        self.write_then_refused(".CLAUDE/settings.json", same_file)
        self.assertEqual(seen, [True])

    def test_every_gate_judges_protected_names_through_the_one_shared_rule(self):
        """A local copy of the rule answers like the rule, so widen the shared rule by one name: every gate must follow."""
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor" / "lib.py").write_text("x = 1\n")
        git.git(self.repo, "add", ".")
        git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false",
                "commit", "-qm", "vendor")
        (self.repo / "vendor" / "new.py").write_text("y = 2\n")
        tasks = plan()
        tasks["tasks"][0]["evidence_requirements"] = [{"id": "lib", "acceptance_index": 0, "kind": "static",
                                                       "path": "vendor/lib.py"}]
        job = scratch(self).resolve() / "worker"
        manifest = create_manifest(self.config, self.repo, job, delete_scope={"task_id": "t", "allowed_paths": ["**"]})
        broker = Broker(manifest["artifact"], manifest["artifact_digest"])

        def refused(call):
            try:
                call()
            except HarnessError as error:
                return error.stop["reason"] if error.stop else True
            return False

        def verdicts():
            # Planning, acceptance, the readers of worker-named paths, and delete_path last: it unlinks.
            preflight = candidate_preflight(tasks["tasks"][0], {}, worktree=self.repo, candidate_snapshot={},
                                            current_snapshot={}, worker_artifact=job / "absent", worker_artifact_digest="")
            return {"scope pattern": refused(lambda: git.validate_scope_pattern("vendor/**")),
                    "evidence requirement": refused(lambda: validate_plan(tasks)),
                    "candidate": refused(lambda: git.check_scope(self.repo, ["**"])),
                    "evidence preflight": [f["reason"] for f in preflight["failures"] if f["code"] == "evidence_invalid"],
                    "repair claim": claim_gaps({"repair_claims": {"k": {"evidence_paths": ["vendor/lib.py"]}}}, [],
                                               {"files": {"vendor/lib.py": ""}}) != [],
                    "checkpoint source": refused(lambda: ProgressRecovery(SimpleNamespace(worktree=self.repo))
                                                 ._checkpoint_path("vendor/lib.py")),
                    "delete_path": broker.delete_path({"path": "vendor/lib.py"})["outcome"]}

        shared = git.protected_name
        with patch.object(git, "protected_name", lambda segment: shared(segment) or segment == "vendor"):
            self.assertEqual(verdicts(), {"scope pattern": True, "evidence requirement": True, "candidate": "protected_path",
                                          "evidence preflight": ["unsafe_path"], "repair claim": True,
                                          "checkpoint source": True, "delete_path": "refused"})
        self.assertTrue((self.repo / "vendor" / "lib.py").is_file())
        # The same calls without the widening: nothing but the shared rule refused the name.
        self.assertEqual(verdicts(), {"scope pattern": False, "evidence requirement": False, "candidate": False,
                                      "evidence preflight": ["outside_candidate"], "repair claim": False,
                                      "checkpoint source": False, "delete_path": "deleted"})


# ---- T6-16, T6-17: an operator answer lives until a decide lands ---------------------------------------
class AnswerValidityTests(HeldCase):
    def test_answer_survives_hold_and_rejection(self):
        def then(backend, packet, result):
            if "rejected_revision" in packet:
                decided(result, "implement", selected_task="greet", instruction="Do it within hello.py")
            else:
                new_task(backend, packet, result)
        engine, backend, reply = self.asked(then)
        with self.assertRaisesRegex(HarnessError, "无法证明"):
            self.engine(backend).resume(engine.run["id"], operator_decision=reply)
        held = saved(engine)
        answered = decides(backend)[-1]["packet"]["operator_answer"]
        run = self.engine(backend).resume(engine.run["id"], reject_request=rejection(self, request_of(held)))
        self.assertEqual(run["status"], "completed")
        last = decides(backend)[-1]["packet"]
        self.assertEqual((last["operator_answer"], last["rejected_revision"]["request_id"]), (answered, request_of(held)))

    def test_answer_survives_hold_until_scope_lands(self):
        state = {"revised": False}

        def then(backend, packet, result):
            if not state["revised"]:
                state["revised"] = True
                widen("support.txt")(backend, packet, result)
            else:
                decided(result, "implement", selected_task="greet", instruction="Finish")
        backend = blocked_then(ask_then(then), blocked_workers=(1, 2))
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            engine.start("Implement greet", plan(), scope=["hello.py"])
        reply = answer(self, saved(engine)["decision_requests"][-1])
        with self.assertRaisesRegex(HarnessError, "无法证明"):
            self.engine(backend).resume(engine.run["id"], operator_decision=reply)
        self.assertIn("operator_answer", decides(backend)[-1]["packet"])  # the held call carried it
        run = self.engine(backend).resume(engine.run["id"], scope=["support.txt"])
        self.assertEqual(run["status"], "completed")
        last = decides(backend)[-1]
        self.assertEqual(last["packet"]["trigger"]["kind"], "task")
        self.assertNotIn("operator_answer", last["packet"])  # a decide landed since


# ---- T6-18: answers, the boundary and a relaxation in one commit (and I4, I5) ----------------------------
class CombinedCommitTests(HeldCase):
    def test_answer_scope_and_relaxation_are_one_commit_and_restore_on_failure(self):
        engine, backend, reply = self.asked(lambda b, p, r: decided(r, "implement", selected_task="greet",
                                                                    instruction="Finish greet"))
        extra = {"cwd": ".", "argv": ["python3", "-c", "print(1)"]}
        self.config.data["permissions"]["worker_commands"].append(extra)
        before = (engine.directory / "state.json").read_bytes()
        decisions = engine.directory / "decisions"
        listing = lambda: sorted(p.name for p in decisions.rglob("*")) if decisions.exists() else []
        files = listing()

        def three(run):
            return ("support.txt" in run["scope"], bool(run["operator_decisions"]),
                    extra in run["config_snapshot"]["permissions"]["worker_commands"])
        with self.subTest(window="I4: before the prewrite"):
            resumed = self.engine(backend)
            crash, die = crash_point(resumed)
            with crash, patch.object(resumed.services.decisions, "commit_resume_inputs", side_effect=lambda *a, **k: die()), \
                    self.assertRaises(KeyboardInterrupt):
                resumed.resume(engine.run["id"], operator_decision=reply, scope=["support.txt"])
            self.assertEqual(((engine.directory / "state.json").read_bytes(), listing()), (before, files))
        with self.subTest(window="I5: prewritten, the save fails"):
            failing = self.engine(backend)
            original = RunStore.commit

            def fail(store, *args, **kwargs):
                if "support.txt" in store.run["scope"]:
                    raise OSError("disk full")
                return original(store, *args, **kwargs)
            with patch.object(RunStore, "commit", fail), self.assertRaises(OSError):
                failing.resume(engine.run["id"], operator_decision=reply, scope=["support.txt"])
            old = read_json(engine.directory / "state.json")
            self.assertEqual(three(failing.run), (False, False, False))  # memory restored to the old values
            self.assertEqual((engine.directory / "state.json").read_bytes(), before)
            self.assertNotEqual(listing(), files)  # the prewritten answer is an orphan file
            self.assertEqual(three(old), (False, False, False))
        with self.subTest(window="a failed save restores the three values in memory"):
            probe = self.engine(backend)
            probe.load_run(engine.run["id"])
            values, records = probe.services.decisions.prepare_operator_decisions(reply)
            old = {key: probe.run[key] for key in ("operator_decisions", "scope", "config_snapshot")}
            with patch.object(RunStore, "commit", side_effect=OSError("disk full")), self.assertRaises(OSError):
                probe.services.decisions.commit_resume_inputs(records, scope_added=["support.txt"],
                                                              relaxed_config=self.config.data)
            self.assertEqual({key: probe.run[key] for key in old}, old)
            self.assertTrue(all(probe.run[key] is old[key] for key in old))
        recorder, commits = commit_recorder()
        with recorder:
            run = self.engine(backend).resume(engine.run["id"], operator_decision=reply, scope=["support.txt"])
        self.assertEqual(run["status"], "completed")
        self.assertTrue(commits)
        for ledger in commits:
            self.assertIn(three(ledger), ((False, False, False), (True, True, True)))


# ---- T6-22: the field shape (a directory boundary) ----------------------------------------------------
class FieldReplayTests(RepoCase):
    def test_directory_boundary_lets_commander_extend_unaccepted_task_without_stop(self):
        app = "spikes/sp-2-export/app"
        missing = [f"{app}/assets/fonts/a.ttf", f"{app}/assets/fonts/b.ttf", f"{app}/.gitignore",
                   f"{app}/.metadata", f"{app}/analysis_options.yaml"]
        task = {"id": "pv11_apply", "title": "Apply export", "description": "Write the app files",
                "depends_on": [], "allowed_paths": [f"{app}/android/**", f"{app}/ios/**", f"{app}/pubspec.yaml", "hello.py"],
                "acceptance": ["test_greet passes"], "skills": []}

        def change(backend, kwargs, packet, result):
            workers = [c for c in backend.calls if c["role"] == "worker"]
            if kwargs["role"] == "worker":
                if len(workers) == 1:
                    result.update(status="blocked", blocker_kind="task", blockers=["Need five more app paths"])
                else:
                    for path in missing:
                        target = Path(kwargs["cwd"]) / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text("x\n")
            if trigger_kind(kwargs, packet) == "task" and (packet["trigger"].get("feedback") or {}).get("worker"):
                revised(result, packet, "Add the missing app paths",
                        updates=[update(packet["original_tasks"][0], paths=missing)], selected="pv11_apply")
        backend = ScriptedBackend(change)
        run = self.engine(backend).start("Finish export", {"tasks": [task]}, scope=[f"{app}/**", "hello.py"])
        self.assertEqual((run["status"], len(decides(backend))), ("completed", 1))
        self.assertEqual(read_json(Path(run["plan_history"][1]["artifact"]))["candidate"], "retain")
        self.assertEqual(run["tasks"][0]["allowed_paths"][-5:], missing)


# ---- T6-23: read-only projections ------------------------------------------------------------------
def files(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file())


class ScopeSummaryTests(HeldCase):
    def test_summary_and_held_view_are_read_only_and_bounded(self):
        from harness.cli import status_summary
        tasks = [{"id": f"t{i}", "allowed_paths": [f"d{i}/f{j}.py" for j in range(100)] + ["top.py", "lib/*.py"]}
                 for i in range(6)]
        summary = boundary.summary({"scope": ["d0/**"], "tasks": tasks})
        listed = sum(len(rows) for task in summary["tasks"] for rows in task["groups"].values())
        self.assertEqual((listed, summary["patterns_omitted"]), (512, 6 * 102 - 512))
        first = summary["tasks"][0]
        self.assertEqual((first["concrete"], first["wildcard"]), (101, 1))
        self.assertEqual(sorted(first["groups"]), [".", "d0", "lib"])
        self.assertTrue(all(row["in_scope"] for row in first["groups"]["d0"]))
        self.assertFalse(first["groups"]["."][0]["in_scope"])
        self.assertIsNone(boundary.summary({"scope": [], "tasks": tasks[:1]})["tasks"][0]["groups"]["."][0]["in_scope"])
        engine, _ = self.held(lambda b, p, r: new_task(b, p, r, path="tests/*.py"))
        before = files(engine.directory)
        held = status_summary(saved(engine))["scope"]["held"]
        self.assertEqual((held["gaps"], held["suggested_scope"], held["widened"], held["request_id"]),
                         ({"prepare": ["tests/*.py"]}, ["tests/**"], ["tests/**"], request_of(saved(engine))))
        probe = self.engine()
        probe.load_run(engine.run["id"])
        probe.services.planning.returned_proposal()
        self.assertEqual(files(engine.directory), before)


# ---- T6-24 .. T6-31: the four classes of judgment -----------------------------------------------------
class ClassificationTests(HeldCase):
    def test_capacity_flip_on_replay_keeps_the_judged_original(self):
        engine, backend = self.capacity_tight()
        source = saved(engine)["commander_receipts"][DECIDE_KEY]["dispatch_id"]
        self.tighten()
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "无法证明"):
            self.engine(backend).resume(engine.run["id"])  # a replay checks the boundary before capacity
        with self.assertRaisesRegex(HarnessError, "提案保留"):
            self.engine(backend).resume(engine.run["id"], scope=["support.txt"])
        run = saved(engine)
        self.assertEqual((run["stop"]["kind"], run["stop"]["reason"]), ("budget", "proposal_capacity"))
        self.assertEqual((len(backend.calls), run["commander_receipts"][DECIDE_KEY]["dispatch_id"]), (calls, source))
        self.assertEqual((receipt_entry(run)["status"], invalid_entries(run)), ("completed", []))
        self.config.data["runtime"]["context_chars"] += 30000
        after = walk_exit(self, engine.directory, ("budget", "proposal_capacity"), backend=backend,
                          expect_status="completed")
        self.assertEqual((source_of(after, 1), invalid_entries(after)), (source, []))

    def test_replay_failure_does_not_demote_a_judged_original(self):
        engine, backend = self.held()
        hello = engine.worktree / "hello.py"
        before = hello.read_bytes() if hello.exists() else None
        hello.write_text("changed during hold\n")
        with self.assertRaisesRegex(HarnessError, "修改了代码/HEAD"):
            self.engine(backend).resume(engine.run["id"])
        run = saved(engine)
        self.assertEqual((run["stop"]["reason"], receipt_entry(run)["status"]), ("candidate_changed", "completed"))
        self.assertIn("error", receipt_entry(run))
        hello.unlink() if before is None else hello.write_bytes(before)
        source = run["commander_receipts"][DECIDE_KEY]["dispatch_id"]
        after = self.engine(backend).resume(engine.run["id"], scope=["support.txt"])
        self.assertEqual((after["status"], source_of(after, 1), invalid_entries(after)), ("completed", source, []))

    def test_first_plan_prewritten_then_first_declaration_then_reject_then_new_plan(self):
        def change(backend, kwargs, packet, result):
            if trigger_kind(kwargs, packet) == "plan" and "rejected_revision" in packet:
                result["new_tasks"][0]["allowed_paths"] = ["src/**"]
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        crash, die = crash_point(engine)
        original = engine.services.planning.write_plan

        def write_then_die(revision, content):
            original(revision, content)
            die()
        with crash, patch.object(engine.services.planning, "write_plan", side_effect=write_then_die), \
                self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", pause_after_plan=True)
        orphan = engine.directory / "plans" / "revision-0.json"
        prewritten = orphan.read_bytes()
        self.assertEqual(saved(engine)["tasks"], [])
        with self.assertRaisesRegex(HarnessError, "首版计划.*无法证明"):
            self.engine(backend).resume(engine.run["id"], scope=["src/**"])
        held = saved(engine)
        run = self.engine(backend).resume(engine.run["id"], reject_request=rejection(self, request_of(held), "Plan in src"))
        self.assertEqual((run["status"], run["tasks"][0]["allowed_paths"]), ("awaiting_approval", ["src/**"]))
        archived = list((engine.directory / "plans" / "rejected").glob("*.json"))
        self.assertEqual([path.read_bytes() for path in archived], [prewritten])
        self.assertNotEqual(source_of(run, 0), held["commander_receipts"][DECIDE_KEY]["dispatch_id"])

    def test_supplied_plan_recovery_rechecks_the_boundary(self):
        engine = self.engine()
        crash, fired = crash_after_save(engine, lambda run: bool(run["worktree_binding"]) and not run["tasks"])
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        with self.assertRaisesRegex(HarnessError, "供给的首版计划越出"):
            self.engine().resume(engine.run["id"], scope=["src/**"])
        run = saved(engine)
        self.assertEqual((run["stop"]["reason"], run["tasks"], run["stop"]["data"]["suggested_scope"]),
                         ("supplied_plan", [], ["hello.py"]))
        self.assertFalse((engine.directory / "plans").exists())
        walk_exit(self, engine.directory, ("scope_extension", "supplied_plan"), backend=ScriptedBackend(),
                  expect_status="completed")

    def test_evidence_fault_during_judgment_keeps_the_original(self):
        state = {"tampered": None}

        def then(backend, packet, result):
            new_task(backend, packet, result, path="hello.py")
            target = next((engine.directory / "decisions").glob("decision_*.json"))
            state["tampered"] = (target, target.read_bytes())
            target.write_bytes(target.read_bytes().replace(b"Yes", b"No!"))
        engine, backend, reply = self.asked(then)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"], operator_decision=reply)
        run = saved(engine)
        self.assertEqual(run["stop"]["kind"], "evidence")
        self.assertIn(DECIDE_KEY, run["commander_receipts"])
        self.assertEqual(invalid_entries(run), [])
        calls = len(backend.calls)
        target, raw = state["tampered"]
        target.write_bytes(raw)
        source = run["commander_receipts"][DECIDE_KEY]["dispatch_id"]
        after = self.engine(backend).resume(engine.run["id"])
        self.assertEqual((after["status"], source_of(after, 1), invalid_entries(after)), ("completed", source, []))
        self.assertEqual(decides(backend)[calls:], [])  # no redo of the original

    def test_capacity_stopped_proposal_can_be_rejected(self):
        engine, backend = self.capacity_tight()
        self.tighten()
        with self.assertRaisesRegex(HarnessError, "提案保留"):
            self.engine(backend).resume(engine.run["id"], scope=["support.txt"])
        run = saved(engine)
        calls = len(backend.calls)
        after = walk_exit(self, engine.directory, ("budget", "proposal_capacity"), backend=backend, choose=1,
                          fill={"FILE": rejection(self, request_of(run), "Too large; split it")}, expect_status="completed")
        self.assertEqual(len([d for d in after["dispatches"] if "proposal_rejection" in d]), 1)
        later = [c for c in backend.calls[calls:] if c["role"] == "commander"]
        self.assertEqual(later[0]["packet"]["rejected_revision"]["request_id"], request_of(run))


# ---- T6-27, T6-28: suggested scope ------------------------------------------------------------------
class SuggestionTests(RepoCase):
    def test_suggested_entries_are_operator_entries_that_cover_the_gap(self):
        rng = random.Random(20261115)
        segments = ["app", "a", "b", "src", "x.py", "*", "?", "[ab]", "a*", "*.py", "**", "-x", "c d", "it's"]
        checked = 0
        for _ in range(3000):
            gap = "/".join(rng.choice(segments) for _ in range(rng.randint(1, 4)))
            try:
                git.validate_scope_pattern(gap)
            except HarnessError:
                continue
            entry = boundary.suggest_entry(gap)
            self.assertEqual(boundary.operator_entry(entry), entry, gap)
            self.assertTrue(boundary.contains(gap, [entry]), (gap, entry))
            entries, widened = boundary.suggestions({"t": [gap]})
            self.assertTrue(boundary.contains(gap, entries))
            self.assertEqual(widened, [] if entry == gap else [entry])
            checked += 1
        self.assertGreater(checked, 1000)
        self.assertEqual(boundary.suggestions({"t": ["tests/*.py", "tests/a/**", "**/*.py"], "u": ["src/x*.py"]}),
                         (["**"], ["**"]))

    def test_rendered_command_reaches_the_cli_unchanged(self):
        import contextlib
        import io
        from unittest.mock import Mock
        from harness.cli import _main
        entries = ["-file", "a b/c", "it's/**", "tests/**", "**"]
        words = shlex.split(boundary.scope_args(entries))
        engine = Mock()
        engine.resume.return_value = {"id": "run_fixture", "status": "paused", "branch": "b", "worktree": "/tmp/w"}
        with patch("harness.cli.Config.load"), \
                patch("harness.operations.probe_cli", return_value={"path": None, "version": None}), \
                patch("harness.cli.Runtime", return_value=engine), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = _main(["--repo", str(self.repo), "resume", "run_fixture", *words])
        self.assertEqual(code, 0)
        self.assertEqual(engine.resume.call_args.kwargs["scope"], entries)


# ---- T6-34 .. T6-36: which judgment a recovery chooses (confirmation review suggestion 2) -------------------
class JudgmentWindowTests(HeldCase):
    """The ledger fact ``completed`` with the receipt kept tells a replay from a first judgment.

    Each window ends the process at a save; before recovery the grants grow so that the final
    review's capacity fails for the returned proposal (a running condition), which a first
    judgment treats as invalid output and a replay of a judged original as a kept proposal.
    """

    def test_bound_but_unjudged_original_is_judged_as_first(self):
        _, backend = self.capacity_tight(("hello.py", "support.txt"), held=False)
        engine = self.engine(backend)
        crash, fired = crash_after_save(engine, decide_bound)
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan(), scope=["hello.py", "support.txt"])
        run = saved(engine)
        bound = run["commander_receipts"][DECIDE_KEY]["dispatch_id"]
        self.assertNotEqual(receipt_entry(run)["status"], "completed")
        self.tighten()
        calls = len(decides(backend))
        with self.assertRaisesRegex(HarnessError, "连续两次无效"):
            self.engine(backend).resume(engine.run["id"])
        after = saved(engine)
        first = next(d for d in after["dispatches"] if d["id"] == bound)
        redo = [d for d in after["dispatches"] if d.get("retry_of") == bound]
        self.assertEqual((first["status"], len(redo), len(decides(backend)) - calls), ("invalid", 1, 1))
        self.assertNotIn(DECIDE_KEY, after["commander_receipts"])

    def test_saved_verdict_then_crash_is_replayed(self):
        for window in ("hold", "landable"):
            with self.subTest(window=window):
                if window == "landable":
                    self.setUp()
                scope = ["hello.py"] if window == "hold" else ["hello.py", "support.txt"]
                _, backend = self.capacity_tight(scope, held=False)
                engine = self.engine(backend)
                judged = lambda run: (DECIDE_KEY in run["commander_receipts"]
                                      and receipt_entry(run)["status"] == "completed")
                crash, fired = crash_after_save(engine, judged)
                with crash, self.assertRaises(KeyboardInterrupt):
                    engine.start("Implement greet", plan(), scope=scope)
                run = saved(engine)
                self.assertEqual(receipt_entry(run)["status"], "completed")
                self.assertFalse((engine.directory / "plans" / "revision-1.json").exists())
                self.tighten()
                calls = len(backend.calls)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                after = saved(engine)
                expected = ("scope_extension", "revision") if window == "hold" else ("budget", "proposal_capacity")
                self.assertEqual((after["stop"]["kind"], after["stop"]["reason"]), expected)
                self.assertEqual((len(backend.calls), invalid_entries(after), receipt_entry(after)["status"]),
                                 (calls, [], "completed"))

    def test_crash_after_redo_returns_is_first_judgment_without_a_third_call(self):
        for second in ("held", "invalid"):
            with self.subTest(second=second):
                if second == "invalid":
                    self.setUp()
                count = {"n": 0}

                def decide(backend, packet, result):
                    count["n"] += 1
                    new_task(backend, packet, result)
                    if count["n"] == 1 or second == "invalid":
                        result["selected_task"] = "missing"  # a contract defect: invalid output
                backend = blocked_then(decide)
                engine = self.engine(backend)
                redo_bound = lambda run: decide_bound(run) and "retry_of" in receipt_entry(run)
                crash, fired = crash_after_save(engine, redo_bound)
                with crash, self.assertRaises(KeyboardInterrupt):
                    engine.start("Implement greet", plan(), scope=["hello.py"])
                run = saved(engine)
                self.assertNotEqual(receipt_entry(run)["status"], "completed")
                calls = len(decides(backend))
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                after = saved(engine)
                self.assertEqual(len(decides(backend)), calls)  # no third call
                expected = ("scope_extension", "revision") if second == "held" else ("invalid_output", "redo_exhausted")
                self.assertEqual((after["stop"]["kind"], after["stop"]["reason"]), expected)


# ---- T6-19: the save windows of spec-6 3.4 ------------------------------------------------------------
class CrashWindowTests(HeldCase):
    def test_i1_held_first_plan_replays_and_lands_the_held_original(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        crash, fired = crash_after_save(engine, lambda run: DECIDE_KEY in run["commander_receipts"]
                                        and receipt_entry(run)["status"] == "completed")
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", scope=["src/**"], pause_after_plan=True)
        self.assertEqual((saved(engine)["tasks"], (engine.directory / "plans").exists()), ([], False))
        calls, request_id = len(backend.calls), request_of(saved(engine))
        for _ in range(2):
            with self.assertRaisesRegex(HarnessError, "首版计划"):
                self.engine(backend).resume(engine.run["id"])
            run = saved(engine)
            self.assertEqual((run["stop"]["reason"], run["stop"]["source"]["request_id"], len(backend.calls)),
                             ("initial_plan", request_id, calls))
        raw = (engine.directory / "state.json").read_bytes()
        with self.assertRaisesRegex(HarnessError, "首版计划尚未发布"):
            self.engine(backend).resume(engine.run["id"], approve_plan=True)
        self.assertEqual((engine.directory / "state.json").read_bytes(), raw)
        source = saved(engine)["commander_receipts"][DECIDE_KEY]["dispatch_id"]
        run = self.engine(backend).resume(engine.run["id"], scope=["hello.py"])
        self.assertEqual((run["status"], source_of(run, 0), len(backend.calls)), ("awaiting_approval", source, calls))

    def test_i2_widened_then_crash_before_replay_lands_the_same_original(self):
        engine, backend = self.held()
        source = saved(engine)["commander_receipts"][DECIDE_KEY]["dispatch_id"]
        resumed = self.engine(backend)
        crash, fired = crash_after_save(resumed, lambda run: "support.txt" in run["scope"])
        with crash, self.assertRaises(KeyboardInterrupt):
            resumed.resume(engine.run["id"], scope=["support.txt"])
        calls = len(decides(backend))
        run = self.engine(backend).resume(engine.run["id"])
        self.assertEqual((run["status"], source_of(run, 1)), ("completed", source))
        self.assertTrue(after_landing(backend, calls))  # the replay landed before any commander call

    def test_i3_revision_file_written_before_the_save_is_reused(self):
        engine, backend = self.held()
        source = saved(engine)["commander_receipts"][DECIDE_KEY]["dispatch_id"]
        resumed = self.engine(backend)
        crash, wrapped = self.write_plan_then_die(resumed, lambda revision: revision >= 1)
        with crash, wrapped, self.assertRaises(KeyboardInterrupt):
            resumed.resume(engine.run["id"], scope=["support.txt"])
        prewritten = (engine.directory / "plans" / "revision-1.json").read_bytes()
        self.assertNotIn("pending_revision", saved(engine))
        calls = len(decides(backend))
        run = self.engine(backend).resume(engine.run["id"])
        self.assertEqual((run["status"], source_of(run, 1)), ("completed", source))
        self.assertEqual(Path(run["plan_history"][1]["artifact"]).read_bytes(), prewritten)
        self.assertTrue(after_landing(backend, calls))

    def test_i3_first_plan_file_written_before_the_save_is_reused(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        crash, wrapped = self.write_plan_then_die(engine, lambda revision: revision == 0)
        with crash, wrapped, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", scope=["hello.py"], pause_after_plan=True)
        source = saved(engine)["commander_receipts"][DECIDE_KEY]["dispatch_id"]
        calls = len(backend.calls)
        run = self.engine(backend).resume(engine.run["id"])
        self.assertEqual((run["status"], source_of(run, 0), len(backend.calls)), ("awaiting_approval", source, calls))

    def test_i6_answer_scope_and_relaxation_saved_then_crash_before_scheduling(self):
        """Code-6 review suggestion: die right after the one save of the three inputs; a plain resume goes on."""
        def then(backend, packet, result):
            if "support.txt" in packet["scope"]:
                widen("support.txt")(backend, packet, result)
            else:
                decided(result, "implement", selected_task="greet", instruction="Finish greet")
        engine, backend, reply = self.asked(then)
        self.tighten()  # a configuration relaxation adopted by the same resume
        resumed = self.engine(backend)
        committed = lambda run: "support.txt" in run["scope"]
        crash, fired = crash_after_save(resumed, committed)
        calls = len(decides(backend))
        with crash, self.assertRaises(KeyboardInterrupt):
            resumed.resume(engine.run["id"], operator_decision=reply, scope=["support.txt"])
        self.assertTrue(fired["fired"])
        run = saved(engine)
        self.assertEqual(("support.txt" in run["scope"], len(run["operator_decisions"]),
                          self.COMMAND in run["config_snapshot"]["permissions"]["worker_commands"]), (True, 1, True))
        self.assertEqual(len(decides(backend)), calls)  # nothing was scheduled before the death
        after = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(after["status"], "completed")
        packet = decides(backend)[calls]["packet"]
        self.assertEqual((packet["operator_answer"]["request_id"], packet["scope"]),
                         (run["operator_decisions"][0]["request_id"], ["hello.py", "support.txt"]))
        self.assertEqual(after["tasks"][0]["allowed_paths"], ["hello.py", "support.txt"])

    def test_i7_rejection_file_written_then_crash(self):
        engine, backend = self.held(implement_after_rejection())
        request_id = request_of(saved(engine))
        resumed = self.engine(backend)
        crash, die = crash_point(resumed)
        from harness import planning as planning_module
        real_write = planning_module.write_json

        def write_then_die(path, value):
            real_write(path, value)
            if "proposal-rejections" in str(path):
                die()
        with crash, patch.object(planning_module, "write_json", side_effect=write_then_die), \
                self.assertRaises(KeyboardInterrupt):
            resumed.resume(engine.run["id"], reject_request=rejection(self, request_id, "First reason"))
        self.assertEqual(len(list((engine.directory / "proposal-rejections").glob("*.json"))), 1)
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "无法证明"):
            self.engine(backend).resume(engine.run["id"])  # still held, no model call
        self.assertEqual(len(backend.calls), calls)
        calls = len(decides(backend))
        run = self.engine(backend).resume(engine.run["id"], reject_request=rejection(self, request_id, "Second reason"))
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(list((engine.directory / "proposal-rejections").glob("*.json"))), 2)
        self.assertEqual(decides(backend)[calls]["packet"]["rejected_revision"]["reason"], "Second reason")

    def test_i7a_prewritten_plan_archived_then_crash(self):
        engine, backend, orphan = self.prewritten_and_kept()
        prewritten = orphan.read_bytes()
        run = saved(engine)
        source, request_id = run["commander_receipts"][DECIDE_KEY]["dispatch_id"], request_of(run)
        resumed = self.engine(backend)
        crash, die = crash_point(resumed)
        from harness import planning as planning_module
        real_replace = planning_module.os.replace

        def replace_then_die(old, new):
            real_replace(old, new)
            if Path(new).parent.name == "rejected":  # the archive rename, not an atomic file write
                die()
        with crash, patch.object(planning_module.os, "replace", side_effect=replace_then_die), \
                self.assertRaises(KeyboardInterrupt):
            resumed.resume(engine.run["id"], reject_request=rejection(self, request_id))
        self.assertFalse(orphan.exists())
        self.assertIn(DECIDE_KEY, saved(engine)["commander_receipts"])
        with self.assertRaisesRegex(HarnessError, "提案保留"):
            self.engine(backend).resume(engine.run["id"])  # still kept
        calls = len(decides(backend))
        after = self.engine(backend).resume(engine.run["id"], reject_request=rejection(self, request_id))
        self.assertEqual(after["status"], "completed")
        self.assertEqual([p.read_bytes() for p in (engine.directory / "plans" / "rejected").glob("*.json")], [prewritten])
        self.assertIn("proposal_rejection", next(d for d in after["dispatches"] if d["id"] == source))
        self.assertEqual(decides(backend)[calls]["packet"]["rejected_revision"]["request_id"], request_id)
        self.assertNotIn(source, sources(after))

    def test_i7_prime_rejection_saved_then_crash_never_revives(self):
        engine, backend = self.held(implement_after_rejection())
        run = saved(engine)
        source, request_id = run["commander_receipts"][DECIDE_KEY]["dispatch_id"], request_of(run)
        resumed = self.engine(backend)
        crash, fired = crash_after_save(resumed, lambda ledger: any("proposal_rejection" in d for d in ledger["dispatches"]))
        with crash, self.assertRaises(KeyboardInterrupt):
            resumed.resume(engine.run["id"], reject_request=rejection(self, request_id))
        self.assertNotIn(DECIDE_KEY, saved(engine)["commander_receipts"])
        calls = len(decides(backend))
        after = self.engine(backend).resume(engine.run["id"], scope=["support.txt"])
        self.assertEqual(after["status"], "completed")
        self.assertEqual(decides(backend)[calls]["packet"]["rejected_revision"]["dispatch_id"], source)
        self.assertNotIn(source, sources(after))

    def test_i8_partial_widening_keeps_the_rest_held(self):
        def two_gaps(backend, packet, result):
            original = packet["original_tasks"][0]
            task = {**copy.deepcopy(original), "id": "prepare", "allowed_paths": ["hello.py", "a.txt", "b.txt"]}
            revised(result, packet, "Two prerequisites", selected="prepare", new_tasks=[task],
                    updates=[update(original, deps=["prepare"])])
        engine, backend = self.held(two_gaps)
        request_id = request_of(saved(engine))
        self.assertEqual(saved(engine)["stop"]["data"]["gaps"], {"prepare": ["a.txt", "b.txt"]})
        calls = len(backend.calls)
        with self.assertRaisesRegex(HarnessError, "无法证明"):
            self.engine(backend).resume(engine.run["id"], scope=["a.txt"])
        run = saved(engine)
        self.assertEqual((run["stop"]["data"]["gaps"], run["stop"]["source"]["request_id"], len(backend.calls)),
                         ({"prepare": ["b.txt"]}, request_id, calls))
        self.assertEqual(self.engine(backend).resume(engine.run["id"], scope=["b.txt"])["status"], "completed")

    def test_i9a_retained_candidate_published_after_a_crash(self):
        engine, backend = self.held(widen("support.txt"))
        resumed = self.engine(backend)
        crash, fired = crash_after_save(resumed, lambda run: "pending_revision" in run)
        with crash, self.assertRaises(KeyboardInterrupt):
            resumed.resume(engine.run["id"], scope=["support.txt"])
        pending = saved(engine)["pending_revision"]
        self.assertEqual(read_json(Path(pending["artifact"]))["candidate"], "retain")
        run = self.engine(backend).resume(engine.run["id"])
        self.assertEqual((run["status"], run["tasks"][0]["allowed_paths"]), ("completed", ["hello.py", "support.txt"]))
        self.assertFalse((engine.directory / "recovery").exists())

    def archive_window(self, partial):
        engine, backend = self.held()
        resumed = self.engine(backend)
        crash, die = crash_point(resumed)
        from harness import planning as planning_module
        original = planning_module.git.archive_and_reset

        def archive(repo, expected_head, folder, owned_root, *, binding=None):
            if partial:
                folder.mkdir(parents=True, exist_ok=False)
            else:
                original(repo, expected_head, folder, owned_root, binding=binding)
            die()
        with crash, patch.object(planning_module.git, "archive_and_reset", side_effect=archive), \
                self.assertRaises(KeyboardInterrupt):
            resumed.resume(engine.run["id"], scope=["support.txt"])
        self.assertIn("pending_revision", saved(engine))
        calls = len(decides(backend))
        run = self.engine(backend).resume(engine.run["id"])
        self.assertEqual((run["status"], len(run["plan_history"])), ("completed", 2))
        self.assertTrue(after_landing(backend, calls))
        return sorted(path.name for path in (engine.directory / "recovery").iterdir())

    def test_i9b1_archive_returned_then_crash_publishes_without_another_archive(self):
        self.assertEqual(self.archive_window(partial=False), ["revision-1"])

    def test_i9b2_archive_folder_created_reset_unfinished_archives_again(self):
        folders = self.archive_window(partial=True)
        self.assertEqual(len(folders), 2)
        self.assertEqual(folders[0], "revision-1")
        self.assertTrue(folders[1].startswith("revision-1-retry"))

    def test_i9c_boundary_rechecked_before_publication(self):
        engine, backend = self.held()
        resumed = self.engine(backend)
        crash, fired = crash_after_save(resumed, lambda run: "pending_revision" in run)
        with crash, self.assertRaises(KeyboardInterrupt):
            resumed.resume(engine.run["id"], scope=["support.txt"])
        path = engine.directory / "state.json"
        raw = path.read_bytes()
        ledger = read_json(path)
        ledger["scope"] = ["hello.py"]
        write_json(path, ledger)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        run = saved(engine)
        self.assertEqual((run["stop"]["kind"], run["stop"]["reason"]), ("evidence", "runtime_integrity"))
        self.assertIn("pending_revision", run)
        path.write_bytes(raw)
        self.assertEqual(self.engine(backend).resume(engine.run["id"])["status"], "completed")


if __name__ == "__main__":
    unittest.main()
