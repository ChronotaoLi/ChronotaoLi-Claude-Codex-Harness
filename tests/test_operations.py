"""Operation records bind each CLI run/resume to its persisted run without changing the lifecycle."""
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import unittest
from unittest.mock import Mock, patch

import harness.engine
from harness import git
from harness.adapters import DemoBackend
from harness.cli import main
from harness.engine import Engine
from harness.operations import OperationRecord
from harness.shared import asset_digest
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase


def fake_probe(name):
    return {"path": f"/tools/{name}", "version": f"{name} test-version"}


def notice(event, run_id, operation_id):
    return "[harness-json] " + json.dumps({"event": event, "run_id": run_id, "operation_id": operation_id},
                                          ensure_ascii=True, separators=(",", ":"))


class RejectingStderr(io.StringIO):
    """Refuse observation notices and warnings; other diagnostics still go through."""

    def __init__(self):
        super().__init__()
        self.rejected = []

    def write(self, text):
        if "[harness-json]" in text or "[harness] 警告" in text:
            self.rejected.append(text)
            raise OSError("stderr rejected observation output")
        return super().write(text)


class OperationCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.state = self.repo / ".harness"
        self.operations = self.state / "operations"

    def invoke(self, args, backend=None, errors=None, **patches):
        """Real CLI and Engine with a simulated model backend; provider CLIs are never executed."""
        output, errors = io.StringIO(), io.StringIO() if errors is None else errors
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("harness.cli.Config.load", return_value=self.config))
            stack.enter_context(patch("harness.engine.CLIBackend", return_value=backend or DemoBackend()))
            stack.enter_context(patch("harness.operations.probe_cli", side_effect=fake_probe))
            for name, value in patches.items():
                stack.enter_context(patch(name, value))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            code = main(["--repo", str(self.repo), *args])
        return code, output.getvalue(), errors.getvalue()

    def record(self, operation_id):
        return read_json(self.operations / f"{operation_id}.json")

    def ledger(self, run_id):
        return read_json(self.state / "runs" / run_id / "state.json")

    def events(self, run_id, name):
        path = self.state / "runs" / run_id / "events.jsonl"
        return [event for event in map(json.loads, path.read_text().splitlines()) if event["event"] == name]


