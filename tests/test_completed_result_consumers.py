"""Case 12 uses a native completed run whose accepted Git object is retired."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unittest
from copy import deepcopy
from html import unescape
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from git_isolation import fixture_git
from harness import dashboard, git, observe
from harness.util import read_json
from harness.versions import RUN_SCHEMA_VERSION
from test_harness import ROOT, RepoCase, write_project_config


OFFLINE_RUN = """
import runpy
import sys
from pathlib import Path
from unittest.mock import patch
from harness.adapters import DemoBackend
from harness.run_context import RunContext

stale = Path(sys.argv.pop(1))
checkpoint = RunContext.checkpoint

def save_first_handoff(context):
    checkpoint(context)
    if context.run["status"] != "completed" and not stale.exists():
        stale.write_bytes((context.directory / "handoff.json").read_bytes())

with patch("harness.run_context.CLIBackend", return_value=DemoBackend()), \
     patch.object(RunContext, "checkpoint", save_first_handoff):
    runpy.run_module("harness", run_name="__main__")
"""

# Trace the actual subprocess boundary, including provenance and Git query paths
# that bypass harness.git._launch. Forward mode executes every normal check.
GIT_TRACE = """
import json
import os
import subprocess
import sys
import traceback
from harness.run_lifecycle import RunLifecycle

popen = subprocess.Popen
load_run = RunLifecycle.load_run
loaded = False

def load(lifecycle, *args, **kwargs):
    global loaded
    loaded = True
    return load_run(lifecycle, *args, **kwargs)

def trace(argv, *args, **kwargs):
    if isinstance(argv, (tuple, list)) and os.path.basename(argv[0]) == "git":
        mode = os.environ["COMPLETED_RESULT_GIT_MODE"]
        denied = mode == "deny" or (
            mode == "locate" and argv[-2:] != ["rev-parse", "--show-toplevel"])
        with open(os.environ["COMPLETED_RESULT_GIT_LOG"], "a") as stream:
            frames = traceback.extract_stack()[:-1]
            print(json.dumps({"argv": argv, "cwd": os.getcwd(),
                              "subprocess_cwd": kwargs.get("cwd"), "loaded": loaded,
                              "denied": denied,
                              "callers": [(frame.filename, frame.name) for frame in frames]}), file=stream)
        if denied:
            argv = [sys.executable, "-c",
                    "import sys; sys.stderr.write('completed result fixture: Git deliberately denied'); sys.exit(93)"]
    return popen(argv, *args, **kwargs)

