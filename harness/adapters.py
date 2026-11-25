from __future__ import annotations

import json
import hashlib
import math
import os
import shlex
import sys
import time
from copy import deepcopy
from pathlib import Path

from . import interrupts, stops
from .command_server import create_manifest
from .provider_failures import LIMIT, claude_http_status, display_label
from .shared import mcp_configs, toml_value
from .state_files import StateFiles, StateReadError
from .util import EvidenceError, HarnessError, dump, output_tail_text, read_json, run_process, strict_json_loads, write_json
from .worker_returns import decode_terminal


class _CompletedResultReadError(StateReadError):
    """Local I/O after a CLI returned zero, before its result could be judged."""


class UndecodableOutput(HarnessError):
    """A complete provider terminal whose result is not a strictly decodable JSON object.

    ``artifact_digest`` binds the bytes judged: recording the invalid output never reads the original again.
    """

    def __init__(self, artifact, raw, message):
        self.artifact = Path(artifact)
        self.artifact_digest = hashlib.sha256(raw).hexdigest()
        super().__init__(message)


def result_kind(provider, raw):
    """Return the terminal's classification, envelope, payload and decode detail."""
    if provider != "codex":
        return decode_terminal(raw)
    try:
        value = strict_json_loads(raw)
    except (ValueError, UnicodeError, RecursionError) as error:
        return "undecodable", None, None, error
    if not isinstance(value, dict):
        return "undecodable", None, None, None
    return "object", None, value, None


_USAGE_MAX_BYTES = 16 * 1024 * 1024
_USAGE_MAX_COUNT = 2**63 - 1
_CODEX_ERROR_TAIL_BYTES = 64 * 1024
# The outer session's skill (operator/<name>/SKILL.md) is linked into the user's
# skill directories; a role Harness launches must not pick it up and drive Harness itself.
OPERATOR_SKILL = "harness-operator"


def codex_error_summary(path, *, files=None):
    """Read only error envelopes from a bounded JSONL tail, not agent/tool text."""
    path = Path(path)
    if files is None:
        files = StateFiles(path.parent)
    tail = files.read((path.name,), limit=_CODEX_ERROR_TAIL_BYTES, part="tail")
    raw = tail.bytes_or_none()
    if raw is None:
        return ""
    # A tail can start inside a UTF-8 character or a large JSON record.
    # Never parse the partial first record, even if its suffix looks valid.
    if tail.size > _CODEX_ERROR_TAIL_BYTES:
        raw = raw.partition(b"\n")[2]
    details = []
    for line in reversed(raw.split(b"\n")):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            message = event.get("message")
        elif event.get("type") == "turn.failed" and isinstance(event.get("error"), dict):
            message = event["error"].get("message")
        else:
            continue
        if not isinstance(message, str) or not message.strip():
            continue
        # ASCII JSON escaping makes terminal controls, newlines and bidi
        # characters inert. Truncate before escaping to bound expansion.
        excerpt = json.dumps(message[:240], ensure_ascii=True)
        if len(message) > 240:
            excerpt += " [truncated]"
        if excerpt not in details:
            details.append(excerpt)
        if len(details) == 3:
            break
    return "Codex reported an error: " + "; ".join(reversed(details)) if details else ""


def _usage_counts(value, fields):
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in fields
            if type(value.get(key)) is int and 0 <= value[key] <= _USAGE_MAX_COUNT}


