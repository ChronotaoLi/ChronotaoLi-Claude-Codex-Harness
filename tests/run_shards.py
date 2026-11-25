"""Run the offline unittest suite in parallel shards and prove that every test ran once.

    python3 tests/run_shards.py [--shards 4] [--out DIR] [--pattern 'test*.py']

Test IDs are collected with standard discovery, exactly as by
``python3 -m unittest discover -s tests``; a test ID collected more than once
fails the run before any shard starts.  Modules stay together unless their
sole test class opts into method sharding and discovery finds no shared
fixture or custom loading.  Each group uses unittest's CLI loader and text
runner (loading ``stop_tripwire`` first when ``tests/stop_tripwire.py`` exists)
in its own process with the repository root as cwd and ``tests/`` on PYTHONPATH, so
test modules keep importing each other by top-level name (``test_x``, never
``tests.test_x``).  The runner's own writes are copied separately from test
stderr for outcome accounting.  The executed IDs are compared with the
collected IDs as multisets.

The exit status is non-zero when collection repeats a test ID, a shard exits
non-zero, a module fails to import, a log cannot be fully accounted for, or a
test is missing, duplicated or unexpected.  The single-process command remains
the reference run.
"""

import argparse
import collections
import functools
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import git_isolation  # collection and every shard inherit the isolated environment

ROOT = Path(__file__).resolve().parent.parent
START_DIR = "tests"
DEFAULT_PATTERN = "test*.py"
FAILED_IMPORT = "unittest.loader._FailedTest."
FIXTURES = frozenset({"setUpModule", "tearDownModule", "setUpClass", "tearDownClass"})
STOP_GRACE = 10.0
LIST_LIMIT = 50
TRIPWIRE = "stop_tripwire"   # tests/stop_tripwire.py: no test of its own; it watches the tests of its process
# Rough load per unmeasured test, estimated from P9 shard runtimes.
ORDINARY_TEST_SECONDS = 4.34

# The stderr line the tripwire of the collection or of a shard writes at exit when its report file could not be
# written (test results stand): this prefix, ": ", then one JSON object in printable ASCII, so no path or error text
# in it can end the line.
REPORT_INCOMPLETE = "stop tripwire: report incomplete"
INCOMPLETE_TITLE = "stop tripwire report incomplete (test results unaffected)"

# Runs in a child process (cwd = root) so test imports never touch this process.
_COLLECT = r'''
import json, sys, unittest
from types import FunctionType, MethodType

def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item

def lifecycle_function(cls, name):
    method = getattr(cls, name, None)
    return getattr(method, "__func__", method)

def independent_methods(module, suite, tests, ids):
    if not tests:
        return {}
    cls = type(tests[0])
    if cls.__dict__.get("shard_methods") is not True:
        return {}
    # Package hooks can own the discovery and lifecycle of their descendants.
    package = module.__package__
    while package:
        if getattr(sys.modules.get(package), "load_tests", None) is not None:
            return {}
        package = package.rpartition(".")[0]
    if (type(suite) is not unittest.TestSuite
            or any(type(child) is not unittest.TestSuite for child in suite)
            or hasattr(module, "__path__")
            or any(getattr(module, hook, None) is not None
                   for hook in ("setUpModule", "tearDownModule"))
            or cls.__module__ != module.__name__
            or getattr(module, cls.__name__, None) is not cls
            or any(type(test) is not cls for test in tests)
            or getattr(cls, "_class_cleanups", None)):
        return {}
    if any(lifecycle_function(cls, name) is not lifecycle_function(unittest.TestCase, name)
           for name in ("setUpClass", "tearDownClass", "doClassCleanups", "__init__", "__call__", "run", "id")):
        return {}
    prefix = module.__name__ + "." + cls.__name__ + "."
    hints = cls.__dict__.get("shard_seconds")
    hints = hints if type(hints) is dict else {}
    weights = {}
    for test, test_id in zip(tests, ids):
        method = test._testMethodName
        # Named loads treat static methods, class methods and other callables as factories.
        if (test_id != prefix + method or not isinstance(getattr(cls, method, None), FunctionType)
                or not isinstance(getattr(test, method, None), MethodType)):
            return {}
        seconds = hints.get(method)
        weights[test_id] = seconds if type(seconds) in (int, float) and 0 < seconds <= sys.float_info.max else None
    return weights

module_suites = []
custom_loads = set()

class ModuleLoad:
    # Forward unittest's own lookups without probing the hook separately. Its
    # final load_tests lookup decides whether it calls the hook.
    def __init__(self, module):
        self.module = module
        self.used_hook = False

    def __dir__(self):
        return dir(object.__getattribute__(self, "module"))

    def __getattribute__(self, name):
        module = object.__getattribute__(self, "module")
        if name != "load_tests":
            return getattr(module, name)
        self.used_hook = False
        hook = getattr(module, name)
        self.used_hook = hook is not None
        return hook

class Loader(unittest.TestLoader):
    def loadTestsFromModule(self, module, *args, **kwargs):
        observed = ModuleLoad(module)
        suite = super().loadTestsFromModule(observed, *args, **kwargs)
        if object.__getattribute__(observed, "used_hook"):
            custom_loads.add(module.__name__)
        tests = list(flatten(suite))
        module_suites.append((module, suite, tests, [test.id() for test in tests]))
        return suite

loader = Loader()
suite = loader.discover(sys.argv[1], pattern=sys.argv[2])
failed = [test.id() for test in flatten(suite) if test.id().startswith("unittest.loader._FailedTest.")]
modules = [[module.__name__, ids] for module, _, _, ids in module_suites]
methods = {}
# Import-time module cleanups live in one unittest-wide stack with no owner metadata.
# Inspect it after all imports, including imports by other tests or package hooks.
if not getattr(unittest.case, "_module_cleanups", None):
    for module, suite, tests, ids in module_suites:
        if module.__name__ in custom_loads:
            continue
        weights = independent_methods(module, suite, tests, ids)
        if weights:
            methods[module.__name__] = weights
with open(sys.argv[3], "w", encoding="utf-8") as handle:
    data = {"modules": modules, "failed": failed, "errors": loader.errors}
    if methods:
        data["independent_methods"] = methods
    json.dump(data, handle)
'''


