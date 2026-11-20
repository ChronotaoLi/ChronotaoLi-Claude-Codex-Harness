"""The ledger's compare-and-swap store, and the run directory it reads and writes the ledger in (design 4, 3.2, 3.5).

Directory replacement follows probe R1: the run directory is renamed away and a byte-identical copy put at
its path, in the same process. FIFO cases end through FifoWatchdog, so a read that blocks fails instead of
hanging.
"""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import errno
import threading
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from harness import run_lifecycle, state_files, stops
from harness.run_lifecycle import RunLifecycle
from harness.run_store import RunStore
from harness.state_files import StateReadError
from harness.util import EvidenceError, HarnessError, read_json
from test_harness import ROOT, RepoCase
from test_provider_failures import FifoWatchdog
from test_state_files import FakeOs, failing, held_lock


def kind(error):
    value = stops.of(error)
    return value["kind"], value["reason"]


def swap(directory, moved):
    """Rename ``directory`` to ``moved`` and put a byte-identical copy at its path (probe R1)."""
    os.rename(directory, moved)
    shutil.copytree(moved, directory, symlinks=True)


@contextlib.contextmanager
def laid_out(path, layout, make):
    """``path`` replaced by ``make(path)`` for the block, then a regular file with its bytes again; yields the watchdog.

    Only a FIFO gets a FifoWatchdog: a reader stuck on it is recorded and released instead of hanging the test.
    """
    kept = path.read_bytes()
    path.unlink()
    make(path)
    watchdog = FifoWatchdog(path, bound=5) if layout == "fifo" else None
    try:
        yield watchdog
    finally:
        if watchdog is not None:
            watchdog.stop()
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        else:
            path.unlink()
        path.write_bytes(kept)


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RunStore(self.root)

    def state(self, ident="run_one"):
        return {"id": ident, "accepted": {}, "pending_acceptance": {"task_id": "one"}}

    def load(self, ident="run_one"):
        return json.loads((self.root / "runs" / ident / "state.json").read_text())

    def test_sequential_acceptance_and_post_accept_barriers(self):
        run = self.state()
        self.store.attach(run)
        self.assertEqual(self.store.commit(), 1)
        self.assertIn("pending_acceptance", self.load())
        run["accepted"]["one"] = {"commit": "abc"}
        run.pop("pending_acceptance")
        run["pending_post_accept"] = {"task_id": "one"}
        self.store.commit()
        saved = self.load()
        self.assertEqual(saved["store_revision"], 2)
        self.assertIn("one", saved["accepted"])
        self.assertIn("pending_post_accept", saved)

    def test_two_loaded_writers_cannot_overwrite(self):
        self.store.attach(self.state())
        self.store.commit()
        other = RunStore(self.root)
        other.attach(self.load())
        self.store.run["status"] = "accepted"
        self.store.commit()
        other.run["status"] = "blocked"
        with self.assertRaisesRegex(HarnessError, "其他写入者"):
            other.commit()
        self.assertEqual(self.load()["status"], "accepted")

    def test_competing_new_runs_cannot_replace_first_publication(self):
        self.store.attach(self.state())
        other = RunStore(self.root)
        other.attach(self.state())
        self.store.run["status"] = "planning"
        self.store.commit()
        other.run["status"] = "blocked"
        with self.assertRaises(HarnessError):
            other.commit()
        self.assertEqual(self.load()["status"], "planning")

    def test_deleted_source_is_not_recreated_by_stale_writer(self):
        self.store.attach(self.state())
        self.store.commit()
        path = self.root / "runs/run_one/state.json"
        path.unlink()
        with self.assertRaises(HarnessError):
            self.store.commit()
        self.assertFalse(path.exists())

    def test_concurrent_commits_publish_only_one_revision(self):
        self.store.attach(self.state())
        self.store.commit()
        other = RunStore(self.root)
        other.attach(self.load())
        barrier = threading.Barrier(2)

        def commit(store, status):
            store.run["status"] = status
            barrier.wait()
            try:
                store.commit()
                return status
            except HarnessError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(commit, self.store, "first")
            second = executor.submit(commit, other, "second")
            published = [result for future in (first, second) if (result := future.result())]
        self.assertEqual(len(published), 1)
        self.assertEqual(self.load()["status"], published[0])
        self.assertEqual(self.load()["store_revision"], 2)

    def test_external_edit_at_same_revision_rejected(self):
        self.store.attach(self.state())
        self.store.commit()
        path = self.root / "runs/run_one/state.json"
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(HarnessError, "其他写入者"):
            self.store.commit()

    def test_stale_attachment_does_not_adopt_new_source(self):
        self.store.attach(self.state())
        self.store.commit()
        stale = self.load()
        self.store.commit()
        with self.assertRaisesRegex(HarnessError, "来源不一致"):
            RunStore(self.root).attach(stale)

    def test_switching_runs_preserves_separate_revisions(self):
        self.store.attach(self.state())
        self.store.commit()
        self.store.attach(self.state("run_two"))
        self.assertEqual(self.store.commit(), 1)
        self.store.attach(self.load())
        self.assertEqual(self.store.commit(), 2)
        self.assertEqual(self.load("run_two")["store_revision"], 1)

    def test_write_failure_does_not_advance_expected_revision(self):
        self.store.attach(self.state())
        with patch.object(state_files.os, "replace", side_effect=OSError(errno.EIO, "I/O error")):
            with self.assertRaises(StateReadError) as caught:
                self.store.commit()
        self.assertEqual(kind(caught.exception), ("runtime_failure", "io"))
        self.assertNotIn("store_revision", self.store.run)
        self.assertFalse((self.root / "runs/run_one/state.json").exists())
        self.assertEqual(self.store.commit(), 1)

    def test_revision_and_identity_tampering_rejected(self):
        self.store.attach(self.state())
        self.store.commit()
        self.store.run["store_revision"] = 7
        with self.assertRaises(HarnessError):
            self.store.commit()
        self.store.run["store_revision"] = 1
        self.store.run["id"] = "run_other"
        with self.assertRaises(HarnessError):
            self.store.commit()


