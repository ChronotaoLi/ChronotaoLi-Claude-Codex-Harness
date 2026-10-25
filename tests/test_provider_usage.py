import copy
import json
import sys
from unittest.mock import patch

from harness.adapters import CLIBackend, provider_usage
from harness.shared import HookError
from harness.util import HarnessError, read_json
from test_harness import RepoCase, ROOT


# Exercise the actual synchronous adapter and engine with local subprocesses.
# These are representative CLI envelopes, not proof of a live model call.
FIXTURE_PROCESS = r'''
import json
import sys
from pathlib import Path

role, provider, directory, encoded, response = sys.argv[1:]
options, result = json.loads(encoded), json.loads(response)
sys.stdin.read()
job = Path(directory)
if role == "worker":
    Path("hello.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n')
if options.get("invalid_result"):
    result.pop("status", None)
if provider == "claude":
    envelope = {"type": "result", "subtype": "success", "is_error": False,
                "structured_output": result,
                "total_cost_usd": 0.25, "num_turns": 3,
                "usage": {"input_tokens": 20, "output_tokens": 5,
                          "cache_creation_input_tokens": 10, "cache_read_input_tokens": 40}}
    envelope.update(options.get("envelope", {}))
    if options.get("missing_usage"):
        for field in ("total_cost_usd", "num_turns", "usage"):
            envelope.pop(field)
        # Model-generated text must never be interpreted as provider accounting.
        result["summary"] = json.dumps({"total_cost_usd": 999, "num_turns": 999,
                                      "usage": {"input_tokens": 999}})
    print("{truncated" if options.get("malformed") else json.dumps(envelope))
else:
    (job / "response.json").write_text(json.dumps(result))
    events = options.get("events", [
        {"type": "turn.started"},
        {"type": "item.completed", "usage": {"input_tokens": 999999}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20}}])
    for event in events:
        print(json.dumps(event))
sys.exit(options.get("exit_code", 0))
'''


class FixtureCLIBackend(CLIBackend):
    def __init__(self, config, state, options=None):
        super().__init__(config, state)
        self.options = options or {}
        self.calls = []

    def command(self, role, model, cwd, job, schema, run_path):
        self.calls.append({"role": role, "model": copy.deepcopy(model), "dispatch_id": job.name})
        result = {key: schema["properties"][key]["enum"][0] for key in ("task_id", "dispatch_id")}
        if role == "worker":
            result.update(status="completed", summary="Implemented greet", evidence=["hello.py"],
                          blockers=[], blocker_kind="none", proposed_memories=[])
        else:
            result.update(verdict="approve", summary="Checked greeting", findings=[], approved_memory_keys=[])
        return [sys.executable, "-c", FIXTURE_PROCESS, role, model["cli"], str(job),
                json.dumps(self.options.get(role, {})), json.dumps(result)]


class ProviderUsageTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def backend(self, options=None):
        return FixtureCLIBackend(self.config, self.repo / ".harness", options)

    def events(self, engine):
        return [event for line in (engine.directory / "events.jsonl").read_text().splitlines()
                if (event := json.loads(line))["event"] == "dispatch_finished"]

    def test_completed_dispatches_keep_provider_usage_and_completed_resume_does_not_duplicate(self):
        backend = self.backend()
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        worker = next(entry for entry in result["dispatches"] if entry["role"] == "worker")
        self.assertEqual(worker["provider_usage"], {
            "provider": "claude", "source": "process.stdout", "reported_cost_usd": 0.25,
            "num_turns": 3, "tokens": {"input_tokens": 20, "output_tokens": 5,
                                      "cache_creation_input_tokens": 10, "cache_read_input_tokens": 40}})
        reviews = [entry for entry in result["dispatches"] if entry["role"] == "reviewer"]
        self.assertEqual(len(reviews), 2)
        for entry in reviews:
            self.assertEqual(entry["provider_usage"], {
                "provider": "codex", "source": "process.stdout", "completed_turns": 1,
                "tokens": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20}})
        events = self.events(engine)
        self.assertEqual(len(events), 3)
        by_id = {entry["id"]: entry for entry in result["dispatches"]}
        for event in events:
            self.assertEqual(event["provider_usage"], by_id[event["dispatch_id"]]["provider_usage"])
        calls = copy.deepcopy(backend.calls)
        resumed = self.engine(backend).resume(result["id"])
        self.assertEqual(resumed["dispatches"], result["dispatches"])
        self.assertEqual(self.events(engine), events)
        self.assertEqual(backend.calls, calls)
        self.assertEqual([call["model"] for call in calls], [self.config.role(call["role"]) for call in calls])
        self.assertEqual(len({call["dispatch_id"] for call in calls}), len(calls))

    def test_cli_failure_keeps_reported_usage_and_original_error(self):
        backend = self.backend({"worker": {"exit_code": 7, "envelope": {
            "is_error": True, "subtype": "error_max_turns"}}})
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "worker CLI exit=7"):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        entry = saved["dispatches"][-1]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["provider_usage"]["reported_cost_usd"], 0.25)
        self.assertEqual(saved["total_attempts"], 1)
        self.assertEqual(saved["agent_dispatches"], 1)
        self.assertEqual(saved["accepted"], {})
        self.assertEqual(self.events(engine)[-1]["provider_usage"], entry["provider_usage"])

    def test_protocol_failure_still_records_usage_and_blocks_acceptance(self):
        engine = self.engine(self.backend({"worker": {"invalid_result": True}}))
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(engine.run["dispatches"][-1]["provider_usage"]["num_turns"], 3)
        self.assertEqual(self.events(engine)[-1]["status"], "failed")

    def test_missing_and_invalid_metrics_remain_unknown_without_blocking_acceptance(self):
        options = {"worker": {"envelope": {
            "total_cost_usd": float("nan"), "num_turns": True,
            "usage": {"input_tokens": -1, "output_tokens": 0,
                      "cache_creation_input_tokens": "20", "cache_read_input_tokens": 2**64}}}}
        engine = self.engine(self.backend(options))
        result = engine.start("Implement greet", self.plan())
        worker = next(entry for entry in result["dispatches"] if entry["role"] == "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(worker["provider_usage"], {
            "provider": "claude", "source": "process.stdout", "tokens": {"output_tokens": 0}})
        options["worker"] = {"missing_usage": True}
        engine = self.engine(self.backend(options))
        result = engine.start("Implement greet", self.plan())
        worker = next(entry for entry in result["dispatches"] if entry["role"] == "worker")
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("provider_usage", worker)
        event = next(event for event in self.events(engine) if event["role"] == "worker")
        self.assertIsNone(event["provider_usage"])

    def test_receipt_resume_keeps_one_observation_for_the_existing_dispatch(self):
        backend = self.backend()
        engine = self.engine(backend)

        def fail_review_hook(event, payload, logs):
            if event == "post_agent" and payload["role"] == "reviewer":
                raise HookError(event, 7, logs)

        with patch.object(engine.hooks, "emit", side_effect=fail_review_hook):
            with self.assertRaises(HookError):
                engine.start("Implement greet", self.plan())
        prior = copy.deepcopy(engine.run["dispatches"][-1])
        self.assertEqual(prior["role"], "reviewer")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        entry = next(entry for entry in result["dispatches"] if entry["id"] == prior["id"])
        self.assertEqual(entry["provider_usage"], prior["provider_usage"])
        self.assertEqual(len(backend.calls), 3)
        self.assertEqual(len([event for event in self.events(engine) if event["dispatch_id"] == prior["id"]]), 1)

    def test_telemetry_errors_do_not_mask_cli_error_or_gate_acceptance(self):
        engine = self.engine(self.backend({"worker": {"malformed": True, "exit_code": 7}}))
        with self.assertRaisesRegex(HarnessError, "worker CLI exit=7"):
            engine.start("Implement greet", self.plan())
        self.assertNotIn("provider_usage", engine.run["dispatches"][-1])
        backend = self.backend()
        engine = self.engine(backend)
        original = engine.event

        def unavailable_event(event, **fields):
            if event == "dispatch_finished":
                raise OSError("telemetry event unavailable")
            return original(event, **fields)

        with patch("harness.dispatch.provider_usage", side_effect=OSError("telemetry read unavailable")), \
                patch.object(engine, "event", side_effect=unavailable_event):
            result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_attempts"], 1)

    def test_codex_totals_do_not_assume_missing_usage_fields_are_zero(self):
        options = {"reviewer": {"events": [
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}},
            {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 40}},
            {"type": "turn.failed", "usage": {"input_tokens": 999999}}]}}
        engine = self.engine(self.backend(options))
        result = engine.start("Implement greet", self.plan())
        reviews = [entry for entry in result["dispatches"] if entry["role"] == "reviewer"]
        for entry in reviews:
            self.assertEqual(entry["provider_usage"], {
                "provider": "codex", "source": "process.stdout", "completed_turns": 2,
                "tokens": {"input_tokens": 150}})
            with patch("harness.adapters._USAGE_MAX_BYTES", 10):
                self.assertIsNone(provider_usage(engine.directory / "dispatches" / entry["id"], "codex"))
