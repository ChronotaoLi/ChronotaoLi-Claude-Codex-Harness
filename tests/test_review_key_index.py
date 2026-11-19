"""Context projection of review history: one inline mapping, the rest in a checked, digest-bound index."""
from contextlib import nullcontext
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import harness.contracts as contracts
from harness.context import compact_reviews, review_digest
import harness.dispatch as dispatch
import harness.input_projection as input_projection
import harness.packets as packets
from harness.process_runtime import ProcessOutcome
from harness.task_lifecycle import TaskLifecycle
from harness.util import HarnessError, read_json, run_process
from harness.verification_plan import finding_progress
from test_harness import RepoCase
from test_review_batches import calls, plan, scripted, state, stop_of, touch
from test_runtime_changes import ScriptedBackend, finding, revised, trigger_kind, update


def worker_calls(backend):
    return calls(backend, role="worker")


def keys_in(value, name, path=()):
    """Every path inside ``value`` at which a dict has the key ``name``."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == name:
                found.append((*path, key))
            found.extend(keys_in(child, name, (*path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(keys_in(child, name, (*path, index)))
    return found


def ledger_records(records):
    """Every disposition entry of a task history, as the index states it."""
    return [{"review_dispatch_id": record["review_source"]["dispatch_id"],
             "snapshot_digest": record["snapshot_digest"],
             "artifact": record["review_source"]["artifact"],
             "artifact_digest": record["review_source"]["artifact_digest"],
             "required_findings": record["required_findings"],
             "finding_dispositions": record["review"]["finding_dispositions"]}
            for record in records if "finding_dispositions" in record.get("review", {})]


def without_own(packet):
    """A packet without its own required_findings, the dispatch's current mapping."""
    return {key: value for key, value in packet.items() if key != "required_findings"}


def index_files(engine, folder="input-indexes"):
    folder = engine.directory / folder
    return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


def history_registry(run):
    """Each review dispatch and record or original path of a history entry, mapped to its history: (task ID,
    False) or ("final", True). Consulted only at a position's own source fields, never for arbitrary values."""
    owners = {}
    histories = [((tid, False), records) for tid, records in run["review_history"].items()]
    histories.append((("final", True), run.get("final_history", [])))
    for history, records in histories:
        for record in records:
            for item in [*record.get("approved_batches", []), record]:
                source = item.get("review_source") or {}
                values = [item.get("dispatch_id"), item.get("artifact"), source.get("dispatch_id"), source.get("artifact")]
                owners.update(dict.fromkeys((value for value in values if isinstance(value, str)), history))
    return owners


# The position table: where a packet of the five calls holds review content. A saved feedback (and a
# decide's trigger) nests: a feedback holds its own task review, a whole-run record and a revision's
# trigger; a trigger holds a whole-run record, a task feedback and its current task's recovery.
REVIEW_KEYS = {"verdict", "findings", "finding_dispositions", "review", "review_in_prior_reviews", "review_source",
               "review_dispatch_id", "review_artifact", "review_content_sha256", "summary_source", "required_findings"}


def review_shaped(value):
    """An object shaped like review content: a review or its excerpt, a record holding one, a mapping row, a
    voided approval, or an accepted task's summary beside its record."""
    return isinstance(value, dict) and (not REVIEW_KEYS.isdisjoint(value) or {"summary", "details"} <= set(value))


def saved_position(rest, feedback=True):
    """The table row of a path inside a saved feedback (``feedback``) or a decide trigger, or None."""
    if not rest:
        return "feedback" if feedback else None
    key, more = rest[0], rest[1:]
    if key == "final_review":
        return "final_review"
    if feedback and key in ("review", "acceptance"):
        return "feedback"
    if feedback and key == "trigger":
        return saved_position(more, False)
    if not feedback and key == "feedback":
        return saved_position(more, True)
    if not feedback and key == "recovery" and more[:2] == ("latest", "review"):
        return "trigger_recovery"
    return None


def position(path):
    """The table row a packet path lies in, or None."""
    head, rest = path[0], path[1:]
    if head in ("prior_reviews", "required_findings", "dependencies", "accepted"):
        return head if rest else None
    if head == "required_finding_sources":
        return head
    if head == "recovery":
        return head if rest[:2] == ("latest", "review") else None
    if head in ("feedback", "trigger"):
        return saved_position(rest, head == "feedback")
    return None


def unregistered(packet):
    """The guard for new positions: every path at which a review-shaped object lies outside the table."""
    found = []

    def visit(value, path):
        if path and review_shaped(value) and position(path) is None:
            found.append(path)
            return
        children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
        for key, child in children:
            visit(child, (*path, key))
    visit(packet, ())
    return found


def table_positions(packet):
    """Each table position a packet holds, as (row, path, value), the saved feedback and triggers followed down."""
    rows = [(head, (head, key), value) for head in ("prior_reviews", "required_findings")
            for key, value in enumerate(packet.get(head) or [])]
    rows += [(head, (head, key), value) for head in ("dependencies", "accepted")
             for key, value in (packet.get(head) or {}).items()]
    if packet.get("required_finding_sources"):
        rows.append(("required_finding_sources", ("required_finding_sources",), packet["required_finding_sources"]))
    if isinstance(((packet.get("recovery") or {}).get("latest") or {}).get("review"), dict):
        rows.append(("recovery", ("recovery", "latest", "review"), packet["recovery"]["latest"]["review"]))
    pending = [("feedback", ("feedback",), packet.get("feedback")), ("trigger", ("trigger",), packet.get("trigger"))]
    while pending:
        kind, path, value = pending.pop()
        if not isinstance(value, dict):
            continue
        if kind == "feedback" and not {"review", "review_in_prior_reviews", "acceptance"}.isdisjoint(value):
            rows.append(("feedback", path, value))
        if isinstance(value.get("final_review"), dict):
            rows.append(("final_review", (*path, "final_review"), value["final_review"]))
        if kind == "feedback":
            pending.append(("trigger", (*path, "trigger"), value.get("trigger")))
        else:
            pending.append(("feedback", (*path, "feedback"), value.get("feedback")))
            latest = (value.get("recovery") or {}).get("latest") or {}
            if isinstance(latest.get("review"), dict):
                rows.append(("trigger_recovery", (*path, "recovery", "latest", "review"), latest["review"]))
    return rows


