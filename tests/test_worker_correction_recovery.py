from pathlib import Path
from unittest.mock import patch

from harness.util import HarnessError, read_json, write_json
from test_engine_repairs import RecordingBackend
from test_harness import ROOT, RepoCase


class WorkerCorrectionRecoveryTests(RepoCase):
    correction_key = "worker:implement:greet"

    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    @staticmethod
    def workers(backend):
        return [call for call in backend.calls if call["role"] == "worker"]

    @staticmethod
    def worker_requests(backend):
        return [request for request in backend.requests if request["role"] == "worker"]

    def assert_accounting(self, backend, state, attempts):
        self.assertEqual(state["attempts"], {"greet": attempts})
        self.assertEqual(state["total_attempts"], attempts)
        self.assertEqual(state["agent_dispatches"], len(backend.requests))
        self.assertEqual(state["format_repairs"], {self.correction_key: 1})
        requests = self.worker_requests(backend)
        self.assertEqual(len(requests), attempts)
        self.assertEqual(len({request["dispatch_id"] for request in requests}), attempts)
        self.assertEqual(len({request["job"] for request in requests}), attempts)
        self.assertTrue(all(request["model"] == self.config.role("worker") for request in requests))

    def test_exhausted_correction_resumes_as_ordinary_attempts_with_latest_failure(self):
        self.config.data["runtime"].update(max_attempts=4, max_total_attempts=4)

        def malformed(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["summary"] = "x" * 4001

        backend = RecordingBackend(malformed)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assert_accounting(backend, saved, 2)
        self.assertEqual(saved["pending_format_corrections"], {})
        self.assertEqual(saved["accepted"], {})
        self.assertEqual(self.workers(backend)[1]["packet"]["format_correction"]["previous_dispatch"],
                         self.worker_requests(backend)[0]["dispatch_id"])

        for attempts in (3, 4):
            latest = self.worker_requests(backend)[-1]
            first_new_call = len(backend.calls)
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(saved["id"])
            saved = read_json(engine.directory / "state.json")
            self.assert_accounting(backend, saved, attempts)
            self.assertEqual(saved["pending_format_corrections"], {})
            self.assertEqual(saved["accepted"], {})
            for call in backend.calls[first_new_call:]:
                self.assertNotIn("format_correction", call["packet"])
                self.assertEqual(call["packet"]["recovery"]["dispatch_id"], latest["dispatch_id"])
                self.assertEqual(call["packet"]["recovery"]["artifact"], str(latest["job"]))
            self.assertEqual(saved["worker_recovery"]["greet"]["dispatch_id"],
                             self.worker_requests(backend)[-1]["dispatch_id"])

        self.assertEqual(sum("format_correction" in call["packet"] for call in self.workers(backend)), 1)
        calls = list(backend.calls)
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(saved["id"])
        self.assertEqual(backend.calls, calls)
        self.assert_accounting(backend, read_json(engine.directory / "state.json"), 4)

    def test_returned_identity_or_semantic_error_clears_pending_correction(self):
        for invalid_kind in ("identity", "semantics"):
            with self.subTest(invalid_kind=invalid_kind):
                def invalid_correction(backend, kwargs, packet, result):
                    if kwargs["role"] != "worker":
                        return
                    attempt = len(self.workers(backend))
                    if attempt == 1:
                        result["summary"] = "x" * 4001
                    elif attempt == 2:
                        if invalid_kind == "identity":
                            result["dispatch_id"] = "another-dispatch"
                        else:
                            result["blocker_kind"] = "environment"

                backend = RecordingBackend(invalid_correction)
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start("Implement greet", self.plan())
                saved = read_json(engine.directory / "state.json")
                self.assert_accounting(backend, saved, 2)
                self.assertEqual(saved["pending_format_corrections"], {})
                self.assertEqual(saved["accepted"], {})
                failed = self.worker_requests(backend)[-1]
                artifact = failed["job"] / "returned-result.json"
                evidence = artifact.read_bytes()
                result = self.engine(backend).resume(saved["id"])
                self.assertEqual(result["status"], "completed")
                self.assert_accounting(backend, result, 3)
                worker = self.workers(backend)[-1]
                self.assertNotIn("format_correction", worker["packet"])
                self.assertEqual(worker["packet"]["recovery"]["dispatch_id"], failed["dispatch_id"])
                self.assertEqual(artifact.read_bytes(), evidence)
                self.assertEqual(result["pending_format_corrections"], {})

    def test_correction_cli_failure_without_returned_result_remains_retryable(self):
        def unavailable_correction(backend, kwargs, packet, result):
            if kwargs["role"] != "worker":
                return
            attempt = len(self.workers(backend))
            if attempt == 1:
                result["summary"] = "x" * 4001
            elif attempt == 2:
                raise HarnessError("worker correction service unavailable")

        backend = RecordingBackend(unavailable_correction)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "correction service unavailable"):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assert_accounting(backend, saved, 2)
        failed = self.worker_requests(backend)[-1]
        pending = saved["pending_format_corrections"][self.correction_key]
        self.assertEqual(pending["dispatch_id"], failed["dispatch_id"])
        self.assertFalse((failed["job"] / "returned-result.json").exists())
        self.assertEqual(saved["accepted"], {})
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assert_accounting(backend, result, 3)
        worker = self.workers(backend)[-1]
        self.assertEqual(worker["packet"]["format_correction"], pending["correction"])
        self.assertEqual(worker["packet"]["recovery"]["dispatch_id"], failed["dispatch_id"])
        self.assertEqual(result["pending_format_corrections"], {})

    def test_atomic_return_before_pending_cleanup_does_not_replay_correction(self):
        def malformed_twice(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and len(self.workers(backend)) <= 2:
                result["summary"] = "x" * 4001

        backend = RecordingBackend(malformed_twice)
        engine = self.engine(backend)
        interrupted_artifact = None

        def write_then_interrupt(path, value):
            nonlocal interrupted_artifact
            write_json(path, value)
            if (interrupted_artifact is None and Path(path).name == "returned-result.json"
                    and len(self.worker_requests(backend)) == 2):
                interrupted_artifact = Path(path)
                raise KeyboardInterrupt()

        with patch("harness.dispatch.write_json", side_effect=write_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine.start("Implement greet", self.plan())
        self.assertIsNotNone(interrupted_artifact)
        evidence = interrupted_artifact.read_bytes()
        saved = read_json(engine.directory / "state.json")
        self.assert_accounting(backend, saved, 2)
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["accepted"], {})
        failed = self.worker_requests(backend)[-1]
        self.assertEqual(saved["pending_format_corrections"][self.correction_key]["dispatch_id"],
                         failed["dispatch_id"])

        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assert_accounting(backend, result, 3)
        self.assertEqual(result["pending_format_corrections"], {})
        worker = self.workers(backend)[-1]
        self.assertNotIn("format_correction", worker["packet"])
        self.assertEqual(worker["packet"]["recovery"]["dispatch_id"], failed["dispatch_id"])
        self.assertEqual(interrupted_artifact.read_bytes(), evidence)
        self.assertEqual(sum("format_correction" in call["packet"] for call in self.workers(backend)), 1)
