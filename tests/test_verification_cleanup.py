import contextlib
from copy import deepcopy
import errno
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

from git_isolation import fixture_git
from harness import checkout_identity, cleanup, git, state_files, stops
from harness.cleanup import cleanup_run
from harness.cli import main
from harness.path_evidence import project_paths
from harness.run_store import RunStore
from harness.state_files import LockedDirectory, StateReadError
from harness.util import HarnessError, read_json, write_json, run_process
from harness.versions import RUN_SCHEMA_VERSION
from harness.worker_returns import inspect_return
from test_harness import RepoCase, ROOT, add_worker_dispatch, cli_notice, run_cli, write_project_config
from test_readonly_cleanup import nonregular, tree_bytes
from test_run_store import swap
from test_state_files import FakeOs, held_lock
from test_stop_exits import final_failure_scenario


class VerificationCleanupTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.config.data["verification"].update(fresh_checkout=True,
            commands=[{"cwd": ".", "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('hello.py').is_file()"]}])
        self.engine_instance = self.engine()
        # A verified checkout the runtime did not reclaim: the state a reclamation
        # failure before its intent leaves behind (no cleanup.json).
        with patch("harness.finalization.FinalAcceptance.reclaim_verified_checkout"):
            self.run = self.engine_instance.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.directory = self.engine_instance.directory
        self.artifact = next(self.directory.glob("verification/final/*/verification-*/checkout.json"))
        self.environment = read_json(self.artifact)
        self.entry, = self.run["verification_checkouts"]
        self.checkout = Path(self.entry["worktree"])
        self.roots = [self.checkout.parents[1]]
        self.addCleanup(self.cleanup_roots)

    def cleanup_roots(self):
        for root in self.roots:
            shutil.rmtree(root, ignore_errors=True)

    def cleanup(self, remove=False, **kwargs):
        return cleanup_run(self.repo, self.run["id"], remove=remove, **kwargs)

    def git_calls(self, interrupt_after_remove=None):
        """Record exact worktree removals; optionally interrupt after the Nth one."""
        removed, original = [], git.git_process

        def recording(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[:2] == ("worktree", "remove"):
                removed.append(args[2])
                if len(removed) == interrupt_after_remove:
                    raise KeyboardInterrupt()
            return result
        return removed, patch("harness.cleanup.git.git_process", side_effect=recording)

    def additional_checkout(self, *, prefix="harness-verify-"):
        root = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
        self.roots.append(root)
        checkout = root / ".harness" / "checkout"
        fixture_git(self.repo, "worktree", "add", "--detach", str(checkout), self.environment["head"])
        binding = git.capture_worktree_binding(checkout, None)
        identity = {key: checkout_identity.directory_identity(path) for key, path in (
            ("root", root), ("worktree", checkout), ("git_dir", Path(binding["git_dir"])))}
        history = self.run["final_history"]
        attempt = len(history) + 1
        # A later attempt has a retained round whose verdict was never published.
        environment = {**self.environment, "worktree": str(checkout)}
        self.run["verification_checkouts"].append(checkout_identity.build_entry(
            {"attempt": attempt, "round": 1}, root, checkout, identity, binding))
        attempt_dir = self.directory / "verification" / "final" / str(attempt)
        folder = attempt_dir / "verification-1"
        artifact = folder / "checkout.json"
        write_json(artifact, environment)
        history.append({"attempt": attempt, "head": environment["head"], "tree": history[0]["tree"],
                        "snapshot_digest": history[0]["snapshot_digest"],
                        "snapshot_artifact": str(attempt_dir / "snapshot.json"),
                        "artifact": str(attempt_dir / "final.json"), "accepted_tasks": sorted(self.run["accepted"]),
                        "status": "running", "phase": "verifying", "approved_batches": [],
                        "verification_rounds": [{"artifact_dir": str(folder), "status": "running"}]})
        write_json(self.directory / "state.json", self.run)
        return checkout, artifact

    def test_checkout_registration_requires_complete_unique_observation(self):
        root = Path(tempfile.mkdtemp(prefix="harness-verify-")).resolve()
        self.roots.append(root)
        other = root / ".harness" / "checkout"
        fixture_git(self.repo, "worktree", "add", "--detach", str(other), self.environment["head"])
        other_admin = Path(git.capture_worktree_binding(other, None)["git_dir"])
        target_admin = Path(self.entry["binding"]["git_dir"])
        target = os.fsencode(self.checkout)
        other_bytes = os.fsencode(other)
        main_record = b"worktree " + os.fsencode(self.repo.resolve()) + b"\0bare\0\0"

        def linked(path):
            return b"worktree " + path + b"\0HEAD " + self.environment["head"].encode() + b"\0detached\0\0"

        main_record += linked(os.fsencode(self.run["worktree"]))
        real_process, real_binding_file = git.git_process, git._binding_file_at
        cases = (
            ("duplicate", target, linked(target), "2 条登记"),
            ("case variant", target.upper(), linked(target.upper()), "2 条登记"),
            ("noncanonical suffix", b"/elsewhere/" + target, linked(b"/elsewhere/" + target), "无法核实"),
            ("wrong owner", target, linked(other_bytes), "不属于创建时的管理目录"),
            ("omitted", other_bytes, b"", "无法核实"),
            ("malformed", other_bytes, linked(other_bytes)[:-1], "无法核实"),
            ("malformed field", other_bytes, linked(other_bytes).replace(b"detached", b"unknown"), "无法核实"),
            ("malformed admin", None, linked(other_bytes), "无法核实"),
        )
        receipt = self.artifact.with_name("cleanup.json")
        identity = checkout_identity.directory_identity
        for label, other_path, other_record, message in cases:
            for legacy in (False, True) if label in ("duplicate", "wrong owner", "malformed admin") else (False,):
                removals, writes = [], []

                def process(repo, *args, **kwargs):
                    if args[:2] == ("worktree", "remove"):
                        removals.append(args)
                        raise AssertionError("The fixture never invokes Git removal")
                    if args == ("worktree", "list", "--porcelain", "-z"):
                        if legacy:
                            return subprocess.CompletedProcess(
                                args, 129, b"", b"error: unknown switch `z'\nusage: git worktree add ...\n")
                        return subprocess.CompletedProcess(args, 0, main_record + linked(target) + other_record, b"")
                    return real_process(repo, *args, **kwargs)

                def binding_file(held, fd, path):
                    if label == "wrong owner" and path == target_admin / "gitdir":
                        return other_bytes + b"/.git\n"
                    if path == other_admin / "gitdir":
                        return b"malformed" if other_path is None else other_path + b"/.git\n"
                    return real_binding_file(held, fd, path)

                def directory_identity(path):
                    return None if stale and path == self.checkout else identity(path)

                with self.subTest(label=label, legacy=legacy):
                    with patch.object(git, "git_process", side_effect=process), \
                            patch.object(git, "_binding_file_at", side_effect=binding_file), \
                            patch.object(cleanup, "_clean_worktree", return_value={"status": "retained"}), \
                            patch.object(cleanup, "write_json", side_effect=lambda path, record: writes.append(path)):
                        for remove in (False, True):
                            for stale in (False, True):
                                with self.subTest(remove=remove, stale=stale), \
                                        patch.object(checkout_identity, "directory_identity", side_effect=directory_identity):
                                    item = self.cleanup(remove=remove)["checkouts"][0]
                                    self.assertEqual(item["status"], "blocked")
                                    self.assertIn(message, item["error"])
                        if label == "duplicate":
                            self.assertEqual(git._worktree_registration(
                                self.repo, target, binding=None, isolated_config=False), "unknown")
                    self.assertEqual(removals, [])
                    self.assertEqual(writes, [])
                    self.assertTrue(self.checkout.is_dir())
                    self.assertTrue(other.is_dir())
                    self.assertFalse(receipt.exists())

    def test_checkout_registration_preserves_distinct_native_same_basename_paths(self):
        other, _ = self.additional_checkout(prefix="harness-verify-line\nbreak\t ")
        self.assertEqual(other.name, self.checkout.name)
        self.assertNotEqual(other, self.checkout)
        items = self.cleanup()["checkouts"]
        self.assertEqual([(item.get("worktree"), item["status"]) for item in items],
                         [(str(self.checkout), "eligible"), (str(other), "eligible")])

    def statuses(self, result):
        return [item["status"] for item in result["checkouts"]], result["worktree"]["status"]

    def cleanup_cli(self, *options, run_id=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--repo", str(self.repo), "cleanup-run", run_id or self.run["id"], *options])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_preview_rejects_special_files_across_cleanup_gates_without_writing(self):
        worker = next(entry for entry in self.run["dispatches"] if entry["role"] == "worker")
        packet = Path(worker["worker_receipt"]["packet_artifact"])
        job = self.directory / "dispatches" / "ended"
        dispatch = add_worker_dispatch(self.run, self.directory, job.name, worker["task_id"],
                                       exit_confirmed=True, worker_receipt_consumed="provider_failure")
        original = job / "process.stdout"
        original.write_bytes(b"")
        dispatch["worker_return_evidence"] = inspect_return(job)
        ref, = project_paths({"changed_files": ["path" * 1100]}, self.directory)["path_evidence_indexes"].values()
        dispatch["path_evidence"] = [ref]
        commands = job / "commands"
        dispatch["command_directory"] = str(commands)
        journal = commands / ("a" * 32) / "process.json"
        write_json(journal, {"id": f"ended:command:{journal.parent.name}", "command_id": "test",
                             "pgid": None, "process_state": "exited", "exit_confirmed": True})
        write_json(self.directory / "state.json", self.run)
        self.assertEqual(self.statuses(self.cleanup()), (["eligible"], "eligible"))

        files = {
            journal: ("process_unconfirmed", "command_exit"),
            self.directory / "run.lock": ("evidence", "runtime_integrity"),
            self.artifact.with_name("cleanup.json"): ("resource", "checkouts"),
            self.directory / "worktree-cleanup.json": ("resource", "worktree"),
            packet: ("evidence", "runtime_integrity"),
            original: ("evidence", "runtime_integrity"),
            Path(ref["artifact"]): ("evidence", "runtime_integrity"),
        }
        for path, expected in files.items():
            for form in ("fifo", "symlink"):
                with self.subTest(path=path.name, form=form), nonregular(self, path, form):
                    before = tree_bytes(self.engine_instance.context.state)
                    code, output, error = self.cleanup_cli()
                    self.assertEqual(code, 1, output + error)
                    if expected[0] == "resource" and output:
                        item = json.loads(output)[expected[1]]
                        if isinstance(item, list):
                            item, = item
                        self.assertEqual(item["status"], "blocked")
                    else:
                        stop = cli_notice(error)
                        self.assertIsNotNone(stop, error)
                        self.assertEqual((stop["kind"], stop["reason"]),
                                         ("evidence", "runtime_integrity") if expected[0] == "resource" else expected)
                    self.assertEqual(tree_bytes(self.engine_instance.context.state), before)
                    self.assertTrue(self.checkout.is_dir())

    def test_damaged_final_records_block_preview_and_removal_before_any_resource_changes(self):
        variants = {
            "missing_head": lambda record: record.pop("head"),
            "empty_head": lambda record: record.update(head=""),
            "bad_head": lambda record: record.update(head=None),
            "unapproved_head": lambda record: record.update(head=self.run["base_commit"]),
            "attempt_type": lambda record: record.update(attempt="1"),
            "attempt_identity": lambda record: record.update(attempt=2),
            "attempt_artifact": lambda record: record.update(artifact=str(self.artifact)),
            "empty_rounds": lambda record: record.update(verification_rounds=[]),
            "round_element": lambda record: record.update(verification_rounds=[None]),
            "round_status": lambda record: record["verification_rounds"][0].update(status=[]),
            "uncompleted_round": lambda record: record["verification_rounds"][0].update(status="running"),
            "missing_round_digest": lambda record: record["verification_rounds"][0].pop("checkout_digest"),
            "round_digest": lambda record: record["verification_rounds"][0].update(checkout_digest=None),
            "invalid_round_digest": lambda record: record["verification_rounds"][0].update(checkout_digest="not a digest"),
            "verdict_for_another_round": lambda record: record.update(
                verification=str(self.artifact.parent.parent / "verification-2" / "results.json")),
        }
        for label, damage in variants.items():
            with self.subTest(label=label):
                run = deepcopy(self.run)
                damage(run["final_history"][0])
                write_json(self.directory / "state.json", run)
                self.assert_corrupt_ledger_rejected(self.engine_instance, self.checkout)
        write_json(self.directory / "state.json", {**self.run, "final_history": []})
        self.assert_corrupt_ledger_rejected(self.engine_instance, self.checkout)
        self.add_unknown_dispatch("needs-confirmation")
        variants["missing_round_digest"](self.run["final_history"][0])
        write_json(self.directory / "state.json", self.run)
        self.assert_corrupt_ledger_rejected(self.engine_instance, self.checkout,
                                           options=(("--remove", "--confirm-exited", "needs-confirmation"),))

    def assert_corrupt_ledger_rejected(self, engine, checkout, *, options=((), ("--remove",))):
        before = tree_bytes(engine.context.state)
        checkout_before = tree_bytes(checkout)
        registered = git.git(self.repo, "worktree", "list", "--porcelain")
        for args in options:
            with self.subTest(options=args):
                code, output, error = self.cleanup_cli(*args, run_id=engine.run["id"])
                self.assertEqual((code, output), (1, ""), error)
                stop = cli_notice(error)
                self.assertIsNotNone(stop, error)
                self.assertEqual((stop["kind"], stop["reason"]), ("evidence", "runtime_integrity"))
                self.assertIn("整体", error)
                self.assertEqual(tree_bytes(engine.context.state), before)
                self.assertTrue(checkout.is_dir() and engine.worktree.is_dir())
                self.assertEqual(tree_bytes(checkout), checkout_before)
                self.assertEqual(git.git(self.repo, "worktree", "list", "--porcelain"), registered)

    def test_invalid_registry_refuses_audited_removal_even_with_a_confirmable_dispatch(self):
        dispatch = self.add_unknown_dispatch()
        write_json(self.directory / "state.json", {**self.run, "verification_checkouts": [None]})
        before = tree_bytes(self.engine_instance.context.state)
        code, output, error = self.cleanup_cli("--remove", "--confirm-exited", dispatch["id"])
        self.assertEqual((code, output), (1, ""), error)
        stop = cli_notice(error)
        self.assertEqual((stop["kind"], stop["reason"]), ("evidence", "runtime_integrity"))
        self.assertIn("verification_checkouts", error)
        self.assertEqual(tree_bytes(self.engine_instance.context.state), before)
        self.assertTrue(self.checkout.is_dir() and self.engine_instance.worktree.is_dir())

    def test_malformed_verification_checks_refuse_cleanup_of_a_failed_attempt(self):
        self.config.data["verification"]["commands"].append(
            {"cwd": ".", "argv": [sys.executable, "-c", "pass"]})
        _, engine, _ = final_failure_scenario(self)
        artifact, = engine.directory.glob("verification/final/*/verification-*/checkout.json")
        checkout = Path(read_json(artifact)["worktree"])
        self.roots.append(checkout.parents[1])
        state = engine.directory / "state.json"
        original = read_json(state)
        final, = original["final_history"]
        check, passing = final["verification_failures"]
        self.assertEqual((final["status"], check["exit_code"]), ("verification_failed", 1))
        self.assertEqual(passing["status"], "passed")
        code, output, error = self.cleanup_cli(run_id=original["id"])
        self.assertEqual(code, 0, error)
        self.assertEqual(self.statuses(json.loads(output)), (["eligible"], "retained"))
        for label, checks in {
            "exit_code_type": [{**check, "exit_code": []}],
            "empty_object": [{}],
            "null": [None],
            "process_outcome_type": [{**check, "process_outcome": None}],
            "empty_list": [],
            "empty_termination_reason": [{**check, "termination_reason": ""}],
            "only_passing": [passing],
            "only_failed": [check],
            "reordered": [passing, check],
            "failed_zero_exit": [{**check, "exit_code": 0}],
            "inconsistent_timeout": [{**check, "timed_out": True}],
        }.items():
            with self.subTest(label=label):
                damaged = deepcopy(original)
                damaged["final_history"][0]["verification_failures"] = checks
                write_json(state, damaged)
                self.assert_corrupt_ledger_rejected(engine, checkout)
        damaged = deepcopy(original)
        damaged["final_history"][0]["failure_kind"] = "timeout"
        write_json(state, damaged)
        self.assert_corrupt_ledger_rejected(engine, checkout)
        write_json(state, original)
        # Editing today's project file does not change the historical command set.
        write_project_config(self.repo, commands=[])
        code, output, error = self.cleanup_cli("--remove", run_id=original["id"])
        self.assertEqual(code, 0, error)
        self.assertEqual(self.statuses(json.loads(output)), (["removed"], "retained"))
        self.assertFalse(checkout.exists())

    def run_failed_check(self, expected_reason, **patches):
        engine = self.engine()
        with patch("harness.verification.run_process", wraps=run_process, **patches), self.assertRaises(HarnessError):
            engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        state = engine.directory / "state.json"
        original = read_json(state)
        artifact, = engine.directory.glob("verification/final/*/verification-*/checkout.json")
        checkout = Path(read_json(artifact)["worktree"])
        self.roots.append(checkout.parents[1])
        final, = original["final_history"]
        check, = final["verification_failures"]
        self.assertEqual(check["termination_reason"], expected_reason)
        return engine, checkout, state, original

    def test_timeout_exit_and_result_dispatch_must_agree(self):
        self.config.data["runtime"]["verify_timeout_seconds"] = 0.5
        self.config.data["verification"]["commands"] = [{"cwd": ".", "argv": [sys.executable, "-c",
            "from pathlib import Path; import time; time.sleep(5) if Path.cwd().name == 'checkout' else None"]}]
        engine, checkout, state, original = self.run_failed_check("timeout")
        final, = original["final_history"]
        check, = final["verification_failures"]
        self.assertIs(type(check["exit_code"]), int)
        self.assertTrue(check["exit_confirmed"] and check["evidence_complete"])
        damaged = deepcopy(original)
        damaged["final_history"][0]["verification_failures"][0]["exit_code"] = None
        write_json(state, damaged)
        self.assert_corrupt_ledger_rejected(engine, checkout)
        write_json(state, original)

        artifact = Path(final["verification"])
        results = read_json(artifact)
        damaged = deepcopy(results)
        damaged[0]["timing_source"]["dispatch_id"] = "not-this-check"
        write_json(artifact, damaged)
        self.assert_corrupt_ledger_rejected(engine, checkout)
        write_json(artifact, [])
        self.assert_corrupt_ledger_rejected(engine, checkout)
        write_json(artifact, results)
        code, output, error = self.cleanup_cli("--remove", run_id=original["id"])
        self.assertEqual(code, 0, error)
        self.assertEqual(self.statuses(json.loads(output)), (["removed"], "retained"))

    def test_completed_start_failure_can_be_cleaned_up(self):
        def missing_directory(argv, cwd, log, **kwargs):
            if cwd.name == "checkout":
                kwargs["relative_cwd"] = "absent-directory"
            return run_process(argv, cwd, log, **kwargs)

        _, checkout, _, original = self.run_failed_check("start_failed", side_effect=missing_directory)
        check, = original["final_history"][0]["verification_failures"]
        self.assertIsNone(check["exit_code"])
        self.assertTrue(check["exit_confirmed"] and check["evidence_complete"])
        code, output, error = self.cleanup_cli("--remove", run_id=original["id"])
        self.assertEqual(code, 0, error)
        self.assertEqual(self.statuses(json.loads(output)), (["removed"], "retained"))
        self.assertFalse(checkout.exists())

    def test_abandon_accepts_optional_fields_missing_from_old_failure_records(self):
        backend, engine, _ = final_failure_scenario(self)
        state = engine.directory / "state.json"
        original = read_json(state)
        artifact, = engine.directory.glob("verification/final/*/verification-*/checkout.json")
        self.roots.append(Path(read_json(artifact)["worktree"]).parents[1])
        for field in ("stdout", "stderr", "log"):
            with self.subTest(field=field):
                legacy = deepcopy(original)
                del legacy["final_history"][0]["verification_failures"][0][field]
                write_json(state, legacy)
                code, error = run_cli(self, backend, ["abandon", original["id"]])
                self.assertEqual(code, 0, error)
                self.assertEqual(read_json(state)["status"], "abandoned")
                code, output, error = self.cleanup_cli(run_id=original["id"])
                self.assertEqual(code, 0, error)
                self.assertEqual(self.statuses(json.loads(output)), (["eligible"], "eligible"))

    def test_confirmation_refuses_replaced_directory_before_attach_or_audit(self):
        selected = self.add_unknown_dispatch()
        attach = RunStore.attach
        for boundary in ("attach", "audit"):
            with self.subTest(boundary=boundary):
                moved = self.outside / boundary
                snapshots = []

                def replace_directory():
                    swap(self.directory, moved)
                    (self.directory / "store.lock").unlink()
                    snapshots.append(tree_bytes(self.directory))

                def replace_at_attach(store, run):
                    replace_directory()
                    return attach(store, run)

                selected["pgid"] = 2_000_000_000
                write_json(self.directory / "state.json", self.run)
                before = tree_bytes(self.directory)
                hook = (patch.object(RunStore, "attach", replace_at_attach) if boundary == "attach" else
                        patch("harness.cleanup.group_alive", side_effect=lambda pgid: replace_directory()))
                try:
                    with hook:
                        code, output, error = self.cleanup_cli("--remove", "--confirm-exited", selected["id"])
                    self.assertEqual((code, output), (1, ""), error)
                    stop = cli_notice(error)
                    self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "invariant"))
                    self.assertEqual(len(snapshots), 1)
                    self.assertEqual(tree_bytes(self.directory), snapshots[0])
                    self.assertEqual(tree_bytes(moved), before)
                    self.assertTrue(self.checkout.is_dir() and self.engine_instance.worktree.is_dir())
                finally:
                    if moved.exists():
                        shutil.rmtree(self.directory)
                        moved.rename(self.directory)

    def test_cleanup_validates_the_ledger_in_the_locked_directory(self):
        damaged = deepcopy(self.run)
        damaged["final_history"][0].pop("head")
        state = self.directory / "state.json"
        write_json(state, damaged)
        before = tree_bytes(self.directory)
        entered = LockedDirectory.__enter__
        for options in ((), ("--remove",)):
            with self.subTest(options=options):
                moved = self.outside / "moved"
                snapshots = []

                def replace_after_lock(lock):
                    locked = entered(lock)
                    if locked.outcome.path == str(self.directory / "run.lock"):
                        swap(self.directory, moved)
                        write_json(state, self.run)
                        snapshots.append(tree_bytes(self.directory))
                    return locked

                try:
                    with patch.object(LockedDirectory, "__enter__", replace_after_lock):
                        code, output, error = self.cleanup_cli(*options)
                    self.assertEqual((code, output), (1, ""), error)
                    self.assertIn("final_history[0].head", error)
                    stop = cli_notice(error)
                    self.assertEqual((stop["kind"], stop["reason"]), ("evidence", "runtime_integrity"))
                    self.assertEqual(len(snapshots), 1)
                    self.assertEqual(tree_bytes(self.directory), snapshots[0])
                    self.assertEqual(tree_bytes(moved), before)
                    self.assertTrue(self.checkout.is_dir() and self.engine_instance.worktree.is_dir())
                finally:
                    if moved.exists():
                        shutil.rmtree(self.directory)
                        moved.rename(self.directory)

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

    def test_missing_worktree_without_cleanup_intent_is_blocked_and_checkout_still_removed(self):
        worktree = self.engine_instance.worktree
        state = self.directory / "state.json"
        # The worktree disappeared outside cleanup-run: no receipt records an intent.
        git.git(self.repo, "worktree", "remove", str(worktree))
        write_json(state, {**read_json(state), "status": "abandoned"})
        ledger = state.read_bytes()
        for remove in (False, True):
            with self.subTest(remove=remove):
                result = self.cleanup(remove=remove)
                self.assertEqual(result["worktree"]["status"], "blocked")
                self.assertIn("清理意图", result["worktree"]["error"])
                self.assertEqual(result["checkouts"][0]["status"], "removed" if remove else "eligible")
        self.assertFalse(self.checkout.exists())
        self.assertFalse((self.directory / "worktree-cleanup.json").exists())
        self.assertEqual(state.read_bytes(), ledger)
        self.assertEqual(git.git(self.repo, "rev-parse", "refs/heads/" + self.run["branch"]).strip(),
                         self.run["expected_head"])

    def test_refuses_modified_untracked_and_hidden_index_files(self):
        variants = ("tracked", "untracked", "assume-unchanged", "skip-worktree")
        for variant in variants:
            with self.subTest(variant=variant):
                if variant == "tracked":
                    (self.checkout / "hello.py").write_text("unsaved")
                elif variant == "untracked":
                    path = self.checkout / "scratch"
                    path.write_text("unsaved")
                else:
                    git.git(self.checkout, "update-index", "--" + variant, "hello.py")
                item = self.cleanup(remove=True)["checkouts"][0]
                self.assertEqual(item["status"], "blocked")
                self.assertTrue(self.checkout.is_dir())
                self.assertFalse(self.artifact.with_name("cleanup.json").exists())
                if variant == "tracked":
                    git.git(self.checkout, "checkout", "--", "hello.py")
                elif variant == "untracked":
                    self.assertEqual(path.read_text(), "unsaved")
                    path.unlink()
                else:
                    git.git(self.checkout, "update-index", "--no-" + variant, "hello.py")

    def test_run_lock_and_live_or_unknown_process_refuse_all_removal(self):
        with held_lock(self.directory / "run.lock"), self.assertRaisesRegex(HarnessError, "运行锁"):
            self.cleanup(remove=True)
        self.run["dispatches"].append({"id": "old", "role": "verification", "pgid": 12345,
                                       "process_record": "verification/fixture/old/check-0.outcome.json",
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
        write_json(self.artifact, {**self.environment, "worktree": str(self.engine_instance.worktree),
                                   "head": "0" * 40, "owner": {"run_id": "foreign"}})
        item, = self.cleanup()["checkouts"]
        self.assertEqual((item["status"], item["worktree"]), ("eligible", str(self.checkout)))
        self.assertTrue(self.engine_instance.worktree.exists() and self.checkout.exists())
        write_json(self.directory / "state.json", {**self.run, "verification_checkouts": [
            {**self.entry, "worktree": str(self.engine_instance.worktree)}]})
        with self.assertRaisesRegex(HarnessError, "verification_checkouts"):
            self.cleanup(remove=True)
        write_json(self.directory / "state.json", self.run)
        git.git(self.checkout, "checkout", "--detach", self.run["base_commit"])
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "blocked")
        self.assertTrue(self.checkout.exists())

    def test_missing_checkout_unregisters_only_exact_saved_binding(self):
        other, other_artifact = self.additional_checkout()
        shutil.rmtree(self.checkout)
        shutil.rmtree(other)
        # The unreferenced stale checkout must never be touched by a broad prune.
        self.run["final_history"].pop()
        self.run["verification_checkouts"].pop()
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
        backlink = Path(self.entry["binding"]["git_dir"]) / "gitdir"
        backlink.write_text(str(self.engine_instance.worktree / ".git") + "\n")
        self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "blocked")
        self.assertTrue(backlink.parent.exists())
        self.assertTrue(self.engine_instance.worktree.exists())

    def test_completed_cleanup_ignores_reused_git_administrative_name(self):
        self.cleanup(remove=True)
        other, artifact = self.additional_checkout()
        self.assertEqual(self.run["verification_checkouts"][-1]["binding"]["git_dir"], self.entry["binding"]["git_dir"])
        result = self.cleanup()
        self.assertEqual([item["status"] for item in result["checkouts"]], ["absent", "eligible"])
        self.assertTrue(other.is_dir())

    def test_missing_pruned_checkout_accepts_reused_admin_name_without_a_second_removal(self):
        self.run["status"] = "stopped"
        shutil.rmtree(self.checkout.parents[1])
        fixture_git(self.repo, "worktree", "prune", "--expire", "now")
        other, _ = self.additional_checkout()
        replacement = self.run["verification_checkouts"].pop()
        self.run["final_history"].pop()
        write_json(self.directory / "state.json", self.run)
        self.assertEqual(replacement["binding"]["git_dir"], self.entry["binding"]["git_dir"])
        self.assertNotEqual(replacement["identity"]["git_dir"], self.entry["identity"]["git_dir"])
        cache = other / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "neighbor").write_text("keep")
        self.assertEqual(self.cleanup()["checkouts"][0]["status"], "absent")
        removed, recording = self.git_calls()
        with recording:
            self.assertEqual(self.cleanup(remove=True)["checkouts"][0]["status"], "removed")
        self.assertEqual(removed, [])
        self.assertEqual((cache / "neighbor").read_text(), "keep")
        self.assertIn(str(other), fixture_git(self.repo, "worktree", "list", "--porcelain"))

    def test_interruption_after_git_removal_recovers_pending_receipt(self):
        original_git = git.git_process

        def interrupted(repo, *args, **kwargs):
            result = original_git(repo, *args, **kwargs)
            if args[:2] == ("worktree", "remove"):
                raise KeyboardInterrupt()
            return result

        with patch("harness.cleanup.git.git_process", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
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

    def test_checkout_receipt_io_failure_blocks_only_that_checkout(self):
        other, _ = self.additional_checkout()
        ledger = (self.directory / "state.json").read_bytes()
        checkout_before = tree_bytes(self.checkout)

        def fail_first_receipt_probe(open_file, name, *args, **kwargs):
            if name == "cleanup.json" and not injected:
                injected.append(name)
                raise OSError(errno.EIO, "receipt probe")
            return open_file(name, *args, **kwargs)

        for options, expected in (((), "eligible"), (("--remove",), "removed")):
            with self.subTest(options=options):
                injected = []
                with patch.object(state_files, "os", FakeOs(open=fail_first_receipt_probe)):
                    code, output, error = self.cleanup_cli(*options)
                self.assertEqual((code, error), (1, ""), output)
                self.assertEqual(injected, ["cleanup.json"])
                result = json.loads(output)
                self.assertEqual(self.statuses(result), (["blocked", expected], expected))
                blocked = result["checkouts"][0]
                self.assertEqual(blocked["artifact"], str(self.artifact))
                self.assertIn("读取失败", blocked["error"])
                self.assertIn(os.strerror(errno.EIO), blocked["error"])
                self.assertEqual(tree_bytes(self.checkout), checkout_before)
                self.assertFalse(self.artifact.with_name("cleanup.json").exists())
                self.assertEqual((self.directory / "state.json").read_bytes(), ledger)
                registered = git.git(self.repo, "worktree", "list", "--porcelain")
                self.assertIn(str(self.checkout), registered)
                for checkout in (other, self.engine_instance.worktree):
                    self.assertEqual(checkout.is_dir(), not options)
                    self.assertEqual(str(checkout) in registered, not options)

    def add_unknown_dispatch(self, ident="unknown"):
        entry = {"id": ident, "role": "verification", "status": "running",
                 "process_record": f"verification/fixture/{ident}/check-0.outcome.json",
                 "pgid": None, "exit_confirmed": False}
        self.run["dispatches"].append(entry)
        write_json(self.directory / "state.json", self.run)
        return entry

    def confirmation_events(self):
        return [entry for line in (self.directory / "events.jsonl").read_text().splitlines()
                if (entry := json.loads(line))["event"] == "dispatch_exit_confirmed_by_operator"]

    def test_terminal_confirmation_preserves_status_and_version(self):
        evidence = self.artifact.read_bytes()
        for status in ("completed", "abandoned"):
            with self.subTest(status=status):
                self.run.update(status=status)
                ident = f"unknown-{status}"
                self.add_unknown_dispatch(ident)
                with self.assertRaisesRegex(HarnessError, "无法确认"):
                    self.cleanup()
                result = cleanup_run(self.repo, self.run["id"], confirm_exited=[ident, ident])
                self.assertEqual(result["checkouts"][0]["status"], "eligible")
                self.run = read_json(self.directory / "state.json")
                self.assertEqual(self.run["status"], status)
                self.assertEqual(self.run["schema_version"], RUN_SCHEMA_VERSION)
                self.assertTrue(self.run["dispatches"][-1]["exit_confirmed"])
                self.assertIsNone(self.confirmation_events()[-1]["observed"]["group_alive"])
        self.assertEqual(len(self.confirmation_events()), 2)
        self.assertTrue(self.checkout.exists())
        self.assertEqual(self.artifact.read_bytes(), evidence)

    def test_confirmation_checks_lock_and_entire_id_batch_before_audit_or_mutation(self):
        entry = self.add_unknown_dispatch()
        state = (self.directory / "state.json").read_bytes()
        events = (self.directory / "events.jsonl").read_bytes()
        with held_lock(self.directory / "run.lock"), self.assertRaisesRegex(HarnessError, "运行锁"):
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
        def ledger_rename_fails(real, source, target, **kwargs):  # only the ledger's rename in its locked directory
            if target == "state.json" and kwargs.get("dst_dir_fd") is not None:
                raise OSError(errno.EIO, "state unavailable")
            return real(source, target, **kwargs)
        with patch.object(state_files, "os", FakeOs(replace=ledger_rename_fails)), \
                self.assertRaises(StateReadError) as caught:
            cleanup_run(self.repo, self.run["id"], confirm_exited=[selected["id"]])
        step = caught.exception.outcome.step
        self.assertEqual((step.name, step.stage, caught.exception.outcome.errno), ("state.json", "rename", errno.EIO))
        self.assertEqual(caught.exception.stop, stops.make("runtime_failure", "io", data={"error_type": "OSError"}))
        self.assertEqual((self.directory / "state.json").read_bytes(), before)
        self.assertEqual(self.confirmation_events()[0]["dispatch_id"], selected["id"])
        self.assertTrue(self.checkout.exists())

    def test_cli_terminal_confirmation_needs_no_configuration_and_keeps_file_gates(self):
        selected = self.add_unknown_dispatch()
        unsaved = self.checkout / "operator-data"
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
