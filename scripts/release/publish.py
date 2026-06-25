from __future__ import annotations

import os
import shlex
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import smoke
from .state import (
    CommandRunner,
    DerivedReleaseState,
    NpmRelease,
    PypiRelease,
    ReleaseContext,
    ReleaseError,
    ReleaseManifest,
    ReleaseStatus,
    command_exists,
    derive_release_state,
    fail_if_generated_metadata_stale,
    inspect_git_state,
    is_tag_only_missing_error,
    query_npm_release,
    query_pypi_release,
    read_formula_state,
    read_manifest,
    read_release_context,
    require_publish_git_state,
    verify_formula_state_for_release,
    verify_git_tag_state,
    verify_local_manifest_artifacts,
    verify_npm_artifact,
    verify_pypi_artifacts,
)

REGISTRY_VERIFICATION_ATTEMPTS = 6


@dataclass(frozen=True)
class NpmOtp:
    publish: str
    dist_tag: str


def confirm_release(root: Path) -> None:
    context = read_release_context(root)
    prompt = (
        f"Type {context.version.project} to release {context.package_name} "
        "to PyPI and npm latest: "
    )
    answer = input(prompt)
    if answer != context.version.project:
        raise ReleaseError(
            "confirmation did not match pyproject.toml version; aborting release"
        )


def publish_pypi(root: Path, runner: CommandRunner, execute: bool) -> int:
    context = read_release_context(root)
    if not execute:
        print(
            "Dry run only. Re-run with --execute through make release-pypi to upload PyPI artifacts."
        )
        return 1
    manifest = read_manifest(root)
    fail_if_generated_metadata_stale(context, manifest)
    require_pypi_auth()
    pypi = query_pypi_release(context)
    npm = query_npm_release(context)
    require_publish_git_state(
        context,
        runner,
        pypi.exists,
        is_registry_recovery(pypi, npm),
    )
    issues = verify_existing_pypi(context, pypi, manifest)
    issues.extend(verify_existing_npm(context, npm, manifest))
    if issues:
        raise ReleaseError("PyPI publication is blocked:\n  " + "\n  ".join(issues))
    if pypi.exists:
        print(
            f"PyPI already has {context.package_name} {context.version.project}; verified existing files."
        )
        smoke.post_publish_pypi_check(context, runner)
        return 0
    fail_if_local_artifacts_stale(
        context, manifest, ("pypi_sdist", "pypi_wheel"), "PyPI"
    )
    sdist = context.root / manifest.artifact("pypi_sdist").path
    wheel = context.root / manifest.artifact("pypi_wheel").path
    command = [
        sys.executable,
        "-m",
        "twine",
        "upload",
        "--repository",
        os.environ.get("PYPI_REPOSITORY", "pypi"),
    ]
    command.extend(shlex.split(os.environ.get("TWINE_UPLOAD_ARGS", "")))
    command.extend([str(sdist), str(wheel)])
    runner.run(command, cwd=context.root, env=pypi_upload_env(), capture_output=False)
    issues = wait_for_registry_verification(
        "PyPI",
        lambda: pypi_publication_issues(context, manifest),
    )
    if issues:
        raise ReleaseError(
            "PyPI upload completed but verification failed:\n  " + "\n  ".join(issues)
        )
    smoke.post_publish_pypi_check(context, runner)
    return 0


def publish_npm(root: Path, runner: CommandRunner, execute: bool) -> int:
    context = read_release_context(root)
    if not execute:
        print(
            "Dry run only. Re-run with --execute through make release-npm to publish npm."
        )
        return 1
    manifest = read_manifest(root)
    fail_if_generated_metadata_stale(context, manifest)
    require_npm_auth()
    pypi = query_pypi_release(context)
    npm = query_npm_release(context)
    require_publish_git_state(
        context,
        runner,
        npm.exists,
        is_registry_recovery(pypi, npm),
    )
    issues = verify_existing_npm(context, npm, manifest)
    issues.extend(verify_existing_pypi(context, pypi, manifest))
    if issues:
        raise ReleaseError("npm publication is blocked:\n  " + "\n  ".join(issues))
    needs_dist_tag = npm.exists is False or npm.latest != context.version.npm
    if not npm.exists:
        fail_if_local_artifacts_stale(context, manifest, ("npm_tarball",), "npm")
    otp = resolve_npm_otp(npm_exists=npm.exists, needs_dist_tag=needs_dist_tag)
    if not npm.exists:
        package = context.root / manifest.artifact("npm_tarball").path
        command = ["npm", "publish", str(package), "--tag", "latest"]
        command.extend(npm_extra_args())
        command.extend(otp_arg(otp.publish))
        runner.run(command, cwd=context.root, capture_output=False)
    if needs_dist_tag:
        reconcile_npm_latest(context, runner, otp.dist_tag)
    issues = wait_for_registry_verification(
        "npm",
        lambda: npm_publication_issues(context, manifest),
    )
    if issues:
        raise ReleaseError(
            "npm publish completed but verification failed:\n  " + "\n  ".join(issues)
        )
    smoke.post_publish_npm_check(context, runner)
    return 0