class CollectionError(Exception):
    def __init__(self, message, report_incomplete=()):
        super().__init__(message)
        self.report_incomplete = list(report_incomplete)  # the collecting process's REPORT_INCOMPLETE lines


@dataclass
class Collection:
    modules: dict  # module name -> test IDs in discovery order
    failed_modules: list
    errors: list
    report_incomplete: list = field(default_factory=list)  # the collecting process's REPORT_INCOMPLETE lines
    independent_methods: dict = field(default_factory=dict)  # module -> test ID -> optional seconds

    @property
    def ids(self):
        return [test_id for ids in self.modules.values() for test_id in ids]

    @property
    def duplicates(self):
        """Test IDs that discovery yielded more than once, e.g. from a custom ``load_tests``."""
        counts = collections.Counter(self.ids)
        return [{"test": test_id, "collected": count} for test_id, count in sorted(counts.items()) if count > 1]


def child_env(root):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(Path(root) / START_DIR), env.get("PYTHONPATH")) if part)
    return env


def collect(root, pattern, out_dir, python=sys.executable):
    """Discover tests like ``unittest discover -s tests -p PATTERN`` and group IDs by module."""
    target = Path(out_dir) / "collection.json"
    proc = subprocess.run([python, "-c", _COLLECT, START_DIR, pattern, str(target)],
                          cwd=root, env=child_env(root), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, errors="replace")
    # Discovery imports the test modules, so a stop published while loading them reaches the tripwire there too; read
    # before the exit status, so a collecting process that failed still says what its report misses.
    incomplete = [dict(entry, source="collection") for entry in report_incomplete(proc.stderr)]
    if proc.returncode != 0 or not target.is_file():
        detail = (proc.stderr or proc.stdout).strip()[-4000:]
        raise CollectionError(f"test discovery exited {proc.returncode}\n{detail}", incomplete)
    data = json.loads(target.read_text(encoding="utf-8"))
    modules = {}
    for name, ids in data["modules"]:
        modules.setdefault(name, []).extend(ids)
    failed = sorted({test_id[len(FAILED_IMPORT):] for test_id in data["failed"]})
    return Collection(modules, failed, data["errors"], incomplete, data.get("independent_methods", {}))


