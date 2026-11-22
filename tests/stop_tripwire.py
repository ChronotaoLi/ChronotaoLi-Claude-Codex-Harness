"""The stop tripwire (design 5, 3.6): an ``unclassified`` stop a test actually publishes is a finding on that test.

Importing this module installs, once per process, wrappers on the functions that publish a stop, and wraps
``unittest.TestCase.run`` so that each test checks its own findings in its last cleanup. ``test_harness`` imports
it and ``run_shards`` lists it first in every shard command, so every test process of the suite has it.

A publication is counted where a reader can see it, and only when this call made it:

- cli: ``cli.report_stop`` returned a projection of kind ``unclassified``;
- cli_notice: ``cli.stop_notice`` outside ``report_stop`` (``operate``'s success branch) printed one;
- record: ``OperationRecord.finish``'s own ``write_json`` of its record returned, and the record's stop is one;
- ledger: inside ``Workflow.stop``, ``RunStore.commit``'s own ``_publish_candidate`` of the ledger returned, and the
  candidate it wrote (the live ledger at the next ``store_revision``) holds one; a commit refused or failed
  before that write counts nothing, a failure after it (reading the bytes back) still counts;
- escape: an exception left ``cli._main`` that is not ``SystemExit`` and whose stop is one;
- entry: an exception whose stop is one left ``Runtime.start``/``resume``/``abandon``,
  ``workspace_inputs.prepare_run`` or ``cleanup_run`` (tests that bypass the CLI).

A write is observed on the call that made it: the calls in progress (``Workflow.stop``, ``commit``, ``finish``) are kept
per thread, and the writes stay wrapped for the whole process, so a commit of another thread never marks this
one. Publications of one test that share an exception, an operation ID or a commit identity
``(ledger path, store_revision)`` are one finding. A finding is exempt when every exception in it was raised by test
code (``injected``), when the test's ``allow_unclassified`` is a non-empty string, or when the test took it with
``consume``. With ``ENFORCE`` a finding that is not exempt fails its test; without it findings are only reported.
``HARNESS_STOP_TRIPWIRE_REPORT`` names a file that gets one JSON line per finding, exempt ones included; a write of it
that fails leaves the test's result alone, appends to ``<report>.incomplete`` where it can (not beside a report that is
no regular file), and the process ends with one ``INCOMPLETE`` line on stderr, its details one JSON object in printable
ASCII.

The work has two parts of different kinds.

Registration (``_guard``) runs right after a watched call returned or raised, with the asynchronous signals held back on
the main thread (``Held``), and only appends values that are plain already: fields of the published stop value; of an
exception its type and identity, the stop hung on it (looked up in its and its classes' namespaces), the ``str`` items of
its ``args`` and the file names and lines of its traceback. It runs no code a test can override (no ``str`` or ``repr``
of the exception, no ``case.id()``, no ``stops.of``), opens, stats and resolves no path, and waits for nothing, so it
cannot hang while signals are held, and an ``Exception`` it raises is the tripwire's own failure: that fails the test
and never reaches the watched call, whose return value or exception (identity and ``__context__``) stays. A
``KeyboardInterrupt`` or ``SystemExit`` goes on at once, as it is. It keeps no exception, so no traceback lives longer:
a weak reference, where the class takes one, tells whether an ``id`` is still the same exception's (``_key``).

Reporting (working out each exception's stop, merging, exempting, formatting, writing) runs with the signals as they
are, in three places, and calls nothing that catches every ``Exception`` (a signal handler's error would vanish there):
an exception's stop is worked out from what registration kept by the rules of ``stops.of`` (``_classified``; a stop it
carries is given its diagnostic and fitted to the size limit with ``stops._fit`` before it is checked, as ``of`` does),
never by calling it. The test's last cleanup (``_check``): only the report file's own ``OSError`` or ``ValueError``
marks the report incomplete (a handler's ``OSError`` inside that file I/O cannot be told from the file's own), and an
``lstat`` of the report that fails for another reason than its not being there marks it so before any open; anything
else raised there, a watchdog handler's error too, is that test's error, and what was not written stays for the next
place. The end of a test's run whose cleanups never ran, and the process's exit (``_unchecked``): every step of it,
counting what is left and reading the configuration included, is inside one ``try`` of its caller, where an
``Exception`` only marks the report incomplete and never replaces an exception that is leaving; an interrupt or exit
goes on. Not guaranteed: the exit's ``INCOMPLETE`` line when a handler raises just as the exit calls ``_announce``,
before the ``try`` inside it; the exception leaves the exit callback and the line is not printed. A test's ID is made as
unittest makes it, from its class's module and qualified name and its method's name; ``case.id()``, which a test may
override, is never asked.

Signals (``Held``): only the outermost registration on the main thread blocks them, and it puts the mask back as it
found it with one call, inside which Python runs the handlers of the signals that came meanwhile: what they raise, and
what they do to the mask, goes on as it would a few microseconds later without the tripwire. Not guaranteed: while
another thread of the process leaves these signals unblocked, the kernel may hand a signal to it, and Python still runs
the handler on the main thread at its next bytecode, which may be inside a registration; then a ``KeyboardInterrupt`` or
``SystemExit`` the handler raises goes on at once, and an ``Exception`` it raises (a watchdog's ``AssertionError``) is
taken for a failure of the tripwire and goes no further.

Not guaranteed either: that a registration runs no code a test can override holds for the objects the runtime makes,
those of built-in types and instances of classes defined under ``harness/``, whose attributes it reads are plain data
attributes. Nothing guards against an exception class or a watched class that overrides ``__dict__`` or an attribute a
registration reads with a descriptor of its own; a dict holding a key that hashes like a string key a registration looks
up and has an ``__eq__`` of its own; a ``str`` subclass as a keyword name or as a code object's ``co_filename``; a
CPython audit hook (``id()`` and a traceback's ``tb_frame`` raise audit events), trace function or profile function
installed in the test process. A built-in exception handed to ``report_stop`` before it was ever raised, and raised from
a watched entry afterwards, makes two findings, not one: its first registration saw no raising line (``_key``). That the
attributes of ``harness/`` a registration reads stay plain ones (none becomes, say, a property written in Python) is
what the structural self-test watches (``test_stop_tripwire.Registration``).
"""
from __future__ import annotations

