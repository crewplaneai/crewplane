from __future__ import annotations

from typing import Literal

EventType = Literal[
    "workflow_started",
    "workflow_finished",
    "workflow_failed",
    "node_started",
    "node_finished",
    "node_failed",
    "node_blocked",
    "invocation_started",
    "invocation_finished",
    "invocation_failed",
    "runtime_log",
]
WorkflowEventType = Literal[
    "workflow_started",
    "workflow_finished",
    "workflow_failed",
]
NodeEventType = Literal[
    "node_started",
    "node_finished",
    "node_failed",
    "node_blocked",
]
InvocationEventType = Literal[
    "invocation_started",
    "invocation_finished",
    "invocation_failed",
]
WorkflowStatus = Literal["pending", "running", "succeeded", "failed"]
NodeStatus = Literal["pending", "running", "succeeded", "failed", "blocked"]
InvocationStatus = Literal["pending", "running", "succeeded", "failed"]
LogLevel = Literal["debug", "info", "warning", "error"]
RuntimeLogValue = str | int | float | bool | None
