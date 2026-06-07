from __future__ import annotations

import hashlib
from pathlib import Path

from orchestrator_cli.architecture.ports import ArtifactStorePort

from ..common import ExecutionTelemetry
from .types import (
    ActivityWindow,
    DriftCheckResult,
    DriftGuardCallRequest,
    DriftMonitoringWindow,
    EventLogAppendCapture,
)


def file_snapshot_signature(file_path: Path) -> tuple[int, str]:
    payload = file_path.read_bytes()
    return len(payload), hashlib.sha256(payload).hexdigest()


def snapshot_files(
    root: Path,
    excluded_paths: set[Path] | None = None,
) -> dict[Path, tuple[int, str]]:
    excluded = excluded_paths or set()
    if not root.exists():
        return {}
    if root.is_file():
        if root in excluded:
            return {}
        return {root: file_snapshot_signature(root)}

    snapshot: dict[Path, tuple[int, str]] = {}
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path in excluded:
            continue
        snapshot[file_path] = file_snapshot_signature(file_path)
    return snapshot


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


def read_file_bytes(file_path: Path) -> bytes | None:
    if not file_path.exists():
        return None
    return file_path.read_bytes()


def shared_reserved_snapshot(
    node_dir: Path,
    output: ArtifactStorePort,
) -> dict[Path, tuple[int, str]]:
    manifests_dir = node_dir.parent / "manifests"
    run_log_dir = output.get_run_log_dir()
    excluded_run_log_paths = {
        output.get_orchestrator_event_log_path(),
        output.get_orchestrator_summary_path(),
    }
    snapshot: dict[Path, tuple[int, str]] = {}
    for root in (output.results_dir, manifests_dir):
        snapshot.update(snapshot_files(root))
    snapshot.update(snapshot_files(run_log_dir, excluded_paths=excluded_run_log_paths))
    return snapshot


def capture_drift_monitoring_window(
    node_id: str,
    node_dir: Path,
    output: ArtifactStorePort,
    telemetry: ExecutionTelemetry | None,
) -> DriftMonitoringWindow:
    activity_window = capture_activity_window(telemetry, node_id)
    reserved_snapshot = (
        shared_reserved_snapshot(node_dir, output)
        if activity_window.is_exclusive
        else None
    )
    return DriftMonitoringWindow(
        node_snapshot=snapshot_files(node_dir),
        shared_reserved_snapshot=reserved_snapshot,
        summary_before=read_file_bytes(output.get_orchestrator_summary_path()),
        event_log_before=read_file_bytes(output.get_orchestrator_event_log_path()),
        activity_window=activity_window,
    )


def should_check_shared_reserved_drift(
    activity_window: ActivityWindow,
    telemetry: ExecutionTelemetry | None,
    node_id: str,
) -> bool:
    if not activity_window.is_exclusive:
        return False
    if telemetry is None or telemetry.activity_tracker is None:
        return True
    current_window = telemetry.activity_tracker.snapshot(node_id)
    return (
        current_window.is_exclusive
        and current_window.version == activity_window.version
    )


def is_fatal_drift_path(path: Path, output: ArtifactStorePort, node_dir: Path) -> bool:
    manifests_dir = node_dir.parent / "manifests"
    run_log_dir = output.get_run_log_dir()
    if path.is_relative_to(output.results_dir):
        return True
    if path.is_relative_to(manifests_dir):
        return True
    return path.is_relative_to(run_log_dir)


def detect_artifact_drift(
    before_snapshot: dict[Path, tuple[int, str]],
    after_snapshot: dict[Path, tuple[int, str]],
    allowed_paths: set[Path],
    output: ArtifactStorePort,
    node_dir: Path,
) -> DriftCheckResult:
    changed_paths = sorted({*before_snapshot.keys(), *after_snapshot.keys()})
    unexpected_paths = [
        path
        for path in changed_paths
        if before_snapshot.get(path) != after_snapshot.get(path)
        and path not in allowed_paths
    ]
    if not unexpected_paths:
        return DriftCheckResult()

    fatal_paths = tuple(
        path
        for path in unexpected_paths
        if is_fatal_drift_path(path, output=output, node_dir=node_dir)
    )
    warning_paths = tuple(
        path for path in unexpected_paths if path not in set(fatal_paths)
    )
    return DriftCheckResult(
        warning_paths=warning_paths,
        fatal_paths=fatal_paths,
    )


