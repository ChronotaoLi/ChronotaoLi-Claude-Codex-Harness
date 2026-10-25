import copy
from unittest.mock import patch

from harness.util import read_json
from test_engine_repairs import RecordingBackend
from test_harness import RepoCase, ROOT


class OperationalRecoveryDetailsTests(RepoCase):
    def test_dispatch_retrieves_memory_for_all_ready_tasks(self):
        plan = read_json(ROOT / "examples" / "plan.json")
        second = copy.deepcopy(plan["tasks"][0])
        second.update(id="invoice", title="Serialize invoice", description="Serialize invoices")
        plan["tasks"].append(second)
        backend = RecordingBackend()
        engine = self.engine(backend)
        memory_id = engine.memory.remember(key="invoices", abstract="invoice serialization",
            overview="invoice serialization", detail="invoice serialization", source="operator")
        result = engine.start("Implement planned features", plan, step=True)
        self.assertEqual(result["status"], "paused")
        dispatch = next(request for request in backend.requests if request["kind"] == "dispatch")
        report = read_json(dispatch["job"] / "context-report.json")
        self.assertIn(memory_id, report["memory_ids"])

    def test_completed_resume_returns_saved_result_after_configuration_changes(self):
        backend = RecordingBackend()
        engine = self.engine(backend)
        completed = engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        ledger = (engine.directory / "state.json").read_bytes()
        calls = copy.deepcopy(backend.calls)
        with patch.object(self.config, "resume_changes", side_effect=AssertionError("must not revalidate completed config")), \
             patch("harness.run_lifecycle.asset_digest", return_value="changed assets"):
            result = self.engine(backend).resume(completed["id"])
        self.assertEqual(result, completed)
        self.assertEqual(backend.calls, calls)
        self.assertEqual((engine.directory / "state.json").read_bytes(), ledger)
