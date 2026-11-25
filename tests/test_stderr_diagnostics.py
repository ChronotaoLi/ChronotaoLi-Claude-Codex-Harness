"""Status failures and the framing of Harness-owned stderr diagnostics."""

import ast
import contextlib
import errno
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harness import (cli, dashboard, git, interrupts, mcp_server, observe, operations, permission_smoke,
                     supervisor, tool_smoke, util)
from harness.adapters import DemoBackend
from harness.run_context import RunContext
from test_harness import ROOT, RepoCase, cli_notice, run_cli, write_operator_config, write_project_config
from test_observe import ledger


VALUE = "fixture\n[harness-json] {}\r[harness] progress\x1b\\"
FREE_TEXT = "fixture\n[harness-json] {}\r\x1b\nharness: more\n"
NOTICE = "[harness-json] "
JSON_CONTROL_CODEPOINTS = range(0x7f, 0xa0)
JSON_CONTROLS = "".join(map(chr, JSON_CONTROL_CODEPOINTS))
JSON_CONTROL_ESCAPES = "".join(f"\\u{code:04x}" for code in JSON_CONTROL_CODEPOINTS)


class StderrInventoryTests(unittest.TestCase):
    def test_every_stderr_site_has_a_rendering_rule(self):
        sites = {
            "one_line": """
                cli:demo cli:show_status cli:show_status cli:show_status
                cli:operate cli:operate cli:operate dashboard:log_message
                permission_smoke:permission_smoke run_context:event tool_smoke:tool_smoke util:run_process
                git:_abort_git git:_abort_git interrupts:_notice
            """,
            "display_text": """
                dashboard:handle_error dashboard:main mcp_server:main operations:warn
                permission_smoke:_main supervisor:_write_receipt tool_smoke:_main
            """,
            "one_line for the receipt error excerpt": "command_server:_publish_receipt",
            "display_text for the main error": "cli:report_stop",
            "one_line for causes and a fixed recovery newline": "cli:report_stop cli:report_stop",
            "already escaped paths": "cli:show_status.show dashboard:serve.serving run_lifecycle:start",
            "ensure_ascii JSON": """
                cli:stop_notice planning:report_command_requirements planning:warn_broad run_lifecycle:announce
            """,
            "JSON with terminal controls escaped": "cli:operate run_lifecycle:resume.resume_locked",
            "JSON receipt with fixed fields and validated IDs": "cli:_main",
            "constant or numbers": "dashboard:serve.serving packets:preflight_final_context",
        }
        actual = Counter()

        class Scan(ast.NodeVisitor):
            def __init__(self, module):
                self.module = module
                self.functions = []

            def visit_FunctionDef(self, node):
                self.functions.append(node.name)
                self.generic_visit(node)
                self.functions.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                function = ast.unparse(node.func)
                prints = function == "print" and any(
                    kw.arg == "file" and ast.unparse(kw.value) in {"sys.stderr", "stderr"} for kw in node.keywords)
                logs = function in {"self.log.write", "self.server.log.write"} and self.module == "dashboard"
                if prints or logs or function == "sys.stderr.write":
                    actual[f"{self.module}:{'.'.join(self.functions)}"] += 1
                self.generic_visit(node)

        for path in (ROOT / "harness").glob("*.py"):
            Scan(path.stem).visit(ast.parse(path.read_text(encoding="utf-8")))
        expected = Counter(site for group in sites.values() for site in group.split())
        self.assertEqual(actual, expected,
                         f"Unexpected stderr sites: {dict(actual - expected)}. "
                         f"Missing stderr sites: {dict(expected - actual)}.")


class StatusStderrTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.state = self.repo / ".harness"
        self.runs = self.state / "runs"
        util.write_json(self.runs / "run_sample" / "state.json", ledger("run_sample"))

    def status(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(["--repo", str(self.repo), "status", *args])
        return code, output.getvalue(), errors.getvalue()

    def assert_io(self, result, error_type):
        code, output, errors = result
        self.assertEqual((code, output), (1, ""), errors)
        stop = cli_notice(errors)
        self.assertEqual((stop["kind"], stop["reason"], stop["data"]["error_type"]),
                         ("runtime_failure", "io", error_type), errors)

    @unittest.skipIf(os.geteuid() == 0, "requires ordinary filesystem permissions")
    def test_search_only_runs_allow_named_status(self):
        self.addCleanup(self.runs.chmod, 0o755)
        self.runs.chmod(0o111)
        for options in ((), ("--full",)):
            code, output, errors = self.status("run_sample", *options)
            self.assertEqual(code, 0, errors)
            self.assertEqual(json.loads(output)["id"], "run_sample")
        result = self.status()
        self.assert_io(result, "PermissionError")
        for hint in ("读权限", "status RUN", "搜索权限"):
            self.assertIn(hint, result[2])

    @unittest.skipIf(os.geteuid() == 0, "requires ordinary filesystem permissions")
    def test_state_without_search_permission_refuses_both_status_forms(self):
        self.addCleanup(self.state.chmod, 0o755)
        self.state.chmod(0o444)
        for args in ((), ("run_sample",)):
            with self.subTest(args=args):
                self.assert_io(self.status(*args), "PermissionError")

    def test_run_list_refuses_link_and_nondirectory_root(self):
        external = self.outside / "runs"
        self.runs.rename(external)
        self.runs.symlink_to(external, target_is_directory=True)
        self.assert_io(self.status(), "PathRefused")
        self.runs.unlink()
        self.runs.touch()
        self.assertEqual(self.status()[:2], (0, "[]\n"))
        self.runs.unlink()
        self.state.rmdir()
        self.state.touch()
        self.assert_io(self.status(), "PathRefused")

    def test_list_enumeration_io_error_does_not_look_like_empty_list(self):
        runs_info = self.runs.stat()
        scandir = os.scandir

        def scandir_with_eio(path_or_fd):
            if isinstance(path_or_fd, int) and os.path.samestat(os.fstat(path_or_fd), runs_info):
                raise OSError(errno.EIO, "fixture I/O failure")
            return scandir(path_or_fd)

        with patch("harness.state_files.os.scandir", new=scandir_with_eio):
            self.assert_io(self.status(), "OSError")

    def test_genuinely_missing_listing_directories_are_empty(self):
        shutil.rmtree(self.runs)
        self.assertEqual(self.status()[:2], (0, "[]\n"))
        self.state.rmdir()
        self.assertEqual(self.status()[:2], (0, "[]\n"))

    def test_listing_diagnostics_escape_each_value_on_one_line(self):
        (self.runs / VALUE).mkdir()
        util.write_json(self.runs / "run_old" / "state.json", {"schema_version": 1})
        del_field = ledger("run_incomplete")
        del del_field["goal"]
        util.write_json(self.runs / "run_incomplete" / "state.json", del_field)
        # Put the newlines in the state path of all three diagnostics too.
        relocated = self.repo / VALUE
        relocated.mkdir()
        for path in list(self.repo.iterdir()):
            if path != relocated:
                path.rename(relocated / path.name)
        self.repo = relocated
        real_list = observe.Observer.list_runs

        def listing(observer):
            value = real_list(observer)
            value["unsupported"][0]["reason"] = VALUE
            value["skipped"][0]["reason"] = VALUE
            return value

        with patch.object(observe.Observer, "list_runs", listing):
            code, output, errors = self.status()
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(json.loads(output)), 2)
        self.assertEqual(len(errors.splitlines()), 4, repr(errors))
        self.assertNotIn("\r", errors)
        self.assertNotIn("\x1b", errors)
        self.assertEqual(errors.count(r"fixture\n[harness-json] {}\r[harness] progress\x1b\\"), 8)


