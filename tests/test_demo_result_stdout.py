"""Completed demo and offline-substituted smoke use the ledger's result on stdout."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def invoke_fixture(directory, command):
    git = shutil.which("git")
    if git is None:
        raise AssertionError("this fixture requires local Git")
    bin_dir = directory / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(Path(git).resolve())
    for vendor in ("codex", "claude"):
        stub = bin_dir / vendor
        stub.write_text("#!/bin/sh\nexit 93\n", encoding="utf-8")
        stub.chmod(0o700)
    home = directory / "home"
    home.mkdir()
    return subprocess.run(
        [sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--offline-fixture", command],
        cwd=directory, capture_output=True, text=True, timeout=60,
        env={
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home),
            "CODEX_HOME": str(home / "codex"),
            "CLAUDE_CONFIG_DIR": str(home / "claude"),
            "TMPDIR": str(directory),
            "PATH": str(bin_dir),
            "PYTHONNOUSERSITE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )


def offline_cli(command):
    # Allow only the fixture's local Git and the exact demo verification command.
    # This guards this Python process's launches, within the test's controlled environment.
    trace = Path.cwd() / "launches.jsonl"
    verification = [sys.executable, "-m", "unittest", "-v"]

    def record(kind, **values):
        with trace.open("a", encoding="utf-8") as stream:
            print(json.dumps({"kind": kind, **values}), file=stream)

    def guard(event, args):
        if event == "subprocess.Popen":
            executable, argv, _, _ = args
            permitted = (executable == "git" and argv[0] == "git") or (
                executable == sys.executable and argv == verification)
            record("launch", argv=argv, permitted=permitted)
            if not permitted:
                raise AssertionError(f"unexpected fixture subprocess: {argv!r}")
        elif event in ("os.system", "os.exec", "os.posix_spawn"):
            raise AssertionError(f"unexpected fixture launch: {event}")

    sys.addaudithook(guard)
    sys.path.insert(0, str(ROOT))
    from unittest.mock import patch

    from harness import adapters, cli, run_context

    def refuse_provider(*args, **kwargs):
        record("refused_provider")
        raise AssertionError("a live provider was reached")

    def offline_backend(config, state):
        if command != "smoke":
            refuse_provider()
        record("smoke_backend_substitution")
        return adapters.DemoBackend()

    # The CLI selects None for smoke, which normally constructs the live backend
    # in RunContext. Substitute at that lookup while guarding the underlying class.
    with patch.object(adapters, "CLIBackend", side_effect=refuse_provider), \
            patch.object(run_context, "CLIBackend", side_effect=offline_backend):
        return cli.main(["--config", str(ROOT / "harness.toml"), command])


class DemoResultStdoutTests(unittest.TestCase):
    def test_completed_stdout_matches_its_saved_result(self):
        for command in ("demo", "smoke"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as scratch:
                folder = Path(scratch).resolve()
                call = invoke_fixture(folder, command)
                self.assertEqual(call.returncode, 0, call.stderr)
                stdout = json.loads(call.stdout)
                repo = Path(stdout["repo"])
                self.assertEqual(repo.parent, folder)
                trace = [
                    json.loads(line)
                    for line in (folder / "launches.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                substitutions = ["smoke_backend_substitution"] if command == "smoke" else []
                self.assertEqual([row["kind"] for row in trace if row["kind"] != "launch"], substitutions)
                self.assertTrue(all(row["permitted"] for row in trace if row["kind"] == "launch"))
                self.assertEqual(
                    sum(row.get("argv", [])[1:] == ["-m", "unittest", "-v"] for row in trace), 2
                )

                (ledger_path,) = repo.glob(".harness/runs/*/state.json")
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                self.assertEqual(ledger["status"], "completed")
                self.assertEqual(ledger_path.parent.name, ledger["id"])
                self.assertFalse(ledger["config_snapshot"]["verification"]["fresh_checkout"])
                (approval,) = ledger["final_history"]
                self.assertEqual((approval["status"], approval["head"]), ("approved", ledger["expected_head"]))
                expected = {
                    "commit": approval["head"],
                    "tree": approval["tree"],
                    "snapshot_digest": approval["snapshot_digest"],
                    "attempts": [approval["attempt"]],
                    "verification_mode": "run_worktree",
                }
                self.assertEqual(ledger["result"], expected)
                self.assertEqual({key: value for key, value in stdout.items() if key != "result"}, {
                    "simulation": command == "demo",
                    "models_called": command == "smoke",
                    "verification": "real unittest + git",
                    "repo": str(repo),
                    "run": ledger["id"],
                    "branch": ledger["branch"],
                    "status": "completed",
                })

                self.assertEqual(stdout.get("result"), ledger["result"], "stdout omitted or changed the saved result")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--offline-fixture"] and sys.argv[2:] in (["demo"], ["smoke"]):
        raise SystemExit(offline_cli(sys.argv[2]))
    unittest.main()
