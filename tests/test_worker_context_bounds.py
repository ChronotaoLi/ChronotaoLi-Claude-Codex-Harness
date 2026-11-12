import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from harness.context import WORKER_CONTEXT_CHARS, compact_feedback, compose, worker_view
from harness.util import HarnessError, dump, read_json, write_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend, validate_work_contract, revised, trigger_kind


def maximum_worker(blocker_kind="task"):
    def detail(label, size=4000):
        return (label + '\\\n"' * size)[:size - 8] + "END-TEXT"

    result = {
        "status": "blocked", "blocker_kind": blocker_kind,
        "summary": detail("Blocked summary ", 32000),
        "evidence": [detail(f"Evidence {index} ") for index in range(20)],
        "blockers": [detail(f"Blocker {index} ") for index in range(20)],
        "proposed_memories": [
            {"key": f"proposal-{index}", "abstract": detail("Abstract ", 500),
             "content": detail("Unapproved proposal ", 8000),
             "source_paths": [f"source-{item}-" + "x" * 490 for item in range(10)]}
            for index in range(8)],
    }
    validate_work_contract(result)
    return result


class BoundedWorkerLifecycleTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def assert_full_evidence(self, excerpt, original):
        self.assertTrue(excerpt["details_truncated"])
        self.assertEqual(excerpt["status"], original["status"])
        self.assertEqual(excerpt["blocker_kind"], original["blocker_kind"])
        self.assertEqual(excerpt["evidence_count"], len(original["evidence"]))
        self.assertEqual(excerpt["blocker_count"], len(original["blockers"]))
        self.assertEqual(excerpt["proposed_memory_keys"], [p["key"] for p in original["proposed_memories"]])
        self.assertNotIn("proposed_memories", excerpt)
        artifact = Path(excerpt["full_worker_artifact"])
        self.assertTrue(artifact.is_absolute())
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), excerpt["worker_artifact_sha256"])
        raw = read_json(artifact)
        self.assertEqual({key: value for key, value in raw.items() if key not in ("task_id", "dispatch_id")}, original)
        self.assertLessEqual(len(dump(excerpt)), WORKER_CONTEXT_CHARS)

    def test_maximum_task_blocker_reaches_decide_and_fresh_worker_under_default_budget(self):
        blocked = maximum_worker()
        sizes = []
        inspected = []

        def repair(backend, kwargs, packet, result):
            sizes.append(len(kwargs["prompt"]))
            if kwargs["role"] == "worker" and engine.run["attempts"]["greet"] == 1:
                # A blocked worker writes no commit message (the demo's completed one is dropped).
                result.pop("commit_message")
                result.update(copy.deepcopy(blocked))
            elif trigger_kind(kwargs, packet) == "task" and packet["trigger"]["feedback"]:
                self.assert_full_evidence(packet["trigger"]["feedback"]["worker"], blocked)
                inspected.append("commander")
                revised(result, packet, "Resolve prerequisite explicitly",
                        updates=[{"id": "greet", "description": "Implement greet after checking prerequisites",
                                  "add_depends_on": []}])
            elif kwargs["role"] == "worker":
                # The published revision's feedback carries the decision's trigger.
                self.assert_full_evidence(packet["feedback"]["trigger"]["feedback"]["worker"], blocked)
                inspected.append(kwargs["role"])
            elif kwargs["role"] == "reviewer":
                self.assertNotIn("worker", packet)
                self.assertNotIn("Unapproved proposal", dump(packet))

        backend = ScriptedBackend(repair)
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(inspected, ["commander", "worker"])
        self.assertLessEqual(max(sizes), 32000)

    def test_maximum_environment_blocker_survives_resume_and_stays_out_of_review(self):
        blocked = maximum_worker("environment")
        sizes = []
        inspected = []

        def repair(backend, kwargs, packet, result):
            sizes.append(len(kwargs["prompt"]))
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.pop("commit_message")
                result.update(copy.deepcopy(blocked))
            elif kwargs["kind"] == "decide":
                inspected.append("decide")
                self.assert_full_evidence(packet["trigger"]["feedback"]["worker"], blocked)
            elif kwargs["role"] == "worker":
                inspected.append("worker")
                self.assert_full_evidence(packet["feedback"]["worker"], blocked)
            elif kwargs["role"] == "reviewer":
                self.assertNotIn("worker_artifact", dump(packet))
                self.assertNotIn("Unapproved proposal", dump(packet))

        backend = ScriptedBackend(repair)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "环境受阻"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["feedback"]["worker"], blocked)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        # The environment update leads to one decide and the next worker, both with the full blocker.
        self.assertEqual(inspected, ["decide", "worker"])
        self.assertLessEqual(max(sizes), 32000)

    def test_next_attempt_source_and_full_proposals_are_retained_for_independent_review(self):
        proposal = {"key": "greeting", "abstract": "Greeting interface", "content": "greet accepts a name",
                    "source_paths": ["hello.py"]}
        corrected = []

        def repair(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                # The first output is invalid; the next ordinary attempt replaces it.
                if sum(call["role"] == "worker" for call in backend.calls) == 1:
                    result["summary"] = "x" * 32001
                else:
                    result["proposed_memories"] = [copy.deepcopy(proposal)]
                    corrected.append(result["dispatch_id"])
            elif kwargs["kind"] == "review":
                self.assertEqual(packet["proposed_memories"][0]["content"], proposal["content"])
                pending = engine.run["pending_task"]
                source = Path(pending["worker_artifact"])
                self.assertEqual(source.parent.name, corrected[0])
                self.assertEqual(read_json(source)["proposed_memories"], [proposal])
                self.assertEqual(pending["worker_result"]["proposed_memories"], [proposal])
                self.assertNotIn("worker_artifact", packet)

        engine = self.engine(ScriptedBackend(repair))
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(len(corrected), 1)

    def test_missing_or_changed_worker_artifact_blocks_resume_before_dispatch(self):
        blocked = maximum_worker("environment")

        def stop(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.pop("commit_message")
                result.update(copy.deepcopy(blocked))

        backend = ScriptedBackend(stop)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "环境受阻"):
            engine.start("Implement greet", self.plan())
        source = Path(engine.run["feedback"]["worker_artifact"])
        original = source.read_bytes()
        saved_state = (engine.directory / "state.json").read_bytes()
        calls = len(backend.calls)
        for mutation in ("missing", "changed"):
            with self.subTest(mutation=mutation):
                if mutation == "missing":
                    source.unlink()
                else:
                    source.write_bytes(original + b" ")
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                self.assertEqual(len(backend.calls), calls)
                self.assertEqual((engine.directory / "state.json").read_bytes(), saved_state)
                self.assertEqual(read_json(engine.directory / "state.json")["feedback"]["worker"], blocked)
                source.write_bytes(original)


class BoundedWorkerContextTests(unittest.TestCase):
    def test_nested_feedback_shares_serialized_budget_and_does_not_mutate_full_result(self):
        worker = maximum_worker()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        artifact = Path(temp.name) / "worker.json"
        write_json(artifact, worker)
        record = {"worker": worker, "worker_artifact": str(artifact),
                  "worker_artifact_digest": hashlib.sha256(artifact.read_bytes()).hexdigest()}
        feedback = {"trigger": copy.deepcopy(record), "prior": [copy.deepcopy(record)]}
        before = copy.deepcopy(feedback)
        compacted = compact_feedback(feedback)
        excerpts = [compacted["trigger"]["worker"], compacted["prior"][0]["worker"]]
        self.assertLessEqual(sum(len(dump(item)) for item in excerpts), WORKER_CONTEXT_CHARS)
        self.assertEqual(feedback, before)
        self.assertEqual(compact_feedback(compacted), compacted)
        self.assertTrue(all(item["blocker_count"] == 20 for item in excerpts))

    def test_without_complete_source_feedback_is_not_silently_truncated(self):
        worker = maximum_worker()
        for source in ({}, {"worker_artifact": "/run/worker.json"}, {"worker_artifact_digest": "a" * 64}):
            with self.subTest(source=source):
                compacted = compact_feedback({"worker": worker, **source})
                # Core evidence stays whole; proposals are projected as the keys of a valid batch.
                self.assertEqual(compacted["worker"], worker_view(worker))
                self.assertEqual(compacted["worker"]["proposed_memory_keys"],
                                 [proposal["key"] for proposal in worker["proposed_memories"]])
                with self.assertRaisesRegex(HarnessError, "上下文"):
                    compose(compacted, [], 32000)


if __name__ == "__main__":
    unittest.main()
