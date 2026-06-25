from __future__ import annotations

import re
import sys
from pathlib import Path

from .state_checks import (
    inspect_git_state,
    required_homebrew_resources,
    resource_specs_from_lock,
    verify_generated_metadata,
    verify_npm_package_json,
    verify_uv_lock,
)
from .state_state import manifest_context_issues, publishing_git_issues
from .state_types import (
    COMMAND_TIMEOUT_SECONDS,
    CommandRunner,
    DerivedReleaseState,
    GitState,
    ReleaseContext,
    ReleaseError,
    ReleaseManifest,
    command_exists,
    load_json,
    write_json,
)


def replace_one_regex(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ReleaseError(f"expected exactly one replacement in {path}: {pattern}")
    path.write_text(updated, encoding="utf-8")


def replace_optional_regex(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated = re.sub(pattern, replacement, text, count=1, flags=re.MULTILINE)
    path.write_text(updated, encoding="utf-8")


def sync_generated_metadata(context: ReleaseContext, runner: CommandRunner) -> None:
    sync_install_script(context)
    sync_npm_package_json(context)
    sync_public_docs(context)
    refresh_uv_lock(context, runner)
    sync_homebrew_formula_metadata(context, "")
    issues = verify_generated_metadata(context, None)
    if issues:
        rendered = "\n  ".join(issues)
        raise ReleaseError(
            f"generated metadata sync failed read-back validation:\n  {rendered}"
        )


def sync_install_script(context: ReleaseContext) -> None:
    replace_one_regex(
        context.root / "install.sh",
        r'^CREWPLANE_VERSION="\$\{CREWPLANE_VERSION:-[^}]+\}"$',
        f'CREWPLANE_VERSION="${{CREWPLANE_VERSION:-{context.version.project}}}"',
    )


def sync_npm_package_json(context: ReleaseContext) -> None:
    path = context.root / "packaging" / "npm" / "package.json"
    package = load_json(path)
    crewplane = package.get("crewplane")
    if not isinstance(crewplane, dict):
        raise ReleaseError("packaging/npm/package.json is missing crewplane metadata")
    package["version"] = context.version.npm
    crewplane["pythonPackageVersion"] = context.version.project
    write_json(path, package)
    issues = verify_npm_package_json(context)
    if issues:
        raise ReleaseError("; ".join(issues))


def sync_public_docs(context: ReleaseContext) -> None:
    replacements = {
        context.root / "README.md": [
            (r"npm install -g crewplane@alpha", "npm install -g crewplane", False),
        ],
        context.root / "docs" / "getting-started" / "installation.md": [
            (
                r"https://raw\.githubusercontent\.com/crewplaneai/crewplane/main/install\.sh",
                "https://raw.githubusercontent.com/crewplaneai/crewplane/master/install.sh",
                False,
            ),
            (r"npm install -g crewplane@alpha", "npm install -g crewplane", False),
        ],
        context.root / "packaging" / "npm" / "README.md": [
            (r"This alpha npm package exposes", "This npm package exposes", False),
            (r"npm install -g crewplane@alpha", "npm install -g crewplane", False),
            (r"npx crewplane@alpha --help", "npx crewplane --help", False),
            (r"\./crewplane-[^/\s]+\.tgz", f"./{context.npm_filename}", True),
        ],
    }
    for path, path_replacements in replacements.items():
        for pattern, replacement, required in path_replacements:
            if required:
                replace_one_regex(path, pattern, replacement)
            else:
                replace_optional_regex(path, pattern, replacement)


def sync_homebrew_formula_metadata(context: ReleaseContext, sdist_sha256: str) -> None:
    formula = (
        context.root
        / "packaging"
        / "homebrew"
        / "Formula"
        / f"{context.package_name}.rb"
    )
    replace_one_regex(formula, r'^\s*url "[^"]+"$', f'  url "{context.sdist_url}"')
    replace_one_regex(
        formula,
        r'^\s*version "[^"]+"$',
        f'  version "{context.version.project}"',
    )
    if sdist_sha256:
        replace_one_regex(
            formula,
            r'^\s*sha256 "[a-f0-9]{64}"$',
            f'  sha256 "{sdist_sha256}"',
        )
    sync_formula_resource_specs(formula, context)
    replace_one_regex(
        formula,
        r'^\s*head "https://github\.com/crewplaneai/crewplane\.git", branch: "[^"]+"$',
        '  head "https://github.com/crewplaneai/crewplane.git", branch: "master"',
    )


def sync_formula_resource_specs(formula: Path, context: ReleaseContext) -> None:
    text = formula.read_text(encoding="utf-8")
    required_resources = required_homebrew_resources(context)
    resource_names = set(
        re.findall(r"^\s*resource \"([^\"]+)\" do", text, re.MULTILINE)
    )
    resources_to_sync = resource_names | required_resources
    expected = resource_specs_from_lock(context, resources_to_sync, allow_missing=True)
    missing_specs = required_resources - expected.keys()
    if missing_specs:
        missing = ", ".join(sorted(missing_specs))
        raise ReleaseError(
            f"uv.lock is missing pin metadata for required Homebrew resources: {missing}"
        )
    for name, (expected_url, expected_sha) in expected.items():
        pattern = re.compile(
            r'^([ \t]*)resource "'
            + re.escape(name)
            + r'" do[ \t]*(?:\n[ \t]*)+'
            + r'url "[^"]+"[ \t]*(?:\n[ \t]*)+'
            + r'sha256 "[a-f0-9]{64}"[ \t]*(?:\n[ \t]*)+([ \t]*)end',
            re.MULTILINE,
        )
        match = pattern.search(text)
        if not match:
            raise ReleaseError(f"Homebrew formula is missing resource block for {name}")
        indent = match.group(1)
        replacement = (
            f'{indent}resource "{name}" do\n'
            f'{indent}  url "{expected_url}"\n'
            f'{indent}  sha256 "{expected_sha}"\n'
            f"{indent}end"
        )
        text, _ = pattern.subn(replacement, text, count=1)
    formula.write_text(text, encoding="utf-8")


def refresh_uv_lock(context: ReleaseContext, runner: CommandRunner) -> None:
    if not command_exists("uv"):
        raise ReleaseError(
            "uv is required to refresh uv.lock during release preparation"
        )
    runner.run(["uv", "lock"], cwd=context.root, timeout=COMMAND_TIMEOUT_SECONDS)
    issues = verify_uv_lock(context)
    if issues:
        rendered = "; ".join(issues)
        raise ReleaseError(
            f"uv.lock refresh did not produce expected metadata: {rendered}"
        )


def fail_if_generated_metadata_stale(
    context: ReleaseContext, manifest: ReleaseManifest | None
) -> None:
    if manifest is not None:
        manifest_issues = manifest_context_issues(context, manifest)
        if manifest_issues:
            rendered = "; ".join(manifest_issues)
            raise ReleaseError(
                "release manifest metadata is stale; run make release-prepare first: "
                f"{rendered}"
            )
    issues = verify_generated_metadata(context, manifest)
    if issues:
        rendered = "\n  ".join(issues)
        raise ReleaseError(
            "generated release metadata is stale; run make release-prepare first:\n"
            f"  {rendered}"
        )


def require_publish_git_state(
    context: ReleaseContext,
    runner: CommandRunner,
    allow_existing_tag: bool,
    allow_local_changes: bool = False,
) -> GitState:
    runner.run(
        ["git", "fetch", "--quiet", "origin", "--tags"], cwd=context.root, timeout=120
    )
    git = inspect_git_state(context, runner)
    issues = publishing_git_issues(git, allow_existing_tag, allow_local_changes)
    if issues:
        rendered = "\n  ".join(issues)
        raise ReleaseError(f"publishing is blocked by Git state:\n  {rendered}")
    return git


def print_state(state: DerivedReleaseState) -> None:
    print(f"Release state: {state.status}")
    for reason in state.reasons:
        print(f"  - {reason}")
    for item in state.guidance:
        print(f"  {item}")


def exit_with_error(error: Exception) -> int:
    print(f"error: {error}", file=sys.stderr)
    return 1
