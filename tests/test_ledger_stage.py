"""`ledger_stage`: the step a planning or running ledger last recorded, read from the ledger alone."""
import copy
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import observe
from harness.cli import status_summary
from harness.diagnostics import ledger_stage
from harness.util import read_json
from test_harness import ROOT, RepoCase
# By module: a TestCase imported by name is collected again here.
import test_final_attempt_switch as switch
import test_review_batches as batches
from test_runtime_changes import ScriptedBackend, finding


def stage(step, task_id=None, attempt=None, role=None, kind=None, dispatch_id=None, process_state=None):
    return {"step": step, "task_id": task_id, "attempt": attempt, "role": role, "kind": kind,
            "dispatch_id": dispatch_id, "process_state": process_state}


def entry(ident, role="worker", kind="implement", task_id="greet", status="running", process_state="running", **fields):
    value = {"id": ident, "role": role, "kind": kind, "task_id": task_id, "status": status, "pgid": None,
             "process_state": process_state, "invoked": False}
    value.update(fields)
    if kind is None:
        value.pop("kind")  # a verification entry has no kind
    return value


def run(status="running", **fields):
    value = {"id": "run_stage", "status": status, "attempts": {"greet": 2}, "accepted": {}, "dispatches": [
        entry("d-done", status="completed", process_state="exited", worker_receipt={"attempt": 1})]}
    value.update(fields)
    return value


WAIT = {"dispatch_id": "d-quota", "role": "reviewer", "window": "session", "resets_at": 1, "until": 2,
        "recorded_at": 0, "limit_seconds": 60, "sample": {}}
REVIEWING = {"task_id": "greet", "attempt": 2, "stage": "reviewing"}


def timeline(engine, backend):
    """In order, the ledger as state.json holds it right after every save ("save") and while every backend call
    runs (the call's kind), each with the stage status shows for it."""
    seen, save = [], engine.context.save

    def read(when):
        ledger = read_json(engine.directory / "state.json")
        seen.append((when, ledger, status_summary(ledger)["ledger_stage"]))

    def saved(*args, **kwargs):
        save(*args, **kwargs)
        read("save")
    backend.before_result = lambda backend, kwargs, packet: read(kwargs["kind"])
    return patch.object(engine.context, "save", side_effect=saved), seen


def until_prepared(seen, start):
    """The stages saved from ``start`` until the save that prepares the next call (its entry is running), each once."""
    shown = []
    for when, ledger, value in seen[start:]:
        if when != "save" or any(entry.get("status") == "running" for entry in ledger["dispatches"]):
            break
        if not shown or shown[-1] != value:
            shown.append(value)
    return shown


def first(seen, when, shape):
    """The index of the first save or call ``when`` whose ledger has ``shape``."""
    return next(index for index, (kind, ledger, _) in enumerate(seen) if kind == when and shape(ledger))


