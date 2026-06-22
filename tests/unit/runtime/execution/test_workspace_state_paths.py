from __future__ import annotations

from pathlib import Path

from orchestrator_cli.artifacts.manager import OutputManager
from orchestrator_cli.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    ProviderRecord,
    WorkspaceSelectionRecord,
    WorkspaceSetupCommandRecord,
    WorkspaceSetupRecord,
)
from orchestrator_cli.core.workspace_policy import WorktreeContract
from orchestrator_cli.runtime.execution.review_loop.workspace_state_paths import (
    workspace_artifact_allowed_paths,
)


def test_workspace_artifact_allowlist_empty_without_managed_workspace(
    tmp_path: Path,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = execution_node(workspace_policy=None)

    allowed_paths = workspace_artifact_allowed_paths(
        output,
        node,
        "alpha",
        "executor",
        None,
        1,
    )

    assert allowed_paths == set()
    assert output.get_stage_dir("implement") is None


def test_workspace_artifact_allowlist_contains_runtime_owned_workspace_paths(
    tmp_path: Path,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = execution_node(
        workspace_policy=WorkspaceSelectionRecord(
            enabled=True,
            logical_worktree_name="primary",
            declaration_kind="worktree",
            materialization="worktree_checkout",
            worktree_contract=WorktreeContract(),
            setup=WorkspaceSetupRecord(
                profile_name="bootstrap",
                commands=[
                    WorkspaceSetupCommandRecord(
                        argv=["true"],
                        command_index=0,
                    )
                ],
            ),
            writable=True,
            lineage_producer=True,
        )
    )

    allowed_paths = workspace_artifact_allowed_paths(
        output,
        node,
        "alpha",
        "executor",
        None,
        1,
    )

    stage_dir = output.get_stage_dir("implement")
    assert stage_dir is not None
    assert allowed_paths == {
        stage_dir / "workspace-state.json",
        stage_dir / "workspace-setup" / "setup.json",
        stage_dir / "workspace-setup" / "setup.log",
        stage_dir / "workspace-bundles" / "implement-alpha-round1.bundle",
    }


def execution_node(
    workspace_policy: WorkspaceSelectionRecord | None,
) -> PreflightExecutionNode:
    return PreflightExecutionNode(
        id="implement",
        mode="sequential",
        provider_records=[
            ProviderRecord(
                provider="alpha",
                role="executor",
                task_id="alpha",
                agent_config_key="alpha",
                invoker_alias="mock",
                agent_config_signature="agent-signature",
                invoker_config_signature="invoker-signature",
            )
        ],
        workspace_policy=workspace_policy,
        artifact_contract=ArtifactContract(
            stage_path="implement",
            output_path="implement/output.md",
        ),
    )
