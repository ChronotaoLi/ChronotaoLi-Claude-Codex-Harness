"""Exit-table rows (spec-4 14.3): each row test makes the stop, runs the exit its projection lists, checks the next state.

Every exit runs through ``walk_exit()`` (a saved stop) or ``walk_notice_exit()`` (an
operation refusal reported on stderr): the real CLI parser, operation record and exit
code, with only the model backend and the configuration replaced. Removing an exit
from the registry, or changing it to one that does not reach the next state, fails
the row's test even though the mapping below still names it. Rows without a command
exit are bound with ``no_command_exit()`` or ``refused_by_cli()``, rows that are not a
stop with ``continues_without_stop()``. The exit-table guard (G8, test_stop_guards)
checks the mapping against ``docs/recovery.md`` and the registry, and that each mapped
test calls one of these helpers with its row.
"""
import copy
import json
import shlex
import sys
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from harness import stops
from harness.budgets import work_attempts
from harness.process_runtime import ProcessOutcome
from harness.runtime import Runtime
from harness.util import HarnessError, read_json, write_json
from test_decide import DecideCase, Script, ask, block_first, blocked_by_task, implement, task
from test_harness import (ROOT, RepoCase, cli_notice, continues_without_stop, no_command_exit, refused_by_cli, scratch,
                          run_cli, walk_exit, walk_notice_exit, write_operator_config, write_project_config)
from test_runtime_changes import ScriptedBackend, revised, trigger_kind


def plan():
    return read_json(ROOT / "examples" / "plan.json")


Row = namedtuple("Row", "stop boundary test")
RUN, OPERATION = "run", "operation"

