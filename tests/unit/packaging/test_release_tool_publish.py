from __future__ import annotations

# ruff: noqa: E402, I001

import sys
from pathlib import Path

_LOCAL_TEST_DIR = Path(__file__).resolve().parent
if str(_LOCAL_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_TEST_DIR))

import pytest

from scripts.release import publish, state
from test_release_tool_fixtures import (
    FakeRunner,
    constant,
    matching_npm,
    matching_pypi,
    no_op,
    release_state_fixture,
    write_manifest,
    write_minimal_repo,
)


def test_publish_commands_without_execute_are_non_publishing_failures(
    tmp_path: Path,
) -> None:
    write_minimal_repo(tmp_path)
    runner = FakeRunner()

    assert publish.publish_pypi(tmp_path, runner, execute=False) == 1
    assert publish.publish_npm(tmp_path, runner, execute=False) == 1
    assert not runner.commands


def test_publish_auth_checks_fail_before_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(publish, "command_exists", constant(True))
    monkeypatch.delenv("TWINE_USERNAME", raising=False)
    monkeypatch.delenv("TWINE_PASSWORD", raising=False)
    monkeypatch.delenv("PYPI_TOKEN", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(state.ReleaseError, match="PyPI credentials"):
        publish.require_pypi_auth()

    monkeypatch.setenv("TWINE_USERNAME", "__token__")
    monkeypatch.setenv("TWINE_PASSWORD", "not-a-token")
    with pytest.raises(state.ReleaseError, match="TWINE_PASSWORD is malformed"):
        publish.require_pypi_auth()

    monkeypatch.delenv("TWINE_PASSWORD", raising=False)
    monkeypatch.setenv("PYPI_TOKEN", "pypi-valid-token")
    assert publish.pypi_upload_env() == {
        "TWINE_USERNAME": "__token__",
        "TWINE_PASSWORD": "pypi-valid-token",
    }

    monkeypatch.setattr(publish, "command_exists", constant(False))
    with pytest.raises(state.ReleaseError, match="npm is required"):
        publish.require_npm_auth()


def test_lost_npm_login_blocks_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish, "command_exists", constant(True))

    class FailedNpmRunner:
        def run(
            self,
            command,
            cwd: Path,
            env=None,
            timeout=None,
            capture_output=True,
            check=True,
        ):
            del cwd, env, timeout, capture_output, check
            return state.CommandResult(tuple(command), 1, "", "not logged in")

    monkeypatch.setattr(publish, "CommandRunner", FailedNpmRunner)
    with pytest.raises(state.ReleaseError, match="npm authentication"):
        publish.require_npm_auth()


def test_registry_recheck_blocks_changed_pypi_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path)
    write_manifest(tmp_path, manifest)
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_pypi_auth", lambda: None)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    changed = state.PypiRelease(
        True,
        context.version.python,
        {
            context.sdist_filename: state.PypiFile(
                context.sdist_filename, 10, "8" * 64
            ),
            context.wheel_filename: state.PypiFile(
                context.wheel_filename,
                manifest.artifact("pypi_wheel").size,
                manifest.artifact("pypi_wheel").sha256,
            ),
        },
    )
    monkeypatch.setattr(publish, "query_pypi_release", constant(changed))

    with pytest.raises(state.ReleaseError, match="PyPI publication is blocked"):
        publish.publish_pypi(tmp_path, FakeRunner(), execute=True)


def test_publish_pypi_blocks_mismatched_npm_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path)
    write_manifest(tmp_path, manifest)
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_pypi_auth", lambda: None)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    changed_npm = state.NpmRelease(
        True,
        context.version.npm,
        "latest",
        context.package_name,
        context.version.npm,
        context.package_name,
        context.version.project,
        "not-a-matching-sha",
        "sha512-good",
    )
    monkeypatch.setattr(publish, "query_npm_release", constant(changed_npm))
    monkeypatch.setattr(
        publish, "query_pypi_release", constant(state.PypiRelease(False, "", {}))
    )

    with pytest.raises(state.ReleaseError, match="PyPI publication is blocked"):
        publish.publish_pypi(tmp_path, FakeRunner(), execute=True)


