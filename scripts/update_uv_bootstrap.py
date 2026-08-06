#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(
    os.environ.get(
        "CREWPLANE_UV_BOOTSTRAP_ROOT",
        Path(__file__).resolve().parents[1],
    )
).resolve()
LATEST_RELEASE_URL = "https://api.github.com/repos/astral-sh/uv/releases/latest"
RELEASE_BASE_URL = "https://github.com/astral-sh/uv/releases/download"
MANIFEST_PATH = Path("packaging/uv-bootstrap.json")
INSTALLER_PATH = Path("install.sh")
POSTINSTALL_PATH = Path("packaging/npm/scripts/postinstall.js")
SHELL_BEGIN = "# BEGIN GENERATED UV BOOTSTRAP METADATA"
SHELL_END = "# END GENERATED UV BOOTSTRAP METADATA"
JAVASCRIPT_BEGIN = "// BEGIN GENERATED UV BOOTSTRAP METADATA"
JAVASCRIPT_END = "// END GENERATED UV BOOTSTRAP METADATA"
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
CHECKSUM_PATTERN = re.compile(r"[0-9a-f]{64}")
TextFetcher = Callable[[str], str]


class UvBootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class UvRelease:
    version: str
    checksums: dict[str, str]


@dataclass(frozen=True)
class UvTarget:
    archive_target: str
    shell_platforms: tuple[str, ...]
    node_platforms: tuple[str, ...]


UV_TARGETS: tuple[UvTarget, ...] = (
    UvTarget(
        "aarch64-apple-darwin",
        ("Darwin:arm64",),
        ("darwin:arm64",),
    ),
    UvTarget(
        "x86_64-apple-darwin",
        ("Darwin:x86_64",),
        ("darwin:x64",),
    ),
    UvTarget(
        "aarch64-unknown-linux-gnu",
        ("Linux:aarch64:gnu", "Linux:arm64:gnu"),
        ("linux:arm64:gnu",),
    ),
    UvTarget(
        "aarch64-unknown-linux-musl",
        ("Linux:aarch64:musl", "Linux:arm64:musl"),
        ("linux:arm64:musl",),
    ),
    UvTarget(
        "x86_64-unknown-linux-gnu",
        ("Linux:x86_64:gnu", "Linux:amd64:gnu"),
        ("linux:x64:gnu",),
    ),
    UvTarget(
        "x86_64-unknown-linux-musl",
        ("Linux:x86_64:musl", "Linux:amd64:musl"),
        ("linux:x64:musl",),
    ),
)


def validate_uv_targets() -> None:
    archive_targets = tuple(target.archive_target for target in UV_TARGETS)
    shell_platforms = tuple(
        platform for target in UV_TARGETS for platform in target.shell_platforms
    )
    node_platforms = tuple(
        platform for target in UV_TARGETS for platform in target.node_platforms
    )
    if any(
        not target.archive_target
        or not target.shell_platforms
        or not target.node_platforms
        for target in UV_TARGETS
    ):
        raise UvBootstrapError(
            "uv targets require an archive target and shell and Node platform selectors"
        )
    for label, values in (
        ("archive target", archive_targets),
        ("shell platform", shell_platforms),
        ("Node platform", node_platforms),
    ):
        if len(values) != len(set(values)):
            raise UvBootstrapError(f"duplicate {label} in uv target matrix")


def validate_release(release: UvRelease) -> None:
    validate_uv_targets()
    if VERSION_PATTERN.fullmatch(release.version) is None:
        raise UvBootstrapError(f"invalid uv version: {release.version!r}")
    expected_targets = {target.archive_target for target in UV_TARGETS}
    actual_targets = set(release.checksums)
    if actual_targets != expected_targets:
        missing = sorted(expected_targets - actual_targets)
        unexpected = sorted(actual_targets - expected_targets)
        raise UvBootstrapError(
            f"invalid uv checksum target set; missing={missing}, unexpected={unexpected}"
        )
    for target, checksum in release.checksums.items():
        if CHECKSUM_PATTERN.fullmatch(checksum) is None:
            raise UvBootstrapError(f"invalid uv checksum for {target}: {checksum!r}")


