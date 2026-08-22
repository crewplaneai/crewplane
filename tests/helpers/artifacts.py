from __future__ import annotations

from crewplane.architecture.contracts import (
    NodeArtifactRequest,
    artifact_contract_for_node,
)


def node_artifact_request(
    node_id: str,
    findings_enabled: bool = False,
) -> NodeArtifactRequest:
    """Build the canonical derived artifact request used by test fixtures."""
    return NodeArtifactRequest(
        node_id,
        artifact_contract_for_node(node_id, findings_enabled=findings_enabled),
    )
