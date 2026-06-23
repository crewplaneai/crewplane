from __future__ import annotations

import stat
from pathlib import Path


def contained_regular_file(root: Path, relative_path: str) -> Path | None:
    raw_parts = relative_path.split("/")
    if (
        not relative_path
        or any(part in {"", ".", ".."} for part in raw_parts)
        or Path(relative_path).is_absolute()
    ):
        return None
    if _has_symlink_component(root):
        return None
    path = Path(*raw_parts)
    candidate = root
    for part in path.parts:
        candidate = candidate / part
        if _path_is_symlink(candidate):
            return None
    try:
        resolved = candidate.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except PermissionError:
        raise
    except OSError:
        return None
    if not resolved.is_relative_to(root_resolved):
        return None
    try:
        file_stat = resolved.stat()
    except PermissionError:
        raise
    except OSError:
        return None
    if not resolved.is_file() or file_stat.st_nlink != 1:
        return None
    return resolved


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        if _path_is_symlink(current):
            return True
    return False


def _path_is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except PermissionError:
        raise
    except OSError:
        return False