class DirectoryIdentityTests(unittest.TestCase):
    """The ledger is read and written only in the run directory this process first locked; a replacement stops it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "state"
        self.root.mkdir()  # the state directory exists before any store (ensure_private_state)
        self.directory = self.root / "runs" / "run_one"
        self.moved = Path(self.temp.name).resolve() / "moved"
        self.store = RunStore(self.root)

    def ledger(self, directory=None):
        return ((directory or self.directory) / "state.json").read_bytes()

    def committed(self):
        self.store.attach({"id": "run_one", "status": "planning"})
        self.store.commit()
        return self.ledger()

    def assertReplaced(self, call):
        with self.assertRaises(HarnessError) as caught:
            call()
        self.assertEqual(kind(caught.exception), ("runtime_failure", "invariant"))
        return caught.exception

    def test_a_directory_replaced_before_a_commit_is_refused_before_anything_is_written(self):
        before = self.committed()
        swap(self.directory, self.moved)
        self.assertReplaced(self.store.commit)
        self.assertReplaced(self.store.check_current)
        # Attaching the ledger again in this process finds the same replacement; the store keeps its selection.
        self.assertReplaced(lambda: self.store.attach(json.loads(self.ledger())))
        self.assertEqual(self.store.run["store_revision"], 1)
        self.assertEqual((self.ledger(self.moved), self.ledger()), (before, before))
        # A new process starts from the directory it first locks: the copy is its own, the moved one untouched.
        other = RunStore(self.root)
        other.attach(json.loads(self.ledger()))
        self.assertEqual(other.commit(), 2)
        self.assertEqual(self.ledger(self.moved), before)

    def test_a_directory_moved_while_the_ledger_is_replaced_keeps_the_write_and_stops_the_next_lock(self):
        before = self.committed()
        rename = os.replace

        def moving(*args, **kwargs):  # after the temporary file is written and fsynced, before the rename
            swap(self.directory, self.moved)
            return rename(*args, **kwargs)
        with patch.object(state_files.os, "replace", side_effect=moving):
            self.assertEqual(self.store.commit(), 2)
        self.assertEqual(json.loads(self.ledger(self.moved))["store_revision"], 2)
        self.assertEqual(self.ledger(), before)
        self.assertReplaced(self.store.check_current)
        self.assertReplaced(self.store.commit)
        self.assertEqual(self.ledger(), before)

    def test_only_a_new_runs_first_commit_makes_its_directory(self):
        self.store.attach({"id": "run_one"})
        self.assertEqual(os.listdir(self.root), [])  # selecting a new run takes no lock and makes nothing
        self.assertEqual(self.store.commit(), 1)
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ["state.json", "store.lock"])
        shutil.rmtree(self.directory)
        for call in (self.store.commit, self.store.check_current):
            with self.assertRaises(HarnessError) as caught:
                call()
            self.assertEqual(kind(caught.exception), ("runtime_failure", "invariant"))
            self.assertFalse(self.directory.exists())

    def test_a_run_lock_holder_binds_the_directory_the_store_locks(self):
        self.committed()
        info = os.stat(self.directory)
        self.store.bind_directory((info.st_dev, info.st_ino))
        elsewhere = os.stat(self.root)
        self.assertReplaced(lambda: self.store.bind_directory((elsewhere.st_dev, elsewhere.st_ino)))
        # A store that has locked nothing yet records what the holder locked, and its first lock must find it.
        fresh = RunStore(self.root)
        fresh.attach({"id": "run_two"})
        (self.root / "runs" / "run_two").mkdir()
        made = os.stat(self.root / "runs" / "run_two")
        fresh.bind_directory((made.st_dev, made.st_ino))
        swap(self.root / "runs" / "run_two", self.moved)
        self.assertReplaced(fresh.commit)
        self.assertEqual(os.listdir(self.root / "runs" / "run_two"), [])  # refused before anything is made there
        # Another run's ledger is a new selection: its directory is recorded afresh, and none is kept after None.
        self.store.attach({"id": "run_three"})
        self.store.bind_directory((elsewhere.st_dev, elsewhere.st_ino))
        self.store.attach(None)
        self.store.attach(json.loads(self.ledger()))
        self.store.bind_directory((info.st_dev, info.st_ino))

    def test_a_replacement_without_the_store_lock_gets_none_made_in_it(self):
        before = self.committed()
        swap(self.directory, self.moved)
        (self.directory / "store.lock").unlink()
        calls = (("commit", self.store.commit), ("check_current", self.store.check_current),
                 ("attach", lambda: self.store.attach(json.loads(before))))
        for name, call in calls:
            with self.subTest(call=name):
                self.assertReplaced(call)
                self.assertEqual(os.listdir(self.directory), ["state.json"])  # the identity is told before any lock file
        self.assertEqual((self.ledger(self.moved), self.ledger()), (before, before))

    def test_only_a_run_never_committed_nor_bound_may_be_missing_from_disk(self):
        before = self.committed()
        os.rename(self.directory, self.moved)
        # A ledger read from disk whose directory has gone, in this process or another: stopped, never made again.
        for name, store in (("this process", self.store), ("another", RunStore(self.root))):
            with self.subTest(store=name):
                self.assertReplaced(lambda: store.attach(json.loads(before)))
                self.assertEqual(os.listdir(self.root / "runs"), [])
        # A run never committed is selected without its directory; once a run.lock holder bound one, it must be there.
        fresh = RunStore(self.root)
        fresh.attach({"id": "run_two"})
        made = self.root / "runs" / "run_two"
        made.mkdir()
        info = os.stat(made)
        fresh.bind_directory((info.st_dev, info.st_ino))
        made.rmdir()
        self.assertReplaced(lambda: fresh.attach(fresh.run))
        self.assertEqual(os.listdir(self.root / "runs"), [])
        self.assertEqual(self.ledger(self.moved), before)

    def test_a_store_lock_or_ledger_that_is_not_a_regular_file_is_damaged_evidence_at_once(self):
        before = self.committed()
        outside = Path(self.temp.name).resolve() / "outside"
        outside.write_bytes(b"keep")
        lock, ledger = self.directory / "store.lock", self.directory / "state.json"
        cases = (("store.lock", "fifo", os.mkfifo), ("store.lock", "link", lambda path: path.symlink_to(outside)),
                 ("store.lock", "dangling", lambda path: path.symlink_to(outside.with_name("missing"))),
                 ("store.lock", "directory", os.mkdir), ("state.json", "fifo", os.mkfifo),
                 ("state.json", "link", lambda path: path.symlink_to(outside)))
        for name, layout, make in cases:
            path = lock if name == "store.lock" else ledger
            for call in (self.store.commit, self.store.check_current):
                with self.subTest(name=name, layout=layout, call=call.__name__):
                    with laid_out(path, layout, make) as watchdog, self.assertRaises(EvidenceError) as caught:
                        call()
                    self.assertFalse(watchdog and watchdog.hung)
                    self.assertEqual(kind(caught.exception), ("evidence", "runtime_integrity"))
                    self.assertEqual(stops.of(caught.exception)["source"], {"artifact": str(path)})
                    self.assertEqual((outside.read_bytes(), outside.with_name("missing").exists()), (b"keep", False))
                    self.assertEqual(self.ledger(), before)
        self.assertEqual(self.store.commit(), 2)


class RunLockTests(RepoCase):
    """run.lock for start, resume and abandon: taken through the primitive, its directory bound to the store."""

    def paused(self):
        engine = self.engine()
        run = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"), pause_after_plan=True)
        return engine.directory, run["id"]

    def test_resume_stops_when_the_run_directory_is_replaced_after_its_lock(self):
        directory, ident = self.paused()
        before = (directory / "state.json").read_bytes()
        moved = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, moved)
        moved = moved / "moved"
        verify = RunLifecycle.verify_assets_and_plan

        def replacing(lifecycle):  # under run.lock, after the reload there, before any commit
            swap(directory, moved)
            return verify(lifecycle)
        with patch.object(RunLifecycle, "verify_assets_and_plan", replacing), \
                self.assertRaises(HarnessError) as caught:
            self.engine().resume(ident, approve_plan=True)
        self.assertEqual(kind(caught.exception), ("runtime_failure", "invariant"))
        self.assertEqual(((moved / "state.json").read_bytes(), (directory / "state.json").read_bytes()), (before, before))

    def moved(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        return folder / "moved"

    def test_a_run_directory_replaced_before_its_run_lock_gets_none_made_in_it(self):
        load = RunLifecycle.load_run
        for entry in ("resume", "abandon"):
            with self.subTest(entry=entry):
                directory, ident = self.paused()
                before, moved, listing = (directory / "state.json").read_bytes(), self.moved(), []

                def replacing(lifecycle, run_id):  # the ledger loaded and its directory known, before run.lock
                    load(lifecycle, run_id)
                    swap(directory, moved)
                    (directory / "run.lock").unlink()
                    listing.append(sorted(os.listdir(directory)))
                with patch.object(RunLifecycle, "load_run", replacing), self.assertRaises(HarnessError) as caught:
                    getattr(self.engine(), entry)(ident)
                self.assertEqual(kind(caught.exception), ("runtime_failure", "invariant"))
                self.assertEqual(len(listing), 1)
                self.assertNotIn("run.lock", listing[0])
                self.assertEqual(sorted(os.listdir(directory)), listing[0])  # no run.lock, nothing else either
                self.assertEqual(((moved / "state.json").read_bytes(), (directory / "state.json").read_bytes()),
                                 (before, before))

    def test_an_existing_ledger_whose_directory_goes_after_it_is_read_is_an_invariant_stop(self):
        parse = run_lifecycle.parse_current_ledger
        for entry in ("resume", "abandon"):
            with self.subTest(entry=entry):
                directory, ident = self.paused()
                before, moved = (directory / "state.json").read_bytes(), self.moved()
                listing = sorted(os.listdir(directory))

                def moving(raw):  # the ledger read and parsed, its directory gone before the store binds it
                    parsed = parse(raw)
                    os.rename(directory, moved)
                    return parsed
                with patch.object(run_lifecycle, "parse_current_ledger", moving), \
                        self.assertRaises(HarnessError) as caught:
                    getattr(self.engine(), entry)(ident)
                self.assertEqual(kind(caught.exception), ("runtime_failure", "invariant"))
                self.assertFalse(os.path.lexists(directory))  # never made again
                self.assertEqual((sorted(os.listdir(moved)), (moved / "state.json").read_bytes()), (listing, before))

    def test_a_run_lock_that_is_not_a_regular_file_or_is_held_stops_resume_and_abandon_at_once(self):
        directory, ident = self.paused()
        lock = directory / "run.lock"
        info = os.lstat(lock)  # as the run's own lock made it
        self.assertEqual((stat.S_ISREG(info.st_mode), stat.S_IMODE(info.st_mode)), (True, 0o600 & ~self.umask()))
        before = [(directory / name).read_bytes() for name in ("state.json", "events.jsonl")]
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        (outside / "target").write_bytes(b"keep")
        cases = (("fifo", os.mkfifo), ("link", lambda path: path.symlink_to(outside / "target")),
                 ("dangling", lambda path: path.symlink_to(outside / "missing")), ("directory", os.mkdir))
        for layout, make in cases:
            for entry in ("resume", "abandon"):
                with self.subTest(layout=layout, entry=entry):
                    engine = self.engine()
                    with laid_out(lock, layout, make) as watchdog, self.assertRaises(EvidenceError) as caught:
                        getattr(engine, entry)(ident)
                    self.assertFalse(watchdog and watchdog.hung)
                    self.assertEqual(kind(caught.exception), ("evidence", "runtime_integrity"))
                    self.assertEqual(stops.of(caught.exception)["source"], {"artifact": str(lock)})
                    self.assertEqual(sorted(os.listdir(outside)), ["target"])
                    self.assertEqual((outside / "target").read_bytes(), b"keep")
                    self.assertEqual([(directory / name).read_bytes() for name in ("state.json", "events.jsonl")], before)
        for entry in ("resume", "abandon"):
            with self.subTest(layout="held", entry=entry), held_lock(lock), self.assertRaises(HarnessError) as caught:
                getattr(self.engine(), entry)(ident)
            self.assertEqual((kind(caught.exception), stops.exit_code(caught.exception)), (("busy", "run_lock"), 3))
        self.assertEqual(self.engine().resume(ident, approve_plan=True)["status"], "completed")

    @staticmethod
    def umask():
        mask = os.umask(0)
        os.umask(mask)
        return mask

    def test_a_ledger_that_is_not_a_regular_file_or_cannot_be_read_stops_the_load(self):
        directory, ident = self.paused()
        ledger = directory / "state.json"
        kept = ledger.read_bytes()
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        (outside / "copy.json").write_bytes(kept)
        cases = (("fifo", os.mkfifo), ("link", lambda path: path.symlink_to(outside / "copy.json")),
                 ("eio", lambda path: path.write_bytes(kept)))
        for layout, make in cases:
            for entry in ("resume", "abandon"):
                with self.subTest(layout=layout, entry=entry):
                    engine = self.engine()
                    injected = (patch.object(state_files, "os", FakeOs(open=failing(errno.EIO, "state.json")))
                                if layout == "eio" else contextlib.nullcontext())
                    with laid_out(ledger, layout, make) as watchdog, injected, self.assertRaises(HarnessError) as caught:
                        getattr(engine, entry)(ident)
                    self.assertFalse(watchdog and watchdog.hung)
                    if layout == "eio":
                        self.assertEqual(stops.of(caught.exception)["data"]["error_type"], "OSError")
                        self.assertEqual(kind(caught.exception), ("runtime_failure", "io"))
                    else:
                        self.assertEqual(kind(caught.exception), ("evidence", "load_integrity"))
                    self.assertEqual((outside / "copy.json").read_bytes(), kept)
        self.assertEqual(self.engine().resume(ident, approve_plan=True)["status"], "completed")