class LedgerStageTests(unittest.TestCase):
    def test_each_recorded_step(self):
        everything = dict(pending_task=REVIEWING, quota_wait=WAIT,
                          dispatches=[entry("d-review", role="reviewer", kind="review")])
        for status in ("blocked", "interrupted", "paused", "awaiting_approval", "completed", "abandoned"):
            with self.subTest(status=status):
                self.assertIsNone(ledger_stage(run(status, **everything)))
        final = [{"attempt": 1, "status": "rejected", "phase": "finished"}]
        rows = {
            "nothing recorded": (run(), stage("between_steps")),
            "planning": (run("planning", dispatches=[]), stage("between_steps")),
            "commander planning": (run("planning", dispatches=[entry("d-plan", role="commander", kind="plan", task_id="run",
                                                                     process_state="starting")]),
                                   stage("dispatch", "run", None, "commander", "plan", "d-plan", "starting")),
            "quota wait only": (run(quota_wait=WAIT), stage("quota_wait", role="reviewer", dispatch_id="d-quota")),
            "prepared worker": (run(dispatches=[entry("d-work", process_state="prepared", worker_receipt={"attempt": 2})]),
                                stage("dispatch", "greet", 2, "worker", "implement", "d-work", "prepared")),
            "task verification": (run(pending_task={**REVIEWING, "stage": "verifying"}),
                                  stage("task_verification", "greet", 2)),
            "task review": (run(pending_task=REVIEWING), stage("task_review", "greet", 2)),
            "running task review call": (run(pending_task=REVIEWING, dispatches=[entry("d-review", role="reviewer", kind="review")]),
                                         stage("dispatch", "greet", 2, "reviewer", "review", "d-review", "running")),
            "running verification command": (run(pending_task={**REVIEWING, "stage": "verifying"},
                                                 dispatches=[entry("d-check", role="verification", kind=None,
                                                                   process_state="starting")]),
                                             stage("dispatch", "greet", 2, "verification", None, "d-check", "starting")),
            "acceptance": (run(pending_acceptance={"task_id": "greet", "attempt": 2}), stage("task_acceptance", "greet", 2)),
            "after acceptance": (run(pending_post_accept={"payload": {"task_id": "greet", "commit": "abc"}}),
                                 stage("task_acceptance", "greet")),
            "final verification": (run(final_history=[*final, {"attempt": 2, "status": "running", "phase": "verifying"}]),
                                   stage("final_verification", "final", 2)),
            "final review call": (run(final_history=[*final, {"attempt": 2, "status": "running", "phase": "reviewing"}],
                                      dispatches=[entry("d-final", role="reviewer", kind="final_review", task_id="final")]),
                                  stage("dispatch", "final", 2, "reviewer", "final_review", "d-final", "running")),
            "finished final attempt": (run(final_history=[{"attempt": 1, "status": "approved", "phase": "finished"}]),
                                       stage("between_steps")),
            # The task stage comes first; the whole-run record is read only when no task stage is recorded.
            "task stage over final": (run(pending_acceptance={"task_id": "greet", "attempt": 2},
                                          final_history=[{"attempt": 1, "status": "running", "phase": "reviewing"}]),
                                      stage("task_acceptance", "greet", 2)),
            "last running entry": (run(pending_task=REVIEWING, dispatches=[
                entry("d-old", role="worker", worker_receipt={"attempt": 1}),
                entry("d-new", role="reviewer", kind="review", process_state="exited"),
                entry("d-done-2", role="reviewer", kind="review", status="completed")]),
                stage("dispatch", "greet", 2, "reviewer", "review", "d-new", "exited")),
            # A worker entry is named by its own receipt only, never by the stage it shares a task with.
            "worker without receipt": (run(pending_acceptance={"task_id": "greet", "attempt": 2},
                                           dispatches=[entry("d-work")]),
                                       stage("dispatch", "greet", None, "worker", "implement", "d-work", "running")),
            "call for another task": (run(pending_task=REVIEWING, dispatches=[entry("d-other", role="reviewer", kind="review",
                                                                                    task_id="other")]),
                                      stage("dispatch", "other", None, "reviewer", "review", "d-other", "running")),
            "quota wait over a call": (run(pending_task=REVIEWING, quota_wait=WAIT,
                                           dispatches=[entry("d-review", role="reviewer", kind="review")]),
                                       stage("quota_wait", "greet", 2, "reviewer", None, "d-quota", None)),
        }
        for name, (ledger, expected) in rows.items():
            with self.subTest(row=name):
                before = copy.deepcopy(ledger)
                self.assertEqual(ledger_stage(ledger), expected)
                self.assertEqual(ledger, before)

    def test_damaged_shapes_never_raise(self):
        damaged = {
            "dispatches not a list": (run(dispatches={"d": entry("d")}), stage("between_steps")),
            "entries not objects": (run(dispatches=["d", 3, None, ["running"]]), stage("between_steps")),
            "receipt not an object": (run(dispatches=[entry("d", worker_receipt=["attempt", 2])]),
                                      stage("dispatch", "greet", None, "worker", "implement", "d", "running")),
            "boolean attempt": (run(dispatches=[entry("d", worker_receipt={"attempt": True})]),
                                stage("dispatch", "greet", None, "worker", "implement", "d", "running")),
            "non-string entry fields": (run(dispatches=[{"id": 7, "role": ["worker"], "kind": {}, "task_id": 1,
                                                         "status": "running", "process_state": 0}]),
                                        stage("dispatch")),
            "pending task not an object": (run(pending_task=["greet", 2, "reviewing"]), stage("between_steps")),
            "pending task stage unknown": (run(pending_task={"task_id": "greet", "attempt": 2, "stage": ["reviewing"]}),
                                           stage("between_steps")),
            "pending task fields damaged": (run(pending_task={"task_id": 3, "attempt": "2", "stage": "reviewing"}),
                                            stage("task_review")),
            # An entry without a task does not borrow the attempt of a stage whose task is unreadable.
            "no task on either side": (run(pending_task={"task_id": None, "attempt": 2, "stage": "reviewing"},
                                           dispatches=[entry("d", role="reviewer", kind="review", task_id=None)]),
                                       stage("dispatch", None, None, "reviewer", "review", "d", "running")),
            "post accept payload damaged": (run(pending_post_accept={"payload": "greet"}), stage("task_acceptance")),
            "final history not a list": (run(final_history={"attempt": 1, "status": "running", "phase": "reviewing"}),
                                         stage("between_steps")),
            "final history entries damaged": (run(final_history=[{"attempt": 1, "status": "running", "phase": "reviewing"},
                                                                 "garbage", 4]),
                                              stage("final_review", "final", 1)),
            "final phase damaged": (run(final_history=[{"attempt": 1.0, "status": "running", "phase": {"x": 1}}]),
                                    stage("between_steps")),
            "quota wait not an object": (run(quota_wait="d-quota"), stage("between_steps")),
            "quota wait fields damaged": (run(quota_wait={"role": 1, "dispatch_id": None}), stage("quota_wait")),
            "status not a string": (run(status=["running"]), None),
            "ledger not an object": (["running"], None),
        }
        for name, (ledger, expected) in damaged.items():
            with self.subTest(row=name):
                self.assertEqual(ledger_stage(ledger), expected)


