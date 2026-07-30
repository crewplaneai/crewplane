import tempfile
from pathlib import Path

import pytest

from tests.helpers.working_directory import temporary_project_cwd


def test_temporary_project_cwd_resolves_alias_and_restores_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_temp_dir = tmp_path / "real"
    real_temp_dir.mkdir()
    alias_temp_dir = tmp_path / "alias"
    alias_temp_dir.symlink_to(real_temp_dir, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", alias_temp_dir.as_posix())
    original_cwd = Path.cwd()

    with temporary_project_cwd() as project_root:
        assert project_root == project_root.resolve()
        assert project_root.parent == real_temp_dir
        assert Path.cwd() == project_root

    assert Path.cwd() == original_cwd
    assert not project_root.exists()


def test_temporary_project_cwd_restores_cwd_after_error() -> None:
    original_cwd = Path.cwd()

    with (
        pytest.raises(RuntimeError, match="expected failure"),
        temporary_project_cwd(),
    ):
        raise RuntimeError("expected failure")

    assert Path.cwd() == original_cwd
