from __future__ import annotations

import json
from pathlib import Path

from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.runtime.workspace.worktree import (
    WorktreeSourceRef,
)


def project_source_ref(source: WorkspaceSourceSnapshot) -> WorktreeSourceRef:
    return WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=source.run_base_commit,
        source_tree=source.source_tree,
    )


def import_owner_state(tmp_path: Path, source: WorkspaceSourceSnapshot) -> Path:
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text(
        json.dumps(
            {
                "run_id": "run-001",
                "run_key_name": "workspace-run-001",
                "node_id": "implement",
                "task_id": "alpha",
                "role": "executor",
                "round_num": 1,
                "audit_round_num": None,
                "git": {"repo_id": source.repository_id},
            }
        ),
        encoding="utf-8",
    )
    return state_path
