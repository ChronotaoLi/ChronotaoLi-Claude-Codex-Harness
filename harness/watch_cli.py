"""Compact JSONL observation using native events, independent of model execution."""
from __future__ import annotations

import json
import sys

from . import observe
from .util import HarnessError
from .watch import RunWatcher

STOPPED = frozenset(("completed", "abandoned", "blocked", "interrupted", "paused", "awaiting_approval"))


def watch_run(state, run_id, *, follow=False, stdout=None):
    from .cli import status_summary

    stdout = sys.stdout if stdout is None else stdout
    try:
        with observe.Observer(state, required=True) as observer:
            # Register first: writes racing the initial read remain queued.
            with RunWatcher(observer.reader, run_id) as watcher:
                previous = None
                while True:
                    run, error = observer._read_ledger(run_id)
                    if error:
                        raise HarnessError(error)
                    if run.get("id") != run_id:
                        raise HarnessError("运行台账 ID 与观察目录不符")
                    summary = status_summary(run)
                    active = [{key: item.get(key) for key in ("id", "role", "task_id", "pgid", "process_state")}
                              for item in run.get("dispatches", [])
                              if isinstance(item, dict) and item.get("status") == "running"]
                    snapshot = {"type": "snapshot", "id": run_id, "backend": watcher.backend,
                                "status": summary["status"], "current_task": summary["current_task"],
                                "accepted_tasks": summary["accepted_tasks"][:50],
                                "accepted_task_count": len(summary["accepted_tasks"]),
                                "work_attempts": summary["budgets"]["work_attempts"],
                                "error": summary.get("error"), "active_dispatches": active[:20],
                                "active_dispatch_count": len(active), "convergence": summary["convergence"],
                                "process_observation": "ledger_only"}
                    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
                    if encoded != previous:
                        print(encoded, file=stdout, flush=True)
                        previous = encoded
                    if not follow and summary["status"] in STOPPED:
                        return 0
                    while not watcher.wait(30):
                        pass  # timeout is only an interrupt boundary; no state read
    except observe.ObserveError as error:
        raise HarnessError(f"无法观察运行 {run_id}：{error}") from error
