"""Execution states and the subsets supported by each lifecycle."""

from enum import StrEnum, unique
from typing import Literal


@unique
class ExecutionStatus(StrEnum):
    """Lifecycle states shared by execution and its observers."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


type LifecycleStatus = Literal[
    ExecutionStatus.PENDING,
    ExecutionStatus.RUNNING,
    ExecutionStatus.SUCCEEDED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
]
type WorkflowStatus = LifecycleStatus
type InvocationStatus = LifecycleStatus
type NodeStatus = ExecutionStatus
type SchedulerNodeStatus = Literal[
    ExecutionStatus.PENDING,
    ExecutionStatus.RUNNING,
    ExecutionStatus.SUCCEEDED,
    ExecutionStatus.FAILED,
    ExecutionStatus.BLOCKED,
]


def parse_lifecycle_status(value: str) -> LifecycleStatus:
    """Parse a workflow or invocation status, rejecting node-only states."""
    status = ExecutionStatus(value)
    if status is ExecutionStatus.BLOCKED:
        raise ValueError("Blocked is only valid for node status.")
    return status
