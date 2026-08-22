"""Detect drift in shared reserved artifacts and runtime logs."""

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
    EventLogAppendCapture,
)
from .capture import (
    capture_consistent_publication_snapshot,
    shared_reserved_directory_snapshot,
    shared_reserved_snapshot,
)
from .comparison import (
    allowed_directory_paths,
    detect_artifact_drift,
    detect_event_log_drift,
    detect_strict_file_drift,
    include_current_runtime_publications,
    is_fatal_drift_path,
    merge_drift_results,
    suppress_new_ancestor_directory_drift,
)
from .snapshots import manifests_dir_for, read_file_bytes


def detect_shared_reserved_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
) -> DriftCheckResult:
    publications = _request_publications(request)
    after_files, after_directories, expected_publications = (
        _stable_shared_reserved_snapshot(request, publications)
    )
    after_files, expected_publications = _reconcile_reserved_publications(
        request,
        publications,
        after_files,
        expected_publications,
    )
    file_drift = _detect_reserved_file_drift(
        request,
        monitoring_window,
        after_files,
        expected_publications,
    )
    if monitoring_window.shared_reserved_directory_snapshot is None:
        return file_drift
    directory_drift = _detect_reserved_directory_drift(
        request,
        monitoring_window.shared_reserved_directory_snapshot,
        after_directories,
        expected_publications,
        file_drift,
    )
    return merge_drift_results(file_drift, directory_drift)


def _request_publications(
    request: DriftGuardCallRequest,
) -> RuntimePublicationRegistry | None:
    publications = request.runtime_publications
    if publications is None and request.drift_session is not None:
        publications = request.drift_session.runtime_publications
    return publications


def _reconcile_reserved_publications(
    request: DriftGuardCallRequest,
    publications: RuntimePublicationRegistry | None,
    after_snapshot: dict[Path, tuple[int, str]],
    expected_publications: dict[Path, tuple[int, str]],
) -> tuple[dict[Path, tuple[int, str]], dict[Path, tuple[int, str]]]:
    if publications is None:
        return after_snapshot, expected_publications
    expected_publications = {
        path: signature
        for path, signature in expected_publications.items()
        if is_fatal_drift_path(path, request.output)
        and not path.is_relative_to(request.node_dir)
    }
    return (
        include_current_runtime_publications(
            after_snapshot,
            expected_publications,
        ),
        expected_publications,
    )


def _detect_reserved_file_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    after_snapshot: dict[Path, tuple[int, str]],
    expected_publications: dict[Path, tuple[int, str]],
) -> DriftCheckResult:
    return detect_artifact_drift(
        before_snapshot=monitoring_window.shared_reserved_snapshot or {},
        after_snapshot=after_snapshot,
        allowed_paths=request.allowed_paths,
        output=request.output,
        expected_runtime_publications=expected_publications,
    )


def _detect_reserved_directory_drift(
    request: DriftGuardCallRequest,
    before_snapshot: dict[Path, DirectorySnapshot],
    after_snapshot: dict[Path, DirectorySnapshot],
    expected_publications: dict[Path, tuple[int, str]],
    file_drift: DriftCheckResult,
) -> DriftCheckResult:
    directory_drift = detect_artifact_drift(
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        allowed_paths=allowed_directory_paths(
            expected_publications,
            (
                request.output.results_dir,
                manifests_dir_for(request.output),
                request.output.get_run_log_dir(),
            ),
        ),
        output=request.output,
    )
    return suppress_new_ancestor_directory_drift(
        directory_drift,
        file_drift,
        before_snapshot,
    )


