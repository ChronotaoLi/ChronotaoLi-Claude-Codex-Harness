"""Resources and persistence barriers shared by named runtime services."""
from __future__ import annotations
import sys
from copy import deepcopy
from pathlib import Path
from . import git
from .adapters import CLIBackend
from .budgets import work_attempts
from .memory import Memory
from .run_store import RunStore
from .util import HarnessError, append_event, confirm_dispatches_exited, ensure_dispatches_exited, group_alive, validate_command_directories, write_json

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
        self.reverify_requested = False
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

    def snapshot(self):
        self.store.check_current()
        git.check_worktree_state(self.worktree)
        return git.snapshot(self.worktree, binding=self.run["worktree_binding"])

    def ignored_inventory(self):
        """The run worktree's current ignored set for one packet: bounded, never saved.

        An unreadable listing says so with its reason; it is never an empty list.
        """
        try:
            return git.bounded_paths(git.ignored_paths(self.worktree, binding=self.run["worktree_binding"]))
        except (HarnessError, OSError) as error:
            return {"coverage": "paths_only_not_contents", "entries": None, "entry_count": None,
                    "omitted_count": None, "error": str(error)[:500]}

    def save(self):
        """Publish through RunStore, then notify the current observer."""
        self.store.commit()
        if self.on_save is not None:
            self.on_save(self.run)

    def event(self, event, **fields):
        append_event(self.directory / "events.jsonl", self.run["id"], event, **fields)
        print(f"[harness] {event} {fields.get('task_id', '')}", file=sys.stderr, flush=True)

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
        # handoff.json is the only handoff artifact.
        write_json(self.directory / "handoff.json", handoff)

    def ensure_exited(self):
        validate_command_directories(self.run["dispatches"], self.directory)
        for entry in ensure_dispatches_exited(self.run["dispatches"], observe=group_alive):
            entry["exit_confirmed"] = True

    def confirm_exited(self, identifiers):
        """Record an operator's explicit assertion; never signal an unowned group."""
        validate_command_directories(self.run["dispatches"], self.directory)
        confirm_dispatches_exited(self.run["dispatches"], identifiers,
                                  audit=self.event, save=self.save, observe=group_alive)
