from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

RESERVED_WORKSPACE_FILE_ROOTS = (
    ".orchestrator/execution-stages",
    ".orchestrator/execution-results",
    ".orchestrator/locks",
)


@dataclass(frozen=True)
class WorkspaceFilePathRecord:
    source_root: str
    source_root_relative_to_project: str
    git_top_relative_path: str
    workspace_relative_path: str


def lexical_absolute_path(path: Path) -> Path:
    return path.expanduser().absolute()


def source_root_relative_to_project(
    source_root_path: Path,
    project_root: Path,
) -> str | None:
    try:
        relative = source_root_path.relative_to(project_root)
    except ValueError:
        return None
    return relative.as_posix() or "."


def project_relative_workspace_path(
    source_root_relative: str,
    raw_path: str,
) -> str | None:
    base_parts = (
        ()
        if source_root_relative == "."
        else tuple(PurePosixPath(source_root_relative).parts)
    )
    return normalize_project_relative_parts(
        (*base_parts, *PurePosixPath(raw_path).parts)
    )


def normalize_project_relative_parts(parts: tuple[str, ...]) -> str | None:
    normalized: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not normalized:
                return None
            normalized.pop()
            continue
        normalized.append(part)
    if not normalized:
        return None
    return PurePosixPath(*normalized).as_posix()


def is_reserved_workspace_path(path: str) -> bool:
    return any(
        path == root or path.startswith(f"{root}/")
        for root in RESERVED_WORKSPACE_FILE_ROOTS
    )
