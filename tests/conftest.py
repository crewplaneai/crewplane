import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def fixed_terminal_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")


@pytest.fixture(scope="session", autouse=True)
def isolated_session_git_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    from tests.helpers.isolated_git import configure_isolated_git_environment

    floor_bin = os.environ.get("GIT_FLOOR_BIN")
    with pytest.MonkeyPatch.context() as environment:
        configure_isolated_git_environment(
            environment, tmp_path_factory.mktemp("git-environment")
        )
        if floor_bin is not None:
            environment.setenv("GIT_FLOOR_BIN", floor_bin)
        yield
