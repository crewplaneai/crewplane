from __future__ import annotations

import json
from pathlib import Path

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.workspace.state.contracts import (
    require_workspace_state_contract,
)
from crewplane.core.preflight.models import PreflightExecutionNode
from crewplane.runtime.workspace.state_selection import (
    latest_executor_lineage_state_path,
    required_lineage_state_path,
)


def required_workspace_state(
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
) -> dict[str, object]:
    return load_workspace_state(required_lineage_state_path(output, node))


def latest_executor_workspace_state(
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
) -> dict[str, object]:
    stage_dir = output.get_node_dir(
        NodeArtifactRequest(node.id, node.artifact_contract)
    )
    if stage_dir is None:
        raise RuntimeError(f"Workspace source node has no stage directory: {node.id}.")
    state_path = latest_executor_lineage_state_path(stage_dir)
    if state_path is None:
        raise RuntimeError(
            f"Workspace source node has no succeeded executor state: {node.id}."
        )
    return load_workspace_state(state_path)


def load_workspace_state(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError(f"Invalid workspace state file: {path.as_posix()}") from None
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid workspace state payload: {path.as_posix()}")
    require_workspace_state_contract(payload, "rendering")
    return payload
