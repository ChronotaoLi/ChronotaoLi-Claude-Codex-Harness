import json
from pathlib import Path
from unittest.mock import patch

from harness import stops
from harness.adapters import DemoBackend
from harness.commands import command_id
from harness.process_runtime import ProcessOutcome
from harness.run_context import RunContext
from harness.util import HarnessError, read_json, write_json
from test_harness import RepoCase, ROOT, cli_notice, commit_recorder
from test_operations import OperationCase
from test_runtime_changes import ScriptedBackend


COMMAND = {"cwd": ".", "argv": ["git", "status", "--short"]}


def starting_command(job):
    folder = job / "commands" / ("a" * 32)
    folder.mkdir()
    write_json(folder / "process.json", {
        "id": f"{job.name}:command:{folder.name}", "command_id": command_id(COMMAND),
        "pgid": None, "process_state": "starting", "exit_confirmed": False,
    })
    return folder


class CommandEngineBackend(DemoBackend):
    def __init__(self, testcase, inject_unknown=False):
        self.testcase = testcase
        self.inject_unknown = inject_unknown
        self.workers = []

    def invoke(self, **kwargs):
        if kwargs['role'] == 'worker':
            state = read_json(kwargs['run_path'])
            entry = state['dispatches'][-1]
            directory = kwargs['job'] / 'commands'
            self.testcase.assertEqual(entry['role'], 'worker')
            self.testcase.assertEqual(entry['id'], kwargs['job'].name)
            self.testcase.assertEqual(entry['command_directory'], str(directory))
            self.testcase.assertTrue(directory.is_dir())
            self.testcase.assertTrue(entry['invoked'])
            self.workers.append(entry['id'])
            if self.inject_unknown:
                starting_command(kwargs['job'])
        return super().invoke(**kwargs)


class CommandEngineTests(RepoCase):
    def plan(self):
        return read_json(ROOT / 'examples' / 'plan.json')

    def test_worker_command_directory_is_persisted_before_invoke(self):
        backend = CommandEngineBackend(self)
        result = self.engine(backend).start('Implement greet', self.plan())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(backend.workers), 1)
        worker = next(d for d in result['dispatches'] if d['role'] == 'worker')
        self.assertTrue(Path(worker['command_directory']).is_dir())
        self.assertTrue(worker['exit_confirmed'])

    def test_unknown_child_blocks_completed_worker_and_resume_despite_parent_exit(self):
        backend = CommandEngineBackend(self, inject_unknown=True)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', self.plan())
        saved = read_json(engine.directory / 'state.json')
        worker = next(d for d in saved['dispatches'] if d['role'] == 'worker')
        self.assertTrue(worker['exit_confirmed'])
        self.assertEqual(saved['accepted'], {})
        before = len(saved['dispatches'])
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(saved['id'])
        after = read_json(engine.directory / 'state.json')
        self.assertEqual(len(after['dispatches']), before)
        self.assertEqual(len(backend.workers), 1)
        self.assertEqual(after['accepted'], {})
        self.assertFalse(any(d['role'] in ('verification', 'reviewer') for d in after['dispatches']))

    def test_missing_registered_command_directory_blocks_resume(self):
        backend = CommandEngineBackend(self, inject_unknown=True)
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start('Implement greet', self.plan())
        saved = read_json(engine.directory / 'state.json')
        worker = next(d for d in saved['dispatches'] if d['role'] == 'worker')
        directory = Path(worker['command_directory'])
        child = directory / ('a' * 32)
        (child / 'process.json').unlink()
        child.rmdir()
        directory.rmdir()
        before = len(saved['dispatches'])
        with self.assertRaises(HarnessError):
            self.engine(backend).resume(saved['id'])
        after = read_json(engine.directory / 'state.json')
        self.assertEqual(len(after['dispatches']), before)
        self.assertEqual(len(backend.workers), 1)
        self.assertEqual(after['accepted'], {})


