"""Explicit dependencies: shared resources and named stage implementations."""
from __future__ import annotations


class RuntimeService:
    def __init__(self, context, services=None):
        self.context = context
        self.services = services


class Services:
    """One composition root; no dynamic forwarding or saved bound callbacks."""
    def __init__(self, context):
        from .budgets import AttemptAccounting
        from .decisions import DecisionRecovery
        from .dispatch import Dispatcher
        from .environment import EnvironmentRecovery
        from .finalization import FinalAcceptance
        from .packets import PacketBuilder
        from .planning import Planner
        from .progress import ProgressRecovery
        from .receipts import ResultReceipts
        from .run_lifecycle import RunLifecycle
        from .scheduler import Scheduler
        from .task_lifecycle import TaskLifecycle
        from .verification import Verification
        from .workflow import Workflow
        from .worker_receipts import WorkerReceipts
        self.budgets = AttemptAccounting(context, self)
        self.decisions = DecisionRecovery(context, self)
        self.dispatch = Dispatcher(context, self)
        self.environment = EnvironmentRecovery(context, self)
        self.finalization = FinalAcceptance(context, self)
        self.packets = PacketBuilder(context, self)
        self.planning = Planner(context, self)
        self.progress = ProgressRecovery(context, self)
        self.receipts = ResultReceipts(context, self)
        self.worker_receipts = WorkerReceipts(context, self)
        self.lifecycle = RunLifecycle(context, self)
        self.tasks = TaskLifecycle(context, self)
        self.verification = Verification(context, self)
        self.workflow = Workflow(context, self)
        self.scheduler = Scheduler(context, self)
