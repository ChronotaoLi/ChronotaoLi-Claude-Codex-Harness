"""Public lifecycle API and explicit composition for the execution kernel."""
from __future__ import annotations

from .run_context import RunContext
from .services import Services


class Runtime:
    def __init__(self, config, repo, state=None, backend=None, commander="astra", operation=None):
        self.context = RunContext(config, repo, state, backend, commander, operation)
        self.services = Services(self.context)

    @property
    def run(self):
        return self.context.run

    @run.setter
    def run(self, value):
        self.context.run = value

    @property
    def config(self):
        return self.context.config

    @property
    def repo(self):
        return self.context.repo

    @property
    def state(self):
        return self.context.state

    @property
    def directory(self):
        return self.context.directory

    @property
    def worktree(self):
        return self.context.worktree

    @property
    def memory(self):
        return self.context.memory

    @property
    def backend(self):
        return self.context.backend

    @backend.setter
    def backend(self, value):
        self.context.backend = value

    @property
    def hooks(self):
        return self.context.hooks

    @property
    def commander(self):
        return self.context.commander

    @property
    def operation(self):
        return self.context.operation

    def start(self, goal, plan=None, *, pause_after_plan=False, step=False):
        return self.services.lifecycle.start(goal, plan, pause_after_plan=pause_after_plan, step=step)

    def load_run(self, ident):
        return self.services.lifecycle.load_run(ident)

    def abandon(self, ident, *, confirm_exited=()):
        return self.services.lifecycle.abandon(ident, confirm_exited=confirm_exited)

    def resume(self, ident, *, step=False, approve_plan=False, reverify=False, retry_stopped=None, confirm_exited=(),
               allow_scope=(), environment_update=None, operator_decision=None,
               execution_guidance=None, reject_continuation=None):
        return self.services.lifecycle.resume(ident, step=step, approve_plan=approve_plan, reverify=reverify,
            retry_stopped=retry_stopped, confirm_exited=confirm_exited,
            allow_scope=allow_scope, environment_update=environment_update, operator_decision=operator_decision,
            execution_guidance=execution_guidance, reject_continuation=reject_continuation)

    def execute(self, supplied_plan=None, *, step=False):
        return self.services.scheduler.execute(supplied_plan, step=step)
