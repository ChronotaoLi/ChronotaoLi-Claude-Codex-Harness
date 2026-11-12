"""The worker writes the commit message; the reviewer judges it; the runtime commits it verbatim.

Every case runs real Git. The message's single authority is the sealed worker
result that the approving review's worker_source binds by digest.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.budgets import work_attempts
from harness.contracts import WORK, judge_work
from harness.git import commit_message_text
from harness.shared import HookError
from harness.util import HarnessError, read_json, write_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend, finding


def message(subject, body=""):
    return {"subject": subject, "body": body}


def commit_object(repo, commit):
    """The raw commit header and message bytes."""
    header, _, body = git.git_bytes(repo, "cat-file", "commit", commit).partition(b"\n\n")
    return header, body


def read_trailers(repo, commit):
    """The two documented read commands, with the trailer separator pinned."""
    def run(*args, **kwargs):
        return subprocess.run(["git", "-C", str(repo), "-c", "trailer.separators=:", *args],
                              capture_output=True, check=True, **kwargs).stdout.decode()
    logged = run("log", "-1", "--format=%(trailers)", commit)
    parsed = run("interpret-trailers", "--parse", "--no-divider", input=commit_object(repo, commit)[1])
    return [line for line in logged.splitlines() if line], parsed.splitlines()


def worker_originals(engine):
    """Each invoked worker dispatch's sealed result, in dispatch order."""
    return [read_json(engine.directory / "dispatches" / entry["id"] / "returned-result.json")
            for entry in engine.run["dispatches"] if entry.get("role") == "worker" and entry.get("invoked")]


def worker_source(engine, entry):
    artifact = engine.directory / "dispatches" / entry["id"] / "returned-result.json"
    return {"artifact": str(artifact), "artifact_digest": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "dispatch_id": entry["id"]}