class OperationRecordTests(OperationCase):
    def test_completed_run_is_linked_before_first_dispatch_and_finished_with_exit_status(self):
        class Observing(DemoBackend):
            def __init__(self, path):
                self.path, self.seen = path, []

            def invoke(self, **kwargs):
                self.seen.append(read_json(self.path))
                return super().invoke(**kwargs)

        backend = Observing(self.operations / "op-a.json")
        head = git.head(self.repo)
        code, output, errors = self.invoke(["run", "Implement greet", "--operation-id", "op-a"], backend)
        self.assertEqual(code, 0, errors)
        result = json.loads(output)  # a single JSON document; trailing data would fail to parse
        self.assertEqual(set(result), {"id", "status", "branch", "worktree", "accepted", "operation_id"})
        self.assertEqual(result["operation_id"], "op-a")
        self.assertEqual(result["status"], "completed")
        run_id = result["id"]
        first = backend.seen[0]
        self.assertEqual((first["run_id"], first["status"]), (run_id, "running"))
        self.assertIsInstance(first["run_linked_at"], float)
        self.assertIsNone(first["exit_status"])
        record = self.record("op-a")
        self.assertEqual({key: record[key] for key in ("operation_id", "kind", "status", "run_id", "exit_status",
                                                        "result_status", "error", "error_type")},
                         {"operation_id": "op-a", "kind": "run", "status": "finished", "run_id": run_id,
                          "exit_status": 0, "result_status": "completed", "error": None, "error_type": None})
        self.assertEqual(record["arguments"], {"goal": "Implement greet", "plan": None, "commander": "astra",
                                               "pause_after_plan": False, "step": False})
        self.assertEqual((record["repo"], record["repo_head"]), (str(self.repo.resolve()), head))
        self.assertEqual(record["harness"]["package"], str(ROOT / "harness"))
        self.assertEqual(record["harness"]["python"], sys.version.split()[0])
        self.assertEqual(record["harness"]["executable"], sys.executable)
        self.assertEqual(record["config"], {"path": str(self.config.path),
                                            "sha256": hashlib.sha256(self.config.path.read_bytes()).hexdigest()})
        self.assertEqual(record["asset_digest"], asset_digest(self.config))
        self.assertEqual(record["models"], self.config.data["roles"])
        self.assertEqual(record["cli"], {"claude": fake_probe("claude"), "codex": fake_probe("codex")})
        self.assertLessEqual(record["started_at"], record["run_linked_at"])
        self.assertLessEqual(record["run_linked_at"], record["finished_at"])
        self.assertRegex(record["finished_at_utc"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        ledger = self.ledger(run_id)
        self.assertEqual((ledger["schema_version"], ledger["created_by_operation"]), (11, "op-a"))
        created = self.events(run_id, "run_created")
        self.assertEqual([(event["run_id"], event["operation_id"]) for event in created], [(run_id, "op-a")])
        self.assertIn(notice("run_created", run_id, "op-a"), errors.splitlines())

    def test_failure_before_run_creation_leaves_record_without_run(self):
        (self.repo / "test_hello.py").write_text("print('tracked modification')\n")
        code, output, errors = self.invoke(["run", "Implement greet", "--operation-id", "op-b"])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("已跟踪", errors)
        record = self.record("op-b")
        self.assertEqual((record["status"], record["run_id"], record["run_linked_at"], record["exit_status"]),
                         ("finished", None, None, 1))
        self.assertEqual((record["result_status"], record["error_type"]), (None, "HarnessError"))
        self.assertIn("已跟踪", record["error"])
        self.assertFalse((self.state / "runs").exists())

    def test_failure_after_run_creation_keeps_link_and_original_error(self):
        class Failing(DemoBackend):
            def invoke(self, **kwargs):
                raise HarnessError("simulated provider failure\nsecond line of detail")

        code, output, errors = self.invoke(["run", "Implement greet", "--operation-id", "op-c"], Failing())
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("harness: simulated provider failure", errors)
        record = self.record("op-c")
        self.assertEqual((record["status"], record["exit_status"], record["result_status"]), ("finished", 1, None))
        self.assertEqual((record["error"], record["error_type"]), ("simulated provider failure", "HarnessError"))
        ledger = self.ledger(record["run_id"])
        self.assertEqual((ledger["status"], ledger["created_by_operation"]), ("blocked", "op-c"))
        self.assertIn(notice("run_created", record["run_id"], "op-c"), errors.splitlines())

    def test_resume_creates_its_own_record_with_event_and_notice(self):
        code, output, errors = self.invoke(["run", "Implement greet", "--pause-after-plan", "--operation-id", "op-start"])
        self.assertEqual(code, 0, errors)
        run_id = json.loads(output)["id"]
        start = self.record("op-start")
        code, output, errors = self.invoke(["resume", run_id, "--approve-plan", "--operation-id", "op-resume"])
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertEqual(set(result), {"id", "status", "branch", "worktree", "operation_id"})
        self.assertEqual((result["id"], result["status"], result["operation_id"]), (run_id, "completed", "op-resume"))
        record = self.record("op-resume")
        self.assertEqual((record["kind"], record["status"], record["run_id"], record["exit_status"],
                          record["result_status"]), ("resume", "finished", run_id, 0, "completed"))
        self.assertEqual(record["arguments"], {
            "run_id": run_id, "approve_plan": True, "step": False, "reverify": False, "retry_stopped": None,
            "confirm_exited": [], "repair_invalid": None, "environment_update": None,
            "operator_decision": [], "allow_scope": []})
        self.assertIsInstance(record["run_linked_at"], float)
        self.assertEqual([event["operation_id"] for event in self.events(run_id, "run_resumed")], ["op-resume"])
        self.assertIn(notice("run_resumed", run_id, "op-resume"), errors.splitlines())
        self.assertEqual(self.record("op-start"), start)
        self.assertEqual(self.ledger(run_id)["created_by_operation"], "op-start")

    def test_early_rejected_resume_links_record_without_touching_run_evidence(self):
        code, output, errors = self.invoke(["run", "Implement greet", "--pause-after-plan", "--operation-id", "op-paused"])
        self.assertEqual(code, 0, errors)
        run_id = json.loads(output)["id"]
        directory = self.state / "runs" / run_id
        before = ((directory / "state.json").read_bytes(), (directory / "events.jsonl").read_bytes())
        code, output, errors = self.invoke(["resume", run_id, "--confirm-exited", "dispatch_unknown",
                                            "--operation-id", "op-reject"])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("dispatch_unknown", errors)
        self.assertEqual(((directory / "state.json").read_bytes(), (directory / "events.jsonl").read_bytes()), before)
        self.assertNotIn("[harness-json]", errors)
        record = self.record("op-reject")
        self.assertEqual((record["status"], record["run_id"], record["exit_status"], record["error_type"]),
                         ("finished", run_id, 1, "HarnessError"))
        self.assertEqual(record["arguments"]["confirm_exited"], ["dispatch_unknown"])

    def test_resume_stopped_before_dispatch_keeps_events_hooks_and_budgets(self):
        plan = read_json(ROOT / "examples" / "plan.json")
        plan["tasks"][0]["required_commands"] = [["required-tool", "run", "--exact"]]
        plan_path = self.repo / "input-plan.json"
        write_json(plan_path, plan)
        backend = Mock(spec=DemoBackend)
        backend.invoke.side_effect = AssertionError("Blocked commands must not dispatch a model")
        code, output, errors = self.invoke(["run", "Implement greet", "--plan", str(plan_path),
                                            "--operation-id", "op-missing"], backend)
        self.assertEqual(code, 1, errors)
        self.assertEqual(output, "")
        run_id = self.record("op-missing")["run_id"]
        directory = self.state / "runs" / run_id
        events_path = directory / "events.jsonl"
        for index in range(2):
            operation_id = f"op-stopped-{index}"
            before = self.ledger(run_id)
            events_before = events_path.read_bytes()
            with patch.object(Engine, "emit_hook", autospec=True, side_effect=Engine.emit_hook) as hook:
                code, output, errors = self.invoke(["resume", run_id, "--operation-id", operation_id], backend)
            self.assertEqual(code, 1, errors)
            self.assertEqual(output, "")
            self.assertIn(notice("run_resumed", run_id, operation_id), errors.splitlines())
            events_after = events_path.read_bytes()
            self.assertTrue(events_after.startswith(events_before))
            appended = [json.loads(line) for line in events_after[len(events_before):].splitlines()]
            self.assertEqual([event["event"] for event in appended], ["run_resumed", "run_stopped"])
            self.assertEqual(appended[0]["operation_id"], operation_id)
            saved = self.ledger(run_id)
            self.assertEqual(saved["status"], "blocked")
            self.assertEqual(saved["error_hook_attempts"], before["error_hook_attempts"] + 1)
            for key in ("dispatches", "attempts", "total_attempts", "agent_dispatches", "accepted"):
                self.assertEqual(saved[key], before[key], key)
            hook.assert_called_once()
            self.assertEqual(hook.call_args.args[1], "on_error")
            self.assertEqual(hook.call_args.args[3], directory.resolve() / "hooks" / f"error-{saved['error_hook_attempts']}")
            record = self.record(operation_id)
            self.assertEqual((record["run_id"], record["status"], record["exit_status"]),
                             (run_id, "finished", 1))
        backend.invoke.assert_not_called()

    def test_approval_rejection_keeps_prior_grant_audit_without_resume_notice(self):
        command = ["required-tool", "run", "--exact"]
        plan = read_json(ROOT / "examples" / "plan.json")
        plan["tasks"][0]["required_commands"] = [command]
        plan_path = self.repo / "input-plan.json"
        write_json(plan_path, plan)
        backend = Mock(spec=DemoBackend)
        backend.invoke.side_effect = AssertionError("Unapproved plans must not dispatch a model")
        code, output, errors = self.invoke(["run", "Implement greet", "--plan", str(plan_path),
                                            "--pause-after-plan", "--operation-id", "op-approval"], backend)
        self.assertEqual(code, 0, errors)
        run_id = json.loads(output)["id"]
        directory = self.state / "runs" / run_id
        state_path, events_path = directory / "state.json", directory / "events.jsonl"
        before = self.ledger(run_id)
        for index, add_grant in enumerate((False, True, False)):
            with self.subTest(add_grant=add_grant, index=index):
                if add_grant:
                    self.config.data["permissions"]["worker_commands"].append(command)
                state_before, events_before = state_path.read_bytes(), events_path.read_bytes()
                operation_id = f"op-approval-reject-{index}"
                code, output, errors = self.invoke(["resume", run_id, "--operation-id", operation_id], backend)
                self.assertEqual(code, 1, errors)
                self.assertEqual(output, "")
                self.assertNotIn("[harness-json]", errors)
                saved = self.ledger(run_id)
                self.assertFalse(saved["plan_approved"])
                for key in ("status", "dispatches", "attempts", "total_attempts", "agent_dispatches", "accepted"):
                    self.assertEqual(saved[key], before[key], key)
                self.assertNotIn("error_hook_attempts", saved)
                if add_grant:
                    self.assertNotEqual(state_path.read_bytes(), state_before)
                    self.assertTrue(events_path.read_bytes().startswith(events_before))
                    appended = [json.loads(line) for line in events_path.read_bytes()[len(events_before):].splitlines()]
                    self.assertEqual([event["event"] for event in appended],
                                     ["resume_config_relaxed", "command_requirements_checked"])
                    self.assertEqual(saved["config_snapshot"]["permissions"]["worker_commands"], [command])
                    self.assertEqual(saved["command_requirements"], {"missing": {}})
                else:
                    self.assertEqual((state_path.read_bytes(), events_path.read_bytes()), (state_before, events_before))
                record = self.record(operation_id)
                self.assertEqual((record["run_id"], record["exit_status"]), (run_id, 1))
        backend.invoke.assert_not_called()

    def test_operation_id_is_validated_before_any_write(self):
        for invalid in ("", "-leading", "has space", "a/b", "x" * 65, "..", "opü"):
            with self.subTest(operation_id=invalid):
                code, output, errors = self.invoke(["run", "Implement greet", f"--operation-id={invalid}"])
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertIn("--operation-id", errors)
        self.assertFalse(self.state.exists())

    def test_existing_record_is_refused_without_touching_it_or_the_repository(self):
        self.operations.mkdir(parents=True)
        path = self.operations / "taken.json"
        path.write_bytes(b"original bytes that are not even JSON")
        exclude = self.repo / ".git" / "info" / "exclude"
        for command in (["run", "Implement greet"], ["resume", "run_unknown"]):
            with self.subTest(command=command[0]):
                code, output, errors = self.invoke([*command, "--operation-id", "taken"])
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertIn("已存在", errors)
                self.assertEqual(path.read_bytes(), b"original bytes that are not even JSON")
        self.assertEqual(sorted(self.operations.iterdir()), [path])
        self.assertFalse((self.state / "runs").exists())
        self.assertFalse(exclude.exists() and "/.harness/" in exclude.read_text().splitlines())

    def test_custom_operation_id_is_used_verbatim_and_conflicts_after_a_real_run(self):
        code, output, errors = self.invoke(["run", "Implement greet", "--pause-after-plan", "--operation-id", "custom.id-1"])
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["operation_id"], "custom.id-1")
        path = self.operations / "custom.id-1.json"
        before = path.read_bytes()
        runs = sorted((self.state / "runs").iterdir())
        code, output, errors = self.invoke(["run", "Implement greet", "--operation-id", "custom.id-1"])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("已存在", errors)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted((self.state / "runs").iterdir()), runs)
        self.assertEqual(sorted(self.operations.iterdir()), [path])

    def test_keyboard_interrupt_records_exit_status_130(self):
        class Interrupting(DemoBackend):
            def invoke(self, **kwargs):
                raise KeyboardInterrupt()

        code, output, errors = self.invoke(["run", "Implement greet", "--operation-id", "op-f"], Interrupting())
        self.assertEqual(code, 130)
        self.assertEqual(output, "")
        self.assertIn("已中断", errors)
        record = self.record("op-f")
        self.assertEqual((record["status"], record["exit_status"], record["error"], record["error_type"]),
                         ("finished", 130, "KeyboardInterrupt", "KeyboardInterrupt"))
        self.assertEqual(self.ledger(record["run_id"])["status"], "interrupted")

    def test_record_excludes_environment_values_and_file_contents(self):
        plan = read_json(ROOT / "examples" / "plan.json")
        plan["tasks"][0]["description"] = "PLAN-CONTENT-SENTINEL implement greet"
        plan_path = self.repo / "input-plan.json"
        write_json(plan_path, plan)
        answer = self.repo / "answer.json"
        answer.write_text('{"request_id": "DECISION-CONTENT-SENTINEL", "decision": "continue"}')
        with patch.dict(os.environ, {"HARNESS_TEST_SECRET": "sentinel-xyz"}):
            code, output, errors = self.invoke(["run", "Implement greet", "--plan", str(plan_path),
                                                "--operation-id", "op-g"])
            self.assertEqual(code, 0, errors)
            run_id = json.loads(output)["id"]
            code, _, errors = self.invoke(["resume", run_id, "--operator-decision", str(answer),
                                           "--environment-update", str(answer), "--operation-id", "op-g2"])
            self.assertEqual(code, 1, errors)
        for name in ("op-g", "op-g2"):
            raw = (self.operations / f"{name}.json").read_bytes()
            self.assertNotIn(b"sentinel-xyz", raw)
            self.assertNotIn(b"PLAN-CONTENT-SENTINEL", raw)
            self.assertNotIn(b"DECISION-CONTENT-SENTINEL", raw)
            self.assertNotIn(b"HARNESS_TEST_SECRET", raw)
        self.assertEqual(self.record("op-g")["arguments"]["plan"], str(plan_path))
        arguments = self.record("op-g2")["arguments"]
        self.assertEqual((arguments["operator_decision"], arguments["environment_update"]), ([str(answer)], str(answer)))
        self.assertEqual(self.record("op-g2")["run_id"], run_id)

    def test_observation_failures_never_change_lifecycle_or_exit_status(self):
        with patch.object(OperationRecord, "link", side_effect=OSError("record disk full")):
            code, output, errors = self.invoke(["run", "Implement greet", "--operation-id", "op-link"])
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertEqual(result["status"], "completed")
        self.assertIn("record disk full", errors)
        self.assertIn("警告", errors)
        record = self.record("op-link")
        self.assertEqual((record["status"], record["exit_status"], record["run_id"]), ("finished", 0, result["id"]))
        self.assertEqual(self.ledger(result["id"])["created_by_operation"], "op-link")
        self.assertIn(notice("run_created", result["id"], "op-link"), errors.splitlines())

        real = harness.engine.append_event

        def failing(path, run_id, event, **fields):
            if event == "run_created":
                raise OSError("events unavailable")
            return real(path, run_id, event, **fields)

        code, output, errors = self.invoke(["run", "Implement greet", "--pause-after-plan", "--operation-id", "op-event"],
                                           **{"harness.engine.append_event": failing})
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertIn("events unavailable", errors)
        self.assertIn(notice("run_created", result["id"], "op-event"), errors.splitlines())
        self.assertEqual(self.events(result["id"], "run_created"), [])
        self.assertIsInstance(self.record("op-event")["run_linked_at"], float)

        class BrokenNotify(io.StringIO):
            def write(self, text):
                if "[harness-json]" in text:
                    raise OSError("stderr notice rejected")
                return super().write(text)

        code, output, errors = self.invoke(["run", "Implement greet", "--pause-after-plan", "--operation-id", "op-notify"],
                                           errors=BrokenNotify())
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertIn("stderr notice rejected", errors)
        self.assertEqual([event["operation_id"] for event in self.events(result["id"], "run_created")], ["op-notify"])
        self.assertEqual(self.record("op-notify")["run_id"], result["id"])

    def test_legacy_run_without_operation_field_still_loads_and_resumes(self):
        code, output, errors = self.invoke(["run", "Implement greet", "--pause-after-plan", "--operation-id", "op-old"])
        self.assertEqual(code, 0, errors)
        run_id = json.loads(output)["id"]
        path = self.state / "runs" / run_id / "state.json"
        ledger = read_json(path)
        del ledger["created_by_operation"]
        write_json(path, ledger)
        code, output, errors = self.invoke(["status", run_id])
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["status"], "awaiting_approval")
        code, output, errors = self.invoke(["resume", run_id, "--approve-plan"])
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["status"], "completed")
        self.assertNotIn("created_by_operation", read_json(path))
        self.assertEqual(read_json(path)["schema_version"], 11)

    def test_engine_accepts_operation_double_and_null_operation(self):
        state = self.state

        class Double:
            id = "op-double"

            def __init__(self):
                self.linked = []

            def link(self, run_id):
                self.linked.append((run_id, (state / "runs" / run_id / "state.json").is_file()))

        plan = read_json(ROOT / "examples" / "plan.json")
        double = Double()
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result = Engine(self.config, self.repo, backend=DemoBackend(), operation=double).start(
                "Implement greet", plan, pause_after_plan=True)
        self.assertEqual(result["created_by_operation"], "op-double")
        self.assertEqual(double.linked, [(result["id"], True)])
        self.assertIn(notice("run_created", result["id"], "op-double"), errors.getvalue().splitlines())
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result = Engine(self.config, self.repo, backend=DemoBackend()).start("Implement greet", plan, pause_after_plan=True)
        self.assertIsNone(result["created_by_operation"])
        self.assertEqual([event["operation_id"] for event in self.events(result["id"], "run_created")], [None])
        self.assertIn(notice("run_created", result["id"], None), errors.getvalue().splitlines())

    def test_exclusive_creation_never_overwrites_an_existing_record(self):
        arguments = {"goal": "x", "plan": None, "commander": "astra", "pause_after_plan": False, "step": False}
        with patch("harness.operations.probe_cli", side_effect=fake_probe):
            first = OperationRecord.create(self.state, kind="run", arguments=arguments, config=self.config,
                                           repo=self.repo, operation_id="shared")
            before = first.path.read_bytes()
            with self.assertRaisesRegex(HarnessError, "已存在"):
                OperationRecord.create(self.state, kind="run", arguments=arguments, config=self.config,
                                       repo=self.repo, operation_id="shared")
            generated = OperationRecord.create(self.state, kind="run", arguments=arguments, config=self.config,
                                               repo=self.repo)
        self.assertEqual(first.path.read_bytes(), before)
        self.assertRegex(generated.id, r"op_[0-9a-f]{16}")
        self.assertEqual(read_json(first.path)["status"], "prepared")
        first.link("run_abc")
        with self.assertRaisesRegex(HarnessError, "不能改绑"):
            first.link("run_other")
        first.link("run_abc")
        self.assertEqual(read_json(first.path)["run_id"], "run_abc")


