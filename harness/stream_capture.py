"""Bounded Claude JSONL transport. Only projected metadata reaches diagnostics."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import selectors
import queue
import threading
import sys
import subprocess
import tempfile
import time
from pathlib import Path

from .util import HarnessError, ProcessStartError, ProcessTimeout, read_json, write_json

TERMINAL_LIMIT = 16 * 1024 * 1024
METADATA_LIMIT = 1024 * 1024
RECORD_LIMIT = TERMINAL_LIMIT


def claude_result_path(job):
    manifest = job / "capture-manifest.json"
    if not manifest.exists():
        return job / "process.stdout"
    value = read_json(manifest)
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("mode") not in ("legacy_json", "stream_metadata_v1"):
        raise HarnessError("Invalid capture manifest")
    return job / ("stream-terminal.json" if value["mode"] == "stream_metadata_v1" else "process.stdout")


def freeze_capture(job, mode, cli_version):
    value = {"version": 1, "mode": mode, "cli_version": cli_version,
             "transport": "jsonl" if mode == "stream_metadata_v1" else "json"}
    with (job / "capture-manifest.json").open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=True)
    return value


def _identifier(value):
    return value if isinstance(value, str) and 0 < len(value) <= 160 and all(
        char.isascii() and (char.isalnum() or char in "_-.:") for char in value) else None


def project_event(event, dispatch_id):
    """Tool bodies, inputs, assistant text and thinking are never projected."""
    result = {"dispatch_id": dispatch_id, "received_at": time.time()}
    session = _identifier(event.get("session_id"))
    if session:
        result["provider_session_id"] = session
    if event.get("type") == "result":
        return {**result, "event": "terminal"}
    message = event.get("message")
    content = message.get("content", []) if isinstance(message, dict) else []
    records = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use":
                ident, name = _identifier(block.get("id")), _identifier(block.get("name"))
                if ident and name:
                    records.append({**result, "event": "tool_start", "tool_id": ident, "tool": name})
            elif kind == "tool_result":
                ident = _identifier(block.get("tool_use_id"))
                if ident:
                    records.append({**result, "event": "tool_end", "tool_id": ident,
                                    "status": "error" if block.get("is_error") is True else "reported_return"})
    return records


def run_stream_process(argv, cwd, log, *, stdin="", timeout=900, env=None, on_start=None,
                       terminal_limit=TERMINAL_LIMIT, metadata_limit=METADATA_LIMIT,
                       record_limit=RECORD_LIMIT, on_outcome=None,
                       execution_deadline=None, cleanup_deadline=None):
    """One selector drains both pipes; no observer queue can block the child.

    Parsing finishes before exit is interpreted. Oversized/malformed transport
    permanently invalidates the protocol even when a terminal was already seen.
    """
    from .process_runtime import ProcessSupervisor, defer_cleanup_signals
    execution_deadline = min(time.monotonic() + timeout, execution_deadline if execution_deadline is not None else float("inf"))
    writer_deadline = None
    job = log.parent
    terminal = job / "stream-terminal.json"
    if terminal.exists():
        raise HarnessError("Stream terminal already exists")
    selector = selectors.DefaultSelector()
    pending = bytearray()
    damaged = False
    overflow = False
    terminals = 0
    candidate = None
    metadata_bytes = dropped = stderr_bytes = 0
    diagnostics_incomplete = False
    digest = hashlib.sha256()
    p = None
    metadata_queue = queue.Queue(maxsize=128)
    writer_stop = threading.Event()
    writer_failed = threading.Event()
    writer_counts = {"enqueued": 0, "written": 0}

    def writer_main():
        try:
            with (job / "stream-metadata.jsonl").open("xb") as output:
                while not writer_stop.is_set() or not metadata_queue.empty():
                    try:
                        raw = metadata_queue.get(timeout=.05)
                    except queue.Empty:
                        continue
                    output.write(raw)
                    output.flush()
                    writer_counts["written"] += 1
        except OSError:
            writer_failed.set()

    writer = threading.Thread(target=writer_main, daemon=True, name="harness-metadata-writer")

    def finish_writer():
        nonlocal writer_deadline
        if writer_deadline is None:
            writer_deadline = min(time.monotonic() + 2, execution_deadline)
        writer_stop.set()
        writer.join(timeout=max(0, (writer_deadline if writer_deadline is not None else execution_deadline) - time.monotonic()))
        return writer_failed.is_set() or writer.is_alive()

    def emit(record):
        nonlocal metadata_bytes, dropped, diagnostics_incomplete
        raw = (json.dumps(record, ensure_ascii=True) + "\n").encode()
        if writer_failed.is_set() or metadata_bytes + len(raw) > metadata_limit:
            dropped += 1
            diagnostics_incomplete = True
            return
        try:
            metadata_queue.put_nowait(raw)
            writer_counts["enqueued"] += 1
            metadata_bytes += len(raw)
        except queue.Full:
            diagnostics_incomplete = True
            dropped += 1

    def parse(raw):
        nonlocal damaged, terminals, candidate
        if not raw.strip():
            return
        try:
            event = json.loads(raw)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("invalid event")
        except (ValueError, UnicodeError, RecursionError):
            damaged = True
            return
        if event["type"] == "result":
            terminals += 1
            if len(raw) > terminal_limit:
                damaged = True
            elif terminals == 1:
                candidate = event
        projected = project_event(event, job.name)
        for record in projected if isinstance(projected, list) else [projected]:
            emit(record)

    supervision = ProcessSupervisor(log, on_outcome=on_outcome, cleanup_deadline=cleanup_deadline)
    try:
        writer.start()
        with tempfile.TemporaryFile(dir=job) as source:
            source.write(stdin.encode("utf-8"))
            source.seek(0)
            if time.monotonic() >= execution_deadline:
                raise ProcessTimeout(timeout, log)
            # Retain cancellation until the supervisor owns the new process.
            with defer_cleanup_signals():
                supervision.begin_launch()
                try:
                    p = subprocess.Popen(argv, cwd=cwd, stdin=source, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env=env, start_new_session=True)
                except (OSError, ValueError) as error:
                    raise ProcessStartError("Unable to start stream CLI") from error
                supervision.started(p)
            started = time.monotonic()
            if on_start:
                on_start(p.pid)
            for stream in (p.stdout, p.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = execution_deadline - time.monotonic()
                if remaining <= 0:
                    raise ProcessTimeout(timeout, log)
                for key, _ in selector.select(min(.1, remaining)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.fileobj is p.stderr:
                        # Stderr content is untrusted: retain only bounded byte counts.
                        stderr_bytes += len(chunk)
                        if stderr_bytes > metadata_limit:
                            diagnostics_incomplete = True
                        continue
                    digest.update(chunk)
                    parts = chunk.split(b"\n")
                    for index, part in enumerate(parts):
                        if index < len(parts) - 1:
                            part += b"\n"
                        if not overflow:
                            pending.extend(part)
                            if len(pending) > record_limit:
                                damaged = overflow = True
                                pending.clear()
                        if part.endswith(b"\n"):
                            if not overflow:
                                parse(bytes(pending))
                            pending.clear()
                            overflow = False
            if pending or overflow:
                damaged = True  # A truncated final record cannot exclude lost terminal data.
            remaining = execution_deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessTimeout(timeout, log)
            try:
                code = p.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                raise ProcessTimeout(timeout, log) from error
            diagnostics_incomplete = finish_writer() or diagnostics_incomplete
            if writer.is_alive():
                raise HarnessError("Metadata writer did not converge; stream capture incomplete")
            write_json(job / "stream-capture.json", {
                "version": 1, "stdout_sha256": digest.hexdigest(), "terminal_count": terminals,
                "protocol_complete": not damaged and terminals == 1,
                "diagnostics_incomplete": diagnostics_incomplete,
                "dropped_events": dropped + writer_counts["enqueued"] - writer_counts["written"],
                "stderr_bytes": min(stderr_bytes, metadata_limit), "exit_code": code})
            if damaged or terminals != 1:
                raise HarnessError("Incomplete or ambiguous Claude stream protocol")
            try:
                artifact = (json.dumps(candidate, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
            except (ValueError, UnicodeError, RecursionError) as error:
                raise HarnessError("Unable to serialize Claude terminal safely") from error
            if len(artifact) > terminal_limit:
                raise HarnessError("Claude terminal exceeds capture limit")
            temporary = None
            try:
                # Bound these exact bytes, then atomically publish without replacing
                # any existing authority, including a concurrently created symlink.
                with tempfile.NamedTemporaryFile(mode="wb", dir=job,
                                                 prefix=".stream-terminal-", delete=False) as output:
                    temporary = Path(output.name)
                    output.write(artifact)
                    output.flush()
                    os.fsync(output.fileno())
                os.link(temporary, terminal)
            except OSError as error:
                raise HarnessError("Unable to publish Claude terminal safely") from error
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return code
    finally:
        primary = sys.exc_info()[1]
        if primary is not None and candidate is not None and not terminal.exists():
            # Preserve a received whole terminal as refusal evidence even when
            # later timeout/cancellation prevents transport completion. This is
            # never the authoritative stream terminal or successful protocol.
            try:
                write_json(job / "stream-incomplete-terminal.json", {
                    "terminal": candidate, "terminal_count": terminals,
                    "protocol_complete": False, "damaged": damaged})
            except (Exception, KeyboardInterrupt, SystemExit) as error:
                supervision.outcome.cleanup_errors.append({"step": "preserve_incomplete_terminal",
                    "type": type(error).__name__, "message": str(error), "errno": getattr(error, "errno", None)})
        try:
            with defer_cleanup_signals(primary=primary) as deferred:
                writer_deadline = min(writer_deadline or float("inf"), time.monotonic() + 2, cleanup_deadline if cleanup_deadline is not None else float("inf"))
                supervision.cleanup_deadline = writer_deadline
                cleanup_failure = None
                actions = []
                if p is not None:
                    actions.extend([("close_stdout", p.stdout.close), ("close_stderr", p.stderr.close)])
                actions.append(("close_selector", selector.close))
                if writer.ident is not None:
                    actions.append(("finish_writer", finish_writer))
                for step, action in actions:
                    try:
                        action()
                    except (Exception, KeyboardInterrupt, SystemExit) as error:
                        supervision.outcome.cleanup_errors.append({"step": step,
                            "type": type(error).__name__, "message": str(error),
                            "errno": getattr(error, "errno", None)})
                        cleanup_failure = cleanup_failure or error
                supervision.finish(primary or cleanup_failure, captured=deferred)
                if primary is None and cleanup_failure is not None:
                    raise cleanup_failure
        except KeyboardInterrupt as error:
            if primary is not None:
                raise primary
            if getattr(error, "outcome", None) is None:
                supervision.record_cancellation(error)
            raise



def permission_denial_projection(value):
    """Reason is unknown unless a verified provider enum is supported."""
    entries = value.get("permission_denials", []) if isinstance(value, dict) else []
    if not isinstance(entries, list):
        return []
    projected = []
    for entry in entries[:100]:
        if not isinstance(entry, dict):
            continue
        item = {"reason": "unavailable"}
        for source, target in (("tool_name", "tool"), ("tool_use_id", "tool_id")):
            ident = _identifier(entry.get(source))
            if ident:
                item[target] = ident
        projected.append(item)
    return projected


def command_capability_projection(config, argv):
    # Compatibility import for callers of the initial stream projection API.
    from .permission_diagnostics import command_capability_projection as project
    return project(config, argv)
