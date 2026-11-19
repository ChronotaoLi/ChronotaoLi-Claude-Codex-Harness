"""Review batches: no count stops a review; one candidate is reviewed in batches of at most sixty."""
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import harness.finalization as finalization
import harness.review_progress as review_progress
from harness.review_progress import BATCH_LIMIT, repair_findings, required_findings
from harness.task_lifecycle import TaskLifecycle
from harness.util import EvidenceError, HarnessError, read_json
from test_harness import ROOT, RepoCase
import test_review_finding_identity  # by module: a TestCase imported by name is collected again here
from test_runtime_changes import ScriptedBackend, finding, revised, trigger_kind


def plan():
    return read_json(ROOT / "examples" / "plan.json")


def calls(backend, **match):
    return [call for call in backend.calls if all(call.get(k) == v for k, v in match.items())]


def sizes(items):
    return [len(call["packet"].get("required_findings", [])) for call in items]


def labels(rows):
    return [row["label"] for row in rows]


def touch(kwargs, backend):
    path = Path(kwargs["cwd"]) / "hello.py"
    path.write_text(path.read_text(encoding="utf-8") + f"# worker {len(calls(backend, role='worker'))}\n",
                    encoding="utf-8")


def state(engine):
    return read_json(engine.directory / "state.json")


def stop_of(saved):
    return (saved["stop"]["kind"], saved["stop"]["reason"]) if saved.get("stop") else None


def events(engine, name):
    rows = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
    return [row for row in rows if row["event"] == name]


def rounds(*spec):
    """Labels in source order: rounds(("r1", 20), ("r2", 3)) is r1-0..r1-19, r2-0..r2-2."""
    return [f"{name}-{index}" for name, count in spec for index in range(count)]


def scripted(new_blocking, after=None):
    """Every worker changes the candidate; task review n (1-based) rejects with new_blocking[n-1]
    new findings, resolving every row it was given; later reviews approve unless ``after`` changes them."""
    def change(backend, kwargs, packet, result):
        if kwargs["role"] == "worker":
            touch(kwargs, backend)
        elif kwargs["kind"] == "review":
            number = len(calls(backend, kind="review"))
            if number <= len(new_blocking):
                result.update(verdict="request_changes", findings=[
                    {**finding(), "id": f"r{number}-{index}"} for index in range(new_blocking[number - 1])])
            elif after:
                after(backend, kwargs, packet, result, number)
    return ScriptedBackend(change)


def interrupt_at_event(engine, name):
    original = engine.context.event

    def event(event_name, **fields):
        original(event_name, **fields)
        if event_name == name:
            raise KeyboardInterrupt()
    return patch.object(engine.context, "event", side_effect=event)


