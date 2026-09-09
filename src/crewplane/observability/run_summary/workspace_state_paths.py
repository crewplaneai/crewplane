from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from crewplane.artifacts.naming import build_stage_directory_name
from crewplane.artifacts.workspace.state.paths import workspace_state_candidates


def workspace_state_candidate_paths(stages_dir: Path) -> tuple[Path, ...]:
    if not stages_dir.is_dir():
        return ()
    candidates: list[Path] = []
    for stage_dir in sorted(stages_dir.iterdir()):
        if not stage_dir.is_dir() or stage_dir.is_symlink():
            continue
        candidates.extend(
            path
            for path in sorted(workspace_state_candidates(stage_dir))
            if path.is_file() and not path.is_symlink()
        )
    return tuple(candidates)


def workspace_state_path_matches_node(
    state_path: Path,
    payload: Mapping[str, object],
) -> bool:
    node_id = payload.get("node_id")
    return (
        isinstance(node_id, str)
        and bool(node_id)
        and state_path.parent.name == build_stage_directory_name(node_id)
    )
