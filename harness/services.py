"""Explicit composition with one live runtime; never copy state or bind callbacks."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Engine


class RuntimeService:
    def __init__(self, runtime: Engine):
        # start/load_run replace runtime.run; always resolve it when called.
        self.runtime = runtime
