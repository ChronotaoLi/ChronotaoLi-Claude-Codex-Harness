"""Guards for typed stops (spec-4 14.4): registry, literal reads, process matrix, read classes, settling."""
import ast
import errno
import hashlib
import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import state_files, stops
from harness.adapters import DemoBackend
from harness.dispatch import invocation_stop, process_stop
from harness.process_runtime import ProcessOutcome, failure
from harness.recovery import bound_bytes, read_bound_bytes
from harness.run_context import RunContext
from harness.run_lifecycle import RunLifecycle
from harness.runtime import Runtime
from harness.util import EvidenceError, HarnessError, ProcessStartError, ProcessTimeout, read_json, write_json
from test_decide import DecideCase, Script, ask, block_first, blocked_by_task, implement, task
from test_harness import (ROOT, RepoCase, cli_notice, crash_after_save, no_command_exit, refused_by_cli, run_cli,
                          scratch)
from test_runtime_changes import ScriptedBackend
from test_stop_exits import final_answer, final_failure_scenario
import test_checkpoint_declaration_failures as _checkpoints
import test_decide_recovery as _ask_recovery


def plan():
    return read_json(ROOT / "examples" / "plan.json")


def refused(case, run_id):
    try:
        Runtime(case.config, case.repo, backend=DemoBackend()).abandon(run_id)
        return False
    except HarnessError:
        return True


def worker_packet(run):
    entry = next(d for d in run["dispatches"] if d["role"] == "worker" and d.get("worker_receipt"))
    return Path(entry["worker_receipt"]["packet_artifact"])


# ---- G4: the registry's exit codes -------------------------------------------------------
class RegistryGuard(unittest.TestCase):
    def test_exit_codes_by_kind(self):
        expected = {"evidence": 1, "workspace_mismatch": 1, "scope_violation": 1, "runtime_failure": 1,
                    "unclassified": 1, "interrupted": 130, "plan_approval": 0, "invalid_input": 2,
                    "config_mismatch": 2, "unsupported_ledger": 2}
        # 53 from step 4, four provider types from step 5, six scope boundary types from step 6,
        # one transport failure type, one configuration file type, one special file type
        self.assertEqual(len(stops.REGISTRY), 66)
        for (kind, reason), entry in stops.REGISTRY.items():
            with self.subTest(row=(kind, reason)):
                self.assertEqual(entry["exit_code"], expected.get(kind, 3))

    def test_rows_without_a_command_exit(self):
        for key in (("evidence", "load_integrity"), ("runtime_failure", "io"), ("runtime_failure", "database"),
                    ("runtime_failure", "invariant"), ("unclassified", "untyped_harness_error"),
                    ("unsupported_ledger", "ledger_version")):
            with self.subTest(row=key):
                self.assertEqual(stops.project(stops.make(*key), "run_x")["exits"], [])


# ---- G6: no runtime code reads the ledger's stop ------------------------------------------
WRITERS = {("workflow.py", "stop"), ("scheduler.py", "_execute_until_terminal"), ("cli.py", "status_summary")}
DELETERS = {("scheduler.py", "_execute_until_terminal"), ("run_lifecycle.py", "abandon_locked")}
OUTPUT_KEYS = {("cli.py", "stop_notice"), ("operations.py", "create"), ("watch_cli.py", "watching")}
READERS = {("observe.py", "_operation_summary"), ("watch_cli.py", "watching")}  # projections and records only
WRITTEN_LEAVE_ERROR_ASSIGNMENT = ast.dump(ast.parse("""
leave_error = {"stop": f"{value['kind']}/{value['reason']}",
               "error": value["data"].get("detail") or type(error).__name__}
""").body[0])


def stop_literals(name, text):
    """Every literal "stop" in a module, with its syntactic role and enclosing function."""
    tree = ast.parse(text)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def function(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return "<module>"
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and node.value == "stop"):
            continue
        parent, role = parents.get(node), "read"
        grand = parents.get(parent)
        if isinstance(parent, ast.Compare) and all(isinstance(op, (ast.Eq, ast.NotEq)) for op in parent.ops):
            role = "value"  # decision == "stop"
        elif (isinstance(parent, (ast.Tuple, ast.List, ast.Set)) and isinstance(grand, ast.Compare)
              and parent in grand.comparators and all(isinstance(op, (ast.In, ast.NotIn)) for op in grand.ops)):
            role = "value"  # decision in ("continue", "stop")
        elif isinstance(parent, ast.Dict) and node in parent.values:
            role = "value"  # {"event": "stop"}
        elif isinstance(parent, ast.Subscript) and isinstance(parent.ctx, ast.Store):
            role = "write"
        elif isinstance(parent, ast.Subscript) and isinstance(parent.ctx, ast.Del):
            role = "delete"
        elif (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute) and parent.func.attr == "pop"
              and isinstance(grand, ast.Expr)):
            role = "delete"
        elif isinstance(parent, ast.Dict) and node in parent.keys:
            handler = parents.get(grand)
            if (name == "run_lifecycle.py" and function(node) == "abandon_locked"
                    and isinstance(handler, ast.ExceptHandler) and grand is handler.body[-1]
                    and ast.dump(grand) == WRITTEN_LEAVE_ERROR_ASSIGNMENT):
                # This diagnostic is published as run_abandoned.written_leave_error.
                continue
            role = "output"
        found.append((name, function(node), role, node.lineno))
    return found


def stop_violations(found):
    allowed = {"write": WRITERS, "delete": DELETERS, "output": OUTPUT_KEYS, "read": READERS}
    return [item for item in found if item[2] != "value" and (item[0], item[1]) not in allowed[item[2]]]


class StopLiteralGuard(unittest.TestCase):
    def test_runtime_has_no_other_stop_literal(self):
        found = []
        for path in sorted((ROOT / "harness").glob("*.py")):
            if path.name != "stops.py":
                found += stop_literals(path.name, path.read_text(encoding="utf-8"))
        self.assertEqual(stop_violations(found), [])

    def test_indirect_reads_are_caught(self):
        samples = {
            "dict.get": "def f(run):\n    return dict.get(run, 'stop')\n",
            "getattr": "def f(run):\n    return getattr(run, 'get')('stop')\n",
            "itemgetter": "import operator\ndef f(run):\n    return operator.itemgetter('stop')(run)\n",
            "match": "def f(run):\n    match run:\n        case {'stop': s}:\n            return s\n",
            "alias": "def f(self):\n    c = self.context.run\n    return c.get('stop')\n",
            "inside writer": "def _execute_until_terminal(self):\n    if self.context.run.get('stop'):\n        return 1\n",
            "operate branches": "def operate(args):\n    if result.get('stop'):\n        return 3\n",
        }
        for name, text in samples.items():
            with self.subTest(sample=name):
                module = "cli.py" if name == "operate branches" else "scheduler.py"
                self.assertTrue(stop_violations(stop_literals(module, text)), name)


# ---- G7's exemption rule: only exceptions raised by test code count as injected ---------------------
class InjectionRule(unittest.TestCase):
    @staticmethod
    def caught(function):
        try:
            function()
        except BaseException as error:  # the exception and its traceback are the subject
            return error
        raise AssertionError("nothing raised")

    def test_test_frames_and_mock_side_effects_are_injected_runtime_and_library_frames_are_not(self):
        from unittest.mock import Mock
        from test_harness import injected

        def raised_by_a_test():
            raise HarnessError("raised by the test itself")
        cases = {
            "a frame under tests/": (raised_by_a_test, True),
            "a mock side_effect": (Mock(side_effect=HarnessError("patched failure")), True),
            "a standard library frame (json)": (lambda: json.loads("{not json"), False),
            "a runtime frame (harness/stops.py)": (lambda: stops.validate({}), False),
        }
        for name, (function, expected) in cases.items():
            with self.subTest(case=name):
                self.assertIs(injected(self.caught(function)), expected)


