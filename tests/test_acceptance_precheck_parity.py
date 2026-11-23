"""Ordinary saved-intent outcomes through both acceptance readers."""
import errno
import os
import subprocess
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch

from harness import acceptance, git, stops
from harness.util import HarnessError, read_json, write_json
from test_git_invocation import IDENTITY, plain_git
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend


class AcceptancePrecheckParityTests(RepoCase):
    def hold(self, *, zero_diff=False):
        if zero_diff:
            (self.repo / "hello.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n')
            plain_git(self.repo, "add", "--", "hello.py")
            plain_git(self.repo, *IDENTITY, "commit", "-qm", "Already implemented")
        backend = ScriptedBackend()
        engine = self.engine(backend)
        pause = HarnessError("Fixture pause after saving the intent",
                             stop=stops.make("runtime_failure", "invariant"))
        with patch.object(acceptance, "state", side_effect=pause), self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertIs(caught.exception, pause)
        engine.load_run(engine.run["id"])
        pending = engine.run["pending_acceptance"]
        self.assertEqual(pending["task_id"], "greet")
        self.assertEqual(engine.run["accepted"], {})
        self.assertNotIn("pending_post_accept", engine.run)
        self.assertEqual(zero_diff, pending["intent"]["parent"] == pending["intent"]["commit"])
        return engine, backend, pending["intent"]

    def git_snapshot(self, engine, *, objects=True):
        binding = engine.run["worktree_binding"]
        common = Path(binding["common_dir"])
        # Raw metadata remains readable even when native status cannot open HEAD or its tree.
        return (
            {path.relative_to(common): path.read_bytes()
             for path in common.rglob("*")
             if path.is_file() and (objects or path.relative_to(common).parts[0] != "objects")},
            {path.relative_to(engine.worktree): path.read_bytes()
             for path in engine.worktree.iterdir() if path.is_file()},
        )

    def evidence_snapshot(self, engine):
        root = engine.directory
        return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def read_pair(self, engine, backend, *, expected=None, fault=nullcontext):
        before_git = self.git_snapshot(engine)
        before_evidence = self.evidence_snapshot(engine)
        before_calls = len(backend.calls)
        ident = engine.run["id"]

        def assert_unchanged():
            self.assertEqual(self.evidence_snapshot(engine), before_evidence)
            self.assertEqual(self.git_snapshot(engine), before_git)
            self.assertEqual(len(backend.calls), before_calls)

        def observe(operation):
            with fault(), self.assertRaises(HarnessError) as caught:
                operation()
            actual = caught.exception.stop
            self.assertEqual(actual, expected)
            return actual

        def check():
            wake_checks = engine.services.lifecycle.wake_checks
            accept_pending = engine.services.tasks.accept_pending
            if expected is None:
                self.assertIsNone(wake_checks())
                assert_unchanged()
                self.assertIs(accept_pending(), True)
                return
            precheck_stop = observe(wake_checks)
            assert_unchanged()
            acceptance_stop = observe(accept_pending)
            assert_unchanged()
            self.assertEqual(precheck_stop, acceptance_stop)

        engine.context.run_lock(stops.make("busy", "run_lock"), check)
        self.assertEqual(len(backend.calls), before_calls)
        if expected is not None:
            public = self.engine(backend)
            checks = []
            preflight = public.services.lifecycle.verify_head_and_inputs

            def inspect_preflight():
                try:
                    return preflight()
                finally:
                    assert_unchanged()
                    checks.append(True)

            with patch.object(public.services.lifecycle, "verify_head_and_inputs", inspect_preflight):
                self.assertEqual(observe(lambda: public.resume(ident)), expected)
            self.assertEqual(checks, [True])
            self.assertEqual(self.git_snapshot(engine), before_git)
            # A public preflight refusal is settled and returned. Its acceptance and
            # decision ledger fields must survive even when stop evidence is published.
            prior = engine.run
            saved = read_json(engine.directory / "state.json")
            for field in ("pending_acceptance", "accepted", "expected_head", "dispatches",
                          "current_task", "decision_requests", "operator_decisions"):
                self.assertEqual(saved[field], prior[field], field)
            self.assertNotIn("pending_post_accept", saved)
            self.assertEqual(len(backend.calls), before_calls)

    def check_completed(self, engine, backend, intent):
        calls_before = len(backend.calls)
        result = self.engine(backend).resume(engine.run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("pending_acceptance", result)
        self.assertNotIn("pending_post_accept", result)
        self.assertEqual(result["expected_head"], intent["commit"])
        self.assertEqual(result["accepted"]["greet"]["commit"], intent["commit"])
        self.assertEqual(result["accepted"]["greet"]["tree"], intent["tree"])
        self.assertEqual(plain_git(engine.worktree, "rev-parse", "HEAD").stdout.decode().strip(), intent["commit"])
        self.assertEqual(git.index_digest(engine.worktree, binding=result["worktree_binding"]), intent["index_synced"])
        self.assertEqual(result["decision_requests"], [])
        self.assertEqual(result["operator_decisions"], [])
        self.assertEqual([(call["role"], call["kind"]) for call in backend.calls[calls_before:]],
                         [("reviewer", "final_review")])

    def check_other_head(self, *, zero_diff):
        engine, backend, intent = self.hold(zero_diff=zero_diff)
        cwd = engine.worktree
        (cwd / "extra.txt").write_text("An independent change\n")
        plain_git(cwd, "add", "--", "extra.txt")
        plain_git(cwd, *IDENTITY, "commit", "-qm", "Independent change")
        head = plain_git(cwd, "rev-parse", "HEAD").stdout.decode().strip()
        self.assertNotIn(head, (intent["parent"], intent["commit"]))
        data = {"expected_head": intent["parent"]}
        if not zero_diff:
            data["accept_commit"] = intent["commit"]
        self.read_pair(engine, backend, expected={
            "kind": "workspace_mismatch", "reason": "head_changed", "source": {"task_id": "greet"}, "data": data,
        })

    def test_other_head_with_reviewed_changes(self):
        self.check_other_head(zero_diff=False)

    def test_other_head_with_zero_difference(self):
        self.check_other_head(zero_diff=True)

    def check_staged_other_file(self, *, branch_at_commit):
        engine, backend, intent = self.hold()
        cwd = engine.worktree
        if branch_at_commit:
            plain_git(cwd, "reset", "--mixed", intent["commit"])
        expected_head = intent["commit" if branch_at_commit else "parent"]
        self.assertEqual(plain_git(cwd, "rev-parse", "HEAD").stdout.decode().strip(), expected_head)
        extra = cwd / "extra.txt"
        extra.write_text("Staged after the approved intent\n")
        plain_git(cwd, "add", "--", "extra.txt")
        self.assertTrue(plain_git(cwd, "ls-files", "--stage", "--", "extra.txt").stdout)
        self.read_pair(engine, backend, expected={
            "kind": "workspace_mismatch", "reason": "candidate_changed", "source": {"task_id": "greet"}, "data": {},
        })
        # Normalize only this disposable fixture to the reviewed tree.
        plain_git(cwd, "read-tree", "--reset", intent["commit"])
        extra.unlink()
        self.check_completed(engine, backend, intent)

    def test_staged_other_file_at_parent(self):
        self.check_staged_other_file(branch_at_commit=False)

    def test_staged_other_file_at_accept_commit(self):
        self.check_staged_other_file(branch_at_commit=True)

    def test_single_g10_eagain_then_normal_resume(self):
        engine, backend, intent = self.hold()
        state, launch = acceptance.state, git._launch

        @contextmanager
        def fail_g10():
            injection = OSError(errno.EAGAIN, "Fixture G10 launch unavailable")
            matched = []

            def run(argv, env, **kwargs):
                if argv[-3:] == ["symbolic-ref", "-q", "HEAD"] and not matched:
                    self.assertIs(kwargs["effect"], git.READ)
                    matched.append(argv)
                    raise injection
                return launch(argv, env, **kwargs)

            def read_intent(*args):
                with patch.object(git, "_launch", run):
                    try:
                        return state(*args)
                    except git.GitLaunchError as error:
                        self.assertIs(error.__cause__, injection)
                        raise

            with patch.object(acceptance, "state", read_intent):
                yield
            self.assertEqual(len(matched), 1)

        self.read_pair(engine, backend, fault=fail_g10, expected={
            "kind": "runtime_failure", "reason": "io", "source": {}, "data": {},
        })
        self.check_completed(engine, backend, intent)

    def test_healthy_intent_passes_both_readers(self):
        engine, backend, intent = self.hold()
        self.read_pair(engine, backend)
        saved = read_json(engine.directory / "state.json")
        self.assertNotIn("pending_acceptance", saved)
        self.assertEqual(saved["accepted"]["greet"]["commit"], intent["commit"])
        self.assertEqual(saved["expected_head"], intent["commit"])
        self.check_completed(engine, backend, intent)

    def fixture_oid(self, engine, revision):
        return plain_git(engine.worktree, "rev-parse", "--verify", revision).stdout.decode().strip()

    def object_snapshot(self, engine, objects):
        contents = {}
        for oid, kind in objects.items():
            cwd = engine.worktree
            self.assertEqual(plain_git(cwd, "cat-file", "-t", oid).stdout.decode().strip(), kind)
            contents[oid] = plain_git(cwd, "cat-file", "-p", oid).stdout
        return contents

    def check_existing_objects(self, engine, backend, objects, *, expected, fault=nullcontext):
        before = self.object_snapshot(engine, objects)
        with patch.object(git, "build_commit", wraps=git.build_commit) as build, \
                patch.object(acceptance, "move_branch", wraps=acceptance.move_branch) as move, \
                patch.object(acceptance, "sync_index", wraps=acceptance.sync_index) as sync:
            self.read_pair(engine, backend, expected=expected, fault=fault)
        build.assert_not_called()
        move.assert_not_called()
        sync.assert_not_called()
        self.assertEqual(self.object_snapshot(engine, objects), before)

    def check_existing_object_mismatch(self, engine, backend, intent, field, oid, kind):
        self.assertNotEqual(oid, intent[field])
        objects = {intent["parent"]: "commit", intent["commit"]: "commit",
                   intent["tree"]: "tree", oid: kind}
        saved = read_json(engine.directory / "state.json")
        saved["pending_acceptance"]["intent"][field] = oid
        write_json(engine.directory / "state.json", saved)
        engine.load_run(saved["id"])
        self.assertEqual(engine.run["pending_acceptance"], saved["pending_acceptance"])
        self.check_existing_objects(engine, backend, objects, expected={
            "kind": "evidence", "reason": "runtime_integrity", "source": {"task_id": "greet"}, "data": {},
        })

    def test_saved_commit_is_existing_blob(self):
        engine, backend, intent = self.hold()
        blob = self.fixture_oid(engine, intent["commit"] + ":hello.py")
        self.check_existing_object_mismatch(engine, backend, intent, "commit", blob, "blob")

    def test_zero_diff_tree_is_existing_blob(self):
        engine, backend, intent = self.hold(zero_diff=True)
        blob = self.fixture_oid(engine, intent["parent"] + ":hello.py")
        self.check_existing_object_mismatch(engine, backend, intent, "tree", blob, "blob")

    def test_zero_diff_tree_is_different_existing_tree(self):
        engine, backend, intent = self.hold(zero_diff=True)
        self.assertEqual(self.fixture_oid(engine, intent["parent"] + "^{tree}"), intent["tree"])
        tree = self.fixture_oid(engine, intent["parent"] + "^1^{tree}")
        self.check_existing_object_mismatch(engine, backend, intent, "tree", tree, "tree")

    def test_saved_commit_has_different_existing_parent(self):
        engine, backend, intent = self.hold()
        commit = plain_git(engine.worktree, *IDENTITY, "commit-tree", intent["tree"],
                           "-p", intent["commit"], "-m", "Independent fixture commit").stdout.decode().strip()
        self.assertEqual(self.fixture_oid(engine, commit + "^{tree}"), intent["tree"])
        self.assertEqual(plain_git(engine.worktree, "rev-list", "--parents", "-n", "1", commit).stdout.split(),
                         [commit.encode(), intent["commit"].encode()])
        self.assertNotEqual(intent["commit"], intent["parent"])
        self.check_existing_object_mismatch(engine, backend, intent, "commit", commit, "commit")

    def test_batch_check_failure_then_normal_resume(self):
        engine, backend, intent = self.hold()
        launch = git._launch
        lookup_input = "".join(intent[key] + "\n" for key in ("parent", "tree", "commit")).encode()

        @contextmanager
        def fail_lookup():
            matched = []

            def run(argv, env, **kwargs):
                if argv[-2:] == ["cat-file", "--batch-check"] and kwargs["input"] == lookup_input and not matched:
                    self.assertIs(kwargs["effect"], git.READ)
                    matched.append(argv)
                    return subprocess.CompletedProcess(argv, 1, b"", b"Fixture object lookup failed")
                return launch(argv, env, **kwargs)

            with patch.object(git, "_launch", run):
                yield
            self.assertEqual(len(matched), 1)

        self.check_existing_objects(engine, backend, {
            intent["parent"]: "commit", intent["tree"]: "tree", intent["commit"]: "commit",
        }, fault=fail_lookup, expected={
            "kind": "runtime_failure", "reason": "io", "source": {}, "data": {},
        })
        self.check_completed(engine, backend, intent)

    def test_unreachable_saved_commit_pruned_then_rebuilt_on_resume(self):
        engine, backend, intent = self.hold()
        cwd = engine.worktree
        parent, tree, candidate = (intent[field] for field in ("parent", "tree", "commit"))
        common = Path(engine.run["worktree_binding"]["common_dir"])
        self.assertEqual(common, (self.repo / ".git").resolve())
        self.assertEqual(self.fixture_oid(engine, "HEAD"), parent)
        self.assertEqual(self.fixture_oid(engine, candidate + "^{tree}"), tree)
        self.assertEqual(plain_git(cwd, "rev-list", "--parents", "-n", "1", candidate).stdout.split(),
                         [candidate.encode(), parent.encode()])

        # Anchor the reviewed tree without making the unpublished candidate reachable.
        keeper = plain_git(self.repo, *IDENTITY, "commit-tree", tree, "-p", parent,
                           "-m", "Keep the reviewed tree for the prune fixture").stdout.decode().strip()
        self.assertNotEqual(keeper, candidate)
        plain_git(self.repo, "update-ref", "refs/heads/keep-reviewed-tree", keeper)
        retained = self.object_snapshot(engine, {parent: "commit", tree: "tree", keeper: "commit"})
        reachable = {line.split(b" ", 1)[0] for line in
                     plain_git(self.repo, "rev-list", "--objects", "--all", "--reflog").stdout.splitlines()}
        self.assertTrue({parent.encode(), tree.encode(), keeper.encode()} <= reachable)
        self.assertNotIn(candidate.encode(), reachable)
        fsck = plain_git(self.repo, "fsck", "--unreachable", env={**os.environ, "LC_ALL": "C"})
        self.assertIn(f"unreachable commit {candidate}".encode(), fsck.stdout.splitlines())

        def git_metadata():
            return {path.relative_to(common): path.read_bytes()
                    for path in common.rglob("*") if path.is_file()}

        def check_read_only(expected):
            before = (self.git_snapshot(engine), self.evidence_snapshot(engine), git_metadata())
            before_calls = len(backend.calls)
            with patch.object(git, "build_commit", wraps=git.build_commit) as build:
                self.assertIsInstance(acceptance.state(engine.context, intent), expected)
                engine.context.run_lock(stops.make("busy", "run_lock"), engine.services.lifecycle.wake_checks)
            build.assert_not_called()
            self.assertEqual((self.git_snapshot(engine), self.evidence_snapshot(engine), git_metadata()), before)
            self.assertEqual(len(backend.calls), before_calls)

        check_read_only(acceptance.Ready)
        before_prune = (self.git_snapshot(engine, objects=False), self.evidence_snapshot(engine))
        dry_run = plain_git(self.repo, "prune", "--dry-run", "--expire=now")
        self.assertIn(f"{candidate} commit".encode(), dry_run.stdout.splitlines())
        plain_git(self.repo, "prune", "--expire=now")
        lookup = plain_git(cwd, "cat-file", "--batch-check", input=(candidate + "\n").encode())
        self.assertEqual(lookup.stdout, f"{candidate} missing\n".encode())
        self.assertEqual(self.object_snapshot(engine, {parent: "commit", tree: "tree", keeper: "commit"}), retained)
        self.assertEqual((self.git_snapshot(engine, objects=False), self.evidence_snapshot(engine)), before_prune)
        check_read_only(acceptance.Rebuild)

        pending = read_json(engine.directory / "state.json")["pending_acceptance"]
        before_calls = len(backend.calls)
        resumed = self.engine(backend)
        move_branch = acceptance.move_branch
        transitions = []

        def observe_published(context, rebuilt, task_id):
            saved = read_json(engine.directory / "state.json")
            self.assertEqual(saved["pending_acceptance"], {**pending, "intent": rebuilt})
            self.assertEqual(saved["accepted"], {})
            self.assertEqual(saved["expected_head"], parent)
            self.assertEqual(self.fixture_oid(engine, "HEAD"), parent)
            self.assertEqual(rebuilt, {**intent, "commit": rebuilt["commit"]})
            transitions.append("intent")
            return move_branch(context, rebuilt, task_id)

        finish_post_accept = resumed.services.tasks.finish_post_accept

        def observe_successor():
            saved = read_json(engine.directory / "state.json")
            if "pending_post_accept" in saved:
                self.assertNotIn("pending_acceptance", saved)
                self.assertEqual(saved["pending_post_accept"]["payload"]["commit"], saved["expected_head"])
                self.assertEqual(saved["accepted"]["greet"]["tree"], tree)
                transitions.append("successor")
            return finish_post_accept()

        with patch.object(git, "build_commit", wraps=git.build_commit) as build, \
                patch.object(acceptance, "move_branch", side_effect=observe_published) as move, \
                patch.object(resumed.services.tasks, "finish_post_accept", side_effect=observe_successor):
            result = resumed.resume(engine.run["id"])
        build.assert_called_once()
        move.assert_called_once()
        self.assertEqual(transitions, ["intent", "successor"])
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("pending_acceptance", result)
        self.assertNotIn("pending_post_accept", result)
        accepted = result["accepted"]["greet"]
        self.assertEqual(accepted["commit"], self.fixture_oid(engine, "HEAD"))
        self.assertEqual(accepted["commit"], result["expected_head"])
        self.assertEqual(self.fixture_oid(engine, accepted["commit"] + "^{tree}"), tree)
        self.assertEqual(plain_git(cwd, "rev-list", "--parents", "-n", "1", accepted["commit"]).stdout.split(),
                         [accepted["commit"].encode(), parent.encode()])
        for field in ("review", "review_artifact", "review_source", "verification", "worker_source"):
            self.assertEqual(accepted[field], pending[field])
        self.assertEqual(git.index_digest(cwd, binding=result["worktree_binding"]), intent["index_synced"])
        self.assertEqual([(call["role"], call["kind"]) for call in backend.calls[before_calls:]],
                         [("reviewer", "final_review")])

    def test_deleted_run_ref_keeps_symbolic_head_binding(self):
        engine, backend, intent = self.hold()
        plain_git(self.repo, "update-ref", "-d", intent["ref"], intent["parent"])
        self.assertEqual(plain_git(engine.worktree, "symbolic-ref", "HEAD").stdout.strip(),
                         intent["ref"].encode())
        self.assertNotEqual(plain_git(engine.worktree, "rev-parse", "--verify", "-q",
                                      intent["ref"], check=False).returncode, 0)
        self.read_pair(engine, backend, expected=stops.make(
            "workspace_mismatch", "head_changed", source={"task_id": "greet"},
            data={"expected_head": intent["parent"], "accept_commit": intent["commit"]}))

    def test_run_ref_now_points_symbolically_to_main(self):
        engine, backend, intent = self.hold()
        main = self.fixture_oid(engine, "refs/heads/main")
        plain_git(self.repo, "symbolic-ref", intent["ref"], "refs/heads/main")
        self.assertEqual(plain_git(engine.worktree, "symbolic-ref", "HEAD").stdout.strip(),
                         b"refs/heads/main")
        self.read_pair(engine, backend, expected=stops.make("workspace_mismatch", "binding_changed"))
        self.assertEqual(self.fixture_oid(engine, "refs/heads/main"), main)

    def loose_object(self, engine, oid):
        return Path(engine.run["worktree_binding"]["common_dir"]) / "objects" / oid[:2] / oid[2:]

    @contextmanager
    def temporarily_change_object(self, engine, oid, *, truncate=False):
        loose = self.loose_object(engine, oid)
        data = loose.read_bytes()
        mode = loose.stat().st_mode
        try:
            if truncate:
                loose.chmod(0o644)
                loose.write_bytes(data[:-6])
            else:
                loose.unlink()
            yield
        finally:
            if loose.exists():
                loose.chmod(0o644)
            loose.write_bytes(data)
            loose.chmod(mode)

    def verify_missing(self, engine, oid):
        lookup = plain_git(engine.worktree, "cat-file", "--batch-check", input=(oid + "\n").encode())
        self.assertEqual(lookup.stdout, f"{oid} missing\n".encode())

    def check_required_objects_missing(self, missing, *, moved=False, zero_diff=False):
        engine, backend, intent = self.hold(zero_diff=zero_diff)
        if moved:
            plain_git(self.repo, "update-ref", intent["ref"], intent["commit"], intent["parent"])
            plain_git(engine.worktree, "read-tree", "--reset", intent["commit"])
        if moved or zero_diff:
            self.assertEqual(git.index_digest(engine.worktree, binding=engine.run["worktree_binding"]),
                             intent["index_synced"])
        with ExitStack() as restorations:
            for kind in missing:
                restorations.enter_context(self.temporarily_change_object(engine, intent[kind]))
                self.verify_missing(engine, intent[kind])
            self.assertEqual(self.fixture_oid(engine, "HEAD"), intent["commit" if moved else "parent"])
            with patch.object(git, "build_commit", wraps=git.build_commit) as build:
                self.read_pair(engine, backend, expected=stops.make(
                    "evidence", "runtime_integrity", source={"task_id": "greet"}))
            build.assert_not_called()

    def test_reachable_or_required_object_missing(self):
        cases = (
            ("at_commit_missing_tree", True, ("tree",)),
            ("at_commit_missing_commit", True, ("commit",)),
            ("at_parent_missing_parent", False, ("parent",)),
            ("at_commit_missing_parent", True, ("parent",)),
            ("at_parent_missing_both_commits", False, ("parent", "commit")),
        )
        for name, moved, missing in cases:
            with self.subTest(name=name):
                self.check_required_objects_missing(missing, moved=moved)

    def test_zero_difference_requires_parent_tree_object(self):
        self.check_required_objects_missing(("tree",), zero_diff=True)

    def test_unpublished_missing_object_rebuilds_at_both_entries(self):
        for missing, direct in (("tree", False), ("tree", True), ("commit", True)):
            with self.subTest(missing=missing, direct=direct), ExitStack() as restorations:
                engine, backend, intent = self.hold()
                pending = engine.run["pending_acceptance"]
                self.assertEqual(git.commit_header(engine.worktree, intent["commit"],
                                                   binding=engine.run["worktree_binding"]).tree, intent["tree"])
                restorations.enter_context(self.temporarily_change_object(engine, intent[missing]))
                self.verify_missing(engine, intent[missing])
                self.assertEqual(self.fixture_oid(engine, "HEAD"), intent["parent"])
                before = (self.git_snapshot(engine), self.evidence_snapshot(engine))
                calls = len(backend.calls)
                with patch.object(git, "build_commit", wraps=git.build_commit) as build:
                    engine.context.run_lock(stops.make("busy", "run_lock"), engine.services.lifecycle.wake_checks)
                    self.assertEqual((self.git_snapshot(engine), self.evidence_snapshot(engine)), before)
                    build.assert_not_called()
                    if direct:
                        self.assertTrue(engine.context.run_lock(
                            stops.make("busy", "run_lock"), engine.services.tasks.accept_pending))
                    else:
                        self.assertEqual(self.engine(backend).resume(engine.run["id"])["status"], "completed")
                    build.assert_called_once()
                saved = read_json(engine.directory / "state.json")
                accepted = saved["accepted"]["greet"]
                self.assertNotIn("pending_acceptance", saved)
                self.assertEqual(accepted["tree"], intent["tree"])
                self.assertEqual(self.fixture_oid(engine, "HEAD^{tree}"), intent["tree"])
                self.assertEqual(accepted["commit"], self.fixture_oid(engine, "HEAD"))
                self.assertEqual(accepted["review_source"], pending["review_source"])
                self.assertEqual([(call["role"], call["kind"]) for call in backend.calls[calls:]],
                                 [] if direct else [("reviewer", "final_review")])

    def test_saved_commit_header_points_to_a_blob(self):
        engine, backend, intent = self.hold()
        blob = self.fixture_oid(engine, intent["commit"] + ":hello.py")
        header = (f"tree {blob}\nparent {intent['parent']}\nauthor Fixture <fixture@example.test> 0 +0000\n"
                  "committer Fixture <fixture@example.test> 0 +0000\n\nWrong object type\n").encode()
        commit = plain_git(self.repo, "hash-object", "-w", "-t", "commit", "--literally",
                           "--stdin", input=header).stdout.decode().strip()
        self.assertEqual(plain_git(self.repo, "cat-file", "commit", commit).stdout, header)
        ledger = engine.directory / "state.json"
        saved = read_json(ledger)
        saved["pending_acceptance"]["intent"].update(commit=commit, tree=blob)
        write_json(ledger, saved)
        engine.load_run(saved["id"])
        self.read_pair(engine, backend, expected=stops.make(
            "evidence", "runtime_integrity", source={"task_id": "greet"}))

    def test_loose_object_truncated_after_git_can_still_read_its_type(self):
        # An ordinary larger tree keeps enough of the compressed header for batch-check.
        for number in range(40):
            (self.repo / f"fixture-{number}.txt").write_text(f"{number}\n")
        plain_git(self.repo, "add", "--", ".")
        plain_git(self.repo, *IDENTITY, "commit", "-qm", "Tracked fixture files")
        for kind, object_type in (("tree", "tree"), ("parent", "commit")):
            with self.subTest(kind=kind), ExitStack() as restorations:
                engine, backend, intent = self.hold()
                if kind == "parent":
                    restorations.enter_context(self.temporarily_change_object(engine, intent["commit"]))
                    self.verify_missing(engine, intent["commit"])
                oid = intent[kind]
                restorations.enter_context(self.temporarily_change_object(engine, oid, truncate=True))
                lookup = plain_git(engine.worktree, "cat-file", "--batch-check", input=(oid + "\n").encode())
                self.assertEqual(lookup.stdout.split()[:2], [oid.encode(), object_type.encode()])
                failed = plain_git(engine.worktree, "cat-file", object_type, oid, check=False)
                self.assertNotEqual(failed.returncode, 0)
                with patch.object(git, "build_commit", wraps=git.build_commit) as build:
                    self.read_pair(engine, backend, expected=stops.make("runtime_failure", "io"))
                build.assert_not_called()

    def test_acceptance_confirmation_refuses_concurrent_head_or_index_change(self):
        for changed in ("head", "index"):
            with self.subTest(changed=changed):
                engine, backend, intent = self.hold()
                ledger = engine.directory / "state.json"
                pending = engine.run["pending_acceptance"]
                confirmed_scene = []
                confirm = acceptance.confirm
                calls = len(backend.calls)

                def change_before_confirm(context, current, task_id):
                    cwd = engine.worktree
                    self.assertEqual(current, intent)
                    self.assertEqual(self.fixture_oid(engine, "HEAD"), intent["commit"])
                    self.assertEqual(git.index_digest(cwd, binding=context.run["worktree_binding"]),
                                     intent["index_synced"])
                    if changed == "head":
                        other = plain_git(self.repo, *IDENTITY, "commit-tree", intent["tree"],
                                          "-p", intent["commit"], "-m", "Concurrent commit").stdout.decode().strip()
                        plain_git(self.repo, "update-ref", intent["ref"], other, intent["commit"])
                    else:
                        (cwd / "extra.txt").write_text("Stage after acceptance sync\n")
                        plain_git(cwd, "add", "--", "extra.txt")
                    confirmed_scene.append(self.git_snapshot(engine))
                    return confirm(context, current, task_id)

                with patch.object(acceptance, "confirm", change_before_confirm), self.assertRaises(HarnessError) as caught:
                    self.engine(backend).resume(engine.run["id"])
                expected = (stops.make(
                    "workspace_mismatch", "head_changed", source={"task_id": "greet"},
                    data={"expected_head": intent["parent"], "accept_commit": intent["commit"]})
                    if changed == "head" else stops.make(
                        "workspace_mismatch", "candidate_changed", source={"task_id": "greet"}))
                self.assertEqual(caught.exception.stop, expected)
                self.assertEqual(confirmed_scene, [self.git_snapshot(engine)])
                saved = read_json(ledger)
                self.assertEqual(saved["pending_acceptance"], pending)
                self.assertEqual(saved["expected_head"], intent["parent"])
                self.assertEqual(saved["accepted"], {})
                self.assertNotIn("pending_post_accept", saved)
                self.assertEqual(len(backend.calls), calls)

    def test_ref_launch_symbolic_race_keeps_main_during_full_acceptance(self):
        launch = git._launch
        for main_at_parent in (True, False):
            with self.subTest(main_at_parent=main_at_parent):
                engine, backend, intent = self.hold()
                main_ref = "refs/heads/main"
                main = intent["parent"]
                if not main_at_parent:
                    main_tree = self.fixture_oid(engine, main + "^{tree}")
                    main = plain_git(self.repo, *IDENTITY, "commit-tree", main_tree,
                                     "-p", intent["parent"], "-m", "Independent main commit").stdout.decode().strip()
                    plain_git(self.repo, "update-ref", main_ref, main, intent["parent"])
                native_status = []

                def replace_ref_at_launch(argv, env, *, effect, **kwargs):
                    if effect is not git.REFS or "update-ref" not in argv:
                        return launch(argv, env, effect=effect, **kwargs)
                    plain_git(self.repo, "symbolic-ref", intent["ref"], main_ref)
                    result = launch(argv, env, effect=effect, **kwargs)
                    native_status.append(result.returncode)
                    return result

                with patch.object(git, "_launch", replace_ref_at_launch):
                    if main_at_parent:
                        self.check_completed(engine, backend, intent)
                        self.assertEqual(plain_git(self.repo, "symbolic-ref", "-q",
                                                   intent["ref"], check=False).returncode, 1)
                        self.assertEqual(native_status, [0])
                    else:
                        pending = engine.run["pending_acceptance"]
                        calls = len(backend.calls)
                        with self.assertRaises(HarnessError) as caught:
                            self.engine(backend).resume(engine.run["id"])
                        self.assertEqual(caught.exception.stop, stops.make("workspace_mismatch", "binding_changed"))
                        self.assertEqual(len(native_status), 1)
                        self.assertNotEqual(native_status[0], 0)
                        saved = read_json(engine.directory / "state.json")
                        self.assertEqual(saved["pending_acceptance"], pending)
                        self.assertEqual(saved["accepted"], {})
                        self.assertNotIn("pending_post_accept", saved)
                        self.assertEqual(len(backend.calls), calls)
                        self.assertEqual(plain_git(self.repo, "symbolic-ref", intent["ref"]).stdout.strip(),
                                         main_ref.encode())
                self.assertEqual(self.fixture_oid(engine, main_ref), main)
