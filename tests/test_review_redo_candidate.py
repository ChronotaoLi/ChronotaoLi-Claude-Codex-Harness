"""A review is dispatched only on the candidate its batch belongs to, and is redone only on that candidate (spec-1 rule 15)."""
import copy
import os
from pathlib import Path
from unittest.mock import patch

from harness.dispatch import Dispatcher
from harness.receipts import ResultReceipts
from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend, finding, revised, trigger_kind

FINAL_KEY = "reviewer:final_review:final"


def plan():
    return read_json(ROOT / "examples" / "plan.json")


def calls(backend, **match):
    return [call for call in backend.calls if all(call.get(k) == v for k, v in match.items())]


def state(engine):
    return read_json(engine.directory / "state.json")


def stop_of(saved):
    return (saved["stop"]["kind"], saved["stop"]["reason"]) if saved.get("stop") else None


def final_entries(run):
    return [entry for entry in run["dispatches"] if entry["role"] == "reviewer" and entry["kind"] == "final_review"]


def final_backend(invalid=()):
    """Three whole-run rejections of 22 findings each, each followed by a correction task; the
    whole-run reviews numbered in ``invalid`` (1-based) return an output without a verdict."""
    def change(backend, kwargs, packet, result):
        finals = len(calls(backend, kind="final_review"))
        if kwargs["kind"] == "final_review" and finals <= 3:
            result.update(verdict="request_changes",
                          findings=[{**finding(), "id": f"g{finals}-{index}"} for index in range(22)])
        elif kwargs["kind"] == "final_review" and finals in invalid:
            result.pop("verdict")
        elif trigger_kind(kwargs, packet) == "final":
            task = copy.deepcopy(packet["original_tasks"][0])
            task.update(id=f"correct{finals}", depends_on=["greet"])
            revised(result, packet, "Resolve final findings", new_tasks=[task])
        elif kwargs["role"] == "worker" and kwargs["task"]["id"].startswith("correct"):
            path = Path(kwargs["cwd"]) / "hello.py"
            path.write_text(path.read_text(encoding="utf-8") + f"# worker {len(calls(backend, role='worker'))}\n",
                            encoding="utf-8")
    return ScriptedBackend(change)


def changing_before_binding(change, role, kind):
    """Run ``change`` on the run's hello.py once, inside dispatch_once of the first matching call: after
    its packet is built, before its snapshot is bound. Returns the patch and what the ledger held then."""
    original, seen = Dispatcher.dispatch_once, {}

    def dispatch_once(service, call_role, call_kind, *args, **kwargs):
        if (call_role, call_kind) == (role, kind) and not seen:
            seen.update(agent_dispatches=service.context.run["agent_dispatches"],
                        dispatches=len(service.context.run["dispatches"]))
            change(service.context.worktree / "hello.py")
        return original(service, call_role, call_kind, *args, **kwargs)
    return patch.object(Dispatcher, "dispatch_once", autospec=True, side_effect=dispatch_once), seen


