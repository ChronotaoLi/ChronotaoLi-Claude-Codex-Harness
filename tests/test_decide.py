"""The single commander call: decide lands as a directive, a revision or a question.

Every script branches on packet content (trigger kind, ready IDs, feedback,
previous_invalid), never on call counts, so redo and resume paths stay honest.
"""
import collections
import copy
import json
import sys
from pathlib import Path
from unittest import mock

from harness.planning import Planner
from harness.util import HarnessError, read_json
from test_harness import RepoCase
from test_runtime_changes import ScriptedBackend

KEY = "commander:decide:run"
EMPTY = {"selected_task": "", "instruction": "", "reason": "", "new_tasks": [], "updates": []}
DECIDE_FIELDS = ("action", *EMPTY)
FINDING = {"id": "gap", "severity": "blocking", "category": "defect", "location": "a.txt:1",
           "message": "The edge case is missing", "evidence": ["a.txt lacks the edge case"]}


def task(tid, deps=(), paths=None, description=None, skills=()):
    return {"id": tid, "title": f"Task {tid}", "description": description or f"Produce {tid}.txt",
            "depends_on": list(deps), "allowed_paths": list(paths or [f"{tid}.txt"]),
            "acceptance": [f"{tid}.txt exists"], "skills": list(skills)}


def implement(tid, instruction=None, reason=""):
    return {"action": "implement", "selected_task": tid, "instruction": instruction or f"do {tid}", "reason": reason}


def ask(reason="Which interface should the task keep?"):
    return {"action": "ask", "reason": reason}


def revise(reason="refine", selected="", instruction="", new_tasks=(), updates=()):
    return {"action": "revise", "reason": reason, "selected_task": selected, "instruction": instruction,
            "new_tasks": list(new_tasks), "updates": list(updates)}


def describe(tid, text=None, deps=()):
    return {"id": tid, "description": text or f"Produce {tid}.txt (refined)", "add_depends_on": list(deps)}


def blocked_by_task(feedback):
    worker = (feedback or {}).get("worker") or {}
    return worker.get("blocker_kind") == "task"


def rejected(feedback):
    return "request_changes" in json.dumps(feedback or {})


class Script(ScriptedBackend):
    """Offline backend for decide flows.

    decide(script, trigger, packet) returns the commander result fields or None
    for the demo default; work(script, tid, n, kwargs, result) and
    review(script, tid, n, kwargs, result) adjust the n-th worker or review.
    """

    def __init__(self, decide=None, work=None, review=None):
        super().__init__(self._apply)
        self.decide, self.work, self.review = decide, work, review
        self.counts = collections.Counter()

    def _apply(self, backend, kwargs, packet, result):
        role = kwargs["role"]
        if role == "worker":
            tid = kwargs["task"]["id"]
            self.counts["w:" + tid] += 1
            cwd = kwargs["cwd"]
            (cwd / "hello.py").unlink(missing_ok=True)
            (cwd / f"{tid}.txt").write_text(f"{tid} attempt {self.counts['w:' + tid]}\n")
            result["evidence"] = [f"{tid}.txt"]
            result["summary"] = f"Produced {tid}.txt"
            for claim in result.get("repair_claims", {}).values():
                claim["evidence_paths"] = [f"{tid}.txt"]
            if self.work:
                self.work(self, tid, self.counts["w:" + tid], kwargs, result)
        elif role == "reviewer":
            tid = kwargs["schema"]["properties"]["task_id"]["enum"][0]
            self.counts["r:" + tid] += 1
            if self.review:
                self.review(self, tid, self.counts["r:" + tid], kwargs, result)
        elif self.decide:
            value = self.decide(self, packet["trigger"], packet)
            if value is not None:
                for field in DECIDE_FIELDS:
                    result.pop(field, None)
                result.update({**EMPTY, **value})

    def of(self, role=None, kind=None, task_id=None):
        return [c for c in self.calls if (role is None or c["role"] == role) and (kind is None or c["kind"] == kind)
                and (task_id is None or c["task_id"] == task_id)]


