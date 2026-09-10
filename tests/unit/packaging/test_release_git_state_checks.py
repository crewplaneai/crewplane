from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release import publish, state
from tests.helpers.isolated_git import (
    IsolatedGit,
    configure_isolated_git_environment,
    require_git,
)
from tests.unit.packaging.release_tool_support import (
    FakeRunner,
    constant,
    matching_npm,
    matching_pypi,
    no_op,
    release_state_fixture,
    write_manifest,
)


@pytest.fixture
def release_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> IsolatedGit:
    environment = configure_isolated_git_environment(monkeypatch, tmp_path)
    return require_git(environment, required=False)


def test_publish_pypi_checks_git_state_with_existing_pypi_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, manifest, _formula, _git = release_state_fixture(tmp_path)
    write_manifest(tmp_path, manifest)
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_pypi_auth", lambda: None)
    monkeypatch.setattr(
        publish,
        "query_pypi_release",
        constant(state.PypiRelease(True, context.version.python, {})),
    )
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(state.NpmRelease(False, "", "", "", "", "", "", "", "")),
    )
    called: dict[str, bool] = {
        "allow_existing_tag": False,
        "allow_local_changes": False,
    }

    def capture_state(
        _context: state.ReleaseContext,
        _runner: FakeRunner,
        allow_existing_tag: bool,
        allow_local_changes: bool = False,
    ) -> state.GitState:
        del _context, _runner
        called["allow_existing_tag"] = allow_existing_tag
        called["allow_local_changes"] = allow_local_changes
        raise state.ReleaseError("publish blocked")

    monkeypatch.setattr(publish, "require_publish_git_state", capture_state)
    with pytest.raises(state.ReleaseError, match="publish blocked"):
        publish.publish_pypi(tmp_path, FakeRunner(), execute=True)
    assert called["allow_existing_tag"] is True
    assert called["allow_local_changes"] is True


def test_publish_npm_checks_git_state_with_existing_npm_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, manifest, _formula, _git = release_state_fixture(tmp_path)
    write_manifest(tmp_path, manifest)
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_npm_auth", lambda: None)
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(
            state.NpmRelease(
                True,
                context.version.npm,
                context.version.npm,
                context.package_name,
                context.version.npm,
                context.package_name,
                context.version.project,
                "d" * 40,
                "sha512-good",
            )
        ),
    )
    monkeypatch.setattr(
        publish,
        "query_pypi_release",
        constant(state.PypiRelease(False, "", {})),
    )
    called: dict[str, bool] = {
        "allow_existing_tag": False,
        "allow_local_changes": False,
    }

    def capture_state(
        _context: state.ReleaseContext,
        _runner: FakeRunner,
        allow_existing_tag: bool,
        allow_local_changes: bool = False,
    ) -> state.GitState:
        del _context, _runner
        called["allow_existing_tag"] = allow_existing_tag
        called["allow_local_changes"] = allow_local_changes
        raise state.ReleaseError("publish blocked")

    monkeypatch.setattr(publish, "require_publish_git_state", capture_state)
    with pytest.raises(state.ReleaseError, match="publish blocked"):
        publish.publish_npm(tmp_path, FakeRunner(), execute=True)
    assert called["allow_existing_tag"] is True
    assert called["allow_local_changes"] is True


def test_publish_npm_allows_dirty_git_state_when_pypi_is_already_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, manifest, _formula, _git = release_state_fixture(tmp_path)
    write_manifest(tmp_path, manifest)
    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "require_npm_auth", lambda: None)
    monkeypatch.setattr(
        publish, "query_pypi_release", constant(matching_pypi(context, manifest))
    )
    monkeypatch.setattr(
        publish,
        "query_npm_release",
        constant(state.NpmRelease(False, "", "", "", "", "", "", "", "")),
    )
    called: dict[str, bool] = {"allow_existing_tag": True, "allow_local_changes": False}

    def capture_state(
        _context: state.ReleaseContext,
        _runner: FakeRunner,
        allow_existing_tag: bool,
        allow_local_changes: bool = False,
    ) -> state.GitState:
        del _context, _runner
        called["allow_existing_tag"] = allow_existing_tag
        called["allow_local_changes"] = allow_local_changes
        raise state.ReleaseError("publish blocked")

    monkeypatch.setattr(publish, "require_publish_git_state", capture_state)
    with pytest.raises(state.ReleaseError, match="publish blocked"):
        publish.publish_npm(tmp_path, FakeRunner(), execute=True)
    assert called == {"allow_existing_tag": False, "allow_local_changes": True}


