---
name: harness-operator
description: Operate the Personal Harness CLI from another project on the owner's behalf - start a run, watch it, handle its stops, resume or abandon it. Use when the owner wants a goal in the current repository done through Harness, or asks about a Harness run. 中文触发词：用 harness 完成、交给 harness 做、harness 跑一下、启动 harness run、查看 run 状态、恢复 run、继续 run、放弃 run、harness 停了。
---

You are the outer session: the owner opened you in a target project and wants Harness to do the work. You call the `harness` CLI, observe the run and relay the owner's decisions. Harness itself starts the commander, workers and reviewers.

## Where this skill applies

- Only the outer session uses this skill. The roles Harness launches (commander, worker, reviewer) never use it and never call `run` or `resume`. Do not use it inside a Harness task worktree (anything under `.harness/worktrees/`).
- It does not replace the Harness documents: `docs/operations.md` covers the entry point and observation, and `docs/recovery.md` is authoritative for what to do after a stop. To find them, run `harness doctor`: `harness.package` is the package directory, and the documents are in `docs/` under its parent directory.

## Entry point

- The command is `harness` (an editable install). If it is not on `PATH`, do not install anything yourself; give the owner `uv tool install --editable <harness-source-dir>` and wait.
- Global options go before the subcommand: `harness [--config PATH] [--repo PATH] SUBCOMMAND ...`; after the subcommand they are rejected. `--repo` defaults to the current directory. Use absolute paths.
- `--config` selects only the operator configuration (`roles`, `runtime`, `memory`, `shared`), which is the same for every project. It is usually omitted; the default is `harness.toml` in the Harness source directory. A `resume` normally uses the same file as the `run`; with a different path it must still pass the recovery comparison of the configuration and the shared assets (`docs/recovery.md`, "通用规则").
- The project configuration is always `<target Git root>/.harness/project.toml`. It holds `verification`, `permissions` and optional `runtime` overrides. No option moves it and Harness does not look in other directories. `run`, `resume` and `permission-diagnostics` refuse with exit 2 (`invalid_input/cli_arguments`, `data.option` = `--repo`) while it is missing; `abandon` does not read it.
- Between `run` and `resume`, change either file only in the ways `docs/recovery.md` ("通用规则") allows, and only with the owner's approval; other changes are refused.

## First use in a project

1. Run `harness doctor` (it calls no model). Check that `harness.package` and `harness.revision` are the Harness you expect and that `checks.<cli>.logged_in` is true. Its exit code reflects only CLI login.
2. Read its `project` field: `{path, exists, sha256, error}` (null when `--repo` is not in a Git repository).
3. If `project.exists` is false: draft the complete file, starting from `examples/project.toml` in the Harness source directory, and show the owner the full content and the absolute path (`project.path`). The verification commands and worker command grants in it are the execution capability given to workers, so write it only after the owner confirms. If the owner already gave you the exact content, write it without asking again. Create the `.harness/` directory at the Git root first when it does not exist. Each command is a table `{cwd, argv}`: `cwd` is the directory, relative to the repository root, it runs in (`"."` for the root; no absolute path, `..` or symbolic link), and `argv` runs without a shell. When the project builds in a subdirectory (where its `pubspec.yaml`, `package.json` or `pyproject.toml` is), write that directory as `cwd` instead of a `cd` shell script. The same argv in two directories is two grants. A grant does not make a read-only Bash prefix unavailable; it adds no Bash permission.
4. If `project.error` is set, show it to the owner and fix the file with their agreement before any `run`.
5. Never `git add` or commit `.harness/`. Harness adds its ignore rule on the first `run`; until then the directory is only untracked.
6. Check the target repository: it has commits, tracked files and the index have no uncommitted changes, the verification commands and their dependencies are available in a worktree created from HEAD (untracked and ignored files are not copied), every verification command's cwd exists in HEAD, and every setup command's cwd exists in HEAD or is created by an earlier setup command (a wrong directory there cannot be fixed by resume; it needs a new run), and generated files are ignored.