subprocess.Popen = trace
RunLifecycle.load_run = load
"""


BROWSER_DOM = r"""
const [debuggerUrl, url] = process.argv.slice(1);
const socket = new WebSocket(debuggerUrl);
let stage = "connecting";
const timer = setTimeout(() => {
  console.error("Chrome automation timed out while " + stage);
  process.exit(1);
}, 12000);
socket.addEventListener("error", event => {
  console.error("Chrome debugging connection failed", event.error);
  process.exit(1);
});
let nextId = 0;
const pending = new Map();
socket.addEventListener("message", ({data}) => {
  const response = JSON.parse(data);
  const resolve = pending.get(response.id);
  if (resolve) {
    pending.delete(response.id);
    resolve(response);
  }
});
function call(method, params, timeout = 1000) {
  const id = ++nextId;
  return new Promise(resolve => {
    pending.set(id, resolve);
    setTimeout(() => {
      if (pending.delete(id)) resolve({error: method + " timed out"});
    }, timeout);
    socket.send(JSON.stringify({id, method, params}));
  });
}
socket.addEventListener("open", async () => {
  stage = "navigating the selected tab to " + url;
  const navigation = await call("Page.navigate", {url}, 10000);
  if (!navigation.result?.frameId || navigation.result.errorText || navigation.result.isDownload) {
    console.error("Chrome navigation failed for " + url + ": " + JSON.stringify(navigation));
    process.exit(1);
  }
  stage = "reading the selected tab at " + url;
  const until = Date.now() + 10000;
  while (Date.now() < until) {
    const result = await call("Runtime.evaluate", {
      expression: `(() => {
        const url = location.href;
        const tab = document.querySelector("#tab-meta");
        if (url !== ${JSON.stringify(url)} || !tab) {
          return {url, text: document.querySelector("#main")?.innerText};
        }
        tab.click();
        const panel = document.querySelector("#panel-meta");
        return panel.getClientRects().length ? {panel: panel.outerHTML} : {hidden: true};
      })()`,
      returnByValue: true
    });
    const value = result.result?.result?.value;
    stage = "reading the selected tab " + JSON.stringify(value || result).slice(0, 300);
    if (value?.panel) {
      clearTimeout(timer);
      process.stdout.write(value.panel, () => process.exit(0));
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  console.error("run information tab did not render in Chrome: " + stage);
  process.exit(1);
});
"""


class CompletedResultConsumers(RepoCase):
    def cli(self, *command, repo=None, runner=("-m", "harness"), env=None):
        return subprocess.run(
            [sys.executable, *runner, "--repo", str(repo or self.repo), *command],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=45,
        )

    def successful_json(self, call):
        self.assertEqual(call.returncode, 0, call.stderr)
        return json.loads(call.stdout)

    def completed_fixture(self):
        self.repo = self.repo.resolve()
        write_project_config(self.repo)
        self.subdirectory = self.repo / "existing"
        self.subdirectory.mkdir()
        (self.subdirectory / "tracked").write_text("fixture\n")
        fixture_git(self.repo, "add", "existing/tracked")
        fixture_git(self.repo, "commit", "-m", "Add tracked subdirectory")
        self.stale = self.outside / "stale-handoff.json"
        launched = self.cli(
            "run", "Implement greet", "--plan", str(ROOT / "examples/plan.json"),
            runner=("-c", OFFLINE_RUN, str(self.stale)),
        )
        self.start_output = self.successful_json(launched)
        self.run_id = self.start_output["id"]
        self.state = self.repo / ".harness"
        self.directory = self.state / "runs" / self.run_id
        self.ledger = self.directory / "state.json"
        self.handoff = self.directory / "handoff.json"
        self.saved = read_json(self.ledger)
        self.saved_bytes = self.ledger.read_bytes()
        self.worktree = Path(self.saved["worktree"]).resolve()
        self.commit = self.saved["expected_head"]
        self.base = self.saved["base_commit"]
        self.branch = self.saved["branch"]
        self.assertEqual(self.saved["status"], "completed")
        self.assertEqual(self.saved["schema_version"], RUN_SCHEMA_VERSION)
        self.assertEqual(self.start_output["status"], "completed")
        self.assertEqual(self.start_output["accepted"], self.saved["accepted"])
        self.assertEqual(self.start_output["branch"], self.branch)
        self.assertEqual(self.start_output["worktree"], self.saved["worktree"])
        approval, = self.saved["final_history"]
        self.assertEqual((approval["status"], approval["head"]), ("approved", self.commit))
        self.expected = {
            "commit": self.commit,
            "tree": fixture_git(self.repo, "rev-parse", f"{self.commit}^{{tree}}").strip(),
            "snapshot_digest": approval["snapshot_digest"],
            "attempts": [approval["attempt"]],
            "verification_mode": "run_worktree",
        }
        self.assertEqual(self.saved["result"], self.expected)
        self.assertNotEqual(self.commit, self.base)
        self.assertNotEqual(read_json(self.stale)["status"], "completed")
        self.assert_operation(self.start_output, "run")
        self.engine_for_resume = self.engine()
        self.trace_dir = self.outside / "trace"
        self.trace_dir.mkdir()
        (self.trace_dir / "sitecustomize.py").write_text(GIT_TRACE)

    def retire_approved_commit(self):
        fixture_git(self.worktree, "reset", "--hard", self.base)
        self.assertEqual(fixture_git(self.repo, "rev-parse", self.branch).strip(), self.base)
        with (self.worktree / "test_hello.py").open("a") as source:
            source.write("\n# Changed after completion\n")
        self.assertIn("test_hello.py", fixture_git(self.worktree, "status", "--porcelain"))
        self.assertEqual(fixture_git(self.repo, "for-each-ref", "--contains", self.commit), "")
        fixture_git(self.repo, "reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
        fixture_git(self.repo, "gc", "--prune=now")
        unavailable = git.git_process(self.repo, "cat-file", "-t", self.commit)
        self.assertEqual(unavailable.returncode, 128, unavailable.stderr)
        self.assertEqual(unavailable.stdout, b"")
        self.assertTrue(unavailable.stderr)
        self.assertEqual(fixture_git(self.repo, "cat-file", "-t", self.base).strip(), "commit")

    def cold_cli(self, cwd, command, *, mode="forward"):
        trace = self.trace_dir / "git.jsonl"
        trace.unlink(missing_ok=True)
        child = self.cli(command, self.run_id, repo=cwd, env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(map(str, (self.trace_dir, ROOT))),
            "COMPLETED_RESULT_GIT_LOG": str(trace),
            "COMPLETED_RESULT_GIT_MODE": mode,
        })
        self.assertTrue(trace.exists(), child.stderr)
        return child, [json.loads(line) for line in trace.read_text().splitlines()]

    def assert_operation(self, stdout, kind):
        record = read_json(self.state / "operations" / (stdout["operation_id"] + ".json"))
        self.assertEqual((record["kind"], record["run_id"], record["result_status"], record["exit_status"]),
                         (kind, self.run_id, "completed", 0))
        self.assertEqual(record["repo_head"], self.base)
        self.assertEqual(record["harness"]["revision"], fixture_git(ROOT, "rev-parse", "HEAD").strip())

    def assert_cold_guards(self, trace, cwd, command):
        self.assertEqual(trace[0]["argv"][1:3], ["-C", str(cwd)])
        self.assertEqual(trace[0]["argv"][-2:], ["rev-parse", "--show-toplevel"])
        self.assertFalse(any(row["loaded"] for row in trace), trace)
        for row in trace:
            argv = row["argv"]
            self.assertEqual(row["cwd"], str(ROOT))
            self.assertIn(argv[2], {str(cwd), str(self.repo), str(ROOT)})
            self.assertTrue({"show_status", "status_summary", "print_saved_result"}.isdisjoint(
                name for _, name in row["callers"]), row)
            self.assertFalse(any(any(value in arg for value in (self.commit, self.branch, str(self.worktree)))
                                 for arg in argv), row)
        if command == "resume":
            common_dir = [row for row in trace if row["argv"][-1] == "--git-common-dir"]
            self.assertTrue(any(any(name == "create" for _, name in row["callers"]) for row in common_dir))
            self.assertTrue(any(any(name == "ensure_private_state" for _, name in row["callers"])
                                for row in common_dir))
            source = [row for row in trace if row["argv"][2] == str(ROOT)]
            self.assertTrue(any(row["argv"][-3:] == ["rev-parse", "--show-toplevel", "HEAD"] for row in source))
            self.assertTrue(any(row["argv"][-5:] == ["status", "--porcelain", "--untracked-files=normal", "--", "harness"]
                                for row in source))
            self.assertTrue(any(row["argv"][2] == str(self.repo) and row["argv"][-2:] == ["rev-parse", "HEAD"]
                                for row in trace))
            for suffix in (["ls-files", "--cached", "-z", "--", ".harness"],
                           ["ls-tree", "-r", "--name-only", "-z", "HEAD", "--", ".harness"],
                           ["check-ignore", "-q", "--", ".harness/.gitignore"]):
                self.assertTrue(any(row["argv"][-len(suffix):] == suffix for row in trace), suffix)

    def browser_dom(self, profile_name="chrome-profile"):
        chrome = os.environ.get("HARNESS_TEST_CHROME")
        if not chrome:
            self.skipTest("set HARNESS_TEST_CHROME to opt in with an installed Chrome/Chromium binary")
        node = shutil.which("node")
        if not node or subprocess.run([node, "-p", "typeof WebSocket"], capture_output=True,
                                      text=True, timeout=5).stdout.strip() != "function":
            self.skipTest("dashboard browser test needs Node with WebSocket support")
        profile = self.outside / profile_name
        macos_flags = (
            ["--use-mock-keychain", "--password-store=basic", "--no-default-browser-check"]
            if sys.platform == "darwin" else []
        )
        with dashboard.create_server(self.state, port=0, log=io.StringIO()) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with subprocess.Popen([
                    chrome, *macos_flags, "--headless", "--disable-background-networking", "--no-first-run",
                    "--disable-component-update", "--disable-sync", "--disable-default-apps", "--no-proxy-server",
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1",
                    f"--user-data-dir={profile}", "--remote-debugging-port=0", "about:blank",
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as browser:
                    try:
                        active_port = profile / "DevToolsActivePort"
                        deadline = time.monotonic() + 10
                        while not active_port.exists() and browser.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.05)
                        self.assertTrue(active_port.exists(), "Chrome did not expose its local debugging port")
                        port = active_port.read_text().splitlines()[0]
                        url = f"{server.url}#/runs/{self.run_id}"
                        request = Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
                        with urlopen(request, timeout=5) as response:
                            target = json.load(response)
                        launched = subprocess.run([
                            node, "-e", BROWSER_DOM, target["webSocketDebuggerUrl"], url,
                        ], text=True, capture_output=True, timeout=20)
                    finally:
                        if browser.poll() is None:
                            browser.terminate()
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(launched.returncode, 0, launched.stderr)
        return launched.stdout

    def test_browser_fixture_requires_opt_in_and_isolates_macos_keychain(self):
        chrome = str(self.outside / "fake-chrome")
        macos_flags = {"--use-mock-keychain", "--password-store=basic", "--no-default-browser-check"}
        with patch.dict(os.environ, clear=True), patch.object(subprocess, "Popen") as launch:
            with self.assertRaisesRegex(unittest.SkipTest, "set HARNESS_TEST_CHROME"):
                self.browser_dom()
            launch.assert_not_called()

        self.completed_fixture()
        for platform, expected_flags in (("darwin", macos_flags), ("linux", set())):
            with self.subTest(platform=platform):
                profile_name = f"fake-profile-{platform}"
                profile = self.outside / profile_name
                profile.mkdir()
                (profile / "DevToolsActivePort").write_text("12345\n")
                target = io.StringIO('{"webSocketDebuggerUrl": "ws://fixture"}')
                responses = [
                    subprocess.CompletedProcess([], 0, "function", ""),
                    subprocess.CompletedProcess([], 0, "fixture DOM", ""),
                ]
                with (
                    patch.dict(os.environ, {"HARNESS_TEST_CHROME": chrome}),
                    patch.object(sys, "platform", platform),
                    patch.object(shutil, "which", return_value="fake-node"),
                    patch.object(subprocess, "run", side_effect=responses),
                    patch.object(subprocess, "Popen") as launch,
                    patch(f"{__name__}.urlopen") as devtools,
                ):
                    devtools.return_value.__enter__.return_value = target
                    self.assertEqual(self.browser_dom(profile_name), "fixture DOM")
                launch.assert_called_once()
                argv = launch.call_args.args[0]
                self.assertEqual(argv[0], chrome)
                self.assertEqual(set(argv) & macos_flags, expected_flags)
                self.assertIn(f"--user-data-dir={profile}", argv)

    def test_damaged_result_is_omitted_from_status_and_observe(self):
        self.completed_fixture()
        for label, field, value in (
            ("nonterminal", "status", "running"),
            ("unusable attempts", "result", {**self.expected, "attempts": [{"toString": 1}]}),
            ("mismatched tree", "result", {**self.expected, "tree": "wrong-tree"}),
            ("mismatched mode", "result", {**self.expected, "verification_mode": "fresh_checkout"}),
            ("non-array history", "final_history", {"attempt": 1}),
            ("non-object history record", "final_history", [None]),
            ("incomplete history record", "final_history", [{"status": "approved"}]),
            ("unusable config", "config_snapshot", {"verification": []}),
        ):
            with self.subTest(damage=label):
                changed = deepcopy(self.saved)
                changed[field] = value
                encoded = json.dumps(changed).encode()
                self.ledger.write_bytes(encoded)
                summary = self.successful_json(self.cli("status", self.run_id))
                self.assertEqual(summary["status"], changed["status"])
                self.assertEqual(summary["worker_outcomes"]["completed"], 1)
                self.assertNotIn("result", summary)
                self.assertEqual(self.successful_json(self.cli("status", self.run_id, "--full")), changed)
                detail = observe.run_detail(self.state, self.run_id)
                self.assertIsNone(detail["ledger_error"])
                self.assertIsNone(detail["summary_error"])
                self.assertNotIn("result", detail["summary"])
                self.assertIn("result", detail["unavailable"])
                self.assertEqual(detail["run"]["expected_head"], self.commit)
                self.assertEqual(self.ledger.read_bytes(), encoded)

    def test_damaged_result_leaves_dashboard_run_information_readable(self):
        self.completed_fixture()
        for label, field, value in (
            ("nonterminal", "status", "running"),
            ("attempts", "result", {**self.expected, "attempts": [{"toString": 1}]}),
        ):
            with self.subTest(damage=label):
                self.ledger.write_text(json.dumps({**self.saved, field: value}))
                dom = self.browser_dom(f"chrome-{label}")
                self.assertIn('id="panel-meta"', dom)
                self.assertIn("期望 HEAD", dom)
                self.assertIn(self.commit, dom)
                self.assertIn("worker 结果计数", dom)
                for hidden in ("结果提交", "结果树", "批准它的整体尝试", "验证模式", "分支之后可能已移动"):
                    self.assertNotIn(hidden, dom)

    def test_completed_result_surfaces(self):
        self.completed_fixture()
        with self.subTest(surface="CLI run stdout"):
            self.assertEqual(self.start_output.get("result"), self.expected)

        self.retire_approved_commit()
        for cwd in (self.repo, self.subdirectory):
            for command in ("status", "resume"):
                with self.subTest(surface="cold Git protections", command=command, cwd=cwd.name):
                    child, trace = self.cold_cli(cwd, command)
                    output = self.successful_json(child)
                    self.assertEqual(output["status"], "completed")
                    self.assert_cold_guards(trace, cwd, command)
                    if command == "resume":
                        self.assert_operation(output, "resume")
                    with self.subTest(surface=f"CLI {command} stdout"):
                        self.assertEqual(output.get("result"), self.expected)
            for mode in ("deny", "locate"):
                with self.subTest(surface="cold failure protections", cwd=cwd.name, mode=mode):
                    for command in ("status", "resume"):
                        records = set((self.state / "operations").iterdir())
                        child, trace = self.cold_cli(cwd, command, mode=mode)
                        self.assertEqual(set((self.state / "operations").iterdir()), records)
                        if mode == "locate" and command == "status":
                            self.assertEqual(child.returncode, 0, child.stderr)
                            continue
                        failing = "--show-toplevel" if mode == "deny" else "--git-common-dir"
                        self.assertEqual(child.returncode, 1, child.stderr)
                        self.assertIn(failing, child.stderr)
                        self.assertIn(failing, trace[-1]["argv"])

        with self.subTest(surface="actual dashboard DOM"):
            dom = self.browser_dom()
            self.assertIn('id="panel-meta"', dom)
            markup = dom
            rows = [(unescape(label), unescape(re.sub(r"<[^>]*>", "", value)))
                    for label, value in re.findall(r"<dt>([^<]+)</dt><dd>(.*?)</dd>", markup, re.S)]
            expected_rows = [
                ("期望 HEAD", self.commit),
                ("结果提交", self.commit),
                ("结果树", self.expected["tree"]),
            ]
            self.assertIn(expected_rows[0], rows)
            head = rows.index(expected_rows[0])
            self.assertEqual(rows[head:head + 3], expected_rows)
            self.assertEqual(rows[head + 3][0], "批准它的整体尝试")
            self.assertEqual(re.findall(r"\d+", rows[head + 3][1]), list(map(str, self.expected["attempts"])))
            self.assertEqual(rows[head + 4], ("验证模式", self.expected["verification_mode"]))
            self.assertIn("台账记录", markup)
            self.assertIn("分支之后可能已移动", markup)

        popen = subprocess.Popen
        denied_calls = []

        def refuse_git(argv, *args, **kwargs):
            if isinstance(argv, (tuple, list)) and os.path.basename(argv[0]) == "git":
                denied_calls.append(argv)
                raise AssertionError("completed consumer attempted Git")
            return popen(argv, *args, **kwargs)

        with patch.object(subprocess, "Popen", refuse_git):
            with self.subTest(surface="normal Runtime resume with every Git launch denied"):
                self.assertEqual(self.engine_for_resume.resume(self.run_id), self.saved)
            with self.subTest(surface="direct status summary"):
                from harness.cli import status_summary

                self.assertEqual(status_summary(self.saved).get("result"), self.expected)
            with self.subTest(surface="read-only observe detail"):
                detail = observe.run_detail(self.state, self.run_id)
                self.assertIsNone(detail["ledger_error"])
                self.assertIsNone(detail["summary_error"])
                self.assertEqual(detail["summary"].get("result"), self.expected)
            with self.subTest(surface="handoff JSON"):
                handoff = read_json(self.handoff)
                self.assertEqual(handoff["status"], "completed")
                self.assertEqual(handoff.get("result"), self.expected)
            self.assertEqual(denied_calls, [])

        for handoff in ("stale", "missing"):
            if handoff == "stale":
                self.handoff.write_bytes(self.stale.read_bytes())
            else:
                self.handoff.unlink()
            before = self.handoff.read_bytes() if self.handoff.exists() else None
            for command in ("status", "resume"):
                child, trace = self.cold_cli(self.repo, command)
                output = self.successful_json(child)
                with self.subTest(surface="ledger authority", handoff=handoff, command=command):
                    self.assertEqual(output.get("result"), self.expected)
                with self.subTest(surface="optional handoff untouched", handoff=handoff, command=command):
                    self.assertEqual(self.handoff.read_bytes() if self.handoff.exists() else None, before)
                    self.assertEqual(self.ledger.read_bytes(), self.saved_bytes)
                    self.assert_cold_guards(trace, self.repo, command)
        with self.subTest(surface="full status ledger after handoff removal"):
            self.assertEqual(self.successful_json(self.cli("status", self.run_id, "--full")), self.saved)
