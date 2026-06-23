#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT = "crewplane-release-check"


@dataclass(frozen=True)
class LocalVersions:
    npm_version: str
    npm_python_package_version: str
    homebrew_version: str


@dataclass(frozen=True)
class RemoteRelease:
    label: str
    exists: bool


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_homebrew_version(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith('version "'):
            return stripped.split('"', 2)[1]
    raise ValueError(f"missing Homebrew formula version in {path}")


def read_local_versions(root: Path) -> LocalVersions:
    npm_package = read_json(root / "packaging" / "npm" / "package.json")
    return LocalVersions(
        npm_version=str(npm_package["version"]),
        npm_python_package_version=str(
            npm_package["crewplane"]["pythonPackageVersion"]
        ),
        homebrew_version=read_homebrew_version(
            root / "packaging" / "homebrew" / "Formula" / "crewplane.rb"
        ),
    )


def local_mismatches(root: Path, expected_version: str) -> list[tuple[str, str]]:
    versions = read_local_versions(root)
    candidates = (
        ("packaging/npm/package.json version", versions.npm_version),
        (
            "packaging/npm/package.json crewplane.pythonPackageVersion",
            versions.npm_python_package_version,
        ),
        ("packaging/homebrew/Formula/crewplane.rb version", versions.homebrew_version),
    )
    return [
        (label, actual_version)
        for label, actual_version in candidates
        if actual_version != expected_version
    ]


def fetch_registry_json(url: str) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def pypi_release(package_name: str, version: str) -> RemoteRelease:
    package_path = urllib.parse.quote(package_name, safe="")
    data = fetch_registry_json(f"https://pypi.org/pypi/{package_path}/json")
    return RemoteRelease(
        label="PyPI",
        exists=data is not None and version in data.get("releases", {}),
    )


def npm_release(package_name: str, version: str) -> RemoteRelease:
    package_path = urllib.parse.quote(package_name, safe="@")
    data = fetch_registry_json(f"https://registry.npmjs.org/{package_path}")
    return RemoteRelease(
        label="npm",
        exists=data is not None and version in data.get("versions", {}),
    )


def check_local(root: Path, expected_version: str) -> int:
    mismatches = local_mismatches(root, expected_version)
    if not mismatches:
        print(f"Packaging versions match pyproject.toml version {expected_version}.")
        return 0

    print(f"Packaging versions differ from pyproject.toml version {expected_version}:")
    for label, actual_version in mismatches:
        print(f"  {label}: {actual_version}")
    return 1


def check_remote(package_name: str, version: str) -> int:
    try:
        releases = (
            pypi_release(package_name, version),
            npm_release(package_name, version),
        )
    except urllib.error.URLError as error:
        print(f"Could not query package registries: {error}", file=sys.stderr)
        return 1

    existing_releases = [release.label for release in releases if release.exists]
    if existing_releases:
        registries = ", ".join(existing_releases)
        print(
            f"{package_name} {version} already exists on {registries}; "
            "bump pyproject.toml before releasing."
        )
        return 1

    print(f"{package_name} {version} is not present on PyPI or npm.")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crewplane release preflight checks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    local_parser = subparsers.add_parser("local")
    local_parser.add_argument("--root", type=Path, default=Path.cwd())
    local_parser.add_argument("--version", required=True)

    remote_parser = subparsers.add_parser("remote")
    remote_parser.add_argument("--package-name", required=True)
    remote_parser.add_argument("--version", required=True)

    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.command == "local":
        return check_local(args.root, args.version)
    if args.command == "remote":
        return check_remote(args.package_name, args.version)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