def source_histories(row, value, owners):
    """The histories one table position's own source fields name, or None when it holds no review content:
    a whole-run record none of whose review fields is present, as after a failed verification."""
    if row in ("prior_reviews", "recovery", "trigger_recovery"):
        cited = [(value.get("review_source") or {}).get("dispatch_id") or value.get("dispatch_id") or value.get("artifact")]
    elif row == "required_findings":
        cited = [value["review_dispatch_id"]]
    elif row == "required_finding_sources":
        cited = list(value)
    elif row in ("dependencies", "accepted"):
        cited = [value["details"]]
    elif row == "feedback":
        cited = [value.get("artifact") or value["acceptance"]["review_artifact"]]
    elif {"review", "review_in_prior_reviews", "review_source", "required_findings"}.isdisjoint(value):
        return None
    else:   # a reviewed whole-run record: the review registered with it and its mapping rows
        cited = [(value.get("review_source") or {}).get("dispatch_id"),
                 *(item["review_dispatch_id"] for item in value.get("required_findings") or [])]
    return {owners[item] for item in cited if isinstance(item, str) and item in owners}


def index_histories(packet, owners):
    """The histories the rows of a packet's progress and key indexes name, read back from their files."""
    found = set()
    if progress := packet.get("finding_progress"):
        for row in read_json(Path(progress["index"]["artifact"]))["progress"]["findings"]:
            found.add(("final", True) if row["task_id"] == "final" else (row["task_id"], False))
    if reference := packet.get("prior_review_keys_index"):
        for row in read_json(Path(packet["input_evidence_indexes"][reference["input_index"]]["artifact"]))["records"]:
            found.add(owners[row["review_dispatch_id"]])
    return found


class SourceCapture:
    """Patches runtime_packet, verify_history and compose: each composed packet is paired with the histories
    its last build traversed and handed to ``check(identity, packet, run, verified)``."""

    def __init__(self, check):
        self.check, self.builds = check, []

    def __enter__(self):
        traverse, build, compose = packets.verify_history, packets.PacketBuilder.runtime_packet, dispatch.compose

        def traversed(run, task_id, **kwargs):
            self.builds[-1]["verified"].add(("final", True) if kwargs.get("whole_run") else (task_id, False))
            return traverse(run, task_id, **kwargs)

        def built(service, packet, *, role, kind, task_id):
            self.builds.append({"identity": (role, kind, task_id), "verified": set(), "run": service.context.run})
            return build(service, packet, role=role, kind=kind, task_id=task_id)

        def composed(required, *args, **kwargs):
            last = self.builds[-1]
            identity = tuple(required[field] for field in ("role", "kind", "task_id"))
            assert last["identity"] == identity, (last["identity"], identity)
            self.check(identity, required["packet"], last["run"], last["verified"])
            return compose(required, *args, **kwargs)
        self.patches = [patch.object(packets, "verify_history", traversed),
                        patch.object(packets.PacketBuilder, "runtime_packet", built),
                        patch.object(dispatch, "compose", composed)]
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *exc):
        for item in reversed(self.patches):
            item.stop()


class IndexCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.config.data["runtime"]["max_attempts"] = 30

    def index_records(self, packet):
        """The records of a packet's prior_review_keys_index, read through input_evidence_indexes and checked by digest."""
        reference = packet["prior_review_keys_index"]
        entry = packet["input_evidence_indexes"][reference["input_index"]]
        raw = Path(entry["artifact"]).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), reference["input_index"])
        value = json.loads(raw)
        self.assertEqual((value["version"], len(value["records"])), (1, reference["count"]))
        return value["records"]

    def sixty_one(self, after=None):
        """T3-1: 20, 20, 20 and 1 new findings, then a candidate reviewed in two batches."""
        backend = scripted([20, 20, 20, 1], after)
        engine = self.engine(backend)
        result = engine.start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")
        return backend, engine, result


