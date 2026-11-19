"""A review's registration changes the ledger in one step, and one review dispatch registers once."""
import ast
import contextlib
import copy
import inspect
import json
import os
import signal
import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import harness.finalization as finalization
from harness.finalization import FinalAcceptance
from harness.task_lifecycle import TaskLifecycle
from harness.util import HarnessError, read_json, write_json
from test_harness import RepoCase, after_save
import test_review_batches as batches   # by module: a TestCase imported by name is collected again here
from test_runtime_changes import ScriptedBackend, finding, revised, trigger_kind

TASK_KEY, FINAL_KEY = "reviewer:review:greet", "reviewer:final_review:final"


def transition_span(function, marker=None):
    """The first and last line of a ledger transition in ``function``: its only one, or the one whose block
    or enclosing condition holds ``marker``."""
    lines, start = inspect.getsourcelines(function)
    source = textwrap.dedent("".join(lines))
    spans = [(node.lineno + start - 1, node.end_lineno + start - 1)
             for parent in ast.walk(ast.parse(source)) for field in ("body", "orelse")
             for node in (getattr(parent, field, None) if isinstance(getattr(parent, field, None), list) else [])
             if isinstance(node, ast.With) and any(
                 isinstance(item.context_expr, ast.Call) and getattr(item.context_expr.func, "attr", None) == "transition"
                 for item in node.items)
             and (marker is None or marker in ast.get_source_segment(source, node)
                  or isinstance(parent, ast.If) and marker in ast.get_source_segment(source, parent.test))]
    span, = spans
    return span


STOP_FIELDS = ("status", "stop", "store_revision", "quota_wait")


def ledger(run):
    """A ledger as saved, without the fields a stop writes itself."""
    value = json.loads(json.dumps(run))
    for field in STOP_FIELDS:
        value.pop(field, None)
    return value


@contextlib.contextmanager
def sigint_in_transition(function, select, at=None, marker=None, keep=False):
    """Send SIGINT to this process at line event ``at`` of the transition of the call ``select`` picks.

    ``select`` reads the call's locals at the transition and returns the review
    dispatch it registers (any true value when it registers none), or None for
    another call. Event 0 is the with line,
    before the signals are deferred; every later one is inside the block, the
    last on leaving it. Yields the events seen and the selected dispatch; with
    ``keep``, also the in-memory ledger at each event.
    """
    first, last = transition_span(function, marker)
    seen = {"events": [], "ledgers": [], "dispatch": None, "frame": None, "done": False}

    def local(frame, event, arg):
        if event != "line" or seen["done"]:
            return local
        inside = first <= frame.f_lineno <= last
        if seen["frame"] is None and inside:
            seen["dispatch"] = select(frame.f_locals)
            seen["frame"] = frame if seen["dispatch"] else None
        if frame is not seen["frame"]:
            return local
        if not inside:
            seen["done"] = True
            return local
        seen["events"].append(frame.f_lineno)
        if keep:
            seen["ledgers"].append(ledger(frame.f_locals["self"].context.run))
        if at is not None and len(seen["events"]) == at + 1:
            os.kill(os.getpid(), signal.SIGINT)
        return local

    def calls(frame, event, arg):
        return local if frame.f_code is function.__code__ else None
    previous = sys.gettrace()
    sys.settrace(calls)
    try:
        yield seen
    finally:
        sys.settrace(previous)


@contextlib.contextmanager
def sigint_at_line(function, text):
    """Send SIGINT to this process once, at the first line event of ``function`` on the first source line holding ``text``."""
    lines, start = inspect.getsourcelines(function)
    target = start + next(index for index, line in enumerate(lines) if text in line)
    fired = {"fired": False}

    def local(frame, event, arg):
        if event == "line" and frame.f_lineno == target and not fired["fired"]:
            fired["fired"] = True
            os.kill(os.getpid(), signal.SIGINT)
        return local

    def calls(frame, event, arg):
        return local if frame.f_code is function.__code__ else None
    previous = sys.gettrace()
    sys.settrace(calls)
    try:
        yield fired
    finally:
        sys.settrace(previous)