# ---- G12: construction never fails on values and never lists an exit it cannot fill ----
class DegradedStops(unittest.TestCase):
    def test_a_stop_never_lists_an_exit_it_cannot_fill(self):
        for key, entry in stops.REGISTRY.items():
            flags = [name for name, check in entry["data"].items() if check is stops._bool]
            for setting in (False, True):
                data = {name: setting for name in flags}
                required = [name for name in stops.required_fields(*key, data) if name not in data]
                if not required:
                    continue
                with self.subTest(row=key, flags=data):
                    value = stops.make(*key, data=dict(data), source={name: "not an id!" for name in required})
                    stops.validate(value)
                    self.assertEqual((value["kind"], value["data"]["error_type"]), ("unclassified", "IncompleteStop"))
                    good = stops.make(*key, data=dict(data), source={name: "d1" for name in required})
                    self.assertEqual((good["kind"], good["reason"]), key)
                    self.assertEqual(len(stops.project(good, "run_x")["exits"]),
                                     len(list(stops.active_exits(entry, good["data"]))))

    def test_a_missing_or_dropped_negated_condition_is_not_read_as_false(self):
        for data in ({}, {"exit_unconfirmed": "no"}):
            with self.subTest(data=data):
                value = stops.make("evidence", "process_incomplete", data=data, source={"dispatch_id": "d1"})
                self.assertEqual((value["kind"], value["data"]["error_type"]), ("unclassified", "IncompleteStop"))

    def test_an_interrupt_without_the_exit_fact_lists_resume_only(self):
        value = stops.of(KeyboardInterrupt())
        self.assertEqual((value["kind"], stops.project(value, "run_x")["exits"]), ("interrupted", ["resume run_x"]))
        confirmed = stops.make("interrupted", "signal", data={"exits_confirmed": True})
        self.assertEqual(stops.project(confirmed, "run_x")["exits"], ["resume run_x", "abandon run_x"])

    def test_scope_args_exits_render_operator_entries(self):
        """{field:scope_args} (spec-6 5.6): every listed --scope value is an operator entry, the suggestion unchanged."""
        import random
        import shlex
        from harness import scope
        rows = sorted(key for key, entry in stops.REGISTRY.items()
                      if any(":scope_args}" in (item if isinstance(item, str) else item[0]) for item in entry["exits"]))
        self.assertEqual(rows, [("scope_extension", "initial_plan"), ("scope_extension", "revision"),
                                ("scope_extension", "supplied_plan")])
        rng = random.Random(20261116)
        segments = ["app", "src", "x.py", "*", "?", "[ab]", "a*", "*.py", "**", "-x", "c d", "it's", "$HOME"]
        for key in rows:
            for _ in range(300):
                gaps = {}
                for task in ("t1", "t2"):
                    patterns = ["/".join(rng.choice(segments) for _ in range(rng.randint(1, 4))) for _ in range(3)]
                    gaps[task] = [p for p in patterns if not p.startswith("/") and "//" not in p
                                  and all(part not in ("", ".", "..") for part in p.split("/"))]
                gaps = {task: patterns for task, patterns in gaps.items() if patterns}
                entries, widened = scope.suggestions(gaps)
                value = stops.make(*key, data={"suggested_scope": entries, "gaps": gaps, "widened": widened, "scope": []})
                self.assertEqual((value["kind"], value["reason"]), key)
                commands = [c for c in stops.project(value, "run_x")["exits"] if "--scope=" in c]
                self.assertEqual(len(commands), 1 if entries else 0)
                for command in commands:
                    scoped = [word[len("--scope="):] for word in shlex.split(command) if word.startswith("--scope=")]
                    self.assertEqual(scoped, entries)
                    for entry in scoped:
                        self.assertEqual(scope.operator_entry(entry), entry)
                        self.assertTrue(all(any(scope.contains(gap, [e]) for e in scoped) for gap in sum(gaps.values(), [])))
            # Without suggestions the conditional exit is not listed and nothing degrades.
            empty = stops.make(*key, data={"suggested_scope": []})
            self.assertEqual(empty["kind"], key[0])
            self.assertFalse(any("--scope" in c for c in stops.project(empty, "run_x")["exits"]))

    def test_unknown_field_names_still_raise(self):
        with self.assertRaises(KeyError):
            stops.make("busy", "run_lock", data={"holder": 1})


# ---- G13: the process matrix --------------------------------------------------------------
def with_outcome(error, **fields):
    error.outcome = ProcessOutcome(**fields)
    return error


class ProcessMatrix(unittest.TestCase):
    """The commander/reviewer call boundary; a worker's failure is typed by its judgment (spec-5 2.7)."""
    def kind(self, error, job=Path("/nonexistent/job")):
        value = invocation_stop(error, job=job, role="commander", provider="claude", dispatch_id="d1",
                                task_id="t1", timeout=600)
        return value["kind"] + "/" + value["reason"]

    def test_each_cell(self):
        execution = lambda e: [failure("execution", e)]
        timeout = ProcessTimeout(1, Path("/x/process"))
        cells = {
            "timeout, clean, exit confirmed": (
                with_outcome(ProcessTimeout(1, Path("/x")), started=True, exit_confirmed=True, errors=execution(timeout)),
                "budget/agent_timeout"),
            "timeout, clean, exit unconfirmed": (
                with_outcome(ProcessTimeout(1, Path("/x")), started=True, exit_confirmed=False, errors=execution(timeout)),
                "process_unconfirmed/dispatch_exit"),
            "timeout with cleanup errors": (
                with_outcome(ProcessTimeout(1, Path("/x")), started=True, exit_confirmed=True, errors=execution(timeout),
                             cleanup_errors=[failure("sigkill", PermissionError())]),
                "evidence/process_incomplete"),
            "start failure, outcome persisted": (
                with_outcome(ProcessStartError("no cli"), started=False, errors=execution(ProcessStartError("no cli"))),
                "provider_failure/local_start"),
            "start failure, outcome not persisted": (
                with_outcome(ProcessStartError("no cli"), started=False, errors=execution(ProcessStartError("no cli")) + [
                    failure("persist_outcome", OSError(errno.ENOSPC, "full"))]),
                "evidence/process_incomplete"),
            "normal exit, outcome write failed (OSError)": (
                with_outcome(OSError(errno.ENOSPC, "full"), started=True, exit_confirmed=True, exit_code=0,
                             errors=[failure("persist_outcome", OSError())]),
                "evidence/process_incomplete"),
            "no process facts": (HarnessError("worker CLI exit=1"), "provider_failure/unknown"),
        }
        for name, (error, expected) in cells.items():
            with self.subTest(cell=name):
                self.assertEqual(self.kind(error), expected)

    def test_turns_and_provider_reports_come_from_the_envelope(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder)
            # The envelope is read only after a normal, complete process end (spec-5 2.2).
            (job / "process.outcome.json").write_text(json.dumps(ProcessOutcome(
                started=True, startup_state="started", exit_code=1, exit_confirmed=True).to_dict()))
            (job / "process.stdout").write_text(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}))
            self.assertEqual(self.kind(HarnessError("exit=1"), job), "budget/agent_turns")
            (job / "process.stdout").write_text(json.dumps({"type": "result", "subtype": "error_during_execution",
                                                            "is_error": True}))
            self.assertEqual(self.kind(HarnessError("exit=1"), job), "provider_failure/invocation_failed")
            (job / "process.stdout").write_text(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}))
            (job / "process.outcome.json").write_text(json.dumps(ProcessOutcome(
                started=True, exit_code=1, exit_confirmed=True, cleanup_errors=[{"step": "sigterm"}]).to_dict()))
            self.assertEqual(self.kind(HarnessError("exit=1"), job), "provider_failure/unknown")  # incomplete record

    def test_unconfirmed_incomplete_process_lists_only_the_confirming_abandon(self):
        error = with_outcome(ProcessTimeout(1, Path("/x")), started=True, exit_confirmed=False,
                             cleanup_errors=[failure("sigkill", PermissionError())])
        self.assertEqual(stops.project(process_stop(error, "d1"), "run_x")["exits"],
                         ["abandon run_x --confirm-exited d1"])


# ---- G14: only path loss and changed bytes are evidence; local I/O stays I/O -----------------
class ReadFailureClasses(RepoCase):
    def test_missing_is_evidence_permission_is_io(self):
        from test_state_files import FakeOs

        root = self.repo / ".harness"
        folder = root / "evidence"
        folder.mkdir(parents=True)
        artifact = folder / "result.json"
        files = state_files.StateFiles(root).under("evidence", artifact.name)
        raw = b"{}"
        digest = hashlib.sha256(raw).hexdigest()
        source = {"artifact": str(artifact), "artifact_digest": digest}

        with self.assertRaises(EvidenceError) as missing:
            bound_bytes(files, source, "测试")
        stop = stops.of(missing.exception)
        self.assertEqual((stop["kind"], stop["reason"], stop["source"]),
                         ("evidence", "runtime_integrity", source))

        artifact.write_bytes(raw)
        self.assertEqual(bound_bytes(files, source, "测试"), (raw, digest))
        parent = folder.stat()
        for code, error_type in ((errno.EACCES, "PermissionError"), (errno.EIO, "OSError")):
            with self.subTest(errno=code):
                def failed(real, name, *args, dir_fd=None, **kwargs):
                    if name == artifact.name and dir_fd is not None:
                        directory = os.fstat(dir_fd)
                        if (directory.st_dev, directory.st_ino) == (parent.st_dev, parent.st_ino):
                            raise OSError(code, os.strerror(code), name)
                    return real(name, *args, dir_fd=dir_fd, **kwargs)

                with patch.object(state_files, "os", FakeOs(open=failed)):
                    with self.assertRaises(state_files.StateReadError) as caught:
                        bound_bytes(files, source, "测试")
                error = caught.exception
                stop = stops.of(error)
                self.assertEqual((stop["kind"], stop["reason"], stop["data"]["error_type"]),
                                 ("runtime_failure", "io", error_type))
                self.assertEqual(error.outcome.errno, code)
                self.assertIn(os.strerror(code), str(error))
                self.assertIn(str(artifact), str(error))

    def test_legacy_path_read_for_held_view(self):
        with self.assertRaises(EvidenceError):
            read_bound_bytes({"artifact": str(self.repo / "gone.json"), "artifact_digest": "a" * 64}, "测试")
        locked = self.repo / "locked.json"
        locked.write_text("{}")
        with patch.object(Path, "read_bytes", side_effect=PermissionError(errno.EACCES, "denied")):
            with self.assertRaises(PermissionError) as caught:
                read_bound_bytes({"artifact": str(locked)}, "测试")
        self.assertEqual(stops.of(caught.exception)["kind"], "runtime_failure")