## Starting a run

- Before `run`, settle with the owner which areas may change (directory level), the needed tests and fixtures, and where generated files go. Submit that as the boundary with `--scope PATTERN`, repeatable: a concrete path, a literal subtree `dir/**`, or `**`. Quote patterns so the shell does not expand them.
- Give every `run` and `resume` a traceable `--operation-id ID`.
- Example: `harness run "GOAL" --scope 'src/**' --scope 'tests/**' --operation-id ID`. Add `--pause-after-plan` when the owner wants to approve the plan first.
- stderr echoes `[harness] config=…`, `[harness] project=…` and `[harness] verification=…`; confirm they are the files and commands you expect. The run ID appears on stderr as `[harness-json] {"event":"run_created",…}` and in `harness status`.

## Long runs and limited sandboxes

- A run can take a long time. Start `run`, and any `resume` that continues execution, as a background task of your own tool, then observe with `harness status RUN` and `harness watch RUN` (JSONL on each change; it exits when the run blocks, pauses, awaits approval, is interrupted or ends).
- `&` and `nohup` do not keep a run alive; do not rely on them.
- If your tool has no reliable background tasks, or its sandbox does not allow network access, writes under `.git`, or starting `claude`/`codex` child processes, do not run `run` or a continuing `resume` yourself. Give the owner the complete command to run in an ordinary terminal. You still observe, prepare input files (operator decisions, environment updates, request rejections) and propose the next command.
- `status` and `watch` are read-only and load no configuration. A `running` ledger does not prove that a process is alive.
- `dashboard` reads one state directory for its whole life and names it, with its checkout, on stderr before the URL; the page header and tab title show the same. Every form of `status` names them on the first line of its stderr and leaves stdout unchanged, so keep that stderr when you need to know which checkout a result came from. Start `dashboard` with `--repo` set to the checkout that holds the run, and when you give the owner its URL, say which checkout it shows. If another dashboard may already hold the default port, use `--port 0`; a port in use is an error, not a redirect.

## Reading the outcome

Route by exit code and `stop.kind`/`stop.reason`; never parse message text. Find the stop's row in `docs/recovery.md` ("阻塞状态与操作者出口").

When the call you just ran fails, read its own stop first: the stderr line that starts with `[harness-json] ` and has `"event":"stop"` (its operation record, when there is one, holds the same value; a call refused before its record is created, for example a duplicate `--operation-id`, has none). `harness status RUN` shows the ledger's stop (`stop` with `kind`, `reason`, `exits`, `data`, `source`), which can be a different, earlier one: a call refused before it changes the run (rows marked "（操作）") leaves the ledger as it was. For example, while the run awaits an operator answer, `resume --operator-decision FILE` with an invalid file exits 2 and `status RUN` still shows `operator_decision/answer_required`. When no run was created (for example `run` refused for a missing project file), there is no ledger stop to read.

- 0: read `status` in the stdout JSON: `completed`, `paused` or awaiting plan approval. 0 does not mean completed. On `completed`, give the owner `result.commit` (the commit that passed the whole-run review in `result.verification_mode`), plus `branch` and `worktree`. The owner integrates that commit. Harness never merges. The branch may have moved since completion, which leaves the saved result unchanged. If `git cat-file -t <commit>` fails in the target repository, tell the owner the object is gone. If `handoff.json` disagrees with `status`, use the ledger result from `status`.
- 3: the run is consistent; a documented action or an external condition unblocks it. Take the applicable command from `stop.exits`, but ask the owner first when it needs new authority or a requirement choice.
- 2: this call's input was not accepted (for example a missing project file, bad options, an `--operation-id` already used or an invalid input file). Fix the input and retry.
- 1: evidence or the workspace disagrees with the ledger, Harness itself failed, or the stop is unclassified. Stop and report to the owner; do not guess a repair.
- 130: interrupted. A plain `resume RUN` reports which dispatches need confirmed exits; when every exit already has evidence it continues execution directly, so it is not a diagnostic command: start it under the background and terminal handoff rules in "Long runs and limited sandboxes".

