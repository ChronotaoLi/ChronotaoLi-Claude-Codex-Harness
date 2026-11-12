"""Interruption at the decide landing, the archive/reset/publish sequence and the ask landing.

The kill mode writes the boundary save, then fails every later write of the
process, as SIGKILL would: no in-memory state can repair a split save.
"""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.recovery import snapshot_digest
from harness.util import HarnessError, read_json
from test_decide import (KEY, DecideCase, Script, ask, block_first, blocked_by_task, describe, implement,
                         revise, task)


class Killed:
    """Write the first state save that ``boundary(old, new)`` marks, then kill the process."""

    def __init__(self, boundary=None, target=None):
        self.boundary, self.target = boundary, target
        self.killed = False
        self.original = Path.replace

    def replace(self, source, destination):
        if self.killed:
            raise OSError("the killed process cannot write")
        destination = Path(destination)
        hit = False
        if self.target is not None and destination.name == self.target:
            hit = True
        elif self.boundary is not None and destination.name == "state.json" and destination.exists():
            hit = self.boundary(json.loads(destination.read_text()), json.loads(Path(source).read_text()))
        result = self.original(source, destination)
        if hit:
            self.killed = True
            raise KeyboardInterrupt()
        return result

    def __enter__(self):
        self.patch = patch.object(Path, "replace", new=lambda source, destination: self.replace(source, destination))
        self.patch.__enter__()
        return self

    def __exit__(self, *exc):
        self.patch.__exit__(*exc)
        return False


def consumed(old, new):
    """The save that removes the decide receipt or adds what a decide lands."""
    return ((KEY in old["commander_receipts"] and KEY not in new["commander_receipts"])
            or ("pending_revision" in new and "pending_revision" not in old)
            or len(new["decision_requests"]) > len(old["decision_requests"]))