class PriorReviewKeysIndexTests(IndexCase):
    def test_only_the_latest_record_keeps_its_mapping_and_the_index_holds_every_entry(self):
        """T6-2: the fifth worker's prior_reviews keeps one mapping; the index equals the ledger's entries."""
        backend, engine, result = self.sixty_one()
        history = result["review_history"]["greet"]
        packet = worker_calls(backend)[4]["packet"]
        prior = packet["prior_reviews"]
        self.assertEqual(len(prior), 4)
        self.assertEqual([("required_findings" in item, "finding_dispositions" in item["review"]) for item in prior],
                         [(False, False)] * 3 + [(True, True)])
        self.assertEqual(prior[-1]["required_findings"], history[3]["required_findings"])
        self.assertEqual(keys_in(packet, "approved_batches"), [])
        records = self.index_records(packet)
        self.assertEqual(records, ledger_records(history[:4]))
        self.assertEqual([len(record["required_findings"]) for record in records], [20, 40, 60])
        # The dispatch recorded the index as its input evidence, so load and dispatch checks verify it.
        entry = [item for item in result["dispatches"] if item["role"] == "worker" and item.get("invoked")][4]
        reference = packet["input_evidence_indexes"][packet["prior_review_keys_index"]["input_index"]]
        self.assertIn(reference, entry["input_evidence"])
        # The second batch's review packet carries prior_reviews from the worker's handoff; the
        # first batch of its own candidate is not inline there, and the index has it.
        second = calls(backend, kind="review")[5]["packet"]
        self.assertEqual(len(second["prior_reviews"]), 4)
        self.assertEqual(self.index_records(second), ledger_records(history[:5]))

    def test_a_changed_source_stops_before_any_key_index_is_published(self):
        """T6-3: an indexed original whose bytes changed stops the next worker packet before the index is written."""
        # Intact, three rejections of one finding each give the fourth worker an index of the second and third.
        backend = scripted([1, 1, 1])
        result = self.engine(backend).start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual([record["review_dispatch_id"] for record in self.index_records(worker_calls(backend)[3]["packet"])],
                         [record["review_source"]["dispatch_id"] for record in result["review_history"]["greet"][1:3]])
        self.setUp()
        holder, seen = {}, {}
        backend = scripted([1, 1, 1])
        rejecting = backend.change

        def change(backend, kwargs, packet, result):
            rejecting(backend, kwargs, packet, result)
            if kwargs["kind"] == "review" and len(calls(backend, kind="review")) == 3:
                # The second review's original, an entry of the index the next worker would get.
                second = Path(holder["engine"].run["review_history"]["greet"][1]["review_source"]["artifact"])
                second.write_bytes(second.read_bytes() + b" ")
                seen["files"] = index_files(holder["engine"])
        backend.change = change
        engine = holder["engine"] = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        saved = state(engine)
        self.assertEqual(stop_of(saved), ("evidence", "runtime_integrity"))
        self.assertEqual(len(worker_calls(backend)), 3)
        self.assertEqual(len(saved["review_history"]["greet"]), 3)
        self.assertEqual(index_files(engine), seen["files"])

    def test_feedback_and_triggers_carry_no_earlier_mapping(self):
        """T6-4: a task rejection's feedback is deduplicated, and only the latest record keeps a mapping."""
        backend, engine, result = self.sixty_one()
        packet = worker_calls(backend)[4]["packet"]
        self.assertTrue(packet["feedback"]["review_in_prior_reviews"])
        self.assertEqual(keys_in(without_own(packet), "required_findings"),
                         [("prior_reviews", 3, "required_findings")])
        self.assertEqual(keys_in(packet, "finding_dispositions"),
                         [("prior_reviews", 3, "review", "finding_dispositions")])

    def test_projection_moves_no_gate(self):
        """T6-6: without the published index the batches, keys and acceptance are the same."""
        def run(patched):
            if patched:
                with patch("harness.packets.project_review_keys", side_effect=lambda packet, directory, records: packet):
                    backend, engine, result = self.sixty_one()
            else:
                backend, engine, result = self.sixty_one()
            shape = [(call["role"], call["kind"], [(row["key"], row["label"], row["finding_index"])
                                                   for row in call["packet"].get("required_findings", [])])
                     for call in backend.calls]
            verdicts = [record["review"]["verdict"] for record in result["review_history"]["greet"]]
            published = any("prior_review_keys_index" in call["packet"] for call in backend.calls)
            return shape, verdicts, sorted(result["accepted"]), result["status"], published
        with_index = run(False)
        self.setUp()
        without_index = run(True)
        self.assertTrue(with_index[-1])
        self.assertFalse(without_index[-1])
        self.assertEqual(with_index[:-1], without_index[:-1])


class OneReferencePerValueTests(IndexCase):
    def test_excerpts_and_identities_are_cited_once(self):
        """T6-8: no excerpt names its artifact again, truncated excerpts read back by their record, no duplicate ids."""
        backend, engine, result = self.sixty_one()
        prior = worker_calls(backend)[4]["packet"]["prior_reviews"]
        self.assertEqual(keys_in(prior, "full_review_artifact"), [])
        truncated = [item for item in prior if item["review"].get("details_truncated")]
        self.assertTrue(truncated)
        for item in truncated:
            self.assertEqual(review_digest(read_json(Path(item["artifact"]))["review"]),
                             item["review"]["review_content_sha256"])
        self.assertFalse([item for item in prior if "dispatch_id" in item])
        self.assertFalse([item for item in prior if "snapshot_digest" in item])
        self.assertTrue(all(item["review_source"]["dispatch_id"] for item in prior))

    def test_an_earlier_batch_of_the_same_candidate_names_the_next(self):
        """T6-8: after a rejected second batch, the first batch cites the second for its candidate references."""
        def after(backend, kwargs, packet, result, number):
            if number == 6:   # the second batch of the fifth candidate
                result.update(verdict="request_changes")
                result["finding_dispositions"][packet["required_findings"][0]["key"]] = {
                    "status": "open", "evidence": ["hello.py still has the defect"]}
        backend, engine, result = self.sixty_one(after)
        history = result["review_history"]["greet"]
        self.assertEqual(history[4]["diff_artifact"], history[5]["diff_artifact"])
        prior = worker_calls(backend)[5]["packet"]["prior_reviews"]
        self.assertEqual(len(prior), 6)
        first, second = prior[4], prior[5]
        self.assertEqual(first["same_candidate_as"], second["review_source"]["dispatch_id"])
        self.assertEqual(first["review_source"], history[4]["review_source"])
        for field in ("diff_artifact", "verification", "worker_source"):
            self.assertNotIn(field, first)
            self.assertEqual(second[field], history[5][field])
        # Records of other candidates keep their own references.
        self.assertTrue(all("same_candidate_as" not in item and "diff_artifact" in item for item in prior[:4]))