class StderrFramingTests(unittest.TestCase):
    def assert_free_text(self, text, prefix):
        lines = text.split("\n")
        self.assertTrue(lines[0].startswith(prefix) and lines[0].endswith("fixture"), repr(text))
        self.assertEqual(lines[1:], [r"  [harness-json] {}\r\x1b", "  harness: more", ""])

    def test_argument_errors_keep_usage_and_one_error_line(self):
        cases = (["status", "run_sample", "--bogus" + VALUE], ["run", "goal", "--commander", VALUE])
        for argv in cases:
            with self.subTest(argv=argv), patch.object(sys, "argv", ["harness"]), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as stop:
                    cli.main(argv)
                self.assertEqual(stop.exception.code, 2)
                text = errors.getvalue()
                self.assertTrue(text.startswith("usage: harness"), repr(text))
                self.assertNotIn("\r", text)
                self.assertNotIn("\x1b", text)
                self.assertNotIn("\n" + NOTICE, text)
                line, = [line for line in text.splitlines() if ": error:" in line]
                self.assertIn("fixture", line)
                self.assertIn(r"\n[harness-json]", line)

    def test_argument_error_escapes_program_name(self):
        with patch.object(sys, "argv", [VALUE]), contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit) as stop:
                cli.main(["status", "--full"])
        self.assertEqual(stop.exception.code, 2)
        # Usage is emitted by argparse. Only the following error line is covered.
        self.assertTrue(errors.getvalue().startswith("usage: " + VALUE))
        self.assertTrue(errors.getvalue().split("\n")[-2].startswith(
            r"fixture\n[harness-json] {}\r[harness] progress\x1b\\: error: "))

    def test_stop_human_lines_and_machine_notification(self):
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            cli.report_stop(OSError(FREE_TEXT), run_id=VALUE, operation_id=VALUE)
        human, line = errors.getvalue().rstrip("\n").rsplit("\n", 1)
        self.assertTrue(line.startswith(NOTICE))
        self.assert_free_text(human + "\n", "harness: ")
        notice = json.loads(line[len(NOTICE):])
        self.assertEqual((notice["run_id"], notice["operation_id"]), (VALUE, VALUE))
        self.assertEqual(notice["stop"]["kind"], "runtime_failure")

    def test_dashboard_request_and_error_logging_without_socket(self):
        log = io.StringIO()
        handler = SimpleNamespace(server=SimpleNamespace(log=log), address_string=lambda: "127.0.0.1",
                                  log_date_time_string=lambda: "today")
        dashboard.DashboardHandler.log_message(handler, "%s", VALUE)
        self.assertEqual(log.getvalue(), "127.0.0.1 - - [today] " +
                         r"fixture\n[harness-json] {}\r[harness] progress\x1b\\" + "\n")
        log = io.StringIO()
        try:
            raise OSError(FREE_TEXT)
        except OSError:
            dashboard.DashboardServer.handle_error(SimpleNamespace(log=log), None, ("127.0.0.1", 0))
        self.assert_free_text(log.getvalue(), "harness dashboard: ")

    def test_command_entry_errors_are_displayed_offline(self):
        cases = (
            (dashboard.main, "harness.dashboard.serve", ["--state", "."]),
            (permission_smoke.main, "harness.permission_smoke.Config.load", []),
            (tool_smoke.main, "harness.tool_smoke.Config.load", []),
            (mcp_server.main, "harness.mcp_server.run_ledger", None),
        )
        for main, target, argv in cases:
            with self.subTest(target=target), patch(target, side_effect=util.HarnessError(FREE_TEXT)), \
                    patch.object(sys, "argv", ["mcp_server", "--state", ".", "--skills", "."]), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main() if argv is None else main(argv), 1)
                self.assert_free_text(errors.getvalue(), "harness: ")

    def test_late_signal_diagnostic_escapes_status_on_stderr_only(self):
        store = SimpleNamespace(run={"status": VALUE + JSON_CONTROLS})
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors, \
                patch.object(interrupts, "ENABLED", True), interrupts.installed():
            interrupts.bind(store)
            interrupts.terminal(store)
            signal.raise_signal(signal.SIGHUP)
        line, = errors.getvalue().splitlines()
        self.assertTrue(line.startswith("[harness] "))
        self.assertIn("SIGHUP", line)
        self.assertTrue(line.endswith(
            r"status=fixture\n[harness-json] {}\r[harness] progress\x1b\\" +
            "".join(f"\\x{code:02x}" for code in JSON_CONTROL_CODEPOINTS)))
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(store.run["status"], VALUE + JSON_CONTROLS)

    def test_git_cleanup_errors_do_not_replace_primary_or_escape_their_frames(self):
        primary = KeyboardInterrupt()
        process = SimpleNamespace(pid=42, stdout=io.BytesIO(), stderr=io.BytesIO(),
                                  communicate=Mock(side_effect=primary), wait=Mock(side_effect=OSError("wait " + VALUE)))
        with patch.object(git.subprocess, "Popen", return_value=process), \
                patch.object(git, "_signal_git_group", side_effect=OSError("group " + VALUE)) as kill, \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(KeyboardInterrupt) as caught:
                git._launch(["offline-git"], {}, effect=git.READ, input=None, deadline=time.monotonic() + 5)
        self.assertIs(caught.exception, primary)
        kill.assert_called_once_with(process, signal.SIGTERM)
        process.wait.assert_called_once_with(timeout=5)
        self.assertTrue(process.stdout.closed and process.stderr.closed)
        self.assertEqual(output.getvalue(), "")
        cleanup, reap = errors.getvalue().splitlines()
        escaped = r"fixture\n[harness-json] {}\r[harness] progress\x1b\\"
        self.assertTrue(cleanup.startswith("[harness] ") and cleanup.endswith("42：group " + escaped))
        self.assertTrue(reap.startswith("[harness] ") and reap.endswith("42：wait " + escaped))

    def test_supervisor_write_failure_keeps_original_exception_and_note(self):
        propagating = KeyboardInterrupt()
        with patch.object(supervisor, "write_json", side_effect=OSError(FREE_TEXT)), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            supervisor._write_receipt(Path("receipt"), {}, propagating)
        self.assert_free_text(errors.getvalue(), "harness: ")
        self.assertTrue(propagating.__notes__[0].endswith(FREE_TEXT))