def block_first(*tids):
    """The first worker attempt of each named task reports a task blocker."""
    def work(script, tid, n, kwargs, result):
        if tid in tids and n == 1:
            result.update(status="blocked", blocker_kind="task", blockers=["The interface is ambiguous"],
                          summary="blocked on the interface")
    return work


def reject_first(*tids):
    def review(script, tid, n, kwargs, result):
        if tid in tids and n == 1:
            result.update(verdict="request_changes", findings=[copy.deepcopy(FINDING)])
    return review


class DecideCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.config.data["verification"]["commands"] = [[sys.executable, "-c", "pass"]]

    def state(self, engine):
        return read_json(engine.directory / "state.json")

    def answer(self, run, decision="continue", summary="Keep the documented interface.", request=None):
        rows = [r for r in run["decision_requests"]
                if not any(o["request_id"] == r["id"] for o in run["operator_decisions"])]
        request = request or rows[-1]
        path = self.repo.parent / f"answer-{request['id'][-12:]}-{len(run['operator_decisions'])}.json"
        path.write_text(json.dumps({"schema_version": 1, "request_id": request["id"], "task_id": request["task_id"],
                                    "decision": decision, "summary": summary}))
        return str(path)

    def commander_entries(self, run):
        return [d for d in run["dispatches"] if d["role"] == "commander"]


class ImplementTests(DecideCase):
    def test_implement_lands_directive_and_receipt_in_one_save_and_keeps_reason_out_of_the_ledger(self):
        def decide(script, trigger, packet):
            if trigger["kind"] == "task" and trigger["ready"] == ["a", "b"]:
                return implement("b", "Build b against the documented interface", reason="b unblocks review")
        script = Script(decide)
        landed = []
        original = Planner.land_implement

        def spy(planner, decision):
            receipt = copy.deepcopy(planner.context.run["commander_receipts"][KEY])
            result = original(planner, decision)
            landed.append((receipt, read_json(planner.context.directory / "state.json")))
            return result
        engine = self.engine(script)
        with mock.patch.object(Planner, "land_implement", spy):
            result = engine.start("two", {"tasks": [task("a"), task("b")]})
        self.assertEqual(result["status"], "completed")
        receipt, saved = landed[0]
        self.assertEqual(saved["pending_directive"], {
            "directive": {"selected_task": "b", "action": "implement",
                          "instruction": "Build b against the documented interface"},
            "snapshot_digest": receipt["snapshot_digest"]})
        self.assertEqual(saved["current_task"], "b")
        self.assertNotIn(KEY, saved["commander_receipts"])
        self.assertNotIn("b unblocks review", (engine.directory / "state.json").read_text())
        self.assertEqual(read_json(Path(receipt["artifact"]))["reason"], "b unblocks review")
        decide_calls = script.of("commander", "decide")
        self.assertEqual(len(decide_calls), 1)
        self.assertNotIn("instruction", decide_calls[0]["packet"])
        self.assertNotIn("ready", decide_calls[0]["packet"])
        self.assertEqual(decide_calls[0]["packet"]["trigger"]["ready"], ["a", "b"])
        workers = script.of("worker")
        self.assertEqual([c["task_id"] for c in workers], ["b", "a"])
        self.assertEqual(workers[0]["packet"]["instruction"], "Build b against the documented interface")
        self.assertIsNone(workers[1]["packet"]["instruction"])

    def test_selection_outside_the_ready_list_is_invalid_and_redone(self):
        def decide(script, trigger, packet):
            if trigger["kind"] != "task":
                return None
            if "previous_invalid" in packet:
                return implement(trigger["ready"][0])
            # b depends on unaccepted a at first; later a is already accepted.
            return implement("b" if "a" in trigger["ready"] else "a")
        script = Script(decide)
        result = self.engine(script).start("chain", {"tasks": [task("a"), task("b", ["a"]), task("c")]})
        self.assertEqual(result["status"], "completed")
        entries = self.commander_entries(result)
        statuses = [d["status"] for d in entries]
        self.assertEqual(statuses, ["invalid", "completed", "invalid", "completed"])
        self.assertEqual(entries[1]["retry_of"], entries[0]["id"])
        self.assertEqual(entries[3]["retry_of"], entries[2]["id"])
        self.assertTrue(all("retry_of" not in d for d in (entries[0], entries[2])))
        self.assertEqual(sum(result["attempts"].values()), 3)

    def test_gated_task_cannot_be_selected_by_implement_or_revise(self):
        def work(script, tid, n, kwargs, result):
            if tid == "a":
                result["decision_requests"] = [{"id": "risk", "task_id": "c", "question": "Ship c as well?"}]

        def decide(script, trigger, packet):
            if trigger["kind"] != "task":
                return None
            if trigger["ready"] == ["a", "b", "d", "e"]:
                return implement("a")
            if "previous_invalid" in packet:
                return implement(trigger["ready"][0])
            if "c" not in trigger["ready"] and script.counts["gated"] == 0:
                script.counts["gated"] += 1
                return implement("c")
            if "c" not in trigger["ready"] and script.counts["gated"] == 1:
                script.counts["gated"] += 1
                # Otherwise valid: d is unaccepted; only the gated selection is wrong.
                return revise(selected="c", instruction="do c", updates=[describe("d")])
            return implement(trigger["ready"][0])
        script = Script(decide, work)
        engine = self.engine(script)
        plan = {"tasks": [task("a"), task("b"), task("c", ["a"]), task("d"), task("e")]}
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            engine.start("gated", plan)
        run = self.state(engine)
        entries = self.commander_entries(run)
        self.assertEqual([d["status"] for d in entries], ["completed", "invalid", "completed", "invalid", "completed"])
        self.assertEqual(entries[2]["retry_of"], entries[1]["id"])
        self.assertEqual(entries[4]["retry_of"], entries[3]["id"])
        self.assertEqual(len(run["plan_history"]), 1)
        self.assertNotIn("c", run["attempts"])


