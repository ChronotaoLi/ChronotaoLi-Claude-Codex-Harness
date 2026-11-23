"""Resource lifecycle: verified-checkout reclamation, --remove-ignored and the instant ignored listing.

Every case uses real Git in a temporary repository and simulated agents.
"""
import contextlib
import errno
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from git_isolation import fixture_git
from harness import cleanup, git, state_files
from harness.versions import RUN_SCHEMA_VERSION
from harness.cleanup import cleanup_run
from harness.cli import main
from harness.util import HarnessError, read_json, write_json
from test_harness import RepoCase, ROOT, crash_after_save, crash_point
from test_readonly_cleanup import nonregular, tree_bytes
from test_runtime_changes import ScriptedBackend
from test_state_files import FakeOs


def events(engine):
    return [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]


def reviewing(run):
    return bool(run["final_history"]) and run["final_history"][-1]["phase"] == "reviewing"


class ReclamationTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.config.data["verification"]["fresh_checkout"] = True
        self.roots = []
        self.addCleanup(lambda: [shutil.rmtree(root, ignore_errors=True) for root in self.roots])

    def artifacts(self, engine):
        found = sorted(engine.directory.glob("verification/final/*/verification-*/checkout.json"))
        for artifact in found:
            root = Path(read_json(artifact)["worktree"]).parents[1]
            if root not in self.roots:
                self.roots.append(root)
        return found

    def final_checks(self, run):
        return [e for e in run["dispatches"] if e["role"] == "verification" and e["task_id"] == "final"]

    def plan(self):
        return read_json(ROOT / "examples/plan.json")

    def stop_at_verdict(self, backend):
        """A run whose verdict (phase=reviewing) is saved and whose process then died."""
        engine = self.engine(backend)
        crash, _ = crash_after_save(engine, reviewing)
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        return engine

    def foreign_record(self, head):
        """A legitimate checkout record of another run (or round) verifying the same HEAD."""
        root = Path(tempfile.mkdtemp(prefix="harness-verify-")).resolve()
        self.roots.append(root)
        checkout = root / ".harness" / "checkout"
        fixture_git(self.repo, "worktree", "add", "--detach", str(checkout), head)
        return checkout, {"mode": "fresh_checkout", "worktree": str(checkout), "head": head,
                          "status": "verified", "setup_commands": []}

    def test_verified_checkout_with_ignored_cache_is_reclaimed_after_the_verdict(self):
        # The default unittest command leaves an ignored __pycache__ in the checkout.
        backend = ScriptedBackend()
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        artifact, = self.artifacts(engine)
        record = read_json(artifact)
        checkout = Path(record["worktree"])
        entry, = result["verification_checkouts"]
        self.assertEqual((entry["attempt"], entry["round"], entry["worktree"]), (1, 1, str(checkout)))
        self.assertTrue({"owner", "binding"}.isdisjoint(record))
        self.assertEqual(result["final_history"][-1]["verification_rounds"][-1]["checkout_digest"],
                         hashlib.sha256(artifact.read_bytes()).hexdigest())
        self.assertFalse(checkout.exists())
        self.assertFalse(checkout.parents[1].exists())
        receipt = read_json(artifact.with_name("cleanup.json"))
        self.assertEqual((receipt["status"], receipt["by"]), ("removed", "runtime"))
        self.assertNotIn(str(checkout), git.git(self.repo, "worktree", "list", "--porcelain"))
        names = [e["event"] for e in events(engine)]
        self.assertEqual(names.count("verification_checkout_reclaimed"), 1)
        self.assertLess(names.index("verification_checkout_reclaimed"),
                        max(i for i, name in enumerate(names) if name == "dispatch_started"))
        preview = cleanup_run(self.repo, result["id"])["checkouts"]
        self.assertEqual([(i["status"], i["removed_by"]) for i in preview], [("absent", "runtime")])

    def test_verified_record_before_the_verdict_save_stays_and_the_next_round_is_separate(self):
        # W0: checkout.json already says verified, the verdict save never happened.
        backend = ScriptedBackend()
        engine = self.engine(backend)
        crash, die = crash_point(engine)
        verify = engine.services.verification.verify

        def verify_then_die(*args, **kwargs):
            result = verify(*args, **kwargs)
            if kwargs.get("whole_run"):
                die()
            return result
        with crash, patch.object(engine.services.verification, "verify", side_effect=verify_then_die), \
                self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", self.plan())
        first, = self.artifacts(engine)
        self.assertEqual(read_json(first)["status"], "verified")
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(saved["final_history"][-1]["verification_rounds"], [
            {"artifact_dir": str(first.parent), "status": "running"}])
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        first, second = self.artifacts(engine)
        self.assertTrue(Path(read_json(first)["worktree"]).is_dir())
        self.assertFalse(first.with_name("cleanup.json").exists())
        self.assertEqual([(entry["round"], entry["worktree"]) for entry in result["verification_checkouts"]],
                         [(1, read_json(first)["worktree"]), (2, read_json(second)["worktree"])])
        self.assertFalse(Path(read_json(second)["worktree"]).exists())
        preview = cleanup_run(self.repo, result["id"])["checkouts"]
        self.assertEqual([i["status"] for i in preview], ["eligible", "absent"])

    def test_current_ledger_previews_non_fresh_retries_without_round_owners(self):
        self.config.data["verification"]["fresh_checkout"] = False
        for verbosity in ("-v", "-q"):
            with self.subTest(verbosity=verbosity):
                command = {"cwd": ".", "argv": [sys.executable, "-B", "-m", "unittest", verbosity]}
                self.config.data["verification"]["commands"] = [command]
                backend = ScriptedBackend()
                engine = self.engine(backend)
                crash, die = crash_point(engine)
                verify = engine.services.verification.verify

                def verify_then_die(*args, **kwargs):
                    result = verify(*args, **kwargs)
                    if kwargs.get("whole_run"):
                        die()
                    return result

                with crash, patch.object(engine.services.verification, "verify", side_effect=verify_then_die), \
                        self.assertRaises(KeyboardInterrupt):
                    engine.start("Implement greet", self.plan())
                result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual((result["schema_version"], result["status"]), (RUN_SCHEMA_VERSION, "completed"))
                final, = result["final_history"]
                first, second = final["verification_rounds"]
                self.assertEqual((first["status"], second["status"]), ("running", "completed"))
                self.assertEqual(final["verification"], str(Path(second["artifact_dir"]) / "results.json"))

                dispatch_ids = []
                dispatches = {entry["id"]: entry for entry in self.final_checks(result)}
                for record in (first, second):
                    folder = Path(record["artifact_dir"])
                    check, = read_json(folder / "results.json")
                    log = folder / "check-0"
                    self.assertEqual((check["log"], check["status"]), (str(log), "passed"))
                    dispatch_id = check["timing_source"]["dispatch_id"]
                    dispatch_ids.append(dispatch_id)
                    entry = dispatches[dispatch_id]
                    self.assertEqual(check["command"], command)
                    self.assertEqual(entry["command"], command)
                    process_record = engine.directory / entry["process_record"]
                    self.assertEqual(entry["process_record"],
                                     log.with_suffix(".outcome.json").relative_to(engine.directory).as_posix())
                    self.assertTrue(process_record.resolve(strict=True).is_relative_to(engine.directory.resolve()))
                    self.assertEqual(entry["id"], dispatch_id)
                    self.assertEqual(entry["worktree"], result["worktree"])

                    pgid = entry["pgid"]
                    self.assertIs(type(pgid), int)
                    self.assertGreater(pgid, 0)
                    facts = {
                        "started": True,
                        "startup_state": "started",
                        "pid": pgid,
                        "pgid": pgid,
                        "exit_code": 0,
                        "termination_reason": "normal",
                        "exit_confirmed": True,
                        "cleanup_errors": [],
                        "cleanup_actions": ["poll_leader"],
                        "evidence_complete": True,
                        "errors": [],
                    }
                    outcome = read_json(process_record)
                    self.assertEqual(outcome, {**facts, "owner": entry["id"]})
                    for copy in (check["process_outcome"], entry["process_outcome"]):
                        self.assertNotIn("owner", copy)
                        self.assertEqual(copy, facts)
                    self.assertTrue({"run_id", "attempt", "round", "dispatch_id"}.isdisjoint(outcome))
                    self.assertNotIn("owner", entry)
                    self.assertNotIn("checkout_digest", record)
                self.assertEqual(set(dispatch_ids), set(dispatches))
                self.assertNotEqual(*dispatch_ids)

                worktree = Path(result["worktree"])
                before = (tree_bytes(engine.directory), tree_bytes(worktree))
                preview = cleanup_run(self.repo, result["id"])
                self.assertEqual((preview["remove"], preview["checkouts"]), (False, []))
                self.assertEqual((preview["worktree"]["worktree"], preview["worktree"]["status"]),
                                 (str(worktree), "eligible"), preview["worktree"])
                self.assertEqual((tree_bytes(engine.directory), tree_bytes(worktree)), before)

    def test_crash_after_verdict_save_reclaims_on_resume_without_reverifying(self):
        backend = ScriptedBackend()
        engine = self.stop_at_verdict(backend)
        artifact, = self.artifacts(engine)
        checkout = Path(read_json(artifact)["worktree"])
        self.assertTrue(checkout.is_dir())
        self.assertFalse(artifact.with_name("cleanup.json").exists())
        before = len(self.final_checks(read_json(engine.directory / "state.json")))
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.final_checks(result)), before)
        self.assertEqual(len(result["final_history"][-1]["verification_rounds"]), 1)
        self.assertFalse(checkout.exists())
        self.assertEqual(read_json(artifact.with_name("cleanup.json"))["status"], "removed")
        self.assertEqual(sum(c["kind"] == "final_review" for c in backend.calls), 1)

    def test_interrupted_reclamation_windows_resume_without_repeated_deletion(self):
        # W2: intent saved, Git not run. W3: Git removed everything, completion not written.
        # W3': the directory is gone but the exact registration remains.
        for window in ("before_git", "after_git", "directory_only"):
            with self.subTest(window=window):
                backend = ScriptedBackend()
                engine = self.engine(backend)
                crash, die = crash_point(engine)
                original = git.git_process

                def interrupted(repo, *args, **kwargs):
                    if args[:2] == ("worktree", "remove"):
                        if window == "before_git":
                            die()
                        if window == "directory_only":
                            shutil.rmtree(args[2])
                            die()
                        value = original(repo, *args, **kwargs)
                        die()
                    return original(repo, *args, **kwargs)
                with crash, patch("harness.cleanup.git.git_process", side_effect=interrupted), \
                        self.assertRaises(KeyboardInterrupt):
                    engine.start("Implement greet", self.plan())
                artifact, = self.artifacts(engine)
                record = read_json(artifact)
                checkout = Path(record["worktree"])
                receipt = artifact.with_name("cleanup.json")
                self.assertEqual(read_json(receipt)["status"], "pending")
                self.assertEqual(checkout.exists(), window == "before_git")
                registered = str(checkout) in git.git(self.repo, "worktree", "list", "--porcelain")
                self.assertEqual(registered, window != "after_git")
                # Another registration that must survive; created only while the original Git
                # administrative directory still exists (Git reuses a freed name).
                other = None
                if window != "after_git":
                    other, _ = self.foreign_record(record["head"])
                removals = []

                def recording(repo, *args, **kwargs):
                    if args[:2] == ("worktree", "remove"):
                        removals.append(args[2])
                    return original(repo, *args, **kwargs)
                with patch("harness.cleanup.git.git_process", side_effect=recording):
                    result = self.engine(backend).resume(engine.run["id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(removals, [] if window == "after_git" else [str(checkout)])
                self.assertFalse(checkout.exists())
                self.assertEqual(read_json(receipt)["status"], "removed")
                listing = git.git(self.repo, "worktree", "list", "--porcelain")
                self.assertNotIn(str(checkout) + "\n", listing)
                if other is not None:
                    self.assertIn(str(other), listing)
                self.assertEqual(sum(c["kind"] == "final_review" for c in backend.calls), 1)

    def test_reclamation_failure_keeps_the_verdict_and_leaves_the_checkout_to_cleanup(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        original = git.git_process

        def failing(repo, *args, **kwargs):
            if args[:2] == ("worktree", "remove"):
                return subprocess.CompletedProcess(args, 1, b"", b"simulated removal failure")
            return original(repo, *args, **kwargs)
        with patch("harness.cleanup.git.git_process", side_effect=failing):
            result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_history"][-1]["status"], "approved")
        self.assertEqual(len(result["final_history"]), 1)
        self.assertEqual(len(result["final_history"][-1]["verification_rounds"]), 1)
        self.assertEqual(sum(c["kind"] == "final_review" for c in backend.calls), 1)
        artifact, = self.artifacts(engine)
        checkout = Path(read_json(artifact)["worktree"])
        self.assertTrue(checkout.is_dir())
        self.assertEqual(read_json(artifact.with_name("cleanup.json"))["status"], "pending")
        failed = [e for e in events(engine) if e["event"] == "verification_checkout_reclaim_failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("simulated", failed[0]["error"])
        self.assertEqual(failed[0]["recorded_worktree"], str(checkout))
        item, = cleanup_run(self.repo, result["id"], remove=True)["checkouts"]
        self.assertEqual((item["status"], item["worktree"]), ("removed", str(checkout)))

    def test_reclamation_events_are_observations_only(self):
        for remove_fails in (True, False):
            with self.subTest(remove_fails=remove_fails):
                backend = ScriptedBackend()
                engine = self.engine(backend)
                original_git, original_event = git.git_process, engine.context.event

                def failing_git(repo, *args, **kwargs):
                    if remove_fails and args[:2] == ("worktree", "remove"):
                        return subprocess.CompletedProcess(args, 1, b"", b"simulated removal failure")
                    return original_git(repo, *args, **kwargs)

                def failing_event(name, **fields):
                    if name.startswith("verification_checkout_"):
                        raise OSError("event append unavailable")
                    return original_event(name, **fields)
                with patch("harness.cleanup.git.git_process", side_effect=failing_git), \
                        patch.object(engine.context, "event", side_effect=failing_event):
                    result = engine.start("Implement greet", self.plan())
                self.assertEqual(result["status"], "completed")
                self.assertEqual(sum(c["kind"] == "final_review" for c in backend.calls), 1)
                artifact, = self.artifacts(engine)
                self.assertEqual(read_json(artifact.with_name("cleanup.json"))["status"],
                                 "pending" if remove_fails else "removed")
                self.assertEqual(Path(read_json(artifact)["worktree"]).exists(), remove_fails)

    def test_malformed_cleanup_receipt_is_a_reclamation_failure_not_a_stop(self):
        for content in ("[]", "null", '"removed"', "{not json"):
            with self.subTest(content=content):
                backend = ScriptedBackend()
                engine = self.stop_at_verdict(backend)
                artifact, = self.artifacts(engine)
                checkout = Path(read_json(artifact)["worktree"])
                receipt = artifact.with_name("cleanup.json")
                receipt.write_text(content)
                before = len(self.final_checks(read_json(engine.directory / "state.json")))
                result = self.engine(backend).resume(engine.run["id"])
                # The damaged receipt keeps the checkout; the verdict and the review are untouched.
                self.assertEqual(result["status"], "completed")
                self.assertEqual(len(self.final_checks(result)), before)
                self.assertEqual(len(result["final_history"][-1]["verification_rounds"]), 1)
                self.assertEqual(sum(c["kind"] == "final_review" for c in backend.calls), 1)
                self.assertTrue(checkout.is_dir())
                self.assertEqual(receipt.read_text(), content)
                failed = [e for e in events(engine) if e["event"] == "verification_checkout_reclaim_failed"]
                self.assertEqual(len(failed), 1)

    def test_authority_or_exit_failure_stops_instead_of_skipping_reclamation(self):
        from harness.finalization import FinalAcceptance
        for gate in ("ensure_exited", "check_current"):
            with self.subTest(gate=gate):
                backend = ScriptedBackend()
                engine = self.engine(backend)
                state = {"inside": False}
                reclaim = FinalAcceptance.reclaim_verified_checkout
                owner = engine.context if gate == "ensure_exited" else engine.context.store
                original = getattr(owner, gate)

                def flagged(service, record):
                    state["inside"] = True
                    try:
                        return reclaim(service, record)
                    finally:
                        state["inside"] = False

                def failing(*args, **kwargs):
                    if state["inside"]:
                        raise HarnessError(f"simulated {gate} failure")
                    return original(*args, **kwargs)
                with patch.object(FinalAcceptance, "reclaim_verified_checkout", flagged), \
                        patch.object(owner, gate, side_effect=failing), \
                        self.assertRaisesRegex(HarnessError, f"simulated {gate} failure"):
                    engine.start("Implement greet", self.plan())
                # Neither gate is a resource failure: the run stops, nothing is removed or reviewed.
                self.assertEqual(engine.run["status"], "blocked")
                self.assertFalse(any(c["kind"] == "final_review" for c in backend.calls))
                artifact, = self.artifacts(engine)
                self.assertTrue(Path(read_json(artifact)["worktree"]).is_dir())
                self.assertFalse(artifact.with_name("cleanup.json").exists())

    def test_gate_after_pending_propagates_its_own_stop_and_prevents_review(self):
        for gate in ("ledger_io", "exit"):
            with self.subTest(gate=gate):
                backend = ScriptedBackend()
                engine = self.stop_at_verdict(backend)
                artifact, = self.artifacts(engine)
                receipt = artifact.with_name("cleanup.json")
                checkout = Path(read_json(artifact)["worktree"])
                faults, removals = [], []
                git_process = git.git_process
                ensure = cleanup.ensure_dispatches_exited

                def fail_ledger_open(real_open, name, *args, **kwargs):
                    if gate == "ledger_io" and name == "state.json" and receipt.exists() and not faults:
                        faults.append("ledger_io")
                        raise OSError(errno.EIO, "injected ledger read error")
                    return real_open(name, *args, **kwargs)

                def fail_exit(dispatches, **kwargs):
                    if gate == "exit" and receipt.exists() and not faults:
                        faults.append("exit")
                        entry = next(entry for entry in dispatches if entry["role"] == "verification")
                        kwargs["observe"] = lambda pgid: True
                        return ensure([{**entry, "exit_confirmed": False, "pgid": 1}], **kwargs)
                    return ensure(dispatches, **kwargs)

                def record(repo, *args, **kwargs):
                    if args[:2] == ("worktree", "remove"):
                        removals.append(args[2])
                    return git_process(repo, *args, **kwargs)

                resumed = self.engine(backend)
                with patch.object(state_files, "os", FakeOs(open=fail_ledger_open)), \
                        patch.object(cleanup, "ensure_dispatches_exited", side_effect=fail_exit), \
                        patch.object(git, "git_process", side_effect=record), self.assertRaises(HarnessError):
                    resumed.resume(engine.run["id"])
                self.assertEqual(faults, [gate])
                self.assertEqual(removals, [])
                self.assertTrue(checkout.exists())
                self.assertEqual(read_json(receipt)["status"], "pending")
                ledger = read_json(engine.directory / "state.json")
                stop = ledger["stop"]
                expected = ("runtime_failure", "io") if gate == "ledger_io" else ("process_unconfirmed", "dispatch_exit")
                self.assertEqual((stop["kind"], stop["reason"]), expected)
                self.assertEqual(ledger["final_history"][-1]["phase"], "reviewing")
                self.assertFalse(any(c["kind"] == "final_review" for c in backend.calls))
                self.assertFalse(any(event["event"] == "verification_checkout_reclaim_failed"
                                     for event in events(engine)))
                self.assertEqual(self.engine(backend).resume(engine.run["id"])["status"], "completed")
                self.assertFalse(checkout.exists())
                self.assertEqual(read_json(receipt)["status"], "removed")

    def assert_both_kept_and_refused(self, engine, backend, kept):
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue([e for e in events(engine) if e["event"] == "verification_checkout_reclaim_failed"])
        for path in kept:
            self.assertTrue(path.is_dir(), str(path))
        removal = cleanup_run(self.repo, result["id"], remove=True)["checkouts"]
        self.assertTrue(all(item["status"] == "blocked" for item in removal), removal)
        self.assertTrue(all("recorded_worktree" in item and "worktree" not in item for item in removal))
        for path in kept:
            self.assertTrue(path.is_dir(), str(path))
        return removal

    def removed_receipt(self, engine, artifact):
        round_number = int(artifact.parent.name.removeprefix("verification-"))
        entry = next(entry for entry in engine.run["verification_checkouts"] if entry["round"] == round_number)
        return {"run_id": engine.run["id"], "attempt": entry["attempt"], "round": entry["round"],
                "worktree": entry["worktree"], "identity": entry["identity"],
                "status": "removed", "by": "cleanup-run"}

    def test_reclamation_cannot_bypass_the_removed_receipt_checks(self):
        for damaged in ("incomplete", "foreign_round", "run_id", "identity", "worktree", "reappeared"):
            with self.subTest(damaged=damaged):
                backend = ScriptedBackend()
                engine = self.stop_at_verdict(backend)
                artifact, = self.artifacts(engine)
                checkout_record = read_json(artifact)
                checkout = Path(checkout_record["worktree"])
                receipt = artifact.with_name("cleanup.json")
                value = self.removed_receipt(engine, artifact)
                if damaged == "incomplete":
                    value = {"status": "removed"}
                elif damaged == "foreign_round":
                    peer = self.stop_at_verdict(ScriptedBackend())
                    peer_artifact, = self.artifacts(peer)
                    peer_result, = cleanup_run(self.repo, peer.run["id"], remove=True)["checkouts"]
                    self.assertEqual(peer_result["status"], "removed")
                    value = read_json(peer_artifact.with_name("cleanup.json"))
                elif damaged != "reappeared":
                    value[damaged] = "does-not-match"
                write_json(receipt, value)
                original = receipt.read_bytes()

                removal = self.assert_both_kept_and_refused(engine, backend, [checkout])
                self.assertIn("重新出现" if damaged == "reappeared" else "清理记录", removal[0]["error"])
                self.assertEqual(receipt.read_bytes(), original)
                self.assertIn(str(checkout), git.git(self.repo, "worktree", "list", "--porcelain"))
                self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 1)

    def test_runtime_accepts_an_already_removed_verified_checkout_without_another_event(self):
        backend = ScriptedBackend()
        engine = self.stop_at_verdict(backend)
        artifact, = self.artifacts(engine)
        removed, = cleanup_run(self.repo, engine.run["id"], remove=True)["checkouts"]
        self.assertEqual(removed["status"], "removed")
        receipt = artifact.with_name("cleanup.json")
        original = receipt.read_bytes()
        real = git.git_process

        def query(repo, *args, **kwargs):
            self.assertNotEqual(args[:2], ("worktree", "remove"))
            return real(repo, *args, **kwargs)

        with patch.object(git, "git_process", side_effect=query):
            result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertFalse(any(event["event"].startswith("verification_checkout_reclaim") for event in events(engine)))
        self.assertEqual(receipt.read_bytes(), original)

    def test_reclamation_reads_the_receipt_once_and_retains_checkout_on_eio(self):
        backend = ScriptedBackend()
        engine = self.stop_at_verdict(backend)
        artifact, = self.artifacts(engine)
        checkout = Path(read_json(artifact)["worktree"])
        receipt = artifact.with_name("cleanup.json")
        write_json(receipt, {**self.removed_receipt(engine, artifact), "status": "pending"})
        original = receipt.read_bytes()
        opens, removals = [], []
        real_git = git.git_process

        def open_once(real, name, *args, **kwargs):
            if name == "cleanup.json":
                opens.append(name)
                if len(opens) == 1:
                    raise OSError(errno.EIO, "injected fault")
            return real(name, *args, **kwargs)

        def record_git(repo, *args, **kwargs):
            if args[:2] == ("worktree", "remove"):
                removals.append(args[2])
            return real_git(repo, *args, **kwargs)

        with patch.object(state_files, "os", FakeOs(open=open_once)), patch.object(git, "git_process", side_effect=record_git):
            result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_history"][-1]["status"], "approved")
        self.assertEqual(opens, ["cleanup.json"])
        self.assertEqual(removals, [])
        self.assertEqual(receipt.read_bytes(), original)
        self.assertTrue(checkout.is_dir())
        failure, = [event for event in events(engine) if event["event"] == "verification_checkout_reclaim_failed"]
        self.assertIn("cleanup.json", failure["error"])
        self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 1)

    def test_reclamation_diagnostics_use_the_derived_directory_when_ledger_path_changes(self):
        backend = ScriptedBackend()
        engine = self.stop_at_verdict(backend)
        artifact, = self.artifacts(engine)
        checkout = Path(read_json(artifact)["worktree"])
        foreign = self.outside / "foreign"
        write_json(foreign / "cleanup.json", {"status": "removed"})
        write_json(foreign / "checkout.json", {"worktree": "foreign"})
        state = engine.directory / "state.json"
        changed = read_json(state)
        changed["final_history"][-1]["verification_rounds"][-1]["artifact_dir"] = str(foreign)
        write_json(state, changed)

        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        failure, = [event for event in events(engine) if event["event"] == "verification_checkout_reclaim_failed"]
        self.assertEqual((failure["artifact"], failure["recorded_worktree"]), (str(artifact), str(checkout)))
        self.assertTrue(checkout.is_dir())
        self.assertEqual(read_json(foreign / "cleanup.json"), {"status": "removed"})
        self.assertEqual(read_json(foreign / "checkout.json"), {"worktree": "foreign"})

    def test_foreign_record_in_an_unconcluded_round_preserves_foreign_checkout(self):
        self.config.data["verification"]["setup_commands"] = [
            {"cwd": ".", "argv": [sys.executable, "-c", "raise SystemExit(1)"]}]
        engine = self.engine(ScriptedBackend())
        with self.assertRaisesRegex(HarnessError, "依赖准备失败"):
            engine.start("Implement greet", self.plan())
        artifact, = self.artifacts(engine)
        own = Path(read_json(artifact)["worktree"])
        other, record = self.foreign_record(read_json(artifact)["head"])
        cache = other / "__pycache__" / "keep"
        cache.parent.mkdir()
        cache.write_text("keep")
        write_json(artifact, record)
        preview, = cleanup_run(self.repo, engine.run["id"])["checkouts"]
        self.assertEqual((preview["status"], preview["worktree"]), ("eligible", str(own)))
        removed, = cleanup_run(self.repo, engine.run["id"], remove=True)["checkouts"]
        self.assertEqual((removed["status"], removed["worktree"]), ("removed", str(own)))
        self.assertFalse(own.exists())
        self.assertEqual(cache.read_text(), "keep")
        self.assertIn(str(other), fixture_git(self.repo, "worktree", "list", "--porcelain"))

    def test_records_swapped_between_rounds_still_remove_each_registered_checkout(self):
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        marker = Path(external.name) / "ready"
        self.config.data["verification"]["setup_commands"] = [{"cwd": ".", "argv": [sys.executable, "-c",
            f"from pathlib import Path; assert Path({str(marker)!r}).exists()"]}]
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "依赖准备失败"):
            engine.start("Implement greet", self.plan())
        marker.write_text("ready")
        resumed = self.engine(backend)
        crash, _ = crash_after_save(resumed, reviewing)
        with crash, self.assertRaises(KeyboardInterrupt):
            resumed.resume(engine.run["id"])
        first, second = self.artifacts(engine)
        dirs = [Path(read_json(first)["worktree"]), Path(read_json(second)["worktree"])]
        one, two = first.read_bytes(), second.read_bytes()
        first.write_bytes(two)
        second.write_bytes(one)
        preview = cleanup_run(self.repo, engine.run["id"])["checkouts"]
        self.assertEqual([(item["status"], item["worktree"]) for item in preview],
                         [("eligible", str(path)) for path in dirs])
        self.assertEqual(self.engine(backend).resume(engine.run["id"])["status"], "completed")
        self.assertTrue(dirs[0].exists())
        self.assertFalse(dirs[1].exists())
        removed = cleanup_run(self.repo, engine.run["id"], remove=True)["checkouts"]
        self.assertEqual([(item["status"], item["worktree"]) for item in removed],
                         [("removed", str(path)) for path in dirs])
        self.assertFalse(any(path.exists() for path in dirs))

    def test_failed_round_is_retained_and_cleanup_removes_its_ignored_files_without_option(self):
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        marker = Path(external.name) / "ready"
        self.config.data["verification"]["setup_commands"] = [{"cwd": ".", "argv": [sys.executable, "-c",
            "from pathlib import Path; Path('__pycache__').mkdir(exist_ok=True); "
            "Path('__pycache__/setup.log').write_text('x'); "
            f"assert Path({str(marker)!r}).exists()"]}]
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "依赖准备失败"):
            engine.start("Implement greet", self.plan())
        marker.write_text("ready")
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        first, second = self.artifacts(engine)
        self.assertEqual(read_json(first)["status"], "setup_failed")
        self.assertTrue(Path(read_json(first)["worktree"]).is_dir())
        self.assertFalse(first.with_name("cleanup.json").exists())
        self.assertFalse(Path(read_json(second)["worktree"]).exists())
        preview = cleanup_run(self.repo, result["id"])["checkouts"]
        self.assertEqual([i["status"] for i in preview], ["eligible", "absent"])
        removed = cleanup_run(self.repo, result["id"], remove=True)["checkouts"]
        self.assertEqual([i["status"] for i in removed], ["removed", "removed"])
        self.assertEqual([i.get("removed_by") for i in removed], [None, "runtime"])
        self.assertFalse(Path(read_json(first)["worktree"]).exists())

    def test_blocked_checkout_diagnostics_use_registration_or_reporting_record(self):
        self.config.data["verification"]["setup_commands"] = [
            {"cwd": ".", "argv": [sys.executable, "-c", "raise SystemExit(1)"]}]
        engine = self.engine(ScriptedBackend())
        with self.assertRaisesRegex(HarnessError, "依赖准备失败"):
            engine.start("Implement greet", self.plan())
        artifact, = self.artifacts(engine)
        checkout = Path(read_json(artifact)["worktree"])
        notes = checkout / "notes.txt"
        notes.write_text("keep")
        reported = "diagnostic only"
        write_json(artifact, {**read_json(artifact), "worktree": reported})
        for remove in (False, True):
            item, = cleanup_run(self.repo, engine.run["id"], remove=remove)["checkouts"]
            self.assertEqual((item["status"], item["recorded_worktree"]), ("blocked", str(checkout)))
            self.assertNotIn("worktree", item)
            self.assertIn("notes.txt", item["error"])

        ledger = read_json(engine.directory / "state.json")
        write_json(engine.directory / "state.json", {**ledger, "verification_checkouts": []})
        for remove in (False, True):
            item, = cleanup_run(self.repo, engine.run["id"], remove=remove)["checkouts"]
            self.assertEqual((item["status"], item["recorded_worktree"]), ("blocked", reported))
            self.assertIn("没有创建登记", item["error"])
        self.assertEqual(notes.read_text(), "keep")
        self.assertFalse(artifact.with_name("cleanup.json").exists())

    def test_pending_without_lock_retries_but_nonregular_lock_preserves_intent(self):
        backend = ScriptedBackend()
        engine = self.stop_at_verdict(backend)
        artifact, = self.artifacts(engine)
        receipt = artifact.with_name("cleanup.json")
        lock = artifact.with_name("remove.lock")
        checkout = Path(read_json(artifact)["worktree"])
        write_json(receipt, {**self.removed_receipt(engine, artifact), "status": "pending"})
        before = receipt.read_bytes()
        for form in ("symlink", "fifo"):
            with self.subTest(form=form), nonregular(self, lock, form):
                for remove in (False, True):
                    item, = cleanup_run(self.repo, engine.run["id"], remove=remove)["checkouts"]
                    self.assertEqual(item["status"], "blocked")
                    self.assertIn("remove.lock", item["error"])
                self.assertEqual(receipt.read_bytes(), before)
                self.assertTrue(checkout.is_dir())
        self.assertFalse(lock.exists())
        item, = cleanup_run(self.repo, engine.run["id"])["checkouts"]
        self.assertEqual(item["status"], "eligible")
        self.assertFalse(lock.exists())
        item, = cleanup_run(self.repo, engine.run["id"], remove=True)["checkouts"]
        self.assertEqual(item["status"], "removed")
        self.assertFalse(checkout.exists())


class RemoveIgnoredTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.engine_instance = self.engine()
        self.run = self.engine_instance.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.worktree = self.engine_instance.worktree
        self.receipt = self.engine_instance.directory / "worktree-cleanup.json"
        self.cache = self.worktree / "__pycache__"
        self.assertTrue(self.cache.is_dir())  # left by the task verification command

    def cli(self, *extra):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["--repo", str(self.repo), "cleanup-run", self.run["id"], *extra])
        return code, json.loads(stdout.getvalue())["worktree"]

    def test_four_invocations_on_a_worktree_with_only_ignored_files(self):
        code, item = self.cli()
        self.assertEqual((code, item["status"]), (1, "blocked"))
        self.assertIn("--remove-ignored", item["error"])
        self.assertIn("__pycache__/", item["error"])
        code, item = self.cli("--remove-ignored")
        self.assertEqual((code, item["status"]), (0, "eligible"))
        self.assertEqual(item["ignored_paths"]["entries"], ["__pycache__/"])
        self.assertEqual(item["ignored_paths"]["coverage"], "paths_only_not_contents")
        self.assertFalse(self.receipt.exists())
        code, item = self.cli("--remove")
        self.assertEqual((code, item["status"]), (1, "blocked"))
        self.assertTrue(self.cache.is_dir())
        self.assertFalse(self.receipt.exists())
        code, item = self.cli("--remove", "--remove-ignored")
        self.assertEqual((code, item["status"]), (0, "removed"))
        self.assertFalse(self.worktree.exists())
        saved = read_json(self.receipt)
        self.assertEqual((saved["status"], saved["remove_ignored"]), ("removed", True))
        self.assertEqual(saved["ignored_paths"]["entries"], ["__pycache__/"])

    def test_unignored_untracked_file_blocks_even_with_the_option(self):
        (self.worktree / "notes.txt").write_text("keep")
        for extra in ((), ("--remove-ignored",), ("--remove",), ("--remove", "--remove-ignored")):
            with self.subTest(extra=extra):
                code, item = self.cli(*extra)
                self.assertEqual((code, item["status"]), (1, "blocked"))
                self.assertIn("notes.txt", item["error"])
        self.assertEqual((self.worktree / "notes.txt").read_text(), "keep")
        self.assertTrue(self.cache.is_dir())
        self.assertFalse(self.receipt.exists())

    def test_ignored_file_appearing_after_the_intent_without_option_is_preserved(self):
        shutil.rmtree(self.cache)
        from harness import cleanup as module
        original = module.write_json

        def racing(path, value):
            original(path, value)
            if path == self.receipt and value["status"] == "pending":
                self.cache.mkdir()
                (self.cache / "late.pyc").write_text("late")
        with patch("harness.cleanup.write_json", side_effect=racing):
            code, item = self.cli("--remove")
        self.assertEqual((code, item["status"]), (1, "blocked"))
        self.assertTrue((self.cache / "late.pyc").exists())
        self.assertEqual(read_json(self.receipt)["status"], "pending")

    def test_rerun_after_interruption_follows_the_current_option(self):
        original = git.git

        def before_removal(repo, *args, **kwargs):
            if args[:2] == ("worktree", "remove"):
                raise KeyboardInterrupt()
            return original(repo, *args, **kwargs)
        with patch("harness.cleanup.git.git", side_effect=before_removal), self.assertRaises(KeyboardInterrupt):
            cleanup_run(self.repo, self.run["id"], remove=True, remove_ignored=True)
        self.assertEqual(read_json(self.receipt)["remove_ignored"], True)
        # A saved permission does not carry over to a command without the option.
        code, item = self.cli("--remove")
        self.assertEqual((code, item["status"]), (1, "blocked"))
        self.assertTrue(self.cache.is_dir())
        self.assertEqual(read_json(self.receipt)["status"], "pending")

        def after_removal(repo, *args, **kwargs):
            value = original(repo, *args, **kwargs)
            if args[:2] == ("worktree", "remove"):
                raise KeyboardInterrupt()
            return value
        with patch("harness.cleanup.git.git", side_effect=after_removal), self.assertRaises(KeyboardInterrupt):
            cleanup_run(self.repo, self.run["id"], remove=True, remove_ignored=True)
        self.assertFalse(self.worktree.exists())
        # Completing the earlier deletion keeps what that command recorded.
        code, item = self.cli("--remove")
        self.assertEqual((code, item["status"]), (0, "removed"))
        saved = read_json(self.receipt)
        self.assertEqual((saved["status"], saved["remove_ignored"]), ("removed", True))
        self.assertEqual(saved["ignored_paths"]["entries"], ["__pycache__/"])

    def test_core_excludes_file_counts_as_ignored(self):
        excludes = Path(self.temp.name) / "global-excludes"
        excludes.write_text("scratch.log\n")
        git.git(self.repo, "config", "core.excludesFile", str(excludes))
        (self.worktree / "scratch.log").write_text("x")
        code, item = self.cli("--remove-ignored")
        self.assertEqual((code, item["status"]), (0, "eligible"))
        self.assertEqual(item["ignored_paths"]["entries"], ["__pycache__/", "scratch.log"])
        code, item = self.cli("--remove", "--remove-ignored")
        self.assertEqual((code, item["status"]), (0, "removed"))


