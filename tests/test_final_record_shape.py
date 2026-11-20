"""Whole-run record shapes are checked when the ledger is loaded (design 5, K5).

A record whose keys its readers index directly is missing or malformed, or whose review the
decide trigger projects before any traversal authenticates it, is refused by every load as
evidence/load_integrity: resume and abandon exit 1, no model is called and the ledger keeps its bytes.
Every record a writer leaves still loads.
"""
import copy
import os

from harness import stops
from harness.runtime import Runtime
from harness.util import HarnessError, read_json, write_json
from test_final_attempt_switch import before_judgment
from test_harness import RepoCase, crash_after_save, refused_by_cli
from test_runtime_changes import ScriptedBackend, finding
from test_stop_exits import final_failure_scenario, plan


def calls(backend, kind):
    return [call for call in backend.calls if call["kind"] == kind]


def pop(key):
    return lambda record: record.pop(key)


def put(**values):
    return lambda record: record.update(values)


class FinalRecordShapeTests(RepoCase):
    def assert_refused_at_load(self, backend, engine, ledger, field):
        """load_run, then resume and abandon through the real CLI, refuse ``ledger`` and name ``field``."""
        path = engine.directory / "state.json"
        write_json(path, ledger)
        before, model_calls = path.read_bytes(), len(backend.calls)
        with self.assertRaises(HarnessError) as caught:
            Runtime(self.config, self.repo, backend=backend).load_run(engine.run["id"])
        value = stops.of(caught.exception)
        self.assertEqual((value["kind"], value["reason"]), ("evidence", "load_integrity"))
        self.assertIn(field, str(caught.exception))
        for command in ("resume", "abandon"):
            refused_by_cli(self, backend, [command, engine.run["id"]], ("evidence", "load_integrity"), 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(backend.calls), model_calls)

    def each_damage(self, backend, engine, index, damages):
        clean = read_json(engine.directory / "state.json")
        for name, field, damage in damages:
            with self.subTest(name):
                ledger = copy.deepcopy(clean)
                damage(ledger["final_history"][index])
                self.assert_refused_at_load(backend, engine, ledger, f"final_history[{index}]{field}")
        write_json(engine.directory / "state.json", clean)
        Runtime(self.config, self.repo, backend=backend).load_run(engine.run["id"])   # the undamaged record loads

    def test_a_verification_failure_with_a_missing_or_malformed_key_is_refused_at_load(self):
        """Today the readers meet these as a KeyError (same_snapshot_record, needs_decision) or AttributeError (compact_checks)."""
        backend, engine, _ = final_failure_scenario(self)
        record = read_json(engine.directory / "state.json")["final_history"][0]
        self.assertEqual((record["status"], record["phase"], record["failure_kind"]),
                         ("verification_failed", "finished", "exit_code"))
        round_, check = record["verification_rounds"][0], record["verification_failures"][0]
        damages = [
            # Table K5-1: the (status, phase) pairs and the keys every writer of the pair writes.
            ("a pair no writer leaves", ".status/phase", put(phase="reviewing")),
            ("a phase that is no text", ".status/phase", put(phase=["finished"])),
            ("no phase", ".status/phase", pop("phase")),
            ("running/reviewing without its mapping", ".required_findings", put(status="running", phase="reviewing")),
            *((f"no {key}", f".{key}", pop(key)) for key in (
                "attempt", "head", "snapshot_digest", "snapshot_artifact", "artifact", "accepted_tasks",
                "verification_rounds", "verification", "diff_artifact", "failure_kind", "verification_failures")),
            # Table K5-2: wherever a key appears, its value.
            ("attempt 0", ".attempt", put(attempt=0)),
            ("attempt true", ".attempt", put(attempt=True)),
            ("attempt as text", ".attempt", put(attempt="1")),
            ("head null", ".head", put(head=None)),
            ("snapshot_digest a number", ".snapshot_digest", put(snapshot_digest=7)),
            ("artifact null", ".artifact", put(artifact=None)),
            ("snapshot_artifact a list", ".snapshot_artifact", put(snapshot_artifact=["x"])),
            ("accepted_tasks [1]", ".accepted_tasks", put(accepted_tasks=[1])),
            ("accepted_tasks a text", ".accepted_tasks", put(accepted_tasks="greet")),
            ("verification_rounds [null]", ".verification_rounds", put(verification_rounds=[None])),
            ("a round without its directory", ".verification_rounds",
             put(verification_rounds=[{"status": "completed"}])),
            ("a round of an unknown status", ".verification_rounds",
             put(verification_rounds=[{**round_, "status": "done"}])),
            ("a checkout digest that is no text", ".verification_rounds",
             put(verification_rounds=[{**round_, "checkout_digest": 5}])),
            ("verification null", ".verification", put(verification=None)),
            ("diff_artifact a number", ".diff_artifact", put(diff_artifact=3)),
            ("an unknown failure kind", ".failure_kind", put(failure_kind="crash")),
            ("verification_failures [null]", ".verification_failures", put(verification_failures=[None])),
            ("verification_failures an object", ".verification_failures", put(verification_failures={})),
            ("a check whose stderr is null", ".verification_failures",
             put(verification_failures=[{**check, "stderr": None}])),
            ("a check whose log is a number", ".verification_failures",
             put(verification_failures=[{**check, "log": 1}])),
            ("a check whose outcome is a list", ".verification_failures",
             put(verification_failures=[{**check, "process_outcome": []}])),
            ("required_findings a text", ".required_findings", put(required_findings="f1")),
            ("review_source a list", ".review_source", put(review_source=[])),
            ("reason a number", ".reason", put(reason=5)),
        ]
        self.each_damage(backend, engine, 0, damages)

    def rejected_before_the_decide(self):
        """The whole-run review rejects and the commander's decide fails at the provider: no answer, no preflight."""
        def reject(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result.update(verdict="request_changes", findings=[finding()])
        seen = {"final": False}

        def provider_down(backend, kwargs, packet):
            if kwargs["kind"] == "final_review":
                seen["final"] = True
            elif seen["final"] and kwargs["kind"] == "decide":
                seen["final"] = False
                raise HarnessError("provider down", stop=stops.make("provider_failure", "unknown"))
        backend = ScriptedBackend(reject)
        backend.before_result = provider_down
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual((saved["stop"]["kind"], saved["stop"]["reason"]), ("provider_failure", "unknown"))
        self.assertEqual([record["status"] for record in saved["final_history"]], ["rejected"])
        self.assertEqual((saved["operator_decisions"], saved["environment_updates"]), ([], []))
        return backend, engine

    def test_a_review_the_decide_trigger_projects_is_refused_at_load(self):
        """Today resume projects findings [null] into a TypeError (compact_review) before any original is authenticated."""
        backend, engine = self.rejected_before_the_decide()
        review = read_json(engine.directory / "state.json")["final_history"][0]["review"]

        def review_with(**values):
            return lambda record: record["review"].update(values)

        def finding_without(key):
            return lambda record: record["review"]["findings"][0].pop(key)
        batch = {"required_findings": [], "review": {**review, "findings": "edge-case"}}
        damages = [
            ("findings [null] and a 10,000-character summary", ".review.findings[0]",
             review_with(findings=[None], summary="x" * 10000)),
            ("a finding without its severity", ".review.findings[0]", finding_without("severity")),
            ("a finding whose evidence is a text", ".review.findings[0].evidence",
             lambda record: record["review"]["findings"][0].update(evidence="see the test")),
            ("a summary that is a number", ".review.summary", review_with(summary=7)),
            ("an unknown verdict", ".review.verdict", review_with(verdict="maybe")),
            ("a projection's digest key", ".review", review_with(review_content_sha256="0" * 64)),
            ("dispositions as a list", ".review.finding_dispositions", review_with(finding_dispositions=[])),
            ("a review that is a list", ".review", put(review=[])),
            ("an approved batch whose findings are a text", ".approved_batches[0].review.findings",
             lambda record: record["approved_batches"].append(batch)),
            # Table K5-1 for a concluded record.
            ("an unknown status", ".status/phase", put(status="failed")),
            ("a status that is no text", ".status/phase", put(status=["rejected"])),
            ("no review_source", ".review_source", pop("review_source")),
            ("no required_findings", ".required_findings", pop("required_findings")),
            ("no verification", ".verification", pop("verification")),
            ("an invalidated record without its reason", ".reason", put(status="invalidated")),
        ]
        self.each_damage(backend, engine, 0, damages)

    def test_every_writer_leaves_a_record_that_loads(self):
        """The conclusion's invalid rejection (no reason), a leave's readable invalid review, an approval whose
        memory approvals judge_review skips: each is saved, and the ledger loads."""
        with self.subTest("invalid rejection at the conclusion"):
            def invalid_veto(backend, kwargs, packet, result):
                if kwargs["kind"] == "final_review":
                    result.update(verdict="request_changes", findings=[finding()], summary="x" * 4001)
            backend = ScriptedBackend(invalid_veto)
            engine = self.engine(backend)
            crash, fired = crash_after_save(engine, lambda run: [r["status"] for r in run["final_history"]] == ["invalid_rejected"])
            with crash, self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", plan())
            self.assertTrue(fired["fired"])
            record = read_json(engine.directory / "state.json")["final_history"][0]
            self.assertTrue(record["validation_error"])
            self.assertNotIn("reason", record)
            Runtime(self.config, self.repo, backend=backend).load_run(engine.run["id"])
        with self.subTest("a leave keeps a readable invalid review"):
            self.setUp()

            def long_first_approval(backend, kwargs, packet, result):
                if kwargs["kind"] == "final_review" and len(calls(backend, "final_review")) == 1:
                    result["summary"] = "x" * 4001
            backend = ScriptedBackend(long_first_approval)
            engine = self.engine(backend)
            with before_judgment(backend, 1), self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", plan())
            os.chmod(engine.worktree / "hello.py", 0o664)       # the candidate changes: the attempt is left
            result = self.engine(backend).resume(engine.run["id"])
            self.assertEqual([r["status"] for r in result["final_history"]], ["invalidated", "approved"])
            left = result["final_history"][0]
            self.assertEqual((left["review"]["verdict"], bool(left["validation_error"])), ("approve", True))
            Runtime(self.config, self.repo, backend=backend).load_run(engine.run["id"])
        with self.subTest("memory approvals judge_review skips"):
            self.setUp()

            def odd_memory(backend, kwargs, packet, result):
                if kwargs["kind"] == "final_review":
                    result["approved_memory_keys"] = "not-a-list"
            backend = ScriptedBackend(odd_memory)
            engine = self.engine(backend)
            self.assertEqual(engine.start("Implement greet", plan())["status"], "completed")
            record = read_json(engine.directory / "state.json")["final_history"][0]
            self.assertEqual((record["status"], record["review"]["approved_memory_keys"]), ("approved", "not-a-list"))
            Runtime(self.config, self.repo, backend=backend).load_run(engine.run["id"])