class TaskBatchTests(RepoCase):
    """Rules 4, 5, 9, 10: 20, 20, 20 and 1 new findings, then approval: 61 lack a resolution on the fifth candidate."""

    def setUp(self):
        super().setUp()
        self.config.data["runtime"]["max_attempts"] = 30

    def test_sixty_one_findings_are_reviewed_in_two_batches_on_one_candidate(self):
        backend = scripted([20, 20, 20, 1])
        engine = self.engine(backend)
        result = engine.start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("stop", result)
        self.assertIn("greet", result["accepted"])
        reviews = calls(backend, kind="review")
        self.assertEqual(sizes(calls(backend, role="worker")), [0, 20, 20, 20, 1])
        self.assertEqual(sizes(reviews), [0, 20, 40, 60, 60, 1])
        # The open finding r4-0 is chosen first; the chosen rows keep source order and keys.
        first, second = reviews[4]["packet"]["required_findings"], reviews[5]["packet"]["required_findings"]
        self.assertEqual(labels(first), rounds(("r1", 20), ("r2", 20), ("r3", 19), ("r4", 1)))
        self.assertEqual([row["key"] for row in first], [f"f{n}" for n in range(1, 61)])
        self.assertEqual(labels(second), ["r3-19"])
        records = result["review_history"]["greet"]
        self.assertEqual(len(records), 6)
        self.assertEqual(records[-1]["snapshot_digest"], records[-2]["snapshot_digest"])
        self.assertEqual(records[-1]["diff_artifact"], records[-2]["diff_artifact"])
        self.assertEqual([Path(record["artifact"]).name for record in records[-2:]], ["review.json", "review-2.json"])
        for record in records:
            self.assertEqual(read_json(Path(record["artifact"])), record)
        # The last batch is the approval that was accepted.
        self.assertEqual(result["accepted"]["greet"]["review_artifact"], records[-1]["artifact"])
        self.assertEqual(result["accepted"]["greet"]["review_source"], records[-1]["review_source"])
        self.assertEqual(required_findings(result, "greet", records[-1]["snapshot_digest"]), [])
        self.assertEqual([(row["dispatch_id"], row["next_batch"]) for row in events(engine, "review_batch_approved")],
                         [(records[-2]["dispatch_id"], 1)])

    def test_a_rejected_second_batch_reworks_and_a_new_candidate_needs_every_batch_again(self):
        """Rule 5: any batch's rejection is rework; the next candidate starts again from the first batch."""
        def after(backend, kwargs, packet, result, number):
            if number == 6:   # the second batch of the fifth candidate
                result.update(verdict="request_changes")
                result["finding_dispositions"][packet["required_findings"][0]["key"]] = {
                    "status": "open", "evidence": ["hello.py still has the defect"]}
        backend = scripted([20, 20, 20, 1], after)
        engine = self.engine(backend)
        result = engine.start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")
        workers, reviews = calls(backend, role="worker"), calls(backend, kind="review")
        self.assertEqual(sizes(workers), [0, 20, 20, 20, 1, 1])
        self.assertEqual(labels(workers[5]["packet"]["required_findings"]), ["r3-19"])
        # The earlier candidate's batches close nothing here: the reopened finding goes first.
        self.assertEqual(sizes(reviews), [0, 20, 40, 60, 60, 1, 60, 1])
        self.assertIn("r3-19", labels(reviews[6]["packet"]["required_findings"]))
        self.assertEqual(labels(reviews[7]["packet"]["required_findings"]), ["r4-0"])
        statuses = [record["review"]["verdict"] for record in result["review_history"]["greet"]]
        self.assertEqual(statuses[4:], ["approve", "request_changes", "approve", "approve"])

    def test_the_rotation_reaches_findings_beyond_the_first_sixty(self):
        """Rule 3: the worker's first sixty open are reviewed; once resolved, the rest move up."""
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                touch(kwargs, backend)
            elif kwargs["kind"] == "review":
                number = len(calls(backend, kind="review"))
                if number <= 3:
                    result.update(verdict="request_changes",
                                  findings=[{**finding(), "id": f"r{number}-{index}"} for index in range(30)])
                if number <= 3 or number == 5:   # the fifth is the fourth candidate's second batch
                    result.update(verdict="request_changes")
                    for row in result.get("finding_dispositions", {}).values():
                        row.update(status="open", evidence=["hello.py still has the defect"])
        backend = ScriptedBackend(change)
        result = self.engine(backend).start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")
        workers = calls(backend, role="worker")
        self.assertEqual(sizes(workers), [0, 30, 60, 60, 30])
        self.assertEqual(sizes(calls(backend, kind="review")), [0, 30, 60, 60, 30, 60, 30])
        self.assertEqual(labels(workers[3]["packet"]["required_findings"]), rounds(("r1", 30), ("r2", 30)))
        self.assertEqual(labels(workers[4]["packet"]["required_findings"]), rounds(("r3", 30)))

    def test_an_interruption_between_batches_resumes_with_the_next_batch_only(self):
        """Rule 6: the registered batch, the next mapping and the receipt's removal are one save."""
        backend = scripted([20, 20, 20, 1])
        engine = self.engine(backend)
        with interrupt_at_event(engine, "review_batch_approved"), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        saved = state(engine)
        self.assertEqual(saved["pending_task"]["stage"], "reviewing")
        self.assertEqual(labels(saved["pending_task"]["required_findings"]), ["r3-19"])
        self.assertNotIn("reviewer:review:greet", saved["review_receipts"])
        self.assertEqual(len(saved["review_history"]["greet"]), 5)
        self.assertNotIn("pending_acceptance", saved)
        reviews, workers = len(calls(backend, kind="review")), len(calls(backend, role="worker"))
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(calls(backend, kind="review")), reviews + 1)
        self.assertEqual(len(calls(backend, role="worker")), workers)

    def test_a_returned_second_batch_is_registered_without_a_new_call(self):
        backend = scripted([20, 20, 20, 1])
        engine = self.engine(backend)
        original, raised = TaskLifecycle._finish_review, []

        def interrupt(service, *args):
            if len(calls(backend, kind="review")) == 6 and not raised:
                raised.append(True)
                raise KeyboardInterrupt()
            return original(service, *args)
        with patch.object(TaskLifecycle, "_finish_review", autospec=True, side_effect=interrupt), \
                self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        saved = state(engine)
        receipt = saved["review_receipts"]["reviewer:review:greet"]
        self.assertEqual(len(saved["review_history"]["greet"]), 5)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(calls(backend, kind="review")), 6)
        self.assertEqual(result["review_history"]["greet"][-1]["review_source"]["dispatch_id"], receipt["dispatch_id"])

    def test_a_candidate_change_between_batches_starts_the_batches_again(self):
        """Rule 5 and section 6: an approved batch closes findings only on its own candidate."""
        backend = scripted([20, 20, 20, 1])
        engine = self.engine(backend)
        with interrupt_at_event(engine, "review_batch_approved"), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        with (engine.worktree / "hello.py").open("a", encoding="utf-8") as stream:
            stream.write("# changed between batches\n")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(events(engine, "pending_task_invalidated")), 1)
        self.assertEqual(len(calls(backend, role="worker")), 6)
        self.assertEqual(sizes(calls(backend, kind="review")), [0, 20, 40, 60, 60, 60, 1])
        records = result["review_history"]["greet"]
        self.assertEqual(len(records), 7)
        self.assertNotEqual(records[4]["snapshot_digest"], records[-1]["snapshot_digest"])
        self.assertEqual(records[-1]["snapshot_digest"], records[-2]["snapshot_digest"])

    def test_acceptance_refuses_an_approval_registered_before_its_last_batch(self):
        """Rule 8: a successor that treats the first approving batch as complete is never committed."""
        backend = scripted([20, 20, 20, 1])
        engine = self.engine(backend)
        original = TaskLifecycle._finish_review

        def premature(service, pending, record, paths, keys, diagnostics, remaining):
            return original(service, pending, record, paths, keys, diagnostics, [])
        with patch.object(TaskLifecycle, "_finish_review", autospec=True, side_effect=premature), \
                self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), ("runtime_failure", "invariant"))
        saved = state(engine)
        self.assertEqual(saved["accepted"], {})
        self.assertEqual(saved["pending_acceptance"]["task_id"], "greet")
        self.assertEqual(len(calls(backend, kind="review")), 5)
        self.assertEqual(saved["expected_head"], saved["base_commit"])

    def test_a_failed_recomputation_leaves_the_batch_unregistered_and_replayable(self):
        """Rule 12: the next batch is computed after the record file and before any ledger change."""
        holder, kept = {}, {}

        def after(backend, kwargs, packet, result, number):
            if number == 5:   # the first batch's own original stays intact; an older one changes
                first = Path(holder["engine"].run["review_history"]["greet"][0]["review_source"]["artifact"])
                kept["path"], kept["bytes"] = first, first.read_bytes()
                first.write_bytes(kept["bytes"] + b" ")
        backend = scripted([20, 20, 20, 1], after)
        engine = holder["engine"] = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        saved = state(engine)
        self.assertEqual(stop_of(saved), ("evidence", "runtime_integrity"))
        self.assertEqual(len(calls(backend, kind="review")), 5)
        self.assertEqual(len(saved["review_history"]["greet"]), 4)
        self.assertEqual(saved["pending_task"]["stage"], "reviewing")
        self.assertEqual(len(saved["pending_task"]["required_findings"]), 60)
        self.assertIn("r4-0", labels(saved["pending_task"]["required_findings"]))
        receipt = saved["review_receipts"]["reviewer:review:greet"]
        self.assertIn("artifact_digest", receipt)
        # The record file was written first; the ledger does not name it yet.
        self.assertTrue(Path(saved["pending_task"]["diff_artifact"]).with_name("review.json").exists())
        kept["path"].write_bytes(kept["bytes"])
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        # The returned batch was registered from its receipt; the only new call is the next batch.
        self.assertEqual(sizes(calls(backend, kind="review"))[5:], [1])
        self.assertEqual(result["review_history"]["greet"][4]["review_source"]["dispatch_id"], receipt["dispatch_id"])


