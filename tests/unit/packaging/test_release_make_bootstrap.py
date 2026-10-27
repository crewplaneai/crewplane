import os
import shlex
import shutil
import sys
import tomllib
from pathlib import Path

import pytest

from tests.helpers.processes import run_process
from tests.unit.packaging.release_surfaces_support import ROOT


def write_project_version(root: Path, version: str) -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "crewplane"\nversion = "{version}"\n'
        'requires-python = ">=3.13"\n'
        "[project.optional-dependencies]\ndev = []\n"
        '[project.scripts]\ncrewplane = "crewplane.cli.app:app"\n',
        encoding="utf-8",
    )


@pytest.fixture
def bumped_release_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in tuple(os.environ):
        if name.startswith(("UV_", "MAKE")) or name in {"MFLAGS", "VIRTUAL_ENV"}:
            monkeypatch.delenv(name)
    for name, value in {
        "UV_NO_CONFIG": "1",
        "UV_OFFLINE": "1",
        "UV_PYTHON": sys.executable,
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / ".venv"),
    }.items():
        monkeypatch.setenv(name, value)
    shutil.copyfile(ROOT / "Makefile", tmp_path / "Makefile")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "release.py").touch()
    write_project_version(tmp_path, "0.1.0")
    run_process(["uv", "lock"], cwd=tmp_path, check=True)
    write_project_version(tmp_path, "0.2.0")
    return tmp_path


def test_release_prepare_uses_project_version_as_source_of_truth(
    bumped_release_project: Path,
) -> None:
    root = bumped_release_project
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    run_process(
        [
            "make",
            "--no-print-directory",
            "release-prepare",
            f"PYTHON={shlex.quote(sys.executable)}",
            "TEST_PROFILE=local",
        ],
        cwd=root,
        check=True,
    )

    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    package = next(
        package for package in lock["package"] if package["name"] == project["name"]
    )
    assert package["version"] == project["version"]