class LedgerStageLifecycleTests(RepoCase):
    def test_calls_are_named_by_their_recorded_attempt(self):
        seen = []

        def script(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                path = Path(kwargs["cwd"]) / "hello.py"
                number = sum(call["role"] == "worker" for call in backend.calls)
                path.write_text(path.read_text(encoding="utf-8") + f"# attempt {number}\n", encoding="utf-8")
            if kwargs["kind"] == "review" and sum(call["kind"] == "review" for call in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding()])

        def during(backend, kwargs, packet):
            # What status and the dashboard read while the call runs: the saved ledger, nothing else.
            state_json = Path(kwargs["run_path"])
            saved = read_json(state_json)
            shown = status_summary(saved)["ledger_stage"]
            detail = observe.run_detail(state_json.parents[2], state_json.parent.name)
            self.assertEqual(detail["summary"]["ledger_stage"], shown)
            running = [item for item in saved["dispatches"] if item.get("status") == "running"]
            seen.append((kwargs["role"], kwargs["kind"], shown, running[-1]["id"], running[-1]["process_state"]))

        backend = ScriptedBackend(script)
        backend.before_result = during
        result = self.engine(backend).start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(result["status"], "completed")
        calls = [(role, kind) for role, kind, *_ in seen]
        self.assertEqual(calls, [("worker", "implement"), ("reviewer", "review"), ("commander", "decide"),
                                 ("worker", "implement"), ("reviewer", "review"), ("reviewer", "final_review")])
        ids = {kind: [d["id"] for d in result["dispatches"] if d.get("kind") == kind]
               for kind in ("implement", "review", "decide", "final_review")}
        self.assertEqual([record["attempt"] for record in result["review_history"]["greet"]], [1, 2])
        expected = [stage("dispatch", "greet", 1, "worker", "implement", ids["implement"][0]),
                    stage("dispatch", "greet", 1, "reviewer", "review", ids["review"][0]),
                    # After the rejection no task stage is recorded: the commander's call has no attempt.
                    stage("dispatch", "run", None, "commander", "decide", ids["decide"][0]),
                    stage("dispatch", "greet", 2, "worker", "implement", ids["implement"][1]),
                    stage("dispatch", "greet", 2, "reviewer", "review", ids["review"][1]),
                    stage("dispatch", "final", 1, "reviewer", "final_review", ids["final_review"][0])]
        for (role, kind, shown, running_id, process_state), want in zip(seen, expected):
            with self.subTest(call=(role, kind, want["attempt"])):
                # The scripted backend starts no process, so the ledger still says "starting": the stage
                # repeats that record of the running entry and claims nothing about a live process.
                self.assertEqual(shown, {**want, "process_state": "starting"})
                self.assertEqual((shown["dispatch_id"], shown["process_state"]), (running_id, process_state))
        # The finished run has no step; the projection is a record, so it went away with the status.
        self.assertIsNone(status_summary(result)["ledger_stage"])
        self.assertIsNone(status_summary(read_json(self.repo / ".harness" / "runs" / result["id"] / "state.json"))["ledger_stage"])


