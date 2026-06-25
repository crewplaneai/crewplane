from __future__ import annotations

from .state_checks import (
    _verify_required_homebrew_resource_specs as _verify_homebrew_specs,
)
from .state_checks import (
    verify_npm_artifact,
    verify_pypi_artifacts,
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

    artifact_issues: list[str] = []
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
            artifact_issues.extend(verify_pypi_artifacts(context, pypi, manifest))
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
                    "recover manually before rerunning release commands."
                ),
            )

    formula_issues = verify_formula_state_for_release(context, formula, manifest)
    tag_issues = verify_git_tag_state(git)

    if pypi.exists and npm.exists and npm.latest == context.version.npm:
        if formula_issues or tag_issues:
            reasons.extend(formula_issues)
            reasons.extend(tag_issues)
            guidance.extend(
                guidance_for_missing_side_effects(False, False, npm.latest, context)
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
        reasons.extend(formula_issues)
        reasons.extend(tag_issues)
        guidance.extend(
            guidance_for_missing_side_effects(
                pypi.exists, npm.exists, npm.latest, context
            )
        )
        return DerivedReleaseState(
            ReleaseStatus.PARTIAL, tuple(reasons), tuple(guidance)
        )

    reasons.extend(formula_issues)
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
    return []


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
    if git.tag_commit and git.tag_commit != git.head_commit:
        return ["Git tag points at a different commit"]
    if git.remote_tag_commit and git.remote_tag_commit != git.head_commit:
        return ["remote Git tag points at a different commit"]
    if not git.tag_commit or not git.remote_tag_commit:
        return ["Git tag is missing locally or on origin"]
    return []


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


def publishing_git_issues(git: GitState, allow_existing_tag: bool = True) -> list[str]:
    issues: list[str] = []
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