class ObservationFailureTests(OperationCase):
    def paused_run(self, operation_id):
        code, output, errors = self.invoke(["run", "Implement greet", "--pause-after-plan", "--operation-id", operation_id])
        self.assertEqual(code, 0, errors)
        return json.loads(output)["id"]

    def test_run_completes_when_notice_and_warning_output_both_fail(self):
        errors = RejectingStderr()
        code, output, _ = self.invoke(["run", "Implement greet", "--operation-id", "op-stderr"], errors=errors)
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual(result["status"], "completed")
        ledger = self.ledger(result["id"])
        self.assertEqual(ledger["status"], "completed")
        self.assertTrue(ledger["worktree_binding"])
        self.assertEqual(ledger["created_by_operation"], "op-stderr")
        record = self.record("op-stderr")
        self.assertEqual((record["status"], record["exit_status"], record["run_id"]), ("finished", 0, result["id"]))
        self.assertTrue(any("[harness-json]" in text for text in errors.rejected))
        self.assertTrue(any("警告" in text for text in errors.rejected))
        self.assertNotIn("[harness-json]", errors.getvalue())
        self.assertNotIn("警告", errors.getvalue())

    def test_interrupt_exit_status_survives_record_and_warning_failures(self):
        class Interrupting(DemoBackend):
            def invoke(self, **kwargs):
                raise KeyboardInterrupt()

        errors = RejectingStderr()
        with patch("harness.operations.write_json", side_effect=OSError("record disk full")):
            code, output, _ = self.invoke(["run", "Implement greet", "--operation-id", "op-int"], Interrupting(),
                                          errors=errors)
        self.assertEqual(code, 130)
        self.assertEqual(output, "")
        self.assertIn("已中断", errors.getvalue())
        record = self.record("op-int")
        self.assertEqual((record["status"], record["run_id"], record["exit_status"]), ("prepared", None, None))
        self.assertTrue(any("操作记录更新失败" in text for text in errors.rejected))
        run_id = next(path.name for path in (self.state / "runs").iterdir())
        self.assertEqual(self.ledger(run_id)["status"], "interrupted")

    def test_resume_completes_when_link_notice_and_warning_output_fail(self):
        run_id = self.paused_run("op-paused-resume")
        errors = RejectingStderr()
        with patch.object(OperationRecord, "link", side_effect=OSError("record locked")):
            code, output, _ = self.invoke(["resume", run_id, "--approve-plan", "--operation-id", "op-resume-stderr"],
                                          errors=errors)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["status"], "completed")
        self.assertEqual(self.ledger(run_id)["status"], "completed")
        record = self.record("op-resume-stderr")
        self.assertEqual((record["status"], record["exit_status"], record["run_id"]), ("finished", 0, run_id))
        self.assertEqual([event["operation_id"] for event in self.events(run_id, "run_resumed")], ["op-resume-stderr"])
        self.assertTrue(any("[harness-json]" in text for text in errors.rejected))
        self.assertTrue(any("record locked" in text for text in errors.rejected))


