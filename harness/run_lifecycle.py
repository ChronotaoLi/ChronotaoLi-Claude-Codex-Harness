"""Own run creation, loading, recovery preflight and abandonment."""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

from .versions import RUN_SCHEMA_VERSION, missing_ledger, parse_current_ledger
from .commands import validate_commands
from .config import normalized, require_run_commands
from .contracts import validate_required_commands
from . import git, quota_wait, scope as boundary, stops
from .operations import warn
from .input_projection import validate_dispatch_indexes as validate_input_dispatch_indexes
from .path_evidence import validate_dispatch_indexes
from .review_progress import entries
from .services import RuntimeService
from .continuation import (packet as continuation_packet, prepare_continuation_rejection,
                           publish_continuation_rejection, read_execution_guidance, read_request_rejection,
                           record_execution_guidance)
from .shared import asset_digest, validate_mcp
from .state_files import StateFiles
from .util import HarnessError, fold_name, uid, validate_command_directories


class RunLifecycle(RuntimeService):
    def start(self, goal, plan=None, *, scope=(), pause_after_plan=False, step=False):
        runtime = self.context
        if not goal.strip():
            raise HarnessError("目标不能为空", stop=stops.make("invalid_input", "cli_arguments", data={"option": "goal"}))
        scope = boundary.declared(scope)
        untracked = git.check_starting_checkout(self.context.repo)
        require_run_commands(self.context.config)
        git.reject_submodules(self.context.repo)
        with stops.classify("invalid_input", "input_schema", data={"option": "--plan"}):
            supplied_tasks = self.services.planning.validate_tasks(plan) if plan is not None else []
        missing = boundary.gaps({t["id"]: t["allowed_paths"] for t in supplied_tasks}, scope) if scope else {}
        if missing:
            # Before any run directory, ledger or worktree exists.
            entries, _ = boundary.suggestions(missing)
            raise HarnessError("供给的计划越出声明的范围边界（当前规则无法证明包含）：" + json.dumps(missing, ensure_ascii=False)
                               + "；修正计划或调整 --scope 后重新 run；未创建 run",
                               stop=stops.make("invalid_input", "plan_outside_scope",
                                               data={"option": "--plan", "scope": scope, "gaps": missing,
                                                     "suggested_scope": entries}))
        validate_mcp(self.context.config)
        if untracked:
            print("[harness] 主检出存在未跟踪文件；保留原处，不进入隔离 worktree："
                  + git.describe_paths(untracked), file=sys.stderr, flush=True)
        ident = uid("run")
        worktree = self.context.state / "worktrees" / ident
        operation = self.context.operation
        self.context.run = {"schema_version": RUN_SCHEMA_VERSION, "workspace_inputs": None, "id": ident, "repo": str(self.context.repo), "goal": goal, "status": "planning", "commander": self.context.commander,
                    "branch": "harness/" + ident, "worktree": str(worktree), "tasks": [], "accepted": {}, "attempts": {},
                    "dispatches": [], "expected_head": git.head(self.context.repo), "feedback": None,
                    "config_path": str(self.context.config.path), "config_snapshot": deepcopy(self.context.config.data),
                    "asset_digest": asset_digest(self.context.config), "base_commit": git.head(self.context.repo),
                    "original_tasks": deepcopy(supplied_tasks),
                    "review_history": {}, "final_history": [], "plan_history": [],
                    "worker_receipts": {}, "review_receipts": {}, "commander_receipts": {},
                    "scope": scope,
                    "environment_updates": [], "environment_checks": [],
                    "decision_requests": [], "operator_decisions": [], "task_checkpoints": [], "checkpoint_batches": [],
                    "continuation_history": [], "execution_guidance": [],
                    "worktree_binding": {},
                    "agent_dispatches": 0,
                    "plan_approval_required": pause_after_plan, "plan_approved": not pause_after_plan,
                    "created_by_operation": getattr(operation, "id", None) if operation is not None else None}
        # The first save makes the run directory.
        self.context.save()
        self.announce("run_created")

        def start_locked():
            try:
                git.git(self.context.repo, "worktree", "add", "-b", self.context.run["branch"], str(worktree), "HEAD",
                        effect=git.CHECKOUT)
                self.context.run["worktree_binding"] = git.capture_worktree_binding(worktree, self.context.run["branch"])
                self.context.save()
            except BaseException as error:
                self.services.workflow.stop(error)
                raise
            self.context.memory.repo = self.context.worktree
            self.context.memory.binding = self.context.run["worktree_binding"]
            return self.services.scheduler.execute(plan, step=step)
        return self.context.run_lock(stops.make("busy", "run_lock"), start_locked)

    def announce(self, event):
        """Observation after a successful save: event, machine-readable stderr line, operation link.

        Ordinary observation errors only emit warnings and do not change the
        lifecycle, save order or exit status. Process interrupts still follow
        the existing recovery limits; the first save precedes worktree binding.
        """
        runtime = self.context
        operation = self.context.operation
        run_id = self.context.run["id"]
        operation_id = getattr(operation, "id", None) if operation is not None else None
        notice = json.dumps({"event": event, "run_id": run_id, "operation_id": operation_id},
                            ensure_ascii=True, separators=(",", ":"))
        steps = [("event", lambda: self.context.event(event, operation_id=operation_id)),
                 ("notify", lambda: print("[harness-json] " + notice, file=sys.stderr, flush=True))]
        if operation is not None:
            steps.append(("link", lambda: operation.link(run_id)))
        for name, step in steps:
            try:
                step()
            except Exception as error:
                warn(f"{event} 的观察步骤 {name} 失败，不影响运行：{error}")

    def load_run(self, ident):
        """Load a run; a failure is classified by its own nature (spec 2.2), never as one blanket type."""
        if not ident.startswith("run_") or not ident[4:].isalnum():
            raise HarnessError("无效 run ID", stop=stops.make("invalid_input", "cli_arguments", data={"option": "run_id"}))
        path = self.context.state / "runs" / ident / "state.json"
        try:
            # Version and identity are checked on a local value: binding the store
            # reads id and store_revision and locks the directory that id names.
            outcome = StateFiles(self.context.state).read(("runs", ident, "state.json"), limit=None)
            if outcome.state == "absent":
                raise missing_ledger(path)
            raw = outcome.evidence({"artifact": str(path)})
            with stops.evidence_reads(source={"artifact": str(path)}):
                run = parse_current_ledger(raw)
                if run.get("id") != ident:
                    raise HarnessError("运行台账字段缺失或类型无效；保留原文件并核对证据")
            self.context.run = run
            if isinstance(run.get("repo"), str) and run["repo"] != str(self.context.repo):
                raise HarnessError("Run 属于另一个项目",
                                   stop=stops.make("invalid_input", "cli_arguments", data={"option": "repo"}))
            self.load_checks()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            value = stops.load_failure(error)
            if isinstance(error, HarnessError):
                error.stop = value
                raise
            raise HarnessError(str(error) or type(error).__name__, stop=value) from error

    def load_checks(self):
        """The read-only checks load_run applies after reading the ledger; settling reruns them."""
        with stops.evidence_reads():
            self._load_checks()

    def _load_checks(self):
        runtime = self.context
        required = {"id": str, "repo": str, "worktree": str, "status": str, "commander": str,
                    "goal": str, "branch": str, "expected_head": str, "base_commit": str,
                    "config_snapshot": dict, "tasks": list, "original_tasks": list,
                    "accepted": dict, "attempts": dict, "dispatches": list,
                    "review_history": dict, "review_receipts": dict,
                    "worker_receipts": dict, "commander_receipts": dict, "worktree_binding": dict,
                    "scope": list,
                    "final_history": list, "plan_history": list,
                    "agent_dispatches": int, "plan_approval_required": bool, "plan_approved": bool,
                    "environment_updates": list, "environment_checks": list,
                    "decision_requests": list, "operator_decisions": list, "task_checkpoints": list, "checkpoint_batches": list,
                    "continuation_history": list, "execution_guidance": list, "store_revision": int}
        if (any(type(self.context.run.get(k)) is not t for k, t in required.items())
                or any(not isinstance(d, dict) for d in self.context.run["dispatches"])):
            raise HarnessError("运行台账字段缺失或类型无效；保留原文件并核对证据")
        self.validate_stop()
        self.validate_quota_wait()
        try:
            if boundary.operator_entries(self.context.run["scope"]) != self.context.run["scope"]:
                raise HarnessError("duplicate entry")
        except HarnessError as error:
            raise HarnessError(f"运行台账的范围边界无效（{error}）；保留原文件并核对证据") from error
        if self.context.run['config_snapshot'] != normalized(self.context.run['config_snapshot']):
            raise HarnessError('运行配置快照字段缺失；当前协议不补写旧版本默认值')
        if ("workspace_inputs" not in self.context.run
                or self.context.run["workspace_inputs"] is not None and not isinstance(self.context.run["workspace_inputs"], dict)):
            raise HarnessError("运行输入绑定字段缺失或无效；保留现场")
        validate_command_directories(self.context.run["dispatches"], self.context.directory)
        self.validate_recovery_links()
        self.validate_command_shapes()
        self.services.worker_receipts.validate_links()
        self.services.budgets.validate_attempt_accounting()
        self.services.environment.validate_environment_links()
        self.services.decisions.validate_decision_links()
        self.services.progress.validate_checkpoint_links()
        continuation_packet(runtime)

    def settle_stop(self, error, *, ident=None):
        """Settle a stop before it is saved or leaves (spec 2.2): rerun the load checks.

        A failure makes the load failure the final value, recording the stop it
        replaces in ``superseded``; the final value is written back to the error.
        Interrupts are not settled. Returns the final value (``stops.of``).
        """
        if isinstance(error, (KeyboardInterrupt, SystemExit)) or getattr(error, "_stop_settled", False):
            return stops.of(error)
        original = stops.of(error)
        final, _ = self._settled(original, ident)
        if final is not None:
            try:
                error.stop = final
            except (AttributeError, TypeError):
                pass
        try:
            error._stop_settled = True
        except (AttributeError, TypeError):
            pass
        return stops.of(error)

    def settle_publication(self, kind, reason, *, ident=None):
        """Settle a stop published without an exception (the plan awaiting approval), the same way.

        Called before that stop is saved (``ident`` None: the in-memory ledger that already
        holds it) or reported again by resume (``ident``: the ledger on disk). A failure is
        raised, already settled, as the load failure's own type with ``superseded`` naming
        the publication; the caller's boundary then stops the run with it.
        """
        final, failure = self._settled({"kind": kind, "reason": reason}, ident)
        if final is not None:
            error = HarnessError(f"{failure}（未发布停止 {kind}/{reason}）", stop=final)
            error._stop_settled = True
            raise error from failure

    def _settled(self, original, ident):
        """Rerun the load checks: (None, None) when they pass, else (final value replacing ``original``, failure)."""
        try:
            if ident is None:
                self.load_checks()      # the in-memory ledger the stop save is about to commit
            else:
                self.load_run(ident)    # what the next resume or abandon will load from disk
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as failure:
            final = stops.load_failure(failure)
            final["data"].pop("detail", None)
            final["data"]["superseded"] = {"kind": original["kind"], "reason": original["reason"]}
            return final, failure
        return None, None

    def validate_stop(self):
        """``stop`` exists exactly while the status is a stop, matches it, and is registered."""
        stops.validate_ledger(self.context.run)

    def validate_quota_wait(self):
        """A quota wait record exists only while running and has exactly its shape (harness.quota_wait)."""
        quota_wait.validate_ledger(self.context.run)

    # ---- resume()'s read-only preconditions; a quota wait reruns them in its process on waking ----
    def verify_assets_and_plan(self):
        """The shared assets the run was created with, the environment artifacts and the published plans."""
        if self.context.run.get("asset_digest") != asset_digest(self.context.config):
            raise HarnessError("共享规则、skills 或 MCP 已改变；请恢复原版本后继续", stop=stops.make("config_mismatch", "shared_assets"))
        self.services.environment.environment_context()
        # The published plan is the authority.
        self.services.planning.verify_plan_authority()

    def verify_binding_and_indexes(self):
        """The saved worktree Git binding exists; the dispatches' path and input indexes are intact."""
        if not self.context.run["worktree_binding"]:
            raise HarnessError("缺少已保存的 worktree Git 绑定，无法安全恢复；"
                               f"使用 abandon {self.context.run['id']} 保留证据后重新运行；"
                               "若工作树或分支已创建，请核对后手工清理", stop=stops.make("workspace_mismatch", "binding_missing"))
        validate_dispatch_indexes(self.context.directory, self.context.run["dispatches"])
        validate_input_dispatch_indexes(self.context.directory, self.context.run["dispatches"])

    def verify_head_and_inputs(self):
        """The worktree HEAD is the checkpoint (through the binding), prepared inputs and receipt links are intact."""
        if git.head(self.context.worktree, binding=self.context.run["worktree_binding"]) != self.context.run["expected_head"]:
            raise HarnessError("worktree HEAD 与检查点不同；请先核对提交，不自动重放", stop=stops.make("workspace_mismatch", "head_changed", data={"expected_head": self.context.run["expected_head"]}))
        from .workspace_inputs import validate_inputs
        validate_inputs(self.context.run, self.context.directory)
        self.services.worker_receipts.validate_links()

    def wake_checks(self):
        """After a quota wait, before the scheduling loop: resume()'s read-only preconditions, in its order.

        The ledger on disk is still the one this process just committed (``check_current``),
        and the load checks run on it as ``load_run`` would; a load failure is the settled
        stop itself. Then the three functions resume() calls. Nothing is written: the steps
        of resume() that write (exit confirmation, landing a returned ask, terminal capture
        digests, recovery inputs) and the configuration comparison (this process never
        reloads its configuration) are not part of it.
        """
        self.context.store.check_current()
        try:
            self.load_checks()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as failure:
            error = HarnessError(str(failure) or type(failure).__name__, stop=stops.load_failure(failure))
            error._stop_settled = True   # the load failure is already the final value
            raise error from failure
        self.verify_assets_and_plan()
        self.verify_binding_and_indexes()
        self.verify_head_and_inputs()

    @stops.reads_evidence
    def validate_command_shapes(self):
        """The saved commands that decide grants and dispatch obey the command shape (rule 21.1).

        ``tasks`` and ``original_tasks`` include a supplied plan not yet registered as the current plan.
        """
        snapshot = self.context.run["config_snapshot"]
        for section, key in (("verification", "commands"), ("verification", "setup_commands"),
                             ("permissions", "worker_commands")):
            validate_commands(snapshot[section][key], f"config_snapshot.{section}.{key}")
        for field in ("tasks", "original_tasks"):
            for index, task in enumerate(self.context.run[field]):
                if not isinstance(task, dict):
                    raise HarnessError(f"运行台账 {field}[{index}] 不是对象；保留原文件并核对证据")
                try:
                    validate_required_commands(task)
                except HarnessError as error:
                    raise HarnessError(f"运行台账 {field}[{index}] 的命令无效：{error}；保留原文件并核对证据") from error

    def validate_recovery_links(self):
        """Reject inconsistent recovery links before any resumed side effects.

        This checks ledger structure, not artifact integrity or candidate validity;
        the existing phase-specific gates still validate those when consumed.
        """
        runtime = self.context
        def fail(field):
            raise HarnessError(f"运行台账恢复状态无效：{field}；保留原文件并核对证据")

        def mapping(value, field):
            if not isinstance(value, dict):
                fail(field)
            return value

        for field in ("pending_task", "pending_acceptance", "pending_post_accept", "pending_revision",
                      "pending_directive", "worker_recovery"):
            if field in self.context.run:
                mapping(self.context.run[field], field)
        if "pending_revision" in self.context.run and (
                any(field in self.context.run for field in ("pending_task", "pending_acceptance",
                                                            "pending_post_accept", "pending_directive"))
                or "commander:decide:run" in self.context.run["commander_receipts"]):
            fail("pending_revision 不能与任务阶段、待派发指令或指挥决定回执同时存在")

        def plan_reference(value, field, revision):
            value = mapping(value, field)
            digest = value.get("artifact_digest")
            if (set(value) != {"revision", "artifact", "artifact_digest"} or value.get("revision") != revision
                    or value.get("artifact") != str(self.context.directory / "plans" / f"revision-{revision}.json")
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)):
                fail(field + " 必须只含修订号、计划文件路径与 SHA256")

        history = self.context.run["plan_history"]
        for index, row in enumerate(history):
            plan_reference(row, f"plan_history[{index}]", index)
        if bool(history) != bool(self.context.run["tasks"]):
            fail("plan_history 与 tasks 必须同时为空或同时非空")
        if "pending_revision" in self.context.run:
            plan_reference(self.context.run["pending_revision"], "pending_revision", len(history))

        task_ids, folded_ids = set(), set()
        for index, task in enumerate(self.context.run["tasks"]):
            field = f"tasks[{index}]"
            ident = mapping(task, field).get("id")
            # validate_plan's rule: IDs equal under fold_name name one evidence directory.
            if not isinstance(ident, str) or not ident or fold_name(ident) in folded_ids:
                fail(field + ".id 缺失、类型无效或重复（不区分大小写等折叠后比较）")
            task_ids.add(ident)
            folded_ids.add(fold_name(ident))

        # Review histories are read from disk only here and afterwards changed only by the
        # runtime, so every later reader sees objects that hold a review unless their state says not.
        for tid, records in self.context.run["review_history"].items():
            if not isinstance(records, list):
                fail(f"review_history.{tid} 必须是列表")
            entries(records)
        entries(self.context.run["final_history"], whole_run=True)
        from .finalization import validate_final_records
        validate_final_records(self.context.run["final_history"])

        def task_reference(ident, field):
            if not isinstance(ident, str) or ident not in task_ids:
                fail(field + " 必须引用现有任务的字符串 ID")

        for field in ("pending_task", "pending_acceptance"):
            if field in self.context.run:
                task_reference(self.context.run[field].get("task_id"), field + ".task_id")
        if "pending_post_accept" in self.context.run:
            field = "pending_post_accept.payload"
            payload = mapping(self.context.run["pending_post_accept"].get("payload"), field)
            task_reference(payload.get("task_id"), field + ".task_id")
        if self.context.run.get("current_task") is not None:
            task_reference(self.context.run["current_task"], "current_task")
        directive = self.context.run.get("pending_directive")
        if directive is not None:
            selected = mapping(directive.get("directive"), "pending_directive.directive").get("selected_task")
            if selected != self.context.run.get("current_task"):
                fail("pending_directive 的任务必须等于 current_task")
            if "revision" in directive and (type(directive["revision"]) is not int
                                            or directive["revision"] != len(self.context.run["plan_history"]) - 1):
                fail("pending_directive.revision 必须指向最后一个计划版本")

        dispatches = {}
        for entry in self.context.run["dispatches"]:
            if (entry.get('role') in ('worker', 'commander', 'reviewer') or 'invoked' in entry) and type(entry.get("invoked")) is not bool:
                fail("dispatches.invoked 必须是布尔值")
            ident = entry.get("id")
            if isinstance(ident, str):
                if ident in dispatches:
                    fail("dispatches 的 ID 重复")
                dispatches[ident] = entry

        def reference(ident, key, field):
            if not isinstance(ident, str) or ident not in dispatches:
                fail(field + " 引用了不存在的 dispatch")
            entry = dispatches[ident]
            if key != f"{entry.get('role')}:{entry.get('kind')}:{entry.get('task_id')}":
                fail(field + " 与 dispatch 的角色、类型或任务不符")

        def source(value, key, field):
            value = mapping(value, field)
            reference(value.get("dispatch_id"), key, field)

        for field, role in (("worker_receipts", "worker"), ("review_receipts", "reviewer"), ("commander_receipts", "commander")):
            for key, receipt in self.context.run[field].items():
                if not key.startswith(role + ":"):
                    fail(field + "." + key)
                source(receipt, key, field + "." + key)
        # Redo links (dispatch.redo_basis): rechecked here so the derivation holds at load.
        model_roles = ("commander", "worker", "reviewer")
        dispatch_list = self.context.run["dispatches"]
        retried = set()
        for index, entry in enumerate(dispatch_list):
            key = f"{entry.get('role')}:{entry.get('kind')}:{entry.get('task_id')}"
            if entry.get("status") == "invalid":
                output = mapping(entry.get("invalid_output"), "dispatches.invalid_output")
                artifact, digest = output.get("artifact"), output.get("artifact_digest")
                if (not isinstance(artifact, str) or Path(artifact).parent != self.context.directory / "dispatches" / str(entry.get("id"))
                        or not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
                    fail("dispatches.invalid_output 必须指向本派发目录内的原件并带 SHA256")
            if "retry_of" not in entry:
                continue
            previous = next((item for item in reversed(dispatch_list[:index])
                             if item.get("role") in model_roles and item.get("invoked") is True), None)
            if (previous is None or previous.get("id") != entry["retry_of"]
                    or f"{previous.get('role')}:{previous.get('kind')}:{previous.get('task_id')}" != key
                    or previous.get("status") != "invalid" or "retry_of" in previous
                    or entry.get("role") not in ("commander", "reviewer")
                    or entry.get("role") == "reviewer" and previous.get("snapshot_digest") != entry.get("snapshot_digest")):
                fail("dispatches.retry_of 必须指向它之前最后一次模型调用，且那是同一调用、未重做过的无效输出")
            if entry.get("invoked") is True:
                if entry["retry_of"] in retried:
                    fail("dispatches.retry_of 重复")
                retried.add(entry["retry_of"])
        for tid, recovery in self.context.run.get("worker_recovery", {}).items():
            source(recovery, f"worker:implement:{tid}", "worker_recovery." + tid)
        # A rejected proposal (spec-6 5.3): shape only; the rejection file is read where it is used.
        receipt = self.context.run["commander_receipts"].get("commander:decide:run") or {}
        for entry in dispatch_list:
            if "proposal_rejection" not in entry:
                continue
            value = mapping(entry["proposal_rejection"], "dispatches.proposal_rejection")
            digest = value.get("artifact_digest")
            if (set(value) != {"artifact", "artifact_digest"} or not isinstance(digest, str) or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)
                    or value.get("artifact") != str(self.context.directory / "proposal-rejections" / f"{digest}.json")
                    or (entry.get("role"), entry.get("kind"), entry.get("task_id")) != ("commander", "decide", "run")
                    or entry.get("invoked") is not True or entry.get("status") != "completed"
                    or receipt.get("dispatch_id") == entry.get("id")):
                fail("dispatches.proposal_rejection 必须只在已调用、已判定的指挥决定上，引用拒绝文件，且该派发没有回执")

    def abandon(self, ident, *, confirm_exited=()):
        """Mark an unfinished run abandoned; the worktree is removed only by cleanup-run."""
        self.load_run(ident)

        def abandon_locked():
            # Reload under the lock so a concurrent completion cannot be lost.
            self.load_run(ident)
            try:
                if self.context.run["status"] == "completed":
                    raise HarnessError("已完成的 run 不能标记为放弃",
                                       stop=stops.make("invalid_input", "run_state", data={"status": "completed"}))
                self.context.confirm_exited(confirm_exited)
                self.context.ensure_exited()
                if self.context.run["status"] != "abandoned":
                    self.context.run["status"] = "abandoned"
                    self.context.run.pop("stop", None)
                    quota_wait.clear(self.context.run)   # a killed process's leftover record
                    self.context.save()
                    self.context.checkpoint()
                    self.context.event("run_abandoned")
                return self.context.run
            except BaseException as error:
                # The operation boundary: a refusal after a successful load is settled once.
                self.settle_stop(error, ident=ident)
                raise
        # A busy lock points to a wait the ledger read outside the lock records.
        return self.context.run_lock(quota_wait.busy_stop(self.context.run), abandon_locked)

    def resume(self, ident, *, step=False, approve_plan=False, reverify=False, confirm_exited=(),
               scope=(), environment_update=None, operator_decision=None,
               execution_guidance=None, reject_request=None):
        runtime = self.context
        self.load_run(ident)

        def resume_locked():
            self.load_run(ident)
            try:
                def state(status):
                    return stops.make("invalid_input", "run_state", data={"status": status})

                def incompatible(*options):
                    return stops.make("invalid_input", "incompatible_options", data={"options": list(options)})
                if self.context.run["status"] == "abandoned":
                    raise HarnessError("该 run 已放弃，不能继续派发；分支与运行证据保留，工作树保留到 cleanup-run 移除为止",
                                       stop=state("abandoned"))
                self.context.commander = self.context.run["commander"]
                if self.context.run["status"] == "completed":
                    if (confirm_exited or scope or environment_update is not None or operator_decision is not None
                            or execution_guidance is not None or reject_request is not None):
                        raise HarnessError("恢复修改选项仅用于未完成的 run（含 --scope、--reject-request 与 --execution-guidance）",
                                           stop=state("completed"))
                    return self.context.run
                if reject_request is not None and (operator_decision is not None or environment_update is not None
                        or execution_guidance is not None or reverify or scope):
                    raise HarnessError("--reject-request 不能与决定、环境更新、执行指导、重验或扩大范围合用",
                                       stop=incompatible("--reject-request", "--operator-decision", "--environment-update",
                                                         "--execution-guidance", "--reverify", "--scope"))
                if execution_guidance is not None and (operator_decision is not None or environment_update is not None
                        or reverify or scope):
                    raise HarnessError("--execution-guidance 不能与决定、环境更新、重验或扩大范围合用",
                                       stop=incompatible("--execution-guidance", "--operator-decision", "--environment-update",
                                                         "--reverify", "--scope"))
                if operator_decision is not None and environment_update is not None:
                    raise HarnessError("--operator-decision 不能与环境更新合用",
                                       stop=incompatible("--operator-decision", "--environment-update"))
                if environment_update is not None and (reverify or scope):
                    raise HarnessError("--environment-update 不能与 --reverify 或 --scope 合用",
                                       stop=incompatible("--environment-update", "--reverify", "--scope"))
                declared = boundary.declared(scope)
                if approve_plan and not self.context.run["plan_history"]:
                    # Only a published first plan can be approved; nothing is written.
                    raise HarnessError("首版计划尚未发布，--approve-plan 无可批准的计划；先普通 resume 或扩大范围，发布后再批准",
                                       stop=stops.make("invalid_input", "plan_unpublished",
                                                       data={"option": "--approve-plan", "status": self.context.run["status"]}))
                # Checked before any write, including an exit confirmation: a returned
                # decision is never discarded by re-verification.
                if reverify and self.services.planning.decide_returned():
                    raise HarnessError("--reverify 不能用于已返回、尚未处理的指挥决定；先普通 resume",
                                       stop=state(self.context.run["status"]))
                parsed_decisions = None
                if operator_decision is not None:
                    # Read and parse the whole batch once, before any write; every later
                    # check, the prewrite and the registration use these same values.
                    from .decisions import read_decision
                    paths = [operator_decision] if isinstance(operator_decision, (str, Path)) else list(operator_decision)
                    with stops.classify("invalid_input", "input_schema", data={"option": "--operator-decision"}):
                        parsed_decisions = [read_decision(path) for path in paths]
                    if reverify and any(value["decision"] == "stop" for value in parsed_decisions):
                        # A stop answer ends this resume before re-verification could happen.
                        raise HarnessError("--reverify 不能与 stop 答复合用",
                                           stop=incompatible("--operator-decision(stop)", "--reverify"))
                # The other input files are read here too, once, before any write: a file that
                # cannot be read or parsed is refused without rewriting the run.
                guidance = read_execution_guidance(execution_guidance) if execution_guidance is not None else None
                rejection = read_request_rejection(reject_request) if reject_request is not None else None
                self.context.reverify_requested = reverify
                with stops.classify("config_mismatch", "immutable_config"):
                    changes = self.context.config.resume_changes(self.context.run["config_snapshot"])
                # Checked before any write of this resume, the audited exit confirmation included.
                self.verify_assets_and_plan()
                self.context.confirm_exited(confirm_exited)
                self.context.ensure_exited()
                self.verify_binding_and_indexes()
                # A returned ask lands on the common preconditions alone, before the
                # HEAD gate below; the run then stops at the question.
                try:
                    self.services.planning.land_returned_ask()
                except BaseException as error:
                    self.services.workflow.stop(error)
                    raise
                self.verify_head_and_inputs()
                with stops.classify("invalid_input", "input_schema", data={"option": "--environment-update"}):
                    update = (self.services.environment.prepare_environment_update(environment_update)
                              if environment_update is not None else None)
                decision_values, decisions = (self.services.decisions.prepare_operator_decisions(
                                                  operator_decision, values=parsed_decisions)
                                              if operator_decision is not None else ([], []))
                publish_rejection = None
                if rejection is not None:
                    if rejection["request_id"].startswith("proposal_"):
                        prepared = self.services.planning.prepare_proposal_rejection(rejection)
                        publish_rejection = lambda: self.services.planning.record_proposal_rejection(prepared)
                    else:
                        prepared = prepare_continuation_rejection(runtime, rejection)
                        publish_rejection = lambda: publish_continuation_rejection(runtime, prepared)
                added = [pattern for pattern in declared if pattern not in self.context.run["scope"]]
                # After every input is prepared: a rejected input never follows this possible digest save.
                self.services.receipts.validate_pending_terminal_captures()
                # One commit: the answers, the widened boundary and the adopted relaxation (spec-6 3.1).
                self.services.decisions.commit_resume_inputs(
                    decisions, scope_added=added, relaxed_config=self.context.config.data if changes else None)
                if changes:
                    self.context.event("resume_config_changed", changes=changes)
                if added:
                    print("[harness] 范围边界已扩大：" + json.dumps(added, ensure_ascii=False)
                          + "；当前与之后的提案都可复用这些条目", file=sys.stderr, flush=True)
                if operator_decision is not None:
                    stopped = next((value for value in decision_values if value["decision"] == "stop"), None)
                    if stopped is not None:
                        request = next(item for item in self.context.run["decision_requests"]
                                       if item["id"] == stopped["request_id"])
                        error = HarnessError("操作者决定停止；决定与原证据已保存，未派发模型",
                                             stop=self.services.workflow.operator_stopped(request))
                        self.services.workflow.stop(error)
                        raise error
                self.context.memory.repo = self.context.worktree
                self.context.memory.binding = self.context.run["worktree_binding"]
                self.services.environment.record_environment_update(update)
                if execution_guidance is not None:
                    record_execution_guidance(runtime, execution_guidance, value=guidance)
                if publish_rejection is not None:
                    publish_rejection()
                self.services.planning.report_command_requirements()
                if self.context.run["tasks"] and self.context.run["plan_approval_required"] and not self.context.run["plan_approved"]:
                    if not approve_plan and self.context.run["status"] == "awaiting_approval":
                        # Reported again, unchanged: the plan still waits for an explicit approval.
                        # Settled first, as any reported stop: its exits must not meet a load failure.
                        self.settle_publication("plan_approval", "initial_plan", ident=ident)
                        return self.context.run
                    if approve_plan:
                        self.context.run["plan_approved"] = True
                        self.context.event("plan_approved")
                        self.context.save()
                # Announce after resume() gates pass; earlier explicit audit writes
                # survive rejection. Stops inside execute() still emit run_stopped.
                self.announce("run_resumed")
                return self.services.scheduler.execute(step=step)
            except BaseException as error:
                # The operation boundary: a refusal after a successful load is settled once.
                self.settle_stop(error, ident=ident)
                raise
        # A busy lock points to a wait the ledger read outside the lock records.
        return self.context.run_lock(quota_wait.busy_stop(self.context.run), resume_locked)