class WholeRunRedoCandidateTests(RepoCase):
    """Three whole-run rejections of 22 findings; the fourth attempt's first review (candidate A) returns
    invalid output and the controller dies right after it is recorded; then the candidate becomes B."""

    def setUp(self):
        super().setUp()
        self.config.data["runtime"]["max_attempts"] = 10

    def invalid_on_a_then_b(self, backend):
        engine = self.engine(backend)
        original, raised = ResultReceipts.record_invalid, []

        def interrupt(service, key, error, **kwargs):
            invalid = original(service, key, error, **kwargs)
            if key == FINAL_KEY and not raised:
                raised.append(True)
                raise KeyboardInterrupt()
            return invalid
        with patch.object(ResultReceipts, "record_invalid", autospec=True, side_effect=interrupt), \
                self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        saved = state(engine)
        on_a = final_entries(saved)[-1]
        self.assertEqual((on_a["status"], len(calls(backend, kind="final_review"))), ("invalid", 4))
        self.assertNotIn(FINAL_KEY, saved["review_receipts"])
        # Git still sees a clean tree; the snapshot digest (file modes included) is another candidate.
        os.chmod(engine.worktree / "hello.py", 0o664)
        return engine, on_a

    def test_a_review_of_another_candidate_is_not_a_redo(self):
        backend = final_backend(invalid=(4, 5))
        engine, on_a = self.invalid_on_a_then_b(backend)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        entries = final_entries(result)
        finals = calls(backend, kind="final_review")
        self.assertEqual([len(call["packet"].get("required_findings", [])) for call in finals], [0, 22, 44, 60, 60, 60, 6])
        self.assertEqual([entry.get("retry_of") for entry in entries],
                         [None] * 5 + [entries[4]["id"], None])
        # B's first review is a new request: no redo link and no hint of A's invalid output.
        first_on_b, redo_on_b = entries[4], entries[5]
        self.assertNotEqual(first_on_b["snapshot_digest"], on_a["snapshot_digest"])
        self.assertNotIn("previous_invalid", finals[4]["packet"])
        # Its own invalid output on B gets the one redo, shown that output.
        self.assertEqual(first_on_b["status"], "invalid")
        self.assertEqual(redo_on_b["snapshot_digest"], first_on_b["snapshot_digest"])
        self.assertEqual(finals[5]["packet"]["previous_invalid"]["dispatch_id"], first_on_b["id"])
        self.assertEqual(result["final_history"][-1]["status"], "approved")

    def test_a_redo_basis_read_on_another_candidate_stops_before_the_call(self):
        """The candidate is A while the redo basis is derived and B again when the entry is bound:
        the batch belongs to B, so only the redo condition of dispatch_once can refuse it."""
        backend = final_backend(invalid=(4,))
        engine, on_a = self.invalid_on_a_then_b(backend)
        hello = engine.worktree / "hello.py"
        original, seen = Dispatcher.redo_basis, []

        def derived_on_a(service, key):
            if key != FINAL_KEY or seen:
                return original(service, key)
            seen.append(service.context.run["agent_dispatches"])
            os.chmod(hello, 0o644)
            try:
                return original(service, key)
            finally:
                os.chmod(hello, 0o664)
        with patch.object(Dispatcher, "redo_basis", autospec=True, side_effect=derived_on_a), \
                self.assertRaises(HarnessError) as caught:
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]),
                         ("workspace_mismatch", "candidate_changed"))
        saved = state(engine)
        self.assertEqual(stop_of(saved), ("workspace_mismatch", "candidate_changed"))
        self.assertEqual(len(calls(backend, kind="final_review")), 4)
        refused = final_entries(saved)[-1]
        self.assertEqual((refused["invoked"], refused["status"]), (False, "failed"))
        self.assertNotIn("retry_of", refused)
        self.assertEqual(refused["snapshot_digest"], saved["final_history"][-1]["snapshot_digest"])
        self.assertNotEqual(refused["snapshot_digest"], on_a["snapshot_digest"])
        self.assertEqual(saved["agent_dispatches"], seen[0])
        self.assertNotIn(FINAL_KEY, saved["review_receipts"])
        # Resumed on B, the review is a new request and the run completes.
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual([entry.get("retry_of") for entry in final_entries(result) if entry["invoked"]], [None] * 6)
        self.assertNotIn("previous_invalid", calls(backend, kind="final_review")[4]["packet"])


class FirstDispatchCandidateTests(RepoCase):
    """The candidate changes after a review's packet is built and before dispatch_once binds its snapshot."""

    def assert_refused(self, engine, backend, kind, key, seen):
        saved = state(engine)
        self.assertEqual(stop_of(saved), ("workspace_mismatch", "candidate_changed"))
        self.assertEqual(calls(backend, role="reviewer", kind=kind), [])
        self.assertNotIn(key, saved["review_receipts"])
        self.assertEqual(len(saved["dispatches"]), seen["dispatches"] + 1)
        refused = saved["dispatches"][-1]
        self.assertEqual((refused["role"], refused["kind"], refused["invoked"], refused["status"]),
                         ("reviewer", kind, False, "failed"))
        self.assertEqual(saved["agent_dispatches"], seen["agent_dispatches"])
        return saved, refused

    def test_a_task_review_bound_to_another_candidate_is_never_called(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        patcher, seen = changing_before_binding(
            lambda hello: hello.write_text(hello.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8"),
            "reviewer", "review")
        with patcher, self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]),
                         ("workspace_mismatch", "candidate_changed"))
        saved, refused = self.assert_refused(engine, backend, "review", "reviewer:review:greet", seen)
        self.assertEqual(saved["pending_task"]["stage"], "reviewing")
        self.assertNotEqual(refused["snapshot_digest"], saved["pending_task"]["snapshot_digest"])
        self.assertEqual(saved["review_history"].get("greet", []), [])
        # resume finds the changed candidate, invalidates the stage and starts a fresh worker.
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(calls(backend, role="worker")), 2)
        self.assertEqual(len(calls(backend, kind="review")), 1)

    def test_a_whole_run_review_bound_to_another_candidate_is_never_called(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        patcher, seen = changing_before_binding(lambda hello: os.chmod(hello, 0o664), "reviewer", "final_review")
        with patcher, self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]),
                         ("workspace_mismatch", "candidate_changed"))
        saved, refused = self.assert_refused(engine, backend, "final_review", FINAL_KEY, seen)
        attempt = saved["final_history"][-1]
        self.assertEqual((attempt["status"], attempt["phase"]), ("running", "reviewing"))
        self.assertNotEqual(refused["snapshot_digest"], attempt["snapshot_digest"])
        # Back on the attempt's candidate, the same attempt dispatches its batch and the run completes.
        os.chmod(engine.worktree / "hello.py", 0o644)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(calls(backend, kind="final_review")), 1)
        self.assertEqual([item["status"] for item in result["final_history"]], ["approved"])


