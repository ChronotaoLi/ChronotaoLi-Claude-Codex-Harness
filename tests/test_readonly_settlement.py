"""Commander/reviewer originals can be classified before resume writes anything."""
import errno
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from harness import state_files, stops
from harness.adapters import CLIBackend, DemoBackend
from harness.planning import DECIDE_KEY
from harness.util import HarnessError, ProcessTimeout, read_json, write_json
from test_harness import ROOT, RepoCase
from test_state_files import FakeOs


class TerminalCLI(DemoBackend):
    def __init__(self, config, state, role, provider, *, timeout=False):
        self.config, self.state = config, state
        self.role, self.provider, self.timeout = role, provider, timeout
        self.calls = []

    def invoke(self, **kwargs):
        role, job = kwargs["role"], kwargs["job"]
        self.calls.append((role, job))
        if role != self.role:
            return super().invoke(**kwargs)
        result = {"action": "ask", "question": "Synthetic question",
                  "dispatch_id": job.name, "task_id": "run"}
        if role == "reviewer":
            result = {"verdict": "request_changes", "dispatch_id": job.name, "task_id": "greet"}
        raw = json.dumps(result)
        if self.provider == "claude":
            raw = json.dumps({"type": "result", "structured_output": result})
            program = f"print({raw!r}, flush=True)\n"
        else:
            program = f"open({str(job / 'response.json')!r}, 'w').write({raw!r})\n"
        if self.timeout:
            program += "import time\ntime.sleep(30)\n"
        cli = CLIBackend(self.config, self.state)
        cli.command = lambda *args: [sys.executable, "-c", program]
        return cli.invoke(**kwargs)


def tree_bytes(directory):
    return {str(path.relative_to(directory)): ("dir" if path.is_dir() else path.read_bytes())
            for path in directory.rglob("*")}


def stop_pair(error):
    stop = stops.of(error)
    return stop["kind"], stop["reason"]


