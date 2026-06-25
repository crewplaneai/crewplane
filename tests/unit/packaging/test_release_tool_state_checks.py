from __future__ import annotations

# ruff: noqa: E402, I001

from pathlib import Path
import sys

_LOCAL_TEST_DIR = Path(__file__).resolve().parent
if str(_LOCAL_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_TEST_DIR))

import pytest

from scripts.release import publish, state
from test_release_tool_fixtures import (
    FakeRunner,
    append_uv_lock_package,
    constant,
    load_release_script,
    matching_npm,
    matching_pypi,
    no_op,
    release_state_fixture,
    write_manifest,
    write_minimal_repo,
)


def test_release_state_derivation_ready_complete_partial_and_blocked(
    tmp_path: Path,
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path)
    missing_pypi = state.PypiRelease(False, "", {})
    missing_npm = state.NpmRelease(False, "", "", "", "", "", "", "", "")

    ready = state.derive_release_state(
        context, missing_pypi, missing_npm, formula, git, manifest
    )
    assert ready.status == state.ReleaseStatus.READY

    pypi = matching_pypi(context, manifest)
    npm = matching_npm(context, manifest, latest=context.version.npm)
    complete = state.derive_release_state(context, pypi, npm, formula, git, manifest)
    assert complete.status == state.ReleaseStatus.COMPLETE

    partial = state.derive_release_state(
        context, pypi, missing_npm, formula, git, manifest
    )
    assert partial.status == state.ReleaseStatus.PARTIAL
    assert any("release-npm" in item for item in partial.guidance)

    bad_pypi = state.PypiRelease(
        True,
        context.version.python,
        {
            context.sdist_filename: state.PypiFile(
                context.sdist_filename, 10, "9" * 64
            ),
            context.wheel_filename: state.PypiFile(
                context.wheel_filename,
                manifest.artifact("pypi_wheel").size,
                manifest.artifact("pypi_wheel").sha256,
            ),
        },
    )
    blocked = state.derive_release_state(
        context, bad_pypi, missing_npm, formula, git, manifest
    )
    assert blocked.status == state.ReleaseStatus.BLOCKED


def test_complete_state_requires_formula_resources_to_match_requirements(
    tmp_path: Path,
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path)
    pypi = matching_pypi(context, manifest)
    npm = matching_npm(context, manifest, latest=context.version.npm)
    incomplete_formula = state.FormulaState(
        path=formula.path,
        url=formula.url,
        version=formula.version,
        sha256=formula.sha256,
        head_branch=formula.head_branch,
        resources=frozenset({"hatchling"}),
    )
    state_result = state.derive_release_state(
        context, pypi, npm, incomplete_formula, git, manifest
    )
    assert state_result.status == state.ReleaseStatus.PARTIAL
    assert any(
        "missing expected pin metadata" in reason for reason in state_result.reasons
    )


def test_homebrew_resources_include_lock_backed_runtime_transitives(
    tmp_path: Path,
) -> None:
    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)
    resources = state.required_homebrew_resources(context)

    assert {"typer", "click"} <= resources
    assert "tzdata" not in resources


def test_homebrew_resources_include_lock_backed_build_transitives(
    tmp_path: Path,
) -> None:
    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)
    resources = state.required_homebrew_resources(context)

    assert {"hatchling", "packaging"} <= resources


def test_homebrew_build_resource_specs_prefer_wheels_when_available(
    tmp_path: Path,
) -> None:
    write_minimal_repo(tmp_path)
    lock_path = tmp_path / "uv.lock"
    lock_text = lock_path.read_text(encoding="utf-8")
    lock_text = lock_text.replace(
        'sdist = { url = "https://example.com/packaging-0.0.0.tar.gz", hash = "sha256:'
        + "c" * 64
        + '" }',
        'sdist = { url = "https://example.com/packaging-0.0.0.tar.gz", hash = "sha256:'
        + "c" * 64
        + '" }\n'
        + 'wheels = [ { url = "https://example.com/packaging-0.0.0-py3-none-any.whl", hash = "sha256:'
        + "d" * 64
        + '" } ]',
    )
    lock_path.write_text(lock_text, encoding="utf-8")

    context = state.read_release_context(tmp_path)
    specs = state.resource_specs_from_lock(context)

    assert specs["packaging"] == (
        "https://example.com/packaging-0.0.0-py3-none-any.whl",
        "d" * 64,
    )
    assert specs["typer"] == (
        "https://example.com/typer-0.0.0.tar.gz",
        "e" * 64,
    )