def _stable_shared_reserved_snapshot(
    request: DriftGuardCallRequest,
    publications: RuntimePublicationRegistry | None,
) -> tuple[
    dict[Path, tuple[int, str]],
    dict[Path, DirectorySnapshot],
    dict[Path, tuple[int, str]],
]:
    if publications is None:
        return (
            shared_reserved_snapshot(request.output),
            shared_reserved_directory_snapshot(request.output),
            {},
        )

    def capture() -> tuple[
        dict[Path, tuple[int, str]],
        dict[Path, DirectorySnapshot],
    ]:
        return (
            shared_reserved_snapshot(request.output),
            shared_reserved_directory_snapshot(request.output),
        )

    (files, directories), expected_publications = (
        capture_consistent_publication_snapshot(
            publications,
            capture,
            (RuntimeError,),
        )
    )
    return files, directories, expected_publications


def detect_summary_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
) -> DriftCheckResult:
    summary_path = request.output.get_run_summary_path()
    try:
        summary_after = read_file_bytes(summary_path)
    except (OSError, RuntimeError, ValueError):
        return DriftCheckResult(fatal_paths=(summary_path,))
    return detect_strict_file_drift(
        summary_path,
        monitoring_window.summary_before,
        summary_after,
    )


def detect_event_log_append_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    event_log_capture: EventLogAppendCapture | None,
    event_log_start_index: int,
    check_shared_reserved_drift: bool,
) -> DriftCheckResult:
    event_log_path = request.output.get_run_event_log_path()
    expected_append = _expected_event_append(
        event_log_capture,
        event_log_start_index,
    )
    publications = _request_publications(request)
    observation = _capture_event_log_observation(
        event_log_path,
        publications,
        monitoring_window.event_publication_cursor,
        event_log_capture,
    )
    if isinstance(observation, DriftCheckResult):
        return observation
    return detect_event_log_drift(
        event_log_path=event_log_path,
        before=monitoring_window.event_log_before,
        after=observation.after,
        expected_append=expected_append,
        strict_expected_append=(
            observation.registered_append is not None or check_shared_reserved_drift
        ),
        registered_append=observation.registered_append,
        registered_owned_append=observation.registered_owned_append,
    )


@dataclass(frozen=True, slots=True)
class _EventLogObservation:
    after: bytes | None
    registered_append: bytes | None = None
    registered_owned_append: bytes | None = None


type _EventLogObservationResult = _EventLogObservation | DriftCheckResult


def _capture_event_log_observation(
    event_log_path: Path,
    publications: RuntimePublicationRegistry | None,
    publication_cursor: int | None,
    event_log_capture: EventLogAppendCapture | None,
) -> _EventLogObservationResult:
    if publications is None or publication_cursor is None:
        return _read_event_log_observation(event_log_path)
    with publications.event_observation():
        observation = _read_event_log_observation(event_log_path)
        if isinstance(observation, DriftCheckResult):
            return observation
        registered_append, registered_owned_append = _registered_event_appends(
            publications,
            publication_cursor,
            event_log_capture,
        )
    return _EventLogObservation(
        after=observation.after,
        registered_append=registered_append,
        registered_owned_append=registered_owned_append,
    )


def _read_event_log_observation(event_log_path: Path) -> _EventLogObservationResult:
    try:
        event_log_after = read_file_bytes(event_log_path)
    except (OSError, RuntimeError, ValueError):
        return DriftCheckResult(fatal_paths=(event_log_path,))
    return _EventLogObservation(after=event_log_after)


def _expected_event_append(
    event_log_capture: EventLogAppendCapture | None,
    event_log_start_index: int,
) -> bytes:
    if event_log_capture is None:
        return b""
    return event_log_capture.expected_append_bytes_since(event_log_start_index)


def _registered_event_appends(
    publications: RuntimePublicationRegistry,
    publication_cursor: int,
    event_log_capture: EventLogAppendCapture | None,
) -> tuple[bytes, bytes | None]:
    registered_publications = publications.event_publications_since(publication_cursor)
    registered_append = b"".join(
        publication.line for publication in registered_publications
    )
    if event_log_capture is None:
        return registered_append, None
    registered_owned_append = b"".join(
        publication.line
        for publication in registered_publications
        if publication.owner_id == event_log_capture.owner_id
    )
    return registered_append, registered_owned_append
