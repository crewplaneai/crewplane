#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from release import build, publish, smoke
from release.state import (
    CommandRunner,
    ReleaseError,
    ReleaseStatus,
    derive_release_state,
    exit_with_error,
    fail_if_generated_metadata_stale,
    inspect_git_state,
    is_tag_only_missing_error,
    print_state,
    query_registry_state,
    read_formula_state,
    read_manifest_if_present,
    read_release_context,
    verify_formula_state_for_release,
    verify_git_tag_state,
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crewplane local release tool.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "prepare",
        "check",
        "confirm",
        "package-build",
        "package-check",
        "package-wheelhouse",
        "install-smoke-pip",
        "install-smoke-uv",
        "install-smoke-pipx",
        "install-smoke",
        "install-script-smoke",
        "npm-pack",
        "npm-smoke",
        "brew-smoke",
        "install-check",
        "changelog-check",
    ):
        subparsers.add_parser(command)
    for command in ("publish-pypi", "publish-npm", "finalize"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    runner = CommandRunner()
    root = Path.cwd()
    try:
        return dispatch(args, root, runner)
    except (ReleaseError, subprocess.TimeoutExpired) as error:
        return exit_with_error(error)


def dispatch(args: argparse.Namespace, root: Path, runner: CommandRunner) -> int:
    command = args.command
    if command == "prepare":
        build.prepare_release(root, runner)
        return 0
    if command == "check":
        return release_check(root, runner)
    if command == "confirm":
        publish.confirm_release(root)
        return 0
    if command == "publish-pypi":
        return publish.publish_pypi(root, runner, bool(args.execute))
    if command == "publish-npm":
        return publish.publish_npm(root, runner, bool(args.execute))
    if command == "finalize":
        return publish.finalize_release(root, runner, bool(args.execute))
    if command == "package-build":
        build.package_build(root, runner)
        return 0
    if command == "package-check":
        build.package_check(root, runner)
        return 0
    if command == "package-wheelhouse":
        build.package_wheelhouse(root, runner)
        return 0
    if command == "install-smoke-pip":
        smoke.install_smoke_pip(root, runner)
        return 0
    if command == "install-smoke-uv":
        smoke.install_smoke_uv(root, runner)
        return 0
    if command == "install-smoke-pipx":
        smoke.install_smoke_pipx(root, runner)
        return 0
    if command == "install-smoke":
        smoke.install_smoke(root, runner)
        return 0
    if command == "install-script-smoke":
        smoke.install_script_smoke(root, runner)
        return 0
    if command == "npm-pack":
        build.npm_pack(root, runner)
        return 0
    if command == "npm-smoke":
        smoke.npm_smoke(root, runner)
        return 0
    if command == "brew-smoke":
        smoke.brew_smoke(root, runner)
        return 0
    if command == "install-check":
        smoke.install_check(root, runner)
        return 0
    if command == "changelog-check":
        changelog_check(root)
        return 0
    raise AssertionError(f"unhandled command: {command}")


def release_check(root: Path, runner: CommandRunner) -> int:
    context = read_release_context(root)
    manifest = read_manifest_if_present(root)
    pypi, npm = query_registry_state(context)
    formula = read_formula_state(context)
    git = inspect_git_state(context, runner)
    state = derive_release_state(context, pypi, npm, formula, git, manifest)
    print_state(state)
    if state.status == ReleaseStatus.COMPLETE:
        print(
            "Release already fully published and verified; no pre-publish checks needed."
        )
        return 0
    if state.status == ReleaseStatus.PARTIAL:
        formula_issues = verify_formula_state_for_release(context, formula, manifest)
        tag_issues = verify_git_tag_state(git)
        if (
            pypi.exists
            and npm.exists
            and npm.latest == context.version.npm
            and not formula_issues
            and is_tag_only_missing_error(tag_issues)
        ):
            print(
                "Release registries are published and Git tag is missing; pre-publish checks are not needed."
            )
            return 0
    if state.status in {ReleaseStatus.PARTIAL, ReleaseStatus.BLOCKED}:
        return 1
    fail_if_generated_metadata_stale(context, manifest)
    run_pre_publish_checks(root, runner)
    print(
        "Verify CHANGELOG.md describes the pyproject.toml version before running make release."
    )
    return 0


def run_pre_publish_checks(root: Path, runner: CommandRunner) -> None:
    runner.run(["make", "lint"], cwd=root)
    runner.run(["make", "format-check"], cwd=root)
    runner.run(["make", "test"], cwd=root)
    smoke.install_check(root, runner)


def changelog_check(root: Path) -> None:
    context = read_release_context(root)
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if (
        f"## [{context.version.project}]" not in text
        and f"## {context.version.project}" not in text
    ):
        raise ReleaseError(
            f"CHANGELOG.md does not contain a section for {context.version.project}. "
            "Changelog content is still reviewed manually."
        )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