# Row identity (the first column of docs/recovery.md's exit table, in its order) -> its stop,
# the boundary the row's fixture reaches (a stop saved in the run, or an operation's own stop
# reported on stderr and in the operation record without a run write; the document marks
# those "（操作）"), and the test that walks it. ``stop`` is None for rows that are not a stop.
ROW_COVERAGE = {
    "plan-file": Row(("invalid_input", "input_schema"), OPERATION, "test_stop_exits.PlanRowTests.test_plan_file"),
    "plan-approval": Row(("plan_approval", "initial_plan"), RUN,
                         "test_cli_controls.CLIControlsTests.test_plan_gate_requires_explicit_cli_approval"),
    "plan-unpublished": Row(("invalid_input", "plan_unpublished"), OPERATION,
                            "test_scope_boundary.InitialPlanHoldTests."
                            "test_model_first_plan_outside_scope_is_held_then_lands_and_pauses"),
    "plan-outside-scope": Row(("invalid_input", "plan_outside_scope"), OPERATION,
                              "test_scope_boundary.StartBoundaryTests.test_supplied_plan_outside_scope_creates_nothing"),
    "scope-pattern": Row(("invalid_input", "scope_pattern"), OPERATION,
                         "test_scope_boundary.StartBoundaryTests.test_scope_pattern_forms_rejected_before_anything"),
    "plan-approval-after-capacity": Row(("budget", "context_capacity"), RUN,
                                        "test_stop_exits.PlanRowTests.test_plan_approval_after_capacity"),
    "commands-all-ready": Row(("missing_commands", "all_ready_tasks"), RUN,
                              "test_command_requirements.CommandRequirementsTests."
                              "test_blocked_resumes_spend_no_budgets_and_append_grant_recovers"),
    "commands-selected": Row(("missing_commands", "selected_task"), RUN,
                             "test_stop_exits.PlanRowTests.test_commands_selected"),
    "continuation-commands": Row(("continuation", "commands_required"), RUN,
                                 "test_continuation_rejection.RejectionLifecycleTests."
                                 "test_rejection_fresh_worker_new_request_requires_new_exact_grant_and_all_gates"),
    "continuation-timeout": Row(("continuation", "timeout_required"), RUN,
                                "test_redesign_integration.RedesignIntegrationTests."
                                "test_budget_request_requires_explicit_increase"),
    "environment-blocked": Row(("environment", "worker_blocked"), RUN,
                               "test_stop_exits.ExitRowTests.test_environment_blocked"),
    "environment-checks": Row(("environment", "checks_failed"), RUN,
                              "test_environment_updates.EnvironmentUpdateTests."
                              "test_failed_check_is_durable_idempotent_and_spends_no_model_budget"),
    "operator-answer": Row(("operator_decision", "answer_required"), RUN,
                           "test_decide.AskTests.test_answered_question_reaches_the_next_decide_including_its_redo"),
    "operator-answer-reverify": Row(("operator_decision", "answer_required"), RUN,
                                    "test_stop_exits.AnswerRowTests.test_operator_answer_reverify"),
    "ask-head-changed": Row(("workspace_mismatch", "head_changed"), RUN,
                            "test_stop_exits.AskHeadChangedRowTests.test_ask_head_changed"),
    "decision-conflict": Row(("invalid_input", "conflicting_retry"), OPERATION,
                             "test_stop_exits.AnswerRowTests.test_decision_conflict"),
    "operator-stopped": Row(("operator_decision", "operator_stopped"), RUN,
                            "test_decide.AskTests.test_stop_answer_blocks_until_it_is_revised_to_continue"),
    "scope-revision": Row(("scope_extension", "revision"), RUN,
                          "test_scope_boundary.RevisionHoldTests."
                          "test_hold_keeps_receipt_replays_without_model_and_scope_lands_same_original"),
    "scope-initial-plan": Row(("scope_extension", "initial_plan"), RUN,
                              "test_scope_boundary.InitialPlanHoldTests."
                              "test_model_first_plan_outside_scope_is_held_then_lands_and_pauses"),
    "scope-supplied-plan": Row(("scope_extension", "supplied_plan"), RUN,
                               "test_scope_boundary.ClassificationTests.test_supplied_plan_recovery_rechecks_the_boundary"),
    "task-attempts": Row(("budget", "task_attempts"), RUN,
                         "test_invalid_output_redo.WorkerInvalidOutputTests.test_always_invalid_worker_stops_at_the_attempt_limit"),
    "dispatch-limit": Row(("budget", "agent_dispatches"), RUN, "test_stop_exits.ExitRowTests.test_dispatch_limit"),
    "context-dispatch": Row(("budget", "context_capacity"), RUN,
                            "test_context_projection_lifecycle.ContextProjectionLifecycleTests."
                            "test_decide_overflow_keeps_budgets_and_the_derived_decision_after_more_context"),
    "agent-timeout": Row(("budget", "agent_timeout"), RUN, "test_worker_return_recovery.WorkerReturnRecoveryTests."
                                                            "test_worker_timeout_with_empty_output_is_retried_by_plain_resume"),
    "agent-turns": Row(("budget", "agent_turns"), RUN, "test_role_turn_limits.RoleTurnLimitRecoveryTests."
                                                        "test_exhaustion_then_role_increase_keeps_counts_evidence_and_fresh_dispatch"),
    "final-timeout": Row(("verification", "final_timeout"), RUN,
                         "test_final_stage_recovery.FinalStageRecoveryTests."
                         "test_timeout_resumes_same_snapshot_with_new_bounded_attempt_and_preserves_failure"),
    "dispatch-exit": Row(("process_unconfirmed", "dispatch_exit"), RUN,
                         "test_invalid_output_redo.RedoBoundaryTests.test_unconfirmed_exit_blocks_judgment"),
    "command-exit": Row(("process_unconfirmed", "command_exit"), OPERATION, "test_stop_exits.ExitRowTests.test_command_exit"),
    "provider-quota": Row(("provider_quota", "usage_limit"), RUN, "test_provider_failures.ImmediateJudgmentTests."
                                                                   "test_quota_failure_is_judged_exempted_and_typed_at_once"),
    "provider-rate-limit": Row(("provider_quota", "rate_limit"), RUN,
                               "test_provider_failures.ImmediateJudgmentTests.test_rate_limit_row"),
    "provider-overloaded": Row(("provider_failure", "overloaded"), RUN,
                               "test_provider_failures.ImmediateJudgmentTests.test_overloaded_row"),
    "provider-authentication": Row(("provider_failure", "authentication"), RUN,
                                   "test_provider_failures.ImmediateJudgmentTests.test_authentication_row"),
    "provider-transport": Row(("provider_failure", "transport"), RUN,
                              "test_provider_failures.ImmediateJudgmentTests.test_transport_row"),
    "provider-failed": Row(("provider_failure", "invocation_failed"), RUN,
                           "test_worker_return_recovery.WorkerReturnRecoveryTests.test_explicit_provider_error_allows_counted_retry"),
    "provider-unknown": Row(("provider_failure", "unknown"), RUN,
                            "test_worker_receipts.WorkerReceiptTests.test_provider_failure_without_result_allows_fresh_counted_retry"),
    "provider-start": Row(("provider_failure", "local_start"), RUN,
                          "test_error_repairs.ErrorRepairsTests."
                          "test_missing_cli_blocks_run_instead_of_classifying_it_as_interrupted"),
    "unsealed-return": Row(("evidence", "unsealed_return"), RUN,
                           "test_worker_receipts.WorkerReceiptTests.test_unsealed_return_never_rebinds_or_retries"),
    "unsealed-unknown": Row(("evidence", "unsealed_unknown"), RUN, "test_stop_exits.EvidenceRowTests.test_unsealed_unknown"),
    "invalid-worker-output": Row(None, RUN, "test_invalid_output_redo.WorkerInvalidOutputTests."
                                            "test_invalid_worker_output_spends_one_attempt_and_continues"),
    "invalid-blocker-environment": Row(("environment", "worker_blocked"), RUN,
                                       "test_invalid_output_redo.WorkerInvalidOutputTests."
                                       "test_invalid_environment_and_operator_blockers_keep_their_route_without_exemption"),
    "invalid-blocker-operator": Row(("operator_decision", "answer_required"), RUN,
                                    "test_invalid_output_redo.WorkerInvalidOutputTests."
                                    "test_invalid_environment_and_operator_blockers_keep_their_route_without_exemption"),
    "invalid-blocker-task": Row(None, RUN, "test_stop_exits.NonStopRowTests.test_invalid_blocker_task"),
    "scope-violation": Row(("scope_violation", "candidate_outside_scope"), RUN,
                           "test_worker_receipts.WorkerReceiptTests."
                           "test_scope_rejection_retains_recovery_and_requires_cleanup_before_fresh_worker"),
    "protected-path": Row(("scope_violation", "protected_path"), RUN, "test_stop_exits.WorkspaceRowTests.test_protected_path"),
    "redo-exhausted": Row(("invalid_output", "redo_exhausted"), RUN, "test_stop_exits.ExitRowTests.test_redo_exhausted"),
    "invalid-original-changed": Row(("evidence", "runtime_integrity"), RUN,
                                    "test_stop_exits.ExitRowTests.test_invalid_original_changed"),
    "head-changed": Row(("workspace_mismatch", "head_changed"), OPERATION, "test_stop_exits.WorkspaceRowTests.test_head_changed"),
    "load-integrity": Row(("evidence", "load_integrity"), OPERATION, "test_stop_exits.EvidenceRowTests.test_load_integrity"),
    "index-damaged": Row(("evidence", "runtime_integrity"), RUN, "test_stop_exits.EvidenceRowTests.test_index_damaged"),
    "candidate-changed": Row(("workspace_mismatch", "candidate_changed"), RUN,
                             "test_description_replan.DescriptionReplanTests."
                             "test_retained_candidate_change_before_dispatch_stops_and_restores"),
    "directive-mismatch": Row(("workspace_mismatch", "directive_mismatch"), RUN,
                              "test_stop_exits.WorkspaceRowTests.test_directive_mismatch"),
    "continuation-candidate": Row(("workspace_mismatch", "candidate_changed"), RUN,
                                  "test_redesign_integration.RedesignIntegrationTests."
                                  "test_changed_candidate_does_not_reuse_continuation"),
    "context-final": Row(("budget", "context_capacity"), RUN,
                         "test_context_capacity.FinalContextCapacityTests."
                         "test_required_overflow_saves_plan_before_workers_and_resumes_with_larger_budget"),
    "proposal-capacity": Row(("budget", "proposal_capacity"), RUN,
                             "test_scope_boundary.ClassificationTests.test_capacity_flip_on_replay_keeps_the_judged_original"),
    "plan-file-damaged": Row(("evidence", "runtime_integrity"), OPERATION,
                             "test_stop_exits.EvidenceRowTests.test_plan_file_damaged"),
    "revision-head": Row(("workspace_mismatch", "head_changed"), RUN, "test_stop_exits.WorkspaceRowTests.test_revision_head"),
    "shared-assets": Row(("config_mismatch", "shared_assets"), OPERATION, "test_stop_exits.OperationRowTests.test_shared_assets"),
    "wake-shared-assets": Row(("config_mismatch", "shared_assets"), RUN,
                              "test_quota_wait.GateTests.test_shared_skill_changed_during_wait_stops_before_any_model_call"),
    "config-changed": Row(("config_mismatch", "immutable_config"), OPERATION,
                          "test_harness.RuntimeTests.test_resume_rejects_changed_configuration"),
    "binding-missing": Row(("workspace_mismatch", "binding_missing"), OPERATION,
                           "test_startup_recovery.StartupRecoveryTests."
                           "test_first_save_preserves_independent_plan_but_cannot_resume_without_binding"),
    "unsupported": Row(("unsupported_ledger", "ledger_version"), OPERATION,
                       "test_stop_exits.OperationRowTests.test_unsupported"),
    "busy": Row(("busy", "run_lock"), OPERATION,
                "test_quota_wait.ObservationTests.test_busy_while_waiting_points_to_the_record"),
    "setup-failed": Row(("verification", "setup_failed"), RUN,
                        "test_fresh_verification.FreshVerificationTests."
                        "test_failed_setup_resume_uses_new_checkout_same_attempt_and_no_extra_worker"),
    "checks-modified": Row(("verification", "checks_modified_files"), RUN,
                           "test_stopped_recovery.StoppedRecoveryTests."
                           "test_generated_final_artifact_can_resume_after_explicit_local_exclusion"),
    "dirty-final": Row(("workspace_mismatch", "dirty_final_workspace"), RUN, "test_stop_exits.WorkspaceRowTests.test_dirty_final"),
    "binding-changed": Row(("workspace_mismatch", "binding_changed"), RUN,
                           "test_worktree_binding.EngineWorktreeBindingTests."
                           "test_redirect_before_acceptance_cannot_commit_to_main"),
    "process-incomplete": Row(("evidence", "process_incomplete"), RUN, "test_stop_exits.ProcessRowTests.test_process_incomplete"),
    "runtime-io": Row(("runtime_failure", "io"), RUN,
                      "test_engine_repairs.EngineRepairsTests.test_worktree_creation_failure_persists_blocked_checkpoint"),
    "runtime-database": Row(("runtime_failure", "database"), OPERATION,
                            "test_stop_exits.OperationRowTests.test_runtime_database"),
    "runtime-invariant": Row(("runtime_failure", "invariant"), OPERATION,
                             "test_stop_exits.OperationRowTests.test_runtime_invariant"),
    "unclassified-harness-error": Row(("unclassified", "untyped_harness_error"), RUN,
                                      "test_stops.LedgerStopTests.test_untyped_error_is_unclassified_and_exits_1"),
    "unclassified-exception": Row(("unclassified", "unexpected_exception"), RUN,
                                  "test_engine_repairs.EngineRepairsTests."
                                  "test_runtime_failure_is_blocked_and_operator_interrupt_is_interrupted"),
    "interrupted": Row(("interrupted", "signal"), RUN, "test_stop_exits.ExitRowTests.test_interrupted"),
    "input-arguments": Row(("invalid_input", "cli_arguments"), OPERATION, "test_stop_exits.InputRowTests.test_input_arguments"),
    "input-file": Row(("invalid_input", "input_schema"), OPERATION, "test_stop_exits.InputRowTests.test_input_file"),
    "options": Row(("invalid_input", "incompatible_options"), OPERATION,
                   "test_stop_exits.RefusalRowTests.test_incompatible_options"),
    "stale-request": Row(("invalid_input", "stale_request"), OPERATION, "test_stop_exits.InputRowTests.test_stale_request"),
    "run-state": Row(("invalid_input", "run_state"), OPERATION, "test_stop_exits.RefusalRowTests.test_run_state"),
    "config-file": Row(("invalid_input", "config_file"), OPERATION, "test_stop_exits.InputRowTests.test_config_file"),
}


