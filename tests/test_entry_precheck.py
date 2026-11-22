"""Entry refusals and the first writes of the offline CLI."""
from copy import deepcopy
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import tomllib
from unittest.mock import patch

from harness import git, state_files, versions, workspace_input_names, workspace_inputs
from harness.util import HarnessError, read_json, write_json
from test_dashboard import tree_snapshot
from test_harness import ROOT, cli_notice, crash_after_save, write_project_config
from test_memory_pipeline import ProposedKnowledge
from test_project_config_gate import GateCase
from test_provider_failures import FifoWatchdog
from test_state_files import FakeOs, failing


class EntryPrecheckTests(GateCase):
    def setUp(self):
        super().setUp()
        self.repo = self.repo.resolve()
        self.state = self.repo / ".harness"
        self.manifest_path = self.outside / "manifest.json"

    def refuse(self, argv, expected, code):
        actual, output, errors = self.invoke(*argv)
        self.assertEqual((actual, output), (code, ""), errors)
        stop = cli_notice(errors)
        self.assertIsNotNone(stop, errors)
        self.assertEqual((stop["kind"], stop["reason"]), expected, errors)
        return stop

    def run_entries(self, ident):
        return (
            ("abandon", ident),
            ("prepare-run", ident, "--manifest", str(self.manifest_path)),
            ("cleanup-run", ident),
            ("cleanup-run", ident, "--remove"),
        )

    def assert_precheck(self, ident, kind, reason, *, data=None, artifact=None):
        before = self.footprint()
        for argv in self.run_entries(ident):
            with self.subTest(argv=argv):
                entry_reason = "load_integrity" if kind == "evidence" and argv[0] == "abandon" else reason
                code = 2 if kind in ("invalid_input", "unsupported_ledger") and argv[0] != "cleanup-run" else 1
                stop = self.refuse(argv, (kind, entry_reason), code)
                if data is not None:
                    self.assertEqual(stop["data"], data)
                if artifact is not None:
                    self.assertEqual(stop["source"]["artifact"], str(artifact))
                self.assertEqual(self.footprint(), before)
                self.assertEqual(self.engines, [])

    def test_bad_id_and_missing_run_do_not_initialize_private_state(self):
        self.assertFalse(self.state.exists())
        for ident in ("run_missing", "bad-id"):
            self.assert_precheck(ident, "invalid_input", "cli_arguments", data={"option": "run_id"})
        (self.state / "runs" / "run_empty").mkdir(parents=True)
        self.assert_precheck("run_empty", "invalid_input", "cli_arguments", data={"option": "run_id"})

    def test_invalid_memory_arguments_are_refused_before_source_binding_and_writes(self):
        before = self.footprint()
        for ttl in ("-1", "1e308", "nan", "inf"):
            with self.subTest(ttl=ttl), patch("harness.cli.bind_source_files") as bind:
                stop = self.refuse(("memory", "add", "--key", "fact", "--title", "Fact", "--text", "Details",
                                    "--source", "operator", "--ttl-days", ttl), ("invalid_input", "cli_arguments"), 2)
                self.assertEqual(stop["data"], {"option": "--ttl-days"})
                bind.assert_not_called()
        self.assertEqual(self.footprint(), before)

    def test_explicit_invalid_memory_run_is_refused_without_widening_scope(self):
        before = self.footprint()
        self.assertFalse(self.state.exists())
        for run_id in ("", "invalid/id", "run_\udcff"):
            with self.subTest(run_id=run_id), patch("harness.cli.bind_source_files") as bind:
                stop = self.refuse(("memory", "add", "--key", "fact", "--title", "Fact",
                                    "--text", "Details", "--run", run_id), ("invalid_input", "cli_arguments"), 2)
                self.assertEqual(stop["data"], {"option": "--run"})
                bind.assert_not_called()
        self.assertEqual(self.footprint(), before)

    def test_non_utf8_memory_fields_are_refused_before_source_binding_and_writes(self):
        before = self.footprint()
        for option in ("--key", "--title", "--text", "--source"):
            arguments = {"--key": "fact", "--title": "Fact", "--text": "Details", "--source": "operator"}
            arguments[option] = os.fsdecode(b"value-\xff")
            argv = [value for pair in arguments.items() for value in pair]
            with self.subTest(option=option), patch("harness.cli.bind_source_files") as bind:
                stop = self.refuse(("memory", "add", *argv), ("invalid_input", "cli_arguments"), 2)
                self.assertEqual(stop["data"], {"option": option})
                bind.assert_not_called()
                self.assertEqual(self.footprint(), before)

    def test_memory_add_preserves_unicode_text(self):
        fields = {"key": "事实", "title": "标题", "text": "詳細 📚", "source": "操作者"}
        argv = [value for name, text in fields.items() for value in (f"--{name}", text)]
        code, output, errors = self.invoke("memory", "add", *argv, "--ttl-days", "1e300")
        self.assertEqual(code, 0, errors)
        code, record_json, errors = self.invoke("memory", "read", output.strip())
        self.assertEqual(code, 0, errors)
        record = json.loads(record_json)
        self.assertEqual((record["key"], record["abstract"], record["detail"], record["source"]),
                         tuple(fields.values()))
        code, exported, errors = self.invoke("memory", "export")
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(exported)[0]["expires"], record["expires"])

    def test_resume_reports_legacy_task_memory_encoding_error(self):
        self.assert_legacy_memory_stop("summary")

    def test_resume_reports_legacy_approved_knowledge_encoding_error(self):
        self.assert_legacy_memory_stop("knowledge")

    def test_unencodable_review_summary_stops_before_acceptance_commit(self):
        self.assert_memory_preflight_stop("summary")

    def test_unencodable_approved_knowledge_stops_before_acceptance_commit(self):
        self.assert_memory_preflight_stop("knowledge")

    def test_unencodable_unapproved_knowledge_is_never_written(self):
        backend = self.memory_backend("knowledge")
        backend.approve_memory = False
        engine = self.engine(backend)
        result = engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual([record["kind"] for record in engine.memory.export()], ["accepted_task"])

    def memory_backend(self, invalid):
        class Backend(ProposedKnowledge):
            def invoke(self, **kwargs):
                result = super().invoke(**kwargs)
                if invalid == "summary" and kwargs["kind"] == "review":
                    result["summary"] += "\ud800"
                if invalid == "knowledge" and kwargs["role"] == "worker":
                    result["proposed_memories"][0]["content"] += "\ud800"
                return result

        return Backend()

    def assert_memory_preflight_stop(self, invalid):
        engine = self.engine(self.memory_backend(invalid))
        with self.assertRaises(HarnessError) as raised:
            engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        stop = raised.exception.stop
        self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "invariant"))
        self.assertIn("无法编码为 UTF-8", str(raised.exception))
        saved = read_json(engine.directory / "state.json")
        self.assertEqual(git.head(engine.worktree), saved["base_commit"])
        self.assertEqual(saved["accepted"], {})
        self.assertNotIn("pending_post_accept", saved)
        self.assertEqual(engine.memory.export(), [])
        pending = saved["pending_acceptance"]
        self.assertEqual(read_json(Path(pending["review_artifact"]))["review"], pending["review"])

    def assert_legacy_memory_stop(self, invalid):
        write_project_config(self.repo)
        engine = self.engine(self.memory_backend(invalid))
        crash, state = crash_after_save(engine, lambda run: "pending_post_accept" in run)
        with crash, patch("harness.task_lifecycle.Memory.validate_record"), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        self.assertTrue(state["fired"])
        ledger = engine.directory / "state.json"
        saved = read_json(ledger)
        review_artifact = Path(saved["accepted"]["greet"]["review_artifact"])
        review_bytes = review_artifact.read_bytes()
        evidence = tree_snapshot(engine.directory / "dispatches")
        calls = self.backend.calls.copy()

        stop = self.refuse(("resume", saved["id"], "--operation-id", "legacy-task-memory"),
                           ("runtime_failure", "invariant"), 1)
        self.assertEqual(stop["data"], {})
        self.assertEqual(stop["exits"], [])
        record = read_json(self.state / "operations" / "legacy-task-memory.json")
        self.assertEqual(record["exit_status"], 1)
        self.assertIn("无法编码为 UTF-8", record["stop"]["detail"])
        self.assertEqual({key: value for key, value in record["stop"].items() if key != "detail"}, stop)
        persisted = read_json(ledger)
        completion = persisted["pending_post_accept"]["completion"].copy()
        if invalid == "knowledge":
            self.assertEqual([row["id"] for row in engine.memory.export()], [completion.pop("task_memory_id")])
        else:
            self.assertEqual(engine.memory.export(), [])
        self.assertEqual(completion, saved["pending_post_accept"]["completion"])
        self.assertEqual(persisted["accepted"], saved["accepted"])
        self.assertEqual(git.head(engine.worktree), saved["expected_head"])
        self.assertEqual(review_artifact.read_bytes(), review_bytes)
        self.assertEqual(tree_snapshot(engine.directory / "dispatches"), evidence)
        self.assertEqual(self.backend.calls, calls)

    def operator_with_servers(self, servers):
        mcp = self.outside / "mcp.json"
        write_json(mcp, {"servers": servers})
        config = self.outside / "harness.toml"
        original = (ROOT / "harness.toml").read_text(encoding="utf-8")
        config.write_text(original.replace('"shared/mcp.json"', '"mcp.json"')
                          .replace('"shared/RULES.md"', json.dumps(str(ROOT / "shared" / "RULES.md")))
                          .replace('"shared/skills"', json.dumps(str(ROOT / "shared" / "skills"))), encoding="utf-8")
        return config, mcp

    def sync_with_servers(self, servers, role="worker"):
        config, _ = self.operator_with_servers(servers)
        return self.invoke("sync", "--role", role, config=config)

    @staticmethod
    def unencodable_mcp_fields():
        stdio = {"transport": "stdio", "command": "example-mcp"}
        http = {"transport": "http", "url": "https://example.test/mcp"}
        bad = "value-\ud800"
        return {
            "command": {**stdio, "command": bad},
            "args": {**stdio, "args": [bad]},
            "env_vars": {**stdio, "env_vars": [bad]},
            "url": {**http, "url": bad},
            "bearer_token_env_var": {**http, "bearer_token_env_var": bad},
        }

    def test_invalid_shared_mcp_is_refused_before_creating_exclusions_or_state(self):
        before = self.footprint()
        specs = {"missing_command": {"transport": "stdio"}, **self.unencodable_mcp_fields()}
        for role in ("commander", "worker", "reviewer"):
            for field, spec in specs.items():
                with self.subTest(role=role, field=field):
                    code, output, errors = self.sync_with_servers(
                        {"invalid": dict(spec, roles=[role], read_only=True)}, role)
                    self.assertEqual((code, output), (2, ""), errors)
                    stop = cli_notice(errors)
                    self.assertEqual((stop["kind"], stop["reason"], stop["data"]),
                                     ("invalid_input", "input_schema", {"option": "config"}))
                    self.assertEqual(self.footprint(), before)

    def test_sync_preserves_unicode_arguments_in_both_generated_configs(self):
        args = ["中文", "📚", "", "control\x7f", r"literal\u007f"]
        code, _, errors = self.sync_with_servers({"example": {"transport": "stdio", "command": "example-mcp", "args": args}})
        self.assertEqual(code, 0, errors)
        folder = self.state / "generated" / "worker"
        self.assertEqual(read_json(folder / "claude.mcp.json")["mcpServers"]["example"]["args"], args)
        with (folder / "codex.mcp.toml").open("rb") as stream:
            self.assertEqual(tomllib.load(stream)["mcp_servers"]["example"]["args"], args)

    def test_run_audits_bad_shared_mcp_without_creating_run_or_dispatch(self):
        write_project_config(self.repo)
        for field, spec in self.unencodable_mcp_fields().items():
            with self.subTest(field=field):
                config, _ = self.operator_with_servers({"svc": dict(spec, roles=["reviewer"], read_only=True)})
                operation = f"bad-run-mcp-{field}"
                result = self.invoke("run", "Implement greet", "--operation-id", operation, config=config)
                self.assert_mcp_refusal(result, operation, field)
                self.assertFalse((self.state / "runs").exists())
                self.assertEqual(self.backend.calls, [])

    def assert_mcp_refusal(self, result, operation, field):
        code, output, errors = result
        self.assertEqual((code, output), (2, ""), errors)
        stop = cli_notice(errors)
        self.assertEqual((stop["kind"], stop["reason"], stop["data"]),
                         ("invalid_input", "input_schema", {"option": "config"}))
        self.assertIn(f"servers.svc.{field}", errors)
        record = read_json(self.state / "operations" / f"{operation}.json")
        self.assertEqual(record["exit_status"], 2)
        self.assertEqual({key: value for key, value in record["stop"].items() if key != "detail"}, stop)

    def test_resume_refuses_bad_shared_mcp_before_reviewer_invocation_and_budget(self):
        write_project_config(self.repo)
        for field, spec in self.unencodable_mcp_fields().items():
            with self.subTest(field=field):
                config, _ = self.operator_with_servers({"svc": dict(spec, roles=["reviewer"], read_only=True)})
                # Older releases could record this asset before any model call.
                with patch("harness.shared._require_utf8"):
                    code, output, errors = self.invoke("run", "Implement greet", "--pause-after-plan",
                                                       "--plan", str(ROOT / "examples/plan.json"), config=config)
                self.assertEqual(code, 0, errors)
                ident = json.loads(output)["id"]
                calls = self.backend.calls.copy()
                operation = f"bad-resume-mcp-{field}"
                result = self.invoke("resume", ident, "--approve-plan", "--operation-id", operation, config=config)
                self.assert_mcp_refusal(result, operation, field)
                saved = read_json(self.state / "runs" / ident / "state.json")
                self.assertEqual(saved["agent_dispatches"], 1)
                self.assertEqual(saved["attempts"], {"greet": 1})
                dispatch = saved["dispatches"][-1]
                self.assertEqual((dispatch["role"], dispatch["status"], dispatch["invoked"]),
                                 ("reviewer", "failed", False))
                self.assertEqual(self.backend.calls, calls + ["worker"])

    def make_ledger(self):
        """Start a real paused ledger, then simulate missing local controls and locks."""
        exclude = self.common / "info" / "exclude"
        previous = exclude.read_bytes() if exclude.exists() else None
        ident, ledger = self.paused_run()
        for path in (*self.state.glob("memory.sqlite3*"), self.state / ".gitignore",
                     self.common / "harness-private-state.lock",
                     ledger.parent / "run.lock", ledger.parent / "store.lock"):
            path.unlink(missing_ok=True)
        if previous is None:
            exclude.unlink()
        else:
            exclude.write_bytes(previous)
        return ident, ledger

    def test_precheck_refuses_ledger_and_run_directory_links_and_fifo(self):
        ident, ledger = self.make_ledger()
        raw = ledger.read_bytes()
        copy = self.outside / "ledger-copy" / "state.json"
        copy.parent.mkdir()
        copy.write_bytes(raw)
        ledger.unlink()
        before = tree_snapshot(copy.parent)
        ledger.symlink_to(copy.resolve())
        self.assert_precheck(ident, "evidence", "runtime_integrity", artifact=ledger)
        ledger.unlink()
        os.mkfifo(ledger)
        watchdog = FifoWatchdog(ledger, bound=5)
        try:
            self.assert_precheck(ident, "evidence", "runtime_integrity", artifact=ledger)
        finally:
            self.assertFalse(watchdog.stop(), "precheck waited on a FIFO without a writer")
            ledger.unlink()
        moved = self.outside / "run-copy"
        ledger.parent.rename(moved)
        (moved / ledger.name).write_bytes(raw)
        ledger.parent.symlink_to(moved, target_is_directory=True)
        copied = tree_snapshot(moved)
        self.assert_precheck(ident, "evidence", "runtime_integrity", artifact=ledger)
        self.assertEqual(tree_snapshot(moved), copied)
        self.assertEqual(tree_snapshot(copy.parent), before)

    def test_precheck_distinguishes_bad_json_and_unsupported_ledger(self):
        ident, ledger = self.make_ledger()
        for raw, kind, reason in (
            (b"{", "evidence", "runtime_integrity"),
            (b"\xff", "evidence", "runtime_integrity"),
            (b"[]", "unsupported_ledger", "ledger_version"),
            (b'{"schema_version": 18}', "unsupported_ledger", "ledger_version"),
        ):
            with self.subTest(raw=raw):
                ledger.write_bytes(raw)
                self.assert_precheck(ident, kind, reason, artifact=ledger if kind == "evidence" else None)

    def test_precheck_preserves_local_io_types(self):
        ident, ledger = self.make_ledger()
        for code, error_type in ((errno.EACCES, "PermissionError"), (errno.EIO, "OSError")):
            with self.subTest(errno=code), patch.object(state_files, "os", FakeOs(open=failing(code, ledger.name))):
                self.assert_precheck(ident, "runtime_failure", "io", data={"error_type": error_type})

    def test_precheck_maps_an_over_limit_outcome_as_evidence(self):
        ident, ledger = self.make_ledger()
        # Runtime ledger reads are unbounded. Exercise the outcome contract without adding a size cap.
        result = state_files.Outcome("too_large", str(ledger), state_files.Step(2, ledger.name, "read", True))
        with patch.object(versions.StateFiles, "read", return_value=result):
            self.assert_precheck(ident, "evidence", "runtime_integrity", artifact=ledger)

    def test_resume_option_conflict_still_leaves_a_finished_audit_record(self):
        ident, ledger = self.paused_run()
        before = ledger.read_bytes()
        write_project_config(self.repo)
        stop = self.refuse(("resume", ident, "--operator-decision", "decision.json",
                            "--environment-update", "update.json", "--operation-id", "incompatible"),
                           ("invalid_input", "incompatible_options"), 2)
        record = read_json(self.state / "operations" / "incompatible.json")
        self.assertEqual((record["kind"], record["status"], record["exit_status"], record["run_id"]),
                         ("resume", "finished", 2, ident))
        self.assertEqual(record["stop"], stop)
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(self.backend.calls, [])