class ReviseTests(DecideCase):
    def test_revise_without_instruction_unready_selection_or_no_change_is_invalid(self):
        variants = {
            "no instruction": revise(selected="a", updates=[describe("a")]),
            "unready selection": revise(selected="b", instruction="do b", updates=[describe("a")]),
            "no change": revise(selected="a", instruction="do a", updates=[]),
        }
        for name, bad in variants.items():
            with self.subTest(name=name):
                self.setUp()

                def decide(script, trigger, packet, bad=bad):
                    if trigger["kind"] == "task" and blocked_by_task(trigger["feedback"]):
                        return revise(selected="a", instruction="continue a", updates=[describe("a")]) \
                            if "previous_invalid" in packet else bad
                    return None
                script = Script(decide, block_first("a"))
                engine = self.engine(script)
                result = engine.start("one", {"tasks": [task("a"), task("b", ["a"])]})
                self.assertEqual(result["status"], "completed")
                entries = self.commander_entries(result)
                self.assertEqual([d["status"] for d in entries], ["invalid", "completed"])
                self.assertEqual(len(result["plan_history"]), 2)
                # The invalid revision left no plan file of its own.
                self.assertEqual(sorted(p.name for p in (engine.directory / "plans").iterdir()),
                                 ["revision-0.json", "revision-1.json"])
                self.assertEqual(read_json(Path(result["plan_history"][1]["artifact"]))["source"]["dispatch_id"],
                                 entries[1]["id"])


