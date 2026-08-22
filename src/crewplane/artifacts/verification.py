from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts import NodeArtifactRequest, VerifiedNodeArtifact
from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.execution_state import ArtifactDescriptor, NodeState

from .naming import build_node_state_filename


def read_verified_node_artifact(
    stages_dir: Path,
    results_dir: Path,
    request: NodeArtifactRequest,
    kind: str,
) -> VerifiedNodeArtifact:
    """Read a compiled result after verifying its persisted descriptor."""

    expected_relative = _expected_relative_path(request, kind)
    state = _load_successful_node_state(stages_dir, request.node_id)
    descriptor = _descriptor_for_artifact(state, kind, expected_relative)
    if descriptor is None:
        raise ValueError(
            f"Node '{request.node_id}' {kind} descriptor does not match its plan."
        )
    artifact_path = contained_regular_file(results_dir, descriptor.relative_path)
    if artifact_path is None:
        raise ValueError(f"Node '{request.node_id}' {kind} artifact is unavailable.")
    payload = _read_payload(artifact_path, request.node_id, kind)
    try:
        return VerifiedNodeArtifact(
            path=artifact_path,
            payload=payload,
            size_bytes=descriptor.size_bytes,
            sha256=descriptor.sha256,
        )
    except ValueError as exc:
        raise ValueError(
            f"Node '{request.node_id}' {kind} artifact bytes do not match state."
        ) from exc


def _expected_relative_path(
    request: NodeArtifactRequest,
    kind: str,
) -> str:
    match kind:
        case "output":
            return request.contract.output_path
        case "findings":
            findings_path = request.contract.findings_path
            if findings_path is None:
                raise ValueError(
                    f"Node '{request.node_id}' has no {kind} artifact locator."
                )
            return findings_path
        case _:
            raise ValueError(f"Unsupported node artifact kind '{kind}'.")


def _load_successful_node_state(stages_dir: Path, node_id: str) -> NodeState:
    state_path = contained_regular_file(
        stages_dir,
        f"manifests/nodes/{build_node_state_filename(node_id)}",
    )
    if state_path is None:
        raise ValueError(f"Node '{node_id}' has no valid successful state descriptor.")
    try:
        state = NodeState.model_validate_json(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Node '{node_id}' has no valid successful state descriptor."
        ) from exc
    if state.status != "succeeded" or state.node_id != node_id:
        raise ValueError(f"Node '{node_id}' does not have successful artifact state.")
    return state


def _descriptor_for_artifact(
    state: NodeState,
    kind: str,
    expected_relative: str,
) -> ArtifactDescriptor | None:
    descriptor = next((item for item in state.artifacts if item.kind == kind), None)
    if descriptor is None or descriptor.relative_path != expected_relative:
        return None
    return descriptor


def _read_payload(path: Path, node_id: str, kind: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Node '{node_id}' {kind} artifact is unavailable.") from exc
