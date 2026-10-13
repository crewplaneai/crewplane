from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers import platforms
from tests.unit.runtime.workspace.cleanup_support import cleanup_git_repo


@pytest.mark.parametrize(
    ("required", "floor_required", "expected_outcome"),
    [
        (False, False, pytest.skip.Exception),
        (True, False, pytest.fail.Exception),
        (False, True, pytest.fail.Exception),
    ],
    ids=["optional-git", "required-git", "required-git-floor"],
)
def test_cleanup_repository_checks_git_before_acquiring_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    required: bool,
    floor_required: bool,
    expected_outcome: type[BaseException],
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "missing-bin"))
    monkeypatch.setenv("CREWPLANE_REQUIRED_GIT", "1" if required else "0")
    monkeypatch.delenv("GIT_FLOOR_BIN", raising=False)
    if floor_required:
        monkeypatch.setenv("GIT_FLOOR_BIN", str(tmp_path / "floor-bin"))

    with pytest.raises(expected_outcome, match="git is unavailable"):
        cleanup_git_repo(tmp_path)

    assert not (tmp_path / "repo").exists()


@pytest.mark.parametrize(
    ("platform", "winerror", "skips"),
    [
        ("nt", 1314, True),
        ("nt", 5, False),
        ("posix", 1314, False),
        ("posix", None, False),
    ],
    ids=[
        "windows-missing-privilege",
        "windows-permission-error",
        "posix-permission-error",
        "io-error",
    ],
)
def test_symlink_capability_skips_only_documented_unavailability(
    monkeypatch, tmp_path, platform, winerror, skips
) -> None:
    error = OSError("unexpected filesystem failure")
    error.winerror = winerror

    def fail_link(self, target, target_is_directory=False):
        del self, target, target_is_directory
        raise error

    monkeypatch.setattr(platforms, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(Path, "symlink_to", fail_link)
    with pytest.raises(pytest.skip.Exception if skips else OSError) as captured:
        platforms.symlink_or_skip(tmp_path / "link", tmp_path / "target")
    if not skips:
        assert captured.value is error


@platforms.requires_posix
@pytest.mark.parametrize(
    "setup_failure", [False, True], ids=["normal-exit", "setup-failure"]
)
def test_session_fixture_initializes_and_restores_process_timezone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    setup_failure: bool,
) -> None:
    import time
    from contextlib import closing
    from datetime import UTC, datetime

    from tests import conftest
    from tests.helpers import isolated_git

    def utc_noon() -> datetime:
        return datetime(2026, 4, 10, 12).astimezone(UTC)

    def unavailable_git(*args, **kwargs):
        del args, kwargs
        assert utc_noon().hour == 12
        raise RuntimeError("injected capability failure")

    try:
        with monkeypatch.context() as environment:
            environment.setenv("TZ", "America/Los_Angeles")
            time.tzset()
            assert utc_noon().hour == 19
            if setup_failure:
                environment.setenv("CREWPLANE_REQUIRED_GIT", "1")
                environment.setattr(isolated_git, "require_git", unavailable_git)
            fixture = conftest.isolated_session_git_environment.__wrapped__(
                tmp_path_factory
            )
            with closing(fixture):
                if setup_failure:
                    with pytest.raises(
                        RuntimeError, match="injected capability failure"
                    ):
                        next(fixture)
                else:
                    next(fixture)
                    assert utc_noon().hour == 12
            assert utc_noon().hour == 19
    finally:
        time.tzset()