class AskTests(DecideCase):
    def ask_on_blocker(self, script, trigger, packet):
        if trigger["kind"] != "task" or not blocked_by_task(trigger["feedback"]):
            return None
        if "operator_answer" not in packet:
            return ask()
        return revise(selected="a", instruction="continue a", updates=[describe("a")])

    def test_answered_question_reaches_the_next_decide_including_its_redo(self):
        summary = ("Keep the documented interface. " + "x" * 4000)[:4000]
        self.assertEqual(len(summary), 4000)

        def decide(script, trigger, packet):
            if trigger["kind"] != "task" or not blocked_by_task(trigger["feedback"]):
                return None
            if "operator_answer" not in packet:
                return ask()
            if "previous_invalid" not in packet:
                return revise(selected="a", updates=[describe("a")])  # no instruction: invalid, redone
            return revise(selected="a", instruction="continue a", updates=[describe("a")])
        script = Script(decide, block_first("a"))
        engine = self.engine(script)
        with self.assertRaisesRegex(HarnessError, r"等待操作者决定：task_id=a request_id="):
            engine.start("ask", {"tasks": [task("a"), task("b", ["a"])]})
        run = self.state(engine)
        request = run["decision_requests"][-1]
        self.assertEqual((request["kind"], request["source_task_id"], request["task_id"]), ("commander", "run", "a"))
        result = self.engine(script).resume(run["id"], operator_decision=[self.answer(run, summary=summary)])
        self.assertEqual(result["status"], "completed")
        decides = script.of("commander", "decide")
        self.assertEqual(len(decides), 3)
        for call in decides[1:]:
            self.assertEqual(call["packet"]["operator_answer"]["summary"], summary)
            self.assertEqual(call["packet"]["operator_answer"]["request_id"], request["id"])
        self.assertIn("previous_invalid", decides[2]["packet"])

    def test_unanswered_question_blocks_every_dispatch_even_after_budget_increases(self):
        script = Script(self.ask_on_blocker, block_first("a"))
        engine = self.engine(script)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            engine.start("ask", {"tasks": [task("a"), task("b")]})
        run = self.state(engine)
        count = len(script.calls)
        for raise_budgets in (False, True):
            if raise_budgets:
                self.config.data["runtime"]["max_agent_dispatches"] += 10
                self.config.data["runtime"]["max_attempts"] += 2
            with self.subTest(raise_budgets=raise_budgets), self.assertRaisesRegex(HarnessError, "等待操作者决定"):
                self.engine(script).resume(run["id"])
            self.assertEqual(len(script.calls), count)
            self.assertEqual(self.state(engine)["agent_dispatches"], run["agent_dispatches"])

    def test_stop_answer_blocks_until_it_is_revised_to_continue(self):
        script = Script(self.ask_on_blocker, block_first("a"))
        engine = self.engine(script)
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            engine.start("ask", {"tasks": [task("a"), task("b", ["a"])]})
        run = self.state(engine)
        request = run["decision_requests"][-1]
        with self.assertRaisesRegex(HarnessError, "操作者决定停止"):
            self.engine(script).resume(run["id"], operator_decision=[self.answer(run, "stop", "Not now.")])
        count = len(script.calls)
        with self.assertRaisesRegex(HarnessError, "操作者已决定停止"):
            self.engine(script).resume(run["id"])
        self.assertEqual(len(script.calls), count)
        run = self.state(engine)
        result = self.engine(script).resume(run["id"], operator_decision=[self.answer(run, request=request)])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(script.of("commander", "decide")), 2)

    def test_malformed_question_still_registers_and_stops_without_redo_or_hook(self):
        variants = {
            "operations": {**ask(), "new_tasks": [task("z")]},
            "long": ask("Q" * 5000),
            "number": {**ask(), "reason": 7},
            "missing field": {**ask(), "updates": None},
        }
        for name, value in variants.items():
            with self.subTest(name=name):
                self.setUp()

                def decide(script, trigger, packet, value=value):
                    if trigger["kind"] == "task" and blocked_by_task(trigger["feedback"]):
                        return value
                    return None
                script = Script(decide, block_first("a"))
                engine = self.engine(script)
                emitted = []
                emit = engine.hooks.emit

                def spy(event, payload, logs):
                    emitted.append((event, payload.get("kind")))
                    return emit(event, payload, logs)
                if name == "missing field":
                    original = script.decide
                    script.decide = lambda s, t, p: (lambda v: v and {k: x for k, x in v.items() if x is not None})(original(s, t, p))
                    apply = script._apply

                    def drop(backend, kwargs, packet, result):
                        apply(backend, kwargs, packet, result)
                        if kwargs["role"] == "commander" and result.get("action") == "ask":
                            result.pop("updates", None)
                    script.change = drop
                with mock.patch.object(engine.hooks, "emit", side_effect=spy), \
                        self.assertRaisesRegex(HarnessError, "等待操作者决定"):
                    engine.start("ask", {"tasks": [task("a"), task("b", ["a"])]})
                run = self.state(engine)
                entry = self.commander_entries(run)[-1]
                self.assertEqual(entry["status"], "invalid_asked")
                self.assertTrue(entry["error"])
                self.assertEqual(len(script.of("commander", "decide")), 1)
                self.assertNotIn(("post_agent", "decide"), emitted)
                request = run["decision_requests"][-1]
                self.assertEqual(request["kind"], "commander")
                self.assertEqual(request["source"]["dispatch_id"], entry["id"])
                if name in ("long", "number"):
                    self.assertIn("Read the bound original result", request["question"])
                self.engine(script).load_run(run["id"])

    def test_planning_question_keeps_the_plan_empty_until_the_answered_decide(self):
        def decide(script, trigger, packet):
            if trigger["kind"] == "plan":
                if "operator_answer" not in packet:
                    return ask("Which module should own the parser?")
                return revise(reason="plan", new_tasks=[task("a")])
            return None
        script = Script(decide)
        engine = self.engine(script)
        with self.assertRaisesRegex(HarnessError, r"等待操作者决定：task_id=final"):
            engine.start("plan it", pause_after_plan=True)
        run = self.state(engine)
        self.assertEqual((run["tasks"], run["original_tasks"], run["plan_history"]), ([], [], []))
        # Approving a plan does not answer the question that precedes it.
        with self.assertRaisesRegex(HarnessError, "等待操作者决定"):
            self.engine(script).resume(run["id"], approve_plan=True)
        self.assertFalse(self.state(engine)["plan_approved"])
        paused = self.engine(script).resume(run["id"], operator_decision=[self.answer(run)])
        self.assertEqual(paused["status"], "awaiting_approval")
        self.assertNotIn("pending_directive", paused)
        self.assertIsNone(paused.get("current_task"))
        result = self.engine(script).resume(run["id"], approve_plan=True)
        self.assertEqual(result["status"], "completed")
        decides = script.of("commander", "decide")
        self.assertEqual(decides[1]["packet"]["trigger"]["kind"], "plan")
        self.assertIn("operator_answer", decides[1]["packet"])
        plan = read_json(engine.directory / "plans" / "revision-0.json")
        self.assertEqual(plan["source"]["dispatch_id"], self.commander_entries(result)[1]["id"])
        self.assertEqual(result["original_tasks"], [task("a")])