def interrupt():
    raise KeyboardInterrupt()


def registrations(saved):
    """Every review dispatch each history registers, records and approved batches alike, in order."""
    histories = [*saved["review_history"].values(), saved["final_history"]]
    return [[item["review_source"]["dispatch_id"] for record in history
             for item in (*record.get("approved_batches", []), record) if isinstance(item.get("review_source"), dict)]
            for history in histories]


def signature(backend):
    return [(call["role"], call["kind"], call["task_id"], len(call["packet"].get("required_findings", [])))
            for call in backend.calls]


def final_rejections(count, findings, task_change=None):
    """The first ``count`` whole-run reviews reject with ``findings`` new findings each; a correction task follows."""
    def change(backend, kwargs, packet, result):
        finals = len(batches.calls(backend, kind="final_review"))
        if kwargs["kind"] == "final_review" and finals <= count:
            result.update(verdict="request_changes",
                          findings=[{**finding(), "id": f"g{finals}-{index}"} for index in range(findings)])
        elif trigger_kind(kwargs, packet) == "final":
            correction = copy.deepcopy(packet["original_tasks"][0])
            correction.update(id=f"correct{finals}", depends_on=["greet"])
            revised(result, packet, "Resolve final findings", new_tasks=[correction])
        elif kwargs["role"] == "worker" and kwargs["task"]["id"].startswith("correct"):
            batches.touch(kwargs, backend)
        elif task_change:
            task_change(backend, kwargs, packet, result)
    return ScriptedBackend(change)


def final_120():
    """Four whole-run rejections of 30 findings: the fifth candidate lacks 120, two batches with keys f1..f60 each."""
    return final_rejections(4, 30)


class TransitionCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.config.data["runtime"].update(max_attempts=30)

    def assert_stop(self, saved, kind, reason):
        self.assertEqual(batches.stop_of(saved), (kind, reason))


