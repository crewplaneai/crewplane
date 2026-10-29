import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from functools import partial
from pathlib import Path

import pytest

from tests.helpers.execution_environment import directory_environment

_STANDALONE_ROOT = pytest.StashKey[Path]()

pytest_plugins = ("tests.helpers.isolated_git",)


@pytest.fixture
def posix_cli_plans(monkeypatch: pytest.MonkeyPatch) -> None:
    from crewplane.adapters.invokers.cli_invoker import command_strategy
    from crewplane.adapters.invokers.cli_invoker.command_posix import (
        PosixCommandStrategy,
    )

    monkeypatch.setattr(command_strategy, "command_strategy", PosixCommandStrategy)


@pytest.fixture(autouse=True)
def fixed_terminal_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")


def pytest_configure(config: pytest.Config) -> None:
    """Assign controller/worker locations before importing collected tests."""
    from tests.helpers.isolated_git import (
        configure_isolated_git_environment,
        require_git,
    )

    startup_root = os.environ.get("CREWPLANE_TEST_ENV_ROOT")
    if startup_root is None:
        root = Path(tempfile.mkdtemp(prefix="crewplane-pytest-"))
        config.stash[_STANDALONE_ROOT] = root
    else:
        root = Path(startup_root)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "controller")
    environment = pytest.MonkeyPatch()

    def restore() -> None:
        environment.undo()
        if hasattr(time, "tzset"):
            time.tzset()

    config.add_cleanup(restore)
    floor_bin = os.environ.get("GIT_FLOOR_BIN")
    configure_isolated_git_environment(environment, root / worker)
    environment.setattr(tempfile, "tempdir", os.environ["TMPDIR"])
    if hasattr(time, "tzset"):
        time.tzset()
    if floor_bin is not None:
        environment.setenv("GIT_FLOOR_BIN", floor_bin)
    if os.environ.get("CREWPLANE_REQUIRED_GIT") == "1" or floor_bin is not None:
        require_git(dict(os.environ), required=True, expected_floor_bin=floor_bin)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if exitstatus == 0 and _STANDALONE_ROOT in session.config.stash:
        root = session.config.stash[_STANDALONE_ROOT]
        session.config.add_cleanup(partial(shutil.rmtree, root))


@pytest.fixture(autouse=True)
def isolated_test_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("environment")
    with pytest.MonkeyPatch.context() as environment:
        for key, value in directory_environment(root).items():
            environment.setenv(key, value)
        environment.setattr(tempfile, "tempdir", os.environ["TMPDIR"])
        yield root
