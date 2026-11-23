import sys
from unittest.mock import patch

from harness import git
from harness.util import HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_runtime_changes import ScriptedBackend


class SnapshotEfficiencyTests(RepoCase):
    def test_normal_lifecycle_reuses_only_adjacent_snapshots(self):
        engine = self.engine()
        self.assertFalse(self.config.data["verification"]["fresh_checkout"])
        with (patch("harness.git.read_worktree", wraps=git.read_worktree) as snapshots,
              patch.object(git, "prove_image", wraps=git.prove_image) as proofs,
              patch.object(git, "invocation", wraps=git.invocation) as invocations,
              patch.object(engine.services.finalization, "final_review",
                           wraps=engine.services.finalization.final_review) as entries):
            result = engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(result["attempts"].values()), 1)
        record, = result["final_history"]
        self.assertEqual(entries.call_count, 1)
        proofs.assert_called_once_with(engine.worktree, result["expected_head"], record["tree"],
                                       binding=result["worktree_binding"])
        self.assertEqual(record["tree"], git.git(engine.worktree, "rev-parse", "HEAD^{tree}").strip())
        self.assertEqual(read_json(engine.directory / record["artifact"])["tree"], record["tree"])
        commit = result["expected_head"]
        commands = {("rev-parse", "--verify", f"{commit}^{{tree}}"), ("ls-tree", "-r", "-z", commit)}
        reads = [call for call in invocations.call_args_list if call.args[1] in commands]
        self.assertEqual({call.args[1] for call in reads}, commands)
        self.assertEqual(len(reads), 2)
        for call in reads:
            self.assertIs(call.kwargs["effect"], git.READ)
            self.assertEqual(call.kwargs["binding"], result["worktree_binding"])
            self.assertIsNone(call.kwargs["index_file"])
        comparisons = [call.args[1] for call in invocations.call_args_list if call.args[1][0] == "diff-tree"
                       and call.args[1][-3:] == (result["base_commit"], commit, "--")]
        self.assertEqual(len(comparisons), 2)
        self.assertTrue(any("--binary" in call for call in comparisons))
        self.assertTrue(any("--name-only" in call and "-z" in call for call in comparisons))
        # Durable worker return, replay and publication guards add net reads;
        # none may reuse a snapshot across those boundaries.
        self.assertGreater(snapshots.call_count, 0)
        self.assertLessEqual(snapshots.call_count, 15)
        self.assertEqual(result["result"]["commit"], commit)
        self.assertEqual(result["result"]["tree"], record["tree"])

    def test_reused_verification_snapshot_still_detects_changes(self):
        # The last command changes the candidate after the checks have passed.
        self.config.data["verification"]["commands"] = [
            {"cwd": ".", "argv": [sys.executable, "-m", "unittest", "-v"]},
            {"cwd": ".", "argv": [sys.executable, "-c", "open('hello.py', 'a').write('# Changed after checks\\n')"]}]
        engine = self.engine()
        with self.assertRaisesRegex(HarnessError, "验证命令修改"):
            engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        self.assertFalse(engine.run["accepted"])
        self.assertFalse(any(d["role"] == "reviewer" for d in engine.run["dispatches"]))

    def test_blank_commander_instruction_needs_new_dispatch_before_worker(self):
        def change(backend, kwargs, packet, result):
            if kwargs["kind"] == "decide" and "previous_invalid" not in packet:
                result["instruction"] = " "

        plan = read_json(ROOT / "examples" / "plan.json")
        plan["tasks"].append({**plan["tasks"][0], "id": "other"})
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        result = engine.start("Implement greet", plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([c["kind"] for c in backend.calls[:3]], ["decide", "decide", "implement"])
        workers = [c for c in backend.calls if c["role"] == "worker"]
        # The decided worker gets the redone instruction; a later pristine task has none.
        self.assertTrue(workers[0]["packet"]["instruction"].strip())
        self.assertTrue(all(c["packet"]["instruction"] is None for c in workers[1:]))
        calls = [d["id"] for d in result["dispatches"] if d["role"] == "commander"]
        self.assertEqual(len(calls), len(set(calls)))
        selections = [d for d in result["dispatches"] if d.get("kind") == "decide"]
        self.assertEqual((selections[0]["status"], selections[1]["retry_of"]), ("invalid", selections[0]["id"]))