class TaskBatchSelectionTests(unittest.TestCase):
    """Rule 4 and the conditional progress of rule 5, on authenticated histories."""
    history = test_review_finding_identity.FindingIdentityTests.history
    dispositions = staticmethod(test_review_finding_identity.FindingIdentityTests.dispositions)

    def reported(self, record, ident, count, snapshot="S0"):
        return record(ident, dict(verdict="request_changes",
                                  findings=[{**finding(), "id": f"{ident}-{index}"} for index in range(count)]),
                      snapshot)

    def resolve(self, record, ident, rows, snapshot, verdict="approve", **statuses):
        """A usable review of ``snapshot`` disposing of ``rows``: resolved unless named otherwise."""
        marks = {row["key"]: statuses.get(row["key"], "resolved") for row in rows}
        return record(ident, dict(verdict=verdict, findings=[], finding_dispositions=self.dispositions(**marks)),
                      snapshot, required_findings=rows)

    def test_open_findings_are_chosen_first_and_keyed_in_source_order(self):
        with TemporaryDirectory() as tmp:
            run, record = self.history(tmp)
            self.reported(record, "R1", 61)
            on_a = required_findings(run, "t", "A")
            self.assertEqual(labels(on_a), rounds(("R1", 60)))
            self.resolve(record, "R2", on_a, "A")
            self.assertEqual(labels(repair_findings(run, "t")), ["R1-60"])
            # 61 lack a resolution on C; one is open: it joins the batch, R1-59 waits, keys follow source order.
            batch = required_findings(run, "t", "C")
            self.assertEqual(labels(batch), rounds(("R1", 59)) + ["R1-60"])
            self.assertEqual([row["key"] for row in batch], [f"f{n}" for n in range(1, BATCH_LIMIT + 1)])
            self.assertEqual([row["finding_index"] for row in batch], [*range(59), 60])

    def test_a_list_that_fits_is_the_whole_list_in_source_order(self):
        with TemporaryDirectory() as tmp:
            run, record = self.history(tmp)
            original = self.reported(record, "R1", 60)
            on_a = required_findings(run, "t", "A")
            # Half resolved, half open on A: the statuses differ, the order on C does not.
            self.resolve(record, "R2", on_a, "A", verdict="request_changes",
                         **{f"f{n}": "open" for n in range(31, 61)})
            source = original["review_source"]
            whole = [{"key": f"f{index + 1}", "review_dispatch_id": "R1", "finding_index": index,
                      "label": f"R1-{index}", "artifact": source["artifact"]} for index in range(60)]
            self.assertEqual(required_findings(run, "t", "C"), whole)
            self.assertEqual(on_a, whole)

    def test_each_approved_batch_closes_exactly_its_rows_on_a_fixed_candidate(self):
        for count, expected in ((0, 1), (60, 1), (61, 2), (120, 2)):
            with self.subTest(findings=count), TemporaryDirectory() as tmp:
                run, record = self.history(tmp)
                self.reported(record, "R1", count)
                reviews, batch = 0, required_findings(run, "t", "C")
                while True:
                    reviews += 1
                    if batch:
                        self.resolve(record, f"B{reviews}", batch, "C")
                    else:
                        record(f"B{reviews}", dict(verdict="approve", findings=[]), "C")
                    following = required_findings(run, "t", "C")
                    self.assertFalse({(r["review_dispatch_id"], r["finding_index"]) for r in following}
                                     & {(r["review_dispatch_id"], r["finding_index"]) for r in batch})
                    if not following:
                        break
                    batch = following
                self.assertEqual(reviews, expected)

    def test_a_batch_of_findings_already_closed_on_its_candidate_is_refused(self):
        with TemporaryDirectory() as tmp:
            run, record = self.history(tmp)
            self.reported(record, "R1", 120)
            first = required_findings(run, "t", "C")
            self.resolve(record, "B1", first, "C")
            self.assertEqual(labels(required_findings(run, "t", "C")), rounds(("R1", 120))[60:])
            # Sixty rows that the candidate already closed are not the batch recomputed after B1.
            self.resolve(record, "B2", first, "C")
            for read in (lambda: required_findings(run, "t", "C"), lambda: repair_findings(run, "t")):
                with self.assertRaises(EvidenceError) as caught:
                    read()
                self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]),
                                 ("evidence", "runtime_integrity"))
                self.assertIn("对照不符", str(caught.exception))