class LandingKillTests(DecideCase):
    def run_killed(self, script, plan, boundary=consumed, target=None):
        engine = self.engine(script)
        with Killed(boundary, target) as killed, self.assertRaises(BaseException):
            engine.start("landing", plan)
        self.assertTrue(killed.killed)
        return engine, self.state(engine)

    def test_revise_landing_is_one_save(self):
        def decide(script, trigger, packet):
            if trigger["kind"] == "task" and blocked_by_task(trigger["feedback"]):
                return revise(selected="a", instruction="continue a", updates=[describe("a")])
            return None
        script = Script(decide, block_first("a"))
        engine, disk = self.run_killed(script, {"tasks": [task("a")]})
        # Either nothing landed, or the receipt is consumed and the whole reference is there.
        self.assertNotIn(KEY, disk["commander_receipts"])
        pending = disk["pending_revision"]
        self.assertEqual(set(pending), {"revision", "artifact", "artifact_digest"})
        self.assertEqual(read_json(Path(pending["artifact"]))["directive"]["selected_task"], "a")
        result = self.engine(script).resume(disk["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(script.of("commander", "decide")), 1)
        self.assertEqual(len(result["plan_history"]), 2)

    def test_implement_landing_is_one_save(self):
        script = Script(lambda s, t, p: implement("b") if t["kind"] == "task" and t["ready"] == ["a", "b"] else None)
        engine, disk = self.run_killed(script, {"tasks": [task("a"), task("b")]})
        self.assertNotIn(KEY, disk["commander_receipts"])
        self.assertEqual(disk["current_task"], "b")
        self.assertEqual(disk["pending_directive"]["directive"],
                         {"selected_task": "b", "action": "implement", "instruction": "do b"})
        result = self.engine(script).resume(disk["id"], step=True)
        self.assertEqual(set(result["accepted"]), {"b"})
        self.assertEqual(len(script.of("commander", "decide")), 1)
        self.assertEqual([c["task_id"] for c in script.of("worker")], ["b"])

    def test_ask_landing_is_one_save(self):
        script = Script(lambda s, t, p: ask() if t["kind"] == "task" and blocked_by_task(t["feedback"]) else None,
                        block_first("a"))
        engine, disk = self.run_killed(script, {"tasks": [task("a")]})
        self.assertNotIn(KEY, disk["commander_receipts"])
        self.assertEqual([r["kind"] for r in disk["decision_requests"]], ["commander"])
        entry = next(d for d in disk["dispatches"] if d["role"] == "commander")
        self.assertEqual(disk["decision_requests"][0]["source"]["dispatch_id"], entry["id"])
        with self.assertRaisesRegex(HarnessError, "等待操作者决定：task_id=a"):
            self.engine(script).resume(disk["id"])
        self.assertEqual(len(self.state(engine)["decision_requests"]), 1)
        self.assertEqual(len(script.of("commander", "decide")), 1)


class RevisionArchiveTests(DecideCase):
    def archive_script(self):
        def decide(script, trigger, packet):
            if trigger["kind"] == "task" and blocked_by_task(trigger["feedback"]):
                return revise(selected="p", instruction="prepare first", new_tasks=[task("p", paths=["a.txt"])],
                              updates=[describe("a", deps=["p"])])
            return None

        def work(script, tid, n, kwargs, result):
            if tid == "a" and n == 1:
                cwd = kwargs["cwd"]
                (cwd / "a.txt").write_text("partial a\n")
                git.git(cwd, "add", "a.txt")
                (cwd / "a.txt").write_text("partial a, unstaged too\n")
            if tid == "p":
                # The prerequisite starts from the accepted commit, not the blocked edits.
                script.counts["p clean"] = int(not (kwargs["cwd"] / "a.txt").exists())
                (kwargs["cwd"] / "p.txt").unlink()
                (kwargs["cwd"] / "a.txt").write_text("prepared\n")
                result["evidence"] = ["a.txt"]
        return Script(decide, work=lambda s, tid, n, kw, r: (block_first("a")(s, tid, n, kw, r), work(s, tid, n, kw, r)))

    def test_archive_publication_binds_the_clean_snapshot_and_selected_task(self):
        script = self.archive_script()
        engine = self.engine(script)
        planning = engine.services.planning
        original = planning.apply_pending_revision

        def publish():
            original()
            raise KeyboardInterrupt()
        with patch.object(planning, "apply_pending_revision", side_effect=publish), self.assertRaises(KeyboardInterrupt):
            engine.start("archive", {"tasks": [task("a")]})
        saved = self.state(engine)
        content = read_json(engine.directory / "plans" / "revision-1.json")
        clean = snapshot_digest(engine.context.snapshot())
        self.assertEqual(content["candidate"], "archive")
        self.assertTrue(git.clean(engine.worktree, binding=saved["worktree_binding"]))
        self.assertEqual(saved["current_task"], "p")
        self.assertEqual(saved["pending_directive"], {"directive": content["directive"], "snapshot_digest": clean,
                                                      "revision": 1})
        self.assertNotEqual(clean, content["snapshot_digest"])
        archive = engine.directory / "recovery" / "revision-1"
        self.assertEqual((archive / "files" / "a.txt").read_text(), "partial a, unstaged too\n")
        self.assertTrue((archive / "restored.json").is_file())
        result = self.engine(script).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(script.counts["p clean"], 1)
        self.assertEqual(result["attempts"], {"a": 2, "p": 1})
        decides = script.of("commander", "decide")
        self.assertEqual(len(decides), 2)
        self.assertEqual(decides[1]["packet"]["trigger"]["ready"], ["a"])

    def test_plan_file_written_before_the_landing_save_is_reused(self):
        script = self.archive_script()
        engine = self.engine(script)
        with Killed(target="revision-1.json") as killed, self.assertRaises(BaseException):
            engine.start("archive", {"tasks": [task("a")]})
        self.assertTrue(killed.killed)
        disk = self.state(engine)
        self.assertIn(KEY, disk["commander_receipts"])
        self.assertNotIn("pending_revision", disk)
        plan = engine.directory / "plans" / "revision-1.json"
        written = plan.read_bytes()
        result = self.engine(script).resume(disk["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(plan.read_bytes(), written)
        self.assertEqual(result["plan_history"][1]["artifact_digest"], hashlib.sha256(written).hexdigest())
        self.assertEqual(len(script.of("commander", "decide")), 2)

    def test_interruption_between_archive_and_reset_archives_again_and_publishes_once(self):
        script = self.archive_script()
        engine = self.engine(script)
        real = git.git
        state = {"fired": False}

        def interrupt_reset(repo, *args, **kwargs):
            if args[:2] == ("reset", "--hard") and not state["fired"]:
                state["fired"] = True
                raise KeyboardInterrupt()
            return real(repo, *args, **kwargs)
        with patch("harness.git.git", side_effect=interrupt_reset), self.assertRaises(KeyboardInterrupt):
            engine.start("archive", {"tasks": [task("a")]})
        disk = self.state(engine)
        self.assertIn("pending_revision", disk)
        self.assertEqual(len(disk["plan_history"]), 1)
        self.assertFalse((engine.directory / "recovery" / "revision-1" / "restored.json").exists())
        result = self.engine(script).resume(disk["id"])
        self.assertEqual(result["status"], "completed")
        folders = sorted(p.name for p in (engine.directory / "recovery").iterdir() if p.name.startswith("revision-1"))
        self.assertEqual(len(folders), 2)
        self.assertEqual(len(list((engine.directory / "recovery").glob("revision-1*/restored.json"))), 1)
        self.assertEqual(len(result["plan_history"]), 2)
        self.assertEqual(script.counts["p clean"], 1)
        # Only the blocked task's later selection is a new decide.
        self.assertEqual(len(script.of("commander", "decide")), 2)