def load_manifest(repository: Path) -> UvRelease:
    manifest_path = repository / MANIFEST_PATH
    try:
        manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(
            manifest.get("checksums"), dict
        ):
            raise TypeError("expected an object with a checksums object")
        release = UvRelease(
            version=str(manifest["version"]),
            checksums={
                str(target): str(checksum)
                for target, checksum in manifest["checksums"].items()
            },
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise UvBootstrapError(f"invalid uv bootstrap manifest: {error}") from error
    validate_release(release)
    return release


def render_manifest(release: UvRelease) -> str:
    manifest = {
        "version": release.version,
        "checksums": {
            target.archive_target: release.checksums[target.archive_target]
            for target in UV_TARGETS
        },
    }
    return f"{json.dumps(manifest, indent=2)}\n"


def render_shell_target(target: UvTarget, checksum: str) -> str:
    platforms = "|".join(target.shell_platforms)
    return f'''        {platforms})
            printf '%s|%s\\n' \\
                "{target.archive_target}" \\
                "{checksum}"
            ;;'''


def render_shell_metadata(release: UvRelease) -> str:
    target_cases = "\n".join(
        render_shell_target(target, release.checksums[target.archive_target])
        for target in UV_TARGETS
    )
    return f'''UV_VERSION="{release.version}"
UV_RELEASE_BASE_URL="https://github.com/astral-sh/uv/releases/download/${{UV_VERSION}}"

uv_archive_details() {{
    uv_platform="$(uname -s 2>/dev/null || printf unknown):$(uname -m 2>/dev/null || printf unknown)"
    case "$uv_platform" in
        Linux:*)
            uv_libc="gnu"
            if command -v ldd >/dev/null 2>&1 && ldd --version 2>&1 | grep -qi musl; then
                uv_libc="musl"
            fi
            uv_platform="${{uv_platform}}:${{uv_libc}}"
            ;;
    esac
    case "$uv_platform" in
{target_cases}
        *)
            fail "unsupported platform for automatic uv installation: $uv_platform"
            ;;
    esac
}}'''


def render_javascript_target(target: UvTarget, checksum: str) -> str:
    return "\n".join(
        f'''  "{platform}": {{
    target: "{target.archive_target}",
    sha256: "{checksum}",
  }},'''
        for platform in target.node_platforms
    )


def render_javascript_metadata(release: UvRelease) -> str:
    archive_entries = "\n".join(
        render_javascript_target(target, release.checksums[target.archive_target])
        for target in UV_TARGETS
    )
    return f'''const UV_VERSION = "{release.version}";
const UV_RELEASE_BASE_URL = `https://github.com/astral-sh/uv/releases/download/${{UV_VERSION}}`;
const UV_ARCHIVES = {{
{archive_entries}
}};'''


def replace_generated_region(
    content: str,
    begin_marker: str,
    end_marker: str,
    generated: str,
) -> str:
    pattern = re.compile(
        rf"{re.escape(begin_marker)}\n.*?\n{re.escape(end_marker)}",
        re.DOTALL,
    )
    replacement = f"{begin_marker}\n{generated.rstrip()}\n{end_marker}"

    def replace_region(match: re.Match[str]) -> str:
        del match
        return replacement

    updated, count = pattern.subn(replace_region, content)
    if count != 1:
        raise UvBootstrapError(
            f"expected one generated region between {begin_marker!r} and {end_marker!r}"
        )
    return updated


def update_workflow_versions(content: str, version: str, path: Path) -> str:
    lines = content.splitlines(keepends=True)
    setup_indices = [
        index for index, line in enumerate(lines) if "astral-sh/setup-uv@" in line
    ]
    for setup_index in setup_indices:
        version_index = find_setup_uv_version_line(lines, setup_index, path)
        indentation_length = indentation_width(lines[version_index])
        indentation = lines[version_index][:indentation_length]
        newline = "\n" if lines[version_index].endswith("\n") else ""
        lines[version_index] = f'{indentation}version: "{version}"{newline}'
    return "".join(lines)


def find_setup_uv_version_line(
    lines: list[str],
    setup_index: int,
    path: Path,
) -> int:
    setup_line = lines[setup_index]
    property_indentation = indentation_width(setup_line)
    if setup_line.lstrip().startswith("- uses:"):
        property_indentation += 2
    with_indentation: int | None = None
    input_indentation: int | None = None
    for index in range(setup_index + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indentation = indentation_width(line)
        if indentation < property_indentation:
            break
        if indentation == property_indentation and stripped == "with:":
            with_indentation = indentation
            continue
        if with_indentation is None:
            continue
        if indentation <= with_indentation:
            break
        if input_indentation is None:
            input_indentation = indentation
        if indentation == input_indentation and stripped.startswith("version:"):
            return index
    line_number = setup_index + 1
    raise UvBootstrapError(
        f"{path}:{line_number} setup-uv step has no explicit version input"
    )


def indentation_width(line: str) -> int:
    return len(line) - len(line.lstrip())


def rendered_repository_files(
    repository: Path,
    release: UvRelease,
) -> dict[Path, str]:
    validate_release(release)
    rendered = {MANIFEST_PATH: render_manifest(release)}
    installer = (repository / INSTALLER_PATH).read_text(encoding="utf-8")
    rendered[INSTALLER_PATH] = replace_generated_region(
        installer,
        SHELL_BEGIN,
        SHELL_END,
        render_shell_metadata(release),
    )
    postinstall = (repository / POSTINSTALL_PATH).read_text(encoding="utf-8")
    rendered[POSTINSTALL_PATH] = replace_generated_region(
        postinstall,
        JAVASCRIPT_BEGIN,
        JAVASCRIPT_END,
        render_javascript_metadata(release),
    )
    workflows = sorted((repository / ".github/workflows").glob("*.y*ml"))
    setup_uv_workflows = 0
    for workflow_path in workflows:
        relative_path = workflow_path.relative_to(repository)
        content = workflow_path.read_text(encoding="utf-8")
        if "astral-sh/setup-uv@" not in content:
            continue
        setup_uv_workflows += 1
        rendered[relative_path] = update_workflow_versions(
            content,
            release.version,
            relative_path,
        )
    if setup_uv_workflows == 0:
        raise UvBootstrapError("no setup-uv workflow steps were found")
    return rendered


def validate_repository(repository: Path) -> UvRelease:
    release = load_manifest(repository)
    expected_files = rendered_repository_files(repository, release)
    mismatches = [
        str(relative_path)
        for relative_path, expected in expected_files.items()
        if (repository / relative_path).read_text(encoding="utf-8") != expected
    ]
    if mismatches:
        raise UvBootstrapError(
            "uv bootstrap metadata is out of sync: " + ", ".join(mismatches)
        )
    return release


def synchronize_repository(
    repository: Path,
    release: UvRelease,
) -> tuple[Path, ...]:
    rendered = rendered_repository_files(repository, release)
    changed_paths = tuple(
        relative_path
        for relative_path, content in rendered.items()
        if (repository / relative_path).read_text(encoding="utf-8") != content
    )
    for relative_path in changed_paths:
        write_text_atomic(repository / relative_path, rendered[relative_path])
    validate_repository(repository)
    return changed_paths


def write_text_atomic(path: Path, content: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_path = Path(temporary_file.name)
        temporary_path.chmod(mode)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def fetch_release(version: str, fetch_text: TextFetcher) -> UvRelease:
    if VERSION_PATTERN.fullmatch(version) is None:
        raise UvBootstrapError(f"invalid uv version: {version!r}")
    checksums: dict[str, str] = {}
    for target in UV_TARGETS:
        filename = f"uv-{target.archive_target}.tar.gz"
        checksum_url = f"{RELEASE_BASE_URL}/{version}/{filename}.sha256"
        checksum_text = fetch_text(checksum_url).strip()
        match = re.fullmatch(
            rf"([0-9a-f]{{64}})  {re.escape(filename)}",
            checksum_text,
        )
        if match is None:
            raise UvBootstrapError(
                "invalid checksum response for uv "
                f"{version} target {target.archive_target}"
            )
        checksums[target.archive_target] = match.group(1)
    release = UvRelease(version, checksums)
    validate_release(release)
    return release


def fetch_latest_release(fetch_text: TextFetcher) -> UvRelease:
    try:
        metadata: Any = json.loads(fetch_text(LATEST_RELEASE_URL))
        tag_name = metadata["tag_name"]
        draft = metadata["draft"]
        prerelease = metadata["prerelease"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise UvBootstrapError(
            f"invalid latest uv release metadata: {error}"
        ) from error
    if not isinstance(tag_name, str) or draft is not False or prerelease is not False:
        raise UvBootstrapError(
            "latest uv release metadata did not describe a stable release"
        )
    return fetch_release(tag_name.removeprefix("v"), fetch_text)


def fetch_text(url: str) -> str:
    headers = {
        "User-Agent": "crewplane-uv-bootstrap-updater",
    }
    if url.startswith("https://api.github.com/"):
        headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(1_048_577)
    except (OSError, urllib.error.URLError) as error:
        raise UvBootstrapError(f"failed to fetch {url}: {error}") from error
    if len(body) > 1_048_576:
        raise UvBootstrapError(f"response exceeded 1 MiB: {url}")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UvBootstrapError(f"response was not UTF-8: {url}") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synchronize Crewplane's pinned uv bootstrap metadata."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="verify all committed uv pins and checksums")
    update_parser = subparsers.add_parser(
        "update",
        help="update all uv pins and checksums",
    )
    update_parser.add_argument(
        "version",
        help="exact uv version or 'latest'",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "check":
            release = validate_repository(ROOT)
        elif arguments.version == "latest":
            release = fetch_latest_release(fetch_text)
            synchronize_repository(ROOT, release)
        else:
            release = fetch_release(arguments.version, fetch_text)
            synchronize_repository(ROOT, release)
    except UvBootstrapError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(release.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
