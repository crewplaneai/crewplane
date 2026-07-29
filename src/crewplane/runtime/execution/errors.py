from __future__ import annotations

from crewplane.runtime.agent.failures import InvocationFailureError
from crewplane.runtime.workspace.setup import WorkspaceSetupError


class NodeExecutionError(RuntimeError):
    """Raised for expected terminal failures of a workflow node."""


class WorkflowExecutionError(RuntimeError):
    """Raised when scheduling completes with failed or blocked workflow nodes."""


def is_expected_execution_failure(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (NodeExecutionError, InvocationFailureError, WorkspaceSetupError),
    )
