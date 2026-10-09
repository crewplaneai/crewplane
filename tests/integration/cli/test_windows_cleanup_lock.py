import asyncio

import pytest

import crewplane.cli.app as cli
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)
from tests.integration.cli.repeat_force_run_support import create_project


@pytest.mark.parametrize("wrapped", ["direct", "cause", "group", "cancellation"])
def test_windows_retains_lock_on_unconfirmed_cleanup(tmp_path, monkeypatch, wrapped):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("crewplane.core.platform.platform.system", lambda: "Windows")
    project = create_project(tmp_path, None, 1)
    evidence = ProcessDrainError(
        ProcessDrainEvidence(123, None, True, False), "tree cleanup unresolved"
    )
    error: BaseException = evidence
    if wrapped == "cause":
        error = RuntimeError("publication failed")
        error.__cause__ = evidence
    elif wrapped == "group":
        error = ExceptionGroup("parallel", [ValueError("another failure"), evidence])
    elif wrapped == "cancellation":
        error = asyncio.CancelledError()
        error.__cause__ = evidence

    async def failed(*args, **kwargs):
        assert args or kwargs
        raise error

    monkeypatch.setattr(cli, "execute_workflow", failed)
    if wrapped == "cancellation":
        with pytest.raises(asyncio.CancelledError):
            project.run("--no-live")
    else:
        result = project.run("--no-live")
        assert result.exit_code != 0
    assert len(list((tmp_path / ".crewplane/locks").iterdir())) == 1
    retry = project.run("--no-live", "--force")
    assert retry.exit_code != 0
    assert "unsupported on native Windows" in " ".join(retry.output.split())


def test_windows_clean_failure_before_provider_launch_releases_lock(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("crewplane.core.platform.platform.system", lambda: "Windows")
    project = create_project(tmp_path, None, 1)

    async def failed(*args, **kwargs):
        assert args or kwargs
        raise RuntimeError("before any provider starts")

    monkeypatch.setattr(cli, "execute_workflow", failed)
    result = project.run("--no-live")
    assert result.exit_code != 0
    assert not list((tmp_path / ".crewplane/locks").iterdir())