class SignalInTransitionTests(TransitionCase):
    """Layer one: a signal before a transition saves the whole old ledger, one inside it the whole new one."""

    def sweep(self, backend_of, function, key, select, old, new, after=None, marker=None):
        """Interrupt at every line event of the selected transition, then resume to the uninterrupted end."""
        backend = backend_of()
        with sigint_in_transition(function, select, marker=marker) as seen:
            baseline = self.engine(backend).start("Implement greet", batches.plan())
        self.assertEqual(baseline["status"], "completed")
        self.assertEqual(seen["events"][0], transition_span(function, marker)[0])
        self.assertGreaterEqual(len(seen["events"]), 4)
        expected = signature(backend)
        if after:
            after(baseline, backend)
        for at in range(len(seen["events"])):
            with self.subTest(event=at, line=seen["events"][at]):
                self.setUp()
                backend = backend_of()
                engine = self.engine(backend)
                with sigint_in_transition(function, select, at, marker) as fired, self.assertRaises(KeyboardInterrupt):
                    engine.start("Implement greet", batches.plan())
                self.assertGreater(len(fired["events"]), at)
                saved = batches.state(engine)
                self.assertEqual(saved["status"], "interrupted")
                dispatch, registered = fired["dispatch"], sum(registrations(saved), [])
                if at == 0:
                    self.assertEqual(saved["review_receipts"][key]["dispatch_id"], dispatch)
                    self.assertNotIn(dispatch, registered)
                    old(saved)
                else:
                    self.assertNotIn(key, saved["review_receipts"])
                    self.assertEqual(registered.count(dispatch), 1)
                    new(saved, dispatch)
                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(signature(backend), expected)
                self.assertTrue(all(len(set(ids)) == len(ids) for ids in registrations(result)))
                if after:
                    after(result, backend)

    def test_task_next_batch(self):
        def old(saved):
            self.assertEqual(len(saved["review_history"]["greet"]), 4)
            self.assertEqual(len(saved["pending_task"]["required_findings"]), 60)

        def new(saved, dispatch):
            self.assertEqual(saved["review_history"]["greet"][-1]["dispatch_id"], dispatch)
            self.assertEqual((saved["pending_task"]["stage"], batches.labels(saved["pending_task"]["required_findings"])),
                             ("reviewing", ["r3-19"]))
        self.sweep(lambda: batches.scripted([20, 20, 20, 1]), TaskLifecycle._finish_review, TASK_KEY,
                   lambda local: local["outcome"] == "next_batch" and local["record"]["dispatch_id"], old, new)

    def test_task_approval(self):
        def new(saved, dispatch):
            self.assertNotIn("pending_task", saved)
            self.assertEqual(saved["pending_acceptance"]["review_source"]["dispatch_id"], dispatch)
        self.sweep(ScriptedBackend, TaskLifecycle._finish_review, TASK_KEY,
                   lambda local: local["outcome"] == "approved" and local["record"]["dispatch_id"],
                   lambda saved: self.assertNotIn("pending_acceptance", saved), new)

    def test_task_rejection(self):
        def new(saved, dispatch):
            self.assertNotIn("pending_task", saved)
            self.assertEqual(saved["feedback"]["artifact"], saved["review_history"]["greet"][-1]["artifact"])
        self.sweep(lambda: batches.scripted([1]), TaskLifecycle._finish_review, TASK_KEY,
                   lambda local: local["outcome"] == "rejected" and local["record"]["dispatch_id"],
                   lambda saved: self.assertEqual(saved["pending_task"]["stage"], "reviewing"), new)

    def test_final_intermediate_batch_of_one_hundred_twenty(self):
        """The review's case: both batches have keys f1..f60; after any interruption the second is reviewed."""
        def old(saved):
            record = saved["final_history"][-1]
            self.assertEqual((record["status"], record["approved_batches"], len(record["required_findings"])),
                             ("running", [], 60))

        def new(saved, dispatch):
            record = saved["final_history"][-1]
            self.assertEqual((record["status"], record["phase"]), ("running", "reviewing"))
            self.assertEqual([batch["dispatch_id"] for batch in record["approved_batches"]], [dispatch])
            first = {(row["review_dispatch_id"], row["finding_index"]) for row in record["approved_batches"][0]["required_findings"]}
            second = {(row["review_dispatch_id"], row["finding_index"]) for row in record["required_findings"]}
            self.assertEqual((len(first), len(second), first & second), (60, 60, set()))

        def after(result, backend):
            finals = batches.calls(backend, kind="final_review")
            self.assertEqual(batches.sizes(finals), [0, 30, 60, 60, 60, 60])
            reviewed = [{(row["review_dispatch_id"], row["finding_index"]) for row in call["packet"]["required_findings"]}
                        for call in finals[-2:]]
            self.assertEqual(len(reviewed[0] | reviewed[1]), 120)
            last = result["final_history"][-1]
            self.assertNotEqual(last["approved_batches"][0]["dispatch_id"], last["review_source"]["dispatch_id"])
        self.sweep(final_120, FinalAcceptance.final_review, FINAL_KEY,
                   lambda local: local.get("remaining") and local["source"]["dispatch_id"], old, new, after,
                   marker="review_receipts")

    def test_final_last_batch_approval(self):
        def new(saved, dispatch):
            record = saved["final_history"][-1]
            self.assertEqual((record["status"], record["review_source"]["dispatch_id"], saved["feedback"]),
                             ("approved", dispatch, None))
        self.sweep(ScriptedBackend, FinalAcceptance.final_review, FINAL_KEY,
                   lambda local: local.get("approved") and not local.get("remaining") and local["source"]["dispatch_id"],
                   lambda saved: self.assertEqual(saved["final_history"][-1]["status"], "running"), new,
                   marker="review_receipts")

    def test_final_last_batch_rejection(self):
        def new(saved, dispatch):
            record = saved["final_history"][-1]
            self.assertEqual((record["status"], record["review_source"]["dispatch_id"]), ("rejected", dispatch))
            self.assertEqual(saved["feedback"]["final_review"]["review_source"]["dispatch_id"], dispatch)
        self.sweep(lambda: final_rejections(1, 1), FinalAcceptance.final_review, FINAL_KEY,
                   lambda local: "approved" in local and not local["approved"] and local["source"]["dispatch_id"],
                   lambda saved: self.assertEqual(saved["final_history"][-1]["status"], "running"), new,
                   marker="review_receipts")