class FileInputPrivacyTests(OperationCase):
    OMITTED = "file inputs present; see stderr"

    def paused_run(self, operation_id):
        code, output, errors = self.invoke(["run", "Implement greet", "--pause-after-plan", "--operation-id", operation_id])
        self.assertEqual(code, 0, errors)
        return json.loads(output)["id"]

    def assert_omitted(self, operation_id, sentinels):
        raw = (self.operations / f"{operation_id}.json").read_bytes()
        for sentinel in sentinels:
            self.assertNotIn(sentinel.encode("utf-8"), raw)
        record = self.record(operation_id)
        self.assertEqual((record["status"], record["exit_status"], record["error"], record["error_type"],
                          record["error_omitted"]), ("finished", 1, None, "HarnessError", self.OMITTED))
        return record

    def test_invalid_plan_error_text_is_omitted_from_record(self):
        plan = read_json(ROOT / "examples" / "plan.json")
        plan["tasks"][0]["allowed_paths"] = ["../FILE-CONTENT-SENTINEL"]
        plan_path = self.repo / "plan.json"
        write_json(plan_path, plan)
        code, output, errors = self.invoke(["run", "Implement greet", "--plan", str(plan_path), "--operation-id", "op-plan"])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("FILE-CONTENT-SENTINEL", errors)
        record = self.assert_omitted("op-plan", ["FILE-CONTENT-SENTINEL"])
        self.assertIsNone(record["run_id"])
        self.assertEqual(record["arguments"]["plan"], str(plan_path))

    def test_invalid_environment_update_error_text_is_omitted_from_record(self):
        run_id = self.paused_run("op-env-start")
        update = self.repo / "update.json"
        update.write_text('{"schema_version": 1, "ENV-KEY-SENTINEL": "ENV-VALUE-SENTINEL", "ENV-KEY-SENTINEL": 2}')
        code, output, errors = self.invoke(["resume", run_id, "--environment-update", str(update), "--operation-id", "op-env"])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("ENV-KEY-SENTINEL", errors)
        record = self.assert_omitted("op-env", ["ENV-KEY-SENTINEL", "ENV-VALUE-SENTINEL"])
        self.assertEqual((record["run_id"], record["arguments"]["environment_update"]), (run_id, str(update)))
        self.assertEqual(self.ledger(run_id)["status"], "awaiting_approval")

    def test_invalid_operator_decision_error_text_is_omitted_from_record(self):
        run_id = self.paused_run("op-decision-start")
        answer = self.repo / "answer.json"
        write_json(answer, {"schema_version": 1, "request_id": "DECISION-ID-SENTINEL", "task_id": "greet",
                            "decision": "continue", "summary": "DECISION-SUMMARY-SENTINEL"})
        code, output, errors = self.invoke(["resume", run_id, "--operator-decision", str(answer), "--operation-id", "op-decision"])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("harness: ", errors)
        record = self.assert_omitted("op-decision", ["DECISION-ID-SENTINEL", "DECISION-SUMMARY-SENTINEL"])
        self.assertEqual((record["run_id"], record["arguments"]["operator_decision"]), (run_id, [str(answer)]))
        self.assertEqual(self.ledger(run_id)["status"], "awaiting_approval")

    def test_error_text_is_kept_without_file_inputs(self):
        (self.repo / "test_hello.py").write_text("print('tracked modification')\n")
        code, _, _ = self.invoke(["run", "Implement greet", "--operation-id", "op-plain"])
        self.assertEqual(code, 1)
        record = self.record("op-plain")
        self.assertIn("已跟踪", record["error"])
        self.assertIsNone(record["error_omitted"])