def split(counts, shards):
    """Split modules into at most ``shards`` groups, largest first onto the lightest group.

    Ties are broken by module name and group index, so equal input always
    gives the same groups.  Each group lists its modules in name order.
    """
    if shards < 1:
        raise ValueError("shards must be at least 1")
    groups = [[] for _ in range(shards)]
    loads = [0] * shards
    for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        index = min(range(shards), key=lambda i: (loads[i], i))
        groups[index].append(name)
        loads[index] += count
    return [sorted(group) for group in groups if group]


def plan_groups(collection, shards, root=None, pattern=DEFAULT_PATTERN):
    """Balance whole modules and discovered independent methods in the same units.

    The discovery result already reflects the caller's root and pattern.
    """
    counts = {name: len(ids) for name, ids in collection.modules.items() if ids}
    if shards == 1 or not collection.independent_methods:
        return split(counts, shards)
    weights = {name: count * ORDINARY_TEST_SECONDS for name, count in counts.items()
               if name not in collection.independent_methods}
    for methods in collection.independent_methods.values():
        weights.update((test_id, seconds or ORDINARY_TEST_SECONDS) for test_id, seconds in methods.items())
    return split(weights, shards)


# --- log parsing -------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# TextTestResult.startTest writes its description at the current stderr position. A cleanup can
# leave text on that line; the method repeated inside the parentheses identifies the actual start.
_START = re.compile(r"(?<![^\s()])(?P<name>[^\s()]+) \((?P<target>[^\s()]+)\)")
_DELIMITER = re.compile(r"(?:\n[^\n]*?)? \.\.\. ")
_SKIP = r"skipped (?:'.*'|\".*\")"
_STATUS = rf"ok|FAIL|ERROR|expected failure|unexpected success|{_SKIP}"
_SUBTEST = re.compile(rf"^  [^\s()]+ \([^\s()]+\) .+(?:\n[^\n]*?)? \.\.\. (?P<status>FAIL|ERROR|{_SKIP})$", re.M)
_SKIP_LINE = re.compile(_SKIP)
_STATUS_LINE = re.compile(_STATUS)
_STATUS_HEAD = re.compile(rf"(?:{_STATUS})(?=\n|\Z)")
_STATUS_TAIL = re.compile(rf"(?:{_STATUS})$")
_END = re.compile(r"^(?:={70}\n(?:ERROR|FAIL|UNEXPECTED SUCCESS): |-{70}\nRan \d+ tests? in )", re.M)
_RAN = re.compile(r"^-{70}\nRan (\d+) tests? in [\d.]+s\n\n(OK|FAILED|NO TESTS RAN)(?: \((.*)\))?$", re.M)
_BLOCK = re.compile(r"^={70}\n(ERROR|FAIL): ([^\n]*)\n(?:[^\n]*\n)??-{70}\n(.*?)(?=^={70}\n|^-{70}\nRan |\Z)",
                    re.M | re.S)
_DESCRIPTION = re.compile(r"^([^\s()]+) \(([^\s()]+)\)(.*)$")
_OUTCOMES = ("error", "failed", "unexpected success", "skipped", "expected failure", "passed")


def _kind(status):
    if status.startswith("skipped"):
        return "skipped"
    return {"ok": "passed", "FAIL": "failed", "ERROR": "error"}.get(status, status)


@dataclass
class Execution:
    test_id: str
    statuses: list = field(default_factory=list)

    @property
    def outcome(self):
        kinds = {_kind(status) for status in self.statuses}
        return next((kind for kind in _OUTCOMES if kind in kinds), "unknown")


@dataclass
class ParsedLog:
    executions: list
    fixtures: list  # (fixture name, class or module, status)
    ran: int | None
    verdict: str | None
    failures: list  # (ERROR|FAIL, description, first error line)
    report_incomplete: list = field(default_factory=list)  # REPORT_INCOMPLETE lines, decoded


_INCOMPLETE_PREFIX = re.escape(REPORT_INCOMPLETE + ": ")
# The tripwire can write immediately after an unterminated diagnostic. A joined
# warning starts its JSON object there. Keep the unreadable fallback for cut lines.
_INCOMPLETE = re.compile(rf"(?:^{_INCOMPLETE_PREFIX}|{_INCOMPLETE_PREFIX}(?=\{{))(.*)$", re.M)


