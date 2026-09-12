from pathlib import Path

import pytest

from crewplane.core.workspace.cache import (
    paths_overlap,
    workspace_cache_forbidden_roots,
    workspace_cache_root_failure,
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


def test_cache_root_failure_prioritizes_relative_then_symlink_then_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    state = project / ".crewplane"
    cache = tmp_path / "cache"
    cache.symlink_to(project, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    assert workspace_cache_root_failure(Path("cache"), project, state) == "relative"
    assert workspace_cache_root_failure(cache, project, state) == "symlink"
    assert workspace_cache_root_failure(project, project, state) == "overlap"
    assert workspace_cache_root_failure(tmp_path / "separate", project, state) is None


@pytest.mark.parametrize("blocked_location", ["state", "active_git", "common_git"])
def test_cache_root_failure_checks_external_state_and_git_paths(
    tmp_path: Path, blocked_location: str
) -> None:
    project = tmp_path / "project"
    blocked_paths = {
        "state": tmp_path / "external-state",
        "active_git": tmp_path / "active-git",
        "common_git": tmp_path / "common-git",
    }

    assert (
        workspace_cache_root_failure(
            blocked_paths[blocked_location] / "cache",
            project,
            blocked_paths["state"],
            blocked_paths["active_git"],
            blocked_paths["common_git"],
        )
        == "overlap"
    )


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