class InterruptedCommandTests(OperationCase):
    settled = ProcessOutcome(startup_state="not_started", exit_confirmed=True,
                             termination_reason="cancelled")

    def run_interrupted_worker(self, command_outcome, *, gate_error=None):
        backend = ScriptedBackend()
        self.config.data["permissions"]["worker_commands"] = [COMMAND]
        interrupt = KeyboardInterrupt("worker interrupted")

        def interrupt_worker(backend, kwargs, packet):
            self.assertEqual((kwargs["role"], kwargs["model"]["cli"]), ("worker", "claude"))
            job = kwargs["job"]
            folder = starting_command(job)
            write_json(folder / "output.outcome.json", command_outcome.to_dict())
            kwargs["on_start"](4242)
            outcome = ProcessOutcome(started=True, startup_state="started", pid=4242, pgid=4242,
                                     exit_code=1, termination_reason="cancelled", exit_confirmed=True)
            write_json(job / "process.outcome.json", outcome.to_dict())
            kwargs["on_outcome"](outcome)
            raise interrupt

        backend.before_result = interrupt_worker
        ensure_exited = RunContext.ensure_exited
        checks = []

        def observe_stop_check(context, *, commit=True):
            if commit:
                return ensure_exited(context)
            path = context.directory / "state.json"
            before = path.read_bytes()
            try:
                if gate_error is not None:
                    raise gate_error
                return ensure_exited(context, commit=False)
            finally:
                checks.append((context, before, path.read_bytes()))

        recorder, commits = commit_recorder()
        with patch.object(RunContext, "ensure_exited", observe_stop_check), recorder:
            code, _, errors = self.invoke(["run", "Implement greet", "--plan", str(ROOT / "examples/plan.json"),
                                           "--operation-id", "interrupted-worker"], backend)
        self.assertEqual(code, 130, errors)
        (context, before, after), = checks
        self.assertEqual(before, after)
        self.assertIs(context.saved_stop[0], interrupt)
        self.assertEqual(context.saved_stop[1], 130)
        run_id = context.run["id"]
        saved = self.ledger(run_id)
        self.assertEqual(commits[-2:], [json.loads(before), saved])
        self.assertEqual(saved["store_revision"], json.loads(before)["store_revision"] + 1)
        worker, = saved["dispatches"]
        self.assertIs(worker["exit_confirmed"], True)
        stop = saved["stop"]
        self.assertEqual((saved["status"], stop["kind"], stop["reason"]), ("interrupted", "interrupted", "signal"))

        projection = stops.project_ledger(saved)
        public = stops.project_ledger(saved, omit_detail=True)
        self.assertEqual(projection["exit_code"], 130)
        self.assertEqual(cli_notice(errors), public)
        record = self.record("interrupted-worker")
        self.assertEqual((record["exit_status"], record["stop"]), (130, public))
        stopped, = self.events(run_id, "run_stopped")
        self.assertEqual(stopped["stop"], projection)
        code, output, errors = self.invoke(["status", run_id], backend)
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["stop"], projection)
        return backend, saved, projection

    def test_pending_command_prevents_confirmed_interrupt_and_abandon_advice(self):
        backend, saved, projection = self.run_interrupted_worker(ProcessOutcome())
        run_id = saved["id"]
        worker, = saved["dispatches"]
        folder, = Path(worker["command_directory"]).iterdir()
        journal = folder / "process.json"
        outcome = folder / "output.outcome.json"
        directory = self.state / "runs" / run_id
        protected = [directory / "state.json", directory / "events.jsonl", journal, outcome]
        before = [path.read_bytes() for path in protected]
        for args in (["abandon", run_id], ["resume", run_id, "--operation-id", "pending-command"]):
            code, _, errors = self.invoke(args, backend)
            notice = cli_notice(errors)
            self.assertEqual((code, notice["kind"], notice["reason"]), (3, "process_unconfirmed", "command_exit"))
            self.assertEqual(notice["source"]["dispatch_id"], worker["id"])
            self.assertEqual(notice["data"]["command_id"], read_json(journal)["id"])
            self.assertEqual([path.read_bytes() for path in protected], before)

        # Later exit evidence permits abandon without rewriting the interruption fact.
        write_json(outcome, self.settled.to_dict())
        self.assertEqual(self.ledger(run_id), saved)
        code, output, errors = self.invoke(["abandon", run_id], backend)
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["status"], "abandoned")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual((saved["stop"]["data"]["exits_confirmed"], projection["exits"]),
                         (False, [f"resume {run_id}"]))

    def test_settled_command_confirms_interrupt_and_allows_abandon(self):
        backend, saved, projection = self.run_interrupted_worker(self.settled)
        run_id = saved["id"]
        self.assertIs(saved["stop"]["data"]["exits_confirmed"], True)
        self.assertEqual(projection["exits"], [f"resume {run_id}", f"abandon {run_id}"])
        code, output, errors = self.invoke(projection["exits"][-1].split(), backend)
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["status"], "abandoned")
        self.assertNotIn("stop", self.ledger(run_id))
        self.assertEqual(len(backend.calls), 1)

    def test_unexpected_exit_check_exception_preserves_interrupt_without_confirmation(self):
        _, saved, projection = self.run_interrupted_worker(self.settled, gate_error=OSError("exit check failed"))
        self.assertEqual((saved["stop"]["data"]["exits_confirmed"], projection["exits"]),
                         (False, [f"resume {saved['id']}"]))

    def test_interrupt_without_dispatches_confirms_exits(self):
        engine = self.engine()
        with patch.object(engine.services.scheduler, "next_step", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
        saved = self.ledger(engine.run["id"])
        self.assertEqual(saved["dispatches"], [])
        self.assertIs(saved["stop"]["data"]["exits_confirmed"], True)

    def test_new_interrupt_or_system_exit_from_exit_check_propagates(self):
        for new_error in (KeyboardInterrupt("during exit check"), SystemExit(47)):
            with self.subTest(error=type(new_error).__name__):
                engine = self.engine()
                original = engine.context.ensure_exited

                def check(*, commit=True):
                    if not commit:
                        raise new_error
                    return original()

                with patch.object(engine.context, "ensure_exited", check), \
                        patch.object(engine.services.scheduler, "next_step", side_effect=KeyboardInterrupt), \
                        self.assertRaises(type(new_error)) as caught:
                    engine.start("Implement greet", read_json(ROOT / "examples/plan.json"))
                self.assertIs(caught.exception, new_error)
                self.assertIsNone(engine.context.saved_stop)
                self.assertNotIn("stop", self.ledger(engine.run["id"]))
