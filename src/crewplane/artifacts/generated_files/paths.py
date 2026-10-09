from __future__ import annotations

from pathlib import Path

from crewplane.architecture.safe_files import is_safe_relative_path
from crewplane.core.platform import is_native_windows

from ..naming import build_generated_file_result_dir_name

RESERVED_WORKSPACE_PATH_ROOTS = frozenset(
    {
        ".crewplane",
        ".git",
        ".hg",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        "__pycache__",
        "execution-results",
        "execution-stages",
        "node_modules",
    }
)
GENERATED_FILE_SOURCE_METADATA_NAME = ".crewplane-generated-file-source.json"
GENERATED_FILE_SNAPSHOT_METADATA_NAME = ".crewplane-generated-file-snapshot.json"


def is_reserved_workspace_path(relative_path: Path) -> bool:
    first = relative_path.parts[0] if relative_path.parts else ""
    if is_native_windows():
        first = first.casefold()
    if first in {
        GENERATED_FILE_SOURCE_METADATA_NAME,
        GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    }:
        return True
    return first in RESERVED_WORKSPACE_PATH_ROOTS


def generated_file_node_prefix(node_id: str) -> Path:
    return Path("generated-files") / build_generated_file_result_dir_name(node_id)


def generated_file_path_belongs_to_node(relative_path: str, node_id: str) -> bool:
    parts = relative_path.split("/")
    return (
        len(parts) >= 4
        and tuple(parts[:2]) == generated_file_node_prefix(node_id).parts
        and is_safe_relative_path(relative_path)
    )
