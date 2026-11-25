"""Saved-mapping check points and completion gates: an approval covers the whole review history."""
import copy
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import harness.finalization as finalization
import harness.packets as packets
from harness import git
from harness.state_files import StateFiles
from harness.finalization import FinalAcceptance
from harness.receipts import ResultReceipts
from harness.review_progress import repair_findings, required_findings
from harness.task_lifecycle import TaskLifecycle
from harness.util import EvidenceError, HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase, crash_after_save
import test_review_finding_identity  # by module: a TestCase imported by name is collected again here
from test_runtime_changes import ScriptedBackend, finding, revised, trigger_kind


def plan():
    return read_json(ROOT / "examples" / "plan.json")


def calls(backend, **match):
    return [call for call in backend.calls if all(call.get(k) == v for k, v in match.items())]


def touch(kwargs, backend):
    path = Path(kwargs["cwd"]) / "hello.py"
    path.write_text(path.read_text(encoding="utf-8") + f"# worker {len(calls(backend, role='worker'))}\n",
                    encoding="utf-8")


def state(engine):
    return read_json(engine.directory / "state.json")


def stop_of(saved):
    return (saved["stop"]["kind"], saved["stop"]["reason"]) if saved.get("stop") else None


def swap(rows):
    """Exchange two rows' sources while their keys stay: the contract built from the keys is unchanged."""
    first, second = dict(rows[0]), dict(rows[1])
    rows[0], rows[1] = {**second, "key": first["key"]}, {**first, "key": second["key"]}


def final_history_fixture(tmp):
    """A whole-run history whose review originals are real files (review_progress only reads these)."""
    run = {"id": "run_final", "dispatches": [], "final_history": []}

    def original(ident, review, snapshot):
        path = Path(tmp).resolve() / "runs/run_final/dispatches" / ident / "returned-result.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(task_id="final", dispatch_id=ident, **review)))
        run["dispatches"].append(dict(id=ident, role="reviewer", kind="final_review", task_id="final",
                                      snapshot_digest=snapshot))
        return dict(dispatch_id=ident, artifact=str(path),
                    artifact_digest=hashlib.sha256(path.read_bytes()).hexdigest(), snapshot_digest=snapshot)

    def attempt(ident, review, snapshot, status, required=()):
        run["final_history"].append(dict(attempt=len(run["final_history"]) + 1, head="h", snapshot_digest=snapshot,
                                         status=status, required_findings=list(required), review=review,
                                         review_source=original(ident, review, snapshot), approved_batches=[]))
    return run, attempt


def two_findings_backend():
    """The first task review rejects with two new findings; every worker changes the candidate."""
    def change(backend, kwargs, packet, result):
        if kwargs["role"] == "worker":
            touch(kwargs, backend)
        elif kwargs["kind"] == "review" and len(calls(backend, kind="review")) == 1:
            result.update(verdict="request_changes",
                          findings=[{**finding(), "id": f"r1-{index}"} for index in range(2)])
    return ScriptedBackend(change)


