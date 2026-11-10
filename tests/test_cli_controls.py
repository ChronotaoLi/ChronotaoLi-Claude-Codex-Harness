import contextlib
import io
import json
import signal
import sys
from unittest.mock import patch

from harness.adapters import DemoBackend
from harness.cli import main
from harness.runtime import Runtime
from harness.shared import Hooks
from harness.util import HarnessError, read_json, write_json
from test_harness import ROOT, RepoCase


class CLIControlsTests(RepoCase):
    def invoke(self, args, backend=None):
        engines = []

        def factory(config, repo, **kwargs):
            engine = Runtime(config, repo, backend=backend or DemoBackend(), **kwargs)
            engines.append(engine)
            return engine

        previous = signal.getsignal(signal.SIGTERM)
        output, errors = io.StringIO(), io.StringIO()
        try:
            with patch("harness.cli.Config.load", return_value=self.config), patch("harness.cli.Runtime", side_effect=factory), \
                    patch("harness.operations.probe_cli", return_value={"path": None, "version": None}), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = main(["--repo", str(self.repo), *args])
        finally:
            signal.signal(signal.SIGTERM, previous)
        return code, engines, errors.getvalue()

    def test_plan_gate_requires_explicit_cli_approval(self):
        code, engines, error = self.invoke(["run", "Implement greet", "--pause-after-plan"])
        self.assertEqual(code, 0, error)
        run = engines[0].run
        self.assertEqual(run["status"], "awaiting_approval")
        self.assertFalse(any(d["role"] == "worker" for d in run["dispatches"]))
        code, _, _ = self.invoke(["resume", run["id"]])
        self.assertEqual(code, 1)
        state = read_json(engines[0].directory / "state.json")
        self.assertEqual(state["status"], "awaiting_approval")
        code, approved, error = self.invoke(["resume", run["id"], "--approve-plan"])
        self.assertEqual(code, 0, error)
        self.assertEqual(approved[0].run["status"], "completed")
        reviewers = [d for d in approved[0].run["dispatches"] if d["role"] == "reviewer"]
        self.assertEqual({d["task_id"] for d in reviewers}, {"greet", "final"})

    def test_step_pauses_after_one_acceptance_and_resumes(self):
        class DocumentBackend(DemoBackend):
            def invoke(self, **kwargs):
                if kwargs["role"] == "worker" and kwargs["task"]["id"] == "document":
                    ids = {k: kwargs["schema"]["properties"][k]["enum"][0] for k in ("task_id", "dispatch_id")}
                    (kwargs["cwd"] / "usage.txt").write_text("Use greet(name).")
                    return {**ids, "status": "completed", "summary": "Documented greet", "evidence": ["usage.txt"],
                            "blockers": [], "blocker_kind": "none", "proposed_memories": []}
                return super().invoke(**kwargs)

        plan = read_json(ROOT / "examples" / "plan.json")
        plan["tasks"].append({"id": "document", "title": "Document greet", "description": "Document greet in usage.txt",
                              "depends_on": ["greet"], "allowed_paths": ["usage.txt"],
                              "acceptance": ["usage.txt explains greet"], "skills": []})
        plan_path = self.repo / ".harness" / "input-plan.json"
        write_json(plan_path, plan)
        code, engines, error = self.invoke(["run", "Implement and document greet", "--plan", str(plan_path), "--step"], DocumentBackend())
        self.assertEqual(code, 0, error)
        run = engines[0].run
        self.assertEqual(run["status"], "paused")
        self.assertEqual(set(run["accepted"]), {"greet"})
        code, resumed, error = self.invoke(["resume", run["id"], "--step"], DocumentBackend())
        self.assertEqual(code, 0, error)
        self.assertEqual(resumed[0].run["status"], "completed")
        self.assertEqual(set(resumed[0].run["accepted"]), {"greet", "document"})

    def test_context_hook_receives_prepared_event_once(self):
        output = self.repo / "hook-event.json"
        hooks = Hooks(self.config)
        script = "import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps(json.load(sys.stdin)))"
        hooks.spec = {"context_prepared": [{"argv": [sys.executable, "-c", script, str(output)]}]}
        hooks.emit("context_prepared", {"report": {"used_chars": 10}}, self.repo / "logs")
        self.assertEqual(json.loads(output.read_text()), {"event": "context_prepared", "report": {"used_chars": 10}})

    def test_removed_context_hook_alias_is_an_unknown_event_naming_its_replacement(self):
        hook = {"argv": [sys.executable, "-c", "raise SystemExit(9)"]}
        path = self.repo / "hooks.json"
        for spec in ({"context_compacted": [hook]}, {"context_prepared": [], "context_compacted": [hook]}):
            with self.subTest(events=sorted(spec)):
                write_json(path, spec)
                self.config.data["shared"]["hooks"] = str(path)
                with self.assertRaises(HarnessError) as raised:
                    Hooks(self.config)
                self.assertIn("未知事件", str(raised.exception))
                self.assertIn("context_compacted", str(raised.exception))
                self.assertIn("请改用 context_prepared", str(raised.exception))
        # An in-process override is rejected before any hook script runs.
        write_json(path, {})
        hooks = Hooks(self.config)
        hooks.spec = {"context_compacted": [hook]}
        with self.assertRaisesRegex(HarnessError, "请改用 context_prepared"):
            hooks.emit("context_prepared", {"report": {}}, self.repo / "logs")
        self.assertFalse((self.repo / "logs").exists())