import atexit
import errno
import functools
import inspect
import itertools
import json
import os
import signal
import sqlite3
import stat
import sys
import threading
import unittest
import weakref
from dataclasses import dataclass, field
from pathlib import Path

import harness
from harness import cleanup, cli, operations, stops, workspace_inputs
from harness.operations import OperationRecord
from harness.run_store import RunStore
from harness.runtime import Runtime
from harness.util import HarnessError
from harness.workflow import Workflow

ENFORCE = False
REPORT = "HARNESS_STOP_TRIPWIRE_REPORT"
# run_shards collects this stderr line, "<INCOMPLETE>: <one JSON object>", from the collection and every shard
INCOMPLETE = "stop tripwire: report incomplete"
TEXT_LIMIT = 160
TESTS_DIR = Path(__file__).resolve().parent
PACKAGE = Path(harness.__file__).resolve().parent
MARK = "__stop_tripwire__"
# Raised by the faulting instruction itself (or abort()): never held back. SIGKILL and SIGSTOP cannot be.
SYNCHRONOUS = frozenset({signal.SIGSEGV, signal.SIGBUS, signal.SIGFPE, signal.SIGILL, signal.SIGTRAP, signal.SIGSYS,
                         signal.SIGABRT} | ({signal.SIGEMT} if hasattr(signal, "SIGEMT") else set()))
HELD = frozenset(signal.valid_signals()) - SYNCHRONOUS - {signal.SIGKILL, signal.SIGSTOP}
# Registration reads an exception through BaseException's and type's own descriptors, so no code of its class runs.
_ARGS = BaseException.__dict__["args"].__get__
_TRACEBACK = BaseException.__dict__["__traceback__"].__get__
_MRO = type.__dict__["__mro__"].__get__
_NAMESPACE = type.__dict__["__dict__"].__get__
_OPAQUE = object()   # a part of a carried stop that is no JSON value: no stop with it validates


def injected(error):
    """Whether the exception was raised by test code (a patched function), not by the runtime."""
    return _injected(_frames(error))


def _injected(frames):
    if not frames:
        return False
    path = Path(frames[-1][0]).resolve()
    # A patched side_effect raises inside unittest.mock: test machinery, not the runtime.
    return path.is_relative_to(TESTS_DIR) or (path.name == "mock.py" and path.parent.name == "unittest")


@dataclass(frozen=True, eq=False)
class Seen:
    """What registration keeps of an exception: plain values, never the exception (its traceback holds frames)."""
    type: type
    id: int
    key: tuple       # the merge key, this exception's alone (``_key``)
    texts: tuple     # the str items of its args
    frames: tuple    # (file name, line) of its traceback, outermost first
    stop: object     # the stop hung on it, copied (``_plain``), or None


@dataclass(eq=False)
class Publication:
    channel: str
    stop: tuple | None    # (kind, reason, text) of the published value; None: the exception's, worked out when reported
    seen: Seen | None
    operation: str | None
    commit: tuple | None  # (ledger path, store_revision) the commit wrote