class ReadonlySettlementTests(RepoCase):
    def fixture(self, provider="codex", *, role="commander", timeout=False, final_plan=False):
        self.config.data["roles"][role]["cli"] = provider
        if timeout:
            self.config.data["runtime"]["agent_timeout_seconds"] = 1
        backend = TerminalCLI(self.config, self.repo / ".harness", role, provider, timeout=timeout)
        engine = self.engine(backend)
        command = ([read_json(ROOT / "examples" / "plan.json")]
                   if role == "reviewer" or final_plan else [])
        if timeout:
            with self.assertRaises(ProcessTimeout):
                engine.start("Synthetic recovery", *command)
        else:
            # A real stub wrote the original and the supervisor completed.
            # Interrupt at the adapter boundary before publishing derived files.
            with patch.object(CLIBackend, "completed_result", side_effect=KeyboardInterrupt), \
                    self.assertRaises(KeyboardInterrupt):
                engine.start("Synthetic recovery", *command)
        key = DECIDE_KEY if role == "commander" else "reviewer:review:greet"
        field = "commander_receipts" if role == "commander" else "review_receipts"
        receipt = engine.run[field][key]
        entry = next(d for d in engine.run["dispatches"] if d["id"] == receipt["dispatch_id"])
        return engine, backend, key, receipt, entry, Path(receipt["artifact"]).parent

    def rejected(self, engine, backend, expected, *, confirm=()):
        before = tree_bytes(engine.directory)
        calls = list(backend.calls)
        with self.assertRaises(HarnessError) as caught:
            self.engine(backend).resume(engine.run["id"], reverify=True, confirm_exited=confirm)
        self.assertEqual(stop_pair(caught.exception), expected)
        self.assertEqual(tree_bytes(engine.directory), before)
        self.assertEqual(backend.calls, calls)

    def test_normal_provider_originals_and_reverify_are_read_only(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                engine, backend, key, receipt, entry, job = self.fixture(provider)
                self.assertTrue(entry["process_outcome"]["evidence_complete"])
                terminal = job / ("response.json" if provider == "codex" else "process.stdout")
                healthy = terminal.read_bytes()
                original_value = json.loads(healthy)
                if provider == "claude":
                    original_value = original_value["structured_output"]
                cases = [(healthy, "recoverable"), (b"{broken", "invalid")]
                if provider == "codex":
                    cases.extend([(b"", "invalid"), (b'{"action":"revise"}', "recoverable")])
                else:
                    cases.extend([(b" \n", "unreturned"),
                                  (b'{"type":"result","is_error":true}', "unreturned")])
                for raw, kind in cases:
                    with self.subTest(raw=raw):
                        terminal.write_bytes(raw)
                        before = tree_bytes(engine.directory)
                        result = engine.services.receipts.settlement(key, capture=True)
                        self.assertEqual(result.kind, kind)
                        self.assertFalse(result.capturable)
                        self.assertEqual(result.raw, raw if kind in ("recoverable", "invalid") else None)
                        if raw == healthy:
                            self.assertEqual(result.value, original_value)
                        self.assertEqual(tree_bytes(engine.directory), before)
                        if kind != "unreturned":
                            self.rejected(engine, backend, ("invalid_input", "run_state"))
                terminal.unlink()
                if provider == "codex":
                    self.assertEqual(engine.services.receipts.settlement(key).kind, "unreturned")
                else:
                    self.rejected(engine, backend, ("evidence", "runtime_integrity"))
                terminal.mkdir()
                self.rejected(engine, backend, ("evidence", "runtime_integrity"))

    def test_record_absence_mismatch_refusal_and_io_are_distinct(self):
        engine, backend, key, receipt, entry, job = self.fixture()
        service = engine.services.receipts
        record = job / "process.outcome.json"
        facts = record.read_bytes()
        record.unlink()
        self.rejected(engine, backend, ("evidence", "runtime_integrity"))
        entry.pop("process_outcome")
        engine.context.save()
        self.assertIsNone(service.process_facts(entry))
        self.assertEqual(service.settlement(key).kind, "unreturned")
        for raw in (b"{", b"[]", b'{"duplicate":1,"duplicate":2}', b" " * (16 * 1024 * 1024 + 1)):
            record.write_bytes(raw)
            self.rejected(engine, backend, ("evidence", "runtime_integrity"))
        record.unlink()
        record.mkdir()
        self.rejected(engine, backend, ("evidence", "runtime_integrity"))
        record.rmdir()
        record.write_bytes(facts)
        saved_facts = read_json(record)
        self.assertEqual(service.process_facts(entry), saved_facts)
        entry["process_outcome"] = {**saved_facts, "exit_code": 5}
        engine.context.save()
        self.rejected(engine, backend, ("evidence", "process_incomplete"))
        entry["process_outcome"] = saved_facts
        engine.context.save()
        for failing in (record.name, "response.json"):
            def fault(real, name, flags, *args, **kwargs):
                if name == failing:
                    raise OSError(errno.EIO, "synthetic result read")
                return real(name, flags, *args, **kwargs)

            with patch.object(state_files, "os", FakeOs(open=fault)):
                self.rejected(engine, backend, ("runtime_failure", "io"))
        moved = job.with_name(job.name + "-moved")
        job.rename(moved)
        with self.assertRaises(HarnessError) as caught:
            service.settlement(key)
        self.assertEqual(stop_pair(caught.exception), ("evidence", "runtime_integrity"))

    def test_binding_and_marker_precede_originals(self):
        engine, backend, key, receipt, entry, job = self.fixture()
        service = engine.services.receipts
        marker = job / "terminal-capture.json"
        marker.write_bytes(b"{}")
        (job / "process.outcome.json").unlink()
        self.assertEqual(service.settlement(key).kind, "captured")
        self.rejected(engine, backend, ("invalid_input", "run_state"))
        marker.unlink()
        receipt["terminal_capture_digest"] = "a" * 64
        self.assertEqual(service.settlement(key).kind, "captured")
        artifact = Path(receipt["artifact"])
        artifact.write_bytes(b"{}")
        self.assertEqual(service.settlement(key).kind, "returned")
        artifact.unlink()
        receipt["artifact_digest"] = "b" * 64
        self.assertEqual(service.settlement(key).kind, "returned")

    def test_timeout_record_without_ledger_copy_blocks_then_captures_on_plain_resume(self):
        engine, backend, key, receipt, entry, job = self.fixture("claude", timeout=True)
        # Reconstruct the checkpoint before on_outcome saved the supervisor record
        # and before the adapter tried to capture an abnormal terminal.
        entry.pop("process_outcome")
        receipt.pop("terminal_capture_digest")
        receipt.pop("artifact_digest")
        for name in ("terminal-capture.json", "returned-result.json", "provider-result.json", "response.json"):
            (job / name).unlink()
        engine.context.save()
        terminal = job / "process.stdout"
        original = terminal.read_bytes()
        terminal.write_bytes(b"{partial\n")
        self.assertFalse(engine.services.receipts.settlement(key, capture=True).capturable)
        engine.services.lifecycle.verify_reverify()
        terminal.write_bytes(original)
        self.assertTrue(engine.services.receipts.settlement(key, capture=True).capturable)
        self.rejected(engine, backend, ("invalid_input", "run_state"))
        calls = list(backend.calls)
        with self.assertRaises(HarnessError) as caught:
            self.engine(backend).resume(engine.run["id"])
        self.assertEqual(stop_pair(caught.exception), ("evidence", "process_incomplete"))
        self.assertTrue((job / "terminal-capture.json").is_file())
        self.assertTrue((job / "returned-result.json").is_file())
        self.assertIn(key, read_json(engine.directory / "state.json")["commander_receipts"])
        self.assertEqual(backend.calls, calls)

    def test_bound_review_marker_missing_fails_before_republishing_originals(self):
        def fail_result_publication(path, value):
            if path.name == "returned-result.json":
                raise OSError(errno.EIO, "synthetic capture publication failure")
            write_json(path, value)

        with patch("harness.adapters.write_json", side_effect=fail_result_publication):
            engine, backend, key, receipt, entry, job = self.fixture("codex", role="reviewer", timeout=True)
        self.assertIn("terminal_capture_digest", receipt)
        self.assertNotIn("artifact_digest", receipt)
        self.assertFalse(Path(receipt["artifact"]).exists())
        (job / "terminal-capture.json").unlink()
        calls = list(backend.calls)
        original = (job / "response.json").read_bytes()
        for raw in (original, b'{"verdict":"approve"}'):
            with self.subTest(raw=raw):
                (job / "response.json").write_bytes(raw)
                before = tree_bytes(job)
                with self.assertRaises(HarnessError) as caught:
                    self.engine(backend).resume(engine.run["id"])
                self.assertEqual(stop_pair(caught.exception), ("evidence", "process_incomplete"))
                self.assertEqual(tree_bytes(job), before)
                self.assertIn(key, read_json(engine.directory / "state.json")["review_receipts"])
                self.assertEqual(backend.calls, calls)

    def test_unreturned_rows_allow_final_reverification(self):
        for case in ("missing Codex response", "not started", "partial abnormal terminal"):
            with self.subTest(case=case):
                counter = self.repo / ".harness" / f"counter-{case}"
                # A local verification command passes on the task, fails on
                # the first whole-run attempt, and passes on re-verification.
                program = ("from pathlib import Path\n"
                           f"counter = Path({str(counter)!r})\n"
                           "count = int(counter.read_text()) if counter.exists() else 0\n"
                           "counter.write_text(str(count + 1))\n"
                           "raise SystemExit(1 if count == 1 else 0)")
                self.config.data["verification"]["commands"] = [
                    {"cwd": ".", "argv": [sys.executable, "-c", program]}]
                engine, backend, key, receipt, entry, job = self.fixture(final_plan=True)
                confirm = []
                if case == "missing Codex response":
                    (job / "response.json").unlink()
                else:
                    record = job / "process.outcome.json"
                    facts = read_json(record)
                    if case == "not started":
                        facts.update(started=False, startup_state="not_started", pid=None, pgid=None, exit_code=None)
                        entry.update(exit_confirmed=False, process_state="starting", pgid=None)
                        confirm.append(entry["id"])
                    else:
                        facts.update(termination_reason="timeout", exit_code=7)
                        (job / "response.json").write_bytes(b"{partial")
                    write_json(record, facts)
                    entry["process_outcome"] = facts
                    engine.context.save()
                run = self.engine(backend).resume(engine.run["id"], reverify=True, confirm_exited=confirm)
                self.assertEqual(run["status"], "completed")
                if confirm:
                    confirmed = next(d for d in run["dispatches"] if d["id"] == confirm[0])
                    self.assertIn("operator_exit_confirmation", confirmed)
                self.assertEqual([item["status"] for item in run["final_history"]],
                                 ["verification_failed", "approved"])
                self.assertNotIn(key, run["commander_receipts"])
                self.assertEqual([role for role, _ in backend.calls].count("commander"), 1)

    def test_exit_gate_precedes_settlement(self):
        engine, backend, key, receipt, entry, job = self.fixture()
        (job / "response.json").unlink()
        record = job / "process.outcome.json"
        facts = read_json(record)
        facts.update(pid=os.getpgrp(), pgid=os.getpgrp(), exit_confirmed=False)
        write_json(record, facts)
        entry.pop("process_outcome")
        entry.update(exit_confirmed=False, process_state="started", pgid=None)
        engine.context.save()
        self.rejected(engine, backend, ("process_unconfirmed", "dispatch_exit"))
