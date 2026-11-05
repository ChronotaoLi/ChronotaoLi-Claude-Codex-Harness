"""Exercise service persistence barriers against the selected run authority."""
from harness.util import HarnessError, read_json
from test_harness import RepoCase


class RuntimeKernelTests(RepoCase):
    def run_state(self, ident):
        return {"id": ident, "tasks": [], "accepted": {}}

    def test_service_tracks_selected_run_across_new_run_and_reload(self):
        runtime = self.engine()
        runtime.run = self.run_state("run_one")
        runtime.services.planning.report_command_requirements()
        first = read_json(runtime.directory / "state.json")
        runtime.run = self.run_state("run_two")
        runtime.services.planning.report_command_requirements()
        second_path = runtime.directory / "state.json"
        self.assertEqual(read_json(second_path)["id"], "run_two")
        runtime.run = first
        runtime.run.pop("command_requirements")
        runtime.services.planning.report_command_requirements()
        self.assertEqual(read_json(runtime.directory / "state.json")["store_revision"], 2)
        self.assertEqual(read_json(second_path)["store_revision"], 1)

    def test_service_save_rejects_stale_authority_before_audit_event(self):
        runtime = self.engine()
        runtime.run = self.run_state("run_one")
        runtime.context.save()
        other = self.engine()
        other.run = read_json(runtime.directory / "state.json")
        runtime.services.planning.report_command_requirements()
        events_before = (runtime.directory / "events.jsonl").read_bytes()
        with self.assertRaisesRegex(HarnessError, "其他写入者"):
            other.services.planning.report_command_requirements()
        self.assertEqual((runtime.directory / "events.jsonl").read_bytes(), events_before)
        self.assertEqual(read_json(runtime.directory / "state.json")["store_revision"], 2)

    def test_save_observer_runs_after_durable_publication(self):
        runtime = self.engine()
        runtime.run = self.run_state("run_one")
        observed = []
        runtime.context.on_save = lambda run: observed.append(read_json(runtime.directory / "state.json"))
        runtime.services.planning.report_command_requirements()
        self.assertEqual(observed[0]["store_revision"], 1)
        self.assertEqual(observed[0]["command_requirements"], {"missing": {}})