class FinalKeyIndexTests(IndexCase):
    def final_backend(self, rejections, count):
        def change(backend, kwargs, packet, result):
            finals = len(calls(backend, kind="final_review"))
            if kwargs["kind"] == "final_review" and finals <= rejections:
                result.update(verdict="request_changes",
                              findings=[{**finding(), "id": f"g{finals}-{index}"} for index in range(count)])
            elif trigger_kind(kwargs, packet) == "final":
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id=f"correct{finals}", depends_on=["greet"])
                revised(result, packet, "Resolve final findings", new_tasks=[task])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"].startswith("correct"):
                touch(kwargs, backend)
        return ScriptedBackend(change)

    def test_whole_run_entries_join_finding_progress_with_their_approved_batches(self):
        """T6-5: after T3-10, the run-level progress has the whole-run rows, resolved by their batch."""
        backend = self.final_backend(3, 22)
        result = self.engine(backend).start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")
        last = result["final_history"][-1]
        batch = last["approved_batches"][0]
        progress = finding_progress(result, list(result["review_history"]), whole_run=True, limit=None)
        self.assertEqual(progress["history_task_ids"][-1], "final")
        rows = {(row["review_dispatch_id"], row["finding_index"]): row
                for row in progress["findings"] if row["task_id"] == "final"}
        self.assertEqual(len(rows), 66)
        for row in batch["required_findings"]:
            found = rows[(row["review_dispatch_id"], row["finding_index"])]
            self.assertEqual(found["status"], "resolved")
            self.assertEqual(found["disposition_source"]["review_dispatch_id"], batch["dispatch_id"])
            self.assertEqual(found["disposition_source"]["snapshot_digest"], last["snapshot_digest"])
        for row in last["required_findings"]:
            self.assertEqual(rows[(row["review_dispatch_id"], row["finding_index"])]["disposition_source"]["review_dispatch_id"],
                             last["review_source"]["dispatch_id"])
        # The whole-run review packet's progress index covers the whole-run history too.
        packet = calls(backend, kind="final_review")[-1]["packet"]
        self.assertEqual(packet["finding_progress"]["history_task_ids"][-1], "final")
        # A task's own view stays its own history.
        self.assertEqual(finding_progress(result, ["greet"], limit=None)["history_task_ids"], ["greet"])

    def test_whole_run_feedback_and_trigger_carry_only_the_latest_mapping(self):
        """T6-4: after a whole-run rejection, the correction worker and the decide see the latest record's mapping only."""
        backend = self.final_backend(3, 22)
        result = self.engine(backend).start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")
        latest = result["final_history"][2]
        self.assertEqual(len(latest["required_findings"]), 44)
        decide = [call for call in calls(backend, role="commander") if trigger_kind(call, call["packet"]) == "final"][2]
        self.assertEqual(keys_in(decide["packet"], "required_findings"), [("trigger", "final_review", "required_findings")])
        self.assertEqual(decide["packet"]["trigger"]["final_review"]["required_findings"], latest["required_findings"])
        worker = [call for call in worker_calls(backend) if call["task_id"] == "correct3"]
        self.assertEqual(len(worker), 1)
        packet = worker[0]["packet"]
        self.assertEqual(keys_in(without_own(packet), "required_findings"),
                         [("feedback", "trigger", "final_review", "required_findings")])
        self.assertEqual(packet["feedback"]["trigger"]["final_review"]["required_findings"], latest["required_findings"])
        # The trigger's excerpt is complete in the artifact of the record holding it, which it does not name again.
        record = decide["packet"]["trigger"]["final_review"]
        self.assertTrue(record["review"]["details_truncated"])
        self.assertEqual(record["artifact"], latest["artifact"])
        self.assertEqual(review_digest(read_json(Path(record["artifact"]))["review"]), record["review"]["review_content_sha256"])
        self.assertEqual(keys_in(decide["packet"], "full_review_artifact") + keys_in(packet, "full_review_artifact"), [])

    def test_one_visible_prior_review_still_gets_the_current_attempts_batches(self):
        """T6-7: the full ledger, not the count of prior_reviews, decides the index."""
        backend = self.final_backend(1, 61)
        # Only to reach one visible prior review beside an approved batch of the current attempt.
        with patch.dict(contracts.REVIEW["properties"]["findings"], {"maxItems": 61}):
            result = self.engine(backend).start("Implement greet", plan())
        self.assertEqual(result["status"], "completed")
        finals = calls(backend, kind="final_review")
        self.assertEqual([len(call["packet"].get("required_findings", [])) for call in finals], [0, 60, 1])
        packet = finals[-1]["packet"]
        # The one visible record is the first attempt, whose review had nothing to dispose of.
        self.assertEqual(len(packet["prior_reviews"]), 1)
        self.assertEqual(packet["prior_reviews"][0]["required_findings"], [])
        batch = result["final_history"][-1]["approved_batches"][0]
        records = self.index_records(packet)
        self.assertEqual([record["review_dispatch_id"] for record in records], [batch["dispatch_id"]])
        self.assertEqual(records[0]["required_findings"], batch["required_findings"])
        self.assertEqual(records[0]["finding_dispositions"], batch["review"]["finding_dispositions"])
        self.assertEqual(records[0]["snapshot_digest"], result["final_history"][-1]["snapshot_digest"])


