"""Command directories on the broker's side: its manifest rows, launches, receipts and admitted root."""
import errno
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_isolation import fixture_git as _git
from harness import command_server, git, util
from harness.command_server import Broker, create_manifest, ensure_command_processes_exited, handle
from harness.util import HarnessError, read_json, write_json
from harness.versions import RUN_SCHEMA_VERSION


# The child reports where it started: the directory's path and its identity.
REPORT = "import os; s = os.stat('.'); print(os.getcwd()); print(s.st_dev, s.st_ino)"

# The word that names each kind of directory failure in a start failure's error; the rest of its text is free.
REASONS = {"missing": "不存在", "not a directory": "不是目录", "symlink": "是符号链接",
           "no permission": "无权限", "cannot enter": "fchdir"}


def reasons(text):
    return [kind for kind, word in REASONS.items() if word in text]


def capture_tail(data):
    """What a record carries of a capture, computed here: its last 4,000 bytes decoded with replacement, marked when cut."""
    text = data[-4000:].decode("utf-8", errors="replace")
    return "[earlier output omitted; see log]\n" + text if len(data) > 4000 else text


def identity(path):
    info = os.stat(path, follow_symlinks=False)
    return [info.st_dev, info.st_ino]


def expected_id(command):
    """The command ID of rule 5, computed here rather than by the code under test."""
    text = json.dumps({"argv": command["argv"], "cwd": command["cwd"]}, ensure_ascii=True,
                      separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def reported(result):
    path, numbers = result["stdout"].splitlines()
    return path, [int(number) for number in numbers.split()]


def call(broker, name, arguments):
    response = handle({"method": "tools/call", "params": {"name": name, "arguments": arguments}}, broker)
    return response["isError"], json.loads(response["content"][0]["text"])


def journals(job):
    folder = Path(job) / "commands"
    return sorted(path.name for path in folder.iterdir()) if folder.exists() else []


class UnboundBrokerTests(unittest.TestCase):
    """Brokers without a run ledger: the root is the manifest's directory and its identity."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root, self.outside = self.base / "root", self.base / "outside"
        (self.root / "sub").mkdir(parents=True)
        self.outside.mkdir()
        # Written by a command that must not start, wherever it would have run.
        self.marker = self.base / "ran"
        self.jobs = iter(range(1 << 30))

    def command(self, cwd, script=REPORT):
        return {"cwd": cwd, "argv": [sys.executable, "-c", script]}

    def broker(self, commands, **options):
        config = {"verification": {"commands": []}, "permissions": {"worker_commands": commands},
                  "runtime": {"verify_timeout_seconds": 30, "agent_timeout_seconds": 120}}
        # The dispatch folder is beside the root, so replacing the root leaves the manifest in place.
        metadata = create_manifest(config, self.root, self.base / f"job{next(self.jobs)}", None, **options)
        return Broker(metadata["artifact"], metadata["artifact_digest"])

    def assert_not_started(self, result, command, reason):
        """Rule 10: the response and, on disk, the call's receipt, captures and journal record a command that never started, and why."""
        folder = Path(result["stdout_path"]).parent
        receipt = read_json(folder / "receipt.json")
        captures = {stream: (folder / ("output." + stream)).read_bytes() for stream in ("stdout", "stderr")}
        self.assertEqual(captures["stdout"], b"")
        # The diagnosis is read from the whole capture; a record carries only its tail, however long the paths in it.
        written = captures["stderr"].decode()
        self.assertIn(repr(command["cwd"]), written)
        self.assertEqual(reasons(written), [reason])
        expected = {"command": command, "exit_code": None, "termination_reason": "start_failed", "timed_out": False,
                    "exit_confirmed": True, "evidence_complete": True,
                    **{stream: capture_tail(data) for stream, data in captures.items()}}
        for record in (result, receipt):
            # Each key must be written: a receipt without exit_code does not record a null one.
            self.assertEqual({key: record.get(key, "<not written>") for key in expected}, expected)
            self.assertIs(record["lifecycle"]["started"], False)
            error = record.get("error")
            self.assertIsInstance(error, str)
            self.assertIn(error, written)
            # It names the directory and the one failure that happened, not another.
            self.assertIn(repr(command["cwd"]), error)
            self.assertEqual(reasons(error), [reason])
        # The response and the receipt are two records of one call: they agree.
        shared = (*expected, "error", "lifecycle")
        self.assertEqual({key: result.get(key) for key in shared}, {key: receipt.get(key) for key in shared})
        journal = read_json(folder / "process.json")
        self.assertEqual((journal["process_state"], journal["exit_confirmed"], journal["pgid"]), ("exited", True, None))
        return captures["stderr"]

    def test_missing_program_keeps_a_complete_start_failed_receipt(self):
        command = {"cwd": ".", "argv": [str(self.root / "missing-program")]}
        broker = self.broker([command])
        tool_error, result = call(broker, "run_command", {"command_id": expected_id(command)})
        self.assertFalse(tool_error)
        receipt = read_json(Path(result["stdout_path"]).parent / "receipt.json")
        for row in (result, receipt):
            self.assertEqual((row["exit_code"], row["termination_reason"], row["exit_confirmed"],
                              row["evidence_complete"]), (None, "start_failed", True, True))
            self.assertFalse(row["lifecycle"]["started"])
            self.assertIn(command["argv"][0], row["stderr"])

    def test_manifest_version_3_lists_each_command_with_its_directory_and_records_the_root_once(self):
        sub, top = self.command("sub"), self.command(".")
        scope = {"task_id": "task", "allowed_paths": ["a.txt"]}
        broker = self.broker([sub, top, sub], delete_scope=scope)
        manifest = read_json(broker.path)
        self.assertEqual(manifest["version"], 3)
        self.assertEqual(manifest["worktree"], str(self.root))
        self.assertNotIn("cwd", manifest)
        self.assertEqual(manifest["root_identity"], identity(self.root))
        self.assertEqual(manifest["delete_scope"], scope)
        # One argv in two directories is two rows; the repeated command is one.
        rows = [{"command_id": expected_id(sub), "command": sub}, {"command_id": expected_id(top), "command": top}]
        self.assertNotEqual(rows[0]["command_id"], rows[1]["command_id"])
        self.assertEqual(manifest["commands"], rows)
        self.assertEqual(call(broker, "list_commands", {}), (False, rows))

    def test_a_subdirectory_command_starts_there_and_the_same_argv_at_the_root_starts_at_the_root(self):
        sub, top = self.command("sub"), self.command(".")
        broker = self.broker([sub, top])
        rows = {row["command_id"]: row["command"] for row in read_json(broker.path)["commands"]}
        for command, directory in ((sub, self.root / "sub"), (top, self.root)):
            with self.subTest(cwd=command["cwd"]):
                failed, result = call(broker, "run_command", {"command_id": expected_id(command)})
                self.assertFalse(failed)
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(reported(result), (str(directory), identity(directory)))
                receipt = read_json(Path(result["stdout_path"]).with_name("receipt.json"))
                self.assertEqual(receipt["command_id"], expected_id(command))
                # The receipt names the manifest's row for this ID, its directory included.
                self.assertEqual(receipt["command"], rows[expected_id(command)])
                self.assertEqual(receipt["command"], command)

    def test_a_missing_directory_is_a_recorded_start_failure_and_the_next_call_is_admitted(self):
        (self.root / "plain").write_text("a file, not a directory\n")
        (self.root / "locked" / "inner").mkdir(parents=True)
        # Readable but not searchable: "locked" opens and entering it fails; nothing below it opens.
        os.chmod(self.root / "locked", 0o600)
        self.addCleanup(os.chmod, self.root / "locked", 0o700)
        # Twenty levels, each name within the 255-byte limit: the diagnosis of this one is longer than the 4,000-byte tail.
        long = "/".join(["x" * 200] * 20)
        cases = {"absent": "missing", long: "missing", "plain/inner": "not a directory",
                 "locked": "cannot enter", "locked/inner": "no permission"}
        commands = {cwd: self.command(cwd, f"open({str(self.marker)!r}, 'w')") for cwd in cases}
        top = self.command(".")
        broker = self.broker([*commands.values(), top])
        job, ran = broker.manifest["job"], []
        for cwd, reason in cases.items():
            with self.subTest(cwd=cwd[:40]):
                if cwd.startswith("locked") and os.geteuid() == 0:
                    self.skipTest("root bypasses directory permissions")
                failed, result = call(broker, "run_command", {"command_id": expected_id(commands[cwd])})
                # A receipt, not a tool error: the call was admitted and its command never started.
                self.assertFalse(failed)
                written = self.assert_not_started(result, commands[cwd], reason)
                if cwd == long:
                    # Whole on disk, so the records carry a cut tail of it.
                    self.assertGreater(len(written), 4000)
                self.assertFalse(self.marker.exists())
                ensure_command_processes_exited(job, state=Path(job).parent)
                failed, following = call(broker, "run_command", {"command_id": expected_id(top)})
                self.assertFalse(failed)
                self.assertEqual(following["exit_code"], 0)
                ran.append(cwd)
        self.assertFalse(os.path.lexists(self.root / "absent"))
        # One journal per call: the failed start and the call admitted after it.
        self.assertEqual(len(journals(job)), 2 * len(ran))

    def test_a_stderr_tail_cut_inside_a_character_is_complete_evidence_and_the_next_call_is_admitted(self):
        # 4,201 bytes: the 4,000-byte tail starts on the last byte of a three-byte character.
        written = b"x" + "汉".encode() * 1400
        self.assertEqual(written[-4000] & 0xC0, 0x80)
        expected = capture_tail(written)
        self.assertEqual(expected, "[earlier output omitted; see log]\n�" + "汉" * 1333)
        cut = self.command(".", "import sys; sys.stderr.buffer.write(b'x' + '\\u6c49'.encode() * 1400)")
        top = self.command(".")
        broker = self.broker([cut, top])
        job = broker.manifest["job"]
        failed, result = call(broker, "run_command", {"command_id": expected_id(cut)})
        self.assertFalse(failed)
        folder = Path(result["stdout_path"]).parent
        self.assertEqual((folder / "output.stderr").read_bytes(), written)

        def carries_the_cut_tail(record):
            self.assertEqual((record.get("evidence_complete"), record.get("persistence_error")), (True, None))
            self.assertEqual((record["exit_code"], record["stdout"]), (0, ""))
            self.assertEqual(record.get("stderr"), expected)
            # The digest is of the whole capture, not of the tail the record carries.
            self.assertEqual(record["stderr_sha256"], hashlib.sha256(written).hexdigest())
        # The response first: a tail that failed to decode leaves no receipt to read.
        carries_the_cut_tail(result)
        carries_the_cut_tail(read_json(folder / "receipt.json"))
        failed, following = call(broker, "run_command", {"command_id": expected_id(top)})
        self.assertFalse(failed, following)
        self.assertEqual(following["exit_code"], 0)
        self.assertEqual(len(journals(job)), 2)

    def test_a_symlinked_directory_inside_or_outside_the_root_does_not_start(self):
        (self.root / "inner").symlink_to(self.root / "sub", target_is_directory=True)
        (self.root / "outer").symlink_to(self.outside, target_is_directory=True)
        script = f"open({str(self.marker)!r}, 'w')"
        inner, outer, top = self.command("inner", script), self.command("outer", script), self.command(".")
        broker = self.broker([inner, outer, top])
        for command in (inner, outer):
            with self.subTest(cwd=command["cwd"]):
                failed, result = call(broker, "run_command", {"command_id": expected_id(command)})
                self.assertFalse(failed)
                self.assert_not_started(result, command, "symlink")
                self.assertFalse(self.marker.exists())
        failed, result = call(broker, "run_command", {"command_id": expected_id(top)})
        self.assertFalse(failed)
        self.assertEqual(result["exit_code"], 0)

    def test_incomplete_evidence_refuses_later_calls_and_a_directory_start_failure_alone_does_not(self):
        # A repository, so delete_path has tracked files of the task to delete.
        files = ("a.txt", "b.txt", "c.txt")
        for name in files:
            (self.root / name).write_text(name + "\n")
        _git(self.root, "init", "-q", "-b", "main")
        _git(self.root, "add", *files)
        _git(self.root, "-c", "user.name=Test", "-c", "user.email=test@localhost",
             "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
        writes = f"open({str(self.marker)!r}, 'w')"
        absent, gone, sub, top = (self.command("absent"), self.command("gone", writes),
                                  self.command("sub", writes), self.command("."))
        scope = {"task_id": "task", "allowed_paths": list(files)}
        broker = self.broker([absent, gone, sub, top], delete_scope=scope)
        # (c) The listed descriptions name the receipt fields a role reads to decide its next call.
        described = {tool["name"]: tool["description"] for tool in handle({"method": "tools/list"}, broker)["tools"]}
        for field in ("start_failed", "evidence_complete"):
            self.assertIn(field, described["run_command"])
        self.assertIn("evidence_complete", described["delete_path"])
        # (a) A directory failure with complete evidence: later calls run and delete, the same command's once it is corrected.
        failed, result = call(broker, "run_command", {"command_id": expected_id(absent)})
        self.assertFalse(failed)
        self.assert_not_started(result, absent, "missing")
        failed, following = call(broker, "run_command", {"command_id": expected_id(top)})
        self.assertEqual((failed, following.get("exit_code")), (False, 0))
        failed, deletion = call(broker, "delete_path", {"path": "a.txt"})
        self.assertEqual((failed, deletion.get("outcome")), (False, "deleted"))
        self.assertFalse(os.path.lexists(self.root / "a.txt"))
        (self.root / "absent").mkdir()
        failed, following = call(broker, "run_command", {"command_id": expected_id(absent)})
        self.assertFalse(failed)
        self.assertEqual(reported(following), (str(self.root / "absent"), identity(self.root / "absent")))
        # (b) Incomplete evidence from either source refuses every later call of the dispatch.
        real_open, real_write = util.Descriptors.open, util.write_json

        def failing_open(held, path, flags, *, dir_fd=None):
            if path == "sub" and dir_fd is not None:
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            return real_open(held, path, flags, dir_fd=dir_fd)

        def failing_outcome(path, value, *args, **kwargs):
            # The start's intent is written; the outcome of the failed start is not.
            if Path(path).name == "output.outcome.json" and value.get("termination_reason") == "start_failed":
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            return real_write(path, value, *args, **kwargs)

        sources = {"another I/O error opening the cwd": (sub, patch.object(util.Descriptors, "open", failing_open), "error"),
                   "a missing cwd whose outcome is not written": (gone, patch.object(util, "write_json", failing_outcome),
                                                                  "start_failed")}
        for source, (command, fault, reason) in sources.items():
            with self.subTest(source):
                broker = self.broker([absent, gone, sub, top], delete_scope=scope)
                with fault:
                    failed, result = call(broker, "run_command", {"command_id": expected_id(command)})
                # A receipt of an admitted call that did not start, not a tool error.
                self.assertFalse(failed)
                receipt = read_json(Path(result["stdout_path"]).with_name("receipt.json"))
                for record in (result, receipt):
                    self.assertEqual((record["exit_code"], record["termination_reason"], record["evidence_complete"],
                                      record["lifecycle"]["started"]), (None, reason, False, False))
                # Refused before anything starts or is deleted, the unchanged retry included.
                for name, arguments in (("run_command", {"command_id": expected_id(top)}),
                                        ("run_command", {"command_id": expected_id(command)}),
                                        ("delete_path", {"path": "b.txt"})):
                    failed, value = call(broker, name, arguments)
                    self.assertTrue(failed, name)
                    self.assertFalse(value["started"])
                    self.assertIn("evidence incomplete", value["error"])
                    self.assertIn(result["call_id"], value["error"])
                self.assertEqual(len(journals(broker.manifest["job"])), 1)
                self.assertTrue((self.root / "b.txt").is_file())
                self.assertFalse(self.marker.exists())

    def test_a_root_replaced_after_the_manifest_is_written_is_a_tool_error_without_a_journal(self):
        command = self.command(".", f"open({str(self.marker)!r}, 'w')")
        broker = self.broker([command])
        os.rename(self.root, self.base / "root-moved")
        self.root.mkdir()
        failed, value = call(broker, "run_command", {"command_id": expected_id(command)})
        self.assertTrue(failed)
        self.assertFalse(value["started"])
        self.assertIn("not the bound directory", value["error"])
        self.assertEqual(journals(broker.manifest["job"]), [])
        self.assertFalse(self.marker.exists())

    def test_a_root_replaced_after_admission_does_not_redirect_the_child(self):
        command = self.command(".")
        broker = self.broker([command])
        original, moved, real = identity(self.root), self.base / "root-moved", command_server.run_process

        def replaced_then_started(*args, **kwargs):
            os.rename(self.root, moved)
            self.root.mkdir()
            return real(*args, **kwargs)

        with patch("harness.command_server.run_process", side_effect=replaced_then_started):
            failed, result = call(broker, "run_command", {"command_id": expected_id(command)})
        self.assertFalse(failed)
        self.assertEqual(result["exit_code"], 0)
        # The admitted root, now moved aside, not the directory put on its path.
        self.assertEqual(reported(result), (str(moved), original))
        self.assertNotEqual(reported(result)[1], identity(self.root))

    def test_a_receipt_whose_command_is_not_the_manifest_s_refuses_the_next_call(self):
        sub, top = self.command("sub"), self.command(".")
        broker = self.broker([sub, top])
        job = broker.manifest["job"]
        _, result = call(broker, "run_command", {"command_id": expected_id(sub)})
        path = Path(result["stdout_path"]).with_name("receipt.json")
        original = read_json(path)
        for variant, change in (("another directory", lambda receipt: receipt["command"].update(cwd=".")),
                                ("no command", lambda receipt: receipt.pop("command"))):
            with self.subTest(variant):
                tampered = json.loads(json.dumps(original))
                change(tampered)
                write_json(path, tampered)
                failed, value = call(broker, "run_command", {"command_id": expected_id(top)})
                self.assertTrue(failed)
                self.assertFalse(value["started"])
                self.assertIn("evidence incomplete", value["error"])
                self.assertIn(result["call_id"], value["error"])
                self.assertEqual(len(journals(job)), 1)
        # The untouched receipt admits the same call: only its command was refused.
        write_json(path, original)
        failed, value = call(broker, "run_command", {"command_id": expected_id(top)})
        self.assertFalse(failed)
        self.assertEqual(value["exit_code"], 0)

    def test_a_manifest_row_whose_id_is_not_its_command_s_is_refused(self):
        sub, top = self.command("sub"), self.command(".")
        broker = self.broker([sub, top])
        manifest = read_json(broker.path)
        for variant, change in (("another directory", lambda m: m["commands"][0]["command"].update(cwd="other")),
                                ("another argv", lambda m: m["commands"][0]["command"]["argv"].append("extra"))):
            with self.subTest(variant):
                changed = json.loads(json.dumps(manifest))
                change(changed)
                write_json(broker.path, changed)
                with self.assertRaises(HarnessError):
                    Broker(broker.path, hashlib.sha256(broker.path.read_bytes()).hexdigest())
        # The same rewrite without the change loads: only the mismatch was refused.
        write_json(broker.path, manifest)
        reloaded = Broker(broker.path, hashlib.sha256(broker.path.read_bytes()).hexdigest())
        self.assertEqual(reloaded.list_commands(), manifest["commands"])

    def test_a_call_whose_receipt_is_lost_passes_the_exit_gate_and_refuses_the_next_call(self):
        top = self.command(".")
        broker = self.broker([top])
        job = broker.manifest["job"]
        _, result = call(broker, "run_command", {"command_id": expected_id(top)})
        Path(result["stdout_path"]).with_name("receipt.json").unlink()
        # The journal was written as exited before the receipt, so the exit gate has nothing to confirm.
        records = ensure_command_processes_exited(job, state=Path(job).parent)
        self.assertEqual([(r["process_state"], r["exit_confirmed"]) for r in records], [("exited", True)])
        failed, value = call(broker, "run_command", {"command_id": expected_id(top)})
        self.assertTrue(failed)
        self.assertFalse(value["started"])
        self.assertIn("evidence incomplete", value["error"])
        self.assertIn(result["call_id"], value["error"])
        self.assertEqual(len(journals(job)), 1)


class BoundBrokerTests(unittest.TestCase):
    """A run-bound manifest on a real linked worktree; Git is not stubbed."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        repo = self.base / "repo"
        (repo / "sub").mkdir(parents=True)
        # Built apart from the user's Git configuration and hooks, as the smokes build theirs; the checks are real.
        _git(repo, "init", "-q", "-b", "main")
        (repo / "sub" / "tracked.txt").write_text("tracked\n")
        _git(repo, "add", "sub/tracked.txt")
        _git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
             "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
        self.worktree = self.base / "worktrees" / "run"
        _git(repo, "worktree", "add", "-b", "harness/run", str(self.worktree))
        binding = git.capture_worktree_binding(self.worktree, "harness/run")
        self.job, self.marker = self.base / "dispatches" / "worker", self.base / "ran"
        # The ledger of a live worker dispatch, as test_command_server writes it, with the real binding and HEAD.
        run_path = self.base / "state.json"
        write_json(run_path, {"schema_version": RUN_SCHEMA_VERSION, "status": "running", "worktree": binding["worktree"],
                              "expected_head": git.head(self.worktree, binding=binding), "worktree_binding": binding,
                              "dispatches": [{"id": self.job.name, "role": "worker", "kind": "implement",
                                              "status": "running", "invoked": True,
                                              "command_directory": str(self.job / "commands")}]})
        self.report = {"cwd": "sub", "argv": [sys.executable, "-c", REPORT]}
        self.writes = {"cwd": "sub", "argv": [sys.executable, "-c", f"open({str(self.marker)!r}, 'w')"]}
        config = {"verification": {"commands": []}, "permissions": {"worker_commands": [self.report, self.writes]},
                  "runtime": {"verify_timeout_seconds": 30, "agent_timeout_seconds": 120}}
        metadata = create_manifest(config, self.worktree, self.job, run_path)
        self.broker = Broker(metadata["artifact"], metadata["artifact_digest"])
        self.assertEqual(self.broker.manifest["binding"], binding)

    def test_a_subdirectory_command_starts_below_the_checked_root(self):
        failed, result = call(self.broker, "run_command", {"command_id": expected_id(self.report)})
        self.assertFalse(failed)
        self.assertEqual(result["exit_code"], 0)
        directory = self.worktree / "sub"
        self.assertEqual(reported(result), (str(directory), identity(directory)))
        receipt = read_json(Path(result["stdout_path"]).with_name("receipt.json"))
        self.assertEqual((result["command"], receipt["command"]), (self.report, self.report))

    def test_an_unbound_directory_on_the_path_as_the_root_opens_is_a_tool_error_without_a_journal(self):
        path = self.worktree
        moved, parked = path.with_name("run-moved"), path.with_name("run-impostor")
        real_open, real_check, refusals = git.open_bound_root, git._check_binding, []

        def bound_only_during_check(*args):
            # The path-based part of the check sees the bound worktree.
            os.rename(path, parked)
            os.rename(moved, path)
            try:
                return real_check(*args)
            finally:
                os.rename(path, moved)
                os.rename(parked, path)

        def impostor_as_the_root_opens(held, worktree, binding):
            # Only for this call: an unbound directory A, with a sub/ of its own, is on the path.
            os.rename(path, moved)
            (path / "sub").mkdir(parents=True)
            try:
                with patch("harness.git._check_binding", side_effect=bound_only_during_check):
                    return real_open(held, worktree, binding)
            except HarnessError as error:
                refusals.append(error.stop)
                raise
            finally:
                os.rmdir(path / "sub")
                os.rmdir(path)
                os.rename(moved, path)

        with patch("harness.git.open_bound_root", side_effect=impostor_as_the_root_opens):
            failed, value = call(self.broker, "run_command", {"command_id": expected_id(self.writes)})
        self.assertTrue(failed)
        self.assertFalse(value["started"])
        # The binding check on the opened root refused it, not only the manifest's identity.
        self.assertEqual([(stop["kind"], stop["reason"]) for stop in refusals],
                         [("workspace_mismatch", "binding_changed")])
        self.assertEqual(journals(self.job), [])
        self.assertFalse(self.marker.exists())
        # With the bound worktree on the path the same call is admitted and runs.
        failed, result = call(self.broker, "run_command", {"command_id": expected_id(self.writes)})
        self.assertFalse(failed)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
