from pathlib import Path

import pytest

from crewplane.core.workspace.cache import (
    paths_overlap,
    workspace_cache_forbidden_roots,
)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("project", "project", True),
        ("project/cache", "project", True),
        ("project", "project/cache", True),
        ("project/../cache", "cache", True),
        ("CAFÉ/cache", "cafe\u0301", True),
        ("project", "project-other", False),
    ],
)
def test_cache_path_overlap(
    tmp_path: Path, left: str, right: str, expected: bool
) -> None:
    assert paths_overlap(tmp_path / left, tmp_path / right) is expected


@pytest.mark.parametrize("with_git", [False, True])
def test_cache_exclusions_preserve_explicit_state_and_optional_git_paths(
    tmp_path: Path, with_git: bool
) -> None:
    project = tmp_path / "project"
    state = tmp_path / "external-state"
    active = tmp_path / "active-git" if with_git else None
    common = tmp_path / "common-git" if with_git else None

    roots = workspace_cache_forbidden_roots(project, state, active, common)

    assert roots == (
        project,
        state,
        state / "execution-stages",
        state / "execution-results",
        state / "locks",
        *((active, common) if with_git else ()),
    )