def test_publish_npm_blocks_mismatched_pypi_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path)
    write_manifest(tmp_path, manifest)
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_npm_auth", lambda: None)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    changed_pypi = state.PypiRelease(
        True,
        context.version.python,
        {
            context.sdist_filename: state.PypiFile(context.sdist_filename, 10, "wrong"),
            context.wheel_filename: state.PypiFile(
                context.wheel_filename,
                manifest.artifact("pypi_wheel").size,
                manifest.artifact("pypi_wheel").sha256,
            ),
        },
    )
    monkeypatch.setattr(publish, "query_pypi_release", constant(changed_pypi))
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(state.NpmRelease(False, "", "", "", "", "", "", "", "")),
    )

    with pytest.raises(state.ReleaseError, match="npm publication is blocked"):
        publish.publish_npm(tmp_path, FakeRunner(), execute=True)


def test_publish_pypi_rehashes_local_artifacts_before_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path)
    write_manifest(tmp_path, manifest)
    sdist = tmp_path / manifest.artifact("pypi_sdist").path
    wheel = tmp_path / manifest.artifact("pypi_wheel").path
    sdist.parent.mkdir(parents=True)
    sdist.write_text("changed sdist", encoding="utf-8")
    wheel.write_text("changed wheel", encoding="utf-8")
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_pypi_auth", lambda: None)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(
        publish,
        "query_pypi_release",
        constant(state.PypiRelease(False, "", {})),
    )
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(state.NpmRelease(False, "", "", "", "", "", "", "", "")),
    )
    runner = FakeRunner()

    with pytest.raises(state.ReleaseError, match="local artifact drift"):
        publish.publish_pypi(tmp_path, runner, execute=True)

    assert runner.commands == []


def test_publish_npm_rehashes_local_tarball_before_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, _formula, git = release_state_fixture(tmp_path)
    write_manifest(tmp_path, manifest)
    tarball = tmp_path / manifest.artifact("npm_tarball").path
    tarball.parent.mkdir(parents=True)
    tarball.write_text("changed npm tarball", encoding="utf-8")
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_npm_auth", lambda: None)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(
        publish,
        "query_pypi_release",
        constant(state.PypiRelease(False, "", {})),
    )
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(state.NpmRelease(False, "", "", "", "", "", "", "", "")),
    )
    runner = FakeRunner()

    with pytest.raises(state.ReleaseError, match="local artifact drift"):
        publish.publish_npm(tmp_path, runner, execute=True)

    assert runner.commands == []


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
    assert sleeps == [1]
    assert not pypi_responses
    assert "PyPI registry verification passed after 1 retry." in capsys.readouterr().out


def test_publish_npm_retries_registry_visibility_after_publish(
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
    monkeypatch.setattr(publish, "require_npm_auth", lambda: None)
    monkeypatch.setattr(publish, "require_publish_git_state", constant(git))
    monkeypatch.setattr(publish, "fail_if_local_artifacts_stale", no_op)
    monkeypatch.setattr(publish.smoke, "post_publish_npm_check", no_op)
    monkeypatch.setattr(publish, "resolve_npm_otp", constant(publish.NpmOtp("", "")))
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

    assert publish.publish_npm(tmp_path, FakeRunner(), execute=True) == 0
    assert sleeps == [1]
    assert not npm_responses
    assert "npm registry verification passed after 1 retry." in capsys.readouterr().out


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
    )

    assert issues == []
    assert sleeps == [1, 2]
    assert (
        "npm registry verification passed after 2 retries." in capsys.readouterr().out
    )


def test_npm_otp_handling_requires_independent_non_tty_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setenv("NPM_OTP", "111111")
    monkeypatch.delenv("NPM_PUBLISH_OTP", raising=False)
    monkeypatch.delenv("NPM_DIST_TAG_OTP", raising=False)
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