def report_incomplete(text):
    """The REPORT_INCOMPLETE lines of a process's stderr, each decoded from its JSON (``{"unreadable": line}`` when
    that fails, e.g. a line cut short)."""
    entries = []
    for match in _INCOMPLETE.finditer(_ANSI.sub("", text)):
        try:
            entry = json.loads(match[1])
        except ValueError:
            entry = None
        entries.append(entry if isinstance(entry, dict) else {"unreadable": match[1]})
    return entries


def _test_id(name, target):
    # Python 3.11+ prints "name (module.Class.name)"; older versions "name (module.Class)".
    return target if target.endswith("." + name) else f"{target}.{name}"


def _fixture(name, target):
    return name in FIXTURES and not target.endswith("." + name)


def _result_starts(region):
    result_start = 0
    for match in _START.finditer(region):
        if match.start() < result_start:
            continue
        name, target = match["name"], match["target"]
        line_start = region.rfind("\n", 0, match.start()) + 1
        _, dot, method = target.rpartition(".")
        if dot and name.endswith(method):
            actual = method
        elif dot and match.start() == line_start:
            actual = name
        else:
            actual = next((fixture for fixture in FIXTURES if name.endswith(fixture)), None)
        delimiter = _DELIMITER.match(region, match.end()) if actual else None
        if delimiter:
            result_start = delimiter.end()
            yield match.start() + len(name) - len(actual), result_start, actual, target


def _statuses(segment, failure_statuses=()):
    """Statuses of one test, and fixture statuses printed after it without a description."""
    found = [match["status"] for match in _SUBTEST.finditer(segment)]
    lines = [line.rstrip() for line in segment.split("\n") if line.strip() and not _SUBTEST.match(line)]
    head = _STATUS_HEAD.match(segment)
    if head and head.group() in ("expected failure", "unexpected success"):
        # unittest prints these without marking the line finished, so a class or module
        # fixture result that follows is printed bare, without its description.
        return [head.group()], [line for line in lines[1:] if _STATUS_LINE.fullmatch(line)]
    if head:
        found.append(head.group())
    # Output a test wrote to stderr lands between the dots and the status,
    # so the status may also end the last line before the next test.
    if lines and not (head and len(lines) == 1):
        tail = _STATUS_TAIL.search(lines[-1])
        if tail:
            found.append(tail.group())
    # Cleanup diagnostics can follow a standalone status. Confirm earlier FAIL/ERROR lines against
    # the traceback headers so plain diagnostic text from a passing test cannot supply a failure.
    for line in lines:
        if line not in found and (line in failure_statuses or _SKIP_LINE.fullmatch(line)):
            found.append(line)
    return [status for status in found if status not in ("FAIL", "ERROR") or status in failure_statuses], []


def _error_line(body):
    """The first exception line of a traceback (the last line for an import failure)."""
    lines = [line for line in body.split("\n") if line.strip()]
    if lines and lines[0].startswith("ImportError: Failed to import test module"):
        return lines[-1].strip()
    for line in lines:
        if not line[0].isspace() and not line.startswith("Traceback ("):
            return line.strip()
    return lines[-1].strip() if lines else ""


def parse_log(text):
    """Return the executions, fixture results, summary and failure details of a ``unittest -v`` log."""
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    end = _END.search(text)
    region = text[:end.start()] if end else text
    failures = []
    failure_statuses = collections.defaultdict(set)
    if end:
        for kind, description, body in _BLOCK.findall(text[end.start():]):
            failures.append((kind, description, _error_line(body)))
            match = _DESCRIPTION.match(description)
            if match and not _fixture(match[1], match[2]):
                failure_statuses[_test_id(match[1], match[2])].add(kind)
    starts = list(_result_starts(region))
    executions, fixtures = [], []
    for index, (_, result_start, name, target) in enumerate(starts):
        stop = starts[index + 1][0] if index + 1 < len(starts) else len(region)
        segment = region[result_start:stop]
        if _fixture(name, target):
            statuses, _ = _statuses(segment, ("ERROR",))
            fixtures.append((name, target, statuses[-1] if statuses else "unknown"))
            continue
        test_id = _test_id(name, target)
        statuses, bare = _statuses(segment, failure_statuses.get(test_id, ()))
        fixtures.extend((f"unnamed fixture after {test_id}", None, status) for status in bare)
        previous = executions[-1] if executions else None
        # A second status for the same test (a failure after failed subtests, or a
        # cleanup error after a failure) repeats the description; it is not a rerun.
        if (previous and previous.test_id == test_id and previous.statuses
                and previous.outcome in ("error", "failed", "skipped")):
            previous.statuses.extend(statuses)
        else:
            executions.append(Execution(test_id, statuses))
    counts = collections.Counter(execution.test_id for execution in executions)
    for execution in executions:
        # A traceback header names the method exactly, even when stderr split or obscured its
        # result. Avoid assigning one traceback to multiple executions of a duplicated ID.
        failures_for_test = failure_statuses.get(execution.test_id)
        if failures_for_test and counts[execution.test_id] == 1:
            execution.statuses.extend(sorted(failures_for_test.difference(execution.statuses)))
    ran = verdict = None
    for summary in _RAN.finditer(text):
        ran, verdict = int(summary[1]), summary[0].rsplit("\n", 1)[-1]
    return ParsedLog(executions, fixtures, ran, verdict, failures, report_incomplete(text))