def saved(directory):
    return read_json(Path(directory) / "state.json")


class PlanRowTests(RepoCase):
    def test_plan_file(self):
        bad = plan()
        bad["tasks"][0]["allowed_paths"] = ["../outside"]
        path = scratch(self) / "plan.json"
        write_json(path, bad)
        backend = ScriptedBackend()
        notice = refused_by_cli(self, backend, ["run", "Implement greet", "--plan", str(path)],
                                ("invalid_input", "input_schema"), 2)
        self.assertEqual(notice["data"]["option"], "--plan")
        self.assertFalse((self.repo / ".harness" / "runs").exists())  # no run was created
        self.assertEqual(backend.calls, [])
        write_json(path, plan())  # the documented way on: correct the file, run again
        code, errors = run_cli(self, backend, ["run", "Implement greet", "--plan", str(path)])
        self.assertEqual(code, 0, errors)
        run_id = json.loads(next(line for line in errors.splitlines() if '"run_created"' in line).split(" ", 1)[1])["run_id"]
        self.assertEqual(saved(self.repo / ".harness" / "runs" / run_id)["status"], "completed")

    def test_plan_approval_after_capacity(self):
        big = plan()
        big["tasks"][0]["description"] = "Detailed acceptance context " * 4000
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", big, pause_after_plan=True)
        state = saved(engine.directory)
        self.assertEqual((state["stop"]["data"]["scope"], state["plan_approved"]), ("final_review", False))
        # Unchanged budget: the same stop again (3), nothing approved, no model call.
        again = walk_exit(self, engine.directory, ("budget", "context_capacity"), backend=backend,
                          expect_code=3, expect_status="blocked")
        self.assertEqual((again["stop"]["kind"], again["stop"]["data"]["scope"]), ("budget", "final_review"))
        self.config.data["runtime"]["context_chars"] = state["context_capacity"]["projected_chars"] + 20000
        after = walk_exit(self, engine.directory, ("budget", "context_capacity"), backend=backend,
                          expect_status="awaiting_approval")
        self.assertEqual((after["stop"]["kind"], after["plan_approved"]), ("plan_approval", False))
        self.assertEqual(backend.calls, [])

    def test_commands_selected(self):
        command = {"cwd": ".", "argv": [sys.executable, "-c", "print('prerequisite tool')"]}

        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="task", blockers=["Need a prerequisite"])
            elif trigger_kind(kwargs, packet) == "task" and packet["trigger"]["feedback"]:
                original = copy.deepcopy(packet["original_tasks"][0])
                revised(result, packet, "Add a prerequisite with its own command",
                        new_tasks=[{**original, "id": "prep", "required_commands": [command]}],
                        updates=[{"id": "greet", "description": original["description"], "add_depends_on": ["prep"], "add_allowed_paths": [], "add_required_commands": []}],
                        selected="prep", instruction="Prepare first")
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        before = saved(engine.directory)
        self.assertEqual(before["stop"]["source"], {"task_id": "prep"})
        self.assertEqual(before["stop"]["data"]["missing"], {"prep": [command]})
        directive = before["pending_directive"]
        # Without the grant the listed exit stops at the same place and spends nothing.
        again = walk_exit(self, engine.directory, ("missing_commands", "selected_task"), backend=backend,
                          expect_code=3, expect_status="blocked")
        self.assertEqual((again["stop"]["reason"], again["attempts"], again["agent_dispatches"], again["pending_directive"]),
                         ("selected_task", before["attempts"], before["agent_dispatches"], directive))
        self.config.data["permissions"]["worker_commands"].append(command)
        after = walk_exit(self, engine.directory, ("missing_commands", "selected_task"), backend=backend,
                          expect_status="completed")
        # The selection survived the stop: the next worker ran the saved directive, no new decide.
        self.assertEqual(sum(c["kind"] == "decide" for c in backend.calls), 2)  # the revision, then greet's retry
        prep = next(c for c in backend.calls if c["role"] == "worker" and c["task_id"] == "prep")
        self.assertEqual(prep["packet"]["instruction"], directive["directive"]["instruction"])
        self.assertEqual(set(after["accepted"]), {"prep", "greet"})