def final_fixture(tmp):
    """A whole-run history whose review originals are real files; records are appended by the test."""
    run = {"id": "run_final", "dispatches": [], "final_history": []}

    def original(ident, review, snapshot):
        path = Path(tmp).resolve() / "runs/run_final/dispatches" / ident / "returned-result.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(task_id="final", dispatch_id=ident, **review)))
        run["dispatches"].append(dict(id=ident, role="reviewer", kind="final_review", task_id="final",
                                      snapshot_digest=snapshot))
        return dict(dispatch_id=ident, artifact=str(path),
                    artifact_digest=hashlib.sha256(path.read_bytes()).hexdigest(), snapshot_digest=snapshot)
    return run, original


class ApprovedBatchContractTests(unittest.TestCase):
    def rejected_then(self, tmp):
        run, original = final_fixture(tmp)
        r1 = dict(verdict="request_changes", findings=[{**finding(), "id": "X"}], summary="s")
        run["final_history"].append(dict(attempt=1, head="h", snapshot_digest="S0", status="rejected",
                                         required_findings=[], review=r1, review_source=original("R1", r1, "S0"),
                                         approved_batches=[]))
        return run, original

    def test_a_rejection_recorded_as_an_approved_batch_never_closes(self):
        """T3-14: whatever the attempt's status, an approved batch passes the approval contract first."""
        for status in ("running", "rejected", "invalidated"):
            with self.subTest(status=status), TemporaryDirectory() as tmp:
                run, original = self.rejected_then(tmp)
                rows = required_findings(run, "final", "A", whole_run=True)
                self.assertEqual(labels(rows), ["X"])
                bad = dict(verdict="request_changes", findings=[], summary="s",
                           finding_dispositions={"f1": {"status": "resolved", "evidence": ["x"]}})
                attempt = dict(attempt=2, head="h", snapshot_digest="A", status=status, required_findings=[],
                               approved_batches=[dict(dispatch_id="R2", required_findings=rows, review=bad,
                                                      review_source=original("R2", bad, "A"))])
                if status != "running":
                    last = dict(verdict="request_changes", findings=[], summary="s")
                    attempt.update(phase="finished", review=last, review_source=original("R2b", last, "A"))
                run["final_history"].append(attempt)
                ok = dict(verdict="approve", findings=[], summary="s")
                run["final_history"].append(dict(attempt=3, head="h", snapshot_digest="A", status="approved",
                                                 required_findings=[], review=ok, approved_batches=[],
                                                 review_source=original("R3", ok, "A")))
                with self.assertRaises(EvidenceError) as caught:
                    required_findings(run, "final", "A", whole_run=True)
                self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]),
                                 ("evidence", "runtime_integrity"))
                self.assertIn("批准条目不满足批准契约", str(caught.exception))

    def test_approved_batches_must_be_a_list_of_reviews(self):
        """Every whole-run record carries its approved batches, each written with its review."""
        approval = dict(verdict="approve", findings=[], summary="s",
                        finding_dispositions={"f1": {"status": "resolved", "evidence": ["checked on A"]}})
        cases = {"missing": None, "not_a_list": {}, "not_an_object": [[]], "without_review": "drop"}
        for name, damage in cases.items():
            with self.subTest(case=name), TemporaryDirectory() as tmp:
                run, original = self.rejected_then(tmp)
                rows = required_findings(run, "final", "A", whole_run=True)
                batch = dict(dispatch_id="R2", required_findings=rows, review=approval,
                             review_source=original("R2", approval, "A"))
                attempt = dict(attempt=2, head="h", snapshot_digest="A", status="running", phase="reviewing",
                               required_findings=[], approved_batches=[batch])
                run["final_history"].append(attempt)
                # Intact, the batch closes X on A and on A only.
                self.assertEqual(required_findings(run, "final", "A", whole_run=True), [])
                self.assertEqual(labels(required_findings(run, "final", "B", whole_run=True)), ["X"])
                if damage is None:
                    attempt.pop("approved_batches")
                elif damage == "drop":
                    batch.pop("review")
                else:
                    attempt["approved_batches"] = damage
                with self.assertRaises(EvidenceError) as caught:
                    required_findings(run, "final", "A", whole_run=True)
                self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]),
                                 ("evidence", "runtime_integrity"))


