"""Standard-library execution policy shared by test and installed-package launchers."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable, Mapping
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


def remove_execution_directory(root: Path) -> None:
    """Remove owned artifacts, including paths beyond Windows' legacy limit."""
    if os.name == "nt":
        absolute = str(root.resolve())
        if not absolute.startswith("\\\\?\\"):
            if absolute.startswith("\\\\"):
                absolute = "UNC\\" + absolute[2:]
            root = Path("\\\\?\\" + absolute)
    shutil.rmtree(root, onexc=_remove_readonly_file)


def _remove_readonly_file(
    function: Callable[[str], object], path: str, error: BaseException
) -> None:
    if (
        os.name != "nt"
        or function is not os.unlink
        or not isinstance(error, PermissionError)
    ):
        raise error
    file = Path(path)
    mode = file.lstat().st_mode
    if not stat.S_ISREG(mode) or mode & stat.S_IWRITE:
        raise error
    # Git objects are read-only on Windows; deletion requires clearing that bit.
    file.chmod(mode | stat.S_IWRITE, follow_symlinks=False)
    function(path)


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