class FinalStageTests(DecideCase):
    def test_final_rejection_is_decided_with_a_corrective_task_that_archives(self):
        def decide(script, trigger, packet):
            if trigger["kind"] == "final":
                return revise(reason="Fix the final finding", selected="fix", instruction="Fix the edge case",
                              new_tasks=[task("fix", ["a"], paths=["a.txt"])])
            return None

        def work(script, tid, n, kwargs, result):
            if tid == "fix":
                (kwargs["cwd"] / "fix.txt").unlink()
                (kwargs["cwd"] / "a.txt").write_text("a fixed\n")
                result["evidence"] = ["a.txt"]
                for claim in result.get("repair_claims", {}).values():
                    claim["evidence_paths"] = ["a.txt"]
        script = Script(decide, work, reject_first("final"))
        engine = self.engine(script)
        result = engine.start("final", {"tasks": [task("a")]})
        self.assertEqual(result["status"], "completed")
        decides = script.of("commander", "decide")
        self.assertEqual([c["packet"]["trigger"]["kind"] for c in decides], ["final"])
        self.assertEqual(read_json(engine.directory / "plans" / "revision-1.json")["candidate"], "archive")
        self.assertEqual(len(script.of("reviewer", "final_review")), 2)
        self.assertEqual([r["status"] for r in result["final_history"]], ["rejected", "approved"])


