"""Invocation accounting and evidence-bound exclusions from task work budgets."""

from .services import RuntimeService
import re

from .contracts import validate_work_bounds, validate_work_core
from .worker_receipts import delivered_work_contract
from .recovery import read_bound_result
from .util import HarnessError


def legacy_accounting(run):
    """Schema 15 and earlier saved their own run-wide counter and task-budget policy."""
    version = run.get("schema_version")
    return not (type(version) is int and version >= 16)


def historical_policy(run):
    """The task-budget policy a schema 15-or-earlier ledger saved, for display only.

    Those ledgers stored attempt_budget_policy, and an absent field meant
    all_invocations. Schema 16 removed the field: it always counts work.
    """
    return run.get("attempt_budget_policy", "all_invocations")  # historical display branch


def counts_work(run):
    return not legacy_accounting(run) or historical_policy(run) == "work"


def work_attempts(run):
    """Project saved accounting without changing the monotonic invocation totals.

    Work counting spares only evidence-bound environment/operator waits.
    """
    attempts = dict(run.get("attempts", {}))
    if counts_work(run):
        for entry in run.get("dispatches", []):
            if isinstance(entry, dict) and entry.get("attempt_exemption"):
                task_id = entry.get("task_id")
                attempts[task_id] = attempts.get(task_id, 0) - 1
    return attempts


def attempts_total(run):
    """Every worker invocation of the run, derived from the per-task counters."""
    return sum(value for value in run.get("attempts", {}).values() if type(value) is int)


class AttemptAccounting(RuntimeService):
    def task_budget_used(self, task_id):
        return work_attempts(self.context.run).get(task_id, 0)

    def validate_attempt_accounting(self):
        attempts = self.context.run["attempts"]
        if any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in attempts.items()):
            raise HarnessError("任务调用计数无效；保留台账")
        tasks = {task["id"] for task in self.context.run["tasks"]}
        for entry in self.context.run["dispatches"]:
            if "attempt_exemption" not in entry:
                continue
            exemption = entry["attempt_exemption"]
            task_id, dispatch_id = entry.get("task_id"), entry.get("id")
            if (entry.get("role") != "worker" or entry.get("invoked") is not True
                    or not isinstance(task_id, str) or task_id not in tasks or not isinstance(dispatch_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", dispatch_id)
                    or not isinstance(exemption, dict)
                    or set(exemption) != {"blocker_kind", "artifact", "artifact_digest"}
                    or exemption.get("blocker_kind") not in ("environment", "operator")
                    or not isinstance(exemption.get("artifact_digest"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", exemption["artifact_digest"])
                    or exemption.get("artifact") != str(self.context.directory / "dispatches" / dispatch_id / "returned-result.json")):
                raise HarnessError("任务尝试豁免来源无效；保留台账")
            result, _ = read_bound_result(exemption, "任务尝试豁免")
            contract = delivered_work_contract(self.context.run, self.context.directory, dispatch_id)
            # The whole core contract, as when the exemption was granted.
            validate_work_core(result, contract)
            validate_work_bounds(result, contract)
            if (result["status"] != "blocked" or result["blocker_kind"] != exemption["blocker_kind"]
                    or entry.get("worker_outcome") != exemption["blocker_kind"] + "_blocked"):
                raise HarnessError("任务尝试豁免与原始阻断结果不符；保留台账")
        if any(value < 0 for value in work_attempts(self.context.run).values()):
            raise HarnessError("任务尝试豁免超过实际调用次数；保留台账")

    def record_attempt_exemption(self, entry, result, source):
        """Only a fully validated, durably retained wait may spare the task budget.

        Caller saves this with the blocked feedback. Crashes before that boundary
        conservatively retain the invocation's charge. Neither counters nor
        previous results are retroactively rewritten.
        """
        if result["status"] != "blocked" or result["blocker_kind"] not in ("environment", "operator"):
            return
        entry["attempt_exemption"] = {
            "blocker_kind": result["blocker_kind"],
            "artifact": source["worker_artifact"], "artifact_digest": source["worker_artifact_digest"]}
        self.validate_attempt_accounting()
