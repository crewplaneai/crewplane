from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from packaging.version import InvalidVersion, Version

from scripts.release import state
from tests.unit.packaging.release_tool_support import (
    append_uv_lock_package,
    matching_npm,
    matching_pypi,
    release_state_fixture,
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

    tag_missing_git = replace(git, tag_commit="", remote_tag_commit="")
    ready_before_tagging = state.derive_release_state(
        context, missing_pypi, missing_npm, formula, tag_missing_git, manifest
    )
    assert ready_before_tagging.status == state.ReleaseStatus.READY
    assert "Git tag is missing locally or on origin" not in ready_before_tagging.reasons

    local_tag_only = replace(git, remote_tag_commit="")
    ready_with_partial_tag = state.derive_release_state(
        context, missing_pypi, missing_npm, formula, local_tag_only, manifest
    )
    assert "Git tag is missing locally or on origin" in ready_with_partial_tag.reasons

    unreachable_git = state.GitState(
        branch="release-fix",
        default_branch="master",
        head_commit=git.head_commit,
        head_reachable_from_origin_master=False,
        upstream_ahead=1,
        upstream_behind=0,
        dirty=False,
        tag_commit="",
        remote_tag_commit="",
    )
    unreachable = state.derive_release_state(
        context, missing_pypi, missing_npm, formula, unreachable_git, manifest
    )
    assert unreachable.status == state.ReleaseStatus.BLOCKED
    assert unreachable.reasons == (
        "release commit is not reachable from origin/master",
    )

    pypi = matching_pypi(context, manifest)
    npm = matching_npm(context, manifest, latest=context.version.npm)
    complete = state.derive_release_state(context, pypi, npm, formula, git, manifest)
    assert complete.status == state.ReleaseStatus.COMPLETE

    partial = state.derive_release_state(
        context, pypi, missing_npm, formula, git, manifest
    )
    assert partial.status == state.ReleaseStatus.PARTIAL
    assert any("release-npm" in item for item in partial.guidance)

    partial_before_tagging = state.derive_release_state(
        context, pypi, missing_npm, formula, tag_missing_git, manifest
    )
    assert "Git tag is missing locally or on origin" in partial_before_tagging.reasons

    finalization_pending = state.derive_release_state(
        context, pypi, npm, formula, tag_missing_git, manifest
    )
    assert finalization_pending.guidance == (
        "Rerun make release after fixing Git tag or Homebrew formula state.",
    )

    sdist = manifest.artifact("pypi_sdist")
    partial_pypi = state.PypiRelease(
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
    recoverable_pypi = state.derive_release_state(
        context, partial_pypi, npm, formula, tag_missing_git, manifest
    )
    assert recoverable_pypi.status == state.ReleaseStatus.PARTIAL
    assert (
        recoverable_pypi.reasons.count(f"PyPI is missing {context.wheel_filename}") == 1
    )
    assert any("release-pypi" in item for item in recoverable_pypi.guidance)
    assert not any("release-npm" in item for item in recoverable_pypi.guidance)

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
    assert blocked.guidance == (
        "Remote registry artifacts do not match local manifest; "
        "recover manually before rerunning release commands.",
    )


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


def test_derive_release_state_blocks_noncanonical_pypi_manifest_filename(
    tmp_path: Path,
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path)
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

    state_result = state.derive_release_state(
        context,
        matching_pypi(context, manifest),
        matching_npm(context, manifest, latest=context.version.npm),
        formula,
        git,
        mismatched_manifest,
    )

    assert state_result.status == state.ReleaseStatus.BLOCKED
    assert state_result.reasons == (
        "release manifest pypi_wheel filename does not match release context: "
        f"expected {context.wheel_filename}, found {alternate_filename}",
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


def test_pypi_artifact_verification_rejects_unexpected_files(
    tmp_path: Path,
) -> None:
    context, manifest, _formula, _git = release_state_fixture(tmp_path)
    pypi = matching_pypi(context, manifest)
    unexpected_filename = (
        f"{context.package_name}-{context.version.python}-cp313-cp313-manylinux.whl"
    )
    release_with_unexpected_wheel = state.PypiRelease(
        exists=True,
        version_key=pypi.version_key,
        files={
            **pypi.files,
            unexpected_filename: state.PypiFile(
                unexpected_filename,
                99,
                "f" * 64,
            ),
        },
        latest_stable=pypi.latest_stable,
    )

    assert state.verify_pypi_artifacts(
        context,
        release_with_unexpected_wheel,
        manifest,
    ) == [f"PyPI has unexpected files: {unexpected_filename}"]


def test_pypi_artifact_verification_reports_missing_file_once(
    tmp_path: Path,
) -> None:
    context, manifest, _formula, _git = release_state_fixture(tmp_path)
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

    assert state.verify_pypi_artifacts(context, partial_release, manifest) == [
        f"PyPI is missing {context.wheel_filename}"
    ]


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


def test_pypi_registry_lookup_reports_highest_published_stable_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_repo(tmp_path, "1.2.3")
    context = state.read_release_context(tmp_path)
    releases = {
        "1.2.3": [],
        "2.0.0-alpha.1": [{}],
        "1.9.0": [{}],
        "invalid": [{}],
        "3.0.0": [],
    }

    def fake_fetch(_url: str) -> dict[str, object]:
        del _url
        return {
            "releases": releases,
        }

    monkeypatch.setattr(state.state_types, "fetch_registry_json", fake_fetch)

    release = state.query_pypi_release(context)

    assert release.exists
    stable_versions: list[Version] = []
    for version, files in releases.items():
        if not files:
            continue
        try:
            parsed = Version(version)
        except InvalidVersion:
            continue
        if parsed.is_prerelease:
            continue
        stable_versions.append(parsed)
    assert release.latest_stable == str(max(stable_versions))
