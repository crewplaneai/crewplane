from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .state import WorkspaceStateRetention, update_workspace_retention
from .worktree.cleanup import remove_unknown_workspace_path, worktree_disk_usage
from .worktree.ref_cleanup import WorkspaceRunRefCleanup

WorkspaceStatus = str | None
WorkspaceStatusLookup = Callable[[str, str], WorkspaceStatus]


@dataclass(frozen=True)
class WorkspaceCleanupEligibility:
    deletable: bool
    reason: str | None = None
    state_paths: tuple[Path, ...] = ()
    expected_worktree_git_dir: Path | None = None


WorkspaceCleanupEligibilityLookup = Callable[
    [str, Path, WorkspaceStatus], WorkspaceCleanupEligibility
]


class AbsentWorkspaceStateProjection(NamedTuple):
    run_key_name: str
    workspace_path: Path
    status: WorkspaceStatus
    state_paths: tuple[Path, ...]


type _AbsentWorkspaceStateProjectionValues = tuple[
    str,
    Path,
    WorkspaceStatus,
    tuple[Path, ...],
]


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
    retained_reason: str | None = None


@dataclass(frozen=True)
class WorkspaceCleanupResult:
    cache_root: Path
    entries: tuple[WorkspaceCleanupEntry, ...]
    removed_ref_count: int = 0

    @property
    def removed_count(self) -> int:
        return sum(1 for entry in self.entries if entry.removed)

    @property
    def selected_count(self) -> int:
        return sum(1 for entry in self.entries if entry.retained_reason is None)

    @property
    def total_size_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self.entries)


@dataclass(frozen=True)
class _WorkspaceCleanupRequest:
    cache_root: Path
    cleanup_filter: WorkspaceCleanupFilter
    dry_run: bool
    status_lookup: WorkspaceStatusLookup | None
    ref_cleanup: WorkspaceRunRefCleanup | None
    eligibility_lookup: WorkspaceCleanupEligibilityLookup | None
    ref_cleanup_run_keys: Iterable[str]
    absent_state_projections: Iterable[_AbsentWorkspaceStateProjectionValues]


@dataclass(frozen=True)
class _WorkspaceCleanupCandidate:
    run_key_name: str
    workspace_path: Path
    status: WorkspaceStatus
    eligibility: WorkspaceCleanupEligibility


@dataclass(frozen=True)
class _IgnoredCleanupCandidate:
    pass


@dataclass(frozen=True)
class _RetainedCleanupCandidate:
    run_key_name: str
    reported_candidate: _WorkspaceCleanupCandidate | None = None


@dataclass(frozen=True)
class _SelectedCleanupCandidate:
    candidate: _WorkspaceCleanupCandidate


type _WorkspaceCleanupDecision = (
    _IgnoredCleanupCandidate | _RetainedCleanupCandidate | _SelectedCleanupCandidate
)

_IGNORED_CLEANUP_CANDIDATE = _IgnoredCleanupCandidate()


@dataclass
class _WorkspaceCleanupSelection:
    entries: list[WorkspaceCleanupEntry] = field(default_factory=list)
    selected_run_keys: set[str] = field(default_factory=set)
    retained_run_keys: set[str] = field(default_factory=set)

    def record_retained_run(
        self,
        run_key_name: str,
        entry: WorkspaceCleanupEntry | None = None,
    ) -> None:
        self.retained_run_keys.add(run_key_name)
        if entry is not None:
            self.entries.append(entry)

    def record_candidate_entry(self, entry: WorkspaceCleanupEntry) -> None:
        self.entries.append(entry)
        if entry.removed:
            self.selected_run_keys.add(entry.run_key_name)

    def record_selected_run(self, run_key_name: str) -> None:
        self.selected_run_keys.add(run_key_name)