class LoadFailureClasses(RepoCase):
    def blocked_run(self):
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                result.update(status="blocked", blocker_kind="operator", blockers=["Proceed?"])
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        return backend, engine

    def resume_error(self, backend, run_id):
        with self.assertRaises(BaseException) as caught:
            self.engine(backend).resume(run_id)
        return stops.of(caught.exception)

    def test_worker_input_chain(self):
        from test_state_files import FakeOs

        backend, engine = self.blocked_run()
        packet = worker_packet(engine.run)
        parent = packet.parent.stat()

        def denied(real, name, *args, dir_fd=None, **kwargs):
            if name == packet.name and dir_fd is not None:
                directory = os.fstat(dir_fd)
                if (directory.st_dev, directory.st_ino) == (parent.st_dev, parent.st_ino):
                    raise PermissionError(errno.EACCES, "Permission denied", str(packet))
            return real(name, *args, dir_fd=dir_fd, **kwargs)

        with patch.object(state_files, "os", FakeOs(open=denied)):
            value = self.resume_error(backend, engine.run["id"])
        self.assertEqual((value["kind"], value["reason"]), ("runtime_failure", "io"))
        self.assertEqual(stops.project(value, "run_x")["exits"], [])
        self.assertNotIn("恢复原字节", stops.REGISTRY[("runtime_failure", "io")]["title"])
        raw = packet.read_bytes()
        for name, damage in (("missing", packet.unlink), ("changed", lambda: packet.write_text("{}\n"))):
            with self.subTest(case=name):
                damage()
                value = self.resume_error(backend, engine.run["id"])
                self.assertEqual((value["kind"], value["reason"]), ("evidence", "load_integrity"))
                packet.write_bytes(raw)

    def test_checkpoint_chain(self):
        from test_state_files import FakeOs

        case = _checkpoints.CheckpointDeclarationFailureTests
        self.declaration = case.declaration.__get__(self)

        def keep_valid(items, kwargs):
            good = self.declaration(kwargs["cwd"], bad=False)
            good["id"] = "first-valid"
            items[:] = [good]
        backend = case.backend(self, mutate=keep_valid)
        engine = self.engine(backend)
        result = engine.start("Implement greet", plan())
        checkpoint, = result["task_checkpoints"]
        target = Path(checkpoint["artifact"])
        parent = target.parent.stat()
        mode = target.stat().st_mode
        raw = target.read_bytes()

        def denied(real, name, *args, dir_fd=None, **kwargs):
            if name == target.name and dir_fd is not None:
                directory = os.fstat(dir_fd)
                if (directory.st_dev, directory.st_ino) == (parent.st_dev, parent.st_ino):
                    raise PermissionError(errno.EACCES, "Permission denied", str(target))
            return real(name, *args, dir_fd=dir_fd, **kwargs)

        with patch.object(state_files, "os", FakeOs(open=denied)):
            value = self.resume_error(backend, engine.run["id"])
        self.assertEqual((value["kind"], value["reason"]), ("runtime_failure", "io"))
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(target.stat().st_mode, mode)
        self.addCleanup(target.write_bytes, raw)
        target.write_bytes(raw + b"x")
        value = self.resume_error(backend, engine.run["id"])
        self.assertEqual((value["kind"], value["reason"]), ("evidence", "load_integrity"))


    def test_checkpoint_source_original_chain(self):
        """The accepted worker result a registered checkpoint rests on is changed (code-4b 1).

        Two tasks, the first accepted with a valid checkpoint, then ``--step`` pauses: only the
        checkpoint's source check reads that result at load. A load failure, no exit listed.
        """
        from test_dispatch_events import two_tasks
        case = _checkpoints.CheckpointDeclarationFailureTests
        self.declaration = case.declaration.__get__(self)

        def keep_valid(items, kwargs):
            good = self.declaration(kwargs["cwd"], bad=False)
            good["id"] = "first-valid"
            items[:] = [good]
        backend = case.backend(self, mutate=keep_valid)
        engine = self.engine(backend)
        saved = engine.start("Implement greet twice", two_tasks(), step=True)
        self.assertEqual((saved["status"], len(saved["task_checkpoints"])), ("paused", 1))
        original = engine.directory / "dispatches" / saved["checkpoint_batches"][0]["dispatch_id"] / "returned-result.json"
        raw = original.read_bytes()
        original.write_bytes(raw.replace(b"}", b" }", 1))  # the same JSON, other bytes
        before = (engine.directory / "state.json").read_bytes()
        for command in ("resume", "abandon"):
            with self.subTest(command=command):
                notice = refused_by_cli(self, backend, [command, saved["id"]], ("evidence", "load_integrity"), 1)
                no_command_exit(self, notice, ("evidence", "load_integrity"))
                self.assertEqual(notice["source"]["artifact"], str(original))
        self.assertEqual((engine.directory / "state.json").read_bytes(), before)
        original.write_bytes(raw)  # restoring the bytes is the way on
        self.assertEqual(run_cli(self, backend, ["resume", saved["id"]])[0], 0)

    def test_inconsistent_continuation_pointer_is_a_load_failure(self):
        """A ledger whose pending continuation names no recorded request: load_integrity, not an invariant stop."""
        backend, engine = self.blocked_run()
        path = engine.directory / "state.json"
        ledger = read_json(path)
        ledger["pending_continuation"] = "continuations_" + "0" * 64
        write_json(path, ledger)
        for command in ("resume", "abandon"):
            with self.subTest(command=command):
                notice = refused_by_cli(self, backend, [command, ledger["id"]], ("evidence", "load_integrity"), 1)
                no_command_exit(self, notice, ("evidence", "load_integrity"))

    def test_rejected_continuation_made_pending_again_is_a_load_failure(self):
        """A ledger that points its pending continuation back at a rejected one: load_integrity (code-4b 1 census)."""
        from test_continuation_rejection import RejectionLifecycleTests
        engine, backend, path, _, _ = RejectionLifecycleTests.waiting(self, second_request=True)
        self.assertEqual(run_cli(self, backend, ["resume", engine.run["id"], "--reject-request", str(path)])[0], 3)
        state = engine.directory / "state.json"
        ledger = read_json(state)
        rejected = ledger["continuation_rejections"][0]["continuation_id"]
        self.assertNotEqual(ledger["pending_continuation"], rejected)
        ledger["pending_continuation"] = rejected
        write_json(state, ledger)
        notice = refused_by_cli(self, backend, ["resume", engine.run["id"]], ("evidence", "load_integrity"), 1)
        no_command_exit(self, notice, ("evidence", "load_integrity"))

