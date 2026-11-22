"""Return and terminal recovery distinguish a missing leaf from damaged evidence."""
from contextlib import nullcontext
from copy import deepcopy
import errno
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from harness import state_files, stops
from harness.adapters import CLIBackend
from harness.run_context import RunContext
from harness.services import Services
from harness.util import EvidenceError, HarnessError
from test_provider_failures import FifoWatchdog


class ReceiptProbeTests(unittest.TestCase):
    def setUp(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        directory = state / "runs" / "run_probes"
        self.job = directory / "dispatches" / "dispatch"
        self.job.mkdir(parents=True)
        self.artifact = self.job / "returned-result.json"
        self.marker = self.job / "terminal-capture.json"
        self.receipt = {"dispatch_id": "dispatch", "artifact": str(self.artifact)}
        self.entry = {"id": "dispatch", "role": "commander", "process_state": "prepared"}
        self.run = {"id": directory.name, "dispatches": [self.entry], "agent_dispatches": 1,
                    "commander_receipts": {"commander:decide:run": self.receipt}, "review_receipts": {},
                    "worker_receipts": {}, "review_history": {}, "final_history": [{}]}
        self.config = SimpleNamespace(role=Mock(return_value={"cli": "claude"}))
        self.context = SimpleNamespace(run=self.run, state=state, directory=directory, config=self.config,
                                       commander="fable", ensure_exited=Mock(), save=Mock())
        self.services = Services(self.context)
        self.enterContext(patch.object(self.services.packets, "runtime_packet", side_effect=lambda packet, **_: packet))
        self.send = self.enterContext(patch.object(self.services.dispatch, "dispatch_once"))

    def fault(self, file, operation):
        """Inject a failure in one actual descriptor operation and count each hit."""
        hits = []
        self.addCleanup(lambda: self.assertTrue(hits, (file, operation)))
        directory = file.parent.stat()
        file_id = file.stat() if operation == "pread" else None
        real = getattr(os, operation)

        def fail(*args, **kwargs):
            if operation == "stat":
                descriptor = kwargs.get("dir_fd")
                matches = (descriptor is not None and args[0] == file.name
                           and os.path.samestat(os.fstat(descriptor), directory))
            elif operation == "scandir":
                matches = isinstance(args[0], int) and os.path.samestat(os.fstat(args[0]), directory)
            else:
                matches = os.path.samestat(os.fstat(args[0]), file_id)
            if matches:
                hits.append(args[0])
                raise OSError(errno.EIO, "synthetic descriptor fault")
            return real(*args, **kwargs)

        return patch.object(state_files.os, operation, fail)

    def assert_stop(self, call, kind, reason):
        with self.assertRaises(HarnessError) as caught:
            call()
        stop = stops.of(caught.exception)
        self.assertEqual((stop["kind"], stop["reason"]), (kind, reason))
        return caught.exception

    def test_each_returned_gate_preserves_the_intent_when_its_evidence_cannot_be_probed(self):
        dispatch = lambda: self.services.dispatch.dispatch("commander", "decide", "run", {}, {})
        discard = lambda: self.services.receipts.discard_unreturned("commander:decide:run")
        pending = {"task_id": "greet"}
        review = lambda: self.services.tasks.read_invalidated_review(pending, "reviewer:review:greet")
        for case in ("io", "fifo", "missing directory", "wrong reference"):
            with self.subTest(case=case):
                self.job.mkdir(exist_ok=True)
                self.receipt["artifact"] = str(self.artifact)
                injection = nullcontext()
                watchdog = None
                if case == "io":
                    injection = self.fault(self.artifact, "stat")
                elif case == "fifo":
                    os.mkfifo(self.artifact)
                    watchdog = FifoWatchdog(self.artifact, bound=5)
                elif case == "missing directory":
                    self.job.rmdir()
                else:
                    self.receipt["artifact"] = str(self.artifact.with_name("elsewhere.json"))
                try:
                    with injection:
                        self.run["review_receipts"]["reviewer:final_review:final"] = self.receipt
                        self.run["review_receipts"]["reviewer:review:greet"] = self.receipt
                        before = deepcopy(self.run)
                        for gate in (dispatch, discard, self.services.planning.decide_returned,
                                     self.services.finalization.leave_attempt, review):
                            with self.subTest(gate=gate):
                                self.assert_stop(gate, "runtime_failure" if case == "io" else "evidence",
                                                 "io" if case == "io" else "runtime_integrity")
                                self.assertEqual(self.run, before)
                                self.context.save.assert_not_called()
                                self.send.assert_not_called()
                finally:
                    if watchdog is not None:
                        self.assertFalse(watchdog.stop())
                        self.artifact.unlink()

    def test_missing_leaf_is_read_only_for_decide_and_allows_discard_after_exit_check(self):
        before = deepcopy(self.run)
        self.assertFalse(self.services.planning.decide_returned())
        self.assertEqual(self.run, before)
        self.context.save.assert_not_called()
        self.artifact.write_text("{}")
        self.assertTrue(self.services.planning.decide_returned())
        self.artifact.unlink()
        self.services.receipts.discard_unreturned("commander:decide:run")
        self.context.ensure_exited.assert_called_once()
        self.context.save.assert_called_once()
        self.assertEqual(self.run["commander_receipts"], {})
        self.assertEqual(self.run["agent_dispatches"], before["agent_dispatches"])

    def test_marker_probe_order_preserves_receipt_and_avoids_capture_on_io_failure(self):
        receipts = self.services.receipts
        self.entry["process_outcome"] = {"started": True, "termination_reason": "timeout", "exit_code": None}
        before = deepcopy(self.run)
        with self.fault(self.marker, "stat"), patch.object(CLIBackend, "preserve_terminal") as capture:
            for call in (lambda: receipts.has_terminal_capture(self.receipt), receipts.validate_pending_terminal_captures,
                         lambda: receipts.validate_terminal_capture(self.receipt)):
                self.assert_stop(call, "runtime_failure", "io")
            capture.assert_not_called()
        self.context.save.assert_not_called()
        self.assertEqual(self.run, before)

        self.receipt["terminal_capture_digest"] = "bound"
        self.entry["process_outcome"] = {"started": False}
        with patch.object(state_files.StateFiles, "probe", autospec=True,
                          side_effect=state_files.StateFiles.probe) as probe:
            self.assertTrue(receipts.has_terminal_capture(self.receipt))
            probe.assert_not_called()
            self.assert_stop(receipts.validate_pending_terminal_captures, "evidence", "process_incomplete")
            self.assertEqual([call.args[-1] for call in probe.call_args_list], [("terminal-capture.json",)])
        self.receipt.pop("terminal_capture_digest")
        self.entry.pop("process_outcome")
        with patch.object(receipts, "validate_terminal_capture") as validate:
            receipts.validate_pending_terminal_captures()
            validate.assert_not_called()

    def prepare_timeout(self, provider):
        self.config.role.return_value = {"cli": provider}
        terminal = ({"type": "result", "structured_output": {"answer": 42}}
                    if provider == "claude" else {"answer": 42})
        name = "process.stdout" if provider == "claude" else "response.json"
        (self.job / name).write_text(json.dumps(terminal))
        outcome = {"started": True, "termination_reason": "timeout", "exit_code": None}
        self.entry["process_outcome"] = outcome
        (self.job / "process.outcome.json").write_text(json.dumps(outcome))
        return name

    def test_both_recovery_gates_keep_the_receipt_when_original_pread_fails(self):
        receipts = self.services.receipts
        for provider in ("claude", "codex"):
            for gate in ("pending", "discard"):
                for file in ("terminal", "process.outcome.json"):
                    with self.subTest(provider=provider, gate=gate, file=file):
                        name = self.prepare_timeout(provider)
                        call = (receipts.validate_pending_terminal_captures if gate == "pending" else
                                lambda: receipts.discard_unreturned("commander:decide:run"))
                        before = deepcopy(self.run)
                        with self.fault(self.job / (name if file == "terminal" else file), "pread"):
                            error = self.assert_stop(call, "runtime_failure", "io")
                        self.assertEqual(error.outcome.step.stage, "read")
                        self.assertEqual(self.run, before)
                        self.assertFalse(self.marker.exists())
                        self.assertFalse(self.artifact.exists())
                        self.context.save.assert_not_called()
                        self.send.assert_not_called()

                        self.assert_stop(call, "evidence", "process_incomplete")
                        self.assertTrue(self.marker.is_file())
                        self.assertTrue(self.artifact.is_file())
                        self.assertEqual(self.run, before)
                        self.context.save.assert_not_called()
                        for generated in ("terminal-capture.json", "returned-result.json", "response.json",
                                          "provider-result.json", "process.stdout"):
                            (self.job / generated).unlink(missing_ok=True)

    def test_terminal_read_refuses_a_linked_dispatch_directory(self):
        self.prepare_timeout("claude")
        existing = self.job.with_name("ordinary-directory")
        self.job.rename(existing)
        self.job.symlink_to(existing, target_is_directory=True)
        before = {path.name: path.read_bytes() for path in existing.iterdir()}
        # The marker lookup can already have observed an absent leaf before a
        # fresh read finds that the parent is a link.
        missing = state_files.Outcome("absent", str(self.marker),
                                      state_files.Step(4, self.marker.name, "open", True))
        ledger = deepcopy(self.run)
        with patch.object(state_files.StateFiles, "probe", return_value=missing) as probe, \
                self.assertRaises(EvidenceError) as caught:
            self.services.receipts.validate_terminal_capture(self.receipt)
        probe.assert_called_once_with(("terminal-capture.json",))
        self.assertEqual(caught.exception.stop["reason"], "runtime_integrity")
        self.assertEqual({path.name: path.read_bytes() for path in existing.iterdir()}, before)
        self.assertEqual(self.run, ledger)
        self.context.save.assert_not_called()
        self.send.assert_not_called()

    def test_exit_gate_only_offers_resume_when_a_worker_return_cannot_be_confirmed(self):
        self.entry["process_state"] = "starting"
        self.run["worker_receipts"]["worker:implement:greet"] = self.receipt
        response = self.job / "response.json"
        response.write_text("{}")

        def exit_stop(expected):
            error = self.assert_stop(lambda: RunContext.ensure_exited(self.context),
                                     "process_unconfirmed", "dispatch_exit")
            stop = stops.of(error)
            self.assertEqual(stop["data"]["returned_unsealed"], expected)
            self.assertEqual(stops.project(stop, self.run["id"])["exits"],
                             ([] if expected else ["resume run_probes --confirm-exited dispatch"])
                             + ["abandon run_probes --confirm-exited dispatch"])

        exit_stop(True)
        seal = self.job / "worker-seal.fixture.json"
        seal.write_text("{}")
        exit_stop(False)
        seal.unlink()
        seal.symlink_to(response)
        exit_stop(True)
        seal.unlink()
        with self.fault(response, "scandir"):
            exit_stop(False)
        # Fail the descriptor-relative stat only during the probe, after listing.
        original = state_files.StateFiles.probe

        def probe(files, components):
            if components != ("response.json",):
                return original(files, components)
            with self.fault(response, "stat"):
                return original(files, components)

        with patch.object(state_files.StateFiles, "probe", probe):
            exit_stop(False)