class ExitRowTests(RepoCase):
    def blocked(self, *, change=None, before=None, **runtime):
        self.config.data["runtime"].update(runtime)
        backend = ScriptedBackend(change)
        backend.before_result = before
        engine = self.engine(backend)
        with self.assertRaises(BaseException):
            engine.start("Implement greet", plan())
        return backend, engine

    def test_dispatch_limit(self):
        from test_dispatch_events import two_tasks
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet twice", two_tasks())
        state = saved(engine.directory)
        data = state["stop"]["data"]
        self.assertEqual((data["used"], data["limit"], state["agent_dispatches"]), (1, 1, 1))
        # Raise the limit by exactly the reported lower bound; with every call passing first time it suffices.
        self.config.data["runtime"]["max_agent_dispatches"] = data["used"] + data["minimum_remaining_calls"]
        after = walk_exit(self, engine.directory, ("budget", "agent_dispatches"), backend=backend,
                          expect_status="completed")
        self.assertEqual(after["agent_dispatches"], data["used"] + data["minimum_remaining_calls"])

    def blocked_by_environment(self):
        def blocked_once(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="environment", blockers=["Docker stopped"])
        backend, engine = self.blocked(change=blocked_once)
        state = saved(engine.directory)
        self.assertIs(state["stop"]["data"]["invalid_output"], False)
        worker = next(d for d in state["dispatches"] if d["role"] == "worker")
        self.assertEqual(state["stop"]["source"]["dispatch_id"], worker["id"])
        self.assertIn("attempt_exemption", worker)  # a valid environment blocker spends no attempt
        return backend, engine, worker

    def test_environment_blocked(self):
        for exit_number in (0, 1):
            with self.subTest(exit=["plain resume", "--environment-update"][exit_number]):
                if exit_number:
                    self.setUp()
                backend, engine, worker = self.blocked_by_environment()
                update = scratch(self) / "update.json"
                write_json(update, {"schema_version": 1, "task_id": "greet", "blocker_dispatch_id": worker["id"],
                                    "summary": "Docker restarted"})
                after = walk_exit(self, engine.directory, ("environment", "worker_blocked"), backend=backend,
                                  choose=exit_number, fill={"FILE": update} if exit_number else None,
                                  expect_status="completed")
                workers = [d for d in after["dispatches"] if d["role"] == "worker"]
                # Same task, a fresh worker; the exempted blocker spent no work attempt.
                self.assertEqual((len(workers), work_attempts(after)), (2, {"greet": 1}))
                self.assertEqual(len(after["environment_updates"]), exit_number)

    def test_command_exit(self):
        import uuid

        def fail_leaving_a_child(backend, kwargs, packet):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                folder = Path(kwargs["job"]) / "commands" / uuid.uuid4().hex
                folder.mkdir()
                write_json(folder / "process.json", {"id": f"{Path(kwargs['job']).name}:command:{folder.name}",
                                                     "command_id": "fixture", "pgid": None,
                                                     "process_state": "starting", "exit_confirmed": False})
                raise HarnessError("worker CLI failed; no result")
        backend, engine = self.blocked(before=fail_leaving_a_child)
        worker = next(d for d in engine.run["dispatches"] if d["role"] == "worker")
        state_file = engine.directory / "state.json"
        before = state_file.read_bytes()
        # Resume finds the broker child of the failed worker unconfirmed: refused before any write.
        notice = refused_by_cli(self, backend, ["resume", engine.run["id"]], ("process_unconfirmed", "command_exit"), 3)
        self.assertEqual(notice["source"], {"dispatch_id": worker["id"]})  # confirmed through the parent dispatch
        self.assertTrue(notice["data"]["command_id"].startswith(worker["id"] + ":command:"))
        self.assertEqual(state_file.read_bytes(), before)
        after = walk_notice_exit(self, notice, ("process_unconfirmed", "command_exit"), backend=backend,
                                 directory=engine.directory, expect_status="completed")
        parent = next(d for d in after["dispatches"] if d["id"] == worker["id"])
        self.assertEqual(list(parent["command_exit_confirmations"]), [notice["data"]["command_id"]])
        self.assertEqual(work_attempts(after), {"greet": 2})  # the failed worker counted; a fresh one followed

    def test_redo_exhausted(self):
        def invalid_twice(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and sum(c["kind"] == "review" for c in backend.calls) <= 2:
                result["summary"] = "x" * 4001
        backend, engine = self.blocked(change=invalid_twice)
        walk_exit(self, engine.directory, ("invalid_output", "redo_exhausted"), backend=backend,
                  expect_status="completed")

    def test_interrupted(self):
        def interrupt_once(backend, kwargs, packet):
            if kwargs["kind"] == "review" and sum(c["kind"] == "review" for c in backend.calls) == 1:
                raise KeyboardInterrupt
        backend, engine = self.blocked(before=interrupt_once)
        state = saved(engine.directory)
        # A simulated call leaves its exit unconfirmed: abandon is not listed until resume confirms it.
        self.assertEqual(stops.project_ledger(state)["exits"], [f"resume {state['id']}"])
        # Resume reports the exact dispatch to confirm; that exit then completes the run.
        walk_exit(self, engine.directory, ("interrupted", "signal"), backend=backend, expect_code=3,
                  expect_status="interrupted")
        code, errors = run_cli(self, backend, ["resume", state["id"]])
        notice = cli_notice(errors)
        self.assertEqual((code, notice["kind"]), (3, "process_unconfirmed"))
        self.assertEqual(run_cli(self, backend, shlex.split(notice["exits"][0]))[0], 0)
        self.assertEqual(saved(engine.directory)["status"], "completed")

    def test_invalid_original_changed(self):
        from harness.dispatch import Dispatcher

        def invalid_once(backend, kwargs, packet, result):
            if kwargs["kind"] == "review" and sum(c["kind"] == "review" for c in backend.calls) == 1:
                result["summary"] = "x" * 4001
        original_dispatch = Dispatcher.dispatch_once

        def stop_before_redo(self_, role, kind, task_id, schema, packet, task=None, **kwargs):
            if role == "reviewer" and kwargs.get("retry_of"):
                raise KeyboardInterrupt
            return original_dispatch(self_, role, kind, task_id, schema, packet, task, **kwargs)
        for side in ("restore the bytes, then resume", "abandon"):
            with self.subTest(exit=side):
                if side == "abandon":
                    self.setUp()
                backend = ScriptedBackend(invalid_once)
                engine = self.engine(backend)
                with patch.object(Dispatcher, "dispatch_once", stop_before_redo), self.assertRaises(KeyboardInterrupt):
                    engine.start("Implement greet", plan())
                invalid = next(d for d in engine.run["dispatches"] if d.get("invalid_output"))
                original = Path(invalid["invalid_output"]["artifact"])
                raw = original.read_bytes()
                original.write_text("{}")
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                state = saved(engine.directory)
                source = state["stop"]["source"]
                self.assertEqual((source["artifact"], source["artifact_digest"]),
                                 (str(original), invalid["invalid_output"]["artifact_digest"]))
                reviews = sum(c["kind"] == "review" for c in backend.calls)
                if side == "abandon":
                    # The invalid original is not load-checked: abandon is not blocked by it.
                    walk_exit(self, engine.directory, ("evidence", "runtime_integrity"), backend=backend, choose=1,
                              expect_status="abandoned")
                    self.assertEqual(original.read_text(), "{}")
                    continue
                original.write_bytes(raw)  # the documented exit: restore the bytes, then resume
                after = walk_exit(self, engine.directory, ("evidence", "runtime_integrity"), backend=backend,
                                  expect_status="completed")
                redo = [d for d in after["dispatches"] if d.get("retry_of") == invalid["id"]]
                self.assertEqual((len(redo), sum(c["kind"] == "review" for c in backend.calls)), (1, reviews + 1))


class EvidenceRowTests(RepoCase):
    def blocked_at_limit(self, *, plan_value=None):
        """A run stopped at the dispatch limit after its worker returned (a clean place to damage evidence)."""
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan_value or plan())
        self.config.data["runtime"]["max_agent_dispatches"] = 50
        return backend, engine

    def test_unsealed_unknown(self):
        conflict = {"type": "result", "subtype": "success", "is_error": False, "structured_output": {"status": "completed"}}

        def conflicting_return(backend, kwargs, packet):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                job = Path(kwargs["job"])
                (job / "process.stdout").write_text(json.dumps(conflict))
                (job / "provider-result.json").write_text(json.dumps({**conflict, "is_error": True,
                                                                       "subtype": "error_during_execution"}))
                raise HarnessError("worker CLI ended without a result")
        backend = ScriptedBackend()
        backend.before_result = conflicting_return
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"])
        state = saved(engine.directory)
        worker = next(d for d in state["dispatches"] if d["role"] == "worker")
        self.assertEqual(state["stop"]["data"]["return_reason"], "provider result conflicts with original output")
        # Confirming the exit does not unblock it; no new worker is started.
        code, errors = run_cli(self, backend, ["resume", state["id"], "--confirm-exited", worker["id"]])
        self.assertEqual((code, cli_notice(errors)["reason"]), (1, "unsealed_unknown"))
        self.assertEqual(sum(c["role"] == "worker" for c in backend.calls), 1)
        walk_exit(self, engine.directory, ("evidence", "unsealed_unknown"), backend=backend, expect_status="abandoned")

    def test_index_damaged(self):
        from test_path_evidence import PathEvidenceLifecycleTests as Fixture
        self.fixture, self.remove_legacy = Fixture.fixture.__get__(self), Fixture.remove_legacy.__get__(self)
        for side in ("restore the bytes, then resume", "abandon"):
            with self.subTest(exit=side):
                if side == "abandon":
                    self.setUp()
                plan_value, damaged = self.fixture(), {}

                def corrupt(backend, kwargs, packet, result):
                    if kwargs["role"] == "worker":
                        self.remove_legacy(kwargs["cwd"])
                    elif kwargs["kind"] == "review" and not damaged:
                        ref = next(iter(packet["path_evidence_indexes"].values()))
                        damaged.update(path=Path(ref["artifact"]), raw=Path(ref["artifact"]).read_bytes())
                        damaged["path"].write_bytes(b"{}")
                backend = ScriptedBackend(corrupt)
                engine = self.engine(backend)
                with self.assertRaises(HarnessError):
                    engine.start("Replace legacy docs", plan_value)
                if side == "abandon":
                    # The index is not load-checked: abandon works with the damage in place.
                    walk_exit(self, engine.directory, ("evidence", "runtime_integrity"), backend=backend, choose=1,
                              expect_status="abandoned")
                    continue
                damaged["path"].write_bytes(damaged["raw"])
                after = walk_exit(self, engine.directory, ("evidence", "runtime_integrity"), backend=backend,
                                  expect_status="completed")
                self.assertEqual(sum(c["kind"] == "review" for c in backend.calls), 1)  # the verdict is not redrawn
                self.assertEqual(after["attempts"], {"greet": 1})

    def test_plan_file_damaged(self):
        for side in ("restore the bytes, then resume", "abandon"):
            with self.subTest(exit=side):
                if side == "abandon":
                    self.setUp()
                backend, engine = self.blocked_at_limit()
                path = Path(engine.run["plan_history"][0]["artifact"])
                raw = path.read_bytes()
                path.unlink()
                state_file = engine.directory / "state.json"
                before = state_file.read_bytes()
                notice = refused_by_cli(self, backend, ["resume", engine.run["id"]], ("evidence", "runtime_integrity"), 1)
                self.assertEqual(state_file.read_bytes(), before)
                if side == "abandon":
                    walk_notice_exit(self, notice, ("evidence", "runtime_integrity"), backend=backend, choose=1,
                                     directory=engine.directory, expect_status="abandoned")
                    continue
                path.write_bytes(raw)
                walk_notice_exit(self, notice, ("evidence", "runtime_integrity"), backend=backend,
                                 directory=engine.directory, expect_status="completed")

    def test_load_integrity(self):
        """Three ways to meet it: the load_run boundary, a run stop settled by Workflow.stop, the operation boundary."""
        from harness.run_context import RunContext
        with self.subTest(where="load_run"):
            backend, engine = self.blocked_at_limit()
            packet = Path(next(d for d in engine.run["dispatches"] if d.get("worker_receipt"))["worker_receipt"]["packet_artifact"])
            raw = packet.read_bytes()
            packet.write_text("{}\n")
            state_file = engine.directory / "state.json"
            before = state_file.read_bytes()
            for command in ("resume", "abandon"):  # abandon is refused as well: no command exit
                refused_by_cli(self, backend, [command, engine.run["id"]], ("evidence", "load_integrity"), 1)
            self.assertEqual(state_file.read_bytes(), before)
            packet.write_bytes(raw)  # the documented way on: restore the original bytes
            self.assertEqual(run_cli(self, backend, ["resume", engine.run["id"]])[0], 0)
            self.assertEqual(saved(engine.directory)["status"], "completed")
        with self.subTest(where="run stop, settled"):
            self.setUp()
            backend, engine = self.blocked_at_limit()
            self.config.data["runtime"]["max_agent_dispatches"] = 2  # one more call: the review, then the limit
            packet = Path(next(d for d in engine.run["dispatches"] if d.get("worker_receipt"))["worker_receipt"]["packet_artifact"])
            raw = packet.read_bytes()

            def damage_during_review(backend_, kwargs, packet_, result):
                if kwargs["kind"] == "review":
                    packet.write_text("{}\n")  # a load-checked artifact changes while the run continues
            backend.change = damage_during_review
            with self.assertRaises(HarnessError):
                self.engine(backend).resume(engine.run["id"])
            state = saved(engine.directory)
            value = no_command_exit(self, stops.project_ledger(state), ("evidence", "load_integrity"))
            self.assertEqual(value["data"]["superseded"], {"kind": "budget", "reason": "agent_dispatches"})
            self.assertTrue(refused(self, backend, engine))
            packet.write_bytes(raw)
            self.config.data["runtime"]["max_agent_dispatches"] = 50
            self.assertEqual(run_cli(self, backend, ["resume", engine.run["id"]])[0], 0)
            self.assertEqual(saved(engine.directory)["status"], "completed")
        with self.subTest(where="operation boundary, settled"):
            self.setUp()
            backend, engine = self.blocked_at_limit()
            packet = Path(next(d for d in engine.run["dispatches"] if d.get("worker_receipt"))["worker_receipt"]["packet_artifact"])
            raw = packet.read_bytes()
            original = RunContext.confirm_exited

            def replace_then_confirm(context, *args, **kwargs):
                packet.write_text("{}\n")  # after resume's locked load
                return original(context, *args, **kwargs)
            state_file = engine.directory / "state.json"
            before = state_file.read_bytes()
            with patch.object(RunContext, "confirm_exited", replace_then_confirm):
                notice = refused_by_cli(self, backend, ["resume", engine.run["id"]], ("evidence", "load_integrity"), 1)
            self.assertEqual((notice["exits"], state_file.read_bytes()), ([], before))
            self.assertIn("superseded", notice["data"])
            packet.write_bytes(raw)
            self.assertEqual(run_cli(self, backend, ["resume", engine.run["id"]])[0], 0)


