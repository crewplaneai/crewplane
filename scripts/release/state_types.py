from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

REQUEST_TIMEOUT_SECONDS = 15
COMMAND_TIMEOUT_SECONDS = 900
USER_AGENT = "crewplane-release/1"
MANIFEST_PATH = Path(".release/release-manifest.json")
COMMAND_FAILURE_OUTPUT_LIMIT = 12000


class ReleaseError(RuntimeError):
    """Raised when release state is unsafe or cannot be verified."""


class ReleaseStatus(StrEnum):
    READY = "ready"
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: int | None = COMMAND_TIMEOUT_SECONDS,
        capture_output: bool = True,
        check: bool = True,
    ) -> CommandResult:
        merged_env = os.environ.copy()
        if env is not None:
            merged_env.update(env)
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=merged_env,
            timeout=timeout,
            check=False,
            capture_output=capture_output,
            text=True,
        )
        result = CommandResult(
            args=tuple(command),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        if check and result.returncode != 0:
            rendered = " ".join(_redacted_args(command))
            output = _command_failure_output(result)
            suffix = f": {output}" if output else ""
            raise ReleaseError(
                f"command failed ({result.returncode}): {rendered}{suffix}"
            )
        return result


def _redacted_args(command: Sequence[str]) -> tuple[str, ...]:
    redacted: list[str] = []
    previous = ""
    for arg in command:
        if previous == "--otp":
            redacted.append("<redacted>")
            previous = ""
            continue
        if arg == "--otp":
            redacted.append(arg)
            previous = "--otp"
            continue
        if arg.startswith("--otp="):
            redacted.append("--otp=<redacted>")
            previous = ""
            continue
        previous = ""
        redacted.append(arg)
    if previous == "--otp":
        redacted.append("<missing-otp>")
    return tuple(redacted)


def _command_failure_output(result: CommandResult) -> str:
    sections: list[str] = []
    if result.stdout.strip():
        sections.append("stdout:\n" + result.stdout.strip())
    if result.stderr.strip():
        sections.append("stderr:\n" + result.stderr.strip())
    output = "\n\n".join(sections)
    if len(output) <= COMMAND_FAILURE_OUTPUT_LIMIT:
        return output
    return output[-COMMAND_FAILURE_OUTPUT_LIMIT:]


@dataclass(frozen=True)
class ReleaseVersion:
    project: str
    python: str
    npm: str
    tag: str

    @classmethod
    def from_project(cls, project_version: str) -> ReleaseVersion:
        try:
            parsed = Version(project_version)
        except InvalidVersion as error:
            raise ReleaseError(
                f"invalid pyproject.toml version: {project_version}"
            ) from error
        if parsed.local is not None:
            raise ReleaseError(
                "local version identifiers are not allowed for public releases"
            )
        npm_version = npm_semver_from_python_version(parsed)
        return cls(
            project=project_version,
            python=str(parsed),
            npm=npm_version,
            tag=f"v{project_version}",
        )


@dataclass(frozen=True)
class ReleaseContext:
    root: Path
    package_name: str
    console_script: str
    version: ReleaseVersion

    @property
    def sdist_filename(self) -> str:
        return f"{self.package_name}-{self.version.python}.tar.gz"

    @property
    def wheel_filename(self) -> str:
        return f"{self.package_name}-{self.version.python}-py3-none-any.whl"

    @property
    def npm_filename(self) -> str:
        return f"{self.package_name}-{self.version.npm}.tgz"

    @property
    def sdist_url(self) -> str:
        first = self.package_name[0]
        return (
            "https://files.pythonhosted.org/packages/source/"
            f"{first}/{self.package_name}/{self.sdist_filename}"
        )


@dataclass(frozen=True)
class ArtifactIdentity:
    key: str
    path: str
    filename: str
    size: int
    sha256: str
    sha1: str = ""
    integrity: str = ""


@dataclass(frozen=True)
class ReleaseManifest:
    package_name: str
    project_version: str
    python_version: str
    npm_version: str
    git_tag: str
    artifacts: dict[str, ArtifactIdentity]

    def artifact(self, key: str) -> ArtifactIdentity:
        try:
            return self.artifacts[key]
        except KeyError as error:
            raise ReleaseError(
                f"release manifest is missing artifact {key!r}"
            ) from error


@dataclass(frozen=True)
class PypiFile:
    filename: str
    size: int
    sha256: str


@dataclass(frozen=True)
class PypiRelease:
    exists: bool
    version_key: str
    files: dict[str, PypiFile]


@dataclass(frozen=True)
class NpmRelease:
    exists: bool
    version_key: str
    latest: str
    name: str
    version: str
    python_package: str
    python_package_version: str
    shasum: str
    integrity: str


@dataclass(frozen=True)
class FormulaState:
    path: Path
    url: str
    version: str
    sha256: str
    head_branch: str
    resources: frozenset[str]
    resource_specs: Mapping[str, tuple[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class GitState:
    branch: str
    default_branch: str
    head_commit: str
    upstream_ahead: int
    upstream_behind: int
    dirty: bool
    tag_commit: str
    remote_tag_commit: str

    def has_matching_tag(self) -> bool:
        return (
            self.tag_commit == self.head_commit
            and self.remote_tag_commit == self.head_commit
        )


@dataclass(frozen=True)
class DerivedReleaseState:
    status: ReleaseStatus
    reasons: tuple[str, ...]
    guidance: tuple[str, ...]

    def is_blocked(self) -> bool:
        return self.status == ReleaseStatus.BLOCKED


def npm_semver_from_python_version(version: Version) -> str:
    release_parts = [str(part) for part in version.release]
    while len(release_parts) < 3:
        release_parts.append("0")
    base = ".".join(release_parts[:3])

    prerelease_parts: list[str] = []
    if version.pre is not None:
        phase, number = version.pre
        prerelease_parts.extend([npm_prerelease_phase(phase), str(number)])
    if version.dev is not None:
        prerelease_parts.extend(["dev", str(version.dev)])
    if version.post is not None:
        prerelease_parts.extend(["post", str(version.post)])
    if prerelease_parts:
        return f"{base}-{'.'.join(prerelease_parts)}"
    return base


def npm_prerelease_phase(phase: str) -> str:
    phases = {"a": "alpha", "b": "beta", "rc": "rc"}
    try:
        return phases[phase]
    except KeyError as error:
        raise ReleaseError(f"unsupported prerelease phase: {phase}") from error


def read_release_context(root: Path) -> ReleaseContext:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    package_name = str(project["name"])
    scripts = project.get("scripts", {})
    if list(scripts) != [package_name]:
        raise ReleaseError(
            f"expected one [project.scripts] key matching {package_name!r}, "
            f"got {list(scripts)!r}"
        )
    return ReleaseContext(
        root=root,
        package_name=package_name,
        console_script=package_name,
        version=ReleaseVersion.from_project(str(project["version"])),
    )


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ReleaseError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(loaded, dict):
        raise ReleaseError(f"expected a JSON object in {path}")
    return loaded


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )


def read_manifest(root: Path) -> ReleaseManifest:
    path = root / MANIFEST_PATH
    if not path.exists():
        raise ReleaseError(f"release manifest is missing: {path}")
    payload = load_json(path)
    package = payload.get("package")
    artifacts = payload.get("artifacts")
    if not isinstance(package, dict) or not isinstance(artifacts, dict):
        raise ReleaseError(f"release manifest has an invalid shape: {path}")
    identities: dict[str, ArtifactIdentity] = {}
    for key, value in artifacts.items():
        if not isinstance(value, dict):
            raise ReleaseError(f"release manifest artifact {key!r} is malformed")
        identities[str(key)] = ArtifactIdentity(
            key=str(key),
            path=str(value["path"]),
            filename=str(value["filename"]),
            size=int(value["size"]),
            sha256=str(value["sha256"]),
            sha1=str(value.get("sha1", "")),
            integrity=str(value.get("integrity", "")),
        )
    return ReleaseManifest(
        package_name=str(package["name"]),
        project_version=str(package["project_version"]),
        python_version=str(package["python_version"]),
        npm_version=str(package["npm_version"]),
        git_tag=str(package["git_tag"]),
        artifacts=identities,
    )


def read_manifest_if_present(root: Path) -> ReleaseManifest | None:
    if not (root / MANIFEST_PATH).exists():
        return None
    return read_manifest(root)


def artifact_identity(path: Path, root: Path, key: str) -> ArtifactIdentity:
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    sha1 = hashlib.sha1(content).hexdigest()
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(content).digest()).decode(
        "ascii"
    )
    return ArtifactIdentity(
        key=key,
        path=str(path.relative_to(root)),
        filename=path.name,
        size=len(content),
        sha256=digest,
        sha1=sha1,
        integrity=integrity,
    )


def fetch_registry_json(url: str) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise ReleaseError(
            f"registry query failed for {url}: HTTP {error.code}"
        ) from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ReleaseError(f"registry query failed for {url}: {error}") from error
    if not isinstance(payload, dict):
        raise ReleaseError(f"registry response is not a JSON object: {url}")
    return payload


def query_pypi_release(context: ReleaseContext) -> PypiRelease:
    package_path = urllib.parse.quote(context.package_name, safe="")
    data = fetch_registry_json(f"https://pypi.org/pypi/{package_path}/json")
    if data is None:
        return PypiRelease(exists=False, version_key="", files={})
    releases = data.get("releases")
    if not isinstance(releases, dict):
        raise ReleaseError("PyPI response is missing releases")
    version_key = ""
    files_payload: list[Any] = []
    for candidate, files in releases.items():
        try:
            normalized = str(Version(str(candidate)))
        except InvalidVersion:
            continue
        if normalized == context.version.python:
            version_key = str(candidate)
            if not isinstance(files, list):
                raise ReleaseError(f"PyPI release {candidate!r} files are malformed")
            files_payload = files
            break
    if not version_key:
        return PypiRelease(exists=False, version_key="", files={})
    files_by_name: dict[str, PypiFile] = {}
    for file_payload in files_payload:
        if not isinstance(file_payload, dict):
            raise ReleaseError(
                f"PyPI release {version_key!r} includes a malformed file"
            )
        filename = str(file_payload.get("filename", ""))
        digests = file_payload.get("digests")
        if not filename or not isinstance(digests, dict) or "sha256" not in digests:
            raise ReleaseError(f"PyPI file metadata is incomplete for {version_key!r}")
        files_by_name[filename] = PypiFile(
            filename=filename,
            size=int(file_payload.get("size", 0)),
            sha256=str(digests["sha256"]),
        )
    return PypiRelease(exists=True, version_key=version_key, files=files_by_name)


def query_npm_release(context: ReleaseContext) -> NpmRelease:
    package_path = urllib.parse.quote(context.package_name, safe="@")
    data = fetch_registry_json(f"https://registry.npmjs.org/{package_path}")
    if data is None:
        return NpmRelease(False, "", "", "", "", "", "", "", "")
    versions = data.get("versions")
    tags = data.get("dist-tags")
    if not isinstance(versions, dict) or not isinstance(tags, dict):
        raise ReleaseError("npm response is missing versions or dist-tags")
    latest = str(tags.get("latest", ""))
    version_payload = versions.get(context.version.npm)
    if version_payload is None:
        return NpmRelease(False, "", latest, "", "", "", "", "", "")
    if not isinstance(version_payload, dict):
        raise ReleaseError(f"npm version {context.version.npm!r} is malformed")
    crewplane_payload = version_payload.get("crewplane", {})
    dist_payload = version_payload.get("dist", {})
    if not isinstance(crewplane_payload, dict) or not isinstance(dist_payload, dict):
        raise ReleaseError(
            f"npm version {context.version.npm!r} metadata is incomplete"
        )
    return NpmRelease(
        exists=True,
        version_key=context.version.npm,
        latest=latest,
        name=str(version_payload.get("name", "")),
        version=str(version_payload.get("version", "")),
        python_package=str(crewplane_payload.get("pythonPackage", "")),
        python_package_version=str(crewplane_payload.get("pythonPackageVersion", "")),
        shasum=str(dist_payload.get("shasum", "")),
        integrity=str(dist_payload.get("integrity", "")),
    )


def query_registry_state(context: ReleaseContext) -> tuple[PypiRelease, NpmRelease]:
    return query_pypi_release(context), query_npm_release(context)