class LedgerStageBatchTests(RepoCase):
    """The shapes review batches and a left whole-run attempt write: an approved batch leaves its attempt's review
    the step, a left attempt is no step. Read from state.json after real saves and during real calls."""

    def test_a_task_batch_after_an_approved_one_is_named_by_the_work_attempt(self):
        self.config.data["runtime"]["max_attempts"] = 30
        backend = batches.scripted([20, 20, 20, 1])     # the fifth candidate lacks 61: batches of 60 and 1
        engine = self.engine(backend)
        window, seen = timeline(engine, backend)
        with window:
            result = engine.start("Implement greet", batches.plan())
        self.assertEqual(result["status"], "completed")

        def batch_approved(ledger):
            # An approval registered while the review of the same candidate goes on: an intermediate batch.
            pending, records = ledger.get("pending_task"), ledger["review_history"].get("greet", [])
            return bool(pending and records and records[-1].get("review", {}).get("verdict") == "approve"
                        and records[-1]["snapshot_digest"] == pending["snapshot_digest"])
        registered = first(seen, "save", batch_approved)
        ledger = seen[registered][1]
        work = [entry["worker_receipt"]["attempt"] for entry in ledger["dispatches"] if entry["role"] == "worker"]
        self.assertEqual((work[-1], ledger["pending_task"]["attempt"], len(ledger["pending_task"]["required_findings"])),
                         (5, 5, 1))
        # Between the two batches every save shows the task's review of this work attempt.
        self.assertEqual(until_prepared(seen, registered), [stage("task_review", "greet", 5)])
        # The second batch's call is the reviewer's, named by the same work attempt.
        dispatch = result["review_history"]["greet"][-1]["review_source"]["dispatch_id"]
        self.assertEqual([value for when, ledger, value in seen if when == "review" and batch_approved(ledger)],
                         [stage("dispatch", "greet", 5, "reviewer", "review", dispatch, "starting")])

    def test_a_whole_run_batch_after_an_approved_one_is_still_that_attempts_review(self):
        self.config.data["runtime"]["max_attempts"] = 10
        backend = switch.three_rejections()             # the fourth attempt lacks 66: batches of 60 and 6
        engine = self.engine(backend)
        window, seen = timeline(engine, backend)
        with window:
            result = engine.start("Implement greet", batches.plan())
        self.assertEqual(result["status"], "completed")

        def batch_approved(ledger):
            history = ledger["final_history"]
            return bool(history and history[-1]["status"] == "running" and history[-1]["approved_batches"])
        registered = first(seen, "save", batch_approved)
        record = seen[registered][1]["final_history"][-1]
        self.assertEqual((record["attempt"], record["phase"], len(record["approved_batches"]), len(record["required_findings"])),
                         (4, "reviewing", 1, 6))
        self.assertEqual(until_prepared(seen, registered), [stage("final_review", "final", 4)])
        dispatch = result["final_history"][-1]["review_source"]["dispatch_id"]
        self.assertEqual([value for when, ledger, value in seen if when == "final_review" and batch_approved(ledger)],
                         [stage("dispatch", "final", 4, "reviewer", "final_review", dispatch, "starting")])

    def test_a_left_whole_run_attempt_is_no_longer_the_step(self):
        for left, change in (("invalidated", None), ("invalid_rejected", switch.reject_first)):
            with self.subTest(left=left):
                self.setUp()
                backend = ScriptedBackend(change)
                engine = self.engine(backend)
                # The first whole-run review returns on A and the run stops before judging it.
                with switch.before_judgment(backend, 1), self.assertRaises(KeyboardInterrupt):
                    engine.start("Implement greet", batches.plan())
                os.chmod(engine.worktree / "hello.py", 0o664)   # B: the resume leaves the attempt on A
                resumed = self.engine(backend)
                window, seen = timeline(resumed, backend)
                with window:
                    result = resumed.resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                leave = first(seen, "save", lambda ledger: ledger["final_history"][0]["status"] != "running")
                self.assertEqual([(item["status"], item["phase"]) for item in seen[leave][1]["final_history"]],
                                 [(left, "finished")])
                self.assertEqual(seen[leave][2], stage("between_steps"))
                # Before the leave the attempt on A is the step; after it nothing, until the new attempt's verification.
                self.assertEqual(until_prepared(seen, 0), [stage("final_review", "final", 1), stage("between_steps"),
                                                           stage("final_verification", "final", 2)])
                # The next call is the new attempt's review; the left attempt names nothing.
                call = next(index for index, (when, _, _) in enumerate(seen) if when != "save")
                when, ledger, value = seen[call]
                self.assertEqual((when, [item["status"] for item in ledger["final_history"]]), ("final_review", [left, "running"]))
                dispatch = result["final_history"][1]["review_source"]["dispatch_id"]
                self.assertEqual(value, stage("dispatch", "final", 2, "reviewer", "final_review", dispatch, "starting"))


if __name__ == "__main__":
    unittest.main()
