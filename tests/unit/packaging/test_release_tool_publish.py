from __future__ import annotations

# ruff: noqa: E402, I001

from dataclasses import replace
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


def test_verify_complete_release_returns_zero_for_complete_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path)
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(
        publish, "query_pypi_release", constant(matching_pypi(context, manifest))
    )
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(matching_npm(context, manifest, latest=context.version.npm)),
    )
    monkeypatch.setattr(publish, "read_formula_state", constant(formula))
    monkeypatch.setattr(publish, "inspect_release_tag_state", constant(git))

    assert publish.verify_complete_release(tmp_path, FakeRunner()) == 0
    assert (
        publish.verify_complete_release(
            tmp_path, FakeRunner(), expected_tag=context.version.tag
        )
        == 0
    )
    assert "Release state: complete" in capsys.readouterr().out


def test_verify_complete_release_fails_on_expected_tag_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path)
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(
        publish, "query_pypi_release", constant(matching_pypi(context, manifest))
    )
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(matching_npm(context, manifest, latest=context.version.npm)),
    )
    monkeypatch.setattr(publish, "read_formula_state", constant(formula))

    with pytest.raises(
        state.ReleaseError, match="expected workflow tag .* but context declares"
    ):
        publish.verify_complete_release(
            tmp_path, FakeRunner(), expected_tag="v0.0.0-mismatch"
        )


@pytest.mark.parametrize(
    ("version", "npm_latest", "pypi_latest_stable", "expected"),
    (
        (
            "1.2.3",
            "1.2.3",
            "1.2.3",
            "prerelease=false\nlatest=true\nnotes_start_tag=v1.0.0\n",
        ),
        (
            "1.2.3",
            "2.0.0",
            "2.0.0",
            "prerelease=false\nlatest=false\nnotes_start_tag=v1.0.0\n",
        ),
        (
            "1.2.3",
            "2.0.0-alpha.1",
            "1.2.3",
            "prerelease=false\nlatest=true\nnotes_start_tag=v1.0.0\n",
        ),
        (
            "1.2.3-alpha.4",
            "1.2.3-alpha.4",
            "1.1.0",
            "prerelease=true\nlatest=false\nnotes_start_tag=v1.0.0\n",
        ),
        (
            "1.2.3.dev5",
            "1.2.3-dev.5",
            "1.1.0",
            "prerelease=true\nlatest=false\nnotes_start_tag=v1.0.0\n",
        ),
        (
            "1.2.3.post6",
            "1.2.3-post.6",
            "1.2.3.post6",
            "prerelease=false\nlatest=true\nnotes_start_tag=v1.0.0\n",
        ),
    ),
)
def test_github_release_plan_uses_fresh_registry_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    version: str,
    npm_latest: str,
    pypi_latest_stable: str,
    expected: str,
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path, version)
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(
        publish,
        "query_pypi_release",
        constant(
            matching_pypi(
                context,
                manifest,
                latest_stable=pypi_latest_stable,
            )
        ),
    )
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(matching_npm(context, manifest, latest=npm_latest)),
    )
    monkeypatch.setattr(publish, "read_formula_state", constant(formula))
    monkeypatch.setattr(publish, "inspect_release_tag_state", constant(git))
    monkeypatch.setattr(publish, "github_release_bundle_issues", constant([]))
    monkeypatch.setattr(publish, "verify_local_manifest_artifacts", constant([]))
    monkeypatch.setattr(
        publish,
        "verified_release_notes_start_tag",
        constant("v1.0.0"),
    )

    publish.print_github_release_plan(
        tmp_path,
        FakeRunner(),
        expected_tag=context.version.tag,
    )

    assert capsys.readouterr().out == expected


