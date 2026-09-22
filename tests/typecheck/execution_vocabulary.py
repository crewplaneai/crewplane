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
from crewplane.architecture.contracts.execution_status import TerminalWorkspaceStatus

# Allowed assignments, checked separately for each public alias even though
# WorkflowStatus and InvocationStatus currently share the same definition.
workflow: WorkflowStatus = ExecutionStatus.CANCELLED
invocation: InvocationStatus = ExecutionStatus.CANCELLED
node: NodeStatus = ExecutionStatus.BLOCKED
scheduler_node: SchedulerNodeStatus = ExecutionStatus.BLOCKED
workspace: TerminalWorkspaceStatus = ExecutionStatus.SUCCEEDED
workspace = ExecutionStatus.FAILED
workspace = ExecutionStatus.CANCELLED

# Deliberately invalid assignments: BLOCKED is node-only; scheduler nodes
# exclude CANCELLED; workspace terminal states require terminal enum members.
# Strict mypy reports unused ignores, so these checks fail if any assignment
# unexpectedly becomes valid.
workflow = ExecutionStatus.BLOCKED  # type: ignore[assignment]
invocation = ExecutionStatus.BLOCKED  # type: ignore[assignment]
scheduler_node = ExecutionStatus.CANCELLED  # type: ignore[assignment]
workspace = ExecutionStatus.PENDING  # type: ignore[assignment]
workspace = ExecutionStatus.RUNNING  # type: ignore[assignment]
workspace = ExecutionStatus.BLOCKED  # type: ignore[assignment]
workspace = "succeeded"  # type: ignore[assignment]

InvocationDiagnostic(LogLevel.WARNING, "message", "operation")
RuntimeLogEventPayload(LogLevel.WARNING, "message", "operation")
InvocationDiagnostic("warning", "message", "operation")  # type: ignore[arg-type]
RuntimeLogEventPayload("warning", "message", "operation")  # type: ignore[arg-type]