@dataclass(eq=False)
class Finding:
    channels: frozenset
    kind: str
    reason: str
    errors: tuple       # id() of each distinct exception in it
    injected: bool
    facts: dict
    exempt: str | None = None


@dataclass(eq=False)
class Frame:
    case: unittest.TestCase | None
    publications: list = field(default_factory=list)
    consumed: list = field(default_factory=list)
    failures: list = field(default_factory=list)   # the tripwire's own errors: (type, str items of args)


@dataclass(eq=False)
class Call:
    """One ``RunStore.commit`` or ``OperationRecord.finish`` in progress, and what its own write wrote."""
    path: object
    wrote: bool = False
    value: object = None


_tests = []        # frames of the running tests, innermost last (a test's threads publish on it too)
_outside = Frame(None)   # setUpClass, setUpModule and the like: reported, never failed
_local = threading.local()   # this thread's calls in progress; another thread's calls are never on its stacks
_lost = []         # the report writes that failed: the report misses these findings
_depth = 0         # the main thread's registrations in progress (Held)
_alive = {}        # id() of a registered exception still alive -> (weak reference to it, its key)
_serial = itertools.count()


class Held:
    """``with Held():`` runs a registration with the asynchronous signals (``HELD``) blocked, on the main thread, where
    Python runs signal handlers; another thread's registrations leave the mask alone. Only the outermost one blocks
    them, and it puts the mask back as it found it with one call: the handlers of signals that came meanwhile run
    inside that call, so what they raise leaves the registration as it ends."""

    def __enter__(self):
        global _depth
        self.counted, self.previous = False, None
        if threading.current_thread() is not threading.main_thread():
            return self
        if _depth == 0:
            previous = signal.pthread_sigmask(signal.SIG_BLOCK, ())   # a handler due already runs here, before it
            if not HELD <= previous:   # when all are blocked already there is nothing to block or put back
                try:
                    signal.pthread_sigmask(signal.SIG_BLOCK, HELD)
                except BaseException:
                    # A signal that came just before: its handler ran once they were blocked, so it never began.
                    signal.pthread_sigmask(signal.SIG_SETMASK, previous)
                    raise
                self.previous = previous
        _depth += 1
        self.counted = True
        return self

    def __exit__(self, *exc_info):
        global _depth
        if self.counted:
            _depth -= 1
        if self.previous is not None:
            # Whatever this call raises goes on: a handler raised it, and what a handler did to the mask stays.
            signal.pthread_sigmask(signal.SIG_SETMASK, self.previous)
        return False


def _calls(name):
    """This thread's stack of calls in progress: ``stopping`` (Workflow.stop: error, operation ID), ``reporting``
    (report_stop: its stop_notice is the same publication), ``commit`` and ``finish`` (``Call``); innermost last."""
    calls = getattr(_local, name, None)
    if calls is None:
        calls = []
        setattr(_local, name, calls)
    return calls


def _frame():
    return _tests[-1] if _tests else _outside


# ---- registration: plain values only, with the signals held ---------------------------------------------------------

def _guard(function):
    """Run ``function``, a registration, as a step of the tripwire's own (``Held``). An ``Exception`` it raises is the
    tripwire's failure, kept as its type and the str items of its args, which fails the test at its check; the watched
    call's return value or exception goes on unchanged."""
    with Held():
        try:
            function()
        except Exception as failure:
            _frame().failures.append((type(failure), _texts(failure)))


def _step(function):
    """``function()`` run as a step of the tripwire's own (``_guard``); None when it raised."""
    found = []
    _guard(lambda: found.append(function()))
    return found[0] if found else None


def _texts(error):
    return tuple(item for item in _ARGS(error) if type(item) is str)


def _frames(error):
    frames, tb = [], _TRACEBACK(error)
    while tb is not None:
        frames.append((tb.tb_frame.f_code.co_filename, tb.tb_lineno))
        tb = tb.tb_next
    return tuple(frames)


def _key(error, frames):
    """A merge key that is ``error``'s alone. Its ``id`` may be a later exception's once it is gone, so a weak
    reference, which keeps neither it nor its traceback alive, follows it; an exception of a class that takes none (a
    built-in one) is keyed by its ``id``, type and raising line, so it can be taken only for a later one of its type
    raised at the same line at the same address, never for one raised elsewhere (which could change the exemption)."""
    ident = id(error)
    known = _alive.get(ident)
    if known is not None and known[0]() is error:
        return known[1]
    try:
        reference = weakref.ref(error, lambda _, ident=ident, alive=_alive: alive.pop(ident, None))
    except TypeError:
        return ("error", ident, type(error), frames[-1] if frames else None)
    key = ("error", next(_serial))
    _alive[ident] = (reference, key)
    return key


