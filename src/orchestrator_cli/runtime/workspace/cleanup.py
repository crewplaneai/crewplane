from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .ref_cleanup import WorkspaceRunRefCleanup
from .worktree_cleanup import remove_unknown_workspace_path, worktree_disk_usage

WorkspaceStatus = str | None
WorkspaceStatusLookup = Callable[[str, str], WorkspaceStatus]


@dataclass(frozen=True)
class WorkspaceCleanupFilter:
    run_key_name: str | None = None
    repository_id: str | None = None
    expected_common_git_dir: Path | None = None
    older_than_seconds: int | None = None
    statuses: frozenset[str] = frozenset()
    orphans: bool = False


@dataclass(frozen=True)
class WorkspaceCleanupEntry:
    path: Path
    run_key_name: str
    size_bytes: int
    removed: bool
    status: WorkspaceStatus
    orphan: bool


@dataclass(frozen=True)
class WorkspaceCleanupResult:
    cache_root: Path
    entries: tuple[WorkspaceCleanupEntry, ...]
    removed_ref_count: int = 0

    @property
    def removed_count(self) -> int:
        return sum(1 for entry in self.entries if entry.removed)

    @property
    def total_size_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self.entries)


def cleanup_workspace_cache(
    cache_root: Path,
    cleanup_filter: WorkspaceCleanupFilter,
    dry_run: bool,
    status_lookup: WorkspaceStatusLookup | None = None,
    ref_cleanup: WorkspaceRunRefCleanup | None = None,
) -> WorkspaceCleanupResult:
    entries: list[WorkspaceCleanupEntry] = []
    selected_run_keys: set[str] = set()
    for run_key_name, workspace_path in cleanup_candidates_for_filter(
        cache_root,
        cleanup_filter,
    ):
        if (
            cleanup_filter.run_key_name is not None
            and run_key_name != cleanup_filter.run_key_name
        ):
            continue
        if not older_than_matches(workspace_path, cleanup_filter.older_than_seconds):
            continue
        status = workspace_status(run_key_name, workspace_path, status_lookup)
        if not status_matches(status, cleanup_filter):
            continue
        size_bytes = worktree_disk_usage(workspace_path)
        removed = False
        if not dry_run:
            remove_unknown_workspace_path(
                workspace_path,
                cleanup_filter.expected_common_git_dir,
            )
            removed = True
            selected_run_keys.add(run_key_name)
        entries.append(
            WorkspaceCleanupEntry(
                path=workspace_path,
                run_key_name=run_key_name,
                size_bytes=size_bytes,
                removed=removed,
                status=status,
                orphan=status is None,
            )
        )
    removed_ref_count = 0
    if not dry_run and ref_cleanup is not None:
        for run_key_name in sorted(selected_run_keys):
            removed_ref_count += ref_cleanup(run_key_name)
    return WorkspaceCleanupResult(
        cache_root=cache_root,
        entries=tuple(entries),
        removed_ref_count=removed_ref_count,
    )


def cleanup_candidates(cache_root: Path) -> tuple[tuple[str, Path], ...]:
    candidates: list[tuple[str, Path]] = []
    candidates.extend(flat_workspace_candidates(cache_root / "workspace-runs"))
    for family in ("workspaces", "snapshots"):
        candidates.extend(repository_workspace_candidates(cache_root / family))
    candidates.extend(review_workspace_candidates(cache_root / "review-workspaces"))
    return tuple(sorted(candidates, key=lambda item: item[1].as_posix()))


def cleanup_candidates_for_filter(
    cache_root: Path,
    cleanup_filter: WorkspaceCleanupFilter,
) -> tuple[tuple[str, Path], ...]:
    if cleanup_filter.repository_id is None:
        return cleanup_candidates(cache_root)
    return cleanup_candidates_for_repository(cache_root, cleanup_filter.repository_id)


