import contextlib
import io
import json
from unittest.mock import Mock, patch

from harness.cli import main
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class OperationalControlsTests(RepoCase):
    def invoke(self, args, **patches):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("harness.cli.Config.load", return_value=self.config))
            stack.enter_context(patch("harness.operations.probe_cli", return_value={"path": None, "version": None}))
            for name, value in patches.items():
                stack.enter_context(patch("harness.cli." + name, value))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            code = main(["--repo", str(self.repo), *args])
        return code, output.getvalue(), errors.getvalue()

    def test_status_summary_and_full_ledger_preserve_budget_and_evidence(self):
        engine = self.engine()
        engine.start("Implement greet", read_json(ROOT / "examples/plan.json"), pause_after_plan=True)
        for full in (False, True):
            code, output, errors = self.invoke(["status", engine.run["id"], *(["--full"] if full else [])])
            self.assertEqual(code, 0, errors)
            state = json.loads(output)
            self.assertEqual(state["status"], "awaiting_approval")
            if full:
                self.assertEqual(state, read_json(engine.directory / "state.json"))
            else:
                self.assertNotIn("dispatches", state)
                self.assertEqual(state["budgets"]["total_attempts"], 0)

    def test_cli_explicit_stop_retry_and_abandon_preserve_engine_behavior(self):
        def blocker(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.update(status="blocked", blockers=["Needs prerequisite"], blocker_kind="task")
        backend = ScriptedBackend(blocker)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        factory = lambda *args, **kwargs: self.engine(backend)
        calls = len(backend.calls)
        code, _, _ = self.invoke(["resume", engine.run["id"]], Engine=factory)
        self.assertEqual(code, 1)
        self.assertEqual(len(backend.calls), calls)
        state_path = engine.directory / "state.json"
        before_retry = read_json(state_path)
        request = before_retry["decision_requests"][-1]
        code, _, errors = self.invoke(["resume", engine.run["id"], "--retry-stopped", "greet"], Engine=factory)
        self.assertEqual(code, 1)
        self.assertIn(request["id"], errors)
        self.assertIn("--operator-decision", errors)
        self.assertEqual(len(backend.calls), calls)
        self.assertEqual(read_json(state_path), before_retry)
        answer = self.repo / "continue-replan.json"
        write_json(answer, {"schema_version": 1, "request_id": request["id"], "task_id": "greet",
                            "decision": "continue", "summary": "Explicitly retry the saved planning request"})
        code, _, _ = self.invoke(["resume", engine.run["id"], "--operator-decision", str(answer)], Engine=factory)
        self.assertEqual(code, 1)  # The simulated commander explicitly stops again.
        self.assertEqual(len(backend.calls), calls + 1)
        self.assertEqual(backend.calls[-1]["kind"], "replan")
        self.assertNotEqual(read_json(state_path)["decision_requests"][-1]["id"], request["id"])
        code, _, errors = self.invoke(["abandon", engine.run["id"]], Engine=factory)
        self.assertEqual(code, 0, errors)
        self.assertEqual(read_json(engine.directory / "state.json")["status"], "abandoned")

    def test_doctor_checks_only_configured_provider_clis(self):
        for role in self.config.data["roles"].values():
            role["cli"] = "claude"
        process = Mock(return_value=Mock(returncode=0, stdout="test-version"))
        with patch("harness.cli.shutil.which", side_effect=lambda cli: "/tools/claude" if cli == "claude" else None), \
                patch("harness.cli.subprocess.run", process):
            code, output, errors = self.invoke(["doctor"])
        self.assertEqual(code, 0, errors)
        self.assertEqual(set(json.loads(output)["checks"]), {"claude"})
        self.assertTrue(all(call.args[0][0] == "claude" for call in process.call_args_list))

    def test_cli_does_not_hide_programming_key_errors(self):
        self.engine()  # Existing database; absent read-only queries need no Memory instance.
        with self.assertRaises(KeyError):
            self.invoke(["memory", "export"], Memory=Mock(side_effect=KeyError("programming defect")))

    def test_malformed_summary_fields_report_diagnostic_and_full_keeps_evidence(self):
        path = self.repo / ".harness" / "runs" / "run_damaged" / "state.json"
        damaged = {"id": "run_damaged", "accepted": None}
        write_json(path, damaged)
        code, output, errors = self.invoke(["status", "run_damaged"])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("字段无效", errors)
        code, output, errors = self.invoke(["status", "run_damaged", "--full"])
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output), damaged)

    def test_damaged_attempt_accounting_keeps_status_readable_and_ledger_unchanged(self):
        path = self.repo / ".harness" / "runs" / "run_damaged" / "state.json"
        base = {"id": "run_damaged", "attempts": {"task": 1},
                "attempt_budget_policy": "work", "dispatches": []}
        exemption = {"task_id": "task", "attempt_exemption": {"blocker_kind": "operator"}}
        variants = [{"attempts": value} for value in
                    (None, 5, ["a"], [["task", 1]], {"task": "1"}, {"task": True}, {"task": -1})]
        variants += [{"dispatches": value} for value in (None, 5, {})]
        variants += [
            {"attempt_budget_policy": "unknown"},
            {"dispatches": [dict(exemption, task_id=[])]},
            {"dispatches": [dict(exemption, task_id="missing")]},
            {"dispatches": [dict(exemption, attempt_exemption=True)]},
            {"dispatches": [exemption, exemption]},
        ]
        for fields in variants:
            with self.subTest(fields=fields):
                damaged = {**base, **fields}
                write_json(path, damaged)
                original = path.read_bytes()
                code, output, errors = self.invoke(["status", "run_damaged"])
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertIn("status --full", errors)
                self.assertNotIn("Traceback", errors)
                code, output, errors = self.invoke(["status", "run_damaged", "--full"])
                self.assertEqual(code, 0, errors)
                self.assertEqual(json.loads(output), damaged)
                self.assertEqual(path.read_bytes(), original)