## Quota and provider stops

- `provider_quota/*`, `provider_failure/overloaded` and `provider_failure/authentication` exit 3: wait for the quota; for `provider_failure/authentication` (a login or permission failure), check the login with `harness doctor`, which only checks and repairs nothing, and have the owner fix the authentication or permission. Then plain `resume RUN` as `stop.exits` says.
- `provider_failure/transport` exits 3: the Claude CLI reported `api_error=connection_failed` (in the CLI's own definition: it could not reach the API host, or the connection was cut). For a worker, `stop.data.attempt_charged` is false: the call did not count as a work attempt, though it counted as a dispatch. You may run the plain `resume RUN` that `stop.exits` lists without asking the owner, at most 3 times in a row for the same run:
  - Act only on the stop returned by a call you ran yourself in this session (its `[harness-json]` line with `"event":"stop"`, or its operation record), never on the stop `status RUN` shows.
  - Give each such `resume` a new, unique `--operation-id` and start it under "Long runs and limited sandboxes". Under the terminal handoff, give the owner the command as usual; that resume is the owner's, not one of yours.
  - Count the transport stops of that run you resumed this way; the first one you resume counts as 1. Only another stop of the same run returned by a call you ran, or a decision by the owner, resets the count. `busy/run_lock` does not reset it and is never a reason to resume again: another process may still hold the run while the ledger shows the old transport stop; handle it as the `busy/run_lock` item below says.
  - The count exists only in this session; a new session starts at zero.
  - At the fourth transport stop in a row, do not resume: report to the owner that the network or proxy needs checking.
- Claude roles' usage limits are waited out inside the Harness process by default (`runtime.quota_wait_max_seconds`). Do not sleep and loop `resume` yourself. On `provider_quota/usage_limit`, read `stop.data.wait_reason` first.
- `busy/run_lock` from `resume` or `abandon` may mean another process is waiting for quota: check `quota_wait` in `status RUN`, which is a record, not proof that the process is alive. To cancel the wait, interrupt that process.
- Never judge quota by matching error text.

## You may

- Start `run` with the agreed `--scope` and an `--operation-id`.
- Observe with `status`, `status RUN`, `status RUN --full`, `watch RUN` and `dashboard`, and read the target's `.harness/` ledger, events, dispatch artifacts and operation records.
- Use the exits `docs/recovery.md` lists for the current state, within what the owner has already authorized: `resume RUN --approve-plan`, a plain `resume RUN` after exact command grants were added, `--environment-update FILE`, `--operator-decision FILE` carrying the owner's answer.
- A plain `resume RUN` after `provider_failure/transport`, within the limit and rules in "Quota and provider stops".

## Ask the owner first

- New authority: command grants (exact `{cwd, argv}` commands in `permissions.worker_commands` of the project file), scope extension, higher budgets, timeouts, turns or context limits.
- Requirement choices: answers to `pending_decisions`, accepting a risk, abandoning a run (`abandon RUN` keeps the branch, worktree and evidence).
- A held proposal (`scope_extension/*`): both `resume RUN --scope ...` and `resume RUN --reject-request FILE` need the owner. Suggested entries can be wider than the gap (`data.widened`); the full gap is `scope.held` in `status RUN`.
- Creating or changing `.harness/project.toml`, and the scope before a run starts.

## Never

- Loop `resume` until it passes. The same snapshot gives the same stop; repeats only spend dispatches. The only exception is the bounded resume after `provider_failure/transport` in "Quota and provider stops".
- Raise budgets, timeouts, turns or context limits on your own to clear a stop.
- Change models, providers or effort.
- Edit `state.json`, `events.jsonl`, dispatch artifacts or the memory store.
- Merge or cherry-pick the run branch; the owner does the final integration.
- Answer an operator question on the owner's behalf.