# --- running -----------------------------------------------------------------

# Give the standard text runner a private stream that also forwards its writes
# to stderr. Test and cleanup writes still go directly to the raw stderr log.
_RUN_SHARD = r'''
import os, sys, unittest

__unittest = True
if sys.path and sys.path[0] == "":
    sys.path[0] = os.getcwd()
sys.argv[0] = os.path.basename(sys.executable) + " -m unittest"

class ResultStream:
    def __init__(self, stderr, evidence):
        self.stderr = stderr
        self.evidence = evidence

    def write(self, text):
        self.evidence.write(text)
        return self.stderr.write(text)

    def flush(self):
        self.evidence.flush()
        self.stderr.flush()

    def __getattr__(self, name):
        return getattr(self.stderr, name)

class Runner(unittest.TextTestRunner):
    def __init__(self, **kwargs):
        super().__init__(stream=ResultStream(sys.stderr, evidence), **kwargs)

fd = os.environ.pop("HARNESS_SHARD_RESULT_FD", None)
if fd is None:
    unittest.main(module=None)  # allow running the recorded command on its own
fd = int(fd)
os.set_inheritable(fd, False)
with os.fdopen(fd, "w", encoding="utf-8", errors="backslashreplace") as evidence:
    unittest.main(module=None, testRunner=Runner)
'''


@dataclass
class Shard:
    index: int
    modules: list
    tests: int
    log: Path
    stdout_log: Path
    command: list
    returncode: int | None = None
    seconds: float | None = None
    parsed: ParsedLog | None = None
    test_ids: list | None = field(default=None, kw_only=True)


def _signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except OSError:
        pass


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


def _wait_groups(procs, groups, seconds):
    """Reap exited shard leaders and wait until no group has a member left."""
    deadline = time.monotonic() + seconds
    while True:
        for proc in procs:
            proc.poll()
        if not any(_group_alive(pgid) for pgid in groups) or time.monotonic() >= deadline:
            return
        time.sleep(0.1)


def _stop(procs, grace=STOP_GRACE):
    """Interrupt each shard's process group like Ctrl-C would, then kill what remains.

    Every shard leads its own session, so its process group ID is its PID.
    Groups are signalled whether or not the shard process itself already
    exited: descendants that outlive it, or ignore SIGINT, stay in the group.
    """
    groups = [proc.pid for proc in procs]
    for pgid in groups:
        _signal_group(pgid, signal.SIGINT)
    try:
        _wait_groups(procs, groups, grace)
    except KeyboardInterrupt:
        pass
    for pgid in groups:
        if _group_alive(pgid):
            _signal_group(pgid, signal.SIGKILL)
    for proc in procs:
        proc.wait()
    # Orphaned descendants are reaped by init once killed; wait briefly for that.
    _wait_groups(procs, groups, 5)


