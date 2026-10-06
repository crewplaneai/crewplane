from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from scripts.release import publish, state
from tests.unit.packaging.release_tool_support import (
    FakeRunner,
    constant,
    matching_npm,
    matching_pypi,
    no_op,
    release_state_fixture,
)


@pytest.mark.parametrize("problem", ["npm-digest", "missing-wheel"])
def test_finalize_rejects_registry_defects_before_tagging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path)
    git = replace(git, tag_commit="", remote_tag_commit="")
    pypi = matching_pypi(context, manifest)
    npm = matching_npm(context, manifest, context.version.npm)
    if problem == "npm-digest":
        npm = replace(npm, shasum="wrong")
    else:
        pypi = replace(
            pypi, files={context.sdist_filename: pypi.files[context.sdist_filename]}
        )
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "query_pypi_release", constant(pypi))
    monkeypatch.setattr(publish, "query_npm_release", constant(npm))
    monkeypatch.setattr(publish, "read_formula_state", constant(formula))
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(publish, "inspect_git_state", constant(git))
    runner = FakeRunner()

    with pytest.raises(state.ReleaseError, match="refusing to create"):
        publish.finalize_release(tmp_path, runner, execute=True)

    assert runner.commands == []


def test_npm_rejects_missing_pypi_before_authentication_or_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path)
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(
        publish, "query_pypi_release", constant(state.PypiRelease(False, "", {}))
    )
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(state.NpmRelease(False, "", "", "", "", "", "", "", "")),
    )

    def unexpected_authentication() -> None:
        pytest.fail("npm authentication must follow the PyPI prerequisite check")

    monkeypatch.setattr(publish, "require_npm_auth", unexpected_authentication)
    runner = FakeRunner()

    with pytest.raises(state.ReleaseError, match="PyPI version is missing"):
        publish.publish_npm(tmp_path, runner, execute=True)

    assert runner.commands == []


@pytest.mark.parametrize("exists", [False, True])
@pytest.mark.parametrize(
    ("latest", "expected_error"),
    [
        ("1.3.0", "newer release"),
        ("1.3.0-dev.1.post.1", "newer release"),
        ("1.3.0-alpha.1.dev.1.post.1", "newer release"),
        ("invalid-version", "invalid"),
        ("1.3.0-dev.bad.post.1", "invalid"),
    ],
)
def test_npm_rejects_newer_or_invalid_latest_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exists: bool,
    latest: str,
    expected_error: str,
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path, "1.2.3")
    npm = replace(matching_npm(context, manifest, latest), exists=exists)
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(
        publish, "query_pypi_release", constant(matching_pypi(context, manifest))
    )
    monkeypatch.setattr(publish, "query_npm_release", constant(npm))

    def unexpected_authentication() -> None:
        pytest.fail("npm latest must be checked before authentication")

    monkeypatch.setattr(publish, "require_npm_auth", unexpected_authentication)
    runner = FakeRunner()

    with pytest.raises(state.ReleaseError, match=expected_error):
        publish.publish_npm(tmp_path, runner, execute=True)

    assert runner.commands == []


@pytest.mark.parametrize("partial_pypi", [False, True])
@pytest.mark.parametrize(
    ("latest", "expected_error"),
    [("1.3.0", "newer release"), ("invalid-version", "invalid")],
)
def test_pypi_rejects_blocking_npm_latest_before_authentication_or_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    partial_pypi: bool,
    latest: str,
    expected_error: str,
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path, "1.2.3")
    pypi = state.PypiRelease(False, "", {})
    if partial_pypi:
        pypi = matching_pypi(context, manifest)
        pypi = replace(
            pypi, files={context.sdist_filename: pypi.files[context.sdist_filename]}
        )
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(publish, "query_pypi_release", constant(pypi))
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(state.NpmRelease(False, "", latest, "", "", "", "", "", "")),
    )

    def unexpected_authentication() -> None:
        pytest.fail("npm latest must be checked before PyPI authentication")

    monkeypatch.setattr(publish, "require_pypi_auth", unexpected_authentication)
    runner = FakeRunner()

    with pytest.raises(state.ReleaseError, match=expected_error):
        publish.publish_pypi(tmp_path, runner, execute=True)

    assert runner.commands == []


def test_pypi_verifies_completed_upload_when_npm_latest_is_newer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path, "1.2.3")
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(
        publish, "query_pypi_release", constant(matching_pypi(context, manifest))
    )
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(matching_npm(context, manifest, latest="1.3.0")),
    )
    checked: list[state.ReleaseContext] = []

    def verify_install(context_arg: state.ReleaseContext, runner: FakeRunner) -> None:
        del runner
        checked.append(context_arg)

    monkeypatch.setattr(publish.smoke, "post_publish_pypi_check", verify_install)
    runner = FakeRunner()

    assert publish.publish_pypi(tmp_path, runner, execute=True) == 0
    assert checked == [context]
    assert runner.commands == []


@pytest.mark.parametrize("version", ["1.2.3.post1.dev1", "1.2.3a1.post1.dev1"])
@pytest.mark.parametrize("latest", ["1.2.2", "1.2.2-dev.2.post.2"])
def test_npm_publishes_compound_prerelease_and_verifies_retry_without_reupload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    latest: str,
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path, version)
    published = matching_npm(context, manifest, context.version.npm)
    responses = iter(
        [
            replace(published, exists=False, latest=latest),
            published,
            published,
            published,
        ]
    )

    def query_npm(context_arg: state.ReleaseContext) -> state.NpmRelease:
        del context_arg
        return next(responses)

    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(
        publish, "query_pypi_release", constant(matching_pypi(context, manifest))
    )
    monkeypatch.setattr(publish, "query_npm_release", query_npm)
    monkeypatch.setattr(publish, "require_npm_auth", no_op)
    monkeypatch.setattr(publish, "resolve_npm_otp", constant(publish.NpmOtp("", "")))
    monkeypatch.setattr(publish, "fail_if_local_artifacts_stale", no_op)
    monkeypatch.setattr(publish.smoke, "post_publish_npm_check", no_op)
    monkeypatch.delenv("NPM_PUBLISH_ARGS", raising=False)
    runner = FakeRunner()

    assert publish.publish_npm(tmp_path, runner, execute=True) == 0
    assert publish.publish_npm(tmp_path, runner, execute=True) == 0
    assert runner.commands == [
        (
            "npm",
            "publish",
            str(tmp_path / manifest.artifact("npm_tarball").path),
            "--tag",
            "latest",
        )
    ]