def test_publishing_git_issues_allows_local_state_only_when_requested() -> None:
    git = state.GitState(
        branch="release-fix",
        default_branch="master",
        head_commit="abc",
        head_reachable_from_origin_master=True,
        upstream_ahead=1,
        upstream_behind=0,
        dirty=True,
        tag_commit="",
        remote_tag_commit="",
    )

    strict_issues = state.publishing_git_issues(git, True)
    assert "worktree is dirty" in strict_issues
    assert any("origin default" in issue for issue in strict_issues)
    assert "current branch is not synchronized with its upstream" in strict_issues
    assert not state.publishing_git_issues(git, True, True)

    unreachable_git = state.GitState(
        branch="release-fix",
        default_branch="master",
        head_commit="abc",
        head_reachable_from_origin_master=False,
        upstream_ahead=1,
        upstream_behind=0,
        dirty=True,
        tag_commit="",
        remote_tag_commit="",
    )
    assert "release commit is not reachable from origin/master" in (
        state.publishing_git_issues(unreachable_git, True, True)
    )

    conflicting_tag = state.GitState(
        branch="release-fix",
        default_branch="master",
        head_commit="abc",
        head_reachable_from_origin_master=True,
        upstream_ahead=1,
        upstream_behind=0,
        dirty=True,
        tag_commit="different",
        remote_tag_commit="",
    )
    assert (
        "existing Git tag points at a different commit"
        in state.publishing_git_issues(conflicting_tag, True, True)
    )