class CommitMessageTests(RepoCase):
    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def run_with(self, change):
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        return engine, backend, engine.start("Implement greet", self.plan())

    def workers(self, backend):
        return sum(call["role"] == "worker" for call in backend.calls)

    def test_commit_is_the_worker_message_verbatim_under_hostile_repository_config(self):
        for key, value in (("commit.cleanup", "strip"), ("i18n.commitEncoding", "ISO-8859-1"),
                           ("author.name", "Repository Author"), ("committer.email", "repo@example.invalid")):
            git.git(self.repo, "config", key, value)
        wanted = message("实现 greet：返回问候语",
                         "# kept heading\nline with trailing spaces   \n\n\nafter two blank lines\n\tindented")

        def write(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["commit_message"] = dict(wanted)
        engine, backend, result = self.run_with(write)
        self.assertEqual(result["status"], "completed")
        commit = result["accepted"]["greet"]["commit"]
        header, body = commit_object(self.repo, commit)
        self.assertEqual(body, commit_message_text(wanted, result["id"], "greet").encode("utf-8"))
        lines = header.split(b"\n")
        self.assertFalse(any(line.startswith(b"encoding ") for line in lines))
        self.assertTrue(any(line.startswith(b"author Personal Harness <harness@localhost> ") for line in lines))
        self.assertTrue(any(line.startswith(b"committer Personal Harness <harness@localhost> ") for line in lines))
        expected = [f"Harness-Run: {result['id']}", "Harness-Task: greet"]
        self.assertEqual(read_trailers(self.repo, commit), (expected, expected))

    def test_empty_body_is_subject_blank_line_and_two_trailers(self):
        def write(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["commit_message"] = message("Add greet")
        engine, backend, result = self.run_with(write)
        _, body = commit_object(self.repo, result["accepted"]["greet"]["commit"])
        self.assertEqual(body, f"Add greet\n\nHarness-Run: {result['id']}\nHarness-Task: greet\n".encode())

    def test_pinned_read_commands_find_only_runtime_trailers(self):
        git.git(self.repo, "config", "trailer.separators", "=")
        text = "Explain the change.\n---\nNot a patch divider.\n\nSigned-off-by: A <a@example.invalid>"

        def write(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["commit_message"] = message("Add greet", text)
        engine, backend, result = self.run_with(write)
        expected = [f"Harness-Run: {result['id']}", "Harness-Task: greet"]
        self.assertEqual(read_trailers(self.repo, result["accepted"]["greet"]["commit"]), (expected, expected))

    def test_completed_message_rules_through_the_judgment(self):
        state = Path(tempfile.gettempdir()) / "b5-state" / ".harness"
        base = {"status": "completed", "summary": "Done", "evidence": [], "blockers": [],
                "blocker_kind": "none", "proposed_memories": []}
        cases = [
            (message("x" * 100), "valid"), (message("界" * 100), "valid"), (message("x" * 101), "invalid"),
            (message("   "), "invalid"), (message("ok", "y" * 4000), "valid"), (message("ok", "y" * 4001), "invalid"),
            (message("ok", "line\n\tindented\n"), "valid"),
            (message("a\nb"), "invalid"), (message("a\tb"), "invalid"), (message("a\x7fb"), "invalid"),
            (message("a\x85b"), "invalid"), (message("ok", "a\r\nb"), "invalid"), (message("ok", "a\x00b"), "invalid"),
            (message("ok", "a\ud800b"), "invalid"), (message("ok", "\udcff"), "invalid"),
            (message("ok", "x\nharness-task: x"), "invalid"), (message("ok", " Harness-Run :x"), "invalid"),
            (message("Harness-Task: t"), "invalid"), (message("ok", "Harness-Runner: fine"), "valid"),
            (message(f"see {state}/runs"), "invalid"),
            (message("ok", f"see {os.path.realpath(state)}/worktrees"), "invalid"),
            ({"subject": "ok"}, "invalid"), (None, "invalid"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(judge_work({**base, "commit_message": value}, WORK, state).state, expected)
        self.assertEqual(judge_work(base, WORK, state).state, "invalid")

    def test_message_with_a_trailer_line_costs_one_work_attempt(self):
        def write(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["commit_message"] = (message("Add greet", "Harness-Run: forged")
                                            if self.workers(backend) == 1 else message("Add greet(name)"))
        engine, backend, result = self.run_with(write)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        first = next(entry for entry in result["dispatches"] if entry["role"] == "worker")
        self.assertEqual(first["status"], "invalid")
        _, body = commit_object(self.repo, result["accepted"]["greet"]["commit"])
        self.assertTrue(body.startswith(b"Add greet(name)\n\nHarness-Run: "))

    def test_completed_result_without_message_costs_one_work_attempt(self):
        def write(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and self.workers(backend) == 1:
                result.pop("commit_message")
        engine, backend, result = self.run_with(write)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], {"greet": 2})
        first = next(entry for entry in result["dispatches"] if entry["role"] == "worker")
        self.assertEqual(first["status"], "invalid")
        self.assertNotIn("pending_task", result)

    def rejected_then_approved(self, gated=False):
        """Review 1 rejects worker A's subject; worker B changes only the message; review 2 approves."""
        wanted = "feat: add greet"

        def script(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["commit_message"] = message("greet stuff" if self.workers(backend) == 1 else wanted)
            if kwargs["kind"] == "review" and packet["commit_message"]["subject"] != wanted:
                result.update(verdict="request_changes", findings=[{
                    **finding(), "location": "commit_message.subject",
                    "message": "The repository requires a conventional commit subject"}])
        backend = ScriptedBackend(script)
        engine = self.engine(backend)
        if gated:
            engine.hooks.spec = self.pre_accept_gate(engine)
        return engine, backend, wanted

    def test_review_judges_the_message_and_the_repair_commits_the_new_one(self):
        engine, backend, wanted = self.rejected_then_approved()
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        originals = [original["commit_message"] for original in worker_originals(engine)]
        reviewed = [call["packet"]["commit_message"] for call in backend.calls if call["kind"] == "review"]
        self.assertEqual(reviewed, originals)
        self.assertEqual([item["subject"] for item in originals], ["greet stuff", wanted])
        _, body = commit_object(self.repo, result["accepted"]["greet"]["commit"])
        self.assertTrue(body.startswith(f"{wanted}\n\n".encode()))

    def pre_accept_gate(self, engine):
        marker = engine.state / "acceptance-permitted"
        return {"pre_accept": [{"argv": [sys.executable, "-c",
                "import pathlib,sys; sys.exit(0 if pathlib.Path(sys.argv[1]).exists() else 7)", str(marker)]}]}

    def stopped_before_acceptance(self):
        """Worker B's approval waits in pending_acceptance behind a failing pre_accept hook."""
        engine, backend, wanted = self.rejected_then_approved(gated=True)
        with self.assertRaises(HookError):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertIn("pending_acceptance", saved)
        (engine.state / "acceptance-permitted").touch()
        first = next(entry for entry in saved["dispatches"] if entry["role"] == "worker")
        return engine, backend, saved, worker_source(engine, first)

    def resume_refused(self, engine, backend, saved, pattern):
        write_json(engine.directory / "state.json", saved)
        head = git.head(engine.worktree)
        resumed = self.engine(backend)
        resumed.hooks.spec = self.pre_accept_gate(resumed)
        with self.assertRaisesRegex(HarnessError, pattern):
            resumed.resume(saved["id"])
        self.assertEqual(git.head(engine.worktree), head)
        self.assertFalse((engine.directory / "hooks" / "greet-2" / "pre_accept-2").exists())
        self.assertIn("pending_acceptance", read_json(engine.directory / "state.json"))

    def test_acceptance_refuses_a_worker_source_other_than_the_approved_one(self):
        engine, backend, saved, rejected = self.stopped_before_acceptance()
        saved["pending_acceptance"]["worker_source"] = rejected
        # Even with that dispatch's work attempt made to match, only the approving record's source counts.
        entry = next(item for item in saved["dispatches"] if item["id"] == rejected["dispatch_id"])
        entry["worker_receipt"]["attempt"] = saved["pending_acceptance"]["attempt"]
        self.resume_refused(engine, backend, saved, "来源与批准它的审阅不符")

    def test_acceptance_refuses_a_swapped_review_record_by_its_work_attempt(self):
        engine, backend, saved, rejected = self.stopped_before_acceptance()
        pending = saved["pending_acceptance"]
        pending["worker_source"] = rejected
        record = next(item for item in saved["review_history"]["greet"]
                      if item.get("review_source") == pending["review_source"])
        record["worker_source"] = dict(rejected)
        self.resume_refused(engine, backend, saved, "来源与批准它的审阅不符")

    def test_acceptance_requires_a_complete_worker_source(self):
        engine, backend, saved, _ = self.stopped_before_acceptance()
        pending = saved["pending_acceptance"]
        record = next(item for item in saved["review_history"]["greet"]
                      if item.get("review_source") == pending["review_source"])
        for source in (pending["worker_source"], record["worker_source"]):
            source.pop("artifact_digest")
        self.resume_refused(engine, backend, saved, "worker source digest required")

    def test_review_packet_refuses_a_ledger_result_that_differs_from_the_original(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)

        def stop(*args, **kwargs):
            raise KeyboardInterrupt
        with patch.object(engine.services.receipts, "dispatch_review", side_effect=stop), \
                self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["pending_task"]["stage"], "reviewing")
        saved["pending_task"]["worker_result"]["commit_message"] = message("A message nobody returned")
        write_json(engine.directory / "state.json", saved)
        with self.assertRaisesRegex(HarnessError, "worker 结果台账与原件不同"):
            self.engine(backend).resume(saved["id"])
        self.assertFalse(any(call["kind"] == "review" for call in backend.calls))

    def hook_changes_original(self, mode):
        script = ("import json,pathlib,sys; path = pathlib.Path(sys.argv[1]); "
                  "value = json.loads(path.read_text()); value['commit_message']['subject'] = 'Changed by a hook'; "
                  "path.unlink() if sys.argv[2] == 'delete' else path.write_text(json.dumps(value))")

        def arm(backend, kwargs, packet, result):
            if kwargs["kind"] == "review":
                artifact = engine.run["pending_task"]["worker_artifact"]
                engine.hooks.spec = {"pre_accept": [{"argv": [sys.executable, "-c", script, artifact, mode]}]}
        backend = ScriptedBackend(arm)
        engine = self.engine(backend)
        base = git.head(self.repo)
        with self.assertRaisesRegex(HarnessError, "原始结果已改变|non-symlink file"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(git.head(engine.worktree), base)
        saved = read_json(engine.directory / "state.json")
        self.assertIn("pending_acceptance", saved)
        self.assertEqual(saved["accepted"], {})
        self.assertTrue((engine.directory / "hooks" / "greet-1" / "pre_accept-1").exists())

    def test_hook_that_rewrites_the_worker_original_stops_before_commit(self):
        self.hook_changes_original("rewrite")

    def test_hook_that_deletes_the_worker_original_stops_before_commit(self):
        self.hook_changes_original("delete")

    def test_interruption_after_the_commit_never_commits_twice(self):
        def write(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result["commit_message"] = message("Add greet", "Committed before the ledger save.")
        backend = ScriptedBackend(write)
        engine = self.engine(backend)
        base = git.head(self.repo)
        accept = git.accept

        def commit_then_die(*args, **kwargs):
            accept(*args, **kwargs)
            raise KeyboardInterrupt
        with patch("harness.git.accept", side_effect=commit_then_die), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        saved = read_json(engine.directory / "state.json")
        head = git.head(engine.worktree)
        self.assertNotEqual(head, saved["expected_head"])
        with self.assertRaisesRegex(HarnessError, "HEAD 与检查点不同"):
            self.engine(backend).resume(saved["id"])
        self.assertEqual(git.head(engine.worktree), head)
        self.assertEqual(git.git(self.repo, "rev-list", "--count", f"{base}..{head}").strip(), "1")
        original = read_json(Path(saved["pending_acceptance"]["worker_source"]["artifact"]))["commit_message"]
        self.assertEqual(commit_object(self.repo, head)[1],
                         commit_message_text(original, saved["id"], "greet").encode())

    def test_interruption_after_staging_voids_the_approval_and_commits_the_new_message(self):
        def write(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                acceptance = "acceptance" in (packet.get("feedback") or {})
                result["commit_message"] = message("Second message" if acceptance else "First message")
        backend = ScriptedBackend(write)
        engine = self.engine(backend)
        base = git.head(self.repo)
        run_git = git.git

        def die_before_commit(repo, *args, **kwargs):
            if "--cleanup=verbatim" in args:
                raise KeyboardInterrupt
            return run_git(repo, *args, **kwargs)
        with patch("harness.git.git", side_effect=die_before_commit), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.workers(backend), 2)
        head = result["accepted"]["greet"]["commit"]
        self.assertEqual(git.git(self.repo, "rev-list", "--count", f"{base}..{head}").strip(), "1")
        self.assertTrue(commit_object(self.repo, head)[1].startswith(b"Second message\n\n"))


class BlockedMessageTests(RepoCase):
    """A blocked result never commits: its commit_message, valid or not, changes nothing."""

    def plan(self):
        return read_json(ROOT / "examples" / "plan.json")

    def blocked(self, kind, value):
        def script(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(call["role"] == "worker" for call in backend.calls) == 1:
                result.update(status="blocked", blocker_kind=kind, blockers=[f"Needs {kind} input"],
                              commit_message=value)
        backend = ScriptedBackend(script)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", self.plan())
        entry = next(item for item in engine.run["dispatches"] if item["role"] == "worker")
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["worker_outcome"], f"{kind}_blocked")
        self.assertEqual(read_json(engine.directory / "dispatches" / entry["id"] / "returned-result.json")["commit_message"],
                         value)
        return engine, backend, entry

    def test_environment_blocker_with_a_null_message_keeps_route_exemption_and_history(self):
        engine, backend, entry = self.blocked("environment", None)
        self.assertIn("attempt_exemption", entry)
        # Loading rechecks the exemption's source through the same core projection.
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")

    def test_operator_blocker_with_a_malformed_message_keeps_its_gate_and_exemption(self):
        engine, backend, entry = self.blocked("operator", {"subject": ""})
        self.assertIn("attempt_exemption", entry)
        self.assertTrue(engine.run["decision_requests"])

    # Null, an object missing `body`, and a body over the bound.
    INVALID_MESSAGES = {"null": None, "missing body": {"subject": "Fix greet"},
                        "oversized body": message("x", "y" * 5000)}

    def recover(self, kind, value):
        """Block on `kind` with `value` as commit_message, then take the documented exit."""
        self.setUp()
        engine, backend, entry = self.blocked(kind, value)
        options = {}
        if kind == "environment":
            options["environment_update"] = self.repo / "operator-update.json"
            write_json(options["environment_update"], {
                "schema_version": 1, "task_id": "greet", "blocker_dispatch_id": entry["id"],
                "summary": "Dependency installed", "checks": []})
        else:
            calls = len(backend.calls)
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run["id"])
            self.assertEqual(len(backend.calls), calls)
            request = engine.run["decision_requests"][0]
            options["operator_decision"] = self.repo / "answer.json"
            write_json(options["operator_decision"], {
                "schema_version": 1, "request_id": request["id"], "task_id": "greet",
                "decision": "continue", "summary": "Proceed"})
        result = self.engine(backend).resume(engine.run["id"], **options)
        self.assertEqual(result["status"], "completed")
        source = next(item for item in result["dispatches"] if item["id"] == entry["id"])
        self.assertEqual(source["worker_outcome"], f"{kind}_blocked")
        self.assertEqual(source["attempt_exemption"], entry["attempt_exemption"])
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(work_attempts(result), {"greet": 1})
        for call in backend.calls:
            if call["role"] == "commander":
                self.assertNotIn("commit_message", json.dumps(call["packet"]))
        return result, [(call["role"], call["kind"], (call["packet"].get("trigger") or {}).get("kind"))
                        for call in backend.calls]

    def assert_history_recheck_ignores_the_message(self, kind):
        reference, route = self.recover(kind, message("Fix greet"))
        for label, value in self.INVALID_MESSAGES.items():
            with self.subTest(message=label):
                result, observed = self.recover(kind, value)
                self.assertEqual(observed, route)
                for key in ("environment_updates", "operator_decisions"):
                    self.assertEqual(len(result.get(key, [])), len(reference.get(key, [])))

    def test_environment_update_recovery_rechecks_the_blocker_whatever_its_message(self):
        self.assert_history_recheck_ignores_the_message("environment")

    def test_operator_answer_recovery_rechecks_the_blocker_whatever_its_message(self):
        self.assert_history_recheck_ignores_the_message("operator")

    def test_task_blocker_with_an_oversized_message_reaches_the_commander_without_it(self):
        engine, backend, entry = self.blocked("task", message("x", "y" * 5000))
        self.assertNotIn("attempt_exemption", entry)
        commander = [call for call in backend.calls if call["role"] == "commander"][-1]
        self.assertEqual(commander["packet"]["trigger"]["kind"], "task")
        self.assertNotIn("commit_message", json.dumps(commander["packet"]))
