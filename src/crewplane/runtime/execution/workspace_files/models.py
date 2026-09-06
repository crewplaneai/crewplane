from __future__ import annotations

from dataclasses import dataclass

from crewplane.core.preflight.models import WorkspaceFileLocator
from crewplane.runtime.workspace.worktree import WorktreeSourceRef


@dataclass(frozen=True)
class ResolvedWorkspaceFile:
    locator: WorkspaceFileLocator
    text: str
    byte_size: int
    sha256: str
    source_ref: WorktreeSourceRef | None = None
    git_blob: str | None = None
    git_file_mode: str | None = None
    literal_path_verified: bool = False
    utf8_validated: bool = False
