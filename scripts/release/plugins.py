from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from .state_types import ReleaseContext, ReleaseError, ReleaseManifest, ReleaseVersion


@dataclass(frozen=True)
class PluginRelease:
    source: Path
    context: ReleaseContext

    def artifact_key(self, kind: str) -> str:
        return f"plugin:{self.context.package_name}:{kind}"

    def manifest(self, release: ReleaseManifest) -> ReleaseManifest:
        artifacts = {}
        for kind, filename in (
            ("sdist", self.context.sdist_filename),
            ("wheel", self.context.wheel_filename),
        ):
            artifact = release.artifact(self.artifact_key(kind))
            if (
                artifact.filename != filename
                or Path(artifact.path) != Path("dist") / filename
            ):
                raise ReleaseError(
                    f"Release plugin artifact has an unexpected identity: {artifact.key}"
                )
            artifacts[f"pypi_{kind}"] = artifact
        version = self.context.version
        return replace(
            release,
            package_name=self.context.package_name,
            project_version=version.project,
            python_version=version.python,
            npm_version=version.npm,
            git_tag=version.tag,
            artifacts=artifacts,
        )


def release_plugins(context: ReleaseContext) -> tuple[PluginRelease, ...]:
    with (context.root / "pyproject.toml").open("rb") as stream:
        metadata = tomllib.load(stream)
    paths = (
        metadata.get("tool", {})
        .get("crewplane", {})
        .get("release", {})
        .get("plugins", [])
    )
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ReleaseError("tool.crewplane.release.plugins must be a list of paths")
    dependencies = {
        canonicalize_name(requirement.name): requirement
        for requirement in map(Requirement, metadata["project"].get("dependencies", []))
    }
    plugins = []
    names: set[str] = set()
    for path in paths:
        source = (context.root / path).resolve()
        if Path(path).is_absolute() or not source.is_relative_to(
            context.root.resolve()
        ):
            raise ReleaseError(
                f"Release plugin must stay inside the repository: {path}"
            )
        with (source / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)["project"]
        name = canonicalize_name(project["name"])
        version = ReleaseVersion.from_project(project["version"])
        requirement = dependencies.get(name)
        if (
            requirement is None
            or requirement.marker is not None
            or version.python not in requirement.specifier
        ):
            raise ReleaseError(
                f"Release plugin {name} {version.python} must satisfy a required project dependency"
            )
        if name in names or name == canonicalize_name(context.package_name):
            raise ReleaseError(f"Duplicate release package: {name}")
        names.add(name)
        plugins.append(
            PluginRelease(source, ReleaseContext(context.root, name, "", version))
        )
    return tuple(plugins)