class TaskSavedMappingTests(RepoCase):
    """Rule 7(a)(b): a task's saved, unregistered mapping is rechecked before dispatch, replay or preservation."""

    def interrupted(self, where):
        """Run until the second review is about to be dispatched, or has returned but is unregistered."""
        backend = two_findings_backend()
        engine = self.engine(backend)
        if where == "before_dispatch":
            original = ResultReceipts.dispatch_review

            def interrupt(service, kind, *args, **kwargs):
                if kind == "review" and len(calls(backend, kind="review")) == 1:
                    raise KeyboardInterrupt()
                return original(service, kind, *args, **kwargs)
            patcher = patch.object(ResultReceipts, "dispatch_review", autospec=True, side_effect=interrupt)
        else:
            original = TaskLifecycle._finish_review

            def interrupt(service, *args):
                if len(calls(backend, kind="review")) == 2:
                    raise KeyboardInterrupt()
                return original(service, *args)
            patcher = patch.object(TaskLifecycle, "_finish_review", autospec=True, side_effect=interrupt)
        with patcher, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        saved = state(engine)
        self.assertEqual((saved["pending_task"]["stage"], len(saved["pending_task"]["required_findings"])),
                         ("reviewing", 2))
        self.assertEqual(len(saved["review_history"]["greet"]), 1)
        return backend, engine, saved

    def test_a_changed_mapping_stops_before_the_review_is_dispatched_or_replayed(self):
        for where in ("before_dispatch", "before_replay"):
            with self.subTest(where=where):
                self.setUp()
                backend, engine, saved = self.interrupted(where)
                self.assertEqual("reviewer:review:greet" in saved["review_receipts"], where == "before_replay")
                swap(saved["pending_task"]["required_findings"])
                write_json(engine.directory / "state.json", saved)
                reviews = len(calls(backend, kind="review"))
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                after = state(engine)
                self.assertEqual(stop_of(after), ("evidence", "runtime_integrity"))
                self.assertEqual(len(calls(backend, kind="review")), reviews)
                self.assertEqual(len(after["review_history"]["greet"]), 1)
                self.assertEqual(after["pending_task"]["required_findings"], saved["pending_task"]["required_findings"])
                self.assertEqual("reviewer:review:greet" in after["review_receipts"], where == "before_replay")

    def test_a_changed_mapping_stops_before_a_returned_review_is_preserved_or_bound(self):
        for unbound in (False, True):
            with self.subTest(unbound=unbound):
                self.setUp()
                backend, engine, saved = self.interrupted("before_replay")
                receipt = saved["review_receipts"]["reviewer:review:greet"]
                if unbound:
                    receipt.pop("artifact_digest")   # returned, digest not yet bound: a legal crash point
                swap(saved["pending_task"]["required_findings"])
                write_json(engine.directory / "state.json", saved)
                hello = engine.worktree / "hello.py"
                hello.write_text(hello.read_text(encoding="utf-8") + "# candidate changed\n", encoding="utf-8")
                calls_before = len(backend.calls)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                after = state(engine)
                self.assertEqual(stop_of(after), ("evidence", "runtime_integrity"))
                self.assertEqual(len(backend.calls), calls_before)
                # Nothing preserved, nothing bound: the receipt is byte for byte the saved one.
                self.assertEqual(len(after["review_history"]["greet"]), 1)
                self.assertEqual(after["review_receipts"]["reviewer:review:greet"], receipt)
                self.assertIn("pending_task", after)


class TaskAcceptanceGateTests(RepoCase):
    def test_an_approval_leaving_a_historical_finding_open_is_never_committed(self):
        """Rule 8: acceptance recomputes the whole history on the approved snapshot before the commit."""
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                touch(kwargs, backend)
            elif kwargs["kind"] == "review" and len(calls(backend, kind="review")) == 1:
                result.update(verdict="request_changes", findings=[finding()])
            elif kwargs["kind"] == "review":
                for row in result["finding_dispositions"].values():
                    row.update(status="open", evidence=["still wrong"])
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        # The review-side gap check is bypassed, so an approve with an open key is taken as an approval,
        # and so is the progress check with the batch sequence, so that approval reaches acceptance.
        with patch("harness.task_lifecycle.disposition_gaps", return_value=[]), \
                patch("harness.task_lifecycle.batch_after", return_value=[]), \
                self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), ("runtime_failure", "invariant"))
        saved = state(engine)
        self.assertEqual(stop_of(saved), ("runtime_failure", "invariant"))
        self.assertEqual(saved["accepted"], {})
        self.assertEqual(saved["pending_acceptance"]["task_id"], "greet")
        self.assertEqual(git.git(engine.worktree, "rev-parse", "HEAD").strip(), saved["expected_head"])
        self.assertEqual(saved["expected_head"], saved["base_commit"])


