"""Invocation accounting and evidence-bound exclusions from task work budgets."""
import re

from .contracts import WORK, envelope, validate, validate_work_semantics
from .recovery import read_bound_result
from .util import HarnessError


def work_attempts(run):
    """Project saved accounting without changing the monotonic invocation totals."""
    attempts = dict(run.get("attempts", {}))
    if run.get("attempt_budget_policy", "all_invocations") == "work":
        for entry in run.get("dispatches", []):
            if isinstance(entry, dict) and entry.get("attempt_exemption"):
                task_id = entry.get("task_id")
                attempts[task_id] = attempts.get(task_id, 0) - 1
    return attempts


class AttemptAccounting:
    def task_budget_used(self, task_id):
        return work_attempts(self.run).get(task_id, 0)

    def validate_attempt_accounting(self):
        policy = self.run.get("attempt_budget_policy", "all_invocations")
        if policy not in ("all_invocations", "work"):
            raise HarnessError("未知任务尝试计数策略；保留台账")
        attempts = self.run["attempts"]
        if any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in attempts.items()):
            raise HarnessError("任务调用计数无效；保留台账")
        tasks = {task["id"] for task in self.run["tasks"]}
        for entry in self.run["dispatches"]:
            if "attempt_exemption" not in entry:
                continue
            exemption = entry["attempt_exemption"]
            task_id, dispatch_id = entry.get("task_id"), entry.get("id")
            if (policy != "work" or entry.get("role") != "worker" or entry.get("invoked") is not True
                    or not isinstance(task_id, str) or task_id not in tasks or not isinstance(dispatch_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", dispatch_id)
                    or not isinstance(exemption, dict)
                    or set(exemption) != {"blocker_kind", "artifact", "artifact_digest"}
                    or exemption.get("blocker_kind") not in ("environment", "operator")
                    or not isinstance(exemption.get("artifact_digest"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", exemption["artifact_digest"])
                    or exemption.get("artifact") != str(self.directory / "dispatches" / dispatch_id / "returned-result.json")):
                raise HarnessError("任务尝试豁免来源无效；保留台账")
            result, _ = read_bound_result(exemption, "任务尝试豁免")
            validate(result, envelope(WORK, task_id, dispatch_id))
            validate_work_semantics(result)
            if (result["status"] != "blocked" or result["blocker_kind"] != exemption["blocker_kind"]
                    or entry.get("worker_outcome") != exemption["blocker_kind"] + "_blocked"):
                raise HarnessError("任务尝试豁免与原始阻断结果不符；保留台账")
        if any(value < 0 for value in work_attempts(self.run).values()):
            raise HarnessError("任务尝试豁免超过实际调用次数；保留台账")

    def record_attempt_exemption(self, entry, result, source):
        """Only a fully validated, durably retained wait may spare the task budget.

        Caller saves this with the blocked feedback. Crashes before that boundary
        conservatively retain the invocation's charge. Historical runs keep their
        original policy and no old counters or results are retroactively rewritten.
        """
        if (self.run.get("attempt_budget_policy") != "work" or result["status"] != "blocked"
                or result["blocker_kind"] not in ("environment", "operator")):
            return
        entry["attempt_exemption"] = {
            "blocker_kind": result["blocker_kind"],
            "artifact": source["worker_artifact"], "artifact_digest": source["worker_artifact_digest"]}
        self.validate_attempt_accounting()
