from __future__ import annotations

from typing import Protocol

from crewplane.observability.events import ExecutionEvent
from crewplane.observability.types import (
    DashboardSnapshot,
    RunContext,
    RunResult,
)


class Observer(Protocol):
    def start(self, context: RunContext) -> None: ...

    def on_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: DashboardSnapshot,
    ) -> None: ...

    def stop(self, result: RunResult) -> None: ...