def make_shards(groups, collection, out_dir, root, python=sys.executable):
    """One unittest command per group; ``root``'s stop tripwire, when it has one, is the first module of each."""
    out_dir = Path(out_dir)
    # Imported first, the tripwire is installed before any test module imports a function it wraps by name.
    first = [TRIPWIRE] if (Path(root) / START_DIR / f"{TRIPWIRE}.py").is_file() else []
    method_modules = {test_id: module for module, methods in collection.independent_methods.items()
                      for test_id in methods}
    shards = []
    for index, targets in enumerate(groups, 1):
        modules, test_ids = [], []
        for target in targets:
            if target in method_modules:
                module = method_modules[target]
                if module not in modules:
                    modules.append(module)
                test_ids.append(target)
            else:
                modules.append(target)
                test_ids.extend(collection.modules.get(target, ()))
        shards.append(Shard(index, modules, len(test_ids),
                            out_dir / f"shard-{index}.log", out_dir / f"shard-{index}.stdout.log",
                            [python, "-c", _RUN_SHARD, "-v", *first, *targets], test_ids=test_ids))
    return shards


def run_groups(groups, collection, out_dir, root=ROOT, python=sys.executable, echo=print):
    """Run each group in its own unittest process and verify the results against ``collection``."""
    shards = make_shards(groups, collection, out_dir, root, python)
    with ExitStack() as stack:
        results = [stack.enter_context(tempfile.TemporaryFile(mode="w+", encoding="utf-8")) for _ in shards]
        wall = _run_shards(shards, results, root, echo)
        for shard, stream in zip(shards, results, strict=True):
            stream.seek(0)
            shard.parsed = parse_log(stream.read())
            stderr = shard.log.read_text(encoding="utf-8", errors="replace")
            shard.parsed.report_incomplete = report_incomplete(stderr)
    return verify(collection, shards, wall)


def _run_shards(shards, results, root, echo):
    env = child_env(root)
    procs = []  # shards whose process group may still have members
    started = time.monotonic()
    try:
        for shard, stream in zip(shards, results, strict=True):
            fd = stream.fileno()
            shard_env = dict(env, HARNESS_SHARD_RESULT_FD=str(fd))
            with open(shard.log, "wb") as err, open(shard.stdout_log, "wb") as out:
                procs.append(subprocess.Popen(shard.command, cwd=root, env=shard_env, stdin=subprocess.DEVNULL,
                                              stdout=out, stderr=err, pass_fds=(fd,), start_new_session=True))
        pending = dict(zip(procs, shards))
        while pending:
            for proc, shard in list(pending.items()):
                if proc.poll() is not None:
                    shard.returncode, shard.seconds = proc.returncode, time.monotonic() - started
                    del pending[proc]
                    if not _group_alive(proc.pid):
                        # An empty group never regains members, and its ID may be reused.
                        procs.remove(proc)
                    echo(f"shard {shard.index} finished: exit {shard.returncode} after {shard.seconds:.1f}s "
                         f"({len(shards) - len(pending)}/{len(shards)} done)")
            if pending:
                time.sleep(0.2)
    except BaseException:
        _stop(procs)
        raise
    return time.monotonic() - started


