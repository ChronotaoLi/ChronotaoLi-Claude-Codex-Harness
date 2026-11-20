"""A dispatch's command journals read once each, through harness.state_files, and the gates over them (design 4, 3.3.2-3.3.3).

A record's facts and digest come from one read. A journal that is missing, a link, a FIFO or a directory,
or that is not one whole valid journal (over 64 KiB, not strict JSON, a field outside its domain), leaves
the call's exit unconfirmed: never a pass and never an untyped error. An invalid journal is confirmed bound
to the whole file's digest; under ledger 19 a call without a readable journal cannot be confirmed, so a
batch that would need such a confirmation writes nothing. A read that fails is local I/O; an entry of
commands/ that is not a call folder is damaged evidence, and so is a link at any level down to it from the state
directory the caller gives, or a dispatch path not written below it as a ledger records one; nothing above that
directory is opened. A group ID
past pid_t is an invalid journal, and a group that cannot be looked at is not one seen gone. FIFO cases end through
FifoWatchdog, read failures are injected by replacing ``state_files.os`` (or ``os`` itself, for the lookups made by
path). No model CLI is invoked.
"""
import contextlib
import copy
import errno
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from harness import command_server, state_files, stops
from harness.cleanup import cleanup_run
from harness.command_server import Broker, command_id, command_processes, create_manifest, handle
from harness.state_files import StateReadError
from harness.util import (EvidenceError, HarnessError, confirm_dispatches_exited, ensure_dispatches_exited,
                          read_json, validate_command_directories, write_json)
from test_harness import ROOT, RepoCase, refused_by_cli, run_cli
from test_provider_failures import FifoWatchdog
from test_runtime_changes import ScriptedBackend
from test_state_files import FakeOs, failing, passing


JOURNAL_MAX = 64 * 1024  # design 4, 3.3.2: a longer process.json is never read as process facts


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def journal(call, **fields):
    return {"id": call, "command_id": "fixture", "pgid": None, "process_state": "starting", "exit_confirmed": False,
            **fields}


def exited(call, **fields):
    return journal(call, **{"process_state": "exited", "exit_confirmed": True, **fields})


def encoded(value):
    return (json.dumps(value) + "\n").encode()


# process.json laid out in each way, with the journal_state it reads as. Each invalid one would pass the
# exit gate as an exited, confirmed call if it were read as facts; the link points at such a journal.
UNREADABLE = {"absent": "absent", "fifo": "refused", "link": "refused", "directory": "refused"}
INVALID = {
    "not_json": lambda call: b'{"id": "' + call.encode() + b'", "exit_confirmed": true',
    "shape": lambda call: encoded({**exited(call), "extra": None}),
    "oversized": lambda call: (json.dumps(exited(call)) + " " * JOURNAL_MAX).encode(),
    "duplicate_key": lambda call: ('{"id": "x", ' + json.dumps(exited(call))[1:]).encode(),
    "other_call": lambda call: encoded(exited(call[:-1] + ("0" if call[-1] != "0" else "1"))),
    "command_id": lambda call: encoded(exited(call, command_id=7)),
    "process_state": lambda call: encoded(exited(call, process_state="done")),
    "exit_confirmed": lambda call: encoded(exited(call, exit_confirmed=1)),
    "pgid_zero": lambda call: encoded(exited(call, pgid=0)),
    "pgid_negative": lambda call: encoded(exited(call, pgid=-3)),
    "pgid_bool": lambda call: encoded(exited(call, pgid=True)),
    "pgid_float": lambda call: encoded(exited(call, pgid=1.5)),
    # No process group can have it: pid_t is a 32-bit signed int, and os.killpg raises OverflowError for it.
    "pgid_past_pid_t": lambda call: encoded(exited(call, pgid=2**31)),
}
# What design 4 lists for the operations: missing, FIFO, link, not JSON, wrong shape, over 64 KiB.
LIFECYCLE_LAYOUTS = ("absent", "fifo", "link", "directory", "not_json", "shape", "oversized")