class SharedScopeTests(IndexCase):
    """Rule 13 and its corrections: a packet projects its call's histories (the call, not a task's name, makes
    it run-level) and, before its call, traverses every history whose review content it carries."""

    def correction_backend(self, name, *, task_rejections=False):
        """The first whole-run review rejects one finding and the decide adds the correction task ``name``;
        with ``task_rejections`` each task's first review rejects one finding too."""
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                touch(kwargs, backend)
            elif kwargs["kind"] == "final_review" and len(calls(backend, kind="final_review")) == 1:
                result.update(verdict="request_changes", findings=[{**finding(), "id": "w1"}])
            elif kwargs["kind"] == "review" and task_rejections:
                task_id = backend.calls[-1]["task_id"]
                if len(calls(backend, kind="review", task_id=task_id)) == 1:
                    result.update(verdict="request_changes", findings=[{**finding(), "id": f"{task_id}-1"}])
            elif trigger_kind(kwargs, packet) == "final":
                task = copy.deepcopy(packet["original_tasks"][0])
                task.update(id=name, depends_on=["greet"])
                revised(result, packet, "Resolve the whole-run finding", new_tasks=[task])
        return ScriptedBackend(change)

    def prerequisite_backend(self):
        """greet's first review rejects one finding; the task decide adds the prerequisite prep and selects it,
        so prep's worker carries greet's rejection in the trigger saved with the revision."""
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                touch(kwargs, backend)
            elif (kwargs["kind"] == "review" and backend.calls[-1]["task_id"] == "greet"
                    and len(calls(backend, kind="review", task_id="greet")) == 1):
                result.update(verdict="request_changes", findings=[{**finding(), "id": "greet-1"}])
            elif trigger_kind(kwargs, packet) == "task" and len(calls(backend, role="commander")) == 1:
                original = packet["original_tasks"][0]
                task = copy.deepcopy(original)
                task.update(id="prep")
                revised(result, packet, "Add a prerequisite", new_tasks=[task], selected="prep",
                        updates=[update(original, deps=["prep"])])
        return ScriptedBackend(change)

    def stopped_before_worker(self, backend, target, original):
        """Change the original ``original(run)`` names just before the worker of ``target`` is built; that
        build stops with evidence/runtime_integrity: no model call and no index after the change."""
        seen, actual = {}, TaskLifecycle.task_attempt

        def tamper(service, task, directive):
            if task["id"] == target and not seen:
                artifact = Path(original(service.context.run))
                artifact.write_bytes(artifact.read_bytes() + b" ")
                seen.update(calls=len(backend.calls),
                            indexes=(index_files(engine), index_files(engine, "finding-progress/indexes")))
            return actual(service, task, directive)
        engine = self.engine(backend)
        with patch.object(TaskLifecycle, "task_attempt", tamper), self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        self.assertTrue(seen)
        self.assertEqual(stop_of(state(engine)), ("evidence", "runtime_integrity"))
        self.assertEqual(len(backend.calls), seen["calls"])
        self.assertEqual(calls(backend, role="worker", task_id=target), [])
        self.assertEqual((index_files(engine), index_files(engine, "finding-progress/indexes")), seen["indexes"])

    def test_a_changed_original_a_packet_carries_stops_it_before_its_call(self):
        """Confirm review 1f2, blocker 1: a worker packet traverses every history it carries review content of.

        The whole-run original behind a correction's feedback (task named commander or not), an accepted
        dependency's approving original, and the rejected task's original behind a prerequisite's feedback:
        each changed after the decide stops that worker's build. The names run and final never become a task.
        """
        cases = (("commander", lambda: self.correction_backend("commander"),
                  lambda run: run["final_history"][0]["review_source"]["artifact"]),
                 ("fixup", lambda: self.correction_backend("fixup"),
                  lambda run: run["final_history"][0]["review_source"]["artifact"]),
                 ("fixup", lambda: self.correction_backend("fixup"),
                  lambda run: run["accepted"]["greet"]["review_source"]["artifact"]),
                 ("prep", self.prerequisite_backend,
                  lambda run: run["review_history"]["greet"][0]["review_source"]["artifact"]))
        for number, (target, backend, original) in enumerate(cases):
            with self.subTest(case=number, task=target):
                if number:
                    self.setUp()
                backend = backend()
                self.stopped_before_worker(backend, target, original)
                if target != "prep":
                    self.assertEqual(len(calls(backend, kind="final_review")), 1)
        for name in ("run", "final"):
            with self.subTest(name=name):
                self.setUp()
                backend = self.correction_backend(name)
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start("Implement greet", plan())
                saved = state(engine)
                self.assertEqual(stop_of(saved), ("invalid_output", "redo_exhausted"))
                self.assertEqual([task["id"] for task in saved["tasks"]], ["greet"])
                self.assertEqual(calls(backend, role="worker", task_id=name), [])

    def verification_failure(self):
        """The first whole-run review rejects and the decide adds the correction first; the second whole-run
        verification fails, and the decide adds second, whose worker's feedback holds only that failure."""
        attempts = []

        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                touch(kwargs, backend)
            elif kwargs["kind"] == "final_review" and len(calls(backend, kind="final_review")) == 1:
                result.update(verdict="request_changes", findings=[{**finding(), "id": "w1"}])
            elif trigger_kind(kwargs, packet) == "final":
                target = "first" if packet["trigger"]["final_review"]["status"] == "rejected" else "second"
                task = {**packet["original_tasks"][0], "id": target, "depends_on": []}
                revised(result, packet, "Perform the correction", new_tasks=[task])

        def verify(argv, cwd, log, **kwargs):
            if "final" in Path(log).parts:
                attempts.append(log)
                if len(attempts) == 2:
                    kwargs["on_start"](123)
                    Path(log).with_suffix(".stdout").write_text("")
                    Path(log).with_suffix(".stderr").write_text("Integration check failed")
                    kwargs["on_outcome"](ProcessOutcome(started=True, startup_state="started", pid=123, pgid=123,
                                                        exit_code=1, exit_confirmed=True, termination_reason="normal"))
                    return 1
            return run_process(argv, cwd, log, **kwargs)
        return ScriptedBackend(change), patch("harness.verification.run_process", side_effect=verify)

    def tampered_before_worker(self, target, original):
        """A task_attempt patch that changes the original ``original(run)`` names just before ``target``'s worker."""
        seen, actual = {}, TaskLifecycle.task_attempt

        def tamper(service, task, directive):
            if task["id"] == target and not seen:
                seen["artifact"] = artifact = Path(original(service.context.run))
                artifact.write_bytes(artifact.read_bytes() + b" ")
            return actual(service, task, directive)
        return patch.object(TaskLifecycle, "task_attempt", tamper), seen

    def test_a_failed_verification_carries_no_review_content(self):
        """Confirm review 1f3, blocker, case 1: the feedback of the correction after a failed whole-run
        verification holds that verification only (its record path is a whole-run entry's), so its worker
        traverses no history. With the rejected review's original changed, the worker is still called, as in
        1f2, and the run stops where the whole-run gate traverses that history, before any further call."""
        backend, failing = self.verification_failure()
        tamper, seen = self.tampered_before_worker("second", lambda run: run["final_history"][0]["review_source"]["artifact"])
        built = []

        def check(identity, packet, run, verified):
            if identity == ("worker", "implement", "second"):
                record = packet["feedback"]["trigger"]["final_review"]
                self.assertEqual((record["status"], record["artifact"]), ("verification_failed", run["final_history"][1]["artifact"]))
                self.assertTrue({"review", "review_source", "required_findings"}.isdisjoint(record))
                built.append(set(verified))
        engine = self.engine(backend)
        with failing, tamper, SourceCapture(check), self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        self.assertTrue(seen)
        self.assertEqual(built, [set()])
        self.assertEqual(stop_of(state(engine)), ("evidence", "runtime_integrity"))
        self.assertEqual([(call["role"], call["kind"], call["task_id"]) for call in backend.calls],
                         [("worker", "implement", "greet"), ("reviewer", "review", "greet"),
                          ("reviewer", "final_review", "final"), ("commander", "decide", "run"),
                          ("worker", "implement", "first"), ("reviewer", "review", "first"),
                          ("commander", "decide", "run"),
                          ("worker", "implement", "second"), ("reviewer", "review", "second")])

    def test_a_value_equal_to_an_unrelated_review_identity_widens_nothing(self):
        """Confirm review 1f3, blocker, case 2 and its variants: the independent fixup's allowed path, title,
        description and the decide's instruction name greet's approving review dispatch and original, and its
        worker's goal, a command argv and an environment note are given them too. It traverses only the
        whole-run history its feedback holds, so with greet's original changed the worker is still called, as
        in 1f2, and the run stops where the whole-run review's build traverses greet's history."""
        source, built = {}, []

        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                touch(kwargs, backend)
                if backend.calls[-1]["task_id"] == "fixup":
                    (Path(kwargs["cwd"]) / source["dispatch_id"]).write_text("ordinary file\n")
            elif kwargs["kind"] == "final_review" and len(calls(backend, kind="final_review")) == 1:
                result.update(verdict="request_changes", findings=[{**finding(), "id": "w1"}])
            elif trigger_kind(kwargs, packet) == "final":
                source.update(engine.run["accepted"]["greet"]["review_source"])
                ident, artifact = source["dispatch_id"], source["artifact"]
                task = {**packet["original_tasks"][0], "id": "fixup", "depends_on": [], "title": f"Write {ident}",
                        "description": f"Create the data file {ident} as {artifact} describes",
                        "allowed_paths": ["hello.py", ident]}
                revised(result, packet, "Add the requested file", new_tasks=[task],
                        instruction=f"Write {ident}; compare {artifact}")
        actual = packets.PacketBuilder.runtime_packet

        def planted(service, packet, *, role, kind, task_id):
            if (role, task_id) == ("worker", "fixup"):
                cited = [source["dispatch_id"], source["artifact"]]
                task = packet["task"]
                self.assertEqual((task["allowed_paths"][1], cited[0] in task["title"]), (cited[0], True))
                self.assertTrue(cited[1] in task["description"] and all(value in packet["instruction"] for value in cited))
                command = {"cwd": ".", "argv": ["python3", "-c", "pass", *cited]}
                packet = {**packet, "goal": " ".join([service.context.run["goal"], *cited]),
                          "verification_commands": [*(item for item in packet["verification_commands"] if item != command), command],
                          "environment_updates": [{"summary": f"Read {cited[1]}", "blocker_dispatch_id": cited[0]}]}
            return actual(service, packet, role=role, kind=kind, task_id=task_id)

        def check(identity, packet, run, verified):
            if identity == ("worker", "implement", "fixup"):
                built.append(set(verified))
        backend = ScriptedBackend(change)
        tamper, seen = self.tampered_before_worker("fixup", lambda run: run["accepted"]["greet"]["review_source"]["artifact"])
        engine = self.engine(backend)
        with patch.object(packets.PacketBuilder, "runtime_packet", planted), tamper, SourceCapture(check), \
                self.assertRaises(HarnessError):
            engine.start("Implement greet", plan(), scope=("**",))
        self.assertTrue(seen)
        self.assertEqual(built, [{("final", True)}])
        self.assertEqual(stop_of(state(engine)), ("evidence", "runtime_integrity"))
        self.assertEqual([(call["role"], call["kind"], call["task_id"]) for call in backend.calls],
                         [("worker", "implement", "greet"), ("reviewer", "review", "greet"),
                          ("reviewer", "final_review", "final"), ("commander", "decide", "run"),
                          ("worker", "implement", "fixup"), ("reviewer", "review", "fixup")])

    def test_every_table_position_names_a_traversed_history_and_none_lies_outside(self):
        """Over whole packets of a task worker, a task review, a correction's worker, the decide and the whole-run
        review: (a) every table position a packet holds review content at names, by its own source fields, a
        history its build traversed, and the progress and key index rows too, while the progress projection
        stays the call's own; (b) no review-shaped object lies outside the table. Only what these runs build is
        seen, and only objects with a review key: a bare summary, finding label or index elsewhere is not."""
        checked = []

        def check(identity, packet, run, verified):
            role, kind, task_id = identity
            owners, held = history_registry(run), {}
            self.assertEqual(unregistered(packet), [], identity)
            for row, path, value in table_positions(packet):
                histories = source_histories(row, value, owners)
                if histories is None:
                    self.assertEqual(row, "final_review", (identity, path))
                    continue
                self.assertTrue(histories, (identity, path))
                self.assertLessEqual(histories, verified, (identity, path))
                held[path] = histories
            self.assertLessEqual(index_histories(packet, owners), verified, identity)
            run_level = role == "commander" or kind == "final_review"
            scope = [*run["review_history"], *(["final"] if run["final_history"] else [])] if run_level else [task_id]
            if "finding_progress" in packet:
                self.assertEqual(packet["finding_progress"]["history_task_ids"], scope, identity)
            correction = role == "worker" and "revision" in (packet.get("feedback") or {})
            checked.append({"call": "correction" if correction else kind, "task_id": task_id, "held": held,
                            "rows": {row for row, _, _ in table_positions(packet)}, "packet": packet,
                            "verified": set(verified), "progress": "finding_progress" in packet})
        scenarios = (lambda: (self.correction_backend("commander", task_rejections=True), None),
                     lambda: (self.prerequisite_backend(), None), self.verification_failure)
        for number, scenario in enumerate(scenarios):
            if number:
                self.setUp()
            backend, failing = scenario()
            with failing or nullcontext(), SourceCapture(check):
                result = self.engine(backend).start("Implement greet", plan())
            self.assertEqual(result["status"], "completed")
        self.assertEqual({row["call"] for row in checked},
                         {"implement", "review", "correction", "decide", "final_review"})
        self.assertLessEqual({"prior_reviews", "required_findings", "dependencies", "accepted", "feedback",
                              "final_review"}, set().union(*(row["rows"] for row in checked)))
        # The correction's first worker holds the whole-run rejection and its dependency's approval and
        # traverses both histories, but projects no progress beyond its own (none yet).
        first = next(row for row in checked if row["call"] == "correction" and row["task_id"] == "commander")
        self.assertEqual(first["held"][("feedback", "trigger", "final_review")], {("final", True)})
        self.assertEqual(first["held"][("dependencies", "greet")], {("greet", False)})
        self.assertEqual(first["verified"], {("final", True), ("greet", False)})
        self.assertFalse(first["progress"])
        # Its next worker has its own progress, and that progress is still its own history alone.
        later = [row for row in checked if row["call"] == "implement" and row["task_id"] == "commander"]
        self.assertTrue(later and all(row["progress"] for row in later))
        # The prerequisite's worker holds greet's rejection through the saved task trigger.
        prep = next(row for row in checked if row["call"] == "correction" and row["task_id"] == "prep")
        self.assertEqual(prep["held"], {("feedback", "trigger", "feedback"): {("greet", False)}})
        self.assertEqual(prep["verified"], {("greet", False)})
        self.assertFalse(prep["progress"])
        # The correction after a failed verification holds no review content and traverses nothing.
        second = next(row for row in checked if row["call"] == "correction" and row["task_id"] == "second")
        self.assertEqual((second["held"], second["verified"]), ({}, set()))
        # The guard names a review-shaped object at a new position, including one nested in feedback.
        planted = copy.deepcopy(first["packet"])
        planted["notes"] = [{"verdict": "approve", "summary": "elsewhere"}]
        planted["feedback"]["trigger"]["extra"] = {"review_source": {"dispatch_id": "R"}}
        self.assertEqual(unregistered(planted), [("feedback", "trigger", "extra"), ("notes", 0)])

    def test_a_stale_ledger_is_refused_before_any_key_index_is_written(self):
        """Code review 1f, suggestion: a ledger another writer changed publishes no index for the next worker."""
        backend, seen = scripted([1, 1, 1]), {}
        build = packets.PacketBuilder.runtime_packet

        def stale(service, packet, *, role, kind, task_id):
            if role == "worker" and len(service.context.run["review_history"].get("greet", [])) == 3 and not seen:
                seen["indexes"] = index_files(engine)
                ledger = service.context.directory / "state.json"
                ledger.write_bytes(ledger.read_bytes() + b"\n")
            return build(service, packet, role=role, kind=kind, task_id=task_id)
        engine = self.engine(backend)
        with patch.object(packets.PacketBuilder, "runtime_packet", stale), self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        # The stop path refuses to overwrite the other writer's ledger too.
        self.assertEqual((caught.exception.stop["kind"], caught.exception.stop["reason"]), ("runtime_failure", "invariant"))
        self.assertEqual(len(worker_calls(backend)), 3)
        self.assertEqual(index_files(engine), seen["indexes"])


