"""Interruption at the decide landing, the archive/reset/publish sequence and the ask landing.

The kill mode writes the boundary save, then fails every later write of the
process, as SIGKILL would: no in-memory state can repair a split save.
"""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.planning import Planner
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
        events = [json.loads(line)["event"] for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        self.assertIn("revision_edits_archived", events)
        self.assertNotIn("replan_edits_archived", events)
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


def retain_script():
    def decide(script, trigger, packet):
        if trigger["kind"] == "task" and blocked_by_task(trigger["feedback"]):
            return revise(selected="a", instruction="continue a", updates=[describe("a")])
        return None
    return Script(decide, block_first("a"))


def damage(path, change):
    if change == "missing":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b"\n")


class PlanAuthorityTests(DecideCase):
    """The published plan files are the authority wherever the plan is used, not only for a pending directive."""

    def test_revision_zero_is_verified_before_approval(self):
        for change in ("missing", "changed"):
            with self.subTest(change=change):
                self.setUp()
                script = Script()
                engine = self.engine(script)
                state = engine.start("approve", {"tasks": [task("a")]}, pause_after_plan=True)
                damage(Path(state["plan_history"][0]["artifact"]), change)
                path = engine.directory / "state.json"
                before = path.read_bytes()
                with self.assertRaisesRegex(HarnessError, r"已发布的计划文件（revision-0）(缺失|已改变)"):
                    self.engine(script).resume(state["id"], approve_plan=True)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(self.state(engine)["plan_approved"])
                self.assertEqual(script.calls, [])

    def test_damaged_plan_stops_resume_before_the_exit_confirmation_write(self):
        script = Script()
        engine = self.engine(script)
        saved = engine.start("confirm", None, pause_after_plan=True)
        entry = saved["dispatches"][-1]
        entry["exit_confirmed"] = False
        from harness.util import write_json
        write_json(engine.directory / "state.json", saved)
        Path(saved["plan_history"][0]["artifact"]).unlink()
        before = (engine.directory / "state.json").read_bytes()
        with self.assertRaisesRegex(HarnessError, r"已发布的计划文件（revision-0）缺失"):
            self.engine(script).resume(saved["id"], confirm_exited=[entry["id"]])
        self.assertEqual((engine.directory / "state.json").read_bytes(), before)
        # abandon keeps its own audited --confirm-exited and does not need the plan file.
        result = self.engine(script).abandon(saved["id"], confirm_exited=[entry["id"]])
        self.assertEqual(result["status"], "abandoned")

    def interrupted_after_directive(self):
        script = retain_script()
        engine = self.engine(script)
        publish = engine.services.tasks.publish_worker_result

        def interrupt(task_, result, prior):
            if script.counts["w:a"] == 2:
                raise KeyboardInterrupt()
            return publish(task_, result, prior)
        with patch.object(engine.services.tasks, "publish_worker_result", side_effect=interrupt), \
                self.assertRaises(KeyboardInterrupt):
            engine.start("consumed", {"tasks": [task("a")]})
        saved = self.state(engine)
        self.assertEqual(len(saved["plan_history"]), 2)
        self.assertNotIn("pending_directive", saved)
        self.assertIn("worker:implement:a", saved["worker_receipts"])
        return engine, script, saved

    def test_latest_plan_is_verified_after_the_directive_is_consumed_and_before_worker_replay(self):
        for change in ("missing", "changed", "ledger tasks"):
            with self.subTest(change=change):
                self.setUp()
                engine, script, saved = self.interrupted_after_directive()
                if change == "ledger tasks":
                    saved["tasks"][0]["description"] = "A description no published plan contains"
                    from harness.util import write_json
                    write_json(engine.directory / "state.json", saved)
                    pattern = "台账任务表与已发布的计划文件不符"
                else:
                    damage(Path(saved["plan_history"][1]["artifact"]), change)
                    pattern = r"已发布的计划文件（revision-1）(缺失|已改变)"
                calls = len(script.calls)
                with self.assertRaisesRegex(HarnessError, pattern):
                    self.engine(script).resume(saved["id"])
                after = self.state(engine)
                # The sealed worker result was not replayed: no review, no acceptance.
                self.assertIn("worker:implement:a", after["worker_receipts"])
                self.assertEqual(after["accepted"], {})
                self.assertEqual(len(script.calls), calls)

    def test_plan_removed_during_the_run_stops_at_the_next_step(self):
        def work(script, tid, n, kwargs, result):
            block_first("a")(script, tid, n, kwargs, result)
            if tid == "a" and n == 2:
                (Path(kwargs["run_path"]).parent / "plans" / "revision-1.json").unlink()
        script = retain_script()
        script.work = work
        engine = self.engine(script)
        with self.assertRaisesRegex(HarnessError, r"已发布的计划文件（revision-1）缺失"):
            engine.start("removed", {"tasks": [task("a")]})
        self.assertEqual(script.of("reviewer", "final_review"), [])
        self.assertNotEqual(self.state(engine)["status"], "completed")

    def test_plan_is_verified_before_whole_run_completion(self):
        holder = {}

        def review(script, tid, n, kwargs, result):
            if tid == "final":
                plan = Path(kwargs["run_path"]).parent / "plans" / "revision-0.json"
                holder["bytes"] = plan.read_bytes()
                plan.write_bytes(holder["bytes"] + b"\n")
        script = Script(review=review)
        engine = self.engine(script)
        with self.assertRaisesRegex(HarnessError, r"已发布的计划文件（revision-0）已改变"):
            engine.start("complete", {"tasks": [task("a")]})
        saved = self.state(engine)
        self.assertNotEqual(saved["status"], "completed")
        self.assertEqual(saved["final_history"][-1]["status"], "approved")
        # Restoring the original bytes completes from the saved approval, without another call.
        (engine.directory / "plans" / "revision-0.json").write_bytes(holder["bytes"])
        calls = len(script.calls)
        result = self.engine(script).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(script.calls), calls)


