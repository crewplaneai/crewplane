"""Detect drift within a provider call's node artifact directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crewplane.runtime.execution.publication_registry import (
    RuntimePublicationRegistry,
)

from ..types import (
    DirectorySnapshot,
    DriftCheckResult,
    DriftGuardCallRequest,
    DriftMonitoringWindow,
    GeneratedFileDriftAllowance,
)
from .comparison import (
    allowed_directory_paths,
    detect_artifact_drift,
    include_current_runtime_publications,
    merge_drift_results,
    same_directory_identity_and_access,
    suppress_new_ancestor_directory_drift,
)
from .snapshots import (
    snapshot_directories,
    snapshot_files,
    snapshot_path_is_within_roots,
)


@dataclass(frozen=True)
class _NodeSnapshotBoundary:
    generated_publications: dict[Path, tuple[int, str]]
    in_progress_generated_roots: set[Path]
    generated_version: int | None
    runtime_publications: dict[Path, tuple[int, str]]
    runtime_publication_owners: dict[Path, str]
    runtime_version: int | None


type _StableNodeSnapshot = tuple[
    dict[Path, tuple[int, str]],
    dict[Path, DirectorySnapshot],
    _NodeSnapshotBoundary,
]


def detect_node_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
) -> DriftCheckResult:
    allowance, runtime_publications = _node_snapshot_sources(request)
    after_snapshot, after_directory_snapshot, boundary = _stable_node_snapshot(
        request.node_dir,
        allowance,
        runtime_publications,
        request.runtime_owned_paths,
        request.runtime_owned_roots,
    )
    expected_publications = _expected_node_publications(request, boundary)
    after_snapshot = include_current_runtime_publications(
        after_snapshot,
        expected_publications,
    )
    file_drift = _detect_node_file_drift(
        request,
        monitoring_window,
        after_snapshot,
        boundary.in_progress_generated_roots,
        expected_publications,
    )
    directory_drift = _detect_node_directory_drift(
        request,
        monitoring_window,
        after_directory_snapshot,
        boundary.in_progress_generated_roots,
        expected_publications,
        file_drift,
    )
    drift = _suppress_foreign_protected_publications(
        request,
        merge_drift_results(file_drift, directory_drift),
        after_snapshot,
        boundary,
    )
    return _promote_fatal_node_mutations(
        request,
        monitoring_window,
        drift,
    )


def _suppress_foreign_protected_publications(
    request: DriftGuardCallRequest,
    drift: DriftCheckResult,
    after_snapshot: dict[Path, tuple[int, str]],
    boundary: _NodeSnapshotBoundary,
) -> DriftCheckResult:
    foreign_publications = {
        path
        for path in request.protected_paths
        if (
            boundary.runtime_publication_owners.get(path) is not None
            and boundary.runtime_publication_owners[path]
            != request.publication_owner_id
            and after_snapshot.get(path) == boundary.runtime_publications.get(path)
        )
    }
    if not foreign_publications:
        return drift
    return DriftCheckResult(
        warning_paths=tuple(
            path for path in drift.warning_paths if path not in foreign_publications
        ),
        fatal_paths=tuple(
            path for path in drift.fatal_paths if path not in foreign_publications
        ),
    )


def _node_snapshot_sources(
    request: DriftGuardCallRequest,
) -> tuple[
    GeneratedFileDriftAllowance | None,
    RuntimePublicationRegistry | None,
]:
    allowance = request.generated_file_allowance
    runtime_publications = request.runtime_publications
    if request.drift_session is None:
        return allowance, runtime_publications
    if allowance is None:
        allowance = request.drift_session.generated_file_allowance
    if runtime_publications is None:
        runtime_publications = request.drift_session.runtime_publications
    return allowance, runtime_publications


def _expected_node_publications(
    request: DriftGuardCallRequest,
    boundary: _NodeSnapshotBoundary,
) -> dict[Path, tuple[int, str]]:
    in_progress_roots = boundary.in_progress_generated_roots
    expected_publications = {
        path: signature
        for path, signature in boundary.generated_publications.items()
        if not snapshot_path_is_within_roots(path, in_progress_roots)
    }
    expected_publications.update(
        {
            path: signature
            for path, signature in boundary.runtime_publications.items()
            if path.is_relative_to(request.node_dir)
            and path not in request.protected_paths
            and not snapshot_path_is_within_roots(path, in_progress_roots)
        }
    )
    return expected_publications


def _detect_node_file_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    after_snapshot: dict[Path, tuple[int, str]],
    in_progress_runtime_roots: set[Path],
    expected_publications: dict[Path, tuple[int, str]],
) -> DriftCheckResult:
    return detect_artifact_drift(
        before_snapshot=monitoring_window.node_snapshot,
        after_snapshot=after_snapshot,
        allowed_paths=request.allowed_paths,
        output=request.output,
        in_progress_runtime_roots=in_progress_runtime_roots,
        expected_runtime_publications=expected_publications,
    )


def _detect_node_directory_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    after_snapshot: dict[Path, DirectorySnapshot],
    in_progress_runtime_roots: set[Path],
    expected_publications: dict[Path, tuple[int, str]],
    file_drift: DriftCheckResult,
) -> DriftCheckResult:
    directory_drift = detect_artifact_drift(
        before_snapshot=monitoring_window.node_directory_snapshot,
        after_snapshot=after_snapshot,
        allowed_paths=allowed_directory_paths(
            {
                *request.allowed_paths,
                *expected_publications,
                *in_progress_runtime_roots,
                *request.runtime_owned_paths,
                *request.runtime_owned_roots,
            },
            request.node_dir,
        ),
        output=request.output,
        in_progress_runtime_roots=in_progress_runtime_roots,
    )
    directory_drift = suppress_new_ancestor_directory_drift(
        directory_drift,
        file_drift,
        monitoring_window.node_directory_snapshot,
    )
    return _suppress_added_child_directory_drift(
        directory_drift,
        file_drift,
        monitoring_window.node_directory_snapshot,
        after_snapshot,
        request.protected_paths,
    )


def _promote_fatal_node_mutations(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    drift: DriftCheckResult,
) -> DriftCheckResult:
    fatal_mutations = tuple(
        path
        for path in drift.warning_paths
        if (
            path in monitoring_window.node_snapshot
            or path in monitoring_window.node_directory_snapshot
            or path in request.protected_paths
        )
        and path not in request.runtime_owned_paths
        and not snapshot_path_is_within_roots(path, request.runtime_owned_roots)
    )
    if not fatal_mutations:
        return drift
    return DriftCheckResult(
        warning_paths=tuple(
            path for path in drift.warning_paths if path not in fatal_mutations
        ),
        fatal_paths=tuple(sorted({*drift.fatal_paths, *fatal_mutations})),
    )


def _stable_node_snapshot(
    node_dir: Path,
    allowance: GeneratedFileDriftAllowance | None,
    runtime_publications: RuntimePublicationRegistry | None,
    runtime_owned_paths: set[Path],
    runtime_owned_roots: set[Path],
) -> _StableNodeSnapshot:
    def capture() -> _StableNodeSnapshot | None:
        return _capture_node_snapshot_once(
            node_dir,
            allowance,
            runtime_publications,
            runtime_owned_paths,
            runtime_owned_roots,
        )

    attempt: _StableNodeSnapshot | None = None
    while attempt is None:
        attempt = capture()
    return attempt


def _capture_node_snapshot_once(
    node_dir: Path,
    allowance: GeneratedFileDriftAllowance | None,
    runtime_publications: RuntimePublicationRegistry | None,
    runtime_owned_paths: set[Path],
    runtime_owned_roots: set[Path],
) -> _StableNodeSnapshot | None:
    before = _node_snapshot_boundary(allowance, runtime_publications)
    try:
        files, directories = _capture_node_artifact_snapshots(
            node_dir,
            before,
            runtime_owned_paths,
            runtime_owned_roots,
        )
    except RuntimeError:
        if _node_snapshot_boundary(allowance, runtime_publications) == before:
            raise
        return None
    after = _node_snapshot_boundary(allowance, runtime_publications)
    if after != before:
        return None
    return files, directories, after


def _capture_node_artifact_snapshots(
    node_dir: Path,
    boundary: _NodeSnapshotBoundary,
    runtime_owned_paths: set[Path],
    runtime_owned_roots: set[Path],
) -> tuple[dict[Path, tuple[int, str]], dict[Path, DirectorySnapshot]]:
    excluded_roots = {
        *boundary.in_progress_generated_roots,
        *runtime_owned_roots,
    }
    files = snapshot_files(
        node_dir,
        excluded_paths=runtime_owned_paths,
        excluded_roots=excluded_roots,
    )
    directories = snapshot_directories(
        node_dir,
        excluded_paths=runtime_owned_paths,
        excluded_roots=excluded_roots,
    )
    return files, directories


def _node_snapshot_boundary(
    allowance: GeneratedFileDriftAllowance | None,
    runtime_publications: RuntimePublicationRegistry | None,
) -> _NodeSnapshotBoundary:
    if allowance is None:
        generated_publications: dict[Path, tuple[int, str]] = {}
        in_progress_generated_roots: set[Path] = set()
        generated_version = None
    else:
        (
            generated_publications,
            in_progress_generated_roots,
            generated_version,
        ) = allowance.snapshot()
    if runtime_publications is None:
        publications: dict[Path, tuple[int, str]] = {}
        publication_owners: dict[Path, str] = {}
        runtime_version = None
    else:
        (
            publications,
            publication_owners,
            runtime_version,
        ) = runtime_publications.snapshot_with_owners()
    return _NodeSnapshotBoundary(
        generated_publications=generated_publications,
        in_progress_generated_roots=in_progress_generated_roots,
        generated_version=generated_version,
        runtime_publications=publications,
        runtime_publication_owners=publication_owners,
        runtime_version=runtime_version,
    )


def _suppress_added_child_directory_drift(
    directory_drift: DriftCheckResult,
    file_drift: DriftCheckResult,
    before_snapshot: dict[Path, DirectorySnapshot],
    after_snapshot: dict[Path, DirectorySnapshot],
    protected_paths: set[Path],
) -> DriftCheckResult:
    changed_paths = {
        *file_drift.warning_paths,
        *file_drift.fatal_paths,
        *directory_drift.warning_paths,
        *directory_drift.fatal_paths,
    }
    return DriftCheckResult(
        warning_paths=tuple(
            path
            for path in directory_drift.warning_paths
            if not _is_derivative_directory_drift(
                path,
                before_snapshot,
                after_snapshot,
                protected_paths,
                changed_paths,
            )
        ),
        fatal_paths=tuple(
            path
            for path in directory_drift.fatal_paths
            if not _is_derivative_directory_drift(
                path,
                before_snapshot,
                after_snapshot,
                protected_paths,
                changed_paths,
            )
        ),
    )


def _is_derivative_directory_drift(
    path: Path,
    before_snapshot: dict[Path, DirectorySnapshot],
    after_snapshot: dict[Path, DirectorySnapshot],
    protected_paths: set[Path],
    changed_paths: set[Path],
) -> bool:
    before = before_snapshot.get(path)
    after = after_snapshot.get(path)
    if (
        path in protected_paths
        or before is None
        or after is None
        or not same_directory_identity_and_access(before, after)
    ):
        return False
    added_names = set(after.entry_names) - set(before.entry_names)
    if not added_names or set(before.entry_names) - set(after.entry_names):
        return False
    added_roots = {path / name for name in added_names}
    descendant_changes = {
        changed
        for changed in changed_paths
        if changed != path and changed.is_relative_to(path)
    }
    return (
        bool(descendant_changes)
        and all(
            any(changed == root or changed.is_relative_to(root) for root in added_roots)
            for changed in descendant_changes
        )
        and all(
            any(
                changed == root or changed.is_relative_to(root)
                for changed in descendant_changes
            )
            for root in added_roots
        )
    )
