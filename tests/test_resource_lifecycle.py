"""Resource lifecycle: verified-checkout reclamation, --remove-ignored and the instant ignored listing.

Every case uses real Git in a temporary repository and simulated agents.
"""
import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.cleanup import cleanup_run
from harness.cli import main
from harness.util import HarnessError, read_json, write_json
from test_harness import RepoCase, ROOT, crash_after_save, crash_point
from test_runtime_changes import ScriptedBackend


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

    def foreign_record(self, head, owner):
        """A legitimate checkout record of another run (or round) verifying the same HEAD."""
        root = Path(tempfile.mkdtemp(prefix="harness-verify-")).resolve()
        self.roots.append(root)
        checkout = root / ".harness" / "checkout"
        git.git(self.repo, "worktree", "add", "--detach", str(checkout), head)
        return checkout, {"mode": "fresh_checkout", "owner": owner, "worktree": str(checkout), "head": head,
                          "status": "verified", "setup_commands": [],
                          "binding": git.capture_worktree_binding(checkout, None)}

    def test_verified_checkout_with_ignored_cache_is_reclaimed_after_the_verdict(self):
        # The default unittest command leaves an ignored __pycache__ in the checkout.
        backend = ScriptedBackend()
        engine = self.engine(backend)
        result = engine.start("Implement greet", self.plan())
        self.assertEqual(result["status"], "completed")
        artifact, = self.artifacts(engine)
        record = read_json(artifact)
        checkout = Path(record["worktree"])
        self.assertEqual(record["owner"], {"run_id": result["id"], "attempt": 1, "round": 1})
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
        self.assertEqual(read_json(second)["owner"]["round"], 2)
        self.assertFalse(Path(read_json(second)["worktree"]).exists())
        preview = cleanup_run(self.repo, result["id"])["checkouts"]
        self.assertEqual([i["status"] for i in preview], ["eligible", "absent"])

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
                original = git.git

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
                with crash, patch("harness.cleanup.git.git", side_effect=interrupted), \
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
                    other, _ = self.foreign_record(record["head"], {"run_id": "run_other", "attempt": 1, "round": 1})
                removals = []

                def recording(repo, *args, **kwargs):
                    if args[:2] == ("worktree", "remove"):
                        removals.append(args[2])
                    return original(repo, *args, **kwargs)
                with patch("harness.cleanup.git.git", side_effect=recording):
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
        original = git.git

        def failing(repo, *args, **kwargs):
            if args[:2] == ("worktree", "remove"):
                raise HarnessError("simulated removal failure")
            return original(repo, *args, **kwargs)
        with patch("harness.cleanup.git.git", side_effect=failing):
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
                original_git, original_event = git.git, engine.context.event

                def failing_git(repo, *args, **kwargs):
                    if remove_fails and args[:2] == ("worktree", "remove"):
                        raise HarnessError("simulated removal failure")
                    return original_git(repo, *args, **kwargs)

                def failing_event(name, **fields):
                    if name.startswith("verification_checkout_"):
                        raise OSError("event append unavailable")
                    return original_event(name, **fields)
                with patch("harness.cleanup.git.git", side_effect=failing_git), \
                        patch.object(engine.context, "event", side_effect=failing_event):
                    result = engine.start("Implement greet", self.plan())
                self.assertEqual(result["status"], "completed")
                self.assertEqual(sum(c["kind"] == "final_review" for c in backend.calls), 1)
                artifact, = self.artifacts(engine)
                self.assertEqual(read_json(artifact.with_name("cleanup.json"))["status"],
                                 "pending" if remove_fails else "removed")
                self.assertEqual(Path(read_json(artifact)["worktree"]).exists(), remove_fails)

    def test_authority_or_exit_failure_stops_instead_of_skipping_reclamation(self):
        from harness.finalization import FinalAcceptance
        backend = ScriptedBackend()
        engine = self.engine(backend)
        state = {"inside": False}
        reclaim, ensure = FinalAcceptance.reclaim_verified_checkout, engine.context.ensure_exited

        def flagged(service, record):
            state["inside"] = True
            try:
                return reclaim(service, record)
            finally:
                state["inside"] = False

        def ensure_exited():
            if state["inside"]:
                raise HarnessError("simulated unconfirmed exit")
            return ensure()
        with patch.object(FinalAcceptance, "reclaim_verified_checkout", flagged), \
                patch.object(engine.context, "ensure_exited", side_effect=ensure_exited), \
                self.assertRaisesRegex(HarnessError, "simulated unconfirmed exit"):
            engine.start("Implement greet", self.plan())
        self.assertEqual(engine.run["status"], "blocked")
        self.assertFalse(any(c["kind"] == "final_review" for c in backend.calls))
        artifact, = self.artifacts(engine)
        self.assertTrue(Path(read_json(artifact)["worktree"]).is_dir())
        self.assertFalse(artifact.with_name("cleanup.json").exists())

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

    def test_another_runs_record_at_the_same_head_is_refused_by_both_entries(self):
        backend = ScriptedBackend()
        engine = self.stop_at_verdict(backend)
        artifact, = self.artifacts(engine)
        own = Path(read_json(artifact)["worktree"])
        other, record = self.foreign_record(read_json(artifact)["head"], {"run_id": "run_other", "attempt": 1, "round": 1})
        write_json(artifact, record)
        removal = self.assert_both_kept_and_refused(engine, backend, [own, other])
        self.assertIn("不属于", removal[0]["error"])
        self.assertEqual(removal[0]["recorded_worktree"], str(other))

    def test_forged_owner_is_caught_by_the_digest_saved_with_the_verdict(self):
        backend = ScriptedBackend()
        engine = self.stop_at_verdict(backend)
        artifact, = self.artifacts(engine)
        own = Path(read_json(artifact)["worktree"])
        other, record = self.foreign_record(read_json(artifact)["head"], read_json(artifact)["owner"])
        write_json(artifact, record)
        removal = self.assert_both_kept_and_refused(engine, backend, [own, other])
        self.assertIn("摘要", removal[0]["error"])

    def test_records_swapped_between_rounds_are_refused_by_both_entries(self):
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        marker = Path(external.name) / "ready"
        self.config.data["verification"]["setup_commands"] = [[sys.executable, "-c",
            f"from pathlib import Path; assert Path({str(marker)!r}).exists()"]]
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
        removal = self.assert_both_kept_and_refused(engine, backend, dirs)
        self.assertEqual(len(removal), 2)

    def test_failed_round_is_retained_and_cleanup_removes_its_ignored_files_without_option(self):
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        marker = Path(external.name) / "ready"
        self.config.data["verification"]["setup_commands"] = [[sys.executable, "-c",
            "from pathlib import Path; Path('__pycache__').mkdir(exist_ok=True); "
            "Path('__pycache__/setup.log').write_text('x'); "
            f"assert Path({str(marker)!r}).exists()"]]
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

    def test_blocked_checkout_reports_its_unverified_recorded_directory(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with patch("harness.finalization.FinalAcceptance.reclaim_verified_checkout"):
            result = engine.start("Implement greet", self.plan())
        artifact, = self.artifacts(engine)
        checkout = Path(read_json(artifact)["worktree"])
        (checkout / "notes.txt").write_text("keep")
        item, = cleanup_run(self.repo, result["id"], remove=True)["checkouts"]
        self.assertEqual((item["status"], item["recorded_worktree"]), ("blocked", str(checkout)))
        self.assertNotIn("worktree", item)
        self.assertIn("notes.txt", item["error"])
        self.assertTrue((checkout / "notes.txt").exists())


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