def cleanup_workspace_cache(
    cache_root: Path,
    cleanup_filter: WorkspaceCleanupFilter,
    dry_run: bool,
    status_lookup: WorkspaceStatusLookup | None = None,
    ref_cleanup: WorkspaceRunRefCleanup | None = None,
    eligibility_lookup: WorkspaceCleanupEligibilityLookup | None = None,
    ref_cleanup_run_keys: Iterable[str] = (),
    absent_state_projections: Iterable[
        tuple[str, Path, WorkspaceStatus, tuple[Path, ...]]
    ] = (),
) -> WorkspaceCleanupResult:
    request = _WorkspaceCleanupRequest(
        cache_root=cache_root,
        cleanup_filter=cleanup_filter,
        dry_run=dry_run,
        status_lookup=status_lookup,
        ref_cleanup=ref_cleanup,
        eligibility_lookup=eligibility_lookup,
        ref_cleanup_run_keys=ref_cleanup_run_keys,
        absent_state_projections=absent_state_projections,
    )
    return _run_workspace_cleanup(request)


def _run_workspace_cleanup(
    request: _WorkspaceCleanupRequest,
) -> WorkspaceCleanupResult:
    selection = _cleanup_workspace_candidates(request)
    if not request.dry_run:
        _reconcile_absent_state_projections(request, selection)
    removed_ref_count = _cleanup_selected_run_refs(request, selection)
    return WorkspaceCleanupResult(
        cache_root=request.cache_root,
        entries=tuple(selection.entries),
        removed_ref_count=removed_ref_count,
    )


def _cleanup_workspace_candidates(
    request: _WorkspaceCleanupRequest,
) -> _WorkspaceCleanupSelection:
    selection = _WorkspaceCleanupSelection()
    for run_key_name, workspace_path in cleanup_candidates_for_filter(
        request.cache_root,
        request.cleanup_filter,
    ):
        decision = _assess_workspace_cleanup_candidate(
            run_key_name,
            workspace_path,
            request,
        )
        _apply_workspace_cleanup_decision(decision, request, selection)
    return selection


def _assess_workspace_cleanup_candidate(
    run_key_name: str,
    workspace_path: Path,
    request: _WorkspaceCleanupRequest,
) -> _WorkspaceCleanupDecision:
    cleanup_filter = request.cleanup_filter
    if cleanup_filter.run_key_name not in (None, run_key_name):
        return _IGNORED_CLEANUP_CANDIDATE
    if not older_than_matches(workspace_path, cleanup_filter.older_than_seconds):
        return _RetainedCleanupCandidate(run_key_name)
    candidate = _load_workspace_cleanup_candidate(
        run_key_name,
        workspace_path,
        request,
    )
    if not candidate.eligibility.deletable:
        return _RetainedCleanupCandidate(run_key_name, candidate)
    if not status_matches(candidate.status, cleanup_filter):
        return _RetainedCleanupCandidate(run_key_name)
    return _SelectedCleanupCandidate(candidate)


def _load_workspace_cleanup_candidate(
    run_key_name: str,
    workspace_path: Path,
    request: _WorkspaceCleanupRequest,
) -> _WorkspaceCleanupCandidate:
    status = workspace_status(run_key_name, workspace_path, request.status_lookup)
    eligibility_lookup = request.eligibility_lookup
    eligibility = (
        WorkspaceCleanupEligibility(deletable=True)
        if eligibility_lookup is None
        else eligibility_lookup(run_key_name, workspace_path, status)
    )
    return _WorkspaceCleanupCandidate(
        run_key_name,
        workspace_path,
        status,
        eligibility,
    )


def _apply_workspace_cleanup_decision(
    decision: _WorkspaceCleanupDecision,
    request: _WorkspaceCleanupRequest,
    selection: _WorkspaceCleanupSelection,
) -> None:
    match decision:
        case _IgnoredCleanupCandidate():
            return
        case _RetainedCleanupCandidate(run_key_name, reported_candidate):
            entry = (
                None
                if reported_candidate is None
                else _retained_workspace_entry(reported_candidate)
            )
            selection.record_retained_run(run_key_name, entry)
        case _SelectedCleanupCandidate(candidate):
            entry = _cleanup_workspace_candidate(candidate, request)
            selection.record_candidate_entry(entry)