class FinalBatchTests(RepoCase):
    """Rules 5, 8, 10, 12: three whole-run rejections of 22 findings each, then a candidate that resolves all 66."""

    def setUp(self):
        super().setUp()
        self.config.data["runtime"]["max_attempts"] = 10

    def final_backend(self, extra=None):
        def change(backend, kwargs, packet, result):
            finals = len(calls(backend, kind="final_review"))
            if kwargs["kind"] == "final_review" and finals <= 3:
                result.update(verdict="request_changes",
                              findings=[{**finding(), "id": f"g{finals}-{index}"} for index in range(22)])
            elif kwargs["kind"] == "final_review" and extra:
                extra(backend, kwargs, packet, result, finals)
            elif trigger_kind(kwargs, packet) == "final":
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id=f"correct{finals}", depends_on=["greet"])
                revised(result, packet, "Resolve final findings", new_tasks=[task])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"].startswith("correct"):
                touch(kwargs, backend)
        return ScriptedBackend(change)

    def test_whole_run_batches_share_the_task_rules_and_pass_the_completion_gate(self):
        backend = self.final_backend()
        engine = self.engine(backend)
        result = engine.start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")
        finals = calls(backend, kind="final_review")
        self.assertEqual(sizes(finals), [0, 22, 44, 60, 6])
        self.assertEqual(labels(finals[3]["packet"]["required_findings"]),
                         rounds(("g1", 22), ("g2", 16), ("g3", 22)))
        self.assertEqual(labels(finals[4]["packet"]["required_findings"]), [f"g2-{index}" for index in range(16, 22)])
        history = result["final_history"]
        self.assertEqual([item["status"] for item in history], ["rejected"] * 3 + ["approved"])
        self.assertTrue(all(item["approved_batches"] == [] for item in history[:3]))
        last = history[-1]
        self.assertEqual((len(last["approved_batches"]), len(last["required_findings"])), (1, 6))
        batch = last["approved_batches"][0]
        self.assertEqual([(row["key"], row["review_dispatch_id"], row["finding_index"]) for row in batch["required_findings"]],
                         [(row["key"], row["review_dispatch_id"], row["finding_index"])
                          for row in finals[3]["packet"]["required_findings"]])
        self.assertEqual(batch["review_source"]["dispatch_id"], batch["dispatch_id"])
        self.assertNotEqual(batch["dispatch_id"], last["review_source"]["dispatch_id"])
        self.assertEqual(read_json(Path(last["artifact"])), last)
        self.assertEqual([(row["attempt"], row["next_batch"]) for row in events(engine, "final_review_batch_approved")],
                         [(4, 6)])
        final = engine.services.finalization
        self.assertTrue(final.final_review())
        # A changed copy of an approved batch's mapping refuses completion.
        label = batch["required_findings"][0]["label"]
        batch["required_findings"][0]["label"] = "rebound"
        with self.assertRaises(HarnessError) as caught:
            final.final_review()
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), ("evidence", "runtime_integrity"))
        batch["required_findings"][0]["label"] = label
        self.assertTrue(final.final_review())
        # An approved batch whose contract no longer holds under its own mapping stops every traversal.
        gaps = review_progress.disposition_gaps

        def failing(review, rows):
            return [{"code": "finding_not_independently_resolved"}] if rows == batch["required_findings"] else gaps(review, rows)
        with patch("harness.review_progress.disposition_gaps", side_effect=failing), \
                self.assertRaises(HarnessError) as caught:
            final.final_review()
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), ("evidence", "runtime_integrity"))
        self.assertIn("批准条目不满足批准契约", str(caught.exception))
        self.assertTrue(final.final_review())
        # The load checks refuse a damaged approved_batches before anything reads it.
        batch.pop("review")
        with self.assertRaises(EvidenceError):
            engine.services.lifecycle.load_checks()

    def test_a_batch_record_file_leads_the_ledger_and_is_replayed(self):
        """Rule 12: the intermediate batch's final.json is written before the ledger advances."""
        for mode in ("write_fails", "interrupt_after_write"):
            with self.subTest(mode=mode):
                self.setUp()
                backend = self.final_backend()
                engine = self.engine(backend)
                original, hit = finalization.write_json, []

                def writing(path, value):
                    if (Path(path).name == "final.json" and isinstance(value, dict)
                            and value.get("approved_batches") and not hit):
                        hit.append(True)
                        if mode == "write_fails":
                            raise PermissionError(13, "denied", str(path))
                        original(path, value)
                        raise KeyboardInterrupt()
                    return original(path, value)
                with patch.object(finalization, "write_json", side_effect=writing), \
                        self.assertRaises((HarnessError, PermissionError, KeyboardInterrupt)):
                    engine.start("Implement greet", plan())
                self.assertTrue(hit)
                saved = state(engine)
                record = saved["final_history"][-1]
                self.assertEqual((record["status"], record["phase"]), ("running", "reviewing"))
                self.assertEqual((len(record["approved_batches"]), len(record["required_findings"])), (0, 60))
                self.assertIn("reviewer:final_review:final", saved["review_receipts"])
                self.assertEqual(len(read_json(Path(record["artifact"]))["approved_batches"]),
                                 0 if mode == "write_fails" else 1)
                finals = len(calls(backend, kind="final_review"))
                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(len(calls(backend, kind="final_review")), finals + 1)   # only the next batch
                last = result["final_history"][-1]
                self.assertEqual(last["approved_batches"][0]["dispatch_id"],
                                 saved["review_receipts"]["reviewer:final_review:final"]["dispatch_id"])
                self.assertEqual(read_json(Path(last["artifact"])), last)

    def test_feedback_triggers_and_prior_reviews_leave_approved_batches_out(self):
        """Rule 13: an attempt rejected on its second batch keeps the batch in the ledger only."""
        captured = {}

        def second_batch_rejects(backend, kwargs, packet, result, finals):
            if finals == 5:
                result.update(verdict="request_changes")
                result["finding_dispositions"][packet["required_findings"][0]["key"]] = {
                    "status": "open", "evidence": ["the whole-run candidate still has it"]}
        backend = self.final_backend(second_batch_rejects)
        engine = self.engine(backend)
        original = engine.context.event

        def event(name, **fields):
            original(name, **fields)
            if name == "final_review_rejected" and fields.get("attempt") == 4:
                captured["feedback"] = copy.deepcopy(engine.run["feedback"])
        with patch.object(engine.context, "event", side_effect=event):
            result = engine.start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")
        history = result["final_history"]
        self.assertEqual([item["status"] for item in history], ["rejected"] * 4 + ["approved"])
        self.assertEqual(len(history[3]["approved_batches"]), 1)
        self.assertEqual(sizes(calls(backend, kind="final_review")), [0, 22, 44, 60, 6, 60, 6])
        feedback = captured["feedback"]["final_review"]
        self.assertNotIn("approved_batches", feedback)
        self.assertEqual(feedback["required_findings"], history[3]["required_findings"])
        decides = [call for call in calls(backend, role="commander") if call["packet"].get("trigger", {}).get("kind") == "final"]
        self.assertEqual(len(decides), 4)
        self.assertNotIn("approved_batches", decides[-1]["packet"]["trigger"]["final_review"])
        self.assertEqual(decides[-1]["packet"]["trigger"]["final_review"]["attempt"], 4)
        reviewed = calls(backend, kind="final_review")[-1]["packet"]["prior_reviews"]
        self.assertEqual(len(reviewed), 4)
        self.assertFalse(any("approved_batches" in item for item in reviewed))
        worker = [call for call in calls(backend, role="worker") if call["task_id"] == "correct5"]
        self.assertEqual(len(worker), 1)
        self.assertNotIn("approved_batches", json.dumps(worker[0]["packet"]))


if __name__ == "__main__":
    unittest.main()
