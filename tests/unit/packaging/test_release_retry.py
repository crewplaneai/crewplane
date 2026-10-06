from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release import publish, retry, smoke, state
from tests.unit.packaging.release_tool_support import (
    FakeRunner,
    constant,
    matching_npm,
    matching_pypi,
    no_op,
    release_state_fixture,
    write_minimal_repo,
)


@pytest.mark.parametrize("registry", ["pypi", "npm"])
@pytest.mark.parametrize("visible_after", [180, None])
def test_publish_waits_for_registry_processing_without_reuploading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry: str,
    visible_after: int | None,
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path)
    delays: list[int] = []
    missing_pypi = state.PypiRelease(False, "", {})
    missing_npm = state.NpmRelease(False, "", "", "", "", "", "", "", "")
    pypi = matching_pypi(context, manifest)
    npm = matching_npm(context, manifest, context.version.npm)

    def query_pypi(context_arg: state.ReleaseContext) -> state.PypiRelease:
        del context_arg
        if registry == "npm" or (
            visible_after is not None and sum(delays) >= visible_after
        ):
            return pypi
        return missing_pypi

    def query_npm(context_arg: state.ReleaseContext) -> state.NpmRelease:
        del context_arg
        if (
            registry == "npm"
            and visible_after is not None
            and sum(delays) >= visible_after
        ):
            return npm
        return missing_npm

    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(publish, "require_pypi_auth", no_op)
    monkeypatch.setattr(publish, "require_npm_auth", no_op)
    monkeypatch.setattr(publish, "resolve_npm_otp", constant(publish.NpmOtp("", "")))
    monkeypatch.setattr(publish, "fail_if_local_artifacts_stale", no_op)
    monkeypatch.setattr(publish, "query_pypi_release", query_pypi)
    monkeypatch.setattr(publish, "query_npm_release", query_npm)
    monkeypatch.setattr(smoke, "post_publish_pypi_check", no_op)
    monkeypatch.setattr(smoke, "post_publish_npm_check", no_op)
    monkeypatch.setattr(retry.time, "sleep", delays.append)
    command = publish.publish_pypi if registry == "pypi" else publish.publish_npm
    runner = FakeRunner()

    if visible_after is None:
        with pytest.raises(state.ReleaseError, match=f"rerun make release-{registry}"):
            command(tmp_path, runner, execute=True)
        assert sum(delays) == 600
    else:
        assert command(tmp_path, runner, execute=True) == 0
        assert sum(delays) == visible_after

    assert len(runner.commands) == 1
    assert ("upload" if registry == "pypi" else "publish") in runner.commands[0]
    assert max(delays) == 30


@pytest.mark.parametrize("registry", ["pypi", "npm"])
@pytest.mark.parametrize("visible_after", [180, None])
def test_post_publish_installs_use_the_same_extended_retry_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry: str,
    visible_after: int | None,
) -> None:
    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)
    delays: list[int] = []
    monkeypatch.setattr(retry.time, "sleep", delays.append)

    def install(context_arg: state.ReleaseContext, runner: FakeRunner) -> None:
        del context_arg, runner
        if visible_after is None or sum(delays) < visible_after:
            raise state.ReleaseError("package is not installable yet")

    install_name = (
        "remote_pip_install_check" if registry == "pypi" else "remote_npm_install_check"
    )
    monkeypatch.setattr(smoke, install_name, install)
    check = (
        smoke.post_publish_pypi_check
        if registry == "pypi"
        else smoke.post_publish_npm_check
    )

    if visible_after is None:
        with pytest.raises(
            state.ReleaseError, match="post-publish install check did not pass"
        ):
            check(context, FakeRunner())
        assert sum(delays) == 600
    else:
        check(context, FakeRunner())
        assert sum(delays) == visible_after
    assert max(delays) == 30
