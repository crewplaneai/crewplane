from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest

from scripts.release import build, state
from tests.helpers import isolated_git as git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.unit.packaging.release_tool_support import (
    FakeRunner,
    constant,
    matching_npm,
    matching_pypi,
    no_op,
    write_minimal_repo,
)

isolated_git = git_support.isolated_git


@pytest.fixture
def prepared_release(
    tmp_path: Path, isolated_git: IsolatedGit
) -> tuple[Path, state.ReleaseContext, IsolatedGit]:
    root = tmp_path / "source"
    root.mkdir()
    write_minimal_repo(root, "1.2.3")
    context = state.read_release_context(root)
    state.sync_generated_metadata(context, FakeRunner())
    artifacts: dict[str, state.ArtifactIdentity] = {}
    for key, relative in {
        "pypi_sdist": f"dist/{context.sdist_filename}",
        "pypi_wheel": f"dist/{context.wheel_filename}",
        "npm_tarball": f".release/npm/{context.npm_filename}",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key.encode())
        artifacts[key] = state.artifact_identity(path, root, key)
    state.sync_homebrew_formula_metadata(context, artifacts["pypi_sdist"].sha256)
    build.write_release_manifest(context, artifacts)
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [1.2.3]\n")
    (root / ".gitignore").write_text("dist/\n.release/\n.release-manifests/\n")
    (root / "source.py").write_text('VERSION = "A"\n')
    isolated_git.run(
        tmp_path, "init", "--bare", "--initial-branch=master", "origin.git"
    )
    isolated_git.run(root, "init", "--initial-branch=master")
    isolated_git.run(root, "config", "user.name", "Release Test")
    isolated_git.run(root, "config", "user.email", "release@example.invalid")
    isolated_git.run(root, "add", ".")
    isolated_git.run(root, "commit", "-m", "Prepare release A")
    isolated_git.run(root, "remote", "add", "origin", str(tmp_path / "origin.git"))
    isolated_git.run(root, "push", "-u", "origin", "master")
    return root, context, isolated_git


def unpublished_registries() -> tuple[state.PypiRelease, state.NpmRelease]:
    return (
        state.PypiRelease(False, "", {}),
        state.NpmRelease(False, "", "", "", "", "", "", "", ""),
    )


def test_validation_requires_npm_to_rebuild_the_prepared_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_script: ModuleType,
) -> None:
    monkeypatch.setattr(release_script, "command_exists", constant(False))
    monkeypatch.setattr(release_script.smoke, "install_check", no_op)
    runner = FakeRunner()

    with pytest.raises(release_script.ReleaseError, match="npm is required"):
        release_script.run_pre_publish_checks(tmp_path, runner)

    assert runner.commands == []


@pytest.mark.parametrize("source_commit", [None, 123])
def test_manifest_rejects_invalid_source_commit_types(
    prepared_release: tuple[Path, state.ReleaseContext, IsolatedGit],
    source_commit: object,
) -> None:
    root, _context, _git = prepared_release
    path = root / state.MANIFEST_PATH
    payload = json.loads(path.read_text())
    payload["source_commit"] = source_commit
    path.write_text(json.dumps(payload))

    with pytest.raises(state.ReleaseError, match="source commit must be a string"):
        state.read_manifest(root)


def test_release_check_records_the_validated_commit(
    prepared_release: tuple[Path, state.ReleaseContext, IsolatedGit],
    monkeypatch: pytest.MonkeyPatch,
    release_script: ModuleType,
) -> None:
    root, context, git = prepared_release
    monkeypatch.setattr(
        release_script, "query_registry_state", constant(unpublished_registries())
    )
    monkeypatch.setattr(release_script, "run_pre_publish_checks", no_op)

    assert release_script.release_check(root, state.CommandRunner()) == 0

    expected_commit = git.run_text(root, "rev-parse", "HEAD")
    manifest = json.loads((root / state.MANIFEST_PATH).read_text())
    assert manifest.get("source_commit") == expected_commit
    archive = (
        root / ".release-manifests" / f"release-manifest.{context.version.project}.json"
    )
    assert json.loads(archive.read_text())["source_commit"] == expected_commit


def test_recovery_tags_the_validated_commit_with_local_changes(
    prepared_release: tuple[Path, state.ReleaseContext, IsolatedGit],
    monkeypatch: pytest.MonkeyPatch,
    release_script: ModuleType,
) -> None:
    root, context, git = prepared_release
    monkeypatch.setattr(
        release_script, "query_registry_state", constant(unpublished_registries())
    )
    monkeypatch.setattr(release_script, "run_pre_publish_checks", no_op)
    assert release_script.release_check(root, state.CommandRunner()) == 0
    manifest = state.read_manifest(root)
    pypi = matching_pypi(context, manifest)
    npm = matching_npm(context, manifest, context.version.npm)
    monkeypatch.setattr(release_script, "query_registry_state", constant((pypi, npm)))
    publish = release_script.publish
    monkeypatch.setattr(publish, "query_pypi_release", constant(pypi))
    monkeypatch.setattr(publish, "query_npm_release", constant(npm))
    monkeypatch.setattr(publish.smoke, "post_publish_pypi_check", no_op)
    monkeypatch.setattr(publish.smoke, "post_publish_npm_check", no_op)
    git.run(root, "switch", "-c", "recovery", "--track", "origin/master")
    (root / "source.py").write_text('VERSION = "uncommitted"\n')
    runner = state.CommandRunner()

    assert release_script.release_check(root, runner) == 0
    assert publish.publish_pypi(root, runner, execute=True) == 0
    assert publish.publish_npm(root, runner, execute=True) == 0
    assert publish.finalize_release(root, runner, execute=True) == 0
    assert publish.finalize_release(root, runner, execute=True) == 0

    remote_tag = git.run_text(
        root, "ls-remote", "origin", f"refs/tags/{context.version.tag}^{{}}"
    )
    assert remote_tag.split()[0] == manifest.source_commit
    assert (
        git.run_text(root, "cat-file", "-t", f"refs/tags/{context.version.tag}")
        == "tag"
    )