class SavedSourceTests(unittest.TestCase):
    """The histories a task-level packet carries, read from saved feedback shapes the runs above do not reach."""

    def test_kind_and_source_fields_name_the_histories_and_nothing_else_does(self):
        original = "/run/dispatches/R1/returned-result.json"
        record = "/run/verification/tasks/greet/1/review.json"
        review = {"verdict": "request_changes", "summary": "s", "findings": []}
        reviewed = {"attempt": 1, "status": "rejected", "artifact": "/run/verification/final/1/final.json",
                    "review": review, "review_source": {"dispatch_id": "R1", "artifact": original}, "required_findings": []}
        failed = {"attempt": 2, "status": "verification_failed", "artifact": "/run/verification/final/2/final.json",
                  "verification_failures": [{"command": {"cwd": ".", "argv": ["check", "R1"]}, "log": original}]}
        task_feedback = {"task_id": "greet", "review": review, "artifact": record}
        # A correction's worker after an invalid output: the next revision's task trigger holds the earlier
        # revision's feedback, whose final trigger holds the whole-run rejection; its recovery is fix's.
        nested = {"revision": 3, "plan_artifact": "/run/plans/3.json", "trigger": {
            "kind": "task", "ready": ["fix"], "current_task": "fix",
            "recovery": {"relation": "latest_worker_failure", "latest": {"review": {"dispatch_id": "R9", "verdict": "approve"}}},
            "feedback": {"revision": 2, "plan_artifact": "/run/plans/2.json",
                         "trigger": {"kind": "final", "final_review": reviewed, "accepted_since": []}}}}
        cases = [
            ({"feedback": task_feedback}, {("greet", False)}),
            ({"feedback": {"task_id": "greet", "review_in_prior_reviews": True, "artifact": record}}, {("greet", False)}),
            ({"feedback": {"task_id": "greet", "acceptance": {"error": "e", "review_artifact": record}}}, {("greet", False)}),
            ({"feedback": {"final_review": reviewed}}, {("final", True)}),
            ({"feedback": {"final_review": failed}}, set()),
            ({"feedback": {"revision": 1, "plan_artifact": "/run/plans/1.json",
                           "trigger": {"kind": "final", "final_review": failed, "accepted_since": ["greet"]}}}, set()),
            ({"feedback": {"revision": 1, "plan_artifact": "/run/plans/1.json",
                           "trigger": {"kind": "task", "ready": ["prep"], "current_task": "greet",
                                       "feedback": task_feedback, "recovery": None}}}, {("greet", False)}),
            ({"feedback": nested}, {("final", True), ("fix", False)}),
            # Kinds that are not review content, holding the same paths and dispatch.
            ({"feedback": {"task_id": "greet", "preflight": {"failures": [original]}, "artifact": record}}, set()),
            ({"feedback": {"task_id": "greet", "verification": failed["verification_failures"],
                           "verification_changes": {"artifact": original}}}, set()),
            ({"feedback": {"task_id": "greet", "recovery": {"error": "R1", "verification": record}}}, set()),
            ({"feedback": {"task_id": "greet", "worker": {"summary": original, "evidence": ["R1"]},
                           "worker_artifact": record}}, set()),
            ({"goal": original, "instruction": "R1", "task": {"title": "R1", "allowed_paths": ["R1", record]},
              "verification_commands": [{"cwd": ".", "argv": ["check", original]}], "environment_updates": [{"summary": record}],
              "prior_reviews": [{"review": review, "review_source": {"dispatch_id": "R2"}}]}, set()),
            ({"dependencies": {"greet": {"summary": "s", "details": record}, "other": {"summary": "t", "details": "R1"}}},
             {("greet", False)}),
        ]
        run = {"accepted": {"greet": {"summary_source": "reviewer"}, "other": {"summary_source": "operator"}}}
        for number, (packet, expected) in enumerate(cases):
            with self.subTest(case=number):
                self.assertEqual(packets.carried_histories(run, packet), expected)


