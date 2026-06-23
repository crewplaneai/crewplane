from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from rich.console import Console

from crewplane.observability.events import EventSink

type NodeStatus = Literal["pending", "running", "succeeded", "failed", "blocked"]


@dataclass
class WorkflowExecutionState:
    ready: list[str]
    running: dict[str, asyncio.Task[None]]
    statuses: dict[str, NodeStatus]
    node_errors: dict[str, Exception]
    failed_dependencies: dict[str, set[str]]
    remaining_dependencies: dict[str, int]
    dependents: dict[str, list[str]]
    dependencies_by_node: dict[str, set[str]]
    node_order: dict[str, int]


@dataclass(frozen=True)
class ActivityTrackerSnapshot:
    is_exclusive: bool
    version: int


class RuntimeActivityTracker:
    def __init__(self) -> None:
        self._active_nodes: set[str] = set()
        self._version = 0

    def mark_node_running(self, node_id: str) -> None:
        if node_id in self._active_nodes:
            return
        self._active_nodes.add(node_id)
        self._version += 1

    def mark_node_finished(self, node_id: str) -> None:
        if node_id not in self._active_nodes:
            return
        self._active_nodes.remove(node_id)
        self._version += 1

    def snapshot(self, node_id: str) -> ActivityTrackerSnapshot:
        other_nodes = self._active_nodes - {node_id}
        return ActivityTrackerSnapshot(
            is_exclusive=not other_nodes,
            version=self._version,
        )


@dataclass(frozen=True)
class ExecutionTelemetry:
    workflow_name: str
    run_id: str
    event_sink: EventSink | None = None
    suppress_console_output: bool = False
    activity_tracker: RuntimeActivityTracker | None = None
    console: Console = field(default_factory=Console)
