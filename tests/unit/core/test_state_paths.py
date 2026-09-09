from pathlib import Path

import pytest

from crewplane.core.state_paths import (
    FILE_TOKEN_EXCLUDED_ROOTS,
    RUNTIME_ARTIFACT_ROOTS,
    STATE_DIR_NAME,
    ensure_state_dir,
    get_state_dir,
    is_reserved_state_path,
    project_root_from_config_path,
    resolve_state_file,
)


@pytest.mark.parametrize(
    ("path", "runtime_reserved", "file_token_reserved"),
    [
        (".crewplane/execution-stages", True, True),
        (".crewplane/execution-results", True, True),
        (".crewplane/locks", True, True),
        (".crewplane/preflight", False, True),
        (".crewplane/inputs", False, False),
        (".crewplane/config.yml", False, False),
        (".crewplane/execution-stages-old", False, False),
        (".crewplane/preflight-old", False, False),
        (".crewplane-old/locks", False, False),
        ("nested/.crewplane/locks", False, False),
    ],
)
@pytest.mark.parametrize("suffix", ["", "/child"])
def test_reserved_state_path_policy_boundaries(
    path: str, runtime_reserved: bool, file_token_reserved: bool, suffix: str
) -> None:
    assert (
        is_reserved_state_path(path + suffix, RUNTIME_ARTIFACT_ROOTS)
        is runtime_reserved
    )
    assert (
        is_reserved_state_path(path + suffix, FILE_TOKEN_EXCLUDED_ROOTS)
        is file_token_reserved
    )


def test_get_state_dir_uses_project_root(tmp_path: Path) -> None:
    assert get_state_dir(tmp_path) == tmp_path / STATE_DIR_NAME


def test_ensure_state_dir_creates_directory(tmp_path: Path) -> None:
    state_dir = ensure_state_dir(tmp_path)

    assert state_dir == tmp_path / STATE_DIR_NAME
    assert state_dir.is_dir()


def test_resolve_state_file_uses_override_when_provided(tmp_path: Path) -> None:
    override = tmp_path / "custom-config.yml"

    assert resolve_state_file(override, "config.yml", tmp_path) == override


def test_resolve_state_file_uses_state_dir_default(tmp_path: Path) -> None:
    assert resolve_state_file(None, "config.yml", tmp_path) == (
        tmp_path / STATE_DIR_NAME / "config.yml"
    )


def test_project_root_from_config_path_uses_state_parent(tmp_path: Path) -> None:
    config_path = tmp_path / STATE_DIR_NAME / "config.yml"

    assert project_root_from_config_path(config_path) == tmp_path


def test_project_root_from_config_path_allows_external_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "crewplane.yml"

    assert project_root_from_config_path(config_path) == config_path.parent