def _carried(error):
    """``error.stop`` read where ``getattr`` finds a plain value, its own ``__dict__`` then its classes' namespaces: no
    property or ``__getattr__`` of its class runs."""
    own = object.__getattribute__(error, "__dict__")
    if type(own) is dict and "stop" in own:
        return own["stop"]
    for klass in _MRO(type(error)):
        namespace = _NAMESPACE(klass)
        if "stop" in namespace:
            return namespace["stop"]
    return None


def _plain(value):
    """``value`` copied when it is made of JSON's own types exactly (no code of a subclass runs); any other part is
    ``_OPAQUE``."""
    kind = type(value)
    if value is None or kind is str or kind is int or kind is float or kind is bool:
        return value
    if kind is list:
        return [_plain(item) for item in value]
    if kind is dict and all(type(key) is str for key in value):
        return {key: _plain(item) for key, item in value.items()}
    return _OPAQUE


def _seen(error):
    frames = _frames(error)
    return Seen(type(error), id(error), _key(error, frames), _texts(error), frames, _plain(_carried(error)))


def _published(value):
    """(kind, reason, text) of a published stop value that is an unclassified one. A dict, a subclass of one too, is
    read through dict's own methods: no method of a subclass runs. Only fields of built-in types exactly count."""
    if not issubclass(type(value), dict):
        return None
    kind = dict.get(value, "kind")
    if type(kind) is not str or kind != "unclassified":
        return None
    data, reason = dict.get(value, "data"), dict.get(value, "reason")
    # The text is the published value's diagnostic (bounded when it was made), not the exception formatted again.
    if dict.__contains__(value, "detail"):
        detail = dict.get(value, "detail")
    else:
        detail = dict.get(data, "detail") if issubclass(type(data), dict) else None
    return "unclassified", reason if type(reason) is str else None, detail if type(detail) is str else ""


def _publish(channel, value, *, error=None, operation=None, commit=None):
    stop = _published(value)
    if stop is not None:
        _frame().publications.append(Publication(
            channel, stop, _seen(error) if issubclass(type(error), BaseException) else None,
            operation if type(operation) is str else None, commit))


def _left(channel, error):
    """``error`` left a watched entry; whether its stop is an unclassified one is worked out when reported."""
    _frame().publications.append(Publication(channel, None, _seen(error), None, None))


# ---- reporting: signals as they are ---------------------------------------------------------------------------------

def _diagnostic(seen):
    """The text ``stops.of`` makes an exception's diagnostic of (its ``str``, else its class's name), as far as
    registration kept it: the str items of its args, which is its ``str`` when it has one text argument."""
    return ", ".join(seen.texts) or seen.type.__name__


def _classified(seen):
    """(kind, reason) of the stop ``stops.of`` gives the exception registration kept as ``seen``, by the rules of
    ``stops.of`` and ``stops._fallback`` but without calling them: ``of`` catches every ``Exception``, a signal
    handler's too. ``test_stop_tripwire.Classified`` holds the two together."""
    kind, carried = seen.type, seen.stop
    if issubclass(kind, (KeyboardInterrupt, SystemExit)):
        return "interrupted", "signal"
    if carried is not None:   # its own stop when that is a valid one, else an invalid stop
        return ((carried["kind"], carried["reason"]) if _valid(carried, _diagnostic(seen))
                else ("unclassified", "untyped_harness_error"))
    if issubclass(kind, HarnessError):
        return "unclassified", "untyped_harness_error"
    if issubclass(kind, sqlite3.Error):
        return "runtime_failure", "database"
    if issubclass(kind, OSError):
        return "runtime_failure", "io"
    return "unclassified", "unexpected_exception"


def _valid(stop, text):
    """Whether ``stops.of`` keeps ``stop``, a carried stop made of plain values (``_plain``), for an exception whose
    diagnostic is ``text``: given that diagnostic and fitted by ``stops._fit`` (lists, then the diagnostic and the other
    free texts shortened until the whole value fits), it is one ``stops.validate`` takes. A value ``_fit`` cannot
    measure (a part that is no JSON value, ``_OPAQUE``, taken for one ``of`` cannot measure either), look up (no
    registered row) or shorten (``_shortenable``) never reaches it and is no valid stop, as for ``of``; on the others
    ``_fit`` raises only its own ``ValueError``, the value cannot be made to fit."""
    if type(stop) is not dict or type(stop.get("data")) is not dict or type(stop.get("source")) is not dict:
        return False
    value = {**stop, "data": {**stop["data"], "detail": stops.bounded(text)}}
    row = stop.get("kind"), stop.get("reason")
    if type(row[0]) is not str or type(row[1]) is not str or row not in stops.REGISTRY or _opaque(value) \
            or not _shortenable(stops.REGISTRY[row], value["data"]):
        return False
    try:
        stops.validate(stops._fit(value, len(text)))
    except (ValueError, HarnessError):   # it cannot be made to fit, validate refuses it: nothing else
        return False
    return True


