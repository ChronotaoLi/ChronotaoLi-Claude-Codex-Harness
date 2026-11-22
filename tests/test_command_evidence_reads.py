"""Offline coverage of broker authority, prior receipts and command captures."""
import errno
import os
import sys
from pathlib import Path
from unittest.mock import patch

from harness import state_files
from harness.command_server import create_manifest
from harness.util import read_json, write_json
import test_delete_path as deletion
from test_command_admission import tree
from test_provider_failures import FifoWatchdog
from test_state_files import FakeOs, failing


class PriorEvidenceTests(deletion.BrokerFixture):
    CALLS = (("run_command", {"command_id": deletion.command_id(deletion.ECHO)}),
             ("delete_path", {"path": "legacy/tool.sh"}))

    def assert_refused(self, broker, prior):
        before = tree(self.job)
        for name, arguments in self.CALLS:
            with self.subTest(tool=name):
                blocked, result = deletion.call(broker, name, arguments)
                self.assertTrue(blocked, result)
                self.assertEqual((result["status"], result["started"]), ("blocked", False))
                self.assertEqual(result["error"], "Command evidence incomplete: " + prior["call_id"])
                self.assertEqual(tree(self.job), before)
                self.assertTrue((self.repo / "legacy/tool.sh").is_file())

    def test_prior_artifacts_refuse_fifo_links_and_io_failure_for_both_tools(self):
        for name in ("receipt.json", "delete-intent.json", "output.stdout", "output.stderr"):
            with self.subTest(artifact=name):
                broker = self.broker()
                if name == "delete-intent.json":
                    prior = broker.delete_path({"path": "legacy/old.txt"})
                    self.assertTrue(prior["deleted"], prior)
                else:
                    prior = broker.run_command(self.CALLS[0][1])
                    self.assertEqual(prior["exit_code"], 0)
                self.assertTrue(prior["evidence_complete"], prior)
                path = self.job / "commands" / prior["call_id"].rsplit(":", 1)[1] / name
                reads = FakeOs(open=failing(errno.EIO, name))
                with self.subTest(layout="EIO"), patch.object(state_files, "os", reads):
                    self.assert_refused(broker, prior)
                self.assertEqual(reads.count("open", name), 2)
                target = self.outside / name
                raw = path.read_bytes()
                path.replace(target)
                with self.subTest(layout="link"):
                    path.symlink_to(target)
                    self.assert_refused(broker, prior)
                    path.unlink()
                with self.subTest(layout="FIFO"):
                    os.mkfifo(path)
                    watchdog = FifoWatchdog(path, bound=3)
                    try:
                        self.assert_refused(broker, prior)
                    finally:
                        self.assertFalse(watchdog.stop())
                self.assertEqual(target.read_bytes(), raw)

    def test_receipt_paths_cannot_redirect_a_digest_to_identical_bytes_elsewhere(self):
        broker = self.broker()
        prior = broker.run_command(self.CALLS[0][1])
        path = Path(prior["stdout_path"])
        other = self.outside / path.name
        other.write_bytes(path.read_bytes())
        receipt = path.with_name("receipt.json")
        write_json(receipt, {**read_json(receipt), "stdout_path": str(other)})
        attempts = []

        def record(real, leaf, *args, dir_fd=None, **kwargs):
            if leaf == path.name:
                attempts.append(os.fstat(dir_fd).st_ino)
            return real(leaf, *args, dir_fd=dir_fd, **kwargs)

        with patch.object(state_files, "os", FakeOs(open=record)):
            self.assert_refused(broker, prior)
        self.assertEqual(attempts, [])  # A path mismatch is decided before opening any stdout.

        write_json(receipt, prior)
        with patch.object(state_files, "os", FakeOs(open=record)):
            result = broker.run_command(self.CALLS[0][1])
        self.assertTrue(result["evidence_complete"], result)
        self.assertIn(path.parent.stat().st_ino, attempts)
        self.assertNotIn(other.parent.stat().st_ino, attempts)

    def test_output_tail_is_bounded_and_a_diagnostic_read_fault_preserves_the_result(self):
        command = {"cwd": ".", "argv": [sys.executable, "-c",
                   "import sys; sys.stdout.buffer.write(b'A' * 40 + b'\\xff' + b'B' * 3999)"]}
        broker = self.broker(commands=(command,))
        arguments = {"command_id": deletion.command_id(command)}
        result = broker.run_command(arguments)
        self.assertEqual(result["stdout"], "[earlier output omitted; see log]\n\ufffd" + "B" * 3999)
        self.assertEqual(result["stdout_sha256"], deletion.sha(Path(result["stdout_path"]).read_bytes()))
        failed = []

        def read(real, fd, size, offset):
            if offset == 40:
                failed.append(offset)
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            return real(fd, size, offset)

        with patch.object(state_files, "os", FakeOs(pread=read)):
            result = broker.run_command(arguments)
        self.assertEqual(failed, [40])
        self.assertEqual(result["exit_code"], 0)
        self.assertNotIn("stdout", result)
        self.assertEqual(result["stdout_sha256"], deletion.sha(Path(result["stdout_path"]).read_bytes()))
        self.assertEqual(deletion.receipt_of(self.job, result), result)
        self.assertTrue(broker.run_command(arguments)["evidence_complete"])

    def test_manifest_probes_failure_before_publishing_the_lock_or_manifest(self):
        job = self.run_dir / "dispatches" / "refused"
        (job / "commands").mkdir(parents=True)
        before = tree(job)
        config = {"verification": {"commands": [deletion.ECHO]}, "permissions": {"worker_commands": []},
                  "runtime": {"verify_timeout_seconds": 5}}
        reads = FakeOs(stat=failing(errno.EIO, "commands.json"))
        with patch.object(state_files, "os", reads), self.assertRaises(state_files.StateReadError):
            create_manifest(config, self.repo, job)
        self.assertEqual(reads.count("stat", "commands.json"), 1)
        self.assertEqual(tree(job), before)