class FinalSavedMappingTests(RepoCase):
    def test_a_changed_final_mapping_stops_before_the_review_is_dispatched(self):
        """Rule 7(c)."""
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and len(calls(backend, kind="final_review")) == 1:
                result.update(verdict="request_changes",
                              findings=[{**finding(), "id": f"g1-{index}"} for index in range(2)])
            elif trigger_kind(kwargs, packet) == "final":
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id="correct", depends_on=["greet"])
                revised(result, packet, "Resolve final findings", new_tasks=[task])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                touch(kwargs, backend)
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        original = ResultReceipts.dispatch_review

        def interrupt(service, kind, *args, **kwargs):
            if kind == "final_review" and len(calls(backend, kind="final_review")) == 1:
                raise KeyboardInterrupt()
            return original(service, kind, *args, **kwargs)
        with patch.object(ResultReceipts, "dispatch_review", autospec=True, side_effect=interrupt), \
                self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        saved = state(engine)
        record = saved["final_history"][-1]
        self.assertEqual((record["status"], record["phase"], len(record["required_findings"])), ("running", "reviewing", 2))
        swap(record["required_findings"])
        write_json(engine.directory / "state.json", saved)
        calls_before = len(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        after = state(engine)
        self.assertEqual(stop_of(after), ("evidence", "runtime_integrity"))
        self.assertEqual(len(backend.calls), calls_before)
        self.assertEqual(after["final_history"][-1], record)


class FinalCompletionGateTests(RepoCase):
    def rejected_once_backend(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and len(calls(backend, kind="final_review")) == 1:
                result.update(verdict="request_changes", findings=[finding()])
            elif trigger_kind(kwargs, packet) == "final":
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id="correct", depends_on=["greet"])
                revised(result, packet, "Resolve final findings", new_tasks=[task])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                touch(kwargs, backend)
        return ScriptedBackend(change)

    def test_a_reused_approval_must_cover_findings_reported_on_another_snapshot(self):
        """Rule 8: approve A, flip a mode bit (B, still clean), reject B, flip back: A needs one more review."""
        phase = {"b_rejects": False}

        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and phase["b_rejects"]:
                phase["b_rejects"] = False
                result.update(verdict="request_changes", findings=[{**finding(), "id": "only-on-b"}])
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        original = FinalAcceptance.final_review
        stop_after = {"approved": True, "rejected": False}

        def interrupting(service, **kwargs):
            outcome = original(service, **kwargs)
            last = service.context.run["final_history"][-1]
            if (outcome and stop_after["approved"]) or (not outcome and stop_after["rejected"] and last["status"] == "rejected"):
                stop_after.update(approved=False, rejected=False)
                raise KeyboardInterrupt()
            return outcome
        with patch.object(FinalAcceptance, "final_review", autospec=True, side_effect=interrupting):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", plan())
            self.assertEqual([r["status"] for r in state(engine)["final_history"]], ["approved"])
            hello = engine.worktree / "hello.py"
            os.chmod(hello, 0o664)
            phase["b_rejects"], stop_after["rejected"] = True, True
            with self.assertRaises(KeyboardInterrupt):
                self.engine(backend).resume(engine.run["id"])
            self.assertEqual([r["status"] for r in state(engine)["final_history"]], ["approved", "rejected"])
            os.chmod(hello, 0o644)
            result = self.engine(backend).resume(engine.run["id"])
        finals = calls(backend, kind="final_review")
        history = result["final_history"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual([r["status"] for r in history], ["approved", "rejected", "approved"])
        self.assertEqual(history[0]["snapshot_digest"], history[2]["snapshot_digest"])
        self.assertNotEqual(history[0]["snapshot_digest"], history[1]["snapshot_digest"])
        self.assertEqual(len(finals), 3)
        self.assertEqual([row["label"] for row in finals[-1]["packet"]["required_findings"]], ["only-on-b"])

        identity = result["result"]
        self.assertEqual(identity, {
            "commit": result["expected_head"], "tree": history[0]["tree"],
            "snapshot_digest": history[0]["snapshot_digest"], "attempts": [1, 3],
            "verification_mode": "run_worktree",
        })
        self.assertEqual(self.engine(backend).resume(result["id"])["result"], identity)

        def reject(label, edited):
            with self.subTest(damage=label):
                ledger = engine.directory / "state.json"
                write_json(ledger, edited)
                before = ledger.read_bytes()
                with self.assertRaises(HarnessError) as error:
                    self.engine(backend).resume(result["id"])
                self.assertEqual((error.exception.stop["kind"], error.exception.stop["reason"]),
                                 ("evidence", "load_integrity"))
                self.assertEqual(ledger.read_bytes(), before)

        for key, values in {
            "commit": (None, "other commit"),
            "tree": (None, "other tree"),
            "snapshot_digest": (None, "other snapshot", history[1]["snapshot_digest"]),
            "attempts": (None, [], [True, 3], [1.0, 3], [1, "3"], [1], [3], [3, 1],
                         [1, 1, 3], [1, 2, 3], [0, 3], [1, 4]),
            "verification_mode": (None, "fresh_checkout"),
            "extra_key": (True,),
        }.items():
            for value in values:
                reject((key, value), {**result, "result": {**identity, key: value}})
        for key in identity:
            reject(("missing field", key),
                   {**result, "result": {k: v for k, v in identity.items() if k != key}})
        for value in (None, [], "identity"):
            reject(("result shape", value), {**result, "result": value})
            reject(("result on blocked run", value), {**result, "status": "blocked", "result": value})
        reject("completed without result", {key: value for key, value in result.items() if key != "result"})
        reject("result on running run", {**result, "status": "running"})
        for field, value in (("attempt", 2), ("head", "other commit"), ("tree", "other tree"),
                             ("snapshot_digest", "other snapshot")):
            edited = copy.deepcopy(result)
            edited["final_history"][2][field] = value
            reject(("approval mismatch", field), edited)
        self.assertEqual(calls(backend, kind="final_review"), finals)

    def test_every_completion_passes_the_approval_contract_prefix_and_source_checks(self):
        """Rules 7 and 8: a reuse passes the same gate; each damaged part stops with its own type."""
        engine = self.engine(self.rejected_once_backend())
        result = engine.start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual([r["status"] for r in engine.run["final_history"]], ["rejected", "approved"])
        final = engine.services.finalization
        self.assertTrue(final.final_review())
        approved = engine.run["final_history"][1]
        with patch("harness.review_progress.disposition_gaps", return_value=[{"code": "finding_not_independently_resolved"}]), \
                self.assertRaises(HarnessError) as caught:
            final.final_review()
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), ("evidence", "runtime_integrity"))
        self.assertIn("批准条目不满足批准契约", str(caught.exception))
        label = approved["required_findings"][0]["label"]
        approved["required_findings"][0]["label"] = "rebound"
        with self.assertRaisesRegex(HarnessError, "整体批准") as caught:
            final.final_review()
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), ("runtime_failure", "invariant"))
        approved["required_findings"][0]["label"] = label
        summary = approved["review"]["summary"]
        approved["review"]["summary"] = "A summary the original never had"
        with self.assertRaises(HarnessError) as caught:
            final.final_review()
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), ("evidence", "runtime_integrity"))
        approved["review"]["summary"] = summary
        self.assertTrue(final.final_review())

    def test_a_failed_record_write_leaves_the_last_batch_unregistered_and_replayable(self):
        """Rule 12: final.json is written before the ledger advances; resume replays without a model call."""
        backend = ScriptedBackend()
        engine = self.engine(backend)
        original = finalization.write_json
        hit = []

        def writing(path, value):
            if Path(path).name == "final.json" and isinstance(value, dict) and value.get("phase") == "finished" and not hit:
                hit.append(True)
                raise PermissionError(13, "denied", str(path))
            return original(path, value)
        with patch.object(finalization, "write_json", side_effect=writing), \
                self.assertRaises((HarnessError, PermissionError)):
            engine.start("Implement greet", plan())
        self.assertTrue(hit)
        saved = state(engine)
        record = saved["final_history"][-1]
        self.assertEqual((record["status"], record["phase"]), ("running", "reviewing"))
        self.assertNotIn("review", record)
        self.assertIn("reviewer:final_review:final", saved["review_receipts"])
        self.assertEqual(read_json(Path(record["artifact"]))["phase"], "reviewing")
        calls_before = len(backend.calls)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(backend.calls), calls_before)
        last = result["final_history"][-1]
        self.assertEqual(last["status"], "approved")
        self.assertEqual(read_json(Path(last["artifact"])), last)

    def test_a_rejection_in_its_record_file_but_not_the_ledger_is_replayed_and_registered_in_one_save(self):
        """Rule 12: final.json replaced, the ledger not yet saved; resume rewrites the same bytes and saves once."""
        backend = self.rejected_once_backend()
        engine = self.engine(backend)
        original_write, original_save = finalization.write_json, engine.context.save
        written, dead = [], {"fired": False}

        def writing(path, value):
            original_write(path, value)
            if Path(path).name == "final.json" and value.get("phase") == "finished":
                written.append(Path(path).read_bytes())

        def saving(**kwargs):
            if dead["fired"]:
                return None
            if written:
                dead["fired"] = True
                raise KeyboardInterrupt()   # the process dies between the record file and the ledger
            return original_save(**kwargs)
        with patch.object(finalization, "write_json", side_effect=writing), \
                patch.object(engine.context, "save", side_effect=saving), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        saved = state(engine)
        record, receipt = saved["final_history"][-1], saved["review_receipts"]["reviewer:final_review:final"]
        self.assertEqual((len(saved["final_history"]), record["status"], record["phase"]), (1, "running", "reviewing"))
        artifact = Path(record["artifact"])
        self.assertEqual(read_json(artifact)["status"], "rejected")   # the record file leads the ledger by one step
        reviews = len(calls(backend, kind="final_review"))

        resumed = self.engine(backend)
        steps, save, checkpoint = [], resumed.context.save, resumed.context.checkpoint

        def rewriting(path, value):
            original_write(path, value)
            if Path(path) == artifact:
                steps.append(("record", artifact.read_bytes()))

        def counting(**kwargs):
            revision = save(**kwargs)
            steps.append(("save", state(engine)))
            return revision

        def checkpointing():
            checkpoint()
            steps.append(("checkpoint", None))
        with patch.object(finalization, "write_json", side_effect=rewriting), \
                patch.object(resumed.context, "save", side_effect=counting), \
                patch.object(resumed.context, "checkpoint", side_effect=checkpointing):
            result = resumed.resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        first = result["final_history"][0]
        # The returned review was replayed, not redrawn: the next call is the second attempt's review.
        self.assertEqual(len(calls(backend, kind="final_review")), reviews + 1)
        self.assertEqual((first["status"], first["review_source"]["dispatch_id"]), ("rejected", receipt["dispatch_id"]))
        start = steps.index(("record", written[0]))
        end = steps.index(("checkpoint", None), start)
        # The registration is one save: the record as its file holds it, the receipt removed, the feedback.
        self.assertEqual([kind for kind, _ in steps[start + 1:end]], ["save"])
        ledger = steps[start + 1][1]
        self.assertEqual(ledger["final_history"][0], read_json(artifact))
        self.assertNotIn("reviewer:final_review:final", ledger["review_receipts"])
        self.assertEqual(ledger["feedback"], {"final_review": finalization.feedback_view(ledger["final_history"][0])})
        self.assertEqual(artifact.read_bytes(), written[0])
        self.assertEqual(read_json(artifact), first)