def cleanup_candidates_for_repository(
    cache_root: Path,
    repository_id: str,
) -> tuple[tuple[str, Path], ...]:
    candidates: list[tuple[str, Path]] = []
    for family in ("workspaces", "snapshots"):
        candidates.extend(
            repository_workspace_candidates_for_repo(
                cache_root / family / repository_id
            )
        )
    candidates.extend(
        review_workspace_candidates_for_repo(
            cache_root / "review-workspaces" / repository_id
        )
    )
    return tuple(sorted(candidates, key=lambda item: item[1].as_posix()))


def repository_workspace_candidates_for_repo(
    repo_root: Path,
) -> tuple[tuple[str, Path], ...]:
    candidates: list[tuple[str, Path]] = []
    for run_dir in workspace_run_dirs(repo_root):
        candidates.extend((run_dir.name, path) for path in workspace_paths(run_dir))
    return tuple(candidates)


def review_workspace_candidates_for_repo(
    repo_root: Path,
) -> tuple[tuple[str, Path], ...]:
    candidates: list[tuple[str, Path]] = []
    for run_dir in workspace_run_dirs(repo_root):
        for node_dir in workspace_paths(run_dir):
            candidates.extend(
                (run_dir.name, path) for path in workspace_paths(node_dir)
            )
    return tuple(candidates)


def flat_workspace_candidates(root: Path) -> tuple[tuple[str, Path], ...]:
    candidates: list[tuple[str, Path]] = []
    for run_dir in workspace_run_dirs(root):
        candidates.extend((run_dir.name, path) for path in workspace_paths(run_dir))
    return tuple(candidates)


def repository_workspace_candidates(root: Path) -> tuple[tuple[str, Path], ...]:
    candidates: list[tuple[str, Path]] = []
    for repo_dir in workspace_run_dirs(root):
        for run_dir in workspace_run_dirs(repo_dir):
            candidates.extend((run_dir.name, path) for path in workspace_paths(run_dir))
    return tuple(candidates)


def review_workspace_candidates(root: Path) -> tuple[tuple[str, Path], ...]:
    candidates: list[tuple[str, Path]] = []
    for repo_dir in workspace_run_dirs(root):
        for run_dir in workspace_run_dirs(repo_dir):
            for node_dir in workspace_paths(run_dir):
                candidates.extend(
                    (run_dir.name, path) for path in workspace_paths(node_dir)
                )
    return tuple(candidates)


def workspace_run_dirs(root: Path) -> tuple[Path, ...]:
    if not safe_workspace_directory(root):
        return ()
    return tuple(
        sorted(path for path in root.iterdir() if safe_workspace_directory(path))
    )


def workspace_paths(run_dir: Path) -> tuple[Path, ...]:
    if not safe_workspace_directory(run_dir):
        return ()
    return tuple(
        sorted(path for path in run_dir.iterdir() if safe_workspace_directory(path))
    )


def safe_workspace_directory(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def older_than_matches(path: Path, older_than_seconds: int | None) -> bool:
    if older_than_seconds is None:
        return True
    try:
        age_seconds = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age_seconds >= older_than_seconds


def workspace_status(
    run_key_name: str,
    workspace_path: Path,
    status_lookup: WorkspaceStatusLookup | None,
) -> WorkspaceStatus:
    if status_lookup is None:
        return None
    return status_lookup(run_key_name, workspace_path.name)


def status_matches(
    status: WorkspaceStatus,
    cleanup_filter: WorkspaceCleanupFilter,
) -> bool:
    if not cleanup_filter.statuses and not cleanup_filter.orphans:
        return True
    if status is None:
        return cleanup_filter.orphans
    return status in cleanup_filter.statuses


def parse_duration_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    raw = value.strip().lower()
    if not raw:
        raise ValueError("Duration must not be empty.")
    suffix = raw[-1]
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(suffix)
    number = raw[:-1] if multiplier is not None else raw
    if not number.isdigit():
        raise ValueError(
            "Duration must be an integer optionally followed by s, m, h, or d."
        )
    seconds = int(number) * (multiplier or 1)
    if seconds < 0:
        raise ValueError("Duration must be non-negative.")
    return seconds