class AskCandidateChangeTests(DecideCase):
    def changing_script(self, decision, head=False):
        script = Script(lambda s, t, p: decision if t["kind"] == "task" and blocked_by_task(t["feedback"]) else None,
                        block_first("a"))
        apply = script.change

        def change(backend, kwargs, packet, result):
            apply(backend, kwargs, packet, result)
            if kwargs["role"] == "commander":
                (kwargs["cwd"] / "a.txt").write_text("changed while the commander was called\n")
                if head:
                    git.git(kwargs["cwd"], "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c",
                            "commit.gpgsign=false", "commit", "--allow-empty", "-m", "unexpected commit")
        script.change = change
        return script

    def assert_registered_then_head_gated(self, engine, script, calls):
        saved = self.state(engine)
        self.assertEqual([r["kind"] for r in saved["decision_requests"]], ["commander"])
        self.assertNotIn(KEY, saved["commander_receipts"])
        entry = next(d for d in saved["dispatches"] if d["role"] == "commander")
        self.assertTrue(entry["candidate_changed"])
        self.assertEqual(len(script.calls), calls)
        # The answer does not open execution: the HEAD gate still holds.
        request = saved["decision_requests"][0]
        answer = self.repo.parent / "continue.json"
        answer.write_text(json.dumps({"schema_version": 1, "request_id": request["id"], "task_id": "a",
                                      "decision": "continue", "summary": "Continue"}))
        with self.assertRaisesRegex(HarnessError, "worktree HEAD 与检查点不同"):
            self.engine(script).resume(saved["id"], operator_decision=[str(answer)])
        self.assertEqual(len(script.calls), calls)
        self.assertEqual(self.state(engine)["accepted"], {})

    def test_returned_ask_interrupted_before_landing_registers_on_resume_despite_a_head_change(self):
        script = self.changing_script(ask("Choose an interface"), head=True)
        engine = self.engine(script)
        with patch.object(Planner, "land_ask", side_effect=KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            engine.start("head", {"tasks": [task("a")]})
        saved = self.state(engine)
        self.assertIn(KEY, saved["commander_receipts"])
        self.assertEqual(saved["decision_requests"], [])
        calls = len(script.calls)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定：task_id=a.*候选代码或 HEAD 已改变"):
            self.engine(script).resume(saved["id"])
        self.assert_registered_then_head_gated(engine, script, calls)

    def test_returned_ask_hook_is_rerun_after_repair_despite_a_head_change(self):
        script = self.changing_script(ask("Choose an interface"), head=True)
        engine = self.engine(script)
        emit = engine.context.emit_hook

        def failing(name, value, logs):
            if name == "post_agent" and value.get("role") == "commander":
                raise HarnessError("commander hook unavailable")
            return emit(name, value, logs)
        with patch.object(engine.context, "emit_hook", side_effect=failing), \
                self.assertRaisesRegex(HarnessError, "commander hook unavailable"):
            engine.start("hook", {"tasks": [task("a")]})
        saved = self.state(engine)
        self.assertIn(KEY, saved["commander_receipts"])
        self.assertEqual(saved["decision_requests"], [])
        calls = len(script.calls)
        resumed = self.engine(script)
        with patch.object(resumed.context, "emit_hook", wraps=resumed.context.emit_hook) as hooks, \
                self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            resumed.resume(saved["id"])
        self.assertEqual([c.args[0] for c in hooks.call_args_list].count("post_agent"), 1)
        self.assertEqual(self.state(engine)["commander_receipts"], {})
        self.assert_registered_then_head_gated(engine, script, calls)

    def test_ask_is_registered_when_the_candidate_changed_during_the_call(self):
        script = self.changing_script(ask("Choose an interface"))
        engine = self.engine(script)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定：task_id=a.*候选代码或 HEAD 已改变"):
            engine.start("ask", {"tasks": [task("a")]})
        saved = self.state(engine)
        self.assertEqual([r["kind"] for r in saved["decision_requests"]], ["commander"])
        self.assertNotIn(KEY, saved["commander_receipts"])
        entry = next(d for d in saved["dispatches"] if d["role"] == "commander")
        self.assertEqual((entry["status"], entry["candidate_changed"]), ("completed", True))
        events = [json.loads(line)["event"] for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        self.assertIn("commander_candidate_changed", events)
        # Registering grants the change nothing: it stays an unaccepted edit.
        self.assertEqual(saved["accepted"], {})
        self.assertIn("changed while", (engine.worktree / "a.txt").read_text())
        calls = len(script.calls)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            self.engine(script).resume(saved["id"])
        self.assertEqual(len(script.calls), calls)

    def test_other_decisions_still_stop_on_a_changed_candidate(self):
        script = self.changing_script(revise(selected="a", instruction="continue a", updates=[describe("a")]))
        engine = self.engine(script)
        with self.assertRaisesRegex(HarnessError, "指挥或 hook 修改了代码/HEAD"):
            engine.start("revise", {"tasks": [task("a")]})
        saved = self.state(engine)
        self.assertIn(KEY, saved["commander_receipts"])
        self.assertEqual(saved["decision_requests"], [])
        self.assertNotIn("pending_revision", saved)
