from pathlib import Path

import pytest

from crewplane.core.workspace.cache import (
    REVIEW_WORKTREE_CACHE_LAYOUT,
    SNAPSHOT_CACHE_LAYOUT,
    WORKSPACE_CACHE_LAYOUTS,
    WORKTREE_CACHE_LAYOUT,
    WorkspaceCacheLayout,
    workspace_cache_run_hierarchy,
    workspace_invocation_path,
)


@pytest.mark.parametrize(
    ("layout", "relative", "parent"),
    [
        (WORKTREE_CACHE_LAYOUT, "workspaces/repo/run/invocation", None),
        (SNAPSHOT_CACHE_LAYOUT, "snapshots/repo/run/invocation", None),
        (
            REVIEW_WORKTREE_CACHE_LAYOUT,
            "review-workspaces/repo/run/import-build/invocation",
            "import/build",
        ),
    ],
)
def test_cache_construction_and_recognition_share_fixed_layout(
    tmp_path: Path, layout: WorkspaceCacheLayout, relative: str, parent: str | None
) -> None:
    cache = tmp_path / "cache"
    hierarchy = workspace_cache_run_hierarchy(cache, layout.family, ("repo", "run"))
    path = workspace_invocation_path(hierarchy[-1], "invocation", parent)

    assert path == cache / relative
    assert layout.matches(path.relative_to(cache).parts, ("repo", "run"), parent)
    assert not cache.exists()


@pytest.mark.parametrize(
    ("layout", "relative", "parent"),
    [
        (WORKTREE_CACHE_LAYOUT, "workspaces/repo/run", None),
        (WORKTREE_CACHE_LAYOUT, "workspaces/repo/run/node/invocation", None),
        (WORKTREE_CACHE_LAYOUT, "workspaces/other/run/invocation", None),
        (WORKTREE_CACHE_LAYOUT, "workspaces/repo/other/invocation", None),
        (SNAPSHOT_CACHE_LAYOUT, "workspaces/repo/run/invocation", None),
        (REVIEW_WORKTREE_CACHE_LAYOUT, "review-workspaces/repo/run/invocation", "node"),
        (
            REVIEW_WORKTREE_CACHE_LAYOUT,
            "review-workspaces/repo/run/other/invocation",
            "node",
        ),
        (
            REVIEW_WORKTREE_CACHE_LAYOUT,
            "review-workspaces/repo/run/node/invocation",
            None,
        ),
        (
            REVIEW_WORKTREE_CACHE_LAYOUT,
            "review-workspaces/repo/run/node/invocation/extra",
            "node",
        ),
    ],
)
def test_cache_recognition_rejects_wrong_owner_depth_or_reviewer_parent(
    layout: WorkspaceCacheLayout, relative: str, parent: str | None
) -> None:
    assert not layout.matches(Path(relative).parts, ("repo", "run"), parent)


def test_legacy_cache_layout_has_no_repository_or_node_nesting() -> None:
    legacy = next(
        layout for layout in WORKSPACE_CACHE_LAYOUTS if not layout.repository_scoped
    )

    assert legacy.matches(("workspace-runs", "run", "invocation"), ("repo", "run"))
    assert not legacy.matches(
        ("workspace-runs", "repo", "run", "invocation"), ("repo", "run")
    )