class ManifestEntryTests(GateCase):
    def create_manifest(self):
        self.ident, self.ledger = self.paused_run()
        self.run = read_json(self.ledger)
        self.checkout = self.repo.resolve() / ".harness" / "worktrees" / self.ident
        self.source = self.repo / "dependency.txt"
        self.source.write_bytes(b"test dependency")
        self.source.chmod(0o644)
        self.manifest = {
            "schema_version": 1, "run_id": self.ident, "head": self.run["expected_head"],
            "source_root": ".", "max_files": 2, "max_bytes": 64,
            "files": [{"source": self.source.name, "destination": "__pycache__/input.txt",
                       "type": "file", "ownership": "input_copy", "mode": 0o644,
                       "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest()}],
        }
        self.manifest_path = self.outside.resolve() / "manifest.json"
        self.locks = [self.ledger.parent / name for name in ("run.lock", "store.lock")]
        self.clear_locks()

    def clear_locks(self):
        for path in self.locks:
            path.unlink(missing_ok=True)

    def refuse_manifest(self, *, locked):
        before = self.ledger.read_bytes()
        code, output, errors = self.invoke("prepare-run", self.ident, "--manifest", str(self.manifest_path))
        self.assertEqual((code, output), (2, ""), errors)
        stop = cli_notice(errors)
        self.assertEqual((stop["kind"], stop["reason"], stop["data"]["option"]),
                         ("invalid_input", "input_schema", "manifest"), errors)
        self.assertEqual([path.is_file() for path in self.locks], [locked, locked])
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertFalse((self.ledger.parent / "workspace-inputs-manifest.json").exists())
        return errors

    def test_manifest_parse_and_shape_refusals_precede_lock_creation(self):
        self.create_manifest()
        for raw in (b"{broken", b"{}", b"[]"):
            with self.subTest(raw=raw):
                self.manifest_path.write_bytes(raw)
                self.refuse_manifest(locked=False)
        for field, invalid in (
            ("run_id", ["run_other"]), ("schema_version", [2]), ("source_root", ["elsewhere"]),
            ("head", [None, "main", "A" * 40]), ("max_files", [0, True]), ("max_bytes", [-1, "64"]),
            ("files", [[], [None], self.manifest["files"] * 3]),
            ("file.sha256", [None, 123, "A" * 64, "a" * 40]),
            ("file.mode", [0o666]), ("file.type", ["directory"]), ("file.ownership", ["other"]),
            ("file.source", ["../dependency.txt", "data/CREDENTIALS.json", "data/local.key"]),
            ("file.destination", ["/absolute", ".harness/input.txt"]),
        ):
            for value in invalid:
                with self.subTest(field=field, value=value):
                    manifest = deepcopy(self.manifest)
                    if field.startswith("file."):
                        manifest["files"][0][field.removeprefix("file.")] = value
                    else:
                        manifest[field] = value
                    write_json(self.manifest_path, manifest)
                    self.refuse_manifest(locked=False)
        for field, unique in (("source", "__pycache__/second.txt"), ("destination", "second-source.txt")):
            with self.subTest(duplicate=field):
                manifest = deepcopy(self.manifest)
                other = "destination" if field == "source" else "source"
                manifest["files"].append({**manifest["files"][0], other: unique})
                write_json(self.manifest_path, manifest)
                self.refuse_manifest(locked=False)

    def test_shape_refusal_precedes_locked_run_state_gate(self):
        self.create_manifest()
        write_json(self.ledger, {**self.run, "status": "running"})
        write_json(self.manifest_path, {**self.manifest, "head": None})
        self.refuse_manifest(locked=False)

    def test_unencodable_manifest_paths_are_refused_before_any_writes(self):
        self.create_manifest()
        before = self.footprint()
        for field in ("source", "destination"):
            for bad in ("\ud800", "\0"):
                with self.subTest(field=field, bad=bad):
                    manifest = deepcopy(self.manifest)
                    manifest["files"][0][field] += bad
                    write_json(self.manifest_path, manifest)
                    self.refuse_manifest(locked=False)
                    self.assertEqual(self.footprint(), before)
                    with self.assertRaises(HarnessError) as raised:
                        workspace_inputs.prepare_run(self.repo, self.ident, manifest)
                    stop = raised.exception.stop
                    self.assertEqual((stop["kind"], stop["reason"], stop["data"]),
                                     ("invalid_input", "input_schema", {"option": "manifest"}))
                    self.assertEqual(self.footprint(), before)

    def test_source_file_ancestors_are_refused_before_any_writes(self):
        self.create_manifest()
        first = self.manifest["files"][0]
        second = {**first, "source": first["source"] + "/child", "destination": "__pycache__/other.txt"}
        before = self.footprint()
        for entries in ([first, second], [second, first]):
            write_json(self.manifest_path, {**self.manifest, "files": entries})
            self.refuse_manifest(locked=False)
            self.assertEqual(self.footprint(), before)

    def test_file_destinations_that_overlap_are_refused_before_manifest_is_frozen(self):
        self.create_manifest()
        second_source = self.repo / "other.txt"
        second_source.write_bytes(b"another dependency")
        second_source.chmod(0o644)
        parent = {**self.manifest["files"][0], "destination": "__pycache__/package"}
        child = {**parent, "source": second_source.name, "destination": "__pycache__/package/entry.txt",
                 "sha256": hashlib.sha256(second_source.read_bytes()).hexdigest()}
        before = self.footprint()
        for entries in ([parent, child], [child, parent]):
            with self.subTest(entries=[entry["destination"] for entry in entries]):
                write_json(self.manifest_path, {**self.manifest, "files": entries})
                self.refuse_manifest(locked=False)
                self.assertEqual(self.footprint(), before)

        child["destination"] = "__pycache__/package-other/entry.txt"
        write_json(self.manifest_path, {**self.manifest, "files": [parent, child]})
        code, _, errors = self.invoke("prepare-run", self.ident, "--manifest", str(self.manifest_path))
        self.assertEqual(code, 0, errors)
        for entry in (parent, child):
            self.assertEqual((self.checkout / entry["destination"]).read_bytes(),
                             (self.repo / entry["source"]).read_bytes())

    def test_target_case_aliases_follow_actual_volume(self):
        self.assert_volume_ancestor_layout("Package", "package")

    def test_target_normalization_aliases_follow_actual_volume(self):
        self.assert_volume_ancestor_layout("café", "cafe\u0301")

    def test_target_casefold_equivalence_does_not_override_actual_volume(self):
        self.assert_volume_ancestor_layout("Straße", "STRASSE")

    def test_single_name_rejected_by_apfs_creation_leaves_manifest_unbound(self):
        self.create_manifest()
        name = "x" + "\u0301" * 85
        with tempfile.TemporaryDirectory(dir=self.checkout) as folder:
            with workspace_input_names._open_directory(folder) as fd:
                if not workspace_input_names._name_rules(fd).startswith("apfs_"):
                    self.skipTest("requires an APFS volume")
                with self.assertRaises(FileNotFoundError):
                    os.stat(name, dir_fd=fd, follow_symlinks=False)
                try:
                    os.close(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd))
                except OSError as error:
                    if error.errno in (errno.EPERM, errno.EACCES):
                        self.skipTest("temporary root policy masks the APFS creation error")
                    self.assertEqual(error.errno, errno.EILSEQ)
                else:
                    self.skipTest("this APFS volume accepts the combining sequence")

        entry = self.manifest["files"][0]
        for destination in (f"__pycache__/{name}", f"__pycache__/{name}/child"):
            with self.subTest(destination=destination):
                entry["destination"] = destination
                write_json(self.manifest_path, self.manifest)
                detail = self.refuse_manifest(locked=True)
                self.assertIn("invalid destination on this volume", detail)
                self.assertFalse((self.ledger.parent / "workspace-inputs.json").exists())
                self.assertEqual(list(self.ledger.parent.glob("input-names-*")), [])
                self.assertFalse((self.checkout / "__pycache__").exists())

        entry["destination"] = "__pycache__/é"
        write_json(self.manifest_path, self.manifest)
        code, _, errors = self.invoke("prepare-run", self.ident, "--manifest", str(self.manifest_path))
        self.assertEqual(code, 0, errors)
        self.assertEqual((self.checkout / entry["destination"]).read_bytes(), self.source.read_bytes())
        journal = read_json(self.ledger.parent / "workspace-inputs.json")
        self.assertEqual(journal["destination_name_observations"][0]["comparison"], "private_apfs_volume")
        self.assertEqual(list(self.ledger.parent.glob("input-names-*")), [])

    def assert_volume_ancestor_layout(self, file_name, alias):
        self.create_manifest()
        second = self.repo / "other.txt"
        second.write_bytes(b"another dependency")
        second.chmod(0o644)
        first = {**self.manifest["files"][0], "destination": f"__pycache__/{file_name}"}
        child = {**first, "source": second.name, "destination": f"__pycache__/{alias}/child.txt",
                 "sha256": hashlib.sha256(second.read_bytes()).hexdigest()}
        with tempfile.TemporaryDirectory(dir=self.checkout) as folder:
            probe = Path(folder)
            (probe / file_name).touch()
            aliases = (probe / alias).exists()
        from harness import workspace_input_names
        fd = os.open(self.checkout, os.O_RDONLY | os.O_DIRECTORY)
        try:
            observed = workspace_input_names._name_rules(fd) != "unobserved"
        finally:
            os.close(fd)
        if aliases or not observed:
            for entries in ([first, child], [child, first]):
                write_json(self.manifest_path, {**self.manifest, "files": entries})
                self.refuse_manifest(locked=True)
                self.assertFalse((self.ledger.parent / "workspace-inputs.json").exists())
                self.assertFalse((self.checkout / "__pycache__").exists())
                self.assertEqual(list(self.checkout.glob(".harness-input-probe-*")), [])
            # Directory aliases can share a parent when their leaves are distinct.
            first["destination"] += "/other.txt"
        write_json(self.manifest_path, {**self.manifest, "files": [first, child]})
        code, _, errors = self.invoke("prepare-run", self.ident, "--manifest", str(self.manifest_path))
        self.assertEqual(code, 0, errors)
        for entry in (first, child):
            self.assertEqual((self.checkout / entry["destination"]).read_bytes(),
                             (self.repo / entry["source"]).read_bytes())

    def test_destination_bytes_follow_actual_volume(self):
        self.create_manifest()
        raw_name = os.fsdecode(b"input-\xff")
        self.manifest["files"][0]["destination"] = "__pycache__/" + raw_name
        with tempfile.TemporaryDirectory(dir=self.checkout) as folder:
            probe = Path(folder)
            (probe / "source").touch()
            try:
                os.link(probe / "source", probe / raw_name)
            except OSError as error:
                self.assertIn(error.errno, (errno.EILSEQ, errno.EINVAL, errno.EPERM, errno.EACCES))
                supports_name = False
            else:
                supports_name = True
        write_json(self.manifest_path, self.manifest)
        if not supports_name:
            self.refuse_manifest(locked=True)
            self.assertFalse((self.ledger.parent / "workspace-inputs.json").exists())
            self.assertFalse((self.checkout / "__pycache__").exists())
            self.assertEqual(list(self.checkout.glob(".harness-input-probe-*")), [])
            self.manifest["files"][0]["destination"] = "__pycache__/input-ok"
        else:
            raw_source = self.repo / os.fsdecode(b"source-\xff")
            self.source.rename(raw_source)
            self.source = raw_source
            self.manifest["files"][0]["source"] = raw_source.name
        write_json(self.manifest_path, self.manifest)
        code, _, errors = self.invoke("prepare-run", self.ident, "--manifest", str(self.manifest_path))
        self.assertEqual(code, 0, errors)
        self.assertEqual((self.checkout / self.manifest["files"][0]["destination"]).read_bytes(),
                         self.source.read_bytes())

    def test_layout_read_failure_leaves_manifest_unbound_and_retryable(self):
        from harness import workspace_input_names
        self.create_manifest()
        write_json(self.manifest_path, self.manifest)
        before = self.ledger.read_bytes()
        argv = ("prepare-run", self.ident, "--manifest", str(self.manifest_path))
        with patch.object(workspace_input_names, "_name_rules",
                          side_effect=OSError(errno.EIO, "injected directory query failure")):
            code, _, errors = self.invoke(*argv)
        self.assertEqual(code, 1, errors)
        stop = cli_notice(errors)
        self.assertEqual((stop["kind"], stop["reason"]), ("runtime_failure", "io"))
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertFalse((self.ledger.parent / "workspace-inputs.json").exists())
        self.assertFalse((self.checkout / "__pycache__").exists())
        code, _, errors = self.invoke(*argv)
        self.assertEqual(code, 0, errors)

    def test_unobserved_layout_rejects_aliases_and_leaves_existing_file_untouched(self):
        from harness import workspace_input_names
        self.create_manifest()
        existing = self.checkout / "__pycache__" / "Input"
        existing.parent.mkdir()
        existing.write_text("unowned")
        self.manifest["files"][0]["destination"] = "__pycache__/input/child"
        write_json(self.manifest_path, self.manifest)
        with patch.object(workspace_input_names, "_name_rules", return_value="unobserved"):
            code, output, errors = self.invoke("prepare-run", self.ident, "--manifest", str(self.manifest_path))
            self.assertEqual((code, output), (2, ""), errors)
            detail = errors
            if existing.with_name("input").exists():
                self.assertIn("destination parent is not a directory", detail)
            else:
                self.assertIn("naming semantics are unobserved", detail)
            self.assertEqual(existing.read_text(), "unowned")
            self.assertIsNone(read_json(self.ledger)["workspace_inputs"])
            self.assertFalse((self.ledger.parent / "workspace-inputs.json").exists())
            self.manifest["files"][0]["destination"] = "__pycache__/independent"
            write_json(self.manifest_path, self.manifest)
            code, _, errors = self.invoke("prepare-run", self.ident, "--manifest", str(self.manifest_path))
        self.assertEqual(code, 0, errors)
        journal = read_json(self.ledger.parent / "workspace-inputs.json")
        self.assertEqual(journal["destination_name_observations"], [{
            "parent": "__pycache__", "destinations": ["__pycache__/independent"],
            "target_rule": "unobserved", "comparison": "unobserved",
            "target_directory": {"device": existing.parent.stat().st_dev,
                                 "inode": existing.parent.stat().st_ino},
        }])

    def test_unobserved_directory_alias_allows_disjoint_children(self):
        self.create_manifest()
        existing = self.checkout / "__pycache__" / "Package"
        existing.mkdir(parents=True)
        (existing / "old").write_text("retained")
        self.manifest["files"][0]["destination"] = "__pycache__/package/child"
        write_json(self.manifest_path, self.manifest)
        with patch.object(workspace_input_names, "_name_rules", return_value="unobserved"):
            code, _, errors = self.invoke("prepare-run", self.ident, "--manifest", str(self.manifest_path))
        self.assertEqual(code, 0, errors)
        self.assertEqual((existing.with_name("package") / "child").read_bytes(), self.source.read_bytes())
        self.assertEqual((existing / "old").read_text(), "retained")

    def test_cross_volume_apfs_case_pair_stops_before_freezing(self):
        self.create_manifest()
        with workspace_input_names._open_directory(self.checkout) as fd:
            if workspace_input_names._name_rules(fd) != "apfs_insensitive":
                self.skipTest("requires case-insensitive APFS")
        device = self.checkout.stat().st_dev
        other_volume = next((path for path in (Path("/"), Path("/System/Volumes/Preboot"))
                             if path.is_dir() and path.stat().st_dev != device), None)
        if other_volume is None:
            self.skipTest("requires a second visible volume")
        with tempfile.TemporaryDirectory(dir=self.checkout) as folder:
            fixture = Path(folder)
            (fixture / "ɤ").touch()
            if not (fixture / "Ɤ").exists():
                self.skipTest("this APFS volume distinguishes the Unicode case pair")

        first = self.manifest["files"][0]
        first["destination"] = "__pycache__/ɤ"
        other = self.source.with_name("second-dependency.txt")
        other.write_bytes(self.source.read_bytes())
        other.chmod(0o644)
        second = {**first, "source": other.name, "destination": "__pycache__/Ɤ/child"}
        self.manifest["files"].append(second)
        checker = workspace_input_names.check_destination_layout

        def without_same_volume_scratch(destinations, checkout, private_root, temp_name, *, run_id):
            return checker(destinations, checkout, other_volume, temp_name, run_id=run_id)

        with patch.object(workspace_inputs, "check_destination_layout", without_same_volume_scratch), \
                patch.object(workspace_input_names, "_private_cache_fd") as scratch:
            for entries in ([first, second], [second, first]):
                self.manifest["files"] = entries
                write_json(self.manifest_path, self.manifest)
                self.assertIn("naming semantics are unobserved", self.refuse_manifest(locked=True))
                self.assertFalse((self.ledger.parent / "workspace-inputs.json").exists())
                self.assertFalse((self.checkout / "__pycache__").exists())
            second["destination"] = "__pycache__/Ɤother/child"
            write_json(self.manifest_path, self.manifest)
            code, _, errors = self.invoke("prepare-run", self.ident, "--manifest", str(self.manifest_path))
            scratch.assert_not_called()
        self.assertEqual(code, 0, errors)
        journal = read_json(self.ledger.parent / "workspace-inputs.json")
        self.assertEqual(journal["destination_name_observations"][0]["comparison"], "unobserved")
        for entry in (first, second):
            self.assertEqual((self.checkout / entry["destination"]).read_bytes(), self.source.read_bytes())

    def test_utf16_manifest_parses_and_prepares(self):
        self.create_manifest()
        self.manifest_path.write_bytes(json.dumps(self.manifest).encode("utf-16"))
        code, _, errors = self.invoke("prepare-run", self.ident, "--manifest", str(self.manifest_path))
        self.assertEqual(code, 0, errors)
        self.assertEqual(read_json(self.ledger.parent / "workspace-inputs-manifest.json"), self.manifest)

    def test_publication_temp_path_limit_leaves_manifest_unbound_and_retryable(self):
        self.create_manifest()
        path_max = os.pathconf(self.checkout, "PC_PATH_MAX")
        name_max = os.pathconf(self.checkout, "PC_NAME_MAX")
        temp_name = ".harness-input-" + "0" * 32
        parent = self.checkout / "__pycache__"
        # Leave room for the temporary basename, separator, and terminating NUL.
        remaining = path_max - len(os.fsencode(parent / temp_name)) - 2
        if remaining < 2 or name_max < len(temp_name):
            self.skipTest("volume limits do not allow the long publication path fixture")
        while remaining > 1:
            length = min(100, name_max, remaining - 1)
            parent /= "a" * length
            remaining -= length + 1
        short_destination = parent / "input.txt"
        destination = parent / ("a" * 24) / short_destination.name
        temp = destination.with_name(temp_name)
        with self.assertRaises(FileNotFoundError):
            destination.lstat()
        with self.assertRaises(OSError) as raised:
            temp.stat()
        self.assertEqual(raised.exception.errno, errno.ENAMETOOLONG)

        entry = self.manifest["files"][0]
        entry["destination"] = destination.relative_to(self.checkout).as_posix()
        write_json(self.manifest_path, self.manifest)
        self.refuse_manifest(locked=True)
        journal = self.ledger.parent / "workspace-inputs.json"
        self.assertFalse(journal.exists())
        self.assertFalse((self.checkout / "__pycache__").exists())
        self.assertEqual(list(self.checkout.glob(".harness-input-probe-*")), [])

        entry["destination"] = short_destination.relative_to(self.checkout).as_posix()
        write_json(self.manifest_path, self.manifest)
        code, _, errors = self.invoke("prepare-run", self.ident, "--manifest", str(self.manifest_path))
        self.assertEqual(code, 0, errors)
        self.assertEqual(read_json(journal)["files"][0]["stage"], "copied")
        self.assertEqual(read_json(self.ledger.parent / "workspace-inputs-manifest.json"), self.manifest)
        self.assertEqual(short_destination.read_bytes(), self.source.read_bytes())

    def test_valid_cli_manifest_publishes_a_bound_independent_copy(self):
        self.create_manifest()
        self.manifest["files"][0]["destination"] = "__pycache__/依赖.txt"
        write_json(self.manifest_path, self.manifest)
        code, _, errors = self.invoke("prepare-run", self.ident, "--manifest", str(self.manifest_path))
        self.assertEqual(code, 0, errors)
        saved = read_json(self.ledger)
        self.assertIsNotNone(saved["workspace_inputs"])
        destination = self.checkout / self.manifest["files"][0]["destination"]
        self.assertEqual(destination.read_bytes(), self.source.read_bytes())
        self.assertNotEqual(destination.stat().st_ino, self.source.stat().st_ino)

    def test_current_head_paths_source_identity_and_total_are_checked_with_locks(self):
        self.create_manifest()
        for field, value in (
            ("head", "1" + "0" * 39 if self.manifest["head"] == "0" * 40 else "0" * 40),
            ("head", "0" * 64), ("destination", "test_hello.py"), ("destination", "unignored.txt"),
            ("sha256", "0" * 64), ("mode", 0o600), ("max_bytes", 1),
        ):
            with self.subTest(field=field, value=value):
                manifest = deepcopy(self.manifest)
                target = manifest if field in ("head", "max_bytes") else manifest["files"][0]
                target[field] = value
                write_json(self.manifest_path, manifest)
                self.refuse_manifest(locked=True)
                self.clear_locks()
        write_json(self.manifest_path, self.manifest)
        destination = self.checkout / self.manifest["files"][0]["destination"]
        destination.parent.mkdir()
        destination.symlink_to(self.source)
        self.refuse_manifest(locked=True)
        self.assertTrue(destination.is_symlink())
        destination.unlink()
        self.clear_locks()
        alias = self.source.with_suffix(".alias")
        os.link(self.source, alias)
        self.refuse_manifest(locked=True)
        alias.unlink()
        self.clear_locks()
        # The normal worktree binding makes equality impossible because source paths
        # cannot include .harness. Inject it after the real destination check.
        original = workspace_inputs._destination

        def same_path(run, name):
            original(run, name)
            return self.source.resolve()

        with patch.object(workspace_inputs, "_destination", side_effect=same_path):
            self.refuse_manifest(locked=True)
        self.assertEqual(self.source.read_bytes(), b"test dependency")
        self.assertFalse(destination.exists())

    def test_source_ancestor_name_is_checked_with_locks(self):
        moved = self.outside / "credentials"
        self.repo.rename(moved)
        self.repo = moved
        self.create_manifest()
        write_json(self.manifest_path, self.manifest)
        self.refuse_manifest(locked=True)
