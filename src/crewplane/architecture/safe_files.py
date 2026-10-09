from __future__ import annotations

from pathlib import Path

from . import safe_file_operations
from .safe_file_paths import (
    is_safe_path_component,
    is_safe_relative_path,
    is_single_link_regular_file,
    is_windows_device_name,
    path_has_symlink_component,
    path_is_absent,
    path_is_symlink,
    relative_path_has_symlink,
    relative_path_parts,
    relative_path_parts_optional,
    resolved_path_is_contained,
)

__all__ = [
    "is_windows_device_name",
    "is_safe_path_component",
    "path_is_absent",
    "is_safe_relative_path",
    "is_single_link_regular_file",
    "resolved_path_is_contained",
    "relative_path_has_symlink",
    "path_has_symlink_component",
    "path_is_symlink",
    "ensure_contained_directory",
    "contained_directory",
    "contained_regular_file",
    "ensure_single_link_regular_file",
    "replace_contained_file",
]


def ensure_contained_directory(root: Path, relative_path: str) -> Path:
    """Create and return a non-symlink directory below ``root``."""

    return safe_file_operations.safe_file_operations().ensure_contained_directory(
        root, relative_path_parts(relative_path)
    )


def contained_directory(root: Path, relative_path: str) -> Path | None:
    """Resolve a non-symlink directory below ``root`` when it exists."""

    return safe_file_operations.safe_file_operations().contained_directory(
        root, relative_path_parts(relative_path)
    )


def contained_regular_file(root: Path, relative_path: str) -> Path | None:
    """Resolve a single-link regular file contained below ``root``."""

    parts = relative_path_parts_optional(relative_path)
    if not parts:
        return None
    return safe_file_operations.safe_file_operations().contained_regular_file(
        root, parts
    )


def ensure_single_link_regular_file(path: Path) -> Path:
    """Create or return a single-link regular file without following links."""

    return safe_file_operations.safe_file_operations().ensure_regular_file(path)


def replace_contained_file(root: Path, relative_path: str, source: Path) -> Path:
    """Publish a same-filesystem file below a stable directory without clobbering."""

    parts = relative_path_parts(relative_path)
    if not parts:
        raise ValueError("Contained replacement requires a file path.")
    return safe_file_operations.safe_file_operations().replace_contained_file(
        root, parts, source
    )
