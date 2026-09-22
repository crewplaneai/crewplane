import json
from typing import cast

import pytest

from crewplane.architecture.contracts import (
    ExecutionStatus,
    InvocationStatus,
    NodeStatus,
    WorkflowStatus,
)
from crewplane.architecture.contracts.execution_status import (
    TERMINAL_WORKSPACE_STATUSES,
    TerminalWorkspaceStatus,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import (
    InvocationRuntimeState,
    NodeRuntimeState,
    RunDashboardState,
)
from crewplane.observability.log_presentation.follow import status_from_snapshot


def invocation_state(status: InvocationStatus) -> InvocationRuntimeState:
    return InvocationRuntimeState(
        task_id="task",
        provider="mock",
        role=ProviderRole.EXECUTOR,
        model=None,
        audit_round_num=None,
        round_num=None,
        status=status,
    )


@pytest.mark.parametrize(
    "status",
    [ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED],
)
def test_terminal_workspace_statuses_share_enum_members_and_persisted_values(
    status: TerminalWorkspaceStatus,
) -> None:
    assert any(member is status for member in TERMINAL_WORKSPACE_STATUSES)
    assert json.loads(json.dumps(status)) in TERMINAL_WORKSPACE_STATUSES


@pytest.mark.parametrize(
    "status",
    [ExecutionStatus.PENDING, ExecutionStatus.RUNNING, ExecutionStatus.BLOCKED],
)
def test_terminal_workspace_statuses_exclude_nonterminal_members(
    status: ExecutionStatus,
) -> None:
    assert status not in TERMINAL_WORKSPACE_STATUSES
    assert status.value not in TERMINAL_WORKSPACE_STATUSES


@pytest.mark.parametrize("status", ExecutionStatus)
def test_node_state_normalizes_status_and_preserves_json_value(
    status: ExecutionStatus,
) -> None:
    node = NodeRuntimeState("node", "parallel", (), cast(NodeStatus, status.value))

    assert node.status is status
    assert json.loads(json.dumps({"status": node.status})) == {"status": status.value}


@pytest.mark.parametrize(
    "status", [item for item in ExecutionStatus if item is not ExecutionStatus.BLOCKED]
)
def test_workflow_and_invocation_states_normalize_supported_statuses(
    status: ExecutionStatus,
) -> None:
    workflow = RunDashboardState(
        "workflow", "run", {}, workflow_status=cast(WorkflowStatus, status.value)
    )
    invocation = invocation_state(cast(InvocationStatus, status.value))

    assert workflow.workflow_status is status
    assert invocation.status is status
    assert status_from_snapshot({"invocation_status": status.value}) is status


@pytest.mark.parametrize("status", ["blocked", "unknown", "", None, 1])
def test_workflow_and_invocation_states_reject_unsupported_statuses(
    status: object,
) -> None:
    with pytest.raises(ValueError):
        RunDashboardState(
            "workflow", "run", {}, workflow_status=cast(WorkflowStatus, status)
        )
    with pytest.raises(ValueError):
        invocation_state(cast(InvocationStatus, status))


@pytest.mark.parametrize("status", ["unknown", "", None, 1])
def test_node_state_rejects_unknown_statuses(status: object) -> None:
    with pytest.raises(ValueError):
        NodeRuntimeState("node", "parallel", (), cast(NodeStatus, status))


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"invocation_status": "blocked"},
        {"invocation_status": "invalid"},
        {"invocation_status": None},
    ],
)
def test_snapshot_status_preserves_running_fallback(
    snapshot: dict[str, object],
) -> None:
    assert status_from_snapshot(snapshot) is ExecutionStatus.RUNNING
