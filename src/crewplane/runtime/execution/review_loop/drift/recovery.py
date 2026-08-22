from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.safe_files import ensure_contained_directory
from crewplane.runtime.execution.publication_registry import (
    RuntimePublicationRegistry,
)

from ..types import DirectorySnapshot, DriftGuardCallRequest, DriftMonitoringWindow
from .snapshots import lstat_or_none, manifests_dir_for


@dataclass(frozen=True, slots=True)
class _ArtifactRecoveryState:
    original_payloads: dict[Path, bytes]
    original_directories: set[Path]
    directory_snapshots: dict[Path, DirectorySnapshot]
    event_log_path: Path
    event_log_payload: bytes | None
    restore_roots: tuple[Path, ...]
    publications: RuntimePublicationRegistry | None


def restore_fatal_artifacts(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    fatal_paths: tuple[Path, ...],
) -> None:
    if not fatal_paths:
        return
    publications = request.runtime_publications
    if publications is None and request.drift_session is not None:
        publications = request.drift_session.runtime_publications
    if publications is None:
        publications = request.runtime_context.runtime_publications
    with publications.transaction():
        _restore_fatal_artifacts(
            request,
            monitoring_window,
            fatal_paths,
            publications,
        )


def _restore_fatal_artifacts(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    fatal_paths: tuple[Path, ...],
    publications: RuntimePublicationRegistry | None,
) -> None:
    state = _build_artifact_recovery_state(
        request,
        monitoring_window,
        fatal_paths,
        publications,
    )
    restored_files: set[Path] = set()
    for path in sorted(fatal_paths, key=lambda item: (len(item.parts), item)):
        if any(parent in restored_files for parent in path.parents):
            continue
        _ensure_restore_parent(path, state.restore_roots)
        if path in state.original_directories:
            _restore_directory(path, state.directory_snapshots[path])
            continue
        if _restore_file_artifact(path, state):
            restored_files.add(path)


def _build_artifact_recovery_state(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    fatal_paths: tuple[Path, ...],
    publications: RuntimePublicationRegistry | None,
) -> _ArtifactRecoveryState:
    event_log_path = request.output.get_run_event_log_path()
    return _ArtifactRecoveryState(
        original_payloads=_original_recovery_payloads(request, monitoring_window),
        original_directories={
            *monitoring_window.node_original_directories,
            *monitoring_window.shared_reserved_original_directories,
        },
        directory_snapshots={
            **monitoring_window.node_directory_snapshot,
            **(monitoring_window.shared_reserved_directory_snapshot or {}),
        },
        event_log_path=event_log_path,
        event_log_payload=(
            _event_log_recovery_payload(monitoring_window, publications)
            if event_log_path in fatal_paths
            else None
        ),
        restore_roots=(
            request.node_dir,
            request.output.results_dir,
            manifests_dir_for(request.output),
            request.output.get_run_log_dir(),
        ),
        publications=publications,
    )


def _original_recovery_payloads(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
) -> dict[Path, bytes]:
    original_payloads = {
        **monitoring_window.node_original_bytes,
        **monitoring_window.shared_reserved_original_bytes,
    }
    if monitoring_window.summary_before is not None:
        summary_path = request.output.get_run_summary_path()
        original_payloads[summary_path] = monitoring_window.summary_before
    return original_payloads


def _restore_file_artifact(path: Path, state: _ArtifactRecoveryState) -> bool:
    if path == state.event_log_path:
        original = state.event_log_payload
    elif state.publications is not None and _atomic_restore_registered(
        path,
        state.publications,
    ):
        return True
    else:
        original = state.original_payloads.get(path)
    if original is None:
        _remove_existing_path(path)
        return False
    _remove_existing_directory(path)
    _atomic_restore(path, original)
    return True


def _event_log_recovery_payload(
    monitoring_window: DriftMonitoringWindow,
    publications: RuntimePublicationRegistry | None,
) -> bytes | None:
    baseline = monitoring_window.event_log_before
    cursor = monitoring_window.event_publication_cursor
    if publications is None or cursor is None:
        return baseline
    registered_append = b"".join(
        publication.line
        for publication in publications.event_publications_since(cursor)
    )
    if baseline is None and not registered_append:
        return None
    return (baseline or b"") + registered_append


def _ensure_restore_parent(path: Path, roots: tuple[Path, ...]) -> None:
    for root in roots:
        if path == root:
            return
        try:
            relative_parent = path.parent.relative_to(root)
        except ValueError:
            continue
        ensure_contained_directory(
            root,
            "" if relative_parent == Path(".") else relative_parent.as_posix(),
        )
        return
    raise RuntimeError(f"Cannot safely restore artifact outside reserved roots: {path}")


def _atomic_restore(path: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _atomic_restore_registered(
    path: Path,
    publications: RuntimePublicationRegistry,
) -> bool:
    temporary_path: Path | None = None
    try:
        _remove_existing_directory(path)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            if not publications.copy_recovery_payload_to(path, temporary):
                return False
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        return True
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _restore_directory(
    path: Path,
    expected_snapshot: DirectorySnapshot,
) -> None:
    path_stat = lstat_or_none(path)
    if path_stat is None or not stat.S_ISDIR(path_stat.st_mode):
        _remove_existing_path(path)
        ensure_contained_directory(path.parent, path.name)
        path_stat = path.lstat()
    ownership_changed = (
        path_stat.st_uid != expected_snapshot.user_id
        or path_stat.st_gid != expected_snapshot.group_id
    )
    if ownership_changed:
        os.chown(path, expected_snapshot.user_id, expected_snapshot.group_id)
    expected_mode = stat.S_IMODE(expected_snapshot.mode)
    if ownership_changed or stat.S_IMODE(path_stat.st_mode) != expected_mode:
        path.chmod(expected_mode)


def _remove_existing_directory(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(path_stat.st_mode) and not stat.S_ISLNK(path_stat.st_mode):
        shutil.rmtree(path)


def _remove_existing_path(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(path_stat.st_mode) and not stat.S_ISLNK(path_stat.st_mode):
        shutil.rmtree(path)
        return
    path.unlink()