def test_finalize_allows_local_git_state_when_registries_are_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path)
    write_manifest(tmp_path, manifest)
    partial_git = state.GitState(
        branch="release-fix",
        default_branch=git.default_branch,
        head_commit=git.head_commit,
        head_reachable_from_origin_master=True,
        upstream_ahead=1,
        upstream_behind=0,
        dirty=True,
        tag_commit="",
        remote_tag_commit="",
    )
    pypi = matching_pypi(context, manifest)
    npm = matching_npm(context, manifest, latest=context.version.npm)

    monkeypatch.setattr(publish, "read_release_context", constant(context))
    monkeypatch.setattr(publish, "read_manifest", constant(manifest))
    monkeypatch.setattr(publish, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(publish, "query_pypi_release", constant(pypi))
    monkeypatch.setattr(publish, "query_npm_release", constant(npm))
    monkeypatch.setattr(publish, "read_formula_state", constant(formula))

    called: dict[str, bool] = {
        "allow_existing_tag": False,
        "allow_local_changes": False,
    }

    def capture_repo_checks(
        _context: state.ReleaseContext,
        _runner: FakeRunner,
        allow_existing_tag: bool,
        allow_local_changes: bool = False,
    ) -> state.GitState:
        del _context, _runner
        called["allow_existing_tag"] = allow_existing_tag
        called["allow_local_changes"] = allow_local_changes
        return partial_git

    monkeypatch.setattr(publish, "require_publish_git_state", capture_repo_checks)
    monkeypatch.setattr(publish, "inspect_git_state", constant(partial_git))

    tagged: list[str] = []

    def capture_create_tag(
        context_arg: state.ReleaseContext, runner_arg: FakeRunner
    ) -> None:
        del runner_arg
        tagged.append(context_arg.version.tag)

    monkeypatch.setattr(publish, "create_and_push_tag", capture_create_tag)
    assert publish.finalize_release(tmp_path, FakeRunner(), execute=True) == 0
    assert called == {"allow_existing_tag": True, "allow_local_changes": True}
    assert tagged == [context.version.tag]


def test_create_and_push_tag_rejects_freshly_unreachable_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, _manifest, _formula, git = release_state_fixture(tmp_path)
    unreachable_git = state.GitState(
        branch=git.branch,
        default_branch=git.default_branch,
        head_commit=git.head_commit,
        head_reachable_from_origin_master=False,
        upstream_ahead=git.upstream_ahead,
        upstream_behind=git.upstream_behind,
        dirty=git.dirty,
        tag_commit="",
        remote_tag_commit="",
    )
    monkeypatch.setattr(publish, "inspect_git_state", constant(unreachable_git))
    runner = FakeRunner()

    with pytest.raises(
        state.ReleaseError,
        match="release commit is not reachable from origin/master",
    ):
        publish.create_and_push_tag(context, runner)

    assert runner.commands == []


def test_release_notes_predecessor_uses_target_history_for_delayed_release(
    tmp_path: Path,
    release_git: IsolatedGit,
) -> None:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    repository.mkdir()

    release_git.run_text(tmp_path, "init", "--bare", str(origin))
    release_git.run_text(repository, "init", "-b", "master")
    release_git.run_text(repository, "config", "user.name", "Release Test")
    release_git.run_text(repository, "config", "user.email", "release@example.com")
    release_git.run_text(repository, "remote", "add", "origin", str(origin))

    for version in ("1.0.0", "1.1.0", "1.2.0"):
        (repository / "version.txt").write_text(version, encoding="utf-8")
        release_git.run_text(repository, "add", "version.txt")
        release_git.run_text(repository, "commit", "-m", f"Release {version}")
        release_git.run_text(
            repository, "tag", "-a", f"v{version}", "-m", f"Release {version}"
        )

    release_git.run_text(repository, "push", "origin", "master", "--tags")
    release_git.run_text(repository, "checkout", "--detach", "v1.1.0")

    delayed_context = state.ReleaseContext(
        root=repository,
        package_name="crewplane",
        console_script="crewplane",
        version=state.ReleaseVersion.from_project("1.1.0"),
    )
    assert (
        state.verified_release_notes_start_tag(
            delayed_context,
            state.CommandRunner(),
        )
        == "v1.0.0"
    )

    release_git.run_text(repository, "checkout", "--detach", "v1.0.0")
    first_context = state.ReleaseContext(
        root=repository,
        package_name="crewplane",
        console_script="crewplane",
        version=state.ReleaseVersion.from_project("1.0.0"),
    )
    assert (
        state.verified_release_notes_start_tag(
            first_context,
            state.CommandRunner(),
        )
        == ""
    )


def test_release_commit_ancestry_allows_a_delayed_release_checkout(
    tmp_path: Path,
    release_git: IsolatedGit,
) -> None:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    repository.mkdir()

    release_git.run_text(tmp_path, "init", "--bare", str(origin))
    release_git.run_text(repository, "init", "-b", "master")
    release_git.run_text(repository, "config", "user.name", "Release Test")
    release_git.run_text(repository, "config", "user.email", "release@example.com")
    release_git.run_text(repository, "remote", "add", "origin", str(origin))

    for version in ("1.0.0", "1.1.0"):
        (repository / "version.txt").write_text(version, encoding="utf-8")
        release_git.run_text(repository, "add", "version.txt")
        release_git.run_text(repository, "commit", "-m", f"Release {version}")

    release_git.run_text(repository, "push", "origin", "master")
    release_git.run_text(repository, "checkout", "--detach", "HEAD^")

    assert state.head_reachable_from_origin_master(
        state.CommandRunner(),
        repository,
    )

    release_git.run_text(repository, "checkout", "-b", "unreleased-change")
    (repository / "version.txt").write_text("unreleased", encoding="utf-8")
    release_git.run_text(repository, "add", "version.txt")
    release_git.run_text(repository, "commit", "-m", "Unreleased change")

    assert not state.head_reachable_from_origin_master(
        state.CommandRunner(),
        repository,
    )


def test_local_artifact_identity_rejects_paths_outside_repo(
    tmp_path: Path,
) -> None:
    context, manifest, _formula, _git = release_state_fixture(tmp_path)
    sdist = manifest.artifact("pypi_sdist")
    escaped_artifact = state.ArtifactIdentity(
        key=sdist.key,
        path="../escaped.tar.gz",
        filename=sdist.filename,
        size=sdist.size,
        sha256=sdist.sha256,
    )
    escaped_manifest = state.ReleaseManifest(
        package_name=manifest.package_name,
        project_version=manifest.project_version,
        python_version=manifest.python_version,
        npm_version=manifest.npm_version,
        git_tag=manifest.git_tag,
        artifacts={**manifest.artifacts, "pypi_sdist": escaped_artifact},
    )

    issues = state.verify_local_manifest_artifacts(
        context, escaped_manifest, ("pypi_sdist",)
    )

    assert any("escapes the repository root" in issue for issue in issues)


def test_remote_git_tag_uses_peeled_annotated_tag_commit(tmp_path: Path) -> None:
    class TagRunner:
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
            assert command[:4] == ["git", "ls-remote", "--tags", "origin"]
            return state.CommandResult(
                tuple(command),
                0,
                "tag-object\trefs/tags/v1.0.0\ncommit-sha\trefs/tags/v1.0.0^{}\n",
                "",
            )

    assert state.remote_git_tag_commit(TagRunner(), tmp_path, "v1.0.0") == "commit-sha"


@pytest.mark.parametrize(("merge_base_status", "expected"), [(0, True), (1, False)])
def test_release_commit_reachability_uses_fresh_origin_master(
    tmp_path: Path,
    merge_base_status: int,
    expected: bool,
) -> None:
    class AncestryRunner:
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
            if command_tuple[:2] == ("git", "fetch"):
                return state.CommandResult(command_tuple, 0, "", "")
            assert command_tuple == (
                "git",
                "merge-base",
                "--is-ancestor",
                "HEAD",
                "FETCH_HEAD",
            )
            return state.CommandResult(command_tuple, merge_base_status, "", "")

    runner = AncestryRunner()

    assert state.head_reachable_from_origin_master(runner, tmp_path) is expected
    assert runner.commands == [
        (
            "git",
            "fetch",
            "--quiet",
            "--no-tags",
            "origin",
            "refs/heads/master",
        ),
        (
            "git",
            "merge-base",
            "--is-ancestor",
            "HEAD",
            "FETCH_HEAD",
        ),
    ]