class PlanCapacityTests(DecideCase):
    HUGE = "x" * 120000

    def assert_redone_for_capacity(self, engine, script):
        entries = self.commander_entries(self.state(engine))
        self.assertEqual(entries[0]["status"], "invalid")
        self.assertIn("计划的整体审阅必要上下文", entries[0]["error"])
        self.assertIn("最大的字段", entries[0]["error"])
        redo = next(c for c in script.of("commander") if "previous_invalid" in c["packet"])
        self.assertIn("计划的整体审阅必要上下文", redo["packet"]["previous_invalid"]["error"])
        self.assertEqual(entries[1]["retry_of"], entries[0]["id"])

    def test_oversized_model_plan_is_invalid_and_redone(self):
        def decide(script, trigger, packet):
            if trigger["kind"] == "plan":
                big = task("a", description=self.HUGE)
                return revise(reason="plan", new_tasks=[task("a")] if "previous_invalid" in packet else [big])
            return None
        script = Script(decide)
        engine = self.engine(script)
        result = engine.start("plan")
        self.assertEqual(result["status"], "completed")
        self.assert_redone_for_capacity(engine, script)
        self.assertEqual(result["original_tasks"], [task("a")])

    def test_oversized_revision_is_invalid_and_redone(self):
        def decide(script, trigger, packet):
            if trigger["kind"] == "task" and blocked_by_task(trigger["feedback"]):
                if "previous_invalid" in packet:
                    return revise(selected="a", instruction="continue a", updates=[describe("a")])
                return revise(selected="a", instruction="continue a", updates=[describe("a")],
                              new_tasks=[task("big", paths=["a.txt"], description=self.HUGE)])
            return None
        script = Script(decide, block_first("a"))
        engine = self.engine(script)
        result = engine.start("revise", {"tasks": [task("a")]})
        self.assertEqual(result["status"], "completed")
        self.assert_redone_for_capacity(engine, script)
        self.assertEqual(len(result["plan_history"]), 2)

    def test_oversized_supplied_plan_stops_without_a_model_call(self):
        script = Script()
        engine = self.engine(script)
        with self.assertRaisesRegex(HarnessError, "计划的整体审阅必要上下文 .*请增加 context_chars"):
            engine.start("supplied", {"tasks": [task("a", description=self.HUGE)]})
        self.assertEqual(script.calls, [])


class ReservedNameTests(DecideCase):
    def test_reserved_names_in_model_plans_and_revisions_are_invalid_and_redone(self):
        for stage, name in (("plan", "run"), ("revise", "final")):
            with self.subTest(stage=stage):
                self.setUp()

                def decide(script, trigger, packet, stage=stage, name=name):
                    retry = "previous_invalid" in packet
                    if trigger["kind"] == "plan" and stage == "plan":
                        return revise(reason="plan", new_tasks=[task("a" if retry else name, paths=["a.txt"])])
                    if trigger["kind"] == "task" and stage == "revise" and blocked_by_task(trigger["feedback"]):
                        extra = [] if retry else [task(name, paths=["a.txt"])]
                        return revise(selected="a", instruction="continue a", updates=[describe("a")], new_tasks=extra)
                    return None
                script = Script(decide, block_first("a") if stage == "revise" else None)
                engine = self.engine(script)
                result = engine.start("reserved", None if stage == "plan" else {"tasks": [task("a")]})
                self.assertEqual(result["status"], "completed")
                entries = self.commander_entries(result)
                self.assertEqual([d["status"] for d in entries[:2]], ["invalid", "completed"])
                self.assertIn("保留名", entries[0]["error"])
                self.assertNotIn(name, {t["id"] for t in result["tasks"]})


