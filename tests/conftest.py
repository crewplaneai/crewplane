import os
import time
from collections.abc import Iterator

import pytest

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


@pytest.fixture(scope="session", autouse=True)
def isolated_session_git_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    from tests.helpers.isolated_git import (
        configure_isolated_git_environment,
        require_git,
    )

    floor_bin = os.environ.get("GIT_FLOOR_BIN")
    try:
        with pytest.MonkeyPatch.context() as environment:
            configure_isolated_git_environment(
                environment, tmp_path_factory.mktemp("git-environment")
            )
            if hasattr(time, "tzset"):
                time.tzset()
            if floor_bin is not None:
                environment.setenv("GIT_FLOOR_BIN", floor_bin)
            if os.environ.get("CREWPLANE_REQUIRED_GIT") == "1" or floor_bin is not None:
                require_git(
                    dict(os.environ), required=True, expected_floor_bin=floor_bin
                )
            yield
    finally:
        if hasattr(time, "tzset"):
            time.tzset()