def test_declared_formula_build_resources_prefer_wheels_when_available(
    tmp_path: Path,
) -> None:
    write_minimal_repo(tmp_path)
    formula_path = tmp_path / "packaging" / "homebrew" / "Formula" / "crewplane.rb"
    formula = formula_path.read_text(encoding="utf-8").replace(
        "end\n",
        "  def install\n"
        "    build_resources = %w[\n"
        "      packaging\n"
        "    ]\n"
        "  end\n"
        "end\n",
        1,
    )
    formula_path.write_text(formula, encoding="utf-8")
    lock_path = tmp_path / "uv.lock"
    lock_text = lock_path.read_text(encoding="utf-8")
    lock_text = lock_text.replace(
        'sdist = { url = "https://example.com/packaging-0.0.0.tar.gz", hash = "sha256:'
        + "c" * 64
        + '" }',
        'sdist = { url = "https://example.com/packaging-0.0.0.tar.gz", hash = "sha256:'
        + "c" * 64
        + '" }\n'
        + 'wheels = [ { url = "https://example.com/packaging-0.0.0-py3-none-any.whl", hash = "sha256:'
        + "d" * 64
        + '" } ]',
    )
    lock_path.write_text(lock_text, encoding="utf-8")

    context = state.read_release_context(tmp_path)
    specs = state.resource_specs_from_lock(context, {"packaging"})

    assert specs["packaging"] == (
        "https://example.com/packaging-0.0.0-py3-none-any.whl",
        "d" * 64,
    )


def test_release_check_allows_tag_missing_partial_without_pre_publish_smokes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_script = load_release_script()
    context, manifest, formula, git = release_state_fixture(tmp_path)
    pypi = matching_pypi(context, manifest)
    npm = matching_npm(context, manifest, latest=context.version.npm)
    tag_missing_git = state.GitState(
        branch="master",
        default_branch="master",
        head_commit=git.head_commit,
        upstream_ahead=0,
        upstream_behind=0,
        dirty=False,
        tag_commit="",
        remote_tag_commit="",
    )
    monkeypatch.setattr(release_script, "read_release_context", constant(context))
    monkeypatch.setattr(release_script, "read_manifest_if_present", constant(manifest))
    monkeypatch.setattr(release_script, "query_registry_state", constant((pypi, npm)))
    monkeypatch.setattr(release_script, "read_formula_state", constant(formula))
    monkeypatch.setattr(release_script, "inspect_git_state", constant(tag_missing_git))

    def fail_suite(_root: Path, _runner: FakeRunner) -> None:
        del _root, _runner
        raise AssertionError("pre-publish suite should not run")

    monkeypatch.setattr(release_script, "run_pre_publish_checks", fail_suite)
    assert release_script.release_check(tmp_path, FakeRunner()) == 0


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
    called: dict[str, bool] = {"allow_existing_tag": True}

    def capture_state(
        _context: state.ReleaseContext,
        _runner: FakeRunner,
        allow_existing_tag: bool,
    ) -> state.GitState:
        del _context, _runner
        called["allow_existing_tag"] = allow_existing_tag
        raise state.ReleaseError("publish blocked")

    monkeypatch.setattr(publish, "require_clean_publish_git_state", capture_state)
    with pytest.raises(state.ReleaseError, match="publish blocked"):
        publish.publish_pypi(tmp_path, FakeRunner(), execute=True)
    assert called["allow_existing_tag"] is True


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
    called: dict[str, bool] = {"allow_existing_tag": True}

    def capture_state(
        _context: state.ReleaseContext,
        _runner: FakeRunner,
        allow_existing_tag: bool,
    ) -> state.GitState:
        del _context, _runner
        called["allow_existing_tag"] = allow_existing_tag
        raise state.ReleaseError("publish blocked")

    monkeypatch.setattr(publish, "require_clean_publish_git_state", capture_state)
    with pytest.raises(state.ReleaseError, match="publish blocked"):
        publish.publish_npm(tmp_path, FakeRunner(), execute=True)
    assert called["allow_existing_tag"] is True


def test_formula_resource_checks_follow_lock_runtime_graph(
    tmp_path: Path,
) -> None:
    write_minimal_repo(tmp_path)
    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = pyproject.replace(
        'dependencies = ["typer>=0.12.0"]',
        'dependencies = ["typer>=0.12.0", "newdep>=1.0.0"]',
    )
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    append_uv_lock_package(
        tmp_path, "newdep", "https://example.com/newdep-0.0.0.whl", "c" * 64
    )
    lock_path = tmp_path / "uv.lock"
    lock_text = lock_path.read_text(encoding="utf-8").replace(
        '    { name = "typer" },',
        '    { name = "typer" },\n    { name = "newdep" },',
    )
    lock_path.write_text(lock_text, encoding="utf-8")

    context = state.read_release_context(tmp_path)
    formula = state.read_formula_state(context)
    issues = state.verify_formula_state_for_release(context, formula, None)

    assert any(
        "newdep" in issue and "missing expected pin metadata" in issue
        for issue in issues
    )


