from __future__ import annotations

from crewplane.architecture.contracts.invocation_failures import InvocationFailureError
from crewplane.runtime.agent.process.drain import unconfirmed_process_cleanup
from crewplane.runtime.workspace.setup import WorkspaceSetupError


class NodeExecutionError(RuntimeError):
    """Raised for expected terminal failures of a workflow node."""


class WorkflowExecutionError(RuntimeError):
    """Raised when scheduling completes with failed or blocked workflow nodes."""


def is_expected_execution_failure(exc: BaseException) -> bool:
    return unconfirmed_process_cleanup(exc) is None and isinstance(
        exc,
        (NodeExecutionError, InvocationFailureError, WorkspaceSetupError),
    )
