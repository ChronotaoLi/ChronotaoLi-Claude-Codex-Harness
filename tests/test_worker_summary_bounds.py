"""Worker summary length: its own bound, projected excerpts, and the remaining format gate."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from harness.adapters import provider_schema
from harness.contracts import (DETAIL, REVIEW, WORK, envelope, output_constraints,
                               validate_review)
from harness.context import WORKER_CONTEXT_CHARS
from harness.util import dump, HarnessError, read_json, write_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend, validate_work_contract


def work(**fields):
    return {"status": "completed", "summary": "done", "evidence": ["hello.py"], "blockers": [],
            "blocker_kind": "none", "proposed_memories": [], **fields}


def events(engine):
    return [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]


class WorkerSummaryContractTests(unittest.TestCase):
    def test_summary_has_its_own_bound_while_shared_detail_fields_keep_theirs(self):
        validate_work_contract(work(summary="s" * 32000))
        with self.assertRaisesRegex(HarnessError, "response.summary"):
            validate_work_contract(work(summary="s" * 32001))
        with self.assertRaisesRegex(HarnessError, "response.summary"):
            validate_work_contract(work(summary=" " * 10))
        for field in ("evidence", "blockers"):
            with self.subTest(field=field), self.assertRaisesRegex(HarnessError, f"response.{field}"):
                validate_work_contract(work(**{field: ["e" * 4001]},
                                   **({"status": "blocked", "blocker_kind": "task"} if field == "blockers" else {})))
        self.assertEqual(DETAIL["maxLength"], 4000)
        with self.assertRaisesRegex(HarnessError, "response.summary"):
            validate_review({"verdict": "approve", "summary": "r" * 4001, "findings": [], "approved_memory_keys": []})
        self.assertIs(REVIEW["properties"]["summary"], DETAIL)

    def test_exposed_constraints_and_provider_schema_follow_the_new_bound(self):
        contract = envelope(WORK, "greet", "dispatch-1")
        constraints = {item["path"]: item for item in output_constraints(contract)}
        self.assertEqual(constraints["response.summary"]["maxLength"], 32000)
        self.assertTrue(constraints["response.summary"]["non_blank"])
        self.assertEqual(constraints["response.blockers[]"]["maxLength"], 4000)
        self.assertEqual(constraints["response.evidence[]"]["maxLength"], 4000)
        for strict in (False, True):
            wire = provider_schema(contract, strict=strict)
            self.assertEqual(wire["properties"]["summary"], {"type": "string"})


class WorkerSummaryLifecycleTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def test_field_test_summary_length_reaches_verification_as_a_valid_result(self):
        summary = ("Implemented greet and recorded the verification evidence. " * 80)[:4484]
        self.assertEqual(len(summary), 4484)

        def long_summary(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["summary"] = summary
        backend = ScriptedBackend(long_summary)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 1})
        self.assertEqual([call["role"] for call in backend.calls].count("worker"), 1)
        names = [event["event"] for event in events(engine)]
        self.assertNotIn("output_invalid", names)
        workers = [d for d in result["dispatches"] if d["role"] == "worker"]
        self.assertEqual(len(workers), 1)
        self.assertEqual(workers[0]["status"], "completed")
        worker_index = result["dispatches"].index(workers[0])
        self.assertEqual(result["dispatches"][worker_index + 1]["role"], "verification")
        returned = read_json(engine.directory / "dispatches" / workers[0]["id"] / "returned-result.json")
        self.assertEqual(returned["summary"], summary)

    def test_next_commander_input_excerpts_long_summary_with_full_reference(self):
        for size in (4484, 31000):
            with self.subTest(size=size):
                self.setUp()
                summary = (f"Blocked {size}: prerequisite missing. " * 2000)[:size - 4] + "-END"
                seen = []

                def blocked_first(backend, kwargs, packet, result):
                    if kwargs["role"] == "worker" and engine.run["attempts"]["greet"] == 1:
                        result.update(status="blocked", blocker_kind="task", summary=summary,
                                      blockers=["A prerequisite is missing"])
                    elif kwargs["kind"] == "replan":
                        seen.append(packet["trigger"]["worker"])
                        result.update(action="revise", reason="Clarify prerequisite", new_tasks=[],
                                      updates=[{"id": "greet", "description": "Implement greet with the prerequisite",
                                                "add_depends_on": []}])
                backend = ScriptedBackend(blocked_first)
                engine = self.engine(backend)
                result = engine.start("Implement greet", self.plan())
                self.assertEqual(result["status"], "completed")
                self.assertNotIn("output_invalid", [event["event"] for event in events(engine)])
                excerpt, = seen
                self.assertTrue(summary.startswith(excerpt["summary"]))
                self.assertLessEqual(len(dump(excerpt)), WORKER_CONTEXT_CHARS)
                if size > 9000:
                    self.assertLess(len(excerpt["summary"]), size)
                    self.assertTrue(excerpt["details_truncated"])
                artifact = Path(excerpt["full_worker_artifact"])
                self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), excerpt["worker_artifact_sha256"])
                self.assertEqual(read_json(artifact)["summary"], summary)

    def test_environment_blocked_stop_error_is_a_bounded_excerpt_with_the_full_artifact_identity(self):
        summary = ("Sandbox refused the build step; details follow. " * 1000)[:31992 - 4] + "-END"
        self.assertEqual(len(summary), 31992)
        blocker = "The sandbox denied the required build command"
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        hooks = Path(outside.name)
        captured = hooks / "on_error-input.json"
        write_json(hooks / "hooks.json", {"on_error": [{"argv": [
            sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write(sys.stdin.read())", str(captured)]}]})
        self.config.data["shared"]["hooks"] = str(hooks / "hooks.json")

        def environment_blocked(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.update(status="blocked", blocker_kind="environment", summary=summary, blockers=[blocker])
        engine = self.engine(ScriptedBackend(environment_blocked))
        with self.assertRaisesRegex(HarnessError, "环境受阻"):
            engine.start("Implement greet", self.plan())
        error = engine.run["error"]
        stopped = [event for event in events(engine) if event["event"] == "run_stopped"]
        hook_error = json.loads(captured.read_text())["error"]
        self.assertEqual(read_json(engine.directory / "state.json")["error"], error)
        self.assertEqual([event["error"] for event in stopped], [error])
        self.assertEqual(hook_error, error)
        # The bound is structural, so it holds for any temp root: the blockers and
        # summary excerpt stays within 2,000 characters, and everything else is the
        # artifact path plus fixed wording, the truncation note and the digest.
        head, marker, rest = error.partition("worker 环境受阻：")
        excerpt, truncated, _ = rest.partition("……（已截断")
        self.assertEqual((head, marker, truncated), ("", "worker 环境受阻：", "……（已截断"))
        self.assertLessEqual(len(excerpt), 2000)
        self.assertTrue(f"{blocker} {summary}".startswith(excerpt))
        self.assertIn(blocker, excerpt)
        self.assertNotIn("-END", error)
        artifact = Path(engine.run["feedback"]["worker_artifact"])
        self.assertIn(str(artifact), error)
        self.assertIn(hashlib.sha256(artifact.read_bytes()).hexdigest(), error)
        fixed_allowance = 300  # fixed wording, truncation note and the 64-character SHA256
        self.assertLessEqual(len(error), 2000 + len(str(artifact)) + fixed_allowance)
        self.assertEqual(read_json(artifact)["summary"], summary)

    def test_summary_beyond_its_bound_is_an_invalid_output_that_spends_one_attempt(self):
        def overlong_first(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(call["role"] == "worker" for call in backend.calls) == 1:
                result["summary"] = "x" * 32001
        backend = ScriptedBackend(overlong_first)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        invalid = [event for event in events(engine) if event["event"] == "output_invalid"]
        self.assertEqual([(event["role"], event["task_id"]) for event in invalid], [("worker", "greet")])
        first = next(d for d in result["dispatches"] if d["role"] == "worker")
        self.assertEqual(first["status"], "invalid")
        self.assertIn("response.summary", first["error"])
        self.assertEqual(result["attempts"], {"greet": 2})


if __name__ == "__main__":
    unittest.main()