class RedoJudgedOnAnotherCandidateTests(RepoCase):
    """A review's first output on candidate A is invalid. While its redo is judged the candidate is
    briefly B, and it is A again when dispatch_once binds the entry: the judgment is redone on the
    bound snapshot, and a call judged as a new request is not made as one (spec-1 rule 15)."""

    def judged_on_b(self, kind, key):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == kind and len(calls(backend, kind=kind)) <= 2:
                result.pop("verdict")
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        original, seen = Dispatcher.redo_basis, {}

        def visit_b(service, call_key):
            if call_key != key or seen or not calls(backend, kind=kind):
                return original(service, call_key)
            run = service.context.run
            seen.update(agent_dispatches=run["agent_dispatches"], dispatches=len(run["dispatches"]),
                        invalid=copy.deepcopy(run["dispatches"][-1]))
            hello = service.context.worktree / "hello.py"
            mode = hello.stat().st_mode & 0o777
            os.chmod(hello, mode ^ 0o020)
            try:
                return original(service, call_key)
            finally:
                os.chmod(hello, mode)
        with patch.object(Dispatcher, "redo_basis", autospec=True, side_effect=visit_b), \
                self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]),
                         ("workspace_mismatch", "candidate_changed"))
        saved, invalid = state(engine), seen["invalid"]
        self.assertEqual((invalid["role"], invalid["kind"], invalid["status"]), ("reviewer", kind, "invalid"))
        self.assertEqual(stop_of(saved), ("workspace_mismatch", "candidate_changed"))
        self.assertEqual(len(calls(backend, kind=kind)), 1)
        self.assertNotIn(key, saved["review_receipts"])
        self.assertEqual(len(saved["dispatches"]), seen["dispatches"] + 1)
        refused = saved["dispatches"][-1]
        self.assertEqual((refused["role"], refused["kind"], refused["invoked"], refused["status"]),
                         ("reviewer", kind, False, "failed"))
        self.assertNotIn("retry_of", refused)
        # Bound to A, the candidate of the invalid output it was not judged a redo of.
        self.assertEqual(refused["snapshot_digest"], invalid["snapshot_digest"])
        self.assertEqual(saved["agent_dispatches"], seen["agent_dispatches"])
        return engine, backend, invalid

    def resumed_as_the_redo(self, kind, key):
        engine, backend, invalid = self.judged_on_b(kind, key)
        # Still on A: the call is the one redo of A's invalid output, and a second invalid output ends it.
        with self.assertRaises(HarnessError) as caught:
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]),
                         ("invalid_output", "redo_exhausted"))
        entries = [entry for entry in state(engine)["dispatches"]
                   if (entry.get("role"), entry.get("kind")) == ("reviewer", kind) and entry["invoked"]]
        self.assertEqual([entry.get("retry_of") for entry in entries], [None, invalid["id"]])
        self.assertEqual([entry["status"] for entry in entries], ["invalid", "invalid"])
        reviews = calls(backend, kind=kind)
        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[1]["packet"]["previous_invalid"]["dispatch_id"], invalid["id"])

    def test_a_task_review_judged_on_another_candidate_is_redone_on_its_own(self):
        self.resumed_as_the_redo("review", "reviewer:review:greet")

    def test_a_whole_run_review_judged_on_another_candidate_is_redone_on_its_own(self):
        self.resumed_as_the_redo("final_review", FINAL_KEY)

    def test_a_damaged_invalid_original_still_stops_the_redo_before_the_call(self):
        engine, backend, invalid = self.judged_on_b("review", "reviewer:review:greet")
        Path(invalid["invalid_output"]["artifact"]).write_text("{}", encoding="utf-8")
        before = state(engine)
        with self.assertRaises(HarnessError) as caught:
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]),
                         ("evidence", "runtime_integrity"))
        saved = state(engine)
        self.assertEqual(len(calls(backend, kind="review")), 1)
        self.assertEqual(saved["agent_dispatches"], before["agent_dispatches"])
        self.assertNotIn("reviewer:review:greet", saved["review_receipts"])
        # Refused while the redo was prepared (the original is read by its digest), not at load.
        self.assertEqual([(entry["role"], entry["kind"], entry["invoked"], entry["status"])
                          for entry in saved["dispatches"][len(before["dispatches"]):]],
                         [("reviewer", "review", False, "failed")])