@pytest.mark.parametrize(
    ("failed_check", "expected_error"),
    (
        ("manifest", "manifest mismatch"),
        ("bundle", "bundle mismatch"),
        ("local", "local artifact mismatch"),
        ("pypi", "PyPI mismatch"),
        ("npm", "npm mismatch"),
        ("formula", "formula mismatch"),
        ("tag", "tag mismatch"),
    ),
)
def test_github_release_plan_fails_closed_on_verification_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_check: str,
    expected_error: str,
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path, "1.2.3")
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(
        publish, "query_pypi_release", constant(matching_pypi(context, manifest))
    )
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(matching_npm(context, manifest, latest=context.version.npm)),
    )
    monkeypatch.setattr(publish, "read_formula_state", constant(formula))
    monkeypatch.setattr(publish, "inspect_release_tag_state", constant(git))
    monkeypatch.setattr(publish, "manifest_context_issues", constant([]))
    monkeypatch.setattr(publish, "github_release_bundle_issues", constant([]))
    monkeypatch.setattr(publish, "verify_local_manifest_artifacts", constant([]))
    monkeypatch.setattr(publish, "verify_pypi_artifacts", constant([]))
    monkeypatch.setattr(publish, "verify_npm_artifact", constant([]))
    monkeypatch.setattr(publish, "verify_formula_state_for_release", constant([]))
    monkeypatch.setattr(publish, "verify_git_tag_state", constant([]))
    monkeypatch.setattr(
        publish,
        "verified_release_notes_start_tag",
        constant("v1.0.0"),
    )

    check_name = {
        "manifest": "manifest_context_issues",
        "bundle": "github_release_bundle_issues",
        "local": "verify_local_manifest_artifacts",
        "pypi": "verify_pypi_artifacts",
        "npm": "verify_npm_artifact",
        "formula": "verify_formula_state_for_release",
        "tag": "verify_git_tag_state",
    }[failed_check]
    monkeypatch.setattr(publish, check_name, constant([expected_error]))

    with pytest.raises(state.ReleaseError, match=expected_error):
        publish.verified_github_release_plan(
            tmp_path,
            FakeRunner(),
            expected_tag=context.version.tag,
        )


@pytest.mark.parametrize(
    ("npm_latest", "expected_error"),
    (
        ("", "missing or invalid"),
        ("not-a-version", "missing or invalid"),
        ("1.2.3.0", "does not exactly match"),
        ("1.2.2", "older release"),
    ),
)
def test_github_release_plan_rejects_invalid_or_older_npm_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    npm_latest: str,
    expected_error: str,
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path, "1.2.3")
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(
        publish, "query_pypi_release", constant(matching_pypi(context, manifest))
    )
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(matching_npm(context, manifest, latest=npm_latest)),
    )
    monkeypatch.setattr(publish, "read_formula_state", constant(formula))
    monkeypatch.setattr(publish, "inspect_release_tag_state", constant(git))
    monkeypatch.setattr(publish, "github_release_bundle_issues", constant([]))
    monkeypatch.setattr(publish, "verify_local_manifest_artifacts", constant([]))

    with pytest.raises(state.ReleaseError, match=expected_error):
        publish.verified_github_release_plan(tmp_path, FakeRunner())


def test_github_release_bundle_requires_exact_generated_layout(
    tmp_path: Path,
) -> None:
    context, manifest, _formula, _git = release_state_fixture(tmp_path, "1.2.3")
    for artifact in manifest.artifacts.values():
        path = tmp_path / artifact.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"artifact")

    assert publish.github_release_bundle_issues(context, manifest) == []

    (tmp_path / "dist" / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    assert publish.github_release_bundle_issues(context, manifest) == [
        "release bundle directory has unexpected contents: dist"
    ]


@pytest.mark.parametrize(
    "case",
    (
        "missing-pypi",
        "missing-npm-latest",
        "stale-manifest",
        "mismatched-tag",
        "missing-remote-tag",
        "not-on-origin-master",
    ),
)
def test_verify_complete_release_returns_nonzero_for_incomplete_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path)
    pypi = matching_pypi(context, manifest)
    npm = matching_npm(context, manifest, latest=context.version.npm)
    release_manifest = manifest
    release_git = git
    if case == "missing-pypi":
        pypi = state.PypiRelease(False, "", {})
    elif case == "missing-npm-latest":
        npm = matching_npm(context, manifest, latest="previous")
    elif case == "stale-manifest":
        release_manifest = state.ReleaseManifest(
            package_name=manifest.package_name,
            project_version="0.0.0",
            python_version=manifest.python_version,
            npm_version=manifest.npm_version,
            git_tag=manifest.git_tag,
            artifacts=manifest.artifacts,
        )
    elif case == "mismatched-tag":
        release_git = state.GitState(
            branch="",
            default_branch="",
            head_commit=git.head_commit,
            head_reachable_from_origin_master=True,
            upstream_ahead=0,
            upstream_behind=0,
            dirty=False,
            tag_commit="different",
            remote_tag_commit=git.remote_tag_commit,
        )
    elif case == "missing-remote-tag":
        release_git = state.GitState(
            branch="",
            default_branch="",
            head_commit=git.head_commit,
            head_reachable_from_origin_master=True,
            upstream_ahead=0,
            upstream_behind=0,
            dirty=False,
            tag_commit=git.tag_commit,
            remote_tag_commit="",
        )
    elif case == "not-on-origin-master":
        release_git = state.GitState(
            branch="",
            default_branch="",
            head_commit=git.head_commit,
            head_reachable_from_origin_master=False,
            upstream_ahead=0,
            upstream_behind=0,
            dirty=False,
            tag_commit=git.tag_commit,
            remote_tag_commit=git.remote_tag_commit,
        )

    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(release_manifest))
    monkeypatch.setattr(publish, "query_pypi_release", constant(pypi))
    monkeypatch.setattr(publish, "query_npm_release", constant(npm))
    monkeypatch.setattr(publish, "read_formula_state", constant(formula))
    monkeypatch.setattr(publish, "inspect_release_tag_state", constant(release_git))

    assert publish.verify_complete_release(tmp_path, FakeRunner()) == 1