def finalize_release(root: Path, runner: CommandRunner, execute: bool) -> int:
    context = read_release_context(root)
    if not execute:
        print("Dry run only. Re-run with --execute to create and push the release tag.")
        return 1
    manifest = read_manifest(root)
    fail_if_generated_metadata_stale(context, manifest)
    pypi = query_pypi_release(context)
    npm = query_npm_release(context)
    git = require_publish_git_state(
        context, runner, True, is_registry_recovery(pypi, npm)
    )
    formula = read_formula_state(context)
    state = derive_release_state(context, pypi, npm, formula, git, manifest)
    if state.status == ReleaseStatus.COMPLETE:
        print(f"{context.version.tag} already exists locally and on origin.")
        return 0
    formula_issues = verify_formula_state_for_release(context, formula, manifest)
    tag_issues = verify_git_tag_state(git)
    if not is_tag_only_missing_error(tag_issues):
        raise ReleaseError(
            "registries are not fully verified; refusing to create the Git tag"
        )
    if (
        pypi.exists
        and npm.exists
        and npm.latest == context.version.npm
        and not formula_issues
    ):
        create_and_push_tag(context, runner)
        return 0
    raise ReleaseError(
        "registries are not fully verified; refusing to create the Git tag"
    )


def verify_completed_release(root: Path, runner: CommandRunner) -> DerivedReleaseState:
    context = read_release_context(root)
    manifest = read_manifest(root)
    pypi = query_pypi_release(context)
    npm = query_npm_release(context)
    formula = read_formula_state(context)
    git = inspect_git_state(context, runner)
    return derive_release_state(context, pypi, npm, formula, git, manifest)


def is_registry_recovery(pypi: PypiRelease, npm: NpmRelease) -> bool:
    return pypi.exists or npm.exists


def verify_existing_pypi(
    context: ReleaseContext, release: PypiRelease, manifest
) -> list[str]:
    if not release.exists:
        return []
    return verify_pypi_artifacts(context, release, manifest)


def verify_existing_npm(
    context: ReleaseContext, release: NpmRelease, manifest
) -> list[str]:
    if not release.exists:
        return []
    return verify_npm_artifact(context, release, manifest)


def pypi_publication_issues(
    context: ReleaseContext,
    manifest: ReleaseManifest,
) -> list[str]:
    pypi = query_pypi_release(context)
    npm = query_npm_release(context)
    issues = verify_pypi_artifacts(context, pypi, manifest)
    issues.extend(verify_existing_npm(context, npm, manifest))
    return issues


def npm_publication_issues(
    context: ReleaseContext,
    manifest: ReleaseManifest,
) -> list[str]:
    npm = query_npm_release(context)
    pypi = query_pypi_release(context)
    issues = verify_npm_artifact(context, npm, manifest)
    issues.extend(verify_pypi_artifacts(context, pypi, manifest))
    if npm.latest != context.version.npm:
        issues.append("npm latest dist-tag does not point at the release version")
    return issues


def wait_for_registry_verification(
    label: str,
    collect_issues: Callable[[], list[str]],
    attempts: int = REGISTRY_VERIFICATION_ATTEMPTS,
) -> list[str]:
    issues: list[str] = []
    for attempt in range(1, attempts + 1):
        issues = collect_issues()
        if not issues:
            if attempt > 1:
                retries = attempt - 1
                suffix = "retry" if retries == 1 else "retries"
                print(f"{label} registry verification passed after {retries} {suffix}.")
            return []
        if attempt == attempts:
            return issues
        print(
            f"{label} registry verification did not pass; "
            f"retrying ({attempt}/{attempts})."
        )
        time.sleep(2 ** (attempt - 1))
    return issues


