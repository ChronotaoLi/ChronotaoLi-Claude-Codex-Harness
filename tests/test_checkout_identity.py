from copy import deepcopy
import errno
import json
import os
from pathlib import Path
import shutil
import shlex
import signal
import sys
import time
import unittest
from unittest.mock import patch

from git_isolation import fixture_git
from harness import checkout_identity, cleanup, git, stops
from harness.util import HarnessError, read_json, write_json
from harness.verification import Verification, run_process
from test_harness import RepoCase, ROOT, after_save, crash_after_save, crash_point, refused_by_cli
from test_readonly_cleanup import tree_bytes
from test_runtime_changes import ScriptedBackend
from test_state_files import FakeOs


class CheckoutRootTests(RepoCase):
    """D7-T2a: detection and removal of registered checkout identities."""

    def setUp(self):
        super().setUp()
        self.config.data["verification"].update(fresh_checkout=True, commands=[
            {"cwd": ".", "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('hello.py').is_file()"]},
        ])
        self.backend = ScriptedBackend()
        self.runtime = self.engine(self.backend)

    def start(self):
        return self.runtime.start("Implement greet", read_json(ROOT / "examples/plan.json"))

    def capture_checkout(self):
        artifact, = self.runtime.directory.glob("verification/final/*/verification-*/checkout.json")
        self.entry, = read_json(self.runtime.directory / "state.json")["verification_checkouts"]
        self.checkout = Path(self.entry["worktree"])
        self.root = Path(self.entry["root"])
        self.admin = Path(self.entry["binding"]["git_dir"])
        self.receipt = artifact.with_name("cleanup.json")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def retain_checkout(self):
        with patch("harness.finalization.FinalAcceptance.reclaim_verified_checkout"):
            self.start()
        self.capture_checkout()

    def cleanup_checkout(self, *, remove=False):
        item, = cleanup.cleanup_run(self.repo, self.runtime.run["id"], remove=remove)["checkouts"]
        return item

    def assert_registered(self, registered=True):
        records = fixture_git(self.repo, "worktree", "list", "--porcelain", "-z").split("\0")
        self.assertEqual(f"worktree {self.checkout}" in records, registered)
        self.assertEqual(self.admin.is_dir(), registered)

    def test_missing_root_allows_removal_of_its_exact_registration(self):
        self.retain_checkout()
        shutil.rmtree(self.root)
        self.assert_registered()
        self.assertEqual(self.cleanup_checkout()["status"], "stale_registration")
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.cleanup_checkout(remove=True)["status"], "removed")
        self.assert_registered(False)

    def test_existing_root_allows_removal_of_a_stale_registration(self):
        self.retain_checkout()
        shutil.rmtree(self.checkout)
        self.assertEqual(self.cleanup_checkout()["status"], "stale_registration")
        self.assert_registered()
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.cleanup_checkout(remove=True)["status"], "removed")
        self.assert_registered(False)
        self.assertEqual(read_json(self.receipt)["status"], "removed")

    def test_root_is_checked_again_after_the_removal_intent(self):
        self.retain_checkout()
        shutil.rmtree(self.checkout)
        write_json = cleanup.write_json

        moved = self.outside / "old-root"

        def replace_root_after_intent(path, data):
            write_json(path, data)
            if path == self.receipt and data["status"] == "pending":
                self.root.rename(moved)
                self.root.mkdir()

        with patch.object(cleanup, "write_json", side_effect=replace_root_after_intent):
            self.assertEqual(self.cleanup_checkout(remove=True)["status"], "blocked")
        self.assertTrue(self.root.exists())
        self.assert_registered()
        self.assertEqual(read_json(self.receipt)["status"], "pending")
        self.assert_blocked()

    def test_root_lstat_io_failure_blocks_removal(self):
        self.retain_checkout()
        shutil.rmtree(self.checkout)

        def fail_root_probe(lstat, path):
            if path == self.root:
                raise OSError(errno.EIO, os.strerror(errno.EIO), path)
            return lstat(path)

        with patch.object(checkout_identity, "os", FakeOs(lstat=fail_root_probe)):
            item = self.cleanup_checkout(remove=True)
        self.assertEqual(item["status"], "blocked")
        self.assertIn(os.strerror(errno.EIO), item["error"])
        self.assert_registered()
        self.assertFalse(self.receipt.exists())

    def assert_blocked(self):
        state = self.runtime.context.state
        before = tree_bytes(state)
        item = self.cleanup_checkout()
        self.assertEqual(item["status"], "blocked", item)
        self.assertEqual(tree_bytes(state), before)
        item = self.cleanup_checkout(remove=True)
        self.assertEqual(item["status"], "blocked", item)
        self.assertEqual(item["recorded_worktree"], str(self.checkout))
        return item

    def test_replacement_checkout_is_kept_by_runtime_and_explicit_cleanup(self):
        def verdict_saved(run):
            return any("checkout_digest" in row for attempt in run["final_history"]
                       for row in attempt["verification_rounds"])

        def replace_checkout():
            self.capture_checkout()
            moved = self.root / "old-checkout"
            self.checkout.rename(moved)
            shutil.copytree(moved, self.checkout)
            cache = self.checkout / "__pycache__"
            cache.mkdir(exist_ok=True)
            (cache / "keep").write_text("replacement")
            git.check_worktree_binding(self.checkout, self.entry["binding"])

        hook, _ = after_save(self.runtime, verdict_saved, replace_checkout)
        with hook:
            result = self.start()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(call["kind"] == "final_review" for call in self.backend.calls), 1)
        self.assertEqual(result["final_history"][-1]["verification_rounds"][0]["status"], "completed")
        self.assert_registered()
        self.assert_blocked()
        self.assertEqual((self.checkout / "__pycache__" / "keep").read_text(), "replacement")
        self.assertFalse(self.receipt.exists())
        events = [json.loads(line) for line in (self.runtime.directory / "events.jsonl").read_text().splitlines()]
        failed, = [event for event in events if event["event"] == "verification_checkout_reclaim_failed"]
        self.assertEqual(failed["recorded_worktree"], str(self.checkout))

    def test_missing_replacement_checkout_leaves_its_registration(self):
        self.retain_checkout()
        fixture_git(self.repo, "worktree", "move", str(self.checkout), str(self.outside / "original"))
        fixture_git(self.repo, "worktree", "add", "--detach", str(self.checkout), self.entry["binding"]["detached_head"])
        replacement = git.capture_worktree_binding(self.checkout, None)
        new_admin = Path(replacement["git_dir"])
        self.assertNotEqual(checkout_identity.directory_identity(new_admin), self.entry["identity"]["git_dir"])
        shutil.rmtree(self.root)
        self.assert_blocked()
        self.assertTrue(new_admin.is_dir())
        self.assertIn(str(self.checkout), fixture_git(self.repo, "worktree", "list", "--porcelain"))

    def test_moved_checkout_preserves_original_registration(self):
        self.retain_checkout()
        destination = self.outside / "moved-native-checkout"
        fixture_git(self.repo, "worktree", "move", str(self.checkout), str(destination))
        self.assert_blocked()
        self.assertTrue(destination.is_dir() and self.admin.is_dir())
        self.assertIn(str(destination), fixture_git(self.repo, "worktree", "list", "--porcelain"))

    def test_byte_identical_restored_admin_is_preserved(self):
        self.retain_checkout()
        backup = self.admin.with_name("backup-admin")
        before = tree_bytes(self.admin)
        self.admin.rename(backup)
        shutil.copytree(backup, self.admin)
        self.assertEqual(tree_bytes(self.admin), before)
        git.check_worktree_binding(self.checkout, self.entry["binding"])
        self.assert_blocked()
        self.assertTrue(self.checkout.is_dir() and self.admin.is_dir())

    @unittest.skipIf(os.geteuid() == 0, "root can remove files from the read-only fixture directory")
    def test_retry_after_partial_native_deletion(self):
        self.retain_checkout()
        cache = self.checkout / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "locked").write_text("keep")
        cache.chmod(0o500)
        try:
            self.assertEqual(self.cleanup_checkout()["status"], "eligible")
            removed = self.cleanup_checkout(remove=True)
            self.assertEqual(removed["status"], "blocked")
            self.assertIn("git worktree remove", removed["error"])
            self.assertEqual(read_json(self.receipt)["status"], "pending")
            self.assert_registered(False)
            self.assertIn("手工删除", self.assert_blocked()["error"])
        finally:
            if cache.exists():
                cache.chmod(0o700)
        shutil.rmtree(self.checkout)
        self.assertEqual(self.cleanup_checkout()["status"], "absent")
        self.assertEqual(self.cleanup_checkout(remove=True)["status"], "removed")

    def test_native_git_holds_remove_lock_after_parent_death(self):
        def reviewing(run):
            return any(row["phase"] == "reviewing" for row in run["final_history"])

        crash, _ = crash_after_save(self.runtime, reviewing)
        with crash, self.assertRaises(KeyboardInterrupt):
            self.start()
        self.capture_checkout()
        peer = self.outside / "other-native-checkout"
        fixture_git(self.repo, "worktree", "add", "--detach", str(peer), self.entry["binding"]["detached_head"])

        # Pause only the filter in the verification checkout. Resume also runs
        # Git status in the run checkout, which shares the filter configuration.
        entered, release = self.outside / "filter-entered", self.outside / "filter-release"
        helper = self.outside / "filter.py"
        helper.write_text("""import pathlib, sys, time
checkout, entered, release = map(pathlib.Path, sys.argv[1:])
if pathlib.Path.cwd() == checkout:
    entered.touch()
    deadline = time.monotonic() + 45
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(.01)
sys.stdout.buffer.write(sys.stdin.buffer.read())
""")
        attr = Path(self.entry["binding"]["common_dir"]) / "info" / "attributes"
        attr.write_text("hello.py filter=d7-lock-fixture\n")
        fixture_git(self.repo, "config", "filter.d7-lock-fixture.clean",
                    shlex.join([sys.executable, str(helper), str(self.checkout), str(entered), str(release)]))
        os.utime(self.checkout / "hello.py", (1, 1))

        owner = os.fork()
        if owner == 0:
            try:
                self.cleanup_checkout(remove=True)
            finally:
                os._exit(0)
        killed = False
        try:
            deadline = time.monotonic() + 10
            while not entered.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(entered.is_file(), "native Git never entered the filter")
            os.kill(owner, signal.SIGKILL)
            self.assertEqual(os.waitpid(owner, 0)[1], signal.SIGKILL)
            killed = True
            pending = self.receipt.read_bytes()
            self.assertEqual(json.loads(pending)["status"], "pending")
            remove_calls = []
            launch = git.git_process

            def observe(repo, *args, **kwargs):
                if args[:2] == ("worktree", "remove"):
                    remove_calls.append(args[2])
                return launch(repo, *args, **kwargs)

            with patch.object(git, "git_process", side_effect=observe):
                item = self.assert_blocked()
                self.assertIn("上一次删除仍在进行", item["error"])
                result = self.engine(self.backend).resume(self.runtime.run["id"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(remove_calls, [])
            self.assertEqual(self.receipt.read_bytes(), pending)
            self.assertTrue(self.checkout.exists() and self.admin.exists())
            events = [json.loads(line) for line in (self.runtime.directory / "events.jsonl").read_text().splitlines()]
            failed, = [event for event in events if event["event"] == "verification_checkout_reclaim_failed"]
            self.assertIn("上一次删除仍在进行", failed["error"])
        finally:
            release.touch()
            if not killed:
                os.kill(owner, signal.SIGKILL)
                os.waitpid(owner, 0)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            item = self.cleanup_checkout()
            if item["status"] == "absent":
                break
            time.sleep(.02)
        self.assertEqual(item["status"], "absent", item)
        self.assertEqual(self.cleanup_checkout(remove=True)["status"], "removed")
        self.assert_registered(False)
        self.assertTrue(peer.exists())



class CheckoutIdentityTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.config.data["verification"].update(
            fresh_checkout=True,
            setup_commands=[{"cwd": ".", "argv": [sys.executable, "-c", "pass"]}],
        )
        self.plan = read_json(ROOT / "examples/plan.json")
        self.base_commit = fixture_git(self.repo, "rev-parse", "HEAD").strip()
        self.backend = ScriptedBackend()
        self.runtime = self.engine(self.backend)
        # Retain this test's own completed checkouts until RepoCase cleans the fixture.
        reclaim = patch("harness.finalization.FinalAcceptance.reclaim_verified_checkout")
        reclaim.start()
        self.addCleanup(reclaim.stop)
        self.addCleanup(self.remove_fixture_roots)

    def remove_fixture_roots(self):
        for artifact in self.runtime.directory.glob("verification/final/*/verification-*/checkout.json"):
            root = Path(read_json(artifact)["worktree"]).parents[1]
            shutil.rmtree(root, ignore_errors=True)

    def start(self):
        return self.runtime.start("Implement greet", self.plan)

    def ledger(self):
        return read_json(self.runtime.directory / "state.json")

    def assert_stop(self, kind, reason):
        stop = self.ledger()["stop"]
        self.assertEqual((stop["kind"], stop["reason"]), (kind, reason))

    def assert_retry_completes(self, expected_rounds):
        result = self.engine(self.backend).resume(self.runtime.run["id"])
        self.assertEqual(result["status"], "completed")
        entries = self.ledger()["verification_checkouts"]
        self.assertEqual([entry["round"] for entry in entries], expected_rounds)
        self.assertEqual(len({entry["root"] for entry in entries}), len(entries))
        return entries

    def test_creates_and_saves_identity_before_starting_setup(self):
        original_add = git.add_worktree
        original_checks = Verification.run_verification_commands
        created, ready, started = [], [], []

        def identity(path):
            info = os.lstat(path)
            return [info.st_dev, info.st_ino]

        def observe_add(repo, path, commit, **kwargs):
            if kwargs.get("branch") is not None:
                return original_add(repo, path, commit, **kwargs)
            root = path.parents[1]
            self.assertEqual(list(path.iterdir()), [])
            self.assertEqual(self.ledger()["verification_checkouts"], [])
            before = {"root": identity(root), "worktree": identity(path)}
            binding = original_add(repo, path, commit, **kwargs)
            record, = self.ledger()["final_history"]
            self.assertEqual(commit, record["head"])
            self.assertNotEqual(commit, self.base_commit)
            self.assertEqual(fixture_git(path, "rev-parse", "HEAD").strip(), commit)
            self.assertEqual(identity(root), before["root"])
            self.assertEqual(identity(path), before["worktree"])
            created.append({"attempt": 1, "round": 1, "root": str(root), "worktree": str(path),
                            "identity": {**before, "git_dir": identity(binding["git_dir"])},
                            "binding": binding})
            return binding

        def observe_checks(service, *args, **kwargs):
            if kwargs.get("phase") == "setup":
                ready.extend(self.ledger()["verification_checkouts"])
                self.assertEqual(ready, created)
            return original_checks(service, *args, **kwargs)

        def observe_process(argv, cwd, *args, **kwargs):
            if argv == self.config.data["verification"]["setup_commands"][0]["argv"]:
                started.extend(self.ledger()["verification_checkouts"])
                self.assertEqual(started, created)
            return run_process(argv, cwd, *args, **kwargs)

        with patch.object(git, "add_worktree", side_effect=observe_add), \
                patch.object(Verification, "run_verification_commands", observe_checks), \
                patch("harness.verification.run_process", side_effect=observe_process):
            self.assertEqual(self.start()["status"], "completed")
        self.assertEqual(len(created), 1)
        self.assertEqual(ready, created)
        self.assertEqual(started, created)
        ledger = self.ledger()
        entry, = ledger["verification_checkouts"]
        self.assertEqual([entry], created)
        self.assertIs(checkout_identity.entry_for(ledger, 1, 1), entry)
        self.assertIsNone(checkout_identity.entry_for(ledger, 1, 2))
        self.assertIsNone(checkout_identity.entry_for(ledger, 2, 1))
        with self.assertRaises(TypeError):
            checkout_identity.entry_for({"attempt": 1, "round": 1}, Path(entry["root"]),
                                        Path(entry["worktree"]), entry["identity"], entry["binding"])
        env = read_json(next(self.runtime.directory.glob("verification/final/*/verification-*/checkout.json")))
        self.assertTrue({"owner", "binding"}.isdisjoint(env))
        self.assertEqual(env["worktree"], entry["worktree"])

    def test_lookup_matches_both_keys_in_an_interleaved_registry(self):
        self.assertEqual(self.start()["status"], "completed")
        ledger = self.ledger()
        first, = ledger["verification_checkouts"]
        record, = ledger["final_history"]
        round_record, = record["verification_rounds"]
        record["verification_rounds"].append(deepcopy(round_record))
        ledger["final_history"].append({**deepcopy(record), "attempt": 2})

        next_attempt = {**deepcopy(first), "attempt": 2}
        next_round = {**deepcopy(first), "round": 2}
        ledger["verification_checkouts"] = [next_attempt, next_round, first]
        checkout_identity.validate_checkout_entries(ledger)
        for entry in ledger["verification_checkouts"]:
            with self.subTest(attempt=entry["attempt"], round=entry["round"]):
                self.assertIs(checkout_identity.entry_for(ledger, entry["attempt"], entry["round"]), entry)
        self.assertIsNone(checkout_identity.entry_for(ledger, 2, 2))

    def start_with_changed_checkout(self, alter):
        original = git.add_worktree

        def after_add(repo, path, commit, **kwargs):
            binding = original(repo, path, commit, **kwargs)
            if kwargs.get("branch") is None:
                alter(path, binding)
            return binding

        with patch.object(git, "add_worktree", side_effect=after_add), self.assertRaises(HarnessError):
            self.start()
        self.assert_stop("workspace_mismatch", "binding_changed")
        self.assertEqual(self.runtime.run["verification_checkouts"], [])
        self.assertEqual(self.ledger()["verification_checkouts"], [])
        self.assertFalse(any(row.get("phase") == "setup" for row in self.ledger()["dispatches"]))
        self.engine(self.backend).load_run(self.runtime.run["id"])

    def test_changed_live_head_refuses_registration_and_retries(self):
        self.start_with_changed_checkout(
            lambda path, binding: fixture_git(path, "update-ref", "--no-deref", "HEAD", self.base_commit))
        self.assert_retry_completes([2])

    def test_changed_returned_head_refuses_registration_and_retries(self):
        self.start_with_changed_checkout(
            lambda path, binding: binding.update(detached_head=self.base_commit))
        self.assert_retry_completes([2])

    def test_changed_worktree_identity_stops_before_preparation(self):
        def change(path, binding):
            # The checkout still points to the original management directory.
            moved = path.with_name("original")
            path.rename(moved)
            path.mkdir()
            (path / ".git").write_bytes((moved / ".git").read_bytes())
            git.check_worktree_binding(path, binding)

        self.start_with_changed_checkout(change)

    def test_rejects_candidate_using_the_load_validator(self):
        original = checkout_identity.build_entry

        def candidate(*args):
            entry = original(*args)
            entry["identity"]["git_dir"][1] = True
            return entry

        with patch.object(checkout_identity, "build_entry", side_effect=candidate), self.assertRaises(HarnessError):
            self.start()
        self.assert_stop("workspace_mismatch", "binding_changed")
        self.assertEqual(self.runtime.run["verification_checkouts"], [])
        self.assertEqual(self.ledger()["verification_checkouts"], [])
        self.assertFalse(any(row.get("phase") == "setup" for row in self.ledger()["dispatches"]))
        self.assert_retry_completes([2])

    def test_death_after_git_registration_leaves_unrecorded_round(self):
        crash, die = crash_point(self.runtime)
        original = git.capture_worktree_binding

        def capture(path, branch, **kwargs):
            if branch is None:
                self.assertTrue((path / ".git").is_file())
                die()
            return original(path, branch, **kwargs)

        with crash, patch.object(git, "capture_worktree_binding", side_effect=capture), self.assertRaises(KeyboardInterrupt):
            self.start()
        first = self.ledger()
        self.assertEqual(first["verification_checkouts"], [])
        first_round, = first["final_history"][0]["verification_rounds"]
        environment = read_json(Path(first_round["artifact_dir"]) / "checkout.json")
        orphan = environment["worktree"]
        self.assertNotIn("binding", environment)
        self.assertIn(f"worktree {orphan}", fixture_git(self.repo, "worktree", "list", "--porcelain"))

        entries = self.assert_retry_completes([2])
        self.assertNotEqual(entries[0]["worktree"], orphan)
        preview = cleanup.cleanup_run(self.repo, first["id"])
        self.assertEqual(preview["checkouts"][0]["status"], "blocked")

    def test_death_after_save_preserves_first_round_registration(self):
        crash, fired = crash_after_save(self.runtime, lambda run: bool(run["verification_checkouts"]))
        with crash, self.assertRaises(KeyboardInterrupt):
            self.start()
        self.assertTrue(fired["fired"])
        first = self.ledger()
        entry, = first["verification_checkouts"]
        self.assertEqual(first["final_history"][0]["verification_rounds"][0]["status"], "running")
        self.assertEqual(set(entry["identity"]), {"root", "worktree", "git_dir"})
        for field, path in (("root", entry["root"]), ("worktree", entry["worktree"]),
                            ("git_dir", entry["binding"]["git_dir"])):
            info = os.lstat(path)
            self.assertEqual(entry["identity"][field], [info.st_dev, info.st_ino])
        self.assertEqual(self.assert_retry_completes([1, 2])[0], entry)

    def test_resume_and_abandon_refuse_bad_registrations(self):
        crash, _ = crash_after_save(self.runtime, lambda run: bool(run["verification_checkouts"]))
        with crash, self.assertRaises(KeyboardInterrupt):
            self.start()
        path = self.runtime.directory / "state.json"
        good = self.ledger()
        entry, = good["verification_checkouts"]
        root_in_repo = str(self.repo.resolve() / "harness-verify-test")
        broken_entries = [
            None,
            {},
            [None],
            [{**entry, "extra": "field"}],
            [entry, deepcopy(entry)],
            *[[{**entry, **change}] for change in (
                {"attempt": 2}, {"attempt": True}, {"round": 2}, {"round": False},
                {"identity": {**entry["identity"], "root": [1]}},
                {"identity": {**entry["identity"], "root": [True, 2]}},
                {"identity": {**entry["identity"], "root": [1, -1]}},
                {"binding": {**entry["binding"], "branch": "main"}},
                {"binding": {**entry["binding"], "detached_head": self.base_commit}},
                {"binding": {**entry["binding"], "common_dir": str(self.outside / ".git")}},
                {"binding": {**entry["binding"], "git_dir": str(self.outside / "checkout")}},
                {"binding": {**entry["binding"], "gitfile_sha256": False}},
                {"worktree": entry["root"] + "/checkout"},
                {"root": root_in_repo, "worktree": root_in_repo + "/.harness/checkout"},
                {"root": entry["root"] + "/."},
            )],
        ]
        missing = deepcopy(good)
        del missing["verification_checkouts"]
        variants = [missing, *[{**good, "verification_checkouts": value} for value in broken_entries]]
        model_calls = len(self.backend.calls)
        for index, damaged in enumerate(variants):
            with self.subTest(variant=index):
                write_json(path, damaged)
                before = path.read_bytes()
                for command in ("resume", "abandon"):
                    refused_by_cli(self, self.backend, [command, good["id"]], ("evidence", "load_integrity"), 1)
                state_before = tree_bytes(self.runtime.context.state)
                for options in ([], ["--remove"]):
                    refused_by_cli(self, self.backend, ["cleanup-run", good["id"], *options],
                                   ("evidence", "runtime_integrity"), 1)
                self.assertEqual(tree_bytes(self.runtime.context.state), state_before)
                self.assertEqual(path.read_bytes(), before)
                self.assertTrue(Path(entry["worktree"]).exists())
                self.assertEqual(len(self.backend.calls), model_calls)
        write_json(path, good)
        self.engine(self.backend).load_run(good["id"])