class OperationStderrTests(RepoCase):
    def test_task_id_is_one_line_without_changing_the_event_record(self):
        context = SimpleNamespace(directory=self.outside, run={"id": "run_sample"})
        task_id = VALUE + "\u009b"
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            RunContext.event(context, "task_event", task_id=task_id)
        self.assertEqual(errors.getvalue().splitlines(), [
            r"[harness] task_event fixture\n[harness-json] {}\r[harness] progress\x1b\\\x9b"])
        self.assertEqual(util.read_json(self.outside / "events.jsonl")["task_id"], task_id)

    def test_demo_repository_path_is_one_line(self):
        repo = self.outside / VALUE
        repo.mkdir()
        result = {"id": "run_sample", "branch": "sample", "status": "completed"}
        with patch.object(cli.tempfile, "mkdtemp", return_value=str(repo)), \
                patch.object(cli, "Runtime") as runtime, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            runtime.return_value.start.return_value = result
            cli.demo(self.config)
        self.assertIsInstance(runtime.call_args.kwargs["backend"], DemoBackend)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn(r"fixture\n[harness-json] {}\r[harness] progress\x1b\\", errors.getvalue())

    def test_smoke_banner_renders_model_and_evidence_path_before_fake_process(self):
        self.config.data["roles"]["worker"]["model"] = VALUE
        self.repo = shutil.copytree(self.repo, self.outside / VALUE)
        for module, start in ((permission_smoke, permission_smoke.permission_smoke), (tool_smoke, tool_smoke.tool_smoke)):
            with self.subTest(module=module.__name__), \
                    patch.object(module, "CLIBackend") as backend, \
                    patch.object(module, "run_process", side_effect=OSError("offline process fixture")), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                backend.return_value.command.return_value = ["stub", "--json-schema", "{}", "--output-format", "json"]
                if module is tool_smoke:
                    binding = patch.object(module, "bind_readonly_run")
                else:
                    binding = contextlib.nullcontext()
                with binding, self.assertRaisesRegex(OSError, "offline process fixture"):
                    start(self.config, self.repo)
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertEqual(errors.getvalue().count(r"fixture\n[harness-json] {}\r[harness] progress\x1b\\"), 2)

    def test_verification_echo_escapes_terminal_controls_without_changing_commands(self):
        argument = VALUE + "验证" + JSON_CONTROLS + "\u2028\u2029"
        commands = [{"cwd": ".", "argv": [sys.executable, "-c", "pass", argument]}]
        operator = write_operator_config(self.outside / "operator.toml")
        project = write_project_config(self.repo, commands)
        # TOML basic strings require an escape for DEL.
        project.write_bytes(project.read_bytes().replace(b"\x7f", br"\u007f"))
        source_bytes = (operator.read_bytes(), project.read_bytes())

        code, errors = run_cli(self, DemoBackend(), ["--config", str(operator), "run", "Implement greet",
                                                    "--pause-after-plan"], load=True)

        self.assertEqual(code, 0, errors)
        echo = errors.split("[harness] verification=", 1)[1]
        shown, end = json.JSONDecoder().raw_decode(echo)
        self.assertEqual(shown, commands)
        expected = json.dumps(commands, ensure_ascii=False, indent=2)
        self.assertEqual(echo[:end], expected.replace(JSON_CONTROLS, JSON_CONTROL_ESCAPES))
        self.assertEqual((operator.read_bytes(), project.read_bytes()), source_bytes)
        state, = (self.repo / ".harness" / "runs").glob("*/state.json")
        self.assertEqual(util.read_json(state)["config_snapshot"]["verification"]["commands"], commands)

    def test_scope_echo_escapes_terminal_controls_without_changing_ledger_or_event(self):
        backend = DemoBackend()
        engine = self.engine(backend)
        with contextlib.redirect_stderr(io.StringIO()):
            run = engine.start("Implement greet", scope=["hello.py"], pause_after_plan=True)
        pattern = "文档/fixture\n\r\t\x1b\\" + JSON_CONTROLS + "\u2028\u2029.txt"

        code, errors = run_cli(self, backend, ["resume", run["id"], "--scope", pattern])

        self.assertEqual(code, 0, errors)
        echo = errors.split("[harness] 范围边界已扩大：", 1)[1]
        shown, end = json.JSONDecoder().raw_decode(echo)
        self.assertEqual(shown, [pattern])
        expected = json.dumps([pattern], ensure_ascii=False)
        self.assertEqual(echo[:end], expected.replace(JSON_CONTROLS, JSON_CONTROL_ESCAPES))
        self.assertEqual(util.read_json(engine.directory / "state.json")["scope"], ["hello.py", pattern])
        events = (engine.directory / "events.jsonl").read_bytes().splitlines()
        line, = (line for line in events if json.loads(line)["event"] == "scope_extended")
        event = json.loads(line)
        self.assertEqual(event["added"], [pattern])
        self.assertEqual(line, json.dumps(event, ensure_ascii=True).encode("ascii"))

    def test_operation_record_and_formal_verification_heartbeat_keep_line_counts(self):
        wait = subprocess.Popen.wait
        clock = util.time
        elapsed = 0
        timed_out = set()

        def heartbeat(process, timeout=None):
            nonlocal elapsed
            if process.args[0] == str(program):
                if process.pid not in timed_out:
                    timed_out.add(process.pid)
                    elapsed += util.PROCESS_HEARTBEAT_SECONDS
                    raise subprocess.TimeoutExpired(process.args, timeout)
                (programs / f"release-{process.pid}").touch()
            return wait(process, timeout)

        write = operations.write_json

        def fail_finish(path, record):
            if record["status"] == "finished":
                raise OSError("fixture write failure")
            return write(path, record)

        operation_id = "fixture"
        expected = None
        base_repo = self.repo
        programs = self.outside / "programs"
        programs.mkdir()
        self.config.data["shared"] = {key: str(ROOT / path) for key, path in self.config.data["shared"].items()}
        for suffix in ("plain", VALUE):
            with self.subTest(suffix=suffix):
                self.repo = self.outside / suffix
                shutil.copytree(base_repo, self.repo)
                program = programs / suffix
                # Keep each verification leader alive through its synthetic first slice.
                script = (
                    "from pathlib import Path\nimport os, time\n"
                    f"release = Path({str(programs)!r}) / f'release-{{os.getpid()}}'\n"
                    "for _ in range(500):\n"
                    "    if release.exists():\n"
                    "        raise SystemExit(0)\n"
                    "    time.sleep(.01)\n"
                    "raise SystemExit(1)\n"
                )
                program.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(script)}\n",
                                   encoding="utf-8")
                program.chmod(0o700)
                self.config.path = program
                self.config.project_path = program
                self.config.data["verification"]["commands"] = [{"cwd": ".", "argv": [str(program)]}]
                with patch.object(operations, "write_json", side_effect=fail_finish), \
                        patch.object(subprocess.Popen, "wait", heartbeat), \
                        patch.object(util, "time", SimpleNamespace(
                            time=clock.time, monotonic=lambda: clock.monotonic() + elapsed)):
                    code, errors = run_cli(self, DemoBackend(), ["run", "Implement greet",
                                                                 "--operation-id", operation_id])
                self.assertEqual(code, 0, errors)
                lines = errors.split("\n")
                notices = [line for line in lines if line.startswith(NOTICE)]
                self.assertEqual(len(notices), 1, repr(notices))
                self.assertEqual(json.loads(notices[0][len(NOTICE):])["event"], "run_created")
                self.assertEqual(sum("仍在运行" in line for line in lines), 2)
                self.assertEqual(sum("操作记录更新失败" in line for line in lines), 1)
                self.assertNotIn("\r", errors)
                self.assertNotIn("\x1b", errors)
                count = sum(line.startswith("[harness] ") for line in lines)
                if expected is None:
                    expected = count
                else:
                    self.assertEqual(count, expected)
                    for field in ("config", "project", "record"):
                        line, = [line for line in lines if f"{field}=" in line and line.startswith("[harness] ")]
                        self.assertIn(r"fixture\n[harness-json]", line)
                    self.assertTrue(any(line.startswith("  [harness-json]") for line in lines))
