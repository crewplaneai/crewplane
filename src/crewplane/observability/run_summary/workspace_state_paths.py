from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.architecture.safe_files import contained_directory
from crewplane.artifacts.naming import build_stage_directory_name
from crewplane.artifacts.workspace.state.paths import workspace_state_candidates


def workspace_state_candidate_paths(
    stages_dir: Path,
    node_requests: Mapping[str, NodeArtifactRequest],
) -> tuple[Path, ...]:
    if not stages_dir.is_dir() or stages_dir.is_symlink():
        return ()
    stage_paths = {path.name for path in stages_dir.iterdir()}
    stage_paths.update(
        request.contract.stage_path
        for request in node_requests.values()
        if request.contract.stage_path is not None
    )
    candidates: list[Path] = []
    for stage_path in sorted(stage_paths):
        try:
            stage_dir = contained_directory(stages_dir, stage_path)
        except (OSError, ValueError):
            continue
        if stage_dir is None:
            continue
        candidates.extend(
            path
            for path in sorted(workspace_state_candidates(stage_dir))
            if path.is_file() and not path.is_symlink()
        )
    return tuple(candidates)


def workspace_state_path_matches_node(
    stages_dir: Path,
    state_path: Path,
    payload: Mapping[str, object],
    node_requests: Mapping[str, NodeArtifactRequest],
) -> bool:
    node_id = payload.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        return False
    request = node_requests.get(node_id)
    stage_path = (
        request.contract.stage_path
        if request is not None
        else build_stage_directory_name(node_id)
    )
    return stage_path is not None and state_path.parent == stages_dir / stage_path