class BudgetConfigTests(DecideCase):
    def test_removed_budget_keys_are_rejected_with_their_replacement(self):
        from harness.config import Config
        text = (Path(__file__).resolve().parents[1] / "harness.toml").read_text()
        self.assertIn("max_attempts = 5", text)
        self.assertEqual(Config.load(Path(__file__).resolve().parents[1] / "harness.toml").data["runtime"]["max_attempts"], 5)
        for key, hint in (("max_replans", "runtime.max_agent_dispatches"), ("max_final_attempts", "runtime.max_agent_dispatches"),
                          ("max_tasks", "runtime.context_chars")):
            with self.subTest(key=key):
                path = self.repo.parent / f"{key}.toml"
                path.write_text(text.replace("[runtime]\n", f"[runtime]\n{key} = 3\n", 1))
                with self.assertRaisesRegex(HarnessError, f"未知配置键：runtime.{key}.*{hint}"):
                    Config.load(path)

    def test_attempt_limit_names_the_key_and_its_value_before_any_decide(self):
        self.config.data["runtime"]["max_attempts"] = 1
        script = Script(work=block_first("a"))
        engine = self.engine(script)
        with self.assertRaisesRegex(HarnessError, r"a 已达尝试次数上限（runtime\.max_attempts 当前值 1）"):
            engine.start("limit", {"tasks": [task("a")]})
        self.assertEqual(script.of("commander"), [])
        from harness.planning import attempt_limit_error
        self.assertIn("runtime.max_attempts 当前值 7", str(attempt_limit_error("b", 7)))
        # The selected worker's attempt is checked again where it starts.
        resumed = self.engine(Script())
        resumed.load_run(engine.run["id"])
        with self.assertRaisesRegex(HarnessError, r"runtime\.max_attempts 当前值 1"):
            resumed.services.tasks.task_attempt(resumed.run["tasks"][0], {"selected_task": "a", "action": "implement",
                                                                          "instruction": None}, False)


