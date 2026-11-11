import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

from harness import git, observe
from harness.cleanup import cleanup_run
from harness.cli import main
from harness.util import HarnessError, lock, read_json, write_json
from test_harness import RepoCase, ROOT


class VerificationCleanupTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.config.data["verification"].update(fresh_checkout=True,
            commands=[[sys.executable, "-c", "from pathlib import Path; assert Path('hello.py').is_file()"]])
        self.engine_instance = self.engine()
        self.run = self.engine_instance.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.directory = self.engine_instance.directory
        self.artifact = next(self.directory.glob("verification/final/*/verification-*/checkout.json"))
        self.environment = read_json(self.artifact)
        self.checkout = Path(self.environment["worktree"])
        self.roots = [self.checkout.parents[1]]
        self.addCleanup(self.cleanup_roots)

    def cleanup_roots(self):
        for root in self.roots:
            shutil.rmtree(root, ignore_errors=True)

    def cleanup(self, remove=False, **kwargs):
        return cleanup_run(self.repo, self.run["id"], remove=remove, **kwargs)

    def git_calls(self, interrupt_after_remove=None):
        """Record exact worktree removals; optionally interrupt after the Nth one."""
        removed, original = [], git.git

        def recording(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[:2] == ("worktree", "remove"):
                removed.append(args[2])
                if len(removed) == interrupt_after_remove:
                    raise KeyboardInterrupt()
            return result
        return removed, patch("harness.cleanup.git.git", side_effect=recording)

    def additional_checkout(self):
        root = Path(tempfile.mkdtemp(prefix="harness-verify-")).resolve()
        self.roots.append(root)
        checkout = root / ".harness" / "checkout"
        git.git(self.repo, "worktree", "add", "--detach", str(checkout), self.environment["head"])
        environment = {**self.environment, "worktree": str(checkout),
                       "binding": git.capture_worktree_binding(checkout, None)}
        final = self.run["final_history"][-1]
        folder = self.artifact.parent.parent / f"verification-{len(final['verification_rounds']) + 1}"
        artifact = folder / "checkout.json"
        write_json(artifact, environment)
        final["verification_rounds"].append({"artifact_dir": str(folder), "status": "completed"})
        write_json(self.directory / "state.json", self.run)
        return checkout, artifact

    def statuses(self, result):
        return [item["status"] for item in result["checkouts"]], result["worktree"]["status"]

    def test_one_removal_clears_checkout_and_worktree_and_keeps_branch_and_evidence(self):
        worktree = self.engine_instance.worktree
        evidence = self.artifact.read_bytes()
        ledger = (self.directory / "state.json").read_bytes()
        results = (self.artifact.parent / "results.json").read_bytes()
        self.assertEqual(self.statuses(self.cleanup()), (["eligible"], "eligible"))
        self.assertTrue(self.checkout.is_dir())
        self.assertTrue(worktree.is_dir())
        self.assertFalse(self.artifact.with_name("cleanup.json").exists())
        self.assertFalse((self.directory / "worktree-cleanup.json").exists())
        self.assertEqual(self.statuses(self.cleanup(remove=True)), (["removed"], "removed"))
        self.assertFalse(self.checkout.parents[1].exists())
        self.assertFalse(worktree.exists())
        self.assertEqual(git.git(self.repo, "rev-parse", "refs/heads/" + self.run["branch"]).strip(),
                         self.run["expected_head"])
        self.assertEqual(self.artifact.read_bytes(), evidence)
        self.assertEqual((self.artifact.parent / "results.json").read_bytes(), results)
        self.assertEqual((self.directory / "state.json").read_bytes(), ledger)
        saved = read_json(self.directory / "state.json")
        self.assertEqual(saved["status"], "completed")
        self.assertFalse({"worktree_removed", "pending_worktree_removal"} & set(saved))
        self.assertEqual(self.statuses(self.cleanup()), (["absent"], "absent"))
        self.assertEqual(self.statuses(self.cleanup(remove=True)), (["removed"], "removed"))
        self.assertEqual(read_json(self.artifact.with_name("cleanup.json"))["status"], "removed")
        self.assertEqual(read_json(self.directory / "worktree-cleanup.json")["status"], "removed")

    def test_nonterminal_run_removes_only_checkouts_and_reports_the_retained_worktree(self):
        worktree = self.engine_instance.worktree
        self.run["status"] = "stopped"
        write_json(self.directory / "state.json", self.run)
        for remove, checkouts in ((True, ["removed"]), (False, ["absent"])):
            stdout = io.StringIO()
            args = ["--repo", str(self.repo), "cleanup-run", self.run["id"]] + (["--remove"] if remove else [])
            with contextlib.redirect_stdout(stdout):
                # A policy-retained worktree alone is not a refusal.
                self.assertEqual(main(args), 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["run_status"], "stopped")
            self.assertEqual(self.statuses(result), (checkouts, "retained"))
            self.assertEqual(result["worktree"]["reason_kind"], "finish_or_abandon_first")
            self.assertNotIn("error", result["worktree"])
        self.assertFalse(self.checkout.exists())
        self.assertEqual(git.head(worktree), self.run["expected_head"])
        self.assertFalse((self.directory / "worktree-cleanup.json").exists())
        self.assertEqual(read_json(self.directory / "state.json")["status"], "stopped")
        # A blocked checkout still makes the same run exit 1 while the worktree stays retained.
        other, _ = self.additional_checkout()
        (other / "scratch").write_text("keep")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["--repo", str(self.repo), "cleanup-run", self.run["id"], "--remove"]), 1)
        self.assertEqual(self.statuses(json.loads(stdout.getvalue())), (["removed", "blocked"], "retained"))

    def cli(self, *extra):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["--repo", str(self.repo), "cleanup-run", self.run["id"], *extra])
        return code, self.statuses(json.loads(stdout.getvalue()))

    def test_closing_sequence_survives_branch_deletion_and_movement(self):
        # README order: clean first, then the operator handles the branch.
        self.assertEqual(self.cli("--remove"), (0, (["removed"], "removed")))
        git.git(self.repo, "branch", "-D", self.run["branch"])
        self.assertEqual(self.cli(), (0, (["absent"], "absent")))
        self.assertEqual(self.cli("--remove"), (0, (["removed"], "removed")))
        # A branch recreated elsewhere (moved) does not reopen a finished removal.
        git.git(self.repo, "branch", self.run["branch"], self.run["base_commit"])
        self.assertEqual(self.cli("--remove"), (0, (["removed"], "removed")))
        # Nor does a later audited exit confirmation that rewrites the ledger.
        self.run = read_json(self.directory / "state.json")
        self.run["dispatches"].append({"id": "late", "process_state": "starting"})
        write_json(self.directory / "state.json", self.run)
        result = self.cleanup(remove=True, confirm_exited=["late"])
        self.assertEqual(self.statuses(result), (["removed"], "removed"))
        self.assertTrue(read_json(self.directory / "state.json")["dispatches"][-1]["exit_confirmed"])
        self.assertEqual(read_json(self.directory / "worktree-cleanup.json")["status"], "removed")

    def test_closing_sequence_with_a_remaining_checkout_after_branch_deletion(self):
        scratch = self.checkout / "scratch.txt"
        scratch.write_text("inspect first")
        self.assertEqual(self.cli("--remove"), (1, (["blocked"], "removed")))
        git.git(self.repo, "branch", "-D", self.run["branch"])
        self.assertEqual(self.cli(), (1, (["blocked"], "absent")))
        scratch.unlink()
        self.assertEqual(self.cli(), (0, (["eligible"], "absent")))
        self.assertEqual(self.cli("--remove"), (0, (["removed"], "removed")))
        self.assertFalse(self.checkout.exists())

    def test_old_ledger_removal_key_survives_branch_deletion(self):
        worktree = self.engine_instance.worktree
        state = self.directory / "state.json"
        git.git(self.repo, "worktree", "remove", str(worktree))
        git.git(self.repo, "branch", "-D", self.run["branch"])
        write_json(state, {**read_json(state), "status": "abandoned", "schema_version": 12, "worktree_removed": True})
        self.assertEqual(self.cli(), (0, (["eligible"], "absent")))
        self.assertEqual(self.cli("--remove"), (0, (["removed"], "removed")))
        self.assertEqual(self.cli(), (0, (["absent"], "absent")))

    def test_interrupted_removals_resume_without_repeating_deletion_or_losing_receipts(self):
        worktree = self.engine_instance.worktree
        receipt = self.directory / "worktree-cleanup.json"
        removed, recording = self.git_calls(interrupt_after_remove=1)
        with recording, self.assertRaises(KeyboardInterrupt):
            self.cleanup(remove=True)
        self.assertEqual(removed, [str(self.checkout)])
        self.assertEqual(read_json(self.artifact.with_name("cleanup.json"))["status"], "pending")
        self.assertFalse(receipt.exists())
        self.assertTrue(worktree.is_dir())
        removed, recording = self.git_calls(interrupt_after_remove=1)
        with recording, self.assertRaises(KeyboardInterrupt):
            self.cleanup(remove=True)
        # The checkout's saved intent completes without a second Git removal.
        self.assertEqual(removed, [str(worktree)])
        self.assertEqual(read_json(self.artifact.with_name("cleanup.json"))["status"], "removed")
        self.assertEqual(read_json(receipt)["status"], "pending")
        removed, recording = self.git_calls()
        with recording:
            result = self.cleanup(remove=True)
        self.assertEqual(removed, [])
        self.assertEqual(self.statuses(result), (["removed"], "removed"))
        self.assertEqual(read_json(receipt)["status"], "removed")
        self.assertFalse(self.checkout.exists() or worktree.exists())
        self.assertEqual(git.git(self.repo, "rev-parse", "refs/heads/" + self.run["branch"]).strip(),
                         self.run["expected_head"])

    def test_ledgers_from_the_removed_abandon_option_clean_their_remaining_checkout(self):
        worktree = self.engine_instance.worktree
        state = self.directory / "state.json"
        # What abandon --remove-worktree left: no worktree, no cleanup receipt.
        git.git(self.repo, "worktree", "remove", str(worktree))
        base = read_json(state)
        for version, key in ((16, "worktree_removed"), (12, "worktree_removed"), (16, "pending_worktree_removal")):
            with self.subTest(version=version, key=key):
                write_json(state, {**base, "status": "abandoned", "schema_version": version, key: True})
                self.assertEqual(self.statuses(self.cleanup()), (["eligible"], "absent"))
                # Observation keeps reading the old completion key; there is no receipt yet.
                self.assertIs(observe.run_detail(self.repo / ".harness", self.run["id"])["run"]["worktree_removed"],
                              key == "worktree_removed")
        write_json(state, {**base, "status": "abandoned"})
        missing = self.cleanup()["worktree"]
        self.assertEqual(missing["status"], "blocked")
        self.assertIn("清理意图", missing["error"])
        write_json(state, {**base, "status": "abandoned", "schema_version": 12, "worktree_removed": True})
        ledger = state.read_bytes()
        self.assertEqual(self.statuses(self.cleanup(remove=True)), (["removed"], "removed"))
        self.assertFalse(self.checkout.exists())
        self.assertEqual(state.read_bytes(), ledger)
        self.assertEqual(read_json(self.directory / "worktree-cleanup.json")["status"], "removed")
        self.assertEqual(self.statuses(self.cleanup()), (["absent"], "absent"))
        self.assertEqual(git.git(self.repo, "rev-parse", "refs/heads/" + self.run["branch"]).strip(),
                         self.run["expected_head"])

    def test_refuses_modified_untracked_ignored_and_hidden_index_files(self):
        variants = ("tracked", "untracked", "ignored", "assume-unchanged", "skip-worktree")
        for variant in variants:
            with self.subTest(variant=variant):
                if variant == "tracked":
                    (self.checkout / "hello.py").write_text("unsaved")
                elif variant in ("untracked", "ignored"):
                    name = "scratch" if variant == "untracked" else "__pycache__/scratch"
                    path = self.checkout / name
                    path.parent.mkdir(exist_ok=True)
                    path.write_text("unsaved")
                else:
                    git.git(self.checkout, "update-index", "--" + variant, "hello.py")
                item = self.cleanup(remove=True)["checkouts"][0]
                self.assertEqual(item["status"], "blocked")
                self.assertTrue(self.checkout.is_dir())
                self.assertFalse(self.artifact.with_name("cleanup.json").exists())
                if variant == "tracked":
                    git.git(self.checkout, "checkout", "--", "hello.py")
                elif variant in ("untracked", "ignored"):
                    self.assertEqual(path.read_text(), "unsaved")
                    path.unlink()
                    if variant == "ignored":
                        path.parent.rmdir()
                else:
                    git.git(self.checkout, "update-index", "--no-" + variant, "hello.py")

    def test_run_lock_and_live_or_unknown_process_refuse_all_removal(self):
        with lock(self.directory / "run.lock"), self.assertRaisesRegex(HarnessError, "运行锁"):
            self.cleanup(remove=True)
        self.run["dispatches"].append({"id": "old", "role": "verification", "pgid": 12345,
                                       "status": "running", "exit_confirmed": False})
        write_json(self.directory / "state.json", self.run)
        with patch("harness.cleanup.group_alive", return_value=True), self.assertRaisesRegex(HarnessError, "进程组仍存在"):
            self.cleanup(remove=True)
        self.run["dispatches"][-1]["pgid"] = None
        write_json(self.directory / "state.json", self.run)
        with self.assertRaisesRegex(HarnessError, "无法确认"):
            self.cleanup(remove=True)
        self.assertTrue(self.checkout.exists())
        self.assertFalse(self.artifact.with_name("cleanup.json").exists())

    def test_changed_binding_and_run_worktree_substitution_are_preserved(self):
        # A non-terminal run keeps its worktree out of reach of the worktree
        # resource, so only the checkout gates stand between it and removal.
        self.run["status"] = "stopped"
        write_json(self.directory / "state.json", self.run)
        gitfile = self.checkout / ".git"
        original = gitfile.read_bytes()
        gitfile.write_bytes(original + b"\n")
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "blocked")
        gitfile.write_bytes(original)
        write_json(self.artifact, {**self.environment, "worktree": str(self.engine_instance.worktree)})
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "blocked")
        self.assertTrue(self.engine_instance.worktree.exists())
        self.assertTrue(self.checkout.exists())
        write_json(self.artifact, {**self.environment, "head": "0" * 40})
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "blocked")
        write_json(self.artifact, self.environment)
        git.git(self.checkout, "checkout", "--detach", self.run["base_commit"])
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "blocked")
        self.assertTrue(self.checkout.exists())

    def test_missing_checkout_unregisters_only_exact_saved_binding(self):
        other, other_artifact = self.additional_checkout()
        shutil.rmtree(self.checkout)
        shutil.rmtree(other)
        # The unreferenced stale checkout must never be touched by a broad prune.
        self.run["final_history"][-1]["verification_rounds"].pop()
        write_json(self.directory / "state.json", self.run)
        other_artifact.unlink()
        self.assertEqual(self.cleanup()["checkouts"][0]["status"], "stale_registration")
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "removed")
        registrations = git.git(self.repo, "worktree", "list", "--porcelain")
        self.assertNotIn(str(self.checkout), registrations)
        self.assertIn(str(other), registrations)

    def test_missing_checkout_with_tampered_backlink_keeps_registration(self):
        self.run["status"] = "stopped"
        write_json(self.directory / "state.json", self.run)
        shutil.rmtree(self.checkout)
        backlink = Path(self.environment["binding"]["git_dir"]) / "gitdir"
        backlink.write_text(str(self.engine_instance.worktree / ".git") + "\n")
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "blocked")
        self.assertTrue(backlink.parent.exists())
        self.assertTrue(self.engine_instance.worktree.exists())

    def test_completed_cleanup_ignores_reused_git_administrative_name(self):
        self.cleanup(remove=True)
        other, artifact = self.additional_checkout()
        self.assertEqual(read_json(artifact)["binding"]["git_dir"], self.environment["binding"]["git_dir"])
        result = self.cleanup()
        self.assertEqual([item["status"] for item in result["checkouts"]], ["absent", "eligible"])
        self.assertTrue(other.is_dir())

    def test_interruption_after_git_removal_recovers_pending_receipt(self):
        original_git = git.git

        def interrupted(repo, *args, **kwargs):
            result = original_git(repo, *args, **kwargs)
            if args[:2] == ("worktree", "remove"):
                raise KeyboardInterrupt()
            return result

        with patch("harness.cleanup.git.git", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.cleanup(remove=True)
        receipt = read_json(self.artifact.with_name("cleanup.json"))
        self.assertEqual(receipt["status"], "pending")
        self.assertFalse(self.checkout.exists())
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "removed")
        self.assertFalse(self.checkout.parents[1].exists())

    def test_partial_cleanup_preserves_dirty_checkout_and_unknown_sibling(self):
        other, _ = self.additional_checkout()
        (self.checkout / "scratch").write_text("keep")
        sibling = other.parents[1] / "operator-notes"
        sibling.write_text("keep")
        statuses = [item["status"] for item in self.cleanup(remove=True)["checkouts"]]
        self.assertEqual(statuses, ["blocked", "removed"])
        self.assertEqual((self.checkout / "scratch").read_text(), "keep")
        self.assertFalse(other.exists())
        self.assertEqual(sibling.read_text(), "keep")

    def test_cli_lists_blocking_checkout_path_and_still_removes_the_clean_worktree(self):
        (self.checkout / "scratch-notes.txt").write_text("keep")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--repo", str(self.repo), "--config", str(self.repo / "absent.toml"),
                         "cleanup-run", self.run["id"], "--remove"])
        self.assertEqual(code, 1)
        result = json.loads(stdout.getvalue())
        self.assertEqual(self.statuses(result), (["blocked"], "removed"))
        self.assertIn("scratch-notes.txt", result["checkouts"][0]["error"])
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual((self.checkout / "scratch-notes.txt").read_text(), "keep")
        self.assertFalse(self.artifact.with_name("cleanup.json").exists())
        self.assertFalse(self.engine_instance.worktree.exists())

    def add_unknown_dispatch(self, ident="unknown"):
        entry = {"id": ident, "role": "verification", "status": "running",
                 "pgid": None, "exit_confirmed": False}
        self.run["dispatches"].append(entry)
        write_json(self.directory / "state.json", self.run)
        return entry

    def confirmation_events(self):
        return [entry for line in (self.directory / "events.jsonl").read_text().splitlines()
                if (entry := json.loads(line))["event"] == "dispatch_exit_confirmed_by_operator"]

    def test_terminal_confirmation_supports_all_ledger_versions_and_preserves_status(self):
        evidence = self.artifact.read_bytes()
        for version in (7, 8, 9, 10):
            for status in ("completed", "abandoned"):
                with self.subTest(version=version, status=status):
                    self.run.update(schema_version=version, status=status)
                    ident = f"unknown-{version}-{status}"
                    self.add_unknown_dispatch(ident)
                    with self.assertRaisesRegex(HarnessError, "无法确认"):
                        self.cleanup()
                    result = cleanup_run(self.repo, self.run["id"], confirm_exited=[ident, ident])
                    self.assertEqual(result["checkouts"][0]["status"], "eligible")
                    self.run = read_json(self.directory / "state.json")
                    self.assertEqual(self.run["status"], status)
                    self.assertEqual(self.run["schema_version"], version)
                    self.assertTrue(self.run["dispatches"][-1]["exit_confirmed"])
                    self.assertIsNone(self.confirmation_events()[-1]["observed"]["group_alive"])
        self.assertEqual(len(self.confirmation_events()), 8)
        self.assertTrue(self.checkout.exists())
        self.assertEqual(self.artifact.read_bytes(), evidence)

    def test_confirmation_checks_lock_and_entire_id_batch_before_audit_or_mutation(self):
        entry = self.add_unknown_dispatch()
        state = (self.directory / "state.json").read_bytes()
        events = (self.directory / "events.jsonl").read_bytes()
        with lock(self.directory / "run.lock"), self.assertRaisesRegex(HarnessError, "运行锁"):
            cleanup_run(self.repo, self.run["id"], confirm_exited=[entry["id"]])
        with patch("harness.cleanup.group_alive") as observe, self.assertRaisesRegex(HarnessError, "unknown-id"):
            cleanup_run(self.repo, self.run["id"], confirm_exited=[entry["id"], "unknown-id"])
        observe.assert_not_called()
        self.assertEqual((self.directory / "state.json").read_bytes(), state)
        self.assertEqual((self.directory / "events.jsonl").read_bytes(), events)

    def test_confirmation_never_implicitly_confirms_other_dispatches_or_sends_signals(self):
        selected = self.add_unknown_dispatch("selected")
        selected["pgid"] = 2_000_000_000
        other = self.add_unknown_dispatch("other")
        with patch("harness.cleanup.group_alive", return_value=True), \
                patch("harness.util.os.killpg", side_effect=AssertionError("must not signal")), \
                self.assertRaisesRegex(HarnessError, "other"):
            cleanup_run(self.repo, self.run["id"], remove=True, confirm_exited=[selected["id"]])
        saved = read_json(self.directory / "state.json")
        self.assertTrue(saved["dispatches"][-2]["exit_confirmed"])
        self.assertEqual(saved["dispatches"][-1], other)
        self.assertTrue(self.confirmation_events()[-1]["observed"]["group_alive"])
        self.assertTrue(self.checkout.exists())
        self.assertFalse(self.artifact.with_name("cleanup.json").exists())

    def test_confirmation_audit_is_persisted_before_state_and_failure_cannot_grant_exit(self):
        selected = self.add_unknown_dispatch()
        before = (self.directory / "state.json").read_bytes()
        with patch("harness.cleanup.append_event", side_effect=OSError("audit unavailable")), \
                self.assertRaisesRegex(OSError, "audit unavailable"):
            cleanup_run(self.repo, self.run["id"], confirm_exited=[selected["id"]])
        self.assertEqual((self.directory / "state.json").read_bytes(), before)
        self.assertEqual(self.confirmation_events(), [])
        with patch("harness.run_store.write_json", side_effect=OSError("state unavailable")), \
                self.assertRaisesRegex(OSError, "state unavailable"):
            cleanup_run(self.repo, self.run["id"], confirm_exited=[selected["id"]])
        self.assertEqual((self.directory / "state.json").read_bytes(), before)
        self.assertEqual(self.confirmation_events()[0]["dispatch_id"], selected["id"])
        self.assertTrue(self.checkout.exists())

    def test_cli_terminal_confirmation_needs_no_configuration_and_keeps_file_gates(self):
        selected = self.add_unknown_dispatch()
        unsaved = self.checkout / "__pycache__" / "operator-data"
        unsaved.parent.mkdir()
        unsaved.write_text("keep")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--repo", str(self.repo), "--config", str(self.repo / "absent.toml"),
                         "cleanup-run", self.run["id"], "--remove",
                         "--confirm-exited", selected["id"]])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["checkouts"][0]["status"], "blocked")
        self.assertEqual(unsaved.read_text(), "keep")
        saved = read_json(self.directory / "state.json")
        self.assertTrue(saved["dispatches"][-1]["exit_confirmed"])
        self.assertEqual(saved["status"], "completed")
