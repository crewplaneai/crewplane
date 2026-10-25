from __future__ import annotations

from pathlib import Path

from .plugins import release_plugins
from .state_types import ReleaseContext


def release_artifact_layout(context: ReleaseContext) -> dict[str, Path]:
    """Return canonical release paths in artifact identity collection order."""
    paths = {
        "pypi_sdist": Path("dist") / context.sdist_filename,
        "pypi_wheel": Path("dist") / context.wheel_filename,
        "npm_tarball": Path(".release") / "npm" / context.npm_filename,
    }
    for plugin in release_plugins(context):
        paths[plugin.artifact_key("sdist")] = (
            Path("dist") / plugin.context.sdist_filename
        )
        paths[plugin.artifact_key("wheel")] = (
            Path("dist") / plugin.context.wheel_filename
        )
    return paths