# ---- G17: settling (spec 2.2, 4.4) ----------------------------------------------------------
class SettledStops(RepoCase):
    allow_unclassified = True  # one case refuses with an injected untyped error, then settles
    blocked_run = LoadFailureClasses.blocked_run
    def test_preflight_sees_a_packet_replaced_after_the_locked_load(self):
        backend, engine = self.blocked_run()
        path = engine.directory / "state.json"
        before = path.read_bytes()
        packet = worker_packet(read_json(path))
        original = RunContext.confirm_exited

        def replace_then_confirm(context, *args, **kwargs):
            packet.write_text("{}\n")  # after the locked load, before the link check
            return original(context, *args, **kwargs)
        with patch.object(RunContext, "confirm_exited", replace_then_confirm), self.assertRaises(HarnessError) as caught:
            self.engine(backend).resume(engine.run["id"])
        value = stops.of(caught.exception)
        self.assertEqual((value["kind"], value["reason"]), ("evidence", "load_integrity"))
        self.assertIn("superseded", value["data"])
        self.assertEqual(stops.project(value, "run_x")["exits"], [])
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(refused(self, engine.run["id"]))

    def test_abandon_refusal_after_the_load_is_settled_too(self):
        backend, engine = self.blocked_run()
        packet = worker_packet(read_json(engine.directory / "state.json"))

        def replace_then_refuse(context, *args, **kwargs):
            packet.write_text("{}\n")  # after abandon's locked load
            raise HarnessError("refused after the load")
        with patch.object(RunContext, "confirm_exited", replace_then_refuse), self.assertRaises(HarnessError) as caught:
            self.engine(backend).abandon(engine.run["id"])
        value = stops.of(caught.exception)
        self.assertEqual((value["kind"], value["reason"]), ("evidence", "load_integrity"))
        self.assertEqual(value["data"]["superseded"]["kind"], "unclassified")

    def test_incomplete_process_with_a_damaged_packet_settles_as_load_integrity(self):
        backend = ScriptedBackend()
        original = backend.invoke

        def invoke(**kwargs):
            if kwargs["role"] != "worker":
                return original(**kwargs)
            (Path(kwargs["job"]) / "packet.json").write_text("{}\n")
            error = ProcessTimeout(1, Path(kwargs["job"]) / "process")
            error.outcome = ProcessOutcome(started=True, exit_confirmed=True, errors=[failure("execution", error)],
                                           cleanup_errors=[failure("sigkill", PermissionError())])
            raise error
        backend.invoke = invoke
        engine = self.engine(backend)
        with self.assertRaises(HarnessError) as caught:
            engine.start("Implement greet", plan())
        ledger = read_json(engine.directory / "state.json")["stop"]
        self.assertEqual((ledger["kind"], ledger["reason"]), ("evidence", "load_integrity"))
        # A worker's failure is judged (spec-5 2.7): the judgment's own load check finds the
        # damaged packet first, and settling turns that evidence stop into the load failure.
        self.assertEqual(ledger["data"]["superseded"], {"kind": "evidence", "reason": "runtime_integrity"})
        self.assertEqual(stops.of(caught.exception)["reason"], "load_integrity")
        self.assertTrue(refused(self, engine.run["id"]))

    def test_an_interrupt_is_not_settled(self):
        backend, engine = self.blocked_run()
        state = {"interrupted": False, "checks_after": 0}
        original = RunLifecycle.load_checks

        def checks(self_):
            if state["interrupted"]:
                state["checks_after"] += 1
            return original(self_)

        def interrupt(context, *args, **kwargs):
            state["interrupted"] = True
            raise KeyboardInterrupt
        with patch.object(RunLifecycle, "load_checks", checks), patch.object(RunContext, "confirm_exited", interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.engine(backend).resume(engine.run["id"])
        self.assertEqual((state["interrupted"], state["checks_after"]), (True, 0))


# ---- G8: the exit table in docs/recovery.md, the registry and the row tests agree ------------
EXIT_TABLE_HEADER = "| 行标识 |"
NO_COMMAND, NOT_A_STOP, OPERATION_MARK = "无命令出口", "非停止", "（操作）"
EXIT_ITEM = re.compile(r"(?:〔`(!?[a-z_]+)`〕)?`([^`]+)`")


def table_cells(line):
    """Split one Markdown table row on the pipes outside code spans."""
    cells, current, code = [], "", False
    for char in line.strip()[1:-1]:
        if char == "`":
            code = not code
        if char == "|" and not code:
            cells.append(current.strip())
            current = ""
        else:
            current += char
    cells.append(current.strip())
    return cells


def exit_table(text):
    """The exit table's rows by their structure: {row id: (stop or None, operation, exit code, exits)}.

    ``exits`` is a list of (template, condition) pairs, [] for "无命令出口" and None for a
    row that is not a stop. Only the identifier, type, exit code and command columns are read.
    """
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(EXIT_TABLE_HEADER))
    rows, problems = {}, []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        cells = table_cells(line)
        if len(cells) != 6:
            problems.append(("table row shape", line[:60]))
            continue
        ident, kind, code, _, _, commands = cells
        match = re.fullmatch(r"`([a-z0-9-]+)`", ident)
        if match is None or match.group(1) in rows:
            problems.append(("row identity", ident))
            continue
        if kind == NOT_A_STOP:
            stop, operation = None, False
        else:
            found = re.match(r"`([a-z_]+)/([a-z_]+)`", kind)
            stop, operation = (found.groups() if found else ("?", kind)), kind.endswith(OPERATION_MARK)
        if commands == NO_COMMAND:
            exits = []
        elif commands == "—":
            exits = None
        else:
            items = commands.split("；")
            parsed = [EXIT_ITEM.fullmatch(item) for item in items]
            exits = [(m.group(2), m.group(1)) for m in parsed if m]
            if len(exits) != len(items):
                problems.append(("exit commands", ident, commands))
        rows[match.group(1)] = (stop, operation, int(code) if code.isdigit() else code, exits)
    return rows, problems


def registry_exits(key, registry=None):
    """The registry's exit templates as the document writes them: ``{run}`` shown as ``RUN``."""
    entry = (registry or stops.REGISTRY)[key]
    pairs = [(item, None) if isinstance(item, str) else item for item in entry["exits"]]
    return [(template.replace("{run}", "RUN"), flag) for template, flag in pairs]


def binding_calls(row_test):
    """(helper name, row) for every binding helper the row test (or a same-class helper it calls) makes."""
    module, cls, name = row_test.split(".")
    tree = ast.parse((Path(__file__).parent / f"{module}.py").read_text(encoding="utf-8"))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls]
    methods = {item.name: item for node in classes for item in node.body if isinstance(item, ast.FunctionDef)}
    if not name.startswith("test") or name not in methods:
        return None
    bodies = [methods[name]] + [methods[call.func.attr] for call in ast.walk(methods[name])
                                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                                and isinstance(call.func.value, ast.Name) and call.func.value.id == "self"
                                and call.func.attr in methods and call.func.attr != name]
    found = set()
    for body in bodies:
        for call in ast.walk(body):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in BINDERS:
                rows = [ast.literal_eval(arg) for arg in call.args if isinstance(arg, ast.Tuple)]
                found |= {(call.func.id, row) for row in rows} or {(call.func.id, None)}
    return found


BINDERS = ("walk_exit", "walk_notice_exit", "refused_by_cli", "no_command_exit", "continues_without_stop")


def exit_table_problems(text, coverage, registry, calls_of):
    """Everything that makes the document, the registry and the row tests disagree."""
    rows, problems = exit_table(text)
    for ident in sorted(set(rows) - set(coverage)):
        problems.append(("documented row without a row test", ident))
    for ident in sorted(set(coverage) - set(rows)):
        problems.append(("row test without a documented row", ident))
    documented = set()
    for ident in sorted(set(rows) & set(coverage)):
        stop, operation, code, exits = rows[ident]
        row = coverage[ident]
        if stop != (tuple(row.stop) if row.stop else None):
            problems.append(("type differs from the mapping", ident, stop, row.stop))
            continue
        if stop is None:
            if (code, exits, operation) != ("—", None, False):
                problems.append(("a non-stop row lists an exit code or exits", ident))
            required = [("continues_without_stop", None)]
        else:
            documented.add(stop)
            if stop not in registry:
                problems.append(("type not registered", ident, stop))
                continue
            if code != registry[stop]["exit_code"]:
                problems.append(("exit code differs from the registry", ident, code, registry[stop]["exit_code"]))
            if exits != registry_exits(stop, registry):
                problems.append(("exit commands differ from the registry", ident, exits, registry_exits(stop, registry)))
            if operation != (row.boundary == "operation"):
                problems.append(("boundary differs from the mapping", ident))
            listed = bool(registry_exits(stop, registry))
            if row.boundary == "operation":
                required = [("refused_by_cli", stop)] + ([("walk_notice_exit", stop)] if listed else [])
            else:
                required = [("walk_exit" if listed else "no_command_exit", stop)]
        calls = calls_of(row.test)
        if calls is None:
            problems.append(("row test not found", ident, row.test))
            continue
        for helper, value in required:
            if (helper, value) not in calls:
                problems.append(("row test does not run its exit", ident, row.test, helper))
    for key in sorted(set(registry) - documented):
        problems.append(("registered type without a documented row", key))
    return problems