class CompactShapeTests(unittest.TestCase):
    """Rule 14 on hand-written histories: what compact_reviews keeps, and the index publication itself."""

    def record(self, ident, diff, **extra):
        review = {"verdict": "approve", "summary": f"review {ident}", "findings": [],
                  "finding_dispositions": {"f1": {"status": "resolved", "evidence": ["checked"]}}}
        return {"dispatch_id": ident, "snapshot_digest": f"S-{diff}", "artifact": f"/run/{ident}/review.json",
                "diff_artifact": f"/run/{diff}/change.diff", "verification": f"/run/{diff}/results.json",
                "worker_source": {"dispatch_id": f"w-{diff}", "artifact": f"/run/w-{diff}.json", "artifact_digest": "d"},
                "required_findings": [{"key": "f1", "review_dispatch_id": "R0", "finding_index": 0,
                                       "label": "x", "artifact": "/run/R0.json"}],
                "review": review, "review_source": {"dispatch_id": ident, "artifact": f"/run/{ident}.json",
                                                    "artifact_digest": "a", "snapshot_digest": f"S-{diff}"}, **extra}

    def test_same_candidate_references_chain_to_the_latest_batch(self):
        three = [self.record("B1", "c"), self.record("B2", "c"), self.record("B3", "c")]
        invalidated = self.record("B4", "c", status="invalidated")
        invalidated.pop("worker_source")
        history = [self.record("A1", "a"), *three, invalidated]
        before = copy.deepcopy(history)
        compacted = compact_reviews(history)
        self.assertEqual(history, before)
        self.assertEqual([item.get("same_candidate_as") for item in compacted], [None, "B2", "B3", "B4", None])
        # Fields equal to the next record's are cited there only; one the next lacks stays here.
        self.assertEqual([sorted(set(item) & {"diff_artifact", "verification", "worker_source"}) for item in compacted],
                         [["diff_artifact", "verification", "worker_source"], [], [], ["worker_source"],
                          ["diff_artifact", "verification"]])
        self.assertEqual(compacted[3]["worker_source"], three[2]["worker_source"])
        # Top-level identities equal to review_source's are dropped; only the latest keeps its mapping.
        self.assertFalse([item for item in compacted if "dispatch_id" in item or "snapshot_digest" in item])
        self.assertEqual([("required_findings" in item, "finding_dispositions" in item["review"]) for item in compacted],
                         [(False, False)] * 4 + [(True, True)])
        # An earlier record's digest is still of its complete review.
        self.assertEqual(compacted[0]["review"]["review_content_sha256"], review_digest(history[0]["review"]))
        # A differing identity is not a duplicate, and stays.
        history[0]["snapshot_digest"] = "other"
        self.assertEqual(compact_reviews(history)[0]["snapshot_digest"], "other")

    def test_the_index_is_published_only_with_records_and_merges_existing_indexes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            packet = {"prior_reviews": []}
            self.assertIs(input_projection.project_review_keys(packet, root, []), packet)
            records = [{"review_dispatch_id": "R1", "snapshot_digest": "S", "artifact": "/raw.json",
                        "artifact_digest": "a", "required_findings": [], "finding_dispositions": {}}]
            other = {"input_evidence_indexes": {}}
            first = input_projection.project_review_keys(other, root, records)
            reference = first["prior_review_keys_index"]
            self.assertEqual(reference["count"], 1)
            artifact = Path(first["input_evidence_indexes"][reference["input_index"]]["artifact"])
            self.assertEqual(json.loads(artifact.read_bytes()), {"version": 1, "records": records})
            # Projecting again is the same packet; another index already in the packet is kept.
            self.assertEqual(input_projection.project_review_keys(first, root, records), first)
            kept = {**first["input_evidence_indexes"]}
            again = input_projection.project_review_keys({"input_evidence_indexes": kept}, root, [*records, *records])
            self.assertEqual(set(again["input_evidence_indexes"]),
                             {reference["input_index"], again["prior_review_keys_index"]["input_index"]})


if __name__ == "__main__":
    unittest.main()
