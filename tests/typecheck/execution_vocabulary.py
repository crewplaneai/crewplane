"""Check lifecycle subsets and shared diagnostic levels for public consumers."""

from crewplane.architecture.contracts import (
    ExecutionStatus,
    InvocationDiagnostic,
    InvocationStatus,
    LogLevel,
    NodeStatus,
    RuntimeLogEventPayload,
    SchedulerNodeStatus,
    WorkflowStatus,
)

# Allowed assignments, checked separately for each public alias even though
# WorkflowStatus and InvocationStatus currently share the same definition.
workflow: WorkflowStatus = ExecutionStatus.CANCELLED
invocation: InvocationStatus = ExecutionStatus.CANCELLED
node: NodeStatus = ExecutionStatus.BLOCKED
scheduler_node: SchedulerNodeStatus = ExecutionStatus.BLOCKED

# Deliberately invalid assignments: BLOCKED is node-only; scheduler nodes
# exclude CANCELLED. Strict mypy reports unused ignores, so these checks fail
# if any assignment unexpectedly becomes valid.
workflow = ExecutionStatus.BLOCKED  # type: ignore[assignment]
invocation = ExecutionStatus.BLOCKED  # type: ignore[assignment]
scheduler_node = ExecutionStatus.CANCELLED  # type: ignore[assignment]

InvocationDiagnostic(LogLevel.WARNING, "message", "operation")
RuntimeLogEventPayload(LogLevel.WARNING, "message", "operation")
InvocationDiagnostic("warning", "message", "operation")  # type: ignore[arg-type]
RuntimeLogEventPayload("warning", "message", "operation")  # type: ignore[arg-type]
