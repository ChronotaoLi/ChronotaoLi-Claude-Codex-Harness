"""Registered evidence and final-attempt recovery through ordinary owned state."""
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from harness import stops
from harness.recovery import bound_bytes, bound_result
from harness.state_files import StateFiles
from harness.util import EvidenceError, read_json
from test_harness import ROOT, RepoCase, crash_after_save
from test_runtime_changes import ScriptedBackend


class BoundEvidenceTests(unittest.TestCase):
    def test_registered_result_requires_the_expected_file_and_bytes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            artifact = root / "source.json"
            raw = b'{"value": true}'
            digest = hashlib.sha256(raw).hexdigest()
            files = StateFiles(root).under(artifact.name)
            source = {"artifact": str(artifact), "artifact_digest": digest}
            with self.assertRaises(EvidenceError):
                bound_bytes(files, source, "result")
            artifact.mkdir()
            with self.assertRaises(EvidenceError):
                bound_bytes(files, source, "result")
            artifact.rmdir()
            artifact.write_bytes(raw)
            self.assertEqual(bound_result(files, source, "result"), ({"value": True}, digest))
            with self.assertRaises(EvidenceError):
                bound_bytes(files, {**source, "artifact": str(root / "other.json")}, "result")
            with self.assertRaises(EvidenceError):
                bound_bytes(files, {**source, "artifact_digest": "0" * 64}, "result")

    def test_binding_precedes_decoding(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            artifact = root / "source.json"
            artifact.write_bytes(b"{")
            source = {"artifact": str(artifact)}
            saved = []
            with self.assertRaises(EvidenceError):
                bound_result(StateFiles(root).under(artifact.name), source, "result",
                             bind=lambda: saved.append(dict(source)))
            self.assertEqual(saved, [{**source, "artifact_digest": hashlib.sha256(b"{").hexdigest()}])

    def test_missing_first_read_never_binds(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            artifact = root / "returned-result.json"
            files = StateFiles(root).under(artifact.name)
            source = {"artifact": str(artifact)}
            saved = []
            with self.assertRaisesRegex(EvidenceError, "审阅原始结果缺失或无法读取") as caught:
                bound_bytes(files, source, "审阅", bind=lambda: saved.append(dict(source)))
            self.assertEqual(stops.of(caught.exception)["source"], {"artifact": str(artifact)})
            self.assertNotIn("artifact_digest", source)
            self.assertEqual(saved, [])


class FinalSnapshotRecoveryTests(RepoCase):
    def run_until_attempt(self, backend):
        engine = self.engine(backend)
        interrupt, saved = crash_after_save(engine, lambda run: bool(run["final_history"]))
        with interrupt, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertTrue(saved["fired"])
        ledger = read_json(engine.directory / "state.json")
        attempt, = ledger["final_history"]
        artifact = Path(attempt["snapshot_artifact"])
        self.assertTrue(artifact.parent.is_dir())
        self.assertEqual(list(artifact.parent.iterdir()), [])
        self.assertEqual(attempt["phase"], "verifying")
        return engine, artifact

    def test_resume_writes_missing_snapshot_and_completes_same_attempt(self):
        backend = ScriptedBackend()
        engine, artifact = self.run_until_attempt(backend)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        attempt, = result["final_history"]
        self.assertEqual((attempt["attempt"], attempt["status"]), (1, "approved"))
        self.assertEqual(len(attempt["verification_rounds"]), 1)
        self.assertTrue(artifact.is_file())
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 1)

    def test_attempt_directory_type_mismatch_stops_before_registration(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        make_directories = StateFiles.make_directories

        def require_directory(files, components):
            if components == ("verification", "final", "1"):
                (engine.directory / "verification" / "final").write_text("ordinary file")
            return make_directories(files, components)

        with patch.object(StateFiles, "make_directories", require_directory), self.assertRaises(EvidenceError):
            engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(read_json(engine.directory / "state.json")["final_history"], [])
        self.assertFalse(any(call["kind"] == "final_review" for call in backend.calls))
        self.assertEqual((engine.directory / "verification" / "final").read_text(), "ordinary file")

    def test_resume_reuses_directory_created_before_attempt_registration(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        make_directories = StateFiles.make_directories

        def interrupt(files, components):
            outcome = make_directories(files, components)
            if components == ("verification", "final", "1"):
                self.assertEqual(outcome.state, "present")
                raise KeyboardInterrupt()
            return outcome

        with patch.object(StateFiles, "make_directories", interrupt), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(read_json(engine.directory / "state.json")["final_history"], [])
        folder = engine.directory / "verification" / "final" / "1"
        self.assertEqual(list(folder.iterdir()), [])

        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        attempt, = result["final_history"]
        self.assertEqual(attempt["attempt"], 1)
        self.assertEqual(Path(attempt["artifact"]).parent, folder)

    def test_resume_retains_registration_and_stops_if_attempt_directory_is_missing(self):
        backend = ScriptedBackend()
        engine, artifact = self.run_until_attempt(backend)
        artifact.parent.rmdir()
        self.assert_stops_at_snapshot(engine, backend)
        self.assertFalse(artifact.parent.exists())

    def test_resume_retains_registration_and_stops_if_snapshot_is_a_directory(self):
        backend = ScriptedBackend()
        engine, artifact = self.run_until_attempt(backend)
        artifact.mkdir()
        self.assert_stops_at_snapshot(engine, backend)
        self.assertTrue(artifact.is_dir())

    def assert_stops_at_snapshot(self, engine, backend):
        ledger = engine.directory / "state.json"
        before = read_json(ledger)
        calls = list(backend.calls)
        with self.assertRaises(EvidenceError) as caught:
            self.engine(backend).resume(engine.run["id"])
        stop = stops.of(caught.exception)
        self.assertEqual((stop["kind"], stop["reason"]), ("evidence", "runtime_integrity"))
        self.assertEqual(read_json(ledger)["final_history"], before["final_history"])
        self.assertEqual(backend.calls, calls)
