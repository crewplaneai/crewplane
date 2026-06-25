from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from packaging.markers import InvalidMarker, Marker

from .state_types import (
    ArtifactIdentity,
    CommandRunner,
    FormulaState,
    GitState,
    NpmRelease,
    PypiRelease,
    ReleaseContext,
    ReleaseError,
    ReleaseManifest,
    artifact_identity,
    load_json,
)

HOMEBREW_MARKER_ENVIRONMENTS = (
    {
        "implementation_name": "cpython",
        "os_name": "posix",
        "platform_python_implementation": "CPython",
        "platform_system": "Darwin",
        "python_full_version": "3.13.0",
        "python_version": "3.13",
        "sys_platform": "darwin",
        "extra": "",
    },
    {
        "implementation_name": "cpython",
        "os_name": "posix",
        "platform_python_implementation": "CPython",
        "platform_system": "Linux",
        "python_full_version": "3.13.0",
        "python_version": "3.13",
        "sys_platform": "linux",
        "extra": "",
    },
)


def read_formula_state(context: ReleaseContext) -> FormulaState:
    path = (
        context.root
        / "packaging"
        / "homebrew"
        / "Formula"
        / f"{context.package_name}.rb"
    )
    text = path.read_text(encoding="utf-8")
    url = required_regex(text, r'^\s*url "([^"]+)"', path, "Homebrew url")
    version = required_regex(text, r'^\s*version "([^"]+)"', path, "Homebrew version")
    sha256 = required_regex(
        text, r'^\s*sha256 "([a-f0-9]{64})"', path, "Homebrew sha256"
    )
    branch = required_regex(
        text,
        r'^\s*head "https://github\.com/crewplaneai/crewplane\.git", branch: "([^"]+)"',
        path,
        "Homebrew head branch",
    )
    resources = frozenset(re.findall(r'^\s*resource "([^\"]+)" do', text, re.MULTILINE))
    resource_specs = parse_formula_resource_specs(text)
    return FormulaState(
        path=path,
        url=url,
        version=version,
        sha256=sha256,
        head_branch=branch,
        resources=resources,
        resource_specs=resource_specs,
    )


