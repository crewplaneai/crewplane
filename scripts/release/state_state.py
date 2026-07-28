from __future__ import annotations

from .state_checks import (
    _verify_required_homebrew_resource_specs as _verify_homebrew_specs,
)
from .state_checks import (
    missing_pypi_artifact_keys,
    verify_npm_artifact,
    verify_present_pypi_artifacts,
)
from .state_types import (
    DerivedReleaseState,
    FormulaState,
    GitState,
    NpmRelease,
    PypiRelease,
    ReleaseContext,
    ReleaseManifest,
    ReleaseStatus,
)

ORIGIN_MASTER_ANCESTRY_ERROR = "release commit is not reachable from origin/master"


def derive_release_state(
    context: ReleaseContext,
    pypi: PypiRelease,
    npm: NpmRelease,
    formula: FormulaState,
    git: GitState | None,
    manifest: ReleaseManifest | None,
) -> DerivedReleaseState:
    reasons: list[str] = []
    guidance: list[str] = []

    manifest_issues = manifest_context_issues(context, manifest) if manifest else []
    if manifest_issues:
        return DerivedReleaseState(
            ReleaseStatus.BLOCKED,
            tuple(manifest_issues),
            (
                "run make release-prepare to regenerate the release manifest and synced metadata",
            ),
        )
    if git is not None and not git.head_reachable_from_origin_master:
        return DerivedReleaseState(
            ReleaseStatus.BLOCKED,
            (ORIGIN_MASTER_ANCESTRY_ERROR,),
            ("Create releases only from commits reachable from origin/master.",),
        )

    artifact_issues: list[str] = []
    missing_pypi_keys: tuple[str, ...] = ()
    if pypi.exists or npm.exists:
        if manifest is None:
            return DerivedReleaseState(
                ReleaseStatus.BLOCKED,
                ("remote release exists but local release manifest is missing",),
                (
                    "Recover manually by verifying registry artifacts before rerunning release commands.",
                ),
            )
        if pypi.exists:
            artifact_issues.extend(
                verify_present_pypi_artifacts(context, pypi, manifest)
            )
            missing_pypi_keys = missing_pypi_artifact_keys(pypi, manifest)
        if npm.exists:
            artifact_issues.extend(verify_npm_artifact(context, npm, manifest))
        if artifact_issues:
            reasons.extend(artifact_issues)
            reasons.extend(verify_formula_state_for_release(context, formula, manifest))
            reasons.extend(verify_git_tag_state(git))
            return DerivedReleaseState(
                ReleaseStatus.BLOCKED,
                tuple(reasons),
                (
                    "Remote registry artifacts do not match local manifest; "
                    "recover manually before rerunning release commands.",
                ),
            )

    formula_issues = verify_formula_state_for_release(context, formula, manifest)
    tag_issues = verify_git_tag_state(git)

    pypi_complete = pypi.exists and not missing_pypi_keys
    missing_pypi_issues = [
        f"PyPI is missing {manifest.artifact(key).filename}"
        for key in missing_pypi_keys
        if manifest is not None
    ]

    if pypi_complete and npm.exists and npm.latest == context.version.npm:
        if formula_issues or tag_issues:
            reasons.extend(formula_issues)
            reasons.extend(tag_issues)
            guidance.extend(
                guidance_for_missing_side_effects(
                    pypi_complete, npm.exists, npm.latest, context
                )
            )
            return DerivedReleaseState(
                ReleaseStatus.PARTIAL, tuple(reasons), tuple(guidance)
            )
        return DerivedReleaseState(
            ReleaseStatus.COMPLETE,
            (
                f"{context.package_name} {context.version.project} is fully published and verified.",
            ),
            (),
        )

    if pypi.exists or npm.exists:
        reasons.extend(missing_pypi_issues)
        reasons.extend(formula_issues)
        reasons.extend(tag_issues)
        guidance.extend(
            guidance_for_missing_side_effects(
                pypi_complete, npm.exists, npm.latest, context
            )
        )
        return DerivedReleaseState(
            ReleaseStatus.PARTIAL, tuple(reasons), tuple(guidance)
        )

    reasons.extend(formula_issues)
    tag_is_expectedly_absent = (
        git is not None and not git.tag_commit and not git.remote_tag_commit
    )
    if not tag_is_expectedly_absent:
        reasons.extend(tag_issues)
    return DerivedReleaseState(ReleaseStatus.READY, tuple(reasons), tuple(guidance))