def test_verify_completed_release_allows_detached_tag_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, formula, _git = release_state_fixture(tmp_path)

    class DetachedTagRunner:
        def __init__(self) -> None:
            self.commands: list[tuple[str, ...]] = []

        def run(
            self,
            command,
            cwd: Path,
            env=None,
            timeout=None,
            capture_output: bool = True,
            check: bool = True,
        ) -> state.CommandResult:
            del cwd, env, timeout, capture_output, check
            command_tuple = tuple(command)
            self.commands.append(command_tuple)
            if command_tuple == ("git", "rev-parse", "HEAD"):
                return state.CommandResult(command_tuple, 0, "abc\n", "")
            if command_tuple == (
                "git",
                "fetch",
                "--quiet",
                "--no-tags",
                "origin",
                "refs/heads/master",
            ):
                return state.CommandResult(command_tuple, 0, "", "")
            if command_tuple == (
                "git",
                "merge-base",
                "--is-ancestor",
                "HEAD",
                "FETCH_HEAD",
            ):
                return state.CommandResult(command_tuple, 0, "", "")
            if command_tuple == (
                "git",
                "rev-parse",
                "-q",
                "--verify",
                f"refs/tags/{context.version.tag}^{{}}",
            ):
                return state.CommandResult(command_tuple, 0, "abc\n", "")
            if command_tuple == (
                "git",
                "ls-remote",
                "--tags",
                "origin",
                f"refs/tags/{context.version.tag}*",
            ):
                return state.CommandResult(
                    command_tuple, 0, f"abc\trefs/tags/{context.version.tag}^{{}}\n", ""
                )
            raise AssertionError(f"unexpected git command: {command_tuple}")

    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(
        publish, "query_pypi_release", constant(matching_pypi(context, manifest))
    )
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(matching_npm(context, manifest, latest=context.version.npm)),
    )
    monkeypatch.setattr(publish, "read_formula_state", constant(formula))

    runner = DetachedTagRunner()
    release_state = publish.verify_completed_release(tmp_path, runner)

    assert release_state.status == state.ReleaseStatus.COMPLETE
    assert ("git", "rev-list", "--left-right", "--count", "@{u}...HEAD") not in (
        runner.commands
    )
    assert (
        "git",
        "fetch",
        "--quiet",
        "--no-tags",
        "origin",
        "refs/heads/master",
    ) in runner.commands
    assert (
        "git",
        "merge-base",
        "--is-ancestor",
        "HEAD",
        "FETCH_HEAD",
    ) in runner.commands


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
    assert sleeps == [1]
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
    )

    assert issues == []
    assert sleeps == [1, 2]
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
        publish.wait_for_registry_verification("npm", collect_issues, attempts=2) == []
    )
    assert calls == 2
    assert sleeps == [1]
    output = capsys.readouterr().out
    assert "registry query failed: HTTP 503" in output
    assert "retrying in 1s" in output


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
