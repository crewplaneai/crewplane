from __future__ import annotations

from pathlib import Path

from orchestrator_cli.artifacts.manager import OutputManager
from orchestrator_cli.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceSetupCommandRecord,
    WorkspaceSetupRecord,
)
from orchestrator_cli.runtime.workspace import WorkspaceInvocationRequest
from orchestrator_cli.runtime.workspace.service import MaterializationLimiter
from orchestrator_cli.runtime.workspace.worktree.cache import WorktreeReuseCache
from tests.helpers.workspace_service import workspace_plan


def two_node_lineage_plan(
    repo: Path,
    cache_root: Path,
    cleanup_on_success: bool = True,
) -> PreflightExecutionPlan:
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=cleanup_on_success,
        kind="worktree",
    )
    first = plan.nodes[0]
    first_policy = first.workspace_policy
    assert first_policy is not None
    second = first.model_copy(
        update={
            "id": "verify",
            "dependencies": ["implement"],
            "artifact_contract": ArtifactContract(
                stage_path="verify",
                output_path="verify/output.md",
            ),
            "workspace_policy": first_policy.model_copy(
                update={
                    "source_kind": "node",
                    "source_node_id": "implement",
                }
            ),
        }
    )
    return plan.model_copy(
        update={
            "execution_order": ["implement", "verify"],
            "nodes": [first, second],
        }
    )


def three_node_lineage_plan(repo: Path, cache_root: Path) -> PreflightExecutionPlan:
    plan = two_node_lineage_plan(repo, cache_root)
    first, second = plan.nodes
    third = node_sourced_from(first, "finalize", "verify")
    return plan.model_copy(
        update={
            "execution_order": ["implement", "verify", "finalize"],
            "nodes": [first, second, third],
        }
    )


def node_sourced_from(
    template: PreflightExecutionNode,
    node_id: str,
    upstream_node_id: str,
) -> PreflightExecutionNode:
    policy = template.workspace_policy
    assert policy is not None
    return template.model_copy(
        update={
            "id": node_id,
            "dependencies": [upstream_node_id],
            "artifact_contract": ArtifactContract(
                stage_path=node_id,
                output_path=f"{node_id}/output.md",
            ),
            "workspace_policy": policy.model_copy(
                update={
                    "source_kind": "node",
                    "source_node_id": upstream_node_id,
                }
            ),
        }
    )


def with_node_setup(
    plan: PreflightExecutionPlan,
    node_id: str,
    commands: list[list[str]],
) -> PreflightExecutionPlan:
    runtime_snapshot = dict(plan.runtime_config_snapshot)
    workspace = dict(runtime_snapshot.get("workspace", {}))
    workspace["setup_profiles"] = {"bootstrap": {"run": commands}}
    runtime_snapshot["workspace"] = workspace

    found = False
    nodes = []
    for node in plan.nodes:
        if node.id != node_id:
            nodes.append(node)
            continue
        found = True
        policy = node.workspace_policy
        assert policy is not None
        nodes.append(
            node.model_copy(
                update={
                    "workspace_policy": policy.model_copy(
                        update={
                            "setup": WorkspaceSetupRecord(
                                profile_name="bootstrap",
                                commands=[
                                    WorkspaceSetupCommandRecord(
                                        argv=argv,
                                        command_index=index,
                                    )
                                    for index, argv in enumerate(commands)
                                ],
                            )
                        }
                    )
                }
            )
        )
    if not found:
        raise AssertionError(f"Unknown test node: {node_id}")
    return plan.model_copy(
        update={
            "nodes": nodes,
            "runtime_config_snapshot": runtime_snapshot,
        }
    )


def workspace_request(
    plan: PreflightExecutionPlan,
    output: OutputManager,
    node_id: str,
    reuse_cache: WorktreeReuseCache,
    limiter: MaterializationLimiter,
) -> WorkspaceInvocationRequest:
    return WorkspaceInvocationRequest(
        plan=plan,
        output=output,
        node_id=node_id,
        task_id="alpha",
        provider="alpha",
        role_label="executor",
        round_num=1,
        audit_round_num=None,
        materialization_limiter=limiter,
        worktree_reuse_cache=reuse_cache,
    )