class ExitTableGuard(unittest.TestCase):
    def setUp(self):
        from test_stop_exits import ROW_COVERAGE
        self.coverage = ROW_COVERAGE
        self.text = (ROOT / "docs" / "recovery.md").read_text(encoding="utf-8")

    def test_document_registry_and_row_tests_agree(self):
        self.assertEqual(exit_table_problems(self.text, self.coverage, stops.REGISTRY, binding_calls), [])

    def test_the_guard_sees_each_kind_of_disagreement(self):
        def mutated_calls(target, helper):
            def calls(test):
                found = binding_calls(test)
                return {item for item in found if item[0] != helper} if test == target else found
            return calls
        rows, _ = exit_table(self.text)
        line = next(line for line in self.text.splitlines() if line.startswith("| `busy` |"))
        cases = {
            "exit code": (self.text.replace(line, line.replace("| 3 |", "| 1 |")), stops.REGISTRY, binding_calls),
            "exit command": (self.text.replace("`abandon RUN`；〔`question_registered`〕", "〔`question_registered`〕", 1),
                             stops.REGISTRY, binding_calls),
            "unregistered type": (self.text, {key: value for key, value in stops.REGISTRY.items() if key[0] != "busy"},
                                  binding_calls),
            "registered type without a row": (self.text, {**stops.REGISTRY, ("busy", "other"): stops.REGISTRY[("busy", "run_lock")]},
                                              binding_calls),
            "exit action removed": (self.text, stops.REGISTRY,
                                    mutated_calls(self.coverage["task-attempts"].test, "walk_exit")),
            "refusal not observed": (self.text, stops.REGISTRY,
                                     mutated_calls(self.coverage["config-changed"].test, "refused_by_cli")),
            "row dropped from the document": (self.text.replace(line + "\n", ""), stops.REGISTRY, binding_calls),
        }
        self.assertIn("busy", rows)
        for name, (text, registry, calls) in cases.items():
            with self.subTest(case=name):
                self.assertTrue(exit_table_problems(text, self.coverage, registry, calls), name)