def _opaque(value):
    if type(value) is list:
        return any(_opaque(item) for item in value)
    if type(value) is dict:
        return any(_opaque(item) for item in value.values())
    return value is _OPAQUE


def _shortenable(row, data):
    """Whether ``stops._fit`` can shorten every list field of ``data`` that it may (``row`` is the registry's): a map's
    values are all ones it iterates and the omission count is a number. Any other one ``validate`` refuses, and
    ``_fit`` raises ``TypeError`` once it gets to it: ``of`` keeps no such stop either way."""
    for name in row["data"]:
        if stops._listed(row, name):
            value, omitted = data.get(name), data.get(name + "_omitted", 0)
            if type(value) is dict and any(type(item) not in (str, list, dict) for item in value.values()):
                return False
            if type(omitted) not in (int, float, bool):
                return False
    return True


def _stop(publication):
    """(kind, reason, text) of a publication whose stop is an unclassified one, else None. An exception's text is its
    diagnostic (``_diagnostic``), bounded as ``stops.of`` bounds it."""
    if publication.stop is not None:
        return publication.stop
    kind, reason = _classified(publication.seen)
    if kind != "unclassified":
        return None
    return kind, reason, stops.bounded(_diagnostic(publication.seen))


def _where(path, line):
    path = Path(path).resolve()
    shown = path.relative_to(PACKAGE.parent) if path.is_relative_to(PACKAGE.parent) else Path(*path.parts[-2:])
    return f"{shown}:{line}"


def _facts(seen, text):
    if seen is None:
        return {"error_type": None, "where": None, "site": None, "text": text}
    frames = seen.frames
    site = next((frame for frame in reversed(frames) if Path(frame[0]).resolve().is_relative_to(PACKAGE)), None)
    return {"error_type": seen.type.__name__, "where": _where(*frames[-1]) if frames else None,
            "site": _where(*site) if site else None, "text": text, "injected": _injected(frames)}


def _merge(publications):
    """One finding per stop: the unclassified publications that share a key, an exception, an operation ID or a commit
    identity."""
    items = []   # (publication, its stop, its keys)
    for publication in publications:
        stop = _stop(publication)
        if stop is None:
            continue
        keys = {publication.seen.key} if publication.seen is not None else set()
        if publication.operation is not None:
            keys.add(("operation", publication.operation))
        if publication.commit is not None:
            keys.add(("commit", str(publication.commit[0]), publication.commit[1]))
        items.append((publication, stop, keys))
    groups = []   # [keys, members]; an item joins every group it shares a key with
    for index, (_, _, keys) in enumerate(items):
        joined = [group for group in groups if group[0] & keys]
        merged, members = set(keys), [index]
        for group in joined:
            merged |= group[0]
            members = group[1] + members
        groups = [group for group in groups if all(group is not other for other in joined)] + [[merged, members]]
    findings = []
    for _, members in sorted(groups, key=lambda group: min(group[1])):
        members = [items[index] for index in sorted(members)]
        firsts = []   # the first publication of each distinct exception
        for publication, stop, _ in members:
            if publication.seen is not None and all(publication.seen.key != other.seen.key for other, _ in firsts):
                firsts.append((publication, stop))
        facts = [_facts(publication.seen, stop[2]) for publication, stop in firsts]
        first, stop, _ = members[0]
        findings.append(Finding(frozenset(publication.channel for publication, _, _ in members), stop[0], stop[1],
                                tuple(publication.seen.id for publication, _ in firsts),
                                bool(facts) and all(item["injected"] for item in facts),
                                facts[0] if facts else _facts(None, stop[2])))
    return findings


def _exempted(case, findings):
    for finding in findings:
        if finding.injected:
            finding.exempt = "injected"
        else:
            reason = getattr(case, "allow_unclassified", None) if case is not None else None
            finding.exempt = "allow_unclassified" if isinstance(reason, str) and reason else None
    return findings