def test_finalize_creates_tag_when_registries_are_present_and_tag_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path)
    write_manifest(tmp_path, manifest)
    partial_git = state.GitState(
        branch=git.branch,
        default_branch=git.default_branch,
        head_commit=git.head_commit,
        upstream_ahead=0,
        upstream_behind=0,
        dirty=False,
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

    def skip_repo_checks(
        _context: state.ReleaseContext,
        _runner: FakeRunner,
        _allow_existing_tag: bool,
    ) -> state.GitState:
        del _context, _runner, _allow_existing_tag
        return partial_git

    monkeypatch.setattr(publish, "require_clean_publish_git_state", skip_repo_checks)
    monkeypatch.setattr(publish, "inspect_git_state", constant(partial_git))

    tagged: list[str] = []

    def capture_create_tag(
        context_arg: state.ReleaseContext, runner_arg: FakeRunner
    ) -> None:
        del runner_arg
        tagged.append(context_arg.version.tag)

    monkeypatch.setattr(publish, "create_and_push_tag", capture_create_tag)
    assert publish.finalize_release(tmp_path, FakeRunner(), execute=True) == 0
    assert tagged == [context.version.tag]


def test_derive_release_state_is_blocked_on_manifest_identity_mismatch(
    tmp_path: Path,
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path)
    mismatched_manifest = state.ReleaseManifest(
        package_name=manifest.package_name,
        project_version="wrong-version",
        python_version=manifest.python_version,
        npm_version=manifest.npm_version,
        git_tag=manifest.git_tag,
        artifacts=manifest.artifacts,
    )
    state_result = state.derive_release_state(
        context,
        state.PypiRelease(False, "", {}),
        state.NpmRelease(False, "", "", "", "", "", "", "", ""),
        formula,
        git,
        mismatched_manifest,
    )
    assert state_result.status == state.ReleaseStatus.BLOCKED
    assert "release manifest package identity does not match pyproject.toml" in (
        state_result.reasons
    )


def test_artifact_identity_checks_detect_matching_and_mismatching_registries(
    tmp_path: Path,
) -> None:
    context, manifest, _formula, _git = release_state_fixture(tmp_path)
    assert not state.verify_pypi_artifacts(
        context, matching_pypi(context, manifest), manifest
    )
    assert not state.verify_npm_artifact(
        context, matching_npm(context, manifest, latest=context.version.npm), manifest
    )

    bad_npm = matching_npm(context, manifest, latest=context.version.npm, shasum="bad")
    assert "npm shasum" in "\n".join(
        state.verify_npm_artifact(context, bad_npm, manifest)
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


def test_npm_registry_lookup_reads_version_and_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)

    def fake_fetch(_url: str) -> dict[str, object]:
        del _url
        return {
            "dist-tags": {"latest": context.version.npm},
            "versions": {
                context.version.npm: {
                    "name": context.package_name,
                    "version": context.version.npm,
                    "crewplane": {
                        "pythonPackage": context.package_name,
                        "pythonPackageVersion": context.version.project,
                    },
                    "dist": {"shasum": "abc", "integrity": "sha512-abc"},
                }
            },
        }

    monkeypatch.setattr(state.state_types, "fetch_registry_json", fake_fetch)
    npm = state.query_npm_release(context)
    assert npm.exists
    assert npm.latest == context.version.npm
    assert npm.python_package_version == context.version.project


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


def test_completed_release_check_skips_pre_publish_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_script = load_release_script()
    context, manifest, formula, git = release_state_fixture(tmp_path)
    pypi = matching_pypi(context, manifest)
    npm = matching_npm(context, manifest, latest=context.version.npm)
    monkeypatch.setattr(release_script, "read_release_context", constant(context))
    monkeypatch.setattr(release_script, "read_manifest_if_present", constant(manifest))
    monkeypatch.setattr(release_script, "query_registry_state", constant((pypi, npm)))
    monkeypatch.setattr(release_script, "read_formula_state", constant(formula))
    monkeypatch.setattr(release_script, "inspect_git_state", constant(git))

    def fail_suite(_root: Path, _runner: FakeRunner) -> None:
        del _root, _runner
        raise AssertionError("pre-publish suite should not run")

    monkeypatch.setattr(release_script, "run_pre_publish_checks", fail_suite)
    assert release_script.release_check(tmp_path, FakeRunner()) == 0
