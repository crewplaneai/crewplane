"""Git-backed workspace facade."""

from .lineage import ensure_source_commit_available
from .orchestration import (
    capture_worktree_result,
    create_worktree_workspace,
    inspect_disposable_worktree,
    remove_worktree_workspace,
)
from .types import WorktreeCaptureRequest, WorktreeSourceRef, WorktreeWorkspace

__all__ = [
    "WorktreeCaptureRequest",
    "WorktreeSourceRef",
    "WorktreeWorkspace",
    "capture_worktree_result",
    "create_worktree_workspace",
    "ensure_source_commit_available",
    "inspect_disposable_worktree",
    "remove_worktree_workspace",
]
