from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from scripts.release import state
from tests.unit.packaging.release_tool_support import (
    FakeRunner,
    constant,
    matching_npm,
    matching_pypi,
    no_op,
    release_state_fixture,
)


def test_release_check_allows_tag_missing_partial_without_pre_publish_smokes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_script: ModuleType,
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path)
    pypi = matching_pypi(context, manifest)
    npm = matching_npm(context, manifest, latest=context.version.npm)
    tag_missing_git = state.GitState(
        branch="master",
        default_branch="master",
        head_commit=git.head_commit,
        head_reachable_from_origin_master=True,
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


def test_release_check_does_not_treat_partial_pypi_as_tag_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_script: ModuleType,
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path)
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
    npm = matching_npm(context, manifest, latest=context.version.npm)
    tag_missing_git = replace(git, tag_commit="", remote_tag_commit="")
    monkeypatch.setattr(release_script, "read_release_context", constant(context))
    monkeypatch.setattr(release_script, "read_manifest_if_present", constant(manifest))
    monkeypatch.setattr(
        release_script, "query_registry_state", constant((partial_pypi, npm))
    )
    monkeypatch.setattr(release_script, "read_formula_state", constant(formula))
    monkeypatch.setattr(release_script, "inspect_git_state", constant(tag_missing_git))

    assert release_script.release_check(tmp_path, FakeRunner()) == 1


def test_release_check_requires_current_changelog_section_before_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_script: ModuleType,
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path)
    missing_pypi = state.PypiRelease(False, "", {})
    missing_npm = state.NpmRelease(False, "", "", "", "", "", "", "", "")
    tag_missing_git = replace(git, tag_commit="", remote_tag_commit="")
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## {context.version.project}0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release_script, "read_release_context", constant(context))
    monkeypatch.setattr(release_script, "read_manifest_if_present", constant(manifest))
    monkeypatch.setattr(
        release_script, "query_registry_state", constant((missing_pypi, missing_npm))
    )
    monkeypatch.setattr(release_script, "read_formula_state", constant(formula))
    monkeypatch.setattr(release_script, "inspect_git_state", constant(tag_missing_git))
    monkeypatch.setattr(release_script, "fail_if_generated_metadata_stale", no_op)

    def fail_suite(_root: Path, _runner: FakeRunner) -> None:
        del _root, _runner
        raise AssertionError("pre-publish suite should not run")

    monkeypatch.setattr(release_script, "run_pre_publish_checks", fail_suite)

    with pytest.raises(release_script.ReleaseError, match="does not contain a section"):
        release_script.release_check(tmp_path, FakeRunner())


def test_release_check_accepts_dated_current_changelog_heading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_script: ModuleType,
) -> None:
    context, manifest, formula, git = release_state_fixture(tmp_path)
    missing_pypi = state.PypiRelease(False, "", {})
    missing_npm = state.NpmRelease(False, "", "", "", "", "", "", "", "")
    tag_missing_git = replace(git, tag_commit="", remote_tag_commit="")
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{context.version.project}] - 2026-07-27\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release_script, "read_release_context", constant(context))
    monkeypatch.setattr(release_script, "read_manifest_if_present", constant(manifest))
    monkeypatch.setattr(
        release_script, "query_registry_state", constant((missing_pypi, missing_npm))
    )
    monkeypatch.setattr(release_script, "read_formula_state", constant(formula))
    monkeypatch.setattr(release_script, "inspect_git_state", constant(tag_missing_git))
    monkeypatch.setattr(release_script, "fail_if_generated_metadata_stale", no_op)
    suites: list[Path] = []

    def record_suite(root: Path, runner: FakeRunner) -> None:
        del runner
        suites.append(root)

    monkeypatch.setattr(
        release_script,
        "run_pre_publish_checks",
        record_suite,
    )

    assert release_script.release_check(tmp_path, FakeRunner()) == 0
    assert suites == [tmp_path]


def test_changelog_check_accepts_linked_version_heading(
    tmp_path: Path,
    release_script: ModuleType,
) -> None:
    context, _manifest, _formula, _git = release_state_fixture(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{context.version.project}]"
        f"(https://example.test/releases/{context.version.tag}) - 2026-07-27\n",
        encoding="utf-8",
    )

    release_script.changelog_check(tmp_path)


@pytest.mark.parametrize("indentation", ["", " ", "  ", "   "])
def test_changelog_check_accepts_valid_heading_indentation(
    tmp_path: Path,
    indentation: str,
    release_script: ModuleType,
) -> None:
    context, _manifest, _formula, _git = release_state_fixture(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n{indentation}## [{context.version.project}] - 2026-07-27\n",
        encoding="utf-8",
    )

    release_script.changelog_check(tmp_path)


@pytest.mark.parametrize(
    "hidden_section",
    [
        "```markdown\n## [{version}]\n```\n",
        "<!--\n## [{version}]\n-->\n",
    ],
)
def test_changelog_check_rejects_hidden_version_headings(
    tmp_path: Path,
    hidden_section: str,
    release_script: ModuleType,
) -> None:
    context, _manifest, _formula, _git = release_state_fixture(tmp_path)
    changelog = "# Changelog\n\n" + hidden_section.format(
        version=context.version.project
    )
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")

    with pytest.raises(release_script.ReleaseError, match="does not contain a section"):
        release_script.changelog_check(tmp_path)


def test_completed_release_check_skips_pre_publish_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_script: ModuleType,
) -> None:
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