class SingleRegistrationTests(TransitionCase):
    """Layer two: a ledger that registers one review dispatch twice never loads, and a registered one is never replayed."""

    def assert_refused_at_load(self, error, after, damaged):
        """A ledger that fails the load checks is never run, and the stop is not written into it."""
        self.assertEqual((error.stop["kind"], error.stop["reason"]), ("evidence", "load_integrity"))
        self.assertIn("登记了不止一次", str(error))
        self.assertEqual(after, damaged)

    def tamper_and_resume(self, engine, backend, saved, damage):
        damaged = copy.deepcopy(saved)
        damage(damaged)
        write_json(engine.directory / "state.json", damaged)
        before = len(backend.calls)
        with self.assertRaises(HarnessError) as caught:
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(len(backend.calls), before)
        return caught.exception, batches.state(engine), damaged

    def test_task_history(self):
        """Two batches of 60 on the fifth candidate: the first batch's receipt would pass the second's key contract."""
        backend = batches.scripted([30, 30, 30, 30])
        engine = self.engine(backend)
        with batches.interrupt_at_event(engine, "review_batch_approved"), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", batches.plan())
        saved = batches.state(engine)
        history = saved["review_history"]["greet"]
        self.assertEqual((len(history), len(saved["pending_task"]["required_findings"])), (5, 60))
        with self.subTest("a registered receipt is not replayed into the next batch"):
            error, after, damaged = self.tamper_and_resume(engine, backend, saved, lambda run: run["review_receipts"].update(
                {TASK_KEY: copy.deepcopy(run["review_history"]["greet"][-1]["review_source"])}))
            self.assert_stop(after, "evidence", "runtime_integrity")
            self.assertIn("已登记", str(error))
            self.assertEqual(after["review_receipts"], damaged["review_receipts"])
            self.assertEqual((after["review_history"], after["pending_task"]), (damaged["review_history"], damaged["pending_task"]))
        with self.subTest("two records"):
            error, after, damaged = self.tamper_and_resume(engine, backend, saved, lambda run: run["review_history"]["greet"].append(
                copy.deepcopy(run["review_history"]["greet"][-1])))
            self.assert_refused_at_load(error, after, damaged)

    def test_final_history(self):
        backend = final_120()
        engine = self.engine(backend)
        with batches.interrupt_at_event(engine, "final_review_batch_approved"), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", batches.plan())
        saved = batches.state(engine)
        batch = saved["final_history"][-1]["approved_batches"][0]

        def completed_by_the_batch(run):
            """The state the review reached: the first batch's original registered again as the attempt's approval."""
            run["final_history"][-1].update(review=copy.deepcopy(batch["review"]), review_source=copy.deepcopy(batch["review_source"]),
                                            phase="finished", status="approved")
        with self.subTest("a registered receipt is not replayed into the next batch"):
            error, after, damaged = self.tamper_and_resume(engine, backend, saved, lambda run: run["review_receipts"].update(
                {FINAL_KEY: copy.deepcopy(batch["review_source"])}))
            self.assert_stop(after, "evidence", "runtime_integrity")
            self.assertIn("已登记", str(error))
            self.assertEqual(after["review_receipts"], damaged["review_receipts"])
            self.assertEqual(after["final_history"], damaged["final_history"])
            self.assertEqual(read_json(Path(after["final_history"][-1]["artifact"]))["approved_batches"], [batch])
        for name, damage in (("a record and an approved batch", completed_by_the_batch),
                             ("two approved batches", lambda run: run["final_history"][-1]["approved_batches"].append(copy.deepcopy(batch)))):
            with self.subTest(name):
                self.assert_refused_at_load(*self.tamper_and_resume(engine, backend, saved, damage))

    def test_legal_histories_still_load(self):
        """A kept original of a changed candidate, a redone review and an approval voided at acceptance register once each."""
        def interrupted_then_changed(engine, backend):
            original = TaskLifecycle._finish_review

            def interrupt(service, *args):
                if len(batches.calls(backend, kind="review")) == 2:
                    raise KeyboardInterrupt()
                return original(service, *args)
            with patch.object(TaskLifecycle, "_finish_review", autospec=True, side_effect=interrupt), \
                    self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", batches.plan())
            with (engine.worktree / "hello.py").open("a", encoding="utf-8") as stream:
                stream.write("# changed with a returned review\n")
            return self.engine(backend).resume(engine.run["id"])

        def redone(engine, backend):
            return engine.start("Implement greet", batches.plan())

        def voided(engine, backend):
            def mutate():
                with (engine.worktree / "hello.py").open("a", encoding="utf-8") as stream:
                    stream.write("# changed after review\n")
            change, _ = after_save(engine, lambda run: "pending_acceptance" in run, mutate)
            with change:
                return engine.start("Implement greet", batches.plan())

        def invalid_first(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(batches.calls(backend, kind="review")) == 1:
                result["summary"] = "x" * 4001
        cases = (("kept original", batches.scripted([1]), interrupted_then_changed, "invalidated"),
                 ("redone review", ScriptedBackend(invalid_first), redone, None),
                 ("voided approval", ScriptedBackend(), voided, "invalidated"))
        for name, backend, run, status in cases:
            with self.subTest(name):
                self.setUp()
                engine = self.engine(backend)
                result = run(engine, backend)
                self.assertEqual(result["status"], "completed")
                history = result["review_history"]["greet"]
                if status:
                    self.assertIn(status, [record.get("status") for record in history])
                else:
                    redo = [entry for entry in result["dispatches"] if entry.get("retry_of")]
                    self.assertEqual(len(redo), 1)
                    self.assertEqual([record["dispatch_id"] for record in history], [redo[0]["id"]])
                self.assertTrue(all(len(set(ids)) == len(ids) for ids in registrations(result)))
                engine.services.lifecycle.load_checks()


class ApprovedBatchProgressTests(TransitionCase):
    """An approved batch that closed less than its rows is never registered; it is not dispatched until the budget ends."""

    def test_an_approval_that_leaves_its_own_row_open_stops_before_registration(self):
        def leave_open(backend, kwargs, packet, result):
            rows = result.get("finding_dispositions") or {}
            if "f1" in rows:
                rows["f1"].update(status="open", evidence=["not resolved"])
        for side in ("task", "final"):
            with self.subTest(side):
                self.setUp()
                self.config.data["runtime"]["max_agent_dispatches"] = 12
                if side == "task":
                    backend, key = batches.scripted([1], lambda backend, kwargs, packet, result, number: leave_open(
                        backend, kwargs, packet, result)), TASK_KEY
                    gaps = patch("harness.task_lifecycle.disposition_gaps", return_value=[])
                else:
                    backend = final_rejections(1, 1, None)
                    original = backend.change

                    def change(backend, kwargs, packet, result, original=original):
                        original(backend, kwargs, packet, result)
                        if kwargs["kind"] == "final_review" and len(batches.calls(backend, kind="final_review")) == 2:
                            leave_open(backend, kwargs, packet, result)
                    backend.change, key = change, FINAL_KEY
                    # The approval contract of the traversal is bypassed too, as the gap check of registration is.
                    gaps = contextlib.ExitStack()
                    for target in ("harness.finalization.disposition_gaps", "harness.review_progress.disposition_gaps"):
                        gaps.enter_context(patch(target, return_value=[]))
                engine = self.engine(backend)
                with gaps, self.assertRaises(HarnessError) as caught:
                    engine.start("Implement greet", batches.plan())
                self.assertIn("没有关闭", str(caught.exception))
                saved = batches.state(engine)
                self.assert_stop(saved, "evidence", "runtime_integrity")
                kind = "review" if side == "task" else "final_review"
                self.assertEqual(batches.sizes(batches.calls(backend, kind=kind)), [0, 1])
                receipt = saved["review_receipts"][key]
                self.assertIn("artifact_digest", receipt)
                self.assertNotIn(receipt["dispatch_id"], sum(registrations(saved), []))
                if side == "task":
                    self.assertEqual((saved["pending_task"]["stage"], len(saved["pending_task"]["required_findings"])), ("reviewing", 1))
                    self.assertEqual(saved["accepted"], {})
                else:
                    record = saved["final_history"][-1]
                    self.assertEqual((record["status"], record["approved_batches"], len(record["required_findings"])),
                                     ("running", [], 1))
                    self.assertEqual(read_json(Path(record["artifact"]))["approved_batches"], [])


def evidence_plan():
    """The example plan with a required evidence file that only workers after the first write."""
    plan = batches.plan()
    plan["tasks"][0].update(allowed_paths=["hello.py", "proof.txt"], evidence_requirements=[
        {"id": "proof", "acceptance_index": 0, "kind": "static", "path": "proof.txt"}])
    return plan


class LedgerSweepCase(TransitionCase):
    def sweep_ledger(self, backend_of, function, marker, run, old_stop=None):
        """Interrupt at every line event of one transition: the stop saves the whole ledger of the with line, or the
        whole ledger at the block's end, and resuming makes the uninterrupted calls.

        ``run(engine, backend)`` drives the run through the transition. ``old_stop`` is where a resume from the
        old ledger stops instead, when the step before the transition already changed the worktree.
        """
        backend = backend_of()
        with sigint_in_transition(function, lambda local: True, marker=marker) as seen:
            baseline = run(self.engine(backend), backend)
        self.assertEqual(baseline["status"], "completed")
        self.assertGreaterEqual(len(seen["events"]), 4)
        expected = signature(backend)
        for at in range(len(seen["events"])):
            with self.subTest(event=at, line=seen["events"][at]):
                self.setUp()
                backend = backend_of()
                engine = self.engine(backend)
                with sigint_in_transition(function, lambda local: True, at, marker, keep=True) as fired, \
                        self.assertRaises(KeyboardInterrupt):
                    run(engine, backend)
                saved = batches.state(engine)
                self.assertEqual(saved["status"], "interrupted")
                # Before the block the signal stops the trace at once; inside it, the block runs to its end.
                self.assertEqual(len(fired["ledgers"]), 1 if at == 0 else len(seen["events"]))
                self.assertTrue(at == 0 or fired["ledgers"][0] != fired["ledgers"][-1])
                expected_ledger, saved_ledger = fired["ledgers"][0 if at == 0 else -1], ledger(saved)
                self.assertEqual([key for key in sorted({*expected_ledger, *saved_ledger})
                                  if expected_ledger.get(key) != saved_ledger.get(key)], [])
                if at == 0 and old_stop:
                    with self.assertRaises(HarnessError) as caught:
                        self.engine(backend).resume(engine.run["id"])
                    self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), old_stop)
                    continue
                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(signature(backend), expected)
                self.assertTrue(all(len(set(ids)) == len(ids) for ids in registrations(result)))


