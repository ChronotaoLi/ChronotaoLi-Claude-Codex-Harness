"""Operator environment updates and read-only prerequisite observations.

These records clarify task assumptions. They do not grant capabilities, amend
task obligations, or turn a worker's evidence into an accepted result.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import time

from .contracts import WORK, envelope, validate_structure, validate_work_semantics
from .recovery import read_bound_result, snapshot_digest
from .util import HarnessError, write_json


ENVIRONMENT_POLICY = (
    "Environment updates are explicit operator assertions for the named task, ordered by sequence. "
    "Use the latest update to correct stale environmental assumptions in task descriptions, old feedback, "
    "or dispatch instructions; preserve original goal, acceptance, file scope and actual tool permissions. "
    "They are not permission grants, proof of completion, or approval of prior worker evidence. "
    "Read the referenced prior worker result and existing candidate files; continue the remaining work, "
    "rechecking time-sensitive facts. Local checks only observe declared prerequisites at that instant. "
    "Never reinterpret an update as authority to bypass review, reroll a result, or execute external writes."
)


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise HarnessError(f"环境更新 {label} 必须是非空文本，最多 {limit} 字符且不含 NUL")


def validate_update(value):
    required = {"schema_version", "task_id", "blocker_dispatch_id", "summary"}
    if (not isinstance(value, dict) or not required <= value.keys()
            or value.keys() - required - {"checks"}):
        raise HarnessError("环境更新字段无效；需要 schema_version、task_id、blocker_dispatch_id、summary，可选 checks")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise HarnessError("环境更新 schema_version 必须为 1")
    for key, limit in (("task_id", 120), ("blocker_dispatch_id", 120), ("summary", 4000)):
        _text(value[key], key, limit)
    checks = value.get("checks", [])
    if not isinstance(checks, list) or len(checks) > 16:
        raise HarnessError("环境更新 checks 必须是最多 16 项的数组")
    for check in checks:
        if not isinstance(check, dict):
            raise HarnessError("环境检查必须是对象")
        kind = check.get("kind")
        if kind == "path" and set(check) == {"kind", "path", "path_type"}:
            _text(check["path"], "checks.path", 4096)
            if not Path(check["path"]).is_absolute() or check["path_type"] not in ("file", "directory", "any"):
                raise HarnessError("环境路径检查需要绝对路径和 file/directory/any 类型")
        elif kind == "executable" and set(check) == {"kind", "name"}:
            _text(check["name"], "checks.name", 4096)
            if "/" in check["name"] and not Path(check["name"]).is_absolute():
                raise HarnessError("环境可执行程序检查只接受程序名或绝对路径")
        elif kind == "worker_command" and set(check) == {"kind", "argv"}:
            argv = check["argv"]
            if not isinstance(argv, list) or not argv or len(argv) > 128:
                raise HarnessError("环境命令检查需要非空 argv，最多 128 项")
            for arg in argv:
                if (not isinstance(arg, str) or len(arg) > 4096
                        or any(char in arg for char in ("\0", "*", "\n", "\r"))):
                    raise HarnessError("环境命令检查只接受精确 argv，不能包含 *、NUL 或换行")
            _text(argv[0], "checks.argv[0]", 4096)
        else:
            raise HarnessError("环境检查只支持 path、executable、worker_command 的规定字段")
    return {**deepcopy(value), "checks": deepcopy(checks)}


def read_update(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise HarnessError(f"环境更新含重复字段：{key}")
            result[key] = value
        return result

    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise HarnessError("环境更新文件超过 64 KiB")
        return validate_update(json.loads(raw, object_pairs_hook=unique_pairs))
    except (OSError, ValueError, UnicodeError) as error:
        raise HarnessError(f"无法读取环境更新 JSON：{error}") from error


class EnvironmentRecovery:
    def validate_environment_links(self):
        updates = self.run.get("environment_updates", [])
        checks = self.run.get("environment_checks", [])
        if not isinstance(updates, list) or not isinstance(checks, list):
            raise HarnessError("环境恢复台账字段无效")
        if self.run["schema_version"] == 8 and (updates or checks):
            raise HarnessError("含环境更新的台账必须使用版本 9")
        ids = set()
        tasks = {task["id"] for task in self.run["tasks"]}
        for sequence, row in enumerate(updates, 1):
            if (not isinstance(row, dict) or type(row.get("sequence")) is not int
                    or row["sequence"] != sequence or not isinstance(row.get("task_id"), str)
                    or row["task_id"] not in tasks
                    or not isinstance(row.get("id"), str) or row["id"] in ids
                    or row["id"] != "env_" + str(row.get("update_digest", ""))
                    or len(row["id"]) != 68
                    or any(c not in "0123456789abcdef" for c in row["id"][4:])
                    or not isinstance(row.get("artifact_digest"), str)
                    or row.get("artifact") != str(self.directory / "environment" / (row["id"] + ".json"))):
                raise HarnessError("环境更新台账引用无效；保留现场")
            ids.add(row["id"])
            if not any(d["id"] == row.get("blocker_dispatch_id") and d["role"] == "worker"
                       and d["task_id"] == row["task_id"] for d in self.run["dispatches"]):
                raise HarnessError("环境更新引用未知 worker dispatch")
        update_tasks = {row["id"]: row["task_id"] for row in updates}
        for row in checks:
            if (not isinstance(row, dict) or not isinstance(row.get("update_id"), str)
                    or row["update_id"] not in ids or not isinstance(row.get("task_id"), str)
                    or row["task_id"] != update_tasks[row["update_id"]]
                    or not isinstance(row.get("artifact"), str)
                    or not isinstance(row.get("artifact_digest"), str) or type(row.get("passed")) is not bool):
                raise HarnessError("环境检查引用或字段无效")
            name = Path(row["artifact"]).name
            if (not name.startswith("checks-") or not name.endswith(".json")
                    or not name[7:-5].isdigit()
                    or row["artifact"] != str(self.directory / "environment" / name)):
                raise HarnessError("环境检查产物路径无效")

    def environment_context(self):
        records = []
        for row in self.run.get("environment_updates", []):
            record, _ = read_bound_result(row, "环境更新")
            if not isinstance(record, dict):
                raise HarnessError("环境更新产物必须为对象")
            update = validate_update({key: record[key] for key in
                ("schema_version", "task_id", "blocker_dispatch_id", "summary", "checks") if key in record})
            digest = snapshot_digest({"run_id": self.run["id"], "update": update})
            if (digest != row["update_digest"] or record.get("sequence") != row["sequence"]
                    or record.get("id") != row["id"] or record.get("task_id") != row["task_id"]
                    or record.get("blocker_dispatch_id") != row["blocker_dispatch_id"]):
                raise HarnessError("环境更新产物与台账不一致")
            self.environment_source(record["task_id"], record["blocker_dispatch_id"], record.get("source"))
            records.append({**record, "artifact": row["artifact"], "artifact_digest": row["artifact_digest"]})
        return records

    def environment_packet(self, packet):
        records = self.environment_context()
        if not records:
            return packet
        latest = {record["task_id"]: record["id"] for record in records}
        observations = {}
        for row in self.run.get("environment_checks", []):
            if row["update_id"] == latest.get(row.get("task_id")):
                observations[row["task_id"]] = row
        reports = []
        for row in observations.values():
            report, _ = read_bound_result(row, "环境检查")
            if (not isinstance(report, dict) or report.get("update_id") != row["update_id"]
                    or report.get("task_id") != row["task_id"]):
                raise HarnessError("环境检查产物与台账不一致")
            reports.append({**report, "artifact": row["artifact"], "artifact_digest": row["artifact_digest"]})
        return {**packet, "environment_updates": records, "environment_observations": reports,
                "environment_update_policy": ENVIRONMENT_POLICY}

    def environment_source(self, task_id, dispatch_id, source):
        expected = self.directory / "dispatches" / dispatch_id / "returned-result.json"
        if (not isinstance(source, dict) or source.get("artifact") != str(expected)
                or not isinstance(source.get("artifact_digest"), str)):
            raise HarnessError("环境阻断原始证据引用无效")
        result, _ = read_bound_result(source, "环境阻断")
        validate_structure(result, envelope(WORK, task_id, dispatch_id))
        validate_work_semantics(result)
        if result["status"] != "blocked" or result["blocker_kind"] != "environment":
            raise HarnessError("环境更新必须绑定 worker 环境阻断结果")
        return result

    def prepare_environment_update(self, path):
        update = read_update(path)
        self.environment_context()
        task_id, dispatch_id = update["task_id"], update["blocker_dispatch_id"]
        feedback = self.run.get("feedback")
        if not isinstance(feedback, dict) or not isinstance(feedback.get("worker"), dict):
            raise HarnessError("环境更新缺少当前 worker 环境阻断反馈")
        # A pre-invocation interruption can leave interrupted/running status
        # without invalidating the saved blocker. Resume guards terminal runs;
        # the evidence and pending-state checks below guard this update boundary.
        if (self.run.get("current_task") != task_id
                or task_id in self.run["accepted"] or feedback.get("task_id") != task_id):
            raise HarnessError("--environment-update 仅用于当前未接受任务的环境阻断")
        busy = ("pending_task", "pending_acceptance", "pending_post_accept", "pending_replan", "pending_directive",
                "replan_stopped", "review_receipts", "commander_receipts", "pending_format_corrections", "review_corrections")
        if any(self.run.get(key) for key in busy) or any(
                self.active_invalid_repair(key) for key in self.run["invalid_repairs"]):
            raise HarnessError("已有待处理阶段、指令、receipt 或修正；不能用环境更新替换原请求")
        matches = [i for i, d in enumerate(self.run["dispatches"])
                   if d["id"] == dispatch_id and d["role"] == "worker" and d["task_id"] == task_id]
        if not matches or any(d.get("role") in ("worker", "commander", "reviewer")
                              and d.get("invoked", True) for d in self.run["dispatches"][matches[0] + 1:]):
            raise HarnessError("环境阻断来源已过期；后续模型调用已开始，保留原恢复路径")
        source = {"artifact": feedback.get("worker_artifact"), "artifact_digest": feedback.get("worker_artifact_digest")}
        result = self.environment_source(task_id, dispatch_id, source)
        stored = {k: v for k, v in result.items() if k not in ("task_id", "dispatch_id")}
        supplied = {k: v for k, v in feedback["worker"].items() if k not in ("task_id", "dispatch_id")}
        if stored != supplied:
            raise HarnessError("当前环境反馈与原始 worker 证据不一致")
        task = next(t for t in self.run["tasks"] if t["id"] == task_id)
        self.check_task_scope(task)
        digest = snapshot_digest({"run_id": self.run["id"], "update": update})
        ident = "env_" + digest
        if any(row["id"] == ident for row in self.run.get("environment_updates", [])):
            return None
        if len(self.run.get("environment_updates", [])) >= 32:
            raise HarnessError("环境更新最多 32 条；保留全部历史，不静默裁剪")
        return {**update, "id": ident, "sequence": len(self.run.get("environment_updates", [])) + 1,
                "source": source, "recorded_at": time.time(), "update_digest": digest}

    def record_environment_update(self, record):
        if record is None:
            return
        artifact = self.directory / "environment" / (record["id"] + ".json")
        if artifact.exists():
            # Recover artifact-before-ledger interruption without replacing evidence.
            try:
                existing = json.loads(artifact.read_bytes())
            except (OSError, ValueError, UnicodeError) as error:
                raise HarnessError("未登记环境更新产物损坏；保留证据") from error
            if not isinstance(existing, dict) or {k: v for k, v in existing.items() if k != "recorded_at"} != {
                    k: v for k, v in record.items() if k != "recorded_at"}:
                raise HarnessError("未登记环境更新产物冲突；保留证据")
            record = existing
        else:
            write_json(artifact, record)
        row = {key: record[key] for key in ("id", "sequence", "task_id", "blocker_dispatch_id", "update_digest")}
        row.update(artifact=str(artifact), artifact_digest=hashlib.sha256(artifact.read_bytes()).hexdigest())
        # Audit before use. A crash can repeat the event with the same stable ID;
        # only the single ledger entry authorizes injection into model context.
        self.event("environment_update_recorded", **row)
        self.run["schema_version"] = 9
        self.run.setdefault("environment_updates", []).append(row)
        self.run.setdefault("environment_checks", [])
        self.save()

    def check_environment(self, task_id):
        records = [record for record in self.environment_context() if record["task_id"] == task_id]
        if not records or not records[-1]["checks"]:
            return
        record = records[-1]
        results = []
        for check in record["checks"]:
            try:
                if check["kind"] == "path":
                    path = Path(check["path"])
                    passed = {"file": path.is_file, "directory": path.is_dir, "any": path.exists}[check["path_type"]]()
                elif check["kind"] == "executable":
                    passed = shutil.which(check["name"]) is not None
                else:
                    allowed = self.config.data["permissions"]["worker_commands"] + self.config.data["verification"]["commands"]
                    passed = check["argv"] in allowed
                results.append({"check": check, "passed": passed})
            except OSError as error:
                results.append({"check": check, "passed": False, "error": str(error)})
        ident = len(self.run.setdefault("environment_checks", [])) + 1
        artifact = self.directory / "environment" / f"checks-{ident}.json"
        report = {"update_id": record["id"], "task_id": task_id, "observed_at": time.time(),
                  "results": results, "passed": all(item["passed"] for item in results),
                  "limitation": "Local observations only; no commands executed, permissions granted, or acceptance proven."}
        # Independent artifacts survive repeated failed resumes.
        while artifact.exists():
            ident += 1
            artifact = artifact.with_name(f"checks-{ident}.json")
        write_json(artifact, report)
        self.run["environment_checks"].append({"update_id": record["id"], "task_id": task_id,
            "passed": report["passed"], "artifact": str(artifact),
            "artifact_digest": hashlib.sha256(artifact.read_bytes()).hexdigest()})
        self.save()
        self.event("environment_checked", task_id=task_id, passed=report["passed"], artifact=str(artifact))
        if not report["passed"]:
            raise HarnessError(f"环境前置检查未通过；未派发本次 worker、未消耗本次 worker 尝试；"
                               f"已发生的指挥调用仍计入派发预算。检查日志：{artifact}")