def _retained_workspace_entry(
    candidate: _WorkspaceCleanupCandidate,
) -> WorkspaceCleanupEntry:
    return WorkspaceCleanupEntry(
        path=candidate.workspace_path,
        run_key_name=candidate.run_key_name,
        size_bytes=worktree_disk_usage(candidate.workspace_path),
        removed=False,
        status=candidate.status,
        orphan=candidate.status is None,
        retained_reason=(candidate.eligibility.reason or "cleanup eligibility unknown"),
    )


def _cleanup_workspace_candidate(
    candidate: _WorkspaceCleanupCandidate,
    request: _WorkspaceCleanupRequest,
) -> WorkspaceCleanupEntry:
    size_bytes = worktree_disk_usage(candidate.workspace_path)
    if not request.dry_run:
        remove_unknown_workspace_path(
            candidate.workspace_path,
            request.cleanup_filter.expected_common_git_dir,
            candidate.eligibility.expected_worktree_git_dir,
        )
        _update_deleted_state_paths(candidate.eligibility.state_paths)
    return WorkspaceCleanupEntry(
        path=candidate.workspace_path,
        run_key_name=candidate.run_key_name,
        size_bytes=size_bytes,
        removed=not request.dry_run,
        status=candidate.status,
        orphan=candidate.status is None,
    )


def _cleanup_selected_run_refs(
    request: _WorkspaceCleanupRequest,
    selection: _WorkspaceCleanupSelection,
) -> int:
    if request.dry_run or request.ref_cleanup is None:
        return 0
    cleanup_run_keys = (
        selection.selected_run_keys - selection.retained_run_keys
    ) | set(request.ref_cleanup_run_keys)
    return sum(
        request.ref_cleanup(run_key_name) for run_key_name in sorted(cleanup_run_keys)
    )


def _reconcile_absent_state_projections(
    request: _WorkspaceCleanupRequest,
    selection: _WorkspaceCleanupSelection,
) -> None:
    for projection_values in request.absent_state_projections:
        projection = AbsentWorkspaceStateProjection(*projection_values)
        if not _projection_targets_requested_run(
            projection,
            request.cleanup_filter,
        ):
            continue
        if not _absent_projection_matches_filter(
            projection,
            request.cleanup_filter,
        ) or not _workspace_path_is_confirmed_absent(projection.workspace_path):
            selection.record_retained_run(projection.run_key_name)
            continue
        _update_deleted_state_paths(projection.state_paths)
        selection.record_selected_run(projection.run_key_name)


def _projection_targets_requested_run(
    projection: AbsentWorkspaceStateProjection,
    cleanup_filter: WorkspaceCleanupFilter,
) -> bool:
    return cleanup_filter.run_key_name in (None, projection.run_key_name)


def _absent_projection_matches_filter(
    projection: AbsentWorkspaceStateProjection,
    cleanup_filter: WorkspaceCleanupFilter,
) -> bool:
    return cleanup_filter.older_than_seconds is None and status_matches(
        projection.status,
        cleanup_filter,
    )


def _workspace_path_is_confirmed_absent(workspace_path: Path) -> bool:
    try:
        workspace_path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _update_deleted_state_paths(state_paths: Iterable[Path]) -> None:
    for state_path in state_paths:
        update_workspace_retention(
            state_path,
            WorkspaceStateRetention("deleted"),
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
    if status is None:
        return cleanup_filter.orphans
    if not cleanup_filter.statuses and not cleanup_filter.orphans:
        return status in {"succeeded", "failed", "cancelled"} or (
            cleanup_filter.repository_id is None and status == "unknown"
        )
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