class InstantListingTests(RepoCase):
    def test_review_packets_carry_a_bounded_listing_and_failure_is_not_empty(self):
        from harness import run_context
        backend = ScriptedBackend()
        engine = self.engine(backend)
        calls = {"n": 0}
        original = git.ignored_paths

        def flaky(repo, *, binding=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise HarnessError("simulated listing failure")
            return original(repo, binding=binding)
        with patch("harness.run_context.git.ignored_paths", side_effect=flaky):
            result = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.assertEqual(result["status"], "completed")
        reviews = [c["packet"]["ignored_paths"] for c in backend.calls if c["role"] == "reviewer"]
        self.assertEqual(len(reviews), 2)
        task, final = reviews
        self.assertEqual(task["entries"], ["__pycache__/"])
        self.assertEqual((task["entry_count"], task["omitted_count"]), (1, 0))
        self.assertIsNone(final["entries"])
        self.assertIsNone(final["entry_count"])
        self.assertIn("simulated", final["error"])
        for key in ("ignored_baseline", "ignored_diagnostics"):
            self.assertNotIn(key, result)
        self.assertFalse(list(engine.directory.rglob("ignored-*.json")))
        self.assertNotIn("ignored_paths_changed", (engine.directory / "events.jsonl").read_text())

    def test_bounds_and_folding(self):
        names = [f"build/{i:03d}/" for i in range(150)]
        listing = git.bounded_paths(names)
        self.assertEqual((len(listing["entries"]), listing["entry_count"], listing["omitted_count"]), (100, 150, 50))
        long = ["d" * 1000 + f"{i}" for i in range(20)]
        listing = git.bounded_paths(long)
        self.assertLessEqual(len(json.dumps(listing["entries"]).encode()), 8192)
        self.assertEqual(listing["entries"], long[:len(listing["entries"])])
        self.assertEqual(listing["omitted_count"], 20 - len(listing["entries"]))
