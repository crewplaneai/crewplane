"""Locate and copy file-backed test inputs."""

import shutil
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fixture_path(name: str) -> Path:
    return FIXTURES / name


def copy_fixture(name: str, destination: Path) -> Path:
    shutil.copyfile(fixture_path(name), destination)
    return destination
