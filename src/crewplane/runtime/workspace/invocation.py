from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.workspace.state.paths import (
    WORKSPACE_STATE_FILENAME,
    workspace_state_filename,
)
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from crewplane.core.workspace import invocation_identity as _invocation_identity

MAX_INVOCATION_SLUG_CHARS = _invocation_identity.MAX_INVOCATION_SLUG_CHARS
INVOCATION_SLUG_HASH_CHARS = _invocation_identity.INVOCATION_SLUG_HASH_CHARS
invocation_slug = _invocation_identity.invocation_slug
bounded_invocation_slug = _invocation_identity.bounded_invocation_slug


def node_by_id(
    plan: PreflightExecutionPlan,
    node_id: str,
) -> PreflightExecutionNode:
    for node in plan.nodes:
        if node.id == node_id:
            return node
    raise ValueError(f"Compiled plan does not contain node '{node_id}'.")


def workspace_state_path(
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
    slug: str,
    audit_round_num: int | None,
    round_num: int = 1,
) -> Path:
    request = NodeArtifactRequest(node.id, node.artifact_contract)
    stage_dir = output.get_node_dir(request)
    if stage_dir is None:
        stage_dir = output.create_node_dir(request)
    if len(node.provider_records) == 1 and audit_round_num is None and round_num == 1:
        return stage_dir / WORKSPACE_STATE_FILENAME
    return stage_dir / workspace_state_filename(slug)


def workspace_cleanup_on_success(plan: PreflightExecutionPlan) -> bool:
    workspace = plan.runtime_config_snapshot.get("workspace")
    if not isinstance(workspace, dict):
        return True
    value = workspace.get("cleanup_on_success")
    return value if isinstance(value, bool) else True


def controlled_child_environment_required(plan: PreflightExecutionPlan) -> bool:
    invoker = plan.runtime_config_snapshot.get("invoker")
    if not isinstance(invoker, dict):
        return False
    capabilities = invoker.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    workspace = capabilities.get("workspace")
    if not isinstance(workspace, dict):
        return False
    return (
        workspace.get("launch_mode") == "runtime_command_runner"
        and workspace.get("controlled_child_environment") is True
    )
