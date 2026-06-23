from pathlib import Path

from crewplane.core.state_paths import (
    STATE_DIR_NAME,
    ensure_state_dir,
    get_state_dir,
    project_root_from_config_path,
    resolve_state_file,
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
