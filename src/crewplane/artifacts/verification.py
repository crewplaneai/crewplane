from __future__ import annotations

import hashlib
from pathlib import Path

from crewplane.architecture.contracts import NodeArtifactRequest, VerifiedNodeArtifact
from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.execution_state import NodeState

from .naming import build_node_state_filename


def read_verified_node_artifact(
    stages_dir: Path,
    results_dir: Path,
    request: NodeArtifactRequest,
    kind: str,
) -> VerifiedNodeArtifact:
    """Read a compiled result only after its successful descriptor is verified."""

    expected_relative: str | None
    if kind == "output":
        expected_relative = request.contract.output_path
    elif kind == "findings":
        expected_relative = request.contract.findings_path
    else:
        raise ValueError(f"Unsupported node artifact kind '{kind}'.")
    if expected_relative is None:
        raise ValueError(f"Node '{request.node_id}' has no {kind} artifact locator.")

    state_path = contained_regular_file(
        stages_dir,
        f"manifests/nodes/{build_node_state_filename(request.node_id)}",
    )
    if state_path is None:
        raise ValueError(
            f"Node '{request.node_id}' has no valid successful state descriptor."
        )
    try:
        state = NodeState.model_validate_json(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Node '{request.node_id}' has no valid successful state descriptor."
        ) from exc
    if state.status != "succeeded" or state.node_id != request.node_id:
        raise ValueError(
            f"Node '{request.node_id}' does not have successful artifact state."
        )
    descriptor = next(
        (item for item in state.artifacts if item.kind == kind),
        None,
    )
    if descriptor is None or descriptor.relative_path != expected_relative:
        raise ValueError(
            f"Node '{request.node_id}' {kind} descriptor does not match its plan."
        )
    artifact_path = contained_regular_file(results_dir, descriptor.relative_path)
    if artifact_path is None:
        raise ValueError(f"Node '{request.node_id}' {kind} artifact is unavailable.")
    try:
        payload = artifact_path.read_bytes()
    except OSError as exc:
        raise ValueError(
            f"Node '{request.node_id}' {kind} artifact is unavailable."
        ) from exc
    size_bytes = len(payload)
    sha256 = hashlib.sha256(payload).hexdigest()
    if size_bytes != descriptor.size_bytes or sha256 != descriptor.sha256:
        raise ValueError(
            f"Node '{request.node_id}' {kind} artifact bytes do not match state."
        )
    return VerifiedNodeArtifact(
        path=artifact_path,
        payload=payload,
        size_bytes=size_bytes,
        sha256=sha256,
    )
