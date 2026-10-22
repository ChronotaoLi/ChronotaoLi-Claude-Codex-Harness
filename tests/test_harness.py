import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

from harness import git
from harness.adapters import CLIBackend, DemoBackend
from harness.config import Config
from harness.context import compose
from harness.contracts import PLAN, WORK, envelope, validate, validate_plan
from harness.engine import Engine
from harness.mcp_server import handle
from harness.memory import Memory
from harness.shared import Hooks, mcp_configs, toml_value
from harness.util import HarnessError, read_json, run_process

ROOT = Path(__file__).resolve().parents[1]


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        git.git(self.repo, "init", "-b", "main")
        (self.repo / ".gitignore").write_text("__pycache__/\n")
        (self.repo / "test_hello.py").write_text('import unittest\nfrom hello import greet\nclass Tests(unittest.TestCase):\n    def test_greet(self):\n        self.assertEqual(greet("Ada"), "Hello, Ada!")\n')
        git.git(self.repo, "add", ".")
        git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        self.config = Config.load(ROOT / "harness.toml")
        self.config.data["verification"]["commands"] = [[sys.executable, "-m", "unittest", "-v"]]

    def engine(self, backend=None):
        return Engine(self.config, self.repo, backend=backend or DemoBackend())


class ContractTests(unittest.TestCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def test_rejects_cycle_unknown_dependency_duplicate_id(self):
        for mode in ("cycle", "unknown", "duplicate"):
            plan = self.plan()
            second = copy.deepcopy(plan["tasks"][0])
            second["id"] = "other"
            plan["tasks"].append(second)
            if mode == "cycle":
                plan["tasks"][0]["depends_on"] = ["other"]
                second["depends_on"] = ["greet"]
            elif mode == "unknown":
                second["depends_on"] = ["missing"]
            else:
                second["id"] = "greet"
            with self.subTest(mode=mode), self.assertRaises(HarnessError):
                validate_plan(plan)

    def test_rejects_escape_and_empty_acceptance(self):
        for path in ("../secret", "/absolute", "a/../../x"):
            plan = self.plan()
            plan["tasks"][0]["allowed_paths"] = [path]
            with self.assertRaises(HarnessError):
                validate_plan(plan)
        plan = self.plan()
        plan["tasks"][0]["acceptance"] = []
        with self.assertRaises(HarnessError):
            validate_plan(plan)

    def test_stale_dispatch_cannot_complete_new_attempt(self):
        schema = envelope(WORK, "task1", "new")
        value = {"task_id": "task1", "dispatch_id": "old", "status": "completed", "summary": "done", "evidence": [],
                 "blockers": [], "blocker_kind": "none", "proposed_memories": []}
        with self.assertRaises(HarnessError):
            validate(value, schema)

    def test_invalid_structured_output_rejected(self):
        with self.assertRaises(HarnessError):
            validate({"tasks": "all done"}, PLAN)


class ContextMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite3")

    def remember(self, key="model", text="Use gpt-6-astra", ttl=None):
        return self.memory.remember(key=key, abstract="模型选择", overview=text, detail="证据 " + text, source="operator", ttl_days=ttl)

    def test_supersession_retains_history_and_retrieves_current(self):
        old = self.remember()
        new = self.remember(text="Use claude-fable-5-1")
        self.assertFalse(self.memory.read(old)["active"])
        self.assertEqual(self.memory.read(new)["supersedes"], old)
        self.assertEqual(self.memory.search("模型选择")[0]["id"], new)
        self.assertEqual(self.memory.search("gpt-6-astra"), [])

    def test_expiry_filters_recall_without_deleting_history(self):
        ident = self.remember(ttl=1)
        with self.memory.connect() as db:
            db.execute("UPDATE memories SET expires=? WHERE id=?", (time.time() - 1, ident))
        self.assertEqual(self.memory.search("模型"), [])
        self.assertTrue(self.memory.read(ident)["expired"])

    def test_layers_and_project_isolation(self):
        self.remember()
        self.assertNotIn("overview", self.memory.search("模型", level=0)[0])
        self.assertNotIn("detail", self.memory.search("模型", level=1)[0])
        self.assertIn("detail", self.memory.search("模型", level=2)[0])
        other = Memory(Path(self.temp.name) / "other.sqlite3")
        self.assertEqual(other.search("模型"), [])

    def test_context_preserves_invariants_and_drops_whole_optional_records(self):
        required = {"acceptance": ["Never lose this exact criterion"], "task_id": "abc"}
        memory = [{"id": "m", "overview": "x" * 1000}]
        prompt, report = compose(required, memory, 150)
        self.assertEqual(json.loads(prompt), required)
        self.assertEqual(report["omitted_memories"], 1)
        with self.assertRaises(HarnessError):
            compose(required, [], 5)

    def test_mcp_can_read_but_cannot_write_memory(self):
        ident = self.remember()
        response = handle({"method": "tools/call", "params": {"name": "memory_read", "arguments": {"id": ident}}}, self.memory, ROOT / "shared/skills", None)
        self.assertFalse(response["isError"])
        refused = handle({"method": "tools/call", "params": {"name": "memory_write", "arguments": {}}}, self.memory, ROOT / "shared/skills", None)
        self.assertTrue(refused["isError"])

    def test_mcp_stdio_protocol(self):
        data = '\n'.join(json.dumps(x) for x in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]) + '\n'
        p = subprocess.run([sys.executable, str(ROOT / "harness/mcp_entry.py"), "--state", self.temp.name,
                            "--skills", str(ROOT / "shared/skills")], input=data, text=True, capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        results = [json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual([r["id"] for r in results], [1, 2])
        self.assertEqual(results[0]["result"]["protocolVersion"], "2025-11-25")


class RuntimeTests(RepoCase):
    def test_full_pipeline_isolated_commit_and_verified_memory(self):
        original = git.head(self.repo)
        engine = self.engine()
        result = engine.start("Implement greet")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(git.head(self.repo), original)
        self.assertFalse((self.repo / "hello.py").exists())
        self.assertTrue(git.clean(self.repo))
        self.assertNotEqual(result["accepted"]["greet"]["commit"], original)
        self.assertEqual(len(engine.memory.export()), 1)
        self.assertTrue((engine.directory / "handoff.json").exists())
        ids = [d["id"] for d in result["dispatches"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_failed_review_uses_new_worker_and_reviewer(self):
        class RejectOnce(DemoBackend):
            count = 0
            def invoke(self, **kwargs):
                value = super().invoke(**kwargs)
                if kwargs["role"] == "reviewer" and kwargs["kind"] == "review":
                    self.count += 1
                    if self.count == 1:
                        value.update(verdict="request_changes", findings=[{
                            "id": "edge-case", "severity": "blocking", "category": "evidence",
                            "location": "hello.py", "message": "Please inspect edge case",
                            "evidence": ["The first review requests an additional check."]}])
                return value
        engine = self.engine(RejectOnce())
        result = engine.start("Implement greet")
        self.assertEqual(result["attempts"]["greet"], 2)
        for role in ("worker", "reviewer"):
            ids = [d["id"] for d in result["dispatches"] if d["role"] == role and d["task_id"] == "greet"]
            self.assertEqual(len(ids), 2)
            self.assertNotEqual(ids[0], ids[1])
        self.assertEqual(len(engine.memory.export()), 1)

    def test_bad_tests_never_reach_reviewer_or_memory(self):
        class Broken(DemoBackend):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs["role"] == "worker":
                    (kwargs["cwd"] / "hello.py").write_text('def greet(name):\n    return "wrong"\n')
                return result
        self.config.data["runtime"]["max_attempts"] = 1
        engine = self.engine(Broken())
        with self.assertRaises(HarnessError):
            engine.start("Implement greet")
        self.assertFalse(any(d["role"] == "reviewer" for d in engine.run["dispatches"]))
        self.assertEqual(engine.memory.export(), [])
        self.assertEqual(engine.run["accepted"], {})

    def test_readonly_agent_mutation_stops_run(self):
        class MutatingReviewer(DemoBackend):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs["role"] == "reviewer":
                    (kwargs["cwd"] / "oops.txt").write_text("should not write")
                return result
        engine = self.engine(MutatingReviewer())
        with self.assertRaisesRegex(HarnessError, "修改了代码"):
            engine.start("Implement greet")
        self.assertEqual(engine.memory.export(), [])

    def test_out_of_scope_changes_stop_run(self):
        class Escaping(DemoBackend):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if kwargs["role"] == "worker":
                    (kwargs["cwd"] / "unrelated.py").write_text("pass")
                return result
        with self.assertRaisesRegex(HarnessError, "越界"):
            self.engine(Escaping()).start("Implement greet")

    def test_dirty_original_is_not_stashed_or_modified(self):
        (self.repo / "mine.txt").write_text("user work")
        with self.assertRaises(HarnessError):
            self.engine().start("Implement greet")
        self.assertEqual((self.repo / "mine.txt").read_text(), "user work")

    def test_resume_completed_run_is_idempotent(self):
        engine = self.engine()
        result = engine.start("Implement greet")
        count = len(result["dispatches"])
        result2 = self.engine().resume(result["id"])
        self.assertEqual(len(result2["dispatches"]), count)

    def test_resume_interrupted_worker_creates_new_dispatch(self):
        class InterruptOnce(DemoBackend):
            def invoke(self, **kwargs):
                if kwargs["role"] == "worker":
                    raise KeyboardInterrupt()
                return super().invoke(**kwargs)
        engine = self.engine(InterruptOnce())
        with self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet")
        resumed = self.engine().resume(engine.run["id"])
        self.assertEqual(resumed["status"], "completed")
        workers = [d for d in resumed["dispatches"] if d["role"] == "worker"]
        self.assertEqual(len(workers), 2)
        self.assertNotEqual(workers[0]["id"], workers[1]["id"])

    def test_resume_refuses_live_old_process(self):
        engine = self.engine()
        engine.start("Implement greet")
        engine.run["status"] = "interrupted"
        engine.run["dispatches"][-1]["pgid"] = os.getpgrp()
        engine.run["dispatches"][-1]["exit_confirmed"] = False
        engine.save()
        with self.assertRaisesRegex(HarnessError, "进程组仍存在"):
            self.engine().resume(engine.run["id"])

    def test_verification_must_not_rewrite_review_candidate(self):
        self.config.data["verification"]["commands"] = [[sys.executable, "-c", "from pathlib import Path; Path('hello.py').write_text('bad')"]]
        with self.assertRaisesRegex(HarnessError, "验证命令修改"):
            self.engine().start("Implement greet")

    def test_dependent_tasks_receive_separate_dispatches_and_bounded_handoffs(self):
        class TwoTasks(DemoBackend):
            def invoke(self, **kwargs):
                if kwargs["role"] == "worker" and kwargs["task"]["id"] == "document":
                    ids = {k: kwargs["schema"]["properties"][k]["enum"][0] for k in ("task_id", "dispatch_id")}
                    packet = json.loads(kwargs["prompt"].split("\n\nREFERENCE MEMORY")[0])["packet"]
                    assert "greet" in packet["dependencies"]
                    (kwargs["cwd"] / "usage.txt").write_text("Use greet(name)")
                    return {**ids, "status": "completed", "summary": "Documented greet", "evidence": ["usage.txt"],
                            "blockers": [], "blocker_kind": "none", "proposed_memories": []}
                return super().invoke(**kwargs)
        plan = read_json(ROOT / "examples/plan.json")
        plan["tasks"].append({"id": "document", "title": "Document greet", "description": "Document greet in usage.txt",
                              "depends_on": ["greet"], "allowed_paths": ["usage.txt"], "acceptance": ["usage.txt explains greet"], "skills": []})
        engine = self.engine(TwoTasks())
        result = engine.start("Implement and document greet", plan)
        workers = [d for d in result["dispatches"] if d["role"] == "worker"]
        self.assertEqual([d["task_id"] for d in workers], ["greet", "document"])
        self.assertEqual(len({d["id"] for d in workers}), 2)
        self.assertEqual(len(engine.memory.export()), 2)

    def test_mutation_after_review_prevents_acceptance(self):
        engine = self.engine()
        emit = engine.hooks.emit
        def mutate(event, payload, logs):
            if event == "pre_accept":
                (engine.worktree / "hello.py").write_text("changed after review")
            return emit(event, payload, logs)
        engine.hooks.emit = mutate
        with self.assertRaisesRegex(HarnessError, "审阅后文件发生变化"):
            engine.start("Implement greet")
        self.assertEqual(engine.run["accepted"], {})
        self.assertEqual(engine.memory.export(), [])

    def test_resume_rejects_changed_configuration(self):
        engine = self.engine()
        engine.start("Implement greet", pause_after_plan=True)
        self.assertEqual(engine.run["status"], "awaiting_approval")
        self.config.data["roles"]["reviewer"]["model"] = "changed-model"
        with self.assertRaisesRegex(HarnessError, "配置已改变"):
            self.engine().resume(engine.run["id"])


class IntegrationTests(RepoCase):
    def test_adapter_commands_never_resume_or_bypass_permissions(self):
        backend = CLIBackend(self.config, self.repo / ".harness")
        job = self.repo / "job"
        job.mkdir()
        for role in ("commander", "worker", "reviewer"):
            for commander in ("astra", "fable"):
                argv = backend.command(role, self.config.role(role, commander), self.repo, job, WORK, None)
                self.assertFalse(any(x in ("resume", "--resume", "--continue", "--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox") for x in argv))
                if argv[0] == "codex":
                    self.assertIn("read-only", argv)
                    self.assertIn("--ephemeral", argv)
                else:
                    self.assertIn("--no-session-persistence", argv)
                    self.assertIn("--strict-mcp-config", argv)

    def test_generated_mcp_toml_is_parseable_and_shared(self):
        claude, codex = mcp_configs(self.config, self.repo / ".harness", "worker")
        parsed = tomllib.loads("mcp_servers = " + toml_value(codex))
        self.assertEqual(parsed["mcp_servers"]["harness"]["args"], claude["mcpServers"]["harness"]["args"])

    def test_hooks_receive_common_envelope_and_can_block(self):
        hooks = Hooks(self.config)
        hooks.spec = {"pre_agent": [{"argv": [sys.executable, "-c", "import sys,json; x=json.load(sys.stdin); sys.exit(7 if x['event']=='pre_agent' else 0)"]}]}
        with self.assertRaisesRegex(HarnessError, "exit=7"):
            hooks.emit("pre_agent", {"task_id": "x"}, self.repo / "logs")

    def test_timeout_kills_owned_child(self):
        pgids = []
        with self.assertRaises(HarnessError):
            run_process([sys.executable, "-c", "import time; time.sleep(30)"], self.repo,
                        self.repo / "timeout", timeout=1, on_start=pgids.append)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgids[0], 0)


if __name__ == "__main__":
    unittest.main()
