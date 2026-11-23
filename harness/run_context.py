"""Resources and persistence barriers shared by named runtime services."""
from __future__ import annotations
import contextlib
import sys
from copy import deepcopy
from pathlib import Path
from . import git
from .adapters import CLIBackend
from .budgets import work_attempts
from .memory import Memory
from .operations import first_line, warn
from .process_runtime import defer_cleanup_signals
from .run_store import RunStore, replaced
from .state_files import LockUnavailable, StateFiles, holding
from .util import (HarnessError, append_event, confirm_dispatches_exited, ensure_dispatches_exited, group_alive,
                   one_line, validate_command_directories, write_json)

class RunContext:
    def __init__(self, config, repo, state=None, backend=None, commander="astra", operation=None):
        self.config = config
        self.repo = git.repository(Path(repo))
        from .private_state import ensure_private_state
        self.state = ensure_private_state(self.repo, state)
        self.memory = Memory(self.state / "memory.sqlite3")
        self.backend = backend or CLIBackend(config, self.state)
        self.commander = commander
        self.operation = operation
        self.store = RunStore(self.state)
        self.saved_stop = None  # (outgoing error, exit code) for a confirmed stop in this call
        self.reverify_requested = False
        self._run_lock = None
        # Optional post-commit observer; failures propagate after durable state.
        self.on_save = None

    @property
    def run(self):
        return self.store.run

    @run.setter
    def run(self, value):
        self.store.attach(value)

    @property
    def directory(self):
        return self.state / "runs" / self.run["id"]

    @property
    def worktree(self):
        return Path(self.run["worktree"])

    def _check_snapshot_workspace(self):
        self.store.check_current()
        git.check_worktree_state(self.worktree)

    def snapshot(self):
        self._check_snapshot_workspace()
        return git.snapshot(self.worktree, binding=self.run["worktree_binding"])

    def read_worktree(self):
        self._check_snapshot_workspace()
        return git.read_worktree(self.worktree, binding=self.run["worktree_binding"])

    def ignored_inventory(self):
        """The run worktree's current ignored set for one packet: bounded, never saved.

        An unreadable listing says so with its reason; it is never an empty list.
        """
        try:
            return git.bounded_paths(git.ignored_paths(self.worktree, binding=self.run["worktree_binding"]))
        except (HarnessError, OSError) as error:
            return {"coverage": "paths_only_not_contents", "entries": None, "entry_count": None,
                    "omitted_count": None, "error": str(error)[:500]}

    @property
    def run_lock_descriptor(self) -> int | None:
        """The lock file descriptor while the lifecycle callback owns it."""
        # An interrupt can skip clearing the reference, but holding still closes the lock.
        lock = self._run_lock
        return None if lock is None else lock.held_descriptor

    def run_lock(self, stop, use):
        """``use()`` with run.lock held, the run directory it lies in bound to the store first.

        Taken through ``holding``, so the lock is released however ``use`` ends, one signal included. A lock
        not taken leaves before ``use`` runs: busy with ``stop``; a run directory other than the one the store
        is bound to, told before run.lock is opened or made there, as the store's replacement stop; a link or
        non-regular file at any level, or a missing run directory, as damaged evidence; a failed open or flock
        as local I/O.
        """
        path = self.directory / "run.lock"
        entered = False

        def held(locked):
            nonlocal entered
            entered = True
            self.store.bind_directory(locked.identity)
            try:
                self._run_lock = locked
                return use()
            finally:
                self._run_lock = None
        try:
            return holding(StateFiles(self.state).lock(("runs", self.run["id"], "run.lock"), create=True,
                                                       identity=self.store.directory_identity), held)
        except LockUnavailable as error:
            if entered:  # not this lock's: one taken inside ``use``
                raise
            if error.outcome.state == "busy":
                raise HarnessError(f"已有进程持有运行锁：{path}", stop=stop) from None
            if error.outcome.state == "replaced":
                raise replaced() from None
            error.outcome.evidence({"artifact": str(path)})
            raise

    def save(self, *, terminal=False, stop=False):
        """Publish through RunStore, then notify the current observer."""
        revision = self.store.commit(terminal=terminal, stop=stop) if terminal or stop else self.store.commit()
        if revision is not RunStore.KEPT and self.on_save is not None:
            self.on_save(self.run)
        return revision

    def save_fact(self, *keys):
        """Publish verification feedback, carrying it into the stop if the save fails."""
        for key in keys:
            self.store.carry(key, self.run[key])
        try:
            self.save()
        except Exception as error:
            try:
                detail = first_line(error)
            except Exception:
                detail = type(error).__name__
            warn(f"验证反馈的提交失败（{detail}）；停止仍按原错误保存")
            return False
        return True

    @contextlib.contextmanager
    def transition(self):
        """Change the ledger in one step, then save it.

        The body only assigns values computed before it. Keep its save in
        the same signal deferral scope so a held signal cannot skip the save.
        """
        with defer_cleanup_signals(inherit=False):
            yield
            self.save()

    def event(self, event, **fields):
        append_event(self.directory / "events.jsonl", self.run["id"], event, **fields)
        print(f"[harness] {event} {one_line(fields.get('task_id', ''))}", file=sys.stderr, flush=True)

    def observe(self, event, **fields):
        """Append a best-effort observation that cannot change the lifecycle.

        The ledger and its event log are not written atomically: a crash can omit
        an event, and recovery neither re-emits nor repeats one.
        """
        try:
            self.event(event, **fields)
        except Exception:
            pass

    def checkpoint(self):
        # Model-independent handoff. Full logs remain separate and are never recycled as conversation.
        handoff = {"goal": self.run["goal"], "branch": self.run["branch"], "status": self.run["status"],
                   "accepted": self.run["accepted"], "pending": [t for t in self.run["tasks"] if t["id"] not in self.run["accepted"]],
                   "feedback": self.run.get("feedback"), "expected_head": self.run["expected_head"],
                   "original_tasks": self.run["original_tasks"], "base_commit": self.run["base_commit"],
                   "attempts": deepcopy(self.run["attempts"]),
                   "work_attempts": work_attempts(self.run),
                   "final_history": self.run["final_history"], "plan_revision": len(self.run["plan_history"]),
                   "environment_updates": deepcopy(self.run.get("environment_updates", [])),
                   "environment_checks": deepcopy(self.run.get("environment_checks", [])),
                   "decision_requests": deepcopy(self.run.get("decision_requests", [])),
                   "operator_decisions": deepcopy(self.run.get("operator_decisions", [])),
                   "task_checkpoints": deepcopy(self.run.get("task_checkpoints", [])),
                   "next": "Resume the harness run; every dispatch creates a fresh agent process."}
        if "result" in self.run:
            handoff["result"] = self.run["result"]
        # handoff.json is the only handoff artifact.
        write_json(self.directory / "handoff.json", handoff)

    def ensure_exited(self, *, commit=True):
        validate_command_directories(self.run["dispatches"], self.directory)
        try:
            exited = ensure_dispatches_exited(self.run["dispatches"], state=self.state, directory=self.directory,
                                             observe=group_alive)
        except HarnessError as error:
            stop = error.stop
            if isinstance(stop, dict) and stop.get("kind") == "process_unconfirmed":
                # The exits listed depend on whether a returned result waits unsealed.
                from .worker_receipts import returned_unsealed
                stop["data"]["returned_unsealed"] = returned_unsealed(
                    self.run, self.directory, stop["source"].get("dispatch_id"))
            raise
        save = commit and exited and self.store.matches_commit()
        for entry in exited:
            entry["exit_confirmed"] = True
        if save:
            self.save()

    def confirm_exited(self, identifiers):
        """Record an operator's explicit assertion; never signal an unowned group."""
        validate_command_directories(self.run["dispatches"], self.directory)
        confirm_dispatches_exited(self.run["dispatches"], identifiers, state=self.state,
                                  audit=self.event, save=self.save, observe=group_alive)