# ---- G1: a preflight refusal never rewrites the run (spec 6.3) ------------------------------------
class PreflightRefusals(RepoCase):
    """Every kind of preflight refusal, through the real CLI: its type and exit code, and the ledger untouched.

    Refusals before the first write leave state.json byte-identical; after an explicitly
    audited --confirm-exited only that confirmation (and the store revision) may change,
    never the status or the stop. No refusal calls a model.
    """

    def operator_run(self):
        import shutil
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.rules = Path(folder.name) / "RULES.md"
        shutil.copyfile(ROOT / "shared" / "RULES.md", self.rules)
        self.config.data["shared"]["rules"] = str(self.rules)

        def ask_operator(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="operator", blockers=["Which greeting?"])
        backend = ScriptedBackend(ask_operator)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        return backend, engine

    def files(self, request):
        folder = scratch(self)
        answer = {"schema_version": 1, "request_id": request["id"], "task_id": request["task_id"],
                  "decision": "continue", "summary": "Use the documented greeting."}
        paths = {"continue": answer, "stop": {**answer, "decision": "stop"},
                 "stale": {**answer, "request_id": "request_" + "0" * 64},
                 "update": {"schema_version": 1}, "guidance": {"task_id": "greet"},
                 "rejection": {"request_id": "continuations_" + "0" * 64, "reason": "Not this request."}}
        for name, value in paths.items():
            write_json(folder / f"{name}.json", value)
        (folder / "broken.json").write_text("{not json")
        return {name: str(folder / f"{name}.json") for name in [*paths, "broken"]}

    def check(self, backend, engine, argv, row, code, *, audited=False, run_id=None):
        path = engine.directory / "state.json"
        before, calls = read_json(path), len(backend.calls)
        raw = path.read_bytes()
        result, errors = run_cli(self, backend, ["resume", run_id or engine.run["id"], *argv])
        notice = cli_notice(errors)
        self.assertIsNotNone(notice, errors)
        self.assertEqual((result, notice["kind"], notice["reason"]), (code, *row), errors)
        records = sorted((self.repo / ".harness" / "operations").glob("*.json"), key=lambda item: item.stat().st_mtime_ns)
        record = read_json(records[-1])
        self.assertEqual((record["exit_status"], record["stop"]["kind"], record["stop"]["reason"]), (code, *row))
        self.assertEqual(len(backend.calls), calls)  # no model call
        if not audited:
            self.assertEqual(path.read_bytes(), raw)
            return
        after = read_json(path)
        self.assertEqual((after["status"], after["stop"]), (before["status"], before["stop"]))

        def without_the_audit(run):
            run = {key: value for key, value in run.items() if key != "store_revision"}
            run["dispatches"] = [{key: value for key, value in entry.items()
                                  if key not in ("exit_confirmed", "operator_exit_confirmation")}
                                 for entry in run["dispatches"]]
            return run
        self.assertEqual(without_the_audit(after), without_the_audit(before))
        self.assertTrue(any(entry.get("operator_exit_confirmation") for entry in after["dispatches"]))

    def test_every_preflight_refusal_leaves_the_run_unchanged(self):
        from harness import git
        from test_state_files import held_lock
        backend, engine = self.operator_run()
        path = engine.directory / "state.json"
        request = read_json(path)["decision_requests"][-1]
        files = self.files(request)
        worker = next(d for d in read_json(path)["dispatches"] if d["role"] == "worker")
        plan_file = Path(read_json(path)["plan_history"][0]["artifact"])
        original = path.read_bytes()

        def unconfirmed():
            ledger = read_json(path)
            entry = next(d for d in ledger["dispatches"] if d["id"] == worker["id"])
            entry.update(exit_confirmed=False, pgid=None, process_state="running")
            write_json(path, ledger)

        def head_moved():
            git.git(engine.worktree, "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c",
                    "commit.gpgsign=false", "commit", "--allow-empty", "-m", "unexpected commit")

        def damage(target, data):
            return lambda: target.write_bytes(data)
        plan_raw, rules_raw = plan_file.read_bytes(), self.rules.read_bytes()
        model = self.config.data["roles"]["reviewer"]["model"]

        def set_model(value):
            return lambda: self.config.data["roles"]["reviewer"].update(model=value)

        def set_version(value):
            def apply():
                ledger = read_json(path)
                ledger["schema_version"] = value
                write_json(path, ledger)
            return apply
        reset_head = lambda: git.git(engine.worktree, "reset", "--soft", engine.run["expected_head"])
        cases = [
            # (name, setup, undo, argv, row, exit code, audited)
            ("--reverify with --environment-update", None, None, ["--reverify", "--environment-update", files["update"]],
             ("invalid_input", "incompatible_options"), 2, False),
            ("--execution-guidance with --operator-decision", None, None,
             ["--execution-guidance", files["guidance"], "--operator-decision", files["continue"]],
             ("invalid_input", "incompatible_options"), 2, False),
            ("--reject-request with --reverify", None, None, ["--reject-request", files["rejection"], "--reverify"],
             ("invalid_input", "incompatible_options"), 2, False),
            ("--reject-request with --scope", None, None, ["--reject-request", files["rejection"], "--scope", "x.py"],
             ("invalid_input", "incompatible_options"), 2, False),
            ("--environment-update with --scope", None, None, ["--environment-update", files["update"], "--scope", "x.py"],
             ("invalid_input", "incompatible_options"), 2, False),
            ("--scope entry of an unaccepted form", None, None, ["--scope", "x.py", "--scope=src/*.py"],
             ("invalid_input", "scope_pattern"), 2, False),
            ("--scope under a protected root", None, None, ["--scope", ".harness/**"],
             ("invalid_input", "scope_pattern"), 2, False),
            ("unreadable rejection file", None, None, ["--reject-request", files["broken"]],
             ("invalid_input", "input_schema"), 2, False),
            ("rejection of a request that is not pending", None, None, ["--reject-request", files["rejection"]],
             ("invalid_input", "stale_request"), 2, False),
            ("stop answer with --reverify, before the exit-confirmation audit", unconfirmed, None,
             ["--operator-decision", files["stop"], "--reverify", "--confirm-exited", worker["id"]],
             ("invalid_input", "incompatible_options"), 2, False),
            ("unreadable answer file", None, None, ["--operator-decision", files["broken"]],
             ("invalid_input", "input_schema"), 2, False),
            ("immutable configuration changed", set_model("changed-model"), set_model(model), [],
             ("config_mismatch", "immutable_config"), 2, False),
            ("shared assets changed", damage(self.rules, rules_raw + b"\nnew rule\n"), damage(self.rules, rules_raw), [],
             ("config_mismatch", "shared_assets"), 2, False),
            ("published plan damaged, before the exit-confirmation audit", lambda: (unconfirmed(), plan_file.unlink()),
             damage(plan_file, plan_raw), ["--confirm-exited", worker["id"]], ("evidence", "runtime_integrity"), 1, False),
            ("unknown --confirm-exited ID", None, None, ["--confirm-exited", "not-a-dispatch"],
             ("invalid_input", "cli_arguments"), 2, False),
            ("exit unconfirmed", unconfirmed, None, [], ("process_unconfirmed", "dispatch_exit"), 3, False),
            ("HEAD changed", head_moved, reset_head, [], ("workspace_mismatch", "head_changed"), 1, False),
            ("HEAD changed after an audited --confirm-exited", lambda: (unconfirmed(), head_moved()), reset_head,
             ["--confirm-exited", worker["id"]], ("workspace_mismatch", "head_changed"), 1, True),
            ("stale answer", None, None, ["--operator-decision", files["stale"]], ("invalid_input", "stale_request"), 2, False),
            ("unreadable environment update", None, None, ["--environment-update", files["broken"]],
             ("invalid_input", "input_schema"), 2, False),
            ("unsupported ledger version", set_version(18), None, [], ("unsupported_ledger", "ledger_version"), 2, False),
        ]
        for name, setup, undo, argv, row, code, audited in cases:
            with self.subTest(refusal=name):
                path.write_bytes(original)
                if setup:
                    setup()
                try:
                    self.check(backend, engine, argv, row, code, audited=audited)
                finally:
                    if undo:
                        undo()
        path.write_bytes(original)
        with self.subTest(refusal="run lock held"), held_lock(engine.directory / "run.lock"):
            self.check(backend, engine, [], ("busy", "run_lock"), 3)
        with self.subTest(refusal="invalid run ID"):
            self.check(backend, engine, [], ("invalid_input", "cli_arguments"), 2, run_id="run_bad-id")
        # The run was never changed: the documented answer exit still goes on from here.
        self.assertEqual(run_cli(self, backend, ["resume", engine.run["id"], "--operator-decision", files["continue"]])[0], 0)
        with self.subTest(refusal="completed run with a modifying option"):
            self.check(backend, engine, ["--confirm-exited", worker["id"]], ("invalid_input", "run_state"), 2)
        self.setUp()
        backend, engine = self.operator_run()
        self.assertEqual(run_cli(self, backend, ["abandon", engine.run["id"]])[0], 0)
        with self.subTest(refusal="abandoned run"):
            self.check(backend, engine, [], ("invalid_input", "run_state"), 2)

    def test_scope_boundary_refusals_leave_the_run_unchanged(self):
        """The first plan is held (spec-6): approving it, or rejecting a stale or moved proposal, writes nothing."""
        from harness.scope import proposal_request_id
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", scope=["src/**"], pause_after_plan=True)
        held = read_json(engine.directory / "state.json")
        folder = scratch(self)
        stale, current = folder / "stale.json", folder / "current.json"
        write_json(stale, {"request_id": "proposal_" + "0" * 64, "reason": "Not this proposal."})
        write_json(current, {"request_id": proposal_request_id(held, held["commander_receipts"]["commander:decide:run"]),
                             "reason": "Not this proposal."})
        self.check(backend, engine, ["--approve-plan"], ("invalid_input", "plan_unpublished"), 2)
        self.check(backend, engine, ["--approve-plan", "--scope", "hello.py"], ("invalid_input", "plan_unpublished"), 2)
        self.check(backend, engine, ["--reject-request", str(stale)], ("invalid_input", "stale_request"), 2)
        (engine.worktree / "hello.py").write_text("changed while held\n")
        self.check(backend, engine, ["--reject-request", str(current)], ("workspace_mismatch", "candidate_changed"), 1)

    def test_reverify_refuses_a_returned_decision_before_any_write(self):
        from test_harness import decide_bound

        def blocked_task(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="task", blockers=["Unclear"])
        backend = ScriptedBackend(blocked_task)
        engine = self.engine(backend)
        crash, fired = crash_after_save(engine, decide_bound)
        with crash, self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        self.assertTrue(fired["fired"])
        self.check(backend, engine, ["--reverify"], ("invalid_input", "run_state"), 2)


# ---- G10 and G7 see the approval publication and prepare-run (code-4b suggestion 2) ------------------
class GuardCoverage(DecideCase):
    def test_g10_sees_a_plan_awaiting_approval_published_over_a_load_failure(self):
        from harness.packets import PacketBuilder
        script, engine = ApprovalPublicationIsSettled.asked_while_planning(self)
        original = PacketBuilder.preflight_final_context

        def preflight_then_damage(builder, *args, **kwargs):
            result = original(builder, *args, **kwargs)
            ApprovalPublicationIsSettled.damage_the_registered_answer(builder.context)
            return result
        # Settling switched off in the test: G10 alone must notice the listed approval exit is blocked.
        with patch.object(PacketBuilder, "preflight_final_context", preflight_then_damage), \
                patch.object(RunLifecycle, "settle_publication", lambda *args, **kwargs: None):
            result = self.engine(script).resume(engine.run["id"], operator_decision=self.answer(self.state(engine)))
        self.assertEqual(result["status"], "awaiting_approval")
        findings = self.stop_guard_findings["mismatches"]
        self.assertIn(("exit listed but the load fails", "plan_approval", "initial_plan"), findings)
        findings.clear()

    def test_g7_sees_an_unclassified_prepare_run_failure(self):
        from harness import workspace_inputs
        engine = self.engine(ScriptedBackend())
        engine.start("Implement greet", plan(), pause_after_plan=True)
        manifest = scratch(self).resolve() / "manifest.json"
        write_json(manifest, {"files": []})
        # A failure raised by library code inside the operation (innermost frame json), not by the test.
        with patch.object(workspace_inputs, "manifest_shape", lambda ident, value: json.loads("{not json")):
            code, _ = run_cli(self, ScriptedBackend(), ["prepare-run", engine.run["id"], "--manifest", str(manifest)])
        self.assertEqual(code, 1)
        findings = self.stop_guard_findings["unclassified"]
        self.assertEqual([item[:2] for item in findings], [("unexpected_exception", "JSONDecodeError")])
        findings.clear()


# ---- A verification timeout whose cleanup failed is process evidence, not a timeout (code-4b 3) ------
class VerificationProcessFacts(RepoCase):
    def test_timeout_with_a_cleanup_failure_is_process_incomplete(self):
        from harness.util import run_process
        for scope in ("final", "greet"):
            with self.subTest(verification=scope):
                if scope != "final":
                    self.setUp()
                hit = {"n": 0}

                def time_out_with_a_cleanup_failure(argv, cwd, log, **kwargs):
                    if scope in Path(log).parts and hit["n"] == 0:
                        hit["n"] += 1
                        kwargs["on_start"](123)
                        for suffix in (".stdout", ".stderr"):
                            Path(log).with_suffix(suffix).write_text("")
                        error = ProcessTimeout(1, Path(log))
                        error.outcome = ProcessOutcome(
                            started=True, startup_state="started", pid=123, pgid=123, exit_code=-9,
                            termination_reason="timeout", exit_confirmed=True,
                            errors=[failure("execution", error)], cleanup_errors=[failure("sigkill", PermissionError())])
                        kwargs["on_outcome"](error.outcome)
                        raise error
                    return run_process(argv, cwd, log, **kwargs)
                backend = ScriptedBackend()
                with patch("harness.verification.run_process", side_effect=time_out_with_a_cleanup_failure):
                    code, errors = run_cli(self, backend, ["run", "Implement greet", "--plan", str(ROOT / "examples" / "plan.json")])
                notice = cli_notice(errors)
                self.assertEqual((code, notice["kind"], notice["reason"]), (1, "evidence", "process_incomplete"), errors)
                run_id = next((self.repo / ".harness" / "runs").iterdir()).name
                saved = read_json(self.repo / ".harness" / "runs" / run_id / "state.json")
                self.assertEqual((saved["stop"]["kind"], saved["stop"]["reason"]), ("evidence", "process_incomplete"))
                self.assertEqual(stops.project_ledger(saved)["exits"], [f"abandon {run_id}"])
                self.assertFalse(any(r.get("status") == "approved" for r in saved["final_history"]))


# ---- Every operator file input that cannot be read is this call's input refusal (code-4b 4) ------------
class FileInputRefusals(RepoCase):
    """Bad JSON, bad encoding and a missing file: invalid_input/input_schema, exit 2, the run not rewritten."""

    def broken_files(self):
        folder = scratch(self).resolve()  # canonical: the manifest check rejects symlinked paths first
        (folder / "bad-json.json").write_text("{ invalid")
        (folder / "bad-encoding.json").write_bytes(b"\xff\xfe{")
        return {"bad JSON": folder / "bad-json.json", "bad encoding": folder / "bad-encoding.json",
                "missing": folder / "missing.json"}

    def test_resume_inputs_are_refused_before_any_write(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        # A relaxed budget would be saved by this resume: the input must be refused before that write.
        self.config.data["runtime"]["max_agent_dispatches"] = 50
        state = engine.directory / "state.json"
        before = state.read_bytes()
        for option in ("--operator-decision", "--environment-update", "--execution-guidance", "--reject-request"):
            for name, path in self.broken_files().items():
                with self.subTest(option=option, file=name):
                    notice = refused_by_cli(self, backend, ["resume", engine.run["id"], option, str(path)],
                                            ("invalid_input", "input_schema"), 2)
                    self.assertEqual((notice["data"]["option"], state.read_bytes()), (option, before))

    def test_plan_file_is_refused_before_a_run_exists(self):
        for name, path in self.broken_files().items():
            with self.subTest(file=name):
                refused_by_cli(self, ScriptedBackend(), ["run", "Implement greet", "--plan", str(path)],
                               ("invalid_input", "input_schema"), 2)
                self.assertFalse((self.repo / ".harness" / "runs").exists())

    def test_prepare_run_manifest(self):
        engine = self.engine(ScriptedBackend())
        engine.start("Implement greet", plan(), pause_after_plan=True)
        state = engine.directory / "state.json"
        before = state.read_bytes()
        for name, path in self.broken_files().items():
            with self.subTest(file=name):
                notice = refused_by_cli(self, ScriptedBackend(), ["prepare-run", engine.run["id"], "--manifest", str(path)],
                                        ("invalid_input", "input_schema"), 2)
                self.assertEqual((notice["data"]["option"], state.read_bytes()), ("manifest", before))


# ---- The plan awaiting approval is settled like every other stop (code-4b 2) ------------------------
class ApprovalPublicationIsSettled(DecideCase):
    def asked_while_planning(self):
        def decide(script, trigger, packet):
            if trigger["kind"] == "plan" and "operator_answer" not in packet:
                return ask("Which interface should the plan use?")
            return None
        script = Script(decide)
        engine = self.engine(script)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", None, pause_after_plan=True)
        return script, engine

    @staticmethod
    def damage_the_registered_answer(context):
        Path(context.run["operator_decisions"][-1]["artifact"]).write_text("{}\n")  # a load-checked artifact

    def test_damage_before_the_approval_save_stops_the_run_instead(self):
        from harness.packets import PacketBuilder
        script, engine = self.asked_while_planning()
        original = PacketBuilder.preflight_final_context

        def preflight_then_damage(builder, *args, **kwargs):
            result = original(builder, *args, **kwargs)
            self.damage_the_registered_answer(builder.context)  # after the capacity check, before the save
            return result
        with patch.object(PacketBuilder, "preflight_final_context", preflight_then_damage):
            notice = refused_by_cli(self, script, ["resume", engine.run["id"], "--operator-decision",
                                                   self.answer(self.state(engine))], ("evidence", "load_integrity"), 1)
        saved = self.state(engine)
        self.assertEqual((saved["status"], saved["stop"]["kind"], saved["stop"]["reason"]),
                         ("blocked", "evidence", "load_integrity"))
        self.assertEqual(saved["stop"]["data"]["superseded"], {"kind": "plan_approval", "reason": "initial_plan"})
        self.assertEqual((notice["exits"], stops.project_ledger(saved)["exits"]), ([], []))
        self.assertTrue(refused(self, saved["id"]))

    def test_damage_before_the_approval_is_reported_again_is_a_load_failure(self):
        from harness.planning import Planner
        script, engine = self.asked_while_planning()
        code, errors = run_cli(self, script, ["resume", engine.run["id"], "--operator-decision",
                                              self.answer(self.state(engine))])
        self.assertEqual((code, self.state(engine)["status"]), (0, "awaiting_approval"), errors)
        before = (engine.directory / "state.json").read_bytes()
        original = Planner.report_command_requirements

        def report_then_damage(planner, *args, **kwargs):
            result = original(planner, *args, **kwargs)
            self.damage_the_registered_answer(planner.context)  # after the locked load, before the report
            return result
        with patch.object(Planner, "report_command_requirements", report_then_damage):
            notice = refused_by_cli(self, script, ["resume", engine.run["id"]], ("evidence", "load_integrity"), 1)
        self.assertEqual((notice["exits"], notice["data"]["superseded"]),
                         ([], {"kind": "plan_approval", "reason": "initial_plan"}))
        self.assertEqual((engine.directory / "state.json").read_bytes(), before)  # a refusal: the run is not rewritten


# ---- G16: interrupted after the plan was registered, before the awaiting-approval save ------
class PlanRegisteredBeforeApprovalSave(RepoCase):
    def test_plain_resume_reaches_awaiting_approval(self):
        from harness.run_store import RunStore
        original = RunStore.commit
        state = {"armed": True}

        def die_before_awaiting(store, *args, **kwargs):
            if state["armed"] and store.run.get("status") == "awaiting_approval":
                state["armed"] = False
                raise KeyboardInterrupt()
            return original(store, *args, **kwargs)
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with patch.object(RunStore, "commit", die_before_awaiting), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan(), pause_after_plan=True)
        saved = read_json(engine.directory / "state.json")
        self.assertEqual((saved["status"], saved["plan_approved"], bool(saved["tasks"])), ("interrupted", False, True))
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual((result["status"], result["stop"]["kind"]), ("awaiting_approval", "plan_approval"))
        self.assertEqual(backend.calls, [])




# ---- G11: a settled stop is one value in the ledger, stderr and the operation record ---------


class SettledStopIsOneValue(DecideCase):
    def test_answer_changed_during_judgment(self):
        def decide(script, trigger, packet):
            if trigger["kind"] != "task" or not blocked_by_task(trigger["feedback"]):
                return None
            if "operator_answer" not in packet:
                return ask()
            Path(packet["operator_answer"]["artifact"]).write_text("{}\n")  # a load-checked answer file
            return implement("a", instruction="continue a")
        script = Script(decide, block_first("a"))
        engine = self.engine(script)
        with self.assertRaises(HarnessError):
            engine.start("ask", {"tasks": [task("a")]})
        run = self.state(engine)
        commanders = len(script.of("commander"))
        code, errors = run_cli(self, script, ["resume", run["id"], "--operator-decision", str(self.answer(run))])
        ledger = stops.project_ledger(self.state(engine))
        self.assertEqual((ledger["kind"], ledger["reason"], code), ("evidence", "load_integrity", 1))
        self.assertEqual(ledger["data"]["superseded"], {"kind": "evidence", "reason": "runtime_integrity"})
        notice = json.loads([l for l in errors.splitlines() if l.startswith("[harness-json] ")][-1].split(" ", 1)[1])
        records = sorted((self.repo / ".harness" / "operations").glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
        record = read_json(records[-1])["stop"]
        for name, other in (("stderr", notice["stop"]), ("operation record", record)):
            with self.subTest(view=name):
                self.assertEqual((other["kind"], other["reason"], other["exits"]),
                                 (ledger["kind"], ledger["reason"], ledger["exits"]))
        self.assertEqual(ledger["exits"], [])
        self.assertEqual(len(script.of("commander")), commanders + 1)  # judged once, never redrawn
        self.assertTrue(refused(self, run["id"]))


# ---- G5: an answer together with --reverify (spec 9.2-9.4) -----------------------------------------

ANSWER_PREWRITTEN, ANSWERS_SAVED, RUNNING_SAVED, ATTEMPT_SAVED = (
    "answer file prewritten", "answers saved", "running saved", "new whole-run attempt saved")
SAME_COMMAND, REVERIFY_ALONE, PLAIN_RESUME = "same command", "--reverify alone", "plain resume"
DONE = (0, "completed", None, 1, ["verification_failed", "approved"], 0, 1, 1)
# window -> what landed: (status, answers in the ledger, answer files, whole-run attempts)
LANDED = {ANSWER_PREWRITTEN: ("blocked", 0, 1, 1), ANSWERS_SAVED: ("blocked", 1, 1, 1),
          RUNNING_SAVED: ("running", 1, 1, 1), ATTEMPT_SAVED: ("running", 1, 1, 2)}
# (window, recovery) -> (exit code, status, stop reason, answers, final history, extra decides,
#                        extra whole-run verification runs, extra model calls)
OUTCOMES = {
    (ANSWER_PREWRITTEN, SAME_COMMAND): DONE,
    (ANSWER_PREWRITTEN, REVERIFY_ALONE): (3, "blocked", "answer_required", 0, ["verification_failed"], 0, 0, 0),
    (ANSWER_PREWRITTEN, PLAIN_RESUME): (3, "blocked", "answer_required", 0, ["verification_failed"], 0, 0, 0),
    (ANSWERS_SAVED, SAME_COMMAND): DONE,
    (ANSWERS_SAVED, REVERIFY_ALONE): DONE,
    # A plain resume derives the ordinary decision, which now carries the answer (the demo commander asks again).
    (ANSWERS_SAVED, PLAIN_RESUME): (3, "blocked", "answer_required", 1, ["verification_failed"], 1, 0, 1),
    (RUNNING_SAVED, SAME_COMMAND): DONE,
    (RUNNING_SAVED, REVERIFY_ALONE): DONE,
    (RUNNING_SAVED, PLAIN_RESUME): (3, "blocked", "answer_required", 1, ["verification_failed"], 1, 0, 1),
    (ATTEMPT_SAVED, SAME_COMMAND): DONE,
    (ATTEMPT_SAVED, REVERIFY_ALONE): DONE,
    (ATTEMPT_SAVED, PLAIN_RESUME): DONE,
}


class AnswerWithReverify(RepoCase):
    def interrupted_at(self, engine, window):
        """A process death in ``window`` of ``resume --operator-decision A --reverify``; later saves are lost."""
        if window == ANSWER_PREWRITTEN:
            save = engine.context.save

            def die_before_the_answer_save(*args, **kwargs):
                if engine.run.get("operator_decisions"):
                    raise KeyboardInterrupt()
                return save(*args, **kwargs)
            return patch.object(engine.context, "save", side_effect=die_before_the_answer_save)
        predicate = {ANSWERS_SAVED: lambda run: bool(run["operator_decisions"]),
                     RUNNING_SAVED: lambda run: run["status"] == "running",
                     ATTEMPT_SAVED: lambda run: len(run["final_history"]) == 2}[window]
        return crash_after_save(engine, predicate)[0]

    def test_four_save_windows_by_three_recoveries(self):
        first = True
        for (window, recovery), expected in OUTCOMES.items():
            with self.subTest(window=window, recovery=recovery):
                if not first:
                    self.setUp()
                first = False
                backend, engine, request = final_failure_scenario(self)
                answer = final_answer(self, request)
                decides, calls = sum(c["kind"] == "decide" for c in backend.calls), len(backend.calls)
                runs = self.final_runs["runs"]
                resumed = self.engine(backend)
                with self.interrupted_at(resumed, window), self.assertRaises(KeyboardInterrupt):
                    resumed.resume(engine.run["id"], operator_decision=answer, reverify=True)
                landed = read_json(engine.directory / "state.json")
                files = list((engine.directory / "decisions").glob("decision_*.json"))
                self.assertEqual((landed["status"], len(landed["operator_decisions"]), len(files), len(landed["final_history"])),
                                 LANDED[window])
                self.assertEqual(len(backend.calls), calls)  # nothing was called before the death
                argv = {SAME_COMMAND: ["--operator-decision", str(answer), "--reverify"],
                        REVERIFY_ALONE: ["--reverify"], PLAIN_RESUME: []}[recovery]
                code, errors = run_cli(self, backend, ["resume", engine.run["id"], *argv])
                after = read_json(engine.directory / "state.json")
                stop = after.get("stop") or {}
                observed = (code, after["status"], stop.get("reason"), len(after["operator_decisions"]),
                            [r["status"] for r in after["final_history"]],
                            sum(c["kind"] == "decide" for c in backend.calls) - decides,
                            self.final_runs["runs"] - runs, len(backend.calls) - calls)
                self.assertEqual(observed, expected, errors)
                # Never registered twice: one answer file, one ledger row at most.
                self.assertEqual(len(list((engine.directory / "decisions").glob("decision_*.json"))), 1)
                if stop.get("reason") == "answer_required":
                    # The same unanswered question, or (after a new decision) the commander's new one.
                    self.assertEqual(stop["source"]["request_id"] == request["id"], not expected[5])
                    self.assertIs(stop["data"]["reverify_available"], True)  # the combined exit is still offered
                    self.assertIn(f"resume {after['id']} --operator-decision FILE --reverify", cli_notice(errors)["exits"])
                if expected[5]:
                    self.assertIn("operator_answer", [c for c in backend.calls if c["kind"] == "decide"][-1]["packet"])

    def test_answer_and_reverify_in_one_call(self):
        backend, engine, request = final_failure_scenario(self)
        decides = sum(c["kind"] == "decide" for c in backend.calls)
        result = self.engine(backend).resume(engine.run["id"], operator_decision=final_answer(self, request), reverify=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(c["kind"] == "decide" for c in backend.calls), decides)
        self.assertEqual([r["status"] for r in result["final_history"]], ["verification_failed", "approved"])
        self.assertEqual(len(result["operator_decisions"]), 1)

    def test_stop_answer_with_reverify_is_refused_before_any_write(self):
        backend, engine, request = final_failure_scenario(self)
        path = engine.directory / "state.json"
        before = path.read_bytes()
        with self.assertRaises(HarnessError) as caught:
            self.engine(backend).resume(engine.run["id"], operator_decision=final_answer(self, request, "stop"),
                                        reverify=True)
        value = stops.of(caught.exception)
        self.assertEqual((value["kind"], value["reason"]), ("invalid_input", "incompatible_options"))
        self.assertEqual(path.read_bytes(), before)

    def test_answers_are_read_once(self):
        backend, engine, request = final_failure_scenario(self)
        answer = final_answer(self, request)
        from harness import decisions as decisions_module
        original = decisions_module.read_decision
        reads = []

        def read_then_swap(path):
            value = original(path)
            reads.append(path)
            write_json(Path(path), {**value, "decision": "stop"})  # replaced right after the first read
            return value
        with patch.object(decisions_module, "read_decision", side_effect=read_then_swap):
            result = self.engine(backend).resume(engine.run["id"], operator_decision=answer, reverify=True)
        self.assertEqual((len(reads), result["status"]), (1, "completed"))
        self.assertEqual([d["decision"] for d in result["operator_decisions"]], ["continue"])

    def test_reverify_with_an_answer_does_not_bypass_a_whole_run_rejection(self):
        from test_runtime_changes import finding

        def reject_final(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review":
                result.update(verdict="request_changes", findings=[finding()])
        backend = ScriptedBackend(reject_final)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual((saved["stop"]["reason"], saved["stop"]["data"]["reverify_available"]), ("answer_required", False))
        self.assertNotIn("--reverify", " ".join(stops.project_ledger(saved)["exits"]))  # no combined exit after a rejection
        request = saved["decision_requests"][-1]
        reviews, decides = (sum(c["kind"] == kind for c in backend.calls) for kind in ("final_review", "decide"))
        code, errors = run_cli(self, backend, ["resume", saved["id"], "--operator-decision", str(final_answer(self, request)),
                                               "--reverify"])
        after = read_json(engine.directory / "state.json")
        self.assertEqual(len(after["operator_decisions"]), 1)  # the answer is registered
        self.assertEqual(sum(c["kind"] == "decide" for c in backend.calls), decides + 1)  # the derived decision
        self.assertEqual(sum(c["kind"] == "final_review" for c in backend.calls), reviews)  # not redrawn
        self.assertEqual([r["status"] for r in after["final_history"]], ["rejected"])
        self.assertEqual(code, 3, errors)


# ---- An ask landed while HEAD moved: the question is kept, the stop says HEAD changed ----------


class AskWithHeadChange(DecideCase):
    changing_script = _ask_recovery.AskCandidateChangeTests.changing_script

    def test_head_change_is_typed_with_the_registered_question(self):
        for head, expected in ((True, ("workspace_mismatch", "head_changed")),
                               (False, ("operator_decision", "answer_required"))):
            with self.subTest(head=head):
                script = self.changing_script(ask(), head=head)
                engine = self.engine(script)
                with self.assertRaises(HarnessError) as caught:
                    engine.start("ask", {"tasks": [task("a")]})
                saved = self.state(engine)
                stop = saved["stop"]
                self.assertEqual((stop["kind"], stop["reason"]), expected)
                self.assertEqual(stops.of(caught.exception)["reason"], expected[1])
                request = saved["decision_requests"][-1]
                if head:
                    self.assertIs(stop["data"]["question_registered"], True)
                    self.assertEqual(stop["source"]["request_id"], request["id"])
                    self.assertTrue(any(text.startswith("resume ") and "--operator-decision" in text
                                        for text in stops.project_ledger(saved)["exits"]))
                else:
                    self.assertIs(stop["data"]["candidate_changed"], True)


if __name__ == "__main__":
    unittest.main()
