from __future__ import annotations

import pytest

from crewplane.core.preflight.models import (
    ArtifactContract,
    ExecutionPolicy,
    PreflightExecutionNode,
    ProviderRecord,
    WorkspaceSelectionRecord,
)
from crewplane.core.workspace.policy import WorktreeContract
from crewplane.runtime.execution.parallel import enforce_parallel_failure_policy
from crewplane.runtime.execution.stage_tasks import ParallelResultSummary


def test_lineage_worktree_parallel_node_rejects_allowed_executor_failure() -> None:
    node = _parallel_node(
        workspace_policy=WorkspaceSelectionRecord(
            enabled=True,
            logical_worktree_name="primary",
            declaration_kind="worktree",
            materialization="worktree_checkout",
            worktree_contract=WorktreeContract(),
            writable=True,
            lineage_producer=True,
        ),
        execution_policy=ExecutionPolicy(failure_threshold=1),
    )

    with pytest.raises(RuntimeError, match="lineage-producing worktree"):
        enforce_parallel_failure_policy(
            node,
            ParallelResultSummary(total=2, successful=1, failed=1),
            telemetry=None,
        )


def _parallel_node(
    workspace_policy: WorkspaceSelectionRecord,
    execution_policy: ExecutionPolicy,
) -> PreflightExecutionNode:
    return PreflightExecutionNode(
        id="implement",
        mode="parallel",
        provider_records=[
            _provider_record("alpha"),
            _provider_record("beta"),
        ],
        execution_policy=execution_policy,
        workspace_policy=workspace_policy,
        artifact_contract=ArtifactContract(
            stage_path="implement",
            output_path="implement/output.md",
        ),
    )


def _provider_record(task_id: str) -> ProviderRecord:
    return ProviderRecord(
        provider=task_id,
        role="executor",
        task_id=task_id,
        agent_config_key=task_id,
        invoker_alias="mock",
        agent_config_signature=f"{task_id}-agent",
        invoker_config_signature="mock-invoker",
    )
