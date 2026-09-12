"""Capture drift monitoring windows and recovery baselines."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.architecture.safe_files import is_single_link_regular_file
from crewplane.core.file_hashing import ContentSignature
from crewplane.runtime.execution.publication_registry import (
    RuntimePublicationRegistry,
)

from ...common import ExecutionTelemetry
from ..types import (
    ActivityWindow,
    DirectorySnapshot,
    DriftMonitoringWindow,
    DriftRecoveryBaseline,
)
from .snapshots import (
    lstat_or_none,
    manifests_dir_for,
    read_file_bytes,
    signatures_for_bytes,
    snapshot_directories,
    snapshot_file_bytes,
    snapshot_files,
)


def capture_consistent_publication_snapshot[Snapshot](
    publications: RuntimePublicationRegistry,
    capture: Callable[[], Snapshot],
    retryable_errors: tuple[type[Exception], ...],
) -> tuple[Snapshot, dict[Path, ContentSignature]]:
    attempt: tuple[Snapshot, dict[Path, ContentSignature]] | None = None
    while attempt is None:
        attempt = _capture_publication_snapshot_once(
            publications,
            capture,
            retryable_errors,
        )
    return attempt


def _capture_publication_snapshot_once[Snapshot](
    publications: RuntimePublicationRegistry,
    capture: Callable[[], Snapshot],
    retryable_errors: tuple[type[Exception], ...],
) -> tuple[Snapshot, dict[Path, ContentSignature]] | None:
    _, before_version = publications.snapshot()
    try:
        captured = capture()
    except retryable_errors:
        _, after_version = publications.snapshot()
        if after_version == before_version:
            raise
        return None
    expected_publications, after_version = publications.snapshot()
    if after_version != before_version:
        return None
    return captured, expected_publications


def capture_activity_window(
    telemetry: ExecutionTelemetry | None,
    node_id: str,
) -> ActivityWindow:
    if telemetry is None or telemetry.activity_tracker is None:
        return ActivityWindow(is_exclusive=True, version=None)
    snapshot = telemetry.activity_tracker.snapshot(node_id)
    return ActivityWindow(
        is_exclusive=snapshot.is_exclusive,
        version=snapshot.version,
    )


def _shared_reserved_scope(
    output: ArtifactStorePort,
) -> tuple[tuple[Path, set[Path] | None], ...]:
    manifests_dir = manifests_dir_for(output)
    run_log_dir = output.get_run_log_dir()
    excluded_run_log_paths = {
        output.get_run_event_log_path(),
        output.get_run_summary_path(),
    }
    return (
        (output.results_dir, None),
        (manifests_dir, None),
        (run_log_dir, excluded_run_log_paths),
    )


def shared_reserved_snapshot(
    output: ArtifactStorePort,
) -> dict[Path, ContentSignature]:
    snapshot: dict[Path, ContentSignature] = {}
    for root, excluded_paths in _shared_reserved_scope(output):
        snapshot.update(snapshot_files(root, excluded_paths=excluded_paths))
    return snapshot


def shared_reserved_original_bytes(
    output: ArtifactStorePort,
) -> dict[Path, bytes]:
    original_bytes: dict[Path, bytes] = {}
    for root, excluded_paths in _shared_reserved_scope(output):
        original_bytes.update(snapshot_file_bytes(root, excluded_paths=excluded_paths))
    return original_bytes


def capture_drift_monitoring_window(
    node_id: str,
    node_dir: Path,
    output: ArtifactStorePort,
    telemetry: ExecutionTelemetry | None,
    runtime_publications: RuntimePublicationRegistry | None = None,
    runtime_owned_paths: set[Path] | None = None,
    runtime_owned_roots: set[Path] | None = None,
    recovery_baseline: DriftRecoveryBaseline | None = None,
) -> DriftMonitoringWindow:
    if runtime_publications is None:
        return _capture_drift_monitoring_window_once(
            node_id,
            node_dir,
            output,
            telemetry,
            None,
            runtime_owned_paths,
            runtime_owned_roots,
            recovery_baseline,
        )

    def capture() -> DriftMonitoringWindow:
        return _capture_drift_monitoring_window_once(
            node_id,
            node_dir,
            output,
            telemetry,
            runtime_publications,
            runtime_owned_paths,
            runtime_owned_roots,
            recovery_baseline,
        )

    monitoring_window, _ = capture_consistent_publication_snapshot(
        runtime_publications,
        capture,
        (RuntimeError,),
    )
    return monitoring_window


def _capture_drift_monitoring_window_once(
    node_id: str,
    node_dir: Path,
    output: ArtifactStorePort,
    telemetry: ExecutionTelemetry | None,
    runtime_publications: RuntimePublicationRegistry | None,
    runtime_owned_paths: set[Path] | None,
    runtime_owned_roots: set[Path] | None,
    recovery_baseline: DriftRecoveryBaseline | None,
) -> DriftMonitoringWindow:
    activity_window = capture_activity_window(telemetry, node_id)
    baseline = recovery_baseline or _capture_drift_recovery_baseline_once(
        node_dir,
        output,
        runtime_publications,
        runtime_owned_paths,
        runtime_owned_roots,
    )
    if runtime_publications is None:
        event_log_before = read_file_bytes(output.get_run_event_log_path())
        event_publication_cursor = None
    else:
        with runtime_publications.event_observation():
            event_log_before = read_file_bytes(output.get_run_event_log_path())
            event_publication_cursor = runtime_publications.event_publication_cursor()
    return DriftMonitoringWindow(
        node_snapshot=baseline.node_snapshot,
        shared_reserved_snapshot=baseline.shared_reserved_snapshot,
        summary_before=read_file_bytes(output.get_run_summary_path()),
        event_log_before=event_log_before,
        activity_window=activity_window,
        node_original_bytes=baseline.node_original_bytes,
        shared_reserved_original_bytes=baseline.shared_reserved_original_bytes,
        node_directory_snapshot=baseline.node_directory_snapshot,
        shared_reserved_directory_snapshot=baseline.shared_reserved_directory_snapshot,
        node_original_directories=set(baseline.node_directory_snapshot),
        shared_reserved_original_directories=set(
            baseline.shared_reserved_directory_snapshot
        ),
        event_publication_cursor=event_publication_cursor,
    )


def capture_drift_recovery_baseline(
    node_dir: Path,
    output: ArtifactStorePort,
    runtime_publications: RuntimePublicationRegistry | None = None,
    runtime_owned_paths: set[Path] | None = None,
    runtime_owned_roots: set[Path] | None = None,
) -> DriftRecoveryBaseline:
    if runtime_publications is None:
        return _capture_drift_recovery_baseline_once(
            node_dir,
            output,
            None,
            runtime_owned_paths,
            runtime_owned_roots,
        )

    def capture() -> DriftRecoveryBaseline:
        return _capture_drift_recovery_baseline_once(
            node_dir,
            output,
            runtime_publications,
            runtime_owned_paths,
            runtime_owned_roots,
        )

    baseline, _ = capture_consistent_publication_snapshot(
        runtime_publications,
        capture,
        (OSError, RuntimeError, ValueError),
    )
    return baseline


def _capture_drift_recovery_baseline_once(
    node_dir: Path,
    output: ArtifactStorePort,
    runtime_publications: RuntimePublicationRegistry | None,
    runtime_owned_paths: set[Path] | None,
    runtime_owned_roots: set[Path] | None,
) -> DriftRecoveryBaseline:
    owned_paths = runtime_owned_paths or set()
    owned_roots = runtime_owned_roots or set()
    if runtime_publications is None:
        node_original_bytes = snapshot_file_bytes(
            node_dir,
            excluded_paths=owned_paths,
            excluded_roots=owned_roots,
        )
        shared_original_bytes = shared_reserved_original_bytes(output)
        node_snapshot = signatures_for_bytes(node_original_bytes)
        shared_snapshot = signatures_for_bytes(shared_original_bytes)
    else:
        node_snapshot = snapshot_files(
            node_dir,
            excluded_paths=owned_paths,
            excluded_roots=owned_roots,
        )
        shared_snapshot = shared_reserved_snapshot(output)
        _capture_recovery_snapshots(runtime_publications, node_snapshot)
        _capture_recovery_snapshots(runtime_publications, shared_snapshot)
        node_original_bytes = {}
        shared_original_bytes = {}
    node_directories = snapshot_directories(
        node_dir,
        excluded_paths=owned_paths,
        excluded_roots=owned_roots,
    )
    shared_directories = shared_reserved_directory_snapshot(output)
    return DriftRecoveryBaseline(
        node_snapshot=node_snapshot,
        shared_reserved_snapshot=shared_snapshot,
        node_original_bytes=node_original_bytes,
        shared_reserved_original_bytes=shared_original_bytes,
        node_directory_snapshot=node_directories,
        shared_reserved_directory_snapshot=shared_directories,
    )


def _capture_recovery_snapshots(
    publications: RuntimePublicationRegistry,
    signatures: dict[Path, ContentSignature],
) -> None:
    for path, signature in signatures.items():
        path_stat = lstat_or_none(path)
        if path_stat is None or not is_single_link_regular_file(path_stat):
            continue
        publications.capture_recovery_snapshot(path, signature)


def shared_reserved_directory_snapshot(
    output: ArtifactStorePort,
) -> dict[Path, DirectorySnapshot]:
    snapshot: dict[Path, DirectorySnapshot] = {}
    for root, excluded_paths in _shared_reserved_scope(output):
        snapshot.update(snapshot_directories(root, excluded_paths=excluded_paths))
    return snapshot