def detect_strict_file_drift(
    file_path: Path,
    before: bytes | None,
    after: bytes | None,
) -> DriftCheckResult:
    if before == after:
        return DriftCheckResult()
    return DriftCheckResult(fatal_paths=(file_path,))


def detect_event_log_drift(
    event_log_path: Path,
    before: bytes | None,
    after: bytes | None,
    expected_append: bytes,
    strict_expected_append: bool,
) -> DriftCheckResult:
    if before is None and after is None:
        return DriftCheckResult()
    if before is None:
        if not strict_expected_append:
            return DriftCheckResult()
        if expected_append and after == expected_append:
            return DriftCheckResult()
        return DriftCheckResult(fatal_paths=(event_log_path,))
    if after is None:
        return DriftCheckResult(fatal_paths=(event_log_path,))
    if len(after) < len(before) or not after.startswith(before):
        return DriftCheckResult(fatal_paths=(event_log_path,))
    if not strict_expected_append:
        return DriftCheckResult()
    if after[len(before) :] != expected_append:
        return DriftCheckResult(fatal_paths=(event_log_path,))
    return DriftCheckResult()


def merge_drift_results(*results: DriftCheckResult) -> DriftCheckResult:
    warning_paths = tuple(
        sorted({path for result in results for path in result.warning_paths})
    )
    fatal_paths = tuple(
        sorted({path for result in results for path in result.fatal_paths})
    )
    return DriftCheckResult(
        warning_paths=warning_paths,
        fatal_paths=fatal_paths,
    )


def detect_provider_call_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    event_log_capture: EventLogAppendCapture | None,
    event_log_start_index: int,
) -> DriftCheckResult:
    check_shared_reserved_drift = should_check_shared_reserved_drift(
        monitoring_window.activity_window,
        request.telemetry,
        request.node.id,
    )
    return merge_drift_results(
        detect_node_drift(request, monitoring_window),
        detect_shared_reserved_drift(
            request,
            monitoring_window,
            check_shared_reserved_drift,
        ),
        detect_summary_drift(request, monitoring_window),
        detect_event_log_append_drift(
            request,
            monitoring_window,
            event_log_capture,
            event_log_start_index,
            check_shared_reserved_drift,
        ),
    )


def detect_node_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
) -> DriftCheckResult:
    return detect_artifact_drift(
        before_snapshot=monitoring_window.node_snapshot,
        after_snapshot=snapshot_files(request.node_dir),
        allowed_paths=request.allowed_paths,
        output=request.output,
        node_dir=request.node_dir,
    )


def detect_shared_reserved_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    check_shared_reserved_drift: bool,
) -> DriftCheckResult:
    if not check_shared_reserved_drift:
        return DriftCheckResult()
    return detect_artifact_drift(
        before_snapshot=monitoring_window.shared_reserved_snapshot or {},
        after_snapshot=shared_reserved_snapshot(request.node_dir, request.output),
        allowed_paths=request.allowed_paths,
        output=request.output,
        node_dir=request.node_dir,
    )


def detect_summary_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
) -> DriftCheckResult:
    summary_path = request.output.get_orchestrator_summary_path()
    return detect_strict_file_drift(
        summary_path,
        monitoring_window.summary_before,
        read_file_bytes(summary_path),
    )


def detect_event_log_append_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    event_log_capture: EventLogAppendCapture | None,
    event_log_start_index: int,
    check_shared_reserved_drift: bool,
) -> DriftCheckResult:
    event_log_path = request.output.get_orchestrator_event_log_path()
    expected_append = (
        event_log_capture.expected_append_bytes_since(event_log_start_index)
        if event_log_capture is not None
        else b""
    )
    return detect_event_log_drift(
        event_log_path,
        monitoring_window.event_log_before,
        read_file_bytes(event_log_path),
        expected_append,
        check_shared_reserved_drift,
    )
