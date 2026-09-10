from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release import publish, state
from tests.unit.packaging.release_tool_support import (
    FakeRunner,
    constant,
    matching_npm,
    matching_pypi,
    release_state_fixture,
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
    [
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
    ],
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
    [
        ("manifest", "manifest mismatch"),
        ("bundle", "bundle mismatch"),
        ("local", "local artifact mismatch"),
        ("pypi", "PyPI mismatch"),
        ("npm", "npm mismatch"),
        ("formula", "formula mismatch"),
        ("tag", "tag mismatch"),
    ],
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
    [
        ("", "missing or invalid"),
        ("not-a-version", "missing or invalid"),
        ("1.2.3.0", "does not exactly match"),
        ("1.2.2", "older release"),
    ],
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
    [
        "missing-pypi",
        "missing-npm-latest",
        "stale-manifest",
        "mismatched-tag",
        "missing-remote-tag",
        "not-on-origin-master",
    ],
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
