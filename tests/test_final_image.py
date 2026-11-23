"""Whole-run entry proves both the real index and Git's committable worktree image."""
import os
from pathlib import Path
from unittest.mock import patch

from harness import git, stops
from harness.util import HarnessError, read_json, write_json
from test_final_attempt_switch import after_first_approval
from test_git_invocation import IDENTITY, IsolatedRepoCase, plain_git
from test_runtime_changes import ScriptedBackend
from test_stop_exits import plan


class FinalImageTests(IsolatedRepoCase):
    def attempt(self, edit, *, expected=None, detail=None, listing=None):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        service = engine.services.finalization
        final_review = service.final_review
        observed = []

        def enter(**kwargs):
            worktree = engine.worktree
            diagnostic = edit(worktree)
            binding = engine.run["worktree_binding"]
            index = Path(binding["git_dir"]) / "index"
            before = self.files(worktree, index)
            call_count = len(backend.calls)
            try:
                with patch.object(git, "prove_image", wraps=git.prove_image) as proof:
                    return final_review(**kwargs)
            except HarnessError as error:
                if detail:
                    self.assertEqual(stops.of(error)["data"]["detail"], diagnostic)
                raise
            finally:
                observed.extend(proof.call_args_list)
                self.assertEqual(self.files(worktree, index), before)
                if expected is not None or detail is not None:
                    self.assertEqual(len(backend.calls), call_count)

        read = git.read_worktree

        def inspect(*args, **kwargs):
            snapshot, raw = read(*args, **kwargs)
            if listing is not None and engine.run.get("accepted"):
                raw = listing(raw)
            return snapshot, raw

        with (patch.object(service, "final_review", side_effect=enter),
              patch.object(git, "read_worktree", side_effect=inspect)):
            if expected is None and detail is None:
                self.assertEqual(engine.start("Implement greet", plan())["status"], "completed")
                self.assertEqual(sum(call["kind"] == "final_review" for call in backend.calls), 1)
            else:
                with self.assertRaises(HarnessError) as caught:
                    engine.start("Implement greet", plan())
                stop = stops.of(caught.exception)
                self.assertEqual((stop["kind"], stop["reason"]), ("workspace_mismatch", "dirty_final_workspace"))
                self.assertEqual(stop, read_json(engine.directory / "state.json")["stop"])
                self.assertEqual(engine.run["final_history"], [])
                if expected is not None:
                    self.assertEqual(stop["data"]["detail"], git.describe_paths(sorted(expected)))
                else:
                    self.assertTrue(stop["data"]["detail"])
        proof, = observed
        commit = engine.run["expected_head"]
        tree = plain_git(engine.worktree, "rev-parse", f"{commit}^{{tree}}").stdout.decode().strip()
        self.assertEqual(proof.args, (engine.worktree, commit, tree))
        self.assertEqual(proof.kwargs, {"binding": engine.run["worktree_binding"]})

    def test_hidden_edits_and_index_conflicts_stop_before_review(self):
        def same_time(path):
            original = path.stat()
            data = path.read_bytes()
            path.write_bytes(b"X" + data[1:])
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

        def flag_edit(worktree):
            plain_git(worktree, "update-index", "--assume-unchanged", "hello.py")
            same_time(worktree / "hello.py")

        def conflicts(worktree):
            oid = plain_git(worktree, "rev-parse", "HEAD:hello.py").stdout.strip()
            plain_git(worktree, "update-index", "--index-info",
                      input=b"0 " + b"0" * len(oid) + b"\thello.py\n100644 " + oid + b" 2\thello.py\n")

        for edit in (flag_edit, lambda worktree: same_time(worktree / "hello.py"), conflicts):
            with self.subTest(edit=edit):
                self.attempt(edit, expected=["hello.py"])

    def test_real_index_and_image_differences_are_combined_by_full_path(self):
        staged = "staged\t\n雪"
        untracked = ["untracked\t\n雪", *[f"extra-{i:02}" for i in range(9)]]

        def edit(worktree):
            path = worktree / staged
            path.write_text("only in index")
            plain_git(worktree, "add", "--", staged)
            path.unlink()
            for name in untracked:
                (worktree / name).write_text("only in worktree")
        self.attempt(edit, expected=[staged, *untracked])

    def test_a_duplicate_stage_zero_entry_fails_even_when_each_entry_matches(self):
        def duplicate(raw):
            record = next(row for row in raw.split(b"\0") if row.endswith(b"\thello.py"))
            return raw + record + b"\0"

        self.attempt(lambda worktree: None, listing=duplicate, expected=["hello.py"])

    def test_untracked_symlink_nested_repository_and_exec_bit_are_reported(self):
        def edit(worktree):
            (worktree / "untracked-link").symlink_to("missing")
            nested = worktree / "nested"
            nested.mkdir()
            plain_git(nested, "init", "-q")
            plain_git(nested, *IDENTITY, "commit", "--allow-empty", "-qm", "nested fixture")
            plain_git(worktree, "config", "core.fileMode", "true")
            (worktree / "hello.py").chmod(0o755)
        self.attempt(edit, expected=["hello.py", "nested", "untracked-link"])

    def test_git_ignored_inputs_and_untracked_fifo_still_allow_completion(self):
        def edit(worktree):
            (worktree / "__pycache__").mkdir(exist_ok=True)
            (worktree / "__pycache__" / "ignored").write_text("ignored build output")
            os.mkfifo(worktree / "pipe")
            plain_git(worktree, "config", "core.fileMode", "false")
            (worktree / "hello.py").chmod(0o755)
        self.attempt(edit)

    def test_required_filter_failure_keeps_git_diagnostic(self):
        def edit(worktree):
            self.configure_filter(worktree, "false")
            env = dict(os.environ, GIT_INDEX_FILE=str(self.scratch / "filter-control-index"))
            plain_git(worktree, "read-tree", "HEAD", env=env)
            added = plain_git(worktree, "add", "-A", env=env, check=False)
            self.assertNotEqual(added.returncode, 0)
            return added.stderr.decode("utf-8", errors="replace").splitlines()[0]

        self.attempt(edit, detail=True)

    def test_required_filter_failure_without_a_first_line_persists_exit_diagnostic(self):
        for empty_stderr in (False, True):
            with self.subTest(empty_stderr=empty_stderr):
                self.assert_filter_exit_diagnostic(empty_stderr=empty_stderr)

    def assert_filter_exit_diagnostic(self, *, empty_stderr):
        engine = self.engine(ScriptedBackend())
        service = engine.services.finalization
        final_review = service.final_review
        git_process = git.git_process
        commands = []
        exits = []

        def observe(repo, *args, **kwargs):
            commands.append(args)
            result = git_process(repo, *args, **kwargs)
            if args == ("add", "-A"):
                self.assertIs(kwargs["effect"], git.PRIVATE)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr.startswith(b"\n"), result.stderr)
                self.assertTrue(result.stderr[1:].strip(), result.stderr)
                exits.append(result.returncode)
                if empty_stderr:
                    # Git itself reports the filter failure. Control the returned stderr for this case.
                    result.stderr = b""
            return result

        def enter(**kwargs):
            self.configure_filter(engine.worktree, "sh -c 'echo >&2; exit 1'")
            binding = engine.run["worktree_binding"]
            index = Path(binding["git_dir"]) / "index"
            ref = Path(binding["common_dir"]) / binding["branch"]
            self.assertTrue(index.is_file())
            self.assertTrue(ref.is_file())
            before = self.files(engine.worktree, index, ref)
            try:
                with patch.object(git, "git_process", side_effect=observe):
                    return final_review(**kwargs)
            finally:
                self.assertEqual(self.files(engine.worktree, index, ref), before)

        try:
            with (patch.object(service, "final_review", side_effect=enter),
                  self.assertRaises(HarnessError) as caught):
                engine.start("Implement greet", plan())
        finally:
            (self.repo / ".git" / "info" / "attributes").unlink(missing_ok=True)
        code, = exits
        self.assertEqual(commands[-1], ("add", "-A"))
        self.assertFalse(any(args[0] in ("write-tree", "diff-tree") for args in commands))
        saved = read_json(engine.directory / "state.json")
        stop = stops.of(caught.exception)
        self.assertEqual((stop["kind"], stop["reason"]), ("workspace_mismatch", "dirty_final_workspace"))
        self.assertEqual(saved["stop"], stop)
        self.assertEqual(saved["final_history"], [])
        self.assertEqual(stop["data"]["detail"], f"git add -A exited with status {code}")

    def configure_filter(self, worktree, clean, smudge=None, *, name="hello.py"):
        info = self.repo / ".git" / "info"
        (info / "attributes").write_text(f"{name} filter=finalproof\n")
        plain_git(worktree, "config", "filter.finalproof.clean", clean)
        if smudge is not None:
            plain_git(worktree, "config", "filter.finalproof.smudge", smudge)
        plain_git(worktree, "config", "filter.finalproof.required", "true")

    def test_reversible_filter_uses_git_conversion_even_when_file_bytes_equal_blob(self):
        name = "filtered-data"
        (self.repo / name).write_text("raw bytes\n")
        plain_git(self.repo, "add", "--", name)
        plain_git(self.repo, *IDENTITY, "commit", "-qm", "filter data")

        def configure(worktree):
            converter = "tr a-z n-za-m"
            self.configure_filter(worktree, converter, converter, name=name)

        def checkout(worktree):
            configure(worktree)
            path = worktree / name
            blob = path.read_bytes()
            path.unlink()
            plain_git(worktree, "checkout-index", "--", name)
            self.assertNotEqual(path.read_bytes(), blob)

        self.attempt(checkout)
        # The next run starts with the normal filter-free task inputs as well.
        (self.repo / ".git" / "info" / "attributes").unlink()

        def exact_blob(worktree):
            configure(worktree)
            self.assertEqual((worktree / name).read_bytes(),
                             plain_git(worktree, "cat-file", "blob", f"HEAD:{name}").stdout)

        self.attempt(exact_blob, expected=[name])

    def test_core_autocrlf_accepts_a_commit_that_already_contains_crlf(self):
        path = self.repo / "existing-crlf"
        path.write_bytes(b"committed with CRLF\r\n")
        plain_git(self.repo, "config", "core.autocrlf", "false")
        plain_git(self.repo, "add", "--", path.name)
        plain_git(self.repo, *IDENTITY, "commit", "-qm", "existing CRLF")
        self.attempt(lambda worktree: plain_git(worktree, "config", "core.autocrlf", "true"))

    def test_record_tree_conflicting_with_its_commit_stops_approval_reuse(self):
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with after_first_approval(), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        state = engine.directory / "state.json"
        ledger = read_json(state)
        record, = ledger["final_history"]
        record["tree"] = plain_git(engine.worktree, "rev-parse", ledger["base_commit"] + "^{tree}").stdout.decode().strip()
        write_json(state, ledger)
        model_calls = len(backend.calls)
        with patch.object(git, "prove_image", wraps=git.prove_image) as proof, self.assertRaises(HarnessError) as caught:
            self.engine(backend).resume(ledger["id"])
        proof.assert_not_called()
        self.assertEqual(len(backend.calls), model_calls)
        stop = stops.of(caught.exception)
        self.assertEqual((stop["kind"], stop["reason"]), ("evidence", "runtime_integrity"))
        self.assertEqual(stop["source"], {"artifact": record["artifact"]})
        saved = read_json(state)
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(saved["final_history"], ledger["final_history"])