def required_regex(text: str, pattern: str, path: Path, label: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        raise ReleaseError(f"missing {label} in {path}")
    return match.group(1)


def parse_formula_resource_specs(text: str) -> dict[str, tuple[str, str]]:
    pattern = re.compile(
        r'^[ \t]*resource "([^\"]+)" do[ \t]*(?:\n[ \t]*)+'
        r'url "([^"]+)"[ \t]*(?:\n[ \t]*)+'
        r'sha256 "([a-f0-9]{64})"[ \t]*(?:\n[ \t]*)+end',
        re.MULTILINE,
    )
    specs: dict[str, tuple[str, str]] = {}
    for match in pattern.finditer(text):
        specs[match.group(1)] = (match.group(2), match.group(3))
    return specs


def inspect_git_state(context: ReleaseContext, runner: CommandRunner) -> GitState:
    root = context.root
    branch = git_output(runner, root, ["git", "branch", "--show-current"])
    default_branch = remote_default_branch(runner, root)
    head_commit = git_output(runner, root, ["git", "rev-parse", "HEAD"])
    dirty = bool(git_output(runner, root, ["git", "status", "--porcelain=v1"]))
    ahead, behind = upstream_counts(runner, root)
    tag_commit = git_tag_commit(runner, root, context.version.tag)
    remote_tag_commit = remote_git_tag_commit(runner, root, context.version.tag)
    return GitState(
        branch=branch,
        default_branch=default_branch,
        head_commit=head_commit,
        upstream_ahead=ahead,
        upstream_behind=behind,
        dirty=dirty,
        tag_commit=tag_commit,
        remote_tag_commit=remote_tag_commit,
    )


def git_output(runner: CommandRunner, root: Path, command: Sequence[str]) -> str:
    result = runner.run(command, cwd=root, timeout=60, capture_output=True, check=True)
    return result.stdout.strip()


def remote_default_branch(runner: CommandRunner, root: Path) -> str:
    result = runner.run(
        ["git", "ls-remote", "--symref", "origin", "HEAD"],
        cwd=root,
        timeout=60,
        capture_output=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("ref: "):
            ref = line.split()[1]
            return ref.rsplit("/", 1)[-1]
    raise ReleaseError("could not determine origin default branch")


def upstream_counts(runner: CommandRunner, root: Path) -> tuple[int, int]:
    result = runner.run(
        ["git", "rev-list", "--left-right", "--count", "@{u}...HEAD"],
        cwd=root,
        timeout=60,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseError("current branch has no usable upstream tracking branch")
    parts = result.stdout.split()
    if len(parts) != 2:
        raise ReleaseError("could not parse upstream divergence")
    behind = int(parts[0])
    ahead = int(parts[1])
    return ahead, behind


def git_tag_commit(runner: CommandRunner, root: Path, tag: str) -> str:
    result = runner.run(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{}}"],
        cwd=root,
        timeout=60,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def remote_git_tag_commit(runner: CommandRunner, root: Path, tag: str) -> str:
    result = runner.run(
        ["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}*"],
        cwd=root,
        timeout=60,
        capture_output=True,
        check=True,
    )
    direct = ""
    peeled_ref = f"refs/tags/{tag}^{{}}"
    direct_ref = f"refs/tags/{tag}"
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        if parts[1] == peeled_ref:
            return parts[0]
        if parts[1] == direct_ref:
            direct = parts[0]
    return direct


def verify_generated_metadata(
    context: ReleaseContext, manifest: ReleaseManifest | None
) -> list[str]:
    issues: list[str] = []
    issues.extend(verify_install_script(context))
    issues.extend(verify_npm_package_json(context))
    issues.extend(verify_uv_lock(context))
    issues.extend(verify_docs_snippets(context))
    issues.extend(verify_homebrew_formula(context, manifest))
    return issues


def verify_install_script(context: ReleaseContext) -> list[str]:
    path = context.root / "install.sh"
    text = path.read_text(encoding="utf-8")
    expected = f'CREWPLANE_VERSION="${{CREWPLANE_VERSION:-{context.version.project}}}"'
    return (
        []
        if expected in text
        else [f"{path.relative_to(context.root)} has a stale default version"]
    )


def verify_npm_package_json(context: ReleaseContext) -> list[str]:
    path = context.root / "packaging" / "npm" / "package.json"
    package = load_json(path)
    issues: list[str] = []
    if package.get("version") != context.version.npm:
        issues.append("packaging/npm/package.json has a stale npm package version")
    crewplane = package.get("crewplane")
    if not isinstance(crewplane, dict):
        issues.append("packaging/npm/package.json is missing crewplane metadata")
        return issues
    if crewplane.get("pythonPackageVersion") != context.version.project:
        issues.append("packaging/npm/package.json has a stale Python package version")
    return issues


def verify_uv_lock(context: ReleaseContext) -> list[str]:
    path = context.root / "uv.lock"
    lock = tomllib.loads(path.read_text(encoding="utf-8"))
    packages = lock.get("package")
    if not isinstance(packages, list):
        return ["uv.lock is missing package entries"]
    editable = [
        package
        for package in packages
        if isinstance(package, dict) and package.get("source") == {"editable": "."}
    ]
    if len(editable) != 1:
        return ["uv.lock must contain one editable crewplane package"]
    package = editable[0]
    if package.get("name") != context.package_name:
        return ["uv.lock editable package has the wrong name"]
    if package.get("version") != context.version.python:
        return ["uv.lock editable package version is stale"]
    return []


def verify_docs_snippets(context: ReleaseContext) -> list[str]:
    issues: list[str] = []
    public_docs = [
        context.root / "README.md",
        context.root / "docs" / "getting-started" / "installation.md",
        context.root / "packaging" / "npm" / "README.md",
    ]
    for path in public_docs:
        text = path.read_text(encoding="utf-8")
        if "crewplane@alpha" in text:
            issues.append(
                f"{path.relative_to(context.root)} still documents npm @alpha"
            )
        if "raw.githubusercontent.com/crewplaneai/crewplane/main/" in text:
            issues.append(
                f"{path.relative_to(context.root)} still references main install URL"
            )
    npm_readme = public_docs[-1].read_text(encoding="utf-8")
    expected_tarball = f"./{context.npm_filename}"
    if expected_tarball not in npm_readme:
        issues.append("packaging/npm/README.md has a stale local tarball example")
    return issues


def verify_homebrew_formula(
    context: ReleaseContext, manifest: ReleaseManifest | None
) -> list[str]:
    formula = read_formula_state(context)
    issues: list[str] = []
    if formula.version != context.version.project:
        issues.append("Homebrew formula has a stale version")
    if formula.url != context.sdist_url:
        issues.append("Homebrew formula has a stale sdist URL")
    if formula.head_branch != "master":
        issues.append("Homebrew formula head branch must be master")
    if manifest is not None:
        expected_sha = manifest.artifact("pypi_sdist").sha256
        if formula.sha256 != expected_sha:
            issues.append(
                "Homebrew formula sdist SHA does not match the release manifest"
            )
    issues.extend(_verify_required_homebrew_resource_specs(context, formula))
    return issues


def resource_specs_from_lock(
    context: ReleaseContext,
    required_resources: set[str] | None = None,
    allow_missing: bool = False,
) -> dict[str, tuple[str, str]]:
    required = (
        required_resources
        if required_resources is not None
        else required_homebrew_resources(context)
    )
    specs = _resource_specs_from_lock(
        context,
        required,
        prefer_wheel_resources=homebrew_build_resource_names(context),
    )
    if allow_missing:
        return specs
    missing = required - specs.keys()
    if missing:
        missing_names = ", ".join(sorted(missing))
        raise ReleaseError(
            f"uv.lock is missing pin metadata for required Homebrew resources: {missing_names}"
        )
    return specs


def _resource_specs_from_lock(
    context: ReleaseContext,
    required_resources: set[str],
    prefer_wheel_resources: set[str],
) -> dict[str, tuple[str, str]]:
    package_entries = lock_package_entries(context)
    specs: dict[str, tuple[str, str]] = {}
    for package in package_entries:
        if not isinstance(package, dict):
            continue
        package_name = str(package.get("name", ""))
        normal_name = resource_name_from_requirement(package_name)
        if normal_name not in required_resources:
            continue
        package_url, package_sha = _lock_package_resource_spec(
            package,
            prefer_wheel=normal_name in prefer_wheel_resources,
        )
        package_sha = _normalize_hash_value(package_sha)
        if package_name and package_url and package_sha:
            specs[normal_name] = (package_url, package_sha)
    return specs


def _lock_package_resource_spec(
    package: dict[str, Any],
    prefer_wheel: bool,
) -> tuple[str, str]:
    wheel_spec = _first_wheel_spec(package)
    sdist_spec = _sdist_spec(package)
    if prefer_wheel:
        return wheel_spec or sdist_spec or ("", "")
    return sdist_spec or wheel_spec or ("", "")


def _sdist_spec(package: dict[str, Any]) -> tuple[str, str] | None:
    sdist = package.get("sdist")
    if not isinstance(sdist, dict):
        return None
    package_url = str(sdist.get("url", ""))
    package_sha = str(sdist.get("hash", ""))
    if not package_url or not package_sha:
        return None
    return package_url, package_sha


def _first_wheel_spec(package: dict[str, Any]) -> tuple[str, str] | None:
    wheels = package.get("wheels")
    if not isinstance(wheels, list) or not wheels:
        return None
    wheel = wheels[0]
    if not isinstance(wheel, dict):
        return None
    package_url = str(wheel.get("url", ""))
    package_sha = str(wheel.get("hash", ""))
    if not package_url or not package_sha:
        return None
    return package_url, package_sha


def _normalize_hash_value(raw: str) -> str:
    if raw.startswith("sha256:"):
        return raw.removeprefix("sha256:")
    return raw


def required_homebrew_resources(context: ReleaseContext) -> set[str]:
    packages = lock_packages_by_name(context)
    project_package = editable_lock_package(context, packages)
    runtime_roots = dependency_names(project_package)
    build_roots = lock_backed_build_roots(context, packages)
    names = collect_transitive_lock_dependencies(packages, runtime_roots | build_roots)
    names.discard(context.package_name)
    return names


def build_homebrew_resources(context: ReleaseContext) -> set[str]:
    packages = lock_packages_by_name(context)
    build_roots = lock_backed_build_roots(context, packages)
    names = collect_transitive_lock_dependencies(packages, build_roots)
    names.discard(context.package_name)
    return names


def homebrew_build_resource_names(context: ReleaseContext) -> set[str]:
    return build_homebrew_resources(context) | declared_homebrew_build_resources(
        context
    )


def declared_homebrew_build_resources(context: ReleaseContext) -> set[str]:
    formula = (
        context.root
        / "packaging"
        / "homebrew"
        / "Formula"
        / f"{context.package_name}.rb"
    )
    text = formula.read_text(encoding="utf-8")
    match = re.search(r"build_resources\s*=\s*%w\[(.*?)\]", text, re.DOTALL)
    if match is None:
        return set()
    return {
        resource_name_from_requirement(resource)
        for resource in match.group(1).split()
        if resource.strip()
    }


def lock_package_entries(context: ReleaseContext) -> list[Any]:
    lock = tomllib.loads((context.root / "uv.lock").read_text(encoding="utf-8"))
    package_entries = lock.get("package")
    if not isinstance(package_entries, list):
        raise ReleaseError("uv.lock is missing package entries")
    return package_entries


def lock_packages_by_name(context: ReleaseContext) -> dict[str, dict[str, Any]]:
    packages: dict[str, dict[str, Any]] = {}
    for package in lock_package_entries(context):
        if not isinstance(package, dict):
            continue
        name = resource_name_from_requirement(str(package.get("name", "")))
        if name:
            packages[name] = package
    return packages


def editable_lock_package(
    context: ReleaseContext, packages: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    package = packages.get(context.package_name)
    if package is None or package.get("source") != {"editable": "."}:
        raise ReleaseError("uv.lock is missing the editable crewplane package")
    return package


def lock_backed_build_roots(
    context: ReleaseContext, packages: dict[str, dict[str, Any]]
) -> set[str]:
    pyproject = tomllib.loads(
        (context.root / "pyproject.toml").read_text(encoding="utf-8")
    )
    build_system = pyproject.get("build-system", {})
    if not isinstance(build_system, dict):
        return set()
    requirements = build_system.get("requires", [])
    if not isinstance(requirements, list):
        raise ReleaseError("pyproject.toml build-system.requires must be a list")
    names = {
        resource_name_from_requirement(str(requirement))
        for requirement in requirements
        if marker_applies_to_homebrew(str(requirement))
    }
    return {name for name in names if name in packages}


def collect_transitive_lock_dependencies(
    packages: dict[str, dict[str, Any]], root_names: set[str]
) -> set[str]:
    required: set[str] = set()
    pending = list(sorted(root_names))
    while pending:
        name = pending.pop()
        if name in required:
            continue
        package = packages.get(name)
        if package is None:
            required.add(name)
            continue
        required.add(name)
        for dependency in sorted(dependency_names(package) - required):
            pending.append(dependency)
    return required


def dependency_names(package: dict[str, Any]) -> set[str]:
    dependencies = package.get("dependencies", [])
    if not isinstance(dependencies, list):
        return set()
    names: set[str] = set()
    for dependency in dependencies:
        name = lock_dependency_name(dependency)
        if name:
            names.add(name)
    return names


def lock_dependency_name(dependency: object) -> str:
    if isinstance(dependency, str):
        if marker_applies_to_homebrew(dependency):
            return resource_name_from_requirement(dependency)
        return ""
    if not isinstance(dependency, dict):
        return ""
    marker = dependency.get("marker")
    if marker is not None and not marker_applies_to_homebrew(str(marker)):
        return ""
    return resource_name_from_requirement(str(dependency.get("name", "")))


def marker_applies_to_homebrew(requirement_or_marker: str) -> bool:
    marker_text = marker_text_from_requirement(requirement_or_marker)
    if not marker_text:
        return True
    try:
        marker = Marker(marker_text)
    except InvalidMarker as error:
        raise ReleaseError(f"invalid environment marker: {marker_text}") from error
    return any(
        marker.evaluate(environment) for environment in HOMEBREW_MARKER_ENVIRONMENTS
    )


def marker_text_from_requirement(requirement_or_marker: str) -> str:
    if ";" in requirement_or_marker:
        return requirement_or_marker.split(";", 1)[1].strip()
    return (
        requirement_or_marker
        if any(
            token in requirement_or_marker
            for token in (
                "sys_platform",
                "platform_system",
                "os_name",
                "python_version",
                "extra",
            )
        )
        else ""
    )


def resource_name_from_requirement(requirement: str) -> str:
    name = re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip()
    return name.lower().replace("_", "-")


def verify_pypi_artifacts(
    context: ReleaseContext, release: PypiRelease, manifest: ReleaseManifest
) -> list[str]:
    if not release.exists:
        return ["PyPI version is missing"]
    issues: list[str] = []
    for key in ("pypi_sdist", "pypi_wheel"):
        artifact = manifest.artifact(key)
        remote = release.files.get(artifact.filename)
        if remote is None:
            issues.append(f"PyPI is missing {artifact.filename}")
            continue
        if remote.sha256 != artifact.sha256:
            issues.append(f"PyPI hash mismatch for {artifact.filename}")
        if remote.size and remote.size != artifact.size:
            issues.append(f"PyPI size mismatch for {artifact.filename}")
    expected_names = {context.sdist_filename, context.wheel_filename}
    for filename in expected_names:
        if filename not in release.files:
            issues.append(f"PyPI is missing expected file {filename}")
    return issues


def verify_npm_artifact(
    context: ReleaseContext, release: NpmRelease, manifest: ReleaseManifest
) -> list[str]:
    if not release.exists:
        return ["npm version is missing"]
    artifact = manifest.artifact("npm_tarball")
    issues: list[str] = []
    expected_identity = {
        "name": context.package_name,
        "version": context.version.npm,
        "python_package": context.package_name,
        "python_package_version": context.version.project,
    }
    actual_identity = {
        "name": release.name,
        "version": release.version,
        "python_package": release.python_package,
        "python_package_version": release.python_package_version,
    }
    if actual_identity != expected_identity:
        issues.append("npm package identity does not match pyproject.toml metadata")
    if release.shasum != artifact.sha1:
        issues.append("npm shasum does not match the release manifest")
    if release.integrity != artifact.integrity:
        issues.append("npm integrity does not match the release manifest")
    return issues


def verify_local_manifest_artifacts(
    context: ReleaseContext, manifest: ReleaseManifest, keys: tuple[str, ...]
) -> list[str]:
    issues: list[str] = []
    for key in keys:
        expected = manifest.artifact(key)
        artifact_path = Path(expected.path)
        if artifact_path.is_absolute():
            issues.append(f"release manifest artifact {key} uses an absolute path")
            continue
        root = context.root.resolve(strict=False)
        local_path = (context.root / artifact_path).resolve(strict=False)
        if not local_path.is_relative_to(root):
            issues.append(
                f"release manifest artifact {key} escapes the repository root"
            )
            continue
        if not local_path.is_file():
            issues.append(f"release artifact is missing: {expected.path}")
            continue
        actual = artifact_identity(local_path, context.root, key)
        issues.extend(local_artifact_identity_issues(key, expected, actual))
    return issues


def local_artifact_identity_issues(
    key: str, expected: ArtifactIdentity, actual: ArtifactIdentity
) -> list[str]:
    issues: list[str] = []
    if actual.filename != expected.filename:
        issues.append(f"{key} filename does not match the release manifest")
    if actual.size != expected.size:
        issues.append(f"{key} size does not match the release manifest")
    if actual.sha256 != expected.sha256:
        issues.append(f"{key} sha256 does not match the release manifest")
    if expected.sha1 and actual.sha1 != expected.sha1:
        issues.append(f"{key} sha1 does not match the release manifest")
    if expected.integrity and actual.integrity != expected.integrity:
        issues.append(f"{key} integrity does not match the release manifest")
    return issues


def _verify_required_homebrew_resource_specs(
    context: ReleaseContext, formula: FormulaState
) -> list[str]:
    issues: list[str] = []
    required_resources = required_homebrew_resources(context)
    expected_resource_specs = resource_specs_from_lock(
        context, required_resources, allow_missing=True
    )
    missing_lock_specs = required_resources - expected_resource_specs.keys()
    if missing_lock_specs:
        missing = ", ".join(sorted(missing_lock_specs))
        issues.append(
            f"uv.lock is missing pin metadata for required Homebrew resources: {missing}"
        )
    for resource_name, expected in expected_resource_specs.items():
        actual = formula.resource_specs.get(resource_name)
        if actual is None:
            issues.append(
                f"Homebrew formula resource {resource_name} is missing expected pin metadata"
            )
            continue
        if actual != expected:
            issues.append(
                f"Homebrew formula resource {resource_name} does not match uv.lock pins"
            )
    return issues
