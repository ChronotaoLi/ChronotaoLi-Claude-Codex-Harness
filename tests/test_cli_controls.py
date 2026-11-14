import contextlib
import io
import signal
from unittest.mock import patch

from harness.adapters import DemoBackend
from harness.cli import main
from harness.runtime import Runtime
from harness.util import read_json, write_json
from test_harness import ROOT, RepoCase, walk_exit


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
        code, _, error = self.invoke(["resume", run["id"]])
        self.assertEqual(code, 0, error)  # a plain resume reports the same stop again; it never approves
        self.assertIn('"kind":"plan_approval","reason":"initial_plan"', error)
        state = read_json(engines[0].directory / "state.json")
        self.assertEqual((state["status"], state["stop"]["kind"]), ("awaiting_approval", "plan_approval"))
        # The listed exit (row plan-approval): resume RUN --approve-plan.
        approved = walk_exit(self, engines[0].directory, ("plan_approval", "initial_plan"), backend=DemoBackend(),
                             choose=1, expect_status="completed")
        reviewers = [d for d in approved["dispatches"] if d["role"] == "reviewer"]
        self.assertEqual({d["task_id"] for d in reviewers}, {"greet", "final"})

    def test_step_pauses_after_one_acceptance_and_resumes(self):
        class DocumentBackend(DemoBackend):
            def invoke(self, **kwargs):
                if kwargs["role"] == "worker" and kwargs["task"]["id"] == "document":
                    ids = {k: kwargs["schema"]["properties"][k]["enum"][0] for k in ("task_id", "dispatch_id")}
                    (kwargs["cwd"] / "usage.txt").write_text("Use greet(name).")
                    return {**ids, "status": "completed", "summary": "Documented greet", "evidence": ["usage.txt"],
                            "blockers": [], "blocker_kind": "none", "proposed_memories": [],
                            "commit_message": {"subject": "Document greet", "body": ""}}
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