def clear(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def lay_out(folder, call, layout):
    """Put the call's process.json in ``layout``; (journal_state, the bytes a read gets or None)."""
    path = folder / "process.json"
    clear(path)
    if layout in INVALID:
        raw = INVALID[layout](call)
        path.write_bytes(raw)
        return "invalid", raw
    if layout == "fifo":
        os.mkfifo(path)
    elif layout == "link":
        target = folder.parent.parent / f"elsewhere-{folder.name}.json"
        target.write_bytes(encoded(exited(call)))
        path.symlink_to(target)
    elif layout == "directory":
        path.mkdir()
    return UNREADABLE[layout], None


@contextlib.contextmanager
def unblocked(path, layout):
    """A FifoWatchdog on a FIFO layout; the test fails if any read waited on it."""
    watchdog = FifoWatchdog(path, bound=5) if layout == "fifo" else None
    try:
        yield
    finally:
        if watchdog is not None and watchdog.stop():
            raise AssertionError(f"a read waited on the FIFO {path}")


@contextlib.contextmanager
def failing_lookups(directory, code, *, opens):
    """Each lookup of ``directory`` that does not follow it fails with ``code``.

    Always the ones by its path, ``os.lstat`` and ``os.stat(follow_symlinks=False)``: what ``Path.is_symlink()`` makes
    (3.11 raises that failure, 3.15 reads it as "not a link"). With ``opens``, also every ``os.open`` of it and every
    no-follow stat of it by name from a descriptor of its parent: what a walk from an ancestor makes.
    """
    real_open, real_stat, real_lstat = os.open, os.stat, os.lstat
    parent = real_stat(directory.parent)

    def target(path, dir_fd):
        if not isinstance(path, (str, os.PathLike)):
            return False
        if dir_fd is None:
            return os.path.abspath(path) == str(directory)
        held = os.fstat(dir_fd)
        return opens and os.fspath(path) == directory.name and (held.st_dev, held.st_ino) == (parent.st_dev, parent.st_ino)

    def fail():
        raise OSError(code, os.strerror(code))

    def lstat(path, *, dir_fd=None):
        if target(path, dir_fd):
            fail()
        return real_lstat(path, dir_fd=dir_fd)

    def stat(path, *, dir_fd=None, follow_symlinks=True):
        if not follow_symlinks and target(path, dir_fd):
            fail()
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def open_(path, flags, mode=0o777, *, dir_fd=None):
        if opens and target(path, dir_fd):
            fail()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    with patch.object(os, "lstat", lstat), patch.object(os, "stat", stat), patch.object(os, "open", open_):
        yield


def identity(path_or_fd):
    info = os.fstat(path_or_fd) if isinstance(path_or_fd, int) else os.stat(path_or_fd, follow_symlinks=False)
    return info.st_dev, info.st_ino


@contextlib.contextmanager
def journal_opens(fail=None):
    """The opens state_files makes while command journals are enumerated: (the path of an open by path, else None; identity).

    An open there for which ``fail(name, dir_fd)`` is true raises EIO instead: the opens the enumeration really makes.
    """
    opened, reading, enumerate_journals = [], [], command_server.command_processes

    def records(*args, **kwargs):
        reading.append(True)
        try:
            return enumerate_journals(*args, **kwargs)
        finally:
            reading.pop()

    def open_(real, name, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        if reading and fail is not None and fail(name, dir_fd):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        fd = real(name, *args, **kwargs)
        if reading:
            opened.append((os.fspath(name) if dir_fd is None else None, identity(fd)))
        return fd
    with patch.object(command_server, "command_processes", records), patch.object(state_files, "os", FakeOs(open=open_)):
        yield opened


def stop_of(error):
    value = stops.of(error)
    value["data"].pop("detail", None)
    return value


def unconfirmed(call, dispatch_id):
    return stops.make("process_unconfirmed", "command_exit", data={"command_id": call, "returned_unsealed": False},
                      source={"dispatch_id": dispatch_id})


def local_io(error_type):
    return stops.make("runtime_failure", "io", data={"error_type": error_type})


class JournalFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def dispatch(self, ident=None, *, exit_confirmed=True):
        """A ledger entry of an exited dispatch with an empty commands/ of its own."""
        ident = ident or "d" + uuid.uuid4().hex[:12]
        commands = self.root / "dispatches" / ident / "commands"
        commands.mkdir(parents=True)
        return {"id": ident, "pgid": None, "process_state": "exited", "exit_confirmed": exit_confirmed,
                "command_directory": str(commands)}

    def call(self, entry, name=None):
        folder = Path(entry["command_directory"]) / (name or uuid.uuid4().hex)
        folder.mkdir()
        return folder, f"{entry['id']}:command:{folder.name}"

    def confirm(self, dispatches, identifiers, audits=None, saves=None, **options):
        """The audit and save callbacks' calls, kept in the lists given: a confirmation that raises leaves them there."""
        audits, saves = [] if audits is None else audits, [] if saves is None else saves
        confirm_dispatches_exited(dispatches, identifiers, state=self.root, audit=lambda event, **fields: audits.append((event, fields)),
                                  save=lambda: saves.append(copy.deepcopy(dispatches)), **options)
        return audits, saves


class JournalRecordTests(JournalFixture):
    def test_facts_and_digest_come_from_one_read(self):
        entry = self.dispatch()
        folder, call = self.call(entry)
        first = encoded(journal(call, pgid=4242, process_state="running"))
        (folder / "process.json").write_bytes(first)
        parse = json.loads

        def parse_then_replace(*args, **kwargs):
            value = parse(*args, **kwargs)
            if isinstance(value, dict) and value.get("pgid") == 4242:
                # Replaced as the broker writes a journal: a whole new file renamed over the one just read.
                write_json(folder / "process.json", exited(call))
            return value
        with patch("json.loads", parse_then_replace):
            [record] = command_processes(entry["command_directory"], state=self.root)
        self.assertEqual((record["journal_digest"], record["pgid"], record["process_state"]), (sha(first), 4242, "running"))
        self.assertEqual(record["journal_state"], "valid")

    def test_each_journal_layout_has_its_record_and_only_a_valid_exit_passes_the_gate(self):
        entry = self.dispatch()
        folder, call = self.call(entry)
        (folder / "process.json").write_bytes(encoded(exited(call, pgid=77)))
        ensure_dispatches_exited([entry], state=self.root)
        [record] = command_processes(entry["command_directory"], state=self.root)
        self.assertEqual(record, {**exited(call, pgid=77), "journal": str(folder / "process.json"),
                                  "journal_digest": sha((folder / "process.json").read_bytes()), "journal_state": "valid"})
        for layout in (*UNREADABLE, *INVALID):
            with self.subTest(layout=layout):
                entry = self.dispatch()
                folder, call = self.call(entry)
                state, raw = lay_out(folder, call, layout)
                with unblocked(folder / "process.json", layout):
                    [record] = command_processes(entry["command_directory"], state=self.root)
                    with self.assertRaises(HarnessError) as raised:
                        ensure_dispatches_exited([entry], state=self.root)
                self.assertEqual(record, {"id": call, "command_id": None, "pgid": None, "process_state": None,
                                          "exit_confirmed": False, "journal": str(folder / "process.json"),
                                          "journal_digest": None if raw is None else sha(raw), "journal_state": state})
                self.assertEqual(stop_of(raised.exception), unconfirmed(call, entry["id"]))

    def test_a_read_that_fails_is_local_io_and_never_a_record(self):
        entry = self.dispatch()
        folder, call = self.call(entry)
        (folder / "process.json").write_bytes(encoded(exited(call)))
        cases = (("open EIO", FakeOs(open=failing(errno.EIO, "process.json")), "OSError"),
                 ("open EACCES", FakeOs(open=failing(errno.EACCES, "process.json")), "PermissionError"),
                 ("read EIO", FakeOs(pread=failing(errno.EIO)), "OSError"),
                 ("listing EIO", FakeOs(scandir=failing(errno.EIO)), "OSError"))
        for name, fake, error_type in cases:
            with self.subTest(case=name), patch.object(state_files, "os", fake):
                self.assert_local_io(entry, error_type)
        # The levels above the journal are read as well: the dispatch directory and every directory above it.
        dispatch = Path(entry["command_directory"]).parent
        for level, code, error_type in ((dispatch, errno.EIO, "OSError"), (dispatch.parent, errno.EACCES, "PermissionError")):
            with self.subTest(case=f"{level.name} lookup {errno.errorcode[code]}"), failing_lookups(level, code, opens=True):
                self.assert_local_io(entry, error_type)

    def assert_local_io(self, entry, error_type):
        before = copy.deepcopy(entry)
        audits, saves = [], []
        for name, gate in (("records", lambda: command_processes(entry["command_directory"], state=self.root)),
                           ("exit gate", lambda: ensure_dispatches_exited([entry], state=self.root)),
                           ("confirmation", lambda: self.confirm([entry], [entry["id"]], audits, saves))):
            with self.subTest(gate=name):
                with self.assertRaises(StateReadError) as raised:
                    gate()
                self.assertEqual(stop_of(raised.exception), local_io(error_type))
        self.assertEqual((audits, saves, entry), ([], [], before))

    def test_a_link_on_the_dispatch_path_is_refused_whatever_a_lookup_by_path_says(self):
        # Code review D4-T3, blocker 1: Path.is_symlink() on Python 3.15 reads a failing no-follow lookup by path as
        # "not a link", so a check made of such lookups let the gate read through a link. Here each one of the linked
        # level fails with EIO, as a failing disk makes it fail; the gate never asks one, it opens every level from its
        # parent without following it.
        for level in ("dispatch", "dispatches"):
            for gate in ("records", "exit gate", "confirmation"):
                with self.subTest(level=level, gate=gate):
                    shutil.rmtree(self.root, ignore_errors=True)
                    self.root.mkdir()
                    # A confirmation would have its dispatch's exit to write; the exit gate nothing left to stop at.
                    entry = self.dispatch(exit_confirmed=gate != "confirmation")
                    folder, call = self.call(entry)
                    (folder / "process.json").write_bytes(encoded(exited(call)))
                    linked = folder.parent.parent if level == "dispatch" else self.root / "dispatches"
                    # The link leads to the very same journal: only the link itself may refuse it.
                    moved = linked.with_name("moved-" + linked.name)
                    linked.rename(moved)
                    linked.symlink_to(moved, target_is_directory=True)
                    before, audits, saves = copy.deepcopy(entry), [], []
                    with failing_lookups(linked, errno.EIO, opens=False), self.assertRaises(EvidenceError) as raised:
                        if gate == "records":
                            command_processes(entry["command_directory"], state=self.root)
                        elif gate == "exit gate":
                            ensure_dispatches_exited([entry], state=self.root)
                        else:
                            self.confirm([entry], [entry["id"]], audits, saves)
                    self.assertEqual((stop_of(raised.exception)["kind"], stop_of(raised.exception)["reason"]),
                                     ("evidence", "runtime_integrity"))
                    self.assertEqual((audits, saves, entry), ([], [], before))
        # The same lookups failing on a real directory decide nothing either: the walk's own opens found no link.
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir()
        entry = self.dispatch()
        folder, call = self.call(entry)
        (folder / "process.json").write_bytes(encoded(exited(call)))
        with failing_lookups(folder.parent.parent, errno.EIO, opens=False):
            [record] = command_processes(entry["command_directory"], state=self.root)
            ensure_dispatches_exited([entry], state=self.root)
        self.assertEqual(record["journal_state"], "valid")

    def test_a_dispatch_path_not_recorded_below_the_state_directory_is_damaged_evidence(self):
        # Confirm review D4-T3b, blocker 2: a ".." reached the primitive's component check and left as a bare ValueError.
        # Each spelling below leads to a valid exited journal; only how the path is written may refuse it.
        entry = self.dispatch()
        folder, call = self.call(entry)
        (folder / "process.json").write_bytes(encoded(exited(call)))
        job = folder.parent.parent
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        elsewhere = Path(outside.name).resolve()
        alias = elsewhere / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        (elsewhere / "dispatches" / job.name / "commands" / folder.name).mkdir(parents=True)
        (elsewhere / "dispatches" / job.name / "commands" / folder.name / "process.json").write_bytes(encoded(exited(call)))
        spellings = {
            "a .. segment": str(self.root / "dispatches" / ".." / "dispatches" / job.name),
            "a .. below commands/": str(job / "commands" / ".." / "commands"),
            "an empty segment": str(job).replace("/dispatches/", "/dispatches//"),
            "a . segment": str(job).replace("/dispatches/", "/dispatches/./"),
            "through a link to the state directory": str(alias / "dispatches" / job.name / "commands"),
            "another directory": str(elsewhere / "dispatches" / job.name),
            "relative": os.path.relpath(job),
            "the state directory": str(self.root),
            "commands/ of the state directory": str(self.root / "commands"),
        }
        for name, path in spellings.items():
            for gate in ("records", "exit gate", "confirmation"):
                with self.subTest(path=name, gate=gate):
                    spelled = dict(entry, command_directory=path, exit_confirmed=gate != "confirmation")
                    before, audits, saves = copy.deepcopy(spelled), [], []
                    with self.assertRaises(EvidenceError) as raised:
                        if gate == "records":
                            command_processes(path, state=self.root)
                        elif gate == "exit gate":
                            ensure_dispatches_exited([spelled], state=self.root)
                        else:
                            self.confirm([spelled], [spelled["id"]], audits, saves)
                    # The stop names the path as given; make() drops a relative one, as any unfit source.
                    self.assertEqual(stop_of(raised.exception),
                                     stops.make("evidence", "runtime_integrity", source={"artifact": path}))
                    self.assertEqual((audits, saves, spelled), ([], [], before))
        # The ledger's spelling, of the job or of its commands/, reads the journal; so does a state directory given
        # through a link, resolved once as every state root is.
        for path, state in ((str(job), self.root), (entry["command_directory"], self.root), (job, alias)):
            with self.subTest(path=str(path), state=str(state)):
                [record] = command_processes(path, state=state)
                self.assertEqual(record["journal_state"], "valid")
        ensure_dispatches_exited([entry], state=alias)

    def test_journal_reads_open_nothing_above_the_state_directory(self):
        # Confirm review D4-T3b, suggestion: each read walked from / and opened every directory above the state tree.
        entry = self.dispatch(exit_confirmed=False)
        folder, call = self.call(entry)
        (folder / "process.json").write_bytes(encoded(exited(call)))
        job = folder.parent.parent
        walked = {identity(path) for path in (self.root, job.parent, job, folder.parent, folder, folder / "process.json")}
        above = {identity(path) for path in self.root.parents}
        for name, gate in (("records", lambda: command_server.command_processes(entry["command_directory"], state=self.root)),
                           ("exit gate", lambda: ensure_dispatches_exited([dict(entry, exit_confirmed=True)], state=self.root)),
                           ("confirmation", lambda: self.confirm([copy.deepcopy(entry)], [entry["id"]]))):
            with self.subTest(gate=name):
                with journal_opens() as opened:
                    gate()
                # Opened by path: the state directory alone; from there down, the journal's own levels and nothing else.
                self.assertEqual({path for path, _ in opened if path is not None}, {str(self.root)})
                self.assertEqual({ident for _, ident in opened}, walked)
                self.assertFalse({ident for _, ident in opened} & above)

    def test_commands_holds_only_call_folders(self):
        def file(entry, name="notes.txt"):
            (Path(entry["command_directory"]) / name).write_text("{}")

        def bad_name(entry):
            (Path(entry["command_directory"]) / ("A" * 31 + "x")).mkdir()

        def linked_call(entry):
            real = self.root / ("real-" + uuid.uuid4().hex)
            real.mkdir()
            (real / "process.json").write_bytes(encoded(exited(entry["id"] + ":command:" + "c" * 32)))
            (Path(entry["command_directory"]) / ("c" * 32)).symlink_to(real, target_is_directory=True)

        def replaced(entry, by):
            commands = Path(entry["command_directory"])
            elsewhere = self.dispatch()
            folder, call = self.call(elsewhere)
            (folder / "process.json").write_bytes(encoded(exited(call)))
            shutil.rmtree(commands)
            by(commands, Path(elsewhere["command_directory"]))

        def raced(entry, hooks):
            folder, call = self.call(entry)
            (folder / "process.json").write_bytes(encoded(exited(call)))
            return patch.object(state_files, "os", FakeOs(**hooks(folder)))

        def listed_then_removed(folder):
            # Gone between the listing and the type lookup of its name: the primitive lists it as absent.
            def scandir(real, fd):
                with real(fd) as iterator:
                    listed = list(iterator)
                shutil.rmtree(folder)
                return contextlib.nullcontext(iter(listed))
            return {"scandir": scandir}

        def before_open(folder, change):
            def hook(real, name, *args, **kwargs):
                if name == folder.name and folder.is_dir() and not folder.is_symlink():
                    change(folder)
                return real(name, *args, **kwargs)
            return {"open": hook}

        def to_link(folder):
            target = folder.parent.parent / ("moved-" + folder.name)
            folder.rename(target)
            folder.symlink_to(target, target_is_directory=True)

        cases = {
            "a regular file": file,
            "a file with a call folder's name": lambda entry: file(entry, "b" * 32),
            "a directory not named as a call folder": bad_name,
            "a link to a call folder": linked_call,
            "commands/ a link": lambda entry: replaced(entry, lambda path, target: path.symlink_to(target)),
            "commands/ a regular file": lambda entry: replaced(entry, lambda path, target: path.write_text("")),
            "the dispatch directory a link": lambda entry: replaced(
                entry, lambda path, target: (path.parent.rmdir(), path.parent.symlink_to(target.parent))),
            "a call folder gone after it was listed": lambda entry: raced(entry, listed_then_removed),
            "a call folder gone before its journal is read": lambda entry: raced(
                entry, lambda folder: before_open(folder, shutil.rmtree)),
            "a call folder made a link before its journal is read": lambda entry: raced(
                entry, lambda folder: before_open(folder, to_link)),
        }
        for name, arrange in cases.items():
            for gate in ("records", "exit gate", "confirmation"):
                with self.subTest(case=name, gate=gate):
                    entry = self.dispatch()
                    before, audits, saves = copy.deepcopy(entry), [], []
                    with arrange(entry) or contextlib.nullcontext(), self.assertRaises(EvidenceError) as raised:
                        if gate == "records":
                            command_processes(entry["command_directory"], state=self.root)
                        elif gate == "exit gate":
                            ensure_dispatches_exited([entry], state=self.root)
                        else:
                            self.confirm([entry], [entry["id"]], audits, saves)
                    self.assertEqual((stop_of(raised.exception)["kind"], stop_of(raised.exception)["reason"]),
                                     ("evidence", "runtime_integrity"))
                    self.assertEqual((audits, saves, entry), ([], [], before))

    def test_no_commands_directory_has_no_records(self):
        entry = self.dispatch()
        shutil.rmtree(entry["command_directory"])
        self.assertEqual(command_processes(entry["command_directory"], state=self.root), [])
        shutil.rmtree(Path(entry["command_directory"]).parent)
        self.assertEqual(command_processes(entry["command_directory"], state=self.root), [])

    def test_a_record_without_a_digest_passes_only_a_confirmation_stored_for_it(self):
        # Design 4, probe R6: a digest of None compared with .get() matched a missing confirmation.
        entry = self.dispatch()
        call = f"{entry['id']}:command:{'a' * 32}"
        record = {"id": call, "command_id": None, "pgid": None, "process_state": None, "exit_confirmed": False,
                  "journal": entry["command_directory"] + "/" + "a" * 32 + "/process.json", "journal_digest": None,
                  "journal_state": "absent"}
        with patch.object(command_server, "command_processes", return_value=[record]):
            for confirmations in (None, {}, {call[:-1] + "b": {"journal_digest": "b" * 64}},
                                  {call: {"time": 1.0, "observed": {}}}):
                parent = dict(entry) if confirmations is None else dict(entry, command_exit_confirmations=confirmations)
                with self.subTest(confirmations=confirmations):
                    with self.assertRaises(HarnessError) as raised:
                        ensure_dispatches_exited([parent], state=self.root)
                    self.assertEqual(stop_of(raised.exception), unconfirmed(call, entry["id"]))
            # Only a stored None, the shape ledger 20 adds, matches a call that has no readable journal.
            ensure_dispatches_exited([dict(entry, command_exit_confirmations={call: {"journal_digest": None}})], state=self.root)

    def test_command_directory_checks_go_through_the_primitive(self):
        entry = self.dispatch("d")
        validate_command_directories([entry], self.root)
        for name, fake, error_type in (("EIO", FakeOs(stat=failing(errno.EIO, "commands")), "OSError"),
                                       ("EACCES", FakeOs(stat=failing(errno.EACCES, "commands")), "PermissionError")):
            with self.subTest(lookup=name):
                with patch.object(state_files, "os", fake), self.assertRaises(StateReadError) as raised:
                    validate_command_directories([entry], self.root)
                self.assertEqual(stop_of(raised.exception), local_io(error_type))
        commands = Path(entry["command_directory"])
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        for name, damage in (("missing", lambda: commands.rmdir()),
                             ("a link", lambda: commands.symlink_to(elsewhere, target_is_directory=True)),
                             ("a regular file", lambda: (commands.unlink(), commands.write_text(""))),
                             ("the dispatch a link", lambda: (commands.unlink(), commands.parent.rmdir(),
                                                              commands.parent.symlink_to(elsewhere)))):
            with self.subTest(commands=name):
                damage()
                with self.assertRaises(EvidenceError):
                    validate_command_directories([entry], self.root)


class JournalConfirmationTests(JournalFixture):
    def test_an_invalid_journal_is_confirmed_bound_to_the_whole_file_until_it_changes(self):
        for layout in ("shape", "oversized", "not_json"):
            with self.subTest(layout=layout):
                entry = self.dispatch()
                folder, call = self.call(entry)
                _, raw = lay_out(folder, call, layout)
                with self.assertRaises(HarnessError):
                    ensure_dispatches_exited([entry], state=self.root)
                audits, saves = self.confirm([entry], [entry["id"]])
                self.assertEqual([(event, fields["command_id"], fields["journal_digest"], fields["observed"])
                                  for event, fields in audits],
                                 [("command_exit_confirmed_by_operator", call, sha(raw), {"pgid": None, "group_alive": None})])
                self.assertEqual(len(saves), 1)
                self.assertEqual(entry["command_exit_confirmations"][call]["journal_digest"], sha(raw))
                ensure_dispatches_exited([entry], state=self.root)
                # Already bound to these bytes: nothing to confirm again.
                self.assertEqual(self.confirm([entry], [entry["id"]]), ([], []))
                # Other invalid bytes are another journal: the confirmation no longer covers it.
                (folder / "process.json").write_bytes(raw + b" ")
                with self.assertRaises(HarnessError) as raised:
                    ensure_dispatches_exited([entry], state=self.root)
                self.assertEqual(stop_of(raised.exception), unconfirmed(call, entry["id"]))
                audits, _ = self.confirm([entry], [entry["id"]])
                self.assertEqual([fields["journal_digest"] for _, fields in audits], [sha(raw + b" ")])
                ensure_dispatches_exited([entry], state=self.root)

    def test_a_batch_that_needs_a_confirmation_without_a_digest_writes_nothing(self):
        for order, layout in ((("d1", "d2"), "absent"), (("d2", "d1"), "fifo"), (("d1", "d2"), "link")):
            with self.subTest(order=order, layout=layout):
                shutil.rmtree(self.root / "dispatches", ignore_errors=True)
                first, second = self.dispatch("d1", exit_confirmed=False), self.dispatch("d2", exit_confirmed=False)
                folder, call = self.call(first)
                _, raw = lay_out(folder, call, "shape")
                missing, unbound = self.call(second)
                lay_out(missing, unbound, layout)
                before, audits, saves = copy.deepcopy([first, second]), [], []
                with unblocked(missing / "process.json", layout), self.assertRaises(HarnessError) as raised:
                    self.confirm([first, second], list(order), audits, saves)
                self.assertEqual(stop_of(raised.exception), unconfirmed(unbound, "d2"))
                self.assertEqual((audits, saves, [first, second]), ([], [], before))
        # d1 alone is confirmed as before: its call bound to the whole invalid file, then the dispatch.
        audits, saves = self.confirm([first, second], ["d1"])
        self.assertEqual([(event, fields.get("command_id"), fields.get("journal_digest")) for event, fields in audits],
                         [("command_exit_confirmed_by_operator", call, sha(raw)),
                          ("dispatch_exit_confirmed_by_operator", None, None)])
        self.assertEqual(len(saves), 2)
        self.assertNotIn("command_exit_confirmations", second)

    def test_a_journal_read_failing_for_the_second_selected_dispatch_writes_nothing(self):
        for order in (("d1", "d2"), ("d2", "d1")):
            with self.subTest(order=order):
                shutil.rmtree(self.root / "dispatches", ignore_errors=True)
                dispatches = {ident: self.dispatch(ident, exit_confirmed=False) for ident in ("d1", "d2")}
                confirmable, call = self.call(dispatches[order[0]])
                lay_out(confirmable, call, "shape")
                failing, call = self.call(dispatches[order[1]])
                (failing / "process.json").write_bytes(encoded(exited(call)))
                inode = failing.stat().st_ino

                def open_(real, name, *args, **kwargs):
                    fd = kwargs.get("dir_fd")
                    if name == "process.json" and fd is not None and os.fstat(fd).st_ino == inode:
                        raise OSError(errno.EIO, os.strerror(errno.EIO))
                    return real(name, *args, **kwargs)
                before, audits, saves = copy.deepcopy(list(dispatches.values())), [], []
                with patch.object(state_files, "os", FakeOs(open=open_)), self.assertRaises(StateReadError) as raised:
                    self.confirm(list(dispatches.values()), list(order), audits, saves)
                self.assertEqual(stop_of(raised.exception), local_io("OSError"))
                self.assertEqual((audits, saves, list(dispatches.values())), ([], [], before))

    def test_a_group_id_past_pid_t_is_an_invalid_journal_confirmed_with_its_whole_batch(self):
        # Code review D4-T3, blocker 2: such a journal passed the field checks, and os.killpg raised OverflowError
        # for it from the gate, and from the confirmation after the batch's first confirmations were saved.
        first, second = self.dispatch("d1", exit_confirmed=False), self.dispatch("d2", exit_confirmed=False)
        folder, first_call = self.call(first)
        _, shape = lay_out(folder, first_call, "shape")
        folder, second_call = self.call(second)
        wide = encoded(journal(second_call, pgid=2**40, process_state="running"))
        (folder / "process.json").write_bytes(wide)
        [record] = command_processes(second["command_directory"], state=self.root)
        self.assertEqual((record["journal_state"], record["pgid"], record["journal_digest"]), ("invalid", None, sha(wide)))
        with self.assertRaises(HarnessError) as raised:
            ensure_dispatches_exited([second], state=self.root)
        self.assertEqual(stop_of(raised.exception), unconfirmed(second_call, "d2"))
        audits, saves = self.confirm([first, second], ["d1", "d2"])
        self.assertEqual([(event, fields["dispatch_id"], fields.get("journal_digest")) for event, fields in audits],
                         [("command_exit_confirmed_by_operator", "d1", sha(shape)),
                          ("dispatch_exit_confirmed_by_operator", "d1", None),
                          ("command_exit_confirmed_by_operator", "d2", sha(wide)),
                          ("dispatch_exit_confirmed_by_operator", "d2", None)])
        self.assertEqual(len(saves), 4)
        ensure_dispatches_exited([first, second], state=self.root)
        # The largest group ID pid_t holds is a journal's own fact, looked at like any other.
        entry = self.dispatch()
        folder, call = self.call(entry)
        (folder / "process.json").write_bytes(encoded(journal(call, pgid=2**31 - 1, process_state="running")))
        [record] = command_processes(entry["command_directory"], state=self.root)
        self.assertEqual((record["journal_state"], record["pgid"]), ("valid", 2**31 - 1))
        looked = []
        ensure_dispatches_exited([entry], state=self.root, observe=lambda pgid: looked.append(pgid) or False)
        self.assertEqual(looked, [2**31 - 1])

    def test_a_group_that_cannot_be_looked_at_is_unconfirmed_and_its_confirmation_says_so(self):
        entry = self.dispatch(exit_confirmed=False)
        folder, call = self.call(entry)
        (folder / "process.json").write_bytes(encoded(journal(call, pgid=4242, process_state="running")))
        def failing(error):
            def observe(pgid):
                raise error
            return observe
        # os.killpg's own errors besides ESRCH and EPERM; OverflowError for a group ID past pid_t.
        for error in (OSError(errno.EINVAL, os.strerror(errno.EINVAL)), OverflowError("pid_t")):
            with self.subTest(error=type(error).__name__):
                before = copy.deepcopy(entry)
                with self.assertRaises(HarnessError) as raised:
                    ensure_dispatches_exited([dict(entry, exit_confirmed=True)], state=self.root, observe=failing(error))
                self.assertEqual(stop_of(raised.exception), unconfirmed(call, entry["id"]))
                self.assertEqual(entry, before)
        # The confirmation that stop asks for is still written: the observation records that nothing was seen.
        observe = failing(OSError(errno.EINVAL, os.strerror(errno.EINVAL)))
        audits, saves = self.confirm([entry], [entry["id"]], observe=observe)
        self.assertEqual([(event, fields["observed"]["pgid"], fields["observed"]["group_alive"]) for event, fields in audits],
                         [("command_exit_confirmed_by_operator", 4242, None), ("dispatch_exit_confirmed_by_operator", None, None)])
        self.assertEqual(len(saves), 2)
        ensure_dispatches_exited([entry], state=self.root, observe=observe)

    def test_an_observation_that_fails_leaves_before_the_first_audit(self):
        # Every confirmation of the batch is observed before any is written: an interrupt while the second
        # dispatch's call is looked at leaves the first dispatch unwritten too.
        first, second = self.dispatch("d1", exit_confirmed=False), self.dispatch("d2", exit_confirmed=False)
        folder, call = self.call(first)
        lay_out(folder, call, "shape")
        folder, call = self.call(second)
        (folder / "process.json").write_bytes(encoded(journal(call, pgid=4242, process_state="running")))

        def observe(pgid):
            raise KeyboardInterrupt
        before, audits, saves = copy.deepcopy([first, second]), [], []
        with self.assertRaises(KeyboardInterrupt):
            self.confirm([first, second], ["d1", "d2"], audits, saves, observe=observe)
        self.assertEqual((audits, saves, [first, second]), ([], [], before))


PRINT ={"cwd": ".", "argv": [sys.executable, "-c", "print('ok')"]}


class BrokerJournalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.job = self.root / "dispatch"
        config = {"verification": {"commands": [PRINT]}, "permissions": {"worker_commands": []},
                  "runtime": {"verify_timeout_seconds": 5}}
        metadata = create_manifest(config, self.root, self.job, None)
        self.broker = Broker(metadata["artifact"], metadata["artifact_digest"])

    def call(self):
        return handle({"method": "tools/call", "params": {"name": "run_command",
                                                          "arguments": {"command_id": command_id(PRINT)}}}, self.broker)

    def test_admission_checks_receipts_against_the_gate_records(self):
        for _ in range(2):
            self.assertFalse(self.call()["isError"])
        journals = sorted((self.job / "commands").glob("*/process.json"))
        gate, listings = command_server.ensure_command_processes_exited, []

        def gate_then_rewrite(*args, **kwargs):
            records = gate(*args, **kwargs)
            # The same facts in other bytes: a journal read again would no longer match its receipt.
            for path in journals:
                path.write_text(json.dumps(json.loads(path.read_text())), encoding="utf-8")
            return records
        reads = FakeOs(open=passing)
        listing = command_server.command_processes
        with patch.object(command_server, "ensure_command_processes_exited", gate_then_rewrite), \
                patch.object(command_server, "command_processes",
                             lambda *args, **kwargs: listings.append(args) or listing(*args, **kwargs)), \
                patch.object(state_files, "os", reads):
            response = self.call()
        self.assertFalse(response["isError"], response)
        self.assertEqual(json.loads(response["content"][0]["text"])["exit_code"], 0)
        self.assertEqual((len(listings), reads.count("open", "process.json")), (1, len(journals)))

    def test_admission_reads_journals_from_the_job_s_own_directory_down(self):
        self.assertFalse(self.call()["isError"])
        above = {identity(path) for path in self.root.parents}
        with journal_opens() as opened:
            self.assertFalse(self.call()["isError"])
        # The manifest's job is the trusted path: its directory is the root, the job itself is opened without following.
        self.assertEqual({path for path, _ in opened if path is not None}, {str(self.root)})
        self.assertIn(identity(self.job), {ident for _, ident in opened})
        self.assertFalse({ident for _, ident in opened} & above)

    def test_a_journal_without_a_confirmed_exit_refuses_the_next_call_at_once(self):
        for layout in ("fifo", "absent", "shape"):
            with self.subTest(layout=layout):
                shutil.rmtree(self.job / "commands")
                (self.job / "commands").mkdir()
                folder = self.job / "commands" / uuid.uuid4().hex
                folder.mkdir()
                lay_out(folder, f"{self.job.name}:command:{folder.name}", layout)
                with unblocked(folder / "process.json", layout):
                    response = self.call()
                self.assertTrue(response["isError"])
                self.assertIn("Cannot confirm command process exited", json.loads(response["content"][0]["text"])["error"])
                self.assertEqual(list((self.job / "commands").iterdir()), [folder])


class JournalLifecycleTests(RepoCase):
    """A worker left a broker call in ``starting`` and failed: the run stopped at process_unconfirmed/command_exit."""

    def stopped_run(self, failures=1):
        def fail_leaving_a_call(backend, kwargs, packet):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) <= failures:
                job = Path(kwargs["job"])
                folder = job / "commands" / uuid.uuid4().hex
                folder.mkdir()
                write_json(folder / "process.json", journal(f"{job.name}:command:{folder.name}"))
                raise HarnessError("worker CLI failed; no result")
        backend = ScriptedBackend()
        backend.before_result = fail_leaving_a_call
        engine = self.engine(backend)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(HarnessError):
            engine.start("Implement greet", read_json(ROOT / "examples" / "plan.json"))
        return backend, engine

    def workers(self, engine):
        return [d for d in read_json(engine.directory / "state.json")["dispatches"] if d["role"] == "worker"]

    def the_call(self, worker):
        [folder] = Path(worker["command_directory"]).iterdir()
        return folder, f"{worker['id']}:command:{folder.name}"

    def unchanged(self, engine):
        """A check that the ledger and the event log still hold the bytes they hold now."""
        paths = (engine.directory / "state.json", engine.directory / "events.jsonl")
        before = [path.read_bytes() for path in paths]
        return lambda: self.assertEqual([path.read_bytes() for path in paths], before)

    def restored_at_cleanup(self, folder, call):
        # Before the stop guard abandons the run through its listed exit: a journal that exit can confirm.
        self.addCleanup(lambda: (clear(folder / "process.json"), write_json(folder / "process.json", journal(call))))

    def test_resume_abandon_and_cleanup_stop_on_each_journal_without_a_confirmed_exit(self):
        backend, engine = self.stopped_run()
        [worker] = self.workers(engine)
        folder, call = self.the_call(worker)
        self.restored_at_cleanup(folder, call)
        run_id, unchanged = engine.run["id"], self.unchanged(engine)
        for layout in LIFECYCLE_LAYOUTS:
            with self.subTest(layout=layout):
                lay_out(folder, call, layout)
                with unblocked(folder / "process.json", layout):
                    for operation in ("resume", "abandon"):
                        notice = refused_by_cli(self, backend, [operation, run_id],
                                                ("process_unconfirmed", "command_exit"), 3)
                        self.assertEqual((notice["data"]["command_id"], notice["source"]),
                                         (call, {"dispatch_id": worker["id"]}))
                    with self.assertRaises(HarnessError) as raised:
                        cleanup_run(self.repo, run_id)
                self.assertEqual(stop_of(raised.exception), unconfirmed(call, worker["id"]))
                unchanged()
        with self.subTest(layout="a stray file in commands/"):
            stray = folder.parent / "notes.txt"
            stray.write_text("")
            try:
                for operation in ("resume", "abandon"):
                    refused_by_cli(self, backend, [operation, run_id], ("evidence", "runtime_integrity"), 1)
                with self.assertRaises(EvidenceError):
                    cleanup_run(self.repo, run_id)
            finally:
                stray.unlink()
            unchanged()
        lay_out(folder, call, "shape")
        for name, fake, error_type in (("journal open EIO", FakeOs(open=failing(errno.EIO, "process.json")), "OSError"),
                                       ("commands lookup EACCES", FakeOs(stat=failing(errno.EACCES, "commands")),
                                        "PermissionError")):
            with self.subTest(failure=name), patch.object(state_files, "os", fake):
                for argv in (["resume", run_id], ["resume", run_id, "--confirm-exited", worker["id"]],
                             ["abandon", run_id], ["abandon", run_id, "--confirm-exited", worker["id"]]):
                    notice = refused_by_cli(self, backend, argv, ("runtime_failure", "io"), 1)
                    self.assertEqual(notice["data"]["error_type"], error_type)
                for confirm in ((), [worker["id"]]):
                    with self.assertRaises(StateReadError) as raised:
                        cleanup_run(self.repo, run_id, confirm_exited=confirm)
                    self.assertEqual(stop_of(raised.exception), local_io(error_type))
                unchanged()

    def exited_run(self):
        """A stopped run whose one call journal says exited: nothing but how it is read can stop an entry."""
        backend, engine = self.stopped_run()
        [worker] = self.workers(engine)
        folder, call = self.the_call(worker)
        self.restored_at_cleanup(folder, call)
        write_json(folder / "process.json", exited(call))
        return backend, engine, folder

    def test_each_entry_stops_typed_when_the_journal_walk_cannot_open_the_dispatch_directory(self):
        # Confirm review D4-T3b, blocker 1 (the journal's part): the enumeration opens the dispatch directory from its
        # parent's descriptor and never looks it up by path, so EIO at that open is local I/O in every entry, on every
        # Python version. The lookups by path that other gates still make belong to design 4's T5, not to this test.
        for operation in ("resume", "abandon", "cleanup-run"):
            with self.subTest(operation=operation):
                backend, engine, folder = self.exited_run()
                dispatch, hits = folder.parent.parent, []
                parent = identity(dispatch.parent)

                def fail(name, dir_fd):
                    hits.append(dir_fd is not None and name == dispatch.name and identity(dir_fd) == parent)
                    return hits[-1]
                unchanged = self.unchanged(engine)
                with journal_opens(fail):
                    notice = refused_by_cli(self, backend, [operation, engine.run["id"]], ("runtime_failure", "io"), 1)
                self.assertEqual(notice["data"]["error_type"], "OSError")
                self.assertTrue(any(hits))
                unchanged()

    def test_each_entry_through_a_linked_repo_reads_journals_from_the_state_directory_down(self):
        # Confirm review D4-T3b, suggestion: the walk starts at the run's state directory, resolved once, so a --repo
        # given through a link still passes every entry (the ledger records canonical paths), and no directory above
        # the state directory is opened while a journal is read.
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        alias = Path(outside.name) / "linked-repo"
        alias.symlink_to(self.repo, target_is_directory=True)
        state = self.repo.resolve() / ".harness"
        above = {identity(path) for path in state.parents}
        for operation in ("resume", "abandon", "cleanup-run"):
            with self.subTest(operation=operation):
                backend, engine, folder = self.exited_run()
                journal = identity(folder / "process.json")
                repo, self.repo = self.repo, alias
                try:
                    with journal_opens() as opened:
                        code, errors = run_cli(self, backend, [operation, engine.run["id"]])
                finally:
                    self.repo = repo
                self.assertEqual(code, 0, errors)
                self.assertIn(journal, {ident for _, ident in opened})
                self.assertEqual({path for path, _ in opened if path is not None}, {str(state)})
                self.assertFalse({ident for _, ident in opened} & above)

    def test_an_invalid_journal_is_confirmed_by_abandon_and_cleanup_until_its_bytes_change(self):
        backend, engine = self.stopped_run()
        [worker] = self.workers(engine)
        folder, call = self.the_call(worker)
        run_id = engine.run["id"]
        _, raw = lay_out(folder, call, "shape")
        refused_by_cli(self, backend, ["resume", run_id], ("process_unconfirmed", "command_exit"), 3)
        code, errors = run_cli(self, backend, ["abandon", run_id, "--confirm-exited", worker["id"]])
        self.assertEqual(code, 0, errors)
        self.assert_confirmed(engine, worker, call, [sha(raw)], status="abandoned")
        self.assertEqual(cleanup_run(self.repo, run_id)["run_status"], "abandoned")
        # Other invalid bytes are another journal: cleanup's gate stops until it is confirmed again.
        (folder / "process.json").write_bytes(raw + b" ")
        with self.assertRaises(HarnessError) as raised:
            cleanup_run(self.repo, run_id)
        self.assertEqual(stop_of(raised.exception), unconfirmed(call, worker["id"]))
        self.assertEqual(cleanup_run(self.repo, run_id, confirm_exited=[worker["id"]])["run_status"], "abandoned")
        self.assert_confirmed(engine, worker, call, [sha(raw), sha(raw + b" ")], status="abandoned")

    def test_an_oversized_exited_journal_is_confirmed_by_resume_bound_to_the_whole_file(self):
        backend, engine = self.stopped_run()
        [worker] = self.workers(engine)
        folder, call = self.the_call(worker)
        run_id = engine.run["id"]
        _, raw = lay_out(folder, call, "oversized")
        # It says exited and confirmed, but past 64 KiB it is never read as an exit.
        refused_by_cli(self, backend, ["resume", run_id], ("process_unconfirmed", "command_exit"), 3)
        code, errors = run_cli(self, backend, ["resume", run_id, "--confirm-exited", worker["id"]])
        self.assertEqual(code, 0, errors)
        self.assert_confirmed(engine, worker, call, [sha(raw)], status="completed")

    def assert_confirmed(self, engine, worker, call, digests, *, status):
        state = read_json(engine.directory / "state.json")
        entry = next(d for d in state["dispatches"] if d["id"] == worker["id"])
        self.assertEqual((state["status"], entry["command_exit_confirmations"][call]["journal_digest"]),
                         (status, digests[-1]))
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        self.assertEqual([(e["command_id"], e["journal_digest"]) for e in events
                          if e["event"] == "command_exit_confirmed_by_operator"], [(call, digest) for digest in digests])

    def test_a_call_without_a_readable_journal_cannot_be_confirmed_under_ledger_19(self):
        backend, engine = self.stopped_run()
        [worker] = self.workers(engine)
        folder, call = self.the_call(worker)
        self.restored_at_cleanup(folder, call)
        run_id, unchanged = engine.run["id"], self.unchanged(engine)
        for layout in ("absent", "fifo", "link", "directory"):
            with self.subTest(layout=layout):
                lay_out(folder, call, layout)
                with unblocked(folder / "process.json", layout):
                    for operation in ("resume", "abandon"):
                        notice = refused_by_cli(self, backend, [operation, run_id, "--confirm-exited", worker["id"]],
                                                ("process_unconfirmed", "command_exit"), 3)
                        self.assertEqual(notice["data"]["command_id"], call)
                    with self.assertRaises(HarnessError) as raised:
                        cleanup_run(self.repo, run_id, confirm_exited=[worker["id"]])
                self.assertEqual(stop_of(raised.exception), unconfirmed(call, worker["id"]))
                unchanged()

    def test_a_confirmation_batch_that_needs_a_journal_digest_writes_nothing(self):
        backend, engine = self.stopped_run(failures=2)
        run_id = engine.run["id"]
        [first] = self.workers(engine)
        first_folder, first_call = self.the_call(first)
        # Confirming the first failed worker lets the run go on to a second, which fails the same way.
        code, errors = run_cli(self, backend, ["resume", run_id, "--confirm-exited", first["id"]])
        self.assertEqual(code, 3, errors)
        first, second = self.workers(engine)
        folder, call = self.the_call(second)
        self.restored_at_cleanup(folder, call)
        # The first call's journal is now other, invalid bytes (confirmable); the second call has none to bind.
        _, raw = lay_out(first_folder, first_call, "shape")
        unchanged = self.unchanged(engine)
        for order, layout in (((first, second), "absent"), ((second, first), "fifo")):
            with self.subTest(order=[d["id"] for d in order], layout=layout):
                lay_out(folder, call, layout)
                argv = ["resume", run_id]
                for dispatch in order:
                    argv += ["--confirm-exited", dispatch["id"]]
                with unblocked(folder / "process.json", layout):
                    notice = refused_by_cli(self, backend, argv, ("process_unconfirmed", "command_exit"), 3)
                self.assertEqual((notice["data"]["command_id"], notice["source"]), (call, {"dispatch_id": second["id"]}))
                unchanged()
        # The first alone is confirmed as before; the gate then stops at the second call.
        lay_out(folder, call, "absent")
        notice = refused_by_cli(self, backend, ["resume", run_id, "--confirm-exited", first["id"]],
                                ("process_unconfirmed", "command_exit"), 3)
        self.assertEqual(notice["data"]["command_id"], call)
        state = read_json(engine.directory / "state.json")
        entry = next(d for d in state["dispatches"] if d["id"] == first["id"])
        self.assertEqual(entry["command_exit_confirmations"][first_call]["journal_digest"], sha(raw))
        self.assertNotIn("command_exit_confirmations", next(d for d in state["dispatches"] if d["id"] == second["id"]))

    def test_a_group_id_past_pid_t_or_one_not_looked_at_stops_each_entry_typed(self):
        # Code review D4-T3, blocker 2: resume, abandon and cleanup-run each let os.killpg's OverflowError out.
        backend, engine = self.stopped_run()
        [worker] = self.workers(engine)
        folder, call = self.the_call(worker)
        self.restored_at_cleanup(folder, call)
        run_id, unchanged = engine.run["id"], self.unchanged(engine)
        real = os.killpg

        def killpg(pgid, number):
            if pgid == 2**31 - 1 and number == 0:
                raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))  # neither ESRCH nor EPERM
            return real(pgid, number)
        for name, pgid in (("past pid_t", 2**40), ("os.killpg failing", 2**31 - 1)):
            with self.subTest(case=name), patch("harness.util.os.killpg", killpg):
                write_json(folder / "process.json", journal(call, pgid=pgid, process_state="running"))
                for operation in ("resume", "abandon"):
                    notice = refused_by_cli(self, backend, [operation, run_id], ("process_unconfirmed", "command_exit"), 3)
                    self.assertEqual((notice["data"]["command_id"], notice["source"]), (call, {"dispatch_id": worker["id"]}))
                with self.assertRaises(HarnessError) as raised:
                    cleanup_run(self.repo, run_id)
                self.assertEqual(stop_of(raised.exception), unconfirmed(call, worker["id"]))
                unchanged()
        # The exit that stop lists still works: the confirmation records that the group could not be looked at.
        with patch("harness.util.os.killpg", killpg):
            code, errors = run_cli(self, backend, ["resume", run_id, "--confirm-exited", worker["id"]])
        self.assertEqual(code, 0, errors)
        self.assert_confirmed(engine, worker, call, [sha((folder / "process.json").read_bytes())], status="completed")
        state = read_json(engine.directory / "state.json")
        observed = next(d for d in state["dispatches"] if d["id"] == worker["id"])["command_exit_confirmations"][call]["observed"]
        self.assertEqual((observed["pgid"], observed["group_alive"]), (2**31 - 1, None))

    def test_a_batch_with_a_group_id_past_pid_t_is_confirmed_whole(self):
        # Code review D4-T3, blocker 2: the first selected dispatch's confirmation was saved and audited, then the
        # second's observation raised OverflowError. Such a journal is invalid, so the batch is confirmed as one.
        backend, engine = self.stopped_run(failures=2)
        run_id = engine.run["id"]
        [first] = self.workers(engine)
        first_folder, first_call = self.the_call(first)
        code, errors = run_cli(self, backend, ["resume", run_id, "--confirm-exited", first["id"]])
        self.assertEqual(code, 3, errors)
        first, second = self.workers(engine)
        folder, call = self.the_call(second)
        _, shape = lay_out(first_folder, first_call, "shape")
        write_json(folder / "process.json", journal(call, pgid=2**40, process_state="running"))
        wide = (folder / "process.json").read_bytes()
        code, errors = run_cli(self, backend, ["resume", run_id, "--confirm-exited", first["id"],
                                               "--confirm-exited", second["id"]])
        self.assertEqual(code, 0, errors)
        state = read_json(engine.directory / "state.json")
        self.assertEqual(state["status"], "completed")
        self.assertEqual([next(d for d in state["dispatches"] if d["id"] == dispatch["id"])["command_exit_confirmations"]
                          [ident]["journal_digest"] for dispatch, ident in ((first, first_call), (second, call))],
                         [sha(shape), sha(wide)])
        events = [json.loads(line) for line in (engine.directory / "events.jsonl").read_text().splitlines()]
        self.assertEqual([(e["command_id"], e["journal_digest"]) for e in events
                          if e["event"] == "command_exit_confirmed_by_operator"][-2:],
                         [(first_call, sha(shape)), (call, sha(wide))])


if __name__ == "__main__":
    unittest.main()