class DoctorEchoTests(OperationCase):
    def doctor(self):
        process = Mock(return_value=Mock(returncode=0, stdout="test-version"))
        with patch("harness.cli.shutil.which", side_effect=lambda cli: "/tools/claude" if cli == "claude" else None), \
                patch("harness.cli.subprocess.run", process):
            code, output, errors = self.invoke(["doctor"])
        self.assertTrue(all(call.args[0][0] == "claude" for call in process.call_args_list))
        return code, json.loads(output) if output else None, errors

    def test_doctor_echoes_runtime_identity_configuration_and_shared_assets(self):
        for role in self.config.data["roles"].values():
            role["cli"] = "claude"
        expected_revision = git.head(ROOT)
        code, report, errors = self.doctor()
        self.assertEqual(code, 0, errors)
        self.assertEqual(report["checks"], {"claude": {"installed": True, "version": "test-version", "logged_in": True}})
        self.assertEqual(report["harness"]["package"], str(ROOT / "harness"))
        self.assertEqual(report["harness"]["revision"], expected_revision)
        self.assertIsInstance(report["harness"]["runtime_modified"], bool)
        self.assertEqual((report["harness"]["python"], report["harness"]["executable"]),
                         (sys.version.split()[0], sys.executable))
        self.assertEqual(report["config"], {"path": str(self.config.path),
                                            "sha256": hashlib.sha256(self.config.path.read_bytes()).hexdigest()})
        self.assertEqual(report["asset_digest"], asset_digest(self.config))
        self.assertNotIn("asset_error", report)
        self.assertEqual(report["shared"], {key: str(self.config.asset(key)) for key in ("rules", "skills", "mcp", "hooks")})
        self.config.data["shared"]["rules"] = "missing-rules.md"
        code, report, errors = self.doctor()
        self.assertEqual(code, 0, errors)
        self.assertIsNone(report["asset_digest"])
        self.assertIn("FileNotFoundError", report["asset_error"])
        self.assertEqual(report["shared"]["rules"], str(self.config.root / "missing-rules.md"))

    def test_doctor_exit_status_still_follows_login_checks(self):
        for role in self.config.data["roles"].values():
            role["cli"] = "claude"
        process = Mock(return_value=Mock(returncode=1, stdout="test-version"))
        with patch("harness.cli.shutil.which", side_effect=lambda cli: "/tools/claude" if cli == "claude" else None), \
                patch("harness.cli.subprocess.run", process):
            code, output, errors = self.invoke(["doctor"])
        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertFalse(report["checks"]["claude"]["logged_in"])
        self.assertEqual(report["harness"]["package"], str(ROOT / "harness"))


if __name__ == "__main__":
    unittest.main()