def verify(collection, shards, wall):
    """Compare executed IDs with collected IDs as multisets and summarise outcomes."""
    expected = collections.Counter(collection.ids)
    executed = collections.Counter()
    outcomes = collections.Counter()
    import_failures, fixture_errors, problems, failures, rows = [], [], [], [], []
    incomplete = list(collection.report_incomplete)
    for shard in shards:
        parsed = shard.parsed
        seen = collections.Counter()
        for execution in parsed.executions:
            if execution.test_id.startswith(FAILED_IMPORT):
                import_failures.append(f"{execution.test_id[len(FAILED_IMPORT):]} (shard {shard.index})")
                continue
            seen[execution.test_id] += 1
            outcomes[execution.outcome] += 1
            if execution.outcome == "unknown":
                problems.append(f"shard {shard.index}: no unittest outcome for {execution.test_id}")
        # Tests behind a skipped setUpClass/setUpModule never start; they are skipped, not missing.
        skipped_scopes = [scope + "." for name, scope, status in parsed.fixtures
                          if scope and name.startswith("setUp") and status.startswith("skipped")]
        if skipped_scopes:
            ids = shard.test_ids
            if ids is None:  # call sites that construct Shard directly still specify whole modules
                ids = [test_id for name in shard.modules for test_id in collection.modules.get(name, ())]
            for test_id in ids:
                if not seen[test_id] and test_id.startswith(tuple(skipped_scopes)):
                    seen[test_id] += 1
                    outcomes["skipped"] += 1
        for name, scope, status in parsed.fixtures:
            label = f"{name} ({scope})" if scope else name
            if not status.startswith("skipped"):
                fixture_errors.append(f"{label} {status} (shard {shard.index})")
            elif not scope:
                problems.append(f"shard {shard.index}: {label} was skipped without naming its class or module; "
                                f"its tests cannot be attributed")
        if parsed.ran is None:
            problems.append(f"shard {shard.index}: no unittest summary in {shard.log} (crashed or interrupted?)")
        elif parsed.ran != len(parsed.executions):
            problems.append(f"shard {shard.index}: log shows {len(parsed.executions)} test results "
                            f"but unittest ran {parsed.ran}")
        for kind, description, line in parsed.failures:
            match = _DESCRIPTION.match(description)
            is_test = match and not _fixture(match[1], match[2])
            test = _test_id(match[1], match[2]) + match[3] if is_test else description
            failures.append({"shard": shard.index, "kind": kind, "test": test, "line": line})
        incomplete.extend(dict(entry, source=f"shard {shard.index}") for entry in parsed.report_incomplete)
        executed += seen
        rows.append({"index": shard.index, "modules": shard.modules, "tests": shard.tests,
                     "executed": sum(seen.values()), "ran": parsed.ran, "verdict": parsed.verdict,
                     "exit": shard.returncode, "seconds": round(shard.seconds or 0.0, 3),
                     "log": str(shard.log), "stdout_log": str(shard.stdout_log), "command": shard.command})
    extra = executed - expected
    missing = sorted((expected - executed).elements())
    duplicated = [{"test": test_id, "executed": executed[test_id], "collected": expected[test_id]}
                  for test_id in sorted(extra) if test_id in expected]
    unexpected = sorted(test_id for test_id in extra if test_id not in expected)
    ok = (all(shard.returncode == 0 for shard in shards) and not missing and not duplicated
          and not unexpected and not import_failures and not fixture_errors and not problems
          and not (outcomes["failed"] or outcomes["error"] or outcomes["unexpected success"]))
    return {"ok": ok, "python": sys.version.split()[0], "collected": len(collection.ids),
            "executed": sum(executed.values()), "outcomes": {kind: outcomes[kind] for kind in (*_OUTCOMES, "unknown")},
            "wall_seconds": round(wall, 3), "shards": rows, "missing": missing, "duplicated": duplicated,
            "unexpected": unexpected, "import_failures": import_failures, "fixture_errors": fixture_errors,
            "log_problems": problems, "failures": failures,
            # Not a test outcome, so not part of "ok": the stop tripwire's report file misses findings.
            "report_incomplete": incomplete}


# --- reporting ---------------------------------------------------------------

def _listing(echo, title, items):
    if not items:
        return
    echo(f"{title} ({len(items)}):")
    for item in items[:LIST_LIMIT]:
        echo(f"  {item}")
    if len(items) > LIST_LIMIT:
        echo(f"  ... {len(items) - LIST_LIMIT} more in result.json")


def _incomplete_text(entry):
    # Decoded values are shown with repr: a path or an error text keeps to one line here too.
    if "unreadable" in entry:
        return f"{entry['source']}: unreadable line {entry['unreadable']!r}"
    return (f"{entry['source']}: {entry.get('lines')!r} line(s) of {entry.get('writes')!r} write(s) missing from "
            f"{entry.get('report')!r} (first error: {entry.get('first_error')!r})")