def consume(case):
    """This test's findings so far, merged; they are taken (exempt) and still reported. Worked out here, in the
    test, as its check would."""
    frame = next((item for item in reversed(_tests) if item.case is case), None)
    if frame is None:
        return []
    taken = len(frame.publications)
    findings = _exempted(case, _merge(frame.publications[:taken]))
    del frame.publications[:taken]
    for finding in findings:
        finding.exempt = finding.exempt or "consume"
    frame.consumed.extend(findings)
    return findings


def _test_name(case):
    """The test's ID as unittest makes it, from its class's module and qualified name and its method's name;
    ``case.id()``, which a test may override, is not asked. A subtest's is its test's."""
    if case is None:
        return None
    if isinstance(case, unittest.case._SubTest):
        case = case.test_case
    return f"{type(case).__module__}.{type(case).__qualname__}.{case._testMethodName}"


def _line(test, case, finding):
    allowed = getattr(case, "allow_unclassified", None) if case is not None else None
    facts = finding.facts
    return {"test": test, "channels": sorted(finding.channels),
            "kind": finding.kind, "reason": finding.reason, "error_type": facts["error_type"],
            "where": facts["where"], "site": facts["site"], "text": facts["text"][:TEXT_LIMIT],
            "exempt": finding.exempt, "allow_unclassified": allowed if isinstance(allowed, (str, bool)) else None}


def _describe(failure):
    return f"{type(failure).__name__}: {failure}"


def _failure_text(kind, texts):
    return f"{kind.__name__}: {', '.join(texts)}"


def _report(frame):
    """Work out ``frame``'s findings and failures and write their lines (``_write``); only then are they taken from
    the frame, so what raises before leaves them to be reported later."""
    taken, failed = len(frame.publications), len(frame.failures)
    findings = frame.consumed + _exempted(frame.case, _merge(frame.publications[:taken]))
    failures = [_failure_text(*failure) for failure in frame.failures[:failed]]
    path = os.environ.get(REPORT)
    if path and (findings or failures):
        _write(path, frame.case, findings, failures)
    del frame.publications[:taken], frame.failures[:failed]
    frame.consumed.clear()
    return findings, failures


def _write(path, case, findings, failures):
    test = _test_name(case)
    lines = [_line(test, case, finding) for finding in findings]
    lines += [{"test": test, "channels": ["tripwire_error"], "kind": None,
               "reason": None, "error_type": None, "where": None, "site": None, "text": text[:TEXT_LIMIT],
               "exempt": None, "allow_unclassified": None} for text in failures]
    data = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines).encode("utf-8", "backslashreplace")
    try:
        _append(path, data)   # one append per test: the shards of one run write to the same file
    except (OSError, ValueError) as failure:
        # The report cannot be written: a fault of the report, not of the test, whose result stands.
        _incomplete(path, test, len(lines), _describe(failure), beside=not isinstance(failure, NotRegular))


class NotRegular(OSError):
    """The report path is no regular file (a pipe, a device, a directory, a link): never opened for writing."""


def _append(path, data):
    """Append ``data`` to the regular file ``path``. A path known to be something else is refused before it is opened,
    the open neither waits (a pipe with no reader) nor follows a link, and what it opened is checked again (the path
    may have been replaced in between)."""
    if _special(path):
        raise NotRegular("not a regular file")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise NotRegular("not a regular file")
        while data:
            data = data[os.write(descriptor, data):]
    finally:
        os.close(descriptor)


def _special(path):
    """Whether ``path`` is there and no regular file (``lstat``: a link is one too). Only its not being there is left
    to the open, which creates it; any other failure of the ``lstat`` is raised, and nothing is opened."""
    try:
        mode = os.lstat(path).st_mode
    except OSError as error:
        if error.errno == errno.ENOENT:
            return False
        raise
    return not stat.S_ISREG(mode)


def _one_line(entry):
    """``entry`` as JSON in printable ASCII (json escapes every other character): a path or an error text may hold a
    newline or any control character."""
    return json.dumps(entry, ensure_ascii=True)


def _incomplete(path, test, lines, error, *, beside=True):
    """Note that ``lines`` report lines of ``test`` were not written (``error``): kept for the exit line and, when
    ``beside``, in the marker beside the report, which the exit line then names."""
    entry = {"report": path, "test": test, "lines": lines, "error": error, "pid": os.getpid()}
    _lost.append(entry)
    if beside:
        marker = path + ".incomplete"
        try:
            _append(marker, (_one_line(entry) + "\n").encode("ascii"))
        except (OSError, ValueError):
            return   # not writable either: the exit line still tells
        entry["marker"] = marker


