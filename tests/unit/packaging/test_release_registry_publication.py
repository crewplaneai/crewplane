from __future__ import annotations

import sys
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
    write_manifest,
    write_minimal_repo,
)


def test_publish_pypi_retries_registry_visibility_after_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path)
    write_manifest(tmp_path, manifest)
    pypi_responses = [
        state.PypiRelease(False, "", {}),
        state.PypiRelease(False, "", {}),
        matching_pypi(context, manifest),
    ]
    sleeps: list[int] = []
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_pypi_auth", lambda: None)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(publish, "fail_if_local_artifacts_stale", no_op)
    monkeypatch.setattr(publish.smoke, "post_publish_pypi_check", no_op)
    monkeypatch.setattr(publish.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(state.NpmRelease(False, "", "", "", "", "", "", "", "")),
    )

    def query_pypi(context_arg: state.ReleaseContext) -> state.PypiRelease:
        del context_arg
        return pypi_responses.pop(0)

    monkeypatch.setattr(publish, "query_pypi_release", query_pypi)

    assert publish.publish_pypi(tmp_path, FakeRunner(), execute=True) == 0
    assert sleeps == [2]
    assert not pypi_responses
    assert "PyPI registry verification passed after 1 retry." in capsys.readouterr().out


def test_publish_pypi_recovers_by_uploading_only_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path)
    write_manifest(tmp_path, manifest)
    sdist = manifest.artifact("pypi_sdist")
    partial_release = state.PypiRelease(
        True,
        context.version.python,
        {
            sdist.filename: state.PypiFile(
                sdist.filename,
                sdist.size,
                sdist.sha256,
            )
        },
    )
    pypi_responses = iter([partial_release, matching_pypi(context, manifest)])
    checked_keys: list[tuple[str, ...]] = []

    def query_pypi(context_arg: state.ReleaseContext) -> state.PypiRelease:
        del context_arg
        return next(pypi_responses)

    def capture_checked_keys(
        context_arg: state.ReleaseContext,
        manifest_arg: state.ReleaseManifest,
        keys: tuple[str, ...],
        label: str,
    ) -> None:
        del context_arg, manifest_arg, label
        checked_keys.append(keys)

    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_pypi_auth", no_op)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(publish.smoke, "post_publish_pypi_check", no_op)
    monkeypatch.setattr(
        publish,
        "query_pypi_release",
        query_pypi,
    )
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(state.NpmRelease(False, "", "", "", "", "", "", "", "")),
    )
    monkeypatch.setattr(
        publish,
        "fail_if_local_artifacts_stale",
        capture_checked_keys,
    )
    runner = FakeRunner()

    assert publish.publish_pypi(tmp_path, runner, execute=True) == 0

    wheel_path = str(tmp_path / manifest.artifact("pypi_wheel").path)
    sdist_path = str(tmp_path / manifest.artifact("pypi_sdist").path)
    assert checked_keys == [("pypi_wheel",)]
    assert len(runner.commands) == 1
    assert wheel_path in runner.commands[0]
    assert sdist_path not in runner.commands[0]


def test_publish_pypi_rejects_noncanonical_manifest_filename_before_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, _formula, _git = release_state_fixture(tmp_path)
    state.sync_generated_metadata(context, FakeRunner())
    state.sync_homebrew_formula_metadata(
        context, manifest.artifact("pypi_sdist").sha256
    )
    wheel = manifest.artifact("pypi_wheel")
    alternate_filename = wheel.filename.replace(
        "-py3-none-any.whl", "-1-py3-none-any.whl"
    )
    alternate_wheel = replace(
        wheel,
        path=f"dist/{alternate_filename}",
        filename=alternate_filename,
    )
    mismatched_manifest = replace(
        manifest,
        artifacts={**manifest.artifacts, "pypi_wheel": alternate_wheel},
    )
    write_manifest(tmp_path, mismatched_manifest)

    def fail_registry_query(context_arg: state.ReleaseContext) -> None:
        del context_arg
        pytest.fail("manifest validation must run before registry queries")

    monkeypatch.setattr(publish, "query_pypi_release", fail_registry_query)
    monkeypatch.setattr(publish, "query_npm_release", fail_registry_query)
    runner = FakeRunner()

    with pytest.raises(
        state.ReleaseError,
        match="pypi_wheel filename does not match release context",
    ):
        publish.publish_pypi(tmp_path, runner, execute=True)

    assert not runner.commands


def test_publish_npm_enforces_latest_and_retries_registry_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path)
    write_manifest(tmp_path, manifest)
    npm_responses = [
        state.NpmRelease(False, "", "", "", "", "", "", "", ""),
        state.NpmRelease(False, "", "", "", "", "", "", "", ""),
        matching_npm(context, manifest, latest=context.version.npm),
    ]
    sleeps: list[int] = []
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setenv("NPM_PUBLISH_ARGS", "--tag next")
    monkeypatch.setattr(publish, "require_npm_auth", lambda: None)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(publish, "fail_if_local_artifacts_stale", no_op)
    monkeypatch.setattr(publish.smoke, "post_publish_npm_check", no_op)
    monkeypatch.setattr(
        publish, "resolve_npm_otp", constant(publish.NpmOtp("222222", ""))
    )
    monkeypatch.setattr(publish.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        publish,
        "query_pypi_release",
        constant(matching_pypi(context, manifest)),
    )

    def query_npm(context_arg: state.ReleaseContext) -> state.NpmRelease:
        del context_arg
        return npm_responses.pop(0)

    monkeypatch.setattr(publish, "query_npm_release", query_npm)

    runner = FakeRunner()
    assert publish.publish_npm(tmp_path, runner, execute=True) == 0
    assert sleeps == [2]
    assert not npm_responses
    npm_mutations = [command for command in runner.commands if command[0] == "npm"]
    assert len(npm_mutations) == 1
    assert npm_mutations[0][:2] == ("npm", "publish")
    assert npm_mutations[0][-3:] == ("--tag", "latest", "--otp=222222")
    assert "npm registry verification passed after 1 retry." in capsys.readouterr().out