def provider_usage(job, provider, *, files=None):
    """Read bounded diagnostics from the CLI, never from the model's response.

    Missing/invalid metrics stay unknown. ``reported_cost_usd`` is the CLI's nominal
    estimate, not an account bill or the subscription's quota; it is kept per dispatch and
    never summed. None of these observations affect execution budgets.
    """
    try:
        if files is None:
            files = StateFiles(job.parent).under(job.name)
        raw = files.read(("process.stdout",), limit=_USAGE_MAX_BYTES).bytes_or_none()
        if raw is None:
            return None
        if provider == "claude":
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("type") != "result":
                return None
            metrics = _usage_counts(result, ("num_turns",))
            cost = result.get("total_cost_usd")
            if type(cost) in (int, float) and 0 <= cost <= 1_000_000_000 and math.isfinite(cost):
                metrics["reported_cost_usd"] = cost
            tokens = _usage_counts(result.get("usage"), (
                "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        elif provider == "codex":
            turns, tokens = 0, None
            for line in raw.splitlines():
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if not isinstance(event, dict) or event.get("type") != "turn.completed":
                    continue
                turns += 1
                reported = _usage_counts(event.get("usage"), (
                    "input_tokens", "cached_input_tokens", "output_tokens"))
                # Sum only fields reported for every completed turn. A missing
                # field cannot turn into a misleading partial dispatch total.
                tokens = reported if tokens is None else {
                    key: value + reported[key] for key, value in tokens.items()
                    if key in reported and value + reported[key] <= _USAGE_MAX_COUNT}
            metrics = {"completed_turns": turns} if turns else {}
        else:
            return None
        if tokens:
            metrics["tokens"] = tokens
        return {"provider": provider, **metrics} if metrics else None
    except (ValueError, UnicodeError, RecursionError):
        return None


def claude_error_summary(value):
    """Expose bounded CLI diagnostics, never model-generated result text."""
    if (not isinstance(value, dict) or value.get("type") != "result"
            or not (value.get("is_error") or value.get("subtype", "success") != "success")):
        return ""
    details = []
    if "is_error" in value:
        flag = value["is_error"]
        details.append("is_error=" + (str(flag).lower() if type(flag) is bool else "invalid"))
    # These fields are CLI envelope diagnostics, not instructions or evidence from the
    # model's result, shown by where they come from (display_label); a value that cannot
    # show is the bare word unrecognized and stays private in process.stdout.
    for field in ("subtype", "terminal_reason", "api_error", "api_error_type"):
        if field in value:
            label = display_label(field, value[field])
            details.append(field + "=" + (json.dumps(label) if label is not None else "unrecognized"))
    status = claude_http_status(value)
    details.append("api_error_status=" + (str(status["value"]) if status["state"] == "valid" else status["state"]))
    errors = value.get("errors")
    if isinstance(errors, list):
        details.extend(json.dumps(message[:240], ensure_ascii=True)
                       for message in errors[:3] if isinstance(message, str))
    return "Claude reported an error" + (": " + "; ".join(details) if details else "")


def cli_error_summary(job, provider, *, files=None):
    if provider not in ("codex", "claude"):
        return ""
    if files is None:
        files = StateFiles(job.parent).under(job.name)
    if provider == "codex":
        return codex_error_summary(job / "process.stdout", files=files)
    try:
        raw = files.read(("process.stdout",), limit=_USAGE_MAX_BYTES).bytes_or_none()
        return claude_error_summary(json.loads(raw)) if raw is not None else ""
    except (ValueError, UnicodeError, RecursionError):
        return ""


def provider_schema(schema, strict=False):
    """Project our richer local contract onto both CLIs' common schema core."""
    core = {"type", "properties", "required", "additionalProperties", "items", "enum"}
    result = {key: deepcopy(value) for key, value in schema.items() if key in core}
    if "properties" in result:
        result["properties"] = {name: provider_schema(value, strict) for name, value in result["properties"].items()}
        if strict:
            # Codex strict outputs reject optional keys; the local contract
            # still accepts their absence in supplied and legacy artifacts.
            result["required"] = list(result["properties"])
    if "items" in result:
        result["items"] = provider_schema(result["items"], strict)
    return result


def claude_literal_worktree(cwd):
    path = cwd.resolve().as_posix().rstrip("/")
    if "\n" in path or "\r" in path:
        raise HarnessError("Claude 工作树路径不能包含换行符，无法生成精确文件权限")
    return "".join("\\" + char if char in "\\*?[]" else char for char in path)


def claude_worktree_edit_rule(cwd):
    """Grant built-in file writes only below this literal, resolved worktree."""
    # Claude uses gitignore patterns: // anchors at the filesystem root and
    # Edit(path) also governs Write. Escape the directory, not the final /**.
    return "Edit(/" + claude_literal_worktree(cwd) + "/**)"


def claude_worktree_git_deny_rules(cwd):
    """Protect Git discovery metadata as well as any replacement directory."""
    path = claude_literal_worktree(cwd)
    return ["Edit(/" + path + suffix + ")" for suffix in ("/.git", "/.git/**")]


class CLIBackend:
    def __init__(self, config, state):
        self.config, self.state = config, state

    @staticmethod
    def process_environment():
        """The environment of every provider CLI process this backend starts: model calls and quota probes alike.

        Claude Code's own in-call quota wait is never inherited: the quota wait policy is
        Harness's (``runtime.quota_wait_max_seconds``), and two layers must not stack.
        """
        env = os.environ.copy()
        env.pop("CLAUDE_CODE_RETRY_WATCHDOG", None)
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        return env

    def quota_snapshot(self, provider):
        """One model-free account quota probe (observation only). Having it is what makes a run sample."""
        from .quota import snapshot
        return snapshot(provider, self.process_environment())

    def command(self, role, model, cwd, job, schema, run_path, *, parent_deadline=None, delete_scope=None):
        if role == "worker" and model["cli"] == "codex":
            raise HarnessError("当前 Codex 适配器仅支持只读角色，不能配置为 worker")
        claude, codex = mcp_configs(self.config, self.state, role, run_path)
        if role == "worker":
            # Run dispatches take delete_path's task scope from the ledger; only a
            # ledger-less probe passes an explicit delete_scope.
            manifest = create_manifest(self.config, cwd, job.resolve(), run_path, parent_deadline=parent_deadline if parent_deadline is not None else getattr(self, "_parent_deadline", None),
                                       delete_scope=delete_scope)
            claude["mcpServers"]["harness_commands"] = {
                "type": "stdio", "command": sys.executable,
                "timeout": int(self.config.data["runtime"]["verify_timeout_seconds"] * 1000) + 10000,
                "args": [str(Path(__file__).with_name("command_entry.py").resolve()),
                         "--manifest", manifest["artifact"], "--sha256", manifest["artifact_digest"]]}
        write_json(job / "mcp.json", claude)
        wire_schema = provider_schema(schema, strict=model["cli"] == "codex")
        write_json(job / "provider-schema.json", wire_schema)
        if model["cli"] == "codex":
            return ["codex", "exec", "--model", model["model"], "--ephemeral", "--ignore-user-config",
                    "--sandbox", "read-only", "--cd", str(cwd), "--json", "--color", "never",
                    "-c", 'approval_policy="never"', "-c", f'model_reasoning_effort={json.dumps(model["effort"])}',
                    "-c", "features.multi_agent=false", "-c", "features.memories=false",
                    "-c", "features.hooks=false", "-c", "features.plugins=false",
                    "-c", "skills.config=" + toml_value([{"name": OPERATOR_SKILL, "enabled": False}]),
                    "-c", "project_doc_max_bytes=0",
                    "-c", "mcp_servers=" + toml_value(codex),
                    "--output-schema", str(job / "provider-schema.json"),
                    "--output-last-message", str(job / "response.json"), "-"]
        # Bash gets only the read-only prefixes, whatever the configuration lists:
        # configured commands are broker grants and never become Bash rules.
        allow = ["Read", "Glob", "Grep"]
        allow += ["Bash(" + shlex.join(cmd) + " *)" for cmd in self.config.readonly_commands()]
        tools = "Read,Glob,Grep,Bash"
        if role == "worker":
            allow.append(claude_worktree_edit_rule(cwd))
            tools = "Read,Edit,Write,Glob,Grep,Bash"
        # Only configured MCP servers are exposed. Their permissions are a trusted operator choice.
        allow += ["mcp__" + name for name in claude["mcpServers"]]
        permissions = {"allow": allow}
        if role == "worker":
            permissions["deny"] = claude_worktree_git_deny_rules(cwd)
        settings = {"autoMemoryEnabled": False, "disableAllHooks": True, "permissions": permissions}
        return ["claude", "--print", "--model", model["model"], "--effort", model["effort"],
                "--no-session-persistence", "--disable-slash-commands", "--session-id", job.name,
                "--setting-sources", "", "--settings", json.dumps(settings),
                "--strict-mcp-config", "--mcp-config", str(job / "mcp.json"),
                "--output-format", "json",
                "--json-schema", json.dumps(wire_schema),
                "--max-turns", str(self.config.claude_max_turns(role)),
                "--permission-mode", "dontAsk", "--disallowedTools", "Agent,Task",
                "--tools", tools]

    def invoke(self, *, role, model, cwd, job, schema, prompt, run_path, on_start, task=None, kind=None, on_outcome=None):
        parent_deadline = time.monotonic() + self.config.data["runtime"]["agent_timeout_seconds"]
        contract = job / "schema.json"
        if role == "worker":
            # The runtime published this immutable contract before charging the
            # invocation. Only use it unchanged; never write or replace it here.
            if (contract.is_symlink() or not contract.is_file()
                    or contract.read_bytes() != (dump(schema) + "\n").encode("utf-8")):
                raise EvidenceError(f"worker 契约文件缺失或与本次调用不符；保留现场：{contract}", stop=stops.make("evidence", "runtime_integrity", source={"artifact": str(contract), "dispatch_id": Path(job).name}))
        else:
            write_json(contract, schema)
        (job / "prompt.txt").write_text(prompt, encoding="utf-8")
        self._parent_deadline = parent_deadline
        try:
            argv = self.command(role, model, cwd, job, schema, run_path)
        finally:
            self._parent_deadline = None
        write_json(job / "command.json", argv)
        env = self.process_environment()
        remaining = parent_deadline - time.monotonic()
        if remaining <= 0:
            raise HarnessError("Agent budget exhausted before CLI startup", stop=stops.make("budget", "agent_timeout", data={"role": role, "config_key": "runtime.agent_timeout_seconds", "limit_seconds": self.config.data["runtime"]["agent_timeout_seconds"]}, source={"dispatch_id": Path(job).name, **({"task_id": task["id"]} if task else {})}))
        observed_outcome = None
        def record_outcome(value):
            nonlocal observed_outcome
            observed_outcome = value
            if on_outcome:
                on_outcome(value)
        try:
            code = run_process(argv, cwd, job / "process", stdin=prompt,
                               timeout=remaining, env=env, on_start=on_start,
                               execution_deadline=parent_deadline, on_outcome=record_outcome,
                               checkpoint=interrupts.deliver if interrupts.active() else None)
        except (Exception, KeyboardInterrupt, SystemExit) as error:
            outcome = getattr(error, "outcome", None)
            if (role in ("commander", "reviewer") and outcome is not None
                    and outcome.started):
                try:
                    self.preserve_terminal(job, model["cli"], outcome.to_dict())
                except (Exception, KeyboardInterrupt, SystemExit) as capture_error:
                    error.add_note(f"Terminal evidence preservation failed: {type(capture_error).__name__}: {capture_error}")
            raise
        if code:
            note = None
            if role in ("commander", "reviewer") and observed_outcome is not None:
                try:
                    self.preserve_terminal(job, model["cli"], observed_outcome.to_dict(), terminal_only=True)
                except HarnessError as capture_error:
                    # As on the exception path: evidence that cannot be read never replaces the CLI failure.
                    note = f"Terminal evidence preservation failed: {type(capture_error).__name__}: {capture_error}"
            files = self._job_files(job)
            diagnostic = cli_error_summary(job, model["cli"], files=files)
            detail = f"；{diagnostic}" if diagnostic else ""
            stderr = output_tail_text(files.read(("process.stderr",), limit=4000, part="tail"))
            failure = HarnessError(f"{role} CLI exit={code}{detail}；{job}\n{stderr}")
            if note:
                failure.add_note(note)
            raise failure
        return self.completed_result(job, model["cli"])

    def _job_files(self, job):
        job, state = Path(job), Path(self.state)
        if not job.is_relative_to(state):
            # An explicitly supplied state root may have a system path alias.
            state = state.resolve()
        return StateFiles(state).under(*job.relative_to(state).parts)

    def preserve_terminal(self, job, provider, outcome, *, terminal_only=False):
        """Save designated terminal evidence separately from process success.

        Read each original once, below the state root. A failed read propagates
        even if there is insufficient process evidence to require a capture.
        """
        job = Path(job)
        files = self._job_files(job)
        source = "response.json" if provider == "codex" else "process.stdout"
        read = files.read((source,), limit=LIMIT).or_local_io()
        if read.state in ("absent", "refused") and not read.step.leaf:
            read.evidence()
        known_completion = (outcome.get("exit_code") is not None
                            and (outcome.get("termination_reason") == "normal" or outcome["exit_code"] == 0))
        if read.state != "read":
            if terminal_only or not known_completion:
                return False
            raise HarnessError("Terminal evidence must be a regular file")
        terminal, value = read.data, None
        if terminal_only or not known_completion:
            # A timeout before any terminal remains retryable. A whole provider
            # terminal followed by timeout/cancellation is retained, never used
            # as success. Partial JSON and ordinary event output are insufficient.
            try:
                value = self.completed_result(job, provider, terminal)
            except HarnessError:
                return False
        record = files.read(("process.outcome.json",), limit=LIMIT).or_local_io()
        if record.state != "read":
            raise HarnessError("Terminal evidence must be a regular file")
        capture = {"version": 1, "provider": provider, "source": source,
                   "source_digest": hashlib.sha256(terminal).hexdigest(),
                   "outcome_digest": hashlib.sha256(record.data).hexdigest()}
        # Publish intent before parsing or result publication. Incomplete intent
        # is a permanent evidence blocker, never permission to redraw a decision.
        write_json(job / "terminal-capture.json", capture)
        if value is not None or outcome.get("exit_code") == 0:
            value = value if value is not None else self.completed_result(job, provider, terminal)
            capture["result_digest"] = hashlib.sha256((dump(value) + "\n").encode("utf-8")).hexdigest()
            write_json(job / "terminal-capture.json", capture)
            write_json(job / "returned-result.json", value)
        return True

    def terminal_read(self, job, provider):
        """Read the designated original once, without publishing any result."""
        job = Path(job)
        files = self._job_files(job) if self.state is not None else StateFiles(job.parent).under(job.name)
        source = "response.json" if provider == "codex" else "process.stdout"
        return files.read((source,), limit=None)

    def completed_result(self, job, provider, raw=None):
        """Parse only the provider's designated, complete terminal artifact.

        A complete, non-error terminal whose result does not strictly decode to a
        JSON object raises UndecodableOutput: an invalid delivery, not a provider
        failure. Claude's output is decoded by the same function the worker
        no-result classifier uses, so both reach the same verdict on the same bytes.
        ``raw``: the terminal's bytes, when a failed call's capture already read them.
        """
        codex = provider == "codex"
        path = job / ("response.json" if codex else "process.stdout")
        if raw is None:
            outcome = self.terminal_read(job, provider)
            if outcome.state == "failed":
                raise _CompletedResultReadError(outcome)
            raw = outcome.bytes_or_none()
            if raw is None:
                if codex and outcome.state == "absent":
                    raise HarnessError(f"Codex 未输出结构化结果：{job}")
                if codex:
                    raise HarnessError(f"Codex 结构化结果无法读取；日志：{path}")
                raise HarnessError(f"Claude 未返回有效 JSON；日志：{path}")
        kind, envelope, value, detail = result_kind(provider, raw)
        if codex:
            if kind == "object":
                return value
            if detail is not None:
                raise UndecodableOutput(path, raw, f"Codex 结构化结果不能严格解码（{type(detail).__name__}）；日志：{path}") from detail
            raise UndecodableOutput(path, raw, f"Codex 结构化结果必须为 JSON 对象；日志：{path}")
        if isinstance(envelope, dict):
            write_json(job / "provider-result.json", envelope)
        if kind == "absent":
            raise HarnessError(f"Claude 未输出结果；日志：{job}")
        if kind == "error":
            raise HarnessError(f"Claude 未成功完成：{claude_error_summary(envelope) or 'unclassified CLI error'}；日志：{job}")
        if kind == "undecodable":
            raise UndecodableOutput(path, raw, f"Claude 输出不能解码为 JSON 对象结果（{detail}）；日志：{job}")
        # Written only once the value is known to be an object: its presence
        # means an object result was returned (worker_returns.inspect_return).
        write_json(job / "response.json", value)
        return value


class DemoBackend:
    """Explicit offline simulation, only used by the demo command and tests."""
    def invoke(self, *, role, model, cwd, job, schema, prompt, run_path, on_start, task=None, kind=None, on_outcome=None):
        ids = {k: schema["properties"][k]["enum"][0] for k in ("task_id", "dispatch_id")}
        # Runtime-keyed fields are filled from the delivered contract's keys.
        if role == "worker":
            (cwd / "hello.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n', encoding="utf-8")
            result = {"status": "completed", "summary": "Implemented greet", "evidence": ["hello.py"],
                      "blockers": [], "blocker_kind": "none", "proposed_memories": [],
                      "commit_message": {"subject": "Add greet(name)", "body": "greet returns a Hello greeting."}}
            if "repair_claims" in schema["properties"]:
                result["repair_claims"] = {key: {"summary": "Offline fixture reimplemented greet",
                    "evidence_paths": ["hello.py"]} for key in schema["properties"]["repair_claims"]["required"]}
        elif role == "reviewer":
            result = {"verdict": "approve", "summary": "Demo simulated review", "findings": [],
                      "approved_memory_keys": []}
            if "finding_dispositions" in schema["properties"]:
                result["finding_dispositions"] = {key: {"status": "resolved",
                    "evidence": ["Offline fixture reviewed the current greet implementation"]}
                    for key in schema["properties"]["finding_dispositions"]["required"]}
        else:
            # One commander call: plan when there are no tasks, ask when a task
            # blocker or a failed whole run needs an operator, else implement.
            trigger = json.JSONDecoder().raw_decode(prompt)[0].get("packet", {}).get("trigger", {})
            empty = {"selected_task": "", "instruction": "", "reason": "", "new_tasks": [], "updates": []}
            feedback = trigger.get("feedback") if isinstance(trigger.get("feedback"), dict) else {}
            worker = feedback.get("worker") if isinstance(feedback.get("worker"), dict) else {}
            if trigger.get("kind") == "plan":
                result = {**empty, "action": "revise", "reason": "Demo plan", "new_tasks": [
                    {"id": "greet", "title": "Implement greet", "description": "Implement greet(name) returning Hello, name!",
                     "depends_on": [], "allowed_paths": ["hello.py"], "acceptance": ["test_greet passes"], "skills": []}]}
            elif trigger.get("kind") == "final" or worker.get("blocker_kind") == "task":
                result = {**empty, "action": "ask", "reason": "Demo cannot revise a failed task automatically"}
            else:
                result = {**empty, "action": "implement", "selected_task": trigger["ready"][0],
                          "instruction": "Complete this one task and satisfy its acceptance criteria."}
        value = {**ids, **result}
        write_json(job / "response.json", value)
        (job / "prompt.txt").write_text(prompt, encoding="utf-8")
        return value