def _announce():
    """One stderr line when report writes failed; written at exit, after unittest's summary. Its details are one JSON
    object (``_one_line``), so no text in them can end the line or pass for a summary of unittest."""
    if not _lost:
        return
    first = _lost[0]
    try:
        details = {"lines": sum(entry["lines"] for entry in _lost), "writes": len(_lost), "report": first["report"],
                   "first_error": first["error"],
                   "marker": next((entry["marker"] for entry in _lost if "marker" in entry), None)}
        print(f"{INCOMPLETE}: {_one_line(details)}", file=sys.stderr, flush=True)
    except Exception:
        pass   # no stderr left at exit


def _safely(function, *args):
    try:
        return function(*args)
    except Exception:
        return None


def _unchecked(frame):
    """Report ``frame`` where no test can fail for it any more: at the end of a test's run whose cleanups never ran
    (an interrupt), and at exit for what no test took. A failure of the report marks it incomplete (``_lose``). Its
    callers (``_test_run``, ``_at_exit``) hold every step they take, this call included, in one ``try``: an
    ``Exception`` there, raised while marking the report, is marked once more and never replaces an exception that is
    leaving; an interrupt or exit goes on."""
    try:
        _report(frame)
    except Exception as failure:
        _lose(frame, failure)


def _lose(frame, failure):
    """Mark the report incomplete, for ``failure``, by what ``frame`` still holds (an upper bound: some of it may have
    been written)."""
    path = os.environ.get(REPORT)
    if path:
        lines = len(frame.consumed) + len(frame.publications) + len(frame.failures)
        _incomplete(path, _safely(_test_name, frame.case), lines, _safely(_describe, failure))


def _at_exit():
    try:
        _unchecked(_outside)
    except Exception as failure:   # as at the end of a test's run
        _safely(_lose, _outside, failure)
    finally:
        _announce()


def _check(frame):
    """The test's last cleanup: report its findings, and those no test took, then fail it for what fails it. What is
    raised here, but for the report file's ``OSError`` or ``ValueError`` (``_write``), is this test's error, a signal
    handler's included, and what was not written is left for the end of the run (``_unchecked``)."""
    findings, failures = _report(frame)
    _report(_outside)
    failing = [finding for finding in findings if finding.exempt is None] if ENFORCE else []
    if failing or failures:
        lines = [f"{sorted(finding.channels)} {finding.kind}/{finding.reason} {finding.facts['error_type']} at "
                 f"{finding.facts['where']}: {finding.facts['text'][:TEXT_LIMIT]}" for finding in failing]
        frame.case.fail("stop tripwire: " + "; ".join(lines + [f"tripwire error: {text}" for text in failures]))


# ---- the wrappers -------------------------------------------------------------------------------------------------

def _wrote(name, path, value):
    calls = _calls(name)
    if calls and calls[-1].path is not None and path is calls[-1].path:
        calls[-1].wrote, calls[-1].value = True, value


def _ledger_written(store, candidate):
    """The ledger channel's one observation point: ``RunStore._publish_candidate(locked, candidate, data)`` returned.

    It marks the innermost ``RunStore.commit`` in progress on this thread, when ``store``'s ledger is that commit's, as
    having written ``candidate``: a write of another thread or of another call never marks it. It relies on one step of
    ``RunStore.commit`` (harness/run_store.py): ``_publish_candidate``, called under ``store.lock`` after the
    compare-and-swap check, which returns once the write succeeded and before the candidate is adopted,
    ``candidate`` being the ledger it publishes with its new ``store_revision``. However that write is made, this
    observation point stays.
    """
    _wrote("commit", getattr(store, "_path", None), candidate)


def _publishing(observe):
    def wrap(original):
        @functools.wraps(original)
        def publish(store, locked, candidate, *args, **kwargs):
            result = original(store, locked, candidate, *args, **kwargs)
            _guard(lambda: observe(store, candidate))
            return result
        return publish
    return wrap


def _record_written(path, value):
    """The record channel's observation point: ``operations.write_json(path, value)`` returned inside the innermost
    ``OperationRecord.finish`` of this thread, for its record file (``finish`` writes ``self.record`` once)."""
    _wrote("finish", path, value)


def _observed(observe):
    def wrap(original):
        @functools.wraps(original)
        def write_json(path, value, *args, **kwargs):
            result = original(path, value, *args, **kwargs)
            _guard(lambda: observe(path, value))
            return result
        return write_json
    return wrap


def _report_stop(original):
    signature = inspect.signature(original)

    @functools.wraps(original)
    def report_stop(*args, **kwargs):
        reporting = _calls("reporting")
        reporting.append(True)
        try:
            value = original(*args, **kwargs)
        finally:
            reporting.pop()

        def note():
            bound = signature.bind(*args, **kwargs).arguments
            _publish("cli", value, error=bound.get("error"), operation=bound.get("operation_id"))
        _guard(note)
        return value
    return report_stop