def test_publish_npm_reconciles_stale_latest_without_republishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path)
    write_manifest(tmp_path, manifest)
    npm_responses = iter(
        [
            matching_npm(context, manifest, latest="previous"),
            matching_npm(context, manifest, latest=context.version.npm),
        ]
    )

    def query_npm(context_arg: state.ReleaseContext) -> state.NpmRelease:
        del context_arg
        return next(npm_responses)

    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_npm_auth", no_op)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(publish.smoke, "post_publish_npm_check", no_op)
    monkeypatch.setattr(
        publish,
        "resolve_npm_otp",
        constant(publish.NpmOtp("", "444444")),
    )
    monkeypatch.setattr(
        publish,
        "query_pypi_release",
        constant(matching_pypi(context, manifest)),
    )
    monkeypatch.setattr(publish, "query_npm_release", query_npm)
    runner = FakeRunner()

    assert publish.publish_npm(tmp_path, runner, execute=True) == 0
    assert runner.commands == [
        (
            "npm",
            "dist-tag",
            "add",
            f"{context.package_name}@{context.version.npm}",
            "latest",
            "--otp=444444",
        )
    ]


def test_registry_verification_success_message_uses_plural_retries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    responses = iter([["missing version"], ["missing version"], []])
    sleeps: list[int] = []
    monkeypatch.setattr(publish.time, "sleep", lambda seconds: sleeps.append(seconds))

    issues = publish.wait_for_registry_verification(
        "npm",
        lambda: next(responses),
        attempts=3,
        initial_delay_seconds=2,
    )

    assert issues == []
    assert sleeps == [2, 4]
    assert (
        "npm registry verification passed after 2 retries." in capsys.readouterr().out
    )


def test_registry_verification_retries_transient_query_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = 0
    sleeps: list[int] = []

    def collect_issues() -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise state.RetryableRegistryError("registry query failed: HTTP 503")
        return []

    monkeypatch.setattr(publish.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert (
        publish.wait_for_registry_verification(
            "npm",
            collect_issues,
            attempts=2,
            initial_delay_seconds=2,
        )
        == []
    )
    assert calls == 2
    assert sleeps == [2]
    output = capsys.readouterr().out
    assert "registry query failed: HTTP 503" in output
    assert "retrying in 2s" in output


def test_registry_verification_does_not_retry_non_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_sleep(seconds: int) -> None:
        del seconds
        pytest.fail("non-transient failures must not be retried")

    monkeypatch.setattr(
        publish.time,
        "sleep",
        fail_sleep,
    )

    with pytest.raises(state.ReleaseError, match="malformed registry response"):
        publish.wait_for_registry_verification(
            "npm",
            lambda: (_ for _ in ()).throw(
                state.ReleaseError("malformed registry response")
            ),
        )


def test_npm_otp_handling_requires_independent_non_tty_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setenv("NPM_OTP", "111111")
    monkeypatch.delenv("NPM_PUBLISH_OTP", raising=False)
    monkeypatch.delenv("NPM_DIST_TAG_OTP", raising=False)
    otp = publish.resolve_npm_otp(npm_exists=False, needs_dist_tag=False)
    assert otp.publish == "111111"
    assert otp.dist_tag == ""

    with pytest.raises(state.ReleaseError, match="separate OTP"):
        publish.resolve_npm_otp(npm_exists=False, needs_dist_tag=True)

    monkeypatch.setenv("NPM_PUBLISH_OTP", "222222")
    monkeypatch.setenv("NPM_DIST_TAG_OTP", "333333")
    otp = publish.resolve_npm_otp(npm_exists=False, needs_dist_tag=True)
    assert otp.publish == "222222"
    assert otp.dist_tag == "333333"

    otp = publish.resolve_npm_otp(npm_exists=True, needs_dist_tag=False)
    assert otp.publish == ""
    assert otp.dist_tag == ""

    monkeypatch.delenv("NPM_DIST_TAG_OTP", raising=False)
    otp = publish.resolve_npm_otp(npm_exists=True, needs_dist_tag=True)
    assert otp.publish == ""
    assert otp.dist_tag == "111111"

    monkeypatch.delenv("NPM_PUBLISH_OTP", raising=False)
    monkeypatch.delenv("NPM_DIST_TAG_OTP", raising=False)
    monkeypatch.delenv("NPM_OTP", raising=False)
    with pytest.raises(state.ReleaseError, match="dist-tag"):
        publish.resolve_npm_otp(npm_exists=True, needs_dist_tag=True)


def test_npm_latest_reconciliation_uses_latest_dist_tag(tmp_path: Path) -> None:
    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)
    runner = FakeRunner()
    publish.reconcile_npm_latest(context, runner, "444444")

    assert runner.commands == [
        (
            "npm",
            "dist-tag",
            "add",
            f"{context.package_name}@{context.version.npm}",
            "latest",
            "--otp=444444",
        )
    ]