class RunLevelPacketTests(RepoCase):
    def test_a_bad_copy_in_an_accepted_task_stops_the_whole_run_review_packet(self):
        """Rule 13: a run-level packet traverses every task history before any projection."""
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                touch(kwargs, backend)
            elif kwargs["kind"] == "review":
                number = len(calls(backend, kind="review"))
                if number == 1:
                    result.update(verdict="request_changes", findings=[{**finding(), "id": "r1-0"}])
                elif number == 2:
                    result.update(verdict="request_changes")
                    for row in result["finding_dispositions"].values():
                        row.update(status="open", evidence=["still wrong"])
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        original = packets.verify_history
        before = []

        def indexes():
            return {path: path.read_bytes() for folder in ("finding-progress/indexes", "input-indexes")
                    for path in (engine.directory / folder).glob("*.json")}

        def verify_bad_copy(run, task_id, *, files, whole_run=False):
            if (task_id == "greet" and not whole_run and run["final_history"]
                    and run["final_history"][-1]["phase"] == "reviewing"):
                self.assertEqual(sorted(run["accepted"]), ["greet"])
                self.assertFalse(before)
                history = run["review_history"]["greet"]
                before.append((copy.deepcopy(history), indexes()))
                # Give the real packet gate a bad saved mapping without editing the live ledger.
                run = copy.deepcopy(run)
                row = run["review_history"]["greet"][1]["required_findings"][0]
                self.assertEqual(row["label"], "r1-0")
                row["label"] = "rebound"
            return original(run, task_id, files=files, whole_run=whole_run)

        with patch.object(packets, "verify_history", side_effect=verify_bad_copy), \
                self.assertRaises(EvidenceError) as caught:
            engine.start("Implement greet", plan())

        saved = state(engine)
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]),
                         ("evidence", "runtime_integrity"))
        self.assertEqual(stop_of(saved), ("evidence", "runtime_integrity"))
        self.assertEqual(sorted(saved["accepted"]), ["greet"])
        self.assertEqual(len(before), 1)
        history, saved_indexes = before[0]
        self.assertEqual(saved["review_history"]["greet"], history)
        self.assertEqual(engine.run["review_history"]["greet"], history)
        self.assertEqual(calls(backend, kind="final_review"), [])
        self.assertFalse(any(item.get("kind") == "final_review" for item in saved["dispatches"]))
        self.assertEqual(indexes(), saved_indexes)