def _stop_notice(original):
    signature = inspect.signature(original)

    @functools.wraps(original)
    def stop_notice(*args, **kwargs):
        result = original(*args, **kwargs)
        if not _calls("reporting"):
            def note():
                bound = signature.bind(*args, **kwargs).arguments
                _publish("cli_notice", bound.get("value"), operation=bound.get("operation_id"))
            _guard(note)
        return result
    return stop_notice


def _observing(name, path, function, publish):
    """Run ``function()`` as one ``name`` call in progress on this thread writing ``path``; ``publish(call)`` when its
    own write returned, whether or not the call returned afterwards."""
    call, calls = Call(path), _calls(name)
    calls.append(call)
    try:
        return function()
    finally:
        calls.remove(call)
        if call.wrote:
            _guard(lambda: publish(call))


def _finish(original):
    @functools.wraps(original)
    def finish(record, *args, **kwargs):
        return _observing("finish", _step(lambda: getattr(record, "path", None)),
                          lambda: original(record, *args, **kwargs),
                          lambda call: _publish("record", call.value.get("stop"), error=kwargs.get("error"),
                                                operation=call.value.get("operation_id")))
    return finish


def _operation(workflow):
    current = getattr(workflow.context, "operation", None)   # a test's stand-in context may have none
    return getattr(current, "id", None) if current is not None else None


def _workflow_stop(original):
    @functools.wraps(original)
    def stop(workflow, error, *args, **kwargs):
        operation = _step(lambda: _operation(workflow))
        stopping = _calls("stopping")
        stopping.append((error, operation))
        try:
            return original(workflow, error, *args, **kwargs)
        finally:
            stopping.pop()
    return stop


def _commit(original):
    @functools.wraps(original)
    def commit(store, *args, **kwargs):
        stopping = _calls("stopping")
        if not stopping:
            return original(store, *args, **kwargs)
        error, operation = stopping[-1]
        # The candidate is what this commit's own write wrote (``_ledger_written``), not rebuilt here.
        return _observing("commit", _step(lambda: getattr(store, "_path", None)),
                          lambda: original(store, *args, **kwargs),
                          lambda call: _publish("ledger", call.value.get("stop"), error=error, operation=operation,
                                                commit=(call.path, call.value.get("store_revision"))))
    return commit


def _leaving(channel):
    def wrap(original):
        @functools.wraps(original)
        def wrapped(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except BaseException as error:
                _guard(lambda: _left(channel, error))
                raise   # the very exception the call raised, its context untouched
        return wrapped
    return wrap


def _test_run(original):
    @functools.wraps(original)
    def run(case, result=None):
        frame = Frame(case)
        # Registered before setUp: cleanups run last-in first-out, so the check follows every cleanup of the test.
        case.addCleanup(_check, frame)
        _tests.append(frame)
        try:
            return original(case, result)
        finally:
            try:   # every step here is the tripwire's own: none may replace what the run raised (_unchecked)
                _tests.remove(frame)
                if frame.publications or frame.consumed or frame.failures:
                    _unchecked(frame)   # the check did not run, or left something unwritten
            except Exception as failure:
                _safely(_lose, frame, failure)
    return run


def _wrap(owner, name, make):
    current = getattr(owner, name)
    if not getattr(current, MARK, False):
        wrapper = make(current)
        setattr(wrapper, MARK, True)
        setattr(owner, name, wrapper)
    return getattr(owner, name)


def install():
    """Install every wrapper once; a second call (or a second import under another name) changes nothing."""
    _wrap(cli, "report_stop", _report_stop)
    _wrap(cli, "stop_notice", _stop_notice)
    _wrap(OperationRecord, "finish", _finish)
    _wrap(operations, "write_json", _observed(_record_written))
    _wrap(Workflow, "stop", _workflow_stop)
    _wrap(RunStore, "commit", _commit)
    _wrap(RunStore, "_publish_candidate", _publishing(_ledger_written))
    _wrap(cli, "_main", _leaving("escape"))
    for name in ("start", "resume", "abandon"):
        _wrap(Runtime, name, _leaving("entry"))
    _wrap(workspace_inputs, "prepare_run", _leaving("entry"))
    wrapped = _wrap(cleanup, "cleanup_run", _leaving("entry"))
    if not getattr(cli.cleanup_run, MARK, False):
        cli.cleanup_run = wrapped   # cli imported it by name
    _wrap(unittest.TestCase, "run", _test_run)


install()
atexit.register(_at_exit)
