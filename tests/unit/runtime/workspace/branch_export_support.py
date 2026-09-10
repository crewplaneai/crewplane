from __future__ import annotations

from pathlib import Path

from crewplane.artifacts.naming import build_workspace_export_filename


def export_record_path(
    stages_dir: Path, logical_worktree_name: str = "primary"
) -> Path:
    return (
        stages_dir
        / "workspace-exports"
        / build_workspace_export_filename(logical_worktree_name)
    )