def report(result, echo):
    echo("")
    echo(f"{'shard':>5} {'modules':>7} {'tests':>6} {'executed':>8} {'ran':>6} {'exit':>5} {'seconds':>8}")
    for row in result["shards"]:
        ran = "-" if row["ran"] is None else row["ran"]
        echo(f"{row['index']:>5} {len(row['modules']):>7} {row['tests']:>6} {row['executed']:>8} "
             f"{ran:>6} {str(row['exit']):>5} {row['seconds']:>8.1f}")
    outcomes = result["outcomes"]
    echo(f"collected {result['collected']}, executed {result['executed']} "
         f"(missing {len(result['missing'])}, duplicated {len(result['duplicated'])}, "
         f"unexpected {len(result['unexpected'])})")
    others = ", ".join(f"{kind} {outcomes[kind]}" for kind in
                       ("expected failure", "unexpected success", "unknown") if outcomes[kind])
    echo(f"passed {outcomes['passed']}, failed {outcomes['failed']}, errors {outcomes['error']}, "
         f"skipped {outcomes['skipped']}" + (f", {others}" if others else ""))
    echo(f"wall time {result['wall_seconds']:.1f}s")
    _listing(echo, "failures", [f"{item['kind']} {item['test']} (shard {item['shard']}): {item['line']}"
                                for item in result["failures"]])
    _listing(echo, "import failures", result["import_failures"])
    _listing(echo, "fixture errors", result["fixture_errors"])
    _listing(echo, "missing tests", result["missing"])
    _listing(echo, "duplicated tests", [f"{item['test']}: ran {item['executed']} times, collected {item['collected']}"
                                        for item in result["duplicated"]])
    _listing(echo, "unexpected tests", result["unexpected"])
    _listing(echo, "log problems", result["log_problems"])
    _listing(echo, INCOMPLETE_TITLE, [_incomplete_text(entry) for entry in result["report_incomplete"]])
    echo("OK" if result["ok"] else "FAILED")


def _write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv=None, root=ROOT, echo=functools.partial(print, flush=True)):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--shards", type=_positive, default=4, help="number of parallel unittest processes (default 4)")
    parser.add_argument("--out", type=Path, help="empty or new directory for logs (default: a fresh temp directory)")
    parser.add_argument("--pattern", default=DEFAULT_PATTERN, help="discovery pattern, as in unittest discover -p")
    args = parser.parse_args(argv)
    if args.out is not None and args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
        parser.error(f"--out {args.out} must be a new or empty directory")
    out = args.out.resolve() if args.out is not None else Path(tempfile.mkdtemp(prefix="harness-shards-"))
    out.mkdir(parents=True, exist_ok=True)
    main_thread = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, _interrupt) if main_thread else None
    try:
        return _main(args, Path(root), out, echo)
    except KeyboardInterrupt:
        echo(f"interrupted: shard processes stopped; partial logs in {out}")
        return 130
    finally:
        if main_thread and previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _main(args, root, out, echo):
    echo(f"logs: {out}")
    began = time.monotonic()
    try:
        collection = collect(root, args.pattern, out)
    except CollectionError as error:
        echo(f"collection failed: {error}")
        _listing(echo, INCOMPLETE_TITLE, [_incomplete_text(entry) for entry in error.report_incomplete])
        _write_json(out / "result.json", {"ok": False, "collection_error": str(error),
                                          "report_incomplete": error.report_incomplete})
        return 1
    ids = collection.ids
    echo(f"collected {len(ids)} tests from {len(collection.modules)} modules in {time.monotonic() - began:.1f}s "
         f"(python {sys.version.split()[0]}, pattern {args.pattern!r})")
    duplicates = collection.duplicates
    if collection.failed_modules or not ids or duplicates:
        _listing(echo, "modules that failed to import", collection.failed_modules)
        for error in collection.errors:
            echo(error.rstrip())
        # A repeated ID cannot be attributed to one execution, so nothing runs.
        _listing(echo, "duplicate test IDs in collection",
                 [f"{item['test']}: collected {item['collected']} times" for item in duplicates])
        if not ids:
            echo("no tests collected")
        _listing(echo, INCOMPLETE_TITLE, [_incomplete_text(entry) for entry in collection.report_incomplete])
        _write_json(out / "result.json", {"ok": False, "collected": len(ids),
                                          "import_failures": collection.failed_modules,
                                          "import_errors": collection.errors,
                                          "duplicate_ids": duplicates,
                                          "report_incomplete": collection.report_incomplete})
        echo("FAILED")
        return 1
    groups = plan_groups(collection, args.shards)
    shards = make_shards(groups, collection, out, root)
    _write_json(out / "manifest.json", {
        "root": str(root), "python": sys.executable, "pattern": args.pattern,
        "pythonpath": child_env(root)["PYTHONPATH"], "collected": len(ids),
        "shards": [{"index": shard.index, "tests": shard.tests, "modules": shard.modules,
                    "command": shard.command} for shard in shards]})
    for shard in shards:
        echo(f"shard {shard.index}: {len(shard.modules)} modules, {shard.tests} tests -> {shard.log}")
    result = run_groups(groups, collection, out, root=root, echo=echo)
    _write_json(out / "result.json", result)
    report(result, echo)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