class CandidateChangeTests(LedgerSweepCase):
    """A changed candidate keeps the returned original, ends the gate and consumes the receipt in one step:
    the original is never saved registered beside the receipt that would replay it."""

    def returned_then_changed(self, engine):
        """The task review returns and the run stops before registering it; the candidate then changes."""
        window, _ = after_save(engine, lambda run: "artifact_digest" in run["review_receipts"].get(TASK_KEY, {}), interrupt)
        with window, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", batches.plan())
        path = engine.worktree / "hello.py"
        original = path.read_bytes()
        path.write_bytes(original + b"# changed with a returned review\n")
        return original

    def test_review_cases_resume_to_the_uninterrupted_end(self):
        """The review's case: a signal after the original is kept, before the receipt is consumed, then the candidate
        changes back. An invalid original is reviewed afresh, a valid approval registers once, as after an uninterrupted change."""
        def invalid(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and len(batches.calls(backend, kind="review")) == 1:
                result.pop("summary")
        points = (None, 'self.context.run.pop("pending_task")', 'self.context.run["review_receipts"].pop(key, None)')
        for name, change in (("invalid original", invalid), ("valid approval", None)):
            ends = {}
            for point in points:
                with self.subTest(name, point=point):
                    self.setUp()
                    backend = ScriptedBackend(change)
                    engine = self.engine(backend)
                    original = self.returned_then_changed(engine)
                    dispatch = batches.state(engine)["review_receipts"][TASK_KEY]["dispatch_id"]
                    resumed = self.engine(backend)
                    # None: the change runs uninterrupted and the run stops only after its save.
                    window = (after_save(resumed, lambda run: "pending_task" not in run, interrupt)[0] if point is None
                              else sigint_at_line(TaskLifecycle.continue_task, point))
                    with window, self.assertRaises(KeyboardInterrupt):
                        resumed.resume(engine.run["id"])
                    saved = batches.state(engine)
                    kept, = saved["review_history"]["greet"]
                    self.assertEqual((kept["status"], kept["dispatch_id"], "review" in kept),
                                     ("invalidated", dispatch, change is None))
                    self.assertNotIn("pending_task", saved)
                    self.assertNotIn(TASK_KEY, saved["review_receipts"])
                    self.assertIn("recovery", saved["feedback"])
                    (engine.worktree / "hello.py").write_bytes(original)
                    before = len(backend.calls)
                    result = self.engine(backend).resume(engine.run["id"])
                    self.assertEqual(result["status"], "completed")
                    self.assertTrue(all(len(set(ids)) == len(ids) for ids in registrations(result)))
                    history = result["review_history"]["greet"]
                    self.assertEqual(([record["dispatch_id"] for record in history].count(dispatch), len(history)), (1, 2))
                    self.assertEqual(history[-1]["review"]["verdict"], "approve")
                    ends[point] = (signature(backend)[before:], [record.get("status") for record in history])
                    self.assertEqual(ends[point], ends[None])

    def test_signal_in_the_transition(self):
        def run(engine, backend):
            self.returned_then_changed(engine)
            return self.engine(backend).resume(engine.run["id"])
        self.sweep_ledger(ScriptedBackend, TaskLifecycle.continue_task, "review_receipts", run)


class LeaveTransitionTests(LedgerSweepCase):
    """Leaving a whole-run attempt concludes it and consumes its receipt in one step (spec-1 rule 15)."""

    def test_signal_in_the_transition(self):
        def run(engine, backend):
            # The first whole-run review returns and the run stops before judging it; the candidate then changes.
            window, _ = after_save(engine, lambda run: "artifact_digest" in run["review_receipts"].get(FINAL_KEY, {}),
                                   interrupt)
            with window, self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", batches.plan())
            os.chmod(engine.worktree / "hello.py", 0o664)
            return self.engine(backend).resume(engine.run["id"])
        self.sweep_ledger(ScriptedBackend, FinalAcceptance.leave_attempt, None, run)

    def test_signal_in_the_transition_of_a_written_leave(self):
        """Review 1d2: a signal after the leave wrote its record file; back on the reviewed candidate the same
        transition publishes that leave, and a signal at any line of it saves the whole old or new ledger."""
        def run(engine, backend):
            window, _ = after_save(engine, lambda run: "artifact_digest" in run["review_receipts"].get(FINAL_KEY, {}),
                                   interrupt)
            with window, self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", batches.plan())
            hello = engine.worktree / "hello.py"
            os.chmod(hello, 0o664)
            written = finalization.write_json

            def signal_after_the_leave(path, value):
                written(path, value)
                if Path(path).name == "final.json" and value.get("status") == "invalidated":
                    os.kill(os.getpid(), signal.SIGINT)
            with patch.object(finalization, "write_json", side_effect=signal_after_the_leave), \
                    self.assertRaises(KeyboardInterrupt):
                self.engine(backend).resume(engine.run["id"])
            # Off its snapshot the leave never reached its transition: the one swept is the written leave's, on A.
            self.assertEqual([record["status"] for record in batches.state(engine)["final_history"]], ["running"])
            os.chmod(hello, 0o644)
            result = self.engine(backend).resume(engine.run["id"])
            self.assertEqual([record["status"] for record in result["final_history"]], ["invalidated", "approved"])
            return result
        self.sweep_ledger(ScriptedBackend, FinalAcceptance.leave_attempt, None, run)

    def test_signal_in_the_transition_of_a_written_leave_before_a_decide(self):
        """Review 1d2 r2: back on C, rejected before, the scheduler publishes the leave written on B before it derives
        C's decide; a signal at any line of that transition saves the whole old or new ledger."""
        from test_final_attempt_switch import c_then_a, rejected_on_c, written_on_b

        def run(engine, backend):
            rejected_on_c(self, engine, lambda: engine.start("Implement greet", batches.plan()))
            hello, _ = written_on_b(self, engine, backend)
            os.chmod(hello, 0o644)
            resumed = self.engine(backend)
            # The swept transition is the one the scheduler reaches before any model call on C.
            before, step = len(backend.calls), resumed.services.scheduler.next_step

            def derived(*args, **kwargs):
                result = step(*args, **kwargs)
                if not seen["calls"]:
                    seen["calls"].append(len(backend.calls) - before)
                    seen["history"] = [record["status"] for record in resumed.run["final_history"]]
                return result
            seen = {"calls": []}
            with patch.object(resumed.services.scheduler, "next_step", side_effect=derived):
                result = resumed.resume(engine.run["id"])
            self.assertEqual((seen["calls"], seen["history"]), ([0], ["rejected", "invalid_rejected"]))
            self.assertEqual([record["status"] for record in result["final_history"]],
                             ["rejected", "invalid_rejected", "approved"])
            return result
        self.sweep_ledger(lambda: c_then_a("request_changes"), FinalAcceptance.leave_attempt, None, run)


class ExistingTransitionTests(LedgerSweepCase):
    """The other migrations that were several steps: each one's assignments between two saves are one transition."""

    def test_preflight_failure(self):
        def proof(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and len(batches.calls(backend, role="worker")) > 1:
                (Path(kwargs["cwd"]) / "proof.txt").write_text("proof\n", encoding="utf-8")
        self.sweep_ledger(lambda: ScriptedBackend(proof), TaskLifecycle.continue_task, 'preflight["passed"]',
                          lambda engine, backend: engine.start("Implement greet", evidence_plan()))

    def test_verification_failure(self):
        def broken(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and len(batches.calls(backend, role="worker")) == 1:
                (Path(kwargs["cwd"]) / "hello.py").write_text("def greet(name):\n    return name\n", encoding="utf-8")
        self.sweep_ledger(lambda: ScriptedBackend(broken), TaskLifecycle.continue_task, "checks_passed",
                          lambda engine, backend: engine.start("Implement greet", batches.plan()))

    def test_acceptance_invalidation(self):
        def run(engine, backend):
            def mutate():
                with (engine.worktree / "hello.py").open("a", encoding="utf-8") as stream:
                    stream.write("# changed after review\n")
            change, _ = after_save(engine, lambda run: "pending_acceptance" in run, mutate)
            with change:
                return engine.start("Implement greet", batches.plan())
        self.sweep_ledger(ScriptedBackend, TaskLifecycle.accept_pending, None, run)

    def test_recorded_acceptance(self):
        """The commit precedes the transition: from the old ledger a resume stops at the moved HEAD, as it always has."""
        self.sweep_ledger(ScriptedBackend, TaskLifecycle._record_accepted, None,
                          lambda engine, backend: engine.start("Implement greet", batches.plan()),
                          old_stop=("workspace_mismatch", "head_changed"))

    def test_final_verification(self):
        self.sweep_ledger(ScriptedBackend, FinalAcceptance.final_review, "verifying",
                          lambda engine, backend: engine.start("Implement greet", batches.plan()))


if __name__ == "__main__":
    unittest.main()
