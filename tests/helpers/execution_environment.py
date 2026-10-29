"""Standard-library execution policy shared by test and installed-package launchers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

# Keep only interpreter/platform launch requirements; scenario values are explicit.
LAUNCH_KEYS = (
    "SYSTEMROOT",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "SYSTEMDRIVE",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
)
VALIDATION_KEYS = ("CREWPLANE_REQUIRED_GIT", "GIT_FLOOR_BIN")
DIRECTORY_NAMES = {
    "HOME": "home",
    "USERPROFILE": "home",
    "XDG_CONFIG_HOME": "xdg-config",
    "XDG_CACHE_HOME": "xdg-cache",
    "XDG_DATA_HOME": "xdg-data",
    "XDG_STATE_HOME": "xdg-state",
    "APPDATA": "appdata",
    "LOCALAPPDATA": "local-appdata",
    "TMPDIR": "tmp",
    "TMP": "tmp",
    "TEMP": "tmp",
}


def directory_environment(root: Path) -> dict[str, str]:
    root = root.resolve()
    values = {}
    for key, name in DIRECTORY_NAMES.items():
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        values[key] = directory.as_posix().removeprefix("//?/")
    if os.name == "nt":
        home = Path(values["HOME"])
        values.update(HOMEDRIVE=home.drive, HOMEPATH=str(home)[len(home.drive) :])
    return values


def build_execution_environment(
    root: Path,
    tool_path: str,
    scenario: Mapping[str, str] | None = None,
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if parent is None else parent
    environment = {key: source[key] for key in LAUNCH_KEYS if key in source}
    environment.update(directory_environment(root))
    environment.update(
        PATH=tool_path,
        TERM="dumb",
        COLUMNS="80",
        LINES="24",
        LC_ALL="C",
        LANG="C",
        TZ="UTC",
        PYTHONUTF8="1",
        PYTHONUNBUFFERED="1",
        PYTHONNOUSERSITE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    environment.update(scenario or {})
    return environment


def validation_controls(parent: Mapping[str, str]) -> dict[str, str]:
    return {key: parent[key] for key in VALIDATION_KEYS if key in parent}


def validation_tool_path(parent: Mapping[str, str]) -> str:
    path = parent.get("PATH", os.defpath)
    floor = parent.get("GIT_FLOOR_BIN")
    return os.pathsep.join((floor, path)) if floor else path