class LedgerLinkTests(DecideCase):
    def paused(self):
        script = Script()
        engine = self.engine(script)
        engine.start("links", {"tasks": [task("a"), task("b")]}, step=True)
        return engine, script

    def assert_load_rejects(self, engine, state, pattern="运行台账恢复状态无效"):
        path = engine.directory / "state.json"
        from harness.util import write_json
        write_json(path, state)
        before = path.read_bytes()
        with self.assertRaisesRegex(HarnessError, pattern):
            self.engine(Script()).resume(state["id"])
        self.assertEqual(path.read_bytes(), before)

    def test_plan_references_and_directive_links_are_checked_at_load(self):
        engine, script = self.paused()
        saved = self.state(engine)
        reference = {"revision": 1, "artifact": str(engine.directory / "plans" / "revision-1.json"),
                     "artifact_digest": "0" * 64}
        cases = {
            "wrong revision": {"pending_revision": {**reference, "revision": 2}},
            "extra key": {"pending_revision": {**reference, "tasks": []}},
            "beside directive": {"pending_revision": reference, "current_task": "b", "pending_directive": {
                "directive": {"selected_task": "b", "action": "implement", "instruction": "do b"},
                "snapshot_digest": "0" * 64}},
            "history path": {"plan_history": [{**saved["plan_history"][0], "artifact": "/tmp/elsewhere.json"}]},
            "history digest": {"plan_history": [{**saved["plan_history"][0], "artifact_digest": "XYZ"}]},
            "history tasks": {"plan_history": [{**saved["plan_history"][0], "tasks": saved["tasks"]}]},
            "directive revision": {"current_task": "b", "pending_directive": {
                "directive": {"selected_task": "b", "action": "implement", "instruction": "do b"},
                "snapshot_digest": "0" * 64, "revision": 3}},
            "directive task": {"current_task": "a", "pending_directive": {
                "directive": {"selected_task": "b", "action": "implement", "instruction": "do b"},
                "snapshot_digest": "0" * 64}},
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.assert_load_rejects(engine, {**copy.deepcopy(saved), **change})

    def test_redo_link_cannot_name_an_out_of_scope_stop(self):
        def decide(script, trigger, packet):
            if trigger["kind"] == "task" and blocked_by_task(trigger["feedback"]):
                return revise(selected="z", instruction="do z", new_tasks=[task("z")],
                              updates=[describe("a", deps=["z"])])
            return None
        script = Script(decide, block_first("a"))
        engine = self.engine(script)
        with self.assertRaisesRegex(HarnessError, "越出已批准的文件范围"):
            engine.start("scope", {"tasks": [task("a")]})
        saved = self.state(engine)
        stop = self.commander_entries(saved)[-1]
        self.assertEqual(stop["out_of_scope_paths"], ["z.txt"])
        redo = {**copy.deepcopy(stop), "id": "fabricated-redo", "retry_of": stop["id"], "status": "completed"}
        redo.pop("out_of_scope_paths")
        state = copy.deepcopy(saved)
        state["dispatches"].append(redo)
        self.assert_load_rejects(engine, state)


class InterfaceTests(DecideCase):
    def test_retry_stopped_option_no_longer_exists(self):
        import contextlib
        import io
        from harness.cli import main
        engine = self.engine(Script(work=block_first("a"), decide=lambda s, t, p: ask() if t["kind"] == "task" and t["feedback"] else None))
        with self.assertRaises(HarnessError):
            engine.start("cli", {"tasks": [task("a")]})
        before = (engine.directory / "state.json").read_bytes()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["--repo", str(self.repo), "resume", engine.run["id"], "--retry-stopped", "final"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--retry-stopped", stderr.getvalue())
        self.assertEqual((engine.directory / "state.json").read_bytes(), before)


class FieldReplayTests(DecideCase):
    """The field run's seven commander replans, replayed as decide calls (spec 17.4).

    a: review rejects -> ask -> answer -> description-only revise (R1, R2)
    b: worker task blocker -> description-only revise (R3)
    c: worker task blocker -> ask -> answer -> description-only revise (R4, R5)
    d: review rejects -> ask -> answer -> description-only revise (R6, R7)
    """

    def test_seven_replans_need_eight_decides_and_three_questions(self):
        chain = [task("a"), task("b", ["a"]), task("c", ["b"]), task("d", ["c"])]
        asking, revised_ids = {"a", "c", "d"}, set()

        def decide(script, trigger, packet):
            if trigger["kind"] == "plan":
                return revise(reason="plan", new_tasks=chain)
            if trigger["kind"] != "task":
                return None
            tid = trigger.get("current_task") or trigger["ready"][0]
            feedback = trigger.get("feedback")
            needs = tid not in revised_ids and (rejected(feedback) or blocked_by_task(feedback))
            if needs and tid in asking and "operator_answer" not in packet:
                return ask(f"question about {tid}")
            if needs:
                revised_ids.add(tid)
                return revise(reason=f"refine {tid}", selected=tid, instruction=f"continue {tid}", updates=[describe(tid)])
            return implement(tid)
        script = Script(decide, block_first("b", "c"), reject_first("a", "d"))
        engine = self.engine(script)
        stops = 0
        try:
            engine.start("replay")
        except HarnessError as error:
            self.assertIn("等待操作者决定", str(error))
            stops += 1
        for _ in range(6):
            run = self.state(engine)
            if run["status"] == "completed":
                break
            try:
                self.engine(script).resume(run["id"], operator_decision=[self.answer(run)])
            except HarnessError as error:
                self.assertIn("等待操作者决定", str(error))
                stops += 1
        run = self.state(engine)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(stops, 3)
        self.assertEqual(len(script.of("commander", "decide")), 8)
        self.assertEqual(len(script.calls), 23)
        self.assertEqual(len(run["plan_history"]), 5)
        self.assertEqual({r["kind"] for r in run["decision_requests"]}, {"commander"})
