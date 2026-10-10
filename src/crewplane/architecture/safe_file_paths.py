from __future__ import annotations

import os
import re
import stat
from pathlib import Path

_WINDOWS_DEVICE = re.compile(
    r"^(?:con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³]) *(?:\.|$)",
    re.IGNORECASE,
)


def is_windows_device_name(name: str) -> bool:
    return _WINDOWS_DEVICE.match(name) is not None


def is_safe_path_component(component: str) -> bool:
    return (
        bool(component)
        and component not in {".", ".."}
        and not component.endswith((".", " "))
        and not is_windows_device_name(component)
        and not any(ord(char) < 32 or char in '<>:"/\\|?*' for char in component)
    )


def path_is_absent(path: Path) -> bool:
    """Return true only for a missing entry; dangling links remain present."""

    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def is_safe_relative_path(relative_path: str) -> bool:
    """Check raw relative POSIX syntax without normalizing or inspecting the path."""
    return all(is_safe_path_component(part) for part in relative_path.split("/"))


def relative_path_parts(relative_path: str) -> tuple[str, ...]:
    if relative_path in {"", "."}:
        return ()
    if not is_safe_relative_path(relative_path):
        raise ValueError("Contained paths must be safe relative POSIX paths.")
    return tuple(relative_path.split("/"))


def relative_path_parts_optional(relative_path: str) -> tuple[str, ...] | None:
    try:
        return relative_path_parts(relative_path)
    except ValueError:
        return None


def is_single_link_regular_file(file_stat: os.stat_result) -> bool:
    """Classify supplied metadata without following paths or acquiring resources."""

    return stat.S_ISREG(file_stat.st_mode) and file_stat.st_nlink == 1


def resolved_path_is_contained(root: Path, candidate: Path) -> bool:
    """Check containment, resolving the root first and propagating failures."""

    root_resolved = root.resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    return candidate_resolved.is_relative_to(root_resolved)


def relative_path_has_symlink(root: Path, relative: Path) -> bool:
    """Inspect relative components below a root already checked by the caller."""

    current = root
    for part in relative.parts:
        current = current / part
        if path_is_symlink(current):
            return True
    return False


def path_has_symlink_component(path: Path) -> bool:
    """Return whether any existing component in ``path`` is a symlink."""

    current = Path(path.anchor) if path.is_absolute() else Path()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        if path_is_symlink(current):
            return True
    return False


def path_is_symlink(path: Path) -> bool:
    """Return whether ``path`` itself is a symlink without following it."""

    try:
        metadata = path.lstat()
        return stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & 0x400
        )
    except PermissionError:
        raise
    except OSError:
        return False