class DamagedHistoryTests(unittest.TestCase):
    history = test_review_finding_identity.FindingIdentityTests.history

    def assert_evidence_stop(self, call):
        with self.assertRaises(EvidenceError) as caught:
            call()
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), ("evidence", "runtime_integrity"))
        return caught.exception

    def test_a_disposition_that_is_not_a_status_mapping_is_an_evidence_stop(self):
        for value in (0, {}, "resolved", {"evidence": ["checked"]}):
            with self.subTest(value=value), TemporaryDirectory() as tmp:
                run, record = self.history(tmp)
                record("R1", dict(verdict="request_changes", findings=[finding()]))
                rows = required_findings(run, "t", "candidate", files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))
                record("R2", dict(verdict="request_changes", findings=[], finding_dispositions={"f1": value}),
                       required_findings=rows)
                self.assert_evidence_stop(lambda: required_findings(run, "t", "candidate", files=StateFiles(Path(tmp).resolve()).under("runs", run["id"])))
                self.assert_evidence_stop(lambda: repair_findings(run, "t", files=StateFiles(Path(tmp).resolve()).under("runs", run["id"])))

    def test_a_history_record_that_is_not_an_object_is_an_evidence_stop(self):
        for damaged in ([], None, "record"):
            with self.subTest(damaged=damaged), TemporaryDirectory() as tmp:
                run, record = self.history(tmp)
                record("R1", dict(verdict="request_changes", findings=[finding()]))
                run["review_history"]["t"].insert(0, damaged)
                self.assert_evidence_stop(lambda: required_findings(run, "t", "candidate", files=StateFiles(Path(tmp).resolve()).under("runs", run["id"])))
                self.assert_evidence_stop(lambda: repair_findings(run, "t", files=StateFiles(Path(tmp).resolve()).under("runs", run["id"])))
            with self.subTest(damaged=damaged, history="final"), TemporaryDirectory() as tmp:
                run, attempt = final_history_fixture(tmp)
                attempt("R1", dict(verdict="request_changes", findings=[finding()], summary="s"), "S0", "rejected")
                run["final_history"].append(damaged)
                self.assert_evidence_stop(lambda: required_findings(run, "final", "S0", files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]), whole_run=True))

    def test_whether_a_record_may_lack_a_review_is_decided_by_its_state(self):
        """A state whose writer leaves no review is skipped; any other record without one is damaged evidence."""
        rejection = dict(verdict="request_changes", findings=[{**finding(), "id": "X"}], summary="s")
        cases = [  # (history, state of a record without a review, whether that is legal)
            ("final", dict(status="running", phase="verifying"), True),
            ("final", dict(status="running", phase="reviewing", required_findings=[]), True),
            ("final", dict(status="verification_failed", phase="finished", failure_kind="exit_code"), True),
            ("final", dict(status="approved", phase="finished"), False),
            ("final", dict(status="rejected", phase="finished"), False),
            ("final", dict(status="invalid_rejected", phase="finished", validation_error="veto"), False),
            # A left attempt keeps its readable review; only an unreadable original is kept without one.
            ("final", dict(status="invalidated", phase="finished"), False),
            ("final", dict(status="invalidated", phase="finished", validation_error="review result is not a JSON object"), True),
            # An unreadable original kept when the task candidate changed.
            ("task", dict(status="invalidated", validation_error="review result is not a JSON object"), True),
            ("task", dict(status="invalidated"), False),        # an approval invalidated at acceptance keeps its review
            ("task", dict(status="invalid_rejected", validation_error="veto"), False),
            ("task", dict(), False),                             # a registered review
        ]
        for history, fields, legal in cases:
            with self.subTest(history=history, fields=fields), TemporaryDirectory() as tmp:
                if history == "final":
                    run, attempt = final_history_fixture(tmp)
                    attempt("R1", rejection, "S0", "rejected")
                    run["final_history"].append(dict(attempt=2, head="h", snapshot_digest="A", approved_batches=[], **fields))
                    reads = [lambda: required_findings(run, "final", "A", files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]), whole_run=True)]
                else:
                    run, record = self.history(tmp)
                    record("R1", rejection)
                    run["review_history"]["t"].append(dict(snapshot_digest="candidate", **fields))
                    reads = [lambda: required_findings(run, "t", "candidate", files=StateFiles(Path(tmp).resolve()).under("runs", run["id"])), lambda: repair_findings(run, "t", files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]))]
                for read in reads:
                    if legal:
                        self.assertEqual([row["label"] for row in read()], ["X"])
                    else:
                        self.assertIn("要求审阅原文", str(self.assert_evidence_stop(read)))

    def test_an_approval_entry_must_pass_the_review_contract_under_its_own_mapping(self):
        """Rule 7: an approved record contributes only if the contract it was judged by still holds."""
        resolved = {"f1": {"status": "resolved", "evidence": ["checked on A"]}}
        cases = {
            "holds": dict(verdict="approve", findings=[], summary="s", finding_dispositions=resolved),
            # Approve, no blocking finding, every key resolved, but no evidence: the contract rejects it.
            "no_evidence": dict(verdict="approve", findings=[], summary="s",
                                finding_dispositions={"f1": {"status": "resolved", "evidence": []}}),
            # A real request_changes original recorded as an approval.
            "rejection": dict(verdict="request_changes", findings=[], summary="s", finding_dispositions=resolved),
        }
        for name, review in cases.items():
            with self.subTest(case=name), TemporaryDirectory() as tmp:
                run, attempt = final_history_fixture(tmp)
                attempt("R1", dict(verdict="request_changes", findings=[{**finding(), "id": "X"}], summary="s"),
                        "S0", "rejected")
                rows = required_findings(run, "final", "A", files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]), whole_run=True)
                attempt("R2", review, "A", "approved", rows)
                if name == "holds":
                    self.assertEqual(required_findings(run, "final", "A", files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]), whole_run=True), [])
                    continue
                error = self.assert_evidence_stop(lambda: required_findings(run, "final", "A", files=StateFiles(Path(tmp).resolve()).under("runs", run["id"]), whole_run=True))
                self.assertIn("批准条目不满足批准契约", str(error))