@pytest.mark.parametrize("change", ["none", "dirty", "commit"])
def test_ci_artifacts_bind_only_to_unchanged_clean_source(
    prepared_release: tuple[Path, state.ReleaseContext, IsolatedGit],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    root, context, git = prepared_release
    source_commit = git.run_text(root, "rev-parse", "HEAD")
    git.run(root, "checkout", "--detach")
    manifest = state.read_manifest(root)
    monkeypatch.setattr(build, "clean_release_outputs", no_op)

    def build_artifacts(
        context_arg: state.ReleaseContext, runner: state.CommandRunner
    ) -> dict[str, state.ArtifactIdentity]:
        del context_arg, runner
        if change != "none":
            (root / "source.py").write_text('VERSION = "B"\n')
        if change == "commit":
            git.run(root, "add", ".")
            git.run(root, "commit", "-m", "Change during build")
        return manifest.artifacts

    monkeypatch.setattr(build, "build_release_artifacts", build_artifacts)

    if change == "none":
        build.release_artifacts(root, state.CommandRunner())
        assert state.read_manifest(root).source_commit == source_commit
    else:
        with pytest.raises(state.ReleaseError, match="source"):
            build.release_artifacts(root, state.CommandRunner())
        assert not state.read_manifest(root).source_commit


@pytest.mark.parametrize("failure", ["suite", "dirty", "commit", "artifact"])
def test_release_check_never_binds_failed_or_changed_inputs(
    prepared_release: tuple[Path, state.ReleaseContext, IsolatedGit],
    monkeypatch: pytest.MonkeyPatch,
    release_script: ModuleType,
    failure: str,
) -> None:
    root, context, git = prepared_release
    monkeypatch.setattr(
        release_script, "query_registry_state", constant(unpublished_registries())
    )

    def validation(root_arg: Path, runner: state.CommandRunner) -> None:
        del root_arg, runner
        if failure == "suite":
            raise release_script.ReleaseError("validation failed")
        if failure == "artifact":
            (root / "dist" / context.wheel_filename).write_bytes(b"changed wheel")
            return
        (root / "source.py").write_text('VERSION = "B"\n')
        if failure == "commit":
            git.run(root, "add", ".")
            git.run(root, "commit", "-m", "Change source during validation")
            git.run(root, "push", "origin", "master")

    monkeypatch.setattr(release_script, "run_pre_publish_checks", validation)

    with pytest.raises(release_script.ReleaseError):
        release_script.release_check(root, state.CommandRunner())

    assert not json.loads((root / state.MANIFEST_PATH).read_text()).get("source_commit")


@pytest.mark.parametrize(
    "operation", ["check", "pypi", "npm", "finalize", "verify", "github-plan"]
)
@pytest.mark.parametrize("source", ["missing", "changed"])
def test_recovery_rejects_unbound_or_different_commits(
    prepared_release: tuple[Path, state.ReleaseContext, IsolatedGit],
    monkeypatch: pytest.MonkeyPatch,
    release_script: ModuleType,
    operation: str,
    source: str,
) -> None:
    root, context, git = prepared_release
    manifest_path = root / state.MANIFEST_PATH
    payload = json.loads(manifest_path.read_text())
    if source == "changed":
        payload["source_commit"] = git.run_text(root, "rev-parse", "HEAD")
        (root / "source.py").write_text('VERSION = "B"\n')
        git.run(root, "add", ".")
        git.run(root, "commit", "-m", "Advance master after publication")
        git.run(root, "push", "origin", "master")
    else:
        payload.pop("source_commit")
    manifest_path.write_text(json.dumps(payload))
    manifest = state.read_manifest(root)
    pypi = matching_pypi(context, manifest)
    npm = matching_npm(context, manifest, context.version.npm)
    monkeypatch.setattr(release_script, "query_registry_state", constant((pypi, npm)))
    publish = release_script.publish
    monkeypatch.setattr(publish, "query_pypi_release", constant(pypi))
    monkeypatch.setattr(publish, "query_npm_release", constant(npm))
    monkeypatch.setattr(publish.smoke, "post_publish_pypi_check", no_op)
    monkeypatch.setattr(publish.smoke, "post_publish_npm_check", no_op)

    if operation == "check":
        assert release_script.release_check(root, state.CommandRunner()) == 1
    elif operation == "verify":
        assert publish.verify_complete_release(root, state.CommandRunner()) == 1
    elif operation == "github-plan":
        with pytest.raises(release_script.ReleaseError, match="source commit"):
            publish.verified_github_release_plan(root, state.CommandRunner())
    else:
        command = {
            "pypi": publish.publish_pypi,
            "npm": publish.publish_npm,
            "finalize": publish.finalize_release,
        }[operation]
        with pytest.raises(
            release_script.ReleaseError, match="source commit|refusing to create"
        ):
            command(root, state.CommandRunner(), execute=True)

    assert not git.run_text(root, "ls-remote", "--tags", "origin")