class WorkspaceRowTests(RepoCase):
    blocked_at_limit = EvidenceRowTests.blocked_at_limit

    @staticmethod
    def commit_in(worktree):
        from harness import git
        git.git(worktree, "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false",
                "commit", "--allow-empty", "-m", "unexpected commit")

    def test_head_changed(self):
        from harness import git
        for side in ("restore HEAD, then resume", "abandon"):
            with self.subTest(exit=side):
                if side == "abandon":
                    self.setUp()
                backend, engine = self.blocked_at_limit()
                self.commit_in(engine.worktree)
                state_file = engine.directory / "state.json"
                before = state_file.read_bytes()
                notice = refused_by_cli(self, backend, ["resume", engine.run["id"]],
                                        ("workspace_mismatch", "head_changed"), 1)
                self.assertEqual((notice["data"]["expected_head"], state_file.read_bytes()),
                                 (engine.run["expected_head"], before))
                if side == "abandon":
                    walk_notice_exit(self, notice, ("workspace_mismatch", "head_changed"), backend=backend, choose=1,
                                     directory=engine.directory, expect_status="abandoned")
                    continue
                git.git(engine.worktree, "reset", "--soft", engine.run["expected_head"])
                walk_notice_exit(self, notice, ("workspace_mismatch", "head_changed"), backend=backend,
                                 directory=engine.directory, expect_status="completed")

    def test_revision_head(self):
        from harness import git
        from test_harness import after_save

        def revise_once(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="task", blockers=["The description is ambiguous"])
            elif trigger_kind(kwargs, packet) == "task" and packet["trigger"]["feedback"]:
                revised(result, packet, "Clarify", selected="greet",
                        updates=[{"id": "greet", "description": "Implement greet(name) precisely", "add_depends_on": [], "add_allowed_paths": [], "add_required_commands": []}])
        backend = ScriptedBackend(revise_once)
        engine = self.engine(backend)
        change, fired = after_save(engine, lambda run: "pending_revision" in run, lambda: self.commit_in(engine.worktree))
        with change, self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        self.assertTrue(fired["fired"])
        state = saved(engine.directory)
        self.assertEqual((len(state["plan_history"]), "pending_revision" in state), (1, True))
        git.git(engine.worktree, "reset", "--soft", state["expected_head"])  # restore the verified HEAD
        after = walk_exit(self, engine.directory, ("workspace_mismatch", "head_changed"), backend=backend,
                          expect_status="completed")
        self.assertEqual(len(after["plan_history"]), 2)  # the revision was published
        self.assertEqual(after["tasks"][0]["description"], "Implement greet(name) precisely")

    def test_directive_mismatch(self):
        from test_description_replan import DescriptionReplanTests as Fixture
        for name in ("interrupted", "plan", "assert_candidate", "assert_no_archive"):
            setattr(self, name, getattr(Fixture, name).__get__(self))
        for side in ("restore the ledger value, then resume", "abandon"):
            with self.subTest(exit=side):
                if side == "abandon":
                    self.setUp()
                engine, backend = self.interrupted("published")
                path = engine.directory / "state.json"
                ledger = read_json(path)
                self.assertGreaterEqual(ledger["pending_directive"]["revision"], 1)
                original = ledger["pending_directive"]["directive"]["instruction"]
                ledger["pending_directive"]["directive"]["instruction"] = "an instruction the plan never published"
                write_json(path, ledger)
                with self.assertRaises(HarnessError):
                    self.engine(backend).resume(engine.run["id"])
                state = saved(engine.directory)
                self.assertEqual(state["stop"]["source"], {"task_id": "greet"})
                if side == "abandon":
                    walk_exit(self, engine.directory, ("workspace_mismatch", "directive_mismatch"), backend=backend,
                              choose=1, expect_status="abandoned")
                    continue
                state["pending_directive"]["directive"]["instruction"] = original
                write_json(path, state)
                workers = len(backend.worker_ids)
                walk_exit(self, engine.directory, ("workspace_mismatch", "directive_mismatch"), backend=backend,
                          expect_status="completed")
                worker = [c for c in backend.calls if c["role"] == "worker"][workers]
                self.assertEqual(worker["packet"]["instruction"], original)

    def test_protected_path(self):
        def write_claude_settings(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                (kwargs["cwd"] / ".claude").mkdir()
                (kwargs["cwd"] / ".claude" / "settings.json").write_text("{}\n")
        backend = ScriptedBackend(write_claude_settings)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        state = saved(engine.directory)
        self.assertEqual((state["stop"]["data"]["path"], state["stop"]["source"]), (".claude/settings.json", {"task_id": "greet"}))
        self.assertEqual(state["accepted"], {})
        # Not removed for the operator: cleaning it up is the documented step before the listed resume.
        (engine.worktree / ".claude" / "settings.json").unlink()
        (engine.worktree / ".claude").rmdir()
        after = walk_exit(self, engine.directory, ("scope_violation", "protected_path"), backend=backend,
                          expect_status="completed")
        self.assertEqual(sum(d["role"] == "worker" for d in after["dispatches"]), 2)  # a fresh worker

    def test_dirty_final(self):
        from test_runtime_changes import finding

        def reject_final(backend, kwargs, packet, result):
            if kwargs["kind"] == "final_review" and sum(c["kind"] == "final_review" for c in backend.calls) == 1:
                result.update(verdict="request_changes", findings=[finding()])
        backend = ScriptedBackend(reject_final)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        request = engine.run["decision_requests"][-1]
        tracked = engine.worktree / "hello.py"
        accepted = tracked.read_bytes()
        tracked.write_bytes(accepted + b"# unreviewed\n")
        answer = scratch(self) / "continue.json"
        write_json(answer, {"schema_version": 1, "request_id": request["id"], "task_id": "final",
                            "decision": "continue", "summary": "Proceed with the edited tree."})
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(engine.run["id"], operator_decision=answer)
        state = saved(engine.directory)
        self.assertEqual((state["stop"]["kind"], state["stop"]["reason"]), ("workspace_mismatch", "dirty_final_workspace"))
        decides = sum(c["kind"] == "decide" for c in backend.calls)
        tracked.write_bytes(accepted)  # clean the unaccepted edit
        reviews = sum(c["kind"] == "final_review" for c in backend.calls)
        after = walk_exit(self, engine.directory, ("workspace_mismatch", "dirty_final_workspace"), backend=backend,
                          expect_code=3, expect_status="blocked")
        # The derived decision runs with the answer (here the demo commander asks again); the
        # rejected snapshot is not reviewed again.
        self.assertEqual(sum(c["kind"] == "decide" for c in backend.calls), decides + 1)
        self.assertIn("operator_answer", [c for c in backend.calls if c["kind"] == "decide"][-1]["packet"])
        self.assertEqual(sum(c["kind"] == "final_review" for c in backend.calls), reviews)
        self.assertEqual((after["stop"]["kind"], after["stop"]["reason"]), ("operator_decision", "answer_required"))


class ProcessRowTests(RepoCase):
    def test_process_incomplete(self):
        """A reviewer exits normally but its outcome is not persisted: abandon (with --confirm-exited when unconfirmed)."""
        import errno
        from harness.process_runtime import failure
        for confirmed in (True, False):
            with self.subTest(exit_confirmed=confirmed):
                if not confirmed:
                    self.setUp()

                def outcome_not_persisted(backend, kwargs, packet):
                    if kwargs["kind"] == "review" and sum(c["kind"] == "review" for c in backend.calls) == 1:
                        kwargs["on_start"](2_000_000_000)
                        outcome = ProcessOutcome(started=True, startup_state="started", pgid=2_000_000_000, exit_code=0,
                                                 termination_reason="normal", exit_confirmed=confirmed,
                                                 evidence_complete=False,
                                                 errors=[failure("persist_outcome", OSError(errno.ENOSPC, "full"))])
                        kwargs["on_outcome"](outcome)
                        error = OSError(errno.ENOSPC, "No space left on device")
                        error.outcome = outcome
                        raise error
                backend = ScriptedBackend()
                backend.before_result = outcome_not_persisted
                engine = self.engine(backend)
                with patch("harness.run_context.group_alive", return_value=not confirmed), \
                        patch("harness.dispatch.group_alive", return_value=not confirmed):
                    with self.assertRaises(HarnessError):
                        engine.start("Implement greet", plan())
                    state = saved(engine.directory)
                    review = next(d for d in state["dispatches"] if d.get("kind") == "review")
                    self.assertEqual((state["stop"]["data"]["exit_unconfirmed"], state["stop"]["source"]["dispatch_id"]),
                                     (not confirmed, review["id"]))
                    exits = stops.project_ledger(state)["exits"]
                    self.assertEqual(exits, [f"abandon {state['id']}" + ("" if confirmed else f" --confirm-exited {review['id']}")])
                    if not confirmed:
                        # A plain abandon would be held by the unconfirmed exit (why it is not listed).
                        held = run_cli(self, backend, ["abandon", state["id"]])
                        self.assertEqual((held[0], cli_notice(held[1])["kind"]), (3, "process_unconfirmed"))
                    # Confirming the exit does not repair the recorded persistence failure: resume stays refused.
                    reviews = sum(c["kind"] == "review" for c in backend.calls)
                    code, errors = run_cli(self, backend, ["resume", state["id"], "--confirm-exited", review["id"]])
                    self.assertEqual((code, cli_notice(errors)["reason"]), (1, "process_incomplete"))
                    self.assertEqual(sum(c["kind"] == "review" for c in backend.calls), reviews)
                    walk_exit(self, engine.directory, ("evidence", "process_incomplete"), backend=backend,
                              expect_status="abandoned")


class OperationRowTests(RepoCase):
    blocked_at_limit = EvidenceRowTests.blocked_at_limit

    def test_shared_assets(self):
        import shutil
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        rules = Path(folder.name) / "RULES.md"
        shutil.copyfile(ROOT / "shared" / "RULES.md", rules)
        self.config.data["shared"]["rules"] = str(rules)
        backend, engine = self.blocked_at_limit()
        raw = rules.read_bytes()
        rules.write_bytes(raw + b"\nA rule added after the run started.\n")
        state_file = engine.directory / "state.json"
        before = state_file.read_bytes()
        notice = refused_by_cli(self, backend, ["resume", engine.run["id"]], ("config_mismatch", "shared_assets"), 2)
        self.assertEqual((state_file.read_bytes(), backend.calls[-1]["kind"] != "decide"), (before, True))
        rules.write_bytes(raw)  # restore the original assets
        walk_notice_exit(self, notice, ("config_mismatch", "shared_assets"), backend=backend,
                         directory=engine.directory, expect_status="completed")

    def test_busy(self):
        from harness.util import lock
        backend, engine = self.blocked_at_limit()
        state_file = engine.directory / "state.json"
        before = state_file.read_bytes()
        with lock(engine.directory / "run.lock"):
            notice = refused_by_cli(self, backend, ["resume", engine.run["id"]], ("busy", "run_lock"), 3)
            no_command_exit(self, notice, ("busy", "run_lock"))
            self.assertEqual(state_file.read_bytes(), before)
        # The other holder finished: the same command again goes on.
        code, errors = run_cli(self, backend, ["resume", engine.run["id"]])
        self.assertEqual((code, saved(engine.directory)["status"]), (0, "completed"), errors)

    def test_unsupported(self):
        backend, engine = self.blocked_at_limit()
        path = engine.directory / "state.json"
        run = read_json(path)
        run["schema_version"] = 18
        write_json(path, run)
        before = path.read_bytes()
        notice = refused_by_cli(self, backend, ["resume", engine.run["id"]], ("unsupported_ledger", "ledger_version"), 2)
        no_command_exit(self, notice, ("unsupported_ledger", "ledger_version"))
        self.assertEqual((notice["data"]["schema_version"], notice["data"]["supported"]), (18, 19))
        for argv in (["abandon", engine.run["id"]], ["status", engine.run["id"]]):
            self.assertEqual(run_cli(self, backend, argv)[0], 2, argv)
        self.assertEqual(run_cli(self, backend, ["cleanup-run", engine.run["id"]])[0], 1)
        code, errors = run_cli(self, backend, ["status"])  # the list still shows its path and version
        self.assertEqual(code, 0)
        self.assertIn(f"runs/{engine.run['id']}", errors)
        self.assertIn("18", errors)
        self.assertEqual(path.read_bytes(), before)

    def test_runtime_database(self):
        backend, engine = self.blocked_at_limit()
        database = self.repo / ".harness" / "memory.sqlite3"
        raw = database.read_bytes()
        database.write_bytes(b"not a database" * 512)
        state_file = engine.directory / "state.json"
        before = state_file.read_bytes()
        notice = refused_by_cli(self, backend, ["resume", engine.run["id"]], ("runtime_failure", "database"), 1)
        no_command_exit(self, notice, ("runtime_failure", "database"))
        self.assertEqual(state_file.read_bytes(), before)
        database.write_bytes(raw)  # the database is repaired; the same command goes on
        self.assertEqual(run_cli(self, backend, ["resume", engine.run["id"]])[0], 0)

    def test_runtime_invariant(self):
        from harness.run_store import RunStore
        backend, engine = self.blocked_at_limit()
        original = RunStore.commit
        state = {"commits": 0}

        def another_writer_after_the_first_commit(store, *args, **kwargs):
            result = original(store, *args, **kwargs)
            state["commits"] += 1
            if state["commits"] == 1:  # another writer rewrites the ledger between our commits
                ledger = read_json(store._path)
                store._path.write_text(json.dumps(ledger, indent=1) + "\n")
            return result
        with patch.object(RunStore, "commit", another_writer_after_the_first_commit):
            notice = refused_by_cli(self, backend, ["resume", engine.run["id"]], ("runtime_failure", "invariant"), 1)
        no_command_exit(self, notice, ("runtime_failure", "invariant"))
        # Nothing overwrote the other writer's ledger; it still loads.
        Runtime(self.config, self.repo, backend=backend).load_run(engine.run["id"])


class InputRowTests(RepoCase):
    def operator_question(self):
        def ask_operator(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="operator", blockers=["Which greeting?"])
        backend = ScriptedBackend(ask_operator)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        request = engine.run["decision_requests"][-1]
        answer = scratch(self) / "answer.json"
        return backend, engine, request, answer

    def test_input_arguments(self):
        backend, engine, request, answer = self.operator_question()
        notice = refused_by_cli(self, backend, ["resume", "run_not-an-id"], ("invalid_input", "cli_arguments"), 2)
        no_command_exit(self, notice, ("invalid_input", "cli_arguments"))
        self.assertEqual(run_cli(self, backend, ["status", engine.run["id"]])[0], 0)  # the corrected argument

    def test_input_file(self):
        backend, engine, request, answer = self.operator_question()
        answer.write_text('{"schema_version": 1, "request_id": "x", "request_id": "y"}')  # duplicate keys
        state_file = engine.directory / "state.json"
        before = state_file.read_bytes()
        notice = refused_by_cli(self, backend, ["resume", engine.run["id"], "--operator-decision", str(answer)],
                                ("invalid_input", "input_schema"), 2)
        no_command_exit(self, notice, ("invalid_input", "input_schema"))
        self.assertEqual((notice["data"]["option"], state_file.read_bytes()), ("--operator-decision", before))
        write_json(answer, {"schema_version": 1, "request_id": request["id"], "task_id": "greet",
                            "decision": "continue", "summary": "Use the documented greeting."})
        walk_exit(self, engine.directory, ("operator_decision", "answer_required"), backend=backend,
                  fill={"FILE": answer}, expect_status="completed")

    def test_config_file(self):
        from harness import git
        backend = ScriptedBackend()
        project = git.repository(self.repo) / ".harness" / "project.toml"
        valid = write_project_config(self.repo, self.config.data["verification"]["commands"]).read_bytes()
        project.write_bytes(b'[verification]\ncommands = [["make", "test"]]\n')  # an argv array, not a command object
        argv = ["--config", str(write_operator_config(scratch(self) / "harness.toml")),
                "run", "Implement greet", "--plan", str(ROOT / "examples" / "plan.json")]
        # The CLI reads both files itself: the case's configuration would never read the broken one.
        notice = refused_by_cli(self, backend, argv, ("invalid_input", "config_file"), 2, load=True)
        no_command_exit(self, notice, ("invalid_input", "config_file"))
        self.assertEqual((notice["data"], notice["source"]), ({"file": "project"}, {"artifact": str(project)}))
        # Refused before any write: no operation record, no run, no private state beyond the file itself.
        self.assertEqual([path.name for path in (self.repo / ".harness").iterdir()], ["project.toml"])
        self.assertEqual(backend.calls, [])
        project.write_bytes(valid)  # the file is corrected; the same command goes on
        code, errors = run_cli(self, backend, argv, load=True)
        self.assertEqual(code, 0, errors)
        (ledger,) = (self.repo / ".harness" / "runs").glob("*/state.json")
        self.assertEqual(read_json(ledger)["status"], "completed")

    def test_stale_request(self):
        backend, engine, request, answer = self.operator_question()
        write_json(answer, {"schema_version": 1, "request_id": "request_" + "0" * 64, "task_id": "greet",
                            "decision": "continue", "summary": "An answer to a request that does not exist."})
        state_file = engine.directory / "state.json"
        before = state_file.read_bytes()
        notice = refused_by_cli(self, backend, ["resume", engine.run["id"], "--operator-decision", str(answer)],
                                ("invalid_input", "stale_request"), 2)
        self.assertEqual(state_file.read_bytes(), before)
        # The listed exit reads the current requests; nothing changes.
        after = walk_notice_exit(self, notice, ("invalid_input", "stale_request"), backend=backend,
                                 directory=engine.directory, expect_status="blocked")
        self.assertEqual(state_file.read_bytes(), before)
        self.assertEqual(after["decision_requests"][-1]["id"], request["id"])


def refused(case, backend, engine):
    """Whether abandon is refused (the load checks hold it), through the real CLI."""
    return run_cli(case, backend, ["abandon", engine.run["id"]])[0] != 0


def final_failure_scenario(case):
    """A run whose whole-run verification failed once (ordinary exit 1) and whose commander asked about it.

    The first final verification command reports exit 1 ("service down"); later ones run
    for real. Returns (backend, engine, request) with the stop operator_decision/answer_required;
    ``case.final_runs["runs"]`` counts whole-run verification command executions.
    """
    from harness.util import run_process
    failures = case.final_runs = {"n": 0, "runs": 0}

    def fail_once(argv, cwd, log, **kwargs):
        if "final" in Path(log).parts:
            failures["runs"] += 1  # every whole-run verification command execution
        if "final" in Path(log).parts and failures["n"] == 0:
            failures["n"] += 1
            kwargs["on_start"](123)
            Path(log).with_suffix(".stdout").write_text("")
            Path(log).with_suffix(".stderr").write_text("service down")
            kwargs["on_outcome"](ProcessOutcome(started=True, startup_state="started", pid=123, pgid=123,
                                                exit_code=1, exit_confirmed=True, termination_reason="normal"))
            return 1
        return run_process(argv, cwd, log, **kwargs)
    backend = ScriptedBackend()
    engine = Runtime(case.config, case.repo, backend=backend)
    patcher = patch("harness.verification.run_process", side_effect=fail_once)
    patcher.start()
    case.addCleanup(patcher.stop)
    with case.assertRaises(HarnessError):
        engine.start("Implement greet", plan())
    state = saved(engine.directory)
    case.assertEqual((state["stop"]["kind"], state["stop"]["reason"]), ("operator_decision", "answer_required"))
    case.assertIs(state["stop"]["data"]["reverify_available"], True)
    return backend, engine, state["decision_requests"][-1]


def final_answer(case, request, decision="continue"):
    path = scratch(case) / f"answer-{decision}.json"
    write_json(path, {"schema_version": 1, "request_id": request["id"], "task_id": "final",
                      "decision": decision, "summary": "环境已修复，请对同一快照重验"})
    return path


class AnswerRowTests(RepoCase):
    def test_operator_answer_reverify(self):
        backend, engine, request = final_failure_scenario(self)
        decides = sum(c["kind"] == "decide" for c in backend.calls)
        # The conditional exit (reverify_available): answer and re-verify the same snapshot in one call.
        after = walk_exit(self, engine.directory, ("operator_decision", "answer_required"), backend=backend, choose=1,
                          fill={"FILE": final_answer(self, request)}, expect_status="completed")
        self.assertEqual(sum(c["kind"] == "decide" for c in backend.calls), decides)
        self.assertEqual([r["status"] for r in after["final_history"]], ["verification_failed", "approved"])
        self.assertEqual(after["final_history"][0]["snapshot_digest"], after["final_history"][1]["snapshot_digest"])
        self.assertEqual(len(after["operator_decisions"]), 1)

    def test_decision_conflict(self):
        from test_decision_lifecycle import DecisionLifecycleTests
        self.plan = DecisionLifecycleTests.plan.__get__(self)
        engine, backend, paths = DecisionLifecycleTests.batch_handoff(self)
        resumed = self.engine(backend)
        original = resumed.services.decisions._decision_artifact
        written = []

        def die_after_first(record, category):
            if written:
                raise KeyboardInterrupt()  # the prewrite dies after the first answer file
            written.append(record["request_id"])
            return original(record, category)
        with patch.object(resumed.services.decisions, "_decision_artifact", side_effect=die_after_first), \
                self.assertRaises(KeyboardInterrupt):
            resumed.resume(engine.run["id"], operator_decision=paths)
        state_file = engine.directory / "state.json"
        before = state_file.read_bytes()
        calls = len(backend.calls)
        swapped = [arg for path in reversed(paths) for arg in ("--operator-decision", str(path))]
        notice = refused_by_cli(self, backend, ["resume", engine.run["id"], *swapped],
                                ("invalid_input", "conflicting_retry"), 2)
        self.assertEqual(notice["data"]["option"], "--operator-decision")
        self.assertEqual((state_file.read_bytes(), len(backend.calls)), (before, calls))
        # The documented way on: the original files in the original order.
        ordered = [arg for path in paths for arg in ("--operator-decision", str(path))]
        code, errors = run_cli(self, backend, ["resume", engine.run["id"], *ordered])
        self.assertEqual(code, 0, errors)
        after = saved(engine.directory)
        self.assertEqual((after["status"], [r["sequence"] for r in after["operator_decisions"]]), ("completed", [1, 2]))


class AskHeadChangedRowTests(DecideCase):
    def test_ask_head_changed(self):
        from harness import git

        def decide(script, trigger, packet):
            if trigger["kind"] != "task" or not blocked_by_task(trigger["feedback"]):
                return None
            return ask() if "operator_answer" not in packet else implement("a", instruction="continue a")
        script = Script(decide, block_first("a"))
        apply = script.change

        def change(backend, kwargs, packet, result):
            apply(backend, kwargs, packet, result)
            if kwargs["role"] == "commander" and "operator_answer" not in packet:
                git.git(kwargs["cwd"], "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c",
                        "commit.gpgsign=false", "commit", "--allow-empty", "-m", "unexpected commit")
        script.change = change
        engine = self.engine(script)
        with self.assertRaises(HarnessError):
            engine.start("ask", {"tasks": [task("a")]})
        state = self.state(engine)
        request = state["decision_requests"][-1]
        self.assertEqual((state["stop"]["data"]["question_registered"], state["stop"]["source"]["request_id"]),
                         (True, request["id"]))
        answer = self.answer(state)
        calls = len(script.calls)
        # The answer exit before HEAD is restored: the HEAD gate refuses it (1), nothing registered.
        refused = walk_exit(self, engine.directory, ("workspace_mismatch", "head_changed"), backend=script, choose=2,
                            fill={"FILE": answer}, expect_code=1, expect_status="blocked")
        self.assertEqual((refused["operator_decisions"], len(script.calls)), ([], calls))
        git.git(engine.worktree, "reset", "--soft", state["expected_head"])  # restore the checkpoint HEAD
        after = walk_exit(self, engine.directory, ("workspace_mismatch", "head_changed"), backend=script, choose=2,
                          fill={"FILE": answer}, expect_status="completed")
        self.assertEqual(len(after["operator_decisions"]), 1)
        self.assertIn("operator_answer", script.of("commander", "decide")[-1]["packet"])


class NonStopRowTests(RepoCase):
    def test_invalid_blocker_task(self):
        def malformed_operator_blocker(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(c["role"] == "worker" for c in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="operator", blockers=[])  # structurally invalid
        backend = ScriptedBackend(malformed_operator_blocker)
        engine = self.engine(backend)
        result = engine.start("Implement greet", plan())
        # Row invalid-blocker-task: no stop; the commander decides with the error evidence.
        continues_without_stop(self, engine.directory)
        self.assertEqual(result["status"], "completed")
        worker = next(d for d in result["dispatches"] if d["role"] == "worker")
        self.assertEqual(worker["status"], "invalid_blocked")
        decide = next(c for c in backend.calls if c["kind"] == "decide")
        self.assertIn("output_error", decide["packet"]["trigger"]["feedback"])
        self.assertEqual(result["decision_requests"], [])


class RefusalRowTests(RepoCase):
    def blocked(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        return backend, engine

    def test_incompatible_options(self):
        backend, engine = self.blocked()
        update = scratch(self) / "update.json"
        write_json(update, {"schema_version": 1})
        before = (engine.directory / "state.json").read_bytes()
        refused_by_cli(self, backend, ["resume", engine.run["id"], "--reverify", "--environment-update", str(update)],
                       ("invalid_input", "incompatible_options"), 2)
        self.assertEqual((engine.directory / "state.json").read_bytes(), before)

    def test_run_state(self):
        backend, engine = self.blocked()
        self.assertEqual(run_cli(self, backend, ["abandon", engine.run["id"]])[0], 0)
        before = (engine.directory / "state.json").read_bytes()
        notice = refused_by_cli(self, backend, ["resume", engine.run["id"]], ("invalid_input", "run_state"), 2)
        self.assertEqual(notice["data"]["status"], "abandoned")
        # The listed exit only reads the run's state.
        walk_notice_exit(self, notice, ("invalid_input", "run_state"), backend=backend, directory=engine.directory,
                         expect_status="abandoned")
        self.assertEqual((engine.directory / "state.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