class DamagedHistoryLoadTests(RepoCase):
    """A damaged review history is refused when the ledger is loaded, before any reader of it runs."""

    def interrupted(self, backend, predicate):
        engine = self.engine(backend)
        crash, fired = crash_after_save(engine, predicate)
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        self.assertTrue(fired["fired"])
        return engine, state(engine)

    def assert_refused_at_load(self, backend, engine, saved):
        """resume and abandon both stop as a load integrity failure: no model call, the ledger byte for byte."""
        write_json(engine.directory / "state.json", saved)
        ledger = (engine.directory / "state.json").read_bytes()
        calls_before = len(backend.calls)
        for operation in ("resume", "abandon"):
            with self.subTest(operation=operation):
                with self.assertRaises(HarnessError) as caught:
                    getattr(self.engine(backend), operation)(engine.run["id"])
                self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), ("evidence", "load_integrity"))
        self.assertEqual(len(backend.calls), calls_before)
        self.assertEqual((engine.directory / "state.json").read_bytes(), ledger)

    def test_a_record_that_is_not_an_object_is_refused_on_every_recovery_path(self):
        """The reviewer's cases: each reached an unclassified crash in a reader that runs before any traversal."""
        approved = lambda run: any(item["status"] == "approved" for item in run["final_history"])
        cases = {
            # Whole run approved, not yet completed: the damage after the approval, or before it.
            "final_last": (ScriptedBackend, approved, lambda saved: saved["final_history"].append([])),
            "final_before_approval": (ScriptedBackend, approved, lambda saved: saved["final_history"].insert(0, [])),
            # Task side: a saved directive, a returned worker receipt, a pending acceptance.
            "directive": (two_findings_backend, lambda run: run.get("pending_directive") and run["review_history"].get("greet"),
                          lambda saved: saved["review_history"]["greet"].__setitem__(0, [])),
            "worker_returned": (two_findings_backend, lambda run: run["review_history"].get("greet")
                                and "artifact_digest" in run["worker_receipts"].get("worker:implement:greet", {}),
                                lambda saved: saved["review_history"]["greet"].__setitem__(0, [])),
            "acceptance": (ScriptedBackend, lambda run: bool(run.get("pending_acceptance")),
                           lambda saved: saved["review_history"]["greet"].insert(0, [])),
        }
        for name, (make, predicate, damage) in cases.items():
            with self.subTest(case=name):
                self.setUp()
                backend = make()
                engine, saved = self.interrupted(backend, predicate)
                damage(saved)
                self.assert_refused_at_load(backend, engine, saved)
        # A quota wake and a stop's settlement rerun the same checks on the ledger in memory.
        saved["review_history"]["greet"].pop(0)
        write_json(engine.directory / "state.json", saved)
        loaded = self.engine(backend)
        loaded.load_run(engine.run["id"])
        loaded.run["review_history"]["greet"].append([])
        with self.assertRaises(EvidenceError):
            loaded.services.lifecycle.load_checks()

    def test_a_record_whose_state_requires_a_review_is_refused_without_one(self):
        """Review 1b, blocking 2: a rejection recorded as an approval without its review never hides its blocker."""
        reject = {"on": True}

        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and reject["on"]:
                result.update(verdict="request_changes", findings=[{**finding(), "id": "omitted-blocker"}])
        backend = ScriptedBackend(change)
        engine, saved = self.interrupted(backend, lambda run: any(item["status"] == "rejected" for item in run["final_history"]))
        record = saved["final_history"][0]
        self.assertEqual(read_json(Path(record["review_source"]["artifact"]))["findings"][0]["id"], "omitted-blocker")
        record["status"] = "approved"
        record.pop("review")      # the original and its source binding stay
        os.chmod(engine.worktree / "hello.py", 0o664)   # snapshot B: the old approval is not this snapshot's
        reject["on"] = False
        self.assert_refused_at_load(backend, engine, saved)
        self.assertNotEqual(state(engine)["status"], "completed")
        # Task side: a registered rejection (no status) that lost its review would drop its findings.
        self.setUp()
        backend = two_findings_backend()
        engine, saved = self.interrupted(backend, lambda run: run.get("pending_directive") and run["review_history"].get("greet"))
        saved["review_history"]["greet"][0].pop("review")
        self.assert_refused_at_load(backend, engine, saved)


class DamagedHistoryLifecycleTests(RepoCase):
    def test_packets_stop_on_a_non_object_record_of_their_history(self):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                touch(kwargs, backend)
            elif kwargs["kind"] == "review" and len(calls(backend, kind="review")) == 1:
                result.update(verdict="request_changes", findings=[finding()])
        engine = self.engine(ScriptedBackend(change))
        self.assertEqual(engine.start("Implement greet", plan())["status"], "completed")
        packets = engine.services.packets
        for history, task_id, kind in (("review_history", "greet", "review"), ("final_history", "final", "final_review")):
            with self.subTest(history=history):
                records = engine.run[history]["greet"] if history == "review_history" else engine.run[history]
                kept = records[0]
                records[0] = []
                with self.assertRaises(EvidenceError) as caught:
                    packets.runtime_packet({}, role="reviewer", kind=kind, task_id=task_id)
                self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]),
                                 ("evidence", "runtime_integrity"))
                records[0] = kept


if __name__ == "__main__":
    unittest.main()