def fail_if_local_artifacts_stale(
    context: ReleaseContext,
    manifest: ReleaseManifest,
    keys: tuple[str, ...],
    registry_label: str,
) -> None:
    issues = verify_local_manifest_artifacts(context, manifest, keys)
    if issues:
        raise ReleaseError(
            f"{registry_label} publication is blocked by local artifact drift:\n  "
            + "\n  ".join(issues)
        )


def require_pypi_auth() -> None:
    if not command_exists("twine"):
        raise ReleaseError("twine is required for PyPI publishing")
    username = os.environ.get("TWINE_USERNAME", "")
    password = os.environ.get("TWINE_PASSWORD", "")
    token = os.environ.get("PYPI_TOKEN", "")
    if token and not token.startswith("pypi-"):
        raise ReleaseError(
            "PYPI_TOKEN is malformed; expected a PyPI token beginning with pypi-"
        )
    if username == "__token__" and password and not password.startswith("pypi-"):
        raise ReleaseError("TWINE_PASSWORD is malformed for TWINE_USERNAME=__token__")
    if not sys.stdin.isatty() and not token and not (username and password):
        raise ReleaseError(
            "PyPI credentials are required in non-TTY mode. Set TWINE_USERNAME=__token__ "
            "and TWINE_PASSWORD to a fresh PyPI token."
        )


def pypi_upload_env() -> dict[str, str]:
    token = os.environ.get("PYPI_TOKEN", "")
    if token and not os.environ.get("TWINE_PASSWORD"):
        return {"TWINE_USERNAME": "__token__", "TWINE_PASSWORD": token}
    return {}


def require_npm_auth() -> None:
    if not command_exists("npm"):
        raise ReleaseError("npm is required for npm publishing")
    runner = CommandRunner()
    result = runner.run(
        ["npm", "whoami"], cwd=Path.cwd(), capture_output=True, check=False
    )
    if result.returncode != 0:
        raise ReleaseError("npm authentication is required before publishing")


def resolve_npm_otp(*, npm_exists: bool, needs_dist_tag: bool) -> NpmOtp:
    single = os.environ.get("NPM_OTP", "") or os.environ.get("NPM_CONFIG_OTP", "")
    publish = os.environ.get("NPM_PUBLISH_OTP", "")
    dist_tag = os.environ.get("NPM_DIST_TAG_OTP", "")
    needs_publish = not npm_exists
    if not sys.stdin.isatty():
        if needs_publish and needs_dist_tag:
            if not (publish and dist_tag):
                raise ReleaseError(
                    "npm publish and npm dist-tag add may each consume a separate OTP. "
                    "In non-TTY mode set both NPM_PUBLISH_OTP and NPM_DIST_TAG_OTP, "
                    "or use an interactive terminal."
                )
            return NpmOtp(publish=publish, dist_tag=dist_tag)
        if needs_publish:
            publish_otp = publish or single
            if not publish_otp:
                raise ReleaseError(
                    "npm publish in non-TTY mode requires a one-time password."
                )
            return NpmOtp(publish=publish_otp, dist_tag="")
        if needs_dist_tag:
            dist_tag_otp = dist_tag or single
            if not dist_tag_otp:
                raise ReleaseError(
                    "npm dist-tag add in non-TTY mode requires a one-time password."
                )
            return NpmOtp(publish="", dist_tag=dist_tag_otp)
        return NpmOtp(publish="", dist_tag="")
    publish_otp = (publish or single) if needs_publish else ""
    dist_tag_otp = (dist_tag or single) if needs_dist_tag else ""
    return NpmOtp(publish=publish_otp, dist_tag=dist_tag_otp)


def reconcile_npm_latest(
    context: ReleaseContext, runner: CommandRunner, otp: str
) -> None:
    command = [
        "npm",
        "dist-tag",
        "add",
        f"{context.package_name}@{context.version.npm}",
        "latest",
    ]
    command.extend(npm_extra_args())
    command.extend(otp_arg(otp))
    runner.run(command, cwd=context.root, capture_output=False)


def npm_extra_args() -> list[str]:
    return shlex.split(os.environ.get("NPM_PUBLISH_ARGS", ""))


def otp_arg(value: str) -> list[str]:
    return [f"--otp={value}"] if value else []


def create_and_push_tag(context: ReleaseContext, runner: CommandRunner) -> None:
    git = inspect_git_state(context, runner)
    if not git.tag_commit:
        runner.run(
            [
                "git",
                "tag",
                "-a",
                context.version.tag,
                "-m",
                f"Release {context.version.project}",
            ],
            cwd=context.root,
        )
    runner.run(["git", "push", "origin", context.version.tag], cwd=context.root)
