from harness.util import HarnessError, read_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend, finding


class ReviewCorrectionRecoveryTests(RepoCase):
    def backend_with_interrupted_final_correction(self, *, preserve_rejection):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] != "final_review":
                return
            number = sum(call["kind"] == "final_review" for call in backend.calls)
            if number == 1:
                result.update(verdict="request_changes", findings=[finding()], summary="x" * 4001)
            elif number == 2:
                raise KeyboardInterrupt()
            elif preserve_rejection:
                result.update(verdict="request_changes", findings=[finding()], summary="The recorded defect remains")
        return ScriptedBackend(change)

    def interrupted_run(self, backend):
        engine = self.engine(backend)
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(engine.run["status"], "interrupted")
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 2)
        return engine

    def test_resumed_final_correction_preserves_rejection_before_replanning(self):
        backend = self.backend_with_interrupted_final_correction(preserve_rejection=True)
        engine = self.interrupted_run(backend)
        head = engine.run["expected_head"]
        resumed = self.engine(backend)
        # Demo commander returns stop; the repaired rejection must reach it.
        with self.assertRaises(HarnessError):
            resumed.resume(engine.run["id"])
        self.assertEqual(resumed.run["expected_head"], head)
        self.assertEqual(resumed.run["final_history"][-1]["status"], "rejected")
        self.assertEqual(resumed.run["final_history"][-1]["review"]["findings"], [finding()])
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 3)
        self.assertEqual(sum(call["kind"] == "replan" for call in backend.calls), 1)
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)
        reviews = [entry["id"] for entry in resumed.run["dispatches"] if entry.get("kind") == "final_review"]
        self.assertEqual(len(reviews), 3)
        self.assertEqual(len(reviews), len(set(reviews)))

    def test_resumed_final_correction_cannot_reroll_or_retry_a_changed_verdict(self):
        backend = self.backend_with_interrupted_final_correction(preserve_rejection=False)
        engine = self.interrupted_run(backend)
        head = engine.run["expected_head"]
        resumed = self.engine(backend)
        with self.assertRaises(HarnessError):
            resumed.resume(engine.run["id"])
        self.assertEqual(resumed.run["status"], "blocked")
        self.assertEqual(resumed.run["expected_head"], head)
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 3)
        reviews = [entry for entry in resumed.run["dispatches"] if entry["role"] == "reviewer"]
        again = self.engine(backend)
        with self.assertRaises(HarnessError):
            again.resume(engine.run["id"])
        self.assertEqual(again.run["status"], "blocked")
        self.assertEqual([entry for entry in again.run["dispatches"] if entry["role"] == "reviewer"], reviews)
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 3)
        self.assertEqual(sum(call["role"] == "worker" for call in backend.calls), 1)

    def test_exhausted_format_repair_still_preserves_a_new_snapshot_rejection(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                number = sum(call["kind"] == "final_review" for call in backend.calls)
                if number in (1, 3):
                    result.update(verdict="request_changes", findings=[finding()], summary="x" * 4001)
                elif number == 2:
                    result.update(verdict="request_changes", findings=[finding()], summary="The defect remains")
            elif kwargs["kind"] == "replan":
                corrective = copy.deepcopy(packet["original_tasks"][0])
                corrective.update(id="correct", title="Correct finding", description="Address the defect",
                                  depends_on=["greet"])
                result.update(action="revise", reason="Address the final finding", new_tasks=[corrective], updates=[])
            elif kwargs["role"] == "worker" and kwargs["task"]["id"] == "correct":
                with (kwargs["cwd"] / "hello.py").open("a") as output:
                    output.write("# Independently reviewed correction\n")

        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(set(engine.run["accepted"]), {"greet", "correct"})
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 3)
        head = engine.run["expected_head"]
        resumed = self.engine(backend)
        with self.assertRaises(HarnessError):
            resumed.resume(engine.run["id"])
        self.assertEqual(resumed.run["status"], "blocked")
        self.assertEqual(resumed.run["expected_head"], head)
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 4)


if __name__ == "__main__":
    import unittest
    unittest.main()
import copy