def manifest_context_issues(
    context: ReleaseContext, manifest: ReleaseManifest
) -> list[str]:
    expected = {
        "package_name": context.package_name,
        "project_version": context.version.project,
        "python_version": context.version.python,
        "npm_version": context.version.npm,
        "git_tag": context.version.tag,
    }
    actual = {
        "package_name": manifest.package_name,
        "project_version": manifest.project_version,
        "python_version": manifest.python_version,
        "npm_version": manifest.npm_version,
        "git_tag": manifest.git_tag,
    }
    if actual != expected:
        return ["release manifest package identity does not match pyproject.toml"]
    return _pypi_manifest_filename_issues(context, manifest)


def _pypi_manifest_filename_issues(
    context: ReleaseContext, manifest: ReleaseManifest
) -> list[str]:
    expected_filenames = {
        "pypi_sdist": context.sdist_filename,
        "pypi_wheel": context.wheel_filename,
    }
    issues: list[str] = []
    for key, expected_filename in expected_filenames.items():
        actual_filename = manifest.artifact(key).filename
        if actual_filename != expected_filename:
            issues.append(
                f"release manifest {key} filename does not match release context: "
                f"expected {expected_filename}, found {actual_filename}"
            )
    return issues


def verify_formula_state_for_release(
    context: ReleaseContext, formula: FormulaState, manifest: ReleaseManifest | None
) -> list[str]:
    issues: list[str] = []
    if formula.version != context.version.project:
        issues.append("Homebrew formula version is missing or stale")
    if formula.url != context.sdist_url:
        issues.append("Homebrew formula sdist URL is missing or stale")
    if formula.head_branch != "master":
        issues.append("Homebrew formula head branch is not master")
    if (
        manifest is not None
        and formula.sha256 != manifest.artifact("pypi_sdist").sha256
    ):
        issues.append("Homebrew formula sdist SHA is missing or stale")
    issues.extend(_verify_homebrew_specs(context, formula))
    return issues


def verify_git_tag_state(git: GitState | None) -> list[str]:
    if git is None:
        return ["Git tag state could not be inspected"]
    issues: list[str] = []
    if not git.head_reachable_from_origin_master:
        issues.append(ORIGIN_MASTER_ANCESTRY_ERROR)
    if git.tag_commit and git.tag_commit != git.head_commit:
        issues.append("Git tag points at a different commit")
    if git.remote_tag_commit and git.remote_tag_commit != git.head_commit:
        issues.append("remote Git tag points at a different commit")
    if not git.tag_commit or not git.remote_tag_commit:
        issues.append("Git tag is missing locally or on origin")
    return issues


def is_tag_only_missing_error(issues: list[str]) -> bool:
    return issues == ["Git tag is missing locally or on origin"]


def guidance_for_missing_side_effects(
    pypi_exists: bool, npm_exists: bool, latest: str, context: ReleaseContext
) -> list[str]:
    guidance: list[str] = []
    if not pypi_exists:
        guidance.append("Run make release-pypi after fixing the PyPI issue.")
    if not npm_exists or latest != context.version.npm:
        guidance.append("Run make release-npm after fixing the npm issue.")
    if pypi_exists and npm_exists and latest == context.version.npm:
        guidance.append(
            "Rerun make release after fixing Git tag or Homebrew formula state."
        )
    return guidance


def publishing_git_issues(
    git: GitState,
    allow_existing_tag: bool = True,
    allow_local_changes: bool = False,
) -> list[str]:
    issues: list[str] = []
    if not git.head_reachable_from_origin_master:
        issues.append(ORIGIN_MASTER_ANCESTRY_ERROR)
    if not allow_local_changes:
        if git.dirty:
            issues.append("worktree is dirty")
        if git.branch != git.default_branch:
            issues.append(
                f"current branch {git.branch!r} is not origin default {git.default_branch!r}"
            )
        if git.upstream_ahead or git.upstream_behind:
            issues.append("current branch is not synchronized with its upstream")
    if git.tag_commit and git.tag_commit != git.head_commit:
        issues.append("existing Git tag points at a different commit")
    if git.remote_tag_commit and git.remote_tag_commit != git.head_commit:
        issues.append("existing remote Git tag points at a different commit")
    if not allow_existing_tag and (git.tag_commit or git.remote_tag_commit):
        issues.append("Git tag already exists before registry publication")
    return issues
