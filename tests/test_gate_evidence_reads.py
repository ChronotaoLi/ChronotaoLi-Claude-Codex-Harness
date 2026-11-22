"""Shared recovery gates exercise the filesystem operations that decide state-file reads."""
import errno
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness import input_projection, path_evidence, state_files, stops, worker_returns
from harness.util import EvidenceError, HarnessError, read_json, write_json
from harness.worker_returns import LIMIT, SOURCES, inspect_return, validate_return_evidence
from test_harness import RepoCase, refused_by_cli, run_cli
from test_provider_failures import FailingWorker, FifoWatchdog, QUOTA, plan, worker
from test_state_files import FakeOs, failing


IO = ("runtime_failure", "io")
INTEGRITY = ("evidence", "runtime_integrity")
ENTRIES = ("resume", "abandon", "cleanup-run")


def stop_pair(error):
    notice = stops.of(error)
    return notice["kind"], notice["reason"]


class WorkerEvidenceEntryTests(RepoCase):
    def judged_run(self):
        backend = FailingWorker(self.config, self.repo, QUOTA)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError) as raised:
            engine.start("Implement greet", plan())
        self.assertEqual(stop_pair(raised.exception), ("provider_quota", "usage_limit"))
        state = read_json(engine.directory / "state.json")
        entry = worker(state)
        self.assertEqual(entry["worker_return_evidence"]["version"], 2)
        return backend, engine.directory, state["id"], engine.directory / "dispatches" / entry["id"]

    def check_entries(self, backend, directory, run_id, failure, hits):
        paths = (directory / "state.json", directory / "events.jsonl")
        original = tuple(path.read_bytes() for path in paths)
        calls = list(backend.calls)
        for entry in ENTRIES:
            with self.subTest(entry=entry), patch.object(state_files, "os", failure):
                hits.clear()
                notice = refused_by_cli(self, backend, [entry, run_id], IO, 1)
                self.assertEqual(notice["data"]["error_type"], "OSError")
                self.assertTrue(hits)
                self.assertEqual(tuple(path.read_bytes() for path in paths), original)
                self.assertEqual(backend.calls, calls)
        code, errors = run_cli(self, backend, ["cleanup-run", run_id])
        self.assertEqual(code, 0, errors)
        self.assertEqual(tuple(path.read_bytes() for path in paths), original)

    def test_dispatch_metadata_io_is_closed_at_each_entry_even_if_open_works(self):
        backend, directory, run_id, job = self.judged_run()
        parent = job.parent.stat()
        hits = []

        def dispatch_stat(real, name, *, dir_fd=None, follow_symlinks=True):
            if name == job.name and dir_fd is not None and not follow_symlinks:
                info = os.fstat(dir_fd)
                if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
                    hits.append(name)
                    raise OSError(errno.EIO, os.strerror(errno.EIO))
            return real(name, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

        self.check_entries(backend, directory, run_id, FakeOs(stat=dispatch_stat), hits)

    def test_worker_input_and_bound_verdict_reads_are_closed_at_each_entry(self):
        backend, directory, run_id, job = self.judged_run()
        for leaf, stage in (("packet.json", "open"), ("schema.json", "read"), ("process.stdout", "read")):
            with self.subTest(leaf=leaf, stage=stage):
                info = (job / leaf).stat()
                hits = []

                def open_evidence(real, name, *args, **kwargs):
                    if name == leaf and kwargs.get("dir_fd") is not None:
                        hits.append(name)
                        raise OSError(errno.EIO, os.strerror(errno.EIO))
                    return real(name, *args, **kwargs)

                def read_evidence(real, fd, *args):
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino):
                        hits.append(leaf)
                        raise OSError(errno.EIO, os.strerror(errno.EIO))
                    return real(fd, *args)

                failure = FakeOs(open=open_evidence) if stage == "open" else FakeOs(pread=read_evidence)
                self.check_entries(backend, directory, run_id, failure, hits)


class WorkerReturnReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.job = self.parent / "dispatch"
        self.job.mkdir()

    def test_only_the_three_inspected_files_retain_bytes_and_the_digest_covers_the_whole_file(self):
        for name in SOURCES:
            (self.job / name).write_bytes(b" " * LIMIT + name.encode())
        calls = []
        digest = state_files.StateFiles.digest

        def recorded(files, names, *, keep=0):
            outcome = digest(files, names, keep=keep)
            calls.append((names[0], keep, None if outcome.data is None else len(outcome.data)))
            return outcome

        with patch.object(state_files.StateFiles, "digest", recorded):
            result = inspect_return(self.job)
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["state"], "returned")
        inspected = {"process.stdout", "process.outcome.json", "provider-result.json"}
        self.assertEqual(calls, [(name, LIMIT, LIMIT + 1) if name in inspected else (name, 0, None)
                                 for name in SOURCES] + [(name, 0, None) for name in SOURCES])
        for name in SOURCES:
            self.assertEqual(result["sources"][name], hashlib.sha256((self.job / name).read_bytes()).hexdigest())

    def test_inspected_sources_at_the_limit_and_one_byte_over_keep_version_two_verdicts(self):
        terminal = b'{"type":"result","is_error":true}'
        contents = {"process.stdout": terminal, "process.outcome.json": b"{}", "provider-result.json": terminal}
        for name, data in contents.items():
            with self.subTest(source=name):
                for source, raw in contents.items():
                    (self.job / source).write_bytes(raw)
                leaf = self.job / name
                raw = data.ljust(LIMIT, b" ")
                leaf.write_bytes(raw)
                at_limit = inspect_return(self.job)
                self.assertEqual((at_limit["version"], at_limit["state"]), (2, "retryable"))
                leaf.write_bytes(raw + b" ")
                over = inspect_return(self.job)
                self.assertEqual((over["version"], over["state"], over["reason"]),
                                 (2, "unknown", "unreadable worker return evidence: ValueError"))
                self.assertEqual(over["sources"][name], hashlib.sha256(raw + b" ").hexdigest())
                validate_return_evidence(self.job, over)

    def test_absent_leaf_is_allowed_but_missing_dispatch_linked_ancestor_and_fifo_are_refused(self):
        version_two = inspect_return(self.job)
        self.assertEqual((version_two["version"], version_two["state"]), (2, "retryable"))
        self.assertEqual(version_two["sources"], dict.fromkeys(SOURCES))

        leaf = self.job / "process.stdout"
        os.mkfifo(leaf)
        watchdog = FifoWatchdog(leaf, bound=2)
        try:
            with self.assertRaises(EvidenceError) as raised:
                inspect_return(self.job)
            self.assertEqual(stop_pair(raised.exception), INTEGRITY)
        finally:
            self.assertFalse(watchdog.stop())
        leaf.unlink()

        alias = self.parent / "alias"
        alias.symlink_to(self.parent, target_is_directory=True)
        for job in (alias / self.job.name, self.parent / "missing"):
            with self.subTest(job=job), self.assertRaises(EvidenceError) as raised:
                inspect_return(job)
            self.assertEqual(stop_pair(raised.exception), INTEGRITY)

    def test_replacement_between_decision_read_and_digest_recheck_cannot_publish_a_verdict(self):
        leaf = self.job / "process.stdout"
        write_json(leaf, {"type": "result", "is_error": True})
        original = leaf.read_bytes()
        replacement = self.job / "replacement"
        write_json(replacement, {"type": "result", "structured_output": {}})
        opens = []

        def replace_after_first_open(real, name, *args, **kwargs):
            fd = real(name, *args, **kwargs)
            if name == leaf.name and not opens:
                opens.append(name)
                replacement.replace(leaf)
            return fd

        with patch.object(state_files, "os", FakeOs(open=replace_after_first_open)), \
                patch.object(worker_returns, "decode_terminal", wraps=worker_returns.decode_terminal) as decode, \
                self.assertRaises(EvidenceError):
            inspect_return(self.job)
        self.assertEqual(opens, [leaf.name])
        decode.assert_called_once_with(original)
        self.assertEqual(inspect_return(self.job)["state"], "returned")


class IndexReadbackTests(unittest.TestCase):
    def test_both_indexes_fail_closed_on_probe_and_readback_errors(self):
        cases = ((path_evidence, path_evidence.project_paths, {"changed_files": ["a" * 4100]}, "path"),
                 (input_projection, input_projection.project_history,
                  {"continuation_history": [{"id": "old", "reason": "a" * 4100}]}, "input"))
        for module, project, packet, kind in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary).resolve()
                [ref] = project(packet, directory)[kind + "_evidence_indexes"].values()
                path = Path(ref["artifact"])
                original = path.read_bytes()
                for stage, code in (("stat", errno.EIO), ("open", errno.EACCES)):
                    with self.subTest(stage=stage), patch.object(module, "write_json") as write, \
                            patch.object(state_files, "os", FakeOs(**{stage: failing(code, path.name)})):
                        with self.assertRaises(state_files.StateReadError) as raised:
                            project(packet, directory)
                        self.assertEqual((stop_pair(raised.exception), raised.exception.outcome.errno), (IO, code))
                        write.assert_not_called()
                        if stage == "open":
                            with self.assertRaises(state_files.StateReadError):
                                module.validate_indexes(directory, [ref])
                    self.assertEqual(path.read_bytes(), original)
                module.validate_indexes(directory, [ref])

    def test_publication_io_after_an_absent_index_still_leaves_as_io(self):
        packet = {"continuation_history": [{"id": "old", "reason": "a" * 4100}]}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            failure = FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))
            with patch.object(input_projection, "write_json", side_effect=failure), \
                    self.assertRaises(FileNotFoundError) as raised:
                input_projection.project_history(packet, directory)
            self.assertEqual(stop_pair(raised.exception), IO)
